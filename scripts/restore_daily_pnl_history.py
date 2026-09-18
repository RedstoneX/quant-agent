#!/usr/bin/env python3
"""Restore `daily_pnl` rows a reset deleted, so the ladder sees the real high.

THE DEFECT THIS CLOSES. The gross-exposure de-levering ladder
(`src/risk/rules.py::resolve_gross_ceiling`) resolves its ceiling from
`peak_to_trough_pct` over `Database.get_daily_pnl(limit=252)`. That function
takes `max()` over the stored history plus today's equity, so **a missing
row can only ever LOWER the peak.** The error is therefore one-directional:
a hole in `daily_pnl` always makes the desk read a SHALLOWER drawdown than
the account really has, and hold a LOOSER exposure ceiling than the ratified
ladder intends. It is a safety error, not noise.

WHAT WAS MISSING (all figures read from the files, 2026-09-18):
  * The live `daily_pnl` table's earliest row is 2026-09-02 (9862.74). Its
    peak across the four stored rows is therefore 9862.74.
  * The desk reset of 2026-09-02 (`data/resets/20260902T181859Z/`) deleted
    13 `daily_pnl` rows by design — its own `reset_manifest.json` records
    `{"table": "daily_pnl", "rows": 13, "deleting": 13}` — and took a full
    database snapshot beside the manifest first. Those 13 rows run
    2026-08-14 .. 2026-09-01 and peak at **10005.68 on 2026-08-20**.
  * The account was NOT restarted by that reset. The reset flattened
    positions to cash on the same paper account (`PA3DFXH9FF5V` in both
    `book_before.json` and `book_after.json`); equity ran 9870.37 (08-27
    close) -> 9865.27 (pre-flatten) -> 9864.04 (post-flatten) -> 9862.74
    (09-02 close) with no capital added or removed. A high-water mark is a
    property of the account's capital, not of the strategy record the reset
    discarded, so 10005.68 is this account's real high.

WHY A SCRIPT AND NOT AN `INSERT`. So the restore is reviewable, repeatable
and reversible: it is a dry run by default, it copies the target database
before writing, it NEVER writes a row it cannot read from the source
snapshot, and it is idempotent (`INSERT OR IGNORE`, `date` is the primary
key) so a second run changes nothing.

NOTHING IS INVENTED. Only rows physically present in the source database
are copied. Dates absent from BOTH databases (2026-09-03, and the
2026-09-04 .. 2026-09-14 desk pause) are left absent — `daily_pnl` is
written only by an evening run, and no row exists for a day the desk did
not run. Interpolating one would fabricate an equity reading.

USAGE (dry run first, always):

    scripts/restore_daily_pnl_history.py \
        --source data/resets/20260902T181859Z/quant_agent.db \
        --target data/quant_agent.db
    scripts/restore_daily_pnl_history.py --source ... --target ... --apply

TO REVERSE: the `--apply` run prints the path of the pre-write copy it
took. Restore that file, or delete exactly the dates this script reports as
inserted (they are the only rows it touches).

Exit codes: 0 ran, 2 bad input / refused, 3 the restore could not verify.
"""
from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
import time
from pathlib import Path

#: Columns copied verbatim. `timestamp` is carried across on purpose — the
#: restored row keeps the moment the evening run actually wrote it, rather
#: than claiming to have been written today.
COLUMNS = (
    "date", "total_value", "daily_pnl", "daily_return_pct",
    "equity_close", "timestamp",
)


def _read_rows(db_path: Path) -> list[dict]:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        conn.row_factory = sqlite3.Row
        cols = ", ".join(COLUMNS)
        return [dict(r) for r in conn.execute(
            f"SELECT {cols} FROM daily_pnl ORDER BY date"
        )]
    finally:
        conn.close()


def _peak(rows) -> tuple[str, float] | None:
    usable = [r for r in rows if isinstance(r["total_value"], (int, float))]
    if not usable:
        return None
    best = max(usable, key=lambda r: r["total_value"])
    return best["date"], float(best["total_value"])


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True,
                        help="database to copy missing daily_pnl rows FROM "
                             "(a reset snapshot or backup; never written to)")
    parser.add_argument("--target", required=True,
                        help="database to restore INTO")
    parser.add_argument("--apply", action="store_true",
                        help="actually write (default is a dry run)")
    args = parser.parse_args(argv)

    source, target = Path(args.source), Path(args.target)
    for label, path in (("source", source), ("target", target)):
        if not path.is_file():
            print(f"ERROR: --{label} does not exist: {path}", file=sys.stderr)
            return 2
    if source.resolve() == target.resolve():
        print("ERROR: --source and --target are the same file", file=sys.stderr)
        return 2

    source_rows = _read_rows(source)
    target_rows = _read_rows(target)
    have = {r["date"] for r in target_rows}
    missing = [r for r in source_rows if r["date"] not in have]

    before = _peak(target_rows)
    after = _peak(target_rows + missing)
    print(f"source : {source}  ({len(source_rows)} rows)")
    print(f"target : {target}  ({len(target_rows)} rows)")
    print(f"peak before : {before}")
    print(f"peak after  : {after}")
    if not missing:
        print("Nothing to restore — every source row is already present.")
        return 0
    print(f"\n{len(missing)} row(s) would be inserted:")
    for r in missing:
        print(f"  {r['date']}  total_value={r['total_value']}")

    if not args.apply:
        print("\nDRY RUN — nothing written. Re-run with --apply to write.")
        return 0

    backup = target.with_name(
        f"{target.name}.pre-daily-pnl-restore-{time.strftime('%Y%m%dT%H%M%S')}"
    )
    shutil.copy2(target, backup)
    print(f"\nPre-write copy taken: {backup}")

    conn = sqlite3.connect(str(target))
    try:
        placeholders = ", ".join("?" for _ in COLUMNS)
        cols = ", ".join(COLUMNS)
        with conn:
            conn.executemany(
                f"INSERT OR IGNORE INTO daily_pnl ({cols}) VALUES ({placeholders})",
                [tuple(r[c] for c in COLUMNS) for r in missing],
            )
    finally:
        conn.close()

    verify = _read_rows(target)
    still_missing = [r["date"] for r in missing
                     if r["date"] not in {v["date"] for v in verify}]
    if still_missing:
        print(f"ERROR: rows did not land: {still_missing}", file=sys.stderr)
        return 3
    print(f"Restored {len(missing)} row(s). Peak is now {_peak(verify)}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
