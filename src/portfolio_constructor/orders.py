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

import json
import math
import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass

from src.data.levels import (
    describe_stop_level_basis,
    COVERAGE_UNKNOWN,
    FAULT_NO_ANALYSIS,
    FAULT_NO_ENTRY,
    FAULT_NO_PRICE,
    TargetDerivation,
    derive_structural_target,
    level_zone_halfwidth,
    stop_rests_on_level,
    touch_probability,
)
from src.data.technical import LONGEST_INDICATOR_WINDOW
from src.models import (
    Position, TargetPosition, TechAnalysisResult, TradeDecision,
    reward_to_risk, stated_soft_exit,
)
from src.risk.constants import (
    REWARD_RISK_PARITY,
    gap_adjusted_risk_per_share,
    reward_risk_floor_applies,
    risk_budget_allocation_pct,
    reward_risk_parity_refuses,
)

from src.portfolio_constructor.config import logger  # the package logger, named as before the split
from src.portfolio_constructor.config import (
    _named_reduction_trigger,
    STOP_REFUSAL_SIZED_TO_ZERO,
    STOP_REFUSAL_TARGET_NOT_ABOVE_ENTRY,
    STOP_REFUSAL_TARGET_NOT_BELOW_ENTRY,
    RiskPlan,
)


class _OrderBuildMixin:
    """Per-leg order builders for `PortfolioConstructor` (moved verbatim)."""

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
        # ITEM 222: this is NOT a third, independent notional cap. It is
        # `risk_budget_pct x entry / |entry - stop|` — the per-position RISK
        # envelope converted into notional units so it can be compared with
        # the notional ceiling below. See `SINGLE_NAME_BINDING_SENTENCE`.
        risk_per_share = abs(entry_price - stop_loss)
        # qty_by_risk = risk_dollars_allowed / risk_per_share
        # position_$ = qty_by_risk * entry_price
        # allocation_by_risk_pct = position_$ / total_value * 100
        #                        = (risk_dollars_allowed / risk_per_share) * entry_price / total_value * 100
        cap_note = ""
        if risk_per_share > 0:
            # ONE definition of stop-derived size (board item 221): the
            # PM-facing projected-portfolio preview calls this same helper,
            # so the sector mix the PM self-corrects against is the mix this
            # constructor would actually build. The arithmetic is unchanged.
            alloc_cap_by_risk = risk_budget_allocation_pct(
                entry_price=entry_price, stop_price=stop_loss,
                total_value=total_value,
                risk_budget_pct=self.cfg.risk_budget_pct,
            )
            if alloc_cap_by_risk is not None and allocation_pct > alloc_cap_by_risk:
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
            # Item 55 RECORDING, no behaviour: the same answer as stop_rule
            # above, with the ingredients that produced it, so the desk can
            # later ask what its levels actually did. See
            # TradeDecision.stop_level_basis.
            stop_level_basis=self.shipped_stop_level_basis(
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
            # The MEASURED half of the same verdict (item 82) — see
            # TradeDecision.structural_ceiling. Same expression the reward:
            # risk gate above (inside `_widen_stop_past_noise`, via
            # `_resolve_entry_and_stop`) used against THIS `derivation`, so
            # the Risk Manager sees the identical verdict construction saw.
            structural_ceiling=(derivation.level_used is not None),
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
        # (the stop above was already resolved before this line runs). Paper
        # trading fills unrealistically through a gap on IEX data with no
        # borrow-cost model, so this haircut is what keeps the measured size
        # honest relative to what live capital would actually risk. One
        # definition, in `src.risk.constants` (board item 216).
        risk_per_share = gap_adjusted_risk_per_share(
            risk_per_share, is_short=True,
            multiple=self.cfg.short_gap_risk_multiple,
        )
        cap_note = ""
        if risk_per_share > 0:
            # Same ONE definition the long leg and the preview call (item
            # 221). The gap haircut is applied inside it, exactly once.
            alloc_cap_by_risk = risk_budget_allocation_pct(
                entry_price=entry_price, stop_price=stop_loss,
                total_value=total_value,
                risk_budget_pct=self.cfg.risk_budget_pct,
                is_short=True,
                short_gap_risk_multiple=self.cfg.short_gap_risk_multiple,
            )
            if alloc_cap_by_risk is not None and allocation_pct > alloc_cap_by_risk:
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
            # Item 55 RECORDING, no behaviour: the same answer as stop_rule
            # above, with the ingredients that produced it, so the desk can
            # later ask what its levels actually did. See
            # TradeDecision.stop_level_basis.
            stop_level_basis=self.shipped_stop_level_basis(
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
            # The MEASURED half of the same verdict (item 82) — see
            # TradeDecision.structural_ceiling and the identical comment in
            # `_build_buy`.
            structural_ceiling=(derivation.level_used is not None),
            # Real, untruncated field alongside the embedded-in-reasoning
            # text above — see TradeDecision.thesis_invalid_if.
            thesis_invalid_if=stated_soft_exit(target.thesis_invalid_if) or None,
        )
