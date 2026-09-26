#!/usr/bin/env python3
"""Detect and safely clean up stale session-scratch git worktrees.

Deterministic. Read-only by default (reports; `--prune` acts). No LLM call,
no daemon, no new alert path, no trade-path impact — this is housekeeping.

THE PROBLEM THIS CLOSES (board item 139). Every agent session that runs off
this repo registers a git worktree under a scratch prefix (`/tmp/.../wt/<name>`
or `<repo>/.claude/worktrees/<name>`). Nothing ever de-registers them, so the
registry grows without bound: ~90 registered worktrees, ~74 of them session
scratch, filed 2026-09-18. The cost is disk and an unreadable `git worktree
list`, not dangling refs — on the filing date none was stale in git's sense
because every path still existed. But finished sessions DELETE their scratch
directory without running `git worktree remove`, which leaves the admin entry
behind pointing at a path that is now gone. This script is the sweep that
git's own `prune` half-does, plus the flagging git will not do on its own.

WHAT COUNTS AS SESSION SCRATCH — and nothing else is ever touched:
  A registered worktree whose path lies under one of the scratch prefixes
  (default: the system temp dir, and `<repo>/.claude/worktrees`). The main
  checkout and any worktree outside those prefixes (e.g. a hand-made review
  checkout somewhere in $HOME) are classified `kept` and never acted on.

  `git worktree list` only ever returns worktrees registered to THIS repo,
  so the sweep is inherently scoped to our own worktrees — it can never see,
  let alone remove, another user's or another tenant's worktree on this
  shared box. As belt-and-suspenders, a scratch path that still exists but is
  owned by a different uid is refused (classified `kept`, reason recorded).

THREE BUCKETS:
  gone   — a scratch worktree whose path no longer exists on disk. The
           session removed its directory but not its registration. `git
           worktree prune` removes exactly these admin entries and nothing
           else; it never touches a path that still exists. ALWAYS safe.
  stale  — a scratch worktree whose path still exists, whose branch is fully
           merged into the base ref (default `origin/main`), whose working
           tree is clean (no modified or untracked files), which is not
           locked, is owned by us, and whose directory is older than the
           age floor. A finished session that left everything behind.
  kept   — everything else: the main worktree, non-scratch paths, locked
           worktrees, dirty or unmerged or too-recent scratch, and anything
           owned by another uid. Each carries the reason it was kept.

PRUNING IS CONSERVATIVE. `--prune` runs `git worktree prune` (clears the
`gone` bucket) and then `git worktree remove` — WITHOUT `--force` — on each
`stale` worktree. `remove` without `--force` is itself a second safety gate:
git refuses to remove a worktree with a dirty or non-merged state, so a race
that dirtied a worktree between classification and removal is caught by git,
not by us. A locked worktree is never removed and never unlocked.

Usage:
    scripts/prune_worktrees.py                 # report only, exit 1 if any found
    scripts/prune_worktrees.py --prune         # actually clean up
    scripts/prune_worktrees.py --json          # machine-readable report
    scripts/prune_worktrees.py --min-age-days 3
    scripts/prune_worktrees.py --base-ref origin/main
    scripts/prune_worktrees.py --repo /path/to/repo

Exit codes:
    0  nothing to clean (report), or --prune left nothing behind
    1  stale/gone scratch worktrees found (report), or some survived --prune
    3  could not run (not a git repo, git error)
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Worktree:
    """One entry from `git worktree list --porcelain`."""

    path: str
    head: str | None = None
    branch: str | None = None
    bare: bool = False
    detached: bool = False
    locked: bool = False
    # `prunable` is git's own verdict that the path is gone; we also check the
    # filesystem ourselves so the classifier is testable without a real git.
    prunable: bool = False


@dataclass
class Classified:
    gone: list[tuple[Worktree, str]] = field(default_factory=list)
    stale: list[tuple[Worktree, str]] = field(default_factory=list)
    kept: list[tuple[Worktree, str]] = field(default_factory=list)


def parse_porcelain(text: str) -> list[Worktree]:
    """Parse `git worktree list --porcelain` into `Worktree` records.

    Records are separated by a blank line; the first line of each is
    `worktree <path>`. Absolute paths, so a path is never split on spaces.
    """
    entries: list[Worktree] = []
    current: Worktree | None = None
    for raw in text.splitlines():
        line = raw.rstrip("\n")
        if not line:
            if current is not None:
                entries.append(current)
                current = None
            continue
        if line.startswith("worktree "):
            current = Worktree(path=line[len("worktree ") :])
        elif current is None:
            continue
        elif line.startswith("HEAD "):
            current.head = line[len("HEAD ") :]
        elif line.startswith("branch "):
            current.branch = line[len("branch ") :]
        elif line == "bare":
            current.bare = True
        elif line == "detached":
            current.detached = True
        elif line == "locked" or line.startswith("locked "):
            current.locked = True
        elif line == "prunable" or line.startswith("prunable "):
            current.prunable = True
    if current is not None:
        entries.append(current)
    return entries


def _norm(p: str) -> str:
    return os.path.normpath(os.path.abspath(p))


def default_scratch_prefixes(repo_root: str) -> list[str]:
    """The two prefixes session scratch actually lives under here."""
    return [
        _norm(tempfile.gettempdir()),
        _norm(os.path.join(repo_root, ".claude", "worktrees")),
    ]


def _under_prefix(path: str, prefixes: list[str]) -> bool:
    npath = _norm(path)
    for pref in prefixes:
        if npath == pref or npath.startswith(pref + os.sep):
            return True
    return False


def classify(
    entries: list[Worktree],
    *,
    repo_root: str,
    scratch_prefixes: list[str],
    min_age_days: float,
    now: float,
    our_uid: int,
    is_merged,
    is_clean,
    path_exists=os.path.exists,
    path_stat=os.stat,
) -> Classified:
    """Pure classifier. All git/fs access is injected so the test can drive it.

    `is_merged(wt)` -> bool, `is_clean(wt)` -> bool are called only for a
    scratch worktree whose path still exists (never for a gone path).
    """
    result = Classified()
    repo_norm = _norm(repo_root)
    age_floor = min_age_days * 86400.0

    for wt in entries:
        if wt.bare:
            result.kept.append((wt, "bare repository"))
            continue
        if _norm(wt.path) == repo_norm:
            result.kept.append((wt, "main worktree"))
            continue
        if not _under_prefix(wt.path, scratch_prefixes):
            result.kept.append((wt, "not under a scratch prefix"))
            continue

        exists = path_exists(wt.path)
        if not exists or wt.prunable:
            result.gone.append((wt, "path no longer exists (git-prunable)"))
            continue

        # Path exists and is scratch. Now the conservative gates.
        if wt.locked:
            result.kept.append((wt, "locked"))
            continue
        try:
            st = path_stat(wt.path)
            if st.st_uid != our_uid:
                result.kept.append((wt, f"owned by uid {st.st_uid}, not us"))
                continue
            age = now - st.st_mtime
        except OSError as exc:  # pragma: no cover - defensive
            result.kept.append((wt, f"could not stat ({exc})"))
            continue

        if age < age_floor:
            result.kept.append(
                (wt, f"too recent ({age / 86400.0:.1f}d < {min_age_days}d)")
            )
            continue
        if not is_clean(wt):
            result.kept.append((wt, "working tree not clean"))
            continue
        if not is_merged(wt):
            result.kept.append((wt, "branch not merged into base"))
            continue

        result.stale.append(
            (wt, f"merged, clean, idle {age / 86400.0:.1f}d")
        )

    return result


# ---------------------------------------------------------------------------
# git interaction (not exercised by the pure-classifier unit test; covered by
# the real-git integration test)
# ---------------------------------------------------------------------------

def _git(args: list[str], cwd: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", cwd, *args],
        check=check,
        capture_output=True,
        text=True,
    )


def list_worktrees(repo_root: str) -> list[Worktree]:
    out = _git(["worktree", "list", "--porcelain"], repo_root).stdout
    return parse_porcelain(out)


def make_is_merged(repo_root: str, base_ref: str):
    def is_merged(wt: Worktree) -> bool:
        if not wt.head:
            return False
        # HEAD is an ancestor of base_ref  <=>  fully merged.
        cp = _git(
            ["merge-base", "--is-ancestor", wt.head, base_ref],
            repo_root,
            check=False,
        )
        return cp.returncode == 0

    return is_merged


def make_is_clean(_repo_root: str):
    def is_clean(wt: Worktree) -> bool:
        cp = _git(["status", "--porcelain"], wt.path, check=False)
        if cp.returncode != 0:
            return False  # can't confirm clean -> treat as not clean (keep)
        return cp.stdout.strip() == ""

    return is_clean


def do_prune(repo_root: str, classified: Classified) -> list[tuple[Worktree, str]]:
    """Clear `gone` via `git worktree prune`, then `remove` each `stale`.

    Returns the list of (worktree, error) that could NOT be removed.
    """
    failures: list[tuple[Worktree, str]] = []
    # `prune` clears every admin entry whose path is gone. It never touches a
    # path that still exists, so it can only affect the `gone` bucket.
    _git(["worktree", "prune", "-v"], repo_root, check=False)

    for wt, _reason in classified.stale:
        # No --force: git refuses a dirty/unmerged worktree, a final gate
        # against a race between classification and removal.
        cp = _git(["worktree", "remove", wt.path], repo_root, check=False)
        if cp.returncode != 0:
            failures.append((wt, cp.stderr.strip() or "git worktree remove failed"))
    return failures


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _describe(wt: Worktree) -> str:
    name = os.path.basename(wt.path.rstrip("/")) or wt.path
    branch = wt.branch.replace("refs/heads/", "") if wt.branch else "(detached)"
    return f"{name} [{branch}]"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo",
        default=str(Path(__file__).resolve().parent.parent),
        help="Repository root to sweep (default: this repo).",
    )
    parser.add_argument(
        "--base-ref",
        default="origin/main",
        help="A scratch branch fully merged into this ref is removable (default: origin/main).",
    )
    parser.add_argument(
        "--min-age-days",
        type=float,
        default=7.0,
        help="Only a scratch worktree idle at least this long is stale (default: 7).",
    )
    parser.add_argument(
        "--scratch-prefix",
        action="append",
        default=None,
        help="Override the scratch prefixes (repeatable). Default: temp dir + <repo>/.claude/worktrees.",
    )
    parser.add_argument("--prune", action="store_true", help="Actually clean up (default: report only).")
    parser.add_argument("--json", action="store_true", help="Machine-readable output.")
    args = parser.parse_args(argv)

    repo_root = _norm(args.repo)
    try:
        entries = list_worktrees(repo_root)
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        print(f"prune_worktrees: cannot list worktrees for {repo_root}: {exc}", file=sys.stderr)
        return 3

    prefixes = (
        [_norm(p) for p in args.scratch_prefix]
        if args.scratch_prefix
        else default_scratch_prefixes(repo_root)
    )

    classified = classify(
        entries,
        repo_root=repo_root,
        scratch_prefixes=prefixes,
        min_age_days=args.min_age_days,
        now=time.time(),
        our_uid=os.getuid(),
        is_merged=make_is_merged(repo_root, args.base_ref),
        is_clean=make_is_clean(repo_root),
    )

    failures: list[tuple[Worktree, str]] = []
    if args.prune:
        failures = do_prune(repo_root, classified)

    if args.json:
        payload = {
            "repo": repo_root,
            "base_ref": args.base_ref,
            "min_age_days": args.min_age_days,
            "scratch_prefixes": prefixes,
            "gone": [{"path": w.path, "reason": r} for w, r in classified.gone],
            "stale": [{"path": w.path, "branch": w.branch, "reason": r} for w, r in classified.stale],
            "kept_count": len(classified.kept),
            "pruned": args.prune,
            "failures": [{"path": w.path, "error": e} for w, e in failures],
        }
        print(json.dumps(payload, indent=2))
    else:
        total = len(classified.gone) + len(classified.stale)
        print(
            f"{len(entries)} registered worktrees: "
            f"{len(classified.gone)} gone, {len(classified.stale)} stale, "
            f"{len(classified.kept)} kept."
        )
        for wt, reason in classified.gone:
            print(f"  GONE  {_describe(wt)} — {reason}")
        for wt, reason in classified.stale:
            print(f"  STALE {_describe(wt)} — {reason}")
        if args.prune:
            removed = len(classified.stale) - len(failures)
            print(f"pruned {len(classified.gone)} gone + removed {removed} stale worktree(s).")
            for wt, err in failures:
                print(f"  KEPT (removal failed) {_describe(wt)} — {err}", file=sys.stderr)
        elif total:
            print("run with --prune to clean these up.")

    if args.prune:
        # Success unless something we meant to remove survived.
        return 1 if failures else 0
    return 1 if (classified.gone or classified.stale) else 0


if __name__ == "__main__":
    raise SystemExit(main())
