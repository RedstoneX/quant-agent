"""The model-policy benchmark harness must stay runnable.

`ops/` is not collected by pytest, and that is how this rotted: Phase 1 made
`setup_type`, `expected_horizon_sessions` and a structural level required on
`TechAnalysisResult`, the `ops/model_policy/scenarios.py` fixtures were never
updated, and the module stopped importing. Nothing failed, because nothing
imported it — so `benchmark_models.py` was dead from Phase 1 until 2026-08-27
and no one could have noticed.

That matters more than a broken script. `docs/architecture/MODEL_ROUTING_POLICY.md`
names this harness as the thing that "re-derives the whole decision from
scratch", and `tests/test_model_routing_policy.py` enforces that every
decision seat's model carries a committed `quality_min` of 1.00 at its own
scenario. Those committed results stayed valid; what was lost was the ability
to REPRODUCE them, or to qualify any new model for a seat. A policy whose
evidence cannot be regenerated is a policy that quietly becomes unfalsifiable.

These tests are deliberately cheap — an import and a schema round-trip, no
network, no LLM calls — because their whole job is to fail the moment a
`src/models.py` schema change outruns the fixtures again.
"""

from __future__ import annotations

import importlib

import pytest


def test_scenarios_module_imports():
    """Constructing the fixtures at import time is the canary.

    Every fixture is a real `src.models` object, so any newly-required field
    raises `ValidationError` here rather than silently at the next sweep —
    which is months later, costs real money, and is exactly when the harness
    needs to work.
    """
    module = importlib.import_module("ops.model_policy.scenarios")
    assert module is not None


def test_every_scenario_is_registered_and_gradeable():
    scenarios = importlib.import_module("ops.model_policy.scenarios")
    registry = getattr(scenarios, "SCENARIOS", None)
    if registry is None:
        pytest.skip("scenarios module exposes no SCENARIOS registry")
    assert registry, "the benchmark has no scenarios to run"
    for name, scenario in (
        registry.items() if isinstance(registry, dict) else enumerate(registry)
    ):
        invoke = getattr(scenario, "invoke", None)
        grade = getattr(scenario, "grade", None)
        assert callable(invoke), f"{name}: scenario has no callable invoke"
        assert callable(grade), f"{name}: scenario has no callable grade"


def test_benchmark_entrypoint_imports():
    """The runner itself, not just its fixtures."""
    assert importlib.import_module("ops.model_policy.benchmark_models") is not None


def test_pm_grading_sizes_risk_based_targets():
    """A risk-sized target must grade on its EFFECTIVE weight.

    Phase 2b (spec §2.1) made `TargetPosition.target_weight_pct` optional —
    conviction is now `risk_allocation_pct`. The PM scenarios did unguarded
    arithmetic on the notional field, so a risk-sized decision either raised
    `TypeError` or, worse, compared `None == 0` and silently mis-graded: a PM
    that correctly closed a name via `risk_allocation_pct=0` scored as having
    contradicted its own inputs. A wrong number here propagates into which
    models are qualified for decision seats.
    """
    scenarios = importlib.import_module("ops.model_policy.scenarios")
    from src.models import TargetPosition

    by_symbol = {a.symbol: a for a in scenarios._PM_ANALYSES}
    symbol = next(iter(by_symbol))
    analysis = by_symbol[symbol]
    gap = analysis.entry_price - analysis.stop_loss

    risk_sized = TargetPosition(
        symbol=symbol, risk_allocation_pct=2.0, conviction="high", thesis="t",
    )
    weight = scenarios._effective_weight_pct(risk_sized, by_symbol)
    # risk_pct x entry / (entry - stop) — the constructor's own formula.
    assert weight == pytest.approx(2.0 * analysis.entry_price / gap)

    legacy = TargetPosition(
        symbol=symbol, target_weight_pct=8.0, conviction="high", thesis="t",
    )
    assert scenarios._effective_weight_pct(legacy, by_symbol) == 8.0


def test_a_risk_based_close_is_recognised_as_a_close():
    """`risk_allocation_pct=0` is an exit. Graded as `None == 0` it reads as
    "not a close", which is how a correct exit became a scored contradiction."""
    scenarios = importlib.import_module("ops.model_policy.scenarios")
    from src.models import TargetPosition

    by_symbol = {a.symbol: a for a in scenarios._PM_ANALYSES}
    symbol = next(iter(by_symbol))
    closing = TargetPosition(
        symbol=symbol, risk_allocation_pct=0.0, conviction="low", thesis="exit",
    )
    assert closing.is_close is True
    assert scenarios._effective_weight_pct(closing, by_symbol) == 0.0


def test_an_unsizable_risk_target_is_excluded_not_scored_as_zero():
    """No stop means no honest size. Returning 0.0 would read as freed cash
    and could turn an over-committed book into a passing grade."""
    scenarios = importlib.import_module("ops.model_policy.scenarios")
    from src.models import TargetPosition

    by_symbol = {a.symbol: a for a in scenarios._PM_ANALYSES}
    orphan = TargetPosition(
        symbol="NOT_IN_FIXTURE", risk_allocation_pct=2.0,
        conviction="high", thesis="t",
    )
    assert scenarios._effective_weight_pct(orphan, by_symbol) is None
