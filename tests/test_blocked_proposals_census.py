"""`scripts/blocked_proposals_census.py` attributes durable reason records.

Funnel-queue item 2's "9 unexplained proposals" investigation (2026-09
overnight session) traced every one of a set of previously-unexplained
proposals to one of four real, legitimate pipeline causes — a malformed
Risk Manager response causing a whole-plan refusal, insufficient cash,
a sector-concentration block, and a symbol-guard block — and found the
pipeline already writes a durable, queryable reason record for each (via
`_record_pipeline_event`/`_record_execution_skip` in
`src/pipeline_stages.py`). The census script itself was never updated to
read three of those four records (`symbol_guard`, `hard_risk` — which
covers sector concentration — and `risk_manager_unparseable_output`), so
a new occurrence of any of them still reported as `order_not_placed`, the
same "unexplained" shape as a genuinely stalled/interrupted run, even
though the real reason was sitting in the database. `insufficient_cash`
was already correctly attributed before this fix, because it is recorded
as an `execution_skip` row — a kind the script has always read.

These tests pin, against a real sqlite database (not a mock), that the
script now attributes all three previously-misclassified causes by name,
and that the already-working `insufficient_cash` path is undisturbed.
"""

import json

from scripts.blocked_proposals_census import (
    _load_pairs,
    _load_recorded_reasons,
    _load_skips,
    _load_verdicts,
    _load_fills,
    _connect,
    classify,
)
from src.storage.db import Database


def _db(tmp_path, name="census.db"):
    path = tmp_path / name
    db = Database(str(path))
    db.initialize()
    return db, path


def _target(db, run_id, decision_id, symbol, days_ago=1):
    row_id = db.insert_specialist_evidence(
        run_id=run_id, decision_id=decision_id, agent_name="portfolio_manager",
        kind="target", scope="symbol", symbol=symbol,
        evidence_json=json.dumps({
            "symbol": symbol, "risk_allocation_pct": 1.0,
            "conviction": "high", "thesis": "t", "thesis_invalid_if": "x",
        }),
    )
    db.conn.execute(
        "UPDATE specialist_evidence SET timestamp = datetime('now', ?) "
        "WHERE id = ?", (f"-{days_ago} days", row_id),
    )
    db.conn.commit()


def _proposed_order(db, run_id, decision_id, symbol, days_ago=1):
    row_id = db.insert_specialist_evidence(
        run_id=run_id, decision_id=decision_id, agent_name="portfolio_manager",
        kind="proposed_order", scope="symbol", symbol=symbol,
        evidence_json=json.dumps({"action": "BUY", "symbol": symbol,
                                  "allocation_pct": 5.0}),
    )
    db.conn.execute(
        "UPDATE specialist_evidence SET timestamp = datetime('now', ?) "
        "WHERE id = ?", (f"-{days_ago} days", row_id),
    )
    db.conn.commit()


def _pipeline_event(db, run_id, decision_id, symbol, stage, outcome, reason,
                     detail=None, days_ago=1):
    payload = {"stage": stage, "outcome": outcome, "reason": reason}
    if detail is not None:
        payload["detail"] = detail
    row_id = db.insert_specialist_evidence(
        run_id=run_id, decision_id=decision_id, agent_name="pipeline",
        kind="pipeline_event", scope="symbol", symbol=symbol,
        evidence_json=json.dumps(payload),
    )
    db.conn.execute(
        "UPDATE specialist_evidence SET timestamp = datetime('now', ?) "
        "WHERE id = ?", (f"-{days_ago} days", row_id),
    )
    db.conn.commit()


def _skip(db, run_id, decision_id, symbol, reason, days_ago=1):
    row_id = db.insert_specialist_evidence(
        run_id=run_id, decision_id=decision_id, agent_name="execution",
        kind="execution_skip", scope="symbol", symbol=symbol,
        evidence_json=json.dumps({"symbol": symbol, "reason": reason,
                                  "detail": "d"}),
    )
    db.conn.execute(
        "UPDATE specialist_evidence SET timestamp = datetime('now', ?) "
        "WHERE id = ?", (f"-{days_ago} days", row_id),
    )
    db.conn.commit()


def _classify_all(path):
    """Run the same load+classify sequence `main()` does, against `path`."""
    con = _connect(path)
    try:
        ordered = _load_pairs(con, "proposed_order")
        verdicts = _load_verdicts(con)
        skips = _load_skips(con)
        fills = _load_fills(con)
        recorded_reasons = _load_recorded_reasons(con)
        return ordered, verdicts, skips, fills, recorded_reasons
    finally:
        con.close()


