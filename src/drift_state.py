"""Read-only deploy-drift snapshot reader.

Split out of `src.coverage_watchdog` so the read-only dashboard (`src/api/`)
can read drift state without its import closure reaching the write-capable
repair and scale-in code under `src/execution/`. This module imports only the
standard library; `tests/test_api_cannot_trade.py` pins that by walking the
real import closure.

The snapshot is written by `scripts/check_deploy_drift.py` (which owns the
write side) and read by the /health API, so a checkout that is behind
origin/main is visible on the desk's own board, not only in a Telegram message.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from src.data_paths import alerting_dir

DEPLOY_DRIFT_STATE_PATH = alerting_dir() / "deploy_drift.json"


def load_drift_state(path: Path | None = None) -> dict[str, Any]:
    """Return the parsed snapshot, or {} when missing or unreadable. Never raises."""
    try:
        raw = json.loads((path or DEPLOY_DRIFT_STATE_PATH).read_text())
    except (OSError, ValueError):
        return {}
    return raw if isinstance(raw, dict) else {}
