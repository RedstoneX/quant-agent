from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from pathlib import Path
from typing import Any
from src.alert_claims import load_state, repair_failure_alert_day, save_state


# ---------------------------------------------------------------------------
# a refusal that is the TAPE's, and a refusal that is the desk's
# ---------------------------------------------------------------------------
# Measured, production log, all retained rotations (2026-08-21..2026-09-23):
# the stop-repair refusal `no_trade_print_today` occurred 6 times on 4
# sessions -- NET and RSG on 09-18, BRK-B, NUE and RSG on 09-21, RSG on
# 09-23. Every one was raised between 13:30:43 and 13:30:45 UTC, inside 45
# seconds of the opening bell, and every one was resolved in the same
# session. Not one survived to a second observation.
#
# The refusal itself is right and is NOT changed here: a stop priced off a
# quote the tape never confirmed can fire immediately
# (`src/execution/stop_repair.py`). What was wrong is WHICH pass pages. The
# first attempt, seconds after the bell, paged the owner in red and told him
# to place a stop by hand -- for a name that had simply not printed yet, on
# a desk whose own next pass places it.
#
# NO CONSTANT, and deliberately none. docs/OUTCOME.md forbids fitting a
# threshold to this desk's own past record ("an arbitrary number with a
# backtest stapled to it") and requires reformulating the rule so it needs
# no number at all before anything else is tried. A sweep count or a delay
# fitted to the six observations above would have been exactly the banned
# shape. What is named below is a CONDITION, re-read from the tape every
# pass: has this name printed today, and is anything still standing watch
# over the position. The measurement above is why the condition is worth
# naming; it is not the source of any number, because there is none.
#
# THE RETRY IS NOT DEFERRED. docs/INCIDENT_HISTORY.md (2026-09-18) records
# "waiting for a later check of the day to retry" as explicitly rejected,
# because waiting adds unprotected time. That ruling stands and is not
# touched: every pass still attempts the repair exactly as before, at the
# same cadence, with the same guards. What waits is the PAGE, not the fix.
#
# A refusal for any other reason is a fault on its first observation and
# still pages immediately -- a broker rejection with retries exhausted
# (2026-09-16, BRK-B), a corrupt recorded stop level, a level the tape has
# already passed. None of those resolves by the tape catching up.

#: Refusal codes (`_refuse(..., code=...)` in `src/execution/stop_repair.py`)
#: that say the TAPE has not produced a price yet, rather than that the desk
#: failed at something. An allowlist on purpose: a refusal code added later
#: pages on its first observation, exactly as every code does today, until
#: somebody looks at it and puts it here deliberately.
AWAITING_FIRST_PRINT_CODES: frozenset[str] = frozenset({"no_trade_print_today"})


def awaiting_first_print(
    *,
    refusal_code: str,
    still_covered: bool,
    market_open: bool,
) -> bool:
    """Whether a repair that did not place is waiting on the TAPE rather than
    reporting a failure of the desk. Pure; reads no state and no clock.

    All three terms are load-bearing:

    * `refusal_code` in `AWAITING_FIRST_PRINT_CODES` -- the desk declined
      because this name has no confirmed print today, not because anything
      it tried was refused.
    * `still_covered` -- the durable whole-share GTC leg is standing watch
      over the rest of the position. It is the whole reason waiting is
      tolerable, so a position with NOTHING covering it is never in this
      state and pages on the first attempt, whatever the refusal said.
    * `market_open` -- the session still has passes to come, and each one
      re-reads the tape. Once the market is shut there is no later pass to
      resolve it and the quiet state has run out; see
      `session_awaiting_print_symbols`, which is what makes the silence
      end rather than simply lapse into the expected overnight bucket.
    """
    return bool(market_open) and bool(still_covered) and str(refusal_code or "") in AWAITING_FIRST_PRINT_CODES


def _awaiting_print_state(state: dict[str, Any], day: str) -> set[str]:
    raw = state.get("awaiting_first_print_symbols")
    if not isinstance(raw, dict) or raw.get("day") != day:
        return set()
    return {str(sym).strip().upper() for sym in (raw.get("symbols") or []) if str(sym).strip()}


def _write_awaiting_print_state(
    state: dict[str, Any],
    day: str,
    symbols: set[str],
) -> None:
    state["awaiting_first_print_symbols"] = {
        "day": day,
        "symbols": sorted(symbols),
    }


def note_awaiting_first_print(
    symbol: str,
    *,
    now: datetime | None = None,
    path: Path | None = None,
    state: dict[str, Any] | None = None,
) -> None:
    """Record that `symbol` spent a pass of THIS session waiting for its
    first print with its sub-share remainder uncovered.

    The quiet state above has to end somewhere or it is a suppression. This
    is what ends it: a name still on this list when the market is shut never
    got its stop back all session, which is not the ratified overnight lapse
    (that one is a stop that was placed and expired at 16:00 by design) and
    must not be filed as one.
    """
    sym = str(symbol or "").strip().upper()
    if not sym:
        return
    day = repair_failure_alert_day(now)
    own_state = state is None
    st = load_state(path) if own_state else state
    current = _awaiting_print_state(st, day)
    if sym in current:
        return
    _write_awaiting_print_state(st, day, current | {sym})
    if own_state:
        save_state(st, path)


def clear_awaiting_first_print(
    symbols: Iterable[str],
    *,
    now: datetime | None = None,
    path: Path | None = None,
    state: dict[str, Any] | None = None,
) -> None:
    """Forget the awaiting-print state for names whose gap has closed."""
    day = repair_failure_alert_day(now)
    own_state = state is None
    st = load_state(path) if own_state else state
    current = _awaiting_print_state(st, day)
    wanted = {str(raw).strip().upper() for raw in symbols if str(raw).strip()}
    if not (current & wanted):
        return
    _write_awaiting_print_state(st, day, current - wanted)
    if own_state:
        save_state(st, path)


def session_awaiting_print_symbols(
    *,
    now: datetime | None = None,
    path: Path | None = None,
    state: dict[str, Any] | None = None,
) -> set[str]:
    """The names that waited on a first print at some point today and have
    not been repaired since. Read by the market-shut pass, which is the one
    entitled to say the session's repair path is exhausted."""
    day = repair_failure_alert_day(now)
    st = load_state(path) if state is None else state
    return _awaiting_print_state(st, day)
