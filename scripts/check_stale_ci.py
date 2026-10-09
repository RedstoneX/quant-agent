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

SECOND RULE — OBSOLETE RED VERDICTS. The guards compare a branch with
`origin/main`, and main moves constantly (85 merges on 2026-10-04, one every
~17 minutes). A PR's recorded result is a comparison against a main that no
longer exists, and nothing re-runs it: PR 1250 failed at 12:40 UTC on a
baseline entry that main removed at 13:08, and sat red for four and a half
hours. A verdict computed against a superseded trunk is UNKNOWN, not FAILED,
and an unknown must be re-asked rather than left sitting as a red.

WHY THE FIRST VERSION OF THIS RULE WAS UNAFFORDABLE, measured 2026-10-04
19:27 UTC against the live repository: main moves faster (~17 min) than this
sweep runs (30 min), so "the newest run started before main's tip" was true
for ALL 26 open PRs on every single sweep, green ones included. The rule was
a re-run-everything loop wearing a condition: 10 dispatches per sweep x 48
sweeps = 480 runs a day, forever, and a permanently-broken PR was re-asked
48 times a day. Two things make it meaningful instead:

  * ONLY A RED REQUIRED CHECK IS RE-ASKED. A green verdict against an older
    trunk already unblocks the merge — branch protection here is
    deliberately non-strict — so re-running it buys nothing and spends the
    cap that a genuinely blocked change needs. Of the 26 open PRs measured,
    25 were red on `pytest` and 1 was green; the green one needed nothing.
  * A HEAD COMMIT IS RE-ASKED AT MOST MAX_RERUNS_PER_HEAD TIMES. After that
    the red is a real failure, not a stale one, and the sweep says so and
    stops spending runners. This does not loosen anything: the check stays
    red, stays required, and the change stays blocked.

  Nothing is stored. The re-run budget already spent on a head commit is
  read back off the `workflow_dispatch` runs recorded against that exact
  SHA, so a new push (a new SHA) starts with a full budget.
  Cap: MAX_OBSOLETE_DISPATCH per sweep, oldest verdict first.
  Obsolete PRs never make the sweep exit non-zero (that would start the
  job red on arrival); only the no-run-at-all rule does.

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
from datetime import datetime

WORKFLOW_FILE = "test.yml"
#: The `name:` of the workflow in .github/workflows/test.yml. Matched
#: case-insensitively so a rename of the display name alone does not silently
#: turn this guard into a no-op that reports everything healthy.
WORKFLOW_NAME = "tests"
GH_TIMEOUT_S = 60
#: Obsolete re-runs started per sweep. GitHub's documented limit is 20
#: concurrent jobs on the free plan (the plan is not known from here), and
#: ordinary push-triggered runs share it, so half of it (10) is the most this
#: sweep may take. 24 stale PRs then clear in three sweeps (~1.5h) instead
#: of one burst that starves real pushes. Main moves ~3.5 times an hour, so
#: an uncapped sweep would otherwise queue a full set of runs per merge.
MAX_OBSOLETE_DISPATCH = 10
#: The status check branch protection requires on this repository. Only a
#: PR whose required check is red is worth re-asking; a green one already
#: merges. Protection is non-strict, so a green computed against an older
#: trunk is a valid answer and is deliberately left alone.
REQUIRED_CHECK = "pytest"
#: How many times one head commit may be re-asked before its red is taken as
#: a real failure rather than a superseded one. Three gives a change three
#: separate trunks to be fixed by, and bounds the cost: ~85 merges a day
#: produce ~85 new head commits, so even if every one landed red the ceiling
#: is 255 re-runs a day, and in practice only the red minority qualifies.
MAX_RERUNS_PER_HEAD = 3


class GhError(RuntimeError):
    """A `gh` invocation failed."""


