"""Series-cache reads for `MacroDataProvider`, moved out of `macro.py`
(file-size baseline is shrink-only). Board item 187.

Two reads of the same on-disk entry:
- `serve_from_cache`: the pre-open entry, only while still the latest print.
- `serve_last_good`: the LAST entry we ever fetched, however old, used only
  after retries on the wire were exhausted. It is returned with its age and
  the caller records the series as a coverage failure with that age in the
  reason, so the hole stays visible. No staleness cutoff exists here: the age
  is reported, never compared, so no number governs a decision.
"""

from __future__ import annotations

import logging
from datetime import datetime

import pandas as pd

from src.data.macro_series_cache import MacroSeriesCache


logger = logging.getLogger(__name__)


def _entry_to_series(provider, series_id: str, entry: dict) -> pd.Series | None:
    try:
        rows = entry["observations"]
        index = pd.DatetimeIndex([pd.Timestamp(d) for d, _ in rows])
        values = [float("nan") if v is None else float(v) for _, v in rows]
        series = pd.Series(values, index=index, dtype=float)
    except Exception as e:  # noqa: BLE001
        logger.warning("FRED series cache for %s did not parse (%s)", series_id, e)
        return None
    if len(series) == 0:
        return None
    # Seed the metadata the due-date derivation needs so freshness is
    # re-derived from FRED's own numbers WITHOUT a second HTTP call.
    info = entry.get("info")
    provider._series_info_cache[series_id] = info if isinstance(info, dict) else None
    return series


def serve_from_cache(provider, series_id: str, kwargs: dict, now: datetime):
    """The cached copy while still current, or None to go to the wire."""
    try:
        entry = provider.series_cache.load(series_id, kwargs)
    except Exception as e:  # noqa: BLE001 — a broken cache is a miss
        logger.warning("FRED series cache unusable for %s: %s", series_id, e)
        return None
    if entry is None or not MacroSeriesCache.is_usable(entry, now):
        return None
    return _entry_to_series(provider, series_id, entry)


def serve_last_good(provider, series_id: str, kwargs: dict, now: datetime):
    """`(series, age_days)` of the last-good entry, or None when none exists."""
    try:
        entry = provider.series_cache.load(series_id, kwargs)
        fetched = datetime.fromisoformat(entry["fetched_at"]) if entry else None
    except Exception as e:  # noqa: BLE001
        logger.warning("No last-good FRED cache for %s: %s", series_id, e)
        return None
    if entry is None or fetched is None:
        return None
    series = _entry_to_series(provider, series_id, entry)
    if series is None:
        return None
    return series, max((now - fetched).days, 0)
