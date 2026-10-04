"""Item 211 — the owner is shown what the global mute has swallowed.

The mute stays on. These tests pin the READ: that an empty record is
reported as empty rather than as broken, that the record's incomplete
coverage is always stated, that live-risk messages are listed one by one
and never folded into a total, and that the grouping is by kind and by ET
day so "would I be flooded again" is answerable from the surface.
"""

from __future__ import annotations

import sqlite3

import pytest

from src.api import db_reads


def _make_db(path, rows):
    conn = sqlite3.connect(str(path))
    conn.execute(
        "CREATE TABLE notifier_sends ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, "
        "status TEXT NOT NULL, run_id TEXT, text TEXT NOT NULL, "
        "detail TEXT, timestamp TEXT NOT NULL)"
    )
    conn.executemany(
        "INSERT INTO notifier_sends (kind, status, run_id, text, detail, timestamp) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        rows,
    )
    conn.commit()
    conn.close()


@pytest.fixture()
def muted_db(tmp_path, monkeypatch):
    def _build(rows):
        path = tmp_path / "quant_agent.db"
        _make_db(path, rows)
        monkeypatch.setattr(db_reads, "_connect", lambda: sqlite3.connect(str(path)))
        return path

    return _build


def test_empty_record_is_an_answer_not_a_failure(muted_db):
    """Nothing dropped is a real, good answer — not an error state."""
    muted_db([("owner_alert", "sent", None, "delivered fine", None, "2026-10-01 12:00:00")])
    out = db_reads.get_muted_backlog()
    assert out["record_available"] is True
    assert out["total"] == 0
    assert out["live_risk_total"] == 0
    assert out["by_kind"] == [] and out["by_day"] == []


def test_coverage_gap_is_always_stated(muted_db):
    """The record began after the mute did; the surface must say so."""
    muted_db([])
    out = db_reads.get_muted_backlog()
    assert out["coverage_complete"] is False
    assert out["mute_began_on"] in out["coverage_gap"]
    assert out["record_begins_at"][:10] in out["coverage_gap"]


def test_missing_table_is_not_reported_as_an_empty_backlog(tmp_path, monkeypatch):
    """Unreadable is a different fact from "nothing was muted"."""
    path = tmp_path / "empty.db"
    sqlite3.connect(str(path)).close()
    monkeypatch.setattr(db_reads, "_connect", lambda: sqlite3.connect(str(path)))
    out = db_reads.get_muted_backlog()
    assert out["record_available"] is False
    assert out["total"] == 0


def test_live_risk_is_listed_per_message_and_grouped_without_collapsing(muted_db):
    muted_db([
        ("owner_alert", "muted", None,
         "\U0001f6d1\U0001f6d1 PROTECTIVE STOP UNREADABLE\nthe desk cannot read it",
         "suppressed by TELEGRAM_DISABLED; symbols: aaa", "2026-10-01 14:00:00"),
        ("owner_alert", "muted", None,
         "\U0001f534 COULD NOT PUT THE PROTECTIVE STOP BACK\nnothing is guarding it",
         "suppressed by TELEGRAM_DISABLED; symbols: bbb", "2026-10-01 15:00:00"),
        ("intra_check", "muted", None, "half-hourly check: nothing to do",
         "suppressed by TELEGRAM_DISABLED; not sent", "2026-10-01 16:00:00"),
    ])
    out = db_reads.get_muted_backlog()
    assert out["total"] == 3
    assert out["live_risk_total"] == 2
    # Each live-risk message keeps its own row — two unprotected names are
    # not one event, which is the whole reason the owner muted the desk.
    assert len(out["live_risk"]) == 2
    assert {m["symbols"][0] for m in out["live_risk"]} == {"AAA", "BBB"}
    kinds = {k["kind"]: k for k in out["by_kind"]}
    assert kinds["owner_alert"]["count"] == 2
    assert kinds["owner_alert"]["live_risk_count"] == 2
    assert kinds["intra_check"]["live_risk_count"] == 0