def _gh(args: list[str]) -> str:
    try:
        proc = subprocess.run(
            ["gh", *args],
            capture_output=True,
            text=True,
            timeout=GH_TIMEOUT_S,
        )
    except FileNotFoundError as exc:  # pragma: no cover - environment
        raise GhError("the `gh` CLI is not installed") from exc
    except subprocess.TimeoutExpired as exc:
        raise GhError(f"`gh {' '.join(args)}` timed out") from exc
    if proc.returncode != 0:
        raise GhError(f"`gh {' '.join(args)}` failed: {proc.stderr.strip() or proc.returncode}")
    return proc.stdout


def _gh_json(args: list[str]) -> object:
    raw = _gh(args)
    try:
        return json.loads(raw)
    except ValueError as exc:
        raise GhError(f"`gh {' '.join(args)}` returned non-JSON output") from exc


def open_pull_requests(repo: str | None) -> list[dict]:
    """Open, non-draft PRs. Drafts are excluded: nobody is waiting on them."""
    args = [
        "pr",
        "list",
        "--state",
        "open",
        "--limit",
        "100",
        "--json",
        "number,headRefName,headRefOid,isDraft,title,mergeStateStatus,statusCheckRollup",
    ]
    if repo:
        args += ["--repo", repo]
    rows = _gh_json(args)
    if not isinstance(rows, list):
        raise GhError("unexpected `gh pr list` payload")
    return [row for row in rows if not row.get("isDraft")]


def runs_for_sha(sha: str, repo: str | None) -> list[dict]:
    args = [
        "run",
        "list",
        "--commit",
        sha,
        "--limit",
        "30",
        "--json",
        "workflowName,status,conclusion,databaseId,event,createdAt,startedAt",
    ]
    if repo:
        args += ["--repo", repo]
    rows = _gh_json(args)
    if not isinstance(rows, list):
        raise GhError("unexpected `gh run list` payload")
    return [row for row in rows if str(row.get("workflowName", "")).strip().lower() == WORKFLOW_NAME]


def classify(runs: list[dict]) -> str:
    """ok / running / stale, from the runs recorded against one commit."""
    for run in runs:
        if str(run.get("status")) in ("queued", "in_progress", "waiting", "pending", "requested"):
            return "running"
    for run in runs:
        if str(run.get("status")) == "completed" and run.get("conclusion") not in (
            None,
            "cancelled",
            "skipped",
            "stale",
        ):
            return "ok"
    return "stale"


def _ts(value: object) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def main_tip_time(repo: str | None, branch: str = "main") -> datetime | None:
    """When the current tip of the default branch was committed."""
    target = f"repos/{repo or '{owner}/{repo}'}/commits/{branch}"
    data = _gh_json(["api", target])
    stamp = ((data or {}).get("commit") or {}).get("committer", {}).get("date")
    return _ts(stamp)


def is_obsolete(runs: list[dict], tip: datetime | None) -> bool:
    """True when the newest completed run began before main's tip existed.

    Only meaningful once classify() said "ok"; a PR with an in-flight run or
    no run is handled by the other rule. A cancelled run is not a verdict.
    """
    if tip is None:
        return False
    started = [
        _ts(r.get("startedAt") or r.get("createdAt"))
        for r in runs
        if str(r.get("status")) == "completed" and r.get("conclusion") not in (None, "cancelled", "skipped", "stale")
    ]
    started = [t for t in started if t is not None]
    return bool(started) and max(started) < tip


def required_check_is_red(pr: dict) -> bool:
    """True when the check branch protection requires is a recorded failure.

    A rollup entry is a check run (`name`/`conclusion`) or a commit status
    (`context`/`state`); both spellings are read. An entry that is missing,
    still running, or neutral is not a red — only a settled negative is.
    """
    negative = {"FAILURE", "TIMED_OUT", "ACTION_REQUIRED", "STARTUP_FAILURE", "ERROR", "CANCELLED"}
    for entry in pr.get("statusCheckRollup") or []:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name") or entry.get("context") or "").strip()
        if name.lower() != REQUIRED_CHECK.lower():
            continue
        verdict = str(entry.get("conclusion") or entry.get("state") or "").upper()
        if verdict in negative:
            return True
    return False


