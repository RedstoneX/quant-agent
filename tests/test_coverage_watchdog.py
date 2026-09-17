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

    # The exchange calendar the placement gate reads. A MagicMock's default
    # answer here would be a Mock object, and comparing one to a datetime
    # raises — which the gate treats as "shut", so the default is already
    # safe. Wiring real edges anyway makes the market-hours tests say what
    # they mean instead of relying on a TypeError.
    def _edge(hour, minute):
        def _get(on_date=None):
            if not trading_days or on_date is None:
                return None
            return datetime(
                on_date.year, on_date.month, on_date.day, hour, minute, tzinfo=ET,
            )
        return _get

    broker.get_session_open.side_effect = _edge(9, 30)
    broker.get_session_close.side_effect = _edge(16, 0)
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
    assert "Nothing has been sold, resized or cancelled" in text
    # 06:15 ET is hours before the bell: the alert must say the stop goes
    # back at the open, and must not pretend the overnight gap is solved.
    assert "The market is shut right now" in text
    assert "unprotected OVERNIGHT no matter what" in text
    assert status.repairs == []
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


def test_run_after_the_close_judges_today_not_yesterday(db, state_path):
    """Run by hand at 23:50 ET on Friday (this is exactly how the live probe
    on 2026-09-11 first ran, and it judged Thursday): Friday's session is
    over, so Friday is the day that can have failed to re-place a stop."""
    fri_late = datetime(2026, 9, 11, 23, 50, tzinfo=ET).astimezone(timezone.utc)
    status = coverage_watchdog.check_coverage(
        _orcl_broker(), now=fri_late, sweep_symbol="SGOV", db_path=db, state_path=state_path,
    )
    assert status.trading_day == "2026-09-11"


def test_run_during_the_session_judges_yesterday(db, state_path):
    """At 12:00 ET on Friday the session is live and its sweep still has
    hours to run; the finished day to judge is Thursday."""
    fri_noon = datetime(2026, 9, 11, 12, 0, tzinfo=ET).astimezone(timezone.utc)
    status = coverage_watchdog.check_coverage(
        _orcl_broker(), now=fri_noon, sweep_symbol="SGOV", db_path=db, state_path=state_path,
    )
    assert status.trading_day == "2026-09-10"


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


# ===========================================================================
# 6. It now PUTS THE STOP BACK — item 53, closed 2026-09-14
#
# The load-bearing tests here are the refusals: it must not place into a shut
# market, must not place a second stop over shares a live order already
# covers, and must never reach a path that sells anything. Placing is the
# easy half.
# ===========================================================================

_FRI_1005 = datetime(2026, 9, 11, 10, 5, tzinfo=ET).astimezone(timezone.utc)


def _repairable_broker(**kw):
    """`_orcl_broker` plus the two things a placement needs: a live price and
    the retrying submit. The recorded BUY stop is $137.53, below the price,
    so no guard blocks the repair for the wrong reason."""
    broker = _orcl_broker(**kw)
    broker.get_latest_price.return_value = 150.28
    broker.STOP_LIMIT_BUFFER_PCT = 0.01
    broker._submit_protective_stop_retrying.return_value = {
        "id": "new-day-stop", "uncovered_qty": 0.0, "day_qty": 0.3089,
    }
    return broker


def _last_buy(_symbol, action="BUY"):
    if action == "SHORT":
        return {"stop_loss": 220.0}
    return {"stop_loss": 137.53}


def test_pre_open_run_places_nothing_and_says_the_market_is_shut(db, state_path):
    """06:15 ET, three hours before the bell. The unit this rides on fires
    then, and a fractional DAY stop submitted into a shut market is a
    rejection at best. It must report, not hope."""
    _seed_session(db, source="evening", when=datetime(2026, 9, 3, 0, 3, tzinfo=timezone.utc))
    broker = _repairable_broker()
    status = coverage_watchdog.check_coverage(
        broker, now=_SAT_0615, sweep_symbol="SGOV", db_path=db,
        state_path=state_path, last_buy=_last_buy,
    )
    assert status.market_open is False
    assert "has not opened yet" in status.market_reason
    assert status.repairs == []
    assert not broker._submit_protective_stop_retrying.called
    # and the exposure is still reported
    assert status.should_alert is True


