import os
import re
from pathlib import Path

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

from src.agents.base import VALID_PROVIDERS, resolve_provider


class ApiKeysConfig(BaseModel):
    anthropic: str
    openai: str = ""
    deepseek: str = ""
    # OpenRouter (Stage 1 QAMC provider/model plumbing) — optional, only
    # required when an agent's explicit `provider: openrouter` is selected
    # (enforced in AppConfig._check_llm_provider_keys, not here, since that's
    # the layer that already knows which agents are configured for it).
    openrouter: str = ""
    fred: str
    alpaca_key: str
    alpaca_secret: str

    @model_validator(mode="after")
    def _check_required_keys(self):
        for field_name in ("alpaca_key", "alpaca_secret", "fred"):
            if not getattr(self, field_name):
                raise ValueError(f"Required API key '{field_name}' is empty — check your .env file")
        if not self.anthropic and not self.openai and not self.deepseek and not self.openrouter:
            raise ValueError(
                "At least one of 'anthropic', 'openai', 'deepseek', or 'openrouter' API key must be set"
            )
        return self


# Alpaca's paper-trading host. `base_url` is declarative today — no code path
# reads it (the real switch is the `paper` flag below, which alpaca-py turns
# into an endpoint choice) — so the validator's job is to stop the two from
# disagreeing and giving a reader a false impression of which venue is in use.
_ALPACA_PAPER_HOST = "paper-api.alpaca.markets"


class AlpacaConfig(BaseModel):
    base_url: str
    paper: bool

    @model_validator(mode="after")
    def _enforce_paper_only(self):
        """Fail closed unless this is a paper account.

        "Alpaca **Paper only**; live trading is not authorized" is a hard
        boundary in CLAUDE.md, docs/STATE.md and docs/WORK.md, but until now
        it lived entirely in prose: flipping `paper: false` in settings.yaml
        would have silently pointed the whole decision chain at a live
        brokerage account with no test, guard, or log to notice. A one-token
        config edit should not be able to do that.

        This is deliberately a hard failure with no env-var escape hatch. If
        live trading is ever authorized, removing this guard should be a
        reviewed code change in its own commit — the same deliberate,
        auditable act that authorizing it is.
        """
        if self.paper is not True:
            raise ValueError(
                "alpaca.paper must be true — live trading is not authorized "
                "(see the hard boundaries in CLAUDE.md / docs/STATE.md). "
                "Enabling live trading requires removing this guard in a "
                "reviewed change, not a settings.yaml edit."
            )
        host = self.base_url.strip().lower()
        if host and _ALPACA_PAPER_HOST not in host:
            raise ValueError(
                f"alpaca.base_url must point at {_ALPACA_PAPER_HOST} while "
                f"paper-only is in force; got {self.base_url!r}"
            )
        return self


# The nine agents that carry a per-agent model (and, as of Stage 1, an
# optional explicit provider). Single list reused by LLMConfig.get_provider
# and AppConfig._check_llm_provider_keys so the two can't drift apart.
AGENT_NAMES = (
    "tech_analyst", "news_analyst", "macro_analyst", "earnings_analyst",
    "smart_money_analyst",
    "portfolio_manager", "risk_manager", "position_reviewer",
    "evening_analyst", "meta_reflector",
)


