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

Nothing is stored. At check time each src module is compared with the SAME
module on `origin/main` (via scripts.guard_reference, as the cram and size
ratchets do): only an accumulator name the working copy has MORE of than the
trunk copy is refused. A file absent from the trunk is entirely new. If the
trunk cannot be read the guard raises ReferenceUnavailable -- it never reads
a missing reference as "no offenders on trunk".

Not caught (gameable): mutation from ANOTHER module via import; mutable
state hidden in a class attribute, a function default arg, a closure or an
`lru_cache`; a new accumulator that replaces a same-named trunk one in the same module; mutation
through an alias (`d = _REG; d[k] = v`); `globals()[...]`; sqlite/file state.
"""
from __future__ import annotations

import ast
from collections import Counter
from pathlib import Path

import pytest

from scripts import guard_reference
from scripts.guard_reference import ReferenceUnavailable

ROOT = Path(__file__).resolve().parent.parent
SCANNED = ("src",)

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


def _scan(sources: dict[str, str]) -> dict[str, Counter]:
    out: dict[str, Counter] = {}
    for path, text in sources.items():
        try:
            out[path] = Counter(offenders_in_source(text))
        except SyntaxError:
            continue
    return out


def working_sources() -> dict[str, str]:
    paths = [p for p in guard_reference.working_paths("*.py")
             if p.split("/", 1)[0] in SCANNED]
    return {p: (guard_reference.ROOT / p).read_text(encoding="utf-8")
            for p in paths}


def new_accumulators(work: dict[str, str] | None = None) -> list[str]:
    """Accumulators the working tree has beyond the same module's trunk copy."""
    work = working_sources() if work is None else work
    trunk = guard_reference.trunk_blobs(sorted(work))  # raises if unreadable
    now, before = _scan(work), _scan(trunk)
    bad = []
    for path, names in sorted(now.items()):
        for name in sorted(names - before.get(path, Counter())):
            bad.append(f"{path}: {name}")
    return bad


def test_no_new_module_level_accumulator_against_the_trunk():
    bad = new_accumulators()
    assert not bad, (
        "New module-level mutable accumulator(s) vs origin/main:\n"
        + "\n".join(bad)
        + "\nStored bookkeeping is banned: write a counted row / compute at "
        "check time."
    )


def test_trunk_accumulators_are_actually_read():
    """The trunk has pre-existing accumulators; seeing none would mean the
    reference read is empty and the guard passes vacuously."""
    paths = [p for p in guard_reference.trunk_paths(".py")
             if p.split("/", 1)[0] in SCANNED]
    seen = sum(sum(c.values())
               for c in _scan(guard_reference.trunk_blobs(paths)).values())
    assert seen > 0, "no accumulators seen on origin/main at all"


def test_new_file_is_wholly_new_and_preexisting_passes(monkeypatch):
    old = "_A = {}\ndef f():\n    _A['k'] = 1\n"
    monkeypatch.setattr(guard_reference, "trunk_blobs",
                        lambda paths: {"src/a.py": old})
    assert new_accumulators({"src/a.py": old}) == []
    assert new_accumulators({"src/a.py": old, "src/b.py": old}) == ["src/b.py: _A"]


def test_it_refuses_when_the_trunk_cannot_be_read(tmp_path, monkeypatch):
    import subprocess
    repo = tmp_path / "norepo"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "m.py").write_text("_A = []\ndef f():\n    _A.append(1)\n")
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    subprocess.run(["git", "add", "src/m.py"], cwd=repo, check=True)
    subprocess.run(["git", "-c", "user.email=t@example.invalid", "-c",
                    "user.name=t", "commit", "-qm", "base"], cwd=repo, check=True)
    monkeypatch.setattr(guard_reference, "ROOT", Path(repo))
    with pytest.raises(ReferenceUnavailable):
        new_accumulators()


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
