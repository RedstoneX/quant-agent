"""A module may not derive a day in Python and also date rows with SQL 'now'.

Rule and rationale: scripts/one_clock_guard.py. Stores no baseline; compares
against origin/main at check time.
"""
from __future__ import annotations

from scripts import one_clock_guard as g

MIXED = (
    "from src.trading_calendar import et_today\n"
    "def f(c):\n"
    "    d = et_today()\n"
    "    return c.execute(\"SELECT 1 FROM t WHERE date(timestamp) = date('now')\")\n"
)


def test_tree_adds_no_mixed_clock_site():
    assert g.violations() == []


def test_trunk_is_measured_and_mixed_modules_exist():
    assert g.trunk_sites(g.scanned_paths()), "reference read is dead or nothing measured"


def test_planted_mixed_module_is_caught(monkeypatch):
    now = g.working_sites()
    for kind, line, scope, src in g.scan_sites("src/planted.py", MIXED):
        now[("src/planted.py", kind, scope, src)] = [line]
    monkeypatch.setattr(g, "working_sites", lambda: now)
    bad = g.violations()
    assert any("src/planted.py" in b and "[py_day]" in b for b in bad), bad
    assert any("src/planted.py" in b and "[sql_now]" in b for b in bad), bad


def test_removing_the_planted_module_passes():
    assert g.violations() == []


def test_one_clock_alone_is_not_flagged():
    assert g.scan_sites("src/a.py", MIXED.replace("et_today()", "1")) == []
    assert g.scan_sites("src/a.py", MIXED.replace("date('now')", "?")) == []


def test_docstring_mentions_are_ignored():
    src = 'def f():\n    """uses datetime(\'now\')"""\n    return et_today()\n'
    assert g.scan_sites("src/a.py", src) == []