class LLMConfig(BaseModel):
    tech_analyst_model: str = "claude-opus-4-7"
    news_analyst_model: str = "claude-opus-4-7"
    macro_analyst_model: str = "claude-opus-4-7"
    earnings_analyst_model: str = "claude-opus-4-7"
    smart_money_analyst_model: str = "claude-opus-4-7"
    portfolio_manager_model: str = "claude-opus-4-7"
    risk_manager_model: str = "claude-opus-4-7"
    position_reviewer_model: str = "claude-opus-4-7"
    evening_analyst_model: str = "claude-opus-4-7"
    # Quarterly meta-reflector — strategic self-audit agent. Opus by default
    # because the input (deterministic digest) is dense and the output must
    # cite numbers precisely; a weaker model tends to vibe-reason.
    meta_reflector_model: str = "claude-opus-4-7"
    # Stage 1: explicit per-agent provider override. `None` (every agent's
    # default) means "infer from the model-id prefix", exactly as before
    # Stage 1 — this field is additive-only and changes nothing for a
    # settings.yaml that doesn't set it. Required (not inferrable) for
    # OpenRouter, whose "vendor/model" ids collide with native prefixes —
    # see resolve_provider() in src/agents/base.py.
    tech_analyst_provider: str | None = None
    news_analyst_provider: str | None = None
    macro_analyst_provider: str | None = None
    earnings_analyst_provider: str | None = None
    smart_money_analyst_provider: str | None = None
    portfolio_manager_provider: str | None = None
    risk_manager_provider: str | None = None
    position_reviewer_provider: str | None = None
    evening_analyst_provider: str | None = None
    meta_reflector_provider: str | None = None
    # OpenRouter endpoint preference, per seat. OpenRouter serves one model id
    # from several endpoints ("providers") at DIFFERENT PRICES — `openai/gpt-5.5`
    # is offered by `openai/flex` at $2.50/$15 and by `openai` / `azure` at
    # $5/$30, all three serving the identical `gpt-5.5-20260423` weights. This
    # field pins the preferred endpoint ORDER; it never changes which MODEL
    # answers, so it is not a routing-policy decision and needs no benchmark.
    # `None` (every seat's default) leaves endpoint choice to OpenRouter,
    # exactly as before. Only meaningful when the seat's provider is
    # `openrouter`; the validator enforces that.
    tech_analyst_provider_order: list[str] | None = None
    news_analyst_provider_order: list[str] | None = None
    macro_analyst_provider_order: list[str] | None = None
    earnings_analyst_provider_order: list[str] | None = None
    smart_money_analyst_provider_order: list[str] | None = None
    portfolio_manager_provider_order: list[str] | None = None
    risk_manager_provider_order: list[str] | None = None
    position_reviewer_provider_order: list[str] | None = None
    evening_analyst_provider_order: list[str] | None = None
    meta_reflector_provider_order: list[str] | None = None
    # Global fallback — used by any agent without an explicit override below.
    max_tokens: int
    # Per-agent overrides. Each agent emits a different output shape; the PM
    # writes 7-step reasoning + 20-35 target positions, while Macro emits a
    # single compact regime call. One-size-fits-all can silently truncate the
    # heavy ones when the global is tuned to the average. `None` inherits
    # `max_tokens`; set explicitly in settings.yaml to tune per agent.
    tech_analyst_max_tokens: int | None = None
    news_analyst_max_tokens: int | None = None
    macro_analyst_max_tokens: int | None = None
    earnings_analyst_max_tokens: int | None = None
    smart_money_analyst_max_tokens: int | None = None
    portfolio_manager_max_tokens: int | None = None
    risk_manager_max_tokens: int | None = None
    position_reviewer_max_tokens: int | None = None
    evening_analyst_max_tokens: int | None = None
    meta_reflector_max_tokens: int | None = None

    @model_validator(mode="before")
    @classmethod
    def _inherit_new_specialist_routing(cls, values):
        """Old configs inherit the technical specialist's provider/model."""
        if isinstance(values, dict) and "smart_money_analyst_model" not in values:
            values["smart_money_analyst_model"] = values.get("tech_analyst_model", "claude-opus-4-7")
            values["smart_money_analyst_provider"] = values.get("tech_analyst_provider")
        return values

    @field_validator("max_tokens")
    @classmethod
    def _max_tokens_sane(cls, v: int) -> int:
        # A non-positive or trivially small max_tokens will fail at LLM-call
        # time with an opaque provider error. Fail fast at config load instead.
        if v < 512:
            raise ValueError(
                f"llm.max_tokens must be >= 512 for agent outputs; got {v}"
            )
        return v

    @field_validator(
        "tech_analyst_max_tokens",
        "news_analyst_max_tokens",
        "macro_analyst_max_tokens",
        "earnings_analyst_max_tokens",
        "smart_money_analyst_max_tokens",
        "portfolio_manager_max_tokens",
        "risk_manager_max_tokens",
        "position_reviewer_max_tokens",
        "evening_analyst_max_tokens",
        "meta_reflector_max_tokens",
    )
    @classmethod
    def _per_agent_max_tokens_sane(cls, v: int | None) -> int | None:
        # Same floor as the global — prevents a misconfigured override from
        # silently starving an agent. None means "inherit global".
        if v is None:
            return None
        if v < 512:
            raise ValueError(
                f"per-agent max_tokens override must be >= 512 (or null to "
                f"inherit global); got {v}"
            )
        return v

    def get_max_tokens(self, agent_name: str) -> int:
        """Return the max_tokens for `agent_name`, falling back to the global.

        `agent_name` is the logical agent name (e.g. "tech_analyst"). Returns
        the per-agent override when set, else `self.max_tokens`. Unknown
        agent names also fall back to the global.
        """
        override = getattr(self, f"{agent_name}_max_tokens", None)
        if override is not None:
            return override
        return self.max_tokens

    def get_provider(self, agent_name: str) -> str | None:
        """Return the explicit provider override for `agent_name`, or None
        (meaning "infer from the model-id prefix", the pre-Stage-1 behavior).
        Unknown agent names also return None."""
        return getattr(self, f"{agent_name}_provider", None)

    def get_provider_order(self, agent_name: str) -> list[str] | None:
        """Return the OpenRouter endpoint preference for `agent_name`, or None
        (meaning "let OpenRouter choose", the pre-existing behavior). Unknown
        agent names also return None. This selects an ENDPOINT for the seat's
        configured model, never a different model."""
        return getattr(self, f"{agent_name}_provider_order", None)

    @field_validator(
        "tech_analyst_provider", "news_analyst_provider", "macro_analyst_provider",
        "earnings_analyst_provider", "portfolio_manager_provider", "risk_manager_provider",
        "smart_money_analyst_provider",
        "position_reviewer_provider", "evening_analyst_provider", "meta_reflector_provider",
    )
    @classmethod
    def _provider_is_valid_or_unset(cls, v: str | None) -> str | None:
        # None = "not set, infer from model prefix" (the default/backward-
        # compatible case). A typo'd provider string must fail loudly at
        # config load rather than silently falling through to prefix
        # inference and picking an unintended provider.
        if v is None:
            return None
        normalized = v.strip().lower()
        if normalized not in VALID_PROVIDERS:
            raise ValueError(
                f"Invalid provider {v!r}; must be one of {sorted(VALID_PROVIDERS)} or unset"
            )
        return normalized

    @field_validator(
        "tech_analyst_provider_order", "news_analyst_provider_order",
        "macro_analyst_provider_order", "earnings_analyst_provider_order",
        "smart_money_analyst_provider_order", "portfolio_manager_provider_order",
        "risk_manager_provider_order", "position_reviewer_provider_order",
        "evening_analyst_provider_order", "meta_reflector_provider_order",
    )
    @classmethod
    def _provider_order_is_wellformed(cls, v: list[str] | None) -> list[str] | None:
        # An empty list is not "no preference" — it is a preference that
        # nothing may serve the seat. That reads as a typo far more often
        # than as intent, so reject it and make the operator write null.
        if v is None:
            return None
        if not v:
            raise ValueError(
                "provider_order must be null (no preference) or a non-empty "
                "list of OpenRouter endpoint slugs; got []"
            )
        cleaned: list[str] = []
        for entry in v:
            if not isinstance(entry, str) or not entry.strip():
                raise ValueError(
                    f"provider_order entries must be non-empty strings; got {entry!r}"
                )
            cleaned.append(entry.strip())
        return cleaned

    @model_validator(mode="after")
    def _provider_order_requires_openrouter(self):
        """An endpoint preference only means anything to OpenRouter. Set on an
        Anthropic/OpenAI/DeepSeek seat it would be silently ignored, which is
        how an operator ends up believing a seat is on a cheaper tier that it
        never reached. Fail at config load instead."""
        for agent_name in AGENT_NAMES:
            order = getattr(self, f"{agent_name}_provider_order", None)
            if order is None:
                continue
            provider = resolve_provider(
                getattr(self, f"{agent_name}_model"),
                getattr(self, f"{agent_name}_provider", None),
            )
            if provider != "openrouter":
                raise ValueError(
                    f"{agent_name}_provider_order is set but {agent_name} routes "
                    f"to {provider!r}, not 'openrouter'. Endpoint preferences are "
                    f"an OpenRouter concept and would be ignored — remove the "
                    f"preference or route the seat through OpenRouter."
                )
        return self


