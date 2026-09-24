#!/usr/bin/env python3
"""Backfill HISTORICAL `margin_interest_daily` rows — owner ask, 2026-09-24.

PR #637 added the cumulative margin-interest panel (this week / current
month / prior months / all-time) but it only counts from the day that PR
started persisting daily rows — no historical daily debit balance was ever
stored, so "all-time" reads as "since the tracker started", not "since the
desk went on margin". The owner wants that gap filled with a BEST-EFFORT
ESTIMATE. His own words: accuracy is not required, it's paper money, get
close and move on.

METHOD (see `src/margin_interest.py`'s "Historical backfill" section for
the full reasoning): Alpaca has no historical cash or positions endpoint —
`portfolio_history` gives only a total-equity time series. What it DOES
keep for the account's whole life is the account-activities ledger (every
deposit, fill, fee, withholding). This script fetches that full ledger
(`AlpacaBroker.get_all_account_activities`), replays it from $0 to
reconstruct an approximate daily cash balance
(`reconstruct_daily_cash_balances`), and treats a negative reconstructed
balance as the historical debit balance — checked against the account's own
live `cash` figure on 2026-09-24: replaying the full ledger reproduces it
to within $0.04 on a ~$6,100 balance.

SAFE TO RE-RUN. Every row this script would write is
`source='estimate_backfill'`; `Database.backfill_margin_interest_daily`
never overwrites a day the LIVE tracker already wrote (`source` `'estimate'`
or `'broker_actual'`), and re-running this script only re-upserts its own
prior backfill rows, never doubles them.

Dry-run by default, same posture as `scripts/backfill_position_ids.py`.
"""

import argparse
import sys
from datetime import timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def _parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=(
            "Backfill margin_interest_daily with a best-effort HISTORICAL "
            "estimate reconstructed from the broker's own activity ledger "
            "(owner ask 2026-09-24). Dry-run by default."
        ),
    )
    p.add_argument(
        "--db-path", type=Path, required=True,
        help="SQLite database to backfill. For review/testing this MUST "
             "be a COPY, never the live production database.",
    )
    p.add_argument(
        "--since", type=str, default=None,
        help="First trading day (YYYY-MM-DD) to backfill. Default: the "
             "earliest date on the account's own activity ledger.",
    )
    p.add_argument(
        "--until", type=str, default=None,
        help="Last trading day (YYYY-MM-DD) to backfill, INCLUSIVE. "
             "Default: yesterday (ET) — today is left for the live "
             "morning tracker to write for itself, not guessed here.",
    )
    p.add_argument(
        "--apply", action="store_true",
        help="Actually write the reconstructed rows. Without this flag "
             "the script only computes and PRINTS what it would write.",
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

    from datetime import date as _date

    from src.api.deps import get_alpaca_credentials, get_alpaca_paper
    from src.config import load_config
    from src.execution.broker import AlpacaBroker
    from src.margin_interest import backfill_daily_estimates
    from src.storage.db import Database
    from src.trading_calendar import et_today

    key, secret = get_alpaca_credentials()
    broker = AlpacaBroker(api_key=key, secret_key=secret, paper=get_alpaca_paper())
    rate_pct = load_config(PROJECT_ROOT / "config/settings.yaml").risk.margin_interest_rate_pct

    print("Fetching the account's full activity ledger from Alpaca...")
    activities = broker.get_all_account_activities()
    print(f"  {len(activities)} activity records fetched")
    if not activities:
        print(
            "ERROR: broker returned no activity records at all — cannot "
            "reconstruct anything. Nothing written.", file=sys.stderr,
        )
        return 3

    from src.margin_interest import activity_effective_date

    dated = sorted(
        d for a in activities if (d := activity_effective_date(a)) is not None
    )
    earliest_activity = dated[0] if dated else None
    since = _date.fromisoformat(args.since) if args.since else earliest_activity
    until = _date.fromisoformat(args.until) if args.until else (et_today() - timedelta(days=1))
    if since is None:
        print("ERROR: could not determine a start date. Nothing written.", file=sys.stderr)
        return 3
    if since > until:
        print(f"ERROR: --since {since} is after --until {until}.", file=sys.stderr)
        return 2

    print(f"Reconstructing trading days from {since} through {until} (inclusive)...")
    trading_days = []
    cursor = since
    while cursor <= until:
        if broker.is_trading_day(cursor):
            trading_days.append(cursor)
        cursor += timedelta(days=1)
    print(f"  {len(trading_days)} trading days")

    rows = backfill_daily_estimates(
        trading_days, activities, rate_pct, broker.is_trading_day,
    )
    nonzero = [r for r in rows if r.period_usd > 0]
    total_period_usd = sum(r.period_usd for r in rows)
    print()
    print(f"Rate used (current config, applied retroactively): {rate_pct:.2f}%")
    print(f"Days with a reconstructed debit balance:            {len(nonzero)} / {len(rows)}")
    print(f"Reconstructed total interest across the range:      ${total_period_usd:,.2f} (est.)")
    print()

    db = Database(str(db_path))
    db.initialize()
    try:
        # Always compute the dry-run counts first — this is also what
        # --apply needs to know BEFORE writing, so the confirmation
        # prompt below can name a real number rather than "some rows".
        preview = db.backfill_margin_interest_daily(rows, dry_run=True)

        print(f"Target database: {db_path}")
        print(f"Mode:            {'APPLY (will write)' if args.apply else 'DRY RUN (no writes)'}")
        label = "Would insert/update" if not args.apply else "Would insert/update"
        print(f"{label}: {preview['inserted']}")
        print(f"Skipped (a live tracker row already exists for that date): {preview['skipped_live_row']}")
        print(f"Total rows considered: {preview['total']}")

        if not args.apply:
            print("\nDry run complete. No changes were made. Re-run with --apply to write.")
            return 0

        if not args.yes and preview["inserted"] > 0:
            resp = input(
                f"About to WRITE {preview['inserted']} backfilled row(s) "
                f"into {db_path}. Type 'yes' to continue: "
            )
            if resp.strip().lower() != "yes":
                print("Aborted — no changes made.")
                return 1

        result = db.backfill_margin_interest_daily(rows, dry_run=False)
        print(f"\nInserted/updated: {result['inserted']}")
        print("Backfill complete.")
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
