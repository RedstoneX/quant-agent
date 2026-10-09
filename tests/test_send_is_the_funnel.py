"""A notifier built anywhere cannot escape the retry-and-record discipline.

Before this, the discipline lived only in `send_owner_alert`; code that built
its own `TelegramNotifier` and called `send` got one attempt and left no trace
of the loss. `send` IS the funnel now, so there is nothing left to bypass.
"""

import pytest

from src.notifier import TelegramNotifier
from src.notifier import owner_alert_delivery as d
from src.notifier.category import SUPPRESSED


@pytest.fixture(autouse=True)
def _fast_and_isolated(monkeypatch, tmp_path):
    monkeypatch.setattr(d, "RETRY_DELAYS_S", (0, 0))
    import src.notifier.base as base

    monkeypatch.setattr(base, "_DB_PATH", tmp_path / "n.db")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "c")


def _notifier(monkeypatch, attempts, outcome):
    n = TelegramNotifier()
    assert n.enabled
    rows = []
    monkeypatch.setattr(
        type(n),
        "send_once",
        lambda self, text, **kw: (attempts.append(text), outcome)[1],
    )
    monkeypatch.setattr(type(n), "_safe_record_send", lambda self, **kw: rows.append(kw))
    return n, rows


def test_a_direct_send_retries_and_records_the_loss(monkeypatch):
    attempts = []
    n, rows = _notifier(monkeypatch, attempts, False)
    assert n.send("naked position", kind="owner_alert") is False
    assert len(attempts) == d.MAX_ATTEMPTS
    assert [r["status"] for r in rows] == [d.UNDELIVERED_STATUS]


def test_a_delivered_send_is_attempted_once(monkeypatch):
    attempts = []
    n, rows = _notifier(monkeypatch, attempts, True)
    assert n.send("fine") is True
    assert len(attempts) == 1 and rows == []


def test_a_deliberate_suppression_is_settled_not_retried(monkeypatch):
    attempts = []
    n, rows = _notifier(monkeypatch, attempts, SUPPRESSED)
    assert n.send("muted") is SUPPRESSED
    assert len(attempts) == 1 and rows == []
