"""Portfolio Constructor — turns PM target-state into concrete orders.

Phase 2 of the architecture work. Previously the LLM (Portfolio Manager)
emitted TradeDecision objects directly, including entry_price / stop_loss /
take_profit. That put the LLM dangerously close to the execution layer:
- fat-finger-protection patches
- vol-adjusted sizing patches
- stop-limit buffer patches
- sub-penny quantize patches
...were all band-aids for "LLM output an execution detail it shouldn't own."

Now PM emits TargetPosition (target_weight_pct, conviction, thesis,
invalid_if) and this module derives the actual orders from:
- Target state
- Current positions (broker truth)
- TA's ATR + suggested stop (for stop distance)
- Broker's live price (for entry price)
- Total equity + cash (for sizing)

The constructor is deterministic and unit-testable. LLM creativity is
confined to intent; math is code.
"""

from __future__ import annotations

import math
import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass

from src.data.levels import (
    COVERAGE_UNKNOWN,
    FAULT_NO_ANALYSIS,
    FAULT_NO_ENTRY,
    FAULT_NO_PRICE,
    TargetDerivation,
    derive_structural_target,
    horizon_reach,
    level_zone_halfwidth,
    touch_probability,
)
from src.data.technical import LONGEST_INDICATOR_WINDOW
from src.models import (
    Position, TargetPosition, TechAnalysisResult, TradeDecision,
    reward_to_risk, stated_soft_exit,
)
from src.risk.constants import reward_risk_floor_applies

logger = logging.getLogger(__name__)

# Python-stamped named trigger for a funding-trim / size-down. Checkable
# from the same live-book weight (and risk, when passed) the constructor
# used. PM thesis free text explains; it cannot create the sell. Do NOT
# add this phrase to pipeline._HARD_TRIGGER_KEYWORDS — a midday LLM
# could emit it with nothing behind it (correlation-breach lesson,
# 2026-09-13). Descriptive categorisation in storage is separate.
MECHANICAL_SIZE_DOWN_TRIGGER = "mechanical size-down vs live book"


def format_mechanical_size_down_reason(
    *,
    current_weight_pct: float,
    target_weight_pct: float,
    current_risk_pct: float | None = None,
    target_risk_pct: float | None = None,
) -> str:
    """Named trigger stamped from live-book numbers, never from PM prose."""
    parts = [
        f"{MECHANICAL_SIZE_DOWN_TRIGGER}: "
        f"weight {current_weight_pct:.2f}% → {target_weight_pct:.2f}%"
    ]
    if (
        current_risk_pct is not None
        and target_risk_pct is not None
        and math.isfinite(current_risk_pct)
        and math.isfinite(target_risk_pct)
    ):
        parts.append(f"risk {current_risk_pct:.2f}% → {target_risk_pct:.2f}%")
    return "; ".join(parts)


def cites_mechanical_size_down(reason: str | None) -> bool:
    return MECHANICAL_SIZE_DOWN_TRIGGER in (reason or "")


def is_soft_exit_reduction(decision) -> bool:
    """True when a SELL/COVER's named trigger is thesis/falsifier free text.

    A funding-trim whose reasoning starts with the Python mechanical
    size-down warrant is not a soft-exit.
    """
    action = getattr(decision, "action", None)
    if action not in ("SELL", "COVER"):
        return False
    reason = getattr(decision, "reasoning", None) or ""
    return not reason.startswith(MECHANICAL_SIZE_DOWN_TRIGGER)


def _size_down_checkable(
    current_pct: float, target_pct: float, *, long_side: bool,
) -> bool:
    if not math.isfinite(current_pct) or not math.isfinite(target_pct):
        return False
    if long_side:
        return current_pct > 0 and target_pct < current_pct
    return current_pct < 0 and target_pct > current_pct


def _lookup_existing_risk(
    existing_risk_pct: dict[str, float] | None, symbol: str,
) -> float | None:
    if not existing_risk_pct:
        return None
    if symbol in existing_risk_pct:
        val = existing_risk_pct[symbol]
    else:
        val = existing_risk_pct.get(str(symbol).upper())
    if val is None:
        return None
    try:
        parsed = float(val)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _named_reduction_trigger(
    target: TargetPosition,
    current_pct: float,
    target_pct: float,
    *,
    long_side: bool,
    current_risk_pct: float | None = None,
) -> tuple[str, str] | None:
    """(reasoning, falsifier) for a SELL/COVER, or None if no desk warrant.

    A stated thesis_invalid_if is a checkable soft-exit warrant; reasoning
    then keeps the existing thesis + parenthetical so a Python-authored
    close reason already on the target is preserved. With a blank
    falsifier, PM thesis cannot create the sell: only a live-book
    size-down (weight, and risk when known) is stamped as the named
    trigger. Never invents a falsifier.
    """
    falsifier = stated_soft_exit(target.thesis_invalid_if)
    thesis = (target.thesis or "").strip()
    if falsifier:
        reasoning = thesis
        reasoning += f" (thesis_invalid_if: {falsifier})"
        return reasoning, falsifier
    if not _size_down_checkable(current_pct, target_pct, long_side=long_side):
        logger.warning(
            "Constructor: %s %s skipped — blank thesis_invalid_if and "
            "size-down vs live book is not checkable (current_pct=%s "
            "target_pct=%s); PM thesis cannot create the sell",
            "SELL" if long_side else "COVER", target.symbol,
            current_pct, target_pct,
        )
        return None
    trigger = format_mechanical_size_down_reason(
        current_weight_pct=current_pct,
        target_weight_pct=target_pct,
        current_risk_pct=current_risk_pct,
        target_risk_pct=target.risk_allocation_pct,
    )
    if thesis:
        trigger = f"{trigger} (PM explanation: {thesis[:200]})"
    return trigger, ""


class _DropReasonCapture(logging.Handler):
    """Funnel-queue item 2 (2026-09-03): the census
    (`scripts/blocked_proposals_census.py`) found ~19 PM targets per
    reproduction window that never became a `proposed_order` row and
    carried NO other evidence either — classified `no_order_built`. Root
    cause, confirmed against `data/resets/20260902T181859Z/quant_agent.db`
    plus the production `quant_agent.log*` files: the constructor's OWN
    reason for dropping a target (`_resolve_entry_and_stop`,
    `_derive_target`, the reward:risk floor, the sector-crowding refusal,
    ~20 distinct call sites below) has ALWAYS been logger-only — the
    module docstring on `blocked_proposals_census.py` already documents
    this as something "only ever logger.info/logger.warning text — never
    persisted to a table." Every one of the reproduced cases DID have a
    real log line explaining it (in journalctl for a systemd-timer run, or
    in the rotated `quant_agent.log*` file for a manually-triggered one) —
    so "13 with no record anywhere" overstated it; the record existed, just
    not in the database the census script reads and not always in the one
    log sink an operator thinks to check first.

    Rather than thread a reason string through every one of those ~20
    return points (a much larger, riskier change to a stateless,
    heavily-tested sizing function), this captures the SAME log lines the
    module already emits, via a handler scoped to exactly one
    `construct_orders` call, and exposes them on
    `PortfolioConstructor.last_drop_reasons` so the caller
    (`pipeline_stages.DecisionStage`) can persist a terminal per-symbol
    evidence row for every constructor-dropped target — never nothing —
    using the constructor's own real words instead of a generic label.
    """

    _SYMBOL = re.compile(
        r"Constructor:\s*(?:BUY|SHORT|SELL|COVER)?\s*"
        r"([A-Za-z][A-Za-z0-9.\-]{0,9})\s+"
        r"(?:rejected|refused|skipped)\b"
    )

    def __init__(self) -> None:
        super().__init__(level=logging.INFO)
        self.reasons: dict[str, list[str]] = {}

    def emit(self, record: logging.LogRecord) -> None:  # noqa: D102
        try:
            msg = record.getMessage()
        except Exception:  # noqa: BLE001 — a capture side-channel must never raise
            return
        m = self._SYMBOL.search(msg)
        if not m:
            return
        symbol = m.group(1).strip().upper()
        self.reasons.setdefault(symbol, []).append(msg)


# ---------------------------------------------------------------------------
# Named outcomes for the stop rule (spec §12.1, 2026-09-01)
# ---------------------------------------------------------------------------
# Same discipline `src/data/levels.py::derive_structural_target` established
# in §10.4: every path that MOVES or REFUSES a stop says which rule fired, by
# name. "No trade" without a reason is what let the original defect — the ATR
# floor silently overwriting a real structural level — survive unnoticed
# through 38 signals and zero trades on 2026-09-01. The codes are for logs and
# tests; the message beside each one is written for a reader who does not know
# the code.
STOP_RULE_LEVEL_HONOURED = "stop_honoured_at_computed_level"
STOP_RULE_ABSOLUTE_FLOOR = "stop_widened_to_absolute_atr_floor"
STOP_RULE_ATR_BAND = "stop_widened_to_atr_noise_band"
# The two paths that leave the stop exactly where structure put it. Both
# existed before 2026-09-02 as unnamed early returns, and that anonymity is
# precisely how they escaped the reward:risk floor for two weeks — see
# `_widen_stop_past_noise`. Named now, for the same reason every other
# outcome is named.
STOP_RULE_OUTSIDE_BAND = "stop_kept_already_outside_atr_band"
STOP_RULE_NO_VOLATILITY = "stop_kept_no_atr_reading"
STOP_REFUSAL_WRONG_SIDE = "stop_on_wrong_side_of_entry"
#: The unbacked-stop fallback placed the stop under the SIGNAL BAR's low
#: (above its high for a short) because that sat further from entry than
#: the ATR noise band did. Kristjan Kullamägi's own placement for a stop
#: with no level under it — "the low of the day" — read from the last
#: completed bar the analyst judged (`TechAnalysisResult.signal_bar_low` /
#: `signal_bar_high`, Python-set from the same bars as the levels). Rare by
#: construction: it only wins when the signal bar itself spans more than
#: the noise band, i.e. a climactic bar.
STOP_RULE_SIGNAL_BAR = "stop_placed_past_signal_bar"
#: 2026-09-12 (second ruling of the day — see docs/WORK.md item 54). The
#: stop this trade REQUIRES is wider than the instrument can plausibly
#: travel inside the trade's own horizon. The published constraint on a
#: stop is its WIDTH, not whether a level exists under it — Kristjan
#: Kullamägi, in his own words: "stop should not be wider than the ATR or
#: ADR of the stock" (https://qullamaggie.com/my-3-timeless-setups-that-
#: have-made-me-tens-of-millions/). What is adopted is the SHAPE — width
#: measured in the stock's own volatility units, refused past a cap — and
#: NOT his unit, for a reason that is arithmetic, not taste: this desk's
#: own fallback for an unbacked stop is the 2.5 x ATR noise band
#: (`ConstructorConfig.min_stop_atr_multiple`, a doctrine-grounded
#: convention for a days-to-weeks hold; his rule is for an entry timed
#: intraday with the stop under the day's low), so at his 1 x daily range
#: the fallback would refuse itself. Nor can the band be the cap: the
#: ratified sizing rule (§2.1, `_plan_risk_targets`) answers a wide stop
#: with a SMALLER position, never a refusal, and is pinned by tests. The
#: only instrument-read width the desk already has that sits past both is
#: `horizon_reach` — ATR x sqrt(horizon) x the same reach multiple the
#: target derivation and the level scan use. A stop beyond it cannot be
#: hit inside the trade, so the risk-based size computed from it is
#: fiction. Substitution stated plainly: the desk computes ATR(14), not
#: ADR; ATR includes the overnight gap and runs a little wider than ADR on
#: the same stock. The width test is on WHATEVER stop would ship — an
#: honoured level, the absolute floor, the band, or the signal bar.
STOP_REFUSAL_WIDER_THAN_REACH = "stop_wider_than_instrument_reach"
#: The instrument has fewer completed sessions than the LONGEST indicator
#: window the analyst is briefed with (`src/data/technical.py::
#: LONGEST_INDICATOR_WINDOW`, the 200-session moving average). The 2026-
#: 07-16 audit already treated that reference's absence as the analyst
#: judging trend blind; a listing too young to have it also has too few
#: repeated turning points for the level scan to say anything reliable.
#: This is a refusal about the INSTRUMENT (a listing is young), not a data
#: fault (a feed is dead) — PR #326's split classifies the latter. Read
#: from `TechAnalysisResult.bars_available`, Python-set by the analyst from
#: the bars it actually received; None (older row, hand-built object) is
#: not judged, because an unknown count is not a short one.
STOP_REFUSAL_INSUFFICIENT_HISTORY = "insufficient_history"
#: retired board item 49 (`docs/INCIDENT_HISTORY.md`, 2026-09-14). The portfolio risk budget was spent
#: before this candidate's turn came round. NOT a judgement about the idea:
#: it passed every gate, and on a day with fewer competing names it would
#: have been bought. The only reason it produced no order is that better-
#: ranked names took the 25% ceiling first.
#:
#: This exists because the budget denial was the ONE constructor drop path
#: with no durable per-symbol reason at all: it logged
#: "Constructor: X produces no order — risk budget granted 0% ...", which
#: `_DropReasonCapture._SYMBOL` does not match (it requires
#: rejected|refused|skipped after the symbol), so every budget-rationed name
#: reached `_record_constructor_drops` as the generic `constructor_dropped`
#: with detail "no matching constructor log line captured". Once the budget
#: actually binds on a normal day — which is what item 49 is about — that is
#: the largest silent bucket on the sheet. Verified on this branch before the
#: fix by running the capture's own regex against the real message.
STOP_REFUSAL_BUDGET_EXHAUSTED = "risk_budget_exhausted"
#: Board item 10 (2026-09-14): the same defect item 49 fixed for the
#: PORTFOLIO-level budget allocator, found again by statically running
#: `_DropReasonCapture._SYMBOL` against every other constructor drop message
#: in the module (no live data needed — the regex is deterministic and the
#: messages are compile-time strings). §9.4's OTHER refusal path — the net
#: independent source score landing at or below zero — used
#: the identical "Constructor: X produces no order — ..." phrasing item 49
#: already found the regex does not match (it requires rejected|refused|
#: skipped directly after the symbol; "produces" is neither), and reached
#: `_record_constructor_drops` the same generic way. Fixed the same way:
#: `_note_refusal` files the code as data instead of relying on the log
#: scrape ever catching up to a sentence.
STOP_REFUSAL_AGREEMENT_NET = "agreement_net_at_or_below_zero"
#: Board item 10 (2026-09-14). `_build_buy`/`_build_short` already LOG when
#: the risk-budget-per-trade cap or the single-name ceiling
#: shrinks a request ("alloc capped by risk budget" / "... by the single-
#: name ceiling"), but neither message contains rejected/refused/skipped, so
#: the regex never matches them. When one of those caps (or the sector dial
#: above them) leaves nothing to round to above zero, the order silently
#: never ships — and unlike the ATR/no-stop path a few lines up, there is no
#: SECOND log line for the same symbol that happens to match: `_build_buy`
#: just returns `None`. Filed directly from `cap_note`, which already
#: carries the deterministic provenance of whichever cap(s) bound.
STOP_REFUSAL_SIZED_TO_ZERO = "position_sized_to_zero"
#: Board item 10 (2026-09-14). `apply_gross_ceiling` (`src/risk/rules.py`)
#: refuses a BUY/SHORT outright — equity unusable, headroom below the
#: minimum-order floor, or a granted slice that rounds to nothing — and logs
#: through its OWN `Constructor: %s`-wrapped note (`portfolio_constructor.py`
#: relays `outcome.notes` verbatim at `logger.warning("Constructor: %s",
#: note)`). Every one of those notes reads "Constructor: max_gross_exposure:
#: SYMBOL refused — ..." — the rule name sits BETWEEN "Constructor:" and the
#: symbol, which `_DropReasonCapture._SYMBOL` requires to follow immediately
#: (optionally through one of BUY/SHORT/SELL/COVER only). Every gross-ceiling
#: block was therefore invisible to the regex. `GrossCeilingOutcome.
#: blocked_detail` now carries the per-symbol reason text out of
#: `apply_gross_ceiling` so the constructor can file it with `_note_refusal`
#: directly, the same precedent as every other code in this block.
STOP_REFUSAL_GROSS_EXPOSURE_CEILING = "gross_exposure_ceiling_refused"
#: Board item 10 (2026-09-14). The delta loop's churn filter
#: (`min_trade_weight_delta`) silently `continue`s a brand-new position too
#: small to bother with — but only records anything when one already exists
#: to HOLD (`current_pct > 0`). A target asking to open a position below the
#: threshold (`current_pct <= 0`) left, and still leaves, no `TradeDecision`
#: row and no log line of any kind — not a regex miss, there was never
#: anything for the regex to see. Named and filed rather than left mute:
#: this is not a judgement on the idea, only on its size.
CONSTRUCTOR_NO_ACTION_BELOW_MIN_DELTA = "delta_below_min_trade_weight"
#: 2026-09-17 (intra_check-44594a05). A risk-based TRIM of a held name this
#: session did not analyse is sized from the position's live broker stop
#: (see `_held_trim_entry_and_stop`). When there is no usable live stop the
#: trim cannot be sized, and the position is left unchanged. That is NOT a
#: market-data fault — the price feed is fine — so it is filed under its own
#: name instead of `analysis_missing`, which pages the owner to check the feed.
TRIM_REFUSAL_NO_USABLE_LIVE_STOP = "trim_without_analysis_has_no_usable_live_stop"
#: Board item 10, second pass (2026-09-14). The five fixes above closed every
#: path that reached `_record_constructor_drops` as the literal "no matching
#: constructor log line captured". They did NOT make every drop path
#: MACHINE-READABLE: eight more sites still ended a candidate with nothing but
#: a sentence in a log line, recovered by regex into a generic
#: `constructor_dropped` row whose `reason` column is the same for all of
#: them. One of those eight — no typed stop AND no ATR to read one from — the
#: regex misses outright ("Constructor: BUY SYM has no stop ...", and the
#: pattern requires rejected|refused|skipped straight after the symbol), so
#: the "five is the complete set" claim was wrong on its own terms. Codes
#: below, one per site, filed with `_note_refusal` exactly like the rest. The
#: rule this whole item is about, stated once: a candidate is never dropped
#: without a durable, per-symbol, machine-readable reason.
#:
#: The stop the analyst typed, or the entry it is measured from, is not a
#: finite number. Not a trade judgement and not a data fault either — an
#: input arrived and is nonsense, so it is refused rather than compared.
STOP_REFUSAL_STOP_NOT_FINITE = "stop_price_not_finite"
STOP_REFUSAL_ENTRY_NOT_FINITE = "entry_price_not_finite"
#: Nothing typed a stop and there is no ATR reading to derive one from, so
#: there is no stop at all to judge. The ONE path the regex never saw.
STOP_REFUSAL_NO_STOP_NO_VOLATILITY = "no_stop_and_no_volatility_reading"
#: `_resolve_entry_and_stop`'s terminal side check: whatever the stop rules
#: above produced is absent, non-positive, or on the wrong side of entry.
#: Filed only when no more specific refusal was already recorded for this
#: symbol this call — the specific reason always wins over the generic one.
STOP_REFUSAL_NO_VALID_STOP = "no_valid_stop_against_entry"
#: `derive_structural_target` refused (not a data fault) and the trade has no
#: take-profit to measure against. The code filed is the derivation's OWN
#: `refusal` (`no_structural_levels`, `no_level_in_direction`,
#: `no_expected_horizon`, `projection_implausible` — `src/data/levels.py`),
#: because it is already a machine code and inventing a second name for it
#: here would just be a synonym to keep in sync. This constant is the
#: fallback for the impossible case of a refusal with no code on it.
STOP_REFUSAL_NO_STRUCTURAL_TARGET = "no_structural_target"
#: The derivation produced a price, but on the wrong side of the entry — a
#: long whose computed target sits at or below entry, a short whose sits at
#: or above it. Distinct from the above: structure was measurable, and what
#: it measured does not pay for this direction.
STOP_REFUSAL_TARGET_NOT_ABOVE_ENTRY = "target_not_above_entry"
STOP_REFUSAL_TARGET_NOT_BELOW_ENTRY = "target_not_below_entry"
#: §10.3's two sector-crowding refusals. The dial normally SHRINKS a trade;
#: these are the two ends where it has nothing left to shrink to. Both
#: already logged a sentence the regex happened to match, which is how they
#: escaped the first pass — a matched sentence is still not a code.
STOP_REFUSAL_SECTOR_AT_HARD_CEILING = "sector_at_absolute_ceiling"
STOP_REFUSAL_SECTOR_BELOW_MIN_ORDER = "sector_crowding_leaves_below_min_order"
#: The `pipeline_event` reason under which `pipeline_stages.DecisionStage`
#: files a structured constructor refusal (`refusal=<code>` beside it).
#: Distinct from `constructor_dropped`, whose detail is recovered by regex
#: over log text and misses several messages; this one is written from
#: `last_refusals` directly, so the funnel and
#: `scripts/blocked_proposals_census.py` see the code, not a sentence.
CONSTRUCTOR_REFUSED_EVENT_REASON = "constructor_refused"
# DEAD AS OF 2026-09-11 (docs/WORK.md item 1(d)) — the three
# below-floor refusal codes and the PERMIT code below them. Nothing in this
# module refuses a trade on a reward:risk floor any more: a breakout is not
# measured against one at all, and a range trade's real ratio became a
# ranking input instead of a cutoff. Kept, not deleted, because they are
# greppable keys that appear in historical production logs and in
# `docs/INCIDENT_HISTORY.md`, and because whether the sub-floor catalyst
# machinery is retired is an owner call that has not been made. Flagged
# rather than quietly removed.
STOP_REFUSAL_GEOMETRY_AT_BAND = "reward_risk_below_floor_at_widened_stop"
STOP_REFUSAL_GEOMETRY_AT_LEVEL = "reward_risk_below_floor_at_honoured_stop"
STOP_REFUSAL_GEOMETRY_AT_KEPT = "reward_risk_below_floor_at_kept_stop"
STOP_REFUSAL_GEOMETRY_UNMEASURABLE = "reward_risk_not_measurable"
#: Not a refusal — the one PERMIT code in this block. A below-floor ratio let
#: through because the PM's sub-floor catalyst gate verified the citation and
#: capped the size (docs/WORK.md item 1, parts (b)+(c)). Greppable so "how
#: often does the exception actually fire?" is answerable from logs, which is
#: the number part (d) will need before it replaces the hard floor at all.
STOP_PERMIT_SUBFLOOR_CATALYST = "reward_risk_below_floor_catalyst_verified"

