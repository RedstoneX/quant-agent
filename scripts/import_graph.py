"""Import-graph logic for the layering guard (tests/test_import_layering.py).

Pure standard library. Builds the graph of internal ``src.*`` imports with
``ast``, separating runtime imports from ``TYPE_CHECKING``-only imports (a
type-only import is not a true dependency and is ignored by every check).

The cycle guard stores NOTHING: it builds the graph from the working tree,
builds it again from ``origin/main`` at check time, and reports only the DELTA
(docs/GUARDS_WITHOUT_STORED_STATE.md). If the trunk cannot be read it REFUSES
rather than pass. ``tests/import_layers.json`` is NOT a cached measurement --
it is the hand-written layering policy -- so it stays.

CLI:  PYTHONPATH=. .venv/bin/python -m scripts.import_graph          (report)
      PYTHONPATH=. .venv/bin/python -m scripts.import_graph --check  (guard)
"""
from __future__ import annotations

import ast
import json
import sys
from collections import defaultdict, deque
from pathlib import Path

from scripts.guard_reference import (
    ROOT,
    ReferenceUnavailable,
    TRUNK,
    trunk_blobs,
    trunk_paths,
)

SRC = ROOT / "src"
LAYERS_PATH = ROOT / "tests" / "import_layers.json"

Edge = tuple[str, str]


def module_name(path: Path) -> str:
    rel = path.relative_to(ROOT).with_suffix("")
    parts = list(rel.parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _is_type_checking(test: ast.expr) -> bool:
    return (isinstance(test, ast.Name) and test.id == "TYPE_CHECKING") or (
        isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"
    )


def _collect(tree: ast.AST):
    """Yield (node, type_only) for every import statement."""
    def walk(node, type_only):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.Import, ast.ImportFrom)):
                yield child, type_only
            elif isinstance(child, ast.If) and _is_type_checking(child.test):
                for sub in child.body:
                    yield from walk_one(sub, True)
                for sub in child.orelse:
                    yield from walk_one(sub, type_only)
            else:
                yield from walk(child, type_only)

    def walk_one(node, type_only):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            yield node, type_only
        else:
            yield from walk(node, type_only)

    yield from walk(tree, False)


def _module_of(rel: str) -> str:
    """``src/pkg/mod.py`` -> ``src.pkg.mod``; a package __init__ names the package."""
    parts = rel[:-len(".py")].split("/")
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def disk_sources(src_dir: Path = SRC) -> dict[str, str]:
    """Every ``src/**/*.py`` in the working tree, keyed by repo-relative path."""
    return {
        f.relative_to(ROOT).as_posix(): f.read_text(encoding="utf-8")
        for f in sorted(src_dir.rglob("*.py"))
    }


def trunk_sources() -> dict[str, str]:
    """The same files as they stand on ``origin/main``. Raises if it cannot read them."""
    return trunk_blobs([p for p in trunk_paths(".py") if p.startswith("src/")])


def graph_from_sources(sources: dict[str, str]):
    """Return (modules, runtime_edges, type_only_edges, edge_sites) for a source map.

    Taking text rather than a directory is what lets the guard measure the trunk
    without checking it out, so nothing has to be cached in the repo.
    """
    modules = {_module_of(rel): rel for rel in sorted(sources)}
    runtime: set[Edge] = set()
    type_only: set[Edge] = set()
    sites: dict[Edge, str] = {}
    for mod, rel in modules.items():
        is_pkg = rel.endswith("/__init__.py") or rel == "__init__.py"
        tree = ast.parse(sources[rel], filename=rel)
        for node, t_only in _collect(tree):
            targets: list[str] = []
            if isinstance(node, ast.Import):
                targets = [a.name for a in node.names]
            else:
                if node.level:
                    base = mod.split(".")
                    if not is_pkg:
                        base = base[:-1]
                    base = base[: len(base) - (node.level - 1)]
                    prefix = ".".join(base + ([node.module] if node.module else []))
                else:
                    prefix = node.module or ""
                subs = [f"{prefix}.{a.name}" for a in node.names if f"{prefix}.{a.name}" in modules]
                # `from pkg import submodule` depends on the submodule, not on pkg/__init__.
                targets = subs if len(subs) == len(node.names) else [prefix] + subs
            for t in targets:
                if t in modules and t != mod:
                    edge = (mod, t)
                    (type_only if t_only else runtime).add(edge)
                    sites.setdefault(edge, f"{rel}:{node.lineno}")
    type_only -= runtime
    return set(modules), runtime, type_only, sites


