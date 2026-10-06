"""Broker CONFORMANCE check: the rehearsal stand-in vs the real Alpaca API.

Why this exists: the offline stand-in (`ops/rehearsal/broker.py`) silently
lacked `replace_order_by_id`, the call the desk prefers for moving a protective
stop, so every rehearsal ever run failed to move a stop and reported success.
Only the real API can catch that class of hole. This script exercises every
SDK call the desk makes (`grep self.client. src/execution/`) against BOTH:

  * the disposable SANDBOX paper account, using the same systemd credential-file
    boundary as production and connecting directly to Alpaca — `--live`;
  * the offline stand-in — always.

and prints one row per call: real answer shape vs stand-in shape vs verdict.

Safety (non-negotiable, enforced in code, not in prose):
  * the key and secret must arrive through systemd's
    private credential directory; none is accepted from the environment;
  * the real account is fetched FIRST and the run aborts unless its
    `account_number` and status confirm an active Paper account;
  * proxy and custom-CA environment variables are removed before the Alpaca
    clients are built, so this check cannot silently fall back to OneCLI;
  * every order this script places is cancelled before it exits.

The offline rehearsal stays offline: nothing here is imported by `run.py`.

Usage:
    scripts/run_rehearsal_conformance.sh
    .venv/bin/python -m ops.rehearsal.conformance            # stand-in only
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import traceback
import uuid
from datetime import date, datetime, timedelta, timezone
from enum import Enum
from types import SimpleNamespace
from urllib.parse import urlparse

from ops.rehearsal.conformance_sequence import SYMBOL, _exercise, _fields, _plain, _status_of, run_sequence  # noqa: F401
from ops.rehearsal.direct_credentials import (
    RehearsalCredentialError,
    assert_expected_paper_account,
    bind_systemd_directory_to_rehearsal_identity,
    force_direct_alpaca_transport,
    load_rehearsal_credentials,
)

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

def build_live():
    try:
        bind_systemd_directory_to_rehearsal_identity()
        credentials = load_rehearsal_credentials()
    except RehearsalCredentialError as exc:
        raise SystemExit(f"STOP: {exc}") from exc
    force_direct_alpaca_transport()
    os.environ["QAMC_REHEARSAL"] = "1"
    from alpaca.data.historical.stock import StockHistoricalDataClient
    from alpaca.trading.client import TradingClient
    trading = TradingClient(credentials.api_key, credentials.secret_key, paper=True)
    endpoint = getattr(trading, "_base_url", "")
    resolved = urlparse(str(getattr(endpoint, "value", endpoint)).rstrip("/"))
    if (resolved.scheme != "https" or resolved.netloc != "paper-api.alpaca.markets" or
            resolved.path not in ("", "/") or resolved.query or resolved.fragment):
        raise SystemExit("STOP: resolved trading endpoint is not Alpaca Paper")
    data = StockHistoricalDataClient(credentials.api_key, credentials.secret_key)
    acct = trading.get_account()
    try:
        assert_expected_paper_account(acct)
    except RehearsalCredentialError as exc:
        raise SystemExit(f"STOP: {exc}") from exc
    print(f"account assertion PASSED: account_number == <redacted>, "
          f"status={acct.status}, equity={acct.equity}")
    return trading, data


# ------------------------------------------------------------- the calls

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
