"""`scripts/git_merge_driver_docs.sh` and its `.gitattributes` wiring.

`scripts/resolve_doc_conflict.py` only protects a merge if something actually
calls it. This is the front end git calls automatically, once a clone has
registered it with `git config merge.docsmerge.driver ...` (see README.md
"### Install"). These tests exercise the front end directly, the same way
git itself invokes it — four positional temp-file-shaped arguments — without
needing a real git merge in flight.

The one property that matters most: the resolver's exit code 2 ("refusing to
write, needs a human") must come back out of this wrapper as a NONZERO exit.
A refusal that this wrapper turned into exit 0 would be worse than having no
automatic resolver at all — see docs/WORK.md item 68's own history of a
resolver that wrote a plausible file instead of stopping.

CORRECTED 2026-09-23. This module also used to assert that %A must be left
ALONE on a refusal, on the stated belief that doing so "is what makes git's
own conflict machinery take over". That belief is false — git marks the
INDEX and writes nothing into the file — and because %A arrives holding the
OURS copy, the assertion was pinning the driver to leaving a markerless file
with the incoming side's content missing. The defect was tested-in, which is
why it survived to lose content in three merges in one night. What a refusal
must now leave behind is covered by
`tests/test_doc_merge_refusal_loses_no_content.py`.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
DRIVER = REPO_ROOT / "scripts" / "git_merge_driver_docs.sh"
ATTRIBUTES = REPO_ROOT / ".gitattributes"

WORK_BASE = """\
# QAMC Current Work

## THE FUNNEL QUEUE — why trades do not happen, ranked by measured cost

**1. The first queue item — OPEN.** Queue body one.

**2. The second queue item — OPEN.** Queue body two.

**Retired item numbers — never reuse.** 4 in this queue, and 1 in the PM test gate, were deleted once written up in `docs/INCIDENT_HISTORY.md`.
"""


def _run(base: str, ours: str, theirs: str, tree_path: str, tmp_path: Path):
    b, a, t = tmp_path / "base", tmp_path / "ours", tmp_path / "theirs"
    b.write_text(base)
    a.write_text(ours)
    t.write_text(theirs)
    proc = subprocess.run(
        [str(DRIVER), str(b), str(a), str(t), tree_path],
        capture_output=True, text=True, cwd=REPO_ROOT,
    )
    return proc, a


def test_gitattributes_routes_all_three_documents_to_the_driver():
    text = ATTRIBUTES.read_text()
    for doc in ("docs/WORK.md", "docs/BOARD_NOTES.md", "docs/INCIDENT_HISTORY.md"):
        assert f"{doc} merge=docsmerge" in text, doc


def test_clean_merge_exits_zero_and_writes_the_result_to_ours(tmp_path):
    """The common case: both sides agree (here, trivially, neither side
    changed anything), so the driver should behave like git's own successful
    merge — exit 0, %A holding the merged text."""
    proc, ours_path = _run(WORK_BASE, WORK_BASE, WORK_BASE, "docs/WORK.md", tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert ours_path.read_text() == WORK_BASE


def test_a_refusal_exits_nonzero_and_leaves_a_visibly_unresolved_file(tmp_path):
    """Two sides file different text under the same item number 3 — the
    resolver's NUMBER COLLISION refusal (exit 2). git must see this as an
    unresolved path, so the wrapper must exit nonzero; and because git reads
    the worktree copy out of %A and writes no markers of its own, the file
    left there must say for itself that it is unresolved."""
    ours = WORK_BASE.replace(
        "**Retired item numbers",
        "**3. Ours: alpha finding — OPEN.** Ours body.\n\n**Retired item numbers",
    )
    theirs = WORK_BASE.replace(
        "**Retired item numbers",
        "**3. Theirs: beta finding — OPEN.** Theirs body.\n\n**Retired item numbers",
    )
    proc, ours_path = _run(WORK_BASE, ours, theirs, "docs/WORK.md", tmp_path)
    assert proc.returncode != 0, "a refusal must not look like success to git"
    assert "REFUSING TO WRITE" in proc.stderr
    result = ours_path.read_text()
    assert result != ours, (
        "the wrapper left %A as the ours copy: valid markdown, no marker, and "
        "the incoming side's content gone — the 2026-09-23 data loss"
    )
    assert "<<<<<<< " in result, "a refusal must be visible in the file itself"
    assert "Theirs: beta finding" in result, "the incoming side was dropped"


def test_an_unmapped_path_exits_nonzero_without_running_the_resolver(tmp_path):
    """A path the driver does not recognise (should never happen given
    .gitattributes, but defence in depth) must fail closed, not guess."""
    proc, ours_path = _run(WORK_BASE, WORK_BASE, WORK_BASE, "docs/UNKNOWN.md", tmp_path)
    assert proc.returncode != 0
    assert "no resolver kind" in proc.stderr


def test_board_notes_and_incident_history_route_to_their_own_kinds(tmp_path):
    notes = "# Board notes\n\n## item 1\n\nnote one.\n"
    proc, ours_path = _run(notes, notes, notes, "docs/BOARD_NOTES.md", tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert ours_path.read_text() == notes

    history = "# Incident history\n\n### 2026-09-14 — one entry\n\nbody.\n\n---\n\n"
    proc, ours_path = _run(history, history, history, "docs/INCIDENT_HISTORY.md", tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert ours_path.read_text() == history


def test_wrong_argument_count_fails_closed():
    proc = subprocess.run([str(DRIVER), "one", "two"],
                          capture_output=True, text=True, cwd=REPO_ROOT)
    assert proc.returncode != 0
