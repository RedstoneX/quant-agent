"""Refresh the local ``origin/main`` ref once per process, before a guard reads it.

A worktree's ``origin/main`` is whatever was last fetched. Guards that measure
the branch against that ref report growth in files the branch never touched once
main has moved. This module fetches main so the reference is current.

The fetch only ever MOVES the reference to the real remote tip; it cannot make a
violation pass, because the comparison is still branch against trunk. It never
raises: when the network is down the local ref is used and the returned note says
the verdict is STALE, so a stale verdict is never mistaken for a fresh one. In CI
(full history already checked out) nothing is fetched, which keeps CI deterministic.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

FETCH_TIMEOUT_SECONDS = 30
_notes: dict[str, str] = {}


def _in_ci() -> bool:
    return bool(os.environ.get("GITHUB_ACTIONS") or os.environ.get("CI"))


def refresh_trunk(root: Path, remote_ref: str = "origin/main") -> str:
    """Fetch once per root per process; return "" if fresh/skipped, else a STALE note."""
    key = str(root)
    if key in _notes:
        return _notes[key]
    note = ""
    if not _in_ci():
        remote, _, branch = remote_ref.partition("/")
        try:
            done = subprocess.run(
                ["git", "fetch", "--quiet", "--no-tags", remote, branch],
                cwd=root, capture_output=True, text=True,
                timeout=FETCH_TIMEOUT_SECONDS,
                env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
            )
            if done.returncode != 0:
                note = f"git fetch failed: {done.stderr.strip()[:200]}"
        except (OSError, subprocess.TimeoutExpired) as exc:
            note = f"git fetch could not run: {type(exc).__name__}"
    if note:
        note = (f"WARNING: {remote_ref} could not be refreshed ({note}); this verdict "
                "uses the LOCAL, possibly STALE ref and may charge you for main's own growth")
        print(note, file=sys.stderr)
    _notes[key] = note
    return note
