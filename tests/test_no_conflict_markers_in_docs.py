"""No merge-conflict marker may reach main.

This exists because one did, on 2026-09-13: resolving two same-day edits to
`docs/INCIDENT_HISTORY.md` by regex missed the diff3 merge-base marker, and
`||||||| <sha>` was committed and merged into the incident record. Nothing
caught it — the docs are prose, so no parser complained, and the board
renders around it.

A marker in a source-of-truth document is worse than untidy: it means two
versions of a fact are sitting side by side with nothing saying which one is
true, in the files the whole desk reads to decide what is real.
"""
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

#: The four documents the desk treats as authoritative, plus the config the
#: engine actually reads. A marker in any of these is a live wrong fact.
GUARDED = (
    "docs/WORK.md",
    "docs/BOARD_NOTES.md",
    "docs/INCIDENT_HISTORY.md",
    "docs/OUTCOME.md",
    "config/settings.yaml",
)

#: Anchored at the start of a line, which is the only place git writes them —
#: so a document that legitimately DISCUSSES conflict markers (this file's own
#: docstring, for instance) does not trip it.
MARKERS = ("<<<<<<< ", "||||||| ", ">>>>>>> ")


@pytest.mark.parametrize("rel", GUARDED)
def test_no_merge_conflict_markers(rel: str):
    path = REPO_ROOT / rel
    if not path.exists():
        pytest.skip(f"{rel} is not present in this checkout")
    offenders = [
        (n, line) for n, line in enumerate(path.read_text().splitlines(), 1)
        if line.startswith(MARKERS) or line.rstrip() == "======="
    ]
    assert not offenders, (
        f"{rel} carries unresolved merge-conflict markers at "
        f"{[n for n, _ in offenders]}. Two versions of a fact are sitting "
        "side by side in a file the desk treats as true. Resolve the merge "
        "properly rather than deleting the markers and hoping."
    )
