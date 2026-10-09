"""A naked position is paged EVERY session of the day, risk-only or not.

Defect 1 of the PR #978 adversary review. The `🛑🛑🛑 NO STOP AT ALL`
banner had exactly two carriers: the pipeline escalation, which claims the
symbol for the whole trading DAY and returns early thereafter, and the
session summary, which the per-category mute classifies as operational and
drops. A position still naked at midday, at the close and in the evening
therefore produced nothing the owner could see. These tests pin the
third, unconditional carrier: its own message, every session, no daily
claim, no category (so it resolves `risk` and survives the filter).
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
)
from src.trader_feed import (
    naked_position_alert,
    send_naked_position_alert,
    uncovered_stop_gaps,
)

NAKED = {
    "stop_coverage_gaps": [
        {"symbol": "AAPL", "coverage": "none", "held_qty": 10, "covered_qty": 0},
    ],
}


@pytest.fixture
def creds(monkeypatch, tmp_path):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "12345")
    monkeypatch.delenv("TELEGRAM_DISABLED", raising=False)
    monkeypatch.setenv("TELEGRAM_RISK_ONLY", "1")
    monkeypatch.setattr(notifier_mod, "_DB_PATH", tmp_path / "qamc.db")
    monkeypatch.setattr(notifier_mod, "_REHEARSAL_MODE", False, raising=False)
    return tmp_path / "qamc.db"


def test_kind_resolves_to_risk_so_the_filter_cannot_drop_it():
    assert resolve_category(None, "no_stop_at_all") == CATEGORY_RISK


def test_the_session_summary_itself_is_still_operational():
    # The switch exists to stop this stream; the fix must not re-admit it.
    for kind in ("morning", "midday", "close", "evening", "intra_check"):
        assert resolve_category(None, kind) == CATEGORY_OPERATIONAL


def test_banner_text_is_produced_only_when_something_is_naked():
    assert naked_position_alert(NAKED)
    assert "NO STOP AT ALL" in naked_position_alert(NAKED)
    assert naked_position_alert({"stop_coverage_gaps": []}) is None
    assert naked_position_alert(None) is None
    # An UNREADABLE row asserts nothing about coverage (board item 172).
    assert (
        naked_position_alert(
            {
                "stop_coverage_gaps": [
                    {"symbol": "MSFT", "coverage": "unreadable", "read_error": "timeout"},
                ]
            }
        )
        is None
    )
    assert [g["symbol"] for g in uncovered_stop_gaps(NAKED)] == ["AAPL"]


def test_second_and_third_session_of_the_same_day_still_page(creds, monkeypatch):
    """THE POINT OF THE FIX. Three sessions, one day, one still-naked
    position, TELEGRAM_RISK_ONLY on: three delivered Telegram messages."""
    delivered = []
    with patch("src.notifier.requests.post") as post:
        post.return_value = MagicMock(status_code=200, json=lambda: {"ok": True})
        post.return_value.raise_for_status = MagicMock()
        for _ in ("morning", "midday", "close", "evening"):
            notifier = TelegramNotifier()
            assert notifier.risk_only is True
            assert send_naked_position_alert(notifier, NAKED) is True
            delivered.append(post.call_count)
    assert delivered == [1, 2, 3, 4], delivered

    conn = sqlite3.connect(str(creds))
    try:
        rows = conn.execute("SELECT kind, status FROM notifier_sends ORDER BY id").fetchall()
    finally:
        conn.close()
    assert rows == [("no_stop_at_all", "sent")] * 4, rows


def test_nothing_is_sent_when_no_position_is_naked(creds):
    with patch("src.notifier.requests.post") as post:
        post.return_value = MagicMock(status_code=200, json=lambda: {"ok": True})
        assert send_naked_position_alert(TelegramNotifier(), {"stop_coverage_gaps": []}) is False
        assert post.call_count == 0


def test_a_broken_notifier_cannot_break_the_session(creds):
    boom = MagicMock()
    boom.send.side_effect = RuntimeError("telegram exploded")
    assert send_naked_position_alert(boom, NAKED) is False


def test_a_session_that_died_still_tells_the_owner_something(creds):
    """PR #978 defect D: silence must never be the output of a dead session.

    `_run_safe`'s except leaves `result=None`, and the early returns in
    src/pipeline.py (`evidence_gate_skip`, `broker_error`, `no_data`) omit
    `stop_coverage_gaps` entirely. Those are exactly the sessions in which
    protection is doubtful, so they must say so rather than say nothing --
    and must NOT invent a default of "everything is fine".
    """
    from src.trader_feed import protection_undetermined_alert

    for result in (None, {"status": "broker_error"}, {"status": "no_data"}, {"status": "evidence_gate_skip"}):
        text = protection_undetermined_alert(result)
        assert text is not None, result
        assert "UNDETERMINED" in text
        assert "not an all-clear" in text
        sender = MagicMock()
        sender.send.return_value = True
        assert send_naked_position_alert(sender, result) is True
        (sent_text,), _kwargs = sender.send.call_args
        assert "UNDETERMINED" in sent_text


def test_an_audited_session_with_no_gaps_is_not_called_undetermined(creds):
    from src.trader_feed import protection_undetermined_alert

    assert protection_undetermined_alert({"stop_coverage_gaps": []}) is None
