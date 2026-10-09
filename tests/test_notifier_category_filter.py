"""Per-category mute: `TELEGRAM_RISK_ONLY`.

The owner muted the whole channel (`TELEGRAM_DISABLED`) because of the
volume of operational-fault messages, which also silenced money-at-risk
alarms such as "NO STOP AT ALL". These tests pin the narrower switch:
money-at-risk alarms always land, operational noise is dropped AND
recorded, the hard mute still silences everything, and an unclassified
send fails CLOSED (delivered).
"""

import sqlite3
from unittest.mock import MagicMock, patch

import pytest

from src import notifier as notifier_mod
from src.notifier import (
    CATEGORY_OPERATIONAL,
    CATEGORY_RISK,
    TelegramNotifier,
    resolve_category,
    was_suppressed,
)


@pytest.fixture
def creds(monkeypatch, tmp_path):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "12345")
    monkeypatch.delenv("TELEGRAM_DISABLED", raising=False)
    monkeypatch.delenv("TELEGRAM_RISK_ONLY", raising=False)
    monkeypatch.setattr(notifier_mod, "_DB_PATH", tmp_path / "qamc.db")
    monkeypatch.setattr(notifier_mod, "_REHEARSAL_MODE", False, raising=False)
    return tmp_path / "qamc.db"


def _rows(db):
    if not db.exists():
        return []
    conn = sqlite3.connect(str(db))
    try:
        return conn.execute("SELECT kind, status, text FROM notifier_sends ORDER BY id").fetchall()
    finally:
        conn.close()


def _send(text, **kwargs):
    """Send with the HTTP layer stubbed out; returns (result, post_mock)."""
    with patch("src.notifier.requests.post") as post:
        post.return_value = MagicMock(status_code=200, json=lambda: {"ok": True})
        post.return_value.raise_for_status = MagicMock()
        return TelegramNotifier().send(text, **kwargs), post


# --- requirement 4: these must ALWAYS pass when risk-only is on ---

RISK_MESSAGES = [
    ("naked position", "🔴 NO STOP AT ALL: 2 position(s) with nothing protecting them"),
    ("stop placement failed", "🔴 COULD NOT PUT THE PROTECTIVE STOP BACK"),
    ("order rejected", "🛑 AN ORDER WAS REJECTED AND RISK IS STILL ON"),
    ("trade executed", "BOUGHT 10 AAPL at $100.00"),
    ("broker unreachable", "🔴 BROKER UNREACHABLE while 4 position(s) are held"),
    ("liquidation", "🛑 LIQUIDATION: the desk is flattening the book"),
]


@pytest.mark.parametrize("label,text", RISK_MESSAGES, ids=[m[0] for m in RISK_MESSAGES])
def test_risk_messages_pass_under_risk_only(creds, monkeypatch, label, text):
    monkeypatch.setenv("TELEGRAM_RISK_ONLY", "1")
    ok, post = _send(text, kind="owner_alert", category=CATEGORY_RISK)
    assert ok is True
    assert post.call_count == 1
    assert [r[1] for r in _rows(creds)] == ["sent"]


# --- requirement 5: these must be suppressed, and recorded ---

OPERATIONAL_MESSAGES = [
    ("cost circuit", "generic", "🔴 QAMC PAID ANALYSIS SUSPENDED"),
    ("provider failure", "owner_alert", "INTRADAY SNAPSHOT UNAVAILABLE for NVDA"),
    ("data quality", "owner_alert", "DATA QUALITY ALERT — the morning session ran on stale data"),
    ("scan crash", "generic", "🛑 CRASHED: the search for intraday opportunities stopped"),
    ("routine check", "intra_check", "hourly check: nothing to report"),
    ("session summary", "evening", "evening session complete"),
]


@pytest.mark.parametrize("label,kind,text", OPERATIONAL_MESSAGES, ids=[m[0] for m in OPERATIONAL_MESSAGES])
def test_operational_messages_suppressed_and_recorded(creds, monkeypatch, label, kind, text):
    monkeypatch.setenv("TELEGRAM_RISK_ONLY", "1")
    category = CATEGORY_OPERATIONAL if kind in ("generic", "owner_alert") else None
    ok, post = _send(text, kind=kind, category=category)
    assert was_suppressed(ok)  # a deliberate drop, never a failed send
    assert post.call_count == 0
    rows = _rows(creds)
    assert [r[1] for r in rows] == ["filtered"], "a dropped alarm must never be invisible"
    assert rows[0][0] == kind
    assert rows[0][2] == text


def test_filtered_status_is_distinct_from_sent_and_muted(creds, monkeypatch):
    monkeypatch.setenv("TELEGRAM_RISK_ONLY", "1")
    _send("routine", kind="evening")
    _send("🔴 NO STOP AT ALL", kind="owner_alert")
    statuses = [r[1] for r in _rows(creds)]
    assert statuses == ["filtered", "sent"]
    assert "muted" not in statuses


# --- requirement 7: fail closed ---


def test_unclassified_generic_send_is_delivered(creds, monkeypatch):
    """No category, no kind mapping -> money-at-risk -> goes through."""
    monkeypatch.setenv("TELEGRAM_RISK_ONLY", "1")
    ok, post = _send("something nobody classified")
    assert ok is True
    assert post.call_count == 1


