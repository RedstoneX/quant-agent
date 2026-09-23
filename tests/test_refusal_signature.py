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
    streak_and_skipped,
    unvarying_streak,
    _plain_key,
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


# ---------------------------------------------------------------------------
# a beginning is not an end (2026-09-22 false alert, both halves)
# ---------------------------------------------------------------------------
#
# What the owner was sent on 2026-09-22, and why every clause of it was
# false:
#
#   🔴 THE DESK HAS REFUSED EVERY IDEA FOR THE SAME REASON
#   Across the last 6 session(s) with candidates, every single candidate was
#   refused, and every refusal carried the SAME reason ...
#   The one reason: the desk recorded a reason it has no plain wording for.
#   (... opportunity|discovered|intraday_move_threshold||)
#
# Nothing was refused. The six runs were the 17:15–19:45 UTC intra_check
# ticks, every one of which the cost circuit suspended at paid-analysis
# entry; each had already told the owner so in its own message. The only
# pipeline_event any of them wrote was the DISCOVERY of a mover — SNDK
# 6.69% past a 3.0% threshold — which says the desk found the name, not
# that it turned it down. And the reason it claimed to have no wording for
# was sitting in the key: `intraday_move_threshold`.

def _report_tables(con) -> None:
    """The two run-scoped report tables the desk records its own verdict in.

    Deliberately NOT created by `_make_db`, so every test above this point
    still exercises the no-report-tables path a fresh database presents.
    """
    con.execute(
        "CREATE TABLE intra_check_reports (run_id TEXT PRIMARY KEY,"
        " date TEXT NOT NULL, payload_json TEXT NOT NULL,"
        " positions_json TEXT, timestamp TEXT NOT NULL)",
    )
    con.execute(
        "CREATE TABLE session_reports (date TEXT NOT NULL, mode TEXT NOT NULL,"
        " run_id TEXT, payload_json TEXT NOT NULL, positions_json TEXT,"
        " timestamp TEXT NOT NULL, PRIMARY KEY (date, mode))",
    )


def _intra_report(con, run_id, day, scan_status, hour=10):
    con.execute(
        "INSERT INTO intra_check_reports (run_id, date, payload_json,"
        " positions_json, timestamp) VALUES (?,?,?,?,?)",
        (run_id, day, json.dumps({
            "status": "ok",
            "run_id": run_id,
            # The tick itself completed — the deterministic loss check is
            # never the part that gets suspended. The scan's own nested
            # status is the half that either reached a decision or did not,
            # which is exactly why this module reads the nested one.
            "intraday_scan": {"status": scan_status, "run_id": run_id},
        }), None, _et_stamp(day, hour)),
    )


def _discovery(move_pct):
    """`opportunity|discovered` — what `_run_intraday_opportunity_scan`
    writes the moment it notices a mover, before anything looks at it."""
    return {
        "stage": "opportunity", "outcome": "discovered",
        "reason": "intraday_move_threshold",
        "move_pct": move_pct, "threshold_pct": 3.0,
    }


#: The six suspended 2026-09-22 intra_check ticks, verbatim in shape: the
#: run's short id, and the movers it discovered before it stopped.
SUSPENDED_20260922 = (
    ("intra_check-d784af8d", 13, [("SNDK", 6.688374200124576),
                                  ("GME", 5.533596837944671)]),
    ("intra_check-cd97d679", 13, [("MU", 3.922339028854545)]),
    ("intra_check-fa514b8a", 14, [("JPM", 3.3014313302283314)]),
    ("intra_check-dc092d5c", 14, [("ONDS", 3.2498307379823994),
                                  ("RKLB", 3.12477654629961),
                                  ("FLNC", 3.0446549391068967)]),
    ("intra_check-3d70b931", 15, [("DRAM", 3.165584415584408),
                                  ("VLO", 3.1553521484871663),
                                  ("WDC", 3.052549369630706)]),
    ("intra_check-d8eb4cf4", 15, [("MRVL", 3.1790447320352913)]),
)
TUE_20260922 = "2026-09-22"


