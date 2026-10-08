"""The intra-check stops making paid ticks once the exchange's real close has passed."""
from __future__ import annotations

from datetime import date
from types import SimpleNamespace

from src.intraday.session import IntradaySession
from src.session_calendar import et_datetime
from tests.test_session_calendar import EARLY_CLOSE_DAY, NORMAL_DAY, FakeCalendarBroker, _normal_sessions

H13, H14, H16_30 = 13 * 60, 14 * 60, 16 * 60 + 30


def _run(day: date, minute: int, close_minute: int):
    sessions = _normal_sessions(day)
    sessions[day] = (570, close_minute)
    ticks: list[int] = []
    broker = FakeCalendarBroker(sessions)
    broker.get_account = lambda: {"portfolio_value": 1.0, "cash": 1.0}
    broker.get_positions = lambda: []
    session = IntradaySession(
        broker=broker,
        is_trading_day=lambda: True,
        kill_switch_halt_result=lambda run_id: None,
        run_intra_safety_preamble=lambda run_id: ([], ""),
        activate_cost_session=lambda *a: None,
        compute_deployable_cash=lambda *a: 1.0,
        record_account_snapshot=lambda *a: None,
        sync_positions_from_broker=lambda *a: None,
        total_pnl_since_reset=lambda v: (0, 0, None),
        run_intraday_opportunity_scan=lambda ctx: ticks.append(1),
        now=lambda: et_datetime(day, minute),
        state=SimpleNamespace(get=lambda n: None, set=lambda n, v: None),
    )
    return session._run_intra_check_body(), ticks


def test_early_close_day_after_the_real_close_makes_no_tick():
    result, ticks = _run(EARLY_CLOSE_DAY, H13 + 5, H13)
    assert result["status"] == "session_closed"
    assert ticks == []


def test_early_close_day_before_the_close_still_ticks():
    assert _run(EARLY_CLOSE_DAY, H13 - 30, H13)[1] == [1]


def test_normal_day_at_two_pm_ticks():
    result, ticks = _run(NORMAL_DAY, H14, 960)
    assert result["status"] == "ok"
    assert ticks == [1]


def test_normal_day_after_four_pm_does_not_tick():
    result, ticks = _run(NORMAL_DAY, H16_30, 960)
    assert result["status"] == "session_closed"
    assert ticks == []
