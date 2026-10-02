"""__new__-pipeline ratchet: no test file may gain a constructor-skipping build.

The guard stores nothing (docs/GUARDS_WITHOUT_STORED_STATE.md). It counts the
``TradingPipeline.__new__`` sites in this working tree, counts them again on
``origin/main`` at check time, and compares the two, so two unrelated changes can
never collide over a shared offender list. If ``origin/main`` cannot be read it
REFUSES — it must never pass by default.

See scripts/pipeline_new_guard.py for why, and tests/pipeline_factory.py for the
replacement.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from scripts import guard_reference, pipeline_new_guard
from scripts.guard_reference import ReferenceUnavailable


def test_no_test_file_gained_a_dunder_new_pipeline_build():
    bad = pipeline_new_guard.violations()
    assert not bad, (
        "Test file(s) build TradingPipeline with __new__ more than origin/main does:\n"
        + "\n".join(bad)
    )


def test_trunk_measurement_is_actually_read():
    """A guard that measures nothing on the trunk would pass vacuously."""
    paths = pipeline_new_guard.test_paths()
    assert len(paths) > 50, f"only {len(paths)} test .py files seen in the working tree"
    blobs = guard_reference.trunk_blobs(sorted(paths))
    assert len(blobs) > 50, f"only {len(blobs)} of those were readable on origin/main"
    assert sum(pipeline_new_guard.trunk_sites(paths).values()) > 0, (
        "origin/main has no __new__ pipeline sites at all, so the comparison is vacuous"
    )


def test_growth_is_caught_and_reported_as_a_delta(monkeypatch):
    """Pretend the trunk copy had one site fewer: the guard must say so."""
    now = pipeline_new_guard.working_sites()
    assert now, "no __new__ sites in the tree at all; pick a different fixture"
    offender = sorted(now)[0]
    real = pipeline_new_guard.trunk_sites

    def fewer(paths):
        sites = real(paths)
        sites[offender] = sites.get(offender, 1) - 1
        return sites

    monkeypatch.setattr(pipeline_new_guard, "trunk_sites", fewer)
    bad = pipeline_new_guard.violations()
    assert any(offender in line and "grew from" in line and "+1" in line for line in bad), bad


def test_a_brand_new_offender_file_is_caught(monkeypatch):
    """A test file absent from the trunk gets nothing grandfathered."""
    monkeypatch.setattr(
        pipeline_new_guard, "working_sites", lambda: {"tests/test_invented.py": 2}
    )
    bad = pipeline_new_guard.violations()
    assert any("tests/test_invented.py" in line and "NEW test file" in line for line in bad), bad


def test_it_refuses_when_the_trunk_cannot_be_read(tmp_path, monkeypatch):
    repo = tmp_path / "norepo"
    (repo / "tests").mkdir(parents=True)
    # Assembled, not written literally: this file must not itself become an offender.
    site = "TradingPipeline." + "__new__" + "(TradingPipeline)"
    (repo / "tests" / "test_m.py").write_text(f"p = {site}\n")
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    subprocess.run(["git", "add", "tests/test_m.py"], cwd=repo, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@example.invalid", "-c", "user.name=t",
         "commit", "-qm", "base"], cwd=repo, check=True,
    )
    monkeypatch.setattr(guard_reference, "ROOT", Path(repo))
    monkeypatch.setattr(pipeline_new_guard, "ROOT", Path(repo))

    with pytest.raises(ReferenceUnavailable) as exc:
        pipeline_new_guard.violations()
    assert "origin/main" in str(exc.value)
    assert pipeline_new_guard.main() == 2
