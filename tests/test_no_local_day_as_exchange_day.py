"""An exchange day must be compared to an exchange day -- never to the runner's.

WHAT KEEPS GOING WRONG
----------------------
This repo has now hit the same class of bug at least four times: a trading
day derived from one clock is compared against a day derived from another.
The runner is in UTC; the exchange is in US/Eastern; and a stamp taken at
one moment is compared to a day read at a later one. Each time, a window of
tests reds on the SAME commit for a quarter-hour around ET midnight, every
open change goes red at once, and a full test round is lost.

The forbidden shapes, and the scanner that finds them, live in
``scripts/local_day_guard.py``.

NO STORED OFFENDER LIST
-----------------------
This file used to carry a hardcoded ``_BASELINE`` of every current offender.
That was a cached measurement committed to the repo -- the same collision
engine as the JSON baselines, written in Python -- and every change touching a
listed file had to edit it. The guard now stores nothing
(docs/GUARDS_WITHOUT_STORED_STATE.md): it names each offending site in this
working tree, names them again on ``origin/main`` at check time, and fails on
any site identity the tree holds that the trunk does not -- never a total, so
removing one offender never licenses adding a different one. If ``origin/main`` cannot be read it REFUSES; it never passes by default.
"""
from __future__ import annotations

import ast
import subprocess
from pathlib import Path

import pytest

from scripts import guard_reference, local_day_guard
from scripts.guard_reference import ReferenceUnavailable


def test_no_local_day_is_used_where_an_exchange_day_is_meant():
    bad = local_day_guard.violations()
    assert not bad, (
        "a local or import-time day is compared where an exchange day is meant; "
        "read the exchange day at the moment of comparison (src.trading_calendar."
        "et_today / tests.desk_clock.freeze_desk_day) instead:\n  " + "\n  ".join(bad)
    )


def test_the_trunk_is_actually_measured():
    """A guard that measured nothing on the trunk would pass vacuously."""
    paths = local_day_guard.scanned_paths()
    assert len(paths) > 100, f"only {len(paths)} scanned .py files seen"
    before = local_day_guard.trunk_offences(paths)
    assert before, "no offender measured on origin/main -- the reference read is dead"


def test_a_new_offender_is_caught_and_reported_as_a_delta(monkeypatch):
    """Pretend the trunk copy had one fewer: the guard must report the delta."""
    now = local_day_guard.working_offences()
    assert now, "nothing to compare against"
    key = sorted(now)[0]
    real = local_day_guard.trunk_offences

    def fewer(paths):
        counts = real(paths)
        counts[key] = max(0, counts.get(key, 0) - 1)
        return counts

    monkeypatch.setattr(local_day_guard, "trunk_offences", fewer)
    bad = local_day_guard.violations()
    assert any(key[0] in line and f"[{key[1]}]" in line and "+1" in line for line in bad), bad


def test_removing_one_offender_and_adding_another_still_fails(monkeypatch):
    """The count hole: -1 old +1 new in the same file and kind nets to zero.

    Identity is (path, kind, enclosing scope, source text), so it must still fail.
    """
    now = local_day_guard.working_offences()
    assert now, "nothing to compare against"
    path, kind, _scope, src = old = sorted(now)[0]
    new = (path, kind, "_added_for_proof", src)
    pretend = {site: lines for site, lines in now.items() if site != old}
    pretend[new] = [1]
    monkeypatch.setattr(local_day_guard, "working_offences", lambda: pretend)
    bad = local_day_guard.violations()
    assert len(bad) == 1 and path in bad[0] and "in _added_for_proof" in bad[0], bad


def test_removing_offenders_alone_passes(monkeypatch):
    monkeypatch.setattr(local_day_guard, "working_offences", lambda: {})
    assert local_day_guard.violations() == []


def test_it_refuses_when_the_trunk_cannot_be_read(tmp_path, monkeypatch):
    repo = tmp_path / "norepo"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "m.py").write_text("x = 1\n")
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    subprocess.run(["git", "add", "src/m.py"], cwd=repo, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@example.invalid", "-c", "user.name=t",
         "commit", "-qm", "base"], cwd=repo, check=True,
    )
    monkeypatch.setattr(guard_reference, "ROOT", Path(repo))
    monkeypatch.setattr(local_day_guard, "ROOT", Path(repo))

    with pytest.raises(ReferenceUnavailable) as exc:
        local_day_guard.violations()
    assert "origin/main" in str(exc.value)
    assert local_day_guard.main() == 2


def test_the_scanner_sees_each_forbidden_shape():
    src = (
        "from datetime import date, datetime, timezone\n"
        "a = date.today()\n"
        "b = datetime.now()\n"
        "c = datetime.now(timezone.utc).date()\n"
        "d = datetime.utcnow().date()\n"
        "e = datetime.now(UTC).date()\n"
    )
    kinds = sorted(k for k, _ in local_day_guard.scan_text("src/x.py", src))
    assert kinds == ["local_today", "naive_now", "utc_day", "utc_day", "utc_day"], kinds
    stamped = "from src.trading_calendar import et_today\nX = {'date': str(et_today())}\n"
    assert local_day_guard._reads_clock(ast.parse(stamped).body[1].value)
    assert ("import_time_stamp", 2) in local_day_guard.scan_text("tests/t.py", stamped)
    assert ("import_time_stamp", 2) not in local_day_guard.scan_text("src/t.py", stamped)


def test_the_scan_set_is_every_tracked_module_not_a_directory_list():
    paths = local_day_guard.scanned_paths()
    assert "main.py" in paths, "the live entry point at the repository root must be scanned"
    assert any(p.startswith("ops/") for p in paths) and any(p.startswith("scripts/") for p in paths)
    assert paths == sorted(set(guard_reference.working_paths("*.py")))


def test_a_root_level_offender_is_a_delta(monkeypatch):
    planted = "from datetime import date\n_X = date.today()\n"
    base = local_day_guard.working_offences()
    base[("main.py", "local_today", "<module>", "date.today()")] = [2]
    monkeypatch.setattr(local_day_guard, "working_offences", lambda: base)
    assert local_day_guard.main() == 1
    assert ("local_today", 2) in local_day_guard.scan_text("main.py", planted)
