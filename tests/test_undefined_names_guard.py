"""The undefined-name guard: clean on the trunk, and proven to bite."""
from __future__ import annotations

from pathlib import Path

from scripts import check_undefined_names as g

ROOT = Path(__file__).resolve().parent.parent


def test_the_tree_has_no_undefined_names():
    assert g.main([str(ROOT / "src"), str(ROOT / "scripts"), str(ROOT / "main.py")]) == 0


def test_a_bare_undefined_name_in_a_function_is_caught():
    found, star = g.check_source("def f():\n    return _log.error('x')\n")
    assert not star and [n for _, n in found] == ["_log"]


def test_defining_the_name_makes_it_pass():
    assert g.check_source("_log = object()\ndef f():\n    return _log\n")[0] == []


def test_a_name_moved_out_and_left_behind_is_caught_in_a_method():
    src = "class C:\n    def m(self):\n        return helper(1)\n"
    assert [n for _, n in g.check_source(src)[0]] == ["helper"]
    assert g.check_source("def helper(x):\n    return x\n" + src)[0] == []


def test_locals_builtins_and_closures_are_not_flagged():
    src = ("import os\ndef f(a):\n    b = len(a)\n    def g():\n        return b + 1\n"
           "    return g, os, [i for i in a]\n")
    assert g.check_source(src)[0] == []


def test_star_import_module_is_skipped_not_guessed():
    assert g.check_source("from os import *\ndef f():\n    return nope\n") == ([], True)


def test_module_level_read_of_a_name_nothing_binds_is_caught():
    assert [n for _, n in g.check_source("x = _missing + 1\n")[0]] == ["_missing"]
    assert g.check_source("_missing = 1\nx = _missing + 1\n")[0] == []


def test_a_missing_or_empty_tree_is_refused_not_read_as_clean(tmp_path):
    assert g.main([str(tmp_path / "nope")]) == 1
    assert g.main([str(tmp_path)]) == 1


def test_the_default_scan_is_derived_from_git_and_reaches_the_root_and_ops():
    files = {p.relative_to(ROOT).as_posix() for p in g.tracked_production_files()}
    assert "main.py" in files and any(p.startswith("ops/") for p in files)
    assert any(p.startswith("tests/") for p in files)
    assert g.main([]) == 0
