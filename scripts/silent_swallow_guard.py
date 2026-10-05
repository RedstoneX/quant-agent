"""Silent-swallow guard with no stored baseline: scan here, scan trunk, compare.

The pattern it hunts (measured 2026-10-01: 949 broad handlers in src/, 922 of
which never re-raise)::

    try:
        return broker.get_stop(symbol)
    except Exception as exc:
        logger.warning("...", exc)      # a log line is NOT a record
        return None                     # caller reads this as "no stop exists"

A desk that swallows a broker error and returns None is indistinguishable from a
desk that genuinely found nothing; ``get_current_stop_price`` returning None on
ANY error made ``pipeline_protection`` skip the position as "no stop to adjust".

"Durable" means what THIS codebase actually uses to persist a failure: the
event journal (``record_pipeline_event`` / ``persist_evidence``), the sqlite
writers (``insert_*`` / ``save_*`` / ``upsert_*`` / ``write_back_*`` /
``_write_ahead_*`` / ``mark_*``), the owner alert path (``send_owner_alert``,
``_alert_owner_*``, ``claim_typed_alert``), the refusal/heal recorders
(``record_*`` / ``_record_*``) and raw ``execute`` / ``commit`` on a connection.
A handler that re-raises is never a swallow. Logging never counts.

Scope: the money-touching modules only, derived from source at check time by
``scripts/money_modules.py`` -- never a pinned list of paths.

This guard stores nothing (docs/GUARDS_WITHOUT_STORED_STATE.md). The old
tests/silent_swallow_baseline.json was one shared shrink-only file that every
open change had to edit, so changes jammed each other. At check time the scan
runs twice -- once over the working tree, once over the same modules as they
stand on ``origin/main`` -- and the guard reports the DELTA: which handlers this
change ADDED. Sites are compared by IDENTITY (path, enclosing scope, handler
source text), never by a total or an ordinal, so removing one offender and
adding another in the same function still fails. If ``origin/main`` cannot be
read it REFUSES; it never passes by default.

Run it directly: ``python -m scripts.silent_swallow_guard``.
"""
from __future__ import annotations

import ast
import sys

from scripts.money_modules import derive as derive_money_modules
from scripts.guard_reference import (
    ROOT,
    ReferenceUnavailable,
    TRUNK,
    added_sites,
    enclosing_scopes,
    site_identity,
    trunk_blobs,
)

#: Money-touching modules are DERIVED at check time (scripts/money_modules.py):
#: every module holding a function from which an exchange write is reachable,
#: plus every module such a function calls into. The hand-edited tuple that
#: stood here was stored bookkeeping; measured 2026-10-04 it had drifted from
#: the code (protected_sell, stage_execution, order_idempotency, pending_stop_drain
#: all unnamed) and it named pipeline_stages as "the execution stage itself"
#: after that body had moved to stage_execution.
def money_modules() -> tuple[str, ...]:
    return derive_money_modules()


BROAD_NAMES = {"Exception", "BaseException"}

#: A callee whose first name-token (leading underscores stripped) is one of
#: these, or which contains one of DURABLE_ANYWHERE, is a durable record.
DURABLE_FIRST = {
    "record", "insert", "persist", "save", "upsert", "write", "mark",
    "claim", "release", "send", "commit", "execute", "replace", "reconcile",
}
DURABLE_ANYWHERE = {"alert", "notify", "journal", "record"}
EMPTY_CALLS = {"list", "dict", "set", "tuple", "str", "int", "float"}


def import_bindings(tree: ast.AST) -> dict[str, tuple[str, str]]:
    """Map each name an import binds to (module, original name).

    ``from a.b import record_site as _site`` binds ``_site`` to
    ``("a.b", "record_site")``. A call is judged by what it binds to, never by
    the spelling at the call site.
    """
    out: dict[str, tuple[str, str]] = {}
    for n in ast.walk(tree):
        if isinstance(n, ast.ImportFrom):
            mod = "." * n.level + (n.module or "")
            for a in n.names:
                out[a.asname or a.name] = (mod, a.name)
    return out


