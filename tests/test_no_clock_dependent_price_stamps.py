"""A synthetic "today's print" stamp must not depend on what time it is run.

WHAT WENT WRONG
---------------
`src/data/live_price.py::resolve_live_price` counts a point-in-time stamp as
this session's only when its ET date is today AND it is at or after 09:30 ET.
Several test helpers stamped their synthetic snapshots with `et_now()`, which
satisfies the date bound but not the open bound for the fourteen and a half
hours a day outside the regular session.

The result was a suite that passed whenever CI happened to run between 09:30
and 16:00 ET and failed with 22 red tests at every other hour — on the same
commit, with no change in between. That is the worst shape a test defect can
take: it is not reproducible on demand, it looks like a regression in the
code under test, and it had been latent long enough that nobody associated it
with the clock.

WHAT THIS FILE DOES ABOUT IT
----------------------------
Two guards, because neither alone is enough.

1. A STATIC scan of the whole `tests/` tree. It fails, by name and line, on
   any synthetic snapshot field the resolver judges (`last_trade_at`,
   `minute_bar_at`) that is stamped from a live clock — whether the clock
   call is written inline, reached through a local variable, or passed as a
   keyword argument. This is the one that catches a NEW test written the old
   way, at the moment it is written, at any hour.

   The indirect forms are not an afterthought: the original defect WAS the
   indirect form (`trade_at = et_now()` two lines above `"last_trade_at":
   trade_at`), and a scan that only matched the inline call would have missed
   the file that produced twenty of the twenty-two failures. `_clock_bound_
   names` exists for exactly that reason.

2. A BEHAVIOURAL guard that pins the actual mechanism: it drives the real
   resolver at a pre-open instant and shows that the shared helper's stamp
   still resolves as this session's while a bare `now` does not. This is the
   one that would catch the static scan going blind — if the resolver's rule
   or the helper's time ever changed, the scan would still pass and this
   would not.

Neither guard asserts anything about production behaviour, and neither
weakens the resolver's rule. The 09:30 bound is deliberate and documented
(the desk rejected date-equality alone on 2026-09-18 because a pre-market
print "could sit on the wrong side of the real price on a gap day"); the
defect was always in the test helpers, never in the resolver.
"""

from __future__ import annotations

import ast
from datetime import datetime, timedelta
from pathlib import Path

from src.data.live_price import SOURCE_LAST_TRADE, resolve_live_price
from src.trading_calendar import ET, REGULAR_SESSION_CLOSE_MIN, REGULAR_SESSION_OPEN_MIN
from tests import _shared_ast_cache
from tests.session_clock import (
    IN_SESSION_MIN,
    todays_session_bar_stamp,
    todays_session_snapshot_stamps,
    todays_session_stamp,
)

TESTS_ROOT = Path(__file__).resolve().parent

#: Snapshot fields `resolve_live_price` judges with the date-AND-open rule.
#: `session_bar_at` is deliberately absent: a daily bar is DATED, not
#: stamped, and is tested by date-equality alone, so 00:00 ET is correct for
#: it at any hour.
POINT_IN_TIME_FIELDS = frozenset({"last_trade_at", "minute_bar_at"})

#: Calls that return "right now" and are therefore pre-open for most of the
#: day. Matched on the function/attribute name, so `et_now()`,
#: `datetime.now(...)`, `dt.now(...)` and `datetime.datetime.utcnow()` all
#: reduce to one of these. `date.today()` is left out on purpose: it yields a
#: naive value, which `live_price_is_today` rejects at EVERY hour — a test
#: written that way is broken loudly and constantly, not silently and only
#: before the open, so it is not this guard's failure mode.
LIVE_CLOCK_CALLS = frozenset({"et_now", "now", "utcnow"})

#: Paths, relative to `tests/`, exempt from the scan: the helper that is
#: allowed to read the live clock precisely because it then moves the time
#: inside the session, and this file, which names the bad pattern in order
#: to forbid it. Matched on the full relative path, not the basename, so a
#: new file elsewhere in the tree cannot inherit the exemption by name.
ALLOWED = frozenset({"session_clock.py", "test_no_clock_dependent_price_stamps.py"})


def _reads_the_live_clock(node: ast.AST) -> str | None:
    """The clock function's name if `node` evaluates to "right now", else None.

    `.replace(hour=...)` on top of a clock call is NOT a live read: that is
    the author deliberately pinning the time of day, which is the fix, not
    the defect.
    """
    if not isinstance(node, ast.Call):
        return None
    func = node.func
    if isinstance(func, ast.Attribute):
        if func.attr == "replace" and any(kw.arg == "hour" for kw in node.keywords):
            return None
        if func.attr in LIVE_CLOCK_CALLS:
            return func.attr
        # e.g. `et_now().astimezone(...)` — still a live read underneath.
        return _reads_the_live_clock(func.value)
    if isinstance(func, ast.Name) and func.id in LIVE_CLOCK_CALLS:
        return func.id
    return None