class ExecutionConfig(BaseModel):
    """How aggressively an entry may cross the spread.

    Separate from `RiskConfig` on purpose: this bounds EXECUTION cost, not
    position risk. A too-tight cap does not make the book safer — it silently
    stops it trading, which is what happened to VLO on 2026-08-27.
    """

    max_entry_slippage_bps: float = Field(default=40.0, gt=0, le=500)
    """Max basis points above the verified reference price an entry limit may
    sit. When the displayed offer is already beyond this, the BUY is skipped
    with reason `slippage_gated` rather than submitted as an unfillable
    order."""


class RiskConfig(BaseModel):
    max_position_pct: float = Field(gt=0, le=100)
    max_total_position_pct: float = Field(gt=0)
    max_daily_loss_pct: float = Field(gt=0, le=100)
    max_sector_pct: float = Field(gt=0, le=100)
    require_stop_loss: bool
    # Owner-ratified total at-risk ceiling (2026-08-27): the sum of every
    # position's loss-if-stopped, measured against cost basis, may not exceed
    # this share of equity. Distinct from `max_total_position_pct`, which caps
    # NOTIONAL: a $50k book with 10% stops is 50% invested and 5% at risk.
    # Capital is meant to be fully deployed; it is RISK that is rationed.
    #
    # Reporting-only today — `PMFacts` renders the figure and its headroom so
    # the Portfolio Manager sizes against a real number instead of a rule it
    # was told about but never shown. Phase 2b makes it a hard gate.
    max_portfolio_risk_pct: float = Field(default=25.0, gt=0, le=100)
    # Spec §2.1. The owner-ratified per-trade envelope (2026-08-27). Conviction
    # is expressed as the share of equity an idea may lose if its stop is hit,
    # and the constructor derives share count from it:
    #     shares = (equity x risk_pct / 100) / |entry - stop|
    # A wider stop therefore yields a SMALLER position rather than a rejected
    # trade, which is what removes the incentive to squeeze stops. The prior
    # 0.5% ceiling lived in a constructor dataclass default nobody chose.
    max_position_risk_pct: float = Field(default=5.0, gt=0, le=100)
    # Below this an idea is not worth trading: a token position pays full
    # commission and full attention for an immaterial payoff. A request
    # rationed under the floor is denied outright rather than shrunk.
    min_position_risk_pct: float = Field(default=0.5, ge=0, le=100)
    # Spec §2.2. The most of the total at-risk ceiling any ONE correlated
    # cluster may take. Without it "total risk is under 25%" says nothing
    # about diversification — a book holding one theme four times over
    # satisfies it while carrying exactly the concentration the ceiling
    # exists to prevent. Correlated names consume one bet's budget.
    max_cluster_risk_share_pct: float = Field(default=40.0, gt=0, le=100)
    # Minimum stop distance in ATRs. Structure places the stop; this only
    # pushes it out when structure put it inside ordinary volatility. Measured
    # 2026-08-27: stops sat a median 4.3% below entry against a median ATR of
    # 2.56% of price — about 1.7 ATRs, barely more than one ordinary day's
    # range, which is what was firing exits inside noise AND forcing enormous
    # positions to reach any meaningful risk.
    min_stop_atr_multiple: float = Field(default=3.0, gt=0, le=10)
    # Widening a stop lowers reward:risk, because the target does not move.
    # Under this the setup only ever qualified on a stop too tight to survive.
    min_reward_risk_after_widening: float = Field(default=1.5, ge=0, le=10)
    # Cash-only default. When False: no BUY may drive `cash` below zero, and
    # any session that starts with `cash < 0` must de-lever (SELL) before any
    # new BUY. When True: normal margin account behavior, risk engine only
    # enforces the exposure / sector / loss caps. Default False is the
    # conservative choice — margin leverage amplifies drawdowns and is not
    # the bot's intended mode unless explicitly opted in.
    allow_margin: bool = False


class CashSweepConfig(BaseModel):
    """Idle-cash sweep into a T-bill ETF (default SGOV).

    The sweep vehicle is treated as CASH-EQUIVALENT everywhere: excluded
    from every LLM-facing position view, excluded from risk-engine exposure
    math (its market value counts toward cash in the cash_only filter),
    exempt from stop-coverage audits (it deliberately carries no stop), and
    force_delever liquidates it FIRST. Deterministic and zero-LLM — the
    LLM never decides to park or unpark; the pipeline bookends do.
    """
    enabled: bool = False
    """Master switch. False = the sweeper is inert everywhere (no view
    filtering, no funding sells, no parking buys)."""

    symbol: str = "SGOV"
    """The parking vehicle. Must be a cash-like T-bill ETF (SGOV/BIL);
    anything with real market beta breaks the cash-equivalence assumption
    that justifies every exemption listed above."""

    reserve_pct: float = Field(default=1.0, ge=0, le=20)
    """% of equity kept as raw cash (fees, slippage, partial fills).
    Excess above the reserve is parked.

    Deliberately left at 1.0. An earlier pass in the 2026-08-19 tranche
    raised this to 5.0 as a workaround for BUYs being skipped for lack of
    cash — that was treating a symptom. Alpaca credits `cash` as soon as a
    SELL fills, so a filled SGOV liquidation funds an equity BUY in the
    same session; the real fix is confirming that fill before the BUY
    phase (see `CashSweeper.fund_buys`), not starving the sweep of the
    idle cash it exists to put to work."""

    min_order_usd: float = Field(default=500.0, ge=0)
    """Don't churn sub-$500 parking orders — spread + noise beat the
    few cents of yield."""

    @field_validator("symbol")
    @classmethod
    def _symbol_nonempty(cls, v: str) -> str:
        v = (v or "").strip().upper()
        if not v:
            raise ValueError("cash_sweep.symbol must be a non-empty ticker")
        return v


