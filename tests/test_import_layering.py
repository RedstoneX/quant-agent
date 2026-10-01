"""Mechanical architecture guard: no new import cycles, declared layer rules hold.

See scripts/import_graph.py for the graph logic. Only RUNTIME imports count;
TYPE_CHECKING-only imports are not real dependencies and are ignored.
"""
import ast

from scripts import import_graph as ig


def _state():
    nodes, rt, type_only, sites = ig.build_graph()
    return nodes, rt, sites, ig.cycle_edges(nodes, rt)


def _baseline():
    return {tuple(e) for e in ig.load_json(ig.BASELINE_PATH, {"edges": []})["edges"]}


def test_no_new_import_cycles():
    nodes, rt, sites, now = _state()
    new = sorted(now - _baseline())
    if not new:
        return
    cycles = ig.shortest_cycles(now)
    lines = []
    for a, b in new:
        cyc = next((c for c in cycles if (a, b) in zip(c, c[1:] + c[:1])), None)
        loop = " -> ".join(cyc + [cyc[0]]) if cyc else "(cycle)"
        lines.append(f"  {sites[(a, b)]}: {a} imports {b}, which closes the cycle {loop}")
    raise AssertionError(
        "NEW import cycle(s) introduced:\n" + "\n".join(lines) +
        "\nFix: remove or invert that import (move the shared piece into a lower module both can import, "
        "or import lazily at the call site only if the dependency is truly one-way). "
        "Do NOT add it to tests/import_cycle_baseline.json; that list may only shrink.")


def test_baseline_only_shrinks():
    _, _, _, now = _state()
    stale = sorted(_baseline() - now)
    assert not stale, (
        "These baseline cycle edges are no longer in any cycle (good, you fixed them): "
        + ", ".join(f"{a} -> {b}" for a, b in stale) +
        "\nFix: run `PYTHONPATH=. .venv/bin/python -m scripts.import_graph --shrink-baseline` "
        "and commit tests/import_cycle_baseline.json so the ratchet tightens.")


def test_layer_rules_hold():
    _, rt, sites, _ = _state()
    cfg = ig.load_json(ig.LAYERS_PATH, {"rules": []})
    v = ig.layer_violations(rt, sites, cfg)
    assert not v, "Layer rule violation(s):\n" + "\n".join("  " + x for x in v)


def test_layer_allowlists_only_shrink():
    _, rt, _, _ = _state()
    cfg = ig.load_json(ig.LAYERS_PATH, {"rules": []})
    stale = ig.stale_allowlist_entries(rt, cfg)
    assert not stale, "Stale layer allowlist entries:\n" + "\n".join("  " + x for x in stale)


def test_type_checking_imports_are_not_runtime():
    src = "import a\nif TYPE_CHECKING:\n    import b\nelse:\n    import c\ndef f():\n    import d\n"
    got = {n.names[0].name: t for n, t in ig._collect(ast.parse(src))}
    assert got == {"a": False, "b": True, "c": False, "d": False}
