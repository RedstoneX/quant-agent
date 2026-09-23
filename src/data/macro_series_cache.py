"""On-disk cache of raw FRED series, written ahead of the open and read by
the trading sessions.

WHY THIS EXISTS — board item 119
--------------------------------
Measured in the production log (`/home/qamc/quant-agent/quant_agent.log`):

* The morning session's macro stage starts at **09:30:49 ET** — 49 seconds
  AFTER the opening bell (13:30:48.283 / 13:30:49.653 / 13:30:49.386 UTC on
  2026-09-18, 09-21, 09-22).
* On 2026-09-22 that fetch began at 13:30:49.4 UTC and hit its 90-second
  ceiling at 13:32:19.39 UTC, having consumed the budget exactly. Eight of the
  fifteen series were skipped **without a single attempt** and the macro seat
  ran the open on 7 of 15.
* The 90-second ceiling was fully exhausted in seven distinct runs in the
  retained log; across twelve recorded runs coverage was complete on four and
  partial on eight (5, 6, 7, 7, 8, 11, 11, 11 of 15).

The previous attempt at this (PR #565) fetched the same fifteen series in
PARALLEL at the open. That fights the symptom: it spends more of FRED's
tolerance at the single moment the desk can least afford to be told no, and
during its own live stress test the burst got this box's IP temporarily
blocked by FRED's abuse detection. Nothing about CPI, the unemployment rate,
the fed funds rate or the 10-year yield requires being fetched at 09:30:49.

So the fetch moves off the trading path entirely. A timer job walks every
series ahead of the open, strictly serially and paced, and writes what it gets
here. The sessions then read this file instead of the wire whenever the cached
copy is provably still the latest print that exists.

WHAT MAKES A CACHED COPY USABLE
-------------------------------
Not an age in hours. This desk has a standing rule against age-based staleness
for macro data, and `SeriesFreshness` in `src/data/macro.py` explains at length
why a day count is the wrong test (CPI is monthly; a twenty-day-old CPI reading
IS the current reading).

**The test is the release clock.** An entry is served only while no publication
boundary has passed since it was fetched — 08:30 ET, when BLS releases its
principal indicators, and 16:15 ET, when the Fed's H.15 posts. See
`src/data/fred_publication_days.py` for both citations. Nothing can have
changed in between, so re-fetching could only return the same rows.

This is the correction that makes the cache safe, and the design did not
survive review without it. The obvious test — "today is on or before the
series' own derived `expected_next_by`" — is wrong twice over, and an
adversarial review caught both:

* On a CPI morning, `expected_next_by == today`, so that test APPROVES the
  cache on the one day a month the print actually moves. The desk would serve
  the pre-CPI number into the 09:30 session.
* The evening session runs at 20:00 ET and today reads the H.15 print that
  posted at 16:15. A date-granular test would have served it the morning copy
  instead — a daily regression on every daily rate series, traded for a
  morning that was mostly not the one failing.

The derived due date is still checked, as a second and weaker condition: an
entry whose `expected_next_by` has passed is never served, because the desk
already knows a newer print is owed. Both conditions must hold.

An entry written with no `expected_next_by` (the provider could not establish
one — FRED's metadata call missed) is held to the boundary test alone, which
is the strictly stronger of the two.

WHAT IT DOES NOT DO
-------------------
It never invents freshness. An entry carries the observation dates FRED
actually returned and the metadata pair the due-date derivation was built
from, and the provider re-derives freshness from those, so a cache-served
series reports exactly what a wire-served one would — including `overdue`.

Only the prefetch job writes here. A trading session never populates the
cache, so a session can never turn its own partial fetch into tomorrow's
"cached" answer.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import date, datetime
from pathlib import Path

from src.data.fred_publication_days import crossed_a_publication_boundary

logger = logging.getLogger(__name__)

#: On-disk schema tag. Bumped only when the file layout changes in a way an
#: older reader would misread; an unrecognised tag is treated as a cache miss,
#: which degrades to today's live-fetch behaviour and loses nothing.
SCHEMA = "fred-series-cache/1"


def _kwargs_key(kwargs: dict) -> str:
    """Stable text form of the fetch arguments an entry was produced with.

    A cached series is only valid for the SAME query. `observation_start`
    differs per indicator and changes as the calendar moves, so an entry
    fetched for a different window is a miss, not a near-enough hit.
    """
    try:
        return json.dumps(
            {str(k): str(v) for k, v in sorted((kwargs or {}).items())},
            sort_keys=True,
        )
    except Exception:  # noqa: BLE001 — an unserialisable kwarg is just a miss
        return "<unserialisable>"


class MacroSeriesCache:
    """Per-series JSON files under `data_dir`, written atomically."""

    def __init__(self, data_dir: str = "data/macro/series_cache"):
        self.data_dir = Path(data_dir)

    # -- paths ---------------------------------------------------------

    def _path(self, series_id: str) -> Path:
        safe = "".join(c for c in str(series_id) if c.isalnum() or c in "._-")
        return self.data_dir / f"{safe}.json"

    # -- write ---------------------------------------------------------

    def save(
        self,
        series_id: str,
        kwargs: dict,
        observations: list[tuple[str, float | None]],
        info: dict | None,
        expected_next_by: date | None,
        fetched_at: datetime,
    ) -> None:
        """Record one fetched series. Best effort: a write failure is logged
        and swallowed, because a cache that cannot be written must never take
        down the job that was only trying to help tomorrow's open."""
        payload = {
            "schema": SCHEMA,
            "series_id": str(series_id),
            "kwargs": _kwargs_key(kwargs),
            "fetched_at": fetched_at.isoformat(),
            "expected_next_by": (
                expected_next_by.isoformat() if expected_next_by is not None else None
            ),
            "info": info if isinstance(info, dict) else None,
            "observations": [
                [str(d), (None if v is None else float(v))] for d, v in observations
            ],
        }
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            path = self._path(series_id)
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(payload), encoding="utf-8")
            os.replace(tmp, path)
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "Could not write FRED series cache for %s: %s — the next "
                "session will fetch this series live", series_id, e,
            )

    # -- read ----------------------------------------------------------

    def load(self, series_id: str, kwargs: dict) -> dict | None:
        """The stored entry for this exact query, or None on any miss."""
        try:
            raw = self._path(series_id).read_text(encoding="utf-8")
        except (FileNotFoundError, NotADirectoryError):
            return None
        except Exception as e:  # noqa: BLE001
            logger.warning("Unreadable FRED series cache for %s: %s", series_id, e)
            return None
        try:
            entry = json.loads(raw)
        except Exception:  # noqa: BLE001
            return None
        if not isinstance(entry, dict) or entry.get("schema") != SCHEMA:
            return None
        if entry.get("kwargs") != _kwargs_key(kwargs):
            return None
        if not isinstance(entry.get("observations"), list):
            return None
        return entry

    @staticmethod
    def is_usable(entry: dict, now: datetime) -> bool:
        """Whether `entry` is still the latest print that exists, as of `now`.

        Both conditions must hold, and the first is the load-bearing one:

        1. **No publication boundary has passed since the entry was fetched.**
           08:30 ET (BLS principal indicators) and 16:15 ET (Fed H.15) — see
           `src/data/fred_publication_days.py` for the published schedules.
           Nothing can have been released in between, so the stored rows are
           still what the wire would return. An entry with no readable
           `fetched_at` fails this outright.
        2. **The series' own derived due date has not passed**, when the entry
           carries one. If the desk already knows a newer print is owed, it
           asks for it rather than serving what it has.

        Anything else is a miss and the caller goes to the wire — which is
        exactly the behaviour that shipped before this cache existed, so a
        miss costs nothing that was not already being paid.
        """
        if not isinstance(entry, dict):
            return False
        fetched_at = entry.get("fetched_at")
        if not isinstance(fetched_at, str) or not fetched_at:
            return False
        try:
            stamp = datetime.fromisoformat(fetched_at)
        except ValueError:
            return False
        if stamp.tzinfo is None:
            return False
        if crossed_a_publication_boundary(stamp, now):
            return False
        due = entry.get("expected_next_by")
        if isinstance(due, str) and due:
            try:
                return now.date() <= date.fromisoformat(due)
            except ValueError:
                return False
        return True
