"""The coverage watchdog's READ-ONLY core, importable by anything.

Lifted verbatim from `src/coverage_watchdog.py` so that the dashboard
(`src/api/drift_state.py`) and the refusal-signature reader can read the
on-box alerting state and judge the most recent trading day WITHOUT
importing the watchdog itself, which lazily reaches the stop-repair and
scale-in code under `src/execution/`. This module imports nothing that can
place, amend or cancel an order; keep it that way — the dashboard layering
test (`tests/test_api_cannot_trade.py`) derives its doors from source.

Behaviour is unchanged: same files, same keys, same per-day dedup. The
watchdog re-exports every name here so its existing callers are untouched.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from collections.abc import Iterable
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from src.coverage_watchdog_records import record_watchdog_pass
from src.silence_watchdog import SLACK_MINUTES
from src.trading_calendar import ET, SESSION_WINDOWS

logger = logging.getLogger(__name__)

#: On-box record, gitignored like its siblings under data/alerting/.
STATE_PATH = (
    Path(__file__).resolve().parent.parent / "data" / "alerting" / "coverage_heartbeat.json"
)

#: Deploy-drift snapshot, written by scripts/check_deploy_drift.py and read
#: by the /health API so a checkout that is behind origin/main is VISIBLE on
#: the desk's own board, not only in a Telegram message. Alerts can be muted;
#: the board cannot. It lives beside the other alerting state and is read and
#: written with the same `load_state`/`save_state` helpers, so the per-day
#: dedup that stops a repeating alert is the one already in use here rather
#: than a fourth private implementation.
DEPLOY_DRIFT_STATE_PATH = (
    Path(__file__).resolve().parent.parent / "data" / "alerting" / "deploy_drift.json"
)


#: How many weekdays back to look for the most recent trading day. A long
#: weekend plus a holiday is three; five is comfortably past that and bounds
#: the calendar lookups when the broker's calendar cannot be read at all.
MAX_WEEKDAYS_BACK = 5


def _utc_now() -> datetime:
    """Seam for tests — real code never patches `datetime` itself."""
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# which session are we judging?
# ---------------------------------------------------------------------------

def most_recent_trading_day(now: datetime, broker: Any = None, db: Any = None) -> date:
    """The most recent weekday whose cash session has already ENDED (plus
    the timer slack) and that the broker's calendar confirms as a trading
    day.

    "Already ended" rather than "strictly before today": at the 06:15 ET
    run this is yesterday either way, but run by hand at 23:50 ET on a
    Friday it must judge Friday, not Thursday — a session that has not
    finished cannot yet have failed to re-place anything, and one that has
    finished can. Holidays are excluded through `broker.is_trading_day`
    when available. That helper answers False on a calendar-read failure,
    so a broker outage would walk PAST a real trading day and could judge
    a holiday-free week as "no session, because there was no day" — to
    keep the failure on the alerting side, the walk is bounded and falls
    back to the most recent plain weekday, which can only over-alert on a
    holiday, never suppress a real gap.
    """
    today_et = now.astimezone(ET).date()
    _start, today_end = _session_bounds_utc(today_et)
    candidate = today_et if now >= today_end else today_et - timedelta(days=1)
    first_weekday: date | None = None
    checked = 0
    while checked < MAX_WEEKDAYS_BACK:
        if candidate.weekday() < 5:
            if first_weekday is None:
                first_weekday = candidate
            checked += 1
            if broker is None:
                return candidate
            try:
                if broker.is_trading_day(candidate):
                    return candidate
            except Exception as exc:  # noqa: BLE001
                record_watchdog_pass("calendar_lookup", exc, db=db)
                logger.warning("coverage watchdog: calendar lookup failed for %s: %s", candidate, exc)
                return candidate
        candidate -= timedelta(days=1)
    return first_weekday or (today_et - timedelta(days=1))


def _session_bounds_utc(day: date) -> tuple[datetime, datetime]:
    """[09:30 ET, 16:00 ET + SLACK_MINUTES) for `day`, in UTC. Only a session
    completing inside the cash session can have re-placed a DAY stop; the
    evening run sees a shut market and, correctly, places nothing."""
    lo, hi = SESSION_WINDOWS["intra_check"]
    midnight = datetime(day.year, day.month, day.day, tzinfo=ET)
    start = midnight + timedelta(minutes=lo)
    end = midnight + timedelta(minutes=hi + SLACK_MINUTES)
    return start.astimezone(timezone.utc), end.astimezone(timezone.utc)


# ---------------------------------------------------------------------------
# on-box state — one alert per trading day
# ---------------------------------------------------------------------------

def load_state(path: Path | None = None) -> dict[str, Any]:
    try:
        raw = json.loads((path or STATE_PATH).read_text())
    except (OSError, ValueError):
        raw = None
    if not isinstance(raw, dict):
        raw = {}
    raw.setdefault("alerted_for_day", None)
    raw.setdefault("exposure_alerted_symbols", None)
    # Kept as a truthful record of the last day a placement-failure alert
    # went out, and still written; it is no longer what SUPPRESSES one.
    # Suppression reads `repair_failure_alerted_symbols` below, which is
    # keyed per position as well as per day — see
    # `_repair_failure_alerted_symbols`.
    raw.setdefault("repair_failure_alerted_for_day", None)
    raw.setdefault("repair_failure_alerted_symbols", None)
    raw.setdefault("last_result", None)
    raw.setdefault("updated_at", None)
    return raw


def save_state(state: dict[str, Any], path: Path | None = None) -> bool:
    """Atomic write, same shape as the sibling watchdogs. Never raises."""
    target = path or STATE_PATH
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=str(target.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as handle:
                json.dump(state, handle, indent=2, sort_keys=True)
                handle.write("\n")
            os.replace(tmp_name, target)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise
    except OSError:
        return False
    return True


def repair_failure_alert_day(now: datetime | None = None) -> str:
    """The ET calendar date a placement failure is filed under.

    Deliberately NOT `most_recent_trading_day`: that answers "which session
    should have re-placed a stop overnight", which at 10:00 ET is still
    yesterday. A placement that just failed is happening TODAY, and both
    paths must agree on the key or the shared marker is no marker at all.
    """
    return (now or _utc_now()).astimezone(ET).date().isoformat()


# --- Generic per-symbol, per-trading-day alert claim -------------------------
# Same state file, same trading-day key and the same claim-before-send
# discipline as `claim_repair_failure_alert`, but keyed by an arbitrary
# `kind` so a new fail-closed page does not need its own pair of helpers.
# Callers that page the owner about a per-symbol condition use this; the
# older named helpers keep their own keys so their history is unaffected.

def _typed_alerted_symbols(
    state: dict[str, Any], day: str, kind: str,
) -> set[str]:
    raw = state.get(f"typed_alerted_symbols::{kind}")
    if not isinstance(raw, dict) or raw.get("day") != day:
        return set()
    return {
        str(sym).strip().upper()
        for sym in (raw.get("symbols") or [])
        if str(sym).strip()
    }


#: How many suppression records to retain per alert type. Bounds the state
#: file; the running `count` is never truncated, only the per-event list.
_SUPPRESSION_LOG_LIMIT = 50


def _record_suppressed_alert(
    state: dict[str, Any], kind: str, day: str, keys: Iterable[str],
) -> None:
    """Durably note an alert this helper declined to resend (item 211).

    Nothing is silently dropped: the owner not being paged a second time is
    a presentation decision, and the underlying fact still has to be
    readable afterwards or the desk has stopped reporting its true state.
    """
    log = state.get("suppressed_alerts")
    if not isinstance(log, dict):
        log = {}
    entry = log.get(kind)
    if not isinstance(entry, dict) or entry.get("day") != day:
        entry = {"day": day, "count": 0, "events": []}
    events = entry.get("events")
    if not isinstance(events, list):
        events = []
    for key in keys:
        entry["count"] = int(entry.get("count") or 0) + 1
        events.append({"key": key, "day": day})
    entry["events"] = events[-_SUPPRESSION_LOG_LIMIT:]
    log[kind] = entry
    state["suppressed_alerts"] = log


def claim_typed_alert(
    kind: str, symbols: Iterable[str], *, now: datetime | None = None,
    path: Path | None = None,
) -> list[str]:
    """Reserve today's `kind` alert for `symbols`; return those NOT yet
    alerted today, in the order given.

    Same contract as `claim_repair_failure_alert`, including that a state
    file that cannot be read errs towards telling the owner twice over not
    at all.
    """
    key = str(kind).strip() or "unspecified"
    day = repair_failure_alert_day(now)
    state = load_state(path)
    already = _typed_alerted_symbols(state, day, key)
    fresh = [
        sym for sym in dict.fromkeys(
            str(raw).strip().upper() for raw in symbols if str(raw).strip()
        )
        if sym not in already
    ]
    stale = [sym for sym in dict.fromkeys(
        str(raw).strip().upper() for raw in symbols if str(raw).strip()
    ) if sym in already]
    if stale:
        _record_suppressed_alert(state, key, day, stale)
    if not fresh:
        if stale:
            save_state(state, path)
        return []
    state[f"typed_alerted_symbols::{key}"] = {
        "day": day, "symbols": sorted(already | set(fresh)),
    }
    save_state(state, path)
    return fresh
