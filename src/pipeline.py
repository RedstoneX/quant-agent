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
from src.pipeline_admission_shell import AdmissionMixin
from src.pipeline_delever import (  # noqa: F401
    DeleverMixin,
    _optional_risk_number,
    _risk_number,
)
from src.pipeline_risk_gate import RiskGate
from src.pipeline_risk_gate_mixin import RiskGateMixin  # noqa: F401
# Step 7 of docs/PIPELINE_SPLIT_PLAN.md: the research-continuity cluster
# (change detectors, carry-forward, Form-4 backlog, seat healing) moved to a
# mixin module. `CarryForward` travelled with it because only those bodies
# construct it and that module may not import this one; it is re-exported here
# so `from src.pipeline import CarryForward` keeps working.
from src.pipeline_research_continuity import (  # noqa: F401
    CarryForward,
    ResearchContinuityMixin,
)
from src.pipeline_intraday import IntradayMixin
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


class TradingPipeline(
    ProtectionMixin, PromptFactsMixin, DeleverMixin, ExitEngineMixin,
    RiskGateMixin, AdmissionMixin, ResearchContinuityMixin, IntradayMixin,
):
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
        self.risk_gate = RiskGate(risk_engine=self.risk_engine, db=self.db, sweeper=self._sweeper, config=self.config)
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
        pre-clamp behavior) — never raises. The body lives in
        `src.data.technical.atr_for_symbol`; this is the delegation.
        """
        from src.data.technical import atr_for_symbol
        return atr_for_symbol(getattr(self, "market", None), symbol)

    def _constructor_cfg_or_none(self):
        """The LIVE `ConstructorConfig`, for rules that must agree with the
        stops the desk actually places (board item 185: the universe
        screen's volatility ceiling is 1 / the widest stop this object can
        produce). `None` when no constructor has been built -- some tests
        drive a bare pipeline -- and the caller then falls back to
        `config.risk` plus the class defaults. The body lives in
        `src.risk.constants.live_constructor_cfg_or_none`.
        """
        from src.risk.constants import live_constructor_cfg_or_none
        return live_constructor_cfg_or_none(
            getattr(self, "portfolio_constructor", None),
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
        """Parse stored fenced/prose-wrapped JSON exactly as live agents do.

        The body lives in `src.agents.logged_response`; this delegation
        keeps every `self._parse_logged_agent_response(row)` caller working.
        """
        from src.agents.logged_response import parse_logged_agent_response
        return parse_logged_agent_response(row)

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
