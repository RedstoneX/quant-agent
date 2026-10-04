"""End-to-end: the morning session's DURABLE record, hermetic.

tests/test_e2e_morning_session.py proves the morning session's SHAPE and
tests/test_e2e_morning_protection.py proves the ORDERS it sends. This file
runs the SAME session once more and proves what it LEAVES BEHIND — the
rows every later surface reads. The desk has shipped recorders that ran
without error and wrote nothing (docs/INCIDENT_HISTORY.md), and the
morning suite's own log shows the live-stop recorder can warn
"no opening row ... was NOT recorded" while the session still reports
`executed`; this is the file that turns that red.

Asserted from the INPUTS and the broker stand-in, never from the desk's
own output compared with itself:

- exactly one `trades` BUY row for the bought name, under THIS run's id,
  as a fill, for the quantity the broker was sent;
- that row's `stop_loss` is the price of the stop actually RESTING at the
  broker — the live-stop recorder found its opening row and wrote;
- the `session_reports` row for ('morning', today) exists, is read back
  through the read-only replay reader, carries the money-visible payload
  the run returned, and renders into the owner's message naming the name;
- every model seat the trace saw answer left an `agent_logs` row under
  this run's id (the spend ledger the owner's cost view reads).

No network, no broker, no live provider, no production database:
`tmp_path/desk.db` is the whole world.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from src.trader_feed.stored import (
    read_stored_session_report, render_stored_session_report,
)
from tests.test_e2e_morning_protection import (
    _assert_decision_and_protection, _seed_company_profile_cache,
)
from tests.test_e2e_morning_session import _assert_full_shape, _run_session


def _ro(tmp_path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{tmp_path / 'desk.db'}?mode=ro", uri=True)


def _opening_rows(tmp_path: Path) -> list[tuple]:
    con = _ro(tmp_path)
    try:
        return con.execute(
            "SELECT symbol, action, qty, run_id, fill_status, stop_loss "
            "FROM trades WHERE action IN ('BUY', 'SHORT') ORDER BY id",
        ).fetchall()
    finally:
        con.close()


def _seat_rows(tmp_path: Path, run_id: str) -> list[str]:
    con = _ro(tmp_path)
    try:
        return [r[0] for r in con.execute(
            "SELECT agent_name FROM agent_logs WHERE run_id=? ORDER BY id",
            (run_id,),
        ).fetchall()]
    finally:
        con.close()


def _stored_morning_row(tmp_path: Path) -> dict:
    record = read_stored_session_report("morning", db_path=tmp_path / "desk.db")
    assert record is not None, "run_morning left no session_reports row behind"
    payload = record.get("payload")
    if isinstance(payload, str):
        payload = json.loads(payload)
    assert isinstance(payload, dict), record
    record["payload"] = payload
    return record


def _money_visible(payload: dict) -> dict:
    return {k: payload.get(k) for k in (
        "status", "orders", "run_id", "stop_coverage_gaps",
    )}


def _run(tmp_path, monkeypatch):
    from ops.rehearsal.network_wall import no_network

    _seed_company_profile_cache(tmp_path)
    attempts: list[str] = []
    with no_network(attempts):
        result, trace, trading = _run_session(tmp_path, monkeypatch)
    assert attempts == [], f"the session tried to leave the box: {attempts}"
    _assert_full_shape(result, trace, trading)
    _assert_decision_and_protection(result, trading)
    return result, trace, trading


def _the_buy_and_its_stop(trading):
    buys = [o for o in trading.submitted if str(o.side).lower().endswith("buy")]
    stops = [o for o in trading.submitted if str(o.order_type).lower() == "stop"]
    assert len(buys) == 1 and len(stops) == 1, [o.as_plain() for o in trading.submitted]
    return buys[0], stops[0]


def test_the_buy_is_in_the_ledger_with_the_stop_that_rests_at_the_broker(
        tmp_path, monkeypatch):
    result, _trace, trading = _run(tmp_path, monkeypatch)
    buy, stop = _the_buy_and_its_stop(trading)

    rows = _opening_rows(tmp_path)
    assert len(rows) == 1, f"one buy, one opening row: {rows}"
    symbol, action, qty, run_id, fill_status, stop_loss = rows[0]
    assert (symbol, action) == (buy.symbol, "BUY"), rows[0]
    assert float(qty) == float(buy.qty), rows[0]
    assert run_id == result["run_id"] and run_id, rows[0]
    assert fill_status == "filled", rows[0]
    assert stop_loss is not None and float(stop_loss) == float(stop.stop_price), (
        f"ledger carries stop_loss={stop_loss} but the stop resting at the "
        f"broker is {stop.stop_price}: {rows[0]}"
    )


def test_the_morning_report_is_stored_rereadable_and_renders_the_name(
        tmp_path, monkeypatch):
    result, _trace, trading = _run(tmp_path, monkeypatch)
    buy, _stop = _the_buy_and_its_stop(trading)

    record = _stored_morning_row(tmp_path)
    payload = record["payload"]
    assert record.get("mode", "morning") == "morning", record
    assert _money_visible(payload) == _money_visible(
        json.loads(json.dumps(result, default=str))), (
        _money_visible(payload), _money_visible(result),
    )
    assert payload["status"] == "executed", payload["status"]
    assert payload["run_id"] == result["run_id"] and payload["run_id"]
    orders = payload["orders"]
    assert len(orders) == 1 and orders[0]["status"] == "filled", orders
    assert orders[0]["symbol"] == buy.symbol, orders
    assert float(orders[0]["qty"]) == float(buy.qty), orders

    # The renderer reads the run's evidence rows from the desk's configured
    # database file; point that one edge at the file this session wrote.
    monkeypatch.setattr("src.trader_feed.common._DB_PATH", str(tmp_path / "desk.db"))
    text = render_stored_session_report("morning", record)
    assert buy.symbol in text and "BOUGHT" in text, text


def test_every_seat_that_answered_left_a_spend_row_under_this_run(
        tmp_path, monkeypatch):
    result, trace, _trading = _run(tmp_path, monkeypatch)
    asked = [seat for kind, seat in trace if kind == "llm"]
    assert asked, trace

    # The script keys its seats by short name ('macro', 'tech', ...); the
    # ledger stores the agent's full name ('macro_analyst', ...).
    logged = _seat_rows(tmp_path, result["run_id"])
    missing = sorted(seat for seat in set(asked)
                     if not any(seat in name for name in logged))
    assert missing == [], (
        f"seats answered but left no agent_logs row under {result['run_id']}: "
        f"{missing}; logged={logged}"
    )
    assert len(logged) >= len(asked), (asked, logged)
