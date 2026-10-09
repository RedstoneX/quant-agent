"""Size and shape limits, checked by a standard tool at FIXED numbers.

The limits live in ``pyproject.toml`` under ``[tool.ruff]``: line width, and per
function the complexity, statement and branch caps. They are never re-derived
from trunk, so trunk moving cannot redden an unrelated change, and they are
counted per function, so squeezing statements onto fewer lines cannot dodge
them. Files that already broke a limit are listed there by path; everything
else is held to the limit.

Runs inside the required ``pytest`` check rather than as a separate status.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_ruff_reports_nothing():
    # ruff ships in the `dev` extra; a missing ruff fails here rather than skipping.
    out = subprocess.run(
        [sys.executable, "-m", "ruff", "check", "--no-cache", "src", "scripts", "tests"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert out.returncode == 0, out.stdout + out.stderr


def test_ruff_format_reports_nothing():
    # The standard formatter, so squeezed code cannot hide from the size limits.
    out = subprocess.run(
        [sys.executable, "-m", "ruff", "format", "--check", "src", "scripts", "tests"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert out.returncode == 0, out.stdout + out.stderr
