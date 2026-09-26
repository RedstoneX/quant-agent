"""Tests for scripts/prune_worktrees.py.

Two layers:
  1. The pure classifier — driven with injected git/fs stubs, so every branch
     of "is this scratch worktree safe to remove?" is exercised without a real
     repo. This is the load-bearing logic: it decides what gets deleted.
  2. A real-git integration test — a live repo with worktrees under a fake
     scratch prefix, one whose directory is deleted (gone), one merged+clean+
     old (stale), one dirty, one unmerged, one too-recent, and one outside the
     scratch prefix — asserting the sweep reports and prunes exactly the right
     ones and touches nothing else. Real git, because the whole point of the
     script is shelling out to `git worktree` correctly.
"""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = PROJECT_ROOT / "scripts" / "prune_worktrees.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("prune_worktrees_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module  # dataclasses needs this registered pre-exec
    spec.loader.exec_module(module)
    return module


mod = _load_module()


# ---------------------------------------------------------------------------
# porcelain parsing
# ---------------------------------------------------------------------------

def test_parse_porcelain_fields():
    text = (
        "worktree /home/u/repo\n"
        "HEAD abc123\n"
        "branch refs/heads/main\n"
        "\n"
        "worktree /tmp/wt/a\n"
        "HEAD def456\n"
        "branch refs/heads/feature\n"
        "locked\n"
        "\n"
        "worktree /tmp/wt/b\n"
        "HEAD 789aaa\n"
        "detached\n"
        "prunable gitdir file points to non-existent location\n"
    )
    entries = mod.parse_porcelain(text)
    assert [e.path for e in entries] == ["/home/u/repo", "/tmp/wt/a", "/tmp/wt/b"]
    assert entries[0].branch == "refs/heads/main"
    assert entries[1].locked is True
    assert entries[2].detached is True
    assert entries[2].prunable is True


# ---------------------------------------------------------------------------
# pure classifier
# ---------------------------------------------------------------------------

def _wt(path, head="h", branch="refs/heads/x", **kw):
    return mod.Worktree(path=path, head=head, branch=branch, **kw)


def _classify(entries, *, exists, merged, clean, uid=1000, stat_uid=1000, age_days=30.0, min_age=7.0):
    now = 1_000_000_000.0

    class _St:
        st_uid = stat_uid
        st_mtime = now - age_days * 86400.0

    return mod.classify(
        entries,
        repo_root="/home/u/repo",
        scratch_prefixes=["/tmp/wt", "/home/u/repo/.claude/worktrees"],
        min_age_days=min_age,
        now=now,
        our_uid=uid,
        is_merged=lambda w: merged,
        is_clean=lambda w: clean,
        path_exists=lambda p: exists,
        path_stat=lambda p: _St(),
    )


def test_main_worktree_always_kept():
    c = _classify([_wt("/home/u/repo", branch="refs/heads/main")], exists=True, merged=True, clean=True)
    assert len(c.kept) == 1 and c.kept[0][1] == "main worktree"
    assert not c.gone and not c.stale


def test_non_scratch_path_kept():
    c = _classify([_wt("/home/u/review-checkout")], exists=True, merged=True, clean=True)
    assert c.kept and "not under a scratch prefix" in c.kept[0][1]


def test_gone_path_is_gone_bucket():
    c = _classify([_wt("/tmp/wt/dead")], exists=False, merged=False, clean=False)
    assert len(c.gone) == 1 and not c.stale and not c.kept


def test_prunable_flag_forces_gone_even_if_stub_says_exists():
    c = _classify([_wt("/tmp/wt/x", prunable=True)], exists=True, merged=True, clean=True)
    assert len(c.gone) == 1


def test_stale_when_merged_clean_old():
    c = _classify([_wt("/tmp/wt/done")], exists=True, merged=True, clean=True)
    assert len(c.stale) == 1 and not c.gone and not c.kept


def test_dirty_worktree_kept():
    c = _classify([_wt("/tmp/wt/dirty")], exists=True, merged=True, clean=False)
    assert c.kept and c.kept[0][1] == "working tree not clean"
    assert not c.stale


def test_unmerged_worktree_kept():
    c = _classify([_wt("/tmp/wt/wip")], exists=True, merged=False, clean=True)
    assert c.kept and c.kept[0][1] == "branch not merged into base"
    assert not c.stale


def test_recent_worktree_kept():
    c = _classify([_wt("/tmp/wt/fresh")], exists=True, merged=True, clean=True, age_days=1.0)
    assert c.kept and "too recent" in c.kept[0][1]
    assert not c.stale


def test_locked_worktree_kept():
    c = _classify([_wt("/tmp/wt/held", locked=True)], exists=True, merged=True, clean=True)
    assert c.kept and c.kept[0][1] == "locked"
    assert not c.stale


