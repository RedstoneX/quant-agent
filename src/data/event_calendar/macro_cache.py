"""On-disk cache of FRED release schedules."""

import json
import logging
import os
from datetime import date
from pathlib import Path

from src.sentinel.counted import record_swallowed


logger = logging.getLogger(__name__)


#: On-disk schema tag for the release-schedule cache. An unrecognised tag is
#: a miss, which degrades to the live fetch this cache exists to relieve.
RELEASE_SCHEDULE_CACHE_SCHEMA = "fred-release-schedule-cache/1"

#: Default location of that cache. Sits beside the FRED series cache because
#: it is written by the same pre-open job, for the same reason.
RELEASE_SCHEDULE_CACHE_PATH = "data/macro/release_schedule_cache.json"


class ReleaseScheduleCache:
    """Forward release schedules written ahead of the open, read at the open.

    WHY THIS EXISTS — board items 187 / 119
    ---------------------------------------
    Item 187's fair-share split (below, in `get_upcoming_events`) stopped one
    slow release from eating the whole 20 s ceiling, and it stopped the tail of
    `MACRO_RELEASES` starving on every single run. It did not make the fetch
    fast enough to belong on the trading path, and the production log says so:
    on 2026-09-30, with the split deployed, the 13:33 morning run still came
    back 1/7 and the 14:04 run 0/7 [measured, `/home/qamc/quant-agent/
    quant_agent.log`]. Seven serial HTTPS round trips to FRED, started inside
    the first minutes after the opening bell — the same minute the fifteen-
    series macro fetch used to fail in — cannot be made reliable by dividing
    the same twenty seconds more fairly.

    The fix is the one that already worked for the series fetch
    (`src/data/macro_series_cache.py`): move the wire off the trading path.
    A forward release schedule is the most cacheable thing this desk fetches
    — FRED publishes CPI, PPI, PCE, GDP, Retail Sales, the Employment
    Situation and Initial Jobless Claims months ahead, and the dates change
    rarely. Nothing about "when is the next CPI" requires being asked at
    09:30:49.

    WHAT MAKES A CACHED COPY USABLE — two conditions, both existing numbers
    ----------------------------------------------------------------------
    Exactly the pair `FOMCCalendar` already uses on the same class of data (a
    published forward calendar from a government source), for the same
    reasons, with the same constant:

    1. The entry was written no more than `cache_ttl_days` ago.
    2. Its schedule still reaches the horizon being asked about. A young entry
       whose last date stops short of the horizon cannot answer the question
       and is a miss, not a near-enough hit.

    No new threshold is introduced here.

    WHAT IT DOES NOT DO
    -------------------
    It never invents a schedule. A release that is in neither the cache nor
    the wire's answer stays a named failure in `EventCalendarCoverage`, and a
    release answered from cache is reported as cached, with its age, wherever
    the coverage prose travels. Only the prefetch writes: a trading session
    can never turn its own partial run into tomorrow's cached answer.
    """

    def __init__(self, path: str = RELEASE_SCHEDULE_CACHE_PATH):
        self.path = Path(path)

    def _read(self) -> dict:
        try:
            if not self.path.exists():
                return {}
            raw = json.loads(self.path.read_text()) or {}
        except Exception as e:  # noqa: BLE001 — a broken cache is a miss
            record_swallowed("data.event_calendar.macro_cache.read", e, log=logger)
            logger.warning("Release-schedule cache unreadable (%s) — ignoring", e)
            return {}
        if raw.get("schema") != RELEASE_SCHEDULE_CACHE_SCHEMA:
            return {}
        entries = raw.get("releases")
        return entries if isinstance(entries, dict) else {}

    def load(self, release_id: int) -> tuple[list[date], date] | None:
        """`(dates, fetched_on)` for one release, or None on any miss."""
        entry = self._read().get(str(release_id))
        if not isinstance(entry, dict):
            return None
        try:
            fetched_on = date.fromisoformat(str(entry.get("fetched_on")))
            dates = sorted(date.fromisoformat(str(d)) for d in (entry.get("dates") or []))
        except Exception as e:  # noqa: BLE001 — a malformed entry is a miss
            record_swallowed("data.event_calendar.macro_cache.load", e, log=logger, release_id=release_id)
            return None
        if not dates:
            return None
        return dates, fetched_on

    def save(self, release_id: int, label: str, dates: list[date], today: date) -> None:
        """Never raises: an unwritable cache costs a live fetch, not a session."""
        try:
            entries = self._read()
            entries[str(release_id)] = {
                "label": label,
                "fetched_on": today.isoformat(),
                "dates": [d.isoformat() for d in sorted(dates)],
            }
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            tmp.write_text(
                json.dumps(
                    {
                        "schema": RELEASE_SCHEDULE_CACHE_SCHEMA,
                        "releases": entries,
                    },
                    indent=2,
                )
            )
            os.replace(tmp, self.path)
        except Exception as e:  # noqa: BLE001
            logger.warning("Release-schedule cache unwritable: %s", e)
