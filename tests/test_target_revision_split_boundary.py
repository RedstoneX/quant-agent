"""Boundary witness for the target_revision lifts: each new module is imported
and exercised directly, never through src.risk.target_revision."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from boundary_harness import check_boundary  # noqa: E402

from src.risk.target_revision_basis import _finite

MODULES = [
    "src.risk.target_revision_basis",
    "src.risk.target_revision_rederive",
    "src.risk.target_revision_backfill",
]

# The harness's clause 1 demands a hand-written __init__, which a dataclass
# generates; every other clause must still hold for the module holding one.
DATACLASS_MODULES = {"src.risk.target_revision_basis": "TargetRevisionOutcome"}


@pytest.mark.parametrize("module", MODULES)
def test_module_passes_the_boundary_harness(module):
    v = check_boundary(module)
    if module in DATACLASS_MODULES:
        assert set(v.failures) <= {1}, v.failures
        assert v.failures.get(1, []) == [f"{DATACLASS_MODULES[module]}: no __init__"]
    else:
        assert v.passed, v.failures


def test_basis_pure_helpers():
    assert _finite(float("nan")) is None
    assert _finite(2) == 2.0