def test_inside_the_session_it_re_places_the_missing_day_stop(db, state_path):
    """Friday 10:05 ET, desk paused, ORCL's 0.3089 remainder bare. The stop
    goes back through the same repair the in-session sweep uses."""
    _seed_session(db, source="evening", when=datetime(2026, 9, 3, 0, 3, tzinfo=timezone.utc))
    broker = _repairable_broker()
    covered = {"n": 0}

    def _snapshot(symbol, side="sell"):
        if symbol != "ORCL":
            return True, []
        if covered["n"] == 0:
            return True, [{"id": "gtc", "qty": 5.0, "stop_price": 137.53}]
        return True, [
            {"id": "gtc", "qty": 5.0, "stop_price": 137.53},
            {"id": "new-day-stop", "qty": 0.3089, "stop_price": 137.53},
        ]

    broker.snapshot_protective_stops.side_effect = _snapshot

    def _place(**kwargs):
        covered["n"] = 1
        return {"id": "new-day-stop", "uncovered_qty": 0.0}

    broker._submit_protective_stop_retrying.side_effect = _place

    status = coverage_watchdog.check_coverage(
        broker, now=_FRI_1005, sweep_symbol="SGOV", db_path=db,
        state_path=state_path, last_buy=_last_buy,
    )
    assert status.market_open is True
    assert [r.symbol for r in status.repaired] == ["ORCL"]
    assert status.repaired[0].qty == pytest.approx(0.3089)
    # The re-read after placing shows the position whole again, so there is
    # no exposure left to alert about.
    assert status.gaps == []
    assert status.should_alert is False
    assert "RE-PLACED" in coverage_watchdog.status_line(status)
    kwargs = broker._submit_protective_stop_retrying.call_args.kwargs
    assert kwargs["symbol"] == "ORCL"
    assert kwargs["qty"] == pytest.approx(0.3089)
    assert kwargs["stop_price"] == pytest.approx(137.53)
    assert kwargs["side"] == "sell"


# ===========================================================================
# 2b. THE 2026-09-17 DEFECT: this repair must defer to a live session
# ===========================================================================
#
# All six session timers shared one tick and, on 2026-09-17, a session's own
# stop-coverage reconcile and this standalone unit's reconcile ran ~90ms
# apart. Nothing needed repairing that day; the same timing with a real gap
# present is how a repair placing a stop collides with a session cancelling
# one to sell. This unit is not a party to run_if_et_window.sh's cross-mode
# session lock, so it must check it explicitly and defer — never skip the
# alert, only the ADD — whenever a session holds it.

def _hold_session_lock(monkeypatch, tmp_path, mode="midday"):
    """Simulate run_if_et_window.sh's acquire_session_lock having the
    named mode's lock currently held, the same directory
    `src.execution.scale_in.trading_session_lock_held` reads."""
    from src.execution import scale_in

    lock_dir = tmp_path / "active-session.lock"
    lock_dir.mkdir()
    (lock_dir / "owner").write_text(f"{mode} 2026-09-17 1000 12345")
    monkeypatch.setattr(scale_in, "_SESSION_LOCK_DIR", lock_dir)
    return lock_dir


def test_repair_defers_while_a_session_holds_the_trading_lock(
    db, state_path, tmp_path, monkeypatch,
):
    """THE INCIDENT, reproduced: a session (midday, that day) holds the
    lock. The standalone coverage-sweep tick must not place its own stop —
    that session already runs the identical repair itself — but the
    exposure must still be read and still reported, not silently dropped.
    """
    _hold_session_lock(monkeypatch, tmp_path, mode="midday")
    _seed_session(db, source="evening", when=datetime(2026, 9, 3, 0, 3, tzinfo=timezone.utc))
    broker = _repairable_broker()

    status = coverage_watchdog.check_coverage(
        broker, now=_FRI_1005, sweep_symbol="SGOV", db_path=db,
        state_path=state_path, last_buy=_last_buy,
    )

    assert not broker._submit_protective_stop_retrying.called, (
        "the repair must not fire while a session holds the trading lock"
    )
    assert status.repairs == []
    assert status.market_open is True
    assert "defer" in status.market_reason.lower()
    # The gap is real and unresolved this tick — it must still be visible,
    # not swallowed by the defer.
    assert status.gaps and status.gaps[0].symbol == "ORCL"
    assert status.should_alert is True


