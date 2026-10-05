"""A failed owner-alert send must not consume the day's elected-unfilled claim."""
from __future__ import annotations

import pytest

from src import coverage_watchdog, notifier


@pytest.fixture
def state_path(tmp_path, monkeypatch):
    path = tmp_path / "alerting" / "coverage_heartbeat.json"
    monkeypatch.setattr("src.coverage_watchdog_state.STATE_PATH", path)
    return path


def test_failed_send_does_not_leave_the_elected_unfilled_day_claimed(
    state_path, monkeypatch,
):
    """The claim is written before the send; a failed send must roll it back
    so the next run retries instead of treating the alert as delivered."""
    import src.pipeline_protection as pp

    monkeypatch.setattr(notifier, "send_owner_alert", lambda *a, **k: False)
    rows = [{"symbol": "ZZZZ", "stop_price": 10.0, "price": 9.0}]
    cls = next(
        c for c in vars(pp).values()
        if isinstance(c, type) and "_alert_owner_elected_unfilled" in vars(c)
    )
    cls._alert_owner_elected_unfilled(rows)

    state = coverage_watchdog.load_state(state_path)
    claimed = coverage_watchdog._elected_unfilled_alerted_symbols(
        state, coverage_watchdog.repair_failure_alert_day(None),
    )
    assert "ZZZZ" not in claimed, "a failed send must not consume the day"
    assert coverage_watchdog.claim_elected_unfilled_alert(["ZZZZ"]) == ["ZZZZ"]
