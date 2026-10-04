"""Mechanical guard: no function in `src/` may read a name nothing defines.

Why this exists: `update_open_take_profit` shipped two refusal branches that
called a bare `_log` the ledger module never defined, so a live
order-management refusal raised `NameError` instead of refusing. Nothing
caught it, because an undefined global inside a function body is legal at
import time and only fails when that branch runs -- and a refusal branch is
exactly the branch tests rarely drive. This guard reads every module under
`src/` and fails on a load of a name that no enclosing scope, no module-level
binding and no builtin provides. There is no baseline and no exemption list:
the stored one drained to empty and was deleted, because a recorded offence
outlives the code that earned it and then reds every unrelated change until
someone notices. Zero offenders is computed from the tree on every run.
"""
from __future__ import annotations

import ast
import builtins
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"

_BUILTINS = set(dir(builtins)) | {"__file__", "__name__", "__doc__", "__spec__", "__package__", "__loader__", "__builtins__", "__debug__", "__path__", "WindowsError", "reveal_type"}


def _bound_names(node: ast.AST) -> set[str]:
    """Every name a scope binds, without descending into nested scopes."""
    out: set[str] = set()
    args = getattr(node, "args", None)
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)) and args is not None:
        for a in [*args.posonlyargs, *args.args, *args.kwonlyargs]:
            out.add(a.arg)
        for a in (args.vararg, args.kwarg):
            if a is not None:
                out.add(a.arg)
    stack = _own_children(node)
    while stack:
        cur = stack.pop()
        if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            out.add(cur.name)
            continue
        if isinstance(cur, ast.Lambda):
            continue
        if isinstance(cur, ast.Name) and isinstance(cur.ctx, (ast.Store, ast.Del)):
            out.add(cur.id)
        elif isinstance(cur, (ast.Import, ast.ImportFrom)):
            for alias in cur.names:
                out.add(alias.asname or alias.name.split(".")[0])
        elif isinstance(cur, (ast.Global, ast.Nonlocal)):
            out.update(cur.names)
        elif isinstance(cur, ast.ExceptHandler) and cur.name:
            out.add(cur.name)
        stack.extend(ast.iter_child_nodes(cur))
    return out


def _own_children(node: ast.AST) -> list:
    """Children evaluated INSIDE this scope.

    A scope node's own decorators and argument defaults are evaluated in the
    ENCLOSING scope, so they are excluded here and hoisted by the parent.
    """
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        kids = [c for c in ast.iter_child_nodes(node)
                if c not in getattr(node, "decorator_list", [])]
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            defaults = [*node.args.defaults, *[d for d in node.args.kw_defaults if d is not None]]
            kids = [c for c in kids if c not in defaults]
        return kids
    return list(ast.iter_child_nodes(node))


def _hoisted(node: ast.AST) -> list:
    """What a nested scope evaluates in THIS scope: decorators and defaults."""
    out = list(getattr(node, "decorator_list", []))
    args = getattr(node, "args", None)
    if args is not None:
        out.extend(args.defaults)
        out.extend([d for d in args.kw_defaults if d is not None])
    return out


def _scope_nodes(node: ast.AST):
    """Nested scopes directly inside this one."""
    stack = _own_children(node)
    while stack:
        cur = stack.pop()
        if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda, ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
            yield cur
            continue
        stack.extend(ast.iter_child_nodes(cur))


def _loads(node: ast.AST):
    """Name loads in this scope, skipping nested scopes."""
    stack = _own_children(node)
    while stack:
        cur = stack.pop()
        if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda, ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
            stack.extend(_hoisted(cur))
            continue
        if isinstance(cur, ast.Name) and isinstance(cur.ctx, ast.Load):
            yield cur
        stack.extend(ast.iter_child_nodes(cur))


def _walk(node: ast.AST, visible: set[str], out: list[tuple[int, str]]) -> None:
    here = _bound_names(node)
    if isinstance(node, ast.ClassDef):
        inner_visible = visible | here
        child_visible = visible          # a class body is not an enclosing scope
    else:
        inner_visible = visible | here
        child_visible = inner_visible
    for name_node in _loads(node):
        if name_node.id not in inner_visible and name_node.id not in _BUILTINS:
            out.append((name_node.lineno, name_node.id))
    for child in _scope_nodes(node):
        _walk(child, child_visible, out)


def _offenders(path: Path) -> list[tuple[int, str]]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for n in ast.walk(tree):
        if isinstance(n, ast.ImportFrom) and any(a.name == "*" for a in n.names):
            return []  # a star-import can define anything; nothing to assert
    module_names = _bound_names(tree)
    found: list[tuple[int, str]] = []
    for child in _scope_nodes(tree):
        _walk(child, module_names, found)
    for name_node in _loads(tree):
        if name_node.id not in module_names and name_node.id not in _BUILTINS:
            found.append((name_node.lineno, name_node.id))
    return found


def test_no_undefined_name_is_read_anywhere_under_src() -> None:
    assert SRC.is_dir(), f"guard cannot read its reference tree: {SRC} is not a directory"
    hits: list[str] = []
    scanned = 0
    for path in sorted(SRC.rglob("*.py")):
        rel = str(path.relative_to(SRC.parent))
        scanned += 1
        for lineno, name in _offenders(path):
            hits.append(f"{rel}:{lineno} reads undefined name {name!r}")
    assert scanned, f"guard scanned no modules under {SRC}; its reference is unreadable, not clean"
    assert not hits, (
        "A function reads a name nothing defines; it will raise NameError when "
        "that branch runs:\n" + "\n".join(sorted(hits))
    )
