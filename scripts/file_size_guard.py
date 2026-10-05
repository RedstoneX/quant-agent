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

* AST statements -- invariant under renaming, line-joining, wrapping and
  re-indenting, so no re-layout and no rename can move it; a file over the
  floor may not gain one against ``origin/main``. This measure REPLACED a
  non-whitespace character count on 2026-10-05: a character count is a proxy
  for complexity, and it refused pure renames. Moving a method off the giant
  pipeline class onto a real collaborator turns every call site from
  ``self.foo(...)`` into ``self.admission.foo(...)`` -- ten more characters,
  zero new behaviour -- so the ratchet reddened the exact refactor it exists
  to encourage, and the only green route was to keep a delegating shim. That
  is the mechanism by which the character rule was wrong, not merely awkward:
  it measured spelling, and a boundary is drawn by renaming.

  RESIDUAL HOLE, named here because true identity is unreachable: a statement
  count errs PERMISSIVE on expression growth. Four statements of ``for``/``if``
  /``append`` collapse into one comprehension (measured 4 -> 1), and chained
  ternaries, ``lambda`` and the walrus launder the same way. Nothing in this
  guard sees that. What binds instead is the width fence below -- a laundered
  expression is long, and a new line past the fence fails -- and the line
  ratchet, which forbids spending the saved statements on new lines. Semicolon
  joining, the route that killed the line-only rule, is NOT a hole here:
  ``a = 1; b = 2`` parses as two statements (measured), and the companion
  statement-cram ratchet refuses the shape outright.