def test_the_2026_09_22_false_alert_is_never_sent_again(db, tmp_path):
    """The exact production evidence, pinned. Six runs, changing movers,
    one identical key — every surface condition the detector fires on — and
    it must stay silent, because not one of those runs ran a gate."""
    con, path = db
    _report_tables(con)
    for run_id, hour, movers in SUSPENDED_20260922:
        for symbol, move_pct in movers:
            _event(con, run_id, TUE_20260922, symbol, _discovery(move_pct), hour)
        _intra_report(con, run_id, TUE_20260922, "paid_analysis_suspended", hour)
    con.commit()

    sessions, err = load_sessions(path)
    assert err is None
    assert sessions == [], (
        "a run whose every candidate is still undecided considered nothing "
        "this check has an opinion about"
    )
    status = check_refusal_signature(
        now=_now_after(TUE_20260922), db_path=path,
        state_path=tmp_path / "state.json",
    )
    assert status.should_alert is False
    assert status.key is None


def test_a_discovery_is_never_a_terminal_outcome(db):
    """Finding a name is a beginning. It cannot be what killed it."""
    con, path = db
    _event(con, "r1", WED, "AAPL", _refusal("AAPL"))
    _event(con, "r1", WED, "SNDK", _discovery(6.69))
    con.commit()
    sessions, _ = load_sessions(path)
    assert sessions[0].candidates == frozenset({"AAPL"})
    assert "SNDK" not in sessions[0].outcomes_by_symbol


def test_a_later_discovery_discards_an_earlier_disposition(db):
    """The evidence stream only moves a candidate forward, so a discovery
    arriving after something else means this run re-opened the name."""
    con, path = db
    _event(con, "r1", WED, "AAPL", _refusal("AAPL"))
    _event(con, "r1", WED, "AAPL", _discovery(4.2))
    con.commit()
    sessions, _ = load_sessions(path)
    assert sessions == []


def test_a_specialist_evaluation_is_a_beginning_too(db):
    """`opportunity` is not the only stage with that character, which is
    why the rule is keyed on the outcome word and not on one stage name."""
    con, path = db
    _event(con, "r1", WED, "AAPL", {
        "stage": "specialist", "outcome": "evaluated",
        "reason": "technical_analysis_validated",
    })
    con.commit()
    assert load_sessions(path)[0] == []


# ---------------------------------------------------------------------------
# a run that never reached a decision is not evidence about the gate
# ---------------------------------------------------------------------------

def _jam_session(con, run_id, day, symbols, hour=10, code="stop_wider_than_instrument_reach"):
    """A run that really did put candidates through the gate and had every
    one of them refused by the same rule."""
    for symbol in symbols:
        _event(con, run_id, day, symbol, _refusal(symbol, code=code), hour)


def test_a_cost_circuit_suspension_is_excluded_by_the_desks_own_record(db, tmp_path):
    """The 15:15 tick of 2026-09-22 is the case the undecided-outcome rule
    does NOT catch: it got as far as running its specialists, one of them
    failed, and that failure reads as a disposition. Only the run's own
    recorded status says it never reached the gate."""
    con, path = db
    _report_tables(con)
    _event(con, "r1", TUE_20260922, "AAPL", {
        "stage": "specialist", "outcome": "failed", "reason": "tech_analyst_error",
    }, 11)
    _intra_report(con, "r1", TUE_20260922, "paid_analysis_suspended", 11)
    con.commit()

    sessions, _ = load_sessions(path)
    assert len(sessions) == 1
    assert sessions[0].disposition == "paid_analysis_suspended"
    assert sessions[0].reached_decision is False

    streak, skipped = streak_and_skipped(sessions)
    assert streak == []
    assert [s.run_id for s in skipped] == ["r1"]
    assert check_refusal_signature(
        now=_now_after(TUE_20260922), db_path=path,
        state_path=tmp_path / "state.json",
    ).should_alert is False


