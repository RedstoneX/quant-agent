#!/usr/bin/env python3
"""Backfill the conviction-ledger columns on `trades` (spec §7.2) for rows
written before they existed.

Phase 7.2 (QAMC remediation spec): "Log each trade's allocated risk against
its realized outcome. If the desk's conviction predicts results,
conviction-weighted sizing amplifies the edge. If it does not, flat sizing
is superior — and that must be discovered from data, not assumed." That
requires `conviction`, `requested_risk_pct`, `allocated_risk_pct`,
`decision_model`, and `decision_id_status` to exist on `trades`. New trades
get them for free (`Database.insert_trade` / `insert_stop_out_trade` derive
`decision_id_status` automatically; `ExecutionStage` pins the other four at
entry). This script is the one-time pass for every row that predates them.

It runs `Database.backfill_conviction_ledger`, which does TWO independent
repairs in one pass — mirrors `scripts/backfill_position_ids.py`'s shape
and safety posture:

  1. `decision_id_status` on every exit-family row — NEVER ambiguous
     (every insert_trade/insert_stop_out_trade call site in this codebase
     is enumerated, so a NULL decision_id on an exit row is a known fact,
     not a gap to guess at): every eligible row resolves to EITHER
     'linked' or 'no_originating_decision'.

  2. `conviction` / `requested_risk_pct` / `decision_model` on BUY/SHORT
     rows that already carry a real decision_id — recovered by joining
     `agent_logs` and reading the matching `targets` entry out of
     `full_response`.

     `allocated_risk_pct` (the POST-clamp figure the constructor's budget
     actually granted) is NEVER backfilled by this script — see
     `Database.backfill_conviction_ledger`'s docstring for why: it was
     never persisted anywhere retroactively readable, and approximating it
     would mean guessing at point-in-time book state this database does
     not preserve. The report below states this plainly rather than
     hiding it — measured against real production data 2026-08-30, this
     figure is 0% recoverable for every historical entry, because no
     completed round-trip yet used risk-based sizing at all.

SAFETY — read before running:
  * Default mode is a DRY RUN: it computes and prints counts, writes
    nothing. Nothing is written unless you pass --apply.
  * Idempotent — an exit row already carrying decision_id_status, or an
    entry row already carrying BOTH conviction and decision_model, is
    never reprocessed. Running this twice against the same database
    changes nothing the second time.
  * --apply asks for an interactive "yes" confirmation showing the
    resolved --db-path before writing, unless --yes is also passed.
  * This script only opens --db-path with `Database` (read + write to that
    one SQLite file) — it never touches a broker connection.

Usage:
    # Preview against a COPY of the production database — never the live one.
    ./scripts/backfill_conviction_ledger.py --db-path /path/to/copy.db

    # After reviewing the dry-run output above, actually write:
    ./scripts/backfill_conviction_ledger.py --db-path /path/to/copy.db --apply
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
            "Backfill the conviction-ledger columns on trades for rows "
            "written before they existed (spec §7.2). Dry-run by default."
        ),
    )
    p.add_argument(
        "--db-path", type=Path, required=True,
        help="SQLite database to backfill. For review/testing this MUST "
             "be a COPY, never the live production database.",
    )
    p.add_argument(
        "--apply", action="store_true",
        help="Actually write the resolved values. Without this flag the "
             "script only computes and PRINTS counts.",
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
            f"About to WRITE conviction-ledger values into {db_path}. "
            f"Type 'yes' to continue: "
        )
        if resp.strip().lower() != "yes":
            print("Aborted — no changes made.")
            return 1

    from src.storage.db import Database

    db = Database(str(db_path))
    db.initialize()
    try:
        result = db.backfill_conviction_ledger(dry_run=not args.apply)
    finally:
        db.close()

    label = "Would be set" if not args.apply else "Set"
    print("--- Exit rows: decision_id_status (fully recoverable) ---")
    print(f"Exit-family rows considered:        {result['exit_rows_considered']}")
    print(f"{label} 'linked':{' ' * max(1, 22 - len(label))}{result['exit_linked']}")
    print(f"{label} 'no_originating_decision':  {result['exit_no_originating_decision']}")
    print(f"Not applicable (BUY/SHORT/HOLD/SWEEP_*): {result['exit_not_applicable']}")
    print()
    print("--- Entry rows: conviction / requested_risk_pct / decision_model ---")
    print(f"Entry rows considered (BUY/SHORT with decision_id): {result['entry_rows_considered']}")
    print(f"{label}: {result['entry_recovered']}")
    print(f"Unrecoverable — no matching agent_logs row:  {result['entry_unrecoverable_no_agent_log']}")
    print(f"Unrecoverable — no matching target in PM response: "
          f"{result['entry_unrecoverable_no_matching_target']}")
    print()
    print(
        "allocated_risk_pct recoverable by this backfill: "
        f"{result['allocated_risk_pct_recoverable']} (ALWAYS 0 — the post-clamp "
        "granted figure was never persisted anywhere retroactively readable; "
        "see the module docstring)."
    )
    print()
    if not args.apply:
        print("Dry run complete. No changes were made. Re-run with --apply to write.")
    else:
        print("Backfill complete.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
