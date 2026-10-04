"""The composition root: AppConfig assembles every section and load_config builds it from settings.yaml.

Moved verbatim from src/config/__init__.py (pure move; bodies AST-identical).
"""

import os
import re
from pathlib import Path
import yaml

from pydantic import BaseModel, Field, model_validator
from src.agents.base import (
    provider_attempt_budget,
    resolve_provider,
)
from src.config.notifications import NotificationsConfig
from src.config.macro import MacroConfig
from src.config.broker import AlpacaConfig, ApiKeysConfig
from src.config.llm import AGENT_NAMES, LLMConfig
from src.config.execution import ExecutionConfig
from src.config.risk import CashReserveConfig, CashSweepConfig, EventRiskConfig, RiskConfig
from src.config.research import IntradayScanConfig, NewsConfig, NominationConfig, SmartMoneyConfig, UniverseScreenConfig
from src.config.operations import DeploymentGapConfig, EvolutionConfig, ReconciliationConfig, StorageConfig, TradingConfig
from src.config.llm_cost import LLMCostCircuitConfig


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
    deployment_gap: DeploymentGapConfig = Field(default_factory=DeploymentGapConfig)
    cash_reserve: CashReserveConfig = Field(default_factory=CashReserveConfig)
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

        # DELIBERATELY NOT CHECKED HERE: a missing credential for
        # `llm.tertiary_alt_provider`. The fallback check above is a hard
        # error because failover is a route the operator configured and is
        # relying on; the route-3 second-road substitute is a default-on
        # improvement nobody asked for, and refusing to BOOT over a key it
        # needs would turn a resilience feature into an outage of its own —
        # a single-provider deployment (every seat and the fallback on one
        # road) is a legal configuration and must still start. Without the
        # key the substitution simply does not happen and route 3 stays
        # where it was configured. The guard that matters for THIS
        # deployment is mechanical and lives in CI instead:
        # tests/test_route_failover_ladder.py::
        # test_the_shipped_config_leaves_no_seat_on_a_single_road reads
        # config/settings.yaml and fails if any seat's routes collapse onto
        # one provider — which is the 2026-09-29 defect stated as a test
        # rather than as a runtime hope.

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