class IntradayScanConfig(BaseModel):
    """2026-08-19 intraday opportunity-discovery fix.

    The full opportunity-generation chain (macro/news/tech/earnings ->
    PM -> RM -> deterministic gate -> execution) runs once each morning.
    Tech's data is completed-daily-bar-as-of-prior-close; `intra_check`
    (every 30 min) is loss-protection only; midday/close review existing
    holdings only. A material move developing after the morning run could
    not generate a new trade. This adds a bounded, cheap trigger onto the
    EXISTING intra_check cadence — no new systemd timer, no full research
    stack rerun: one bulk current-session snapshot call flags symbols that
    moved materially since the last close; only THOSE few symbols (capped)
    get real daily bars/indicators and a real tech_analyst call, then the
    SAME DecisionStage -> RiskStage -> ExecutionStage chain morning uses.
    """
    enabled: bool = False
    """Master switch. False = intra_check's existing loss-protection-only
    behavior is completely unchanged. Off by default: this is new
    autonomous-decision surface added mid-tranche, not yet operator-
    reviewed in production — flip on deliberately after reviewing the PR,
    the same rollout pattern cash_sweep followed."""

    move_threshold_pct: float = Field(default=3.0, ge=0.5, le=50)
    """Minimum |% move| since the last daily close (via a single bulk
    Alpaca snapshot call) for a symbol to qualify as a candidate."""

    cooldown_hours: float = Field(default=3.0, ge=0.5, le=24)
    """Minimum hours between two intraday-scan decisions for the SAME
    symbol — prevents repeated scans from churning the same setup every
    30-minute tick while a move is still developing."""

    max_candidates_per_scan: int = Field(default=5, ge=1, le=20)
    """Hard cap on how many symbols get a real tech_analyst call in one
    tick — keeps this a bounded, occasional check, not a high-frequency
    system, even on a broad-market move day when many symbols qualify."""


class SmartMoneyConfig(BaseModel):
    enabled: bool = False
    search_url: str = "https://efts.sec.gov/LATEST/search-index"
    archives_url: str = "https://www.sec.gov/Archives/edgar/data"
    data_dir: str = "data/smart_money"
    user_agent: str = "QAMC research-intelligence qamc-contact@proton.me"
    request_timeout_s: float = Field(default=15.0, ge=1, le=60)
    refresh_deadline_s: float = Field(default=180.0, ge=10, le=600)
    requests_per_second: float = Field(default=8.0, ge=0.5, le=10.0)
    lookback_days: int = Field(default=7, ge=1, le=30)
    max_filings_per_refresh: int = Field(default=1000, ge=1, le=5000)
    max_observations: int = Field(default=40, ge=1, le=200)
    min_transaction_value_usd: float = Field(default=100_000, ge=1_000)
    external_min_transaction_value_usd: float = Field(default=250_000, ge=1_000)
    cluster_window_days: int = Field(default=14, ge=1, le=45)
    min_cluster_owners: int = Field(default=2, ge=2, le=10)
    max_external_candidates: int = Field(default=3, ge=1, le=10)
    min_external_price_usd: float = Field(default=5.0, ge=1.0)
    min_external_avg_dollar_volume_usd: float = Field(default=10_000_000, ge=1_000_000)
    min_external_history_days: int = Field(default=20, ge=10, le=120)

    # --- Routine-versus-opportunistic Form 4 classification ---------------
    # `src/data/insider_signal.py::classify_transaction`. Evidence basis is
    # Cohen, Malloy & Pomorski, *Decoding Inside Information* (JF 2012), via
    # `docs/RESEARCH_FINDINGS.md` section 1. These were module-level
    # constants during initial development; moved here 2026-08-28 per the
    # standing rule that a threshold able to change classification output is
    # an operator-tunable setting, not a fixed number buried in code.
    #
    # A routine insider trades the same issuer in the same calendar month in
    # each of this many consecutive preceding years. This is Cohen/Malloy/
    # Pomorski's own definition, so 3 is the literature's number, not a
    # guess — but it is still exposed here rather than hardcoded, since a
    # future re-derivation against QAMC's own filing history may want a
    # different value.
    insider_calendar_routine_years: int = Field(default=3, ge=1, le=10)
    # Fallback cadence test for insiders who lack the full calendar-year
    # history above (the common case on a fresh cache — see the 2026-08-28
    # measurement note in `docs/WORK.md`, where zero of 2,188 filings matched
    # the calendar rule because the history index was brand new). Needs at
    # least this many prior same-direction trades before the gap statistics
    # are trusted.
    insider_min_cadence_trades: int = Field(default=3, ge=2, le=20)
    # Mean gap between trades, in days, that reads as a scheduled programme
    # rather than a one-off. 20-120 days admits a monthly-to-quarterly
    # cadence; narrower or wider than that is either noise (too frequent to
    # be a real event) or too sparse to call a pattern.
    insider_cadence_min_mean_gap_days: float = Field(default=20.0, gt=0)
    insider_cadence_max_mean_gap_days: float = Field(default=120.0, gt=0)
    # Coefficient of variation (population stdev / mean) of the trade gaps.
    # 0.25 admits a monthly or quarterly programme that drifts by a few days;
    # it rejects lumpy, irregularly-spaced discretionary trading.
    insider_cadence_max_gap_dispersion: float = Field(default=0.25, gt=0, le=2.0)
    # A disposition smaller than this share of the insider's pre-transaction
    # holding is diversification/liquidity noise rather than a directional
    # view — RESEARCH_FINDINGS.md: "only sales that are also large relative
    # to the insider's total position predict negative returns." Deliberately
    # NOT combined with the 10b5-1 flag on its own: a large planned sale is
    # never demoted to routine by this filter, only a proportionally small
    # one may additionally cite the plan (see `insider_signal.py` module
    # docstring, departure #1).
    insider_min_material_sell_fraction: float = Field(default=0.05, ge=0.0, le=1.0)
    # How long `data/smart_money/insider_history.json` retains a trade date
    # before it is pruned. Must comfortably exceed the calendar-routine
    # lookback (`insider_calendar_routine_years` years) with slack for late
    # and amended filings — `observations.json` itself is pruned to
    # `lookback_days`, far too short for the calendar test, which is the
    # entire reason a separate long-horizon index exists. Default is 5
    # years (5 * 366 days, leap-safe).
    insider_history_retention_days: int = Field(default=5 * 366, ge=366, le=20 * 366)

    @model_validator(mode="after")
    def _insider_cadence_window_is_well_formed(self):
        if self.insider_cadence_min_mean_gap_days >= self.insider_cadence_max_mean_gap_days:
            raise ValueError(
                "smart_money.insider_cadence_min_mean_gap_days must be less "
                "than insider_cadence_max_mean_gap_days; got "
                f"{self.insider_cadence_min_mean_gap_days} >= "
                f"{self.insider_cadence_max_mean_gap_days}"
            )
        required_days = self.insider_calendar_routine_years * 366
        if self.insider_history_retention_days < required_days:
            raise ValueError(
                "smart_money.insider_history_retention_days "
                f"({self.insider_history_retention_days}) is shorter than "
                f"insider_calendar_routine_years ({self.insider_calendar_routine_years}) "
                f"requires (>= {required_days} days) — the calendar-routine "
                "test would silently lose its own history before it could "
                "ever match."
            )
        return self