* lines wider than the width fence -- a file may not gain one (by identity:
  path + the line's text) against ``origin/main``, so a line widened past the
  fence fails and passes once it is wrapped. Pre-existing wide lines pass.

NEITHER FENCE IS WRITTEN DOWN. The line ceiling and the width fence were once
two integers measured on one day (2561 and 120) and committed -- stored
bookkeeping, and made-up numbers, the same defect twice: they rot as the tree
moves and an author can edit them to pass. They are now DERIVED at check time
from the population itself, twice: once over the working tree's tracked ``.py``
files and once over ``origin/main``'s, and the TIGHTER of the two is used. That
clamp is what closes the obvious attack on a derived fence -- deleting many
small files raises Q3, and padding lines to just under the fence raises the
width percentile, but neither can move the fence because the trunk's own value
still binds. A branch may only make the fence stricter, never looser.

Only files the branch itself changed are judged (byte identity with the merge
base decides; see ``guard_reference.untouched_paths``): on 2026-10-05 the trunk
was shrinking minute by minute as a split landed, and branches that never opened
the split files were billed for the trunk's older copy they still carried.

Run it directly: ``python -m scripts.file_size_guard``.
"""
from __future__ import annotations

import ast
import math
import sys

from collections import Counter

from scripts.guard_reference import (
    ROOT,
    ReferenceUnavailable,
    TRUNK,
    added_sites,
    trunk_blobs,
    trunk_paths,
    untouched_paths,
    working_paths,
)

# Files at or under this many lines are not individually ratcheted: small files
# may grow until they reach it.
FLOOR = 400
# The line ceiling is the "extreme outlier" fence of the file-size
# distribution, Q3 + 3*IQR; the width fence is the 99.9th percentile of line
# widths. Both are recomputed on every run from both trees -- see the clamp in
# ``fences``. Neither is stored.
CEILING_IQR_MULTIPLE = 3.0
WIDTH_PERCENTILE = 0.999

Wide = tuple[str, str]  # (path, stripped text of a line wider than the fence)


def _count(text: str) -> int:
    return len(text.splitlines())


def _statements(text: str) -> int | None:
    """AST statement nodes, or None when the text does not parse.

    Invariant under renaming, line-joining, wrapping and re-indenting, and
    blind to comments and docstring prose, because none of those are
    behaviour. Genuine new behaviour is new statements.
    """
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return None
    return sum(1 for node in ast.walk(tree) if isinstance(node, ast.stmt))


def _wide(path: str, text: str, width: int) -> Counter:
    return Counter((path, ln.strip()) for ln in text.splitlines() if len(ln) > width)


def _quantile(values: list[int], q: float) -> float:
    """Linear-interpolated quantile of an already-sorted list."""
    pos = q * (len(values) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(values) - 1)
    return values[lo] + (values[hi] - values[lo]) * (pos - lo)


def line_ceiling(texts: list[str]) -> int:
    """Q3 + 3*IQR of one population's file lengths; never below the floor."""
    sizes = sorted(_count(t) for t in texts)
    if not sizes:
        raise ReferenceUnavailable(
            "no tracked .py files to derive the line ceiling from; this guard "
            "derives its fences and REFUSES rather than invent one."
        )
    q1, q3 = _quantile(sizes, 0.25), _quantile(sizes, 0.75)
    return max(FLOOR, int(q3 + CEILING_IQR_MULTIPLE * (q3 - q1)))


def width_fence(texts: list[str]) -> int:
    """The 99.9th percentile line width of one population, by nearest rank."""
    widths = sorted(len(ln) for t in texts for ln in t.splitlines())
    if not widths:
        raise ReferenceUnavailable(
            "no tracked .py lines to derive the width fence from; this guard "
            "derives its fences and REFUSES rather than invent one."
        )
    return widths[math.ceil(WIDTH_PERCENTILE * len(widths)) - 1]


def fences(now: list[str], was: list[str]) -> tuple[int, int]:
    """(line ceiling, width fence): the TIGHTER of the two populations.

    Taking the minimum is the whole defence. A derived fence that read only the
    working tree could be dragged outwards -- delete enough small files and Q3
    rises, pad enough lines to one under the fence and the percentile rises.
    The trunk's own value is computed in the same run and binds, so a branch
    can only tighten.
    """
    return (
        min(line_ceiling(now), line_ceiling(was)),
        min(width_fence(now), width_fence(was)),
    )


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
    all_working = working_texts()  # fence population: the WHOLE tree
    now_text = all_working
    skip = untouched_paths(now_text)
    now = {p: n for p, n in working_sizes().items() if p not in skip}
    now_text = {p: t for p, t in now_text.items() if p not in skip}
    before = trunk_sizes(sorted(now))
    trunk_pop = trunk_paths(".py")
    all_trunk = trunk_blobs(sorted(set(now_text) | set(trunk_pop)))
    ceiling, width = fences(
        list(all_working.values()), [all_trunk[p] for p in trunk_pop if p in all_trunk]
    )
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
        elif size > ceiling >= was:
            bad.append(
                f"{path}: crossed the {ceiling}-line hard ceiling derived from this "
                f"run's two trees, {was} -> {size} "
                f"on {TRUNK}'s reckoning."
            )
    was_text = all_trunk
    wide_now: Counter = Counter()
    wide_was: Counter = Counter()
    for path, text in sorted(now_text.items()):
        wide_now.update(_wide(path, text, width))
        old = was_text.get(path)
        if old is None:
            continue
        wide_was.update(_wide(path, old, width))
        stmts, had = _statements(text), _statements(old)
        if stmts is None:
            bad.append(f"{path}: does not parse, so its statements cannot be counted.")
        elif had is not None and _count(text) > FLOOR and stmts > had:
            bad.append(
                f"{path}: grew from {had} to {stmts} statements "
                f"(+{stmts - had}) against {TRUNK} while holding {_count(text)} lines. "
                f"Renaming and re-layout do not move this count; split it instead."
            )
    for (path, text), n, before_n in added_sites(wide_now, wide_was):
        bad.append(
            f"{path}: new line wider than the derived {width}-character fence "
            f"({n} now, "
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
        "file-size ratchet: no tracked .py file grew in lines, AST "
        f"statements or over-wide lines against {TRUNK}; both fences derived "
        "at check time from both trees, nothing stored."
    )
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())
