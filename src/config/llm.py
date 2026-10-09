"""Model routing settings: which model and provider each seat uses.

Moved verbatim from src/config/__init__.py (pure move; bodies AST-identical).
"""

from pydantic import BaseModel, field_validator, model_validator
from src.agents.base import (
    VALID_PROVIDERS,
    resolve_provider,
)


# The nine agents that carry a per-agent model (and, as of Stage 1, an
# optional explicit provider). Single list reused by LLMConfig.get_provider
# and AppConfig._check_llm_provider_keys so the two can't drift apart.
AGENT_NAMES = (
    "tech_analyst",
    "news_analyst",
    "macro_analyst",
    "earnings_analyst",
    "smart_money_analyst",
    "portfolio_manager",
    "risk_manager",
    "position_reviewer",
    "evening_analyst",
    "meta_reflector",
)


# OpenRouter's documented reasoning.effort values — see
# https://openrouter.ai/docs/use-cases/reasoning-tokens.
_VALID_REASONING_EFFORTS = frozenset({"max", "xhigh", "high", "medium", "low", "minimal", "none"})


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
    # === Route 3: the TERTIARY, a genuinely DIFFERENT model ================
    # The fallback above changes the ROAD but keeps the MODEL, which is route
    # diversity only: when the model itself is saturated both routes fail
    # together (2026-09-22). This third rung is a different model on a third
    # provider, reached ONLY after both of the above have failed.
    #
    # `o4-mini` rather than `claude-haiku-4-5` — the two are within 10% on
    # price and both are credentialed, and the deciding factor is the cost
    # circuit: Anthropic documents a 529 `overloaded_error` that
    # src/cost_circuit.py cannot prove cost $0, so an Anthropic tertiary's
    # most likely failure would hard-latch the desk. See
    # src/agents/base.py's _DEFAULT_TERTIARY_PROVIDER for the full note.
    #
    # Setting `tertiary_model` to "" disables route 3 outright, and the
    # attempt-budget check below then stops requiring the extra attempt.
    tertiary_provider: str = "openai"
    tertiary_model: str = "o4-mini"
    # === ROUTE 3 SECOND-ROAD SUBSTITUTE (2026-09-30) =====================
    # Used INSTEAD of the tertiary above, per seat, when that seat's routes
    # 1 and 2 have collapsed onto a single provider and route 3 would land
    # on it too — measured on 2026-09-29, when all three of
    # portfolio_manager's routes were OpenRouter and one exhausted balance
    # (HTTP 402) killed every intraday decision run while Google direct was
    # answering at $0.00 in the same process. Never adds a rung and never
    # touches a seat whose ladder already spans two roads (the eight
    # Google-primary specialists keep the OpenRouter tertiary unchanged).
    # The full rule, the measurement and the accepted residual are on
    # src/agents/base.py's `_DEFAULT_TERTIARY_ALT_PROVIDER`; the selection
    # itself is `select_tertiary_route` in that same file.
    # Set `tertiary_alt_model` to "" to switch the substitution off.
    tertiary_alt_provider: str = "google"
    tertiary_alt_model: str = "gemini-3.5-flash-lite"
    # Global output-ceiling fallback — used by any agent without an explicit
    # override below.
    max_tokens: int
    # ONE explicit reasoning-effort setting for EVERY seat calling through
    # OpenRouter, so every model is measured (and later traded) under
    # identical, declared thinking budget rather than each provider's own
    # undeclared default. "medium" is OpenRouter's documented default when
    # `reasoning.enabled=true` is set without an explicit effort — see
    # https://openrouter.ai/docs/use-cases/reasoning-tokens. No per-model
    # overrides: the whole point is one comparable setting across seats.
    reasoning_effort: str = "medium"
    # Ask OpenRouter to constrain the response to this agent's pydantic
    # result schema via `response_format` (json_schema), instead of relying
    # on prose-JSON prompting alone. See
    # https://openrouter.ai/docs/features/structured-outputs. Applies only
    # to agents that have a known result model (BaseAgent.result_model) and
    # only on the OpenRouter wire path.
    structured_output: bool = True
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
            raise ValueError(f"llm.max_tokens must be >= 512 for agent outputs; got {v}")
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
            raise ValueError(f"per-agent max_tokens override must be >= 512 (or null to inherit global); got {v}")
        return v

    @field_validator("reasoning_effort")
    @classmethod
    def _reasoning_effort_is_valid(cls, v: str) -> str:
        if v not in _VALID_REASONING_EFFORTS:
            raise ValueError(f"llm.reasoning_effort must be one of {sorted(_VALID_REASONING_EFFORTS)}; got {v!r}")
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
        "tech_analyst_provider",
        "news_analyst_provider",
        "macro_analyst_provider",
        "earnings_analyst_provider",
        "portfolio_manager_provider",
        "risk_manager_provider",
        "smart_money_analyst_provider",
        "position_reviewer_provider",
        "evening_analyst_provider",
        "meta_reflector_provider",
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
            raise ValueError(f"Invalid provider {v!r}; must be one of {sorted(VALID_PROVIDERS)} or unset")
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
            raise ValueError(f"Invalid llm.fallback_provider {v!r}; must be one of {sorted(VALID_PROVIDERS)}")
        return normalized

    @field_validator("fallback_model")
    @classmethod
    def _fallback_model_is_nonempty(cls, v: str) -> str:
        if not isinstance(v, str) or not v.strip():
            raise ValueError("llm.fallback_model must be a non-empty model id")
        return v.strip()

    @field_validator("tertiary_provider")
    @classmethod
    def _tertiary_provider_is_valid(cls, v: str) -> str:
        # Same no-escape-hatch rule as `fallback_provider`: route 3 names its
        # provider directly, so a typo must fail at load rather than resolve
        # to whatever `tertiary_model`'s prefix implies and quietly misroute
        # the desk's last line of defence.
        normalized = (v or "").strip().lower()
        if normalized not in VALID_PROVIDERS:
            raise ValueError(f"Invalid llm.tertiary_provider {v!r}; must be one of {sorted(VALID_PROVIDERS)}")
        return normalized

    @field_validator("tertiary_model")
    @classmethod
    def _tertiary_model_is_str(cls, v: str) -> str:
        # Empty IS legal here, unlike `fallback_model`: it is how an operator
        # turns route 3 off, and `AppConfig.tertiary_available` reads it as
        # such so the attempt-budget floor drops back to 3 in step.
        if not isinstance(v, str):
            raise ValueError('llm.tertiary_model must be a string model id or ""')
        return v.strip()

    @field_validator("tertiary_alt_provider")
    @classmethod
    def _tertiary_alt_provider_is_valid(cls, v: str) -> str:
        # Same no-escape-hatch rule as `tertiary_provider` above. This field
        # exists precisely to name a DIFFERENT road, so letting a typo fall
        # through to prefix inference could silently put the substitute back
        # on the road it was added to escape.
        normalized = (v or "").strip().lower()
        if normalized not in VALID_PROVIDERS:
            raise ValueError(f"Invalid llm.tertiary_alt_provider {v!r}; must be one of {sorted(VALID_PROVIDERS)}")
        return normalized

    @field_validator("tertiary_alt_model")
    @classmethod
    def _tertiary_alt_model_is_str(cls, v: str) -> str:
        # Empty turns the substitution off and restores the pre-2026-09-30
        # behaviour exactly; it never turns route 3 itself off.
        if not isinstance(v, str):
            raise ValueError('llm.tertiary_alt_model must be a string model id or ""')
        return v.strip()

    @field_validator(
        "tech_analyst_provider_order",
        "news_analyst_provider_order",
        "macro_analyst_provider_order",
        "earnings_analyst_provider_order",
        "smart_money_analyst_provider_order",
        "portfolio_manager_provider_order",
        "risk_manager_provider_order",
        "position_reviewer_provider_order",
        "evening_analyst_provider_order",
        "meta_reflector_provider_order",
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
                "provider_order must be null (no preference) or a non-empty list of OpenRouter endpoint slugs; got []"
            )
        cleaned: list[str] = []
        for entry in v:
            if not isinstance(entry, str) or not entry.strip():
                raise ValueError(f"provider_order entries must be non-empty strings; got {entry!r}")
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
