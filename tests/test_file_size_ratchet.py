"""File-size ratchet: no tracked Python file may grow against ``origin/main``,
unless the change as a whole shrinks the tree and the file stays under the ceiling.

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
from scripts.file_size_guard import CEILING, FLOOR, Change
from scripts.guard_reference import ReferenceUnavailable


def test_no_tracked_file_grew_against_the_trunk():
    bad = file_size_guard.violations()
    assert not bad, (
        "Files grew against origin/main without the change shrinking the tree; "
        "split them instead of growing them:\n" + "\n".join(bad)
    )


def test_trunk_measurement_is_actually_read():
    """A guard that measures nothing on the trunk would pass vacuously."""
    paths = guard_reference.trunk_paths(".py")
    assert len(paths) > 100, f"only {len(paths)} .py files seen on origin/main"
    sizes = file_size_guard.trunk_sizes(paths[:50])
    assert sizes and all(n >= 0 for n in sizes.values())
    change = file_size_guard.measure()
    assert len(change.before) == len(paths)


def test_pure_addition_is_refused_with_the_delta_and_the_net():
    """The guard still bites: one file grows, nothing shrinks."""
    change = Change(now={"a.py": 520, "b.py": 300}, before={"a.py": 500, "b.py": 300},
                    touched={"a.py"})
    bad = file_size_guard.violations(change)
    assert len(bad) == 1, bad
    assert "a.py: grew from 500 to 520 lines (+20)" in bad[0]
    assert "nets +20 lines over 1 touched file" in bad[0]
    assert "Split it" in bad[0]


def test_net_reducing_consolidation_is_allowed_and_reported():
    """Seven mixins die, the holder gains their wiring: net down, one file up."""
    before = {"holder.py": 500, **{f"mixin{i}.py": 60 for i in range(7)}, "other.py": 900}
    now = {"holder.py": 700, "other.py": 900}
    change = Change(now=now, before=before, touched={"holder.py", *(f"mixin{i}.py" for i in range(7))})
    bad, waived = file_size_guard.assess(change)
    assert bad == [], bad
    assert len(waived) == 1 and "holder.py: grew from 500 to 700 lines (+200)" in waived[0]
    assert "nets -220 lines over 8 touched files" in waived[0]


def test_growth_that_merely_matches_removals_is_allowed_but_one_line_over_is_not():
    before = {"a.py": 500, "dead.py": 100}
    assert file_size_guard.violations(Change({"a.py": 600}, before, {"a.py", "dead.py"})) == []
    bad = file_size_guard.violations(Change({"a.py": 601}, before, {"a.py", "dead.py"}))
    assert len(bad) == 1 and "nets +1 lines" in bad[0], bad


def test_untouched_files_never_count_toward_the_net():
    """A big untouched file on the trunk is not currency for growth elsewhere."""
    change = Change(now={"a.py": 501, "big.py": 3000}, before={"a.py": 500, "big.py": 3000},
                    touched={"a.py"})
    bad = file_size_guard.violations(change)
    assert len(bad) == 1 and "a.py: grew from 500 to 501" in bad[0], bad


def test_net_reduction_never_lifts_a_file_over_the_ceiling():
    before = {"a.py": CEILING - 10, "dead.py": 500}
    change = Change({"a.py": CEILING + 1}, before, {"a.py", "dead.py"})
    bad = file_size_guard.violations(change)
    assert len(bad) == 1 and f"over the {CEILING}-line hard ceiling" in bad[0], bad
    assert file_size_guard.violations(Change({"a.py": CEILING}, before, {"a.py", "dead.py"})) == []


def test_new_file_over_the_floor_follows_the_same_rule():
    before = {"m1.py": 300, "m2.py": 300}
    consolidated = Change({"holder.py": FLOOR + 100}, before, {"holder.py", "m1.py", "m2.py"})
    assert file_size_guard.violations(consolidated) == []
    fresh = Change({"holder.py": FLOOR + 1, **before}, before, {"holder.py"})
    bad = file_size_guard.violations(fresh)
    assert len(bad) == 1 and "new file at 401 lines" in bad[0] and "nets +401" in bad[0], bad


def _commit(repo: Path, msg: str) -> None:
    subprocess.run(
        ["git", "-c", "user.email=t@example.invalid", "-c", "user.name=t",
         "commit", "-qam", msg], cwd=repo, check=True,
    )


def _throwaway_clone(tmp_path: Path, files: dict[str, int]) -> Path:
    """A fresh upstream with ``files`` (name -> line count) and a clone of it whose
    ``origin/main`` is that upstream. Nothing here touches the real repository."""
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(upstream)], check=True)
    for name, n in files.items():
        (upstream / name).write_text("x = 1\n" * n)
        subprocess.run(["git", "add", name], cwd=upstream, check=True)
    _commit(upstream, "base")
    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", "-q", str(upstream), str(clone)], check=True)
    return clone


def _point_guard_at(monkeypatch, repo: Path) -> None:
    monkeypatch.setattr(guard_reference, "ROOT", repo)
    monkeypatch.setattr(file_size_guard, "ROOT", repo)


def test_end_to_end_pure_addition_fails_in_a_throwaway_repo(tmp_path, monkeypatch, capsys):
    clone = _throwaway_clone(tmp_path, {"a.py": 500, "b.py": 100})
    (clone / "a.py").write_text("x = 1\n" * 510)
    _point_guard_at(monkeypatch, clone)
    assert file_size_guard.main() == 1
    err = capsys.readouterr().err
    assert "a.py: grew from 500 to 510 lines (+10)" in err and "nets +10 lines" in err


def test_end_to_end_consolidation_passes_in_a_throwaway_repo(tmp_path, monkeypatch, capsys):
    clone = _throwaway_clone(tmp_path, {"holder.py": 500, "m1.py": 80, "m2.py": 80})
    (clone / "holder.py").write_text("x = 1\n" * 600)
    (clone / "m1.py").unlink()
    (clone / "m2.py").unlink()
    _point_guard_at(monkeypatch, clone)
    assert file_size_guard.main() == 0
    out = capsys.readouterr().out
    assert "holder.py: grew from 500 to 600 lines (+100)" in out and "nets -60 lines over 3 touched files" in out


def test_it_refuses_when_the_trunk_cannot_be_read(tmp_path, monkeypatch):
    repo = tmp_path / "norepo"
    (repo / "pkg").mkdir(parents=True)
    (repo / "pkg" / "m.py").write_text("x = 1\n")
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    subprocess.run(["git", "add", "pkg/m.py"], cwd=repo, check=True)
    _commit(repo, "base")
    _point_guard_at(monkeypatch, Path(repo))

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


def test_every_grown_file_is_named_even_when_one_is_waived():
    """The net is only a permission; detection stays per path, so two grown files
    under a positive net are both reported, never summed into one number."""
    change = Change(now={"a.py": 510, "b.py": 510}, before={"a.py": 500, "b.py": 500},
                    touched={"a.py", "b.py"})
    bad = file_size_guard.violations(change)
    assert [line.split(":")[0] for line in bad] == ["a.py", "b.py"], bad
