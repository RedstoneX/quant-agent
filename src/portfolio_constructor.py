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
from dataclasses import dataclass

from src.data.levels import TargetDerivation, derive_structural_target
from src.models import Position, TargetPosition, TechAnalysisResult, TradeDecision

logger = logging.getLogger(__name__)


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
STOP_REFUSAL_WRONG_SIDE = "stop_on_wrong_side_of_entry"
STOP_REFUSAL_GEOMETRY_AT_BAND = "reward_risk_below_floor_at_widened_stop"
STOP_REFUSAL_GEOMETRY_AT_LEVEL = "reward_risk_below_floor_at_honoured_stop"


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
    # will hard-block. Risk-based sizing (§2.1) makes this binding in the
    # ordinary case, not the exotic one: notional = risk_pct x entry/(entry -
    # stop), so at this book's median 4.3% stop distance even 1.5% risk asks
    # for 35% of equity in one name. `max_position_pct` is in
    # HARD_BLOCK_RULES, so without this clamp those BUYs are dropped entirely
    # and the session trades nothing. Keep in sync with
    # `risk.max_position_pct` — pipeline.py wires them from the same setting.
    max_position_pct: float = 20.0
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
    # Stage 3 (shorts). Mirrors `max_position_pct` for a short's single-name
    # ceiling — deliberately HALF of it (see src/config.py for why) — so
    # `_build_short` sizes UNDER the risk engine's hard block instead of
    # proposing an order the engine will drop outright. Keep in sync with
    # `risk.max_single_short_pct` — pipeline.py wires them from the same
    # setting, the same way it already does for `max_position_pct`.
    max_single_short_pct: float = 10.0
    # Stage 3 (shorts). SIZING ONLY (never applied to stop placement — see
    # `_widen_stop_past_noise`): a short's risk-per-share is multiplied by
    # this before it is converted to a weight, so the same risk allocation
    # opens a smaller short than an equivalent long. Keep in sync with
    # `risk.short_gap_risk_multiple`.
    short_gap_risk_multiple: float = 1.5
    # Spec §9.4 "agreement earns size". Ceiling on a risk-based target's
    # `risk_allocation_pct`, indexed by the number of independent seats
    # whose canonical stance is directionally aligned with the target
    # (see `src/risk/rules.py::count_aligned_sources` /
    # `agreement_ceiling_for_count`, and `RiskConfig.agreement_ceiling_pct`
    # in `src/config.py` for the measurement behind these numbers).
    # Applied in `_plan_risk_targets`, strictly BEFORE `allocate_risk_budget`
    # and the single-name clamps below — it can only ever REDUCE what a
    # target receives, never raise it, and never past `risk_budget_pct`.
    # Kept in sync with `risk.agreement_ceiling_pct` — pipeline.py wires
    # them from the same setting, same pattern as every other ceiling here.
    agreement_ceiling_pct: tuple[float, ...] = (3.0, 4.0, 5.0, 5.0, 5.0)
    # Minimum stop distance, in ATRs. A stop inside ordinary volatility is not
    # a thesis invalidation, it is a coin flip on noise — Phase 3 already
    # established 1.25 ATR as one ordinary day's range for a TRAILING stop,
    # and an entry stop has to survive the whole expected hold, not one
    # session. Measured 2026-08-27: the book's stops sat a median 4.3% below
    # entry against a median ATR of 2.56% of price — about 1.7 ATRs, barely
    # more than a single day. Structure still places the stop; this only
    # pushes it out when structure put it inside the noise.
    # This is a BASE, not a constant. `_stop_atr_multiple` adjusts it per
    # trade — a breakout has a clean invalidation level and does not need the
    # room a range setup does, and a risk-off tape chops harder than a
    # trending one. ATR itself already adapts the distance to each stock and
    # each session; these adjust how many ATRs that stock's setup deserves.
    min_stop_atr_multiple: float = 3.0
    #: Multipliers ON the base, by `TechAnalysisResult.setup_type`. Breakout
    #: invalidation is a level ("back below the breakout"), so it earns a
    #: tighter stop than a range trade being shaken out inside its own band.
    #: Same keying Phase 3's deterministic trailing already uses.
    stop_atr_setup_scale: tuple[tuple[str, float], ...] = (
        ("breakout", 0.85),
        ("range", 1.15),
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
    # Below this the trade only ever looked good on a stop too tight to
    # survive, so it is rejected rather than taken at a worse payoff.
    min_reward_risk_after_widening: float = 1.5
    # --- Level-backed stops (spec §12.1, 2026-09-01) --------------------
    # How close the stop must sit to a level `find_structural_levels`
    # actually COMPUTED before `_widen_stop_past_noise` treats it as sitting
    # AT that level and honours it whatever the band says.
    #
    # ATR-relative rather than a percentage, deliberately. The question is
    # "did the analyst place this stop at that level?", which is a question
    # about the name's own price noise — and ATR is the unit every other
    # stop rule in this file already speaks (`min_stop_atr_multiple`,
    # `min_target_atr_multiple`, Phase 3's trailing band). A flat percentage
    # would be too tight to ever match on a 9%-ATR small cap and loose
    # enough on a 1.5%-ATR utility to match a level the stop is nowhere
    # near, so the SAME tolerance would mean two different things. A
    # computed level is also a ZONE, not a number — `find_structural_levels`
    # clusters pivots within `CLUSTER_TOLERANCE_PCT` (1%) into one level —
    # so the tolerance has to be at least as wide as that zone.
    # Kept in sync with `risk.level_match_atr_tolerance`.
    level_match_atr_tolerance: float = 0.25
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
    max_target_horizon_sessions: int = 60
    # The model's target is not thrown away — it becomes evidence. Above this
    # absolute percentage gap between the computed target and the model's
    # guess, the disagreement is logged at WARNING rather than INFO, because
    # a model that is consistently far from the chart is a finding about the
    # model, not about the trade.
    target_divergence_warn_pct: float = 25.0
    # Minimum delta to trigger a rebalance order (avoid tiny 0.2% churn trades).
    min_trade_weight_delta: float = 0.5
    # NOTE (2026-08-27): the ATR-multiple and naive-percent stop fallbacks were
    # REMOVED. They were never the intended design — `_resolve_stop` always
    # preferred the analyst's structural level and fell through to
    # `entry - 2*ATR` (then `entry * 0.95`) only when none was supplied. In
    # practice the analyst supplied none, so the fallback became the norm and
    # positions were managed against stops nobody derived from the chart.
    # A stop must now come from structure; without one there is no trade.
    # ATR is retained only as a noise-band input for exit decisions.
    # See docs/QAMC_REMEDIATION_SPEC.md Phase 1.


class PortfolioConstructor:
    """Stateless translator: target state → concrete orders."""

    def __init__(self, config: ConstructorConfig | None = None):
        self.cfg = config or ConstructorConfig()

    def construct_orders(
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
    ) -> list[TradeDecision]:
        """Produce the order list that moves the book from current → target state.

        Orders are returned in a canonical order: exits (SELL/COVER, partials
        and full closes) first, then entries (BUY/SHORT). Execution layer is
        free to re-order, but this matches the existing pipeline assumption
        (exits free up capacity first).

        `price_map`: optional {symbol: live_price} — required for BUYs so
        the constructor can sanity-check TA's entry. If absent for a BUY
        symbol, we fall back to TA's entry_price.

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
        """
        if total_value <= 0:
            return []
        price_map = price_map or {}
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
                    # _plan_risk_targets has already logged which.
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
                signed_target = 0.0

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
                continue

            if delta_pct < 0:
                if current_pct > 0:
                    # Trim or close a LONG.
                    sell_decision = self._build_sell(
                        target, positions_by_sym.get(sym), current_pct, signed_target,
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
        return sells + buys

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

        Spec §9.4: before EITHER of the above, each request is additionally
        ceilinged by how many independent sources agree with the target's
        direction (`evidence_registry` + `count_aligned_sources`). This
        composes with, and is applied strictly BEFORE, the envelope clamp
        and `allocate_risk_budget` — a reduction only, never a multiplier:
        it can only ever refuse size a request did not earn agreement for.
        """
        from src.risk.budget import RiskRequest, allocate_risk_budget
        from src.risk.rules import (
            _gross_multiplier, agreement_ceiling_for_count, count_aligned_sources,
        )

        priced: dict[str, tuple[float, float]] = {}   # symbol -> (entry, stop)
        # Stage 3: direction is tracked alongside the priced entry/stop so
        # the weight formula below can pick the right (unsigned) risk-per-
        # share denominator and apply the short sizing haircut. The RESULT
        # (`target_weight_pct`) stays an unsigned magnitude either way — the
        # delta loop in `construct_orders` applies the sign from
        # `target.direction`.
        directions: dict[str, str] = {}
        requests: list[RiskRequest] = []
        closes: set[str] = set()
        # §9.4 provenance for the AI Risk Manager, same reason every other
        # deterministic cut here carries one: an unexplained size difference
        # between PM's stated allocation and the constructed order reads as
        # PM contradicting itself (2026-08-20 incident), not as arithmetic.
        agreement_notes: dict[str, str] = {}

        for target in targets:
            if target.risk_allocation_pct is None:
                continue  # legacy notional target — sized the old way
            sym = target.symbol
            if target.risk_allocation_pct == 0.0:
                # A close needs no price, no stop and no budget. Routing it
                # through the pricing checks below would let a missing quote
                # silently cancel an exit PM had decided on.
                closes.add(sym)
                continue
            analysis = analyses_by_sym.get(sym)
            entry, stop = self._resolve_entry_and_stop(
                target, analysis, price_map.get(sym), regime=regime,
            )
            if entry is None or stop is None:
                continue  # already logged; no stop means no honest size
            priced[sym] = (entry, stop)
            directions[sym] = target.direction

            # The single-name envelope binds before the portfolio one. A PM
            # asking for more than the ratified envelope is clamped rather
            # than refused — the idea is sound, the size is not.
            envelope_capped = min(target.risk_allocation_pct, self.cfg.risk_budget_pct)
            # §9.4: then the agreement ceiling, computed from THIS session's
            # canonical registry (not from target.provenance — see
            # `count_aligned_sources`), before the request ever reaches the
            # portfolio-level budget allocator. Same "no view, don't invent
            # one" posture as `existing_risk_pct`/`clusters` above: when the
            # caller has no registry to offer, the ceiling is UNENFORCED
            # (infinite), never silently treated as zero agreement — a
            # missing registry is not evidence of disagreement.
            agreement_count: int | None = None
            if evidence_registry is not None:
                sources = evidence_registry.get(sym.upper(), {})
                agreement_count = count_aligned_sources(sym, sources, target.direction)
                agreement_ceiling = agreement_ceiling_for_count(
                    self.cfg.agreement_ceiling_pct, agreement_count,
                )
            else:
                agreement_ceiling = float("inf")
            requested_pct = min(envelope_capped, agreement_ceiling)
            if agreement_ceiling < envelope_capped:
                logger.info(
                    "Constructor: %s risk capped by the agreement ceiling "
                    "(%.2f%% → %.2f%%; %d independent source(s) aligned "
                    "with this %s)",
                    sym, envelope_capped, requested_pct, agreement_count,
                    target.direction,
                )
                agreement_notes[sym] = (
                    f"[constructor: {sym} risk capped to {requested_pct:.2f}% "
                    f"by the agreement ceiling — only {agreement_count} "
                    f"independent source(s) align with this {target.direction}, "
                    f"below the {envelope_capped:.2f}% the envelope alone would "
                    "allow. More independent confirmation earns more of the "
                    "risk budget; this idea earned less. Deterministic, not "
                    "PM inconsistency]"
                )
            requests.append(RiskRequest(sym, requested_pct))

        allocation = allocate_risk_budget(
            requests,
            existing_pct=existing_risk_pct,
            clusters=clusters,
            ceiling_pct=self.cfg.max_portfolio_risk_pct,
            cluster_share_pct=self.cfg.max_cluster_risk_share_pct,
            floor_pct=self.cfg.min_risk_pct,
        ) if (existing_risk_pct is not None or clusters is not None) else None

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
            # §9.4 note (if the agreement ceiling bound) always leads —
            # order matters for audit readability, not correctness: it
            # explains why the REQUEST itself was already smaller before
            # the budget allocator ever saw it.
            note_parts = [agreement_notes[sym]] if sym in agreement_notes else []
            if allocation is not None:
                grant = allocation.grants.get(sym.upper())
                granted = grant.granted_pct if grant else 0.0
                if grant and grant.note:
                    note_parts.append(grant.note)
                if granted <= 0:
                    logger.info(
                        "Constructor: %s produces no order — risk budget "
                        "granted 0%% of the %.2f%% requested (%s)",
                        sym, requested,
                        grant.limited_by if grant else "no grant",
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
            )
        return plans

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
            logger.warning(
                "Constructor: cannot construct %s %s — no entry price available",
                "SHORT" if is_short else "BUY", target.symbol,
            )
            return (None, None)

        # Round FIRST, then validate: the TradeDecision ships
        # round(stop_loss, 2), so validating the unrounded value let a stop
        # that rounds UP to exactly the entry price through the
        # `stop_loss < entry_price` check (e.g. entry $10.00, stop $9.999 →
        # ships $10.00 == entry → risk_per_share = 0, and a stop at the entry
        # fires on the first tick down). 2026-07-16 audit.
        # The target is derived BEFORE the stop is finalised, because the
        # reward:risk check inside `_widen_stop_past_noise` needs a real
        # target to measure against. It depends only on entry, direction and
        # the chart — never on the stop — so there is no circularity.
        derivation = self._derive_target(
            target.symbol, analysis, entry_price, target.direction,
        )
        if derivation.price is None:
            logger.warning(
                "Constructor: %s %s rejected — no target could be computed "
                "from structure [%s]: %s",
                "SHORT" if is_short else "BUY", target.symbol,
                derivation.refusal, derivation.detail,
            )
            return (None, None)

        stop_loss = self._resolve_stop(target, analysis, entry_price)
        stop_loss = self._widen_stop_past_noise(
            target.symbol, analysis, entry_price, stop_loss, regime=regime,
            direction=target.direction, target_price=derivation.price,
        )
        if stop_loss is not None:
            stop_loss = round(stop_loss, 2)
        if is_short:
            invalid = stop_loss is None or stop_loss <= 0 or stop_loss <= entry_price
        else:
            invalid = stop_loss is None or stop_loss <= 0 or stop_loss >= entry_price
        if invalid:
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
        scales how many of them the setup earns: a breakout invalidates at a
        level and does not need range-trade room, and a risk-off tape swings
        wider for the same ATR reading than a trending one does.
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
        atr: float,
        is_short: bool,
    ) -> float | None:
        """The computed structural level this stop sits at, or None.

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
        stop is actually sitting on.
        """
        raw_levels = getattr(analysis, "computed_levels", None) or []
        tolerance = self.cfg.level_match_atr_tolerance * atr
        if tolerance <= 0:
            return None

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

        None when the target is missing or points the wrong way, which is
        the existing "cannot judge it, do not invent a judgement" posture —
        callers treat that as "no opinion", not as a failure.
        """
        if not target_price or stop_price <= 0 or entry_price <= 0:
            return None
        if is_short:
            if target_price >= entry_price:
                return None
            risk = stop_price - entry_price
            reward = entry_price - target_price
        else:
            if target_price <= entry_price:
                return None
            risk = entry_price - stop_price
            reward = target_price - entry_price
        if risk <= 0:
            return None
        return reward / risk

    def _widen_stop_past_noise(
        self,
        symbol: str,
        analysis: TechAnalysisResult | None,
        entry_price: float,
        stop_loss: float | None,
        regime: str | None = None,
        direction: str = "long",
        target_price: float | None = None,
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
        rule. `min_reward_risk_after_widening` applies identically to a
        short: a widened short stop that drops reward:risk below the floor
        rejects the trade exactly as it would for a long.

        Returns None when widening would leave a reward:risk the trade cannot
        justify. That is deliberate: a trade that only cleared the bar on a
        stop too tight to survive was never the trade it appeared to be.

        `target_price` (2026-09-01) is the DERIVED target — computed from
        structure by `_derive_target`. It has to be passed in rather than
        read off the analysis, because the whole point of the change is that
        `analysis.reference_target` is the language model's guess and this
        gate is arithmetic. When it is omitted the old read is kept, for the
        one caller that legitimately supplies its own structural target: the
        backtest engine, which computes the nearest level itself and hands it
        over on a shim (`src/backtest/engine.py`). That path was never
        exposed to the defect.
        """
        if stop_loss is None or stop_loss <= 0 or entry_price <= 0:
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
        if is_short and stop_loss <= entry_price:
            logger.warning(
                "Constructor: SHORT %s refused [%s] — the stop $%.2f is at or "
                "below the entry $%.2f, so it protects nothing. Refusing "
                "rather than widening it into validity.",
                symbol, STOP_REFUSAL_WRONG_SIDE, stop_loss, entry_price,
            )
            return None
        if not is_short and stop_loss >= entry_price:
            logger.warning(
                "Constructor: BUY %s refused [%s] — the stop $%.2f is at or "
                "above the entry $%.2f, so it protects nothing. Refusing "
                "rather than widening it into validity.",
                symbol, STOP_REFUSAL_WRONG_SIDE, stop_loss, entry_price,
            )
            return None

        atr = getattr(analysis, "atr_14", None) if analysis else None
        try:
            atr = float(atr) if atr is not None else None
        except (TypeError, ValueError):
            atr = None
        if atr is None or not math.isfinite(atr) or atr <= 0:
            return stop_loss  # no volatility reading — leave structure alone

        multiple = self._stop_atr_multiple(analysis, regime)
        if is_short:
            band_edge = entry_price + multiple * atr
            if stop_loss >= band_edge:
                return stop_loss  # already outside the noise band
        else:
            band_edge = entry_price - multiple * atr
            if band_edge <= 0 or stop_loss <= band_edge:
                return stop_loss  # already outside the noise band

        if target_price is None and analysis is not None:
            target_price = getattr(analysis, "reference_target", None)
        try:
            target_price = float(target_price) if target_price else None
        except (TypeError, ValueError):
            target_price = None

        floor = self.cfg.min_reward_risk_after_widening
        side_word = "above" if is_short else "below"

        # -------------------------------------------------------------
        # §12.1 — is this stop sitting on something the system computed?
        # -------------------------------------------------------------
        level = self._level_backing_stop(
            analysis, entry_price, stop_loss, atr, is_short,
        )
        if level is not None:
            honoured = stop_loss
            floor_multiple = self.cfg.absolute_min_stop_atr_multiple
            hard_floor = (
                entry_price + floor_multiple * atr if is_short
                else entry_price - floor_multiple * atr
            )
            inside_hard_floor = (
                floor_multiple > 0
                and hard_floor > 0
                and (stop_loss < hard_floor if is_short else stop_loss > hard_floor)
            )
            if inside_hard_floor:
                # Real structure, still too close to survive one ordinary
                # session. Pushed out to the 1x floor and NOT to the full
                # band — the band is what §12.1 removed. See the docstring
                # for why this floor exists in code rather than in a prompt.
                honoured = hard_floor
                logger.info(
                    "Constructor: %s %s stop $%.2f → $%.2f [%s] — it sits at "
                    "the computed structural level $%.2f, which is real, but "
                    "only %.2f ATRs from the $%.2f entry. A stop inside one "
                    "ordinary day's range is a coin flip, so it is moved out "
                    "to the %.2f x ATR floor — not to the %.2f x ATR noise "
                    "band, which the level exempts it from.",
                    "SHORT" if is_short else "BUY", symbol, stop_loss,
                    honoured, STOP_RULE_ABSOLUTE_FLOOR, level,
                    abs(entry_price - stop_loss) / atr, entry_price,
                    floor_multiple, multiple,
                )
            else:
                logger.info(
                    "Constructor: %s %s stop $%.2f kept [%s] — it sits at the "
                    "computed structural level $%.2f (%.2f ATRs %s the $%.2f "
                    "entry). The %.2f x ATR noise band would have moved it to "
                    "$%.2f, which is not a level anyone is defending, so the "
                    "band does not apply.",
                    "SHORT" if is_short else "BUY", symbol, stop_loss,
                    STOP_RULE_LEVEL_HONOURED, level,
                    abs(entry_price - stop_loss) / atr, side_word, entry_price,
                    multiple, band_edge,
                )

            # The whole point of §12.1: reward:risk is measured against the
            # stop that will ACTUALLY ship. Judging it against a stop nobody
            # will ever use is the defect being removed. The floor itself is
            # untouched at 1.5 — a level-backed trade whose real geometry
            # still does not work is still refused.
            reward_risk = self._reward_risk_at(
                entry_price, honoured, target_price, is_short,
            )
            if reward_risk is not None and reward_risk < floor:
                logger.info(
                    "Constructor: %s %s refused [%s] — against the stop it "
                    "will actually use ($%.2f, at the computed level $%.2f) "
                    "the computed target $%.2f is reward:risk %.2f, under "
                    "the %.2f minimum. Both numbers are measured, and this "
                    "is the real geometry of the trade, not a widened one — "
                    "the entry does not support it.",
                    "SHORT" if is_short else "BUY", symbol,
                    STOP_REFUSAL_GEOMETRY_AT_LEVEL, honoured, level,
                    target_price, reward_risk, floor,
                )
                return None
            return honoured

        # -------------------------------------------------------------
        # Nothing computed backs this stop — unchanged behaviour.
        # -------------------------------------------------------------
        # The refusal below is about GEOMETRY, and says so. Both sides of
        # the ratio are computed from measured volatility: the stop is
        # `min_stop_atr_multiple` ATRs out, the target is where structure or
        # the horizon says price travels. When those two cannot clear the
        # floor together, the trade is refused because its shape does not
        # work at this entry — NOT because a model guessed a poor target,
        # which is what this message used to mean and no longer does.
        reward_risk = self._reward_risk_at(
            entry_price, band_edge, target_price, is_short,
        )
        if reward_risk is not None and reward_risk < floor:
            logger.info(
                "Constructor: %s %s refused [%s] — no computed structural "
                "level sits at the $%.2f stop, so it is widened to the "
                "%.2f x ATR noise band at $%.2f. Against that the computed "
                "target $%.2f is reward:risk %.2f, under the %.2f minimum. "
                "Both numbers are measured; this entry does not support the "
                "trade.",
                "SHORT" if is_short else "BUY", symbol,
                STOP_REFUSAL_GEOMETRY_AT_BAND, stop_loss, multiple, band_edge,
                target_price, reward_risk, floor,
            )
            return None

        logger.info(
            "Constructor: %s %s stop widened $%.2f → $%.2f (%.1f%% %s entry) "
            "[%s] — no computed structural level sits at it, and it was "
            "placed inside %.2f x ATR of $%.2f (%s setup, %s tape). A stop "
            "nothing on the chart defends does not earn the §12.1 exemption.",
            "SHORT" if is_short else "BUY", symbol, stop_loss, band_edge,
            100 * abs(entry_price - band_edge) / entry_price, side_word,
            STOP_RULE_ATR_BAND, multiple, atr,
            getattr(analysis, "setup_type", None) or "unknown",
            regime or "unknown",
        )
        return band_edge

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
        from src.risk.rules import _gross_multiplier
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
            p.symbol: (p.market_value * _gross_multiplier(p.symbol) / total_value * 100)
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
            logger.info(
                "Constructor: %s refused — sector '%s' %s side is at %.1f%% "
                "gross, at or past the %.0f%% absolute ceiling; no size is "
                "available.",
                symbol, sector, side, current_pct, self.cfg.max_sector_hard_pct,
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
            logger.info(
                "Constructor: %s refused — sector '%s' at %.1f%% leaves only "
                "%.2f%% (~$%.0f), under the $%.0f minimum order.",
                symbol, sector, current_pct, final, notional,
                self.cfg.min_order_usd,
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
        if target_pct == 0:
            # Full close
            alloc = 100.0
        else:
            # Partial: sell enough to land on target_pct
            # fraction to sell = (current - target) / current
            fraction = (current_pct - target_pct) / current_pct
            alloc = max(1.0, min(99.0, round(fraction * 100, 1)))
        reasoning = target.thesis
        if target.thesis_invalid_if:
            reasoning += f" (thesis_invalid_if: {target.thesis_invalid_if})"
        # SELLs don't need live entry/stop/target — execution uses market price
        return TradeDecision(
            action="SELL",
            symbol=target.symbol,
            allocation_pct=alloc,
            entry_price=0.0,
            stop_loss=0.0,
            take_profit=0.0,
            reasoning=reasoning[:500],
        )

    @staticmethod
    def _build_cover(
        target: TargetPosition,
        position: Position | None,
        current_pct: float,
        target_pct: float,
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
        if target_pct == 0:
            # Full cover
            alloc = 100.0
        else:
            # Partial: buy back enough to land on target_pct.
            fraction = (current_pct - target_pct) / current_pct
            alloc = max(1.0, min(99.0, round(fraction * 100, 1)))
        reasoning = target.thesis
        if target.thesis_invalid_if:
            reasoning += f" (thesis_invalid_if: {target.thesis_invalid_if})"
        # COVERs don't need live entry/stop/target — execution uses market price
        return TradeDecision(
            action="COVER",
            symbol=target.symbol,
            allocation_pct=alloc,
            entry_price=0.0,
            stop_loss=0.0,
            take_profit=0.0,
            reasoning=reasoning[:500],
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
            logger.warning(
                "Constructor: BUY %s rejected — no target could be computed "
                "above entry $%.2f [%s]: %s",
                target.symbol, entry_price,
                derivation.refusal or "target_not_above_entry", derivation.detail,
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
                return None

        allocation_pct = max(0.0, round(allocation_pct, 2))
        if allocation_pct <= 0:
            return None

        reasoning = target.thesis
        if target.thesis_invalid_if:
            reasoning += f" (invalid if: {target.thesis_invalid_if})"
        if target.catalyst:
            reasoning += f" (catalyst: {target.catalyst})"

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
            logger.warning(
                "Constructor: SHORT %s rejected — no target could be computed "
                "below entry $%.2f [%s]: %s",
                target.symbol, entry_price,
                derivation.refusal or "target_not_below_entry", derivation.detail,
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

        # D9: single-short ceiling — deliberately HALF of the long
        # single-name ceiling (see ConstructorConfig.max_single_short_pct).
        # Mirrors _build_buy's max_position_pct clamp so the constructor
        # sizes UNDER the risk engine's hard block instead of proposing an
        # order the engine will drop outright.
        current_short_gross_pct = abs(current_pct)  # already gross-scaled, <= 0
        name_headroom_pct = (self.cfg.max_single_short_pct - current_short_gross_pct) / gross_mul
        if allocation_pct > name_headroom_pct:
            logger.info(
                "Constructor: SHORT %s alloc capped by the single-short "
                "ceiling (delta %.2f%% → %.2f%%; %.1f%% max short, %.2f%% "
                "already held)",
                target.symbol, allocation_pct, max(0.0, name_headroom_pct),
                self.cfg.max_single_short_pct, current_short_gross_pct,
            )
            cap_note += (
                f" [constructor: size capped to {max(0.0, name_headroom_pct):.2f}% "
                f"by the {self.cfg.max_single_short_pct:.0f}% single-short "
                f"ceiling — deliberately half the long single-name ceiling. "
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
                return None

        allocation_pct = max(0.0, round(allocation_pct, 2))
        if allocation_pct <= 0:
            return None

        reasoning = target.thesis
        if target.thesis_invalid_if:
            reasoning += f" (invalid if: {target.thesis_invalid_if})"
        if target.catalyst:
            reasoning += f" (catalyst: {target.catalyst})"

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
        )

    def _resolve_stop(
        self,
        target: TargetPosition,
        analysis: TechAnalysisResult | None,
        entry_price: float,
    ) -> float | None:
        """Priority: target's suggested stop → the technical analyst's stop → no trade.

        There is deliberately no synthesized fallback. A stop that nobody
        derived from the chart cannot be risk-sized honestly, and the previous
        `entry - 2*ATR` / `entry * 0.95` fallbacks were the source of exits
        that fired inside ordinary noise. If neither source supplies a level,
        return None and let the caller reject the position.
        """
        if target.suggested_stop_price and target.suggested_stop_price > 0:
            return float(target.suggested_stop_price)
        if analysis and analysis.stop_loss and analysis.stop_loss > 0:
            return float(analysis.stop_loss)
        # No structural stop from either source — reject rather than invent one.
        logger.warning(
            "Constructor: %s has no structural stop (suggested_stop_price and "
            "analysis.stop_loss both absent) — rejecting. Stops are no longer "
            "synthesized from ATR or a flat percentage.",
            target.symbol,
        )
        return None
