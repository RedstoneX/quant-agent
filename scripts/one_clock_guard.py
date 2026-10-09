"""One clock per decision: a module may not derive a day in Python AND date rows with SQL ``'now'``.

Python's day is the exchange's (``et_today``); SQLite's ``'now'`` is UTC. Between
UTC midnight and ET midnight they name different days, so a module holding both
silently reads the wrong day in that window. Three incidents had this shape; the
local-day guard only watches the Python half.

A MIXED module is one whose code (docstrings and comments excluded) holds both:
  py_day   ``et_today()``, ``todays_session_*()``, ``date.today()`` or ``<expr>.date()``
  sql_now  a string constant containing ``'now'``, ``CURRENT_TIMESTAMP`` or ``CURRENT_DATE``

The rule is ABSOLUTE: no trunk comparison, no grandfathered list. Every site in
a mixed module is a violation, except modules named in ``EXEMPT`` (identity, with
a reason). Run: ``python -m scripts.one_clock_guard``.
"""

from __future__ import annotations

import ast
import re
import sys

from scripts.guard_reference import ROOT, enclosing_scopes, site_identity, working_paths

Site = tuple[str, str, str, str]

#: Modules allowed to hold both, by path. Not a count: a new module is never exempt.
EXEMPT = {
    "src/cost_circuit/clock.py": "IS the sanctioned clock: pins SQL 'now' to the caller's "
    "instant and converts a stored UTC stamp to its ET day",
}

_SQL_NOW = re.compile(r"'now'|CURRENT_TIMESTAMP|CURRENT_DATE", re.IGNORECASE)
_DAY_READERS = {"et_today", "todays_session_stamp", "todays_session_bar_stamp", "todays_session_snapshot_stamps"}


def _docstrings(tree: ast.AST) -> set[int]:
    ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                ids.add(id(body[0].value))
    return ids


def _py_day(call: ast.Call) -> bool:
    fn = call.func
    name = fn.id if isinstance(fn, ast.Name) else fn.attr if isinstance(fn, ast.Attribute) else ""
    if name in _DAY_READERS:
        return True
    if isinstance(fn, ast.Attribute) and fn.attr == "date" and isinstance(fn.value, ast.Call):
        return True
    return isinstance(fn, ast.Attribute) and fn.attr == "today" and isinstance(fn.value, (ast.Name, ast.Attribute))


def scan_sites(path: str, text: str) -> list[tuple[str, int, str, str]]:
    """Every (kind, line, scope, source) site in a module that holds BOTH clocks; else none."""
    tree = ast.parse(text)
    scopes = enclosing_scopes(tree)
    skip = _docstrings(tree)
    py: list[tuple[str, int, str, str]] = []
    sql: list[tuple[str, int, str, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and _py_day(node):
            py.append(("py_day", node.lineno, *site_identity(node, scopes)))
        elif (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in skip
            and _SQL_NOW.search(node.value)
        ):
            scope, _ = site_identity(node, scopes)
            sql.append(("sql_now", node.lineno, scope, " ".join(node.value.split())[:120]))
    return py + sql if py and sql else []


def scanned_paths() -> list[str]:
    return sorted(working_paths("src/*.py"))


def working_sites() -> dict[Site, list[int]]:
    out: dict[Site, list[int]] = {}
    for path in scanned_paths():
        try:
            text = (ROOT / path).read_text(encoding="utf-8", errors="replace")
            found = scan_sites(path, text)
        except SyntaxError:
            continue
        for kind, line, scope, src in found:
            out.setdefault((path, kind, scope, src), []).append(line)
    return out


def violations() -> list[str]:
    return [
        f"{path} [{kind}] in {scope}: `{src}`; lines {sorted(lines)}"
        for (path, kind, scope, src), lines in sorted(working_sites().items())
        if path not in EXEMPT
    ]


def main() -> int:
    bad = violations()
    if bad:
        print(
            "a module mixes a Python exchange day with SQL 'now' (UTC); use one clock "
            "(bind the day from et_today and compare timestamps against its UTC bounds):\n" + "\n".join(bad),
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
