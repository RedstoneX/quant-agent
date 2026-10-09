"""Sandbox reclaim for the rehearsal cost-ceiling test (moved out of that file).

Each prepared sandbox is a full copy of a database, so the autouse fixture
removes them when each test ends. Also holds the recording-start helper moved
verbatim out of tests/test_rehearsal_reproduces_cost_ceiling.py.
"""

from __future__ import annotations

import shutil
import sqlite3

import pytest


def _recording_started_utc(db_path, run_id: str) -> str:
    """When the recorded run this rehearsal replays actually began (UTC).

    `agent_logs.timestamp` is SQLite's `datetime('now')`, i.e. UTC — the same
    assumption `select_replay_run` documents and orders on.
    """
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        row = conn.execute(
            "SELECT MIN(timestamp) FROM agent_logs WHERE run_id = ?",
            (run_id,),
        ).fetchone()
    finally:
        conn.close()
    return str((row or [None])[0] or "")


# Measured 2026-10-04 (du over /tmp/pytest-of-ubuntu): each run of the test below leaves two
# prepared sandboxes, each a full snapshot of the production database, 1.35 GB per run, and
# pytest keeps the last three runs of every worker: about 4 GB of the 6 GB pytest scratch.
SANDBOX_DIRS = ("sandbox", "sandbox-2")


def reclaim_sandboxes(root):
    """Remove the prepared sandboxes under `root`; returns how many were removed."""
    removed = 0
    for name in SANDBOX_DIRS:
        target = root / name
        if target.exists():
            shutil.rmtree(target, ignore_errors=True)
            removed += 1
    return removed


@pytest.fixture(autouse=True)
def _reclaim_sandboxes_after(tmp_path):
    yield
    reclaim_sandboxes(tmp_path)


def test_the_sandboxes_this_file_prepares_are_removed_when_the_test_ends(tmp_path):
    for name in SANDBOX_DIRS:
        (tmp_path / name / "data").mkdir(parents=True)
        (tmp_path / name / "data" / "quant_agent.db").write_bytes(b"x" * 1024)
    assert reclaim_sandboxes(tmp_path) == len(SANDBOX_DIRS)
    assert not any((tmp_path / name).exists() for name in SANDBOX_DIRS)
