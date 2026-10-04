"""Boundary witness for src/entry_record.py.

The pending-entry row writer was lifted out of ``ExecutionStage._run_session``.
The project's boundary test is: can the piece be CONSTRUCTED and EXERCISED from
stubs, without building the pipeline that used to own it?  Nothing here names
the pipeline; the database is a MagicMock.

Second witness: the three fields a settlement recording pins at entry
(``entry_atr``, ``stop_basis``, ``requested_risk_pct``) are read LOUDLY -- a
decision object missing one of them raises at the accessor instead of quietly
handing ``insert_trade`` a NULL.
"""
from __future__ import annotations

import inspect
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src.entry_record import insert_pending_entry
from tests.boundary_harness import check_boundary

MODULE = "src.entry_record"


def _decision(**overrides):
    fields = dict(
        symbol="ABCD", action="BUY", reasoning="r", take_profit=None,
        setup_type="breakout", structural_ceiling=True, stop_rule="atr",
        stop_level_basis=None, conviction="HIGH", requested_risk_pct=0.5,
        allocated_risk_pct=0.4, thesis_invalid_if="x",
    )
    fields.update(overrides)
    return SimpleNamespace(**fields)


def _call(db, decision, add_prep=None, is_short=False):
    return insert_pending_entry(
        db=db, decision=decision, add_prep=add_prep, is_short=is_short,
        qty=3, executed_price=10.0, run_id="run", stop_price=9.0,
        decision_id=7, entry_analysis=SimpleNamespace(atr_14=0.3, expected_horizon_sessions=5),
        decision_model="model",
    )


def test_module_passes_the_boundary_clauses():
    verdict = check_boundary(MODULE)
    assert not verdict.failures, verdict.failures


def test_every_collaborator_is_keyword_only():
    params = inspect.signature(insert_pending_entry).parameters
    assert all(p.kind is inspect.Parameter.KEYWORD_ONLY for p in params.values())


def test_fresh_entry_is_exercised_from_a_stub_db():
    db = MagicMock()
    db.insert_trade.return_value = 41
    row_id, side = _call(db, _decision())
    assert (row_id, side) == (41, "buy")
    kw = db.insert_trade.call_args.kwargs
    assert kw["fill_status"] == "pending_submit"
    assert kw["entry_atr"] == 0.3
    assert kw["stop_basis"] == "atr"
    assert kw["requested_risk_pct"] == 0.5
    assert kw["setup_type"] == "breakout" and kw["structural_ceiling"] is True
    db.get_symbol_last_buy.assert_not_called()


def test_scale_in_carries_the_pinned_row_forward_and_shorts_read_short_rows():
    db = MagicMock()
    db.get_symbol_last_buy.return_value = {"setup_type": "pullback", "structural_ceiling": 0}
    add_prep = SimpleNamespace(is_scale_in=True)
    _, side = _call(db, _decision(action="SHORT"), add_prep=add_prep, is_short=True)
    assert side == "sell_short"
    assert db.get_symbol_last_buy.call_args.kwargs["action"] == "SHORT"
    kw = db.insert_trade.call_args.kwargs
    assert kw["setup_type"] == "pullback" and kw["structural_ceiling"] is False


@pytest.mark.parametrize("missing", ["requested_risk_pct", "stop_rule"])
def test_a_missing_pinned_field_fails_at_the_accessor_not_as_null(missing):
    db = MagicMock()
    decision = _decision()
    delattr(decision, missing)
    with pytest.raises(AttributeError, match=missing):
        _call(db, decision)
    db.insert_trade.assert_not_called()


def test_a_missing_entry_analysis_records_no_atr_rather_than_a_reconstructed_one():
    db = MagicMock()
    insert_pending_entry(
        db=db, decision=_decision(), add_prep=None, is_short=False, qty=1,
        executed_price=1.0, run_id="run", stop_price=0.9, decision_id=1,
        entry_analysis=None, decision_model="m",
    )
    kw = db.insert_trade.call_args.kwargs
    assert kw["entry_atr"] is None and kw["expected_horizon_sessions"] is None
