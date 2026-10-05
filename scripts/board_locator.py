"""Find the work board by what it IS, never by where it sits.

The board is the one tracked ``.md`` file that opens with the board's own title
and sits beside a ``board_notes/`` directory of ``item-N.md`` files. Zero or
several such files is a refusal: a guard that cannot find its subject must not
pass.
"""
from __future__ import annotations

import re
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


def _paths_under(root: Path) -> list[str]:
    """Every file under ``root`` (a throwaway directory need not be a git repo)."""
    if not root.is_dir():
        raise ReferenceUnavailable(f"cannot locate the board: {root} is not a directory; it refuses rather than pass.")
    return sorted(
        f.relative_to(root).as_posix() for f in root.rglob("*")
        if f.is_file() and ".git" not in f.relative_to(root).parts
    )


def working_board(root: Path | str | None = None) -> tuple[str, str]:
    """The board under ``root`` (default: this repository); paths are relative to it."""
    base = ROOT if root is None else Path(root)
    paths = working_paths("*.md") if root is None else _paths_under(base)

    def read(cands):
        return {p: (base / p).read_text(encoding="utf-8", errors="replace") for p in cands}
    return locate(paths, read, "the working tree" if root is None else f"{base}")


def trunk_board() -> tuple[str, str]:
    return locate(trunk_paths(".md"), trunk_blobs, "the trunk")
