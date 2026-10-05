"""File-size ratchet with no stored baseline: measure here, measure trunk, compare.

Two source files once reached 19,100 and 10,200 lines because nothing stopped
them. The old ratchet remembered every file's size in
``tests/file_size_baseline.json``; because leftover headroom was itself a
failure, every change in the tree had to edit that one file, and they jammed
each other all day (measured: 8 of 11 red changes on 2026-10-02).

So this stores nothing. At check time it counts lines in the working tree, counts
them again on ``origin/main``, and reports the DELTA — "this file grew from N to
M". If ``origin/main`` cannot be read it REFUSES; it never passes by default.

Lines are not size. On 2026-10-04 two changes added error logging to dozens of
sites on the money paths, reported their files SHRANK, and between them added
33 lines over 140 characters (measured from the two diffs): the line counter
was satisfied by making lines wider. So the same ratchet is applied to two more
measures of the same files, with the same rule and no stored record:

* non-whitespace characters -- invariant under wrapping, joining and
  re-indenting, so no re-layout can move it; a file over the floor may not
  gain any against ``origin/main``;
* lines wider than ``WIDTH`` -- a file may not gain one (by identity: path +
  the line's text) against ``origin/main``, so a line widened past the limit
  fails and passes once it is wrapped. Pre-existing wide lines pass.

Only files the branch itself changed are judged (byte identity with the merge
base decides; see ``guard_reference.untouched_paths``): on 2026-10-05 the trunk
was shrinking minute by minute as a split landed, and branches that never opened
the split files were billed for the trunk's older copy they still carried.

Run it directly: ``python -m scripts.file_size_guard``.
"""
from __future__ import annotations

import sys

from collections import Counter

from scripts.guard_reference import (
    ROOT,
    ReferenceUnavailable,
    TRUNK,
    added_sites,
    trunk_blobs,
    untouched_paths,
    working_paths,
)

# Files at or under this many lines are not individually ratcheted: small files
# may grow until they reach it.
FLOOR = 400
# Hard ceiling for any file not already above it on the trunk.
# 2561 = Q3 + 3*IQR of all 541 tracked .py files (Q1 197.5, Q3 788.5): the
# statistical "extreme outlier" fence, measured 2026-10-01 on origin/main.
CEILING = 2561
# Widest a line may be before it counts as "wide". 120 is the 99.9th percentile
# of the 410,999 lines in the 1,022 tracked .py files on origin/main (p99 = 90,
# p99.9 = 121; 416 lines exceed it), measured 2026-10-04. It is a RATCHET, not
# a ceiling: the trunk's existing wide lines pass, a new one never does.
WIDTH = 120

Wide = tuple[str, str]  # (path, stripped text of a line wider than WIDTH)


def _count(text: str) -> int:
    return len(text.splitlines())


def _ink(text: str) -> int:
    """Non-whitespace characters: the one measure no re-layout can move."""
    return sum(1 for ch in text if not ch.isspace())


def _wide(path: str, text: str) -> Counter:
    return Counter((path, ln.strip()) for ln in text.splitlines() if len(ln) > WIDTH)


def working_texts() -> dict[str, str]:
    """Every tracked .py file as it stands in the working tree."""
    return {
        p: (ROOT / p).read_text(encoding="utf-8", errors="replace")
        for p in working_paths("*.py")
    }


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
    """Every file this working tree made worse than ``origin/main``, as deltas.

    Only files the branch wrote to are judged: a file byte-identical to the
    merge base is the trunk's own copy, and the trunk shrinking it since is not
    growth here (``guard_reference.untouched_paths`` carries the argument).
    Every file that differs is judged in full against the current trunk.
    """
    now_text = working_texts()
    skip = untouched_paths(now_text)
    now = {p: n for p, n in working_sizes().items() if p not in skip}
    now_text = {p: t for p, t in now_text.items() if p not in skip}
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
    was_text = trunk_blobs(sorted(now_text))
    wide_now: Counter = Counter()
    wide_was: Counter = Counter()
    for path, text in sorted(now_text.items()):
        wide_now.update(_wide(path, text))
        old = was_text.get(path)
        if old is None:
            continue
        wide_was.update(_wide(path, old))
        ink, had = _ink(text), _ink(old)
        if _count(text) > FLOOR and ink > had:
            bad.append(
                f"{path}: grew from {had} to {ink} non-whitespace characters "
                f"(+{ink - had}) against {TRUNK} while holding {_count(text)} lines. "
                f"Wider lines are still growth; split it instead."
            )
    for (path, text), n, before_n in added_sites(wide_now, wide_was):
        bad.append(
            f"{path}: new line wider than {WIDTH} characters ({n} now, "
            f"{before_n} on {TRUNK}): {text[:60]!r}... Wrap it."
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
    print(
        f"file-size ratchet: no tracked .py file grew in lines, non-whitespace "
        f"characters or lines wider than {WIDTH} against {TRUNK}."
    )
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())
