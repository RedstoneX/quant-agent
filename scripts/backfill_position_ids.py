#!/usr/bin/env python3
"""Backfill `trades.position_id` for rows written before that column existed.

Phase 6 (QAMC remediation spec §6.2a): every trade against a symbol now
gets linked into the position it belongs to — a BUY from flat mints a new
`position_id`; SELL/REDUCE/TRAIL_STOP/etc inherit it until the position goes
flat again. New trades get this for free (`Database.insert_trade` /
`insert_stop_out_trade` derive it automatically). This script is the
one-time pass that fills it in for every row that predates the column.

It runs the EXACT SAME FIFO reconstruction (`Database.backfill_position_ids`,
which shares its logic with the live per-insert resolver via
`_assign_position_ids` in src/storage/db.py) — matched by symbol,
oldest-first, a BUY opens/adds, a recognized exit-family action reduces —
so the backfilled chains never disagree with the win-rate / avg-hold-days
numbers `compute_trade_calibration` already trusts.

It never guesses. A row this can't confidently attach to an open chain —
typically the ledger's oldest record for a symbol whose real position
predates this system (a SELL/exit with no prior BUY on record), or a stray
exit recorded after the book had already gone flat — is left NULL and
counted separately from a row that was never eligible in the first place
(HOLD / SWEEP_BUY / SWEEP_SELL).

SAFETY — read before running:
  * Default mode is a DRY RUN: it computes and prints counts, writes
    nothing. Nothing is written unless you pass --apply.
  * Idempotent — a row that already carries a position_id (e.g. from live
    trading that started after this column shipped, before this backfill
    ran against older history) is never reassigned. Running this twice
    against the same database changes nothing the second time.
  * --apply asks for an interactive "yes" confirmation showing the
    resolved --db-path before writing, unless --yes is also passed.
  * This script only opens --db-path with `Database` (read + write to that
    one SQLite file) — it never touches a broker connection.

Usage:
    # Preview against a COPY of the production database — never the live one.
    ./scripts/backfill_position_ids.py --db-path /path/to/copy.db

    # After reviewing the dry-run output above, actually write:
    ./scripts/backfill_position_ids.py --db-path /path/to/copy.db --apply
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def _parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=(
            "Backfill trades.position_id for rows written before that "
            "column existed (Phase 6, spec §6.2a). Dry-run by default."
        ),
    )
    p.add_argument(
        "--db-path", type=Path, required=True,
        help="SQLite database to backfill. For review/testing this MUST "
             "be a COPY, never the live production database.",
    )
    p.add_argument(
        "--apply", action="store_true",
        help="Actually write the resolved position_id values. Without "
             "this flag the script only computes and PRINTS counts.",
    )
    p.add_argument(
        "--yes", action="store_true",
        help="Skip the interactive confirmation prompt when --apply is "
             "set. Only use once you've already reviewed a dry run.",
    )
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = _parse_args(argv)

    db_path = args.db_path.resolve()
    if not db_path.exists():
        print(f"ERROR: --db-path {db_path} does not exist", file=sys.stderr)
        return 2

    print(f"Target database: {db_path}")
    print(f"Mode:            {'APPLY (will write)' if args.apply else 'DRY RUN (no writes)'}")
    print()

    if args.apply and not args.yes:
        resp = input(
            f"About to WRITE position_id values into {db_path}. "
            f"Type 'yes' to continue: "
        )
        if resp.strip().lower() != "yes":
            print("Aborted — no changes made.")
            return 1

    from src.storage.db import Database

    db = Database(str(db_path))
    db.initialize()
    try:
        result = db.backfill_position_ids(dry_run=not args.apply)
    finally:
        db.close()

    label = "Would be assigned" if not args.apply else "Assigned"
    print(f"Total trades rows:                 {result['total']}")
    print(f"Already had position_id:           {result['already_assigned']}")
    print(f"{label}:{' ' * (36 - len(label) - 1)}{result['assigned']}")
    print(f"Left NULL (ambiguous — no open      "
          f"chain to confidently attach): {result['left_null_ambiguous']}")
    print(f"Not applicable (HOLD/SWEEP_*):      {result['not_applicable']}")
    print()
    if not args.apply:
        print("Dry run complete. No changes were made. Re-run with --apply to write.")
    else:
        print("Backfill complete.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
