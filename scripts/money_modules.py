"""Which modules are money paths -- DERIVED from source at check time, stored nowhere.

Definition (one line): a module is a money path when it holds a function from
which an exchange write is reachable through the call graph, or when a function
in such a module calls into it (the sizes, limits, reads and records a writer
acts on -- a read that swallows and returns None is how a stop went unadjusted).

"Exchange write" is not a hand-written verb list either: it is every method of
the installed SDK trading client whose body issues an HTTP POST/PATCH/DELETE/PUT
(``alpaca.trading.client.TradingClient``, read with ``inspect``). From those
seeds the closure walks every ``def`` under ``src/``: a def that calls a writer
is a writer. Calls are resolved MODULE-AWARE, never by bare name, so a generic
``run``/``main`` defined in seventeen modules cannot leak money status into the
whole tree: ``name(...)`` resolves to a def of that name in the same module or
one it imports by name; ``obj.name(...)`` resolves to a def of that name in the
same module, in a module it imports, or -- when exactly one module anywhere
defines that name -- to that module.

The previous rule was a hand-edited tuple of 23 paths with "lifted from" notes
(docs/GUARDS_WITHOUT_STORED_STATE.md). Measured 2026-10-04 against this
derivation it had drifted: it never named ``protected_sell`` (calls the SDK's
``close_position``), ``stage_execution`` (submits orders), ``order_idempotency``
(submits orders), ``pending_stop_drain`` or ``stop_shift`` (re-places stops).
A list a human must remember to extend is silent on exactly the module the
last refactor created. If the SDK cannot be imported the derivation REFUSES
(``ReferenceUnavailable``); it never falls back to a pinned set.
"""
from __future__ import annotations

import ast
import inspect
import subprocess
import textwrap
from collections import Counter
from pathlib import Path

from scripts.guard_reference import ROOT, ReferenceUnavailable

HTTP_WRITE_VERBS = frozenset({"post", "patch", "delete", "put"})
#: ``None`` means every tracked ``.py`` outside ``tests/`` -- root ``main.py``, ``ops/``,
#: ``scripts/`` included. Loading ``src/`` alone missed 31 money modules [measured 2026-10-05].
SOURCE_DIR: str | None = None


def sdk_write_methods() -> frozenset[str]:
    """Names of the trading client's methods whose body does an HTTP write."""
    try:
        from alpaca.trading.client import TradingClient  # noqa: WPS433 - derived at check time
    except Exception as exc:  # pragma: no cover - environment without the SDK
        raise ReferenceUnavailable(f"cannot import the exchange SDK to derive its writes: {exc}")
    out: set[str] = set()
    for name, fn in inspect.getmembers(TradingClient, inspect.isfunction):
        try:
            tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
        except (OSError, SyntaxError, TypeError):  # pragma: no cover - no source for a builtin
            continue
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in HTTP_WRITE_VERBS
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "self"
            ):
                out.add(name)
    if not out:
        raise ReferenceUnavailable("the exchange SDK exposed no HTTP-writing method; refusing")
    return frozenset(out)


Call = tuple[str, str | None]  #: (callee name, receiver name or None for a bare call)


class _Module:
    def __init__(self, rel: str, tree: ast.AST) -> None:
        self.rel = rel
        self.defs: dict[str, set[Call]] = {}
        self.bodies: dict[str, list[ast.AST]] = {}
        self.imported_names: dict[str, str] = {}  # local name -> module path it came from
        self.imported_modules: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                mod = _module_rel(node.module or "", rel if node.level else None, node.level)
                self.imported_modules.add(mod)
                for alias in node.names:
                    self.imported_names[alias.asname or alias.name] = mod
                    self.imported_modules.add(mod[:-3] + "/" + alias.name + ".py")  # `from pkg import module`
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    self.imported_modules.add(_module_rel(alias.name))
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                calls = self.defs.setdefault(node.name, set())
                self.bodies.setdefault(node.name, []).append(node)
                for c in ast.walk(node):
                    if isinstance(c, ast.Call):
                        call = _call_ref(c)
                        if call:
                            calls.add(call)


def _module_rel(dotted: str, from_rel: str | None = None, level: int = 0) -> str:
    """Dotted import -> repo-relative path; a relative import resolves against the importer."""
    if from_rel is not None and level:
        base = from_rel.rsplit("/", level)[0]
        return base + ("/" + dotted.replace(".", "/") if dotted else "") + ".py"
    return dotted.replace(".", "/") + ".py"


