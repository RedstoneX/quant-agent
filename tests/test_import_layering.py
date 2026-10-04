"""Mechanical architecture guard: no new import cycles, declared layer rules hold.

See scripts/import_graph.py for the graph logic. Only RUNTIME imports count;
TYPE_CHECKING-only imports are not real dependencies and are ignored.

The cycle half stores nothing: it measures this tree and ``origin/main`` at
check time and reports the delta, and REFUSES (never passes) when the trunk is
unreadable. ``tests/import_layers.json`` is the hand-written layering policy,
not a cached measurement, so the layer rules below still read it.
"""
import ast

import pytest

from scripts import import_graph as ig
from scripts.guard_reference import ReferenceUnavailable


def _state():
    nodes, rt, type_only, sites = ig.build_graph()
    return nodes, rt, sites, ig.cycle_edges(nodes, rt)


def test_no_new_import_cycles():
    try:
        lines = ig.new_cycle_report()
    except ReferenceUnavailable as exc:  # never pass by default
        pytest.fail(f"REFUSING: {exc}")
    assert not lines, (
        "NEW import cycle(s) introduced:\n" + "\n".join(lines) + ig.CYCLE_FIX_HINT)


def test_guard_refuses_when_trunk_is_unreadable(monkeypatch):
    def boom(*_a, **_k):
        raise ReferenceUnavailable("cannot read origin/main (simulated)")

    monkeypatch.setattr(ig, "trunk_sources", boom)
    with pytest.raises(ReferenceUnavailable):
        ig.new_cycle_edges()
    assert ig.check() == 2


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
