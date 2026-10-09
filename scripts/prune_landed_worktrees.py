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
import os
import subprocess
import sys
import time

# Second, age-based rule (STALE path), added so abandoned scratch trees drain.
# Measured 2026-10-04 on the 147 kept trees, hours since newest commit-or-file mtime:
#   live cluster 0-9.9h (n=~45, incl. this session's own trees); empty gap 9.9h-39.8h;
#   dead clusters at 39.8-47.5h (n~70), 55-72h, 79-96h; oldest ~96h.
# Cut = 30h = 3x the live cluster's upper edge (9.9h), 10h short of the first dead
# cluster: nothing observed live is within 3x of it. Wrongly kept costs disk only.
STALE_HOURS = 30
# A STALE removal may never touch these (other tenants / system), whatever git says.
FORBIDDEN_PREFIXES = ("/home/dev", "/home/orca", "/home/northstar", "/var/lib/docker", "/var/lib/containerd")
STALE_BUCKETS = ("uncommitted or untracked changes", "no PR found for branch", "detached HEAD, no branch to look up")


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
    out = run(["gh", "pr", "list", "--head", branch, "--state", "all", "--json", "number,state"], cwd=repo)
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


def newest_commit_ts(path):
    return int(run(["git", "log", "-1", "--format=%ct"], cwd=path).strip())


def has_file_newer_than(path, cutoff):
    """True if any file/dir under path (excluding .git) was modified after cutoff."""
    for root, dirs, files in os.walk(path):
        dirs[:] = [d for d in dirs if d != ".git"]
        for n in files + dirs:
            try:
                if os.lstat(os.path.join(root, n)).st_mtime > cutoff:
                    return True
            except OSError:
                return True  # cannot stat -> doubt -> treat as fresh
    return False


def judge_stale(
    wt,
    repo,
    reason,
    states=None,
    cwd=None,
    now=None,
    commit_ts=newest_commit_ts,
    newer=has_file_newer_than,
    hours=STALE_HOURS,
):
    """Second rule, only for the three never-landing buckets. Any doubt -> keep."""
    if reason not in STALE_BUCKETS:
        return False, reason
    states = states or pr_states
    path = os.path.realpath(wt["worktree"])
    cwd = os.path.realpath(cwd or os.getcwd())
    if path.startswith(FORBIDDEN_PREFIXES) or path == os.path.realpath(repo):
        return False, reason + "; stale rule: protected path"
    if cwd == path or cwd.startswith(path + os.sep):
        return False, reason + "; stale rule: live session directory"
    branch = (wt.get("branch") or "").removeprefix("refs/heads/")
    try:
        if branch and any(p.get("state") not in ("MERGED", "CLOSED") for p in states(branch, repo)):
            return False, reason + "; stale rule: open PR"
        cutoff = (now or time.time()) - hours * 3600
        if commit_ts(path) > cutoff or newer(path, cutoff):
            return False, reason + f"; stale rule: activity within {hours}h"
    except Exception as e:
        return False, reason + f"; stale rule: cannot prove age ({e})"
    return True, reason + f"; STALE: no commit or file change for {hours}h+, no open PR"


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
    remove, keep, stale = [], [], set()
    for wt in trees:
        if wt["worktree"] == main_path:
            keep.append((wt["worktree"], "main checkout"))
            continue
        ok, why = judge(wt, a.repo)
        if not ok:
            ok, why = judge_stale(wt, a.repo, why)
            if ok:
                stale.add(wt["worktree"])
        (remove if ok else keep).append((wt["worktree"], why))
    verb = "REMOVING" if a.apply else "WOULD REMOVE"
    for p, why in remove:
        print(f"{verb} {p}  [{why}]")
    for p, why in keep:
        print(f"KEEP {p}  [{why}]")
    failed = 0
    if a.apply:
        for p, _ in remove:
            try:  # landed trees: no --force, git refuses a dirty tree; stale ones are dirty by design
                run(["git", "worktree", "remove"] + (["--force"] if p in stale else []) + [p], cwd=a.repo)
            except Exception as e:
                failed += 1
                print(f"FAILED {p}: {e}")
    print(f"summary: {len(remove)} removable, {len(keep)} kept, mode={'apply' if a.apply else 'dry-run'}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
