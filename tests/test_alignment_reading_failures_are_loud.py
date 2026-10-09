"""A swallowed alignment-exit recording must not look like "nothing happened".

`alignment_exit_readings` was empty in production for sessions that
demonstrably ran the scan. Two broad catch-alls could have eaten it — the
per-position one in `_alignment_exit_scan` and the one around the write in
`_record_alignment_reading` — and both downgraded the failure to a log line
that is not stored in the database, so the data carried no trace of either.

These tests pin the two halves of the remedy: a position the scan fails on
now leaves an explicit not-evaluated ROW naming the error, and a write that
is refused outright is logged with its traceback so the layer that refused
it is named.
"""

from __future__ import annotations

import logging

from src.exits.alignment_exit import AlignmentExit


class _Position:
    def __init__(self, symbol: str, qty: float) -> None:
        self.symbol = symbol
        self.qty = qty
        self.thesis_invalid_if = None
        self.avg_entry = 10.0
        self.stop_loss = 9.0


class _RecordingDb:
    def __init__(self) -> None:
        self.readings: list[dict] = []

    def record_alignment_exit_reading(self, **kwargs) -> None:
        self.readings.append(kwargs)


class _RefusingDb:
    def record_alignment_exit_reading(self, **kwargs) -> None:
        raise RuntimeError("no such table: alignment_exit_readings")


def _scan(db, *, cached):
    return AlignmentExit(
        alignment_exit_cached=cached,
        structural_protection_for_holding=lambda *a, **k: None,
        config=object(),
        db=db,
        market=object(),
    )


def test_a_position_the_scan_fails_on_is_recorded_as_not_evaluated():
    db = _RecordingDb()

    def _boom(**kwargs):
        raise ValueError("chart download refused")

    scan = _scan(db, cached=_boom)
    scan._alignment_exit_scan(
        positions=[_Position("AAA", 5.0)],
        best_by_symbol={},
        priority={"SELL": 1, "COVER": 1},
        position_facts={},
        run_id="run-1",
        displaced={},
    )

    assert len(db.readings) == 1, db.readings
    row = db.readings[0]
    assert row["symbol"] == "AAA"
    assert row["verdict"] is None
    assert row["is_short"] is False
    assert "chart download refused" in row["not_evaluated_reason"]
    assert "ValueError" in row["not_evaluated_reason"]


def test_a_short_the_scan_fails_on_keeps_its_side_on_the_row():
    db = _RecordingDb()

    def _boom(**kwargs):
        raise ValueError("chart download refused")

    _scan(db, cached=_boom)._alignment_exit_scan(
        positions=[_Position("BBB", -4.0)],
        best_by_symbol={},
        priority={"SELL": 1, "COVER": 1},
        position_facts={},
        run_id="run-1",
        displaced={},
    )

    assert db.readings[0]["is_short"] is True


def test_a_refused_write_is_logged_with_its_traceback(caplog):
    scan = _scan(_RefusingDb(), cached=lambda **k: None)
    with caplog.at_level(logging.ERROR):
        scan._record_alignment_reading(
            symbol="CCC",
            verdict=None,
            run_id="run-1",
            is_short=False,
            not_evaluated_reason="whatever",
        )
    records = [r for r in caplog.records if "CCC" in r.getMessage()]
    assert records, caplog.text
    assert records[0].levelno >= logging.ERROR
    assert records[0].exc_info is not None
    assert "no such table" in caplog.text


def test_a_refused_write_never_propagates_out_of_the_scan():
    db = _RefusingDb()

    def _boom(**kwargs):
        raise ValueError("chart download refused")

    # Both catch-alls fire at once: the scan fails on the position AND the
    # not-evaluated row cannot be written. The scan still returns.
    _scan(db, cached=_boom)._alignment_exit_scan(
        positions=[_Position("DDD", 1.0)],
        best_by_symbol={},
        priority={"SELL": 1, "COVER": 1},
        position_facts={},
        run_id="run-1",
        displaced={},
    )
