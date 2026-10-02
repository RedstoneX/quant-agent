"""An exchange day must be compared to an exchange day -- never to the runner's.

WHAT KEEPS GOING WRONG
----------------------
This repo has now hit the same class of bug at least four times: a trading
day derived from one clock is compared against a day derived from another.
The runner is in UTC; the exchange is in US/Eastern; and a stamp taken at
one moment is compared to a day read at a later one. Each time, a window of
tests reds on the SAME commit for a quarter-hour around ET midnight, every
open change goes red at once, and a full test round is lost.

The two shapes recorded on 2026-10-02 (the first reproduced on demand by
shifting the clock across ET midnight between collection and run; the second
is the cost circuit's own documented quarter-hour flake, see
`src/cost_circuit/clock.py`):

* a TEST stamps "today" at import (module-level `str(et_today())`) while the
  code under test reads `et_today()` at run time -- a suite that collects
  before ET midnight and runs the file after it compares two exchange days;
* a TEST backdates a row by `now - 16 minutes` on the real clock and asserts
  it is still "today" -- which is false for the first sixteen minutes of
  every ET day.

The forbidden set below is derived by inspecting those incidents and the
earlier ones (`tests/session_clock.py`, `tests/test_no_clock_dependent_price_
stamps.py`, `tests/desk_clock.py`). It is deliberately an AST scan, not a
grep: the indirect spellings are the ones that slipped before.

WHAT IS FORBIDDEN
-----------------
In `src/` and `tests/`:

  local_today        `date.today()` / `datetime.date.today()` -- the runner's
                     local calendar day, never an exchange day.
  naive_now          `datetime.now()` with no timezone -- the runner's local
                     wall clock; its `.date()` is the local day.
  utc_day            `.date()` taken directly off `datetime.now(<utc>)` or
                     `utcnow()` -- a UTC calendar day used as a day.
In `tests/` only:
  import_time_stamp  a module-level assignment whose value reads the clock
                     (`et_today()`, `et_now()`, `todays_session_stamp()`,
                     `datetime.now(...)`) -- a stamp frozen at collection and
                     compared against a run-time read.

HOW TO EXTEND IT
----------------
Add a new shape to `_classify` with a one-word kind, re-run, and copy the
new offenders into `_BASELINE` as the adopted count for that kind. Never add
a key for new code: the allowlist only shrinks. When a listed count drops,
lower the number; when a key reaches zero, delete it.

THE BASELINE
------------
Existing offenders are adopted as-is, per file and kind, as a SHRINK-ONLY
allowlist: a file may have fewer of a kind than listed, never more, and a
file not listed may have none. What is on it is what the repo contained on
2026-10-02 -- these are candidates for the same fix, not approvals.
"""
from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCAN_DIRS = ("src", "tests")

_CLOCK_READERS = {"et_today", "et_now", "todays_session_stamp",
                  "todays_session_bar_stamp", "todays_session_snapshot_stamps"}


def _name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


def _is_now_call(node: ast.AST) -> bool:
    return isinstance(node, ast.Call) and _name(node.func) in {"now", "utcnow"}


def _classify_call(call: ast.Call) -> str | None:
    fn = call.func
    # date.today() / datetime.date.today()
    if isinstance(fn, ast.Attribute) and fn.attr == "today" and _name(fn.value) == "date":
        return "local_today"
    # datetime.now() with no tz
    if isinstance(fn, ast.Attribute) and fn.attr == "now" and _name(fn.value) == "datetime" \
            and not call.args and not call.keywords:
        return "naive_now"
    # <now(...)|utcnow()>.date()
    if isinstance(fn, ast.Attribute) and fn.attr == "date" and _is_now_call(fn.value):
        inner = fn.value
        if _name(inner.func) == "utcnow" or not inner.args and not inner.keywords:
            return "utc_day"
        tz = _name(inner.args[0]) if inner.args else _name(inner.keywords[0].value)
        if tz.lower() in {"utc", "timezone"} or "utc" in tz.lower():
            return "utc_day"
    return None


