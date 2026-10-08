from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from pathlib import Path
from typing import Any
from src.alert_claims import load_state, repair_failure_alert_day, save_state


# ---------------------------------------------------------------------------
# the placement-failure marker — shared with the live session path
# ---------------------------------------------------------------------------
# A failed session-hours re-placement can be found by EITHER of two
# processes: this standalone unit, or the coverage reconcile a live session
# runs on itself (`src/pipeline.py`). On 2026-09-18 the session found two
# and said in the log that it alerts, while the only code that could alert
# lived here — and no watchdog process ran that day. Both paths now send,
# and both claim the same marker so the owner is not told twice about the
# same position.
#
# The marker is keyed per POSITION as well as per day, which is the honest
# version of "at most once a trading day": a 10:00 failure on one name must
# never be swallowed by an earlier alert about a different one. That is the
# same trap `should_alert_repair_failure` was written to avoid, one level
# down.


def _exposure_alerted_symbols(state: dict[str, Any], day: str) -> set[str]:
    """Which UNPROTECTED positions the owner was already paged about today.

    Item 211 defect 2, a live-risk hole. "This position is unprotected" was
    deduped on a bare per-DAY marker while both of its siblings
    (placement-failure, unreadable-stop) dedupe per symbol per day. A
    SECOND name going naked later the same day was therefore silenced
    completely — the exact trap `should_alert_repair_failure` was written
    to avoid, one level down. Same shape, same discipline.
    """
    raw = state.get("exposure_alerted_symbols")
    if not isinstance(raw, dict) or raw.get("day") != day:
        return set()
    return {
        str(sym).strip().upper()
        for sym in (raw.get("symbols") or [])
        if str(sym).strip()
    }


def _repair_failure_alerted_symbols(state: dict[str, Any], day: str) -> set[str]:
    raw = state.get("repair_failure_alerted_symbols")
    if not isinstance(raw, dict) or raw.get("day") != day:
        return set()
    return {
        str(sym).strip().upper()
        for sym in (raw.get("symbols") or [])
        if str(sym).strip()
    }


def _record_repair_failure_alert(
    state: dict[str, Any], day: str, symbols: Iterable[str],
) -> None:
    merged = _repair_failure_alerted_symbols(state, day) | {
        str(sym).strip().upper() for sym in symbols if str(sym).strip()
    }
    state["repair_failure_alerted_symbols"] = {
        "day": day, "symbols": sorted(merged),
    }
    state["repair_failure_alerted_for_day"] = day


def claim_repair_failure_alert(
    symbols: Iterable[str], *, now: datetime | None = None,
    path: Path | None = None,
) -> list[str]:
    """Reserve today's placement-failure alert for `symbols` and return the
    ones NOT already alerted today, in the order given.

    An empty list means every one of them has already been reported and the
    caller should stay quiet. A non-empty list is the caller's to send, and
    is recorded as sent before it returns — an unwritable state file
    therefore errs towards telling the owner twice rather than not at all,
    which is the right way round for an unprotected position.
    """
    day = repair_failure_alert_day(now)
    state = load_state(path)
    already = _repair_failure_alerted_symbols(state, day)
    fresh = [
        sym for sym in dict.fromkeys(
            str(raw).strip().upper() for raw in symbols if str(raw).strip()
        )
        if sym not in already
    ]
    if not fresh:
        return []
    _record_repair_failure_alert(state, day, fresh)
    save_state(state, path)
    return fresh


def _resolution_notified_symbols(state: dict[str, Any], day: str) -> set[str]:
    raw = state.get("repair_resolution_notified_symbols")
    if not isinstance(raw, dict) or raw.get("day") != day:
        return set()
    return {
        str(sym).strip().upper()
        for sym in (raw.get("symbols") or [])
        if str(sym).strip()
    }


def claim_repair_resolution_notice(
    symbols: Iterable[str], *, now: datetime | None = None,
    path: Path | None = None, state: dict[str, Any] | None = None,
) -> list[str]:
    """Reserve today's RESOLUTION notice and return the symbols entitled to
    one: the names the owner was actually paged about today and has not yet
    been told about again.

    THE RETRACTION HALF, and it is not decoration. A red "COULD NOT PUT THE
    PROTECTIVE STOP BACK ... Place the missing stop by hand" was, until now,
    the owner's last word on a position for the rest of the trading day even
    when the desk's own next sweep put the stop back minutes later
    (2026-09-23, RSG: paged 13:30:45, repaired 13:45:45, never retracted).
    He was left holding an instruction to do by hand a thing that was
    already done.

    Deliberately its OWN marker rather than a release of the placement-
    failure claim. Releasing that claim would make the sentence both alerts
    end on -- "Each position is reported at most once per trading day" --
    false, and would open a fail/succeed/fail name to two messages per
    cycle. The failure claim therefore stands for the day exactly as before;
    this adds one retraction per symbol per day and nothing else.

    Claim-before-send, same as every marker here: an unwritable state file
    errs towards telling the owner twice.
    """
    day = repair_failure_alert_day(now)
    own_state = state is None
    st = load_state(path) if own_state else state
    paged = _repair_failure_alerted_symbols(st, day)
    already = _resolution_notified_symbols(st, day)
    fresh = [
        sym for sym in dict.fromkeys(
            str(raw).strip().upper() for raw in symbols if str(raw).strip()
        )
        if sym in paged and sym not in already
    ]
    if not fresh:
        return []
    st["repair_resolution_notified_symbols"] = {
        "day": day, "symbols": sorted(already | set(fresh)),
    }
    if own_state:
        save_state(st, path)
    return fresh