class ScheduleConfig(BaseModel):
    earnings_preprocess: str = "08:00"
    morning: str
    intra_check: str = "10:30"
    midday: str
    close: str = "15:30"
    evening: str


class TradingConfig(BaseModel):
    # Universe must be non-empty — empty list silently produces zero
    # data, zero analyses, zero trades for the whole session. Catch
    # at config load instead of letting it surface as a degraded
    # day with no obvious cause.
    universe: list[str] = Field(min_length=1)
    # Lookback for OHLCV bars feeding the technical indicators. Negative
    # or zero values used to load silently and fail downstream with
    # opaque pandas slicing errors. Floor at 1 (one day of bars is
    # the absolute minimum for any indicator).
    lookback_days: int = Field(ge=1)
    schedule: ScheduleConfig


class StorageConfig(BaseModel):
    db_path: str


class LLMCostCircuitConfig(BaseModel):
    """Fail-closed limits for every paid model request.

    These are deliberately configuration values (visible and testable), but
    disabling the breaker is not supported by production settings.  The
    optional ``enabled`` field exists for isolated unit fixtures and defaults
    on so older settings files acquire protection automatically.
    """

    enabled: bool = True
    require_telegram_alerts: bool = True
    session_cost_limit_usd: float = Field(default=0.90, gt=0, allow_inf_nan=False)
    daily_cost_limit_usd: float = Field(default=1.50, gt=0, allow_inf_nan=False)
    session_reserved_exposure_limit_usd: float = Field(
        default=1.80, gt=0, allow_inf_nan=False,
    )
    daily_reserved_exposure_limit_usd: float = Field(
        default=1.90, gt=0, allow_inf_nan=False,
    )
    # RUNAWAY BACKSTOP, not the working budget (Defect 4, 2026-08-28). Until
    # this fix it was 2 and it WAS the binding constraint on every trading
    # day: intra_check fires 14 times between 09:30 and 16:00 ET, two of
    # those could think and the other twelve suspended. Measured 2026-08-25/
    # 26/27: 4, 7 and 6 suspensions per day while only $1.02 of a $2.75 daily
    # budget was spent -- the 2026-08-28 11:30 ET stop hit this at 17 cents
    # of actual spend. max_mode_daily_exposure_pct below is the real,
    # dollar-based per-mode limit now; this exists only to stop a retry loop
    # spinning forever within one mode without ever spending real money (a
    # provably-zero-cost failure loop, now possible after the Defect 2 fix,
    # would never trip a dollar-based check at all) -- an infinite-loop
    # backstop, not a budget.
    max_paid_sessions_per_mode_per_day: int = Field(default=8, ge=1)
    # Defect 4 operative per-mode limit: the fraction of
    # daily_reserved_exposure_limit_usd any ONE mode may reserve/spend in a
    # single ET day. A fraction of the existing day-wide exposure ceiling
    # (not an independent dollar figure) so it stays proportionate if that
    # ceiling is ever retuned, and because a mode's own call cost varies too
    # much for a flat count to fit every mode (an intra_check tick can be a
    # few cents; a morning portfolio_manager call can be tens of cents).
    # 100 disables the per-mode ceiling (falls back to the day-wide one).
    max_mode_daily_exposure_pct: float = Field(default=60.0, gt=0.0, le=100.0)
    # Phase 6.1 afternoon reserve: the fraction of daily_reserved_exposure_
    # limit_usd that is NOT spendable by any session before
    # afternoon_reserve_release_et_hour, regardless of mode. The morning is
    # where the cheap, plentiful setups look most attractive and where a
    # retry storm is most likely; the afternoon is where every exit decision
    # lives (position_reviewer, risk_manager, the close pass). A day that
    # spends itself out by noon has funded entries and defunded exits, which
    # is exactly backwards for capital preservation. 0 disables the reserve.
    afternoon_reserve_pct: float = Field(default=40.0, ge=0.0, le=90.0)
    # ET hour (0-23, local wall clock) at which the reserve above releases
    # and the full daily_reserved_exposure_limit_usd becomes spendable again.
    afternoon_reserve_release_et_hour: int = Field(default=12, ge=0, le=23)
    # Includes the initial request.  Two means one transient retry at most;
    # a provider failover would be attempt three and is blocked/latches.
    max_provider_attempts_per_call: int = Field(default=2, ge=1)
    # Aggregate retries across parallel specialist calls in one run.
    max_retry_attempts_per_session: int = Field(default=2, ge=0)
    reservation_ttl_minutes: int = Field(default=30, ge=5, le=180)
    reservation_multiplier: float = Field(
        default=1.05, ge=1.0, le=2.0, allow_inf_nan=False,
    )
    # Defect 1 (2026-08-28): the pre-fix reservation treated one UTF-8 byte
    # of the prompt as one token and always reserved the full
    # max_output_tokens ceiling. On the production 09:32 ET portfolio_manager
    # call that reserved $1.8657 against a call that actually cost ~$0.11 --
    # ~3.2x the worst real portfolio_manager call ever recorded (measured
    # $0.5783) and ~11x the average ($0.1718). Below this, the reservation
    # is instead derived from that agent+model's own recent history in
    # agent_logs (see LLMCostCircuitBreaker._measure_reservation_tokens).
    # Below the minimum sample count, or on any unknown model/agent or
    # error reading history, the ORIGINAL byte=token / max_output_tokens
    # formula is still the fallback -- conservative, unchanged, and now
    # exercised only as a fail-closed floor rather than every call.
    #
    # Minimum number of (agent, model) rows in agent_logs required before
    # trusting measured history at all. Below this, guessing from a
    # handful of calls is worse than the conservative fallback.
    reservation_min_history_samples: int = Field(default=20, ge=10, le=1000)
    # Percentile of the observed prompt-bytes-per-token ratio used to
    # convert this call's prompt size into a token estimate. A LOW ratio
    # means MORE tokens per byte -- denser text, therefore a HIGHER cost --
    # so the low percentile is the conservative end; bounded well below the
    # median (lt=0.5) so a misconfiguration can't quietly pick the cheap
    # end of the distribution.
    reservation_conservative_percentile: float = Field(
        default=0.10, gt=0.0, lt=0.5, allow_inf_nan=False,
    )
    # Safety margin applied to the maximum output tokens observed for this
    # agent+model's history; the result is still capped at that call's own
    # max_output_tokens, so this can only ever narrow the old
    # always-reserve-the-ceiling behaviour, never widen past it.
    reservation_output_margin: float = Field(
        default=1.20, ge=1.0, le=3.0, allow_inf_nan=False,
    )

    # === OpenRouter pricing staleness grace window (SPOF fix, 2026-08-28) ===
    # Before this fix, `cost_table.refresh_openrouter_pricing()` accepted a
    # cached rate ONLY while under 24h old. Past that boundary it had to
    # reach openrouter.ai/api/v1/models or return False, and both
    # `TradingPipeline.__init__` and `activate_paid_call_session()` respond
    # to False with `breaker.mark_unavailable(...)` -- the durable,
    # cross-process emergency latch that `LLMCostCircuitBreaker.reset()`
    # (operator-only, reason mandatory) is the sole way to clear. Because the
    # cache file is only rewritten when a fetch actually happens, and a fetch
    # only happens once the cache is ALREADY stale, this meant one
    # openrouter.ai outage overlapping the first session after the 24h mark
    # -- verified reproducible 2026-08-28 via
    # test_mandatory_openrouter_refresh_rejects_stale_cache_when_network_is_down
    # -- could stop every future session, including the next day's, until a
    # human ran `reset()` by hand. The desk runs unattended specifically
    # because the owner cannot be relied on to intervene quickly, so a
    # guardrail whose failure mode is "wait for a human" defeats the reason
    # it exists.
    #
    # A price that turned stale five minutes ago is a different fact from a
    # price nobody has ever fetched: OpenRouter's routed rates change on the
    # order of once a quarter, not hour to hour, and the figure only ever
    # BOUNDS a reservation that already carries `reservation_multiplier` on
    # top. So: within this many hours PAST the 24h freshness boundary, a
    # cache that can't be refreshed live is used rather than latched --
    # widened per `openrouter_pricing_stale_multiplier_max` below and logged
    # loudly on every call -- and only a cache older than 24h + this grace,
    # or no cache at all, or a cache missing a rate for a model actually
    # configured, still fails closed exactly as before. 0 restores the
    # pre-fix behaviour (fail the instant the cache turns stale) for anyone
    # who wants it back.
    openrouter_pricing_grace_period_hours: float = Field(
        default=24.0, ge=0.0, le=168.0, allow_inf_nan=False,
    )
    # Reservation multiplier applied at the FAR edge of the grace window
    # above (`cost_table.openrouter_pricing_reservation_multiplier` scales
    # linearly from `reservation_multiplier` itself -- i.e. no widening at
    # all -- the instant the cache turns stale, up to this value once the
    # cache is about to age out of grace entirely). Deliberately allowed
    # above `reservation_multiplier`'s own 2.0 ceiling: the entire point is
    # a WIDER margin than an in-hours call gets, proportional to how old the
    # bound actually is, never a narrower one.
    openrouter_pricing_stale_multiplier_max: float = Field(
        default=1.50, ge=1.0, le=5.0, allow_inf_nan=False,
    )

    @model_validator(mode="after")
    def _daily_not_below_session(self):
        if self.enabled is not True:
            raise ValueError(
                "llm_cost_circuit.enabled must remain true; paid-analysis protection is mandatory"
            )
        if self.require_telegram_alerts is not True:
            raise ValueError(
                "llm_cost_circuit.require_telegram_alerts must remain true; "
                "shutdown notification is mandatory"
            )
        if self.daily_cost_limit_usd < self.session_cost_limit_usd:
            raise ValueError("daily_cost_limit_usd must be >= session_cost_limit_usd")
        if self.session_reserved_exposure_limit_usd < self.session_cost_limit_usd:
            raise ValueError(
                "session_reserved_exposure_limit_usd must be >= session_cost_limit_usd"
            )
        if self.daily_reserved_exposure_limit_usd < self.daily_cost_limit_usd:
            raise ValueError(
                "daily_reserved_exposure_limit_usd must be >= daily_cost_limit_usd"
            )
        if (self.daily_reserved_exposure_limit_usd
                < self.session_reserved_exposure_limit_usd):
            raise ValueError(
                "daily_reserved_exposure_limit_usd must be >= "
                "session_reserved_exposure_limit_usd"
            )
        if self.openrouter_pricing_stale_multiplier_max < self.reservation_multiplier:
            raise ValueError(
                "openrouter_pricing_stale_multiplier_max must be >= reservation_multiplier "
                "-- a reservation built on stale pricing must never be LESS conservative "
                "than one built on fresh pricing"
            )
        return self


