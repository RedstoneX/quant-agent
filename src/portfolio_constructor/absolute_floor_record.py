"""Counted record of the hard ATR floor under every level-backed stop.

`risk.absolute_min_stop_atr_multiple` (ledger id
`src.config.RiskConfig.absolute_min_stop_atr_multiple`, value 1, filed
`arbitrary`) is the last line of defence under a stop: a stop that sits on a
real computed structural level is honoured however tight DOWN TO this many
ATRs, and inside it the stop is pushed back out to exactly the floor.

**MEASURED, production DB read-only, 2026-10-04.** Across 84 trades, 725
agent logs, 14 trade refusals and 14,179 specialist-evidence rows there are
9 recorded `stop_honoured_at_computed_level` placements and ZERO
`stop_widened_to_absolute_atr_floor` ones. The floor has NEVER bound. That
is NEVER EXERCISED, not broken: every level-backed stop the live desk has
ever shipped already sat further than 1 ATR from its entry, so the floor had
nothing to do.

So the multiple is NOT changed here. Re-deriving a protection number from
bars, on a path with a zero denominator, is the trap two earlier
measurements on this desk fell into. Protection changes are one-way:
RAISING this floor forces a wider stop on exactly the names whose structure
is tightest, and LOWERING it permits stops tighter than anything the desk
has ever placed. Neither is justifiable off zero observations.

What this module adds is the denominator. It CHANGES NO BEHAVIOUR: `noted`
returns the same `(honoured, rule)` pair the caller would have computed
without it, and emits the same two log lines. It additionally counts, per
desk session:

  * how often the level-honoured branch is reached at all;
  * how often the floor actually BINDS (the stop is widened to it);
  * the distance-in-ATRs of every level-backed stop seen, so the next
    session can read the real distribution rather than assume one.

The third is the measurement the ledger row's settle condition asks for,
taken on the desk's own live placements instead of on an offline universe
proxy. Counts are per process, which is one desk session; `snapshot` reads
them back and `reset` exists for tests, which must never inherit another
test's tallies.
"""
from __future__ import annotations

import math
from collections import Counter

from src.portfolio_constructor.config import (
    STOP_RULE_ABSOLUTE_FLOOR,
    STOP_RULE_LEVEL_HONOURED,
    logger,
)

#: Level-backed stop outcomes, e.g. {"floor_bound": 0, "floor_clear": 9}.
_OUTCOMES: Counter[str] = Counter()

#: Running summary of entry-to-stop distance in ATRs across every
#: level-backed stop seen. Summed rather than listed so nothing here needs a
#: cap, and so no number in this module could ever govern a trade.
_SEEN: dict[str, float | None] = {"n": 0, "sum": 0.0, "min": None, "max": None}


def distance_in_atrs(entry_price: float, stop_loss: float, atr: float) -> float | None:
    """|entry - stop| in ATRs, or None when it is not readable.

    None -- never zero -- for a non-finite or non-positive ATR, so a reader
    can tell "no reading" from "a reading of nothing".
    """
    try:
        entry = float(entry_price)
        stop = float(stop_loss)
        unit = float(atr)
    except (TypeError, ValueError):
        return None
    if not (math.isfinite(entry) and math.isfinite(stop) and math.isfinite(unit)):
        return None
    if unit <= 0:
        return None
    return abs(entry - stop) / unit


def noted(
    *,
    inside_hard_floor: bool,
    symbol: str,
    side_label: str,
    side_word: str,
    entry_price: float,
    stop_loss: float,
    atr: float,
    level: float,
    hard_floor: float,
    floor_multiple: float,
    band_multiple: float,
    band_edge: float,
) -> tuple[float, str]:
    """Count one level-backed stop and return its `(honoured, rule)` unchanged.

    `inside_hard_floor` is the caller's own verdict, recomputed nowhere here:
    this module observes the decision, it does not make it.
    """
    observed = distance_in_atrs(entry_price, stop_loss, atr)
    if observed is not None:
        _SEEN["n"] += 1
        _SEEN["sum"] += observed
        lo, hi = _SEEN["min"], _SEEN["max"]
        _SEEN["min"] = observed if lo is None else min(lo, observed)
        _SEEN["max"] = observed if hi is None else max(hi, observed)
    _OUTCOMES["floor_bound" if inside_hard_floor else "floor_clear"] += 1
    shown = float("nan") if observed is None else observed

    if inside_hard_floor:
        # Real structure, still too close to survive one ordinary session.
        # Pushed out to the floor and NOT to the full band -- the band is
        # what §12.1 removed. See the module docstring for why this floor
        # lives in code rather than in a prompt.
        logger.info(
            "Constructor: %s %s stop $%.2f → $%.2f [%s] — it sits at the "
            "computed structural level $%.2f, which is real, but only %.2f "
            "ATRs from the $%.2f entry. A stop inside one ordinary day's "
            "range is a coin flip, so it is moved out to the %.2f x ATR "
            "floor — not to the %.2f x ATR noise band, which the level "
            "exempts it from.",
            side_label, symbol, stop_loss, hard_floor,
            STOP_RULE_ABSOLUTE_FLOOR, level, shown, entry_price,
            floor_multiple, band_multiple,
        )
        return hard_floor, STOP_RULE_ABSOLUTE_FLOOR

    logger.info(
        "Constructor: %s %s stop $%.2f kept [%s] — it sits at the computed "
        "structural level $%.2f (%.2f ATRs %s the $%.2f entry). The %.2f x "
        "ATR noise band would have moved it to $%.2f, which is not a level "
        "anyone is defending, so the band does not apply.",
        side_label, symbol, stop_loss, STOP_RULE_LEVEL_HONOURED, level,
        shown, side_word, entry_price, band_multiple, band_edge,
    )
    return stop_loss, STOP_RULE_LEVEL_HONOURED


def snapshot() -> dict[str, object]:
    """What this session has counted so far, as plain values.

    `tightest_atr_seen` is the live answer to the ledger row's open question:
    the closest any level-backed stop has actually been placed to its entry.
    None while nothing has been seen -- never a stand-in number.
    """
    return {
        "level_backed_by_outcome": dict(_OUTCOMES),
        "level_backed_total": sum(_OUTCOMES.values()),
        "floor_binds": _OUTCOMES["floor_bound"],
        "distance_readings": int(_SEEN["n"]),
        "tightest_atr_seen": _SEEN["min"],
        "widest_atr_seen": _SEEN["max"],
        "mean_atr_seen": (_SEEN["sum"] / _SEEN["n"]) if _SEEN["n"] else None,
    }


def reset() -> None:
    """Drop the tallies. For tests only."""
    _OUTCOMES.clear()
    _SEEN.update({"n": 0, "sum": 0.0, "min": None, "max": None})
