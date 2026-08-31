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
    "smart_money_analyst",
    "portfolio_manager", "risk_manager", "position_reviewer",
    "evening_analyst", "meta_reflector",
)

# The seats whose output is a trading decision or the gate over one — where
# a quality regression reaches the broker. They are held to a higher bar than
# the specialist seats, but the bar is MEASURED QUALITY AT THAT SEAT, not
# price (see test_decision_seats_run_a_model_measured_at_that_seat).
DECISION_SEATS = ("portfolio_manager", "risk_manager", "position_reviewer")

# 2026-08-31 owner decision (docs/WORK.md "NEXT UP"): the primary moved to
# Gemini direct for eight specialist/review seats; PM and RM were
# deliberately left on their existing (measured, decision-chain-independent)
# models and stayed on OpenRouter. GOOGLE_SEATS | OPENROUTER_SEATS == AGENTS.
GOOGLE_SEATS = (
    "tech_analyst", "news_analyst", "macro_analyst", "earnings_analyst",
    "smart_money_analyst", "evening_analyst", "meta_reflector",
)
# position_reviewer is HELD BACK from the Gemini-direct migration on purpose.
# It is a DECISION SEAT, and the gate directly below
# (test_decision_seats_run_a_model_measured_at_that_seat) requires a model
# measured at that seat. No benchmark exists for gemini-3.5-flash-lite at the
# midday_exit scenario, and one cannot be produced against the 2.5 incumbent
# because Google refuses 2.5 to new keys. Moving it would mean asserting
# unmeasured quality on a seat that decides whether to exit a live position,
# so it stays on OpenRouter until the benchmark is run and committed.
OPENROUTER_SEATS = ("portfolio_manager", "risk_manager", "position_reviewer")

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
    session. Every configured model resolves from the in-process table
    offline — via `_PRICING_OPENROUTER` for the OpenRouter-routed seats (PM,
    RM) or `_PRICING_PINNED` for the Google-direct seats (2026-08-31:
    `gemini-3.5-flash-lite`, the AI Studio free tier) — so a session run
    with no route to any pricing catalog still logs real cost instead of
    "$?.??". `estimate_cost` is provider-agnostic by design (it keys purely
    on the model id), which is exactly what makes this assertion meaningful
    without re-deriving which pricing dict backs which provider."""
    for agent in AGENTS:
        model = llm[f"{agent}_model"]
        assert estimate_cost(model, 1000, 1000) is not None, f"{agent}: {model} not pinned"


def test_policy_is_cheaper_than_the_commissioning_baseline(llm):
    """The point of the tranche. Priced on a fixed synthetic workload so the
    assertion is about the POLICY, not about how chatty a model happened to
    be on one run.

    Uses `estimate_cost` (provider-agnostic) rather than reaching into
    `_PRICING_OPENROUTER` directly — after 2026-08-31 most seats route
    through Google-direct (`_PRICING_PINNED`, genuinely $0.00 on the AI
    Studio free tier), not OpenRouter, so a direct dict lookup would KeyError
    on them. The free primary makes this comparison MORE lopsided than it
    was when every seat paid OpenRouter's rate, which is the correct
    direction, not a weakened assertion."""
    baseline_rate = _PRICING_OPENROUTER[BASELINE_MODEL]
    per_call_in, per_call_out = 40_000, 3_000

    def sweep(model_for_agent) -> float:
        return sum(
            estimate_cost(model_for_agent(a), per_call_in, per_call_out)
            for a in AGENTS
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


def test_pm_seat_is_measured_at_production_scale(llm):
    model = llm["portfolio_manager_model"]
    pair = _benchmark_pairs().get(f"{model}|pm_production_scale")
    assert pair is not None, f"{model} lacks the production-scale PM regression"
    assert pair["runs"] >= 2
    assert pair["quality_min"] == 1.0
    assert not pair["errors"]


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
    """Every pinned rate is either in the policy, is the baseline the cost
    reduction is measured against, or is the process-wide cross-provider
    failover target (2026-08-31: `llm.fallback_model`, reachable from any
    seat whose primary differs from it — which after the same-day Google
    migration is every seat except PM/RM's specific model, since the
    OpenRouter-routed seats among AGENTS are now only PM/RM themselves).
    Rows for models nothing uses go stale unnoticed and make
    `verify_pricing.py` noisy."""
    llm = (yaml.safe_load(SETTINGS.read_text()) or {}).get("llm") or {}
    used = {llm[f"{a}_model"] for a in AGENTS} | {BASELINE_MODEL}
    fallback_provider = llm.get("fallback_provider")
    fallback_model = llm.get("fallback_model")
    if fallback_provider == "openrouter" and fallback_model:
        used.add(fallback_model)
    unused = set(_PRICING_OPENROUTER) - used
    assert not unused, f"pinned but unused: {sorted(unused)}"


# --- failure behaviour the policy depends on ------------------------------


def test_openrouter_seat_fails_closed_rather_than_substituting_a_model():
    """An agent instance built with NO fallback credential at all (as
    opposed to the production pipeline.py wiring, which always passes one —
    see llm.fallback_provider/fallback_model, 2026-08-31) must fail closed
    rather than substitute an unrecorded model: `_fallback_api_key` empty
    means `_failover_reachable` is False regardless of what the process-wide
    fallback target is configured to, so an exhausted OpenRouter call
    re-raises. What must never happen is a session quietly completing on a
    model the policy did not select for that seat — that is the "unrecorded
    model choice" the authorization forbids, and it would also mis-price the
    call.
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


def test_every_seat_resolves_to_its_accepted_provider(llm):
    """A seat that resolved to any other provider would be reaching for a
    credential OneCLI does not hold, and would fail on its first call.

    Before 2026-08-31 this asserted a single provider ('openrouter') for
    every seat. The primary migration split that: eight specialist/review
    seats now resolve to 'google' (Google AI Studio direct, free tier); PM
    and RM were deliberately left on OpenRouter (see GOOGLE_SEATS /
    OPENROUTER_SEATS above and docs/WORK.md)."""
    assert set(GOOGLE_SEATS) | set(OPENROUTER_SEATS) == set(AGENTS)
    for agent in GOOGLE_SEATS:
        assert resolve_provider(
            llm[f"{agent}_model"], llm[f"{agent}_provider"]
        ) == "google", agent
    for agent in OPENROUTER_SEATS:
        assert resolve_provider(
            llm[f"{agent}_model"], llm[f"{agent}_provider"]
        ) == "openrouter", agent