class EvolutionConfig(BaseModel):
    """Quarterly meta-reflection prompt-evolution settings.

    `enabled=False` is the safe default — PR3 (the meta_reflector) writes
    reflection.json to disk but the editor never runs. Flip to True only
    after reviewing a quarter or two of reflection.json contents by hand.
    Every guard below is redundantly enforced in src/evolution/prompt_editor.py;
    this block makes them tunable per deployment.
    """
    enabled: bool = False
    """Master switch. PR4 default is False — the editor stays dormant
    until explicitly flipped. Flipping back to False does not retract
    already-applied learnings; use the retract path in the reflector."""

    auto_commit: bool = True
    """After successful prompt edits, `git add` + `git commit` each
    modified prompt file so `git revert <hash>` provides a one-shot
    rollback for a whole quarter's evolution. Only meaningful when
    `dry_run=False`."""

    dry_run: bool = True
    """Default True for safety. When True, `PromptEditor.apply_reflection`
    does NOT modify any prompt file — instead it writes the proposed
    edits to `data/evolution/{period}/proposed_edits.json` for human
    review. To actually apply a quarter's proposals, flip `dry_run` to
    False temporarily and re-run `python main.py --mode meta --force`,
    OR edit the prompt files by hand using the JSON as a reference.

    Reason this defaults True (audit H3 follow-up): meta-reflection
    auto-fires from evening on quarter-end (added in Round 2). A bad
    learning landing as an auto-commit is silently degrading — affects
    every decision until next quarter or until operator notices via git
    log. The 4 gates (FIFO cap / Jaccard dedup / prohibited-words regex
    / agent allowlist) catch obvious bad learnings but not subtle
    polarity-flipped polite proposals. Keep dry_run=True for the first
    2-3 quarters; once the proposals track operator's expectations,
    flip to False."""

    max_agents_per_cycle: int = 3
    """Hard cap — at most N agents get edited per quarterly run even if
    the meta-reflector proposes more. Schema cap on proposed_learnings
    is already 3; this is the second belt."""

    max_learnings_per_agent: int = 10
    """FIFO buffer per agent prompt. When an append would push past the
    cap, the oldest auto-added entry (by date-tag, not manual) is
    rolled off before the new one is appended."""

    max_learning_chars: int = 200
    """Upper bound per entry. Schema enforces ≥20 already; this is the
    ≤200 end. Prevents prompt bloat."""

    min_justification_chars: int = 40
    """Schema floor on PromptLearning.justification. Echoed here so a
    deployment can tighten it (the schema's 40 is the loosest allowed)."""

    jaccard_dedup_threshold: float = 0.6
    """Token-level Jaccard similarity against EACH existing entry in
    the target agent's Learnings section. If any pair exceeds this,
    the new entry is treated as a near-duplicate and rejected.
    0.6 tuned loose — catches paraphrases without rejecting legitimately
    similar-topic learnings written differently."""

    prohibited_words: list[str] = Field(
        default_factory=lambda: [
            "never", "always", "override", "ignore all",
            "must always", "must never",
        ],
    )
    """Case-insensitive word-boundary regex check on learning_text. These
    directly conflict with invariant wording in the core prompts (e.g.
    RM's 'ALWAYS require stop_loss'); letting an LLM append a 'never' rule
    can flip the hard discipline."""

    protected_agents: list[str] = Field(
        default_factory=lambda: ["risk_manager", "position_reviewer"],
    )
    """Agents whose prompts the editor MUST NOT touch. The Pydantic
    MetaReflectionAgentName literal already excludes these — this is
    the second belt at the editor layer."""


