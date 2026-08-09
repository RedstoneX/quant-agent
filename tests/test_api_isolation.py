"""Stage 2 Checkpoint C — trading has zero runtime dependency on the API
process being alive.

`tests/test_api_safety.py` already proves this *structurally* (no
trading-critical file imports `src.api`, at the AST/import-graph level).
This file proves it *behaviorally*: the API is started as a real, separate
OS process (matching how it actually runs in production — `python -m
src.api`, a different process from `python main.py`), confirmed live, then
killed — and an ordinary trading-process SQLite write, performed afterward
in this same test process, succeeds identically whether the API process is
running, killed, or was never started at all.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from pathlib import Path

from src.storage.db import Database

REPO_ROOT = Path(__file__).parent.parent


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_for_port(port: int, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.2)
            try:
                s.connect(("127.0.0.1", port))
                return True
            except OSError:
                time.sleep(0.1)
    return False


def _trading_write_roundtrip(tmp_path: Path, tag: str) -> None:
    """A completely ordinary trading-process DB write + read, independent
    of anything API-related."""
    db_path = tmp_path / f"trading_{tag}.db"
    db = Database(str(db_path))
    db.initialize()
    row_id = db.insert_trade(
        symbol="AAPL", action="BUY", qty=1, price=100.0,
        reasoning=f"isolation test ({tag})", run_id=f"run-{tag}",
    )
    assert row_id
    rows = db.get_trades(limit=10)
    assert len(rows) == 1
    db.close()


def test_trading_write_succeeds_with_api_never_started(tmp_path):
    """Baseline: trading works when the API package was never touched at
    all in this process (no `import src.api` anywhere above)."""
    _trading_write_roundtrip(tmp_path, "never_started")


def test_trading_write_succeeds_after_api_process_is_killed(tmp_path):
    """Start the Mission Control API as a real, separate OS process (the
    same entry point production uses — `python -m src.api`), confirm it
    answers /health, SIGKILL it, then perform an ordinary trading DB write
    in this test process and confirm it succeeds identically. Demonstrates
    process-level independence, not just import-graph independence."""
    port = _free_port()
    env = os.environ.copy()
    env["QUANT_AGENT_API_HOST"] = "127.0.0.1"
    env["QUANT_AGENT_API_PORT"] = str(port)

    proc = subprocess.Popen(
        [sys.executable, "-m", "src.api"],
        cwd=str(REPO_ROOT),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        came_up = _wait_for_port(port, timeout=15.0)
        assert came_up, "API subprocess never opened its port — cannot exercise the kill scenario"

        # Confirm it actually answers before killing it (a genuinely "up"
        # process, not just an open socket mid-crash).
        import urllib.request

        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=5) as resp:
            assert resp.status == 200
    finally:
        proc.kill()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:  # pragma: no cover - defensive
            proc.terminate()

    # The API process is now dead. An ordinary trading write must succeed
    # exactly as it would have if the API had never existed.
    _trading_write_roundtrip(tmp_path, "after_kill")
