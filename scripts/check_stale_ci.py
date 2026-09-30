#!/usr/bin/env python3
"""Find open pull requests whose head commit has no test run, and start one.

THE BLIND SPOT THIS CLOSES. On 2026-09-30 a push of commit 8d83e60d to the
already-open branch `retire-item-147` produced no `pull_request` workflow run
at all, although earlier pushes to that same branch had. The consequence is
silent and expensive: the branch still carries the PREVIOUS commit's failure,
auto-merge therefore never fires, and nothing anywhere notices. Roughly six
finished changes sat stuck that way for hours.

Why it happens is not fully established and does not need to be for this
guard to work. `.github/workflows/test.yml` cancels superseded `pull_request`
runs (`cancel-in-progress` on PRs), and GitHub itself drops or coalesces run
creation under load and around rapid pushes; either way the observable state
is the same — a head commit with no run. This script tests the observable
state, not the cause.

WHAT IT DOES. For every open, non-draft PR targeting the default branch:
  1. Resolve the PR's head SHA.
  2. Ask for the `tests` workflow runs recorded against that exact SHA.
  3. Classify:
       ok       — a completed run exists (pass or fail; a real answer)
       running  — a queued or in-progress run exists; nothing to do
       stale    — no run at all, or only cancelled ones, i.e. no answer is
                  coming and none is on its way
  4. For each stale PR, dispatch `test.yml` on its head branch so the gap
     repairs itself, unless --no-dispatch is given.
  5. Print a one-line verdict per PR, and exit non-zero if any PR was stale.

WHY NON-ZERO MATTERS. The scheduled workflow that runs this goes red in the
Actions tab when it exits non-zero. That is the visible surface: it does not
depend on a Telegram message being delivered, and alerts on this desk are
deliberately muted. A cancelled run counts as stale on purpose — a cancelled
run is not an answer, and treating it as one would be weakening the check to
make a failure go away.

Usage:
    scripts/check_stale_ci.py
    scripts/check_stale_ci.py --no-dispatch     # report only
    scripts/check_stale_ci.py --repo owner/name

Exit codes:
    0  every open PR's head commit has a completed or in-flight test run
    1  at least one PR head commit had no test run
    2  the check could not run (no `gh`, no auth, API failure)
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys

WORKFLOW_FILE = "test.yml"
#: The `name:` of the workflow in .github/workflows/test.yml. Matched
#: case-insensitively so a rename of the display name alone does not silently
#: turn this guard into a no-op that reports everything healthy.
WORKFLOW_NAME = "tests"
GH_TIMEOUT_S = 60


class GhError(RuntimeError):
    """A `gh` invocation failed."""


def _gh(args: list[str]) -> str:
    try:
        proc = subprocess.run(
            ["gh", *args],
            capture_output=True, text=True, timeout=GH_TIMEOUT_S,
        )
    except FileNotFoundError as exc:  # pragma: no cover - environment
        raise GhError("the `gh` CLI is not installed") from exc
    except subprocess.TimeoutExpired as exc:
        raise GhError(f"`gh {' '.join(args)}` timed out") from exc
    if proc.returncode != 0:
        raise GhError(
            f"`gh {' '.join(args)}` failed: {proc.stderr.strip() or proc.returncode}"
        )
    return proc.stdout


def _gh_json(args: list[str]) -> object:
    raw = _gh(args)
    try:
        return json.loads(raw)
    except ValueError as exc:
        raise GhError(f"`gh {' '.join(args)}` returned non-JSON output") from exc


def open_pull_requests(repo: str | None) -> list[dict]:
    """Open, non-draft PRs. Drafts are excluded: nobody is waiting on them."""
    args = ["pr", "list", "--state", "open", "--limit", "100",
            "--json", "number,headRefName,headRefOid,isDraft,title"]
    if repo:
        args += ["--repo", repo]
    rows = _gh_json(args)
    if not isinstance(rows, list):
        raise GhError("unexpected `gh pr list` payload")
    return [row for row in rows if not row.get("isDraft")]


def runs_for_sha(sha: str, repo: str | None) -> list[dict]:
    args = ["run", "list", "--commit", sha, "--limit", "30",
            "--json", "workflowName,status,conclusion,databaseId,event"]
    if repo:
        args += ["--repo", repo]
    rows = _gh_json(args)
    if not isinstance(rows, list):
        raise GhError("unexpected `gh run list` payload")
    return [
        row for row in rows
        if str(row.get("workflowName", "")).strip().lower() == WORKFLOW_NAME
    ]


def classify(runs: list[dict]) -> str:
    """ok / running / stale, from the runs recorded against one commit."""
    for run in runs:
        if str(run.get("status")) in ("queued", "in_progress", "waiting", "pending",
                                      "requested"):
            return "running"
    for run in runs:
        if str(run.get("status")) == "completed" and run.get("conclusion") not in (
            None, "cancelled", "skipped", "stale",
        ):
            return "ok"
    return "stale"


def dispatch(branch: str, repo: str | None) -> str | None:
    """Start `test.yml` on a branch. Returns an error string, or None on success."""
    args = ["workflow", "run", WORKFLOW_FILE, "--ref", branch]
    if repo:
        args += ["--repo", repo]
    try:
        _gh(args)
    except GhError as exc:
        return str(exc)
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default=None,
                        help="owner/name; defaults to the checkout's remote")
    parser.add_argument("--no-dispatch", action="store_true",
                        help="report stale PRs without starting a run")
    args = parser.parse_args(argv)

    try:
        prs = open_pull_requests(args.repo)
    except GhError as exc:
        print(f"check_stale_ci: could not list pull requests — {exc}",
              file=sys.stderr)
        return 2

    stale: list[dict] = []
    for pr in sorted(prs, key=lambda row: row.get("number") or 0):
        sha = str(pr.get("headRefOid") or "")
        number = pr.get("number")
        branch = str(pr.get("headRefName") or "")
        if not sha or not branch:
            print(f"check_stale_ci: PR #{number} has no resolvable head — skipped",
                  file=sys.stderr)
            continue
        try:
            verdict = classify(runs_for_sha(sha, args.repo))
        except GhError as exc:
            print(f"check_stale_ci: PR #{number} run lookup failed — {exc}",
                  file=sys.stderr)
            return 2
        print(f"PR #{number} {sha[:10]} {branch}: {verdict}")
        if verdict == "stale":
            stale.append(pr)

    if not stale:
        print(f"check_stale_ci: all {len(prs)} open PR head commits have a "
              f"test run")
        return 0

    print("")
    print(f"check_stale_ci: {len(stale)} open PR(s) have NO test run on their "
          f"head commit — their last recorded result belongs to an older "
          f"commit and auto-merge cannot fire:")
    for pr in stale:
        print(f"  #{pr.get('number')}  {pr.get('headRefName')}  "
              f"{str(pr.get('title') or '')[:70]}")

    if args.no_dispatch:
        return 1

    for pr in stale:
        error = dispatch(str(pr.get("headRefName")), args.repo)
        if error:
            print(f"check_stale_ci: could not start a run for "
                  f"#{pr.get('number')} — {error}", file=sys.stderr)
        else:
            print(f"check_stale_ci: started a run for #{pr.get('number')} "
                  f"({pr.get('headRefName')})")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