#: Which refusal code names the failure, given the rule that placed the stop.
#: One refusal per stop rule, so a log line says both what the stop IS and
#: why the ratio against it did not clear.
_GEOMETRY_REFUSAL_BY_RULE = {
    STOP_RULE_LEVEL_HONOURED: STOP_REFUSAL_GEOMETRY_AT_LEVEL,
    STOP_RULE_ABSOLUTE_FLOOR: STOP_REFUSAL_GEOMETRY_AT_LEVEL,
    STOP_RULE_ATR_BAND: STOP_REFUSAL_GEOMETRY_AT_BAND,
    STOP_RULE_OUTSIDE_BAND: STOP_REFUSAL_GEOMETRY_AT_KEPT,
    STOP_RULE_NO_VOLATILITY: STOP_REFUSAL_GEOMETRY_AT_KEPT,
}

#: Stop rules whose stop sits on a level `find_structural_levels` COMPUTED.
#: `src/pipeline_stages.py` reads this off `TradeDecision.stop_rule` to know
#: it must not re-apply its own execution-time ATR floor to that stop.
LEVEL_BACKED_STOP_RULES = frozenset({
    STOP_RULE_LEVEL_HONOURED, STOP_RULE_ABSOLUTE_FLOOR,
})


@dataclass(frozen=True)
class RiskPlan:
    """A risk-based target resolved into the units the order path speaks.

    `risk_pct` is what the budget actually granted, which may be less than the
    PM asked for; `target_weight_pct` is that risk converted through the stop
    distance into a gross-leverage weight. `note` explains any cut, and is
    carried into the order's reasoning so the AI Risk Manager reads a
    deterministic reduction as arithmetic rather than as the PM contradicting
    itself.
    """

    symbol: str
    risk_pct: float
    target_weight_pct: float
    entry_price: float | None
    stop_price: float | None
    note: str = ""
    #: True when this plan is a trim of a held, unanalysed name sized from its
    #: live broker stop. Such a plan may only REDUCE the position.
    sized_from_live_stop: bool = False


@dataclass
class ConstructorConfig:
    """Tunables for how the constructor sizes and prices orders."""
    # Ceiling on any SINGLE position's risk, and the fallback sizing basis for
    # a legacy notional target. Owner-ratified at 5% (2026-08-27); the prior
    # 0.5% was a constructor default nobody chose. Under risk-based sizing
    # (spec §2.1) this caps `TargetPosition.risk_allocation_pct` rather than
    # driving it — conviction sets the size, this bounds it.
    risk_budget_pct: float = 5.0
    # Below this, an idea is not worth trading: a token position pays full
    # commission and full attention for an immaterial payoff. A request
    # rationed under the floor is denied outright rather than shrunk.
    min_risk_pct: float = 0.5
    # Spec §2.2. Total at-risk ceiling across the book, and the share of it any
    # one correlated cluster may take. Enforced only when the caller supplies
    # `existing_risk_pct` / `clusters` — without those the constructor has no
    # view of the book's risk and must not invent one.
    max_portfolio_risk_pct: float = 25.0
    max_cluster_risk_share_pct: float = 40.0
    # The risk engine's single-name GROSS notional ceiling, mirrored here so
    # the constructor sizes UNDER it instead of proposing an order the engine
    # will hard-block. `max_position_pct` is in HARD_BLOCK_RULES, so without
    # this clamp a BUY over the ceiling is dropped entirely rather than
    # trimmed.
    #
    # 20 -> 100 on 2026-09-04 (real-data audit): at 20 this bound on nearly
    # every trade — notional = risk_pct x entry/(entry - stop) — so delivered
    # risk collapsed to ~1% regardless of stated conviction (6 of 13 real
    # proposed orders pinned at exactly 20%). 100 removed that clipping.
    #
    # 100 -> 33 on 2026-09-11. The 2026-09-04 justification for 100 rested on
    # `allow_margin` being false, which had ALREADY been flipped to true two
    # days earlier (2026-09-02) — so 100 was a live single-name ceiling, not
    # the unreachable documentation it was described as (a separate pass
    # this same day independently caught and recorded the same drift before
    # this fix landed — see docs/INCIDENT_HISTORY.md). 33 was derived from
    # this desk's own -20% `GROSS_LADDER` rung against a -60% median real,
    # dated single-session idiosyncratic collapse — see `risk.max_position_pct`
    # in config/settings.yaml for the full derivation.
    #
    # 33 -> 65 the same day, owner override. Reviewed the derivation directly
    # and set his own risk-appetite number rather than the ladder-consistent
    # one — recorded honestly, not re-derived to fit. At 65 the SAME median
    # disaster (-60%) now costs ~39% of equity, past the -20% alert rung
    # rather than under it; only the mildest of the five reference events
    # stays under that line. The ladder still de-levers what remains — this
    # number no longer prevents that rung from being reached by one name
    # alone, the way 33 was built to. In exchange, 65 mostly does not bind
    # on ordinary trades at current stop distances (33 bound knowingly; 65
    # is mostly a pure backstop against the no-stop-fills case, not a live
    # tax on everyday sizing). This is the ONLY parameter bounding a loss
    # when the stop does not fill at all (gap, halt, fraud, regulatory
    # action) — every other risk number on the desk is stop-conditional.
    # SURVIVAL against single-name tail risk, NOT diversification, variance
    # reduction or risk parity, all of which are rejected for this desk.
    # Keep in sync with `risk.max_position_pct` — pipeline.py wires them from
    # the same setting.
    max_position_pct: float = 65.0
    # Spec §10.3 "concentration scales size, it does not veto". The sector
    # diversification target and the absolute ceiling behind it. Unlike every
    # other ceiling in this dataclass these do not merely make the constructor
    # size UNDER a hard block — between the two the block no longer exists at
    # all, and this is the only place the shrinking happens. See
    # `src/risk/rules.py::sector_size_scale` for the dial and the reasoning
    # behind the ceiling. Kept in sync with `risk.max_sector_pct` /
    # `risk.max_sector_hard_pct` — pipeline.py wires them from the same
    # settings the risk engine reads; these defaults exist only for callers
    # that construct a `ConstructorConfig` directly. Spec §12.3 moved them
    # 40 -> 75 and 60 -> 90.
    max_sector_pct: float = 75.0
    max_sector_hard_pct: float = 90.0
    # Spec §10.3's floor. A position shrunk to near-nothing by sector crowding
    # still pays commission, still consumes a slot, still needs a stop and
    # still needs watching — it cannot pay for its own risk. Below this the
    # honest answer is no trade, not a token trade. Deliberately the SAME
    # $500 threshold `cash_sweep.min_order_usd` already uses rather than a
    # second, divergent notion of "too small to bother"; pipeline.py wires it
    # from that setting.
    min_order_usd: float = 500.0
    # Stage 3 (shorts). SIZING ONLY (never applied to stop placement — see
    # `_widen_stop_past_noise`): a short's risk-per-share is multiplied by
    # this before it is converted to a weight, so the same risk allocation
    # opens a smaller short than an equivalent long. Keep in sync with
    # `risk.short_gap_risk_multiple`.
    short_gap_risk_multiple: float = 1.5
    # Minimum stop distance, in ATRs. A stop inside ordinary volatility is not
    # a thesis invalidation, it is a coin flip on noise — Phase 3 already
    # established 1.25 ATR as one ordinary day's range for a TRAILING stop,
    # and an entry stop has to survive the whole expected hold, not one
    # session. Measured 2026-08-27: the book's stops sat a median 4.3% below
    # entry against a median ATR of 2.56% of price — about 1.7 ATRs, barely
    # more than a single day. Structure still places the stop; this only
    # pushes it out when structure put it inside the noise.
    # This is a BASE, not a constant. `_stop_atr_multiple` adjusts it per
    # trade — a range setup invalidates inside a defined band and does not
    # need the room a breakout does, and a risk-off tape chops harder than a
    # trending one. ATR itself already adapts the distance to each stock and
    # each session; these adjust how many ATRs that stock's setup deserves.
    #
    # 3.0 -> 1.5 -> 2.5 (2026-09-10). This ONLY applies when no real level
    # backs the stop (see `absolute_min_stop_atr_multiple` below for that
    # case) — the owner's standing rule is that a REAL level is always judged
    # on its own honest distance, never overwritten by this number. This is
    # purely the fallback for trades that have no such level.
    #
    # The 1.5 this replaces was measured (Sweeney MAE) against this desk's
    # own ~2-week trade history — the SAME history whose signals were later
    # found to include seats that misreported confidence and data quality
    # (the "content-honesty" fixes, 2026-09-04/05). That window is too short,
    # too clean a regime (no risk-off), and now of suspect provenance to be
    # the sole basis for a risk-of-ruin number. Not necessarily wrong, just
    # no longer trustworthy as the ONLY input.
    #
    # 2.5 instead comes from published doctrine that does not depend on this
    # desk's own data at all: general swing-trading stop-placement guidance
    # puts a FIXED entry stop at 2.5-3.0x ATR (vs. 1.0x scalping, 1.5-2.0x
    # intraday momentum) for a multi-day hold. QAMC's prompt describes itself
    # as exactly that kind of book (days-to-weeks holds), so 2.5 sits inside
    # that bracket rather than at either edge.
    #
    # Caveat, stated honestly: Chuck LeBeau's Chandelier Exit and Van Tharp's
    # volatility-stop work (also cited in this debate, also 2.0-3.0x) are
    # TRAILING-stop mechanisms — the stop recalculates off each new high, it
    # is not a fixed distance from a static entry. This settings file already
    # flagged, correctly, that applying a trailing-stop multiple to a fixed
    # entry stop plus a hard reward:risk floor is a different, more binding
    # combination than the literature's trailing-stop use case. 2.5 is
    # grounded in the general entry-stop consensus above, not in Chandelier/
    # Tharp specifically — noted so the two aren't conflated later.
    #
    # Known tension, disclosed rather than hidden: `min_reward_risk_after_
    # widening` (1.5) requires roughly `sqrt(H) >= 1.5 x effective_multiple`
    # to clear (H = hold in sessions). At the tightest reachable case (range
    # setup, risk-on: 2.5 x 0.90 x 0.95 = 2.14) that needs H >= ~10 sessions
    # — in line with this desk's real observed holds (e.g. ORCL, 10-session
    # horizon). At the widest case (breakout, risk-off: 2.5 x 1.00 x 1.20 =
    # 3.0) it needs H >= ~20 sessions — a real ask, not a free pass. This is
    # the same shape of tension the old 3.0 constant created (which needed
    # ~27 sessions and effectively passed nothing); 2.5 does not eliminate
    # it, it moves the binding constraint back into a range doctrine and this
    # desk's own stated horizons can plausibly both satisfy. Re-measure once
    # honest post-fix trade history exists — this is a doctrine-grounded
    # placeholder, not a permanent constant.
    min_stop_atr_multiple: float = 2.5
    #: Multipliers ON the base, by `TechAnalysisResult.setup_type`.
    #:
    #: DIRECTION CORRECTED 2026-09-04 — these used to read breakout 0.85 /
    #: range 1.15, i.e. the desk's calmest and most common setup was given the
    #: WIDEST floor. That is backwards on both doctrine and the data. A range
    #: trade is a mean-reversion structure inside a defined band: it is the
    #: LOWER-volatility setup, its invalidation is the band edge, and it is
    #: where the too-wide floor did all its damage (0 of 222 real signals
    #: cleared). A breakout enters on volatility EXPANSION, and the ATR
    #: reading at entry is computed over the quiet consolidation that preceded
    #: it — so ATR systematically UNDERSTATES a breakout's post-entry range.
    #: A breakout therefore earns at least the base, never a discount.
    #:
    #: HOW THESE TWO NUMBERS WERE PICKED, and how far to trust them.
    #: The relative direction (range tighter than breakout) is the
    #: well-grounded part — it is doctrine, not this desk's own data: a range
    #: setup invalidates at its own band edge (lower-volatility, mean-
    #: reversion structure), while a breakout enters on volatility EXPANSION
    #: whose ATR reading (taken over the quiet pre-break consolidation)
    #: systematically UNDERSTATES its post-entry range. There is still no
    #: per-setup-type MAE breakdown in this repo to size the magnitudes from,
    #: so they are unchanged from the 2026-09-04 correction:
    #:   breakout 1.00 — runs at the base; no measurement supports a specific
    #:     widening beyond it.
    #:   range 0.90 — a modest tightening off the base, not a specific
    #:     measured number.
    #: Net effect with the 2.5 base (2026-09-10): reachable floor spans
    #: [2.14, 3.00] ATR (range/risk-on to breakout/risk-off) — see
    #: `min_stop_atr_multiple`'s comment for why that range is now judged
    #: against published doctrine rather than this desk's own (suspect)
    #: noise-band/MAE measurements.
    stop_atr_setup_scale: tuple[tuple[str, float], ...] = (
        ("breakout", 1.00),
        ("range", 0.90),
    )
    #: Multipliers ON the base, by macro regime. A risk-off or transitional
    #: tape produces wider ordinary swings for the same ATR reading, so the
    #: same structural stop is nearer the noise than it looks.
    stop_atr_regime_scale: tuple[tuple[str, float], ...] = (
        ("risk-off", 1.20),
        ("transitional", 1.10),
        ("risk-on", 0.95),
    )
    # Widening a stop lowers reward:risk, because the target does not move.
    # **Retired as a gate (owner 2026-09-17): rejects nothing and caps
    # nothing.** No code in this class reads
    # `self.min_reward_risk_after_widening` — grep confirms it. Kept only
    # as the default of a historical settings key so a silent rename
    # cannot drop a deployed threshold; see `src/risk/constants.py`
    # (`REWARD_RISK_FLOOR`) for the full history. Do not re-arm it.
    min_reward_risk_after_widening: float = 1.5
    # --- Level-backed stops (spec §12.1, 2026-09-01) --------------------
    # NO `level_match_atr_tolerance` HERE ANY MORE — removed 2026-09-13,
    # docs/WORK.md item 46, along with the `risk.*` setting it mirrored.
    # It said "within 0.25 ATR of a computed level counts as sitting AT it"
    # and justified 0.25 as being at least as wide as the 1%-of-price zone
    # `find_structural_levels` clusters pivots into. Different units: the
    # claim only held where ATR >= 4% of price, and at this desk's quoted
    # 2.56% median ATR the tolerance was 0.64% — 1.56x narrower than the
    # zone. `_level_backing_stop` now reads the bound off the zone itself
    # via `level_zone_halfwidth`, so there is one number, in one unit, in
    # one file. The ATR question ("is the stop far enough out to survive
    # this name's noise") is unchanged and still lives in
    # `min_stop_atr_multiple` / `absolute_min_stop_atr_multiple`.
    # The deterministic floor UNDER the exemption above, applying to
    # level-backed stops too. See `_widen_stop_past_noise` for the reasoning;
    # in short, §12.1 argues the exemption is safe because
    # `config/prompts/tech_analyst.md` forbids a sub-1-ATR stop, but that is
    # a prompt and Invariant 2 requires the deterministic layer to be the
    # final authority and to fail closed. A level-backed stop is honoured
    # however tight down to this many ATRs; inside it, it is widened to
    # exactly this floor — NOT to the `min_stop_atr_multiple` band.
    # 0 disables the floor entirely. Kept in sync with
    # `risk.absolute_min_stop_atr_multiple`.
    absolute_min_stop_atr_multiple: float = 1.0
    # How many prior touches a computed level needs before `_level_backing_stop`
    # treats a stop resting on it as verified enough for the exemption above
    # (Phase 12.1, 2026-09-03). `find_structural_levels` already requires 2
    # touches to register a level at all, but that was never a quality bar
    # for trusting a TIGHT stop on it — see docs/RESEARCH_FINDINGS.md §7's
    # measured table. Real-vs-shuffled bounce probability only separates with
    # non-overlapping 95% CIs at 5+ touches (real 0.644 [0.590, 0.696] vs
    # shuffled 0.505 [0.470, 0.539]); every lower bucket's CIs overlap or
    # nearly touch, i.e. could be noise. A level below this bar is not
    # "verified" here and the stop falls back to the ATR floors, exactly as
    # an unbacked stop does. Kept in sync with
    # `risk.min_level_touches_for_stop_honor`.
    min_level_touches_for_stop_honor: int = 5
    # --- Target derivation (2026-09-01) ---------------------------------
    # The stop has been computed from measured volatility since 2026-08-27;
    # the target was still the language model's `reference_target`, so the
    # reward:risk gate above was dividing a measurement by an opinion. These
    # tune `src/data/levels.py::derive_structural_target`, which computes the
    # target from the same bars the stop comes from. See that module's
    # target-derivation section for the rule and the evidence; the defaults
    # here deliberately mirror its module-level constants.
    min_target_atr_multiple: float = 1.0
    breakout_projection_atr_multiple: float = 1.0
    max_target_reach_atr_multiple: float = 1.5
    # The stop-WIDTH refusal threshold (item 56, 2026-09-13). Was the line
    # above until today; same value, separate knob, because estimating a
    # target and refusing a trade are two jobs and neither derived the 1.5.
    max_stop_width_reach_atr_multiple: float = 1.5
    max_target_horizon_sessions: int = 60
    # The model's target is not thrown away — it becomes evidence. Above this
    # absolute percentage gap between the computed target and the model's
    # guess, the disagreement is logged at WARNING rather than INFO, because
    # a model that is consistently far from the chart is a finding about the
    # model, not about the trade.
    target_divergence_warn_pct: float = 25.0
    # Minimum delta to trigger a rebalance order (avoid tiny 0.2% churn trades).
    min_trade_weight_delta: float = 0.5
    # --- Spec §11.2 — the gross-exposure ceiling (2026-09-01) ------------
    # The SIZING half of the ceiling. `max_gross_exposure` is in
    # HARD_BLOCK_RULES, so without this clamp an entry that breaches the
    # ceiling is DROPPED at the execution gate rather than taken smaller —
    # the same relationship `max_position_pct` already has with its clamp
    # here, and the same reason: a ceiling that only refuses produces
    # no-trade sessions instead of right-sized ones.
    #
    # This is the STANDING cap. The ladder-resolved ceiling for the session
    # is passed to `construct_orders` per run, because it depends on live
    # drawdown and a config default cannot know it. Kept in sync with
    # `risk.max_gross_exposure_x` — pipeline.py wires them from the same
    # setting.
    max_gross_exposure_x: float = 2.0
    # The cash-park vehicle (`cash_sweep.symbol`, SGOV by default), which is
    # parked cash and NOT exposure. Taken from config rather than hardcoded;
    # None when sweeping is off.
    cash_park_symbol: str | None = None
    # NOTE (2026-08-27): the naive-percent stop fallback (`entry * 0.95`)
    # was REMOVED and stays removed. The ATR-multiple fallback came back on
    # 2026-09-12 (docs/WORK.md item 54) in a different form: not a silent
    # `entry - 2*ATR` that became the norm because the analyst typed
    # nothing, but the desk's own noise band (`min_stop_atr_multiple`, per
    # setup and tape) or the signal bar's edge, whichever is wider, applied
    # ONLY when nothing computed backs the typed stop — and the result is
    # then gated on width. The analyst schema requires a stop on every
    # actionable rating, so "nothing typed" is the rare case, not the norm.