def test_unknown_category_string_is_delivered(creds, monkeypatch):
    monkeypatch.setenv("TELEGRAM_RISK_ONLY", "1")
    ok, post = _send("mystery", kind="generic", category="wat")
    assert ok is True
    assert post.call_count == 1


def test_resolve_category_fails_closed():
    assert resolve_category(None, "generic") == CATEGORY_RISK
    assert resolve_category(None, "owner_alert") == CATEGORY_RISK
    assert resolve_category("", None) == CATEGORY_RISK
    assert resolve_category("nonsense", "generic") == CATEGORY_RISK
    assert resolve_category(CATEGORY_OPERATIONAL, "owner_alert") == CATEGORY_OPERATIONAL
    assert resolve_category(CATEGORY_RISK, "evening") == CATEGORY_RISK
    assert resolve_category(None, "evening") == CATEGORY_OPERATIONAL


# --- requirement 1/2: the hard mute is unchanged and wins ---


def test_hard_mute_still_silences_everything(creds, monkeypatch):
    monkeypatch.setenv("TELEGRAM_DISABLED", "1")
    ok, post = _send("🔴 NO STOP AT ALL", kind="owner_alert", category=CATEGORY_RISK)
    assert was_suppressed(ok)  # a deliberate drop, never a failed send
    assert post.call_count == 0
    assert [r[1] for r in _rows(creds)] == ["muted"]


def test_hard_mute_wins_over_risk_only(creds, monkeypatch):
    monkeypatch.setenv("TELEGRAM_DISABLED", "1")
    monkeypatch.setenv("TELEGRAM_RISK_ONLY", "1")
    n = TelegramNotifier()
    assert n.enabled is False
    assert n.muted is True
    ok, post = _send("🔴 NO STOP AT ALL", kind="owner_alert", category=CATEGORY_RISK)
    assert was_suppressed(ok)  # a deliberate drop, never a failed send
    assert post.call_count == 0
    assert [r[1] for r in _rows(creds)] == ["muted"]


# --- requirement 8: nothing changes when neither switch is set ---


@pytest.mark.parametrize(
    "kind,category",
    [("evening", None), ("generic", CATEGORY_OPERATIONAL), ("owner_alert", CATEGORY_RISK)],
)
def test_nothing_changes_when_neither_switch_is_set(creds, kind, category):
    ok, post = _send("anything at all", kind=kind, category=category)
    assert ok is True
    assert post.call_count == 1
    assert [r[1] for r in _rows(creds)] == ["sent"]


def test_risk_only_default_is_off(creds):
    assert TelegramNotifier().risk_only is False


def test_send_document_is_not_filtered_because_it_carries_money(creds, monkeypatch):
    """`document` was in the suppressed table. Its only caller is the P&L
    history export (`TradingPipeline.run_daily`), which is money-bearing,
    so it is classified `risk` and delivered — fail closed."""
    monkeypatch.setenv("TELEGRAM_RISK_ONLY", "1")
    assert resolve_category(None, "document") == CATEGORY_RISK
    with patch("src.notifier.requests.post") as post:
        post.return_value = MagicMock(status_code=200, json=lambda: {"ok": True})
        post.return_value.raise_for_status = MagicMock()
        ok = TelegramNotifier().send_document(b"a,b\n1,2\n", "pnl.csv", "daily P&L")
    assert ok is True
    assert post.call_count == 1
    assert [r[1] for r in _rows(creds)] == ["sent"]


def test_a_suppressed_cost_alert_is_not_reported_as_delivered(tmp_path):
    """PR #978 defect A: settled is not failed, but settled is not DELIVERED.

    `status()` publishes `alert_delivered` to Mission Control and the
    session summaries as durable proof the operator was actually told. A
    message the desk deliberately dropped must read back as suppressed, in
    its own state, not as delivered.
    """
    from unittest.mock import MagicMock

    from src.cost_circuit import UnavailableLLMCostCircuit
    from src.notifier import SUPPRESSED

    notifier = MagicMock()
    notifier.send.return_value = SUPPRESSED
    circuit = UnavailableLLMCostCircuit(
        RuntimeError("breaker unavailable"),
        notifier=notifier,
    )
    circuit._alert()

    state = circuit.status()
    assert state["alert_delivered"] is False, state
    assert state["alert_suppressed"] is True, state
    # Settled: never retried, so exactly one send for this incident.
    circuit._last_alert_attempt = 0.0
    circuit._alert()
    assert notifier.send.call_count == 1


def test_a_delivered_cost_alert_is_still_reported_as_delivered(tmp_path):
    from unittest.mock import MagicMock

    from src.cost_circuit import UnavailableLLMCostCircuit

    notifier = MagicMock()
    notifier.send.return_value = True
    circuit = UnavailableLLMCostCircuit(
        RuntimeError("breaker unavailable"),
        notifier=notifier,
    )
    circuit._alert()
    state = circuit.status()
    assert state["alert_delivered"] is True
    assert state["alert_suppressed"] is False
