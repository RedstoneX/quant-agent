"""Resolve a call in a handler to the recorder it ACTUALLY binds to.

Identity, not spelling. The silent-swallow guard recognises a durable record
by the name the call binds to, so ``from X import record_site as _site`` is
followed to ``record_site``; ``import os as rec; rec.write(...)`` binds to the
stdlib and records nothing; a module-local ``def record_site`` is judged by
what its own body does, never by its name; and a name whose origin is unknown
(star import, builtin, undefined) is not a recorder. A name match alone was
the hole: a guard identified by filename and an allow-list keyed on a count
were both bitten by the same shape (docs/GUARDS_WITHOUT_STORED_STATE.md).
"""

from __future__ import annotations

import ast
import sys
from collections.abc import Callable

#: A name bound by ``import a.b.c [as x]``: the name IS a module, not a callable.
MODULE = ""


def import_bindings(tree: ast.AST) -> dict[str, tuple[str, str]]:
    """Map each name an import binds to (module, original name).

    ``from a.b import record_site as _site`` binds ``_site`` to
    ``("a.b", "record_site")``; ``import a.b.c as x`` binds ``x`` to
    ``("a.b.c", MODULE)`` and bare ``import a.b.c`` binds ``a`` to
    ``("a", MODULE)``. A call is judged by what it binds to, never by the
    spelling at the call site.
    """
    out: dict[str, tuple[str, str]] = {}
    for n in ast.walk(tree):
        if isinstance(n, ast.ImportFrom):
            mod = "." * n.level + (n.module or "")
            for a in n.names:
                out[a.asname or a.name] = (mod, a.name)
        elif isinstance(n, ast.Import):
            for a in n.names:
                if a.asname:
                    out[a.asname] = (a.name, MODULE)
                else:
                    root = a.name.split(".")[0]
                    out[root] = (root, MODULE)
    return out


def has_star_import(tree: ast.AST) -> bool:
    return any(
        isinstance(n, ast.ImportFrom) and any(a.name == "*" for a in n.names)
        for n in ast.walk(tree)
    )


def local_defs(tree: ast.AST) -> dict[str, ast.AST]:
    """Every function defined in this module, by name, wherever it sits.

    A module-local ``def record_site`` is NOT the recorder of that name: it is
    judged by what its own body does (see ``Resolver``), never by its name.
    """
    return {
        n.name: n
        for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def _is_foreign(module: str) -> bool:
    """True for a stdlib or third-party module: it can never be the recorder."""
    if module.startswith("."):
        return False
    root = module.split(".")[0]
    return root not in {"src", "scripts", "tests"} and (
        root in sys.stdlib_module_names or root in {"yfinance", "requests", "alpaca"}
    )


def _attr_chain(f: ast.expr) -> tuple[ast.expr, list[str]]:
    """``a.b.c`` -> (Name a, ["b", "c"]); returns the base node and attrs."""
    attrs: list[str] = []
    while isinstance(f, ast.Attribute):
        attrs.insert(0, f.attr)
        f = f.value
    return f, attrs


class Resolver:
    """Resolve a call to the durable-record name it ACTUALLY binds to.

    Identity, not spelling: an alias is followed to its import; a foreign
    (stdlib / third-party) origin is never a recorder; a module-local def of a
    recorder-shaped name is judged by its own body; a name whose origin is
    unknown (star import, builtin, undefined) is not a recorder.
    """

    def __init__(self, tree: ast.AST, is_durable: Callable[[str], bool]) -> None:
        self.is_durable = is_durable
        self.binds = import_bindings(tree)
        self.defs = local_defs(tree)
        self.star = has_star_import(tree)
        self._memo: dict[str, bool] = {}

    def callee_name(self, call: ast.Call) -> str:
        f = call.func
        if isinstance(f, ast.Attribute):
            base, attrs = _attr_chain(f)
            if isinstance(base, ast.Name):
                hit = self.binds.get(base.id)
                if hit is not None:
                    mod = hit[0] if hit[1] == MODULE else hit[0] + "." + hit[1]
                    if _is_foreign(mod):
                        return ""  # ``import os as rec; rec.write(...)``
            return attrs[-1]  # an instance we cannot type: ``self.db.insert(...)``
        if isinstance(f, ast.Name):
            hit = self.binds.get(f.id)
            if hit is not None:
                if _is_foreign(hit[0]):
                    return ""  # ``from logging import warning as record_x``
                return hit[1]  # ``_site`` -> ``record_site``
            local = self.defs.get(f.id)
            if local is not None:
                return f.id if self._body_records(f.id, local) else ""
            return ""  # star-imported, builtin or undefined: origin unknown
        return ""

    def _body_records(self, name: str, fn: ast.AST) -> bool:
        """A local def counts only if ITS body makes a durable call."""
        if name in self._memo:
            return self._memo[name]
        self._memo[name] = False  # recursion guard
        found = False
        stack = list(fn.body)
        while stack and not found:
            n = stack.pop()
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)):
                continue
            if isinstance(n, ast.Call) and self.is_durable(self.callee_name(n)):
                found = True
            stack.extend(ast.iter_child_nodes(n))
        self._memo[name] = found
        return found
