"""The locator can be pointed at a root, so tests serve it a fake board without a path constant."""

from __future__ import annotations

import pytest

from scripts.board_locator import BOARD_TITLE, working_board
from scripts.guard_reference import ReferenceUnavailable


def test_a_shaped_board_under_a_root_is_found(tmp_path):
    (tmp_path / "x" / "board_notes").mkdir(parents=True)
    (tmp_path / "x" / "board_notes" / "item-1.md").write_text("n\n")
    (tmp_path / "x" / "B.md").write_text(f"{BOARD_TITLE}\n")
    assert working_board(tmp_path) == ("x/B.md", "x/board_notes")


def test_no_board_under_a_root_refuses(tmp_path):
    (tmp_path / "B.md").write_text(f"{BOARD_TITLE}\n")
    with pytest.raises(ReferenceUnavailable):
        working_board(tmp_path)


def test_a_missing_root_refuses(tmp_path):
    with pytest.raises(ReferenceUnavailable):
        working_board(tmp_path / "absent")