def build_graph(src_dir: Path = SRC):
    """The working tree's graph. edge_sites maps an edge to ``"path:line"``."""
    return graph_from_sources(disk_sources(src_dir))


def adjacency(edges) -> dict[str, set[str]]:
    adj: dict[str, set[str]] = defaultdict(set)
    for a, b in edges:
        adj[a].add(b)
    return adj


def sccs(nodes, edges) -> list[set[str]]:
    """Tarjan (iterative). Returns only components that contain a cycle."""
    adj = adjacency(edges)
    index, low, on, stack, out, counter = {}, {}, set(), [], [], [0]
    for root in sorted(nodes):
        if root in index:
            continue
        work = [(root, iter(sorted(adj[root])))]
        index[root] = low[root] = counter[0]; counter[0] += 1
        stack.append(root); on.add(root)
        while work:
            v, it = work[-1]
            for w in it:
                if w not in index:
                    index[w] = low[w] = counter[0]; counter[0] += 1
                    stack.append(w); on.add(w)
                    work.append((w, iter(sorted(adj[w]))))
                    break
                elif w in on:
                    low[v] = min(low[v], index[w])
            else:
                work.pop()
                if work:
                    low[work[-1][0]] = min(low[work[-1][0]], low[v])
                if low[v] == index[v]:
                    comp = set()
                    while True:
                        w = stack.pop(); on.discard(w); comp.add(w)
                        if w == v:
                            break
                    if len(comp) > 1:
                        out.append(comp)
    return out


def cycle_edges(nodes, edges) -> set[Edge]:
    """Every edge that lies on some cycle (both ends in one multi-node SCC)."""
    comp_of = {}
    for i, comp in enumerate(sccs(nodes, edges)):
        for m in comp:
            comp_of[m] = i
    return {(a, b) for a, b in edges if a in comp_of and comp_of.get(b) == comp_of[a]}


def shortest_cycles(edges) -> list[list[str]]:
    """Shortest cycle through each cycle edge, de-duplicated, shortest first."""
    adj = adjacency(edges)
    found: dict[frozenset, list[str]] = {}
    for a, b in edges:
        prev = {b: None}
        q = deque([b])
        while q and a not in prev:
            v = q.popleft()
            for w in sorted(adj[v]):
                if w not in prev:
                    prev[w] = v
                    q.append(w)
        if a in prev:
            path, v = [], a
            while v is not None:
                path.append(v); v = prev[v]
            path.reverse()  # b ... a
            cyc = path  # b -> ... -> a, closes via a -> b
            found.setdefault(frozenset(zip(cyc, cyc[1:] + cyc[:1])), cyc)
    return sorted(found.values(), key=lambda c: (len(c), c))


def load_json(path: Path, default):
    return json.loads(path.read_text()) if path.exists() else default


def layer_violations(runtime_edges, sites, config) -> list[str]:
    """Check declared rules. Config: {"rules": [{"name", "target_prefix",
    "allowed_importers": [prefixes], "why"}]}. An importer is allowed if its
    module equals or starts with ``prefix + "."`` for some allowed prefix."""
    def under(mod, prefix):
        return mod == prefix or mod.startswith(prefix + ".")

    out = []
    for rule in config.get("rules", []):
        if rule.get("exact_importers"):
            allowed = set(rule["exact_importers"])
            rule = dict(rule, allowed_importers=sorted(allowed) + rule.get("allowed_importers", []))
        for a, b in sorted(runtime_edges):
            if under(b, rule["target_prefix"]) and not any(
                under(a, p) for p in rule["allowed_importers"]
            ):
                out.append(
                    f"[{rule['name']}] {sites[(a, b)]}: {a} imports {b}. "
                    f"{rule['why']} Fix: route this through one of "
                    f"{rule['allowed_importers']} instead of importing {b} directly "
                    f"(or, if the architecture genuinely changed, edit "
                    f"tests/import_layers.json in the same PR and say why)."
                )
    return out


