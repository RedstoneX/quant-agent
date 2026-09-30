"""The trend-over ALIGNMENT read — the desk's profit-taking exit.

WHY THIS EXISTS (owner, 2026-09-30, relayed by the orchestrator)
----------------------------------------------------------------
Asked whether reaching a computed target should sell a position, the owner
said no: "The target is just a made-up number. The chart, the ATR, support
and resistance, and you should also have an SMA in there that if it crosses
would be another sign — not just one, but there has to be alignment to say
the trend is over or take profit needs to be done." Told that waiting for
several readings to agree lags by construction and gives back more of the
top, he added: "maybe the agreement needs to be tight."

So a computed target is never an exit trigger. The exit is an ALIGNMENT of
independent readings taken LIVE off the instrument, and "tight" comes from
the SHAPE of the rule, never from a count, a weight, a lookback or a
tolerance chosen here.

THE THREE READINGS, EACH ALREADY RATIFIED ON THIS DESK
------------------------------------------------------
Nothing below introduces a number. Every quantity is one the repo already
computes and uses to move money, read on the latest COMPLETED daily close.

* STRUCTURE (support and resistance) — has the last higher low gone?
  The level is the trailing-stop module's own pivot
  (`src.risk.trailing._structural_pivot` over `_swing_lows`, its ratified
  `PIVOT_WINDOW`): the most recent confirmed low, counted only when it is a
  genuine higher low. "Gone" is the desk's one level-break standard
  (`src.risk.exit_guard.check_structural_protection`): a close beyond the
  level by `BREAK_CONFIRMATION_ATR_MULTIPLE` ATRs, CONFIRMED on the next
  session's close, with adjacency — a session with no read ends the streak
  (`exit_guard._consecutive_prior_break_count`), so a gap can never chain
  two lone closes into a confirmation. The structure reading is ALSO
  satisfied, without a margin or a close count, when the pivot SEQUENCE
  itself has turned — the latest confirmed low is below the one before
  it. The higher low is then not "about to go", it is gone, and the
  pivot's own `PIVOT_WINDOW` bars either side are its confirmation.

* VOLATILITY (the ATR) — has price given back more than the instrument's
  own volatility allows a live trend? The Chandelier exit the doctrine
  calls textbook (`docs/OUTCOME.md`): the run's extreme (highest high
  since entry for a long) less `src.risk.trailing.CHANDELIER_ATR_MULTIPLE`
  ATRs. Close at or beyond that level is the reading.

* MOVING AVERAGE (the SMA cross) — is the chart still in an uptrend by the
  technical seat's OWN definition? `config/prompts/tech_analyst.md` defines
  an established uptrend as price above a RISING MA20/MA50; the desk
  computes MA20 and MA50 in `src.data.technical.compute_indicators`. The
  reading is the close relative to MA20 and whether MA20 is rising
  (today's MA20 above yesterday's — a comparison, not a number).

THE SHAPE — every reading must agree, and the structure break must be
CONFIRMED
------------------------------------------------------------------------
The three do not resolve at the same speed. A structural break and an
ATR-relative giveback are readable the session they happen; a moving-average
cross lags by construction, because the average carries weeks of prior
closes. So the two instrument-speed readings carry the decision and the
lagging one is only a veto — "the analyst would still call this an
established uptrend, do not sell into it":

    aligned  =  structure_CONFIRMED  AND  chandelier_hit  AND  NOT uptrend_intact

CORRECTION 2026-09-30, before this ever ran on money. The first cut of this
module read `structure_broken_today` — ONE close through the break margin —
and argued that the owner's word "tight" meant tightness in time, so the
second independent reading on the same close replaced the second day. That
is the recurring bug `docs/OUTCOME.md` already names: "a same-day break
trigger that had to be corrected to a two-trading-day closing confirmation
once it was pointed out that a one-day dip which reclaims its level is a
well-known reversal pattern, not evidence of a breakdown... Treat a new
instance of this shape as the same recurring bug, not a fresh question."
The owner did not say "tight in time"; that reading was invented here. It
also discarded the ONE sourced element in this whole stack
(`TREND_CONFIRMING_CLOSES` = 2, Edwards & Magee) while keeping the
unsourced ones. The confirmation is required. Measured below, it costs 9
fires out of 237 — the argument for dropping it was not merely unsourced,
it was about nothing.

THE RUN EXTREME IS MEASURED FROM ENTRY AND NEVER RESETS
-------------------------------------------------------
`run_start_index` is the first bar on or after the position's entry, and
the run extreme is the highest high from there to the close under
judgment. That is deliberate and it matches two things it has to match:
the incumbent trail's own chandelier, which takes `max(high)` over the
bars SINCE ENTRY (`trailing.evaluate_trailing_stop`, and its contract
"`bars` are the daily bars SINCE ENTRY"), and LeBeau's published
Chandelier Exit, which hangs from the highest high of the move. If this
module reset the run on each chandelier hit, the desk would read two
different chandelier levels off the same instrument on the same day.

MEASURED, NOT ASSERTED — and the first measurement described a different
rule
------------------------------------------------------------------------
The measurement this module originally quoted (14 / 44 / 306 fires, 1.3
per symbol-year, 9.9% giveback, 25% false exits) was taken with a run
extreme that RESET on every chandelier fire. The code does not reset. So
those numbers never described this code and they are struck. RE-MEASURED
2026-09-30 against the shipped definition, on the same 19 instruments and
the same 9,519 daily bars (2024-09-30 .. 2026-09-29), as a chain of
synthetic positions — enter, hold until the shape fires, re-enter the next
session — with the incumbent ratcheting trail simulated alongside. Full
table and method in docs/INCIDENT_HISTORY.md. Nothing was tuned; the only
constants are the desk's own.

  * one close through the margin (the shape first proposed): 237 fires,
    7.1 per symbol-year, median giveback from the run high 10.8%, 56% of
    fires followed by a new high within 60 sessions;
  * the break CONFIRMED on two closes (this shape): 228 fires, 6.8 per
    symbol-year, 11.1% giveback, 55% false;
  * confirmed OR the higher-low sequence turned (this shape, with the
    lower-low fix below): 230 fires, 6.9 per symbol-year, 11.0%, 55%.

Read honestly, that table says two uncomfortable things. The rule fires
about seven times per symbol-year, not 1.3; and more than half of its
exits are followed by a new high inside a quarter. It is a far more
active, far less discriminating rule than the record it was merged on
claimed.

AND IT MOSTLY ARRIVES AFTER THE TRAIL
-------------------------------------
The desk already rests a broker stop that ratchets to the SAME two levels
this read tests — the structural pivot and the chandelier
(`trailing.evaluate_trailing_stop`). So when this read says "price has
closed at or below the chandelier", price has by construction traded
through the level the trail would have ratcheted to, if it had managed to
ratchet. Simulated over the same bars: 90% of the confirmed shape's fires
happen on or after the session the trail's own resting stop was already
taken out, a median 4 sessions earlier. The exit's real scope is the
remaining tenth — the cases where the trail was blocked by the
minimum-ratchet clamp, the noise band, or a pivot on the wrong side. That
is a real gap and this rule closes it; it is not the profit-taking engine
the first write-up implied, and nothing here should be read as saying it
is.

STRUCTURE HAS THREE STATES, AND ONLY MISSING DATA IS A FAULT
------------------------------------------------------------
`trailing._structural_pivot` returns None both when the latest confirmed
pivot is a LOWER low and when no pivot is confirmed at all. The first cut
filed both as UNREADABLE, so the exit became impossible at exactly the
moment a confirmed lower low says the uptrend is over, and the seat was
told the data had failed when it had not. See `structure_state` in
`read_trend_alignment`: a turned sequence is a structure break carrying
its own two-sided pivot confirmation, no pivot yet is a chart state that
reads NOT broken, and only a missing close, ATR or MA20 is a fault.

WHAT THIS IS NOT
----------------
Not a target: no price level chosen in advance appears anywhere. Not a
fixed-gain trim: nothing here reads the position's P&L. Not a swing-pivot
Dow read at the moment of a touch (three earlier cuts died on that). And
not a discretionary narrative: every reading is a measured fact the seat can
be shown and a later reader can recompute from the bars.
"""

