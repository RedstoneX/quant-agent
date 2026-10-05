#!/usr/bin/env python3
"""Runnable entry point for the live-capital pre-flight gate (board item 150).

Prints a per-condition pass/fail report and exits non-zero (BLOCKED) unless
every named pre-flight condition is satisfied. Run this before any live-capital
activation; a non-zero exit means live capital must NOT be switched on.

Usage:
    .venv/bin/python scripts/live_capital_preflight.py
    .venv/bin/python scripts/live_capital_preflight.py --attestations <path>
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.config import AlpacaConfig  # noqa: E402
from src.live_capital_preflight import _PAPER_HOST, main  # noqa: E402


def _non_paper_config() -> object:
    """Probe for the paper-guard audit check: must raise while the guard holds.

    Injected from here because the gate module cannot import the config class
    that calls it (that was an import cycle); this script sits above both.
    """
    return AlpacaConfig(base_url=f"https://{_PAPER_HOST}", paper=False)


if __name__ == "__main__":
    sys.exit(main(paper_guard_probe=_non_paper_config))