class NotificationsConfig(BaseModel):
    """Where Telegram alerts point the operator back into Mission Control.

    The operator reads these on his phone. He got a BUY CRM alert whose
    rationale read "...strong heavy accumulation volume" and just stopped
    there mid-sentence, with no way to see the rest or jump into the
    dashboard for the full picture. `mission_control_url` is the tap-through
    target `TelegramNotifier.send()` appends as an HTML link to relevant
    alerts (see src/notifier.py, src/trader_feed.py). An empty string
    disables the link entirely — never emit a broken one instead.

    Defaults to the tailnet address Tailscale Serve exposes for the qamc
    API (`ovh-vps.wallaby-bowfin.ts.net`, proxying tailnet-only port 443 to
    the API on 127.0.0.1:8800), which mounts the cockpit
    (`app.mount("/cockpit", ...)` in src/api/server.py). Unreachable from
    the public internet, matching Mission Control's "private, read-only,
    non-critical to trading" posture.
    """

    mission_control_url: str = "https://ovh-vps.wallaby-bowfin.ts.net/cockpit/"
    """Base URL Telegram alerts link to. Empty string = no link. Must be
    http(s) when non-empty — the value lands inside an href="..." attribute,
    and rejecting other schemes here (e.g. an accidental "javascript:") is
    cheaper than relying on Telegram's client-side handling of it."""

    @field_validator("mission_control_url")
    @classmethod
    def _validate_scheme(cls, v: str) -> str:
        v = v.strip()
        if v and not (v.startswith("http://") or v.startswith("https://")):
            raise ValueError(
                "notifications.mission_control_url must be http:// or "
                "https:// (or empty, to disable the link) — got: " + v
            )
        return v