def stale_allowlist_entries(runtime_edges, config) -> list[str]:
    """Ratchet for ``exact_importers``: entries that no longer import the target."""
    out = []
    for rule in config.get("rules", []):
        for m in rule.get("exact_importers", []):
            if not any(a == m and (b == rule["target_prefix"] or b.startswith(rule["target_prefix"] + "."))
                       for a, b in runtime_edges):
                out.append(f"[{rule['name']}] {m} no longer imports {rule['target_prefix']}. "
                           f"Fix: delete it from exact_importers in tests/import_layers.json (the allowlist only shrinks).")
    return out


def new_cycle_edges():
    """Cycle edges this working tree has that ``origin/main`` does not.

    Both sides are measured here and now; nothing is read from or written to a
    committed file, so two unrelated changes can never collide over this guard.
    Raises ``ReferenceUnavailable`` when the trunk cannot be read -- the guard
    refuses rather than passing by default.
    """
    nodes, rt, _, sites = build_graph()
    now = cycle_edges(nodes, rt)
    t_nodes, t_rt, _, _ = graph_from_sources(trunk_sources())
    before = cycle_edges(t_nodes, t_rt)
    return sorted(now - before), now, sites


def new_cycle_report() -> list[str]:
    """One human line per newly introduced cycle edge, naming the loop it closes."""
    new, now, sites = new_cycle_edges()
    if not new:
        return []
    cycles = shortest_cycles(now)
    lines = []
    for a, b in new:
        cyc = next((c for c in cycles if (a, b) in zip(c, c[1:] + c[:1])), None)
        loop = " -> ".join(cyc + [cyc[0]]) if cyc else "(cycle)"
        lines.append(f"  {sites[(a, b)]}: {a} imports {b}, which closes the cycle {loop}")
    return lines


CYCLE_FIX_HINT = (
    "\nFix: remove or invert that import (move the shared piece into a lower module "
    "both can import, or import lazily at the call site only if the dependency is "
    "truly one-way). There is no baseline to add it to: this guard stores nothing "
    f"and compares the working tree against {TRUNK} every time it runs."
)


def check(argv: list[str] | None = None) -> int:
    try:
        lines = new_cycle_report()
    except ReferenceUnavailable as exc:
        print(f"REFUSING: {exc}", file=sys.stderr)
        return 2
    if lines:
        print("NEW import cycle(s) introduced against %s:\n%s%s"
              % (TRUNK, "\n".join(lines), CYCLE_FIX_HINT), file=sys.stderr)
        return 1
    print(f"import-cycle guard: this tree adds no import cycle against {TRUNK}.")
    return 0


def report() -> str:
    nodes, rt, to, sites = build_graph()
    cyc = shortest_cycles(cycle_edges(nodes, rt))
    inb, outb = defaultdict(int), defaultdict(int)
    for a, b in rt:
        outb[a] += 1; inb[b] += 1
    top = lambda d: sorted(d.items(), key=lambda kv: (-kv[1], kv[0]))[:10]
    lines = [f"modules={len(nodes)} runtime_edges={len(rt)} type_only_edges={len(to)}",
             f"cycles(shortest per edge)={len(cyc)}"]
    lines += [f"  {len(c)}: " + " -> ".join(c + [c[0]]) for c in cyc]
    lines.append("hubs(inbound): " + ", ".join(f"{m}={n}" for m, n in top(inb)))
    lines.append("entanglers(outbound): " + ", ".join(f"{m}={n}" for m, n in top(outb)))
    ex = {a for a, b in rt if b.startswith("src.execution") and not a.startswith("src.execution")}
    lines.append(f"modules importing src.execution from outside it: {len(ex)}: {sorted(ex)}")
    return "\n".join(lines)


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    if "--check" in sys.argv:
        raise SystemExit(check())
    print(report())
