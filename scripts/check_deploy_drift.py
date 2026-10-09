#!/usr/bin/env python3
"""Detect a deployed checkout that is behind what was merged.

Deterministic and read-only. No LLM call, no daemon, no new alert path.

The problem this closes: QAMC is deployed by hand to a detached-HEAD git
checkout (`/home/qamc/quant-agent`). A merge to `origin/main` can be
recorded as "deployed" without the box ever being `git checkout`'d onto
it. This script compares RUNNING vs MERGED.

What it does:
  1. `git fetch origin main` in the deployed checkout (remote-tracking
     ref only; never touches the working tree or HEAD).
  2. Compares the checkout's HEAD against the fetched `origin/main`.
  3. If behind, prints the count and subject of every missing commit,
     sends one Telegram alert via the existing `TelegramNotifier`, and
     exits non-zero.
  4. If in sync, prints one line and exits 0. No Telegram push.

Expected, non-alarming states (must NOT be reported as drift):
  - Detached HEAD. That's how deploys work here; this script never
    compares branch names, only commit reachability.
  - A dirty `config/settings.yaml` (intentional tracked config delta).
    A dirty file *other than* that is surfaced as an informational note
    only; it does not affect the exit code or trigger an alert.
  - Network / fetch failure: NOT drift, no Telegram alert, but NOT clean
    either; exits 4 naming what could not be read.

Usage: scripts/check_deploy_drift.py [--deployed-path P] [--no-telegram] [--no-fetch]

Exit codes:
    0  checked, and in sync
    1  deployed checkout is behind origin/main
    3  deployed HEAD unreadable (not a git repo, missing, no permission)
    4  check could not complete (fetch failed, ref unresolved, git error)
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from datetime import date, datetime, timezone
from dataclasses import dataclass, field
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

DEFAULT_DEPLOYED_PATH = "/home/qamc/quant-agent"
DEFAULT_REMOTE_REF = "origin/main"
GIT_TIMEOUT_S = 20
# The one file this box intentionally carries a local edit for (noise suppression).
EXPECTED_DIRTY_FILES = {"config/settings.yaml"}


class GitError(RuntimeError):
    """A git invocation against the deployed checkout failed."""


@dataclass
class DriftReport:
    deployed_path: str
    head_sha: str | None = None
    remote_sha: str | None = None
    fetch_ok: bool = False
    fetch_error: str | None = None
    check_error: str | None = None  # a git read failed mid-check
    behind_count: int = 0
    missing_commits: list[tuple[str, str]] = field(default_factory=list)
    unexpected_dirty_files: list[str] = field(default_factory=list)

    @property
    def checked(self) -> bool:
        """False when we couldn't determine drift at all (no fetch, no HEAD)."""
        return self.head_sha is not None and self.remote_sha is not None and self.check_error is None

    @property
    def is_behind(self) -> bool:
        return self.checked and self.behind_count > 0


