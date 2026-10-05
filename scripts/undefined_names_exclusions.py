"""Which files the undefined-name guard reads: git-derived, minus one frozen fixture."""
from __future__ import annotations

import subprocess
from pathlib import Path

# Frozen, never-imported verbatim copy of two log bodies (indented fragments, so
# their free names are expected); excluded by exact path, nothing else is.
FIXTURE_FRAGMENTS = frozenset({"tests/fixtures/resolver_log_bodies_pre_move.py"})


def drop_frozen_fixtures(files: list[Path]) -> list[Path]:
    frozen = tuple("/" + x for x in FIXTURE_FRAGMENTS)
    return [f for f in files if not ("/" + f.as_posix()).endswith(frozen)]


def tracked_production_files() -> list[Path]:
    """Every ``.py`` git tracks, ``tests/``, root ``main.py`` and ``ops/`` included.

    Derived from git, never a written-down directory list.
    """
    root = Path(__file__).resolve().parent.parent
    out = subprocess.run(["git", "-C", str(root), "ls-files", "--", "*.py"],
                         capture_output=True, text=True)
    if out.returncode:
        print(f"git ls-files failed ({out.stderr.strip()}); refusing to read nothing as clean")
        return []
    return [root / p for p in sorted(out.stdout.splitlines())
            if p.endswith(".py")]
