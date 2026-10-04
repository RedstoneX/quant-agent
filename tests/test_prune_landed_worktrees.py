"""The pruner removes only landed-and-clean worktrees; everything else is refused."""
import importlib.util
import subprocess
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "prune", Path(__file__).resolve().parents[1] / "scripts" / "prune_landed_worktrees.py")
prune = importlib.util.module_from_spec(spec)
spec.loader.exec_module(prune)


def g(cwd, *a):
    subprocess.run(["git", *a], cwd=cwd, check=True, capture_output=True)


def make(tmp_path):
    """A real repo + bare 'remote' + one pushed-branch worktree."""
    remote = tmp_path / "remote.git"
    g(tmp_path, "init", "-q", "--bare", str(remote))
    repo = tmp_path / "repo"
    g(tmp_path, "init", "-q", str(repo))
    g(repo, "config", "user.email", "t@t")
    g(repo, "config", "user.name", "t")
    (repo / "f").write_text("x")
    g(repo, "add", "f")
    g(repo, "commit", "-qm", "init")
    g(repo, "remote", "add", "origin", str(remote))
    g(repo, "push", "-q", "origin", "HEAD:refs/heads/main")
    g(repo, "fetch", "-q", "origin")
    wt = tmp_path / "wt"
    g(repo, "worktree", "add", "-q", "-b", "feat", str(wt))
    g(repo, "push", "-q", "origin", "feat")
    g(repo, "fetch", "-q", "origin")
    return repo, wt


def entry(repo, wt):
    return next(t for t in prune.list_worktrees(str(repo)) if t["worktree"] == str(wt))


def merged(branch, repo):
    return [{"number": 1, "state": "MERGED"}]


def test_removes_merged_clean_pushed(tmp_path, monkeypatch):
    repo, wt = make(tmp_path)
    ok, why = prune.judge(entry(repo, wt), str(repo), states=merged)
    assert ok, why
    monkeypatch.setattr(prune, "pr_states", merged)
    assert prune.main(["--repo", str(repo), "--apply"]) == 0
    assert not wt.exists()


def test_dry_run_deletes_nothing(tmp_path, monkeypatch):
    repo, wt = make(tmp_path)
    monkeypatch.setattr(prune, "pr_states", merged)
    assert prune.main(["--repo", str(repo)]) == 0
    assert wt.exists()


def test_refuses_uncommitted_changes(tmp_path):
    repo, wt = make(tmp_path)
    (wt / "new.txt").write_text("precious")
    ok, why = prune.judge(entry(repo, wt), str(repo), states=merged)
    assert not ok and "uncommitted" in why
    assert (wt / "new.txt").exists()


def test_refuses_open_pr(tmp_path):
    repo, wt = make(tmp_path)
    ok, why = prune.judge(entry(repo, wt), str(repo),
                          states=lambda b, r: [{"number": 7, "state": "OPEN"}])
    assert not ok and "OPEN" in why


def test_refuses_unpushed_commits_and_unknown_pr(tmp_path):
    repo, wt = make(tmp_path)
    (wt / "c").write_text("c")
    g(wt, "add", "c")
    g(wt, "commit", "-qm", "local only")
    ok, why = prune.judge(entry(repo, wt), str(repo), states=merged)
    assert not ok and "unpushed" in why
    ok, why = prune.judge(entry(repo, wt), str(repo), states=lambda b, r: [])
    assert not ok and "no PR" in why


def test_refuses_when_github_unreadable(tmp_path):
    repo, wt = make(tmp_path)

    def boom(b, r):
        raise RuntimeError("gh down")
    ok, why = prune.judge(entry(repo, wt), str(repo), states=boom)
    assert not ok and "cannot read" in why


DIRTY = "uncommitted or untracked changes"
NOPR = "no PR found for branch"
FUTURE = lambda: __import__("time").time() + 100 * 3600  # noqa: E731


def _stale(repo, wt, reason=DIRTY, states=lambda b, r: [], now=None, cwd="/nonexistent"):
    return prune.judge_stale(entry(repo, wt), str(repo), reason, states=states, cwd=cwd,
                             now=now if now is not None else FUTURE())


def test_stale_rule_fires_on_old_prless_dirty_tree(tmp_path):
    repo, wt = make(tmp_path)
    (wt / "stray").write_text("x")
    ok, why = _stale(repo, wt)
    assert ok and "STALE" in why


def test_stale_rule_holds_back_recent_tree(tmp_path):
    repo, wt = make(tmp_path)
    (wt / "stray").write_text("x")
    ok, why = _stale(repo, wt, now=__import__("time").time())
    assert not ok and "activity within" in why


def test_stale_rule_holds_back_open_pr(tmp_path):
    repo, wt = make(tmp_path)
    ok, why = _stale(repo, wt, states=lambda b, r: [{"number": 7, "state": "OPEN"}])
    assert not ok and "open PR" in why


def test_stale_rule_holds_back_live_cwd_and_unlisted_reason(tmp_path):
    repo, wt = make(tmp_path)
    assert not _stale(repo, wt, cwd=str(wt / "sub"))[0]
    assert not _stale(repo, wt, reason="unpushed commits")[0]
    assert not _stale(repo, wt, reason="PR #1 is OPEN")[0]


def test_stale_rule_holds_back_on_pr_lookup_failure(tmp_path):
    repo, wt = make(tmp_path)
    def boom(b, r):
        raise RuntimeError("gh down")
    assert not _stale(repo, wt, states=boom)[0]


def test_stale_rule_old_commit_but_fresh_file_is_kept(tmp_path):
    repo, wt = make(tmp_path)
    (wt / "stray").write_text("x")
    # commit is "old" (now far in future) but a file claims to be newer than the cutoff
    ok, _ = prune.judge_stale(entry(repo, wt), str(repo), NOPR, states=lambda b, r: [],
                              cwd="/nonexistent", now=FUTURE(), newer=lambda p, c: True)
    assert not ok
