"""``__new__``-built pipelines may only shrink: a fixed list of sites.

``TradingPipeline.__new__(TradingPipeline)`` skips the constructor, so every
service the constructor wires is missing or disconnected; that half-built
object is why each service pulled out of src/pipeline.py had to leave a
delegating mixin behind. The honest way is tests/pipeline_factory.py's
``build_pipeline(...)``, which runs the real ``__init__`` with stand-ins.

The existing sites are pinned by identity (path | the site's own source line,
whitespace collapsed) in ``config/check_allowlists/struct_pipeline_new.txt``.
Nothing is compared with any trunk. The check fails on a site not in the list
and on a listed site that no longer occurs (stale entry); a repeated identical
line counts once per copy.

Run it directly: ``PYTHONPATH=. .venv/bin/python -m scripts.pipeline_new_guard``.
"""
from __future__ import annotations

import re
import subprocess
import sys

from scripts import struct_allowlist
from scripts.struct_allowlist import ROOT

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
    out = subprocess.run(["git", "ls-files", "tests/"], cwd=ROOT, capture_output=True, text=True, check=True)
    return sorted(p for p in out.stdout.splitlines() if p.endswith(".py"))


def working_sites() -> dict[Site, list[int]]:
    """Every __new__ pipeline site in the working tree, with the lines it occurs on."""
    found: dict[Site, list[int]] = {}
    for path in test_paths():
        found.update(_sites(path, (ROOT / path).read_text(encoding="utf-8", errors="replace")))
    return found


def found() -> list[str]:
    """One ``path | source line`` entry per copy of each site."""
    return [f"{path} | {line}" for (path, line), lines in working_sites().items() for _ in lines]


def violations(directory=None) -> list[str]:
    """Sites not in the fixed list, and listed sites that no longer occur."""
    return struct_allowlist.problems("pipeline_new", found(), FIX, directory)


def main(argv: list[str] | None = None) -> int:
    bad = violations()
    if bad:
        print("Constructor-skipping pipeline builds differ from the fixed list:\n%s" % "\n".join(bad), file=sys.stderr)
        return 1
    print("__new__-pipeline ratchet: sites match the fixed list.")
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())
