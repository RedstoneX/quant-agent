"""The ALIGNMENT EXIT — the desk's only sanctioned way to realise a gain.

Owner ruling, 2026-09-30: *"exit on ALIGNMENT, never on a target"*, and the
same day's correction: *"it doesn't have to be all three in alignment.
Structure, volatility and trend don't all have to agree. Just agree in
alignment. The chart alone can tell us what to do."*

So this is NOT a three-way vote, and it is deliberately not written as
`structure AND atr AND sma_cross`. A unanimity rule gives each detector a
veto, almost never fires, and leaving the desk unable to realise a gain is
exactly the hole this module exists to close.

ONE READING, NOT A QUORUM
-------------------------
The reading is: **an uptrend is over when the last thing holding it up has
been given up, by more than this instrument's own noise.**

Structure, trend and volatility are not three voters here. They are the
three things a chart gives you when you read it, and each plays a
different, non-interchangeable part in that single sentence:

  * STRUCTURE and TREND both supply MARKS — the lines the position has
    been standing on. A confirmed-broken structural level is a mark. The
    moving average the position's own thesis names is a mark. The next
    longer average the desk computes is a mark. They are the same kind of
    thing (a price the chart says matters), read off the same bars, so
    they go into one set rather than into separate ballots.
  * VOLATILITY supplies the UNIT and the TOLERANCE. It is not a condition
    at all; it is how the single distance below the last mark is judged
    to be real rather than noise.

The verdict therefore comes from ONE comparison: how far below the LOWEST
live mark price has closed, measured in this name's own ATR, against this
name's own noise band. A chart that plainly says the move is over says it
through that comparison — with whichever marks it actually presents. A
position with only a structural level can exit on it. A position with only
its averages can exit on it. Neither has to wait for the other to line up.

WHY THERE ARE NO NEW NUMBERS
----------------------------
Nothing in this module was picked:

  * Marks are read off the instrument. The structural mark is whatever
    `exit_guard.check_structural_protection` already confirmed broken —
    this module does not re-derive levels, touch counts or the cross-day
    confirmation gate. The trend marks are plain arithmetic averages of
    the closes.
  * Which averages, per name, is read off the POSITION'S OWN thesis:
    whatever period its `thesis_invalid_if` names, parsed by the same
    regex `exit_guard.check_thesis_invalid_if` uses, plus the next longer
    period the pipeline computes. Only 20/50/200 are ever computed
    upstream, so that ladder is fully determined and no pair is chosen
    here. A thesis naming no average simply contributes no trend mark —
    it does not get one invented for it.
  * The tolerance is `exit_guard.noise_band_atr(...)`, the desk's existing
    ratified band with its existing sqrt(sessions) scaling, multiplied by
    this name's ATR right now. A quiet stock and a violent one get
    different tolerances from the same code. That is the per-name read the
    owner asked for, and it is why no global "give back N%" dial appears.

WHAT THIS MODULE REFUSES TO DO
------------------------------
It never exits on a price target. It never summarises history: a 2026-09
attempt to source an exit distance from the MEDIAN of a stock's past
adverse moves was rejected, because any summary statistic of a
distribution is a hidden choice of percentile, and "read off the
instrument" cannot be satisfied by summarising that instrument's past. The
only history touched here is the close series an average is computed from
— a reading of the tape's present state, not a fit to it.

It is also never anchored to the entry price. What the desk paid says
nothing about whether a trend has ended.

It does not place, size or time an order, and it does not trim. It returns
a verdict.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal

from src.risk.exit_guard import _MA_REF_RE, noise_band_atr

__all__ = [
    "AlignmentExitCheck",
    "SMA_LADDER",
    "ChartMark",
    "check_alignment_exit",
    "simple_moving_average",
    "thesis_ma_period",
]


#: thesis-named period -> the next longer period the pipeline computes.
#: Not a tunable: 20/50/200 are the only averages computed anywhere
#: upstream (see `exit_guard._SUPPORTED_MA_PERIODS`), so "the next longer
#: one" has exactly one answer for 20 and 50 and none for 200. A thesis
#: riding the MA200 contributes that one mark and no slower partner,
#: rather than being paired with something invented.
SMA_LADDER: dict[int, int] = {20: 50, 50: 200}


@dataclass(frozen=True)
class ChartMark:
    """One price the chart says matters for this position, and where it
    came from. Marks are compared to each other, so they are all in the
    instrument's own price units by construction."""

    price: float
    source: str


@dataclass(frozen=True)
class AlignmentExitCheck:
    """Verdict on whether the chart says this position's move is over.

      "EXIT"        - price has closed below (above, for a short) the last
                      mark holding the trend, by more than this name's own
                      noise band. `owner_reason` says so in plain language.
      "HOLD"        - the last mark is still holding, or price has only
                      slipped through it by an amount inside the noise.
      "UNPARSEABLE" - the chart presents no live mark at all, or no ATR to
                      judge distance with. Treated exactly as HOLD by
                      callers; a separate value only so the desk can tell
                      "the trend is intact" apart from "the desk could not
                      read the chart", and report the true state of each.
    """

    status: Literal["EXIT", "HOLD", "UNPARSEABLE"]
    marks: tuple[ChartMark, ...]
    last_mark: ChartMark | None
    breach_atrs: float | None
    band_atrs: float | None
    reason: str
    owner_reason: str | None = None

    @property
    def exit_cleared(self) -> bool:
        return self.status == "EXIT"


