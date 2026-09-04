import contextlib
import json as _json
import logging
import math
import re
import uuid
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
from src.data.smart_money import SECForm4Provider
from src.data.earnings import EarningsDataProvider
from src.risk.constants import REWARD_RISK_FLOOR
from src.risk.metrics import unrealized_pnl_pct
from src.risk.rules import (
    GROSS_LADDER,
    GrossCeiling,
    RiskRuleEngine,
    apply_gross_ceiling,
    distance_to_forced_liquidation_pct,
    gross_exposure,
    peak_to_trough_pct,
    position_weight_pct,
    resolve_gross_ceiling,
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
    # D9 (Stage 3, shorts). Hard blocks on opening/adding a short; a COVER
    # is exempted before either rule can fire (src/risk/rules.py).
    "max_single_short_pct",
    # Renamed from max_short_gross_pct (2026-08-30) — now hard blocks any
    # BEARISH order (a SHORT of an ordinary name, or a BUY of an inverse
    # ETF SH/SDS/PSQ/SQQQ), not only `action == "SHORT"`. A SHORT of an
    # inverse ETF is a BULLISH bet and is correctly excluded
    # (src/risk/rules.py).
    "max_gross_bearish_pct",
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
# shock, a correlation breach, a thesis invalidation. Price movement alone is
# not new information. This tuple is that list, expressed as prose the LLM
# actually emits.
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
    "correlation breach",
    "correlation cluster breach",
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

        self.tech_analyst = TechAnalystAgent(
            api_key=_key_for(config.llm.tech_analyst_model, config.llm.tech_analyst_provider),
            model=config.llm.tech_analyst_model,
            max_tokens=config.llm.get_max_tokens("tech_analyst"),
            fallback_api_key=_fallback_api_key,
            fallback_provider=config.llm.fallback_provider,
            fallback_model=config.llm.fallback_model,
            provider=config.llm.tech_analyst_provider,
            provider_order=config.llm.get_provider_order("tech_analyst"),
        )
        self.portfolio_manager = PortfolioManagerAgent(
            api_key=_key_for(config.llm.portfolio_manager_model, config.llm.portfolio_manager_provider),
            model=config.llm.portfolio_manager_model,
            max_tokens=config.llm.get_max_tokens("portfolio_manager"),
            fallback_api_key=_fallback_api_key,
            fallback_provider=config.llm.fallback_provider,
            fallback_model=config.llm.fallback_model,
            provider=config.llm.portfolio_manager_provider,
            provider_order=config.llm.get_provider_order("portfolio_manager"),
        )
        self.risk_manager = RiskManagerAgent(
            api_key=_key_for(config.llm.risk_manager_model, config.llm.risk_manager_provider),
            model=config.llm.risk_manager_model,
            max_tokens=config.llm.get_max_tokens("risk_manager"),
            fallback_api_key=_fallback_api_key,
            fallback_provider=config.llm.fallback_provider,
            fallback_model=config.llm.fallback_model,
            provider=config.llm.risk_manager_provider,
            provider_order=config.llm.get_provider_order("risk_manager"),
        )
        self.risk_engine = RiskRuleEngine(RiskConfig(
            max_position_pct=config.risk.max_position_pct,
            max_total_position_pct=config.risk.max_total_position_pct,
            max_daily_loss_pct=config.risk.max_daily_loss_pct,
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
        ))
        self.position_reviewer = PositionReviewerAgent(
            api_key=_key_for(config.llm.position_reviewer_model, config.llm.position_reviewer_provider),
            model=config.llm.position_reviewer_model,
            max_tokens=config.llm.get_max_tokens("position_reviewer"),
            fallback_api_key=_fallback_api_key,
            fallback_provider=config.llm.fallback_provider,
            fallback_model=config.llm.fallback_model,
            provider=config.llm.position_reviewer_provider,
            provider_order=config.llm.get_provider_order("position_reviewer"),
        )
        self.evening_analyst = EveningAnalystAgent(
            api_key=_key_for(config.llm.evening_analyst_model, config.llm.evening_analyst_provider),
            model=config.llm.evening_analyst_model,
            max_tokens=config.llm.get_max_tokens("evening_analyst"),
            fallback_api_key=_fallback_api_key,
            fallback_provider=config.llm.fallback_provider,
            fallback_model=config.llm.fallback_model,
            provider=config.llm.evening_analyst_provider,
            provider_order=config.llm.get_provider_order("evening_analyst"),
        )
        self.news_analyst = NewsAnalystAgent(
            api_key=_key_for(config.llm.news_analyst_model, config.llm.news_analyst_provider),
            model=config.llm.news_analyst_model,
            max_tokens=config.llm.get_max_tokens("news_analyst"),
            fallback_api_key=_fallback_api_key,
            fallback_provider=config.llm.fallback_provider,
            fallback_model=config.llm.fallback_model,
            provider=config.llm.news_analyst_provider,
            provider_order=config.llm.get_provider_order("news_analyst"),
        )
        self.macro_analyst = MacroAnalystAgent(
            api_key=_key_for(config.llm.macro_analyst_model, config.llm.macro_analyst_provider),
            model=config.llm.macro_analyst_model,
            max_tokens=config.llm.get_max_tokens("macro_analyst"),
            fallback_api_key=_fallback_api_key,
            fallback_provider=config.llm.fallback_provider,
            fallback_model=config.llm.fallback_model,
            provider=config.llm.macro_analyst_provider,
            provider_order=config.llm.get_provider_order("macro_analyst"),
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
            provider=config.llm.earnings_analyst_provider,
            provider_order=config.llm.get_provider_order("earnings_analyst"),
        )
        self.smart_money_analyst = SmartMoneyAnalystAgent(
            api_key=_key_for(config.llm.smart_money_analyst_model, config.llm.smart_money_analyst_provider),
            model=config.llm.smart_money_analyst_model,
            max_tokens=config.llm.get_max_tokens("smart_money_analyst"),
            fallback_api_key=_fallback_api_key,
            fallback_provider=config.llm.fallback_provider,
            fallback_model=config.llm.fallback_model,
            provider=config.llm.smart_money_analyst_provider,
            provider_order=config.llm.get_provider_order("smart_money_analyst"),
        )
        self.smart_money_provider = SECForm4Provider(
            search_url=config.smart_money.search_url,
            archives_url=config.smart_money.archives_url,
            data_dir=config.smart_money.data_dir,
            user_agent=config.smart_money.user_agent,
            request_timeout_s=config.smart_money.request_timeout_s,
            refresh_deadline_s=config.smart_money.refresh_deadline_s,
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
            insider_min_material_sell_fraction=config.smart_money.insider_min_material_sell_fraction,
            insider_history_retention_days=config.smart_money.insider_history_retention_days,
        )
        self.meta_reflector = MetaReflectorAgent(
            api_key=_key_for(config.llm.meta_reflector_model, config.llm.meta_reflector_provider),
            model=config.llm.meta_reflector_model,
            max_tokens=config.llm.get_max_tokens("meta_reflector"),
            fallback_api_key=_fallback_api_key,
            fallback_provider=config.llm.fallback_provider,
            fallback_model=config.llm.fallback_model,
            provider=config.llm.meta_reflector_provider,
            provider_order=config.llm.get_provider_order("meta_reflector"),
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
        self.broker = AlpacaBroker(
            api_key=config.api_keys.alpaca_key,
            secret_key=config.api_keys.alpaca_secret,
            paper=config.alpaca.paper,
            kill_switch_path=str(self._kill_switch_path),
        )
        # Wire the broker as yfinance's fallback so a yfinance outage doesn't
        # blackout the technical analyst. Alpaca's daily bars cover the same
        # universe we trade on, so fallback coverage is effectively 100%.
        self.market.set_fallback_bars(self.broker.get_bars)
        raw_storage_db_path = config.storage.db_path
        if raw_storage_db_path == ":memory:":
            # ``:memory:`` is a SQLite sentinel, not a relative filename.
            # Rewriting it under the repository creates a persistent DB and
            # lets otherwise-isolated tests/processes contaminate one another.
            self._storage_db_path = raw_storage_db_path
        else:
            storage_db_path = Path(raw_storage_db_path)
            if not storage_db_path.is_absolute():
                storage_db_path = Path(__file__).resolve().parent.parent / storage_db_path
            self._storage_db_path = str(storage_db_path)
        self.db = Database(self._storage_db_path)
        self.db.initialize()
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
        from src.portfolio_constructor import ConstructorConfig
        _risk_cfg = getattr(config, "risk", None)

        def _risk_setting(name: str, default: float) -> float:
            """Read a risk ceiling, or the ratified default.

            Coerced through a real float check rather than trusted from
            `getattr`: many tests construct the pipeline against a MagicMock
            config, where attribute access auto-creates a child mock that is
            neither the default nor a number — and a MagicMock reaching the
            sizing arithmetic fails with an opaque TypeError deep inside the
            constructor. Same defensive posture as `_coerce_token_count`.
            """
            value = getattr(_risk_cfg, name, default)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                return default
            return float(value) if value > 0 else default

        def _risk_list_setting(name: str, default: list[float]) -> tuple[float, ...]:
            """Read a risk-config list setting, or the ratified default.

            Same Mock-safety posture as `_risk_setting`: a MagicMock
            config fixture auto-creates a child mock for any attribute
            access, which is neither a list nor numeric — guard for that
            explicitly rather than let it reach the constructor as a
            non-iterable and blow up deep in the sizing arithmetic.
            """
            value = getattr(_risk_cfg, name, default)
            if not isinstance(value, (list, tuple)) or not value:
                return tuple(default)
            try:
                return tuple(float(v) for v in value)
            except (TypeError, ValueError):
                return tuple(default)

        self.portfolio_constructor = PortfolioConstructor(ConstructorConfig(
            risk_budget_pct=_risk_setting("max_position_risk_pct", 5.0),
            min_risk_pct=_risk_setting("min_position_risk_pct", 0.5),
            max_portfolio_risk_pct=_risk_setting("max_portfolio_risk_pct", 25.0),
            max_cluster_risk_share_pct=_risk_setting("max_cluster_risk_share_pct", 40.0),
            # Same setting the risk engine enforces (line ~326), so the
            # constructor sizes under the ceiling rather than proposing orders
            # `max_position_pct` — a HARD_BLOCK rule — will drop outright.
            max_position_pct=_risk_setting("max_position_pct", 100.0),
            # Spec §10.3 "concentration scales size". Read back off the risk
            # ENGINE's own resolved config rather than re-derived from
            # settings, so the number the constructor shrinks against is
            # provably the identical number the engine will enforce — the
            # drift `max_position_pct`'s "keep in sync" comment can only ask
            # for, this one gets structurally.
            max_sector_pct=self.risk_engine.config.max_sector_pct,
            max_sector_hard_pct=self.risk_engine.config.sector_hard_ceiling_pct,
            # §10.3's floor — reuses the existing $500 threshold rather than
            # inventing a second notion of "too small to bother". It lives
            # under `cash_sweep` because that is where it was first needed;
            # the number, not the section, is what is being reused.
            min_order_usd=_risk_number(
                getattr(getattr(config, "cash_sweep", None), "min_order_usd", None),
                500.0,
            ),
            # Stage 3 (shorts) — same "size under the hard block" pattern as
            # max_position_pct just above, mirrored for the short-specific
            # ceiling and its sizing haircut.
            max_single_short_pct=_risk_setting("max_single_short_pct", 10.0),
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
            min_stop_atr_multiple=_risk_setting("min_stop_atr_multiple", 3.0),
            min_reward_risk_after_widening=_risk_setting(
                "min_reward_risk_after_widening", 1.5,
            ),
            # Spec §12.1 — a stop sitting at a level the system COMPUTED is
            # honoured whatever the band says, down to a deterministic 1x ATR
            # floor. Same "wire from the ratified setting, not the
            # constructor's own default" pattern as every ceiling above.
            level_match_atr_tolerance=_risk_setting(
                "level_match_atr_tolerance", 0.25,
            ),
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
            max_target_horizon_sessions=int(
                _risk_setting("max_target_horizon_sessions", 60),
            ),
            target_divergence_warn_pct=_risk_setting(
                "target_divergence_warn_pct", 25.0,
            ),
            # Spec §9.4 "agreement earns size" — same "wire from the
            # ratified setting, not the constructor's own default" pattern
            # as every ceiling above.
            agreement_ceiling_pct=_risk_list_setting(
                "agreement_ceiling_pct", [3.0, 4.0, 5.0, 5.0, 5.0],
            ),
        ))
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
            event_calendar=self.event_calendar,
            fomc_calendar=self.fomc_calendar,
            has_actionable_signal_fn=self._has_actionable_signal_fn,
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
        not authoritative for execution: ExecutionStage still re-reads raw
        broker `cash` after the funding sale and skips any BUY that cash
        does not actually cover. See `CashSweeper.fund_buys`.

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

        Used for idempotence checks on system-generated orders like
        TAKE_PROFIT: a pending submitted trim should block a duplicate order,
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
        grouped: dict[str, list] = {}
        for observation in observations or []:
            symbol = str(getattr(observation, "symbol", "") or "").strip().upper()
            if not symbol or symbol in configured:
                continue
            if str(getattr(observation, "transaction_code", "") or "").upper() != "P":
                continue
            if not bool(getattr(observation, "admission_eligible", False)):
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
        macro_target_invested_pct: float | None = None,
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
        # D9 (Stage 3): running total of gross BEARISH notional already
        # allowed earlier in this batch — a SHORT of an ordinary name, or a
        # BUY of an inverse ETF, but NOT a SHORT of an inverse ETF (that is
        # a bullish bet, not a bearish one; see the signed accumulation
        # below) — so `max_gross_bearish_pct` sees two bearish orders in
        # the same run rather than checking each against only the
        # pre-existing book. Renamed from pending_short_gross_investment
        # (2026-08-30) alongside the ceiling itself.
        pending_gross_bearish_investment = 0.0
        # Spec §11.2: running total of GROSS notional (direction-agnostic,
        # leverage-adjusted) already allowed earlier in this batch. Without
        # it two entries in one run would each be measured against only the
        # pre-existing book and never see each other — the same gap
        # `pending_investment` closes for net exposure.
        pending_gross_investment = 0.0
        # Raw (unsigned, UN-leveraged) notional already approved this batch.
        # This is the pending leg of `book_exposure`'s `deployed` measure —
        # capital committed, which is what macro's `target_invested_pct` is
        # defined against. Distinct from `pending_cash_outflow` (BUYs only,
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
                pending_gross_bearish_investment=pending_gross_bearish_investment,
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
                # Unlike the gross-bearish accumulator just below, this is
                # unconditional on direction — a SHORT of any symbol never
                # spends this settled-cash pool (RiskRuleEngine.check), a
                # BUY of any symbol always does.
                pending_cash_outflow += raw_investment
            # Gross BEARISH accumulator: keyed off the SIGN of
            # `signed_investment`, not off `decision.action` or `is_short`.
            # Shorting an ordinary name and buying an inverse ETF both push
            # `signed_investment` negative and both count. The quadrant
            # this mirrors: SHORTING an inverse ETF (e.g. SHORT SQQQ) is a
            # BULLISH bet (SQQQ falls when the index it inverts rises), so
            # it pushes `signed_investment` POSITIVE and must NOT count,
            # even though it is mechanically a SHORT — matches the signed
            # gate in RiskRuleEngine.check.
            if signed_investment < 0:
                pending_gross_bearish_investment += abs(signed_investment)
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

        # Advisory check: projected net exposure vs macro's target_invested_pct.
        # Does NOT block trades; emits a non-hard violation so RiskManager sees it
        # and can either scale_all_buys or override with a reasoning.
        if macro_target_invested_pct is not None and total_value > 0:
            from src.risk.rules import book_exposure, RiskViolation
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
            deviation = projected_invested_pct - macro_target_invested_pct
            if abs(deviation) > 15:
                # RC3: direction matters. The old symmetric message told RM
                # to "consider scale_all_buys" for BOTH directions — for an
                # UNDER-deployed book that advice compounds the exact drag
                # it should be correcting (three months of 39% invested vs
                # a 72-75% target).
                if deviation < 0:
                    guidance = (
                        "advisory — book is UNDER macro's target; do NOT "
                        "scale down the remaining BUYs for this reason. If "
                        "cutting anything, name a risk specific to the trade, "
                        "not the gap."
                    )
                else:
                    guidance = "advisory — RM should consider scale_all_buys"
                remaining_violations.append(RiskViolation(
                    rule="macro_exposure_deviation",
                    message=(
                        f"Projected invested {projected_invested_pct:.0f}% (capital at "
                        f"work; net direction {projected.net_pct:+.0f}%) deviates "
                        f"from Macro target {macro_target_invested_pct:.0f}% by {deviation:+.0f}pp "
                        f"({guidance})"
                    ),
                    value=projected_invested_pct,
                    limit=macro_target_invested_pct,
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
        2. **A stop/target edit cannot bypass the floors a fresh decision
           would have to clear.** The constructor enforces `REWARD_RISK_FLOOR`
           and a noise-band stop distance before a decision ever reaches the
           Risk Manager; both checks compared a modified decision only
           against itself, so an RM edit that widened a stop or pulled in a
           target could ship a BUY/SHORT with a reward:risk the constructor
           itself would have refused, or a stop resting inside the ATR noise
           band. This reuses the SAME arithmetic (`TradeDecision.reward_risk`,
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
                    updated_decisions[idx] = None
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

        return [d for d in updated_decisions if d is not None], rejected_mods

    def _risk_mod_floor_breach(
        self,
        original: TradeDecision,
        modified: TradeDecision,
        mod,
        symbols_bars: dict | None,
    ) -> str | None:
        """Return a refusal reason if `modified` breaches a floor a freshly
        constructed decision would have to clear, else None.

        Reuses `TradeDecision.reward_risk` (== `models.reward_to_risk`, the
        one ratio definition this codebase shares end to end) against
        `REWARD_RISK_FLOOR` — the exact floor the constructor itself
        enforces before a decision ever reaches the Risk Manager. Does not
        re-derive the ratio or the number.
        """
        new_rr = modified.reward_risk
        if new_rr is None or new_rr < REWARD_RISK_FLOOR:
            rr_text = "unmeasurable" if new_rr is None else f"{new_rr:.2f}"
            return (
                f"modified geometry (entry ${modified.entry_price:.2f}, stop "
                f"${modified.stop_loss:.2f}, target ${modified.take_profit:.2f}) "
                f"reward:risk={rr_text} < {REWARD_RISK_FLOOR} floor — the "
                f"constructor would have refused this trade at these prices. "
                f"RM reason given: {mod.reason!r}"
            )

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
    def _has_actionable_signal_fn(indicators, symbol: str, bars, positions) -> bool:
        """Pre-filter: only send symbols with interesting signals to the LLM.

        Lifted from a nested function in run_morning so MorningResearchStage
        can inject it as a dependency. Takes positions explicitly rather than
        closing over an outer scope.
        """
        held_symbols = {p.symbol for p in positions}
        if symbol in held_symbols:
            return True
        if not isinstance(indicators, TechnicalIndicators):
            return True  # can't filter unknown types, pass through
        if indicators.rsi_14 is not None and (indicators.rsi_14 < 35 or indicators.rsi_14 > 65):
            return True
        if indicators.bb_upper and indicators.bb_lower and bars:
            last_close = bars[-1].close
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

        Read-only for longs, auto-repairing for longs only (see
        `_repair_stop_coverage` — it reconstructs the stop from the recorded
        BUY row, which only exists for a long). A short's gap is still
        detected and returned — Stage 1 made a held short visible to this
        audit; leaving it unchecked would have made a naked short the ONE
        risk state this reconciler can't see — but it is flagged rather than
        repaired: no order path can open a short yet, so there is no
        recorded entry row to reconstruct its original stop from, and
        inventing a level here would be exactly the policy call this
        reconciler has always refused to make for an unrecorded stop.
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
        longs_checked = 0
        shorts_checked = 0
        sweeper = self._sweeper()
        sweep_symbol = sweeper.symbol if sweeper is not None else None
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
                    repaired = (
                        False if is_short
                        else self._repair_stop_coverage(symbol, held - covered)
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
                if is_short:
                    # No order path can open a short yet, so there is no BUY
                    # trade row to reconstruct its original stop level from
                    # (_repair_stop_coverage reads the last BUY). Flag it for
                    # the operator; inventing a level would be exactly the
                    # policy call this reconciler refuses to make for a long
                    # too when the level is unknown.
                    gap["repaired"] = False
                else:
                    gap["repaired"] = self._repair_stop_coverage(symbol, held - covered)
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
        if naked:
            self._alert_owner_no_stop(naked)
        return gaps

    @staticmethod
    def _alert_owner_no_stop(naked: list[dict]) -> None:
        """Push the NO-STOP-AT-ALL escalation to the owner. Never raises."""
        try:
            from src import notifier as _notifier

            detail = "\n".join(
                f"  {g.get('symbol', '?')}: held {g.get('held_qty')}, "
                f"covered {g.get('covered_qty')}"
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

    def _repair_stop_coverage(self, symbol: str, uncovered_qty: float) -> bool:
        """Best-effort: re-place protective stop coverage on an uncovered long
        using the stop level recorded on its last BUY. Returns True when the
        gap was actually closed.

        Spec §11.1 hybrid fractional stops: this is also THE re-placement
        path for a sub-share DAY stop that lapsed at yesterday's close. It
        needs no fractional special-case of its own — it routes through
        `_submit_protective_stop_retrying`, which splits a fractional
        `uncovered_qty` into its GTC and DAY legs, so re-placing 0.3456
        share(s) at the open and protecting a whole fresh 12.3456-share entry
        are the same one code path. The caller (`_reconcile_stop_coverage`)
        owns the decision of WHETHER to call this at the current hour; this
        function does not consult the clock.

        Why this is now safe to auto-repair (it deliberately wasn't before):
        the old objection was "the original protective level is unknown for a
        position with no live stop, so picking one is a policy decision". It
        isn't unknown — the BUY row carries the `stop_loss` the PM/RM agreed
        and the constructor sized against. Repairing to THAT level restores the
        reviewed intent rather than inventing a new one.

        This is the belt for the 2026-07-16 CRITICAL (BUY-attached OTO stops
        inherited a DAY tif and expired at the close, leaving positions naked
        overnight) — both for any position that bug left uncovered, and for a
        crash between an entry fill and `place_entry_protection`.

        Guards: never place a stop at/above the current price (that would
        instantly fire and turn a repair into a market-order exit — a decision
        for the reviewer, not for a janitor), and never invent a level when the
        BUY row has none.
        """
        if uncovered_qty <= 0:
            return False
        try:
            # include_in_flight: a same-session BUY still at fill_status=
            # 'submitted' is the row whose stop we want — under the strict
            # executed predicate the repair either no-op'd or read a months-
            # old prior BUY's stop level (audit round 2).
            buy = self.db.get_symbol_last_buy(symbol, include_in_flight=True) or {}
        except Exception as exc:  # noqa: BLE001
            logger.warning("coverage repair: last-BUY lookup failed for %s: %s", symbol, exc)
            return False
        try:
            stop_price = float(buy.get("stop_loss") or 0)
        except (TypeError, ValueError):
            stop_price = 0.0
        if stop_price <= 0:
            logger.warning(
                "coverage repair: %s has no recorded BUY stop_loss — leaving the "
                "gap flagged for manual review", symbol,
            )
            return False
        try:
            price = self.broker.get_latest_price(symbol)
        except Exception as exc:  # noqa: BLE001
            logger.warning("coverage repair: price lookup failed for %s: %s", symbol, exc)
            return False
        if not (isinstance(price, (int, float)) and price > 0 and math.isfinite(price)):
            return False
        if stop_price >= price:
            logger.warning(
                "coverage repair: %s recorded stop $%.2f is at/above the live "
                "price $%.2f — a repair would fire immediately. Leaving the gap "
                "flagged; the reviewer owns this exit decision.",
                symbol, stop_price, price,
            )
            return False
        # Spec §11.1 guard 1 belongs here too. This was a single bare
        # `_submit_stop_limit_order` call with NO retry burst at all — a
        # transient failure (429, dropped connection) cost the position a
        # full 30-minute cycle instead of clearing in ~2 seconds the way the
        # entry path does, and for a FRACTIONAL `uncovered_qty` (this repair
        # is reached with one whenever the gapped position is itself
        # fractional) there was also no whole-share fallback — the exact gap
        # guard 1 exists to close on the entry side, left open on the belt
        # that is supposed to be its backstop. Route through the same
        # retrying+fallback machinery instead of a second, weaker copy of it.
        result = self.broker._submit_protective_stop_retrying(
            symbol=symbol, qty=uncovered_qty, stop_price=stop_price,
            limit_price=stop_price * (1 - self.broker.STOP_LIMIT_BUFFER_PCT),
            side="sell",
        )
        if result is None:
            logger.error(
                "coverage repair FAILED for %s (%.4f uncovered, stop $%.2f) — "
                "retries exhausted", symbol, uncovered_qty, stop_price,
            )
            return False
        residual = 0.0
        if isinstance(result, dict):
            try:
                residual = float(result.get("uncovered_qty") or 0)
            except (TypeError, ValueError):
                residual = 0.0
        if residual > 0:
            # A whole-share floor stop landed but a sub-share sliver is
            # still gapped. Reported as NOT repaired — not because nothing
            # happened, but because the gap is real and smaller, not gone.
            # The broker snapshot the NEXT sweep takes reflects the partial
            # cover on its own and reclassifies this symbol "partial" rather
            # than "none"; this pass keeps escalating instead of going quiet
            # on a still-real gap.
            logger.warning(
                "coverage repair PARTIAL for %s: covered %.4f of %.4f "
                "uncovered share(s) at stop $%.2f — %.4f share(s) still "
                "gapped; next sweep will re-check", symbol,
                uncovered_qty - residual, uncovered_qty, stop_price, residual,
            )
            return False
        logger.warning(
            "COVERAGE REPAIRED: %s — placed protective stop-limit coverage for "
            "%.4f uncovered share(s) at the recorded BUY stop $%.2f (GTC over "
            "the whole shares, DAY over any sub-share remainder)",
            symbol, uncovered_qty, stop_price,
        )
        return True

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
            # next drain (this used to vary by site — only auto_take_profit
            # restored; the others rode naked until drain).
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
                    self.broker.wait_for_order_terminal(prot["order_id"])
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
        """After a partial exit (TAKE_PROFIT / REDUCE / PARTIAL_SELL), place a
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
            return True
        stop_prices = [s.get("stop_price", 0) for s in cancelled_specs]
        best_stop = min(stop_prices, default=0) if side == "buy" else max(stop_prices, default=0)
        if best_stop <= 0:
            return True

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
        try:
            for leg_qty in legs:
                self.broker._submit_stop_limit_order(
                    symbol=symbol, qty=leg_qty, stop_price=best_stop, **side_kwargs,
                )
            logger.info(
                "Re-protected %s residual qty=%s @ stop $%.2f after partial exit",
                symbol, self._format_qty(residual_qty), best_stop,
            )
            return True
        except Exception as exc:
            logger.warning(
                "Re-protect failed for %s residual=%s @ $%.2f: %s — position "
                "is unprotected until the next session re-attaches a stop",
                symbol, self._format_qty(residual_qty), best_stop, exc,
            )
            return False

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
                        evidence_json=json.dumps({
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
        `positions` table (synced directly from `AlpacaBroker.get_positions`
        every session — see `Database.sync_positions`) correctly went to
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
        out: dict[str, dict] = {}
        today = et_today()
        for p in positions:
            sym = p.symbol
            entry = None
            try:
                entry = self.db.get_symbol_last_buy(sym)
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
        """L3b memory: last 7 days of macro regime / confidence / target_invested_pct."""
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
            pg = snap.get("position_guidance") or {}
            target = pg.get("target_invested_pct", "?")
            lines.append(f"- {d}: {regime} ({conf}) → target {target}%")
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
            if not order:
                continue
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

    def _auto_take_profit(self, positions, run_id: str,
                          profit_pct_trigger: float = 30.0,
                          trim_fraction: float = 0.15) -> list[dict]:
        """Auto-sell `trim_fraction` of any position up ≥ `profit_pct_trigger`%.

        Runs once per holding (detected by looking for a prior TAKE_PROFIT row
        in trades after the most recent BUY for that symbol). Defaults bias
        hard toward "let winners run" — auto-TP is a give-back guardrail, not
        an alpha-generating mechanism, and the LLM position_reviewer (which
        runs AFTER this) is the right place for thesis-aware trims.

        Earlier 15%/33% defaults were clipping early-innings multi-baggers:
        2026-04-30 GOOGL trim fired at +27% gain on the same morning that
        news_analyst flagged a HIGH bullish state change (AI capex split
        favoring Alphabet). The LLM reviewer's later read was "clearest hold,
        fast winner with reinforced thesis should keep running" — but auto-TP
        had already cut 28% of the position before the LLM got to vote.

        30%/15% defaults: only truly outsized single-name gains (30%+) trigger,
        and when triggered, the trim is a clip (15%) not a harvest (33%).
        Combined effect ~75% less auto-TP turnover. Backstops still in place:
        OTO stop, trailing stop, LLM reviewer at midday/close, hard daily-loss
        circuit breaker, evening thesis_health_review.
        """
        orders: list[dict] = []
        pending_protections: list[dict] = []
        for p in positions:
            if p.qty <= 0 or p.avg_entry <= 0:
                continue
            # Same `unrealized_pnl_pct` every P&L% in the system now uses.
            # Long-only here (the `p.qty <= 0` filter above), so the absolute
            # cost basis is arithmetically identical to the signed one this
            # replaces — routed through the shared function so a future
            # short-side auto-TP cannot inherit a fifth denominator.
            pnl_pct = unrealized_pnl_pct(p)
            if pnl_pct is None or pnl_pct < profit_pct_trigger:
                continue
            # Did we already trim this holding? Look at trades newer than the
            # most recent BUY for this symbol. If a TAKE_PROFIT exists there,
            # skip.
            try:
                sym_trades = self.db.get_trades(symbol=p.symbol, limit=20)
            except Exception as e:
                logger.warning("auto_take_profit: trade history lookup failed for %s: %s", p.symbol, e)
                continue
            # Trades are newest-first; find the index of the most recent BUY
            # and check for TAKE_PROFIT rows AFTER it.
            recent_buy_idx = None
            for i, t in enumerate(sym_trades):
                if (
                    (t.get("action") or "").upper() == "BUY"
                    and self._trade_executed_or_pending(t)
                ):
                    recent_buy_idx = i
                    break
            if recent_buy_idx is None:
                # No prior BUY on record — odd; could be a pre-existing manual
                # position. Skip auto-TP to avoid touching things we didn't open.
                continue
            already_tp = any(
                (t.get("action") or "").upper() == "TAKE_PROFIT"
                and self._trade_executed_or_pending(t)
                for t in sym_trades[:recent_buy_idx]
            )
            if already_tp:
                continue

            # Compute trim qty. For integer holdings round down, min 1 share.
            trim_qty = p.qty * trim_fraction
            if float(p.qty).is_integer():
                trim_qty = max(1.0, float(int(trim_qty)))
            if trim_qty <= 0 or trim_qty >= p.qty:
                # Trimming the whole position isn't 'take-profit' — skip and
                # let the trailing stop handle that decision.
                continue
            sell_limit = round(p.current_price * 0.995, 2)
            # audit F1 review #1: snapshot -> persist WAL -> cancel, so
            # the recovery row is durable BEFORE any broker mutation.
            sale = self._submit_protected_sell(
                symbol=p.symbol, qty=trim_qty, limit_price=sell_limit,
                reference_price=p.current_price, position_qty_before_sell=p.qty,
                label="TAKE_PROFIT",
            )
            if sale is None:
                continue
            order, prot = sale
            pending_protections.append(prot)
            try:
                self.db.insert_trade(
                    symbol=p.symbol, action="TAKE_PROFIT", qty=trim_qty,
                    price=p.current_price,
                    reasoning=(
                        f"Auto take-profit: {pnl_pct:+.1f}% ≥ {profit_pct_trigger}%, "
                        f"trimming {trim_fraction * 100:.0f}% (remaining {p.qty - trim_qty:.0f} "
                        f"shares continue riding stop)"
                    ),
                    run_id=run_id,
                    broker_order_id=order.get("id"),
                    fill_status="submitted",
                )
            except Exception as e:
                logger.warning("auto_take_profit: audit log failed for %s: %s", p.symbol, e)
            orders.append(order)
            logger.info(
                "Auto take-profit: %s +%.1f%% → sold %s of %s @ limit $%.2f",
                p.symbol, pnl_pct, self._format_qty(trim_qty),
                self._format_qty(p.qty), sell_limit,
            )
        # Wait for each accepted sell to terminate, then reprotect on
        # actual residual or restore originals if the sell didn't fill.
        self._finalize_pending_protections(
            pending_protections, context="auto_take_profit",
        )
        return orders

    def _wait_for_midday_auto_tp_orders(self, auto_tp_orders: list[dict]) -> set[str]:
        """Wait briefly for midday auto take-profit sells and return symbols still in flight."""
        pending_symbols: set[str] = set()
        terminal_states = {
            "filled",
            "canceled",
            "cancelled",
            "expired",
            "rejected",
            "done_for_day",
            "replaced",
        }
        for order in auto_tp_orders:
            symbol = (order.get("symbol") or "").strip().upper()
            if not symbol:
                continue
            order_id = order.get("id")
            status = str(order.get("status") or "").lower()
            if order_id:
                try:
                    polled = self.broker.wait_for_order_terminal(order_id)
                    if polled:
                        status = str(polled).lower()
                except Exception as e:
                    logger.warning(
                        "Midday auto-TP wait failed for %s (%s): %s",
                        symbol, order_id, e,
                    )
            if status not in terminal_states:
                pending_symbols.add(symbol)
        if pending_symbols:
            logger.info(
                "Midday: blocking same-symbol LLM exits while auto take-profit is still in flight: %s",
                ", ".join(sorted(pending_symbols)),
            )
        return pending_symbols

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
        # stays out because it's rule-based, not a reviewer decision.
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
        # audit round 2: get_ohlcv's end is exclusive, so bars stop at
        # YESTERDAY's close — while the stock leg uses a LIVE quote. For a
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
        `execution_skip.reason` (`qty_zero`, `geometry_rr`,
        `insufficient_cash`), `verdict.reason_category` (`rr_fail`, …),
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
                # `scripts/blocked_proposals_census.py::_load_constructor_drops`
                # — without it, a constructor drop falls through to the
                # generic `no_order_built` bucket below with no explanation,
                # even though the real reason was captured at drop time.
                if (data.get("stage") == "deterministic_gate"
                        and data.get("outcome") == "blocked"
                        and data.get("reason") == "constructor_dropped"):
                    constructor_drops[(did, sym)] = (
                        data.get("detail") or "constructor_dropped"
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
            try:
                buy = self.db.get_symbol_last_buy(sym)
            except Exception as e:  # noqa: BLE001
                logger.warning("stop map: last-buy lookup failed for %s: %s", sym, e)
                buy = None
            initial = float((buy or {}).get("stop_loss") or 0)
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
        # pre-trade gate's `macro_exposure_deviation` advisory reads, so PM
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

        # RC3: deployment gap vs the macro target as a hard fact in PM's
        # face. `invested_pct` above is sweep-aware (the DecisionStage view
        # already counts parked T-bills as cash, not exposure).
        try:
            target = None
            if macro_analysis is not None:
                guidance = getattr(macro_analysis, "position_guidance", None)
                target = getattr(guidance, "target_invested_pct", None)
            if isinstance(target, (int, float)) and math.isfinite(target):
                f.macro_target_invested_pct = float(target)
                f.deployment_gap_pp = round(f.invested_pct - float(target), 1)
        except Exception as e:  # noqa: BLE001
            logger.warning("pm_facts: deployment gap failed: %s", e)

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

        Returns e.g. {'rolling_5d_pct': -2.3, 'rolling_20d_pct': -6.1,
                      'in_drawdown': True, 'trailing_days': 18}
        """
        try:
            rows = self.db.get_daily_pnl(limit=25)
        except Exception as e:
            logger.warning("Failed to read daily_pnl for drawdown context: %s", e)
            return {}
        if not rows:
            return {"rolling_5d_pct": None, "rolling_20d_pct": None,
                    "in_drawdown": False, "trailing_days": 0,
                    "peak_to_trough_pct": None}

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

        in_drawdown = False
        if rolling_5d is not None and rolling_5d < -3.0:
            in_drawdown = True
        if rolling_20d is not None and rolling_20d < -8.0:
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
            "trailing_days": len(rows),
            "peak_to_trough_pct": peak_to_trough,
        }

    def _refresh_account_state(self):
        account = self.broker.get_account()
        positions = self.broker.get_positions()
        price_map = {p.symbol: p.current_price for p in positions}
        return account, positions, price_map

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

    # ---------------------------------------------------------------
    # Morning stages (extracted from the legacy monolithic run_morning).
    # Phase 4 #1 final wire-up: each stage is a method taking ctx; the
    # orchestrating run_morning just composes them. Stages can be tested
    # individually by constructing a ctx, populating the needed fields,
    # and calling the method directly.
    # ---------------------------------------------------------------

    def _check_late_breach_and_emergency_liquidate(
        self, run_id: str, where: str,
    ) -> dict | None:
        """Refresh broker state and emergency-liquidate if the daily-loss
        limit was crossed mid-session.

        Used by morning at the post-research checkpoint and (potentially)
        by other long-running phases to close the gap between the
        pre-research circuit breaker (#45) and the pre-execution recheck
        (#48). On a slow-OpenAI day research can take 5-10 min — plenty
        of time for the tape to gap through the limit while morning is
        still computing.

        Returns the emergency-sold response dict on breach, None to
        proceed. ``where`` is a short tag for the log message
        (post-research / post-decision / etc).
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

        total_value = account["portfolio_value"]
        last_equity = account.get("last_equity", total_value)
        daily_pnl = total_value - last_equity

        loss_violation = self.risk_engine.check_daily_loss(last_equity, daily_pnl)
        if not (loss_violation and positions):
            return None

        logger.warning(
            "Morning late-breach (%s): %s — force-liquidating before "
            "early-return; intra would otherwise wait 30 min",
            where, loss_violation.message,
        )
        orders = self._midday_emergency_liquidate(positions, loss_violation, run_id)
        return {
            "status": "emergency_sold",
            "orders": orders,
            "run_id": run_id,
        }

    def _midday_emergency_liquidate(
        self, positions, loss_violation, run_id: str,
    ) -> list[dict]:
        """Force-close every position when daily loss breaches the cap —
        a long is SOLD, a short is BOUGHT-TO-COVER.

        Isolated from run_midday so the midday execution flow stays
        readable. Uses a 1% slippage cushion on the limit (vs the 0.5%
        used for ordinary sells) because the tape is usually ugly when
        this fires — mirrored above/below the reference price by side (see
        ``_EMERGENCY_LIMIT_CUSHION_PCT``).

        Before this fix, a short position could ONLY ever be closed by its
        own stop order — this loop's gate (`_full_sell_qty`) refused any
        negative qty outright, so a held short was silently skipped on
        every breach, with no log line and no operator signal. If that
        short's stop had been cancelled, rejected, or the position needed
        closing for a reason other than price, there was no mechanism at
        all to get out of it. `_forced_close_side_and_qty` closes that gap
        by reading the position's OWN sign to pick a side rather than
        assuming SELL; see its docstring for why an indeterminate qty
        refuses outright rather than guessing.
        """
        logger.warning(
            "MIDDAY RISK ALERT: %s — force-closing all positions",
            loss_violation.message,
        )
        # Reconcile pending fills BEFORE the per-symbol idempotence dedupe.
        # Without this, a stale 'submitted' row whose broker order was
        # actually cancelled/expired/rejected (e.g., halted symbol, day-order
        # expiry) would falsely mask the symbol as "still in flight" and
        # block this fresh emergency exit — the circuit breaker would
        # silently stop trying to sell. Reconciliation flips terminal
        # statuses in DB so has_pending_action_for_symbol sees truth.
        self._reconcile_fills()
        # Cancel the day's resting entry BUY limits BEFORE selling (audit
        # round 2): a DAY entry order left working would re-buy into the very
        # crash the breaker is liquidating — "force-close everything" must
        # mean pending intentions too. Best-effort; preserves protective legs.
        try:
            self.broker.cancel_open_entry_orders()
        except Exception as exc:  # noqa: BLE001
            logger.warning("emergency liquidate: entry-order cancel failed: %s", exc)
        orders: list[dict] = []
        pending_protections: list[dict] = []
        for p in positions:
            try:
                closing = self._forced_close_side_and_qty(p.qty)
                if closing is None:
                    logger.error(
                        "Midday emergency liquidate: %s has an "
                        "indeterminate position qty (%r) — refusing to "
                        "guess SELL vs BUY-to-cover. A wrong guess here "
                        "would ADD to the exposure instead of closing it. "
                        "Left untouched; needs operator attention.",
                        p.symbol, p.qty,
                    )
                    continue
                side, qty = closing
                action = "EMERGENCY_SELL" if side == "sell" else "EMERGENCY_COVER"
                if self.db.has_pending_action_for_symbol(p.symbol, action):
                    logger.info(
                        "Midday emergency %s: skipping %s — prior "
                        "%s submission still pending at broker",
                        side, p.symbol, action,
                    )
                    continue
                cushion = self._EMERGENCY_LIMIT_CUSHION_PCT
                emergency_limit = round(
                    p.current_price * ((1 + cushion) if side == "buy" else (1 - cushion)),
                    2,
                )
                # audit F1 review #1: snapshot -> persist WAL -> cancel.
                sale = self._submit_protected_sell(
                    symbol=p.symbol, qty=qty, limit_price=emergency_limit,
                    reference_price=p.current_price, position_qty_before_sell=qty,
                    label=action, side=side,
                )
                if sale is None:
                    continue
                order, prot = sale
                pending_protections.append(prot)
                orders.append(order)
                self.db.insert_trade(
                    symbol=p.symbol, action=action, qty=qty,
                    price=emergency_limit,
                    reasoning=f"Daily loss limit breached: {loss_violation.message}",
                    run_id=run_id,
                    broker_order_id=order.get("id"),
                    fill_status="submitted",
                )
                logger.info(
                    "Emergency %s: %s %s @ limit $%.2f",
                    "sell" if side == "sell" else "buy-to-cover",
                    self._format_qty(qty), p.symbol, emergency_limit,
                )
            except Exception as e:
                logger.error("Emergency liquidate failed for %s: %s", p.symbol, e)
        # Wait + finalize: if any limit didn't fill, restore the original
        # stops so the position doesn't ride the rest of the session naked.
        self._finalize_pending_protections(
            pending_protections, context="Midday emergency",
        )
        return orders

    def _symbols_already_trimmed_today(self) -> set[str]:
        """Symbols that received a sell-side action earlier today (ET).

        Used by position_reviewer's same-day-trim discipline at midday/close:
        if midday already trimmed AMZN at +12% on TARGET_BREACH, close should
        not trim it AGAIN at +13% on the same flag — that loop produced a
        73% one-day cut on a still-working position (2026-05-04 AMZN 41 →
        21 → 11 shares).

        Sell-side = REDUCE / SELL / TAKE_PROFIT / PARTIAL_SELL(...) /
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
        """
        from src.risk.trailing import compute_trailing_stop

        orders: list[dict] = []
        for position in positions:
            symbol = position.symbol
            try:
                buy = self.db.get_symbol_last_buy(symbol)
            except Exception as e:  # noqa: BLE001
                logger.warning("trail: last-buy lookup failed for %s: %s", symbol, e)
                continue
            if not buy:
                continue
            try:
                current_stop = self.broker.get_current_stop_price(symbol)
            except Exception as e:  # noqa: BLE001
                logger.warning("trail: stop lookup failed for %s: %s", symbol, e)
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

            proposal = compute_trailing_stop(
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
                # one -- buy.stop_loss is written once at BUY and never
                # mutated by a later TRAIL_STOP (that writes a separate
                # trade row), so it stays the true initial risk for the
                # life of the position. Powers the Type A +1R breakeven
                # ratchet.
                initial_stop=(buy or {}).get("stop_loss"),
            )
            if proposal is None:
                continue
            logger.info("Deterministic trail: %s", proposal.reason)
            try:
                order = self.broker.replace_stop_loss(symbol, proposal.new_stop)
            except Exception as e:  # noqa: BLE001
                logger.error(
                    "trail: replace_stop_loss failed for %s (%s) — the OLD "
                    "stop remains in force", symbol, e,
                )
                continue
            if not order:
                continue
            if isinstance(order, dict):
                order.setdefault("action", "TRAIL_STOP")
            orders.append(order)
            try:
                self.db.insert_trade(
                    symbol=symbol, action="TRAIL_STOP", qty=position.qty,
                    price=proposal.new_stop, reasoning=proposal.reason,
                    run_id=run_id,
                )
            except Exception as e:  # noqa: BLE001
                logger.warning("trail: trade row write failed for %s: %s", symbol, e)
        return orders

    def _risk_review_exits(
        self, review, positions, *, run_id: str, total_value: float,
        macro_summary: dict | None = None, position_facts: dict | None = None,
    ):
        """Put the reviewer's exits in front of the AI Risk Manager — Phase 3.4.

        `AGENTS.md` states the chain as `Specialists -> Portfolio Manager ->
        AI Risk -> deterministic Python -> broker`, **for exits as well as
        entries**. Until this landed, `run_position_review` called only
        `position_reviewer` and then executed, so the entire sell side skipped
        the veto layer the buy side has always had.

        Returns `(vetoed_symbols, verdict_or_None)`. Symbols in the returned
        set are dropped by the caller.

        **Failure posture: FAIL OPEN.** An unparseable or errored Risk Manager
        lets the exits through, logged loudly. This deliberately differs from
        the entry path, which fails closed with zero orders (`RiskStage`).
        The asymmetry is intentional and owner-ratified (2026-08-27):
        - failing closed on an ENTRY means not buying, which costs nothing;
        - failing closed on an EXIT means a thesis-invalidated position cannot
          be closed because a language model is unavailable, and the loss is
          then bounded only by the broker stop.
        The deterministic gates — the named-trigger requirement and the
        metric-contradiction veto — have already run by this point and are the
        real protection. AI Risk here is a second opinion, not the gate.
        """
        from src.models import (
            PortfolioDecision, ReasoningChain, TradeDecision,
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
        for action in exits:
            symbol = action.symbol.upper()
            if symbol not in held:
                continue
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
                reasoning=action.reason[:500],
            ))
        if not decisions:
            return set(), None

        summary = (review.overall_assessment or "")[:400]
        proposal = PortfolioDecision(
            reasoning_chain=ReasoningChain(
                macro_filter=(review.reasoning_chain.macro_continuity_check or "n/a")[:800],
                news_check="see position reviewer thesis_integrity_check",
                earnings_check="see position reviewer thesis_integrity_check",
                signal_conflicts=(review.reasoning_chain.thesis_integrity_check or "n/a")[:800],
                sizing_logic=(review.reasoning_chain.execution_rationale or "n/a")[:800],
                portfolio_balance=(review.reasoning_chain.winners_discipline_check or "n/a")[:800],
                cash_target=(review.reasoning_chain.session_disposition_check or "n/a")[:800],
            ),
            decisions=decisions,
            portfolio_view=f"EXIT REVIEW (position reviewer): {summary}",
        )

        try:
            verdict, rm_result = self.risk_manager.review(
                portfolio_decision=proposal,
                positions=positions,
                macro_summary=macro_summary or {},
                rule_violations=[],
                total_value=total_value,
                heat=self._build_portfolio_heat(positions, total_value),
            )
        except Exception as e:  # noqa: BLE001
            logger.error(
                "AI Risk exit review RAISED (%s) — failing OPEN: %d exit(s) "
                "proceed unreviewed. The named-trigger gate and the "
                "metric-contradiction veto already passed.",
                e, len(decisions),
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
        return vetoed, verdict

    def _midday_execute_llm_actions(
        self, positions, review, run_id: str, blocked_symbols: set[str] | None = None,
        already_trimmed_today: set[str] | None = None,
        metric_deltas: dict | None = None,
        risk_vetoed_symbols: set[str] | None = None,
        position_facts: dict | None = None,
    ) -> list[dict]:
        """Dispatch LLM-recommended SELL / REDUCE / TRAIL_STOP / COVER actions
        to broker.

        Dedups same-symbol conflicting actions by priority (SELL/COVER >
        REDUCE > TRAIL_STOP > HOLD) to avoid the broker seeing two orders
        fighting each other on one position. `blocked_symbols` lets midday
        suppress LLM exits for symbols that already have an in-flight system
        sell order.

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
        pending_protections: list[dict] = []
        blocked = {
            symbol.strip().upper()
            for symbol in (blocked_symbols or set())
            if symbol and symbol.strip()
        }
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
        for action_item in best_by_symbol.values():
            act = action_item.get("action")
            if act not in ("SELL", "REDUCE", "TRAIL_STOP", "COVER"):
                continue
            symbol = action_item.get("symbol", "")
            if symbol in blocked:
                logger.info(
                    "Midday: skipping %s %s — auto take-profit sell still in flight",
                    act, symbol,
                )
                continue
            # Same-day trim discipline: a symbol that already had a sell-side
            # action TODAY (auto-TP, midday REDUCE, etc.) is off-limits for
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
                        continue

            reason_text = action_item.get("reason", "")
            if act in ("SELL", "REDUCE", "COVER") and not _reason_cites_hard_trigger(reason_text):
                logger.warning(
                    "Position reviewer: blocking %s %s — the reason names no "
                    "recognised trigger. Exits require NEW INFORMATION "
                    "(thesis invalidation, adverse news, earnings, regime "
                    "shift, sector shock, correlation breach, stop hit); "
                    "price action and soft flags are not triggers. Reason "
                    "was: %r",
                    act, symbol, reason_text[:200],
                )
                try:
                    self.db.record_intraday_evaluation(
                        symbol=symbol, run_id=run_id,
                        status="exit_blocked_no_named_trigger",
                        detail=f"{act}: {reason_text[:400]}",
                    )
                except Exception as e:  # noqa: BLE001
                    logger.warning("exit gate: audit write failed: %s", e)
                continue

            # The same-day-trim gate that used to sit here is GONE, not
            # relaxed: it read `symbol in already_trimmed and not
            # _reason_cites_hard_trigger(...)`, and the unconditional gate
            # above now `continue`s on every untriggered SELL/REDUCE before
            # control ever reaches it. Leaving it in place would have been
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
            try:
                if act == "TRAIL_STOP":
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
                    order = self.broker.replace_stop_loss(symbol, new_stop)
                    if order:
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
                pending_protections.append(prot)
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
        self._finalize_pending_protections(
            pending_protections, context="Midday reviewer",
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

        Sell limit uses a 1% below-market buffer (same as
        `_midday_emergency_liquidate`) because we prioritize fill over price
        when clearing an unintended margin position.

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
        pending_protections: list[dict] = []
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
            pending_protections.append(prot)
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

        # Block the session until fills land so the post-refresh cash is real.
        # Then finalize protection — if any limit didn't fill, restore the
        # original stop coverage so the position isn't left naked.
        self._finalize_pending_protections(
            pending_protections, context="FORCE DE-LEVER",
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
        # equity curve yet (see `resolve_gross_ceiling`'s docstring) — but
        # by inspection `peak_to_trough_pct` only ever returns None when
        # TODAY's reading itself is unusable: a fresh account's own current
        # equity is finite, so it always produces a real number (0.0
        # against an empty history), never None. "Unknown" is therefore
        # reachable in production ONLY on a bad read, and Alpaca has been
        # observed to return NaN portfolio_value during market-open
        # glitches (see `RiskRuleEngine.check_daily_loss`'s docstring).
        # Silently holding the loosest cap on exactly that kind of
        # broken-snapshot day is the failure this guard closes: halting new
        # risk (the ladder's own floor rung) is safer than assuming zero
        # drawdown, and — unlike the ordinary "unknown" fallback — this
        # ALSO alerts the owner (`alert_owner=True` below) via the same
        # leverage-line/Telegram path `GROSS_LADDER_ALERT_PCT` already
        # uses, rather than staying silent.
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
        if pending_protections:
            self._finalize_pending_protections(
                pending_protections, context="GROSS-EXPOSURE DE-LEVER",
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
        except Exception as e:  # noqa: BLE001
            logger.error("GROSS-EXPOSURE DE-LEVER: broker refresh failed: %s", e)
        return orders

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
        emergency = self._check_late_breach_and_emergency_liquidate(run_id, where)
        if emergency is not None:
            if session == "morning":
                # A liquidation supersedes any PM checkpoint written before
                # the breaker opened (for example while entering RM).  Never
                # allow that pre-liquidation plan to resume after a reset.
                from src import decision_checkpoint as _dc
                _dc.mark_consumed("morning")
                _dc.write_status("morning", "emergency_sold")
            emergency["orders"] = existing_orders + list(emergency.get("orders") or [])
            emergency["paid_analysis_suspended"] = True
            emergency["suspension_error"] = str(error)
            if extra:
                emergency.update(extra)
            return emergency
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

    def run_morning(self) -> dict:
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

            # Hard circuit breaker before any LLM/research work. If the account
            # opens through the daily-loss limit, deterministic liquidation must
            # not depend on PM/RM producing a tradeable plan later in the run.
            daily_pnl = total_value - last_equity
            loss_violation = self.risk_engine.check_daily_loss(last_equity, daily_pnl)
            if loss_violation and positions:
                logger.warning(
                    "Morning risk alert before research: %s — force-closing all positions",
                    loss_violation.message,
                )
                orders = self._midday_emergency_liquidate(positions, loss_violation, run_id)
                # Any same-day plan is superseded by the liquidation — a
                # stale unconsumed checkpoint must not resume, and the
                # dead-man probe must not read this as a killed morning.
                from src import decision_checkpoint as _dc
                _dc.mark_consumed("morning")
                _dc.write_status("morning", "emergency_sold")
                return {
                    "status": "emergency_sold",
                    "orders": orders,
                    "run_id": run_id,
                }

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
                # bypass: deterministic emergency liquidate, no LLM dependency.
                late_breach = self._check_late_breach_and_emergency_liquidate(
                    run_id, "post-research",
                )
                if late_breach is not None:
                    _dc.mark_consumed("morning")
                    _dc.write_status("morning", "emergency_sold")
                    return late_breach

                if not analyses:
                    logger.warning("No analyses produced, skipping trading")
                    # Legit PM-less completion — record it so the evening
                    # dead-man probe doesn't read "research rows, no PM row"
                    # as a killed morning.
                    _dc.write_status("morning", "no_data")
                    return {"status": "no_data", "orders": [], "run_id": run_id}

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
                # or empty-plan exit at this point would still skip
                # deterministic liquidation until the next intra tick. Codex
                # r8 #1 caught this gap — same fix as #60, just one stage
                # later in the pipeline.
                late_breach = self._check_late_breach_and_emergency_liquidate(
                    run_id, "post-decision",
                )
                if late_breach is not None:
                    # The just-written checkpoint is superseded by the
                    # emergency liquidation — never resume it.
                    _dc.mark_consumed("morning")
                    _dc.write_status("morning", "emergency_sold")
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
        # Morning BUY lookup by symbol for stop/target/days_held.
        buy_rows: dict[str, dict] = {}
        for t in morning_trades or []:
            sym = t.get("symbol")
            if not sym or t.get("action") != "BUY":
                continue
            if sym not in buy_rows:
                buy_rows[sym] = t

        facts: dict[str, dict] = {}
        for p in positions:
            sym = p.symbol
            entry = p.avg_entry
            cur = p.current_price

            # Find the last executed BUY in the DB for this symbol to derive
            # target/stop/days_held. Falls back to the morning row if present.
            buy = buy_rows.get(sym)
            if not buy:
                try:
                    buy = self.db.get_symbol_last_buy(sym)
                except Exception:
                    buy = None

            stop_loss = float((buy or {}).get("stop_loss") or 0)
            take_profit = float((buy or {}).get("take_profit") or 0)
            # Keep the ENTRY stop before the live-stop override below. It is
            # the R-multiple's denominator: the bet that was actually made,
            # not the level a trail later ratcheted it to (audit §1.4).
            initial_stop = stop_loss

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
                if take_profit and entry and take_profit != entry:
                    progress_pct = (cur - entry) / (take_profit - entry) * 100

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
                if progress_pct is None or not pinned_horizon or days_held is None:
                    pace_status = "unavailable_no_pinned_horizon"
                elif days_held < max(1, pinned_horizon / 3):
                    # Below one third of the pinned horizon the metric is
                    # mathematically meaningless — a thesis given 15 sessions
                    # cannot be "behind schedule" on day 2, and reading it as
                    # such is exactly how a day-5 position gets sold for "not
                    # progressing".
                    pace_status = "too_early"
                else:
                    time_fraction = days_held / pinned_horizon
                    if time_fraction > 0:
                        pace = progress_pct / (time_fraction * 100)
                        pace_status = "measured"

            # Distance-to-stop / distance-to-target as % of current price.
            dist_stop_pct = None
            dist_target_pct = None
            if stop_loss and cur > 0:
                dist_stop_pct = (cur - stop_loss) / cur * 100
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
                "weight_pct": weight_pct,
                "parabolic_flag": parabolic_flag,
                "drift_flag": drift_flag,
                "target_breach_flag": target_breach_flag,
                "atr_pct": atr_pct,
                "stop_distance_atrs": stop_distance_atrs,
            }
        return facts

    #: Metric keys snapshotted after every review and compared on the next one.
    #: Kept deliberately small — these are the numbers a "stalling" claim is
    #: actually about, and every one of them has a defined direction.
    _REVIEW_METRIC_KEYS = (
        "thesis_progress_pct", "distance_to_stop_pct", "r_multiple", "pace",
        "days_held", "expected_horizon_sessions", "setup_type", "pace_status",
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
        """Unified entry for both midday (13:00 ET) and close (15:30 ET).

        Same memory layers, same schema, same agent. Session bias is injected
        via prompt language driven by `session_type`. Everything else — force
        de-lever / auto take-profit / ex-div / news / earnings / LLM review /
        emergency liquidate / execution / reconcile — is identical.
        """
        if session_type not in ("midday", "close"):
            raise ValueError(f"run_position_review: unknown session_type {session_type!r}")

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
        self.db.sync_positions(positions)

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

        # Hard circuit breaker: if the session is already through the daily-loss
        # limit, bypass all LLM/news/earnings work and force-liquidate
        # immediately. This keeps the deterministic safety path alive even when
        # the reviewer model/provider is unavailable.
        daily_pnl = total_value - last_equity
        loss_violation = self.risk_engine.check_daily_loss(last_equity, daily_pnl)
        if loss_violation and positions:
            logger.warning(
                "%s risk alert before LLM review: %s — bypassing reviewer and force-closing all positions",
                session_type.capitalize(),
                loss_violation.message,
            )
            orders = self._midday_emergency_liquidate(positions, loss_violation, run_id)
            self._reconcile_fills()
            return {
                "status": "emergency_sold",
                "session": session_type,
                "positions": len(positions),
                "review": None,
                "orders": orders,
                "run_id": run_id,
            }

        # 1b. Auto take-profit (midday only — close is too near EOD to start
        # a partial-trim cycle that won't finish). At close, LLM handles trims
        # explicitly via the reasoning chain.
        auto_tp_orders: list[dict] = []
        blocked_position_symbols: set[str] = set()
        if session_type == "midday":
            auto_tp_orders = self._auto_take_profit(positions, run_id)
            if auto_tp_orders:
                blocked_position_symbols = self._wait_for_midday_auto_tp_orders(auto_tp_orders)
                # Refresh account + positions after auto-TP.
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
                self.db.sync_positions(positions)

        # 1c. Ex-dividend stop adjustment (both sessions — a dividend tomorrow
        # is still a dividend tomorrow no matter which session looks at it).
        exdiv_orders = self._handle_ex_dividends(positions, run_id)

        # Auto-TP/ex-dividend actions and all protection reconciliation above
        # are deterministic. A latched paid-analysis breaker stops only at
        # this boundary, before news/reviewer model requests.
        orders = list(forced_orders) + list(auto_tp_orders) + list(exdiv_orders)
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
            already_trimmed_today = self._symbols_already_trimmed_today()

            yesterday_insights = self.db.get_latest_insights(before_date=session_date_key())
            recent_performance = self._compute_recent_performance(last_equity)

            try:
                review, md_result = self.position_reviewer.review(
                    positions=review_positions,
                    macro_summary=macro_summary,
                    cash_balance=review_cash,
                    reserve_balance=reserve_balance,
                    total_value=total_value,
                    session_type=session_type,
                    position_facts=position_facts,
                    metric_deltas=metric_deltas,
                    morning_trades=morning_trades,
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
                )
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

            # Risk check: if daily loss limit breached, force-sell all. Else:
            # dispatch the LLM's per-position action list.
            daily_pnl = total_value - last_equity
            loss_violation = self.risk_engine.check_daily_loss(last_equity, daily_pnl)
            if loss_violation:
                # Review fix: this branch previously FELL THROUGH to the park
                # bookend — the system would buy SGOV with ~all equity minutes
                # after force-selling everything, and the next intra tick
                # would emergency-sell the fresh SGOV lot (spurious 🚨 alert +
                # a full round-trip on the worst possible day). Mirror the
                # pre-review breaker: reconcile and return, never park.
                #
                # audit round 2: refresh positions first — the locals here
                # date from BEFORE the LLM review (minutes of crash tape ago);
                # emergency limits priced off stale current_price can be
                # unfillable on the very day fills matter most.
                try:
                    fresh_positions = self.broker.get_positions()
                    if fresh_positions:
                        positions = fresh_positions
                except Exception as e:  # noqa: BLE001
                    logger.warning("post-review breach: position refresh failed "
                                   "(using pre-review snapshot): %s", e)
                orders.extend(self._midday_emergency_liquidate(
                    positions, loss_violation, run_id,
                ))
                self._reconcile_fills()
                return {
                    "status": "emergency_sold",
                    "session": session_type,
                    "positions": len(positions),
                    "review": review.model_dump() if review else None,
                    "orders": orders,
                    "run_id": run_id,
                    "stop_coverage_gaps": coverage_gaps,
                    # Spec §11.2 — gross exposure and its ceiling.
                    "leverage": dict(ctx.leverage),
                }
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
                )
                orders.extend(self._midday_execute_llm_actions(
                    review_positions, review, run_id,
                    blocked_symbols=blocked_position_symbols,
                    already_trimmed_today=already_trimmed_today,
                    metric_deltas=metric_deltas,
                    risk_vetoed_symbols=risk_vetoed,
                    position_facts=position_facts,
                ))

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
            # Spec §11.2 — gross exposure, its ladder-resolved ceiling and the
            # distance to forced liquidation, for the operator alert.
            "leverage": dict(ctx.leverage),
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
                smart_money_refresh = self.smart_money_provider.refresh()
                logger.info("SEC Form 4 refresh: %s", smart_money_refresh)
            except Exception as exc:
                logger.warning("SEC Form 4 refresh failed softly: %s", exc)
                smart_money_refresh = {
                    "status": "provider_error",
                    "error": type(exc).__name__,
                }

        try:
            reports = self.earnings_provider.check_and_fetch(
                self.config.trading.universe,
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
            return {"status": "analysis_error", "run_id": run_id, "error": str(e)}

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
        return {
            "status": "preprocessed",
            "run_id": run_id,
            "analyzed": analyzed_count,
            "confirmed": confirmed,
            "failed": len(failed_reports),
            "smart_money_refresh": smart_money_refresh,
        }

    def run_intra_check(self) -> dict:
        """Lightweight intra-session circuit-breaker check (no LLM calls).

        Scheduled between morning and midday (typically 12:00 ET) to catch a
        flash crash that would otherwise accumulate unchecked through the
        busiest trading hour. Only one rule: daily P&L vs loss limit. If
        breached, emergency-sell every position. Runs in ~5 seconds; OK for
        a 30-minute cadence if the user wants even tighter coverage.
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
        coverage_gaps: list[dict] = []
        try:
            coverage_gaps = self._reconcile_stop_coverage()
        except Exception as exc:  # noqa: BLE001
            logger.warning("intra coverage reconcile failed (non-fatal): %s", exc)
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
        daily_return_pct = (daily_pnl / last_equity * 100) if last_equity > 0 else 0
        logger.info(
            "Intra snapshot: equity=$%.2f, last_close=$%.2f, pnl=$%.2f (%.2f%%), positions=%d",
            total_value, last_equity, daily_pnl, daily_return_pct, len(positions),
        )

        loss_violation = self.risk_engine.check_daily_loss(last_equity, daily_pnl)
        if not loss_violation or not positions:
            result = {
                "status": "ok",
                "daily_pnl": daily_pnl,
                "daily_return_pct": daily_return_pct,
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
            return result

        logger.warning(
            "INTRA RISK ALERT: %s — force-closing all %d positions",
            loss_violation.message, len(positions),
        )
        # Reconcile before per-symbol dedupe — see _midday_emergency_liquidate
        # for full rationale. Critical for intra specifically because intra
        # ticks every 30 min: a stale 'submitted' row from an earlier tick
        # whose limit got cancelled at the broker would otherwise lock out
        # every subsequent tick until end-of-day, silently disabling the
        # circuit breaker for the rest of the session.
        self._reconcile_fills()
        # Cancel the day's resting entry BUY limits BEFORE selling (audit
        # round 2): a DAY entry order left working would re-buy into the very
        # crash the breaker is liquidating — "force-close everything" must
        # mean pending intentions too. Best-effort; preserves protective legs.
        try:
            self.broker.cancel_open_entry_orders()
        except Exception as exc:  # noqa: BLE001
            logger.warning("emergency liquidate: entry-order cancel failed: %s", exc)
        orders: list[dict] = []
        pending_protections: list[dict] = []
        for p in positions:
            try:
                # Direction-aware forced close (long → SELL, short → BUY-
                # to-cover); see _midday_emergency_liquidate and
                # _forced_close_side_and_qty for the full rationale — this
                # loop used to be the near-verbatim twin of that one and
                # inherits the same gap fix.
                closing = self._forced_close_side_and_qty(p.qty)
                if closing is None:
                    logger.error(
                        "Intra emergency liquidate: %s has an "
                        "indeterminate position qty (%r) — refusing to "
                        "guess SELL vs BUY-to-cover. A wrong guess here "
                        "would ADD to the exposure instead of closing it. "
                        "Left untouched; needs operator attention.",
                        p.symbol, p.qty,
                    )
                    continue
                side, qty = closing
                action = "EMERGENCY_SELL" if side == "sell" else "EMERGENCY_COVER"
                if self.db.has_pending_action_for_symbol(p.symbol, action):
                    logger.info(
                        "Intra emergency %s: skipping %s — prior "
                        "%s submission still pending at broker",
                        side, p.symbol, action,
                    )
                    continue
                cushion = self._EMERGENCY_LIMIT_CUSHION_PCT
                emergency_limit = round(
                    p.current_price * ((1 + cushion) if side == "buy" else (1 - cushion)),
                    2,
                )
                # audit F1 review #1: snapshot -> persist WAL -> cancel.
                sale = self._submit_protected_sell(
                    symbol=p.symbol, qty=qty, limit_price=emergency_limit,
                    reference_price=p.current_price, position_qty_before_sell=qty,
                    label=action, side=side,
                )
                if sale is None:
                    continue
                order, prot = sale
                pending_protections.append(prot)
                orders.append(order)
                self.db.insert_trade(
                    symbol=p.symbol, action=action, qty=qty,
                    price=emergency_limit,
                    reasoning=(
                        f"Intra-session daily-loss breach: {loss_violation.message}"
                    ),
                    run_id=run_id,
                    broker_order_id=order.get("id"),
                    fill_status="submitted",
                )
                logger.info(
                    "Intra emergency %s: %s %s @ limit $%.2f",
                    "sell" if side == "sell" else "buy-to-cover",
                    self._format_qty(qty), p.symbol, emergency_limit,
                )
            except Exception as e:
                logger.error("Intra emergency liquidate failed for %s: %s", p.symbol, e)

        # Wait + finalize: restore originals on any no-fill terminal.
        self._finalize_pending_protections(
            pending_protections, context="Intra emergency",
        )

        return {
            "status": "emergency_sold",
            "daily_pnl": daily_pnl,
            "daily_return_pct": daily_return_pct,
            "orders": orders,
            "run_id": run_id,
            "stop_coverage_gaps": coverage_gaps,
        }

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

    def _another_session_recently_active(self, run_id: str,
                                         within_minutes: float = 15.0) -> bool:
        """True when a DIFFERENT session wrote a trade row in the last
        `within_minutes` — i.e. a morning/midday/close run is probably
        mid-flight right now.

        Why this exists (2026-08-19, found while verifying the scheduling
        assumptions rather than assuming them): `scripts/run_if_et_window.sh`
        deliberately exempts `intra_check` from the cross-mode session lock
        so the flash-crash circuit breaker fires on every 30-min tick "
        regardless of what else is running" — and it justifies that
        exemption explicitly on the grounds that all of intra_check's
        actions (force_delever / emergency_liquidate / P&L read) are
        IDEMPOTENT.

        Opening a NEW position is not idempotent. Without this guard the
        intraday scan could run concurrently with a morning run that is
        still executing: both snapshot the same positions and the same
        deployable cash, both size against caps computed from that stale
        pre-fill state, and the combined result can breach
        `max_position_pct` / `cash_only` even though each process's own
        deterministic gate passed. Loss protection keeps its exemption (it
        runs before this, and must never be gated); only the new
        opportunity-discovery path backs off — it simply waits for the next
        30-minute tick, which costs at most one tick of latency on a
        deliberately non-high-frequency feature.

        Fails CLOSED: a query failure returns True (skip the scan).
        """
        from datetime import datetime as _dt, timedelta, timezone
        import os
        import time as _time

        # The wrapper writes this owner record before Python starts, so it is
        # visible during the long research window before any trade row exists.
        # That closes the 09:30 race where morning and intra both used the same
        # stale cash/position snapshot and could independently authorize BUYs.
        owner_path = Path.home() / ".cache" / "quant-agent" / "active-session.lock" / "owner"
        if owner_path.exists():
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
                if owner_mode != "intra_check" and alive and 0 <= age <= 1800:
                    logger.info(
                        "Intraday scan: wrapper reports active %s session (pid=%d, age=%.0fs); "
                        "skipping paid opportunity discovery",
                        owner_mode, owner_pid, age,
                    )
                    return True
            except (OSError, ValueError, IndexError) as exc:
                logger.warning(
                    "Intraday scan: could not validate active-session owner (%s) — "
                    "skipping paid discovery fail-closed", exc,
                )
                return True
        try:
            rows = self.db.get_trades(today_only=True, limit=50)
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "Intraday scan: concurrent-session query failed (%s) — "
                "skipping the scan this tick (fail-closed)", e,
            )
            return True
        cutoff = _dt.now(timezone.utc) - timedelta(minutes=within_minutes)
        for row in rows:
            other = row.get("run_id") or ""
            if not other or other == run_id:
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
                logger.info(
                    "Intraday scan: session %s wrote a trade row within the "
                    "last %.0f min — another session is likely mid-flight; "
                    "skipping this tick to avoid concurrent position sizing",
                    other, within_minutes,
                )
                return True
        return False

    @contextlib.contextmanager
    def _intraday_scan_process_lock(self):
        """Non-blocking process-level mutex for the intraday scan.

        Yields True when this process holds the lock, False otherwise.

        Why (independent review finding, 2026-08-19): the DB-row-based
        `_another_session_recently_active` guard can only see a concurrent
        session AFTER that session has written a trade row. Two
        `intra_check` processes launched at nearly the same instant would
        both pass it during the window before either writes — and could
        then size BUYs against the same pre-fill snapshot, breaching
        `max_position_pct`.

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
        `mkdir`-based session lock, and it applies ONLY to the new
        opportunity-discovery path. Loss protection keeps its exemption and
        never touches this. The lock is released on process exit even if we
        are SIGKILLed, so a killed run cannot wedge it.
        """
        import fcntl

        lock_path = Path(self.config.storage.db_path).parent / ".intraday_scan.lock"
        fh = None
        acquired = False
        try:
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
            `_intraday_scan_process_lock`) or a morning/midday/close/
            intra_check session detected by
            `_another_session_recently_active` inside the body.
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

    def _carry_forward_macro(self) -> dict | None:
        """This morning's macro regime, for an intraday tick to reason inside.

        Read from the store rather than re-derived: the macro analyst already
        ran today and its call is on disk. Returns None when nothing is
        stored, which leaves the tick exactly as blind as it used to be
        rather than substituting a stale regime from a previous day —
        `load_last_state` is not date-scoped, so the freshness check is this
        method's job.
        """
        try:
            state = self.macro_store.load_last_state() or None
        except Exception as e:  # noqa: BLE001 — never fail a tick on carry-forward
            logger.warning("Intraday scan: macro carry-forward failed: %s", e)
            return None
        if not state:
            return None
        # Only today's regime may be carried into today's session. Yesterday's
        # is exactly the "citing stale evidence as if it ran this tick"
        # failure the blindfold was protecting against.
        stored_date = str(state.get("date") or state.get("as_of") or "").strip()[:10]
        if stored_date and stored_date != str(et_today()):
            logger.info(
                "Intraday scan: stored macro is from %s, not today — not carried",
                stored_date,
            )
            return None
        return dict(state)

    def _carry_forward_news(self):
        """This morning's news intelligence, re-validated from its stored dump.

        `load_daily_report` is already date-scoped to today, so no freshness
        check is needed here. A schema failure degrades to None rather than
        raising: a malformed cache must cost the tick its news context, never
        the deterministic loss protection that already ran.
        """
        try:
            report = self.news_store.load_daily_report()
            if not report:
                return None
            from src.models import NewsIntelligenceReport
            return NewsIntelligenceReport(**report)
        except Exception as e:  # noqa: BLE001
            logger.warning("Intraday scan: news carry-forward failed: %s", e)
            return None

    def _intraday_opportunity_scan_body(self, ctx: RunContext) -> dict:
        """Bounded intraday opportunity discovery (2026-08-19 fix).

        Runs on the existing intra_check cadence — no new systemd timer,
        no full morning research stack. One cheap bulk current-session
        snapshot call flags symbols that moved materially since the last
        close; only those (capped, cooldown-deduped against repeat churn)
        get real daily bars/indicators and a real tech_analyst call, then
        the SAME DecisionStage -> RiskStage -> ExecutionStage chain
        morning uses — no separate/duplicated decision logic, so PM's
        sizing rules, RM's veto authority and the deterministic gate all
        apply exactly as they do in the morning run. Bullish AND bearish
        setups both surface: the universe already includes the approved
        inverse ETFs (SH/SDS/PSQ/SQQQ), so a broad-market decline shows up
        as a qualifying move in those symbols the same way a rally shows
        up in a long candidate — no separate bearish code path needed.

        Returns a status dict at every early-exit point — never a bare
        None (2026-08-31 visibility fix; see `_run_intraday_opportunity_scan`
        for the full rationale). "intraday_scan_lock_contended" when
        `_another_session_recently_active` detects a concurrent session;
        "intraday_scan_no_opportunity" for every other early return (no
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

        # Second concurrency layer, complementing the process lock held by
        # the wrapper: the lock stops two *intra_check* processes, this
        # stops racing a morning/midday/close session, which runs as a
        # different process and so takes a different lock. intra_check is
        # deliberately exempt from the wrapper script's cross-mode session
        # lock (for the circuit breaker), so this path must check itself.
        if self._another_session_recently_active(ctx.run_id):
            return {"status": "intraday_scan_lock_contended", "run_id": ctx.run_id}

        universe = list(self.config.trading.universe)
        snapshots = self.broker.get_intraday_snapshots(universe)
        if not snapshots:
            return {"status": "intraday_scan_no_opportunity", "run_id": ctx.run_id}

        candidates: list[tuple[str, float]] = []
        for symbol in universe:
            snap = snapshots.get(symbol) or {}
            last = snap.get("last_price")
            prev = snap.get("prev_close")
            if not (isinstance(last, (int, float)) and isinstance(prev, (int, float))):
                continue
            if prev <= 0:
                continue
            move_pct = abs(last - prev) / prev * 100.0
            if move_pct < cfg.move_threshold_pct:
                continue
            if self._recently_intraday_evaluated(symbol, cfg.cooldown_hours):
                continue
            candidates.append((symbol, move_pct))

        if not candidates:
            return {"status": "intraday_scan_no_opportunity", "run_id": ctx.run_id}

        # Largest moves first, capped — bounded per-tick cost regardless of
        # how many symbols move on a broad market day; not a scan of
        # everything, a check of the few things that moved most.
        candidates.sort(key=lambda t: -t[1])
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

        symbols_data = []
        symbols_bars: dict[str, list] = {}
        for symbol in symbols:
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

        # Truthful current-session evidence for exactly the candidates being
        # analyzed (2026-08-19): the scan detects on live prices, so Tech
        # must see those same live prices — not just daily bars ending at
        # yesterday's close, which is what triggered the scan being
        # invisible to the analyst that had to judge it. Rendered by
        # `build_user_message` as an explicit INCOMPLETE-session block,
        # never as a completed daily bar.
        intraday_context = {
            s: snapshots[s] for s in symbols if s in snapshots
        }
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
        carried_news = self._carry_forward_news()
        ctx.data_status = {
            "tech": "partial" if failed_count else "ok",
            # `carried_from_morning` rather than `ok`: RiskStage's existing
            # degraded-sources advisory must still fire, because a regime call
            # from 09:30 IS weaker evidence at 14:00 than a fresh one. It is
            # not, however, no evidence, which is what `not_run_intraday`
            # asserted.
            "macro": "carried_from_morning" if carried_macro else "not_run_intraday",
            "news": "carried_from_morning" if carried_news else "not_run_intraday",
            "earnings": "not_run_intraday",
        }
        ctx.macro_analysis = carried_macro
        ctx.news_intel = carried_news
        ctx.earnings_results = []

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
            return early_exit

        orders = self.execution_stage.run(ctx)
        return {
            "status": "intraday_executed" if orders else "intraday_no_trades",
            "candidates": symbols, "orders": orders, "run_id": ctx.run_id,
        }

    def run_evening(self) -> dict:
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

        meta_result = self._maybe_run_quarterly_meta()
        missing_sessions = self._expected_sessions_missing_today()
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
            "max_daily_loss_pct": getattr(
                getattr(self.config, "risk", None), "max_daily_loss_pct", None,
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
        }

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
            # A legit PM-less completion (no_data / emergency_sold) records a
            # status marker — skip both probes for it.
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
