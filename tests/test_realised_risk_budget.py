"""Board item 186 — the realised total at-risk and the per-cluster shares of
it, one durable row per run.

WHY. `max_portfolio_risk_pct` (25) and `max_cluster_risk_share_pct` (40) are
owner-ratified appetite with no measurement behind either, and the owner's
2026-09-30 ruling ("risk is never a global dial") bars picking a replacement
value. What is buildable without inventing a number is the OBSERVATION: write
down what the book's realised concentration actually was, session by session.

RECORDING ONLY. These tests assert the row's shape, its unit and its NULL
discipline. Nothing here derives, tunes or proposes a ceiling, and nothing in
the product reads these rows back into a sizing, ordering or refusal decision.
"""
import json

import pytest

from src.risk.budget import RiskRequest, allocate_risk_budget
from src.storage.db import Database


@pytest.fixture()
def db(tmp_path):
    d = Database(str(tmp_path / "t.db"))
    d.initialize()
    return d


def _rows(db):
    cur = db.conn.execute("SELECT * FROM realised_risk_budget")
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def _allocation():
    """A real allocator run, not a stub: the recording must describe the
    object that actually rations orders in production."""
    return allocate_risk_budget(
        [RiskRequest("AAA", 5.0), RiskRequest("BBB", 5.0),
         RiskRequest("CCC", 5.0)],
        existing_pct={"AAA": 0.0, "BBB": 0.0, "CCC": 0.0},
        clusters=[["AAA", "BBB"]],
        ceiling_pct=25.0,
        cluster_share_pct=40.0,
        floor_pct=0.5,
    )


def test_a_real_allocator_run_is_recorded_with_its_cluster_shares(db):
    alloc = _allocation()
    assert db.record_realised_risk_budget(
        allocation=alloc,
        existing_pct={"AAA": 1.0, "BBB": 2.5},
        equity=100_000.0,
        cluster_share_pct=40.0,
        run_id="run-1",
    ) is True
    rows = _rows(db)
    assert len(rows) == 1
    row = rows[0]
    assert row["allocator_ran"] == 1
    assert row["equity"] == 100_000.0
    assert row["ceiling_pct"] == 25.0
    assert row["cluster_share_pct"] == 40.0
    # The HELD book's risk is recorded separately from what this session
    # committed — the ceiling is spent by positions that already exist.
    assert row["held_risk_pct"] == pytest.approx(3.5)
    assert row["committed_pct"] == pytest.approx(alloc.committed_pct)
    clusters = json.loads(row["cluster_shares_json"])
    assert clusters, "a run with a cluster must record that cluster"
    # The 40% ceiling is written as a share OF THE COMMITTED TOTAL, so the
    # row has to carry that quantity and not only the raw at-risk percent.
    for entry in clusters:
        assert set(entry) == {
            "members", "at_risk_pct", "share_of_committed_pct",
        }
        assert entry["members"] == sorted(entry["members"])
    aaa_bbb = [c for c in clusters if c["members"] == ["AAA", "BBB"]]
    assert aaa_bbb, "the cluster the allocator rationed must be named"
    assert aaa_bbb[0]["share_of_committed_pct"] == pytest.approx(
        aaa_bbb[0]["at_risk_pct"] / alloc.committed_pct * 100.0
    )
    grants = json.loads(row["grants_json"])
    assert {g["symbol"] for g in grants} == {"AAA", "BBB", "CCC"}
    assert row["rationed_names"] == sum(
        1 for g in grants if g["limited_by"]
    )


def test_an_unknown_book_records_unknown_and_not_zero(db):
    """The allocator does not run when the held book's risk is unreadable.

    That is the state the ceilings go UNENFORCED in, so it must be visible as
    unknown — a row of zeros would read as a book with no concentration.
    """
    assert db.record_realised_risk_budget(
        allocation=None, existing_pct=None, equity=100_000.0,
        cluster_share_pct=40.0, run_id="run-2",
    ) is True
    row = _rows(db)[0]
    assert row["allocator_ran"] == 0
    assert row["committed_pct"] is None
    assert row["cluster_shares_json"] is None
    assert row["held_risk_pct"] is None
    assert row["rationed_names"] is None
    assert row["equity"] == 100_000.0


def test_the_recording_is_idempotent_per_run(db):
    for _ in range(2):
        db.record_realised_risk_budget(
            allocation=_allocation(), existing_pct={}, equity=1.0,
            cluster_share_pct=40.0, run_id="run-3",
        )
    assert len(_rows(db)) == 1


def test_an_empty_held_book_is_zero_not_unknown(db):
    """`{}` is a book that was READ and holds no risk; None is a book that
    could not be read. The row must not collapse the two."""
    db.record_realised_risk_budget(
        allocation=_allocation(), existing_pct={}, equity=1.0,
        cluster_share_pct=40.0, run_id="run-4",
    )
    assert _rows(db)[0]["held_risk_pct"] == 0.0


def test_the_decision_stage_calls_the_recorder():
    """The column existing is not the check; something reaching it is.

    Three settlement recordings shipped in 2026 with live columns and no
    reachable write (`tests/test_settlement_recording_writes.py`), so this
    asserts the production call site exists rather than trusting the method.
    """
    import inspect

    from src import stage_decision
    src = inspect.getsource(stage_decision)
    assert "_record_realised_risk_budget(pipeline, ctx, total_value)" in src
