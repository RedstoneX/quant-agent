"""Neutral owner-facing timestamp formatting.

Callers own timezone conversion; this module only renders the datetime it is
given.  ``src.notifier.sections`` re-exports the same function object for
backward compatibility.
"""

from __future__ import annotations


def fmt_time_12h(dt) -> str:
    """'2026-10-04 1:05 PM ET' — date, then 12-hour clock, no leading zero,
    AM/PM, ET suffix.

    Owner ratified 2026-09-17: no 24-hour clock anywhere in a Telegram
    message — every header and any timestamp inside a message body or
    DETAILS block uses this, never a bare `strftime('%H:%M')`.
    Owner ruled 2026-10-02 (item 231): a bare time cannot be placed when he
    scrolls back hours later, so the date is part of the one formatter and
    no call site can omit it.
    `strftime('%-I:%M %p')` (no leading zero) is a glibc-only extension —
    computed manually here instead so this doesn't silently regress on a
    non-glibc platform."""
    hour12 = dt.hour % 12 or 12
    ampm = "AM" if dt.hour < 12 else "PM"
    return f"{dt:%Y-%m-%d} {hour12}:{dt.minute:02d} {ampm} ET"
