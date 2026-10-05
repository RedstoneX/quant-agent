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
    now = file_size_guard.working_texts()
    biggest = max(now, key=lambda p: len(now[p].splitlines()))
    real = file_size_guard.trunk_blobs

    def shrunk(paths):
        blobs = real(paths)
        # Relative to the working copy, so a change that itself shrinks the
        # biggest file cannot hide the simulated growth.
        lines = now[biggest].splitlines()[:-50]
        blobs[biggest] = "\n".join(lines) + "\n"
        return blobs

    monkeypatch.setattr(file_size_guard, "trunk_blobs", shrunk)
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
    width = file_size_guard.width_fence(
        list(guard_reference.trunk_blobs(guard_reference.trunk_paths(".py")).values())
    )
    wrapped = ["x = 1", "log.warning(", "    'stop placement failed: %s', exc,", ")"]
    widened = ["x = 1", "log.warning(" + "'stop placement failed: %s', exc".ljust(width) + ")"]
    assert len(widened[1]) > width and all(len(ln) <= width for ln in wrapped)
    trunk = "\n".join(["x = 1", "pass", "pass", "pass"]) + "\n"  # same line count

    bad = _one_file(monkeypatch, trunk, "\n".join(widened) + "\n")
    assert any(f"wider than the derived {width}" in b for b in bad), bad
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


def test_the_file_size_guard_is_already_per_file_identity(monkeypatch):
    """Lines per file is numeric by nature; its identity is the path, and a shrink
    in one file never offsets growth in another."""
    trunk = {"a.py": "x\n" * 500, "b.py": "x\n" * 500}
    now = {"a.py": "x\n" * 400, "b.py": "x\n" * 501}
    monkeypatch.setattr(file_size_guard, "working_texts", lambda: now)
    monkeypatch.setattr(file_size_guard, "trunk_blobs", lambda paths: trunk)
    monkeypatch.setattr(file_size_guard, "trunk_paths", lambda suffix: list(trunk))
    bad = file_size_guard.violations()
    assert not any(b.startswith("a.py") for b in bad), bad  # its shrink offsets nothing
    lines_bad = [b for b in bad if "lines (+" in b]
    assert lines_bad == [
        "b.py: grew from 500 to 501 lines (+1) against origin/main. "
        "Split it instead of growing it."
    ], bad


# --- the two fences are DERIVED, never stored -------------------------------


def test_neither_fence_is_a_stored_number():
    """The defect being removed: a once-measured integer committed to the file.

    Both fences were literals (2561 and 120) until this change. A stored fence
    rots as the tree moves and an author can edit it to pass; these must be
    computed from the populations on every run."""
    src = (file_size_guard.ROOT / "scripts" / "file_size_guard.py").read_text()
    assert "CEILING = " not in src and "WIDTH = " not in src, src[:200]
    assert not hasattr(file_size_guard, "CEILING")
    assert not hasattr(file_size_guard, "WIDTH")


def test_the_derived_fences_match_the_trunk_population():
    """Re-derive both numbers here and demand the guard agrees."""
    trunk = guard_reference.trunk_blobs(guard_reference.trunk_paths(".py"))
    assert len(trunk) > 100, len(trunk)
    texts = list(trunk.values())
    assert file_size_guard.line_ceiling(texts) > file_size_guard.FLOOR
    assert 60 < file_size_guard.width_fence(texts) < 400


def test_padding_lines_cannot_drag_the_width_fence_out():
    """The gaming case for a derived percentile: fill the tree with near-fence
    lines and the percentile rises. The trunk side is computed in the same run
    and the tighter one binds, so it cannot."""
    trunk = ["short\n" * 50]
    padded = ["x" * 400 + "\n"] * 50
    _, width = file_size_guard.fences(padded, trunk)
    assert width == file_size_guard.width_fence(trunk), width
    # ... and the same clamp in the other direction is allowed: tighter binds.
    _, tighter = file_size_guard.fences(trunk, padded)
    assert tighter == file_size_guard.width_fence(trunk), tighter


def test_deleting_small_files_cannot_raise_the_line_ceiling():
    """Q3 rises when the small files go. The trunk's own Q3 still binds."""
    trunk = ["x\n" * 10] * 90 + ["x\n" * 900] * 10
    thinned = ["x\n" * 900] * 10
    assert file_size_guard.line_ceiling(thinned) > file_size_guard.line_ceiling(trunk)
    ceiling, _ = file_size_guard.fences(thinned, trunk)
    assert ceiling == file_size_guard.line_ceiling(trunk), ceiling


def test_the_ceiling_never_falls_below_the_new_module_floor():
    """400 is the owner's ceiling for a NEW module; a derived fence under it
    would contradict the floor rule rather than tighten it."""
    assert file_size_guard.line_ceiling(["x\n" * 3] * 100) == file_size_guard.FLOOR


def test_it_refuses_rather_than_invent_a_fence_from_nothing():
    with pytest.raises(ReferenceUnavailable):
        file_size_guard.line_ceiling([])
    with pytest.raises(ReferenceUnavailable):
        file_size_guard.width_fence([])
