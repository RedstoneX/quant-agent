"""Throwaway git repositories for the definition-of-done tests.

Real repositories, not mocks, because the thing under test IS the git read.
Lifted out of `tests/test_definition_of_done.py` so that file holds only the
checks and their proofs.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from scripts import definition_of_done as dod
from scripts.board_locator import BOARD_TITLE

REPO = Path(__file__).resolve().parents[1]

#: Where the throwaway repositories put their board. A fixture choice, not a
#: fact about the gate: the gate finds the board through `board_path_in`.
BOARD = "docs/WORK.md"


# ---------------------------------------------------------------------------
# fixtures — real repositories, because the checks really read git
# ---------------------------------------------------------------------------

_ENV = {
    "GIT_AUTHOR_NAME": "dod-test",
    "GIT_AUTHOR_EMAIL": "dod@example.com",
    "GIT_COMMITTER_NAME": "dod-test",
    "GIT_COMMITTER_EMAIL": "dod@example.com",
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_SYSTEM": "/dev/null",
}


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True, env=_ENV)


def _write(repo: Path, rel: str, text: str) -> None:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _write(repo, "docs/board_notes/item-1.md", "note\n")
    _commit(repo, "board notes", "docs/board_notes/item-1.md")
    return repo


def _commit(repo: Path, message: str, *paths: str) -> None:
    for rel in paths:
        _git(repo, "add", rel)
    _git(repo, "commit", "-q", "-m", message)


def _board(items: str, retired: str) -> str:
    return (
        f"{BOARD_TITLE}\n\n"
        "## THE FUNNEL QUEUE\n\n"
        f"{items}\n\n"
        "**Retired item numbers — never reuse.** APPEND-ONLY.\n"
        f"- retired queue: {retired}\n"
    )


def _change(repo: Path, base: str) -> dod.Change:
    """The same object the live gate builds, for a throwaway repository."""
    board = dod.board_path_in(repo)
    return dod.Change(
        base=base,
        paths=dod.changed_paths(base, repo),
        messages=dod.commit_messages(base, repo),
        work_md_before=dod.file_at(base, board, repo) if board else None,
        work_md_after=(repo / board).read_text() if board else None,
        tree=repo,
    )


def _change_retiring_an_item(messages: str, tmp_path: Path | None = None) -> dod.Change:
    """A Change that retires one item, so the observable check arms.

    The tree is this repository, so a path cited in `messages` resolves the
    way it does in the live gate.
    """
    return dod.Change(
        base="BASE",
        paths=["docs/WORK.md"],
        messages=dod.unwrap_trailers(messages),
        work_md_before=_board("**7. Thing — OPEN.**", "1, 2"),
        work_md_after=_board("", "1, 2, 7"),
        tree=Path(__file__).resolve().parents[1],
    )


def _base_with_board(tmp_path: Path, items: str, retired: str = "1, 2") -> tuple[Path, str]:
    repo = _repo(tmp_path)
    _write(repo, BOARD, _board(items, retired))
    _commit(repo, "base board", BOARD)
    base = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()
    return repo, base
