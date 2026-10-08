"""Boundary witness for the stage_risk lift: the new module is checked directly."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from boundary_harness import check_boundary  # noqa: E402

MODULES = ["src.stage_risk_helpers"]


@pytest.mark.parametrize("module", MODULES)
def test_module_passes_the_boundary_harness(module):
    v = check_boundary(module)
    assert v.passed, v.failures
