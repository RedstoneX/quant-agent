"""The board is found by shape; a vanished or ambiguous board refuses, never passes."""
from __future__ import annotations

import pytest

from scripts import board_item_guard, board_locator
from scripts.guard_reference import ReferenceUnavailable

BOARD = board_locator.BOARD_TITLE + "\n\nbody\n"


def _read(files):
    return lambda cands: {p: files[p] for p in cands if p in files}


def test_finds_the_board_wherever_it_lives():
    for home in ("docs", "elsewhere/deep", ""):
        pre = home + "/" if home else ""
        files = {pre + "ANY_NAME.md": BOARD, pre + "OTHER.md": "# other\n"}
        paths = list(files) + [pre + "board_notes/item-001.md"]
        assert board_locator.locate(paths, _read(files), "t") == (pre + "ANY_NAME.md", pre + "board_notes")


def test_a_vanished_board_refuses():
    files = {"docs/RENAMED.md": "# something else\n"}
    with pytest.raises(ReferenceUnavailable):
        board_locator.locate(["docs/RENAMED.md", "docs/board_notes/item-001.md"], _read(files), "t")
    with pytest.raises(ReferenceUnavailable):
        board_locator.locate(["docs/WORK.md"], _read({"docs/WORK.md": BOARD}), "t")


def test_two_boards_refuse():
    files = {"docs/A.md": BOARD, "docs/B.md": BOARD}
    with pytest.raises(ReferenceUnavailable):
        board_locator.locate(list(files) + ["docs/board_notes/item-001.md"], _read(files), "t")


def test_item_guard_refuses_when_the_board_is_gone(monkeypatch):
    def gone():
        raise ReferenceUnavailable("no board")
    monkeypatch.setattr(board_item_guard, "working_board", gone)
    with pytest.raises(ReferenceUnavailable):
        board_item_guard.working_offences()


def test_the_real_trunk_and_tree_boards_are_found():
    assert board_locator.working_board() == board_locator.trunk_board()
