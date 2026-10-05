#!/usr/bin/env python3
"""Watch `main` itself: go red when the default branch's own test run failed.

THE BLIND SPOT THIS CLOSES. Branch protection requires the `pytest` check on
a pull request, but it does not require a branch to be up to date with `main`
before merging (`strict: false`), and nothing anywhere reads the run that
`main` produces AFTER a merge. So `main` can be red -- every shard's failure
plainly recorded in the Actions tab -- and no person is told. On 2026-10-01
`main` sat red on 13 cost-circuit tests while every open pull request showed
green, because each PR's own run had passed earlier against a different
clock. The thirteen tests were not the defect; nothing watching `main` was.

WHY THIS SHAPE. `scripts/check_stale_ci.py` already answers the neighbouring
question ("does every open PR head have a run?") and already established how
this desk raises an operational fault that does not depend on a message being
delivered: exit non-zero, which turns the scheduled workflow RED in the
Actions tab. Owner alerts are muted, so a Telegram page would reach nobody.
This script joins that family rather than inventing a second mechanism, and
it costs nothing: one `gh` read on a schedule, no paid provider, no
dependency beyond the `gh` CLI the runner already carries.

WHAT IT DOES.
  1. List the `tests` workflow runs recorded against the default branch.
  2. Keep only `push` runs that have COMPLETED. A queued or in-progress run
     is not an answer yet; a `pull_request` run is about a branch, not about
     `main`.
  3. The newest of those is `main`'s standing verdict.
  4. If that verdict is anything other than success, walk further back to
     find how long the branch has been that way, and report the whole streak.

WHAT IT DELIBERATELY DOES NOT DO. It never dispatches a re-run and never
retries: a red `main` is a real answer and re-running it would be asking the
same question until it gives a different reply. A cancelled newest run counts
as red for the same reason `check_stale_ci.py` counts one as stale -- it is
not a pass, and treating it as one would be weakening the watch to make a
failure disappear.

THE DURABLE RECORD. The verdict is printed in full and, when the runner
provides `GITHUB_STEP_SUMMARY`, written there as well, so the record survives
on its own page: the failing commit, its subject, the run that failed, and
when the branch first went red. Nothing in that record needs a Telegram
message, a dashboard, or a database to be legible.

Usage:
    scripts/check_main_red.py
    scripts/check_main_red.py --repo owner/name
    scripts/check_main_red.py --branch main

Exit codes:
    0  the default branch's newest completed push run succeeded
    1  it did not -- `main` is red and a person must look
    2  the check could not run (no `gh`, no auth, API failure, no run found)
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys

WORKFLOW_NAME = "tests"
GH_TIMEOUT_S = 60
#: How many runs to ask for. Only ever used to bound one API read; the
#: verdict itself is always the single newest completed push run, so this
#: number governs nothing but how far back the "how long has it been red"
#: streak can be traced.
RUN_LOOKBACK = 50


class GhError(RuntimeError):
    """A `gh` invocation failed."""


def _gh_json(args: list[str]) -> object:
    try:
        proc = subprocess.run(
            ["gh", *args], capture_output=True, text=True, timeout=GH_TIMEOUT_S,
        )
    except FileNotFoundError as exc:  # pragma: no cover - environment
        raise GhError("the `gh` CLI is not installed") from exc
    except subprocess.TimeoutExpired as exc:
        raise GhError(f"`gh {' '.join(args)}` timed out") from exc
    if proc.returncode != 0:
        raise GhError(
            f"`gh {' '.join(args)}` failed: "
            f"{proc.stderr.strip() or proc.returncode}"
        )
    try:
        return json.loads(proc.stdout)
    except ValueError as exc:
        raise GhError(f"`gh {' '.join(args)}` returned non-JSON output") from exc


def branch_runs(branch: str, repo: str | None) -> list[dict]:
    args = ["run", "list", "--branch", branch, "--limit", str(RUN_LOOKBACK),
            "--json", "workflowName,status,conclusion,databaseId,headSha,"
                      "createdAt,event,displayTitle,url"]
    if repo:
        args += ["--repo", repo]
    rows = _gh_json(args)
    if not isinstance(rows, list):
        raise GhError("unexpected `gh run list` payload")
    return rows


def settled_push_runs(runs: list[dict]) -> list[dict]:
    """The branch's own completed runs of the test workflow, newest first.

    `gh run list` already returns newest first. Matching the workflow name
    case-insensitively mirrors `check_stale_ci.py`: a rename of the display
    name alone must not quietly turn this watch into a no-op that reports
    everything healthy.
    """
    return [
        run for run in runs
        if str(run.get("workflowName", "")).strip().lower() == WORKFLOW_NAME
        and str(run.get("event", "")).strip() == "push"
        and str(run.get("status", "")).strip() == "completed"
    ]


def verdict(runs: list[dict]) -> str:
    """green / red / unknown, from the branch's completed push runs."""
    settled = settled_push_runs(runs)
    if not settled:
        return "unknown"
    return "green" if str(settled[0].get("conclusion")) == "success" else "red"


def red_streak(runs: list[dict]) -> list[dict]:
    """Every consecutive non-success run from the newest one backwards."""
    streak: list[dict] = []
    for run in settled_push_runs(runs):
        if str(run.get("conclusion")) == "success":
            break
        streak.append(run)
    return streak


