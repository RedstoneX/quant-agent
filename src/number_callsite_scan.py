"""Rule (f): numeric literals passed as KEYWORD ARGUMENTS at a call site.

A default on a parameter is only the number the desk uses when nobody passes
one. When the one caller passes ``lookback_days=5`` the default is dead and the
literal at the call is the live number, which rules (a)-(e) in
``src.number_site_scan`` never saw. This walks the same syntax tree, with the
same leaf rule, and yields one site per such literal.

Excluded as noise, each judged on what it is a setting OF, not on its value:

* ``0`` (the scanner's own neutral value) and an integer ``1`` / ``-1``: an
  index, a count of one or a sign. A float ``1.0`` is NOT excluded; the
  scanner's own history shows one ATR is a real setting.
* Pydantic ``Field(...)`` / dataclass ``field(...)``: its ``default`` is rule
  (b)'s field default and ``le``/``ge``/``min_length`` are schema bounds.
* Keyword names that configure plumbing, never a decision: timeouts, JSON
  indent, worker/queue/log-rotation sizes, HTTP status codes, the clock parts
  of a ``datetime`` constructor, string-split limits and float tolerances.
"""

from __future__ import annotations

import ast
from pathlib import Path

from src.number_site_scan import (
    NEUTRAL_VALUES,
    NumberSite,
    _imported_constants,
    _leaves,
    _module_constants,
    _qualified_scopes,
)
from src.number_universe import py_universe

#: Callees whose keywords are schema declarations, not call-site settings.
SCHEMA_CALLEES: frozenset[str] = frozenset({"Field", "field"})

#: Keyword names that are formatting, I/O or clock plumbing at any call.
PLUMBING_KEYWORDS: frozenset[str] = frozenset(
    {
        "timeout", "timeout_seconds", "indent", "max_workers", "status_code", "maxsize",
        "maxBytes", "backupCount", "maxsplit", "width", "height", "microseconds",
        "hour", "minute", "second", "day", "month", "year", "rel_tol", "abs_tol",
        "le", "ge", "lt", "gt", "min_length", "max_length", "ndigits",
    }
)


def _callee(func: ast.AST) -> str:
    """The dotted name of what is called, ``?`` when it is not a plain name."""
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return f"{_callee(func.value)}.{func.attr}"
    return "?"


def _is_unit_int(node: ast.AST) -> bool:
    """True for the integer literals 1 and -1 (an index, a count of one, a sign)."""
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
        node = node.operand
    return isinstance(node, ast.Constant) and type(node.value) is int and node.value == 1


def _walk_scope(scope: ast.AST):
    """Every node in the scope's own body, nested defs and classes excluded, lambdas included."""
    stack = list(ast.iter_child_nodes(scope))
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        yield node
        stack.extend(ast.iter_child_nodes(node))


def scan_callsite_literals(
    tree: ast.Module,
    module: str,
    rel: str,
    names: dict[str, float],
    local: dict[str, float],
) -> list[NumberSite]:
    """One site per numeric keyword-argument literal, id ``<scope>:call[<callee>(<kw>)]``.

    A repeat of the same callee and keyword in one scope gets ``#1``, ``#2``,
    in source order, so an edit elsewhere never renumbers it.
    """
    sites: list[NumberSite] = []
    scopes: list[tuple[ast.AST, str]] = [(tree, module)]
    scopes += [(n, f"{module}.{q}") for n, q in _qualified_scopes(tree)]
    for scope, qual in scopes:
        found: list[tuple[int, int, str, ast.AST]] = []
        for node in _walk_scope(scope):
            if not isinstance(node, ast.Call):
                continue
            callee = _callee(node.func)
            if callee.rsplit(".", 1)[-1] in SCHEMA_CALLEES:
                continue
            for kw in node.keywords:
                if kw.arg is None or kw.arg in PLUMBING_KEYWORDS or _is_unit_int(kw.value):
                    continue
                found.append((kw.value.lineno, kw.value.col_offset, f"{callee}({kw.arg})", kw.value))
        seen: dict[str, int] = {}
        for lineno, _col, label, value_node in sorted(found, key=lambda f: f[:2]):
            ordinal = seen.get(label, 0)
            seen[label] = ordinal + 1
            suffix = f"#{ordinal}" if ordinal else ""
            for site_id, value, line in _leaves(value_node, f"{qual}:call[{label}{suffix}]", names, local):
                if value not in NEUTRAL_VALUES:
                    sites.append(NumberSite(site_id, rel, line or lineno, value))
    return sites


def collect_callsite_sites(root: Path, exclude: tuple[str, ...] = ("src/frontend",)) -> list[NumberSite]:
    """Rule (f) over every production ``.py`` under ``root``, scoped or not, sorted by id.

    Deliberately a second collector and not part of ``collect_sites``: that one
    obliges the ledger to hold a row for every site it returns, and 700-odd
    call-site literals were never reviewed. ``scripts/unscoped_number_guard.py``
    refuses a NEW one; ``audit`` only knows these ids so a row for one is not an orphan.
    """
    sites: list[NumberSite] = []
    for file_path in py_universe(root):
        rel = str(file_path.relative_to(root))
        if any(rel.startswith(prefix) for prefix in exclude):
            continue
        try:
            tree = ast.parse(file_path.read_text(encoding="utf-8"), filename=rel)
        except SyntaxError:  # pragma: no cover - a broken file fails elsewhere
            continue
        module = rel[: -len(".py")].replace("/", ".")
        if module.endswith(".__init__"):
            module = module[: -len(".__init__")]
        local = _module_constants(tree)
        names = {**_imported_constants(tree, root), **local}
        sites.extend(scan_callsite_literals(tree, module, rel, names, local))
    return sorted(sites, key=lambda s: s.site_id)
