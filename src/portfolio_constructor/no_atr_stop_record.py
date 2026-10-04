"""Counted record of every no-ATR structural stop placement.

The desk places a protective stop read from price STRUCTURE whenever a name
arrives with no ATR reading (owner ruling 2026-09-25, board item 80). Nothing
has ever counted those placements, so two questions about that path have been
unanswerable from the record:

  * how OFTEN does it run at all -- is it a daily event or has the live desk
    never reached it?
  * when it runs, is a per-name denominator available to replace the flat
    `structural_stop_buffer_pct` buffer, or is the name's signal bar missing
    exactly when its ATR is?

The second question is live: the owner's 2026-10-04 ruling ("self adjusting
is not a made up number -- it is specific to its own stock move") says the
buffer should be a multiple of the name's own movement rather than a flat
0.5% of its price. That conversion is NOT made here. Protection changes on
this desk are one-way, and re-expressing the buffer tightens the stop on a
quiet name; tightening a stop on a path whose frequency nobody has measured
is the trap this module exists to close. The conversion waits on branch
``ledger/atr-express-buffer-conversion`` until these counts exist.

So this module CHANGES NO BEHAVIOUR. ``noted`` passes its placement straight
back to the caller; it only counts it and logs it on the way through. The
signal-bar range it records is the candidate denominator, observed and never
applied.

Counts are per process, which is one desk session. They are read back through
``snapshot`` and emitted with the session's other counters; `reset` exists for
tests, which must never inherit another test's tallies.
"""
from __future__ import annotations

import logging
import math
from collections import Counter

logger = logging.getLogger(__name__)

#: Placements by rule, e.g. {"stop_read_from_structure_no_atr": 3}.
_BY_RULE: Counter[str] = Counter()
#: Whether the candidate per-name denominator was readable at placement.
_DENOMINATOR: Counter[str] = Counter()


def signal_bar_range(analysis) -> float | None:
    """The signal bar's high-minus-low, or None when it is not readable.

    None -- never zero -- for a missing edge, a non-finite edge or a
    non-positive span, so a caller can tell "no denominator" from "a
    denominator of nothing".
    """
    high = getattr(analysis, "signal_bar_high", None)
    low = getattr(analysis, "signal_bar_low", None)
    try:
        high = float(high) if high is not None else None
        low = float(low) if low is not None else None
    except (TypeError, ValueError):
        return None
    if high is None or low is None:
        return None
    if not (math.isfinite(high) and math.isfinite(low)):
        return None
    span = high - low
    if not math.isfinite(span) or span <= 0:
        return None
    return span


def noted(
    level: float, stop: float, rule: str, analysis,
) -> tuple[float, float, str]:
    """Count one no-ATR structural stop placement and return it unchanged.

    The returned tuple is exactly what the caller would have returned without
    this module, so a failure to record can never cost a name its stop.
    """
    span = signal_bar_range(analysis)
    _BY_RULE[rule] += 1
    _DENOMINATOR["readable" if span is not None else "unreadable"] += 1
    buffer_distance = abs(level - stop)
    logger.info(
        "no-ATR structural stop placed: rule=%s level=%.4f stop=%.4f "
        "buffer=%.4f buffer_pct_of_level=%.4f signal_bar_range=%s "
        "buffer_in_bar_ranges=%s",
        rule, level, stop, buffer_distance,
        (buffer_distance / level * 100.0) if level else float("nan"),
        "none" if span is None else f"{span:.4f}",
        "none" if not span else f"{buffer_distance / span:.4f}",
    )
    return (level, stop, rule)


def snapshot() -> dict[str, object]:
    """What this session has counted so far, as plain dicts."""
    return {
        "placements_by_rule": dict(_BY_RULE),
        "signal_bar_denominator": dict(_DENOMINATOR),
        "placements_total": sum(_BY_RULE.values()),
    }


def reset() -> None:
    """Drop the tallies. For tests only."""
    _BY_RULE.clear()
    _DENOMINATOR.clear()
