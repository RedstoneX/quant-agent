"""The no-ATR structural stop's buffer, in the NAME'S OWN movement.

Owner ruling 2026-10-04: *"self adjusting the 2.5 is not a made up number. It
is specific to its own stock move. It's the way we should be handling
everything."* A FLAT fraction of price is both a picked number and wrong on
its own terms -- 0.5% is most of a quiet utility's whole day and a rounding
error on a volatile semiconductor -- so the slack the protective stop is given
past a structural level is measured in the bar range that name actually
prints.

The denominator is the SIGNAL BAR's high-minus-low. It is already on the
``TechAnalysisResult`` the structural-stop path reads (the same object its
tier-2 prior-bar fallback reads), so this adds no data dependency, and unlike
ATR it is present exactly where this path runs -- the no-ATR case.

The multiple is DERIVED, not picked. It is the value that reproduces the flat
0.5% on a TYPICAL name, so converting the units changes no stop on a typical
name: across the 33-symbol recorded live universe
(``ops/rehearsal/recordings/market_bars.json.gz``, 60 sessions to 2026-09-30)
the median daily range is 1.904% of close, and 0.005 / 0.01904 = 0.263.

What it changes, which is the point of the ruling:

  * a QUIET name (XLF, 1.05% median range) goes from 0.50% of price to 0.28%;
  * a VOLATILE name (FLNC, 5.81%) goes from 0.50% of price to 1.53%.

Protection changes are one-way, so the tightening half is stated plainly
rather than hidden: a quiet name's stop moves closer to its level, which is
what that name's own noise argues for, and the loss per share if it fills is
correspondingly smaller. A name whose signal bar is unreadable keeps the flat
fraction, because a missing denominator must never cost a name its protection.
"""
from __future__ import annotations

import math

#: Buffer past the structural level, in multiples of the signal bar's own
#: range. See the module docstring for the derivation; ledgered as `derived`
#: in config/number_ledger.yaml.
STRUCTURAL_BUFFER_BAR_RANGE_MULTIPLE = 0.263


def signal_bar_range(analysis) -> float | None:
    """The signal bar's own high-minus-low, or None when it is not readable.

    Returns None -- never zero -- for a missing edge, a non-finite edge, or a
    non-positive span, which is the caller's signal to fall back to the flat
    fraction rather than to place a stop AT the level with no slack at all.
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


def structural_buffer_beyond(
    level_price: float,
    analysis,
    fallback_pct: float,
    *,
    is_short: bool,
) -> float:
    """Place a stop one buffer BEYOND ``level_price`` on the losing side.

    The buffer is this name's own signal-bar range times
    ``STRUCTURAL_BUFFER_BAR_RANGE_MULTIPLE``; when that range is unreadable it
    is ``fallback_pct`` of the level price, which is what every name got
    before the 2026-10-04 ruling.
    """
    span = signal_bar_range(analysis)
    if span is not None:
        offset = STRUCTURAL_BUFFER_BAR_RANGE_MULTIPLE * span
    else:
        offset = level_price * fallback_pct
    return level_price + offset if is_short else level_price - offset
