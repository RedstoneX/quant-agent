"""Desk-side free-space floor: a trading session that cannot finish does not start.

Separate from scripts/disk_guard.py, which sizes a DEVELOPMENT box (40 worktrees) for CI.
The threat here is another tenant filling the shared disk mid-session, so this reads
real free space at session start and refuses with what it needs and what it found.
"""
from __future__ import annotations

import shutil
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def gib(b):
    return f"{b / 2**30:.1f} GiB"


# --- desk-side floor ------------------------------------------------------
# scripts/disk_guard.py sizes a DEVELOPMENT box (40 worktrees).
# A trading session needs a different quantity, and it is MEASURED, not
# chosen. Measured 2026-10-05, read-only, on the live desk data directory
# (file mtime day in UTC, summing file sizes):
#   * Largest single day of writes under the data directory: 182,631,819 B
#     (2026-08-14, a one-off bulk fetch of 60 earnings filings: genuinely new
#     bytes). The worst day of REWRITE-type writes is 2026-10-01 at 126,384,781
#     B (79,187,968 database + 28,680,785 observations file + the rest).
#     The maximum is used so the floor covers either shape of day.
#   * Why a rewritten file counts at its FULL size, stated honestly: the
#     database is rewritten in place, so its size is not new space on most days.
#     It counts because an in-place rewrite, a journal or a VACUUM can
#     transiently need roughly the file's size again, and the observations
#     file is written temp-then-rename, which holds both copies at once.
#   * EXCLUDED on purpose, do not "fix" back: .git, .venv and the frontend's
#     node_modules (2026-09-10, 227,229,218 B, a one-off package install, not
#     session writes); code, docs and built assets outside data/ (deploys, not
#     sessions: they add 28,977,075 B to 2026-10-01 if included); and the log,
#     which is counted once by the ceiling below, not twice.
#   * The session log cannot run away: main.py installs a RotatingFileHandler
#     with maxBytes=10 MiB and backupCount=5, so the whole log tree is capped
#     at 6 x 10 MiB (observed on the desk: 5 rotations plus the current file).
#   * Bar data adds nothing separate: the desk stores bars in the database and
#     writes no bar cache files.
# FLOOR = 2 x (worst measured day + log ceiling) = 491,092,758 B = 468 MiB.
# The 2 is one further worst-day budget, not a round number: this check runs
# ONCE at session start, and the desk runs unattended on consecutive days with
# nothing reclaiming between them, so a session may not start unless the day
# after it could also run. Recorded in config/number_ledger.yaml.
DESK_WORST_DAY_BYTES = 182_631_819
DESK_LOG_CEILING_BYTES = 6 * 10 * 1024 * 1024
DESK_DAY_BUDGETS_REQUIRED = 2
DESK_FLOOR_BYTES = DESK_DAY_BUDGETS_REQUIRED * (DESK_WORST_DAY_BYTES + DESK_LOG_CEILING_BYTES)


def desk_disk_state(data_dir, floor=DESK_FLOOR_BYTES):
    """Return (ok, detail). Unmeasurable is NOT ok -- this guard fails closed."""
    try:
        u = shutil.disk_usage(data_dir if Path(data_dir).exists() else REPO)
    except Exception as exc:  # noqa: BLE001 - any failure to measure refuses
        return False, f"CANNOT MEASURE free space for {data_dir}: {exc}"
    pct = 100.0 * u.free / u.total if u.total else 0.0
    detail = (f"{gib(u.free)} free = {pct:.1f}% of {gib(u.total)}; "
              f"session floor {gib(floor)}")
    return u.free >= floor and u.total > 0, detail


def require_desk_disk(data_dir, floor=DESK_FLOOR_BYTES, log=None):
    """Refuse the session, loudly, when free space is below the floor or unmeasurable.

    Raises SystemExit naming what the session needs and what was found. It
    deletes nothing: this box is shared, so reclaiming space is the owner's call.
    """
    ok, detail = desk_disk_state(data_dir, floor)
    if ok:
        return 0
    message = (
        f"REFUSING TO START: disk below the session floor. {detail}. "
        f"Needs {floor:,} bytes free = 2 x (worst measured day "
        f"{DESK_WORST_DAY_BYTES:,} + log ceiling {DESK_LOG_CEILING_BYTES:,}). "
        f"A session that cannot write is worse than a session that does not start."
    )
    if log is not None:
        log.critical(message)
    raise SystemExit(message)
