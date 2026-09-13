"""Stop-coverage watchdog for a paused desk — docs/INCIDENT_HISTORY.md 2026-09-12.

THE REAL CASE, reproduced exactly: ORCL 5.3089 shares held, one GTC stop
for 5.0 at the broker, the 0.3089-share DAY leg lapsed at the close on
2026-09-02, and no session ran on any trading day afterwards because the
timers were paused. Every record on the box called that "expected
overnight"; nobody was told for six sessions.

THE LOAD-BEARING TESTS ARE THE NEGATIVE ONES, as in the sibling watchdog
suites: the ordinary healthy morning (remainder lapsed, session ran
yesterday) MUST stay silent — the owner ratified that a nightly lapse does
not page — and the alert must fire exactly when the design's own
precondition ("the next session re-places it") has been false for a whole
trading session.

Nothing here touches the network, the broker, or a real Telegram chat.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from src import alert_watchdog, coverage_watchdog
from src.trading_calendar import ET

# 2026-09-11 was a Friday; the check runs 06:15 ET the next morning
# (Saturday) and must judge Friday's session. Saturday is deliberate: the
# most-recent-trading-day walk has to skip nothing here, and the Monday
# case below covers the weekend skip.
_SAT_0615 = datetime(2026, 9, 12, 6, 15, tzinfo=ET).astimezone(timezone.utc)
_MON_0615 = datetime(2026, 9, 14, 6, 15, tzinfo=ET).astimezone(timezone.utc)
_FRI = datetime(2026, 9, 11, tzinfo=ET)


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = tmp_path / "quant_agent.db"
    monkeypatch.setattr(alert_watchdog, "DB_PATH", path)
    monkeypatch.setattr(coverage_watchdog, "DB_PATH", path)
    return path


@pytest.fixture
def state_path(tmp_path, monkeypatch):
    path = tmp_path / "alerting" / "coverage_heartbeat.json"
    monkeypatch.setattr(coverage_watchdog, "STATE_PATH", path)
    return path


def _seed_session(db_path, *, source: str, when: datetime) -> None:
    alert_watchdog.record_check(
        ok=True, stage="delivered", detail="", source=source,
        db_path=db_path, now=when,
    )


def _orcl_broker(held=5.3089, stops=(5.0,), price=150.28, trading_days=True):
    """The live 2026-09-12 broker state, read-only: 5.3089 ORCL held, one
    GTC stop-limit for 5.0, plus the stopless SGOV cash sweep."""
    broker = MagicMock()
    broker.get_positions.return_value = [
        SimpleNamespace(symbol="ORCL", qty=held, current_price=price),
        SimpleNamespace(symbol="SGOV", qty=89.0, current_price=100.52),
    ]

    def _snapshot(symbol, side="sell"):
        if symbol == "ORCL":
            return True, [{"id": f"stop-{i}", "qty": q, "stop_price": 137.53} for i, q in enumerate(stops)]
        return True, []

    broker.snapshot_protective_stops.side_effect = _snapshot
    broker.is_trading_day.return_value = trading_days
    return broker


# ===========================================================================
# 1. The real incident: fires
# ===========================================================================

def test_the_orcl_case_alerts_when_no_session_ran_on_the_last_trading_day(db, state_path):
    """ORCL 5.3089 held, 5.0 covered, timers paused: no session on Friday.
    This is the state the box was in on 2026-09-12 and nothing said so.
    The last real session was the 2026-09-02 evening run (00:03 UTC 09-03)."""
    _seed_session(db, source="evening", when=datetime(2026, 9, 3, 0, 3, tzinfo=timezone.utc))
    broker = _orcl_broker()
    status = coverage_watchdog.check_coverage(
        broker, now=_SAT_0615, sweep_symbol="SGOV", db_path=db, state_path=state_path,
    )
    assert status.trading_day == "2026-09-11"
    assert status.session_ran is False
    assert [g.symbol for g in status.gaps] == ["ORCL"]
    gap = status.gaps[0]
    assert gap.held_qty == pytest.approx(5.3089)
    assert gap.covered_qty == pytest.approx(5.0)
    assert gap.uncovered_qty == pytest.approx(0.3089)
    assert gap.unprotected_value == pytest.approx(46.42, abs=0.01)  # 0.3089 x $150.28
    assert status.should_alert is True
    text = coverage_watchdog.alert_text(status)
    assert "UNPROTECTED" in text and "ORCL" in text and "$46.42" in text
    assert "Nothing has been placed, changed or cancelled" in text
    # Read-only against the broker, always.
    assert not broker.submit_order.called
    assert not broker.cancel_order_by_id.called


def test_the_cash_sweep_vehicle_is_never_a_gap(db, state_path):
    """SGOV deliberately carries no stop. Flagging it would page every day."""
    broker = _orcl_broker(stops=(5.0, 0.3089))
    status = coverage_watchdog.check_coverage(
        broker, now=_SAT_0615, sweep_symbol="SGOV", db_path=db, state_path=state_path,
    )
    assert status.gaps == []
    assert status.should_alert is False


# ===========================================================================
# 2. The healthy morning: silent (owner-ratified)
# ===========================================================================

def test_a_lapsed_remainder_after_a_normal_session_day_stays_silent(db, state_path):
    """The DAY leg lapses every night on a healthy desk. A session ran during
    Friday's cash session, so Monday's sweep owns the re-placement. NOT an
    alert — this is the case the owner said must never page."""
    _seed_session(db, source="intra_check", when=_FRI.replace(hour=12, minute=1).astimezone(timezone.utc))
    broker = _orcl_broker()
    status = coverage_watchdog.check_coverage(
        broker, now=_SAT_0615, sweep_symbol="SGOV", db_path=db, state_path=state_path,
    )
    assert status.gaps and status.session_ran is True
    assert status.should_alert is False
    assert "not alerting" in coverage_watchdog.status_line(status)


def test_only_a_session_inside_cash_hours_counts_as_evidence(db, state_path):
    """The evening run sees a shut market and places nothing (by design), so
    an evening row alone does not prove the remainder was re-covered."""
    _seed_session(db, source="evening", when=_FRI.replace(hour=20, minute=1).astimezone(timezone.utc))
    _seed_session(db, source="earnings_preprocess", when=_FRI.replace(hour=8, minute=1).astimezone(timezone.utc))
    status = coverage_watchdog.check_coverage(
        _orcl_broker(), now=_SAT_0615, sweep_symbol="SGOV", db_path=db, state_path=state_path,
    )
    assert status.session_ran is False
    assert status.should_alert is True


def test_the_heartbeat_probe_itself_is_not_a_session(db, state_path):
    _seed_session(db, source="heartbeat_timer", when=_FRI.replace(hour=12).astimezone(timezone.utc))
    status = coverage_watchdog.check_coverage(
        _orcl_broker(), now=_SAT_0615, sweep_symbol="SGOV", db_path=db, state_path=state_path,
    )
    assert status.session_ran is False


def test_fully_covered_book_is_clean(db, state_path):
    broker = _orcl_broker(stops=(5.0, 0.3089))
    status = coverage_watchdog.check_coverage(
        broker, now=_SAT_0615, sweep_symbol="SGOV", db_path=db, state_path=state_path,
    )
    assert status.gaps == [] and status.should_alert is False
    assert coverage_watchdog.status_line(status).startswith("coverage_watchdog: OK")


# ===========================================================================
# 3. Which day is judged
# ===========================================================================

def test_monday_morning_judges_friday_not_the_weekend(db, state_path):
    status = coverage_watchdog.check_coverage(
        _orcl_broker(), now=_MON_0615, sweep_symbol="SGOV", db_path=db, state_path=state_path,
    )
    assert status.trading_day == "2026-09-11"


def test_a_market_holiday_is_skipped_via_the_broker_calendar(db, state_path):
    """2026-09-07 (Labor Day) is a weekday the exchange was shut. Tuesday
    06:15 must judge the preceding Friday, not the holiday."""
    broker = _orcl_broker()
    broker.is_trading_day.side_effect = lambda d: d.isoformat() != "2026-09-07"
    tue = datetime(2026, 9, 8, 6, 15, tzinfo=ET).astimezone(timezone.utc)
    status = coverage_watchdog.check_coverage(
        broker, now=tue, sweep_symbol="SGOV", db_path=db, state_path=state_path,
    )
    assert status.trading_day == "2026-09-04"


def test_a_dead_calendar_falls_back_to_the_weekday_and_still_alerts(db, state_path):
    """`is_trading_day` answers False on a calendar failure. That must not
    walk past every real day and suppress the alert."""
    status = coverage_watchdog.check_coverage(
        _orcl_broker(trading_days=False), now=_SAT_0615, sweep_symbol="SGOV",
        db_path=db, state_path=state_path,
    )
    assert status.trading_day == "2026-09-11"
    assert status.should_alert is True


# ===========================================================================
# 4. Once per trading day, and failures stay on the alerting side
# ===========================================================================

def test_alerts_once_per_trading_day_then_again_the_next_day(db, state_path):
    broker = _orcl_broker()
    first = coverage_watchdog.check_coverage(broker, now=_SAT_0615, sweep_symbol="SGOV", db_path=db, state_path=state_path)
    again = coverage_watchdog.check_coverage(broker, now=_SAT_0615 + timedelta(hours=1), sweep_symbol="SGOV", db_path=db, state_path=state_path)
    monday = coverage_watchdog.check_coverage(broker, now=_MON_0615, sweep_symbol="SGOV", db_path=db, state_path=state_path)
    tuesday = coverage_watchdog.check_coverage(broker, now=_MON_0615 + timedelta(days=1), sweep_symbol="SGOV", db_path=db, state_path=state_path)
    assert first.should_alert is True
    assert again.should_alert is False and again.already_alerted_for_day is True
    assert monday.should_alert is False          # still judging Friday
    assert tuesday.trading_day == "2026-09-14" and tuesday.should_alert is True


def test_an_unreadable_database_cannot_prove_a_session_and_alerts(state_path, tmp_path):
    status = coverage_watchdog.check_coverage(
        _orcl_broker(), now=_SAT_0615, sweep_symbol="SGOV",
        db_path=tmp_path / "does-not-exist.db", state_path=state_path,
    )
    assert status.session_ran is None and status.db_error
    assert status.should_alert is True
    assert "could not be read" in coverage_watchdog.alert_text(status)


def test_a_broker_failure_reports_could_not_check_never_clean(db, state_path):
    broker = _orcl_broker()
    broker.get_positions.side_effect = RuntimeError("401")
    status = coverage_watchdog.check_coverage(
        broker, now=_SAT_0615, sweep_symbol="SGOV", db_path=db, state_path=state_path,
    )
    assert status.gaps == [] and status.broker_error
    assert status.should_alert is False
    assert "could NOT check" in coverage_watchdog.status_line(status)


def test_a_short_is_checked_against_buy_stops(db, state_path):
    broker = MagicMock()
    broker.get_positions.return_value = [SimpleNamespace(symbol="XYZ", qty=-10.0, current_price=20.0)]
    broker.snapshot_protective_stops.return_value = (True, [{"qty": 10.0}])
    broker.is_trading_day.return_value = True
    status = coverage_watchdog.check_coverage(
        broker, now=_SAT_0615, sweep_symbol="SGOV", db_path=db, state_path=state_path,
    )
    assert broker.snapshot_protective_stops.call_args.kwargs["side"] == "buy"
    assert status.gaps == []


# ===========================================================================
# 5. Wired into the daily heartbeat, and it cannot change the probe's verdict
# ===========================================================================

def test_heartbeat_runs_the_coverage_check_after_the_probe_and_keeps_its_own_exit_code(monkeypatch, capsys):
    import scripts.alert_heartbeat as hb

    monkeypatch.setattr(hb, "run_probe", lambda: (0, "alert_heartbeat: ok"))
    calls = []

    def fake_coverage():
        calls.append(1)
        raise RuntimeError("broker exploded")

    monkeypatch.setattr(hb, "run_coverage_check", fake_coverage)
    assert hb.main([]) == 0
    assert calls == [1]
    out = capsys.readouterr()
    assert "coverage_watchdog" in out.out + out.err


def test_heartbeat_status_mode_does_not_touch_the_broker(monkeypatch):
    import scripts.alert_heartbeat as hb

    monkeypatch.setattr(hb, "run_status", lambda: (0, "status"))
    called = MagicMock()
    monkeypatch.setattr(hb, "run_coverage_check", called)
    assert hb.main(["--status"]) == 0
    assert not called.called


def test_run_coverage_check_sends_the_owner_alert_exactly_when_exposed(monkeypatch, db, state_path):
    import scripts.alert_heartbeat as hb

    monkeypatch.setattr(hb, "_build_broker", lambda: _orcl_broker())
    monkeypatch.setattr(hb, "_cash_sweep_symbol", lambda: "SGOV")
    sent = []
    with patch("src.notifier.send_owner_alert", side_effect=lambda text, **kw: sent.append(text) or True):
        line = hb.run_coverage_check(now=_SAT_0615)
    assert len(sent) == 1 and "ORCL" in sent[0]
    assert "EXPOSED" in line and "delivered" in line
