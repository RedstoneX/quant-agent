#!/usr/bin/env python3
"""Show the last recorded outgoing Telegram message of each kind.

Answers one operator question directly, without waiting for a live message
to happen to arrive: "did the last thing the desk tried to tell me look
right?" Every `TelegramNotifier.send()` / `send_document()` call now
records its attempt — sent, failed, or (in rehearsal) suppressed — in the
`notifier_sends` table (`src/storage/db.py`); see `src/notifier.py::
TelegramNotifier._record_send` for what is stored and why this is a table,
not a log line.

Read-only: opens the database with a `file:...?mode=ro` URI, same
convention as `scripts/blocked_proposals_census.py` / `scripts/weekly_review.py`
/ `scripts/desk_reset.py` — never write access, no pipeline imports, no
broker call, safe to run at any time including against the live box.

Usage (from the project root):

    ./scripts/notifier_last_sends.py                 # every kind seen
    ./scripts/notifier_last_sends.py --kind morning   # one kind only
    ./scripts/notifier_last_sends.py --db /path/to/quant_agent.db
    ./scripts/notifier_last_sends.py --full           # don't clip the body

Each row shown is the single most recent `notifier_sends` row for that
kind, regardless of status — a `failed` or `suppressed` row still shows
(labelled), because "the last thing we tried" is the honest answer even
when it didn't arrive. Use `--status sent` to see only sends that actually
went out.
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = PROJECT_ROOT / "data" / "quant_agent.db"

#: Terminal-friendly preview length. Not the stored value — the DB keeps
#: the full body; this only bounds what gets printed. `--full` disables it.
_PREVIEW_CHARS = 600


def _connect(db_path: Path) -> sqlite3.Connection:
    uri = f"file:{db_path}?mode=ro"
    con = sqlite3.connect(uri, uri=True)
    con.row_factory = sqlite3.Row
    return con


def _fetch_latest(
    con: sqlite3.Connection, kind: str | None, status: str | None,
) -> list[sqlite3.Row]:
    where = []
    args: list[str] = []
    if kind:
        where.append("kind = ?")
        args.append(kind)
    if status:
        where.append("status = ?")
        args.append(status)
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    # One row per kind: the highest id for that kind (id, not timestamp —
    # timestamp is second-resolution and two sends in the same second must
    # still resolve to insertion order, same reasoning as
    # Database.get_intra_check_report's ORDER BY timestamp DESC, rowid DESC).
    sql = f"""
        SELECT * FROM notifier_sends
        WHERE id IN (
            SELECT MAX(id) FROM notifier_sends {clause} GROUP BY kind
        )
        ORDER BY kind
    """
    return con.execute(sql, args).fetchall()


def _status_marker(status: str) -> str:
    return {"sent": "OK", "failed": "FAILED", "suppressed": "SUPPRESSED"}.get(status, status.upper())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB, help="Path to quant_agent.db")
    parser.add_argument("--kind", default=None, help="Show only this kind (e.g. morning, owner_alert)")
    parser.add_argument("--status", default=None, choices=["sent", "failed", "suppressed"],
                         help="Show only rows with this status")
    parser.add_argument("--full", action="store_true", help="Print the full message body, not a preview")
    args = parser.parse_args()

    if not args.db.exists():
        print(f"no database at {args.db}", file=sys.stderr)
        return 1

    con = _connect(args.db)
    try:
        try:
            rows = _fetch_latest(con, args.kind, args.status)
        except sqlite3.OperationalError as exc:
            print(f"notifier_sends not readable ({exc}) — has any message been sent since this shipped?",
                  file=sys.stderr)
            return 1
    finally:
        con.close()

    if not rows:
        print("no matching notifier_sends rows")
        return 0

    for row in rows:
        body = row["text"] or ""
        if not args.full and len(body) > _PREVIEW_CHARS:
            body = body[:_PREVIEW_CHARS] + " …[clipped, use --full]"
        print(f"=== {row['kind']} — {_status_marker(row['status'])} — {row['timestamp']} "
              f"(run_id={row['run_id'] or '-'}) ===")
        if row["detail"]:
            print(f"[detail: {row['detail']}]")
        print(body)
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
