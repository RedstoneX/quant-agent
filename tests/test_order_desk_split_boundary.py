"""Boundary witness for the order_desk lift: the new module is checked and
exercised directly, never through OrderDesk or the pipeline."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from boundary_harness import check_boundary  # noqa: E402


def test_order_desk_reads_passes_the_boundary_harness():
    v = check_boundary("src.execution.broker_parts.order_desk_reads")
    assert v.passed, v.failures
