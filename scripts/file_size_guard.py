"""File-size ratchet with no stored baseline: measure here, measure trunk, compare.

Two source files once reached 19,100 and 10,200 lines because nothing stopped
them. The old ratchet remembered every file's size in
``tests/file_size_baseline.json``; because leftover headroom was itself a
failure, every change in the tree had to edit that one file, and they jammed
each other all day (measured: 8 of 11 red changes on 2026-10-02).

So this stores nothing. At check time it counts lines in the working tree, counts
them again on ``origin/main``, and reports the DELTA — "this file grew from N to
M". If ``origin/main`` cannot be read it REFUSES; it never passes by default.

THE RULE: split, don't grow — but consolidation is not growth.
A file that is larger here than on ``origin/main`` is refused, UNLESS both hold:
  (a) the change as a whole removed at least as many lines as it added, summed
      over every tracked .py file it touched (added, modified or deleted,
      judged against ``origin/main``), and
  (b) the grown file is at or under the hard ``CEILING``.
Rewiring seven mixin files into one explicit holder deletes far more than it
adds yet makes one file bigger; the first version of this guard refused exactly
that, which is the rebuild the owner ordered (2026-10-04). A change that only
adds lines has a positive net and is refused exactly as before.

Run it directly: ``python -m scripts.file_size_guard``.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, field

from scripts.guard_reference import (
    ROOT,
    ReferenceUnavailable,
    TRUNK,
    trunk_blobs,
    trunk_paths,
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


@dataclass
class Change:
    """Line counts on both sides plus which paths this change actually touched.

    ``now`` holds every tracked .py file in the working tree; ``before`` every
    .py file on ``origin/main``. A path in only one of them was added or
    deleted; a path in both is ``touched`` only when its content differs.
    """

    now: dict[str, int]
    before: dict[str, int]
    touched: set[str] = field(default_factory=set)

    def net(self) -> int:
        """Lines added minus lines removed over the touched files (signed)."""
        return sum(self.now.get(p, 0) - self.before.get(p, 0) for p in self.touched)


def working_sizes() -> dict[str, int]:
    """Line count of every tracked .py file as it stands in the working tree."""
    return {p: _count(t) for p, t in _working_texts().items()}


def _working_texts() -> dict[str, str]:
    return {
        p: (ROOT / p).read_text(encoding="utf-8", errors="replace")
        for p in working_paths("*.py")
    }


def trunk_sizes(paths: list[str]) -> dict[str, int]:
    """Line count of each given path on ``origin/main``; absent paths are omitted."""
    return {p: _count(t) for p, t in trunk_blobs(paths).items()}


def measure() -> Change:
    """Read both sides once and work out which paths the change touched."""
    here = _working_texts()
    on_trunk = trunk_blobs(trunk_paths(".py"))
    touched = {
        p for p in set(here) | set(on_trunk)
        if here.get(p) != on_trunk.get(p)
    }
    return Change(
        now={p: _count(t) for p, t in here.items()},
        before={p: _count(t) for p, t in on_trunk.items()},
        touched=touched,
    )


def assess(change: Change | None = None) -> tuple[list[str], list[str]]:
    """``(violations, waivers)``: files made worse than ``origin/main`` as deltas,
    and files that grew but were allowed because the change shrank the tree."""
    change = measure() if change is None else change
    net = change.net()
    signed = f"{net:+d}"
    scope = f"{len(change.touched)} touched file{'s' if len(change.touched) != 1 else ''}"
    bad: list[str] = []
    waived: list[str] = []
    for path, size in sorted(change.now.items()):
        was = change.before.get(path)
        if was is None:
            if size <= FLOOR:
                continue
            if net <= 0 and size <= CEILING:
                waived.append(
                    f"{path}: new file at {size} lines, over the {FLOOR}-line floor, "
                    f"allowed because the change nets {signed} lines over {scope}."
                )
            else:
                bad.append(
                    f"{path}: new file at {size} lines, over the {FLOOR}-line floor "
                    f"for a file that is not on {TRUNK}; the change nets {signed} "
                    f"lines over {scope}. Split it before landing it."
                )
            continue
        if size > max(was, FLOOR):
            grew = f"{path}: grew from {was} to {size} lines (+{size - was}) against {TRUNK}"
            if net > 0:
                bad.append(
                    f"{grew}; the change nets {signed} lines over {scope}. "
                    "Split it instead of growing it."
                )
            elif size > CEILING:
                bad.append(
                    f"{grew}; the change nets {signed} lines, but {size} is over the "
                    f"{CEILING}-line hard ceiling. Split it instead of growing it."
                )
            else:
                waived.append(
                    f"{grew}, allowed because the change nets {signed} lines over {scope}."
                )
        elif size > CEILING >= was:
            bad.append(
                f"{path}: crossed the {CEILING}-line hard ceiling, {was} -> {size} "
                f"on {TRUNK}'s reckoning."
            )
    return bad, waived


def violations(change: Change | None = None) -> list[str]:
    """Every file this working tree made worse than ``origin/main``, as deltas."""
    return assess(change)[0]


def main(argv: list[str] | None = None) -> int:
    try:
        bad, waived = assess()
    except ReferenceUnavailable as exc:
        print(f"REFUSING: {exc}", file=sys.stderr)
        return 2
    if bad:
        print("Files grew against %s:\n%s" % (TRUNK, "\n".join(bad)), file=sys.stderr)
        return 1
    if waived:
        print("file-size ratchet: growth absorbed by a net-reducing change:\n" + "\n".join(waived))
    else:
        print(f"file-size ratchet: no tracked .py file grew against {TRUNK}.")
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())
