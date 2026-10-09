"""Board item 187: the per-open FRED coverage record keeps attempted-and-failed
apart from never-attempted, and never turns a missing report into a zero."""

import json

from src.data.event_calendar import EventCalendarCoverage, ReleaseFailure
from src.data.fetch_coverage_record import build_row
from src.data.macro import NOT_ATTEMPTED_REASON, MacroCoverage, SeriesFailure
from src.storage.db import Database


def test_attempted_failure_and_never_attempted_are_separate():
    cov = MacroCoverage(3, 1, [SeriesFailure("AAA", "read_timeout"), SeriesFailure("BBB", NOT_ATTEMPTED_REASON)])
    row = build_row("r1", cov, None)
    assert json.loads(row["series_failed"]) == [{"series": "AAA", "reason": "read_timeout"}]
    assert json.loads(row["series_not_attempted"]) == ["BBB"]
    assert row["releases_configured"] is None  # unreported, not zero
    assert row["full_coverage"] == 0


def test_full_coverage_needs_both_halves():
    macro = MacroCoverage(2, 2, [])
    events = EventCalendarCoverage(7, 7, [], from_cache=[("CPI", 1)] * 7)
    row = build_row("r2", macro, events)
    assert (row["full_coverage"], row["releases_from_cache"]) == (1, 7)
    assert build_row("r3", macro, None)["full_coverage"] == 0
    bad = EventCalendarCoverage(7, 6, [ReleaseFailure(1, "CPI", "fetch_deadline_exceeded")])
    assert build_row("r4", macro, bad)["full_coverage"] == 0


def test_row_round_trips_through_the_table(tmp_path):
    db = Database(str(tmp_path / "t.db"))
    db.initialize()
    db.insert_fred_fetch_coverage_run(build_row("r5", MacroCoverage(2, 2, []), None))
    got = db.conn.execute("SELECT run_id, series_succeeded, full_coverage FROM fred_fetch_coverage_runs").fetchall()
    assert [tuple(r) for r in got] == [("r5", 2, 0)]
