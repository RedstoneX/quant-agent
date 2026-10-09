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

FIXED ALLOW-LIST
----------------
Existing offenders are pinned by identity in the committed, shrink-only file
``config/check_allowlists/code_local_day.txt``. The guard fails on an offender
not in the list and on a listed entry that no longer occurs; it reads no git
ref, so an unrelated merge cannot redden it.
"""

from __future__ import annotations

import ast

from scripts import check_allowlist, guard_reference, local_day_guard


def test_offenders_match_the_fixed_allow_list():
    unlisted, stale = local_day_guard.check()
    assert not unlisted and not stale, check_allowlist.report(unlisted, stale, local_day_guard.ALLOWLIST)


def _listed(tmp_path, sites):
    path = tmp_path / "code_local_day.txt"
    path.write_text(check_allowlist.render("local-day", sites), encoding="utf-8")
    return path


_SITE = ("src/x.py", "local_today", "f", "date.today()")


def test_a_new_offender_not_in_the_list_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(local_day_guard, "working_offences", lambda: {_SITE: [3]})
    unlisted, stale = local_day_guard.check(_listed(tmp_path, []))
    assert len(unlisted) == 1 and "date.today()" in unlisted[0] and not stale


def test_a_listed_offender_passes(tmp_path, monkeypatch):
    monkeypatch.setattr(local_day_guard, "working_offences", lambda: {_SITE: [3]})
    assert local_day_guard.check(_listed(tmp_path, [_SITE])) == ([], [])


def test_a_stale_entry_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(local_day_guard, "working_offences", lambda: {})
    unlisted, stale = local_day_guard.check(_listed(tmp_path, [_SITE]))
    assert not unlisted and len(stale) == 1


def test_swapping_one_offender_for_another_still_fails(tmp_path, monkeypatch):
    """Identity is (path, kind, enclosing scope, source text): a swap nets zero but still bites."""
    other = ("src/x.py", "local_today", "g", "date.today()")
    monkeypatch.setattr(local_day_guard, "working_offences", lambda: {other: [9]})
    unlisted, stale = local_day_guard.check(_listed(tmp_path, [_SITE]))
    assert len(unlisted) == 1 and len(stale) == 1


def test_the_guard_reads_no_git_trunk():
    assert not hasattr(local_day_guard, "trunk_blobs") and not hasattr(local_day_guard, "trunk_offences")


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


def test_a_root_level_offender_is_not_in_the_list(tmp_path, monkeypatch):
    planted = "from datetime import date\n_X = date.today()\n"
    base = {("main.py", "local_today", "<module>", "date.today()"): [2]}
    monkeypatch.setattr(local_day_guard, "working_offences", lambda: base)
    unlisted, _ = local_day_guard.check(_listed(tmp_path, []))
    assert len(unlisted) == 1
    assert ("local_today", 2) in local_day_guard.scan_text("main.py", planted)