from __future__ import annotations

from dataclasses import dataclass

from src.risk.exit_guard import (
    ALIGNMENT_PHRASES,
    BREAK_CONFIRMATION_ATR_MULTIPLE,
    TREND_CONFIRMING_CLOSES,
    _consecutive_prior_break_count,
    reason_cites_alignment,
)
from src.risk.trailing import (
    CHANDELIER_ATR_MULTIPLE,
    _structural_pivot,
    _swing_highs,
    _swing_lows,
)

__all__ = [
    "TrendAlignmentRead",
    "read_trend_alignment",
    "ALIGNMENT_ALIGNED",
    "ALIGNMENT_NOT_ALIGNED",
    "ALIGNMENT_UNREADABLE",
    "ALIGNMENT_PHRASES",
    "reason_cites_alignment",
]

#: Every reading agrees the trend is over — the desk takes profit / exits.
ALIGNMENT_ALIGNED = "TREND_ALIGNMENT_ALIGNED"
#: At least one reading says the trend is not over — no exit on this basis.
ALIGNMENT_NOT_ALIGNED = "TREND_ALIGNMENT_NOT_ALIGNED"
#: A reading could not be taken (no ATR, no MA, no bars) — a data fault,
#: filed, never read as "aligned" and never read as "intact".
ALIGNMENT_UNREADABLE = "TREND_ALIGNMENT_UNREADABLE"

