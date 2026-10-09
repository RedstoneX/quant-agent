"""Board item 187: the durable per-open record of FRED fetch coverage.

Built only from the coverage objects the fetches returned. Nothing is
inferred: a series is "not attempted" only when the fetcher itself marked it
so, and a missing event-calendar coverage object is stored as NULL, never 0.
"""

from __future__ import annotations

import json

from src.data.macro import MacroCoverage
from src.data.macro_coverage_views import stale_age_days


def _series_entry(f) -> dict:
    """A missing series keeps its original two-key shape; a STALE one (value
    held, served with its age) is tagged so a reader never infers "no value"."""
    age = stale_age_days(f.reason)
    entry = {"series": f.series_id, "reason": f.reason}
    if age is not None:
        entry.update(state="stale", age_days=age)
    return entry


def build_row(run_id, macro_coverage, event_coverage) -> dict:
    """Row for `fred_fetch_coverage_runs`. `full_coverage` is 1 only when
    BOTH halves were reported and both complete."""
    row = {
        "run_id": run_id,
        "series_configured": None,
        "series_succeeded": None,
        "series_failed": None,
        "series_not_attempted": None,
        "releases_configured": None,
        "releases_succeeded": None,
        "releases_from_cache": None,
        "releases_failed": None,
    }
    series_ok = False
    if isinstance(macro_coverage, MacroCoverage):
        never = macro_coverage.not_attempted
        never_ids = {f.series_id for f in never}
        row["series_configured"] = macro_coverage.configured
        row["series_succeeded"] = macro_coverage.succeeded
        row["series_failed"] = json.dumps(
            [_series_entry(f) for f in macro_coverage.failed if f.series_id not in never_ids]
        )
        row["series_not_attempted"] = json.dumps(sorted(never_ids))
        series_ok = macro_coverage.complete
    events_ok = False
    if event_coverage is not None and hasattr(event_coverage, "from_cache"):
        row["releases_configured"] = event_coverage.configured
        row["releases_succeeded"] = event_coverage.succeeded
        row["releases_from_cache"] = len(event_coverage.from_cache)
        row["releases_failed"] = json.dumps([{"release": f.label, "reason": f.reason} for f in event_coverage.failed])
        events_ok = event_coverage.complete
    row["full_coverage"] = 1 if (series_ok and events_ok) else 0
    return row