def reruns_already_spent(runs: list[dict]) -> int:
    """How many re-runs this sweep has already started on this head commit.

    Read back off the runs themselves — this sweep dispatches by
    `workflow_dispatch`, pushes arrive as `pull_request` — so nothing has to
    be stored and a new push resets the budget by having a new SHA.
    """
    return sum(1 for run in runs if str(run.get("event")) == "workflow_dispatch")


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
    parser.add_argument("--repo", default=None, help="owner/name; defaults to the checkout's remote")
    parser.add_argument("--no-dispatch", action="store_true", help="report stale PRs without starting a run")
    args = parser.parse_args(argv)

    try:
        prs = open_pull_requests(args.repo)
    except GhError as exc:
        print(f"check_stale_ci: could not list pull requests — {exc}", file=sys.stderr)
        return 2

    try:
        tip = main_tip_time(args.repo)
    except GhError as exc:
        print(f"check_stale_ci: main tip lookup failed, obsolete rule skipped — {exc}", file=sys.stderr)
        tip = None

    stale: list[dict] = []
    obsolete: list[tuple[datetime, dict]] = []
    for pr in sorted(prs, key=lambda row: row.get("number") or 0):
        sha = str(pr.get("headRefOid") or "")
        number = pr.get("number")
        branch = str(pr.get("headRefName") or "")
        if not sha or not branch:
            print(f"check_stale_ci: PR #{number} has no resolvable head — skipped", file=sys.stderr)
            continue
        try:
            runs = runs_for_sha(sha, args.repo)
            verdict = classify(runs)
        except GhError as exc:
            print(f"check_stale_ci: PR #{number} run lookup failed — {exc}", file=sys.stderr)
            return 2
        if (
            verdict == "ok"
            and is_obsolete(runs, tip)
            and required_check_is_red(pr)
            and pr.get("mergeStateStatus") != "DIRTY"
        ):
            if reruns_already_spent(runs) >= MAX_RERUNS_PER_HEAD:
                verdict = "red-for-real"
            else:
                verdict = "obsolete"
            when = min(
                _ts(r.get("startedAt") or r.get("createdAt")) or tip for r in runs if r.get("status") == "completed"
            )
            if verdict == "obsolete":
                obsolete.append((when, pr))
        print(f"PR #{number} {sha[:10]} {branch}: {verdict}")
        if verdict == "stale":
            stale.append(pr)

    if obsolete and not args.no_dispatch:
        obsolete.sort(key=lambda item: item[0])
        batch = obsolete[:MAX_OBSOLETE_DISPATCH]
        for _, pr in batch:
            error = dispatch(str(pr.get("headRefName")), args.repo)
            print(
                f"check_stale_ci: obsolete verdict on #{pr.get('number')} — "
                + (f"could not re-run: {error}" if error else "re-run started")
            )
        if len(obsolete) > len(batch):
            print(
                f"check_stale_ci: {len(obsolete) - len(batch)} more obsolete "
                f"PR(s) wait for the next sweep (cap {MAX_OBSOLETE_DISPATCH})"
            )
    elif obsolete:
        print(f"check_stale_ci: {len(obsolete)} PR(s) hold an obsolete verdict")

    if not stale:
        print(f"check_stale_ci: all {len(prs)} open PR head commits have a test run")
        return 0

    print("")
    print(
        f"check_stale_ci: {len(stale)} open PR(s) have NO test run on their "
        f"head commit — their last recorded result belongs to an older "
        f"commit and auto-merge cannot fire:"
    )
    for pr in stale:
        print(f"  #{pr.get('number')}  {pr.get('headRefName')}  {str(pr.get('title') or '')[:70]}")

    if args.no_dispatch:
        return 1

    for pr in stale:
        error = dispatch(str(pr.get("headRefName")), args.repo)
        if error:
            print(f"check_stale_ci: could not start a run for #{pr.get('number')} — {error}", file=sys.stderr)
        else:
            print(f"check_stale_ci: started a run for #{pr.get('number')} ({pr.get('headRefName')})")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
