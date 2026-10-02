"""__new__-pipeline ratchet: no NEW test file may build TradingPipeline without its constructor.

See scripts/pipeline_new_guard.py for why, and tests/pipeline_factory.py for
the replacement. Baseline in tests/pipeline_new_baseline.json may only shrink.
"""
from __future__ import annotations

from scripts import pipeline_new_guard as g

_FIX = "PYTHONPATH=. .venv/bin/python -m scripts.pipeline_new_guard --shrink-baseline"


def test_no_new_file_builds_pipeline_with_dunder_new():
    now = g.violations()
    new = sorted(set(now) - g.load_baseline())
    assert not new, (
        "NEW test file(s) build TradingPipeline with __new__ (constructor skipped):\n"
        + "\n".join(f"  {k}  ({now[k]} site(s))" for k in new)
        + "\nFix: `from tests.pipeline_factory import build_pipeline` and call "
        "`build_pipeline(db=..., broker=..., ...)`; it runs the real __init__ with your "
        "stand-ins wired at the constructor site. Do NOT add the file to "
        "tests/pipeline_new_baseline.json; that list may only shrink."
    )


def test_baseline_only_shrinks():
    stale = sorted(g.load_baseline() - set(g.violations()))
    assert not stale, (
        "These baseline files no longer use __new__ (good, you migrated them): "
        + ", ".join(stale)
        + f"\nFix: run `{_FIX}` and commit tests/pipeline_new_baseline.json so the ratchet tightens."
    )


def test_baseline_files_still_exist():
    missing = sorted(k for k in g.load_baseline() if not (g.ROOT / k).exists())
    assert not missing, f"Baseline names deleted files; run `{_FIX}`: {missing}"
