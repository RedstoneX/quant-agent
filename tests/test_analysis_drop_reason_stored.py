"""Board item 158: a dropped stock's drop REASON is stored beside the stock.

Before this fix the technical seat's per-stock drop reason lived only in a
rotated log line and an aggregate `parse_telemetry` count keyed by symbol —
so a later reader could not tell WHY a name was absent without the log. The
DONE WHEN is a single criterion: the per-stock drop reason is stored
alongside the stock it was dropped for, not only in the log.

These tests prove the reason is now (a) carried through the telemetry keyed by
symbol and (b) persisted to `specialist_evidence`, queryable by symbol + run.
"""
from __future__ import annotations

import json

from unittest.mock import MagicMock

from src.models import AnalysisParseTelemetry
from src.pipeline_stages import (
    ANALYSIS_DROP_KIND,
    UNIDENTIFIED_DROP_KEY,
    _parse_loss_advisories,
    _persist_dropped_reasons,
)
from src.storage.db import Database

RUN_ID = "run-drop-158"


def _drop_rows(db: Database, symbol: str) -> list[dict]:
    cur = db.conn.execute(
        "SELECT symbol, run_id, evidence_json FROM specialist_evidence "
        "WHERE kind = ? AND symbol = ?",
        (ANALYSIS_DROP_KIND, symbol),
    )
    return [dict(r) for r in cur.fetchall()]


# --------------------------------------------------------------------------
# telemetry: the reason travels with the symbol, not just a count
# --------------------------------------------------------------------------

def test_telemetry_captures_reason_keyed_by_symbol():
    tel = AnalysisParseTelemetry()
    tel.record_dropped_item(
        "TechAnalysisResult", "AAPL", reason="failed validation on rating",
    )
    assert tel.dropped_snapshot() == {("TechAnalysisResult", "AAPL"): 1}
    assert tel.dropped_reasons_snapshot() == {
        ("TechAnalysisResult", "AAPL"): "failed validation on rating",
    }


def test_telemetry_first_reason_wins_and_reset_clears():
    tel = AnalysisParseTelemetry()
    tel.record_dropped_item("TechAnalysisResult", "NVDA", reason="malformed: x")
    tel.record_dropped_item("TechAnalysisResult", "NVDA", reason="second try")
    # First concrete reason is kept, not clobbered by a later retry's drop.
    assert tel.dropped_reasons_snapshot()[("TechAnalysisResult", "NVDA")] == (
        "malformed: x"
    )
    tel.reset()
    assert tel.dropped_reasons_snapshot() == {}
    assert tel.dropped_snapshot() == {}


def test_reason_optional_leaves_other_seats_unchanged():
    tel = AnalysisParseTelemetry()
    tel.record_dropped_item("StockNewsItem", "TSLA")  # no reason, as before
    assert tel.dropped_snapshot() == {("StockNewsItem", "TSLA"): 1}
    assert tel.dropped_reasons_snapshot() == {}


# --------------------------------------------------------------------------
# persistence: the reason is stored beside the stock, queryable by symbol
# --------------------------------------------------------------------------

def test_dropped_reason_is_stored_and_queryable_per_symbol(tmp_path):
    db = Database(str(tmp_path / "t.db"))
    db.initialize()

    written = _persist_dropped_reasons(
        db, RUN_ID,
        dropped={("TechAnalysisResult", "AAPL"): 1},
        reasons={("TechAnalysisResult", "AAPL"): "failed validation on rating"},
        book_symbols=set(),
    )
    assert written == 1

    rows = _drop_rows(db, "AAPL")
    assert len(rows) == 1
    assert rows[0]["run_id"] == RUN_ID
    payload = json.loads(rows[0]["evidence_json"])
    # The load-bearing assertion: the WHY is retrievable per stock, not just a
    # count and not only in the log.
    assert payload["reason"] == "failed validation on rating"
    assert payload["outcome"] == "dropped"
    assert payload["recovered"] is False


def test_recovered_symbol_marked_recovered(tmp_path):
    db = Database(str(tmp_path / "t.db"))
    db.initialize()

    _persist_dropped_reasons(
        db, RUN_ID,
        dropped={("TechAnalysisResult", "META"): 1},
        reasons={("TechAnalysisResult", "META"): "malformed: bad json"},
        book_symbols={"META"},  # dropped but the retry put it back in the book
    )
    payload = json.loads(_drop_rows(db, "META")[0]["evidence_json"])
    assert payload["recovered"] is True
    assert payload["outcome"] == "recovered"
    assert payload["reason"] == "malformed: bad json"


def test_unidentified_drop_not_persisted(tmp_path):
    db = Database(str(tmp_path / "t.db"))
    db.initialize()

    written = _persist_dropped_reasons(
        db, RUN_ID,
        dropped={("TechAnalysisResult", UNIDENTIFIED_DROP_KEY): 1},
        reasons={},
        book_symbols=set(),
    )
    # No stock to file it against — nothing written.
    assert written == 0
    cur = db.conn.execute(
        "SELECT COUNT(*) AS n FROM specialist_evidence WHERE kind = ?",
        (ANALYSIS_DROP_KIND,),
    )
    assert cur.fetchone()["n"] == 0


def test_persist_never_raises_on_db_failure():
    db = MagicMock()
    db.insert_specialist_evidence.side_effect = RuntimeError("disk full")
    # Observability only: a write failure must never propagate into the risk
    # decision it is recording.
    written = _persist_dropped_reasons(
        db, RUN_ID,
        dropped={("TechAnalysisResult", "AAPL"): 1},
        reasons={("TechAnalysisResult", "AAPL"): "failed validation on rating"},
        book_symbols=set(),
    )
    assert written == 0
    db.insert_specialist_evidence.assert_called_once()


# --------------------------------------------------------------------------
# advisory: the reason now appears beside the symbol the RM is shown
# --------------------------------------------------------------------------

def test_advisory_names_the_reason_for_lost_rows():
    out = _parse_loss_advisories(
        dropped={("TechAnalysisResult", "AAPL"): 1},
        book_symbols=set(),
        reasons={("TechAnalysisResult", "AAPL"): "failed validation on rating"},
    )
    lost = [v for v in out if v.rule == "analysis_parse_loss"]
    assert lost, "expected a lost-coverage advisory"
    assert "failed validation on rating" in lost[0].message
