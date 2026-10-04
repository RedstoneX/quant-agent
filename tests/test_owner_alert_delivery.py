"""A failed owner alert is retried, then recorded as a counted durable row."""
import sqlite3

import pytest

from src.notifier import owner_alert, owner_alert_delivery as d
from src.notifier.category import SUPPRESSED


class _Fake:
    enabled = True

    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = 0
        self.rows = []

    def send(self, text, **kw):
        self.calls += 1
        o = self.outcomes.pop(0) if self.outcomes else False
        if isinstance(o, Exception):
            raise o
        return o

    def _safe_record_send(self, **kw):
        self.rows.append(kw)


@pytest.fixture(autouse=True)
def _fast(monkeypatch, tmp_path):
    monkeypatch.setattr(d, "RETRY_DELAYS_S", (0, 0))
    import src.notifier.base as base
    monkeypatch.setattr(base, "_DB_PATH", tmp_path / "n.db")


def test_failed_send_is_retried_and_lands():
    f = _Fake([False, RuntimeError("x"), True])
    assert d.deliver_with_retry(f, "t") is True
    assert f.calls == 3 and f.rows == []


def test_failure_after_retries_writes_counted_row():
    f = _Fake([False, False, False])
    assert d.deliver_with_retry(f, "t") is False
    assert f.calls == d.MAX_ATTEMPTS
    assert len(f.rows) == 1
    assert f.rows[0]["status"] == d.UNDELIVERED_STATUS
    assert "undelivered_total=1" in f.rows[0]["detail"]


def test_count_increments(tmp_path, monkeypatch):
    conn = sqlite3.connect(str(tmp_path / "n.db"))
    conn.execute("CREATE TABLE notifier_sends (status TEXT)")
    conn.execute("INSERT INTO notifier_sends VALUES (?)", (d.UNDELIVERED_STATUS,))
    conn.commit()
    conn.close()
    f = _Fake([False] * 3)
    d.deliver_with_retry(f, "t")
    assert "undelivered_total=2" in f.rows[0]["detail"]


def test_suppressed_is_not_retried_or_recorded():
    f = _Fake([SUPPRESSED])
    assert d.deliver_with_retry(f, "t") is False
    assert f.calls == 1 and f.rows == []


def test_failing_alert_does_not_abort_caller(monkeypatch):
    class Boom(_Fake):
        def _safe_record_send(self, **kw):
            raise RuntimeError("db down")

    monkeypatch.setattr(owner_alert, "TelegramNotifier",
                        lambda: Boom([RuntimeError("net")] * 3))
    assert owner_alert.send_owner_alert("heading\nbody") is False


def test_funnel_never_raises_when_notifier_cannot_build(monkeypatch):
    def _bad():
        raise RuntimeError("no token")
    monkeypatch.setattr(owner_alert, "TelegramNotifier", _bad)
    assert owner_alert.send_owner_alert("x\ny") is False
