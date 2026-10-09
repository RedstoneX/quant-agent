"""The mechanical soft-exit heal buffer: dedupe, counting, run scoping.

Both cases below FAIL on origin/main: the first because an append-per-call
buffer discards everything past the cap, the second because a module-level
global survives a run that never reaches the drain stage and the next run
files those observations under its own id.
"""

from __future__ import annotations

import sqlite3

import pytest

from src import soft_exit_restore_buffer as buf


@pytest.fixture(autouse=True)
def _fresh_buffer():
    buf.open_restore_run("test-run")
    yield
    buf.open_restore_run(None)


def _obs(symbol=None, blank=True, healed=False, source=None):
    return {
        "symbol": symbol,
        "blank_found": blank,
        "healed": healed,
        "source": source,
    }


def test_repeats_are_counted_not_dropped_and_the_cap_is_unreachable():
    """20,122 identical observations: the live shape that lost 15,122 rows."""
    generated = 20122
    for _ in range(generated):
        buf._note_restore_observation(_obs())

    rows, dropped = buf.drain_restore_observations("test-run")

    assert len(rows) == 1, "one identity must park as one row"
    assert rows[0]["occurrences"] == generated, "every repeat must be counted"
    assert dropped == 0, "nothing may be discarded in the ordinary case"
    assert sum(r["occurrences"] for r in rows) + dropped == generated


def test_distinct_identities_stay_distinct_and_a_single_is_a_recorded_one():
    buf._note_restore_observation(_obs(symbol="aaa"))
    buf._note_restore_observation(_obs(symbol="AAA"))  # same identity
    buf._note_restore_observation(_obs(symbol="BBB"))
    buf._note_restore_observation(_obs(symbol="BBB", healed=True, source="raw"))
    buf._note_restore_observation(_obs(symbol=None))

    rows, dropped = buf.drain_restore_observations("test-run")
    by_key = {(r["symbol"], bool(r["healed"])): r["occurrences"] for r in rows}

    assert dropped == 0
    assert by_key == {("aaa", False): 2, ("BBB", False): 1, ("BBB", True): 1, (None, False): 1}
    assert all(r["occurrences"] >= 1 for r in rows), "1 is a recorded count"


def test_the_cap_still_refuses_and_still_reports_what_it_refused():
    """Dedupe must not quietly remove the evidence that drops happen."""
    for i in range(buf._RESTORE_OBSERVATION_CAP + 7):
        buf._note_restore_observation(_obs(symbol=f"S{i}"))

    rows, dropped = buf.drain_restore_observations("test-run")

    assert len(rows) == buf._RESTORE_OBSERVATION_CAP
    assert dropped == 7, "dropped_before must stay honest"


def test_a_run_that_never_drains_cannot_leak_into_the_next_run():
    buf.open_restore_run("run-aaaaaaaa")
    buf._note_restore_observation(_obs(symbol="LEAK"))
    # run exits here, before the late drain stage ever runs.

    abandoned = buf.open_restore_run("run-bbbbbbbb")
    rows, dropped = buf.drain_restore_observations("run-bbbbbbbb")

    assert abandoned == 1, "the leftovers are counted, not silently gone"
    assert rows == [], "the previous run's observations are not this run's"
    assert dropped == 0
    assert buf.abandoned_restore_observations() == 1


def test_a_drain_asking_for_another_run_gets_nothing():
    buf.open_restore_run("run-cccccccc")
    buf._note_restore_observation(_obs(symbol="MINE"))

    assert buf.drain_restore_observations("run-dddddddd") == ([], 0)
    assert len(buf.drain_restore_observations("run-cccccccc")[0]) == 1


def test_seat_heal_reexports_the_same_buffer():
    from src import seat_heal

    assert seat_heal.drain_restore_observations is buf.drain_restore_observations
    assert seat_heal._note_restore_observation is buf._note_restore_observation


def test_migration_backfills_existing_rows_to_exactly_one_occurrence(tmp_path):
    """A pre-fix database: every row already there is one observation."""
    path = tmp_path / "pre_fix.db"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE soft_exit_heal_restores ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT NOT NULL,"
        " run_id TEXT, session_date TEXT, symbol TEXT, blank_found INTEGER,"
        " healed INTEGER, source TEXT, dropped_before INTEGER)"
    )
    conn.executemany(
        "INSERT INTO soft_exit_heal_restores (timestamp, blank_found, healed) VALUES (?,1,0)",
        [("2026-10-05T00:00:00Z",)] * 3,
    )
    conn.commit()
    conn.close()

    from src.storage.db import Database

    db = Database(str(path))
    db.initialize()
    counts = [
        tuple(r)
        for r in db.conn.execute("SELECT occurrences, COUNT(*) FROM soft_exit_heal_restores GROUP BY 1").fetchall()
    ]

    assert counts == [(1, 3)], "existing rows are one occurrence each"


def test_the_writer_stores_the_occurrence_count(tmp_path):
    from src.storage.db import Database

    db = Database(str(tmp_path / "fresh.db"))
    db.initialize()
    written = db.record_soft_exit_heal_restores(
        observations=[{"symbol": "ZZZ", "blank_found": 1, "healed": 0, "occurrences": 4211}],
        run_id="run-eeeeeeee",
        dropped=0,
    )

    row = tuple(db.conn.execute("SELECT occurrences, dropped_before FROM soft_exit_heal_restores").fetchone())

    assert written == 1
    assert row == (4211, 0)
