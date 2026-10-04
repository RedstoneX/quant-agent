"""The stream-attempt budget and the credential once-a-day marker key on the
EXCHANGE's day, not the runner's local day or the UTC day.

Driven INSIDE the broken window: 23:58 ET (already the next UTC day) and
00:05 ET, by moving the one exchange clock the repo owns (trading_calendar.et_now).
"""
from datetime import datetime

import pytest

import src.credentials as credentials
import src.trading_calendar as tc
from src.execution.broker_parts.trade_stream import _StreamAttemptBudget

ET = tc.ET


def _at(monkeypatch, y, mo, d, h, mi):
    when = datetime(y, mo, d, h, mi, tzinfo=ET)
    monkeypatch.setattr(tc, "et_now", lambda: when)


@pytest.mark.parametrize("h,mi,day", [(23, 58, "2026-03-09"), (0, 5, "2026-03-10")])
def test_credentials_day_is_the_exchange_day(monkeypatch, h, mi, day):
    _at(monkeypatch, 2026, 3, int(day[-2:]), h, mi)
    assert credentials._today().isoformat() == day


def test_stream_budget_rolls_on_exchange_midnight_not_utc(monkeypatch):
    budget = _StreamAttemptBudget()
    _at(monkeypatch, 2026, 3, 9, 23, 58)  # UTC is already 03-10 here
    budget.record_attempt()
    assert budget.attempts_today() == 1
    _at(monkeypatch, 2026, 3, 10, 0, 5)
    assert budget.attempts_today() == 0
