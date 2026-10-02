"""File-size ratchet with no stored baseline: measure here, measure trunk, compare.

Two source files once reached 19,100 and 10,200 lines because nothing stopped
them. The old ratchet remembered every file's size in
``tests/file_size_baseline.json``; because leftover headroom was itself a
failure, every change in the tree had to edit that one file, and they jammed
each other all day (measured: 8 of 11 red changes on 2026-10-02).

So this stores nothing. At check time it counts lines in the working tree, counts
them again on ``origin/main``, and reports the DELTA — "this file grew from N to
M". If ``origin/main`` cannot be read it REFUSES; it never passes by default.

Run it directly: ``python -m scripts.file_size_guard``.
"""
from __future__ import annotations

import sys

from scripts.guard_reference import (
    ROOT,
    ReferenceUnavailable,
    TRUNK,
    trunk_blobs,
    working_paths,
)

# Files at or under this many lines are not individually ratcheted: small files
# may grow until they reach it.
FLOOR = 400
# Hard ceiling for any file not already above it on the trunk.
# 2561 = Q3 + 3*IQR of all 541 tracked .py files (Q1 197.5, Q3 788.5): the
# statistical "extreme outlier" fence, measured 2026-10-01 on origin/main.
CEILING = 2561


def _count(text: str) -> int:
    return len(text.splitlines())


def working_sizes() -> dict[str, int]:
    """Line count of every tracked .py file as it stands in the working tree."""
    return {
        p: _count((ROOT / p).read_text(encoding="utf-8", errors="replace"))
        for p in working_paths("*.py")
    }


def trunk_sizes(paths: list[str]) -> dict[str, int]:
    """Line count of each given path on ``origin/main``; absent paths are omitted."""
    return {p: _count(t) for p, t in trunk_blobs(paths).items()}


def violations() -> list[str]:
    """Every file this working tree made worse than ``origin/main``, as deltas."""
    now = working_sizes()
    before = trunk_sizes(sorted(now))
    bad: list[str] = []
    for path, size in sorted(now.items()):
        was = before.get(path)
        if was is None:
            if size > FLOOR:
                bad.append(
                    f"{path}: new file at {size} lines, over the {FLOOR}-line floor "
                    f"for a file that is not on {TRUNK}. Split it before landing it."
                )
            continue
        if size > max(was, FLOOR):
            bad.append(
                f"{path}: grew from {was} to {size} lines (+{size - was}) "
                f"against {TRUNK}. Split it instead of growing it."
            )
        elif size > CEILING >= was:
            bad.append(
                f"{path}: crossed the {CEILING}-line hard ceiling, {was} -> {size} "
                f"on {TRUNK}'s reckoning."
            )
    return bad


def main(argv: list[str] | None = None) -> int:
    try:
        bad = violations()
    except ReferenceUnavailable as exc:
        print(f"REFUSING: {exc}", file=sys.stderr)
        return 2
    if bad:
        print("Files grew against %s:\n%s" % (TRUNK, "\n".join(bad)), file=sys.stderr)
        return 1
    print(f"file-size ratchet: no tracked .py file grew against {TRUNK}.")
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())