def _is_foreign(module: str) -> bool:
    """True for a stdlib or third-party module: it can never be the recorder."""
    if module.startswith("."):
        return False
    root = module.split(".")[0]
    return root not in {"src", "scripts", "tests"} and (
        root in sys.stdlib_module_names or root in {"yfinance", "requests", "alpaca"}
    )


def _callee_name(call: ast.Call, binds: dict[str, tuple[str, str]] | None = None) -> str:
    f = call.func
    if isinstance(f, ast.Attribute):
        return f.attr
    if isinstance(f, ast.Name):
        hit = (binds or {}).get(f.id)
        if hit is not None:
            if _is_foreign(hit[0]):
                return ""  # e.g. ``from logging import warning as record_x``
            return hit[1]  # resolved to the real name: ``_site`` -> ``record_site``
        return f.id
    return ""


def is_durable_call(name: str) -> bool:
    toks = [t for t in name.lower().split("_") if t]
    if not toks:
        return False
    if toks[0] in DURABLE_FIRST:
        return True
    return any(t in DURABLE_ANYWHERE for t in toks)


def is_empty_value(node: ast.AST | None) -> bool:
    if node is None:
        return True
    if isinstance(node, ast.Constant):
        return node.value in (None, False, 0, 0.0, "", b"")
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        return not node.elts
    if isinstance(node, ast.Dict):
        return not node.keys
    if isinstance(node, ast.Call) and not node.args and not node.keywords:
        return _callee_name(node) in EMPTY_CALLS
    return False


def is_broad(handler: ast.ExceptHandler) -> bool:
    t = handler.type
    if t is None:
        return True
    names = t.elts if isinstance(t, ast.Tuple) else [t]
    for n in names:
        if isinstance(n, ast.Name) and n.id in BROAD_NAMES:
            return True
        if isinstance(n, ast.Attribute) and n.attr in BROAD_NAMES:
            return True
    return False


def _handler_body_nodes(handler: ast.ExceptHandler):
    """Every node in the handler, not descending into nested defs/lambdas."""
    stack = list(handler.body)
    while stack:
        n = stack.pop()
        yield n
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)):
            continue
        stack.extend(ast.iter_child_nodes(n))


def _swallows_and_returns_empty(
    handler: ast.ExceptHandler, binds: dict[str, tuple[str, str]] | None = None
) -> bool:
    returns_empty = False
    for n in _handler_body_nodes(handler):
        if isinstance(n, ast.Raise):
            return False
        if isinstance(n, ast.Call) and is_durable_call(_callee_name(n, binds)):
            return False
        if isinstance(n, ast.Return) and is_empty_value(n.value):
            returns_empty = True
    return returns_empty


Site = tuple[str, str, str]  #: (module path, enclosing scope, handler source text)


class _Finder(ast.NodeVisitor):
    """Collect every broad handler that swallows and returns empty.

    Each hit is named by its IDENTITY -- path, enclosing def/class and the
    handler's own source text -- never by an ordinal or a line number, so a
    change that removes one offender and adds a different one in the same
    function is still seen as an addition (scripts/guard_reference.py).
    """

    def __init__(
        self, rel: str, scopes: dict[int, str], binds: dict[str, tuple[str, str]] | None = None
    ) -> None:
        self.rel, self.scopes, self.hits, self.binds = rel, scopes, [], binds or {}

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        if is_broad(node) and _swallows_and_returns_empty(node, self.binds):
            scope, src = site_identity(node, self.scopes)
            # Name the def, not the class that happens to hold it: a body
            # moved to a constructed part keeps its identity, while a second
            # copy anywhere still raises the occurrence count and is reported.
            scope = scope.rsplit(".", 1)[-1]
            self.hits.append(((self.rel, scope, src), node.lineno))
        self.generic_visit(node)


