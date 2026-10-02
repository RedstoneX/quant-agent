"""Broker CONFORMANCE check: the rehearsal stand-in vs the real Alpaca API.

Why this exists: the offline stand-in (`ops/rehearsal/broker.py`) silently
lacked `replace_order_by_id`, the call the desk prefers for moving a protective
stop, so every rehearsal ever run failed to move a stop and reported success.
Only the real API can catch that class of hole. This script exercises every
SDK call the desk makes (`grep self.client. src/execution/`) against BOTH:

  * the disposable SANDBOX paper account, reached ONLY through the OneCLI
    `rehearsal` agent grant (never a raw key) — `--live`;
  * the offline stand-in — always.

and prints one row per call: real answer shape vs stand-in shape vs verdict.

Safety (non-negotiable, enforced in code, not in prose):
  * the sandbox account number is read from `QAMC_SANDBOX_ACCOUNT_NUMBER`;
    it is never written into this repository;
  * the real account is fetched FIRST and the run aborts unless its
    `account_number` equals that value — a mismatch means an account that
    matters, and nothing else runs;
  * the OneCLI agent token is read from the local dashboard API at run time
    and never printed;
  * every order this script places is cancelled before it exits.

The offline rehearsal stays offline: nothing here is imported by `run.py`.

Usage:
    QAMC_SANDBOX_ACCOUNT_NUMBER=<id> .venv/bin/python -m ops.rehearsal.conformance --live
    .venv/bin/python -m ops.rehearsal.conformance            # stand-in only
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import traceback
import urllib.request
import uuid
from datetime import date, datetime, timedelta, timezone
from enum import Enum
from types import SimpleNamespace

ONECLI_DASHBOARD = "http://127.0.0.1:10254"
ONECLI_GATEWAY = "127.0.0.1:10255"
SYMBOL = "SPY"

# Fields the desk actually reads off each answer (grep getattr/attr access in
# src/execution/). A stand-in that lacks one of these lies to the desk.
DESK_READS = {
    "get_account": ["cash", "portfolio_value", "last_equity",
                    "non_marginable_buying_power", "account_number"],
    "get_all_positions": ["symbol", "qty", "avg_entry_price", "current_price",
                          "market_value", "unrealized_pl"],
    "get_portfolio_history": ["timestamp", "equity"],
    "get_calendar": ["date", "open", "close"],
    "get_asset": ["tradable", "fractionable", "status", "symbol"],
    "submit_order": ["id", "status", "symbol", "qty", "filled_qty",
                     "filled_avg_price"],
    "get_order_by_id": ["id", "status", "side", "symbol", "qty", "filled_qty",
                        "filled_avg_price", "order_type", "stop_price",
                        "limit_price"],
    "get_orders": ["id", "status", "side", "symbol", "qty", "order_type",
                   "stop_price", "limit_price", "legs", "filled_qty"],
    "replace_order_by_id": ["id", "status"],
    "cancel_order_by_id": [],
    "cancel_orders": [],
    "close_position": ["id", "status"],
    "get(/assets/{sym})": ["tradable", "fractionable", "status"],
    "get(/account/activities)": [],
    "get(/account/activities/INT)": [],
    "get_stock_latest_trade": ["price", "timestamp"],
    "get_stock_latest_quote": ["bid_price", "ask_price", "timestamp"],
    "get_stock_snapshot": ["latest_trade", "daily_bar", "previous_daily_bar",
                           "minute_bar"],
    "get_stock_bars": ["data"],
}


# ------------------------------------------------------------- shapes

def _plain(value):
    """A JSON-able description of an SDK/stand-in answer."""
    if isinstance(value, Enum):
        return f"enum:{value.value}"
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (datetime, date)):
        return f"datetime:{value.isoformat()}"
    if isinstance(value, uuid.UUID):
        return str(value)
    if hasattr(value, "model_dump"):
        return {k: _plain(v) for k, v in value.model_dump().items()}
    if isinstance(value, SimpleNamespace):
        return {k: _plain(v) for k, v in vars(value).items()}
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    return f"<{type(value).__name__}>"


def _fields(plain) -> set[str]:
    if isinstance(plain, dict) and isinstance(plain.get(SYMBOL), dict):
        return set(plain[SYMBOL])  # symbol-keyed data answers
    if isinstance(plain, dict):
        return set(plain)
    if isinstance(plain, list) and plain and isinstance(plain[0], dict):
        return set(plain[0])
    return set()


def _status_of(plain) -> str | None:
    if isinstance(plain, dict):
        s = plain.get("status")
        return s.replace("enum:", "") if isinstance(s, str) else s
    return None


def _exercise(client, name: str, fn) -> dict:
    try:
        raw = fn(client)
    except Exception as exc:  # noqa: BLE001 — the error SHAPE is the finding
        return {"call": name, "ok": False, "error_type": type(exc).__name__,
                "error": str(exc)[:200],
                "status_code": getattr(exc, "status_code", None), "raw": None}
    plain = _plain(raw)
    return {"call": name, "ok": True, "raw": raw, "plain": plain,
            "fields": sorted(_fields(plain)), "status": _status_of(plain),
            "kind": type(raw).__name__}


# ------------------------------------------------------------- stand-in

def build_stand_in(now: datetime, with_position: bool):
    from ops.rehearsal.broker import (
        BrokerSnapshot, RehearsalDataClient, RehearsalTradingClient,
    )
    positions = []
    if with_position:
        positions = [{"symbol": SYMBOL, "qty": 1, "avg_entry": 500.0,
                      "current_price": 500.0, "market_value": 500.0}]
    snap = BrokerSnapshot(as_of=now.date(), cash=10_000.0,
                          portfolio_value=10_000.0, last_equity=10_000.0,
                          positions=positions, prices={SYMBOL: 500.0},
                          equity_curve=[(now.date().isoformat(), 10_000.0)])
    return RehearsalTradingClient(snap, now=now), RehearsalDataClient(snap)


# ------------------------------------------------------------- live

def _onecli_rehearsal_env() -> dict:
    """Proxy + CA env for the `rehearsal` grant. The token is never printed."""
    with urllib.request.urlopen(f"{ONECLI_DASHBOARD}/api/agents", timeout=5) as r:
        agents = json.load(r)
    grant = next((a for a in agents if a.get("identifier") == "rehearsal"), None)
    if grant is None:
        raise SystemExit("STOP: OneCLI has no agent with identifier=rehearsal")
    with urllib.request.urlopen(f"{ONECLI_DASHBOARD}/api/container-config",
                                timeout=5) as r:
        ca = json.load(r)["caCertificate"]
    ca_path = os.path.join(tempfile.mkdtemp(prefix="qamc-conf-"), "ca.pem")
    with open(ca_path, "w") as fh:
        fh.write(ca)
    proxy = f"http://x:{grant['accessToken']}@{ONECLI_GATEWAY}"
    return {"HTTPS_PROXY": proxy, "https_proxy": proxy,
            "SSL_CERT_FILE": ca_path, "REQUESTS_CA_BUNDLE": ca_path}


def build_live():
    expected = os.environ.get("QAMC_SANDBOX_ACCOUNT_NUMBER", "").strip()
    if not expected:
        raise SystemExit("STOP: QAMC_SANDBOX_ACCOUNT_NUMBER is not set; refusing "
                         "to touch any broker account without the expected id")
    os.environ.update(_onecli_rehearsal_env())
    os.environ["QAMC_REHEARSAL"] = "1"
    from alpaca.data.historical.stock import StockHistoricalDataClient
    from alpaca.trading.client import TradingClient
    trading = TradingClient("placeholder", "placeholder", paper=True)
    data = StockHistoricalDataClient("placeholder", "placeholder")
    acct = trading.get_account()
    got = str(acct.account_number)
    if got != expected:
        raise SystemExit(
            f"STOP: account mismatch — expected <redacted>, got a different id; "
            f"NOTHING was placed")
    print(f"account assertion PASSED: account_number == <redacted>, "
          f"status={acct.status}, equity={acct.equity}")
    return trading, data


# ------------------------------------------------------------- the calls

def run_sequence(trading, data, *, live: bool) -> list[dict]:
    from alpaca.data.requests import (
        StockBarsRequest, StockLatestQuoteRequest, StockLatestTradeRequest,
        StockSnapshotRequest,
    )
    from alpaca.data.timeframe import TimeFrame
    from alpaca.trading.enums import OrderSide, QueryOrderStatus, TimeInForce
    from alpaca.trading.requests import (
        GetCalendarRequest, GetOrdersRequest, LimitOrderRequest,
        ReplaceOrderRequest, StopOrderRequest,
    )

    rows: list[dict] = []
    today = date.today()

    def rec(name, fn):
        row = _exercise(trading, name, fn)
        rows.append(row)
        return row

    rec("get_account", lambda c: c.get_account())
    pos = rec("get_all_positions", lambda c: c.get_all_positions())
    held = [p for p in (pos.get("plain") or []) if isinstance(p, dict)]
    held_symbol = held[0]["symbol"] if held else None
    held_qty = int(float(held[0]["qty"])) if held else 0
    rec("get_portfolio_history", lambda c: c.get_portfolio_history())
    rec("get_calendar", lambda c: c.get_calendar(
        GetCalendarRequest(start=today, end=today + timedelta(days=7))))
    rec("get_asset", lambda c: c.get_asset(SYMBOL))
    rec("get(/assets/{sym})", lambda c: c.get(f"/assets/{SYMBOL}"))
    rec("get(/account/activities)", lambda c: c.get(
        "/account/activities", {"page_size": 5}))
    rec("get(/account/activities/INT)", lambda c: c.get(
        "/account/activities/INT", {"page_size": 5}))

    # A resting entry: a limit far below the market so it can never fill.
    entry = rec("submit_order", lambda c: c.submit_order(LimitOrderRequest(
        symbol=SYMBOL, qty=1, side=OrderSide.BUY, time_in_force=TimeInForce.DAY,
        limit_price=1.00)))
    entry_id = (entry.get("plain") or {}).get("id") if entry["ok"] else None

    rec("get_order_by_id", lambda c: c.get_order_by_id(entry_id))
    rec("get_orders", lambda c: c.get_orders(GetOrdersRequest(
        status=QueryOrderStatus.OPEN, symbols=[SYMBOL])))
    rep = rec("replace_order_by_id", lambda c: c.replace_order_by_id(
        entry_id, ReplaceOrderRequest(limit_price=1.50)))
    rep_id = (rep.get("plain") or {}).get("id") if rep["ok"] else None
    rec("get_order_by_id (after replace)", lambda c: c.get_order_by_id(entry_id))
    rec("cancel_order_by_id", lambda c: c.cancel_order_by_id(rep_id or entry_id))
    rec("get_order_by_id (after cancel)",
        lambda c: c.get_order_by_id(rep_id or entry_id))
    rec("replace_order_by_id (on cancelled)", lambda c: c.replace_order_by_id(
        rep_id or entry_id, ReplaceOrderRequest(limit_price=1.75)))
    rec("cancel_order_by_id (already cancelled)",
        lambda c: c.cancel_order_by_id(rep_id or entry_id))
    rec("get_order_by_id (unknown id)",
        lambda c: c.get_order_by_id(str(uuid.uuid4())))

    # The protective-stop path needs a held position.
    if held_symbol and held_qty > 0:
        stop = rec("submit_order (protective stop)",
                   lambda c: c.submit_order(StopOrderRequest(
                       symbol=held_symbol, qty=held_qty, side=OrderSide.SELL,
                       time_in_force=TimeInForce.GTC, stop_price=0.50)))
        stop_id = (stop.get("plain") or {}).get("id") if stop["ok"] else None
        srep = rec("replace_order_by_id (stop_price)",
                   lambda c: c.replace_order_by_id(
                       stop_id, ReplaceOrderRequest(stop_price=0.75)))
        srep_id = (srep.get("plain") or {}).get("id") if srep["ok"] else None
        rec("get_orders (stops after amend)", lambda c: c.get_orders(
            GetOrdersRequest(status=QueryOrderStatus.OPEN,
                             symbols=[held_symbol], side=OrderSide.SELL)))
        rec("cancel_order_by_id (stop)",
            lambda c: c.cancel_order_by_id(srep_id or stop_id))
    else:
        # No position (and after hours nothing can fill), so exercise the SAME
        # amend call on a buy-stop far above the market: identical request
        # type, identical answer shape, and it needs no shares.
        stop = rec("submit_order (protective stop)",
                   lambda c: c.submit_order(StopOrderRequest(
                       symbol=SYMBOL, qty=1, side=OrderSide.BUY,
                       time_in_force=TimeInForce.GTC, stop_price=9000.0)))
        stop_id = (stop.get("plain") or {}).get("id") if stop["ok"] else None
        srep = rec("replace_order_by_id (stop_price)",
                   lambda c: c.replace_order_by_id(
                       stop_id, ReplaceOrderRequest(stop_price=9100.0)))
        srep_id = (srep.get("plain") or {}).get("id") if srep["ok"] else None
        rec("get_orders (stops after amend)", lambda c: c.get_orders(
            GetOrdersRequest(status=QueryOrderStatus.OPEN, symbols=[SYMBOL])))
        rec("get_order_by_id (old stop id after amend)",
            lambda c: c.get_order_by_id(stop_id))
        rec("cancel_order_by_id (stop)",
            lambda c: c.cancel_order_by_id(srep_id or stop_id))
    if held_symbol:
        rec("close_position", lambda c: c.close_position(held_symbol))
    else:
        rows.append({"call": "close_position", "ok": False,
                     "error_type": "UNTESTED", "error": "no held position"})
    rec("cancel_orders", lambda c: c.cancel_orders())
    rec("get_orders (after cancel_orders)", lambda c: c.get_orders(
        GetOrdersRequest(status=QueryOrderStatus.OPEN)))

    def drec(name, fn):
        rows.append(_exercise(data, name, fn))

    drec("get_stock_latest_trade", lambda d: d.get_stock_latest_trade(
        StockLatestTradeRequest(symbol_or_symbols=[SYMBOL])))
    drec("get_stock_latest_quote", lambda d: d.get_stock_latest_quote(
        StockLatestQuoteRequest(symbol_or_symbols=[SYMBOL])))
    drec("get_stock_snapshot", lambda d: d.get_stock_snapshot(
        StockSnapshotRequest(symbol_or_symbols=[SYMBOL])))
    drec("get_stock_bars", lambda d: d.get_stock_bars(StockBarsRequest(
        symbol_or_symbols=[SYMBOL], timeframe=TimeFrame.Day,
        start=datetime.now(timezone.utc) - timedelta(days=10))))
    return rows


# ------------------------------------------------------------- compare

def _answer(row: dict) -> str:
    if row["ok"]:
        s = f"{row['kind']}"
        if row.get("status") is not None:
            s += f" status={row['status']}"
        s += f" fields={len(row['fields'])}"
        return s
    code = f" http={row['status_code']}" if row.get("status_code") else ""
    return f"RAISES {row['error_type']}{code}: {row['error'][:70]}"


def compare(real: list[dict], stub: list[dict]) -> list[dict]:
    by_name = {r["call"]: r for r in stub}
    out = []
    for r in real:
        s = by_name.get(r["call"])
        base = r["call"].split(" (")[0]
        reads = DESK_READS.get(base, [])
        verdict, detail = "MATCH", ""
        if s is None:
            verdict, detail = "MISSING", "stand-in never produced this row"
        elif r["ok"] and not s["ok"]:
            verdict = "HOLE" if "no attribute" in s["error"] else "DIVERGES"
            detail = f"real answers, stand-in raises {s['error_type']}"
        elif not r["ok"] and s["ok"]:
            verdict, detail = "DIVERGES", "real raises, stand-in answers"
        elif not r["ok"] and not s["ok"]:
            if r["error_type"] != s["error_type"]:
                verdict = "ERROR-SHAPE"
                detail = f"real {r['error_type']} vs stand-in {s['error_type']}"
        else:
            missing = [f for f in reads if f in r["fields"] and f not in s["fields"]]
            if missing:
                verdict, detail = "FIELDS", f"stand-in lacks {missing}"
            if ("order" in base or "position" in base) and \
                    r.get("status") != s.get("status"):
                verdict = "STATUS" if verdict == "MATCH" else verdict + "+STATUS"
                detail += f" status real={r.get('status')} stub={s.get('status')}"
            if isinstance(r.get("plain"), list) and isinstance(s.get("plain"), list) \
                    and bool(r["plain"]) != bool(s["plain"]):
                verdict = "FILTER"
                detail = (f"real returns {len(r['plain'])} row(s), stand-in "
                          f"{len(s['plain'])} — the stand-in ignores the status filter")
            elif isinstance(r.get("plain"), list) != isinstance(s.get("plain"), list):
                verdict = "TYPE"
                detail = f"real {r['kind']} vs stand-in {s['kind']}"
        out.append({"call": r["call"], "real": _answer(r),
                    "stand_in": _answer(s) if s else "-", "verdict": verdict,
                    "detail": detail.strip()})
    return out


def print_table(rows: list[dict]) -> None:
    print("\n| call | real (sandbox) | stand-in | verdict | detail |")
    print("|---|---|---|---|---|")
    for r in rows:
        print(f"| {r['call']} | {r['real']} | {r['stand_in']} | "
              f"**{r['verdict']}** | {r['detail']} |")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true",
                    help="also run against the SANDBOX account via OneCLI")
    ap.add_argument("--json", help="write raw rows here")
    args = ap.parse_args(argv)

    now = datetime.now(timezone.utc)
    live_rows = None
    if args.live:
        trading, data = build_live()
        try:
            live_rows = run_sequence(trading, data, live=True)
        finally:
            # Leave nothing resting, whatever happened above.
            try:
                trading.cancel_orders()
            except Exception:  # noqa: BLE001
                traceback.print_exc()
            left = trading.get_orders()
            print(f"\naccount left with {len(left)} open order(s) and "
                  f"{len(trading.get_all_positions())} position(s)")
    held = bool(live_rows and any(
        r["call"] == "get_all_positions" and r["ok"] and r["plain"] for r in live_rows))
    stub_t, stub_d = build_stand_in(now, with_position=held)
    stub_rows = run_sequence(stub_t, stub_d, live=False)

    if live_rows is None:
        print_table([{"call": r["call"], "real": "(not run)",
                      "stand_in": _answer(r), "verdict": "-", "detail": ""}
                     for r in stub_rows])
        return 0
    table = compare(live_rows, stub_rows)
    print_table(table)
    if args.json:
        with open(args.json, "w") as fh:
            json.dump({"real": [{k: v for k, v in r.items() if k != "raw"}
                                for r in live_rows],
                       "stand_in": [{k: v for k, v in r.items() if k != "raw"}
                                    for r in stub_rows]}, fh, indent=1, default=str)
    bad = [t for t in table if t["verdict"] != "MATCH"]
    print(f"\n{len(bad)} disagreement(s) of {len(table)} calls")
    return 0


if __name__ == "__main__":
    sys.exit(main())
