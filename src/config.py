import os
import re
from pathlib import Path
from typing import ClassVar

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

from src.agents.base import (
    VALID_PROVIDERS,
    provider_attempt_budget,
    resolve_provider,
)
from src.trading_calendar import SESSION_WINDOWS
from src.risk.constants import STARTER_POSITION_RISK_PCT


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

# The deliberate, reviewed code-level authorization for live capital. Flipping
# this is one of the TWO things that must happen for the desk to leave paper;
# the other is the live-capital pre-flight gate (board item 150,
# `src/live_capital_preflight.py`) passing every condition in its ACTIVATION
# scope. Neither alone is enough, on purpose:
#
#   * a settings.yaml edit alone still fails — this constant is False;
#   * flipping this constant alone still fails — the gate blocks and the error
#     names every unmet condition;
#   * a signed attestation file alone still fails — this constant is False.
#
# Before this existed the guard simply raised, which was safe but silent about
# WHY; the checklist lived in prose and nothing checked it. Do not flip this
# without the gate reporting PASS, and never as a drive-by edit.
LIVE_TRADING_AUTHORIZED = False


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
        live trading is ever authorized, it takes BOTH a reviewed code change
        flipping `LIVE_TRADING_AUTHORIZED` above AND the live-capital pre-flight
        gate (board item 150) reporting every activation-scope condition
        satisfied. This is the point where the paper lock would be lifted, so
        this is where the gate sits: there is no code path to a live account
        that does not run it, and a refusal names the conditions that failed.
        """
        if self.paper is not True:
            if not LIVE_TRADING_AUTHORIZED:
                raise ValueError(
                    "alpaca.paper must be true — live trading is not authorized "
                    "(see the hard boundaries in CLAUDE.md / docs/STATE.md). "
                    "Enabling live trading requires BOTH a reviewed change to "
                    "config.LIVE_TRADING_AUTHORIZED and a passing live-capital "
                    "pre-flight gate (src/live_capital_preflight.py), not a "
                    "settings.yaml edit."
                )
            # Authorized in code — the gate still has the last word, and names
            # which condition failed rather than refusing anonymously.
            from src.live_capital_preflight import (  # local: avoids import cycle
                LiveCapitalBlocked,
                assert_live_capital_authorized,
            )

            try:
                assert_live_capital_authorized()
            except LiveCapitalBlocked as exc:
                raise ValueError(
                    f"alpaca.paper is false and live trading is code-authorized, "
                    f"but the live-capital pre-flight gate refuses: {exc}"
                ) from exc
            return self
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


# OpenRouter's documented reasoning.effort values — see
# https://openrouter.ai/docs/use-cases/reasoning-tokens.
_VALID_REASONING_EFFORTS = frozenset(
    {"max", "xhigh", "high", "medium", "low", "minimal", "none"}
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

    @field_validator("reasoning_effort")
    @classmethod
    def _reasoning_effort_is_valid(cls, v: str) -> str:
        if v not in _VALID_REASONING_EFFORTS:
            raise ValueError(
                f"llm.reasoning_effort must be one of "
                f"{sorted(_VALID_REASONING_EFFORTS)}; got {v!r}"
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

    @field_validator("tertiary_provider")
    @classmethod
    def _tertiary_provider_is_valid(cls, v: str) -> str:
        # Same no-escape-hatch rule as `fallback_provider`: route 3 names its
        # provider directly, so a typo must fail at load rather than resolve
        # to whatever `tertiary_model`'s prefix implies and quietly misroute
        # the desk's last line of defence.
        normalized = (v or "").strip().lower()
        if normalized not in VALID_PROVIDERS:
            raise ValueError(
                f"Invalid llm.tertiary_provider {v!r}; must be one of "
                f"{sorted(VALID_PROVIDERS)}"
            )
        return normalized

    @field_validator("tertiary_model")
    @classmethod
    def _tertiary_model_is_str(cls, v: str) -> str:
        # Empty IS legal here, unlike `fallback_model`: it is how an operator
        # turns route 3 off, and `AppConfig.tertiary_available` reads it as
        # such so the attempt-budget floor drops back to 3 in step.
        if not isinstance(v, str):
            raise ValueError("llm.tertiary_model must be a string model id or \"\"")
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
    """Max basis points of adverse excursion from the verified reference
    price an entry limit may sit. A BUY limit is capped this far above the
    reference; a SHORT limit is floored this far below it — the same bound,
    opposite side (fillability parity, not a second risk budget). When the
    displayed quote is already beyond this, the entry is skipped with
    reason `slippage_gated` rather than submitted as an unfillable order."""

    repeg_enabled: bool = False
    """Master switch for the single-shot entry reprice. OFF by default so
    the feature can be deployed dark: with it off, `_repeg_entry_order`
    returns the original order id untouched and not a single broker call is
    made. The owner owns this switch."""

    # `repeg_max_attempts` was DELETED 2026-09-12 (rejected loudly below if
    # still present in settings.yaml). The reprice is now exactly ONE
    # replace, by design, not by a cap set to 1: Alpaca's own community
    # practice for a fast market is a single deliberately aggressive replace
    # that crosses the market, not a ladder of nudges — and every extra
    # replace is another `pending_replace` window an order can get stuck in.
    # A knob whose only legal value is 1 would invite someone to turn it up.

    rotation_enabled: bool = False
    """Master switch for AUTOMATIC opportunity-cost rotation (Phase 14b,
    `src/rotation.py`). OFF by default so it deploys dark, exactly like
    `repeg_enabled`: with it off, the rotation comparison is still computed
    and shown to the Portfolio Manager as information (the Phase 14
    behaviour, unchanged byte for byte), and the desk never closes a
    position on its own. With it on, ONE categorically-ineligible held
    position per morning session — one that fails the desk's own entry
    rules today AND whose structural protection has already broken — is
    closed through the ordinary PM-target → constructor → Risk Manager →
    execution path, to free room for the best-ranked new candidate the PM
    itself asked to buy. Every rotation fires a standalone owner alert."""

    rotation_ranked_margin_enabled: bool = False
    """SECOND switch, board item 39. Extends automatic rotation from the
    CATEGORICAL tier to the RANKED-MARGIN tier (`src/rotation.py`: both
    sides still pass the desk's own entry gates, and the new candidate
    cleared the provisional 25% margin on the like-for-like sub-score).
    OFF by default and required IN ADDITION to `rotation_enabled`.

    Turning it on is NOT sufficient to put a ranked-margin sale on the
    wire. `rotation_sell_reason` refuses to build the sale's reason at all
    unless it is handed a `RotationClearance` — an object only
    `src/pipeline_stages.py::_rotation_buy_leg_projected_refusal` can mint,
    and only after the replacement BUY has been run through the downstream
    refusal gates against PROJECTED POST-SALE state. That guard is
    structural and unconditional: no value of this flag, and no config at
    all, can substitute for the clearance."""

    repeg_poll_seconds: float = Field(default=5.0, gt=0, le=30)
    """How long to let the working order rest before the one reprice, and —
    only if the exchange has not yet acknowledged the order by then — how
    much longer to wait for that acknowledgement before giving up on the
    reprice (a replace against an unacknowledged order is rejected by
    Alpaca). Total added latency per entry is therefore at most
    `2 * repeg_poll_seconds` plus one replace round-trip, and lands BEFORE
    `place_entry_protection`'s own fill wait, which is where an entry still
    unfilled at the end of its session is cancelled."""

    @model_validator(mode="before")
    @classmethod
    def _reject_deleted_repeg_keys(cls, data):
        # Same pattern as `RiskConfig._reject_removed_short_cap_keys`:
        # BaseModel's default `extra="ignore"` would let a settings.yaml still
        # carrying the deleted key load silently, and an operator would
        # believe a ladder length they set was in force. Fail loudly.
        if isinstance(data, dict) and "repeg_max_attempts" in data:
            raise ValueError(
                "execution.repeg_max_attempts was removed 2026-09-12: the "
                "entry reprice is a single replace by design (see "
                "ExecutionConfig). Delete the key from the settings file."
            )
        return data

    # Spec §11.1 (owner-ratified 2026-09-01), reversing the 2026-08-27
    # decision to keep fractional off.
    # The flag is still shipped FALSE — the owner owns the switch and flips
    # it himself — but the reason has changed. It is no longer "a fractional
    # position cannot be protected". HYBRID STOP COVERAGE protects one: a GTC
    # stop over the whole shares plus a DAY stop over the sub-share remainder,
    # re-placed at the start of every session. See config/settings.yaml for
    # the measured broker capability and the accepted overnight trade-off.
    fractional_enabled: bool = True
    """Master switch for exact (fractional) entry sizing. ON by default —
    whole-share rounding is a silent, constant tax on every position the
    desk opens (V wanted 6% of the book and got 3.84%), and the reasoning
    that kept it off no longer holds: the protective stop has been a
    SEPARATE post-fill order since the 2026-07-16 OTO/DAY-tif fix, so the
    fill→stop window this was meant to avoid already exists on every entry.

    Turning this OFF restores whole-share flooring everywhere without a code
    change. A symbol is still only sized fractionally when the broker
    confirms `fractionable` for it (`get_fractionability`, which fails
    CLOSED), so this flag widens nothing on its own.

    The §11.1 open question — whether Alpaca will carry a stop for a
    fractional quantity — was settled empirically on 2026-09-01: not as a
    GTC order, but YES as a DAY order. Hence the hybrid: floor(qty) on a
    durable GTC stop, the sub-share remainder on a DAY stop that lapses at
    the close and is re-placed at the next open."""

    fractional_share_decimals: int = Field(default=4, ge=1, le=9)
    """Decimal places an exact share count is FLOORED to (never rounded up —
    rounding up would spend more risk budget than the sizing math allowed).
    4dp is under a tenth of a cent of notional on any price this desk
    trades, so the residual rounding tax is immaterial while the number
    stays short enough to read in a log line and in a Telegram alert."""

    fill_stream_enabled: bool = False
    """Master switch for the live `trade_updates` websocket fill feed
    (`_TradeUpdatesHub` in `src/execution/broker.py`).

    ON in `config/settings.yaml` since 2026-09-18. The field default stays
    FALSE deliberately: a config that does not mention the socket, and
    every test double that constructs this model bare, must not open one.
    `AlpacaBroker._fill_stream_enabled` defaults false for the same reason.

    The socket pushes a fill the instant it happens; with it off the desk
    only learns of a fill on its next REST poll, which
    on the bounded polling path can be up to half an hour later. The REST
    path is untouched and remains the fallback for any wait the socket
    cannot serve.

    It was OFF from 2026-09-17 because the socket had never once
    authenticated — 1,017 `failed to authenticate` occurrences across the
    retained production logs and zero successes. The cause was not the
    connection's timing (six pull requests adjusted that; none of them
    could have worked). The process held a 29-character PLACEHOLDER
    credential containing the literal word `placeholder`, and neither of
    the two mechanisms that could have substituted a real one applies to
    this socket:

      1. Alpaca authenticates the stream with an in-band websocket
         MESSAGE, not a handshake header, while the local gateway that
         substitutes the real key rewrites HTTP HEADERS — so it cannot
         reach the credential at all.
      2. The installed `alpaca-py` stream is built on `websockets.legacy`,
         which has no proxy support (that arrived in websockets 15.0, and
         only in the asyncio and sync clients), so the socket never
         traversed the gateway either.

    Real credential files were delivered 2026-09-18 and the process now
    reads them directly, so that single blocker is gone and this flag was
    the only remaining thing holding the socket shut. This flip changes no
    timeout, poll interval, retry count or ceiling, and nothing about
    credential handling.

    A refusal is now diagnosable rather than silent: the SDK throws the
    rejection payload away and raises a bare `ValueError`, which the desk
    used to log as `status=unknown` — indistinguishable from a transport
    fault, and the reason this took a fortnight to identify.
    `_install_trading_stream_auth_diagnostics` preserves Alpaca's own
    `message`/`status` and logs
    `trade_updates authentication REJECTED by broker`, with the key's
    length and first two characters only — never the value, never the
    secret. A successful handshake logs
    `trade_updates websocket authenticated`.

    CEILINGS ADDED 2026-09-18 (after the storm audit). Enabling this flag
    no longer risks an unbounded reconnect loop. The installed alpaca-py
    retries a failed handshake every 10ms with no backoff and no limit of
    its own — that is what produced 32,896 attempts and 32,666 HTTP 429
    rejections on 2026-09-15 — so `src/execution/broker.py` now enforces
    both a per-session and a per-day attempt ceiling of its own, treats a
    429 as a rate-limit stand-down rather than a transport retry, and when
    a ceiling is reached stops the socket for the day, tells the owner once
    in plain words, and leaves fills to the bounded REST path. See the
    `_STREAM_ATTEMPT_CEILING_*` constants there for each number's source."""


