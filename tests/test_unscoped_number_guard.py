"""The unscoped-number guard stores no ceiling (docs/GUARDS_WITHOUT_STORED_STATE.md)."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from scripts import guard_reference, unscoped_number_guard
from scripts.guard_reference import ReferenceUnavailable


def test_no_unscoped_number_is_added_against_the_trunk():
    bad = unscoped_number_guard.violations()
    assert not bad, "\n  ".join(bad)


def test_the_trunk_is_actually_measured():
    assert len(unscoped_number_guard.trunk_sites()) > 50, "trunk measured nothing"


def test_a_new_number_is_caught_as_a_delta(monkeypatch):
    real = unscoped_number_guard.working_sites()
    monkeypatch.setattr(unscoped_number_guard, "working_sites", lambda: real)
    monkeypatch.setattr(unscoped_number_guard, "trunk_sites", lambda: real[:-1])
    bad = unscoped_number_guard.violations()
    assert bad and "+1" in bad[0] and real[-1] in bad[0], bad


def test_it_refuses_when_the_trunk_cannot_be_read(tmp_path, monkeypatch):
    repo = tmp_path / "norepo"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "m.py").write_text("X = 1\n")
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    subprocess.run(["git", "add", "src/m.py"], cwd=repo, check=True)
    subprocess.run(["git", "-c", "user.email=t@example.invalid", "-c", "user.name=t",
                    "commit", "-qm", "base"], cwd=repo, check=True)
    monkeypatch.setattr(guard_reference, "ROOT", Path(repo))
    monkeypatch.setattr(unscoped_number_guard, "ROOT", Path(repo))
    monkeypatch.setattr(unscoped_number_guard, "working_sites", lambda: [])
    with pytest.raises(ReferenceUnavailable) as exc:
        unscoped_number_guard.violations()
    assert "origin/main" in str(exc.value)
    assert unscoped_number_guard.main() == 2
