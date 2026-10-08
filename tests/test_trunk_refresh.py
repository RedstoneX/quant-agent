"""The trunk reference refreshes itself, and a stale one never hides a violation.

A worktree's local ``origin/main`` can lag the real remote. The branch, cut from a
fresher fetch, then holds main's own later growth and is charged for it. These
tests build that shape with a real remote and assert: the phantom is gone after
the refresh, a REAL growth still reds with and without it, and a dead network
degrades to the local ref with a loud STALE note rather than hanging or passing.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from scripts import guard_reference, trunk_refresh
from tests.test_trunk_reference_is_coherent import size_growth

AUTHOR = ["-c", "user.email=t@example.invalid", "-c", "user.name=t"]


def _git(repo: Path, *args: str) -> str:
    out = subprocess.run(["git", *AUTHOR, *args], cwd=repo, capture_output=True, text=True, check=True)
    return out.stdout.strip()


def _grow(repo: Path, lines: int, msg: str) -> None:
    (repo / "big.py").write_text("".join(f"x = {i}\n" for i in range(lines)))
    _git(repo, "add", "big.py")
    _git(repo, "commit", "-qm", msg)


@pytest.fixture
def stale_clone(tmp_path, monkeypatch):
    """A clone whose local origin/main is behind the remote, with a branch cut from the newer tip."""
    for var in ("CI", "GITHUB_ACTIONS", "GITHUB_EVENT_NAME"):
        monkeypatch.delenv(var, raising=False)
    remote = tmp_path / "remote"
    remote.mkdir()
    _git(remote, "init", "-q", "-b", "main")
    _grow(remote, 500, "base")
    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", "-q", str(remote), str(clone)], check=True)
    _grow(remote, 700, "main grows big.py legitimately")  # remote moves; clone's ref is now stale
    stale_tip = _git(clone, "rev-parse", "origin/main")
    _git(clone, "fetch", "-q", "origin", "main:refs/scratch/fresh")
    _git(clone, "update-ref", "refs/remotes/origin/main", stale_tip)  # fetch advanced it; wind it back
    _git(clone, "checkout", "-q", "-b", "feature", "refs/scratch/fresh")
    assert _git(clone, "rev-parse", "origin/main") != _git(clone, "rev-parse", "refs/scratch/fresh")
    monkeypatch.setattr(guard_reference, "ROOT", clone)
    monkeypatch.setattr(trunk_refresh, "_notes", {})
    yield clone


def _no_refresh(monkeypatch):
    monkeypatch.setattr(guard_reference, "refresh_trunk", lambda *a, **k: "")


def test_stale_ref_charges_the_branch_for_mains_growth_without_refresh(stale_clone, monkeypatch):
    _no_refresh(monkeypatch)
    assert any("big.py" in v for v in size_growth())


def test_refresh_removes_the_phantom(stale_clone):
    assert size_growth() == []


def test_a_real_growth_still_reds_with_and_without_refresh(stale_clone, monkeypatch):
    _grow(stale_clone, 900, "branch really grows big.py")
    assert any("big.py" in v and "900" in v for v in size_growth())
    monkeypatch.setattr(trunk_refresh, "_notes", {})
    _no_refresh(monkeypatch)
    assert any("big.py" in v and "900" in v for v in size_growth())


def test_dead_network_degrades_to_local_ref_and_says_so(stale_clone, capsys):
    _git(stale_clone, "remote", "set-url", "origin", str(stale_clone / "no-such-remote"))
    assert guard_reference.trunk_rev()  # still resolves from the local ref
    note = trunk_refresh.refresh_trunk(stale_clone)
    assert "STALE" in note and "LOCAL" in note
    assert "STALE" in capsys.readouterr().err


def test_unreadable_trunk_still_refuses_and_names_the_failed_refresh(stale_clone, monkeypatch):
    _git(stale_clone, "remote", "set-url", "origin", str(stale_clone / "no-such-remote"))
    monkeypatch.setattr(guard_reference, "TRUNK", "origin/no-such-branch")
    with pytest.raises(guard_reference.ReferenceUnavailable, match="STALE"):
        guard_reference.trunk_rev()


def test_one_fetch_per_run_and_none_in_ci(stale_clone, monkeypatch):
    calls = []
    real = subprocess.run
    monkeypatch.setattr(trunk_refresh.subprocess, "run", lambda *a, **k: calls.append(a) or real(*a, **k))
    trunk_refresh.refresh_trunk(stale_clone)
    trunk_refresh.refresh_trunk(stale_clone)
    assert len(calls) == 1
    monkeypatch.setattr(trunk_refresh, "_notes", {})
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    trunk_refresh.refresh_trunk(stale_clone)
    assert len(calls) == 1
