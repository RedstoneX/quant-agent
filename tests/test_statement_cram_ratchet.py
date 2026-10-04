"""Statement-cramming ratchet: no tracked .py file may gain a line holding more
than one statement, measured against ``origin/main`` at check time.

Companion to the file-size ratchet. On 2026-10-04 a change satisfied that
ratchet by joining statements onto shared lines instead of splitting the file;
this guard makes that route fail mechanically. It parses, never pattern-matches;
stores nothing; and REFUSES when the trunk cannot be read.
"""
from __future__ import annotations

import subprocess
from collections import Counter
from pathlib import Path

import pytest

from scripts import guard_reference, statement_cram_guard as g
from scripts.guard_reference import ReferenceUnavailable


def test_no_tracked_file_gained_a_crammed_line_against_the_trunk():
    bad = g.violations()
    assert not bad, (
        "Statements were crammed onto shared lines; one statement per line, "
        "split the file instead of compressing it:\n" + "\n".join(bad)
    )


def test_trunk_sites_are_actually_read():
    """Pre-existing crammed lines exist on the trunk; a guard seeing none of
    them would be measuring nothing and passing vacuously."""
    paths = guard_reference.trunk_paths(".py")
    before = g.trunk_sites(paths)
    assert sum(before.values()) > 0, "no crammed lines seen on origin/main at all"


def test_detector_parses_rather_than_scans():
    src = (
        "from A import x; from B import y\n"            # 1 two imports
        "if cond: return call(1)\n"                     # 2 body on header
        "if open_: from X import drain; drain(a)\n"     # 3 the 57bbea4a shape
        "try:\n"
        "    a = 1\n"
        "except E: log()\n"                             # 6 handler body on header
        "else: go()\n"                                  # 7 else body on header
        "class Boom(Exception): pass\n"                 # 8 stub, not cramming
        "def f(self) -> int: ...\n"                     # 9 stub, not cramming
        'x = "a; b"  # c; d\n'                          # 10 text only, invisible
        "if ok:\n"
        "    pass\n"
        "elif other: stop()\n"                          # 13 elif body on header
        "while 1:\n"
        "    break\n"
        "else:\n"
        "    done()\n"                                  # split else: fine
    )
    assert [ln for ln, _ in g.crammed_lines(src)] == [1, 2, 3, 6, 7, 13]


def test_a_new_crammed_line_is_caught_and_a_pre_existing_one_is_not(monkeypatch):
    now = g.working_sites()
    assert now, "the tree has no pre-existing crammed lines; the ratchet premise changed"
    existing = next(iter(now))
    path = existing[0]
    fresh = (path, "<module>", "a = 1; b = 2")

    monkeypatch.setattr(g, "working_sites", lambda: now + Counter({fresh: 1}))
    monkeypatch.setattr(g, "trunk_sites", lambda paths: now)
    bad = g.violations()
    assert len(bad) == 1 and "a = 1; b = 2" in bad[0] and path in bad[0], bad
    assert existing[2] not in bad[0]  # the pre-existing site is never reported

    monkeypatch.setattr(g, "working_sites", lambda: now)
    assert g.violations() == []


def test_removing_one_site_cannot_pay_for_adding_another(monkeypatch):
    before = {("f.py", "<module>", "a = 1; b = 2"): 1}
    now = {("f.py", "<module>", "c = 3; d = 4"): 1}
    monkeypatch.setattr(g, "working_sites", lambda: now)
    monkeypatch.setattr(g, "trunk_sites", lambda paths: before)
    assert len(g.violations()) == 1


def test_it_refuses_when_the_trunk_cannot_be_read(tmp_path, monkeypatch):
    repo = tmp_path / "norepo"
    (repo / "pkg").mkdir(parents=True)
    (repo / "pkg" / "m.py").write_text("x = 1; y = 2\n")
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    subprocess.run(["git", "add", "pkg/m.py"], cwd=repo, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@example.invalid", "-c", "user.name=t",
         "commit", "-qm", "base"], cwd=repo, check=True,
    )
    monkeypatch.setattr(guard_reference, "ROOT", Path(repo))
    monkeypatch.setattr(g, "ROOT", Path(repo))

    with pytest.raises(ReferenceUnavailable) as exc:
        g.violations()
    assert "origin/main" in str(exc.value)
    assert g.main() == 2
