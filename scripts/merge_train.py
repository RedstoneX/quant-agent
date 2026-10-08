#!/usr/bin/env python3
"""Merge armed pull requests ONE AT A TIME, each re-tested on the current main.

WHY. Between 2026-09-29 and 2026-10-06, 672 changes merged and dozens more sat
red or colliding. Branch protection did not require a branch to be current
with main (`strict: false`), so each change was tested once, against whatever
main looked like when it was pushed. Every merge then made every other open
change's result obsolete: a fine change showed red against a main that no
longer existed, and two independently green changes could merge into a red
main. GitHub's own merge queue would fix this but is not offered on a
personal-account repository.

WHAT THIS DOES. It is that merge queue, built from two GitHub primitives.
With `strict: true`, auto-merge only fires on a branch that contains the
latest main and passed `pytest` on it. Each time this runs:

  1. List open, non-draft PRs that have auto-merge armed.
  2. If one is already current with main and its checks are still running,
     the train is BUSY: do nothing. Exactly one change is ever being
     re-tested, so a merge can never invalidate a test in flight.
  3. Otherwise take the OLDEST armed PR that is behind main, merge main into
     it (GitHub's update-branch), and start `tests` on it. A push made with
     the workflow token does not trigger workflows by itself, hence the
     explicit dispatch.
  4. A PR whose update conflicts is reported and skipped, not retried: a
     conflict needs a person or an agent, and the next PR should not wait
     behind it.

It runs on every push to main (a merge just happened: send the next), when a
PR's auto-merge is armed, and on a schedule as a backstop.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys

FAILED = {"FAILURE", "ERROR", "CANCELLED", "TIMED_OUT", "ACTION_REQUIRED", "STARTUP_FAILURE"}


def _checks_running(pr: dict) -> bool:
    for c in pr.get("statusCheckRollup") or []:
        status = (c.get("status") or "").upper()
        state = (c.get("state") or "").upper()
        if status and status != "COMPLETED":
            return True
        if state in {"PENDING", "EXPECTED"}:
            return True
    return False


def _checks_failed(pr: dict) -> bool:
    return any(((c.get("conclusion") or c.get("state") or "").upper() in FAILED)
               for c in pr.get("statusCheckRollup") or [])


def plan(prs: list[dict]) -> tuple[str, list[dict]]:
    """Decide what to do. Returns ("busy", [pr]) / ("advance", ordered candidates) / ("idle", [])."""
    armed = [p for p in prs if p.get("autoMergeRequest") and not p.get("isDraft")]
    for p in armed:
        if p.get("mergeStateStatus") != "BEHIND" and _checks_running(p):
            return "busy", [p]
    behind = sorted((p for p in armed if p.get("mergeStateStatus") == "BEHIND"),
                    key=lambda p: p["createdAt"])
    return ("advance", behind) if behind else ("idle", [])


def _gh(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["gh", *args], capture_output=True, text=True)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True)
    args = ap.parse_args(argv)

    out = _gh("pr", "list", "-R", args.repo, "--state", "open", "--base", "main", "--limit", "100",
              "--json", "number,createdAt,isDraft,autoMergeRequest,mergeStateStatus,headRefName,headRefOid,statusCheckRollup")
    if out.returncode:
        print(f"::error::cannot list pull requests: {out.stderr.strip()}")
        return 1
    action, prs = plan(json.loads(out.stdout))
    if action == "busy":
        print(f"train busy: #{prs[0]['number']} is being tested on current main")
        return 0
    if action == "idle":
        print("train idle: no armed pull request is behind main")
        return 0

    conflicts = []
    for p in prs:
        n = p["number"]
        upd = _gh("api", "-X", "PUT", f"repos/{args.repo}/pulls/{n}/update-branch",
                  "-f", f"expected_head_sha={p['headRefOid']}")
        if upd.returncode:
            conflicts.append(n)
            print(f"::warning::#{n} could not be brought up to date with main: {upd.stderr.strip()[:200]}")
            continue
        run = _gh("workflow", "run", "tests", "-R", args.repo, "--ref", p["headRefName"])
        if run.returncode:
            print(f"::error::#{n} updated but tests could not be started: {run.stderr.strip()[:200]}")
            return 1
        print(f"train advanced: #{n} merged with current main and sent to tests")
        return 0
    print(f"::error::every armed pull request behind main conflicts with it: {conflicts}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
