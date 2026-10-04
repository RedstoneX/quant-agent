"""Broker-read catch-alls are loud: fault, clean pass, never-reached."""
import logging

from unittest import mock

from src.api import broker_reads
from src.api.broker_reads_record import record_read_fault, record_read_pass

_REC = "src.api.broker_reads_record.record_guarded_outcome"


def test_swallowed_fault_logs_traceback_and_keeps_shape(caplog):
    with mock.patch.object(broker_reads, "_get_broker", side_effect=RuntimeError("boom")):
        with caplog.at_level(logging.ERROR, logger="src.api.broker_reads"):
            out = broker_reads.read_account()
    assert out["cash"] is None and out["error"] == "boom"
    recs = [r for r in caplog.records if r.exc_info]
    assert recs and recs[0].exc_info[0] is RuntimeError


def test_fault_row_is_disagreed_and_clean_pass_is_agreed():
    with mock.patch(_REC) as rec:
        record_read_fault("x", ValueError("v"), n=1)
        record_read_pass("x")
    bad, ok = rec.call_args_list
    assert bad.kwargs["exc"].args == ("v",) and bad.kwargs["where"] == "api.broker_reads.x"
    assert bad.kwargs["db"] is None and bad.kwargs["context"] == {"n": 1}
    assert ok.kwargs["exc"] is None


def test_unreached_site_writes_nothing_and_observer_never_raises():
    with mock.patch(_REC) as rec:
        broker_reads.check_broker_reachable  # imported, never called
    assert rec.call_count == 0
    with mock.patch(_REC, side_effect=RuntimeError("observer")):
        record_read_fault("y", ValueError("v"))  # must not raise
