"""The ALIGNMENT EXIT — the desk's only sanctioned way to realise a gain.

Owner ruling, 2026-09-30: *"exit on ALIGNMENT, never on a target"*, and the
same day's correction: *"it doesn't have to be all three in alignment.
Structure, volatility and trend don't all have to agree. Just agree in
alignment. The chart alone can tell us what to do."*

So this is NOT a three-way vote and NOT a quorum. It is ONE READING.

  * STRUCTURE and TREND supply MARKS — prices the chart says matter. A
    confirmed-broken structural level is a mark. The moving average the
    position's own thesis names is a mark, as is the next longer average
    the desk already computes; when the thesis names no average the desk
    computes, the chart's OWN averages are the marks instead, so no
    position is left unreadable by how a model happened to word its
    thesis. Same kind of thing, read off the same
    bars, so they go into one set rather than into separate ballots.
  * VOLATILITY supplies the UNIT and the TOLERANCE. It is not a condition;
    it is how the single distance below the last mark is judged real.

ONE comparison decides: how far below the LAST mark price has closed, in
this name's own ATR, against the give-back tolerance below. A position
with only a structural mark can exit on it; a position with only its
averages can exit on it. Neither waits for the other.

It never exits on a price TARGET alone, never summarises the instrument's
past into a statistic, and is never anchored to what the desk paid.

THE TARGET IS ONE VOTE (owner ruling, 2026-10-08: *"one vote toward the
alignment exit. Can tip a close when other signals agree. But never sells
alone."*). Once a completed close has reached the CURRENT target on or
after the date it took effect, the give-back is measured from the HIGHEST
lost mark (lowest for a short) instead of the lowest. No lost mark: hold.
One lost mark: nothing changes. Two or more: the vote can tip a close that
would otherwise hold. Not reached, or no target: today's rule, said so.

THE TOLERANCE IS AN UNSETTLED LEDGER ROW — not sourced (Wilder/Le Beau
measure from a running EXTREME on ATR(22), this from a MOVING AVERAGE on
ATR(14), so their calibration does not transfer) and NOT an owner dial
(the dials were withdrawn 2026-09-30; the 2026-10-04 route audit settled
the row on its MEASUREMENT route: resumed vs finished trends' give-back
in the name's own ATR). It stays `arbitrary` in `config/number_ledger.yaml`
until that runs. Not `exit_guard.NOISE_BAND_ATR_MULTIPLE` (1.0, FROM ENTRY).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal

from src.risk.exit_guard import _MA_REF_RE

__all__ = [
    "ALIGNMENT_GIVE_BACK_ATR_MULTIPLE",
    "AlignmentExitCheck",
    "CHART_MA_PERIODS",
    "SMA_LADDER",
    "ChartMark",
    "check_alignment_exit",
    "exponential_moving_average",
    "moving_average",
    "simple_moving_average",
    "target_reached",
    "thesis_ma_period",
    "thesis_ma_ref",
]


#: Give-back below the last chart mark, in this name's own ATR, before the
#: desk reads the move as over.
#:
#: UNSETTLED, status `arbitrary` in the ledger, with a MEASUREMENT route
#: (see the module note) — not sourced, and not an owner dial. The
#: published 2.5-3.5 ATR band is context for the order of magnitude only.
#: Deliberately NOT `exit_guard`'s 1.0, which is anchored to ENTRY.
#:
#: NO sqrt(sessions) widening is applied. That scaling belongs to the
#: entry-anchored band, where the question is how long a position has had
#: to prove itself. The reference here moves with the tape every session,
#: so widening the tolerance as well would double-count the same passage of
#: time and let a trend that ended weeks ago keep earning slack.
ALIGNMENT_GIVE_BACK_ATR_MULTIPLE: float = 3.0

#: thesis-named period -> the next longer period the pipeline computes.
#: Not a tunable: 20/50/200 are the only averages computed upstream.
SMA_LADDER: dict[int, int] = {20: 50, 50: 200}

#: Every average the desk already computes upstream, derived from the
#: ladder: the marks the CHART supplies when the thesis names none.
CHART_MA_PERIODS: tuple[int, ...] = tuple(
    sorted(set(SMA_LADDER) | set(SMA_LADDER.values()))
)

#: Durable verdict codes, written per symbol INCLUDING the unreadable
#: states, so a held position and an unreadable one stay telling apart.
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
                      more than `ALIGNMENT_GIVE_BACK_ATR_MULTIPLE` ATR
                      (from the FIRST lost mark once the target has
                      voted — see `target_vote_applied`).
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
    #: The MA period and KIND parsed out of the thesis prose when this
    #: verdict formed, and the (truncated) text they came from: the prose
    #: is model-written and unversioned, so a reword must show as a diff.
    thesis_ma_period: int | None = None
    thesis_ma_kind: str = ""
    thesis_text: str = ""
    sessions_since_mark_lost: int | None = None
    owner_reason: str | None = None
    #: True when a completed close reached the CURRENT target since it took
    #: effect, so the give-back was read from the first lost mark; never a
    #: sale by itself. `target_vote` says the value, version and date.
    target_vote_applied: bool = False
    target_vote: str = ""

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
    vals = [v for v in (_finite(c) for c in closes) if v is not None]
    if period <= 0 or len(vals) < period:
        return None
    return sum(vals[-period:]) / float(period)


def exponential_moving_average(closes: list[float], period: int) -> float | None:
    """Standard EMA: alpha = 2/(period+1), seeded with the simple mean of
    the first `period` values. A thesis naming an EMA is judged against an
    EMA, never an SMA of the same period (a different price in a trend).
    """
    vals = [v for v in (_finite(c) for c in closes) if v is not None]
    if period <= 0 or len(vals) < period:
        return None
    alpha = 2.0 / (period + 1.0)
    ema = sum(vals[:period]) / float(period)
    for v in vals[period:]:
        ema = alpha * v + (1.0 - alpha) * ema
    return ema


def moving_average(closes: list[float], period: int, kind: str) -> float | None:
    """The average of the KIND the caller names: "EMA" or "SMA"."""
    ema = (kind or "").upper() == "EMA"
    return (exponential_moving_average if ema else simple_moving_average)(closes, period)


def thesis_ma_ref(thesis_invalid_if: str | None) -> tuple[int, str] | None:
    """The average this position's OWN thesis names: (period, "SMA"|"EMA").

    Same regex `exit_guard.check_thesis_invalid_if` reads, so the alignment
    exit and the invalidation check can never disagree about which average
    a position is riding. The KIND is read from the same match, because
    "close below the EMA50" and "close below the SMA50" are different
    prices and only one of them is the thesis.
    """
    m = _MA_REF_RE.search(thesis_invalid_if or "")
    if not m:
        return None
    period = int(m.group(1) or m.group(2))
    if period not in (20, 50, 200):
        return None
    return period, ("EMA" if "ema" in m.group(0).lower() else "SMA")


def thesis_ma_period(thesis_invalid_if: str | None) -> int | None:
    """The period of `thesis_ma_ref`, or None. See `thesis_ma_ref`."""
    return (thesis_ma_ref(thesis_invalid_if) or (None,))[0]


def target_reached(
    closes: list[float], bar_dates: list, target: float | None,
    effective_date, *, is_short: bool = False,
) -> bool | None:
    """Has a COMPLETED close reached the CURRENT target since it took effect?

    A close at or beyond `target` (>= long, <= short) dated on or after
    `effective_date` (entry, or the latest applied revision — a revision
    resets the question). Close only, no touch, no tolerance. None when
    there is no target or no dates to judge it against.
    """
    t = _finite(target)
    if t is None or not bar_dates or not effective_date:
        return None
    eff = str(effective_date)[:10]
    return any(
        str(d)[:10] >= eff and (c <= t if is_short else c >= t)
        for d, c in zip(bar_dates, closes) if _finite(c) is not None
    )


def _sessions_since_mark_lost(
    series: list[float], mark: ChartMark, ma: tuple[int, str] | None, *,
    is_short: bool,
) -> int:
    """How many completed sessions price has been on the wrong side of this
    mark. Each close is compared against the average AS IT STOOD on that
    session (comparing history against TODAY's lower average inflated the
    count — the closed attempt's sixth defect); a level is compared to itself.
    """
    n = len(series)
    sessions = 0
    for i in range(n - 1, -1, -1):
        ref = mark.price if ma is None else moving_average(series[: i + 1], ma[0], ma[1])
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
    target: float | None = None,
    target_effective_date=None,
    target_version: str = "",
    bar_dates: list | None = None,
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

    `target` is the CURRENT take-profit, `target_effective_date` when it took
    effect, `target_version` which record it came from; `bar_dates` run
    parallel to `closes`. One vote, never a sale alone (`target_reached`).
    """
    a = _finite(atr)
    fast, kind = thesis_ma_ref(thesis_invalid_if) or (None, "")
    thesis_text = (thesis_invalid_if or "")[:400]
    series = [v for v in (_finite(c) for c in closes) if v is not None]
    if not series:
        return AlignmentExitCheck(
            "UNPARSEABLE", CODE_NO_CLOSES, (), None, None, None,
            "no completed closes to read the chart from",
            thesis_ma_period=fast, thesis_ma_kind=kind, thesis_text=thesis_text,
        )
    last = series[-1]

    # --- the marks the chart actually presents --------------------------
    marks: list[ChartMark] = []
    mark_periods: dict[str, tuple[int, str] | None] = {}
    lvl = _finite(broken_structural_level)
    if lvl is not None:
        m = ChartMark(lvl, "confirmed-broken structural level")
        marks.append(m)
        mark_periods[m.source] = None
    # THE CHART CAN SPEAK WITHOUT THE PROSE: a thesis-named average (of the
    # kind named) plus the next on the ladder, else the chart's own averages.
    # Prose chooses WHICH average, never WHETHER there is one.
    ma_pairs: tuple = (
        ((fast, kind), (SMA_LADDER.get(fast), "SMA")) if fast is not None
        else tuple((p, "SMA") for p in CHART_MA_PERIODS)
    )
    for period, k in ma_pairs:
        if period is None:
            continue
        v = moving_average(series, period, k)
        if v is not None:
            label = (
                f"{k}{period} (thesis rides the {kind}{fast})"
                if fast is not None
                else f"SMA{period} (the chart's own average — the thesis "
                     f"named none the desk computes)"
            )
            m = ChartMark(v, label)
            marks.append(m)
            mark_periods[m.source] = (period, k)

    if not marks:
        return AlignmentExitCheck(
            "UNPARSEABLE", CODE_NO_MARK, (), None, None, None,
            "the chart presents no mark for this position — no confirmed "
            "structural break, and too few closes for any average the desk "
            "computes — so there is nothing to read; refusing to invent one",
            thesis_ma_period=fast, thesis_ma_kind=kind, thesis_text=thesis_text,
        )
    if a is None or a <= 0:
        return AlignmentExitCheck(
            "UNPARSEABLE", CODE_NO_ATR, tuple(marks), None, None, None,
            "no ATR for this name — the tolerance that judges a give-back "
            "cannot be read off the instrument",
            thesis_ma_period=fast, thesis_ma_kind=kind, thesis_text=thesis_text,
        )

    reached = target_reached(
        series, bar_dates or [], target, target_effective_date, is_short=is_short,
    )
    vote = (
        "target vote: NOT APPLIED, no target to read (" + (
            (target_version or "no current target supplied") if _finite(target) is None
            else "no dates to judge the target against"
        ) + ") — today's rule, measured from the last lost mark"
        if reached is None else
        f"target vote: {'APPLIED' if reached else 'not applied'} — target "
        f"{_finite(target):.4f} ({target_version or 'current record'}, in force "
        f"from {str(target_effective_date)[:10]}) "
        f"{'reached' if reached else 'not reached'} by a completed close since then"
    )
    breached = [m for m in marks if (last > m.price if is_short else last < m.price)]
    if not breached:
        held = min(marks, key=lambda m: m.price) if is_short else max(
            marks, key=lambda m: m.price
        )
        return AlignmentExitCheck(
            "HOLD", CODE_HOLD, tuple(marks), None, None, None,
            f"close {last:.4f} is still holding against {held.price:.4f} "
            f"[{held.source}] — the move is not over; {vote}",
            thesis_ma_period=fast, thesis_ma_kind=kind, thesis_text=thesis_text,
            target_vote_applied=bool(reached), target_vote=vote,
        )

    # THE LAST THING HOLDING THE TREND UP: of the marks price has given up,
    # the one it gave up LAST is the lowest (highest, for a short). Once the
    # target has voted, the FIRST one given up is the reference instead.
    last_mark = (max if is_short != bool(reached) else min)(
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
        f"[{last_mark.source}], the {'first' if reached else 'last'} of "
        f"{len(marks)} chart mark(s) it was standing on, lost {sessions} "
        f"session(s) ago; the give-back tolerance is {band_atrs:.2f} ATR "
        f"({band:.4f}); {vote}"
    )
    if breach > band:
        return AlignmentExitCheck(
            "EXIT", CODE_EXIT, tuple(marks), last_mark, breach_atrs, band_atrs,
            detail, thesis_ma_period=fast, thesis_ma_kind=kind, thesis_text=thesis_text,
            sessions_since_mark_lost=sessions, target_vote_applied=bool(reached),
            target_vote=vote,
            owner_reason=(
                f"Trend alignment over: the {'first' if reached else 'last'} line "
                f"this position was standing on — {last_mark.source} at "
                f"{last_mark.price:.2f} — has been given up, and price has closed "
                f"{breach_atrs:.1f}x this name's own ATR "
                f"{'above' if is_short else 'below'} it, more than the "
                f"{band_atrs:.1f}x give-back the desk reads as the end of a "
                f"move. Selling because the move it was riding has ended on "
                f"the chart, not because price reached any target"
                + (
                    f"; the target ({_finite(target):.2f}) voted with the chart, "
                    f"so the give-back was read from the first lost mark."
                    if reached else "."
                )
            ),
        )
    return AlignmentExitCheck(
        "HOLD", CODE_HOLD, tuple(marks), last_mark, breach_atrs, band_atrs,
        f"holding — the slip through the last mark is inside the give-back "
        f"tolerance. {detail}",
        thesis_ma_period=fast, thesis_ma_kind=kind, thesis_text=thesis_text,
        sessions_since_mark_lost=sessions, target_vote_applied=bool(reached),
        target_vote=vote,
    )
