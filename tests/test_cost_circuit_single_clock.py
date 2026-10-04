"""The cost circuit reads ONE clock, and nothing can add a second.

The midnight bug in this module was two clocks disagreeing: the ET day came
from Python, the stored stamps from SQLite's own `'now'`. `clock._now_utc` is
now the only clock read and `_connect` pins SQLite to it when it is replaced.
`scripts/local_day_guard.py` cannot see this shape (no `date.today()` is ever
written), so it is held here, structurally, by reading the package's own
source at check time. It stores nothing.
"""
from __future__ import annotations

import ast
import sqlite3
from datetime import datetime, time as dt_time, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import src.cost_circuit as cc
from src.cost_circuit.breaker import LLMCostCircuitBreaker

PKG = Path(cc.__file__).parent
_CLOCK_CALLS = {"now", "utcnow", "today", "et_today", "et_now",
                "todays_session_stamp", "fromtimestamp", "localtime", "gmtime"}


def clock_reads(text: str) -> list[tuple[str, int]]:
    """(scope-free) every clock-reading call in one module's source."""
    out = []
    for node in ast.walk(ast.parse(text)):
        if isinstance(node, ast.Call):
            fn = node.func
            name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", "")
            owner = fn.value.id if isinstance(fn, ast.Attribute) and isinstance(fn.value, ast.Name) else ""
            if name in _CLOCK_CALLS and (owner in {"datetime", "date", "time", "dt", "_dt"} or name.startswith(("et_", "todays"))):
                out.append((name, node.lineno))
    return out


def connects(text: str) -> list[int]:
    return [n.lineno for n in ast.walk(ast.parse(text)) if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute) and n.func.attr == "connect"
            and getattr(n.func.value, "id", "") == "sqlite3"]


def _sources() -> dict[str, str]:
    return {str(p.relative_to(PKG)): p.read_text() for p in sorted(PKG.rglob("*.py"))}


def test_the_only_clock_read_in_the_package_is_the_one_in_clock_py():
    srcs = _sources()
    assert len(srcs) > 10, "package not found; the scan would pass vacuously"
    stray = {f: clock_reads(t) for f, t in srcs.items() if f != "clock.py" and clock_reads(t)}
    assert not stray, f"a second clock in the cost circuit: {stray}; read `_now_utc()` instead"
    assert len(clock_reads(srcs["clock.py"])) == 1, "clock.py must hold exactly one clock read"


def test_the_only_database_connections_are_the_pinned_one_and_the_memory_keeper():
    stray = {f: connects(t) for f, t in _sources().items() if f != "breaker.py" and connects(t)}
    assert not stray, f"a connection that bypasses `_connect`'s clock pin: {stray}"
    assert len(connects(_sources()["breaker.py"])) == 2


def test_the_scanner_sees_a_second_clock_and_a_bare_connection():
    assert clock_reads("from datetime import datetime\nx = datetime.now()\n")
    assert clock_reads("from datetime import date\nx = date.today()\n")
    assert connects("import sqlite3\nc = sqlite3.connect('x')\n")


def test_sqlite_now_follows_the_replaced_clock_across_an_et_midnight(monkeypatch):
    """With the clock set to 00:05 ET on a day far from today, SQL `'now'` and
    the ET day must be that same instant -- two clocks would give today's."""
    et = ZoneInfo("America/New_York")
    pinned = datetime.combine(datetime(2099, 1, 2).date(), dt_time(0, 5), tzinfo=et).astimezone(timezone.utc)
    monkeypatch.setattr(cc, "_now_utc", lambda: pinned)
    circuit = LLMCostCircuitBreaker(":memory:", type("C", (), {})(), None)
    conn = circuit._connect()
    try:
        stamp = conn.execute("SELECT datetime('now')").fetchone()[0]
        stamp2 = conn.execute("SELECT CURRENT_TIMESTAMP").fetchone()[0]
    finally:
        conn.close()
    assert stamp == stamp2 == pinned.strftime("%Y-%m-%d %H:%M:%S")
    assert cc._et_day_and_utc_bounds()[0] == "2099-01-02"


def test_local_day_guard_sees_datetime_today_and_naive_fromtimestamp():
    from scripts import local_day_guard as g
    kinds = lambda src: [k for k, _ in g.scan_text("src/x.py", src)]
    assert kinds("from datetime import datetime\nx = datetime.today()\n") == ["local_today"]
    assert kinds("from datetime import datetime\nx = datetime.fromtimestamp(5)\n") == ["naive_now"]
    assert kinds("from datetime import datetime, timezone\nx = datetime.fromtimestamp(5, tz=timezone.utc)\n") == []
