"""Boundary witnesses: the protection parts carved out of `ProtectionMixin` on 2026-10-04
(coverage repair, the protected sell, exit relief, the restore drain, the reprotect record,
the ex-dividend shift, and the two over-ceiling bodies kept in the shim module as standalone
classes) build and run with no pipeline behind them.

Every collaborator is an explicit keyword-only constructor argument (clause 5 of
tests/boundary_harness.py). Follows tests/test_intraday_parts_boundary.py. This file never
imports the pipeline class. This is the live stop path: a structural witness only.
"""
from __future__ import annotations

import ast
import inspect
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src.execution.broker import AlpacaBroker
from src.pipeline_protection import ReprotectResidual, StopCoverageReconciler
from src.pipeline_protection import _build_ex_dividends, _build_protected_sell
from src.protection.coverage_repair import CoverageRepair
from src.protection.ex_dividends import ExDividends
from src.protection.exit_relief import ExitRelief
from src.protection.protected_sell import ProtectedSell
from src.protection.reprotect_records import ReprotectRecords
from src.protection.restore_drain import RestoreDrain
from tests.boundary_harness import check_boundary

PARTS = [CoverageRepair, ProtectedSell, ExitRelief, RestoreDrain, ReprotectRecords, ExDividends,
         StopCoverageReconciler, ReprotectResidual]
MODULES = [f"src.protection.{m}" for m in
           ("coverage_repair", "protected_sell", "exit_relief", "restore_drain", "reprotect_records", "ex_dividends")]


def _build(cls, **overrides):
    params = inspect.signature(cls).parameters
    kwargs = {name: MagicMock(name=name) for name in params}
    kwargs.update(overrides)
    return cls(**kwargs)


class _State:
    """A stand-in for the host's get/set state view: a plain dict behind get/set."""

    def __init__(self) -> None:
        self.values: dict = {}

    def get(self, name):
        try:
            return self.values[name]
        except KeyError:
            raise AttributeError(name) from None  # the real view raises what getattr(host, name) raises

    def set(self, name, value) -> None:
        self.values[name] = value


@pytest.mark.parametrize("cls", PARTS)
def test_every_protection_part_is_constructible_from_stubs(cls):
    _build(cls)
    params = inspect.signature(cls).parameters
    assert all(p.kind is inspect.Parameter.KEYWORD_ONLY for p in params.values())


@pytest.mark.parametrize("module", MODULES)
def test_every_new_protection_module_passes_the_boundary_check(module):
    verdict = check_boundary(module)
    assert verdict.passed, verdict.failures


@pytest.mark.parametrize("cls", PARTS)
def test_every_part_reads_only_what_it_is_handed(cls):
    """Every `self.` read in a body is a constructor argument, a state-backed property or the part's own method."""
    tree = ast.parse(inspect.getsource(cls))
    reads = {n.attr for n in ast.walk(tree)
             if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id == "self"}
    handed = set(vars(_build(cls)))
    own = {n for n in dir(cls) if not n.startswith("__")}
    foreign = reads - handed - own
    assert not foreign, foreign


def test_exit_settlement_register_lands_on_the_host_state_not_the_part():
    """Exercised, not just built: the register is created on the HOST through the view and a terminal status clears it."""
    state = _State()
    part = _build(ExitRelief, state=state)
    part._register_exit_settlement({"order_id": "o1", "symbol": "aapl", "submitted_qty": -3, "terminal_status": "new"})
    assert state.values["_unsettled_exit_orders"] == {"o1": {"symbol": "AAPL", "submitted_qty": 3.0}}
    assert "_unsettled_exit_orders" not in vars(part)
    terminal = next(iter(AlpacaBroker._ORDER_TERMINAL_STATES))
    part._register_exit_settlement({"order_id": "o1", "terminal_status": terminal})
    assert state.values["_unsettled_exit_orders"] == {}


def test_stop_clear_refusal_is_written_through_the_state_view():
    state = _State()
    part = _build(ProtectedSell, state=state)
    part._last_stop_clear_refusal = {"symbol": "X"}
    assert state.values == {"_last_stop_clear_refusal": {"symbol": "X"}}
    assert "_last_stop_clear_refusal" not in vars(part)
    assert part._last_stop_clear_refusal == {"symbol": "X"}


def test_a_defaulted_getattr_on_a_missing_host_attribute_still_defaults():
    """`getattr(self, "_last_stop_clear_refusal", None)` must read the HOST, and default when the host lacks it."""
    part = _build(ProtectedSell, state=_State())
    assert getattr(part, "_last_stop_clear_refusal", "DEFAULT") == "DEFAULT"


def test_reprotect_record_runs_against_a_handed_recorder_alone():
    seen = {}
    part = _build(ReprotectRecords, record_exit_refusal=lambda **kw: seen.update(kw))
    part._record_reprotect_identity_gap("msft", "two stops rest")
    assert seen["symbol"] == "msft" and seen["code"] == "reprotect_broker_state_unprovable"
    assert seen["run_id"].startswith("reprotect-MSFT-")


def test_protected_sell_is_handed_the_cancel_or_runs_its_own_body():
    assert hasattr(ProtectedSell, "_cancel_stops_with_write_ahead")
    handed = _build(ProtectedSell, cancel_stops_with_write_ahead=lambda *a, **k: "HANDED IN", state=_State())
    assert handed._cancel_stops_with_write_ahead("X", 1.0) == "HANDED IN"
    own = _build(ProtectedSell, cancel_stops_with_write_ahead=None, state=_State())
    assert own._cancel_stops_with_write_ahead.__func__ is ProtectedSell._cancel_stops_with_write_ahead


def test_ex_dividends_and_the_reconciler_are_handed_the_repair_never_owning_it():
    for cls in (ExDividends, StopCoverageReconciler):
        assert not hasattr(cls, "_repair_stop_coverage"), cls
    assert hasattr(CoverageRepair, "_repair_stop_coverage")
    part = _build(ExDividends, repair_stop_coverage=lambda *a, **k: "HANDED IN")
    assert part._repair_stop_coverage() == "HANDED IN"


def test_the_shim_builder_reads_the_host_live_at_each_call():
    """The seam: a collaborator swapped on the host after one call is what the next call gets."""
    host = SimpleNamespace(broker=object(), db=object(), market="M1", _repair_stop_coverage=lambda: None)
    assert _build_ex_dividends(host).market == "M1"
    host.market = "M2"
    assert _build_ex_dividends(host).market == "M2"
    host2 = SimpleNamespace(broker=object(), db=object(), _alert_owner_exit_declined=None, _order_accepted=None,
                            _write_ahead_protection_restore=None, _cancel_stops_with_write_ahead=None)
    sell = _build_protected_sell(host2)
    assert getattr(sell, "_last_stop_clear_refusal", "DEFAULT") == "DEFAULT"
    sell._last_stop_clear_refusal = {"symbol": "Y"}
    assert host2._last_stop_clear_refusal == {"symbol": "Y"}
