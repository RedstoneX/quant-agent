"""Guard: no module-level mutable accumulator in production source.

Owner mandate: stored bookkeeping is ripped out; counting is a COUNTED ROW,
computed at check time, never a running total held in process memory.

Refused: a module-level (not class, not function) name bound to a mutable
container (dict/list/set literal or comprehension, or a call to dict, list,
set, defaultdict, Counter, OrderedDict, deque) that the SAME module mutates
(subscript store/augassign/del, mutating method call, `+=`, or rebinding via
`global`). A mutable only ever read is a constant table and passes. Tuples,
frozensets and immutable literals pass.

Covers: `src/` (every *.py). Out of scope: tests/, scripts/, ops/, frontend/.

Frozen list: `module_accumulator_frozen.txt` holds offender VARIABLE NAMES
only (no paths, so relocating a file stays legal), computed from trunk when
this guard was built. It may only shrink: an offender whose name is not
listed (or listed fewer times) is refused.

Not caught (gameable): mutation from ANOTHER module via import; mutable
state hidden in a class attribute, a function default arg, a closure or an
`lru_cache`; a new offender sharing a name with a frozen one; mutation
through an alias (`d = _REG; d[k] = v`); `globals()[...]`; sqlite/file state.
"""
from __future__ import annotations

import ast
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCANNED = ("src",)
FROZEN = Path(__file__).with_name("module_accumulator_frozen.txt")

_CTORS = {"dict", "list", "set", "defaultdict", "Counter", "OrderedDict", "deque"}
_MUTATORS = {
    "append", "extend", "insert", "update", "add", "setdefault", "pop",
    "popitem", "clear", "remove", "discard", "appendleft", "extendleft",
    "popleft", "subtract", "sort", "reverse", "difference_update",
    "intersection_update", "symmetric_difference_update",
}


def _is_mutable_value(node: ast.AST | None) -> bool:
    if isinstance(node, (ast.Dict, ast.List, ast.Set, ast.DictComp,
                         ast.ListComp, ast.SetComp)):
        return True
    if isinstance(node, ast.Call):
        f = node.func
        name = f.id if isinstance(f, ast.Name) else (
            f.attr if isinstance(f, ast.Attribute) else None)
        return name in _CTORS
    return False


def _root(node: ast.AST) -> str | None:
    while isinstance(node, (ast.Subscript, ast.Attribute)):
        node = node.value
    return node.id if isinstance(node, ast.Name) else None


def _module_mutables(tree: ast.Module) -> set[str]:
    out: set[str] = set()
    for st in tree.body:
        if isinstance(st, ast.Assign) and _is_mutable_value(st.value):
            out.update(t.id for t in st.targets if isinstance(t, ast.Name))
        elif (isinstance(st, ast.AnnAssign) and isinstance(st.target, ast.Name)
              and _is_mutable_value(st.value)):
            out.add(st.target.id)
    out.discard("__all__")
    return out


def _local_names(fn: ast.AST) -> set[str]:
    """Names a function binds locally (shadowing the module name)."""
    a = fn.args
    names = {x.arg for x in a.posonlyargs + a.args + a.kwonlyargs}
    names |= {x.arg for x in (a.vararg, a.kwarg) if x}
    declared_global: set[str] = set()
    for n in ast.walk(fn):
        if isinstance(n, ast.Global):
            declared_global.update(n.names)
        elif isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store):
            names.add(n.id)
    return names - declared_global