def test_repair_proceeds_once_the_session_lock_is_released(db, state_path):
    """No lock directory at all (the ordinary case, and the case
    immediately after a session finishes) — the repair runs exactly as
    before this change."""
    _seed_session(db, source="evening", when=datetime(2026, 9, 3, 0, 3, tzinfo=timezone.utc))
    broker = _repairable_broker()

    def _place(**kwargs):
        return {"id": "new-day-stop", "uncovered_qty": 0.0}

    broker._submit_protective_stop_retrying.side_effect = _place

    status = coverage_watchdog.check_coverage(
        broker, now=_FRI_1005, sweep_symbol="SGOV", db_path=db,
        state_path=state_path, last_buy=_last_buy,
    )
    assert broker._submit_protective_stop_retrying.called
    assert [r.symbol for r in status.repaired] == ["ORCL"]


def test_repair_defer_does_not_apply_to_intra_check(
    db, state_path, tmp_path, monkeypatch,
):
    """intra_check is deliberately exempt from run_if_et_window.sh's
    session lock, so it never appears as the lock owner here — this proves
    `trading_session_lock_held()` cannot and does not treat intra_check as
    a blocker. (The intra_check-vs-coverage-sweep pairing is instead closed
    by the schedule move in scripts/systemd/quant-agent-intra_check.timer —
    see tests/test_systemd_units.py — since intra_check never takes this
    lock in the first place.)
    """
    from src.execution import scale_in

    lock_dir = tmp_path / "active-session.lock"
    monkeypatch.setattr(scale_in, "_SESSION_LOCK_DIR", lock_dir)
    assert not lock_dir.is_dir()
    assert scale_in.trading_session_lock_held() is False

    _seed_session(db, source="evening", when=datetime(2026, 9, 3, 0, 3, tzinfo=timezone.utc))
    broker = _repairable_broker()

    def _place(**kwargs):
        return {"id": "new-day-stop", "uncovered_qty": 0.0}

    broker._submit_protective_stop_retrying.side_effect = _place

    status = coverage_watchdog.check_coverage(
        broker, now=_FRI_1005, sweep_symbol="SGOV", db_path=db,
        state_path=state_path, last_buy=_last_buy,
    )
    assert broker._submit_protective_stop_retrying.called


def test_it_cannot_double_cover_a_remainder_a_live_order_already_holds(db, state_path):
    """The gap list is a snapshot; the broker is re-read immediately before
    placing. `snapshot_protective_stops` filters Alpaca's OPEN set, which
    includes an order still in flight from an earlier tick — so a remainder
    already covered shrinks the shortfall to zero and zero is not placed."""
    _seed_session(db, source="evening", when=datetime(2026, 9, 3, 0, 3, tzinfo=timezone.utc))
    broker = _repairable_broker()
    reads = {"n": 0}

    def _snapshot(symbol, side="sell"):
        if symbol != "ORCL":
            return True, []
        reads["n"] += 1
        if reads["n"] == 1:            # the survey pass: a gap is real here
            return True, [{"id": "gtc", "qty": 5.0, "stop_price": 137.53}]
        # by the time we go to place, a DAY stop from an earlier tick is live
        return True, [
            {"id": "gtc", "qty": 5.0, "stop_price": 137.53},
            {"id": "in-flight-day", "qty": 0.3089, "stop_price": 137.53},
        ]

    broker.snapshot_protective_stops.side_effect = _snapshot
    status = coverage_watchdog.check_coverage(
        broker, now=_FRI_1005, sweep_symbol="SGOV", db_path=db,
        state_path=state_path, last_buy=_last_buy,
    )
    assert reads["n"] >= 2, "the broker must be re-read right before placing"
    assert not broker._submit_protective_stop_retrying.called
    assert status.repairs == []


def test_a_failed_placement_alerts_and_is_not_swallowed(db, state_path, monkeypatch):
    """A placement that does not land is its own alarm, on its own
    once-a-day marker — the exposure report must not absorb it."""
    _seed_session(db, source="evening", when=datetime(2026, 9, 3, 0, 3, tzinfo=timezone.utc))
    broker = _repairable_broker()
    broker._submit_protective_stop_retrying.return_value = None
    status = coverage_watchdog.check_coverage(
        broker, now=_FRI_1005, sweep_symbol="SGOV", db_path=db,
        state_path=state_path, last_buy=_last_buy,
    )
    assert [r.symbol for r in status.repair_failures] == ["ORCL"]
    assert status.should_alert_repair_failure is True
    text = coverage_watchdog.repair_failure_text(status)
    assert "COULD NOT PUT THE PROTECTIVE STOP BACK" in text and "ORCL" in text
    assert "FAILED to place" in coverage_watchdog.status_line(status)
    # second run the same trading day: still exposed, but not paged twice
    again = coverage_watchdog.check_coverage(
        broker, now=_FRI_1005, sweep_symbol="SGOV", db_path=db,
        state_path=state_path, last_buy=_last_buy,
    )
    assert again.repair_failures and again.should_alert_repair_failure is False


