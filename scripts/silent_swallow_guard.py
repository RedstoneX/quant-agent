"""Silent-swallow guard: a broad ``except`` on a money path may not return an
empty value while recording nothing durable.

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

Scope: the money-touching modules only (``MONEY_MODULES``). A guard over all
949 handlers would be noise and would be deleted within a week.

Baseline lives in tests/silent_swallow_baseline.json and may only SHRINK:
``PYTHONPATH=. .venv/bin/python -m scripts.silent_swallow_guard --shrink-baseline``
after fixing a handler. Never add an entry; fix the handler instead.
"""
from __future__ import annotations

import ast
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BASELINE_PATH = ROOT / "tests" / "silent_swallow_baseline.json"

#: Money-touching modules: anything that places, amends, cancels or sizes an
#: order, or decides whether a position keeps its protection. Order of
#: priority: the broker adapter, then the stages that act on its answers.
MONEY_MODULES: tuple[str, ...] = (
    "src/execution/broker.py",            # every broker call
    "src/execution/stop_repair.py",       # re-places missing stops
    "src/execution/stop_records.py",      # what the desk believes its stops are
    "src/execution/scale_in.py",          # adds to positions
    "src/execution/cash_sweep.py",        # moves cash
    "src/execution/exit_path_records.py", # the record an exit leaves behind
    "src/pipeline_protection.py",         # protective stops
    "src/pipeline_exits.py",              # sells
    "src/pipeline_entry_orders.py",       # buys
    "src/pipeline_delever.py",            # forced reductions
    "src/pipeline_rotation_exec.py",      # prune-and-replace
    "src/pipeline_sizing.py",             # how much
    "src/pipeline_stages.py",             # the execution stage itself
)

BROAD_NAMES = {"Exception", "BaseException"}

#: A callee whose first name-token (leading underscores stripped) is one of
#: these, or which contains one of DURABLE_ANYWHERE, is a durable record.
DURABLE_FIRST = {
    "record", "insert", "persist", "save", "upsert", "write", "mark",
    "claim", "release", "send", "commit", "execute", "replace", "reconcile",
}
DURABLE_ANYWHERE = {"alert", "notify", "journal", "record"}
EMPTY_CALLS = {"list", "dict", "set", "tuple", "str", "int", "float"}


def _callee_name(call: ast.Call) -> str:
    f = call.func
    if isinstance(f, ast.Attribute):
        return f.attr
    if isinstance(f, ast.Name):
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


def _swallows_and_returns_empty(handler: ast.ExceptHandler) -> bool:
    returns_empty = False
    for n in _handler_body_nodes(handler):
        if isinstance(n, ast.Raise):
            return False
        if isinstance(n, ast.Call) and is_durable_call(_callee_name(n)):
            return False
        if isinstance(n, ast.Return) and is_empty_value(n.value):
            returns_empty = True
    return returns_empty


class _Finder(ast.NodeVisitor):
    def __init__(self, rel: str) -> None:
        self.rel, self.scope, self.hits, self.ordinal = rel, [], [], {}

    def _enter(self, node):
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()

    visit_FunctionDef = visit_AsyncFunctionDef = visit_ClassDef = _enter

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        if is_broad(node) and _swallows_and_returns_empty(node):
            qual = ".".join(self.scope) or "<module>"
            k = (self.rel, qual)
            self.ordinal[k] = self.ordinal.get(k, 0) + 1
            self.hits.append((f"{self.rel}::{qual}#{self.ordinal[k]}", node.lineno))
        self.generic_visit(node)


def scan(rel: str) -> list[tuple[str, int]]:
    """Return (stable key, current line) for each violation in one module."""
    path = ROOT / rel
    if not path.exists():
        return []
    f = _Finder(rel)
    f.visit(ast.parse(path.read_text(), filename=rel))
    return f.hits


def violations(modules: tuple[str, ...] = MONEY_MODULES) -> dict[str, int]:
    out: dict[str, int] = {}
    for rel in modules:
        out.update(scan(rel))
    return out


def load_baseline() -> set[str]:
    if not BASELINE_PATH.exists():
        return set()
    return set(json.loads(BASELINE_PATH.read_text())["keys"])


def _write(keys: set[str]) -> None:
    BASELINE_PATH.write_text(json.dumps({
        "_comment": (
            "Silent-swallow handlers (broad except -> empty return, nothing durable recorded) "
            "on money paths when the guard was introduced. This list may only SHRINK: run "
            "`PYTHONPATH=. .venv/bin/python -m scripts.silent_swallow_guard --shrink-baseline` "
            "after fixing one. Never add a key here; record the failure durably instead."),
        "keys": sorted(keys),
    }, indent=1) + "\n")


def shrink_baseline() -> None:
    """Rewrite the baseline to the intersection with today's violations. Never grows it."""
    _write(load_baseline() & set(violations()))


if __name__ == "__main__":
    if "--shrink-baseline" in sys.argv:
        shrink_baseline()
    elif "--seed-baseline" in sys.argv:  # one-time seeding only; the test forbids growth by review
        _write(set(violations()))
    else:
        for k, ln in sorted(violations().items()):
            print(f"{k}  (line {ln})")
    print(f"{len(violations())} silent swallows on money paths; baseline holds {len(load_baseline())}.")
