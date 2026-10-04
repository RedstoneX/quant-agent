"""Mechanical architecture guard: no new import cycles, declared layer rules hold.

See scripts/import_graph.py for the graph logic. Only RUNTIME imports count;
TYPE_CHECKING-only imports are not real dependencies and are ignored.

The cycle half stores nothing: it measures this tree and ``origin/main`` at
check time and reports the delta, and REFUSES (never passes) when the trunk is
unreadable. The layer rule lives in code (``ig.LAYER_RULES``) and is checked the same way.
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


def test_no_new_layer_crossings():
    try:
        v = ig.new_layer_violations()
    except ReferenceUnavailable as exc:  # never pass by default
        pytest.fail(f"REFUSING: {exc}")
    assert not v, "New layer crossing(s):\n" + "\n".join("  " + x for x in v)


def test_layer_guard_refuses_when_trunk_is_unreadable(monkeypatch):
    def boom(*_a, **_k):
        raise ReferenceUnavailable("cannot read origin/main (simulated)")

    monkeypatch.setattr(ig, "trunk_sources", boom)
    with pytest.raises(ReferenceUnavailable):
        ig.new_layer_violations()


def test_layer_guard_flags_a_new_importer_even_when_another_is_removed():
    rule = ig.LAYER_RULES[0]
    before = {("src.a", "src.execution.x"), ("src.b", "src.execution.y")}
    now = {("src.a", "src.execution.x"), ("src.c", "src.execution.y"), ("src.execution.z", "src.execution.x")}
    added = ig.added_sites(ig.crossing_edges(now, rule), ig.crossing_edges(before, rule))
    assert [e for e, _, _ in added] == [("src.c", "src.execution.y")]


def test_type_checking_imports_are_not_runtime():
    src = "import a\nif TYPE_CHECKING:\n    import b\nelse:\n    import c\ndef f():\n    import d\n"
    got = {n.names[0].name: t for n, t in ig._collect(ast.parse(src))}
    assert got == {"a": False, "b": True, "c": False, "d": False}
