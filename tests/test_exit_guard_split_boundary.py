"""Boundary witness for the exit_guard lifts: each new module is imported and
exercised directly, never through src.risk.exit_guard."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from boundary_harness import check_boundary  # noqa: E402

from src.risk.exit_guard_deltas import _finite, is_deterioration_claim
from src.risk.exit_guard_thesis import check_thesis_invalid_if
from src.risk.exit_guard_thesis_macro import _clean_number
from src.risk.exit_guard_trend import REGIME_DEFAULT, _break_confirmation_settings

MODULES = [
    "src.risk.exit_guard_deltas",
    "src.risk.exit_guard_trend",
    "src.risk.exit_guard_thesis",
    "src.risk.exit_guard_thesis_macro",
]


# The harness's clause 1 demands a hand-written __init__, which a dataclass
# generates; every other clause must still hold for the modules holding one.
DATACLASS_MODULES = {
    "src.risk.exit_guard_deltas": "MetricDeltas",
    "src.risk.exit_guard_thesis": "ThesisInvalidationCheck",
}


@pytest.mark.parametrize("module", MODULES)
def test_module_passes_the_boundary_harness(module):
    v = check_boundary(module)
    if module in DATACLASS_MODULES:
        assert set(v.failures) <= {1}, v.failures
        assert v.failures.get(1, []) == [f"{DATACLASS_MODULES[module]}: no __init__"]
    else:
        assert v.passed, v.failures


def test_deltas_pure_helpers():
    assert _finite(float("nan")) is None
    assert _finite(2) == 2.0
    assert is_deterioration_claim("") is False


def test_trend_default_regime_needs_two_closes():
    assert _break_confirmation_settings(REGIME_DEFAULT)[0] == 2


def test_thesis_and_macro_helpers():
    assert _clean_number("1,234.5") == 1234.5
    assert check_thesis_invalid_if("closes below the $100 level", current_price=99.0).status == "TRIGGERED"
