"""File-size ratchet: no tracked Python file may grow against ``origin/main``.

The guard stores nothing (docs/GUARDS_WITHOUT_STORED_STATE.md). It measures this
working tree, measures ``origin/main`` at check time, and compares the two, so
two unrelated changes can never collide over a shared record. If ``origin/main``
cannot be read it REFUSES — it must never pass by default.

Its own self-tests judge invented trees (``_universe``), never the real one: a
self-test that assumes which files this branch left alone is itself a source
of phantom reds.
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


def _file(n: int) -> str:
    return "\n".join(["pass"] * n) + "\n"


_POPULATION = {f"pkg/m{i}.py": _file(100 + 7 * i) for i in range(30)}


def _universe(monkeypatch, working: dict, trunk: dict, untouched=frozenset()):
    """Judge a SYNTHETIC pair of trees; nothing here reads the real ones.

    The self-tests below used to pick the biggest real tracked file as their
    fixture and assume this branch had not touched it. On 2026-10-05 a branch
    that had shrunk that file (an honest split) turned all four red: the
    deltas the tests planted were swallowed by the branch's own shrinkage.
    A self-test that depends on what the branch under test happens to have
    edited is a phantom generator, so the fixtures are now invented in full.
    """
    g = file_size_guard
    working = dict(_POPULATION, **working)
    trunk = dict(_POPULATION, **trunk)
    monkeypatch.setattr(g, "working_texts", lambda: dict(working))
    monkeypatch.setattr(g, "working_sizes",
                        lambda: {p: g._count(t) for p, t in working.items()})
    monkeypatch.setattr(g, "trunk_blobs",
                        lambda paths: {p: trunk[p] for p in paths if p in trunk})
    monkeypatch.setattr(g, "trunk_paths", lambda suffix="": sorted(trunk))
    monkeypatch.setattr(g, "untouched_paths",
                        lambda texts: {p for p in texts if p in untouched})
    return g.violations()


def test_growth_is_caught_and_reported_as_a_delta(monkeypatch):
    bad = _universe(monkeypatch, {"big.py": _file(550)}, {"big.py": _file(500)})
    assert any(b.startswith("big.py: grew from 500 to 550 lines (+50)") for b in bad), bad


def test_trunk_shrinking_a_file_the_branch_never_opened_is_not_growth(monkeypatch):
    """The 2026-10-05 phantom: trunk split a file this tree still holds unchanged,
    so the working copy (500, the merge base's) is larger than trunk's (450)."""
    bad = _universe(monkeypatch, {"big.py": _file(500)}, {"big.py": _file(450)},
                    untouched={"big.py"})
    assert not [b for b in bad if "big.py" in b], bad


def test_touching_a_file_puts_it_back_under_the_full_rule(monkeypatch):
    """The gaming route: edit the file so it counts as touched, then grow it.
    A touched file is judged in full against the trunk it will merge into."""
    bad = _universe(monkeypatch, {"big.py": _file(502)}, {"big.py": _file(500)})
    assert any(b.startswith("big.py: grew from 500 to 502 lines (+2)") for b in bad), bad


def test_a_shrink_in_one_file_never_excuses_growth_in_another(monkeypatch):
    bad = _universe(monkeypatch, {"a.py": _file(480), "b.py": _file(501)},
                    {"a.py": _file(500), "b.py": _file(500)})
    assert any(b.startswith("b.py: grew from 500 to 501 lines (+1)") for b in bad), bad
    assert not [b for b in bad if b.startswith("a.py")], bad


def test_untouched_means_byte_identity_with_the_merge_base():
    """Real git: the merge base's exact bytes are untouched; one byte more or
    less is the branch's own, whatever else the branch did elsewhere."""
    base = guard_reference.merge_base_rev()
    assert base, "no merge base with origin/main: fetch with full history"
    path = min(guard_reference.trunk_paths(".py"))
    text = guard_reference.blobs_at(base, [path])[path]
    assert path in guard_reference.untouched_paths({path: text})
    assert path not in guard_reference.untouched_paths({path: text + "\n"})
    assert path not in guard_reference.untouched_paths({path: text[:-1]})


def test_no_merge_base_excludes_nothing(monkeypatch):
    monkeypatch.setattr(guard_reference, "merge_base_rev", lambda: "")
    path = min(file_size_guard.working_paths("*.py"))
    text = (file_size_guard.ROOT / path).read_text(encoding="utf-8")
    assert guard_reference.untouched_paths({path: text}) == set()


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


def _over_floor(lines: list[str]) -> str:
    """Pad a snippet out past the floor with statements the trunk also carries."""
    return "\n".join(lines + ["pass"] * (file_size_guard.FLOOR + 2 - len(lines))) + "\n"


def test_more_statements_in_fewer_lines_is_still_growth(monkeypatch):
    """THE FAILING CASE. Lines are not size: a file over the floor that loses a
    line while gaining real statements has grown, and the line ratchet alone is
    blind. Delete the statement rule and this test goes green."""
    trunk = _over_floor(["x = 1", "y = 2"])
    working = _over_floor(["x = 1; y = 2; z = 3; w = 4"])
    bad = _one_file(monkeypatch, trunk, working)
    assert any("statements" in b and "(+3)" in b for b in bad), bad
    assert not any("lines (+" in b for b in bad), bad  # the line ratchet saw a shrink


def test_semicolon_joining_still_counts_each_statement(monkeypatch):
    """What killed the LINE rule must not kill this one: joined statements are
    counted individually, so cramming buys a grower nothing."""
    assert file_size_guard._statements("a = 1; b = 2; c = 3") == 3
    bad = _one_file(monkeypatch, _over_floor(["a = 1"]), _over_floor(["a = 1; b = 2"]))
    assert any("statements" in b and "(+1)" in b for b in bad), bad


def test_a_pure_rename_is_not_growth(monkeypatch):
    """The defect that forced this rule. Moving a method onto a collaborator
    rewrites every call site and adds characters; it adds no statements, and
    the old character ratchet refused it, which is why shims were kept."""
    trunk = _over_floor(["self.foo(1)", "self.foo(2)"])
    working = _over_floor(["self.admission.foo(1)", "self.admission.foo(2)"])
    assert len(working) > len(trunk)  # strictly more characters
    assert _one_file(monkeypatch, trunk, working) == []


def test_reflowing_a_call_over_more_lines_is_not_growth(monkeypatch):
    """Wrapping is not growth either; the rule is invariant under re-layout."""
    trunk = _over_floor(["call(a, b, c)"])
    working = _over_floor(["call(", "    a,", "    b,", "    c,", ")"])
    assert not [b for b in _one_file(monkeypatch, trunk, working) if "statements" in b]


def test_a_comprehension_launders_statements_and_the_guard_says_so(monkeypatch):
    """The NAMED RESIDUAL HOLE, pinned by a test so it cannot be forgotten.

    A statement count errs PERMISSIVE on expression growth: a loop collapsed
    into a comprehension loses three statements while the behaviour stands.
    The guard's own docstring names this and names what binds instead -- the
    width fence and the line ratchet. If this ever starts failing, the hole
    closed and the docstring is the thing to fix."""
    loop = "out = []\nfor x in r:\n    if x:\n        out.append(f(x))"
    assert file_size_guard._statements(loop) == 4
    assert file_size_guard._statements("out = [f(x) for x in r if x]") == 1
    src = (file_size_guard.ROOT / "scripts" / "file_size_guard.py").read_text()
    assert "RESIDUAL HOLE" in src and "PERMISSIVE" in src


def test_an_unparseable_file_is_refused_not_waved_through(monkeypatch):
    assert file_size_guard._statements("def (:\n") is None
    bad = _one_file(monkeypatch, _over_floor(["a = 1"]), _over_floor(["def (:"]))
    assert any("does not parse" in b for b in bad), bad


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
    bad = _universe(monkeypatch, {"a.py": _file(400), "b.py": _file(501)},
                    {"a.py": _file(500), "b.py": _file(500)})
    lines = [x for x in bad if " lines (+" in x]
    assert len(lines) == 1 and lines[0].startswith("b.py: grew from 500 to 501"), bad


# --- the two fences are DERIVED, never stored -------------------------------


def test_neither_fence_is_a_stored_number():
    """The defect removed: two once-measured integers committed to the guard.

    A stored fence rots as the tree moves and an author can edit it to pass."""
    src = (file_size_guard.ROOT / "scripts" / "file_size_guard.py").read_text()
    assert "CEILING = " not in src and "WIDTH = " not in src
    assert not hasattr(file_size_guard, "CEILING")
    assert not hasattr(file_size_guard, "WIDTH")


def test_the_derived_fences_match_the_trunk_population():
    trunk = guard_reference.trunk_blobs(guard_reference.trunk_paths(".py"))
    assert len(trunk) > 100, len(trunk)
    texts = list(trunk.values())
    assert file_size_guard.line_ceiling(texts) > file_size_guard.FLOOR
    assert 60 < file_size_guard.width_fence(texts) < 400


def test_padding_lines_cannot_drag_the_width_fence_out():
    """The gaming case for a derived percentile: fill the tree with near-fence
    lines and it rises. The trunk side is computed in the same run and binds."""
    trunk = ["short\n" * 50]
    padded = ["x" * 400 + "\n"] * 50
    _, width = file_size_guard.fences(padded, trunk)
    assert width == file_size_guard.width_fence(trunk), width
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
