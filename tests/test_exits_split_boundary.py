"""Boundary witnesses: the two sell-side bodies lifted out of
`ExitEngineMixin` (src/pipeline_exits.py) into src/exits_parts/ build and
import alone, and the mixin keeps same-signature shims for both."""

import pytest

from tests.boundary_harness import check_boundary

PARTS = [
    "src.exits_parts.trails",
    "src.exits_parts.risk_review",
]


@pytest.mark.parametrize("module", PARTS)
def test_part_passes_boundary_check(module):
    verdict = check_boundary(module)
    assert verdict.passed, verdict.failures


def test_mixin_keeps_the_two_methods_as_shims():
    from src.pipeline_exits import ExitEngineMixin

    assert callable(ExitEngineMixin._apply_deterministic_trails)
    assert callable(ExitEngineMixin._risk_review_exits)
