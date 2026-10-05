"""Rule (f): numeric literals passed as keyword arguments at call sites.

`do_thing(window=20)` fixes a live value that the module-level scan never sees,
and the registered default on the callee is dead because the call overrides it.

Representation: every site is a `NumberSite` object, end to end. A site id string
exists only as that object's `site_id`, used at the two edges (ledger lookup and
display); no collection here or in the guard holds bare id strings.
"""
from __future__ import annotations

import ast
from collections.abc import Collection
from pathlib import Path

from src.feature_flags import config_modules
from src.number_scope import py_universe
from src.number_site_scan import NEUTRAL_VALUES, NumberSite, _numeric
from src.number_sources import UNSCOPED_SENTINEL_EXCLUDE, _scoped_files

_SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)


def _calls(node: ast.AST, qual: str):
    """Yield `(call, qualname)` for every call, attributed to its innermost def or class."""
    for child in ast.iter_child_nodes(node):
        if isinstance(child, _SCOPES):
            yield from _calls(child, f"{qual}.{child.name}" if qual else child.name)
        else:
            if isinstance(child, ast.Call):
                yield child, qual
            yield from _calls(child, qual)


def scan_source(source: str, module: str, rel: str) -> list[NumberSite]:
    """Every keyword-literal site in one module's source, one per distinct (id, value)."""
    sites: dict[tuple[str, float], NumberSite] = {}
    for call, qual in _calls(ast.parse(source), ""):
        callee = ast.unparse(call.func)
        for kw in call.keywords:
            value = _numeric(kw.value) if kw.arg else None
            if value is None or value in NEUTRAL_VALUES:
                continue
            site_id = f"{module}.{qual or '<module>'}:call[{callee}({kw.arg})]"
            sites.setdefault((site_id, value), NumberSite(site_id, rel, kw.value.lineno, value))
    return list(sites.values())


def collect_callsite_sites(root: Path, registered: Collection[str] = ()) -> list[NumberSite]:
    """Unregistered keyword-literal sites over the same files the unscoped sentinel scans."""
    in_scope = {p.resolve() for p in _scoped_files(root)}
    in_scope.update(p.resolve() for p in config_modules(root))
    out: list[NumberSite] = []
    for path in py_universe(root):
        rel = str(path.relative_to(root))
        if path.resolve() in in_scope or any(rel.startswith(p) for p in UNSCOPED_SENTINEL_EXCLUDE):
            continue
        module = rel[: -len(".py")].replace("/", ".").removesuffix(".__init__")
        try:
            found = scan_source(path.read_text(encoding="utf-8"), module, rel)
        except SyntaxError:  # pragma: no cover - a broken file fails elsewhere
            continue
        out.extend(s for s in found if s.site_id not in registered)
    return sorted(out, key=lambda s: (s.site_id, s.value))