def _run_git(args: list[str], *, cwd: str, timeout: float = GIT_TIMEOUT_S) -> str:
    """Run a read-only git command against `cwd`. Raises GitError on failure."""
    try:
        result = subprocess.run(
            ["git", "-C", cwd, *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise GitError(f"git {' '.join(args)} failed to run: {exc}") from exc
    if result.returncode != 0:
        raise GitError(f"git {' '.join(args)} exited {result.returncode}: {result.stderr.strip()}")
    return result.stdout


def get_head_commit(deployed_path: str) -> str | None:
    """Deployed checkout's current commit SHA, or None if it can't be read."""
    try:
        return _run_git(["rev-parse", "HEAD"], cwd=deployed_path).strip()
    except GitError:
        return None


def fetch_remote(deployed_path: str, remote_ref: str) -> tuple[bool, str | None]:
    """Update the remote-tracking ref only. Never touches the working tree.

    Returns (ok, error_message). On failure (network down, DNS, timeout,
    auth), ok is False and the caller must treat this as "can't check
    right now", not "behind".
    """
    remote_name = remote_ref.split("/", 1)[0]
    branch = remote_ref.split("/", 1)[1] if "/" in remote_ref else "main"
    try:
        _run_git(["fetch", "--quiet", remote_name, branch], cwd=deployed_path)
    except GitError as exc:
        return False, str(exc)
    return True, None


def get_remote_commit(deployed_path: str, remote_ref: str) -> str | None:
    try:
        return _run_git(["rev-parse", remote_ref], cwd=deployed_path).strip()
    except GitError:
        return None


def get_missing_commits(
    deployed_path: str,
    head_sha: str,
    remote_sha: str,
) -> list[tuple[str, str]]:
    """Commits reachable from remote_sha but not from head_sha, oldest first.

    Empty when head_sha == remote_sha or already ahead. Raises GitError
    if git fails (never "no missing commits").
    """
    if head_sha == remote_sha:
        return []
    out = _run_git(
        ["log", "--reverse", "--pretty=format:%H\x1f%s", f"{head_sha}..{remote_sha}"],
        cwd=deployed_path,
    )
    commits = []
    for line in out.splitlines():
        if not line:
            continue
        sha, _, subject = line.partition("\x1f")
        commits.append((sha, subject))
    return commits


def get_unexpected_dirty_files(deployed_path: str) -> list[str]:
    """Working-tree files with local modifications, excluding the known delta.

    Raises GitError if git fails (never "no dirty files").
    """
    # `-uno`: untracked files (rotated logs, caches) are not modifications.
    out = _run_git(["status", "--porcelain", "-uno"], cwd=deployed_path)
    dirty = []
    for line in out.splitlines():
        if not line.strip():
            continue
        # porcelain format: "XY path" (path may be quoted; good enough here)
        path = line[3:].strip().strip('"')
        if path in EXPECTED_DIRTY_FILES:
            continue
        dirty.append(path)
    return dirty


def build_report(
    deployed_path: str,
    remote_ref: str = DEFAULT_REMOTE_REF,
    *,
    do_fetch: bool = True,
) -> DriftReport:
    report = DriftReport(deployed_path=deployed_path)

    report.head_sha = get_head_commit(deployed_path)
    if report.head_sha is None:
        return report

    if do_fetch:
        report.fetch_ok, report.fetch_error = fetch_remote(deployed_path, remote_ref)
        if not report.fetch_ok:
            return report
    else:
        report.fetch_ok = True

    report.remote_sha = get_remote_commit(deployed_path, remote_ref)
    if report.remote_sha is None:
        return report

    try:
        report.missing_commits = get_missing_commits(
            deployed_path,
            report.head_sha,
            report.remote_sha,
        )
        report.behind_count = len(report.missing_commits)
        report.unexpected_dirty_files = get_unexpected_dirty_files(deployed_path)
    except GitError as exc:
        report.check_error = str(exc)
    return report


def format_alert(report: DriftReport, remote_ref: str) -> str:
    lines = [
        "⚠️ QAMC deploy drift",
        f"deployed checkout is {report.behind_count} commit"
        f"{'s' if report.behind_count != 1 else ''} behind {remote_ref}",
        f"HEAD:   {report.head_sha[:10] if report.head_sha else '?'}",
        f"{remote_ref}: {report.remote_sha[:10] if report.remote_sha else '?'}",
        "",
        "Missing commits (oldest first):",
    ]
    for sha, subject in report.missing_commits:
        lines.append(f"  {sha[:10]}  {subject}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# durable state — the board reads this, and it is what stops the alert
# repeating unchanged
# ---------------------------------------------------------------------------


def record_state(
    report: "DriftReport", remote_ref: str, *, alerted: bool, state_path=None, today: date | None = None
) -> bool:
    """Write the drift snapshot where /health can read it.

    Written on EVERY run so the board can tell "checked and clean" from
    "never checked". Reuses `save_state` from `src.coverage_watchdog`.
    """
    from src import coverage_watchdog as cw, drift_state as ds

    target = state_path or ds.DEPLOY_DRIFT_STATE_PATH
    day = (today or datetime.now(timezone.utc).date()).isoformat()
    state = cw.load_state(target)
    if report.head_sha is None or report.remote_sha is None:
        status = "unknown"
    elif report.check_error is not None:
        status = "unknown"
    elif report.is_behind:
        status = "behind"
    else:
        status = "in_sync"
    state["deploy_drift"] = {
        "status": status,
        "behind_count": report.behind_count,
        "head_sha": report.head_sha,
        "remote_sha": report.remote_sha,
        "remote_ref": remote_ref,
        "deployed_path": report.deployed_path,
        "fetch_ok": report.fetch_ok,
        "missing_commits": [{"sha": sha, "subject": subject} for sha, subject in report.missing_commits],
        "unexpected_dirty_files": list(report.unexpected_dirty_files),
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }
    if alerted:
        state["drift_alerted_for"] = {"day": day, "remote_sha": report.remote_sha}
    elif report.is_behind and report.remote_sha:
        # Item 211. This run found the SAME drift and deliberately said
        # nothing. Silence that leaves no trace is indistinguishable from
        # "nothing happened", so the refusal is recorded in the same state
        # file, through the same helper the typed-alert claim uses, and is
        # readable at /alerts/suppressed.
        from src.coverage_watchdog import _record_suppressed_alert

        _record_suppressed_alert(
            state,
            "deploy_drift",
            day,
            [report.remote_sha],
        )
    state["updated_at"] = datetime.now(timezone.utc).isoformat()
    return cw.save_state(state, target)


def already_alerted(report: "DriftReport", *, state_path=None, today: date | None = None) -> bool:
    """True when this exact drift (same day, same origin/main tip) was already
    pushed. Five identical "QAMC deploy drift" messages went out in one day and
    changed nothing; a message that repeats unchanged is noise, and the board
    carries the state for as long as it lasts."""
    from src import coverage_watchdog as cw, drift_state as ds

    target = state_path or ds.DEPLOY_DRIFT_STATE_PATH
    day = (today or datetime.now(timezone.utc).date()).isoformat()
    prior = cw.load_state(target).get("drift_alerted_for") or {}
    return bool(
        isinstance(prior, dict)
        and prior.get("day") == day
        and prior.get("remote_sha")
        and prior.get("remote_sha") == report.remote_sha
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--deployed-path", default=DEFAULT_DEPLOYED_PATH)
    parser.add_argument("--remote-ref", default=DEFAULT_REMOTE_REF)
    parser.add_argument(
        "--no-fetch",
        action="store_true",
        help="Skip `git fetch`; compare against the remote-tracking ref "
        "already on disk (used by tests / offline runs).",
    )
    parser.add_argument(
        "--no-telegram",
        action="store_true",
        help="Print findings but don't push a Telegram alert.",
    )
    args = parser.parse_args(argv)

    report = build_report(
        args.deployed_path,
        args.remote_ref,
        do_fetch=not args.no_fetch,
    )

    if report.head_sha is None:
        print(
            f"check_deploy_drift: could not read HEAD in {args.deployed_path} — is it a git checkout?",
            file=sys.stderr,
        )
        return 3

    if not report.fetch_ok:
        print(
            f"check_deploy_drift: COULD NOT CHECK — fetch failed in "
            f"{args.deployed_path} ({report.fetch_error}); this is not a "
            f"clean result",
            file=sys.stderr,
        )
        return 4

    if report.remote_sha is None:
        print(
            f"check_deploy_drift: COULD NOT CHECK — could not resolve "
            f"{args.remote_ref} in {args.deployed_path}; this is not a "
            f"clean result",
            file=sys.stderr,
        )
        return 4

    if report.check_error is not None:
        print(
            f"check_deploy_drift: COULD NOT CHECK — a git read failed in "
            f"{args.deployed_path} ({report.check_error}); this is not a "
            f"clean result",
            file=sys.stderr,
        )
        record_state(report, args.remote_ref, alerted=False)
        return 4

    if report.unexpected_dirty_files:
        print(
            "check_deploy_drift: note — unexpected local modifications "
            f"(not the known config delta): {', '.join(report.unexpected_dirty_files)}",
            file=sys.stderr,
        )

    if not report.is_behind:
        print(f"check_deploy_drift: in sync with {args.remote_ref} ({report.head_sha[:10]})")
        record_state(report, args.remote_ref, alerted=False)
        return 0

    message = format_alert(report, args.remote_ref)
    print(message)

    # The board is the primary surface. It is written FIRST and
    # unconditionally, because alerts can be (and currently are) muted, and a
    # drift nobody can see is the failure this whole check exists to stop.
    sent = False
    repeat = already_alerted(report)
    if not args.no_telegram and not repeat:
        from src.notifier import TelegramNotifier
        from src.notifier.owner_alert_funnel import send_script_alert

        sent, _ = send_script_alert(message, kind="deploy_drift", script="check_deploy_drift", factory=TelegramNotifier)
    elif repeat:
        print(
            "check_deploy_drift: same drift already alerted today — not "
            "repeating the message; the state is on /health until it clears",
            file=sys.stderr,
        )
    if not record_state(report, args.remote_ref, alerted=sent or repeat):
        print(
            "check_deploy_drift: WARNING could not write the drift state file — /health will not show this drift",
            file=sys.stderr,
        )

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
