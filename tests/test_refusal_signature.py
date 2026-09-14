"""Unvarying-refusal escalation — docs/WORK.md item 59.

The property under test is that the trigger contains NO day count: it fires
on "the reason did not vary while the input did", and it must stay silent
for a quiet market, for a single session however emphatic, and for a desk
whose trading timers are paused.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.refusal_signature import (
    alert_text,
    check_refusal_signature,
    load_sessions,
    normalise,
    signature_key,
    status_line,
    unvarying_streak,
)
from src.trading_calendar import ET

# 2026-09-09 is a Wednesday; 09-10 Thursday, 09-11 Friday. All plain
# weekdays, so `most_recent_trading_day` needs no broker calendar.
WED = "2026-09-09"
THU = "2026-09-10"
FRI = "2026-09-11"


def _et_stamp(day: str, hour: int = 10) -> str:
    """A 10:00 ET session moment for `day`, stored the way the pipeline
    stores it: naive UTC text, exactly `datetime('now')`'s shape."""
    et = datetime.fromisoformat(f"{day}T{hour:02d}:00:00").replace(tzinfo=ET)
    return et.astimezone(timezone.utc).replace(tzinfo=None).isoformat(sep=" ")


def _now_after(day: str) -> datetime:
    """06:15 ET on the morning after `day` — when the heartbeat unit runs."""
    et = datetime.fromisoformat(f"{day}T06:15:00").replace(tzinfo=ET)
    return (et + timedelta(days=1)).astimezone(timezone.utc)


