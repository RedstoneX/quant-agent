"""A ranked candidate's composite score lands in a durable `candidate_ranked` event."""

from types import SimpleNamespace

from src.stage_decision import _journal_ranked_candidates
from src.storage.db import Database
from src.verdicts import RankedCandidate


def test_ranked_score_is_stored_and_read_back(tmp_path):
    db = Database(str(tmp_path / "t.db"))
    db.initialize()
    ctx = SimpleNamespace(run_id="run-1", decision_id="dec-1")
    seat = SimpleNamespace(seat="technical", conviction="high", magnitude=0.5)
    ranked = [
        RankedCandidate(
            symbol="AAPL",
            direction="long",
            score=1.2345,
            verdicts=[seat],
            components={"conviction_score": 0.9, "magnitude": 0.3345},
        ),
        RankedCandidate(symbol="MSFT", direction="long", score=0.5),
    ]
    _journal_ranked_candidates(SimpleNamespace(db=db), ctx, ranked)
    rows = db.conn.execute("SELECT * FROM specialist_evidence").fetchall()
    blob = " ".join(str(tuple(r)) for r in rows)
    assert blob.count("candidate_ranked") == 2
    assert "AAPL" in blob and "MSFT" in blob and "run-1" in blob and "dec-1" in blob
    assert "1.2345" in blob and "0.3345" in blob and "technical" in blob