def _call_ref(call: ast.Call) -> Call | None:
    f = call.func
    if isinstance(f, ast.Name):
        return (f.id, None)
    if isinstance(f, ast.Attribute):
        recv = f.value.id if isinstance(f.value, ast.Name) else "<expr>"
        return (f.attr, recv)
    return None


def _source_paths(root: Path, source_dir: str | None) -> list[Path]:
    """Modules to load: git's tracked production ``.py`` set, or one named directory for tests."""
    if source_dir is not None:
        return sorted((root / source_dir).rglob("*.py"))
    out = subprocess.run(["git", "-C", str(root), "ls-files", "--", "*.py"],
                         capture_output=True, text=True)
    if out.returncode or not out.stdout.strip():
        raise ReferenceUnavailable(f"git ls-files listed no sources under {root}; refusing")
    return [root / p for p in sorted(out.stdout.splitlines())
            if p.endswith(".py") and p.split("/", 1)[0] != "tests"]


def _load(root: Path, source_dir: str | None) -> dict[str, _Module]:
    mods: dict[str, _Module] = {}
    for path in _source_paths(root, source_dir):
        rel = path.relative_to(root).as_posix()
        try:
            tree = ast.parse(path.read_text(), filename=rel)
        except SyntaxError as exc:
            raise ReferenceUnavailable(f"cannot parse {rel} to derive money modules: {exc}")
        mods[rel] = _Module(rel, tree)
    return mods


def _candidates(
    mod: _Module, call: Call, mods: dict[str, _Module], owners: dict[str, list[str]]
) -> list[tuple[str, str]]:
    """Every def one call site may land on, resolved module-aware.

    A bare ``name(...)`` lands on the same module's def or the one it imported
    by name. ``obj.name(...)`` lands on the defs of that name in the same module
    and in the modules this one imports; only when none of those define it does
    it fall back to every def of that name in the tree. The caller treats a
    call as write-reaching only when ALL its candidates are writers, so a
    generic name shared by writers and non-writers never carries status.
    """
    name, recv = call
    same = [(mod.rel, name)] if name in mod.defs else []
    if recv is None:
        src = mod.imported_names.get(name)
        if src in mods and name in mods[src].defs:
            return same + [(src, name)]
        # no local def and no import: the name was bound by assignment
        # (``size = pipeline._size_shares``); fall back to every def of that name
        return same or [(m, name) for m in owners.get(name, ())]
    found = same + [
        (m, name) for m in mod.imported_modules if m in mods and m != mod.rel and name in mods[m].defs
    ]
    return found or [(m, name) for m in owners.get(name, ())]


def _reaches(call: Call, mod: _Module, writers: set, seeds: frozenset[str], mods, owners, within: str = "") -> bool:
    """True when every def this call can land on is a writer (or it is an SDK write).

    The calling def itself is never a candidate: a shim ``def submit_order(self):
    return self._desk().submit_order()`` lands on the part, not on itself.
    """
    if call[0] in seeds:
        return True
    cands = [c for c in _candidates(mod, call, mods, owners) if c != (mod.rel, within)]
    return bool(cands) and all(c in writers for c in cands)


def derive(
    root: Path = ROOT, seeds: frozenset[str] | None = None, source_dir: str | None = SOURCE_DIR
) -> tuple[str, ...]:
    """Every tracked production module (or one under ``source_dir``) that is a money path."""
    seeds = sdk_write_methods() if seeds is None else seeds
    mods = _load(root, source_dir)
    owners: dict[str, list[str]] = {}
    for m in mods.values():
        for d in m.defs:
            owners.setdefault(d, []).append(m.rel)
    writers: set[tuple[str, str]] = set()
    changed = True
    while changed:
        changed = False
        for mod in mods.values():
            for d, calls in mod.defs.items():
                if (mod.rel, d) in writers:
                    continue
                if any(_reaches(c, mod, writers, seeds, mods, owners, d) for c in calls):
                    writers.add((mod.rel, d))
                    changed = True
    money = {m for m, _ in writers}
    for rel in list(money):  # one hop: everything a writer module acts on
        mod = mods[rel]
        for calls in mod.defs.values():
            for call in calls:
                for m, _ in _candidates(mod, call, mods, owners):
                    money.add(m)
    return tuple(sorted(money))


def explain(root: Path = ROOT) -> str:
    """Human summary: how many modules, how many seeds."""
    seeds = sdk_write_methods()
    mods = derive(root, seeds)
    return f"{len(mods)} money modules derived from {len(seeds)} SDK write methods"


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    for rel in derive():
        print(rel)
