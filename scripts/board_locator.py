"""Find the work board by what it IS, never by where it sits.

The board is the one tracked ``.md`` file that opens with the board's own title
and sits beside a ``board_notes/`` directory of ``item-N.md`` files. Zero or
several such files is a refusal: a guard that cannot find its subject must not
pass.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Callable

from scripts.guard_reference import ROOT, ReferenceUnavailable, trunk_blobs, trunk_paths, working_paths

BOARD_TITLE = "# QAMC Current Work"
_NOTE_RE = re.compile(r"^(?:(.*)/)?board_notes/item-\d+\.md$")


def locate(paths: list[str], read: Callable[[list[str]], dict[str, str]], where: str) -> tuple[str, str]:
    """(board path, notes directory) among ``paths``; refuses unless exactly one."""
    homes = {m.group(1) or "" for p in paths if (m := _NOTE_RE.match(p))}
    cands = [p for p in paths if p.endswith(".md") and p.rpartition("/")[0] in homes]
    found = sorted(p for p, text in read(cands).items() if text.lstrip().startswith(BOARD_TITLE))
    if len(found) != 1:
        raise ReferenceUnavailable(
            f"cannot locate the board in {where}: expected exactly one .md opening with "
            f"{BOARD_TITLE!r} beside a board_notes/ directory, found {found or 'none'}; "
            f"it refuses rather than pass."
        )
    home = found[0].rpartition("/")[0]
    return found[0], (home + "/" if home else "") + "board_notes"


def working_board(root: Path | str | None = None) -> tuple[str, str]:
    """The board under ``root`` (default: this checkout); paths are relative to it."""
    if root is not None:
        return tree_board(Path(root))

    def read(cands):
        return {p: (ROOT / p).read_text(encoding="utf-8", errors="replace") for p in cands}
    return locate(working_paths("*.md"), read, "the working tree")


def trunk_board() -> tuple[str, str]:
    return locate(trunk_paths(".md"), trunk_blobs, "the trunk")


def tree_board(tree: Path) -> tuple[str, str]:
    """``working_board`` for any checkout, not only this one.

    Reads ``git ls-files`` in ``tree`` so a guard handed a throwaway
    repository locates that repository's board, never this one's. A plain
    directory with no git history is walked instead.
    """
    out = subprocess.run(["git", "-C", str(tree), "ls-files", "*.md"],
                         capture_output=True, text=True)
    if out.returncode == 0:
        paths = sorted(p for p in out.stdout.splitlines() if (tree / p).is_file())
    else:
        paths = sorted(str(p.relative_to(tree)) for p in tree.rglob("*.md")
                       if ".git" not in p.parts and p.is_file())

    def read(cands):
        return {p: (tree / p).read_text(encoding="utf-8", errors="replace") for p in cands}
    return locate(paths, read, f"the tree at {tree}")


def board_path_in(tree: Path = ROOT) -> str | None:
    """``tree``'s board path relative to it, or None when it cannot be located.

    The seam every reader of the board goes through instead of a written-down
    path: tests hand it their throwaway repository, the live gate its own.
    """
    try:
        return (working_board() if tree == ROOT else tree_board(tree))[0]
    except ReferenceUnavailable:
        return None
