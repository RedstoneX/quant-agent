"""Shared AST parse cache for the repo's whole-tree static-scan tests.

Several tests each need every module under one or more of `src/`, `ops/`,
`scripts/`, `tests/` read and parsed once (see `test_one_definition_per_
quantity.py` and `test_ops_scripts_importable.py::test_no_python_312_only_
fstrings`). Their scanned directories overlap (all of them include `src/`),
so caching by absolute file path -- not by which test asked -- means a file
already parsed by one scan is free to any other scan that later visits it,
including within the same test session across different shard scheduling.

This changes nothing about what any scan asserts: every file is still read
and parsed exactly the same way, just at most once per process.
"""

from __future__ import annotations

import ast
import functools
from pathlib import Path


@functools.lru_cache(maxsize=None)
def parse_file(path: Path) -> tuple[str, ast.AST | None]:
    """(source, tree) for one file, cached by absolute path.

    ``tree`` is ``None`` if the file does not parse -- callers that care
    about a SyntaxError already have their own dedicated coverage for that;
    these whole-repo scans skip it, exactly as they did before caching.
    """
    source = path.read_text(encoding="utf-8")
    try:
        tree: ast.AST | None = ast.parse(source, filename=str(path))
    except SyntaxError:
        tree = None
    return source, tree


def parse_tree(root: Path) -> tuple[tuple[Path, str, ast.AST], ...]:
    """(path, source, tree) for every parseable ``*.py`` file under root."""
    out = []
    for path in sorted(root.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        source, tree = parse_file(path)
        if tree is not None:
            out.append((path, source, tree))
    return tuple(out)
