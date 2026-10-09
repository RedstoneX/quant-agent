"""Pure helpers for the muted-backlog read (moved out of db_reads.py to keep it from growing)."""

from __future__ import annotations

import re  # noqa: F401
from datetime import UTC, datetime
from zoneinfo import ZoneInfo


def _muted_symbols(detail: str | None) -> list[str]:
    """The symbols the mute recorded alongside a dropped message."""

    raw = detail or ""
    marker = "symbols:"
    if marker not in raw:
        return []
    tail = raw.split(marker, 1)[1]
    return [s.strip().upper() for s in tail.split(",") if s.strip()]


def _headline(text: str | None) -> str:
    """First line of a message, trimmed — enough to recognise it by."""

    for line in (text or "").splitlines():
        if line.strip():
            return line.strip()[:160]
    return ""


def _et_day(stamp: str) -> str:
    """The ET calendar day a UTC `notifier_sends.timestamp` falls on.

    The owner reads days as his own days; a message dropped at 01:00 UTC
    belongs to the previous evening for him. An unparseable stamp is
    reported as "unknown" rather than guessed at.
    """

    raw = (stamp or "").strip()
    if not raw:
        return "unknown"
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return "unknown"
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(ZoneInfo("America/New_York")).date().isoformat()
