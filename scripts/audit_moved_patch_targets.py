"""Audit every test patch aimed at ``src.pipeline`` / ``src.pipeline_stages``.

Item 210 criterion 3. The file split moves functions out of the two huge
modules. A test doing ``patch("src.pipeline.X")`` rebinds the name ``X`` in
``src.pipeline`` only. If the code that calls ``X`` has moved to another
module (which holds its own binding), the mock is never invoked, the real
function runs, and the test passes while testing nothing.

Every patch target is classified, by AST, into exactly one bucket:

LIVE      the patched module itself still loads ``X`` at run time (a
          ``Name`` load of ``X`` somewhere in its own body, outside the
          import line), so the patch reaches at least one real call site.
MIRRORED  the patched module never loads ``X`` itself, but it installs a
          write-through ``__setattr__`` mirror that forwards the assignment
          to a split-out module which holds a binding for ``X``, so the
          patch still reaches the moved code.
REEXPORT  the patched module merely binds ``X`` (import, assignment or lazy
          ``__getattr__``) without loading it itself and without a mirror
          that forwards it anywhere.  THE PATCH SILENTLY NO-OPS.
MISSING   the patched module has no binding for ``X`` at all; the patch
          raises ``AttributeError`` at test time, which is loud, not silent.

Nothing is inferred from a name's shape. Bindings and loads come from the
module's AST; the mirror's coverage comes from the AST of the modules the
mirror forwards to. Dotted targets deeper than one attribute
(``src.pipeline.TradingPipeline.run``) are classified by their FIRST
attribute: a patch on a class attribute lands on the class object itself and
is found by normal attribute lookup wherever the class is used.

Patch forms recognised: ``patch("a.b.X")`` (any alias of ``unittest.mock.patch``),
``patch.object(<module alias>, "X")`` and ``monkeypatch.setattr("a.b.X", ...)``.
Also reported, without changing the bucket: split-out modules that load the
name through their own unmirrored binding or a function-local ``from ... import``
(the patch cannot reach those call sites, whatever the bucket says).

Usage: ``python -m scripts.audit_moved_patch_targets [--json] [--root DIR]``.
Exit status is 0 regardless; the guard test consumes the JSON.
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path

AUDITED_MODULES = ("src.pipeline", "src.pipeline_stages")


@dataclass(frozen=True)
class PatchTarget:
    test_file: str
    lineno: int
    module: str
    name: str
    raw: str


@dataclass(frozen=True)
class Finding:
    test_file: str
    lineno: int
    module: str
    name: str
    classification: str
    binding: str
    reason: str
    leaks_to: tuple[str, ...]


# --------------------------------------------------------------------------
# Module analysis


class _ModuleFacts:
    """What a module binds at top level and which names its body loads."""

    def __init__(self, dotted: str, root: Path):
        self.dotted = dotted
        self.path = root / (dotted.replace(".", "/") + ".py")
        self.tree = ast.parse(self.path.read_text(), filename=str(self.path))
        self.bindings: dict[str, str] = {}
        self.lazy: dict[str, str] = {}
        self.loads: set[str] = set()
        self.mirror_targets: set[str] = set()
        # Function-local ``from x import name`` lines: a call through such a
        # binding is reached by NO module-level patch except one on ``x``.
        self.local_imports: dict[str, list[tuple[str, int]]] = defaultdict(list)
        self._scan()

    def _scan(self) -> None:
        for node in self.tree.body:
            self._bind_top(node)
        # Lazy ``__getattr__`` maps: ``_X = {"Name": "src.mod", ...}`` consumed
        # by a module-level ``def __getattr__``.
        has_getattr = any(isinstance(n, ast.FunctionDef) and n.name == "__getattr__" for n in self.tree.body)
        dict_consts: dict[str, dict[str, str]] = {}
        for node in self.tree.body:
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.Dict):
                keys = node.value.keys
                vals = node.value.values
                if keys and all(
                    isinstance(k, ast.Constant)
                    and isinstance(v, ast.Constant)
                    and isinstance(k.value, str)
                    and isinstance(v.value, str)
                    for k, v in zip(keys, vals)
                ):
                    for tgt in node.targets:
                        if isinstance(tgt, ast.Name):
                            dict_consts[tgt.id] = {
                                k.value: v.value
                                for k, v in zip(keys, vals)  # type: ignore[union-attr]
                            }
        if has_getattr:
            for mapping in dict_consts.values():
                for name, mod in mapping.items():
                    self.lazy.setdefault(name, mod)
        # Write-through mirror: a ModuleType subclass defining ``__setattr__``
        # that calls ``setattr`` on modules named in one of the dict constants,
        # installed by assigning ``__class__`` on ``sys.modules[__name__]``.
        installed = any(
            isinstance(n, ast.Assign) and any(isinstance(t, ast.Attribute) and t.attr == "__class__" for t in n.targets)
            for n in self.tree.body
        )
        if installed:
            for node in self.tree.body:
                if not isinstance(node, ast.ClassDef):
                    continue
                setattr_fn = next(
                    (b for b in node.body if isinstance(b, ast.FunctionDef) and b.name == "__setattr__"),
                    None,
                )
                if setattr_fn is None:
                    continue
                for sub in ast.walk(setattr_fn):
                    if isinstance(sub, ast.Name) and sub.id in dict_consts:
                        self.mirror_targets.update(dict_consts[sub.id].values())
        # Loads: every Name(ctx=Load) anywhere in the module body, EXCEPT the
        # mirror machinery and ``__getattr__``/``__dir__`` helpers, which touch
        # names generically rather than calling anything.
        skip: set[int] = set()
        for node in self.tree.body:
            if isinstance(node, ast.FunctionDef) and node.name in {"__getattr__", "__dir__"}:
                skip.update(id(n) for n in ast.walk(node))
            if isinstance(node, ast.ClassDef) and any(
                isinstance(b, ast.FunctionDef) and b.name == "__setattr__" for b in node.body
            ):
                skip.update(id(n) for n in ast.walk(node))
        for node in ast.walk(self.tree):
            if id(node) in skip:
                continue
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
                self.loads.add(node.id)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for sub in ast.walk(node):
                    if isinstance(sub, ast.ImportFrom) and sub.module:
                        for alias in sub.names:
                            self.local_imports[alias.asname or alias.name].append((sub.module, sub.lineno))

    def _bind_top(self, node: ast.AST) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            self.bindings[node.name] = "def"
        elif isinstance(node, ast.ClassDef):
            self.bindings[node.name] = "class"
        elif isinstance(node, ast.Import):
            for alias in node.names:
                self.bindings[(alias.asname or alias.name).split(".")[0]] = "import"
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name != "*":
                    self.bindings[alias.asname or alias.name] = f"from {node.module}"
        elif isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for t in targets:
                for n in ast.walk(t):
                    if isinstance(n, ast.Name):
                        self.bindings[n.id] = "assign"
        elif isinstance(node, (ast.If, ast.Try, ast.With)):
            for child in ast.iter_child_nodes(node):
                if isinstance(child, ast.stmt):
                    self._bind_top(child)


_FACTS: dict[str, _ModuleFacts] = {}


def _facts(dotted: str, root: Path) -> _ModuleFacts:
    if dotted not in _FACTS:
        _FACTS[dotted] = _ModuleFacts(dotted, root)
    return _FACTS[dotted]


# --------------------------------------------------------------------------
# Test scanning


def _module_aliases(tree: ast.Module) -> dict[str, str]:
    """Local names in a test file that are bound to an audited module."""
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name in AUDITED_MODULES:
                    aliases[a.asname or a.name.split(".")[0]] = a.name
        elif isinstance(node, ast.ImportFrom) and node.module == "src":
            for a in node.names:
                dotted = f"src.{a.name}"
                if dotted in AUDITED_MODULES:
                    aliases[a.asname or a.name] = dotted
    return aliases


def _patch_aliases(tree: ast.Module) -> set[str]:
    """Local names bound to ``unittest.mock.patch`` (``patch``, ``_patch``, ...)."""
    names = {"patch"}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module in {"unittest.mock", "mock"}:
            for a in node.names:
                if a.name == "patch":
                    names.add(a.asname or a.name)
    return names


def _is_patch_call(func: ast.AST, patch_names: set[str]) -> str | None:
    """'patch' for patch(...)/mock.patch(...)/monkeypatch.setattr("a.b", ...),
    'object' for patch.object(...), else None."""
    if isinstance(func, ast.Name) and func.id in patch_names:
        return "patch"
    if isinstance(func, ast.Attribute):
        if func.attr == "patch" or func.attr == "setattr":
            return "patch"
        if func.attr == "object" and (
            (isinstance(func.value, ast.Name) and func.value.id in patch_names)
            or (isinstance(func.value, ast.Attribute) and func.value.attr == "patch")
        ):
            return "object"
    return None


def collect_targets(tests_dir: Path) -> list[PatchTarget]:
    out: list[PatchTarget] = []
    for path in sorted(tests_dir.rglob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        aliases = _module_aliases(tree)
        patch_names = _patch_aliases(tree)
        rel = str(path.relative_to(tests_dir.parent))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not node.args:
                continue
            kind = _is_patch_call(node.func, patch_names)
            if kind == "patch":
                first = node.args[0]
                if not (isinstance(first, ast.Constant) and isinstance(first.value, str)):
                    continue
                raw = first.value
                for mod in AUDITED_MODULES:
                    if raw.startswith(mod + "."):
                        name = raw[len(mod) + 1 :].split(".")[0]
                        out.append(PatchTarget(rel, node.lineno, mod, name, raw))
                        break
            elif kind == "object" and len(node.args) >= 2:
                tgt, attr = node.args[0], node.args[1]
                if (
                    isinstance(tgt, ast.Name)
                    and tgt.id in aliases
                    and isinstance(attr, ast.Constant)
                    and isinstance(attr.value, str)
                ):
                    mod = aliases[tgt.id]
                    out.append(PatchTarget(rel, node.lineno, mod, attr.value, f"{mod}.{attr.value}"))
    return out


# --------------------------------------------------------------------------
# Classification


def classify(target: PatchTarget, root: Path) -> Finding:
    facts = _facts(target.module, root)
    name = target.name
    binding = facts.bindings.get(name) or (f"lazy -> {facts.lazy[name]}" if name in facts.lazy else "")
    mirrored_in = sorted(
        m
        for m in facts.mirror_targets
        if (root / (m.replace(".", "/") + ".py")).exists() and name in _facts(m, root).bindings
    )
    if not binding:
        cls, reason = "MISSING", "no top-level binding and no lazy __getattr__ entry"
    elif name in facts.loads:
        cls, reason = "LIVE", "module body loads the name itself"
    elif mirrored_in:
        cls, reason = "MIRRORED", "write-through __setattr__ forwards to " + ", ".join(mirrored_in)
    else:
        cls, reason = "REEXPORT", "bound but never loaded here, and no mirror forwards it"
    # Informational: split-out modules the patched module imports from or
    # re-exports, which load the name through their OWN binding and are not
    # reached by this patch.  A LIVE patch can still miss these call sites.
    leaks: list[str] = []
    if cls in {"LIVE", "MIRRORED"}:
        for other in [facts.dotted, *_sibling_modules(facts, root)]:
            of = _facts(other, root)
            if (
                other != facts.dotted
                and cls == "LIVE"
                and (name in of.bindings and name in of.loads and other not in mirrored_in)
            ):
                leaks.append(other)
            for source, lineno in of.local_imports.get(name, ()):
                # A function-local import FROM the patched module is late-bound
                # and therefore reached; one from anywhere else is not.
                if source != facts.dotted:
                    leaks.append(f"{other}:{lineno} (function-local import from {source})")
    return Finding(target.test_file, target.lineno, target.module, name, cls, binding, reason, tuple(leaks))


def _sibling_modules(facts: _ModuleFacts, root: Path) -> list[str]:
    """Split-out modules: ``src.*`` modules this module imports from whose
    file name shares its stem prefix (pipeline_*, stage_*)."""
    stem = facts.dotted.rsplit(".", 1)[1]
    prefixes = (stem.split("_")[0] + "_", "stage_")
    sibs: set[str] = set(facts.mirror_targets)
    for node in facts.tree.body:
        if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("src."):
            leaf = node.module.rsplit(".", 1)[1]
            if leaf.startswith(prefixes) and (root / (node.module.replace(".", "/") + ".py")).exists():
                sibs.add(node.module)
    return sorted(sibs)


def run(root: Path) -> dict:
    _FACTS.clear()
    findings = [classify(t, root) for t in collect_targets(root / "tests")]
    counts = Counter(f.classification for f in findings)
    by_name: dict[str, Counter] = defaultdict(Counter)
    for f in findings:
        by_name[f.classification][f"{f.module}.{f.name}"] += 1
    return {
        "counts": {k: counts.get(k, 0) for k in ("LIVE", "MIRRORED", "REEXPORT", "MISSING")},
        "by_name": {k: dict(v.most_common()) for k, v in by_name.items()},
        "findings": [asdict(f) for f in findings],
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--root", default=str(Path(__file__).resolve().parents[1]))
    ap.add_argument("--json", action="store_true", help="emit the full report as JSON")
    args = ap.parse_args(argv)
    report = run(Path(args.root))
    if args.json:
        json.dump(report, sys.stdout, indent=1)
        return 0
    print("patch targets on", ", ".join(AUDITED_MODULES))
    for k, v in report["counts"].items():
        print(f"  {k:9} {v}")
    for cls in ("REEXPORT", "MISSING", "MIRRORED", "LIVE"):
        names = report["by_name"].get(cls, {})
        if not names:
            continue
        print(f"\n{cls} by name (patch count):")
        for n, c in names.items():
            print(f"  {c:4}  {n}")
    leaky = [f for f in report["findings"] if f["leaks_to"]]
    if leaky:
        print(
            "\nLIVE/MIRRORED but the name is ALSO loaded by split-out modules through an "
            "unmirrored binding or a function-local import (those call sites are NOT reached):"
        )
        seen: set[tuple[str, tuple[str, ...]]] = set()
        for f in leaky:
            key = (f"{f['module']}.{f['name']}", tuple(f["leaks_to"]))
            if key not in seen:
                seen.add(key)
                print(f"  {key[0]} -> {', '.join(key[1])}")
    bad = [f for f in report["findings"] if f["classification"] in {"REEXPORT", "MISSING"}]
    if bad:
        print("\nREEXPORT / MISSING sites:")
        for f in bad:
            print(
                f"  {f['test_file']}:{f['lineno']}  {f['module']}.{f['name']}  [{f['classification']}] {f['binding']}"
            )
    return 0


if __name__ == "__main__":
    sys.exit(main())
