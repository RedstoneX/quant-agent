"""The board-rot guard's own proofs, split out of test_status_board.py."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from scripts import board_rot_guard, guard_reference
from scripts.guard_reference import ReferenceUnavailable


def test_the_trunk_board_is_actually_parsed():
    """A guard whose reference read returned nothing would pass vacuously."""
    paths = board_rot_guard.trunk_board_paths()
    assert "docs/WORK.md" in paths
    assert sum(1 for p in paths if p.startswith("docs/board_notes/")) > 10, paths
    board_rot_guard.trunk_flagged()  # refuses if the trunk board parses to nothing


def test_a_vacuous_reference_board_is_refused_not_passed(monkeypatch):
    """A board the parser could not read must refuse, never report clean."""
    monkeypatch.setattr(board_rot_guard, "_measure",
                        lambda root: (set(), ["the queue could not be read."]))
    with pytest.raises(ReferenceUnavailable) as exc:
        board_rot_guard.trunk_flagged()
    assert "vacuous" in str(exc.value)
    with pytest.raises(ReferenceUnavailable):
        board_rot_guard.working_flagged()


_SMALL_VALID_BOARD = """# QAMC Current Work

## PM TEST GATE

**The gate is EMPTY** -- every gate item closed.

<!-- END PM TEST GATE -->

## THE FUNNEL QUEUE -- ranked

**1. One open item -- filed 2026-10-04. OPEN: still being built.**

DONE WHEN:
  - [ ] the thing is built

## Evidence-only follow-ups
"""


def _board_root(tmp_path: Path, text: str) -> Path:
    (tmp_path / "docs" / "board_notes").mkdir(parents=True)
    (tmp_path / "docs" / "WORK.md").write_text(text, encoding="utf-8")
    return tmp_path


def test_a_small_but_whole_board_is_measured_not_refused(tmp_path):
    """Finishing work is not a parse failure: one open item, or none, passes."""
    flagged, problems = board_rot_guard._measure(
        _board_root(tmp_path, _SMALL_VALID_BOARD))
    assert problems == []
    assert flagged == set()

    all_done = _SMALL_VALID_BOARD.replace(
        "OPEN: still being built.**", "DONE 2026-10-04: built.**"
    ).replace("- [ ] the thing", "- [x] the thing")
    flagged, problems = board_rot_guard._measure(
        _board_root(tmp_path / "zero_open", all_done))
    assert problems == []
    assert flagged == {"item 1"}  # zero open items is a readable state


@pytest.mark.parametrize("damage", [
    # cut off part-way through the queue: no closing heading after it
    lambda s: s.split("## Evidence-only")[0],
    # the queue heading itself is gone
    lambda s: s.replace("## THE FUNNEL QUEUE", "## THE QUEUE"),
    # the gate heading is gone
    lambda s: s.replace("## PM TEST GATE", "## GATE"),
    # the gate lost both its items and its EMPTY declaration
    lambda s: s.replace("**The gate is EMPTY** -- every gate item closed.", ""),
    # the file is not there at all
    lambda s: None,
])
def test_an_unreadable_or_truncated_board_is_refused(tmp_path, damage):
    """A board the parser cannot read whole refuses, loudly, item count aside."""
    broken = damage(_SMALL_VALID_BOARD)
    root = _board_root(tmp_path, broken if broken is not None else "")
    if broken is None:
        (root / "docs" / "WORK.md").unlink()
    flagged, problems = board_rot_guard._measure(root)
    assert problems, "a damaged board measured as whole"
    assert flagged == set()


def test_it_refuses_when_the_trunk_cannot_be_read(tmp_path, monkeypatch):
    """No origin/main, no comparison -- and therefore no pass."""
    repo = tmp_path / "norepo"
    (repo / "docs").mkdir(parents=True)
    (repo / "docs" / "WORK.md").write_text("# board\n")
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    subprocess.run(["git", "add", "docs/WORK.md"], cwd=repo, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@example.invalid", "-c", "user.name=t",
         "commit", "-qm", "base"], cwd=repo, check=True,
    )
    monkeypatch.setattr(guard_reference, "ROOT", Path(repo))
    monkeypatch.setattr(board_rot_guard, "ROOT", Path(repo))

    with pytest.raises(ReferenceUnavailable) as exc:
        board_rot_guard.trunk_flagged()
    assert "origin/main" in str(exc.value)
    assert board_rot_guard.main() == 2