def _mutated_names(tree: ast.Module, candidates: set[str]) -> set[str]:
    hit: set[str] = set()

    def visit(node: ast.AST, shadow: frozenset[str]) -> None:
        live = candidates - shadow

        def note(target: ast.AST, direct_ok: bool) -> None:
            r = _root(target)
            if r in live and (direct_ok or not isinstance(target, ast.Name)):
                hit.add(r)

        if isinstance(node, ast.Assign):
            for t in node.targets:
                note(t, direct_ok=False)
        elif isinstance(node, ast.AugAssign):
            note(node.target, direct_ok=True)
        elif isinstance(node, ast.Delete):
            for t in node.targets:
                note(t, direct_ok=False)
        elif (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
              and node.func.attr in _MUTATORS):
            r = _root(node.func.value)
            if r in live:
                hit.add(r)
        if isinstance(node, ast.Global):
            # a `global X` that rebinds X is handled below via Store walk
            pass
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                visit_fn(child, shadow)
            else:
                visit(child, shadow)

    def visit_fn(fn: ast.AST, outer: frozenset[str]) -> None:
        shadow = outer | frozenset(_local_names(fn))
        # `global X` + rebinding X inside the function is itself accumulation
        globals_here: set[str] = set()
        for n in ast.walk(fn):
            if isinstance(n, ast.Global):
                globals_here.update(n.names)
        for n in ast.walk(fn):
            if (isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)
                    and n.id in globals_here and n.id in candidates):
                hit.add(n.id)
        for child in fn.body:
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                visit_fn(child, shadow)
            else:
                visit(child, shadow)

    for st in tree.body:
        if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef)):
            visit_fn(st, frozenset())
        else:
            visit(st, frozenset())
    return hit


def offenders_in_source(source: str) -> list[str]:
    tree = ast.parse(source)
    cands = _module_mutables(tree)
    return sorted(_mutated_names(tree, cands)) if cands else []


def scan_tree(root: Path = ROOT) -> Counter:
    found: Counter = Counter()
    for d in SCANNED:
        for p in sorted((root / d).rglob("*.py")):
            try:
                names = offenders_in_source(p.read_text(encoding="utf-8"))
            except SyntaxError:
                continue
            found.update(names)
    return found


def _frozen() -> Counter:
    lines = [ln.strip() for ln in FROZEN.read_text().splitlines()]
    return Counter(ln for ln in lines if ln and not ln.startswith("#"))


def test_no_new_module_level_accumulator():
    extra = scan_tree() - _frozen()
    assert not extra, (
        f"module-level mutable accumulator(s) in src/: {sorted(extra)}. "
        "Stored bookkeeping is banned: write a counted row / compute at "
        "check time. Do NOT add to the frozen list; it may only shrink."
    )


def test_frozen_list_only_shrinks():
    stale = _frozen() - scan_tree()
    assert not stale, (
        f"frozen offenders no longer present: {sorted(stale)}. Delete those "
        "lines from module_accumulator_frozen.txt so the list shrinks."
    )


# --- prove it bites -------------------------------------------------------

def test_bites_on_the_banned_pattern():
    src = (
        "_STATS = {}\n"
        "def record(k, v):\n"
        "    s = _STATS.setdefault(k, {'n': 0})\n"
        "    _STATS[k]['n'] += 1\n"
        "def reset():\n"
        "    _STATS.clear()\n"
    )
    assert offenders_in_source(src) == ["_STATS"]


def test_bites_on_each_mutation_shape():
    for body in ("_X.append(1)", "_X[0] = 1", "global _X; _X += [1]", "_X.update({})",
                 "_X.add(1)", "del _X[0]", "_X[0][1] = 2"):
        src = f"_X = []\ndef f():\n    {body}\n"
        assert offenders_in_source(src) == ["_X"], body
    assert offenders_in_source(
        "_X = Counter()\ndef f():\n    global _X\n    _X = Counter()\n"
    ) == ["_X"]


def test_read_only_constants_pass():
    src = (
        "from typing import Final\n"
        "TABLE = {'a': 1}\n"
        "ORDER: Final = ['x', 'y']\n"
        "FS = frozenset({1})\n"
        "T = (1, 2)\n"
        "N = 5\n"
        "def f(k):\n"
        "    return TABLE[k], list(ORDER), 1 in FS\n"
    )
    assert offenders_in_source(src) == []


def test_local_shadow_and_class_scope_pass():
    src = (
        "_cache = {}\n"
        "def f():\n"
        "    _cache = {}\n"
        "    _cache['a'] = 1\n"
        "    return _cache\n"
        "class C:\n"
        "    reg = {}\n"
        "    def g(self):\n"
        "        self.reg['a'] = 1\n"
    )
    assert offenders_in_source(src) == []
