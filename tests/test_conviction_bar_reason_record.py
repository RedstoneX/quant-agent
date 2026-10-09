"""A candidate the PM's gates refuse leaves a durable `candidate_refused` event with its reason."""

from types import SimpleNamespace

from src.agents.portfolio_manager import PortfolioManagerAgent
from src.stage_decision import _journal_refused_candidates
from src.storage.db import Database

REASON = "no technical read"


def test_blocked_reason_lands_in_stored_pipeline_event(tmp_path):
    db = Database(str(tmp_path / "t.db"))
    db.initialize()
    ctx = SimpleNamespace(run_id="run-1", decision_id="dec-1")
    _journal_refused_candidates(SimpleNamespace(db=db), ctx, {"AAPL": REASON})
    c = db.conn
    rows = c.execute("SELECT * FROM specialist_evidence").fetchall()
    blob = " ".join(str(tuple(r)) for r in rows)
    assert "candidate_refused" in blob and "AAPL" in blob and REASON in blob
    assert "last_blocked" in vars(PortfolioManagerAgent)
