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

import logging
from dataclasses import dataclass

from src.models import Position, TargetPosition, TechAnalysisResult, TradeDecision

logger = logging.getLogger(__name__)


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
    ) -> list[TradeDecision]:
        """Produce the order list that moves the book from current → target state.

        Orders are returned in a canonical order: SELLs (partials and exits)
        first, then BUYs. Execution layer is free to re-order, but this
        matches the existing pipeline assumption (sells free up cash first).

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
        )

        sells: list[TradeDecision] = []
        buys: list[TradeDecision] = []

        for target in targets:
            sym = target.symbol
            current_pct = current_weights.get(sym, 0.0)
            if target.risk_allocation_pct is not None:
                plan = risk_plan.get(sym)
                if plan is None:
                    # No stop, no entry, or the budget refused it outright.
                    # _plan_risk_targets has already logged which.
                    continue
                target_pct = plan.target_weight_pct
            else:
                target_pct = target.target_weight_pct or 0.0
            delta_pct = target_pct - current_pct

            # target_weight_pct == 0 is PM saying "CLOSE this position", not
            # "rebalance toward ~0". The churn filter must not swallow it: a
            # 0.4%-weight dreg with an explicit close target was silently
            # converted into a HOLD, so a position PM had decided to exit sat
            # in the book indefinitely (2026-07-16 audit). Anything held with
            # target 0 goes to the SELL builder, which emits a full exit.
            closing = (target_pct == 0 and current_pct > 0)
            if not closing and abs(delta_pct) < self.cfg.min_trade_weight_delta:
                # No action — emit HOLD for audit continuity so PM's intent
                # to keep this position at its current level is recorded.
                if current_pct > 0:
                    buys.append(self._hold_decision(target))
                continue

            if delta_pct < 0:
                # Trim or close
                sell_decision = self._build_sell(
                    target, positions_by_sym.get(sym), current_pct, target_pct,
                )
                if sell_decision is not None:
                    sells.append(sell_decision)
            else:
                # Open or add
                buy_decision = self._build_buy(
                    target,
                    plan=risk_plan.get(sym),
                    analysis=analyses_by_sym.get(sym),
                    current_pct=current_pct,
                    target_pct=target_pct,
                    total_value=total_value,
                    market_price=price_map.get(sym),
                )
                if buy_decision is not None:
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
        """
        from src.risk.budget import RiskRequest, allocate_risk_budget
        from src.risk.rules import _gross_multiplier

        priced: dict[str, tuple[float, float]] = {}   # symbol -> (entry, stop)
        requests: list[RiskRequest] = []
        closes: set[str] = set()

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
                target, analysis, price_map.get(sym),
            )
            if entry is None or stop is None:
                continue  # already logged; no stop means no honest size
            priced[sym] = (entry, stop)
            requests.append(RiskRequest(
                sym,
                # The single-name ceiling binds before the portfolio one. A PM
                # asking for more than the ratified envelope is clamped rather
                # than refused — the idea is sound, the size is not.
                min(target.risk_allocation_pct, self.cfg.risk_budget_pct),
            ))

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
            note = ""
            if allocation is not None:
                grant = allocation.grants.get(sym.upper())
                granted = grant.granted_pct if grant else 0.0
                note = grant.note if grant else ""
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

            # risk_pct x entry / (entry - stop): the §2.1 formula as a weight.
            raw_weight = granted * entry / (entry - stop)
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

    def _resolve_entry_and_stop(
        self,
        target: TargetPosition,
        analysis: TechAnalysisResult | None,
        market_price: float | None,
    ) -> tuple[float | None, float | None]:
        """Entry and a validated stop below it, or (None, None).

        Extracted from `_build_buy` because risk-based sizing needs the stop
        distance one step earlier — the position's weight is not knowable until
        the stop is. `_build_buy` calls this too, so there is exactly one
        definition of what a tradeable entry/stop pair is.
        """
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
                "Constructor: cannot construct BUY %s — no entry price available",
                target.symbol,
            )
            return (None, None)

        # Round FIRST, then validate: the TradeDecision ships
        # round(stop_loss, 2), so validating the unrounded value let a stop
        # that rounds UP to exactly the entry price through the
        # `stop_loss < entry_price` check (e.g. entry $10.00, stop $9.999 →
        # ships $10.00 == entry → risk_per_share = 0, and a stop at the entry
        # fires on the first tick down). 2026-07-16 audit.
        stop_loss = self._resolve_stop(target, analysis, entry_price)
        if stop_loss is not None:
            stop_loss = round(stop_loss, 2)
        if stop_loss is None or stop_loss <= 0 or stop_loss >= entry_price:
            logger.warning(
                "Constructor: BUY %s rejected — no valid stop below entry "
                "(entry=$%.2f, stop=%s)",
                target.symbol, entry_price, stop_loss,
            )
            return (None, None)
        return (entry_price, stop_loss)

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
        return {
            p.symbol: (p.market_value * _gross_multiplier(p.symbol) / total_value * 100)
            for p in positions
            if p.qty > 0
        }

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

    def _build_buy(
        self,
        target: TargetPosition,
        analysis: TechAnalysisResult | None,
        current_pct: float,
        target_pct: float,
        total_value: float,
        market_price: float | None,
        plan: RiskPlan | None = None,
    ) -> TradeDecision | None:
        # A risk-based target already resolved its entry and stop in
        # _plan_risk_targets — reusing them keeps the size the budget granted
        # consistent with the level the order actually ships, which a second
        # resolution against a moved quote would not.
        if plan is not None and plan.entry_price is not None and plan.stop_price is not None:
            entry_price, stop_loss = plan.entry_price, plan.stop_price
        else:
            entry_price, stop_loss = self._resolve_entry_and_stop(
                target, analysis, market_price,
            )
            if entry_price is None or stop_loss is None:
                return None

        # Take-profit comes from the analyst's structural reference_target, or
        # there is no trade. The previous `entry * (1 + 2*stop_gap_pct)` branch
        # manufactured a target whenever the analyst omitted one, and
        # thesis_progress, pace and TARGET_BREACH were then all measured
        # against that invention. TechAnalysisResult now requires
        # reference_target for every actionable rating — structure for a range
        # setup, a measured move for a breakout — so this is a hard read.
        if not (analysis and analysis.reference_target and analysis.reference_target > entry_price):
            logger.warning(
                "Constructor: BUY %s rejected — no structural reference_target "
                "from the technical analyst (entry=$%.2f). Targets are no "
                "longer synthesized.",
                target.symbol, entry_price,
            )
            return None
        take_profit = float(analysis.reference_target)

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
        risk_per_share = entry_price - stop_loss
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
            ),
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
