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
    """A board that parses to almost nothing must refuse, never report clean."""
    monkeypatch.setattr(board_rot_guard, "_measure", lambda root: (set(), 0))
    with pytest.raises(ReferenceUnavailable) as exc:
        board_rot_guard.trunk_flagged()
    assert "vacuous" in str(exc.value)
    with pytest.raises(ReferenceUnavailable):
        board_rot_guard.working_flagged()


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