def test_days_are_the_owners_days_not_utc(muted_db):
    """01:00 UTC is still the previous evening in ET."""
    muted_db([
        ("generic", "muted", None, "late one", None, "2026-10-02 01:00:00"),
        ("generic", "muted", None, "earlier one", None, "2026-10-01 14:00:00"),
    ])
    out = db_reads.get_muted_backlog()
    assert [d["day"] for d in out["by_day"]] == ["2026-10-01"]
    assert out["by_day"][0]["count"] == 2


def test_a_closed_gap_is_not_counted_as_live_risk(muted_db):
    """"The stop is back on" is good news about a gap, not an open one."""
    muted_db([
        ("owner_alert", "muted", None,
         "\U0001f6d1\U0001f6d1 A MISSING STOP WAS PUT BACK\ncovered again", None,
         "2026-10-01 14:00:00"),
    ])
    out = db_reads.get_muted_backlog()
    assert out["total"] == 1
    assert out["live_risk_total"] == 0


def test_endpoint_returns_the_record_without_unmuting_anything(muted_db, monkeypatch):
    from fastapi.testclient import TestClient

    from src.api.server import app

    muted_db([
        ("owner_alert", "muted", None,
         "\U0001f534 UNPROTECTED SHARES, AND THE DESK IS NOT RUNNING\nno stop",
         "suppressed by TELEGRAM_DISABLED; symbols: ccc", "2026-10-01 14:00:00"),
    ])
    monkeypatch.setenv("TELEGRAM_DISABLED", "1")
    with TestClient(app) as client:
        res = client.get("/alerts/muted-backlog")
    assert res.status_code == 200
    payload = res.json()
    assert payload["live_risk_total"] == 1
    assert payload["coverage_complete"] is False
    # The read must not have touched the mute.
    import os
    assert os.environ["TELEGRAM_DISABLED"] == "1"


def test_live_risk_is_never_buried_by_volume(muted_db):
    """The owner muted the desk because the important ones got buried.

    A flood of ordinary messages must not push a live-risk one out of the
    list OR out of any count. The read is uncapped for exactly this reason,
    so a live-risk message recorded before 500 ordinary ones still shows.
    """
    rows = [
        ("owner_alert", "muted", None,
         "\U0001f534 UNPROTECTED SHARES, AND THE DESK IS NOT RUNNING\nno stop",
         "suppressed by TELEGRAM_DISABLED; symbols: zzz", "2026-10-01 09:00:00"),
    ]
    rows += [
        ("intra_check", "muted", None, f"routine note {i}", None,
         f"2026-10-01 1{i // 60:01d}:{i % 60:02d}:00")
        for i in range(500)
    ]
    muted_db(rows)
    out = db_reads.get_muted_backlog()
    assert out["total"] == 501
    assert out["live_risk_total"] == 1
    assert len(out["live_risk"]) == 1
    assert out["live_risk"][0]["symbols"] == ["ZZZ"]


def test_empty_backlog_payload_carries_everything_the_panel_prints(muted_db):
    """The empty state must have words to render, not a blank or an error."""
    muted_db([])
    out = db_reads.get_muted_backlog()
    assert out["record_available"] is True
    assert out["total"] == 0
    assert out["coverage_gap"].strip()
    assert out["oldest"] is None and out["newest"] is None


def test_a_failed_owner_alert_reaches_the_backlog(muted_db):
    """A send the transport TRIED and could not complete is undelivered too.

    The transport writes `status='failed'` with the reason; the caller
    discards the returned False; Telegram is muted, so this page is the
    owner's only channel. Before this test the read enumerated only the
    two deliberate drops, so a naked-position alert that failed to send
    appeared nowhere at all.
    """
    muted_db([
        ("owner_alert", "failed", None,
         "PROTECTIVE STOP UNREADABLE\nthe broker would not say",
         "send error: boom", "2026-10-01 13:00:00"),
        ("owner_alert", "sent", None, "delivered fine", None,
         "2026-10-01 12:00:00"),
    ])
    out = db_reads.get_muted_backlog()
    assert out["total"] == 1
    assert out["failed_total"] == 1
    assert out["muted_total"] == 0 and out["filtered_total"] == 0
    assert out["live_risk_total"] == 1
    assert out["live_risk"][0]["reason"] == "failed"
    assert out["by_kind"][0]["failed_count"] == 1
    assert out["by_day"][0]["failed_count"] == 1
