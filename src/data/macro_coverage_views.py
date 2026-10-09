"""Item 187: the three states of a required FRED series, kept apart.

FRESH (fetched this run), STALE (a last-good value is held, served with its
age) and MISSING (no value at all). STALE is carried as a `SeriesFailure`
whose reason starts with `STALE_PREFIX`; everything that words or counts the
gap goes through here so no surface can call a held number "missing". The age
is reported and never compared with any bound, so no number governs a decision.
"""

from __future__ import annotations

import re

STALE_PREFIX = "stale_last_good_served_age_"
_AGE = re.compile(rf"^{STALE_PREFIX}(\d+)d")


def stale_age_days(reason: str) -> int | None:
    m = _AGE.match(reason or "")
    return int(m.group(1)) if m else None


def split_failed(failed) -> tuple[list, list]:
    """`(stale, missing)` from a coverage's failed list."""
    stale = [f for f in failed if stale_age_days(f.reason) is not None]
    return stale, [f for f in failed if stale_age_days(f.reason) is None]


def stale_names(stale) -> str:
    return ", ".join(f"{f.series_id} (last-good value, {stale_age_days(f.reason)}d old)" for f in stale)


def stamp_note(succeeded: int, configured: int, failed) -> str:
    stale, missing = split_failed(failed)
    parts = [f"{succeeded}/{configured} FRED series fresh"]
    if stale:
        parts.append(f"STALE (old number held): {stale_names(stale)}")
    if missing:
        parts.append("MISSING (no value): " + ", ".join(f.series_id for f in missing))
    return "; ".join(parts)


def stale_prompt_text(stale) -> str:
    if not stale:
        return ""
    return (
        " STALE (the desk HAS a number, but it is old, not fetched this run): "
        + stale_names(stale)
        + ". Weigh each by its age; it is not current."
    )
