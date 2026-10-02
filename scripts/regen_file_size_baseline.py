"""Regenerate tests/file_size_baseline.json, the file-size ratchet's record.

Run deliberately (``python -m scripts.regen_file_size_baseline``) after a file
has shrunk, to re-tighten its baseline. It prints every change. It REFUSES to
raise a baseline or add an entry above the floor unless ``--allow-growth`` is
given, so loosening the ratchet is always a visible, reviewable act.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BASELINE_PATH = ROOT / "tests" / "file_size_baseline.json"

# Files at or under this many lines are not individually tracked.
FLOOR = 400
# Hard ceiling: no file NOT already in the baseline above it may cross it.
# 2561 = Q3 + 3*IQR of all 541 tracked .py files (Q1 197.5, Q3 788.5): the
# statistical "extreme outlier" fence, measured 2026-10-01 on origin/main.
CEILING = 2561
# A file must be re-tightened once it is this many lines under its baseline.
SHRINK_SLACK = 25


def tracked_py_files() -> list[str]:
    out = subprocess.run(
        ["git", "ls-files", "*.py"], cwd=ROOT, check=True, capture_output=True, text=True
    ).stdout
    return sorted(p for p in out.splitlines() if (ROOT / p).is_file())


def line_count(rel: str) -> int:
    return len((ROOT / rel).read_text(encoding="utf-8", errors="replace").splitlines())


def measure() -> dict[str, int]:
    sizes = {p: line_count(p) for p in tracked_py_files()}
    return {p: n for p, n in sizes.items() if n > FLOOR}


def load_baseline() -> dict[str, int]:
    return json.loads(BASELINE_PATH.read_text()) if BASELINE_PATH.exists() else {}


def main(argv: list[str]) -> int:
    allow_growth = "--allow-growth" in argv
    old, new = load_baseline(), measure()
    result: dict[str, int] = {}
    refused = []
    for path, n in new.items():
        prev = old.get(path)
        if prev is not None and n > prev and not allow_growth:
            refused.append(f"GREW   {path}: {prev} -> {n} (kept {prev})")
            result[path] = prev
        elif prev is None and old and not allow_growth:
            refused.append(
                f"NEW    {path}: {n} lines (not recorded). This is NOT a size "
                f"limit -- FLOOR={FLOOR} only decides which files are tracked "
                f"at all, and a new file may be any size up to the {CEILING}"
                f"-line ceiling. Re-run with --allow-growth to record it."
            )
        else:
            result[path] = n
    for path in sorted(set(old) | set(result)):
        a, b = old.get(path), result.get(path)
        if a != b:
            print(f"{'ADDED ' if a is None else 'REMOVED' if b is None else 'TIGHTENED' if b < a else 'LOOSENED'} {path}: {a} -> {b}")
    for line in refused:
        print("REFUSED " + line)
    BASELINE_PATH.write_text(json.dumps(dict(sorted(result.items())), indent=1) + "\n")
    print(f"{len(result)} files in baseline; ceiling {CEILING}.")
    return 1 if refused else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