def _unwrap(node: ast.AST) -> list[ast.AST]:
    """The value nodes a conditional/arithmetic expression can evaluate to."""
    if isinstance(node, ast.IfExp):
        return [node.body, node.orelse]
    if isinstance(node, ast.BoolOp):
        return list(node.values)
    if isinstance(node, ast.BinOp):  # `now - timedelta(...)` is still clock-derived
        return [node.left, node.right]
    return [node]


def _clock_bound_names(tree: ast.AST) -> dict[str, str]:
    """{variable name: clock call} for every local bound to "right now".

    Transitive, so `now = et_now()` followed by `trade_at = now` marks both.
    Deliberately file-scoped rather than scope-aware: a false positive costs
    one explicit datetime at a call site, a false negative costs fourteen
    hours a day of silent wrongness.
    """
    bound: dict[str, str] = {}
    for _ in range(4):  # cheap fixed-point; chains this long do not occur
        before = len(bound)
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                continue
            value = node.value
            if value is None:
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            names = [t.id for t in targets if isinstance(t, ast.Name)]
            if not names:
                continue
            for candidate in _unwrap(value):
                call = _reads_the_live_clock(candidate)
                if call is None and isinstance(candidate, ast.Name):
                    call = bound.get(candidate.id)
                if call is not None:
                    for name in names:
                        bound.setdefault(name, call)
                    break
        if len(bound) == before:
            break
    return bound


def _offence(value: ast.AST, bound: dict[str, str]) -> str | None:
    """How `value` reads the live clock, if it does."""
    for candidate in _unwrap(value):
        call = _reads_the_live_clock(candidate)
        if call is not None:
            return f"{call}()"
        if isinstance(candidate, ast.Name) and candidate.id in bound:
            return f"{candidate.id} (= {bound[candidate.id]}())"
    return None


def _offences_in(tree: ast.AST) -> list[tuple[int, str, str]]:
    """(line, field, how) for every live-clock point-in-time stamp."""
    bound = _clock_bound_names(tree)
    found: list[tuple[int, str, str]] = []

    def check(lineno: int, field: str, value: ast.AST) -> None:
        how = _offence(value, bound)
        if how is not None:
            found.append((lineno, field, how))

    for node in ast.walk(tree):
        # {"last_trade_at": <clock>}
        if isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values):
                if isinstance(key, ast.Constant) and key.value in POINT_IN_TIME_FIELDS:
                    check(key.lineno, str(key.value), value)
        # f(last_trade_at=<clock>)
        elif isinstance(node, ast.Call):
            for kw in node.keywords:
                if kw.arg in POINT_IN_TIME_FIELDS:
                    check(kw.value.lineno, kw.arg, kw.value)
        # snap["last_trade_at"] = <clock>
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if (
                    isinstance(target, ast.Subscript)
                    and isinstance(target.slice, ast.Constant)
                    and target.slice.value in POINT_IN_TIME_FIELDS
                ):
                    check(node.lineno, str(target.slice.value), node.value)
    return found


def test_no_test_stamps_a_point_in_time_price_field_from_the_live_clock():
    """The static guard: catches the old pattern where it is written.

    A stamp built from `et_now()` is on today's DATE but before today's
    09:30 OPEN whenever the suite runs outside the session, so the resolver
    correctly reads it as not-this-session and the test silently means the
    opposite of what it says. Build it with
    `tests.session_clock.todays_session_stamp()` instead.
    """
    offences: list[str] = []
    for path, _source, tree in _shared_ast_cache.parse_tree(TESTS_ROOT):
        relative = path.relative_to(TESTS_ROOT)
        if str(relative) in ALLOWED:
            continue
        for line, field, how in _offences_in(tree):
            offences.append(f"{relative}:{line}: {field} = {how}")

    assert not offences, (
        "these synthetic price stamps are built from the live clock, so they "
        "are BEFORE today's 09:30 ET open — and therefore not this session's "
        "price — whenever the suite runs outside market hours:\n  "
        + "\n  ".join(sorted(offences))
        + "\n\nUse `from tests.session_clock import todays_session_stamp` so "
        "the stamp lands inside today's session at any hour. If the test "
        "genuinely means a stale or pre-open price, write that datetime out "
        "explicitly at the call site instead."
    )