def scan_text(rel: str, text: str) -> list[tuple[Site, int]]:
    """Return (site identity, line) for each violation in one module's source text."""
    tree = ast.parse(text, filename=rel)
    f = _Finder(rel, enclosing_scopes(tree), import_bindings(tree))
    f.visit(tree)
    return f.hits


def scan(rel: str) -> list[tuple[Site, int]]:
    """Return (site identity, current line) for each violation in one module."""
    path = ROOT / rel
    if not path.exists():
        return []
    return scan_text(rel, path.read_text())


def violations(modules: tuple[str, ...] | None = None) -> list[tuple[Site, int]]:
    """Silent swallows in the WORKING TREE: every occurrence, with its line."""
    modules = money_modules() if modules is None else modules
    out: list[tuple[Site, int]] = []
    for rel in modules:
        out.extend(scan(rel))
    return out


def trunk_violations(modules: tuple[str, ...] | None = None) -> list[Site]:
    """Silent swallows on ``origin/main``, one identity per occurrence.

    Raises ``ReferenceUnavailable`` when the trunk cannot be read: this guard
    compares and stores nothing, so an unreadable reference is a refusal, never
    a pass. A module absent from the trunk is simply new, not an error.
    """
    modules = money_modules() if modules is None else modules
    sites: list[Site] = []
    for rel, text in trunk_blobs(list(modules)).items():
        try:
            sites.extend(k for k, _ in scan_text(rel, text))
        except SyntaxError as exc:  # trunk file we cannot parse -> cannot compare
            raise ReferenceUnavailable(
                f"cannot parse {rel} as it stands on {TRUNK}: {exc}"
            ) from exc
    return sites


def added(modules: tuple[str, ...] | None = None) -> list[tuple[Site, int, int, int]]:
    """Silent swallows this working tree holds MORE copies of than ``origin/main``.

    Returns ``(site, line, copies_now, copies_on_trunk)``. Identity comparison
    via ``guard_reference.added_sites``: removals never offset an addition.
    """
    modules = money_modules() if modules is None else modules
    now = violations(modules)
    lines: dict[Site, int] = {}
    for site, line in now:
        lines.setdefault(site, line)
    return [
        (site, lines[site], n, was)
        for site, n, was in added_sites([s for s, _ in now], trunk_violations(modules))
    ]


def delta_report(new: list[tuple[Site, int, int, int]]) -> str:
    """Per-file delta lines: 'this file gained N silently-swallowed exceptions'."""
    per_file: dict[str, list[str]] = {}
    for (rel, scope, src), line, n, was in sorted(new):
        head = src.splitlines()[0]
        per_file.setdefault(rel, []).append(
            f"      {rel}::{scope}  (line {line}, {n} here vs {was} on {TRUNK}): {head}"
        )
    return "\n".join(
        f"  {rel}: gained {len(rows)} silently-swallowed exception(s) against {TRUNK}\n"
        + "\n".join(rows)
        for rel, rows in sorted(per_file.items())
    )


FIX_ADVICE = (
    "A swallowed broker error that returns None/False/[] is read by the caller as "
    "'nothing found' (get_current_stop_price -> 'no stop to adjust'). "
    "Fix: re-raise, or record the failure durably before returning (event journal, "
    "insert_*/save_* row, send_owner_alert / _alert_owner_*, record_*). "
    "A logger call is NOT a record. There is no baseline to add it to."
)


def main(argv: list[str] | None = None) -> int:
    try:
        new = added()
    except ReferenceUnavailable as exc:
        print(f"REFUSING: {exc}", file=sys.stderr)
        return 2
    if new:
        print(
            f"NEW silent swallows on money paths against {TRUNK}:\n"
            + delta_report(new)
            + "\n"
            + FIX_ADVICE,
            file=sys.stderr,
        )
        return 1
    print(f"silent-swallow guard: no money-path module gained a silent swallow against {TRUNK}.")
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())
