from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from pathlib import Path
from typing import Any
from src.alert_claims import load_state, repair_failure_alert_day, save_state


# ---------------------------------------------------------------------------
# the elected-but-unfilled marker — its own identity, the same machinery
# ---------------------------------------------------------------------------
# A protective stop that FIRED and did not FILL is a different condition
# from a protective stop the desk could not PLACE, and it must not share
# the placement-failure key: one key would let either condition silence the
# other on the same name, and "do not double-alert" does not mean "alert
# about only one of two real faults". Same state file, same trading-day
# key, same claim-before-send discipline, separate identity.


def _elected_unfilled_alerted_symbols(state: dict[str, Any], day: str) -> set[str]:
    raw = state.get("elected_unfilled_alerted_symbols")
    if not isinstance(raw, dict) or raw.get("day") != day:
        return set()
    return {
        str(sym).strip().upper()
        for sym in (raw.get("symbols") or [])
        if str(sym).strip()
    }


def claim_elected_unfilled_alert(
    symbols: Iterable[str], *, now: datetime | None = None,
    path: Path | None = None,
) -> list[str]:
    """Reserve today's elected-but-unfilled alert for `symbols` and return
    the ones NOT already alerted today, in the order given.

    Same contract as `claim_repair_failure_alert`. The caller MUST call
    `release_elected_unfilled_alert` if the send then fails.
    """
    day = repair_failure_alert_day(now)
    state = load_state(path)
    already = _elected_unfilled_alerted_symbols(state, day)
    fresh = [
        sym for sym in dict.fromkeys(
            str(raw).strip().upper() for raw in symbols if str(raw).strip()
        )
        if sym not in already
    ]
    if not fresh:
        return []
    merged = already | set(fresh)
    state["elected_unfilled_alerted_symbols"] = {
        "day": day, "symbols": sorted(merged),
    }
    save_state(state, path)
    return fresh


# ---------------------------------------------------------------------------
# the kill-switch-block marker — its own identity, the same machinery
# ---------------------------------------------------------------------------
# The desk's own kill switch refusing a protective stop is a THIRD distinct
# condition from a placement failure and from an elected-but-unfilled stop:
# the broker was never even asked. It must not share either key, for the
# same reason those two don't share one — silencing one condition must
# never silence a different one on the same name. Same state file, same
# trading-day key, same claim-before-send discipline, separate identity.


def _kill_switch_block_alerted_symbols(state: dict[str, Any], day: str) -> set[str]:
    raw = state.get("kill_switch_block_alerted_symbols")
    if not isinstance(raw, dict) or raw.get("day") != day:
        return set()
    return {
        str(sym).strip().upper()
        for sym in (raw.get("symbols") or [])
        if str(sym).strip()
    }


def claim_kill_switch_block_alert(
    symbols: Iterable[str], *, now: datetime | None = None,
    path: Path | None = None,
) -> list[str]:
    """Reserve today's kill-switch-block alert for `symbols` and return the
    ones NOT already alerted today, in the order given.

    Same contract as `claim_repair_failure_alert`: a kill switch left on
    all day would otherwise page the owner on every retry of every symbol
    it touches; an unwritable state file errs towards telling the owner
    twice rather than not at all, which is the right way round for a
    naked position.
    """
    day = repair_failure_alert_day(now)
    state = load_state(path)
    already = _kill_switch_block_alerted_symbols(state, day)
    fresh = [
        sym for sym in dict.fromkeys(
            str(raw).strip().upper() for raw in symbols if str(raw).strip()
        )
        if sym not in already
    ]
    if not fresh:
        return []
    merged = already | set(fresh)
    state["kill_switch_block_alerted_symbols"] = {
        "day": day, "symbols": sorted(merged),
    }
    save_state(state, path)
    return fresh


