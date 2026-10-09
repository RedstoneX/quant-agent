"""A refusal must leave conflict regions a human and a parser can both read.

Two defects lived in `build_refusal_artefact`, both reproduced on 2026-10-04
against a real stale-branch forward merge of `docs/WORK.md`:

  1. `_lines_lost` had no merge base, so it counted every line the TRUNK had
     legitimately superseded as lost content. A branch merging a fast-moving
     trunk forward tripped it as a matter of course.
  2. The block it then appended opened with one `<<<<<<<` and closed with TWO
     `>>>>>>>` lines. That is not conflict syntax. Anything that scans for
     opener/terminator pairs ends the region at the first terminator, which
     silently discards the whole second copy.

Together they turned an ordinary merge — one git resolves into a single
conflict region — into a tripled document with an unparseable region in it,
on the three files that are this project's source of truth.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import resolve_doc_conflict as rdc  # noqa: E402


# A plain forward merge: the trunk ("ours") revised one line since the base;
# the stale branch ("theirs") touched a different one. Nothing is lost here.
# The two edits are far apart so git merges them cleanly rather than folding
# them into one conflict region that happens to carry both originals.
_PAD = "".join(f"filler line {i}\n" for i in range(12))
BASE = "alpha\nbeta\n" + _PAD + "gamma\n"
OURS = "alpha\nbeta revised on the trunk\n" + _PAD + "gamma\n"
THEIRS = "alpha\nbeta\n" + _PAD + "gamma extended on the branch\n"


def _markers(text: str) -> list[str]:
    out = []
    for line in text.splitlines():
        if line.startswith(("<<<<<<< ", "||||||| ", ">>>>>>> ")):
            out.append(line.split(" ", 1)[0])
        elif line.rstrip() == "=======":
            out.append("=======")
    return out


def test_superseded_trunk_line_is_not_reported_as_lost():
    """Defect 1. `beta` is gone from the merge because the trunk replaced it."""
    merged, _clean = rdc._git_merge_file(BASE, OURS, THEIRS)
    assert "beta revised on the trunk" in merged
    assert rdc._lines_lost(merged, OURS, THEIRS, BASE) == []


def test_refusal_artefact_does_not_triple_an_ordinary_merge():
    """Defect 1, as the human sees it: no whole extra copies appended."""
    text = rdc.build_refusal_artefact(BASE, OURS, THEIRS, "x.merge-refusal")
    assert "CONTENT NOT ACCOUNTED FOR" not in text
    assert text.count("gamma extended on the branch") == 1


def test_every_conflict_region_has_exactly_one_terminator(monkeypatch):
    """Defect 2. Force the not-accounted-for block and parse its markers.

    The block is reached by handing `build_refusal_artefact` a merge result
    that really has dropped a line, which is what the block exists for.
    """
    # git's own three-way merge does not drop lines, so the only honest way
    # to exercise the block is to assert that it has lost something. That is
    # exactly the posture the block is written for: a belt-and-braces check
    # against a merge that misbehaved.
    monkeypatch.setattr(rdc, "_lines_lost", lambda *a, **k: ["kept-by-ours"])
    text = rdc.build_refusal_artefact(BASE, "kept-by-ours\n", "kept-by-theirs\n", "x.merge-refusal")
    assert "CONTENT NOT ACCOUNTED FOR" in text, "the block under test did not fire"
    # Both sides must still be present in full.
    assert "kept-by-ours" in text and "kept-by-theirs" in text

    depth = 0
    for m in _markers(text):
        if m == "<<<<<<<":
            assert depth == 0, "a conflict region opened inside another one"
            depth = 1
        else:
            assert depth == 1, f"stray {m!r} outside any conflict region"
            if m == ">>>>>>>":
                depth = 0
    assert depth == 0, "a conflict region was never terminated"