def test_a_placement_error_is_reported_not_swallowed(db, state_path):
    _seed_session(db, source="evening", when=datetime(2026, 9, 3, 0, 3, tzinfo=timezone.utc))
    broker = _repairable_broker()
    broker._submit_protective_stop_retrying.side_effect = RuntimeError("429")
    status = coverage_watchdog.check_coverage(
        broker, now=_FRI_1005, sweep_symbol="SGOV", db_path=db,
        state_path=state_path, last_buy=_last_buy,
    )
    assert status.repair_failures and "429" in status.repair_failures[0].detail


def test_it_never_sells_resizes_or_cancels_anything(db, state_path):
    """A 0% target is read by this desk as 'sell it'. Nothing in this path
    may reach a close, a cancel or an order that is not a protective stop."""
    _seed_session(db, source="evening", when=datetime(2026, 9, 3, 0, 3, tzinfo=timezone.utc))
    broker = _repairable_broker()
    coverage_watchdog.check_coverage(
        broker, now=_FRI_1005, sweep_symbol="SGOV", db_path=db,
        state_path=state_path, last_buy=_last_buy,
    )
    assert not broker.close_position.called
    assert not broker.submit_order.called
    assert not broker.cancel_snapshotted_stops.called
    assert not broker.cancel_open_orders.called
    assert not broker.replace_stop_loss.called


def test_the_cash_sweep_vehicle_is_never_repaired(db, state_path):
    """SGOV is deliberately stopless. It is skipped from the survey, and the
    repair pass skips it a second time."""
    _seed_session(db, source="evening", when=datetime(2026, 9, 3, 0, 3, tzinfo=timezone.utc))
    broker = _repairable_broker()
    coverage_watchdog.check_coverage(
        broker, now=_FRI_1005, sweep_symbol="SGOV", db_path=db,
        state_path=state_path, last_buy=_last_buy,
    )
    placed = [
        c.kwargs.get("symbol")
        for c in broker._submit_protective_stop_retrying.call_args_list
    ]
    assert "SGOV" not in placed


def test_a_naked_short_is_repaired_with_a_buy_stop(db, state_path):
    """Uncovered short → BUY stop at the SHORT row's recorded level.
    Dropping is_short here would take the long path: a SELL stop at the
    BUY row's $137.53 sits below the $200 tape, so the price-side guard
    would pass and the gap would look repaired. Assert side=buy and the
    SHORT row's $220."""
    _seed_session(db, source="evening", when=datetime(2026, 9, 3, 0, 3, tzinfo=timezone.utc))
    broker = _repairable_broker()
    broker.get_positions.return_value = [
        SimpleNamespace(symbol="TSLA", qty=-3.0, current_price=200.0),
    ]
    broker.get_latest_price.return_value = 200.0
    covered = {"n": 0}

    def _snapshot(symbol, side="sell"):
        if symbol != "TSLA":
            return True, []
        assert side == "buy", "a short's protective stops are BUY-side"
        if covered["n"] == 0:
            return True, []
        return True, [{"id": "new-buy-stop", "qty": 3.0, "stop_price": 220.0}]

    broker.snapshot_protective_stops.side_effect = _snapshot

    def _place(**kwargs):
        covered["n"] = 1
        return {"id": "new-buy-stop", "uncovered_qty": 0.0}

    broker._submit_protective_stop_retrying.side_effect = _place
    status = coverage_watchdog.check_coverage(
        broker, now=_FRI_1005, sweep_symbol="SGOV", db_path=db,
        state_path=state_path, last_buy=_last_buy,
    )
    assert status.market_open is True
    assert [r.symbol for r in status.repaired] == ["TSLA"]
    assert status.repaired[0].qty == pytest.approx(3.0)
    assert status.gaps == []
    kwargs = broker._submit_protective_stop_retrying.call_args.kwargs
    assert kwargs["symbol"] == "TSLA"
    assert kwargs["qty"] == pytest.approx(3.0)
    assert kwargs["stop_price"] == pytest.approx(220.0)
    assert kwargs["side"] == "buy"
    assert abs(kwargs["limit_price"] - 220.0 * (1 + broker.STOP_LIMIT_BUFFER_PCT)) < 0.01


