"""Ratchet with no stored baseline: ``__new__``-built pipelines may only shrink.

``TradingPipeline.__new__(TradingPipeline)`` skips the constructor, so every
service the constructor wires is missing or disconnected; that half-built
object is why each service pulled out of src/pipeline.py had to leave a
delegating mixin behind. The honest way is tests/pipeline_factory.py's
``build_pipeline(...)``, which runs the real ``__init__`` with stand-ins.

The old version kept the offender list in ``tests/pipeline_new_baseline.json``,
a single shared file every open change had to edit. Per
docs/GUARDS_WITHOUT_STORED_STATE.md this stores nothing: at check time it counts
the ``__new__`` sites in the working tree, counts them again on ``origin/main``,
and reports the DELTA. If ``origin/main`` cannot be read it REFUSES; it never
passes by default.

Run it directly: ``PYTHONPATH=. .venv/bin/python -m scripts.pipeline_new_guard``.
"""
from __future__ import annotations

import re
import sys

from scripts.guard_reference import (
    ROOT,
    ReferenceUnavailable,
    TRUNK,
    trunk_blobs,
    working_paths,
)

PATTERN = re.compile(r"TradingPipeline\.__new__\s*\(|__new__\s*\(\s*TradingPipeline\b")

FIX = (
    "Fix: `from tests.pipeline_factory import build_pipeline` and call "
    "`build_pipeline(db=..., broker=..., ...)`; it runs the real __init__ with your "
    "stand-ins wired at the constructor site."
)


def _count(text: str) -> int:
    return len(PATTERN.findall(text))


def test_paths() -> list[str]:
    """Every tracked .py file under tests/ in the working tree."""
    return [p for p in working_paths("tests/") if p.endswith(".py")]


def working_sites() -> dict[str, int]:
    """{path: count of __new__ pipeline constructions} in the working tree."""
    found: dict[str, int] = {}
    for path in test_paths():
        n = _count((ROOT / path).read_text(encoding="utf-8", errors="replace"))
        if n:
            found[path] = n
    return found


def trunk_sites(paths: list[str]) -> dict[str, int]:
    """Count of __new__ sites in each given path on ``origin/main``; absent paths omitted."""
    return {p: _count(t) for p, t in trunk_blobs(sorted(paths)).items()}


def violations() -> list[str]:
    """Every file this working tree made worse than ``origin/main``, as deltas."""
    now = working_sites()
    before = trunk_sites(test_paths())
    bad: list[str] = []
    for path, n in sorted(now.items()):
        was = before.get(path)
        if was is None:
            bad.append(
                f"{path}: NEW test file with {n} TradingPipeline.__new__ site(s); "
                f"it is not on {TRUNK}, so none are grandfathered. {FIX}"
            )
        elif n > was:
            bad.append(
                f"{path}: TradingPipeline.__new__ sites grew from {was} to {n} "
                f"(+{n - was}) against {TRUNK}. {FIX}"
            )
    return bad


def main(argv: list[str] | None = None) -> int:
    try:
        bad = violations()
    except ReferenceUnavailable as exc:
        print(f"REFUSING: {exc}", file=sys.stderr)
        return 2
    if bad:
        print(
            "Constructor-skipping pipeline builds grew against %s:\n%s"
            % (TRUNK, "\n".join(bad)),
            file=sys.stderr,
        )
        return 1
    print(f"__new__-pipeline ratchet: no test file gained a site against {TRUNK}.")
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())
