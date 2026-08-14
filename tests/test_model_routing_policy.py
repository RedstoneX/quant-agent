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

import json
from functools import lru_cache
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

# The seats whose output is a trading decision or the gate over one — where
# a quality regression reaches the broker. They are held to a higher bar than
# the specialist seats, but the bar is MEASURED QUALITY AT THAT SEAT, not
# price (see test_decision_seats_run_a_model_measured_at_that_seat).
DECISION_SEATS = ("portfolio_manager", "risk_manager", "position_reviewer")

# Which benchmark scenario measures each decision seat.
SEAT_SCENARIO = {
    "portfolio_manager": "pm_constrained",
    "risk_manager": "risk_rr_breach",
    "position_reviewer": "midday_exit",
}

RESULTS_DIR = Path(__file__).resolve().parents[1] / "ops" / "model_policy" / "results"


@lru_cache(maxsize=1)
def _benchmark_pairs() -> dict:
    """`{"<model>|<scenario>": aggregate_row}` across every committed results
    file, later files winning — the same supersede rule `--report` uses when
    a re-run corrects an earlier sweep."""
    pairs: dict = {}
    for path in sorted(RESULTS_DIR.glob("*.json")):
        data = json.loads(path.read_text())
        pairs.update((data.get("aggregate") or {}).get("pairs") or {})
    return pairs

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


def test_decision_seats_run_a_model_measured_at_that_seat(llm):
    """The invariant the decision seats actually need: their model was
    BENCHMARKED at that seat and did not fail a run.

    This replaces an input-price >= $0.10/M floor (removed at PR #30 review).
    Price is not a quality or safety property, and using it as a proxy
    contradicted the whole method of this tranche — the policy was selected
    on measured quality per dollar, and the model it selected for every seat
    is cheaper than that floor. The proxy would have failed the accepted
    policy while passing any expensive unmeasured model, which is backwards.

    `quality_min` rather than the mean on purpose: a seat that alternates
    between excellent and unparseable averages respectably and silences a
    session every other day.
    """
    for agent in DECISION_SEATS:
        model = llm[f"{agent}_model"]
        scenario = SEAT_SCENARIO[agent]
        pair = _benchmark_pairs().get(f"{model}|{scenario}")
        assert pair is not None, (
            f"{agent} is on {model}, which has no committed benchmark result "
            f"for its own scenario ({scenario}). Decision seats require a "
            f"model measured AT THAT SEAT — run "
            f"`ops/model_policy/benchmark_models.py --scenario {scenario} "
            f"--models {model}` and commit the results."
        )
        assert pair["runs"] >= 2, f"{agent}: {model} has only {pair['runs']} run(s)"
        assert pair["quality_min"] == 1.0, (
            f"{agent} is on {model}, which scored quality_min="
            f"{pair['quality_min']} on {scenario} — a decision seat may not "
            f"run a model with a failing run at its own scenario"
        )
        assert not pair["errors"], f"{agent}: {model} errored — {pair['errors']}"


def test_risk_seat_evidence_covers_the_rules_the_audit_gave_it(llm):
    """The risk seat owns two rules that no deterministic code enforces —
    the drawdown-halve and the <5d holding-discipline audit (2026-08-13
    agent audit, F6). `risk_rr_breach` does not exercise either, so the seat
    must additionally be measured on the scenario that does."""
    model = llm["risk_manager_model"]
    pair = _benchmark_pairs().get(f"{model}|risk_drawdown_discipline")
    assert pair is not None, (
        f"risk_manager is on {model} with no measurement on "
        f"risk_drawdown_discipline — the seat would be unevidenced for the "
        f"two rules it is the only check on"
    )
    assert pair["quality_min"] == 1.0, (
        f"risk_manager on {model} scored quality_min={pair['quality_min']} on "
        f"risk_drawdown_discipline"
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