def _finite(value: object) -> float | None:
    try:
        f = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if f != f or f in (float("inf"), float("-inf")):
        return None
    return f


@dataclass(frozen=True)
class TrendAlignmentRead:
    """One symbol's three readings and their alignment, on one completed close.

    `aligned` is the one field a caller acts on (`structure_confirmed` is
    informational — the two-close confirmation is filed, not required; see
    the module note). `code` names the outcome;
    `reason` is the plain-language owner-facing sentence. Every input that
    went into the read is carried so the row filed for it can be recomputed.
    """

    symbol: str
    code: str
    aligned: bool
    reason: str = ""
    close_price: float | None = None
    atr: float | None = None
    # structure
    structure_level: float | None = None
    structure_broken_today: bool | None = None
    structure_prior_streak: int = 0
    structure_confirmed: bool = False
    #: 'higher_low' / 'lower_high' — the trail's pivot gives a level to
    #: break; 'sequence_turned' — the pivot sequence itself has turned, so
    #: the level is already gone; 'no_pivot' — no confirmed pivot yet, so
    #: structure has nothing to say. None of the three is a data fault.
    structure_state: str = "no_pivot"
    # volatility
    run_extreme: float | None = None
    chandelier_level: float | None = None
    chandelier_hit: bool | None = None
    # moving average
    ma_20: float | None = None
    ma_20_prior: float | None = None
    ma_50: float | None = None
    uptrend_intact: bool | None = None
    unreadable: tuple[str, ...] = ()


