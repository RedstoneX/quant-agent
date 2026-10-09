"""Coverage-watchdog catch-alls write a counted row when a handle is in reach."""

import logging
import sqlite3
from datetime import datetime, timezone
from unittest import mock

from src import coverage_watchdog as cw
from src.coverage_watchdog_records import record_watchdog_pass

_REC = "src.coverage_watchdog_records.record_guarded_outcome"


class _Broker:
    def get_positions(self):
        raise RuntimeError("boom")


def test_swallowed_fault_logs_traceback_and_writes_disagreed_row(caplog):
    db = object()
    with mock.patch(_REC) as rec, caplog.at_level(logging.ERROR):
        gaps, err = cw.uncovered_positions(_Broker(), db=db)
    assert gaps == [] and "boom" in err
    kw = rec.call_args.kwargs
    assert kw["db"] is db and isinstance(kw["exc"], RuntimeError)
    assert kw["where"] == "coverage_watchdog.uncovered.get_positions"


def test_clean_pass_writes_agreed_row():
    with mock.patch(_REC) as rec:
        record_watchdog_pass("x", db="h")
    assert rec.call_args.kwargs["exc"] is None and rec.call_args.kwargs["db"] == "h"


def test_unreached_site_writes_nothing():
    with mock.patch(_REC) as rec:
        cw.uncovered_positions  # defined, never called
    assert rec.call_count == 0


def test_missing_handle_still_logs_and_never_raises(caplog):
    with caplog.at_level(logging.ERROR):
        out = cw.uncovered_positions(_Broker())
    assert out[0] == [] and any(r.exc_info for r in caplog.records)


def test_moved_readers_pass_the_handle(tmp_path):
    moment = datetime(2026, 1, 1, tzinfo=timezone.utc)
    with mock.patch(_REC) as rec:
        assert cw._scale_in_row_age_seconds("not-a-date", moment, "h") is None
        assert cw.measured_window_bound_seconds(str(tmp_path / "none.db"), db="h") == (None, 0)
    assert [c.kwargs["db"] for c in rec.call_args_list] == ["h", "h"]
    assert all(c.kwargs["exc"] is not None for c in rec.call_args_list)
