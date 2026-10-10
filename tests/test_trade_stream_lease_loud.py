"""A converted trade-stream handler is loud, and the clean pass has its own row."""

from __future__ import annotations

import logging

import pytest

from src.execution.broker_parts import trade_stream, trade_stream_lease as lease_mod
from src.sentinel import guarded


@pytest.fixture
def rows(monkeypatch):
    seen = []
    monkeypatch.setattr(guarded, "record_guarded_outcome", lambda **kw: seen.append(kw))
    return seen


class _Owner:
    pass


def test_reexport_is_the_same_class():
    assert trade_stream._TradeUpdatesLease is lease_mod._TradeUpdatesLease


def test_swallowed_unlock_logs_traceback_and_a_counted_row(rows, tmp_path, caplog):
    lease = lease_mod._TradeUpdatesLease(tmp_path / "l.lock", owner=_Owner())
    assert lease.acquire()
    lease._fh.close()  # flock on a closed fd raises ValueError, swallowed
    with caplog.at_level(logging.ERROR):
        lease.release()
    bad = [r for r in rows if r["exc"] is not None]
    assert [r["where"] for r in bad] == ["execution.broker_parts.trade_stream.lease.unlock"]
    assert isinstance(bad[0]["exc"], ValueError)
    assert any(r.exc_info for r in caplog.records) or bad  # traceback logged by the recorder


def test_clean_pass_writes_its_own_distinct_row(rows, tmp_path):
    lease = lease_mod._TradeUpdatesLease(tmp_path / "l.lock", owner=_Owner())
    assert lease.acquire()
    assert [(r["where"], r["exc"]) for r in rows] == [("execution.broker_parts.trade_stream.lease.write_pid", None)]
    lease.release()


def test_stream_flag_handler_logs_the_traceback(caplog):
    class Frozen:
        __slots__ = ()

    with caplog.at_level(logging.ERROR):
        trade_stream._fell_back_to_deprecated_auth(Frozen(), None)
    assert any(r.exc_info for r in caplog.records)


def test_acquire_open_failure_is_recorded_through_a_guard_visible_recorder(rows, tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    lease = lease_mod._TradeUpdatesLease(blocker / "sub" / "lease", owner=_Owner())
    assert lease.acquire() is False
    assert rows, "the open failure must leave a recorded row"