def read_trend_alignment(
    *,
    symbol: str,
    is_short: bool,
    bars: list,
    run_start_index: int,
    atr: float | None,
    ma_20: float | None,
    ma_20_prior: float | None,
    ma_50: float | None,
    prior_break_records: list | None,
    prior_session_dates: list | None,
) -> TrendAlignmentRead:
    """Take the three readings on the LAST bar of `bars` and align them.

    `bars` are COMPLETED daily bars, oldest first, the last one being the
    close under judgment; `run_start_index` is the index of the first bar
    on or after the position's entry (the run's extreme is measured from
    there). `atr`, `ma_20`, `ma_50` are `compute_indicators` on the same
    bars; `ma_20_prior` is MA20 on the bars up to the previous session.
    `prior_break_records` / `prior_session_dates` are the structure flags
    this read filed on earlier sessions and the position's own completed
    sessions before today, most recent first — exactly the inputs
    `check_structural_protection` takes for the stop side.

    Pure. Never raises on missing inputs: each reading that cannot be taken
    is named in `unreadable`, and an unreadable read is neither aligned nor
    a reason to hold — it is a data fault for the caller to file.
    """
    sym = str(symbol or "").strip().upper()
    unreadable: list[str] = []
    series = list(bars or [])
    close = _finite(getattr(series[-1], "close", None)) if series else None
    atr_f = _finite(atr)
    if close is None or close <= 0:
        unreadable.append("close")
    if atr_f is None or atr_f <= 0:
        unreadable.append("atr")

    # --- STRUCTURE: the trail's own last higher low, broken on the desk's
    # standard (margin + two consecutive closes with adjacency).
    #
    # THREE STATES, not two (2026-09-30 correction). `_structural_pivot`
    # returns None in two very different situations and the first cut of
    # this module filed both as a DATA FAULT:
    #
    #   * the latest confirmed pivot is a LOWER low — the higher-low
    #     sequence the trail trails against is already over. That is a
    #     normal chart state and the clearest structural evidence an
    #     uptrend has ended, not a fault. Filing it as unreadable made the
    #     exit IMPOSSIBLE at exactly the moment structure says to take it,
    #     and told the seat the data had failed when it had not.
    #     `trailing._structural_pivot`'s own docstring says it: "the
    #     sequence is making lower lows, so structure has not offered a
    #     trail at all". For the TRAIL that means fall back to the
    #     chandelier; for THIS read it means the structure reading is
    #     satisfied — the last higher low is gone.
    #     It needs no break margin and no second close: the pivot is
    #     already confirmed by `PIVOT_WINDOW` bars on BOTH sides (the
    #     desk's ratified confirmation for a swing point), and it is below
    #     its predecessor. No number is introduced.
    #
    #   * no confirmed pivot exists at all (too few bars, or a straight run
    #     with no pullback — META, 2026-09). Structure has nothing to read.
    #     Also not a data fault: the bars are fine. It reads NOT broken, so
    #     no exit follows, and the reason says so in those words.
    #
    # Only a missing close/ATR/MA is a data fault.
    level = None
    broken_today: bool | None = None
    prior_streak = 0
    confirmed = False
    structure_state = "no_pivot"
    pivots: list = []
    if series:
        pivots = _swing_highs(series) if is_short else _swing_lows(series)
        level = _structural_pivot(pivots, is_short=is_short)
    if level is not None:
        structure_state = "higher_low" if not is_short else "lower_high"
    elif len(pivots) >= 2 and (
        (pivots[-1] > pivots[-2]) if is_short else (pivots[-1] < pivots[-2])
    ):
        # The sequence itself has turned: a confirmed lower low (mirror: a
        # confirmed higher high for a short). Structure is BROKEN, and the
        # pivot's own two-sided confirmation IS the confirmation.
        structure_state = "sequence_turned"
        level = pivots[-1]
        broken_today = True
        confirmed = True
    if structure_state == "no_pivot":
        broken_today = False
    elif structure_state in ("higher_low", "lower_high") and (
        close is not None and atr_f is not None and atr_f > 0
    ):
        margin = BREAK_CONFIRMATION_ATR_MULTIPLE * atr_f
        broken_today = (close >= level + margin) if is_short else (close <= level - margin)

        def _cleared(record) -> bool:
            rc = _finite(record.get("close")) if isinstance(record, dict) else None
            if rc is None:
                return False
            return (rc >= level + margin) if is_short else (rc <= level - margin)

        prior_streak = _consecutive_prior_break_count(
            prior_break_records, prior_session_dates, clears=_cleared,
        )
        confirmed = bool(broken_today) and (prior_streak + 1 >= TREND_CONFIRMING_CLOSES)

    # --- VOLATILITY: the chandelier under the run's extreme.
    extreme = None
    chandelier = None
    hit: bool | None = None
    if series and atr_f is not None and atr_f > 0:
        start = max(0, min(int(run_start_index or 0), len(series) - 1))
        if is_short:
            lows = [_finite(getattr(b, "low", None)) for b in series[start:]]
            lows = [x for x in lows if x is not None]
            if lows:
                extreme = min(lows)
                chandelier = extreme + CHANDELIER_ATR_MULTIPLE * atr_f
                hit = close is not None and close >= chandelier
        else:
            highs = [_finite(getattr(b, "high", None)) for b in series[start:]]
            highs = [x for x in highs if x is not None]
            if highs:
                extreme = max(highs)
                chandelier = extreme - CHANDELIER_ATR_MULTIPLE * atr_f
                hit = close is not None and close <= chandelier
    if hit is None:
        unreadable.append("chandelier")

    # --- MOVING AVERAGE: the analyst's "established uptrend" — price above
    # a RISING MA20 (mirror: below a falling MA20 for a short).
    m20 = _finite(ma_20)
    m20p = _finite(ma_20_prior)
    m50 = _finite(ma_50)
    intact: bool | None = None
    if m20 is not None and m20p is not None and close is not None:
        if is_short:
            intact = close < m20 and m20 < m20p
        else:
            intact = close > m20 and m20 > m20p
    else:
        unreadable.append("ma20")

    base = dict(
        symbol=sym, close_price=close, atr=atr_f,
        structure_level=level, structure_broken_today=broken_today,
        structure_prior_streak=prior_streak, structure_confirmed=confirmed,
        structure_state=structure_state,
        run_extreme=extreme, chandelier_level=chandelier, chandelier_hit=hit,
        ma_20=m20, ma_20_prior=m20p, ma_50=m50, uptrend_intact=intact,
        unreadable=tuple(unreadable),
    )
    if unreadable:
        return TrendAlignmentRead(
            code=ALIGNMENT_UNREADABLE, aligned=False,
            reason=(
                f"trend-alignment read incomplete — could not take: "
                f"{', '.join(unreadable)}. No exit is decided on missing "
                f"data; the broker-resident stop still protects the position."
            ),
            **base,
        )

    # The structure break must be CONFIRMED — the desk's standing rule
    # (docs/OUTCOME.md: a same-day break trigger was corrected to a
    # two-trading-day closing confirmation, and a new instance of that
    # shape is the SAME recurring bug, not a fresh question). A confirmed
    # turn of the pivot sequence carries its own two-sided confirmation and
    # sets `confirmed` directly. The MA can only veto.
    aligned = bool(confirmed and hit and not intact)
    side_word = "lower high" if is_short else "higher low"
    trend_word = "downtrend" if is_short else "uptrend"
    if aligned:
        if structure_state == "sequence_turned":
            struct_clause = (
                f"its {side_word} sequence has turned — the latest confirmed "
                f"pivot (${level:,.2f}) is no longer a {side_word}, so the "
                f"level the trail defends is gone"
            )
        else:
            struct_clause = (
                f"its last {side_word} at ${level:,.2f} has broken on "
                f"{TREND_CONFIRMING_CLOSES} consecutive closes "
                f"(latest ${close:,.2f})"
            )
        reason = (
            f"the trend is over on every reading the desk takes: "
            f"{struct_clause}, price has given back "
            f"more than {CHANDELIER_ATR_MULTIPLE:g} ATRs from the run's "
            f"extreme (${extreme:,.2f}, chandelier ${chandelier:,.2f}), and "
            f"the close is no longer above a rising MA20 (${m20:,.2f}). The "
            f"desk takes profit in full."
        )
    else:
        parts = []
        if structure_state == "no_pivot":
            parts.append(
                f"the run has not yet put in a confirmed pullback low, so "
                f"there is no {side_word} to break"
            )
        elif not broken_today:
            parts.append(f"the last {side_word} at ${level:,.2f} still holds")
        elif not confirmed:
            parts.append(
                f"the close has broken the last {side_word} at "
                f"${level:,.2f} once, but the desk requires "
                f"{TREND_CONFIRMING_CLOSES} consecutive closes to call a "
                f"level broken"
            )
        if not hit:
            parts.append(
                f"price is inside {CHANDELIER_ATR_MULTIPLE:g} ATRs of the run's "
                f"extreme (${extreme:,.2f})"
            )
        if intact:
            parts.append(
                f"the close is still above a rising MA20 (${m20:,.2f}), which "
                f"the desk's own analyst calls an established {trend_word}"
            )
        reason = "the trend is not over on the desk's readings: " + "; ".join(parts) + "."
    return TrendAlignmentRead(
        code=ALIGNMENT_ALIGNED if aligned else ALIGNMENT_NOT_ALIGNED,
        aligned=aligned, reason=reason, **base,
    )
