"""Owner-facing words for the health report's times and durations.

Relocated verbatim from ``src/log_health.py`` so that file shrinks instead of
growing: these are pure functions of a datetime (plus the owner's timezone)
and can be exercised without constructing a Report or a Finding.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from src.notifier.sections import fmt_time_12h

__all__ = ["OWNER_TZ", "_duration_words", "_plural", "_time_words"]

OWNER_TZ = ZoneInfo("America/New_York")


def _plural(n: int) -> str:
    return "" if n == 1 else "s"


def _duration_words(since: datetime, now: datetime) -> str:
    days = max(0, (now - since).days)
    if days >= 14:
        return f"since {since.astimezone(OWNER_TZ).strftime('%-d %B')}"
    if days >= 1:
        return f"for {days} day{_plural(days)}"
    return "since earlier today"


def _time_words(moment: datetime) -> str:
    """Format a moment with date and time, e.g. '2026-10-04 1:05 PM ET'."""
    return fmt_time_12h(moment.astimezone(OWNER_TZ))
