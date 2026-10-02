"""Ratchet: test files that build TradingPipeline with ``__new__`` may only shrink.

``TradingPipeline.__new__(TradingPipeline)`` skips the constructor, so every
service the constructor wires is missing or disconnected; that half-built
object is why each service pulled out of src/pipeline.py had to leave a
delegating mixin behind. The honest way is tests/pipeline_factory.py's
``build_pipeline(...)``, which runs the real ``__init__`` with stand-ins.

Baseline in tests/pipeline_new_baseline.json: the files still doing it when
the guard landed (2026-10-01). Migrate a file, then run
``PYTHONPATH=. .venv/bin/python -m scripts.pipeline_new_guard --shrink-baseline``.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BASELINE_PATH = ROOT / "tests" / "pipeline_new_baseline.json"
PATTERN = re.compile(r"TradingPipeline\.__new__\s*\(|__new__\s*\(\s*TradingPipeline\b")


def violations() -> dict[str, int]:
    """{tests/relative/path.py: count of __new__ pipeline constructions}."""
    found: dict[str, int] = {}
    for path in sorted((ROOT / "tests").rglob("*.py")):
        n = len(PATTERN.findall(path.read_text()))
        if n:
            found[path.relative_to(ROOT).as_posix()] = n
    return found


def load_baseline() -> set[str]:
    if not BASELINE_PATH.exists():
        return set()
    return set(json.loads(BASELINE_PATH.read_text())["files"])


def _write(files: set[str]) -> None:
    BASELINE_PATH.write_text(json.dumps({
        "_comment": (
            "Test files that build TradingPipeline via __new__ (skipping the real "
            "constructor) when the guard was introduced. This list may only SHRINK: "
            "migrate a file to tests/pipeline_factory.py::build_pipeline, then run "
            "`PYTHONPATH=. .venv/bin/python -m scripts.pipeline_new_guard --shrink-baseline`. "
            "Never add a file here."),
        "files": sorted(files),
    }, indent=1) + "\n")


def shrink_baseline() -> None:
    """Rewrite the baseline to the intersection with today's offenders. Never grows it."""
    _write(load_baseline() & set(violations()))


if __name__ == "__main__":
    if "--shrink-baseline" in sys.argv:
        shrink_baseline()
    elif "--seed-baseline" in sys.argv:  # one-time seeding only; the test forbids growth by review
        _write(set(violations()))
    else:
        for path, n in violations().items():
            print(f"{path}: {n}")
