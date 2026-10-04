"""Remove git worktrees whose PR has landed. Dry-run by default; --apply deletes.

A worktree is removed ONLY when every one of these is positively proven:
  - it is on a branch (not detached, not the main checkout, not locked),
  - GitHub reports at least one PR for that branch and ALL are MERGED or CLOSED,
  - it has no uncommitted/untracked changes,
  - it has no commits that are not on any remote branch.
Anything unproven (gh failure, no PR, open PR, git failure) is KEPT and listed.
Judged by PR state, never content diff: squash merges make content comparison useless.
"""
import argparse
import json
import subprocess
import sys


def run(args, cwd=None):
    r = subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        raise RuntimeError(f"{' '.join(args)}: {r.stderr.strip()}")
    return r.stdout


def list_worktrees(repo):
    out = run(["git", "worktree", "list", "--porcelain"], cwd=repo)
    trees, cur = [], {}
    for line in out.splitlines() + [""]:
        if not line:
            if cur:
                trees.append(cur)
            cur = {}
            continue
        k, _, v = line.partition(" ")
        cur[k] = v or True
    return trees


def pr_states(branch, repo):
    out = run(["gh", "pr", "list", "--head", branch, "--state", "all",
               "--json", "number,state"], cwd=repo)
    return json.loads(out)


def is_dirty(path):
    return bool(run(["git", "status", "--porcelain", "--untracked-files=all"], cwd=path).strip())


def has_unpushed(path):
    # commits reachable from HEAD but from no remote-tracking branch
    return bool(run(["git", "rev-list", "-n", "1", "HEAD", "--not", "--remotes"], cwd=path).strip())


def judge(wt, repo, states=None, dirty=is_dirty, unpushed=has_unpushed):
    """Return (removable, reason). Any doubt -> (False, reason)."""
    states = states or pr_states
    path = wt.get("worktree")
    if wt.get("bare"):
        return False, "bare repository"
    if wt.get("locked"):
        return False, "locked"
    if wt.get("prunable"):
        return False, "prunable/missing directory (use git worktree prune)"
    branch = wt.get("branch")
    if not branch:
        return False, "detached HEAD, no branch to look up"
    branch = branch.removeprefix("refs/heads/")
    try:
        prs = states(branch, repo)
    except Exception as e:
        return False, f"cannot read PR state ({e})"
    if not prs:
        return False, "no PR found for branch"
    open_ = [p for p in prs if p.get("state") not in ("MERGED", "CLOSED")]
    if open_:
        return False, f"PR #{open_[0]['number']} is {open_[0]['state']}"
    try:
        if dirty(path):
            return False, "uncommitted or untracked changes"
        if unpushed(path):
            return False, "unpushed commits"
    except Exception as e:
        return False, f"cannot inspect worktree ({e})"
    return True, "PR " + "/".join(f"#{p['number']} {p['state']}" for p in prs) + ", clean, pushed"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--apply", action="store_true", help="actually delete (default: dry-run)")
    ap.add_argument("--dry-run", action="store_true", help="default; list only")
    ap.add_argument("--repo", default=".")
    a = ap.parse_args(argv)
    if a.apply and a.dry_run:
        ap.error("--apply and --dry-run are exclusive")
    trees = list_worktrees(a.repo)
    main_path = trees[0]["worktree"] if trees else None
    remove, keep = [], []
    for wt in trees:
        if wt["worktree"] == main_path:
            keep.append((wt["worktree"], "main checkout"))
            continue
        ok, why = judge(wt, a.repo)
        (remove if ok else keep).append((wt["worktree"], why))
    verb = "REMOVING" if a.apply else "WOULD REMOVE"
    for p, why in remove:
        print(f"{verb} {p}  [{why}]")
    for p, why in keep:
        print(f"KEEP {p}  [{why}]")
    failed = 0
    if a.apply:
        for p, _ in remove:
            try:  # no --force: git itself refuses a dirty tree
                run(["git", "worktree", "remove", p], cwd=a.repo)
            except Exception as e:
                failed += 1
                print(f"FAILED {p}: {e}")
    print(f"summary: {len(remove)} removable, {len(keep)} kept, mode={'apply' if a.apply else 'dry-run'}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
