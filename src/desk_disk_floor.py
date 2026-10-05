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
# chosen. Measured 2026-10-05, read-only, against the live desk checkout:
#   * Largest single day of writes under the desk data directory:
#     126,384,781 B (2026-10-01), summing every file whose mtime falls on that
#     day at its FULL size. Full size is the right quantity: the desk rewrites
#     whole files (its 79 MB SQLite database, a 28 MB smart-money observations
#     file) via write-temp-then-rename, so the bytes must fit on disk whole.
#     24 further active days were measured and all are smaller.
#   * The session log cannot run away: main.py installs a RotatingFileHandler
#     with maxBytes=10 MiB and backupCount=5, so the whole log tree is capped
#     at 6 x 10 MiB (observed on the desk: 5 rotations plus the current file).
#   * Bar data adds nothing separate: the desk stores bars in the database and
#     writes no bar cache files.
# FLOOR = 2 x (worst measured day + log ceiling) = 378,598,682 B = 361 MiB.
# The 2 is one further worst-day budget, not a round number: this check runs
# ONCE at session start, and the desk runs unattended on consecutive days with
# nothing reclaiming between them, so a session may not start unless the day
# after it could also run. Recorded in config/number_ledger.yaml.
DESK_WORST_DAY_BYTES = 126_384_781
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
