import os
import re
from pathlib import Path

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

from src.agents.base import (
    VALID_PROVIDERS,
    provider_attempt_budget,
    resolve_provider,
)


class ApiKeysConfig(BaseModel):
    anthropic: str
    openai: str = ""
    deepseek: str = ""
    # OpenRouter (Stage 1 QAMC provider/model plumbing) — optional, only
    # required when an agent's explicit `provider: openrouter` is selected
    # (enforced in AppConfig._check_llm_provider_keys, not here, since that's
    # the layer that already knows which agents are configured for it).
    openrouter: str = ""
    # Google AI Studio direct (2026-08-31 owner decision: gemini-3.5-flash-lite
    # direct becomes the PRIMARY route for the eight specialist/review seats) —
    # optional, only required when an agent's explicit `provider: google` is
    # selected, or google is reachable as the configured cross-provider
    # failover target (both enforced in AppConfig._check_llm_provider_keys).
    google: str = ""
    fred: str
    alpaca_key: str
    alpaca_secret: str

    @model_validator(mode="after")
    def _check_required_keys(self):
        for field_name in ("alpaca_key", "alpaca_secret", "fred"):
            if not getattr(self, field_name):
                raise ValueError(f"Required API key '{field_name}' is empty — check your .env file")
        if not (
            self.anthropic or self.openai or self.deepseek
            or self.openrouter or self.google
        ):
            raise ValueError(
                "At least one of 'anthropic', 'openai', 'deepseek', 'openrouter', "
                "or 'google' API key must be set"
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
        boundary in CLAUDE.md, docs/STATE.md and AGENTS.md, but until now
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
    # Cross-provider FAILOVER target (2026-08-31 owner decision) — process-
    # wide, not per-agent, so the target can't silently drift seat-by-seat.
    # Threaded through pipeline.py into every BaseAgent.__init__ (see
    # src/agents/base.py's _DEFAULT_FALLBACK_PROVIDER/_DEFAULT_FALLBACK_MODEL,
    # BaseAgent._failover_reachable). Default pairs OpenRouter (paid, backup)
    # with the SAME model Google AI Studio direct serves as the primary for
    # the eight specialist/review seats: a failover changes the ROAD, not the
    # REASONING — the owner's explicit objection to the inherited
    # claude-opus-4-7 Anthropic fallback. Not to be confused with `max_tokens`
    # below, an unrelated per-call OUTPUT ceiling that also uses the word
    # "fallback" for its own inherited-by-every-agent meaning.
    fallback_provider: str = "openrouter"
    fallback_model: str = "google/gemini-3.5-flash-lite"
    # Global output-ceiling fallback — used by any agent without an explicit
    # override below.
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

    @field_validator("fallback_provider")
    @classmethod
    def _fallback_provider_is_valid(cls, v: str) -> str:
        # Unlike the per-agent `*_provider` fields, this one has no "unset ->
        # infer from prefix" escape hatch — it names a provider directly, so
        # a typo must fail loudly rather than silently resolving to whatever
        # `fallback_model`'s prefix happens to imply.
        normalized = (v or "").strip().lower()
        if normalized not in VALID_PROVIDERS:
            raise ValueError(
                f"Invalid llm.fallback_provider {v!r}; must be one of "
                f"{sorted(VALID_PROVIDERS)}"
            )
        return normalized

    @field_validator("fallback_model")
    @classmethod
    def _fallback_model_is_nonempty(cls, v: str) -> str:
        if not isinstance(v, str) or not v.strip():
            raise ValueError("llm.fallback_model must be a non-empty model id")
        return v.strip()

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

    repeg_enabled: bool = False
    """Master switch for bounded entry re-pegging. OFF by default so the
    feature can be deployed dark: with it off, `_repeg_entry_order` returns
    the original order id untouched and not a single broker call is made."""

    repeg_max_attempts: int = Field(default=2, ge=1, le=5)
    """Hard cap on replacements per entry order. A replacement mints a new
    order id at Alpaca, so an unbounded loop is an unbounded chain of
    untracked ids; low single digits is the whole point."""

    repeg_poll_seconds: float = Field(default=5.0, gt=0, le=30)
    """How long to let the working order rest before each re-peg attempt.
    Total added latency per entry is bounded by
    `repeg_max_attempts * repeg_poll_seconds`, and lands BEFORE
    `place_entry_protection`'s own fill wait."""


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
    # --- Stage 3 (shorts) -----------------------------------------------
    # The single-short ceiling is deliberately HALF of `max_position_pct`:
    # a long's loss is bounded at -100% of the position, a short's is not,
    # so the per-name concentration budget for one short is tighter than
    # for one long. Both caps below are HARD BLOCKS in the deterministic
    # risk engine (src/risk/rules.py) on opening/adding a short — never on
    # a COVER, which mirrors the existing exits-fail-open asymmetry.
    max_single_short_pct: float = Field(default=10.0, gt=0, le=100)
    # The largest total gross BEARISH exposure across the whole book, as a
    # percent of equity — true shorts (qty < 0) plus LONG positions in an
    # inverse/leveraged ETF (SH, SDS, PSQ, SQQQ; see `_ETF_LEVERAGE` in
    # `src/risk/rules.py`), since holding one of those long is bearish
    # exposure too. Renamed from `max_short_gross_pct` (2026-08-30): the old
    # name summed only true shorts, leaving an inverse-ETF long invisible to
    # it — the desk could sit at the full short ceiling AND hold a full
    # inverse-ETF position at once and be materially more bearish than
    # either limit intended. The rename reflects what the ceiling actually
    # measures now, not just what enforces it.
    max_gross_bearish_pct: float = Field(default=20.0, gt=0, le=200)
    # Sizing-only haircut (never applied to stop placement) on a short's
    # risk-per-share. A short gaps through its stop upward with no bound —
    # equal nominal risk is not equal real risk — so the same risk
    # allocation opens a SMALLER short than an equivalent long.
    short_gap_risk_multiple: float = Field(default=1.5, gt=1.0, le=3.0)
    # --- Spec §9.4 "agreement earns size" --------------------------------
    # Ceiling on `TargetPosition.risk_allocation_pct`, indexed by the
    # number of independent seats (of technical/news/earnings/macro/
    # smart_money) whose canonical stance is directionally aligned with
    # the target's proposed action — see `src/risk/rules.py::
    # count_aligned_sources` / `agreement_ceiling_for_count`. Index 0 is
    # the ceiling for 1 (or 0 — see `agreement_ceiling_for_count`) aligned
    # source, index 4 is for 5. A REDUCTION only: applied in the
    # constructor strictly BEFORE `allocate_risk_budget` and the
    # single-name clamps, so it can shrink what a target receives but can
    # never grow it past what the PM asked for or past
    # `max_position_risk_pct` (enforced below and again at the point of
    # use).
    #
    # Measured against production `agent_logs` 2026-08-25 through 08-28 —
    # the pre-nomination "technical-analysis bot" era the spec describes,
    # and the most conservative case this schedule has to survive: of 75
    # opening/increasing targets carrying live provenance, 67% (50/75)
    # named exactly ONE aligned source (always `technical`), 29% (22/75)
    # named two, 4% (3/75) named three, and NONE ever reached four or
    # five. A schedule with tier 1 near the 5% envelope would do nothing;
    # one with tier 1 much below ~2% would have clamped roughly nine in
    # ten of the book's trades to a token size. [3.0, 4.0, 5.0, 5.0, 5.0]
    # cuts single-source risk 40% (5.0% -> 3.0%, still 6x the 0.5% floor)
    # and two-source risk 20%, while leaving three-or-more-source
    # agreement — the rare, never-yet-observed high-conviction case — at
    # the full envelope.
    agreement_ceiling_pct: list[float] = Field(
        default_factory=lambda: [3.0, 4.0, 5.0, 5.0, 5.0],
    )

    @model_validator(mode="after")
    def _agreement_ceiling_is_well_formed(self):
        schedule = self.agreement_ceiling_pct
        if len(schedule) != 5:
            raise ValueError(
                "risk.agreement_ceiling_pct must have exactly 5 entries "
                f"(1..5 aligned sources); got {len(schedule)}"
            )
        if any(v <= 0 for v in schedule):
            raise ValueError("risk.agreement_ceiling_pct entries must be > 0")
        if any(v > self.max_position_risk_pct for v in schedule):
            raise ValueError(
                "risk.agreement_ceiling_pct entries must never exceed "
                f"max_position_risk_pct ({self.max_position_risk_pct}) — "
                "the agreement ceiling can only narrow the per-trade "
                "envelope, never widen it"
            )
        if any(b < a for a, b in zip(schedule, schedule[1:])):
            raise ValueError(
                "risk.agreement_ceiling_pct must be non-decreasing — more "
                "independent agreement can never earn a SMALLER ceiling"
            )
        return self

    @model_validator(mode="before")
    @classmethod
    def _reject_renamed_short_gross_key(cls, data):
        # `max_short_gross_pct` was renamed to `max_gross_bearish_pct`
        # (2026-08-30) when the ceiling was widened to also count LONG
        # inverse-ETF positions, not just true shorts — the meaning of the
        # setting genuinely changed, so a name that still said "short" would
        # be a lie. BaseModel's default `extra="ignore"` would let a
        # settings.yaml still carrying the old key load silently, quietly
        # dropping whatever value an operator set and falling back to the
        # 20.0 default — exactly the doc-versus-behaviour drift this rename
        # exists to stop. Fail loudly instead (same pattern as
        # `LLMCostCircuitConfig._reject_renamed_free_failure_key`).
        if isinstance(data, dict) and "max_short_gross_pct" in data:
            raise ValueError(
                "risk.max_short_gross_pct has been renamed to "
                "risk.max_gross_bearish_pct -- update the settings file "
                "(no alias is provided)"
            )
        return data


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


class NominationConfig(BaseModel):
    """Phase 9 (`docs/QAMC_REMEDIATION_SPEC.md` §9.1/§9.2) — bounds on how
    many candidates the News/Earnings/Macro seats may put in front of
    Technical each run. Mirrors the SEC Form 4 smart-money admission cap
    (`SmartMoneyConfig.max_external_candidates`), the working precedent
    this generalises: a bounded, deterministic cap is what keeps an
    on-demand responder call affordable, not a judgment call made per run.
    """
    # Applied FIRST, per seat, before cross-seat dedupe: a single seat
    # cannot flood the responder pass. Same default (3) as
    # smart_money.max_external_candidates by design — one seat's bounded
    # nomination budget should look like the existing external-admission
    # budget an operator already understands.
    max_per_seat_per_run: int = Field(default=3, ge=1, le=10)
    # Applied AFTER cross-seat dedupe: the hard ceiling on how many
    # DISTINCT symbols may reach the on-demand Technical responder call in
    # one run, regardless of how many seats nominated or how many raw
    # nominations survived the per-seat cap.
    max_total_per_run: int = Field(default=6, ge=1, le=20)


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
    # Counts sessions in one mode that made a provider attempt and settled
    # at zero cost -- a RUNAWAY BACKSTOP, not the working budget (Defect 4,
    # 2026-08-28). Until the Defect 4 fix it was 2 and it WAS the binding
    # constraint on every trading day: intra_check fires 14 times between
    # 09:30 and 16:00 ET,
    # two of those could think and the other twelve suspended. Measured
    # 2026-08-25/26/27: 4, 7 and 6 suspensions per day while only $1.02 of a
    # $2.75 daily budget was spent -- the 2026-08-28 11:30 ET stop hit this
    # at 17 cents of actual spend. max_mode_daily_exposure_pct below is the
    # real, dollar-based per-mode limit; this exists only to stop a retry
    # loop spinning forever within one mode without ever spending real money
    # (a provably-zero-cost failure loop, now possible after the Defect 2
    # fix, would never trip a dollar-based check at all) -- an infinite-loop
    # backstop, not a budget.
    #
    # Defect 4.1 (2026-08-29): raising this number was never the fix,
    # because the counting query itself was wrong -- it counted every
    # session that did ANY work (`logical_calls>0 OR provider_attempts>0`),
    # including successful, money-spending ones, so a normal trading day
    # burned the backstop down on its own. Operators kept raising it (2 ->
    # 8 -> 40 in config/settings.yaml) to keep the desk running, which
    # disabled the guard instead of fixing it. `LLMCostCircuitBreaker.
    # begin_call` now counts only sessions that made provider attempts and
    # settled at zero cost (see its comment at the check site) -- under
    # that corrected count, 8 means "eight entirely free, entirely failed
    # sessions in one mode in one day", which is unambiguous breakage. 40
    # was never a real ceiling; it was the old counter's false-positive
    # rate.
    max_free_failure_sessions_per_mode: int = Field(default=8, ge=1)
    # Bounded cooling-off window for the backstop above (Defect 4.1,
    # 2026-08-29): a zero-cost failure loop is almost always a transient
    # provider outage, and latching a mode dark for the rest of the ET day
    # on a transient is exactly the 2026-08-28 failure this remediation
    # exists to stop. The dollar ceilings above are what actually protect
    # money; this guard only needs to stop a spin for a while, then get out
    # of the way -- see LLMCostCircuitBreaker.begin_call. 5..720 minutes
    # (5 minutes .. 12 hours) keeps it from being tuned into either a no-op
    # or a de-facto day-long latch again.
    backstop_cooloff_minutes: int = Field(default=60, ge=5, le=720)
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
    # Ceiling on provider attempts within ONE logical agent call, counting
    # the initial request. NOT an independent number: it must cover what
    # `BaseAgent.run()`'s retry loop can actually spend, or the circuit trips
    # on the loop's own designed behaviour instead of on anything unsafe.
    # Derived by default from `provider_attempt_budget()`, which owns that
    # arithmetic; `AppConfig._check_provider_attempt_budget` rejects any
    # explicit value below it at load time. See the 2026-08-31 incident
    # recorded on `provider_attempt_budget`.
    #
    # Setting it HIGHER than the derived floor is allowed and does not grant
    # extra attempts — the retry loop, not this ceiling, decides how many
    # requests are made. This only decides when the circuit intervenes.
    max_provider_attempts_per_call: int = Field(
        default_factory=lambda: provider_attempt_budget(failover_available=True),
        ge=1,
    )
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

    @model_validator(mode="before")
    @classmethod
    def _reject_renamed_free_failure_key(cls, data):
        # `max_paid_sessions_per_mode_per_day` was renamed to
        # `max_free_failure_sessions_per_mode` (2026-08-29) once the
        # counting query it gates stopped counting paid sessions at all --
        # it now counts sessions that made a provider attempt and settled
        # at zero cost. BaseModel's default `extra="ignore"` would let a
        # settings.yaml still carrying the old key load silently, quietly
        # dropping whatever value an operator set and falling back to the
        # field default -- exactly the doc-versus-behaviour drift this
        # rename exists to stop. Fail loudly instead.
        if isinstance(data, dict) and "max_paid_sessions_per_mode_per_day" in data:
            raise ValueError(
                "llm_cost_circuit.max_paid_sessions_per_mode_per_day has been "
                "renamed to llm_cost_circuit.max_free_failure_sessions_per_mode "
                "-- update the settings file (no alias is provided)"
            )
        return data

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


class NewsConfig(BaseModel):
    """Prompt-size control for the news seat (src/data/news.py).

    Added 2026-08-29 when RSS_FEEDS was widened from 8 to 11 sources (see
    the audit comment block at the top of src/data/news.py). More feeds
    means more raw items per fetch; `max_prompt_items` is the one knob that
    keeps what actually reaches the LLM bounded regardless of how many
    wires are configured. Previously this was a hardcoded
    `max_items=50` default on NewsDataProvider.format_for_prompt() — moved
    here per the repo's standing rule that any cap/threshold lives in
    config, not a module constant, so it can be tuned without a code
    change and is visible next to the other cost-relevant knobs.
    """

    max_prompt_items: int = Field(default=50, ge=1, le=500)
    """Max news items placed in the analyst's prompt after dedup. 50 is the
    pre-existing behavior (the old hardcoded default) — widening the feed
    set does not by itself raise this, so prompt size does not grow just
    because more wires are configured."""

    # --- Per-symbol news (2026-08-30 owner decision) -----------------------
    # The 2026-08-29 audit (src/data/news.py comment block) verified Yahoo
    # Finance's per-symbol RSS live and working, but deliberately left it
    # unwired: at the full ~101-symbol trading.universe it would be
    # 101-202 extra requests/run to a free endpoint with no documented
    # rate-limit tolerance — a real hammering risk — and scoping it to
    # "only the symbols this run actually cares about" needed portfolio
    # state threaded into the fetch call, which was a scope decision for the
    # owner rather than something to bolt on silently. The owner has now
    # made that call: free sources only, scoped to held positions + this
    # run's admitted candidates. These four settings are the caps that make
    # that safe — see `src/data/news.py::NewsDataProvider.fetch_news`.
    per_symbol_enabled: bool = True
    """Master switch. False disables per-symbol fetching entirely (zero
    added requests) regardless of the caps below — an operator emergency-off
    that doesn't require also zeroing out per_symbol_max_symbols."""

    per_symbol_max_symbols: int = Field(default=15, ge=0, le=30)
    """Hard cap on how many symbols get an individual per-symbol RSS fetch in
    one run. This is the one knob standing between this feature and the
    101-request hammering risk the 2026-08-29 audit flagged and refused to
    ship without — and it is enforced a second time inside
    NewsDataProvider itself (not only by the caller's symbol selection), so
    a future caller bug that passes the whole ~101-symbol universe still
    cannot regress to anywhere near 101 requests. Default 15: the live book
    measured 2026-08-30 held 6 positions, and the run's candidate budgets
    (smart_money.max_external_candidates=3,
    nominations.max_total_per_run=6) bound how many more can be admitted in
    one run — 15 covers that combined worst case with headroom for the book
    to grow, at one request per symbol per run. The ge=0/le=30 bounds keep an
    operator typo from silently reopening the 101-request risk (le=30 is
    already generous — it is under a third of the ~101-symbol universe)."""

    per_symbol_max_prompt_items: int = Field(default=15, ge=0, le=100)
    """Of the items that make it into the analyst's prompt (bounded overall
    by `max_prompt_items`), at most this many may be per-symbol-sourced.
    Keeps a flood of single-name headlines (e.g. every held position
    publishing something the same morning) from crowding out the general
    wire feeds that the rest of `max_prompt_items` exists to carry."""

    per_symbol_requests_per_second: float = Field(default=2.0, ge=0.2, le=10.0)
    """Politeness throttle for per-symbol Yahoo Finance requests, same
    request-interval-from-rate convention as
    `smart_money.requests_per_second` (see `SECForm4Provider`'s
    `request_interval_s` / `_RATE_LOCK` in src/data/smart_money.py, mirrored
    for this feed in src/data/news.py). Yahoo's per-symbol RSS endpoint has
    no documented rate-limit tolerance (2026-08-29 audit), so this defaults
    far below smart_money's SEC-sanctioned 8 req/s."""


class MacroConfig(BaseModel):
    """FRED fetch resilience for the macro seat (src/data/macro.py).

    Added Phase 4.2 after production evidence of a full outage: on
    2026-08-26 17:01:29-17:03:49 UTC all nine FRED series failed in ONE run
    with "The read operation timed out", using what was then a
    single-retry / flat-2-second-backoff policy hardcoded as module
    constants (`_FRED_MAX_RETRIES` / `_FRED_RETRY_BACKOFF_S` /
    `_FRED_BREAKER_AFTER_FAILED_SERIES`, added 2026-08-20 off an earlier,
    smaller incident). Per the repo's standing rule that a number able to
    change behaviour is an operator setting, not a constant buried in code,
    these move here — mirrored through to `MacroDataProvider.__init__` the
    same way `smart_money.insider_*` threads into `SECForm4Provider`
    (src/pipeline.py passes every field below explicitly at construction).
    """

    request_timeout_s: float = Field(default=15.0, ge=1.0, le=60.0)
    """Per-HTTP-request socket timeout. 15s is generous — FRED typically
    responds in well under a second; slower than that is network/service
    trouble worth degrading gracefully from rather than hanging on."""

    max_retries: int = Field(default=2, ge=0, le=5)
    """Bounded retries per series BEFORE the consecutive-failure breaker
    (below) trips. Raised from the old hardcoded 1 — a single retry with a
    flat 2s backoff was not enough margin to ride out the network blips
    behind the 2026-08-26 incident. Still bounded: see
    breaker_after_failed_series and total_fetch_deadline_s for why more
    retries can't turn into an unbounded stall."""

    retry_backoff_base_s: float = Field(default=2.0, gt=0, le=30.0)
    """First retry's backoff, in seconds. Doubles each subsequent retry,
    capped at retry_backoff_max_s (see MacroDataProvider._next_backoff)."""

    retry_backoff_max_s: float = Field(default=8.0, gt=0, le=60.0)
    """Ceiling on the exponential backoff — keeps a multi-retry series from
    ballooning its own wait time."""

    retry_backoff_jitter_s: float = Field(default=1.0, ge=0, le=10.0)
    """Uniform random jitter, 0..this many seconds, added to every backoff
    sleep — so a genuine outage spanning many series doesn't retry all of
    them in lockstep against FRED."""

    breaker_after_failed_series: int = Field(default=1, ge=1, le=9)
    """After this many series have each exhausted their own retries and
    still failed, the breaker trips: every subsequent series in the SAME
    get_macro_summary() call gets a single attempt (no retries), because a
    run that has already lost this many series in a row reads as a genuine
    outage, not a flake — full retries on every remaining series would
    only multiply the stall. A success anywhere resets the counter.
    Default 1 (tighter than the old hardcoded 2) because there are now up
    to fifteen series to get through inside the same shared
    total_fetch_deadline_s budget, not nine."""

    total_fetch_deadline_s: float = Field(default=90.0, ge=10.0, le=300.0)
    """Hard wall-clock ceiling for one get_macro_summary() call, independent
    of the retry/backoff arithmetic above. `MacroDataProvider` clips every
    request's timeout AND every retry's backoff sleep to whatever remains
    of this budget, and skips any series not yet started once it's
    exhausted — so this is a real ceiling on added wall-clock, not merely
    an upper bound implied by retry-count × timeout arithmetic. This is
    what keeps a full FRED outage from stalling the live trading session
    that reads this feed."""

    @model_validator(mode="after")
    def _resilience_bounds_are_well_formed(self):
        if self.retry_backoff_base_s > self.retry_backoff_max_s:
            raise ValueError(
                "macro.retry_backoff_base_s must be <= retry_backoff_max_s; "
                f"got {self.retry_backoff_base_s} > {self.retry_backoff_max_s}"
            )
        if self.total_fetch_deadline_s < self.request_timeout_s:
            raise ValueError(
                "macro.total_fetch_deadline_s must be >= request_timeout_s "
                "— a deadline shorter than one request's own timeout would "
                f"abort every fetch immediately without ever really trying; "
                f"got {self.total_fetch_deadline_s} < {self.request_timeout_s}"
            )
        return self


class EventRiskConfig(BaseModel):
    """Scheduled-event lookups that ground the Risk Manager's mandatory
    `event_risk` check (`src/data/event_calendar.py`).

    Added because that check was previously answered from the model's own
    memory: `MarketDataProvider.get_next_earnings_date` had zero callers, and
    no module fetched a macro release calendar at all. The numbers here are
    ceilings, not tuning knobs — a session must never be delayed, and must
    certainly never hang, because a nice-to-have calendar was slow. The FRED
    retry/backoff policy itself is NOT duplicated here: the calendar hits the
    same host as `src/data/macro.py` with the same failure mode, so
    `src/pipeline.py` threads the existing `macro.*` retry settings into it and
    only the deadline below is calendar-specific.
    """

    horizon_days: int = Field(default=10, ge=1, le=60)
    """How far ahead the macro release calendar looks, in calendar days. 10
    covers "the next few sessions" the `event_risk` field asks about with
    enough margin to see a release the desk should already be positioning
    around, without burying the seat in rows it will skim past."""

    calendar_deadline_s: float = Field(default=20.0, ge=1.0, le=120.0)
    """Hard wall-clock ceiling for one `get_upcoming_events()` call. Much
    tighter than `macro.total_fetch_deadline_s` (90s) on purpose: the macro
    summary is load-bearing for the regime call, this calendar is an
    advisory layered on top of a session that must not wait for it. Enforced
    the same way — every request timeout and every backoff sleep is clipped to
    the remaining budget, and releases not yet started are skipped and reported
    as `fetch_deadline_exceeded` rather than silently omitted."""

    earnings_deadline_s: float = Field(default=20.0, ge=1.0, le=120.0)
    """Hard wall-clock ceiling for the whole per-symbol earnings-date sweep.
    Symbols not reached inside it come back labelled
    `unavailable_deadline_exceeded`, never dropped."""

    earnings_symbol_timeout_s: float = Field(default=8.0, ge=0.5, le=60.0)
    """Per-symbol ceiling on the earnings-date lookup. `yfinance`'s calendar
    call has no timeout of its own — the same hang risk `get_ohlcv` /
    `get_valuation_metrics` are already `ThreadPoolExecutor`-bounded against."""

    fomc_request_timeout_s: float = Field(default=10.0, ge=1.0, le=60.0)
    """Per-request timeout for the Federal Reserve's own FOMC calendar. Its own
    setting rather than a reuse of `macro.request_timeout_s` because this is a
    different host with a different failure mode — federalreserve.gov, not
    FRED. The backoff CURVE is still taken from `macro.*`: that is a generic
    retry policy, not a fact about either host."""

    fomc_max_retries: int = Field(default=2, ge=0, le=5)
    """Retries per Fed calendar URL before that source is given up on."""

    fomc_deadline_s: float = Field(default=15.0, ge=1.0, le=120.0)
    """Hard wall-clock ceiling for one `FOMCCalendarProvider.get_meetings()`
    call, covering BOTH the JSON feed and the fallback page. Same enforcement
    as the macro calendar: every request timeout and every backoff sleep is
    clipped to what remains, and a source not reached inside the budget is
    reported as a named absence rather than silently skipped."""

    fomc_cache_ttl_days: float = Field(default=7.0, ge=0.0, le=90.0)
    """How long a cached FOMC schedule is trusted without a refetch. FOMC dates
    are published a year ahead and change perhaps twice a year, so a weekly
    refresh is generous. Freshness alone is never sufficient: a cache is used
    without fetching only if it ALSO spans `horizon_days`, and an expired cache
    is still served — clearly labelled `measured_from_stale_cache`, with its
    age — when the live sources are unreachable."""

    fomc_cache_path: str = Field(default="data/fomc_calendar.json")
    """Where that cache lives. Relative by design, like the other on-disk
    caches (`data/company_profiles.json`, `data/news`, ...), so the rehearsal
    rig's chdir-based filesystem wall redirects it into the sandbox."""

    @model_validator(mode="after")
    def _deadlines_are_well_formed(self):
        if self.earnings_deadline_s < self.earnings_symbol_timeout_s:
            raise ValueError(
                "event_risk.earnings_deadline_s must be >= "
                "earnings_symbol_timeout_s — a sweep budget shorter than one "
                "symbol's own timeout would abandon every symbol before it "
                f"could answer; got {self.earnings_deadline_s} < "
                f"{self.earnings_symbol_timeout_s}"
            )
        if self.fomc_deadline_s < self.fomc_request_timeout_s:
            raise ValueError(
                "event_risk.fomc_deadline_s must be >= fomc_request_timeout_s "
                "— a deadline shorter than one request's own timeout would "
                "abort every fetch immediately without ever really trying; got "
                f"{self.fomc_deadline_s} < {self.fomc_request_timeout_s}"
            )
        return self


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
    # defaults (3 per seat / 6 total), so older configs keep working
    # unchanged and Phase 9 stays off-by-default-bound rather than
    # unbounded.
    nominations: NominationConfig = Field(default_factory=NominationConfig)
    # Optional section — a settings.yaml without it gets the documented
    # default lookback (7 days), so older configs keep working unchanged.
    reconciliation: ReconciliationConfig = Field(default_factory=ReconciliationConfig)
    # Optional section — a settings.yaml without it gets the tailnet cockpit
    # default (see NotificationsConfig docstring), so older configs keep
    # alerting exactly as before, just with a link added.
    notifications: NotificationsConfig = Field(default_factory=NotificationsConfig)
    # Optional section — a settings.yaml without it gets the pre-existing
    # 50-item prompt cap (see NewsConfig docstring), so older configs keep
    # working unchanged.
    news: NewsConfig = Field(default_factory=NewsConfig)
    # Optional section — a settings.yaml without it gets the documented FRED
    # resilience defaults (see MacroConfig docstring), so older configs keep
    # working unchanged.
    macro: MacroConfig = Field(default_factory=MacroConfig)
    # Optional section — a settings.yaml without it gets the documented
    # event-lookup ceilings (see EventRiskConfig docstring), so older configs
    # keep working unchanged.
    event_risk: EventRiskConfig = Field(default_factory=EventRiskConfig)

    def _fallback_key_for_provider(self) -> str:
        """The API key credential that must be present for `llm.fallback_provider`
        to actually be reachable as a cross-provider failover target. Reuses
        the same provider-name -> api_keys.* mapping pipeline.py's own
        `_key_for` closure uses, so config validation and client construction
        can never disagree about which credential a given fallback provider
        needs."""
        return {
            "openai": self.api_keys.openai,
            "deepseek": self.api_keys.deepseek,
            "openrouter": self.api_keys.openrouter,
            "google": self.api_keys.google,
        }.get(self.llm.fallback_provider, self.api_keys.anthropic)

    def _fallback_reachable_for_any_agent(self) -> bool:
        """True when at least one agent's (provider, model) pair differs from
        the configured fallback pair — i.e. failover could ever actually fire
        for that agent (mirrors `BaseAgent._failover_reachable`'s own
        not-identical-pair rule in src/agents/base.py, minus the key check,
        which the two call sites below apply separately).

        Shared by `_check_llm_provider_keys` and `_check_provider_attempt_
        budget` so they can never independently compute this and drift apart
        — which is exactly what caused the 2026-08-31 outage (see
        `provider_attempt_budget`'s docstring): the config check keyed off
        `api_keys.anthropic` while the runtime gate keyed off the primary
        provider, and the two were never proven to agree.
        """
        fallback_pair = (self.llm.fallback_provider, self.llm.fallback_model)
        return any(
            (
                resolve_provider(
                    getattr(self.llm, f"{agent_name}_model"),
                    self.llm.get_provider(agent_name),
                ),
                getattr(self.llm, f"{agent_name}_model"),
            ) != fallback_pair
            for agent_name in AGENT_NAMES
        )

    @model_validator(mode="after")
    def _check_llm_provider_keys(self):
        openai_models = []
        anthropic_models = []
        deepseek_models = []
        openrouter_models = []
        google_models = []

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
            elif provider == "google":
                google_models.append(label)
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

        if google_models and not self.api_keys.google:
            selected = ", ".join(google_models)
            raise ValueError(
                f"GOOGLE_API_KEY is required for selected Google models: {selected}"
            )

        if anthropic_models and not self.api_keys.anthropic:
            selected = ", ".join(anthropic_models)
            raise ValueError(
                f"ANTHROPIC_API_KEY is required for selected Anthropic models: {selected}"
            )

        # The failover credential cannot be silently missing when failover is
        # actually reachable — otherwise it is discovered only when the
        # primary fails and the failover attempt itself gets a 401. That is
        # the second half of the 2026-08-31 incident: no agent used Anthropic
        # as a primary, so the missing ANTHROPIC_API_KEY sat unnoticed until
        # a retry-exhausted call fell through to failover and hit
        # `401 credential_not_found` — after the attempt-budget arithmetic
        # above had ALREADY been fixed, so the failover fired for the first
        # time and immediately hit the second, independent gap.
        if self._fallback_reachable_for_any_agent() and not self._fallback_key_for_provider():
            raise ValueError(
                f"An API key for llm.fallback_provider={self.llm.fallback_provider!r} "
                f"is required: llm.fallback_model={self.llm.fallback_model!r} is "
                "reachable as the cross-provider failover target for at least one "
                "agent, but its credential is not configured. A silently-missing "
                "fallback key is precisely how the 2026-08-31 outage's second half "
                "happened — do not let this ship unnoticed again."
            )

        return self

    @model_validator(mode="after")
    def _check_provider_attempt_budget(self):
        """Refuse to start if the circuit would trip on the retry loop itself.

        The cost circuit stops a logical call once it exceeds
        `llm_cost_circuit.max_provider_attempts_per_call` provider attempts.
        `BaseAgent.run()` decides how many attempts actually happen. When the
        ceiling is below what the loop can spend, the circuit fires on the
        loop's normal, designed behaviour rather than on anything unsafe — and
        because that stop is scoped to the session, a routine upstream
        rate-limit costs the desk a trading session for pennies of spend.

        That is not hypothetical: it is the 2026-08-31 09:32 ET incident
        recorded on `provider_attempt_budget`, where a hand-pinned 2 sat
        against a worst case of 3 and made cross-provider failover impossible
        to ever complete.

        The two numbers live in different worlds — one an env-overridable
        module constant, the other a YAML setting — which is exactly how they
        drifted apart unnoticed for six days across five separate trips. So
        the agreement is enforced here, at load, rather than trusted to
        whoever edits either one next. Failing to boot is the loud failure;
        going dark two minutes after the opening bell is the quiet one.

        `failover_available` is derived from the SAME not-identical-pair rule
        `BaseAgent._failover_reachable` uses at runtime (via
        `_fallback_reachable_for_any_agent`/`_fallback_key_for_provider`
        above) rather than independently keying off `api_keys.anthropic` —
        that independent keying is exactly what let this check and the
        runtime gate disagree in the first place.
        """
        failover_available = (
            bool(self._fallback_key_for_provider())
            and self._fallback_reachable_for_any_agent()
        )
        required = provider_attempt_budget(failover_available=failover_available)
        configured = int(self.llm_cost_circuit.max_provider_attempts_per_call)
        if configured < required:
            raise ValueError(
                "llm_cost_circuit.max_provider_attempts_per_call is "
                f"{configured}, below the {required} provider attempts one "
                "agent call can make ("
                f"{required - (1 if failover_available else 0)} primary "
                + (
                    "attempts plus one cross-provider failover"
                    if failover_available
                    else "attempts, no failover configured"
                )
                + "). The circuit would stop the session on the retry loop's "
                "own designed behaviour — the failure this check exists to "
                "prevent. Raise it to at least "
                f"{required}, or remove it from settings.yaml to let it derive."
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
