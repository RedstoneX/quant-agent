"""Import-graph logic for the layering guard (tests/test_import_layering.py).

Pure standard library. Builds the graph of internal ``src.*`` imports with
``ast``, separating runtime imports from ``TYPE_CHECKING``-only imports (a
type-only import is not a true dependency and is ignored by every check).

The cycle rule is ABSOLUTE: the runtime graph must hold zero cycles. The
layering rule compares this tree's (importer, imported) pairs to a committed,
sorted, shrink-only list (config/check_allowlists/import_seam_pairs.txt): a new
pair fails and a stale pair fails. Nothing here reads origin/main.

CLI:  PYTHONPATH=. .venv/bin/python -m scripts.import_graph          (report)
      PYTHONPATH=. .venv/bin/python -m scripts.import_graph --check  (guard)
"""
from __future__ import annotations

import ast
import sys
from collections import defaultdict, deque
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
SEAM_PAIRS = ROOT / "config" / "check_allowlists" / "import_seam_pairs.txt"

# One-way rule, in code not in a file: src.execution is the broker seam (it can
# move real money), so no module outside it may GAIN a runtime import of it.
# Existing importers are the committed pair list; it may only shrink.
LAYER_RULES = (
    {"name": "broker-seam", "target_prefix": "src.execution",
     "allowed_importers": ("src.execution",),
     "why": "src.execution is the broker seam (it can move real money) and the set of modules reaching it must not widen."},
)

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
            # Start at the smallest module: `edges` is a set, so without this
            # the printed rotation followed string-hash order and changed run
            # to run (PYTHONHASHSEED), flaking any test that reads the report.
            k = cyc.index(min(cyc))
            cyc = cyc[k:] + cyc[:k]
            found.setdefault(frozenset(zip(cyc, cyc[1:] + cyc[:1])), cyc)
    return sorted(found.values(), key=lambda c: (len(c), c))


def _under(mod, prefix):
    return mod == prefix or mod.startswith(prefix + ".")


def crossing_edges(runtime_edges, rule) -> set[Edge]:
    """Runtime edges that reach the rule's target from outside its allowed importers."""
    return {
        (a, b) for a, b in runtime_edges
        if _under(b, rule["target_prefix"])
        and not any(_under(a, p) for p in rule["allowed_importers"])
    }


def read_seam_pairs(path: Path = SEAM_PAIRS) -> list[str]:
    """Lines of the committed pair list (``importer -> imported``), comments dropped."""
    if not path.exists():
        return []
    return [ln for ln in path.read_text().splitlines() if ln.strip() and not ln.startswith("#")]


def layer_violations(rt=None, rules=LAYER_RULES, pairs=None) -> list[str]:
    """Fail on a rule-crossing pair missing from the list, and on a stale listed pair."""
    if rt is None:
        _, rt, _, _ = build_graph()
    listed = read_seam_pairs() if pairs is None else list(pairs)
    now = sorted(f"{a} -> {b}" for rule in rules for a, b in crossing_edges(rt, rule))
    out = []
    if listed != sorted(listed) or len(set(listed)) != len(listed):
        out.append("import_seam_pairs.txt must be sorted with no duplicates.")
    for pair in sorted(set(now) - set(listed)):
        out.append(
            f"[{rules[0]['name']}] NEW pair {pair}. {rules[0]['why']} Fix: route it through "
            f"{list(rules[0]['allowed_importers'])}. The pair list only shrinks.")
    for pair in sorted(set(listed) - set(now)):
        out.append(f"[{rules[0]['name']}] STALE pair {pair}: no longer imported; delete the line.")
    return out


def cycle_report(rt=None, nodes=None) -> list[str]:
    """One line per shortest cycle. The rule is absolute: any cycle fails."""
    if rt is None:
        nodes, rt, _, _ = build_graph()
    return ["  " + " -> ".join(c + [c[0]]) for c in shortest_cycles(cycle_edges(nodes, rt))]


CYCLE_FIX_HINT = (
    "\nFix: remove or invert that import (move the shared piece into a lower module "
    "both can import, or import lazily at the call site only if the dependency is "
    "truly one-way). The rule is zero cycles; there is nothing to add it to."
)


def check(argv: list[str] | None = None) -> int:
    lines = cycle_report() + layer_violations()
    if lines:
        print("Import cycle(s) or seam-pair change(s):\n%s%s"
              % ("\n".join(lines), CYCLE_FIX_HINT), file=sys.stderr)
        return 1
    print("import guard: zero import cycles; seam pairs match the committed list.")
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