def test_the_static_scan_catches_the_indirect_form_that_caused_the_outage():
    """The scan's own regression test — the case an earlier draft missed.

    The real defect was never the inline `{"last_trade_at": et_now()}`; it
    was a helper that assigned the clock to a local first. A scan blind to
    that would have passed on the very file that produced twenty of the
    twenty-two failures, which is why this is pinned rather than trusted.
    """
    indirect = ast.parse(
        "def helper():\n"
        "    now = et_now()\n"
        "    trade_at = now\n"
        "    return {'last_price': 1.0, 'last_trade_at': trade_at}\n"
    )
    keyword = ast.parse("snap = _snap(last_trade_at=datetime.now(ET))\n")
    subscript = ast.parse("snap['minute_bar_at'] = et_now()\n")
    arithmetic = ast.parse(
        "def helper():\n"
        "    stamp = et_now() - timedelta(minutes=5)\n"
        "    return {'last_trade_at': stamp}\n"
    )
    for name, tree in (
        ("indirect", indirect),
        ("keyword", keyword),
        ("subscript", subscript),
        ("arithmetic", arithmetic),
    ):
        assert _offences_in(tree), f"the scan is blind to the {name} form"

    # ...and does NOT fire on a deliberately pinned time of day, which is the
    # fix. A false positive here would push authors back to `et_now()`.
    pinned = ast.parse(
        "def helper():\n"
        "    stamp = et_now().replace(hour=10, minute=0)\n"
        "    return {'last_trade_at': stamp}\n"
    )
    explicit = ast.parse(
        "snap = {'last_trade_at': datetime(2026, 9, 16, 15, 59, tzinfo=ET)}\n"
    )
    assert not _offences_in(pinned)
    assert not _offences_in(explicit)


def _pre_open_instant() -> datetime:
    """Today, well before the open — the window the whole defect lived in."""
    return datetime.now(ET).replace(hour=3, minute=0, second=0, microsecond=0)


def test_the_shared_stamp_is_this_sessions_price_even_at_a_pre_open_clock():
    """The behavioural guard: the real resolver, at 03:00 ET, on the helper.

    `when` is passed explicitly, so this asserts the same thing whatever
    hour the suite actually runs at.
    """
    trade_at, bar_at = todays_session_snapshot_stamps()
    snapshot = {
        "last_price": 110.0,
        "prev_close": 100.0,
        "last_trade_at": trade_at,
        "session_bar_at": bar_at,
    }

    resolved = resolve_live_price(snapshot, when=_pre_open_instant())

    assert resolved.is_today_print, resolved.unavailable
    assert resolved.source == SOURCE_LAST_TRADE
    assert resolved.price == 110.0
    assert resolved.session_bar_is_today is True


def test_a_bare_now_stamp_is_not_this_sessions_price_before_the_open():
    """The counter-example that proves the guard above is testing something.

    This is the exact payload the helpers used to build, evaluated at the
    hour they used to build it at. If this ever starts resolving as a
    today-print, the resolver's rule has changed and both the helper and its
    docstring need re-reading — not this test relaxing.
    """
    pre_open = _pre_open_instant()
    snapshot = {"last_price": 110.0, "prev_close": 100.0, "last_trade_at": pre_open}

    resolved = resolve_live_price(snapshot, when=pre_open)

    assert not resolved.is_today_print
    assert resolved.price is None
    assert resolved.unavailable is not None


def test_the_shared_stamps_agree_about_which_day_it_is():
    """Both halves of one snapshot must be dated to the same session.

    `todays_session_snapshot_stamps` reads the clock once for this reason:
    two independent reads either side of ET midnight would hand a caller a
    trade stamp and a bar stamp on different dates, which is the disagreement
    board item 120 exists to prevent.
    """
    trade_at, bar_at = todays_session_snapshot_stamps()
    assert trade_at.date() == bar_at.date()
    assert bar_at.hour == 0 and bar_at.minute == 0
    assert trade_at.hour * 60 + trade_at.minute == IN_SESSION_MIN


def test_the_shared_stamp_sits_strictly_inside_the_regular_session():
    """Pins the helper's contract against the exchange bounds, not itself."""
    stamp = todays_session_stamp()
    assert stamp.tzinfo is not None

    minute_of_day = stamp.hour * 60 + stamp.minute
    assert REGULAR_SESSION_OPEN_MIN < minute_of_day < REGULAR_SESSION_CLOSE_MIN

    # The bar stamp is before the open by construction, and must stay that
    # way — it is dated, not stamped, and the 09:30 bound does not apply.
    bar = todays_session_bar_stamp()
    assert bar.hour * 60 + bar.minute < REGULAR_SESSION_OPEN_MIN
    assert bar < stamp

    # The stamp is today's DATE, which is what makes it survive the
    # resolver's date-equality test against the real clock. Read from one
    # instant so the assertion cannot straddle ET midnight.
    now = datetime.now(ET)
    assert stamp.date() in (now.date(), (now + timedelta(seconds=2)).date())
