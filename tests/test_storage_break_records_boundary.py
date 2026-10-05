"""The break and target-revision stores are STANDALONE.

Constructed from plain collaborators — an sqlite connection this test
opened and a lock this test made — with `src.storage.db` imported nowhere
in this file. If either store ever reaches back into `Database`, this file
stops importing.
"""

import json
import sqlite3
import sys
import threading

from src.storage.break_records import BreakRecords, build_break_records
from src.storage.target_revisions import (
    TargetRevisionRecords, build_target_revision_records,
)
from tests.boundary_harness import check_boundary

SCHEMA = """
CREATE TABLE specialist_evidence (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    decision_id TEXT,
    agent_name TEXT NOT NULL,
    kind TEXT NOT NULL,
    scope TEXT NOT NULL,
    symbol TEXT,
    evidence_json TEXT NOT NULL,
    timestamp TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


def _conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def test_builders_hand_back_the_collaborators_given():
    conn, lock = _conn(), threading.Lock()
    breaks = build_break_records(conn=conn, lock=lock)
    revs = build_target_revision_records(conn=conn, lock=lock)
    assert isinstance(breaks, BreakRecords)
    assert isinstance(revs, TargetRevisionRecords)
    for store in (breaks, revs):
        assert store.conn is conn
        assert store._lock is lock


def test_neither_store_imports_the_database_module():
    """The boundary is real: neither module pulls `src.storage.db` in."""
    for name in ("src.storage.break_records", "src.storage.target_revisions",
                 "src.storage.locked_write"):
        mod = sys.modules[name]
        src = open(mod.__file__).read()
        for line in src.splitlines():
            stripped = line.strip()
            if stripped.startswith(("import ", "from ")):
                assert "storage.db" not in stripped, (name, line)
                assert "Database" not in stripped, (name, line)


def test_both_stores_pass_the_boundary_harness():
    for module in (
        "src.storage.break_records",
        "src.storage.target_revisions",
    ):
        verdict = check_boundary(module)
        assert not verdict.failures, (module, verdict.failures)


def test_holding_protection_break_round_trips_cross_day():
    breaks = build_break_records(conn=_conn(), lock=threading.Lock())
    breaks.save_holding_protection_break(
        run_id="r1", symbol="aaa", raw_broken=True, bar_date="2026-09-29",
        close=12.5, basis="atr", detail="rule=x",
    )
    assert breaks.get_prior_holding_protection_break(
        ["AAA"], today_bar_date="2026-09-30") == {"AAA": True}
    # Today's own close never confirms itself.
    assert breaks.get_prior_holding_protection_break(
        ["AAA"], today_bar_date="2026-09-29") == {}
    recent = breaks.get_recent_holding_protection_breaks(
        "AAA", before_bar_date="2026-09-30")
    assert recent == [{"bar_date": "2026-09-29", "raw_broken": True, "close": 12.5}]


def test_delever_ceiling_state_reads_back_the_last_write():
    breaks = build_break_records(conn=_conn(), lock=threading.Lock())
    assert breaks.get_last_delever_over_ceiling() is None
    breaks.save_delever_ceiling_state(run_id="r1", over_ceiling=True)
    assert breaks.get_last_delever_over_ceiling() is True
    assert breaks.get_last_delever_over_ceiling(exclude_run_id="r1") is None


def test_target_level_break_carries_three_flags_in_one_row():
    breaks = build_break_records(conn=_conn(), lock=threading.Lock())
    breaks.save_target_level_break(
        run_id="r1", symbol="AAA", raw_broken=True, bar_date="2026-09-29",
        raw_reach=False, raw_wall=None,
    )
    for flag, want in (("raw_broken", {"AAA": True}),
                       ("raw_reach", {"AAA": False}),
                       ("raw_wall", {})):
        assert breaks.get_prior_target_level_break(
            ["AAA"], today_bar_date="2026-09-30", flag=flag) == want


def test_target_revision_records_every_outcome():
    conn = _conn()
    revs = build_target_revision_records(conn=conn, lock=threading.Lock())
    row_id = revs.record_target_revision(
        run_id="r1", symbol="aaa", code="REFUSAL_X", seat="risk",
        evidence="e", applied=False,
    )
    assert row_id > 0
    out = revs.get_target_revisions(["AAA"])
    assert out["AAA"][0]["code"] == "REFUSAL_X"
    assert out["AAA"][0]["applied"] is False
    stored = conn.execute(
        "SELECT kind, evidence_json FROM specialist_evidence").fetchone()
    assert stored["kind"] == "target_revision"
    assert json.loads(stored["evidence_json"])["seat"] == "risk"


def test_the_confirmation_is_keyed_on_the_close_not_on_the_last_row():
    """DEFECT 2. Several intraday cycles can re-read one close; whichever
    ran last used to decide the flag. The reading is now the earliest row
    recorded for the latest prior bar date, and a row that does not answer
    the question is skipped rather than read as False."""
    breaks = build_break_records(conn=_conn(), lock=threading.Lock())
    # Two cycles re-read the SAME prior close and disagree. The first
    # reading of that close wins, whichever ran last.
    breaks.save_target_level_break(
        run_id="r1", symbol="TEST", bar_date="2026-09-29",
        raw_broken=True, raw_wall=True,
    )
    breaks.save_target_level_break(
        run_id="r2", symbol="TEST", bar_date="2026-09-29",
        raw_broken=False, raw_wall=False,
    )
    for flag in ("raw_broken", "raw_wall"):
        got = breaks.get_prior_target_level_break(
            ["TEST"], today_bar_date="2026-09-30", flag=flag,
        )
        assert got.get("TEST") is True, flag
    # A degraded later cycle that could not answer the wall question must
    # not erase the answer already given for that close.
    breaks.save_target_level_break(
        run_id="r3", symbol="TEST", bar_date="2026-09-29",
        raw_broken=False, raw_wall=None,
    )
    assert breaks.get_prior_target_level_break(
        ["TEST"], today_bar_date="2026-09-30", flag="raw_wall",
    ).get("TEST") is True
