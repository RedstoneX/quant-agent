"""The prompt builders' cut now says how many candidates it chose from.

The load-bearing property is that moving the count to the site did NOT move
the cut: the same rows survive, in the same order, as before the change.
"""
from __future__ import annotations

import sqlite3
import threading

import pytest

from src.prompt_facts.decisions import PromptDecisions
from src.sentinel import counted
from src.storage.analytics.agent_log_reads import (
    count_recent_agent_outputs, recent_agent_outputs,
)
from src.storage.schema.sentinel_tables import ensure_sentinel_tables


class _Db:
    """The two things the builder touches: the connection and the lock."""

    def __init__(self, conn):
        self.conn = conn
        self._lock = threading.Lock()

    def get_recent_agent_outputs(self, agent_name, limit=5, before_date=None):
        return recent_agent_outputs(conn=self.conn, lock=self._lock,
                                    agent_name=agent_name, limit=limit,
                                    before_date=before_date)


@pytest.fixture()
def desk(tmp_path, monkeypatch):
    path = tmp_path / "quant_agent.db"
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE agent_logs (agent_name TEXT, timestamp TEXT, "
                 "full_response TEXT, output_summary TEXT)")
    for day in range(1, 10):
        conn.execute("INSERT INTO agent_logs VALUES (?, ?, ?, ?)",
                     ("position_reviewer", f"2026-09-{day:02d} 12:00:00",
                      "{}", f"day {day}"))
    conn.commit()
    ensure_sentinel_tables(conn=conn)
    monkeypatch.setattr(counted, "db_path", lambda: path)
    return _Db(conn), path


def _rows(path):
    conn = sqlite3.connect(path)
    try:
        return conn.execute("SELECT kind, agreed, detail FROM reconciliation_runs "
                            "ORDER BY id").fetchall()
    finally:
        conn.close()


def test_the_surviving_rows_are_identical_to_the_unchanged_query(desk):
    """Not just the count -- the exact surviving set and its order."""
    db, _ = desk
    facts = PromptDecisions(db=db, parse_logged_agent_response=lambda row: {})
    before = db.get_recent_agent_outputs(agent_name="position_reviewer", limit=3)
    after = facts._cut_candidates(agent_name="position_reviewer", limit=3,
                                  before_date=None, where="t.cut")
    assert after == before
    assert [r["output_summary"] for r in after] == ["day 9", "day 8", "day 7"]


def test_the_pre_cut_candidate_count_is_recorded_durably(desk):
    db, path = desk
    facts = PromptDecisions(db=db, parse_logged_agent_response=lambda row: {})
    facts._cut_candidates(agent_name="position_reviewer", limit=3,
                          before_date=None, where="prompt_facts.own.cut")
    got = _rows(path)
    assert len(got) == 1
    assert got[0][0] == "prompt_facts.own.cut" and got[0][1] == 1
    assert '"candidates_before_cut": 9' in got[0][2]
    assert '"survived": 3' in got[0][2]
    assert '"oldest_surviving": "2026-09-07"' in got[0][2]


def test_the_count_sees_every_candidate_the_limit_hid(desk):
    db, _ = desk
    assert count_recent_agent_outputs(conn=db.conn, lock=db._lock,
                                      agent_name="position_reviewer") == 9
    assert len(db.get_recent_agent_outputs(agent_name="position_reviewer",
                                           limit=3)) == 3


def test_a_count_that_fails_leaves_the_prompt_rows_untouched(desk, monkeypatch):
    """An observer must never break the builder it watches."""
    db, _ = desk
    monkeypatch.setattr("src.prompt_facts.decisions.count_recent_agent_outputs",
                        lambda **kw: (_ for _ in ()).throw(sqlite3.Error("gone")))
    facts = PromptDecisions(db=db, parse_logged_agent_response=lambda row: {})
    rows = facts._cut_candidates(agent_name="position_reviewer", limit=3,
                                 before_date=None, where="t.cut")
    assert [r["output_summary"] for r in rows] == ["day 9", "day 8", "day 7"]
