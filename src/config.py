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
    bargo: str = ""

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


class RiskConfig(BaseModel):
    max_position_pct: float = Field(gt=0, le=100)
    max_total_position_pct: float = Field(gt=0)
    max_daily_loss_pct: float = Field(gt=0, le=100)
    max_sector_pct: float = Field(gt=0, le=100)
    require_stop_loss: bool
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
    base_url: str = "https://www.bargo.ai/free-apis/congress/v1"
    timeout_s: float = Field(default=10.0, ge=1, le=60)
    max_rows_per_symbol: int = Field(default=20, ge=1, le=100)


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
    max_paid_sessions_per_mode_per_day: int = Field(default=2, ge=1)
    # Includes the initial request.  Two means one transient retry at most;
    # a provider failover would be attempt three and is blocked/latches.
    max_provider_attempts_per_call: int = Field(default=2, ge=1)
    # Aggregate retries across parallel specialist calls in one run.
    max_retry_attempts_per_session: int = Field(default=2, ge=0)
    reservation_ttl_minutes: int = Field(default=30, ge=5, le=180)
    reservation_multiplier: float = Field(
        default=1.05, ge=1.0, le=2.0, allow_inf_nan=False,
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


class AppConfig(BaseModel):
    api_keys: ApiKeysConfig
    alpaca: AlpacaConfig
    llm: LLMConfig
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