def test_heartbeat_last_buy_reader_forwards_short_action(tmp_path, monkeypatch):
    """The 30-minute sweep's production lookup must pass action=SHORT.
    A reader that only queries BUY would leave this SHORT row invisible
    and the watchdog-suite doubles would not catch it."""
    import scripts.alert_heartbeat as hb
    from src.storage.db import Database

    db_path = tmp_path / "quant_agent.db"
    db = Database(str(db_path))
    db.initialize()
    db.insert_trade(
        symbol="TSLA", action="SHORT", qty=3, price=200.0,
        reasoning="opened short", run_id="r1", stop_loss=220.0,
        fill_status="filled",
    )
    monkeypatch.setattr("src.api.deps.get_db_path", lambda: str(db_path))
    _db, reader = hb._coverage_db_and_last_buy()
    assert reader is not None
    row = reader("TSLA", action="SHORT")
    assert row is not None and row["stop_loss"] == 220.0
    assert reader("TSLA") is None  # default BUY must not see the SHORT row


def test_no_recorded_stop_lookup_means_it_stays_a_pure_reader(db, state_path):
    _seed_session(db, source="evening", when=datetime(2026, 9, 3, 0, 3, tzinfo=timezone.utc))
    broker = _repairable_broker()
    status = coverage_watchdog.check_coverage(
        broker, now=_FRI_1005, sweep_symbol="SGOV", db_path=db,
        state_path=state_path, last_buy=None,
    )
    assert not broker._submit_protective_stop_retrying.called
    assert "nothing was placed" in status.market_reason


# ---- the placement gate reads the exchange calendar, and fails closed ----

def test_session_gate_says_shut_on_a_non_trading_day():
    broker = MagicMock()
    broker.is_trading_day.return_value = False
    open_now, reason = coverage_watchdog.session_is_open(broker, _FRI_1005)
    assert open_now is False and "not a trading day" in reason


def test_session_gate_fails_closed_when_the_calendar_cannot_be_read():
    broker = MagicMock()
    broker.is_trading_day.side_effect = RuntimeError("calendar down")
    open_now, reason = coverage_watchdog.session_is_open(broker, _FRI_1005)
    assert open_now is False and "calendar down" in reason


def test_session_gate_fails_closed_when_an_edge_is_missing():
    broker = MagicMock()
    broker.is_trading_day.return_value = True
    broker.get_session_open.return_value = None
    broker.get_session_close.return_value = datetime(2026, 9, 11, 16, 0, tzinfo=ET)
    open_now, reason = coverage_watchdog.session_is_open(broker, _FRI_1005)
    assert open_now is False and "both session edges" in reason


def test_session_gate_respects_an_early_close_from_the_calendar():
    """13:00 on a half-day is the calendar's answer, not ours. Nothing here
    may assume 16:00."""
    broker = MagicMock()
    broker.is_trading_day.return_value = True
    broker.get_session_open.return_value = datetime(2026, 9, 11, 9, 30, tzinfo=ET)
    broker.get_session_close.return_value = datetime(2026, 9, 11, 13, 0, tzinfo=ET)
    after = datetime(2026, 9, 11, 14, 0, tzinfo=ET).astimezone(timezone.utc)
    open_now, reason = coverage_watchdog.session_is_open(broker, after)
    assert open_now is False and "has closed" in reason
    open_now, _ = coverage_watchdog.session_is_open(broker, _FRI_1005)
    assert open_now is True


# ---- the coverage-only entry point ----

def test_coverage_only_runs_the_check_and_never_the_channel_probe(monkeypatch):
    import scripts.alert_heartbeat as hb

    probe = MagicMock()
    monkeypatch.setattr(hb, "run_probe", probe)
    monkeypatch.setattr(hb, "run_coverage_check", lambda: "coverage_watchdog: OK")
    assert hb.main(["--coverage-only"]) == 0
    assert not probe.called


def test_coverage_only_reports_a_broker_it_cannot_build(monkeypatch, capsys):
    import scripts.alert_heartbeat as hb

    def boom():
        raise RuntimeError("no credentials")

    monkeypatch.setattr(hb, "run_coverage_check", boom)
    assert hb.main(["--coverage-only"]) == 1
    assert "no credentials" in capsys.readouterr().err
