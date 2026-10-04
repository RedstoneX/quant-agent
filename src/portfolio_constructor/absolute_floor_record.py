"""Counted rows for the hard ATR floor under every level-backed stop.

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
ever shipped already sat further than 1 ATR from its entry.

So the multiple is NOT changed here, and no stop moves. Protection changes
are one-way: RAISING this floor forces a wider stop on exactly the names
whose structure is tightest, and LOWERING it permits stops tighter than
anything the desk has ever placed. Neither is justifiable off zero
observations.

What this module adds is the denominator, as COUNTED ROWS. It stores
nothing: `noted` emits one `ABS_FLOOR_ROW` line per level-backed placement
carrying that placement's observed distance in ATRs and whether the floor
bound it, and `summarise` computes the count, the tightest, the widest and
the mean by reading those rows back at the moment something asks. There is
no module-level tally and no reset, so no session can inherit another's
numbers and no test can depend on another test's order.

The two logging bodies below moved VERBATIM out of
`src/portfolio_constructor/entry_stop/resolver.py`: same format strings,
same argument expressions, same local names, so the emitted text is
byte-identical to what the resolver emitted before. The decision itself is
still the resolver's -- `inside_hard_floor` arrives already decided and is
recomputed nowhere here.
"""
from __future__ import annotations

import json
import math

from src.portfolio_constructor.config import (
    STOP_RULE_ABSOLUTE_FLOOR,
    STOP_RULE_LEVEL_HONOURED,
    logger,
)

#: Stable prefix every counted row carries, so rows can be found in a log
#: without guessing at wording.
ROW_TAG = "ABS_FLOOR_ROW "


def distance_in_atrs(entry_price: float, stop_loss: float, atr: float) -> float | None:
    """|entry - stop| in ATRs, or None when it is not readable.

    None -- never zero -- for a non-finite or non-positive ATR, so a reader
    can tell "no reading" from "a reading of nothing". This is the ROW's
    field only; the log lines below keep the resolver's own expression.
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


def emit_row(symbol: str, rule: str, bound: bool, observed: float | None) -> str:
    """Write one counted row for a level-backed placement and return it."""
    row = json.dumps(
        {"symbol": symbol, "rule": rule, "floor_bound": bound,
         "distance_atr": observed},
        sort_keys=True,
    )
    logger.info("%s%s", ROW_TAG, row)
    return row


def rows_from(lines) -> list[dict]:
    """Every counted row in `lines`, in order. Unparseable rows are skipped."""
    out: list[dict] = []
    for line in lines:
        head, sep, tail = str(line).partition(ROW_TAG)
        if not sep:
            continue
        try:
            out.append(json.loads(tail.strip()))
        except ValueError:
            continue
    return out


def summarise(rows) -> dict[str, object]:
    """Count the rows and read their spread back; nothing is stored.

    `tightest_atr_seen` is the live answer to the ledger row's open question:
    the closest any level-backed stop has actually been placed to its entry.
    None while nothing has been seen -- never a stand-in number.
    """
    rows = list(rows)
    binds = sum(1 for r in rows if r.get("floor_bound"))
    seen = [
        float(r["distance_atr"]) for r in rows
        if isinstance(r.get("distance_atr"), (int, float))
    ]
    return {
        "level_backed_total": len(rows),
        "floor_binds": binds,
        "floor_clears": len(rows) - binds,
        "distance_readings": len(seen),
        "tightest_atr_seen": min(seen) if seen else None,
        "widest_atr_seen": max(seen) if seen else None,
        "mean_atr_seen": (sum(seen) / len(seen)) if seen else None,
    }


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
    multiple: float,
    band_edge: float,
) -> tuple[float, str]:
    """Count one level-backed stop and return its `(honoured, rule)` unchanged.

    `inside_hard_floor` is the caller's own verdict, recomputed nowhere here:
    this module observes the decision, it does not make it. `honoured` and
    `rule` are bound to exactly the values the resolver bound them to, so
    the log bodies below are the resolver's verbatim.
    """
    if inside_hard_floor:
        # Real structure, still too close to survive one ordinary session.
        # Pushed out to the 1x floor and NOT to the full band -- the band
        # is what §12.1 removed. See the module docstring for why this
        # floor lives in code rather than in a prompt.
        honoured, rule = hard_floor, STOP_RULE_ABSOLUTE_FLOOR
        emit_row(symbol, rule, True, distance_in_atrs(entry_price, stop_loss, atr))
        logger.info(
            "Constructor: %s %s stop $%.2f → $%.2f [%s] — it "
            "sits at the computed structural level $%.2f, "
            "which is real, but only %.2f ATRs from the "
            "$%.2f entry. A stop inside one ordinary day's "
            "range is a coin flip, so it is moved out to the "
            "%.2f x ATR floor — not to the %.2f x ATR noise "
            "band, which the level exempts it from.",
            side_label, symbol, stop_loss, honoured,
            STOP_RULE_ABSOLUTE_FLOOR, level,
            abs(entry_price - stop_loss) / atr, entry_price,
            floor_multiple, multiple,
        )
        return honoured, rule

    honoured, rule = stop_loss, STOP_RULE_LEVEL_HONOURED
    emit_row(symbol, rule, False, distance_in_atrs(entry_price, stop_loss, atr))
    logger.info(
        "Constructor: %s %s stop $%.2f kept [%s] — it "
        "sits at the computed structural level $%.2f "
        "(%.2f ATRs %s the $%.2f entry). The %.2f x ATR "
        "noise band would have moved it to $%.2f, which "
        "is not a level anyone is defending, so the band "
        "does not apply.",
        side_label, symbol, stop_loss,
        STOP_RULE_LEVEL_HONOURED, level,
        abs(entry_price - stop_loss) / atr, side_word,
        entry_price, multiple, band_edge,
    )
    return honoured, rule
