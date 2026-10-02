"""Ratchet with no stored baseline: ``__new__``-built pipelines may only shrink.

``TradingPipeline.__new__(TradingPipeline)`` skips the constructor, so every
service the constructor wires is missing or disconnected; that half-built
object is why each service pulled out of src/pipeline.py had to leave a
delegating mixin behind. The honest way is tests/pipeline_factory.py's
``build_pipeline(...)``, which runs the real ``__init__`` with stand-ins.

The old version kept the offender list in ``tests/pipeline_new_baseline.json``,
a single shared file every open change had to edit. Per
docs/GUARDS_WITHOUT_STORED_STATE.md this stores nothing: at check time it names
every ``__new__`` site in the working tree (file plus the site's own source
line), names them again on ``origin/main``, and fails on any IDENTITY the tree
holds more copies of than the trunk — never a total, so removing one site and
adding a different one still fails. If ``origin/main`` cannot be read it
REFUSES; it never passes by default.

Run it directly: ``PYTHONPATH=. .venv/bin/python -m scripts.pipeline_new_guard``.
"""
from __future__ import annotations

import re
import sys

from scripts.guard_reference import (
    ROOT,
    ReferenceUnavailable,
    TRUNK,
    added_sites,
    trunk_blobs,
    working_paths,
)

#: One offending site: (path, the source line holding the __new__ call, whitespace collapsed).
Site = tuple[str, str]

PATTERN = re.compile(r"TradingPipeline\.__new__\s*\(|__new__\s*\(\s*TradingPipeline\b")

FIX = (
    "Fix: `from tests.pipeline_factory import build_pipeline` and call "
    "`build_pipeline(db=..., broker=..., ...)`; it runs the real __init__ with your "
    "stand-ins wired at the constructor site."
)


def _sites(path: str, text: str) -> dict[Site, list[int]]:
    """Every __new__ site in one file, keyed by identity, with its line numbers."""
    out: dict[Site, list[int]] = {}
    for lineno, line in enumerate(text.splitlines(), 1):
        for _ in PATTERN.findall(line):
            out.setdefault((path, " ".join(line.split())), []).append(lineno)
    return out


def test_paths() -> list[str]:
    """Every tracked .py file under tests/ in the working tree."""
    return [p for p in working_paths("tests/") if p.endswith(".py")]


def working_sites() -> dict[Site, list[int]]:
    """Every __new__ pipeline site in the working tree, with the lines it occurs on."""
    found: dict[Site, list[int]] = {}
    for path in test_paths():
        found.update(_sites(path, (ROOT / path).read_text(encoding="utf-8", errors="replace")))
    return found


def trunk_sites(paths: list[str]) -> dict[Site, int]:
    """Copies of each __new__ site on ``origin/main``; sites in absent paths are simply none."""
    counts: dict[Site, int] = {}
    for path, text in trunk_blobs(sorted(paths)).items():
        for site, lines in _sites(path, text).items():
            counts[site] = len(lines)
    return counts


def violations() -> list[str]:
    """Every __new__ site this working tree holds that ``origin/main`` does not.

    Identity, not total: a site is (path, source line), so a removed site never
    licenses a different new one, and a test file absent from the trunk has
    nothing grandfathered.
    """
    now = working_sites()
    before = trunk_sites(test_paths())
    on_trunk = {path for path, _line in before}
    bad: list[str] = []
    for (path, line), n, was in added_sites(
        {site: len(lines) for site, lines in now.items()}, before
    ):
        where = f"line(s) {sorted(now[(path, line)])}"
        if path not in on_trunk:
            bad.append(
                f"{path}: NEW test file with a TradingPipeline.__new__ site at {where} "
                f"(`{line}`); it is not on {TRUNK}, so nothing is grandfathered. {FIX}"
            )
        else:
            bad.append(
                f"{path}: NEW TradingPipeline.__new__ site at {where} (`{line}`), "
                f"x{n} on this branch vs x{was} on {TRUNK} (+{n - was}). {FIX}"
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
