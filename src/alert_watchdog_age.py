"""Age in hours of an ISO timestamp.

Lifted verbatim out of `src/alert_watchdog.py`, which re-exports it.
"""
from __future__ import annotations

from datetime import datetime, timezone


def _age_hours(stamp: str, now: datetime) -> float | None:
    try:
        when = datetime.fromisoformat(stamp)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return max((now - when).total_seconds() / 3600.0, 0.0)
