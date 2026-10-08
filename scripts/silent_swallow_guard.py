"""Silent-swallow guard against a fixed, committed allow-list.

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

The limit is FIXED: ``config/check_allowlists/code_silent_swallow.txt`` lists
every existing swallow by IDENTITY (path, enclosing scope, handler source text),
never a total or an ordinal. The guard fails on a swallow not in the list and on
a listed entry that no longer occurs. It reads no git ref.

Run it directly: ``python -m scripts.silent_swallow_guard``.
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

from scripts.money_modules import derive as derive_money_modules
from scripts.swallow_resolver import Resolver, import_bindings  # noqa: F401 -- re-exported
from scripts.check_allowlist import ALLOWLIST_DIR, compare, report
from scripts.guard_reference import ROOT, ReferenceUnavailable, enclosing_scopes, site_identity

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


def _callee_name(call: ast.Call, resolver: Resolver | None = None) -> str:
    """The recorder name a call binds to, or "" when it binds to none.

    Without a resolver there is no module to resolve against, so nothing can
    be identified as a recorder; a name match alone is never enough.
    """
    if resolver is None:
        resolver = Resolver(ast.Module(body=[], type_ignores=[]), is_durable_call)
    return resolver.callee_name(call)


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
        # Builtin constructors are judged by spelling: ``list()`` is empty
        # whatever a module imports; only recorders need identity.
        return isinstance(node.func, ast.Name) and node.func.id in EMPTY_CALLS
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
    handler: ast.ExceptHandler, resolver: Resolver | None = None
) -> bool:
    returns_empty = False
    for n in _handler_body_nodes(handler):
        if isinstance(n, ast.Raise):
            return False
        if isinstance(n, ast.Call) and is_durable_call(_callee_name(n, resolver)):
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
        self, rel: str, scopes: dict[int, str], resolver: Resolver | None = None
    ) -> None:
        self.rel, self.scopes, self.hits, self.resolver = rel, scopes, [], resolver

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        if is_broad(node) and _swallows_and_returns_empty(node, self.resolver):
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
    f = _Finder(rel, enclosing_scopes(tree), Resolver(tree, is_durable_call))
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


ALLOWLIST = ALLOWLIST_DIR / "code_silent_swallow.txt"


def check(
    modules: tuple[str, ...] | None = None, allowlist: Path = ALLOWLIST
) -> tuple[list[str], list[str]]:
    """``(unlisted, stale)``: swallows missing from the fixed list, and listed ones now gone."""
    return compare([site for site, _ in violations(modules)], allowlist)


def delta_report(unlisted: list[str], stale: list[str], allowlist: Path = ALLOWLIST) -> str:
    return report(unlisted, stale, allowlist)


FIX_ADVICE = (
    "A swallowed broker error that returns None/False/[] is read by the caller as "
    "'nothing found' (get_current_stop_price -> 'no stop to adjust'). "
    "Fix: re-raise, or record the failure durably before returning (event journal, "
    "insert_*/save_* row, send_owner_alert / _alert_owner_*, record_*). "
    "A logger call is NOT a record. There is no baseline to add it to."
)


def main(argv: list[str] | None = None) -> int:
    try:
        unlisted, stale = check()
    except ReferenceUnavailable as exc:  # the exchange SDK needed to derive scope
        print(f"REFUSING: {exc}", file=sys.stderr)
        return 2
    if unlisted or stale:
        print(
            "Silent swallows on money paths differ from " + ALLOWLIST.name + ":\n"
            + delta_report(unlisted, stale)
            + ("\n" + FIX_ADVICE if unlisted else ""),
            file=sys.stderr,
        )
        return 1
    print("silent-swallow guard: money-path swallows match the fixed allow-list exactly.")
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())
