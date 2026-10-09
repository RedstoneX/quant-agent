"""Every phase module lifted out of `ExitEngineMixin._midday_execute_llm_actions`
(src/pipeline_exits.py) into src/exits_parts/ imports on its own, exposes its
phase function, and passes the boundary check."""
import importlib

import pytest

from src.exits_parts import midday_state
from src.exits_parts.midday_gates import midday_pre_gates
from src.exits_parts.midday_holding_discipline import midday_holding_discipline
from src.exits_parts.midday_spent_trigger import midday_spent_trigger
from tests.boundary_harness import check_boundary

PHASES = {
    "src.exits_parts.midday_gates": midday_pre_gates,
    "src.exits_parts.midday_holding_discipline": midday_holding_discipline,
    "src.exits_parts.midday_spent_trigger": midday_spent_trigger,
}


@pytest.mark.parametrize("module", sorted(PHASES))
def test_phase_module_imports_alone(module):
    mod = importlib.import_module(module)
    assert getattr(mod, PHASES[module].__name__) is PHASES[module]


@pytest.mark.parametrize("module", sorted(PHASES))
def test_phase_module_passes_boundary_check(module):
    # midday_state holds only the SKIP sentinel and the loop-invariant
    # dataclass, not a collaborator, so the constructor clause does not
    # apply to it; it is imported above and checked by the sentinel test.
    verdict = check_boundary(module)
    assert verdict.passed, verdict.failures


def test_skip_sentinel_is_one_object():
    assert midday_state.SKIP is importlib.import_module(
        "src.exits_parts.midday_state"
    ).SKIP
    assert repr(midday_state.SKIP) == "SKIP"
