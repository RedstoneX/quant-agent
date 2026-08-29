#!/usr/bin/env python3
"""Backfill broker-initiated stop-out fills the `trades` ledger never saw.

2026-08-28 incident: ONDS (17 sh @ 8.53, bought 2026-08-27) and CCJ (2 sh
@ 107.465, bought 2026-08-27) were both closed by their broker-resident
protective stop — a GTC stop-limit order `AlpacaBroker.place_entry_
protection` places on every filled entry but never writes into `trades`.
No SELL/exit row was ever recorded for either symbol; both BUY rows sat
forever at `realized_pnl IS NULL`; the `positions` table (synced straight
from broker truth every session) correctly went to zero while `trades`
kept telling a stale story. Verified against the live paper account:

  ONDS: stop-limit order 865a3187-af9d-4752-be45-f121dcb9a390
        filled 17 @ 7.93 on 2026-08-28 16:16:07 UTC -> realized -$10.20
  CCJ:  stop-limit order c785ae7e-359d-49fc-9853-0930e879eae5
        filled 2 @ 102.955 on 2026-08-28 14:05:17 UTC -> realized -$9.02

This script is NOT a one-off patch for those two symbols. It runs the
EXACT SAME broker-truth reconciliation the live system now runs every
session (`TradingPipeline._reconcile_stop_out_fills`, src/pipeline.py),
just once, standalone, against whatever `--db-path` you point it at. It
is idempotent — keyed on broker_order_id (`Database.insert_stop_out_
trade`) — so running it twice against the same database, or running it
after the live reconciler has already caught some of the gap, records
nothing twice.

SAFETY — read before running:
  * Default mode is a DRY RUN: it prints exactly what it WOULD record and
    writes nothing. Nothing is written unless you pass --apply.
  * This script does not special-case or block any path — that is
    deliberate: its whole purpose is to be run against the real ledger
    once its output has been reviewed. The operator is responsible for
    pointing --db-path at the intended file. NEVER point --apply at a
    live database you have not first dry-run and reviewed. When
    developing/verifying this script, point --db-path at a COPY only.
  * --apply asks for an interactive "yes" confirmation showing the
    resolved --db-path before writing, unless --yes is also passed
    (for non-interactive/scripted use once you've already reviewed the
    dry run).
  * The broker connection is ALWAYS paper (paper=True is hardcoded below,
    mirroring config/settings.yaml::AlpacaConfig's own enforced
    "live trading is not authorized" invariant) — this script cannot
    place, cancel, or modify any order regardless; it only reads order
    history and writes rows into the SQLite ledger at --db-path.

Usage:
    # Preview against a COPY of the production database — never the live one.
    ./scripts/backfill_stop_out_fills.py --db-path /path/to/copy.db

    # After reviewing the dry-run output above, actually write:
    ./scripts/backfill_stop_out_fills.py --db-path /path/to/copy.db --apply

Credentials come from .env (ALPACA_API_KEY / ALPACA_SECRET_KEY), same as
the trading pipeline.
"""

from __future__ import annotations

import argparse
import os
import sys
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def _load_env_file() -> None:
    """Best-effort .env loader (same line-based approach as
    scripts/export_alpaca_trades.py) so the script works standalone
    without `set -a; source .env`. Existing env wins."""
    env_path = PROJECT_ROOT / ".env"
    if not env_path.exists():
        return
    try:
        for raw in env_path.read_text().splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            if line.startswith("export "):
                line = line[len("export "):].lstrip()
            k, v = line.split("=", 1)
            k = k.strip()
            v = v.strip().strip('"').strip("'")
            if k and k not in os.environ:
                os.environ[k] = v
    except Exception:
        pass  # best-effort; caller fails at the credential check below


def _parse_args(argv=None) -> argparse.Namespace:
    from src.config import ReconciliationConfig

    p = argparse.ArgumentParser(
        description=(
            "Backfill broker-initiated stop-out fills the trades ledger "
            "never recorded (2026-08-28 ONDS/CCJ). Dry-run by default."
        ),
    )
    p.add_argument(
        "--db-path", type=Path, required=True,
        help="SQLite database to reconcile. For review/testing this MUST "
             "be a COPY, never the live production database.",
    )
    p.add_argument(
        "--lookback-days", type=int,
        default=ReconciliationConfig.model_fields["stop_out_lookback_days"].default,
        help="How far back to ask the broker for filled SELL orders the "
             "ledger doesn't already know about (default: same as "
             "ReconciliationConfig.stop_out_lookback_days).",
    )
    p.add_argument(
        "--apply", action="store_true",
        help="Actually write the recovered fills. Without this flag the "
             "script only PRINTS what it would do.",
    )
    p.add_argument(
        "--yes", action="store_true",
        help="Skip the interactive confirmation prompt when --apply is "
             "set. Only use once you've already reviewed a dry run.",
    )
    return p.parse_args(argv)


