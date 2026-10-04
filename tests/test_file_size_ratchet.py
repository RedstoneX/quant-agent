"""File-size ratchet: no tracked Python file may grow against ``origin/main``.

The guard stores nothing (docs/GUARDS_WITHOUT_STORED_STATE.md). It measures this
working tree, measures ``origin/main`` at check time, and compares the two, so
two unrelated changes can never collide over a shared record. If ``origin/main``
cannot be read it REFUSES — it must never pass by default.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from scripts import file_size_guard, guard_reference
from scripts.guard_reference import ReferenceUnavailable


def test_no_tracked_file_grew_against_the_trunk():
    bad = file_size_guard.violations()
    assert not bad, (
        "Files grew against origin/main; split them instead of growing them:\n"
        + "\n".join(bad)
    )


def test_trunk_measurement_is_actually_read():
    """A guard that measures nothing on the trunk would pass vacuously."""
    paths = guard_reference.trunk_paths(".py")
    assert len(paths) > 100, f"only {len(paths)} .py files seen on origin/main"
    sizes = file_size_guard.trunk_sizes(paths[:50])
    assert sizes and all(n >= 0 for n in sizes.values())


def test_growth_is_caught_and_reported_as_a_delta(monkeypatch):
    """Pretend the trunk copy is 50 lines shorter: the guard must say so."""
    now = file_size_guard.working_sizes()
    biggest = max(now, key=lambda p: now[p])
    real = file_size_guard.trunk_sizes

    def shrunk(paths):
        sizes = real(paths)
        sizes[biggest] = now[biggest] - 50  # relative to the working copy, so a change that itself shrinks the biggest file cannot hide the simulated growth
        return sizes

    monkeypatch.setattr(file_size_guard, "trunk_sizes", shrunk)
    bad = file_size_guard.violations()
    assert any(biggest in line and "grew from" in line and "+50" in line for line in bad), bad


def test_it_refuses_when_the_trunk_cannot_be_read(tmp_path, monkeypatch):
    repo = tmp_path / "norepo"
    (repo / "pkg").mkdir(parents=True)
    (repo / "pkg" / "m.py").write_text("x = 1\n")
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    subprocess.run(["git", "add", "pkg/m.py"], cwd=repo, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@example.invalid", "-c", "user.name=t",
         "commit", "-qm", "base"], cwd=repo, check=True,
    )
    monkeypatch.setattr(guard_reference, "ROOT", Path(repo))
    monkeypatch.setattr(file_size_guard, "ROOT", Path(repo))

    with pytest.raises(ReferenceUnavailable) as exc:
        file_size_guard.violations()
    assert "origin/main" in str(exc.value)
    assert file_size_guard.main() == 2


def test_added_sites_compares_identities_never_totals():
    """The shared comparison every scanning guard uses: -1 old +1 new must fail."""
    before = {("f.py", "a"): 1, ("f.py", "b"): 1}
    assert guard_reference.added_sites({("f.py", "a"): 1, ("f.py", "c"): 1}, before) == [
        (("f.py", "c"), 1, 0)
    ]
    assert guard_reference.added_sites({("f.py", "a"): 1}, before) == []  # removal only
    assert guard_reference.added_sites({("f.py", "a"): 2}, before) == [(("f.py", "a"), 2, 1)]
    assert guard_reference.added_sites([("g.py", "x")], []) == [(("g.py", "x"), 1, 0)]


def test_the_file_size_guard_is_already_per_file_identity():
    """Lines per file is numeric by nature; its identity is the path, and a shrink
    in one file never offsets growth in another."""
    sizes = {"a.py": 500, "b.py": 500}
    monkeypatch_now = {"a.py": 400, "b.py": 501}
    import scripts.file_size_guard as g
    orig_w, orig_t = g.working_sizes, g.trunk_sizes
    g.working_sizes, g.trunk_sizes = (lambda: monkeypatch_now), (lambda paths: sizes)
    try:
        bad = g.violations()
    finally:
        g.working_sizes, g.trunk_sizes = orig_w, orig_t
    assert len(bad) == 1 and bad[0].startswith("b.py: grew from 500 to 501"), bad
