"""Refuse (exit non-zero) when the repo or scratch filesystem is low on space.

Usage: disk_guard.py [--scratch DIR]   (default DIR: $DISK_GUARD_SCRATCH or /tmp/claude-1000)
Exit 0 = enough space on both; 1 = tight; 2 = could not measure (refuses, never passes blind).
"""

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
# Measured 2026-10-02: a checkout of this repo is 52 MB; 40 of them (one per live scratch
# worktree plus test temp files and pyc/pytest caches) is the floor. Governs no money decision.
CHECKOUTS_REQUIRED = 40
# Measured 2026-10-02: git-tracked tree = 52 MB (du -sm, excluding .git); used if sizing fails.
FALLBACK_CHECKOUT_BYTES = 52 * 1024 * 1024


def checkout_bytes(repo=REPO):
    try:
        out = subprocess.run(
            ["du", "-sb", "--exclude=.git", "--exclude=.venv", str(repo)],
            capture_output=True,
            text=True,
            timeout=60,
            check=True,
        ).stdout
        return int(out.split()[0])
    except Exception:
        return FALLBACK_CHECKOUT_BYTES


def biggest_dirs(scratch, n=5):
    """Largest immediate children of the scratch path: the cheap reclaimable hint."""
    try:
        kids = [p for p in Path(scratch).iterdir() if p.is_dir() and not p.is_symlink()]
        sized = []
        for p in kids:
            r = subprocess.run(["du", "-sb", str(p)], capture_output=True, text=True, timeout=60)
            if r.stdout.strip():
                sized.append((int(r.stdout.split()[0]), str(p)))
        return sorted(sized, reverse=True)[:n]
    except Exception:
        return []


def gib(b):
    return f"{b / 2**30:.1f} GiB"


def check(paths, need):
    """Return (problems, ok_lines). A path that cannot be measured is a problem."""
    problems, seen = [], set()
    for label, path in paths:
        try:
            st = os.stat(path)
            u = shutil.disk_usage(path)
        except Exception as e:
            problems.append(f"CANNOT MEASURE {label} ({path}): {e}; refusing")
            continue
        if st.st_dev in seen:
            continue
        seen.add(st.st_dev)
        pct = 100.0 * u.free / u.total if u.total else 0.0
        if u.total <= 0 or u.free < need:
            problems.append(
                f"LOW DISK on filesystem holding {label} ({path}): "
                f"{gib(u.free)} free = {pct:.1f}% of {gib(u.total)}; need {gib(need)}"
            )
    return problems


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--scratch", default=os.environ.get("DISK_GUARD_SCRATCH", "/tmp/claude-1000"))
    a = ap.parse_args(argv)
    need = CHECKOUTS_REQUIRED * checkout_bytes()
    problems = check([("repo", str(REPO)), ("scratch", a.scratch)], need)
    if not problems:
        print("disk_guard: ok")
        return 0
    for p in problems:
        print(p, file=sys.stderr)
    big = biggest_dirs(a.scratch)
    if big:
        print("Largest reclaimable under scratch:", file=sys.stderr)
        for size, p in big:
            print(f"  {gib(size)}  {p}", file=sys.stderr)
    return 2 if any(p.startswith("CANNOT") for p in problems) else 1


if __name__ == "__main__":
    sys.exit(main())
