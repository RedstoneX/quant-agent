"""The hanging-endpoint safety test, split out of test_alert_watchdog.py."""

from __future__ import annotations

from unittest.mock import patch

from src import alert_watchdog
from tests.test_alert_watchdog import _run_session, telegram_env  # noqa: F401


def test_a_hanging_telegram_endpoint_cannot_stall_or_fail_a_session(
    tmp_path,
    monkeypatch,
    telegram_env,
):
    """The load-bearing safety test: Telegram goes dark and the session is
    unaffected except for being told the alarm is down.

    Stands in for an endpoint that accepts the connection and never answers.
    `requests` gives up at its timeout and raises; the probe catches it,
    calls the channel broken, and the session finishes normally. A session
    that raised, hung, or exited non-zero here would mean the watchdog can
    cost a trading day, which is a worse defect than the one it fixes.
    """
    import time

    import requests as requests_mod

    calls: list[float] = []

    def hangs_then_times_out(*args, **kwargs):
        # A real hang ends in requests raising at `timeout`; the sleep keeps
        # the test honest about elapsed time without waiting 5s per call.
        assert kwargs.get("timeout"), "a request went out unbounded"
        calls.append(time.monotonic())
        time.sleep(0.05)
        raise requests_mod.exceptions.ReadTimeout("simulated hang")

    from src.notifier import owner_alert_delivery as delivery

    # The naked-position alert now retries through the delivery funnel; its
    # backoff is real seconds in production, zero here (as its own tests do).
    monkeypatch.setattr(delivery, "RETRY_DELAYS_S", (0,) * len(delivery.RETRY_DELAYS_S))
    started = time.monotonic()
    with patch("src.notifier.requests.post", side_effect=hangs_then_times_out):
        # No pytest.raises: the session must complete, not survive an error.
        db_path = _run_session(monkeypatch, tmp_path, mode="intra_check")
    elapsed = time.monotonic() - started

    assert calls, "the watchdog never even tried the channel"
    # Bounded work, not an unbounded retry loop against a dead endpoint.
    # One probe, plus each of the session's two owner sends (the naked-position
    # alert and the plain send) at the funnel's bounded MAX_ATTEMPTS: `send`
    # itself is now the retry funnel, so the plain send retries too.
    ceiling = 1 + 2 * delivery.MAX_ATTEMPTS
    assert len(calls) <= ceiling, f"{len(calls)} requests against a hanging endpoint"
    assert elapsed < 5, f"the session stalled for {elapsed:.1f}s on Telegram"

    # And the outage was still detected, recorded and attributed correctly.
    health = alert_watchdog.read_health(db_path)
    assert health.status == "broken"
    assert health.last_stage == "transport"
