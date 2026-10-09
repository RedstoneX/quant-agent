"""Protection catch-alls are loud: fault, clean pass, never-reached, no handle."""

import logging
from unittest import mock

from src import pipeline_protection
from src.pipeline_protection import ExDividends
from src.pipeline_protection_record import record_protection_fault

_REC = "src.pipeline_protection_record.record_guarded_outcome"


class _Owner:
    db = object()


def test_swallowed_fault_row_is_disagreed_with_owner_handle():
    owner = _Owner()
    with mock.patch(_REC) as rec:
        record_protection_fault(owner, "x", ValueError("v"), symbol="AAA")
    kw = rec.call_args.kwargs
    assert kw["db"] is owner.db and kw["where"] == "pipeline_protection.x"
    assert kw["exc"].args == ("v",) and kw["context"] == {"symbol": "AAA"}


def test_clean_pass_writes_its_own_agreed_row():
    with mock.patch(_REC) as rec:
        record_protection_fault(_Owner(), "x")
    assert rec.call_args.kwargs["exc"] is None


def test_missing_handle_passes_none_and_still_logs_traceback(caplog):
    with caplog.at_level(logging.ERROR, logger="src.pipeline_protection"):
        record_protection_fault(object(), "y", RuntimeError("boom"))
    recs = [r for r in caplog.records if r.exc_info]
    assert recs and recs[0].exc_info[0] is RuntimeError


def test_unreached_site_writes_nothing_and_observer_never_raises():
    with mock.patch(_REC) as rec:
        assert pipeline_protection.ExitRelief.__module__.endswith("exit_relief")  # imported, never run
    assert rec.call_count == 0
    with mock.patch(_REC, side_effect=RuntimeError("observer")):
        record_protection_fault(_Owner(), "z", ValueError("v"))  # must not raise


def test_exdiv_fetch_fault_is_swallowed_and_counted():
    pos = mock.Mock(symbol="AAA", qty=1)
    market = mock.Mock()
    market.get_upcoming_ex_dividend.side_effect = RuntimeError("net")
    broker = mock.Mock()
    broker.is_trading_day.return_value = True
    db = mock.Mock()
    db.get_trades.return_value = []
    ex = ExDividends(broker=broker, db=db, market=market)
    with mock.patch(_REC) as rec:
        ex._handle_ex_dividends([pos], "r1")
    assert any(c.kwargs["where"].startswith("pipeline_protection.exdiv") for c in rec.call_args_list)
