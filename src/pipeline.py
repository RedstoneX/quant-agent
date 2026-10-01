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
from src.risk.constants import SHORT_GAP_RISK_MULTIPLE_DEFAULT
from src.quantities import avg_dollar_volume, deployable_cash, dollar_volumes
from src.data.market import MarketDataProvider
from src.data.macro import MacroCoverage, MacroDataProvider
from src.data.event_calendar import FOMCCalendarProvider, MacroEventCalendarProvider
from src.data.news import NewsCoverage, NewsDataProvider
from src.data.news_store import NewsStore
from src.data.macro_store import MacroStore
from src.data.tech_store import TechStore
from src.agents.base import (
    AgentResult, BaseAgent, agent_log_kwargs, seat_acceptance_kwargs,
)
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
from src.risk.metrics import drift_flag as _drift_flag_check
from src.risk.metrics import unrealized_pnl_pct
from src.risk.rules import (
    RiskRuleEngine,
    position_weight_pct,
)
from src.execution.broker import (
    AlpacaBroker,
    _get_sector,
    _split_protective_qty,
)
from src.pipeline_context import PMFacts, RunContext, SessionType
# Step 1 of docs/PIPELINE_SPLIT_PLAN.md: these moved to a mixin module and are
# re-exported here because tests and other modules import them from `src.pipeline`.
from src.pipeline_protection import (  # noqa: F401
    ProtectionMixin,
    _WAL_SELL_SENTINEL,
    _classify_coverage_gap,
    _finite_float_or_none,
    _market_is_open_now,
    _position_notional,
    _price_is_through_stop,
    _reconciled_exit_action,
)
# Step 3 of docs/PIPELINE_SPLIT_PLAN.md: the Spec §11.2 de-levering ladder moved
# to a mixin module. `_optional_risk_number`/`_risk_number` travelled with it
# because the moved bodies read them and that module may not import this one;
# they are re-exported here, still used by `build_risk_config`, and still
# importable as `src.pipeline._risk_number`.
from src.pipeline_exits import (  # noqa: F401
    ExitEngineMixin,
    _CANONICAL_TRIGGER_NAMES,
    _CHART_VERIFIED_TRIGGER_NAMES,
    _HARD_TRIGGER_KEYWORDS,
    _actions_with_scan_fallback,
    _reason_claims_alignment_exit,
    _reason_cites_hard_trigger,
)
from src.pipeline_delever import (  # noqa: F401
    DeleverMixin,
    _optional_risk_number,
    _risk_number,
)
from src.pipeline_prompt_facts import (  # noqa: F401
    PromptFactsMixin,
    _PM_PROFILE_SYMBOL_CAP,
    _missed_ops_quality_metrics,
    _valuation_signal_from,
)
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


class SessionTerminated(BaseException):
    """The wrapper's `timeout` sent SIGTERM; unwind so `finally` blocks run.

    `scripts/run_if_et_window.sh` runs each mode under
    `timeout --kill-after=30 1200`, so a hung session gets SIGTERM and then,
    thirty seconds later, SIGKILL. Python's default SIGTERM handling ends the
    process on the spot and NO `finally` runs — which is how a morning tick
    killed at the PM→RM boundary (the observed death mode: 61/61 BUY-proposal
    days during the 6/30-7/15 relay outage) could leave the deferred §11.2
    gross ceiling unenforced.

    Raised from a handler installed only for the duration of the morning
    body, it converts that silent death into an ordinary unwind that spends
    part of the thirty-second grace window paying the session's outstanding
    safety debts. A `BaseException` on purpose: the body is full of
    `except Exception` guards that would otherwise swallow it and carry on
    inside a process that is about to be killed.
    """


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


