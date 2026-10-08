"""Mechanical architecture guard: zero import cycles, broker-seam pairs shrink-only.

See scripts/import_graph.py. Only RUNTIME imports count; TYPE_CHECKING-only
imports are ignored. Nothing here reads origin/main: the cycle rule is
absolute and the seam pairs are a committed list that may only shrink.
"""
import ast

from scripts import import_graph as ig


def test_zero_import_cycles():
    lines = ig.cycle_report()
    assert not lines, "Import cycle(s):\n" + "\n".join(lines) + ig.CYCLE_FIX_HINT


def test_seam_pairs_match_the_committed_list():
    v = ig.layer_violations()
    assert not v, "Seam pair change(s):\n" + "\n".join("  " + x for x in v)


def test_seam_pair_list_is_refused_when_new_or_stale():
    rt = {("src.a", "src.execution.x"), ("src.execution.z", "src.execution.x")}
    assert ig.layer_violations(rt, pairs=["src.a -> src.execution.x"]) == []
    assert "NEW pair src.a -> src.execution.x" in ig.layer_violations(rt, pairs=[])[0]
    stale = ig.layer_violations(rt, pairs=["src.a -> src.execution.x", "src.b -> src.execution.y"])
    assert len(stale) == 1 and "STALE" in stale[0]
    assert any("sorted" in x for x in ig.layer_violations(set(), pairs=["b", "a"]))


def test_type_checking_imports_are_not_runtime():
    src = "import a\nif TYPE_CHECKING:\n    import b\nelse:\n    import c\ndef f():\n    import d\n"
    got = {n.names[0].name: t for n, t in ig._collect(ast.parse(src))}
    assert got == {"a": False, "b": True, "c": False, "d": False}


def _tree(**mods):
    out = {f"src/{n}.py": body for n, body in mods.items()}
    out["src/__init__.py"] = ""
    return out


def _check(monkeypatch, tree):
    monkeypatch.setattr(ig, "build_graph", lambda *_a, **_k: ig.graph_from_sources(tree))
    monkeypatch.setattr(ig, "read_seam_pairs", lambda *_a, **_k: [])
    return ig.check()


def test_a_cycle_is_refused(monkeypatch, capsys):
    assert _check(monkeypatch, _tree(a="import src.b\n", b="import src.a\n")) == 1
    assert "src.b -> src.a -> src.b" in capsys.readouterr().err


def test_a_cycle_hidden_in_a_function_body_is_still_a_cycle(monkeypatch):
    assert _check(monkeypatch, _tree(a="import src.b\n", b="def f():\n    import src.a\n")) == 1


def test_no_cycle_passes(monkeypatch):
    assert _check(monkeypatch, _tree(a="import src.b\n", b="X = 1\n")) == 0


def test_type_checking_only_loop_is_not_a_cycle(monkeypatch):
    tree = _tree(a="import src.b\n", b="if TYPE_CHECKING:\n    import src.a\n")
    assert _check(monkeypatch, tree) == 0
