"""Both sides of a ratchet must come from the same moment.

On ``pull_request`` CI the tree under test is GitHub's merge ref: the branch
merged into whatever main was when GitHub last computed it. Fetching
``origin/main`` fresh and comparing against its CURRENT tip therefore measures
a stale tree against a newer trunk, and reports main's own later commits as
growth the branch caused (measured 2026-10-04 on PRs 1158 and 1172: a 600-line
"growth" of a file neither branch touched, on 17 of 18 red changes).

These tests build that exact situation in a scratch repo and assert three
things: the phantom is gone, a branch that genuinely grows a guarded file is
still REFUSED, and anything short of a verified merge ref falls back to the
current ``origin/main`` rather than to a reference of the branch's choosing.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from scripts import file_size_guard, guard_reference

AUTHOR = ["-c", "user.email=t@example.invalid", "-c", "user.name=t"]


def _git(repo: Path, *args: str) -> str:
    out = subprocess.run(
        ["git", *AUTHOR, *args], cwd=repo, capture_output=True, text=True, check=True
    )
    return out.stdout.strip()


def _commit(repo: Path, message: str) -> str:
    _git(repo, "add", "-A")  # scratch repo only; never the project tree
    _git(repo, "commit", "-qm", message)
    return _git(repo, "rev-parse", "HEAD")


def _write(repo: Path, name: str, lines: int) -> None:
    (repo / name).write_text("".join(f"x = {i}\n" for i in range(lines)))


@pytest.fixture
def stale_merge_ref(tmp_path, monkeypatch):
    """A repo in the shape CI actually tests: a merge of a branch into OLD main.

    Returns ``(repo, make_pr_event)``; ``make_pr_event`` writes the GitHub
    event payload naming the branch tip as the PR head, as CI does.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _write(repo, "big.py", 500)
    base = _commit(repo, "base: big.py at 500 lines")

    # Main moves on WITHOUT the branch: big.py is split down to 100 lines.
    _write(repo, "big.py", 100)
    _commit(repo, "main: split big.py to 100 lines")
    _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")

    _git(repo, "checkout", "-q", "-b", "feature", base)

    def finish(branch_file_lines: int | None = None) -> str:
        """Commit the branch, then merge it into OLD main, as GitHub's merge ref."""
        if branch_file_lines is not None:
            _write(repo, "big.py", branch_file_lines)
        (repo / "unrelated.py").write_text("y = 1\n")
        head = _commit(repo, "feature work")
        _git(repo, "checkout", "-q", "--detach", base)
        _git(repo, "merge", "-q", "--no-ff", "-m", "Merge feature", head)
        return head

    monkeypatch.setattr(guard_reference, "ROOT", repo)
    monkeypatch.setattr(file_size_guard, "ROOT", repo)
    yield repo, finish


def _arm_pr_event(monkeypatch, tmp_path: Path, head_sha: str) -> None:
    payload = tmp_path / "event.json"
    payload.write_text(json.dumps({"pull_request": {"head": {"sha": head_sha}}}))
    monkeypatch.setenv("GITHUB_EVENT_NAME", "pull_request")
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(payload))


def test_mains_own_later_commits_are_not_charged_to_the_branch(
    stale_merge_ref, tmp_path, monkeypatch
):
    repo, finish = stale_merge_ref
    head = finish()  # branch does not touch big.py at all
    _arm_pr_event(monkeypatch, tmp_path, head)

    assert guard_reference.trunk_rev() == _git(repo, "rev-parse", "HEAD^1")
    assert file_size_guard.violations() == []


def test_the_guard_still_refuses_a_branch_that_really_grows_a_file(
    stale_merge_ref, tmp_path, monkeypatch
):
    """A guard with no failing case is not a guard."""
    repo, finish = stale_merge_ref
    head = finish(branch_file_lines=700)  # 500 -> 700 against the main it merged
    _arm_pr_event(monkeypatch, tmp_path, head)

    bad = file_size_guard.violations()
    assert any("big.py" in line and "grew from 500 to 700" in line for line in bad), bad


def test_an_unverified_merge_ref_falls_back_to_the_current_trunk(
    stale_merge_ref, tmp_path, monkeypatch
):
    """The fallback is the strict direction, never a reference the branch picks.

    With the PR head sha not matching HEAD's second parent, the merge-ref
    reading is refused and the comparison reverts to today's ``origin/main`` --
    harsher, so no branch can win by making the detection fail.
    """
    repo, finish = stale_merge_ref
    finish()
    _arm_pr_event(monkeypatch, tmp_path, "0" * 40)

    assert guard_reference.trunk_rev() == _git(repo, "rev-parse", "origin/main")
    assert any("big.py" in line for line in file_size_guard.violations())


def test_a_push_build_is_judged_against_the_current_trunk(
    stale_merge_ref, monkeypatch
):
    repo, finish = stale_merge_ref
    finish()
    monkeypatch.delenv("GITHUB_EVENT_NAME", raising=False)
    monkeypatch.delenv("GITHUB_EVENT_PATH", raising=False)

    assert guard_reference.trunk_rev() == _git(repo, "rev-parse", "origin/main")


def test_it_still_refuses_when_the_trunk_cannot_be_read(tmp_path, monkeypatch):
    repo = tmp_path / "lonely"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _write(repo, "m.py", 3)
    _commit(repo, "base")
    monkeypatch.setattr(guard_reference, "ROOT", repo)
    with pytest.raises(guard_reference.ReferenceUnavailable):
        guard_reference.trunk_rev()


def test_a_trunk_read_once_is_not_remembered_once_it_becomes_unreadable(monkeypatch):
    """The failure shape that matters most: a reference read earlier in the
    process must not be served after the trunk stops being readable. CI 2026-10-04
    caught exactly this -- a cached tip let the refusal test pass silently."""
    assert guard_reference.trunk_rev()  # a successful read first
    monkeypatch.setattr(guard_reference, "TRUNK", "origin/no-such-branch-for-test")
    with pytest.raises(guard_reference.ReferenceUnavailable):
        guard_reference.trunk_rev()
