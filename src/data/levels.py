"""Deterministic support/resistance detection from completed daily bars.

Why this exists
---------------
Until 2026-08-27 the Tech Analyst was shown the last **20** daily bars and asked
to report "the two or three key levels". Twenty bars is one month: a
consolidation from six weeks ago, a swing high from last spring, a level price
has respected four times since — all invisible. The only things the analyst
could honestly cite were the moving averages and the 20-day high/low, so the
structural levels the whole exit system depends on were effectively absent, and
`PortfolioConstructor` invented stops and targets to fill the gap.

The history was never the problem: the pipeline already fetched hundreds of
bars to compute MA200, then showed the analyst twenty of them.

Finding where price repeatedly stopped is arithmetic, not judgment, so it
belongs here rather than in a language model. Computing five years of levels
costs single-digit milliseconds and zero tokens, and — unlike asking a model to
eyeball a wall of numbers — the same chart yields the same levels every time,
which is what makes the behaviour testable and back-testable.

The model still does the part it is good at: deciding which of these levels
matters right now, and what kind of setup the chart is showing.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, asdict

import numpy as np

from src.models import OHLCV
from src.risk.constants import is_trend_trade

# A pivot is a bar whose high (or low) is the most extreme within this many
# bars either side. 5 keeps genuine swing structure while ignoring single-bar
# wiggles; smaller values produce noise, larger ones miss real turning points.
PIVOT_WINDOW = 5

# Two pivots within this percentage of each other are the same level. Price
# does not respect a number to the cent — it respects a zone.
CLUSTER_TOLERANCE_PCT = 1.0

# A level touched once is a coincidence, not structure.
MIN_TOUCHES = 2

# Levels further than this from the current price are history, not actionable
# structure. Filters out artefacts like a pre-IPO/SPAC period spent pinned at
# $10 while the stock now trades at $42 — technically hundreds of "touches",
# entirely irrelevant to today's decision.
MAX_DISTANCE_PCT = 40.0

# No RECENCY_HALFLIFE_SESSIONS here. Until 2026-09-02 this was 252.0 (~1
# trading year), decaying each touch's contribution to `strength` by
# `0.5 ** (age_sessions / 252)`. It was picked for being round, never
# measured, and is gone rather than slowed down — see the strength
# computation in `find_structural_levels` for the measurement that replaced
# it and docs/RESEARCH_FINDINGS.md §7 for the full caveats.

# How many levels to report per side. Enough to describe the structure, few
# enough to stay readable in a prompt.
MAX_LEVELS_PER_SIDE = 6

# Bad-print detection. A bar is rejected only when its high exceeds, or its low
# falls below, the median of its immediate neighbours by this factor. 5x is
# deliberately permissive: real markets gap, halt and limit-move, and throwing
# away genuine volatility is worse than tolerating a rare bad print.
_CLEAN_WINDOW = 10
_CLEAN_FACTOR = 5.0


@dataclass(frozen=True)
class Level:
    """One structural price level."""

    price: float
    kind: str  # "support" | "resistance"
    touches: int  # pivots clustered into this level
    last_touch_sessions_ago: int  # informational only — not a strength input, see below
    strength: float  # touch count, distance-discounted; higher = more significant

    def as_dict(self) -> dict:
        return asdict(self)


def _clean_bars(bars: list[OHLCV]) -> list[OHLCV]:
    """Drop bars that cannot be true, and single-bar print errors.

    Vendor feeds produce bad prints: a high 10x the surrounding range, a zero
    low, an inverted bar. A single bad print creates a phantom pivot, and a
    phantom pivot becomes a phantom level that a stop then gets placed against.

    The comparison is deliberately **local**. An earlier version compared every
    bar against the median of the whole series, which discarded most of the
    real history of any stock that trended hard — OKLO ran from $10 to $114, so
    a global median rejected its entire recent range as "outliers". A stock
    that multiplied is not corrupt data; a bar five times its immediate
    neighbours is. Only egregious deviations are removed, so genuine gaps and
    limit moves survive.
    """
    usable = [
        b for b in bars
        if b.high > 0 and b.low > 0 and b.close > 0 and b.high >= b.low
    ]
    n = len(usable)
    if n < _CLEAN_WINDOW * 2 + 1:
        return usable

    highs = np.array([b.high for b in usable], dtype=float)
    lows = np.array([b.low for b in usable], dtype=float)

    keep: list[OHLCV] = []
    for i, bar in enumerate(usable):
        lo = max(0, i - _CLEAN_WINDOW)
        hi = min(n, i + _CLEAN_WINDOW + 1)
        # Neighbourhood excluding the bar under test — a bad print must not be
        # allowed to widen the band that is supposed to catch it.
        neighbour_highs = np.concatenate([highs[lo:i], highs[i + 1:hi]])
        neighbour_lows = np.concatenate([lows[lo:i], lows[i + 1:hi]])
        if neighbour_highs.size == 0 or neighbour_lows.size == 0:
            keep.append(bar)
            continue
        local_high = float(np.median(neighbour_highs))
        local_low = float(np.median(neighbour_lows))
        if local_high <= 0 or local_low <= 0:
            keep.append(bar)
            continue
        if bar.high > local_high * _CLEAN_FACTOR:
            continue
        if bar.low < local_low / _CLEAN_FACTOR:
            continue
        keep.append(bar)
    return keep


def _find_pivots(bars: list[OHLCV], window: int) -> list[tuple[int, float, str]]:
    """Locate swing highs and lows. Returns (index, price, "R"|"S")."""
    n = len(bars)
    if n < window * 2 + 1:
        return []
    highs = np.array([b.high for b in bars], dtype=float)
    lows = np.array([b.low for b in bars], dtype=float)

    pivots: list[tuple[int, float, str]] = []
    for i in range(window, n - window):
        lo, hi = i - window, i + window + 1
        if highs[i] >= highs[lo:hi].max():
            pivots.append((i, float(highs[i]), "R"))
        if lows[i] <= lows[lo:hi].min():
            pivots.append((i, float(lows[i]), "S"))
    return pivots


def _cluster(
    pivots: list[tuple[int, float, str]], tolerance_pct: float
) -> list[list[tuple[int, float, str]]]:
    """Group pivots that sit within `tolerance_pct` of each other into zones."""
    if not pivots:
        return []
    ordered = sorted(pivots, key=lambda p: p[1])
    clusters: list[list[tuple[int, float, str]]] = []
    current = [ordered[0]]
    for pivot in ordered[1:]:
        anchor = current[0][1]
        if anchor > 0 and abs(pivot[1] - anchor) / anchor * 100.0 <= tolerance_pct:
            current.append(pivot)
        else:
            clusters.append(current)
            current = [pivot]
    clusters.append(current)
    return clusters


def find_structural_levels(
    bars: list[OHLCV],
    *,
    pivot_window: int = PIVOT_WINDOW,
    tolerance_pct: float = CLUSTER_TOLERANCE_PCT,
    min_touches: int = MIN_TOUCHES,
    max_distance_pct: float = MAX_DISTANCE_PCT,
    max_per_side: int = MAX_LEVELS_PER_SIDE,
) -> tuple[list[Level], list[Level]]:
    """Return ``(support_levels, resistance_levels)``, most significant first.

    Support is below the last close, resistance above it — classified by where
    the level sits *now*, not by whether the pivots forming it were highs or
    lows. A ceiling that price has broken through becomes a floor, and treating
    it as resistance because it was once a swing high would be wrong.

    Returns two empty lists when there is not enough clean history to say
    anything. Callers must treat that as "no structure identified" and decline
    the trade, never as "no structure exists".
    """
    clean = _clean_bars(bars)
    if len(clean) < pivot_window * 2 + 1:
        return [], []

    last_close = clean[-1].close
    if last_close <= 0:
        return [], []

    last_index = len(clean) - 1
    pivots = _find_pivots(clean, pivot_window)

    supports: list[Level] = []
    resistances: list[Level] = []

    for cluster in _cluster(pivots, tolerance_pct):
        if len(cluster) < min_touches:
            continue

        price = float(np.mean([p[1] for p in cluster]))
        if price <= 0:
            continue

        distance_pct = abs(price - last_close) / last_close * 100.0
        if distance_pct > max_distance_pct:
            continue

        newest_index = max(p[0] for p in cluster)
        sessions_ago = last_index - newest_index

        # Strength is touch count, discounted by distance — no age term.
        # A recency half-life stood here until 2026-09-02, weighting each
        # touch down by `0.5 ** (age_sessions / 252)` so old touches barely
        # counted. It is gone because it was checked, not because it was
        # suspected: `src/data/level_quality.py` fit a decay curve directly
        # against our own bars (7,218 daily touch episodes, 101 symbols, 5
        # years) and found no age effect at all — likelihood ratio 0.00
        # against a constant-probability null, on both an age-of-level and a
        # time-since-last-touch definition (docs/RESEARCH_FINDINGS.md §7). A
        # level defended three years ago predicts a bounce exactly as well as
        # one defended last week, in the data we actually have.
        #
        # The same measurement DOES support touches: pooled bounce
        # probability rises from 0.516 (first touch) to 0.644 (5+ prior
        # touches) on real price series, against a flat ~0.48-0.51 on
        # shuffled controls (slope +0.0249 real vs +0.0049 shuffled) — a
        # real, reproduced effect. Treat it as promising, not settled: two
        # settings were changed after an initial run found nothing, and the
        # shuffle control rules out the return distribution as an
        # explanation but does not isolate levels from ordinary volatility
        # clustering (§7 states both caveats plainly). Touch count is the
        # honest way to use a promising-not-settled finding without
        # overclaiming it.
        #
        # The measured probability CURVE itself is deliberately not imported
        # as scoring weights, for two reasons visible in the numbers above:
        # it is non-monotonic at low touch counts (0.507 at one prior touch,
        # BELOW 0.516 at zero), and it pools every level past 5 touches into
        # one number for posterior-sample-size reasons, which would score a
        # 5-touch level and a 20-touch level identically here. Checked
        # directly on the desk's 101-symbol universe (2026-09-02): swapping
        # in that curve moves MORE of the top-6 selection than dropping decay
        # did (33.9% of side-slots vs 27.1%, both measured the same way), so
        # it is not a free upgrade sitting next to the simpler option — it is
        # a different and less defensible ranking. Distance is untouched:
        # nothing here measured it, so nothing here changes it.
        strength = float(len(cluster)) / (1.0 + distance_pct / 10.0)

        level = Level(
            price=round(price, 2),
            kind="support" if price < last_close else "resistance",
            touches=len(cluster),
            last_touch_sessions_ago=int(sessions_ago),
            strength=round(strength, 4),
        )
        (supports if level.kind == "support" else resistances).append(level)

    supports.sort(key=lambda lv: -lv.strength)
    resistances.sort(key=lambda lv: -lv.strength)
    return supports[:max_per_side], resistances[:max_per_side]


def format_levels_block(
    supports: list[Level], resistances: list[Level], last_close: float
) -> str:
    """Render levels for the Tech Analyst prompt.

    Resistance descends toward the price and support descends away from it, so
    the block reads top-to-bottom like a chart's vertical axis.
    """
    if not supports and not resistances:
        return (
            "Structural levels: NONE IDENTIFIED — insufficient clean price "
            "history. Do not invent levels; rate this symbol neutral."
        )

    def line(lv: Level) -> str:
        gap = (lv.price - last_close) / last_close * 100.0
        return (
            f"    ${lv.price:,.2f} ({gap:+.1f}%) · {lv.touches} touches · "
            f"last {lv.last_touch_sessions_ago}d ago"
        )

    out = ["Structural levels (computed from the full price history):", "  Resistance (nearest last):"]
    if resistances:
        out.extend(line(lv) for lv in sorted(resistances, key=lambda x: -x.price))
    else:
        out.append("    none within range")
    out.append(f"  >>> last close ${last_close:,.2f} <<<")
    out.append("  Support (nearest first):")
    if supports:
        out.extend(line(lv) for lv in sorted(supports, key=lambda x: -x.price))
    else:
        out.append("    none within range")
    return "\n".join(out)


# ===========================================================================
# Deriving the TARGET from structure
# ===========================================================================
#
# Why this exists (2026-09-01)
# ----------------------------
# Reward:risk is `(target - entry) / (entry - stop)`. Since 2026-08-27 the
# STOP has been derived in code: structure places it, and
# `min_stop_atr_multiple` pushes it out when structure put it inside ordinary
# volatility. The TARGET was still `TechAnalysisResult.reference_target` — a
# number a language model wrote down. So the gate divided a measured quantity
# by an opinion. Those two are not commensurable, and the failure is
# systematic rather than random: a correctly-sized wide stop plus a modestly
# guessed target fails a 1.5 floor as a matter of arithmetic, whatever the
# trade is actually worth.
#
# Evidence, morning run of 2026-09-01 (`run-64290730`): 38 actionable
# signals, 30 of them (79%) under the floor before any judgement was applied.
# SLB `strong_buy`/`high` scored 1.28 on a 7.7% stop; AGX `sell`/`high`
# scored 0.84. Zero trades were placed.
#
# The floor is not the defect — its numerator is. So the numerator is
# computed here, from the same bars the stop already comes from, and the
# model's guess is demoted from arithmetic input to evidence.
#
# The rule
# --------
# Find the nearest structural level in the trade's direction that is more
# than `min_target_atr_multiple` ATRs away (a "target" inside one ordinary
# session's range is not a destination — price is already there). Then:
#
#   level exists, within reach  ->  the level IS the target. Price has to get
#                                   through the first ceiling before any
#                                   further one, and reaching past it to a
#                                   further level to make the ratio work is
#                                   exactly the invention being removed.
#
#   level exists, beyond reach  ->  nothing structural stands between entry
#                                   and as far as this symbol travels in the
#                                   intended hold, so structure does not
#                                   bound this trade. Target = measured move.
#
#   no level in the direction   ->  nothing overhead is expected to stop this
#                                   trade. Target = measured move.
#                                   (2026-09-11, funnel item 6: this used to
#                                   REFUSE unless `setup_type` separately said
#                                   "breakout". The other reading of an empty
#                                   direction — a chart nothing can be read
#                                   from — is already caught one branch
#                                   earlier as REFUSAL_NO_STRUCTURE, so the
#                                   label was adding nothing but refusals.
#                                   `risk.constants.is_trend_trade` now owns
#                                   this condition, shared with the
#                                   reward:risk exemption.)
#
# "Reach" and the measured move are the same estimate of travel:
# `ATR * sqrt(sessions) * multiple`. Square-root scaling, not linear: daily
# ranges accumulate as a random walk, and `ATR * N` overstates an N-session
# excursion by roughly sqrt(N). `expected_horizon_sessions` is the analyst's
# own estimate of time-to-resolution, already pinned at entry for exactly
# this kind of use.
#
# Note the deliberate asymmetry between the reach multiple and the projection
# multiple. Reach answers "could price plausibly get there at all?", so it is
# loose — a trade that works is by definition an above-typical move. The
# projection answers "how far do I claim it goes when no level says
# otherwise?", so it is the typical excursion and nothing more. The two are
# separately configurable because they are answering different questions.
#
# Everything fails closed. No levels, no level in the direction, no ATR, no
# horizon -> no target and a NAMED reason. There is deliberately no default
# and no fallback: a manufactured target is the defect being removed, and a
# manufactured target with better provenance is still manufactured.
#
# Interaction with `min_stop_atr_multiple` is arithmetic and worth stating
# outright, because it is the binding constraint on the measured-move branch.
# A stop at `k` ATRs and a target at `p * ATR * sqrt(H)` clear a floor `f`
# only when `sqrt(H) >= f * k / p`. At today's settings (p = 1.0, f = 1.5,
# and k = 1.5 scaled by setup: 1.35 for a range, 1.5 for a breakout) that
# is H >= ~6 sessions on the bare base, ~5 for a range setup and ~6 for a
# breakout. A stated horizon shorter than that cannot clear the floor
# however the trade is judged — a legitimate refusal about the trade's
# geometry, reported distinctly from "the model guessed badly".
#
# THIS IS THE ARITHMETIC THAT WAS CLOSING THE FUNNEL. Until 2026-09-04 the
# base `k` was 3.0 (range 3.45, breakout 2.55), which put the same thresholds
# at H >= ~21 / ~27 / ~15 sessions. This desk has never stated a 27-session
# horizon, so the range branch — the majority setup — could not clear the
# floor for ANY real signal, and measured against the record it did not: 0 of
# 222. The stop floor was re-derived from real Maximum Adverse Excursion data
# (see `risk.min_stop_atr_multiple` in config/settings.yaml); these session
# counts fall out of that change, they were not tuned to a target.
#
# The structural-level branch is looser, because the level does not have to
# be a full projection away: it needs `W >= f*k*ATR` to clear the floor and
# `W <= 1.5*ATR*sqrt(H)` to be reachable, so H >= (f*k/1.5)^2 — about 2
# sessions for a range setup (it was ~12).

#: A target closer than this many ATRs is not a destination. Price is
#: already there and the "reward" is one ordinary session's noise.
MIN_TARGET_ATR_MULTIPLE = 1.0

#: Measured move, in sqrt(session)-scaled ATRs, claimed when no level stands
#: in the way. 1.0 = the typical excursion over the stated horizon, and
#: nothing more.
BREAKOUT_PROJECTION_ATR_MULTIPLE = 1.0

#: How far price can plausibly get within the horizon, in the same units.
#: Deliberately looser than the projection above — see the note on asymmetry.
MAX_REACH_ATR_MULTIPLE = 1.5

#: Ceiling on `expected_horizon_sessions` before it enters the sqrt() travel
#: estimate. An analyst claiming a 250-session horizon would otherwise
#: licence a target ~16 ATRs out; this also absorbs a nonsense value.
MAX_HORIZON_SESSIONS = 60

#: Refusal codes. Each names a DIFFERENT thing being wrong, because "no
#: trade" without a reason is what let the original defect survive unnoticed.
#:
#: A REFUSAL is a judgement about the trade: real inputs were measured and
#: the trade's geometry does not work. It belongs in the desk's "why didn't
#: we trade" statistics.
REFUSAL_NO_HORIZON = "no_expected_horizon"
REFUSAL_NO_STRUCTURE = "no_structural_levels"
REFUSAL_NO_LEVEL_IN_DIRECTION = "no_level_in_direction"
REFUSAL_PROJECTION_IMPLAUSIBLE = "projection_implausible"

#: DATA FAULT codes (2026-09-12). These are NOT trade judgements. A listed
#: instrument always has a price, always has volatility and always has
#: bars; when this desk holds none of them, the desk's own data acquisition
#: or computation failed. Until 2026-09-12 these were REFUSAL_* codes and
#: went into the record as "the trade was rejected", so a dead feed and a
#: trade that failed its rules were the same class of outcome and nobody
#: could count either. They are now carried on `TargetDerivation.fault`
#: (never `.refusal`), recorded as `data_fault` rather than
#: `constructor_dropped`, and paged to the owner through `send_owner_alert`
#: (`src/pipeline_stages.py::_alert_unmeasurable_symbols`).
#:
#: The safety posture is unchanged: a symbol the desk cannot measure is
#: NOT traded. Only its classification, record and alerting changed.
FAULT_NO_ENTRY = "entry_price_missing"
FAULT_NO_VOLATILITY = "volatility_reading_missing"
FAULT_NO_STRUCTURE = "price_history_unusable"
#: Raised by the constructor, not here: the desk holds NO technical analysis
#: for a symbol it was asked to size. Every input below is absent at once,
#: so naming the first one ("no ATR") would misdescribe it.
FAULT_NO_ANALYSIS = "analysis_missing"

#: Kept for historical logs and the census: the two codes that used to name
#: the entry and volatility faults as refusals. Never produced any more.
REFUSAL_NO_ENTRY = "no_entry_price"
REFUSAL_NO_VOLATILITY = "no_volatility_reading"

#: Structural-level COVERAGE — what the bar history behind an empty
#: `computed_levels` list actually was. Same idea as
#: `src/data/macro.py::SeriesFreshness`: a separate axis from the reading
#: itself, so an absent reading is never passed off as a real one and a real
#: one is never mistaken for an outage. `find_structural_levels` returns the
#: same `[]` for "the feed gave us four bars" and for "three hundred clean
#: bars with no repeated turning point within reach", and the derivation
#: cannot tell which without this. Recorded by the tech analyst, which has
#: the bars, onto `TechAnalysisResult.levels_coverage`.
COVERAGE_MEASURED = "measured"                    # scan ran; an empty result is about the chart
COVERAGE_NO_BARS = "no_bars"                      # the feed returned nothing
COVERAGE_INSUFFICIENT_HISTORY = "insufficient_history"  # fewer clean bars than the scan's own minimum
COVERAGE_UNUSABLE_BARS = "unusable_bars"          # enough bars arrived; cleaning left too few
COVERAGE_UNKNOWN = "unknown"                      # not recorded (older row, hand-built object)

#: The coverage states under which an empty level list is a DATA fault. The
#: honest reading of `unknown` is "cannot claim the chart was measured", so
#: it is classified with the faults: fail-closed for the trade either way,
#: and the alert names the coverage so an `unknown` that recurs is visible.
_FAULT_COVERAGE = frozenset({
    COVERAGE_NO_BARS, COVERAGE_INSUFFICIENT_HISTORY, COVERAGE_UNUSABLE_BARS,
    COVERAGE_UNKNOWN,
})


def structure_coverage(
    bars: Sequence[OHLCV] | None, *, pivot_window: int = PIVOT_WINDOW,
) -> str:
    """Which COVERAGE_* state this bar history is in, read from the bars.

    The minimum is `find_structural_levels`'s own precondition
    (`pivot_window * 2 + 1` clean bars — the smallest window in which a
    single pivot can exist), not a threshold chosen here. Below it the scan
    cannot run; at or above it the scan runs and whatever it finds,
    including nothing, is a measurement of the chart.
    """
    if not bars:
        return COVERAGE_NO_BARS
    minimum = pivot_window * 2 + 1
    if len(bars) < minimum:
        return COVERAGE_INSUFFICIENT_HISTORY
    if len(_clean_bars(list(bars))) < minimum:
        return COVERAGE_UNUSABLE_BARS
    return COVERAGE_MEASURED


@dataclass(frozen=True)
class TargetDerivation:
    """Outcome of deriving a target. ``price is None`` means NO TRADE.

    Exactly one of two things explains a ``None`` price, and they are
    carried on DIFFERENT fields so they can never be confused in the record:

    * `refusal` — a trade judgement (a REFUSAL_* code): real inputs were
      measured and the trade's geometry does not work.
    * `fault` — a data fault (a FAULT_* code): the desk could not obtain
      the input a real market always has. The symbol is UNMEASURABLE, not
      judged.

    `detail` is the one line a human (or a downstream model reading the
    order's reasoning) needs in either case.
    """

    price: float | None
    basis: str = ""          # "structural_level" | "measured_move" | "" (no trade)
    refusal: str = ""        # one of the REFUSAL_* codes; "" otherwise
    detail: str = ""
    level_used: float | None = None
    horizon_reach: float | None = None      # ATR * sqrt(H) * max_reach_atr_multiple
    model_target: float | None = None       # the LLM's guess, kept as evidence
    divergence_pct: float | None = None     # computed vs. the model's guess
    fault: str = ""          # one of the FAULT_* codes; "" otherwise

    @property
    def refused(self) -> bool:
        """No trade — for EITHER reason. Callers that need to tell the two
        apart read `unmeasurable` / `fault` and `refusal`."""
        return self.price is None

    @property
    def unmeasurable(self) -> bool:
        return bool(self.fault)

    def as_dict(self) -> dict:
        return asdict(self)


def _refused(code: str, detail: str, model_target: float | None) -> TargetDerivation:
    return TargetDerivation(
        price=None, refusal=code, detail=detail, model_target=model_target,
    )


def _faulted(code: str, detail: str, model_target: float | None) -> TargetDerivation:
    return TargetDerivation(
        price=None, fault=code, detail=detail, model_target=model_target,
    )


def _finite_positive(value) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or number <= 0:
        return None
    return number


def derive_structural_target(
    *,
    entry_price: float | None,
    direction: str,
    levels: Sequence[float],
    atr: float | None,
    horizon_sessions: int | None,
    setup_type: str | None,
    model_target: float | None = None,
    min_target_atr_multiple: float = MIN_TARGET_ATR_MULTIPLE,
    breakout_projection_atr_multiple: float = BREAKOUT_PROJECTION_ATR_MULTIPLE,
    max_reach_atr_multiple: float = MAX_REACH_ATR_MULTIPLE,
    max_horizon_sessions: int = MAX_HORIZON_SESSIONS,
    levels_coverage: str = COVERAGE_UNKNOWN,
) -> TargetDerivation:
    """Compute where the instrument actually travels, or decline by name.

    Two classes of "no target", on two fields (see `TargetDerivation`):
    a data FAULT when an input a real market always has is missing (entry
    price, ATR, usable bars), a REFUSAL when the inputs are real and the
    trade's geometry does not work. `levels_coverage` is what separates an
    empty `levels` list into those two — see the COVERAGE_* constants.

    Works both directions. For a long the target is ABOVE entry and drawn
    from levels above it; for a short it is BELOW entry and drawn from levels
    below it. Nothing about the rule is long-only — the comparison operators
    and the sign of the projection are the whole of the difference.

    `levels` is the UNION of the computed supports and resistances, as bare
    prices. That is deliberate: `find_structural_levels` classifies a level
    as support or resistance relative to the LAST CLOSE, while a trade is
    entered at a live price that may sit on the other side of it. What
    matters here is only whether a level is above or below THIS ENTRY, so the
    partition is redone against `entry_price` rather than inherited.

    `model_target` is never used to choose the answer. It is carried through
    so callers can log where the model's guess and the computed level
    disagree, which is the cheapest available read on whether the model's
    targets were ever worth anything.
    """
    guess = _finite_positive(model_target)

    entry = _finite_positive(entry_price)
    if entry is None:
        # A listed instrument always has a price. Not having one is a quote
        # or analysis-acquisition failure on this desk's side.
        return _faulted(
            FAULT_NO_ENTRY,
            "DATA FAULT: no usable entry price was obtained (no live quote "
            "and no analyst entry) — the symbol cannot be measured",
            guess,
        )

    volatility = _finite_positive(atr)
    if volatility is None:
        # ATR is computed by this desk from its own bars
        # (`src/data/technical.py`), never read from a model. A real market
        # always has volatility; a missing reading means the bar history
        # was too short for the calculation or the indicator never ran.
        return _faulted(
            FAULT_NO_VOLATILITY,
            "DATA FAULT: no ATR reading was computed from the bar history, "
            "so neither the noise floor nor the reachable distance can be "
            "measured — the symbol cannot be measured",
            guess,
        )

    try:
        horizon = int(horizon_sessions) if horizon_sessions is not None else 0
    except (TypeError, ValueError):
        horizon = 0
    if horizon <= 0:
        return _refused(
            REFUSAL_NO_HORIZON,
            "no expected_horizon_sessions, so there is no period over which "
            "to ask how far this symbol travels",
            guess,
        )
    horizon = min(horizon, max(1, int(max_horizon_sessions)))

    is_short = str(direction or "").strip().lower() == "short"
    travel = volatility * math.sqrt(horizon)
    reach = travel * max_reach_atr_multiple
    noise = volatility * min_target_atr_multiple
    setup = str(setup_type or "").strip().lower()

    def _divergence(price: float) -> float | None:
        if guess is None:
            return None
        return round((price - guess) / guess * 100.0, 2)

    usable = [p for p in (_finite_positive(lv) for lv in levels or ()) if p is not None]
    if not usable:
        # No structure at all is NOT the same as "no ceiling overhead", and
        # it is not one thing either. `find_structural_levels` returns the
        # same `[]` when the history was too short or too dirty for the
        # pivot scan to run at all (a DATA fault: the desk did not obtain a
        # usable chart) and when the scan ran over enough clean bars and
        # found no level with the minimum touches within reach (a
        # measurement of the chart: a relentless trend, or every repeated
        # turn further away than MAX_DISTANCE_PCT). Only the coverage
        # recorded beside the levels tells them apart. Neither trades —
        # a stop needs a level to sit on — but they go into the record and
        # to the owner as different things.
        coverage = str(levels_coverage or COVERAGE_UNKNOWN).strip().lower()
        if coverage in _FAULT_COVERAGE:
            return _faulted(
                FAULT_NO_STRUCTURE,
                f"DATA FAULT: no structural levels could be computed because "
                f"the price history was unusable (coverage={coverage}) — the "
                f"symbol cannot be measured",
                guess,
            )
        return _refused(
            REFUSAL_NO_STRUCTURE,
            "the price history was measured and holds no structural level "
            "(no repeated turning point within reach of the current price), "
            "so there is nothing for a stop or a target to sit on",
            guess,
        )

    if is_short:
        directional = [p for p in usable if p < entry - noise]
        nearest = max(directional) if directional else None
    else:
        directional = [p for p in usable if p > entry + noise]
        nearest = min(directional) if directional else None

    if nearest is not None and abs(nearest - entry) <= reach:
        price = round(nearest, 2)
        return TargetDerivation(
            price=price,
            basis="structural_level",
            detail=(
                f"nearest structural level {'below' if is_short else 'above'} "
                f"entry ${entry:,.2f} is ${price:,.2f} "
                f"({(price - entry) / entry * 100:+.1f}%), reachable inside "
                f"{horizon} sessions (ATR ${volatility:,.2f} x sqrt({horizon})"
                f" x {max_reach_atr_multiple:g} = ${reach:,.2f})"
            ),
            level_used=price,
            horizon_reach=round(reach, 4),
            model_target=guess,
            divergence_pct=_divergence(price),
        )

    # Past this point no level stands in the way within the horizon.
    #
    # **2026-09-11, docs/WORK.md funnel item 6.** This used to refuse when
    # `nearest is None` unless the analyst had separately typed
    # `setup_type="breakout"`, on the reasoning that no-level-in-direction is
    # ambiguous between a genuine breakout and a chart nothing can be read
    # from. That second possibility is ALREADY excluded above: a chart
    # nothing can be read from yields no `usable` levels at all and is
    # refused as REFUSAL_NO_STRUCTURE. So by the time we get here, the desk's
    # own level computation succeeded AND found nothing in the trade's
    # direction — a measured absence of a ceiling, which is exactly the
    # condition the ATR projection below is for. Requiring a label on top of
    # it refused real trades (funnel item 6) for a wording, while the correct
    # projection sat in this same function unreachable.
    #
    # `is_trend_trade` is the SINGLE definition of that condition, shared
    # with the reward:risk exemption in `PortfolioConstructor.
    # _widen_stop_past_noise` — the two agree by construction rather than by
    # coincidence. Passing the measured fact means this is now always True
    # here and REFUSAL_NO_LEVEL_IN_DIRECTION is unreachable; the constant is
    # kept because it appears in historical logs and in the census.
    if nearest is None and not is_trend_trade(setup_type, structural_ceiling=False):
        return _refused(
            REFUSAL_NO_LEVEL_IN_DIRECTION,
            f"no structural level {'below' if is_short else 'above'} entry "
            f"${entry:,.2f} beyond the ${noise:,.2f} noise floor, and "
            f"setup_type={setup or 'unset'!r} does not claim a breakout",
            guess,
        )

    projection = travel * breakout_projection_atr_multiple
    if projection <= noise:
        return _refused(
            REFUSAL_PROJECTION_IMPLAUSIBLE,
            f"a {horizon}-session measured move of ${projection:,.2f} does "
            f"not clear its own ${noise:,.2f} noise floor",
            guess,
        )
    raw = entry - projection if is_short else entry + projection
    if raw <= 0:
        # Only reachable on an extreme ATR-to-price ratio, but a short whose
        # projection runs through zero is arithmetic, not a trade.
        return _refused(
            REFUSAL_PROJECTION_IMPLAUSIBLE,
            f"a {horizon}-session measured move of ${projection:,.2f} puts "
            f"the short's target at or below zero from entry ${entry:,.2f}",
            guess,
        )

    price = round(raw, 2)
    if nearest is None:
        why = (
            f"no structural level {'below' if is_short else 'above'} entry on "
            f"a chart that yielded {len(usable)} level(s) elsewhere, so "
            f"nothing overhead is expected to stop this trade "
            f"(setup_type={setup or 'unset'!r})"
        )
    else:
        why = (
            f"nearest structural level ${nearest:,.2f} is "
            f"${abs(nearest - entry):,.2f} away, past the ${reach:,.2f} "
            f"reachable in {horizon} sessions, so nothing stands in the way"
        )
    return TargetDerivation(
        price=price,
        basis="measured_move",
        detail=(
            f"{why}; measured move = ATR ${volatility:,.2f} x sqrt({horizon})"
            f" x {breakout_projection_atr_multiple:g} = ${projection:,.2f} -> "
            f"${price:,.2f}"
        ),
        level_used=round(nearest, 2) if nearest is not None else None,
        horizon_reach=round(reach, 4),
        model_target=guess,
        divergence_pct=_divergence(price),
    )
