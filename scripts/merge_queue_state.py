"""Finish before you start: what the merge queue says about starting new work.

Owner, 2026-10-08: ~70 unmerged branches piled up because work started faster
than it merged. The backlog stop hook (scripts/work_queue.py) asks this module
before handing back a new item: a red or conflicting open change is handed
back first; while any other change is open, no new item is handed back; if
GitHub cannot be read, nothing new is handed back on a guess.
"""
from __future__ import annotations

import json
import subprocess

_FAILED_CHECK = {"FAILURE", "ERROR", "CANCELLED", "TIMED_OUT"}


def open_changes() -> tuple[list[str], int] | None:
    """(stuck open PRs described, number open), or None if GitHub is unreadable."""
    try:
        out = subprocess.run(
            ["gh", "pr", "list", "--state", "open", "--limit", "100",
             "--json", "number,title,mergeable,statusCheckRollup"],
            capture_output=True, text=True, timeout=15, check=True).stdout
        prs = json.loads(out)
    except Exception:  # noqa: BLE001 - never break a session over this
        return None
    stuck = []
    for pr in prs:
        red = any((c.get("conclusion") or c.get("state") or "").upper()
                  in _FAILED_CHECK for c in pr.get("statusCheckRollup") or [])
        if red or pr.get("mergeable") == "CONFLICTING":
            why = "is failing its checks" if red else "conflicts with main"
            stuck.append(f"Open change #{pr['number']} ({pr['title']}) {why};")
    return stuck, len(prs)


def finish_first(state: tuple[list[str] | tuple, int] | None) -> tuple | None:
    """HookDecision arguments (block, reason[, kind]) if the queue decides, else None."""
    if state is None:
        return (False, "the merge queue could not be read; not handing back "
                       "new work on a guess")
    stuck, n_open = state
    if stuck:
        return (True, f"{stuck[0]} must be fixed or closed before anything "
                      "new starts.", "finish")
    if n_open:
        return (False, f"{n_open} open change(s) still merging; new work "
                       "waits until they land")
    return None