# ---------------------------------------------------------------------------
# the unreadable-stop marker — its own identity again, board item 172
# ---------------------------------------------------------------------------
# "I could not ask the broker whether this position has a stop" is a third
# condition, distinct from a placement failure and from an elected-unfilled
# stop, and it gets a third key for the reason the second one got a second:
# one key lets either condition silence the other on the same name, and the
# owner would be told about one real fault while another went unreported.


def _unreadable_alerted_symbols(state: dict[str, Any], day: str) -> set[str]:
    raw = state.get("unreadable_stop_alerted_symbols")
    if not isinstance(raw, dict) or raw.get("day") != day:
        return set()
    return {
        str(sym).strip().upper()
        for sym in (raw.get("symbols") or [])
        if str(sym).strip()
    }


def claim_unreadable_stop_alert(
    symbols: Iterable[str], *, now: datetime | None = None,
    path: Path | None = None,
) -> list[str]:
    """Reserve today's unreadable-stop alert for `symbols` and return the
    ones NOT already alerted today, in the order given. Board item 172.

    Same contract as `claim_repair_failure_alert`, including that an
    unwritable state file errs towards telling the owner twice rather than
    not at all — which is the right way round when what is unknown is
    whether a position has any loss protection at all.

    Per SYMBOL, not per run: a broker that cannot describe AAPL's stops at
    09:35 and cannot describe MSFT's at 14:05 is two findings, and the
    second must not be swallowed by the first. The coverage sweep runs on a
    30-minute cadence, so without this the same name would page the owner
    roughly a dozen times a session.
    """
    day = repair_failure_alert_day(now)
    state = load_state(path)
    already = _unreadable_alerted_symbols(state, day)
    fresh = [
        sym for sym in dict.fromkeys(
            str(raw).strip().upper() for raw in symbols if str(raw).strip()
        )
        if sym not in already
    ]
    if not fresh:
        return []
    merged = already | set(fresh)
    state["unreadable_stop_alerted_symbols"] = {
        "day": day, "symbols": sorted(merged),
    }
    save_state(state, path)
    return fresh


# ---------------------------------------------------------------------------
# the declined-exit marker — a FOURTH key, for the same reason as the third
# ---------------------------------------------------------------------------
# "the desk decided to leave this position and could not" is its own
# condition. Sharing the unreadable key would let a stop it could not read
# silence the exit it then refused on the strength of that same read —
# exactly the swallowing this family of keys exists to prevent, and the two
# happen together by construction.


def _exit_declined_alerted_symbols(state: dict[str, Any], day: str) -> set[str]:
    raw = state.get("exit_declined_alerted_symbols")
    if not isinstance(raw, dict) or raw.get("day") != day:
        return set()
    return {
        str(sym).strip().upper()
        for sym in (raw.get("symbols") or [])
        if str(sym).strip()
    }


def claim_exit_declined_alert(
    symbols: Iterable[str], *, now: datetime | None = None,
    path: Path | None = None,
) -> list[str]:
    """Reserve today's declined-exit alert for `symbols` and return the ones
    NOT already alerted today, in the order given.

    Same contract as `claim_unreadable_stop_alert`, including erring towards
    telling the owner twice over not at all: a position the desk decided to
    leave and could not is not a state to under-report.
    """
    day = repair_failure_alert_day(now)
    state = load_state(path)
    already = _exit_declined_alerted_symbols(state, day)
    fresh = [
        sym for sym in dict.fromkeys(
            str(raw).strip().upper() for raw in symbols if str(raw).strip()
        )
        if sym not in already
    ]
    if not fresh:
        return []
    state["exit_declined_alerted_symbols"] = {
        "day": day, "symbols": sorted(already | set(fresh)),
    }
    save_state(state, path)
    return fresh


def _unguarded_alerted_symbols(state: dict[str, Any], day: str) -> set[str]:
    raw = state.get("unguarded_alerted_symbols") or {}
    if not isinstance(raw, dict) or raw.get("day") != day:
        return set()
    return {
        str(sym).strip().upper()
        for sym in (raw.get("symbols") or [])
        if str(sym).strip()
    }
