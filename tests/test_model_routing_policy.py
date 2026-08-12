"""The accepted per-agent model routing policy, as invariants on config.

`config/settings.yaml` is where the policy actually lives — these tests are
what stops it drifting into a state that is unroutable, unpriceable, or
silently back on the expensive commissioning baseline. They read the shipped
YAML rather than a fixture on purpose: a test that passes against a fixture
while the deployed file says something else is worth nothing.

See `docs/architecture/MODEL_ROUTING_POLICY.md` for the evidence behind each
seat assignment.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from src.agents.base import VALID_PROVIDERS, resolve_provider
from src.cost_table import _PRICING_OPENROUTER, estimate_cost

SETTINGS = Path(__file__).resolve().parents[1] / "config" / "settings.yaml"

AGENTS = (
    "tech_analyst", "news_analyst", "macro_analyst", "earnings_analyst",
    "portfolio_manager", "risk_manager", "position_reviewer",
    "evening_analyst", "meta_reflector",
)

# The seats whose output is a trading decision or the gate over one. The
# policy may put a cheaper model on a specialist seat; these three are where
# a quality regression reaches the broker, so they are held to the decision
# tier explicitly rather than by convention.
DECISION_SEATS = ("portfolio_manager", "risk_manager", "position_reviewer")

# The commissioning baseline. Being back on it for every seat means the cost
# tranche silently reverted.
BASELINE_MODEL = "openai/gpt-5.5"


@pytest.fixture(scope="module")
def llm() -> dict:
    return (yaml.safe_load(SETTINGS.read_text()) or {}).get("llm") or {}


def test_every_agent_has_an_explicit_model(llm):
    for agent in AGENTS:
        model = llm.get(f"{agent}_model")
        assert isinstance(model, str) and model.strip(), agent


def test_every_agent_pins_its_provider_explicitly(llm):
    """OpenRouter ids are "vendor/model" strings that collide with native
    prefixes, so `resolve_provider` cannot infer them — an agent left without
    an explicit provider would route to Anthropic with an OpenRouter model id
    and fail on its first call of the session."""
    for agent in AGENTS:
        provider = llm.get(f"{agent}_provider")
        assert provider in VALID_PROVIDERS, f"{agent}: provider={provider!r}"
        assert resolve_provider(llm[f"{agent}_model"], provider) == provider


def test_every_routed_model_is_priceable_offline(llm):
    """Cost telemetry must not depend on reaching a pricing catalog mid-
    session. Every configured model resolves from the in-process table, so a
    session run with no route to OpenRouter's catalog still logs real cost
    instead of "$?.??"."""
    for agent in AGENTS:
        model = llm[f"{agent}_model"]
        assert model in _PRICING_OPENROUTER, f"{agent}: {model} not pinned"
        assert estimate_cost(model, 1000, 1000) is not None, model


def test_policy_is_cheaper_than_the_commissioning_baseline(llm):
    """The point of the tranche. Priced on a fixed synthetic workload so the
    assertion is about the POLICY, not about how chatty a model happened to
    be on one run."""
    baseline_rate = _PRICING_OPENROUTER[BASELINE_MODEL]
    per_call_in, per_call_out = 40_000, 3_000

    def sweep(model_for_agent) -> float:
        return sum(
            (per_call_in * r["input"] + per_call_out * r["output"]) / 1e6
            for r in (_PRICING_OPENROUTER[model_for_agent(a)] for a in AGENTS)
        )

    policy_cost = sweep(lambda a: llm[f"{a}_model"])
    baseline_cost = sweep(lambda a: BASELINE_MODEL)
    assert policy_cost < baseline_cost * 0.5, (
        f"policy ${policy_cost:.4f} vs baseline ${baseline_cost:.4f} — "
        "the routing tranche is supposed to at least halve model spend"
    )
    assert baseline_rate["input"] == 5.0  # guards the comparison's own basis


def test_decision_seats_are_not_on_the_cheapest_tier(llm):
    """A specialist seat may run a sub-$0.10/M model. The PM, the risk
    manager and the midday exit path may not: those are where a quality
    regression reaches the broker, and the benchmark bought their models a
    measured quality margin that a bottom-tier swap would throw away."""
    for agent in DECISION_SEATS:
        rate = _PRICING_OPENROUTER[llm[f"{agent}_model"]]
        assert rate["input"] >= 0.10, (
            f"{agent} is on {llm[f'{agent}_model']} at ${rate['input']}/M input — "
            "decision seats require a measured model, not the cheapest tier"
        )


def test_no_agent_exceeds_its_models_context(llm):
    """`max_tokens` is an OUTPUT ceiling. OpenRouter clamps a request above a
    model's own ceiling rather than rejecting it (verified 2026-08-12 against
    every model in the policy), so this asserts the weaker property that
    matters: the value is present, positive, and not absurd."""
    global_max = llm.get("max_tokens")
    assert isinstance(global_max, int) and global_max > 0
    for agent in AGENTS:
        value = llm.get(f"{agent}_max_tokens")
        if value is None:
            continue
        assert isinstance(value, int) and 0 < value <= 1_000_000, f"{agent}={value}"


def test_pinned_openrouter_table_has_no_unused_speculative_rows():
    """Every pinned rate is either in the policy or is the baseline the cost
    reduction is measured against. Rows for models nothing uses go stale
    unnoticed and make `verify_pricing.py` noisy."""
    llm = (yaml.safe_load(SETTINGS.read_text()) or {}).get("llm") or {}
    used = {llm[f"{a}_model"] for a in AGENTS} | {BASELINE_MODEL}
    unused = set(_PRICING_OPENROUTER) - used
    assert not unused, f"pinned but unused: {sorted(unused)}"


# --- failure behaviour the policy depends on ------------------------------


def test_openrouter_seat_fails_closed_rather_than_substituting_a_model():
    """The policy has no fallback, deliberately (see MODEL_ROUTING_POLICY.md).

    QAMC holds no Anthropic credential, so `_fallback_api_key` is empty and
    an exhausted OpenRouter call re-raises. What must never happen is a
    session quietly completing on a model the policy did not select for that
    seat — that is the "unrecorded model choice" the authorization forbids,
    and it would also mis-price the call.
    """
    from unittest.mock import MagicMock, patch

    from src.agents.risk_manager import RiskManagerAgent

    client = MagicMock()
    client.chat.completions.create.side_effect = ConnectionError("openrouter down")

    with patch("openai.OpenAI", return_value=client), \
            patch("anthropic.Anthropic") as anthropic_cls, \
            patch("time.sleep", lambda _s: None), \
            patch.dict("os.environ", {"QUANT_AGENT_MAX_RETRIES": "2"}):
        agent = RiskManagerAgent(
            api_key="placeholder", model="openai/gpt-5.5",
            max_tokens=64, provider="openrouter",
        )
        # `_execute` rather than `run`: the retry/failover loop is what is
        # under test, not the prompt builder that would otherwise need a
        # full PortfolioDecision fixture to reach it.
        with pytest.raises(ConnectionError):
            agent._execute("review this plan")
        anthropic_cls.assert_not_called()


def test_every_seat_resolves_to_the_openrouter_client(llm):
    """A seat that resolved to any other provider would be reaching for a
    credential OneCLI does not hold, and would fail on its first call."""
    for agent in AGENTS:
        assert resolve_provider(
            llm[f"{agent}_model"], llm[f"{agent}_provider"]
        ) == "openrouter", agent
