"""Every production Python file the unscoped-number sentinel must look at.

Derived from the directory tree at check time, stored nowhere. The sentinel used
to walk ``src/`` alone, so root ``main.py`` (the live entry point), ``ops/`` and
``scripts/`` held numbers no sweep could see [measured 2026-10-05: 137 of 216
derived money modules were outside the ledger's scope, 17 of them outside
``src/``]. A new top-level package is picked up the day it is created.
"""

from __future__ import annotations

from pathlib import Path

#: Directory names that hold no production code: tests, vendored and generated
#: trees, caches. A hidden directory (leading dot) is skipped by rule, not by name.
NON_PRODUCTION_DIRS: frozenset[str] = frozenset(
    {"tests", "node_modules", "frontend", "venv", "site-packages", "__pycache__", "build", "dist"}
)


def is_production(rel: str) -> bool:
    """True for a repo-relative ``.py`` path outside tests, caches and hidden directories."""
    parts = Path(rel).parts[:-1]
    return rel.endswith(".py") and not any(p.startswith(".") or p in NON_PRODUCTION_DIRS for p in parts)


def py_universe(root: Path) -> list[Path]:
    """Every production ``.py`` under ``root``."""
    return [p for p in sorted(root.rglob("*.py")) if is_production(str(p.relative_to(root)))]
