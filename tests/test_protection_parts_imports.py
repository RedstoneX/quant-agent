"""Boundary witnesses (clause 5 of tests/boundary_harness.py) for the protection parts lifted
verbatim into `src/protection_parts/` on 2026-10-09: each module imports on its own, with no
pipeline behind it, and its part builds from keyword-only stubs.
"""

from __future__ import annotations

import inspect
from unittest.mock import MagicMock

import pytest

import src.protection_parts.ex_dividends_repair
import src.protection_parts.reprotect_residual
import src.protection_parts.reprotect_scan
import src.protection_parts.restore_drain
from src.protection_parts.ex_dividends_repair import CoverageRepair, ExDividends
from src.protection_parts.reprotect_residual import ReprotectResidual
from src.protection_parts.reprotect_scan import ScanOutcome, scan_existing_stops
from src.protection_parts.restore_drain import RestoreDrain
from tests.boundary_harness import check_boundary

MODULES = [
    "src.protection_parts.ex_dividends_repair",
    "src.protection_parts.reprotect_residual",
    "src.protection_parts.reprotect_scan",
    "src.protection_parts.restore_drain",
]


@pytest.mark.parametrize("module", MODULES)
def test_every_part_module_passes_the_boundary_check(module):
    verdict = check_boundary(module)
    assert verdict.passed, verdict.failures


@pytest.mark.parametrize("cls", [CoverageRepair, ExDividends, ReprotectResidual, RestoreDrain])
def test_every_part_builds_from_keyword_only_stubs(cls):
    params = inspect.signature(cls).parameters
    assert all(p.kind is inspect.Parameter.KEYWORD_ONLY for p in params.values())
    cls(**{name: MagicMock(name=name) for name in params})


def test_the_parts_are_the_classes_the_mixin_module_builds():
    import src.pipeline_protection as host_module

    assert host_module.CoverageRepair is CoverageRepair
    assert host_module.ExDividends is ExDividends
    assert host_module.ReprotectResidual is ReprotectResidual
    assert host_module.RestoreDrain is RestoreDrain


def test_an_empty_scan_falls_through_to_the_submit():
    outcome = scan_existing_stops(
        MagicMock(),
        symbol="ZZZZ",
        residual_qty=1.0,
        existing=[],
        cancelled_ids=set(),
        identity_unprovable=False,
        best_stop=10.0,
        side="sell",
    )
    assert isinstance(outcome, ScanOutcome)
    assert outcome.done is False


def test_the_ex_dividend_clock_is_read_live_from_the_mixin_module(monkeypatch):
    import src.pipeline_protection as host_module

    part = host_module._build_ex_dividends(MagicMock(name="host"))
    sentinel = object()
    monkeypatch.setattr(host_module, "et_today", lambda: sentinel)
    assert part._today() is sentinel


def test_an_unwired_ex_dividend_part_reads_the_exchange_day_clock():
    from src.trading_calendar import et_today

    assert ExDividends()._today is et_today
