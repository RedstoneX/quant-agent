"""Boundary witness: the lifted entry-protection and stop-cancel pieces pass the repo boundary check.

Both are function-only modules taking the broker first, so clauses 1-2 are
vacuous and clause 5 is satisfied by tests/test_broker_split_direct.py.
"""

from __future__ import annotations

from tests.boundary_harness import check_boundary


def test_entry_protection_module_passes_the_boundary_check():
    verdict = check_boundary("src.execution.broker_parts.entry_protection")
    assert verdict.passed, verdict.failures


def test_stop_cancel_module_passes_the_boundary_check():
    verdict = check_boundary("src.execution.broker_parts.stop_cancel")
    assert verdict.passed, verdict.failures
