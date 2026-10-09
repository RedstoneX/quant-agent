"""The structural-protection fallback band's ANCHOR, as pure functions.

Lifted out of `src/risk/exit_guard.py` (an oversized file) so this can be
built and exercised alone. No imports from `exit_guard`, no constants, no
decisions: the caller hands in every number, including the band multiple.

ANCHOR, MEASURED 2026-10-04 (19 symbols, 9,519 daily bars, 2024-09-30..
2026-09-29, 424 trail-stop exit events, each home's real control flow
modelled) — the anchor is NOT a free choice, it is per-home:

  * `check_structural_protection`'s fallback: re-anchoring removes 54 blocks
    and adds NONE. That is a property of its control flow, not of the sample:
    the anchor is never worse for the holder than entry, so the measured
    adverse move can only grow; if the grown move is still <= 0 the old one
    was too (blocked then, blocked now), and if it is positive but inside
    the band the old one was inside it too. There is no early return between
    the two, so a block can only be released, never created. It passes
    `extreme_since_entry`.
  * The midday position reviewer was the other home, ENTRY-anchored. That
    gate was REMOVED on 2026-10-09 (owner ruling: no sale is refused for the
    price the desk paid), together with its sqrt(sessions) width helper, so
    this module now serves the fallback alone.
"""

from __future__ import annotations

import math

__all__ = ["anchored_adverse_move", "noise_band_anchor"]


def _finite(value: object) -> float | None:
    try:
        out = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def noise_band_anchor(
    entry: float,
    extreme_since_entry: float | None,
    *,
    is_short: bool,
) -> float:
    """The price the noise band is measured FROM: the running extreme since
    entry (highest high for a long, lowest low for a short), falling back to
    entry when no extreme is available.

    Clamped so the anchor is never WORSE for the holder than entry — a long
    anchors at `max(entry, highest_high)`, a short at `min(entry, lowest_low)`.
    A real running extreme since entry already satisfies the clamp; it bites
    only on a malformed or stale value.

    The clamp alone does NOT make re-anchoring block-free: a larger adverse
    move can bring a position that was flat-or-winning versus entry INSIDE the
    band for the first time. Whether that adds a block depends on what the
    calling home does with a non-adverse move, so the no-added-blocks property
    belongs to a home, not to this function — see
    `check_structural_protection`, which already blocks on `adverse <= 0` and
    therefore can only lose blocks by re-anchoring.
    """
    ext = _finite(extreme_since_entry) if extreme_since_entry is not None else None
    if ext is None or ext <= 0:
        return entry
    return min(entry, ext) if is_short else max(entry, ext)


def anchored_adverse_move(
    entry: float,
    current_price: float,
    extreme_since_entry: float | None,
    *,
    is_short: bool,
) -> tuple[float, float]:
    """`(anchor, adverse)`: the anchor price and the move AGAINST the holder
    from it (positive = hurt). A short is hurt by price rising."""
    anchor = noise_band_anchor(entry, extreme_since_entry, is_short=is_short)
    adverse = (current_price - anchor) if is_short else (anchor - current_price)
    return anchor, adverse
