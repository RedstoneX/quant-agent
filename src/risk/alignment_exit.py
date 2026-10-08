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
past into a statistic, and is never anchored to what the desk paid. The
target is ONE VOTE (owner, 2026-10-08): once a completed close has reached
the current target since it took effect, the give-back is read from the
FIRST lost mark instead of the last — see `src.risk.target_vote`. No lost
mark still holds; one lost mark is unchanged; two or more can tip a close.

WHY THE TOLERANCE IS AN UNSETTLED LEDGER ROW, NOT A SOURCED NUMBER AND
NOT AN OWNER DIAL
-----------------------------------------------------------------
An earlier draft of this module claimed 3.0 was SOURCED to Wilder's
Volatility System and Le Beau's Chandelier Exit. That claim was
overstated, and it is withdrawn:

  1. Wilder and Le Beau measure give-back from a running EXTREME — the
     highest high since entry. This module measures it from a MOVING
     AVERAGE, which in any trend sits materially BELOW the extreme. The
     same multiple off a lower reference is a different, looser stop, so
     their calibration does not transfer.
  2. Chandelier's 3.0 is calibrated on ATR(22). This module divides by
     ATR(14). Even the unit differs.

A later draft then called 3.0 an OWNER APPETITE DIAL. That is stale too:
the owner withdrew the risk dials on 2026-09-30, and the ledger's
2026-10-04 route audit settled the row on its MEASUREMENT route — the
give-back distributions of trends that resumed and trends that ended, in
the name's own ATR, read off the tradable universe. Until that runs the
value stays `arbitrary` in `config/number_ledger.yaml`, waiting on a
measurement, not on a person. The published 2.5-3.5 ATR band is kept
only as CONTEXT for the order of magnitude. It is NOT reused from
`exit_guard.NOISE_BAND_ATR_MULTIPLE`, which is 1.0 and measures excess
over noise FROM ENTRY — a different quantity from a different anchor.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from src.risk.chart_averages import _finite, exponential_moving_average
from src.risk.chart_averages import moving_average, simple_moving_average
from src.risk.exit_guard import _MA_REF_RE
from src.risk.target_vote import reference_mark, target_vote

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
    "thesis_ma_period",
    "thesis_ma_ref",
]


#: Give-back below the last chart mark, in this name's own ATR, before the
#: desk reads the move as over.
#:
#: UNSETTLED — ledger status `arbitrary` with a MEASUREMENT route (see the
#: module note); NOT sourced and NOT an owner dial. The published 2.5-3.5
#: ATR band (Wilder 1978; Le Beau's Chandelier) measures give-back from a
#: running EXTREME on ATR(22), while this measures it from a MOVING
#: AVERAGE on ATR(14), so the citation does not transfer and is context
#: for the order of magnitude only. Tightening realises gains sooner and
#: whipsaws more often. It is deliberately NOT `exit_guard`'s 1.0, which
#: is anchored to ENTRY rather than to the chart.
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

#: Every average the desk already computes upstream, derived from the
#: ladder above rather than written out again. These are the marks the
#: CHART supplies when the thesis prose names no average the desk can
#: compute — see `check_alignment_exit`. No new number: the set is exactly
#: the periods already named by `SMA_LADDER`.
CHART_MA_PERIODS: tuple[int, ...] = tuple(
    sorted(set(SMA_LADDER) | set(SMA_LADDER.values()))
)

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
                      more than `ALIGNMENT_GIVE_BACK_ATR_MULTIPLE` ATR (the
                      FIRST lost mark once the target has voted).
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
    #: "SMA" or "EMA" — WHICH KIND of average the thesis named, recorded
    #: because the two are different prices and the thesis only ever named
    #: one of them. "" when the thesis named no average.
    thesis_ma_kind: str = ""
    #: Verbatim thesis text the period was parsed from, truncated. Pins the
    #: input so a later reword is visible as a difference.
    thesis_text: str = ""
    sessions_since_mark_lost: int | None = None
    owner_reason: str | None = None
    #: Whether the target's vote applied (`src.risk.target_vote`), and the
    #: plain-words account: value, version, effective date — or why none.
    target_vote_applied: bool = False
    target_vote: str = ""

    @property
    def exit_cleared(self) -> bool:
        return self.status == "EXIT"


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
    ref = thesis_ma_ref(thesis_invalid_if)
    return ref[0] if ref else None


def _sessions_since_mark_lost(
    series: list[float], mark: ChartMark, ma: tuple[int, str] | None, *,
    is_short: bool,
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
        if ma is None:
            ref: float | None = mark.price
        else:
            ref = moving_average(series[: i + 1], ma[0], ma[1])
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

    `target`, `target_effective_date` and `target_version` are the CURRENT
    take-profit, when it took effect and which record set it; `bar_dates`
    run parallel to `closes`. See `src.risk.target_vote`: one vote, never
    a sale alone, and a missing target is today's rule, said so.
    """
    a = _finite(atr)
    ref = thesis_ma_ref(thesis_invalid_if)
    fast = ref[0] if ref else None
    kind = ref[1] if ref else ""
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
    # THE CHART CAN SPEAK WITHOUT THE PROSE. When the thesis names an
    # average the desk computes, that average (of the KIND THE THESIS
    # NAMED) and the next longer one on the ladder are the marks — the
    # position's own stated line comes first. When the thesis names no
    # average, or names one the desk does not compute, the chart still
    # supplies its own: the simple averages the pipeline already computes
    # for every name. Coverage is therefore NOT set by how a model worded
    # its thesis; prose only chooses WHICH average, never WHETHER there is
    # one. No new number is introduced — `CHART_MA_PERIODS` is derived
    # from the same ladder.
    if fast is not None:
        ma_pairs: tuple[tuple[int | None, str], ...] = (
            (fast, kind), (SMA_LADDER.get(fast), "SMA"),
        )
    else:
        ma_pairs = tuple((p, "SMA") for p in CHART_MA_PERIODS)
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

    reached, vote = target_vote(
        series, bar_dates, target, target_effective_date, target_version,
        is_short=is_short,
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
    # the one it gave up LAST is the lowest (highest, for a short) — or,
    # once the target has voted, the one it gave up FIRST.
    last_mark = reference_mark(breached, reached, is_short=is_short)
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
