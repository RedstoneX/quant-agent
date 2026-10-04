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
        # Relative to the working copy, so a change that itself shrinks the
        # biggest file cannot hide the simulated growth.
        sizes[biggest] = now[biggest] - 50
        return sizes

    monkeypatch.setattr(file_size_guard, "trunk_sizes", shrunk)
    bad = file_size_guard.violations()
    assert any(biggest in line and "grew from" in line and "+50" in line for line in bad), bad


def _with_trunk_copy(monkeypatch, path: str, trunk_text: str):
    """Pretend ``path`` reads as ``trunk_text`` on origin/main, all else real."""
    real = guard_reference.trunk_blobs

    def patched(paths):
        blobs = real(paths)
        if path in blobs:
            blobs[path] = trunk_text
        return blobs

    monkeypatch.setattr(file_size_guard, "trunk_blobs", patched)


def _one_file(monkeypatch, trunk_text: str, working_text: str) -> list[str]:
    """Run the guard over ONE real tracked path with both copies substituted."""
    path = min(file_size_guard.working_paths("*.py"))
    monkeypatch.setattr(file_size_guard, "working_paths", lambda pattern: [path])
    _with_trunk_copy(monkeypatch, path, trunk_text)
    monkeypatch.setattr(type(file_size_guard.ROOT / path), "read_text",
                        lambda self, **kw: working_text)
    return [b for b in file_size_guard.violations() if path in b]


def test_a_line_widened_past_the_limit_fails_and_passes_once_wrapped(monkeypatch):
    """The 2026-10-04 route: same code, wider lines, line count unchanged."""
    width = file_size_guard.WIDTH
    wrapped = ["x = 1", "log.warning(", "    'stop placement failed: %s', exc,", ")"]
    widened = ["x = 1", "log.warning(" + "'stop placement failed: %s', exc".ljust(width) + ")"]
    assert len(widened[1]) > width and all(len(ln) <= width for ln in wrapped)
    trunk = "\n".join(["x = 1", "pass", "pass", "pass"]) + "\n"  # same line count

    bad = _one_file(monkeypatch, trunk, "\n".join(widened) + "\n")
    assert any(f"wider than {width}" in b for b in bad), bad
    assert _one_file(monkeypatch, trunk, "\n".join(wrapped) + "\n") == []


def test_a_pre_existing_wide_line_is_not_reported(monkeypatch):
    """The trunk carries ~416 wide lines; the guard must be green on arrival."""
    bad = [b for b in file_size_guard.violations() if "wider than" in b]
    assert bad == [], bad


def test_more_ink_in_fewer_lines_is_still_growth(monkeypatch):
    """Lines are not size: a file over the floor that loses a line while gaining
    non-whitespace characters has grown, and the line ratchet alone is blind."""
    n = file_size_guard.FLOOR + 2
    trunk = "\n".join(["x = 1"] * n) + "\n"
    working = "\n".join(["x = 1  # loud"] * (n - 1)) + "\n"
    bad = _one_file(monkeypatch, trunk, working)
    assert any("non-whitespace characters" in b for b in bad), bad
    assert not any("lines (+" in b for b in bad), bad  # the line ratchet saw a shrink


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