def _finite(x: float | None) -> float | None:
    try:
        v = float(x)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def simple_moving_average(closes: list[float], period: int) -> float | None:
    """Arithmetic mean of the last `period` closes, or None.

    Plain SMA — no weighting, no smoothing parameter, nothing to tune.
    None when fewer than `period` finite closes exist: a short series is a
    missing reading, never a shorter average silently substituted.
    """
    if period <= 0:
        return None
    vals = [v for v in (_finite(c) for c in closes) if v is not None]
    if len(vals) < period:
        return None
    return sum(vals[-period:]) / float(period)


def thesis_ma_period(thesis_invalid_if: str | None) -> int | None:
    """The average this position's OWN thesis names, if any.

    Same text and same regex `exit_guard.check_thesis_invalid_if` reads,
    so the alignment exit and the invalidation check can never disagree
    about which average a position is riding.
    """
    m = _MA_REF_RE.search(thesis_invalid_if or "")
    if not m:
        return None
    period = int(m.group(1) or m.group(2))
    return period if period in (20, 50, 200) else None


def check_alignment_exit(
    *,
    thesis_invalid_if: str | None,
    closes: list[float],
    atr: float | None,
    broken_structural_level: float | None = None,
    is_short: bool = False,
) -> AlignmentExitCheck:
    """Read one position's chart and say whether its move is over.

    `closes` are that symbol's daily closes in ascending date order,
    ending on the latest COMPLETED session — the same close-based basis
    `pipeline._structural_protection_for_holding` already insists on,
    because a level, an average and a breach all have to be read off a
    finished bar or a routine intraday wick reads as the end of a trend.

    `broken_structural_level` is the level
    `exit_guard.check_structural_protection` already confirmed broken for
    this position, if it found one; this function does not re-derive it,
    and passing None simply means the chart offered no structural mark.

    `is_short` mirrors the whole reading: a short's trend ends when price
    closes ABOVE the highest mark above it by more than the noise band.
    """
    a = _finite(atr)
    series = [v for v in (_finite(c) for c in closes) if v is not None]
    if not series:
        return AlignmentExitCheck(
            "UNPARSEABLE", (), None, None, None,
            "no completed closes to read the chart from",
        )
    last = series[-1]

    # --- collect the marks the chart actually presents ------------------
    marks: list[ChartMark] = []
    lvl = _finite(broken_structural_level)
    if lvl is not None:
        marks.append(ChartMark(lvl, "confirmed-broken structural level"))
    fast = thesis_ma_period(thesis_invalid_if)
    if fast is not None:
        for period in (fast, SMA_LADDER.get(fast)):
            if period is None:
                continue
            v = simple_moving_average(series, period)
            if v is not None:
                marks.append(ChartMark(v, f"MA{period} (thesis rides the MA{fast})"))

    # Only marks price has actually given up are live for this reading —
    # a mark still above (below, for a short) the close is one the trend is
    # still standing on, and the reading is about the LAST one given up.
    breached = [m for m in marks if (last > m.price if is_short else last < m.price)]
    if not marks:
        return AlignmentExitCheck(
            "UNPARSEABLE", (), None, None, None,
            "the chart presents no mark for this position — no confirmed "
            "structural break and no thesis-named average — so there is "
            "nothing to read; refusing to invent one",
        )
    if a is None or a <= 0:
        return AlignmentExitCheck(
            "UNPARSEABLE", tuple(marks), None, None, None,
            "no ATR for this name — the noise band that judges a breach "
            "cannot be read off the instrument",
        )
    if not breached:
        held = min(marks, key=lambda m: m.price) if is_short else max(
            marks, key=lambda m: m.price
        )
        return AlignmentExitCheck(
            "HOLD", tuple(marks), None, None, None,
            f"close {last:.4f} is still holding above {held.price:.4f} "
            f"[{held.source}] — the move is not over",
        )

    # THE LAST THING HOLDING THE TREND UP: of the marks price has given
    # up, the one it gave up LAST is the lowest (highest, for a short).
    # That is the reading — not a tally of how many were given up.
    last_mark = max(breached, key=lambda m: m.price) if is_short else min(
        breached, key=lambda m: m.price
    )
    breach = (last - last_mark.price) if is_short else (last_mark.price - last)

    # Sessions since price last closed on the holding side of that mark —
    # read off this chart, not assumed. The desk's ratified band widens
    # with sqrt(sessions), so a mark only just lost is judged against a
    # tight band and one lost a while ago against a wider one.
    sessions = 0
    for v in reversed(series):
        if (v <= last_mark.price) if is_short else (v >= last_mark.price):
            break
        sessions += 1
    band_atrs = noise_band_atr(sessions)
    band = band_atrs * a
    breach_atrs = breach / a

    detail = (
        f"close {last:.4f} is {breach:.4f} ({breach_atrs:.2f} ATR) "
        f"{'above' if is_short else 'below'} {last_mark.price:.4f} "
        f"[{last_mark.source}], the last of {len(marks)} chart mark(s) it "
        f"was standing on; this name's noise band {sessions} session(s) "
        f"after that mark was lost is {band_atrs:.2f} ATR ({band:.4f})"
    )
    if breach > band:
        return AlignmentExitCheck(
            "EXIT", tuple(marks), last_mark, breach_atrs, band_atrs, detail,
            owner_reason=(
                f"Trend alignment over: the last line this position was "
                f"standing on — {last_mark.source} at {last_mark.price:.2f} — "
                f"has been given up, and price has closed "
                f"{breach_atrs:.1f}x this name's own ATR "
                f"{'above' if is_short else 'below'} it, more than its own "
                f"noise band of {band_atrs:.1f}x. The desk is selling "
                f"because the move it was riding has ended on the chart, "
                f"not because price reached any target."
            ),
        )
    return AlignmentExitCheck(
        "HOLD", tuple(marks), last_mark, breach_atrs, band_atrs,
        f"holding — the slip through the last mark is inside this name's "
        f"own noise. {detail}",
    )