def test_symbol_guard_block_is_attributed_not_order_not_placed(tmp_path):
    """A symbol_guard block leaves a `proposed_order` row and nothing else
    (RiskStage returns before the Risk Manager is ever called), which used
    to be indistinguishable from an interrupted run.
    """
    db, path = _db(tmp_path)
    _target(db, "r1", "d1", "PATH", days_ago=2)
    _proposed_order(db, "r1", "d1", "PATH", days_ago=2)
    _pipeline_event(db, "r1", "d1", "PATH", "deterministic_gate", "blocked",
                     "symbol_guard", detail="no supporting analysis",
                     days_ago=2)

    ordered, verdicts, skips, fills, recorded_reasons = _classify_all(path)
    reason = classify("d1", "PATH", ordered=ordered, verdicts=verdicts,
                       skips=skips, fills=fills,
                       recorded_reasons=recorded_reasons)
    assert reason == "symbol_guard"
    assert reason != "order_not_placed"


def test_sector_concentration_block_is_attributed_via_hard_risk(tmp_path):
    """Sector concentration is one of the rules behind the portfolio-level
    hard risk filter; the pipeline records the gate name (`hard_risk`), not
    the individual rule, so that is what this census attributes.
    """
    db, path = _db(tmp_path)
    _target(db, "r1", "d1", "XOM", days_ago=2)
    _proposed_order(db, "r1", "d1", "XOM", days_ago=2)
    _pipeline_event(db, "r1", "d1", "XOM", "deterministic_gate", "blocked",
                     "hard_risk", detail="sector_concentration: Energy at 32%",
                     days_ago=2)

    ordered, verdicts, skips, fills, recorded_reasons = _classify_all(path)
    reason = classify("d1", "XOM", ordered=ordered, verdicts=verdicts,
                       skips=skips, fills=fills,
                       recorded_reasons=recorded_reasons)
    assert reason == "hard_risk"
    assert reason != "order_not_placed"


def test_malformed_risk_manager_response_is_attributed(tmp_path):
    """No `verdict` row is ever written when the Risk Manager's output is
    unparseable — `RiskStage` only persists kind='verdict' when a verdict
    object exists — so this used to fall straight through to
    `order_not_placed` even though `_record_pipeline_event` already wrote
    the real reason.
    """
    db, path = _db(tmp_path)
    _target(db, "r1", "d1", "NVDA", days_ago=2)
    _proposed_order(db, "r1", "d1", "NVDA", days_ago=2)
    _pipeline_event(db, "r1", "d1", "NVDA", "risk", "failed",
                     "risk_manager_unparseable_output", days_ago=2)

    ordered, verdicts, skips, fills, recorded_reasons = _classify_all(path)
    reason = classify("d1", "NVDA", ordered=ordered, verdicts=verdicts,
                       skips=skips, fills=fills,
                       recorded_reasons=recorded_reasons)
    assert reason == "risk_manager_unparseable_output"
    assert reason != "order_not_placed"


def test_insufficient_cash_was_already_correctly_attributed(tmp_path):
    """Regression guard: `insufficient_cash` is an `execution_skip` row, a
    kind this script has always read via `_load_skips` — this fix must not
    change or duplicate that existing, already-correct attribution.
    """
    db, path = _db(tmp_path)
    _target(db, "r1", "d1", "JPM", days_ago=2)
    _proposed_order(db, "r1", "d1", "JPM", days_ago=2)
    _skip(db, "r1", "d1", "JPM", "insufficient_cash", days_ago=2)

    ordered, verdicts, skips, fills, recorded_reasons = _classify_all(path)
    reason = classify("d1", "JPM", ordered=ordered, verdicts=verdicts,
                       skips=skips, fills=fills,
                       recorded_reasons=recorded_reasons)
    assert reason == "insufficient_cash"
    assert ("d1", "JPM") not in recorded_reasons


def test_constructor_dropped_attribution_is_unchanged(tmp_path):
    """Regression guard for the one cause this script already handled
    (2026-09-03): a constructor drop must still classify as
    `constructor_dropped`, not fall through to `no_order_built`.
    """
    db, path = _db(tmp_path)
    _target(db, "r1", "d1", "AMD", days_ago=2)
    _pipeline_event(db, "r1", "d1", "AMD", "deterministic_gate", "blocked",
                     "constructor_dropped", detail="widened past noise band",
                     days_ago=2)

    ordered, verdicts, skips, fills, recorded_reasons = _classify_all(path)
    reason = classify("d1", "AMD", ordered=ordered, verdicts=verdicts,
                       skips=skips, fills=fills,
                       recorded_reasons=recorded_reasons)
    assert reason == "constructor_dropped"


def test_genuinely_unrecorded_gap_still_falls_through(tmp_path):
    """A proposal with NO recorded reason of any kind must still land in
    the unexplained bucket — this fix narrows the gap, it does not paper
    over what is still genuinely unrecorded.
    """
    db, path = _db(tmp_path)
    _target(db, "r1", "d1", "GAP", days_ago=2)
    _proposed_order(db, "r1", "d1", "GAP", days_ago=2)

    ordered, verdicts, skips, fills, recorded_reasons = _classify_all(path)
    reason = classify("d1", "GAP", ordered=ordered, verdicts=verdicts,
                       skips=skips, fills=fills,
                       recorded_reasons=recorded_reasons)
    assert reason == "order_not_placed"
