"""The lifted entry-order catch-alls are loud: fault, clean pass, unreached,
and the one site that holds no ledger handle."""
import logging

from types import SimpleNamespace
from unittest import mock

from src import entry_orders_observed as obs

_REC = "src.sentinel.entry_guard.record_guarded_outcome"


def _pipeline(fn):
    return SimpleNamespace(db=object(), broker=SimpleNamespace(fill_stream_enabled=fn))


def test_swallowed_fault_logs_traceback_and_keeps_the_default(caplog):
    pipe = _pipeline(mock.Mock(side_effect=RuntimeError("boom")))
    with caplog.at_level(logging.ERROR, logger="src.pipeline_entry_orders"):
        assert obs._fill_stream_enabled(pipe) is True
    assert [r for r in caplog.records if r.exc_info]


def test_fault_row_is_disagreed_and_a_clean_pass_writes_its_own_row():
    with mock.patch(_REC) as rec:
        obs._fill_stream_enabled(_pipeline(mock.Mock(side_effect=RuntimeError("x"))))
        obs._fill_stream_enabled(_pipeline(mock.Mock(return_value=True)))
    bad, ok = rec.call_args_list
    assert bad.kwargs["where"] == "entry_orders.stream.enabled_flag"
    assert isinstance(bad.kwargs["exc"], RuntimeError) and bad.kwargs["db"] is not None
    assert ok.kwargs["exc"] is None


def test_a_site_never_reached_writes_nothing():
    with mock.patch(_REC) as rec:
        assert obs._fill_stream_enabled(SimpleNamespace(db=None, broker=None)) is True
    assert rec.call_count == 0


def test_no_ledger_handle_still_logs_the_traceback_and_passes_db_none(caplog):
    with mock.patch("src.notifier.send_owner_alert", side_effect=RuntimeError("down")):
        with mock.patch(_REC) as rec:
            obs._alert_unmeasurable_symbols({"AAA": {"reason": "no bars"}})
    assert rec.call_args.kwargs["db"] is None
    assert isinstance(rec.call_args.kwargs["exc"], RuntimeError)