def record_lines(branch: str, streak: list[dict]) -> list[str]:
    """The durable record, legible with nothing else to hand."""
    newest = streak[0]
    oldest = streak[-1]
    lines = [
        f"{branch} is RED: its newest completed push run of the "
        f"`{WORKFLOW_NAME}` workflow did not succeed.",
        f"  commit     : {newest.get('headSha')}",
        f"  subject    : {newest.get('displayTitle')}",
        f"  conclusion : {newest.get('conclusion')}",
        f"  run        : {newest.get('url') or newest.get('databaseId')}",
        f"  failed at  : {newest.get('createdAt')}",
    ]
    if len(streak) > 1:
        lines.append(
            f"  red since  : {oldest.get('createdAt')} "
            f"({len(streak)} consecutive non-passing push runs, oldest "
            f"commit {oldest.get('headSha')})"
        )
    lines.append(
        "  nothing re-runs this automatically: a red main is an answer, "
        "not a flake to retry."
    )
    return lines


def _write_summary(lines: list[str]) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write("## main is red\n\n```\n" + "\n".join(lines) + "\n```\n")
    except OSError as exc:  # pragma: no cover - runner filesystem
        print(f"check_main_red: could not write the run summary — {exc}",
              file=sys.stderr)


ISSUE_PREFIX = "[trunk-red]"


def _gh(args: list[str]) -> str:
    proc = subprocess.run(["gh", *args], capture_output=True, text=True,
                          timeout=GH_TIMEOUT_S)
    if proc.returncode != 0:
        raise GhError(proc.stderr.strip() or str(proc.returncode))
    return proc.stdout


def plan_issue_actions(open_issues: list[dict], red_sha: str | None) -> dict:
    """What to do with the tracking issues. red_sha None means main is green.

    ONE issue for the whole red streak, however many pushes it spans: reuse
    the open tracking issue, and when a newer commit is also red, add a
    comment to it instead of opening another. Close only when main is green.
    """
    ours = sorted(
        (i for i in open_issues
         if str(i.get("title", "")).startswith(ISSUE_PREFIX)),
        key=lambda i: i["number"])
    if not red_sha:
        return {"create": False, "close": [i["number"] for i in ours],
                "comment": None}
    if not ours:
        return {"create": True, "close": [], "comment": None}
    keep, extra = ours[0], ours[1:]
    named = red_sha[:8] in keep["title"]
    return {"create": False, "close": [i["number"] for i in extra],
            "comment": None if named else keep["number"]}


def sync_issue(repo: str | None, red: list[str] | None,
               red_sha: str | None) -> bool:
    """Open one issue per red streak; close it once main is green.

    Returns False when the issue could not be synced (for example issues are
    disabled on the repository, which GitHub reports as an error). The caller
    turns that into a visible ::warning:: and a run-summary line, so a dead
    issue path is never silent.
    """
    base = ["--repo", repo] if repo else []
    try:
        listed = json.loads(_gh(["issue", "list", *base, "--state", "open",
                                 "--limit", "50", "--json", "number,title"]))
        todo = plan_issue_actions(listed, red_sha)
        if todo["create"]:
            _gh(["issue", "create", *base, "--title",
                 f"{ISSUE_PREFIX} main is red at {red_sha[:8]}",
                 "--body", "\n".join(red or [])])
        if todo["comment"]:
            _gh(["issue", "comment", str(todo["comment"]), *base, "--body",
                 f"Still red at {red_sha[:8]}.\n\n" + "\n".join(red or [])])
        for number in todo["close"]:
            _gh(["issue", "close", str(number), *base, "--comment",
                 "main is green again." if not red_sha
                 else "Duplicate tracking issue; one covers the streak."])
    except (GhError, ValueError, subprocess.TimeoutExpired) as exc:
        print(f"::warning::check_main_red could not sync the tracking issue "
              f"-- {exc}", file=sys.stderr)
        return False
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default=None, help="owner/name")
    parser.add_argument("--branch", default="main",
                        help="the protected branch to watch")
    parser.add_argument("--issue", action="store_true",
                        help="open/close the tracking issue (needs issues: write)")
    args = parser.parse_args(argv)

    try:
        runs = branch_runs(args.branch, args.repo)
    except GhError as exc:
        print(f"check_main_red: could not read the branch's runs — {exc}",
              file=sys.stderr)
        return 2

    state = verdict(runs)
    if state == "unknown":
        print(
            f"check_main_red: no completed push run of the `{WORKFLOW_NAME}` "
            f"workflow found on {args.branch} — this watch cannot vouch for "
            f"the branch either way.",
            file=sys.stderr,
        )
        return 2
    if state == "green":
        print(f"check_main_red: {args.branch}'s newest completed push run "
              f"passed.")
        if args.issue:
            sync_issue(args.repo, None, None)
        return 0

    streak = red_streak(runs)
    lines = record_lines(args.branch, streak)
    if args.issue and not sync_issue(args.repo, lines,
                                     str(streak[0].get("headSha"))):
        lines.append("  tracking issue: COULD NOT BE OPENED (see the "
                     "warning); this run's red status is the only alarm.")
    for line in lines:
        print(line)
    _write_summary(lines)
    print(f"::error::{args.branch} is red and nothing else is watching it")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