def _make_db(path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(path)
    con.execute(
        "CREATE TABLE specialist_evidence (id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " run_id TEXT NOT NULL, decision_id TEXT, agent_name TEXT NOT NULL,"
        " kind TEXT NOT NULL, scope TEXT NOT NULL, symbol TEXT,"
        " evidence_json TEXT NOT NULL, timestamp TEXT NOT NULL)",
    )
    con.execute(
        "CREATE TABLE trades (id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " symbol TEXT NOT NULL, action TEXT NOT NULL, qty REAL NOT NULL,"
        " price REAL NOT NULL, run_id TEXT, decision_id TEXT,"
        " timestamp TEXT NOT NULL)",
    )
    return con


def _event(con, run_id, day, symbol, payload, hour=10):
    con.execute(
        "INSERT INTO specialist_evidence"
        " (run_id, decision_id, agent_name, kind, scope, symbol,"
        "  evidence_json, timestamp) VALUES (?,?,?,?,?,?,?,?)",
        (run_id, run_id, "pipeline", "pipeline_event", "symbol", symbol,
         json.dumps(payload, sort_keys=True), _et_stamp(day, hour)),
    )


def _refusal(symbol, code="stop_wider_than_instrument_reach", detail=None):
    """The shape `pipeline_stages._record_constructor_drops` writes for a
    refusal the constructor recorded AS DATA."""
    return {
        "stage": "deterministic_gate", "outcome": "blocked",
        "reason": "constructor_refused", "refusal": code,
        "detail": detail if detail is not None else (
            f"Constructor: BUY {symbol} refused — the stop sits 3.42 ATR "
            f"away, wider than the instrument's own reach"
        ),
        "targeted": True,
    }


def _entry(con, run_id, symbol, day):
    con.execute(
        "INSERT INTO trades (symbol, action, qty, price, run_id, timestamp)"
        " VALUES (?,?,?,?,?,?)",
        (symbol, "BUY", 1.0, 10.0, run_id, _et_stamp(day)),
    )


@pytest.fixture()
def db(tmp_path):
    path = tmp_path / "quant_agent.db"
    con = _make_db(path)
    yield con, path
    con.close()


# ---------------------------------------------------------------------------
# the normaliser: merges only, never splits
# ---------------------------------------------------------------------------

def test_numbers_and_the_symbol_are_normalised_away():
    a = normalise("Constructor: BUY AAPL refused — reward:risk 1.12 below 1.50", "AAPL")
    b = normalise("Constructor: BUY MSFT refused — reward:risk 1.31 below 1.50", "MSFT")
    assert a == b
    assert "#" in a and "&" in a


def test_two_different_rules_never_collapse_into_one_key():
    thin = signature_key(_refusal("AAPL", code="insufficient_history"), "AAPL")
    wide = signature_key(_refusal("AAPL"), "AAPL")
    assert thin != wide


def test_a_reason_naming_a_second_symbol_stays_distinct():
    """A known, deliberate false negative — recorded, not papered over."""
    a = signature_key(_refusal("AAPL", detail="AAPL rejected — sector holds NVDA"), "AAPL")
    b = signature_key(_refusal("MSFT", detail="MSFT rejected — sector holds TSLA"), "MSFT")
    assert a != b


# ---------------------------------------------------------------------------
# reading sessions out of the evidence stream
# ---------------------------------------------------------------------------

def test_last_event_for_a_symbol_wins(db):
    con, path = db
    _event(con, "r1", WED, "AAPL", {"stage": "specialist", "outcome": "evaluated",
                                    "reason": "technical_analysis_validated"})
    _event(con, "r1", WED, "AAPL", _refusal("AAPL"))
    con.commit()
    sessions, err = load_sessions(path)
    assert err is None
    assert len(sessions) == 1
    assert sessions[0].is_monomorphic
    assert "constructor_refused" in next(iter(sessions[0].distinct_keys))


def test_a_run_with_an_entry_is_not_empty(db):
    con, path = db
    _event(con, "r1", WED, "AAPL", _refusal("AAPL"))
    _entry(con, "r1", "AAPL", WED)
    con.commit()
    sessions, _ = load_sessions(path)
    assert sessions[0].placed_entry is True
    assert unvarying_streak(sessions) == []


def test_a_surviving_candidate_ends_the_streak(db, tmp_path):
    """Measured on the real 2026-09-02 close run: the cash-sweep vehicle
    fills without ever writing a BUY row, so the trades table alone would
    have called that session "refused everything"."""
    con, path = db
    for run, day, syms in (("r1", WED, ["AAPL"]), ("r2", THU, ["MSFT"])):
        for sym in syms:
            _event(con, run, day, sym, _refusal(sym))
    _event(con, "r3", FRI, "SGOV", {
        "stage": "order", "outcome": "filled", "reason": "broker_reconciliation",
    })
    con.commit()
    sessions, _ = load_sessions(path)
    assert sessions[-1].any_survived is True
    assert unvarying_streak(sessions) == []
    assert check_refusal_signature(
        now=_now_after(FRI), db_path=path, state_path=tmp_path / "state.json",
    ).should_alert is False


def test_runs_that_considered_nothing_are_absent(db):
    con, path = db
    con.execute(
        "INSERT INTO specialist_evidence (run_id, agent_name, kind, scope,"
        " symbol, evidence_json, timestamp) VALUES (?,?,?,?,?,?,?)",
        ("r0", "pipeline", "pipeline_event", "run", None, "{}", _et_stamp(WED)),
    )
    con.commit()
    sessions, _ = load_sessions(path)
    assert sessions == []


# ---------------------------------------------------------------------------
# the trigger
# ---------------------------------------------------------------------------

def _jammed(con):
    """Three sessions, changing candidates, one unvarying reason."""
    for run, day, syms in (
        ("r1", WED, ["AAPL", "MSFT"]),
        ("r2", THU, ["AAPL", "NVDA"]),
        ("r3", FRI, ["TSLA", "NVDA", "AMD"]),
    ):
        for sym in syms:
            _event(con, run, day, sym, _refusal(sym))
    con.commit()


def test_unvarying_refusal_with_changing_candidates_alerts(db, tmp_path):
    con, path = db
    _jammed(con)
    status = check_refusal_signature(
        now=_now_after(FRI), db_path=path, state_path=tmp_path / "state.json",
    )
    assert status.should_alert is True
    assert len(status.streak) == 3
    assert status.inputs_varied is True
    assert status.current is True
    assert "SAME reason" in alert_text(status)
    assert "JAMMED" in status_line(status)


def test_one_varied_session_ends_the_streak_and_the_alarm(db, tmp_path):
    con, path = db
    _jammed(con)
    # Friday: one name dies for a different reason. A quiet market's shape.
    _event(con, "r4", FRI, "AAPL", _refusal("AAPL"))
    _event(con, "r4", FRI, "MSFT", _refusal("MSFT", code="insufficient_history"))
    con.commit()
    status = check_refusal_signature(
        now=_now_after(FRI), db_path=path, state_path=tmp_path / "state.json",
    )
    assert status.streak == []
    assert status.should_alert is False


def test_identical_candidate_sets_do_not_alert(db, tmp_path):
    """The same two names refused the same way is not evidence the reason
    is unvarying — the input never varied either."""
    con, path = db
    for run, day in (("r1", WED), ("r2", THU), ("r3", FRI)):
        for sym in ("AAPL", "MSFT"):
            _event(con, run, day, sym, _refusal(sym))
    con.commit()
    status = check_refusal_signature(
        now=_now_after(FRI), db_path=path, state_path=tmp_path / "state.json",
    )
    assert len(status.streak) == 3
    assert status.inputs_varied is False
    assert status.should_alert is False


def test_a_single_session_never_alerts(db, tmp_path):
    con, path = db
    for sym in ("AAPL", "MSFT", "NVDA"):
        _event(con, "r1", FRI, sym, _refusal(sym))
    con.commit()
    status = check_refusal_signature(
        now=_now_after(FRI), db_path=path, state_path=tmp_path / "state.json",
    )
    assert status.should_alert is False
    assert status.inputs_varied is False


def test_a_paused_desk_never_alerts(db, tmp_path):
    """The streak is real but stale: no session ran on the most recent
    trading day, so the desk is not running and this is not a defect."""
    con, path = db
    _jammed(con)
    later = _now_after(FRI) + timedelta(days=7)
    status = check_refusal_signature(
        now=later, db_path=path, state_path=tmp_path / "state.json",
    )
    assert len(status.streak) == 3
    assert status.current is False
    assert status.should_alert is False
    assert "not running sessions" in status_line(status)


def test_it_arms_itself_when_sessions_return(db, tmp_path):
    """No flag to flip: the same streak becomes current the moment a
    session lands on the most recent trading day."""
    con, path = db
    _jammed(con)
    state = tmp_path / "state.json"
    assert check_refusal_signature(
        now=_now_after(FRI) + timedelta(days=7), db_path=path, state_path=state,
    ).should_alert is False
    assert check_refusal_signature(
        now=_now_after(FRI), db_path=path, state_path=state,
    ).should_alert is True


def test_at_most_one_alert_per_trading_day(db, tmp_path):
    """docs/WORK.md item 41's existing cadence, not a new one."""
    con, path = db
    _jammed(con)
    state = tmp_path / "state.json"
    first = check_refusal_signature(now=_now_after(FRI), db_path=path, state_path=state)
    second = check_refusal_signature(now=_now_after(FRI), db_path=path, state_path=state)
    assert first.should_alert is True
    assert second.should_alert is False
    assert second.is_jammed is True
    assert "already alerted" in status_line(second)


def test_state_file_records_the_verdict(db, tmp_path):
    con, path = db
    _jammed(con)
    state = tmp_path / "state.json"
    check_refusal_signature(now=_now_after(FRI), db_path=path, state_path=state)
    saved = json.loads(state.read_text())
    assert saved["last_result"]["streak_runs"] == ["r1", "r2", "r3"]
    assert saved["alerted_for_day"] is not None


def test_an_unreadable_database_never_alerts(tmp_path):
    status = check_refusal_signature(
        now=_now_after(FRI),
        db_path=tmp_path / "does-not-exist.db",
        state_path=tmp_path / "state.json",
    )
    assert status.db_error is not None
    assert status.should_alert is False
    assert "could NOT read" in status_line(status)


def test_the_alert_names_no_day_count(db, tmp_path):
    """The message must not read as "N days" — the whole point of the
    item is that no such number exists."""
    con, path = db
    _jammed(con)
    text = alert_text(check_refusal_signature(
        now=_now_after(FRI), db_path=path, state_path=tmp_path / "state.json",
    ))
    assert "not a count of empty days" in text
    assert "the reason never varied while the input did" in text