# `HARD_BLOCK_RULES` now lives in `src/risk/rules.py`, beside the engine that
# emits the rule names, so the RISK SEAT'S RENDERER can classify an entry
# without importing the pipeline (which imports the seat — a cycle). Re-
# exported here because this is the name every caller and test already
# imports, and moving the import site would be churn with no benefit.
from src.risk.rules import HARD_BLOCK_RULES  # noqa: E402,F401



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
            max_position_risk_pct=_risk_number(
                getattr(config.risk, "max_position_risk_pct", None), 5.0,
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
    from src.config import RiskConfig
    _risk_cfg = getattr(config, "risk", None)

    def _declared_default(name: str, literal: float) -> float:
        """The default `RiskConfig` itself declares for `name`.

        The fallback literals below used to be hand-copied from
        `src/config.py`, and one of them silently rotted: this function
        passed 1.5 for `min_stop_atr_multiple` long after the declared
        default became 2.5 (2026-09-10), so any path reaching here with the
        setting ABSENT sized live stops against a floor nobody ratified. A
        literal repeated in two files is drift waiting to happen, so the
        declared default now WINS; the literal survives only as the last
        resort for a field `RiskConfig` declares with no default of its own
        (`max_position_pct` is required, so it has none).
        """
        field = RiskConfig.model_fields.get(name)
        if field is not None:
            declared = getattr(field, "default", None)
            if not isinstance(declared, bool) and isinstance(declared, (int, float)):
                return float(declared)
        return float(literal)

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
        default = _declared_default(name, default)
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
            # No `min_order_usd`: board item 183 deleted
            # `ConstructorConfig.min_order_usd` on 2026-09-26. Nothing in the
            # constructor read it — the one call that forwarded it reached an
            # argument `apply_gross_ceiling` has ignored since 2026-09-24.
            # Stage 3 (shorts) — the sizing haircut. A short's single-name
            # ceiling is `max_position_pct` above, the same as a long's.
            short_gap_risk_multiple=_risk_setting(
                "short_gap_risk_multiple", SHORT_GAP_RISK_MULTIPLE_DEFAULT,
            ),
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
            # 1.5 -> 2.5 on 2026-09-30 (board item 90). This fallback was
            # left behind by the 2026-09-10 base move and still named the
            # value the desk EXPLICITLY ABANDONED: 1.5 was the Sweeney MAE
            # fit to this desk's own ~2-week history, dropped both because
            # that window's seat outputs were later found to misreport
            # confidence/data quality AND because fitting a threshold to
            # past outcomes is barred outright (docs/OUTCOME.md, "No
            # arbitrary numbers, ever", the 2026-09-12 correction). Not
            # reachable on the production path today — a real `RiskConfig`
            # always carries the attribute and pydantic coerces the YAML —
            # so this is a stale constant, not a live defect, and it is
            # corrected rather than reported as one. `_risk_setting`'s own
            # docstring says it returns "the ratified default", and 2.5 is
            # the ratified default. The ledger gate cannot see this line:
            # `src/number_sources.py` names "fallback arguments" among the
            # shapes it structurally cannot scan, which is why every
            # fallback in this block is now pinned to its `RiskConfig`
            # field default by `tests/test_risk_setting_fallbacks.py`.
            min_stop_atr_multiple=_risk_setting("min_stop_atr_multiple", 2.5),
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


class TradingPipeline(ProtectionMixin, PromptFactsMixin, DeleverMixin, ExitEngineMixin):
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
        # Route 3's second-road substitute (2026-09-30). Same closure again,
        # so the credential can never disagree with the provider
        # `select_tertiary_route` picks. Empty switches the substitution off,
        # which BaseAgent reads as "keep the configured tertiary".
        _tertiary_alt_api_key = (
            _key_for(config.llm.tertiary_alt_model, config.llm.tertiary_alt_provider)
            if (config.llm.tertiary_alt_model or "").strip() else ""
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
            tertiary_alt_api_key=_tertiary_alt_api_key,
            tertiary_alt_provider=config.llm.tertiary_alt_provider,
            tertiary_alt_model=config.llm.tertiary_alt_model,
            provider=config.llm.tech_analyst_provider,
            provider_order=config.llm.get_provider_order("tech_analyst"),
            reasoning_effort=config.llm.reasoning_effort,
            structured_output=config.llm.structured_output,
            # The standing sheet states this seat's history depth back to it.
            # Passed from the SAME config object `market.get_ohlcv` is called
            # with, so the brief and the fetch cannot disagree (board item 168).
            lookback_days=config.trading.lookback_days,
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
            tertiary_alt_api_key=_tertiary_alt_api_key,
            tertiary_alt_provider=config.llm.tertiary_alt_provider,
            tertiary_alt_model=config.llm.tertiary_alt_model,
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
            tertiary_alt_api_key=_tertiary_alt_api_key,
            tertiary_alt_provider=config.llm.tertiary_alt_provider,
            tertiary_alt_model=config.llm.tertiary_alt_model,
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
        self.risk_engine = RiskRuleEngine(build_risk_config(config))
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
            tertiary_alt_api_key=_tertiary_alt_api_key,
            tertiary_alt_provider=config.llm.tertiary_alt_provider,
            tertiary_alt_model=config.llm.tertiary_alt_model,
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
            tertiary_alt_api_key=_tertiary_alt_api_key,
            tertiary_alt_provider=config.llm.tertiary_alt_provider,
            tertiary_alt_model=config.llm.tertiary_alt_model,
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
            tertiary_alt_api_key=_tertiary_alt_api_key,
            tertiary_alt_provider=config.llm.tertiary_alt_provider,
            tertiary_alt_model=config.llm.tertiary_alt_model,
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
            tertiary_alt_api_key=_tertiary_alt_api_key,
            tertiary_alt_provider=config.llm.tertiary_alt_provider,
            tertiary_alt_model=config.llm.tertiary_alt_model,
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
            tertiary_alt_api_key=_tertiary_alt_api_key,
            tertiary_alt_provider=config.llm.tertiary_alt_provider,
            tertiary_alt_model=config.llm.tertiary_alt_model,
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
            tertiary_alt_api_key=_tertiary_alt_api_key,
            tertiary_alt_provider=config.llm.tertiary_alt_provider,
            tertiary_alt_model=config.llm.tertiary_alt_model,
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
                # Board item 52 deleted the shared insider-Form4 dollar
                # floors from `SmartMoneyConfig`; that item is scoped to
                # Form 4 only, so this congressional-trading gate is left
                # unchanged by taking `CongressionalTradingProvider`'s own
                # default floors (same $100k/$250k values) instead of the
                # now-deleted config fields.
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
            tertiary_alt_api_key=_tertiary_alt_api_key,
            tertiary_alt_provider=config.llm.tertiary_alt_provider,
            tertiary_alt_model=config.llm.tertiary_alt_model,
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
            # Board item 218: the parity refusal is a TRIAL and must leave a
            # durable, numeric, per-symbol record or it cannot be judged.
            db=self.db,
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
        # Exit orders still working at the broker — see the attribute's own
        # comment above `_register_exit_settlement`.
        self._unsettled_exit_orders = {}

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
        cash-equivalent sweep vehicle. Both components are assets QAMC
        already owns, so the sum can never exceed equity and never creates
        leverage. NOTE (item 190): nothing sells the vehicle before the BUY
        phase any more, so the parked component is owned but not
        automatically converted; see docs/WORK.md item 190.

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
        unrelated to and unmoved by the ladder.

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


    @staticmethod
    def _forced_close_side_and_qty(position_qty: float) -> tuple[str, float] | None:
        """Direction-aware sizing for a FORCED close — the §11.2
        de-levering ladder's forced trim, or an operator kill. NOT the
        normal decision
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
        forced close exists to shed. Refusing and logging loudly beats
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
            ScreenThresholds.from_config(
                self.config, self._constructor_cfg_or_none(),
            ),
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
        open a new name now. Sessions are counted with the broker's
        holiday-aware calendar (item 165) — the plain weekday counter
        overstates the count by one per market holiday crossed, which
        skews this gate toward admitting names it should be rejecting.
        """
        from src.util.time import et_today

        disclosed = getattr(observation, "disclosure_date", None)
        if not isinstance(disclosed, date):
            return False
        horizon = int(self.config.risk.max_target_horizon_sessions)
        return self.broker.trading_sessions_held(disclosed, et_today()) <= horizon

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
                th=ScreenThresholds.from_config(
                    self.config, self._constructor_cfg_or_none(),
                ),
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
        invested_target_pct: float | None = None,
        correlation_matrix: dict[str, dict[str, float]] | None = None,
        cash: float | None = None,
        # Spec §11.2. The ladder-resolved gross-exposure ceiling for this
        # session. None falls back to the configured cap inside the engine —
        # a caller that forgets it still gets a ceiling, never none.
        gross_ceiling=None,) -> tuple[list[TradeDecision], list, list[str]]:
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
                pending_investment=pending_investment,
                pending_sector_investment=pending_sector_investment,
                pending_symbol_investment=pending_symbol_investment,
                correlation_matrix=correlation_matrix,
                cash=effective_cash,
                pending_cash_outflow=pending_cash_outflow,
                # Spec §11.2 — the execution half of the gross ceiling. The
                # sweep vehicle has already been split out of `positions`
                # above, so `cash_park_symbol` here is belt-and-braces for
                # any future caller that has not.
                gross_ceiling=gross_ceiling,
                pending_gross_investment=pending_gross_investment,
                cash_park_symbol=(sweeper.symbol if sweeper is not None else None),)
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
            # The band is the owner-set advisory band
            # (`deployment_gap.band_pct`), not an invented number — see
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
        1b. **An entry's `allocation_pct` may only be reduced — BUY and
           SHORT alike.** This seat exists to be MORE protective than the
           constructor, and nothing enforced that:
           `RiskModification.new_value` is unbounded. On an entry that adds
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

                # Guard 1 — the seat may NEVER shrink a protective exit.
                # Owner ruling 2026-09-24 (final): the risk seat can never
                # block OR reduce a protective exit (SELL/REDUCE/COVER). It
                # used to revert only an edit that drove the exit's
                # allocation_pct to <= 0 (a silent cancel); an edit from
                # 100% -> 50% sailed through and cut how much the desk sold to
                # reduce risk. Now ANY downward allocation_pct edit on an exit
                # is reverted — the exit keeps its intended size. An UPWARD
                # edit (selling more) is left alone; it only reduces risk. This
                # is checked BEFORE the candidate is built: a valid smaller
                # allocation_pct would otherwise sail straight through Pydantic.
                if (
                    decision.action in ("SELL", "REDUCE", "COVER")
                    and mod.field == "allocation_pct"
                    and decision.allocation_pct > 0
                    and float(mod.new_value) < decision.allocation_pct
                ):
                    reason = (
                        f"RM modification would REDUCE {mod.symbol}'s exit "
                        f"allocation_pct ({decision.allocation_pct:.2f} -> "
                        f"{mod.new_value:.2f}), shrinking a {decision.action} the "
                        f"desk is using to reduce risk. Reverted — the seat may "
                        f"never block or reduce a protective exit; it stays at "
                        f"its intended size. RM reason given: {mod.reason!r}"
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

                # Guard 1b — an `allocation_pct` edit on an ENTRY (BUY *or*
                # SHORT) may only REDUCE. The Risk Manager's stated job at
                # this seat is to be MORE protective than the constructor;
                # nothing in the schema enforced that for this field
                # (`RiskModification.new_value` is unbounded and
                # `TradeDecision.allocation_pct` only clamps 0-100), so a
                # larger number sailed through as a "protection". Compounding
                # it, on an entry that ADDS to a name already held the field
                # is an INCREMENT on top of the existing position, so an
                # upward edit grows it by more than the number suggests —
                # observed 2026-09-18, where an edit the seat believed cut a
                # name to 30% left it at 50.8%. The prompt now states the
                # increment and the resulting weight; this guard is the part
                # that holds regardless of what the model reasons.
                #
                # Board item 155 (2026-09-26): SHORT was folded in HERE. It
                # used to be policed one layer out, by
                # `_revert_entry_size_increases` in `src/pipeline_stages.py`,
                # only because this file was locked by another workstream on
                # 2026-09-18 — never because two enforcement points for one
                # rule were the right shape. The outer sweep is deleted. This
                # is now the SINGLE enforcement point, and
                # `tests/test_pipeline_stages.py` fails the build if a second
                # one reappears. A short is sized by the explicit mirror of
                # the long clamp and opens new risk exactly as a BUY does, so
                # one condition covers both sides.
                if (
                    decision.action in ("BUY", "SHORT")
                    and mod.field == "allocation_pct"
                    and float(mod.new_value) > decision.allocation_pct
                ):
                    reason = (
                        f"RM modification would INCREASE {mod.symbol}'s "
                        f"{decision.action} allocation_pct "
                        f"({decision.allocation_pct:.2f} -> "
                        f"{mod.new_value:.2f}). Reverted — the risk seat may "
                        f"only reduce an entry's size, never enlarge it; on "
                        f"an add this field is an increment, so an upward "
                        f"edit grows the position by more than the number "
                        f"reads. RM reason given: {mod.reason!r}"
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

                # Guard 3 (board item 134) — a `stop_loss` or `entry_price`
                # edit on a BUY/SHORT must be reconciled back to the position
                # SIZE. The constructor sized the position for the ORIGINAL
                # stop distance: `shares = equity*risk_pct / |entry - stop|`,
                # so `allocation_pct` and the stop distance are two halves of
                # one granted dollar-risk budget. Guards 1b and 2 police
                # `allocation_pct` and the stop's noise band, but NOTHING
                # recomputed the size after
                # a stop/entry edit — so widening the stop (larger
                # |entry - stop|) while `allocation_pct` stayed fixed shipped a
                # position whose real dollar risk (shares x new stop distance)
                # EXCEEDED the granted budget, unflagged. This reconciles the
                # size so a wider stop shrinks the position and can never
                # enlarge dollar risk beyond what the pre-edit ticket carried
                # (desk doctrine: "wider stop -> smaller position, never larger
                # dollar risk"). A TIGHTER stop is deliberately NOT allowed to
                # auto-enlarge the position — the seat's remit is to be more
                # protective, and every sibling guard here fails toward the
                # smaller size — so the reconciliation takes the SMALLER of the
                # original and the recomputed allocation.
                if decision.action in ("BUY", "SHORT") and mod.field in (
                    "stop_loss", "entry_price",
                ):
                    reconciled_alloc = self._reconcile_size_to_risk_budget(
                        decision, updated_decision,
                    )
                    if (
                        reconciled_alloc is not None
                        and reconciled_alloc < updated_decision.allocation_pct
                    ):
                        logger.info(
                            "Risk mod size reconciled for %s: %s edit widened "
                            "risk-per-share, allocation_pct %.2f -> %.2f to hold "
                            "dollar risk within the granted budget",
                            mod.symbol, mod.field,
                            updated_decision.allocation_pct, reconciled_alloc,
                        )
                        updated_decision = updated_decision.model_copy(
                            update={"allocation_pct": reconciled_alloc},
                        )

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
    def _reconcile_size_to_risk_budget(
        original: TradeDecision,
        modified: TradeDecision,
    ) -> float | None:
        """The `allocation_pct` that keeps `modified`'s dollar risk at or below
        the dollar risk the pre-edit `original` ticket carried, or None when it
        cannot be measured.

        Board item 134. The constructor sizes a position so the number of
        shares put its stop distance's worth of loss at exactly the granted
        risk budget: `shares = equity*risk_pct / |entry - stop|`, and
        downstream execution spends the resulting `allocation_pct` as
        `qty = equity * allocation_pct/100 / entry`. Substituting, the fraction
        of equity a ticket risks is

            dollar_risk / equity = allocation_pct/100 * |entry - stop| / entry

        — it depends only on the ticket's own fields, not on the book value.
        So the pre-edit ticket's own risk fraction is the budget to preserve
        (it is already the constructor's granted risk after every single-name,
        portfolio and sector clamp, so it never over-states what was granted).
        Solving that identity for the allocation that reproduces the SAME
        fraction under the edited entry/stop gives the reconciled size:

            reconciled = original_alloc * (|e0 - s0|/e0) / (|e1 - s1|/e1)

        The short-side gap-risk haircut the constructor applies to
        risk-per-share cancels in this ratio, so shorts need no special case.
        Returns the reconciled allocation only; the caller takes the smaller of
        it and the current allocation so a tighter stop can never auto-enlarge
        the position. None when either ticket is geometrically degenerate
        (non-finite or non-positive entry, or a zero pre/post risk-per-share),
        in which case the caller leaves the size untouched.

        Precision of the preserved budget:

        - For a STOP edit the reconciliation is EXACT: the entry is unchanged,
          so `allocation_pct` and stop distance are the only moving parts and
          the identity holds against whatever entry execution ultimately sizes
          off.
        - For an ENTRY edit it is exact ONLY when execution's sizing
          denominator equals the edited entry. Execution actually sizes off
          `sizing_price = max(today_print, entry)` for a long / `min(...)` for
          a short (`_place_buy_with_sizing` in `pipeline_stages.py`), so under
          market drift the denominator differs and the preserved budget is
          APPROXIMATE. It is bounded on the high side by the execution-time 5%
          `_qty_by_risk_budget` ceiling and this reconciliation only ever
          REDUCES the allocation, so the approximation can under-risk but never
          over-risk.

        Scope: this guarantee covers the RM EDIT only. An execution-time ATR
        stop-widen applied AFTER this stage is reconciled solely against that
        same 5% `_qty_by_risk_budget` ceiling (pre-existing behaviour, not
        introduced here) — this method does not and cannot re-run for it.
        """
        e0, s0 = original.entry_price, original.stop_loss
        e1, s1 = modified.entry_price, modified.stop_loss
        alloc0 = original.allocation_pct
        rps0 = abs(e0 - s0)
        rps1 = abs(e1 - s1)
        values = (e0, e1, rps0, rps1, alloc0)
        if not all(isinstance(v, (int, float)) and math.isfinite(v) for v in values):
            return None
        if e0 <= 0 or e1 <= 0 or rps0 <= 0 or rps1 <= 0:
            return None
        original_risk_fraction = alloc0 * (rps0 / e0)
        new_risk_per_alloc = rps1 / e1
        reconciled = original_risk_fraction / new_risk_per_alloc
        if reconciled >= alloc0:
            # A tighter stop (or unchanged risk-per-share) — never auto-enlarge;
            # keep the ticket's own already-valid size untouched.
            return alloc0
        # FLOOR to 2 dp rather than round: rounding could nudge the size back UP
        # a hundredth of a percent and, with it, dollar risk a hair over the
        # pre-edit budget. Flooring guarantees the reconciled size never exceeds
        # the exact budget-preserving allocation.
        return math.floor(reconciled * 100) / 100

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


    @staticmethod
    def _refuse_queued_earnings_buys(
        decisions: list[TradeDecision],
        earnings_results: list[dict],
    ) -> list[TradeDecision]:
        """REFUSE every BUY on a symbol whose just-filed report reached this
        session unread. Board item 186, 2026-10-01.

        MISSING EVIDENCE, NOT LOW CONVICTION. `queued=True` is set in one
        place only (the session-time earnings fetch below): a filing the
        pre-market preprocess failed to pick up and analyse. It records an
        operations failure of this desk's own pipeline, not a seat verdict
        and not a market event, and this gate is argued on exactly those
        terms — the desk meant to read the report before deciding, it did
        not, and it declines to buy into the gap. It is NOT the conviction
        bar and does not touch it: see `risk.rules.unread_filing_block_reason`
        for why routing it through R7 would be wrong and would also change
        behaviour on names the desk already holds.

        WHAT THIS REPLACED, AND WHY THE NUMBER IS GONE. Until now this was a
        clamp: the resulting position weight on such a name was held to 5% of
        the book. That 5 had no source. It was researched to a definite
        negative (the closest published quantity, the ~5.07% average
        one-day absolute earnings-announcement move, measures the size of a
        MOVE and not a share of a BOOK, and the desk's own per-trade risk
        envelope runs forward to a weight near 100%, so it cannot be the
        cap's parent). Under the owner's 2026-09-30 ruling a global constant
        governing risk is a defect to be removed, not an appetite to be
        answered, so the condition is REFORMULATED instead of re-derived and
        no percentage survives. REFUSING rather than sizing down is
        REASONING, not a quoted rule: the entry bar already refuses a name
        whose technical read is merely ABSENT, so requiring the filing to
        have been read before buying is consistent with how this desk
        already treats evidence it does not have, and the standing doctrine
        that all five seats must be right to ENTER is what makes an entry
        the right thing to withhold.

        BUY-ONLY, and silent about everything else. A SELL is untouched, a
        name whose filing has been read is untouched, and nothing already
        held is sold or reclassified — refusing to BUY is not a decision to
        SELL, the same contract `agreement_refuses_trade` carries.

        The refusal is recorded durably per symbol by
        `pipeline_stages._record_queued_earnings_refusals`, which reads the
        before/after lists, so a refused BUY can be judged later from the
        record rather than from argument.
        """
        queued_symbols = {
            (ea.get("symbol") or "").strip().upper()
            for ea in earnings_results
            if ea.get("queued") and not ea.get("analysis")
        }
        queued_symbols.discard("")
        if not queued_symbols:
            return decisions

        from src.risk.rules import unread_filing_block_reason
        kept: list[TradeDecision] = []
        for d in decisions:
            if d.action != "BUY" or d.symbol.upper() not in queued_symbols:
                kept.append(d)
                continue
            logger.warning(
                "Unread-filing refusal: dropping %s BUY %.2f%% — %s",
                d.symbol, d.allocation_pct, unread_filing_block_reason(d.symbol),
            )
        return kept

    def _is_trading_day(self) -> bool:
        try:
            return self.broker.is_trading_day()
        except Exception as exc:
            logger.warning("Trading-day check failed; assuming market closed: %s", exc)
            return False


    # Realized-exit actions whose post-exit trajectory is worth auditing.
    # SWEEP_SELL is deliberately absent — parking churn is not a decision.
    # STOP_OUT (2026-08-28 ONDS/CCJ) belongs here even though it is not a
    # reviewer decision — precisely BECAUSE it isn't one: "did the market
    # force us out right before a bounce" is exactly the question this
    # audit exists to answer, and a forced exit is where the answer is
    # most likely to be uncomfortable.
    # TAKE_PROFIT stays for HISTORICAL rows: the auto trim that wrote it was
    # deleted 2026-09-12 and nothing writes the label any more.
    # RECONCILED_EXIT (item 173(a)): a recovered broker exit whose order_type
    # could not be proven a protective stop. Before 173(a) every recovered
    # exit was labelled STOP_OUT and thus audited here; keeping it out would
    # drop real closed exits from decision-quality auditing — exactly the
    # "did the market force us out before a bounce" question this audit
    # exists to answer.
    _EXIT_AUDIT_ACTIONS = (
        "SELL", "REDUCE", "EMERGENCY_SELL", "FORCE_DELEVER", "TAKE_PROFIT",
        "STOP_OUT", "RECONCILED_EXIT",
    )

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
            self._record_short_overnight_gaps(snapshot)
        except Exception as exc:  # noqa: BLE001
            logger.error("local positions table refresh failed: %s", exc)

    def _record_short_overnight_gaps(self, positions) -> None:
        """Store the adverse overnight gap suffered by each held SHORT.

        SHORT-SIDE GAP EVIDENCE, RECORDING ONLY — item 186. The short-side
        sizing haircut is unsourced and two attempts to read it off the
        instrument have failed; both failed because this desk has never
        kept a record of what a short actually suffers overnight. Bars are
        fetched live and discarded and there is no OHLCV table, so the
        evidence has to be captured beside the trade while the trade is
        open. Nothing reads this back: no threshold, no gate, no sizing
        change. See `TradeStore.record_overnight_gap` for the hard limit on
        its use.

        Shorts only, because only a short's loss above its stop is
        unbounded and only the short-side multiple is the open question.
        Held shorts are a handful at most, so the two-bar fetch per name is
        cheap. Fail-soft per symbol and as a whole: a recording problem
        must never disturb a trading session.
        """
        for p in positions or []:
            try:
                if float(getattr(p, "qty", 0) or 0) >= 0:
                    continue
                bars = self.market.get_ohlcv(p.symbol, 7) or []
                if len(bars) < 2:
                    continue
                prev_bar, today = bars[-2], bars[-1]
                self.db.record_overnight_gap(
                    p.symbol, prev_bar.close, today.open, str(today.date),
                )
            except Exception:  # noqa: BLE001
                logger.debug(
                    "overnight-gap recording skipped for %s",
                    getattr(p, "symbol", "?"), exc_info=True,
                )

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
                **seat_acceptance_kwargs("agent_failure" if not intel_report else None),
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

    def _constructor_cfg_or_none(self):
        """The LIVE `ConstructorConfig`, for rules that must agree with the
        stops the desk actually places (board item 185: the universe
        screen's volatility ceiling is 1 / the widest stop this object can
        produce). `None` when no constructor has been built -- some tests
        drive a bare pipeline -- and the caller then falls back to
        `config.risk` plus the class defaults.
        """
        return getattr(
            getattr(self, "portfolio_constructor", None), "cfg", None,
        )

    def _sweep_symbol(self) -> str | None:
        """The configured cash-park vehicle, or None when sweeping is off.

        Taken from `cash_sweep.symbol` rather than hardcoded to "SGOV" —
        the setting already exists and an operator who changes the vehicle
        must not have to change the risk engine too.
        """
        sweeper = self._sweeper()
        return getattr(sweeper, "symbol", None) if sweeper is not None else None

    def _install_sigterm_unwind(self, context: str):
        """Make the wrapper's SIGTERM raise instead of killing silently.

        Returns whatever handler was installed before, for the caller to
        restore. Returns None — and changes nothing — when signals cannot be
        set here (not the main thread, or a platform without SIGTERM), which
        is the ordinary case under pytest's worker threads.
        """
        import signal
        try:
            return signal.signal(
                signal.SIGTERM,
                lambda *_: (_ for _ in ()).throw(
                    SessionTerminated(f"{context}: SIGTERM from the run wrapper")
                ),
            )
        except (ValueError, OSError, AttributeError, RuntimeError) as exc:
            logger.debug("SIGTERM unwind not installed for %s: %s", context, exc)
            return None

    def _restore_sigterm(self, previous) -> None:
        if previous is None:
            return
        import signal
        try:
            signal.signal(signal.SIGTERM, previous)
        except (ValueError, OSError, AttributeError, RuntimeError):
            pass


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
        run_id: str,
        *,
        orders: list[dict] | None = None,
        error: BaseException | None = None,
        filings_waiting: list[dict] | None = None,
    ) -> dict:
        # `filings_waiting` (2026-09-24): when the cost circuit trips after
        # `run_earnings_preprocess` has already computed which filings were
        # queued for the LLM reader, that backlog was silently dropped here
        # -- the suspended payload carried no earnings keys at all, so
        # `_append_earnings_body` rendered "analyzed:0 confirmed:0
        # failed:0" for a run that actually found N new filings. Passing it
        # through lets the owner-facing message say "suspended, N filing(s)
        # waiting" instead of implying nothing happened.
        waiting = list(filings_waiting or [])
        return {
            "status": "paid_analysis_suspended",
            "run_id": run_id,
            "orders": list(orders or []),
            "error": str(error or "mandatory cost circuit is open"),
            "paid_analysis_suspended": True,
            "filings_waiting": waiting,
            "filings_waiting_count": len(waiting),
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
        """The suspension return payload.

        It used to re-run an account-level loss check first. That
        whole mechanism was removed 2026-09-20 on owner instruction
        (docs/INCIDENT_HISTORY.md, retired item 32): per-position stops are
        the desk's loss protection now, and they live at the broker rather
        than depending on this process reaching this line.

        KNOWN RESIDUE, deliberately not chased in that change: `session`
        and `where` are now unused here, and the name still says "after
        late safety" when there is no late safety check left. Eleven call
        sites pass both. Renaming the method and dropping two keyword
        arguments across all eleven is churn with no behavioural effect, so
        it was left for whoever next touches this path — it is recorded
        here rather than silently tolerated.
        """

        existing_orders = list(orders or [])
        payload = self._paid_suspended_payload(
            run_id, orders=existing_orders, error=error,
        )
        if extra:
            payload.update(extra)
        # 2026-09-30 (item 199): this is the third legit PM-less completion
        # alongside `no_data` and `evidence_gate_skip` above, both of which
        # already call `_dc.write_status` so the evening dead-man probe
        # skips its "research ran, PM never did — killed mid-run?" guess.
        # This path never did, so a same-day cost-circuit suspension the
        # owner was already told about at the time (the morning session's
        # own "SUSPENDED" push) re-arrived ~16h later relabelled as a
        # mystery kill. Morning-only: `read_status`/the sharper probes in
        # `_expected_sessions_missing_today` only ever key on "morning".
        if session == "morning":
            from src import decision_checkpoint as _dc

            _dc.write_status("morning", "paid_analysis_suspended")
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
            # Stamp the classification with WHEN and WHICH RUN before it is
            # persisted. Owner ruling 2026-10-01 (sell what fails the fresh
            # bar) makes "was this seat read in THIS run?" something a sell
            # can rest on, and it must be a recorded fact, not an inference
            # drawn from the shape of the row. Records only — no threshold,
            # nothing gated. Fail-soft on the prior-read lookup: an unknown
            # age is reported as unknown, never as fresh.
            prior = {}
            try:
                if getattr(self, "db", None) is not None:
                    prior = self.db.last_fresh_seat_reads(
                        seats=list(verdict.freshness.data_status)
                    )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "evidence gate: prior seat-read lookup failed (%s) — "
                    "carried seats will report an unknown age", exc,
                )
            stamped = verdict.freshness.stamped(
                run_id=getattr(ctx, "run_id", None),
                mode=str(getattr(ctx, "session", "") or "") or None,
                prior_reads=prior,
            )
            self._last_evidence_freshness = stamped.to_evidence()
            self._last_decision_data_status = dict(verdict.data_status)
            ctx.evidence_freshness = dict(self._last_evidence_freshness)
        except Exception as exc:  # noqa: BLE001 — never break the decision
            logger.warning("evidence gate: freshness record failed: %s", exc)
        logger.info("EVIDENCE FRESHNESS — %s", verdict.freshness.summary)

        evidence = verdict.to_evidence()
        _record(None, evidence.pop("outcome"), evidence.pop("reason"), **evidence)
        self._record_name_coverage(ctx, _record)
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
        self._last_account_snapshot = None
        result = self._run_morning_body()
        self._attach_pnl(result)
        self._attach_evidence_freshness(result)
        self._attach_universe_changes(result)
        self._persist_session_report("morning", result)
        return result

    def _record_name_coverage(self, ctx, _record) -> None:
        """Write down, per candidate name, which seats answered ABOUT it.

        The counting half of docs/WORK.md item 20. It is a RECORD, not a
        bar: no ratio, no minimum, nothing refused here. Both attempts at
        deriving a coverage bar failed and the reasons are written down in
        `src/evidence_gate.py` beside `name_coverage`; this is the
        instrument that would let one be measured from the desk's own data
        instead of picked. Fail-soft — a forensic record must never be able
        to break the trading path it reports on.
        """
        from src import evidence_gate

        try:
            seat_symbols: dict[str, set] = {}
            seat_symbols["tech"] = {
                getattr(a, "symbol", "") for a in (getattr(ctx, "analyses", None) or ())
            }
            seat_symbols["earnings"] = {
                (r.get("symbol") if isinstance(r, dict) else getattr(r, "symbol", ""))
                for r in (getattr(ctx, "earnings_results", None) or ())
            }
            smart: set = set()
            for bucket in ("smart_money_observations", "smart_money_findings"):
                for item in (getattr(ctx, bucket, None) or ()):
                    smart.add(
                        item.get("symbol") if isinstance(item, dict)
                        else getattr(item, "symbol", "")
                    )
            seat_symbols["smart_money"] = smart
            intel = getattr(ctx, "news_intel", None)
            if intel is not None:
                # Absent `news_intel` means the news seat recorded no
                # per-name coverage at all, which `name_coverage` reports as
                # uncovered rather than assuming complete.
                seat_symbols["news"] = set(getattr(intel, "stock_news", None) or {})

            universe: set = set()
            for names in seat_symbols.values():
                universe |= {n for n in names if n}
            universe |= {
                str(s) for s in (getattr(ctx, "admitted_symbols", None) or set())
            }
            # HELD NAMES ARE IN THE UNIVERSE (board item 220). The rule binds
            # on STAYING as well as entering, and a held name that no seat
            # answered about this review was previously absent from this
            # record entirely — the one case where "no row" meant "nothing to
            # see" rather than "nobody looked".
            for pos in (getattr(ctx, "positions", None) or ()):
                sym = (
                    pos.get("symbol") if isinstance(pos, dict)
                    else getattr(pos, "symbol", "")
                )
                if sym:
                    universe.add(str(sym))
            # A name whose technical row came back unreadable may be in no
            # other list at all, and it is the one name that must not vanish.
            unreadable_by_seat = {
                "tech": set(getattr(ctx, "tech_unreadable", None) or {}),
            }
            asked_no_answer_by_seat = {
                "tech": set(getattr(ctx, "tech_unanswered", None) or set()),
            }
            universe |= {str(s) for s in unreadable_by_seat["tech"]}
            universe |= {str(s) for s in asked_no_answer_by_seat["tech"]}
            try:
                universe |= {str(s) for s in self.config.trading.universe}
            except Exception:  # noqa: BLE001 — config shape is not this record's job
                pass

            coverage_by_name = evidence_gate.name_coverage(
                universe, seat_symbols,
                unreadable_by_seat=unreadable_by_seat,
                asked_no_answer_by_seat=asked_no_answer_by_seat,
            )
            for name, coverage in coverage_by_name.items():
                record = coverage.to_evidence()
                _record(
                    name,
                    "recorded",
                    record.pop("summary"),
                    stage="evidence_gate",
                    gate="name_coverage",
                    **record,
                )

            # The per-name reading of the owner's blocking-seat mandate,
            # carried out of here so the entry bar and the holding review
            # both read a MISSING seat rather than an absent objection.
            # Nothing is refused here; the categorical refusals already
            # exist (`risk.rules.own_bar_block_reason` for entry, rotation's
            # `ineligible_hold` tier for the held side) and both already
            # treat "no technical read this review" as blocking.
            gaps = evidence_gate.names_missing_blocking_seat(coverage_by_name)
            ctx.name_coverage_blocking_gaps = dict(gaps)
            if gaps:
                logger.warning(
                    "evidence gate: %d name(s) have NO answer from a seat that "
                    "may stop the desk — treated as a missing seat, never as "
                    "agreement: %s%s",
                    len(gaps),
                    "; ".join(
                        f"{n}={','.join(seats)}" for n, seats in sorted(gaps.items())
                    ),
                    (
                        " (returned-but-unreadable: "
                        + ", ".join(sorted(unreadable_by_seat["tech"])) + ")"
                        if unreadable_by_seat["tech"] else ""
                    ),
                )
        except Exception as exc:  # noqa: BLE001 — never break the decision
            logger.warning("evidence gate: name coverage write failed: %s", exc)

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

    def _record_account_snapshot(self, total_value, last_equity) -> None:
        """Remember the account read this session already made, so the P&L
        block can be built from it on EVERY return path.

        The session takes exactly one broker account snapshot and then may
        leave by any of a dozen returns (no_trades, pm_agent_failure,
        paid_analysis_suspended, executed, ...). Before 2026-09-23 only the
        position-review and intra-check happy paths bothered to carry the
        P&L keys out, so the morning message the owner actually reads said
        "not available" while the same message printed the book it had just
        read. Recording the snapshot here, and attaching in the wrapper,
        makes the figure a property of "the account was read", not of which
        exit the run happened to take.
        """
        try:
            self._last_account_snapshot = (
                float(total_value), float(last_equity),
            )
        except (TypeError, ValueError):
            self._last_account_snapshot = None

    def _attach_pnl(self, result) -> None:
        """Fill the owner-facing P&L keys from this run's own account read.

        SAME basis and SAME source as the path that already worked — the
        broker's day-over-day change against `last_equity`, and
        `_total_pnl_since_reset` for the dated baseline (see
        `trader_feed._pnl_section_lines` for why "total" is dated). No
        second way to compute P&L is introduced here, and nothing is
        computed where the account was not read: a run with no snapshot
        sets the REASON instead, so the message can say something true.

        Never overwrites a figure a body already set, and never raises — a
        P&L fault must not cost the push.
        """
        if not isinstance(result, dict):
            return
        keys = (
            "daily_pnl", "daily_return_pct",
            "total_pnl", "total_return_pct", "total_pnl_since",
        )
        if any(k in result for k in keys):
            return
        snapshot = getattr(self, "_last_account_snapshot", None)
        if not snapshot:
            result.setdefault(
                "pnl_unavailable_reason", "ended_before_account_read",
            )
            return
        try:
            total_value, last_equity = snapshot
            if last_equity > 0:
                daily_pnl = total_value - last_equity
                result["daily_pnl"] = daily_pnl
                result["daily_return_pct"] = daily_pnl / last_equity * 100
            total_pnl, total_return_pct, total_pnl_since = (
                self._total_pnl_since_reset(total_value)
            )
            if total_pnl is not None:
                result["total_pnl"] = total_pnl
                result["total_return_pct"] = total_return_pct
                result["total_pnl_since"] = total_pnl_since
        except Exception as exc:  # noqa: BLE001 — never break the push
            logger.warning("P&L attach failed (non-fatal): %s", exc)
        if not any(k in result for k in keys):
            # The account WAS read; what is missing is a usable prior close
            # (and no dated baseline row exists either). Say that, rather
            # than claiming an account read that demonstrably happened did
            # not.
            result.setdefault("pnl_unavailable_reason", "no_prior_close")

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

        # The wrapper kills a slow morning with SIGTERM 30s before SIGKILL,
        # and that is a DOCUMENTED, OBSERVED death mode for this very
        # function. Without this, SIGTERM ends the process where it stands
        # and the `finally` below — which pays the deferred §11.2 gross
        # ceiling — never runs. Converting it to an unwind spends part of
        # that grace window on the session's outstanding safety debts.
        _prior_sigterm = self._install_sigterm_unwind("morning")

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
            drained = self._drain_pending_protection_restores()
            self._drain_pending_repegs()
            # audit F4: resolve BUY write-ahead orphans from a prior
            # crashed session before this run touches positions/cash.
            self._reconcile_orphan_pending_submits()
            # 0c. Broker-truth EXIT audit (2026-08-28 ONDS/CCJ): a protective
            # stop firing overnight is exactly the case morning must catch
            # first — the position has been closed for hours by the time
            # this runs, and every other session entry point runs this same
            # check again in case morning's own attempt failed.
            #
            # Item 173(2): unlike intra/evening, this site is NOT reordered to
            # run `_reconcile_fills` first. Morning's `_reconcile_fills` lives
            # in the method-end `finally:` block, reconciling THIS session's
            # own just-submitted orders after execution — there are no stale
            # 'submitted' SELLs from earlier today for it to resolve here, so
            # the false-gap page the reorder prevents cannot arise at morning,
            # and moving it ahead would strand this session's fills.
            reco = None
            try:
                reco = self._reconcile_stop_out_fills(run_id)
            except Exception as exc:  # noqa: BLE001
                logger.warning("morning stop-out reconcile failed (non-fatal): %s", exc)
            # Item 101: surface a broker-made stop-out / re-protection to the
            # owner — the write-backs above are otherwise silent.
            self._surface_reconcile_outcomes(reco, drained, run_id=run_id)

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
            # The owner's P&L block is built from THIS read, whichever of
            # the body's return paths the run leaves by (see `_attach_pnl`).
            self._record_account_snapshot(total_value, last_equity)
            logger.info(
                "Account: $%.2f total, $%.2f cash (deployable $%.2f), %d positions (last close $%.2f)",
                total_value, cash, ctx.deployable_cash, len(positions), last_equity)

            # 1a. Cash-only safety net — force-sell if margin was entered before
            # this session. Refreshes ctx.cash / positions on completion, so
            # every stage below runs on clean truth.
            forced_orders = self._force_delever(ctx)

            # 1b. Spec §11.2 — the gross-exposure MARGIN FLOOR. Deliberately
            # here, before ANY agent runs: it is computed from account state
            # alone, so a Portfolio Manager that returns nothing (a measured
            # failure mode — one candidate model truncated mid-JSON on 1 run
            # in 10) still cannot leave the desk in a liquidation-proximity
            # breach. Item 112: the morning lane scopes this to `floor_only`
            # — a genuine margin breach is de-levered NOW on the live price;
            # an ordinary §11.2 ceiling breach is de-levered after the PM has
            # run, by `_enforce_gross_ceiling_by_conviction`, so the WEAKEST-
            # by-conviction names are cut first using this session's fresh
            # per-seat read. Always populates ctx.leverage for the alert and
            # the dashboard, including distance-to-forced-liquidation.
            forced_orders = list(forced_orders) + self._enforce_gross_ceiling(
                ctx, floor_only=True,
            )
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

            # Item 112 — the ordinary §11.2 gross-ceiling de-lever, run HERE
            # (morning only) so it cuts the WEAKEST-by-conviction names first
            # using THIS session's fresh per-seat read, not the stale-stance
            # biggest-loser cut the preamble would have used. After the risk
            # stage (so an RM-driven early exit is honoured first) and before
            # execution (so its SELLs land with the session's other orders).
            # A no-op on any book already under its ceiling — the ordinary
            # case — and re-measures gross first, so if the preamble margin
            # floor already fired it only trims a residual breach.
            conviction_delever = self._enforce_gross_ceiling_by_conviction(ctx)

            # Phase 4 #1: execution stage — HOLDs logged, SELLs then BUYs submitted.
            orders = self._execution_stage(ctx)
            if conviction_delever:
                orders = list(conviction_delever) + list(orders)

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
            # Item 112 — pay the deferred ordinary §11.2 ceiling. The preamble
            # scoped itself to the margin floor so the cut could be ordered by
            # THIS session's fresh conviction read; if the run never reached
            # that pass (any PM-less early return, the resume lane, or an
            # exception), the ordinary ceiling is enforced here instead, with
            # the unchanged biggest-loser ordering. One place, so a lane added
            # later cannot silently lose the ceiling. No-op once discharged.
            self._discharge_deferred_gross_ceiling(ctx)
            # Phase 3: ask broker which of today's submitted orders actually filled.
            # Unfilled ones get flagged so PM memory / calibration skip them.
            self._reconcile_fills(ctx)
            # Fills (or stop-outs since snapshot) change the book. Re-read
            # the broker; do not reuse the pre-execution list.
            self._sync_positions_from_broker()
            self._restore_sigterm(_prior_sigterm)

    def run_midday(self) -> dict:
        """13:00 ET — position reviewer, patient disposition."""
        return self.run_position_review(session_type="midday")

    def run_close(self) -> dict:
        """15:30 ET — position reviewer, act-on-trigger disposition.
        17.5 hours until next intraday control; genuine thesis triggers
        fire now rather than waiting for tomorrow morning."""
        return self.run_position_review(session_type="close")

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
        self._last_account_snapshot = None
        result = self._run_position_review_body(session_type)
        self._attach_pnl(result)
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
        drained = self._drain_pending_protection_restores()
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
        #
        # Item 173(2): unlike intra/evening, this site is NOT reordered to run
        # `_reconcile_fills` first. This session's only `_reconcile_fills` is
        # conditional and runs later — after `_force_delever` /
        # `_enforce_gross_ceiling` — SOLELY to flip THIS session's own
        # FORCE_DELEVER rows so the reviewer can see them; it is not the
        # unscoped stale-'submitted' sweep intra/evening run. Moving it ahead
        # of the ceiling logic would reconcile rows that don't exist yet, so
        # the reorder does not apply here.
        reco = None
        try:
            reco = self._reconcile_stop_out_fills(run_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "%s stop-out reconcile failed (non-fatal): %s",
                session_type, exc,
            )
        # Item 101: surface a broker-made stop-out / re-protection to owner.
        self._surface_reconcile_outcomes(reco, drained, run_id=run_id)

        # 1. Sync positions (snapshot into ctx)
        account = self.broker.get_account()
        positions = self.broker.get_positions()
        cash = account["cash"]
        total_value = account["portfolio_value"]
        last_equity = account.get("last_equity", total_value)
        # Carries the P&L block out of the paid-suspension return paths too,
        # which read the account and then reported "not available".
        self._record_account_snapshot(total_value, last_equity)
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
            # Board item 74 — the seat must SEE which triggers it has already
            # spent today, or the executor's refusal is an invisible filter.
            # Same text the enforcement reads, so prompt and gate cannot rot
            # apart. A failed read renders nothing rather than claiming
            # nothing is spent.
            try:
                from src.risk.spent_trigger import (
                    format_spent_triggers_block, keep_executed_acted_triggers,
                    parse_acted_triggers,
                )
                _acted_rows = self.db.get_acted_exit_triggers_today()
                # Same fill verification the executor applies, so the seat is
                # never told a trigger is spent by a cut that sold nothing.
                _executed_ids = {
                    str(r.get("broker_order_id"))
                    for r in (self.db.get_trades(today_only=True, limit=200) or [])
                    if r.get("broker_order_id")
                    and self._trade_executed_or_pending(r)
                }
                _acted = keep_executed_acted_triggers(
                    None if _acted_rows is None else parse_acted_triggers(_acted_rows),
                    executed_order_ids=_executed_ids,
                )
                spent_triggers_block = (
                    "" if _acted is None else format_spent_triggers_block(
                        _acted, {p.symbol for p in review_positions},
                    )
                )
            except Exception as _e:  # noqa: BLE001
                logger.warning(
                    "spent trigger: prompt block unavailable (%s) — the "
                    "executor still enforces it", _e,
                )
                spent_triggers_block = ""

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
                    spent_triggers_block=spent_triggers_block,
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
                **seat_acceptance_kwargs(
                    "position_review_parse_error" if review is None else None,
                    result=md_result,
                ),
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

            # Refresh the broker book before dispatching the LLM's
            # per-position action list: the locals here date from BEFORE the
            # review (minutes of tape ago). Falls back to the pre-review
            # snapshot if the refresh fails.
            try:
                fresh_positions = self.broker.get_positions()
                if fresh_positions:
                    positions = fresh_positions
            except Exception as e:  # noqa: BLE001
                logger.warning("post-review position refresh failed "
                               "(using pre-review snapshot): %s", e)
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
            # the entry target, it cannot reach a later session's
            # exit-guard veto either. Places no orders: nothing here
            # exits anything, and the trailing stop remains the only
            # automatic exit (PR #321).
            #
            # NOT the same as "it can never contribute to a sale" (item
            # 194, corrected 2026-10-01). The LIVE target still feeds
            # `distance_to_target_pct` in the position facts the reviewing
            # model reads, so a revised target can still influence a sale
            # through that model's prose on a LATER session. What is true
            # is narrower and worth stating precisely: no DETERMINISTIC
            # gate reads it — not the exit guard, not progress or pace,
            # and since item 194 not the trailing-stop regime either.
            try:
                # Runs over the WHOLE open book, not only the symbols a
                # seat flagged (item 194); `seat` here is only the label
                # worn by the outcomes that a seat did raise.
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
        """Pre-market earnings analysis, plus the one true sentence its
        P&L block can say.

        This mode runs before the open and makes no broker account read at
        all, so its message genuinely has no figure. It says so explicitly
        rather than letting the renderer guess from absent keys — the guess
        is what produced the false "built without an account read" line on
        trading sessions that HAD read the account (2026-09-23).
        """
        result = self._run_earnings_preprocess_body()
        if isinstance(result, dict):
            result.setdefault("pnl_unavailable_reason", "no_account_read")
        return result

    def _run_earnings_preprocess_body(self) -> dict:
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
        drained = self._drain_pending_protection_restores()
        self._drain_pending_repegs()
        self._reconcile_orphan_pending_submits()  # audit F4
        # Item 101: this pre-market session runs no stop-out reconcile, but a
        # naked position it re-protects is a live-risk event the owner should
        # still hear about — surface the drain count on its own.
        self._surface_reconcile_outcomes(drained_count=drained, run_id=run_id)

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
            # be eligible after an operator resets the circuit. Attach the
            # already-computed `filings_waiting` backlog so the notifier
            # renders "suspended, N filing(s) waiting" instead of the bare
            # counts, which read as "nothing happened" for a real backlog.
            payload = self._paid_suspended_payload(
                run_id, error=exc, filings_waiting=filings_waiting,
            )
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
                    **seat_acceptance_kwargs("agent_failure" if not analysis else None),
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

    def run_intra_safety(self) -> dict:
        """The FREE safety preamble, on its own schedule (board item 177).

        Fill reconcile, stop-out reconcile, protection-restore drain and
        repeg drain cost no model spend and protect live capital. Until
        2026-10-01 they existed ONLY as the opening block of
        ``_run_intra_check_body``, so they were welded to the *paid*
        intraday tick: cutting the paid cadence would silently have cut
        the loss-protection latency with it. That coupling was the defect
        item 177 names.

        The body is unchanged and lives in ``_run_intra_safety_preamble``.
        Both entry points call that one method, so this is strictly
        ADDITIVE: the paid tick still runs the preamble exactly as before,
        and the standalone ``intra_safety`` mode gives it a second,
        independent chance every tick. There is no new window in which
        protection is not restored — the only change is that one more
        caller can reach the same idempotent work.

        Both callers take the same advisory ``_intraday_scan_process_lock``
        and the same ``_blocking_owner_session`` check (board item 127), so
        two of them firing together cannot race: whichever acquires the
        lock does the work and the other defers, which is the behaviour the
        lock was built for.

        No LLM calls, and deliberately NO cost session — this path can
        never spend, and activating one would put empty rows into the very
        ``llm_budget_sessions`` measurement item 177 reads.
        """
        ctx = RunContext.start("intra_safety")
        run_id = ctx.run_id
        logger.info("=== Intra safety preamble (free): %s ===", run_id)

        if not self._is_trading_day():
            logger.info("Intra safety skipped: market closed for non-trading day")
            return {"status": "market_holiday", "run_id": run_id}

        halt = self._kill_switch_halt_result(run_id)
        if halt is not None:
            return halt

        coverage_gaps, preamble_deferred = self._run_intra_safety_preamble(run_id)
        self._intra_preamble_deferred = preamble_deferred
        return {
            "status": "deferred" if preamble_deferred else "ok",
            "run_id": run_id,
            "stop_coverage_gaps": coverage_gaps,
            "preamble_deferred": preamble_deferred,
        }

    def _run_intra_safety_preamble(self, run_id: str) -> tuple[list[dict], str]:
        """Run the free broker-truth safety work; return (gaps, deferred_reason).

        Called by BOTH ``_run_intra_check_body`` (the paid tick) and
        ``run_intra_safety`` (the standalone free tick). Idempotent and
        fail-soft throughout; an empty deferred reason means it ran.
        """
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
                    "%s. No drain, repair, release or reconcile ran; the next "
                    "tick re-reads the broker.", preamble_deferred,
                )
            else:
                # Drain orphaned protection-restore intents — intra runs every
                # 30 min so this is the most frequent recovery opportunity for
                # bails that landed during morning. Codex r8 #2.
                drained = self._drain_pending_protection_restores()
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
                # 2026-09-17 AMD incident: AMD filled at $549.11 but the trades
                # table still read 'submitted' half an hour later. The stop-coverage
                # and stop-out reconcilers below only watch protective/broker-
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
                #
                # Item 173(2): this runs BEFORE the stop-out reconcile below,
                # not after. A SELL this pipeline submitted but hasn't yet
                # reconciled leaves the ledger believing the position is still
                # open (get_symbols_with_open_ledger_qty ignores 'submitted'
                # rows) while the broker has already reduced it — a positive
                # gap. The stop-out reconciler can't explain that gap either,
                # because the submitted SELL's broker_order_id is already in
                # get_known_broker_order_ids, so its fill is filtered out of
                # new_fills — and it pages a false CRITICAL "records disagree
                # with broker". Reconciling fills first flips that SELL to
                # executed, the gap closes, and the stop-out check stays quiet.
                try:
                    self._reconcile_fills()
                except Exception as exc:  # noqa: BLE001
                    logger.warning("intra fill reconcile failed (non-fatal): %s", exc)
                # Broker-truth EXIT audit (2026-08-28 ONDS/CCJ). intra_check fires
                # every ~30 min, so this is the tightest window this reconciler
                # runs on — a stop that fires mid-session is written back within
                # one tick instead of sitting unrecorded until the next scheduled
                # session hours later.
                reco = None
                try:
                    reco = self._reconcile_stop_out_fills(run_id)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("intra stop-out reconcile failed (non-fatal): %s", exc)
                # Item 101: surface a broker-made stop-out / re-protection to
                # owner — intra is the tightest cadence, so this is where a
                # mid-session stop-out reaches him fastest.
                self._surface_reconcile_outcomes(reco, drained, run_id=run_id)

        return coverage_gaps, preamble_deferred

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
        self._last_account_snapshot = None
        self._intra_preamble_deferred = ""
        result = self._run_intra_check_body()
        if isinstance(result, dict) and self._intra_preamble_deferred:
            result["preamble_deferred"] = self._intra_preamble_deferred
        self._attach_pnl(result)
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
        """Lightweight intra-session maintenance tick (no LLM calls).

        Scheduled between morning and midday (typically 12:00 ET). It
        reconciles fills, repairs stop coverage on anything found
        unprotected, reports the session snapshot, and runs the bounded
        intraday opportunity scan.

        **It no longer carries an account-level loss breaker.** That whole
        mechanism — a daily P&L vs loss-limit test that halted the desk —
        was removed 2026-09-20 on the owner's instruction (retired item 32,
        docs/INCIDENT_HISTORY.md). Loss protection is the per-position stop
        living at the broker, which does not depend on this tick running.
        Runs in ~5 seconds.
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
        # Deferring skips only this tick's preamble, and the next tick
        # re-reads the broker. This used to add "the loss check below still
        # runs every tick" as the rest of the safety argument; there is no
        # loss check any more (2026-09-20, retired item 32), so the
        # argument for deferring now rests entirely on the next tick
        # re-reading. Board item 127 is open on that exposure.
        # Board item 177 (2026-10-01): the free safety work below now also
        # has its own entry point (`run_intra_safety`) and its own systemd
        # unit, so it no longer depends on this paid tick running. The paid
        # tick still calls it, unchanged, so nothing here got less reliable.
        coverage_gaps, preamble_deferred = self._run_intra_safety_preamble(run_id)
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
        self._record_account_snapshot(total_value, last_equity)
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
        # opportunity scan.
        try:
            scan_result = self._run_intraday_opportunity_scan(ctx)
        except PaidAnalysisSuspended as exc:
            scan_result = {
                "status": "paid_analysis_suspended",
                "run_id": run_id,
                "error": str(exc),
                "suspended": "intraday opportunity discovery only",
                "preserved": "fill reconciliation and stop-coverage repair",
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
            # path, while the tick itself still completes normally.
            logger.error("Intraday opportunity scan crashed (non-fatal): %s", e)
            scan_result = {
                "status": "intraday_scan_crashed",
                "run_id": run_id,
                "error": str(e),
                "error_type": type(e).__name__,
                "preserved": "fill reconciliation and stop-coverage repair",
            }
        if scan_result is not None:
            result["intraday_scan"] = scan_result
            if scan_result.get("status") == "intraday_executed":
                # Scan went through ExecutionStage; refresh the
                # local table from broker truth (the start-of-tick
                # snapshot above is now stale).
                self._sync_positions_from_broker()
        return result

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
        held at window end, morning was the session waited on — see below —
        or the owner file unreadable). Returns False when the slot is free.

        Morning shares the 09:30 ``SESSION_WINDOWS`` start with this
        ``intra_check`` fire, so waiting for morning then running paid
        discovery on the SAME tick is still the 09:30 open, not a real
        INTRADAY look (item 121 — measured leftover at 09:37). Sets
        ``self._paid_scan_waited_for = "morning"`` in that case so the
        caller skips this tick instead of scanning; the first true paid
        INTRADAY look is the next existing half-hour fire, which sees
        morning's lock already released and runs immediately with no
        invented offset. Midday/close are a different cadence than this
        fire, so waiting for either and then scanning on release is still
        correct.
        """
        import time as _time

        first = True
        self._paid_scan_waited = False
        self._paid_scan_waited_for = None
        last_blocking = None
        while True:
            blocking = self._blocking_owner_session()
            if blocking is None:
                if not first:
                    if last_blocking == "morning":
                        self._paid_scan_waited_for = "morning"
                        logger.info(
                            "Intraday scan: morning released the owner lock; "
                            "this fire shares the 09:30 open with morning, "
                            "so paid discovery stays skipped this tick — "
                            "the next existing half-hour fire is the first "
                            "true INTRADAY look",
                        )
                        return True
                    self._paid_scan_waited = True
                    logger.info(
                        "Intraday scan: other session released the owner lock; "
                        "running paid discovery on this tick instead of "
                        "waiting for the next 30-minute fire",
                    )
                return False
            last_blocking = blocking
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
          - "intraday_scan_open_overlap": morning released the owner lock
            on this same 09:30-shared tick — still the open, not a real
            INTRADAY look (item 121; see `_intraday_open_overlap_skip`).
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

    def _latest_news_read_today(self) -> dict | None:
        """Today's news report, INCLUDING an answer a paid heal bought.

        The scheduled sessions write `data/news/<ET day>/full_report.json`.
        A paid heal does not, and must not: four readers walk that file
        ACROSS days — `NewsStore.recent_state_changes` and the three
        missed-ops/thesis scans in this module — so overwriting it would
        push a heal's baseline-less `state_changes` into a multi-week
        catalyst memory and delete the morning's from it. A file written for
        a cross-day window is the wrong place to put a within-day refresh.

        So the file stays the base, and the freshest PAID read of the day is
        layered over it from `specialist_evidence`, which is already written
        for every news answer (heal and scheduled alike) and is already
        ET-day scoped. Nothing is written here.

        LIFETIME, because this is the whole question: unchanged. Both
        sources are bounded by the same ET trading day, and what expires the
        result is still `evidence_kind.news_reuse` — the next material wire,
        or the session ending. No clock, no N-minute refresh, no new number.
        What changes is only WHICH of today's paid reads the desk finds.

        Per-symbol coverage the newer read was never asked about is kept
        (`seat_heal.merge_carried_stock_news`). Returns the file alone when
        there is no newer row, when it will not parse, or when the store is
        unreachable — a sick forensic table must never cost the desk the
        news it already has on disk.
        """
        report = self.news_store.load_daily_report()
        fetch = getattr(getattr(self, "db", None), "latest_news_analysis_today", None)
        if not callable(fetch):
            return report
        try:
            raw = fetch()
        except Exception as e:  # noqa: BLE001
            logger.warning("Intraday scan: latest news answer read failed: %s", e)
            return report
        if not raw:
            return report
        try:
            import json as _json
            from src.models import NewsIntelligenceReport
            from src.seat_heal import merge_carried_stock_news
            fresher = _json.loads(raw)
            if not isinstance(fresher, dict):
                return report
            # Must still be a real report. A row that cannot parse is not
            # allowed to demote a file that can — that would turn an
            # `expired` seat into a LOST one, which is strictly worse.
            NewsIntelligenceReport(**fresher)
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "Intraday scan: stored news answer would not parse; using "
                "the day's report file: %s", e,
            )
            return report
        if not report:
            return fresher
        return merge_carried_stock_news(fresher, report)

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
            report = self._latest_news_read_today()
            if not report:
                return CarryForward(None, "carry_forward_empty", same_session=False)
            from src.models import NewsIntelligenceReport
            payload = NewsIntelligenceReport(**report)
        except Exception as e:  # noqa: BLE001
            logger.warning("Intraday scan: news carry-forward failed: %s", e)
            return CarryForward(None, "carry_forward_failed", same_session=False)
        # Both sources `_latest_news_read_today` can return are bounded by
        # the SAME ET trading day — the dated report directory and the
        # ET-day evidence-row filter — so a successful load is same-session
        # either way; there is no undated news snapshot on this path.
        # Empty/failed above cannot claim it.
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

    def _persist_heal_call(self, ctx: RunContext, seat: str, agent_name: str,
                            analysis, call_result) -> None:
        """Record a PAID heal exactly the way an ordinary paid call is recorded.

        Two rows, both of them the EXISTING path, neither of them new:

          * `agent_logs` — the model, the tokens, the cost, the raw answer and
            the prompt that produced it. Written under the seat's ORDINARY
            agent name and marked as a heal in `input_summary`, which is the
            convention the desk's two other paid re-asks already follow (the
            exit-trigger re-ask logs `position_reviewer`, the candidate-
            accounting re-ask logs `portfolio_manager`). A separate agent name
            would hide the spend from every per-seat query that exists today,
            which is a different corruption, not less of one. News keeps its
            `_{session}` suffix because that IS its ordinary name.
          * `specialist_evidence(kind="analysis")` — the model's answer as
            structured evidence, the same row `RiskStage` writes for an
            ordinary news or macro read.

        Never raises: a forensic-write failure must not undo a heal that
        succeeded, the same rule `_persist_evidence` and the two re-ask log
        writes above already follow.
        """
        from src.pipeline_stages import _persist_evidence
        session = getattr(ctx, "session", None) or "intra_check"
        # `news_analyst_{session}` is the ordinary name for the news seat
        # (`_run_news_analysis`); macro logs flat. Match each, don't invent.
        log_name = f"{agent_name}_{session}" if seat == "news" else agent_name
        if call_result is None:
            # An analyst that returned an answer but no call record. Nothing
            # to bill and nothing to quote — say so rather than writing a row
            # of zeroes that would read as a free call.
            logger.warning(
                "seat heal: %s returned no call result; cost and raw answer "
                "for this paid retry cannot be recorded", seat,
            )
        else:
            try:
                self.db.insert_agent_log(
                    agent_name=log_name, run_id=ctx.run_id,
                    input_summary=f"seat heal re-ask | {seat} | session={session}",
                    input_message=getattr(call_result, "user_message", "") or "",
                    output_summary=f"seat heal refreshed {seat}",
                    full_response=getattr(call_result, "raw_text", "") or "",
                    model=getattr(call_result, "model", "") or "",
                    tokens_used=getattr(call_result, "tokens_used", 0) or 0,
                    input_tokens=getattr(call_result, "input_tokens", None),
                    output_tokens=getattr(call_result, "output_tokens", None),
                    cost_usd=getattr(call_result, "cost_usd", None),
                    **agent_log_kwargs(call_result),
                )
            except Exception as e:  # noqa: BLE001
                logger.warning("seat heal: paid-call log write failed: %s", e)
        try:
            dump = getattr(analysis, "model_dump_json", None)
            if callable(dump):
                evidence_json = dump()
            else:
                import json as _json
                evidence_json = _json.dumps(analysis, sort_keys=True, default=str)
        except Exception as e:  # noqa: BLE001
            logger.warning("seat heal: could not serialise %s answer: %s", seat, e)
            return
        _persist_evidence(
            self.db, run_id=ctx.run_id, agent_name=agent_name,
            kind="analysis", scope="run", evidence_json=evidence_json,
        )

    def _persist_healed_macro_store(self, ctx: RunContext, payload: dict) -> None:
        """Write a paid macro heal's answer back to the macro store.

        THE MACRO HALF OF THE SAME DEFECT the news heal already fixed.
        `_persist_heal_call` keeps the forensic rows (`agent_logs`,
        `specialist_evidence`); this keeps the WORKING macro state. Without it
        the paid read only ever reached `ctx.macro_analysis` for this one
        tick's PM, then vanished: `_carry_forward_macro` re-reads
        `macro_store.load_last_state()` every tick, and the evening
        thesis-health read and the 7-day regime history read the same store,
        so the stale morning snapshot — not the fresher regime the desk PAID
        for — was what every later reader saw. That is the KEEP WHAT COSTS
        MONEY class of defect, on the macro seat instead of the news seat.

        Persisted the SAME way the scheduled morning read persists
        (`MorningResearchStage`): `save_last_state(payload, series_prints)`,
        with the FRED fingerprint the heal call actually saw so a later tick's
        expiry compares against real prints rather than re-expiring blind. A
        summary with no prints simply stores none — `_macro_series_prints_
        changed` treats an absent fingerprint as "no change", never as churn.

        Never raises — a store-write failure must not undo a paid heal that
        succeeded, the same rule `_persist_heal_call` and
        `_cover_healed_news_wire` already follow.
        """
        store = getattr(self, "macro_store", None)
        save = getattr(store, "save_last_state", None)
        if not callable(save) or not isinstance(payload, dict):
            return
        try:
            from src.data.macro_store import series_prints_from_summary
            prints = series_prints_from_summary(
                getattr(ctx, "macro_summary", None) or {},
                freshness=getattr(getattr(self, "macro", None), "_run_freshness", None),
            )
            save(payload, series_prints=prints)
            logger.info(
                "seat heal: persisted the paid macro read to the macro store "
                "(regime=%s)", payload.get("regime"),
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("seat heal: macro store-write failed: %s", e)

    def _cover_healed_news_wire(self, ctx: RunContext) -> None:
        """Record the wire a paid news heal just read, so it stops expiring.

        THE SECOND HALF OF THE SAME DEFECT. `_persist_heal_call` keeps the
        ANSWER; this keeps the QUESTION. Without it the answer alone changes
        nothing, because `_news_has_newer_material_wire` compares live RSS
        titles against `covered_news_headlines(report)` — the analyst's own
        REWRITTEN headlines — union today's `raw_headlines.json`. Those two
        strings are not the same ID (`NewsStore.load_raw_headlines` says so
        outright), so a healed report is compared against titles it never
        claimed to contain, the same wire reads as newly moved on the next
        tick, and the seat expires again 30 minutes after the desk bought it.

        Only headlines the model was ACTUALLY SHOWN are recorded — measured
        off the prompt text, not the fetch (`seat_heal.wire_titles_shown_to_
        model`). A title the peek fetched but the prompt truncated away is
        left uncovered on purpose: it must still be able to expire the seat.
        That is the difference between recording research and buying silence.

        Appends, never replaces: overwriting would drop the morning's
        per-symbol titles and re-arm the very compare this is quieting.
        Never raises — a coverage write must not undo a paid heal.
        """
        from src.seat_heal import wire_titles_shown_to_model
        try:
            items = list(getattr(self, "_last_news_peek_items", None) or [])
            titles: list[str] = []
            for item in items:
                title = getattr(item, "title", None)
                if title is None and isinstance(item, dict):
                    title = item.get("title") or item.get("headline")
                text = str(title or "").strip()
                if text:
                    titles.append(text)
            shown = wire_titles_shown_to_model(
                titles, getattr(ctx, "heal_news_text", "") or "",
            )
            if not shown:
                return
            append = getattr(
                getattr(self, "news_store", None), "append_raw_headlines", None,
            )
            if not callable(append):
                return
            added = append([{"title": t, "source": "seat_heal", "summary": ""}
                            for t in shown])
            logger.info(
                "seat heal: recorded %d of %d peeked wire titles as read by "
                "the paid news re-ask", added, len(titles),
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("seat heal: wire-coverage write failed: %s", e)

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
                analysis, call_result = analyze(ctx.macro_summary)
            else:
                # Pass this run's session. The analyst's session guidance
                # defaults to MORNING ("treat today as a fresh book... this
                # report sets the tone for the day's trading"), which is
                # false on a 14:00 intra_check — the same mislabelling audit
                # round 2 #24 already fixed for the close session. The other
                # arguments stay at their defaults: a heal re-ask genuinely
                # has no universe or prior-session baseline to offer, and
                # inventing one would be worse than admitting it.
                analysis, call_result = analyze(
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
        # KEEP WHAT COSTS MONEY. Until 2026-09-23 this function spent real
        # dollars on a research call and then kept neither the answer nor the
        # price: `call_result` was discarded as `_raw`, no `agent_logs` row
        # was written, and `HealResult.to_evidence()` omits `payload`. All 8
        # paid heals in production (2026-09-18, news seat) left the desk with
        # a row saying "paid_retry / usable" and nothing else — no model, no
        # tokens, no cost, not one word the model actually said. The owner's
        # own per-session cost line sums `agent_logs.cost_usd` by `run_id`
        # (`src/notifier.py`), so those eight calls read as free.
        self._persist_heal_call(ctx, seat, agent_name, analysis, call_result)
        if seat == "news":
            self._cover_healed_news_wire(ctx)
        elif seat == "macro":
            self._persist_healed_macro_store(ctx, payload)
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

    @staticmethod
    def _intraday_move_in_atr(
        move_pct: float, atr_14: float | None, prev_close: float | None,
    ) -> tuple[float | None, float | None]:
        """The trigger's move expressed in the NAME'S OWN daily range.

        Board item 177, the trigger third. `move_threshold_pct` is a flat
        3% applied to every symbol alike, and the number ledger's open
        question against it asks what move size *relative to the name's own
        ATR* marks a development worth re-reading. That question cannot be
        answered from the desk's record, because the record never held the
        denominator: `intraday_evaluations.detail` stored `move_pct=` and
        nothing else, so 253 recorded selections (2026-09-02 -> 2026-09-25)
        say how far a name moved and never how far that name normally
        moves. Measured on those 253 rows, the flat threshold does not
        discriminate at all — the median move of a selection that produced
        a BUY/SHORT is 3.50% against 3.67% for one that produced nothing,
        and the 5-7% band produced zero orders from 51 selections — so
        re-picking the flat number in either direction has no basis, and
        the ATR-relative form has no data yet. This records the
        denominator, on bars the scan already paid to fetch, changing no
        behaviour: the threshold, the cap and the cooldown all still
        decide exactly what they decided before.

        Returns (atr_pct_of_prev_close, move_in_atr_multiples), either of
        which is None when the inputs cannot support it.
        """
        if not isinstance(atr_14, (int, float)) or atr_14 <= 0:
            return None, None
        if not isinstance(prev_close, (int, float)) or prev_close <= 0:
            return None, None
        atr_pct = float(atr_14) / float(prev_close) * 100.0
        if atr_pct <= 0:
            return None, None
        return atr_pct, float(move_pct) / atr_pct

    def _record_intraday_trigger_atr_context(
        self, ctx: RunContext, symbol: str, mover_symbols: set[str],
        move_by_symbol: dict, snapshots: dict, indicators,
    ) -> None:
        """Stamp the ATR denominator onto a mover's existing ledger row.

        Upsert on (symbol, run_id), so this updates the row
        `record_intraday_evaluation` already wrote at selection time rather
        than adding one: no new row, no change to the cooldown the row
        enforces, no extra market or model call. Best-effort — a
        measurement must never cost the scan that carries it.
        """
        upper = symbol.upper()
        if upper not in mover_symbols:
            return
        move_pct = move_by_symbol.get(symbol, move_by_symbol.get(upper))
        if not isinstance(move_pct, (int, float)):
            return
        snap = snapshots.get(symbol) or snapshots.get(upper) or {}
        atr_pct, move_atr = self._intraday_move_in_atr(
            float(move_pct), getattr(indicators, "atr_14", None),
            snap.get("prev_close"),
        )
        detail = f"move_pct={float(move_pct):.4f}"
        if atr_pct is None or move_atr is None:
            detail += ";atr_pct=unreadable;move_atr=unreadable"
        else:
            detail += f";atr_pct={atr_pct:.4f};move_atr={move_atr:.4f}"
        try:
            self.db.record_intraday_evaluation(
                symbol=upper, run_id=ctx.run_id, status="selected",
                detail=detail,
            )
        except Exception as exc:  # noqa: BLE001 — measurement, never the scan
            logger.warning(
                "Intraday trigger ATR context not recorded for %s (%s) — the "
                "scan is unaffected", upper, exc,
            )

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

    def _intraday_open_overlap_skip(self, ctx: RunContext, movers: list[str]) -> dict:
        """Morning released the lock on this same 09:30-shared tick.

        Not a lock contention (morning is no longer holding it) and not a
        real INTRADAY opportunity — running paid discovery here would be
        the measured 09:37 leftover (item 121): the SAME open, sold to the
        owner a second time under a different label. Skip; the next
        existing half-hour fire, which sees no lock at all, runs normally.
        """
        named = ",".join(movers) if movers else "none"
        reason = (
            "paid discovery skipped: this fire shares the 09:30 open with "
            f"morning; movers={named}"
        )
        logger.info("Intraday scan: %s", reason)
        for symbol in movers:
            try:
                _record_pipeline_event(
                    self, ctx, symbol, "opportunity", "skipped",
                    "intraday_scan_open_overlap", detail=reason,
                )
            except Exception:  # noqa: BLE001
                logger.warning(
                    "Intraday scan: could not persist skip reason for %s",
                    symbol, exc_info=True,
                )
        return {
            "status": "intraday_scan_open_overlap",
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
        for the full rationale). "intraday_scan_open_overlap" when morning
        released the owner lock on this same 09:30-shared tick (item 121 —
        still the open, not INTRADAY); "intraday_scan_lock_contended" when
        `_await_paid_scan_slot` cannot free the owner lock before this
        tick's calendar window ends; "intraday_scan_no_opportunity" for
        every other early return (no
        snapshots, no qualifying moves, no ledgerable symbols, no usable
        bars). A tech seat that is fully LOST (every submitted symbol
        failed, or the batch call raised) is NOT folded into that status —
        item 20 (board) — it is classified `data_status["tech"]="failed"`
        and routed through the same evidence-gate skip + unsuppressible
        alert morning uses. Past that point, a real result dict
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
            if getattr(self, "_paid_scan_waited_for", None) == "morning":
                return self._intraday_open_overlap_skip(ctx, mover_names)
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
        # Item 177: the mover set, so the ATR context below is stamped only
        # on names the flat `move_threshold_pct` trigger actually selected —
        # held-book coverage never went through that trigger and must not be
        # mixed into the measurement that will answer for it.
        symbols_set = {s.upper() for s in symbols}

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
            self._record_intraday_trigger_atr_context(
                ctx, symbol, symbols_set, move_by_symbol, snapshots, indicators,
            )
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
        try:
            analyses_map, ta_result = self.tech_analyst.analyze_batch(
                symbols_data,
                prior_ratings=prior_ratings,
                valuations={},
                intraday_context=intraday_context,
                prior_macro_regime=prior_macro_state.get("regime"),
                prior_macro_outlook=prior_macro_state.get("equity_outlook"),
            )
        except PaidAnalysisSuspended:
            raise
        except Exception as e:  # noqa: BLE001 — mirrors morning's tech
            # try/except (pipeline_stages.py): a bare call here had no
            # guard at all, so a batch-level raise (provider outage,
            # unparseable response) crashed the whole intraday tick
            # instead of being recorded as a LOST tech seat like every
            # other failure mode this scan already handles.
            logger.error(
                "Intraday scan: tech_analyst.analyze_batch raised: %s. "
                "Tech seat LOST this tick.", e,
            )
            analyses_map, ta_result = {}, None
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
                    **seat_acceptance_kwargs("failed" if not analyses else None),
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

        # Item 20 (board): deliberately no early "no analyses" return here.
        # `symbols_data` was already confirmed non-empty above, so zero
        # usable analyses at this point is a LOST tech seat, not a quiet
        # tick — it must fall through to the shared `data_status`/gate path
        # below (classification just before `ctx.data_status`), not return
        # "intraday_scan_no_opportunity" indistinguishably from a real
        # empty candidate set.

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
        # Item 20 (board): three-way, matching morning's classification.
        # `symbols_data` was non-empty going in, so `analyses` empty here
        # means every submitted symbol failed (or the batch call raised,
        # caught above) — a LOST seat, not an ordinary quiet tick. A
        # partial batch (some resolved) stays REPORTED, exactly as before —
        # this must not start blocking intraday trading on one bad symbol.
        if analyses:
            tech_status = "partial" if failed_count else "ok"
        else:
            tech_status = "failed"
            logger.error(
                "Intraday scan: tech seat LOST — %d/%d submitted symbol(s) "
                "resolved to a usable analysis this tick",
                len(analyses), len(analyses_map),
            )
        ctx.data_status = {
            "tech": tech_status,
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
        drained = self._drain_pending_protection_restores()
        self._drain_pending_repegs()
        self._reconcile_orphan_pending_submits()  # audit F4
        # Broker-truth coverage audit — last check before carrying positions
        # overnight (independent of the WAL).
        coverage_gaps = self._reconcile_stop_coverage()
        # Sweep submitted orders so canceled/expired orders do not get
        # narrated as real trades, and partial terminal fills are reflected
        # in the trade list before the evening prompt is built.
        #
        # Item 173(2): this runs BEFORE the stop-out reconcile below, not
        # after. A SELL this session submitted but hasn't yet reconciled
        # leaves the ledger believing the position is still open
        # (get_symbols_with_open_ledger_qty ignores 'submitted' rows) while
        # the broker has already reduced it — a positive gap the stop-out
        # reconciler can't explain, because the submitted SELL's
        # broker_order_id is already in get_known_broker_order_ids so its
        # fill is filtered out of new_fills, and it pages a false CRITICAL
        # "records disagree with broker". Reconciling fills first flips that
        # SELL to executed, the gap closes, and the stop-out check stays quiet.
        self._reconcile_fills()
        # Broker-truth EXIT audit (2026-08-28 ONDS/CCJ) — last chance before
        # the daily P&L snapshot below is computed, so a same-day stop-out
        # is reflected in tonight's report rather than showing up as an
        # unexplained gap the next time someone looks at realized_pnl.
        reco = None
        try:
            reco = self._reconcile_stop_out_fills(run_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("evening stop-out reconcile failed (non-fatal): %s", exc)
        # Item 101: surface a broker-made stop-out / re-protection to owner.
        self._surface_reconcile_outcomes(reco, drained, run_id=run_id)

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
            **seat_acceptance_kwargs(_ev_log_kwargs.get("status") if _ev_log_kwargs.get("status") in ("failed", "evening_parse_error") else None),
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
            # Observability: surface a silently-missing session so the
            # notifier can raise deterministic escalation (not just LLM).
            "missing_sessions": missing_sessions,
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
                    # The second is the state the stop-limit buffer trade-off
                    # produces on a gap — now only reachable on the stop-limit
                    # FALLBACK leg, since primary protective stops are
                    # stop-market and fill when elected — and it now reads as
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
            # A legit PM-less completion (no_data, say) records a status
            # marker — skip both probes for it.
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