def _dry_run(db, broker, lookback_days: int) -> int:
    """Preview the SAME gap-detection + broker-fill lookup the real
    reconciler uses, without writing anything. Composed from the exact
    building blocks `TradingPipeline._reconcile_stop_out_fills` calls
    (`Database.get_symbols_with_open_ledger_qty`,
    `AlpacaBroker.list_filled_sell_orders`,
    `Database.get_known_broker_order_ids`) so the preview can never drift
    from what --apply would actually do."""
    try:
        ledger_qty = db.get_symbols_with_open_ledger_qty()
    except Exception as exc:
        print(f"ERROR: ledger qty lookup failed: {exc}", file=sys.stderr)
        return 3
    try:
        broker_positions = broker.get_positions()
    except Exception as exc:
        print(f"ERROR: broker.get_positions() failed: {exc}", file=sys.stderr)
        return 3
    broker_qty = {p.symbol: float(p.qty) for p in (broker_positions or [])}

    after = datetime.now(timezone.utc) - timedelta(days=lookback_days)
    found_gap = False
    for symbol, ledger_open in sorted(ledger_qty.items()):
        held = broker_qty.get(symbol, 0.0)
        gap = ledger_open - held
        if gap <= 1e-6:
            continue
        found_gap = True
        print(f"GAP: {symbol} — ledger believes {ledger_open:.4f} sh open, "
              f"broker shows {held:.4f}")
        known = db.get_known_broker_order_ids(symbol)
        fills = broker.list_filled_sell_orders(symbol, after=after)
        if fills is None:
            print("  broker order query FAILED — cannot preview this symbol")
            continue
        new_fills = [f for f in fills if f.get("id") and f["id"] not in known]
        if not new_fills:
            print(f"  no untracked filled SELL found in the last "
                  f"{lookback_days} day(s) — would be FLAGGED for manual "
                  f"review, nothing recorded")
            continue
        for f in new_fills:
            print(f"  WOULD RECORD: order {f['id']} — {f['qty']} sh @ "
                  f"${f['price']:.4f} filled {f.get('filled_at') or '(unknown time)'}")
    if not found_gap:
        print("No ledger/broker mismatch found — nothing to backfill.")
    print()
    print("Dry run complete. No changes were made. Re-run with --apply to write.")
    return 0


def _apply(pipeline, db) -> int:
    results = pipeline._reconcile_stop_out_fills(run_id="backfill-script")
    if not results:
        print("No ledger/broker mismatch found — nothing to backfill.")
        return 0

    exit_code = 0
    for r in results:
        symbol = r["symbol"]
        if not r["matched"]:
            exit_code = 1
            print(
                f"UNRESOLVED: {symbol} — ledger {r['ledger_qty']:.4f} sh, "
                f"broker {r['broker_qty']:.4f} sh, no matching broker fill "
                f"found. Flagged in specialist_evidence for manual review; "
                f"nothing recorded."
            )
            continue
        rows = [
            row for row in db.get_trades(symbol=symbol, executed_only=True)
            if row["action"] == "STOP_OUT"
        ]
        for row in rows:
            pnl = row.get("realized_pnl")
            pnl_str = "UNMATCHED (flagged, not guessed)" if pnl is None else f"${pnl:.2f}"
            print(
                f"RECORDED: {symbol} {row['fill_qty']} sh @ "
                f"${row['fill_price']:.4f} (order {row['broker_order_id']}, "
                f"filled {row['timestamp']}) realized_pnl={pnl_str}"
            )
            if pnl is None:
                exit_code = 1
    return exit_code


def main(argv=None) -> int:
    args = _parse_args(argv)
    _load_env_file()

    api_key = os.environ.get("ALPACA_API_KEY")
    api_secret = os.environ.get("ALPACA_SECRET_KEY")
    if not api_key or not api_secret:
        print(
            "ERROR: ALPACA_API_KEY / ALPACA_SECRET_KEY not found in env or .env",
            file=sys.stderr,
        )
        return 2

    db_path = args.db_path.resolve()
    if not db_path.exists():
        print(f"ERROR: --db-path {db_path} does not exist", file=sys.stderr)
        return 2

    print(f"Target database: {db_path}")
    print("Broker:          Alpaca PAPER (paper=True is hardcoded — this "
          "script cannot touch a live account)")
    print(f"Lookback window: {args.lookback_days} day(s)")
    print(f"Mode:            {'APPLY (will write)' if args.apply else 'DRY RUN (no writes)'}")
    print()

    if args.apply and not args.yes:
        resp = input(
            f"About to WRITE stop-out fills into {db_path}. "
            f"Type 'yes' to continue: "
        )
        if resp.strip().lower() != "yes":
            print("Aborted — no changes made.")
            return 1

    from src.storage.db import Database
    from src.execution.broker import AlpacaBroker

    db = Database(str(db_path))
    db.initialize()
    broker = AlpacaBroker(api_key=api_key, secret_key=api_secret, paper=True)

    if not args.apply:
        return _dry_run(db, broker, args.lookback_days)

    # A minimal pipeline shim — _reconcile_stop_out_fills only touches
    # .db, .broker, and .config, exactly like the unit tests in
    # tests/test_stop_out_reconciliation.py construct it. Building the
    # full TradingPipeline would require every agent's API key.
    from src.pipeline import TradingPipeline

    pipeline = TradingPipeline.__new__(TradingPipeline)
    pipeline.db = db
    pipeline.broker = broker
    pipeline.config = types.SimpleNamespace(
        reconciliation=types.SimpleNamespace(
            stop_out_lookback_days=args.lookback_days,
        ),
    )
    return _apply(pipeline, db)


if __name__ == "__main__":
    sys.exit(main())