def test_a_suspended_tick_inside_a_real_jam_neither_joins_it_nor_breaks_it(db, tmp_path):
    """Stepped over, not counted. A jam does not clear because one tick was
    switched off, and a switched-off tick is not proof of one either."""
    con, path = db
    _report_tables(con)
    _jam_session(con, "r1", WED, ["AAPL", "MSFT"])
    _intra_report(con, "r1", WED, "intraday_no_trades")
    # The suspended tick sits BETWEEN two jammed sessions.
    _event(con, "r2", THU, "NVDA", {
        "stage": "specialist", "outcome": "failed", "reason": "tech_analyst_error",
    })
    _intra_report(con, "r2", THU, "paid_analysis_suspended")
    _jam_session(con, "r3", FRI, ["TSLA", "AMD", "INTC"])
    _intra_report(con, "r3", FRI, "intraday_no_trades")
    con.commit()

    streak, skipped = streak_and_skipped(load_sessions(path)[0])
    assert [s.run_id for s in streak] == ["r1", "r3"]
    assert [s.run_id for s in skipped] == ["r2"]
    status = check_refusal_signature(
        now=_now_after(FRI), db_path=path, state_path=tmp_path / "state.json",
    )
    assert status.should_alert is True


def test_an_unrecognised_status_still_counts_as_evidence(db):
    """The failure direction is deliberate: an abort word nobody has taught
    this module keeps its run in the streak, where the changing-candidates
    test still has to pass. Silencing on the unknown would let one new
    status word hide a real jam."""
    con, path = db
    _report_tables(con)
    _jam_session(con, "r1", WED, ["AAPL"])
    _intra_report(con, "r1", WED, "some_status_invented_next_year")
    con.commit()
    sessions, _ = load_sessions(path)
    assert sessions[0].reached_decision is True


# ---------------------------------------------------------------------------
# the detector must still do its real job
# ---------------------------------------------------------------------------

def test_a_genuine_jam_still_fires(db, tmp_path):
    """Real candidates, a real gate, one unvarying refusal reason, and
    every run's own record saying it reached a decision."""
    con, path = db
    _report_tables(con)
    for run, day, syms in (("r1", WED, ["AAPL", "MSFT"]),
                           ("r2", THU, ["NVDA"]),
                           ("r3", FRI, ["TSLA", "AMD"])):
        _jam_session(con, run, day, syms)
        _intra_report(con, run, day, "intraday_no_trades")
    con.commit()

    status = check_refusal_signature(
        now=_now_after(FRI), db_path=path, state_path=tmp_path / "state.json",
    )
    assert status.should_alert is True
    assert len(status.streak) == 3
    assert status.inputs_varied is True
    assert "JAMMED" in status_line(status)
    text = alert_text(status)
    assert "shape of a jammed gate" in text
    assert "NOT counted above" not in text


def test_the_alert_says_what_it_did_not_count(db, tmp_path):
    con, path = db
    _report_tables(con)
    _jam_session(con, "r1", WED, ["AAPL", "MSFT"])
    _intra_report(con, "r1", WED, "intraday_no_trades")
    _event(con, "r2", THU, "NVDA", {
        "stage": "specialist", "outcome": "failed", "reason": "tech_analyst_error",
    })
    _intra_report(con, "r2", THU, "paid_analysis_suspended")
    _jam_session(con, "r3", FRI, ["TSLA", "AMD"])
    _intra_report(con, "r3", FRI, "intraday_no_trades")
    con.commit()
    text = alert_text(check_refusal_signature(
        now=_now_after(FRI), db_path=path, state_path=tmp_path / "state.json",
    ))
    assert "1 other run(s) are NOT counted above" in text
    assert "paid_analysis_suspended" in text
    assert "reached a decision" in text


# ---------------------------------------------------------------------------
# the plain wording must use the field that is actually there
# ---------------------------------------------------------------------------

def test_a_reason_in_an_earlier_field_is_read_not_thrown_away():
    """The exact key from the 2026-09-22 message. `_plain_key` read the
    CODE field (empty) and announced it had no wording, while the reason
    text sat two fields to its left."""
    said = _plain_key("opportunity|discovered|intraday_move_threshold||")
    assert "intraday_move_threshold" in said
    assert "has no plain wording for this one" in said


def test_the_detail_field_is_used_when_the_reason_is_empty():
    said = _plain_key("some_gate|blocked|||the book is already full")
    assert "the book is already full" in said


def test_the_admission_appears_only_when_nothing_is_describable():
    assert _plain_key("some_gate|blocked|||") == (
        "the desk recorded a reason it has no plain wording for"
    )


def test_a_named_code_is_still_preferred_over_the_raw_reason():
    said = _plain_key("some_gate|blocked|some_reason|some_code|")
    assert "some_code" in said
    assert "some_reason" not in said
