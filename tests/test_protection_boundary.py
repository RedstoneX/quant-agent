"""Boundary witnesses: the lifted protection pieces build and run with no pipeline behind them.

Every collaborator is an explicit keyword-only constructor argument, so each
class is built from stubs alone (clause 5 of tests/boundary_harness.py).
"""

from __future__ import annotations

import inspect
from unittest.mock import MagicMock

import pytest

from src.protection.coverage_election import CoverageElection, _position_notional, _price_is_through_stop
from src.protection.fill_reconciler import FillReconciler, _finite_float_or_none, _reconciled_exit_action
from src.protection.owner_alerts import OwnerAlerts
from src.protection.repeg_drain import RepegDrain
from src.protection.sell_finalization import _WAL_SELL_SENTINEL, SellFinalization
from tests.boundary_harness import check_boundary


def _build(cls, **overrides):
    params = inspect.signature(cls).parameters
    kwargs = {name: MagicMock(name=name) for name in params}
    kwargs.update(overrides)
    return cls(**kwargs)


LIFTED = [OwnerAlerts, SellFinalization, FillReconciler, RepegDrain, CoverageElection]


@pytest.mark.parametrize("cls", LIFTED)
def test_every_lifted_piece_is_constructible_from_stubs(cls):
    _build(cls)
    params = inspect.signature(cls).parameters
    assert all(p.kind is inspect.Parameter.KEYWORD_ONLY for p in params.values())


@pytest.mark.parametrize(
    "module",
    [
        "src.protection.owner_alerts",
        "src.protection.sell_finalization",
        "src.protection.fill_reconciler",
        "src.protection.repeg_drain",
        "src.protection.coverage_election",
    ],
)
def test_every_lifted_module_passes_the_boundary_check(module):
    verdict = check_boundary(module)
    assert verdict.passed, verdict.failures


def test_moved_helpers_still_resolve_on_the_mixin_module():
    import src.pipeline_protection as mixin_module

    assert mixin_module._WAL_SELL_SENTINEL is _WAL_SELL_SENTINEL
    assert mixin_module._finite_float_or_none is _finite_float_or_none
    assert mixin_module._reconciled_exit_action is _reconciled_exit_action
    assert mixin_module._position_notional is _position_notional
    assert mixin_module._price_is_through_stop is _price_is_through_stop


def test_finite_float_or_none_rejects_non_numbers():
    assert _finite_float_or_none(True) is None
    assert _finite_float_or_none(float("nan")) is None
    assert _finite_float_or_none(MagicMock()) is None
    assert _finite_float_or_none(3) == 3.0


def test_sell_finalization_calls_collaborators_not_itself():
    core = MagicMock(name="finalize_protection_after_sell_core")
    sf = _build(SellFinalization, finalize_protection_after_sell_core=core)
    assert sf._finalize_protection_after_sell_core is core