def _reads_clock(expr: ast.AST) -> bool:
    for sub in ast.walk(expr):
        if isinstance(sub, ast.Call) and (_name(sub.func) in _CLOCK_READERS or _is_now_call(sub)):
            return True
    return False


def scan(path: Path) -> list[tuple[str, int]]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            kind = _classify_call(node)
            if kind:
                found.append((kind, node.lineno))
    if path.parts[-len(path.relative_to(ROOT).parts)] == "tests":
        for node in tree.body:  # module level only
            if isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value is not None \
                    and _reads_clock(node.value):
                found.append(("import_time_stamp", node.lineno))
    return found


def collect() -> dict[tuple[str, str], list[int]]:
    out: dict[tuple[str, str], list[int]] = {}
    for d in SCAN_DIRS:
        for path in sorted((ROOT / d).rglob("*.py")):
            if path.name == Path(__file__).name:
                continue
            for kind, line in scan(path):
                out.setdefault((str(path.relative_to(ROOT)), kind), []).append(line)
    return out


_BASELINE: dict[tuple[str, str], int] = {
    ('src/credentials.py', 'utc_day'): 1,
    ('src/execution/broker_parts/trade_stream.py', 'local_today'): 4,
    ('tests/test_bugfixes.py', 'local_today'): 3,
    ('tests/test_congressional_trading.py', 'import_time_stamp'): 1,
    ('tests/test_db.py', 'local_today'): 2,
    ('tests/test_db.py', 'utc_day'): 1,
    ('tests/test_evening_analyst_v2.py', 'local_today'): 5,
    ('tests/test_insider_purchase_cluster.py', 'local_today'): 3,
    ('tests/test_insider_signal.py', 'local_today'): 9,
    ('tests/test_nominations.py', 'local_today'): 5,
    ('tests/test_pm_grounding.py', 'local_today'): 3,
    ('tests/test_proposed_count_scoped_to_pm.py', 'naive_now'): 3,
    ('tests/test_rehearsal_feed_replay.py', 'import_time_stamp'): 1,
    ('tests/test_smart_money.py', 'local_today'): 6,
    ('tests/test_status_board.py', 'local_today'): 2,
    ('tests/test_weekly_review.py', 'local_today'): 1,
}


def test_no_local_day_is_used_where_an_exchange_day_is_meant():
    found = collect()
    grown = []
    for key, lines in found.items():
        allowed = _BASELINE.get(key, 0)
        if len(lines) > allowed:
            grown.append(f"{key[0]} [{key[1]}] lines {lines}: {len(lines)} found, {allowed} allowed")
    assert not grown, (
        "a local or import-time day is compared where an exchange day is meant; "
        "read the exchange day at the moment of comparison (src.trading_calendar."
        "et_today / tests.desk_clock.freeze_desk_day) instead:\n  " + "\n  ".join(grown)
    )


def test_the_baseline_only_shrinks():
    found = collect()
    stale = [f"{k[0]} [{k[1]}]: {v} allowed, {len(found.get(k, []))} found"
             for k, v in _BASELINE.items() if len(found.get(k, [])) < v]
    assert not stale, "lower these baseline counts -- the allowlist only shrinks:\n  " + "\n  ".join(stale)


def test_the_scanner_sees_each_forbidden_shape():
    src = (
        "from datetime import date, datetime, timezone\n"
        "a = date.today()\n"
        "b = datetime.now()\n"
        "c = datetime.now(timezone.utc).date()\n"
        "d = datetime.utcnow().date()\n"
        "e = datetime.now(UTC).date()\n"
    )
    kinds = sorted(k for k, _ in (
        (_classify_call(n), 0) for n in ast.walk(ast.parse(src)) if isinstance(n, ast.Call)
    ) if k)
    assert kinds == ["local_today", "naive_now", "utc_day", "utc_day", "utc_day"], kinds
    tree = ast.parse("from src.trading_calendar import et_today\nX = {'date': str(et_today())}\n")
    assert _reads_clock(tree.body[1].value)
