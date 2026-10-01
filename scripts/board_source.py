"""Assemble `docs/WORK.md` from one file per board item.

THE PROBLEM THIS CLOSES. `docs/WORK.md` is a single shared work board that
every change has to edit: `scripts/definition_of_done.py` reads what a
change FILED and what it CLOSED out of that file's diff, and
`scripts/board_numbers.py` allocates the next item number out of the same
text. That requirement is correct and is not weakened here. What was wrong
is that it made one physical file the collision surface for every parallel
branch: 67% of the last 300 commits touch it, and 28% of simulated parallel
branch pairs collide inside an item block [measured 2026-10-01, 120 pairs
replayed off real history].

THE FIX. Each item's block lives in its own file,
`docs/board/items/item-<N>.md`. `docs/WORK.md` keeps every heading, every
standing instruction and the item ORDER, and carries one marker line,
`<!-- item 55 -->`, where that item's block used to sit. Two branches that
each edit a different item now write to two different files and git merges
them with no driver, server-side, on its own.

NOTHING ELSE CHANGES. `work_md_text()` returns the assembled board
BYTE-IDENTICALLY to the file it replaced, so every parser, guard and test
downstream reads exactly what it read before.

BACKWARDS COMPATIBLE BY CONSTRUCTION. A tree or a git ref with no
`docs/board/items/` directory — every commit before this one, and every
test fixture that writes a plain WORK.md — assembles to itself. That is why
reading an old base commit still works.
"""

from __future__ import annotations

import re
import subprocess
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

WORK_MD_RELPATH = "docs/WORK.md"
ITEMS_RELPATH = "docs/board/items"

#: `<!-- item 39(a) -->` — the placeholder a moved item block leaves behind.
#: Anchored and whole-line: nothing else in the board looks like this.
MARKER_RE = re.compile(r"^<!-- item ([0-9]+(?:\([a-z]\))?) -->$")


def item_filename(number: str) -> str:
    return f"item-{number}.md"


def marker(number: str) -> str:
    return f"<!-- item {number} -->"


def assemble(scaffold: str, blocks: dict[str, str]) -> str:
    """`scaffold` with every `<!-- item N -->` line replaced by block N.

    A marker with no block is left as it is rather than silently dropped: a
    missing item must be visible in the board, never disappear from it.
    """
    out: list[str] = []
    for line in scaffold.split("\n"):
        m = MARKER_RE.match(line)
        if m is not None and m.group(1) in blocks:
            out.append(blocks[m.group(1)].rstrip("\n"))
        else:
            out.append(line)
    return "\n".join(out)


def read_blocks_from_tree(root: Path | None = None) -> dict[str, str]:
    root = Path(root or REPO_ROOT)
    items = root / ITEMS_RELPATH
    if not items.is_dir():
        return {}
    blocks: dict[str, str] = {}
    for path in items.glob("item-*.md"):
        blocks[path.name[len("item-"):-len(".md")]] = path.read_text()
    return blocks


def work_md_text(root: Path | None = None) -> str | None:
    """The whole board as a working tree has it, or None if absent."""
    root = Path(root or REPO_ROOT)
    scaffold = root / WORK_MD_RELPATH
    if not scaffold.is_file():
        return None
    return assemble(scaffold.read_text(), read_blocks_from_tree(root))


def work_md_path(root: Path | None = None) -> Path:
    """A path holding the whole board, for callers that take a path.

    When nothing is split out this IS `docs/WORK.md`, untouched. When items
    are split out the assembled text is materialised into a temporary file,
    because every reader downstream was written against a path and none of
    them should have to change shape for this.
    """
    root = Path(root or REPO_ROOT)
    scaffold = root / WORK_MD_RELPATH
    blocks = read_blocks_from_tree(root)
    if not blocks or not scaffold.is_file():
        return scaffold
    text = assemble(scaffold.read_text(), blocks)
    tmp = Path(tempfile.mkdtemp(prefix="qamc-board-")) / "WORK.md"
    tmp.write_text(text)
    return tmp


def _git(args: list[str], run=None):
    if run is None:
        return subprocess.run(args, capture_output=True, text=True, timeout=30)
    return run(args)


def work_md_text_at_ref(ref: str, repo: Path | None = None,
                        run=None) -> str | None:
    """The whole board as `ref` has it, assembled. None if it is not there.

    Never raises: a ref that cannot be read is None, same as a missing file.
    """
    directory = str(Path(repo or REPO_ROOT))
    try:
        shown = _git(["git", "-C", directory, "show",
                      f"{ref}:{WORK_MD_RELPATH}"], run)
    except Exception:  # noqa: BLE001 - a missing git is a missing board
        return None
    if shown.returncode != 0:
        return None
    scaffold = shown.stdout
    if not MARKER_RE.search(scaffold) and "<!-- item " not in scaffold:
        return scaffold
    blocks: dict[str, str] = {}
    try:
        listing = _git(["git", "-C", directory, "ls-tree", "--name-only",
                        f"{ref}:{ITEMS_RELPATH}"], run)
    except Exception:  # noqa: BLE001
        return scaffold
    if listing.returncode != 0:
        return scaffold
    for name in listing.stdout.split():
        if not (name.startswith("item-") and name.endswith(".md")):
            continue
        got = _git(["git", "-C", directory, "show",
                    f"{ref}:{ITEMS_RELPATH}/{name}"], run)
        if got.returncode == 0:
            blocks[name[len("item-"):-len(".md")]] = got.stdout
    return assemble(scaffold, blocks)


def work_md_text_for(path: Path) -> str | None:
    """Assembled board for a caller that was handed a `docs/WORK.md` PATH.

    A path that is not a repository's `docs/WORK.md` — a test fixture, say —
    is simply read, which is what it was before this module existed.
    """
    path = Path(path)
    if path.name == "WORK.md" and path.parent.name == "docs":
        text = work_md_text(path.parent.parent)
        if text is not None:
            return text
    try:
        return path.read_text()
    except OSError:
        return None
