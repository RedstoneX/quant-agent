"""The undefined-name guard reads the test tree; one frozen fixture is excluded."""
from __future__ import annotations

from pathlib import Path

from scripts import check_undefined_names as g
from scripts.undefined_names_exclusions import FIXTURE_FRAGMENTS

ROOT = Path(__file__).resolve().parent.parent


def test_the_default_scan_covers_the_test_tree_and_is_clean(monkeypatch, capsys):
    monkeypatch.chdir(ROOT)
    assert g.main([]) == 0
    assert "tests/" not in capsys.readouterr().err


def test_an_undefined_name_planted_in_the_test_tree_is_caught(tmp_path, capsys):
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_planted.py").write_text("def test_x():\n    return missing_helper()\n")
    assert g.main([str(tests)]) == 1
    assert "missing_helper" in capsys.readouterr().out
    (tests / "test_planted.py").write_text("def missing_helper():\n    return 1\n")
    assert g.main([str(tests)]) == 0


def test_only_the_one_frozen_fixture_is_excluded_by_name():
    assert FIXTURE_FRAGMENTS == {"tests/fixtures/resolver_log_bodies_pre_move.py"}
