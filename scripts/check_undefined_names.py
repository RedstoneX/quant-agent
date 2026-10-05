#!/usr/bin/env python3
"""Fail the build on a name that resolves nowhere (stdlib only).

Uses `symtable`, the compiler's own scope analysis: a name that a function or
class body reads as an implicit global, but that the module never binds and
that is not a builtin, would raise NameError the moment that line runs.

Blind spots: attribute names (`mod.missing`), names created dynamically
(`globals()[...]`, `setattr`), anything in a module with `from x import *`
(skipped, reported), and names bound at module level only on a path that
never runs. A module-level read of a name that is bound later in the file is
not caught (a name nothing binds at all is).
"""
from __future__ import annotations

import argparse
import ast
import builtins
import symtable
import subprocess
import sys
from pathlib import Path

_BUILTINS = set(dir(builtins)) | {"__file__", "__name__", "__doc__", "__spec__",
                                  "__loader__", "__package__", "__path__",
                                  "__builtins__", "__debug__", "__class__"}


def _walk(table, module_names, out):
    for sym in table.get_symbols():
        n = sym.get_name()
        if (sym.is_referenced() and sym.is_global() and not sym.is_assigned()
                and n not in module_names and n not in _BUILTINS):
            out.add((table.get_lineno(), n))
    for child in table.get_children():
        _walk(child, module_names, out)


def check_source(source: str, filename: str = "<src>"):
    """Return (sorted findings, has_star_import) for one module's source."""
    if any(isinstance(n, ast.ImportFrom) and any(a.name == "*" for a in n.names)
           for n in ast.walk(ast.parse(source))):
        return [], True
    top = symtable.symtable(source, filename, "exec")
    module_names = {s.get_name() for s in top.get_symbols()
                    if s.is_assigned() or s.is_imported() or s.is_namespace()}
    out: set = set()
    for sym in top.get_symbols():  # module-level code reading a name nothing binds
        n = sym.get_name()
        if (sym.is_referenced() and not sym.is_assigned() and not sym.is_imported()
                and not sym.is_namespace() and n not in _BUILTINS):
            out.add((1, n))
    for child in top.get_children():
        _walk(child, module_names, out)
    return sorted(out), False


def tracked_production_files() -> list[Path]:
    """Every ``.py`` git tracks outside ``tests/`` -- root ``main.py`` and ``ops/`` included.

    Derived from git, never a written-down directory list: the old default named
    ``src``/``scripts``/``main.py`` and silently never read ``ops/``. ``tests/`` is
    excluded only because it holds 16 known undefined names in fixture bodies.
    """
    root = Path(__file__).resolve().parent.parent
    out = subprocess.run(["git", "-C", str(root), "ls-files", "--", "*.py"],
                         capture_output=True, text=True)
    if out.returncode:
        print(f"git ls-files failed ({out.stderr.strip()}); refusing to read nothing as clean")
        return []
    return [root / p for p in sorted(out.stdout.splitlines())
            if p.endswith(".py") and p.split("/", 1)[0] != "tests"]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("roots", nargs="*", default=None,
                    help="default: every tracked .py outside tests/, derived from git")
    args = ap.parse_args(argv)
    files = []
    if not args.roots:
        files = tracked_production_files()
    for r in args.roots or []:
        p = Path(r)
        if not p.exists():
            print(f"{p}: root does not exist; refusing to read an unreadable tree as clean")
            return 1
        files += sorted(p.rglob("*.py")) if p.is_dir() else [p]
    if not files:
        print("scanned no modules; refusing to read an empty tree as clean")
        return 1
    bad = 0
    for f in files:
        try:
            found, star = check_source(f.read_text(encoding="utf-8"), str(f))
        except SyntaxError as exc:
            print(f"{f}: syntax error {exc}")
            bad += 1
            continue
        if star:
            print(f"note: {f} skipped (star import)", file=sys.stderr)
        for line, name in found:
            print(f"{f}: scope at line {line}: undefined name {name!r}")
            bad += 1
    print(f"{len(files)} files checked, {bad} finding(s)")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
