"""__new__-pipeline ratchet: no test file may gain a constructor-skipping build.

The guard stores nothing (docs/GUARDS_WITHOUT_STORED_STATE.md). It names each
``TradingPipeline.__new__`` site in this working tree, names them again on
``origin/main`` at check time, and fails on any site identity the tree holds that
the trunk does not -- never a total, so a removed site never licenses a new one --
and two unrelated changes can never collide over a shared offender list. If ``origin/main`` cannot be read it
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
    # Zero trunk sites is the goal state, not vacuity: the blobs above were read.
    assert pipeline_new_guard.trunk_sites(paths) is not None


def test_growth_is_caught_and_reported_as_a_delta(monkeypatch):
    """Pretend the trunk copy had one site fewer: the guard must say so."""
    offender = ("tests/test_planted.py", "p = TradingPipeline." + "__new__" + "(TradingPipeline)")
    now = {offender: [4, 8]}
    monkeypatch.setattr(pipeline_new_guard, "working_sites", lambda: now)
    real = lambda paths: {offender: 2}

    def fewer(paths):
        sites = real(paths)
        sites[offender] = sites.get(offender, 1) - 1
        return sites

    monkeypatch.setattr(pipeline_new_guard, "trunk_sites", fewer)
    bad = pipeline_new_guard.violations()
    path, line_text = offender
    assert any(path in line and line_text in line and "(+1)" in line for line in bad), bad


def test_a_brand_new_offender_file_is_caught(monkeypatch):
    """A test file absent from the trunk gets nothing grandfathered."""
    site = ("tests/test_invented.py", "p = TradingPipeline." + "__new__" + "(TradingPipeline)")
    monkeypatch.setattr(pipeline_new_guard, "working_sites", lambda: {site: [3, 9]})
    bad = pipeline_new_guard.violations()
    assert any("tests/test_invented.py" in line and "NEW test file" in line for line in bad), bad


def test_removing_one_site_and_adding_another_still_fails(monkeypatch):
    """The count hole: -1 old +1 new nets to zero. Identity comparison must still fail."""
    path = "tests/test_planted.py"
    old_line = "p = TradingPipeline." + "__new__" + "(TradingPipeline)"
    now = {(path, old_line): [4]}
    monkeypatch.setattr(pipeline_new_guard, "trunk_sites", lambda paths: {(path, old_line): 1})
    monkeypatch.setattr(pipeline_new_guard, "test_paths", lambda: [path])
    new_site = (path, "q = TradingPipeline." + "__new__" + "(TradingPipeline)  # moved")
    pretend = {site: lines for site, lines in now.items() if site != (path, old_line)}
    pretend[new_site] = [1]
    monkeypatch.setattr(pipeline_new_guard, "working_sites", lambda: pretend)
    bad = pipeline_new_guard.violations()
    assert len(bad) == 1 and path in bad[0] and "# moved" in bad[0], bad


def test_removing_sites_alone_passes(monkeypatch):
    monkeypatch.setattr(pipeline_new_guard, "working_sites", lambda: {})
    assert pipeline_new_guard.violations() == []


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