class ReconciliationConfig(BaseModel):
    """Broker-truth reconciliation of the `trades` ledger against Alpaca.

    2026-08-28 ONDS/CCJ incident: both positions were closed by their
    broker-resident protective stop (a GTC stop-limit order placed by
    `AlpacaBroker.place_entry_protection` / `_repair_stop_coverage` /
    `shift_stops_down`, none of which ever wrote a `trades` row for the
    stop order itself). The stop fired, the position vanished from the
    broker, and the ledger never heard about it — the BUY rows sat forever
    at `realized_pnl IS NULL` and the `positions` table (synced directly
    from broker truth) quietly diverged from the story `trades` told.
    `_reconcile_stop_out_fills` (src/pipeline.py) closes that gap by
    diffing the ledger's own implied share count against the broker's
    actual position and pulling any untracked filled SELL order it finds.
    """

    stop_out_lookback_days: int = Field(default=7, ge=1, le=60)
    """How far back to ask the broker for filled SELL orders when the
    ledger believes a symbol is still (partly) held but the broker shows
    less. Wide enough to survive a multi-day outage of the reconciler
    itself (weekends + a stuck timer) without being so wide it makes the
    per-session broker query expensive. Alpaca's own order-history
    retention is the real outer bound this can't exceed."""


class AppConfig(BaseModel):
    api_keys: ApiKeysConfig
    alpaca: AlpacaConfig
    llm: LLMConfig
    execution: ExecutionConfig = Field(default_factory=ExecutionConfig)
    risk: RiskConfig
    trading: TradingConfig
    storage: StorageConfig
    llm_cost_circuit: LLMCostCircuitConfig = Field(default_factory=LLMCostCircuitConfig)
    evolution: EvolutionConfig = Field(default_factory=EvolutionConfig)
    # Optional section — a settings.yaml without it gets a disabled sweeper
    # (enabled=False default), so older configs keep working unchanged.
    cash_sweep: CashSweepConfig = Field(default_factory=CashSweepConfig)
    # Optional section — a settings.yaml without it gets the scan disabled
    # (enabled=False default), so intra_check's existing behavior is
    # unchanged unless explicitly opted in.
    intraday_scan: IntradayScanConfig = Field(default_factory=IntradayScanConfig)
    smart_money: SmartMoneyConfig = Field(default_factory=SmartMoneyConfig)
    # Optional section — a settings.yaml without it gets the documented
    # default lookback (7 days), so older configs keep working unchanged.
    reconciliation: ReconciliationConfig = Field(default_factory=ReconciliationConfig)
    # Optional section — a settings.yaml without it gets the tailnet cockpit
    # default (see NotificationsConfig docstring), so older configs keep
    # alerting exactly as before, just with a link added.
    notifications: NotificationsConfig = Field(default_factory=NotificationsConfig)

    @model_validator(mode="after")
    def _check_llm_provider_keys(self):
        openai_models = []
        anthropic_models = []
        deepseek_models = []
        openrouter_models = []

        # Bucket by resolve_provider(model, explicit_provider) — the SAME
        # helper BaseAgent.__init__ uses to pick a client — rather than
        # re-deriving prefix logic here. An agent with an explicit
        # `*_provider` override is bucketed by that override, not by
        # whatever its model string's prefix would otherwise imply; this is
        # what makes an OpenRouter "vendor/model" id (which would otherwise
        # mis-bucket as Anthropic) require OPENROUTER_API_KEY instead.
        for agent_name in AGENT_NAMES:
            model_name = getattr(self.llm, f"{agent_name}_model")
            explicit_provider = self.llm.get_provider(agent_name)
            provider = resolve_provider(model_name, explicit_provider)
            label = f"{agent_name}_model={model_name}" + (
                f" (provider={explicit_provider})" if explicit_provider else ""
            )
            if provider == "deepseek":
                deepseek_models.append(label)
            elif provider == "openrouter":
                openrouter_models.append(label)
            elif provider == "openai":
                openai_models.append(label)
            else:
                anthropic_models.append(label)

        if openai_models and not self.api_keys.openai:
            selected = ", ".join(openai_models)
            raise ValueError(
                f"OPENAI_API_KEY is required for selected OpenAI models: {selected}"
            )

        if deepseek_models and not self.api_keys.deepseek:
            selected = ", ".join(deepseek_models)
            raise ValueError(
                f"DEEPSEEK_API_KEY is required for selected DeepSeek models: {selected}"
            )

        if openrouter_models and not self.api_keys.openrouter:
            selected = ", ".join(openrouter_models)
            raise ValueError(
                f"OPENROUTER_API_KEY is required for selected OpenRouter models: {selected}"
            )

        if anthropic_models and not self.api_keys.anthropic:
            selected = ", ".join(anthropic_models)
            raise ValueError(
                f"ANTHROPIC_API_KEY is required for selected Anthropic models: {selected}"
            )

        return self


def _substitute_env_vars(value: str) -> str:
    """Replace ${VAR_NAME} with environment variable values."""
    def replacer(match):
        var_name = match.group(1)
        env_value = os.environ.get(var_name)
        if env_value is None:
            return ""  # Optional env vars resolve to empty string
        return env_value
    return re.sub(r"\$\{(\w+)\}", replacer, value)


def _walk_and_substitute(obj):
    """Recursively substitute env vars in all string values."""
    if isinstance(obj, str):
        return _substitute_env_vars(obj)
    if isinstance(obj, dict):
        return {k: _walk_and_substitute(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_walk_and_substitute(item) for item in obj]
    return obj


def load_config(path: Path) -> AppConfig:
    with open(path) as f:
        raw = yaml.safe_load(f)
    substituted = _walk_and_substitute(raw)
    return AppConfig(**substituted)
