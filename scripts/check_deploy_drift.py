#!/usr/bin/env python3
"""Detect a deployed checkout that is behind what was merged.

Deterministic and read-only. No LLM call, no daemon, no new alert path.

The problem this closes: QAMC is deployed by hand to a detached-HEAD git
checkout (`/home/qamc/quant-agent`). A merge to `origin/main` can be
recorded as "deployed" in docs/chat without the box ever being
`git checkout`'d onto it. Nothing compared RUNNING vs MERGED, so the gap
was silent for hours. This script is that comparison.

What it does:
  1. `git fetch origin main` in the deployed checkout — updates the
     remote-tracking ref only. Never touches the working tree or HEAD,
     so it cannot disturb a running trading session.
  2. Compares the checkout's HEAD against the freshly fetched
     `origin/main`.
  3. If the checkout is behind, prints the count and the subject line of
     every missing commit, sends one Telegram alert via the existing
     `TelegramNotifier` (same class `scripts/cost_circuit.py` and the
     shutdown/hold alerts use — no new sender), and exits non-zero.
  4. If it's in sync, prints one line and exits 0. No Telegram push.

Expected, non-alarming states (must NOT be reported as drift):
  - Detached HEAD. That's how deploys work here; this script never
    compares branch names, only commit reachability.
  - A dirty `config/settings.yaml`. The box carries an intentional
    tracked config delta. Working-tree dirtiness plays no part in the
    drift computation (commit-only comparison), but a dirty file
    *other than* `config/settings.yaml` is surfaced as an informational
    note so an operator notices an unexpected local edit — it still
    does not affect the exit code or trigger an alert.
  - Network / fetch failure. Degrades quietly: prints a warning to
    stderr and exits 0. A box that can't reach GitHub right now is not
    evidence it's behind, and alerting on every transient egress blip
    would train operators to ignore the channel.

Usage:
    scripts/check_deploy_drift.py
    scripts/check_deploy_drift.py --deployed-path /home/qamc/quant-agent
    scripts/check_deploy_drift.py --no-telegram   # print only, no push
    scripts/check_deploy_drift.py --no-fetch      # use existing refs (tests)

Exit codes:
    0  in sync, or the check could not run (fetch/network failure)
    1  deployed checkout is behind origin/main
    3  deployed checkout HEAD could not be determined (not a git repo,
       path missing, etc.) — an operator problem, not a drift finding
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

DEFAULT_DEPLOYED_PATH = "/home/qamc/quant-agent"
DEFAULT_REMOTE_REF = "origin/main"
GIT_TIMEOUT_S = 20
# The one file this box is expected to carry a local edit for. See the
# module docstring — this is not a security allowlist, just noise
# suppression for a known, intentional delta.
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
    behind_count: int = 0
    missing_commits: list[tuple[str, str]] = field(default_factory=list)
    unexpected_dirty_files: list[str] = field(default_factory=list)

    @property
    def checked(self) -> bool:
        """False when we couldn't determine drift at all (no fetch, no HEAD)."""
        return self.head_sha is not None and self.remote_sha is not None

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
        raise GitError(
            f"git {' '.join(args)} exited {result.returncode}: "
            f"{result.stderr.strip()}"
        )
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
    deployed_path: str, head_sha: str, remote_sha: str,
) -> list[tuple[str, str]]:
    """Commits reachable from remote_sha but not from head_sha, oldest first.

    Empty when head_sha == remote_sha or head_sha is already ahead/equal.
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

    Informational only — never affects the drift verdict or exit code.
    """
    try:
        out = _run_git(["status", "--porcelain"], cwd=deployed_path)
    except GitError:
        return []
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
    deployed_path: str, remote_ref: str = DEFAULT_REMOTE_REF, *, do_fetch: bool = True,
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

    report.missing_commits = get_missing_commits(
        deployed_path, report.head_sha, report.remote_sha,
    )
    report.behind_count = len(report.missing_commits)
    report.unexpected_dirty_files = get_unexpected_dirty_files(deployed_path)
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--deployed-path", default=DEFAULT_DEPLOYED_PATH)
    parser.add_argument("--remote-ref", default=DEFAULT_REMOTE_REF)
    parser.add_argument(
        "--no-fetch", action="store_true",
        help="Skip `git fetch`; compare against the remote-tracking ref "
        "already on disk (used by tests / offline runs).",
    )
    parser.add_argument(
        "--no-telegram", action="store_true",
        help="Print findings but don't push a Telegram alert.",
    )
    args = parser.parse_args(argv)

    report = build_report(
        args.deployed_path, args.remote_ref, do_fetch=not args.no_fetch,
    )

    if report.head_sha is None:
        print(
            f"check_deploy_drift: could not read HEAD in "
            f"{args.deployed_path} — is it a git checkout?",
            file=sys.stderr,
        )
        return 3

    if not report.fetch_ok:
        print(
            f"check_deploy_drift: fetch failed, skipping this check "
            f"({report.fetch_error})",
            file=sys.stderr,
        )
        return 0

    if report.remote_sha is None:
        print(
            f"check_deploy_drift: could not resolve {args.remote_ref} "
            f"after fetch — skipping this check",
            file=sys.stderr,
        )
        return 0

    if report.unexpected_dirty_files:
        print(
            "check_deploy_drift: note — unexpected local modifications "
            f"(not the known config delta): {', '.join(report.unexpected_dirty_files)}",
            file=sys.stderr,
        )

    if not report.is_behind:
        print(f"check_deploy_drift: in sync with {args.remote_ref} "
              f"({report.head_sha[:10]})")
        return 0

    message = format_alert(report, args.remote_ref)
    print(message)

    if not args.no_telegram:
        from src.notifier import TelegramNotifier

        notifier = TelegramNotifier()
        if notifier.enabled:
            notifier.send(message)
        else:
            print(
                "check_deploy_drift: Telegram not configured; alert printed "
                "above only",
                file=sys.stderr,
            )

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
