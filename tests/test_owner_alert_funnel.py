"""The naked-position page retries, and no new bare ``notifier.send`` appears.

Reproduced before the fix: a notifier whose first send fails made exactly ONE
attempt, returned False, and wrote no undelivered row -- a transient Telegram
fault silently lost the "NO STOP AT ALL" alert. Both halves below go red if the
bare send is put back.
"""
from __future__ import annotations

import textwrap

import pytest

from scripts import owner_alert_funnel_guard as guard
from src.notifier import owner_alert_delivery
from src.trader_feed.naked import send_naked_position_alert

NAKED_RESULT = {
    "run_id": "r1",
    "stop_coverage": {"gaps": [{"symbol": "AAA", "kind": "no_stop_at_all"}]},
}


class _Recorder:
    """A notifier that fails `fail_times` sends, then succeeds."""

    enabled = True

    def __init__(self, fail_times: int) -> None:
        self.fail_times = fail_times
        self.calls = 0
        self.recorded: list[str] = []

    def send(self, _text: str, **_kwargs) -> bool:
        self.calls += 1
        return self.calls > self.fail_times

    def _safe_record_send(self, **kwargs) -> None:
        self.recorded.append(str(kwargs.get("status")))


@pytest.fixture(autouse=True)
def _no_sleeping(monkeypatch):
    monkeypatch.setattr(owner_alert_delivery, "RETRY_DELAYS_S", (0.0, 0.0, 0.0))


def test_a_transient_failure_is_retried_and_the_alert_still_lands():
    notifier = _Recorder(fail_times=1)
    assert send_naked_position_alert(notifier, NAKED_RESULT) is True
    assert notifier.calls == 2, "a failed first send must be retried"
    assert notifier.recorded == []


def test_an_alert_that_never_lands_is_recorded_undelivered():
    notifier = _Recorder(fail_times=99)
    assert send_naked_position_alert(notifier, NAKED_RESULT) is False
    assert notifier.calls == owner_alert_delivery.MAX_ATTEMPTS
    assert owner_alert_delivery.UNDELIVERED_STATUS in notifier.recorded


def test_a_notifier_fault_never_breaks_the_caller():
    class _Exploding:
        enabled = True

        def send(self, *_a, **_k):
            raise RuntimeError("telegram down")

        def _safe_record_send(self, **_k):
            raise RuntimeError("db down too")

    assert send_naked_position_alert(_Exploding(), NAKED_RESULT) is False


def test_the_guard_is_green_on_arrival_and_its_allowances_are_live():
    assert guard.violations() == []
    assert guard.stale_allowances() == []


def test_the_guard_catches_a_bare_send():
    source = textwrap.dedent(
        """
        def page(notifier, text):
            return notifier.send(text, kind="no_stop_at_all")
        """
    )
    assert guard.scan_sites(source) == [(3, "notifier")]


def test_the_guard_ignores_a_funnel_call():
    source = textwrap.dedent(
        """
        def page(notifier, text):
            return deliver_with_retry(notifier, text)
        """
    )
    assert guard.scan_sites(source) == []
