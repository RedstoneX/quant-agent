"""Execution stage (split out of src/pipeline_stages.py, step 10).

Moved verbatim in the staged split (board item 210, step 10). No behaviour change:
the class body below is byte-for-byte the text that used to live in
``src/pipeline_stages.py``, and ``src.pipeline_stages`` re-exports it so every
existing import path and every ``src.pipeline_stages.ExecutionStage`` patch target
still resolves to this same object.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from src.entry_evidence import (
    record_scale_in_own_verdict as _record_scale_in_own_verdict,
    resolve_entry_pins as _resolve_entry_pins,
)
from src.entry_record import insert_pending_entry
from src.entry_slippage_bound import entry_bound
from src.price_feed_preflight import preflight_price_feed, price_feed_session_start
from src.sizing_refusal import no_price_skip, sizing_price_or_refusal
from src.pipeline_stages import (  # noqa: F401  shared helpers and module-level names
    LEVEL_BACKED_STOP_RULES,
    RunContext,
    _ROTATION_SELL_REFUSED,
    _adopt_stream_stall,
    _alert_owner_entry_cancelled,
    _alert_owner_protection_failed,
    _alert_rotation_executed,
    _drop_rotation_buy_if_room_not_freed,
    _encode_entry_submit_window,
    _entry_deployment_budget,
    _entry_slippage_bps,
    _execution_payoff_skip_reason,
    _fmt_shares,
    _fractional_sizing_allowed,
    _live_fill_price,
    _min_order_usd,
    _pin_approved_entry_ceilings,
    _qty_by_risk_budget,
    _record_execution_skip,
    _record_pipeline_event,
    _record_rotation_buy_leg_outcome,
    _record_scale_in_window_closed,
    _repeg_entry_order,
    _risk_budget_pct,
    _rotation_ranked_margin_sell_reason,
    _drop_buys_sold_today_below_bar,
    _rotation_sell_gate,
    _rotation_sell_last,
    _single_name_execution_cap,
    _size_shares,
    _stop_trade_updates,
    _submit_window_overrun,
    _today_sizing_price,
    _warm_trade_updates,
    compute_indicators,
    logger,
)

if TYPE_CHECKING:
    from src.agents.earnings_analyst import EarningsAnalystAgent
    from src.agents.macro_analyst import MacroAnalystAgent
    from src.agents.news_analyst import NewsAnalystAgent
    from src.agents.tech_analyst import TechAnalystAgent
    from src.agents.smart_money_analyst import SmartMoneyAnalystAgent
    from src.data.smart_money import SmartMoneySource
    from src.config import AppConfig
    from src.data.earnings import EarningsDataProvider
    from src.data.event_calendar import (
        FOMCCalendarProvider, MacroEventCalendarProvider,
    )
    from src.data.macro import MacroDataProvider
    from src.data.macro_store import MacroStore
    from src.data.market import MarketDataProvider
    from src.data.news import NewsCoverage, NewsDataProvider
    from src.data.news_store import NewsStore
    from src.data.tech_store import TechStore
    from src.models import TradeDecision
    from src.pipeline import TradingPipeline

class ExecutionStage:
    """Record HOLDs → submit SELLs → wait → refresh → submit BUYs.

    Reads:  ctx.portfolio_decision.decisions, ctx.positions, ctx.cash,
            ctx.total_value, ctx.symbols_bars
    Writes: ctx.orders, and on SELL refresh: ctx.positions / .cash / .total_value
    """

    def __init__(self, *, pipeline: "TradingPipeline"):
        self._pipeline = pipeline

    def run(self, ctx: RunContext) -> list[dict]:
        try:
            return self._run_session(price_feed_session_start(self._pipeline, ctx))
        finally:
            _stop_trade_updates(self._pipeline)

    def _run_session(self, ctx: RunContext) -> list[dict]:
        pipeline = self._pipeline
        run_id = ctx.run_id
        # Stage 1 (QAMC correlation plumbing): links every trades row this
        # run produces back to the PM proposal / RM review that led to it.
        # None on any run that never reached a successful PM call (e.g. an
        # early-exit before DecisionStage) — trades rows from such a run
        # simply carry no decision_id, which is correct, not a bug.
        decision_id = ctx.decision_id
        positions = ctx.positions
        total_value = ctx.total_value
        cash = ctx.cash
        portfolio_decision = ctx.portfolio_decision

        orders: list[dict] = []
        sell_decisions = [d for d in portfolio_decision.decisions if d.action == "SELL"]
        # Stage 3 (shorts): SHORT is the entry-side twin of BUY — both open
        # or add to a position and both owe a mandatory protective stop, so
        # they share the entry submission loop below (branching internally
        # on `decision.action` for side / geometry / sizing). COVER is the
        # exit-side twin of SELL and gets its OWN loop further down that
        # reuses `_submit_protected_sell` with side="buy", exactly the
        # plumbing PR #135 built for emergency covers.
        buy_decisions = [
            d for d in portfolio_decision.decisions if d.action in ("BUY", "SHORT")
        ]
        cover_decisions = [d for d in portfolio_decision.decisions if d.action == "COVER"]
        hold_decisions = [d for d in portfolio_decision.decisions if d.action == "HOLD"]

        # Board item 178 — `ctx.positions`/`.cash`/`.total_value` are the
        # run-OPEN broker snapshot, taken before Research/Decision/Risk ran;
        # by the time this stage submits an ordinary SELL/COVER that
        # snapshot is ~5-10 minutes stale. A refresh already ran for the
        # RANKED-MARGIN rotation close (`_rotation_sell_gate`, which
        # re-reads for its own case below) and for BUYs (post-loop, further
        # down) — this was the one exit path still sizing qty and limit
        # price off the stale open-of-run read. Re-read ONCE, here, before
        # either loop starts, so ordinary SELL/COVER qty and price come from
        # a current book; a no-op when nothing moved between the two reads.
        if sell_decisions or cover_decisions:
            account, positions, price_map = pipeline._refresh_account_state()
            cash = account["cash"]
            total_value = account["portfolio_value"]
            ctx.positions = positions
            ctx.cash = cash
            ctx.deployable_cash = pipeline._compute_deployable_cash(cash, positions)
            ctx.total_value = total_value
            logger.info(
                "Pre-sell refresh: $%.2f total, $%.2f cash, %d positions",
                total_value, cash, len(positions),
            )

        # Board item 39 — the RANKED-MARGIN rotation's close goes LAST
        # among this session's exits, so that when its paired BUY is
        # checked (in `_rotation_sell_gate`, immediately before the close
        # is submitted) every other exit has a terminal status and the
        # account can simply be re-read rather than guessed at. A no-op on
        # every session without a ranked-margin rotation, which is every
        # session while `execution.rotation_ranked_margin_enabled` is off.
        sell_decisions = _rotation_sell_last(sell_decisions, ctx)

        for d in hold_decisions:
            try:
                pipeline.db.insert_trade(
                    symbol=d.symbol, action="HOLD", qty=0.0, price=0.0,
                    reasoning=d.reasoning, run_id=run_id,
                    decision_id=decision_id,
                )
            except Exception as e:
                logger.warning("Failed to record HOLD decision for %s: %s", d.symbol, e)

        # Terminal status per SELL order id, filled in by the per-name wait
        # below. Phase 14b reads it to decide whether the rotation's freed
        # room is REAL before the replacement BUY is allowed.
        sell_status_by_id: dict[str, str | None] = {}
        for decision in sell_decisions:
            prot = None
            try:
                # Board item 39. For a ranked-margin rotation close this
                # re-reads the account (measuring what the exits above
                # actually did), projects THIS sale, and runs the
                # replacement BUY through every refusal gate that is
                # knowable before the sale. `cleared` False means the buy
                # would be refused, so the close is not submitted and the
                # desk keeps the position instead of going naked. `None`
                # for every other SELL in the desk's history.
                rotation_gate = _rotation_sell_gate(
                    pipeline, ctx, decision, buy_decisions, positions,
                    total_value, cash, cover_decisions,
                )
                if rotation_gate is not None:
                    cleared, positions, total_value, cash = rotation_gate
                    # Adopt the refreshed book so this close is sized and
                    # priced off the same state the gate cleared against.
                    # `deployable_cash` is DERIVED from cash, so it is
                    # computed BEFORE any of the four is assigned: a raise
                    # part-way through would otherwise be swallowed by this
                    # loop's own handler and leave three fresh fields
                    # beside a stale derivation. All four, or none.
                    refreshed = (
                        positions, cash, total_value,
                        pipeline._compute_deployable_cash(cash, positions),
                    )
                    (ctx.positions, ctx.cash, ctx.total_value,
                     ctx.deployable_cash) = refreshed
                    if not cleared:
                        continue
                existing = [p for p in positions if p.symbol == decision.symbol]
                if not existing or existing[0].qty <= 0:
                    continue
                # Board item 39 — the unconditional barrier. A RANKED-MARGIN
                # rotation close may not reach the broker unless
                # `rotation_sell_reason` can build its reason from a real
                # `RotationClearance`, which only the projected post-sale
                # gate above mints. This reads no config: the feature flag
                # decides whether such a close is ever PROPOSED, and cannot
                # decide whether it is permitted to execute. Attempt 1 on
                # this item replaced exactly this kind of structural barrier
                # with a config boolean; it is not a config boolean again.
                rotation_final_reason = _rotation_ranked_margin_sell_reason(
                    pipeline, ctx, decision,
                )
                if rotation_final_reason is _ROTATION_SELL_REFUSED:
                    continue
                if decision.allocation_pct == 0:
                    logger.warning(
                        "Skipping SELL %s with allocation_pct=0 (ambiguous — use 100 for full exit)",
                        decision.symbol,
                    )
                    continue
                if 0 < decision.allocation_pct < 100:
                    sell_fraction = decision.allocation_pct / 100
                    qty = existing[0].qty * sell_fraction
                    if float(existing[0].qty).is_integer():
                        qty = max(1.0, float(int(qty)))
                    if qty <= 0:
                        continue
                    if qty >= existing[0].qty:
                        qty = pipeline._full_sell_qty(existing[0].qty)
                        if qty is None:
                            continue
                        action_label = "SELL"
                    else:
                        action_label = f"PARTIAL_SELL({decision.allocation_pct:.0f}%)"
                else:
                    qty = pipeline._full_sell_qty(existing[0].qty)
                    if qty is None:
                        continue
                    action_label = "SELL"
                sell_price = existing[0].current_price
                sell_limit = round(sell_price * 0.995, 2)
                position_qty = existing[0].qty
                # Single protected-sell discipline (cancel-WAL → submit →
                # accept → restore-on-failure) lives in one helper so this path
                # can't skip a step; defer reprotect/restore to the post-sell
                # wait below, which resolves the actual fill_qty.
                sale = pipeline._submit_protected_sell(
                    symbol=decision.symbol, qty=qty, limit_price=sell_limit,
                    reference_price=existing[0].current_price,
                    position_qty_before_sell=position_qty, label=action_label,
                )
                if sale is None:
                    continue
                order, prot = sale
                orders.append(order)
                pipeline.db.insert_trade(
                    symbol=decision.symbol, action=action_label, qty=qty,
                    price=sell_price, reasoning=decision.reasoning, run_id=run_id,
                    broker_order_id=order.get("id"),
                    fill_status="submitted",
                    decision_id=decision_id,
                )
                _record_pipeline_event(
                    pipeline, ctx, decision.symbol, "order", "submitted",
                    "broker_accepted", broker_order_id=order.get("id"), qty=qty,
                    limit_price=sell_limit, side="sell",
                )
                # Phase 14b — this SELL is the desk's own rotation close.
                # Record it durably and page the owner NOW: broker
                # acceptance is the irreversible act, and a position sold
                # without a human or a model deciding to must never be
                # silent (see `_alert_rotation_executed`).
                rotation = ctx.rotation
                if (
                    isinstance(rotation, dict)
                    and decision.symbol.upper() == rotation.get("held_symbol")
                ):
                    rotation["sell_order_id"] = order.get("id")
                    rotation["sell_qty"] = float(qty)
                    if isinstance(rotation_final_reason, str):
                        # A SECOND durable fact, not an edit of the first.
                        # The proposal the Risk Manager reviewed and the
                        # clearance the sale executed under are two
                        # different things that happened at two different
                        # times; overwriting one with the other leaves the
                        # ledger disagreeing with the alert about what was
                        # said when. Written once, never edited.
                        rotation["cleared_reason"] = rotation_final_reason
                        _record_pipeline_event(
                            pipeline, ctx, decision.symbol, "rotation",
                            "sell_cleared_reason", rotation_final_reason,
                            broker_order_id=order.get("id"),
                            new_symbol=rotation.get("new_symbol"),
                        )
                    _record_pipeline_event(
                        pipeline, ctx, decision.symbol, "rotation",
                        "sell_submitted", rotation.get("reason", ""),
                        broker_order_id=order.get("id"), qty=qty,
                        limit_price=sell_limit,
                        new_symbol=rotation.get("new_symbol"),
                    )
                    _alert_rotation_executed(
                        rotation=rotation, qty=float(qty),
                        limit_price=float(sell_limit), order_id=order.get("id"),
                    )
                logger.info(
                    "Executed: %s %s %s @ limit $%.2f",
                    action_label.lower(), pipeline._format_qty(qty), decision.symbol, sell_limit,
                )
            except Exception as e:
                logger.error("Order failed for %s %s: %s", decision.action, decision.symbol, e)
            if prot is None:
                continue
            # Wait for THIS sell and rebuild THIS name's stop coverage on its
            # actual fill before the loop cancels the next name's stops —
            # the per-name discipline the de-lever loops got (docs/WORK.md
            # item 111). Submitting every SELL first and waiting/finalizing
            # the batch afterwards left every earlier name with no
            # protective stop while later names were cancelled, submitted
            # and waited on. Runs even when the ledger write above raised:
            # the stops are off and the order is live. Which names are sold,
            # how much and at what limit are unchanged.
            order_id = prot["order_id"]
            # ExecutionStage was the lone SELL path missing this guard
            # — every other SELL path (force_delever / midday_emergency /
            # midday_llm / intra_check / take_profit) wraps the wait in
            # try/except. An uncaught exception here (broker 5xx, DNS
            # blip mid-poll) would propagate past the finalize loop
            # below. The audit F1 write-ahead row already covers a hard
            # process kill; this try/except additionally keeps the
            # in-process finalize path alive so coverage is rebuilt now
            # rather than waiting for the next session's drain.
            try:
                status = pipeline.broker.wait_for_order_terminal(order_id)
            except Exception as e:
                logger.warning(
                    "ExecutionStage: wait_for_order_terminal failed for %s: %s "
                    "— treating as unknown status so finalize still runs",
                    order_id, e,
                )
                status = None
            sell_status_by_id[order_id] = status
            if status != "filled":
                logger.warning(
                    "Sell order %s did not fill before buy phase (status=%s); buys will use current cash only",
                    order_id, status or "unknown",
                )
            # The wait above returned, so the broker's fill_info is final.
            # Reprotect on actual residual (filled) or restore originals
            # (no-fill terminal). wait=False: this order was just waited on.
            pipeline._finalize_pending_protections(
                [prot], context="ExecutionStage", wait=False,
            )

        # Stage 3 (shorts): COVER loop — the exit-side twin of the SELL loop
        # just above. Reuses `_submit_protected_sell`'s side="buy" plumbing
        # (PR #135 built this for emergency covers; this is the first
        # decision-path caller). No protective stop is placed afterward —
        # covering REDUCES risk, it doesn't open any.
        for decision in cover_decisions:
            prot = None
            try:
                existing = [p for p in positions if p.symbol == decision.symbol]
                if not existing or existing[0].qty >= 0:
                    continue  # nothing short held — COVER on a long/flat is refused
                held_qty = abs(existing[0].qty)
                if decision.allocation_pct == 0:
                    logger.warning(
                        "Skipping COVER %s with allocation_pct=0 (ambiguous — use 100 for full exit)",
                        decision.symbol,
                    )
                    continue
                if 0 < decision.allocation_pct < 100:
                    cover_fraction = decision.allocation_pct / 100
                    qty = held_qty * cover_fraction
                    if float(held_qty).is_integer():
                        qty = max(1.0, float(int(qty)))
                    if qty <= 0:
                        continue
                    if qty >= held_qty:
                        qty = pipeline._full_sell_qty(held_qty)
                        if qty is None:
                            continue
                        action_label = "COVER"
                    else:
                        action_label = f"PARTIAL_COVER({decision.allocation_pct:.0f}%)"
                else:
                    qty = pipeline._full_sell_qty(held_qty)
                    if qty is None:
                        continue
                    action_label = "COVER"
                cover_price = existing[0].current_price
                # Buy-to-cover needs headroom ABOVE the reference to fill on
                # the way up — the mirror of the SELL loop's limit sitting
                # 0.5% BELOW (same reasoning as `_EMERGENCY_LIMIT_CUSHION_PCT`
                # in pipeline.py, applied here to the ordinary decision path).
                cover_limit = round(cover_price * 1.005, 2)
                sale = pipeline._submit_protected_sell(
                    symbol=decision.symbol, qty=qty, limit_price=cover_limit,
                    reference_price=existing[0].current_price,
                    position_qty_before_sell=held_qty, label=action_label,
                    side="buy",
                )
                if sale is None:
                    continue
                order, prot = sale
                orders.append(order)
                pipeline.db.insert_trade(
                    symbol=decision.symbol, action=action_label, qty=qty,
                    price=cover_price, reasoning=decision.reasoning, run_id=run_id,
                    broker_order_id=order.get("id"),
                    fill_status="submitted",
                    decision_id=decision_id,
                )
                _record_pipeline_event(
                    pipeline, ctx, decision.symbol, "order", "submitted",
                    "broker_accepted", broker_order_id=order.get("id"), qty=qty,
                    limit_price=cover_limit, side="buy",
                )
                logger.info(
                    "Executed: %s %s %s @ limit $%.2f",
                    action_label.lower(), pipeline._format_qty(qty), decision.symbol, cover_limit,
                )
            except Exception as e:
                logger.error("Order failed for %s %s: %s", decision.action, decision.symbol, e)
            if prot is None:
                continue
            # Same per-name discipline as the SELL loop above: this short's
            # BUY-stop coverage is rebuilt before the next short's is touched.
            order_id = prot["order_id"]
            try:
                status = pipeline.broker.wait_for_order_terminal(order_id)
            except Exception as e:
                logger.warning(
                    "ExecutionStage: wait_for_order_terminal failed for %s: %s "
                    "— treating as unknown status so finalize still runs",
                    order_id, e,
                )
                status = None
            if status != "filled":
                logger.warning(
                    "Cover order %s did not fill before buy phase (status=%s)",
                    order_id, status or "unknown",
                )
            pipeline._finalize_pending_protections(
                [prot], context="ExecutionStage-Cover", wait=False,
            )

        if sell_decisions or cover_decisions:
            account, positions, price_map = pipeline._refresh_account_state()
            cash = account["cash"]
            total_value = account["portfolio_value"]
            ctx.positions = positions
            ctx.cash = cash
            ctx.deployable_cash = pipeline._compute_deployable_cash(cash, positions)
            ctx.total_value = total_value
            logger.info(
                "Post-sell refresh: $%.2f total, $%.2f cash, %d positions",
                total_value, cash, len(positions),
            )
        else:
            price_map = {p.symbol: p.current_price for p in positions}

        # Refresh the account before BUYs when no SELL fired, so sizing and
        # the entry-staleness guard read a current snapshot rather than the
        # research-stage one from ~10 minutes ago.
        #
        # An account-level daily-loss re-check used to run here too, dropping
        # every remaining BUY when the day's loss crossed the limit. Removed
        # 2026-09-20 on the owner's instruction with the rest of that
        # mechanism (retired item 32, docs/INCIDENT_HISTORY.md).
        if buy_decisions:
            if not sell_decisions:
                # Take the FRESH price_map too (2026-07-16 audit): it was
                # discarded into `_`, leaving `price_map` at research-time
                # position prices from 5-10 minutes earlier. For an ADD to a
                # held name that stale price is what the 5% entry-staleness
                # guard compares the LLM's entry against, and what sizes the
                # order — so the guard could pass a genuinely stale entry (or
                # reject a good one) on exactly the fast-moving tape where it
                # matters. New symbols were unaffected (they miss the map and
                # fall through to a live quote).
                account, positions, fresh_prices = pipeline._refresh_account_state()
                cash = account["cash"]
                total_value = account["portfolio_value"]
                ctx.positions = positions
                ctx.cash = cash
                ctx.deployable_cash = pipeline._compute_deployable_cash(cash, positions)
                ctx.total_value = total_value
                price_map = {**price_map, **fresh_prices}

        # Phase 14b — the rotation's BUY leg may only proceed on room that
        # is REAL. The constructor granted the new candidate its risk on the
        # premise that the held name closes; if that close was refused
        # upstream (Risk Manager, hard rules, protected-sell skip) or was
        # accepted but did not fill, buying anyway would put the book over
        # the portfolio risk ceiling by the new name's risk — a side door
        # around the ceiling this feature must never open. Same
        # `_record_execution_skip` path every other deterministic BUY skip
        # uses, so the funnel and the evening review see it.
        buy_decisions = _drop_rotation_buy_if_room_not_freed(
            pipeline, ctx, buy_decisions, sell_status_by_id,
        )

        # Anti-churn, the BUY-side mirror of the SELL-side
        # `held_symbol_bought_today` guard: a name this desk closed earlier
        # TODAY for failing its own entry bar is not bought back in the
        # same session. No new number — same exchange-day window.
        buy_decisions = _drop_buys_sold_today_below_bar(
            pipeline, ctx, preflight_price_feed(pipeline, ctx, buy_decisions),
        )

        # Run the cheap deterministic entry-viability checks BEFORE selling
        # SGOV. Production evidence showed the sweep funding names that were
        # guaranteed to die moments later on stale-entry / no-price / qty-zero
        # checks, creating avoidable sell/re-park churn. The full checks remain
        # in the submit loop below; this preflight only removes names whose
        # failure is already knowable and computes the actual quantized
        # notional that funding should cover.
        fundable_notional: dict[str, float] = {}
        preflight_survivors = []
        for decision in buy_decisions:
            market_price = _live_fill_price(pipeline, decision.symbol)
            if market_price is not None:
                price_map[decision.symbol] = market_price
            if not isinstance(market_price, (int, float)) or market_price <= 0:
                _record_execution_skip(
                    pipeline, ctx, decision.symbol, *no_price_skip(pipeline, decision.symbol),
                )
                continue
            if decision.entry_price > 0:
                deviation = abs(decision.entry_price - market_price) / market_price
                if deviation > 0.05:
                    _record_execution_skip(
                        pipeline, ctx, decision.symbol, "stale_entry",
                        f"entry ${decision.entry_price:.2f} is "
                        f"{deviation * 100:.1f}% from market "
                        f"${market_price:.2f} (threshold 5%)",
                    )
                    continue
            # docs/WORK.md item 120: the funding preflight must size off the
            # same TODAY PRINT the submit loop will, never the fill-reference
            # mid. No print -> the submit loop will refuse this name, so the
            # sweep must not sell SGOV to fund it.
            sizing_print, why, detail = sizing_price_or_refusal(
                _today_sizing_price, pipeline, decision.symbol, "buy",
            )
            if sizing_print is None:
                _record_execution_skip(pipeline, ctx, decision.symbol, why, detail)
                continue
            preflight_price = max(sizing_print, decision.entry_price or 0)
            # Spec §11.1: quantized the SAME way the submit loop below will,
            # or the sweep funds a whole-share notional for an order that is
            # about to be placed fractionally — under-funding it, and letting
            # the cash gate re-impose the rounding tax this phase removes.
            # It is also the difference between skipping a sub-one-share
            # position as `qty_zero` and taking it, which under exact sizing
            # is a legitimate position rather than nothing.
            preflight_short = decision.action == "SHORT"
            preflight_fractional = _fractional_sizing_allowed(
                pipeline, decision.symbol, is_short=preflight_short,
            )
            preflight_qty = _size_shares(
                pipeline,
                (total_value * decision.allocation_pct / 100) / preflight_price,
                fractional=preflight_fractional,
            )
            if preflight_qty <= 0:
                _record_execution_skip(
                    pipeline, ctx, decision.symbol, "qty_zero",
                    f"allocation {decision.allocation_pct:.2f}% at "
                    f"${preflight_price:.2f} rounds to zero shares",
                )
                continue
            # Fund what the submit loop will SPEND, not what the allocation
            # asked for. The loop takes `min(qty_by_alloc, qty_by_risk)`; on
            # any session where the vol-adjusted budget binds — the ordinary
            # case — funding the allocation figure over-sells the vehicle and
            # the bookend re-parks the difference within the minute. Same
            # helper, same quantization, so the two cannot drift apart.
            #
            # UNDER-funding is the one direction that costs a trade rather
            # than a spread, so the reference price must be the submit
            # loop's own. It is: for a long the loop takes
            # `max(market_price, limit_price)` and for a short
            # `min(market_price, limit_price)` — which is exactly
            # `preflight_price` above. Every adjustment the loop makes AFTER
            # that point moves the quantity DOWN, never up: a marketable-
            # limit ceiling only raises the price, and the ATR floor only
            # widens the stop, and each of those shrinks the shares the risk
            # budget allows. So this is an upper bound on what will be
            # spent, which is the safe side to be wrong on.
            preflight_risk_qty = _qty_by_risk_budget(
                pipeline, total_value=total_value,
                sizing_price=preflight_price,
                stop_price=decision.stop_loss,
                is_short=preflight_short, fractional=preflight_fractional,
            )
            if preflight_risk_qty is not None and preflight_risk_qty < preflight_qty:
                preflight_qty = preflight_risk_qty
            if preflight_qty <= 0:
                # The risk budget alone cannot carry one orderable unit. The
                # submit loop will reach the same conclusion and skip; there
                # is nothing here for the sweep to fund.
                _record_execution_skip(
                    pipeline, ctx, decision.symbol, "qty_zero",
                    f"risk budget at ${preflight_price:.2f} entry / "
                    f"${decision.stop_loss:.2f} stop rounds to zero shares",
                )
                continue
            # A SHORT is deliberately excluded from the funding total: it
            # sells borrowed shares and spends no cash (see D11 in the submit
            # loop, where a short is never sized by the entry budget).
            # Funding one liquidates the vehicle to raise cash that no order
            # can spend — guaranteed churn, not a safety margin. BUY
            # notionals are still counted in full, so this can only remove
            # waste, never under-fund a BUY.
            if not preflight_short:
                fundable_notional[decision.symbol] = preflight_qty * preflight_price
            preflight_survivors.append(decision)
        buy_decisions = preflight_survivors

        # Cash-sweep funding. `planned_notional` counts BUYs ONLY, at the
        # quantity the submit loop will actually reach — allocation capped by
        # the §11.1 risk budget, quantized by the same helper. A SHORT is
        # excluded outright: it sells borrowed shares and spends no cash (see
        # D11 in the sizing loop), so funding one liquidates the vehicle for
        # cash no order can spend. Both were over-funding, and over-funding
        # is not free: the bookend re-parks
        # the residue minutes later, which is two crossings of the spread
        # for no position (2026-08-27: sold $3,422.61, re-bought $1,007.60
        # 53 seconds later; 2026-08-31: sold $503.47, re-bought $806.40
        # five seconds later).
        #
        # PM/RM/the hard gate size BUYs against
        # `deployable_cash` (raw cash + convertible sweep value), so on any
        # session with meaningful BUYs this sale IS load-bearing — the raw
        # cash on hand is typically just the reserve. The pre-BUY funding
        # sale that used to cover the planned notional was removed in item
        # 190, so nothing converts the vehicle automatically now (the
        # 2026-08-19 loss of a fully-approved plan was a 51s fill outliving
        # a 15s wait, not settlement).
        #
        # Since margin went on (2026-09-02) the sale is no longer what makes
        # a BUY POSSIBLE — the entry budget below is ladder headroom, and a
        # BUY the sale failed to fund now draws a margin loan instead of
        # being skipped. It is still worth doing: borrowing at
        # `margin_interest_rate_pct` against T-bills the desk already owns is
        # a guaranteed negative carry, so the sweep converts first and the
        # loan is what is left over.
        # isinstance guard: stage tests stub `pipeline` with MagicMock.
        if buy_decisions:
            _pin_approved_entry_ceilings(pipeline, ctx, buy_decisions)
            _warm_trade_updates(pipeline, ctx)
            # The cash-sweep funding step (sell the T-bill vehicle before a BUY)
            # was retired with the sweeper (board item 190 step 3); raw cash is
            # the only funding source, so there is nothing to record as freed.
            for d in buy_decisions:
                _record_pipeline_event(
                    pipeline, ctx, d.symbol, "funding", "not_required",
                    "cash_sweep_disabled", raw_cash=cash,
                )
            _adopt_stream_stall(pipeline, ctx)
            # Encode AFTER funding so the fund step's own 180s/30s ceiling
            # cannot sit as leftover slack on a fast no-op. Remaining
            # programmed wait is auth only when the hub did not start.
            _encode_entry_submit_window(pipeline, ctx, will_fund=False)

        # Spec §11.2 — how much NEW exposure this session may still add, and
        # the pool every entry below draws from. Ladder-derived (see
        # `_entry_deployment_budget`); raw cash only when the ladder cannot
        # be read at all. `total_value` and `positions` are the post-sell,
        # post-funding figures adopted above, so the headroom is measured
        # against the book the entries will actually join.
        entry_budget, budget_is_gross, budget_note = _entry_deployment_budget(
            pipeline, ctx, positions, total_value, cash,
        )
        single_name_cap = _single_name_execution_cap(pipeline, total_value)
        if buy_decisions:
            logger.info(
                "Entry budget for %d entr%s: $%.2f — %s (single-order ceiling "
                "$%.2f)",
                len(buy_decisions), "y" if len(buy_decisions) == 1 else "ies",
                entry_budget, budget_note, single_name_cap,
            )
        pending_entry_stops: list[dict] = []
        # A QUEUE, not the decision list. `entry_budget` is drawn on
        # SUBMISSION and is never given back, so an entry that rests unfilled
        # holds its slice of the §11.2 pool for the whole burst and every
        # LATER candidate sizes against the smaller pool. A name whose
        # displayed quote reads through its own ceiling is therefore moved to
        # the BACK of this queue exactly once (board item 183, see the
        # deferral in the limit-price block): it is still submitted, it is
        # still sized the same way, it simply stops taking the pool ahead of
        # names whose quotes are clean. Nothing here refuses anything and no
        # threshold is involved — the test is the ceiling itself.
        submit_queue = list(buy_decisions)
        original_entry_count = len(submit_queue)
        deferred_far_through: set[str] = set()
        queue_index = 0
        while queue_index < len(submit_queue):
            decision = submit_queue[queue_index]
            queue_index += 1
            if decision.action not in ("BUY", "SHORT"):
                continue
            is_short = decision.action == "SHORT"
            add_prep = None
            buy_accepted = False
            submit_attempted = False
            try:
                from src.execution.scale_in import LongAddPrep
                add_prep = LongAddPrep.not_scale_in()
                # D6 (Stage 3): the borrow gate. Refuse to open a short
                # unless the broker reports it BOTH shortable AND easy to
                # borrow — an API error or an unreadable/unknown symbol
                # reports both False in `get_shortability` (fail closed), so
                # a lookup failure refuses the short rather than guessing it
                # open. This is paper trading against IEX data: a
                # hard-to-borrow name fills unrealistically in paper and its
                # borrow cost is not modeled anywhere in this system, so
                # restricting to easy-to-borrow keeps measured results
                # transferable to live capital.
                if is_short:
                    try:
                        borrow = pipeline.broker.get_shortability(decision.symbol)
                    except Exception as e:  # noqa: BLE001
                        logger.warning(
                            "SHORT %s: shortability lookup raised: %s",
                            decision.symbol, e,
                        )
                        borrow = {
                            "shortable": False, "easy_to_borrow": False,
                            "reason": "asset_lookup_failed",
                        }
                    if not (isinstance(borrow, dict) and borrow.get("shortable")
                            and borrow.get("easy_to_borrow")):
                        reason = (
                            borrow.get("reason", "not_shortable")
                            if isinstance(borrow, dict) else "not_shortable"
                        )
                        logger.warning(
                            "SHORT %s skipped: borrow gate refused (%s)",
                            decision.symbol, reason,
                        )
                        _record_execution_skip(
                            pipeline, ctx, decision.symbol, "borrow_gate", reason,
                        )
                        continue
                    # Short scale-in (adding to an existing short) is now a
                    # real path: it is routed through `prepare_short_add` at
                    # the same post-sizing / post-min-order-floor point the
                    # long add uses, below. It is NOT gated here — the
                    # min-order floor must run first so the buy-stop is never
                    # cancelled for an add that then gets dropped.

                live_price = _live_fill_price(pipeline, decision.symbol)
                if live_price is not None:
                    market_price = live_price
                    price_map[decision.symbol] = live_price
                else:
                    market_price = None
                # MUST NOT freeze a morning/last-bar close into a live fill.

                limit_price = None
                sizing_price = None
                if decision.entry_price > 0:
                    limit_price = decision.entry_price

                if market_price and market_price > 0:
                    if limit_price is not None:
                        deviation = abs(limit_price - market_price) / market_price
                        if deviation > 0.05:
                            # Previously fell back to market order here — that
                            # silently absorbed up to 10% slippage against the
                            # LLM's stated entry. Now we skip: if entry_price
                            # is stale by >5%, the stop_loss computed against
                            # that entry is also stale, and the whole R/R math
                            # is bogus. Better to wait for next session.
                            logger.warning(
                                "%s %s skipped: LLM entry_price $%.2f is %.1f%% "
                                "away from market $%.2f (threshold 5%%). Stop/R/R "
                                "computed against stale entry would be unsafe.",
                                decision.action, decision.symbol, decision.entry_price,
                                deviation * 100, market_price,
                            )
                            _record_execution_skip(
                                pipeline, ctx, decision.symbol, "stale_entry",
                                f"entry ${decision.entry_price:.2f} is "
                                f"{deviation * 100:.1f}% from market "
                                f"${market_price:.2f} (threshold 5%)",
                            )
                            continue
                        elif not is_short and limit_price < market_price:
                            logger.info(
                                "Adjusting limit price for %s: $%.2f → $%.2f (raised to market)",
                                decision.symbol, limit_price, market_price,
                            )
                            limit_price = market_price
                        elif is_short and limit_price > market_price:
                            # Mirror: a resting SHORT limit sitting ABOVE
                            # market is not marketable — you can't sell short
                            # above the market and expect an immediate fill —
                            # so pull it DOWN to market instead of UP.
                            logger.info(
                                "Adjusting limit price for SHORT %s: $%.2f → "
                                "$%.2f (lowered to market)",
                                decision.symbol, limit_price, market_price,
                            )
                            limit_price = market_price
                    # `sizing_price` is deliberately NOT set from market_price
                    # here: market_price is the FILL reference (a quote mid is
                    # legitimate for the marketable limit) and the SHARE COUNT
                    # must not divide by a mid. It is anchored to a today
                    # print just below (docs/WORK.md item 120).
                else:
                    logger.error(
                        "%s %s skipped: no verifiable price reference "
                        "(broker + bars both unavailable). "
                        "LLM proposed entry $%.2f but cannot be validated.",
                        decision.action, decision.symbol, decision.entry_price,
                    )
                    _record_execution_skip(
                        pipeline, ctx, decision.symbol, *no_price_skip(
                            pipeline, decision.symbol,
                            "no verifiable price reference (broker + bars unavailable)"),
                    )
                    continue

                # docs/WORK.md item 120: SIZING vs FILL. `market_price` above
                # is the fill reference and may be a quote mid (a legitimate
                # marketable-limit reference, owner 2026-09-12); it drives the
                # limit price. The SHARE COUNT, however, divides the dollar
                # allocation by the price, so it must be a real TODAY PRINT —
                # never a quote mid, never a prior-session trade. Size off the
                # print, bounded conservatively by the already-approved entry
                # (which passed the 5% freshness check above); refuse the name
                # when no print is available rather than size on a bad price.
                sizing_print, why, detail = sizing_price_or_refusal(
                    _today_sizing_price, pipeline, decision.symbol, "order",
                )
                if sizing_print is None:
                    _record_execution_skip(pipeline, ctx, decision.symbol, why, detail)
                    continue
                if decision.entry_price and decision.entry_price > 0:
                    # Size off a today print, bounded by the approved entry.
                    # The HIGHER divisor is conservative on the ALLOCATION
                    # path for BOTH directions (fewer shares: less capital on
                    # a buy, a smaller short on a short). On the RISK-BUDGET
                    # path it is conservative for a BUY only: there
                    # `risk_per_share = entry - stop` and a higher entry
                    # WIDENS it, shrinking qty_by_risk; for a SHORT
                    # (`stop - entry`) a higher entry NARROWS it and can
                    # INFLATE qty_by_risk when the analyst entry sits above
                    # the today print — item 181, fixed just below via
                    # `risk_sizing_price` (this `sizing_price` stays the
                    # allocation-path divisor, unchanged).
                    # What this line does fix: the short no longer divides by
                    # the below-market `bid_limit` (the over-size bug of
                    # item 120). Never size off a below-market number.
                    sizing_price = max(sizing_print, float(decision.entry_price))
                else:
                    sizing_price = sizing_print
                # item 181: a SHORT's higher divisor NARROWS |price - stop|
                # and inflates qty_by_risk, so it falls back to the print.
                risk_sizing_price = sizing_print if is_short else sizing_price

                # Liquid-equity execution policy: cross the displayed quote
                # with a limit (never a market order). A wider spread remains
                # price-protected and may expire after the bounded entry
                # window instead of paying through an abnormal book. If quote
                # data is degraded, retain the validated last/PM limit and
                # the same bounded wait.
                #
                # Fillability parity, not a new risk budget: BUY crosses the
                # displayed OFFER with a ceiling `reference * (1 + bps/1e4)`;
                # SHORT crosses the displayed BID with the same
                # `max_entry_slippage_bps` as a floor
                # `reference * (1 - bps/1e4)`. A SHORT still keeps the >5%
                # stale-entry skip and the direction-aware lower-to-market
                # adjustment just above; this block only adds the NBBO-aware
                # floor a BUY already had as a ceiling. Repeg stays off —
                # walking a short toward the buy-side ceiling would worsen
                # it, not fix an unmarketable birth price.
                try:
                    quote = pipeline.broker.get_latest_quote(decision.symbol)
                except Exception as e:  # noqa: BLE001
                    logger.warning(
                        "%s %s quote lookup failed: %s",
                        decision.action, decision.symbol, e,
                    )
                    quote = None
                ask = quote.get("ask_price") if isinstance(quote, dict) else None
                bid = quote.get("bid_price") if isinstance(quote, dict) else None
                if _submit_window_overrun(ctx):
                    ctx.desk_latency_stall = True
                    logger.warning(
                        "%s %s NOT SUBMITTED — latency blew the window "
                        "(encoded post-Risk budget %.1fs).",
                        decision.action, decision.symbol,
                        float(getattr(ctx, "entry_submit_budget_s", 0.0) or 0.0),
                    )
                    _record_execution_skip(
                        pipeline, ctx, decision.symbol, "latency_window",
                        "latency blew the window",
                    )
                    continue
                slippage_bps = _entry_slippage_bps(pipeline)
                if not is_short and isinstance(ask, (int, float)) and ask > 0:
                    # The protection cap and the offer are two different
                    # numbers, and when they disagree the ORDER CANNOT FILL.
                    #
                    # 2026-08-27 VLO: reference $349.99, ask $350.96 (28bp
                    # above it), cap 25bp -> limit $350.86. That limit sits
                    # TEN CENTS BELOW the offer. A buy limit below the ask
                    # does not fill, by definition — Alpaca fills a limit at
                    # the limit or better, and there was no better. The order
                    # sat unfilled for 31s, the entry-protection sweep
                    # cancelled it, and the session still reported
                    # `status: executed`. The trade was never possible; the
                    # system just never said so.
                    #
                    # Price protection itself is correct and stays: crossing
                    # an abnormal book at the open is how you pay 3% for a
                    # 0.3% idea. What changes is that an unfillable order is
                    # now a DECISION with a reason, not a doomed submission.
                    # A LIMIT IS A CEILING, NOT A PRICE.
                    #
                    # This is the correction that matters. Alpaca fills a buy
                    # limit at the NBBO or better — submitting $50.05 when the
                    # offer is $50.02 does not pay $50.05, it pays $50.02. So
                    # shaving the limit down toward the offer buys NOTHING and
                    # costs fills. The old `min(ask * 1.0005, cap)` treated the
                    # limit as if it were the execution price and haggled over
                    # it, which is how VLO ended up bid ten cents under a
                    # market it was trying to cross.
                    #
                    # Worse, the `ask` being haggled against is not the ask we
                    # trade at. This account is entitled to IEX, not SIP
                    # (verified 2026-08-27: a SIP quote request returns
                    # "subscription does not permit querying recent SIP
                    # data"). IEX is a single venue carrying a small share of
                    # volume, and its top of book is routinely stale or absurd
                    # — CCJ quoted bid $92.96 / ask $107.10, a 15% spread, in
                    # the middle of a normal session. Alpaca's matching engine
                    # uses the consolidated NBBO. Pricing an order against IEX
                    # while filling against NBBO is the root cause.
                    #
                    # So: set the limit AT the ceiling we are willing to pay,
                    # and let the match happen at the real NBBO underneath it.
                    # Price protection is unchanged — `slippage_bps` still
                    # bounds the worst possible fill — it just stops being
                    # self-defeating.
                    cap, offer_limit, ask_premium_bps = entry_bound(
                        pipeline, ctx, decision.symbol, market_price, ask,
                        slippage_bps, is_short=False,
                    )

                    # THE IEX ASK DOES NOT DECIDE ANYTHING HERE (board item
                    # 183, 2026-09-30). It used to: an entry was refused when
                    # `ask > cap * 1.02`, a multiple its own comment called
                    # "deliberately loose because the input is" — a number
                    # chosen to absorb how wrong this venue's top of book can
                    # be, which is not a quantity anyone had measured.
                    #
                    # Measured now, against every firing the gate has on
                    # record (8 rows, `execution_skip` evidence, 2026-09-15 to
                    # 2026-09-24 — the whole life of the telemetry): in all 8
                    # the reference was RIGHT and the ask was garbage. The
                    # reference matched the price the name was actually
                    # trading at to within a few bp, while the quoted ask sat
                    # 392 to 669bp above the HIGHEST price that name traded
                    # anywhere in a +/-15 minute window around the refusal,
                    # and 6 of the 8 were trading strictly INSIDE this ceiling
                    # at the instant they were refused. The gate turned away 8
                    # risk-approved entries and caught zero runaway books.
                    # `src/trader_feed.py` already refuses to render this code
                    # to the owner for the same reason.
                    #
                    # It was never protecting money either. A limit at `cap`
                    # cannot fill above `cap`, whatever the ask claims. When
                    # the market really has run through the ceiling the order
                    # simply rests unfilled inside the bounded entry window
                    # — 90 seconds (`_ENTRY_FILL_TIMEOUT_S`), not the rest of
                    # the session, with the parent order's own tif at DAY —
                    # and the entry-protection sweep cancels it — the same
                    # no-trade the skip produced, minus the refusals of names
                    # that had not moved. So the gate is gone and no multiple
                    # replaces it: the ceiling is its own protection.
                    #
                    # The far-through reading is still WRITTEN DOWN, because a
                    # venue quoting hundreds of bp away from the tape is a
                    # real data fact. A record needs no threshold — it fires
                    # on the ceiling itself.
                    # THE POOL, NOT THE PRICE (board item 183 rework). The
                    # reading still decides nothing about WHETHER to submit —
                    # the order goes either way. What it now decides is
                    # ORDER OF SERVICE against the deployment pool, because
                    # deleting the old skip removed the one thing that used
                    # to stop a possibly-unfillable entry from drawing that
                    # pool: `entry_budget -= estimated_cost` fires on
                    # submission, `order_ceiling = min(entry_budget, ...)` is
                    # read by every later candidate in this same loop, and
                    # the draw is never released.
                    #
                    # Releasing it on cancel would not help and is not what
                    # this does: `entry_budget` is a local of this stage and
                    # is already dead by the time the 90s fill timeout
                    # cancels, and the next session recomputes the pool from
                    # the broker anyway. The only place the draw can be made
                    # to matter is inside this loop, so the far-through name
                    # goes to the BACK of the submit queue, once.
                    #
                    # This is strictly LESS authority than the same reading
                    # carried yesterday, when it refused the entry outright.
                    # When the quote is noise (8 times out of 8 on record)
                    # the cost is bounded at being submitted later in the
                    # same burst; when the market really has run, a resting
                    # order stops starving a name that could have filled.
                    if (
                        ask > cap
                        and queue_index < original_entry_count
                        and decision.symbol not in deferred_far_through
                    ):
                        deferred_far_through.add(decision.symbol)
                        submit_queue.append(decision)
                        logger.info(
                            "BUY %s deferred to the back of the submit queue "
                            "— the displayed IEX offer $%.4f is through the "
                            "%.0fbp ceiling $%.4f, so it draws the deployment "
                            "pool after the names quoting inside theirs. Not "
                            "a refusal: it is submitted below.",
                            decision.symbol, ask, slippage_bps, cap,
                        )
                        _record_pipeline_event(
                            pipeline, ctx, decision.symbol, "execution",
                            "entry_deferred_behind_clean_quotes",
                            "buy_ask_above_cap",
                            detail=(
                                f"IEX ask ${ask:.4f} through the "
                                f"{slippage_bps:.0f}bp ceiling ${cap:.4f}; "
                                f"moved to the back of the submit queue so it "
                                f"draws the deployment pool last"
                            ),
                        )
                        continue

                    if ask > cap:
                        logger.warning(
                            "BUY %s submitted anyway — the displayed IEX offer "
                            "$%.4f is %.1fbp above the $%.4f reference and "
                            "through the %.0fbp ceiling $%.4f. IEX is not the "
                            "NBBO the order fills against; the limit cannot "
                            "pay more than the ceiling either way.",
                            decision.symbol, ask, ask_premium_bps,
                            market_price, slippage_bps, cap,
                        )
                        _record_pipeline_event(
                            pipeline, ctx, decision.symbol, "execution",
                            "venue_quote_through_ceiling", "buy_ask_above_cap",
                            detail=(
                                f"IEX ask ${ask:.4f} is {ask_premium_bps:.1f}bp "
                                f"above reference ${market_price:.4f}, through "
                                f"the {slippage_bps:.0f}bp ceiling ${cap:.4f}; "
                                f"order submitted at the ceiling"
                            ),
                        )

                    if limit_price is None or abs(limit_price - offer_limit) > 0.000001:
                        logger.info(
                            "BUY %s marketable-limit: prior $%s → ceiling $%.4f "
                            "(%.0fbp above reference $%.4f). Fills at NBBO or "
                            "better; IEX ask reads $%.4f (%.1fbp).",
                            decision.symbol,
                            f"{limit_price:.4f}" if limit_price is not None else "none",
                            offer_limit, slippage_bps, market_price,
                            ask, ask_premium_bps,
                        )
                    limit_price = offer_limit
                    sizing_price = max(sizing_price or 0, offer_limit)
                    # Safety net only: stall left the original entry unfillable
                    # but the live offer is still inside the pinned ceiling.
                    # Not the product — do not stamp this on a healthy path.
                    original_entry = getattr(decision, "entry_price", None)
                    if (
                        getattr(ctx, "desk_latency_stall", False)
                        and isinstance(original_entry, (int, float))
                        and ask > float(original_entry)
                        and ask <= cap
                    ):
                        used = dict(getattr(ctx, "catch_up_used", None) or {})
                        if not used.get(decision.symbol):
                            used[decision.symbol] = True
                            ctx.catch_up_used = used
                            _record_pipeline_event(
                                pipeline, ctx, decision.symbol, "execution",
                                "safety_net", "catch_up_inside_ceiling",
                                detail="stall left the original entry unfillable; "
                                "limit stays at the already-approved ceiling",
                            )
                elif is_short and isinstance(bid, (int, float)) and bid > 0:
                    # Mirror of the BUY ceiling: a sell-short limit is a
                    # FLOOR, not a price. Alpaca fills a short at the NBBO
                    # or better — submitting $49.95 when the bid is $50.00
                    # sells at $50.00, not at $49.95. Shaving the limit up
                    # toward the bid costs fills the same way VLO's shaved
                    # buy limit did. Set the limit AT the existing
                    # slippage floor and let the match happen underneath.
                    floor, bid_limit, bid_discount_bps = entry_bound(
                        pipeline, ctx, decision.symbol, market_price, bid,
                        slippage_bps, is_short=True,
                    )

                    # Mirror of the BUY side above, and it goes for the same
                    # measured reason: the IEX bid does not decide anything
                    # here either. The old `bid < floor / 1.02` inverted the
                    # BUY multiple, so it inherited an unmeasured tolerance
                    # for venue noise rather than adding a second one. A sell
                    # -short limit at `floor` cannot fill below `floor`, so
                    # refusing on a quote this account's own code calls
                    # routinely absurd only loses the entries where the venue
                    # was wrong. Recorded, not refused; no multiple.
                    # Mirror of the BUY deferral above, and it applies only
                    # when the pool is the ladder's GROSS headroom — a short
                    # does not draw a settled-cash pool at all (D11), so on
                    # the cash fallback there is no pool for it to hold and
                    # nothing to defer.
                    if (
                        bid < floor
                        and budget_is_gross
                        and queue_index < original_entry_count
                        and decision.symbol not in deferred_far_through
                    ):
                        deferred_far_through.add(decision.symbol)
                        submit_queue.append(decision)
                        logger.info(
                            "SHORT %s deferred to the back of the submit "
                            "queue — the displayed IEX bid $%.4f is through "
                            "the %.0fbp floor $%.4f, so it draws the gross "
                            "deployment pool after the names quoting inside "
                            "theirs. Not a refusal: it is submitted below.",
                            decision.symbol, bid, slippage_bps, floor,
                        )
                        _record_pipeline_event(
                            pipeline, ctx, decision.symbol, "execution",
                            "entry_deferred_behind_clean_quotes",
                            "short_bid_below_floor",
                            detail=(
                                f"IEX bid ${bid:.4f} through the "
                                f"{slippage_bps:.0f}bp floor ${floor:.4f}; "
                                f"moved to the back of the submit queue so it "
                                f"draws the gross deployment pool last"
                            ),
                        )
                        continue

                    if bid < floor:
                        logger.warning(
                            "SHORT %s submitted anyway — the displayed IEX bid "
                            "$%.4f is %.1fbp below the $%.4f reference and "
                            "through the %.0fbp floor $%.4f. IEX is not the "
                            "NBBO the order fills against; the limit cannot "
                            "sell below the floor either way.",
                            decision.symbol, bid, bid_discount_bps,
                            market_price, slippage_bps, floor,
                        )
                        _record_pipeline_event(
                            pipeline, ctx, decision.symbol, "execution",
                            "venue_quote_through_ceiling", "short_bid_below_floor",
                            detail=(
                                f"IEX bid ${bid:.4f} is {bid_discount_bps:.1f}bp "
                                f"below reference ${market_price:.4f}, through "
                                f"the {slippage_bps:.0f}bp floor ${floor:.4f}; "
                                f"order submitted at the floor"
                            ),
                        )

                    if limit_price is None or abs(limit_price - bid_limit) > 0.000001:
                        logger.info(
                            "SHORT %s marketable-limit: prior $%s → floor "
                            "$%.4f (%.0fbp below reference $%.4f). Fills at "
                            "NBBO or better; IEX bid reads $%.4f (%.1fbp).",
                            decision.symbol,
                            f"{limit_price:.4f}" if limit_price is not None else "none",
                            bid_limit, slippage_bps, market_price,
                            bid, bid_discount_bps,
                        )
                    limit_price = bid_limit
                    # `bid_limit` is the LIMIT (a marketable floor BELOW
                    # market); it is deliberately NOT the sizing divisor.
                    # docs/WORK.md item 120: dividing the allocation by a
                    # below-market price OVER-sizes the short (more shares) —
                    # the dangerous direction. Sizing stays anchored to the
                    # today print set above, mirroring the BUY, which raises
                    # its divisor to the offer ceiling (fewer shares) and
                    # never lowers it.
                    original_entry = getattr(decision, "entry_price", None)
                    if (
                        getattr(ctx, "desk_latency_stall", False)
                        and isinstance(original_entry, (int, float))
                        and bid < float(original_entry)
                        and bid >= floor
                    ):
                        used = dict(getattr(ctx, "catch_up_used", None) or {})
                        if not used.get(decision.symbol):
                            used[decision.symbol] = True
                            ctx.catch_up_used = used
                            _record_pipeline_event(
                                pipeline, ctx, decision.symbol, "execution",
                                "safety_net", "catch_up_inside_ceiling",
                                detail="stall left the original entry unfillable; "
                                "limit stays at the already-approved floor",
                            )

                # RC1: code-enforced ATR stop-distance floor at entry. The
                # P1 prompt rule ("fresh-entry stops never tighter than
                # 1×ATR") is advisory — LLM output still occasionally lands
                # stops inside one day's range, which converts routine
                # volatility into a same-week exit. Widen to 1×ATR(14) from
                # bars already fetched by research; qty_by_risk below sizes
                # against the wider distance, so per-trade $ risk is
                # unchanged. No bars → no floor (behavior identical).
                #
                # BUY-only (`not is_short`): the constructor's own
                # `_widen_stop_past_noise` (D5) already applies a mirrored,
                # direction-aware ATR floor to a SHORT's stop before this
                # code ever sees it; this is a SECOND, execution-time-only
                # belt that was never extended to shorts as part of this
                # stage.
                #
                # 2026-09-02: it is now also skipped for a stop the
                # constructor HONOURED at a computed structural level. This
                # belt was the last place spec §12.1 was being undone. §12.1
                # says the ATR floor applies only when nothing computed
                # backs the stop, and the constructor implements that — but
                # this code then re-applied a 1x ATR floor to the result,
                # against an ATR recomputed here from `ctx.symbols_bars`
                # rather than the `analysis.atr_14` the constructor used. Two
                # readings of the same quantity, and the larger one silently
                # won, moving the stop off the level and shrinking the R/R
                # the re-check below then judges. The constructor already
                # applies `absolute_min_stop_atr_multiple` (1x ATR) to a
                # level-backed stop, so the protection is not lost — it is
                # applied once, by the stage that can see the levels.
                stop_price = decision.stop_loss
                level_backed = decision.stop_rule in LEVEL_BACKED_STOP_RULES
                if level_backed and not is_short:
                    logger.info(
                        "BUY %s: execution-time ATR stop floor skipped — the "
                        "constructor honoured this stop at a computed "
                        "structural level [%s]. Re-widening it here would "
                        "undo §12.1 against a second ATR reading.",
                        decision.symbol, decision.stop_rule,
                    )
                if not is_short and not level_backed and stop_price > 0 and sizing_price > stop_price:
                    try:
                        bars = ctx.symbols_bars.get(decision.symbol) or []
                        atr14 = None
                        if len(bars) >= 15:
                            from src.data.technical import compute_indicators
                            atr14 = compute_indicators(decision.symbol, bars).atr_14
                        if atr14 and atr14 > 0 and (sizing_price - stop_price) < atr14:
                            widened = round(sizing_price - atr14, 2)
                            logger.warning(
                                "BUY %s: stop $%.2f is %.2f×ATR from entry "
                                "$%.2f — widening to $%.2f (1×ATR14=$%.2f "
                                "floor; qty sizing compensates)",
                                decision.symbol, stop_price,
                                (sizing_price - stop_price) / atr14,
                                sizing_price, widened, atr14,
                            )
                            stop_price = widened
                    except Exception as e:
                        logger.warning("ATR stop floor skipped for %s: %s",
                                       decision.symbol, e)

                # Geometry may have moved since the Risk Manager audited
                # (ATR-widened stop, or limit raised to market). Reward:risk
                # — computed, thin, or unmeasurable — is never a skip
                # (owner 2026-09-17). The retired 1.2 belt killed RSG on
                # 2026-09-16; renaming that skip is also a defect.
                geometry_changed = (
                    stop_price != decision.stop_loss
                    or (decision.entry_price > 0 and sizing_price > decision.entry_price)
                )
                payoff_skip = _execution_payoff_skip_reason(
                    decision,
                    sizing_price=sizing_price,
                    stop_price=stop_price,
                    geometry_changed=geometry_changed,
                    is_short=is_short,
                )
                if payoff_skip is not None:
                    raise RuntimeError(
                        "reward:risk execution skip is retired; "
                        f"got {payoff_skip!r} for {decision.symbol}"
                    )
                if (
                    not is_short and geometry_changed
                    and decision.take_profit > 0
                ):
                    logger.info(
                        "BUY %s: execution moved the geometry (entry $%.2f -> "
                        "$%.2f, stop $%.2f -> $%.2f) — no reward-side skip "
                        "applies (invented R/R gates retired).",
                        decision.symbol, decision.entry_price, sizing_price,
                        decision.stop_loss, stop_price,
                    )

                # ROOT FIX (2026-10-04, measured live): REPLACES the fallback
                # divisor above — size the risk budget against the price the
                # desk is ABOUT TO PAY. It was bound before the marketable
                # limit existed, so realised fill-to-stop was WIDER than the
                # sized distance and positions carried more than the
                # authorised ~1% of equity: 6.1% median BUY overshoot (25%
                # worst), 10.9% on a short. The limit is known here, so no
                # buffer and no multiplier — the budget divides by it.
                # Identical both ways, the worst fill either way (ruling).
                if isinstance(limit_price, (int, float)) and limit_price > 0:
                    risk_sizing_price = float(limit_price)

                # Spec §11.1. Exact sizing when the flag is on AND the broker
                # confirms the symbol is fractionable; whole shares otherwise.
                # Resolved ONCE per symbol here so every share count below —
                # allocation, risk budget, cash re-size — is quantized the
                # same way. Two different roundings inside one sizing decision
                # is how a stop ends up covering a different number of shares
                # than the entry bought.
                fractional = _fractional_sizing_allowed(
                    pipeline, decision.symbol, is_short=is_short,
                )
                qty_by_alloc = _size_shares(
                    pipeline,
                    (total_value * decision.allocation_pct / 100) / sizing_price,
                    fractional=fractional,
                )
                # Same helper the cash-sweep preflight sized funding with —
                # one definition, so the dollars released can never drift
                # from the dollars spent.
                qty_by_risk = _qty_by_risk_budget(
                    pipeline, total_value=total_value,
                    sizing_price=risk_sizing_price, stop_price=stop_price,
                    is_short=is_short, fractional=fractional,
                )
                if qty_by_risk is not None and qty_by_risk < qty_by_alloc:
                    _risk_pct = _risk_budget_pct(pipeline)
                    logger.info(
                        "Vol-adjusted sizing for %s: qty_by_alloc=%s → qty_by_risk=%s "
                        "(risk %.2f/share, budget $%.0f = %.1f%% of equity)",
                        decision.symbol, _fmt_shares(qty_by_alloc),
                        _fmt_shares(qty_by_risk),
                        abs(risk_sizing_price - stop_price),
                        total_value * _risk_pct / 100, _risk_pct,
                    )
                    qty = qty_by_risk
                else:
                    qty = qty_by_alloc
                if qty <= 0:
                    logger.warning("Calculated qty=0 for %s, skipping", decision.symbol)
                    _record_execution_skip(
                        pipeline, ctx, decision.symbol, "qty_zero",
                        f"allocation {decision.allocation_pct:.2f}% at "
                        f"${sizing_price:.2f} rounds to zero shares",
                    )
                    continue

                estimated_cost = qty * sizing_price
                # The ceiling THIS order may reach: the batch pool, or the
                # single-name cap, whichever is lower. Two different jobs —
                # the pool stops the SESSION deploying past the ladder rung,
                # the cap stops ONE order draining the pool.
                order_ceiling = min(entry_budget, single_name_cap)
                # D11: a SHORT is never trimmed or refused here. It does not
                # spend settled cash (it sells borrowed shares), and the caps
                # (D9) plus the borrow gate (D6) are the sole control surface
                # for a short. It DOES consume gross exposure, so it draws
                # the ladder pool down after submission below — but it is
                # never sized by it, which keeps the short path exactly as it
                # shipped.
                if not is_short and estimated_cost > order_ceiling:
                    affordable_qty = _size_shares(
                        pipeline, order_ceiling / sizing_price,
                        fractional=fractional,
                    )
                    # The skip reason stays `insufficient_cash` even though
                    # the binding number is no longer always cash: it is a
                    # persisted evidence code the funnel, the trader feed and
                    # the blocked-proposal digest already read, and renaming
                    # it would orphan every historical row. The detail line
                    # carries the truth.
                    if affordable_qty <= 0:
                        logger.warning(
                            "Skipping BUY %s: estimated cost $%.2f exceeds the "
                            "$%.2f still deployable — %s",
                            decision.symbol, estimated_cost, order_ceiling,
                            budget_note,
                        )
                        _record_execution_skip(
                            pipeline, ctx, decision.symbol, "insufficient_cash",
                            f"estimated cost ${estimated_cost:.2f} exceeds the "
                            f"${order_ceiling:.2f} still deployable "
                            f"({budget_note})",
                        )
                        continue
                    logger.warning(
                        "Resizing BUY %s from %s to %s share(s): only $%.2f is "
                        "still deployable — %s",
                        decision.symbol, _fmt_shares(qty),
                        _fmt_shares(affordable_qty), order_ceiling, budget_note,
                    )
                    qty = min(qty, affordable_qty)
                    estimated_cost = qty * sizing_price
                    # Fixed 2026-09-24: this used to refuse the re-sized
                    # order outright ("below_min_notional") whenever it fell
                    # under the flat `min_order_usd` floor — an arbitrary
                    # $500 with no broker minimum behind it
                    # (config/number_ledger.yaml), and Alpaca charges no
                    # stock commission. With fractional sizing on,
                    # `affordable_qty` above is already guaranteed nonzero
                    # (the `affordable_qty <= 0` branch already refused as
                    # `insufficient_cash`), so a $3 residue now simply buys
                    # 0.0281 shares rather than being refused for smallness.
                    _record_pipeline_event(
                        pipeline, ctx, decision.symbol, "funding", "resized",
                        "confirmed_cash_partially_funded_order",
                        approved_qty=qty_by_risk if qty_by_risk is not None and qty_by_risk < qty_by_alloc else qty_by_alloc,
                        resized_qty=qty,
                        deployment_budget=entry_budget,
                        order_ceiling=order_ceiling,
                    )

                # Long scale-in path B (owner 2026-09-15): if this BUY adds
                # to a name that already has a resting protective sell,
                # cancel that sell, confirm the cancel via trade_updates,
                # then submit. WAL is written first so a crash cannot leave
                # the position naked without a recovery row. Short adds are
                # blocked above: scale-in is the long path.
                if not is_short:
                    from src.execution.scale_in import prepare_long_add
                    add_prep = prepare_long_add(
                        broker=pipeline.broker, db=pipeline.db,
                        symbol=decision.symbol, positions=positions,
                        intended_stop=stop_price,
                    )
                else:
                    # Short scale-in path (owner-approved). The buy-stop is
                    # cancelled inside prepare_short_add, so the min-order
                    # FLOOR must run FIRST — a below-floor add must be dropped
                    # BEFORE any protection comes off. D11 keeps a short off
                    # the budget-resize floor above (it never spends cash), so
                    # this is where the floor is re-applied for a short add.
                    from src.execution.scale_in import (
                        held_signed_qty, prepare_short_add,
                    )
                    if held_signed_qty(positions, decision.symbol) < 0:
                        floor_usd = _min_order_usd(pipeline)
                        if estimated_cost < floor_usd:
                            logger.warning(
                                "Skipping SHORT add %s: order $%.2f (%s sh) is "
                                "below the $%.0f minimum worth trading — dropped "
                                "before any protective buy-stop is cancelled",
                                decision.symbol, estimated_cost,
                                _fmt_shares(qty), floor_usd,
                            )
                            _record_execution_skip(
                                pipeline, ctx, decision.symbol,
                                "below_min_notional",
                                f"short add ${estimated_cost:.2f} is below the "
                                f"${floor_usd:,.0f} minimum worth trading",
                            )
                            _record_pipeline_event(
                                pipeline, ctx, decision.symbol, "funding",
                                "refused", "short_add_below_min_notional",
                                resized_notional=estimated_cost,
                                min_order_usd=floor_usd,
                            )
                            continue
                    add_prep = prepare_short_add(
                        broker=pipeline.broker, db=pipeline.db,
                        symbol=decision.symbol, positions=positions,
                        intended_stop=stop_price,
                    )
                if add_prep is not None:
                    if add_prep.skip_reason:
                        _record_execution_skip(
                            pipeline, ctx, decision.symbol,
                            add_prep.skip_reason, add_prep.skip_detail,
                        )
                        _record_pipeline_event(
                            pipeline, ctx, decision.symbol, "scale_in",
                            "skipped", add_prep.skip_reason,
                            detail=add_prep.skip_detail,
                        )
                        continue
                    if add_prep.cancelled:
                        # Persisted event code kept as-is (the refusal-
                        # signature registry and historical rows read it); it
                        # covers a cancelled buy-stop on a short add too.
                        _record_pipeline_event(
                            pipeline, ctx, decision.symbol, "scale_in",
                            "protective_sell_cancelled",
                            "cancel_confirmed_via_trade_updates",
                            wal_row_id=add_prep.wal_row_id,
                            intended_stop=add_prep.intended_stop,
                            held_qty_before=add_prep.held_qty_before,
                        )

                # Write-ahead intent: insert a pending row BEFORE calling
                # the broker. Closes the BUY-side phantom-fill window the
                # audit surfaced — pre-fix, submit_order could return
                # successfully and a SIGKILL before db.insert_trade left
                # the broker with an accepted order and the DB with no
                # row. _reconcile_fills queries by broker_order_id, so
                # there was no recovery path for the phantom. With the
                # pending row pre-inserted, even a crash mid-submit
                # leaves a fill_status='pending_submit' row the operator
                # (or a periodic cleanup) can reconcile against the
                # broker's order list.
                executed_price = limit_price if limit_price is not None else sizing_price
                # Phase 3.1 — pin the analyst's stated horizon and setup type to
                # the trade row at entry. Everything downstream that asks "is
                # this position on schedule?" must measure against THIS number,
                # not against the system's own rolling average hold time, which
                # shrinks every time the system sells early and thereby makes
                # the next position look stalled. None when the analysis is
                # missing (resume lanes, sweep buys): the reviewer then gets no
                # pace figure at all, which is correct — it never gets a
                # fabricated one.
                entry_analysis = next(
                    (a for a in (ctx.analyses or []) if a.symbol == decision.symbol),
                    None,
                )
                # Item 82: `setup_type` was being classified a SECOND time
                # here, independently of `PortfolioConstructor._build_buy`/
                # `_build_short` (see `TradeDecision.setup_type` in
                # models.py, which exists specifically so execution does not
                # have to re-derive this fact). On a scale-in ADD to an
                # already-held name this second lookup re-read TODAY's
                # technical read and wrote it onto the new row —
                # `get_symbol_last_buy` returns the newest row, so this
                # silently RECLASSIFIED a position whose setup_type was
                # already pinned on its original entry, which is worse than
                # a mere disagreement: pace/progress (disabled for a
                # breakout) could flip back on, or off, on a held position
                # with no new entry decision behind the change. A genuinely
                # new entry has no prior pinned row and reads the single
                # value the constructor already classified, carried on the
                # decision.
                _is_scale_in = add_prep is not None and add_prep.is_scale_in
                # Item 82 scale-in carry-forward lives in src/entry_evidence.py.
                _existing_buy, _setup_type_unused, _ceiling_unused = (
                    _resolve_entry_pins(
                        pipeline.db, decision,
                        is_short=is_short, is_scale_in=_is_scale_in,
                    )
                )
                pending_row_id, entry_side = insert_pending_entry(
                    db=pipeline.db, decision=decision, add_prep=add_prep,
                    is_short=is_short, qty=qty, executed_price=executed_price,
                    run_id=run_id, stop_price=stop_price, decision_id=decision_id,
                    entry_analysis=entry_analysis, decision_model=ctx.decision_model,
                )

                _record_scale_in_own_verdict(
                    pipeline.db, logger, run_id=run_id, decision_id=decision_id,
                    decision=decision, prior_row=_existing_buy,
                    is_scale_in=_is_scale_in,
                )

                try:
                    # Set BEFORE the call: if submit raises, the broker may
                    # still have accepted the BUY. Restoring the cancelled
                    # stop at the OLD size is the daily-breaker / partial-
                    # fill bug (under-cover a grown position, and a working
                    # BUY plus a restored SELL is the wash-trade block).
                    # Leave the scale-in WAL row; drain rearms at broker qty.
                    submit_attempted = True
                    order = pipeline.broker.submit_order(
                        symbol=decision.symbol, qty=qty, side=entry_side,
                        limit_price=limit_price,
                        # PASSED THROUGH AS-IS (docs/WORK.md item 88). This
                        # used to read `stop_price if stop_price > 0 else
                        # None`, which laundered a garbage stop into the
                        # broker's "no stop was requested" case — so a zero
                        # reaching here submitted an unprotected entry and
                        # the broker never got the chance to refuse it.
                        # Every decision on this loop is a BUY or a SHORT and
                        # therefore OWES a stop, so there is nothing legitimate
                        # to convert to None: `submit_order` judges the value
                        # and returns `rejected_bad_stop` when it is not a
                        # price (surfaced below as `unusable_stop`).
                        stop_loss_price=stop_price,
                        reference_price=market_price,
                        # WORDING ONLY (see `submit_order`'s docstring): the
                        # same measured ATR(14) the constructor sized this
                        # trade against, so a fat-finger refusal can name
                        # the stock's own daily range instead of a bare
                        # percentage. None on the resume/sweep lanes that
                        # carry no analysis — the message then omits the
                        # range rather than inventing one.
                        atr=getattr(entry_analysis, "atr_14", None),
                    )
                except Exception as e:
                    # Submit raised — broker may or may not have the
                    # order. Leave the row as 'pending_submit' so the
                    # next session's orphan sweep
                    # (_reconcile_orphan_pending_submits) can match it
                    # against broker activity by symbol + qty + time
                    # window. Audit 2026-05-27: a prior version called
                    # mark_trade_submit_failed here, but
                    # get_orphaned_pending_submits filters only
                    # fill_status='pending_submit' — flipping it to
                    # submit_failed silently HID the row from the
                    # recovery path it was supposed to be flagged for.
                    _record_pipeline_event(
                        pipeline, ctx, decision.symbol, "order", "submit_unknown",
                        "broker_submit_exception", detail=str(e),
                        trade_row_id=pending_row_id,
                    )
                    raise

                if not pipeline._order_accepted(order, decision.symbol, entry_side):
                    # `order["status"]` distinguishes WHO actually stopped
                    # this — our own pre-flight guards return a status
                    # before the order ever reaches the broker
                    # (rejected_outlier=fat-finger guard, kill_switch_halted)
                    # and both come back with no `id`, same as a real
                    # broker-side rejection. Collapsing all three into one
                    # "broker rejected" skip reason (pre-2026-09-17) read as
                    # if the broker had refused a sane order every time,
                    # when it was usually QAMC's own desk safety check
                    # blocking a bad price before the broker ever saw it
                    # (operator-reported, 2026-09-17: a fat-finger-guard
                    # rejection alerted as "broker rejected short"). Mark
                    # the pending row failed so it doesn't poison
                    # calibration as a "submitted" trade we never tracked.
                    # Distinct from the submit-raised case above: here we
                    # KNOW the order did not go live, so there's no orphan
                    # to sweep.
                    from src.execution.scale_in import restore_after_failed_add
                    restore_after_failed_add(
                        pipeline.broker, pipeline.db, add_prep, decision.symbol,
                    )
                    pipeline.db.mark_trade_submit_failed(pending_row_id)
                    order_status = str((order or {}).get("status") or "")
                    order_detail = (order or {}).get("detail")
                    if order_status == "rejected_outlier":
                        # The plain-word fact only (e.g. "stop $9.66 is 24%
                        # from price $7.79", from broker.py's
                        # _PLAIN_PRICE_LABELS) — WHO blocked it ("desk
                        # safety check, not the broker") is the Telegram
                        # formatter's job (src/trader_feed.py's
                        # `_SKIP_WHO_LABELS`), not repeated here.
                        skip_reason = "fat_finger_guard"
                        skip_detail = order_detail or "price is too far from the market price"
                    elif order_status == "rejected_bad_stop":
                        # Stop-side sanity (non-finite, non-positive, wrong
                        # side of entry) — desk-side, like the fat-finger
                        # guard, not the broker. A SEPARATE reason: folded
                        # into `fat_finger_guard` it would tell the owner a
                        # price was "too far" when the stop could never work.
                        skip_reason = "unusable_stop"
                        skip_detail = order_detail or "the stop price is not usable"
                    elif order_status == "rejected_bad_qty":  # order_gates.py
                        skip_reason = "bad_quantity"
                        skip_detail = order_detail or "the quantity is not usable"
                    elif order_status == "kill_switch_halted":
                        skip_reason = "kill_switch_halted"
                        skip_detail = order_detail or (
                            "the trading kill switch is active"
                        )
                    else:
                        skip_reason = "broker_rejected"
                        # Board item 89 clarity defect — "a missing broker
                        # reason on a rejection". The broker's own words are
                        # kept when it gave any; when it did not, the message
                        # says so instead of leaving the owner to wonder.
                        skip_detail = (
                            f"broker rejected {decision.action.lower()} "
                            f"{_fmt_shares(qty)} @ "
                            f"{'limit $%.2f' % limit_price if limit_price else 'market'}"
                            + (f" — broker said: {order_detail}" if order_detail
                               else " — the broker gave no reason the desk recorded")
                        )
                        # The raw broker status token is not appended to the
                        # owner-facing detail any more (it read
                        # "(status=rejected)"); it stays in the log line and
                        # the pipeline event above.
                    _record_pipeline_event(
                        pipeline, ctx, decision.symbol, "order", "rejected",
                        skip_reason, trade_row_id=pending_row_id, qty=qty,
                    )
                    _record_execution_skip(
                        pipeline, ctx, decision.symbol, skip_reason, skip_detail,
                    )
                    continue

                # Submit accepted — finalize the pending row with the
                # broker's order_id and flip to 'submitted'.
                pipeline.db.confirm_trade_submitted(
                    pending_row_id, broker_order_id=order.get("id"),
                )
                buy_accepted = True
                _record_pipeline_event(
                    pipeline, ctx, decision.symbol, "order", "submitted",
                    "broker_accepted", broker_order_id=order.get("id"), qty=qty,
                    limit_price=executed_price,
                )
                if isinstance(order, dict):
                    order.setdefault("action", decision.action)  # audit F5
                orders.append(order)
                if budget_is_gross or not is_short:
                    # The pool is drawn down by what the order CONSUMES of
                    # it, and the two budgets are consumed by different
                    # things. A ladder budget is GROSS headroom: a short
                    # occupies gross exactly as a long does (`gross_exposure`
                    # sums the magnitude of both), so it must draw the pool
                    # or a batch of shorts would leave the longs behind them
                    # sized against headroom that is already spent. The cash
                    # fallback is a settled-cash pool, which a short does not
                    # touch at all — D11, unchanged.
                    entry_budget -= estimated_cost
                order_type = "limit" if limit_price is not None else "market"
                logger.info(
                    "Executed: %s %s %s @ %s $%.2f",
                    decision.action.lower(), _fmt_shares(qty), decision.symbol,
                    order_type, executed_price,
                )
                # The entry still owes a protective stop: it is placed as a
                # separate GTC order AFTER the fill, because an OTO leg would
                # inherit the parent's DAY tif and be expired by the broker at
                # 16:00 ET the same day (2026-07-16 audit — positions were
                # naked every night). Deferred until all BUYs are submitted so
                # the fill waits don't serialize the submission burst.
                if isinstance(order, dict) and (
                    order.get("pending_stop_price") or (
                        add_prep is not None and add_prep.cancelled
                    )
                ):
                    # Scale-in: the add's own stop is not automatically the
                    # live one. Most-protective for a long is the HIGHEST
                    # trigger (already computed on the prep). Prefer that
                    # over pending_stop_price or a looser cancelled stop
                    # would be replaced by the add's wider number.
                    protect_stop = order.get("pending_stop_price") or 0
                    if (
                        add_prep is not None and add_prep.is_scale_in
                        and add_prep.intended_stop > 0
                    ):
                        protect_stop = add_prep.intended_stop
                    pending_entry_stops.append({
                        "symbol": decision.symbol,
                        "side": entry_side,
                        "order_id": order.get("id"),
                        "stop_price": protect_stop,
                        "qty": qty,
                        # Carried for the bounded re-peg (off by default).
                        # `reference_price` is the verified reference the
                        # slippage ceiling was computed from at SUBMISSION —
                        # the re-peg re-uses it rather than re-deriving a
                        # ceiling from a fresh quote, because a ceiling that
                        # follows the market is not a ceiling.
                        "reference_price": market_price,
                        "limit_price": limit_price,
                        "trade_row_id": pending_row_id,
                        "cover_full_position": bool(
                            add_prep is not None and add_prep.is_scale_in
                        ),
                        "held_qty_before": (
                            add_prep.held_qty_before if add_prep else 0.0
                        ),
                        "wal_row_id": (
                            add_prep.wal_row_id if add_prep else None
                        ),
                        "cancelled_specs": (
                            add_prep.specs if add_prep else []
                        ),
                        "intended_stop": (
                            add_prep.intended_stop if add_prep else 0.0
                        ),
                        # Board item 193: the monotonic instant the BROKER
                        # acknowledged the protective cancel. Carried to the
                        # rearm so the unprotected window is measured end to
                        # end inside one run, from broker acknowledgement to
                        # broker acknowledgement, not from row write times.
                        "cancel_confirmed_at": (
                            add_prep.cancel_confirmed_at if add_prep else None
                        ),
                    })
            except Exception as e:
                if (
                    add_prep is not None and add_prep.cancelled
                    and not buy_accepted
                ):
                    if submit_attempted:
                        logger.critical(
                            "scale-in: BUY submit for %s failed after the "
                            "protective sell was cancelled — WAL row %s "
                            "stays so drain rearms at the broker's current "
                            "qty; restoring the old stop size would under-"
                            "cover a fill that may already have landed",
                            decision.symbol, add_prep.wal_row_id,
                        )
                    else:
                        from src.execution.scale_in import restore_after_failed_add
                        restore_after_failed_add(
                            pipeline.broker, pipeline.db, add_prep,
                            decision.symbol,
                        )
                logger.error("Order failed for %s %s: %s", decision.action, decision.symbol, e)

        # Protect every filled entry (GTC stop-limit keyed to the ACTUAL fill).
        for spec in pending_entry_stops:
            if not spec.get("order_id"):
                continue
            try:
                # Single-shot reprice FIRST, protection second, always. The
                # reprice may hand back a different order id (Alpaca mints one
                # per replacement) plus any shares an ancestor order filled;
                # both feed straight into the stop so no filled share is left
                # without one. With `execution.repeg_enabled` off — the
                # default — this returns the same id and 0.0 without making a
                # single broker call.
                try:
                    entry_order_id, superseded_fill = _repeg_entry_order(
                        pipeline, ctx, spec,
                    )
                except Exception as repeg_exc:  # noqa: BLE001
                    # Protection must run even if the chase blows up. Fall
                    # back to the original id: at worst the re-peg did
                    # nothing, which is the failure direction we want.
                    logger.error(
                        "re-peg raised for %s: %s — protecting the ORIGINAL "
                        "order %s unchanged", spec["symbol"], repeg_exc,
                        spec["order_id"],
                    )
                    entry_order_id, superseded_fill = spec["order_id"], 0.0
                entry_side = spec.get("side", "buy")
                # End-of-session cancel of a still-unfilled entry lives
                # inside `place_entry_protection` (see its docstring for the
                # derivation); the callback is how the owner gets told, with
                # the prices this stage tried, which the broker does not know.
                protection = pipeline.broker.place_entry_protection(
                    symbol=spec["symbol"], order_id=entry_order_id,
                    stop_price=spec["stop_price"], requested_qty=spec["qty"],
                    superseded_filled_qty=superseded_fill,
                    side=entry_side,
                    on_unfilled_cancel=(
                        lambda info, _spec=spec:
                        _alert_owner_entry_cancelled(pipeline, _spec, info)
                    ),
                    cover_full_position=bool(spec.get("cover_full_position")),
                    held_qty_before=float(spec.get("held_qty_before") or 0),
                )
                _record_pipeline_event(
                    pipeline, ctx, spec["symbol"], "protection",
                    "placed" if protection else "not_placed",
                    "protective_stop_result",
                    entry_order_id=entry_order_id, stop_price=spec["stop_price"],
                    protective_order_id=(protection or {}).get("id") if isinstance(protection, dict) else None,
                )
                # Board item 193 — close the measured unprotected window.
                # Every scale-in cancel that reached the broker emits exactly
                # one of these, carrying the same `wal_row_id` as its
                # `protective_sell_cancelled` event, so an unpaired cancel is
                # visible as a missing partner rather than inferred from row
                # ids. `held_qty_before` is the WHOLE position the cancel
                # exposed, not the size of the add.
                _record_scale_in_window_closed(
                    pipeline, ctx, spec, covered=bool(protection),
                )
                # Spec §11.1 guard 2. The broker has already retried hard and
                # immediately (guard 1) by the time this is reached, so a
                # falsy `protection` means a position is open at the broker
                # with NO stop on it, and a non-zero `uncovered_qty` means
                # part of one is. Neither may be reported as a log line: a log
                # line is read after the fact, and the whole reason fractional
                # sizing is acceptable is that the unprotected window is brief
                # — which is only true if a HUMAN is told the moment it stops
                # being brief. Never lets an alerting failure abort the
                # session.
                _alert_owner_protection_failed(
                    pipeline, spec, protection, entry_order_id,
                )
                if spec.get("cover_full_position") or spec.get("wal_row_id") is not None:
                    from src.execution.scale_in import (
                        discharge_scale_in_wal,
                        restore_cancelled_stops,
                    )
                    filled_here = 0.0
                    try:
                        info = pipeline.broker.get_order_fill_info(
                            entry_order_id,
                        ) or {}
                        filled_here = float(info.get("filled_qty") or 0)
                    except Exception:  # noqa: BLE001
                        filled_here = 0.0
                    uncovered = 0.0
                    if isinstance(protection, dict):
                        try:
                            uncovered = float(protection.get("uncovered_qty") or 0)
                        except (TypeError, ValueError):
                            uncovered = 0.0
                    # A short add's protection is a BUY-stop; restore and
                    # write-back must both use the short side.
                    _spec_is_short = str(
                        spec.get("side", "buy")
                    ).lower() != "buy"
                    if protection is None and filled_here <= 0:
                        if restore_cancelled_stops(
                            pipeline.broker, spec["symbol"],
                            spec.get("cancelled_specs") or [],
                            side="buy" if _spec_is_short else "sell",
                        ):
                            discharge_scale_in_wal(
                                pipeline.db, spec.get("wal_row_id"),
                            )
                    elif protection is not None and uncovered <= 0:
                        from src.execution.stop_records import (
                            accepted_stop_order, write_back_stop_loss,
                        )
                        if accepted_stop_order(protection) or not isinstance(
                            protection, dict,
                        ):
                            write_back_stop_loss(
                                pipeline.db, spec["symbol"], spec["stop_price"],
                                is_short=_spec_is_short,
                            )
                        discharge_scale_in_wal(
                            pipeline.db, spec.get("wal_row_id"),
                        )
                    # else: fill happened and rearm did not fully cover.
                    # WAL stays. Guard 2 already paged the owner.
                # D7 (Stage 3): MANDATORY escalation for a SHORT. A long's
                # loss is bounded at -100%; a naked short's is not, so
                # relying on the next session's coverage-reconcile belt (the
                # long behaviour, unchanged above) is not an acceptable
                # exposure window here. If the protective stop could not be
                # placed after the entry actually filled shares, submit an
                # IMMEDIATE market COVER for the filled quantity and log it
                # loudly — this is not a normal exit, it is damage control.
                if protection is None and entry_side == "sell_short":
                    try:
                        fill_info = pipeline.broker.get_order_fill_info(entry_order_id) or {}
                        filled_qty = float(fill_info.get("filled_qty") or 0)
                    except Exception as fill_exc:  # noqa: BLE001
                        logger.critical(
                            "SHORT %s: could not even determine the filled "
                            "quantity after protection failed (%s) — treating "
                            "as the full requested qty %.4f to force a cover "
                            "attempt rather than leaving a possibly-naked "
                            "short untouched",
                            spec["symbol"], fill_exc, spec["qty"],
                        )
                        filled_qty = float(spec.get("qty") or 0)
                    # H1: on a SHORT SCALE-IN the protective buy-stop that
                    # covered the PRE-EXISTING short leg was already cancelled
                    # in prep, so covering only the add's fill (filled_qty)
                    # would leave that older leg naked — exactly the unbounded
                    # exposure D7 exists to prevent. Cover the ENLARGED short:
                    # the broker's current qty (magnitude), the same authority
                    # cover_qty_for_rearm uses, with the |fill|+|held| fallback
                    # when the broker cannot be read. For a NEW short this
                    # equals filled_qty, so the non-scale-in path is unchanged.
                    if spec.get("cover_full_position"):
                        from src.execution.scale_in import cover_qty_for_rearm
                        cover_qty = cover_qty_for_rearm(
                            pipeline.broker, symbol=spec["symbol"],
                            filled_qty=filled_qty,
                            held_qty_before=float(spec.get("held_qty_before") or 0),
                        )
                        if cover_qty < filled_qty:
                            # Never cover LESS than what we know filled.
                            cover_qty = filled_qty
                    else:
                        cover_qty = filled_qty
                    if cover_qty > 0:
                        logger.critical(
                            "SHORT %s: PROTECTIVE STOP FAILED after %.4f "
                            "share(s) filled — a naked short has UNBOUNDED "
                            "loss. Submitting an IMMEDIATE market COVER of the "
                            "full short (%.4f) instead of waiting for the next "
                            "reconcile pass.",
                            spec["symbol"], filled_qty, cover_qty,
                        )
                        try:
                            cover_order = pipeline.broker.submit_order(
                                symbol=spec["symbol"], qty=cover_qty, side="buy",
                            )
                            cover_id = (
                                cover_order.get("id")
                                if isinstance(cover_order, dict) else None
                            )
                            # Board item 183 follow-up (2026-09-30).
                            # `AlpacaBroker.submit_order` no longer RAISES on
                            # a rejection the broker's own response calls
                            # terminal — it returns
                            # `{"id": None, "status": "rejected_by_broker"}`.
                            # Every other `submit_order` caller in this repo
                            # already tests the RESULT via `_order_accepted`;
                            # this one only read `.get("id")`, so a rejected
                            # emergency cover would have written a
                            # `fill_status="submitted"` EMERGENCY_COVER row
                            # and filed a SUCCESS event for an order that
                            # does not exist — on the one path that runs
                            # when a SHORT has filled and its protective stop
                            # did NOT place, i.e. a naked short with
                            # unbounded loss and nobody paged. Raising here
                            # puts a non-accept back on EXACTLY the path a
                            # raised submit took before #786: the CRITICAL
                            # operator page and the `emergency_cover_failed`
                            # event in the `except` branch below, and no
                            # trade row, because the raise precedes
                            # `insert_trade`. `_order_accepted` also catches
                            # the desk's OWN pre-flight refusals (the
                            # fat-finger guard, the kill switch), which reach
                            # here identically id-less and are equally not a
                            # cover.
                            if not pipeline._order_accepted(
                                cover_order, spec["symbol"], "buy",
                            ):
                                raise RuntimeError(
                                    "broker did not accept the emergency "
                                    f"cover order: {cover_order!r}"
                                )
                            pipeline.db.insert_trade(
                                symbol=spec["symbol"], action="EMERGENCY_COVER",
                                qty=cover_qty, price=0.0,
                                reasoning=(
                                    "protective stop failed to place after a "
                                    "SHORT entry filled — immediate market "
                                    "cover of the full (enlarged) short to bound "
                                    "an otherwise naked short"
                                ),
                                run_id=run_id, broker_order_id=cover_id,
                                fill_status="submitted",
                            )
                            _record_pipeline_event(
                                pipeline, ctx, spec["symbol"], "protection",
                                "emergency_cover", "naked_short_protection_failed",
                                qty=cover_qty, broker_order_id=cover_id,
                            )
                        except Exception as cover_exc:  # noqa: BLE001
                            logger.critical(
                                "SHORT %s: EMERGENCY COVER ALSO FAILED (%s) — "
                                "%.4f share(s) are NAKED SHORT with NO "
                                "protective stop and NO cover in flight. "
                                "REQUIRES IMMEDIATE OPERATOR INTERVENTION.",
                                spec["symbol"], cover_exc, cover_qty,
                            )
                            _record_pipeline_event(
                                pipeline, ctx, spec["symbol"], "protection",
                                "emergency_cover_failed",
                                "naked_short_no_protection_no_cover",
                                qty=cover_qty, detail=str(cover_exc),
                            )
            except Exception as e:  # noqa: BLE001 — never abort the session here
                logger.error(
                    "entry protection raised for %s: %s — position may be "
                    "unprotected until the next coverage reconcile",
                    spec["symbol"], e,
                )
                _record_pipeline_event(
                    pipeline, ctx, spec["symbol"], "protection", "failed",
                    "protective_stop_exception", detail=str(e),
                    entry_order_id=spec["order_id"],
                )
                _record_scale_in_window_closed(
                    pipeline, ctx, spec, covered=False,
                )

        # Phase 14b — the rotation's outcome, both legs, recorded durably.
        # A sale that freed room for a BUY that then did not happen is the
        # exact churn the anti-rotation rules exist to prevent, so that
        # case is also paged (`_alert_rotation_buy_leg_missing`).
        _record_rotation_buy_leg_outcome(pipeline, ctx, orders)

        ctx.orders = orders
        return orders
