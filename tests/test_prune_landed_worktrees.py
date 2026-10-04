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