class PortfolioConstructor:
    """Stateless translator: target state → concrete orders."""

    def __init__(self, config: ConstructorConfig | None = None):
        self.cfg = config or ConstructorConfig()
        # Populated fresh by every `construct_orders` call — see
        # `_DropReasonCapture`. {symbol: "Constructor: ... rejected/refused
        # ..."} for every target dropped THIS call. Empty, never absent, so
        # a caller can always `getattr(..., "last_drop_reasons", {})` — or
        # just read the attribute — without a first-call special case.
        self.last_drop_reasons: dict[str, str] = {}
        # DATA FAULTS (2026-09-12): {SYMBOL: {"fault", "detail", "direction"}}
        # for every symbol `_derive_target` (or the no-entry-price branch of
        # `_resolve_entry_and_stop`) found UNMEASURABLE — an input a real
        # market always has (price, volatility, usable bars) that this desk
        # failed to obtain. Never a trade judgement, and deliberately NOT
        # mixed into `last_drop_reasons`' classification: the caller
        # (`pipeline_stages.DecisionStage`) records these as `data_fault`
        # rows, not `constructor_dropped`, and pages the owner.
        #
        # Accumulates across `real_reward_risk_preview` (the PM-eligibility
        # pass over every analysed symbol, which runs BEFORE the PM and is
        # where a symbol silently becomes unanalysable) and
        # `construct_orders`, because both run on this one instance in one
        # session. `drain_data_faults()` hands them over and clears; it is
        # the caller's job to drain once per session.
        self.last_data_faults: dict[str, dict[str, str]] = {}

        # New names the caller could not price to a fresh today print (item
        # 120), mapping SYMBOL -> the fault code to file (FAULT_NO_PRICE or
        # FAULT_STALE_PRICE). Populated per `construct_orders` call from its
        # `unpriceable_symbols` argument; consulted by
        # `_resolve_entry_and_stop` so such a name is refused as a data fault
        # rather than sized on a stale/mid price. Initialised here so the
        # backtest shim and older tests that call `_resolve_entry_and_stop`
        # directly never hit an unset attribute.
        self._unpriceable_symbols: dict[str, str] = {}

        # STRUCTURED refusals (2026-09-12): {SYMBOL: {"refusal", "detail",
        # "direction"}} for every trade this instance refused BY NAME —
        # today STOP_REFUSAL_WIDER_THAN_REACH and
        # STOP_REFUSAL_INSUFFICIENT_HISTORY. Written directly, never
        # recovered from log text: `last_drop_reasons`
        # is a regex over the constructor's own log lines and several
        # messages miss its pattern, so a refusal that mattered could reach
        # the record as "no matching constructor log line captured".
        #
        # Accumulates across `real_reward_risk_preview` (the PM-eligibility
        # pass over every analysed symbol, which runs BEFORE the PM) and
        # `construct_orders`, because both run on this one instance in one
        # session. `drain_refusals()` hands them over and clears; the caller
        # (`pipeline_stages.DecisionStage`) drains once per session.
        self.last_refusals: dict[str, dict[str, str]] = {}

        # SIDE FLIPS (board item 164, 2026-09-19): {SYMBOL: {...}} for every
        # target this `construct_orders` call collapsed from "flip the side"
        # to "close only" (rule D3 below). The symbol is NOT dropped — it
        # still gets its closing leg — so neither `last_drop_reasons` nor
        # `last_refusals` ever saw it, and the only trace was a log line.
        # Reset per call; `pipeline_stages.DecisionStage` persists it.
        self.last_side_flips: dict[str, dict] = {}

    def drain_data_faults(self) -> dict[str, dict[str, str]]:
        """Return every data fault recorded since the last drain, and clear.

        See `last_data_faults`. Returned as a fresh dict so the caller can
        hold it after this instance moves on to the next session.
        """
        faults = dict(self.last_data_faults)
        self.last_data_faults = {}
        return faults

    def _note_data_fault(
        self, symbol: str, direction: str, fault: str, detail: str,
    ) -> None:
        """Record and log one UNMEASURABLE symbol. Never raises.

        The log line deliberately says "skipped" and "UNMEASURABLE", not
        "rejected": the constructor did not judge this trade, it could not
        measure the symbol. `_DropReasonCapture` still picks the line up
        (so the symbol is never silently absent from `last_drop_reasons`),
        and the caller reads `last_data_faults` FIRST to file it under the
        right class.
        """
        try:
            key = str(symbol or "").strip().upper()
            self.last_data_faults[key] = {
                "fault": str(fault), "detail": str(detail),
                "direction": str(direction or ""),
            }
        except Exception:  # noqa: BLE001 — a record side-channel must never raise
            pass
        logger.warning(
            "Constructor: %s %s skipped — UNMEASURABLE, a data fault and "
            "not a trade judgement [%s]: %s",
            "SHORT" if str(direction).lower() == "short" else "BUY",
            symbol, fault, detail,
        )


    def drain_refusals(self) -> dict[str, dict[str, str]]:
        """Return every structured refusal since the last drain, and clear.

        See `last_refusals`. A fresh dict, so the caller can hold it after
        this instance moves on to the next session.
        """
        refusals = dict(self.last_refusals)
        self.last_refusals = {}
        return refusals

    def _note_refusal(
        self, symbol: str, direction: str, refusal: str, detail: str,
        *, only_if_unrecorded: bool = False, action: str | None = None,
    ) -> None:
        """Record and log one NAMED trade refusal. Never raises.

        The log line says "refused" so `_DropReasonCapture` also picks it
        up (the symbol is never absent from `last_drop_reasons`), but the
        durable record is the structured entry — the caller reads
        `last_refusals` FIRST and files the code as data.

        `only_if_unrecorded` is for the BACKSTOP callers (board item 10,
        2026-09-14): a terminal check that fires after a more specific rule
        has already refused the same symbol — `_resolve_entry_and_stop`'s
        side check running on a `None` that `_widen_stop_past_noise` just
        refused by name, say. The specific reason must win, so the backstop
        writes nothing (and logs nothing) when this symbol already carries a
        refusal or a data fault from this call. Without it the generic code
        would overwrite the precise one and the fix would make the record
        worse, not better.
        """
        key = str(symbol or "").strip().upper()
        if only_if_unrecorded and (
            key in self.last_refusals or key in self.last_data_faults
        ):
            return
        try:
            self.last_refusals[key] = {
                "refusal": str(refusal), "detail": str(detail),
                "direction": str(direction or ""),
            }
        except Exception:  # noqa: BLE001 — a record side-channel must never raise
            pass
        logger.warning(
            "Constructor: %s %s refused [%s] — %s",
            action or ("SHORT" if str(direction).lower() == "short" else "BUY"),
            symbol, refusal, detail,
        )

    def _require_sufficient_history(
        self,
        symbol: str,
        analysis: TechAnalysisResult | None,
        direction: str,
    ) -> bool:
        """True unless the instrument is too YOUNG to be measured.

        The one narrow refusal that survived the 2026-09-12 replacement of
        "no floor, no trade" (item 54, retired; this gate is now
        docs/WORK.md item 180): a listing with fewer
        completed sessions than the longest indicator window the analyst is
        briefed with (`LONGEST_INDICATOR_WINDOW`, the 200-session moving
        average) is refused by name, on both setup types, from the ONE
        funnel `real_reward_risk_preview` and `_resolve_entry_and_stop`
        share — FIRST, before the target derivation, because a listing this
        young usually yields no levels either and the honest name for that
        is this one, not the derivation's `no_structural_levels`. Nothing
        about the chart's SHAPE is judged here — absent
        structure is not a reason (no published method refuses a trade for
        lack of support below); an unmeasurable instrument is.

        Not a data fault: the bars arrived and are clean, there are simply
        too few of them yet. A missing count (older persisted row, a
        hand-built analysis) is not judged — an unknown count is not a
        short one, and the desk does not refuse on what it did not measure.
        """
        count = getattr(analysis, "bars_available", None)
        try:
            count = int(count) if count is not None else None
        except (TypeError, ValueError):
            count = None
        if count is None or count >= LONGEST_INDICATOR_WINDOW:
            return True
        self._note_refusal(
            symbol, direction, STOP_REFUSAL_INSUFFICIENT_HISTORY,
            f"only {count} completed session(s) of history against the "
            f"{LONGEST_INDICATOR_WINDOW}-session window the analyst's own "
            f"trend reference needs. Too young to measure, not a judgement "
            f"about the chart: the name qualifies the day it has the history.",
        )
        return False

    def construct_orders(self, *args, **kwargs) -> list[TradeDecision]:
        """Same contract as `_construct_orders_impl` — see its docstring for
        every parameter. Wraps it only to capture, via `_DropReasonCapture`,
        the real reason each dropped target's own log line already states,
        onto `self.last_drop_reasons` (funnel-queue item 2, 2026-09-03: see
        `_DropReasonCapture`'s docstring for why this exists instead of
        threading a reason string through ~20 return points).
        """
        capture = _DropReasonCapture()
        logger.addHandler(capture)
        self.last_side_flips = {}
        try:
            return self._construct_orders_impl(*args, **kwargs)
        finally:
            logger.removeHandler(capture)
            self.last_drop_reasons = {
                sym: " | ".join(msgs) for sym, msgs in capture.reasons.items()
            }

    def _construct_orders_impl(
        self,
        targets: list[TargetPosition],
        positions: list[Position],
        analyses: list[TechAnalysisResult],
        total_value: float,
        price_map: dict[str, float] | None = None,
        existing_risk_pct: dict[str, float] | None = None,
        clusters: list[list[str]] | None = None,
        regime: str | None = None,
        evidence_registry: dict[str, dict[str, str]] | None = None,
        stale_sources: dict[str, frozenset[str]] | None = None,
        gross_ceiling=None,
        ranking: Sequence[str] | None = None,
        live_stops: dict[str, float] | None = None,
        unpriceable_symbols: dict[str, str] | set[str] | None = None,
    ) -> list[TradeDecision]:
        """Produce the order list that moves the book from current → target state.

        Orders are returned in a canonical order: exits (SELL/COVER, partials
        and full closes) first, then entries (BUY/SHORT). Execution layer is
        free to re-order, but this matches the existing pipeline assumption
        (exits free up capacity first).

        `price_map`: optional {symbol: live_price} — required for BUYs so
        the constructor can sanity-check TA's entry. If absent for a BUY
        symbol, we fall back to TA's entry_price.

        `unpriceable_symbols`: new names (docs/WORK.md item 120) for which
        the caller's freshness resolver (`src.data.live_price`) could NOT
        obtain a fresh today print this session — a thin name with no print,
        or a feed that returned only a stale print. A mapping SYMBOL ->
        fault code (`FAULT_NO_PRICE` / `FAULT_STALE_PRICE`); a bare set is
        also accepted and defaults every entry to `FAULT_NO_PRICE`. Sizing a
        buy off a stale price (or a quote mid) mis-sizes it proportionally,
        so each of these is refused as a DATA FAULT and dropped rather than
        sized on a bad price OR on the TA entry fallback. It never affects a
        held name: the caller only lists new targets it tried and failed to
        price. `resolve_live_price` returns a real print or today's forming
        session bar and never a quote mid, so an absent value here means no
        usable today price of any kind — not merely "no last trade".

        `existing_risk_pct` / `clusters`: spec §2.2. The book's current
        per-symbol budget risk (`src/risk/metrics.py`) and its measured
        correlation clusters (`src/data/correlation.py`). Supplied together
        they turn the 25% at-risk ceiling from a figure the PM was shown into
        a gate it cannot exceed. Omitted, the portfolio-level ceilings are not
        enforced — the constructor has no view of the book's risk and must not
        invent one — though per-position sizing and the 5% single-name ceiling
        still apply.

        `evidence_registry`: spec §9.4. {symbol: {source: stance}} — the same
        canonical registry `PortfolioManagerAgent.build_evidence_registry`
        built for the PM's own prompt this session (the caller recomputes it
        from the identical inputs; it is a pure function of them, so this is
        guaranteed to agree with what the PM was shown). Drives the agreement
        ceiling in `_plan_risk_targets`. Omitted, that ceiling is not enforced
        — same "no view, don't invent one" posture as `existing_risk_pct`.

        `stale_sources`: spec §9.4 freshness. {symbol: {source}} — registry
        entries that are real coverage but too old to earn size, from
        `PortfolioManagerAgent.stale_evidence_sources` (the same pure function
        the PM's own prompt used, recomputed by the caller from identical
        inputs). Removed from the agreement tally only, so it can lower a
        ceiling and never raise one. Omitted, nothing is gated — a caller with
        no freshness view must not invent one, exactly as above.

        `gross_ceiling`: spec §11.2. The de-levering ladder's resolved
        `GrossCeiling` for this session — the standing cap, stepped down by
        measured peak-to-trough drawdown. Entries are shrunk to fit it, and
        refused outright when what remains is below `min_order_usd`. Omitted,
        the constructor falls back to the standing cap with no drawdown
        applied, so a caller that forgets it still sizes under A ceiling
        rather than none. **This function never trims the held book** — the
        gross ceiling's de-lever is authored in the session preamble, before
        any agent runs, so it cannot depend on a model returning a book.

        `live_stops`: {symbol: live broker stop} for held positions — the
        same stops the heat roll-up behind `existing_risk_pct` is computed
        from. Used ONLY to size a risk-based trim of a held name that has no
        analysis this session (`_held_trim_entry_and_stop`). Omitted, such a
        trim is refused by name and the position is left unchanged.

        `ranking`: retired board item 49, owner decision 2026-09-12 (`docs/INCIDENT_HISTORY.md`, 2026-09-14). The
        session's candidate order, BEST FIRST — the caller passes the symbols
        of `PortfolioManagerAgent.last_candidate_ranking`, i.e. exactly the
        `rank_verdicts` order the PM itself was shown. When the risk budget
        binds it is spent down this order rather than in whatever order the
        allocator happened to iterate. Omitted, the allocator's pre-decision
        ordering applies — no ranking is invented here or there.
        """
        if total_value <= 0:
            return []
        price_map = price_map or {}
        # New names the caller could not price to a fresh today print (item
        # 120), keyed upper-cased so the lookup in `_resolve_entry_and_stop`
        # cannot miss on case/whitespace drift; the value is the fault code
        # to file. A bare set defaults every entry to FAULT_NO_PRICE. Reset
        # every call — per-run state, like `price_map` itself.
        if isinstance(unpriceable_symbols, dict):
            self._unpriceable_symbols = {
                str(s).strip().upper(): str(code or FAULT_NO_PRICE)
                for s, code in unpriceable_symbols.items()
            }
        else:
            self._unpriceable_symbols = {
                str(s).strip().upper(): FAULT_NO_PRICE
                for s in (unpriceable_symbols or ())
            }
        current_weights = self._current_weights(positions, total_value)
        analyses_by_sym = {a.symbol: a for a in analyses}
        positions_by_sym = {p.symbol: p for p in positions}

        # Spec §2.1/§2.2. Resolve each risk-based target's implied notional
        # weight BEFORE the delta loop, because that weight is what every
        # downstream step — the churn filter, the close test, the partial-sell
        # fraction — already speaks in. Conviction arrives as risk; the stop
        # converts it to a size; the budget rations it across the book.
        risk_plan = self._plan_risk_targets(
            targets,
            analyses_by_sym=analyses_by_sym,
            price_map=price_map,
            current_weights=current_weights,
            existing_risk_pct=existing_risk_pct,
            clusters=clusters,
            regime=regime,
            evidence_registry=evidence_registry,
            stale_sources=stale_sources,
            ranking=ranking,
            live_stops=live_stops,
        )

        # Spec §10.3. Held GROSS exposure per sector, carried through the
        # loop and updated as each entry is built, so the second and third
        # targets in one crowded sector are sized against a book that already
        # contains the first.
        sector_weights = self._current_sector_weights(positions, total_value)

        sells: list[TradeDecision] = []
        buys: list[TradeDecision] = []

        for target in targets:
            sym = target.symbol
            current_pct = current_weights.get(sym, 0.0)
            is_short_target = target.direction == "short"
            if target.risk_allocation_pct is not None:
                plan = risk_plan.get(sym)
                if plan is None:
                    # No stop, no entry, or the budget refused it outright.
                    # drop-reason: delegated — `_plan_risk_targets` files the
                    # named refusal (agreement net, budget exhausted) or
                    # `_resolve_entry_and_stop` filed the fault/refusal that
                    # left this symbol without a plan in the first place.
                    continue
                target_mag = plan.target_weight_pct  # unsigned magnitude
            else:
                target_mag = target.target_weight_pct or 0.0

            # D1 (Stage 3): signed target. `current_pct` is already signed
            # (Stage 1) — negative means a held short. Everything below
            # operates on SIGNED weights, so the sign of the delta IS the
            # side of the order: positive is buy-side (BUY to open/add a
            # long, or COVER to reduce a short); negative is sell-side
            # (SELL to reduce a long, or SHORT to open/add a short).
            signed_target = -target_mag if is_short_target else target_mag

            # D3: sign-crossing is refused. A single order that flips a
            # position from long to short (or back) is unprotected for the
            # instant between legs, and the broker treats a sell LARGER
            # than the held quantity differently again (it opens a short
            # rather than just closing). Emit ONLY the closing leg this
            # session — flatten to zero — and let the position open on the
            # other side next session once the book is actually flat.
            if (current_pct > 0 and signed_target < 0) or (current_pct < 0 and signed_target > 0):
                logger.warning(
                    "Constructor: %s target flips side (held %.2f%%, signed "
                    "target %.2f%%) — refusing the flip. Emitting only the "
                    "flattening leg this session; the other side may open "
                    "next session once the book is actually flat.",
                    sym, current_pct, signed_target,
                )
                try:
                    self.last_side_flips[str(sym).strip().upper()] = {
                        "held_weight_pct": current_pct,
                        "requested_weight_pct": signed_target,
                        "emitted_weight_pct": 0.0,
                    }
                except Exception:  # noqa: BLE001 — a record side-channel must never raise
                    pass
                signed_target = 0.0

            plan_for_sym = (
                risk_plan.get(sym) if target.risk_allocation_pct is not None else None
            )
            if (
                plan_for_sym is not None and plan_for_sym.sized_from_live_stop
                and abs(signed_target) > abs(current_pct)
            ):
                # A trim sized from the live stop may only reduce. At its live
                # stop this position already carries no more than the risk
                # asked for, so there is nothing to sell — and with no
                # analysis there is no basis to buy. Hold it as it is.
                logger.info(
                    "Constructor: %s trim needs no order — at its live stop "
                    "($%.2f) the position already risks no more than the "
                    "%.2f%% asked for; left unchanged.",
                    sym, plan_for_sym.stop_price or 0.0, plan_for_sym.risk_pct,
                )
                signed_target = current_pct

            delta_pct = signed_target - current_pct

            # signed_target == 0 is PM saying "CLOSE this position" (long or
            # short), not "rebalance toward ~0". The churn filter must not
            # swallow it: a 0.4%-weight dreg with an explicit close target
            # was silently converted into a HOLD, so a position PM had
            # decided to exit sat in the book indefinitely (2026-07-16
            # audit). Anything held with target 0 goes to the SELL/COVER
            # builder, which emits a full exit.
            closing = (signed_target == 0 and current_pct != 0)
            if not closing and abs(delta_pct) < self.cfg.min_trade_weight_delta:
                # No action — emit HOLD for audit continuity so PM's intent
                # to keep this position at its current level is recorded.
                # (A held short with no delta gets no HOLD row — HOLD's
                # audit bookkeeping stays long-only for this stage.)
                if current_pct > 0:
                    buys.append(self._hold_decision(target))
                else:
                    # Board item 10 (2026-09-14): a brand-new position too
                    # small to bother opening got neither a TradeDecision
                    # row (HOLD is long-only bookkeeping, above) nor any log
                    # line at all — not a regex miss, there was nothing for
                    # `_DropReasonCapture` to see. Not a judgement on the
                    # idea, only on its size.
                    #
                    # Board item 89 defect 6 (2026-09-18). Two corrections
                    # here, neither of which touches the threshold or the
                    # decision it drives.
                    #
                    # 1. This comment used to call the threshold "an
                    #    already-ratified config threshold". It is neither.
                    #    `min_trade_weight_delta = 0.5` exists ONLY as a
                    #    dataclass default in this file — it is absent from
                    #    `config/settings.yaml`, from every document in
                    #    `docs/`, and from any ratification record; the only
                    #    justification written anywhere is the parenthetical
                    #    "(avoid tiny 0.2% churn trades)" beside the default,
                    #    which does not even match the value. It is an
                    #    unsourced trading number of exactly the class
                    #    `docs/WORK.md` item 90 covers. It is NOT changed
                    #    here: this change is about reporting the drop, and
                    #    the number is a separate owner question.
                    # 2. The sentence the owner reads is rewritten for a
                    #    reader who is not a developer. The measured
                    #    figures are unchanged and nothing is rounded,
                    #    estimated or added; "No existing position to record
                    #    as a HOLD" was internal bookkeeping and is gone.
                    self._note_refusal(
                        sym, target.direction,
                        CONSTRUCTOR_NO_ACTION_BELOW_MIN_DELTA,
                        f"the desk decided to open this but the position it "
                        f"asked for was {abs(delta_pct):.2f}% of the "
                        f"account, and the desk does not place a new trade "
                        f"smaller than {self.cfg.min_trade_weight_delta:.2f}"
                        f"% of the account. The whole plan for this name "
                        f"was dropped on size alone — nothing was judged "
                        f"wrong with the idea. Nothing already held was "
                        f"touched.",
                    )
                # drop-reason: both arms above are accounted for — a held
                # position leaves a HOLD row (the symbol survives), a
                # brand-new one files CONSTRUCTOR_NO_ACTION_BELOW_MIN_DELTA.
                continue

            if delta_pct < 0:
                if current_pct > 0:
                    # Trim or close a LONG.
                    sell_decision = self._build_sell(
                        target, positions_by_sym.get(sym), current_pct, signed_target,
                        current_risk_pct=_lookup_existing_risk(
                            existing_risk_pct, sym,
                        ),
                    )
                    if sell_decision is not None:
                        sells.append(sell_decision)
                else:
                    # Open or add to a SHORT (current_pct <= 0).
                    short_decision = self._build_short(
                        target,
                        plan=risk_plan.get(sym),
                        analysis=analyses_by_sym.get(sym),
                        current_pct=current_pct,
                        target_pct=signed_target,
                        total_value=total_value,
                        market_price=price_map.get(sym),
                        regime=regime,
                        sector_weights=sector_weights,
                    )
                    if short_decision is not None:
                        self._accrue_sector(sector_weights, short_decision)
                        sells.append(short_decision)
            else:
                if current_pct < 0:
                    # Cover (reduce/close) a SHORT.
                    cover_decision = self._build_cover(
                        target, positions_by_sym.get(sym), current_pct, signed_target,
                        current_risk_pct=_lookup_existing_risk(
                            existing_risk_pct, sym,
                        ),
                    )
                    if cover_decision is not None:
                        buys.append(cover_decision)
                else:
                    # Open or add a LONG.
                    buy_decision = self._build_buy(
                        target,
                        plan=risk_plan.get(sym),
                        analysis=analyses_by_sym.get(sym),
                        current_pct=current_pct,
                        target_pct=signed_target,
                        total_value=total_value,
                        market_price=price_map.get(sym),
                        regime=regime,
                        sector_weights=sector_weights,
                    )
                    if buy_decision is not None:
                        self._accrue_sector(sector_weights, buy_decision)
                        buys.append(buy_decision)

        # Canonical ordering: SELLs first (free up cash), then BUYs.
        # Among SELLs: full closes before partials. Among BUYs: by target
        # weight descending (largest commitments first so cash rationing
        # in a tight-cash session prioritizes highest conviction).
        sells.sort(key=lambda d: 0 if d.allocation_pct >= 100 else 1)
        buys.sort(key=lambda d: d.allocation_pct, reverse=True)
        orders = sells + buys

        # Spec §11.2 — the SIZING half of the gross-exposure ceiling. Runs
        # last, on the finished order list, because it is the only ceiling
        # here that is a property of the WHOLE book rather than of one name:
        # the exits above have already reduced what will be held, and every
        # entry has to be rationed against the same headroom.
        #
        # `emit_trims=False` on purpose. Shrinking an order it is about to
        # propose is this class's job; authoring a de-lever of the held book
        # is not. That has exactly one owner — the session preamble, which
        # runs before any agent and therefore keeps working on a run where
        # the Portfolio Manager returns nothing at all.
        from src.risk.rules import (
            GrossCeiling, apply_gross_ceiling, resolve_gross_ceiling,
        )
        # isinstance, not truthiness: a caller (or a Mock pipeline in a test)
        # that hands over something ceiling-shaped-but-not-a-ceiling must fall
        # back to the standing cap rather than silently size against a
        # comparison that raises.
        ceiling = (
            gross_ceiling if isinstance(gross_ceiling, GrossCeiling)
            else resolve_gross_ceiling(
                None, base_x=self.cfg.max_gross_exposure_x,
            )
        )
        outcome = apply_gross_ceiling(
            orders, positions, total_value, ceiling,
            cash_park_symbol=self.cfg.cash_park_symbol,
            min_order_usd=self.cfg.min_order_usd,
            emit_trims=False,
        )
        for note in outcome.notes:
            logger.warning("Constructor: %s", note)
        # Board item 10 (2026-09-14): every note above is relayed through
        # THIS logger as "Constructor: max_gross_exposure: SYMBOL refused —
        # ...", which `_DropReasonCapture._SYMBOL` never matches — the rule
        # name sits between "Constructor:" and the symbol. File the
        # structured refusal directly from `outcome.blocked_detail` instead
        # of leaning on the log scrape. Direction is read off the ORIGINAL
        # decision (before the filter below drops it) so a blocked SHORT is
        # recorded as a short refusal, not defaulted to BUY.
        if outcome.blocked:
            action_by_symbol = {d.symbol: d.action for d in orders}
            for sym in outcome.blocked:
                direction = "short" if action_by_symbol.get(sym) == "SHORT" else "long"
                self._note_refusal(
                    sym, direction, STOP_REFUSAL_GROSS_EXPOSURE_CEILING,
                    outcome.blocked_detail.get(
                        sym,
                        "the §11.2 gross-exposure ceiling refused this "
                        "entry outright; no further detail was recorded",
                    ),
                )
        # An entry rationed to nothing is dropped rather than emitted as a
        # zero-allocation order — `allocation_pct == 0` means SKIP to the
        # execution stage, and leaving it in the list would show the operator
        # a trade that was never going to happen.
        orders = [
            d for d in orders
            if d.action not in ("BUY", "SHORT") or d.allocation_pct > 0
        ]
        return orders

    def _plan_risk_targets(
        self,
        targets: list[TargetPosition],
        *,
        analyses_by_sym: dict,
        price_map: dict[str, float],
        current_weights: dict[str, float],
        existing_risk_pct: dict[str, float] | None,
        clusters: list[list[str]] | None,
        regime: str | None = None,
        evidence_registry: dict[str, dict[str, str]] | None = None,
        stale_sources: dict[str, frozenset[str]] | None = None,
        ranking: Sequence[str] | None = None,
        live_stops: dict[str, float] | None = None,
    ) -> dict[str, RiskPlan]:
        """Turn risk-based targets into notional weights, under the budget.

        Spec §2.1: `shares = (equity x risk_pct) / |entry - stop|`, which as a
        notional weight is `risk_pct x entry / (entry - stop)`. The equity term
        cancels, so this needs no book value — only the stop distance. A wider
        stop yields a SMALLER position rather than a rejected trade, which is
        what eliminates the "stops too tight" failure class: risk is never
        controlled by squeezing the stop.

        Spec §2.2: the requested risks are rationed against the total and
        per-cluster ceilings before any of them is converted to a size, so the
        book is bounded by construction rather than by a later veto.

        Spec §9.4, as of 2026-09-14: the SIGNED source score — aligned seats
        minus opposed ones (`evidence_registry` + `signed_source_score`) — is
        a REFUSAL GATE and nothing more. A score at or below zero refuses the
        request entirely, so the target produces no order at all; a held
        position is left exactly where it is, because refusing to BUY is not
        a decision to SELL. A score of 1 or more imposes no size restriction:
        the graduated ceiling that used to scale risk by sqrt(net / 5 seats)
        is retired, because that law prices INDEPENDENT estimates and these
        seats read overlapping evidence. See
        `src/risk/rules.py::agreement_refuses_trade`. Agreement still ORDERS
        which candidates get funded first, through `rank_verdicts` and the
        allocator's `priority`.
        """
        from src.risk.budget import RiskRequest, allocate_risk_budget
        from src.risk.rules import (
            _gross_multiplier, agreement_refuses_trade, count_aligned_sources,
            count_opposing_sources, signed_source_score,
        )
        from src.risk.size_override import SizeOverride

        priced: dict[str, tuple[float, float]] = {}   # symbol -> (entry, stop)
        # Stage 3: direction is tracked alongside the priced entry/stop so
        # the weight formula below can pick the right (unsigned) risk-per-
        # share denominator and apply the short sizing haircut. The RESULT
        # (`target_weight_pct`) stays an unsigned magnitude either way — the
        # delta loop in `construct_orders` applies the sign from
        # `target.direction`.
        directions: dict[str, str] = {}
        live_stop_trims: set[str] = set()
        requests: list[RiskRequest] = []
        closes: set[str] = set()
        # §9.4 dissent. Since 2026-09-02 it IS subtracted (the refusal reads
        # the signed score); this note records the split that produced the
        # score, so an order backed by 3-for/1-against evidence says so
        # rather than looking like a clean 2-source idea.
        dissent_notes: dict[str, str] = {}

        for target in targets:
            if target.risk_allocation_pct is None:
                # drop-reason: NOT a drop. A legacy notional target simply
                # gets no RiskPlan; the delta loop still sizes it the old
                # way and still builds its order.
                continue  # legacy notional target — sized the old way
            sym = target.symbol
            if target.risk_allocation_pct == 0.0:
                # A close needs no price, no stop and no budget. Routing it
                # through the pricing checks below would let a missing quote
                # silently cancel an exit PM had decided on.
                closes.add(sym)
                # 2026-09-12: the close IS handed to the allocator, as the
                # zero request `allocate_risk_budget` already documents
                # ("a zero request is PM closing the name. It consumes no
                # budget"). Before this, a full close never reached the
                # allocator at all, so the closed name's EXISTING risk kept
                # counting as committed and a same-session "close X, open
                # Y" plan had Y rationed against a book that still held X —
                # denied for "no room" the sale was about to create.
                # Measured: OLD at 24.8% of a 25% ceiling, NEW asking 2%:
                # without this, NEW granted 0.00% (below_floor); with it,
                # 2.00%. Partial trims never had this problem because they
                # are requests already. Whether the sale then FILLS is
                # ExecutionStage's question, answered there (it sells,
                # waits for terminal, and re-reads the account before any
                # BUY); a granted size on an unfilled close is the same
                # exposure a trim-then-add plan has always carried.
                requests.append(RiskRequest(sym, 0.0))
                # drop-reason: NOT a drop. This is PM asking to CLOSE the
                # name; it goes to the exit builder, not to nowhere.
                continue
            analysis = analyses_by_sym.get(sym)
            held_pct = current_weights.get(sym, 0.0)
            held_same_side = (
                held_pct < 0 if target.direction == "short" else held_pct > 0
            )
            if analysis is None and held_same_side:
                # A held name this session still has no Technical for (Tech
                # unresolved after retry, or a path that never asked). The
                # intraday scan now produces Technical for holds; this
                # branch is the remainder, not the product. There is
                # nothing to derive a new stop from, but the position
                # already HAS one at the broker: size the trim from that,
                # and never let it grow the position.
                entry, stop = self._held_trim_entry_and_stop(
                    target, price_map.get(sym), (live_stops or {}).get(sym.upper()),
                )
                if entry is None or stop is None:
                    # drop-reason: delegated — `_held_trim_entry_and_stop`
                    # files TRIM_REFUSAL_NO_USABLE_LIVE_STOP before returning
                    # (None, None). The position is left unchanged.
                    continue
                live_stop_trims.add(sym)
            else:
                entry, stop = self._resolve_entry_and_stop(
                    target, analysis, price_map.get(sym), regime=regime,
                )
            if entry is None or stop is None:
                # drop-reason: delegated. `_resolve_entry_and_stop` has
                # already filed the fault or the named refusal for this
                # symbol — every one of its own exits does.
                continue  # already logged; no stop means no honest size
            priced[sym] = (entry, stop)
            directions[sym] = target.direction

            # The single-name envelope binds before the portfolio one. A PM
            # asking for more than the ratified envelope is clamped rather
            # than refused — the idea is sound, the size is not.
            envelope_capped = min(target.risk_allocation_pct, self.cfg.risk_budget_pct)
            # docs/WORK.md item 13: this cap is always a plain multiplier —
            # the envelope can shrink a request, it never refuses one — so it
            # is never anything but SizeOverride.sized(). The refusal case
            # lives entirely in the agreement refusal below.
            envelope_override = SizeOverride.sized(envelope_capped)
            # §9.4: then the agreement refusal, computed from THIS session's
            # canonical registry (not from target.provenance — see
            # `count_aligned_sources`), before the request ever reaches the
            # portfolio-level budget allocator. Same "no view, don't invent
            # one" posture as `existing_risk_pct`/`clusters` above: when the
            # caller has no registry to offer, the ceiling is UNENFORCED
            # (infinite), never silently treated as zero agreement — a
            # missing registry is not evidence of disagreement.
            agreement_count: int | None = None
            opposing_count: int | None = None
            source_score: int | None = None
            if evidence_registry is not None:
                sources = evidence_registry.get(sym.upper(), {})
                # §9.4 freshness: a stance the caller has judged too old is
                # dropped from the TALLY only. It stays in `sources`, so it is
                # still coverage `validate_grounding` recognises — it can
                # only ever pull the net DOWN, never up. One gate, both sides: a
                # stance too stale to corroborate is too stale to dissent.
                ignored = (stale_sources or {}).get(sym.upper())
                agreement_count = count_aligned_sources(
                    sym, sources, target.direction, ignored_sources=ignored,
                )
                opposing_count = count_opposing_sources(
                    sym, sources, target.direction, ignored_sources=ignored,
                )
                # 2026-09-02: the ceiling reads the SIGNED score, not the
                # aligned count. Before this, a seat that stayed silent, a
                # seat that rated neutral and a seat that actively disagreed
                # all contributed the same zero, so the desk could fund a
                # name on "3 aligned" while one of its own analysts argued
                # the other way. Netting the dissent off is the whole change —
                # there is deliberately NO separate veto rule, because that
                # would charge the same dissenter twice.
                source_score = signed_source_score(
                    sym, sources, target.direction, ignored_sources=ignored,
                )
                # 2026-09-14: agreement is a REFUSAL, not a ceiling. Net at
                # or below zero drops the target; anything above it imposes
                # no size restriction of its own — the ratified per-trade
                # envelope and the budget allocator are the only bounds. See
                # `agreement_refuses_trade` for why the graduated ladder was
                # retired. `SizeOverride.no_trading()` carries the refusal
                # rather than a bare 0.0, which downstream could not tell
                # apart from an intentional zero-weight close (item 13).
                agreement_override = (
                    SizeOverride.no_trading()
                    if agreement_refuses_trade(source_score)
                    else SizeOverride.sized(float("inf"))
                )
                if ignored:
                    gated = sorted(s for s in ignored if s in sources)
                    if gated:
                        logger.info(
                            "Constructor: %s — %s stance(s) present but too "
                            "stale to count toward agreement (%d aligned "
                            "after the freshness gate)",
                            sym, ", ".join(gated), agreement_count,
                        )
                logger.info(
                    "Constructor: %s agreement %d aligned / %d opposed = "
                    "net %+d (direction=%s, %d source(s) with coverage)",
                    sym, agreement_count, opposing_count, source_score,
                    target.direction, len(sources),
                )
            else:
                # No registry to check dissent against — same "no view, don't
                # invent one" posture as everywhere else in this method: an
                # unbounded multiplier, never a refusal.
                agreement_override = SizeOverride.sized(float("inf"))
            # docs/WORK.md item 13 / the pysystemtrade override algebra: the
            # combined override is always the MORE RESTRICTIVE of the two —
            # `no_trading` absorbs a multiplier no matter how large, so this
            # can never be diluted back into "trade a bit". This is the exact
            # generalization of the 2026-09-02 signed-dissent workaround: that
            # patch dropped a target whose combined float landed at or below
            # zero; this makes "combined float at or below zero" and "the
            # combination IS a refusal" the same statement, structurally,
            # rather than two things a future caller could let drift apart.
            combined_override = envelope_override.combine(agreement_override)
            if combined_override.is_refusal:
                # Drop the target entirely rather than sizing it at zero — a
                # zero-weight RiskPlan reads to the delta loop as "PM wants
                # this position CLOSED", so letting a 0% request through would
                # turn a refusal to open into a forced liquidation of whatever
                # is already held. Refusing to BUY is not a decision to SELL.
                # The two dels are load-bearing, not tidiness: the sizing loop
                # below iterates `priced` and looks each symbol up in
                # `requests`, which this target never joins.
                #
                # `_note_refusal` files the CODE as data (board item 10,
                # 2026-09-14) instead of a `logger.warning` whose "produces
                # no order" phrasing `_DropReasonCapture._SYMBOL` does not
                # match — the same defect item 49 already fixed for the
                # portfolio-level budget allocator below, found again here by
                # running the regex against this message.
                self._note_refusal(
                    sym, target.direction, STOP_REFUSAL_AGREEMENT_NET,
                    # Board item 89 defect 4 — a rule cited by NUMBER whose
                    # text says something else. This read "§9.4 refuses a
                    # net at or below zero". `docs/QAMC_REMEDIATION_SPEC.md`
                    # §9.4 is titled "Conviction is agreement, not a
                    # technical rating" and is about agreement EARNING
                    # SIZE; it states no refusal at all, and the sizing
                    # half it does state was RETIRED on 2026-09-14 — which
                    # the sibling dissent note twenty lines below already
                    # says out loud, in the same function. So the owner was
                    # pointed at a section that contradicted the reason he
                    # was given for losing the trade.
                    #
                    # The rule is now STATED rather than cited. That is the
                    # real fix: a section number in owner-facing prose is a
                    # promise that a separate document still says a
                    # particular thing, and nothing in this repo checks
                    # that promise — so it can go stale again the moment
                    # the spec is renumbered or amended, silently and
                    # without touching this file. Nothing about the gate,
                    # the threshold or the decision changes here; only what
                    # the owner is told about it.
                    f"{agreement_count} aligned / {opposing_count} opposed = "
                    f"net {source_score:+d} independent source(s) for this "
                    f"{target.direction}. The desk does not open a position "
                    f"when the independent sources do not net out in favour "
                    f"of it, and here they do not. Any existing position is "
                    f"left untouched.",
                )
                del priced[sym]
                del directions[sym]
                continue
            # `.value` is safe here — `combined_override` is a `multiplier`
            # override by construction whenever it is not a refusal (the only
            # other kinds this method ever produces are `no_trading`, handled
            # above, so nothing else reaches this line).
            requested_pct = combined_override.value
            if opposing_count:
                # Carried into the order's reasoning (see `RiskPlan.note`) so
                # the AI Risk Manager reads it and it lands in the persisted
                # proposed_order evidence. The SPLIT, not just the net: an
                # idea backed 3-for/1-against is a different idea from one
                # with a flat 2 aligned and no dissent, and the note is the
                # only place that survives.
                dissent_notes[sym] = (
                    f"[constructor: {sym} — {opposing_count} independent "
                    f"source(s) took the OPPOSITE side of this "
                    f"{target.direction} ({agreement_count} aligned, net "
                    f"{source_score:+d}). §9.4 nets the SIGNED sum since "
                    "2026-09-02; the net stayed above zero, so the trade was "
                    "not refused. Agreement no longer sizes anything "
                    "(retired 2026-09-14) — read this as evidence quality]"
                )
            requests.append(RiskRequest(sym, requested_pct))

        # `clusters` alone is not enough to run the allocator: without
        # `existing_risk_pct` the held book's risk is invisible, and
        # `allocate_risk_budget` treats a missing map as `{}` — i.e. as a
        # book carrying ZERO existing risk — rather than as "unknown". Ceilings
        # computed against a book presumed empty are computed against the
        # wrong number, not a smaller one, so a partial failure (heat missing,
        # clusters present) must degrade the SAME way as a total one: ceilings
        # unenforced, per-position sizing still applies. See
        # `_book_risk_inputs` in `src/pipeline_stages.py`.
        allocation = allocate_risk_budget(
            requests,
            existing_pct=existing_risk_pct,
            clusters=clusters,
            ceiling_pct=self.cfg.max_portfolio_risk_pct,
            cluster_share_pct=self.cfg.max_cluster_risk_share_pct,
            floor_pct=self.cfg.min_risk_pct,
            # retired board item 49 — best-ranked first. `ranking` is the PM's
            # own `rank_verdicts` order, threaded through unchanged; the
            # allocator scores nothing and this module scores nothing.
            priority=ranking,
        ) if existing_risk_pct is not None else None

        plans: dict[str, RiskPlan] = {}
        for sym in closes:
            plans[sym] = RiskPlan(
                symbol=sym, risk_pct=0.0, target_weight_pct=0.0,
                entry_price=None, stop_price=None, note="",
            )

        for sym, (entry, stop) in priced.items():
            requested = min(
                next(r.requested_pct for r in requests if r.symbol == sym),
                self.cfg.risk_budget_pct,
            )
            note_parts = []
            if sym in dissent_notes:
                note_parts.append(dissent_notes[sym])
            if allocation is not None:
                grant = allocation.grants.get(sym.upper())
                granted = grant.granted_pct if grant else 0.0
                if grant and grant.note:
                    note_parts.append(grant.note)
                if granted <= 0:
                    # retired board item 49 (`docs/INCIDENT_HISTORY.md`, 2026-09-14). This used to be a
                    # bare `logger.info` whose wording the drop-reason
                    # capture's regex does not match, so a budget-rationed
                    # name reached the database as a generic
                    # `constructor_dropped` with "no matching constructor log
                    # line captured". `_note_refusal` files the CODE as data
                    # instead, per symbol, exactly like every other named
                    # constructor refusal.
                    #
                    # The target is DROPPED, not zeroed. A 0% risk target is
                    # read downstream as "sell it"; refusing to open a
                    # position is not a decision to close one, and this
                    # `continue` leaves `plans[sym]` absent so the delta loop
                    # skips the symbol entirely. `closes` is populated on a
                    # different path (an explicit PM 0.0 request) and is
                    # untouched here.
                    limited_by = grant.limited_by if grant else "no grant"
                    self._note_refusal(
                        sym, directions.get(sym, ""),
                        STOP_REFUSAL_BUDGET_EXHAUSTED,
                        f"the portfolio risk budget granted 0.00% of the "
                        f"{requested:.2f}% risk this idea asked for "
                        f"({limited_by}). Better-ranked candidates took the "
                        f"{self.cfg.max_portfolio_risk_pct:.2f}% ceiling "
                        f"first (owner decision 2026-09-12, retired board "
                        f"item 49). Nothing is wrong with the idea — it "
                        f"passed every gate and lost only the queue.",
                    )
                    continue
            else:
                granted = requested
            note = " ".join(note_parts)

            # risk_pct x entry / risk_per_share: the §2.1 formula as a
            # weight. risk_per_share is UNSIGNED — `entry - stop` is
            # negative for a short (whose stop sits ABOVE entry), so a bare
            # `entry - stop` would corrupt the weight's sign; `abs()` keeps
            # this an unsigned magnitude exactly like the long case (D4).
            risk_per_share = abs(entry - stop)
            if directions.get(sym) == "short":
                # D8: gap-risk sizing haircut — SIZING ONLY, never applied
                # to the stop placed above (already resolved). A short gaps
                # through its stop with no bound, so the same nominal risk
                # allocation must open a SMALLER short than an equivalent
                # long at the same stop distance.
                risk_per_share *= self.cfg.short_gap_risk_multiple
            raw_weight = granted * entry / risk_per_share
            plans[sym] = RiskPlan(
                symbol=sym,
                risk_pct=granted,
                # Stored in GROSS-leverage terms, the units _current_weights
                # and the delta loop speak. _build_buy divides back out.
                target_weight_pct=raw_weight * _gross_multiplier(sym),
                entry_price=entry,
                stop_price=stop,
                note=note,
                sized_from_live_stop=sym in live_stop_trims,
            )
        return plans

    def _held_trim_entry_and_stop(
        self, target: TargetPosition, market_price: float | None,
        live_stop: float | None,
    ) -> tuple[float | None, float | None]:
        """(current price, live broker stop) for a trim of an unanalysed
        holding, or (None, None) after filing a named refusal.

        §2.1's own formula, applied to the position as it stands: shares to
        keep = equity x target risk / |price - live stop|. The stop must sit on
        the losing side of the price (below for a long, above for a short) —
        otherwise it bounds no loss and cannot size anything.
        """
        import math as _math
        sym = target.symbol
        is_short = target.direction == "short"
        action = "COVER" if is_short else "SELL"
        price = float(market_price) if market_price else 0.0
        stop = float(live_stop) if live_stop else 0.0
        usable = (
            _math.isfinite(price) and _math.isfinite(stop) and price > 0
            and stop > 0 and (stop > price if is_short else stop < price)
        )
        if usable:
            return (price, stop)
        if not stop:
            why = "it has no live stop order at the broker"
        elif not price:
            why = "there is no current price for it"
        else:
            why = (
                f"its live stop (${stop:,.2f}) is not "
                f"{'above' if is_short else 'below'} the current price "
                f"(${price:,.2f}), so it bounds no loss"
            )
        self._note_refusal(
            sym, target.direction, TRIM_REFUSAL_NO_USABLE_LIVE_STOP,
            f"the PM asked to trim {sym} to {target.risk_allocation_pct:.2f}% "
            f"risk. {sym} was not analysed this session, so the trim can only "
            f"be sized from the position's own stop, and {why}. The position "
            f"is left unchanged. This is not a market data fault.",
            action=action,
        )
        # drop-reason: filed just above (TRIM_REFUSAL_NO_USABLE_LIVE_STOP).
        return (None, None)

    def _derive_target(
        self,
        symbol: str,
        analysis: TechAnalysisResult | None,
        entry_price: float,
        direction: str,
    ) -> TargetDerivation:
        """Compute the take-profit from structure, or refuse by name.

        This replaces reading `analysis.reference_target` as the trade's
        target. The model's number is still passed in — as `model_target`,
        which the derivation never uses to choose an answer and only carries
        so the disagreement can be logged. See
        `src/data/levels.py::derive_structural_target`.

        Deterministic and cheap, so it is called from both
        `_resolve_entry_and_stop` (which needs it for the reward:risk check
        after widening) and the builders (which need the number itself)
        rather than being threaded through as state. Same inputs, same
        answer, both times.
        """
        if analysis is None:
            # The desk holds no technical analysis for this symbol at all.
            # Every derivation input is absent at once, so this is named
            # for what it is rather than for the first missing field.
            detail = (
                "DATA FAULT: no technical analysis exists for this symbol "
                "this session — nothing to measure a target or a stop from"
            )
            self._note_data_fault(symbol, direction, FAULT_NO_ANALYSIS, detail)
            return TargetDerivation(price=None, fault=FAULT_NO_ANALYSIS, detail=detail)
        derivation = derive_structural_target(
            entry_price=entry_price,
            direction=direction,
            levels=getattr(analysis, "computed_levels", None) or [],
            atr=getattr(analysis, "atr_14", None),
            horizon_sessions=getattr(analysis, "expected_horizon_sessions", None),
            setup_type=getattr(analysis, "setup_type", None),
            model_target=getattr(analysis, "reference_target", None),
            min_target_atr_multiple=self.cfg.min_target_atr_multiple,
            breakout_projection_atr_multiple=self.cfg.breakout_projection_atr_multiple,
            max_reach_atr_multiple=self.cfg.max_target_reach_atr_multiple,
            max_horizon_sessions=self.cfg.max_target_horizon_sessions,
            # What the bar history behind `computed_levels` was, so an
            # empty list from a dead feed is a DATA fault and one from a
            # measured, structureless chart is a refusal. `getattr` with
            # the unknown default because older rows and hand-built
            # analyses (backtest shim, tests) predate the field.
            levels_coverage=getattr(analysis, "levels_coverage", None) or COVERAGE_UNKNOWN,
        )
        if derivation.fault:
            # Recorded here, at the single funnel every derivation passes
            # through, so the eligibility preview and order construction
            # cannot disagree about what was unmeasurable.
            self._note_data_fault(
                symbol, direction, derivation.fault, derivation.detail,
            )
        self._log_target_divergence(symbol, derivation)
        return derivation

    def _log_target_divergence(
        self, symbol: str, derivation: TargetDerivation,
    ) -> None:
        """Record where the model's guess and the computed level disagree.

        The model's target is no longer arithmetic, but it is still the only
        read available on whether the model's chart-reading is worth
        anything. A large, one-directional gap across many symbols is a
        finding about the seat; a large gap on one symbol is a finding about
        that symbol.
        """
        if derivation.price is None or derivation.model_target is None:
            return
        gap = derivation.divergence_pct
        if gap is None:
            return
        message = (
            "Constructor: %s target — computed $%.2f (%s) vs analyst's "
            "reference_target $%.2f: %+.1f%%"
        )
        args = (
            symbol, derivation.price, derivation.basis,
            derivation.model_target, gap,
        )
        if abs(gap) >= self.cfg.target_divergence_warn_pct:
            logger.warning(message + " — the model and the chart disagree sharply", *args)
        else:
            logger.info(message, *args)

    @staticmethod
    def _target_note(derivation: TargetDerivation) -> str:
        """Provenance for the order's reasoning, appended after truncation.

        The AI Risk Manager reads `reasoning`. It must be able to see that
        the take-profit is a computed level rather than the analyst's number,
        and where the two differ — otherwise it re-does the comparison in its
        head, which is the class of error that produced two contradictory
        reward:risk figures in one response on 2026-08-31.
        """
        if derivation.price is None:
            return ""
        note = f" [target ${derivation.price:,.2f} — {derivation.basis}]"
        if derivation.model_target is not None and derivation.divergence_pct is not None:
            note = (
                f" [target ${derivation.price:,.2f} computed from "
                f"{derivation.basis.replace('_', ' ')}; analyst's reference "
                f"${derivation.model_target:,.2f}, "
                f"{derivation.divergence_pct:+.1f}%]"
            )
        return note

    def _resolve_entry_and_stop(
        self,
        target: TargetPosition,
        analysis: TechAnalysisResult | None,
        market_price: float | None,
        regime: str | None = None,
    ) -> tuple[float | None, float | None]:
        """Entry and a validated stop, or (None, None).

        Direction-aware (D4, Stage 3): for a long the stop must sit strictly
        BELOW entry (existing behaviour, unchanged); for a short it must sit
        strictly ABOVE entry — a short's stop protects against the price
        RISING, so `stop_loss <= entry_price` is rejected instead of
        `stop_loss >= entry_price`.

        Extracted from `_build_buy` because risk-based sizing needs the stop
        distance one step earlier — the position's weight is not knowable until
        the stop is. `_build_buy`/`_build_short` call this too, so there is
        exactly one definition of what a tradeable entry/stop pair is.
        """
        is_short = target.direction == "short"

        # Item 120: a new name the caller could not price to a fresh today
        # print is refused HERE, before the TA-entry fallback below can size
        # it off a stale analyst number. The freshness resolver
        # (`src.data.live_price.resolve_live_price`) already rejected a quote
        # mid and a prior-session trade for this name — it returns a real
        # print or today's forming session bar, never a quote mid — so an
        # entry here means no usable today price of any kind. Falling back to
        # the TA entry would reintroduce exactly the bad-price sizing the
        # resolver exists to prevent. Filed under its own SIZING fault code
        # (FAULT_NO_PRICE / FAULT_STALE_PRICE, carried from the caller) so it
        # routes through the existing unmeasurable-drop path
        # (`_record_constructor_drops` / `_alert_unmeasurable_symbols`) and
        # is never counted as a trade the desk judged.
        fault_code = self._unpriceable_symbols.get(target.symbol.strip().upper())
        if fault_code is not None:
            self._note_data_fault(
                target.symbol, target.direction, fault_code,
                "DATA FAULT: no fresh today print to size this new name this "
                "session (a quote mid or a prior-session price is not a "
                "sizing reference) — the buy cannot be sized without "
                "inventing a price, so the name is refused as unmeasurable "
                "rather than mis-sized",
            )
            return (None, None)

        entry_price = 0.0
        if market_price and market_price > 0:
            entry_price = float(market_price)
        elif analysis and analysis.entry_price:
            entry_price = float(analysis.entry_price)
            logger.info(
                "Constructor: no live market_price for %s, using TA entry $%.2f",
                target.symbol, entry_price,
            )
        if entry_price <= 0:
            # A listed instrument always has a price. No live quote AND no
            # analyst entry is a data fault on this desk's side, the same
            # class `derive_structural_target` would name one line later
            # (FAULT_NO_ENTRY) — recorded and alerted as such, never as a
            # trade the constructor judged. Still no trade.
            self._note_data_fault(
                target.symbol, target.direction, FAULT_NO_ENTRY,
                "DATA FAULT: no live market price and no analyst entry "
                "price — the symbol cannot be priced, let alone measured",
            )
            return (None, None)

        # Round FIRST, then validate: the TradeDecision ships
        # round(stop_loss, 2), so validating the unrounded value let a stop
        # that rounds UP to exactly the entry price through the
        # `stop_loss < entry_price` check (e.g. entry $10.00, stop $9.999 →
        # ships $10.00 == entry → risk_per_share = 0, and a stop at the entry
        # fires on the first tick down). 2026-07-16 audit.
        # Too young to measure (item 54, 2026-09-12). Checked FIRST, before
        # the target derivation: a listing with too few sessions usually
        # also yields no levels, and "insufficient history" is the true
        # name for that, not `no_structural_levels` (which PR #326 reads
        # as a feed fault).
        if not self._require_sufficient_history(
            target.symbol, analysis, target.direction,
        ):
            # drop-reason: delegated — `_require_sufficient_history` files
            # STOP_REFUSAL_INSUFFICIENT_HISTORY before returning False.
            return (None, None)

        # The target is derived BEFORE the stop is finalised, because the
        # reward:risk check inside `_widen_stop_past_noise` needs a real
        # target to measure against. It depends only on entry, direction and
        # the chart — never on the stop — so there is no circularity.
        derivation = self._derive_target(
            target.symbol, analysis, entry_price, target.direction,
        )
        if derivation.price is None:
            # A data fault has already been recorded and logged as
            # UNMEASURABLE by `_derive_target`; only a genuine refusal is
            # logged here as a rejection. Both fail closed.
            if not derivation.fault:
                # Board item 10 (2026-09-14, second pass): this used to be a
                # bare `logger.warning`. It DOES match the capture's regex,
                # so the drop was never invisible — but it reached the
                # database as a generic `constructor_dropped` row carrying a
                # sentence, indistinguishable by `reason` from six other
                # causes. `derivation.refusal` is already a machine code
                # (`src/data/levels.py`), so file that one rather than mint
                # a synonym for it.
                self._note_refusal(
                    target.symbol, target.direction,
                    derivation.refusal or STOP_REFUSAL_NO_STRUCTURAL_TARGET,
                    f"no take-profit could be computed from structure at the "
                    f"${entry_price:,.2f} entry: {derivation.detail}",
                )
            return (None, None)

        stop_loss = self._resolve_stop(target, analysis, entry_price)
        stop_loss = self._widen_stop_past_noise(
            target.symbol, analysis, entry_price, stop_loss, regime=regime,
            direction=target.direction, target_price=derivation.price,
            # The PM's sub-floor catalyst gate has already run by the time a
            # target reaches here; this is where its verdict is honoured
            # rather than silently re-litigated. `getattr` because
            # `_resolve_entry_and_stop` is also called with hand-built
            # targets from the backtest shim and older tests.
            subfloor_catalyst_exception=bool(
                getattr(target, "subfloor_catalyst_verified", False)
            ),
            # The MEASURED half of the trend-trade test (2026-09-11, funnel
            # item 6 + item 1(d)): `_derive_target` has just run the desk's
            # own level computation for this exact trade, and
            # `level_used is None` means it found nothing in the trade's
            # direction — a computed absence of a ceiling, not a label.
            # `src.risk.constants.is_trend_trade` is the one definition both
            # this and `derive_structural_target` consume.
            structural_ceiling=(derivation.level_used is not None),
        )
        if stop_loss is not None:
            stop_loss = round(stop_loss, 2)
        if is_short:
            invalid = stop_loss is None or stop_loss <= 0 or stop_loss <= entry_price
        else:
            invalid = stop_loss is None or stop_loss <= 0 or stop_loss >= entry_price
        if invalid:
            # Board item 10 (2026-09-14, second pass). THE BACKSTOP. Every
            # named stop refusal in `_widen_stop_past_noise` arrives here as
            # a plain `None`, so this line is the last thing many drops say
            # — and it said it only in prose. Filed as a code now, but
            # `only_if_unrecorded` so the precise upstream reason (wrong
            # side, wider than reach, no volatility reading, ...) is never
            # overwritten by this generic one. What reaches here with
            # nothing already recorded is a stop that is genuinely just on
            # the wrong side of, or equal to, the entry.
            self._note_refusal(
                target.symbol, target.direction, STOP_REFUSAL_NO_VALID_STOP,
                f"no valid stop {'above' if is_short else 'below'} the "
                f"${entry_price:,.2f} entry (stop={stop_loss}). A stop that "
                f"does not sit on the protective side of the entry protects "
                f"nothing, so the trade is refused rather than shipped.",
                only_if_unrecorded=True,
            )
            logger.warning(
                "Constructor: %s %s rejected — no valid stop %s entry "
                "(entry=$%.2f, stop=%s)",
                "SHORT" if is_short else "BUY", target.symbol,
                "above" if is_short else "below", entry_price, stop_loss,
            )
            return (None, None)
        return (entry_price, stop_loss)

    def _stop_atr_multiple(
        self, analysis: TechAnalysisResult | None, regime: str | None,
    ) -> float:
        """How many ATRs of room THIS trade deserves, not a global constant.

        ATR already scales the distance to the stock and the session. This
        scales how many of them the setup earns: a range trade reverts inside
        a defined band and does not need breakout room, and a risk-off tape
        swings wider for the same ATR reading than a trending one does.

        Reachable output is [2.1375, 3.00] ATR — narrowest is a range setup on
        a risk-on tape (2.5 x 0.90 x 0.95), widest a breakout on a risk-off
        one (2.5 x 1.00 x 1.20). (This docstring still read [1.2825, 1.80]
        off the old 1.5 base until 2026-09-17; the base moved to 2.5 on
        2026-09-10.) Both ends are pinned to real measurements;
        see `ConstructorConfig.stop_atr_setup_scale` for the derivation.
        """
        multiple = self.cfg.min_stop_atr_multiple
        setup = (getattr(analysis, "setup_type", None) or "").strip().lower()
        for key, scale in self.cfg.stop_atr_setup_scale:
            if setup == key:
                multiple *= scale
                break
        tape = (regime or "").strip().lower()
        for key, scale in self.cfg.stop_atr_regime_scale:
            if tape == key:
                multiple *= scale
                break
        return multiple

    def _level_backing_stop(
        self,
        analysis: TechAnalysisResult | None,
        entry_price: float,
        stop_loss: float,
        is_short: bool,
    ) -> float | None:
        """The computed structural level this stop sits at, or None.

        Takes no ATR. It used to, because the match tolerance was an ATR
        multiple; since 2026-09-13 (docs/WORK.md item 46) "at this level"
        is decided against the level's own zone, whose width is a
        percentage of price, so the name's volatility is not an input to
        this question at all. It is still very much an input to whether
        the resulting stop is honoured — see `_widen_stop_past_noise`.

        Spec §12.1. "Verified" means the price came out of
        `src/data/levels.py::find_structural_levels` and was attached to the
        analysis IN PYTHON (`TechAnalysisResult.computed_levels`, set by
        `TechAnalystAgent` after parsing and never emitted by the model). A
        model asserting a level in `support_levels` / `resistance_levels`
        earns nothing here — if it could, it would only have to name a level
        beside its stop to buy itself an exemption from the noise floor,
        and the verification would be worthless.

        Side is judged against THIS ENTRY, not against the last close.
        `find_structural_levels` labels a level support or resistance
        relative to the last close, and its own docstring says a ceiling
        price has broken through becomes a floor. The trade is entered at a
        live price that may sit on the other side of a level, so what
        matters is only whether the level is below entry (it can hold a
        long up) or above it (it can cap a short). This is the same
        re-partition `derive_structural_target` performs for the target,
        for the same reason — see the `levels` note in its docstring.

        Returns the CLOSEST matching level so the log names the one the
        stop is actually sitting on. A level with fewer than
        `risk.min_level_touches_for_stop_honor` prior touches is skipped
        entirely here — it is real enough to register in `computed_levels`
        and to anchor a target, but not (per docs/RESEARCH_FINDINGS.md §7's
        measured table) trusted enough to earn the tight-stop exemption. A
        stop resting on it falls through to the ATR floors below, same as a
        stop with nothing computed under it at all.
        """
        raw_levels = getattr(analysis, "computed_levels", None) or []
        touches_by_price = getattr(analysis, "computed_level_touches", None) or {}
        min_touches = self.cfg.min_level_touches_for_stop_honor

        best: float | None = None
        best_gap = float("inf")
        for raw in raw_levels:
            try:
                price = float(raw)
            except (TypeError, ValueError):
                continue
            if not math.isfinite(price) or price <= 0:
                continue
            # A long is held up by structure at or below its entry; a short
            # is capped by structure at or above its entry. A "support"
            # above a long's entry cannot be what its stop is resting on.
            if is_short and price < entry_price:
                continue
            if not is_short and price > entry_price:
                continue
            touches = touches_by_price.get(price)
            if touches is None or touches < min_touches:
                # Unverified touch count (missing map entry, e.g. an older
                # caller/fixture that never set it) is treated the same as
                # "below the bar" — fail closed, per Invariant 2, rather than
                # honour a tight stop we cannot show cleared the bar.
                continue
            # "At" this level means INSIDE this level's own zone. The bound
            # is read per-level off `CLUSTER_TOLERANCE_PCT`, the same
            # constant `find_structural_levels` used to build the zone in the
            # first place, so the tolerance is exactly as wide as the thing
            # it is matching against — never narrower, never a second number
            # that can drift. docs/WORK.md item 46.
            tolerance = level_zone_halfwidth(price)
            gap = abs(stop_loss - price)
            if gap <= tolerance and gap < best_gap:
                best, best_gap = price, gap
        return best

    def _reward_risk_at(
        self,
        entry_price: float,
        stop_price: float,
        target_price: float | None,
        is_short: bool,
    ) -> float | None:
        """Reward:risk measured against the stop that will actually ship.

        A thin alias for `models.reward_to_risk` — the ONE definition of
        this ratio in the codebase, shared with
        `TechAnalysisResult.risk_reward`, `TradeDecision.reward_risk` and
        the execution-time re-check in `src/pipeline_stages.py`. It used to
        be a fourth private copy, and the copies disagreed in ways that
        rejected real trades (see that function's docstring for the XLE
        1.67-vs-1.18 rejection).

        None means "this is not a measurable entry geometry", including
        every non-finite input. **A caller that had a target and got None
        back must refuse, not permit** — a NaN makes every `ratio < floor`
        comparison False, so treating None as "no opinion" there would wave
        a malformed trade straight through the floor.
        """
        return reward_to_risk(
            entry_price, stop_price, target_price, is_short=is_short,
        )

    def real_reward_risk_preview(
        self,
        analysis: TechAnalysisResult | None,
        direction: str,
        regime: str | None = None,
    ) -> float | None:
        """The reward:risk this candidate would actually clear at
        construction time — same target derivation and stop-widening
        `construct_orders` applies — computed BEFORE a `TargetPosition`
        exists.

        Exists to close a gap found in a 2026-09-04 audit: the 2026-09-01
        decision that reward:risk must be "evidence, never arithmetic"
        (this class's own `_derive_target` / `_widen_stop_past_noise`) was
        applied to order construction but never reached
        `PortfolioManagerAgent.candidate_eligibility` /
        `_apply_subfloor_catalyst_rule` — the EARLIER gate that decides
        which candidates even reach construction. That gate kept reading
        `TechAnalysisResult.risk_reward`, which is real arithmetic but over
        the ANALYST's own guessed target, never checked against structure
        (see that field's docstring). On a measured real day the two gates
        passed disjoint sets — zero overlap — because the first gate
        screened out exactly the names the second would have allowed
        through, and vice versa. This method gives the earlier gate the
        SAME real number the later one uses, by calling the same
        `_derive_target` / `_widen_stop_past_noise` this class already
        runs, rather than a second copy of that logic.

        Two inputs are necessarily earlier-stage approximations, because
        nothing later exists yet at PM eligibility time:
          - entry is the analyst's SNAPSHOT `entry_price`, not the live
            market price `construct_orders` prices the real order at —
            that price does not exist until the PM has decided to trade
            the name.
          - the stop is `analysis.stop_loss`, mirroring `_resolve_stop`'s
            second-priority source. There is no `TargetPosition.
            suggested_stop_price` yet, because the PM has not proposed one.
        Both are re-resolved for real at construction time against the
        live price and (if the PM supplies one) its own suggested stop, so
        a name can still legitimately move between preview and shipped
        order — but it will no longer move because of TWO DIFFERENT
        DEFINITIONS of reward:risk, which is the defect this closes: the
        derived target and the noise-floor stop widening were never
        applied before the PM's gate at all.

        None means "cannot judge" — the same fail-closed contract as
        `_widen_stop_past_noise`, which this calls. As of 2026-09-11
        (docs/WORK.md item 1(d)) it no longer ALSO means "under this desk's
        floor": a real but weak range payoff returns its real number, and a
        breakout returns its number without any floor having been consulted.
        Callers must not read a returned number as "eligible" or a None as
        "sub-floor" — see `PortfolioManagerAgent.candidate_eligibility`.
        """
        if analysis is None:
            return None
        entry_price = getattr(analysis, "entry_price", None)
        if not entry_price or entry_price <= 0:
            return None
        entry_price = float(entry_price)
        is_short = direction == "short"

        # Too young to measure — same check, same place in the funnel, as
        # `_resolve_entry_and_stop`, so the PM is not shown a candidate the
        # constructor would refuse one stage later.
        if not self._require_sufficient_history(
            analysis.symbol, analysis, direction,
        ):
            return None

        derivation = self._derive_target(
            analysis.symbol, analysis, entry_price, direction,
        )
        if derivation.price is None:
            return None

        raw_stop = getattr(analysis, "stop_loss", None)
        try:
            raw_stop = float(raw_stop) if raw_stop else None
        except (TypeError, ValueError):
            raw_stop = None
        if raw_stop is not None and raw_stop <= 0:
            raw_stop = None
        # A missing stop is no longer a None here: `_widen_stop_past_noise`
        # reads one from the instrument (item 54) exactly as it will at
        # construction time. A stop on the WRONG side is still refused there.
        if raw_stop is not None:
            # Geometry must already hold before any widening is attempted —
            # a stop on the wrong side of entry is not a candidate for the
            # noise floor, it is not a stop at all.
            if is_short:
                if raw_stop <= entry_price:
                    return None
            elif raw_stop >= entry_price:
                return None

        honoured_stop = self._widen_stop_past_noise(
            analysis.symbol, analysis, entry_price, raw_stop, regime=regime,
            direction=direction, target_price=derivation.price,
            structural_ceiling=(derivation.level_used is not None),
        )
        if honoured_stop is None:
            # Ungeometric, or (Type A only) an unmeasurable ratio against the
            # real target and the stop that would actually ship —
            # `_widen_stop_past_noise` fails closed on both. As of item 1(d)
            # it no longer returns None merely for a sub-floor ratio, so a
            # weak-but-real range payoff now yields a NUMBER here (which the
            # ranking consumes) rather than a None the gate reads as
            # "ineligible".
            return None
        ratio = self._reward_risk_at(
            entry_price, honoured_stop, derivation.price, is_short,
        )
        return None if ratio is None else round(ratio, 2)

    def _widen_stop_past_noise(
        self,
        symbol: str,
        analysis: TechAnalysisResult | None,
        entry_price: float,
        stop_loss: float | None,
        regime: str | None = None,
        direction: str = "long",
        target_price: float | None = None,
        subfloor_catalyst_exception: bool = False,
        structural_ceiling: bool | None = None,
    ) -> float | None:
        """Decide the stop that will actually ship, and say which rule did it.

        Spec §12.1 (2026-09-01). **A stop sitting at a level the system
        COMPUTED is honoured however tight; the ATR band applies only when
        nothing computed backs it.**

        What this replaced, and why. `min_stop_atr_multiple` used to
        overwrite the structural stop unconditionally whenever the level sat
        closer to entry than the band. After that the stop was not at
        anything real — and `min_reward_risk_after_widening` was then judged
        against that fabricated number, so the reward was being divided by a
        risk nobody would ever take. The arithmetic is unforgiving: over a
        ~15-session hold a stock travels ~3.9 ATR, so against a 3.0-ATR stop
        the best achievable ratio is ~1.29 against a 1.5 floor. Essentially
        no trade could pass, and on 2026-09-01 the desk reviewed 38 qualified
        signals and placed ZERO trades. Tuning the multiple down to 2.0 was
        considered and explicitly REJECTED by the owner: it keeps a floor
        that still cannot tell a level-backed tight stop from an arbitrary
        one. Neither threshold moved. What changed is WHICH stop the
        arithmetic is performed on.

        "Verified" is doing real work here — see `_level_backing_stop`. The
        level must have come out of `find_structural_levels` and been
        attached in Python. A stop the analyst merely placed close, with
        nothing computed under it, still gets the full band.

        **The 1x ATR floor below is an addition beyond the ratified §12.1
        wording, and it is deliberate.** §12.1 argues the exemption is safe
        because `config/prompts/tech_analyst.md` already forbids a stop
        inside 1*ATR of entry. That is a PROMPT — an instruction to a
        language model — and Invariant 2 of the spec requires deterministic
        Python protections to be the final authority and to fail closed. A
        prompt is not a deterministic guarantee. Meanwhile a genuine support
        level sitting 0.2 ATR under entry is real structure AND a guaranteed
        whipsaw; both things are true at once. So a level-backed stop is
        honoured however tight DOWN TO `absolute_min_stop_atr_multiple`
        ATRs, and inside that it is widened to exactly that floor — never to
        the full `min_stop_atr_multiple` band, which is the behaviour being
        removed.

        For an unbacked stop nothing has changed: it is pushed out to
        `min_stop_atr_multiple` ATRs, exactly as before. That corrects the
        case the evidence says was routine — stops a median 1.7 ATRs from
        entry, a coin flip on a normal day's range rather than a thesis
        invalidation, which then forced enormous positions to reach any
        meaningful risk. This never invents a stop where none exists, and
        never pulls a wide stop tighter.

        D5 (Stage 3): direction-aware. A long's stop is pushed DOWN, away
        from entry; a short's stop is pushed UP, away from entry, by the
        same number of ATRs — mirrored, not reflected through a different
        rule. A computed reward:risk on a widened short is ranking
        information, not a refusal, exactly as for a long.

        **2026-09-11, docs/WORK.md item 1 part (d) — this function no longer
        REFUSES anything on reward:risk.** Owner decision, by setup type:

          * **Type B / trend** — no reward:risk comparison runs at all.
            Nothing overhead is expected to stop the stock; the position is
            managed by a trailing stop with no fixed target
            (`src/risk/trailing.py`), so the only number available to put in
            the numerator is invented to make the ratio computable. Decided
            by `src.risk.constants.is_trend_trade`, which accepts EITHER the
            analyst's `setup_type="breakout"` label OR
            `structural_ceiling=False` — the desk's own level computation
            having found nothing in this trade's direction. The caller
            supplies that measured fact from the same `_derive_target`
            derivation it already ran, so this exemption and
            `derive_structural_target`'s measured-move projection turn on one
            shared definition rather than two that can drift.
          * **Type A / range** — the ratio is still computed from this
            trade's own real support (the shipping stop) and real resistance
            (`_derive_target`). A computed result is never a refusal and is
            never a size-cap (owner 2026-09-17). It reaches ranking as a
            real ordering signal (`real_reward_risk_preview` ->
            `src/verdicts.py::rank_verdicts`).

        What still refuses, unchanged: every RISK-side check above — a
        wrong-side stop, a non-finite stop or entry. An UNMEASURABLE ratio
        is recorded, not refused (owner 2026-09-17 overnight bind: honesty
        about a payoff without a number is a ranking hint, not a gate).
        `subfloor_catalyst_exception` is inert theater around a dead floor
        and does not admit, refuse, or resize anything.

        `target_price` (2026-09-01) is the DERIVED target — computed from
        structure by `_derive_target`. It has to be passed in rather than
        read off the analysis, because the whole point of the change is that
        `analysis.reference_target` is the language model's guess and this
        gate is arithmetic. When it is omitted the old read is kept, for the
        one caller that legitimately supplies its own structural target: the
        backtest engine, which computes the nearest level itself and hands it
        over on a shim (`src/backtest/engine.py`). That path was never
        exposed to the defect.

        **2026-09-12, docs/WORK.md item 54 — the width gate, and a stop
        that is always derivable.** Sourced research (Bulkowski on gaps,
        George & Hwang 2004 on nearness to the 52-week high, Kullamägi's
        own stop discipline) falsified the one-day "no floor, no trade"
        rule: what published practice constrains is the stop's WIDTH, not
        whether a level exists under it. So: (1) an unbacked stop is placed
        at the wider of the noise band and the signal bar's far edge, both
        read from the instrument; (2) a missing stop is placed the same way
        rather than refused; (3) whatever placed the stop, a width past the
        instrument's own reach over the trade's horizon (`horizon_reach`)
        is REFUSED by code (`STOP_REFUSAL_WIDER_THAN_REACH`) — the only
        place this function refuses on distance. Under the cap, width is
        answered by `_plan_risk_targets` sizing down (§2.1), never by
        refusal; `STOP_RULE_OUTSIDE_BAND` still keeps a wide typed stop.

        **2026-09-02 — the reward:risk floor now runs on EVERY path, not
        only when this function moved the stop.** It previously lived
        inside the two widening branches, behind two unnamed early returns
        ("already outside the noise band", "no ATR reading"). A stop that
        was already wide enough therefore reached the broker with NO
        deterministic reward:risk check at all, and the name
        `min_reward_risk_after_widening` recorded that as if it were the
        design. It was not caught because it fails OPEN and quietly.

        Measured against the pre-reset production database
        (`data/resets/20260902T181859Z`), 2026-08-18 to 2026-09-02:
        **14 of the 49 constructed entry orders — 29% — shipped with a
        reward:risk below the 1.5 floor**, as low as 0.43 (XLB,
        2026-09-02) and 0.50 (NET, 2026-08-27). The floor's own refusal
        message appears ZERO times in the whole production log history
        before 2026-09-02. Two of the 14 reached the broker; XLE on
        2026-08-21 FILLED, 9 shares at $64.26, on a reward:risk of 0.81.
        Everything else was stopped downstream by the Risk Manager — a
        language model — or by the 1.2 execution-time belt in
        `src/pipeline_stages.py`. A deterministic floor was being enforced
        by an LLM's prose, which is exactly the inversion Invariant 2
        forbids.

        So the ratio is now measured against the stop that will ship,
        whichever rule placed it, and refused by a code naming that rule.
        The threshold did not move and no path became more permissive.
        """
        # Non-finite guards come FIRST, and they REFUSE rather than pass the
        # value through. `nan <= 0` is False, so a NaN price sails through
        # every ordering comparison in this function AND through the
        # caller's `stop_loss >= entry_price` validity check in
        # `_resolve_entry_and_stop` — a NaN stop was reaching
        # `TradeDecision` intact. The same NaN then makes `ratio < floor`
        # False at the gate below. Every one of those failures is silent and
        # every one of them is permissive, which is why this is a refusal
        # and not a passthrough.
        if stop_loss is not None and not math.isfinite(stop_loss):
            self._note_refusal(
                symbol, direction, STOP_REFUSAL_STOP_NOT_FINITE,
                f"the stop price supplied for this trade is not a finite "
                f"number ({stop_loss!r}), so no distance can be measured "
                f"from it. Refused rather than let through comparisons a "
                f"NaN silently passes.",
            )
            return None
        if not math.isfinite(entry_price):
            self._note_refusal(
                symbol, direction, STOP_REFUSAL_ENTRY_NOT_FINITE,
                f"the entry price is not a finite number ({entry_price!r}), "
                f"so no stop distance can be measured from it.",
            )
            return None
        # Unchanged legacy passthrough: a non-positive stop or entry is the
        # caller's to reject, and it does (`stop_loss <= 0` there is a real
        # comparison on a real number). A MISSING stop is no longer passed
        # through as None (item 54, 2026-09-12): it is read from the
        # instrument below, in the same branch that widens an unbacked one.
        if entry_price <= 0 or (stop_loss is not None and stop_loss <= 0):
            return stop_loss

        is_short = direction == "short"
        # A stop on the WRONG side of entry is refused here rather than
        # widened into validity. Found 2026-09-01: widening moves the stop to
        # `entry +/- multiple * ATR`, which is unconditionally on the correct
        # side, so a short handed a stop BELOW its entry came out of this
        # function with a valid-looking stop above it — silently repairing
        # exactly the nonsense the caller's side check exists to catch.
        # `test_short_stop_at_or_below_entry_is_rejected` only passed because
        # its fixture carried no ATR, which is not a state production reaches
        # (`atr_14` is Python-set on every analysis). Widening corrects a stop
        # that is too CLOSE; it must not invent one that is on the wrong side.
        if stop_loss is None:
            pass  # nothing typed: derived from the instrument below
        elif is_short and stop_loss <= entry_price:
            self._note_refusal(
                symbol, direction, STOP_REFUSAL_WRONG_SIDE,
                f"the stop ${stop_loss:,.2f} is at or below the "
                f"${entry_price:,.2f} entry on a SHORT, so it protects "
                f"nothing. Refused rather than widened into validity.",
            )
            return None
        elif not is_short and stop_loss >= entry_price:
            self._note_refusal(
                symbol, direction, STOP_REFUSAL_WRONG_SIDE,
                f"the stop ${stop_loss:,.2f} is at or above the "
                f"${entry_price:,.2f} entry on a BUY, so it protects "
                f"nothing. Refused rather than widened into validity.",
            )
            return None

        atr = getattr(analysis, "atr_14", None) if analysis else None
        try:
            atr = float(atr) if atr is not None else None
        except (TypeError, ValueError):
            atr = None
        if atr is not None and (not math.isfinite(atr) or atr <= 0):
            atr = None

        # The target is resolved BEFORE any branch, because the reward:risk
        # floor now applies on every one of them. It used to be resolved
        # after two early returns had already carried most stops past it.
        if target_price is None and analysis is not None:
            target_price = getattr(analysis, "reference_target", None)
        had_target = target_price is not None
        try:
            target_price = float(target_price) if target_price else None
        except (TypeError, ValueError):
            target_price = None

        side_word = "above" if is_short else "below"
        side_label = "SHORT" if is_short else "BUY"

        # -------------------------------------------------------------
        # Decide the shipping stop, and name the rule that placed it.
        # Exactly one of these branches runs (no ATR; nothing typed, read
        # from the instrument; outside the band; level-honoured; absolute
        # floor; widened to the band or the signal bar). `honoured` is what
        # ships; `rule` is why; the width gate and then the single
        # reward:risk gate at the bottom judge the geometry that results,
        # whichever branch produced it.
        # -------------------------------------------------------------
        if atr is None:
            # No volatility reading — leave structure alone, as always. The
            # stop is still judged on its own geometry below: whether we
            # can measure this name's noise has nothing to do with whether
            # the trade's payoff clears the floor. With nothing typed AND
            # nothing to read, there is no stop: the caller rejects None.
            if stop_loss is None:
                # Board item 10 (2026-09-14, second pass). THE ONE PATH THE
                # REGEX NEVER SAW: this message puts "has" straight after the
                # symbol, and `_DropReasonCapture._SYMBOL` requires
                # rejected|refused|skipped there. A candidate dropped here
                # reached the record as the literal "no matching constructor
                # log line captured" — the exact signature the first pass
                # went looking for and reported as fully eliminated.
                self._note_refusal(
                    symbol, direction, STOP_REFUSAL_NO_STOP_NO_VOLATILITY,
                    "no stop was typed by the PM or the analyst and there is "
                    "no ATR reading to derive one from, so this trade has no "
                    "stop at all to judge. Not a view on the idea: nothing "
                    "measurable was available to protect it with.",
                )
                return None
            honoured, rule, level = stop_loss, STOP_RULE_NO_VOLATILITY, None
            band_edge = multiple = None
        else:
            multiple = self._stop_atr_multiple(analysis, regime)
            band_edge = (
                entry_price + multiple * atr if is_short
                else entry_price - multiple * atr
            )
            # The instrument's own fallback (item 54): the WIDER of the
            # noise band and the signal bar's far edge — Kullamägi's
            # "low of the day" placement, read from the last completed bar
            # the analyst judged. Python-set beside the levels; None on an
            # older row or a hand-built object, in which case the band
            # alone decides, as it did before.
            bar_edge = getattr(
                analysis, "signal_bar_high" if is_short else "signal_bar_low", None,
            )
            try:
                bar_edge = float(bar_edge) if bar_edge is not None else None
            except (TypeError, ValueError):
                bar_edge = None
            if bar_edge is not None and (
                not math.isfinite(bar_edge) or bar_edge <= 0
                or (bar_edge <= entry_price if is_short else bar_edge >= entry_price)
            ):
                bar_edge = None
            bar_wins = bar_edge is not None and (
                bar_edge > band_edge if is_short else bar_edge < band_edge
            )
            fallback_edge = bar_edge if bar_wins else band_edge
            fallback_rule = STOP_RULE_SIGNAL_BAR if bar_wins else STOP_RULE_ATR_BAND
            level = None
            if stop_loss is None:
                # Nothing typed by the PM or the analyst. Read the stop from
                # the instrument rather than refuse: a stop is always
                # derivable (item 54), and the width gate below still judges
                # the result.
                honoured, rule = fallback_edge, fallback_rule
                logger.info(
                    "Constructor: %s %s had no stop from the PM or the "
                    "analyst; placed at $%.2f from the instrument [%s] "
                    "(%.2f x ATR band $%.2f%s).",
                    side_label, symbol, honoured, rule, multiple, band_edge,
                    f", signal bar edge ${bar_edge:.2f}" if bar_edge is not None else "",
                )
                placed = True
                stop_loss = honoured
            else:
                placed = False
                outside_band = (
                    stop_loss >= band_edge if is_short
                    else (band_edge <= 0 or stop_loss <= band_edge)
                )
            if placed:
                pass  # read from the instrument above; nothing to widen
            elif outside_band:
                # Already further from entry than the noise band asks for.
                # Nothing to widen — but this is the path that used to
                # return before the floor ran, and it is the majority path.
                honoured, rule = stop_loss, STOP_RULE_OUTSIDE_BAND
            else:
                # ---------------------------------------------------------
                # §12.1 — is this stop sitting on something we COMPUTED?
                # ---------------------------------------------------------
                level = self._level_backing_stop(
                    analysis, entry_price, stop_loss, is_short,
                )
                if level is not None:
                    honoured, rule = stop_loss, STOP_RULE_LEVEL_HONOURED
                    floor_multiple = self.cfg.absolute_min_stop_atr_multiple
                    hard_floor = (
                        entry_price + floor_multiple * atr if is_short
                        else entry_price - floor_multiple * atr
                    )
                    inside_hard_floor = (
                        floor_multiple > 0
                        and hard_floor > 0
                        and (
                            stop_loss < hard_floor if is_short
                            else stop_loss > hard_floor
                        )
                    )
                    if inside_hard_floor:
                        # Real structure, still too close to survive one
                        # ordinary session. Pushed out to the 1x floor and
                        # NOT to the full band — the band is what §12.1
                        # removed. See the docstring for why this floor
                        # lives in code rather than in a prompt.
                        honoured, rule = hard_floor, STOP_RULE_ABSOLUTE_FLOOR
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
                    else:
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
                else:
                    # Nothing computed backs this stop — widen it to the
                    # instrument's own fallback: the band, exactly as
                    # before §12.1, or the signal bar's far edge when that
                    # is wider (item 54).
                    honoured, rule = fallback_edge, fallback_rule
                    logger.info(
                        "Constructor: %s %s stop widened $%.2f → $%.2f "
                        "(%.1f%% %s entry) [%s] — no computed structural "
                        "level sits at it, and it was placed inside %.2f x "
                        "ATR of $%.2f (%s setup, %s tape). A stop nothing on "
                        "the chart defends does not earn the §12.1 exemption.%s",
                        side_label, symbol, stop_loss, honoured,
                        100 * abs(entry_price - honoured) / entry_price,
                        side_word, rule, multiple, atr,
                        getattr(analysis, "setup_type", None) or "unknown",
                        regime or "unknown",
                        (f" The signal bar's {'high' if is_short else 'low'} "
                         f"${bar_edge:.2f} sits past the band's ${band_edge:.2f}, "
                         f"so the bar decides.") if bar_wins else "",
                    )

        # -------------------------------------------------------------
        # The WIDTH gate (item 54, 2026-09-12): whatever placed the stop,
        # it must sit within the instrument's own REACH over this trade's
        # horizon.
        # -------------------------------------------------------------
        # Kullamägi's constraint in SHAPE — the stop's width is measured in
        # the stock's own volatility units and refused past a cap — and
        # deliberately not his unit. See STOP_REFUSAL_WIDER_THAN_REACH for
        # why neither his 1 x daily range nor the desk's 2.5 x ATR band can
        # be the cap on this desk. The cap is `horizon_reach`: ATR x
        # sqrt(horizon) x the reach multiple the target derivation and the
        # level scan already use — the furthest price plausibly travels
        # inside the trade. A stop past it cannot be hit inside the trade,
        # so it is not a stop, and the risk-based size computed from it
        # is fiction. Under the cap, width is answered by `_plan_risk_
        # targets` sizing down — the ratified §2.1 invariant — never here.
        # No horizon means no reach and no gate: `_derive_target` has
        # already refused such a trade by name on both live paths, so this
        # only ever passes a hand-built shim (the backtest engine).
        if atr is not None:
            stated_horizon = getattr(
                analysis, "expected_horizon_sessions", None,
            )
            reach = horizon_reach(
                atr, stated_horizon,
                max_reach_atr_multiple=(
                    self.cfg.max_stop_width_reach_atr_multiple
                ),
                max_horizon_sessions=self.cfg.max_target_horizon_sessions,
            )
            width = abs(entry_price - honoured)
            # The READING (item 56, 2026-09-13): what this width actually
            # means, as the probability the stop is touched inside the
            # horizon. Reflection principle + the range-to-sigma identity,
            # both published, no chosen constant — `levels.touch_probability`
            # carries the citations. Recorded on EVERY stop, passing or
            # refused, because the threshold is still open and this is the
            # measurement that would settle it.
            p_touch = touch_probability(
                width / atr,
                min(
                    int(stated_horizon or 0) or 1,
                    max(1, int(self.cfg.max_target_horizon_sessions)),
                ),
            )
            touch_note = (
                f" Reading: a stop this far out is touched inside the "
                f"horizon with probability {p_touch:.1%} "
                f"(reflection principle; see docs/WORK.md item 56 — the "
                f"desk has no derivation for where that probability becomes "
                f"too low, and this multiple is convention)."
                if p_touch is not None else ""
            )
            if reach is not None and width > reach and not math.isclose(
                width, reach, rel_tol=1e-9,
            ):
                self._note_refusal(
                    symbol, direction, STOP_REFUSAL_WIDER_THAN_REACH,
                    f"the stop this trade needs sits ${honoured:,.2f} "
                    f"[{rule}], {width / atr:.2f} x ATR {side_word} the "
                    f"${entry_price:,.2f} entry — past the ${reach:,.2f} "
                    f"({reach / atr:.2f} x ATR) the instrument can plausibly "
                    f"travel inside this trade's "
                    f"{stated_horizon}-"
                    f"session horizon. A stop price cannot reach is not a "
                    f"stop, and the size computed from it would be fiction "
                    f"(Kullamägi's width rule in shape, the desk's own reach "
                    f"as the unit).{touch_note}",
                )
                return None
            if p_touch is not None:
                logger.info(
                    "Constructor: %s stop width %.2f x ATR over a "
                    "%s-session horizon — touch probability %.1f%% "
                    "(item 56 reading; gate at %.2f x ATR x sqrt(H)).",
                    symbol, width / atr, stated_horizon, 100 * p_touch,
                    self.cfg.max_stop_width_reach_atr_multiple,
                )

        # -------------------------------------------------------------
        # ONE reward:risk gate, on the stop that will actually ship.
        # -------------------------------------------------------------
        # Both sides of this ratio are measured: the stop is whatever the
        # branch above decided, and the target is where structure says
        # price travels (`_derive_target`). The refusal is about GEOMETRY
        # and says so — this entry does not support this trade — not about
        # a model having guessed a poor target.
        setup_type = getattr(analysis, "setup_type", None) if analysis else None
        if not reward_risk_floor_applies(
            setup_type, structural_ceiling=structural_ceiling,
        ):
            # TYPE B / BREAKOUT — no reward-side refusal here at all
            # (owner 2026-09-11, restated 2026-09-17). Nothing overhead is
            # expected to stop this stock. The RISK side is unchanged and
            # has already run above.
            logger.info(
                "Constructor: %s %s stop $%.2f [%s] shipped with NO "
                "reward:risk check — breakout setup. There is no overhead "
                "level to measure a reward against and the position is "
                "managed by trailing, so approval rests on the risk side "
                "alone.",
                side_label, symbol, honoured, rule,
            )
            return honoured
        reward_risk = self._reward_risk_at(
            entry_price, honoured, target_price, is_short,
        )
        if reward_risk is None and had_target:
            # Recorded fact, not a refuse. Owner 2026-09-17: unmeasurable
            # payoff honesty may stay as ranking hint with zero refuse,
            # zero size floor, zero sub-floor branch.
            logger.info(
                "Constructor: %s %s stop $%.2f [%s] shipped with "
                "unmeasurable reward:risk — a target was supplied "
                "(%r) but payoff against this stop at $%.2f cannot be "
                "computed. Honesty about unknown geometry, not a floor.",
                side_label, symbol, honoured, rule, target_price, entry_price,
            )
        return honoured

    def shipped_stop_rule(
        self,
        analysis: TechAnalysisResult | None,
        entry_price: float,
        stop_loss: float,
        direction: str = "long",
    ) -> str | None:
        """Which rule the SHIPPING stop answers to, for `TradeDecision`.

        Asks `_level_backing_stop` — the same function, the same
        `computed_levels`, no second data path — whether the final rounded
        stop sits at a level the system computed. That is deliberately the
        question about the stop as it ships, not about the stop as it
        arrived: what the execution stage needs to know is whether the
        number it is holding is defended by structure.

        Returns `STOP_RULE_LEVEL_HONOURED` when it is, None when it is not
        or cannot be judged. Fails to the SAFE side: None means the
        execution-time ATR floor in `src/pipeline_stages.py` still applies,
        which is the pre-existing behaviour.
        """
        # The ATR check below is NOT the match tolerance any more (item 46
        # made that a percentage of the level's own price). It is kept as a
        # precondition because what this function REPORTS is an exemption
        # from an ATR floor: with no usable ATR there is no floor to be
        # exempt from, and claiming the exemption would be answering a
        # question nobody can evaluate. Unchanged behaviour, different
        # reason.
        atr = getattr(analysis, "atr_14", None) if analysis else None
        try:
            atr = float(atr) if atr is not None else None
        except (TypeError, ValueError):
            return None
        if atr is None or not math.isfinite(atr) or atr <= 0:
            return None
        if (
            not math.isfinite(entry_price) or entry_price <= 0
            or not math.isfinite(stop_loss) or stop_loss <= 0
        ):
            return None
        level = self._level_backing_stop(
            analysis, entry_price, stop_loss, direction == "short",
        )
        return STOP_RULE_LEVEL_HONOURED if level is not None else None

    @staticmethod
    def _current_weights(
        positions: list[Position], total_value: float,
    ) -> dict[str, float]:
        """Current-position weights as gross-leverage percentages.

        Uses the same `_gross_multiplier` convention as
        `RiskRuleEngine.check` (risk/rules.py:28). For inverse / leveraged
        ETFs (SH=−1x, SDS=−2x, PSQ=−1x, SQQQ=−3x) the gross multiplier
        is the unsigned magnitude — a $10K SQQQ position consumes 30%
        gross notional, not 10% raw, exactly as the risk engine
        evaluates it.

        Pre-fix this used raw `market_value / total_value`, so a PM
        target_weight_pct=20 on SQQQ (intended as the 20% single-name
        cap) computed as 20% raw in the constructor but 60% gross at
        the engine — the engine then hard-blocked every leveraged-ETF
        target at the ceiling, while the constructor's delta math saw
        no trim needed. Now constructor + engine agree on the
        semantics: target_weight_pct IS gross-leverage percentage.
        """
        if total_value <= 0:
            return {}
        # Local import to avoid the cyclic risk -> portfolio_constructor
        # import chain at module load.
        from src.risk.rules import position_weight_pct
        # SIGNED, not absolute. A short has a negative qty and a negative
        # market_value (Alpaca convention), so it lands in the map as a
        # NEGATIVE weight. Signed is the correct choice because every consumer
        # of this map does exposure arithmetic, not magnitude arithmetic:
        #   - the delta loop computes `target_pct - current_pct`, and only the
        #     signed form makes "held -8%, want 0%" read as +8% of buying to
        #     do rather than 8% of selling;
        #   - the close test `target_pct == 0 and current_pct > 0` must NOT
        #     fire for a short, because a SELL on a short adds to it;
        #   - `_build_sell` already refuses `current_pct <= 0`, so a short is
        #     structurally excluded from the sell path rather than mis-sized.
        # An absolute weight would make a short indistinguishable from a long
        # of the same size at exactly the places where the direction is the
        # whole question. The previous `p.qty > 0` filter dropped shorts from
        # the map entirely, so `current_weights.get(sym, 0.0)` reported a held
        # short as unheld and the delta loop would re-open it every session.
        return {
            p.symbol: position_weight_pct(p, total_value)
            for p in positions
            if p.qty != 0
        }

    @staticmethod
    def _current_sector_weights(
        positions: list[Position], total_value: float,
    ) -> dict[tuple[str, str], float]:
        """Held GROSS exposure per `(sector, side)`, as % of equity.

        Spec §10.3 (the dial) and §12.2 (the split). Calls the SAME
        `sector_side_weights` that `RiskRuleEngine.check` measures with,
        rather than restating the arithmetic — the constructor sizing against
        a different book than the gate measures is how a scaled order gets
        blocked anyway, and three hand-written copies of this sum is how the
        signed-vs-gross defect survived.

        §12.2 CORRECTION: `market_value` used to be summed SIGNED, so a held
        short REDUCED its sector's measured weight even though the engine's
        own comment on that block claimed "gross ... unsigned magnitude". It
        is now an unsigned magnitude booked to the short side's own budget.
        A long and a short in the same sector do not offset.

        Sector is read off the POSITION's own `sector` field rather than
        `_get_sector(symbol)` — the engine sums held positions the first way
        and resolves only the CANDIDATE symbol the second way, so
        `_apply_sector_dial` does the same.
        """
        from src.risk.rules import sector_side_weights
        return sector_side_weights(positions, total_value)

    def _apply_sector_dial(
        self,
        symbol: str,
        allocation_pct: float,
        *,
        sector_weights: dict[tuple[str, str], float],
        total_value: float,
        action: str = "BUY",
    ) -> tuple[float, str]:
        """Spec §10.3. Shrink a crowded sector's next trade instead of vetoing it.

        Returns `(allocation_pct, note)`. `allocation_pct` is RAW notional
        percent (the units every downstream consumer spends); the sector
        budget is GROSS, so the conversion happens here exactly once, the
        same way the single-name clamp above does it.

        Spec §12.2: the budget consulted is the one for THIS ORDER'S SIDE.
        A crowded long book in a sector does not shrink a short into it, and
        the reverse — "a long and a short in the same sector is not a hedge",
        so neither is it a shared budget. This is what keeps the owner's pair
        trade (long the leader, short the laggard in one hot sector) legal.

        Returns a NEGATIVE allocation to mean "refuse" — either the sector is
        at its absolute ceiling, or what crowding leaves is too small to be
        worth trading. The callers already treat `<= 0` as no order.
        """
        from src.risk.rules import (
            _gross_multiplier, decision_side, sector_allowance_pct,
            sector_size_scale,
        )
        from src.execution.broker import _get_sector

        sector = _get_sector(symbol)
        if not sector or sector == "Unknown":
            # Sizing (this pass) still skips the dial for an unresolved
            # sector — a deliberate, unrelated design choice, not a gap.
            # 2026-09-01 audit: the ENGINE (RiskRuleEngine.check, rule 5)
            # no longer matches this — it now pools "Unknown" as its own
            # bucket and gates it, so an order this pass declines to shrink
            # still cannot silently over-concentrate; the engine's hard wall
            # catches what this pass does not pre-shrink. See
            # src/risk/rules.py rule 5's comment for the full defect.
            return allocation_pct, ""

        side = decision_side(action)
        current_pct = sector_weights.get((sector, side), 0.0)
        scale = sector_size_scale(
            current_pct,
            soft_cap_pct=self.cfg.max_sector_pct,
            hard_cap_pct=self.cfg.max_sector_hard_pct,
        )
        allowance_gross = sector_allowance_pct(
            current_pct,
            soft_cap_pct=self.cfg.max_sector_pct,
            hard_cap_pct=self.cfg.max_sector_hard_pct,
        )
        gross_mul = _gross_multiplier(symbol)
        # Below the diversification target the dial is inert (scale == 1.0)
        # and the allowance is wider than any single name may take anyway —
        # say nothing, change nothing, so an uncrowded trade's audit trail
        # is not cluttered with a cap that never bound.
        scaled = allocation_pct * scale
        allowance_raw = allowance_gross / gross_mul
        final = min(scaled, allowance_raw)
        if final >= allocation_pct:
            return allocation_pct, ""

        if scale <= 0.0 or allowance_raw <= 0.0:
            # Board item 10 (2026-09-14, second pass). Both dial refusals
            # logged a sentence the capture's regex happens to match, so
            # they were never invisible — but a matched sentence lands as a
            # generic `constructor_dropped` row, not as a code the funnel
            # can count. Filed here rather than at the two `return None`
            # sites in the builders, because only this method knows WHICH
            # of the two ends fired.
            self._note_refusal(
                symbol, "short" if side == "short" else "long",
                STOP_REFUSAL_SECTOR_AT_HARD_CEILING,
                f"sector '{sector}' ({side} side) is at {current_pct:.1f}% of "
                f"equity, at or past the {self.cfg.max_sector_hard_pct:.0f}% "
                f"absolute ceiling; no size is available. Concentration "
                f"scales size, but not without end.",
            )
            return -1.0, (
                f" [constructor: REFUSED — sector '{sector}' ({side} side) is "
                f"at {current_pct:.1f}% of equity, at or past the "
                f"{self.cfg.max_sector_hard_pct:.0f}% absolute ceiling. "
                f"Concentration scales size, but not without end]"
            )

        # The floor. A position this small cannot pay for its own risk.
        notional = total_value * final / 100
        if notional < self.cfg.min_order_usd:
            # Board item 10 (2026-09-14, second pass) — see the sibling
            # refusal above.
            self._note_refusal(
                symbol, "short" if side == "short" else "long",
                STOP_REFUSAL_SECTOR_BELOW_MIN_ORDER,
                f"sector '{sector}' ({side} side) is at {current_pct:.1f}% of "
                f"equity, so crowding leaves only {final:.2f}% "
                f"(~${notional:,.0f}) — under the "
                f"${self.cfg.min_order_usd:,.0f} minimum order. A position "
                f"this small pays full commission and full attention for an "
                f"immaterial payoff, so it is not taken at all.",
            )
            return -1.0, (
                f" [constructor: REFUSED — sector '{sector}' ({side} side) is "
                f"at {current_pct:.1f}% of equity, so crowding leaves only "
                f"{final:.2f}% (~${notional:,.0f}). That is under the "
                f"${self.cfg.min_order_usd:,.0f} minimum order: a position "
                f"this small pays full commission and full attention for an "
                f"immaterial payoff, so it is not taken at all]"
            )

        logger.info(
            "Constructor: %s size scaled for sector crowding "
            "(%.2f%% → %.2f%%; sector '%s' at %.1f%% gross, target %.0f%%, "
            "ceiling %.0f%%, dial %.2f)",
            symbol, allocation_pct, final, sector, current_pct,
            self.cfg.max_sector_pct, self.cfg.max_sector_hard_pct, scale,
        )
        # Provenance for the AI Risk Manager and the owner. A smaller position
        # than the PM asked for must never be silently applied — someone
        # seeing an unexpectedly small position has to be able to find out
        # why, and this is the string that tells them.
        return final, (
            f" [constructor: size scaled {allocation_pct:.2f}% → {final:.2f}% "
            f"because sector '{sector}' ({side} side) is already "
            f"{current_pct:.1f}% of equity, over the "
            f"{self.cfg.max_sector_pct:.0f}% concentration "
            f"target. The idea was judged on its own merits and taken, just "
            f"smaller; it is refused only past {self.cfg.max_sector_hard_pct:.0f}%. "
            f"Deterministic, not PM inconsistency]"
        )

    def _accrue_sector(
        self,
        sector_weights: dict[tuple[str, str], float],
        decision: TradeDecision,
    ) -> None:
        """Book an order's GROSS sector consumption so the NEXT order in the
        same batch sees a book that already contains it.

        Without this, three targets in one crowded sector would each be sized
        against the same stale starting weight and collectively breach the
        ceiling — the identical accumulator the pipeline's risk filter keeps
        in `pending_sector_investment`, for the identical reason.
        """
        from src.risk.rules import _gross_multiplier, decision_side
        from src.execution.broker import _get_sector
        if decision.action not in ("BUY", "SHORT"):
            return
        sector = _get_sector(decision.symbol)
        if not sector or sector == "Unknown":
            return
        # Spec §12.2 — into THIS order's side. A SHORT booked into the long
        # bucket would shrink the next long for crowding that is not there.
        key = (sector, decision_side(decision.action))
        sector_weights[key] = sector_weights.get(key, 0.0) + (
            decision.allocation_pct * _gross_multiplier(decision.symbol)
        )

    @staticmethod
    def _hold_decision(target: TargetPosition) -> TradeDecision:
        """Record PM's explicit 'keep' intent as a HOLD for audit trail."""
        return TradeDecision(
            action="HOLD",
            symbol=target.symbol,
            allocation_pct=0.0,
            entry_price=0.0,
            stop_loss=0.0,
            take_profit=0.0,
            reasoning=f"Hold at current weight. Thesis: {target.thesis[:200]}",
        )

    @staticmethod
    def _build_sell(
        target: TargetPosition,
        position: Position | None,
        current_pct: float,
        target_pct: float,
        current_risk_pct: float | None = None,
    ) -> TradeDecision | None:
        if position is None or position.qty <= 0:
            return None
        # Defensive: position.market_value can be NaN during broker price
        # glitches (qty > 0 but current_price NaN → market_value NaN).
        # Without this guard `current_pct` (computed upstream as
        # market_value / total_value * 100) is NaN, the partial-fraction
        # math `(NaN - target_pct) / NaN` is NaN, alloc becomes NaN, and
        # the BUY downstream sends a NaN qty to the broker. Pipeline.py:446
        # has the symmetric guard on the SELL pre-sum path; this is the
        # same fix in the constructor path. R4 audit finding.
        import math as _math
        if not _math.isfinite(current_pct) or current_pct <= 0:
            logger.warning(
                "Constructor: SELL %s skipped — current_pct=%s "
                "(market_value=%s likely NaN/zero from broker glitch)",
                target.symbol, current_pct, position.market_value,
            )
            return None
        named = _named_reduction_trigger(
            target, current_pct, target_pct, long_side=True,
            current_risk_pct=current_risk_pct,
        )
        if named is None:
            return None
        reasoning, falsifier = named
        if target_pct == 0:
            # Full close
            alloc = 100.0
        else:
            # Partial: sell enough to land on target_pct
            # fraction to sell = (current - target) / current
            fraction = (current_pct - target_pct) / current_pct
            alloc = max(1.0, min(99.0, round(fraction * 100, 1)))
        # SELLs don't need live entry/stop/target — execution uses market price
        return TradeDecision(
            action="SELL",
            symbol=target.symbol,
            allocation_pct=alloc,
            entry_price=0.0,
            stop_loss=0.0,
            take_profit=0.0,
            reasoning=reasoning[:500],
            # Real, untruncated field alongside the embedded-in-reasoning
            # text above — see TradeDecision.thesis_invalid_if.
            # Blank-falsifier funding-trims leave this None — never invent.
            thesis_invalid_if=falsifier or None,
        )

    @staticmethod
    def _build_cover(
        target: TargetPosition,
        position: Position | None,
        current_pct: float,
        target_pct: float,
        current_risk_pct: float | None = None,
    ) -> TradeDecision | None:
        """D1/D3 (Stage 3): the SELL-side twin, for reducing/closing a short.

        `current_pct` and `target_pct` are both SIGNED and <= 0 here (the
        `construct_orders` dispatch only reaches this builder when the
        position is currently short and the signed target does not cross
        zero to the long side). The fraction-to-cover formula is
        algebraically identical to `_build_sell`'s — it falls out of the
        same `(current - target) / current` shape on negative numbers.
        """
        if position is None or position.qty >= 0:
            return None
        import math as _math
        if not _math.isfinite(current_pct) or current_pct >= 0:
            logger.warning(
                "Constructor: COVER %s skipped — current_pct=%s "
                "(market_value=%s likely NaN/zero from broker glitch)",
                target.symbol, current_pct, position.market_value,
            )
            return None
        named = _named_reduction_trigger(
            target, current_pct, target_pct, long_side=False,
            current_risk_pct=current_risk_pct,
        )
        if named is None:
            return None
        reasoning, falsifier = named
        if target_pct == 0:
            # Full cover
            alloc = 100.0
        else:
            # Partial: buy back enough to land on target_pct.
            fraction = (current_pct - target_pct) / current_pct
            alloc = max(1.0, min(99.0, round(fraction * 100, 1)))
        # COVERs don't need live entry/stop/target — execution uses market price
        return TradeDecision(
            action="COVER",
            symbol=target.symbol,
            allocation_pct=alloc,
            entry_price=0.0,
            stop_loss=0.0,
            take_profit=0.0,
            reasoning=reasoning[:500],
            # Real, untruncated field alongside the embedded-in-reasoning
            # text above — see TradeDecision.thesis_invalid_if.
            # Blank-falsifier funding-trims leave this None — never invent.
            thesis_invalid_if=falsifier or None,
        )

    def _build_buy(
        self,
        target: TargetPosition,
        analysis: TechAnalysisResult | None,
        current_pct: float,
        target_pct: float,
        total_value: float,
        market_price: float | None,
        plan: RiskPlan | None = None,
        regime: str | None = None,
        sector_weights: dict[tuple[str, str], float] | None = None,
    ) -> TradeDecision | None:
        # A risk-based target already resolved its entry and stop in
        # _plan_risk_targets — reusing them keeps the size the budget granted
        # consistent with the level the order actually ships, which a second
        # resolution against a moved quote would not.
        if plan is not None and plan.entry_price is not None and plan.stop_price is not None:
            entry_price, stop_loss = plan.entry_price, plan.stop_price
        else:
            entry_price, stop_loss = self._resolve_entry_and_stop(
                target, analysis, market_price, regime=regime,
            )
            if entry_price is None or stop_loss is None:
                # drop-reason: delegated — every exit from
                # `_resolve_entry_and_stop` files a fault or a named refusal.
                return None

        # Take-profit is COMPUTED from structure (2026-09-01), not read from
        # the analyst's `reference_target`. Two earlier stages of the same
        # argument: `entry * (1 + 2*stop_gap_pct)` manufactured a target when
        # the analyst omitted one, and was deleted; then the analyst's own
        # number was made mandatory, which removed the fabrication but left
        # the reward:risk gate dividing a measured stop by a guessed target.
        # Now both sides of that ratio come from the bars. The model's guess
        # survives on `analysis.reference_target` as evidence and is logged
        # against the computed level by `_derive_target`.
        derivation = self._derive_target(
            target.symbol, analysis, entry_price, target.direction,
        )
        if derivation.price is None or derivation.price <= entry_price:
            # A data fault is already recorded/logged as UNMEASURABLE by
            # `_derive_target`; only a real refusal is a rejection here.
            if not derivation.fault:
                # Board item 10 (2026-09-14, second pass) — same treatment as
                # the identical check in `_resolve_entry_and_stop`. The
                # derivation's own refusal code wins when it has one; the
                # fallback names the case where a price WAS computed and it
                # simply does not sit above the entry.
                self._note_refusal(
                    target.symbol, target.direction,
                    derivation.refusal or STOP_REFUSAL_TARGET_NOT_ABOVE_ENTRY,
                    f"no take-profit could be computed above the "
                    f"${entry_price:,.2f} entry for this long"
                    + (f": {derivation.detail}" if derivation.detail else
                       f" (computed {derivation.price})"),
                )
            return None
        take_profit = float(derivation.price)

        # `target_pct` and `current_pct` are GROSS-leverage weights (see
        # _current_weights), but every consumer of `allocation_pct` spends it
        # as RAW notional: risk/rules.py does `total_value * alloc/100` and
        # THEN applies the gross multiplier itself, and ExecutionStage sizes
        # `qty = total_value * alloc/100 / price`. Emitting the gross delta
        # raw therefore over-deployed leveraged/inverse ETFs by their
        # multiplier (2026-07-16 audit: a PM target of 6% gross on SQQQ (3x)
        # deployed $6k raw = 18% gross of a $100k book — and the NEXT session
        # saw current_pct=18 vs target 6 and emitted SELL 67% of the hedge PM
        # wanted held, repeating until raw ≈ 2%). Convert once, here, so the
        # delta and every downstream consumer speak the same units. No-op for
        # the ~99% of the universe with multiplier 1.0.
        from src.risk.rules import _gross_multiplier
        allocation_pct = (target_pct - current_pct) / _gross_multiplier(target.symbol)
        # Pull in vol-adj sizing in a uniform way: ensure qty (computed
        # downstream) doesn't put more than risk_budget_pct of equity at risk.
        # NOTE: alloc_cap_by_risk below is computed in RAW notional terms, so
        # this conversion must happen BEFORE the comparison.
        # D4: unsigned everywhere — a plain `entry - stop` is negative for a
        # short (whose stop sits above entry), which would corrupt this cap
        # instead of tightening it.
        risk_per_share = abs(entry_price - stop_loss)
        risk_dollars_allowed = total_value * self.cfg.risk_budget_pct / 100
        # qty_by_risk = risk_dollars_allowed / risk_per_share
        # position_$ = qty_by_risk * entry_price
        # allocation_by_risk_pct = position_$ / total_value * 100
        #                        = (risk_dollars_allowed / risk_per_share) * entry_price / total_value * 100
        cap_note = ""
        if risk_per_share > 0:
            alloc_cap_by_risk = (
                risk_dollars_allowed * entry_price / risk_per_share / total_value * 100
            )
            if allocation_pct > alloc_cap_by_risk:
                logger.info(
                    "Constructor: %s alloc capped by risk budget "
                    "(delta %.2f%% → %.2f%% at %.1f%% risk budget)",
                    target.symbol, allocation_pct, alloc_cap_by_risk,
                    self.cfg.risk_budget_pct,
                )
                # Provenance for the AI Risk Manager: it audits the
                # CONSTRUCTED order against PM's prose. Without this note
                # a capped allocation reads as PM claiming one size and
                # proposing another — on 2026-08-20 the RM called exactly
                # that mismatch (PM 15% vs constructed 10.65%) "plan
                # inconsistency", scored the reasoning chain incoherent
                # and issued a full-plan veto over deterministic math.
                cap_note = (
                    f" [constructor: PM target delta {allocation_pct:.2f}% "
                    f"capped to {alloc_cap_by_risk:.2f}% by the "
                    f"{self.cfg.risk_budget_pct:.1f}% risk budget — the size "
                    f"difference vs PM's stated weight is deterministic, "
                    f"not PM inconsistency]"
                )
                allocation_pct = alloc_cap_by_risk

        # Single-name notional ceiling. The risk engine treats
        # `max_position_pct` as a HARD BLOCK, not a trim, so an order above it
        # is not "reduced" downstream — it is dropped and the trade never
        # happens. Under risk-based sizing that is the common case rather than
        # the edge: risk_pct x entry/(entry - stop) exceeds 20% of equity for
        # any conviction above ~1% at this book's real stop distances. Clamp
        # to what the engine will actually accept, and say so, rather than
        # shipping an order built to be rejected.
        #
        # The resulting position therefore risks LESS than the PM allocated
        # whenever this binds. That is the honest outcome of the two ceilings
        # meeting, and the note carries it into the audit trail — silently
        # delivering under-sized risk is exactly the kind of gap this pass
        # exists to close.
        gross_mul = _gross_multiplier(target.symbol)
        name_headroom_pct = (self.cfg.max_position_pct - current_pct) / gross_mul
        if allocation_pct > name_headroom_pct:
            logger.info(
                "Constructor: %s alloc capped by the single-name ceiling "
                "(delta %.2f%% → %.2f%%; %.1f%% max position, %.2f%% already held)",
                target.symbol, allocation_pct, max(0.0, name_headroom_pct),
                self.cfg.max_position_pct, current_pct,
            )
            cap_note += (
                f" [constructor: size capped to {max(0.0, name_headroom_pct):.2f}% "
                f"by the {self.cfg.max_position_pct:.0f}% single-name ceiling — "
                f"the stop is close enough that the requested risk would need a "
                f"larger position than one name may hold, so this trade carries "
                f"less risk than allocated. Deterministic, not PM inconsistency]"
            )
            allocation_pct = name_headroom_pct

        # Spec §10.3. Sector crowding scales the size; it does not veto the
        # trade. Applied LAST, after the risk budget and the single-name
        # ceiling, because it operates on what this order would actually
        # deploy — scaling a number the clamps below would then have cut
        # anyway would understate the position twice. §12.2: measured against
        # the LONG budget of that sector only.
        if sector_weights is not None:
            allocation_pct, sector_note = self._apply_sector_dial(
                target.symbol, allocation_pct,
                sector_weights=sector_weights, total_value=total_value,
                action="BUY",
            )
            cap_note += sector_note
            if allocation_pct < 0:
                # drop-reason: delegated — `_apply_sector_dial` files the
                # refusal, because only it knows which of its two ends fired.
                return None

        allocation_pct = max(0.0, round(allocation_pct, 2))
        if allocation_pct <= 0:
            # Board item 10 (2026-09-14): the risk-budget-per-trade cap and
            # the single-name ceiling above both LOG when they shrink a
            # request, but neither message says rejected/refused/skipped, so
            # `_DropReasonCapture` never sees them — and unlike the ATR/no-
            # stop path in `_resolve_entry_and_stop`, nothing else logs for
            # this symbol afterward to compensate. `cap_note` already carries
            # the deterministic provenance of whichever cap(s) bound (it is
            # the same text appended to a surviving order's `reasoning`), so
            # it is reused verbatim as the refusal detail rather than
            # re-deriving which cap fired.
            self._note_refusal(
                target.symbol, target.direction, STOP_REFUSAL_SIZED_TO_ZERO,
                (cap_note.strip() or (
                    "the position sizing chain (risk budget, single-name "
                    "ceiling, sector crowding) left nothing to round to "
                    "above zero"
                )),
            )
            return None

        reasoning = target.thesis
        falsifier = stated_soft_exit(target.thesis_invalid_if)
        catalyst = stated_soft_exit(target.catalyst)
        if falsifier:
            reasoning += f" (invalid if: {falsifier})"
        if catalyst:
            reasoning += f" (catalyst: {catalyst})"

        return TradeDecision(
            action="BUY",
            symbol=target.symbol,
            allocation_pct=allocation_pct,
            entry_price=entry_price,
            stop_loss=stop_loss,   # already rounded + validated above
            take_profit=take_profit,
            # Cap note appended AFTER the truncation so provenance never
            # gets sliced off by a long thesis. The budget note (spec §2.2)
            # rides alongside it for the same reason: a portfolio-level cut
            # the AI Risk Manager cannot see the arithmetic behind reads as
            # the PM contradicting itself.
            reasoning=reasoning[:500] + cap_note + (
                f" {plan.note}" if plan is not None and plan.note else ""
            ) + self._target_note(derivation),
            # Conviction ledger (spec §7.2) — pinned at entry, never
            # recomputed. `plan` is None for a legacy notional target, so
            # `allocated_risk_pct` stays None rather than a fabricated
            # figure the budget never actually granted.
            conviction=target.conviction,
            requested_risk_pct=target.risk_allocation_pct,
            allocated_risk_pct=plan.risk_pct if plan is not None else None,
            # Carried so the execution stage does not re-widen a stop this
            # constructor deliberately honoured at a computed level.
            stop_rule=self.shipped_stop_rule(
                analysis, entry_price, stop_loss, target.direction,
            ),
            # Carried for the SAME reason as stop_rule: so the execution
            # stage's own reward:risk belt does not kill an order that was
            # deliberately permitted below the floor. See
            # TradeDecision.subfloor_catalyst_exception.
            subfloor_catalyst_exception=bool(
                getattr(target, "subfloor_catalyst_verified", False)
            ),
            # Carried for the SAME reason as stop_rule: how this position is
            # MANAGED decides whether a reward:risk figure means anything at
            # all downstream. See TradeDecision.setup_type.
            setup_type=getattr(analysis, "setup_type", None),
            # Real, untruncated field alongside the embedded-in-reasoning
            # text above — see TradeDecision.thesis_invalid_if.
            thesis_invalid_if=stated_soft_exit(target.thesis_invalid_if) or None,
        )

    def _build_short(
        self,
        target: TargetPosition,
        analysis: TechAnalysisResult | None,
        current_pct: float,
        target_pct: float,
        total_value: float,
        market_price: float | None,
        plan: RiskPlan | None = None,
        regime: str | None = None,
        sector_weights: dict[tuple[str, str], float] | None = None,
    ) -> TradeDecision | None:
        """The BUY-side mirror (Stage 3, D1): open or add to a short.

        `current_pct` and `target_pct` are both SIGNED and <= 0 (the
        `construct_orders` dispatch only reaches this builder when the
        position is flat-or-short and the signed target does not cross zero
        to the long side).
        """
        if plan is not None and plan.entry_price is not None and plan.stop_price is not None:
            entry_price, stop_loss = plan.entry_price, plan.stop_price
        else:
            entry_price, stop_loss = self._resolve_entry_and_stop(
                target, analysis, market_price, regime=regime,
            )
            if entry_price is None or stop_loss is None:
                # drop-reason: delegated — every exit from
                # `_resolve_entry_and_stop` files a fault or a named refusal.
                return None

        # Take-profit: COMPUTED from structure and BELOW entry for a short
        # (price must FALL for a short to profit) — the exact mirror of
        # _build_buy, using the same derivation. The direction inversion
        # lives inside `derive_structural_target`: it draws from levels below
        # the entry and projects a measured move downward, so nothing here
        # has to know which way the trade points beyond passing
        # `target.direction` through.
        derivation = self._derive_target(
            target.symbol, analysis, entry_price, target.direction,
        )
        if derivation.price is None or derivation.price >= entry_price:
            # Mirror of `_build_buy`: a data fault is already recorded and
            # logged as UNMEASURABLE; only a real refusal is a rejection.
            if not derivation.fault:
                # Board item 10 (2026-09-14, second pass) — mirror of
                # `_build_buy`. See the comment there.
                self._note_refusal(
                    target.symbol, target.direction,
                    derivation.refusal or STOP_REFUSAL_TARGET_NOT_BELOW_ENTRY,
                    f"no take-profit could be computed below the "
                    f"${entry_price:,.2f} entry for this short"
                    + (f": {derivation.detail}" if derivation.detail else
                       f" (computed {derivation.price})"),
                )
            return None
        take_profit = float(derivation.price)

        from src.risk.rules import _gross_multiplier
        gross_mul = _gross_multiplier(target.symbol)
        # Both current_pct and target_pct are signed and <= 0 here; moving
        # FURTHER from zero (more negative) is what grows the short, so the
        # raw notional delta is `current - target` (positive when growing).
        allocation_pct = (current_pct - target_pct) / gross_mul

        # D4: unsigned risk-per-share (stop sits ABOVE entry for a short).
        risk_per_share = abs(entry_price - stop_loss)
        # D8: gap-risk sizing haircut — SIZING ONLY, never stop placement
        # (the stop above was already resolved before this line runs). A
        # short gaps through its stop upward with no bound, so the same
        # nominal risk allocation must open a SMALLER short than an
        # equivalent long at the same stop distance. Paper trading fills
        # unrealistically through a gap on IEX data with no borrow-cost
        # model, so this haircut is what keeps the measured size honest
        # relative to what live capital would actually risk.
        risk_per_share *= self.cfg.short_gap_risk_multiple
        risk_dollars_allowed = total_value * self.cfg.risk_budget_pct / 100
        cap_note = ""
        if risk_per_share > 0:
            alloc_cap_by_risk = (
                risk_dollars_allowed * entry_price / risk_per_share / total_value * 100
            )
            if allocation_pct > alloc_cap_by_risk:
                logger.info(
                    "Constructor: SHORT %s alloc capped by risk budget "
                    "(delta %.2f%% → %.2f%% at %.1f%% risk budget, %.1fx "
                    "gap-risk haircut)",
                    target.symbol, allocation_pct, alloc_cap_by_risk,
                    self.cfg.risk_budget_pct, self.cfg.short_gap_risk_multiple,
                )
                cap_note = (
                    f" [constructor: PM target delta {allocation_pct:.2f}% "
                    f"capped to {alloc_cap_by_risk:.2f}% by the "
                    f"{self.cfg.risk_budget_pct:.1f}% risk budget (x"
                    f"{self.cfg.short_gap_risk_multiple:.1f} gap-risk haircut) "
                    f"— the size difference vs PM's stated weight is "
                    f"deterministic, not PM inconsistency]"
                )
                allocation_pct = alloc_cap_by_risk

        # Single-name ceiling — the SAME `max_position_pct` a long uses
        # (owner decision 2026-09-17: shorts carry the same limits as longs).
        # Mirrors _build_buy's max_position_pct clamp so the constructor
        # sizes UNDER the risk engine's hard block instead of proposing an
        # order the engine will drop outright.
        current_short_gross_pct = abs(current_pct)  # already gross-scaled, <= 0
        name_headroom_pct = (self.cfg.max_position_pct - current_short_gross_pct) / gross_mul
        if allocation_pct > name_headroom_pct:
            logger.info(
                "Constructor: SHORT %s alloc capped by the single-name "
                "ceiling (delta %.2f%% → %.2f%%; %.1f%% max position, %.2f%% "
                "already held)",
                target.symbol, allocation_pct, max(0.0, name_headroom_pct),
                self.cfg.max_position_pct, current_short_gross_pct,
            )
            cap_note += (
                f" [constructor: size capped to {max(0.0, name_headroom_pct):.2f}% "
                f"by the {self.cfg.max_position_pct:.0f}% single-name "
                f"ceiling (the same one a long uses). "
                f"Deterministic, not PM inconsistency]"
            )
            allocation_pct = name_headroom_pct

        # Spec §10.3, identical to `_build_buy`: a short crowds its sector
        # exactly as a long does, and gets scaled for crowding exactly as a
        # long does. Spec §12.2 is what differs — the budget it is measured
        # against is the SHORT side's own, not a shared gross bucket. That is
        # what keeps a pair trade (long the leader, short the laggard in one
        # hot sector) legal: two opportunities that share a label, not a hedge
        # and not one budget.
        if sector_weights is not None:
            allocation_pct, sector_note = self._apply_sector_dial(
                target.symbol, allocation_pct,
                sector_weights=sector_weights, total_value=total_value,
                action="SHORT",
            )
            cap_note += sector_note
            if allocation_pct < 0:
                # drop-reason: delegated — `_apply_sector_dial` files the
                # refusal, because only it knows which of its two ends fired.
                return None

        allocation_pct = max(0.0, round(allocation_pct, 2))
        if allocation_pct <= 0:
            # Board item 10 (2026-09-14) — see the identical comment in
            # `_build_buy`. The risk-budget-per-trade cap and the single-
            # short ceiling above both LOG when they shrink a request
            # without saying rejected/refused/skipped, so nothing here would
            # otherwise reach `_DropReasonCapture` or `last_refusals`.
            self._note_refusal(
                target.symbol, target.direction, STOP_REFUSAL_SIZED_TO_ZERO,
                (cap_note.strip() or (
                    "the position sizing chain (risk budget, single-name "
                    "ceiling, sector crowding) left nothing to round to "
                    "above zero"
                )),
            )
            return None

        reasoning = target.thesis
        falsifier = stated_soft_exit(target.thesis_invalid_if)
        catalyst = stated_soft_exit(target.catalyst)
        if falsifier:
            reasoning += f" (invalid if: {falsifier})"
        if catalyst:
            reasoning += f" (catalyst: {catalyst})"

        return TradeDecision(
            action="SHORT",
            symbol=target.symbol,
            allocation_pct=allocation_pct,
            entry_price=entry_price,
            stop_loss=stop_loss,   # already rounded + validated above
            take_profit=take_profit,
            reasoning=reasoning[:500] + cap_note + (
                f" {plan.note}" if plan is not None and plan.note else ""
            ) + self._target_note(derivation),
            # Conviction ledger (spec §7.2) — mirrors _build_buy's entry
            # pinning; see its comment for what each field means.
            conviction=target.conviction,
            requested_risk_pct=target.risk_allocation_pct,
            allocated_risk_pct=plan.risk_pct if plan is not None else None,
            # Carried so the execution stage does not re-widen a stop this
            # constructor deliberately honoured at a computed level.
            stop_rule=self.shipped_stop_rule(
                analysis, entry_price, stop_loss, target.direction,
            ),
            # Carried for the SAME reason as stop_rule: so the execution
            # stage's own reward:risk belt does not kill an order that was
            # deliberately permitted below the floor. See
            # TradeDecision.subfloor_catalyst_exception.
            subfloor_catalyst_exception=bool(
                getattr(target, "subfloor_catalyst_verified", False)
            ),
            # Carried for the SAME reason as stop_rule: how this position is
            # MANAGED decides whether a reward:risk figure means anything at
            # all downstream. See TradeDecision.setup_type.
            setup_type=getattr(analysis, "setup_type", None),
            # Real, untruncated field alongside the embedded-in-reasoning
            # text above — see TradeDecision.thesis_invalid_if.
            thesis_invalid_if=stated_soft_exit(target.thesis_invalid_if) or None,
        )

    def _resolve_stop(
        self,
        target: TargetPosition,
        analysis: TechAnalysisResult | None,
        entry_price: float,
    ) -> float | None:
        """Priority: target's suggested stop → the technical analyst's stop →
        None, which `_widen_stop_past_noise` then READS FROM THE INSTRUMENT.

        Until 2026-09-12 a None here was a refusal ("no synthesized
        fallback"), on the argument that a stop nobody derived from the
        chart cannot be risk-sized honestly. Item 54 (docs/WORK.md) replaced
        that: the stop is always derivable — the wider of the ATR noise band
        and the signal bar's far edge, both read from the bars — and the
        thing that cannot be sized honestly is a stop WIDER than the
        instrument's own range, which the width gate refuses. The old flat
        `entry * 0.95` fallback stays gone; nothing here is a percentage.
        """
        if target.suggested_stop_price and target.suggested_stop_price > 0:
            return float(target.suggested_stop_price)
        if analysis and analysis.stop_loss and analysis.stop_loss > 0:
            return float(analysis.stop_loss)
        logger.info(
            "Constructor: %s has no typed stop (suggested_stop_price and "
            "analysis.stop_loss both absent) — it will be read from the "
            "instrument (item 54).",
            target.symbol,
        )
        return None
