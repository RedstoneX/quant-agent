import contextlib
import json as _json
import logging
import math
import re
import uuid
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from src.trading_calendar import et_now, et_today, session_date_key

from pydantic import ValidationError

from src.config import AppConfig, RiskConfig
from src.quantities import avg_dollar_volume, deployable_cash, dollar_volumes
from src.data.market import MarketDataProvider
from src.data.macro import MacroCoverage, MacroDataProvider
from src.data.event_calendar import FOMCCalendarProvider, MacroEventCalendarProvider
from src.data.news import NewsCoverage, NewsDataProvider
from src.data.news_store import NewsStore
from src.data.macro_store import MacroStore
from src.data.tech_store import TechStore
from src.agents.base import AgentResult, BaseAgent, agent_log_kwargs
from src.agents.tech_analyst import TechAnalystAgent
# Re-exported for backward-compat with tests that patch
# `src.pipeline.compute_indicators` (the name historically lived here).
from src.data.technical import compute_indicators  # noqa: F401
from src.agents.portfolio_manager import PortfolioManagerAgent
from src.agents.risk_manager import RiskManagerAgent
from src.agents.position_reviewer import PositionReviewerAgent
from src.agents.evening_analyst import EveningAnalystAgent
from src.agents.news_analyst import NewsAnalystAgent
from src.agents.macro_analyst import MacroAnalystAgent
from src.agents.earnings_analyst import EarningsAnalystAgent
from src.agents.meta_reflector import MetaReflectorAgent
from src.agents.smart_money_analyst import SmartMoneyAnalystAgent
from src.data.congressional_trading import CombinedSmartMoneyProvider, CongressionalTradingProvider
from src.data.smart_money import SECForm4Provider
from src.data.earnings import EarningsDataProvider
from src.risk.constants import (
    DEFAULT_DRAWDOWN_VOL_SENSITIVITY,
)
from src.risk.metrics import unrealized_pnl_pct
from src.risk.rules import (
    GROSS_LADDER,
    GROSS_LADDER_ALERT_PCT,
    GrossCeiling,
    PortfolioVolEstimate,
    RiskRuleEngine,
    apply_gross_ceiling,
    distance_to_forced_liquidation_pct,
    gross_exposure,
    peak_to_trough_pct,
    position_weight_pct,
    resolve_gross_ceiling,
    vol_relative_drawdown_threshold_pct,
)
from src.execution.broker import (
    AlpacaBroker,
    _get_sector,
    _split_protective_qty,
)
from src.pipeline_context import PMFacts, RunContext, SessionType
from src.pipeline_stages import (
    DecisionStage,
    ExecutionStage,
    MorningResearchStage,
    RiskStage,
    _persist_evidence,
    _record_pipeline_event,
)
from src.portfolio_constructor import PortfolioConstructor
from src.storage.db import Database
from src.cost_circuit import (
    LLMCostCircuitBreaker,
    PaidAnalysisSuspended,
    UnavailableLLMCostCircuit,
)
from src.models import (
    NewsIntelligenceReport,
    PortfolioDecision,
    RiskVerdict,
    TargetPosition,
    TechAnalysisResult,
    TechnicalIndicators,
    TradeDecision,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CarryForward:
    """Morning evidence reused on an intraday tick, with an honest status.

    `payload` is the stored object when a reusable answer exists; otherwise
    None. `status` is the data_status word the caller must write — never
    inferred from payload truthiness. `same_session` is True only when the
    stored answer is from today's session and carries a trustworthy date —
    an undated snapshot is not same-session. Holding-discipline uses that
    to refuse treating a cross-day remembered regime as proof about today.
    """

    payload: object | None
    status: str
    same_session: bool = True

    def __bool__(self) -> bool:
        """True only when a payload is present.

        Status must still be read from `.status` — truthiness is only a
        safety net so a leftover `if carried` cannot treat an empty or
        failed lookup as a successful carry.
        """
        return self.payload is not None


# audit F1: a pending_protection_restores row written BEFORE the SELL is
# submitted carries this as sell_order_id — it means "protective stops
# were cancelled but the SELL was never confirmed at the broker" (crash
# in the cancel→submit→record window). The drain pass recognises it and
# restores coverage from the broker's CURRENT position rather than
# querying a SELL order that may not exist.
_WAL_SELL_SENTINEL = "__WAL_PENDING__"

#: Ceiling on how many symbols get a company-profile lookup for PM's facts
#: block. Profiles are 30-day-cached, so this only bites on a cold cache —
#: but on a cold cache it is one network round trip per symbol, and a
#: pathological candidate list must not be able to turn a nice-to-have
#: identity block into the longest step of the morning session.
_PM_PROFILE_SYMBOL_CAP = 40

def _optional_risk_number(value) -> float | None:
    """Read an OPTIONAL numeric risk setting, or None.

    Same defensive posture as `_risk_setting` inside `TradingPipeline.__init__`
    (many tests build the pipeline against a MagicMock config, where attribute
    access auto-creates a child mock that pydantic coerces to 1.0), but for a
    setting whose absence is meaningful rather than an error — `None` lets
    `RiskConfig` apply its own documented default instead of a number nobody
    configured.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if value > 0 else None


def _risk_number(value, default: float) -> float:
    """`_optional_risk_number` with a documented fallback, for settings that
    always need a concrete number (§10.3's minimum order size)."""
    resolved = _optional_risk_number(value)
    return default if resolved is None else resolved


def _threaded_risk_settings(risk_config, *names: str) -> dict[str, float]:
    """Real numeric risk settings, keyed by field name, ready to splat into
    `RiskConfig(...)`.

    A name whose value is NOT a real number is OMITTED from the dict rather
    than replaced with a literal, so pydantic applies the field's own
    declared default and the number keeps exactly ONE home in this file's
    source. The omission case is the MagicMock config many pipeline tests
    build, where attribute access auto-creates a child mock pydantic refuses.

    ZERO PASSES THROUGH, unlike `_risk_number`. `min_position_risk_pct` is
    declared `ge=0` — zero is a legal "no floor" — so a `> 0` read would hand
    the engine a floor nobody configured while the seat's standing sheet
    rendered the configured 0. A seat briefed on a number nothing enforces is
    the defect this whole change removes.
    """
    threaded: dict[str, float] = {}
    for name in names:
        value = getattr(risk_config, name, None)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        threaded[name] = float(value)
    return threaded


def _daily_loss_limit_for_alert(engine, risk_config) -> float | None:
    """The daily circuit-breaker limit actually in force, for operator alerts.

    docs/WORK.md item 32 (owner call 2026-09-11). Prefers the engine's
    `daily_loss_limit_pct` — the volatility-relative threshold when the
    account has enough of its own history to measure one — and falls back to
    the configured fixed percentage, then to None. Never raises: this feeds
    a notification, and a broken read must not take a session alert down.
    """
    for source, attribute in (
        (engine, "daily_loss_limit_pct"),
        (risk_config, "effective_max_daily_loss_pct"),
    ):
        if source is None:
            continue
        try:
            value = getattr(source, attribute, None)
        except Exception:  # noqa: BLE001
            continue
        resolved = _optional_risk_number(value)
        if resolved is not None:
            return resolved
    return None


HARD_BLOCK_RULES = {
    "max_daily_loss_pct",
    "max_total_position_pct",
    "max_position_pct",
    "require_stop_loss",
    # Spec §10.3 (owner-ratified 2026-09-01): `max_sector_pct` is NO LONGER
    # a hard block and is deliberately absent from this set. It is now the
    # diversification TARGET — breaching it emits an ADVISORY violation the
    # AI Risk Manager and the audit trail see, while the constructor shrinks
    # the order for crowding instead of the pipeline dropping it. The hard
    # gate moved to `max_sector_hard_pct` below, which fires only past the
    # absolute ceiling or on an order that never went through that sizing.
    # Removing it from here is the whole of "concentration is a dial, not a
    # gate" at the pipeline level; putting it back reinstates the veto.
    "max_sector_hard_pct",
    "cash_only",
    # Audit §1.1: the drawdown-halve rule used to live only in the PM and RM
    # prompts, where "no deterministic code enforces this" was stated outright.
    # It is a hard gate now. `apply_drawdown_scale` halves BUYs before this
    # filter runs, so a violation here means a BUY reached the engine unscaled.
    "drawdown_buy_cap",
    # Spec §11.2 (owner-ratified 2026-09-01). Gross exposure — long market
    # value plus absolute short market value — may not exceed the ladder-
    # resolved multiple of equity. There was NO gross-exposure ceiling in
    # this codebase before: `max_portfolio_risk_pct` bounds capital at risk
    # and `max_total_position_pct` bounds NET exposure, where a hedge
    # cancels a long. Adding this hard block is a tightening.
    "max_gross_exposure",
}


# Named exit triggers — the vocabulary of NEW INFORMATION.
#
# Spec Phase 3.8: the reviewer retains full authority to exit on new
# information — adverse news, an earnings miss, a macro regime shift, a sector
# shock, a thesis invalidation. Price movement alone is
# not new information. This tuple is that list, expressed as prose the LLM
# actually emits. (Spec 3.8 also listed "a correlation breach"; that one was
# removed 2026-09-13 — see the note inside the tuple.)
#
# Soft signals — "TARGET_BREACH", "stretched", "extended", "macro noise",
# "taking profits", "de-risking" — are deliberately ABSENT and must stay
# absent. They are recurring flags, not events, and mechanically
# re-applying them is what produced the repeated same-day double-trims.
#
# Phase 3.3 (2026-08-27) widened where this gate applies. It used to guard
# only the SECOND sell-side action on a symbol in one day, so a position's
# FIRST sale — which is almost every sale — executed on soft reasoning
# entirely unchecked. It now guards every exit. Two categories were added at
# the same time, because gating every exit on a list that did not cover the
# whole of 3.8 would have blocked legitimate exits: macro regime shifts and
# sector shocks are sanctioned by 3.8 but were unrepresented here.
#
# Concentration and drift were considered for inclusion and deliberately
# REJECTED. "Concentration drift; valuation stretched" is the verbatim shape
# of the reason behind the 2026-05-04 AMZN double-trim, and drift trims belong
# to the Portfolio Manager (its rule-priority rows 4 and 5), not to this seat.
# A Tech-rating downgrade alone is likewise excluded: the Risk Manager prompt
# already states it is not sufficient grounds for an exit.
_HARD_TRIGGER_KEYWORDS: tuple[str, ...] = (
    # Thesis invalidation
    "thesis_invalid",
    "thesis invalid",
    "invalidation triggered",
    "broken thesis",
    "thesis broken",
    # Adverse company/sector news and state changes
    "high bearish",
    "high-conviction bearish",
    "high conviction bearish",
    "adverse news",
    "material news",
    "sector shock",
    # Earnings and filings
    "bearish earnings",
    "bearish filing",
    "earnings missed",
    "earnings miss",
    "guidance cut",
    # Macro regime — sanctioned by spec 3.8, previously unrepresented
    "regime shift",
    "regime flip",
    "regime flipped",
    "risk-off",
    "risk off",
    # Deterministic risk management
    "daily loss",
    "daily-loss",
    "circuit breaker",
    # "correlation breach" / "correlation cluster breach" were REMOVED
    # 2026-09-13 (WORK.md item 44). They were the only accepted triggers with
    # nothing behind them: no part of the desk computes a correlation-breach
    # EVENT, `holding_discipline_claim_check` has no branch for the claim (it
    # returns "ok" — not even the log-only "unverifiable"), and a published
    # operational definition with a stated window and threshold was searched
    # for and not found (see docs/INCIDENT_HISTORY.md). Every other keyword
    # here names something the desk records: a news row, an earnings row, a
    # macro regime read, a broker fill, a deterministic circuit breaker.
    # This one named nothing, so it passed on the wording alone. Do NOT
    # re-add it without a verifier that can answer "did that happen today?".
    # Protection already fired
    "stop hit",
    "stopped out",
)


def _reason_cites_hard_trigger(reason: str) -> bool:
    """True when the reason NAMES a recognised new-information trigger.

    Substring match, case-insensitive — the LLM emits prose, so variation is
    tolerated. The point is not to be clever about language; it is to force
    the reason to make a CLAIM ("X happened") rather than express a feeling
    ("it looks tired"). A claim is auditable, gradeable by the evening review,
    and cross-checkable against the reviewer's own metrics by
    `src/risk/exit_guard.py`. A feeling is none of those things.

    A False return on a string is a completed content judgment, not
    uncertainty: the deterministic owner refuses (see
    `src/risk/exit_refusal.py`). Callers that need to distinguish "the
    matcher could not run" from "the matcher ran and found nothing" must
    use `classify_trigger_reason`, not this boolean.
    """
    if not reason:
        return False
    lower = reason.lower()
    return any(kw in lower for kw in _HARD_TRIGGER_KEYWORDS)


def _valuation_signal_from(forward_pe: float | None) -> str:
    """Coarse valuation bucket from forward PE. Conservative thresholds:
    anything < 12 is cheap even for growth names; >= 25 is stretched for
    anything that isn't hyper-growth / secular-leader; 12-25 is fair.
    None → no_data (ETFs, newly-listed, yfinance gap). LLM reads this
    AND the raw PE/PS numbers so it can sector-adjust; the enum is the
    fast first cut that prevents obvious hype-chasing on stretched names.
    """
    if forward_pe is None:
        return "no_data"
    try:
        pe = float(forward_pe)
    except (TypeError, ValueError):
        return "no_data"
    if pe <= 0:
        # Negative / zero forward PE → loss-making; can't judge from PE
        # alone. Treat as no_data so the LLM reasons from other signals.
        return "no_data"
    if pe < 12:
        return "cheap"
    if pe >= 25:
        return "stretched"
    return "fair"


def _missed_ops_quality_metrics(
    bars: list, lookback_days: int
) -> tuple[float | None, float | None, float | None]:
    """Compute (avg_dollar_volume_20d_m, volume_confirmation_ratio,
    single_day_concentration_pct) from a list[OHLCV]-like. All three are
    independent — a symbol with only a few bars may return None for
    dollar-volume while still having a valid single-day concentration.

    Designed for the missed_opportunities digest: thin-liquidity top-
    mover symbols (dollar_vol < $5M) and single-day-gap rallies
    (concentration > 70%) shouldn't dominate the evening LLM's attention.

    Returns (None, None, None) when bars is empty or malformed.
    """
    if not bars or len(bars) < 2:
        return None, None, None

    # 20-day dollar volume via the single shared definition
    # (`src.quantities.avg_dollar_volume`) — this digest and the external-
    # symbol admission gate used to compute the same measure two different
    # ways (a halted session was dropped here and counted there, 5.26%
    # apart on a 20-bar window). Only the THRESHOLDS differ now: $5M here,
    # $10M at admission. `min_bars=5` keeps this caller's deliberate
    # tolerance for short history; the gate demands a full window.
    avg_dvol_m: float | None = None
    vol_conf_ratio: float | None = None
    try:
        dollar_vols = dollar_volumes(bars)
        avg_dvol = avg_dollar_volume(bars, min_bars=5)
        if avg_dvol is not None:
            avg_dvol_m = round(avg_dvol / 1_000_000, 2)
            # Today's dollar volume vs the average. >1.5 = buyers showed up.
            if dollar_vols and avg_dvol > 0:
                today_dvol = dollar_vols[-1]
                vol_conf_ratio = round(today_dvol / avg_dvol, 2)
    except (TypeError, ValueError, AttributeError):
        avg_dvol_m = None
        vol_conf_ratio = None

    # Single-day concentration — what fraction of the window's total return
    # came from the biggest single day? > 70% = gap-up day (event/squeeze);
    # < 50% = distributed (trend). Needs ≥ 3 bars in the window to be
    # meaningful (2 bars = one daily return = always 100%).
    window = (bars[-(lookback_days + 1):]
              if len(bars) > lookback_days else bars)
    single_day_conc: float | None = None
    try:
        if len(window) >= 3:
            daily_returns: list[float] = []
            for prev, cur in zip(window[:-1], window[1:]):
                pc_attr = getattr(prev, "close", None)
                cc_attr = getattr(cur, "close", None)
                if not (isinstance(pc_attr, (int, float))
                        and isinstance(cc_attr, (int, float))):
                    continue
                pc = float(pc_attr)
                cc = float(cc_attr)
                if pc > 0:
                    daily_returns.append((cc - pc) / pc * 100.0)
            if daily_returns:
                total = sum(daily_returns)
                max_abs = max((abs(r) for r in daily_returns), default=0.0)
                # Use absolute totals to avoid sign flips when the window
                # has both up and down days.
                if abs(total) > 0.01:
                    # Percentage of the biggest-day move against total
                    # directional move. Cap at 200 — biggest-day move can
                    # exceed total when subsequent days partially reverse.
                    conc = min(max_abs / abs(total) * 100.0, 200.0)
                    single_day_conc = round(conc, 1)
    except (TypeError, ValueError, AttributeError):
        single_day_conc = None

    return avg_dvol_m, vol_conf_ratio, single_day_conc


def _market_is_open_now(broker) -> bool:
    """Is the regular cash session open RIGHT NOW?

    Spec §11.1 hybrid fractional stops. This is the discriminator the
    whole alerting distinction rests on: a fractional DAY stop that is
    absent while the market is SHUT is the design working — it lapsed at
    16:00 ET exactly as intended and the next session re-places it. The
    same stop absent while the market is OPEN is a placement failure and
    must wake somebody.

    FAILS TOWARD "OPEN" ON PURPOSE. Every way this can be wrong has an
    asymmetric cost: believing the market is shut when it is open would
    SUPPRESS a real naked-position alert, which is the one failure this
    desk cannot absorb. Believing it is open when it is shut costs a
    redundant banner. So anything unknown, unreadable or unexpected
    answers True, and only a confident, positively-established "outside
    the session" answers False.

    The session-window table (`trading_calendar.SESSION_WINDOWS`) is the
    weekday 09:30-16:00 ET baseline; `broker.get_session_close()`
    tightens it on early-close days (Thanksgiving Friday 13:00, July 3),
    and is best-effort — a calendar failure leaves the baseline answer
    rather than inventing a closed market.

    Note the callers all sit behind `_is_trading_day()`, so a holiday
    never reaches here; the weekday check is belt-and-braces for a
    direct call.
    """
    from datetime import datetime as _dt

    try:
        from src.trading_calendar import in_session_window

        now = et_now()
        if not in_session_window("intra_check", now):
            return False
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "market-hours check failed (%s) — assuming the market is OPEN "
            "so a coverage gap still alerts", exc,
        )
        return True
    try:
        session_close = broker.get_session_close()
    except Exception as exc:  # noqa: BLE001
        logger.debug("market-hours: get_session_close failed: %s", exc)
        return True
    if isinstance(session_close, _dt) and now >= session_close:
        return False
    return True


def _limit_is_vol_relative(risk_engine) -> bool:
    """Is the daily-loss limit currently the HELD BOOK's volatility-relative
    one, rather than a fixed percentage of the account?

    docs/WORK.md item 32 — decides which day-change number `daily_loss_
    numerator` compares. Read defensively: an engine that cannot answer (an
    older stub, a Mock in a fixture) is treated as fixed-percentage, which
    is the pre-2026-09-14 behaviour and the more-negative numerator on any
    day with realized losses.
    """
    from src.risk.rules import RiskRuleEngine
    try:
        basis = risk_engine.daily_loss_limit_basis()
    except Exception:  # noqa: BLE001
        return False
    return basis == RiskRuleEngine.VOL_RELATIVE_BASIS


def _price_is_through_stop(price: float, stop_price: float, *, is_short: bool) -> bool:
    """Has the tape passed a protective stop's trigger?

    A long's protective stop is a SELL stop and fires as price FALLS
    through it; a short's is a BUY stop and fires as price RISES through
    it. Same arithmetic, mirrored.

    NO TOLERANCE, no grace band, no minimum distance and no percentage
    lives here, by design: this is a comparison of two numbers the desk
    already holds every session, which is the whole reason this detector
    could be built without inventing a constant. The inequality is STRICT,
    so "price exactly at the trigger" is deliberately NOT through it — the
    only float-equality case is resolved towards silence rather than
    towards an epsilon nobody chose.

    Returns False on any unusable number rather than guessing.
    """
    try:
        px = float(price)
        stop = float(stop_price)
    except (TypeError, ValueError):
        return False
    if not (math.isfinite(px) and math.isfinite(stop)):
        return False
    if px <= 0 or stop <= 0:
        return False
    return (px > stop) if is_short else (px < stop)


def _position_notional(position, qty: float) -> float:
    """Dollar value of `qty` shares of `position`, or 0.0 if unknowable.

    Spec §11.1 hybrid fractional stops, observability half. The owner's
    standing objection to invisible risk is that "a number he can look at
    beats a guarantee he has to trust" — so the overnight sub-share
    exposure is reported in DOLLARS, not in shares. A share count is
    meaningless across a book that holds both a $12 name and a $900 one,
    and the whole reason fractional sizing exists here is the $900 one.

    Uses the price already on the broker's position snapshot rather than
    a fresh quote: this runs inside the coverage sweep's per-position
    loop, and an extra round-trip per held name to decorate an alert
    would be paid on every sweep of every session. Returns 0.0 rather
    than guessing when the snapshot carries no usable price — an omitted
    number is honest, an invented one is not.
    """
    try:
        price = float(getattr(position, "current_price", 0) or 0)
        shares = float(qty)
    except (TypeError, ValueError):
        return 0.0
    if not (math.isfinite(price) and price > 0):
        return 0.0
    if not (math.isfinite(shares) and shares > 0):
        return 0.0
    return round(price * shares, 2)


def _classify_coverage_gap(*, held: float, covered: float) -> tuple[str, float]:
    """Name the shortfall between held shares and stop-covered shares.

    Returns ``(coverage, frac_uncovered)`` where `coverage` is one of:

    ``'none'``       zero protective coverage on a position that should
                     have some. Guard 3's worst condition; escalates.
    ``'partial'``    some coverage, but the WHOLE-SHARE part of the
                     position is under-covered. Guard 3's milder
                     condition; banner, not escalation.
    ``'fractional'`` the ONLY thing missing is the sub-share remainder —
                     the durable GTC leg over floor(held) is intact.

    Spec §11.1 hybrid fractional stops. The third value is the whole
    point: under the hybrid design a sub-share remainder loses its DAY
    stop at every close, so classifying that as 'none' (which is what a
    bare `covered <= 0` test does for a position under one share) would
    fire the NO-STOP-AT-ALL owner alert every single night on a state
    that is expected, bounded and deliberate. An alert that cries wolf
    nightly is worse than no alert, because it trains the owner to swipe
    away the one message that must never be ignored.

    Market hours are deliberately NOT an input here. This answers only
    "what is missing"; the caller decides what that means at this hour.
    Keeping the two apart is what makes the overnight suppression
    auditable — it can only ever soften a gap already known to be
    'fractional', and it is one branch at one call site rather than a
    condition smeared through the classifier.
    """
    whole_held, frac_held = _split_protective_qty(held)
    shortfall = max(0.0, held - covered)
    # The durable leg is intact iff the covered qty reaches the whole-share
    # floor of the position. Anything less means a GTC stop is missing,
    # which is never the expected overnight state.
    durable_leg_intact = covered + 1e-6 >= whole_held
    only_sub_share_missing = shortfall <= frac_held + 1e-6
    if frac_held > 0 and durable_leg_intact and only_sub_share_missing:
        return "fractional", shortfall
    return ("none" if covered <= 1e-6 else "partial"), 0.0




# ---------------------------------------------------------------------------
# The two objects that ENFORCE the desk's numeric limits.
#
# Lifted out of `TradingPipeline.__init__` so a test can build them from a
# candidate settings object and read back what the engine and the sizer would
# actually enforce. That matters because the parity these limits need is
# behavioural: `tests/test_risk_prompt_limits_live.py` asserts that a value a
# seat's standing sheet SHOWS is the value these objects CARRY. Parsing the
# source text of a keyword list could only ever prove a kwarg name was typed,
# not that the setting reached the object — a limit hard-coded at its current
# value would have satisfied it.
#
# NOTHING ELSE CHANGED IN THE MOVE. Both bodies are the code that ran inline.
# ---------------------------------------------------------------------------


def build_risk_config(config) -> RiskConfig:
    """The `RiskConfig` the deterministic risk engine is built from.

    Hand-enumerated: a declared setting left out falls back to the pydantic
    CLASS DEFAULT and settings.yaml is ignored for that field. See the
    comments inline for which are threaded and why the rest are not.
    """
    return RiskConfig(
            max_position_pct=config.risk.max_position_pct,
            max_total_position_pct=config.risk.max_total_position_pct,
            max_daily_loss_pct=config.risk.max_daily_loss_pct,
            # docs/WORK.md item 32: `effective_max_daily_loss_pct` derives
            # from these two when `max_daily_loss_pct` above is left unset,
            # so both must be threaded through here too or a future config
            # with an unset max_daily_loss_pct would silently derive against
            # this dataclass's bare defaults instead of the real settings.
            # `_risk_number` guards against the MagicMock-coerces-to-1.0
            # posture the comment above `max_sector_hard_pct` describes.
            max_position_risk_pct=_risk_number(
                getattr(config.risk, "max_position_risk_pct", None), 5.0,
            ),
            daily_loss_risk_multiple=_risk_number(
                getattr(config.risk, "daily_loss_risk_multiple", None), 3.0,
            ),
            # docs/WORK.md item 32 (owner call 2026-09-11): how many
            # multiples of the HELD BOOK's own normal daily move trip the
            # daily breaker. Threaded for the same reason as the two above —
            # an unthreaded value would silently derive against this model's
            # bare default rather than real settings.
            drawdown_vol_sensitivity=_risk_number(
                getattr(config.risk, "drawdown_vol_sensitivity", None),
                DEFAULT_DRAWDOWN_VOL_SENSITIVITY,
            ),
            max_sector_pct=config.risk.max_sector_pct,
            # Spec §10.3 — the absolute ceiling behind the sector dial.
            # Read through the same MagicMock guard `_risk_setting` applies
            # below (many tests build the pipeline against a mock config, and
            # a child mock coerces to 1.0, which would trip the "ceiling must
            # sit above the target" validator with a number nobody chose).
            # `None` means "derive 1.5x the target", which RiskConfig does.
            max_sector_hard_pct=_optional_risk_number(
                getattr(getattr(config, "risk", None), "max_sector_hard_pct", None),
            ),
            require_stop_loss=config.risk.require_stop_loss,
            # Codex r11 P2: previously omitted, defaulting to False even
            # when settings.yaml said True. Prompts + force_delever read
            # config.risk.allow_margin directly, so the agent saw "margin
            # OK" while the deterministic engine still applied cash_only.
            # Result: a user opting in to margin had their BUYs blocked
            # by a hard rule the agent didn't know was active.
            allow_margin=config.risk.allow_margin,
            # SAME OMISSION CLASS AS `allow_margin` DIRECTLY ABOVE. This
            # `RiskConfig(...)` is hand-enumerated, so any declared setting
            # left out of it silently falls back to the pydantic CLASS
            # DEFAULT and settings.yaml is ignored for that field. 22 of the
            # declared risk settings were in that state before this change;
            # today every one of those defaults happens to equal the settings
            # value, so nothing is live-wrong — it is latent, and
            # `allow_margin` directly above is the proof that it does not
            # stay latent forever.
            #
            # The seven threaded here are the ones the Risk Manager's and
            # Portfolio Manager's standing sheets now RENDER from settings.yaml (see
            # src/agents/prompt_limits.py). Rendering a value into the
            # reviewer's briefing while the engine enforced a different
            # object's default would be the same two-homes defect this
            # change removes, pointed the other way. Threading them makes
            # "the seat is briefed against what the engine enforces" true
            # rather than merely intended, and `tests/
            # test_risk_prompt_limits_live.py` now pins it.
            #
            # The other 15 are NOT touched here: they predate this work, they
            # are not live-wrong, and sweeping them would change enforcement
            # nobody has reviewed. Recorded in docs/WORK.md instead.
            # Splatted through `_threaded_risk_settings`, NOT read through
            # `_risk_number`: a `_risk_number(x, <literal>)` per field would
            # type seven more copies of seven limits into this file, which is
            # the two-homes defect this change removes, pointed inward. The
            # helper omits a non-numeric (MagicMock) read instead, leaving
            # pydantic's own field default as the single fallback home — and
            # it lets a legal 0 through, which `_risk_number` does not.
            **_threaded_risk_settings(
                getattr(config, "risk", None),
                "min_position_risk_pct",
                "max_portfolio_risk_pct",
                # Rendered into the Portfolio Manager's sheet by the same
                # mechanism, so they carry the same parity requirement.
                "max_cluster_risk_share_pct",
                "max_gross_exposure_x",
                "short_gap_risk_multiple",
            ),
    )


def build_constructor_config(config, risk_engine_config):
    """The `ConstructorConfig` the deterministic sizer is built from.

    Takes the risk engine's ALREADY-RESOLVED config rather than re-deriving
    from settings, so the ceilings the sizer shrinks against are provably the
    identical objects the engine enforces.

    This is the enforcement home for four settings the Portfolio Manager's
    standing sheet renders — `min_position_risk_pct`, `max_portfolio_risk_pct`,
    `max_cluster_risk_share_pct` and `short_gap_risk_multiple` — none of which
    `src/risk/rules.py` reads at all. The sizing seat's parity is against THIS
    object, not only against `RiskConfig`.
    """
    from src.portfolio_constructor import ConstructorConfig
    _risk_cfg = getattr(config, "risk", None)

    def _risk_setting(name: str, default: float, allow_zero: bool = False) -> float:
        """Read a risk ceiling, or the ratified default.

        Coerced through a real float check rather than trusted from
        `getattr`: many tests construct the pipeline against a MagicMock
        config, where attribute access auto-creates a child mock that is
        neither the default nor a number — and a MagicMock reaching the
        sizing arithmetic fails with an opaque TypeError deep inside the
        constructor. Same defensive posture as `_coerce_token_count`.

        `allow_zero` for the one setting where 0 is a CONFIGURED value rather
        than an absent one: `min_position_risk_pct` is declared `ge=0`, so 0
        means "no floor". Swallowing it into the default would size under a
        floor nobody configured while the sizing seat's sheet rendered the 0.
        """
        value = getattr(_risk_cfg, name, default)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return default
        if allow_zero and value >= 0:
            return float(value)
        return float(value) if value > 0 else default

    return ConstructorConfig(
            risk_budget_pct=_risk_setting("max_position_risk_pct", 5.0),
            min_risk_pct=_risk_setting("min_position_risk_pct", 0.5, allow_zero=True),
            max_portfolio_risk_pct=_risk_setting("max_portfolio_risk_pct", 25.0),
            max_cluster_risk_share_pct=_risk_setting("max_cluster_risk_share_pct", 40.0),
            # Same setting the risk engine enforces (line ~326), so the
            # constructor sizes under the ceiling rather than proposing orders
            # `max_position_pct` — a HARD_BLOCK rule — will drop outright.
            max_position_pct=_risk_setting("max_position_pct", 65.0),
            # Spec §10.3 "concentration scales size". Read back off the risk
            # ENGINE's own resolved config rather than re-derived from
            # settings, so the number the constructor shrinks against is
            # provably the identical number the engine will enforce — the
            # drift `max_position_pct`'s "keep in sync" comment can only ask
            # for, this one gets structurally.
            max_sector_pct=risk_engine_config.max_sector_pct,
            max_sector_hard_pct=risk_engine_config.sector_hard_ceiling_pct,
            # §10.3's floor — reuses the existing $500 threshold rather than
            # inventing a second notion of "too small to bother". It lives
            # under `cash_sweep` because that is where it was first needed;
            # the number, not the section, is what is being reused.
            min_order_usd=_risk_number(
                getattr(getattr(config, "cash_sweep", None), "min_order_usd", None),
                500.0,
            ),
            # Stage 3 (shorts) — the sizing haircut. A short's single-name
            # ceiling is `max_position_pct` above, the same as a long's.
            short_gap_risk_multiple=_risk_setting("short_gap_risk_multiple", 1.5),
            # Spec §11.2 — same "size under the hard block" pattern again.
            # `max_gross_exposure` is in HARD_BLOCK_RULES, so an entry that
            # breaches the ceiling would be DROPPED rather than taken
            # smaller without this. The per-session ladder step is passed to
            # `construct_orders`; this is the standing cap it starts from.
            max_gross_exposure_x=_risk_setting("max_gross_exposure_x", 2.0),
            # The cash park is not exposure. Read from the SAME config gate
            # `_sweeper()` uses (enabled + symbol) so the sizing gate and the
            # execution gate can never disagree about what counts.
            cash_park_symbol=(
                getattr(getattr(config, "cash_sweep", None), "symbol", None)
                if bool(getattr(getattr(config, "cash_sweep", None), "enabled", False))
                else None
            ),
            min_stop_atr_multiple=_risk_setting("min_stop_atr_multiple", 1.5),
            min_reward_risk_after_widening=_risk_setting(
                "min_reward_risk_after_widening", 1.5,
            ),
            # Spec §12.1 — a stop sitting at a level the system COMPUTED is
            # honoured whatever the band says, down to a deterministic 1x ATR
            # floor. Same "wire from the ratified setting, not the
            # constructor's own default" pattern as every ceiling above.
            # There is no `level_match_atr_tolerance` to wire any more: item
            # 46 (2026-09-13) deleted it, and the constructor reads the
            # match tolerance off the level zone's own definition.
            absolute_min_stop_atr_multiple=_risk_setting(
                "absolute_min_stop_atr_multiple", 1.0,
            ),
            # Phase 12.1, 2026-09-03 — how many prior touches a computed
            # level needs before the tight-stop exemption above trusts it.
            # docs/RESEARCH_FINDINGS.md §7.
            min_level_touches_for_stop_honor=int(
                _risk_setting("min_level_touches_for_stop_honor", 5),
            ),
            # Target derivation (2026-09-01) — the numerator of the ratio
            # above, computed from bars instead of guessed by the analyst.
            # Wired from the ratified settings, same pattern as every
            # ceiling above.
            min_target_atr_multiple=_risk_setting("min_target_atr_multiple", 1.0),
            breakout_projection_atr_multiple=_risk_setting(
                "breakout_projection_atr_multiple", 1.0,
            ),
            max_target_reach_atr_multiple=_risk_setting(
                "max_target_reach_atr_multiple", 1.5,
            ),
            max_stop_width_reach_atr_multiple=_risk_setting(
                "max_stop_width_reach_atr_multiple", 1.5,
            ),
            max_target_horizon_sessions=int(
                _risk_setting("max_target_horizon_sessions", 60),
            ),
            target_divergence_warn_pct=_risk_setting(
                "target_divergence_warn_pct", 25.0,
            ),
    )


def _smart_money_refresh_sources_word(congress_enabled: bool) -> str:
    """What the pre-market smart-money refresh log line should say it read.

    Congressional trading disclosures (`src/data/congressional_trading.py`)
    are only ever fetched when `config.smart_money.congress_enabled` is
    True — switched on 2026-09-20 per owner ruling (see that date's entry
    in `docs/INCIDENT_HISTORY.md`). The log line must say so honestly rather
    than always naming both sources.
    """
    if congress_enabled:
        return "SEC Form 4 + congressional"
    return "SEC Form 4 only (congressional cross-check switched off)"


class TradingPipeline:
    #: Set in __init__ from `risk.kill_switch_path`. Declared here so an
    #: instance built without __init__ (tests do this) reads None rather than
    #: raising: an unconfigured switch is INERT, never armed. Real enforcement
    #: is at the broker seam, where __init__ always runs in production.
    _kill_switch_path: "Path | None" = None
    def __init__(self, config: AppConfig):
        self.config = config
        self.market = MarketDataProvider()
        self.macro = MacroDataProvider(
            api_key=config.api_keys.fred,
            request_timeout_s=config.macro.request_timeout_s,
            max_retries=config.macro.max_retries,
            retry_backoff_base_s=config.macro.retry_backoff_base_s,
            retry_backoff_max_s=config.macro.retry_backoff_max_s,
            retry_backoff_jitter_s=config.macro.retry_backoff_jitter_s,
            breaker_after_failed_series=config.macro.breaker_after_failed_series,
            total_fetch_deadline_s=config.macro.total_fetch_deadline_s,
        )
        # Forward calendar of scheduled macro releases (FRED's free
        # release-dates API). Same host and same failure mode as `self.macro`,
        # so it reuses that feed's operator-set retry/backoff policy verbatim
        # and carries only its own, much tighter, wall-clock ceiling — see
        # src/config.py::EventRiskConfig.
        self.event_calendar = MacroEventCalendarProvider(
            api_key=config.api_keys.fred,
            request_timeout_s=config.macro.request_timeout_s,
            max_retries=config.macro.max_retries,
            retry_backoff_base_s=config.macro.retry_backoff_base_s,
            retry_backoff_max_s=config.macro.retry_backoff_max_s,
            retry_backoff_jitter_s=config.macro.retry_backoff_jitter_s,
            breaker_after_failed_releases=config.macro.breaker_after_failed_series,
            total_fetch_deadline_s=config.event_risk.calendar_deadline_s,
        )
        # FOMC meeting dates, from the Federal Reserve's own free calendar.
        # A separate provider from the one above because it is a different host
        # (federalreserve.gov, not FRED) with its own timeout, its own deadline
        # and a disk cache — see src/data/event_calendar.py's docstring for the
        # live evidence behind the source choice. The backoff CURVE is still
        # the macro feed's: that is a generic retry policy, not a host fact.
        self.fomc_calendar = FOMCCalendarProvider(
            request_timeout_s=config.event_risk.fomc_request_timeout_s,
            max_retries=config.event_risk.fomc_max_retries,
            retry_backoff_base_s=config.macro.retry_backoff_base_s,
            retry_backoff_max_s=config.macro.retry_backoff_max_s,
            retry_backoff_jitter_s=config.macro.retry_backoff_jitter_s,
            total_fetch_deadline_s=config.event_risk.fomc_deadline_s,
            cache_path=config.event_risk.fomc_cache_path,
            cache_ttl_days=config.event_risk.fomc_cache_ttl_days,
        )

        def _key_for(model: str, explicit_provider: str | None = None) -> str:
            """Return the right API key based on (explicit provider, else
            model-name prefix) — the SAME resolve_provider() BaseAgent.__init__
            uses, so this can never pick a different provider than the client
            construction it's keying for."""
            from src.agents.base import resolve_provider
            provider = resolve_provider(model, explicit_provider)
            return {
                "deepseek": config.api_keys.deepseek,
                "openai": config.api_keys.openai,
                "openrouter": config.api_keys.openrouter,
                "google": config.api_keys.google,
            }.get(provider, config.api_keys.anthropic)

        # Cross-provider failover credential — resolved ONCE from the
        # process-wide `config.llm.fallback_provider`/`fallback_model`
        # (2026-08-31 owner decision) via the SAME `_key_for` closure every
        # agent's primary key uses, so config.py's AppConfig._check_llm_
        # provider_keys and this construction site can never pick different
        # credentials for the same configured fallback.
        _fallback_api_key = _key_for(config.llm.fallback_model, config.llm.fallback_provider)
        # Route 3 credential — a genuinely DIFFERENT model (see
        # config.llm.tertiary_model). Resolved through the same closure for
        # the same reason. Empty when route 3 is switched off, which
        # BaseAgent._tertiary_reachable reads as "no third rung".
        _tertiary_api_key = (
            _key_for(config.llm.tertiary_model, config.llm.tertiary_provider)
            if (config.llm.tertiary_model or "").strip() else ""
        )

        self.tech_analyst = TechAnalystAgent(
            api_key=_key_for(config.llm.tech_analyst_model, config.llm.tech_analyst_provider),
            model=config.llm.tech_analyst_model,
            max_tokens=config.llm.get_max_tokens("tech_analyst"),
            fallback_api_key=_fallback_api_key,
            fallback_provider=config.llm.fallback_provider,
            fallback_model=config.llm.fallback_model,
            tertiary_api_key=_tertiary_api_key,
            tertiary_provider=config.llm.tertiary_provider,
            tertiary_model=config.llm.tertiary_model,
            provider=config.llm.tech_analyst_provider,
            provider_order=config.llm.get_provider_order("tech_analyst"),
            reasoning_effort=config.llm.reasoning_effort,
            structured_output=config.llm.structured_output,
        )
        self.portfolio_manager = PortfolioManagerAgent(
            api_key=_key_for(config.llm.portfolio_manager_model, config.llm.portfolio_manager_provider),
            model=config.llm.portfolio_manager_model,
            max_tokens=config.llm.get_max_tokens("portfolio_manager"),
            fallback_api_key=_fallback_api_key,
            fallback_provider=config.llm.fallback_provider,
            fallback_model=config.llm.fallback_model,
            tertiary_api_key=_tertiary_api_key,
            tertiary_provider=config.llm.tertiary_provider,
            tertiary_model=config.llm.tertiary_model,
            provider=config.llm.portfolio_manager_provider,
            provider_order=config.llm.get_provider_order("portfolio_manager"),
            reasoning_effort=config.llm.reasoning_effort,
            structured_output=config.llm.structured_output,
            # Same reason as the Risk Manager below, and it bites harder here:
            # without this the sizing seat's sheet falls back to
            # `load_risk_config_from_settings(config/settings.yaml)` — a
            # HARD-CODED path — while `main.py` builds this pipeline from
            # whatever `--config` names. Run the desk against any other
            # settings file and the seat that picks the sizes would be shown
            # the default file's limits while the engine enforced the chosen
            # one. That is the two-homes defect this change exists to remove,
            # on the very seat it is about.
            risk_config=config.risk,
        )
        self.risk_manager = RiskManagerAgent(
            api_key=_key_for(config.llm.risk_manager_model, config.llm.risk_manager_provider),
            model=config.llm.risk_manager_model,
            max_tokens=config.llm.get_max_tokens("risk_manager"),
            fallback_api_key=_fallback_api_key,
            fallback_provider=config.llm.fallback_provider,
            fallback_model=config.llm.fallback_model,
            tertiary_api_key=_tertiary_api_key,
            tertiary_provider=config.llm.tertiary_provider,
            tertiary_model=config.llm.tertiary_model,
            provider=config.llm.risk_manager_provider,
            provider_order=config.llm.get_provider_order("risk_manager"),
            reasoning_effort=config.llm.reasoning_effort,
            structured_output=config.llm.structured_output,
            # The reviewer's standing sheet renders its limits from THIS
            # object (`{{risk.*}}` placeholders, src/agents/prompt_limits.py),
            # which is the same `config.risk` the engine below is built from.
            # Passing it explicitly means the seat cannot be briefed against a
            # settings file other than the one this process is running on.
            risk_config=config.risk,
        )
        self.risk_engine = RiskRuleEngine(
            build_risk_config(config),
        # docs/WORK.md item 32 (owner call 2026-09-11). Lets the daily
        # circuit breaker measure a loss against the normal daily move of
        # the book actually held — from its holdings' real market price
        # history — rather than a frozen percentage of equity, and NOT
        # against the account's own (ramp-contaminated, malfunction-era)
        # equity curve. Read lazily, at each check, because the breaker
        # fires from six separate places in this file and the book changes
        # intraday. Returns None-safe values; a failing read falls back to
        # the fixed percentage.
            portfolio_vol_provider=self.held_book_daily_vol_pct,
        )
        self.position_reviewer = PositionReviewerAgent(
            api_key=_key_for(config.llm.position_reviewer_model, config.llm.position_reviewer_provider),
            model=config.llm.position_reviewer_model,
            max_tokens=config.llm.get_max_tokens("position_reviewer"),
            fallback_api_key=_fallback_api_key,
            fallback_provider=config.llm.fallback_provider,
            fallback_model=config.llm.fallback_model,
            tertiary_api_key=_tertiary_api_key,
            tertiary_provider=config.llm.tertiary_provider,
            tertiary_model=config.llm.tertiary_model,
            provider=config.llm.position_reviewer_provider,
            provider_order=config.llm.get_provider_order("position_reviewer"),
            reasoning_effort=config.llm.reasoning_effort,
            structured_output=config.llm.structured_output,
        )
        self.evening_analyst = EveningAnalystAgent(
            api_key=_key_for(config.llm.evening_analyst_model, config.llm.evening_analyst_provider),
            model=config.llm.evening_analyst_model,
            max_tokens=config.llm.get_max_tokens("evening_analyst"),
            fallback_api_key=_fallback_api_key,
            fallback_provider=config.llm.fallback_provider,
            fallback_model=config.llm.fallback_model,
            tertiary_api_key=_tertiary_api_key,
            tertiary_provider=config.llm.tertiary_provider,
            tertiary_model=config.llm.tertiary_model,
            provider=config.llm.evening_analyst_provider,
            provider_order=config.llm.get_provider_order("evening_analyst"),
            reasoning_effort=config.llm.reasoning_effort,
            structured_output=config.llm.structured_output,
        )
        self.news_analyst = NewsAnalystAgent(
            api_key=_key_for(config.llm.news_analyst_model, config.llm.news_analyst_provider),
            model=config.llm.news_analyst_model,
            max_tokens=config.llm.get_max_tokens("news_analyst"),
            fallback_api_key=_fallback_api_key,
            fallback_provider=config.llm.fallback_provider,
            fallback_model=config.llm.fallback_model,
            tertiary_api_key=_tertiary_api_key,
            tertiary_provider=config.llm.tertiary_provider,
            tertiary_model=config.llm.tertiary_model,
            provider=config.llm.news_analyst_provider,
            provider_order=config.llm.get_provider_order("news_analyst"),
            reasoning_effort=config.llm.reasoning_effort,
            structured_output=config.llm.structured_output,
        )
        self.macro_analyst = MacroAnalystAgent(
            api_key=_key_for(config.llm.macro_analyst_model, config.llm.macro_analyst_provider),
            model=config.llm.macro_analyst_model,
            max_tokens=config.llm.get_max_tokens("macro_analyst"),
            fallback_api_key=_fallback_api_key,
            fallback_provider=config.llm.fallback_provider,
            fallback_model=config.llm.fallback_model,
            tertiary_api_key=_tertiary_api_key,
            tertiary_provider=config.llm.tertiary_provider,
            tertiary_model=config.llm.tertiary_model,
            provider=config.llm.macro_analyst_provider,
            provider_order=config.llm.get_provider_order("macro_analyst"),
            reasoning_effort=config.llm.reasoning_effort,
            structured_output=config.llm.structured_output,
        )
        # sec_user_agent reuses config.smart_money.user_agent — the same
        # contact-bearing UA this repo already sends to SEC EDGAR for Form 4
        # — rather than inventing a second politeness convention for the
        # "SEC Press Releases" feed added 2026-08-29 (src/data/news.py).
        # per_symbol_* (2026-08-30 owner decision — src/data/news.py audit
        # block): every cap an operator can tune lives in config.news, never
        # a module constant, same rule already applied to max_prompt_items.
        self.news_provider = NewsDataProvider(
            sec_user_agent=config.smart_money.user_agent,
            per_symbol_enabled=config.news.per_symbol_enabled,
            per_symbol_max_symbols=config.news.per_symbol_max_symbols,
            per_symbol_max_prompt_items=config.news.per_symbol_max_prompt_items,
            per_symbol_requests_per_second=config.news.per_symbol_requests_per_second,
        )
        self.news_store = NewsStore()
        self.macro_store = MacroStore()
        self.tech_store = TechStore()
        self.earnings_analyst = EarningsAnalystAgent(
            api_key=_key_for(config.llm.earnings_analyst_model, config.llm.earnings_analyst_provider),
            model=config.llm.earnings_analyst_model,
            max_tokens=config.llm.get_max_tokens("earnings_analyst"),
            fallback_api_key=_fallback_api_key,
            fallback_provider=config.llm.fallback_provider,
            fallback_model=config.llm.fallback_model,
            tertiary_api_key=_tertiary_api_key,
            tertiary_provider=config.llm.tertiary_provider,
            tertiary_model=config.llm.tertiary_model,
            provider=config.llm.earnings_analyst_provider,
            provider_order=config.llm.get_provider_order("earnings_analyst"),
            reasoning_effort=config.llm.reasoning_effort,
            structured_output=config.llm.structured_output,
        )
        self.smart_money_analyst = SmartMoneyAnalystAgent(
            api_key=_key_for(config.llm.smart_money_analyst_model, config.llm.smart_money_analyst_provider),
            model=config.llm.smart_money_analyst_model,
            max_tokens=config.llm.get_max_tokens("smart_money_analyst"),
            fallback_api_key=_fallback_api_key,
            fallback_provider=config.llm.fallback_provider,
            fallback_model=config.llm.fallback_model,
            tertiary_api_key=_tertiary_api_key,
            tertiary_provider=config.llm.tertiary_provider,
            tertiary_model=config.llm.tertiary_model,
            provider=config.llm.smart_money_analyst_provider,
            provider_order=config.llm.get_provider_order("smart_money_analyst"),
            reasoning_effort=config.llm.reasoning_effort,
            structured_output=config.llm.structured_output,
        )
        sec_form4_provider = SECForm4Provider(
            search_url=config.smart_money.search_url,
            archives_url=config.smart_money.archives_url,
            data_dir=config.smart_money.data_dir,
            user_agent=config.smart_money.user_agent,
            request_timeout_s=config.smart_money.request_timeout_s,
            refresh_deadline_s=config.smart_money.refresh_deadline_s,
            watched_drain_deadline_s=config.smart_money.watched_drain_deadline_s,
            requests_per_second=config.smart_money.requests_per_second,
            lookback_days=config.smart_money.lookback_days,
            max_filings_per_refresh=config.smart_money.max_filings_per_refresh,
            max_observations=config.smart_money.max_observations,
            min_transaction_value_usd=config.smart_money.min_transaction_value_usd,
            external_min_transaction_value_usd=(
                config.smart_money.external_min_transaction_value_usd
            ),
            cluster_window_days=config.smart_money.cluster_window_days,
            min_cluster_owners=config.smart_money.min_cluster_owners,
            insider_calendar_routine_years=config.smart_money.insider_calendar_routine_years,
            insider_min_cadence_trades=config.smart_money.insider_min_cadence_trades,
            insider_cadence_min_mean_gap_days=config.smart_money.insider_cadence_min_mean_gap_days,
            insider_cadence_max_mean_gap_days=config.smart_money.insider_cadence_max_mean_gap_days,
            insider_cadence_max_gap_dispersion=config.smart_money.insider_cadence_max_gap_dispersion,
            insider_history_retention_days=config.smart_money.insider_history_retention_days,
        )
        # Congress (House + Senate) trading-disclosure cross-check, switched
        # on 2026-09-20 (config.smart_money.congress_enabled). Two independent
        # free sources fanned into the same SmartMoneySource protocol as SEC
        # Form 4 via CombinedSmartMoneyProvider — one source (or this whole
        # sub-provider) failing never blocks the other's evidence or the
        # rest of the run. See src/data/congressional_trading.py.
        congress_provider = None
        if config.smart_money.congress_enabled:
            congress_provider = CongressionalTradingProvider(
                kadoa_url=config.smart_money.congress_kadoa_url,
                congresswatch_url=config.smart_money.congress_congresswatch_url,
                data_dir=config.smart_money.congress_data_dir,
                user_agent=config.smart_money.user_agent,
                request_timeout_s=config.smart_money.congress_request_timeout_s,
                # Declared in config since 2026-09-04 and never passed until
                # 2026-09-19: the feed's own time budget, separate from Form 4's.
                refresh_deadline_s=config.smart_money.congress_refresh_deadline_s,
                max_trades_per_source=config.smart_money.congress_max_trades_per_source,
                assumed_max_disclosure_lag_days=(
                    config.smart_money.congress_assumed_max_disclosure_lag_days
                ),
                lookback_days=config.smart_money.congress_lookback_days,
                min_transaction_value_usd=config.smart_money.min_transaction_value_usd,
                external_min_transaction_value_usd=(
                    config.smart_money.external_min_transaction_value_usd
                ),
                cluster_window_days=config.smart_money.cluster_window_days,
                min_cluster_owners=config.smart_money.min_cluster_owners,
                max_observations=config.smart_money.max_observations,
            )
        self.smart_money_provider = CombinedSmartMoneyProvider(
            [sec_form4_provider, congress_provider]
        )
        # The universe screen's pending-takeover check reads the same SEC
        # client (same rate limiter, same User-Agent, same CIK cache).
        self.sec_form4_provider = sec_form4_provider
        self.meta_reflector = MetaReflectorAgent(
            api_key=_key_for(config.llm.meta_reflector_model, config.llm.meta_reflector_provider),
            model=config.llm.meta_reflector_model,
            max_tokens=config.llm.get_max_tokens("meta_reflector"),
            fallback_api_key=_fallback_api_key,
            fallback_provider=config.llm.fallback_provider,
            fallback_model=config.llm.fallback_model,
            tertiary_api_key=_tertiary_api_key,
            tertiary_provider=config.llm.tertiary_provider,
            tertiary_model=config.llm.tertiary_model,
            provider=config.llm.meta_reflector_provider,
            provider_order=config.llm.get_provider_order("meta_reflector"),
            reasoning_effort=config.llm.reasoning_effort,
            structured_output=config.llm.structured_output,
        )
        self.earnings_provider = EarningsDataProvider()
        # Guard 1 (2026-09-02): resolve the kill-switch flag path the same
        # way storage.db_path resolves a few lines down — relative to the
        # repo root, absolute paths passed through unchanged — so
        # `touch data/KILL_SWITCH` from the repo root (run.sh's own cwd) is
        # the file ops actually touches. Resolved ONCE here so the
        # broker-level enforcement (AlpacaBroker._kill_switch_active) and
        # this class's own early, alerting check (_kill_switch_halt_result)
        # can never disagree about which file they are each looking at.
        raw_kill_switch_path = config.risk.kill_switch_path
        kill_switch_path = Path(raw_kill_switch_path)
        if not kill_switch_path.is_absolute():
            kill_switch_path = Path(__file__).resolve().parent.parent / kill_switch_path
        self._kill_switch_path = kill_switch_path
        raw_storage_db_path = config.storage.db_path
        if raw_storage_db_path == ":memory:":
            # ``:memory:`` is a SQLite sentinel, not a relative filename.
            # Rewriting it under the repository creates a persistent DB and
            # lets otherwise-isolated tests/processes contaminate one another.
            self._storage_db_path = raw_storage_db_path
            # No on-disk DB to sit beside. Cwd-relative keeps hermetic
            # tests from flocking the production checkout.
            trade_updates_lease_path = Path("data") / ".trade_updates.lock"
        else:
            storage_db_path = Path(raw_storage_db_path)
            if not storage_db_path.is_absolute():
                storage_db_path = Path(__file__).resolve().parent.parent / storage_db_path
            self._storage_db_path = str(storage_db_path)
            trade_updates_lease_path = (
                Path(self._storage_db_path).parent / ".trade_updates.lock"
            )
        self.broker = AlpacaBroker(
            api_key=config.api_keys.alpaca_key,
            secret_key=config.api_keys.alpaca_secret,
            paper=config.alpaca.paper,
            kill_switch_path=str(self._kill_switch_path),
            trade_updates_lease_path=str(trade_updates_lease_path),
            # ON since 2026-09-18. The only site that threads this
            # through — see `ExecutionConfig.fill_stream_enabled` for why it
            # spent a day off, what actually blocked it (a placeholder
            # credential, not the connection's timing), and how a refusal is
            # logged now.
            fill_stream_enabled=config.execution.fill_stream_enabled,
        )
        # Wire the broker as yfinance's fallback so a yfinance outage doesn't
        # blackout the technical analyst. Alpaca's daily bars cover the same
        # universe we trade on, so fallback coverage is effectively 100%.
        self.market.set_fallback_bars(self.broker.get_bars)
        self.db = Database(self._storage_db_path)
        self.db.initialize()
        self._wire_protective_stop_block_recorder()
        if BaseAgent._allow_unmetered_for_tests:
            # Hermetic unit tests use mocked SDKs and explicitly opt out in
            # tests/conftest.py. This flag is false in every application run.
            self.cost_circuit = None
        else:
            try:
                from src.cost_table import refresh_openrouter_pricing
                self.cost_circuit = LLMCostCircuitBreaker(
                    self._storage_db_path, config.llm_cost_circuit,
                )
                # Pricing-staleness SPOF fix (2026-08-28): pass the
                # configured grace window/multiplier through so a stale-
                # but-recent cache is used (widened, logged loudly) instead
                # of latching the whole desk the moment openrouter.ai is
                # briefly unreachable past the cache's 24h freshness mark --
                # see the long note above refresh_openrouter_pricing in
                # src/cost_table.py.
                openrouter_pricing_ok = refresh_openrouter_pricing(
                    grace_period_hours=(
                        config.llm_cost_circuit.openrouter_pricing_grace_period_hours
                    ),
                    max_stale_multiplier=(
                        config.llm_cost_circuit.openrouter_pricing_stale_multiplier_max
                    ),
                )
                if not openrouter_pricing_ok:
                    self.cost_circuit.mark_unavailable(
                        RuntimeError(
                            "current official OpenRouter pricing is unavailable; "
                            "paid calls cannot be bounded safely"
                        ),
                        agent_name="pricing_preflight",
                        attempts=0,
                    )
            except Exception as exc:  # safety work must still initialize
                logger.critical(
                    "Mandatory paid-analysis cost circuit failed to initialize; "
                    "all paid calls are suspended while broker safety remains live: %s",
                    exc,
                    exc_info=True,
                )
                existing = getattr(self, "cost_circuit", None)
                marker = getattr(existing, "mark_unavailable", None)
                if callable(marker):
                    marker(exc, agent_name="pricing_preflight", attempts=0)
                    self.cost_circuit = existing
                else:
                    self.cost_circuit = LLMCostCircuitBreaker.fail_closed(
                        self._storage_db_path,
                        config.llm_cost_circuit,
                        exc,
                        agent_name="circuit_startup",
                    )
        self._attach_cost_circuit_to_agents()
        # Deterministic Target → Orders translator. Phase 2 of the architecture:
        # the LLM (PM) emits TargetPositions (intent); the constructor does the
        # math that turns intent into concrete TradeDecision orders.
        # Spec §2.1/§2.2. The risk envelope lives in `risk:` config, not in the
        # constructor's dataclass defaults — the 0.5% per-trade figure the
        # constructor shipped with was a default nobody chose, and the owner
        # ratified 5% / 25% on 2026-08-27. Reading it here means the deployed
        # ceiling is the one `verify_commissioning.py` can see.
        self.portfolio_constructor = PortfolioConstructor(
            build_constructor_config(config, self.risk_engine.config),
        )
        # Phase 4 #1: morning research stage — parallel macro/news/tech/earnings
        # fan-out extracted from the inline nested-function block.
        self.morning_research_stage = MorningResearchStage(
            config=config, db=self.db,
            market=self.market, macro=self.macro,
            news_provider=self.news_provider, news_store=self.news_store,
            macro_store=self.macro_store, tech_store=self.tech_store,
            earnings_provider=self.earnings_provider,
            macro_analyst=self.macro_analyst,
            news_analyst=self.news_analyst,
            tech_analyst=self.tech_analyst,
            earnings_analyst=self.earnings_analyst,
            smart_money_provider=self.smart_money_provider,
            smart_money_analyst=self.smart_money_analyst,
            admit_smart_money_candidates_fn=self._admit_transient_smart_money_symbols,
            admit_nominated_candidates_fn=self._admit_nominated_external_symbols,
            admit_screened_universe_fn=self._admit_screened_universe_symbols,
            event_calendar=self.event_calendar,
            fomc_calendar=self.fomc_calendar,
            has_actionable_signal_fn=self._has_actionable_signal_fn,
            live_session_context_fn=self._live_session_context,
            run_news_update_fn=self._run_news_update,
            load_earnings_analyses_fn=self._load_earnings_analyses,
        )
        # Downstream stages for run_morning: decision → risk → execution.
        # They take a `pipeline` reference so they can reuse the 15+ memory /
        # filter / sizing helpers that still live on TradingPipeline. Those
        # helpers are the next extraction boundary — see pipeline_stages.py
        # header for the rationale.
        self.decision_stage = DecisionStage(pipeline=self)
        self.risk_stage = RiskStage(pipeline=self)
        self.execution_stage = ExecutionStage(pipeline=self)
        # Idle-cash sweeper (SGOV parking). All consumers access it through
        # self._sweeper() so tests that build the pipeline via __new__ (no
        # __init__) degrade to a disabled sweeper instead of AttributeError.
        from src.execution.cash_sweep import CashSweeper
        self.cash_sweeper = CashSweeper(pipeline=self)

    def _sweeper(self):
        """The cash sweeper, or None when absent/disabled.

        getattr-guarded because ~58 tests build TradingPipeline via
        __new__() without __init__ — for them (and for enabled=False
        configs) every sweep hook must be a structural no-op.
        """
        from src.execution.cash_sweep import CashSweeper
        sweeper = getattr(self, "cash_sweeper", None)
        if not isinstance(sweeper, CashSweeper):
            return None
        try:
            return sweeper if sweeper.enabled() else None
        except Exception:  # noqa: BLE001 — a broken config must not take down a session
            return None

    def _retired_cash_park_symbol(self) -> str | None:
        """The configured sweep vehicle when the sweep is DISABLED, else None.

        Owner mandate 2026-09-17 turned the sweep off. A vehicle bought
        before that is still a deliberately stopless holding until
        `_release_retired_cash_park` sells it, so the stop-coverage audit
        must keep exempting it rather than raising a naked-position banner
        (its opening row is SWEEP_BUY, so the repair could not rebuild a
        stop anyway).
        """
        from src.execution.cash_sweep import CashSweeper
        sweeper = getattr(self, "cash_sweeper", None)
        if not isinstance(sweeper, CashSweeper):
            return None
        try:
            if sweeper.enabled():
                return None
            sym = sweeper.symbol
        except Exception:  # noqa: BLE001
            return None
        return sym if isinstance(sym, str) and sym.strip() else None

    def _release_retired_cash_park(self, run_id: str | None) -> None:
        """Sell any sweep vehicle still held after the sweep was disabled.

        Called at the start of every market-hours session (morning, midday/
        close review, intra_check), right after the stop-coverage audit and
        before any seat reads the book, so the release lands in cash the
        same session. Non-fatal by design; see
        `CashSweeper.release_retired_vehicle`.
        """
        from src.execution.cash_sweep import CashSweeper
        sweeper = getattr(self, "cash_sweeper", None)
        if not isinstance(sweeper, CashSweeper):
            return
        try:
            sweeper.release_retired_vehicle(run_id=run_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("cash sweep retired: release failed (non-fatal): %s", exc)

    def _news_held_symbols(self, positions) -> list[str]:
        """Held symbols eligible for the capped per-symbol company-news fetch.

        Applies the same cash-sweep exclusion as every other LLM-facing
        position view (`CashSweeper.split_positions`) before extracting
        symbols: the parked T-bill vehicle is cash-equivalent, has no
        thesis to follow, and must never consume one of
        `config.news.per_symbol_max_symbols`' capped slots (2026-08-31
        forensic — it was doing exactly that in the midday/close path).

        Shared by every same-day session that fetches held-symbol news
        (`run_position_review`, which itself backs both midday and close,
        and `run_evening`) so the exclusion cannot drift apart between
        them again.
        """
        sweeper = self._sweeper()
        investable = positions
        if sweeper is not None:
            investable, _parked = sweeper.split_positions(positions)
        return [
            s for s in (
                str(getattr(p, "symbol", "")).strip().upper()
                for p in investable if getattr(p, "qty", 0)
            )
            if s
        ]

    def _compute_deployable_cash(self, cash: float, positions) -> float:
        """Cash QAMC can deploy into equities WITHOUT borrowing.

        Verified Alpaca account-field semantics (2026-08-19, official docs):

        - `cash` is credited as soon as a SELL **fills** — Alpaca:
          "The cash is updated post the SELL trade is filled, but the
          cash_withdrawable and cash_transferable are updated post T+1."
          So proceeds of a filled SGOV sale ARE usable for an equity BUY
          the same session; there is no settlement wait for trading.
        - `non_marginable_buying_power` is the settled/non-margin (crypto)
          figure and LAGS a same-day equity sale by one business day. Using
          it to size equity BUYs is wrong in the conservative direction —
          it makes legitimately-available money invisible. An earlier pass
          in this tranche did exactly that; this is the correction.
        - `buying_power` / `regt_buying_power` are MARGIN figures. Every
          Alpaca account is a margin account and this one's equity puts it
          at multiplier 2, so those fields are ~2x equity. QAMC must never
          size against them — that is borrowed money by definition.

        Deployable is therefore raw `cash` plus the market value of the
        cash-equivalent sweep vehicle, which `CashSweeper.fund_buys`
        liquidates before the BUY phase and whose proceeds land in `cash`
        on fill. Both components are assets QAMC already owns, so the sum
        can never exceed equity and never creates leverage.

        This is a PLANNING figure for PM / RM / the pre-trade gate. It is
        not authoritative for execution, and — stale since the 2026-09-02
        margin flip — it is no longer true that execution "skips any BUY
        that cash does not actually cover": ExecutionStage still re-reads
        raw broker `cash` after the funding sale, but with `allow_margin`
        true a BUY may draw beyond that raw cash, bounded by the §11.2
        gross-exposure ladder's headroom, not by this figure (see
        `_entry_deployment_budget` in `src/pipeline_stages.py`). With
        `allow_margin` false the old description still holds: cash is the
        hard ceiling. Either way, this function itself never reads
        `buying_power` / `regt_buying_power` — see above — that boundary is
        unrelated to and unmoved by the ladder. See `CashSweeper.fund_buys`.

        The arithmetic itself lives in `src.quantities.deployable_cash` —
        one definition, shared with Mission Control's "Deployable" tile,
        which used to show `max(cash - sweep_reserve, 0)` instead and read
        1.58x lower than the figure the engine actually sized against.
        """
        sweeper = self._sweeper()
        if sweeper is None:
            return deployable_cash(cash, 0.0)
        try:
            parked = sweeper.parked_value(positions)
        except Exception as e:  # noqa: BLE001 — unknowable sweep state must not inflate
            logger.warning("deployable cash: parked-value read failed (%s) — "
                           "treating sweep reserve as unavailable", e)
            parked = 0.0
        return deployable_cash(cash, parked)

    @staticmethod
    def _format_qty(qty: float) -> str:
        if float(qty).is_integer():
            return str(int(qty))
        return f"{qty:.6f}".rstrip("0").rstrip(".")

    @staticmethod
    def _full_sell_qty(position_qty: float) -> float | None:
        if position_qty <= 0:
            return None
        return float(position_qty)

    @staticmethod
    def _reduce_sell_qty(position_qty: float) -> float | None:
        if position_qty <= 0:
            return None
        if float(position_qty).is_integer():
            return max(1.0, float(int(position_qty) // 2))
        return float(position_qty) / 2

    # Cushion used by BOTH sides of a forced/emergency close so they can
    # never drift apart: a long's exit is a SELL, whose limit needs to sit
    # BELOW the reference price to have room to fill on the way down; a
    # short's exit is a BUY-to-cover, whose limit needs to sit ABOVE the
    # reference price to have room to fill on the way up (same reasoning
    # broker.py's STOP_LIMIT_BUFFER_PCT already documents for stop legs —
    # "beyond", not "below", because a short's protective/exit order works
    # the opposite side of the trigger). One constant, applied with the
    # correct sign per side, rather than two independently hand-picked
    # numbers for the two directions.
    _EMERGENCY_LIMIT_CUSHION_PCT = 0.01

    #: Session status when the daily-loss breaker trips. **Replaces
    #: "emergency_sold" for that breaker only** (2026-09-14, docs/WORK.md
    #: item 32). The breaker no longer sells anything: it stops the desk
    #: taking new risk and verifies that what is held is protected. The
    #: string changed with the behaviour on purpose — a payload that still
    #: said "emergency_sold" while nothing had been sold would be the kind of
    #: record this desk has been burned by. `_force_delever` and the §11.2
    #: gross-exposure ladder still sell, still tag their rows
    #: FORCE_DELEVER, and are untouched by this.
    DAILY_LOSS_HALT_STATUS = "daily_loss_halted"
    #: The durable, per-symbol, machine-readable reason a held name records
    #: when the halt refuses further risk on it. Not a drop of a target and
    #: not a resize — see `_halt_on_daily_loss_breach`.
    DAILY_LOSS_HALT_REASON = "daily_loss_halt"

    def _daily_loss_breach(self, account, positions):
        """`(violation_or_None, baseline, pnl_compared, basis)` for the daily
        circuit breaker — ONE place that decides what number is compared.

        **The defect this closes** (2026-09-14, docs/WORK.md item 32): the
        breaker's threshold is measured from the HELD BOOK's own realized
        volatility, and the loss tested against it was
        `total_value - last_equity` — the whole account's day change,
        including realized losses on positions already closed today,
        commissions and spread. Numerator and denominator did not measure the
        same object, so the breaker could trip (or not) on movement the
        threshold never modelled. Both sides now read the held book, with the
        cash park excluded from each of them.

        `basis` is recorded, never inferred later: ``"held_book"`` when every
        non-park holding exposed a finite intraday change, ``"account"`` when
        any did not. The account fallback is the more negative number on any
        day with realized losses, so it trips SOONER — fail toward not
        trading. It is also used when a book is held but its intraday change
        reads as exactly flat, because a broker that omits the field reports
        precisely that and there is no way to tell the two apart from here;
        a flat read on a held book is therefore not trusted to suppress a
        breach the account-wide number would raise.
        """
        from src.risk.rules import daily_loss_numerator

        total_value = account["portfolio_value"] if isinstance(
            account, dict,
        ) else getattr(account, "portfolio_value", None)
        last_equity = (
            account.get("last_equity", total_value) if isinstance(account, dict)
            else getattr(account, "last_equity", total_value)
        )
        try:
            baseline = float(last_equity)
            account_pnl = float(total_value) - baseline
        except (TypeError, ValueError):
            # Unreadable snapshot — `check_daily_loss` owns the non-finite
            # path and logs the bypass; hand it through unchanged.
            return (
                self.risk_engine.check_daily_loss(float("nan"), float("nan")),
                float("nan"), float("nan"), "unreadable",
            )
        pnl, basis = daily_loss_numerator(
            account_pnl, positions,
            vol_relative=_limit_is_vol_relative(self.risk_engine),
            cash_park_symbol=self._sweep_symbol(),
        )
        return (
            self.risk_engine.check_daily_loss(baseline, pnl),
            baseline, pnl, basis,
        )

    def _total_pnl_since_reset(
        self, total_value: float,
    ) -> tuple[float | None, float | None, str | None]:
        """`(total_pnl, total_return_pct, since_date)` for the Telegram
        feed's "total P&L" line.

        **Why "since reset" and not "since inception".** The desk's
        2026-09-02 book-wide liquidation archived every prior trade/
        daily_pnl row (see docs/INCIDENT_HISTORY.md); the live `daily_pnl`
        table has held no row earlier than that date since. A "total"
        spanning that boundary would silently splice pre-reset and
        post-reset history into one number the owner would act on as if it
        were continuous — exactly the defect he flagged. So the baseline
        is the EARLIEST row this table actually has, never reconstructed
        from the archive.

        **Why that row's `total_value - daily_pnl`, not its `equity_close`.**
        `equity_close` is that day's OWN 4pm close — already one day inside
        the post-reset period, which would drop that first day's P&L from
        the total. `total_value - daily_pnl` recovers the broker's
        last_equity going into that day (the same basis `daily_pnl` itself
        is built from everywhere else in this file), i.e. the account's
        equity immediately before the first post-reset trading day —
        a value already recorded on that row, not invented here.

        Returns `(None, None, None)` when no `daily_pnl` row exists yet
        (fresh DB) or the recorded baseline is non-finite/non-positive —
        never a fabricated 0.
        """
        try:
            earliest = self.db.get_earliest_daily_pnl()
        except Exception as exc:  # noqa: BLE001
            logger.warning("total P&L baseline lookup failed: %s", exc)
            return None, None, None
        if not earliest:
            return None, None, None
        try:
            baseline = float(earliest["total_value"]) - float(earliest["daily_pnl"])
            tv = float(total_value)
        except (TypeError, ValueError, KeyError):
            return None, None, None
        if not (baseline > 0) or not math.isfinite(baseline) or not math.isfinite(tv):
            return None, None, None
        total_pnl = tv - baseline
        total_return_pct = total_pnl / baseline * 100
        since_date = str(earliest.get("date") or "") or None
        return total_pnl, total_return_pct, since_date

    def _verify_stop_coverage_at_halt(self, positions) -> list[dict]:
        """Per-symbol stop-coverage truth, read from the broker, for a halt.

        **This is the precondition of the halt, not a report attached to
        it.** Halting instead of liquidating is only safe if the
        per-position stops are genuinely live at the broker, and the desk's
        own records cannot answer *whether a stop exists*. Coverage qty is
        read from the broker. The archive's `trades.stop_loss` is written
        back on every in-code replace/trail/repair/rearm/ex-div, and a
        session reconcile reports when that number still disagrees with the
        broker (an out-of-band move leaves no write-back row). That is the
        LEVEL. This method still asks the BROKER for coverage, because a
        stop that was cancelled and not replaced is a missing order, not a
        stale price — which is exactly what the 2026-09-14 broker audit
        found for the Visa and Disney *illusion* of an unfired stop (WORK.md
        items 35 and 69, both closed as archive-stale; the write-back that
        would have prevented those filings is item 71). Two live reasons
        the answer can genuinely be "no" remain: a stop can be moved by a
        maintenance action outside this code, and a sub-share remainder
        provably cannot hold an overnight stop at this broker.

        Three outcomes per holding, and the third is the one that exists
        because of that archive:

          ``covered``     open protective stops at least equal the held qty.
          ``uncovered``   they do not — with the shortfall named by
                          `_classify_coverage_gap`, the same classifier the
                          session coverage audit uses, so an expected
                          overnight sub-share lapse is not reported as a
                          naked position.
          ``unverified``  the broker could not be asked. **Never treated as
                          covered.** `_reconcile_stop_coverage` `continue`s
                          past this case and the symbol vanishes from its
                          gap list; a halt that inherited that would
                          silently assume protection exists.

        The cash park is excluded — it is deliberately stopless
        cash-equivalent, the same exemption the session audit makes.
        """
        out: list[dict] = []
        park = (self._sweep_symbol() or "").strip().upper()
        for p in positions or ():
            symbol = str(getattr(p, "symbol", "") or "").strip().upper()
            if not symbol or (park and symbol == park):
                continue
            try:
                qty = float(getattr(p, "qty", 0) or 0)
            except (TypeError, ValueError):
                qty = 0.0
            if qty == 0:
                continue
            is_short = qty < 0
            held = abs(qty)
            try:
                _ok, specs = self.broker.snapshot_protective_stops(
                    symbol, side=("buy" if is_short else "sell"),
                )
            except Exception as exc:  # noqa: BLE001
                logger.critical(
                    "HALT COVERAGE CHECK UNREADABLE: %s — the broker could "
                    "not be asked whether a protective stop is live (%s). "
                    "Reported as UNVERIFIED, never as covered.", symbol, exc,
                )
                out.append({
                    "symbol": symbol, "held_qty": held, "covered_qty": None,
                    "state": "unverified", "detail": str(exc)[:200],
                })
                continue
            covered = sum(float(s.get("qty", 0) or 0) for s in (specs or []))
            if covered + 1e-6 >= held:
                out.append({
                    "symbol": symbol, "held_qty": held,
                    "covered_qty": covered, "state": "covered",
                })
                continue
            coverage, frac_uncovered = _classify_coverage_gap(
                held=held, covered=covered,
            )
            out.append({
                "symbol": symbol, "held_qty": held, "covered_qty": covered,
                "state": "uncovered", "coverage": coverage,
                "uncovered_qty": (
                    frac_uncovered if coverage == "fractional" else held - covered
                ),
                "unprotected_value": _position_notional(
                    p,
                    frac_uncovered if coverage == "fractional"
                    else held - covered,
                ),
            })
        return out

    @staticmethod
    def _halt_coverage_shortfalls(coverage: list[dict]) -> list[dict]:
        """The entries a halt must SHOUT about, not merely record.

        A holding whose protection could not be verified, or whose durable
        whole-share stop is missing or mis-sized. An expected overnight
        sub-share lapse (`'fractional'`) is excluded for the same reason the
        session audit excludes it — the durable leg is intact and the
        remainder's DAY stop is gone by broker design every night; reporting
        it as a naked position is how a banner gets tuned out. Its exposure
        is still carried in the halt payload as a number.
        """
        return [
            c for c in coverage
            if c.get("state") == "unverified"
            or (c.get("state") == "uncovered"
                and c.get("coverage") in ("none", "partial"))
        ]

    def _halt_on_daily_loss_breach(
        self, positions, loss_violation, run_id: str, *,
        where: str, basis: str = "held_book", ctx=None,
    ) -> dict:
        """**Stop taking risk. Do not sell anything.** The daily-loss
        circuit breaker's whole response, replacing the force-liquidation of
        the entire book (2026-09-14, docs/WORK.md item 32).

        WHY THE LIQUIDATION IS GONE. It submitted LIMIT orders 1% through the
        market (`_EMERGENCY_LIMIT_CUSHION_PCT`), then called
        `_finalize_pending_protections`, which restores the original stops on
        any leg that did not fill. On a correlated gap — the only day a
        whole-book dump could be argued for — a limit 1% through does not
        fill, so the sequence was: cancel every protective stop, fail to
        sell, put the stops back. An unprotected window, and nothing
        achieved. On an ordinary day it filled fine, which is to say it
        worked only when it was not needed. It also never once fired in
        production: zero EMERGENCY_SELL/EMERGENCY_COVER rows in the archive
        across 13 days of P&L whose worst day was -0.46% against a
        reconstructed trip point near -0.70% of equity.

        And the proportionate response already exists. `_enforce_gross_ceiling`
        runs in the session preamble, trims only the EXCESS down to a
        drawdown-scaled ceiling, and works with margin on. (`_force_delever`
        is not it — it returns `[]` whenever `allow_margin` is true, which it
        has been since 2026-09-02.) Nor is this breaker the defence against a
        broker-initiated liquidation: at `max_gross_exposure_x: 2.0` against a
        25% maintenance requirement the book can fall 33.3% before a margin
        call, which is a week, not a day.

        WHAT IT DOES INSTEAD, in order:

          1. Reconcile fills, so every judgement below is made against
             broker truth rather than a stale 'submitted' row.
          2. Cancel every resting entry order. A working DAY entry limit is a
             standing intention to add risk; refusing new risk has to mean
             pending intentions too. Best-effort, and a failure is reported
             in the alert rather than swallowed.
          3. Run the session stop-coverage audit, which repairs a
             recoverable long in place.
          4. VERIFY coverage per held position against the broker
             (`_verify_stop_coverage_at_halt`) — the precondition, see there.
          5. File a durable, per-symbol, machine-readable refusal for every
             holding, carrying its verified coverage state.
          6. Alert the owner, escalating hard when any coverage is short or
             unverifiable.

        NOTHING HERE CLOSES, RESIZES OR ZEROES A POSITION, and nothing here
        places an order except the coverage audit's stop REPAIR, which can
        only ADD protection. A held name is refused, not sold; its target is
        dropped, never set to zero (a 0% target reads as "sell it" on this
        desk).

        WHEN A POSITION HAS NO LIVE STOP AT THE MOMENT OF HALT: the halt
        still halts — it must, because the alternative response was the
        broken liquidation — but it does NOT halt quietly. The audit in step
        3 first tries to re-place the stop from the level recorded on the
        position's own BUY. If that fails, or if the broker could not be
        asked at all, the symbol lands in `_halt_coverage_shortfalls`, the
        owner alert leads with it by name and held quantity, and the returned
        payload carries `stop_coverage_verified` plus
        `unprotected_at_halt` so the feed, Mission Control and the evening
        probe all see it. The desk does not sell the position to protect it:
        selling on a breach day is exactly the behaviour being removed, and a
        naked position is an operator escalation, not an excuse to fire the
        mechanism that did not work.
        """
        held = list(positions or ())
        logger.critical(
            "DAILY LOSS HALT (%s): %s — refusing NEW RISK for the rest of "
            "the session. %d position(s) are being kept and verified, not "
            "sold (loss basis: %s).",
            where, loss_violation.message, len(held), basis,
        )
        # 1. Broker truth before any judgement.
        try:
            self._reconcile_fills()
        except Exception as exc:  # noqa: BLE001
            logger.warning("daily-loss halt: fill reconcile failed: %s", exc)
        # 2. Resting entry orders are standing intentions to add risk.
        entries_cancelled = True
        try:
            self.broker.cancel_open_entry_orders()
        except Exception as exc:  # noqa: BLE001
            entries_cancelled = False
            logger.error(
                "DAILY LOSS HALT: could not cancel resting entry orders (%s) "
                "— a working entry limit can still add risk during the halt. "
                "Escalated to the owner.", exc,
            )
        # 3. The audit, which repairs a recoverable long in place.
        try:
            coverage_gaps = self._reconcile_stop_coverage()
        except Exception as exc:  # noqa: BLE001
            logger.error("daily-loss halt: coverage audit failed: %s", exc)
            coverage_gaps = []
        # 4. Verify, per position, against the broker.
        coverage = self._verify_stop_coverage_at_halt(held)
        shortfalls = self._halt_coverage_shortfalls(coverage)
        # 5. Durable per-symbol refusal.
        self._record_daily_loss_halt_refusals(
            coverage, loss_violation, run_id=run_id, where=where, ctx=ctx,
        )
        # 6. Alert.
        self._alert_owner_daily_loss_halt(
            loss_violation, coverage, shortfalls, held,
            where=where, basis=basis, entries_cancelled=entries_cancelled,
        )
        return {
            "status": self.DAILY_LOSS_HALT_STATUS,
            "halted": True,
            "halt_reason": self.DAILY_LOSS_HALT_REASON,
            "halt_where": where,
            "daily_loss_basis": basis,
            "positions": len(held),
            # Explicitly empty and explicitly present: a reader must be able
            # to see that the breaker placed no orders, rather than infer it
            # from a missing key. A call site that already holds the session's
            # own earlier orders (deterministic trails, say) overwrites this
            # key so the feed still renders them; `halted` / `halt_reason`
            # remain the record that the BREAKER sold nothing, and the
            # invariant that this method never appends an order is tested.
            "orders": [],
            "stop_coverage_gaps": coverage_gaps,
            "stop_coverage_verified": coverage,
            "unprotected_at_halt": [
                str(c.get("symbol")) for c in shortfalls if c.get("symbol")
            ],
            "entry_orders_cancelled": entries_cancelled,
            "run_id": run_id,
        }

    def _record_daily_loss_halt_refusals(
        self, coverage: list[dict], loss_violation, *,
        run_id: str, where: str, ctx=None,
    ) -> None:
        """One durable, machine-readable row per held symbol. Never raises.

        The desk's rule is that anything refused leaves a per-symbol reason a
        machine can read, not a sentence in a log file. A halt refuses every
        held name further risk, so every held name gets a row — carrying its
        verified coverage state, which is the fact an operator will actually
        need afterwards.
        """
        from src.pipeline_stages import _persist_evidence
        import json as _json

        decision_id = getattr(ctx, "decision_id", None)
        for entry in coverage or ():
            symbol = entry.get("symbol")
            if not symbol:
                continue
            try:
                _persist_evidence(
                    self.db, run_id=run_id, agent_name="pipeline",
                    kind="pipeline_event", scope="symbol", symbol=symbol,
                    decision_id=decision_id,
                    evidence_json=_json.dumps({
                        "stage": "daily_loss_halt",
                        "outcome": "no_new_risk",
                        "reason": self.DAILY_LOSS_HALT_REASON,
                        "where": where,
                        "detail": loss_violation.message,
                        "stop_coverage": entry.get("state"),
                        "coverage_shortfall": entry.get("coverage"),
                        "held_qty": entry.get("held_qty"),
                        "covered_qty": entry.get("covered_qty"),
                    }, sort_keys=True),
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "daily-loss halt: could not record the refusal reason "
                    "for %s: %s", symbol, exc,
                )

    def _alert_owner_daily_loss_halt(
        self, loss_violation, coverage: list[dict], shortfalls: list[dict],
        positions, *, where: str, basis: str, entries_cancelled: bool,
    ) -> None:
        """Push the halt to the owner. Never raises.

        Two different messages on purpose. A halt with every position
        verifiably protected is a serious but orderly event. A halt with a
        position that has no live stop, or one the broker could not be asked
        about, is the state that ends a desk — it leads the message, by name,
        and is never folded into the same sentence as the orderly case.
        """
        try:
            from src import notifier as _notifier

            lines = [
                "🛑 DAILY LOSS HALT — NO NEW RISK THIS SESSION",
                f"{loss_violation.message}",
                f"Tripped at: {where}. Loss measured on: "
                f"{'the held book' if basis == 'held_book' else 'the whole account'}.",
                "",
                "Nothing was sold. Every position is being KEPT. Resting "
                "entry orders were "
                + ("cancelled." if entries_cancelled
                   else "NOT cancelled — the cancel failed, see below."),
            ]
            if shortfalls:
                lines += [
                    "",
                    f"⚠️ {len(shortfalls)} POSITION(S) ARE NOT VERIFIABLY "
                    "PROTECTED RIGHT NOW. A halt assumes the per-position "
                    "stops are live at the broker. For these, that is not "
                    "established:",
                ]
                for c in shortfalls:
                    if c.get("state") == "unverified":
                        lines.append(
                            f"  {c.get('symbol')}: held {c.get('held_qty')} — "
                            "the broker could NOT be asked whether a stop is "
                            "live. Not the same as 'no stop'; it means "
                            "unknown."
                        )
                    else:
                        lines.append(
                            f"  {c.get('symbol')}: held {c.get('held_qty')}, "
                            f"covered {c.get('covered_qty')} "
                            f"({c.get('coverage')}) — "
                            f"${float(c.get('unprotected_value') or 0):.2f} "
                            "unprotected."
                        )
                lines.append(
                    "The desk did NOT sell these to protect them — selling on "
                    "a breach day is the behaviour that was removed. Place a "
                    "stop manually or flatten, by hand."
                )
            else:
                covered = [c for c in coverage if c.get("state") == "covered"]
                lines += [
                    "",
                    f"Stop coverage verified at the broker on all "
                    f"{len(covered)} position(s).",
                ]
            if not entries_cancelled:
                lines += [
                    "",
                    "⚠️ The resting-entry cancel FAILED. A working entry "
                    "limit can still add risk while the desk is halted — "
                    "check open orders by hand.",
                ]
            _notifier.send_owner_alert(
                "\n".join(lines),
                symbols=[
                    str(c.get("symbol")) for c in shortfalls if c.get("symbol")
                ],
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("daily-loss halt owner alert failed: %s", exc)

    @staticmethod
    def _forced_close_side_and_qty(position_qty: float) -> tuple[str, float] | None:
        """Direction-aware sizing for a FORCED close — circuit breaker,
        risk-breach liquidation, operator kill. NOT the normal decision
        path: SELL/REDUCE decisions and the portfolio constructor keep
        refusing a negative qty exactly as before (see _full_sell_qty /
        _reduce_sell_qty and the Stage 1 guard in portfolio_constructor.py
        — shorts still cannot be opened or covered through that path).

        Returns ``(side, qty)`` where ``side`` is ``'sell'`` to flatten a
        long or ``'buy'`` to cover a short, and ``qty`` is the ABSOLUTE
        number of shares — always positive, never the signed broker qty.

        Returns ``None`` when direction can't be determined (qty is zero,
        NaN, or otherwise not a finite nonzero number). This is the one
        design rule the reviewer called non-negotiable: a forced close is
        only safe when the side is certain, because guessing wrong on a
        short doesn't fail safe — a SELL aimed at a position that's
        actually already short would ADD to the short (sell more of a
        symbol you don't hold long), doubling the very exposure the
        circuit breaker exists to shed. Refusing and logging loudly beats
        guessing every time; the caller is responsible for the loud log,
        this just refuses to hand back an answer to guess with.
        """
        if not isinstance(position_qty, (int, float)) or not math.isfinite(position_qty):
            return None
        if position_qty == 0:
            return None
        if position_qty > 0:
            return "sell", float(position_qty)
        return "buy", float(-position_qty)

    @staticmethod
    def _trade_executed_or_pending(trade: dict) -> bool:
        """True when a trade either executed or is still an open live attempt.

        Used for idempotence checks on sell-side rows (same-day trim
        discipline): a pending submitted trim should block a duplicate order,
        but a canceled/rejected/expired zero-fill should not.
        """
        status = str(trade.get("fill_status") or "").lower()
        if not status:
            return True
        if status in {"submitted", "filled"}:
            return True
        try:
            return float(trade.get("fill_qty") or 0) > 0
        except (TypeError, ValueError):
            return False

    def _filter_supported_symbols(
        self,
        decisions: list[TradeDecision],
        analyses: list[TechAnalysisResult],
        positions,
        admitted_symbols: set[str] | None = None,
    ) -> tuple[list[TradeDecision], list[str]]:
        universe = {symbol.strip().upper() for symbol in self.config.trading.universe}
        buy_allowlist = universe | {
            str(symbol).strip().upper()
            for symbol in (admitted_symbols or set())
            if str(symbol).strip()
        }
        analyzed_symbols = {analysis.symbol.strip().upper() for analysis in analyses}
        held_symbols = {position.symbol.strip().upper() for position in positions}

        allowed_decisions: list[TradeDecision] = []
        blocked_reasons: list[str] = []

        for decision in decisions:
            symbol = decision.symbol.strip().upper()

            if decision.action == "BUY":
                if symbol not in buy_allowlist:
                    blocked_reasons.append(
                        f"{symbol} is neither in the configured universe nor "
                        "deterministically admitted for this run and cannot be bought"
                    )
                    continue
                if symbol not in analyzed_symbols:
                    blocked_reasons.append(
                        f"{symbol} has no supporting analyst output in this run and cannot be bought"
                    )
                    continue
            elif decision.action == "SELL" and symbol not in held_symbols:
                blocked_reasons.append(
                    f"{symbol} is not an existing holding and cannot be sold"
                )
                continue
            # Stage 3 (shorts). SHORT is the sell-side entry twin of BUY —
            # same universe/analyst-coverage bar, because it opens/adds new
            # risk the same way a BUY does. Without this explicit branch a
            # SHORT fell through to `allowed_decisions.append` unconditionally
            # (fail OPEN — the one thing D2 forbids), since it matched
            # neither the BUY nor the SELL condition above.
            elif decision.action == "SHORT":
                if symbol not in buy_allowlist:
                    blocked_reasons.append(
                        f"{symbol} is neither in the configured universe nor "
                        "deterministically admitted for this run and cannot be shorted"
                    )
                    continue
                if symbol not in analyzed_symbols:
                    blocked_reasons.append(
                        f"{symbol} has no supporting analyst output in this run and cannot be shorted"
                    )
                    continue
            # COVER is the buy-side exit twin of SELL — same held-position
            # bar. Same fail-OPEN gap as SHORT above without this branch.
            elif decision.action == "COVER" and symbol not in held_symbols:
                blocked_reasons.append(
                    f"{symbol} is not an existing holding and cannot be covered"
                )
                continue

            allowed_decisions.append(decision)

        return allowed_decisions, blocked_reasons

    def _evaluate_external_admission_gates(
        self,
        symbol: str,
        *,
        context: str = "external",
    ) -> tuple[bool, str | None, dict]:
        """Deterministic broker + market-quality gates for admitting a
        symbol OUTSIDE the configured universe.

        Shared by two callers that each decide WHICH symbols are worth
        gating (a different question) but must apply IDENTICAL gates once
        a symbol is a candidate: the SEC Form 4 smart-money transient-
        admission lane (`_admit_transient_smart_money_symbols`) and the
        Phase 9 nomination responder lane
        (`_admit_nominated_external_symbols`). The source of the candidate
        differs — a material Form 4 purchase vs. a research seat's
        nomination — but the trading-surface facts a candidate must clear
        before it can be bought (broker eligibility, price, liquidity,
        history, resolved sector) are exactly the same facts, so both
        callers share this one gate rather than each maintaining its own
        copy that could quietly drift apart.

        Returns ``(eligible, rejection_reason, details)``. ``details`` is
        populated only when eligible: ``last_price``,
        ``avg_dollar_volume_20d_usd``, ``sector``, ``broker``.
        ``rejection_reason`` is one of: ``broker_ineligible`` (or the
        broker's own reason string), ``market_data_error``,
        ``insufficient_history``, ``invalid_market_data``,
        ``price_below_minimum``, ``dollar_volume_below_minimum``,
        ``unresolved_sector``.
        """
        if self._universe_screen_enabled():
            return self._evaluate_screened_admission(symbol, context=context)
        cfg = self.config.smart_money
        broker_fact = self.broker.get_transient_equity_eligibility(symbol)
        if not broker_fact.get("eligible"):
            reason = broker_fact.get("reason", "broker_ineligible")
            logger.info("%s admission rejected %s: %s", context, symbol, reason)
            return False, reason, {}
        try:
            bars = self.market.get_ohlcv(
                symbol,
                max(self.config.trading.lookback_days, cfg.min_external_history_days + 5),
            ) or []
        except Exception as exc:
            logger.warning("%s admission bars failed for %s: %s", context, symbol, exc)
            return False, "market_data_error", {}
        if len(bars) < cfg.min_external_history_days:
            logger.info("%s admission rejected %s: insufficient_history", context, symbol)
            return False, "insufficient_history", {}
        recent = bars[-20:]
        try:
            last_price = float(recent[-1].close)
        except (AttributeError, TypeError, ValueError, IndexError):
            logger.info("%s admission rejected %s: invalid_market_data", context, symbol)
            return False, "invalid_market_data", {}
        # Single shared definition (`src.quantities.avg_dollar_volume`);
        # the threshold below stays this gate's own. None = the window did
        # not contain a full 20 usable sessions, which fails closed here
        # rather than admitting on partial data.
        adv = avg_dollar_volume(recent)
        if adv is None:
            logger.info("%s admission rejected %s: invalid_market_data", context, symbol)
            return False, "invalid_market_data", {}
        avg_dollar_volume_usd = adv
        if last_price < cfg.min_external_price_usd:
            logger.info(
                "%s admission rejected %s: price %.2f < %.2f",
                context, symbol, last_price, cfg.min_external_price_usd,
            )
            return False, "price_below_minimum", {}
        if avg_dollar_volume_usd < cfg.min_external_avg_dollar_volume_usd:
            logger.info(
                "%s admission rejected %s: avg dollar volume %.0f < %.0f",
                context, symbol, avg_dollar_volume_usd,
                cfg.min_external_avg_dollar_volume_usd,
            )
            return False, "dollar_volume_below_minimum", {}
        sector = _get_sector(symbol) or "Unknown"
        if sector == "Unknown":
            logger.info("%s admission rejected %s: unresolved_sector", context, symbol)
            return False, "unresolved_sector", {}
        return True, None, {
            "last_price": round(last_price, 4),
            "avg_dollar_volume_20d_usd": round(avg_dollar_volume_usd, 2),
            "sector": sector,
            "broker": broker_fact,
        }

    # ------------------------------------------------------------------
    # Universe expansion and pruning (src/universe_screen.py). Everything
    # below is inert while `universe_screen.enabled` is off.
    # ------------------------------------------------------------------

    def _universe_screen_enabled(self) -> bool:
        cfg = getattr(getattr(self, "config", None), "universe_screen", None)
        return bool(getattr(cfg, "enabled", False))

    def _universe_screen_sources(self, deadline: float, listed: dict | None = None):
        """The screen's read path: broker asset directory, yfinance bars and
        company profile, SEC filing history. Every source is read-only."""
        from src.execution.broker import _canonicalize_sector
        from src.universe_screen import HISTORY_FETCH_DAYS, ScreenSources

        def _profile(symbol: str):
            raw = self.market.get_company_profile(symbol)
            if raw is None:
                return None
            sector = _canonicalize_sector(raw.get("sector_raw"))
            if sector == "Unknown":
                sector = _get_sector(symbol) or "Unknown"
            return {"market_cap_usd": raw.get("market_cap_usd"), "sector": sector}

        def _filings(symbol: str):
            provider = getattr(self, "sec_form4_provider", None)
            if provider is None:
                raise RuntimeError("SEC provider not configured")
            return provider.recent_filings(symbol, deadline, listed=listed)

        return ScreenSources(
            get_asset=self.broker.get_asset_record,
            get_bars=lambda symbol: self.market.get_ohlcv(symbol, HISTORY_FETCH_DAYS),
            get_profile=_profile,
            get_filings=_filings,
        )

    def _evaluate_screened_admission(
        self, symbol: str, *, context: str,
    ) -> tuple[bool, str | None, dict]:
        """The side-door gate when the universe screen is on: the SAME
        `screen_symbol` the weekly screen runs, so a Form 4 purchase or a
        seat's nomination can never admit a name the screen would refuse.
        Same return shape as the legacy gate it replaces."""
        import time as _time

        from src.universe_screen import ScreenThresholds, screen_symbol

        # Only the SEC reads take a deadline (the broker and yfinance reads
        # carry their own timeouts): at most the ticker-map refresh plus the
        # issuer filing history, one request timeout each.
        deadline = _time.monotonic() + float(self.config.smart_money.request_timeout_s) * 2
        result = screen_symbol(
            symbol, self._universe_screen_sources(deadline),
            ScreenThresholds.from_config(self.config),
        )
        if not result.passed:
            logger.info(
                "UNIVERSE_SCREEN %s admission rejected %s: %s",
                context, symbol, ", ".join(result.failures),
            )
            return False, result.reason, {}
        measured = dict(result.measured)
        return True, None, {
            "last_price": measured.get("last_price"),
            "sector": measured.get("sector"),
            "screen": "universe_screen",
            "screen_measured": measured,
        }

    def _form4_admission_is_current(self, observation) -> bool:
        """The Form 4 door's age gate, restored (screen on only).

        `lookback_days` went 7 -> 365 on 2026-09-11 and the provider's
        "stale" label is "older than lookback_days", so since then nothing
        inside the cache is ever stale and a 364-day-old purchase could
        admit a symbol (RSG came in that way). The bound is the desk's OWN
        horizon: a purchase disclosed more trading sessions ago than
        `risk.max_target_horizon_sessions` is older than the longest move
        the desk will claim a target for, so it cannot be the reason to
        open a new name now. Sessions are counted with the same weekday
        counter the desk's horizon arithmetic uses.
        """
        from src.trading_calendar import trading_sessions_held
        from src.util.time import et_today

        disclosed = getattr(observation, "disclosure_date", None)
        if not isinstance(disclosed, date):
            return False
        horizon = int(self.config.risk.max_target_horizon_sessions)
        return trading_sessions_held(disclosed, et_today()) <= horizon

    def _admit_screened_universe_symbols(self, positions=None) -> tuple[set[str], dict[str, dict]]:
        """This session's share of the screened universe (screen on only).

        Every held admitted name, plus at most `nominations.
        max_per_seat_per_run` others, rotated least-recently-offered first —
        the screen is one more source of candidates and is capped like one
        seat, so the portfolio manager's bill is a number that is set.
        """
        if not self._universe_screen_enabled():
            return set(), {}
        from src.universe_screen import UniverseStore, select_for_run
        from src.util.time import et_today

        store = UniverseStore(self.config.universe_screen.data_dir)
        state = store.load()
        held = {
            str(getattr(p, "symbol", "") or "").strip().upper()
            for p in (positions or [])
        }
        configured = {str(s).strip().upper() for s in self.config.trading.universe}
        chosen = select_for_run(
            state, held=held,
            cap=int(self.config.nominations.max_per_seat_per_run),
            today=et_today(),
        )
        chosen = {s: d for s, d in chosen.items() if s not in configured}
        if chosen:
            store.save(state)
        return set(chosen), chosen

    def _run_universe_screen(self, run_id: str) -> dict | None:
        """The weekly screen's incremental pass, run after the evening report
        (screen on only). Never raises: a failure costs tonight's pass,
        never the evening push. Every change is logged under
        `UNIVERSE_CHANGE`, kept in the state file until the morning message
        shows it, and written to the evidence record now."""
        if not self._universe_screen_enabled():
            return None
        import json as _json
        import time as _time

        from src.pipeline_stages import _persist_evidence
        from src.universe_screen import (
            HISTORY_FETCH_DAYS, ScreenThresholds, UniverseStore, run_screen,
        )
        from src.util.time import et_today

        cfg = self.config.universe_screen
        deadline = _time.monotonic() + float(cfg.screen_deadline_s)
        store = UniverseStore(cfg.data_dir)
        try:
            state = store.load()
            assets = self.broker.list_assets()
            held = {p.symbol.strip().upper() for p in self.broker.get_positions()}
            listed = None
            provider = getattr(self, "sec_form4_provider", None)
            if provider is not None:
                try:
                    listed = provider.listed_map(deadline)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("universe screen: SEC ticker map unavailable: %s", exc)
            run = run_screen(
                state,
                assets=assets,
                sources=self._universe_screen_sources(deadline, listed=listed),
                get_bars_batch=lambda chunk: self.market.get_ohlcv_batch(
                    chunk, HISTORY_FETCH_DAYS,
                ),
                th=ScreenThresholds.from_config(self.config),
                today=et_today(),
                held=held,
                configured=self.config.trading.universe,
                deadline=deadline,
                batch_size=int(cfg.bars_batch_size),
                confirm_missing_asset=self.broker.get_asset_record,
            )
            store.save(state)
        except Exception as exc:  # noqa: BLE001
            logger.warning("UNIVERSE_SCREEN pass failed (non-fatal): %s", exc)
            return {"status": "error", "error": f"{type(exc).__name__}: {exc}"}
        for event in run.events:
            _persist_evidence(
                self.db, run_id=run_id, agent_name="universe_screen",
                kind="universe_change", scope="symbol", symbol=event.get("symbol"),
                evidence_json=_json.dumps(event, sort_keys=True),
            )
        summary = run.summary()
        _persist_evidence(
            self.db, run_id=run_id, agent_name="universe_screen",
            kind="universe_screen_run", scope="run",
            evidence_json=_json.dumps(
                {k: v for k, v in summary.items() if k != "events"}, sort_keys=True,
            ),
        )
        logger.info(
            "UNIVERSE_SCREEN pass: %d candidates, %d screened, %d passed, %d "
            "unreadable, %d change(s), deadline %s",
            run.candidates, run.screened, run.passed, run.inconclusive,
            len(run.events), "hit" if run.deadline_hit else "not hit",
        )
        return summary

    def _attach_universe_changes(self, result) -> None:
        """Hand the screen's unreported changes to the morning message, then
        mark them shown. Screen on only; fail-soft."""
        if not isinstance(result, dict) or not self._universe_screen_enabled():
            return
        from src.universe_screen import UniverseStore

        try:
            store = UniverseStore(self.config.universe_screen.data_dir)
            state = store.load()
            events = list(state.get("events") or [])
            result["universe_changes"] = {
                "events": events,
                "admitted_count": len(state.get("admitted") or {}),
                "flagged_count": sum(
                    1 for r in (state.get("admitted") or {}).values()
                    if r.get("status") == "flagged"
                ),
            }
            if events:
                state["events"] = []
                store.save(state)
        except Exception as exc:  # noqa: BLE001
            logger.warning("universe changes could not be attached: %s", exc)

    def _admit_nominated_external_symbols(
        self,
        symbols: list,
    ) -> tuple[set[str], dict[str, dict]]:
        """Phase 9 (§9.1/§9.2) — admit nominated symbols OUTSIDE the
        configured universe.

        No LLM output (a nomination) can grant BUY eligibility on its
        own — only the deterministic gates in
        `_evaluate_external_admission_gates` can, the SAME gates the
        SEC Form 4 smart-money lane already applies. A symbol already
        inside the configured universe never reaches this function; the
        caller (`MorningResearchStage._run_nomination_responder_pass`)
        filters those out first since they need no gate at all.

        Unlike `_admit_transient_smart_money_symbols`, there is no cap
        applied HERE — the caller has already applied the per-seat and
        global nomination caps (`src.nominations.select_nominations`)
        before a symbol ever reaches this gate, so every symbol passed in
        is already a bounded, ranked candidate.
        """
        admitted: set[str] = set()
        details: dict[str, dict] = {}
        for symbol in sorted({
            str(s).strip().upper() for s in symbols if str(s).strip()
        }):
            eligible, _reason, gate_details = self._evaluate_external_admission_gates(
                symbol, context="nomination",
            )
            if not eligible:
                continue
            details[symbol] = {
                "temporary": True,
                "reason": "nomination_external_admission",
                **gate_details,
            }
            admitted.add(symbol)
        return admitted, details

    def _admit_transient_smart_money_symbols(
        self,
        observations: list,
    ) -> tuple[set[str], dict[str, dict]]:
        """Apply broker and market-quality gates to SEC-qualified purchases.

        The source provider owns filing provenance, P/S parsing, recency,
        materiality and independent-owner clustering. This second gate owns
        the trading-surface facts the SEC cannot know: Alpaca eligibility,
        price, history and liquidity — via `_evaluate_external_admission_gates`,
        shared with the Phase 9 nomination responder lane
        (`_admit_nominated_external_symbols`). The output lives only on
        RunContext.
        """
        cfg = self.config.smart_money
        configured = {
            str(symbol).strip().upper()
            for symbol in self.config.trading.universe
            if str(symbol).strip()
        }
        screen_on = self._universe_screen_enabled()
        grouped: dict[str, list] = {}
        for observation in observations or []:
            symbol = str(getattr(observation, "symbol", "") or "").strip().upper()
            if not symbol or symbol in configured:
                continue
            if str(getattr(observation, "transaction_code", "") or "").upper() != "P":
                continue
            if not bool(getattr(observation, "admission_eligible", False)):
                continue
            if screen_on and not self._form4_admission_is_current(observation):
                logger.info(
                    "UNIVERSE_SCREEN SEC transient admission skipped %s: purchase "
                    "disclosed %s, older than the desk's %d-session horizon",
                    symbol, getattr(observation, "disclosure_date", "?"),
                    int(self.config.risk.max_target_horizon_sessions),
                )
                continue
            grouped.setdefault(symbol, []).append(observation)

        def _rank(item):
            symbol, rows = item
            value = sum(float(getattr(row, "transaction_value_usd", 0) or 0) for row in rows)
            newest = max(str(getattr(row, "known_at", "") or "") for row in rows)
            return (-value, newest, symbol)

        admitted: set[str] = set()
        details: dict[str, dict] = {}
        for symbol, rows in sorted(grouped.items(), key=_rank):
            if len(admitted) >= cfg.max_external_candidates:
                break
            eligible, _reason, gate_details = self._evaluate_external_admission_gates(
                symbol, context="SEC transient",
            )
            if not eligible:
                continue
            accessions = sorted({
                str(getattr(row, "accession_number", "") or "") for row in rows
                if getattr(row, "accession_number", None)
            })
            total_value = round(sum(
                float(getattr(row, "transaction_value_usd", 0) or 0) for row in rows
            ), 2)
            owners = sorted({
                str(getattr(row, "actor", "") or "").strip() for row in rows
                if str(getattr(row, "actor", "") or "").strip()
            })
            # Every admitting row is opportunistic by construction — the
            # provider strips routine purchases from ``admission_eligible``.
            # Carrying the reasons through anyway makes the operator's
            # admission record self-explaining rather than requiring a
            # re-derivation from the raw filing.
            signal_reasons = sorted({
                str(getattr(row, "signal_class_reason", "") or "")
                for row in rows
                if getattr(row, "signal_class_reason", "")
            })
            details[symbol] = {
                "temporary": True,
                "reason": "material_sec_form4_purchase",
                "signal_class": "opportunistic",
                "signal_class_reasons": signal_reasons,
                "accessions": accessions,
                "owners": owners,
                "transaction_value_usd": total_value,
                **gate_details,
            }
            admitted.add(symbol)
        return admitted, details

    def _filter_hard_risk_decisions(
        self,
        decisions: list[TradeDecision],
        positions,
        total_value: float,
        daily_pnl: float,
        baseline: float | None = None,
        invested_target_pct: float | None = None,
        correlation_matrix: dict[str, dict[str, float]] | None = None,
        cash: float | None = None,
        in_drawdown: bool = False,
        # Spec §11.2. The ladder-resolved gross-exposure ceiling for this
        # session. None falls back to the configured cap inside the engine —
        # a caller that forgets it still gets a ceiling, never none.
        gross_ceiling=None,
    ) -> tuple[list[TradeDecision], list, list[str]]:
        allowed_decisions: list[TradeDecision] = []
        remaining_violations = []
        blocked_reasons: list[str] = []
        pending_investment = 0.0
        # Spec §12.2 — keyed by `(sector, side)`. A pending SHORT must not eat
        # the same sector's LONG budget, and vice versa.
        pending_sector_investment: dict[tuple[str, str], float] = {}
        pending_symbol_investment: dict[str, float] = {}
        pending_cash_outflow = 0.0
        # Spec §11.2: running total of GROSS notional (direction-agnostic,
        # leverage-adjusted) already allowed earlier in this batch. Without
        # it two entries in one run would each be measured against only the
        # pre-existing book and never see each other — the same gap
        # `pending_investment` closes for net exposure.
        pending_gross_investment = 0.0
        # Raw (unsigned, UN-leveraged) notional already approved this batch.
        # This is the pending leg of `book_exposure`'s `deployed` measure —
        # capital committed, which is what the invested target
        # (`DESK_INVESTED_TARGET_PCT`) is defined against. Distinct from `pending_cash_outflow` (BUYs only,
        # a funding question) and from `pending_gross_investment` (leverage
        # multiplied, a ceiling question).
        pending_raw_investment = 0.0

        # Cash-sweep view: the parked T-bill vehicle is cash-equivalent —
        # exclude it from the position list so net-exposure / cluster math
        # doesn't count parked cash as market exposure.
        #
        # This gate does NOT credit the parked vehicle's value into the cash
        # budget. Callers pass `ctx.deployable_cash`, which already includes
        # it (raw `cash` + convertible sweep value — see
        # `_compute_deployable_cash`). Crediting it a second time here would
        # double-count the same dollars and approve BUYs execution cannot
        # fund. The `sell_proceeds` credit just below is unrelated and
        # unchanged: it only credits proceeds of REAL position SELLs this
        # same run, which ExecutionStage always executes and waits for
        # before any BUY submits.
        sweeper = self._sweeper()
        if sweeper is not None:
            positions, _parked = sweeper.split_positions(positions)

        # Pre-pass: sum the cash SELLs in this session will return. The
        # execution stage always runs SELLs before BUYs and waits for fills,
        # so by the time a BUY submits, `cash + sell_proceeds` is available.
        # Without this the cash-only rule would block legitimate SELL→BUY
        # rotations that never actually draw on margin.
        sell_proceeds = 0.0
        if cash is not None:
            for d in decisions:
                if d.action != "SELL":
                    continue
                held = next((p for p in positions if p.symbol == d.symbol), None)
                if held is None or held.qty <= 0:
                    continue
                # CLAUDE.md convention: allocation_pct=0 means SKIP (not full sell).
                # Execution stage skips the order; filter must match or we'd
                # credit phantom SELL proceeds to the BUY cash budget, allowing
                # a BUY that actually draws margin at execution time.
                if d.allocation_pct <= 0:
                    continue
                # Alpaca occasionally returns NaN market_value during market-open
                # glitches or for assets with missing prices. Without this guard
                # `sell_proceeds += NaN * frac` poisons effective_cash to NaN,
                # which silently passes every subsequent BUY hard-rule check
                # (`NaN > limit` is False in Python comparisons). Skip the
                # SELL from the pre-sum — its proceeds aren't safely
                # knowable, so the BUY cash budget shouldn't pre-credit them.
                if not math.isfinite(held.market_value):
                    logger.warning(
                        "SELL pre-sum: skipping %s — broker returned non-finite "
                        "market_value=%s; cash budget will be conservative",
                        d.symbol, held.market_value,
                    )
                    continue
                # Mirror ExecutionStage's exact share rounding so the cash
                # budget credits the proceeds the SELL will *actually* realize.
                # ExecutionStage (pipeline_stages.py) rounds a partial alloc to
                # whole shares for integer-qty positions via
                # `max(1.0, int(qty*frac))`, then promotes to a full sell when
                # the rounded qty meets/exceeds the position. The naive
                # `market_value * (alloc/100)` diverges from that both ways:
                #   - under-credits (e.g. 40% of a 1-share lot rounds UP to a
                #     full sell → 100% proceeds) → false-blocks a legit BUY;
                #   - over-credits (e.g. 99% of a 10-share lot rounds DOWN to 9
                #     shares = 90% proceeds) → phantom cash a BUY could borrow.
                # Crediting `eff_qty / held.qty` closes both gaps.
                if d.allocation_pct >= 100:
                    proceeds_frac = 1.0
                else:
                    eff_qty = held.qty * (d.allocation_pct / 100.0)
                    if float(held.qty).is_integer():
                        eff_qty = max(1.0, float(int(eff_qty)))
                    if eff_qty >= held.qty:
                        eff_qty = held.qty  # rounds up to a full exit
                    proceeds_frac = eff_qty / held.qty if held.qty > 0 else 0.0
                sell_proceeds += held.market_value * proceeds_frac
        effective_cash = None if cash is None else cash + sell_proceeds

        for decision in decisions:
            # Stage 3: a SHORT opens/adds new risk exactly as a BUY does, so
            # it must clear the same hard-block gate (D9's short caps live
            # inside `risk_engine.check`). SELL and COVER bypass this gate
            # entirely and fall straight through to `allowed_decisions` —
            # for COVER that is deliberate (D10: a cover can never be
            # blocked), for SELL it always has been.
            if decision.action not in ("BUY", "SHORT"):
                allowed_decisions.append(decision)
                continue

            violations = self.risk_engine.check(
                decision=decision,
                positions=positions,
                total_value=total_value,
                daily_pnl=daily_pnl,
                pending_investment=pending_investment,
                pending_sector_investment=pending_sector_investment,
                pending_symbol_investment=pending_symbol_investment,
                baseline=baseline,
                correlation_matrix=correlation_matrix,
                cash=effective_cash,
                pending_cash_outflow=pending_cash_outflow,
                in_drawdown=in_drawdown,
                # Spec §11.2 — the execution half of the gross ceiling. The
                # sweep vehicle has already been split out of `positions`
                # above, so `cash_park_symbol` here is belt-and-braces for
                # any future caller that has not.
                gross_ceiling=gross_ceiling,
                pending_gross_investment=pending_gross_investment,
                cash_park_symbol=(sweeper.symbol if sweeper is not None else None),
            )
            hard_violations = [v for v in violations if v.rule in HARD_BLOCK_RULES]
            if hard_violations:
                messages = [v.message for v in hard_violations]
                blocked_reasons.extend(messages)
                logger.warning("Hard risk block for %s %s: %s", decision.action, decision.symbol, "; ".join(messages))
                # sector_unresolved_* is advisory (never in HARD_BLOCK_RULES)
                # but must stay visible even when THIS decision is blocked
                # for a different reason (e.g. the pooled "Unknown" bucket
                # itself tripping max_sector_hard_pct) — the whole point is
                # that an unresolved sector must never go quiet, and the
                # loop `continue`s past the ordinary remaining_violations
                # .extend below for a blocked decision.
                remaining_violations.extend(
                    v for v in violations if v.rule.startswith("sector_unresolved")
                )
                continue

            remaining_violations.extend(violations)
            allowed_decisions.append(decision)

            from src.risk.rules import _effective_multiplier, _gross_multiplier
            raw_investment = total_value * (decision.allocation_pct / 100)
            is_short = decision.action == "SHORT"
            # Total exposure accumulates SIGNED contribution (hedges net
            # out). A SHORT moves it the OPPOSITE way a BUY of the same
            # symbol would — the matching flip lives in
            # RiskRuleEngine.check.
            signed_investment = (
                raw_investment * _effective_multiplier(decision.symbol)
                * (-1.0 if is_short else 1.0)
            )
            # Sector exposure accumulates GROSS (direction-agnostic magnitude).
            gross_investment = raw_investment * _gross_multiplier(decision.symbol)
            pending_investment += signed_investment
            # Deployment accumulates RAW notional for BUY *and* SHORT: both
            # commit capital, and neither leverage nor direction changes how
            # much of the book stops being idle cash.
            pending_raw_investment += raw_investment
            if not is_short:
                # Cash outflow is raw $ notional — leverage/direction don't
                # change the brokerage cash the BUY consumes. Inverse/
                # leveraged ETFs still cost their sticker price in cash.
                # A SHORT of any symbol never
                # spends this settled-cash pool (RiskRuleEngine.check), a
                # BUY of any symbol always does.
                pending_cash_outflow += raw_investment
            # Spec §11.2: gross is direction-agnostic — a BUY and a SHORT of
            # the same size consume the same ceiling. `gross_investment` is
            # already the leverage-adjusted unsigned magnitude.
            pending_gross_investment += gross_investment
            pending_symbol_investment[decision.symbol] = (
                pending_symbol_investment.get(decision.symbol, 0.0) + raw_investment
            )
            # Spec §12.2 — books into the `(sector, side)` bucket this order
            # would actually land in, so a pending SHORT never consumes the
            # long budget the next BUY in that sector is measured against.
            from src.risk.rules import accumulate_pending_sector
            accumulate_pending_sector(
                pending_sector_investment, _get_sector(decision.symbol),
                decision.action, gross_investment,
            )

        # Advisory check: projected capital at work vs the invested target
        # (`DESK_INVESTED_TARGET_PCT`, fixed at 100% by the owner mandate of
        # 2026-09-17 — macro no longer sets it). Does NOT block trades; emits
        # a non-hard violation so RiskManager sees the gap. It reports
        # UNDER-deployment only: a book at or above the target (margin is
        # enabled) is not a reason to scale anything down — leverage is
        # already capped, and enforced, by the §11.2 gross ceiling.
        if invested_target_pct is not None and total_value > 0:
            from src.risk.rules import (
                book_exposure, deployment_gap_band_pct, RiskViolation,
            )
            # Read through `book_exposure` — the SAME function that produces
            # PM's `invested_pct`. Before this, the two seats were judged
            # against one target using two definitions with opposite signs
            # (see the measured example on `book_exposure`), and the RM's leg
            # additionally `abs()`-ed a signed net, so a net-SHORT book read
            # as positively invested and was indistinguishable from the
            # equivalent long. `projected` is the book AFTER this batch:
            # deployment counts every approved order's raw notional (a SHORT
            # commits capital too), direction counts them signed.
            projected = book_exposure(
                positions, total_value,
                pending_deployed_usd=pending_raw_investment,
                pending_net_usd=pending_investment,
            )
            projected_invested_pct = projected.deployed_pct
            deviation = projected_invested_pct - invested_target_pct
            # The band is the desk's own sourced cash reserve
            # (`cash_sweep.reserve_pct`), not an invented number — see
            # `deployment_gap_band_pct`. An UNDER-deployed book beyond that
            # reserve is the drag this advisory exists to surface. The OVER
            # branch that told RM to "consider scale_all_buys" was deleted
            # with the mandate — there is no macro target left to be above,
            # and scaling entries down leaves exactly the idle cash the
            # owner ruled out.
            band = deployment_gap_band_pct(getattr(self, "config", None))
            if deviation < -band:
                remaining_violations.append(RiskViolation(
                    rule="deployment_gap",
                    message=(
                        f"Projected invested {projected_invested_pct:.0f}% (capital at "
                        f"work; net direction {projected.net_pct:+.0f}%) is "
                        f"{-deviation:.0f}pp UNDER the fully-invested mandate "
                        f"({invested_target_pct:.0f}%) (advisory — do NOT scale "
                        f"down BUYs or SHORTs for exposure reasons; idle cash is "
                        f"the cost here. If cutting anything, name a risk "
                        f"specific to the trade, not the gap.)"
                    ),
                    value=projected_invested_pct,
                    limit=invested_target_pct,
                ))

        return allowed_decisions, remaining_violations, blocked_reasons

    def _persist_hard_risk_block(self, ctx: RunContext, reasons: str, *, stage: str) -> None:
        """Forensic record for a run where the deterministic hard-risk gate
        blocks EVERY candidate before `risk_manager` is ever called
        (Stage 2 Checkpoint C reconstruction gap).

        Before this, `RiskStage.run()` returned early with an in-memory
        `{"status": "hard_risk_block", "reason": ...}` dict above the
        `pipeline.risk_manager.review(...)` call — the reason reached a log
        line and a Telegram push, but no row in any table recorded which
        rule fired. This reuses the existing `agent_logs` table via the
        existing `insert_agent_log` mechanism: additive only, no schema
        change, no second risk system, no change to what gets blocked or
        why.

        `agent_name="risk_gate"` is a deliberately distinct sentinel from
        the real `"risk_manager"` LLM agent name so this can never be
        confused with an actual LLM call: `scripts/replay_decision.py`
        selects rows to replay by exact `agent_name` match and would
        otherwise try to replay an empty prompt; per-agent cost/roster
        views (`AGENT_NAMES`-driven) and `Database.agent_names_logged_on`'s
        dead-man's-switch check are unaffected since neither iterates
        unknown agent_names. `cost_usd`/`tokens_used` are 0 (known-zero,
        not unknown — no LLM call happened), not None, so
        `Database.sum_session_cost`'s any-null-means-unknown convention
        doesn't corrupt this run's otherwise-known research/PM cost total.

        Never raises — a persistence failure here must never affect the
        early-return risk decision itself, which has already been made by
        the time this is called.
        """
        try:
            self.db.insert_agent_log(
                agent_name="risk_gate", run_id=ctx.run_id,
                input_summary=f"deterministic hard-risk gate blocked all candidates ({stage})",
                input_message="",
                output_summary=f"HARD_RISK_BLOCK: {reasons}",
                full_response=reasons,
                model="deterministic",
                tokens_used=0, input_tokens=0, output_tokens=0, cost_usd=0.0,
                provider_requests=0,
                decision_id=ctx.decision_id,
                status="hard_risk_block",
            )
        except Exception as exc:
            logger.warning(
                "hard_risk_block: failed to persist forensic record for run %s: %s",
                ctx.run_id, exc,
            )

    _FIELD_ALIASES = {
        "target": "take_profit",
        "tp": "take_profit",
        "stop": "stop_loss",
        "sl": "stop_loss",
        "price": "entry_price",
        "alloc": "allocation_pct",
    }

    def _apply_risk_modifications(
        self,
        decisions: list[TradeDecision],
        modifications,
        symbols_bars: dict | None = None,
        unapplied: list[dict] | None = None,
    ) -> tuple[list[TradeDecision], list[dict]]:
        """Apply RM-proposed field modifications to decisions.

        When a mod fails Pydantic validation, the decision is **dropped** rather
        than left at its original (un-tightened) value. RM's job is to be more
        protective; if their proposed change can't be applied, we cannot assume
        the un-modified decision is safe — the safest invariant is "RM tried to
        change this, we couldn't, so don't execute it". Previously a break left
        the original decision in place, silently dropping RM's protective intent.

        Two further guards (2026-09-03 audit), both because a field-valid
        `TradeDecision` is not the same thing as a MORE PROTECTIVE one, and
        this function's whole job is the latter:

        1. **An exit can never be silently cancelled by an edit.** A SELL or
           COVER's `allocation_pct` reaching 0 through an RM modification
           reads as "skip" at execution (see `pipeline_stages.py`'s
           `RiskStage.run`, "CLAUDE.md convention: allocation_pct=0 means
           SKIP") — a real exit vanishes with no distinguishable trace.
           Observed live 2026-08-24 on two symbols. If the RM genuinely
           believes an exit should not happen, it already has a real
           mechanism for that — `RiskVerdict.rejected_symbols`
           (`SymbolRejection`, handled in `RiskStage.run` before this method
           ever runs) — a REFUSAL, distinguishable from an edit. A
           modification is not that mechanism, so this method refuses the
           EDIT (keeps the exit at its pre-modification size) rather than
           refusing the trade itself: reverting is the closer match to "RM
           tried to protect this and couldn't", the same invariant already
           governing the validation-failure branch below, and it does not
           require inventing a new rejection channel for something the
           schema already has one for.
        1b. **A BUY's `allocation_pct` may only be reduced.** This seat exists
           to be MORE protective than the constructor, and nothing enforced
           that: `RiskModification.new_value` is unbounded. On a BUY that adds
           to a held name the field is an INCREMENT on top of the existing
           weight, so an upward edit grows the position by more than the
           number reads (2026-09-18: an edit believed to cut a name to 30%
           left it at 50.8%). An increase is reverted and recorded in
           `rejected_mods` — same posture as guard 1, the trade still ships at
           the constructor's size. Checked AFTER schema validation, unlike
           guard 1: an out-of-range value (allocation_pct > 100) must keep
           hitting the validation branch below and DROP the decision, which is
           stricter still. This guard governs only the values Pydantic accepts.
        2. **A stop/target edit cannot bypass the checks a fresh decision
           would have to clear.** The constructor measures reward:risk on a
           range setup (refusing only an UNMEASURABLE ratio — a computed
           ratio is a ranking input, not a gate, and a breakout
           is never measured) and enforces a noise-band stop distance before
           a decision ever reaches the Risk Manager; both checks compared a
           modified decision only against itself, so an RM edit that widened
           a stop or pulled in a target could ship a BUY/SHORT whose
           reward:risk the constructor could not have measured, or a stop
           resting inside the ATR noise band. Invented numeric reward:risk
           floors are retired. This reuses the SAME arithmetic (`TradeDecision.reward_risk`,
           which is `models.reward_to_risk` — the one ratio definition every
           other gate in this codebase already shares) and the SAME
           configured floor (`RiskConfig.absolute_min_stop_atr_multiple`) the
           constructor uses, rather than re-deriving either. The noise-band
           half only runs when `symbols_bars` is supplied and yields a usable
           ATR reading; when it can't be computed the edit is refused rather
           than guessed at ("reject outright if it can't be safely
           re-verified" — the same posture as the noise-band check itself,
           which does not invent a stop distance it cannot measure).

        Returns `(decisions, rejected_mods)`. `rejected_mods` records every
        modification this method refused to apply — as opposed to a decision
        DROPPED outright by a validation failure — so the caller can persist
        a visible pipeline event for each one instead of the edit just
        disappearing.

        `unapplied` (board item 164, 2026-09-19) is an optional sink for the
        three outcomes `rejected_mods` deliberately does NOT carry, each of
        which used to reach the log only: a decision DROPPED because the
        edit failed schema validation (`outcome="dropped"`), an edit naming
        a field this method cannot modify, and an edit naming a symbol with
        no decision in the plan (both `outcome="modification_not_applied"`).
        Each entry names the symbol, the gate, the value asked for and the
        seat's own reason. Recording only — nothing here changes what is
        applied, reverted or dropped.
        """
        updated_decisions: list[TradeDecision | None] = list(decisions)
        modifiable_fields = {"allocation_pct", "entry_price", "stop_loss", "take_profit"}
        rejected_mods: list[dict] = []

        for mod in modifications:
            field = self._FIELD_ALIASES.get(mod.field, mod.field)
            if field != mod.field:
                logger.info("Risk mod field alias: '%s' -> '%s'", mod.field, field)
                mod = type(mod)(**{**mod.model_dump(), "field": field})
            if mod.field not in modifiable_fields:
                logger.warning("Risk mod ignored: unknown field '%s'", mod.field)
                if unapplied is not None:
                    unapplied.append({
                        "symbol": mod.symbol, "field": mod.field,
                        "outcome": "modification_not_applied",
                        "gate": "rm_modification_unknown_field",
                        "requested": mod.new_value,
                        "seat_reason": mod.reason,
                        "reason": (
                            f"RM modification NOT APPLIED: {mod.symbol}.{mod.field} "
                            f"-> {mod.new_value} names a field the desk cannot "
                            f"modify (modifiable: "
                            f"{', '.join(sorted(modifiable_fields))}). The "
                            f"decision is unchanged. RM reason given: "
                            f"{mod.reason!r}"
                        ),
                    })
                continue

            for idx, decision in enumerate(updated_decisions):
                if decision is None or (
                    decision.symbol.strip().upper() != mod.symbol.strip().upper()
                ):
                    continue

                # Guard 1 — an exit's allocation must never be silently
                # zeroed through a "modification". This is checked BEFORE
                # the candidate is even built: a valid-but-zero
                # allocation_pct would sail straight through Pydantic.
                if (
                    decision.action in ("SELL", "COVER")
                    and mod.field == "allocation_pct"
                    and decision.allocation_pct > 0
                    and float(mod.new_value) <= 0
                ):
                    reason = (
                        f"RM modification would zero {mod.symbol}'s exit "
                        f"allocation_pct ({decision.allocation_pct:.2f} -> "
                        f"{mod.new_value:.2f}), silently cancelling a "
                        f"{decision.action}. Reverted — an exit is not "
                        f"skipped by edit; a real refusal belongs in "
                        f"rejected_symbols. RM reason given: {mod.reason!r}"
                    )
                    logger.warning("Risk mod REJECTED for %s: %s", mod.symbol, reason)
                    rejected_mods.append({
                        "symbol": mod.symbol,
                        "field": mod.field,
                        "reason": reason,
                    })
                    # updated_decisions[idx] already holds the unmodified
                    # decision — nothing to change, the exit still ships.
                    break

                candidate = decision.model_dump()
                candidate[mod.field] = mod.new_value
                try:
                    updated_decision = TradeDecision(**candidate)
                except ValidationError as exc:
                    logger.warning(
                        "Risk mod rejected for %s.%s %.4f -> %.4f: %s — "
                        "DROPPING decision (RM intended a protection we cannot apply)",
                        mod.symbol, mod.field, mod.original_value, mod.new_value, exc,
                    )
                    if unapplied is not None:
                        errors = "; ".join(
                            f"{'.'.join(str(p) for p in err.get('loc', ()))}: "
                            f"{err.get('msg', '')}"
                            for err in exc.errors()
                        )
                        unapplied.append({
                            "symbol": decision.symbol, "field": mod.field,
                            "outcome": "dropped",
                            "gate": "rm_modification_schema_invalid",
                            "action": decision.action,
                            "before": getattr(decision, mod.field, None),
                            "requested": mod.new_value,
                            "seat_reason": mod.reason,
                            "reason": (
                                f"{decision.action} {decision.symbol} DROPPED: "
                                f"the RM edit {mod.field} "
                                f"{getattr(decision, mod.field, None)} -> "
                                f"{mod.new_value} fails the order schema "
                                f"({errors}), and a protection the seat asked "
                                f"for that cannot be applied is not assumed "
                                f"safe to skip. RM reason given: {mod.reason!r}"
                            ),
                        })
                    updated_decisions[idx] = None
                    break

                # Guard 1b — an `allocation_pct` edit on a BUY may only
                # REDUCE. The Risk Manager's stated job at this seat is to be
                # MORE protective than the constructor; nothing in the schema
                # enforced that for this field (`RiskModification.new_value`
                # is unbounded and `TradeDecision.allocation_pct` only clamps
                # 0-100), so a larger number sailed through as a
                # "protection". Compounding it, on a BUY that ADDS the field
                # is an INCREMENT on top of the existing holding, so an
                # upward edit grows the position by more than the number
                # suggests — observed 2026-09-18, where an edit the seat
                # believed cut a name to 30% left it at 50.8%. The prompt now
                # states the increment and the resulting weight; this guard is
                # the part that holds regardless of what the model reasons.
                if (
                    decision.action == "BUY"
                    and mod.field == "allocation_pct"
                    and float(mod.new_value) > decision.allocation_pct
                ):
                    reason = (
                        f"RM modification would INCREASE {mod.symbol}'s BUY "
                        f"allocation_pct ({decision.allocation_pct:.2f} -> "
                        f"{mod.new_value:.2f}). Reverted — the risk seat may "
                        f"only reduce a BUY's size, never enlarge it; on an "
                        f"add this field is an increment, so an upward edit "
                        f"grows the position by more than the number reads. "
                        f"RM reason given: {mod.reason!r}"
                    )
                    logger.warning("Risk mod REJECTED for %s: %s", mod.symbol, reason)
                    rejected_mods.append({
                        "symbol": mod.symbol,
                        "field": mod.field,
                        "reason": reason,
                    })
                    # updated_decisions[idx] already holds the unmodified
                    # decision — the BUY ships at the constructor's size.
                    break

                # Guard 2 — a stop/target edit on a BUY/SHORT must not ship
                # a reward:risk the constructor would have refused, or (when
                # verifiable) a stop inside the ATR noise band.
                if decision.action in ("BUY", "SHORT") and mod.field in (
                    "stop_loss", "take_profit",
                ):
                    floor_reason = self._risk_mod_floor_breach(
                        decision, updated_decision, mod, symbols_bars,
                    )
                    if floor_reason is not None:
                        logger.warning(
                            "Risk mod REJECTED for %s: %s", mod.symbol, floor_reason,
                        )
                        rejected_mods.append({
                            "symbol": mod.symbol,
                            "field": mod.field,
                            "reason": floor_reason,
                        })
                        break

                logger.info(
                    "Risk mod applied: %s.%s %.4f -> %.4f (%s)",
                    mod.symbol, mod.field, mod.original_value, mod.new_value, mod.reason,
                )
                updated_decisions[idx] = updated_decision
                break
            else:
                logger.warning("Risk mod ignored: no matching decision for '%s'", mod.symbol)
                if unapplied is not None:
                    unapplied.append({
                        "symbol": mod.symbol, "field": mod.field,
                        "outcome": "modification_not_applied",
                        "gate": "rm_modification_no_matching_decision",
                        "requested": mod.new_value,
                        "seat_reason": mod.reason,
                        "reason": (
                            f"RM modification NOT APPLIED: {mod.symbol} has no "
                            f"decision left in the plan to edit ({mod.field} -> "
                            f"{mod.new_value}), so nothing changed. RM reason "
                            f"given: {mod.reason!r}"
                        ),
                    })

        return [d for d in updated_decisions if d is not None], rejected_mods

    def _risk_mod_floor_breach(
        self,
        original: TradeDecision,
        modified: TradeDecision,
        mod,
        symbols_bars: dict | None,
    ) -> str | None:
        """Return a refusal reason if `modified` breaches a constructor
        risk-side floor, else None.

        **2026-09-17.** Invented reward:risk floors are retired. A computed
        or missing ratio does not refuse an RM edit. What still refuses the
        *edit* (not the ticket) is a stop pulled inside the ATR noise band.
        """
        if mod.field != "stop_loss" or not symbols_bars:
            return None

        # Noise-band check — only attempted when bars are available to
        # compute a real ATR reading. `RiskConfig.absolute_min_stop_atr_multiple`
        # is the same configured floor `PortfolioConstructor._widen_stop_past_noise`
        # enforces; this does not invent a new number.
        bars = symbols_bars.get(original.symbol)
        if not bars or len(bars) < 15:
            return None
        try:
            atr14 = compute_indicators(original.symbol, bars).atr_14
        except Exception as exc:
            logger.warning(
                "Risk mod noise-band check skipped for %s: ATR unavailable (%s)",
                original.symbol, exc,
            )
            return None
        if atr14 is None or not math.isfinite(atr14) or atr14 <= 0:
            return None

        # Same defensive posture as `_optional_risk_number` above: tests
        # build this pipeline against `TradingPipeline.__new__`, which never
        # ran `__init__` and carries no `self.config` at all. Absence of a
        # real config means the floor cannot be verified — skip rather than
        # crash or guess at a multiple nobody configured.
        risk_cfg = getattr(getattr(self, "config", None), "risk", None)
        floor_multiple = _optional_risk_number(
            getattr(risk_cfg, "absolute_min_stop_atr_multiple", None)
        )
        if floor_multiple is None:
            return None

        is_short = modified.action == "SHORT"
        entry = modified.entry_price
        new_stop = modified.stop_loss
        distance = (entry - new_stop) if not is_short else (new_stop - entry)
        band_edge = floor_multiple * atr14
        if distance < band_edge:
            return (
                f"modified stop ${new_stop:.2f} sits {distance:.2f} from "
                f"entry ${entry:.2f} — inside the {floor_multiple}x ATR14 "
                f"(${atr14:.2f}) noise band (${band_edge:.2f} minimum) the "
                f"constructor enforces. RM reason given: {mod.reason!r}"
            )
        return None

    @staticmethod
    def _has_actionable_signal_fn(
        indicators, symbol: str, bars, positions, live_price: float | None = None,
    ) -> bool:
        """Pre-filter: only send symbols with interesting signals to the LLM.

        Lifted from a nested function in run_morning so MorningResearchStage
        can inject it as a dependency. Takes positions explicitly rather than
        closing over an outer scope.

        `live_price` (2026-09-14): during market hours the price-vs-band
        proximity check uses the live price, not the last completed close;
        the bands themselves stay on completed bars.
        """
        held_symbols = {p.symbol for p in positions}
        if symbol in held_symbols:
            return True
        if not isinstance(indicators, TechnicalIndicators):
            return True  # can't filter unknown types, pass through
        if indicators.rsi_14 is not None and (indicators.rsi_14 < 35 or indicators.rsi_14 > 65):
            return True
        if indicators.bb_upper and indicators.bb_lower and bars:
            last_close = (
                live_price
                if isinstance(live_price, (int, float)) and live_price > 0
                else bars[-1].close
            )
            band_width = indicators.bb_upper - indicators.bb_lower
            if band_width > 0:
                if abs(last_close - indicators.bb_upper) / band_width < 0.1:
                    return True
                if abs(last_close - indicators.bb_lower) / band_width < 0.1:
                    return True
        if indicators.macd_hist is not None and len(bars) >= 27:
            # A MACD histogram merely being small is common, not a signal.
            # The original prefilter intended to catch a histogram changing
            # sign, but implemented only "near zero"; in production that
            # admitted most of the universe (36/75 sampled names on 2026-08-26
            # qualified solely through this clause).  Recompute the prior
            # completed bar and require an actual zero-line crossover.
            try:
                previous_hist = compute_indicators(symbol, bars[:-1]).macd_hist
            except Exception:
                previous_hist = None
            if previous_hist is not None and (
                (previous_hist < 0 < indicators.macd_hist)
                or (previous_hist > 0 > indicators.macd_hist)
            ):
                return True
        if indicators.volume_change_pct is not None and abs(indicators.volume_change_pct) > 50:
            return True
        if indicators.ma_20 and indicators.ma_50:
            spread = abs(indicators.ma_20 - indicators.ma_50)
            if indicators.atr_14 and indicators.atr_14 > 0:
                if spread < 0.5 * indicators.atr_14:
                    return True
            else:
                if spread / indicators.ma_50 < 0.02:
                    return True
        return False

    @staticmethod
    def _resolve_live_context(snapshots: dict, symbols: list) -> tuple:
        """Freshness-resolve a bulk snapshot reply into per-symbol context.

        Shared by the morning Tech pass and the intraday opportunity scan,
        because both hand the SAME payload to the SAME seat and only one of
        them used to check it (docs/WORK.md item 120). Returns
        `(context, missing, stale, rescued)`.

        `context[sym]` is either `{"live_unavailable": reason}` or the raw
        snapshot decorated with `live_price` / `live_price_source` /
        `live_price_at` / `live_price_description`, with the `session_*`
        block blanked when the daily bar in it belongs to a prior session.
        """
        from src.data.live_price import (
            NO_PRICE_AT_ALL, SOURCE_LAST_TRADE, resolve_live_price,
        )

        out: dict[str, dict] = {}
        missing: list[str] = []
        stale: list[str] = []
        rescued: dict[str, str] = {}
        blanked: list[str] = []
        for sym in symbols:
            snap = snapshots.get(sym) or {}
            resolved = resolve_live_price(snap)
            if resolved.price is None:
                (missing if resolved.unavailable == NO_PRICE_AT_ALL
                 else stale).append(sym)
                out[sym] = {"live_unavailable": resolved.unavailable}
                continue
            # The RAW provider price is deliberately NOT republished here.
            # It sits a key away from the resolved one, still carrying a
            # prior session's number, and the next reader picking the wrong
            # one is this bug returning. What no consumer can reach, no
            # consumer can misread.
            entry = {k: v for k, v in snap.items()
                     if k not in ("last_price", "minute_close")}
            entry["live_price"] = resolved.price
            entry["live_price_source"] = resolved.source
            entry["live_price_at"] = resolved.as_of
            entry["live_price_description"] = resolved.describe()
            if not resolved.session_bar_is_today:
                # The daily bar in this payload belongs to a PRIOR session.
                # Blank it rather than let a caller render yesterday's
                # open/high/low/volume under a "today" heading.
                blanked.append(sym)
                for field in ("session_open", "session_close", "session_high",
                              "session_low", "session_volume"):
                    entry[field] = None
            if resolved.source != SOURCE_LAST_TRADE:
                rescued[sym] = resolved.source
            out[sym] = entry
        # Blanking every priced name at once is the signature of the one
        # assumption in `resolve_live_price` that has never been checked
        # against a live call: that a daily bar's timestamp carries the
        # session's ET date. If that is wrong this fires on day one instead
        # of the session range vanishing silently.
        priced = len(symbols) - len(missing) - len(stale)
        if blanked and priced and len(blanked) == priced:
            logger.error(
                "live session context: EVERY priced symbol (%d) had a daily "
                "bar dated to a prior session. One name is ordinary; all of "
                "them means the daily-bar timestamp convention is not what "
                "`src/data/live_price.py` assumes — check it before trusting "
                "any session range", len(blanked),
            )
        elif blanked:
            logger.info(
                "live session context: %d symbol(s) carried a PRIOR session's "
                "daily bar; their session range is blanked rather than shown "
                "as today's (item 120): %s", len(blanked), blanked[:10],
            )
        return out, missing, stale, rescued

    def _live_session_context(self, symbols) -> dict[str, dict]:
        """Live, in-progress-session price facts for `symbols`, or {}.

        2026-09-14 (docs/INCIDENT_HISTORY.md, ORCL 2026-09-10): the morning
        Tech pass compared price against levels using bars that end at the
        PREVIOUS close, so a stock that opened below its support still
        read as above it. This supplies the live price for the same
        seats, from the broker snapshot the intraday scan already uses
        (`get_intraday_snapshots` — no new data source).

        - Outside regular hours: {} — completed bars ARE current (after the
          close today's bar is complete; pre-market/weekend the last close
          is the latest price that exists).
        - In session: one bulk snapshot, resolved through
          `src.data.live_price.resolve_live_price`. A symbol with no print
          from TODAY on any of the snapshot's three print-derived fields
          gets `{"live_unavailable": reason}` and a WARNING — rendered as an
          explicit STALE label, never silently replaced by yesterday, and
          never replaced by a quote mid.
        Never raises.

        2026-09-20, board item 120: this used to read `last_price` alone. On
        2026-09-17 that cost 8 of 104 names their technical seat at the open
        while today's forming bar in the SAME payload already held the open,
        because a thin name's `latest_trade` can still be yesterday's minutes
        into the session on an IEX entitlement. Two changes follow from that:
        the resolver now falls through to today's minute bar and then today's
        forming session bar (both aggregations of real prints on the same
        entitled venue, neither a quote), and the `session_*` block is
        BLANKED when the snapshot's daily bar is not today's — Alpaca returns
        the previous session's bar in that slot for a name that has not
        printed, and it was being rendered to the analyst as "CURRENT SESSION
        (TODAY)".

        The resolved number is published as `live_price` (with
        `live_price_source` and `live_price_at`), NOT as `last_price`. The
        raw provider field keeps its own name so no reader can pick up an
        unchecked number believing it was checked.
        """
        from src.trading_calendar import in_regular_session

        symbols = [s for s in (symbols or []) if s]
        if not symbols or not in_regular_session():
            return {}
        try:
            snapshots = self.broker.get_intraday_snapshots(symbols) or {}
        except Exception as exc:  # noqa: BLE001
            logger.warning("live session context: snapshot read failed: %s", exc)
            snapshots = {}
        out, missing, stale, rescued = self._resolve_live_context(snapshots, symbols)
        if missing or stale:
            logger.warning(
                "live session context: in-session price unavailable for %d/%d "
                "symbol(s) (no price: %s; no today print: %s) — labelled STALE "
                "in the Tech prompt, not replaced by the last close and never "
                "by a quote mid",
                len(missing) + len(stale), len(symbols),
                missing[:10], stale[:10],
            )
        if rescued:
            logger.info(
                "live session context: %d/%d symbol(s) had no today last-trade "
                "print but a today bar on the same venue, priced from it "
                "rather than losing the seat (item 120): %s",
                len(rescued), len(symbols), sorted(rescued.items())[:10],
            )
        return out

    # Statuses Alpaca uses for terminal/non-terminal orders. Kept as a
    # class attribute so tests can introspect the exact set the
    # finalizer treats as "done".
    _TERMINAL_ORDER_STATUSES = {
        "filled", "canceled", "cancelled", "expired", "rejected",
        "done_for_day", "replaced",
    }

    def _current_position_qty_for_finalize(self, symbol: str) -> float | None:
        """Re-read broker position for finalize residual / restore math.

        intra_check is exempt from the cross-mode session lock, so an
        EMERGENCY_SELL on the same symbol can reduce position between
        when this SELL submitted and when this finalize runs. The cached
        ``position_qty_before_sell`` no longer reflects reality —
        ``position_qty_before_sell - my_fill_qty`` over-states residual
        and the resulting reprotect / restore would submit for more
        shares than exist (broker rejects on insufficient qty, finalize
        bails, drain persists a row, drain replays same wrong math,
        row stays stuck forever).

        Returns:
            >0 — broker reports this many shares held now
            0  — symbol no longer held (concurrent path fully exited)
            None — could not determine (broker error, mocked test path)
        """
        try:
            positions = self.broker.get_positions()
        except Exception as exc:
            logger.warning(
                "get_positions failed during finalize for %s: %s — "
                "falling back to cached residual math",
                symbol, exc,
            )
            return None
        if not isinstance(positions, list):
            return None
        for p in positions:
            sym = getattr(p, "symbol", None)
            if sym == symbol:
                qty = getattr(p, "qty", None)
                if qty is None:
                    return None
                try:
                    return float(qty)
                except (TypeError, ValueError):
                    return None
        return 0.0

    def _reconcile_stop_coverage(self) -> list[dict]:
        """Broker-truth stop-coverage audit, independent of the WAL queue.

        At session entry, enumerate every held position — long or short —
        and compare its held qty against the qty actually covered by its
        open protective stops at the broker (SELL-stops for a long,
        BUY-stops for a short). Flag (log + return) any position whose
        covered qty is materially below its held qty.

        Why this exists (design review's strongest finding): the whole
        naked-protection guarantee otherwise rests on some code path having
        successfully persisted a WAL recovery row. A position that goes naked
        WITHOUT a row — a best-effort persist that silently failed, a manual
        broker action, a future SELL path that skips a step — is never
        re-detected, because the WAL is a log of INTENDED operations, not an
        audit of ACTUAL broker coverage. This reconciler closes that gap by
        reading broker truth directly.

        Auto-repairing for longs AND shorts (see `_repair_stop_coverage` —
        it reconstructs the stop from the recorded opening row: BUY for a
        long, SHORT for a short). Inventing a level is still refused when
        that row has none; the SHORT row stores `stop_loss` the same way
        BUY does, so the old "no recorded entry for a short" objection is
        false.

        Symbols already queued for WAL recovery are skipped — the drain owns
        them. Returns the list of under-covered ``{symbol, held_qty,
        covered_qty, coverage, repaired}`` for the caller to surface to the
        operator.

        SPEC §11.1 HYBRID FRACTIONAL STOPS — this sweep is also the
        re-placement mechanism, and the alerting distinction lives here.

        A fractional position is covered by two orders: a durable GTC stop
        over floor(qty) and a DAY stop over the sub-share remainder, which
        the broker expires at 16:00 ET by design. That means "held qty
        exceeds covered qty" is now THREE different situations, not one, and
        reporting them identically would be the worst possible outcome — a
        nightly red banner on an expected state teaches the owner to ignore
        the banner that must never be ignored:

          (a) the durable GTC leg is intact, only the sub-share remainder is
              uncovered, and the market is SHUT. Expected. Stamped
              ``coverage='fractional_overnight'`` with ``uncovered_qty`` and
              ``unprotected_value`` so the exposure is a NUMBER the owner can
              read. No repair (a DAY order into a shut market is a rejection
              at best), no banner, no escalation.
          (b) the same shortfall while the market is OPEN. A placement
              failure. Repaired in place; if the repair lands it is stamped
              ``'fractional_replaced'`` — this is the ordinary start-of-
              session heartbeat and stays quiet — and if it does NOT land it
              falls back onto guard 3's existing ladder and alerts exactly as
              before.
          (c) the whole-share GTC leg is missing or short. Never suppressed,
              never reclassified, market hours irrelevant: 'none' escalates
              to the owner, 'partial' banners. Unchanged from guard 3.

        The three §11.1 guards are extended by this, not replaced: the retry
        burst (guard 1) now runs over each hybrid leg, the owner alert (guard
        2) still fires on a genuine partial cover, and this sweep still
        separates NO STOP AT ALL from STOP MIS-SIZED (guard 3).
        """
        try:
            positions = self.broker.get_positions()
        except Exception as exc:  # noqa: BLE001
            logger.warning("coverage reconcile: get_positions failed: %s", exc)
            return []
        if not isinstance(positions, list):
            return []
        try:
            pending_syms = {
                r.get("symbol") for r in self.db.get_pending_protection_restores()
            }
        except Exception:  # noqa: BLE001
            pending_syms = set()

        # Spec §11.1 hybrid fractional stops. Read ONCE per pass, not per
        # position: every gap in this sweep must be judged against the same
        # clock, or a sweep straddling 16:00 ET could call one symbol's
        # lapse expected and the next symbol's identical lapse a failure.
        market_open = _market_is_open_now(self.broker)

        gaps: list[dict] = []
        # Positions whose protective stop has been elected and has not
        # filled. Kept OUT of `gaps`: every consumer of that list buckets a
        # row as "no stop at all" or "stop mis-sized", and this is neither —
        # the stop is present and correctly sized, it simply did not fill.
        elected_unfilled: list[dict] = []
        longs_checked = 0
        shorts_checked = 0
        sweeper = self._sweeper()
        # A DISABLED sweep's vehicle is still exempt while it is held: it is
        # awaiting `_release_retired_cash_park`, not naked.
        sweep_symbol = (
            sweeper.symbol if sweeper is not None
            else self._retired_cash_park_symbol()
        )
        for p in positions:
            symbol = getattr(p, "symbol", None)
            try:
                qty = float(getattr(p, "qty", 0) or 0)
            except (TypeError, ValueError):
                continue
            # A short carries a negative qty (Alpaca convention) and is a
            # real, currently-unreachable-but-possible position (shorts-safe,
            # Stage 2). `qty <= 0` used to exempt every short from this audit
            # outright — the "a SELL-stop can't protect a short" reasoning
            # was true, but the fix is to check the OTHER side's stops, not
            # to skip the check. Inverse-ETF hedges have their own handling.
            # Skip symbols the drain already owns.
            if not symbol or qty == 0 or symbol in pending_syms:
                continue
            # The cash-sweep vehicle is deliberately stopless (cash-equivalent;
            # see src/execution/cash_sweep.py) — flagging it every session
            # would train the operator to ignore the 🔴 banner.
            if sweep_symbol is not None and symbol == sweep_symbol:
                continue
            is_short = qty < 0
            if is_short:
                shorts_checked += 1
            else:
                longs_checked += 1
            try:
                _ok, specs = self.broker.snapshot_protective_stops(
                    symbol, side=("buy" if is_short else "sell"),
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "coverage reconcile: snapshot failed for %s: %s", symbol, exc,
                )
                continue
            covered = sum(float(s.get("qty", 0) or 0) for s in (specs or []))
            held = abs(qty)
            # ---- ELECTED BUT UNFILLED -------------------------------------
            # Runs for EVERY held position, including the ones this sweep is
            # about to call perfectly covered — which is the entire point.
            # The desk's protective stops rest at the broker as stop-LIMIT
            # orders (see `StopLimitOrderRequest` / `STOP_LIMIT_BUFFER_PCT`).
            # On a gap past the limit the stop is ELECTED and does not fill,
            # and the order stays `status=OPEN`, so `specs` above still
            # counts its shares as covered and this sweep — correctly by its
            # own logic — does nothing about it, indefinitely. The shares are
            # not protected: an unfilled order is not an exit.
            #
            # Detected only while the market is OPEN, reusing the one
            # `market_open` read this pass already took: with the tape shut
            # there is no live "price through the stop", only yesterday's
            # close, and nobody could act on it anyway.
            if market_open:
                row = self._elected_unfilled_stop_row(
                    p, specs, is_short=is_short,
                )
                if row is not None:
                    elected_unfilled.append(row)
            if covered + 1e-6 < held:
                # Spec §11.1 guard 3. NO STOP AT ALL and STOP PRESENT BUT
                # MIS-SIZED were previously one condition with one message.
                # They are not the same thing and must never read as if they
                # were: a position stopped at the wrong size still has a
                # broker order standing watch over most of it, while a
                # position with zero coverage has nothing between it and the
                # tape. The second is the state that ends a desk, and it was
                # being reported in the same sentence as the first.
                coverage, frac_uncovered = _classify_coverage_gap(
                    held=held, covered=covered,
                )
                gap = {
                    "symbol": symbol, "held_qty": qty, "covered_qty": covered,
                    "coverage": coverage,
                }
                # ---- Spec §11.1 hybrid fractional stops: case (a) ----
                # The durable whole-share GTC leg is intact and the only
                # thing missing is the sub-share remainder, whose DAY stop
                # the broker expires at 16:00 ET BY DESIGN. Outside session
                # hours that is not a fault, it is the mechanism working, and
                # it happens to EVERY fractional position EVERY night. It is
                # reported as measured overnight exposure — a number the
                # owner can look at — and it does not touch either red
                # banner or the owner escalation. Nor is a repair attempted:
                # a DAY order submitted into a shut market is a rejection at
                # best and a surprise queued order at worst.
                if coverage == "fractional" and not market_open:
                    gap["coverage"] = "fractional_overnight"
                    gap["uncovered_qty"] = frac_uncovered
                    gap["unprotected_value"] = _position_notional(
                        p, frac_uncovered,
                    )
                    gap["repaired"] = False
                    logger.info(
                        "FRACTIONAL DAY STOP LAPSED (expected): %s held=%.4f, "
                        "%.4f whole share(s) still covered by the durable GTC "
                        "stop, %s sub-share remainder unprotected until the "
                        "next session re-places its DAY stop.",
                        symbol, qty, covered, frac_uncovered,
                    )
                    gaps.append(gap)
                    continue
                if coverage == "fractional":
                    # ---- case (b), first half: session hours ----
                    # The remainder should be covered RIGHT NOW. Repair it,
                    # and only if the repair fails does it carry a real
                    # condition name into the alerting below.
                    logger.warning(
                        "FRACTIONAL STOP MISSING DURING SESSION HOURS: %s "
                        "held=%.4f, %.4f covered — the sub-share DAY stop is "
                        "absent while the market is OPEN, which is a placement "
                        "failure, not the expected overnight lapse. Repairing.",
                        symbol, qty, covered,
                    )
                    repaired = self._repair_stop_coverage(
                        symbol, held - covered, is_short=is_short,
                        outcome=gap, resting_stops=list(specs or []),
                    )
                    gap["repaired"] = repaired
                    if repaired:
                        # Re-placed inside the same pass. This is the ordinary
                        # start-of-session path for every fractional position
                        # the desk holds, so it must NOT read as a red banner
                        # — it is the design's daily heartbeat.
                        gap["coverage"] = "fractional_replaced"
                        gap["uncovered_qty"] = 0.0
                        logger.info(
                            "FRACTIONAL DAY STOP RE-PLACED: %s — the sub-share "
                            "remainder is covered again for this session.",
                            symbol,
                        )
                    else:
                        # Could not re-place during session hours. Falls back
                        # onto guard 3's existing ladder unchanged: zero
                        # coverage escalates, some coverage banners.
                        gap["coverage"] = "none" if covered <= 1e-6 else "partial"
                        gap["uncovered_qty"] = held - covered
                        gap["unprotected_value"] = _position_notional(
                            p, held - covered,
                        )
                        # The marker that makes the log line below TRUE.
                        # It used to be a claim only: a partial fallback
                        # ('partial' whenever any whole share is still
                        # covered, which is every fractional position) never
                        # reached `_alert_owner_no_stop`, and the only code
                        # that could page lived in the standalone coverage
                        # watchdog — a separate process that need not be
                        # running, and was not on 2026-09-18, when NET and
                        # RSG sat uncovered during the session and the owner
                        # was never told. The session that OBSERVED it now
                        # sends it.
                        gap["session_repair_failed"] = True
                        gap["is_short"] = is_short
                        logger.error(
                            "FRACTIONAL STOP RE-PLACEMENT FAILED for %s during "
                            "session hours (held=%.4f, covered=%.4f) — this is "
                            "case (b) and it alerts.", symbol, qty, covered,
                        )
                    gaps.append(gap)
                    continue
                # ---- case (c) and every pre-existing condition ----
                # The whole-share GTC leg is missing or short. Never
                # suppressed, never reclassified, market hours irrelevant:
                # that leg is the durable protection and its absence is the
                # state that ends a desk.
                if coverage == "none":
                    logger.critical(
                        "NO STOP AT ALL: %s held=%.4f with ZERO open "
                        "protective %s-stops — the position is COMPLETELY "
                        "unprotected and has no WAL recovery row.",
                        symbol, qty, "buy" if is_short else "sell",
                    )
                else:
                    logger.warning(
                        "STOP MIS-SIZED: %s held=%.4f but only %.4f covered by "
                        "open protective %s-stops — partially unprotected with "
                        "no WAL recovery row.", symbol, qty, covered,
                        "buy" if is_short else "sell",
                    )
                gap["repaired"] = self._repair_stop_coverage(
                    symbol, held - covered, is_short=is_short, outcome=gap,
                    resting_stops=list(specs or []),
                )
                gaps.append(gap)
        if (longs_checked or shorts_checked) and not gaps:
            logger.info(
                "Stop-coverage reconcile: all %d long / %d short position(s) "
                "adequately stop-covered", longs_checked, shorts_checked,
            )
        # Spec §11.1 hybrid fractional stops, observability half. Total the
        # deliberate overnight exposure into ONE line the owner can read at a
        # glance. The individual gap dicts carry it too (the notifier renders
        # them), but a running total is what turns "a bounded remainder" from
        # a promise into a measurement.
        overnight = [
            g for g in gaps if g.get("coverage") == "fractional_overnight"
        ]
        if overnight:
            total_value = sum(
                float(g.get("unprotected_value") or 0) for g in overnight
            )
            logger.warning(
                "OVERNIGHT FRACTIONAL EXPOSURE: %d position(s) carrying a "
                "sub-share remainder with no live stop until the next session "
                "— $%.2f total at risk. Expected and bounded by design; the "
                "whole-share part of each is still covered by its GTC stop.",
                len(overnight), total_value,
            )
        # Spec §11.1 guard 3, escalation half. A gap the auto-repair CLOSED
        # needs no interruption — the belt did its job. A position still
        # carrying NO stop at all after the repair attempt is a live naked
        # position, and the sweep runs on a 30-minute cadence whose
        # `intra_check` message is silent unless it liquidates: without this,
        # the worst state this reconciler can find would be reported only in
        # a log file. Mis-sized gaps stay in the session banner rather than
        # interrupting the owner — they are real but bounded, and alerting on
        # both is how a channel gets tuned out.
        #
        # Spec §11.1 hybrid fractional stops: the `coverage == "none"` test is
        # exactly the right filter and needs no exception added to it. An
        # expected overnight lapse is stamped 'fractional_overnight' and a
        # re-placed one 'fractional_replaced', so neither can reach this list
        # — while a sub-share position that could NOT be re-covered during
        # SESSION hours falls back to 'none' above and escalates here, which
        # is precisely case (b). The suppression lives in one classifier, not
        # in a growing list of special cases at the escalation site.
        naked = [
            g for g in gaps
            if g.get("coverage") == "none" and not g.get("repaired")
        ]
        if elected_unfilled:
            self._alert_owner_elected_unfilled(elected_unfilled)
        if naked:
            self._alert_owner_no_stop(naked)
        # A session-hours re-placement that did not land is its own
        # escalation, separate from the naked list above: the whole-share
        # GTC leg is usually still standing watch, so the gap classifies as
        # 'partial' and would otherwise be a banner line the owner reads
        # hours later, if at all. Suppression is shared with the standalone
        # watchdog so whichever process sees it first is the one that tells
        # him, and neither repeats the other.
        session_failures = [
            g for g in gaps
            if g.get("session_repair_failed") and not g.get("repaired")
            # A sub-share failure with zero coverage left already went out
            # as NO STOP AT ALL above; one condition, one message.
            and g not in naked
        ]
        if session_failures:
            self._alert_owner_session_repair_failed(session_failures)
        try:
            from src.execution.stop_records import (
                reconcile_recorded_stop_levels, report_stop_level_mismatches,
                write_back_live_protective_stops,
            )
            mismatches = reconcile_recorded_stop_levels(
                broker=self.broker,
                last_buy=lambda sym, action="BUY": self.db.get_symbol_last_buy(
                    sym, include_in_flight=True, action=action,
                ),
                positions=positions,
                sweep_symbol=sweep_symbol,
                skip_symbols=pending_syms,
            )
            mismatches = write_back_live_protective_stops(self.db, mismatches)
            report_stop_level_mismatches(mismatches)
        except Exception as exc:  # noqa: BLE001
            logger.error("stop-level reconcile failed: %s", exc)
        return gaps

    def _elected_unfilled_stop_row(
        self, position, specs, *, is_short: bool,
    ) -> dict | None:
        """One row per position whose protective stop has FIRED and has not
        FILLED, or None when nothing is in that state. Never raises.

        `specs` is what `snapshot_protective_stops` just returned: open
        protective stop orders at the broker. "Open" is the load-bearing
        word — an order the broker has filled is no longer in that list, so
        a stop order that is still listed has not filled. Comparing the
        live price against its trigger therefore answers the whole question:
        price through the trigger + order still open = elected and unfilled.

        This is the state `STOP_LIMIT_BUFFER_PCT`'s own comment describes
        ("on gaps beyond 3% the limit won't fill and the position stays open
        until a session can act") and which nothing could previously see.

        DETECTS ONLY. Nothing here sells, cancels, replaces or re-prices
        anything — an exit decision on an unfilled stop is an owner-level
        change and is not made here.
        """
        try:
            symbol = str(getattr(position, "symbol", "") or "").strip().upper()
            price = float(getattr(position, "current_price", 0) or 0)
        except (TypeError, ValueError):
            return None
        if not symbol or not (math.isfinite(price) and price > 0):
            return None
        through: list[dict] = []
        for spec in specs or []:
            try:
                stop_price = float(spec.get("stop_price", 0) or 0)
                stop_qty = float(spec.get("qty", 0) or 0)
            except (TypeError, ValueError):
                continue
            if stop_qty <= 0:
                continue
            if _price_is_through_stop(price, stop_price, is_short=is_short):
                through.append({"stop_price": stop_price, "qty": stop_qty})
        if not through:
            return None
        # The trigger the tape is FURTHEST past: for a long that is the
        # highest elected stop, for a short the lowest. Derived from the
        # orders themselves, not chosen.
        worst = (min if is_short else max)(
            through, key=lambda r: r["stop_price"],
        )
        stop_price = float(worst["stop_price"])
        distance = (price - stop_price) if is_short else (stop_price - price)
        stranded = sum(float(r["qty"]) for r in through)
        logger.critical(
            "PROTECTIVE STOP ELECTED AND UNFILLED: %s %s at $%.2f is $%.2f "
            "through its $%.2f protective stop, whose order is still OPEN at "
            "the broker over %.4f share(s) — the stop fired and did not "
            "fill, so the coverage sweep counts those shares as protected "
            "while nothing is standing watch. Detected only; nothing was "
            "sold, cancelled or replaced.",
            "short" if is_short else "long", symbol, price, distance,
            stop_price, stranded,
        )
        return {
            "symbol": symbol,
            "held_qty": float(getattr(position, "qty", 0) or 0),
            "price": price,
            "stop": stop_price,
            "through": distance,
            "stranded_qty": stranded,
            "is_short": is_short,
            "unprotected_value": _position_notional(position, stranded),
            # Rendered by the shared coverage bullet as the trailing plain
            # sentence. Says the two numbers and nothing else.
            "note": (
                f"the protective order at ${stop_price:,.2f} fired and did "
                f"not fill \u2014 price ${price:,.2f} is ${distance:,.2f} past it, "
                "so those shares have nothing standing watch over them"
            ),
        }

    @staticmethod
    def _alert_owner_elected_unfilled(rows: list[dict]) -> None:
        """Tell the owner a protective stop FIRED and did NOT fill. Never
        raises.

        A DIFFERENT condition from the one PR #514 alerts on, and it has to
        stay different: that one is a stop the desk could not PLACE, this
        one is a stop that exists, is correctly sized, and did not execute.
        It therefore takes its own per-position per-day claim in the same
        `data/alerting/coverage_heartbeat.json` state file rather than
        borrowing the repair-failure key — sharing the key would let either
        condition silence the other on the same name, which is the opposite
        of not double-alerting.

        The bullet is `src.trader_feed.format_coverage_gap_line`, the same
        wording the session feed and the placement-failure alert use, so one
        position cannot be described three ways.
        """
        try:
            from src import notifier as _notifier
            from src.coverage_watchdog import claim_elected_unfilled_alert
            from src.trader_feed import _profiles, format_coverage_gap_line

            symbols = [
                str(r.get("symbol")).strip() for r in rows
                if str(r.get("symbol") or "").strip()
            ]
            fresh = set(claim_elected_unfilled_alert(symbols))
            if not fresh:
                logger.info(
                    "Elected-but-unfilled protective stop on %s already "
                    "reported to the owner today \u2014 not paging again.",
                    ", ".join(symbols) or "(unnamed)",
                )
                return
            send = [
                r for r in rows
                if str(r.get("symbol") or "").strip().upper() in fresh
            ]
            try:
                profiles = _profiles(send)
            except Exception:  # noqa: BLE001
                profiles = None
            detail = "\n".join(
                format_coverage_gap_line(row, profiles) for row in send
            )
            _notifier.send_owner_alert(
                "\U0001f534 A PROTECTIVE STOP FIRED AND DID NOT FILL\n"
                f"{len(send)} position(s) have traded past their protective "
                "stop while that stop's order is still sitting unfilled at "
                "the broker. Those shares have nothing standing watch over "
                "them right now, even though a stop still shows as live. "
                "This is not a missing stop and not the expected overnight "
                "lapse on a part-share.\n"
                f"{detail}\n"
                "Nothing was sold, cancelled or replaced. Sell by hand, or "
                "move the stop, if you want out of those shares. Each "
                "position is reported at most once per trading day.",
                symbols=sorted(fresh),
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("elected-but-unfilled stop owner alert failed: %s", exc)

    def _still_uncovered(self, gap: dict) -> bool:
        """Is this position STILL short of stop coverage, read fresh from
        the broker? Unreadable answers True — an unprotected position is
        the one thing this desk cannot go quiet about on a bad read.
        """
        symbol = str(gap.get("symbol") or "").strip()
        if not symbol:
            return True
        try:
            held = abs(float(gap.get("held_qty") or 0))
            _ok, specs = self.broker.snapshot_protective_stops(
                symbol, side=("buy" if gap.get("is_short") else "sell"),
            )
            covered = sum(float(s.get("qty", 0) or 0) for s in (specs or []))
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "could not re-read stops for %s before alerting (%s) — "
                "alerting anyway", symbol, exc,
            )
            return True
        if covered + 1e-6 >= held:
            logger.info(
                "%s is fully stop-covered (%.4f of %.4f) by the time the "
                "alert was about to go out — another process placed it. Not "
                "paging the owner about a failure that succeeded.",
                symbol, covered, held,
            )
            return False
        return True

    def _alert_owner_session_repair_failed(self, failures: list[dict]) -> None:
        """Tell the owner a protective stop could not be put back while the
        market was OPEN. Never raises.

        Deliberately NOT the overnight fractional lapse. That one is owner-
        ratified, bounded and happens to every fractional position every
        night — the broker accepts fractional orders only on DAY
        time-in-force, so the sub-share stop dies at 16:00 by design. It is
        stamped 'fractional_overnight' well before here, is reported as a
        measured number rather than an interruption, and must stay silent: a
        quiet expected state that starts paging is how a channel gets tuned
        out.

        The wording is `src.trader_feed.format_coverage_gap_line` — the same
        bullet the session feed renders — so the alert and the feed cannot
        describe one position two ways.

        The broker is re-read for each failing position immediately before
        sending, and one that turns out to be covered after all is dropped.
        Two processes have already been seen running this same repair
        concurrently (2026-09-16, BRK-B: orders 23 ms apart) and the loser's
        retry loop reported FAILURE on an order that had in fact landed.
        Paging the owner about a failure that succeeded is its own defect,
        and the standalone watchdog already re-reads before it ACTS for the
        same reason. Same epsilon, no new threshold, no retry.
        """
        try:
            from src import notifier as _notifier
            from src.coverage_watchdog import claim_repair_failure_alert
            from src.trader_feed import _profiles, format_coverage_gap_line

            still_open = [g for g in failures if self._still_uncovered(g)]
            if not still_open:
                return
            symbols = [
                str(g.get("symbol")).strip() for g in still_open
                if str(g.get("symbol") or "").strip()
            ]
            fresh = set(claim_repair_failure_alert(symbols))
            if not fresh:
                logger.info(
                    "Session stop-repair failure on %s already reported to "
                    "the owner today — not paging again.",
                    ", ".join(symbols) or "(unnamed)",
                )
                return
            rows = [
                g for g in still_open
                if str(g.get("symbol") or "").strip().upper() in fresh
            ]
            try:
                profiles = _profiles(rows)
            except Exception:  # noqa: BLE001
                profiles = None
            detail = "\n".join(
                format_coverage_gap_line(row, profiles) for row in rows
            )
            _notifier.send_owner_alert(
                "🔴 COULD NOT PUT THE PROTECTIVE STOP BACK\n"
                f"{len(rows)} position(s) lost part of their protective stop "
                "while the market was OPEN, and the desk tried to place the "
                "missing stop and failed. Those shares have nothing standing "
                "watch over them right now. This is not the expected "
                "overnight lapse on a part-share.\n"
                f"{detail}\n"
                "Nothing was sold, resized or cancelled. Place the missing "
                "stop by hand — a stop over a part-share has to be a "
                "day-only order — or close the position. Each position is "
                "reported at most once per trading day.",
                symbols=sorted(fresh),
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("session stop-repair owner alert failed: %s", exc)

    @staticmethod
    def _alert_owner_no_stop(naked: list[dict]) -> None:
        """Push the NO-STOP-AT-ALL escalation to the owner. Never raises."""
        try:
            from src import notifier as _notifier

            # The refusal REASON, not just the shortfall (docs/WORK.md item
            # 88). "The automatic repair could not restore one" was true of a
            # corrupt recorded stop, a level the tape has already passed and
            # an exhausted broker retry alike — three states with three
            # different owner actions. `repair_refusal` is stamped by
            # `repair_stop_coverage` and omitted when it has nothing to say.
            detail = "\n".join(
                f"  {g.get('symbol', '?')}: held {g.get('held_qty')}, "
                f"covered {g.get('covered_qty')}"
                + (f" — {g['repair_refusal']}" if g.get("repair_refusal") else "")
                for g in naked
            )
            _notifier.send_owner_alert(
                "🔴 NO STOP AT ALL\n"
                f"{len(naked)} position(s) are open at the broker with ZERO "
                "protective-stop coverage, and the automatic repair could not "
                "restore one. This is not a mis-sized stop — there is nothing "
                "standing watch.\n"
                f"{detail}\n"
                "Place a protective stop manually or flatten the position.",
                symbols=[str(g.get("symbol")) for g in naked if g.get("symbol")],
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("no-stop owner alert failed: %s", exc)

    def _wire_protective_stop_block_recorder(self) -> None:
        """The broker holds no database, so a protective stop its kill
        switch refuses is recorded through this pipeline's one
        (`kind='protective_stop_blocked'`, `src/execution/exit_path_records.py`).
        Also pages the owner (`_alert_owner_kill_switch_blocked`) — a
        recorded row nobody reads is not an alert, and until this was
        wired a kill-switch refusal left the position naked with no owner
        notice at all. `self.db` is read at call time, not captured, so a
        later swap of the handle is honoured."""
        from src.execution.exit_path_records import record_protective_stop_blocked

        def _on_blocked(**facts) -> None:
            record_protective_stop_blocked(self.db, **facts)
            self._alert_owner_kill_switch_blocked(**facts)

        self.broker.protective_stop_block_recorder = _on_blocked

    @staticmethod
    def _alert_owner_kill_switch_blocked(
        *, symbol: str, qty: float = 0.0, stop_price: float = 0.0,
        side: str = "", kill_switch_path: str = "", **_ignored,
    ) -> None:
        """Page the owner the first time today the desk's own kill switch
        blocks a protective stop for `symbol`. Never raises — see
        `AlpacaBroker._submit_stop_limit_order`, which already swallows
        whatever this callback does.

        Deduped per symbol per trading day (`claim_kill_switch_block_alert`)
        the same way the repair-failure and elected-unfilled alerts are: a
        kill switch left on all session would otherwise page once per
        retry of every symbol it touches.
        """
        try:
            from src import notifier as _notifier
            from src.coverage_watchdog import claim_kill_switch_block_alert
            from src.execution.exit_path_records import kill_switch_blocked_text

            fresh = claim_kill_switch_block_alert([symbol])
            if not fresh:
                return
            _notifier.send_owner_alert(
                "🔴 KILL SWITCH BLOCKED A PROTECTIVE STOP\n"
                f"{kill_switch_blocked_text(symbol)}\n"
                f"qty={qty} side={side} stop=${stop_price}\n"
                "Nothing was sent to the broker, so nothing is standing "
                "watch over this position right now. Turn off the kill "
                "switch and place the stop by hand, or flatten the "
                "position. Reported at most once per trading day.",
                symbols=fresh,
            )
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "kill-switch-block owner alert failed for %s: %s", symbol, exc,
            )

    def _repair_stop_coverage(
        self, symbol: str, uncovered_qty: float, *, is_short: bool,
        outcome: dict | None = None, resting_stops: list | None = None,
    ) -> bool:
        """Best-effort: re-place protective stop coverage on an uncovered
        position using the stop level recorded on its last opening row
        (BUY for a long, SHORT for a short). Returns True when the gap
        was actually closed.

        `outcome`, when given, is the caller's gap dict: a refusal stamps
        `repair_refusal` on it with a plain sentence saying WHY nothing was
        placed (docs/WORK.md item 88). Before that, the caller received a
        bare False and the owner alert could only say the repair "could not
        restore one" — a corrupt recorded stop, a level already through the
        tape and three exhausted broker retries all read identically.

        THE BODY MOVED to `src.execution.stop_repair.repair_stop_coverage`
        and this is now a delegate — see that module for the whole design,
        including why the recorded opening level is not a policy invention and
        why the fractional split needs no special case here. It moved
        because `src/coverage_watchdog.py` needs the SAME re-placement when
        it finds an uncovered sub-share remainder while the trading timers
        are stopped (docs/INCIDENT_HISTORY.md, item 53), and a second copy
        of an order-placement path is how one behaviour ends up with two
        homes. The caller still owns the decision of WHETHER to call this at
        the current hour.
        """
        from src.execution.stop_repair import repair_stop_coverage

        opening = "SHORT" if is_short else "BUY"
        return repair_stop_coverage(
            broker=self.broker,
            # include_in_flight: a same-session open still at fill_status=
            # 'submitted' is the row whose stop we want — under the strict
            # executed predicate the repair either no-op'd or read a months-
            # old prior row's stop level (audit round 2).
            last_buy=lambda sym, action=opening: self.db.get_symbol_last_buy(
                sym, include_in_flight=True, action=action,
            ),
            symbol=symbol,
            uncovered_qty=uncovered_qty,
            is_short=is_short,
            db=self.db,
            outcome=outcome,
            resting_stops=resting_stops,
            caller="session_coverage_reconcile",
        )

    def _submit_protected_sell(
        self,
        *,
        symbol: str,
        qty: float,
        limit_price: float,
        reference_price: float,
        position_qty_before_sell: float,
        label: str,
        side: str = "sell",
    ) -> tuple[dict, dict] | None:
        """Head half of the SELL/COVER discipline: clear protective stops
        (write-ahead) → submit the order → guarantee stops are restored if
        the order never reaches the broker.

        Returns ``(order, pending_protection)`` on broker acceptance, or
        ``None`` when the symbol must be skipped — stop-clear failed, the
        submit raised, or the broker rejected. In every skip case the
        protective stops are already restored (or were never cancelled), so
        the caller just ``continue``s with no naked-position window.

        The caller owns qty/price selection, ``insert_trade``, the orders list,
        and any accounting (projected_proceeds, sell_order_ids); this owns the
        cancel → submit → accept → restore-on-failure invariant so no SELL path
        can silently skip a step (CLAUDE.md's longest convention). ``label`` is
        both the order's action tag and the log context (e.g. 'EMERGENCY_SELL',
        'FORCE_DELEVER', 'SELL').

        ``position_qty_before_sell`` is the FULL held qty (drives the WAL +
        finalize residual math); ``qty`` is the order quantity (may be a
        partial / reduce / trim). Both are always non-negative magnitudes —
        never the broker's signed position qty — so every comparison and
        every arithmetic step downstream (WAL specs, fill_qty, residual math)
        stays identical in shape whether this is closing a long or covering
        a short.

        ``side`` (forced-close support, added alongside the emergency-
        liquidation short-close gap fix): the CLOSING order's side —
        ``'sell'`` (default, unchanged for every pre-existing caller — none
        of them pass this) flattens a long; ``'buy'`` covers a short. It
        doubles as the STOP order's own side to cancel/restore, because a
        long's protective stop and its closing order are BOTH 'sell', and a
        short's protective stop and its closing order (a BUY-to-cover) are
        BOTH 'buy' — one parameter, not two, so there's no way for the
        closing side and the stop-clearing side to disagree.
        """
        # audit F1 review #1: snapshot → persist WAL → cancel, so the recovery
        # row is durable BEFORE any broker mutation.
        #
        # Full exits also cancel the day's resting entry BUY for the SAME
        # symbol first (audit round 2): a still-working DAY entry limit would
        # silently re-open a position the reviewer/breaker just decided to
        # close — and can trip Alpaca's wash-trade rejection of this SELL.
        # Best-effort + symbol-scoped; partial trims (REDUCE, PARTIAL_SELL,
        # TAKE_PROFIT, SWEEP_SELL) keep their entries — trimming isn't exiting.
        # EMERGENCY_COVER is the short-side twin of EMERGENCY_SELL added
        # here: it runs the same entry-order cancel a long exit does.
        # `cancel_open_entry_orders` (src/execution/broker.py) now cancels
        # a resting entry order on EITHER side — BUY-to-open-long or
        # SELL-to-open-short — so an EMERGENCY_COVER here also stops a
        # still-live SHORT entry from re-opening the position it just
        # covered (previously flagged, fixed alongside the review-path
        # COVER gap).
        if label in ("SELL", "EMERGENCY_SELL", "EMERGENCY_COVER", "FORCE_DELEVER"):
            try:
                self.broker.cancel_open_entry_orders(symbol=symbol)
            except Exception as exc:  # noqa: BLE001
                logger.warning("%s: entry-order cancel failed for %s: %s",
                               label, symbol, exc)
        stop_side_kwargs = {} if side == "sell" else {"side": side}
        ok, stop_specs, wal_row_id = self._cancel_stops_with_write_ahead(
            symbol, position_qty_before_sell, **stop_side_kwargs,
        )
        if not ok:
            logger.warning(
                "%s: skipping %s — protective-stop clear failed (broker would "
                "reject the %s on held_for_orders)", label, symbol, side.upper(),
            )
            return None
        try:
            order = self.broker.submit_order(
                symbol=symbol, qty=qty, side=side,
                limit_price=limit_price, reference_price=reference_price,
            )
        except Exception as exc:  # noqa: BLE001
            # Submit raised → the position is intact but its stops are
            # cancelled. Restore them in-session rather than waiting for the
            # next drain (this used to vary by site — only the since-deleted
            # auto take-profit restored; the others rode naked until drain).
            logger.error("%s: submit failed for %s: %s", label, symbol, exc)
            if stop_specs:
                self.broker._restore_stop_orders(
                    symbol, stop_specs, check_idempotency=False, **stop_side_kwargs,
                )
            return None
        if not self._order_accepted(order, symbol, side):
            # Broker rejected — restore the stops we just cancelled.
            if stop_specs:
                self.broker._restore_stop_orders(
                    symbol, stop_specs, check_idempotency=False, **stop_side_kwargs,
                )
            return None
        # audit F5: tag the order dict so the notifier's intervention banner +
        # inline action labels fire (broker.submit_order returns no 'action').
        if isinstance(order, dict):
            order.setdefault("action", label)
        # Defer the reprotect/restore decision to finalize (after the wait) —
        # an accepted limit can still cancel/expire without filling, in which
        # case the FULL original protection is what the position needs.
        prot = {
            "order_id": order["id"], "symbol": symbol,
            "position_qty_before_sell": position_qty_before_sell,
            "specs": stop_specs, "wal_row_id": wal_row_id, "side": side,
        }
        return order, prot

    def _finalize_pending_protections(
        self,
        pending_protections: list[dict],
        *,
        context: str,
        wait: bool = True,
    ) -> None:
        """Tail half of the SELL discipline: drain a batch of stashed
        protection-restore intents after a round of SELLs.

        For each stashed ``{order_id, symbol, position_qty_before_sell, specs,
        wal_row_id}``: (optionally) block until the SELL reaches terminal,
        finalize stop coverage on the ACTUAL fill (reprotect residual / restore
        originals / no-op on full exit), and log when coverage couldn't be
        rebuilt (the WAL row drives a retry next session).

        Previously copy-pasted near-verbatim at 6 call sites — that duplication
        is exactly how a step once went missing (ExecutionStage lacked the wait
        try/except until an audit caught it). Centralizing makes the discipline
        one tested path.

        ``wait=False`` for callers (ExecutionStage) that already waited for
        terminal in an earlier loop — the orders are terminal, so re-waiting
        would be a redundant no-op; skipping it preserves their prior behavior.
        ``context`` is the human-readable log prefix (e.g. 'FORCE DE-LEVER').
        """
        for prot in pending_protections:
            if wait:
                try:
                    # Kept on the intent so a caller can record the outcome
                    # (the gross-exposure de-lever's shortfall row, item 112).
                    prot["terminal_status"] = self.broker.wait_for_order_terminal(
                        prot["order_id"],
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "%s: wait failed for %s order %s: %s — finalize will "
                        "use whatever fill_info reads now",
                        context, prot["symbol"], prot["order_id"], exc,
                    )
            finalize_side = prot.get("side")
            side_kwargs = {} if not finalize_side or finalize_side == "sell" else {"side": finalize_side}
            ok, _retry_specs = self._finalize_protection_after_sell(
                prot["order_id"], prot["symbol"],
                prot["position_qty_before_sell"], prot["specs"],
                wal_row_id=prot.get("wal_row_id"), **side_kwargs,
            )
            prot["coverage_confirmed"] = bool(ok)
            if not ok:
                logger.warning(
                    "%s: finalize for %s (order %s) did not confirm stop "
                    "coverage — recovery intent persisted; drain rebuilds "
                    "next session",
                    context, prot["symbol"], prot["order_id"],
                )

    def _finalize_protection_after_sell(
        self,
        order_id: str,
        symbol: str,
        position_qty_before_sell: float,
        cancelled_specs: list[dict],
        *,
        from_drain: bool = False,
        wal_row_id: int | None = None,
        side: str = "sell",
    ) -> tuple[bool, list[dict]]:
        """Thin wrapper over the finalize core (audit F1 WAL lifecycle).

        ``wal_row_id`` is the pending_protection_restores row written
        BEFORE cancel_protective_stops (write-ahead). The core's bail
        branches UPDATE that row instead of INSERTing a duplicate; here,
        once the core confirms coverage is good (ok=True), the
        write-ahead row is deleted — the recovery intent is discharged.
        ``from_drain`` rows manage their own lifecycle, so the wrapper
        never deletes for them. Backward compatible: callers/tests that
        omit wal_row_id get exactly the pre-F1 behaviour.

        ``side`` — see ``_submit_protected_sell``: 'sell' (default) for a
        long, 'buy' for a short's cover. Passed straight through to the
        core.
        """
        ok, retry_specs = self._finalize_protection_after_sell_core(
            order_id, symbol, position_qty_before_sell, cancelled_specs,
            from_drain=from_drain, wal_row_id=wal_row_id, side=side,
        )
        if ok and wal_row_id is not None and not from_drain:
            try:
                self.db.delete_pending_protection_restore(wal_row_id)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "WAL: failed to clear discharged protection-restore "
                    "row %d for %s: %s (drain will no-op it next session)",
                    wal_row_id, symbol, exc,
                )
        return ok, retry_specs

    def _finalize_protection_after_sell_core(
        self,
        order_id: str,
        symbol: str,
        position_qty_before_sell: float,
        cancelled_specs: list[dict],
        *,
        from_drain: bool = False,
        wal_row_id: int | None = None,
        side: str = "sell",
    ) -> tuple[bool, list[dict]]:
        """Decide stop coverage based on the actual SELL fill outcome,
        not on submit acceptance.

        ``side`` — 'sell' (default, unchanged) for a long being sold; 'buy'
        for a short being covered. ``position_qty_before_sell`` and every
        qty this function reads back from the broker
        (``_current_position_qty_for_finalize``) are ALWAYS treated as
        non-negative magnitudes here (the broker's own signed qty is
        abs()'d on read) — a short's -73 shares and a long's 73 shares
        drive identical arithmetic; only ``side`` decides which stop side
        gets cancelled/restored/re-placed.

        Submit-acceptance is too early — Alpaca can accept a LIMIT and
        then have it expire / cancel / get rejected later in the session
        without ever filling. If we reprotected on the residual qty at
        accept-time and the SELL doesn't fill, the to-be-sold portion
        rides naked for the rest of the day. PR I (#55) had this gap.

        Reads broker.get_order_fill_info() AFTER wait_for_order_terminal
        has returned, so the fill_qty is final:

        1. ``fill_qty == 0`` (cancelled/expired/rejected after acceptance):
           the position is unchanged but we cancelled the protective
           stops. Restore the original specs covering the full position.
        2. ``0 < fill_qty < position_qty``: protect the actual residual
           ``position_qty_before_sell - fill_qty`` at the most-protective
           cancelled stop_price.
        3. ``fill_qty == position_qty``: full exit, nothing to protect.

        Special case: if get_order_fill_info reports a NON-terminal status
        (the SELL is still 'new' / 'accepted' / 'pending_new' because
        wait_for_order_terminal hit its 15s ceiling without the order
        reaching terminal), finalizing now would race with the broker —
        restoring stops while the SELL is open triggers held_for_orders
        rejection on the new stop submit. Force terminal state by
        cancelling the lingering SELL, then re-read fill_info and
        proceed normally. Codex r5 caught this exact gap.

        Returns ``(success, retry_specs)``:
          - success: True iff coverage is in a known-good state (specs
            were successfully restored / residual was reprotected /
            no residual existed / there were no specs at all). False on
            any bail or restore/reprotect failure.
          - retry_specs: when success=False, the subset of cancelled_specs
            that still need a protection retry. For partial-restore this
            is ONLY the failed specs (the ones that landed are already
            alive at the broker). For other failure modes it's the full
            cancelled_specs list. Empty when success=True.

        ``from_drain=True`` skips the persist-on-bail step (drain
        already has a row). Drain uses retry_specs to NARROW the
        existing row to just what still needs retry — avoids the next
        drain re-submitting a stop that already landed (codex r10 #1).

        No-op when there were no specs to begin with — a position that
        had no protective stop pre-SELL has nothing to restore.
        """
        if not cancelled_specs:
            return True, []

        # Built once, reused at every broker call below that's keyed on the
        # STOP side — omitted entirely for the (default, pre-existing) long
        # case so every downstream call is byte-identical to before shorts.
        side_kwargs = {} if side == "sell" else {"side": side}
        order_word = "BUY" if side == "buy" else "SELL"

        fill_info = self.broker.get_order_fill_info(order_id) or {}
        status = (fill_info.get("status") or "").lower()

        if status not in self._TERMINAL_ORDER_STATUSES:
            # The wait window expired with the order still live. We
            # cannot leave this state — restoring or reprotecting now
            # races with the broker. Cancel the lingering SELL so
            # status converges to terminal.
            logger.warning(
                "%s on %s did not reach terminal in wait window "
                "(status=%s) — cancelling so protection state can settle",
                order_word, symbol, status or "?",
            )
            try:
                self.broker.client.cancel_order_by_id(order_id)
                # Cancel propagates fast; a tighter 5s wait is enough.
                self.broker.wait_for_order_terminal(order_id, timeout_seconds=5.0)
            except Exception as exc:
                logger.warning(
                    "Failed to cancel lingering %s on %s (order %s): %s "
                    "— persisting orphaned restore intent for next session.",
                    order_word, symbol, order_id, exc,
                )
                if not from_drain:
                    self._persist_orphaned_protection_restore(
                        order_id, symbol, position_qty_before_sell, cancelled_specs,
                        wal_row_id=wal_row_id,
                        side=side,
                    )
                return False, list(cancelled_specs)
            # Re-read post-cancel — broker may report partial fill that
            # landed during cancel propagation.
            fill_info = self.broker.get_order_fill_info(order_id) or {}
            status = (fill_info.get("status") or "").lower()
            logger.info(
                "Cancelled lingering %s on %s — post-cancel status=%s, "
                "filled_qty=%s",
                order_word, symbol, status, fill_info.get("filled_qty"),
            )
            # Cancel propagation can take longer than the 5s wait window,
            # especially during halts or illiquid conditions. If status
            # is still non-terminal, persist the restore intent and bail
            # — next session's drain pass picks it up. Without persistence
            # the previous bail was a slow leak: the warning promised
            # "next session reconcile rebuilds coverage" but
            # _reconcile_fills only updates fill columns. Codex r7 #3.
            if status not in self._TERMINAL_ORDER_STATUSES:
                logger.warning(
                    "Cancel of lingering %s on %s did not converge to "
                    "terminal within 5s (post-cancel status=%s) — "
                    "persisting orphaned restore intent for next session.",
                    order_word, symbol, status or "?",
                )
                if not from_drain:
                    self._persist_orphaned_protection_restore(
                        order_id, symbol, position_qty_before_sell, cancelled_specs,
                        wal_row_id=wal_row_id,
                        side=side,
                    )
                return False, list(cancelled_specs)

        fill_qty_raw = fill_info.get("filled_qty")
        try:
            fill_qty = float(fill_qty_raw) if fill_qty_raw is not None else 0.0
        except (TypeError, ValueError):
            fill_qty = 0.0

        if fill_qty <= 0:
            # Concurrent-SELL guard: a parallel intra_check EMERGENCY_SELL
            # (exempt from cross-mode lock) may have reduced or zeroed
            # position while this SELL sat unfilled. If broker now shows
            # 0 shares we'd be restoring stops on a phantom position;
            # broker rejects → finalize bails → drain replays same math →
            # row stuck forever. Re-read position and skip / clip
            # accordingly.
            current_qty_raw = self._current_position_qty_for_finalize(symbol)
            # Broker reports the SIGNED position (negative for a short);
            # every comparison below is magnitude-only, so normalize once
            # here rather than abs()-ing at each use.
            current_qty = current_qty_raw if current_qty_raw is None else abs(current_qty_raw)
            if current_qty == 0:
                logger.info(
                    "%s on %s had no fill, but broker reports position=0 "
                    "— concurrent path fully exited; skipping restore",
                    order_word, symbol,
                )
                return True, []
            if current_qty is not None:
                total_spec_qty = sum(float(s.get("qty", 0) or 0) for s in cancelled_specs)
                if current_qty + 1e-6 < total_spec_qty:
                    # Concurrent SELL reduced position below original
                    # stop coverage. Restoring all specs would over-protect
                    # → broker rejects. Collapse to a single reprotect at
                    # the most-protective stop_price for the actual qty.
                    logger.warning(
                        "%s on %s had no fill, but broker position=%.4f "
                        "< original spec qty=%.4f — concurrent path reduced "
                        "position; collapsing restore to single reprotect",
                        order_word, symbol, current_qty, total_spec_qty,
                    )
                    if not self._reprotect_residual_after_partial_sell(
                        symbol, current_qty, cancelled_specs, **side_kwargs,
                    ):
                        if not from_drain:
                            self._persist_orphaned_protection_restore(
                                order_id, symbol, current_qty, cancelled_specs,
                                wal_row_id=wal_row_id,
                                side=side,
                            )
                        return False, list(cancelled_specs)
                    return True, []
            try:
                # Drain replays may re-encounter specs that landed in a
                # prior pass; check_idempotency=from_drain prevents the
                # re-submit dupes that broke down on held_for_orders
                # before the audit fix.
                restored, failed_specs = self.broker._restore_stop_orders(
                    symbol, cancelled_specs, check_idempotency=from_drain, **side_kwargs,
                )
                logger.info(
                    "%s on %s terminated with no fill (status=%s) — "
                    "restored %d/%d original protective stop(s)",
                    order_word, symbol, status or "?", restored, len(cancelled_specs),
                )
            except Exception as exc:
                logger.warning(
                    "Failed to restore stops for %s after no-fill %s: %s — "
                    "persisting recovery intent",
                    symbol, order_word, exc,
                )
                if not from_drain:
                    self._persist_orphaned_protection_restore(
                        order_id, symbol, position_qty_before_sell, cancelled_specs,
                        wal_row_id=wal_row_id,
                        side=side,
                    )
                return False, list(cancelled_specs)
            # PARTIAL restore is incomplete coverage — restoring 1 of 2
            # original stops still leaves the slice covered by the failed
            # spec naked. Codex r9: previously we only flagged 0 of N as
            # failure; now any partial-restore persists ONLY the failed
            # specs (not the originals — the ones that DID restore are
            # already alive at the broker, retrying would double-stack).
            if failed_specs:
                logger.warning(
                    "Restore for %s submitted %d/%d stops — %d failed; "
                    "persisting failed spec(s) for retry",
                    symbol, restored, len(cancelled_specs), len(failed_specs),
                )
                if not from_drain:
                    self._persist_orphaned_protection_restore(
                        order_id, symbol, position_qty_before_sell, failed_specs,
                        wal_row_id=wal_row_id,
                        side=side,
                    )
                return False, list(failed_specs)
            return True, []

        computed_residual = position_qty_before_sell - fill_qty
        # Concurrent-SELL guard: same reasoning as the fill_qty<=0 branch.
        # cached `position_qty_before_sell - fill_qty` can over-state
        # residual if intra_check liquidated some shares while this SELL
        # was in flight. Clip to actual broker position. Magnitude-only,
        # same normalization as the fill_qty<=0 branch above.
        current_qty_raw = self._current_position_qty_for_finalize(symbol)
        current_qty = current_qty_raw if current_qty_raw is None else abs(current_qty_raw)
        if current_qty == 0:
            logger.info(
                "Finalize for %s: cached residual=%.4f but broker shows "
                "position=0 — concurrent path fully exited; skipping reprotect",
                symbol, computed_residual,
            )
            return True, []
        if current_qty is not None and current_qty + 1e-6 < computed_residual:
            logger.warning(
                "Finalize for %s: clipping residual from %.4f to %.4f "
                "(broker position decreased — concurrent SELL took shares)",
                symbol, computed_residual, current_qty,
            )
            actual_residual = current_qty
        else:
            actual_residual = computed_residual
        if actual_residual <= 0:
            return True, []  # full exit — no residual to re-protect

        if not self._reprotect_residual_after_partial_sell(
            symbol, actual_residual, cancelled_specs, **side_kwargs,
        ):
            # Reprotect submit raised. Persist so a later session can retry.
            # Codex r9 #1: previously this just returned False without
            # persisting, and the SELL-path callers ignored that bool —
            # the recovery intent was silently lost.
            #
            # Persist the PRE-sell qty, not `actual_residual` (2026-07-16
            # audit): the drain replays this row through the same finalize
            # core, which recomputes `position_qty_before_sell - fill_qty`
            # from the SAME order. Passing the post-sell residual made the
            # replay subtract the fill twice — for a SELL that filled exactly
            # what it asked for, the recomputed residual hit 0, took the
            # "full exit — nothing to re-protect" early return, reported
            # success, and DELETED the row. Net effect: the residual position
            # stayed naked forever and the recovery intent was destroyed.
            # The drain's downward clip against the live broker position keeps
            # this correct even if a concurrent SELL took shares meanwhile.
            if not from_drain:
                self._persist_orphaned_protection_restore(
                    order_id, symbol, position_qty_before_sell, cancelled_specs,
                    wal_row_id=wal_row_id,
                    side=side,
                )
            return False, list(cancelled_specs)
        return True, []

    def _write_ahead_protection_restore(
        self,
        symbol: str,
        position_qty_before_sell: float,
        specs: list[dict],
        *,
        side: str = "sell",
    ) -> int | None:
        """audit F1: persist the protection-restore intent.

        The recovery-intent persist used to live only inside finalize's
        bail branches, which run AFTER the whole cancel -> submit ->
        wait -> finalize loop. A SIGKILL / reboot / `timeout
        --kill-after` anywhere in that window left the broker with no
        stop and the DB with no recovery row — the position rode naked
        indefinitely (the in-process try/except does NOT survive a
        process kill).

        Called by _cancel_stops_with_write_ahead AFTER snapshotting the
        stops but BEFORE cancelling them (audit F1 review #1), so the
        sentinel row is durable before any broker mutation — there is no
        "stops cancelled but nothing recorded" window. The row is
        flipped to the real order id by finalize's bail and deleted once
        finalize confirms coverage. Returns the row id (to thread
        through), or None when there was nothing to protect or the DB
        write failed (no worse than the pre-F1 behaviour — logged).

        ``side`` (Stage 3, shorts) — the closing order's side, passed
        straight through to ``insert_pending_protection_restore``: 'sell'
        (default) for a long being sold, 'buy' for a short being covered.
        This is the REAL side, known here at write time — recorded so the
        drain path (``_drain_pending_protection_restores``) doesn't have
        to guess it back from live broker state later.
        """
        if not specs:
            return None
        try:
            row_id = self.db.insert_pending_protection_restore(
                symbol=symbol,
                sell_order_id=_WAL_SELL_SENTINEL,
                position_qty_before_sell=position_qty_before_sell,
                specs_json=_json.dumps(specs),
                side=side,
            )
            logger.info(
                "WAL: wrote protection-restore intent for %s (row %d, "
                "%d stop(s)) before cancel/submit", symbol, row_id,
                len(specs),
            )
            return row_id
        except Exception as exc:
            logger.error(
                "WAL: failed to write protection-restore intent for %s: "
                "%s — proceeding without crash-safety for this SELL "
                "(no worse than pre-F1)", symbol, exc,
            )
            return None

    def _cancel_stops_with_write_ahead(
        self, symbol: str, position_qty_before_sell: float,
        *, side: str = "sell",
    ) -> tuple[bool, list[dict], int | None]:
        """Snapshot protective stops -> persist WAL recovery intent ->
        THEN cancel the stops. audit F1 review #1: true write-ahead.

        The previous F1 fix wrote the WAL row AFTER
        cancel_protective_stops, which had already cancelled the stops
        at the broker — a kill inside / just after that call left a
        naked position with no recovery intent. Ordering snapshot →
        persist → cancel guarantees the recovery row is durable BEFORE
        any broker mutation. A kill before the cancel is harmless (stops
        still live; drain's sentinel path re-reads the position and the
        idempotent restore is a no-op). A kill during/after the cancel
        is recoverable from the row.

        ``side`` is the STOP order's own side — 'sell' (default, byte-
        identical to every call site before shorts existed) snapshots the
        SELL stops protecting a long; 'buy' snapshots the BUY stops
        protecting a short. Passed through unchanged to
        ``snapshot_protective_stops``.

        Returns ``(ok, specs, wal_row_id)``. ``ok=False`` ⇒ skip the
        SELL: either the snapshot failed, or the cancel failed and was
        rolled back (position still protected, SELL would be rejected on
        held_for_orders anyway). When there were no stops to begin with,
        returns ``(True, [], None)`` — nothing to protect, SELL proceeds.
        """
        snapshot_kwargs = {} if side == "sell" else {"side": side}
        ok, specs = self.broker.snapshot_protective_stops(symbol, **snapshot_kwargs)
        if not ok:
            return False, [], None
        if not specs:
            return True, [], None
        wal_row_id = self._write_ahead_protection_restore(
            symbol, position_qty_before_sell, specs, side=side,
        )
        if not self.broker.cancel_snapshotted_stops(symbol, specs):
            # Stops NOT cleared (rolled back by cancel_snapshotted_stops).
            # The position is still protected and the SELL would be
            # rejected on held_for_orders — discharge the row we just
            # pre-wrote so the next drain doesn't redundantly "restore"
            # stops that never actually left the broker.
            if wal_row_id is not None:
                try:
                    self.db.delete_pending_protection_restore(wal_row_id)
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "WAL: failed to discharge row %d after cancel "
                        "rollback for %s: %s (drain will idempotently "
                        "no-op it)", wal_row_id, symbol, exc,
                    )
            return False, [], None
        return True, specs, wal_row_id

    def _restore_after_unconfirmed_sell(
        self,
        symbol: str,
        position_qty_before_sell: float,
        cancelled_specs: list[dict],
        *,
        side: str = "sell",
    ) -> tuple[bool, list[dict]]:
        """drain handler for a write-ahead row whose SELL was never
        confirmed (sentinel sell_order_id) — a crash between
        cancel_protective_stops() and recording the SELL.

        There is no SELL order to query, and the broker may or may not
        have received/filled a SELL (crash could land before submit, or
        after submit but before we stored the id). The only trustworthy
        signal is the broker's CURRENT position. Conservative:
          - position 0  → SELL filled / position gone → nothing to
            protect (success).
          - position unknown → don't guess; leave the row.
          - position < original spec coverage → collapse to one
            most-protective stop on the actual shares.
          - position intact → restore the original specs idempotently
            (a prior inline reject-restore or partial drain may have
            already replaced some).
        Returns (ok, retry_specs) like the finalize core.

        ``side`` — 'sell' (default) for a long, 'buy' for a short's cover;
        see ``_submit_protected_sell``. The caller (the drain loop, via
        ``_resolve_wal_row_side``) prefers this row's own persisted `side`
        column (Stage 3) and only derives it from LIVE broker position
        sign as a fallback for a row written before that column existed.
        """
        if not cancelled_specs:
            return True, []
        side_kwargs = {} if side == "sell" else {"side": side}
        current_raw = self._current_position_qty_for_finalize(symbol)
        # Magnitude-only from here — broker reports the SIGNED qty
        # (negative for a short); `side` (not the sign) drives which stop
        # side gets touched.
        current = current_raw if current_raw is None else abs(current_raw)
        if current == 0:
            logger.info(
                "WAL drain: %s now flat — SELL must have filled / position "
                "gone; no protection to restore", symbol,
            )
            return True, []
        if current is None:
            logger.warning(
                "WAL drain: %s position unknown (broker error) — leaving "
                "row for next session", symbol,
            )
            return False, list(cancelled_specs)
        total_spec_qty = sum(
            float(s.get("qty", 0) or 0) for s in cancelled_specs
        )
        if current + 1e-6 < total_spec_qty:
            logger.warning(
                "WAL drain: %s position=%.4f < original spec qty=%.4f "
                "(SELL partially filled before crash) — collapsing to a "
                "single most-protective stop", symbol, current, total_spec_qty,
            )
            if not self._reprotect_residual_after_partial_sell(
                symbol, current, cancelled_specs, **side_kwargs,
            ):
                return False, list(cancelled_specs)
            return True, []
        try:
            restored, failed = self.broker._restore_stop_orders(
                symbol, cancelled_specs, check_idempotency=True, **side_kwargs,
            )
        except Exception as exc:
            logger.warning(
                "WAL drain: restore raised for %s: %s — leaving row",
                symbol, exc,
            )
            return False, list(cancelled_specs)
        if failed:
            logger.warning(
                "WAL drain: %s restored %d/%d stop(s) — %d still failing",
                symbol, restored, len(cancelled_specs), len(failed),
            )
            return False, list(failed)
        logger.info(
            "WAL drain: %s restored %d original protective stop(s)",
            symbol, restored,
        )
        return True, []

    def _persist_orphaned_protection_restore(
        self,
        order_id: str,
        symbol: str,
        position_qty_before_sell: float,
        cancelled_specs: list[dict],
        *,
        wal_row_id: int | None = None,
        side: str = "sell",
    ) -> None:
        """Persist (or update) a protection-restore recovery intent.

        Used by the bail branches of the finalize core: cancel raised,
        OR cancel was accepted but didn't converge to terminal in 5s, OR
        a restore/reprotect failed. The position is sitting with the
        original stops cancelled and a maybe-still-live SELL — neither
        restoring nor reprotecting is safe right now. Record the intent
        and let the next session's drain pass act once broker state
        settles.

        audit F1: when ``wal_row_id`` is set there is already a
        write-ahead row (inserted BEFORE cancel_protective_stops) — flip
        it to the real order id + final specs via UPDATE instead of
        INSERTing a duplicate. Without a wal_row_id (legacy callers /
        tests) it INSERTs as before. Best-effort — DB failure logs but
        never propagates (the immediate path already had no good
        option).

        ``side`` (Stage 3, shorts) — the closing order's side ('sell' for
        a long, 'buy' for a short's cover), passed through to the
        DB layer either way: on UPDATE it re-affirms the value the
        write-ahead row was created with (belt-and-suspenders — the
        write-ahead insert already set it correctly); on INSERT (the
        legacy-caller / no-prior-row path) it's the only place this row
        will ever get a side recorded.
        """
        if not cancelled_specs:
            return
        import json as _json
        specs_json = _json.dumps(cancelled_specs)
        try:
            if wal_row_id is not None:
                self.db.update_pending_protection_restore(
                    wal_row_id,
                    sell_order_id=order_id,
                    position_qty_before_sell=position_qty_before_sell,
                    specs_json=specs_json,
                    side=side,
                )
                logger.info(
                    "WAL: updated protection-restore row %d for %s "
                    "(order %s, %d cancelled stop(s)) — drain retries next "
                    "session", wal_row_id, symbol, order_id,
                    len(cancelled_specs),
                )
            else:
                self.db.insert_pending_protection_restore(
                    symbol=symbol,
                    sell_order_id=order_id,
                    position_qty_before_sell=position_qty_before_sell,
                    specs_json=specs_json,
                    side=side,
                )
                logger.info(
                    "Persisted orphaned protection-restore for %s (order %s, "
                    "%d cancelled stop(s)) — drain pass will retry next session",
                    symbol, order_id, len(cancelled_specs),
                )
        except Exception as exc:
            logger.error(
                "Failed to persist orphaned protection-restore for %s: %s — "
                "position is unprotected with no recovery plan; manual "
                "intervention required",
                symbol, exc,
            )

    def _derive_close_side_for_drain(self, symbol: str) -> str | None:
        """Which stop side an orphaned WAL row needs, derived from LIVE
        broker truth rather than the row itself.

        Stage 3 (shorts): ``pending_protection_restores`` NOW carries a
        persisted ``side`` column (see ``insert_pending_protection_restore``
        / ``_write_ahead_protection_restore``) written at the moment the
        row is created, by whoever is closing the position and therefore
        already knows which side it is. This function is no longer the
        primary source of truth — see ``_resolve_wal_row_side``, which
        prefers the row's own persisted value and calls this ONLY as the
        fallback for a row written before the migration (persisted
        ``side IS NULL``). For those legacy rows this is still the only
        signal available: reading the broker's CURRENT signed position for
        the symbol, fresh (not trusted from whenever the row was written,
        since it can be arbitrarily stale by the time drain gets to it).

        Returns 'sell' / 'buy' when the position is currently held one way
        or the other. Returns None both when the position can't be read
        (broker error — the caller must NOT default to 'sell': that's
        exactly the "guess a side" the design review forbids, and for a
        short's row it would try to restore a SELL stop on a position that
        has no shares to back it) and when the position is already flat
        (0) — the caller's downstream restore/reprotect call independently
        re-checks flatness before ever touching a side-dependent broker
        call, so which side an already-flat symbol "would have" used is
        moot, and returning a value here would look like a real answer.
        """
        raw = self._current_position_qty_for_finalize(symbol)
        if raw is None or raw == 0:
            return None
        return "buy" if raw < 0 else "sell"

    def _resolve_wal_row_side(self, row: dict, symbol: str) -> dict:
        """The ``side`` kwargs (``{}`` or ``{"side": "buy"}``) a drained
        WAL row needs, preferring the row's OWN persisted value.

        Stage 3 (shorts): every row written after the ``side`` column
        migration carries the real answer, recorded at write time by
        whoever created it — no broker lookup, no guessing. A row written
        BEFORE the migration carries ``side IS NULL``; for those, and only
        those, this degrades to the pre-migration behaviour — deriving the
        side from the broker's live position via
        ``_derive_close_side_for_drain`` — logged so the legacy fallback is
        visible in operator logs rather than silent.
        """
        persisted = str(row.get("side") or "").strip().lower()
        if persisted in ("buy", "sell"):
            return {} if persisted == "sell" else {"side": "buy"}
        logger.info(
            "WAL drain: row for %s has no persisted side (written before "
            "the Stage 3 side-column migration) — falling back to the "
            "live-broker-derived side, same as pre-migration behaviour",
            symbol,
        )
        return {"side": "buy"} if self._derive_close_side_for_drain(symbol) == "buy" else {}

    def _drain_pending_repegs(self) -> int:
        """Repoint trade rows the re-peg WAL says were left behind (see
        `pending_repegs`). Returns the number of rows cleared.

        Recovers the one window the bounded re-peg cannot make atomic: the
        broker accepted a replacement — minting a NEW order id and killing the
        old one — and the process died before `trades.broker_order_id` caught
        up. The stale id will report status 'replaced' forever, which is in
        neither of `_reconcile_fills`'s terminal sets, so the trade would sit
        unreconciled while a live order worked untracked.

        The broker is the authority here, not the WAL. A row whose
        `new_order_id` is still the sentinel is resolved by asking Alpaca what
        the old order became (`replaced_by`); a broker read that FAILS leaves
        the row in place for the next session rather than guessing.

        Runs at session start, before `_reconcile_fills`, alongside the other
        recovery drains.
        """
        try:
            rows = self.db.get_pending_repegs()
        except Exception as exc:  # noqa: BLE001
            logger.warning("drain_pending_repegs: DB read failed: %s", exc)
            return 0
        if not rows:
            return 0

        from src.pipeline_stages import _WAL_REPEG_SENTINEL

        drained = 0
        for row in rows:
            row_id = row["id"]
            symbol = row["symbol"]
            old_id = str(row["old_order_id"])
            new_id = str(row["new_order_id"] or "")

            if new_id == _WAL_REPEG_SENTINEL or not new_id:
                # Crash inside the replace window: we do not know whether the
                # PATCH landed. Ask.
                try:
                    resolved = self.broker.resolve_replacement_chain(old_id)
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "drain_pending_repegs: chain read raised for %s (%s) "
                        "— leaving row %d for next session",
                        old_id, exc, row_id,
                    )
                    continue
                if resolved is None:
                    logger.warning(
                        "drain_pending_repegs: broker could not resolve %s — "
                        "leaving row %d for next session", old_id, row_id,
                    )
                    continue
                if resolved == old_id:
                    # The replacement never landed. The trades row was already
                    # correct the whole time; nothing to repair.
                    logger.info(
                        "drain_pending_repegs: %s order %s was never replaced "
                        "— clearing row %d", symbol, old_id, row_id,
                    )
                    self._delete_repeg_row(row_id)
                    drained += 1
                    continue
                new_id = resolved
                try:
                    self.db.resolve_pending_repeg(row_id, new_id)
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "drain_pending_repegs: could not record %s on row %d: "
                        "%s", new_id, row_id, exc,
                    )

            trade_row_id = row.get("trade_row_id")
            if not trade_row_id:
                logger.error(
                    "drain_pending_repegs: row %d (%s, %s → %s) has no trades "
                    "row to repoint — MANUAL REVIEW: the live order id is %s",
                    row_id, symbol, old_id, new_id, new_id,
                )
                continue
            try:
                updated = self.db.repoint_trade_broker_order_id(
                    trade_row_id, old_order_id=old_id, new_order_id=new_id,
                )
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "drain_pending_repegs: repoint of trades row %s failed: "
                    "%s — leaving row %d", trade_row_id, exc, row_id,
                )
                continue
            if updated:
                logger.warning(
                    "drain_pending_repegs: recovered %s — trades row %s "
                    "repointed from replaced order %s to %s",
                    symbol, trade_row_id, old_id, new_id,
                )
            else:
                # Already repointed (the in-session code got there before the
                # crash, or a previous drain did). Nothing left to do.
                logger.info(
                    "drain_pending_repegs: trades row %s already off %s — "
                    "clearing row %d", trade_row_id, old_id, row_id,
                )
            self._delete_repeg_row(row_id)
            drained += 1

        if drained:
            logger.info("drain_pending_repegs: cleared %d row(s)", drained)
        return drained

    def _delete_repeg_row(self, row_id: int) -> None:
        try:
            self.db.delete_pending_repeg(row_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "drain_pending_repegs: could not delete row %d: %s", row_id, exc,
            )

    def _drain_pending_protection_restores(self) -> int:
        """Re-attempt orphaned protection restores from previous sessions.

        For each persisted row: re-query the SELL's terminal status. If
        terminal, run finalize from the persisted specs; on success,
        delete the row. If still non-terminal, leave the row for next
        session. Returns the number of rows successfully drained.

        Called at the start of each pipeline session so a single bail
        doesn't leave a position permanently unprotected.
        """
        try:
            rows = self.db.get_pending_protection_restores()
        except Exception as exc:
            logger.warning("drain_pending_protection_restores: DB read failed: %s", exc)
            return 0
        if not rows:
            return 0

        import json as _json
        drained = 0
        for row in rows:
            row_id = row["id"]
            symbol = row["symbol"]
            order_id = row["sell_order_id"]

            from src.execution.scale_in import WAL_SCALE_IN_SENTINEL, drain_scale_in_row
            if order_id == WAL_SCALE_IN_SENTINEL:
                try:
                    ok = drain_scale_in_row(self.broker, self.db, row)
                except Exception as exc:  # noqa: BLE001
                    logger.error(
                        "drain: scale-in WAL restore raised for %s row %d: %s "
                        "— leaving for next session",
                        symbol, row_id, exc,
                    )
                    continue
                if ok:
                    try:
                        self.db.delete_pending_protection_restore(row_id)
                    except Exception:
                        pass
                    drained += 1
                    logger.info(
                        "drain: scale-in recovery rebuilt coverage for %s "
                        "(row %d cleared)", symbol, row_id,
                    )
                continue

            # audit F1: a write-ahead row whose SELL was never confirmed
            # submitted (crash in the cancel→submit→record window). There
            # is no SELL order to query — restore coverage from the
            # broker's CURRENT position instead.
            if order_id == _WAL_SELL_SENTINEL:
                try:
                    wal_specs = _json.loads(row["specs_json"])
                except Exception as exc:
                    logger.error(
                        "drain: WAL row %d has unparseable specs_json (%s) "
                        "— deleting orphan to unblock the queue", row_id, exc,
                    )
                    try:
                        self.db.delete_pending_protection_restore(row_id)
                    except Exception:
                        pass
                    continue
                # Stage 3 (shorts): the row now carries its own `side` —
                # written at creation time by whoever closed the position,
                # so this is no longer a guess reconstructed from live
                # broker state. `_resolve_wal_row_side` prefers that
                # persisted value and only falls back to the live-broker
                # derivation (`_derive_close_side_for_drain`, defaulting to
                # 'sell' when unreadable) for a row written BEFORE this
                # column existed (`side IS NULL`) — logged when that
                # fallback fires. The premise this comment used to state —
                # "shorts cannot be opened through this system, so the gap
                # is moot" — is no longer true now that they can be.
                side_kwargs = self._resolve_wal_row_side(row, symbol) if wal_specs else {}
                try:
                    ok, retry = self._restore_after_unconfirmed_sell(
                        symbol,
                        float(row["position_qty_before_sell"]),
                        wal_specs,
                        **side_kwargs,
                    )
                except Exception as exc:
                    logger.error(
                        "drain: WAL restore raised for %s row %d: %s — "
                        "leaving for next session", symbol, row_id, exc,
                    )
                    continue
                if ok:
                    try:
                        self.db.delete_pending_protection_restore(row_id)
                    except Exception:
                        pass
                    drained += 1
                    logger.info(
                        "drain: WAL recovery rebuilt coverage for %s "
                        "(row %d cleared)", symbol, row_id,
                    )
                elif retry and len(retry) < len(wal_specs):
                    try:
                        self.db.update_pending_protection_restore_specs(
                            row_id, _json.dumps(retry),
                        )
                    except Exception as exc:
                        logger.warning(
                            "drain: failed to narrow WAL row %d: %s",
                            row_id, exc,
                        )
                continue

            try:
                fill_info = self.broker.get_order_fill_info(order_id) or {}
            except Exception as exc:
                logger.warning(
                    "drain: broker query failed for %s (order %s): %s — "
                    "leaving row %d for next session",
                    symbol, order_id, exc, row_id,
                )
                continue
            status = (fill_info.get("status") or "").lower()
            if status not in self._TERMINAL_ORDER_STATUSES:
                logger.info(
                    "drain: %s (order %s) still non-terminal (status=%s) — "
                    "leaving row %d for next session",
                    symbol, order_id, status, row_id,
                )
                continue
            try:
                cancelled_specs = _json.loads(row["specs_json"])
            except Exception as exc:
                logger.error(
                    "drain: row %d has unparseable specs_json (%s) — "
                    "deleting orphan to unblock the queue",
                    row_id, exc,
                )
                try:
                    self.db.delete_pending_protection_restore(row_id)
                except Exception:
                    pass
                continue
            # Same persisted-side-first resolution as the sentinel branch
            # above (see `_resolve_wal_row_side`): a row written after the
            # Stage 3 migration carries its own real side; only a legacy
            # `side IS NULL` row falls back to the live-broker derivation.
            finalize_side_kwargs = self._resolve_wal_row_side(row, symbol) if cancelled_specs else {}
            # Order is terminal; replay finalize from persisted specs.
            # finalize itself reads fill_info again — same broker call,
            # cheap. ``from_drain=True`` so finalize doesn't re-persist
            # if it bails (the row already exists). Only delete the row
            # when finalize CONFIRMS coverage was actually rebuilt — if
            # restore_stop_orders submits 0/N or reprotect raises, the
            # row stays and the next session retries. Codex r8 #3.
            try:
                ok, retry_specs = self._finalize_protection_after_sell(
                    order_id=order_id,
                    symbol=symbol,
                    position_qty_before_sell=float(row["position_qty_before_sell"]),
                    cancelled_specs=cancelled_specs,
                    from_drain=True,
                    **finalize_side_kwargs,
                )
                if not ok:
                    # Narrow the row to retry_specs if a partial restore
                    # made progress: re-submitting an already-alive stop
                    # next pass would create duplicates / hit
                    # held_for_orders. Codex r10 #1.
                    if retry_specs and len(retry_specs) < len(cancelled_specs):
                        try:
                            self.db.update_pending_protection_restore_specs(
                                row_id, _json.dumps(retry_specs),
                            )
                            logger.info(
                                "drain: row %d narrowed from %d to %d "
                                "spec(s) (partial restore made progress)",
                                row_id, len(cancelled_specs), len(retry_specs),
                            )
                        except Exception as exc:
                            logger.warning(
                                "drain: failed to narrow row %d after "
                                "partial restore: %s",
                                row_id, exc,
                            )
                    logger.warning(
                        "drain: finalize for %s row %d did not rebuild "
                        "coverage — leaving row for next session",
                        symbol, row_id,
                    )
                    continue
                self.db.delete_pending_protection_restore(row_id)
                drained += 1
                logger.info(
                    "drain: replayed protection finalize for %s (order %s, "
                    "row %d cleared)", symbol, order_id, row_id,
                )
            except Exception as exc:
                logger.error(
                    "drain: finalize replay failed for %s row %d: %s — "
                    "leaving row for next session",
                    symbol, row_id, exc,
                )
        if drained:
            logger.info("drain: cleared %d orphaned protection-restore row(s)", drained)
        return drained

    def _reprotect_residual_after_partial_sell(
        self, symbol: str, residual_qty: float, cancelled_specs: list[dict],
        *, side: str = "sell",
    ) -> bool:
        """After a partial exit (REDUCE / PARTIAL_SELL), place a
        fresh stop on the residual qty using the most-protective price among
        the stops we cancelled to clear held_for_orders for the SELL.

        Without this, the cancel-then-sell flow introduced in P1 #3 leaves
        the residual position naked until the next morning's BUY rebuilds an
        OTO leg — which never happens for a held-through position. The stop
        we re-place isn't a perfect copy of the original (we collapse
        multiple stops onto the highest stop_price), but it preserves at
        least the most-protective coverage that was in place pre-SELL.

        ``side`` — 'sell' (default) re-places a SELL stop below price for a
        long; 'buy' re-places a BUY stop above price for a short. This also
        flips which extreme counts as "most protective": for a long's SELL
        stop, tighter/sooner-to-trigger is the HIGHEST stop_price (closest
        to price from below); for a short's BUY stop it's the OPPOSITE —
        the LOWEST stop_price (closest to price from above). Picking the
        long-side extreme for a short would silently place the loosest,
        least-protective stop of the set instead of the tightest one.

        Returns True iff a fresh stop was successfully submitted (or there
        was nothing to do). Returns False if the submit raised — drain
        callers use this to keep the persisted recovery intent alive.
        Best-effort logging: a False return doesn't propagate the
        exception (the SELL itself already succeeded), but the caller
        knows coverage wasn't actually rebuilt.
        """
        if residual_qty <= 0 or not cancelled_specs:
            # NOTHING WAS REQUESTED: no residual to protect, or no stops were
            # cancelled to restore. Legitimately "nothing to do".
            return True
        # docs/WORK.md item 88. `[s.get("stop_price", 0) for s in specs]` then
        # `best_stop <= 0: return True` was the fail-open: a spec set whose
        # prices are all zero/absent/garbage was read as "no stop to restore"
        # and reported as SUCCESS — which makes the drain caller DELETE the
        # persisted recovery intent and leaves the residual position naked
        # with nothing left to retry it. A missing price also made `min`/`max`
        # raise on a None. Judge the values first, then decide; specs existed,
        # so "no usable price among them" is a REFUSAL, not an absence.
        from src.execution.stop_records import usable_stop_prices

        usable = usable_stop_prices(
            s.get("stop_price") for s in cancelled_specs
        )
        if not usable:
            logger.error(
                "Reprotect REFUSED for %s: %d cancelled stop spec(s) carried "
                "no usable trigger price (%r) — the residual %s share(s) are "
                "UNPROTECTED and the recovery intent is kept so the next "
                "drain retries. A garbage stop is not 'no stop needed'.",
                symbol, len(cancelled_specs),
                [s.get("stop_price") for s in cancelled_specs],
                self._format_qty(residual_qty),
            )
            return False
        best_stop = min(usable) if side == "buy" else max(usable)

        # Idempotency: drain may replay finalize on a row whose previous
        # attempt already submitted the residual stop but couldn't
        # delete the pending_protection_restores row (DB error / process
        # kill between broker submit and row delete). Without this
        # check, the next drain pass would add a SECOND stop at the same
        # price on the same residual qty — doubling exit on trigger.
        # Audit 2026-05-27: matches the discipline _restore_stop_orders
        # already enforces via its `check_idempotency` flag for the
        # restore-originals branch.
        try:
            if side == "buy":
                existing = self.broker._list_open_protective_stop_orders(symbol, side="buy")
            else:
                existing = self.broker._list_open_sell_stop_orders(symbol)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Reprotect idempotency check failed for %s: %s — "
                "proceeding with submit (may duplicate if a stop already "
                "exists)", symbol, exc,
            )
            existing = []
        for o in existing or []:
            try:
                existing_sp = float(getattr(o, "stop_price", 0) or 0)
            except (TypeError, ValueError):
                continue
            # Half-penny tolerance covers Alpaca's float<->Decimal round-trip.
            if existing_sp > 0 and abs(existing_sp - best_stop) < 0.005:
                logger.info(
                    "Reprotect skipped for %s — a stop at $%.2f already "
                    "exists at the broker (idempotent re-run)",
                    symbol, best_stop,
                )
                from src.execution.stop_records import write_back_stop_loss
                write_back_stop_loss(
                    getattr(self, "db", None), symbol, best_stop,
                    is_short=(side == "buy"),
                )
                return True

        side_kwargs = {} if side == "sell" else {"side": side}
        # Spec §11.1: a FRACTIONAL residual is re-protected by the hybrid
        # pair (durable GTC over the whole shares, DAY over the sub-share
        # remainder), not by one fractional order that the broker will only
        # accept as DAY and that would therefore take the whole position's
        # protection with it at 16:00 ET. A whole-share residual submits
        # exactly one GTC order with exactly the same arguments as before.
        whole, frac = _split_protective_qty(residual_qty)
        legs = [whole] if whole >= 1 else []
        if frac > 0:
            legs.append(frac)
        if not legs:
            legs = [residual_qty]
        last_order = None
        try:
            for leg_qty in legs:
                last_order = self.broker._submit_stop_limit_order(
                    symbol=symbol, qty=leg_qty, stop_price=best_stop, **side_kwargs,
                )
            logger.info(
                "Re-protected %s residual qty=%s @ stop $%.2f after partial exit",
                symbol, self._format_qty(residual_qty), best_stop,
            )
        except Exception as exc:
            logger.warning(
                "Re-protect failed for %s residual=%s @ $%.2f: %s — position "
                "is unprotected until the next session re-attaches a stop",
                symbol, self._format_qty(residual_qty), best_stop, exc,
            )
            return False
        from src.execution.stop_records import accepted_stop_order, write_back_stop_loss
        if isinstance(last_order, dict) and not accepted_stop_order(last_order):
            logger.warning(
                "Re-protect for %s @ $%.2f returned no accepted order id — "
                "not recording a live stop the broker does not hold",
                symbol, best_stop,
            )
            return False
        write_back_stop_loss(
            getattr(self, "db", None), symbol, best_stop,
            is_short=(side == "buy"),
        )
        return True

    @staticmethod
    def _order_accepted(order: dict, symbol: str, side: str) -> bool:
        """Returns True iff the order payload looks like a live broker order.

        Used before appending to the trades audit log so we don't record
        phantom fills. Alpaca can return an error-shaped dict (missing id, or
        status like 'rejected' / 'expired'); recording those as BUY / SELL
        would make the audit log diverge from broker reality.
        """
        if not order or not order.get("id"):
            logger.error(
                "%s %s: broker returned no order id (payload=%s) — skipping audit",
                side.upper(), symbol, order,
            )
            return False
        status = (order.get("status") or "").lower()
        if status in ("rejected", "canceled", "cancelled", "expired", "error"):
            logger.error(
                "%s %s: broker rejected order (status=%s) — skipping audit",
                side.upper(), symbol, status,
            )
            return False
        return True

    @staticmethod
    def _clamp_queued_earnings_buys(
        decisions: list[TradeDecision],
        earnings_results: list[dict],
        max_pct: float = 5.0,
        positions: list | None = None,
        total_value: float | None = None,
    ) -> list[TradeDecision]:
        """Hard-cap the RESULTING position weight on symbols with queued
        (just-filed) earnings.

        A 10-Q filed today but not yet analyzed by the LLM can move the stock
        ±10% overnight. PM shouldn't size up before the analyst has read it.
        The prompt rule asks PM to self-comply ("cap at target_weight_pct <=
        5.0"); this is the belt that holds when the LLM ignores it.

        2026-07-16 audit: the belt capped the wrong number. By this point in
        the pipeline `allocation_pct` is the constructor's DELTA (target minus
        current weight), not the target — so a name already held at 15% with
        an unread filing could be topped up to 20% because the ADD itself was
        <= 5%. The cap now measures what it documents: existing weight + add.
        `positions`/`total_value` are optional so the old delta-only behavior
        remains for callers that can't supply a book (tests, and any future
        caller with no position context) rather than crashing.
        """
        queued_symbols = {
            (ea.get("symbol") or "").strip().upper()
            for ea in earnings_results
            if ea.get("queued") and not ea.get("analysis")
        }
        queued_symbols.discard("")
        if not queued_symbols:
            return decisions

        # Existing GROSS weights, same convention as the risk engine.
        current: dict[str, float] = {}
        if positions and total_value and total_value > 0:
            try:
                from src.portfolio_constructor import PortfolioConstructor
                current = PortfolioConstructor._current_weights(positions, total_value)
            except Exception as e:  # noqa: BLE001
                logger.warning("Earnings-queued cap: weight lookup failed (%s) — "
                               "falling back to delta-only capping", e)
                current = {}

        clamped: list[TradeDecision] = []
        for d in decisions:
            if d.action != "BUY" or d.symbol.upper() not in queued_symbols:
                clamped.append(d)
                continue
            from src.risk.rules import _gross_multiplier
            held_pct = current.get(d.symbol.upper(), 0.0)
            # Room left under the cap, expressed in the RAW notional units
            # `allocation_pct` is spent in (see PortfolioConstructor._build_buy).
            allowed_raw = max(0.0, max_pct - held_pct) / _gross_multiplier(d.symbol)
            if d.allocation_pct <= allowed_raw:
                clamped.append(d)
                continue
            if allowed_raw <= 0:
                logger.warning(
                    "Earnings-queued cap: DROPPING %s BUY %.2f%% — already at "
                    "%.1f%% weight, at/over the %.1f%% cap with a fresh filing "
                    "not yet analyzed",
                    d.symbol, d.allocation_pct, held_pct, max_pct,
                )
                continue   # a BUY with allocation_pct=0 is not a valid no-op downstream
            try:
                reduced = d.model_copy(update={"allocation_pct": round(allowed_raw, 2)})
                logger.warning(
                    "Earnings-queued cap: %s BUY %.2f%% → %.2f%% (held %.1f%%, "
                    "cap %.1f%%; fresh filing not yet analyzed)",
                    d.symbol, d.allocation_pct, allowed_raw, held_pct, max_pct,
                )
                clamped.append(reduced)
            except Exception as e:
                logger.warning("Earnings-queued cap copy failed for %s: %s — keeping original", d.symbol, e)
                clamped.append(d)
        return clamped

    def _is_trading_day(self) -> bool:
        try:
            return self.broker.is_trading_day()
        except Exception as exc:
            logger.warning("Trading-day check failed; assuming market closed: %s", exc)
            return False

    def _reconcile_fills(self, ctx: RunContext | None = None) -> None:
        """Update trade rows' fill_status by asking the broker for terminal info.

        Phase 3 groundwork: decouples "we submitted an order" from "the order
        actually filled." Readers (compute_trade_calibration, get_symbol_last_buy,
        recent_sells) filter on fill_status so a limit order that never crossed
        doesn't pollute PM memory or calibration stats.

        Scoped to a single run_id when ctx is provided — we don't want to
        retroactively flip stale submissions from previous days. Alpaca
        purges order history after a few days; unreconciled-and-unreachable
        orders stay at 'submitted' and are effectively treated as filled by
        the legacy-compat NULL-or-filled filter, which is a tolerable
        failure mode.
        """
        run_id = ctx.run_id if ctx is not None else None
        try:
            rows = self.db.get_unreconciled_orders(run_id=run_id)
        except Exception as e:
            logger.warning("reconcile_fills: DB lookup failed: %s", e)
            return
        if not rows:
            return
        terminal_ok = {"filled"}
        terminal_fail = {"canceled", "cancelled", "expired", "rejected", "done_for_day"}

        def _record_broker_event(row: dict, status: str, fill_qty, fill_price) -> None:
            import json
            try:
                requested = float(row.get("qty") or 0)
                actual = float(fill_qty or 0)
                action = str(row.get("action") or "")
                event_run_id = row.get("run_id") or (ctx.run_id if ctx else None)
                if not event_run_id:
                    return
                if actual > 0:
                    outcome = "filled" if requested <= 0 or actual + 1e-9 >= requested else "partially_filled"
                else:
                    outcome = status
                payload = {
                    "stage": "order", "outcome": outcome,
                    "reason": "broker_reconciliation", "broker_status": status,
                    "broker_order_id": row.get("broker_order_id"),
                    "fill_qty": actual or None, "fill_price": fill_price,
                }
                self.db.insert_specialist_evidence(
                    run_id=event_run_id,
                    agent_name="pipeline", kind="pipeline_event", scope="symbol",
                    symbol=row.get("symbol"), decision_id=row.get("decision_id"),
                    evidence_json=json.dumps(payload, sort_keys=True),
                )
                if actual > 0 and action not in {"BUY", "SWEEP_BUY", "HOLD"}:
                    self.db.insert_specialist_evidence(
                        run_id=event_run_id,
                        agent_name="pipeline", kind="pipeline_event", scope="symbol",
                        symbol=row.get("symbol"), decision_id=row.get("decision_id"),
                        evidence_json=_json.dumps({
                            "stage": "position_management",
                            "outcome": "exited" if requested <= 0 or actual + 1e-9 >= requested else "partially_exited",
                            "reason": action.lower(), "broker_status": status,
                            "fill_qty": actual, "fill_price": fill_price,
                        }, sort_keys=True),
                    )
            except Exception as e:  # evidence is never trading authority
                logger.warning("reconcile_fills: lifecycle evidence failed: %s", e)

        for row in rows:
            order_id = row.get("broker_order_id")
            if not order_id:
                continue
            try:
                info = self.broker.get_order_fill_info(order_id)
            except Exception as e:
                logger.warning("reconcile_fills: broker lookup failed for %s: %s", order_id, e)
                continue
            if info is None:
                continue
            status = info.get("status") or ""
            fill_qty = info.get("filled_qty") or None
            fill_price = info.get("filled_avg_price") or None
            if status in terminal_ok:
                self.db.update_trade_fill(
                    broker_order_id=order_id, fill_status="filled",
                    fill_qty=fill_qty,
                    fill_price=fill_price,
                )
                _record_broker_event(row, status, fill_qty, fill_price)
                logger.info(
                    "Reconciled %s: filled (qty=%s, avg=$%s)",
                    order_id, fill_qty, fill_price,
                )
            elif status in terminal_fail:
                self.db.update_trade_fill(
                    broker_order_id=order_id, fill_status=status,
                    fill_qty=fill_qty,
                    fill_price=fill_price,
                )
                _record_broker_event(row, status, fill_qty, fill_price)
                if fill_qty and float(fill_qty) > 0:
                    logger.warning(
                        "Reconciled %s: terminal status=%s with partial fill "
                        "(qty=%s, avg=$%s)",
                        order_id, status, fill_qty, fill_price,
                    )
                else:
                    logger.warning("Reconciled %s: did NOT fill (status=%s)", order_id, status)
            # Non-terminal statuses (new, accepted, partially_filled) stay
            # 'submitted' for the next reconciliation pass to pick up.

    def _reconcile_orphan_pending_submits(self) -> int:
        """Resolve BUY write-ahead orphans (audit F4).

        A crash between broker.submit_order() returning and
        confirm_trade_submitted() landing leaves a 'pending_submit' row
        with broker_order_id=NULL while the broker may actually hold (and
        fill) the order. Nothing swept these, so the fill went untracked
        forever — position/cash drift. For each orphan:

          - exactly ONE broker order matching symbol+side+qty → adopt its
            id (confirm_trade_submitted); _reconcile_fills then resolves
            the fill normally.
          - broker query FAILED (list_recent_orders → None) → leave the
            row; retry next session. NEVER mark submit_failed on a
            transient API failure (review #2): a real / already-filled
            BUY would be silently dropped.
          - query OK + ZERO matching orders → the submit never landed;
            mark submit_failed.
          - AMBIGUOUS (>1 candidate) → do NOT guess. Adopting the wrong
            order would mis-track real money — leave the row pending and
            ERROR-log for manual reconciliation.

        Best-effort and self-contained: any per-row failure is logged and
        skipped, never breaks the session. Called once per session at
        entry, beside _drain_pending_protection_restores.
        """
        from datetime import datetime, timedelta, timezone

        try:
            rows = self.db.get_orphaned_pending_submits()
        except Exception as exc:
            logger.warning("orphan-sweep: DB read failed: %s", exc)
            return 0
        if not rows:
            return 0

        resolved = 0
        # Generous lookback — Alpaca submitted_at vs our insert timestamp
        # plus any clock skew. A day covers every realistic crash-restart.
        after = datetime.now(timezone.utc) - timedelta(hours=24)
        for row in rows:
            row_id = row["id"]
            symbol = row["symbol"]
            try:
                want_qty = float(row.get("qty") or 0)
            except (TypeError, ValueError):
                want_qty = 0.0
            try:
                candidates = self.broker.list_recent_orders(symbol, "buy", after)
            except Exception as exc:
                logger.warning(
                    "orphan-sweep: broker query raised for %s row %d: %s — "
                    "leaving for next session", symbol, row_id, exc,
                )
                continue
            if candidates is None:
                # Query FAILED (not "no such order"). Marking
                # submit_failed here would discard a possibly-real /
                # already-filled BUY. Leave the row for next session.
                logger.warning(
                    "orphan-sweep: broker order query unavailable for %s "
                    "row %d — leaving pending_submit for next session "
                    "(NOT marking submit_failed on a transient failure)",
                    symbol, row_id,
                )
                continue
            matches = [
                c for c in candidates
                if c.get("id")
                and abs(float(c.get("qty") or 0) - want_qty) < 1e-6
            ]
            if len(matches) == 1:
                bid = matches[0]["id"]
                try:
                    self.db.confirm_trade_submitted(row_id, broker_order_id=bid)
                    resolved += 1
                    logger.warning(
                        "orphan-sweep: adopted broker order %s for %s row %d "
                        "(BUY write-ahead survived a crash) — _reconcile_fills "
                        "will resolve its fill", bid, symbol, row_id,
                    )
                except Exception as exc:
                    logger.error(
                        "orphan-sweep: adopt failed for %s row %d: %s",
                        symbol, row_id, exc,
                    )
            elif not matches:
                try:
                    self.db.mark_trade_submit_failed(row_id)
                    resolved += 1
                    logger.warning(
                        "orphan-sweep: no broker order matches %s row %d "
                        "(qty=%.4f) — submit never landed; marked "
                        "submit_failed", symbol, row_id, want_qty,
                    )
                except Exception as exc:
                    logger.error(
                        "orphan-sweep: mark_submit_failed for %s row %d: %s",
                        symbol, row_id, exc,
                    )
            else:
                logger.error(
                    "orphan-sweep: %d ambiguous broker orders for %s row %d "
                    "(qty=%.4f) — NOT guessing (mis-adoption mis-tracks "
                    "money); leaving pending_submit for manual review",
                    len(matches), symbol, row_id, want_qty,
                )
        if resolved:
            logger.info("orphan-sweep: resolved %d pending_submit row(s)", resolved)
        return resolved

    @staticmethod
    def _parse_broker_fill_timestamp(filled_at: str | None) -> str | None:
        """Convert a broker `filled_at` ISO-8601 string to the naive-UTC
        `trades.timestamp` format (`Database._sqlite_utc_timestamp`).

        Backdating a stop-out row to when it ACTUALLY filled (rather than
        to whenever this reconciler happened to notice) is what makes
        `compute_trade_calibration`'s hold-days and win/loss dating, and
        `_build_post_exit_reality`'s window filtering, measure the real
        exit instead of the detection lag. This is safe to do: the FIFO
        cost-basis walk in `_realized_pnl_through_trade` orders by `id`,
        not `timestamp`, so backdating this column can never corrupt a
        realized_pnl computation — id order already reflects insertion
        order, which is always AFTER every row it needs to net against.

        Returns None (→ `insert_stop_out_trade` falls back to "now") when
        the broker didn't report a fill time or the string doesn't parse —
        never raises, never guesses a fake time.
        """
        if not filled_at:
            return None
        try:
            from datetime import datetime as _dt
            dt = _dt.fromisoformat(filled_at)
        except (TypeError, ValueError):
            return None
        return Database._sqlite_utc_timestamp(dt)

    def _flag_stop_out_anomaly(
        self, *, run_id: str | None, symbol: str, outcome: str, detail: str,
        **extra,
    ) -> None:
        """Write a `specialist_evidence` flag for a stop-out reconciliation
        anomaly — mirrors `_reconcile_fills`'s `_record_broker_event` shape
        so ops tooling that already reads `kind='pipeline_event'` rows sees
        this the same way. Always ALSO logged at ERROR: the whole point of
        "fail loud" is that this must not depend on anyone going looking in
        the evidence table (2026-08-28 ONDS/CCJ sat silent for a full
        trading day before anyone noticed realized_pnl was NULL)."""
        import json
        logger.error("stop-out reconcile: %s %s — %s", symbol, outcome, detail)
        if not run_id:
            return
        try:
            payload = {
                "stage": "reconciliation", "outcome": outcome,
                "reason": "stop_out_reconciler", "detail": detail, **extra,
            }
            self.db.insert_specialist_evidence(
                run_id=run_id, agent_name="pipeline", kind="pipeline_event",
                scope="symbol", symbol=symbol,
                evidence_json=json.dumps(payload, sort_keys=True, default=str),
            )
        except Exception as exc:  # noqa: BLE001 — evidence is never trading authority
            logger.warning("stop-out reconcile: flag write failed: %s", exc)

    def _reconcile_stop_out_fills(self, run_id: str | None = None) -> list[dict]:
        """Write back exits the broker made unilaterally that the ledger
        never heard about — closing the 2026-08-28 ONDS/CCJ accounting gap.

        WHAT HAPPENED: ONDS (17 sh @ 8.53, bought 2026-08-27) and CCJ (2 sh
        @ 107.465, bought 2026-08-27) were both closed by their broker-
        resident GTC protective stop-limit order on 2026-08-28 — ONDS at
        7.93 (realized -$10.20), CCJ at 102.955 (realized -$9.02). The
        `positions` table (a derived snapshot of `AlpacaBroker.get_positions`
        via `_sync_positions_from_broker` / `Database.sync_positions`) correctly went to
        zero for both. The `trades` table did not: no SELL/exit row was
        ever written, and the original BUY rows sat forever at
        `realized_pnl IS NULL`. Across the whole ledger, `realized_pnl` was
        set on exactly 4 of 36 trades — every one an exit the system itself
        had submitted (SELL / REDUCE / TRAIL_STOP / SWEEP_SELL all call
        `insert_trade` at submission time, and `_reconcile_fills` /
        `update_trade_fill` fill in `realized_pnl` once the broker confirms
        the fill). A protective stop is different: `place_entry_protection`,
        `_repair_stop_coverage`, and `shift_stops_down` all place a REAL
        order at the broker, but none of them ever write that order into
        `trades` — there was no row for `_reconcile_fills` to find, so a
        stop-out was invisible to the ledger by construction, not by bug in
        the reconciliation LOOP itself.

        Why this matters more than a bookkeeping nit: every exit the ledger
        DOES record is one the system chose; every exit it misses is one
        the market forced. Those are not a random sample of trades — a
        protective stop only fires on a LOSS. Silently dropping stop-outs
        biases every realized-P&L figure upward and starves
        `compute_trade_calibration` / the position reviewer / Phase 7
        measurement of exactly the outcomes most worth learning from.

        HOW THIS DETECTS IT (broker-truth diff, not a stop-order allowlist):
        compare what the ledger BELIEVES it holds per symbol
        (`Database.get_symbols_with_open_ledger_qty` — BUY/SWEEP_BUY minus
        every other executed exit) against what the broker ACTUALLY shows
        (`AlpacaBroker.get_positions`). Whenever the ledger claims more
        shares than the broker has, something closed part or all of that
        position without telling the ledger. For each such symbol, ask the
        broker directly for filled SELL orders since the reconciliation
        lookback window (`ReconciliationConfig.stop_out_lookback_days`) and
        record any whose broker_order_id the ledger has never seen — this
        catches the ORIGINAL entry-protection stop, a coverage-repair
        replacement, an ex-dividend-shifted stop, or any other broker-side
        SELL this process placed but never logged, without needing to
        enumerate every code path that can place one.

        Scoped to LONGS only (a positive ledger/broker qty gap): a short's
        protective stop is a BUY-to-cover, which is deliberately deferred —
        no order path in this repo can open a short's exit position yet
        that this reconciler would need to untangle from a BUY-to-cover
        stop (see shorts-safe's staged rollout). Flagged, not silently
        skipped, if a short ever does show a mismatch (see below).

        Idempotent by construction: `Database.insert_stop_out_trade` keys
        on `broker_order_id` under the same lock as the check, so however
        many of the 5 session entry points (morning / intra_check / midday
        / close / evening) run this, and however many times each does, a
        given stop-out fill is written exactly once.

        FAIL LOUD, NEVER GUESS: when a gap is found but the broker's own
        order history doesn't explain it (query failure, or genuinely no
        matching filled SELL inside the lookback window), this does NOT
        invent a price or silently move on — it logs at ERROR and writes a
        `specialist_evidence` flag an operator can find. Same discipline
        for a recorded stop-out whose `realized_pnl` comes back NULL
        because the ledger's own BUY history can't cover the exited
        quantity (`_realized_pnl_through_trade` already refuses to guess
        there) — the row is still written (never dropped), just flagged.

        Returns a list of `{symbol, ledger_qty, broker_qty, matched,
        recorded}` dicts describing what this pass found, for the caller /
        tests to inspect. Every branch is defensive: a broker or DB failure
        on one symbol is logged and skipped, never aborts the pass for the
        rest of the book.
        """
        reco_cfg = getattr(getattr(self, "config", None), "reconciliation", None)
        if reco_cfg is None:
            # No config attached (unit-test pipelines built via
            # TradingPipeline.__new__, or a settings.yaml genuinely missing
            # the section before ReconciliationConfig's default_factory
            # applies) — mirrors _force_delever's same defensive bail.
            return []
        lookback_days = reco_cfg.stop_out_lookback_days

        try:
            ledger_qty = self.db.get_symbols_with_open_ledger_qty()
        except Exception as exc:  # noqa: BLE001
            logger.warning("stop-out reconcile: ledger qty lookup failed: %s", exc)
            return []
        if not ledger_qty:
            return []

        try:
            broker_positions = self.broker.get_positions()
        except Exception as exc:  # noqa: BLE001
            logger.warning("stop-out reconcile: broker positions lookup failed: %s", exc)
            return []
        broker_qty: dict[str, float] = {}
        for p in broker_positions or []:
            symbol = getattr(p, "symbol", None)
            if not symbol:
                continue
            try:
                broker_qty[symbol] = float(getattr(p, "qty", 0) or 0)
            except (TypeError, ValueError):
                continue

        from datetime import datetime, timedelta, timezone
        after = datetime.now(timezone.utc) - timedelta(days=lookback_days)

        results: list[dict] = []
        for symbol, ledger_open in ledger_qty.items():
            if ledger_open <= 1e-6:
                continue  # ledger already believes it's flat — nothing to reconcile
            held = broker_qty.get(symbol, 0.0)
            gap = ledger_open - held
            if gap <= 1e-6:
                # Broker holds AT LEAST what the ledger expects. A broker
                # showing MORE than the ledger (gap negative) is a
                # different defect class — an untracked BUY — and not
                # something this reconciler invents a fix for; it is
                # visibly a short scenario too (ledger_open is a LONG-only
                # count so a negative-qty broker position also lands here
                # with gap << 0 and is correctly skipped).
                continue

            try:
                known_ids = self.db.get_known_broker_order_ids(symbol)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "stop-out reconcile: known-order lookup failed for %s: %s",
                    symbol, exc,
                )
                continue
            try:
                fills = self.broker.list_filled_sell_orders(symbol, after=after)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "stop-out reconcile: broker fill query raised for %s: %s",
                    symbol, exc,
                )
                continue
            if fills is None:
                # Query FAILED (not "no fills") — same None-means-retry
                # contract as list_recent_orders. Leave the gap for the
                # next reconciliation pass rather than concluding anything.
                logger.warning(
                    "stop-out reconcile: broker order query unavailable for "
                    "%s (ledger=%.4f, broker=%.4f) — leaving the gap for "
                    "the next pass", symbol, ledger_open, held,
                )
                continue

            new_fills = [f for f in fills if f.get("id") and f["id"] not in known_ids]
            if not new_fills:
                self._flag_stop_out_anomaly(
                    run_id=run_id, symbol=symbol,
                    outcome="stop_out_gap_unexplained",
                    detail=(
                        f"ledger believes {ledger_open:.4f} sh open, broker "
                        f"shows {held:.4f}, but no untracked filled SELL "
                        f"order was found in the last {lookback_days} "
                        f"day(s) — recording nothing rather than guessing"
                    ),
                    ledger_qty=ledger_open, broker_qty=held,
                    lookback_days=lookback_days,
                )
                # PAGE the owner. Until 2026-09-17 this wrote an ERROR line
                # and an evidence flag and nothing else, which is the same
                # silence that let the 2026-08-28 ONDS/CCJ stop-outs sit
                # unnoticed for a trading day. The desk's record and the
                # broker's record disagree and no sale explains it: that is
                # fill confirmation having failed somewhere upstream, and it
                # is the owner's P&L that is wrong because of it.
                try:
                    from src.notifier import alert_records_disagree_with_broker
                    alert_records_disagree_with_broker(
                        symbol, desk_qty=ledger_open, broker_qty=held,
                        lookback_days=lookback_days,
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "stop-out reconcile: records-disagree alert for %s "
                        "could not be sent: %s", symbol, exc,
                    )
                results.append({
                    "symbol": symbol, "ledger_qty": ledger_open,
                    "broker_qty": held, "matched": False, "recorded": 0,
                })
                continue

            recorded = 0
            for fill in new_fills:
                try:
                    row_id, created = self.db.insert_stop_out_trade(
                        symbol=symbol, qty=fill["qty"], price=fill["price"],
                        broker_order_id=fill["id"],
                        filled_at=self._parse_broker_fill_timestamp(fill.get("filled_at")),
                        run_id=run_id,
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.error(
                        "stop-out reconcile: failed to record %s order %s: %s "
                        "— will retry next pass (NOT lost, just not yet "
                        "written)", symbol, fill.get("id"), exc,
                    )
                    continue
                if not created:
                    # Another session's pass already recorded this exact
                    # broker order — expected under the idempotency
                    # contract, not an error.
                    continue
                recorded += 1
                row = self.db.get_trades(symbol=symbol, limit=1)
                realized = None
                for r in row:
                    if r.get("id") == row_id:
                        realized = r.get("realized_pnl")
                        break
                logger.warning(
                    "STOP-OUT RECORDED: %s %s sh @ $%.4f (order %s, "
                    "realized_pnl=%s) — broker-initiated protective-stop "
                    "fill written back to the ledger by the stop-out "
                    "reconciler", symbol, self._format_qty(fill["qty"]),
                    fill["price"], fill["id"],
                    "unknown" if realized is None else f"${realized:.2f}",
                )
                if realized is None:
                    self._flag_stop_out_anomaly(
                        run_id=run_id, symbol=symbol,
                        outcome="stop_out_pnl_unmatched",
                        detail=(
                            f"order {fill['id']} recorded ({fill['qty']} sh "
                            f"@ ${fill['price']:.4f}) but realized_pnl could "
                            f"not be computed — the ledger's own BUY history "
                            f"doesn't cover this exit quantity; needs manual "
                            f"review, not a guessed number"
                        ),
                        broker_order_id=fill["id"], qty=fill["qty"],
                        price=fill["price"],
                    )
            results.append({
                "symbol": symbol, "ledger_qty": ledger_open,
                "broker_qty": held, "matched": True, "recorded": recorded,
            })
        return results

    def _build_position_history(self, positions) -> dict[str, dict]:
        """L2 memory: for each held symbol, entry context + Tech rating trajectory.

        PM uses this to anchor 'when did I buy + why' and recognize when a fresh
        setup has been maturing vs stuck vs invalidated.
        """
        from datetime import date as _date
        from src.execution.stop_records import recorded_initial_stop
        out: dict[str, dict] = {}
        today = et_today()
        for p in positions:
            sym = p.symbol
            entry = None
            try:
                try:
                    qty = float(getattr(p, "qty", 0) or 0)
                except (TypeError, ValueError):
                    qty = 0.0
                opening = "SHORT" if qty < 0 else "BUY"
                entry = self.db.get_symbol_last_buy(sym, action=opening)
            except Exception as e:
                logger.warning("position_history: last_buy lookup failed for %s: %s", sym, e)

            entry_date_str = None
            days_held: int | None = None
            if entry and entry.get("timestamp"):
                try:
                    ts = entry["timestamp"]
                    entry_date = _date.fromisoformat(ts[:10]) if isinstance(ts, str) else None
                    if entry_date is not None:
                        entry_date_str = str(entry_date)
                        days_held = max(0, (today - entry_date).days)
                except (ValueError, TypeError):
                    pass

            try:
                tech_history = self.tech_store.get_history(sym, days=7)
            except Exception as e:
                logger.warning("position_history: tech history failed for %s: %s", sym, e)
                tech_history = []

            out[sym] = {
                "entry_date": entry_date_str,
                "entry_price": entry.get("price") if entry else None,
                "entry_reasoning": (entry.get("reasoning") or "")[:280] if entry else "",
                # Real, untruncated falsifier condition — see
                # TradeDecision.thesis_invalid_if in models.py. Carried
                # ALONGSIDE entry_reasoning (never a replacement for it):
                # the embedded "(invalid if: ...)"/"(thesis_invalid_if: ...)"
                # text above is truncated at 280 chars here (and 500 chars
                # upstream in the constructor), which could silently cut off
                # a long condition. This column is None for legacy rows
                # written before the trades.thesis_invalid_if column existed.
                "thesis_invalid_if": entry.get("thesis_invalid_if") if entry else None,
                "days_held": days_held,
                "tech_history": tech_history,
                # The stop recorded at entry — RiskStage's structural
                # holding-discipline check (spec item 25) reads this to find
                # the level backing it. Deliberately the frozen entry-time
                # stop (`initial_stop_loss`), not the live `stop_loss` a
                # trail may have since written back, and not the live
                # broker stop: same "the bet that was actually made"
                # reasoning `_build_position_facts.initial_stop` documents.
                "stop_loss": (
                    recorded_initial_stop(entry) or None
                ) if entry else None,
            }
        return out

    def _build_weekly_narrative(self) -> str:
        """L3a memory: last 7 evenings' daily_summary + daily_pnl, compact."""
        try:
            insights = self.db.get_recent_insights(limit=7)
        except Exception as e:
            logger.warning("weekly_narrative: insights fetch failed: %s", e)
            insights = []
        if not insights:
            return ""
        try:
            pnl_rows = self.db.get_daily_pnl(limit=14)
        except Exception:
            pnl_rows = []
        pnl_by_date = {r["date"]: r for r in pnl_rows}
        lines = []
        # insights come newest-first; display oldest→newest so the "arc" reads naturally
        for row in reversed(insights):
            d = row.get("date", "?")
            summary = (row.get("tomorrow_outlook") or row.get("lessons") or "").strip()
            if len(summary) > 220:
                summary = summary[:217] + "..."
            pnl = pnl_by_date.get(d) or {}
            ret = pnl.get("daily_return_pct")
            ret_str = f"{ret:+.2f}%" if isinstance(ret, (int, float)) else "n/a"
            risk = row.get("risk_rating", "?")
            lines.append(f"- {d}: {ret_str} ({risk}) — {summary}")
        return "\n".join(lines)

    def _build_macro_trajectory(self) -> str:
        """L3b memory: last 7 days of macro regime / confidence / equity outlook.

        No invested target: macro stopped setting one with the owner's
        fully-invested mandate (2026-09-17). Older snapshots still carry
        `position_guidance.target_invested_pct`; it is deliberately not read.
        """
        try:
            history = self.macro_store.load_history(days=7)
        except Exception as e:
            logger.warning("macro_trajectory: load_history failed: %s", e)
            history = []
        if not history:
            return ""
        lines = []
        for snap in history:
            d = snap.get("date", "?")
            regime = snap.get("regime", "?")
            conf = snap.get("confidence", "?")
            outlook = snap.get("equity_outlook") or "?"
            lines.append(f"- {d}: {regime} ({conf}) → outlook {outlook}")
        return "\n".join(lines)

    def _build_active_state_changes(self) -> str:
        """L3c memory: HIGH-conviction state_changes from the last 14 days, deduped."""
        try:
            changes = self.news_store.recent_state_changes(lookback_days=14, limit=8)
        except Exception as e:
            logger.warning("active_state_changes: news_store failed: %s", e)
            changes = []
        if not changes:
            return ""
        lines = []
        for ch in changes:
            d = ch.get("first_seen_date", "?")
            event = (ch.get("event") or "")[:160]
            symbols = ch.get("affected_symbols") or []
            # Phase 13 catalyst-gate fix: render each symbol's direction
            # inline as `SYMBOL(direction)` so `PortfolioManagerAgent.
            # _state_change_symbols_by_date` can parse it back out — this
            # is a round-trip over a format this repo owns end to end
            # (same discipline as the rest of this block). A symbol with
            # no recorded `symbol_direction` (older persisted reports
            # predating this field, or the news analyst genuinely
            # omitting one) renders as `(unknown)`, which the PM-side
            # parser treats as not qualifying — fail closed, never an
            # upgrade to "assume it's good news."
            directions = ch.get("symbol_direction") or {}
            if symbols:
                syms = ", ".join(
                    f"{s.strip().upper()}({directions.get(s.strip().upper(), 'unknown')})"
                    for s in symbols[:6]
                )
            else:
                syms = "—"
            lines.append(f"- [{d}] {event} → {syms}")
        return "\n".join(lines)

    def _handle_ex_dividends(self, positions, run_id: str) -> list[dict]:
        """Lower stops by the upcoming dividend amount the day before ex-div.

        On ex-div day, the stock's open drops by approximately the dividend
        per share — a mechanical move, not a thesis break. A tight stop set
        against normal price action can trigger for no real reason and kick
        us out of a winner. This runs at midday the day BEFORE ex-div and
        lowers each relevant position's stop by the dividend amount so the
        mechanical gap doesn't touch it.

        Idempotent per ET date: if we already adjusted this symbol today
        (tagged 'ex-div' in reasoning), skip. Detects "tomorrow is ex-div"
        in ET.
        """
        from datetime import timedelta as _td
        orders: list[dict] = []
        today = et_today()
        # NEXT TRADING day, not calendar tomorrow (2026-07-16 audit): sessions
        # only run Mon-Fri, so `today + 1 day` can never BE a Monday — every
        # Monday ex-div silently went unadjusted, and Friday's sessions (the
        # last chance to act) computed Saturday. Same hole for any ex-div the
        # day after a holiday. Fall back to calendar+1 if the calendar lookup
        # fails — degrading to today's behavior beats crashing the session.
        next_trading_day = today + _td(days=1)
        for _ in range(7):
            try:
                if self.broker.is_trading_day(next_trading_day):
                    break
            except Exception as e:  # noqa: BLE001
                logger.warning("ex-div: is_trading_day failed (%s) — falling back "
                               "to calendar+1", e)
                next_trading_day = today + _td(days=1)
                break
            next_trading_day += _td(days=1)

        for p in positions:
            # Deliberately long-only, not just "not yet generalised" — a
            # short OWES the dividend to the share lender (a cash liability)
            # rather than receiving it, so there is no mechanical gap-down
            # here for a stop-shift to absorb. See broker.shift_stops_down's
            # docstring for the fuller reasoning (shorts-safe, Stage 2).
            if p.qty <= 0:
                continue
            # Check today's trades for a prior ex-div adjustment — idempotent
            try:
                today_trades = self.db.get_trades(
                    symbol=p.symbol, today_only=True, limit=20,
                )
            except Exception as e:
                logger.warning("ex-div: today trades lookup failed for %s: %s", p.symbol, e)
                continue
            already = any(
                (t.get("action") or "").upper() == "TRAIL_STOP"
                and "ex-div" in (t.get("reasoning") or "").lower()
                for t in today_trades
            )
            if already:
                continue

            try:
                div = self.market.get_upcoming_ex_dividend(p.symbol)
            except Exception as e:
                logger.warning("ex-div: fetch failed for %s: %s", p.symbol, e)
                continue
            if not div:
                continue
            div_date = div.get("date")
            if not (div_date and today < div_date <= next_trading_day):
                # Only act on the session BEFORE ex-div. On ex-div day itself
                # the gap has already happened at open — adjustment is too
                # late — and "day after" is wrong (the stock is re-pricing
                # back to normal vol). The window is (today, next_trading_day]
                # so a Monday ex-div is caught by Friday's sessions.
                continue
            amount = div.get("amount") or 0
            if amount <= 0:
                continue

            try:
                current_stop = self.broker.get_current_stop_price(p.symbol)
            except Exception as e:
                logger.warning("ex-div: get_current_stop_price failed for %s: %s", p.symbol, e)
                current_stop = None
            if current_stop is None or current_stop <= 0:
                continue  # nothing to adjust
            new_stop = round(current_stop - amount, 2)
            if new_stop <= 0 or new_stop >= p.current_price:
                logger.warning(
                    "ex-div: %s skipped — new_stop $%.2f not protective vs current $%.2f",
                    p.symbol, new_stop, p.current_price,
                )
                continue
            try:
                # Shift EVERY stop down by the dividend, preserving per-lot
                # levels/qty (audit round 2: with per-BUY GTC stops a
                # consolidating replace could TIGHTEN a wide lot's stop to
                # the tightest lot's level minus the dividend).
                order = self.broker.shift_stops_down(p.symbol, amount)
            except Exception as e:
                logger.error("ex-div: stop shift failed for %s: %s", p.symbol, e)
                continue
            from src.execution.stop_records import accepted_stop_order, write_back_stop_loss
            if not order or (
                isinstance(order, dict) and not accepted_stop_order(order)
            ):
                continue
            try:
                write_back_stop_loss(self.db, p.symbol, new_stop, is_short=False)
            except Exception as e:  # noqa: BLE001
                logger.warning("ex-div: stop write-back failed for %s: %s", p.symbol, e)
            try:
                self.db.insert_trade(
                    symbol=p.symbol, action="TRAIL_STOP", qty=p.qty,
                    price=new_stop,
                    reasoning=(
                        f"ex-div adjustment: ex-div {div['date']}, div ${amount:.4f}/share. "
                        f"Shifted {order.get('shifted', '?')} stop(s) down by the dividend "
                        f"(highest {current_stop:.2f} → {new_stop:.2f}) to absorb the "
                        f"mechanical open gap."
                    ),
                    run_id=run_id,
                    stop_loss=new_stop,
                    broker_order_id=order.get("id"),
                    fill_status="submitted",
                )
            except Exception as e:
                logger.warning("ex-div: audit log failed for %s: %s", p.symbol, e)
            if isinstance(order, dict):
                order.setdefault("action", "TRAIL_STOP")  # audit F5
            orders.append(order)
            logger.info(
                "Ex-div adjust: %s ex-div %s div $%.4f → stop $%.2f → $%.2f",
                p.symbol, div["date"], amount, current_stop, new_stop,
            )
        return orders

    def _build_rm_recent_verdicts(self, limit: int = 5) -> str:
        """How RM has been judging PM's output over the last N sessions.

        PM reading this lets it self-calibrate: if RM has been scaling BUYs
        down for several runs in a row, PM has been oversizing — pull base
        allocations down before RM has to do it again.
        """
        try:
            rows = self.db.get_recent_agent_outputs(
                agent_name="risk_manager", limit=limit,
                before_date=session_date_key(),
            )
        except Exception as e:
            logger.warning("rm_recent_verdicts: DB fetch failed: %s", e)
            return ""
        if not rows:
            return ""
        lines = []
        for row in reversed(rows):  # oldest→newest
            ts = (row.get("timestamp") or "")[:10]
            data = self._parse_logged_agent_response(row)
            if not isinstance(data, dict):
                # PM's L5 layer reads RM history to self-calibrate.
                # Silently dropping a corrupt full_response row makes PM
                # see fewer verdicts than the DB actually contains and
                # the operator never knows. Surface it so a recurring
                # corruption pattern shows up in logs.
                logger.warning(
                    "rm_recent_verdicts: JSON parse failed for row %s: %s",
                    ts or "?", "no decision object found",
                )
                continue
            approved = data.get("approved")
            mods = data.get("modifications") or []
            scale = data.get("scale_all_buys", 1.0)
            try:
                scale = float(scale) if scale is not None else 1.0
            except (TypeError, ValueError):
                scale = 1.0
            verdict = "APPROVED" if approved else "REJECTED"
            category = (data.get("reason_category") or "clean").strip()
            extras: list[str] = [f"cat={category}"]
            if scale < 1.0:
                extras.append(f"scale_all_buys={scale:.2f}")
            if mods:
                mod_syms = sorted({m.get("symbol", "?") for m in mods if isinstance(m, dict)})
                if mod_syms:
                    extras.append(f"mods on {', '.join(mod_syms)}")
            # Phase 10.1 — a per-symbol refusal is the sharpest feedback this
            # loop can carry: `reason_category` alone tells PM the plan had an
            # R/R problem, this tells it which NAME died for it. Rendered as
            # plain text from the stored verdict, tolerant of any shape,
            # because a display line must never raise on a historical row.
            rejected = data.get("rejected_symbols") or []
            if isinstance(rejected, list):
                rej_syms = sorted({
                    (r.get("symbol") if isinstance(r, dict) else r)
                    for r in rejected
                    if isinstance(r, (dict, str))
                } - {None, ""})
                if rej_syms:
                    extras.append(f"refused {', '.join(str(s) for s in rej_syms)}")
            tag = f" [{'; '.join(extras)}]"
            reason = (data.get("reasoning") or "")[:140].strip().replace("\n", " ")
            lines.append(f"- {ts}: {verdict}{tag} — {reason}")
        return "\n".join(lines)

    def _build_pm_recent_decisions(self, limit: int = 3) -> str:
        """PM's own last N decision sets — used to spot flip-flopping against itself."""
        try:
            rows = self.db.get_recent_agent_outputs(
                agent_name="portfolio_manager", limit=limit,
                before_date=session_date_key(),
            )
        except Exception as e:
            logger.warning("pm_recent_decisions: DB fetch failed: %s", e)
            return ""
        if not rows:
            return ""
        lines = []
        for row in reversed(rows):  # oldest→newest
            ts = (row.get("timestamp") or "")[:10]
            data = self._parse_logged_agent_response(row)
            if not isinstance(data, dict):
                # PM's L6 layer reads its own recent decision history to
                # spot flip-flops. A silent skip on JSON corruption hides
                # the gap; same fix as L5 / L3d / L3f builders.
                logger.warning(
                    "pm_recent_decisions: JSON parse failed for row %s: %s",
                    ts or "?", "no decision object found",
                )
                continue
            # Phase 2: new schema emits `targets` (target weights + thesis);
            # older logs in the DB carry `decisions` (legacy TradeDecision).
            # Parse whichever is present so PM reads a unified history.
            targets = data.get("targets") or []
            decisions = data.get("decisions") or []
            summary_parts: list[str] = []
            if targets:
                for t in targets[:8]:
                    if not isinstance(t, dict):
                        continue
                    sym = t.get("symbol", "?")
                    # The live schema sizes a target by `risk_allocation_pct`;
                    # `target_weight_pct` is the legacy notional field older
                    # logs carry. Reading only the legacy one rendered every
                    # recent size as "?", and a flip-flop check that cannot
                    # see the size is not a check. Tag the unit — 1% of risk
                    # and 1% of notional are not the same number.
                    risk = t.get("risk_allocation_pct")
                    weight = t.get("target_weight_pct")
                    if risk is not None:
                        w = f"{risk}%r"
                    elif weight is not None:
                        w = f"{weight}%w"
                    else:
                        w = "?"
                    conv = (t.get("conviction") or "?")[0]
                    summary_parts.append(f"{sym}→{w}({conv})")
            elif decisions:
                for d in decisions[:8]:
                    if not isinstance(d, dict):
                        continue
                    act = d.get("action", "?")
                    sym = d.get("symbol", "?")
                    alloc = d.get("allocation_pct", "?")
                    summary_parts.append(f"{act} {sym} {alloc}%")
            if not summary_parts:
                lines.append(f"- {ts}: (no trades that day)")
                continue
            rc = data.get("reasoning_chain") or {}
            sizing = (rc.get("sizing_logic") or "")[:160].strip().replace("\n", " ")
            continuity = (rc.get("continuity_check") or "")[:160].strip().replace("\n", " ")
            line = f"- {ts}: {'; '.join(summary_parts)}"
            if sizing:
                line += f"\n    sizing: {sizing}"
            if continuity:
                line += f"\n    continuity: {continuity}"
            lines.append(line)
        return "\n".join(lines)

    def _build_projected_portfolio(
        self,
        positions,
        analyses: list[TechAnalysisResult],
        total_value: float,
        default_buy_pct: float = 5.0,
    ) -> str:
        """Preview of the book if PM rubber-stamped every BUY-rated TA candidate.

        Surfaces sector concentration BEFORE PM writes decisions, so it can
        self-correct instead of waiting for RM or the hard sector cap to flag
        it. Kept simple on purpose: no correlation math here (that's RM's
        correlation_cluster advisory). Just current vs projected sector mix.
        """
        from src.execution.broker import _get_sector
        from src.risk.rules import (
            SECTOR_SIDE_LONG, BookExposure, _effective_multiplier,
            _gross_multiplier, book_exposure, sector_side_gross,
        )
        if total_value <= 0:
            return ""
        buy_candidates = [
            a for a in analyses
            if a.rating in ("buy", "strong_buy") and a.entry_price
        ]
        if not positions and not buy_candidates:
            return ""

        cached_sectors = dict(getattr(self, "_last_symbol_sectors", {}))

        def _resolve_sector(symbol: str, fallback: str | None = None) -> str:
            sector = (fallback or "").strip() if fallback else ""
            if sector and sector != "Unknown":
                cached_sectors[symbol] = sector
                return sector

            sector = cached_sectors.get(symbol, "")
            if sector and sector != "Unknown":
                return sector

            sector = _get_sector(symbol) or "Unknown"
            if sector != "Unknown":
                cached_sectors[symbol] = sector
            return sector

        # Same `book_exposure` the PM's Account Status, the PMFacts Book
        # State block and the pre-trade advisory read. This preview used to
        # carry its own `abs(sum(mv * signed_mult))` — a fourth number for
        # the one quantity, in the same prompt as the other three, and the
        # `abs()` made a net-SHORT book render as positively invested.
        current_book = book_exposure(positions, total_value)
        current_invested_pct = current_book.deployed_pct
        current_net = current_book.net_usd
        # Spec §12.2 — GROSS (unsigned) and split by side, keyed
        # `(sector, side)`. Before §12.2 this summed SIGNED `market_value`
        # exactly as the gate did, so a held short shrank its sector in the
        # very preview whose job is to surface concentration.
        sector_gross: dict[tuple[str, str], float] = sector_side_gross(
            positions,
            resolve_sector=lambda p: _resolve_sector(p.symbol, p.sector),
            include_unknown=True,
        )

        proj_net = current_net
        proj_deployed = current_book.deployed_usd
        proj_sector = dict(sector_gross)
        unresolved_symbols: list[str] = []
        for a in buy_candidates:
            raw = total_value * (default_buy_pct / 100)
            proj_net += raw * _effective_multiplier(a.symbol)
            proj_deployed += raw
            sec = _resolve_sector(a.symbol)
            if sec == "Unknown":
                unresolved_symbols.append(a.symbol)
            # Every candidate here is BUY-rated, so it lands long-side.
            key = (sec, SECTOR_SIDE_LONG)
            proj_sector[key] = proj_sector.get(key, 0.0) + raw * _gross_multiplier(a.symbol)
        proj_book = BookExposure(
            equity=total_value, deployed_usd=proj_deployed,
            net_usd=proj_net, gross_usd=0.0,
        )
        proj_invested_pct = proj_book.deployed_pct
        self._last_symbol_sectors = cached_sectors

        def _sector_line(sector_dict: dict[tuple[str, str], float]) -> str:
            if not sector_dict:
                return "(empty)"
            sorted_secs = sorted(sector_dict.items(), key=lambda kv: -kv[1])[:5]
            return ", ".join(
                f"{sec} {side} {v / total_value * 100:.0f}%"
                for (sec, side), v in sorted_secs
            )

        lines = [
            f"- Current: {current_invested_pct:.0f}% invested (capital at work) · "
            f"net direction {current_book.net_pct:+.0f}% · sectors: {_sector_line(sector_gross)}",
        ]
        if buy_candidates:
            n = len(buy_candidates)
            shown = [a.symbol for a in buy_candidates[:8]]
            tail = f" +{n - 8} more" if n > 8 else ""
            lines.append(
                f"- If you allocate {default_buy_pct:.0f}% to each of {n} BUY-rated candidate(s) "
                f"({', '.join(shown)}{tail}):"
            )
            lines.append(
                f"    → {proj_invested_pct:.0f}% invested · net direction "
                f"{proj_book.net_pct:+.0f}% · sectors: {_sector_line(proj_sector)}"
            )
            # Spec §12.2/§12.3 — this used to carry its own hardcoded `35`,
            # a fourth sector number unrelated to config and already stale
            # against the 40 it was shadowing. It now reads the SAME
            # concentration target the constructor sizes against and the gate
            # measures against, so the preview cannot warn about a line the
            # rest of the system does not draw.
            #
            # The target, not some band below it, is the meaningful
            # threshold: at or under it crowding costs a trade nothing
            # (`sector_size_scale` returns 1.0), so there is nothing
            # actionable to tell the PM. Above it every further trade in that
            # sector is shrunk — which is exactly what the PM needs to know
            # before it writes decisions.
            target_pct = getattr(
                getattr(self, "risk_engine", None), "config", None,
            )
            target_pct = getattr(target_pct, "max_sector_pct", None) or 75.0
            overweight = [
                f"{sec} ({side})" for (sec, side), v in proj_sector.items()
                if v / total_value * 100 > target_pct and sec != "Unknown"
            ]
            if overweight:
                lines.append(
                    f"    ⚠ Sector sides over the {target_pct:.0f}% concentration "
                    f"target (each further trade there is scaled down, not "
                    f"refused): {', '.join(sorted(overweight))}"
                )
            if unresolved_symbols:
                unique = list(dict.fromkeys(unresolved_symbols))
                lines.append(
                    "    ⚠ Sector unresolved for: "
                    f"{', '.join(unique)} — projected mix may understate concentration."
                )
        return "\n".join(lines)

    def _build_recent_sells_for_grading(
        self, lookback_days: int = 2,
        symbols_bars: dict | None = None,
    ) -> list[dict]:
        """Return recent SELL-family trades joined with current quote for grading.

        Used by evening to produce `sell_decisions_assessment`. For each SELL
        in the window, we fetch the current price and compute pct move since
        the sell — positive means we left money on the table, negative means
        the exit saved capital. Broker lookup errors fall back to 0% (log).
        """
        try:
            all_rows = self.db.get_trades(limit=200, executed_only=True)
        except Exception as e:
            logger.warning("recent_sells: db fetch failed: %s", e)
            return []
        if not all_rows:
            return []
        from datetime import date as _date, timedelta as _td
        cutoff = et_today() - _td(days=lookback_days)
        # REDUCE = midday reviewer trim (discretionary partial exit — a SELL
        # decision the reviewer owns and should be graded on). TAKE_PROFIT
        # stays out: it was the rule-based auto trim (deleted 2026-09-12),
        # never a reviewer decision — historical rows still carry the label.
        # Belt (audit round 2): the vehicle also exits under EMERGENCY_SELL
        # when the breaker liquidates everything — filter by SYMBOL here,
        # mirroring _build_post_exit_reality, so parking churn never reaches
        # the grading loop under any action name.
        sweeper = self._sweeper()
        sweep_symbol = sweeper.symbol if sweeper is not None else None
        sell_actions = ("SELL", "EMERGENCY_SELL", "FORCE_DELEVER", "REDUCE")
        out: list[dict] = []
        for row in all_rows:
            action = row.get("action") or ""
            if not (action in sell_actions or action.startswith("PARTIAL_SELL")):
                continue
            ts = row.get("timestamp") or ""
            try:
                sell_date = _date.fromisoformat(ts[:10])
            except ValueError:
                continue
            if sell_date < cutoff:
                continue
            sym = row.get("symbol")
            if sweep_symbol is not None and sym == sweep_symbol:
                continue   # parking churn is not a graded decision
            sell_price = float(row.get("fill_price") or row.get("price") or 0) or 0.0
            if not sym or sell_price <= 0:
                continue
            # Current price: prefer live broker quote; degrade to position map;
            # degrade to last known OHLCV close.
            curr = 0.0
            try:
                curr = float(self.broker.get_latest_price(sym) or 0) or 0.0
            except Exception as e:
                logger.warning("recent_sells: latest price failed for %s: %s", sym, e)
            if curr <= 0:
                bars = (symbols_bars or {}).get(sym) or []
                if bars:
                    curr = float(bars[-1].close or 0)
            pct = ((curr / sell_price - 1) * 100) if (curr > 0 and sell_price > 0) else 0.0
            out.append({
                "symbol": sym,
                "sell_date": str(sell_date),
                "sell_price": sell_price,
                "current_price": round(curr, 2) if curr else 0.0,
                "pct_move_since_sell": round(pct, 2),
                "reasoning": row.get("reasoning") or "",
            })
        # Newest first, cap to avoid bloating the evening prompt
        out.sort(key=lambda r: r["sell_date"], reverse=True)
        return out[:10]

    def _build_recent_buys_for_grading(
        self, lookback_days: int = 5,
        symbols_bars: dict | None = None,
    ) -> list[dict]:
        """Mirror of `_build_recent_sells_for_grading` for entry quality.

        For each executed BUY in the window, compute the pct move since
        entry vs current price. Positive = entry still in the money (so
        far); negative = entry is underwater. Lookback is wider than
        SELLs (5d vs 2d) because BUY outcomes take longer to reveal.

        Also injects `market_relative_move_pct` per BUY = (our move) −
        (SPY move over same dates). The evening analyst reads this to
        decide whether a losing BUY was alpha-destruction (we
        under-performed the tape, positive number) vs systemic drawdown
        (market also fell, ~0 or negative number). Fetched once upfront
        so we don't round-trip SPY bars per BUY.
        """
        try:
            all_rows = self.db.get_trades(limit=200, executed_only=True)
        except Exception as e:
            logger.warning("recent_buys: db fetch failed: %s", e)
            return []
        if not all_rows:
            return []
        from datetime import date as _date, timedelta as _td
        cutoff = et_today() - _td(days=lookback_days)
        # SPY bars once — used to compute market_relative_move_pct per BUY.
        # Pad the lookback to cover the oldest BUY date + weekends.
        spy_close_by_date: dict[str, float] = {}
        spy_latest_close: float = 0.0
        try:
            spy_bars = self.market.get_ohlcv(
                "SPY", lookback_days=max(lookback_days + 5, 12)
            )
            for b in spy_bars or []:
                try:
                    spy_close_by_date[str(b.date)] = float(b.close)
                except (AttributeError, TypeError, ValueError):
                    continue
            if spy_bars:
                try:
                    spy_latest_close = float(spy_bars[-1].close)
                except (AttributeError, TypeError, ValueError):
                    spy_latest_close = 0.0
        except Exception as e:
            logger.warning("recent_buys: SPY bars fetch failed (relative-move disabled): %s", e)
        # audit round 2: get_ohlcv returns COMPLETED bars only, so during
        # market hours they stop at the previous close — while the stock leg
        # uses a LIVE quote. For a
        # same-day BUY that mismatch made spy_pct read 0.0 and every
        # market_relative grade compare a live price against a stale
        # benchmark. Same-instant legs: prefer the live SPY quote.
        try:
            spy_live = float(self.broker.get_latest_price("SPY") or 0) or 0.0
            if spy_live > 0:
                spy_latest_close = spy_live
        except Exception as e:  # noqa: BLE001
            logger.warning("recent_buys: live SPY quote failed (using last close): %s", e)
        out: list[dict] = []
        seen_symbols: set[str] = set()  # dedupe multiple buys on same symbol — use latest
        for row in all_rows:
            action = (row.get("action") or "").upper()
            if action != "BUY":
                continue
            ts = row.get("timestamp") or ""
            try:
                buy_date = _date.fromisoformat(ts[:10])
            except ValueError:
                continue
            if buy_date < cutoff:
                continue
            sym = row.get("symbol")
            buy_price = float(row.get("fill_price") or row.get("price") or 0) or 0.0
            if not sym or buy_price <= 0:
                continue
            if sym in seen_symbols:
                continue  # only surface latest BUY per symbol
            seen_symbols.add(sym)
            curr = 0.0
            try:
                curr = float(self.broker.get_latest_price(sym) or 0) or 0.0
            except Exception as e:
                logger.warning("recent_buys: latest price failed for %s: %s", sym, e)
            if curr <= 0:
                bars = (symbols_bars or {}).get(sym) or []
                if bars:
                    curr = float(bars[-1].close or 0)
            pct = ((curr / buy_price - 1) * 100) if (curr > 0 and buy_price > 0) else 0.0
            # SPY return over the same window → alpha-destruction vs systemic
            # drawdown disambiguation. Match buy_date to the nearest SPY close
            # (buy_date might not be a trading day if fill timestamp rolled
            # over into an ET weekend), walking backward up to 5 days.
            spy_entry_close = 0.0
            if spy_close_by_date and spy_latest_close > 0:
                probe = buy_date
                for _ in range(6):
                    got = spy_close_by_date.get(str(probe))
                    if got:
                        spy_entry_close = got
                        break
                    probe = probe - _td(days=1)
            if spy_entry_close > 0 and spy_latest_close > 0:
                spy_pct = (spy_latest_close / spy_entry_close - 1) * 100
                market_relative = round(pct - spy_pct, 2)
            else:
                market_relative = None
            out.append({
                "symbol": sym,
                "buy_date": str(buy_date),
                "buy_price": buy_price,
                "current_price": round(curr, 2) if curr else 0.0,
                "pct_move_since_buy": round(pct, 2),
                "market_relative_move_pct": market_relative,
                "reasoning": row.get("reasoning") or "",
            })
        out.sort(key=lambda r: r["buy_date"], reverse=True)
        return out[:10]

    def _build_recent_outlook_calibration(self, lookback: int = 10) -> dict:
        """Evening's self-calibration — pairs its own past `tomorrow_bias`
        predictions with the actual next-day return from daily_pnl.

        Returns a dict:
        {
          "samples": [{date, predicted_bias, predicted_conviction,
                       actual_return_pct, matched: bool}, ...],
          "bullish_hit_rate": float | None,
          "bearish_hit_rate": float | None,
          "high_conviction_hit_rate": float | None,
          "n": int,
        }
        Empty / None when there aren't enough pairs (first N days of run).

        "Matched" for bullish = actual > 0, bearish = actual < 0, neutral =
        within ±0.3%. This gives evening a deterministic mirror of its own
        accuracy — it can't bullshit itself into pretending it's been right
        when the numbers say otherwise.
        """
        try:
            insights = self.db.get_recent_insights(limit=lookback + 5)
        except Exception as e:
            logger.warning("outlook_calibration: insights fetch failed: %s", e)
            return {"samples": [], "n": 0}
        if not insights:
            return {"samples": [], "n": 0}
        try:
            pnl_rows = self.db.get_daily_pnl(limit=lookback + 10)
        except Exception as e:
            logger.warning("outlook_calibration: daily_pnl fetch failed: %s", e)
            return {"samples": [], "n": 0}
        pnl_by_date = {r["date"]: r.get("daily_return_pct") for r in (pnl_rows or [])}

        # Ordered trading-day series for multi-day (trend) forward returns. The
        # next-day return is NOISE in a trending tape (flat up-days score a
        # bullish call as a "miss"); a 5-session forward cumulative return is
        # the directional scorecard evening should actually weigh, so it stops
        # mis-learning a low next-day hit rate into "default neutral".
        import bisect
        _ordered = sorted(
            ((d, r) for d, r in pnl_by_date.items() if r is not None),
            key=lambda x: x[0],
        )
        _ordered_dates = [d for d, _ in _ordered]

        def _fwd_cumulative(pred_date_str: str, n: int = 5):
            """Sum daily_return_pct over the first n trading days STRICTLY
            after pred_date_str. Returns None unless the FULL n-session window
            has resolved — a partial window (e.g. 1 of 5 days for a very recent
            prediction) is just a relabeled next-day return, not a trend, so we
            withhold it rather than feed a misleading number."""
            i = bisect.bisect_right(_ordered_dates, pred_date_str)
            window = _ordered[i:i + n]
            if len(window) < n:
                return None
            return sum(r for _, r in window)

        from datetime import date as _date, timedelta as _td
        samples: list[dict] = []
        for ins in insights:
            pred_date_str = ins.get("date")
            if not pred_date_str:
                continue
            try:
                pred_date = _date.fromisoformat(pred_date_str)
            except ValueError:
                continue
            # tomorrow_bias written on day D predicts day D+1's direction.
            # But "D+1" has to be a trading day — so we find the NEXT daily_pnl
            # row after pred_date. Simplest: try +1, +2, +3 days until hit.
            actual = None
            for delta in (1, 2, 3, 4):
                cand = str(pred_date + _td(days=delta))
                if cand in pnl_by_date:
                    actual = pnl_by_date[cand]
                    break
            if actual is None:
                continue

            bias = (ins.get("tomorrow_bias") or "neutral").lower()
            conv = (ins.get("tomorrow_conviction") or "medium").lower()
            # Match rule:
            NEUTRAL_BAND = 0.3
            if bias == "bullish":
                matched = actual > NEUTRAL_BAND
            elif bias == "bearish":
                matched = actual < -NEUTRAL_BAND
            else:  # neutral
                matched = -NEUTRAL_BAND <= actual <= NEUTRAL_BAND
            # 5-session forward cumulative return — the trend/direction metric.
            fwd5 = _fwd_cumulative(pred_date_str, 5)
            TREND_BAND = 0.75  # wider neutral band over 5 sessions than the 1d 0.3
            if fwd5 is None:
                trend_matched = None
            elif bias == "bullish":
                trend_matched = fwd5 > TREND_BAND
            elif bias == "bearish":
                trend_matched = fwd5 < -TREND_BAND
            else:  # neutral
                trend_matched = -TREND_BAND <= fwd5 <= TREND_BAND
            samples.append({
                "date": pred_date_str,
                "predicted_bias": bias,
                "predicted_conviction": conv,
                "actual_return_pct": round(actual, 2),
                "matched": bool(matched),
                "fwd5_return_pct": round(fwd5, 2) if fwd5 is not None else None,
                "trend_matched": (None if trend_matched is None else bool(trend_matched)),
            })
            if len(samples) >= lookback:
                break

        n = len(samples)
        def _rate(filter_fn):
            eligible = [s for s in samples if filter_fn(s)]
            if not eligible:
                return None
            return round(100 * sum(1 for s in eligible if s["matched"]) / len(eligible), 1)

        def _trend_rate(filter_fn):
            # Only over samples with a resolved 5-session forward window.
            eligible = [s for s in samples if filter_fn(s) and s.get("trend_matched") is not None]
            if not eligible:
                return None
            return round(100 * sum(1 for s in eligible if s["trend_matched"]) / len(eligible), 1)

        return {
            "samples": samples,
            "n": n,
            # Next-day hit rates — NOISE filter; do not read as a directional verdict.
            "overall_hit_rate_pct": _rate(lambda s: True),
            "bullish_hit_rate_pct": _rate(lambda s: s["predicted_bias"] == "bullish"),
            "bearish_hit_rate_pct": _rate(lambda s: s["predicted_bias"] == "bearish"),
            "neutral_hit_rate_pct": _rate(lambda s: s["predicted_bias"] == "neutral"),
            "high_conviction_hit_rate_pct": _rate(lambda s: s["predicted_conviction"] == "high"),
            "low_conviction_hit_rate_pct": _rate(lambda s: s["predicted_conviction"] == "low"),
            # 5-session forward (trend) hit rates — the real directional scorecard.
            "overall_trend_hit_rate_pct": _trend_rate(lambda s: True),
            "bullish_trend_hit_rate_pct": _trend_rate(lambda s: s["predicted_bias"] == "bullish"),
            "bearish_trend_hit_rate_pct": _trend_rate(lambda s: s["predicted_bias"] == "bearish"),
        }

    def _build_trade_grade_summary(self, lookback_days: int = 14) -> dict:
        """Aggregate evening's structured sell_grades + buy_grades over N days.

        Feeds position_reviewer so it can see patterns like "you marked 5 of
        7 recent SELLs as premature" and lean patient today. Reads the new
        JSON columns on insights (introduced 2026-04-19); pre-v2 rows return
        NULL → treated as empty, summary gracefully degrades.

        Returns {
            "n_sells": int, "n_buys": int,
            "sell_counts": {"correct": int, "premature": int, "wrong": int},
            "buy_counts":  {"correct": int, "premature": int, "wrong": int},
            "repeat_premature_symbols": [str, ...],   # symbol premature >= 2×
            "repeat_wrong_symbols":     [str, ...],
        }
        """
        import json as _json
        empty = {
            "n_sells": 0, "n_buys": 0,
            "sell_counts": {"correct": 0, "premature": 0, "wrong": 0},
            "buy_counts":  {"correct": 0, "premature": 0, "wrong": 0},
            "repeat_premature_symbols": [],
            "repeat_wrong_symbols": [],
        }
        def _with_reality(base: dict) -> dict:
            # The deterministic post-exit block must ride along even when
            # nightly grades are absent/corrupt — it's tape-derived, not
            # grade-derived, and it's the part the grader can't sugar-coat.
            try:
                base["post_exit_reality"] = self._build_post_exit_reality(
                    lookback_days=max(lookback_days, 14),
                )
            except Exception as e:  # noqa: BLE001
                logger.warning("post_exit_reality failed (summary degrades): %s", e)
                base["post_exit_reality"] = None
            return base

        try:
            rows = self.db.get_recent_insights(limit=lookback_days + 5)
        except Exception as e:
            logger.warning("trade_grade_summary: insights fetch failed: %s", e)
            return _with_reality(empty)
        if not rows:
            return _with_reality(empty)

        sell_counts = {"correct": 0, "premature": 0, "wrong": 0}
        buy_counts = {"correct": 0, "premature": 0, "wrong": 0}
        sell_premature_by_symbol: dict[str, int] = {}
        sell_wrong_by_symbol: dict[str, int] = {}

        def _load(col: str, row: dict) -> list[dict]:
            raw = row.get(col)
            if not raw:
                return []
            try:
                v = _json.loads(raw)
            except (TypeError, ValueError) as exc:
                # Silent degradation here previously hid real data loss — if
                # evening wrote grades but they can't be parsed back, the
                # position_reviewer was reading n_sells=0 and silently losing
                # the SELL-discipline feedback loop. Warn loudly so the next
                # evening run can regenerate and we can see the symptom.
                preview = (raw if isinstance(raw, str) else str(raw))[:120]
                logger.warning(
                    "_build_trade_grade_summary: failed to parse insights[%s] "
                    "(row date=%s): %s — preview=%r",
                    col, row.get("date", "?"), exc, preview,
                )
                return []
            if not isinstance(v, list):
                logger.warning(
                    "_build_trade_grade_summary: insights[%s] (row date=%s) "
                    "expected list, got %s — ignoring",
                    col, row.get("date", "?"), type(v).__name__,
                )
                return []
            return v

        rows_in_window = rows[:lookback_days]  # newest first from get_recent_insights
        # One SELL, one vote. `_build_recent_sells_for_grading` uses a 2-day
        # window with no already-graded filter, so evening re-grades the same
        # trade on 2-3 consecutive nights and each re-grade used to count as an
        # independent sell — inflating the premature/wrong counts that drive
        # the reviewer's patience tilt (2026-07-16 audit; the production
        # insights rows show the duplicates). Rows arrive newest-first, so the
        # FIRST grade seen for a (symbol, sell_date) is the freshest — and the
        # one with the most post-exit price history behind it.
        seen_sells: set[tuple] = set()
        for row in rows_in_window:
            for g in _load("sell_grades_json", row):
                if not isinstance(g, dict):
                    continue
                sym = g.get("symbol")
                sell_date = g.get("sell_date")
                # Dedup only with a real (symbol, sell_date) key — SellGrade
                # requires sell_date, so this is the normal path. A malformed
                # row without one is counted rather than collapsed: keying on
                # (symbol, None) would fold every distinct sell of that symbol
                # into a single vote, which is a worse error than the
                # double-count this dedup removes.
                if sym and sell_date:
                    key = (sym, sell_date)
                    if key in seen_sells:
                        continue
                    seen_sells.add(key)
                grade = g.get("grade")
                if grade in sell_counts:
                    sell_counts[grade] += 1
                if sym and grade == "premature":
                    sell_premature_by_symbol[sym] = sell_premature_by_symbol.get(sym, 0) + 1
                if sym and grade == "wrong":
                    sell_wrong_by_symbol[sym] = sell_wrong_by_symbol.get(sym, 0) + 1
            for g in _load("buy_grades_json", row):
                if not isinstance(g, dict):
                    continue
                grade = g.get("grade")
                if grade in buy_counts:
                    buy_counts[grade] += 1

        summary = {
            "n_sells": sum(sell_counts.values()),
            "n_buys": sum(buy_counts.values()),
            "sell_counts": sell_counts,
            "buy_counts": buy_counts,
            "repeat_premature_symbols": sorted(
                s for s, c in sell_premature_by_symbol.items() if c >= 2
            ),
            "repeat_wrong_symbols": sorted(
                s for s, c in sell_wrong_by_symbol.items() if c >= 2
            ),
        }
        # RC4 (2026-07-16): deterministic post-exit reality. The LLM grader
        # scored 32/33 recent sells "correct" at t+1..t+3 while the tape
        # showed 28/53 exits ≥5% higher within 20 days — self-assessment
        # cannot be the only input to the patience tilt. These numbers come
        # from trades × live prices, no LLM in the loop.
        return _with_reality(summary)

    # Realized-exit actions whose post-exit trajectory is worth auditing.
    # SWEEP_SELL is deliberately absent — parking churn is not a decision.
    # STOP_OUT (2026-08-28 ONDS/CCJ) belongs here even though it is not a
    # reviewer decision — precisely BECAUSE it isn't one: "did the market
    # force us out right before a bounce" is exactly the question this
    # audit exists to answer, and a forced exit is where the answer is
    # most likely to be uncomfortable.
    # TAKE_PROFIT stays for HISTORICAL rows: the auto trim that wrote it was
    # deleted 2026-09-12 and nothing writes the label any more.
    _EXIT_AUDIT_ACTIONS = (
        "SELL", "REDUCE", "EMERGENCY_SELL", "FORCE_DELEVER", "TAKE_PROFIT",
        "STOP_OUT",
    )

    def _build_post_exit_reality(
        self, lookback_days: int = 14, min_age_days: int = 2, max_symbols: int = 12,
    ) -> dict | None:
        """What actually happened after our recent exits — from the tape.

        For every realized exit in the window (SELL family + filled
        TRAIL_STOPs) at least `min_age_days` old, compare the exit price to
        the live price. Returns None when there's nothing to audit.

        {"n": int, "n_higher_5pct": int, "avg_move_pct": float,
         "worst": [{"symbol", "date", "move_pct"} × ≤3]}   # worst = ran most
        """
        from datetime import datetime as _dt, timedelta, timezone
        try:
            rows = self.db.get_trades(limit=120)
        except Exception as e:  # noqa: BLE001
            logger.warning("post_exit_reality: trades fetch failed: %s", e)
            return None
        now = _dt.now(timezone.utc)
        window_start = now - timedelta(days=lookback_days)
        age_cutoff = now - timedelta(days=min_age_days)
        sweeper = self._sweeper()
        sweep_symbol = sweeper.symbol if sweeper is not None else None
        exits: list[dict] = []
        for row in rows:
            action = (row.get("action") or "").upper()
            # Belt on top of the SWEEP_* action exclusion: an emergency
            # liquidation can exit the sweep vehicle under EMERGENCY_SELL —
            # a ~0% T-bill "move" is noise in a decision-quality audit.
            if sweep_symbol is not None and (row.get("symbol") or "") == sweep_symbol:
                continue
            is_exit = (
                action in self._EXIT_AUDIT_ACTIONS
                or action.startswith("PARTIAL_SELL")
                or (action == "TRAIL_STOP"
                    and (row.get("fill_status") or "") == "filled")
            )
            if not is_exit:
                continue
            if action != "TRAIL_STOP" and (row.get("fill_status") or "") not in (
                "filled", "submitted",
            ):
                continue
            ts = row.get("timestamp") or ""
            try:
                dt = _dt.fromisoformat(ts.replace("Z", "+00:00")) if "T" in ts \
                    else _dt.strptime(ts, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
            except (TypeError, ValueError):
                continue
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            if not (window_start <= dt <= age_cutoff):
                continue
            exit_px = row.get("fill_price") or row.get("price")
            if not (isinstance(exit_px, (int, float)) and exit_px > 0):
                continue
            exits.append({
                "symbol": row.get("symbol"), "date": ts[:10],
                "exit_px": float(exit_px),
            })
        if not exits:
            return None
        # Live prices — one broker call per distinct symbol, capped.
        prices: dict[str, float] = {}
        for sym in list(dict.fromkeys(e["symbol"] for e in exits))[:max_symbols]:
            try:
                px = self.broker.get_latest_price(sym)
            except Exception:  # noqa: BLE001
                px = None
            if isinstance(px, (int, float)) and px > 0:
                prices[sym] = float(px)
        moves: list[dict] = []
        for e in exits:
            cur = prices.get(e["symbol"])
            if cur is None:
                continue
            moves.append({
                "symbol": e["symbol"], "date": e["date"],
                "move_pct": round((cur - e["exit_px"]) / e["exit_px"] * 100, 1),
            })
        if not moves:
            return None
        moves.sort(key=lambda m: -m["move_pct"])
        return {
            "n": len(moves),
            "n_higher_5pct": sum(1 for m in moves if m["move_pct"] >= 5.0),
            "avg_move_pct": round(sum(m["move_pct"] for m in moves) / len(moves), 1),
            "worst": moves[:3],
        }

    def _build_recent_missed_lessons(self, lookback_days: int = 14) -> str:
        """PM L3d memory: themes that evening flagged ≥ 2 times as missed.

        Reads `insights.missed_opportunities_json` for the last N days, skips
        the two "not-really-a-miss" categories (noise_rally, risk_disciplined),
        groups by `theme_if_any` (falling back to `symbol` when no theme
        tagged), keeps themes seen on 2+ distinct dates. Output is prose
        PM renders directly — the whole point of this memory layer is PM
        sees "nuclear/power keeps showing up — am I blind to it?" before
        deciding today's positions.

        Empty string when there's nothing worth surfacing — PM's L3d section
        then shows a default "no recurring missed themes" note.
        """
        import json as _json
        try:
            rows = self.db.get_recent_insights(limit=lookback_days + 5)
        except Exception as e:
            logger.warning("recent_missed_lessons: insights fetch failed: %s", e)
            return ""
        if not rows:
            return ""
        # RC4 (2026-07-16): value_entry_missed IS a real, actionable miss —
        # it's the category evening uses for "we identified the entry and
        # didn't take it" (SNDK was flagged 16×, ORCL 7×, and PM never saw
        # any of it because this set filtered them out). Only the two
        # "not-really-a-miss" categories stay excluded.
        real_miss_cats = {
            "trend_timing_miss", "theme_blindspot", "fundamentals_mispricing",
            "value_entry_missed",
        }
        theme_dates: dict[str, set[str]] = {}
        theme_symbols: dict[str, list[str]] = {}
        theme_lessons: dict[str, str] = {}  # most recent lesson text per theme
        for row in rows[:lookback_days]:
            row_date = row.get("date") or ""
            raw = row.get("missed_opportunities_json")
            if not raw:
                continue
            try:
                items = _json.loads(raw)
            except (TypeError, ValueError) as e:
                # L3d aggregates 14d of missed themes for PM. A single
                # corrupt insights row used to vanish silently from PM's
                # view; surfaces it so a recurring DB corruption pattern
                # is visible in logs instead of the layer just looking
                # "empty" some days.
                logger.warning(
                    "recent_missed_lessons: JSON parse failed for "
                    "insights row %s: %s",
                    row_date or "?", e,
                )
                continue
            if not isinstance(items, list):
                continue
            for m in items:
                if not isinstance(m, dict):
                    continue
                cat = m.get("miss_category")
                if cat not in real_miss_cats:
                    continue
                theme = (m.get("theme_if_any") or "").strip()
                sym = (m.get("symbol") or "").strip().upper()
                # DUAL grouping keys. RC4 (2026-07-16): theme_if_any is LLM
                # free text that almost never repeats verbatim (45 distinct
                # themes, 0 recurring in the audit window) — keyed ONLY by
                # theme, a symbol missed 16 times (SNDK) diluted into 16
                # one-off "themes" and PM was shown "(no recurring missed
                # themes)" every run. Symbol-keyed counting fixes that;
                # theme-keyed counting is KEPT because cross-symbol theme
                # recurrence (VST + OKLO both "nuclear/power") is a real,
                # distinct signal a symbol key can't see.
                keys = set()
                if sym:
                    keys.add(f"sym:{sym}")
                if theme:
                    keys.add(theme)
                if not keys:
                    continue
                for key in keys:
                    theme_dates.setdefault(key, set()).add(row_date)
                    theme_symbols.setdefault(key, []).append(sym)
                    # Rows are newest-first; first lesson seen is freshest.
                    if key not in theme_lessons:
                        lesson = (m.get("lesson") or "").strip()
                        if lesson:
                            theme_lessons[key] = lesson[:200]
        # Keep themes seen in ≥ 2 distinct EPISODES (audit round 2): the
        # missed-ops digest uses a rolling 5-session window, so one big
        # single-day move re-emits the same miss on ~5 consecutive evenings —
        # "≥2 distinct dates" was auto-satisfied by every one-off spike.
        # Dates within 5 days of the previous date collapse into one episode.
        def _episodes(dates: set[str]) -> int:
            from datetime import date as _d
            parsed = sorted(
                _d.fromisoformat(x) for x in dates
                if isinstance(x, str) and len(x) >= 10
            ) if dates else []
            if not parsed:
                return 0
            n = 1
            for a, b in zip(parsed, parsed[1:]):
                if (b - a).days > 5:
                    n += 1
            return n

        # Recurring = ≥2 separated episodes OR ≥2 distinct symbols. The
        # symbol arm keeps the genuine cross-symbol theme case (VST + OKLO
        # both flagged "nuclear/power" on adjacent days = one market episode
        # but a REAL breadth signal), which pure episode-counting would drop.
        recurring = [
            (k, max(_episodes(theme_dates[k]),
                    len({x for x in theme_symbols.get(k, []) if x})))
            for k in theme_dates
            if (_episodes(theme_dates[k]) >= 2
                or len({x for x in theme_symbols.get(k, []) if x}) >= 2)
        ]
        if not recurring:
            return ""
        # Sort by occurrence count desc, then key alpha for determinism.
        recurring.sort(key=lambda x: (-x[1], x[0]))
        lines: list[str] = []
        for key, n_days in recurring[:5]:
            syms = theme_symbols.get(key, [])
            uniq = sorted(set(syms))
            sym_tally = ", ".join(
                f"{s}×{syms.count(s)}" if syms.count(s) > 1 else s
                for s in uniq[:6]
            )
            lesson = theme_lessons.get(key, "")
            label = key[4:] if key.startswith("sym:") else key
            line = f"- {label}: {n_days} days (symbols: {sym_tally})"
            if lesson:
                line += f' — latest lesson: "{lesson}"'
            lines.append(line)
        return "\n".join(lines)

    def _persist_evening_replay_inputs(
        self,
        *,
        date_iso: str,
        run_id: str,
        positions,
        macro_summary: dict,
        total_value: float,
        daily_pnl: float,
        daily_return_pct: float,
        today_trades: list,
        prior_outlook,
        recent_sells: list,
        recent_buys: list,
        news_intel,
        earnings_analyses: list,
        weekly_narrative: str,
        active_state_changes: str,
        outlook_calibration: dict,
        missed_ops_snapshots: list,
        thesis_health_context: dict,
        root_dir: str = "data/evening_replays",
    ) -> Path:
        """Freeze the full evening-analyst input set as JSON so a candidate
        prompt can be re-scored on the same inputs weeks later.

        Pydantic objects (Position, NewsIntelligenceReport, MissedOpportunity
        Snapshot) are serialized via model_dump; the replay script reverses
        it. Plain dicts/strings pass through untouched. Writes atomically to
        data/evening_replays/YYYY-MM-DD.json. Caller treats the whole call
        as best-effort — a disk full or permission issue on the replay dir
        should NOT break the live evening run.
        """
        from pathlib import Path as _Path
        import json as _json
        import os as _os

        def _dump(obj):
            """Recursively convert Pydantic → dict; leave plain JSON types."""
            if obj is None or isinstance(obj, (bool, int, float, str)):
                return obj
            if hasattr(obj, "model_dump"):
                return obj.model_dump(mode="json")
            if isinstance(obj, list):
                return [_dump(x) for x in obj]
            if isinstance(obj, tuple):
                return [_dump(x) for x in obj]
            if isinstance(obj, dict):
                return {str(k): _dump(v) for k, v in obj.items()}
            # Fall-through: stringify — better than crashing the persist.
            return str(obj)

        payload = {
            "schema_version": 1,
            "date": date_iso,
            "run_id": run_id,
            "kwargs": {
                "positions": [_dump(p) for p in (positions or [])],
                "macro_summary": _dump(macro_summary),
                "total_value": total_value,
                "daily_pnl": daily_pnl,
                "daily_return_pct": daily_return_pct,
                "today_trades": _dump(today_trades),
                "prior_outlook": _dump(prior_outlook),
                "recent_sells": _dump(recent_sells),
                "recent_buys": _dump(recent_buys),
                "news_intel": _dump(news_intel),
                "earnings_analyses": _dump(earnings_analyses),
                "weekly_narrative": weekly_narrative,
                "active_state_changes": active_state_changes,
                "outlook_calibration": _dump(outlook_calibration),
                "missed_ops_snapshots": [_dump(s) for s in (missed_ops_snapshots or [])],
                "thesis_health_context": _dump(thesis_health_context),
            },
        }

        out_dir = _Path(root_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f"{date_iso}.json"
        tmp = out_path.with_suffix(".json.tmp")
        tmp.write_text(_json.dumps(payload, indent=2, ensure_ascii=False))
        _os.replace(str(tmp), str(out_path))
        logger.info("Evening replay inputs frozen → %s", out_path)
        return out_path

    def _build_thesis_health_context(
        self,
        positions,
        lookback_weeks: int = 8,
    ) -> dict[str, dict]:
        """Per-position fundamental-evolution snapshot for the evening
        thesis_health_review step.

        For each held symbol, gather:
          - Entry context (date, price, days_held, original thesis text)
          - Tech rating trajectory (last 4 ratings as a list)
          - News mentions count + 2 latest headlines (8-week window)
          - Most recent earnings sentiment + key_thesis
          - Current macro sector stance
          - Valuation snapshot (trailing PE / forward PE / P/S / signal)

        Shape designed so the evening LLM can answer
        "strengthening / intact / weakening / broken" per holding,
        not just aggregate-level "bullish / bearish". That step is
        what separates a swing-trader feedback bot from a value-
        investor strategic reflection.

        Returns {symbol: dict}. Empty dict when there are no positions.
        Exceptions during data fetch degrade gracefully — a missing
        field is None or [], the helper does not raise.
        """
        if not positions:
            return {}

        from datetime import timedelta
        lookback_days = lookback_weeks * 7
        tech_map_multi = self._thesis_tech_trajectory_map(lookback_days)
        news_events_map = self._thesis_news_events_map(lookback_days)
        earnings_map = self._missed_ops_earnings_signal()
        macro_map = self._missed_ops_macro_sector_map()

        out: dict[str, dict] = {}
        for p in positions:
            sym = p.symbol

            # Entry context
            entry_date: str | None = None
            entry_reasoning = ""
            days_held: int | None = None
            try:
                buy_row = self.db.get_symbol_last_buy(sym)
            except Exception:
                buy_row = None
            if buy_row:
                ts = (buy_row.get("timestamp") or "")[:10]
                if ts:
                    entry_date = ts
                    try:
                        from datetime import date as _d
                        entry_d = _d.fromisoformat(ts)
                        days_held = max(0, (et_today() - entry_d).days)
                    except (ValueError, TypeError):
                        days_held = None
                entry_reasoning = (buy_row.get("reasoning") or "")[:300]

            # P&L% — the one definition (`src.risk.metrics.unrealized_pnl_pct`),
            # which returns None when genuinely unknowable. The `cost > 0`
            # guard this replaces silently returned None for EVERY short
            # (a short's `avg_entry * qty` is negative), so evening's
            # thesis-health review saw no P&L on the short book at all.
            _pnl_pct = unrealized_pnl_pct(p)
            pnl_pct = None if _pnl_pct is None else round(_pnl_pct, 2)

            # Tech trajectory — last 4 ratings for this symbol
            tech_trajectory = tech_map_multi.get(sym, [])[:4]

            # News — total count in window + latest 2 headlines
            news_events = news_events_map.get(sym, [])
            news_count = len(news_events)
            latest_news_headlines = [e["event"] for e in news_events[:2]]

            # Sector stance
            sector = ""
            try:
                from src.execution.broker import _get_sector
                sector = _get_sector(sym) or ""
            except Exception:
                sector = ""
            macro_stance = macro_map.get(sector, "unknown") if sector else "unknown"

            # Valuation — bounded per-symbol yfinance call
            valuation = {
                "trailing_pe": None, "forward_pe": None, "ps_ratio": None,
            }
            try:
                v = self.market.get_valuation_metrics(sym) or {}
                valuation["trailing_pe"] = v.get("trailing_pe")
                valuation["forward_pe"] = v.get("forward_pe")
                valuation["ps_ratio"] = v.get("ps_ratio")
            except Exception:
                pass
            valuation["signal"] = _valuation_signal_from(valuation["forward_pe"])

            # Earnings deep-dive: full reasoning_chain + headline metrics
            # from the canonical analysis_*.md for this symbol. Only
            # surfaced for HELD positions (token-budget reasons); missed_ops
            # still use the 140-char snippet via earnings_map.
            from src.data.earnings_deep_dive import load_earnings_deep_dive
            deep_dive = None
            try:
                manifest = getattr(self.earnings_provider, "manifest", {}) or {}
                deep_dive = load_earnings_deep_dive(sym, manifest)
            except Exception as exc:
                logger.debug(
                    "thesis_health earnings deep-dive failed for %s: %s",
                    sym, exc,
                )

            out[sym] = {
                "symbol": sym,
                "entry_date": entry_date,
                "entry_reasoning": entry_reasoning,
                "days_held": days_held,
                "entry_price": p.avg_entry,
                "current_price": p.current_price,
                "pnl_pct": pnl_pct,
                "sector": sector,
                "tech_trajectory": tech_trajectory,
                "news_count_8w": news_count,
                "latest_news_headlines": latest_news_headlines,
                "recent_earnings_signal": earnings_map.get(sym),
                "earnings_deep_dive": deep_dive,
                "macro_sector_stance": macro_stance,
                "valuation": valuation,
            }
        return out

    def _thesis_tech_trajectory_map(
        self, lookback_days: int,
    ) -> dict[str, list[str]]:
        """For each symbol, extract chronological tech ratings from the last
        `lookback_days` of tech_analyst logs. Returns {sym: ["buy","hold",
        "buy","strong_buy"]} newest-first. Uses the same shape-normalizer
        as the missed_ops digest so bare-list / dict-wrapped / symbol-keyed
        shapes all work. Empty dict on failure."""
        from src.evolution.quarterly_digest import _tech_analyses_from_data
        try:
            rows = self.db.get_recent_agent_outputs(
                agent_name="tech_analyst",
                limit=lookback_days,
                before_date=None,
            )
        except Exception as exc:
            logger.warning("thesis_tech_trajectory: logs fetch failed: %s", exc)
            return {}
        by_sym: dict[str, list[str]] = {}
        for row in rows:
            data = self._parse_logged_agent_response(row)
            if data is None:
                continue
            for a in _tech_analyses_from_data(data):
                sym = (a.get("symbol") or "").upper()
                rating = a.get("rating")
                if sym and rating:
                    by_sym.setdefault(sym, []).append(str(rating))
        return by_sym

    def _thesis_news_events_map(
        self, lookback_days: int,
    ) -> dict[str, list[dict]]:
        """Per-symbol news events over the lookback window. Returns
        {sym: [{event, conviction, date}, ...]} newest-first.

        Walks dated full_report.json files. Every state_change with the
        symbol in affected_symbols is collected. Wider than the 5-day
        window _missed_ops_news_signal uses because the thesis health
        review needs to see the full 8-week arc, not just recent days.
        """
        import json as _json
        from datetime import timedelta
        from pathlib import Path
        news_dir = getattr(self.news_store, "data_dir", None)
        if news_dir is None:
            return {}
        out: dict[str, list[dict]] = {}
        today = et_today()
        for days_ago in range(lookback_days + 1):
            day = today - timedelta(days=days_ago)
            report_path = Path(news_dir) / str(day) / "full_report.json"
            if not report_path.exists():
                continue
            try:
                report = _json.loads(report_path.read_text())
            except (_json.JSONDecodeError, OSError):
                continue
            for ch in report.get("state_changes", []) or []:
                event = (ch.get("event") or "").strip()
                if not event:
                    continue
                affected = ch.get("affected_symbols", []) or []
                conviction = (ch.get("conviction") or "").lower()
                for sym in affected:
                    sym_u = str(sym).upper()
                    if not sym_u:
                        continue
                    out.setdefault(sym_u, []).append({
                        "event": event[:140],
                        "conviction": conviction,
                        "date": str(day),
                    })
        return out

    def _build_watchlist_candidates(
        self, lookback_days: int = 30,
    ) -> list[dict]:
        """Symbols the evening analyst has repeatedly flagged as "add" or
        "watch" to the trading universe — the surface the user reviews
        when deciding whether to actually expand the 77-symbol universe.

        Reads `insights.missed_opportunities_json` for the last N days,
        filters entries with `universe_addition_recommendation != "no"`,
        aggregates by symbol.

        Returns a sorted list of dicts:
          [
            {
              "symbol": "VST",
              "add_count": int,
              "watch_count": int,
              "total_flags": int,
              "dates": [ISO date, ...],   # newest first
              "themes": [str, ...],        # distinct theme_if_any seen
              "latest_reason": str,        # most recent universe_addition_reason
              "latest_miss_category": str, # e.g. "theme_blindspot"
            },
            ...
          ]

        Sort: (add_count desc, watch_count desc, total_flags desc, symbol).
        One "add" carries more weight than one "watch" — an "add" means
        the LLM cleared ALL four quality bars (volume + sustain + theme
        + fundamentals), a "watch" means most-but-not-all.

        THIS FUNCTION DOES NOT MODIFY THE UNIVERSE. Universe expansion
        is a human decision — edit config/settings.yaml manually after
        reviewing this output. By design, so that the system can't
        casually grow the curated list.

        The aggregation itself is a pure function
        (`src.watchlist_candidates.build_watchlist_candidates`) — this
        method is now a thin fetch-then-aggregate wrapper so
        `src/api/db_reads.py` can compute the identical output from its own
        read-only `insights` query without importing `TradingPipeline`
        (Stage 2 Checkpoint C).
        """
        try:
            rows = self.db.get_recent_insights(limit=lookback_days + 5)
        except Exception as exc:
            logger.warning(
                "watchlist_candidates: insights fetch failed: %s", exc,
            )
            return []
        if not rows:
            return []
        from src.watchlist_candidates import build_watchlist_candidates
        return build_watchlist_candidates(rows, lookback_days)

    def _build_recent_loss_pits(self, lookback_days: int = 14) -> str:
        """PM L3f memory: repeat failure modes from losing BUYs.

        Reads `insights.buy_grades_json` for the last N days, pulls entries
        with `grade="wrong"` and a non-null `loss_root_cause`, groups by
        cause, keeps causes occurring ≥ 2 times. Output is prose PM renders
        directly — lets it see "greed_top_chasing × 3 over 14 days"
        BEFORE deciding today's sizing, not after another wrong entry.

        Empty string when no repeat pattern — PM's L3f section then shows
        a default "no recurring pits" note.
        """
        import json as _json
        try:
            rows = self.db.get_recent_insights(limit=lookback_days + 5)
        except Exception as e:
            logger.warning("recent_loss_pits: insights fetch failed: %s", e)
            return ""
        if not rows:
            return ""
        cause_symbols: dict[str, list[str]] = {}
        cause_move: dict[str, list[float]] = {}
        cause_refs: dict[str, list[str]] = {}
        for row in rows[:lookback_days]:
            raw = row.get("buy_grades_json")
            if not raw:
                continue
            try:
                items = _json.loads(raw)
            except (TypeError, ValueError) as e:
                # L3f aggregates 14d of loss-root-cause patterns. Same
                # silent-drop rationale as L3d above.
                logger.warning(
                    "recent_loss_pits: JSON parse failed for insights "
                    "row %s: %s",
                    (row.get("date") or "?"), e,
                )
                continue
            if not isinstance(items, list):
                continue
            for g in items:
                if not isinstance(g, dict):
                    continue
                if g.get("grade") != "wrong":
                    continue
                cause = (g.get("loss_root_cause") or "").strip()
                if not cause:
                    continue
                sym = (g.get("symbol") or "").strip().upper()
                move = g.get("pct_move_since_buy")
                ref = (g.get("missed_warning_ref") or "").strip()
                if sym:
                    cause_symbols.setdefault(cause, []).append(sym)
                if isinstance(move, (int, float)):
                    cause_move.setdefault(cause, []).append(float(move))
                if ref:
                    cause_refs.setdefault(cause, []).append(ref[:100])
        repeats = [(c, len(cause_symbols.get(c, []))) for c in cause_symbols
                   if len(cause_symbols.get(c, [])) >= 2]
        if not repeats:
            return ""
        repeats.sort(key=lambda x: (-x[1], x[0]))
        lines: list[str] = []
        for cause, n in repeats[:4]:
            syms = cause_symbols[cause]
            moves = cause_move.get(cause, [])
            detail_bits: list[str] = []
            for i, s in enumerate(syms[:4]):
                m = moves[i] if i < len(moves) else None
                detail_bits.append(f"{s} ({m:+.1f}%)" if m is not None else s)
            line = f"- {cause} × {n}: {', '.join(detail_bits)}"
            refs = cause_refs.get(cause, [])
            if refs and cause == "macro_warning_ignored":
                line += f' — ignored: "{refs[0]}"'
            lines.append(line)
        return "\n".join(lines)

    def _build_blocked_proposals(
        self,
        lookback_days: int = 21,
        min_proposals: int = 3,
        max_lines: int = 5,
    ) -> str:
        """PM memory: names it keeps asking for and never gets, and why.

        Every other per-symbol memory PM reads (loss pits, missed lessons,
        position history, R-multiples) is keyed on a POSITION, so a symbol
        that never became a position is invisible to all of them — however
        many times PM proposed it. This is the only section that can see a
        block, and a block is the cleanest feedback the desk produces: it
        arrives with its cause attached, where a filled trade's loss is
        confounded by whatever the market did next.

        Computed at prompt-build time from existing tables — no schema
        change. `specialist_evidence` marks each stage of a proposal's life
        and `decision_id` joins it to `trades`:

            target → proposed_order → verdict → execution_skip | trades.fill

        A `target` is one proposal. Targets sized to zero are EXIT
        instructions, not requests to get in, so they are excluded — a
        blocked exit is a different defect and counting it here would
        overstate the entry-side block rate.

        Every blocking reason is copied VERBATIM out of stored data —
        `execution_skip.reason` (`qty_zero`, `geometry_unmeasurable`,
        historical `geometry_rr`, `insufficient_cash`), `verdict.reason_category` (`rr_fail`, …),
        `trades.fill_status` (`canceled`, …) — so this section and the
        RM-verdict section name the same failure the same way. Exactly three
        tokens are ours: `rm_zeroed`, `order_not_placed` and
        `no_order_built`. Each describes an ABSENCE, which no table records:
        nothing was written, so nothing can be quoted. They are kept
        distinct because "the order was never built" and "the order was
        built and never placed" are different halves of the machinery.

        Conversion is judged on any `filled` trade sharing the proposal's
        `decision_id`. Today only entry orders carry a `decision_id`, so
        that is exact; if exits ever carry one, this biases toward calling a
        proposal converted, which makes the section quieter rather than
        making it cry wolf.

        Diagnostic only. Nothing here gates, filters or caps anything.
        That is final, not interim: a count-based re-proposal gate was
        ANSWERED NO on 2026-09-14 (docs/WORK.md item 10(b)) because the
        conversion rate measures this desk's own gates and plumbing, not
        the instrument. Do not add one.

        Returns "" when the window holds no proposals at all — PM's section
        then shows its own "no proposals on record" default. When there are
        proposals but no repeat offender, the aggregate line still renders
        with an explicit "none" so the desk can never mistake a quiet
        section for a missing one.
        """
        import json as _json
        from datetime import timedelta
        try:
            since = (et_today() - timedelta(days=lookback_days)).isoformat()
            raw = self.db.get_proposal_funnel_rows(since)
        except Exception as e:
            logger.warning("blocked_proposals: DB fetch failed: %s", e)
            return ""

        proposals: list[tuple[str, str, str]] = []   # (ts, decision_id, symbol)
        ordered: set[tuple[str, str]] = set()        # (decision_id, symbol)
        skips: dict[tuple[str, str], str] = {}       # → verbatim reason
        verdicts: dict[str, dict] = {}               # decision_id → verdict
        constructor_drops: dict[tuple[str, str], str] = {}  # → constructor's own reason
        data_faults: dict[tuple[str, str], str] = {}        # → FAULT_* code (unmeasurable)
        constructor_refusals: dict[tuple[str, str], str] = {}  # → "constructor_refused:<code>"
        for row in raw.get("evidence") or []:
            kind = row.get("kind")
            did = row.get("decision_id")
            if not did:
                continue
            try:
                data = _json.loads(row.get("evidence_json") or "{}")
            except (TypeError, ValueError) as e:
                # One unparseable row must not blank the whole section, but a
                # silent drop hides a proposal PM did make. Same discipline as
                # the L3d/L3f builders above.
                logger.warning(
                    "blocked_proposals: JSON parse failed for %s row %s: %s",
                    kind, (row.get("timestamp") or "?"), e,
                )
                continue
            if not isinstance(data, dict):
                continue
            if kind == "verdict":
                verdicts[did] = data
                continue
            sym = (row.get("symbol") or data.get("symbol") or "").strip().upper()
            if not sym:
                continue
            if kind == "target":
                # `risk_allocation_pct` is the live field; `target_weight_pct`
                # is the legacy one older rows carry. Either can size a
                # target (see TargetPosition), so read whichever is present.
                size = data.get("risk_allocation_pct")
                if size is None:
                    size = data.get("target_weight_pct")
                try:
                    if size is None or float(size) <= 0.0:
                        continue        # an exit instruction, not a proposal
                except (TypeError, ValueError):
                    continue
                proposals.append((row.get("timestamp") or "", did, sym))
            elif kind == "proposed_order":
                ordered.add((did, sym))
            elif kind == "execution_skip":
                reason = (data.get("reason") or "").strip()
                if reason:
                    skips[(did, sym)] = reason
            elif kind == "pipeline_event":
                # The deterministic constructor's own reason for dropping a
                # target before it ever became a `proposed_order` row (see
                # `pipeline_stages.DecisionStage`, which persists this via
                # `PortfolioConstructor.last_drop_reasons`). Mirrors
                # `scripts/blocked_proposals_census.py::_load_recorded_reasons`
                # (renamed 2026-09-11 when that script started reading three
                # more durable reason kinds the same way — symbol_guard,
                # hard_risk, risk_manager_unparseable_output — this PM-facing
                # helper does NOT read those three yet, only
                # `constructor_dropped`)
                # — without it, a constructor drop falls through to the
                # generic `no_order_built` bucket below with no explanation,
                # even though the real reason was captured at drop time.
                if (data.get("stage") == "deterministic_gate"
                        and data.get("outcome") == "blocked"
                        and data.get("reason") == "constructor_dropped"):
                    constructor_drops[(did, sym)] = (
                        data.get("detail") or "constructor_dropped"
                    )
                elif (data.get("stage") == "deterministic_gate"
                        and data.get("outcome") == "unmeasurable"
                        and data.get("reason") == "data_fault"):
                    # 2026-09-12: a symbol the constructor could not
                    # MEASURE (no price / ATR / usable bars / analysis).
                    # Its own bucket — not `constructor_dropped`, which is
                    # for trades the desk judged. Mirrors
                    # `scripts/blocked_proposals_census.py`.
                    data_faults[(did, sym)] = str(data.get("fault") or "unknown")
                # 2026-09-12: a refusal the constructor recorded AS DATA
                # (`PortfolioConstructor.last_refusals`, filed by
                # `DecisionStage` under `constructor_refused` with the code
                # beside it — today `stop_wider_than_instrument_reach`
                # or `insufficient_history`, item 54). Kept apart from
                # the regex-recovered `constructor_dropped` so the digest
                # names the rule, not a sentence.
                elif (data.get("stage") == "deterministic_gate"
                        and data.get("outcome") == "blocked"
                        and data.get("reason") == "constructor_refused"):
                    constructor_refusals[(did, sym)] = (
                        f"constructor_refused:{data.get('refusal') or 'unknown'}"
                    )

        if not proposals:
            return ""

        fills: dict[tuple[str, str], str] = {}
        for row in raw.get("trades") or []:
            did = row.get("decision_id")
            sym = (row.get("symbol") or "").strip().upper()
            if not did or not sym:
                continue
            status = (row.get("fill_status") or "").strip().lower()
            if not status:
                continue
            # A decision can emit more than one order for a symbol (a retry, a
            # repeg). One fill converts the proposal, so a filled row wins
            # over any other status regardless of arrival order.
            if fills.get((did, sym)) == "filled":
                continue
            fills[(did, sym)] = status

        def _outcome(did: str, sym: str) -> str | None:
            """None == converted. Otherwise the verbatim blocking reason."""
            key = (did, sym)
            status = fills.get(key)
            if status == "filled":
                return None
            if status:
                return f"order_{status}"
            if key in skips:
                return skips[key]
            if key in data_faults:
                # Not a trade judgement: the desk could not measure the
                # symbol. Named by fault so a feed outage and a missing
                # analysis stay distinguishable in the digest.
                return f"data_fault:{data_faults[key]}"
            if key in constructor_refusals:
                # Same precedence as a constructor drop (the constructor
                # runs before the Risk Manager), but the CODE is the
                # category, so "no floor" aggregates under its own line.
                return constructor_refusals[key]
            if key in constructor_drops:
                # Checked before the verdict/`ordered` logic below, so a
                # symbol the deterministic constructor dropped before the
                # Risk Manager ever saw the plan is attributed to the
                # constructor, never to the RM's veto of whatever plan
                # survived. A fixed category (not the per-symbol detail
                # text) so this still aggregates in `top` below; the real
                # sentence lives in `constructor_drops[key]` for anyone
                # who wants it. Mirrors
                # `scripts/blocked_proposals_census.py::classify`.
                return "constructor_dropped"
            # A verdict rejection/zeroing is only attributed to a symbol
            # confirmed to have reached the constructor's own order list
            # (`ordered`). Without this guard every ORIGINALLY-proposed
            # symbol gets blamed for an AI Risk Manager veto — including
            # ones the deterministic constructor had already dropped
            # before the Risk Manager ever saw the plan. Mirrors
            # `scripts/blocked_proposals_census.py::classify`.
            verdict = verdicts.get(did)
            if isinstance(verdict, dict) and key in ordered:
                if verdict.get("approved") is False:
                    cat = (verdict.get("reason_category") or "").strip()
                    return f"rm_rejected:{cat}" if cat else "rm_rejected"
                for mod in (verdict.get("modifications") or []):
                    if not isinstance(mod, dict):
                        continue
                    if (mod.get("symbol") or "").strip().upper() != sym:
                        continue
                    try:
                        if float(mod.get("new_value")) == 0.0:
                            return "rm_zeroed"
                    except (TypeError, ValueError):
                        continue
            if key in ordered:
                return "order_not_placed"
            return "no_order_built"

        by_symbol: dict[str, list[tuple[str, str | None]]] = {}
        block_counts: dict[str, int] = {}
        converted = 0
        for ts, did, sym in proposals:
            reason = _outcome(did, sym)
            by_symbol.setdefault(sym, []).append((ts, reason))
            if reason is None:
                converted += 1
            else:
                block_counts[reason] = block_counts.get(reason, 0) + 1

        total = len(proposals)
        pct = (100.0 * converted / total) if total else 0.0
        top = sorted(block_counts.items(), key=lambda kv: (-kv[1], kv[0]))[:3]
        lines = [
            f"Conversion: {converted} of {total} proposals reached a fill "
            f"({pct:.0f}%) in the last {lookback_days} days.",
        ]
        if top:
            lines.append(
                "Top blocks: "
                + ", ".join(f"{reason} × {n}" for reason, n in top)
                + "."
            )

        repeats = [
            (sym, rows) for sym, rows in by_symbol.items()
            if len(rows) >= min_proposals
            and all(reason is not None for _, reason in rows)
        ]
        if not repeats:
            lines.append(
                f"Repeat blocked names: none — no symbol was proposed "
                f"{min_proposals}+ times without a fill in this window."
            )
            return "\n".join(lines)

        repeats.sort(key=lambda item: (-len(item[1]), item[0]))
        lines.append(
            f"Repeat blocked names ({min_proposals}+ proposals, 0 fills):"
        )
        for sym, rows in repeats[:max_lines]:
            rows = sorted(rows, key=lambda r: r[0], reverse=True)  # newest first
            sessions = len({ts[:10] for ts, _ in rows if ts})
            recent = ", ".join(str(reason) for _, reason in rows[:3])
            lines.append(
                f"- {sym}: proposed {len(rows)}× across {sessions} sessions, "
                f"filled 0 — most recent first: {recent}"
            )
        return "\n".join(lines)

    def _build_missed_opportunities_digest(
        self,
        lookback_days: int = 5,
        move_threshold_pct: float = 8.0,
        top_n: int = 15,
        top_movers_count: int = 15,
        current_position_symbols: set[str] | None = None,
        min_top_mover_dollar_volume_m: float = 5.0,
    ) -> list:
        """Notable movers we did NOT own — input for evening's missed-op review.

        Symbol set = trading universe ∪ Alpaca top gainers. For each, compute
        the `lookback_days` window return; keep those crossing
        `move_threshold_pct` (absolute). Tag each with the signal state that
        was visible at the time (prior TA rating, news headline, earnings
        sentiment, macro sector stance) so the LLM's miss classification has
        to cite observable evidence, not retro-rationalize price.

        Quality filter for TOP-MOVER symbols only (universe symbols always
        pass — they're curated): if 20-day avg dollar volume is below
        `min_top_mover_dollar_volume_m` (default $5M), the symbol is
        dropped before reaching the LLM. Thin-liquidity gappers aren't
        interesting to a medium-long-term investor and flooding the prompt
        with them dilutes the real misses.

        Returns a list[MissedOpportunitySnapshot]. Empty when no symbol
        crosses the threshold. Sort order within the list:
          (a) not-held, has prior signal — real "we saw it, didn't act" misses
          (b) not-held, no prior signal — theme-coverage blindspots
          (c) already held — context for decision-quality review
        Within each group by |move_pct| descending. Top `top_n` only.
        """
        from src.models import MissedOpportunitySnapshot

        universe = list(getattr(self.config.trading, "universe", []) or [])
        universe_set = {s.upper() for s in universe if s}
        try:
            top_movers = self.broker.get_top_movers(n=top_movers_count) or []
        except Exception as exc:
            logger.warning("missed_ops: get_top_movers failed: %s", exc)
            top_movers = []
        top_mover_syms = {
            str(m["symbol"]).upper() for m in top_movers
            if isinstance(m, dict) and m.get("symbol")
        }
        all_syms = universe_set | top_mover_syms
        if not all_syms:
            return []

        # Fetch bars once per symbol. Cache for reuse across move + quality
        # metric computation. Need ≥ 25 bars for a 20-day average volume
        # calculation, so we pad to that even if lookback_days is tight.
        bars_pad = max(lookback_days + 3, 25)
        bars_cache: dict[str, list] = {}
        for sym in all_syms:
            try:
                bars = self.market.get_ohlcv(sym, lookback_days=bars_pad)
            except Exception:
                continue
            if bars and len(bars) >= 2:
                bars_cache[sym] = bars

        # Per-symbol window return.
        symbol_moves: dict[str, float] = {}
        for sym, bars in bars_cache.items():
            window = bars[-(lookback_days + 1):] if len(bars) > lookback_days else bars
            if len(window) < 2:
                continue
            start_close = getattr(window[0], "close", 0) or 0
            end_close = getattr(window[-1], "close", 0) or 0
            if start_close <= 0:
                continue
            move_pct = (end_close - start_close) / start_close * 100.0
            symbol_moves[sym] = round(move_pct, 2)

        candidates = {
            s: m for s, m in symbol_moves.items()
            if abs(m) >= move_threshold_pct
        }
        if not candidates:
            return []

        # Pre-compute signal maps once (not per-symbol): cheap vs. re-running
        # DB/file scans inside the loop.
        held_set = self._missed_ops_held_set(
            lookback_days, current_position_symbols or set()
        )
        tech_map = self._missed_ops_tech_signal(lookback_days)
        news_map = self._missed_ops_news_signal(lookback_days)
        theme_map = self._missed_ops_theme_tags(lookback_days)
        earnings_map = self._missed_ops_earnings_signal()
        macro_sector_map = self._missed_ops_macro_sector_map()

        snapshots: list = []
        for sym, move_pct in candidates.items():
            if sym in universe_set and sym in top_mover_syms:
                source = "both"
            elif sym in top_mover_syms:
                source = "top_mover"
            else:
                source = "universe"

            bars = bars_cache.get(sym) or []
            avg_dvol_m, vol_conf_ratio, single_day_conc = _missed_ops_quality_metrics(
                bars, lookback_days,
            )

            # Liquidity pre-filter: thin TOP-MOVER-only symbols drop out here.
            # Universe symbols bypass — they're already curated for quality.
            if (source == "top_mover"
                    and avg_dvol_m is not None
                    and avg_dvol_m < min_top_mover_dollar_volume_m):
                logger.debug(
                    "missed_ops: dropping thin top-mover %s (avg $vol %.1fM < %.1fM)",
                    sym, avg_dvol_m, min_top_mover_dollar_volume_m,
                )
                continue

            ta_rating, ta_date = tech_map.get(sym, (None, None))
            had_ta = ta_rating in ("buy", "strong_buy")
            news_headline = news_map.get(sym)
            earnings_signal = earnings_map.get(sym)

            sector_stance = "unknown"
            try:
                from src.execution.broker import _get_sector
                sector = _get_sector(sym) or ""
            except Exception:
                sector = ""
            if sector and sector in macro_sector_map:
                sector_stance = macro_sector_map[sector]

            # Valuation (done per-candidate after threshold filter → only
            # ~5-15 yfinance calls, not 90+). Defaults to all-None on
            # error / ETF / data gap.
            trailing_pe = None
            forward_pe = None
            ps_ratio = None
            try:
                val_info = self.market.get_valuation_metrics(sym) or {}
                trailing_pe = val_info.get("trailing_pe")
                forward_pe = val_info.get("forward_pe")
                ps_ratio = val_info.get("ps_ratio")
            except Exception as exc:
                logger.debug(
                    "missed_ops valuation fetch failed for %s: %s", sym, exc,
                )
            valuation_signal = _valuation_signal_from(forward_pe)

            # Bidirectional opportunity framing: a DOWN move with an
            # intact fundamental signal is the classic value-dip the
            # medium-long-term investor wants to catch. Flag it at the
            # snapshot level so the evening LLM's value_entry_missed
            # classification is grounded, not just vibes.
            has_fundamental_signal = (
                news_headline is not None or earnings_signal is not None
            )
            value_entry_candidate = (
                move_pct <= -8.0 and has_fundamental_signal
            )

            snapshots.append(MissedOpportunitySnapshot(
                symbol=sym,
                move_pct=move_pct,
                window_days=lookback_days,
                held_during_window=(sym in held_set),
                had_ta_signal=had_ta,
                had_news_signal=(news_headline is not None),
                had_earnings_signal=(earnings_signal is not None),
                source=source,
                last_ta_rating=ta_rating,
                last_ta_date=ta_date,
                last_news_headline=news_headline,
                theme_tags=theme_map.get(sym, [])[:4],
                recent_earnings_signal=earnings_signal,
                macro_sector_tailwind=sector_stance,  # type: ignore[arg-type]
                avg_dollar_volume_20d_m=avg_dvol_m,
                volume_confirmation_ratio=vol_conf_ratio,
                single_day_concentration_pct=single_day_conc,
                trailing_pe=trailing_pe,
                forward_pe=forward_pe,
                ps_ratio=ps_ratio,
                valuation_signal=valuation_signal,  # type: ignore[arg-type]
                value_entry_candidate=value_entry_candidate,
            ))

        # Drop names we actually held during the window before sorting and
        # truncating. The prompt instructs the LLM to recognize HELD rows
        # and not classify them as "missed", but the LLM-only fence is
        # fragile: a hiccup could let evening emit `value_entry_missed`
        # on a name we literally bought today (held_during_window=True).
        # Pre-filter in Python so even a confused LLM can't surface a
        # held name. Held positions still get full coverage via
        # thesis_health_review — they don't need a "missed" entry.
        snapshots = [s for s in snapshots if not s.held_during_window]

        def _priority_key(s) -> tuple:
            any_signal = s.had_ta_signal or s.had_news_signal or s.had_earnings_signal
            group = 0 if any_signal else 1
            return (group, -abs(s.move_pct))

        snapshots.sort(key=_priority_key)
        return snapshots[:top_n]

    def _missed_ops_held_set(
        self, lookback_days: int, current_position_symbols: set[str]
    ) -> set[str]:
        """Symbols we owned (or traded) within the window.

        Union of (a) symbols currently open in ctx.positions and (b) symbols
        with any executed trade in the last ~2×`lookback_days` calendar days
        (accounts for weekends / holidays). Over-inclusive on purpose — better
        to NOT flag a legitimate hold as "missed" than invent a miss from a
        stale SELL earlier in the week.
        """
        from datetime import timedelta
        held: set[str] = {s.upper() for s in current_position_symbols if s}
        try:
            rows = self.db.get_trades(limit=500, executed_only=True)
        except Exception as exc:
            logger.warning("missed_ops: get_trades failed: %s", exc)
            return held
        cutoff = et_today() - timedelta(days=lookback_days * 2 + 2)
        cutoff_str = cutoff.isoformat()
        for r in rows:
            ts_date = (r.get("timestamp") or "")[:10]
            if not ts_date or ts_date < cutoff_str:
                continue
            sym = (r.get("symbol") or "").upper()
            if sym:
                held.add(sym)
        return held

    def _missed_ops_tech_signal(
        self, lookback_days: int
    ) -> dict[str, tuple[str, str]]:
        """Most recent TA rating per symbol in window → {symbol: (rating, date)}.

        Walks recent tech_analyst agent_logs, parses the batch-output JSON,
        takes the newest rating per symbol. `rating in ("buy","strong_buy")`
        is what drives the `had_ta_signal` flag downstream.

        Production tech_analyst emits two different JSON shapes depending on
        which code path wrote the log — either ``{"analyses": [...]}`` or a
        BARE LIST of per-symbol dicts. We delegate shape normalization to
        `quarterly_digest._tech_analyses_from_data` so both paths stay in
        sync — adding a third shape should only require editing that helper.
        """
        from datetime import timedelta
        from src.evolution.quarterly_digest import _tech_analyses_from_data
        try:
            rows = self.db.get_recent_agent_outputs(
                agent_name="tech_analyst", limit=lookback_days * 3,
                before_date=None,
            )
        except Exception as exc:
            logger.warning("missed_ops: tech_analyst logs fetch failed: %s", exc)
            return {}
        cutoff_str = (et_today() - timedelta(days=lookback_days * 2 + 2)).isoformat()
        latest: dict[str, tuple[str, str]] = {}
        for row in rows:
            ts_date = (row.get("timestamp") or "")[:10]
            if not ts_date or ts_date < cutoff_str:
                continue
            data = self._parse_logged_agent_response(row)
            if data is None:
                continue
            for a in _tech_analyses_from_data(data):
                sym = (a.get("symbol") or "").upper()
                rating = a.get("rating")
                if not sym or not rating:
                    continue
                if sym not in latest:  # newer rows first from get_recent_agent_outputs
                    latest[sym] = (str(rating), ts_date)
        return latest

    def _missed_ops_news_signal(self, lookback_days: int) -> dict[str, str]:
        """Most recent news headline touching each symbol in window.

        Walks dated full_report.json files. For state_changes, harvests
        (event-text, affected_symbols) pairs. For stock_news, takes the first
        alert's headline. Newest day wins. Headlines clipped to 140 chars so
        they don't blow the prompt budget.
        """
        import json as _json
        from datetime import timedelta
        from pathlib import Path
        news_dir = getattr(self.news_store, "data_dir", None)
        if news_dir is None:
            return {}
        out: dict[str, str] = {}
        today = et_today()
        # Iterate newest → oldest so first-seen wins (freshest headline per symbol).
        for days_ago in range(lookback_days + 1):
            day = today - timedelta(days=days_ago)
            report_path = Path(news_dir) / str(day) / "full_report.json"
            if not report_path.exists():
                continue
            try:
                report = _json.loads(report_path.read_text())
            except (_json.JSONDecodeError, OSError):
                continue
            for ch in report.get("state_changes", []) or []:
                event = (ch.get("event") or "").strip()
                if not event:
                    continue
                for sym in ch.get("affected_symbols", []) or []:
                    sym_u = str(sym).upper()
                    if sym_u and sym_u not in out:
                        out[sym_u] = event[:140]
            for sym, items in (report.get("stock_news") or {}).items():
                sym_u = str(sym).upper()
                if sym_u in out or not items:
                    continue
                first = items[0] if isinstance(items, list) else None
                if isinstance(first, dict):
                    headline = (first.get("headline") or "").strip()
                    if headline:
                        out[sym_u] = headline[:140]
        return out

    def _missed_ops_theme_tags(self, lookback_days: int) -> dict[str, list[str]]:
        """Rough theme proxies per symbol from recent state_change event text.

        Extracts the first 1-2 meaningful tokens from each event and tags the
        affected symbols with them. Not a semantic classifier — the LLM
        refines to one canonical theme name in `MissedOpportunity.theme_if_any`.
        Purpose here is surface pattern co-occurrence ("AVGO: ai-capex, compute")
        so the LLM can spot the theme instead of treating each headline
        in isolation.
        """
        import json as _json
        import re
        from datetime import timedelta
        from pathlib import Path
        news_dir = getattr(self.news_store, "data_dir", None)
        if news_dir is None:
            return {}
        out: dict[str, list[str]] = {}
        stopwords = {
            "this", "that", "with", "from", "into", "than", "will", "would",
            "should", "could", "about", "against", "between", "report",
        }
        today = et_today()
        for days_ago in range(lookback_days + 1):
            day = today - timedelta(days=days_ago)
            report_path = Path(news_dir) / str(day) / "full_report.json"
            if not report_path.exists():
                continue
            try:
                report = _json.loads(report_path.read_text())
            except (_json.JSONDecodeError, OSError):
                continue
            for ch in report.get("state_changes", []) or []:
                event = (ch.get("event") or "").strip()
                tokens = [
                    t.lower() for t in re.findall(r"[A-Za-z]{4,}", event)
                    if t.lower() not in stopwords
                ]
                if not tokens:
                    continue
                tag = "-".join(tokens[:2])
                for sym in ch.get("affected_symbols", []) or []:
                    sym_u = str(sym).upper()
                    if not sym_u:
                        continue
                    bucket = out.setdefault(sym_u, [])
                    if tag not in bucket and len(bucket) < 4:
                        bucket.append(tag)
        return out

    def _missed_ops_earnings_signal(self) -> dict[str, str]:
        """Most recent non-bearish earnings take per symbol from on-disk cache.

        Walks earnings_provider.manifest, skips abandoned entries, reads each
        analysis file's head (first 600 chars) and passes any entry whose
        head text contains no "bearish" token. Returns {symbol: snippet} where
        snippet is a clipped first-sentence-ish summary the LLM can cite as
        evidence for `fundamentals_mispricing` classification.
        """
        try:
            manifest = getattr(self.earnings_provider, "manifest", {}) or {}
        except Exception:
            return {}
        from datetime import date as _date
        from pathlib import Path

        # audit round 2, three fixes:
        # (a) newest filing PER SYMBOL — the old loop wrote raw manifest
        #     order, so an older 10-K could shadow this quarter's 10-Q;
        # (b) 90-day recency using the manifest's own filing_date — a stale
        #     analysis from months ago is not "recent earnings evidence";
        # (c) sentiment from the STRUCTURED "Sentiment:" line — the naive
        #     `"bearish" in head` substring dropped NEUTRAL analyses whose
        #     prose merely mentioned the word ("not bearish", "bearish
        #     scenarios considered").
        best: dict[str, tuple[str, dict]] = {}   # symbol -> (filing_date, entry)
        for key, entry in manifest.items():
            if not isinstance(entry, dict) or entry.get("abandoned"):
                continue
            symbol = str(key).split("_")[0].upper()
            fd = str(entry.get("filing_date") or "")
            if symbol not in best or fd > best[symbol][0]:
                best[symbol] = (fd, entry)

        out: dict[str, str] = {}
        today = et_today()
        for symbol, (fd, entry) in best.items():
            try:
                if not fd or (today - _date.fromisoformat(fd)).days > 90:
                    continue
            except ValueError:
                continue   # unparseable date = unknowable age = stale
            analysis_path = entry.get("analysis_path")
            if not analysis_path:
                continue
            p = Path(analysis_path)
            if not p.exists():
                continue
            try:
                text = p.read_text()
            except OSError:
                continue
            head = text[:600]
            m = re.search(r"^\s*-?\s*\*{0,2}Sentiment\*{0,2}\s*:\s*(\w+)",
                          head, re.MULTILINE | re.IGNORECASE)
            sentiment = (m.group(1).lower() if m else None)
            if sentiment == "bearish":
                continue
            if sentiment is None and "bearish" in head.lower():
                continue   # no structured line — keep the conservative fallback
            snippet = head.replace("\n", " ").strip()[:140]
            if snippet:
                out[symbol] = snippet
        return out

    def _missed_ops_macro_sector_map(self) -> dict[str, str]:
        """Latest macro sector stance: {sector: bullish|neutral|bearish}.

        Reads macro_store.load_last_state() — persisted at the end of each
        morning macro run. Missing keys / stances → empty dict, snapshot
        defaults to "unknown" for each symbol, which is itself a signal (if
        macro never covers a whole sector we rally through, that's a
        coverage blindspot the quarterly meta-reflector should notice).
        """
        try:
            state = self.macro_store.load_last_state() or {}
        except Exception as exc:
            logger.warning("missed_ops: macro_store load failed: %s", exc)
            return {}
        guidance = state.get("sector_guidance") or {}
        if not isinstance(guidance, dict):
            return {}
        out: dict[str, str] = {}
        for sector, stance in guidance.items():
            if (isinstance(stance, str)
                    and stance in ("bullish", "neutral", "bearish")):
                out[str(sector)] = stance
        return out

    @staticmethod
    def _actualize_trade_row(row: dict) -> dict:
        """Prefer broker-confirmed execution details when present."""
        out = dict(row)
        if out.get("fill_qty"):
            out["qty"] = float(out["fill_qty"])
        if out.get("fill_price"):
            out["price"] = float(out["fill_price"])
        return out

    @staticmethod
    def _build_macro_tech_alignment(
        macro_analysis: dict | None,
        analyses: list,
    ) -> str:
        """Advisory: does Macro's equity outlook match TA's rating distribution?

        Macro says 'bullish' but TA's ratings are majority bearish → market
        action is diverging from the macro call. That's a signal for PM to
        weight today's TA signals more carefully (market is often right
        about regime flips before FRED data catches up).

        Returns empty string when no divergence, or there's not enough data.
        """
        if not macro_analysis or not analyses:
            return ""
        # macro_analysis is MacroAnalysis (Pydantic) post-Phase-4-#7; dict path
        # still supported for defensive compatibility with legacy callers.
        if hasattr(macro_analysis, "equity_outlook"):
            outlook = (macro_analysis.equity_outlook or "").lower()
        else:
            outlook = (macro_analysis.get("equity_outlook") or "").lower()
        if outlook not in ("bullish", "bearish"):
            return ""
        bullish = sum(1 for a in analyses if a.rating in ("buy", "strong_buy"))
        bearish = sum(1 for a in analyses if a.rating in ("sell", "strong_sell"))
        total = len(analyses)
        if total < 5:
            return ""  # too small a sample to read a tape
        if outlook == "bullish" and bearish > bullish:
            return (
                f"DIVERGENCE: Macro `equity_outlook=bullish` but TA has more bearish "
                f"ratings ({bearish}) than bullish ({bullish}) across {total} symbols. "
                f"Market action may be leading the data — tread carefully on new BUYs "
                f"and respect TA's cautious signals."
            )
        if outlook == "bearish" and bullish > bearish:
            return (
                f"DIVERGENCE: Macro `equity_outlook=bearish` but TA has more bullish "
                f"ratings ({bullish}) than bearish ({bearish}) across {total} symbols. "
                f"Market may be pricing a turnaround before Macro data confirms — "
                f"don't ignore high-R/R long setups just because Macro is cautious."
            )
        return ""

    def _ensure_correlation_matrix(self, ctx, positions) -> dict:
        """Build the run's correlation matrix once, memoized on `ctx`.

        It used to be built inside `RiskStage`, which runs AFTER the Portfolio
        Manager has already chosen — so the PM's prompt could tell it to "avoid
        stacking highly correlated positions" while the only correlation data
        in the system was computed too late to inform that choice (audit §1.2).
        Building it here, from the DecisionStage side, lets PM see the clusters
        BEFORE it decides, and RiskStage reuses the same matrix rather than
        paying for a second one — the deterministic cluster check must judge
        PM against the numbers PM was actually shown.
        """
        cached = getattr(ctx, "correlation_matrix", None)
        if cached:
            return cached
        try:
            from src.data.correlation import build_correlation_matrix
            pool_bars = dict(ctx.symbols_bars)
            for p in positions:
                if p.symbol not in pool_bars:
                    pool_bars[p.symbol] = self.market.get_ohlcv(
                        p.symbol, self.config.trading.lookback_days,
                    ) or []
            matrix = build_correlation_matrix(pool_bars) or {}
        except Exception as e:  # noqa: BLE001
            logger.warning("Failed to build correlation matrix: %s (continuing without)", e)
            matrix = {}
        ctx.correlation_matrix = matrix
        return matrix

    def _build_stop_map(self, positions) -> tuple[dict[str, float], dict[str, float]]:
        """`(live_stops, initial_stops)` keyed by symbol.

        Live stops are broker truth (already trailed). Initial stops come from
        the last executed BUY row and are what an R-multiple's denominator must
        use — the bet that was actually made, not the one it was ratcheted to.
        A symbol missing from `live_stops` is genuinely unprotected and
        `portfolio_heat` charges it at full notional; never substitute the BUY
        row's stop for a missing broker stop, because that would report
        protection the account does not have.
        """
        live_stops: dict[str, float] = {}
        initial_stops: dict[str, float] = {}
        for p in positions:
            sym = p.symbol
            try:
                live = self.broker.get_current_stop_price(sym)
            except Exception as e:  # noqa: BLE001
                logger.warning("stop map: live stop lookup failed for %s: %s", sym, e)
                live = None
            if isinstance(live, (int, float)) and live > 0:
                live_stops[sym] = float(live)
            from src.execution.stop_records import recorded_initial_stop
            try:
                qty = float(getattr(p, "qty", 0) or 0)
            except (TypeError, ValueError):
                qty = 0.0
            try:
                opening = "SHORT" if qty < 0 else "BUY"
                buy = self.db.get_symbol_last_buy(sym, action=opening)
            except Exception as e:  # noqa: BLE001
                logger.warning("stop map: last-buy lookup failed for %s: %s", sym, e)
                buy = None
            initial = recorded_initial_stop(buy)
            if initial > 0:
                initial_stops[sym] = initial
        return live_stops, initial_stops

    def _build_portfolio_heat(self, positions, total_value: float):
        """Audit §1.3 — total capital at risk, which nothing computed before.

        The cash-equivalent sweep vehicle is excluded rather than counted as
        unprotected: it is deliberately stopless everywhere in this codebase
        and is not a risk position. Returns None on failure so the prompt can
        say "unknown" instead of rendering a confident zero.
        """
        from src.risk.metrics import portfolio_heat
        try:
            sweeper = self._sweeper()
            excluded = set()
            if sweeper is not None and sweeper.symbol:
                excluded.add(str(sweeper.symbol).upper())
            live_stops, initial_stops = self._build_stop_map(positions)
            return portfolio_heat(
                positions=positions,
                equity=total_value,
                stops=live_stops,
                initial_stops=initial_stops,
                exclude_symbols=excluded,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("portfolio heat build failed: %s", e)
            return None

    def _build_pm_facts(
        self,
        *,
        positions: list,
        analyses: list,
        total_value: float,
        cash: float,
        recent_performance: dict,
        macro_analysis=None,
        correlation_matrix: dict[str, dict[str, float]] | None = None,
    ) -> PMFacts:
        """Quantitative snapshot surfaced to PM as structured fields.

        Phase 4 #4: reduces PM's reliance on LLM-summarized prose for the
        things that are actually numbers (win rate, sector weights, age
        buckets). Prose layers (weekly_narrative, rm_recent_verdicts)
        stay for qualitative continuity.
        """
        import statistics
        from src.execution.broker import _get_sector as _sector_of

        f = PMFacts()

        # Calibration
        try:
            calib = self.db.compute_trade_calibration(lookback_days=30)
        except Exception as e:
            logger.warning("pm_facts: calibration failed: %s", e)
            calib = {}
        if calib:
            f.closed_trades_30d = int(calib.get("n") or 0)
            f.win_rate_30d_pct = calib.get("win_rate_pct")
            f.avg_return_30d_pct = calib.get("avg_return_pct")
            f.avg_hold_days_30d = calib.get("avg_hold_days")

        # RM discipline
        try:
            rm_rows = self.db.get_recent_agent_outputs(
                agent_name="risk_manager", limit=5,
                before_date=session_date_key(),
            )
        except Exception as e:
            logger.warning("pm_facts: rm outputs failed: %s", e)
            rm_rows = []
        f.rm_verdicts_seen = len(rm_rows)
        for row in rm_rows:
            data = self._parse_logged_agent_response(row)
            if not isinstance(data, dict):
                continue
            scale = data.get("scale_all_buys", 1.0)
            try:
                if float(scale) < 1.0:
                    f.rm_scale_downs_last5 += 1
            except (TypeError, ValueError):
                pass
            if data.get("modifications"):
                f.rm_mods_last5 += 1

        # Book state.
        #
        # `invested_pct` comes from `book_exposure` — the SAME function the
        # pre-trade gate's `deployment_gap` advisory reads, so PM
        # and RM can no longer be told opposite things about one book (they
        # were: 70% "10pp OVER" to PM and 10% "50pp UNDER" to RM on the same
        # $50k-long/$20k-SQQQ book). `positions` here is already sweep-split
        # by DecisionStage, and `cash` is `deployable_cash` (raw cash + the
        # parked vehicle), so the parked T-bills count as cash on both legs.
        #
        # `net_exposure_pct` is reported ALONGSIDE rather than substituted:
        # deployment answers "is the money at work", direction answers "which
        # way does the book lean", and one number cannot be both.
        from src.risk.rules import book_exposure
        if total_value > 0:
            exposure = book_exposure(positions, total_value)
            f.invested_pct = round(exposure.deployed_pct, 1)
            f.net_exposure_pct = round(exposure.net_pct, 1)
            f.cash_pct = round((cash or 0) / total_value * 100, 1)
        f.position_count = len(positions)

        # Sector weights — SEPARATE long and short budgets (spec §12.2).
        #
        # This REVERSES the netting that shipped with the shorts work: a held
        # short used to add a NEGATIVE weight, so a long 15% and a short 5% in
        # Technology rendered as a single 10% line. Owner's ratified reasoning:
        # *"A long and a short in the same sector is not a hedge... We are
        # trading opportunities."* Netting also showed the PM a smaller number
        # than `RiskRuleEngine.check` enforces against — the PM would reason
        # about concentration from one book while the gate refused on another.
        #
        # `sector_side_weights` is the shared definition the gate and the
        # constructor use, so all three cannot drift apart again. The only
        # thing local here is sector RESOLUTION: PM facts fall back to
        # `_sector_of` when the broker left `Position.sector` blank, and
        # "Unknown" is rendered rather than dropped so the PM can see that a
        # slice of the book is unclassified.
        from src.risk.rules import (
            SECTOR_SIDE_SHORT, sector_side_weights,
        )
        for (sector, side), weight in sector_side_weights(
            positions,
            total_value,
            resolve_sector=lambda p: p.sector or _sector_of(p.symbol) or "Unknown",
            include_unknown=True,
        ).items():
            bucket = (
                f.sector_weights_short if side == SECTOR_SIDE_SHORT
                else f.sector_weights_long
            )
            bucket[sector] = round(bucket.get(sector, 0.0) + weight, 1)

        # Age buckets + drift flag
        try:
            position_history = self._build_position_history(positions)
        except Exception:
            position_history = {}
        for p in positions:
            hist = position_history.get(p.symbol) or {}
            days = hist.get("days_held")
            if days is None:
                continue
            if days < 5:
                f.positions_under_5d += 1
            elif days <= 15:
                f.positions_5_to_15d += 1
            else:
                f.positions_over_15d += 1
            # Drift check — SAME weight and SAME P&L% the PM's own position
            # line renders (`position_weight_pct` / `unrealized_pnl_pct`).
            # This block used to carry raw, un-leveraged weight and a
            # `cost_basis > 0` P&L, so a line reading `Weight: 18.0% DRIFT`
            # sat three lines above `drift-flagged: 0` in one prompt.
            if total_value > 0:
                weight = position_weight_pct(p, total_value)
                pnl_pct = unrealized_pnl_pct(p)
                if weight > 12 and pnl_pct is not None and pnl_pct > 10:
                    f.positions_drift_flagged += 1

        # Signal freshness
        ages = [a.signal_age_days for a in analyses if a.signal_age_days is not None]
        f.tech_signals_count = len(analyses)
        if ages:
            f.tech_signals_median_age_days = int(statistics.median(ages))
            f.tech_signals_stale_count = sum(1 for a in ages if a >= 8)

        # System perf
        f.rolling_5d_pct = recent_performance.get("rolling_5d_pct")
        f.rolling_20d_pct = recent_performance.get("rolling_20d_pct")
        f.in_drawdown = bool(recent_performance.get("in_drawdown"))

        # RC3: deployment gap vs the invested target as a hard fact in PM's
        # face. The target is the owner's fixed fully-invested mandate
        # (2026-09-17), not a macro output — macro no longer sets or lowers
        # it. Only rendered when there is a book to measure.
        from src.risk.rules import DESK_INVESTED_TARGET_PCT, deployment_gap_band_pct
        if total_value > 0:
            f.invested_target_pct = DESK_INVESTED_TARGET_PCT
            f.deployment_gap_pp = round(
                f.invested_pct - DESK_INVESTED_TARGET_PCT, 1,
            )
            f.deployment_gap_band_pct = deployment_gap_band_pct(
                getattr(self, "config", None)
            )

        # Audit §1.3/§1.4 — the book's real risk, and each position's
        # R-multiple. None on failure; PMFacts.render() then says "unknown"
        # rather than implying the book is risk-free.
        f.heat = self._build_portfolio_heat(positions, total_value)
        # getattr-guarded for the ~58 tests that build TradingPipeline via
        # __new__() without __init__ — same convention as `_sweeper`.
        risk_cfg = getattr(getattr(self, "config", None), "risk", None)
        ceiling = getattr(risk_cfg, "max_portfolio_risk_pct", None)
        if isinstance(ceiling, (int, float)) and ceiling > 0:
            f.risk_ceiling_pct = float(ceiling)
        # Spec §2.2 — render the per-cluster cap the constructor enforces, so
        # the PM sizes a theme against it instead of meeting it as a surprise.
        cluster_share = getattr(risk_cfg, "max_cluster_risk_share_pct", None)
        if isinstance(cluster_share, (int, float)) and 0 < cluster_share <= 100:
            f.cluster_risk_share_pct = float(cluster_share)

        # Audit §1.2 — PM has been told to avoid stacking correlated names
        # while being shown no correlation data at all. Give it the clusters
        # the deterministic check already builds, BEFORE it chooses.
        try:
            from src.data.correlation import correlation_clusters
            universe = {p.symbol for p in positions if p.qty > 0}
            universe |= {a.symbol for a in analyses}
            f.correlation_coverage = bool(correlation_matrix)
            f.correlation_clusters = correlation_clusters(
                universe, correlation_matrix or {},
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("pm_facts: correlation clusters failed: %s", e)
            f.correlation_coverage = False
            f.correlation_clusters = []

        # Who these tickers actually are. PM has been reasoning about `CCJ`
        # and `PATH` as price series with a sector tag; "Energy" covers both
        # an integrated major and a pre-revenue nuclear startup, and a sector
        # label alone lets it reach for the wrong prior confidently. Scoped to
        # the symbols already in play for THIS decision (held + candidates) —
        # never the configured universe — and capped, because a cold cache
        # pays one network round trip per symbol and the morning session's
        # budget is not the place to discover a slow yfinance. Every failure
        # mode inside the store degrades to an identity-less profile, which
        # PMFacts.render() drops; this except is the belt to that suspenders.
        try:
            from src.data.company import CompanyProfileStore
            profile_symbols = sorted(
                {p.symbol for p in positions if p.qty > 0}
                | {a.symbol for a in analyses}
            )[:_PM_PROFILE_SYMBOL_CAP]
            f.company_profiles = list(
                CompanyProfileStore()
                .get_many(profile_symbols, allow_fetch=True)
                .values()
            )
        except Exception as e:  # noqa: BLE001 — identity is nice-to-have
            logger.warning("pm_facts: company profiles failed: %s", e)
            f.company_profiles = []

        return f

    @staticmethod
    def _log_conviction_outcome_for_operator(stats: dict) -> None:
        """Log the FULL by_conviction / by_allocated_risk breakdown for a
        human operator reading logs — including every bucket below
        `_CONVICTION_OUTCOME_MIN_N`, which `_build_calibration_note` never
        puts in front of an agent (see the "MOST IMPORTANT CONSTRAINT" note
        at its call site). This is the ONLY place that count is surfaced at
        all: recorded, not silently dropped, per spec §7.2 — "that must be
        discovered from data, not assumed" cuts both ways: assumed-absent
        is as wrong as assumed-present.
        """
        try:
            parts = []
            for grouping_key in ("by_conviction", "by_allocated_risk"):
                grouping = stats.get(grouping_key) or {}
                bucket_strs = []
                for label, s in grouping.items():
                    if not s:
                        continue
                    if s.get("insufficient_data"):
                        bucket_strs.append(f"{label}: n={s.get('n', 0)} (below floor)")
                    else:
                        bucket_strs.append(
                            f"{label}: n={s.get('n')} win={s.get('win_rate_pct')}% "
                            f"avg={s.get('avg_return_pct')}%"
                        )
                if bucket_strs:
                    parts.append(f"{grouping_key}=[{'; '.join(bucket_strs)}]")
            if not parts:
                return
            logger.info(
                "Conviction/risk-outcome calibration (OPERATOR-ONLY — never "
                "sent to any agent prompt below the sample floor): %s | "
                "conviction_unknown_n=%s allocated_risk_unknown_n=%s",
                " ".join(parts),
                stats.get("conviction_unknown_n"),
                stats.get("allocated_risk_unknown_n"),
            )
        except Exception as e:  # noqa: BLE001 — logging must never break calibration
            logger.warning("conviction_outcome operator log failed: %s", e)

    def _build_calibration_note(self, lookback_days: int = 45) -> str:
        """Render PM's own hit rate + avg return on closed BUYs in the window.

        L4 calibration memory — the answer to 'has my conviction actually paid
        off recently?'. Without this PM keeps sizing confidence on today's
        alignment score alone, even if that score has been losing lately.
        """
        try:
            stats = self.db.compute_trade_calibration(lookback_days=lookback_days)
        except Exception as e:
            logger.warning("calibration_note: stats failed: %s", e)
            return ""
        if not isinstance(stats, dict) or not stats:
            return ""
        # Conviction ledger (spec §7.2) — operator-only surface. Logged on
        # EVERY call regardless of the floor below, deliberately separate
        # from the prompt text being built: this is how a human operator
        # sees "n=8, split 4/3/1, too few to conclude anything" WITHOUT it
        # ever reaching an agent. Never gate this log on the floor — the
        # whole point is that the operator sees the sub-floor count too.
        self._log_conviction_outcome_for_operator(stats)
        try:
            if stats.get("n", 0) < 3:
                return ""
        except TypeError:
            return ""
        lines = [
            f"- Overall (last {stats.get('lookback_days', lookback_days)}d): "
            f"{stats['n']} closed BUYs, win rate {stats['win_rate_pct']:.0f}%, "
            f"avg return {stats['avg_return_pct']:+.2f}%, avg hold {stats['avg_hold_days']:.1f}d"
        ]
        by_size = stats.get("by_size") or {}
        for label, s in by_size.items():
            if not s or s.get("n", 0) == 0:
                continue
            lines.append(
                f"  - {label}: {s['n']} trades, win {s['win_rate_pct']:.0f}%, "
                f"avg {s['avg_return_pct']:+.2f}%, hold {s['avg_hold_days']:.1f}d"
            )
        # Conviction ledger (spec §7.2) — THE MOST IMPORTANT CONSTRAINT on
        # this whole feature: a bucket below `_CONVICTION_OUTCOME_MIN_N`
        # (db.py) is `_gated_bucket_stats`-shaped ({"n", "insufficient_data":
        # True, "message"}) and is skipped here ENTIRELY — no header, no
        # line, nothing appended to `lines` — never a "too few trades" line
        # either, because even that much would put the bucket's existence
        # and its raw direction in front of the model. Only a bucket that
        # has cleared the floor (`insufficient_data` False) ever reaches
        # this prompt text. With production at n=8 total (2026-08-30),
        # EVERY bucket in both groupings is below floor, so today this
        # appends nothing at all — that is the correct, intended behaviour,
        # not a bug to "fix" by lowering the floor.
        for grouping_key, section_label in (
            ("by_conviction", "By conviction"),
            ("by_allocated_risk", "By allocated risk"),
        ):
            grouping = stats.get(grouping_key) or {}
            qualifying = [
                (label, s) for label, s in grouping.items()
                if s and not s.get("insufficient_data", True) and s.get("n", 0) > 0
            ]
            if not qualifying:
                continue
            lines.append(f"  {section_label} (established sample):")
            for label, s in qualifying:
                lines.append(
                    f"    - {label}: {s['n']} trades, win {s['win_rate_pct']:.0f}%, "
                    f"avg {s['avg_return_pct']:+.2f}%, hold {s['avg_hold_days']:.1f}d"
                )
        return "\n".join(lines)

    def _compute_recent_performance(self, current_equity: float) -> dict:
        """Rolling 5-day and 20-day returns from db.daily_pnl, + drawdown flag.

        Used to tell PM 'we've been losing — size down' regardless of what the market
        is doing. Independent of VIX / macro regime (which reflect market, not us).

        The two thresholds MOVE EVERY SESSION since the 2026-09-11 basis
        change (docs/WORK.md item 32): they are a multiple of the realized
        daily volatility of THE BOOK CURRENTLY HELD, reconstructed from the
        real market price history of its actual holdings at their actual
        weights, not a fixed percentage of equity.
        `held_book_daily_vol_pct` carries the yardstick they came from, and
        is None when nothing is measurable (an all-cash book, or holdings
        without enough price history) and the fixed-percentage fallback is
        governing.

        The ROLLING RETURNS are still the account's own — that is what the
        brake is judging. Only the YARDSTICK moved off the equity curve.

        Returns e.g. {'rolling_5d_pct': -2.3, 'rolling_20d_pct': -6.1,
                      'in_drawdown': True, 'trailing_days': 18,
                      'held_book_daily_vol_pct': 0.94,
                      'drawdown_5d_threshold_pct': -6.3,
                      'drawdown_20d_threshold_pct': -12.6}
        """
        try:
            rows = self.db.get_daily_pnl(limit=25)
        except Exception as e:
            logger.warning("Failed to read daily_pnl for drawdown context: %s", e)
            return {}
        if not rows:
            return {
                "rolling_5d_pct": None, "rolling_20d_pct": None,
                "in_drawdown": False, "trailing_days": 0,
                "peak_to_trough_pct": None,
                "drawdown_5d_threshold_pct": self.config.risk.drawdown_5d_threshold_pct,
                "drawdown_20d_threshold_pct": self.config.risk.drawdown_20d_threshold_pct,
            }

        def _pct_change(start_idx: int) -> float | None:
            if start_idx >= len(rows):
                return None
            start_value = rows[start_idx].get("total_value") or 0
            if start_value <= 0:
                return None
            return round((current_equity - start_value) / start_value * 100, 2)

        # rows are ordered newest-first (DESC); rows[N] = N trading days ago.
        # rows[0] is today, so "5 trading days ago" is rows[5], not rows[4].
        rolling_5d = _pct_change(5)
        rolling_20d = _pct_change(20)

        # docs/WORK.md item 32 (owner call 2026-09-11). These thresholds are
        # no longer a fixed percentage of equity at all — they are a multiple
        # of the realized daily volatility of THE BOOK CURRENTLY HELD,
        # measured from the real market price history of its actual holdings
        # at their actual weights, scaled to each window by sqrt(time). A
        # fixed percentage is only correct for the volatility regime it was
        # chosen in, and markets are not stationary; the owner refused a
        # recalibration of the fixed number for exactly that reason.
        #
        # The yardstick is NOT this account's own equity curve. That was the
        # first implementation and the owner rejected it: the post-reset
        # account spends its first sessions ramping from all-cash, a
        # mostly-cash account barely moves, and the measurement would have
        # been artificially low — setting the alarms artificially tight so
        # they fire on normal behaviour once the book is deployed. See
        # `src/risk/rules.py::measure_portfolio_daily_vol`.
        #
        # `drawdown_5d_threshold_pct` / `drawdown_20d_threshold_pct` (the
        # risk-unit-derived fixed percentages) survive as the fallback for
        # when there is genuinely NOTHING to measure — an all-cash book, or
        # holdings with too little price history. See
        # `RiskConfig.drawdown_vol_sensitivity` for the sensitivity and,
        # honestly, for what about it is and is not research-grounded.
        daily_vol = self.held_book_daily_vol_pct()
        sensitivity = _risk_number(
            getattr(self.config.risk, "drawdown_vol_sensitivity", None),
            DEFAULT_DRAWDOWN_VOL_SENSITIVITY,
        )
        threshold_5d = vol_relative_drawdown_threshold_pct(
            daily_vol_pct=daily_vol, window_sessions=5,
            sensitivity=sensitivity,
            fallback_pct=self.config.risk.drawdown_5d_threshold_pct,
        )
        # The 20-day window is capped at `GROSS_LADDER_ALERT_PCT`, preserving
        # the 2026-09-04 bug-2 fix: this brake must never again be asleep
        # past the point the §11.2 de-levering ladder halves the book and
        # alerts the owner. The cap is why the 20-day threshold is tighter
        # than sqrt(time) alone would put it.
        threshold_20d = vol_relative_drawdown_threshold_pct(
            daily_vol_pct=daily_vol, window_sessions=20,
            sensitivity=sensitivity,
            fallback_pct=self.config.risk.drawdown_20d_threshold_pct,
            cap_pct=GROSS_LADDER_ALERT_PCT,
        )
        # Keep the severity ORDER coherent in a high-volatility regime: once
        # the 20-day cap binds, an uncapped 5-day threshold could end up
        # DEEPER than the 20-day one, i.e. a shorter window tolerating a
        # bigger loss than a longer one. Clamped to the 20-day threshold so
        # |1d| <= |5d| <= |20d| <= |ladder alert| always holds.
        if threshold_5d < threshold_20d:
            threshold_5d = threshold_20d

        in_drawdown = False
        if rolling_5d is not None and rolling_5d < threshold_5d:
            in_drawdown = True
        if rolling_20d is not None and rolling_20d < threshold_20d:
            in_drawdown = True

        # Spec §11.2: peak-to-trough drawdown, which drives the de-levering
        # ladder's gross-exposure ceiling. A SEPARATE measure from
        # `in_drawdown` above, on purpose — that one asks "has our recent
        # edge degraded, so halve new BUYs" over a rolling window; this one
        # asks "how far are we off the high-water mark, so how much may the
        # book own". A longer window is read because a high-water mark over
        # 25 sessions is not a high-water mark.
        try:
            hwm_rows = self.db.get_daily_pnl(limit=252)
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "Failed to read the long daily_pnl window for the §11.2 "
                "high-water mark; falling back to the short one: %s", e,
            )
            hwm_rows = rows
        peak_to_trough = peak_to_trough_pct(
            [r.get("total_value") for r in (hwm_rows or [])], current_equity,
        )

        return {
            "rolling_5d_pct": rolling_5d,
            "rolling_20d_pct": rolling_20d,
            "in_drawdown": in_drawdown,
            # docs/WORK.md item 32: carried through so prompt-facing text
            # (e.g. PortfolioManagerAgent) can render the REAL, currently
            # configured thresholds instead of a hardcoded description that
            # would go stale the moment these are rescaled again.
            "drawdown_5d_threshold_pct": threshold_5d,
            "drawdown_20d_threshold_pct": threshold_20d,
            # docs/WORK.md item 32: the yardstick itself, surfaced so an
            # operator (and Mission Control) can see WHY a threshold sits
            # where it does. None means "nothing measurable — the
            # fixed-percentage fallback is governing". This is the normal
            # daily move of the HELD BOOK, so it is already net of how
            # deployed the book is: a third-deployed book reports roughly a
            # third of the move the same basket fully deployed would.
            "held_book_daily_vol_pct": (
                None if daily_vol is None else round(daily_vol, 3)
            ),
            "trailing_days": len(rows),
            "peak_to_trough_pct": peak_to_trough,
        }

    def measure_held_book_daily_vol(self):
        """`PortfolioVolEstimate` for the book the desk is holding right now.

        docs/WORK.md item 32 (owner call 2026-09-11). The single place the
        drawdown alarms' volatility yardstick is produced. Gathers what only
        the pipeline can reach — the live position list, live equity, and
        real market price history for those exact symbols — and hands it to
        `src/risk/rules.py::measure_portfolio_daily_vol`, which owns the
        arithmetic. The volatility DEFINITION therefore lives in exactly one
        place rather than being re-implemented per caller, which is how this
        desk ended up with two unreconciled drawdown measures in the first
        place.

        Price history comes from `self.market.get_ohlcv` — the same market
        feed (yfinance, with the Alpaca fallback already wired in
        `set_fallback_bars`) that technical analysis, the correlation matrix
        and the ATR reads all use. No second price path is introduced.

        Never raises. Any failure returns an estimate whose `daily_vol_pct`
        is None, which means "fall back to the fixed percentage" — a broken
        read must never disable the circuit breaker.
        """
        from src.risk.rules import (
            measure_portfolio_daily_vol,
            normalized_holding_weights,
        )
        try:
            account, positions, _ = self._refresh_account_state()
            equity = float(
                getattr(account, "portfolio_value", None)
                or getattr(account, "equity", None)
                or 0.0
            )
            # 2026-09-14, docs/WORK.md item 32: the cash park is excluded
            # here, exactly as `gross_exposure` already excludes it. Parked
            # cash was 78% of the gross weight this yardstick was measured
            # over on the archived book, which made the breaker's denominator
            # neither the risk book nor the account.
            weights = normalized_holding_weights(
                positions, equity, cash_park_symbol=self._sweep_symbol(),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Could not read the held book for the volatility-relative "
                "drawdown alarms; they fall back to their fixed-percentage "
                "thresholds: %s", exc,
            )
            return PortfolioVolEstimate(None, reason="held book unreadable")
        if not weights:
            return measure_portfolio_daily_vol({}, {})

        # Session-scoped memo. The daily circuit breaker fires from six
        # separate places in this file, and each measurement is one market
        # fetch per holding — without this the same number would be bought
        # six times a session.
        #
        # Keyed on the TRADING DATE and on the holdings with their weights
        # (to 4dp), so an intraday change in the book re-measures rather
        # than serving a stale yardstick, AND a new session can never serve
        # yesterday's. The date half is not decorative: `src/scheduler.py`
        # holds ONE `TradingPipeline` for the life of the process, so this
        # memo outlives a session. Without the date, a book whose weights
        # happen to round to the same 4dp on two consecutive days would be
        # priced today against yesterday's volatility — a silently stale
        # yardstick under all three loss alarms. This comment previously
        # CLAIMED a bar-date component the key did not have (docs/WORK.md
        # item 32, 2026-09-14); the code now does what it said.
        from src.trading_calendar import et_now
        try:
            measured_on = et_now().date().isoformat()
        except Exception as exc:  # noqa: BLE001
            # A clock/timezone failure must never serve a stale number: fall
            # through with a key that can never match a previous call, so the
            # measurement is simply redone.
            logger.warning(
                "Held-book volatility memo: could not read the trading date "
                "(%s) — re-measuring rather than reusing a cached yardstick.",
                exc,
            )
            # A fresh object each time: it can never equal a stored key, so
            # the memo misses and the measurement is redone.
            measured_on = object()
        key = (
            measured_on,
            tuple(sorted((sym, round(w, 4)) for sym, w in weights.items())),
        )
        cached = getattr(self, "_held_book_vol_memo", None)
        if cached is not None and cached[0] == key:
            return cached[1]

        bars_by_symbol = {}
        for symbol in weights:
            try:
                bars_by_symbol[symbol] = self.market.get_ohlcv(
                    symbol, self.config.trading.lookback_days,
                ) or []
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "Price history fetch failed for %s while measuring the "
                    "held book's volatility: %s", symbol, exc,
                )
                bars_by_symbol[symbol] = []

        estimate = measure_portfolio_daily_vol(weights, bars_by_symbol)
        self._held_book_vol_memo = (key, estimate)
        return estimate

    def held_book_daily_vol_pct(self) -> float | None:
        """The held book's realized daily volatility in percent, or None.

        docs/WORK.md item 32. Wired into `RiskRuleEngine` as
        `portfolio_vol_provider`; also the yardstick
        `_compute_recent_performance` reports and derives its two
        rolling-return brakes from, so all three loss alarms read ONE
        measurement rather than each making their own.
        """
        try:
            return self.measure_held_book_daily_vol().daily_vol_pct
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Held-book volatility measurement failed (%s) — the drawdown "
                "alarms fall back to their fixed-percentage thresholds.", exc,
            )
            return None

    def _refresh_account_state(self):
        account = self.broker.get_account()
        positions = self.broker.get_positions()
        price_map = {p.symbol: p.current_price for p in positions}
        return account, positions, price_map

    def _sync_positions_from_broker(self, positions=None) -> None:
        """Refresh the local SQLite `positions` table from broker truth.

        Broker is book of record. The local table is a derived snapshot for
        Mission Control journal / evening notifier / rehearsal consumers —
        it must not lag the broker after a session snapshot or after fills.

        `positions` is the already-fetched broker list when the caller has
        one (avoids a duplicate round-trip). Omit it to re-read the broker
        after execution. Fail-soft: a snapshot write must never abort a
        trading session.
        """
        try:
            snapshot = (
                list(positions) if positions is not None
                else self.broker.get_positions()
            )
            self.db.sync_positions(snapshot)
        except Exception as exc:  # noqa: BLE001
            logger.error("local positions table refresh failed: %s", exc)

    def _run_news_update(
        self, run_id: str, session: str = "morning",
        universe: list[str] | None = None,
        held_symbols: list[str] | None = None,
        candidate_symbols: list[str] | None = None,
    ) -> "tuple[NewsIntelligenceReport | None, NewsCoverage | None]":
        """Fetch news, run intelligence analysis, save report. Session-aware.

        - morning: full 3-layer build. prior_session_report=None.
        - midday:  delta mode. prior_session_report=morning's snapshot.
        - evening: summary mode. prior_session_report=midday's or morning's.

        Session-tagged reports persist alongside the latest full_report.json so
        each session's output is individually recoverable for audit / debug.

        `held_symbols` / `candidate_symbols` (2026-08-30 owner decision) are
        the caller's ALREADY-ORDERED lists of symbols to also fetch
        individually via Yahoo Finance's per-symbol RSS — see
        NewsDataProvider.fetch_news. The deterministic selection rule lives
        HERE, not in the provider: held positions first, then the run's
        admitted candidates, each list in the caller's own stable order
        (never raw set iteration — see the callers of this method), deduped
        while preserving that order. NewsDataProvider itself enforces the
        symbol-count cap (config.news.per_symbol_max_symbols); this method
        only decides ordering and priority.

        Returns `(intel_report, coverage)`. `coverage` (src.data.news.
        NewsCoverage) is the 2026-08-28 fix for a dead feed vanishing
        silently: before this, a feed that 404'd or 403'd was dropped with a
        log warning and the news stage still reported "ok" regardless of
        how many wires actually came back. `coverage` is returned even when
        the analyst call itself fails below, since the fetch already
        happened and the caller (MorningResearchStage) needs it either way
        to set data_status["news"] honestly.
        """
        coverage = None
        try:
            research_universe = universe or self.config.trading.universe
            per_symbol_symbols = list(dict.fromkeys(
                [str(s).strip().upper() for s in (held_symbols or []) if str(s).strip()]
                + [str(s).strip().upper() for s in (candidate_symbols or []) if str(s).strip()]
            ))
            news_items, coverage = self.news_provider.fetch_news(symbols=per_symbol_symbols)
            news_text = self.news_provider.format_for_prompt(
                news_items, max_items=self.config.news.max_prompt_items,
            )
            stock_mentions = self.news_provider.tag_symbol_mentions(
                news_items, research_universe)
            previous_narrative = self.news_store.load_macro_narrative()
            # For midday/evening, load the most recent prior session report as
            # a diff baseline. Prefer midday over morning when both exist
            # (evening sees the most recent snapshot available).
            prior_session_report = None
            if session == "midday":
                prior_session_report = self.news_store.load_daily_report("morning")
            elif session == "evening":
                prior_session_report = (
                    self.news_store.load_daily_report("midday")
                    or self.news_store.load_daily_report("morning")
                )
            intel_report, result = self.news_analyst.analyze(
                news_text=news_text,
                universe=research_universe,
                stock_mentions=stock_mentions,
                previous_narrative=previous_narrative,
                session=session,
                prior_session_report=prior_session_report,
                news_coverage=coverage,
            )
            if intel_report:
                report_dict = intel_report.model_dump()
                self.news_store.save_daily_report(report_dict, session=session)
                self.news_store.save_macro_narrative(report_dict["macro_narrative"])
                if report_dict.get("stock_news"):
                    self.news_store.save_stock_alerts(report_dict["stock_news"])
                # collapsed_count / source_count are persisted so the dedup
                # stage stays auditable after the fact — you can re-measure
                # the duplication rate from the archive without re-fetching.
                # per_symbol (2026-08-30) is persisted for the same reason:
                # measuring the per-symbol duplicate rate after the fact
                # shouldn't require re-fetching either.
                self.news_store.save_raw_headlines(
                    [{"title": i.title, "source": i.source, "summary": i.summary,
                      "collapsed_count": getattr(i, "collapsed_count", 1),
                      "source_count": getattr(i, "source_count", 1),
                      "per_symbol": getattr(i, "per_symbol", False)}
                     for i in news_items])
                n_changes = len(intel_report.state_changes)
                n_stocks = len(intel_report.stock_news)
                logger.info("[%s] News intelligence: sentiment=%s, changes=%d, stocks=%d",
                            session, intel_report.market_sentiment, n_changes, n_stocks)
            self.db.insert_agent_log(
                agent_name=f"news_analyst_{session}", run_id=run_id,
                input_summary=(
                    f"{len(news_items)} news items "
                    f"({coverage.describe() if coverage is not None else 'coverage unknown'})"
                ),
                input_message=result.user_message,
                output_summary=f"sentiment={intel_report.market_sentiment}, changes={len(intel_report.state_changes)}" if intel_report else "parse_error",
                full_response=result.raw_text,
                model=result.model,
                tokens_used=result.tokens_used,
                input_tokens=result.input_tokens,
                output_tokens=result.output_tokens,
                cost_usd=result.cost_usd,
                **agent_log_kwargs(result),
            )
            return intel_report, coverage
        except PaidAnalysisSuspended:
            raise
        except Exception as e:
            logger.error("[%s] News analyst failed: %s", session, e)
            return None, coverage

    def _load_earnings_analyses(
        self, run_id: str, session: str = "morning",
        ctx: RunContext | None = None,
        universe: list[str] | None = None,
    ) -> tuple[list, list]:
        """Hot-path consumer: read cached earnings analyses, never call the LLM.

        The LLM-producing path is `run_earnings_preprocess()`, which runs
        pre-market (08:00-09:15 ET) and synchronously analyzes + confirms
        every new 10-Q/10-K. By the time morning/midday/evening fire, the
        authoritative result is already on disk.

        This method returns:
          - cached analyses for any filing already confirmed by preprocess
          - placeholder `queued=True` entries for filings that preprocess
            missed (e.g. preprocess didn't run, or the filing dropped after
            preprocess but before a later session). PM sees these and sizes
            down accordingly — better than blocking the session on an LLM.

        No background threads, no session-time token spend. The
        `run_id` + `session` + `ctx` signature is preserved for
        compatibility with MorningResearchStage's callable injection.
        """
        try:
            reports = self.earnings_provider.check_and_fetch(
                universe or self.config.trading.universe,
            )
            if not reports:
                return [], []

            new_reports = [r for r in reports if r.is_new]
            cached_reports = [r for r in reports if not r.is_new]

            cached_results = self.earnings_analyst.analyze_reports(cached_reports)

            for r in new_reports:
                cached_results.append({
                    "symbol": r.symbol,
                    "analysis": None,
                    "is_new": True,
                    "queued": True,
                    "form_type": r.form_type,
                    "filing_date": r.filing_date,
                })

            if new_reports:
                symbols = ", ".join(r.symbol for r in new_reports)
                logger.warning(
                    "[%s] %d filings missed pre-market preprocessing (%s); "
                    "surfacing as placeholder only — PM will size down.",
                    session, len(new_reports), symbols,
                )

            logger.info(
                "[%s] Earnings: %d cached analyses, %d unanalyzed placeholders",
                session, len(cached_results) - len(new_reports), len(new_reports),
            )
            return reports, cached_results
        except Exception as e:
            # audit round 2: swallowing here made data_status["earnings"]
            # "failed" unreachable — a full SEC-EDGAR outage was
            # indistinguishable from "no filings today", so RM's
            # data_degraded advisory never counted earnings. Morning routes
            # through MorningResearchStage, whose except sets the status;
            # midday/evening call sites wrap this locally to keep their
            # continue-without-earnings behavior.
            logger.error("[%s] Earnings load failed: %s", session, e)
            raise

    def _earnings_preprocess_symbols(self) -> list[str]:
        """Configured universe plus Form-4 admission-eligible names.

        2026-09-16: preprocess returned `nothing_new` against the configured
        universe while FTK/RSG were already Form-4 hot. Morning then saw
        those filings as placeholders. The hot list is whatever the
        already-refreshed provider marks `admission_eligible` — not an
        invented "preprocess N names" cap. Morning's broker-quality gate
        and `max_external_candidates` still decide who actually trades.
        """
        configured = [
            str(symbol).strip().upper()
            for symbol in (self.config.trading.universe or [])
            if str(symbol).strip()
        ]
        hot: list[str] = []
        try:
            if not getattr(self.config.smart_money, "enabled", False):
                return configured
            provider = getattr(self, "smart_money_provider", None)
            if provider is None or not hasattr(provider, "fetch"):
                return configured
            observations, _err = provider.fetch(configured)
            seen = set(configured)
            for item in observations or []:
                if not bool(getattr(item, "admission_eligible", False)):
                    continue
                symbol = str(getattr(item, "symbol", "") or "").strip().upper()
                if not symbol or symbol in seen:
                    continue
                seen.add(symbol)
                hot.append(symbol)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Earnings preprocess: hot-admit symbol union failed (%s) — "
                "falling back to the configured universe", exc,
            )
            return configured
        if hot:
            logger.info(
                "Earnings preprocess: adding %d Form-4 admission-eligible "
                "symbol(s) to the filing check: %s",
                len(hot), ", ".join(hot),
            )
        return configured + hot

    # ---------------------------------------------------------------
    # Morning stages (extracted from the legacy monolithic run_morning).
    # Phase 4 #1 final wire-up: each stage is a method taking ctx; the
    # orchestrating run_morning just composes them. Stages can be tested
    # individually by constructing a ctx, populating the needed fields,
    # and calling the method directly.
    # ---------------------------------------------------------------

    def _check_late_breach_and_halt(
        self, run_id: str, where: str, ctx=None,
    ) -> dict | None:
        """Refresh broker state and HALT if the daily-loss limit was crossed
        mid-session. Renamed with the behaviour change — it no longer
        liquidates; see `_halt_on_daily_loss_breach`.

        Used by morning at the post-research checkpoint and (potentially)
        by other long-running phases to close the gap between the
        pre-research circuit breaker (#45) and the pre-execution recheck
        (#48). On a slow-OpenAI day research can take 5-10 min — plenty
        of time for the tape to gap through the limit while morning is
        still computing.

        Returns the halt response dict on breach, None to proceed. ``where``
        is a short tag for the log message (post-research / post-decision /
        etc).
        """
        try:
            account = self.broker.get_account()
            positions = self.broker.get_positions()
        except Exception as exc:
            logger.warning(
                "Late-breach check (%s): broker query failed: %s — "
                "proceeding without recheck", where, exc,
            )
            return None

        loss_violation, _baseline, _pnl, basis = self._daily_loss_breach(
            account, positions,
        )
        if not (loss_violation and positions):
            return None

        return self._halt_on_daily_loss_breach(
            positions, loss_violation, run_id,
            where=f"morning late-breach ({where})", basis=basis, ctx=ctx,
        )

    # `_midday_emergency_liquidate` was DELETED 2026-09-14 (retired-ok; docs/WORK.md
    # item 32). It force-closed the entire book on a daily-loss breach with
    # LIMIT orders 1% through the market and then restored the original
    # stops on any leg that did not fill — so on a correlated gap, the one
    # day a whole-book dump could be argued for, it cancelled every
    # protective stop, failed to sell, and put the stops back. It never
    # fired in production. `_halt_on_daily_loss_breach` replaces it: the
    # breaker now refuses new risk and VERIFIES the stops it is relying on,
    # and the proportionate leverage response stays where it already was,
    # in `_enforce_gross_ceiling`. The EMERGENCY_SELL / EMERGENCY_COVER
    # action tags remain recognised everywhere for historical rows.

    def _symbols_already_trimmed_today(self) -> set[str]:
        """Symbols that received a sell-side action earlier today (ET).

        Used by position_reviewer's same-day-trim discipline at midday/close:
        if midday already trimmed AMZN at +12% on TARGET_BREACH, close should
        not trim it AGAIN at +13% on the same flag — that loop produced a
        73% one-day cut on a still-working position (2026-05-04 AMZN 41 →
        21 → 11 shares).

        Sell-side = REDUCE / SELL / TAKE_PROFIT (historical rows only — the
        auto trim was deleted 2026-09-12) / PARTIAL_SELL(...) /
        EMERGENCY_SELL / FORCE_DELEVER, and its short-side mirror COVER /
        EMERGENCY_COVER / PARTIAL_COVER(...) (Stage 3 — a short trimmed at
        midday must be exempt from a second same-flag COVER at close for
        the exact reason a long is). TRAIL_STOP and HOLD do NOT count
        (TRAIL_STOP is stop adjustment, HOLD is no-op).

        Filters out canceled / rejected / expired orders that filled ZERO
        shares — if a SELL was submitted earlier and the broker rejected it,
        the symbol is fair game for re-trying. A PARTIAL fill still blocks:
        those shares left the book, so a second trim today would be the
        double-application this guard exists to prevent. Pending (`submitted`)
        and `filled` rows both block, so we never double-submit on the same
        symbol within one day.
        """
        try:
            rows = self.db.get_trades(today_only=True, limit=200)
        except Exception as exc:
            logger.warning(
                "_symbols_already_trimmed_today: query failed: %s", exc,
            )
            return set()
        sell_actions = {
            "REDUCE", "SELL", "TAKE_PROFIT",
            "EMERGENCY_SELL", "FORCE_DELEVER",
            "COVER", "EMERGENCY_COVER",
        }
        out: set[str] = set()
        for r in rows:
            action = (r.get("action") or "").upper()
            # Normalise PARTIAL_SELL(15%) → PARTIAL_SELL, PARTIAL_COVER(50%)
            # → PARTIAL_COVER.
            base_action = action.split("(", 1)[0].strip()
            if (base_action not in sell_actions
                    and base_action not in ("PARTIAL_SELL", "PARTIAL_COVER")):
                continue
            # A terminal-fail status that nevertheless moved shares IS a trim.
            # Filtering on fill_status alone (2026-07-16 audit) let a
            # partially-filled-then-canceled REDUCE fall through: the shares
            # left the book at midday, but close saw a clean slate and was free
            # to trim the same name again on the same soft flag — the exact
            # 2026-05-04 AMZN 41→21→11 double-trim this guard exists to stop.
            # `_trade_executed_or_pending` is the codebase's existing contract
            # for this (NULL/submitted/filled → yes; canceled/rejected/expired
            # → only when fill_qty > 0), and matches db._executed_trade_predicate.
            if not self._trade_executed_or_pending(r):
                continue
            sym = r.get("symbol")
            if sym:
                out.add(sym)
        return out

    def _atr_for_symbol(self, symbol: str) -> float | None:
        """ATR(14) from ~30 days of daily bars; None when unknowable.

        Used by the TRAIL_STOP noise-band clamp and the position-facts
        vol-unit metrics. Failure is always None (callers degrade to the
        pre-clamp behavior) — never raises.
        """
        try:
            bars = self.market.get_ohlcv(symbol, 30) or []
            if len(bars) < 15:
                return None
            from src.data.technical import compute_indicators
            atr = compute_indicators(symbol, bars).atr_14
            return float(atr) if atr and atr > 0 else None
        except Exception as e:  # noqa: BLE001
            logger.warning("ATR fetch failed for %s: %s", symbol, e)
            return None

    def _adjudicate_target_revision_flags(
        self, review, positions, *, run_id: str, seat: str,
    ) -> list[dict]:
        """Adjudicate this review's take-profit revision flags.

        A seat raises `src.models.TargetRevisionFlag` — SYMBOL AND EVIDENCE,
        no price; the schema has no price field. This method supplies
        `src.risk.target_revision.assess_target_revision` with real numbers
        recomputed straight from bars, using the same deterministic, no-LLM
        machinery `_structural_protection_for_holding` uses
        (`compute_indicators` for ATR, `find_structural_levels` for levels,
        `levels_coverage_for_bars` for whether an empty result is a fault or
        a reading), and writes the outcome.

        DELIBERATELY runs AFTER `_midday_execute_llm_actions`. Every exit
        decision this session makes has already been made and vetoed against
        `metric_deltas` built before this point, so a revision cannot reach
        them even in principle. That is the second of two independent
        defences; the first is that progress/pace are measured against the
        PINNED `initial_take_profit` (see `_build_position_facts`), so a
        revision cannot move a guarded metric at all.

        EVERY flag produces a durable row — a re-derivation, a named refusal,
        or a named data fault. Never a silent no-op and never a blank.
        Returns the outcome payloads for the session result / cockpit.

        Never raises: a failure here must not take down a review that has
        already executed its orders.
        """
        from src.data.levels import (
            BREAKOUT_PROJECTION_ATR_MULTIPLE,
            CLUSTER_TOLERANCE_PCT,
            COVERAGE_UNKNOWN,
            MAX_HORIZON_SESSIONS,
            MAX_REACH_ATR_MULTIPLE,
            MIN_TARGET_ATR_MULTIPLE,
        )
        from src.risk.target_revision import (
            assess_target_revision,
            level_backing_target,
            target_level_broken,
        )
        from src.trading_calendar import et_today

        flags = list(getattr(review, "target_revision_flags", None) or [])
        if not flags:
            return []

        risk_cfg = getattr(getattr(self, "risk_engine", None), "config", None)
        target_cfg = {
            "min_target_atr_multiple": getattr(
                risk_cfg, "min_target_atr_multiple", MIN_TARGET_ATR_MULTIPLE),
            "breakout_projection_atr_multiple": getattr(
                risk_cfg, "breakout_projection_atr_multiple",
                BREAKOUT_PROJECTION_ATR_MULTIPLE),
            "max_reach_atr_multiple": getattr(
                risk_cfg, "max_target_reach_atr_multiple", MAX_REACH_ATR_MULTIPLE),
            "max_horizon_sessions": getattr(
                risk_cfg, "max_target_horizon_sessions", MAX_HORIZON_SESSIONS),
        }

        # Direction comes from BROKER TRUTH (the sign of the held qty), never
        # from the flag — the seat names a symbol, not a side.
        held: dict[str, object] = {}
        for p in positions or []:
            held[str(getattr(p, "symbol", "")).upper()] = p

        outcomes: list[dict] = []
        seen: set[str] = set()
        for flag in flags:
            sym = str(getattr(flag, "symbol", "") or "").strip().upper()
            evidence = str(getattr(flag, "evidence", "") or "")
            if not sym or sym in seen:
                continue
            seen.add(sym)
            position = held.get(sym)
            if position is None:
                # The seat flagged something not held. Filed, not silently
                # dropped, because a flag on a symbol that is not in the
                # book is itself a finding about the seat's view of the book.
                outcomes.append(self._file_target_revision(
                    run_id=run_id, symbol=sym, seat=seat, evidence=evidence,
                    code="REFUSAL_NOT_HELD", applied=False,
                    detail=(
                        "the seat flagged a take-profit revision for a symbol "
                        "the broker does not show as held"
                    ),
                ))
                continue

            is_short = float(getattr(position, "qty", 0) or 0) < 0
            try:
                buy = self.db.get_symbol_last_buy(
                    sym, action="SHORT" if is_short else None,
                ) if is_short else self.db.get_symbol_last_buy(sym)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "target revision: opening-row lookup failed for %s (%s)",
                    sym, exc,
                )
                buy = None
            buy = buy or {}

            # Same bars, same window, same helpers as
            # `_structural_protection_for_holding` — and the same rule that
            # the price fed to a break test is the latest COMPLETED DAILY
            # CLOSE, never a live quote.
            levels: list[float] = []
            atr = close_price = bar_date = None
            coverage = None
            try:
                bars = self.market.get_ohlcv(
                    sym, self.config.trading.lookback_days,
                ) or []
                from src.data.levels import (
                    find_structural_levels,
                    structure_coverage,
                )
                from src.data.technical import compute_indicators
                # What the bar history behind `levels` was, so an empty list
                # from a dead feed is a DATA fault and one from a measured,
                # structureless chart is a refusal — the same distinction
                # `_derive_target` passes at entry.
                coverage = structure_coverage(bars)
                if bars:
                    last_bar = sorted(bars, key=lambda b: b.date)[-1]
                    close_price = float(last_bar.close)
                    bar_date = str(last_bar.date)
                    atr = compute_indicators(sym, bars).atr_14
                    supports, resistances = find_structural_levels(bars)
                    levels = sorted(lv.price for lv in (*supports, *resistances))
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "target revision: bars/indicator fetch failed for %s (%s) "
                    "— the flag is filed as a data fault, not judged",
                    sym, exc,
                )

            stored_target = None
            try:
                stored_target = float(buy.get("take_profit") or 0) or None
            except (TypeError, ValueError):
                stored_target = None

            # Which level this target was measured against, recovered by the
            # same identity test the stop side uses. None for a measured-move
            # target, which is correct: it never sat on a level.
            target_level = level_backing_target(
                stored_target=stored_target,
                computed_levels=levels,
                # NOT a knob — the exact constant `find_structural_levels`
                # clustered these zones with (docs/WORK.md item 46).
                level_cluster_tolerance_pct=CLUSTER_TOLERANCE_PCT,
            )

            # Cross-day confirmation, keyed off THIS READ's own bar_date so
            # several intraday cycles re-reading one close are never
            # miscounted as two confirming days.
            effective_bar_date = bar_date or str(et_today())
            break_seen_prior_close = False
            try:
                prior = self.db.get_prior_target_level_break(
                    [sym], today_bar_date=effective_bar_date,
                    exclude_run_id=run_id,
                )
                break_seen_prior_close = bool(prior.get(sym, False))
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "target revision: prior-close read failed for %s (%s) — "
                    "today's break, if any, starts unconfirmed", sym, exc,
                )

            outcome = assess_target_revision(
                symbol=sym,
                direction="short" if is_short else "long",
                entry_price=float(getattr(position, "avg_entry", 0) or 0) or None,
                stored_target=stored_target,
                target_level=target_level,
                pinned_horizon_sessions=buy.get("expected_horizon_sessions"),
                setup_type=buy.get("setup_type") or None,
                levels=levels,
                atr=atr,
                close_price=close_price,
                levels_coverage=coverage or COVERAGE_UNKNOWN,
                break_seen_prior_close=break_seen_prior_close,
                # The same ratified derivation bars the constructor passes at
                # entry, read off `risk_engine.config` (what
                # `ConstructorConfig` itself mirrors). Read defensively
                # because this method must survive a lightweight pipeline
                # double in unit tests that never built a real risk_engine;
                # the fallbacks are `src.data.levels`' own module constants,
                # not a second invented set of numbers.
                **target_cfg,
            )

            # File today's raw break state for the NEXT trading day to
            # confirm against — the same read/persist shape as
            # `_structural_protection_for_holding`. A `None` from the break
            # test means the question could not be asked; nothing is filed,
            # so a missing input can never become half of a confirmation.
            raw_broken = target_level_broken(
                target_level=target_level, close_price=close_price,
                atr=atr, is_short=is_short,
            )
            if raw_broken is not None and bar_date:
                try:
                    self.db.save_target_level_break(
                        run_id=run_id, symbol=sym,
                        raw_broken=bool(raw_broken), bar_date=bar_date,
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "target revision: failed to persist %s break state "
                        "(%s) — tomorrow's read starts unconfirmed", sym, exc,
                    )

            applied = False
            if outcome.revised and outcome.new_price:
                try:
                    applied = bool(self.db.update_open_take_profit(
                        sym, outcome.new_price,
                        action="SHORT" if is_short else "BUY",
                    ))
                except Exception as exc:  # noqa: BLE001
                    logger.error(
                        "target revision: write-back failed for %s (%s) — "
                        "the stored target stands", sym, exc,
                    )
                    applied = False
                if applied:
                    logger.info(
                        "Target revised: %s $%.2f -> $%.2f (%s, %s) — "
                        "progress/pace stay measured against the pinned "
                        "entry target",
                        sym, outcome.prior_price or 0.0, outcome.new_price,
                        outcome.basis, outcome.trigger,
                    )
            if not applied and outcome.revised:
                # The derivation succeeded but the row did not move. Recorded
                # as its own outcome so the record can never claim a revision
                # the trade row does not carry.
                outcomes.append(self._file_target_revision(
                    run_id=run_id, symbol=sym, seat=seat, evidence=evidence,
                    code="FAULT_REVISION_WRITE_FAILED", applied=False,
                    trigger=outcome.trigger, prior_price=outcome.prior_price,
                    detail=(
                        f"{outcome.trigger} fired and re-derived "
                        f"${outcome.new_price:,.2f}, but the opening row could "
                        f"not be updated — the stored target stands"
                    ),
                ))
                continue

            outcomes.append(self._file_target_revision(
                run_id=run_id, symbol=sym, seat=seat, evidence=evidence,
                code=outcome.code, applied=applied, trigger=outcome.trigger,
                prior_price=outcome.prior_price, new_price=outcome.new_price,
                basis=outcome.basis, level_used=outcome.level_used,
                detail=outcome.detail,
            ))
        return outcomes

    def _file_target_revision(
        self, *, run_id: str, symbol: str, seat: str, evidence: str,
        code: str, applied: bool, trigger: str = "",
        prior_price: float | None = None, new_price: float | None = None,
        basis: str = "", level_used: float | None = None, detail: str = "",
    ) -> dict:
        """Write one adjudicated flag and return its payload.

        Persistence failure degrades to the in-memory payload (which still
        reaches the session result and the cockpit) rather than losing the
        outcome or raising — but it is logged as an error, because an
        unrecorded refusal is the blank this whole path exists to avoid.
        """
        payload = {
            "symbol": symbol, "code": code, "trigger": trigger, "seat": seat,
            "evidence": evidence, "detail": detail, "basis": basis,
            "prior_price": prior_price, "new_price": new_price,
            "level_used": level_used, "applied": bool(applied),
        }
        evidence_id = None
        try:
            evidence_id = self.db.record_target_revision(
                run_id=run_id, symbol=symbol, code=code, seat=seat,
                evidence=evidence, detail=detail, trigger=trigger,
                prior_price=prior_price, new_price=new_price, basis=basis,
                level_used=level_used, applied=applied,
            )
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "target revision: failed to record %s outcome %s (%s)",
                symbol, code, exc,
            )
        payload["evidence_id"] = evidence_id
        if not applied:
            logger.info(
                "Target revision refused for %s: %s — %s", symbol, code, detail,
            )
        return payload

    def _structural_protection_for_holding(
        self,
        *,
        symbol: str,
        thesis_invalid_if: str | None,
        entry_price: float | None,
        stop_loss: float | None,
        is_short: bool,
        run_id: str,
        persist: bool = True,
    ):
        """Fresh, close-based structural-protection read for one held
        position, including the cross-day confirmation lookup and the
        persist of today's read for the NEXT trading day to confirm
        against. Returns the `StructuralProtectionCheck`.

        `persist=False` makes the call READ-ONLY: the cross-day lookup
        still runs, but today's `raw_broken` is not filed, so this read can
        never become the prior-day half of a future confirmation. Callers
        that are consulting the check purely for the audit trail must pass
        it. Filing a break from a NEW call site would let a break confirm a
        day earlier than it does today, which lifts `protected` a day
        earlier, which can turn a currently-BLOCKED holding-discipline exit
        into an allowed one on the following session — a loosening, by
        side-effect, of a gate this repo deliberately keeps tight
        (docs/WORK.md item 60).

        Spec item 25 (2026-09-03/04, corrected same day) — replaces the
        flat `days_held < 5` holding-discipline window with a data-driven
        one: `src.risk.exit_guard.check_structural_protection`. That
        function is pure; this method supplies it with real numbers
        recomputed straight from bars using the SAME deterministic, no-LLM
        machinery `TechAnalystAgent.analyze_batch` uses when a position is
        first bought (`compute_indicators` for ATR/MAs,
        `find_structural_levels` for the structural levels) — just run
        again here against a HELD position instead of a BUY candidate, on
        the same `config.trading.lookback_days` window so level detection
        sees exactly the bar count it was tuned against.

        DELIBERATELY uses the latest COMPLETED DAILY CLOSE from those same
        bars (`bars[-1].close`), never a live broker/quote price — real
        technical-analysis practice (and `check_structural_protection`'s
        own confirmation gate) requires a level to break on a CLOSE, not an
        intraday tick, or a routine intrabar wick would misread as an
        invalidated thesis.

        The cross-day lookup is keyed off THIS READ's own `bar_date` (the
        actual latest completed close, which may be a prior calendar day if
        the market is still open) rather than wall-clock "today" — several
        same-session pipeline cycles reading the SAME close must never be
        miscounted as two separate confirming trading days.

        Never raises: a bars/indicator failure degrades to no ATR/levels/
        close, which `check_structural_protection` already treats as "no
        qualifying basis" and falls back to its noise-band check on — never
        to a wrong verdict. The DB read/write around it are each wrapped
        separately so a memory hiccup degrades to "unconfirmed" /
        "unpersisted" rather than losing the whole check.
        """
        from src.data.levels import CLUSTER_TOLERANCE_PCT
        from src.risk.exit_guard import check_structural_protection
        from src.trading_calendar import et_today

        computed_levels: list[float] = []
        computed_level_touches: dict[float, int] = {}
        atr = ma_20 = ma_50 = ma_200 = close_price = bar_date = None
        try:
            bars = self.market.get_ohlcv(symbol, self.config.trading.lookback_days) or []
            if bars:
                from src.data.levels import find_structural_levels
                from src.data.technical import compute_indicators
                last_bar = sorted(bars, key=lambda b: b.date)[-1]
                close_price = float(last_bar.close)
                bar_date = str(last_bar.date)
                indicators = compute_indicators(symbol, bars)
                atr = indicators.atr_14
                ma_20, ma_50, ma_200 = indicators.ma_20, indicators.ma_50, indicators.ma_200
                supports, resistances = find_structural_levels(bars)
                all_levels = (*supports, *resistances)
                computed_levels = sorted(lv.price for lv in all_levels)
                computed_level_touches = {lv.price: lv.touches for lv in all_levels}
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "structural protection: bars/indicator fetch failed for %s "
                "(%s) — checking with no close/level/MA data (falls back "
                "to the noise-band check)", symbol, e,
            )
        # A bars-fetch failure leaves no real close date; fall back to
        # wall-clock today purely as a persistence key — harmless because
        # `raw_broken` is always False on the no-close path below, so it can
        # never manufacture a false confirmation regardless of the date
        # it's filed under.
        effective_bar_date = bar_date or str(et_today())

        break_seen_prior_close = False
        try:
            prior = self.db.get_prior_holding_protection_break(
                [symbol], today_bar_date=effective_bar_date, exclude_run_id=run_id,
            )
            break_seen_prior_close = bool(prior.get(symbol.upper(), False))
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "structural protection: prior-close read failed for %s "
                "(%s) — today's break, if any, starts unconfirmed", symbol, e,
            )

        # Same two ratified bars `PortfolioConstructor`'s `ConstructorConfig`
        # mirrors off `self.risk_engine.config` (see its own "Kept in sync
        # with risk.*" comments) — read defensively rather than assumed,
        # since this method must survive a lightweight pipeline double (unit
        # tests) that never built a real `risk_engine`. The fallback values
        # are the RiskConfig field defaults themselves, not a second
        # invented number.
        risk_cfg = getattr(self, "risk_engine", None)
        risk_cfg = getattr(risk_cfg, "config", None)
        min_level_touches = getattr(risk_cfg, "min_level_touches_for_stop_honor", 5)

        check = check_structural_protection(
            thesis_invalid_if=thesis_invalid_if,
            current_price=close_price,
            entry_price=entry_price,
            stop_loss=stop_loss,
            atr=atr,
            is_short=is_short,
            computed_levels=computed_levels,
            computed_level_touches=computed_level_touches,
            min_level_touches=min_level_touches,
            # NOT a setting and not a fallback default — this is the exact
            # constant `find_structural_levels` used to cluster pivots into
            # the zones being matched against, so the tolerance cannot be
            # anything else. docs/WORK.md item 46.
            level_cluster_tolerance_pct=CLUSTER_TOLERANCE_PCT,
            ma_20=ma_20, ma_50=ma_50, ma_200=ma_200,
            break_seen_prior_close=break_seen_prior_close,
        )

        try:
            if persist:
                self.db.save_holding_protection_break(
                    run_id=run_id, symbol=symbol, raw_broken=check.raw_broken,
                    bar_date=effective_bar_date,
                )
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "structural protection: failed to persist today's read for "
                "%s (%s) — the next trading day's confirmation check will "
                "start unconfirmed for it", symbol, e,
            )

        return check

    def _substantiate_exit_triggers(self, review, *, ctx, run_id: str,
                                    review_kwargs: dict):
        """Heal, re-ask, then durably record an unsubstantiated exit trigger.

        The defect this closes (2026-09-18). Every SELL/REDUCE/COVER had to
        "cite a hard trigger" and the entire check was a substring match
        over `reason` prose. On 2026-09-16 the two real exits — COP SELL
        and EQNR REDUCE, run `midday-d8996a51` — carried the reason
        ``"adverse news"``, two words and nothing else, and passed every
        gate: the phrase is on the list, and
        `exit_guard.holding_discipline_claim_check` returned "ok" because
        it reads the PROSE for a claim it knows how to check and those two
        words make none. Meanwhile an exit that honestly described a stall
        is what the metric veto audits, and one naming no listed phrase is
        dropped. The gate was selecting for bad paperwork.

        The desk's standing heal order (owner 2026-09-16, `src.seat_heal`)
        applies, in order and with no step skipped:

        1. **Mechanical heal.** A trigger the prose already names is
           written into `PositionAction.exit_trigger` from the SAME phrase
           vocabulary the executor has matched against since 2026-08-27.
           Nothing is invented and nothing that used to execute stops
           executing.
        2. **One re-ask.** Anything still unsubstantiated — no trigger, no
           evidence behind the trigger, or an explicit
           `cannot_substantiate` — goes back to the seat naming those
           symbols and asking for the trigger plus the recorded thing it
           rests on, with keeping `cannot_substantiate` and HOLDing named
           as a correct answer. Bounded by the same one-paid-retry-per-
           seat-per-session cap the research seats use
           (`seat_heal.can_paid_retry`), so this can never loop or
           double-spend.
        3. **Durable reason.** Whatever is still unsubstantiated after the
           re-ask gets an append-only per-symbol `exit_refusal` row
           (`code=unsubstantiated_after_reask`, `layer=exit_trigger`) and
           a heal-FAILED owner alert, exactly as a failed research heal
           does.

        **What this does NOT do: it does not drop the exit.** `dropped` is
        False on every row this method writes. The call site's disclosed
        reasoning is that stranding the desk in a losing position is
        strictly worse than an uncheckable claim passing, so an
        unsubstantiated exit still reaches the remaining gates. What has
        changed is that it is no longer UNREPORTABLE, and that the trigger
        is now in a field — so `holding_discipline_claim_check` can be
        pointed at the record the claim is about and reach a PROVABLY
        FALSE verdict, which does block and does alert. Three-valued as
        ratified 2026-09-04: false blocks, unverifiable logs, ok passes.
        """
        from src.cost_circuit import PaidAnalysisSuspended
        from src.risk.exit_refusal import record_exit_refusal
        from src.risk.exit_trigger import (
            CODE_UNSUBSTANTIATED_AFTER_REASK, CODE_UNSUBSTANTIATED_TRIGGER,
            REASK_DIRECTIVE, check_exit_trigger,
        )
        from src.seat_heal import (
            HealResult, HEAL_CAP_BLOCKED, HEAL_FAILED, HEAL_PAID_RETRY,
            can_paid_retry, record_paid_retry,
        )
        SEAT = "position_reviewer_exit_trigger"

        def _classify(actions):
            """{SYMBOL: check} for every exit needing substantiation, and
            the count of actions the mechanical heal fixed."""
            pending, healed = {}, 0
            for a in actions or []:
                check = check_exit_trigger(
                    action=getattr(a, "action", None),
                    exit_trigger=getattr(a, "exit_trigger", None),
                    trigger_evidence=getattr(a, "trigger_evidence", ""),
                    reason=getattr(a, "reason", ""),
                    symbol=getattr(a, "symbol", "") or "",
                )
                if check.healed and check.trigger is not None:
                    # Persist the heal on the object the executor reads, so
                    # the downstream fact-check sees a named trigger rather
                    # than re-deriving it from prose a second time.
                    try:
                        a.exit_trigger = check.trigger
                        healed += 1
                    except Exception as e:  # noqa: BLE001
                        logger.warning(
                            "exit trigger: could not write healed trigger "
                            "on %s (%s)", getattr(a, "symbol", "?"), e,
                        )
                if check.needs_reask:
                    pending[(getattr(a, "symbol", "") or "").upper()] = check
            return pending, healed

        if review is None:
            return review
        pending, healed = _classify(getattr(review, "actions", None))
        if healed:
            logger.info(
                "exit trigger: mechanically healed %d exit trigger(s) from "
                "the reason prose — no trigger invented", healed,
            )
        if not pending:
            return review

        for sym, check in sorted(pending.items()):
            logger.warning("exit trigger: %s", check.finding)
            record_exit_refusal(
                self.db, symbol=sym, run_id=run_id, action="EXIT",
                code=CODE_UNSUBSTANTIATED_TRIGGER, dropped=False,
                detail=str(check.finding or "")[:400], layer="exit_trigger",
            )

        retries = dict(getattr(ctx, "heal_paid_retries", None) or {})
        if not can_paid_retry(retries, SEAT):
            logger.warning(
                "exit trigger: the one re-ask for this seat is already "
                "spent this session — %s stay(s) unsubstantiated and "
                "recorded", ", ".join(sorted(pending)),
            )
            return review
        try:
            self._require_paid_analysis("position_reviewer")
        except PaidAnalysisSuspended as exc:
            self._record_heal(ctx, HealResult(
                seat=SEAT, outcome=HEAL_CAP_BLOCKED,
                reason=f"spend cap blocked the exit-trigger re-ask: {exc}",
                details={"symbols": sorted(pending)},
            ), alert=True)
            return review

        ctx.heal_paid_retries = record_paid_retry(retries, SEAT)
        challenge = REASK_DIRECTIVE + ", ".join(sorted(pending))
        try:
            reasked, reask_result = self.position_reviewer.review(
                **{**review_kwargs, "substantiation_challenge": challenge},
            )
        except Exception as exc:  # noqa: BLE001
            self._record_heal(ctx, HealResult(
                seat=SEAT, outcome=HEAL_FAILED,
                reason=f"exit-trigger re-ask raised: {exc}",
                paid_retry=True, details={"symbols": sorted(pending)},
            ), alert=True)
            return review

        try:
            self.db.insert_agent_log(
                agent_name="position_reviewer", run_id=run_id,
                input_summary=f"exit-trigger re-ask | {', '.join(sorted(pending))}",
                input_message=reask_result.user_message,
                output_summary=(
                    reasked.overall_assessment if reasked else "parse_error"
                ),
                full_response=reask_result.raw_text,
                model=reask_result.model,
                tokens_used=reask_result.tokens_used,
                input_tokens=reask_result.input_tokens,
                output_tokens=reask_result.output_tokens,
                cost_usd=reask_result.cost_usd,
                **agent_log_kwargs(reask_result),
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("exit trigger: re-ask log write failed: %s", e)

        if reasked is None:
            self._record_heal(ctx, HealResult(
                seat=SEAT, outcome=HEAL_FAILED,
                reason="exit-trigger re-ask returned no parseable review",
                paid_retry=True, details={"symbols": sorted(pending)},
            ), alert=True)
            return review

        # Merge the re-answered actions for the CHALLENGED symbols only.
        # Every other action stays exactly as first answered: the re-ask
        # asked one question and is not an opportunity to re-decide the
        # rest of the book.
        replacements = {
            (getattr(a, "symbol", "") or "").upper(): a
            for a in (getattr(reasked, "actions", None) or [])
            if (getattr(a, "symbol", "") or "").upper() in pending
        }
        merged = [
            replacements.get((getattr(a, "symbol", "") or "").upper(), a)
            for a in (getattr(review, "actions", None) or [])
        ]
        review.actions = merged
        still, _ = _classify(merged)
        logger.info(
            "exit trigger: re-ask answered %d of %d challenged symbol(s); "
            "%d still unsubstantiated",
            len(replacements), len(pending), len(still),
        )

        if not still:
            self._record_heal(ctx, HealResult(
                seat=SEAT, outcome=HEAL_PAID_RETRY,
                reason="exit-trigger re-ask substantiated every challenged exit",
                paid_retry=True, usable=True,
                details={"symbols": sorted(pending)},
            ), alert=False)
            return review

        for sym, check in sorted(still.items()):
            logger.error(
                "exit trigger: %s STILL unsubstantiated after the re-ask. "
                "The exit is NOT dropped on this ground — stranding the "
                "desk in a losing position is worse than an uncheckable "
                "claim passing — but it is recorded and the named trigger "
                "is now fact-checked against the desk's own records. %s",
                sym, check.finding,
            )
            record_exit_refusal(
                self.db, symbol=sym, run_id=run_id, action="EXIT",
                code=CODE_UNSUBSTANTIATED_AFTER_REASK, dropped=False,
                detail=str(check.finding or "")[:400], layer="exit_trigger",
            )
        self._record_heal(ctx, HealResult(
            seat=SEAT, outcome=HEAL_FAILED,
            reason=(
                "exit trigger still unsubstantiated after the one re-ask "
                "for: " + ", ".join(sorted(still)) + ". Exits not dropped "
                "on this ground; recorded per symbol."
            ),
            paid_retry=True, details={"symbols": sorted(still)},
        ), alert=True)
        return review

    def _holding_discipline_check_for_exit(
        self,
        *,
        symbol: str,
        action: str,
        reason: str,
        positions,
        run_id: str,
        position_history: dict | None = None,
        exit_trigger=None,
    ):
        """Fact-check ONE midday/close exit's hard-trigger claim, using the
        same deterministic checker the morning Portfolio-Manager path uses.

        2026-09-11. `_reason_cites_hard_trigger` is a SUBSTRING MATCH and
        has never been anything else: it forces the reason to make a CLAIM
        ("a regime shift happened"), and until this landed nothing on the
        midday/close surface ever asked whether the claim was TRUE.
        `src/risk/exit_guard.holding_discipline_claim_check` — built for
        exactly that question, and live on the morning PM path since
        2026-09-03/04 (`RiskStage.run`, "Holding-discipline compliance") —
        was imported from that one call site and nowhere else, so the
        desk's two BUSIEST exit surfaces ran on the words alone.

        This method only ASSEMBLES the inputs; the verdict semantics are
        the checker's and are deliberately not re-decided here. The caller
        drops the exit on `check.blocks` (PROVABLY FALSE) and lets an
        UNVERIFIABLE verdict through — absence of proof is not proof, and
        blocking an exit we merely cannot check would strand the desk in a
        losing position, which is strictly worse than the gap being closed.

        Inputs this path can supply, and how:
          - `protected`: YES, in full. `_structural_protection_for_holding`
            already lives on this class (it is the same method RiskStage
            calls) and its entry context — `thesis_invalid_if`,
            `entry_price`, `stop_loss` — comes from
            `_build_position_history`, the same DB-backed builder the
            morning path reads through `ctx.position_history`. No LLM and
            no morning-only state is involved in either.
          - `active_state_changes`: YES, identical. `_build_active_state_changes`
            is a plain news-store read on this class; RiskStage calls the
            very same method.
          - `macro_regime_today` / `macro_status`: PARTIALLY, and honestly
            so. No macro analyst runs at midday or close, so there is no
            fresh read to pass. `_carry_forward_macro` may return this
            MORNING's stored read (`carried_from_morning`, same session)
            or a GOOD prior-day regime (`remembered` until a real
            regime/print change). Only a same-session payload may falsify
            an exit claim — holding-discipline reads `.same_session`, not
            payload truthiness. When payload is None or not same-session,
            `macro_status` is passed as None and the checker's own
            UNVERIFIABLE branch handles it. Nothing is defaulted,
            substituted or invented to fill the gap: an absent or
            cross-day macro read makes a regime claim unverifiable, never
            false.

        Returns the `HoldingDisciplineClaimCheck`, or None when there is
        nothing for it to adjudicate (see the short-circuit below).
        """
        # Local imports: `pipeline_stages` imports this module, so the
        # `_macro_regime` reader (reused rather than reimplemented — it is
        # the same "MacroAnalysis or carried-forward dict" reader RiskStage
        # feeds the checker with) can only be pulled in at call time.
        from src.pipeline_stages import _macro_regime
        from src.risk.exit_guard import (
            claims_bearish_state_change,
            claims_regime_flip,
            claims_thesis_invalidation,
            holding_discipline_claim_check,
        )
        from src.risk.exit_trigger import ExitTrigger, normalize_trigger

        if str(action).upper() not in ("SELL", "REDUCE", "COVER"):
            return None
        # (b)/(c): the two claims `holding_discipline_claim_check` can
        # actually adjudicate. With neither present it returns "ok"
        # regardless of everything else it is passed, so its verdict is not
        # what the thesis branch below is here for.
        # 2026-09-18: read the claim from `PositionAction.exit_trigger`
        # when the seat filled it, and from the prose only as a fallback.
        # The prose-only version of this line is why the two real
        # 2026-09-16 exits were never adjudicated at all: their entire
        # reason was the words "adverse news", which neither regex
        # recognises, so this short-circuited to None and no fact-check of
        # any kind ran. See `src/risk/exit_trigger.py`.
        _structured = normalize_trigger(exit_trigger)
        adjudicable_claim = (
            claims_regime_flip(reason)
            or claims_bearish_state_change(reason)
            or _structured in (
                ExitTrigger.REGIME_SHIFT, ExitTrigger.BEARISH_STATE_CHANGE,
                ExitTrigger.ADVERSE_NEWS,
            )
        )
        # (a) thesis invalidation. Until 2026-09-14 this fell through the
        # short-circuit above and the structural check was NEVER consulted
        # on it — on the one exit class where "did the level backing this
        # stop actually break?" is the whole question, and the only exit
        # class for which the ATR noise band is not already redundant (21
        # of the 26 hard-trigger keywords also match
        # `EXTERNAL_INFORMATION_PATTERNS` and skip the band outright, so
        # these five are its entire non-redundant domain). The desk already
        # computes the answer; it simply was not asked here.
        # docs/WORK.md item 60.
        #
        # This branch is STRICTLY ADDITIVE and is designed so that it
        # cannot change which exits execute:
        #   - the read is taken with `persist=False`, so it can never
        #     become the prior-day half of a future confirmation and so can
        #     never lift `protected` a session earlier than it does today;
        #   - its verdict is recorded and logged, and is NOT fed to
        #     `holding_discipline_claim_check` (which still leaves (a)
        #     unjudged) and NOT returned to the caller as a verdict;
        #   - on a thesis-only reason this method still returns None,
        #     exactly as it did before, so the caller's block/allow path is
        #     byte-for-byte the behaviour it had.
        # A "the level did break" answer is corroboration for the evening
        # grade and the audit trail; an "intact" or "cannot tell" answer
        # changes nothing at all. Tightening the sell path on an intact
        # level was considered and deliberately NOT done here: it is a
        # separate, ratifiable decision, not a side effect of wiring up a
        # check that should always have been consulted.
        thesis_claim = (
            claims_thesis_invalidation(reason)
            or _structured is ExitTrigger.THESIS_INVALID
        )
        if not (adjudicable_claim or thesis_claim):
            return None

        symbol_u = (symbol or "").strip().upper()
        if position_history is None:
            position_history = {}
        hist = position_history.get(symbol) or position_history.get(symbol_u) or {}
        pos = next(
            (p for p in (positions or []) if (p.symbol or "").upper() == symbol_u),
            None,
        )
        protection = self._structural_protection_for_holding(
            symbol=symbol_u,
            thesis_invalid_if=hist.get("thesis_invalid_if"),
            entry_price=hist.get("entry_price"),
            stop_loss=hist.get("stop_loss"),
            is_short=bool(pos is not None and pos.qty < 0),
            run_id=run_id,
            # Read-only unless a (b)/(c) claim is present, i.e. unless this
            # call site would have run anyway. See `persist`'s docstring.
            persist=adjudicable_claim,
        )
        logger.info(
            "Holding-discipline structural protection for %s: protected=%s "
            "basis=%s — %s",
            symbol_u, protection.protected, protection.basis, protection.detail,
        )

        if thesis_claim:
            # Durable, per-symbol, machine-readable record of what the
            # structural check actually said about a thesis-invalidation
            # exit — the answer this surface used to discard. Written as
            # append-only specialist evidence rather than into
            # `intraday_evaluations`, whose (symbol, run_id) upsert would
            # let this observation overwrite, or be overwritten by, a real
            # gate's verdict for the same symbol and run.
            corroborated = not protection.protected
            logger.info(
                "Thesis-invalidation exit %s %s: structural check says "
                "%s (basis=%s). Recorded, not acted on — this observation "
                "neither blocks nor releases the exit. %s",
                action, symbol_u,
                "the backing level HAS broken (exit corroborated)"
                if corroborated else
                "the backing level is INTACT (exit not corroborated)",
                protection.basis, protection.detail,
            )
            try:
                self.db.insert_specialist_evidence(
                    run_id=run_id, agent_name="risk_manager",
                    kind="thesis_invalidation_structural_check",
                    scope="symbol", symbol=symbol_u,
                    evidence_json=_json.dumps({
                        "action": str(action).upper(),
                        "protected": bool(protection.protected),
                        "raw_broken": bool(protection.raw_broken),
                        "basis": protection.basis,
                        "detail": str(protection.detail)[:400],
                        "corroborates_exit": corroborated,
                        "reason": str(reason)[:400],
                        "advisory_only": True,
                    }),
                )
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    "thesis-invalidation structural check: evidence write "
                    "failed for %s (%s) — the check still ran and is in "
                    "the log above", symbol_u, e,
                )

        if not adjudicable_claim:
            # Nothing for `holding_discipline_claim_check` to adjudicate:
            # it would return "ok" for any (a)-only reason. Same None the
            # caller received before this branch existed.
            return None

        # This morning's macro read, or nothing. `_carry_forward_macro` is
        # already the producer of the `carried_from_morning` status
        # elsewhere in this class (see the intraday-scan data_status block),
        # so the label is reused rather than a second one invented.
        # Cross-day remembered regime is usable for the PM but is NOT
        # proof about today — only a same-session payload may falsify an
        # exit claim.
        carried_macro = self._carry_forward_macro()
        if carried_macro.same_session and carried_macro.payload is not None:
            macro_regime_today = _macro_regime(carried_macro.payload)
            macro_status = (
                carried_macro.status
                if carried_macro.status == "carried_from_morning"
                else "carried_from_morning"
            )
        else:
            macro_regime_today = None
            macro_status = None

        try:
            active_state_changes = self._build_active_state_changes()
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "holding discipline: state-change lookup failed (%s) — "
                "bearish-state-change claims go unverified for %s",
                e, symbol_u,
            )
            active_state_changes = ""

        return holding_discipline_claim_check(
            action=action,
            reason=reason,
            symbol=symbol_u,
            protected=protection.protected,
            macro_regime_today=macro_regime_today,
            macro_status=macro_status,
            active_state_changes=active_state_changes,
            exit_trigger=exit_trigger,
        )

    def _trail_tightened_recently(self, symbol: str, calendar_days: int = 4) -> bool:
        """True when a non-canceled TRAIL_STOP for `symbol` landed within the
        last `calendar_days` days (~2 trading days across a weekend).

        RC1 forensics (2026-07-16): the reviewer's ≥1.02×old_stop min-bump
        rule means every ACCEPTED trail tightens ≥2%; per-session trailing
        marched stops into the daily-noise band in 3-4 sessions (GE was
        ratcheted 325→350 in 8 sessions on one flag). A cooldown makes
        tightening a considered, at-most-every-other-day act.
        """
        try:
            rows = self.db.get_trades(symbol=symbol, limit=10)
        except Exception as e:  # noqa: BLE001
            logger.warning("trail cooldown query failed for %s: %s", symbol, e)
            return False
        from datetime import datetime as _dt, timedelta, timezone
        cutoff = _dt.now(timezone.utc) - timedelta(days=calendar_days)
        for row in rows:
            if (row.get("action") or "").upper() != "TRAIL_STOP":
                continue
            # NOTE (audit round 2): no fill_status filter here. A TRAIL_STOP
            # row is only written AFTER the broker accepted the replace, so
            # fill_status='canceled' means accepted-then-superseded (a later
            # trail replaced this stop) — the tighten still happened and is
            # still cooldown evidence. Skipping canceled rows silently
            # disabled the cooldown for exactly the ratchet chains it exists
            # to stop.
            # Ex-div adjustments also write TRAIL_STOP rows, but they LOWER
            # the stop (dividend-drop compensation) — counting them as a
            # "tighten" would hand every dividend payer a spurious cooldown.
            # Same idiom as the ex-div idempotence check.
            if "ex-div" in (row.get("reasoning") or "").lower():
                continue
            ts = row.get("timestamp") or ""
            try:
                dt = _dt.fromisoformat(ts.replace("Z", "+00:00")) if "T" in ts \
                    else _dt.strptime(ts, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
            except (TypeError, ValueError):
                continue
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            if dt >= cutoff:
                return True
        return False

    def _apply_deterministic_trails(self, positions, *, run_id: str) -> list[dict]:
        """Raise stops arithmetically, before the LLM is asked anything — 3.7.

        Trailing is arithmetic. Running it here means the reviewer's
        discretionary `TRAIL_STOP` becomes an override for the unusual case
        rather than the only mechanism, and the stop a winner rides up behind
        no longer depends on a model remembering to propose it.

        Every proposal is bounded by `src/risk/trailing.py`: ratchet upward
        only, a minimum move worth an order, and never inside one ordinary
        day's range. Returns the broker orders placed.

        Every evaluation also names WHY its position did or did not trail,
        and that reason is written to `specialist_evidence`
        (`kind='trail_state'`) whenever it differs from the last one on file
        for that stock — so a stop that has never trailed has a findable
        reason, without a row per stock per tick. Recording only: nothing
        here reads the record back to decide anything but whether to write.
        """
        from src.execution.stop_records import (
            recorded_initial_stop, replace_stop_and_record,
        )
        from src.execution.exit_path_records import (
            last_trail_states, record_trail_state_if_changed,
        )
        from src.risk.trailing import TRAIL_CODE_TRAILED, evaluate_trailing_stop

        orders: list[dict] = []
        last_codes = last_trail_states(
            self.db, [getattr(p, "symbol", "") for p in positions],
        )

        def _note(symbol: str, code: str, detail: str = "", **facts) -> None:
            record_trail_state_if_changed(
                self.db, last_codes, run_id=run_id, symbol=symbol,
                code=code, detail=detail, **facts,
            )

        try:
            from src.execution.scale_in import pending_protection_symbols
            pending_syms = pending_protection_symbols(self.db)
        except Exception:  # noqa: BLE001
            pending_syms = set()
        for position in positions:
            symbol = position.symbol
            if symbol in pending_syms:
                logger.info(
                    "trail: skipping %s — a protection-restore WAL row is "
                    "in flight (scale-in or sell); replacing the stop now "
                    "would race the cancel/rearm sequence",
                    symbol,
                )
                _note(symbol, "protection_restore_in_flight")
                continue
            try:
                buy = self.db.get_symbol_last_buy(symbol)
            except Exception as e:  # noqa: BLE001
                logger.warning("trail: last-buy lookup failed for %s: %s", symbol, e)
                _note(symbol, "opening_row_lookup_failed", str(e))
                continue
            if not buy:
                _note(symbol, "no_opening_buy_row")
                continue
            try:
                current_stop = self.broker.get_current_stop_price(symbol)
            except Exception as e:  # noqa: BLE001
                logger.warning("trail: stop lookup failed for %s: %s", symbol, e)
                _note(symbol, "live_stop_lookup_failed", str(e))
                continue

            # Only bars SINCE ENTRY matter: a swing low from before the
            # position existed is not a level this trade ever defended.
            bars = []
            try:
                all_bars = self.market.get_ohlcv(symbol, 120) or []
                entry_ts = (buy or {}).get("timestamp") or ""
                entry_day = entry_ts[:10]
                bars = [
                    b for b in all_bars
                    if not entry_day or str(getattr(b, "date", ""))[:10] >= entry_day
                ]
            except Exception as e:  # noqa: BLE001
                logger.warning("trail: bar fetch failed for %s: %s", symbol, e)

            evaluation = evaluate_trailing_stop(
                symbol=symbol,
                setup_type=(buy or {}).get("setup_type"),
                entry=position.avg_entry,
                current_price=position.current_price,
                current_stop=current_stop,
                reference_target=(buy or {}).get("take_profit"),
                bars=bars,
                atr=self._atr_for_symbol(symbol),
                # Shorts-safe (Stage 2): `qty` supplies only the side so a
                # short's trail mirrors instead of running the long formula
                # backwards. `get_symbol_last_buy` above only ever returns a
                # BUY row, so a short is filtered out before this point
                # regardless — this is forward-compatible plumbing, not a
                # behaviour change on today's long-only book.
                qty=position.qty,
                # Fix #3 (2026-09-04 audit): the ENTRY stop, never the live
                # one. `initial_stop_loss` is frozen at insert / first
                # write-back; `stop_loss` itself is the live recorded level
                # after a trail. Powers the Type A +1R breakeven ratchet.
                initial_stop=recorded_initial_stop(buy),
            )
            proposal = evaluation.proposal
            if proposal is None:
                _note(
                    symbol, evaluation.code,
                    current_stop=current_stop,
                    current_price=position.current_price,
                    entry=position.avg_entry,
                    setup_type=(buy or {}).get("setup_type"),
                )
                continue
            logger.info("Deterministic trail: %s", proposal.reason)
            try:
                from src.execution.stop_records import accepted_stop_order
                order = replace_stop_and_record(
                    self.broker, self.db, symbol, proposal.new_stop,
                )
            except Exception as e:  # noqa: BLE001
                logger.error(
                    "trail: replace_stop_loss failed for %s (%s) — the OLD "
                    "stop remains in force", symbol, e,
                )
                _note(
                    symbol, "replace_raised", str(e),
                    proposed_stop=proposal.new_stop, current_stop=current_stop,
                )
                continue
            if not order or (
                isinstance(order, dict) and not accepted_stop_order(order)
            ):
                _note(
                    symbol, "replace_not_accepted",
                    str((order or {}).get("status") or "") if isinstance(order, dict) else "",
                    proposed_stop=proposal.new_stop, current_stop=current_stop,
                )
                continue
            _note(
                symbol, TRAIL_CODE_TRAILED, proposal.reason,
                proposed_stop=proposal.new_stop, current_stop=current_stop,
            )
            if isinstance(order, dict):
                order.setdefault("action", "TRAIL_STOP")
            orders.append(order)
            try:
                self.db.insert_trade(
                    symbol=symbol, action="TRAIL_STOP", qty=position.qty,
                    price=proposal.new_stop, reasoning=proposal.reason,
                    run_id=run_id,
                    stop_loss=proposal.new_stop,
                )
            except Exception as e:  # noqa: BLE001
                logger.warning("trail: trade row write failed for %s: %s", symbol, e)
        return orders

    def _exit_event_risk_block(self, symbols: list[str]) -> str:
        """The fetched Event Risk section for an EXIT review.

        `RiskVerdict.reasoning_chain.event_risk` is a mandatory output field.
        The morning path fetches its answer (`RiskStage._build_event_risk_block`);
        this path passed nothing at all, so the renderer's NOT FETCHED fallback
        fired on all three sub-blocks and a mandatory question had no input.

        Earnings proximity IS fetchable here — `self.market` exists on the
        midday/close loop and the sweep is bounded per-symbol and in aggregate
        by the same `config.event_risk` timeouts the morning path uses. The
        macro-release and FOMC calendars are NOT: they are fetched by the
        morning research stage and no equivalent runs on this loop, so they
        render as the labelled NOT FETCHED form, which is the honest answer.

        Never raises. Any failure degrades to the fully-NOT-FETCHED block —
        an absent section reads as a calm calendar, which is the failure the
        block exists to prevent.
        """
        from src.data.event_calendar import (
            fetch_earnings_proximity, format_event_risk_block,
        )

        event_cfg = getattr(getattr(self, "config", None), "event_risk", None)
        horizon_days = getattr(event_cfg, "horizon_days", 10)
        earnings = None
        try:
            if symbols and getattr(self, "market", None) is not None:
                earnings = fetch_earnings_proximity(
                    self.market, symbols,
                    per_symbol_timeout_s=getattr(
                        event_cfg, "earnings_symbol_timeout_s", 8.0,
                    ),
                    total_deadline_s=getattr(event_cfg, "earnings_deadline_s", 20.0),
                )
        except Exception as e:  # noqa: BLE001
            logger.warning("Exit review: earnings proximity sweep failed: %s", e)
            earnings = None
        try:
            return format_event_risk_block(
                earnings=earnings, events=None, coverage=None,
                horizon_days=horizon_days,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("Exit review: event-risk block render failed: %s", e)
            return format_event_risk_block(
                earnings=None, events=None, coverage=None, horizon_days=0,
            )

    def _record_exit_refusal(
        self, *, symbol: str, run_id: str, action: str, code: str,
        dropped: bool, detail: str, layer: str,
    ) -> None:
        """Append-only per-symbol refusal/uncertainty record. Never raises."""
        from src.risk.exit_refusal import record_exit_refusal
        record_exit_refusal(
            self.db, symbol=symbol, run_id=run_id, action=action,
            code=code, dropped=dropped, detail=detail, layer=layer,
        )

    def _risk_review_exits(
        self, review, positions, *, run_id: str, total_value: float,
        macro_summary: dict | None = None, position_facts: dict | None = None,
        news_intel=None, earnings_analyses: list | None = None,
        cash: float | None = None, reserve_balance: float = 0.0,
        recent_performance: dict | None = None,
    ):
        """Put the reviewer's exits in front of the AI Risk Manager — Phase 3.4.

        `AGENTS.md` states the chain as `Specialists -> Portfolio Manager ->
        AI Risk -> deterministic Python -> broker`, **for exits as well as
        entries**. Until this landed, `run_position_review` called only
        `position_reviewer` and then executed, so the entire sell side skipped
        the veto layer the buy side has always had.

        Returns `(vetoed_symbols, verdict_or_None)`. Symbols in the returned
        set are dropped by the caller.

        **Failure posture: FAIL OPEN on uncertainty.** An unparseable or
        errored Risk Manager lets the exits through, logged loudly. This
        deliberately differs from the entry path, which fails closed with
        zero orders (`RiskStage`). The asymmetry is intentional and
        owner-ratified (2026-08-27):
        - failing closed on an ENTRY means not buying, which costs nothing;
        - failing closed on an EXIT means a thesis-invalidated position cannot
          be closed because a language model is unavailable, and the loss is
          then bounded only by the broker stop.

        **Item 60 (2026-09-16) — one owner, one uncertainty direction.**
        Deterministic Python owns refusal. A completed "no named trigger"
        is that owner's drop, not an uncertainty fail; those exits are
        not sent to this seat (the executor still enforces). Uncertainty
        — this seat unavailable/unparseable/verdict-less, or the
        hard-trigger recogniser itself unable to run — fails OPEN on
        both layers, which is the 2026-08-27 ratification applied to the
        pair. AI Risk remains a challenge seat: a parseable reject still
        drops, and an approval cannot override a deterministic drop.
        Every drop and every uncertainty fail-open writes an append-only
        per-symbol reason (`src/risk/exit_refusal.py`).

        The fact gates — the noise band, the metric-contradiction veto and
        `holding_discipline_claim_check` — remain the LAST LINE on data,
        not on word-recognition. Each abstains somewhere: the trigger
        gate checks the words, not the truth of the claim; the noise
        band is bypassed by any reason citing external information (which
        the trigger gate all but requires); the metric veto needs recorded
        prior metrics for that symbol or it does not run; and the claim
        check looks only at a regime-flip or HIGH-conviction-bearish
        claim, only on a still-protected position, passing every
        unverifiable claim by design. A plausibly-worded, deterministically-
        clean, wrong exit passes those. That gap is what this seat is for.

        **Ordering.** Named-trigger filtering now happens in this method
        before the model is called, so a dead Risk Manager cannot
        fail-open an exit the owner already refused. The other fact gates
        still live in `_midday_execute_llm_actions`, which the caller
        invokes AFTER this method; they still run on every surviving exit
        before any order can reach the broker.

        **The verdict's only live effect here is `rejected_symbols`.**
        `modifications` and `scale_all_buys` are applied by
        `_apply_risk_modifications`, which is called ONLY from the morning
        `RiskStage` (`src/pipeline_stages.py`); this method returns a veto set
        and reads neither.

        **Since 2026-09-14 they are no longer emitted at all here.** The seat
        answers `ExitRiskVerdict`, which is `RiskVerdict` without those two
        fields, and `ExitRiskReasoningChain`, which drops the `min_length=1`
        demand from the three chain steps this path's own prompt already
        stands down or inverts (`rr_audit`, `sizing_sanity`, `event_risk`).
        Telling the seat a lever is discarded still spent its judgement on the
        lever; the fix is to stop asking. Nothing about the morning BUY path
        changed — `RiskVerdict` and `RiskReasoningChain` are untouched and all
        six morning chain steps remain mandatory.

        **What this seat is shown (2026-09-13).** It is told explicitly that it
        is on the EXIT path (`review_mode`), so the renderer no longer stamps
        the Portfolio Manager's `continuity_check` / `premortem_check` with a
        NOT-PERFORMED banner for a chain that has never had those fields, and
        no longer asks it to verify a claim against a Tech block that no call
        on this loop produces. Everything the loop genuinely has — news,
        earnings, deployable cash and the parked reserve, drawdown state,
        holding ages, and a fetched earnings-proximity sweep — is now passed.
        See `src/agents/risk_review_mode.py`.
        """
        from src.agents import risk_review_mode
        from src.models import (
            ExitReviewChain, PortfolioDecision, TradeDecision,
        )
        from src.risk.exit_refusal import (
            CODE_AI_RISK_REJECT,
            CODE_AI_RISK_UNAVAILABLE,
            CODE_HARD_TRIGGER_UNCERTAIN,
            CODE_UNRECOGNIZED_TRIGGER,
            classify_trigger_reason,
        )

        # COVER is the short-side twin of SELL/REDUCE (Stage 3 shorts gap
        # fix): a short's exit must reach the AI Risk Manager exactly like a
        # long's does, not skip it.
        exits = [
            a for a in (review.actions if review else [])
            if a.action in ("SELL", "REDUCE", "COVER")
        ]
        if not exits:
            return set(), None

        held = {p.symbol.upper(): p for p in positions}
        decisions: list[TradeDecision] = []
        original_action_by_symbol: dict[str, str] = {}
        for action in exits:
            symbol = action.symbol.upper()
            if symbol not in held:
                continue
            # Item 60: unnamed-trigger exits are the deterministic owner's
            # completed refusal. Do not spend a Risk Manager call on them,
            # and do not let a dead/unparseable model fail-OPEN a sale the
            # owner already refused. Record here so the skip is durable if
            # execute is not reached; the executor still drops.
            judgment = classify_trigger_reason(
                action.reason, cites=_reason_cites_hard_trigger,
            )
            if judgment == "unnamed":
                logger.info(
                    "AI Risk exit review: not sending %s %s — reason names "
                    "no recognised trigger; deterministic owner refuses "
                    "before the challenge seat.",
                    action.action, symbol,
                )
                self._record_exit_refusal(
                    symbol=symbol, run_id=run_id, action=action.action,
                    code=CODE_UNRECOGNIZED_TRIGGER, dropped=True,
                    detail=str(action.reason or "")[:400],
                    layer="hard_trigger",
                )
                continue
            if judgment == "uncertain":
                logger.error(
                    "AI Risk exit review: hard-trigger recogniser raised "
                    "on %s %s — failing OPEN on that gate, sending the "
                    "exit to the challenge seat. Reason was: %r",
                    action.action, symbol, str(action.reason)[:200],
                )
                self._record_exit_refusal(
                    symbol=symbol, run_id=run_id, action=action.action,
                    code=CODE_HARD_TRIGGER_UNCERTAIN, dropped=False,
                    detail=str(action.reason or "")[:400],
                    layer="hard_trigger",
                )
            original_action_by_symbol[symbol] = action.action
            # A COVER must be presented to the RM as a COVER, not relabeled
            # SELL — TradeDecision has a real "COVER" literal (the PM/
            # ExecutionStage decision path already uses it), and mislabeling
            # a short's exit as a stock sale is exactly the "reads a winning
            # short as a loser" failure this fix exists to close.
            decisions.append(TradeDecision(
                action="SELL" if action.action in ("SELL", "REDUCE") else "COVER",
                symbol=symbol,
                # 100 = full exit (SELL and COVER are both full closes on
                # this path); REDUCE is a partial whose exact fraction the
                # executor derives. The RM is being asked to judge WHETHER the
                # exit is sound, not to re-size it.
                allocation_pct=100.0 if action.action in ("SELL", "COVER") else 50.0,
                entry_price=0.0, stop_loss=0.0, take_profit=0.0,
                reasoning=str(action.reason or "")[:500],
            ))
        if not decisions:
            return set(), None

        summary = (review.overall_assessment or "")[:400]
        # The position reviewer's chain travels in the PM's `ReasoningChain`
        # container because that is the container the Risk Manager reads. Only
        # the five fields the reviewer actually authors are populated, and
        # `risk_review_mode` renders them under the reviewer's OWN labels.
        #
        # No `or "n/a"` and no cross-reference strings. Both were placeholder
        # content invented at this call site for fields the reviewer never
        # filled, and a fabricated "n/a" reads to the seat as a real answer —
        # which is how this whole defect started. `ReasoningChain` still
        # requires a non-empty string in each core field, so the substitute is
        # `risk_review_mode.NOT_AUTHORED`, which says exactly what happened.
        rc = review.reasoning_chain

        def _step(value: str) -> str:
            return (value or "").strip()[:800] or risk_review_mode.NOT_AUTHORED

        proposal = PortfolioDecision(
            # `ExitReviewChain`, not `ReasoningChain`: `news_check` is a
            # PM-schema field with no counterpart here and is not rendered to
            # the seat on this path, but the parent makes it `min_length=1`,
            # so it was being filled with a placeholder string that existed
            # only to satisfy the constraint. The subclass relaxes that one
            # field for this path alone; the morning chain is untouched.
            reasoning_chain=ExitReviewChain(
                macro_filter=_step(rc.macro_continuity_check),
                # Slot reuse, not a category claim: `earnings_check` is PM's
                # field name, and `risk_review_mode` labels this row
                # "Thesis progress check" — the reviewer's own field — in the
                # rendered message. Nothing about earnings is implied.
                earnings_check=_step(rc.thesis_progress_check),
                signal_conflicts=_step(rc.thesis_integrity_check),
                sizing_logic=_step(rc.execution_rationale),
                portfolio_balance=_step(rc.winners_discipline_check),
                cash_target=_step(rc.session_disposition_check),
            ),
            decisions=decisions,
            portfolio_view=f"EXIT REVIEW (position reviewer): {summary}",
        )

        # Holding ages. Informational on this path (protection is decided by
        # `check_structural_protection`, not by age) but the renderer prints
        # "held: unknown" without it, and unknown-by-omission is exactly the
        # kind of silent gap this fix exists to remove.
        try:
            exit_position_history = self._build_position_history(positions)
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "Exit review: position history rebuild failed — the seat sees "
                "holding ages as unknown: %s", e,
            )
            exit_position_history = {}

        try:
            verdict, rm_result = self.risk_manager.review(
                portfolio_decision=proposal,
                positions=positions,
                macro_summary=macro_summary or {},
                rule_violations=[],
                total_value=total_value,
                heat=self._build_portfolio_heat(positions, total_value),
                # Everything below was available at this call site all along
                # and simply was not passed. The seat was being asked to audit
                # exits against news, earnings, drawdown state and event risk
                # while being shown none of them.
                news_intel=news_intel,
                earnings_analyses=earnings_analyses or [],
                cash=cash,
                reserve_balance=reserve_balance or 0.0,
                recent_performance=recent_performance or {},
                position_history=exit_position_history,
                event_risk_block=self._exit_event_risk_block(
                    sorted({d.symbol for d in decisions})
                ),
                # Tells the renderer which review this is. Without it the
                # exit path is rendered as a morning plan and the seat is told
                # two audit steps were skipped that do not exist here.
                review_mode=risk_review_mode.EXIT_REVIEW,
            )
        except Exception as e:  # noqa: BLE001
            logger.error(
                "AI Risk exit review RAISED (%s) — failing OPEN: %d exit(s) "
                "proceed unreviewed. Named-trigger exits already passed the "
                "deterministic owner; unnamed exits were not sent here.",
                e, len(decisions),
            )
            for d in decisions:
                self._record_exit_refusal(
                    symbol=d.symbol, run_id=run_id,
                    action=original_action_by_symbol.get(d.symbol, d.action),
                    code=CODE_AI_RISK_UNAVAILABLE, dropped=False,
                    detail=f"risk manager raised: {e}"[:400],
                    layer="ai_risk",
                )
            return set(), None

        try:
            self.db.insert_agent_log(
                agent_name="risk_manager", run_id=run_id,
                input_summary=f"exit review: {len(decisions)} exit(s)",
                input_message=rm_result.user_message,
                output_summary=f"Approved: {verdict.approved if verdict else 'error'}",
                full_response=rm_result.raw_text,
                model=rm_result.model,
                tokens_used=rm_result.tokens_used,
                input_tokens=rm_result.input_tokens,
                output_tokens=rm_result.output_tokens,
                cost_usd=rm_result.cost_usd,
                status="agent_failure" if verdict is None else "ok",
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("AI Risk exit review: agent log write failed: %s", e)

        if verdict is None:
            logger.error(
                "AI Risk exit review returned no verdict — failing OPEN: "
                "%d exit(s) proceed unreviewed.", len(decisions),
            )
            for d in decisions:
                self._record_exit_refusal(
                    symbol=d.symbol, run_id=run_id,
                    action=original_action_by_symbol.get(d.symbol, d.action),
                    code=CODE_AI_RISK_UNAVAILABLE, dropped=False,
                    detail="risk manager returned no verdict",
                    layer="ai_risk",
                )
            return set(), None

        # Phase 10.1 — the same granularity split as the morning plan, on the
        # exit side: `approved=False` still vetoes EVERY exit (the book is
        # what failed), while a per-symbol refusal vetoes only the exit it
        # names and lets the other exits through. Empty `rejected_symbols`
        # (every historical verdict, and any model that never emits the
        # field) reproduces the previous behaviour exactly.
        rejections = verdict.rejections_by_symbol()
        if verdict.approved:
            veto_reasons = {
                d.symbol: rejections[d.symbol.strip().upper()]
                for d in decisions if d.symbol.strip().upper() in rejections
            }
            if not veto_reasons:
                logger.info(
                    "AI Risk approved %d exit(s): %s",
                    len(decisions), (verdict.reasoning or "")[:200],
                )
                self._record_exit_review_approvals(
                    decisions, set(), verdict, run_id=run_id,
                    original_action_by_symbol=original_action_by_symbol,
                )
                return set(), verdict
        else:
            veto_reasons = {d.symbol: (verdict.reasoning or "") for d in decisions}

        vetoed = set(veto_reasons)
        logger.warning(
            "AI Risk REJECTED %d of %d exit(s) %s — holding instead. Reason: %s",
            len(vetoed), len(decisions), sorted(vetoed),
            (verdict.reasoning or "")[:300],
        )
        for symbol in sorted(vetoed):
            try:
                self.db.record_intraday_evaluation(
                    symbol=symbol, run_id=run_id,
                    status="exit_vetoed_by_ai_risk",
                    detail=(veto_reasons[symbol] or "")[:400],
                )
            except Exception as e:  # noqa: BLE001
                logger.warning("AI Risk exit review: audit write failed: %s", e)
            self._record_exit_refusal(
                symbol=symbol, run_id=run_id,
                action=original_action_by_symbol.get(symbol, "SELL"),
                code=CODE_AI_RISK_REJECT, dropped=True,
                detail=(veto_reasons[symbol] or "")[:400],
                layer="ai_risk",
            )
        # The exits the seat let through beside the ones it vetoed.
        self._record_exit_review_approvals(
            decisions, vetoed, verdict, run_id=run_id,
            original_action_by_symbol=original_action_by_symbol,
        )
        return vetoed, verdict

    def _record_exit_review_approvals(
        self, decisions, vetoed: set, verdict, *, run_id: str,
        original_action_by_symbol: dict,
    ) -> None:
        """One durable per-symbol row for every exit the AI Risk seat
        APPROVED on the exit-review path. Never raises.

        Board item 164 (2026-09-19). A veto here was already durable
        (`intraday_evaluations` plus an `exit_refusal` row), but an approval
        reached `agent_logs` only — one raw model response per run, with no
        per-symbol row saying "this exit was reviewed and let through, and
        why". Written to the exit path's own per-symbol record
        (`src/risk/exit_refusal.py`), which already carries non-drop
        outcomes (`dropped=False`, the fail-open codes) — NOT to the
        `pipeline_event` stream, because `src/refusal_signature.py` counts
        any surviving `pipeline_event` as the session having taken an idea,
        and an exit is not one. `ExitRiskVerdict` has no per-symbol approval
        reason, so the detail is the seat's own run-level reasoning, marked
        as such. Recording only: the returned veto set is unchanged.
        """
        from src.risk.exit_refusal import CODE_AI_RISK_APPROVED

        category = getattr(verdict, "reason_category", None)
        for d in decisions:
            if d.symbol in vetoed:
                continue
            self._record_exit_refusal(
                symbol=d.symbol, run_id=run_id,
                action=original_action_by_symbol.get(d.symbol, d.action),
                code=CODE_AI_RISK_APPROVED, dropped=False,
                detail=(
                    f"approved by the risk seat (category {category!r}; no "
                    f"per-symbol reason in the verdict, run-level reasoning "
                    f"follows): {verdict.reasoning or ''}"
                ),
                layer="ai_risk",
            )

    def _midday_execute_llm_actions(
        self, positions, review, run_id: str,
        already_trimmed_today: set[str] | None = None,
        metric_deltas: dict | None = None,
        risk_vetoed_symbols: set[str] | None = None,
        position_facts: dict | None = None,
    ) -> list[dict]:
        """Dispatch LLM-recommended SELL / REDUCE / TRAIL_STOP / COVER actions
        to broker.

        Dedups same-symbol conflicting actions by priority (SELL/COVER >
        REDUCE > TRAIL_STOP > HOLD) to avoid the broker seeing two orders
        fighting each other on one position. (A `blocked_symbols` argument
        used to suppress LLM exits on a symbol whose midday auto-take-profit
        sell was still in flight; that rule was deleted 2026-09-12 and no
        other system sell runs ahead of the reviewer in the same session.)

        COVER is the short-side twin of SELL/REDUCE (Stage 3 shorts gap
        fix): it is the ONLY lever the reviewer has on a held short (never
        SELL — the executor requires the action to match the held side,
        see the qty-sign gate below) and it routes through every protection
        a SELL/REDUCE gets — the named-trigger phrase gate, the exit
        guard's metric-contradiction veto, the noise band, the same-day-trim
        discipline, and (further down `run_position_review`) the AI Risk
        routing via `_risk_review_exits`. It always executes as a FULL
        close (`_full_sell_qty`, mirroring SELL) — the schema
        (`PositionAction`) carries no allocation fraction for it, unlike the
        PM's `TradeDecision.allocation_pct`, so there is no partial-COVER
        signal for this path to act on.
        """
        orders: list[dict] = []
        _priority = {"SELL": 0, "COVER": 0, "REDUCE": 1, "TRAIL_STOP": 2, "HOLD": 3}
        best_by_symbol: dict[str, dict] = {}
        actions_raw = review.actions if review else []
        actions_list = [a.model_dump() for a in actions_raw]
        for ai in actions_list:
            sym = (ai.get("symbol") or "").strip().upper()
            if not sym:
                continue
            curr = best_by_symbol.get(sym)
            if curr is None or _priority.get(ai.get("action"), 99) < _priority.get(curr.get("action"), 99):
                best_by_symbol[sym] = ai
        if len(best_by_symbol) < len(actions_list):
            dropped = len(actions_list) - len(best_by_symbol)
            logger.info(
                "Midday: collapsed %d duplicate same-symbol actions "
                "(priority SELL/COVER>REDUCE>TRAIL_STOP>HOLD)", dropped,
            )

        if not best_by_symbol:
            return orders

        already_trimmed = {
            symbol.strip().upper()
            for symbol in (already_trimmed_today or set())
            if symbol and symbol.strip()
        }
        # Entry context (thesis_invalid_if / entry price / entry stop) for the
        # holding-discipline claim check below. Built ONCE and only if some
        # exit actually reaches that gate — a HOLD-only or TRAIL_STOP-only
        # review must not buy the DB reads.
        hd_position_history: dict | None = None
        for action_item in best_by_symbol.values():
            act = action_item.get("action")
            if act not in ("SELL", "REDUCE", "TRAIL_STOP", "COVER"):
                continue
            symbol = action_item.get("symbol", "")
            # Same-day trim discipline: a symbol that already had a sell-side
            # action TODAY (midday REDUCE, force-delever, etc.) is off-limits for
            # additional REDUCE / SELL on a SECOND session unless the LLM
            # explicitly cites a hard trigger in the reason. TRAIL_STOP is
            # exempt — adjusting a stop is not selling shares.
            #
            # 2026-05-04 AMZN: midday REDUCE 20 of 41 @ +12.4% on TARGET_BREACH,
            # then close REDUCE 10 of 21 @ +13.8% on the SAME TARGET_BREACH
            # flag = 73% one-day trim on a strengthening thesis. Mechanical
            # double-application of one signal violates "good stocks are meant
            # to be held".
            # Phase 3.2 — a deterioration verdict may not contradict the
            # reviewer's own recorded numbers. Vetoes ONLY a SELL/REDUCE/
            # COVER whose stated reason claims the position is stalling
            # while every metric that moved since the previous review
            # improved. Exits on new information (news, earnings, regime,
            # invalidation) are untouched, however good the numbers look —
            # see src/risk/exit_guard.py. metric_deltas is already sign-
            # corrected per symbol (see _build_position_facts), so COVER
            # needs no extra handling here.
            if act in ("SELL", "REDUCE", "COVER") and metric_deltas:
                from src.risk.exit_guard import veto_contradicted_exit
                deltas = metric_deltas.get(symbol)
                if deltas is not None:
                    veto = veto_contradicted_exit(
                        act, action_item.get("reason", ""), deltas,
                    )
                    if veto:
                        logger.warning("Exit guard: %s", veto)
                        try:
                            self.db.record_intraday_evaluation(
                                symbol=symbol, run_id=run_id,
                                status="exit_vetoed_contradicts_own_metrics",
                                detail=veto[:500],
                            )
                        except Exception as e:  # noqa: BLE001
                            logger.warning("exit guard: audit write failed: %s", e)
                        from src.risk.exit_refusal import CODE_CONTRADICTS_METRICS
                        self._record_exit_refusal(
                            symbol=symbol, run_id=run_id, action=act,
                            code=CODE_CONTRADICTS_METRICS, dropped=True,
                            detail=veto[:400], layer="metric_contradiction",
                        )
                        continue

            # Phase 3.3 — EVERY exit must name a trigger, not just the second
            # one on a symbol in a day.
            #
            # The gate below used to be conditioned on `symbol in
            # already_trimmed`, so a position's FIRST sale of the day executed
            # on soft reasoning entirely unchecked — and a first sale is almost
            # every sale. Both of the exits the evening review graded
            # "premature" on 2026-08-26 (EPD, MRVL) were first sales and sailed
            # straight through.
            #
            # Failing closed here means HOLDING, and every position carries a
            # broker-resident stop (AGENTS.md invariant 3), so the downside of
            # a wrongly-blocked exit is bounded by that stop. The downside of a
            # wrongly-allowed one is the pattern that emptied the book.
            # Phase 3.4 — the AI Risk Manager reviewed these exits and
            # rejected this one. Its authority over exits mirrors the veto it
            # has always had over entries.
            if act in ("SELL", "REDUCE", "COVER") and symbol in (risk_vetoed_symbols or set()):
                logger.warning(
                    "Position reviewer: skipping %s %s — vetoed by AI Risk",
                    act, symbol,
                )
                continue

            # Phase 3.6 — noise band on exits. A PRICE-DERIVED failure inside
            # one ATR of entry has not distinguished itself from one ordinary
            # day's range. OKLO was bought and sold on 2026-08-26 at 0.67 ATR,
            # on day zero, never given a single day's normal range to breathe.
            #
            # Triggers originating outside the tape — earnings, news, regime,
            # sector, correlation, circuit breaker, a fired stop — bypass this
            # entirely. An earnings miss is an earnings miss whether the stock
            # has moved 0.2 ATR or 3 ATR, and waiting for price confirmation
            # before acting on information sells the bottom instead of the top.
            if act in ("SELL", "REDUCE", "COVER"):
                from src.risk.exit_guard import (
                    adverse_move_is_noise, cites_external_information,
                )
                held_now = next((p for p in positions if p.symbol == symbol), None)
                reason_for_band = action_item.get("reason", "")
                # COVER's adverse direction is the mirror of SELL/REDUCE's —
                # a short is hurt by price RISING, not falling — so the
                # noise band is measured against the CLOSING side, same
                # convention as _submit_protected_sell's `side` param.
                close_side = "buy" if act == "COVER" else "sell"
                if held_now is not None and not cites_external_information(reason_for_band):
                    from src.risk.exit_guard import noise_band_atr

                    atr = self._atr_for_symbol(symbol)
                    # Phase 3.6 audit follow-up (2026-09-04, fix #1): the band
                    # widens with sqrt(sessions_held) — same convention as
                    # levels.py's target projection — instead of a flat 1.0x
                    # ATR regardless of how long the position has aged. See
                    # `exit_guard.noise_band_atr` for the rationale.
                    #
                    # 2026-09-04 audit follow-up (fix, second pass): this MUST
                    # be `sessions_held` (weekend-aware trading-session count,
                    # `trading_calendar.trading_sessions_held`), NOT the plain
                    # calendar-day `days_held` — levels.py's own precedent
                    # scales by sqrt(TRADING sessions), and a calendar-day
                    # count silently over-widens the band by sqrt(3/1) after
                    # every weekend (Friday entry reviewed Monday shows 3
                    # calendar days but only 1 real session of price action).
                    sessions_held_for_band = (position_facts or {}).get(symbol, {}).get("sessions_held")
                    if adverse_move_is_noise(
                        held_now.avg_entry, held_now.current_price, atr,
                        side=close_side, days_held=sessions_held_for_band,
                    ):
                        adverse_move = (
                            held_now.current_price - held_now.avg_entry
                            if close_side == "buy"
                            else held_now.avg_entry - held_now.current_price
                        )
                        band_multiple = noise_band_atr(sessions_held_for_band)
                        logger.warning(
                            "Position reviewer: blocking %s %s — adverse "
                            "$%.2f move from entry $%.2f, which is inside the "
                            "%.2fxATR noise band (ATR14 $%.2f, sessions_held=%s). "
                            "A price-derived failure this small has not "
                            "distinguished itself from this position's normal "
                            "range so far. External-information triggers "
                            "bypass this. Reason: %r",
                            act, symbol, adverse_move,
                            held_now.avg_entry, band_multiple, atr or 0.0,
                            sessions_held_for_band,
                            reason_for_band[:160],
                        )
                        try:
                            self.db.record_intraday_evaluation(
                                symbol=symbol, run_id=run_id,
                                status="exit_blocked_inside_atr_noise_band",
                                detail=f"{act}: {reason_for_band[:400]}",
                            )
                        except Exception as e:  # noqa: BLE001
                            logger.warning("noise band: audit write failed: %s", e)
                        from src.risk.exit_refusal import CODE_NOISE_BAND
                        self._record_exit_refusal(
                            symbol=symbol, run_id=run_id, action=act,
                            code=CODE_NOISE_BAND, dropped=True,
                            detail=f"{act}: {reason_for_band[:400]}",
                            layer="noise_band",
                        )
                        continue

            reason_text = action_item.get("reason", "")
            if act in ("SELL", "REDUCE", "COVER"):
                from src.risk.exit_refusal import (
                    CODE_HARD_TRIGGER_UNCERTAIN,
                    CODE_UNRECOGNIZED_TRIGGER,
                    classify_trigger_reason,
                )
                trigger_judgment = classify_trigger_reason(
                    reason_text, cites=_reason_cites_hard_trigger,
                )
                if trigger_judgment == "unnamed":
                    logger.warning(
                        "Position reviewer: blocking %s %s — the reason names no "
                        "recognised trigger. Exits require NEW INFORMATION "
                        "(thesis invalidation, adverse news, earnings, regime "
                        "shift, sector shock, stop hit); "
                        "price action and soft flags are not triggers. Reason "
                        "was: %r",
                        act, symbol, str(reason_text)[:200],
                    )
                    try:
                        self.db.record_intraday_evaluation(
                            symbol=symbol, run_id=run_id,
                            status="exit_blocked_no_named_trigger",
                            detail=f"{act}: {str(reason_text)[:400]}",
                        )
                    except Exception as e:  # noqa: BLE001
                        logger.warning("exit gate: audit write failed: %s", e)
                    self._record_exit_refusal(
                        symbol=symbol, run_id=run_id, action=act,
                        code=CODE_UNRECOGNIZED_TRIGGER, dropped=True,
                        detail=f"{act}: {str(reason_text)[:400]}",
                        layer="hard_trigger",
                    )
                    continue
                if trigger_judgment == "uncertain":
                    logger.error(
                        "Position reviewer: hard-trigger recogniser raised "
                        "on %s %s — failing OPEN on that gate (agent "
                        "application of the 2026-08-27 dead-model posture, "
                        "not a new owner ratification). Reason was: %r",
                        act, symbol, str(reason_text)[:200],
                    )
                    self._record_exit_refusal(
                        symbol=symbol, run_id=run_id, action=act,
                        code=CODE_HARD_TRIGGER_UNCERTAIN, dropped=False,
                        detail=f"{act}: {str(reason_text)[:400]}",
                        layer="hard_trigger",
                    )
                reason_text = (
                    reason_text if isinstance(reason_text, str) else str(reason_text or "")
                )

            # 2026-09-11 — and now: is the named trigger actually TRUE?
            #
            # The gate immediately above only proves the reason SAYS the
            # words. Until this landed that was the whole of the midday /
            # close check: "regime shift to risk-off; correlation breach
            # across the book" executed a SELL on a structurally protected
            # position on the strength of the phrasing, with no part of the
            # system ever asking whether a regime shift had happened. The
            # deterministic answer to that question already existed —
            # `exit_guard.holding_discipline_claim_check` — but was wired
            # only to the morning Portfolio-Manager path in
            # `pipeline_stages.RiskStage`. Same function here, same
            # semantics, assembled by `_holding_discipline_check_for_exit`.
            #
            # PROVABLY FALSE drops the exit (the morning path's own
            # response, mirroring the existing gates on this loop).
            # UNVERIFIABLE is recorded and ALLOWED THROUGH, unchanged from
            # the morning path and deliberately: absence of proof is not
            # proof, and refusing an exit on a claim we merely cannot check
            # would trap the desk in a losing position — a far worse
            # failure than the one being fixed. An infrastructure failure
            # inside the check fails OPEN for the same reason, matching
            # `_risk_review_exits`' disclosed posture on this path.
            if act in ("SELL", "REDUCE", "COVER"):
                if hd_position_history is None:
                    try:
                        hd_position_history = self._build_position_history(positions)
                    except Exception as e:  # noqa: BLE001
                        logger.warning(
                            "holding discipline: entry-context lookup failed "
                            "(%s) — protection is read without it this run", e,
                        )
                        hd_position_history = {}
                try:
                    hd_check = self._holding_discipline_check_for_exit(
                        symbol=symbol, action=act, reason=reason_text,
                        positions=positions, run_id=run_id,
                        position_history=hd_position_history,
                        # The STRUCTURED trigger, so the fact-check reads
                        # the claim from the field the seat filled rather
                        # than guessing it from the sentence. This is what
                        # makes the 2026-09-16 "adverse news" shape
                        # adjudicable at all.
                        exit_trigger=action_item.get("exit_trigger"),
                    )
                except Exception as e:  # noqa: BLE001
                    logger.warning(
                        "holding discipline: check failed for %s %s (%s) — "
                        "the claim goes unverified rather than blocking the "
                        "exit", act, symbol, e,
                    )
                    hd_check = None
                if hd_check is not None and hd_check.blocks:
                    logger.warning(
                        "Position reviewer: blocking %s %s — holding-"
                        "discipline claim PROVEN FALSE. %s",
                        act, symbol, hd_check.finding,
                    )
                    try:
                        self.db.record_intraday_evaluation(
                            symbol=symbol, run_id=run_id,
                            status="exit_blocked_holding_discipline_claim_false",
                            detail=(hd_check.finding or "")[:500],
                        )
                    except Exception as e:  # noqa: BLE001
                        logger.warning(
                            "holding discipline: audit write failed: %s", e,
                        )
                    from src.risk.exit_refusal import CODE_HOLDING_DISCIPLINE_FALSE
                    self._record_exit_refusal(
                        symbol=symbol, run_id=run_id, action=act,
                        code=CODE_HOLDING_DISCIPLINE_FALSE, dropped=True,
                        detail=(hd_check.finding or "")[:400],
                        layer="holding_discipline",
                    )
                    continue
                if hd_check is not None and hd_check.verdict == "unverifiable":
                    # Audit trail only. NOT a block — see above.
                    logger.warning("Holding discipline: %s", hd_check.finding)
                    try:
                        self.db.record_intraday_evaluation(
                            symbol=symbol, run_id=run_id,
                            status="holding_discipline_claim_unverified",
                            detail=(hd_check.finding or "")[:500],
                        )
                    except Exception as e:  # noqa: BLE001
                        logger.warning(
                            "holding discipline: audit write failed: %s", e,
                        )

            # The same-day-trim gate that used to sit here is GONE, not
            # relaxed: it read `symbol in already_trimmed and not
            # _reason_cites_hard_trigger(...)`, and a completed unnamed-
            # trigger judgment above now `continue`s on every untriggered
            # SELL/REDUCE before control ever reaches it. (A recogniser that
            # cannot run fails OPEN instead — that is uncertainty, not a
            # completed "no".) Leaving the old gate in place would have been
            # dead code wearing the costume of a safety check, which is worse
            # than no check at all.
            #
            # RESIDUAL GAP, deliberately not closed here: the old gate exempted
            # hard triggers, and so does this one. A symbol trimmed at midday
            # on "bearish earnings" can be trimmed again at close on the SAME
            # "bearish earnings" — one event, two cuts, which is the 2026-05-04
            # AMZN shape with a valid trigger instead of a soft flag. Closing
            # it needs per-event dedup (has THIS trigger already been acted on
            # for this symbol today?), which is a different mechanism from a
            # phrase gate and is not in Phase 3.3's scope. Surfaced rather than
            # silently expanded.
            if act in ("SELL", "REDUCE", "COVER") and symbol in already_trimmed:
                logger.warning(
                    "Position reviewer: %s %s is a SECOND sell-side action "
                    "today, allowed because the reason names a trigger. Check "
                    "the evening grade for one-event double-application. "
                    "Reason: %r",
                    act, symbol, (action_item.get("reason") or "")[:160],
                )
            existing = [p for p in positions if p.symbol == symbol]
            # COVER only matches a held SHORT (qty < 0); SELL / REDUCE /
            # TRAIL_STOP only match a held LONG (qty > 0) — same "the order
            # must match the held side" rule ExecutionStage's COVER loop
            # enforces for the PM's decision path (mirrors it here, not a
            # new rule). A COVER proposed against a long/flat position, or
            # a SELL/REDUCE/TRAIL_STOP proposed against a short, is dropped.
            if act == "COVER":
                if not existing or existing[0].qty >= 0:
                    logger.warning(
                        "Midday: skipping COVER %s — no matching short "
                        "position", symbol,
                    )
                    continue
            elif not existing or existing[0].qty <= 0:
                logger.warning("Midday: skipping %s %s — no matching position",
                               act, symbol)
                continue
            prot = None
            try:
                if act == "TRAIL_STOP":
                    try:
                        from src.execution.scale_in import pending_protection_symbols
                        if symbol in pending_protection_symbols(self.db):
                            logger.info(
                                "Midday: TRAIL_STOP %s skipped — a "
                                "protection-restore WAL row is in flight",
                                symbol,
                            )
                            continue
                    except Exception:  # noqa: BLE001
                        pass
                    try:
                        new_stop = float(action_item.get("new_stop_price") or 0)
                    except (TypeError, ValueError):
                        new_stop = 0.0
                    if new_stop <= 0:
                        logger.warning(
                            "Midday: TRAIL_STOP %s skipped — missing/invalid new_stop_price",
                            symbol,
                        )
                        continue
                    if new_stop >= existing[0].current_price:
                        logger.warning(
                            "Midday: TRAIL_STOP %s skipped — new_stop $%.2f >= current $%.2f",
                            symbol, new_stop, existing[0].current_price,
                        )
                        continue
                    # Sanity: stop < 50% of current price is almost certainly
                    # an LLM typo. Leaving the old stop is safer than
                    # replacing it with a non-protective one.
                    if new_stop < existing[0].current_price * 0.5:
                        logger.warning(
                            "Midday: TRAIL_STOP %s skipped — new_stop $%.2f is <50%% of current $%.2f (likely LLM error)",
                            symbol, new_stop, existing[0].current_price,
                        )
                        continue
                    # RC1 exit-quality clamps (2026-07-16 forensics: 5 trail
                    # fills missed avg +30.7% post-exit; LLY was whipsawed
                    # twice identically). A hard-trigger citation in the
                    # reason bypasses both — mirroring the SELL/REDUCE gate.
                    if not _reason_cites_hard_trigger(action_item.get("reason", "")):
                        # (a) Ratchet cooldown: at most one accepted tighten
                        # per ~2 trading days per symbol.
                        if self._trail_tightened_recently(symbol):
                            logger.warning(
                                "Midday: TRAIL_STOP %s skipped — a trail was "
                                "already tightened within the last 2 trading "
                                "days (ratchet cooldown; cite a hard trigger "
                                "to bypass)", symbol,
                            )
                            continue
                        # (b) Noise-band clamp: a stop inside 1.25×ATR14 of
                        # the current price sits inside one day's normal
                        # range — it converts routine volatility into a
                        # realized exit. Keep the old stop instead.
                        atr = self._atr_for_symbol(symbol)
                        if atr is not None:
                            noise_floor = existing[0].current_price - 1.25 * atr
                            if new_stop > noise_floor:
                                logger.warning(
                                    "Midday: TRAIL_STOP %s skipped — new_stop "
                                    "$%.2f is inside the 1.25×ATR noise band "
                                    "(floor $%.2f, ATR14 $%.2f); routine "
                                    "volatility would fill it. Old stop kept; "
                                    "cite a hard trigger to bypass.",
                                    symbol, new_stop, noise_floor, atr,
                                )
                                continue
                    from src.execution.stop_records import (
                        accepted_stop_order, replace_stop_and_record,
                    )
                    order = replace_stop_and_record(
                        self.broker, self.db, symbol, new_stop,
                    )
                    if order and not (
                        isinstance(order, dict) and not accepted_stop_order(order)
                    ):
                        if isinstance(order, dict):
                            order.setdefault("action", "TRAIL_STOP")  # audit F5
                        orders.append(order)
                        self.db.insert_trade(
                            symbol=symbol, action="TRAIL_STOP",
                            qty=existing[0].qty, price=new_stop,
                            reasoning=action_item.get("reason", "midday trailing stop"),
                            run_id=run_id,
                            stop_loss=new_stop,
                            broker_order_id=order.get("id"),
                            fill_status="submitted",
                        )
                        logger.info(
                            "Midday action: TRAIL_STOP %s → $%.2f — %s",
                            symbol, new_stop, action_item.get("reason"),
                        )
                    continue

                if act == "COVER":
                    # COVER is always a FULL close here — see the docstring
                    # for why (no allocation fraction on this schema).
                    # `existing[0].qty` is the NEGATIVE broker qty; every
                    # downstream qty (WAL specs, fill_qty, insert_trade) is
                    # an absolute magnitude, never the signed qty.
                    qty = self._full_sell_qty(abs(existing[0].qty))
                    if qty is None:
                        continue
                    # Buy-to-cover needs headroom ABOVE the reference to
                    # fill on the way up — the mirror of the SELL limit
                    # sitting 0.5% BELOW (same reasoning as
                    # _EMERGENCY_LIMIT_CUSHION_PCT; matches ExecutionStage's
                    # COVER loop in src/pipeline_stages.py).
                    order_limit = round(existing[0].current_price * 1.005, 2)
                    position_qty = abs(existing[0].qty)
                    close_side = "buy"
                else:
                    if act == "REDUCE":
                        qty = self._reduce_sell_qty(existing[0].qty)
                    else:
                        qty = self._full_sell_qty(existing[0].qty)
                    if qty is None:
                        continue
                    order_limit = round(existing[0].current_price * 0.995, 2)
                    position_qty = existing[0].qty
                    close_side = "sell"
                # audit F1 review #1: snapshot -> persist WAL -> cancel.
                sale = self._submit_protected_sell(
                    symbol=symbol, qty=qty, limit_price=order_limit,
                    reference_price=existing[0].current_price,
                    position_qty_before_sell=position_qty, label=act,
                    side=close_side,
                )
                if sale is None:
                    continue
                order, prot = sale
                orders.append(order)
                self.db.insert_trade(
                    symbol=symbol, action=act, qty=qty,
                    price=existing[0].current_price,
                    reasoning=action_item.get("reason", "midday review"),
                    run_id=run_id,
                    broker_order_id=order.get("id"),
                    fill_status="submitted",
                )
                logger.info(
                    "Midday action: %s %s %s — %s",
                    act, self._format_qty(qty),
                    symbol, action_item.get("reason"),
                )
            except Exception as e:
                logger.error("Midday order failed for %s: %s", symbol, e)
            # Rebuild THIS symbol's stop coverage on its actual fill before
            # the loop cancels the next symbol's stops — the same per-name
            # discipline the de-lever loops got (docs/WORK.md item 111).
            # Finalizing the batch once after the loop left every earlier
            # symbol with no protective stop while later symbols were
            # cancelled, submitted and waited on. Runs even when the ledger
            # write above raised: the stops are off and the order is live.
            # Which names exit, how much and at what limit are unchanged.
            if prot is not None:
                self._finalize_pending_protections(
                    [prot], context="Midday reviewer",
                )
        return orders

    def _force_delever(self, ctx: RunContext) -> list[dict]:
        """Safety net for `allow_margin=False` accounts.

        When cash is meaningfully negative at session start we do NOT trust
        the LLM to pick which positions to cut — we force-sell biggest-loser
        first (most negative unrealized P&L, largest size as tiebreaker)
        until projected cash is ≥ 0. This runs BEFORE any decision / review
        stage, so the rest of the session operates on a clean, cash-only
        snapshot.

        Rationale: the DE-LEVER MANDATE in the PM / midday prompts is
        advisory — if the LLM emits only HOLDs, margin sits. Users who opt
        in to `allow_margin=False` want structural enforcement, not an LLM
        nudge. Speed and safety > LLM judgment here.

        Sell limit uses a 1% below-market buffer
        (`_EMERGENCY_LIMIT_CUSHION_PCT`) because we prioritize fill over
        price when clearing an unintended margin position. Note the contrast
        with the deleted daily-loss liquidator (docs/WORK.md item 32): this
        path clears a MEASURED cash deficit of known size, not a whole book
        on a gap day, and it is reached only when `allow_margin` is false.

        Returns the submitted orders list (empty when no de-lever is needed).
        ctx.cash / positions / total_value are refreshed from broker after
        fills so downstream stages see truth.
        """
        # `config` may be missing in tests that bypass __init__ via
        # TradingPipeline.__new__. Treat that as "not configured for cash-only
        # policy" and skip — the full-init pipeline always has config.
        risk_cfg = getattr(getattr(self, "config", None), "risk", None)
        if risk_cfg is None or bool(getattr(risk_cfg, "allow_margin", False)):
            return []
        from src.risk.constants import MARGIN_DEFICIT_FLOOR_USD
        if ctx.cash >= -MARGIN_DEFICIT_FLOOR_USD:
            return []

        deficit = -ctx.cash
        logger.warning(
            "FORCE DE-LEVER: cash=$%.2f, deficit=$%.2f — auto-selling to restore "
            "cash ≥ 0 (allow_margin=False)", ctx.cash, deficit,
        )
        # A resting entry BUY would deepen the very deficit this sweep exists
        # to clear the moment it fills — cancel entries before selling.
        try:
            self.broker.cancel_open_entry_orders()
        except Exception as exc:  # noqa: BLE001
            logger.warning("force de-lever: entry-order cancel failed: %s", exc)

        sellable = [p for p in ctx.positions if p.qty > 0]
        if not sellable:
            logger.error(
                "FORCE DE-LEVER: cash=$%.2f deficit=$%.2f but no long positions "
                "to sell — account stuck on margin until cash arrives externally",
                ctx.cash, deficit,
            )
            return []

        # Two-tier ordering: prefer longs over inverse-ETF hedges before
        # falling back to the loss-magnitude rule.
        #
        # Inverse ETFs (SH / SDS / PSQ / SQQQ) have `_effective_multiplier < 0`
        # — they HEDGE long exposure. A "biggest-loser-first" pass that
        # ignores direction can pick a hedge in any market where the long
        # book is profitable (the hedge tends to lose precisely when the
        # rest is winning). Selling the hedge first leaves the remaining
        # longs naked, AMPLIFYING directional exposure — the opposite of
        # what cash-only de-lever is trying to do (which is "shrink risk
        # to fit cash"). Cash-flow-wise both raise cash equally, but
        # risk-wise they're opposite.
        #
        # Tier key (lower = sells earlier):
        #  -1 → cash-sweep vehicle (parked T-bills ARE cash — always the
        #       first thing to liquidate; selling anything else first would
        #       realize market risk to cover a deficit that parked cash
        #       can cover for free)
        #   0 → long (effective_mul > 0)
        #   1 → inverse-ETF hedge (effective_mul < 0)
        # Within each tier, classic biggest-loser-first ordering:
        #   - most negative unrealized_pnl
        #   - then larger market_value (clear deficit in fewer orders)
        #   - then symbol alphabetical (deterministic across runs)
        from src.risk.rules import _effective_multiplier
        sweeper = self._sweeper()
        sweep_symbol = sweeper.symbol if sweeper is not None else None
        def _tier(p):
            if sweep_symbol is not None and p.symbol == sweep_symbol:
                return -1
            return 0 if _effective_multiplier(p.symbol) > 0 else 1
        targets = sorted(
            sellable,
            key=lambda p: (_tier(p), p.unrealized_pnl, -p.market_value, p.symbol),
        )

        orders: list[dict] = []
        projected_proceeds = 0.0
        for p in targets:
            if projected_proceeds >= deficit:
                break
            is_sweep = sweep_symbol is not None and p.symbol == sweep_symbol
            if is_sweep and p.current_price and p.current_price > 0:
                # audit round 2: only unpark what the deficit needs (plus a
                # 2% cushion) — full-liquidating an $80k T-bill balance for a
                # $200 deficit forced a full re-park at the session bookend,
                # a pointless round-trip. Real positions keep whole-position
                # sells (partial de-levers of losers re-review next session).
                import math as _math
                needed = (deficit - projected_proceeds) * 1.02
                qty = min(float(_math.ceil(needed / p.current_price)), p.qty)
                if qty >= p.qty:
                    qty = self._full_sell_qty(p.qty)
            else:
                qty = self._full_sell_qty(p.qty)
            if qty is None or qty <= 0:
                continue
            sell_limit = round(p.current_price * 0.99, 2)
            # The sweep vehicle's exit is recorded as SWEEP_SELL, not
            # FORCE_DELEVER (audit round 2): action names are the sweep's
            # ledger-isolation mechanism — a FORCE_DELEVER row on SGOV leaks
            # into evening sell-grading and calibration as if it were a
            # trading decision.
            sale = self._submit_protected_sell(
                symbol=p.symbol, qty=qty, limit_price=sell_limit,
                reference_price=p.current_price, position_qty_before_sell=p.qty,
                label="SWEEP_SELL" if is_sweep else "FORCE_DELEVER",
            )
            if sale is None:
                continue
            order, prot = sale
            try:
                # Count the proceeds BEFORE the ledger write: the SELL is
                # already live at the broker, so its cash is coming whether or
                # not we manage to record it. Booking it only after a
                # successful insert_trade meant a DB hiccup left
                # projected_proceeds short, and the loop force-sold the NEXT
                # position to cover a deficit the in-flight order had already
                # covered — liquidating real holdings over a bookkeeping
                # failure (2026-07-16 audit).
                # Conservative estimate: market × 0.99 (matches our limit).
                projected_proceeds += p.market_value * 0.99
                orders.append(order)
                logger.info(
                    "FORCE DE-LEVER SELL %s qty=%s @ limit=$%.2f "
                    "(unrealized_pnl=$%.2f, mkt_value=$%.2f)",
                    p.symbol, self._format_qty(qty), sell_limit,
                    p.unrealized_pnl, p.market_value,
                )
                self.db.insert_trade(
                    symbol=p.symbol,
                    action="SWEEP_SELL" if is_sweep else "FORCE_DELEVER",
                    qty=qty,
                    price=p.current_price,
                    reasoning=(
                        f"cash-only auto de-lever: session opened with "
                        f"cash=${ctx.cash:.2f} (deficit ${deficit:.2f}); "
                        f"biggest-loser-first sweep"
                    ),
                    run_id=ctx.run_id,
                    broker_order_id=order.get("id"),
                    fill_status="submitted",
                )
            except Exception as e:
                logger.error(
                    "FORCE DE-LEVER SELL %s failed: %s — the order may still be "
                    "live at the broker; its proceeds are already counted so the "
                    "sweep will not over-liquidate", p.symbol, e,
                )
            # Rebuild THIS symbol's stop coverage on its actual fill before
            # the loop cancels the next symbol's stops (docs/WORK.md item
            # 111). Finalizing the whole batch after the loop left every
            # earlier symbol with no protective stop while later symbols were
            # being cancelled, submitted and waited on. Which positions are
            # sold, and how much, is unchanged: `projected_proceeds` above is
            # booked at submit time, never from the fill.
            self._finalize_pending_protections(
                [prot], context="FORCE DE-LEVER",
            )

        # Refresh ctx so downstream stages see post-sell truth.
        try:
            account = self.broker.get_account()
            ctx.positions = self.broker.get_positions()
            ctx.cash = account["cash"]
            ctx.deployable_cash = self._compute_deployable_cash(ctx.cash, ctx.positions)
            ctx.total_value = account["portfolio_value"]
            ctx.last_equity = account.get("last_equity", ctx.total_value)
            logger.info(
                "FORCE DE-LEVER complete: %d orders, post-refresh cash=$%.2f, "
                "positions=%d",
                len(orders), ctx.cash, len(ctx.positions),
            )
        except Exception as e:
            logger.error("FORCE DE-LEVER: broker refresh failed: %s", e)

        return orders

    # --- Spec §11.2 — the gross-exposure ceiling and the de-levering ladder

    def _sweep_symbol(self) -> str | None:
        """The configured cash-park vehicle, or None when sweeping is off.

        Taken from `cash_sweep.symbol` rather than hardcoded to "SGOV" —
        the setting already exists and an operator who changes the vehicle
        must not have to change the risk engine too.
        """
        sweeper = self._sweeper()
        return getattr(sweeper, "symbol", None) if sweeper is not None else None

    def _resolve_gross_ceiling(self, ctx: RunContext):
        """Resolve this session's gross-exposure ceiling from ACCOUNT STATE.

        Nothing the Portfolio Manager produced is an input, and this returns
        a correct ceiling on a run where the PM returned nothing at all. That
        is deliberate: a blank/truncated model response is a measured failure
        mode, and a ceiling that needed a parseable book would leave the desk
        fully levered at exactly the moment it should be shedding exposure.

        Also records the state on `ctx.leverage` for the morning alert and
        the dashboard, including distance-to-forced-liquidation — which
        nothing in this codebase watched before §11.2.
        """
        risk_cfg = getattr(getattr(self, "config", None), "risk", None)
        base_x = _risk_number(getattr(risk_cfg, "max_gross_exposure_x", None), 2.0)
        maintenance_pct = _risk_number(
            getattr(risk_cfg, "maintenance_margin_pct", None), 25.0,
        )
        # Guard 2 (2026-09-02 operational safety guard): a non-finite
        # CURRENT equity read must not fall through to
        # `peak_to_trough_pct`'s "unmeasurable" branch, which
        # `resolve_gross_ceiling` resolves to the STANDING (loosest) cap.
        # That branch is correct for a genuinely fresh account with no
        # equity curve yet (see `resolve_gross_ceiling`'s docstring). Alpaca
        # has been observed to return NaN portfolio_value during market-open
        # glitches (see `RiskRuleEngine.check_daily_loss`'s docstring), and
        # holding the loosest cap on exactly that kind of broken-snapshot
        # day is the failure this guard closes: halting new risk (the
        # ladder's own floor rung) is safer than assuming zero drawdown.
        #
        # CORRECTION 2026-09-18. This comment used to assert, "by
        # inspection", that `peak_to_trough_pct` returns None ONLY when
        # today's own reading is unusable — that an empty history always
        # produced a real 0.0. That was accurate about the code and wrong
        # about safety: it meant a wiped `daily_pnl` table read as a book at
        # record highs and silently held the loosest cap. `peak_to_trough_pct`
        # now returns None when there is no usable PRIOR reading (its Guard
        # 3), so "unknown" is reachable in production from a lost equity
        # curve as well as from a bad read, and `resolve_gross_ceiling` now
        # alerts the owner in that state instead of staying silent. The two
        # paths still differ on purpose, and the difference is the point:
        # a BAD READ (below) is a book of unknown depth that already exists,
        # so it drops to the floor rung; an ABSENT CURVE may be a genuinely
        # fresh account that never fell, so it holds the standing cap and
        # trims nothing. Both now alert.
        total_value = ctx.total_value
        bad_equity_read = (
            isinstance(total_value, bool)
            or not isinstance(total_value, (int, float))
            or not math.isfinite(float(total_value))
        )
        if bad_equity_read:
            floor_x = GROSS_LADDER[-1][1]
            if base_x > 0:
                floor_x = min(base_x, floor_x)
            logger.warning(
                "§11.2: current equity read is non-finite (%r) — forcing "
                "the gross-exposure ceiling to its floor rung (%.1fx) and "
                "alerting the owner instead of assuming zero drawdown.",
                total_value, floor_x,
            )
            ceiling = GrossCeiling(
                ceiling_x=floor_x, base_x=base_x, drawdown_pct=None,
                alert_owner=True, rung="bad_read",
                reason=(
                    f"Current equity read came back non-finite "
                    f"({total_value!r}) — a documented Alpaca market-open "
                    f"glitch, not a fresh account. The book's drawdown "
                    f"cannot be verified, so gross exposure is held to the "
                    f"floor rung ({floor_x:.1f}x) until a valid read "
                    f"arrives."
                ),
            )
        else:
            drawdown_pct = None
            performance = ctx.recent_performance or {}
            if "peak_to_trough_pct" in performance:
                drawdown_pct = performance.get("peak_to_trough_pct")
            else:
                # The preamble runs before DecisionStage populates
                # `recent_performance`, so read the equity curve directly. One
                # cheap local DB read; a failure degrades to "unknown drawdown",
                # which resolves to the standing cap and trims nothing — never
                # to a wrong number that reads as "no drawdown".
                try:
                    rows = self.db.get_daily_pnl(limit=252)
                    drawdown_pct = peak_to_trough_pct(
                        [r.get("total_value") for r in (rows or [])], ctx.total_value,
                    )
                except Exception as e:  # noqa: BLE001
                    logger.warning(
                        "§11.2: could not read the equity curve for the drawdown "
                        "ladder — holding the standing %.1fx ceiling and trimming "
                        "nothing: %s", base_x, e,
                    )
            ceiling = resolve_gross_ceiling(drawdown_pct, base_x=base_x)
        gross = gross_exposure(
            ctx.positions, cash_park_symbol=self._sweep_symbol(),
        )
        equity = ctx.total_value if ctx.total_value else 0.0
        ctx.leverage = {
            "gross_usd": gross,
            "gross_x": (gross / equity) if equity > 0 else None,
            "ceiling_x": ceiling.ceiling_x,
            "base_ceiling_x": ceiling.base_x,
            "drawdown_pct": ceiling.drawdown_pct,
            "rung": ceiling.rung,
            "alert_owner": ceiling.alert_owner,
            "reason": ceiling.reason,
            "distance_to_forced_liquidation_pct":
                distance_to_forced_liquidation_pct(
                    gross, equity, maintenance_margin_pct=maintenance_pct,
                ),
        }
        return ceiling

    def _enforce_gross_ceiling(self, ctx: RunContext) -> list[dict]:
        """De-lever the HELD book when it is over the §11.2 gross ceiling.

        Runs in the session preamble, beside `_force_delever`, and therefore
        BEFORE any agent is called. That placement is the requirement, not a
        convenience: if any part of the ladder depended on the Portfolio
        Manager returning a usable book, a truncated model response would
        mean the desk stays levered exactly when it should be shedding
        exposure. Nothing here reads a PM decision.

        The ordering rule still holds and is enforced inside
        `apply_gross_ceiling`: this call passes NO decisions, so there is no
        new exposure to block, and trims are emitted only because the held
        book alone exceeds the ceiling. New exposure proposed later in the
        same session is blocked by the sizing gate (the constructor) and the
        execution gate (`max_gross_exposure`), never by selling something the
        desk already owns to make room.

        Returns the submitted orders (empty when the book is under its
        ceiling, which is the ordinary case). `ctx` is refreshed from the
        broker after fills so downstream stages see truth.
        """
        risk_cfg = getattr(getattr(self, "config", None), "risk", None)
        if risk_cfg is None:
            # Tests that bypass __init__ via TradingPipeline.__new__.
            return []
        ceiling = self._resolve_gross_ceiling(ctx)
        min_order_usd = _risk_number(
            getattr(getattr(self.config, "cash_sweep", None), "min_order_usd", None),
            500.0,
        )
        outcome = apply_gross_ceiling(
            [], ctx.positions, ctx.total_value, ceiling,
            cash_park_symbol=self._sweep_symbol(),
            min_order_usd=min_order_usd,
        )
        if not outcome.trims:
            return []
        logger.warning(
            "GROSS-EXPOSURE DE-LEVER: the book owns $%.0f against a $%.0f "
            "ceiling (%.2fx equity). %s",
            outcome.held_gross, outcome.ceiling_usd, ceiling.ceiling_x,
            ceiling.reason,
        )
        # A resting entry order would deepen the breach the moment it fills.
        try:
            self.broker.cancel_open_entry_orders()
        except Exception as exc:  # noqa: BLE001
            logger.warning("gross-exposure de-lever: entry-order cancel failed: %s", exc)

        positions_by_symbol = {p.symbol: p for p in ctx.positions}
        # The equity the ceiling was measured against, kept for the item-112
        # shortfall record (ctx.total_value is overwritten by the refresh).
        equity_before = ctx.total_value
        orders: list[dict] = []
        pending_protections: list[dict] = []
        for trim in outcome.trims:
            position = positions_by_symbol.get(trim.symbol)
            if position is None or not position.current_price:
                continue
            held_qty = abs(position.qty)
            qty = held_qty * (trim.allocation_pct / 100.0)
            if float(position.qty).is_integer():
                qty = float(int(qty))
                if qty <= 0:
                    qty = 1.0
            if qty >= held_qty:
                qty = self._full_sell_qty(held_qty)
            if qty is None or qty <= 0:
                continue
            # Same 1%-through-the-market buffer `_force_delever` uses: when
            # clearing unintended leverage, fill beats price. A COVER is a
            # BUY, so it pays UP through the market rather than down.
            #
            # `FORCE_DELEVER` is already an EITHER-SIDE exit action in the
            # ledger (`_EITHER_SIDE_EXIT_ACTIONS`, src/storage/db.py) — "a
            # deterministic de-lever fires against whatever position is
            # open" — so the same label correctly retires a short chain
            # without inventing a second action name.
            is_cover = trim.action == "COVER"
            limit_price = round(
                position.current_price * (1.01 if is_cover else 0.99), 2,
            )
            sale = self._submit_protected_sell(
                symbol=trim.symbol, qty=qty, limit_price=limit_price,
                reference_price=position.current_price,
                position_qty_before_sell=abs(position.qty),
                label="FORCE_DELEVER",
                side="buy" if is_cover else "sell",
            )
            if sale is None:
                continue
            order, protection = sale
            pending_protections.append(protection)
            orders.append(order)
            logger.info(
                "GROSS-EXPOSURE DE-LEVER %s %s qty=%s @ limit=$%.2f (%s)",
                trim.action, trim.symbol, self._format_qty(qty), limit_price,
                ceiling.reason,
            )
            try:
                self.db.insert_trade(
                    symbol=trim.symbol,
                    action="FORCE_DELEVER",
                    qty=qty,
                    price=position.current_price,
                    reasoning=trim.reasoning[:500],
                    run_id=ctx.run_id,
                    broker_order_id=order.get("id"),
                    fill_status="submitted",
                )
            except Exception as e:  # noqa: BLE001
                logger.error(
                    "GROSS-EXPOSURE DE-LEVER: trade row for %s failed: %s — "
                    "the order may still be live at the broker", trim.symbol, e,
                )
            # Rebuild THIS symbol's stop coverage on its actual fill before
            # the loop cancels the next symbol's stops (docs/WORK.md item
            # 111). Finalizing the batch once after the loop left every
            # earlier symbol with no protective stop while the later ones were
            # cancelled, submitted and waited on — and the ladder only fires
            # in a drawdown. The trims themselves (which names, how much, at
            # what limit) were all fixed by `apply_gross_ceiling` before the
            # loop began and are unchanged.
            protection["trim_action"] = trim.action
            protection["trim_qty"] = qty
            self._finalize_pending_protections(
                [protection], context="GROSS-EXPOSURE DE-LEVER",
            )
        try:
            account = self.broker.get_account()
            ctx.positions = self.broker.get_positions()
            ctx.cash = account["cash"]
            ctx.deployable_cash = self._compute_deployable_cash(ctx.cash, ctx.positions)
            ctx.total_value = account["portfolio_value"]
            ctx.last_equity = account.get("last_equity", ctx.total_value)
            # Re-measure so the alert and the dashboard report the book that
            # now exists, not the one that triggered the de-lever.
            self._resolve_gross_ceiling(ctx)
            self._alert_owner_delever_incomplete(ctx)
            self._record_delever_shortfall(
                ctx, held_gross_before=outcome.held_gross,
                ceiling_usd_before=outcome.ceiling_usd,
                equity_before=equity_before, protections=pending_protections,
            )
        except Exception as e:  # noqa: BLE001
            logger.error("GROSS-EXPOSURE DE-LEVER: broker refresh failed: %s", e)
        return orders

    def _record_delever_shortfall(
        self, ctx: RunContext, *, held_gross_before: float,
        ceiling_usd_before: float | None,
        equity_before: float | None, protections: list[dict],
    ) -> None:
        """Durable record of a gross-exposure de-lever that finished with the
        book STILL over its ceiling (docs/WORK.md item 112).

        `_alert_owner_delever_incomplete` already sets the flag the session
        message reads; what nothing kept was the evidence — the book before
        and after, the ceiling, and what each order actually did — so a
        failed de-lever could not be reviewed after the log rotated. This
        writes ONE row in the desk's existing lifecycle-event stream
        (`specialist_evidence`, `agent_name='pipeline'`,
        `kind='pipeline_event'`, `scope='run'` — the same shape
        `_record_pipeline_event` and the stop-out reconciler use), NOT in
        `agent_logs`: that table is the paid-model ledger, and the cost
        circuit refuses a same-day `agent_logs` row whose run has no budget
        session, which a pre-agent preamble write could produce.

        Observability only: no order, alert, sizing or sequencing depends on
        it, it writes nothing when the book cleared its ceiling, and it never
        raises.
        """
        leverage = ctx.leverage or {}
        if not leverage.get("delever_incomplete"):
            return
        try:
            import json
            gross_before_x = (
                held_gross_before / equity_before
                if isinstance(equity_before, (int, float)) and equity_before > 0
                else None
            )
            order_rows = [
                {
                    "symbol": p.get("symbol"),
                    "action": p.get("trim_action"),
                    "qty_submitted": p.get("trim_qty"),
                    "broker_order_id": p.get("order_id"),
                    "terminal_status": p.get("terminal_status"),
                    "stop_coverage_confirmed": p.get("coverage_confirmed"),
                }
                for p in protections
            ]
            payload = {
                "stage": "gross_delever", "outcome": "still_over_ceiling",
                "reason": leverage.get("reason") or "",
                "rung": leverage.get("rung"),
                "gross_usd_before": held_gross_before,
                "gross_x_before": gross_before_x,
                "equity_before": equity_before,
                "ceiling_usd_before": ceiling_usd_before,
                "gross_usd_after": leverage.get("gross_usd"),
                "gross_x_after": leverage.get("gross_x"),
                "ceiling_x": leverage.get("ceiling_x"),
                "orders": order_rows,
            }
            self.db.insert_specialist_evidence(
                run_id=ctx.run_id, agent_name="pipeline", kind="pipeline_event",
                scope="run", symbol=None,
                decision_id=getattr(ctx, "decision_id", None),
                evidence_json=json.dumps(payload, sort_keys=True, default=str),
            )
        except Exception as exc:  # noqa: BLE001 — evidence is never trading authority
            logger.warning(
                "GROSS-EXPOSURE DE-LEVER: could not persist the shortfall "
                "record for run %s: %s", ctx.run_id, exc,
            )

    def _alert_owner_delever_incomplete(self, ctx: RunContext) -> None:
        """Flag it when the gross-exposure de-lever did not work.

        `_enforce_gross_ceiling` already logs a warning the moment it
        *decides* to trim. What nothing checked before this is the OUTCOME:
        a trim can be submitted and still leave the book over the ceiling —
        an order that failed to place, a partial fill, integer-share
        rounding down, or the market moving between the plan and the fill
        can all produce this. That gap is unreportable, not silent: it was
        always in the log, just never in anything the owner actually reads
        (a Telegram message) or in the session result a test can assert on.
        This is a reporting-only check — it runs after every broker call in
        the de-lever is already done and changes no order, no sizing, and no
        sequencing.

        Sets `ctx.leverage["delever_incomplete"] = True` (which every
        session-result dict already threads through, since they all copy
        `ctx.leverage` verbatim) whenever we have a real, freshly-measured
        gross exposure that is still above the ceiling. Never guesses: both
        numbers come from the same post-refresh `_resolve_gross_ceiling`
        call used for the ordinary leverage line, and the check is skipped
        (not defaulted to False) when either is unmeasurable.
        """
        leverage = ctx.leverage or {}
        gross_x = leverage.get("gross_x")
        ceiling_x = leverage.get("ceiling_x")
        if not isinstance(gross_x, (int, float)) or not isinstance(ceiling_x, (int, float)):
            return
        if gross_x <= ceiling_x:
            return
        leverage["delever_incomplete"] = True
        logger.warning(
            "GROSS-EXPOSURE DE-LEVER: still over the ceiling after de-levering "
            "— gross exposure %.2fx equity vs a %.2fx ceiling.",
            gross_x, ceiling_x,
        )

    def _execution_stage(self, ctx: RunContext) -> list[dict]:
        """Delegates to ExecutionStage (class lives in pipeline_stages.py)."""
        return self.execution_stage.run(ctx)

    def _risk_stage(self, ctx: RunContext) -> dict | None:
        """Delegates to RiskStage (class lives in pipeline_stages.py)."""
        return self.risk_stage.run(ctx)

    def _decision_stage(self, ctx: RunContext):
        """Delegates to DecisionStage (class lives in pipeline_stages.py)."""
        self.decision_stage.run(ctx)

    def _activate_cost_session(self, run_id: str, mode: str) -> None:
        """Register paid-call context without interfering with safety work."""

        self._active_cost_run_context = (run_id, mode)
        circuit = getattr(self, "cost_circuit", None)
        if circuit is None:
            if BaseAgent._allow_unmetered_for_tests:
                return
            circuit = UnavailableLLMCostCircuit(
                RuntimeError("mandatory paid-analysis cost circuit is not initialized")
            )
            self.cost_circuit = circuit
            self._attach_cost_circuit_to_agents()
        try:
            circuit.activate_session(run_id, mode)
        except Exception as exc:
            logger.critical(
                "Cost-circuit activation failed for %s/%s; failing paid analysis "
                "closed without interrupting deterministic safety: %s",
                run_id, mode, exc, exc_info=True,
            )
            marker = getattr(circuit, "mark_unavailable", None)
            if callable(marker):
                marker(exc, run_id=run_id, mode=mode)
            else:
                circuit = UnavailableLLMCostCircuit(exc)
                self.cost_circuit = circuit
                self._attach_cost_circuit_to_agents()
                circuit.activate_session(run_id, mode)

    def _require_paid_analysis(self, agent_name: str) -> None:
        circuit = getattr(self, "cost_circuit", None)
        if circuit is None:
            if BaseAgent._allow_unmetered_for_tests:
                return
            raise PaidAnalysisSuspended(
                "mandatory paid-analysis cost circuit is not initialized",
                {"available": False, "suspended": True},
            )
        try:
            circuit.require_paid_analysis(agent_name)
        except PaidAnalysisSuspended:
            raise
        except Exception as exc:
            logger.critical("Cost-circuit preflight failed closed: %s", exc, exc_info=True)
            marker = getattr(circuit, "mark_unavailable", None)
            if callable(marker):
                state = marker(exc)
                raise PaidAnalysisSuspended(
                    "mandatory cost-circuit preflight failed", state,
                ) from exc
            replacement = UnavailableLLMCostCircuit(exc)
            self.cost_circuit = replacement
            self._attach_cost_circuit_to_agents()
            run_id, mode = getattr(
                self, "_active_cost_run_context", ("unscoped", "unknown")
            )
            replacement.activate_session(run_id, mode)
            replacement.require_paid_analysis(agent_name)

    def _attach_cost_circuit_to_agents(self) -> None:
        circuit = getattr(self, "cost_circuit", None)
        for name in (
            "tech_analyst", "news_analyst", "macro_analyst",
            "earnings_analyst", "smart_money_analyst",
            "portfolio_manager", "risk_manager",
            "position_reviewer", "evening_analyst", "meta_reflector",
        ):
            agent = getattr(self, name, None)
            setter = getattr(agent, "set_cost_circuit", None)
            if callable(setter):
                setter(circuit)

    def _cost_circuit_status(self) -> dict:
        circuit = getattr(self, "cost_circuit", None)
        if circuit is None:
            if BaseAgent._allow_unmetered_for_tests:
                return {"enabled": False, "suspended": False}
            return {"available": False, "enabled": True, "suspended": True,
                    "trigger_detail": "mandatory cost circuit is not initialized"}
        try:
            return circuit.status()
        except Exception as exc:
            logger.critical("Cost-circuit status failed closed: %s", exc, exc_info=True)
            marker = getattr(circuit, "mark_unavailable", None)
            if callable(marker):
                return marker(exc)
            replacement = UnavailableLLMCostCircuit(exc)
            self.cost_circuit = replacement
            self._attach_cost_circuit_to_agents()
            run_id, mode = getattr(
                self, "_active_cost_run_context", ("unscoped", "unknown")
            )
            return replacement.activate_session(run_id, mode)

    @staticmethod
    def _parse_logged_agent_response(row: dict):
        """Parse stored fenced/prose-wrapped JSON exactly as live agents do."""

        return AgentResult(
            raw_text=row.get("full_response") or "",
            tokens_used=0,
            model=row.get("model") or "",
        ).parse_json()

    @staticmethod
    def _paid_suspended_payload(
        run_id: str, *, orders: list[dict] | None = None, error: BaseException | None = None,
    ) -> dict:
        return {
            "status": "paid_analysis_suspended",
            "run_id": run_id,
            "orders": list(orders or []),
            "error": str(error or "mandatory cost circuit is open"),
            "paid_analysis_suspended": True,
            "preserved": [
                "broker_resident_protection",
                "order_fill_reconciliation",
                "deterministic_loss_protection",
                "non_llm_safety_jobs",
            ],
        }

    def _paid_suspension_after_late_safety(
        self,
        run_id: str,
        *,
        session: str,
        error: BaseException,
        where: str,
        orders: list[dict] | None = None,
        extra: dict | None = None,
    ) -> dict:
        """Recheck deterministic loss protection before a suspension return."""

        existing_orders = list(orders or [])
        halt = self._check_late_breach_and_halt(run_id, where)
        if halt is not None:
            if session == "morning":
                # A halt supersedes any PM checkpoint written before the
                # breaker opened (for example while entering RM). Never allow
                # that pre-halt plan to resume after a reset.
                from src import decision_checkpoint as _dc
                _dc.mark_consumed("morning")
                _dc.write_status("morning", self.DAILY_LOSS_HALT_STATUS)
            # The halt itself placed no orders; these are the session's own,
            # carried through so the feed still renders them.
            halt["orders"] = existing_orders + list(halt.get("orders") or [])
            halt["paid_analysis_suspended"] = True
            halt["suspension_error"] = str(error)
            if extra:
                halt.update(extra)
            return halt
        payload = self._paid_suspended_payload(
            run_id, orders=existing_orders, error=error,
        )
        if extra:
            payload.update(extra)
        return payload

    def _kill_switch_halt_result(self, run_id: str, **extra) -> dict | None:
        """Guard 1's early, VISIBLE half (2026-09-02 operational safety
        guard). Returns an early-exit result dict when ops has halted the
        desk, else None.

        The broker-level check (`AlpacaBroker._kill_switch_active`) is what
        actually GUARANTEES no order reaches Alpaca while the flag file
        exists — it re-checks on every single submit/replace call, so it
        stays correct even if the file appears mid-session, after this
        early check already passed. This method exists only so a halted
        run (a) does not spend real broker calls and LLM budget on analysis
        that can place no order, and (b) produces exactly ONE clear alert
        on the channel the operator actually reads: the returned
        `status` flows through `format_session_result` to
        `TelegramNotifier.send()` in `main.py`, the SAME path every other
        session result already takes — no new alerting mechanism.

        UNLIKE `_paid_suspended_payload` above, nothing NEW is preserved:
        this is the one guard in the codebase that also blocks a
        risk-reducing order (see RiskConfig.kill_switch_path), so a new
        protective stop cannot go out either while it is active. A stop
        already resting at the broker from before the halt is untouched
        and keeps protecting its position — only new broker-bound order
        flow is refused.
        """
        if self._kill_switch_path is None or not self._kill_switch_path.exists():
            return None
        logger.error(
            "KILL SWITCH ACTIVE (%s exists) — halting run %s before any "
            "broker or LLM work. touch/rm that file to stop/resume the "
            "desk.", self._kill_switch_path, run_id,
        )
        payload = {
            "status": "kill_switch_halted", "run_id": run_id, "orders": [],
            "kill_switch_path": str(self._kill_switch_path),
        }
        payload.update(extra)
        return payload

    def _evidence_gate_skip(
        self, ctx, run_id: str, *, session: str = "morning",
    ) -> dict | None:
        """docs/WORK.md item 20 — refuse to DECIDE on evidence that never
        arrived. Returns a terminal result dict when the run must skip, or
        None to proceed.

        The distinction it rests on is categorical and needs no threshold: a
        seat that had nothing to report answered; a seat whose answer was
        lost did not. See `src/evidence_gate.py` for why no count is used and
        why the counting half of the owner's design is deliberately unbuilt.

        WHICH LOST SEAT ACTUALLY STOPS THE RUN is an owner mandate decision
        of 2026-09-18 — "Only technical analysis can stop the desk" — and
        lives in `evidence_gate.BLOCKING_SEATS`, not here. A lost ADVISORY
        seat is recorded in the same durable rows, logged loudly, carried in
        the result so the unsuppressible data-quality alert still fires, and
        named in the freshness disclosure. It does not halt trading.

        EVERY DECISION DISCLOSES ITS OWN EVIDENCE FRESHNESS. With the other
        seats advisory a decision can rest on one freshly-read seat plus a
        carried-forward book, and every carried seat reports green; this is
        the one path every decision passes through, so the count of seats
        read on THIS tick is computed here and handed to the owner's message
        and the durable record. Disclosure, not a threshold — there is no
        minimum fresh count anywhere and none may be invented.

        THE SKIP IS LOUD, by three independent paths, because retired item 11
        was this desk producing nothing for a whole day with nobody noticing
        (docs/INCIDENT_HISTORY.md, closed 2026-09-13):
          - its own standalone owner alert, sent here — MORNING ONLY as of
            2026-09-18. On an intra_check tick the session message below is
            guaranteed to speak (`evidence_gate_skip` is actionable on the
            trader feed and is in none of its silent-status sets), so this
            alert only duplicated it, one minute apart, word for word;
          - `notifier.maybe_alert_data_quality`, which fires from main.py's
            finally block on the `data_status` carried in the result and
            cannot be suppressed by a mode's noise policy;
          - the session result message, whose `status` says it in one word.

        It drops no candidate and emits no target: it returns before any
        target exists, so it cannot produce the 0%-target-means-SELL shape.
        Every symbol that HAD reached a technical read still gets its own
        durable, machine-readable row saying why the desk never decided on
        it, alongside the run-level row.
        """
        from src import evidence_gate

        try:
            verdict = evidence_gate.evaluate(ctx.data_status)
        except Exception as exc:  # noqa: BLE001
            # A gate that can stop the desk trading must not stop it by
            # crashing. `evaluate` is documented never to raise; if it
            # somehow does, proceed and say so loudly.
            logger.error(
                "evidence gate raised (%s) — PROCEEDING with the decision. "
                "This is a bug in src/evidence_gate.py.", exc,
            )
            return None

        def _record(symbol, outcome, reason, **details):
            # Forensic persistence must never be able to break the trading
            # path it is reporting on (.claude/rules/trading-core.md).
            # `_persist_evidence` already swallows DB errors; this also
            # covers a caller with no `db` wired at all.
            try:
                _record_pipeline_event(
                    self, ctx, symbol, "evidence_gate", outcome, reason,
                    **details,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("evidence gate: event write failed: %s", exc)

        # Disclosure, carried out of here by `_attach_evidence_freshness` on
        # every return path of the session wrappers. Stored on the pipeline
        # as well as on ctx because the result dicts are built in dozens of
        # places and the wrappers are the two that see all of them.
        try:
            self._last_evidence_freshness = verdict.freshness.to_evidence()
            self._last_decision_data_status = dict(verdict.data_status)
            ctx.evidence_freshness = dict(self._last_evidence_freshness)
        except Exception as exc:  # noqa: BLE001 — never break the decision
            logger.warning("evidence gate: freshness record failed: %s", exc)
        logger.info("EVIDENCE FRESHNESS — %s", verdict.freshness.summary)

        evidence = verdict.to_evidence()
        _record(None, evidence.pop("outcome"), evidence.pop("reason"), **evidence)
        if not verdict.skip:
            if verdict.advisory_lost:
                # Owner mandate 2026-09-18: only the technical seat halts the
                # desk. An advisory seat losing its answer is still a real
                # fault and is still said out loud — here, in the durable row
                # above, and by `notifier.maybe_alert_data_quality`, which
                # reads the `data_status` the wrappers now attach to every
                # result. What it no longer does is stop trading.
                logger.error(
                    "evidence gate: ADVISORY seat(s) lost their answer and the "
                    "decision PROCEEDED (owner mandate 2026-09-18, only the "
                    "technical seat blocks): %s",
                    {s: verdict.data_status.get(s) for s in verdict.advisory_lost},
                )
            if verdict.unclassified:
                logger.error(
                    "evidence gate: unclassified seat status this run: %s",
                    {s: verdict.data_status.get(s) for s in verdict.unclassified},
                )
            return None

        logger.error("EVIDENCE GATE — %s", verdict.reason)
        for analysis in ctx.analyses or []:
            symbol = getattr(analysis, "symbol", None)
            if symbol:
                _record(
                    symbol, "not_decided", "evidence_gate_skip",
                    lost_seats=list(verdict.lost),
                    blocking_lost_seats=list(verdict.blocking_lost),
                    data_status=dict(verdict.data_status),
                )
        # Legit PM-less completion — same reason `no_data` records one: the
        # evening dead-man probe must not read "research rows, no PM row" as
        # a morning that was killed mid-run.
        from src import decision_checkpoint as _dc

        # Morning only: the evening dead-man probe keys off this
        # checkpoint. An intra_check skip must not overwrite a completed
        # morning's status with a later refusal.
        if session == "morning":
            _dc.write_status("morning", "evidence_gate_skip")
        # The owner was told the same skip TWICE, one minute apart, on
        # 2026-09-18 11:19 ET: once by this standalone alert and once by the
        # intraday tick's own message. On an intra_check tick the tick
        # message is guaranteed to speak — `evidence_gate_skip` is in
        # `trader_feed._intraday_tick_actionable`'s list and in neither
        # `_BASE_ONLY_STATUSES` nor `_INTRADAY_SILENT_STATUSES`, so the
        # "a quiet tick is silent" policy that this standalone alert exists
        # to defeat cannot apply to a skip. The tick message also carries
        # P&L and the book, which this one cannot. So the tick message
        # speaks for an intraday skip and this alert stays quiet; the skip
        # is not silenced anywhere, and the morning path (whose own session
        # message is a different renderer) keeps its alert unchanged.
        if session == "morning":
            try:
                from src.notifier import describe_skipped_decision, send_owner_alert

                # Plain words only — no run id, no seat key, no state token
                # and no `verdict.reason`. The machine reason is unchanged in
                # the result dict, the event rows and the log line above.
                send_owner_alert("\n".join(
                    describe_skipped_decision(verdict.lost, verdict.data_status)
                ))
            except Exception as exc:  # noqa: BLE001
                logger.warning("evidence gate: owner alert failed: %s", exc)
        return {
            "status": "evidence_gate_skip", "orders": [], "run_id": run_id,
            "data_status": dict(ctx.data_status),
            "lost_seats": list(verdict.lost),
            "blocking_lost_seats": list(verdict.blocking_lost),
            "advisory_lost_seats": list(verdict.advisory_lost),
            "evidence_freshness": verdict.freshness.to_evidence(),
            "reason": verdict.reason,
        }

    def run_morning(self) -> dict:
        """The morning session, plus the durable record of its own output.

        `leverage` (the §11.2 gross-ceiling snapshot) and
        `stop_coverage_gaps` (the broker-truth stop audit) are computed
        fresh from live broker state every call and, before this wrapper,
        were handed to the notifier and dropped — no other durable
        table holds them (unlike PM/RM reasoning and orders, already kept
        via `specialist_evidence`/`trades`). The body below is unchanged;
        this wrapper persists EVERY return path so the message can be
        re-read without paying for a fresh run. Fail-soft — a storage
        problem costs the audit record, never the morning push.
        """
        self._last_evidence_freshness = None
        result = self._run_morning_body()
        self._attach_evidence_freshness(result)
        self._attach_universe_changes(result)
        self._persist_session_report("morning", result)
        return result

    def _attach_evidence_freshness(self, result) -> None:
        """Carry this run's evidence-freshness disclosure out to the owner.

        Owner mandate 2026-09-18 made every seat but the technical one
        advisory, so a decision can now rest on ONE freshly-read seat plus a
        carried-forward book — and every carried seat reports green. The
        disclosure is computed once, by the evidence gate, on the single
        path every decision passes through; this hands it to the message
        renderer and to the durable session report.

        Disclosure only. It states how much was read on this tick; it never
        judges the count and there is no minimum — that number is the
        owner's (docs/WORK.md item 20). Fail-soft: a problem here costs the
        disclosure line, never the session.
        """
        if not isinstance(result, dict):
            return
        record = getattr(self, "_last_evidence_freshness", None)
        if isinstance(record, dict) and "evidence_freshness" not in result:
            result["evidence_freshness"] = dict(record)
        # A lost ADVISORY seat no longer halts the run, so the one alert
        # that cannot be silenced by a mode's noise policy
        # (`notifier.maybe_alert_data_quality`, fired from main.py's finally
        # block) must be able to see it. It reads `result["data_status"]`,
        # which the intra_check result paths never carried — before the
        # mandate change they did not have to, because a lost seat there
        # halted the run instead.
        status = getattr(self, "_last_decision_data_status", None)
        if isinstance(status, dict) and status and "data_status" not in result:
            result["data_status"] = dict(status)

    def _persist_session_report(self, mode: str, result: dict) -> None:
        """Write a morning/midday/close result dict verbatim, keyed by
        trading day + mode. No field is defaulted or filled in here.
        """
        if not isinstance(result, dict):
            return
        try:
            self.db.save_session_report(
                mode=mode, date=session_date_key(),
                run_id=result.get("run_id"), payload=result,
            )
        except Exception as exc:  # noqa: BLE001 — never break the push
            logger.warning(
                "%s report persistence failed (non-fatal): %s", mode, exc,
            )

    def _run_morning_body(self) -> dict:
        ctx = RunContext.start("morning")
        run_id = ctx.run_id
        logger.info("=== Morning run started: %s ===", run_id)

        if not self._is_trading_day():
            logger.info("Morning run skipped: market closed for non-trading day")
            return {"status": "market_holiday", "orders": [], "run_id": run_id}

        halt = self._kill_switch_halt_result(run_id)
        if halt is not None:
            return halt

        self._activate_cost_session(run_id, "morning")

        try:
            # 0a. FIRST BROKER ACTION OF THE DAY: broker-truth coverage audit
            # (independent of the WAL). Catches any long that went naked
            # WITHOUT leaving a recovery row — and, since spec §11.1's hybrid
            # fractional stops, RE-PLACES the sub-share DAY stops that the
            # broker expired at yesterday's close.
            #
            # This used to run at 0b, after three drain passes that each make
            # their own broker round-trips. Every second it spent waiting was
            # a second the fractional remainder of every held position sat
            # unprotected into an open market, and the open is exactly when
            # that matters most. The owner accepted a bounded OVERNIGHT
            # exposure; he did not accept it bleeding into the session, so
            # the unprotected window at the open is now as short as this
            # system can make it.
            #
            # Symbols the drain owns are skipped by the reconciler either way
            # (it reads `get_pending_protection_restores` itself), so moving
            # ahead of the drain changes nothing for them — the drain still
            # restores their coverage microseconds later, exactly as before.
            coverage_gaps = self._reconcile_stop_coverage()
            # 0a'. Sweep retired (owner mandate 2026-09-17): sell any T-bill
            # vehicle still held into cash before any seat reads the book.
            self._release_retired_cash_park(run_id)
            # 0b. Drain orphaned protection-restore intents from prior
            # sessions where finalize had to bail (lingering SELL didn't
            # converge, or broker API hiccup). Each drained row brings a
            # symbol's stop coverage back in line with broker reality.
            self._drain_pending_protection_restores()
            self._drain_pending_repegs()
            # audit F4: resolve BUY write-ahead orphans from a prior
            # crashed session before this run touches positions/cash.
            self._reconcile_orphan_pending_submits()
            # 0c. Broker-truth EXIT audit (2026-08-28 ONDS/CCJ): a protective
            # stop firing overnight is exactly the case morning must catch
            # first — the position has been closed for hours by the time
            # this runs, and every other session entry point runs this same
            # check again in case morning's own attempt failed.
            try:
                self._reconcile_stop_out_fills(run_id)
            except Exception as exc:  # noqa: BLE001
                logger.warning("morning stop-out reconcile failed (non-fatal): %s", exc)

            # 0. Cancel stale entry orders from previous sessions, but preserve live protective exits.
            self.broker.cancel_open_entry_orders()

            # 1. Get account state (snapshot into ctx). Explicit guard mirrors
            # `run_intra_check` — a broker-API failure at snapshot time should
            # bail cleanly with a clear status, not propagate an exception
            # that leaves `ctx` half-populated and every downstream stage
            # guessing at state.
            try:
                account = self.broker.get_account()
                positions = self.broker.get_positions()
            except Exception as e:
                logger.error("Morning: broker snapshot failed: %s", e)
                return {
                    "status": "broker_error", "orders": [],
                    "run_id": run_id, "error": str(e),
                }
            cash = account["cash"]
            total_value = account["portfolio_value"]
            last_equity = account.get("last_equity", total_value)
            ctx.account = account
            ctx.positions = positions
            ctx.cash = cash
            ctx.deployable_cash = self._compute_deployable_cash(cash, positions)
            ctx.total_value = total_value
            ctx.last_equity = last_equity
            logger.info(
                "Account: $%.2f total, $%.2f cash (deployable $%.2f), %d positions (last close $%.2f)",
                total_value, cash, ctx.deployable_cash, len(positions), last_equity)

            # 1a. Cash-only safety net — force-sell if margin was entered before
            # this session. Refreshes ctx.cash / positions on completion, so
            # every stage below runs on clean truth.
            forced_orders = self._force_delever(ctx)

            # 1b. Spec §11.2 — the gross-exposure ceiling and its de-levering
            # ladder. Deliberately here, before ANY agent runs: the ceiling is
            # computed from account state alone, so a Portfolio Manager that
            # returns nothing (a measured failure mode — one candidate model
            # truncated mid-JSON on 1 run in 10) cannot leave the desk levered
            # during a drawdown. Also populates ctx.leverage for the alert and
            # the dashboard, including distance-to-forced-liquidation.
            forced_orders = list(forced_orders) + self._enforce_gross_ceiling(ctx)
            positions = ctx.positions
            cash = ctx.cash
            total_value = ctx.total_value
            last_equity = ctx.last_equity
            # Local `positions` table is a derived snapshot (journal /
            # notifier / rehearsal). Morning used to never write it, so a
            # midday/close that last ran when only one name was held left
            # the table lying after later fills. Refresh from the broker
            # book we just read, before the long research window.
            self._sync_positions_from_broker(positions)

            # Hard circuit breaker before any LLM/research work. If the account
            # opens through the daily-loss limit, the deterministic response
            # must not depend on PM/RM producing a tradeable plan later in the
            # run. That response is a HALT, not a liquidation — docs/WORK.md
            # item 32, see `_halt_on_daily_loss_breach`.
            loss_violation, _bl, _pnl, loss_basis = self._daily_loss_breach(
                account, positions,
            )
            if loss_violation and positions:
                halt = self._halt_on_daily_loss_breach(
                    positions, loss_violation, run_id,
                    where="morning pre-research", basis=loss_basis, ctx=ctx,
                )
                # Any same-day plan is superseded by the halt — a stale
                # unconsumed checkpoint must not resume, and the dead-man
                # probe must not read this as a killed morning.
                from src import decision_checkpoint as _dc
                _dc.mark_consumed("morning")
                _dc.write_status("morning", self.DAILY_LOSS_HALT_STATUS)
                return halt

            # All broker-resident and deterministic safety work above runs
            # even while the paid-analysis circuit is latched. Only now, at
            # the boundary before research/resume-RM, may it stop the run.
            try:
                self._require_paid_analysis("morning_research")
            except PaidAnalysisSuspended as exc:
                return self._paid_suspension_after_late_safety(
                    run_id, session="morning", error=exc, where="paid-pre-research",
                    orders=forced_orders,
                )

            # RC2 resume lane: a prior morning tick may have been killed by
            # the wrapper timeout AFTER the PM produced a plan but BEFORE the
            # RiskStage reviewed it (the observed death mode: 61/61 BUY-
            # proposal days destroyed at the PM→RM boundary during the
            # 6/30-7/15 relay outage). If today's unconsumed checkpoint
            # exists and is fresh, skip research+PM entirely — the full
            # preamble above (drains, coverage audit, force_delever, circuit
            # breaker, FRESH account snapshot) has already run, and the
            # RiskStage + execution guards below all operate on live state.
            # RM always re-runs; there is no resume-past-RM.
            from src import decision_checkpoint as _dc
            resumed = _dc.load("morning")
            if resumed is not None:
                logger.warning(
                    "RESUME LANE: unconsumed decision checkpoint from %s "
                    "(age %.0f min, %d decisions) — skipping research+PM, "
                    "re-entering at RiskStage on fresh account state",
                    resumed["run_id"], resumed["age_minutes"],
                    len(resumed["portfolio_decision"].decisions),
                )
                ctx.macro_summary = resumed["macro_summary"]
                ctx.macro_analysis = resumed["macro_analysis"]
                ctx.news_intel = resumed["news_intel"]
                ctx.analyses = resumed["analyses"]
                ctx.earnings_results = resumed["earnings_results"]
                ctx.data_status = resumed["data_status"]
                ctx.admitted_symbols = set(resumed["admitted_symbols"])
                ctx.portfolio_decision = resumed["portfolio_decision"]
                portfolio_decision = ctx.portfolio_decision
                # Rehydrate bars for the plan's BUY symbols (zero-LLM, fresh
                # data). The checkpoint deliberately omits symbols_bars
                # (huge); without this the entry ATR stop floor silently
                # no-ops and the correlation advisory false-fires on resume.
                bars: dict = {}
                for d in portfolio_decision.decisions:
                    if d.action != "BUY":
                        continue
                    try:
                        bars[d.symbol] = self.market.get_ohlcv(
                            d.symbol, self.config.trading.lookback_days,
                        ) or []
                    except Exception as e:  # noqa: BLE001
                        logger.warning("resume: bar rehydrate failed for %s: %s",
                                       d.symbol, e)
                ctx.symbols_bars = bars
            else:
                # Phase 4 #1: research stage runs the parallel fan-out (macro /
                # news / tech / earnings). Populates ctx fields.
                try:
                    self.morning_research_stage.run(ctx)
                except PaidAnalysisSuspended as exc:
                    return self._paid_suspension_after_late_safety(
                        run_id, session="morning", error=exc, where="paid-research-suspended",
                        orders=forced_orders,
                    )
                circuit_state = self._cost_circuit_status()
                if circuit_state.get("suspended"):
                    return self._paid_suspension_after_late_safety(
                        run_id, session="morning", where="post-research-circuit-open",
                        orders=forced_orders,
                        error=PaidAnalysisSuspended(
                            str(circuit_state.get("trigger_detail") or "cost circuit opened")
                        ),
                    )
                analyses = ctx.analyses

                # Late-breach check: research can take 5-10 min on slow OpenAI
                # days. The pre-research circuit breaker (#45) caught open-gap
                # losses; this catches the case where the tape crosses the
                # daily-loss limit DURING research and the morning would
                # otherwise bail to no_data/no_trades, leaving the breach for
                # the next intra tick (30 min away). Mirror the pre-research
                # bypass: deterministic HALT, no LLM dependency.
                late_breach = self._check_late_breach_and_halt(
                    run_id, "post-research", ctx=ctx,
                )
                if late_breach is not None:
                    _dc.mark_consumed("morning")
                    _dc.write_status("morning", self.DAILY_LOSS_HALT_STATUS)
                    return late_breach

                if not analyses:
                    logger.warning("No analyses produced, skipping trading")
                    # Legit PM-less completion — record it so the evening
                    # dead-man probe doesn't read "research rows, no PM row"
                    # as a killed morning.
                    _dc.write_status("morning", "no_data")
                    return {"status": "no_data", "orders": [], "run_id": run_id}

                # docs/WORK.md item 20 — the owner's own design. Deliberately
                # sequenced HERE: after every safety path above (the two
                # late-breach emergency-liquidation checks and the paid-
                # suspension bails still run, because a refusal to DECIDE must
                # never become a refusal to PROTECT), and before the Portfolio
                # Manager call, which is the expensive one this exists to not
                # spend on absent evidence.
                self._heal_lost_research_seats(ctx)
                gate_skip = self._evidence_gate_skip(ctx, run_id)
                if gate_skip is not None:
                    return gate_skip

                # Phase 4 #1: decision stage — memory layers + PM + Constructor.
                try:
                    self._decision_stage(ctx)
                except PaidAnalysisSuspended as exc:
                    return self._paid_suspension_after_late_safety(
                        run_id, session="morning", error=exc, where="paid-decision-suspended",
                        orders=forced_orders,
                    )
                portfolio_decision = ctx.portfolio_decision

                # Persist the plan the moment it exists — a kill anywhere
                # between here and execution leaves a resumable checkpoint
                # instead of a wasted research+PM spend.
                _dc.write(ctx)

                # Second late-breach check: PM is itself a multi-second LLM
                # call (memory layers + Constructor sizing). The post-research
                # check (#60) caught breaches during research but a parse-fail
                # or empty-plan exit at this point would still skip the
                # deterministic halt until the next intra tick. Codex
                # r8 #1 caught this gap — same fix as #60, just one stage
                # later in the pipeline.
                late_breach = self._check_late_breach_and_halt(
                    run_id, "post-decision", ctx=ctx,
                )
                if late_breach is not None:
                    # The just-written checkpoint is superseded by the halt —
                    # never resume it.
                    _dc.mark_consumed("morning")
                    _dc.write_status("morning", self.DAILY_LOSS_HALT_STATUS)
                    return late_breach

            if not portfolio_decision:
                failure_status = ctx.analysis_failure_status or "pm_agent_failure"
                failure_error = ctx.analysis_failure_error or "no valid PM decision"
                logger.error(
                    "Portfolio manager produced no valid decision (%s): %s",
                    failure_status, failure_error,
                )
                return {
                    # Terminal for this slot. main.py must not repeat the full
                    # paid stack on deterministic parse/schema/grounding faults.
                    "status": failure_status, "orders": [], "run_id": run_id,
                    "error": failure_error,
                    "data_status": dict(ctx.data_status),
                    # Spec §11.2 — gross exposure, its ladder-resolved ceiling and
                    # the distance to forced liquidation, for the operator alert.
                    "leverage": dict(ctx.leverage),
                    "stop_coverage_gaps": coverage_gaps,
                }
            if not portfolio_decision.decisions:
                logger.info("Portfolio manager + Constructor: no trades suggested")
                return {
                    "status": "no_trades", "orders": [], "run_id": run_id,
                    "data_status": dict(ctx.data_status),
                    # Spec §11.2 — gross exposure, its ladder-resolved ceiling and
                    # the distance to forced liquidation, for the operator alert.
                    "leverage": dict(ctx.leverage),
                    "stop_coverage_gaps": coverage_gaps,
                }

            # Phase 4 #1: risk stage — hard filter + earnings cap + RM review + mods.
            try:
                early_exit = self._risk_stage(ctx)
            except PaidAnalysisSuspended as exc:
                return self._paid_suspension_after_late_safety(
                    run_id, session="morning", error=exc, where="paid-risk-suspended",
                    orders=forced_orders,
                )
            # The plan has now been risk-reviewed — whatever the outcome, it
            # must never be re-offered by the resume lane (an RM-rejected
            # plan retried next tick would be a veto bypass), and marking
            # BEFORE execution makes the execution at-most-once (a kill
            # mid-execution is owned by the BUY write-ahead orphan sweep,
            # not by re-running the plan).
            _dc.mark_consumed("morning")
            if early_exit is not None:
                early_exit["run_id"] = run_id
                early_exit["data_status"] = dict(ctx.data_status)
                stop_updates = getattr(
                    getattr(self, "broker", None), "stop_trade_updates", None,
                )
                if callable(stop_updates):
                    try:
                        stop_updates()
                    except Exception:
                        pass
                return early_exit

            # Phase 4 #1: execution stage — HOLDs logged, SELLs then BUYs submitted.
            orders = self._execution_stage(ctx)

            # Bookend: park idle cash above the reserve into the sweep vehicle.
            # After the BUY phase so open BUY limits are subtracted from the
            # parkable excess (see CashSweeper.park_excess).
            sweeper = self._sweeper()
            if sweeper is not None:
                try:
                    sweep_order = sweeper.park_excess(ctx)
                    if sweep_order:
                        orders.append(sweep_order)
                except Exception as e:
                    logger.warning("cash sweep: park_excess failed (non-fatal): %s", e)

            # Truthful terminal status. 2026-08-19: three risk-approved BUYs
            # were skipped as unfunded (the funding sell filled 36s after the
            # session gave up), yet the run reported status='executed' with
            # orders=[] — the day read as done and nothing retried while the
            # freed cash sat idle until midday re-parked it. When the session
            # had approved BUYs, submitted NOTHING, and at least one skip was
            # the transient funding race, report `buys_unfunded` truthfully.
            # It is terminal for this slot: automatically re-running the full
            # paid research -> PM -> RM stack amplified cost for an execution-
            # timing issue. A future execution-only checkpoint can retry this
            # without buying another decision chain.
            approved_buys = [
                d for d in (portfolio_decision.decisions or [])
                if d.action == "BUY"
            ]
            unfunded = [
                s for s in ctx.execution_skips
                if s.get("reason") == "insufficient_cash"
            ]
            # Sweep bookkeeping orders are not "the session traded" — only
            # real BUY/SELL submissions count against the retry decision.
            real_orders = [
                o for o in orders
                if not (isinstance(o, dict)
                        and str(o.get("action", "")).startswith("SWEEP_"))
            ]
            if approved_buys and unfunded and not real_orders:
                logger.warning(
                    "=== Morning run: %d approved BUY(s), 0 submitted, "
                    "%d unfunded skip(s) — reporting terminal "
                    "buys_unfunded ===", len(approved_buys), len(unfunded),
                )
                return {
                    "status": "buys_unfunded", "orders": orders,
                    "run_id": run_id,
                    "data_status": dict(ctx.data_status),
                    # Spec §11.2 — gross exposure, its ladder-resolved ceiling and
                    # the distance to forced liquidation, for the operator alert.
                    "leverage": dict(ctx.leverage),
                    "stop_coverage_gaps": coverage_gaps,
                    "execution_skips": list(ctx.execution_skips),
                }
            if not real_orders:
                logger.info(
                    "=== Morning run complete: no equity order submitted "
                    "(not marking executed) ===",
                )
                return {
                    "status": "no_orders", "orders": orders,
                    "run_id": run_id,
                    "data_status": dict(ctx.data_status),
                    # Spec §11.2 — gross exposure, its ladder-resolved ceiling and
                    # the distance to forced liquidation, for the operator alert.
                    "leverage": dict(ctx.leverage),
                    "stop_coverage_gaps": coverage_gaps,
                    "execution_skips": list(ctx.execution_skips),
                }
            logger.info("=== Morning run complete: %d orders executed ===", len(orders))
            return {
                "status": "executed", "orders": orders, "run_id": run_id,
                "data_status": dict(ctx.data_status),
                # Spec §11.2 — gross exposure, its ladder-resolved ceiling and
                # the distance to forced liquidation, for the operator alert.
                "leverage": dict(ctx.leverage),
                "stop_coverage_gaps": coverage_gaps,
                "execution_skips": list(ctx.execution_skips),
            }
        finally:
            # Phase 3: ask broker which of today's submitted orders actually filled.
            # Unfilled ones get flagged so PM memory / calibration skip them.
            self._reconcile_fills(ctx)
            # Fills (or stop-outs since snapshot) change the book. Re-read
            # the broker; do not reuse the pre-execution list.
            self._sync_positions_from_broker()

    def run_midday(self) -> dict:
        """13:00 ET — position reviewer, patient disposition."""
        return self.run_position_review(session_type="midday")

    def run_close(self) -> dict:
        """15:30 ET — position reviewer, act-on-trigger disposition.
        17.5 hours until next intraday control; genuine thesis triggers
        fire now rather than waiting for tomorrow morning."""
        return self.run_position_review(session_type="close")

    def _build_position_facts(self, positions, morning_trades, total_value):
        """Deterministic per-position metrics surfaced to the reviewer.

        Python does the math (progress %, pace, distance-to-stop/target,
        winner flags) so the LLM sees clean numbers and just interprets
        them. Prevents hallucination of percentages.
        """
        # Morning opening-row lookup by symbol for stop/target/days_held.
        # BUY and SHORT are separate: a leftover purchase on the same
        # ticker is not the short's entry.
        buy_rows: dict[str, dict] = {}
        short_rows: dict[str, dict] = {}
        for t in morning_trades or []:
            sym = t.get("symbol")
            act = (t.get("action") or "").upper()
            if not sym or act not in ("BUY", "SHORT"):
                continue
            bucket = short_rows if act == "SHORT" else buy_rows
            if sym not in bucket:
                bucket[sym] = t

        facts: dict[str, dict] = {}
        for p in positions:
            sym = p.symbol
            entry = p.avg_entry
            cur = p.current_price

            # Find the last executed opening row for this symbol to derive
            # target/stop/days_held. A short must read the SHORT row, not a
            # leftover BUY on the same ticker. Falls back to the morning
            # row of the same side, then the matching last-open lookup.
            if p.qty < 0:
                buy = short_rows.get(sym)
                if not buy:
                    try:
                        buy = self.db.get_symbol_last_buy(sym, action="SHORT")
                    except Exception:
                        buy = None
            else:
                buy = buy_rows.get(sym)
                if not buy:
                    try:
                        buy = self.db.get_symbol_last_buy(sym)
                    except Exception:
                        buy = None

            stop_loss = float((buy or {}).get("stop_loss") or 0)
            # The LIVE target — re-derived if a structural event has since
            # triggered `src.risk.target_revision`. Used for
            # `distance_to_target_pct` and for display, and NEVER as the
            # denominator of progress; see `progress_target` below.
            take_profit = float((buy or {}).get("take_profit") or 0)
            # The ENTRY target, frozen as `initial_take_profit` at insert.
            #
            # THIS, not `take_profit`, is the denominator of
            # `thesis_progress_pct` and therefore of `pace`. The target is
            # the denominator, so RAISING it mechanically LOWERS progress and
            # LOWERS pace — and both are in
            # `src.risk.exit_guard._HIGHER_IS_BETTER`. Measured against the
            # live target, a revision on good news (the name gaps through the
            # resistance the target sat on, the target is re-derived higher)
            # would show up in `MetricDeltas.worsened`, which clears
            # `net_improved`, which switches OFF `veto_contradicted_exit` —
            # so the position would instantly look less progressed and
            # slower than an hour earlier, and a "this position is stalling"
            # SELL that was previously blocked would go through. A machine
            # for making winners look stalled and then selling them.
            #
            # Pinning the denominator removes that by construction rather
            # than by a special case in the guard: a revision cannot move
            # either metric at all, so it cannot appear as a deterioration.
            # Same reasoning as the pinned horizon two paragraphs down — a
            # yardstick that moves measures nothing.
            #
            # Legacy rows that predate the column fall back to the live
            # target, which for them IS the entry target: `take_profit` was
            # only ever written by `insert_trade` before the revision path
            # existed (see the `initial_take_profit` migration in
            # `src/storage/db.py`), and a row with no revision has nothing
            # to diverge from.
            progress_target = float(
                (buy or {}).get("initial_take_profit") or take_profit or 0
            )
            # The ENTRY stop, frozen as `initial_stop_loss` on first
            # write-back. R-multiple's denominator is the bet that was
            # actually made, not the level a trail later ratcheted it to
            # (audit §1.4).
            from src.execution.stop_records import recorded_initial_stop
            initial_stop = recorded_initial_stop(buy)

            # RC1: after any TRAIL_STOP the BUY row's stop is stale-WIDE —
            # the reviewer would see a fat distance_to_stop and keep
            # ratcheting. Prefer live broker truth; fall back to the BUY row.
            try:
                live_stop = self.broker.get_current_stop_price(sym)
            except Exception:  # noqa: BLE001
                live_stop = None
            if isinstance(live_stop, (int, float)) and live_stop > 0:
                stop_loss = float(live_stop)

            # days_held — from BUY timestamp; fall back to None.
            #
            # sessions_held is the weekend-aware companion count (Mon-Fri
            # only, see `trading_calendar.trading_sessions_held`) — the
            # noise-band scaling below needs TRADING SESSIONS, not calendar
            # days, per the 2026-09-04 audit follow-up.
            days_held = None
            sessions_held = None
            buy_ts = (buy or {}).get("timestamp")
            if buy_ts:
                try:
                    from src.trading_calendar import to_et, trading_sessions_held
                    from datetime import datetime as _dt
                    dt = _dt.fromisoformat(buy_ts.replace("Z", "+00:00")) if "T" in buy_ts \
                        else _dt.strptime(buy_ts, "%Y-%m-%d %H:%M:%S")
                    entry_date = to_et(dt).date()
                    days_held = (et_today() - entry_date).days
                    days_held = max(0, days_held)
                    sessions_held = trading_sessions_held(entry_date, et_today())
                except Exception:
                    days_held = None
                    sessions_held = None

            # Phase 3.1 — the thesis horizon and setup type PINNED AT ENTRY.
            # Read from the BUY row, never recomputed. NULL for positions
            # opened before this landed, and for sweep/resume-lane buys with
            # no analysis; those get no pace figure rather than a fabricated
            # one.
            pinned_horizon = (buy or {}).get("expected_horizon_sessions")
            try:
                pinned_horizon = int(pinned_horizon) if pinned_horizon else None
            except (TypeError, ValueError):
                pinned_horizon = None
            setup_type = (buy or {}).get("setup_type") or None

            # Progress: 0 at entry, 100 at target, >100 beyond target.
            #
            # DISABLED for breakout ("Type B") setups. A breakout's target is a
            # measured-move reference, not a level anyone is defending — there
            # is no overhead structure for price to progress TOWARD, so
            # "progress" against it measures nothing and "pace" against that
            # nothing is worse. Breakouts are managed by trailing instead
            # (spec Phase 3.7). `setup_type` is pinned at entry alongside the
            # horizon.
            progress_pct = None
            pace = None
            pace_status = "unavailable"
            if setup_type == "breakout":
                pace_status = "n/a_breakout"
            else:
                if progress_target and entry and progress_target != entry:
                    progress_pct = (cur - entry) / (progress_target - entry) * 100

                # Pace against the horizon the ANALYST pinned at entry.
                #
                # This replaces `days_held / avg_hold_days`, where avg_hold_days
                # came from the system's own rolling 30-day realized-trade
                # calibration (~2.0 days in practice). That made pace a
                # feedback loop: every early sale shrank the average, which made
                # every surviving position look stalled, which drove more early
                # sales. A self-tightening noose, and the single largest
                # identified P&L defect in the system. A trade's expected
                # horizon must never be derived from the system's own past
                # behaviour.
                #
                # Board item 91: `pinned_horizon` (`expected_horizon_sessions`)
                # is denominated in TRADING SESSIONS, so both sides of this
                # division must be — `sessions_held`, never the calendar-day
                # `days_held`. A weekend adds two calendar days and zero
                # sessions; dividing by calendar days made every position look
                # slower than it is, worst on the short horizons this desk
                # trades, and worse across a holiday weekend. `sessions_held`
                # is the same weekend-aware count the noise-band scaling above
                # already uses (`trading_calendar.trading_sessions_held`) —
                # no new number, just the one already computed above.
                if progress_pct is None or not pinned_horizon or sessions_held is None:
                    pace_status = "unavailable_no_pinned_horizon"
                elif sessions_held < max(1, pinned_horizon / 3):
                    # Below one third of the pinned horizon the metric is
                    # mathematically meaningless — a thesis given 15 sessions
                    # cannot be "behind schedule" on session 2, and reading it
                    # as such is exactly how a day-5 position gets sold for
                    # "not progressing".
                    pace_status = "too_early"
                else:
                    time_fraction = sessions_held / pinned_horizon
                    if time_fraction > 0:
                        pace = progress_pct / (time_fraction * 100)
                        pace_status = "measured"

            # Distance-to-stop / distance-to-target as % of current price.
            #
            # `distance_to_stop_pct` MUST be side-aware. A LONG's stop sits
            # BELOW price, so `(cur - stop_loss)` is the room left and is
            # positive while the position is alive. A SHORT's stop sits
            # ABOVE price, so that same expression is NEGATIVE, and it moves
            # the WRONG way: it gets MORE negative (looks worse under
            # `_HIGHER_IS_BETTER`) as the price falls further from the stop
            # — i.e. as the position gets safer. Mirror the numerator for a
            # short (`p.qty < 0`) so the metric means the same thing on both
            # sides: positive, and falling as the stop gets closer. See
            # `src/risk/exit_guard.py::_HIGHER_IS_BETTER`, which trusts this
            # value to already be direction-corrected.
            dist_stop_pct = None
            dist_target_pct = None
            if stop_loss and cur > 0:
                dist_stop_pct = (
                    (stop_loss - cur) if p.qty < 0 else (cur - stop_loss)
                ) / cur * 100
            if take_profit and cur > 0:
                dist_target_pct = (take_profit - cur) / cur * 100

            # GROSS-leverage weight — the one definition (see
            # `src.risk.rules.weight_pct_of`). Raw here meant the reviewer
            # was shown a 3x fund at a third of the weight the engine caps
            # it at, and the drift flag below never fired on one.
            weight_pct = position_weight_pct(p, total_value)

            # Winner flags. `unrealized_pnl_pct` divides by |entry x qty|;
            # a short's negative qty otherwise flips the sign and feeds the
            # parabolic/drift flags the wrong side. None = unknowable, which
            # is not a flag either way.
            pnl_pct = unrealized_pnl_pct(p)
            parabolic_flag = (
                pnl_pct is not None and pnl_pct >= 15
                and days_held is not None and days_held < 3
            )
            drift_flag = (
                weight_pct > 12 and pnl_pct is not None and pnl_pct > 10
            )
            target_breach_flag = progress_pct is not None and progress_pct > 150

            # Vol-unit context so the reviewer reasons about stop distance
            # in ATRs, not raw % (a 3% gap is roomy for KO, suicidal for
            # RKLB). None when bars are unavailable — the prompt treats
            # missing as "unknown", never as zero.
            atr = self._atr_for_symbol(sym)
            atr_pct = round(atr / cur * 100, 2) if (atr and cur > 0) else None
            stop_distance_atrs = None
            if atr and stop_loss and cur > stop_loss:
                stop_distance_atrs = round((cur - stop_loss) / atr, 2)

            # R-multiple (audit §1.4) — profit in units of the risk taken.
            # `thesis_progress_pct` measures distance to TARGET, a different
            # question that does not normalise for how much was risked: a name
            # 20% of the way to a distant target may be +2R or +0.3R, and only
            # the second is a reason to leave it alone.
            from src.risk.metrics import position_risk as _position_risk
            risk = _position_risk(
                symbol=sym, qty=p.qty, entry=entry, current_price=cur,
                stop=stop_loss or None, initial_stop=initial_stop or None,
            )

            facts[sym] = {
                "days_held": days_held,
                "sessions_held": sessions_held,
                "expected_horizon_sessions": pinned_horizon,
                "setup_type": setup_type,
                "pace_status": pace_status,
                "r_multiple": risk.r_multiple,
                "initial_stop": initial_stop or None,
                "risk_released": risk.risk_released,
                "open_risk_dollars": risk.open_risk_dollars,
                "thesis_progress_pct": progress_pct,
                "pace": pace,
                "distance_to_stop_pct": dist_stop_pct,
                "distance_to_target_pct": dist_target_pct,
                # Provenance for `distance_to_stop_pct`, not metrics of
                # their own: that metric is a function of BOTH terms, so
                # without them a rise caused by the stop being widened is
                # indistinguishable from a rise caused by the price moving
                # away. It read as "(improved)" on real 2026-09-01
                # snapshots for V, CMCSA and DIS while all three were
                # deteriorating. See `exit_guard._STOP_DEPENDENT_METRIC`.
                "stop_loss": stop_loss or None,
                "current_price": cur if cur > 0 else None,
                # Side, so the provenance recomputation in
                # `exit_guard.MetricDeltas._distance_move_is_price_driven`
                # can mirror the SAME formula this block uses above
                # (`dist_stop_pct`) rather than assume every position is a
                # long. Never scored — not a metric, just qty's sign.
                "qty": p.qty,
                "weight_pct": weight_pct,
                "parabolic_flag": parabolic_flag,
                "drift_flag": drift_flag,
                "target_breach_flag": target_breach_flag,
                "atr_pct": atr_pct,
                "stop_distance_atrs": stop_distance_atrs,
                # Both targets, named for what they are. `take_profit` is the
                # live (possibly re-derived) number; `entry_take_profit` is
                # the pinned entry derivation that `thesis_progress_pct` and
                # `pace` above are measured against. Surfaced separately so
                # neither the reviewer nor the cockpit has to guess which
                # number a progress figure came from.
                "take_profit": take_profit or None,
                "entry_take_profit": progress_target or None,
                "target_revised": bool(
                    take_profit and progress_target
                    and round(take_profit, 2) != round(progress_target, 2)
                ),
            }
        return facts

    #: Metric keys snapshotted after every review and compared on the next one.
    #: Kept deliberately small — these are the numbers a "stalling" claim is
    #: actually about, and every one of them has a defined direction.
    #: `stop_loss` / `current_price` / `qty` are NOT metrics and are never
    #: scored. `stop_loss` and `current_price` are the two terms of
    #: `distance_to_stop_pct`, snapshotted so the next review can attribute
    #: a move in it to the market or to the desk's own stop (2026-09-18).
    #: `qty` supplies the SIDE that same recomputation needs to mirror the
    #: numerator correctly for a short (2026-09-18 follow-up, alongside the
    #: sign fix to `distance_to_stop_pct` itself). Snapshots written before
    #: 2026-09-18 lack all three; `compute_deltas` handles that explicitly.
    _REVIEW_METRIC_KEYS = (
        "thesis_progress_pct", "distance_to_stop_pct", "r_multiple", "pace",
        "days_held", "expected_horizon_sessions", "setup_type", "pace_status",
        "stop_loss", "current_price", "qty",
    )

    def _build_review_metric_deltas(self, position_facts: dict, *, run_id: str) -> dict:
        """`{symbol: MetricDeltas}` versus this seat's previous review.

        Phase 3.2 / audit §1.5. Degrades to empty deltas (never to a wrong
        comparison) when the prior snapshot is missing or unparseable — a
        first look at a position legitimately has nothing to compare against,
        and the guard downstream treats "no prior" as "do not veto".
        """
        import json as _json
        from src.risk.exit_guard import compute_deltas

        symbols = list(position_facts or {})
        if not symbols:
            return {}
        try:
            prior_rows = self.db.get_prior_position_review_metrics(
                symbols, exclude_run_id=run_id,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "review memory: prior-metric read failed (%s) — this review "
                "runs without memory of its own last look", e,
            )
            prior_rows = {}
        deltas: dict = {}
        for symbol, current in (position_facts or {}).items():
            row = prior_rows.get(symbol.upper())
            prior = None
            if row:
                try:
                    prior = _json.loads(row.get("evidence_json") or "{}")
                except (TypeError, ValueError) as e:
                    logger.warning(
                        "review memory: %s prior snapshot is unparseable (%s) — "
                        "treating as no prior", symbol, e,
                    )
                    prior = None
            deltas[symbol.upper()] = compute_deltas(
                symbol, prior, current,
                prior_timestamp=(row or {}).get("timestamp"),
            )
        return deltas

    def _persist_review_metrics(self, position_facts: dict, *, run_id: str) -> None:
        """Snapshot this review's metrics so the next one can compare.

        Never raises: losing a snapshot degrades the NEXT review to "no prior",
        which the guard handles, and must not take down the current session.
        """
        import json as _json
        for symbol, facts in (position_facts or {}).items():
            payload = {
                key: facts.get(key)
                for key in self._REVIEW_METRIC_KEYS
                if facts.get(key) is not None
            }
            if not payload:
                continue
            try:
                self.db.save_position_review_metrics(
                    run_id=run_id, symbol=symbol,
                    metrics_json=_json.dumps(payload, sort_keys=True),
                )
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    "review memory: failed to snapshot %s (%s) — next review "
                    "will have no prior for it", symbol, e,
                )

    def _build_own_recent_decisions(self, limit: int = 3) -> str:
        """Pull last N position_reviewer sessions from agent_logs.

        Anti-flip-flop memory: shows the reviewer its own previous 3 sessions'
        actions per symbol so it can't silently reverse itself within hours
        without a named trigger. Complement to PM's `_build_pm_recent_decisions`.
        """
        try:
            # No before_date cutoff (audit round 2): the 15:30 close session
            # must see the 13:00 midday row — this anti-flip-flop memory says
            # "don't reverse yourself WITHIN HOURS", and the ET-midnight
            # cutoff excluded exactly those rows. The current session's own
            # row is inserted AFTER this builder runs, so no self-read.
            rows = self.db.get_recent_agent_outputs(
                agent_name="position_reviewer", limit=limit,
            )
        except Exception as e:
            logger.warning("own_recent_decisions: DB fetch failed: %s", e)
            return ""
        if not rows:
            return ""
        lines: list[str] = []
        for row in reversed(rows):  # oldest → newest
            ts = (row.get("timestamp") or "")[:16]
            data = self._parse_logged_agent_response(row)
            if not isinstance(data, dict):
                continue
            actions = data.get("actions") or []
            if not isinstance(actions, list):
                continue
            action_bits = []
            for a in actions:
                if not isinstance(a, dict):
                    continue
                sym = a.get("symbol", "?")
                act = a.get("action", "?")
                if act == "HOLD":
                    continue  # only surface actionable past decisions
                action_bits.append(f"{sym}:{act}")
            if action_bits:
                lines.append(f"- {ts}: {', '.join(action_bits[:8])}")
        return "\n".join(lines)

    def run_position_review(self, session_type: str = "midday") -> dict:
        """Midday/close, plus the durable record of its own output.

        Same gap as `run_morning` (2026-09-18 sweep): `leverage`,
        `stop_coverage_gaps` and this session's own `daily_pnl`/`total_pnl`
        snapshot are computed from live broker state and handed to the
        notifier with no other durable home. The body is unchanged; this
        wrapper persists every return path, keyed by (date, session_type)
        so midday and close each keep their own row. Fail-soft.
        """
        if session_type not in ("midday", "close"):
            raise ValueError(f"run_position_review: unknown session_type {session_type!r}")
        result = self._run_position_review_body(session_type)
        self._persist_session_report(session_type, result)
        return result

    def _run_position_review_body(self, session_type: str) -> dict:
        """Unified entry for both midday (13:00 ET) and close (15:30 ET).

        Same memory layers, same schema, same agent. Session bias is injected
        via prompt language driven by `session_type`. Everything else — force
        de-lever / ex-div / news / earnings / LLM review /
        emergency liquidate / execution / reconcile — is identical.
        """
        ctx = RunContext.start(session_type)
        run_id = ctx.run_id
        logger.info("=== %s check: %s ===", session_type.capitalize(), run_id)

        if not self._is_trading_day():
            logger.info("%s run skipped: market closed for non-trading day", session_type)
            return {"status": "market_holiday", "positions": 0, "orders": [], "run_id": run_id}

        halt = self._kill_switch_halt_result(run_id, positions=0)
        if halt is not None:
            return halt

        self._activate_cost_session(run_id, session_type)

        # Early-close check. On half-day sessions (day after Thanksgiving 13:00
        # close; July 3 half-day) the launchd-gated midday (13:00-14:30 ET) and
        # close (15:30-15:55 ET) windows fire against a market that's already
        # shut. Every submit would land as rejected; the LLM would still burn
        # tokens reviewing. Skip cleanly when today's session_close has already
        # passed. `isinstance(datetime)` instead of `is not None` because we
        # can only compare to a real datetime — a None or unexpected type
        # (misconfigured mock, broker returning a placeholder) defaults to
        # "proceed and let downstream checks handle it" rather than crashing.
        from datetime import datetime as _dt
        session_close = None
        if hasattr(self.broker, "get_session_close"):
            try:
                session_close = self.broker.get_session_close()
            except Exception as exc:
                logger.warning(
                    "early_close check: get_session_close failed (%s); "
                    "proceeding with %s run",
                    exc, session_type,
                )
                session_close = None
        if isinstance(session_close, _dt) and et_now() >= session_close:
            logger.info(
                "%s run skipped: regular session already closed today at %s ET "
                "(early-close day)",
                session_type, session_close.strftime("%H:%M"),
            )
            return {
                "status": "early_close",
                "positions": 0,
                "orders": [],
                "run_id": run_id,
                "session_close_et": session_close.isoformat(),
            }

        # Drain orphaned protection-restore intents from prior sessions.
        # If morning bailed on a finalize and the SELL has since become
        # terminal, recover stop coverage NOW rather than waiting for
        # next-morning's drain — codex r8 #2.
        self._drain_pending_protection_restores()
        self._drain_pending_repegs()
        self._reconcile_orphan_pending_submits()  # audit F4
        # Broker-truth coverage audit (independent of the WAL).
        coverage_gaps = self._reconcile_stop_coverage()
        # Sweep retired (owner mandate 2026-09-17): release any held vehicle.
        self._release_retired_cash_park(run_id)
        # Broker-truth EXIT audit (2026-08-28 ONDS/CCJ) — midday/close run
        # every trading day, so this is the most frequent chance to catch a
        # stop that fired since the last pass and write it back before the
        # reviewer builds its "what happened today" picture.
        try:
            self._reconcile_stop_out_fills(run_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "%s stop-out reconcile failed (non-fatal): %s",
                session_type, exc,
            )

        # 1. Sync positions (snapshot into ctx)
        account = self.broker.get_account()
        positions = self.broker.get_positions()
        cash = account["cash"]
        total_value = account["portfolio_value"]
        last_equity = account.get("last_equity", total_value)
        ctx.account = account
        ctx.positions = positions
        ctx.cash = cash
        ctx.deployable_cash = self._compute_deployable_cash(cash, positions)
        ctx.total_value = total_value
        ctx.last_equity = last_equity

        # Replace the positions snapshot (drops rows for symbols no longer held).
        self._sync_positions_from_broker(positions)

        # 1a. Cash-only safety net — force-sell if the account drifted into
        # margin. Refreshes ctx fields on completion.
        forced_orders = self._force_delever(ctx)

        # 1b. Spec §11.2 — the gross-exposure ceiling and its de-levering
        # ladder. Runs on midday and close too, not just the morning: the
        # ceiling steps down on measured drawdown, and waiting for tomorrow's
        # session to act on it is the coupling the ladder exists to avoid.
        # Computed from account state alone — no agent output is an input.
        forced_orders = list(forced_orders) + self._enforce_gross_ceiling(ctx)
        if forced_orders:
            # Reconcile immediately so the FORCE_DELEVER rows flip from
            # fill_status='submitted' to 'filled' before the reviewer's
            # morning_trades query (executed_only=True) is built. Otherwise
            # the reviewer can't see the same-session forced sells in
            # system_action_lines and would reason about a shrunken book
            # without the explanation.
            self._reconcile_fills(ctx)
        positions = ctx.positions
        cash = ctx.cash
        total_value = ctx.total_value
        last_equity = ctx.last_equity
        self._sync_positions_from_broker(positions)

        # Today's P&L for the Telegram feed (item: "Session P&L" rename) —
        # same basis as `run_intra_check`/`run_evening`: the broker's own
        # last_equity (prior trading-day close), not a run-scoped figure.
        daily_pnl = (total_value - last_equity) if last_equity else 0.0
        daily_return_pct = (daily_pnl / last_equity * 100) if last_equity else 0.0
        total_pnl, total_return_pct, total_pnl_since = (
            self._total_pnl_since_reset(total_value)
        )

        # Hard circuit breaker: if the session is already through the daily-loss
        # limit, bypass all LLM/news/earnings work and HALT (docs/WORK.md item
        # 32 — the force-liquidation this used to do is deleted). Keeps the
        # deterministic safety path alive even when the reviewer
        # model/provider is unavailable.
        loss_violation, _bl, _pnl, loss_basis = self._daily_loss_breach(
            ctx.account, positions,
        )
        if loss_violation and positions:
            logger.warning(
                "%s risk alert before LLM review: %s — bypassing reviewer and "
                "halting new risk", session_type.capitalize(),
                loss_violation.message,
            )
            halt = self._halt_on_daily_loss_breach(
                positions, loss_violation, run_id,
                where=f"{session_type} pre-review", basis=loss_basis, ctx=ctx,
            )
            halt["session"] = session_type
            halt["review"] = None
            halt["daily_pnl"] = daily_pnl
            halt["daily_return_pct"] = daily_return_pct
            halt["total_pnl"] = total_pnl
            halt["total_return_pct"] = total_return_pct
            halt["total_pnl_since"] = total_pnl_since
            return halt

        # 1b. (DELETED 2026-09-12, owner decision.) A midday "auto take-profit"
        # used to sit here: sell 15% of any position once its unrealised
        # gain reached 30%. Both numbers were tuned off ONE trade (a GOOGL
        # trim at +27% on 2026-04-30) — hindsight-tuning on n=1 — and, more
        # fundamentally, it was a preset profit target: a fixed fraction at
        # a fixed gain decided in advance with no reference to what the
        # instrument is doing. The owner removed that class of logic when he
        # removed reward:risk as a universal gate: the reward side of a
        # trade cannot be predetermined because the holding period is
        # unknown, and profit-taking belongs to the trailing stop
        # (`src/risk/trailing.py`). The rule predated QAMC and was never
        # ratified against that doctrine. The ONLY exit rule is the
        # trailing stop; `tests/test_pipeline.py::
        # test_no_fixed_gain_automatic_profit_trim_exists` fails if a
        # fixed-gain trim is reintroduced.

        # 1c. Ex-dividend stop adjustment (both sessions — a dividend tomorrow
        # is still a dividend tomorrow no matter which session looks at it).
        exdiv_orders = self._handle_ex_dividends(positions, run_id)

        # Ex-dividend actions and all protection reconciliation above are
        # deterministic. A latched paid-analysis breaker stops only at this
        # boundary, before news/reviewer model requests.
        orders = list(forced_orders) + list(exdiv_orders)
        try:
            self._require_paid_analysis(f"{session_type}_analysis")
        except PaidAnalysisSuspended as exc:
            self._reconcile_fills()
            return self._paid_suspension_after_late_safety(
                run_id, session=session_type, error=exc,
                where=f"{session_type}-paid-preflight",
                orders=orders,
                extra={"session": session_type, "positions": len(positions),
                       "stop_coverage_gaps": coverage_gaps,
                       # Spec §11.2 — gross exposure and its ceiling.
                       "leverage": dict(ctx.leverage)},
            )

        # 2. News + Earnings update — capture developments since morning.
        try:
            # held_symbols: current book, cash-sweep vehicle excluded (see
            # _news_held_symbols), in broker snapshot order (stable within
            # this run — see _run_news_update's ordering contract). No
            # separate "candidate_symbols" concept exists at this point in
            # the midday/close path (unlike MorningResearchStage, which has
            # ctx.admitted_symbols computed before news fetches) — a
            # deliberate scope limit, not an oversight; see the PR
            # description.
            session_news, session_news_coverage = self._run_news_update(
                run_id, session=session_type,
                held_symbols=self._news_held_symbols(positions),
            )
        except PaidAnalysisSuspended as exc:
            self._reconcile_fills()
            return self._paid_suspension_after_late_safety(
                run_id, session=session_type, error=exc,
                where=f"{session_type}-paid-news",
                orders=orders,
                extra={"session": session_type, "positions": len(positions),
                       "stop_coverage_gaps": coverage_gaps,
                       # Spec §11.2 — gross exposure and its ceiling.
                       "leverage": dict(ctx.leverage)},
            )
        if session_news_coverage is not None and session_news_coverage.status != "ok":
            # midday/close have no data_status mechanism of their own (that
            # is a morning-only construct today — see MorningResearchStage),
            # so a degraded wire here would otherwise be silent even after
            # the 2026-08-28 coverage fix. At minimum this keeps it out of
            # the log-only failure mode the fix exists to close.
            logger.warning("%s: %s", session_type, session_news_coverage.describe())
        if session_news:
            logger.info("%s news: %s", session_type.capitalize(), session_news.pm_briefing[:200])
        try:
            _, session_earnings = self._load_earnings_analyses(
                run_id, session=session_type, ctx=ctx,
            )
        except PaidAnalysisSuspended as exc:
            self._reconcile_fills()
            return self._paid_suspension_after_late_safety(
                run_id, session=session_type, error=exc,
                where=f"{session_type}-paid-earnings",
                orders=orders,
                extra={"session": session_type, "positions": len(positions),
                       "stop_coverage_gaps": coverage_gaps,
                       # Spec §11.2 — gross exposure and its ceiling.
                       "leverage": dict(ctx.leverage)},
            )
        except Exception as e:  # noqa: BLE001 — reviewer proceeds without earnings
            logger.error("%s: earnings load failed (continuing without): %s",
                         session_type, e)
            session_earnings = []

        circuit_state = self._cost_circuit_status()
        if circuit_state.get("suspended"):
            self._reconcile_fills()
            return self._paid_suspension_after_late_safety(
                run_id, session=session_type, orders=orders,
                where=f"{session_type}-post-news-circuit-open",
                error=PaidAnalysisSuspended(
                    str(circuit_state.get("trigger_detail") or "cost circuit opened")
                ),
                extra={"session": session_type, "positions": len(positions),
                       "stop_coverage_gaps": coverage_gaps,
                       # Spec §11.2 — gross exposure and its ceiling.
                       "leverage": dict(ctx.leverage)},
            )

        # 3. LLM position review — memory-heavy, 6-step CoT.
        macro_summary = self.macro.get_macro_summary()
        macro_coverage = self.macro.last_coverage
        if isinstance(macro_coverage, MacroCoverage) and macro_coverage.status != "ok":
            # Same gap noted for news coverage just above: midday/close have
            # no data_status mechanism of their own (that is a morning-only
            # construct today — see MorningResearchStage), so a degraded
            # FRED fetch here would otherwise be silent even after the
            # Phase 4.2 macro-coverage fix. At minimum this keeps it out of
            # the log-only failure mode the fix exists to close.
            logger.warning("%s: %s", session_type, macro_coverage.describe())
        review = None
        # Adjudicated take-profit revision flags, filed per symbol whichever
        # way each one goes (revision, named refusal, named data fault).
        target_revisions: list[dict] = []
        # Pre-LLM orders (take-profit + ex-div) feed into the same bucket.

        # LLM view: the cash-sweep vehicle is cash-equivalent, not a
        # position — the reviewer must never see it, hold-grade it, or sell
        # it. Raw `positions` stays in scope for the paths that need broker
        # truth (emergency liquidate below sells EVERYTHING, parked cash
        # included).
        #
        # 2026-08-19 SGOV/deployable-liquidity forensic: crediting the
        # parked vehicle's market value straight into "cash" (2026-07-16
        # audit's fix) told the reviewer money was instantly available when
        # it was not — Alpaca settlement (T+1) means a same-day SGOV
        # liquidation is not reliably spendable by the time execution
        # rechecks. `review_cash` is now `ctx.deployable_cash` (Alpaca's
        # settled non-margin buying power); `reserve_balance` carries the
        # parked value separately, informationally, so the reviewer still
        # knows the reserve exists without treating it as instant cash.
        review_positions = positions
        review_cash = ctx.deployable_cash
        reserve_balance = 0.0
        sweeper = self._sweeper()
        if sweeper is not None:
            review_positions, parked = sweeper.split_positions(positions)
            if parked is not None:
                reserve_balance = sweeper.parked_value(positions)

        if review_positions:
            # Sweep any straggler fills before building the reviewer prompt.
            # run_morning's final reconcile is run_id-scoped, so a BUY whose
            # fill landed AFTER morning's wait window stays at fill_status=
            # 'submitted' in DB even though broker shows the position. The
            # reviewer's executed_only=True query would skip it, losing
            # entry/stop/thesis context for that holding. An unscoped
            # reconcile here is cheap (1 broker call per pending row) and
            # closes that gap. Codex r11 P2.
            self._reconcile_fills()
            morning_trades = self.db.get_trades(
                limit=50, today_only=True, executed_only=True,
            )

            # Board item 89 defect 3 (and item 104's eighth trade-affecting
            # prompt defect, which is the same root cause one layer down).
            #
            # `morning_trades` is deliberately `today_only=True` — the
            # reviewer's "what already happened this session" block depends
            # on that and must keep it. But it is ALSO the only source the
            # reviewer had for a position's ENTRY THESIS, so every position
            # opened on an earlier day rendered "Entry thesis: (unavailable
            # — position opened before today)". The reason was never
            # missing: it is on the entry row in `trades`, which the lookup
            # simply never searched. The reviewer then wrote "thesis
            # unavailable" into its hold reasons, and those reasons go
            # straight into the owner's message.
            #
            # `get_symbol_last_buy` is the existing unrestricted lookup —
            # the same "most recent executed opening row for this symbol,
            # no date bound" query the evening thesis-health context and
            # the cockpit's "why do we hold this" endpoint already use. No
            # second lookup is written here, and nothing about the review
            # DECISION changes: this only stops a fact the desk already
            # holds from being reported as absent.
            entry_context: dict[str, dict] = {}
            for _p in review_positions:
                _sym = getattr(_p, "symbol", None)
                if not _sym:
                    continue
                # A short's opening row is a SHORT, not a BUY, and mixing
                # the two would hand a short a long's thesis and stop —
                # the precise confusion `get_symbol_last_buy` refuses by
                # taking the opening action explicitly.
                _action = "SHORT" if getattr(_p, "qty", 0) < 0 else "BUY"
                try:
                    _row = self.db.get_symbol_last_buy(_sym, action=_action)
                except Exception as e:  # noqa: BLE001
                    logger.warning(
                        "%s: entry-context lookup failed for %s: %s",
                        session_type, _sym, e,
                    )
                    continue
                if _row:
                    entry_context[_sym] = _row

            # Reuse morning's macro_analysis from macro_store so the
            # reviewer sees the same regime the PM committed to today.
            macro_analysis_dict = None
            try:
                macro_analysis_dict = self.macro_store.load_last_state()
            except Exception as e:
                logger.warning("%s: macro_store load failed: %s", session_type, e)

            # Pre-compute deterministic per-position metrics.
            #
            # Phase 3.1: this used to fetch `avg_hold_days` from the rolling
            # 45-day realized-trade calibration and hand it to the facts
            # builder as the denominator of `pace`. That is the feedback loop —
            # the system's own selling behaviour set the bar every surviving
            # position was measured against. The horizon is now pinned at entry
            # on the trade row and the calibration query is gone from this
            # path entirely, so there is nothing to accidentally reconnect.
            position_facts = self._build_position_facts(
                review_positions, morning_trades, total_value,
            )

            # Phase 3.2 / audit §1.5 — the reviewer's memory of its OWN prior
            # numbers. `_build_own_recent_decisions` below replays past ACTIONS
            # and drops HOLDs, so without this the seat rebuilds its view from
            # scratch every session and can report a position deteriorating
            # while everything it measured six hours ago improved. That is
            # exactly how EPD and MRVL were sold on intact theses.
            metric_deltas = self._build_review_metric_deltas(
                position_facts, run_id=run_id,
            )

            # Memory layers — share the same helpers PM uses.
            weekly_narrative = self._build_weekly_narrative()
            macro_trajectory = self._build_macro_trajectory()
            active_state_changes = self._build_active_state_changes()
            calibration_note = self._build_calibration_note()
            own_recent_decisions = self._build_own_recent_decisions()
            # v2: evening's per-trade grades feed back into position_reviewer.
            # 14-day rolling counts of correct/premature/wrong SELLs (and BUYs)
            # let the reviewer lean patient when past SELLs trended premature.
            trade_grade_summary = self._build_trade_grade_summary(lookback_days=14)
            # Same-day trim discipline — feeds the prompt + the executor.
            # See _symbols_already_trimmed_today for the AMZN-2026-05-04 origin.
            #
            # 2026-09-17 XOM incident: _symbols_already_trimmed_today reads
            # today's trade rows with no notion of what is still held — a
            # symbol that was fully SOLD (not merely trimmed) this morning
            # comes back exactly like one that still has shares open. The
            # prompt section this feeds tells the LLM to render a HOLD
            # decision for every name in the set ("HOLD them at this
            # session unless..."), so a fully-closed name that never
            # appears in `review_positions` (broker truth, fetched above)
            # still got a fabricated action out of the model — 7 actions
            # returned against 6 real broker positions. Intersect with the
            # symbols actually being reviewed right here, at the one place
            # both sets are in scope, so a sold-out name can never reach
            # the reviewer's prompt or its action list again.
            already_trimmed_today = self._symbols_already_trimmed_today() & {
                p.symbol for p in review_positions
            }

            yesterday_insights = self.db.get_latest_insights(before_date=session_date_key())
            recent_performance = self._compute_recent_performance(last_equity)

            # Margin capacity for the reviewer prompt — WORDING ONLY, mirrors
            # the same fix threaded into the PM prompt (`DecisionStage.run`
            # in `src/pipeline_stages.py`). Reuses the EXACT §11.2
            # computation execution's submit loop sizes entries against
            # (`_entry_deployment_budget`, which itself resolves the ladder
            # via `_session_gross_ceiling`) — never a second formula. Book
            # state here (positions/equity/held-gross) has not changed since
            # ctx was built above, so this is the same headroom execution
            # will see for this session's entries.
            from src.pipeline_stages import (
                _entry_deployment_budget, _session_gross_ceiling,
            )
            margin_headroom_usd, margin_ladder_backed, _margin_headroom_note = (
                _entry_deployment_budget(
                    self, ctx, review_positions, total_value, review_cash,
                )
            )
            _margin_ceiling = _session_gross_ceiling(self, ctx)
            margin_ladder_multiple = (
                _margin_ceiling.ceiling_x if _margin_ceiling is not None else None
            )
            margin_ladder_rung = (
                _margin_ceiling.rung if _margin_ceiling is not None else None
            )

            review_kwargs = dict(
                    positions=review_positions,
                    macro_summary=macro_summary,
                    cash_balance=review_cash,
                    reserve_balance=reserve_balance,
                    total_value=total_value,
                    session_type=session_type,
                    position_facts=position_facts,
                    metric_deltas=metric_deltas,
                    morning_trades=morning_trades,
                    # Board item 89 defect 3 — see the build above.
                    entry_context=entry_context,
                    news_intel=session_news,
                    earnings_analyses=session_earnings,
                    macro_analysis=macro_analysis_dict,
                    weekly_narrative=weekly_narrative,
                    macro_trajectory=macro_trajectory,
                    active_state_changes=active_state_changes,
                    calibration_note=calibration_note,
                    own_recent_decisions=own_recent_decisions,
                    trade_grade_summary=trade_grade_summary,
                    yesterday_insights=yesterday_insights,
                    recent_performance=recent_performance,
                    already_trimmed_today=already_trimmed_today,
                    allow_margin=bool(getattr(self.config.risk, "allow_margin", False)),
                    margin_headroom_usd=margin_headroom_usd,
                    margin_ladder_backed=margin_ladder_backed,
                    margin_ladder_multiple=margin_ladder_multiple,
                    margin_ladder_rung=margin_ladder_rung,
            )
            try:
                review, md_result = self.position_reviewer.review(**review_kwargs)
            except PaidAnalysisSuspended as exc:
                self._reconcile_fills()
                return self._paid_suspension_after_late_safety(
                    run_id, session=session_type, error=exc,
                    where=f"{session_type}-paid-reviewer",
                    orders=orders,
                    extra={"session": session_type, "positions": len(positions),
                           "stop_coverage_gaps": coverage_gaps,
                           # Spec §11.2 — gross exposure and its ceiling.
                           "leverage": dict(ctx.leverage)},
                )
            review_log_kwargs = agent_log_kwargs(md_result)
            if review is None:
                review_log_kwargs["status"] = "position_review_parse_error"
            self.db.insert_agent_log(
                agent_name="position_reviewer", run_id=run_id,
                input_summary=(
                    f"{session_type} | {len(review_positions)} positions, ${total_value:.0f} total"
                ),
                input_message=md_result.user_message,
                output_summary=review.overall_assessment if review else "parse_error",
                full_response=md_result.raw_text,
                model=md_result.model,
                tokens_used=md_result.tokens_used,
                input_tokens=md_result.input_tokens,
                output_tokens=md_result.output_tokens,
                cost_usd=md_result.cost_usd,
                **review_log_kwargs,
            )

            # Substantiation pass on the exit side (2026-09-18). Heals the
            # structured trigger from the prose, RE-ASKS once for anything
            # still unsubstantiated, and records a durable reason for what
            # survives both. See `_substantiate_exit_triggers`.
            review = self._substantiate_exit_triggers(
                review, ctx=ctx, run_id=run_id, review_kwargs=review_kwargs,
            )

            # Risk check: if the daily loss limit is breached, HALT — refuse
            # further risk and verify the stops. Else dispatch the LLM's
            # per-position action list. (docs/WORK.md item 32: this used to
            # force-sell the whole book.)
            #
            # Refresh FIRST, then measure: the locals here date from BEFORE
            # the LLM review (minutes of crash tape ago), and the loss
            # numerator is now read off the held positions themselves, so a
            # stale position list would be a stale measurement. Falls back to
            # the pre-review snapshot if the refresh fails — a breach must
            # still be judged, on the best truth available.
            fresh_account = ctx.account
            try:
                fresh_account = self.broker.get_account() or ctx.account
                fresh_positions = self.broker.get_positions()
                if fresh_positions:
                    positions = fresh_positions
            except Exception as e:  # noqa: BLE001
                logger.warning("post-review breach check: refresh failed "
                               "(using pre-review snapshot): %s", e)
            loss_violation, _bl, _pnl, loss_basis = self._daily_loss_breach(
                fresh_account, positions,
            )
            if loss_violation:
                # Review fix, still load-bearing: this branch previously FELL
                # THROUGH to the park bookend — the system would buy SGOV with
                # ~all equity on a breach day and the next intra tick would
                # act on the fresh SGOV lot. Mirror the pre-review breaker:
                # return, never park.
                halt = self._halt_on_daily_loss_breach(
                    positions, loss_violation, run_id,
                    where=f"{session_type} post-review", basis=loss_basis,
                    ctx=ctx,
                )
                halt["session"] = session_type
                halt["review"] = review.model_dump() if review else None
                # Best-available truth at halt time: the just-refreshed
                # account snapshot above, not the pre-review one this
                # function otherwise carries as `total_value`/`daily_pnl`.
                fresh_total_value = (
                    fresh_account.get("portfolio_value", total_value)
                    if isinstance(fresh_account, dict) else total_value
                )
                fresh_last_equity = (
                    fresh_account.get("last_equity", fresh_total_value)
                    if isinstance(fresh_account, dict) else last_equity
                )
                halt["daily_pnl"] = (
                    (fresh_total_value - fresh_last_equity) if fresh_last_equity else 0.0
                )
                halt["daily_return_pct"] = (
                    (halt["daily_pnl"] / fresh_last_equity * 100) if fresh_last_equity else 0.0
                )
                (halt["total_pnl"], halt["total_return_pct"],
                 halt["total_pnl_since"]) = self._total_pnl_since_reset(fresh_total_value)
                # The session's own earlier orders (deterministic trails and
                # the like) are preserved for the feed. The halt itself
                # placed none — `halted` / `halt_reason` are the record of
                # that, and the invariant is tested.
                halt["orders"] = list(orders)
                # Spec §11.2 — gross exposure and its ceiling.
                halt["leverage"] = dict(ctx.leverage)
                return halt
            else:
                # Phase 3.7 — deterministic trailing FIRST, before the LLM's
                # discretionary TRAIL_STOP is considered. Arithmetic does not
                # need a language model's permission, and a winner's stop
                # should not depend on one remembering to propose a move.
                orders.extend(
                    self._apply_deterministic_trails(review_positions, run_id=run_id)
                )

                # Phase 3.4 — AGENTS.md puts AI Risk in the chain for exits
                # as well as entries. Until this landed the entire sell side
                # skipped the veto layer the buy side has always had.
                risk_vetoed, _exit_verdict = self._risk_review_exits(
                    review, review_positions, run_id=run_id,
                    total_value=total_value, macro_summary=macro_summary,
                    position_facts=position_facts,
                    # This loop fetched all of these before the position
                    # reviewer ran; until 2026-09-13 none of them reached the
                    # AI Risk seat, which was then asked to audit the exits
                    # against news and drawdown state it had never been shown.
                    news_intel=session_news,
                    earnings_analyses=session_earnings,
                    cash=review_cash,
                    reserve_balance=reserve_balance,
                    recent_performance=recent_performance,
                )
                orders.extend(self._midday_execute_llm_actions(
                    review_positions, review, run_id,
                    already_trimmed_today=already_trimmed_today,
                    metric_deltas=metric_deltas,
                    risk_vetoed_symbols=risk_vetoed,
                    position_facts=position_facts,
                ))

                # Take-profit revision flags, adjudicated LAST — after every
                # exit decision this session makes. A re-derived target
                # therefore cannot reach this session's exits even in
                # principle; and because progress/pace are measured against
                # the PINNED entry target, it cannot reach a later session's
                # exit-guard veto either. Places no orders: nothing here
                # exits anything, and the trailing stop remains the only
                # automatic exit (PR #321).
                try:
                    target_revisions = self._adjudicate_target_revision_flags(
                        review, review_positions, run_id=run_id,
                        seat="position_reviewer",
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.error(
                        "target revision sweep failed (non-fatal, no target "
                        "was changed): %s", exc,
                    )
                    target_revisions = []

            # Snapshot AFTER the review so the next session compares against
            # what this one actually saw. Written even when the review failed:
            # the metrics are deterministic and their continuity is the point.
            self._persist_review_metrics(position_facts, run_id=run_id)

        logger.info("%s: %d positions, risk=%s, %d orders",
                     session_type.capitalize(), len(positions),
                     review.risk_level if review else "no_positions",
                     len(orders))
        # Reconcile everything still marked submitted (today's new orders +
        # any lingering from morning that didn't reach terminal in time).
        self._reconcile_fills()

        # Bookend: park cash freed by this session's sells (and any still-idle
        # excess) — without this, midday/close SELL proceeds sit unswept until
        # tomorrow's morning bookend. park_excess refreshes account state and
        # subtracts open-BUY holds itself; emergency paths returned earlier and
        # deliberately skip parking.
        sweeper = self._sweeper()
        if sweeper is not None:
            try:
                sweep_order = sweeper.park_excess(ctx)
                if sweep_order:
                    orders.append(sweep_order)
            except Exception as e:  # noqa: BLE001
                logger.warning("cash sweep: park_excess failed (non-fatal): %s", e)

        # Session execution (reviewer exits, sweep) may have changed the
        # book since the start-of-session snapshot.
        self._sync_positions_from_broker()

        return {
            "status": (
                "reviewed" if not review_positions or review is not None
                else "position_review_parse_error"
            ),
            "session": session_type,
            "positions": len(positions),
            "review": review.model_dump() if review else None,
            "orders": orders,
            "run_id": run_id,
            "stop_coverage_gaps": coverage_gaps,
            # Every take-profit revision flag this session adjudicated, with
            # its basis code — read by the cockpit, which draws the target.
            "target_revisions": target_revisions,
            # Spec §11.2 — gross exposure, its ladder-resolved ceiling and the
            # distance to forced liquidation, for the operator alert.
            "leverage": dict(ctx.leverage),
            # Telegram P&L line ("Session P&L" rename): today's account
            # change (broker last_equity basis, same as run_intra_check/
            # run_evening) plus total since the last recorded baseline.
            "daily_pnl": daily_pnl,
            "daily_return_pct": daily_return_pct,
            "total_pnl": total_pnl,
            "total_return_pct": total_return_pct,
            "total_pnl_since": total_pnl_since,
        }

    def run_earnings_preprocess(self) -> dict:
        """Pre-market earnings analysis — the ONLY place that calls the LLM
        for 10-Q/10-K filings.

        Scheduled at 08:00-09:15 ET via launchd. Synchronously fetches any
        new filings, runs the earnings analyst on each, saves the analysis,
        and confirms the filing so later sessions see it as cached.

        Hot sessions (morning/midday/evening) use `_load_earnings_analyses`
        which is read-only. That separation guarantees no session burns
        tokens on fresh LLM work — a filing that drops after preprocess
        surfaces as a `queued=True` placeholder and PM sizes down.
        """
        ctx = RunContext.start("earnings_preprocess")
        run_id = ctx.run_id
        logger.info("=== Earnings preprocessing: %s ===", run_id)

        if not self._is_trading_day():
            logger.info("Earnings preprocess skipped: market closed for non-trading day")
            return {"status": "market_holiday", "run_id": run_id}

        self._activate_cost_session(run_id, "earnings_preprocess")

        # Drain orphaned protection-restore intents from any prior session
        # that died mid-finalize. earnings_preprocess (08:00-09:15 ET) is the
        # first session of the trading day, so if an overnight evening run
        # left state in `pending_protection_restores`, this is the earliest
        # opportunity to recover before the 09:30 ET open. Without this call
        # an unprotected position would ride the open-gap with no stop —
        # matches the drain pattern used in run_morning / run_position_review
        # / run_intra_check / run_evening.
        self._drain_pending_protection_restores()
        self._drain_pending_repegs()
        self._reconcile_orphan_pending_submits()  # audit F4

        # Refresh the credentialless SEC Form 4 cache before any paid-analysis
        # gate. This deterministic source work remains available while the
        # cost circuit is latched and lets the morning session consume a
        # bounded local cache instead of crawling EDGAR on the trading path.
        smart_money_refresh: dict = {"status": "disabled"}
        if self.config.smart_money.enabled:
            try:
                # Watched names are passed so the Form 4 discovery budget
                # (`max_filings_per_refresh`) is spent on the desk's own
                # names before the rest of the listed market. Nothing is
                # filtered out — external candidate nomination still reads
                # filings on names the desk does not watch.
                watched = self._watched_research_symbols()
                try:
                    smart_money_refresh = self.smart_money_provider.refresh(watched)
                except TypeError:
                    smart_money_refresh = self.smart_money_provider.refresh()
                logger.info(
                    "Smart-money refresh (%s): %s",
                    _smart_money_refresh_sources_word(self.config.smart_money.congress_enabled),
                    smart_money_refresh,
                )
                # Backlog depth and watched-name coverage, named in their own
                # line: `refresh` runs once a day pre-market, so a residue
                # cannot drain until tomorrow.
                logger.info(
                    "Smart-money Form 4 backlog: pending=%s watched_pending=%s "
                    "cap_reached=%s watched_read_through=%s/%s "
                    "drain_deadline_hit=%s edgar_coverage=%s",
                    smart_money_refresh.get("pending_filings"),
                    smart_money_refresh.get("watched_pending_filings"),
                    smart_money_refresh.get("discovery_cap_reached"),
                    smart_money_refresh.get("watched_names_read_through"),
                    smart_money_refresh.get("watched_names"),
                    smart_money_refresh.get("watched_drain_deadline_hit"),
                    smart_money_refresh.get("edgar_coverage"),
                )
                # ...and RECORDED where the desk records its status. Until
                # 2026-09-19 these counts existed only in a log line and the
                # job's stdout, so no one could ask the database whether the
                # backlog was draining from one morning to the next.
                self._record_form4_backlog(run_id, smart_money_refresh)
                self._record_congressional_refresh(run_id, smart_money_refresh)
                self._alert_form4_backlog_before_open(smart_money_refresh)
            except Exception as exc:
                logger.warning("SEC Form 4 refresh failed softly: %s", exc)
                smart_money_refresh = {
                    "status": "provider_error",
                    "error": type(exc).__name__,
                }

        try:
            reports = self.earnings_provider.check_and_fetch(
                self._earnings_preprocess_symbols(),
            )
        except Exception as e:
            logger.error("Earnings preprocess: fetch failed: %s", e)
            return {
                "status": "fetch_error", "run_id": run_id, "error": str(e),
                "smart_money_refresh": smart_money_refresh,
            }

        new_reports = [r for r in reports if r.is_new]
        if not new_reports:
            logger.info("Earnings preprocess: no new filings, nothing to analyze.")
            return {
                "status": "nothing_new", "run_id": run_id, "count": 0,
                "smart_money_refresh": smart_money_refresh,
            }

        logger.info(
            "Earnings preprocess: analyzing %d new filings: %s",
            len(new_reports),
            ", ".join(r.symbol for r in new_reports),
        )
        # Owner-facing record of WHICH filings this pass handled (2026-09-18:
        # the message used to say "analyzed: 1 confirmed: 1 failed: 0" and
        # the owner asked "which one? what's the symbol? what's the
        # company?"). Report-only — nothing reads this back into a decision.
        filings_waiting = [
            {"symbol": r.symbol, "form_type": r.form_type,
             "filing_date": r.filing_date, "outcome": "waiting"}
            for r in new_reports
        ]
        try:
            self._require_paid_analysis("earnings_analyst")
            results = self.earnings_analyst.analyze_reports(new_reports)
        except PaidAnalysisSuspended as exc:
            # No filing failure is recorded: the filing remains new and will
            # be eligible after an operator resets the circuit.
            payload = self._paid_suspended_payload(run_id, error=exc)
            payload["smart_money_refresh"] = smart_money_refresh
            return payload
        except Exception as e:
            logger.error("Earnings preprocess: LLM analysis failed: %s", e, exc_info=True)
            # Record failures so the retry bounds kick in for each filing.
            for r in new_reports:
                try:
                    self.earnings_provider.record_failure(r)
                except Exception as re:
                    logger.error("record_failure failed for %s: %s", r.symbol, re)
            return {
                "status": "analysis_error", "run_id": run_id, "error": str(e),
                "filings": filings_waiting,
            }

        # Match results to reports by (symbol, form_type, filing_date), not
        # just symbol. Same-symbol multiple-form-day is rare but real
        # (10-Q + 10-K can land the same fiscal-year-end day). Symbol-only
        # matching meant a successful 10-K silently flagged a failed 10-Q
        # as confirmed and never consumed its retry budget — the failed
        # filing would then be re-queued every preprocess run forever.
        def _filing_key(symbol: str, form_type: str | None, filing_date: str | None):
            return (symbol, form_type, filing_date)

        successful_keys = {
            _filing_key(res["symbol"], res.get("form_type"), res.get("filing_date"))
            for res in results
            if res.get("is_new")
        }
        failed_reports = [
            r for r in new_reports
            if _filing_key(r.symbol, r.form_type, r.filing_date) not in successful_keys
        ]
        for report in failed_reports:
            try:
                self.earnings_provider.record_failure(report)
            except Exception as re:
                logger.error("record_failure failed for %s: %s", report.symbol, re)

        # Log each LLM call (parity with the inline bg-thread path).
        analyzed_count = 0
        for res in results:
            agent_result = res.get("agent_result")
            if agent_result is None:
                continue
            sym = res.get("symbol", "?")
            analysis = res.get("analysis") or {}
            sentiment = (analysis.get("investment_implications") or {}).get("sentiment", "?")
            try:
                self.db.insert_agent_log(
                    agent_name="earnings_analyst_preprocess",
                    run_id=run_id,
                    input_summary=f"{sym} {res.get('form_type','?')} filed {res.get('filing_date','?')}",
                    input_message=agent_result.user_message,
                    output_summary=(
                        f"sentiment={sentiment}" if res.get("analysis") else "parse_error"
                    ),
                    full_response=agent_result.raw_text,
                    model=agent_result.model,
                    tokens_used=agent_result.tokens_used,
                    input_tokens=agent_result.input_tokens,
                    output_tokens=agent_result.output_tokens,
                    cost_usd=agent_result.cost_usd,
                    **agent_log_kwargs(agent_result),
                )
            except Exception as e:
                logger.error("Earnings preprocess: log insert failed for %s: %s", sym, e)
            analyzed_count += 1

        # Confirm filings. Do this AFTER logging so a crash between the two
        # leaves the filing still "new" for the next preprocess run.
        # Match by (symbol, form_type, filing_date) to avoid confirming a
        # failed 10-Q on the back of a successful same-day 10-K.
        confirmed = 0
        for r in new_reports:
            if _filing_key(r.symbol, r.form_type, r.filing_date) in successful_keys:
                try:
                    self.earnings_provider.confirm_filing(r)
                    confirmed += 1
                except Exception as e:
                    logger.warning("confirm_filing failed for %s: %s", r.symbol, e)

        logger.info(
            "Earnings preprocess complete: %d analyzed, %d confirmed, %d failed",
            analyzed_count, confirmed, len(failed_reports),
        )
        # Per-filing outcome for the owner message: the reader's own
        # sentiment / conviction / key_thesis where it produced one, and
        # "failed" where it did not. Same (symbol, form, date) key as the
        # confirmation logic above, so a same-day 10-Q and 10-K stay apart.
        verdict_by_key: dict = {}
        for res in results:
            analysis = res.get("analysis") or {}
            impl = analysis.get("investment_implications") or {}
            if not isinstance(impl, dict):
                impl = {}
            verdict_by_key[_filing_key(
                res.get("symbol"), res.get("form_type"), res.get("filing_date"),
            )] = {
                "sentiment": impl.get("sentiment"),
                "conviction": impl.get("conviction"),
                "key_thesis": impl.get("key_thesis"),
            }
        filings: list[dict] = []
        for r in new_reports:
            key = _filing_key(r.symbol, r.form_type, r.filing_date)
            row = {
                "symbol": r.symbol, "form_type": r.form_type,
                "filing_date": r.filing_date,
                "outcome": "analyzed" if key in successful_keys else "failed",
            }
            row.update(verdict_by_key.get(key) or {})
            filings.append(row)
        return {
            "status": "preprocessed",
            "run_id": run_id,
            "analyzed": analyzed_count,
            "confirmed": confirmed,
            "failed": len(failed_reports),
            "filings": filings,
            "smart_money_refresh": smart_money_refresh,
        }

    def run_intra_check(self) -> dict:
        """Intra-session circuit-breaker check, plus the durable record of
        its own output.

        Same gap as `run_morning`/`run_position_review` (2026-09-18 sweep):
        `stop_coverage_gaps` is computed from live broker state every tick
        and handed to the notifier with no other durable home. This wrapper
        persists every return path, keyed by run_id (not date — this fires
        roughly every 30 minutes, so a date-keyed row would keep only the
        last tick; see `Database.save_intra_check_report`). Fail-soft.
        """
        self._last_evidence_freshness = None
        self._intra_preamble_deferred = ""
        result = self._run_intra_check_body()
        if isinstance(result, dict) and self._intra_preamble_deferred:
            result["preamble_deferred"] = self._intra_preamble_deferred
        self._attach_evidence_freshness(result)
        self._persist_intra_check_report(result)
        return result

    def _persist_intra_check_report(self, result: dict) -> None:
        if not isinstance(result, dict):
            return
        run_id = result.get("run_id")
        if not run_id:
            return
        try:
            self.db.save_intra_check_report(
                run_id=run_id, date=session_date_key(), payload=result,
            )
        except Exception as exc:  # noqa: BLE001 — never break the push
            logger.warning(
                "intra_check report persistence failed (non-fatal): %s", exc,
            )

    def _run_intra_check_body(self) -> dict:
        """Lightweight intra-session circuit-breaker check (no LLM calls).

        Scheduled between morning and midday (typically 12:00 ET) to catch a
        flash crash that would otherwise accumulate unchecked through the
        busiest trading hour. Only one rule: daily P&L vs loss limit. If
        breached, HALT the desk — see `_halt_on_daily_loss_breach`. It
        reconciles fills, cancels resting entry orders, verifies stop
        coverage per held position AT THE BROKER, files a durable per-symbol
        refusal reason and alerts the owner. It closes, resizes and zeroes
        nothing: the whole-book liquidation this used to describe was
        deleted on 2026-09-14 (item 32). Runs in ~5 seconds; OK for a
        30-minute cadence if the user wants even tighter coverage.
        """
        ctx = RunContext.start("intra_check")
        run_id = ctx.run_id
        logger.info("=== Intra-session risk check: %s ===", run_id)

        if not self._is_trading_day():
            logger.info("Intra check skipped: market closed for non-trading day")
            return {"status": "market_holiday", "run_id": run_id}

        halt = self._kill_switch_halt_result(run_id)
        if halt is not None:
            return halt

        self._activate_cost_session(run_id, "intra_check")

        # Board item 127 (2026-09-19). Every write below reaches the broker,
        # and `intra_check` is exempt from the wrapper's session lock (item
        # 128), so this whole preamble used to run with no lock at all. It
        # now runs only while this process holds the same advisory flock the
        # paid scan below takes (`_intraday_scan_process_lock`) — which the
        # standalone coverage sweep's repair pass also takes — and only while
        # no morning/midday/close session owns the desk
        # (`_blocking_owner_session`, the check the paid scan already uses).
        # A live session runs this same preamble itself near the start of its
        # own run, and it may be in the middle of cancelling stops to sell; a
        # stop added here in that window is the worst pairing item 127 names.
        # Deferring skips only this tick's preamble: the loss check below
        # still runs every tick, and the next tick re-reads the broker.
        coverage_gaps: list[dict] = []
        preamble_deferred = ""
        with self._intraday_scan_process_lock() as preamble_lock:
            if not preamble_lock:
                preamble_deferred = (
                    "another desk process holds the broker-write lock"
                )
            else:
                blocking = self._blocking_owner_session()
                if blocking == "unreadable":
                    preamble_deferred = (
                        "the active-session owner file could not be read "
                        "(fail closed)"
                    )
                elif blocking is not None:
                    preamble_deferred = (
                        f"a live {blocking} session owns the desk and runs "
                        "this same reconcile itself"
                    )
            if preamble_deferred:
                logger.warning(
                    "Intra check: broker-writing preamble DEFERRED this tick — "
                    "%s. No drain, repair, release or reconcile ran; the loss "
                    "check still runs.", preamble_deferred,
                )
            else:
                # Drain orphaned protection-restore intents — intra runs every
                # 30 min so this is the most frequent recovery opportunity for
                # bails that landed during morning. Codex r8 #2.
                self._drain_pending_protection_restores()
                self._drain_pending_repegs()
                # Broker-truth coverage audit + auto-repair every tick (audit round
                # 2): an entry that fills after place_entry_protection's wait, or a
                # repair that failed once, otherwise stayed naked until the NEXT
                # session — hours. On the intra cadence the naked window is ≤30 min.
                # Read-only when coverage is fine; ~1 broker call per held long.
                # Spec §11.1 guard 3: the return value used to be DISCARDED here, so
                # the 30-minute sweep — the tightest cadence this audit runs on, and
                # the one the fractional decision leans on — was the one caller whose
                # findings never reached the operator's feed at all. Carried into the
                # result dict now, exactly as every other session already does.
                try:
                    coverage_gaps = self._reconcile_stop_coverage()
                except Exception as exc:  # noqa: BLE001
                    logger.warning("intra coverage reconcile failed (non-fatal): %s", exc)
                # Sweep retired (owner mandate 2026-09-17): release any held vehicle.
                self._release_retired_cash_park(run_id)
                self._reconcile_orphan_pending_submits()  # audit F4
                # Broker-truth EXIT audit (2026-08-28 ONDS/CCJ). intra_check fires
                # every ~30 min, so this is the tightest window this reconciler
                # runs on — a stop that fires mid-session is written back within
                # one tick instead of sitting unrecorded until the next scheduled
                # session hours later.
                try:
                    self._reconcile_stop_out_fills(run_id)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("intra stop-out reconcile failed (non-fatal): %s", exc)

                # 2026-09-17 AMD incident: AMD filled at $549.11 but the trades
                # table still read 'submitted' half an hour later. The stop-coverage
                # and stop-out reconcilers just above only watch protective/broker-
                # initiated exits — neither one asks the broker about the fate of an
                # order THIS pipeline submitted (a BUY/SELL/REDUCE/etc still marked
                # 'submitted' in the trades table). `run_morning` and the midday/
                # close review both call `_reconcile_fills` for exactly that reason;
                # this tick — the one that runs every ~30 minutes and is therefore
                # the tightest window available to close that gap between sessions
                # — never did. The live fill-notification websocket never
                # authenticates on this host (placeholder credential, frozen pending
                # an owner decision — see broker.py), so in production this always
                # resolves through `_reconcile_fills`'s own bounded REST lookup
                # (`broker.get_order_fill_info`), never the socket. Unscoped
                # (no run_id) so a still-'submitted' row from ANY earlier session
                # today is picked up, not just ones this tick itself created.
                try:
                    self._reconcile_fills()
                except Exception as exc:  # noqa: BLE001
                    logger.warning("intra fill reconcile failed (non-fatal): %s", exc)

        self._intra_preamble_deferred = preamble_deferred

        try:
            account = self.broker.get_account()
            positions = self.broker.get_positions()
        except Exception as e:
            logger.error("Intra check: broker query failed: %s", e)
            return {"status": "broker_error", "run_id": run_id, "error": str(e),
                    "stop_coverage_gaps": coverage_gaps}

        total_value = account["portfolio_value"]
        last_equity = account.get("last_equity", total_value)
        daily_pnl = total_value - last_equity
        ctx.account = account
        ctx.positions = positions
        ctx.cash = account["cash"]
        ctx.deployable_cash = self._compute_deployable_cash(ctx.cash, ctx.positions)
        ctx.total_value = total_value
        ctx.last_equity = last_equity
        ctx.daily_pnl = daily_pnl
        self._sync_positions_from_broker(positions)
        daily_return_pct = (daily_pnl / last_equity * 100) if last_equity > 0 else 0
        total_pnl, total_return_pct, total_pnl_since = (
            self._total_pnl_since_reset(total_value)
        )
        logger.info(
            "Intra snapshot: equity=$%.2f, last_close=$%.2f, pnl=$%.2f (%.2f%%), positions=%d",
            total_value, last_equity, daily_pnl, daily_return_pct, len(positions),
        )

        # `daily_pnl` above stays the ACCOUNT's day change — that is what the
        # snapshot log, the dashboard and this payload report, and it is not
        # changing. The BREACH TEST reads the held book instead, so that it
        # is measured against the same object its threshold is built from
        # (docs/WORK.md item 32, `_daily_loss_breach`).
        loss_violation, _bl, _pnl, loss_basis = self._daily_loss_breach(
            account, positions,
        )
        if not loss_violation or not positions:
            result = {
                "status": "ok",
                "daily_pnl": daily_pnl,
                "daily_return_pct": daily_return_pct,
                "total_pnl": total_pnl,
                "total_return_pct": total_return_pct,
                "total_pnl_since": total_pnl_since,
                "positions": len(positions),
                "run_id": run_id,
                "stop_coverage_gaps": coverage_gaps,
            }
            # 2026-08-19 intraday opportunity-discovery fix: bounded new-
            # opportunity scan, gated additionally on `not loss_violation`
            # (belt-and-suspenders) — a daily-loss breach must never add
            # new risk, whether or not there happened to be a position to
            # force-close in the branch above.
            if not loss_violation:
                try:
                    scan_result = self._run_intraday_opportunity_scan(ctx)
                except PaidAnalysisSuspended as exc:
                    scan_result = {
                        "status": "paid_analysis_suspended",
                        "run_id": run_id,
                        "error": str(exc),
                        "suspended": "intraday opportunity discovery only",
                        "preserved": "intraday deterministic loss protection",
                    }
                except Exception as e:  # noqa: BLE001 — never let the scan
                    # turn a routine intra_check tick into a failed run.
                    # Operator-honesty fix: a crash used to set scan_result to
                    # None, which is exactly what a healthy "ran, nothing to
                    # do" tick also produces — no `intraday_scan` key, session
                    # status stays "ok". The Telegram feed and the rehearsal
                    # rig were both blind to the difference. Attaching a
                    # dict (mirroring the `paid_analysis_suspended` shape
                    # above) makes the crash visible through the same nested
                    # path, while the tick itself still completes normally —
                    # the deterministic loss check above already ran and is
                    # unaffected by anything below it.
                    logger.error("Intraday opportunity scan crashed (non-fatal): %s", e)
                    scan_result = {
                        "status": "intraday_scan_crashed",
                        "run_id": run_id,
                        "error": str(e),
                        "error_type": type(e).__name__,
                        "preserved": "intraday deterministic loss protection",
                    }
                if scan_result is not None:
                    result["intraday_scan"] = scan_result
                    if scan_result.get("status") == "intraday_executed":
                        # Scan went through ExecutionStage; refresh the
                        # local table from broker truth (the start-of-tick
                        # snapshot above is now stale).
                        self._sync_positions_from_broker()
            return result

        # docs/WORK.md item 32 (2026-09-14): this was a near-verbatim copy of
        # `_midday_emergency_liquidate` (retired-ok) — the same 1%-through-the-market LIMIT
        # orders, the same `_finalize_pending_protections` restore on no-fill.
        # Both are gone. The intra breaker now HALTS: it cancels resting
        # entries, verifies that every held position really is stop-covered
        # at the broker, files a per-symbol refusal, and alerts. It sells
        # nothing. See `_halt_on_daily_loss_breach`.
        halt = self._halt_on_daily_loss_breach(
            positions, loss_violation, run_id,
            where="intra_check", basis=loss_basis, ctx=ctx,
        )
        halt["daily_pnl"] = daily_pnl
        halt["daily_return_pct"] = daily_return_pct
        halt["total_pnl"] = total_pnl
        halt["total_return_pct"] = total_return_pct
        halt["total_pnl_since"] = total_pnl_since
        return halt

    def _recently_intraday_evaluated(self, symbol: str, cooldown_hours: float) -> bool:
        """True when the explicit evaluation ledger says this symbol ran.

        Trades are not an evaluation ledger: PM parse failures, RM rejects,
        no-target decisions, and pre-execution errors create no trade row and
        previously bypassed cooldown, repeatedly buying the same analysis.
        """
        try:
            rows = self.db.get_recent_intraday_evaluations(
                symbol, cooldown_hours=cooldown_hours,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "Intraday cooldown ledger failed for %s (%s) — skipping scan "
                "for this symbol fail-closed", symbol, e,
            )
            return True
        if isinstance(rows, list):
            return bool(rows)

        # Compatibility for lightweight test doubles and rolling upgrades in
        # which an older DB facade has not exposed the new ledger method yet.
        # Production Database always returns a real list above.
        try:
            legacy_rows = self.db.get_trades(symbol=symbol, limit=10)
        except Exception:
            return True
        from datetime import datetime as _dt, timedelta, timezone
        cutoff = _dt.now(timezone.utc) - timedelta(hours=cooldown_hours)
        for row in legacy_rows if isinstance(legacy_rows, list) else []:
            if not str(row.get("run_id") or "").startswith("intra_check-"):
                continue
            try:
                ts = str(row.get("timestamp") or "")
                when = (_dt.fromisoformat(ts.replace("Z", "+00:00")) if "T" in ts
                        else _dt.strptime(ts, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc))
                if when.tzinfo is None:
                    when = when.replace(tzinfo=timezone.utc)
                if when >= cutoff:
                    return True
            except (TypeError, ValueError):
                continue
        return False

    def _blocking_owner_session(self) -> str | None:
        """Live wrapper owner mode that must not overlap paid discovery.

        Returns the other session's mode when it is alive, ``"unreadable"``
        when the owner file exists but cannot be trusted (fail closed), or
        None when paid discovery may run. ``intra_check`` never blocks
        itself. A dead pid or a vanished file is None — morning that
        already finished must not sleep the 09:30 scan until 10:00.
        """
        import os
        import time as _time

        owner_path = Path.home() / ".cache" / "quant-agent" / "active-session.lock" / "owner"
        if not owner_path.exists():
            return None
        try:
            parts = owner_path.read_text().strip().split()
            owner_mode = parts[0]
            owner_ts = int(parts[2])
            owner_pid = int(parts[3])
            age = _time.time() - owner_ts
            alive = True
            try:
                os.kill(owner_pid, 0)
            except OSError:
                alive = False
            if owner_mode == "intra_check":
                return None
            if alive and 0 <= age <= 1800:
                return owner_mode
            return None
        except (OSError, ValueError, IndexError):
            return "unreadable"

    def _intra_window_remaining_s(self) -> float:
        """Seconds left in the intra_check ET window. Calendar-bound, not invented."""
        from src.trading_calendar import SESSION_WINDOWS, _minute_of_day, et_now
        _start, end = SESSION_WINDOWS["intra_check"]
        now = et_now()
        remaining_min = end - _minute_of_day(now)
        return max(0.0, remaining_min * 60.0)

    def _await_paid_scan_slot(self, run_id: str) -> bool:
        """Wait for morning/midday/close to finish rather than skip the tick.

        Returns True when paid discovery must still be skipped (lock still
        held at window end, or owner file unreadable). Returns False when
        the slot is free.
        """
        import time as _time

        first = True
        self._paid_scan_waited = False
        while True:
            blocking = self._blocking_owner_session()
            if blocking is None:
                if not first:
                    self._paid_scan_waited = True
                    logger.info(
                        "Intraday scan: other session released the owner lock; "
                        "running paid discovery on this tick instead of "
                        "waiting for the next 30-minute fire",
                    )
                return False
            if blocking == "unreadable":
                logger.warning(
                    "Intraday scan: could not validate active-session owner — "
                    "skipping paid discovery fail-closed",
                )
                return True
            remaining = self._intra_window_remaining_s()
            if remaining <= 0:
                logger.info(
                    "Intraday scan: %s still holds the owner lock at window "
                    "end; paid discovery cannot run this tick", blocking,
                )
                return True
            if first:
                logger.info(
                    "Intraday scan: wrapper reports active %s session; waiting "
                    "for it to finish instead of skipping this tick", blocking,
                )
                first = False
            _time.sleep(min(1.0, remaining))

    def _another_session_recently_active(self, run_id: str,
                                         within_minutes: float = 15.0) -> bool:
        """True when a DIFFERENT session currently owns the trading process.

        The 15-minute trade-row heuristic slept the 09:30 and 13:00 scans
        after morning/midday had already written fills — the owner lock is
        the in-flight signal. `within_minutes` is kept for callers but no
        longer gates a finished session.
        """
        blocking = self._blocking_owner_session()
        if blocking == "unreadable":
            return True
        return blocking is not None

    @contextlib.contextmanager
    def _intraday_scan_process_lock(self):
        """Non-blocking process-level mutex for the intraday scan.

        Yields True when this process holds the lock, False otherwise.

        Why (independent review finding, 2026-08-19): the owner-lock
        `_another_session_recently_active` guard sees a concurrent
        morning/midday/close only while that wrapper still owns the
        process. Two `intra_check` processes launched at nearly the same
        instant would both pass it — and could then size BUYs against the
        same pre-fill snapshot, breaching `max_position_pct`.

        In practice `scripts/run_if_et_window.sh` makes that impossible:
        ticks are 1800s apart and the wrapper hard-kills a run at
        `timeout --kill-after=30 1200` (~1230s), so a tick is always dead
        before the next fires. But that guarantee lives in a deployment
        config this code cannot read (the production systemd units are not
        in-repo), and it would silently disappear if the interval were ever
        shortened. A trading safety property should not depend on an
        unverifiable assumption, so this closes the class outright.

        Deliberately NOT a new service/daemon/timer — a plain advisory
        `flock` on a local file, the same idea as the wrapper's existing
        `mkdir`-based session lock. Since 2026-09-19 (board item 127) it
        also guards `intra_check`'s broker-writing preamble, and the
        standalone coverage sweep's repair pass takes the same file
        (`src.coverage_watchdog.repair_lock`). Loss protection keeps its
        exemption and never touches this. The lock is released on process exit even if we
        are SIGKILLed, so a killed run cannot wedge it.
        """
        import fcntl

        fh = None
        acquired = False
        try:
            lock_path = Path(self.config.storage.db_path).parent / ".intraday_scan.lock"
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            fh = open(lock_path, "w")
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except BlockingIOError:
                logger.info(
                    "Intraday scan: another process already holds the scan "
                    "lock — skipping this tick (no concurrent position sizing)",
                )
        except Exception as e:  # noqa: BLE001 — unknowable lock state must not scan
            logger.warning(
                "Intraday scan: could not establish the process lock (%s) — "
                "skipping this tick (fail-closed)", e,
            )
        try:
            # Keep the yield outside the acquisition exception handler.  An
            # exception raised by the protected scan body is injected here by
            # contextlib and must propagate to run_intra_check (not be mistaken
            # for a lock failure and replaced by "generator didn't stop after
            # throw()").
            yield acquired
        finally:
            if fh is not None:
                try:
                    fh.close()   # releases the flock
                except Exception:  # noqa: BLE001
                    pass

    def _track_intraday_snapshot_ok(self, symbol: str) -> None:
        """Reset a symbol's consecutive-miss streak. Never raises — a
        monitoring bug must not be able to break the scan it watches."""
        try:
            self.db.record_intraday_symbol_snapshot_result(symbol, ok=True)
        except Exception:
            logger.warning(
                "intraday snapshot health: failed to record OK for %s", symbol,
                exc_info=True,
            )

    def _track_intraday_snapshot_miss(self, symbol: str) -> None:
        """Record a missed snapshot for `symbol` and alert the owner once
        it has failed 3 consecutive ticks (~90 min) — see
        `Database.record_intraday_symbol_snapshot_result`'s docstring for
        the threshold/cooldown reasoning. Never raises."""
        try:
            result = self.db.record_intraday_symbol_snapshot_result(symbol, ok=False)
        except Exception:
            logger.warning(
                "intraday snapshot health: failed to record miss for %s", symbol,
                exc_info=True,
            )
            return
        if not result.get("should_alert"):
            return
        try:
            from src import notifier as _notifier

            misses = result.get("consecutive_misses", 0)
            _notifier.send_owner_alert(
                "INTRADAY SNAPSHOT UNAVAILABLE\n"
                f"{symbol} has failed to return snapshot data for "
                f"{misses} consecutive scans (~{misses * 30} min). It is being "
                "silently excluded from intraday move detection until this "
                "resolves — check whether the ticker is still valid/tradable "
                "on Alpaca. Will not re-alert on this symbol for 24h."
            )
        except Exception:
            logger.warning(
                "intraday snapshot health: alert failed for %s", symbol,
                exc_info=True,
            )

    def _run_intraday_opportunity_scan(self, ctx: RunContext) -> dict:
        """Concurrency-guarded wrapper around the scan body.

        2026-08-31 visibility fix: every path through this wrapper and the
        body it delegates to now returns an explicit result dict — never a
        bare None — so run_intra_check's `intraday_scan` key distinguishes
        the three everyday reasons a tick adds no new activity from EACH
        OTHER and from a crash. PR #163 (2026-08-30) made a crashed scan
        visible as "intraday_scan_crashed" but left these three still
        collapsed onto the identical absent-key shape:

          - "intraday_scan_disabled": the feature is off in config.
          - "intraday_scan_lock_contended": another scan already owns this
            window — either this process's own advisory flock (see
            `_intraday_scan_process_lock`) or a morning/midday/close
            wrapper that still holds the owner lock at the end of this
            tick's wait (`_await_paid_scan_slot`).
          - "intraday_scan_no_opportunity": the scan ran and found nothing
            worth escalating (see `_intraday_opportunity_scan_body`'s
            early-return points).

        All three are HEALTHY completions — see ops/rehearsal/report.py's
        STATUS_PLAIN entries and `_verdict`'s healthy set, which is where
        "intraday_scan_crashed" is deliberately NOT included.
        """
        cfg = getattr(self.config, "intraday_scan", None)
        if cfg is None or not getattr(cfg, "enabled", False):
            return {"status": "intraday_scan_disabled", "run_id": ctx.run_id}
        with self._intraday_scan_process_lock() as acquired:
            if not acquired:
                return {"status": "intraday_scan_lock_contended", "run_id": ctx.run_id}
            return self._intraday_opportunity_scan_body(ctx)

    def _macro_regime_or_print_changed(self, state: dict) -> bool:
        """True when a later snapshot actually changed the regime or a FRED print.

        Calendar age is not expiry — macro is reusable across days until a
        real regime/print change. A failed detector is not a change.
        Print change is a change in the actual series values/observation
        dates, not a change in the regime label string.
        """
        if not isinstance(state, dict):
            return False
        stored_regime = str(state.get("regime") or "").strip()
        if not stored_regime:
            return False
        try:
            if self._macro_history_regime_changed(state, stored_regime):
                return True
        except Exception:  # noqa: BLE001 — failed detector is not a change
            pass
        try:
            if self._macro_series_prints_changed(state):
                return True
        except Exception:  # noqa: BLE001
            pass
        return False

    def _macro_history_regime_changed(self, state: dict, stored_regime: str) -> bool:
        """True when a newer dated history snapshot carries a different regime."""
        store = getattr(self, "macro_store", None)
        load = getattr(store, "load_history", None)
        if not callable(load):
            return False
        history = load(days=2) or []
        if not isinstance(history, list) or not history:
            return False
        latest = history[-1]
        if not isinstance(latest, dict):
            return False
        latest_regime = str(latest.get("regime") or "").strip()
        latest_date = str(latest.get("date") or "").strip()[:10]
        stored_date = str(state.get("date") or state.get("as_of") or "").strip()[:10]
        if latest_date and stored_date and latest_date > stored_date and latest_regime and latest_regime != stored_regime:
            return True
        return False

    def _live_macro_series_prints(self) -> dict | None:
        """Fetch current FRED prints. None on a failed or missing provider.

        Restores ``last_coverage`` / ``_run_freshness`` afterwards —
        ``get_macro_summary`` mutates both, and this peek must not overwrite
        the morning side-channel a later reader still needs.
        """
        from src.data.macro_store import series_prints_from_summary
        provider = getattr(self, "macro", None)
        getter = getattr(provider, "get_macro_summary", None)
        if not callable(getter):
            return None
        had_coverage = hasattr(provider, "last_coverage")
        had_freshness = hasattr(provider, "_run_freshness")
        previous_coverage = getattr(provider, "last_coverage", None)
        previous_freshness = getattr(provider, "_run_freshness", None)
        summary = None
        freshness = None
        try:
            try:
                summary = getter()
                freshness = getattr(provider, "_run_freshness", None)
            except Exception:  # noqa: BLE001 — failed fetch ≠ print change
                return None
            if not isinstance(summary, dict) or not summary:
                return None
            return series_prints_from_summary(summary, freshness=freshness)
        finally:
            if had_coverage:
                provider.last_coverage = previous_coverage
            if had_freshness:
                provider._run_freshness = previous_freshness

    def _macro_series_prints_changed(self, state: dict) -> bool:
        """True when live FRED prints differ from the stored fingerprint.

        A snapshot that never recorded prints cannot claim a change —
        that would expire every pre-fingerprint last_state and invent
        churn. Failed live fetch is not a change.
        """
        from src.data.macro_store import series_prints_changed
        stored = state.get("series_prints")
        if not isinstance(stored, dict) or not (
            stored.get("values") or stored.get("observations")
        ):
            return False
        live = self._live_macro_series_prints()
        if not live:
            return False
        return series_prints_changed(stored, live)

    def _watched_research_symbols(self, ctx=None, report=None) -> list[str]:
        """Tickers this desk is actually watching. Empty if none are known.

        Form 4 / news peeks must not scan the whole listed market or treat
        an unrelated wire as a change to remembered research.
        """
        out: set[str] = set()
        trading = getattr(getattr(self, "config", None), "trading", None)
        for raw in getattr(trading, "universe", None) or []:
            text = str(raw or "").strip().upper()
            if text:
                out.add(text)
        stock_news = getattr(report, "stock_news", None) if report is not None else None
        if isinstance(report, dict):
            stock_news = report.get("stock_news")
        if isinstance(stock_news, dict):
            for raw in stock_news:
                text = str(raw or "").strip().upper()
                if text:
                    out.add(text)
        if ctx is not None:
            for finding in getattr(ctx, "smart_money_findings", None) or []:
                symbol = getattr(finding, "symbol", None)
                if symbol is None and isinstance(finding, dict):
                    symbol = finding.get("symbol")
                text = str(symbol or "").strip().upper()
                if text:
                    out.add(text)
        return sorted(out)

    def _peek_news_headlines(self, report) -> list[str]:
        """Live RSS titles for mechanical wire-expiry. Failed fetch → [].

        General wires only — do not pass the universe as a per-symbol
        fetch. That cap preserves caller order and morning's order is
        positions-first; an alphabetical universe peek would query a
        different 15 names and invent new titles.
        """
        from src.evidence_kind import headline_mentions_symbols
        self._last_news_peek_items = []
        provider = getattr(self, "news_provider", None)
        fetch = getattr(provider, "fetch_news", None)
        if not callable(fetch):
            return []
        watched = self._watched_research_symbols(report=report)
        if not watched:
            return []
        try:
            try:
                items, _coverage = fetch(symbols=None)
            except TypeError:
                items, _coverage = fetch()
        except Exception:  # noqa: BLE001 — failed fetch ≠ supersede
            return []
        # Keep what we just paid for. The expiry compare only needs titles,
        # but discarding the wire body meant the tick proved its remembered
        # news was superseded and then had nothing to re-ask with.
        self._last_news_peek_items = list(items or [])
        titles: list[str] = []
        for item in items or []:
            title = getattr(item, "title", None)
            summary = getattr(item, "summary", None)
            if isinstance(item, dict):
                if title is None:
                    title = item.get("title") or item.get("headline")
                if summary is None:
                    summary = item.get("summary")
            text = str(title or "").strip()
            if not text:
                continue
            search = f"{text} {summary or ''}"
            if not headline_mentions_symbols(search, watched):
                continue
            titles.append(text)
        return titles

    def _peeked_news_wire_text(self) -> str:
        """The wire text the expiry peek already fetched, formatted for the
        news analyst. Empty when the peek returned nothing.

        No second fetch and no new window: this is the same fetch that
        proved the remembered report superseded. A failed or empty peek
        stays empty so the seat expires and is lost — a fetch that got
        nothing must never be dressed up as fresh news.
        """
        items = list(getattr(self, "_last_news_peek_items", None) or [])
        if not items:
            return ""
        provider = getattr(self, "news_provider", None)
        fmt = getattr(provider, "format_for_prompt", None)
        if not callable(fmt):
            return ""
        try:
            text = fmt(items, max_items=self.config.news.max_prompt_items)
        except Exception as e:  # noqa: BLE001
            logger.warning("Intraday scan: wire text for news heal failed: %s", e)
            return ""
        return text if isinstance(text, str) and text.strip() else ""

    def _news_has_newer_material_wire(self, report) -> bool:
        """Best-effort mechanical headline compare. Failed fetch ≠ supersede.

        Only a new title that names a watched ticker is a wire change
        (the peek already drops unnamed general-wire titles). A sliding
        24h RSS window always grows unrelated headlines; treating those
        as expiry would freeze the midday tick, which cannot re-pay the
        news seat.
        """
        from src.evidence_kind import covered_news_headlines, newer_material_wire
        covered = set(covered_news_headlines(report))
        load_raw = getattr(getattr(self, "news_store", None), "load_raw_headlines", None)
        if callable(load_raw):
            try:
                for item in load_raw() or []:
                    if not isinstance(item, dict):
                        continue
                    text = str(item.get("title") or item.get("headline") or "").strip()
                    if text:
                        covered.add(text)
            except Exception:  # noqa: BLE001
                pass
        try:
            fetched = self._peek_news_headlines(report) or []
        except Exception:  # noqa: BLE001
            return False
        return newer_material_wire(frozenset(covered), fetched)

    def _record_form4_backlog(self, run_id: str, refresh: dict) -> None:
        """Persist the pre-market Form 4 backlog and coverage. Never raises."""
        if not isinstance(refresh, dict):
            return
        import json as _json
        keys = (
            "status", "pending_filings", "watched_pending_filings",
            "discovery_cap_reached", "watched_read_through",
            "watched_names", "watched_names_read_through",
            "watched_names_unread", "watched_unchecked_names",
            "watched_drain_ran", "watched_drain_read",
            "watched_drain_deadline_hit", "edgar_coverage", "error",
        )
        _persist_evidence(
            getattr(self, "db", None), run_id=run_id,
            agent_name="smart_money_refresh", kind="form4_backlog", scope="run",
            evidence_json=_json.dumps(
                {k: refresh.get(k) for k in keys}, sort_keys=True, default=str,
            ),
        )

    def _record_congressional_refresh(self, run_id: str, refresh: dict) -> None:
        """Persist the congressional refresh's counts. Never raises.

        Same record as the Form 4 backlog above: per source fetched, already
        seen, processed, new, dropped by reason, watermark before/after,
        duration, and how old the newest disclosure and each source's copy
        are. Nothing is written when the congressional feed is switched off.
        """
        summary = refresh.get("congressional") if isinstance(refresh, dict) else None
        if not isinstance(summary, dict):
            return
        import json as _json
        _persist_evidence(
            getattr(self, "db", None), run_id=run_id,
            agent_name="smart_money_refresh", kind="congressional_refresh",
            scope="run",
            evidence_json=_json.dumps(summary, sort_keys=True, default=str),
        )

    def _alert_form4_backlog_before_open(self, refresh: dict) -> None:
        """Say BEFORE the open that today's insider evidence is incomplete.

        `refresh` has always computed the backlog numbers and the pipeline
        had only ever logged them. A returned value nobody catches is a
        check that does not exist — on 2026-09-18 the cap bound at the
        pre-market refresh, and the first anyone knew of it was six lost
        decision windows later.

        The condition is coverage: every watched name read through today,
        nothing unread, nothing unchecked. Anything else means the insider
        seat cannot be current on every tick today. Since PR #535 that seat
        is advisory — it no longer stops the desk — so the alert says the
        desk decides WITHOUT complete insider evidence, not that it refuses.
        """
        if not isinstance(refresh, dict):
            return
        from src.util.time import et_today
        read_through = str(refresh.get("watched_read_through") or "").strip()[:10]
        today = et_today().isoformat()
        watched_pending = int(refresh.get("watched_pending_filings") or 0)
        unchecked = list(refresh.get("watched_unchecked_names") or [])
        cap_reached = bool(refresh.get("discovery_cap_reached"))
        names = int(refresh.get("watched_names") or 0)
        names_read = int(refresh.get("watched_names_read_through") or 0)
        # Board item 126. EDGAR publishes its own count of the Form 4s filed
        # on a day. When the morning read could not obtain that count, it
        # cannot tell "nobody filed anything" from "our read of the filings
        # service came back broken" — and the second case used to reach this
        # desk looking exactly like the first.
        #
        # Fail CLOSED on a missing record, matching the morning seat in
        # src/pipeline_stages.py: a refresh that ran a Form 4 pass and
        # recorded no coverage answered the question not at all, which is
        # not the same as answering it well. A refresh that carries the
        # Form 4 drain keys is held to this, and so is one that reports an
        # error — a sub-provider that raised outright produces neither the
        # drain keys nor a coverage record, and that is the LOUDEST case,
        # not an exemption. A wrapper with neither is not asked to answer
        # for coverage it never had; it is NOT thereby let off the alert,
        # because an empty `watched_read_through` still trips the ordinary
        # did-not-finish clause below.
        edgar = refresh.get("edgar_coverage")
        form4_answered = "watched_drain_ran" in refresh or bool(refresh.get("error"))
        edgar_unverified = (
            form4_answered
            and not (isinstance(edgar, dict) and edgar.get("verified"))
        )
        record = edgar if isinstance(edgar, dict) else {}
        # Reported whether or not anything is wrong. The market-wide scan is
        # bounded by its own deadline and in production reaches a minority
        # of the lookback window, so "how much of the window did we check"
        # is a fact the owner needs on an ORDINARY morning — rendering it
        # only on the failure branch would have shown him the honest number
        # exactly when it was least representative.
        #
        # Gated on whether coverage was RECORDED, not merely present. A
        # blank record is all zeros, and "read 0 of 0 filings across 0 of 0
        # days" reads to a human as nothing to worry about when it means
        # the opposite — the same trap `ratio` already avoids by answering
        # None to nought-of-nought rather than 1.0.
        if record.get("known"):
            coverage_line = (
                "Insider-filing coverage this morning: read "
                f"{record.get('enumerated', 0)} of {record.get('edgar_total', 0)} "
                "filings the service reported, across "
                f"{record.get('days_queried', 0)} of "
                f"{record.get('days_in_window', 0)} days looked at."
            )
        elif record:
            coverage_line = (
                "Insider-filing coverage this morning: NOT KNOWN — the "
                "morning read did not record how much of the filing service "
                "it covered."
            )
        else:
            coverage_line = ""
        if coverage_line:
            logger.info("PRE-OPEN: %s", coverage_line)
        if (
            read_through == today and not watched_pending and not unchecked
            and not edgar_unverified
        ):
            return
        why: list[str] = []
        if edgar_unverified:
            reasons = ", ".join(str(r) for r in (record.get("reasons") or [])) \
                or "no coverage was recorded at all"
            why.append(
                "the filing service did not account for how many filings "
                f"existed, so a quiet day and a failed read cannot be told "
                f"apart ({reasons})",
            )
        if names:
            why.append(
                f"{names_read} of our {names} companies have every insider "
                "filing read",
            )
        if watched_pending:
            why.append(
                f"{watched_pending} company filing(s) on names we hold are "
                "still unread",
            )
        if unchecked:
            why.append(
                f"{len(unchecked)} of our own companies could not be checked "
                "at all",
            )
        if bool(refresh.get("watched_drain_deadline_hit")):
            why.append(
                "the morning read of our own companies ran out of time; it "
                "resumes where it stopped tomorrow morning",
            )
        if cap_reached:
            why.append(
                "the morning read stopped at its own limit before finishing",
            )
        if not why:
            why.append(
                "the morning read did not confirm it finished"
                + (f" (last confirmed {read_through})" if read_through else ""),
            )
        text = (
            "Insider-filing check did not finish this morning: "
            + "; ".join(why)
            + ". Until it does, the desk still makes its trading decisions "
            "but without complete insider evidence, and each decision "
            "records that. Existing positions and their stops are unaffected."
            # Carried whatever the reason for the alert, not only when
            # coverage itself is the complaint — the counts are the context
            # for every other line above them.
            + (f" {coverage_line}" if coverage_line else "")
        )
        logger.error("PRE-OPEN: %s", text)
        try:
            from src.notifier import send_owner_alert
            send_owner_alert(text)
        except Exception as exc:  # noqa: BLE001
            logger.error("Form 4 backlog pre-open alert failed to send: %s", exc)

    def _form4_freshness(self, ctx=None, symbols=None) -> dict:
        """"Has anything been FILED on a watched name since our last read?"

        The ONLY freshness question the decision tick asks. It is answered
        from each watched issuer's own SEC filing history — O(watched names)
        plain GETs — not from a full-text crawl of the whole filing stream.
        The crawl answers a different question ("is there a filing I have
        not read?"), belongs to the pre-market producing step, and ran
        inside every decision tick until 2026-09-18, where it cost six
        consecutive decision windows.

        Returns the provider verdict unchanged. A provider that cannot
        answer returns ``ok=False``, and the caller MUST treat that as
        unknown freshness rather than as "nothing new".
        """
        provider = getattr(self, "smart_money_provider", None)
        probe = getattr(provider, "form4_freshness", None)
        if not callable(probe):
            # No probe at all is not a silent pass. The seat's freshness is
            # unknown, and unknown loses the seat at the evidence gate.
            return {
                "ok": False, "new_filings": [], "read_through": "",
                "checked": 0, "unchecked": [],
                "reason": "provider cannot answer Form 4 freshness",
            }
        if symbols is None:
            symbols = self._watched_research_symbols(ctx=ctx)
        try:
            try:
                result = probe(symbols)
            except TypeError:
                result = probe()
        except Exception as exc:  # noqa: BLE001
            # Logged here, not only returned: the caller logs only when it
            # holds findings, so an empty-seat tick used to lose this.
            logger.warning(
                "Form 4 freshness probe raised %s: %s", type(exc).__name__, exc,
            )
            return {
                "ok": False, "new_filings": [], "read_through": "",
                "checked": 0, "unchecked": [],
                "reason": f"freshness probe raised {type(exc).__name__}: {exc}",
            }
        return result if isinstance(result, dict) else {
            "ok": False, "new_filings": [], "read_through": "",
            "checked": 0, "unchecked": [],
            "reason": "freshness probe returned no verdict",
        }

    def _peek_new_form4_accessions(self, ctx=None, symbols=None) -> set[str]:
        """Currently visible Form 4 accessions for names we watch."""
        provider = getattr(self, "smart_money_provider", None)
        peek = getattr(provider, "peek_form4_accessions", None)
        if not callable(peek):
            peek = getattr(provider, "peek_accessions", None)
        if not callable(peek):
            return set()
        if symbols is None:
            symbols = self._watched_research_symbols(ctx=ctx)
        try:
            try:
                found = peek(symbols)
            except TypeError:
                found = peek()
            return {str(a).strip() for a in (found or []) if str(a).strip()}
        except Exception as exc:  # noqa: BLE001 — failed peek ≠ new filing
            # No live caller since PR #529 (the decision tick uses
            # `_form4_freshness`). Still: a swallowed failure here returned
            # "nothing new" with no trace at all. Say so.
            logger.warning(
                "Form 4 accession peek failed, returning no accessions: %s: %s",
                type(exc).__name__, exc,
            )
            return set()

    def _form4_known_accessions(self) -> set[str]:
        """Accessions already processed or cached. No network."""
        out: set[str] = set()
        provider = getattr(self, "smart_money_provider", None)
        providers = getattr(provider, "providers", None)
        if not isinstance(providers, (list, tuple)):
            providers = [provider] if provider is not None else []
        for item in providers:
            known = getattr(item, "known_accessions", None)
            if not callable(known):
                continue
            try:
                out.update(str(a).strip() for a in (known() or []) if str(a).strip())
            except Exception:  # noqa: BLE001
                continue
        return out

    def _findings_from_specialist_evidence(self) -> list:
        """Most recent smart-money findings from specialist_evidence. [] if none."""
        from src.models import SmartMoneyFinding
        db = getattr(self, "db", None)
        execute = getattr(db, "execute", None)
        if not callable(execute):
            return []
        try:
            row = execute(
                "SELECT run_id FROM specialist_evidence "
                "WHERE agent_name = ? AND kind IN ('finding', 'scan_summary') "
                "ORDER BY id DESC LIMIT 1",
                ("smart_money_analyst",),
            ).fetchone()
        except Exception:  # noqa: BLE001
            return []
        if not row:
            return []
        try:
            run_id = row["run_id"] if hasattr(row, "keys") else row[0]
        except Exception:  # noqa: BLE001
            return []
        if not isinstance(run_id, str) or not run_id.strip():
            return []
        try:
            rows = execute(
                "SELECT evidence_json FROM specialist_evidence "
                "WHERE run_id = ? AND agent_name = ? AND kind = ? "
                "ORDER BY id",
                (run_id, "smart_money_analyst", "finding"),
            ).fetchall()
        except Exception:  # noqa: BLE001
            return []
        findings: list = []
        for item in rows or []:
            try:
                raw = item["evidence_json"] if hasattr(item, "keys") else item[0]
            except Exception:  # noqa: BLE001
                continue
            if not isinstance(raw, str) or not raw.strip():
                continue
            try:
                findings.append(SmartMoneyFinding.model_validate_json(raw))
            except Exception:  # noqa: BLE001 — skip unreadable rows
                continue
        return findings

    def _load_remembered_insider_findings(self, ctx) -> tuple[list, set[str]]:
        """Remembered Form 4 findings plus the accessions already seen.

        Findings come from this tick if already populated, else from
        specialist_evidence. Accessions are the cached/processed set so a
        non-material filing already seen cannot look 'new'.
        """
        findings: list = list(getattr(ctx, "smart_money_findings", None) or [])
        if not findings:
            findings = self._findings_from_specialist_evidence()
        accessions = set(self._form4_known_accessions())
        for finding in findings:
            observations = getattr(finding, "observations", None)
            if observations is None and isinstance(finding, dict):
                observations = finding.get("observations")
            for obs in observations or []:
                acc = getattr(obs, "accession_number", None)
                if acc is None and isinstance(obs, dict):
                    acc = obs.get("accession_number")
                text = str(acc or "").strip()
                if text:
                    accessions.add(text)
        return findings, accessions

    def _carry_forward_macro(self) -> CarryForward:
        """Remembered macro regime, keyed by kind+expiry event.

        GOOD same-session reuse stays `carried_from_morning` (PR #430).
        A GOOD prior-day regime is `remembered` until a real regime/print
        change — not `carry_forward_empty`. A blank snapshot with no
        regime is lost. A same-day `{date, regime}` trim is a regime
        snapshot for holding-discipline; it is not a full MacroAnalysis.
        PM still refuses a chain-less dict via `_macro_analysis_as_dict`.
        Holding-discipline must read `.same_session`, not payload
        truthiness, so a cross-day remember cannot falsify today's exit
        claim. An undated snapshot is not same-session — that claim
        needs a trustworthy date.
        """
        from src.evidence_kind import macro_reuse, same_session_from_date
        from src.seat_heal import coerce_macro_shape
        try:
            state = self.macro_store.load_last_state() or None
        except Exception as e:  # noqa: BLE001 — never fail a tick on carry-forward
            logger.warning("Intraday scan: macro carry-forward failed: %s", e)
            return CarryForward(None, "carry_forward_failed", same_session=False)
        if not state:
            return CarryForward(None, "carry_forward_empty", same_session=False)
        stored_date = str(state.get("date") or state.get("as_of") or "").strip()[:10]
        same_session = same_session_from_date(stored_date)
        # A regime snapshot is reusable research for holding-discipline and
        # kind-reuse. Full MacroAnalysis validation is the PM path
        # (`_macro_analysis_as_dict`) — requiring a chain here turned a
        # same-day {date, regime} read into carry_forward_failed and made
        # a provably-false exit claim look unverifiable.
        if not isinstance(state, dict) or not str(state.get("regime") or "").strip():
            return CarryForward(None, "carry_forward_failed", same_session=same_session)
        payload, _fixes = coerce_macro_shape(dict(state))
        verdict = macro_reuse(
            payload,
            same_session=same_session,
            regime_or_print_changed=self._macro_regime_or_print_changed(payload),
        )
        if not verdict.usable:
            return CarryForward(None, verdict.status, same_session=same_session)
        return CarryForward(payload, verdict.status, same_session=same_session)

    def _carry_forward_news(self, ctx: RunContext | None = None) -> CarryForward:
        """This session's news intelligence, re-validated from its stored dump.

        Same-session GOOD reuse is #430. A newer material wire expires it.
        Parse failure is lost, never reused as research.

        When the wire DID move, the peek that proved it holds current wire
        text. Hand that to the heal path (`ctx.heal_news_text`) so the
        expired seat is re-asked with the data this tick already paid for,
        instead of being lost while fresh headlines are thrown away.

        The evidence gate is untouched by this, but NOT because expired is
        lost — it stopped being lost when #535 split `CATEGORY_EXPIRED` out
        on 2026-09-18, which is what orphaned this hand-off for five days.
        The gate is untouched because the re-ask changes only whether a
        fresher answer exists, never what the gate does with the answer the
        desk already holds. See `evidence_gate.HEALABLE_CATEGORIES`.
        """
        from src.evidence_kind import news_reuse
        try:
            report = self.news_store.load_daily_report()
            if not report:
                return CarryForward(None, "carry_forward_empty", same_session=False)
            from src.models import NewsIntelligenceReport
            payload = NewsIntelligenceReport(**report)
        except Exception as e:  # noqa: BLE001
            logger.warning("Intraday scan: news carry-forward failed: %s", e)
            return CarryForward(None, "carry_forward_failed", same_session=False)
        # load_daily_report only opens today's dated directory. A successful
        # load is therefore same-session; there is no undated news snapshot
        # on this path. Empty/failed above cannot claim it.
        wire_moved = self._news_has_newer_material_wire(payload)
        verdict = news_reuse(
            payload,
            same_session=True,
            newer_material_wire=wire_moved,
        )
        if not verdict.usable:
            if wire_moved and ctx is not None:
                wire_text = self._peeked_news_wire_text()
                if wire_text:
                    ctx.heal_news_text = wire_text
            return CarryForward(None, verdict.status, same_session=True)
        return CarryForward(payload, verdict.status, same_session=True)

    def _carry_forward_earnings(self, ctx: RunContext) -> CarryForward:
        """Remembered earnings write-ups until the next report / 8-K.

        Loads the cached analyses (no LLM). A `queued=True` placeholder is
        the expiry event — a new filing the preprocess has not written up.
        """
        from src.evidence_kind import earnings_reuse
        provider = getattr(self, "earnings_provider", None)
        load = getattr(self, "_load_earnings_analyses", None)
        if provider is None or not callable(load):
            return CarryForward([], "not_run_intraday", same_session=True)
        try:
            _, results = self._load_earnings_analyses(
                ctx.run_id, session=ctx.session, ctx=ctx,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("Intraday scan: earnings remember failed: %s", e)
            return CarryForward(None, "carry_forward_failed", same_session=True)
        results = list(results or [])
        new_report = any(
            isinstance(item, dict) and item.get("queued")
            for item in results
        )
        analyzed = [
            item for item in results
            if isinstance(item, dict) and isinstance(item.get("analysis"), dict)
        ]
        payload = analyzed if analyzed else results
        verdict = earnings_reuse(
            payload if payload else [],
            same_session=True,
            new_report_or_8k=new_report,
        )
        # Placeholders for new filings are still handed to PM (it sizes
        # down); the status is `expired` only when we have nothing usable
        # AND a new filing. When we have cached write-ups plus a queued
        # name, keep the write-ups and label the seat partial via the
        # existing earnings classifier — do not drop remembered work.
        if analyzed and new_report:
            return CarryForward(results, "chose_not_to_refetch", same_session=True)
        if not verdict.usable and not results:
            return CarryForward(None, verdict.status, same_session=True)
        status = verdict.status
        if verdict.decision == "refetch" and not analyzed:
            status = "expired"
            return CarryForward(results, status, same_session=True)
        return CarryForward(results, status, same_session=True)

    def _specialist_insider_as_of(self) -> str:
        """Timestamp of the latest smart-money specialist_evidence row, or ''."""
        db = getattr(self, "db", None)
        execute = getattr(db, "execute", None)
        if not callable(execute):
            return ""
        try:
            row = execute(
                "SELECT timestamp FROM specialist_evidence "
                "WHERE agent_name = ? AND kind IN ('finding', 'scan_summary') "
                "ORDER BY id DESC LIMIT 1",
                ("smart_money_analyst",),
            ).fetchone()
        except Exception:  # noqa: BLE001
            return ""
        if not row:
            return ""
        try:
            raw = row["timestamp"] if hasattr(row, "keys") else row[0]
        except Exception:  # noqa: BLE001
            return ""
        return str(raw or "").strip()

    def _insider_same_session(self, findings) -> bool:
        """True only with a trustworthy date equal to today.

        Production ``SmartMoneyFinding`` has no as_of field. The producing
        step's date is the specialist_evidence timestamp. An undated
        finding cannot claim same-session; Form 4 is still remembered
        until a new accession.
        """
        from src.evidence_kind import same_session_from_date
        for finding in findings or []:
            raw = finding if isinstance(finding, dict) else None
            for key in ("as_of", "date", "session_date", "analyzed_on"):
                value = getattr(finding, key, None)
                if value is None and raw is not None:
                    value = raw.get(key)
                if same_session_from_date(value):
                    return True
        return same_session_from_date(self._specialist_insider_as_of())

    def _carry_forward_insider(self, ctx: RunContext) -> CarryForward:
        """Remembered Form 4 findings; refresh only when a NEW filing appears."""
        from src.evidence_kind import insider_reuse
        findings: list = []
        accessions: set[str] = set()
        try:
            findings, accessions = self._load_remembered_insider_findings(ctx)
        except Exception as e:  # noqa: BLE001
            logger.warning("Intraday scan: insider remember failed: %s", e)
            return CarryForward(None, "carry_forward_failed", same_session=False)
        same_session = self._insider_same_session(findings)
        # The freshness ladder, written down deliberately because the old
        # code fell the wrong way at every rung. Previously a failed peek
        # was swallowed and became `new_form4=False`, i.e. "nothing new",
        # i.e. REUSE — so a broken network let the desk decide on research
        # it never checked was current, while a WORKING network that found
        # the desk's own unread backlog refused the decision. Backwards in
        # both directions. Now:
        #
        #   every watched name read through, nothing unread  -> reuse
        #   a read-through name has an unread filing          -> expired (real)
        #   probe failed or partial, or any watched name not
        #   yet fully read (per-issuer coverage)              -> expired
        #
        # Expiry is per tick and the probe is cheap, so an unknown costs one
        # window and the next tick re-asks. Coverage only grows: the
        # pre-market drain records each issuer as it finishes it. Reuse on an unknown would put a
        # decision on evidence nobody checked, which the evidence gate
        # exists to prevent and which no later tick can undo.
        freshness = self._form4_freshness(ctx=ctx)
        probe_ok = bool(freshness.get("ok"))
        incoming = {
            str(a).strip() for a in (freshness.get("new_filings") or [])
            if str(a).strip()
        }
        new_form4 = bool(incoming - set(accessions))
        # Fail closed whenever the probe cannot call the seat current —
        # including when the remembered answer is EMPTY. CORRECTED
        # 2026-09-19: this used to expire only a seat holding findings, on
        # the stated ground that `insider_reuse` "already classifies an
        # empty payload as lost". It does not: an empty list is BLANK, and
        # BLANK reuses as `chose_not_to_refetch` — "Form 4 filings
        # remembered; no new filing", an integrity-clean status. An empty
        # answer is still a claim ("no material insider activity on any
        # watched name"), and it is exactly as uncheckable as a full one
        # when the probe failed or some watched names were never fully
        # read. `not ok` now covers both: a failed or partial probe, and
        # partial COVERAGE (`unread_names`), whose reason says how many
        # names are not yet read. A desk with NO insider provider at all has
        # no seat to be stale about, so an empty answer there is left alone.
        has_provider = getattr(self, "smart_money_provider", None) is not None
        if not probe_ok and (findings or has_provider):
            logger.warning(
                "Intraday scan: insider seat cannot be called current, "
                "expires this tick — %s", freshness.get("reason") or "no reason",
            )
            return CarryForward(findings, "expired", same_session=same_session)
        verdict = insider_reuse(
            findings if findings else [],
            same_session=same_session,
            new_form4=new_form4,
        )
        if verdict.decision == "refetch":
            return CarryForward(findings, "expired", same_session=same_session)
        return CarryForward(findings, verdict.status, same_session=same_session)

    def _record_heal(self, ctx: RunContext, result, *, alert: bool) -> None:
        """Durable heal log. Pages only on attempted-and-failed / cap-block."""
        import json as _json
        try:
            _persist_evidence(
                self.db, run_id=ctx.run_id, agent_name="seat_heal",
                kind="seat_heal", scope="run",
                evidence_json=_json.dumps(result.to_evidence(), sort_keys=True),
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("seat heal: evidence write failed: %s", e)
        if not alert:
            return
        try:
            from src.notifier import send_owner_alert
            from src.seat_heal import HEAL_CAP_BLOCKED, heal_failure_alert_text
            send_owner_alert(
                heal_failure_alert_text(
                    result, cap_blocked=result.outcome == HEAL_CAP_BLOCKED,
                ),
            )
        except Exception as e:  # noqa: BLE001
            logger.error("seat heal: owner alert failed: %s", e)

    def _try_one_paid_research_retry(self, ctx: RunContext, seat: str) -> bool:
        """One paid retry for a LOST or EXPIRED seat, once per ET day.

        False if we cannot honestly retry. An EXPIRED seat is a REFRESH, not
        a recovery: the desk holds the earlier answer and will decide on it
        either way, so every owner-facing sentence out of here must say that
        rather than claim the seat was lost.

        Intra often has no FRED/news stack. A retry without inputs would
        invent the seat — refuse that, and do not burn the retry slot.
        Cost-cap blocks alert the owner. Success is a durable log, not a page.
        """
        from src.cost_circuit import PaidAnalysisSuspended
        from src.seat_heal import (
            HealResult, HEAL_CAP_BLOCKED, HEAL_DAY_CAP, HEAL_FAILED,
            HEAL_PAID_RETRY, can_paid_retry, record_paid_retry,
        )
        from src import evidence_gate as _gate
        # An EXPIRED seat is being REFRESHED, not recovered: the desk holds
        # the earlier answer and will decide on it whatever happens here. Any
        # owner page from this function must say so, because the default
        # sentence ("the desk will not decide on this seat as if it had
        # answered") is true of a lost seat and false of this one — the
        # owner-facing-lie class of defect item 133 closed.
        _incoming = (getattr(ctx, "data_status", None) or {}).get(seat)
        _expired_seat = (
            _gate.STATUS_CATEGORY.get(_incoming) == _gate.CATEGORY_EXPIRED
        )
        _consequence = (
            "The desk still holds this seat's earlier answer and will decide "
            "on it, labelled as carried rather than read this tick. No trade "
            "was withheld for this."
        ) if _expired_seat else ""
        retries = dict(getattr(ctx, "heal_paid_retries", None) or {})
        if not can_paid_retry(retries, seat):
            return False
        require = getattr(self, "_require_paid_analysis", None)
        agent_name = {
            "macro": "macro_analyst",
            "news": "news_analyst",
            "tech": "tech_analyst",
        }.get(seat)
        if agent_name is None or not callable(require):
            return False
        agent = getattr(self, agent_name, None)
        analyze = (
            getattr(agent, "analyze", None) if seat != "tech"
            else getattr(agent, "analyze_batch", None)
        )
        if not callable(analyze):
            return False
        # The analyst already spent the one paid retry on its own parse.
        if getattr(agent, "_heal_retry_used", False) is True:
            return False
        # No inputs → would invent the seat. Do not consume the retry.
        if seat == "macro" and not (ctx.macro_summary or {}):
            return False
        if seat == "news":
            # Fresh wire text is required. Morning parse-site retry lives
            # on the analyst; intra has no honest news_text unless a hook
            # supplied one.
            news_text = getattr(ctx, "heal_news_text", None)
            if not (isinstance(news_text, str) and news_text.strip()):
                return False
        if seat == "tech":
            return False
        # Cross-tick cap, checked HERE — after the honest-inputs checks
        # above, never before them. A tick that has no wire text would have
        # refused anyway, and a day-cap row on that tick would record the cap
        # as the binding constraint when it was not. That row's whole purpose
        # is to be the evidence that later settles whether one refresh a day
        # is the right number, so it must only be written when the cap is
        # what actually stopped the spend.
        #
        # `retries` above is per-RunContext and a RunContext is one tick;
        # intra_check runs every 30 minutes and an expired seat is still
        # expired on the next tick, so without this the "one paid retry" is
        # one per tick. It was: production recorded EIGHT paid news heals on
        # 2026-09-18. See Database.count_paid_seat_heals_today.
        db = getattr(self, "db", None)
        counter = getattr(db, "count_paid_seat_heals_today", None)
        if callable(counter):
            try:
                spent_today = counter(seat)
            except Exception as e:  # noqa: BLE001
                logger.warning("seat heal: day-cap read failed for %s: %s", seat, e)
                spent_today = None
            if spent_today is None:
                # Could not find out, which is NOT the same as nothing spent.
                # Allowed through on purpose: the cost circuit below is the
                # fail-closed authority for spend and still runs, so a sick
                # forensic store cannot silently stop the desk buying fresher
                # news. Logged at WARNING so the degradation is visible
                # rather than assumed.
                logger.warning(
                    "seat heal: could not read %s's day allowance; allowing "
                    "the retry and leaving the spend to the cost circuit", seat,
                )
            elif not can_paid_retry({seat: int(spent_today)}, seat):
                logger.info(
                    "seat heal: %s already had its one paid retry today "
                    "(%d spent); not re-asking", seat, spent_today,
                )
                # Durable, not just a log line. "The desk declined to pay for
                # fresher research on this tick" is a decision about money,
                # and it is the only record that could ever show whether one
                # refresh a day is the right number. Not an owner page: the
                # cap doing its job is not an incident.
                self._record_heal(
                    ctx,
                    HealResult(
                        seat=seat, outcome=HEAL_DAY_CAP,
                        reason=(
                            f"seat already had its one paid heal this ET day "
                            f"({spent_today} recorded); not re-asking"
                        ),
                        paid_retry=False,
                        details={
                            "spent_today": int(spent_today),
                            "was_expired": _expired_seat,
                        },
                        owner_consequence=_consequence,
                    ),
                    alert=False,
                )
                return False
        try:
            require(agent_name)
        except PaidAnalysisSuspended as exc:
            blocked = HealResult(
                seat=seat, outcome=HEAL_CAP_BLOCKED,
                reason=f"spend cap blocked the one paid retry: {exc}",
                paid_retry=False, owner_consequence=_consequence,
                details={"was_expired": _expired_seat},
            )
            self._record_heal(ctx, blocked, alert=True)
            return False
        except Exception as exc:  # noqa: BLE001
            logger.warning("seat heal: cost-circuit preflight failed for %s: %s", seat, exc)
            return False
        ctx.heal_paid_retries = record_paid_retry(retries, seat)
        try:
            if seat == "macro":
                analysis, _raw = analyze(ctx.macro_summary)
            else:
                # Pass this run's session. The analyst's session guidance
                # defaults to MORNING ("treat today as a fresh book... this
                # report sets the tone for the day's trading"), which is
                # false on a 14:00 intra_check — the same mislabelling audit
                # round 2 #24 already fixed for the close session. The other
                # arguments stay at their defaults: a heal re-ask genuinely
                # has no universe or prior-session baseline to offer, and
                # inventing one would be worse than admitting it.
                analysis, _raw = analyze(
                    getattr(ctx, "heal_news_text", ""),
                    session=getattr(ctx, "session", None) or "intra_check",
                )
        except Exception as exc:  # noqa: BLE001
            failed = HealResult(
                seat=seat, outcome=HEAL_FAILED,
                reason=f"paid heal retry raised: {exc}",
                paid_retry=True, owner_consequence=_consequence,
                details={"was_expired": _expired_seat},
            )
            self._record_heal(ctx, failed, alert=True)
            return False
        if analysis is None:
            failed = HealResult(
                seat=seat, outcome=HEAL_FAILED,
                reason="paid heal retry returned no usable output",
                paid_retry=True, owner_consequence=_consequence,
                details={"was_expired": _expired_seat},
            )
            self._record_heal(ctx, failed, alert=True)
            return False
        payload = (
            analysis.model_dump() if hasattr(analysis, "model_dump") else analysis
        )
        if seat == "macro":
            ctx.macro_analysis = payload
        elif seat == "news":
            ctx.news_intel = analysis
        status = dict(ctx.data_status or {})
        status[seat] = "ok"
        ctx.data_status = status
        self._record_heal(
            ctx,
            HealResult(
                seat=seat, outcome=HEAL_PAID_RETRY,
                reason=(
                    "one paid retry refreshed a superseded seat"
                    if _expired_seat else
                    "one paid retry replaced a lost seat"
                ),
                payload=payload, paid_retry=True, usable=True,
                details={"was_expired": _expired_seat},
            ),
            alert=False,
        )
        return True

    def _heal_lost_research_seats(self, ctx: RunContext) -> None:
        """After carry-forward: log unhealed seats. Don't page empty-store gaps
        (the evidence gate already pages those). Attempt a paid retry only
        when the seat's inputs actually exist on this run.

        Selects work by `evidence_gate.HEALABLE_CATEGORIES`, which covers a
        LOST seat (no answer) and an EXPIRED one (an answer the desk knows is
        superseded). Those two are deliberately different categories to the
        evidence gate and stay different: this loop reads the set only to
        decide whether to go and LOOK again, and changes no verdict, no skip,
        no degraded count and no freshness label. Testing for CATEGORY_LOST
        here is what orphaned the expired-news refresh on 2026-09-18.
        """
        from src import evidence_gate
        from src.seat_heal import HealResult, HEAL_FAILED
        data_status = ctx.data_status or {}
        for seat, status in list(data_status.items()):
            category = evidence_gate.STATUS_CATEGORY.get(status)
            if category not in evidence_gate.HEALABLE_CATEGORIES:
                continue
            # Empty store: nothing to heal. Gate skip is the owner page.
            if status == "carry_forward_empty":
                continue
            was_expired = category == evidence_gate.CATEGORY_EXPIRED
            attempted = self._try_one_paid_research_retry(ctx, seat)
            still = (ctx.data_status or {}).get(seat)
            if evidence_gate.STATUS_CATEGORY.get(still) not in evidence_gate.HEALABLE_CATEGORIES:
                continue
            # A LOST seat is recorded whether or not a retry was possible —
            # that row is the forensic trail for an absent answer. An EXPIRED
            # seat is not absent, and most expired seats (insider, earnings)
            # have no heal wired at all by design (#535 dissolved that
            # asymmetry rather than repairing it), so recording every one of
            # them would bury the real rows in noise. Record an expired seat
            # only when the desk actually tried and failed.
            if was_expired and not attempted:
                continue
            # An expired seat that could not be refreshed is NOT a seat the
            # desk will refuse to decide on — it still holds the earlier
            # answer. Saying otherwise in the alert would repeat the
            # owner-facing lie item 133 fixed, so the consequence sentence is
            # overridden rather than defaulted.
            consequence = (
                "The desk still holds this seat's earlier answer and will "
                "decide on it, labelled as carried rather than read this "
                "tick. No trade was withheld for this."
            ) if was_expired else ""
            result = HealResult(
                seat=seat, outcome=HEAL_FAILED,
                reason=f"seat still {still} after mechanical heal",
                details={
                    "status": still,
                    "paid_retry_attempted": attempted,
                    "was_expired": was_expired,
                },
                owner_consequence=consequence,
            )
            # Page only when we actually paid a retry and it failed.
            # Kind-expiry / empty-store without inputs is the evidence
            # gate's skip, not a second owner page.
            self._record_heal(ctx, result, alert=bool(attempted))

    def _intraday_held_tech_symbols(self, ctx: RunContext) -> list[str]:
        """Investable holdings that need current-run Technical on this scan.

        Morning's Tech pre-filter already includes every held name
        (`_has_actionable_signal_fn`). The intraday scan used to send only
        names that moved past the threshold, so a quiet hold the PM can
        still increase had no current-run Technical: grounding failed the
        whole paid decision (`pm_grounding_error`, "increase lacks a
        current-run Technical analysis"). Missing specialist data is a
        defect in the producing step — this list is that step. Cash-park
        vehicles have no thesis and stay out.
        """
        parked: set[str] = set()
        sweeper = self._sweeper()
        investable = list(ctx.positions or [])
        if sweeper is not None:
            investable, _parked = sweeper.split_positions(investable)
        retired = self._retired_cash_park_symbol()
        if isinstance(retired, str) and retired.strip():
            parked.add(retired.strip().upper())
        seen: set[str] = set()
        out: list[str] = []
        for pos in investable:
            if not getattr(pos, "qty", 0):
                continue
            symbol = str(getattr(pos, "symbol", "") or "").strip().upper()
            if not symbol or symbol in seen or symbol in parked:
                continue
            seen.add(symbol)
            out.append(symbol)
        return out

    def _intraday_scan_mover_candidates(
        self, ctx: RunContext,
    ) -> tuple[list[tuple[str, float]], dict]:
        """Cheap snapshot of who moved. No paid calls.

        Runs before the owner-lock wait so a contended morning/midday
        cannot vanish the mover list. A skip after wait names these
        symbols in a durable reason instead of dropping them silently.
        """
        from src.data.live_price import (
            NO_PRICE_AT_ALL, NO_SNAPSHOT, resolve_live_price,
        )

        cfg = self.config.intraday_scan
        universe = list(self.config.trading.universe)
        snapshots = self.broker.get_intraday_snapshots(universe) or {}
        if not snapshots:
            return [], {}
        candidates: list[tuple[str, float]] = []
        for symbol in universe:
            snap = snapshots.get(symbol) or {}
            # item 120: the move that buys a PAID look has to be today's.
            # This read `last_price` straight, so a name still carrying a
            # prior session's print measured a move that did not happen
            # today. Same resolver as the morning Tech pass, so "today" is
            # decided in one place.
            resolved = resolve_live_price(snap)
            last = resolved.price
            prev = snap.get("prev_close")
            # The miss counter pages the owner after three consecutive
            # scans with "check whether the ticker is still valid/tradable
            # on Alpaca". It exists to tell a BROKEN ticker from a quiet
            # one (`src/storage/db.py`), so a thin name that simply has not
            # printed today must NOT feed it — item 120's own filing names
            # two IEX-thin names in exactly that state, and paging on them
            # would be a false alarm. A symbol the feed returned nothing
            # for at all is still a miss.
            fed_nothing = resolved.unavailable in (NO_SNAPSHOT, NO_PRICE_AT_ALL)
            if not isinstance(prev, (int, float)) or (
                last is None and fed_nothing
            ):
                self._track_intraday_snapshot_miss(symbol)
                continue
            self._track_intraday_snapshot_ok(symbol)
            if last is None:
                # Quiet, not broken: the feed answered, the name has no
                # today print. It cannot have moved today, so it buys no
                # paid look — and it does not page anybody either.
                continue
            if prev <= 0:
                continue
            move_pct = abs(last - prev) / prev * 100.0
            if move_pct < cfg.move_threshold_pct:
                continue
            if self._recently_intraday_evaluated(symbol, cfg.cooldown_hours):
                continue
            candidates.append((symbol, move_pct))
        candidates.sort(key=lambda t: -t[1])
        return candidates, snapshots

    def _intraday_paid_scan_skip(self, ctx: RunContext, movers: list[str]) -> dict:
        """Durable skip: lock still held, movers named, no silent drop."""
        blocking = self._blocking_owner_session() or "owner_lock"
        named = ",".join(movers) if movers else "none"
        reason = (
            f"paid discovery skipped: {blocking} still held; movers={named}"
        )
        logger.warning("Intraday scan: %s", reason)
        for symbol in movers:
            try:
                _record_pipeline_event(
                    self, ctx, symbol, "opportunity", "skipped",
                    "intraday_scan_lock_contended", detail=reason,
                )
            except Exception:  # noqa: BLE001
                logger.warning(
                    "Intraday scan: could not persist skip reason for %s",
                    symbol, exc_info=True,
                )
        return {
            "status": "intraday_scan_lock_contended",
            "run_id": ctx.run_id,
            "reason": reason,
            "movers": list(movers),
        }

    def _intraday_opportunity_scan_body(self, ctx: RunContext) -> dict:
        """Bounded intraday opportunity discovery (2026-08-19 fix).

        Runs on the existing intra_check cadence — no new systemd timer,
        no full morning research stack. One cheap bulk current-session
        snapshot call flags symbols that moved materially since the last
        close; those movers (capped, cooldown-deduped against repeat churn)
        PLUS currently held investable names get real daily bars/indicators
        and a real tech_analyst call. Held names join the batch so an
        increase on a quiet hold has current-run Technical and can ground;
        they do not consume the mover cap or the mover cooldown. Then the
        SAME DecisionStage -> RiskStage -> ExecutionStage chain morning
        uses — no separate/duplicated decision logic, so PM's sizing rules,
        RM's veto authority and the deterministic gate all apply exactly
        as they do in the morning run. Bullish AND bearish setups both
        surface: the universe already includes the approved inverse ETFs
        (SH/SDS/PSQ/SQQQ), so a broad-market decline shows up as a
        qualifying move in those symbols the same way a rally shows up in
        a long candidate — no separate bearish code path needed.

        Returns a status dict at every early-exit point — never a bare
        None (2026-08-31 visibility fix; see `_run_intraday_opportunity_scan`
        for the full rationale).         "intraday_scan_lock_contended" when
        `_await_paid_scan_slot` cannot free the owner lock before this
        tick's calendar window ends; "intraday_scan_no_opportunity" for
        every other early return (no
        snapshots, no qualifying moves, no ledgerable symbols, no usable
        bars, no usable tech analysis). Past that point, a real result dict
        mirroring the shape callers of run_morning already expect
        (status/orders/run_id). Best-effort: any failure degrades to a
        status dict, never raises (the caller also wraps this
        defensively) — a scan miss costs a possible trade; a scan crash
        must never cost the loss-protection check that already ran this
        tick.

        The enabled-check and the process-level lock live in the
        `_run_intraday_opportunity_scan` wrapper; this is the body.
        """
        cfg = self.config.intraday_scan

        # Identify movers first (cheap snapshot) so a morning/midday owner
        # lock cannot vanish paid discovery. Then wait. If the lock is
        # still held at window end, skip with a durable reason that names
        # the movers — never sleep the scan away, never drop them silently.
        candidates, snapshots = self._intraday_scan_mover_candidates(ctx)
        mover_names = [s for s, _ in candidates[: cfg.max_candidates_per_scan]]
        if self._await_paid_scan_slot(ctx.run_id):
            return self._intraday_paid_scan_skip(ctx, mover_names)
        # The 09:30/13:00 wait must not size against the pre-fill snapshot
        # taken before morning finished. Refresh after the lock releases.
        if getattr(self, "_paid_scan_waited", False):
            try:
                account, positions, _ = self._refresh_account_state()
                ctx.account = account
                ctx.positions = positions
                ctx.cash = account["cash"]
                ctx.deployable_cash = self._compute_deployable_cash(
                    ctx.cash, positions,
                )
                ctx.total_value = account.get("portfolio_value", ctx.total_value)
                self._sync_positions_from_broker(positions)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "Intraday scan: post-wait broker refresh failed (%s) — "
                    "skipping paid discovery rather than sizing on a "
                    "pre-fill snapshot", exc,
                )
                return {"status": "intraday_scan_no_opportunity", "run_id": ctx.run_id}
            candidates, snapshots = self._intraday_scan_mover_candidates(ctx)

        if not snapshots:
            return {"status": "intraday_scan_no_opportunity", "run_id": ctx.run_id}

        if not candidates:
            return {"status": "intraday_scan_no_opportunity", "run_id": ctx.run_id}

        # Largest moves first, capped — bounded per-tick cost regardless of
        # how many symbols move on a broad market day; not a scan of
        # everything, a check of the few things that moved most.
        symbols = [s for s, _ in candidates[: cfg.max_candidates_per_scan]]
        logger.info(
            "Intraday scan: %d symbol(s) moved >= %.1f%% since last close "
            "and are outside the %.1fh cooldown: %s",
            len(symbols), cfg.move_threshold_pct, cfg.cooldown_hours, symbols,
        )
        move_by_symbol = dict(candidates)
        ledgered_symbols: list[str] = []
        for symbol in symbols:
            # Persist before any paid call. Every outcome—including a model
            # failure or no target—now consumes the configured cooldown.
            try:
                self.db.record_intraday_evaluation(
                    symbol=symbol, run_id=ctx.run_id, status="selected",
                    detail=f"move_pct={move_by_symbol[symbol]:.4f}",
                )
            except Exception as exc:
                logger.warning(
                    "Intraday evaluation ledger write failed for %s (%s) — "
                    "skipping it to avoid unbounded repeat spend", symbol, exc,
                )
                continue
            ledgered_symbols.append(symbol)
            _record_pipeline_event(
                self, ctx, symbol, "opportunity", "discovered",
                "intraday_move_threshold",
                move_pct=move_by_symbol[symbol],
                threshold_pct=cfg.move_threshold_pct,
            )
        symbols = ledgered_symbols
        if not symbols:
            return {"status": "intraday_scan_no_opportunity", "run_id": ctx.run_id}

        # Produce Technical for quiet holds on the same paid call. Discovery
        # stays mover-capped; coverage for names the PM can increase does
        # not compete with that cap and does not consume mover cooldown.
        # Dropping an ungrounded hold is not the product for missing Tech.
        held_for_tech = [
            s for s in self._intraday_held_tech_symbols(ctx)
            if s not in {x.upper() for x in symbols}
        ]
        if held_for_tech:
            logger.info(
                "Intraday scan: producing Technical for %d held name(s) "
                "the mover list did not cover: %s",
                len(held_for_tech), held_for_tech,
            )
        tech_symbols = list(symbols) + held_for_tech

        symbols_data = []
        symbols_bars: dict[str, list] = {}
        for symbol in tech_symbols:
            try:
                bars = self.market.get_ohlcv(symbol, self.config.trading.lookback_days)
            except Exception as e:  # noqa: BLE001
                logger.warning("Intraday scan: bar fetch failed for %s: %s", symbol, e)
                _record_pipeline_event(
                    self, ctx, symbol, "specialist", "failed",
                    "market_data_exception", detail=str(e),
                    specialist="tech_analyst",
                )
                continue
            if not bars:
                _record_pipeline_event(
                    self, ctx, symbol, "specialist", "failed",
                    "market_data_unavailable", specialist="tech_analyst",
                )
                continue
            indicators = compute_indicators(symbol, bars)
            symbols_data.append({"symbol": symbol, "bars": bars, "indicators": indicators})
            symbols_bars[symbol] = bars
        if not symbols_data:
            return {"status": "intraday_scan_no_opportunity", "run_id": ctx.run_id}
        ctx.symbols_bars = symbols_bars

        prior_macro_state: dict = {}
        try:
            prior_macro_state = self.macro_store.load_last_state() or {}
        except Exception as e:  # noqa: BLE001
            logger.warning("Intraday scan: prior macro state load failed: %s", e)
        prior_ratings: dict = {}
        try:
            prior_ratings = self.tech_store.load()
        except Exception as e:  # noqa: BLE001
            logger.warning("Intraday scan: tech store load failed: %s", e)

        # Truthful current-session evidence for exactly the names being
        # analyzed (2026-08-19): the scan detects on live prices, so Tech
        # must see those same live prices — not just daily bars ending at
        # yesterday's close, which is what triggered the scan being
        # invisible to the analyst that had to judge it. Rendered by
        # `build_user_message` as an explicit INCOMPLETE-session block,
        # never as a completed daily bar. Held names already in the
        # universe snapshot are included; a hold outside that snapshot
        # still gets bars, just no live-session block.
        # item 120: resolved through the SAME freshness rule the morning
        # pass uses. This used to hand Tech the raw snapshot, so a name
        # whose last trade was a prior session's could be rendered to the
        # intraday seat as "CURRENT SESSION (TODAY)".
        intraday_context, _missing, _stale, _rescued = self._resolve_live_context(
            snapshots, [s for s in tech_symbols if s in snapshots],
        )
        if _missing or _stale:
            logger.warning(
                "Intraday scan: no today print for %d symbol(s) handed to Tech "
                "(no price: %s; no today print: %s) — labelled as a lost price "
                "seat, never replaced by a prior session's number",
                len(_missing) + len(_stale), _missing[:10], _stale[:10],
            )
        self._require_paid_analysis("intraday_tech_analyst")
        analyses_map, ta_result = self.tech_analyst.analyze_batch(
            symbols_data,
            prior_ratings=prior_ratings,
            valuations={},
            intraday_context=intraday_context,
            prior_macro_regime=prior_macro_state.get("regime"),
            prior_macro_outlook=prior_macro_state.get("equity_outlook"),
        )
        # analyses_map carries every candidate symbol as a key (2026-08-19
        # Tech batch-response symbol-loss fix) — None marks a symbol
        # tech_analyst could not resolve even after its own bounded retry.
        # Filter before treating entries as real analyses.
        analyses = [a for a in analyses_map.values() if a is not None]
        failed_count = len(analyses_map) - len(analyses)
        if failed_count:
            logger.warning(
                "Intraday scan: %d/%d candidate symbol(s) failed to resolve "
                "even after retry: %s", failed_count, len(analyses_map),
                sorted(sym for sym, a in analyses_map.items() if a is None),
            )
        if ta_result:
            try:
                self.db.insert_agent_log(
                    agent_name="tech_analyst", run_id=ctx.run_id,
                    input_summary=(
                        f"Intraday scan batch: {len(analyses)}/{len(analyses_map)} "
                        f"symbols analyzed" + (f", {failed_count} failed" if failed_count else "")
                    ),
                    input_message=ta_result.user_message,
                    output_summary=", ".join(f"{a.symbol}:{a.rating}" for a in analyses),
                    full_response=ta_result.raw_text,
                    model=ta_result.model,
                    tokens_used=ta_result.tokens_used,
                    input_tokens=ta_result.input_tokens,
                    output_tokens=ta_result.output_tokens,
                    cost_usd=ta_result.cost_usd,
                    **agent_log_kwargs(ta_result),
                )
            except Exception as e:  # noqa: BLE001
                logger.warning("Intraday scan: tech_analyst agent_log insert failed: %s", e)
            for analysis in analyses:
                _persist_evidence(
                    self.db, run_id=ctx.run_id, agent_name="tech_analyst",
                    kind="analysis", scope="symbol", symbol=analysis.symbol,
                    evidence_json=analysis.model_dump_json(),
                )
                _record_pipeline_event(
                    self, ctx, analysis.symbol, "specialist", "evaluated",
                    "technical_analysis_validated",
                    specialist="tech_analyst", rating=analysis.rating,
                )
            for symbol, analysis in analyses_map.items():
                if analysis is None:
                    _record_pipeline_event(
                        self, ctx, symbol, "specialist", "failed",
                        "technical_analysis_unresolved_after_retry",
                        specialist="tech_analyst",
                    )
        if analyses:
            try:
                self.tech_store.update(analyses)
                ages = self.tech_store.compute_ages([a.symbol for a in analyses])
                for analysis in analyses:
                    if analysis.symbol in ages:
                        analysis.signal_age_days = ages[analysis.symbol]
            except Exception as e:  # noqa: BLE001
                logger.warning("Intraday scan: tech store update failed: %s", e)

        if not analyses:
            logger.info("Intraday scan: tech_analyst returned no usable analyses this tick")
            return {"status": "intraday_scan_no_opportunity", "run_id": ctx.run_id}

        # Same shared chain morning uses — no separate PM/RM/gate logic.
        #
        # Macro/news/earnings are still NOT re-fetched this tick — that is the
        # expensive research stack this scan exists to avoid rerunning, and
        # the saving is the whole point. But "not re-run" was previously
        # implemented as "not shown", and those are different things. This
        # session was handing the Portfolio Manager a technical-only view
        # while THIS MORNING'S macro regime and news sat on disk, already
        # paid for. The PM was blindfolded, not economical: `intra_check`
        # measured at $0.222/run against `morning`'s $0.221 over the 10 days
        # to 2026-08-27, ~99% of it the PM call, deciding on a fraction of
        # the evidence.
        #
        # So: carry the morning's results forward, and label them as carried.
        # The grounding property that mattered is preserved — nothing is
        # presented as having run this tick — while the PM stops reasoning
        # about an intraday move with no idea what regime it is happening in.
        ctx.analyses = analyses
        carried_macro = self._carry_forward_macro()
        carried_news = self._carry_forward_news(ctx)
        carried_earnings = self._carry_forward_earnings(ctx)
        carried_insider = self._carry_forward_insider(ctx)
        ctx.data_status = {
            "tech": "partial" if failed_count else "ok",
            # Status comes from the kind+event helpers, not from payload
            # truthiness. Same-session GOOD reuse is `carried_from_morning`
            # (PR #430). Cross-day GOOD macro is `remembered`. Empty/failed
            # carry still refuses BEFORE the Portfolio Manager. Earnings
            # without a provider on this object stays the intentional skip.
            "macro": carried_macro.status,
            "news": carried_news.status,
            "earnings": carried_earnings.status,
            "smart_money": carried_insider.status,
        }
        ctx.macro_analysis = carried_macro.payload
        ctx.news_intel = carried_news.payload
        ctx.earnings_results = list(carried_earnings.payload or [])
        ctx.smart_money_findings = list(carried_insider.payload or [])
        self._heal_lost_research_seats(ctx)

        # Same owner rule as morning: a decision on incomplete evidence is
        # fabricated. Applied here now that empty/failed carry-forward is
        # distinguishable from the intentional earnings skip.
        gate_skip = self._evidence_gate_skip(
            ctx, ctx.run_id, session=ctx.session,
        )
        if gate_skip is not None:
            gate_skip["candidates"] = symbols
            return gate_skip

        self.decision_stage.run(ctx)
        if not ctx.portfolio_decision:
            logger.error(
                "Intraday scan: PM failed (%s): %s",
                ctx.analysis_failure_status, ctx.analysis_failure_error,
            )
            return {
                "status": "intraday_analysis_error",
                "failure_status": ctx.analysis_failure_status or "pm_agent_failure",
                "error": ctx.analysis_failure_error or "no valid PM decision",
                "candidates": symbols,
                "run_id": ctx.run_id,
            }
        if not ctx.portfolio_decision.decisions:
            logger.info("Intraday scan: PM produced no actionable decisions")
            return {
                "status": "intraday_no_trades", "candidates": symbols,
                "run_id": ctx.run_id,
            }

        early_exit = self.risk_stage.run(ctx)
        if early_exit is not None:
            early_exit["candidates"] = symbols
            stop_updates = getattr(
                getattr(self, "broker", None), "stop_trade_updates", None,
            )
            if callable(stop_updates):
                try:
                    stop_updates()
                except Exception:
                    pass
            return early_exit

        orders = self.execution_stage.run(ctx)
        return {
            "status": "intraday_executed" if orders else "intraday_no_trades",
            "candidates": symbols, "orders": orders, "run_id": ctx.run_id,
        }

    def run_evening(self) -> dict:
        """The evening session, plus the durable record of its own output.

        The body below is unchanged; this wrapper exists so that EVERY
        return path (holiday short-circuit, paid-analysis suspension, the
        error payloads and the full report) lands in `evening_reports`
        before the result reaches the notifier. Previously the run handed
        stop_coverage_gaps / stop_proximity / earnings_proximity /
        total_pnl / risk_capital_dollars to the Telegram formatter and
        then dropped them: only daily_pnl and insights survived, so last
        night's report could not be re-read without paying for a fresh
        run. Persistence is fail-soft — a storage problem must cost the
        audit record, never the evening push.
        """
        result = self._run_evening_body()
        if isinstance(result, dict) and result.get("status") != "market_holiday":
            screen = self._run_universe_screen(result.get("run_id") or "evening")
            if screen is not None:
                result["universe_screen"] = screen
        self._persist_evening_report(result)
        return result

    def _persist_evening_report(self, result: dict) -> None:
        """Write the evening result dict verbatim, keyed by trading day.

        No field is defaulted or filled in: a value the run could not
        compute is stored absent/None so that a re-render says
        "not available" rather than showing a fabricated zero.
        """
        if not isinstance(result, dict):
            return
        try:
            self.db.save_evening_report(
                date=session_date_key(),
                run_id=result.get("run_id"),
                payload=result,
            )
        except Exception as exc:  # noqa: BLE001 — never break the push
            logger.warning(
                "evening report persistence failed (non-fatal): %s", exc,
            )

    def _run_evening_body(self) -> dict:
        ctx = RunContext.start("evening")
        run_id = ctx.run_id
        logger.info("=== Evening report: %s ===", run_id)

        if not self._is_trading_day():
            logger.info("Evening run skipped: market closed for non-trading day")
            return {"status": "market_holiday", "analysis": None, "run_id": run_id}

        self._activate_cost_session(run_id, "evening")

        # Drain orphaned protection-restore intents — last chance before
        # the trading day ends. If close-session bailed and the SELL has
        # since gone terminal, recover coverage now rather than carrying
        # a naked position overnight. Codex r8 #2.
        self._drain_pending_protection_restores()
        self._drain_pending_repegs()
        self._reconcile_orphan_pending_submits()  # audit F4
        # Broker-truth coverage audit — last check before carrying positions
        # overnight (independent of the WAL).
        coverage_gaps = self._reconcile_stop_coverage()
        # Broker-truth EXIT audit (2026-08-28 ONDS/CCJ) — last chance before
        # the daily P&L snapshot below is computed, so a same-day stop-out
        # is reflected in tonight's report rather than showing up as an
        # unexplained gap the next time someone looks at realized_pnl.
        try:
            self._reconcile_stop_out_fills(run_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("evening stop-out reconcile failed (non-fatal): %s", exc)

        # 1. Record daily PnL — use Alpaca's last_equity (previous trading-day close)
        # as the baseline. This correctly handles weekends/holidays (Alpaca updates
        # last_equity only on trading days) and doesn't depend on whether yesterday's
        # evening run actually persisted a snapshot to our own DB.
        account = self.broker.get_account()
        positions = self.broker.get_positions()
        total_value = account["portfolio_value"]
        last_equity = account.get("last_equity", total_value)
        today_str = session_date_key()  # ET trading-day key — stable across host TZ

        if last_equity > 0:
            daily_pnl = total_value - last_equity
            daily_return_pct = daily_pnl / last_equity * 100
        else:
            daily_pnl = 0.0
            daily_return_pct = 0.0
        ctx.account = account
        ctx.positions = positions
        ctx.total_value = total_value
        ctx.last_equity = last_equity
        ctx.daily_pnl = daily_pnl
        # Sync the full broker book (before the LLM-view split below).
        # Evening's Telegram snapshot reads this table, not the in-memory list.
        self._sync_positions_from_broker(ctx.positions)

        # LLM view: hide the cash-sweep vehicle from evening's position
        # narratives (facts / thesis-health / missed-ops held-set) — parked
        # T-bills have no thesis to review. ctx keeps broker truth.
        sweeper = self._sweeper()
        if sweeper is not None:
            positions, _parked = sweeper.split_positions(positions)

        # Phase 6 (§6.3b): today's P&L expressed against capital actually AT
        # RISK, not just total equity — reuses the same audit §1.3 heat
        # calculation (`_build_portfolio_heat` -> `src.risk.metrics.
        # portfolio_heat`) the risk-manager prompt already trusts, rather
        # than recomputing it. None (not 0.0) on a failed build, so the
        # notifier can say "unknown" instead of a fabricated number.
        try:
            risk_heat = self._build_portfolio_heat(positions, total_value)
            risk_capital_dollars = (
                risk_heat.budget_risk_dollars if risk_heat is not None else None
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("evening: risk-capital heat build failed: %s", e)
            risk_capital_dollars = None

        # Sweep submitted orders before building the evening prompt so
        # canceled/expired orders do not get narrated as real trades, and
        # partial terminal fills are reflected in the trade list.
        self._reconcile_fills()

        # Phase 4 #5: daily_pnl write is deferred to the atomic
        # save_evening_snapshot() below, along with insights. Doing both in
        # one transaction means a crash between them doesn't leave next
        # morning reading a P&L number with no insights narrative attached.
        # Fallback: if the evening LLM fails (analysis is None), we still
        # save the daily_pnl alone below to preserve the P&L audit trail.

        # This boundary is intentionally after broker protection/fill
        # reconciliation and the deterministic P&L snapshot, but before the
        # first paid news/model request. A latched breaker still persists the
        # P&L audit row and returns a truthful suspended status.
        try:
            self._require_paid_analysis("evening_news")
        except PaidAnalysisSuspended as exc:
            equity_close = None
            try:
                closes = self.broker.get_recent_daily_closes(lookback_days=10)
                if closes and closes[-1][0] == today_str:
                    equity_close = closes[-1][1]
            except Exception as close_exc:  # noqa: BLE001
                logger.warning("suspended evening: 4pm close fetch failed: %s", close_exc)
            self.db.insert_daily_pnl(
                date=today_str,
                total_value=total_value,
                daily_pnl=daily_pnl,
                daily_return_pct=daily_return_pct,
                equity_close=equity_close,
            )
            payload = self._paid_suspended_payload(run_id, error=exc)
            payload.update(
                analysis=None,
                total_value=total_value,
                daily_pnl=daily_pnl,
                daily_return_pct=daily_return_pct,
                equity_close=equity_close,
                stop_coverage_gaps=coverage_gaps,
            )
            return payload

        # 2. News + Earnings update — capture end-of-day developments
        try:
            # Same held-symbols-only scope as run_position_review — see the
            # comment there. No separate candidate list exists pre-fetch in
            # this path. `positions` here was already sweeper-split above,
            # so _news_held_symbols' own split is a no-op; called anyway to
            # keep this call site identical to the other two.
            evening_news, evening_news_coverage = self._run_news_update(
                run_id, session="evening",
                held_symbols=self._news_held_symbols(positions),
            )
        except PaidAnalysisSuspended as exc:
            self.db.insert_daily_pnl(
                date=today_str, total_value=total_value,
                daily_pnl=daily_pnl, daily_return_pct=daily_return_pct,
            )
            payload = self._paid_suspended_payload(run_id, error=exc)
            payload.update(
                analysis=None, total_value=total_value, daily_pnl=daily_pnl,
                daily_return_pct=daily_return_pct,
                stop_coverage_gaps=coverage_gaps,
            )
            return payload
        if evening_news_coverage is not None and evening_news_coverage.status != "ok":
            # Same gap noted in run_position_review: evening has no
            # data_status mechanism of its own to carry this further, so at
            # minimum it does not disappear into a log-only "ok".
            logger.warning("evening: %s", evening_news_coverage.describe())
        if evening_news:
            logger.info("Evening news: %s", evening_news.pm_briefing[:200])
        try:
            _, evening_earnings = self._load_earnings_analyses(run_id, session="evening", ctx=ctx)
        except Exception as e:  # noqa: BLE001 — evening proceeds without earnings
            logger.error("evening: earnings load failed (continuing without): %s", e)
            evening_earnings = []

        try:
            self._require_paid_analysis("evening_analyst")
        except PaidAnalysisSuspended as exc:
            self.db.insert_daily_pnl(
                date=today_str, total_value=total_value,
                daily_pnl=daily_pnl, daily_return_pct=daily_return_pct,
            )
            payload = self._paid_suspended_payload(run_id, error=exc)
            payload.update(
                analysis=None, total_value=total_value, daily_pnl=daily_pnl,
                daily_return_pct=daily_return_pct,
                stop_coverage_gaps=coverage_gaps,
            )
            return payload

        # 3. LLM evening analysis — daily review and tomorrow outlook
        macro_summary = self.macro.get_macro_summary()
        evening_macro_coverage = self.macro.last_coverage
        if isinstance(evening_macro_coverage, MacroCoverage) and evening_macro_coverage.status != "ok":
            # Same gap noted in run_position_review / the news coverage
            # check above: evening has no data_status mechanism of its own
            # to carry this further, so at minimum it does not disappear
            # into a log-only "ok".
            logger.warning("evening: %s", evening_macro_coverage.describe())
        # Sweep churn (SWEEP_BUY/SWEEP_SELL) is cash parking, not a trading
        # decision — narrating it to the evening analyst would feed the
        # learning loops noise (review finding). Fetch extra rows so the
        # filter doesn't shrink the real-trade view.
        today_trades = [
            self._actualize_trade_row(t)
            for t in self.db.get_trades(limit=30, today_only=True, executed_only=True)
            if (t.get("action") or "") not in ("SWEEP_BUY", "SWEEP_SELL")
        ][:20]
        # Feed yesterday's insights back so evening can grade its own prior outlook
        # against today's reality — enables calibration over time.
        prior_outlook = self.db.get_latest_insights(before_date=today_str)
        # SELL decisions from the last 2 days + each symbol's move since sell.
        # Evening grades each one {correct|premature|wrong} — the feedback loop
        # on selling discipline.
        recent_sells = self._build_recent_sells_for_grading(
            lookback_days=2,
            symbols_bars=ctx.symbols_bars,  # empty for evening (no tech fetch) — OK, we use broker price
        )
        # v2: mirror SELL grading with BUY grading. Entry quality feedback loop.
        recent_buys = self._build_recent_buys_for_grading(
            lookback_days=5, symbols_bars=ctx.symbols_bars,
        )
        # v2: meta-calibration — evening sees its own recent tomorrow_bias vs
        # actual outcomes so it can detect "I've been too bullish 7/10 days".
        outlook_calibration = self._build_recent_outlook_calibration(lookback=10)
        # v2: share the PM's 7-day narrative + 14-day active state-change
        # memory so evening doesn't drift from or repeat its own previous
        # language unchecked.
        weekly_narrative = self._build_weekly_narrative()
        active_state_changes = self._build_active_state_changes()

        # Phase-1 evening-upgrade: deterministic "what did we miss" digest.
        # Python pre-computes the signal-state context so the LLM's classification
        # has to cite observable evidence rather than retro-rationalize price.
        held_set = {p.symbol for p in positions}
        try:
            missed_ops_snapshots = self._build_missed_opportunities_digest(
                lookback_days=5, move_threshold_pct=8.0, top_n=15,
                current_position_symbols=held_set,
            )
        except Exception as e:
            logger.warning("missed_ops digest failed (proceeding without it): %s", e)
            missed_ops_snapshots = []

        # Value-lens upgrade (2026-04): per-position 8-week fundamentals
        # evolution — feeds the new thesis_health_review reasoning step.
        try:
            thesis_health_context = self._build_thesis_health_context(positions)
        except Exception as e:
            logger.warning(
                "thesis_health_context failed (proceeding without it): %s", e,
            )
            thesis_health_context = {}

        # Replay/shadow mechanism (2026-04 — P2 follow-up): persist the
        # full evening-analyst input set so a candidate prompt can be
        # re-scored on the same frozen inputs later via
        # `scripts/replay_evening.py`. Doesn't affect the live run;
        # failure here is non-fatal and only logged.
        try:
            self._persist_evening_replay_inputs(
                date_iso=today_str,
                run_id=run_id,
                positions=positions,
                macro_summary=macro_summary,
                total_value=total_value,
                daily_pnl=daily_pnl,
                daily_return_pct=daily_return_pct,
                today_trades=today_trades,
                prior_outlook=prior_outlook,
                recent_sells=recent_sells,
                recent_buys=recent_buys,
                news_intel=evening_news,
                earnings_analyses=evening_earnings,
                weekly_narrative=weekly_narrative,
                active_state_changes=active_state_changes,
                outlook_calibration=outlook_calibration,
                missed_ops_snapshots=missed_ops_snapshots,
                thesis_health_context=thesis_health_context,
            )
        except Exception as e:
            logger.warning("evening replay input persistence failed: %s", e)

        analysis = None
        analysis_error = False
        try:
            analysis, ev_result = self.evening_analyst.analyze(
                positions=positions,
                macro_summary=macro_summary,
                total_value=total_value,
                daily_pnl=daily_pnl,
                daily_return_pct=daily_return_pct,
                today_trades=today_trades,
                prior_outlook=prior_outlook,
                recent_sells=recent_sells,
                recent_buys=recent_buys,
                news_intel=evening_news,
                earnings_analyses=evening_earnings,
                weekly_narrative=weekly_narrative,
                active_state_changes=active_state_changes,
                outlook_calibration=outlook_calibration,
                missed_ops_snapshots=missed_ops_snapshots,
                thesis_health_context=thesis_health_context,
            )
        except PaidAnalysisSuspended as exc:
            self.db.insert_daily_pnl(
                date=today_str, total_value=total_value,
                daily_pnl=daily_pnl, daily_return_pct=daily_return_pct,
            )
            payload = self._paid_suspended_payload(run_id, error=exc)
            payload.update(
                analysis=None, total_value=total_value, daily_pnl=daily_pnl,
                daily_return_pct=daily_return_pct,
                stop_coverage_gaps=coverage_gaps,
            )
            return payload
        except Exception as e:
            from src.agents.base import AgentResult, resolve_provider

            analysis_error = True
            logger.error("Evening analyst failed: %s", e, exc_info=True)
            # No call ever completed, so `actual_provider`/model stay unknown
            # (not fabricated) — but WHAT was requested is known regardless
            # of the exception, so record that much for attribution.
            _requested_model = self.config.llm.evening_analyst_model
            _requested_provider = resolve_provider(
                _requested_model, self.config.llm.evening_analyst_provider,
            )
            ev_result = AgentResult(
                raw_text=f"[exception] {e}",
                tokens_used=0,
                model=self.config.llm.evening_analyst_model,
                user_message="",
                requested_model=_requested_model,
                requested_provider=_requested_provider,
                provider_requests=0,
            )

        _ev_log_kwargs = agent_log_kwargs(ev_result)
        if analysis_error:
            # agent_log_kwargs() derives "fallback"/"success" from
            # used_fallback, which is False here (no call ever completed) —
            # override so a hard failure isn't misreported as a success.
            _ev_log_kwargs["status"] = "failed"
        elif analysis is None:
            _ev_log_kwargs["status"] = "evening_parse_error"
        self.db.insert_agent_log(
            agent_name="evening_analyst", run_id=run_id,
            input_summary=f"${total_value:.0f} total, PnL ${daily_pnl:.2f}",
            input_message=ev_result.user_message,
            output_summary=(
                analysis.daily_summary
                if analysis
                else ("analysis_error" if analysis_error else "parse_error")
            ),
            full_response=ev_result.raw_text,
            model=ev_result.model,
            tokens_used=ev_result.tokens_used,
            input_tokens=ev_result.input_tokens,
            output_tokens=ev_result.output_tokens,
            cost_usd=ev_result.cost_usd,
            **_ev_log_kwargs,
        )

        # True close-to-close ("4pm-to-4pm") P&L. account.last_equity is the
        # PRIOR day's close (stale at the 20:00 ET evening run), and
        # total_value here is the 8pm after-hours value — neither gives today's
        # official 4pm close. Alpaca portfolio_history (extended_hours=False)
        # does: its latest 1D point is today's regular-session close. We report
        # the clean close-to-close P&L when available and store today's close
        # for the audit trail; on any gap we fall back to the real-time diff.
        equity_close = None
        pnl_4pm = None
        pnl_4pm_pct = None
        try:
            closes = self.broker.get_recent_daily_closes(lookback_days=10)
            if closes and closes[-1][0] == today_str:
                equity_close = closes[-1][1]
                prev_close = closes[-2][1] if len(closes) >= 2 else None
                # Guard > 0: a negative prior close (corrupted data / underwater
                # account) would flip the sign of the return %; leave pnl_4pm
                # None so the headline falls back to the real-time path.
                if prev_close and prev_close > 0:
                    pnl_4pm = equity_close - prev_close
                    pnl_4pm_pct = pnl_4pm / prev_close * 100
            elif closes:
                logger.info(
                    "4pm snapshot: portfolio_history latest date %s != today %s "
                    "(API lag?) — evening uses the real-time P&L fallback",
                    closes[-1][0], today_str,
                )
            # Self-heal: when portfolio_history is a day behind at the
            # 20:00 ET evening run (the "API lag?" branch above), that
            # evening's equity_close landed NULL — but by a LATER evening
            # the API has caught up on those dates, which are still inside
            # this lookback window. Backfill any still-NULL rows now.
            # today_str is excluded because today's row is owned by the
            # branches above + save_evening_snapshot below: when today's
            # bar is present the first branch already uses it as the
            # official close, and when it's absent there is nothing to
            # backfill yet.
            for d, close_val in closes:
                if d == today_str:
                    continue
                # Mirror the `prev_close > 0` guard above: Alpaca
                # portfolio_history can emit 0.0 (pre-funding / account
                # reset) or non-finite points, and a backfilled value is
                # permanent (the fill targets NULL-only rows, so a bad
                # write can never be corrected by a later run) — never
                # freeze a corrupt equity in. NaN must be caught here
                # anyway: sqlite binds it as NULL, which would make
                # backfill report success while storing nothing.
                if not (math.isfinite(close_val) and close_val > 0):
                    logger.warning(
                        "equity_close backfill skipped for %s: suspect "
                        "equity value %r", d, close_val,
                    )
                    continue
                try:
                    if self.db.backfill_equity_close(d, close_val):
                        logger.info(
                            "equity_close backfilled for %s = %.2f (API lag self-heal)",
                            d, close_val,
                        )
                except Exception as exc:
                    logger.warning("equity_close backfill failed for %s: %s", d, exc)
        except Exception as e:
            logger.warning("4pm snapshot fetch failed: %s — using real-time P&L", e)

        # Save daily_pnl + insights atomically (Phase 4 #5). If the LLM
        # failed (analysis is None), still record the P&L number so the
        # audit trail is complete — just with empty insights fields.
        if analysis:
            self.db.save_evening_snapshot(
                date=today_str,
                total_value=total_value, daily_pnl=daily_pnl,
                daily_return_pct=daily_return_pct,
                equity_close=equity_close,
                tomorrow_outlook=analysis.tomorrow_outlook,
                lessons=analysis.lessons,
                suggested_actions=analysis.suggested_actions,
                risk_rating=analysis.risk_rating,
                tomorrow_bias=analysis.tomorrow_bias,
                tomorrow_conviction=analysis.tomorrow_conviction,
                tomorrow_key_risks=analysis.tomorrow_key_risks,
                sell_decisions_assessment=analysis.sell_decisions_assessment,
                # v2: persist structured grades so next-day position_reviewer
                # can aggregate counts into its "lean patient" bias.
                sell_grades=analysis.sell_grades,
                buy_grades=analysis.buy_grades,
                # Phase-1 upgrade: per-day missed opportunities feed PM's L3d
                # memory next morning and the quarterly meta-reflector's
                # theme_coverage_report.
                missed_opportunities=analysis.missed_opportunities,
                # Defect (d) fix: these four were produced by the LLM every
                # night and declared on EveningReport, but had no parameter
                # here — dropped before ever reaching disk.
                # thesis_updates/selection_rules/discipline_notes feed
                # tomorrow's portfolio_manager (see build_user_message).
                thesis_updates=analysis.thesis_updates,
                selection_rules=analysis.selection_rules,
                discipline_notes=analysis.discipline_notes,
                previous_outlook_assessment=analysis.previous_outlook_assessment,
            )
        else:
            # LLM failed — keep at least the P&L number for daily audit.
            self.db.insert_daily_pnl(
                date=today_str,
                total_value=total_value,
                daily_pnl=daily_pnl,
                daily_return_pct=daily_return_pct,
                equity_close=equity_close,
            )

        # Conviction ledger (spec §9.5) — score on close. Every position
        # chain that went flat today is credited to the seats that took a
        # side on it: aligned with the direction taken scores +R, opposed
        # scores -R, weighted by the conviction that seat declared. Runs
        # HERE, in evening housekeeping, deliberately: it reads closed
        # `trades` rows and writes forensic evidence rows, touches no broker
        # and no open position, and is idempotent (a position already scored
        # is skipped), so it can never influence or delay an execution path.
        # Advisory only — nothing in the trading chain reads what it writes.
        try:
            ledger = self.db.resolve_conviction_ledger()
            if ledger.get("scored_positions"):
                logger.info(
                    "Conviction ledger: scored %d newly closed position(s) into "
                    "%d seat credit(s) (%d already scored, %d unscorable without "
                    "an entry stop, %d with no recorded stances)",
                    ledger["scored_positions"], ledger["credits_written"],
                    ledger["skipped_already_scored"], ledger["skipped_no_r"],
                    ledger["skipped_no_stances"],
                )
        except Exception as e:
            logger.warning("Conviction ledger resolution failed: %s", e)

        # Housekeeping: drop agent_logs older than 2 years (full_response bloats the DB
        # but 730 days supports quarter-over-quarter learning), and trades older than
        # 5 years (keep a long audit tail but bound it).
        try:
            pruned = self.db.prune_agent_logs(keep_days=730)
            if pruned:
                logger.info("Pruned %d old agent_log rows", pruned)
        except Exception as e:
            logger.warning("Agent log prune failed: %s", e)
        try:
            pruned_t = self.db.prune_trades(keep_days=365 * 5)
            if pruned_t:
                logger.info("Pruned %d trades older than 5 years", pruned_t)
        except Exception as e:
            logger.warning("Trades prune failed: %s", e)
        # Stage 4 (QAMC): specialist_evidence is forensic display detail for
        # the same agent calls agent_logs already prunes — same 730-day
        # retention, same never-block-housekeeping discipline.
        try:
            pruned_se = self.db.prune_specialist_evidence(keep_days=730)
            if pruned_se:
                logger.info("Pruned %d old specialist_evidence rows", pruned_se)
        except Exception as e:
            logger.warning("specialist_evidence prune failed: %s", e)
        # Stale orphaned protection-restore rows accumulate when a
        # sell_order_id becomes unqueryable (broker GC) or position
        # gets liquidated by another path. Drain can't make progress on
        # them; 30d cutoff bounds the operational noise.
        try:
            pruned_p = self.db.prune_pending_protection_restores(keep_days=30)
            if pruned_p:
                logger.info("Pruned %d stale pending_protection_restores rows", pruned_p)
        except Exception as e:
            logger.warning("pending_protection_restores prune failed: %s", e)
        try:
            pruned_rp = self.db.prune_pending_repegs(keep_days=30)
            if pruned_rp:
                logger.info("Pruned %d stale pending_repegs rows", pruned_rp)
        except Exception as e:
            logger.warning("pending_repegs prune failed: %s", e)
        # File-store housekeeping: the news dated dirs + narrative backups grow
        # unbounded (the DB side prunes; the file-stores didn't). Nothing reads
        # news artifacts older than ~14 days, so 1000d is very safe headroom.
        try:
            pruned_n = self.news_store.prune(keep_days=1000)
            if pruned_n:
                logger.info("Pruned %d dated news artifact(s)", pruned_n)
        except Exception as e:
            logger.warning("news file-store prune failed: %s", e)
        try:
            pruned_e = self.earnings_provider.prune(keep_days=1000)
            if pruned_e:
                logger.info("Pruned %d old raw earnings filing(s)", pruned_e)
        except Exception as e:
            logger.warning("earnings file-store prune failed: %s", e)

        logger.info("Evening: value=$%.2f, PnL=$%.2f (%.2f%%), risk=%s",
                     total_value, daily_pnl, daily_return_pct,
                     analysis.risk_rating if analysis else "error")
        if analysis:
            logger.info("Summary: %s", analysis.daily_summary)
            logger.info("Tomorrow: %s", analysis.tomorrow_outlook)
        # Evening is the last chance to reconcile today's orders before the
        # next trading day. Sweep everything still marked submitted.
        self._reconcile_fills()
        self._sync_positions_from_broker()

        meta_result = self._maybe_run_quarterly_meta()
        missing_sessions = self._expected_sessions_missing_today()
        # Owner-facing evening report (2026-09-18): today's P&L alone never
        # answered "am I up since the desk restarted". The same
        # `_total_pnl_since_reset` the trader-feed messages already use is
        # read here so the evening message can lead with BOTH figures on the
        # identical basis, rather than computing a second "total" of its own.
        total_pnl, total_return_pct, total_pnl_since = (
            self._total_pnl_since_reset(total_value)
        )
        if missing_sessions:
            logger.warning(
                "Dead-man's check: expected session(s) left no agent_logs "
                "today: %s", ", ".join(missing_sessions),
            )
        return {
            "status": (
                "analyzed" if analysis is not None else
                ("evening_analysis_error" if analysis_error else "evening_parse_error")
            ),
            "total_value": total_value,
            "daily_pnl": daily_pnl,
            "daily_return_pct": daily_return_pct,
            "analysis": analysis.model_dump() if analysis else None,
            "run_id": run_id,
            "auto_meta": meta_result,
            # Observability: surface a silently-missing session + the loss cap
            # so the notifier can raise deterministic escalation (not just LLM).
            "missing_sessions": missing_sessions,
            # docs/WORK.md item 32: the limit ACTUALLY IN FORCE, read off
            # the engine rather than the config, so the operator alert shows
            # the volatility-relative threshold when one is measurable and
            # the fixed-percentage fallback when it is not. Reading the
            # config field here would have quietly reported the fallback
            # every day regardless.
            "max_daily_loss_pct": _daily_loss_limit_for_alert(
                getattr(self, "risk_engine", None),
                getattr(self.config, "risk", None),
            ),
            "stop_coverage_gaps": coverage_gaps,
            # True 4pm-to-4pm headline P&L (None → notifier falls back to the
            # real-time total_value/daily_pnl figures).
            "equity_close": equity_close,
            "pnl_4pm": pnl_4pm,
            "pnl_4pm_pct": pnl_4pm_pct,
            # Phase 6 (§6.3b) — capital actually at risk (sum of
            # (entry-stop) x shares across open positions), for the
            # notifier's "P&L vs risk capital" line. None on a failed heat
            # build; 0.0 for a genuinely flat/fully-released book — the
            # notifier tells those two apart.
            "risk_capital_dollars": risk_capital_dollars,
            # Dated total P&L (see `_total_pnl_since_reset` for why it is
            # dated rather than called "since inception").
            "total_pnl": total_pnl,
            "total_return_pct": total_return_pct,
            "total_pnl_since": total_pnl_since,
            # Two end-of-day facts the desk knew and never told the owner:
            # which holdings sit within one ordinary day's move of their stop,
            # and which report earnings imminently. Both fail soft to [].
            "stop_proximity": self._evening_stop_proximity(positions),
            "earnings_proximity": self._evening_earnings_proximity(positions),
        }

    def _evening_stop_proximity(self, positions) -> list[dict]:
        """Held positions whose live stop is less than one ordinary day's
        move away — the evening report's "close to its stop" line.

        "Close" is read off the instrument, never picked: the yardstick is
        the symbol's own ATR(14) (`_atr_for_symbol`, the same measure the
        trailing-stop noise band uses). A position is listed when the gap
        between the last price and the live broker stop is smaller than one
        ATR, i.e. a single ordinary session could reach it. No percentage
        threshold is invented anywhere in this method.

        A symbol whose stop or ATR cannot be read is returned with
        ``status='unknown'`` rather than dropped: silently omitting it would
        render as "nothing is near its stop", which is not what was
        measured. Never raises — any failure degrades to [].
        """
        rows: list[dict] = []
        try:
            park = (self._sweep_symbol() or "").strip().upper()
            for p in positions or ():
                symbol = str(getattr(p, "symbol", "") or "").strip().upper()
                if not symbol or (park and symbol == park):
                    continue
                try:
                    qty = float(getattr(p, "qty", 0) or 0)
                    price = float(getattr(p, "current_price", 0) or 0)
                except (TypeError, ValueError):
                    continue
                if qty == 0 or not (math.isfinite(price) and price > 0):
                    continue
                try:
                    stop = self.broker.get_current_stop_price(symbol)
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "evening stop-proximity: stop read failed for %s: %s",
                        symbol, exc,
                    )
                    stop = None
                atr = self._atr_for_symbol(symbol)
                if stop is None or atr is None or not (stop > 0):
                    rows.append({"symbol": symbol, "status": "unknown"})
                    continue
                # A long is stopped from BELOW, a short from ABOVE. The
                # distance is the same arithmetic either way.
                gap = (price - stop) if qty > 0 else (stop - price)
                if gap < 0:
                    # PRICE IS THROUGH THE STOP and the broker order is
                    # still open, so it has not filled. This used to be
                    # clamped to 0.0 and reported as `status='near'`, which
                    # merged two different facts into one row: a stop that
                    # is merely TIGHT (an ordinary session could reach it)
                    # and a stop that has already been BLOWN THROUGH without
                    # filling (nothing is standing watch over those shares).
                    # The second is the state the whole stop-limit buffer
                    # trade-off produces on a gap, and it now reads as
                    # itself. The distance is reported as a positive number
                    # of dollars PAST the trigger, which is a different
                    # quantity from `gap` and carries a different name.
                    rows.append({
                        "symbol": symbol, "status": "through", "price": price,
                        "stop": float(stop), "through": -gap,
                        "atr": float(atr),
                    })
                    continue
                if gap < atr:
                    rows.append({
                        "symbol": symbol, "status": "near", "price": price,
                        "stop": float(stop), "gap": gap, "atr": float(atr),
                    })
        except Exception as exc:  # noqa: BLE001 — never break the evening push
            logger.warning("evening stop-proximity sweep failed: %s", exc)
            return []
        return rows

    def _evening_earnings_proximity(self, positions) -> list[dict]:
        """Next-earnings proximity for every held name, for the evening
        report's "reports earnings soon" line.

        Reuses `src.data.event_calendar.fetch_earnings_proximity` — already
        bounded per symbol and in aggregate by the same `config.event_risk`
        timeouts the morning research stage uses — rather than calling the
        unbounded provider method directly. A symbol whose date could not be
        fetched comes back labelled, never as "no earnings".

        Never raises; degrades to [].
        """
        try:
            symbols = self._news_held_symbols(positions)
            if not symbols or getattr(self, "market", None) is None:
                return []
            from src.data.event_calendar import fetch_earnings_proximity
            event_cfg = getattr(getattr(self, "config", None), "event_risk", None)
            rows = fetch_earnings_proximity(
                self.market, symbols,
                per_symbol_timeout_s=getattr(
                    event_cfg, "earnings_symbol_timeout_s", 8.0,
                ),
                total_deadline_s=getattr(event_cfg, "earnings_deadline_s", 20.0),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("evening earnings-proximity sweep failed: %s", exc)
            return []
        return [
            {
                "symbol": r.symbol,
                "sessions_away": r.sessions_away,
                "status": r.status,
            }
            for r in rows or []
        ]

    def _expected_sessions_missing_today(self) -> list[str]:
        """Best-effort internal dead-man's check: on a trading day, which of
        the market-day sessions that should have run by evening left NO
        agent_logs rows? Catches a session that silently never fired — the one
        failure mode push-on-completion observability structurally cannot see
        (a disabled timer, a stuck lock, ET-window math wrong on a half-day).

        Run from evening, which is already gated on `_is_trading_day`, so this
        never false-fires on a holiday. Does NOT cover total host death or
        evening itself not firing — that needs an EXTERNAL dead-man's switch
        (e.g. a healthchecks.io ping the wrapper hits on success). Best-effort:
        any failure returns [] so it can never break the evening push.
        """
        try:
            present = self.db.session_prefixes_logged_on()
        except Exception as exc:  # noqa: BLE001
            logger.warning("missing-session check: agent_logs read failed: %s", exc)
            return []
        # run_id prefix -> display name; morning's prefix is 'run'.
        expected = {"run": "morning", "midday": "midday", "close": "close"}
        missing = [name for prefix, name in expected.items() if prefix not in present]

        # RC5 (2026-07-16): "any run- row exists" cannot tell a completed
        # morning from one killed mid-flight — research rows land BEFORE the
        # kill, so 13 straight days of morning deaths passed this check and
        # the 🔴 banner never fired. Two sharper probes:
        if "morning" not in missing and "run" in present:
            # A legit PM-less completion (no_data / daily_loss_halted)
            # records a status marker — skip both probes for it.
            try:
                from src import decision_checkpoint as _dc0
                legit_early_exit = _dc0.read_status("morning") is not None
            except Exception:  # noqa: BLE001
                legit_early_exit = False
            #  (a) research logged but the PM never ran → died during research.
            try:
                agents = self.db.agent_names_logged_on("run-")
                if (not legit_early_exit and agents
                        and "portfolio_manager" not in agents):
                    missing.append("morning (research ran, PM never did — killed mid-run?)")
            except Exception as exc:  # noqa: BLE001
                logger.warning("missing-session check: agent probe failed: %s", exc)
            #  (b) PM plan checkpointed but never consumed → killed before the
            #      RiskStage reviewed it (the observed 6/30-7/15 death mode).
            try:
                import json as _json
                from src import decision_checkpoint as _dc
                p = _dc.checkpoint_path("morning")
                if p.exists() and _json.loads(p.read_text()).get("consumed") is False:
                    missing.append(
                        "morning (PM plan never risk-reviewed — checkpoint unconsumed)"
                    )
            except Exception as exc:  # noqa: BLE001
                logger.warning("missing-session check: checkpoint probe failed: %s", exc)
        return missing

    def _maybe_run_quarterly_meta(self) -> dict | None:
        """Evening-time piggyback for the quarterly meta-reflection loop.

        There is no separate systemd timer for `meta`; the autonomous-
        evolution loop fires by checking the quarter-end gate inside
        evening. The pre-fix behavior was that `run_quarterly_meta_
        reflection()` had to be invoked by hand (`python main.py --mode
        meta`), so the entire 8-week-built loop never ran automatically.

        Wrapped in try/except so a meta failure can never fail the
        evening report. Evening's artifact is load-bearing for next
        morning's PM; meta is a once-a-quarter bonus.

        Returns None when not quarter-end, a result dict otherwise.
        """
        try:
            from src.trading_calendar import et_today
            today = et_today()
            try:
                is_last = self.broker.is_last_trading_day_of_quarter(on_date=today)
            except Exception as e:
                logger.warning("Evening: meta quarter-end check failed: %s", e)
                return None
            if not is_last:
                return None
            logger.info(
                "Evening: today is last trading day of quarter %d-Q%d — "
                "running auto meta-reflection",
                today.year, (today.month - 1) // 3 + 1,
            )
            return self.run_quarterly_meta_reflection(force=False)
        except Exception as e:
            logger.exception("Evening: meta-reflection piggyback failed: %s", e)
            return {"status": "auto_meta_error", "error": str(e)}

    def run_quarterly_meta_reflection(
        self,
        *,
        force: bool = False,
        period_end=None,
        lookback_days: int = 90,
        evolution_root: str = "data/evolution",
        prompts_dir: str | Path | None = None,
    ) -> dict:
        """Build the quarterly digest, run the meta-reflector, persist both.

        Cadence: normally this is a NOP unless today is the last trading day
        of the current quarter (`broker.is_last_trading_day_of_quarter`).
        Pass `force=True` to override — used by CLI `--mode meta --force`
        for ad-hoc runs and by tests.

        Output always includes `digest_path` (persisted) and, when the LLM
        succeeded, `reflection_path`. PR3 intentionally stops here — it
        does NOT edit any prompt files. PR4 will pick up reflection.json
        from disk and apply proposed_learnings through prompt_editor.
        """
        from src.evolution.quarterly_digest import (
            build_quarterly_digest,
            load_previous_digest,
            persist_digest,
        )
        from src.agents.meta_reflector import (
            load_previous_reflection,
            persist_reflection,
        )

        today = period_end or et_today()
        if not force:
            try:
                is_last = self.broker.is_last_trading_day_of_quarter(on_date=today)
            except Exception as exc:
                logger.warning(
                    "meta reflection skipped: quarter-end check failed (%s); "
                    "pass --force to override", exc,
                )
                return {"status": "skipped", "reason": "quarter_end_check_failed"}
            if not is_last:
                logger.info(
                    "meta reflection skipped: %s is not the last trading "
                    "day of the quarter. Pass --force to run anyway.",
                    today,
                )
                return {"status": "skipped", "reason": "not_quarter_end"}

        logger.info("=== Quarterly meta-reflection: %s ===", today)

        # 1. Build digest — deterministic facts layer.
        prev_digest = load_previous_digest(today, root_dir=evolution_root)
        digest = build_quarterly_digest(
            self.db, self.market,
            period_end=today, lookback_days=lookback_days,
            prev_digest=prev_digest,
            prompts_dir=prompts_dir,
        )
        digest_path = persist_digest(digest, root_dir=evolution_root)
        logger.info(
            "Quarterly digest built for %s: alpha=%s, total_real_misses=%s, "
            "total_wrong_buys=%s",
            digest["period"],
            (digest.get("period_performance") or {}).get("alpha_vs_spy_pct"),
            (digest.get("missed_themes") or {}).get("total_real_misses"),
            (digest.get("loss_patterns") or {}).get("total_wrong_buys"),
        )

        # Every invocation is a distinct paid session.  The period remains in
        # the artifacts/result, while a UUID suffix prevents forced reruns of
        # the same quarter from reusing SQLite counters under the run_id PK.
        meta_run_id = f"meta-{digest['period']}-{uuid.uuid4().hex[:8]}"
        self._activate_cost_session(meta_run_id, "meta")
        try:
            self._require_paid_analysis("meta_reflector")
        except PaidAnalysisSuspended as exc:
            payload = self._paid_suspended_payload(meta_run_id, error=exc)
            payload.update(
                period=digest["period"], digest_path=str(digest_path),
                reflection_path=None, reflection=None,
            )
            return payload

        # 2. Meta-reflector LLM — observe-only in PR3 (no prompt edits).
        # analyze() can raise on provider/network failures after retries. The
        # digest has already been persisted so we must degrade to the
        # digest_only path rather than let the exception abort the run
        # (operators lose the audit / status payload otherwise).
        prev_reflection = load_previous_reflection(today, root_dir=evolution_root)
        reflection = None
        ev_result = None
        try:
            reflection, ev_result = self.meta_reflector.analyze(
                digest=digest, prev_reflection=prev_reflection,
            )
        except PaidAnalysisSuspended as exc:
            payload = self._paid_suspended_payload(meta_run_id, error=exc)
            payload.update(
                period=digest["period"], digest_path=str(digest_path),
                reflection_path=None, reflection=None,
            )
            return payload
        except Exception as exc:
            logger.error(
                "meta_reflector.analyze raised; falling back to digest_only: %s",
                exc, exc_info=True,
            )

        # Always log the agent's raw output for audit, even on failure.
        if ev_result is not None:
            try:
                self.db.insert_agent_log(
                    agent_name="meta_reflector",
                    run_id=meta_run_id,
                    input_summary=(
                        f"{digest['period']} · "
                        f"alpha={(digest.get('period_performance') or {}).get('alpha_vs_spy_pct')}"
                    ),
                    input_message=ev_result.user_message,
                    output_summary=(
                        reflection.style_self_portrait[:200]
                        if reflection else "parse_error"
                    ),
                    full_response=ev_result.raw_text,
                    model=ev_result.model,
                    tokens_used=ev_result.tokens_used,
                    input_tokens=ev_result.input_tokens,
                    output_tokens=ev_result.output_tokens,
                    cost_usd=ev_result.cost_usd,
                    **agent_log_kwargs(ev_result),
                )
            except Exception as exc:
                logger.warning("meta_reflector agent_log insert failed: %s", exc)

        if reflection is None:
            logger.error("Meta-reflector returned no valid reflection; "
                         "digest persisted, reflection missing.")
            return {
                "status": "digest_only",
                "run_id": meta_run_id,
                "period": digest["period"],
                "digest_path": str(digest_path),
                "reflection_path": None,
                "reflection": None,
            }

        reflection_path = persist_reflection(reflection, root_dir=evolution_root)
        logger.info(
            "Quarterly meta-reflection complete: %s · %d proposed learnings",
            digest["period"], len(reflection.proposed_learnings),
        )

        # 3. Prompt editor — only runs when evolution.enabled. When off
        # (default until a deployment has reviewed a quarter or two of
        # reflection.json contents by hand), we return without touching any
        # prompt file. The editor itself short-circuits to a full-rejection
        # report; we still persist the attempt log for audit continuity.
        editor_report: dict | None = None
        try:
            from src.config import EvolutionConfig
            evolution_cfg = getattr(self.config, "evolution", None)
            if evolution_cfg is None:
                evolution_cfg = EvolutionConfig()
        except Exception:
            from src.config import EvolutionConfig
            evolution_cfg = EvolutionConfig()

        try:
            from src.evolution.prompt_editor import PromptEditor
            resolved_prompts_dir = (
                Path(prompts_dir) if prompts_dir is not None
                else Path(__file__).resolve().parent.parent / "config" / "prompts"
            )
            editor = PromptEditor(
                config=evolution_cfg,
                prompts_dir=resolved_prompts_dir,
                evolution_dir=evolution_root,
            )
            result_obj = editor.apply_reflection(reflection)
            editor_report = result_obj.to_dict()
            if result_obj.applied:
                logger.info(
                    "Prompt editor applied %d learning(s) across %d agent(s); "
                    "git_commit=%s",
                    len(result_obj.applied),
                    result_obj.agents_edited,
                    result_obj.git_commit,
                )
            elif result_obj.rejected:
                # Most common: evolution.enabled=false (observe-only). Log
                # at INFO so operators see why nothing was applied.
                logger.info(
                    "Prompt editor did not apply any learnings (%d rejected). "
                    "First reason: %s",
                    len(result_obj.rejected), result_obj.rejected[0].reason,
                )
        except Exception as exc:
            logger.error("Prompt editor invocation failed: %s", exc, exc_info=True)

        return {
            "status": "reflected",
            "run_id": meta_run_id,
            "period": digest["period"],
            "digest_path": str(digest_path),
            "reflection_path": str(reflection_path),
            "reflection": reflection.model_dump(),
            "proposed_learnings_count": len(reflection.proposed_learnings),
            "editor_report": editor_report,
        }

    def run_daily(self) -> dict:
        """Fetch full portfolio history from Alpaca, build a CSV, and send
        via Telegram. No LLM calls — pure data export. Runs on weekdays.

        Returns {"status": "sent", "rows": N, "filename": ...} on delivery,
        {"status": "skipped", ...} when Telegram is disabled (CSV built but no
        sink), {"status": "error", ...} on a real failure. The status must be
        honest: previously it reported "sent" even when the upload failed or
        the notifier was disabled, so the operator couldn't tell a delivered
        export from a silently-dropped one.
        """
        from src.notifier import build_daily_csv, TelegramNotifier
        from src.trading_calendar import et_today
        try:
            closes = self.broker.get_full_portfolio_history()
            if not closes:
                logger.warning("run_daily: no portfolio history returned")
                return {"status": "error", "error": "no data from portfolio_history"}
            csv_bytes = build_daily_csv(closes)
            date_str = et_today().strftime("%Y-%m-%d")
            filename = f"pnl_history_{date_str}.csv"
            caption = f"📊 P&L History export — {date_str} ({len(closes)} trading days)"
            notifier = TelegramNotifier()
            delivered = notifier.send_document(csv_bytes, filename, caption)
            base = {"rows": len(closes), "filename": filename}
            if delivered:
                logger.info("run_daily: sent %d rows as %s", len(closes), filename)
                return {"status": "sent", **base}
            if not notifier.enabled:
                # CSV built fine; Telegram simply isn't configured — not a
                # failure, just nowhere to deliver it.
                logger.info(
                    "run_daily: built %d-row CSV %s but Telegram is disabled",
                    len(closes), filename,
                )
                return {"status": "skipped", **base}
            # Enabled but the upload failed (network / API / rate limit).
            logger.error("run_daily: Telegram delivery failed for %s", filename)
            return {"status": "error", "error": "telegram delivery failed", **base}
        except Exception as exc:
            logger.error("run_daily failed: %s", exc, exc_info=True)
            return {"status": "error", "error": str(exc)}
