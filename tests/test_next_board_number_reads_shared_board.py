"""`scripts/next_board_number.py` must allocate out of the SHARED board.

THE FAILURE THIS PINS. On 2026-10-01 two agents working in parallel were
both handed item number 222, for completely different defects, and one had
to be renumbered by hand. Neither agent did anything wrong: the script read
`docs/WORK.md` out of the working tree, the main development checkout on
this box is shared and nobody pulls it, and it was 120 commits behind
`origin/main`. The number was already taken on the board BEFORE either
agent asked, and nothing in the check could see it — not even the open-
pull-request half, which looks forward at numbers claimed on branches
rather than backward at numbers that have already landed.

The board is shared state, so its authoritative copy is the shared ref.
"""

from __future__ import annotations

import subprocess

import pytest

from scripts import next_board_number
from scripts.board_numbers import OpenPrClaims, read_ref_work_md

RETIRED = "**Retired item numbers — never reuse.**\n\n- retired queue: 3, 5\n"


def _board(*live: int) -> str:
    items = "".join(f"\n**{n}. Something live**\n" for n in live)
    return items + "\n" + RETIRED


def _git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)


@pytest.fixture()
def stale_checkout(tmp_path):
    """A checkout whose `origin/main` carries item 222 and whose working
    tree — the exact 2026-10-01 situation — does not."""
    upstream = tmp_path / "upstream"
    (upstream / "docs").mkdir(parents=True)
    _git(upstream.parent, "init", "-q", "-b", "main", str(upstream))
    _git(upstream, "config", "user.email", "t@example.com")
    _git(upstream, "config", "user.name", "t")
    (upstream / "docs" / "WORK.md").write_text(_board(7))
    _git(upstream, "add", "docs/WORK.md")
    _git(upstream, "commit", "-qm", "stale point")

    clone = tmp_path / "clone"
    _git(tmp_path, "clone", "-q", str(upstream), str(clone))

    # The board moves on upstream; the clone never pulls.
    (upstream / "docs" / "WORK.md").write_text(_board(7, 222))
    _git(upstream, "add", "docs/WORK.md")
    _git(upstream, "commit", "-qm", "item 222 lands")
    _git(clone, "fetch", "-q", "origin")
    # Working tree deliberately left on the old content.
    (clone / "docs" / "WORK.md").write_text(_board(7))
    return clone


def _run(work_md, monkeypatch, claims, *extra):
    monkeypatch.setattr(next_board_number, "read_open_pr_claims", lambda *_a, **_k: claims)
    return next_board_number.main(["--work-md", str(work_md), *extra])


def test_it_refuses_a_number_the_shared_board_has_already_used(stale_checkout, monkeypatch, capsys):
    """The 2026-10-01 collision, reproduced: 222 is taken on the ref."""
    work_md = stale_checkout / "docs" / "WORK.md"
    assert "222" not in work_md.read_text()  # the stale tree cannot see it

    assert _run(work_md, monkeypatch, OpenPrClaims(by_pr={})) == 0
    out = capsys.readouterr().out
    assert "Next free board item number: 223" in out
    assert "origin/main" in out


def test_it_says_which_source_the_board_came_from(stale_checkout, monkeypatch, capsys):
    work_md = stale_checkout / "docs" / "WORK.md"
    _run(work_md, monkeypatch, OpenPrClaims(by_pr={}))
    out = capsys.readouterr().out
    assert "shared, authoritative" in out
    assert "WORKING TREE ONLY" not in out


def test_a_local_item_not_yet_pushed_still_counts(stale_checkout, monkeypatch, capsys):
    """Falling back on the ref must not lose the agent's own local item."""
    work_md = stale_checkout / "docs" / "WORK.md"
    work_md.write_text(_board(7, 400))
    _run(work_md, monkeypatch, OpenPrClaims(by_pr={}))
    assert "Next free board item number: 401" in capsys.readouterr().out


def test_no_origin_falls_back_soft_and_states_why(tmp_path, monkeypatch, capsys):
    """An agent must never be BLOCKED here — it could not file at all."""
    work_md = tmp_path / "WORK.md"
    work_md.write_text(_board(7))
    assert _run(work_md, monkeypatch, OpenPrClaims(by_pr={})) == 0
    out = capsys.readouterr().out
    assert "Next free board item number: 8" in out
    assert "WORKING TREE ONLY" in out


def test_an_unfetched_ref_falls_back_soft_and_states_why(stale_checkout, monkeypatch, capsys):
    work_md = stale_checkout / "docs" / "WORK.md"
    assert _run(work_md, monkeypatch, OpenPrClaims(by_pr={}), "--board-ref", "origin/never-fetched") == 0
    out = capsys.readouterr().out
    assert "WORKING TREE ONLY" in out
    assert "origin/never-fetched" in out


def test_the_ref_read_never_fetches(stale_checkout):
    """Cheap and offline by construction: `git show`, never `git fetch`."""
    calls = []

    def fake_run(args):
        calls.append(args)
        return subprocess.run(args, capture_output=True, text=True)

    read_ref_work_md(stale_checkout / "docs" / "WORK.md", run=fake_run)
    assert calls
    # The git SUBCOMMAND, not the path (a tmp dir can be named
    # anything), is what must never be a network one.
    subcommands = [call[3] for call in calls]
    assert set(subcommands) <= {"rev-parse", "show"}, subcommands


def test_the_dangerous_override_is_still_refused_as_unchecked(stale_checkout, monkeypatch, capsys):
    """`--accept-unchecked-number` keeps its label and its warning."""
    work_md = stale_checkout / "docs" / "WORK.md"
    monkeypatch.setattr(next_board_number, "read_open_pr_claims", lambda *_a, **_k: pytest.fail("must not be read"))
    assert next_board_number.main(["--work-md", str(work_md), "--accept-unchecked-number"]) == 0
    out = capsys.readouterr().out
    assert "(UNCHECKED)" in out
    assert "open pull requests were NOT read" in out
