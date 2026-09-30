"""The ALIGNMENT EXIT — the desk's only sanctioned way to realise a gain.

Owner ruling, 2026-09-30: *"exit on ALIGNMENT, never on a target"*, and the
same day's correction: *"it doesn't have to be all three in alignment.
Structure, volatility and trend don't all have to agree. Just agree in
alignment. The chart alone can tell us what to do."*

So this is NOT a three-way vote and NOT a quorum. It is ONE READING.

  * STRUCTURE and TREND supply MARKS — prices the chart says matter. A
    confirmed-broken structural level is a mark. The moving average the
    position's own thesis names is a mark, as is the next longer average
    the desk already computes. Same kind of thing, read off the same
    bars, so they go into one set rather than into separate ballots.
  * VOLATILITY supplies the UNIT and the TOLERANCE. It is not a condition;
    it is how the single distance below the last mark is judged real.

ONE comparison decides: how far below the LAST mark price has closed, in
this name's own ATR, against the give-back tolerance below. A position
with only a structural mark can exit on it; a position with only its
averages can exit on it. Neither waits for the other.

It never exits on a price TARGET, never summarises the instrument's past
into a statistic, and is never anchored to what the desk paid.

WHY THIS DOES NOT REUSE `exit_guard.NOISE_BAND_ATR_MULTIPLE`
------------------------------------------------------------
The first attempt at this module (PR 837, closed on review) inherited that
constant as its whole exit tolerance. It is the wrong number for this job,
on the desk's own record. `config/number_ledger.yaml` classifies it
`arbitrary`, states that it measures "how far an adverse move must travel
FROM ENTRY", and records the 2026-09-26 research finding that every
published analogue sits between roughly 2.8 and 3.5 ATR while this
constant is 1.0. The ledger declined to move it to 3.0 for one stated
reason: the published systems measure give-back from a RUNNING EXTREME,
not excess over noise from ENTRY, so they were "the closest published
analogue, not an exact fit".

That mismatch does not apply here. This module measures give-back from a
RUNNING REFERENCE — a moving average or a structural level as it stands
now — which is precisely the quantity those systems measure. The reason
the ledger gave for leaving the entry-anchored constant alone is the
reason a chart-referenced tolerance may be sourced to them directly.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal

from src.risk.exit_guard import _MA_REF_RE

__all__ = [
    "ALIGNMENT_GIVE_BACK_ATR_MULTIPLE",
    "AlignmentExitCheck",
    "SMA_LADDER",
    "ChartMark",
    "check_alignment_exit",
    "simple_moving_average",
    "thesis_ma_period",
]


#: Give-back below the last chart mark, in this name's own ATR, before the
#: desk reads the move as over.
#:
#: SOURCED, not picked, and deliberately NOT `exit_guard`'s 1.0 (see the
#: module note). Wilder's Volatility System sets its ARC at roughly
#: 2.8-3.1 ATR (New Concepts in Technical Trading Systems, 1978); Chuck Le
#: Beau's Chandelier Exit uses 3.0 x ATR as its published default, with 2.5
#: quoted as a tight variant and 3.5-4.0 as loose. Both measure give-back
#: from a RUNNING REFERENCE, which is what a chart mark is. 3.0 is the
#: default of the named system and the centre of the published band; the
#: band itself, not this module, is the appetite dial, and moving inside
#: 2.5-3.5 is an owner setting rather than a new invention.
#:
#: NO sqrt(sessions) widening is applied. That scaling belongs to the
#: entry-anchored band, where the question is how long a position has had
#: to prove itself. The reference here moves with the tape every session,
#: so widening the tolerance as well would double-count the same passage of
#: time and let a trend that ended weeks ago keep earning slack.
ALIGNMENT_GIVE_BACK_ATR_MULTIPLE: float = 3.0

#: thesis-named period -> the next longer period the pipeline computes.
#: Not a tunable: 20/50/200 are the only averages computed anywhere
#: upstream, so "the next longer one" has exactly one answer for 20 and 50
#: and none for 200.
SMA_LADDER: dict[int, int] = {20: 50, 50: 200}

#: Durable, machine-readable verdict codes. Every read writes one of these
#: per symbol, INCLUDING the states where the desk could not read the chart
#: — the closed first attempt recorded nothing on those paths, which left
#: the desk unable to tell a held position from an unreadable one.
CODE_EXIT = "alignment_exit_confirmed"
CODE_HOLD = "alignment_intact"
CODE_NO_MARK = "alignment_unreadable_no_mark"
CODE_NO_ATR = "alignment_unreadable_no_atr"
CODE_NO_CLOSES = "alignment_unreadable_no_closes"


@dataclass(frozen=True)
class ChartMark:
    """One price the chart says matters for this position, and where it
    came from."""

    price: float
    source: str


@dataclass(frozen=True)
class AlignmentExitCheck:
    """Verdict on whether the chart says this position's move is over.

      "EXIT"        - price has given up the last mark holding the trend by
                      more than `ALIGNMENT_GIVE_BACK_ATR_MULTIPLE` ATR.
      "HOLD"        - the last mark still holds, or the slip through it is
                      inside the tolerance.
      "UNPARSEABLE" - no live mark, no ATR, or no closes. Treated as HOLD by
                      callers; a separate value so the desk can report the
                      true state instead of a silent hold.
    """

    status: Literal["EXIT", "HOLD", "UNPARSEABLE"]
    code: str
    marks: tuple[ChartMark, ...]
    last_mark: ChartMark | None
    breach_atrs: float | None
    band_atrs: float | None
    reason: str
    #: The MA period PARSED OUT OF THE POSITION'S THESIS PROSE at the moment
    #: this verdict was formed, or None. Recorded because the prose is
    #: model-written and unversioned: a reworded thesis silently changes
    #: which price decides a sale, and without this field the record of the
    #: sale would not say which average actually decided it.
    thesis_ma_period: int | None = None
    #: Verbatim thesis text the period was parsed from, truncated. Pins the
    #: input so a later reword is visible as a difference.
    thesis_text: str = ""
    sessions_since_mark_lost: int | None = None
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
    """Arithmetic mean of the last `period` closes, or None when short."""
    if period <= 0:
        return None
    vals = [v for v in (_finite(c) for c in closes) if v is not None]
    if len(vals) < period:
        return None
    return sum(vals[-period:]) / float(period)


def thesis_ma_period(thesis_invalid_if: str | None) -> int | None:
    """The average this position's OWN thesis names, if any.

    Same regex `exit_guard.check_thesis_invalid_if` reads, so the alignment
    exit and the invalidation check can never disagree about which average
    a position is riding. See `AlignmentExitCheck.thesis_ma_period` for why
    the answer is recorded with every verdict.
    """
    m = _MA_REF_RE.search(thesis_invalid_if or "")
    if not m:
        return None
    period = int(m.group(1) or m.group(2))
    return period if period in (20, 50, 200) else None


def _sessions_since_mark_lost(
    series: list[float], mark: ChartMark, period: int | None, *, is_short: bool
) -> int:
    """How many completed sessions price has been on the wrong side of this
    mark.

    THE FIX FOR THE CLOSED ATTEMPT'S SIXTH DEFECT. That version walked
    historical closes backwards and compared every one of them against
    TODAY's moving-average value. An average that has fallen since is a
    lower bar than the one those closes actually faced, so closes that were
    above their own average at the time were counted as "already lost",
    inflating the count.

    For a moving-average mark, each close is compared against the average
    AS IT STOOD on that session. A structural level does not move, so it is
    compared against itself.
    """
    n = len(series)
    sessions = 0
    for i in range(n - 1, -1, -1):
        if period is None:
            ref: float | None = mark.price
        else:
            ref = simple_moving_average(series[: i + 1], period)
            if ref is None:
                break  # series too short to know; stop counting rather than guess
        close = series[i]
        held = (close <= ref) if is_short else (close >= ref)
        if held:
            break
        sessions += 1
    return sessions


def check_alignment_exit(
    *,
    thesis_invalid_if: str | None,
    closes: list[float],
    atr: float | None,
    broken_structural_level: float | None = None,
    is_short: bool = False,
) -> AlignmentExitCheck:
    """Read one position's chart and say whether its move is over.

    `closes` are that symbol's daily closes in ascending date order, ending
    on the latest COMPLETED session — the same close-based basis
    `pipeline._structural_protection_for_holding` insists on, because a
    level, an average and a give-back all have to be read off a finished
    bar or a routine intraday wick reads as the end of a trend.

    `broken_structural_level` is the level `check_structural_protection`
    has already CONFIRMED broken for this position, if any. This function
    does not re-derive levels, touch counts or the cross-day confirmation
    gate; None simply means the chart offered no structural mark.
    """
    a = _finite(atr)
    fast = thesis_ma_period(thesis_invalid_if)
    thesis_text = (thesis_invalid_if or "")[:400]
    series = [v for v in (_finite(c) for c in closes) if v is not None]
    if not series:
        return AlignmentExitCheck(
            "UNPARSEABLE", CODE_NO_CLOSES, (), None, None, None,
            "no completed closes to read the chart from",
            thesis_ma_period=fast, thesis_text=thesis_text,
        )
    last = series[-1]

    # --- the marks the chart actually presents --------------------------
    marks: list[ChartMark] = []
    mark_periods: dict[str, int | None] = {}
    lvl = _finite(broken_structural_level)
    if lvl is not None:
        m = ChartMark(lvl, "confirmed-broken structural level")
        marks.append(m)
        mark_periods[m.source] = None
    if fast is not None:
        for period in (fast, SMA_LADDER.get(fast)):
            if period is None:
                continue
            v = simple_moving_average(series, period)
            if v is not None:
                m = ChartMark(v, f"MA{period} (thesis rides the MA{fast})")
                marks.append(m)
                mark_periods[m.source] = period

    if not marks:
        return AlignmentExitCheck(
            "UNPARSEABLE", CODE_NO_MARK, (), None, None, None,
            "the chart presents no mark for this position — no confirmed "
            "structural break and no thesis-named average — so there is "
            "nothing to read; refusing to invent one",
            thesis_ma_period=fast, thesis_text=thesis_text,
        )
    if a is None or a <= 0:
        return AlignmentExitCheck(
            "UNPARSEABLE", CODE_NO_ATR, tuple(marks), None, None, None,
            "no ATR for this name — the tolerance that judges a give-back "
            "cannot be read off the instrument",
            thesis_ma_period=fast, thesis_text=thesis_text,
        )

    breached = [m for m in marks if (last > m.price if is_short else last < m.price)]
    if not breached:
        held = min(marks, key=lambda m: m.price) if is_short else max(
            marks, key=lambda m: m.price
        )
        return AlignmentExitCheck(
            "HOLD", CODE_HOLD, tuple(marks), None, None, None,
            f"close {last:.4f} is still holding against {held.price:.4f} "
            f"[{held.source}] — the move is not over",
            thesis_ma_period=fast, thesis_text=thesis_text,
        )

    # THE LAST THING HOLDING THE TREND UP: of the marks price has given up,
    # the one it gave up LAST is the lowest (highest, for a short).
    last_mark = max(breached, key=lambda m: m.price) if is_short else min(
        breached, key=lambda m: m.price
    )
    breach = (last - last_mark.price) if is_short else (last_mark.price - last)
    breach_atrs = breach / a
    band_atrs = ALIGNMENT_GIVE_BACK_ATR_MULTIPLE
    band = band_atrs * a
    sessions = _sessions_since_mark_lost(
        series, last_mark, mark_periods.get(last_mark.source), is_short=is_short
    )

    detail = (
        f"close {last:.4f} is {breach:.4f} ({breach_atrs:.2f} ATR) "
        f"{'above' if is_short else 'below'} {last_mark.price:.4f} "
        f"[{last_mark.source}], the last of {len(marks)} chart mark(s) it "
        f"was standing on, lost {sessions} session(s) ago; the give-back "
        f"tolerance is {band_atrs:.2f} ATR ({band:.4f})"
    )
    if breach > band:
        return AlignmentExitCheck(
            "EXIT", CODE_EXIT, tuple(marks), last_mark, breach_atrs, band_atrs,
            detail, thesis_ma_period=fast, thesis_text=thesis_text,
            sessions_since_mark_lost=sessions,
            owner_reason=(
                f"Trend alignment over: the last line this position was "
                f"standing on — {last_mark.source} at {last_mark.price:.2f} — "
                f"has been given up, and price has closed "
                f"{breach_atrs:.1f}x this name's own ATR "
                f"{'above' if is_short else 'below'} it, more than the "
                f"{band_atrs:.1f}x give-back the desk reads as the end of a "
                f"move. Selling because the move it was riding has ended on "
                f"the chart, not because price reached any target."
            ),
        )
    return AlignmentExitCheck(
        "HOLD", CODE_HOLD, tuple(marks), last_mark, breach_atrs, band_atrs,
        f"holding — the slip through the last mark is inside the give-back "
        f"tolerance. {detail}",
        thesis_ma_period=fast, thesis_text=thesis_text,
        sessions_since_mark_lost=sessions,
    )
