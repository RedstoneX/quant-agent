"""Item 211 — the suppression record has to be readable without Telegram.

The owner muted every desk alert because one self-clearing fault paged him
on both edges all weekend. The fix for that is elsewhere (`src/cost_circuit.py`,
`src/coverage_watchdog.py`); this file covers the last criterion of the item:
a suppressed alert that nothing surfaces is a LOST alert, so every refusal to
re-send must be readable on the read-only API, and no live-risk alert may ever
be silenced by it.

WHERE THE LIVE-RISK LINE IS DRAWN. "Live-risk" here means a message about a
position whose protection is gone or never arrived: a stop that failed to
place, a stop that failed to re-arm after a scale-in, an uncovered position, a
broker rejection. Those travel through the per-symbol, per-ET-day claim
helpers, and the invariant proved below is that the FIRST occurrence for a
symbol on a day is always released — only a byte-identical repeat of the same
fault, for the same symbol, on the same day, is held, and that repeat is
written to `suppressed_alerts` where this endpoint reads it. The cost
circuit's deferral is narrower still and cannot touch these at all: it applies
only to `_SELF_CLEARING_HARD_TRIGGERS` (paid-provider faults), which is proved
in tests/test_cost_circuit.py.
"""

from __future__ import annotations

import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from src.api import db_reads
from src.api.server import create_app


def test_suppression_state_paths_track_the_watchdog(monkeypatch):
    """The API must read the SAME files the suppressing code writes.

    Spelled out as literals in db_reads (it may not import trading modules),
    so this is the pin that stops the two drifting apart into an endpoint
    that truthfully reports "nothing suppressed" about the wrong file.
    """
    from src import coverage_watchdog as cw
    from src import drift_state as ds

    assert db_reads.SUPPRESSION_STATE_PATHS == (
        cw.STATE_PATH, ds.DEPLOY_DRIFT_STATE_PATH,
    )


def _seed_db(path):
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE llm_circuit_events (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "event_type TEXT, trigger_code TEXT, detail TEXT, run_id TEXT, "
        "created_at TEXT)"
    )
    conn.execute(
        "INSERT INTO llm_circuit_events (event_type, trigger_code, detail, "
        "run_id, created_at) VALUES ('suspend_alert_deferred', 'tech_seat', "
        "'held pending the self-clear window', 'run-1', '2026-09-28T12:00:00Z')"
    )
    conn.execute(
        "INSERT INTO llm_circuit_events (event_type, trigger_code, detail, "
        "run_id, created_at) VALUES ('trip', 'tech_seat', 'x', 'run-1', "
        "'2026-09-28T12:00:00Z')"
    )
    conn.commit()
    conn.close()


@pytest.fixture()
def wired(tmp_path, monkeypatch):
    db = tmp_path / "q.db"
    _seed_db(db)
    monkeypatch.setattr(db_reads, "get_db_path", lambda: db)
    state = tmp_path / "coverage_heartbeat.json"
    state.write_text(json.dumps({
        "suppressed_alerts": {
            "deploy_drift": {
                "day": "2026-09-28", "count": 4,
                "events": [{"key": "MAIN@ABC", "day": "2026-09-28"}],
            },
        },
    }))
    monkeypatch.setattr(
        db_reads, "SUPPRESSION_STATE_PATHS", (state, tmp_path / "absent.json"),
    )
    return db


def test_endpoint_reports_both_records(wired):
    client = TestClient(create_app())
    body = client.get("/alerts/suppressed").json()
    assert body["deferred_available"] is True
    assert [d["trigger_code"] for d in body["deferred_suspensions"]] == ["tech_seat"]
    assert body["suppression_state_available"] is True
    assert body["suppressed_repeats"]["deploy_drift"]["count"] == 4


def test_unreadable_record_is_not_reported_as_empty(tmp_path, monkeypatch):
    """"Could not read it" and "nothing was suppressed" are different facts."""
    monkeypatch.setattr(db_reads, "get_db_path", lambda: tmp_path / "missing.db")
    monkeypatch.setattr(
        db_reads, "SUPPRESSION_STATE_PATHS", (tmp_path / "nope.json",),
    )
    out = db_reads.get_suppressed_alerts()
    assert out["deferred_available"] is False
    assert out["suppression_state_available"] is False


def test_live_risk_first_occurrence_is_never_suppressed(tmp_path):
    """Every live-risk alert type releases its first page of the day."""
    from src.coverage_watchdog import claim_typed_alert, load_state
    from src.execution.scale_in import REARM_FAILURE_ALERT_KIND

    live_risk_kinds = [
        REARM_FAILURE_ALERT_KIND,     # protective stop not back after a scale-in
        "stop_placement_failed",      # a stop the broker refused
        "uncovered_position",         # a position with no protection at all
        "broker_rejection",           # an order the broker rejected
    ]
    path = tmp_path / "state.json"
    for kind in live_risk_kinds:
        assert claim_typed_alert(kind, ["AAA"], path=path) == ["AAA"], kind
        # A different symbol is a different fault and is also released.
        assert claim_typed_alert(kind, ["BBB"], path=path) == ["BBB"], kind
    # Only the identical repeat is held, and it is recorded, not dropped.
    for kind in live_risk_kinds:
        assert claim_typed_alert(kind, ["AAA"], path=path) == []
        assert load_state(path)["suppressed_alerts"][kind]["count"] == 1


def test_deploy_drift_repeat_is_recorded_not_dropped(tmp_path):
    from scripts.check_deploy_drift import DriftReport, record_state
    from src.coverage_watchdog import load_state

    report = DriftReport(
        head_sha="a" * 40, remote_sha="b" * 40, behind_count=41,
        deployed_path="/box/checkout", fetch_ok=True, missing_commits=[],
        unexpected_dirty_files=[],
    )
    path = tmp_path / "drift.json"
    record_state(report, "origin/main", alerted=True, state_path=path)
    record_state(report, "origin/main", alerted=False, state_path=path)
    entry = load_state(path)["suppressed_alerts"]["deploy_drift"]
    assert entry["count"] == 1
    assert entry["events"][0]["key"] == "b" * 40