class RiskConfig(BaseModel):
    max_position_pct: float = Field(gt=0, le=100)
    max_total_position_pct: float = Field(gt=0)
    max_sector_pct: float = Field(gt=0, le=100)
    # Spec §10.3 (owner-ratified 2026-09-01). `max_sector_pct` above is no
    # longer a veto — it is the diversification TARGET, past which further
    # trades in that sector are progressively SHRUNK rather than refused
    # (`src/risk/rules.py::sector_size_scale`). This is the absolute ceiling
    # the shrinking runs into, past which the answer is still no. Without it
    # a sector could grow without limit through ever-smaller additions.
    #
    # Default is 1.5x the target, capped at `SECTOR_HARD_CEILING_MAX` (90,
    # spec §12.3), deriving from `max_sector_pct` rather than hard-coding a
    # number so that an operator who tightens or loosens the target moves the
    # ceiling with it instead of silently leaving the two inconsistent. The
    # cap exists because 1.5x an already-permissive target stops being a
    # ceiling: at the §12.3 target of 75 it would give 112.5.
    max_sector_hard_pct: float | None = Field(default=None, gt=0, le=100)
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
    min_position_risk_pct: float = Field(
        default=STARTER_POSITION_RISK_PCT, ge=0, le=100,
    )
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
    #
    # 3.0 -> 1.5 -> 2.5 (2026-09-10). The 1.5 came from this desk's own
    # ~2-week MAE sample — later found to overlap the window whose seat
    # outputs were misreporting confidence/data quality, so no longer trusted
    # as the sole basis. 2.5 comes from published swing-trading doctrine
    # instead (2.5-3.0x ATR for a fixed entry stop on a multi-day hold),
    # independent of this desk's own data. Only applies with no real level
    # backing the stop — a level-backed stop is judged on its own honest
    # distance regardless of this number. Full derivation and caveats:
    # `config/settings.yaml` (this key) and docs/INCIDENT_HISTORY.md
    # 2026-09-10. Keep the three in sync.
    min_stop_atr_multiple: float = Field(default=2.5, gt=0, le=10)
    # NO `min_reward_risk_after_widening` HERE ANY MORE — removed 2026-09-24
    # (board item 81). It refused nothing and capped nothing: no code in
    # `PortfolioConstructor` ever read `self.min_reward_risk_after_widening`,
    # and the one place the value was threaded to
    # (`PortfolioManagerAgent._apply_subfloor_catalyst_rule`) explicitly
    # discards it. Removed keys are rejected loudly by
    # `_reject_deleted_reward_risk_floor_key` below.
    # --- Level-backed stops (spec §12.1, 2026-09-01) ---------------------
    # `min_stop_atr_multiple` above used to OVERWRITE the structural stop
    # whenever the level sat closer than the band, after which the stop was
    # at nothing real. On 2026-09-01 the desk reviewed 38 qualified
    # signals and placed zero trades. A stop that sits at a level
    # `src/data/levels.py::find_structural_levels` actually computed is now
    # honoured whatever its ATR distance; the band only applies when nothing
    # computed backs it.
    #
    # NO `level_match_atr_tolerance` HERE ANY MORE — removed 2026-09-13,
    # docs/WORK.md item 46. It was 0.25 ATR and justified itself as being "at
    # least as wide" as the 1% zone `find_structural_levels` clusters pivots
    # into. Those are different units, so the claim was only ever true above
    # a particular volatility: 0.25 x ATR >= 0.01 x price needs ATR >= 4% of
    # price. At the 2.56%-of-price median ATR this repo's own comments quote,
    # 0.25 ATR is 0.64% — 1.56x NARROWER than the zone it claimed to cover,
    # so a stop sitting inside a level's real zone was not counted as sitting
    # at that level. There is no honest ATR multiple to replace it with: the
    # zone is defined as a percentage of price, the tolerance was a multiple
    # of ATR, and the ratio between them changes with every name on every
    # day. It is not replaced by a different constant — it is deleted, and
    # "is this stop AT this level" now reads the zone's own bound from
    # `src.data.levels.level_zone_halfwidth`, which derives it from the same
    # `CLUSTER_TOLERANCE_PCT` that built the zone. The two can no longer
    # disagree because there is only one of them.
    #
    # The ATR argument the old comment made is not lost, it was misplaced:
    # "is this stop far enough out to survive the name's noise" IS an ATR
    # question, and it is already asked, deterministically, by
    # `min_stop_atr_multiple` and `absolute_min_stop_atr_multiple` below.
    # "Which level is this stop sitting on" is an identity question about a
    # zone, and is answered in the zone's own unit.
    # The deterministic backstop under the exemption above. §12.1's safety
    # argument rests on the 1*ATR hard floor in
    # `config/prompts/tech_analyst.md` — but that is a PROMPT, and Invariant
    # 2 requires deterministic Python protections to be the final authority
    # and to fail closed. A real support level 0.2 ATR under entry is genuine
    # structure AND a guaranteed whipsaw. So a level-backed stop is honoured
    # however tight down to this many ATRs; inside it the stop is pushed out
    # to exactly this floor — never to the full `min_stop_atr_multiple` band.
    absolute_min_stop_atr_multiple: float = Field(default=1.0, ge=0, le=10)
    # How many prior touches a computed level needs before a stop sitting on
    # it is trusted enough to be honoured however tight (Phase 12.1,
    # 2026-09-03 — docs/RESEARCH_FINDINGS.md §7). `find_structural_levels`
    # already requires 2 touches to register a level at all (`MIN_TOUCHES`),
    # but §12.1's own text names that as a SEPARATE, undecided question: "a
    # level currently qualifies on two touches ever ... so two old swing
    # points can justify a very tight stop." The measured table (101
    # symbols, 5 years, daily bars, real vs shuffled arithmetic control)
    # only clears real-vs-shuffled separation cleanly at 5+ touches: real
    # 0.644 [0.590, 0.696] against shuffled 0.505 [0.470, 0.539] — the
    # confidence intervals do not overlap at all. Every lower bucket's
    # intervals overlap or nearly touch (2 touches: real floor 0.510 versus
    # shuffled ceiling 0.510; 3 and 4 touches overlap outright), so a bar
    # below 5 would be honouring a tight stop on a separation that could be
    # noise. Below this bar the stop is NOT treated as level-backed and
    # falls back to `min_stop_atr_multiple` / `absolute_min_stop_atr_multiple`
    # exactly as an unbacked stop does — it does not become untradeable, it
    # loses only the tight-stop exemption.
    min_level_touches_for_stop_honor: int = Field(default=5, ge=1, le=20)
    # --- Target derivation (2026-09-01) ---------------------------------
    # The floor above was dividing a stop computed from measured volatility
    # by a target a language model guessed. On 2026-09-01's morning run that
    # rejected 30 of 38 actionable signals (79%) before any judgement was
    # applied, the two highest-conviction calls among them. The floor is not
    # the defect; its numerator was. These tune the deterministic target
    # derivation that replaced it — see the target-derivation section of
    # src/data/levels.py for the rule and the arithmetic.
    #
    # A target inside this many ATRs of entry is not a destination.
    min_target_atr_multiple: float = Field(default=1.0, gt=0, le=5)
    # Measured move claimed when no structural level stands in the way, in
    # sqrt(session)-scaled ATRs. 1.0 = the typical excursion over the stated
    # horizon. NOTE the interaction with `min_stop_atr_multiple`: a stop at
    # k ATRs and a target at p*ATR*sqrt(H) clear a floor f only when
    # sqrt(H) >= f*k/p — at k=3.0, p=1.0, f=1.5 that is H >= ~21 sessions.
    breakout_projection_atr_multiple: float = Field(default=1.0, gt=0, le=5)
    # How far price can plausibly travel within the horizon, same units.
    # Looser than the projection on purpose: this asks "could it get there",
    # the projection asks "how far do I claim it goes".
    max_target_reach_atr_multiple: float = Field(default=1.5, gt=0, le=5)
    # NO `max_stop_width_reach_atr_multiple` HERE ANY MORE -- the stop-width
    # REFUSAL it threshold-ed was deleted 2026-09-26 (board item 56, route
    # (c)). It was split off from `max_target_reach_atr_multiple` on
    # 2026-09-13 so that estimating a target and refusing a trade stopped
    # sharing one number; the split made them independent without making
    # either derived, and no published work fixes the touch probability
    # below which a stop stops being a stop. Measured before deletion: 648
    # sized stops recorded a touch-probability reading in production
    # (quant_agent.log, 2026-09-13..2026-09-26) and the refusal fired zero
    # times; the widest stop ever seen was 1.29 x ATR x sqrt(H) against a
    # 1.5 cap. A wide stop is answered by a smaller position
    # (`_plan_risk_targets`, the ratified spec 2.1 invariant) and, at the
    # extreme, by `position_sized_to_zero`. Removed keys are rejected loudly
    # by `_reject_deleted_stop_width_gate_key` below. The target-side
    # `max_target_reach_atr_multiple` is UNAFFECTED and still in force.
    # Ceiling on `expected_horizon_sessions` before it enters the sqrt()
    # travel estimate, so an implausible horizon cannot licence a target far
    # outside anything the symbol does.
    max_target_horizon_sessions: int = Field(default=60, ge=1, le=500)
    # Absolute gap between the computed target and the analyst's guess above
    # which the disagreement is logged at WARNING. The guess is kept as
    # evidence, never as arithmetic.
    target_divergence_warn_pct: float = Field(default=25.0, gt=0, le=200)
    # Cash-only default. When False: no BUY may drive `cash` below zero, and
    # any session that starts with `cash < 0` must de-lever (SELL) before any
    # new BUY. When True: normal margin account behavior, risk engine only
    # enforces the exposure / sector / loss caps. Default False is the
    # conservative choice — margin leverage amplifies drawdowns and is not
    # the bot's intended mode unless explicitly opted in.
    allow_margin: bool = False
    # --- Margin interest tracker (spec §11.2, 2026-09-01) ----------------
    # MEASURES, does not gate — this field feeds an estimate/alert only,
    # never a risk check. Alpaca's live non-elite margin rate (elite is
    # 4.75%); a config value rather than a code constant so the desk can
    # correct it without a deploy if Alpaca's rate moves. Interest accrues
    # ONLY on the END-OF-DAY (overnight) debit balance — intraday leverage
    # is free — per `(overnight debit balance x rate) / 360`. See
    # src/margin_interest.py: whether PAPER trading actually charges
    # this is UNCONFIRMED (Alpaca's own comparison lists short-borrow fees
    # as "Coming Soon" and is silent on margin interest either way), so
    # every figure this produces is a labelled ESTIMATE until the broker's
    # own `INT` account activity settles it empirically.
    margin_interest_rate_pct: float = Field(default=6.25, ge=0, le=100)
    # --- Spec §11.2: the gross-exposure ceiling (owner-ratified 2026-09-01)
    #
    # Gross exposure = long market value + ABSOLUTE short market value,
    # measured against equity. Before this setting existed the codebase had
    # NO gross-exposure ceiling of any kind: `max_portfolio_risk_pct` bounds
    # AT-RISK capital (the sum of stop distances), not exposure. Nothing stopped the book reaching the
    # broker's full 4x. Adding this is a TIGHTENING, not a loosening.
    #
    # 2.0x is the owner's deliberate paper-account learning setting, taken
    # against the recommendation to defer — see the §11.2 spec entry and
    # [[qamc-live-capital-checklist]]. Re-derive it before real money.
    #
    # This is the STANDING cap, day AND night. There is deliberately no
    # separate, lower overnight ceiling: an intraday-only allowance would
    # force a trim into every close, selling on a clock rather than on merit,
    # and this desk holds for days so it would almost never use one. The
    # overnight cushion comes from the de-levering ladder
    # (`src/risk/rules.py::resolve_gross_ceiling`) instead.
    #
    # The ladder can only ever tighten this number, never raise it — so
    # lowering this setting lowers every rung with it.
    max_gross_exposure_x: float = Field(default=2.0, gt=0, le=4.0)
    # Broker maintenance-margin requirement, as a percent of gross exposure,
    # used ONLY to report distance-to-forced-liquidation
    # (`src/risk/rules.py::distance_to_forced_liquidation_pct`). It computes
    # nothing the engine enforces; it answers "how far could the book fall
    # before the broker sells without asking", which nothing watched before
    # §11.2. 25% is Alpaca's standard equity maintenance requirement and
    # reproduces the spec's two published figures exactly: ~33% at 2.0x,
    # ~55% at 1.5x.
    maintenance_margin_pct: float = Field(default=25.0, gt=0, lt=100)
    # --- Stage 3 (shorts) -----------------------------------------------
    # Shorts carry the SAME limits as longs (owner decision 2026-09-17).
    # There is deliberately no short-specific concentration or gross-bearish
    # cap: the former `max_single_short_pct` (10) and `max_gross_bearish_pct`
    # (20) were unsourced numbers. One short is capped by `max_position_pct`
    # exactly as one long is (src/risk/rules.py), and the book either way is
    # bounded by `max_gross_exposure_x` and `max_total_position_pct`. Both
    # removed keys are rejected loudly by `_reject_removed_short_cap_keys`.
    # Sizing-only haircut (never applied to stop placement) on a short's
    # risk-per-share. A short gaps through its stop upward with no bound —
    # equal nominal risk is not equal real risk — so the same risk
    # allocation opens a SMALLER short than an equivalent long.
    #
    # 2026-09-26, board item 186: the DIRECTION above is arithmetic and needs
    # no citation. The MAGNITUDE 1.5 is still a chosen number. Researched and
    # deliberately NOT sourced: the skewness-pricing literature measures
    # expected returns to lottery-like stocks, not the size of an overnight
    # gap against a short, and the empirical overnight-gap studies are
    # index-level and disagree in sign. Measuring it properly needs a stored
    # daily-bar history this desk does not keep. The number ledger carries
    # the routed owner-appetite question; 1.5 means a short opens at
    # two-thirds the size of a long carrying the same stated risk.
    short_gap_risk_multiple: float = Field(default=1.5, gt=1.0, le=3.0)
    # --- Kill switch (2026-09-02 operational safety guard) ---------------
    # A file whose mere EXISTENCE halts every order this desk would place —
    # entries, exits, covers, and protective-stop placement/replacement
    # alike. Read with `Path(...).exists()` and nothing else: no parsing, no
    # schema, so a malformed or empty file still halts — it cannot fail open
    # on bad content because it never reads any content. Ops stops the desk
    # with `touch <path>` and resumes it by deleting the file: no code
    # change, no deploy, and it takes effect on the NEXT order attempt even
    # if the process was already mid-session when the file appeared.
    #
    # Checked in `src/execution/broker.py` (the deterministic execution
    # layer), never by an agent or a prompt — a language model has no path
    # to talk the desk out of a halt it cannot see or reason about.
    #
    # UNLIKE every other guard in this file, this ONE also blocks
    # risk-REDUCING orders. Every other hard block and circuit breaker here
    # deliberately lets a SELL/COVER through even while it blocks new risk
    # (`RiskRuleEngine.check`'s `action in ("SELL", "COVER")` exemption
    # below), precisely so a bad account state can never trap a position.
    # The kill switch is the one lever that overrides that, for the case
    # where ops needs EVERYTHING stopped — including an exit that might
    # otherwise go out into a broken/stale market. It only blocks NEW
    # broker-bound order flow; a protective stop already resting at the
    # broker from before the halt is untouched and keeps protecting the
    # position.
    kill_switch_path: str = Field(default="data/KILL_SWITCH")

    #: Spec §10.3. Multiple of `max_sector_pct` used as the absolute sector
    #: ceiling when `max_sector_hard_pct` is not set explicitly. ClassVar, so
    #: pydantic treats it as a constant rather than a settable field.
    SECTOR_HARD_MULTIPLE: ClassVar[float] = 1.5

    #: Spec §12.3. The terminal bound on the DERIVED ceiling. With the target
    #: at 75 (§12.3) the 1.5x multiple gives 112.5, which is not a ceiling at
    #: all — a dial with no terminal bound bounds nothing. 90 keeps a real
    #: ceiling while leaving 15 points of scaling range above the target.
    #:
    #: NOT IN THE RATIFIED §12.3 TEXT: the spec set the target and left the
    #: terminal bound unstated. 90 was chosen when §12.3 was built and is open
    #: for the owner to move. `risk.max_sector_hard_pct` in settings.yaml sets
    #: it explicitly and overrides this derivation entirely.
    SECTOR_HARD_CEILING_MAX: ClassVar[float] = 90.0

    @property
    def sector_hard_ceiling_pct(self) -> float:
        """The absolute sector ceiling, explicit or derived.

        Every consumer reads this rather than `max_sector_hard_pct` directly,
        so the derivation rule lives in exactly one place.

        Derived = 1.5x the target, capped at `SECTOR_HARD_CEILING_MAX` (90),
        and never below the target itself — a ceiling under the target it
        backstops would make the scaling band run backwards.
        """
        if self.max_sector_hard_pct is not None:
            return self.max_sector_hard_pct
        derived = min(
            self.SECTOR_HARD_CEILING_MAX,
            self.max_sector_pct * self.SECTOR_HARD_MULTIPLE,
        )
        return min(100.0, max(self.max_sector_pct, derived))

    @model_validator(mode="after")
    def _sector_hard_ceiling_is_above_the_target(self):
        # A hard ceiling below the diversification target would mean the
        # scaling band runs backwards, and `sector_size_scale` would fall
        # back to gate behaviour silently. That is a config error worth
        # failing on rather than absorbing: the operator asked for something
        # incoherent and would otherwise never find out.
        if (
            self.max_sector_hard_pct is not None
            and self.max_sector_hard_pct < self.max_sector_pct
        ):
            raise ValueError(
                "risk.max_sector_hard_pct "
                f"({self.max_sector_hard_pct}) must be >= risk.max_sector_pct "
                f"({self.max_sector_pct}) — the absolute ceiling cannot sit "
                "below the diversification target it backstops"
            )
        return self

    @model_validator(mode="before")
    @classmethod
    def _reject_deleted_loss_alarm_keys(cls, data):
        # Owner instruction 2026-09-20 (docs/INCIDENT_HISTORY.md, retired
        # board item 32): the entire account-level loss-alarm mechanism — the
        # daily halt and the 5-day / 20-day BUY-halving brakes — was removed.
        # Same pattern and reason as the validators below: `extra="ignore"`
        # would let a stale deployment's settings.yaml keep these keys and
        # load silently, and an operator would believe a daily halt was
        # armed when nothing reads it. That is the single most dangerous
        # form this particular removal could rot into, because the belief
        # it creates is a belief about loss protection.
        if isinstance(data, dict):
            stale = [
                k for k in (
                    "max_daily_loss_pct",           # retired-ok
                    "daily_loss_risk_multiple",      # retired-ok
                    "drawdown_vol_sensitivity",      # retired-ok
                    "drawdown_5d_risk_multiple",     # retired-ok
                    "drawdown_20d_risk_multiple",    # retired-ok
                )
                if k in data
            ]
            if stale:
                raise ValueError(
                    f"risk.{', risk.'.join(stale)} was removed 2026-09-20 on "
                    "the owner's instruction: the account-level daily-loss "
                    "halt and the 5-day/20-day rolling-return BUY brakes are "
                    "retired in full (docs/INCIDENT_HISTORY.md, board item "
                    "32). There is NO replacement key and no account-level "
                    "loss limit — per-position stops are the desk's loss "
                    "protection, and the §11.2 gross-exposure de-levering "
                    "ladder (risk.max_gross_exposure_x) is the only "
                    "account-level drawdown response left. Delete the key "
                    "from the settings file."
                )
        return data

    @model_validator(mode="before")
    @classmethod
    def _reject_deleted_agreement_ceiling_key(cls, data):
        # Same pattern as `_reject_removed_short_cap_keys` below and
        # `ExecutionConfig._reject_deleted_repeg_keys`: BaseModel's default
        # `extra="ignore"` would let a stale deployment's settings.yaml keep
        # the key and load silently, and an operator would believe a sizing
        # ladder they set was in force when nothing reads it.
        if isinstance(data, dict) and "agreement_ceiling_pct" in data:
            raise ValueError(
                "risk.agreement_ceiling_pct was removed 2026-09-14: the "
                "graduated agreement sizing ladder is retired (the sqrt(n/5) "
                "law prices INDEPENDENT estimates and this desk's seats are "
                "not independent). Agreement is now a refusal gate only — "
                "see src/risk/rules.py::agreement_refuses_trade. Delete the "
                "key from the settings file."
            )
        return data

    @model_validator(mode="before")
    @classmethod
    def _reject_removed_short_cap_keys(cls, data):
        # Owner decision 2026-09-17: shorts carry the same limits as longs.
        # `max_single_short_pct` and `max_gross_bearish_pct` (and the latter's
        # pre-2026-08-30 name `max_short_gross_pct`) no longer exist. Same
        # pattern and reason as the validators around it: `extra="ignore"`
        # would let a settings.yaml still carrying one load silently, and an
        # operator would believe a short cap was in force when nothing reads
        # it. There is no replacement key — `max_position_pct` now governs a
        # short exactly as it governs a long.
        if isinstance(data, dict):
            stale = [
                k for k in (
                    "max_single_short_pct",
                    "max_gross_bearish_pct",
                    "max_short_gross_pct",
                )
                if k in data
            ]
            if stale:
                raise ValueError(
                    f"risk.{', risk.'.join(stale)} removed 2026-09-17: shorts "
                    "carry the same limits as longs (risk.max_position_pct, "
                    "risk.max_gross_exposure_x, risk.max_total_position_pct). "
                    "Delete the key from the settings file; there is no "
                    "replacement key."
                )
        return data

    @model_validator(mode="before")
    @classmethod
    def _reject_deleted_level_match_key(cls, data):
        # docs/WORK.md item 46 (2026-09-13). Same pattern and same reason as
        # the validators above: `extra="ignore"` would let a settings.yaml
        # still carrying this key load silently, and an operator would
        # believe a match tolerance they set was in force when nothing reads
        # it any more. There is deliberately NO replacement key to point at
        # — the tolerance is no longer configurable, because it is derived
        # from the level zone's own definition. See the block where this
        # field used to be declared, above.
        if isinstance(data, dict) and "level_match_atr_tolerance" in data:
            raise ValueError(
                "risk.level_match_atr_tolerance was removed 2026-09-13 "
                "(docs/WORK.md item 46): an ATR multiple can never stay "
                "consistent with the percentage-of-price zone it claimed to "
                "cover. The tolerance is now derived from "
                "src.data.levels.CLUSTER_TOLERANCE_PCT and is not "
                "configurable. Delete the key from the settings file; there "
                "is no replacement key."
            )
        return data

    @model_validator(mode="before")
    @classmethod
    def _reject_deleted_reward_risk_floor_key(cls, data):
        # Board item 81 (2026-09-24). Same pattern and same reason as the
        # validators above: `extra="ignore"` would let a settings.yaml still
        # carrying this key load silently, and an operator would believe a
        # reward:risk floor was in force when nothing read it. It refused
        # nothing and capped nothing since 2026-09-17 (owner: residual
        # invented R/R is a defect) — see `src.risk.constants.REWARD_RISK_FLOOR`
        # for the full history. There is no replacement key.
        if isinstance(data, dict) and "min_reward_risk_after_widening" in data:
            raise ValueError(
                "risk.min_reward_risk_after_widening was removed 2026-09-24 "
                "(board item 81): it refused nothing and capped nothing. "
                "Delete the key from the settings file; there is no "
                "replacement key."
            )
        return data

    @model_validator(mode="before")
    @classmethod
    def _reject_deleted_stop_width_gate_key(cls, data):
        # Board item 56 (2026-09-26), route (c). Same pattern and same
        # reason as the validator above: with `extra="ignore"` a
        # settings.yaml still carrying this key would load silently and an
        # operator would believe a stop-width refusal was in force when
        # nothing reads it. The gate is deleted, not retuned -- see
        # docs/INCIDENT_HISTORY.md 2026-09-26. There is no replacement key:
        # width is answered by position size, and the touch-probability
        # READING is still recorded on every sized stop
        # (`src.data.levels.touch_probability`).
        if isinstance(data, dict) and "max_stop_width_reach_atr_multiple" in data:
            raise ValueError(
                "risk.max_stop_width_reach_atr_multiple was removed "
                "2026-09-26 (board item 56): the stop-width refusal it "
                "thresholded is deleted, never having refused a single "
                "trade. Delete the key from the settings file; there is no "
                "replacement key. `max_target_reach_atr_multiple` is a "
                "different number and is unchanged."
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
    moved materially since the last close; those movers (capped) PLUS
    currently held investable names get real daily bars/indicators and a
    real tech_analyst call, then the SAME DecisionStage -> RiskStage ->
    ExecutionStage chain morning uses. Held names are coverage so an
    increase on a quiet hold can ground; they do not consume the mover cap.
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
    """Hard cap on how many MOVER symbols get a real tech_analyst call in
    one tick — keeps discovery bounded even on a broad-market move day.
    Held names are added on top of this cap so quiet holds still receive
    current-run Technical; they are coverage, not extra discovery."""


class SmartMoneyConfig(BaseModel):
    enabled: bool = False
    search_url: str = "https://efts.sec.gov/LATEST/search-index"
    archives_url: str = "https://www.sec.gov/Archives/edgar/data"
    data_dir: str = "data/smart_money"
    user_agent: str = "QAMC research-intelligence qamc-contact@proton.me"
    request_timeout_s: float = Field(default=15.0, ge=1, le=60)
    refresh_deadline_s: float = Field(default=180.0, ge=10, le=600)
    # The watched-name Form 4 drain's OWN budget, started only after the
    # market-wide pass above has finished with `refresh_deadline_s`. Until
    # 2026-09-19 the drain shared that 180 s with the market-wide pass, which
    # ran first and measured ~153 s on its own (journal, 2026-09-18
    # 12:00:41 -> 12:03:14 UTC), so the drain could never finish.
    #
    # Sized to clear the MEASURED watched backlog in one pre-market run, read
    # against SEC read-only on 2026-09-19 with the desk's own User-Agent and
    # rate limiter:
    #   82 watched issuers, one filing-history GET each: 11.0 s measured;
    #   5,431 unread Form 4s inside `lookback_days` on those issuers;
    #   0.156 s per filing read, measured over 40 reads at the 8 req/s
    #   limiter below (SEC's published maximum is 10 req/s:
    #   https://www.sec.gov/os/accessing-edgar-data).
    #   11.0 + 5,431 x 0.156 = 858.2 s -> 859.
    # It must also fit inside the job that runs it: TimeoutStartSec=1260 in
    # scripts/systemd/quant-agent-earnings_preprocess.service. Measured job
    # parts: ~2 s startup, `refresh_deadline_s` 180, and at most 147 s of
    # work after the refresh (2026-09-17 journal, 12:03:14 -> 12:05:41) —
    # 2 + 180 + 859 + 147 = 1,188 <= 1,260. tests/test_form4_backlog_order.py
    # keeps that sum honest if any term changes.
    #
    # It binds only on the one-time catch-up: the steady-state inflow on
    # those issuers is ~16 filings a day (5,801 in-window / 365), ~3 s.
    # Progress is kept per issuer, so a drain that does not finish loses
    # nothing and the next morning resumes where it stopped.
    # Upper bound = the room that sum leaves: 1,260 - 2 - 180 - 147 = 931.
    # Lower bound mirrors `refresh_deadline_s`'s.
    watched_drain_deadline_s: float = Field(default=859.0, ge=10, le=931)
    requests_per_second: float = Field(default=8.0, ge=0.5, le=10.0)
    # 7 -> 90 -> 365 on 2026-09-11. This bounds how far back an insider/SEC
    # observation is FETCHED and RETAINED at full detail — a trade older
    # than this is invisible to correlation entirely, not just discounted.
    #
    # 365 days is a real BEHAVIORAL bound, not a calendar guess: per the
    # owner directly, someone acting on genuine inside information has no
    # logical reason to sit on it for more than a year before trading —
    # if they haven't acted within a year, the information itself either
    # played out already or was never that actionable. That's what sets
    # this number, not a storage/network cost tradeoff.
    #
    # (7 days matched nothing real to begin with: Seyhun (1986) found only
    # ~1/4 of an insider purchase's eventual abnormal return realizes in
    # the first 5 days and ~1/2 is still unrealized after a full month;
    # real M&A run-ups start MONTHS before the announcement. 90 was an
    # intermediate step, matching `EARNINGS_STANCE_MAX_AGE_DAYS`.)
    #
    # Neither real infra cost binds at 365: NETWORK cost is already
    # bounded elsewhere — `refresh()` is accession-keyed and resumable, a
    # filing already processed is never re-fetched, so this number only
    # sets how many PAST DAYS get a "anything new here?" search query each
    # refresh, with headroom left in this file's own rate/deadline budget.
    #
    # RE-CHECKED 2026-09-18, and the paragraph above was TEMPORARILY FALSE
    # for one day. It rests on the claim that only `refresh()` walks the
    # window — once a day, pre-market. `peek_accessions` was added
    # 2026-09-17 and called the same day-by-day discovery from inside every
    # intraday decision tick, so the cost this number was cleared against
    # was being paid ~13 times a day inside the decision path, where it ran
    # the tick out of its deadline. The derivation is sound again because
    # the intraday freshness check no longer walks the window at all: it
    # reads each watched issuer's own filing history
    # (`SECForm4Provider.form4_freshness`), which is O(watched names) and
    # independent of this number. Anything added later that walks the
    # lookback window from inside a decision tick falsifies this paragraph
    # again — that is the thing to check, not the value.
    #
    # RE-CHECKED 2026-09-19: "a filing already processed is never
    # re-fetched" still holds, but the claim that this number only sets how
    # many search queries run does NOT. Since PR #529 the watched-name drain
    # must READ every unread Form 4 inside this window on every watched
    # issuer before that issuer's evidence can be called current. Raising
    # 7 -> 365 therefore created a one-time read of 5,431 filings on the
    # desk's 82 watched issuers (measured 2026-09-19), which the drain
    # could not do inside the 180 s it shared with the market-wide pass.
    # That cost now has its own budget, `watched_drain_deadline_s`, sized
    # from the measurement. The value 365 is unchanged — it is the owner's
    # behavioural bound, and draining it is cheaper than seeding a claim
    # of coverage the desk has not read.
    # STORAGE cost is small: measured directly against the live server's
    # actual cache 2026-09-11 — 4,324 records / 5.76 MB at the old 7-day
    # window, roughly ~300 MB at 365 days on a straight scale-up — trivial
    # for a server either way.
    #
    # This is a FETCH/RETENTION bound, not a support-eligibility gate —
    # whether an old observation can actually support a target is decided
    # by correlation with other current evidence (see
    # `PortfolioManagerAgent`'s grounding validator), not by this number.
    lookback_days: int = Field(default=365, ge=1, le=365)
    max_filings_per_refresh: int = Field(default=1000, ge=1, le=5000)
    max_observations: int = Field(default=40, ge=1, le=200)
    # ROW-RETENTION window for `cluster_survivors`, NOT the research cluster
    # (corrected 2026-09-19, board item 124). Alldredge & Blank's abstract
    # (J. Financial Research, 2019) measures SAME-DAY purchases; "within two
    # days" appears only in a secondary summary (IBKR Campus). The
    # research-defined same-day opportunistic purchase cluster is
    # `src.data.smart_money_cluster.insider_purchase_clusters`. Was 14 days
    # with no documented rationale until the 2026-09-04 audit fix.
    cluster_window_days: int = Field(default=2, ge=1, le=45)
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
    # REMOVED 2026-09-13: `insider_min_material_sell_fraction`. It relabelled
    # a sale below some fraction of the insider's holding as ROUTINE, weight
    # 0.0 — dropping it out of the seat's ranking entirely. Its 0.05 default
    # matched no published band, and the source behind the rule (Scott & Xu,
    # FAJ 2004) marks only 50% as a significance boundary and measures the
    # sub-10% band as significantly POSITIVE, so no edge of it is a "not a
    # directional view" line. The ratio is now reported on every observation
    # (`holdings_fraction`, `holdings_fraction_band`) and gates nothing. Do
    # not reintroduce a cutoff here without a source that measures one; the
    # open question is WORK.md item 63. See `src/data/insider_signal.py`
    # departure #3 and the 2026-09-13 `docs/INCIDENT_HISTORY.md` entry.
    #
    # How long `data/smart_money/insider_history.json` retains a trade date
    # before it is pruned. Must comfortably exceed the calendar-routine
    # lookback (`insider_calendar_routine_years` years) with slack for late
    # and amended filings — `observations.json` itself is pruned to
    # `lookback_days`, far too short for the calendar test, which is the
    # entire reason a separate long-horizon index exists. Default is 5
    # years (5 * 366 days, leap-safe).
    insider_history_retention_days: int = Field(default=5 * 366, ge=366, le=20 * 366)

    # --- Congress (House + Senate) trading-disclosure cross-check ---------
    # `src/data/congressional_trading.py::CongressionalTradingProvider`.
    # Two independent free, credentialless sources are cross-checked against
    # each other rather than trusted singly: both are single-operator, young
    # projects with no track record. Switched on 2026-09-20 per owner
    # ruling 2026-09-19 (docs/INCIDENT_HISTORY.md, that date): congressional
    # trading disclosures are evidence and must be weighted by the PM, never
    # zeroed out on research grounds — see `SmartMoneyFinding
    # .deterministic_eligibility`'s congressional branch (src/models.py) for
    # the confirmatory-ceiling and cluster-cap enforcement that ships with
    # this flip.
    congress_enabled: bool = True
    congress_kadoa_url: str = (
        "https://raw.githubusercontent.com/kadoa-org/"
        "congress-trading-monitor/main/public/data/trades.json"
    )
    congress_congresswatch_url: str = "https://congresswatch.us/data/trades.json"
    congress_data_dir: str = "data/smart_money/congressional"
    congress_request_timeout_s: float = Field(default=15.0, ge=1, le=60)
    congress_refresh_deadline_s: float = Field(default=60.0, ge=10, le=300)
    # kadoa's top-level `trades.json` is itself capped at a 5,000-row recent
    # slice (not our choice, theirs); congresswatch's bulk file is ~8,000
    # rows. This just bounds how many of either we hold in memory per
    # refresh, as a sanity ceiling rather than a real limiter.
    congress_max_trades_per_source: int = Field(default=10_000, ge=100, le=50_000)
    # Congressional disclosures can lag up to ~45 days after the transaction
    # (already documented in src/agents/smart_money_analyst.py's module
    # docstring — not a new number invented here). congresswatch.us's live
    # schema carries no filing/disclosure-date field at all, so when a
    # congresswatch-only trade cannot be cross-matched against kadoa (which
    # does carry a real filing_date), this ceiling is used as the
    # conservative disclosure-date estimate: assume the latest date the
    # statute allows, never an earlier one that would overstate freshness.
    congress_assumed_max_disclosure_lag_days: int = Field(default=45, ge=1, le=90)
    # How recent a congressional disclosure must be to stay in `fetch()`'s
    # output. Deliberately MUCH looser than `lookback_days` (7): that window
    # is sized for SEC Form 4's ~2-business-day filing deadline, and applying
    # it to a stream that can legally lag 45 days would silently discard
    # nearly every real disclosure. Do NOT "harmonise" the two — they measure
    # two different statutory regimes and the Form 4 one is intentionally
    # tighter.
    #
    # Why 180 and not the 30 this shipped with:
    #   * The STOCK Act deadline is "no later than 45 days after the
    #     transaction" (House Ethics / Senate Select Committee on Ethics PTR
    #     instructions, re-verified 2026-09-04) — so a 30-day window cannot
    #     even cover the LEGAL lag, let alone real behaviour.
    #   * Filers in practice file at or near the deadline, and late filings
    #     (past 45 days) are common and still legitimate, recent trades.
    #   * Corroborating real-world datapoint: the author of a comparable free
    #     congressional-trading tool documented choosing a 180-day default for
    #     exactly this reason — a short window silently returned almost
    #     nothing. 180 is that observed-in-the-wild figure, not one invented
    #     here.
    # This is a data-COVERAGE window, not a signal-strength one. Corrected
    # 2026-09-19: this comment used to say `SmartMoneyFinding.
    # deterministic_eligibility` (src/models.py) requires congressional-only
    # evidence to be <=7 days old. That age cutoff was removed by the
    # 2026-09-11 redesign; what that validator still checks is structure (two
    # or more members, one direction, each filed within the STOCK Act's 45
    # days). Age is now weighed downstream by correlation with current
    # evidence, and the congressional refresh reports how old the newest
    # disclosure and the newest trade are.
    congress_lookback_days: int = Field(default=180, ge=1, le=365)

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


class UniverseScreenConfig(BaseModel):
    """Universe expansion and pruning (`src/universe_screen.py`).

    The design agreed with the owner 2026-09-01 (docs/INCIDENT_HISTORY.md,
    "Universe expansion and pruning"), built 2026-09-19. With `enabled` off
    NOTHING changes: no weekly screen runs, no screened symbol reaches a
    session, and the SEC Form 4 and nomination side doors keep their
    pre-existing gates. With it on, both side doors run the same screen and
    the Form 4 door gets its age gate back.

    The spread and volatility thresholds have no field here on purpose: they
    are DERIVED at run time from `execution.max_entry_slippage_bps` and
    `risk.min_stop_atr_multiple` (see the module docstring), and the cap on
    screened names per session is `nominations.max_per_seat_per_run` — the
    screen is one more source of candidates, capped like one seat.
    """

    enabled: bool = False
    data_dir: str = "data/universe"
    # SEC Rule 3a51-1(d), 17 CFR 240.3a51-1: an equity security "that has a
    # price of five dollars or more" is not a penny stock
    # (https://www.law.cornell.edu/cfr/text/17/240.3a51-1, fetched
    # 2026-09-19). The owner's words were "filter out ... the penny stocks";
    # this is the legal line for what a penny stock is.
    min_price_usd: float = Field(default=5.0, gt=0)
    # FTSE Russell US indexes methodology: ineligible — "Companies under $30
    # Million in total market capitalization" (https://www.lseg.com/content/
    # dam/ftse-russell/en_us/documents/other/ftse-russell-us-indexes-
    # methodology-overview-cut-sheet.pdf, fetched 2026-09-19). The floor of
    # the broadest published US investable-equity index.
    min_market_cap_usd: float = Field(default=30_000_000, gt=0)
    # Wall-clock budget for one incremental pass. It runs at the end of the
    # evening session: TimeoutStartSec=1260 in
    # scripts/systemd/quant-agent-evening.service, and the evening body
    # measured 173 s on 2026-09-19 (journal, 00:00:10 -> 00:03:03 UTC).
    # 173 + 900 = 1,073 <= 1,260, leaving 187 s — more than the whole
    # measured body again. A pass that does not finish loses nothing: the
    # next evening resumes with whoever is still due.
    screen_deadline_s: float = Field(default=900.0, ge=10, le=1080)
    # Symbols per daily-bar download request (yfinance multi-ticker).
    bars_batch_size: int = Field(default=50, ge=1, le=200)


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


#: The intraday control's tick spacing, in minutes. Duplicated from
#: `src/scheduler.py::_build_intra_check_trigger`, which is the authority;
#: pinned here so the two are at least visible in one grep, and asserted
#: equal by `tests/test_cost_circuit.py`.
INTRA_CHECK_TICK_MINUTES = 30


def _paid_run_count() -> int:
    """How many scheduled runs in a trading day can make a paid call.

    Every canonical session window, with `intra_check` counted once per
    30-minute tick because it fires that often.

    `src/trading_calendar.py` used to label that window "no LLM" and an
    earlier draft of this number excluded it on that basis. The label is
    false: on the production DB intra_check is the desk's LARGEST paid
    cost centre -- $0.5672 of the day's $0.7883 on 2026-09-22 (72%) and
    $2.3839 of $2.6478 on 2026-09-21 (90%), 13-14 paid sessions a day
    [measured 2026-09-23, llm_budget_sessions]. The 2026-09-22 latch this
    whole change exists for was tripped BY an intra_check run. Excluding
    it would derive a paid-call number by leaving out most of the paid
    calls. Currently 19: 14 ticks plus earnings_preprocess, morning,
    midday, close and evening.
    """
    lo_min, hi_min = SESSION_WINDOWS["intra_check"]
    intra_ticks = len(range(lo_min, hi_min + 1, INTRA_CHECK_TICK_MINUTES))
    return intra_ticks + len([m for m in SESSION_WINDOWS if m != "intra_check"])


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
    # Item 14 (OWNER-APPROVED 2026-09-02, docs/WORK.md): the per-call cost
    # RESERVATION layer -- and every exposure ceiling / per-mode allowance /
    # afternoon reserve / free-failure-session backstop that existed only
    # to manage a reservation's over-holding -- is deleted. Real calls
    # settled at a median 0.38x of the pinned worst-case reservation rate,
    # so that machinery held ~2.6x what was ever spent and stopped the desk
    # on money that was never spent. What replaces it, exactly:
    #   (a) a spend cap on the OpenRouter API key itself -- outside this
    #       codebase; see docs/WORK.md item 14(a). NOT implemented here.
    #   (b) `session_cost_limit_usd` / `daily_cost_limit_usd` above, checked
    #       against REAL SETTLED cost only (no projection) by
    #       `LLMCostCircuitBreaker._enforce_settled_limits_locked`.
    #   (c) `max_calls_per_session` below -- a count-based runaway-loop
    #       backstop, independent of price.
    #
    # Set 2026-09-03 from real production data: the worst COMPLETE session
    # ever recorded made 14 calls, almost all of it tech_analyst chunking
    # the symbol universe, not the portfolio_manager (always exactly 1 call
    # per session). 40 is ~3x that measured ceiling -- a first number, not
    # a final one; see the DECIDE BY line in docs/WORK.md.
    max_calls_per_session: int = Field(default=40, ge=1)
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
        default_factory=lambda: provider_attempt_budget(
            failover_available=True, tertiary_available=True,
        ),
        ge=1,
    )
    # === Transient-latch self-clear (Defect B, 2026-09-22) ===
    # A hard latch raised by a FAILED provider call whose cost could not be
    # proven used to wait for a human. On 2026-09-22 that cost the desk the
    # close and evening runs and nine hours of refused analysis on $0.7883 of
    # a $2.75 day.
    #
    # THE COOLDOWN'S ADMISSIBLE INTERVAL, and why the midpoint:
    #   Upper bound 30 min -- the gap between consecutive paid runs, which
    #     is the intra_check tick, the most frequent paid run there is
    #     [measured: 13-14 paid intra_check sessions a day]. At or above it
    #     a second paid run is lost, which is the damage being fixed.
    #   Lower bound 0 -- there is no run-duration floor. The run that trips
    #     the latch STOPS at the trip (`_trip_locked` suspends its session
    #     and every later `begin_call` raises); the 2026-09-22 tripper ran
    #     1.47 min end to end [measured]. Too short is not unsafe, it just
    #     wastes the day's allowance re-failing against a provider that is
    #     still down.
    # 15.0 is the midpoint of (0, 30): the value furthest from both failure
    # modes, and the one most tolerant of run-start jitter and clock skew in
    # either direction. Two earlier derivations were wrong and are recorded
    # so the number is not re-derived from them: "half the intra tick"
    # (right value, but justified by an aesthetic half) and "bracketed by
    # the longest run at 9.8 min and the 90-min earnings-to-morning gap"
    # (wrong on both ends -- the tripping run does not continue, and 90 min
    # only looks like the smallest gap if intra_check is wrongly excluded).
    transient_latch_cooldown_minutes: float = Field(default=15.0, gt=0, allow_inf_nan=False)
    # One forgiveness per scheduled PAID run in a trading day. A fault
    # recurring past that has outlasted every paid run of the day and is not
    # a transient blip, so the next occurrence latches durably and waits for
    # a human -- which is also what bounds how many unproven-cost calls a
    # single day can forgive without one.
    max_transient_latch_auto_clears_per_day: int = Field(
        default_factory=lambda: _paid_run_count(), ge=1,
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
    # order of once a quarter, not hour to hour. So: within this many hours
    # PAST the 24h freshness boundary, a cache that can't be refreshed live
    # is used rather than latched -- widened per
    # `openrouter_pricing_stale_multiplier_max` below and logged loudly on
    # every call -- and only a cache older than 24h + this grace, or no
    # cache at all, or a cache missing a rate for a model actually
    # configured, still fails closed exactly as before. 0 restores the
    # pre-fix behaviour (fail the instant the cache turns stale) for anyone
    # who wants it back. Independent of item 14: this bounds the pricing
    # CATALOG's own staleness, not a call's dollar reservation (deleted).
    openrouter_pricing_grace_period_hours: float = Field(
        default=24.0, ge=0.0, le=168.0, allow_inf_nan=False,
    )
    # Multiplier applied to a stale-but-in-grace rate at the FAR edge of the
    # grace window above (`cost_table.openrouter_pricing_reservation_
    # multiplier` scales linearly up to this value as the cache ages toward
    # the end of grace) -- still used for the estimated-cost fallback in
    # `estimate_cost()` when a provider does not report its own cost, even
    # though item 14 removed the per-call reservation this was originally
    # sized for.
    openrouter_pricing_stale_multiplier_max: float = Field(
        default=1.50, ge=1.0, le=5.0, allow_inf_nan=False,
    )
    # === Infrastructure-fault retry (docs/WORK.md item 17a, 2026-09-03) ===
    # Before this, ANY exception while reading/seeding the ledger --
    # "I cannot read the budget", e.g. a transient SQLite lock or disk I/O
    # error -- was treated exactly like "I am over budget" (a real, measured
    # breach): both latched paid analysis via the durable file marker on the
    # very first occurrence, requiring an operator to clear it by hand. A
    # real breach still latches immediately and correctly (`_trip_locked`
    # writes the in-DB `llm_circuit_state` row, unaffected by this block).
    # This block only bounds retries for the DB-open/read path itself
    # (`LLMCostCircuitBreaker._run_with_infra_retry`, used by construction,
    # `activate_session`, and `enforce_current_limits`) before IT escalates
    # to the same durable latch.
    #
    # Shape and defaults mirror `MacroConfig` above (`max_retries`,
    # `retry_backoff_base_s`, `retry_backoff_max_s`, `retry_backoff_jitter_s`)
    # -- the same "bounded exponential-backoff retry before a harder failure
    # mode" pattern this codebase already uses for FRED's transient network
    # faults (`MacroDataProvider._next_backoff`), reused rather than a fresh
    # number invented for this circuit.
    infra_fault_max_retries: int = Field(default=2, ge=0, le=5)
    """Bounded retries for a transient cost-circuit infrastructure fault
    BEFORE it escalates to the durable emergency latch. Mirrors
    `MacroConfig.max_retries`."""

    infra_fault_retry_backoff_base_s: float = Field(default=2.0, gt=0, le=30.0)
    """First retry's backoff, in seconds; doubles each subsequent retry,
    capped at `infra_fault_retry_backoff_max_s`. Mirrors
    `MacroConfig.retry_backoff_base_s`."""

    infra_fault_retry_backoff_max_s: float = Field(default=8.0, gt=0, le=60.0)
    """Ceiling on the exponential backoff. Mirrors
    `MacroConfig.retry_backoff_max_s`."""

    infra_fault_retry_backoff_jitter_s: float = Field(default=1.0, ge=0, le=10.0)
    """Uniform jitter, 0..this many seconds, added to every backoff sleep.
    Mirrors `MacroConfig.retry_backoff_jitter_s`."""

    @model_validator(mode="before")
    @classmethod
    def _reject_deleted_reservation_keys(cls, data):
        # Item 14 (OWNER-APPROVED 2026-09-02, docs/WORK.md): the per-call
        # cost reservation layer -- and every key below that existed only
        # to manage its over-holding -- is deleted. BaseModel's default
        # `extra="ignore"` would let a settings.yaml still carrying one of
        # these load silently, quietly dropping whatever an operator set.
        # Fail loudly instead of drifting doc-versus-behaviour again.
        removed_keys = {
            "max_paid_sessions_per_mode_per_day",  # pre-2026-08-29 name
            "session_reserved_exposure_limit_usd",
            "daily_reserved_exposure_limit_usd",
            "max_free_failure_sessions_per_mode",
            "backstop_cooloff_minutes",
            "max_mode_daily_exposure_pct",
            "afternoon_reserve_pct",
            "afternoon_reserve_release_et_hour",
            "max_retry_attempts_per_session",
            "reservation_ttl_minutes",
            "reservation_multiplier",
            "reservation_min_history_samples",
            "reservation_conservative_percentile",
            "reservation_output_margin",
        }
        if isinstance(data, dict):
            present = sorted(removed_keys & set(data))
            if present:
                raise ValueError(
                    "llm_cost_circuit no longer supports: " + ", ".join(present)
                    + " -- item 14 (2026-09-02, docs/WORK.md) deleted the "
                    "per-call cost reservation layer these configured. Remove "
                    "them from the settings file; see max_calls_per_session "
                    "for the replacement runaway-loop backstop."
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
    # Optional section — absent means the screen is off (enabled=False).
    universe_screen: UniverseScreenConfig = Field(default_factory=UniverseScreenConfig)
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

    def _tertiary_key_for_provider(self) -> str:
        """The credential route 3 needs, by the same mapping as the fallback's.

        Deliberately a separate method rather than a parameterised one: the
        two are read in different places and a shared helper with a provider
        argument invites a call site passing the wrong one silently.
        """
        return {
            "openai": self.api_keys.openai,
            "deepseek": self.api_keys.deepseek,
            "openrouter": self.api_keys.openrouter,
            "google": self.api_keys.google,
        }.get(self.llm.tertiary_provider, self.api_keys.anthropic)

    def tertiary_available(self) -> bool:
        """True when route 3 is both configured and credentialed.

        Mirrors `BaseAgent._tertiary_reachable` minus the per-agent
        distinct-pair test, for the same reason `_fallback_reachable_for_any_
        agent` exists: the load-time attempt-budget check and the runtime gate
        drifting apart is the 2026-08-31 outage.
        """
        if not (self.llm.tertiary_model or "").strip():
            return False
        return bool((self._tertiary_key_for_provider() or "").strip())

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
        required = provider_attempt_budget(
            failover_available=failover_available,
            tertiary_available=self.tertiary_available(),
        )
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


def _substitute_env_vars(value: str, overrides: dict[str, str] | None = None) -> str:
    """Replace ${VAR_NAME} with environment variable values.

    `overrides` takes precedence over `os.environ` for the names it carries. It
    exists for credentials systemd delivered as files rather than environment
    variables (see `src/credentials.py`): on the live box `.env` still holds a
    placeholder for those names, and the placeholder must not win.

    With `overrides` omitted or empty this behaves exactly as it always has —
    every other interpolation in `settings.yaml` is untouched.
    """
    def replacer(match):
        var_name = match.group(1)
        if overrides:
            override_value = overrides.get(var_name)
            if override_value is not None:
                return override_value
        env_value = os.environ.get(var_name)
        if env_value is None:
            return ""  # Optional env vars resolve to empty string
        return env_value
    return re.sub(r"\$\{(\w+)\}", replacer, value)


def _walk_and_substitute(obj, overrides: dict[str, str] | None = None):
    """Recursively substitute env vars in all string values."""
    if isinstance(obj, str):
        return _substitute_env_vars(obj, overrides)
    if isinstance(obj, dict):
        return {k: _walk_and_substitute(v, overrides) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_walk_and_substitute(item, overrides) for item in obj]
    return obj


def load_config(path: Path) -> AppConfig:
    """Build the application config.

    Credentials systemd delivered as files are preferred over the environment;
    everything else resolves from the environment as before. A visibly broken
    systemd hand-off raises `CredentialDeliveryError` here rather than letting
    the desk start on a placeholder and fail later at the broker.
    """
    from src.credentials import load_systemd_credentials

    with open(path) as f:
        raw = yaml.safe_load(f)
    credential_overrides = load_systemd_credentials()
    substituted = _walk_and_substitute(raw, credential_overrides)
    return AppConfig(**substituted)
