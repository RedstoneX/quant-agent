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


def files_on_trunk() -> set[str]:
    """Every tracked .py path on origin/main, or an empty set if it is unknown.

    A branch that predates a new file does not contain that file, so measuring
    this worktree alone makes the file's baseline entry look deletable. It is
    not: the entry belongs to the trunk, and dropping it here silently stops
    tracking that file for everyone once the branch lands. Three changes lost
    a baseline entry this way on 2026-10-02 before this existed.
    """
    out = subprocess.run(
        ["git", "ls-tree", "-r", "--name-only", "origin/main"],
        cwd=ROOT, capture_output=True, text=True,
    )
    if out.returncode != 0:
        return set()
    return {p for p in out.stdout.splitlines() if p.endswith(".py")}


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
    unknown = [a for a in argv if a != "--allow-growth"]
    if unknown:
        # `--help` used to fall through and rewrite the baseline for real.
        print(__doc__)
        print("usage: python -m scripts.regen_file_size_baseline [--allow-growth]")
        print("nothing written.")
        return 2
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
    # An entry whose file is absent HERE but present on the trunk is kept: the
    # branch simply predates the file. Only a file gone from both is dropped.
    on_trunk = files_on_trunk()
    kept_from_trunk = []
    for path, prev in old.items():
        # Present HERE but excluded from `result` means it measured at or
        # under FLOOR, so it genuinely stopped being tracked -- that drop is
        # correct and must not be undone. Only a file this worktree does not
        # have at all is the predates-the-branch case.
        if path not in result and path in on_trunk and not (ROOT / path).exists():
            result[path] = prev
            kept_from_trunk.append(path)
    for path in sorted(kept_from_trunk):
        print(f"KEPT   {path}: {old[path]} (absent from this worktree, present on origin/main)")
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
