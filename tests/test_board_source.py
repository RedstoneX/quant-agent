"""The board is assembled from one file per item, and loses nothing.

THE DEFECT THIS CLOSES. `docs/WORK.md` was one physical file that every
change had to edit, so unrelated branches collided over bookkeeping. The
items now live one per file and the board is assembled on read. The whole
safety of that move rests on one property — the assembled text is the SAME
text — so that property is tested here rather than trusted.
"""

import re
import subprocess
from pathlib import Path

import pytest

from scripts import board_source

REPO_ROOT = Path(__file__).resolve().parent.parent
ITEM_HEADING = re.compile(r"^\*\*([0-9]+(?:\([a-z]\))?)\.", re.M)


def test_every_marker_has_an_item_file():
    scaffold = (REPO_ROOT / "docs" / "WORK.md").read_text()
    blocks = board_source.read_blocks_from_tree(REPO_ROOT)
    markers = [m.group(1) for line in scaffold.split("\n")
               if (m := board_source.MARKER_RE.match(line))]
    assert markers, "the board has no item markers at all"
    missing = [n for n in markers if n not in blocks]
    assert not missing, f"markers with no item file: {missing}"


def test_no_item_block_was_left_in_the_scaffold():
    """An item block in both places would be rendered twice."""
    scaffold = (REPO_ROOT / "docs" / "WORK.md").read_text()
    assert not ITEM_HEADING.findall(scaffold)


def test_every_item_file_is_reachable_from_the_board():
    scaffold = (REPO_ROOT / "docs" / "WORK.md").read_text()
    for number in board_source.read_blocks_from_tree(REPO_ROOT):
        assert board_source.marker(number) in scaffold, (
            f"item {number} has a file but no place on the board")


def test_assembled_board_matches_the_last_single_file_version():
    """NOTHING WAS LOST. Every item, every criterion checkbox and every
    retirement record is present, with identical bytes, against the board as
    the merge-base has it."""
    base = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "merge-base", "HEAD", "origin/main"],
        capture_output=True, text=True)
    if base.returncode != 0:
        pytest.skip("no origin/main to compare against")
    before = board_source.work_md_text_at_ref(base.stdout.strip(), REPO_ROOT)
    if before is None:
        pytest.skip("the base board could not be read")
    after = board_source.work_md_text(REPO_ROOT)
    assert after == before


def test_a_tree_with_no_item_files_assembles_to_itself(tmp_path):
    """Backwards compatibility: every commit before the split, and every
    fixture that writes a plain board, must still read."""
    (tmp_path / "docs").mkdir()
    text = "# board\n\n**9. an item.**\n\nDONE WHEN:\n  - [ ] something\n"
    (tmp_path / "docs" / "WORK.md").write_text(text)
    assert board_source.work_md_text(tmp_path) == text
    assert board_source.work_md_path(tmp_path) == tmp_path / "docs" / "WORK.md"


def test_a_marker_with_no_file_is_left_visible_not_dropped():
    assembled = board_source.assemble("a\n<!-- item 404 -->\nb\n", {})
    assert "<!-- item 404 -->" in assembled