def test_other_users_worktree_never_removed():
    """The tenant-safety guard: a scratch path owned by another uid is kept."""
    c = _classify([_wt("/tmp/wt/theirs")], exists=True, merged=True, clean=True, stat_uid=1234, uid=1000)
    assert c.kept and "not us" in c.kept[0][1]
    assert not c.stale


# ---------------------------------------------------------------------------
# real-git integration
# ---------------------------------------------------------------------------

def _git(args, cwd):
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True)


def _run_script(args):
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True,
        text=True,
    )


@pytest.fixture
def repo(tmp_path):
    """A repo whose `origin/main` exists, plus a scratch prefix of worktrees."""
    origin = tmp_path / "origin"
    origin.mkdir()
    _git(["init", "-q", "-b", "main", "."], origin)
    _git(["config", "user.email", "t@t"], origin)
    _git(["config", "user.name", "t"], origin)
    (origin / "f.txt").write_text("base\n")
    _git(["add", "f.txt"], origin)
    _git(["commit", "-qm", "base"], origin)

    work = tmp_path / "work"
    _git(["clone", "-q", str(origin), str(work)], tmp_path)
    _git(["config", "user.email", "t@t"], work)
    _git(["config", "user.name", "t"], work)
    return work


def _add_wt(work, scratch, name, branch, from_ref="origin/main"):
    path = scratch / name
    _git(["worktree", "add", "-q", "-b", branch, str(path), from_ref], work)
    return path


def _old(path):
    old = time.time() - 30 * 86400
    os.utime(path, (old, old))


def test_integration_report_and_prune(repo, tmp_path):
    scratch = tmp_path / "scratch"
    scratch.mkdir()

    # gone: created then directory deleted (session left registration behind)
    gone = _add_wt(repo, scratch, "gone", "b-gone")
    import shutil

    shutil.rmtree(gone)

    # stale: merged into origin/main (points at base), clean, old
    stale = _add_wt(repo, scratch, "stale", "b-stale")
    _old(stale)

    # unmerged: has a commit not in origin/main
    unmerged = _add_wt(repo, scratch, "unmerged", "b-unmerged")
    (unmerged / "new.txt").write_text("x\n")
    _git(["add", "new.txt"], unmerged)
    _git(["commit", "-qm", "wip"], unmerged)
    _old(unmerged)

    # dirty: merged base but uncommitted change
    dirty = _add_wt(repo, scratch, "dirty", "b-dirty")
    (dirty / "f.txt").write_text("dirty\n")
    _old(dirty)

    # recent: merged + clean but young
    _add_wt(repo, scratch, "recent", "b-recent")

    # outside scratch: a worktree NOT under the scratch prefix
    outside = tmp_path / "keep-me"
    _git(["worktree", "add", "-q", "-b", "b-outside", str(outside), "origin/main"], repo)
    _old(outside)

    common = ["--repo", str(repo), "--base-ref", "origin/main", "--scratch-prefix", str(scratch)]

    # Report mode: finds gone + stale, exits 1, changes nothing.
    r = _run_script(common)
    assert r.returncode == 1, r.stdout + r.stderr
    assert "1 gone, 1 stale" in r.stdout, r.stdout
    assert stale.exists()  # not removed in report mode
    # gone registration still present until we prune
    lst = subprocess.run(["git", "-C", str(repo), "worktree", "list"], capture_output=True, text=True)
    assert "gone" in lst.stdout

    # Prune mode: clears gone + removes stale, exits 0.
    r = _run_script(common + ["--prune"])
    assert r.returncode == 0, r.stdout + r.stderr
    assert not stale.exists(), "stale worktree should have been removed"

    lst = subprocess.run(
        ["git", "-C", str(repo), "worktree", "list", "--porcelain"],
        capture_output=True, text=True,
    ).stdout
    # gone + stale cleared; everything else survives.
    assert "/gone" not in lst
    assert "b-stale" not in lst
    assert unmerged.exists() and "b-unmerged" in lst
    assert dirty.exists() and "b-dirty" in lst
    assert "b-recent" in lst
    assert outside.exists() and "b-outside" in lst  # non-scratch untouched


def test_integration_json_output(repo, tmp_path):
    import json

    scratch = tmp_path / "scratch"
    scratch.mkdir()
    stale = _add_wt(repo, scratch, "stale", "b-stale")
    _old(stale)

    r = _run_script(["--repo", str(repo), "--scratch-prefix", str(scratch), "--json"])
    assert r.returncode == 1
    payload = json.loads(r.stdout)
    assert len(payload["stale"]) == 1
    assert payload["pruned"] is False
