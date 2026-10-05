"""SQLite-format UTC stamps taken from Python's one clock.

The desk dates rows and prune cutoffs from the SAME clock it decides with;
SQL ``datetime('now')`` is a second clock and the single-clock guard refuses
it. These helpers are the one place a row stamp is formatted so every
module writes the exact string SQLite itself would have stored.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from src.util.time import UTC


def sqlite_utc_timestamp(when: datetime) -> str:
    """Format a datetime the same way SQLite stores `datetime('now')`.

    Rows are stored as naive UTC strings. Converting ET day boundaries into
    this format lets `today_only=True` mean "this ET trading day" regardless
    of the host timezone.
    """
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return when.astimezone(UTC).replace(tzinfo=None).strftime("%Y-%m-%d %H:%M:%S")


def utc_now_stamp() -> str:
    """This instant, in SQLite's stored format."""
    return sqlite_utc_timestamp(datetime.now(UTC))


def utc_stamp_ago(*, hours: float = 0.0, days: float = 0.0) -> str:
    """A cutoff this far before now, in SQLite's stored format."""
    return sqlite_utc_timestamp(datetime.now(UTC) - timedelta(hours=hours, days=days))
