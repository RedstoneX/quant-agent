"""Derive the repository-layout block in README.md from the tree itself.

WHY THIS EXISTS. The layout block in `README.md` names modules by hand. A
hand-maintained list of what is on disk drifts the moment a module is added,
renamed or deleted somewhere else, and nothing used to notice. The PATHS in
that block are therefore DERIVED here and pinned by
`tests/test_readme_module_tree.py`; only the per-module descriptions after the
`#` are written by a person, because no generator can know what a module is
FOR.

The block is self-describing: this module reads which directories it
enumerates out of the block itself. A directory that the block lists with
children is checked for having exactly the children on disk; a directory the
block lists as a leaf (no children under it) is left alone, so the README may
stop at any depth it likes.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
README = REPO_ROOT / "README.md"

#: Names never shown in the layout block regardless of what is on disk.
IGNORED_NAMES = {
    "__pycache__",
    ".git",
    ".venv",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "__init__.py",
}

_BRANCH = re.compile(r"^(?P<indent>[\s│]*)(?P<tee>├──|└──) (?P<name>\S+)")


def extract_block(text: str) -> tuple[int, int, list[str]]:
    """Return (start, end, lines) of the fenced layout block in `text`.

    `start` and `end` are 0-based indices of the two fence lines.
    """
    lines = text.splitlines()
    fences = [i for i, line in enumerate(lines) if line.strip().startswith("```")]
    for open_i, close_i in zip(fences[0::2], fences[1::2]):
        body = lines[open_i + 1 : close_i]
        if any(_BRANCH.match(line) for line in body):
            return open_i, close_i, body
    raise AssertionError("README.md has no fenced repository-layout block")


def committed_tree(body: list[str]) -> dict[str, set[str]]:
    """Map each enumerated directory (repo-relative, '' for the root) to the
    set of child names the README lists under it."""
    out: dict[str, set[str]] = {}
    stack: list[tuple[int, str]] = []
    for line in body:
        match = _BRANCH.match(line)
        if match is None:
            continue
        depth = len(match.group("indent"))
        name = match.group("name").rstrip("/")
        while stack and stack[-1][0] >= depth:
            stack.pop()
        parent = stack[-1][1] if stack else ""
        out.setdefault(parent, set()).add(name)
        stack.append((depth, f"{parent}/{name}".lstrip("/")))
    return out


def listed_paths(body: list[str]) -> list[str]:
    """Every repo-relative path the block names, parents before children."""
    out: list[str] = []
    for parent, children in committed_tree(body).items():
        for name in sorted(children):
            out.append(f"{parent}/{name}".lstrip("/"))
    return sorted(out)


def _ignored(paths: list[str]) -> set[str]:
    """The subset of `paths` git is told to ignore -- runtime directories such
    as `data/` and `logs/` are documented but are not in the tree."""
    if not paths:
        return set()
    result = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "check-ignore", "--stdin"],
        input="\n".join([*paths, *(f"{path}/" for path in paths)]),
        capture_output=True,
        text=True,
    )
    return {line.strip().rstrip("/") for line in result.stdout.splitlines() if line.strip()}


def drift() -> list[str]:
    """Human-readable complaints; empty means every documented path is real.

    This is deliberately ONE direction. The block is a curated tour, not an
    inventory -- demanding that every module on disk appear in it would mean
    inventing a hand-written description for each, which no generator can do.
    What IS derivable, and what actually rots, is the other direction: a path
    the README names must exist. Renames and deletions land here.
    """
    _, _, body = extract_block(README.read_text(encoding="utf-8"))
    paths = listed_paths(body)
    ignored = _ignored(paths)
    problems = []
    for path in paths:
        if path in ignored:
            continue
        if not (REPO_ROOT / path).exists():
            problems.append(f"README.md names `{path}`, which is not in the tree")
    return problems


if __name__ == "__main__":
    found = drift()
    for line in found:
        print(line)
    raise SystemExit(1 if found else 0)
