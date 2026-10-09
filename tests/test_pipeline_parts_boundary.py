"""Boundary witness: every lifted pipeline part passes the repo boundary check."""
from __future__ import annotations

import pytest

from tests.boundary_harness import check_boundary

# Clause 5 is satisfied by tests/test_pipeline_parts_direct.py, which imports
# each module on its own with no TradingPipeline behind it.


@pytest.mark.parametrize("name", ["evening", "review", "morning_helpers", "pnl_gaps", "kill_repair"])
def test_pipeline_part_passes_the_boundary_check(name):
    verdict = check_boundary(f"src.pipeline_parts.{name}")
    assert verdict.passed, verdict.failures
