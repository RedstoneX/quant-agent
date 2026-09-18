#!/usr/bin/env python3
"""Is the commit that CLOSED a board item actually running in production?

Deterministic and read-only. No LLM call, no daemon, no new alert path —
it reuses the notifier `scripts/check_deploy_drift.py` already uses.

WHY THIS IS NOT ONE OF THE TWO CHECKS ALREADY ON THE BOX
--------------------------------------------------------
`scripts/check_deploy_drift.py` answers "is the deployed checkout behind
`origin/main`?" — one commit against one commit. `scripts/check_unit_drift.py`
answers "do the installed systemd units still match the deployed checkout?"
— bytes against bytes. Both are whole-box questions and both are silent on
the per-item one:

    item 71 is struck through on the board. Is the commit that struck it
    in the checkout that is trading right now?

A box sitting exactly on `origin/main` passes both existing checks and can
still have been reported as "item N deployed" hours before the deploy that
carried it. This script is that comparison, and it is deliberately
disjoint from the other two so one condition never alarms twice.

HOW IT DECIDES, WITHOUT TRUSTING ANY PROSE
------------------------------------------
`docs/WORK.md` carries one line, "Retired item numbers — never reuse.",
and that line is only ever edited to retire an item. So:

  1. walk the history of `docs/WORK.md` on `origin/main`, newest first;
  2. for each commit, diff the set of numbers on that line against its
     first parent's. A number that appears is an item that commit retired.
     That gives number -> retiring commit with no board prose involved;
  3. for each retiring commit, ask git whether it is an ancestor of the
     production checkout's HEAD.

Step 3 is the whole point and it is not falsifiable by anything written
down: `git merge-base --is-ancestor` either holds or it does not.

WHAT IT CANNOT TELL YOU
-----------------------
  * That the fix WORKS. Presence in the checkout is presence, not
    validation. The acceptance observable named at closure is the thing a
    human confirms after a real session; this only proves the code the
    observable refers to is the code that is running.
  * Anything about an item closed without touching the retired line.
  * Anything when the box cannot reach GitHub. A fetch failure degrades to
    exit 0 and a stderr note, the same way `check_deploy_drift.py` does —
    a box that cannot look is not evidence that it is behind.
  * Whether the RUNNING PROCESS was restarted onto the checkout. That is
    `check_unit_drift.py`'s neighbour problem and is not duplicated here.

Usage
-----
    scripts/check_item_deployment.py
    scripts/check_item_deployment.py --deployed-path /home/qamc/quant-agent
    scripts/check_item_deployment.py --no-telegram
    scripts/check_item_deployment.py --no-fetch        # existing refs only

Exit codes
----------
    0  every retired item's closing commit is in the deployed checkout, or
       the check could not run (fetch/network failure)
    1  at least one item is reported closed but its closing commit is not
       deployed
    3  the deployed checkout could not be read at all — an operator
       problem, not a finding
"""
from __future__ import annotations

import argparse

import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.definition_of_done import (  # noqa: E402
    WORK_MD,
    retired_numbers,
)

DEFAULT_DEPLOYED_PATH = "/home/qamc/quant-agent"

#: How far back down `docs/WORK.md`'s history to look. The board's own
#: numbering means an item retired long enough ago that it falls outside
#: this window is deployed many times over; the bound exists so a periodic
#: check on the box has a fixed cost rather than one that grows forever.
DEFAULT_MAX_COMMITS = 200


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, text=True, check=False)


def retiring_commits(repo: Path, ref: str = "origin/main",
                     max_commits: int = DEFAULT_MAX_COMMITS) -> dict[str, str]:
    """Item number -> the commit on `ref` that added it to the retired line.

    Newest-first, so the FIRST commit seen to add a number wins; a number
    re-added after a revert resolves to the re-add, which is the deploy
    that matters.
    """
    log = _git(repo, "log", "--format=%H", f"-n{max_commits}", ref,
               "--", WORK_MD)
    if log.returncode != 0:
        return {}
    found: dict[str, str] = {}
    for sha in [l for l in log.stdout.split("\n") if l]:
        after = _git(repo, "show", f"{sha}:{WORK_MD}")
        if after.returncode != 0:
            continue
        before = _git(repo, "show", f"{sha}^:{WORK_MD}")
        added = retired_numbers(after.stdout) - (
            retired_numbers(before.stdout) if before.returncode == 0 else set())
        for number in added:
            found.setdefault(number, sha)
    return found


@dataclass
class Finding:
    item: str
    commit: str
    subject: str


def undeployed_closures(repo: Path, head: str, ref: str = "origin/main",
                        max_commits: int = DEFAULT_MAX_COMMITS) -> list[Finding]:
    out: list[Finding] = []
    for item, sha in sorted(retiring_commits(repo, ref, max_commits).items(),
                            key=lambda kv: int(kv[0])):
        reachable = _git(repo, "merge-base", "--is-ancestor", sha, head)
        if reachable.returncode == 0:
            continue
        subject = _git(repo, "log", "-1", "--format=%s", sha).stdout.strip()
        out.append(Finding(item=item, commit=sha[:8], subject=subject))
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--deployed-path", default=DEFAULT_DEPLOYED_PATH)
    parser.add_argument("--ref", default="origin/main")
    parser.add_argument("--max-commits", type=int, default=DEFAULT_MAX_COMMITS)
    parser.add_argument("--no-fetch", action="store_true")
    parser.add_argument("--no-telegram", action="store_true")
    args = parser.parse_args(argv)

    repo = Path(args.deployed_path)
    head = _git(repo, "rev-parse", "HEAD")
    if head.returncode != 0:
        print(f"cannot read HEAD of {repo}: {head.stderr.strip()}",
              file=sys.stderr)
        return 3
    if not args.no_fetch and _git(repo, "fetch", "-q", "origin",
                                  "main").returncode != 0:
        print("could not fetch origin/main; not reporting drift from a read "
              "that failed", file=sys.stderr)
        return 0

    findings = undeployed_closures(repo, head.stdout.strip(), args.ref,
                                   args.max_commits)
    if not findings:
        print(f"every retired board item's closing commit is in {repo} "
              f"(HEAD {head.stdout.strip()[:8]})")
        return 0

    lines = [f"item {f.item}: {f.commit} {f.subject}" for f in findings]
    message = ("Board items reported closed but NOT in the production "
               f"checkout ({len(findings)}):\n" + "\n".join(lines))
    print(message)
    if not args.no_telegram:
        try:
            from src.notifier import TelegramNotifier
            TelegramNotifier().send(message)
        except Exception as exc:  # noqa: BLE001 - a push failure is not a verdict
            print(f"could not send Telegram alert: {exc}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
