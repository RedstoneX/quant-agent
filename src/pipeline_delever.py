"""DeleverService — the Spec §11.2 gross-exposure ceiling and ladder. MONEY.

Conversion step 11 (docs/ARCHITECTURE.md §4). This code SELLS HELD POSITIONS
when the book is over-exposed: the forced de-lever against a margin deficit,
the ladder that resolves the ceiling from the drawdown, the ceiling enforcement
and its weakest-conviction variant, the deferred-ceiling discharge, the trim
submission, and the shortfall record and owner alerts. Every body is the former
`DeleverMixin` body, byte for byte; only the collaborators became constructor
parameters. `TradingPipeline` reaches it through `src/pipeline_delever_mixin.py`.
`apply_gross_ceiling`, `resolve_gross_ceiling`, `GROSS_LADDER` and the other
imported names resolve against THIS module (patch them here). Nothing here may
import `src.pipeline` (boundary clause 3).
"""

import logging
import math

from src.pipeline_context import RunContext
from src.risk.rules import (
    GROSS_LADDER,
    GROSS_LADDER_ALERT_PCT,
    GrossCeiling,
    SECTOR_SIDE_LONG,
    apply_gross_ceiling,
    count_aligned_sources,
    count_opposing_sources,
    distance_to_forced_liquidation_pct,
    gross_exposure,
    peak_to_trough_pct,
    position_side,
    resolve_gross_ceiling,
)
from src.verdicts import rank_verdicts

#: The moved code logged under `src.pipeline` before the move and still does;
#: binding the name rather than `__name__` keeps log records byte-identical.
logger = logging.getLogger("src.pipeline")


def _optional_risk_number(value) -> float | None:
    """Read an OPTIONAL numeric risk setting, or None.

    Same defensive posture as `_risk_setting` inside `TradingPipeline.__init__`
    (many tests build the pipeline against a MagicMock config, where attribute
    access auto-creates a child mock that pydantic coerces to 1.0), but for a
    setting whose absence is meaningful rather than an error — `None` lets
    `RiskConfig` apply its own documented default instead of a number nobody
    configured.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if value > 0 else None


def _risk_number(value, default: float) -> float:
    """`_optional_risk_number` with a documented fallback, for settings that
    always need a concrete number (§10.3's minimum order size)."""
    resolved = _optional_risk_number(value)
    return default if resolved is None else resolved


class DeleverService:
    """The gross-exposure ceiling and the de-levering ladder, built alone."""

    def __init__(
        self, *, config, broker, db, protection, sweeper, sweep_symbol,
        full_sell_qty, format_qty, compute_deployable_cash,
    ) -> None:
        # db is residual coupling (five methods called by name; only
        # insert_specialist_evidence is on the EventJournal port).
        self.config = config
        self.broker = broker
        self.db = db
        # protection carries the three order-shaped calls (ProtectionMixin).
        self._submit_protected_sell = protection._submit_protected_sell
        self._open_exit_relief = protection._open_exit_relief
        self._finalize_pending_protections = protection._finalize_pending_protections
        self._sweeper = sweeper                  # TradingPipeline._sweeper
        self._sweep_symbol = sweep_symbol        # TradingPipeline._sweep_symbol
        self._full_sell_qty = full_sell_qty      # TradingPipeline._full_sell_qty
        self._format_qty = format_qty            # TradingPipeline._format_qty
        self._compute_deployable_cash = compute_deployable_cash

    def _live_delever_price(
        self, symbol: str, side: str,
    ) -> tuple[float | None, float | None]:
        """Price a MUST-FILL emergency de-lever off the LIVE quote at submit
        time, never off a fixed % of a possibly-stale mark.

        The emergency de-lever exists to shed exposure NOW when the book is
        over its gross ceiling (or the cash-only account is on margin); it
        must fill regardless of how far a name has gapped. A fixed-% limit off
        a stale `current_price` is the wrong mechanism: on an 8/10/20% gap the
        limit rests ABOVE the falling market and the book stays over its
        ceiling exactly when it must come down (docs/WORK.md item 118); inside
        normal noise the same % is oversized. So this reads the CURRENT bid/ask
        and prices a MARKETABLE limit that crosses it:

          * SELL  -> a limit AT the live BID. A sell limit at/below the bid is
                     immediately marketable and fills at the bid however far
                     the name gapped, because the gap is already IN the quote.
          * COVER -> a limit AT the live ASK (the buy-side mirror).

        The returned reference price is the live MID, so the broker's
        fat-finger guard (`OUTLIER_MAX_DEVIATION`) sees a ~zero deviation
        between the limit and the reference and passes the order however far
        the live quote has moved from yesterday's mark — the guard is
        measuring the limit against a STALE mark today, which is what would
        reject a legitimately gapped fill.

        Returns ``(None, reference_or_None)`` when no usable live quote exists
        (missing/zero/non-finite bid-or-ask, or a broker/data failure) so the
        caller submits a MARKET order — the guaranteed fill, and the correct
        fallback when there is no live price to cross. No new % constant is
        introduced on either branch: the price is the live quote or the market
        itself.
        """
        bid = ask = None
        try:
            quote = self.broker.get_latest_quote(symbol) or {}
            raw_bid = quote.get("bid_price")
            raw_ask = quote.get("ask_price")
            if raw_bid is not None:
                b = float(raw_bid)
                bid = b if math.isfinite(b) and b > 0 else None
            if raw_ask is not None:
                a = float(raw_ask)
                ask = a if math.isfinite(a) and a > 0 else None
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "de-lever: live quote unusable for %s (%s) — falling back to a "
                "MARKET order, the guaranteed fill", symbol, exc,
            )
            bid = ask = None
        if bid is not None and ask is not None:
            mid: float | None = (bid + ask) / 2
        elif bid is not None:
            mid = bid
        elif ask is not None:
            mid = ask
        else:
            mid = None
        # COVER is a BUY (side='buy'); cross the ASK. Everything else is a
        # SELL; cross the BID. A missing side of the book -> None -> MARKET.
        limit = ask if side == "buy" else bid
        return limit, mid

    def _force_delever(self, ctx: RunContext) -> list[dict]:
        """Safety net for `allow_margin=False` accounts: clear the measured
        cash deficit deterministically, without waiting on an LLM.

        When cash is meaningfully negative at session start we do NOT trust
        the LLM to pick which positions to cut — we force-sell worst-
        performing first (most negative unrealized P&L, largest size as
        tiebreaker), hedges last, until projected cash is ≥ 0. This runs
        BEFORE any decision / review stage, so the rest of the session
        operates on a clean, cash-only snapshot.

        The ordering is a biggest-loser-first P&L rule (owner-ratified), NOT
        a risk measure: the goal is to CLEAR THE MEASURED CASH DEFICIT, and
        cutting the worst performers first is simply how the desk chooses
        what to give up to do it. The only risk-shaped rule in the sort is
        keeping inverse-ETF hedges in the last tier (see the ordering block
        below), so raising the cash does not strip directional protection off
        the longs that remain.

        Rationale: the DE-LEVER MANDATE in the PM / midday prompts is
        advisory — if the LLM emits only HOLDs, margin sits. Users who opt
        in to `allow_margin=False` want structural enforcement, not an LLM
        nudge. Speed and safety > LLM judgment here.

        The sell is priced off the LIVE quote at submit time, not a fixed % of
        a possibly-stale mark: a marketable limit AT the live bid (crosses the
        spread, so it fills however far the name has gapped — the gap is in the
        quote), and a MARKET order when no live quote is available (the
        guaranteed fill). We prioritize fill over price when clearing an
        unintended margin position (docs/WORK.md item 118): a fixed % of a
        stale price rests ABOVE the falling market on a gap day and leaves the
        deficit uncleared. See `_live_delever_price`. Note the contrast with
        the deleted daily-loss liquidator (docs/WORK.md item 32): this path
        clears a MEASURED cash deficit of known size, not a whole book on a gap
        day, and it is reached only when `allow_margin` is false.

        Returns the submitted orders list (empty when no de-lever is needed).
        ctx.cash / positions / total_value are refreshed from broker after
        fills so downstream stages see truth.
        """
        # `config` may be missing in tests that bypass __init__ via
        # TradingPipeline.__new__. Treat that as "not configured for cash-only
        # policy" and skip — the full-init pipeline always has config.
        risk_cfg = getattr(getattr(self, "config", None), "risk", None)
        if risk_cfg is None or bool(getattr(risk_cfg, "allow_margin", False)):
            return []
        from src.risk.constants import MARGIN_DEFICIT_FLOOR_USD
        if ctx.cash >= -MARGIN_DEFICIT_FLOOR_USD:
            return []

        deficit = -ctx.cash
        logger.warning(
            "FORCE DE-LEVER: cash=$%.2f, deficit=$%.2f — auto-selling to restore "
            "cash ≥ 0 (allow_margin=False)", ctx.cash, deficit,
        )
        # A resting entry BUY would deepen the very deficit this sweep exists
        # to clear the moment it fills — cancel entries before selling.
        try:
            self.broker.cancel_open_entry_orders()
        except Exception as exc:  # noqa: BLE001
            logger.warning("force de-lever: entry-order cancel failed: %s", exc)

        sellable = [p for p in ctx.positions if p.qty > 0]
        if not sellable:
            logger.error(
                "FORCE DE-LEVER: cash=$%.2f deficit=$%.2f but no long positions "
                "to sell — account stuck on margin until cash arrives externally",
                ctx.cash, deficit,
            )
            # Loud, not silent: no long to sell IS a residual miss (the sibling
            # of the gross-ceiling path's incompleteness alert) — the account
            # stays on margin, so page the owner rather than only logging.
            self._alert_owner_force_delever_incomplete(
                deficit=deficit, projected_proceeds=0.0, failed_symbols=[],
            )
            return []

        # Two-tier ordering: prefer longs over inverse-ETF hedges before
        # falling back to the loss-magnitude rule.
        #
        # Inverse ETFs (SH / SDS / PSQ / SQQQ) have `_effective_multiplier < 0`
        # — they HEDGE long exposure. A "biggest-loser-first" pass that
        # ignores direction can pick a hedge in any market where the long
        # book is profitable (the hedge tends to lose precisely when the
        # rest is winning). Selling the hedge first leaves the remaining
        # longs naked, AMPLIFYING directional exposure. This sweep's job is
        # to clear the measured cash deficit, and a hedge raises that cash
        # just as well as a long does — so the ONLY reason to order the two
        # is to avoid stripping directional protection off the book while
        # doing it. Cash-flow-wise both raise cash equally; hedges sort last
        # so the deficit is cleared without gratuitously un-hedging the book.
        #
        # Tier key (lower = sells earlier):
        #  -1 → cash-sweep vehicle (parked T-bills ARE cash — always the
        #       first thing to liquidate; selling anything else first would
        #       realize market risk to cover a deficit that parked cash
        #       can cover for free)
        #   0 → long (effective_mul > 0)
        #   1 → inverse-ETF hedge (effective_mul < 0)
        # Within each tier, classic biggest-loser-first ordering:
        #   - most negative unrealized_pnl
        #   - then larger market_value (clear deficit in fewer orders)
        #   - then symbol alphabetical (deterministic across runs)
        from src.risk.rules import _effective_multiplier
        sweeper = self._sweeper()
        sweep_symbol = sweeper.symbol if sweeper is not None else None
        def _tier(p):
            if sweep_symbol is not None and p.symbol == sweep_symbol:
                return -1
            return 0 if _effective_multiplier(p.symbol) > 0 else 1
        targets = sorted(
            sellable,
            key=lambda p: (_tier(p), p.unrealized_pnl, -p.market_value, p.symbol),
        )

        orders: list[dict] = []
        projected_proceeds = 0.0
        failed_symbols: list[str] = []
        for p in targets:
            if projected_proceeds >= deficit:
                break
            is_sweep = sweep_symbol is not None and p.symbol == sweep_symbol
            # Price the must-fill exit off the LIVE quote at submit time: a
            # marketable limit AT the live bid, or a MARKET order (limit=None)
            # when no live quote is available. See `_live_delever_price`. The
            # reference is the live mid so the broker's fat-finger guard passes
            # a legitimately gapped fill; it falls back to the mark only when
            # there is no quote, in which case the order is a MARKET order the
            # guard skips anyway.
            #
            # Priced BEFORE the quantity is chosen (board item 182): the
            # partial sweep sale below sizes itself off this limit, and it
            # cannot do that if the limit is only established afterwards.
            sell_limit, quote_ref = self._live_delever_price(p.symbol, "sell")
            exec_ref = quote_ref if quote_ref is not None else p.current_price
            # The price floor each share of a partial sweep sale is
            # guaranteed to raise. A SELL limit fills AT OR ABOVE its limit
            # or it does not fill, so the live limit IS that floor, and a
            # partial sale can be sized off it with no cushion at all.
            #
            # WITH NO LIVE QUOTE THERE IS NO FLOOR, so there is no partial
            # size to justify and the loop sells the whole position, exactly
            # as every non-sweep de-lever target already does. An earlier
            # draft of this change sized that branch off the mark less
            # `AlpacaBroker.STOP_LIMIT_BUFFER_PCT` and called it "an existing
            # number, not a new one". It was neither safe nor a no-op:
            # dividing by 0.97 is a 3.09% pad on the same possibly-stale
            # mark, i.e. LARGER than the flat 2% pad the change claimed to be
            # removing, and 3% is a stop-limit through-buffer picked
            # (`status: arbitrary`) for a different job. Borrowing a constant
            # at the wrong tightness for a new job is not sourcing it.
            sizing_price = sell_limit if (
                sell_limit is not None and sell_limit > 0
            ) else None
            if is_sweep and sizing_price is not None:
                # audit round 2: only unpark what the deficit needs —
                # full-liquidating an $80k T-bill balance for a $200 deficit
                # forced a full re-park at the session bookend, a pointless
                # round-trip. Real positions keep whole-position sells
                # (partial de-levers of losers re-review next session).
                #
                # THE SHARE COUNT IS COMPUTED, NOT PADDED (board item 182,
                # 2026-09-30). This used to divide the remaining deficit by
                # the possibly-stale `current_price` and then multiply by a
                # flat 1.02 — a 2% guess at how far the fill would land under
                # the mark, chosen by nobody and read off nothing. The guess
                # is unnecessary on THIS branch, because the order this loop
                # is about to place already carries its own worst case: the
                # live SELL limit it will rest at. Dividing the remaining
                # deficit by that floor gives the smallest share count that
                # clears it, and `ceil` supplies the whole-share rounding.
                # This extends to the QUANTITY exactly what
                # `_live_delever_price` already states for the PRICE.
                #
                # WHAT THIS DOES NOT COVER: the limit bounds the PRICE of the
                # shares that fill, not HOW MANY fill. The limit is placed at
                # the live bid, whose displayed size is finite, and Alpaca
                # documents `partially_filled` as an order status, so a short
                # fill is a real state on this path. Nothing below reads a
                # filled quantity — `_submit_protected_sell` returns on
                # broker ACCEPTANCE — so a partial fill still leaves a
                # residual deficit this loop will not see. That gap predates
                # this change and is reported, not fixed, here.
                import math as _math
                remaining = deficit - projected_proceeds
                qty = min(
                    float(_math.ceil(remaining / sizing_price)), p.qty,
                )
                if qty >= p.qty:
                    qty = self._full_sell_qty(p.qty)
            else:
                qty = self._full_sell_qty(p.qty)
            if qty is None or qty <= 0:
                continue
            limit_str = f"${sell_limit:.2f}" if sell_limit is not None else "market"
            # The sweep vehicle's exit is recorded as SWEEP_SELL, not
            # FORCE_DELEVER (audit round 2): action names are the sweep's
            # ledger-isolation mechanism — a FORCE_DELEVER row on SGOV leaks
            # into evening sell-grading and calibration as if it were a
            # trading decision.
            sale = self._submit_protected_sell(
                symbol=p.symbol, qty=qty, limit_price=sell_limit,
                reference_price=exec_ref, position_qty_before_sell=p.qty,
                label="SWEEP_SELL" if is_sweep else "FORCE_DELEVER",
                escalate_to_market_on_reject=True,
            )
            if sale is None:
                # Even the MARKET escalation could not place (stop-clear failed,
                # or the market order itself was rejected). This name is a real
                # residual miss — record it so the sweep is reported incomplete
                # below rather than silently skipped.
                failed_symbols.append(p.symbol)
                continue
            order, prot = sale
            try:
                # Count the proceeds BEFORE the ledger write: the SELL is
                # already live at the broker, so its cash is coming whether or
                # not we manage to record it. Booking it only after a
                # successful insert_trade meant a DB hiccup left
                # projected_proceeds short, and the loop force-sold the NEXT
                # position to cover a deficit the in-flight order had already
                # covered — liquidating real holdings over a bookkeeping
                # failure (2026-07-16 audit).
                # Conservative proceeds estimate for the break-early guard:
                # mark × 0.97, i.e. assume the marketable fill lands up to 3%
                # below the mark (the same must-fill slippage the desk's
                # STOP_LIMIT_BUFFER_PCT budgets for a gapping exit). Under-
                # counting proceeds is the safe error here — it never stops the
                # sweep one position too early and leaves a residual deficit.
                #
                # ON THE SHARES ACTUALLY SOLD (2026-09-30). `market_value`
                # is the position's FULL value, and every branch here sells
                # the whole position EXCEPT the sweep slice above — which
                # credited the whole park anyway. The sweep vehicle sorts
                # FIRST and the loop breaks at `projected_proceeds >=
                # deficit`, and the completeness alert at the bottom reads
                # the same figure, so an $80k park sold down by $500
                # credited ~$78k and a short sale produced neither a second
                # sale NOR an owner alert.
                #
                # The slice is credited at the limit it was SIZED off, which
                # is the same arithmetic floor and keeps the two consistent:
                # counting a correctly-sized slice at a lower figure than it
                # was sized to raise would make the loop believe it fell
                # short and sell the next position on top of it.
                if qty < p.qty:
                    projected_proceeds += float(qty) * float(sizing_price)
                else:
                    projected_proceeds += p.market_value * 0.97
                orders.append(order)
                logger.info(
                    "FORCE DE-LEVER SELL %s qty=%s @ limit=%s "
                    "(unrealized_pnl=$%.2f, mkt_value=$%.2f)",
                    p.symbol, self._format_qty(qty), limit_str,
                    p.unrealized_pnl, p.market_value,
                )
                self.db.insert_trade(
                    symbol=p.symbol,
                    action="SWEEP_SELL" if is_sweep else "FORCE_DELEVER",
                    qty=qty,
                    price=p.current_price,
                    reasoning=(
                        f"cash-only auto de-lever: session opened with "
                        f"cash=${ctx.cash:.2f} (deficit ${deficit:.2f}); "
                        f"biggest-loser-first sweep"
                    ),
                    run_id=ctx.run_id,
                    broker_order_id=order.get("id"),
                    fill_status="submitted",
                )
            except Exception as e:
                logger.error(
                    "FORCE DE-LEVER SELL %s failed: %s — the order may still be "
                    "live at the broker; its proceeds are already counted so the "
                    "sweep will not over-liquidate", p.symbol, e,
                )
            # Rebuild THIS symbol's stop coverage on its actual fill before
            # the loop cancels the next symbol's stops (docs/WORK.md item
            # 111). Finalizing the whole batch after the loop left every
            # earlier symbol with no protective stop while later symbols were
            # being cancelled, submitted and waited on. Which positions are
            # sold, and how much, is unchanged: `projected_proceeds` above is
            # booked at submit time, never from the fill.
            self._finalize_pending_protections(
                [prot], context="FORCE DE-LEVER",
            )

        # Refresh ctx so downstream stages see post-sell truth.
        try:
            account = self.broker.get_account()
            ctx.positions = self.broker.get_positions()
            ctx.cash = account["cash"]
            ctx.deployable_cash = self._compute_deployable_cash(ctx.cash, ctx.positions)
            ctx.total_value = account["portfolio_value"]
            ctx.last_equity = account.get("last_equity", ctx.total_value)
            logger.info(
                "FORCE DE-LEVER complete: %d orders, post-refresh cash=$%.2f, "
                "positions=%d",
                len(orders), ctx.cash, len(ctx.positions),
            )
        except Exception as e:
            logger.error("FORCE DE-LEVER: broker refresh failed: %s", e)

        # Report parity with the gross-ceiling path's
        # `_alert_owner_delever_incomplete`: if the sweep could not raise
        # enough to cover the deficit (projected proceeds fell short) or a name
        # could not be sold even at market, the account is still on margin and
        # the owner must hear it — never a silent skip.
        if failed_symbols or projected_proceeds < deficit:
            self._alert_owner_force_delever_incomplete(
                deficit=deficit, projected_proceeds=projected_proceeds,
                failed_symbols=failed_symbols,
            )

        return orders

    def _alert_owner_force_delever_incomplete(
        self, *, deficit: float, projected_proceeds: float,
        failed_symbols: list[str],
    ) -> None:
        """Page the owner when the cash-only forced de-lever could NOT clear
        the margin deficit — the sibling of `_alert_owner_delever_incomplete`
        for the `allow_margin=False` sweep path. Never raises.

        Without this a genuinely unfillable name here (a market order the broker
        rejected, a stop-clear that failed, or simply not enough sellable value)
        would leave the account on margin with only a log line nobody reads. It
        is reporting-only: it changes no order, no sizing and no sequencing.
        """
        try:
            from src import notifier as _notifier

            shortfall = max(0.0, deficit - projected_proceeds)
            names = ", ".join(sorted(failed_symbols)) if failed_symbols else "—"
            msg = (
                f"FORCE DE-LEVER INCOMPLETE: the cash-only sweep could not "
                f"clear the ${deficit:,.2f} margin deficit (still ~"
                f"${shortfall:,.2f} short). Names that could not be sold even "
                f"at a MARKET order: {names}. The account remains on margin "
                f"until this is resolved."
            )
            logger.error(msg)
            _notifier.send_owner_alert(msg)
        except Exception as exc:  # noqa: BLE001
            logger.error("force de-lever incomplete owner alert failed: %s", exc)

    # --- Spec §11.2 — the gross-exposure ceiling and the de-levering ladder

    def _resolve_gross_ceiling(self, ctx: RunContext):
        """Resolve this session's gross-exposure ceiling from ACCOUNT STATE.

        Nothing the Portfolio Manager produced is an input, and this returns
        a correct ceiling on a run where the PM returned nothing at all. That
        is deliberate: a blank/truncated model response is a measured failure
        mode, and a ceiling that needed a parseable book would leave the desk
        fully levered at exactly the moment it should be shedding exposure.

        Also records the state on `ctx.leverage` for the morning alert and
        the dashboard, including distance-to-forced-liquidation — which
        nothing in this codebase watched before §11.2.
        """
        risk_cfg = getattr(getattr(self, "config", None), "risk", None)
        base_x = _risk_number(getattr(risk_cfg, "max_gross_exposure_x", None), 2.0)
        maintenance_pct = _risk_number(
            getattr(risk_cfg, "maintenance_margin_pct", None), 25.0,
        )
        # Guard 2 (2026-09-02 operational safety guard): a non-finite
        # CURRENT equity read must not fall through to
        # `peak_to_trough_pct`'s "unmeasurable" branch, which
        # `resolve_gross_ceiling` resolves to the STANDING (loosest) cap.
        # That branch is correct for a genuinely fresh account with no
        # equity curve yet (see `resolve_gross_ceiling`'s docstring). Alpaca
        # has been observed to return NaN portfolio_value during market-open
        # glitches, and
        # holding the loosest cap on exactly that kind of broken-snapshot
        # day is the failure this guard closes: halting new risk (the
        # ladder's own floor rung) is safer than assuming zero drawdown.
        #
        # CORRECTION 2026-09-18. This comment used to assert, "by
        # inspection", that `peak_to_trough_pct` returns None ONLY when
        # today's own reading is unusable — that an empty history always
        # produced a real 0.0. That was accurate about the code and wrong
        # about safety: it meant a wiped `daily_pnl` table read as a book at
        # record highs and silently held the loosest cap. `peak_to_trough_pct`
        # now returns None when there is no usable PRIOR reading (its Guard
        # 3), so "unknown" is reachable in production from a lost equity
        # curve as well as from a bad read, and `resolve_gross_ceiling` now
        # alerts the owner in that state instead of staying silent. The two
        # paths still differ on purpose, and the difference is the point:
        # a BAD READ (below) is a book of unknown depth that already exists,
        # so it drops to the floor rung; an ABSENT CURVE may be a genuinely
        # fresh account that never fell, so it holds the standing cap and
        # trims nothing. Both now alert.
        total_value = ctx.total_value
        bad_equity_read = (
            isinstance(total_value, bool)
            or not isinstance(total_value, (int, float))
            or not math.isfinite(float(total_value))
        )
        if bad_equity_read:
            floor_x = GROSS_LADDER[-1][1]
            if base_x > 0:
                floor_x = min(base_x, floor_x)
            logger.warning(
                "§11.2: current equity read is non-finite (%r) — forcing "
                "the gross-exposure ceiling to its floor rung (%.1fx) and "
                "alerting the owner instead of assuming zero drawdown.",
                total_value, floor_x,
            )
            ceiling = GrossCeiling(
                ceiling_x=floor_x, base_x=base_x, drawdown_pct=None,
                alert_owner=True, rung="bad_read",
                reason=(
                    f"Current equity read came back non-finite "
                    f"({total_value!r}) — a documented Alpaca market-open "
                    f"glitch, not a fresh account. The book's drawdown "
                    f"cannot be verified, so gross exposure is held to the "
                    f"floor rung ({floor_x:.1f}x) until a valid read "
                    f"arrives."
                ),
            )
        else:
            drawdown_pct = None
            performance = ctx.recent_performance or {}
            if "peak_to_trough_pct" in performance:
                drawdown_pct = performance.get("peak_to_trough_pct")
            else:
                # The preamble runs before DecisionStage populates
                # `recent_performance`, so read the equity curve directly. One
                # cheap local DB read; a failure degrades to "unknown drawdown",
                # which resolves to the standing cap and trims nothing — never
                # to a wrong number that reads as "no drawdown".
                try:
                    rows = self.db.get_daily_pnl(limit=252)
                    drawdown_pct = peak_to_trough_pct(
                        [r.get("total_value") for r in (rows or [])], ctx.total_value,
                    )
                except Exception as e:  # noqa: BLE001
                    logger.warning(
                        "§11.2: could not read the equity curve for the drawdown "
                        "ladder — holding the standing %.1fx ceiling and trimming "
                        "nothing: %s", base_x, e,
                    )
            ceiling = resolve_gross_ceiling(drawdown_pct, base_x=base_x)
        gross = gross_exposure(
            ctx.positions, cash_park_symbol=self._sweep_symbol(),
        )
        equity = ctx.total_value if ctx.total_value else 0.0
        ctx.leverage = {
            "gross_usd": gross,
            "gross_x": (gross / equity) if equity > 0 else None,
            "ceiling_x": ceiling.ceiling_x,
            "base_ceiling_x": ceiling.base_x,
            "drawdown_pct": ceiling.drawdown_pct,
            "rung": ceiling.rung,
            "alert_owner": ceiling.alert_owner,
            # The level the alert fired at, carried so the owner message
            # can state it instead of restating a literal that goes stale
            # the day the sourced threshold moves (board item 182).
            "alert_pct": GROSS_LADDER_ALERT_PCT,
            "reason": ceiling.reason,
            "distance_to_forced_liquidation_pct":
                distance_to_forced_liquidation_pct(
                    gross, equity, maintenance_margin_pct=maintenance_pct,
                ),
        }
        return ceiling

    def _enforce_gross_ceiling(
        self,
        ctx: RunContext,
        *,
        floor_only: bool = False,
        conviction_rank: dict[str, tuple[int, int]] | None = None,
        planned_exits: list | None = None,
    ) -> list[dict]:
        """De-lever the HELD book when it is over the §11.2 gross ceiling.

        Runs in the session preamble, beside `_force_delever`, and therefore
        BEFORE any agent is called. That placement is the requirement, not a
        convenience: if any part of the ladder depended on the Portfolio
        Manager returning a usable book, a truncated model response would
        mean the desk stays levered exactly when it should be shedding
        exposure. Nothing here reads a PM decision.

        The ordering rule still holds and is enforced inside
        `apply_gross_ceiling`: the only decisions this call passes are
        already-working EXITS (`_open_exit_relief`), never an entry, so there
        is no new exposure to block and trims are emitted only because the
        held book alone exceeds the ceiling. New exposure proposed later in the
        same session is blocked by the sizing gate (the constructor) and the
        execution gate (`max_gross_exposure`), never by selling something the
        desk already owns to make room.

        `floor_only` (item 112) scopes this to a live-price MARGIN FLOOR: it
        fires only when the book has eroded the margin buffer the ratified
        BASE leverage cap is engineered to preserve — i.e. it is levered
        beyond the base cap, dangerously close to a broker forced
        liquidation. The morning lane passes `floor_only=True` because its
        ordinary §11.2 ceiling breach is de-levered LATER, after the PM has
        run, by `_enforce_gross_ceiling_by_conviction` — which cuts the
        WEAKEST-by-conviction names first using this session's fresh per-seat
        read, instead of the stale-stance biggest-loser cut. The margin floor
        DEFERRAL IS NOT A WAIVER. `floor_only` only says "not yet": it sets
        `ctx.gross_ceiling_deferred`, and the morning body's `finally`
        discharges that debt with a FULL ordinary-ceiling pass on every lane
        the conviction pass did not reach — the nine PM-less early returns,
        the resume lane and an exception exit included. Midday, close and
        intraday keep `floor_only=False` (default): they have no fresh read,
        so they de-lever the full §11.2 ceiling in the preamble with the
        unchanged biggest-loser ordering.

        `conviction_rank` (item 112) is the weakest-conviction-first cut
        order built by `_conviction_cut_order`. This is the ONE place that
        calls `apply_gross_ceiling` with trims enabled — the single-owner
        invariant `test_trimming_the_held_book_has_exactly_one_owner` pins —
        so the conviction pass delegates here rather than building a second
        de-lever.

        Returns the submitted orders (empty when the book is under its
        ceiling, which is the ordinary case). `ctx` is refreshed from the
        broker after fills so downstream stages see truth.
        """
        risk_cfg = getattr(getattr(self, "config", None), "risk", None)
        if risk_cfg is None:
            # Tests that bypass __init__ via TradingPipeline.__new__.
            return []
        ceiling = self._resolve_gross_ceiling(ctx)
        # ASYNC-FILL RACE. Any exit this process submitted that has not
        # reached a terminal broker state is still shedding exposure the
        # refreshed book has not caught up with. Net those open quantities
        # out of the measurement (STEP 1 of `apply_gross_ceiling` subtracts
        # planned exits before it judges anything) so this pass cuts the TRUE
        # residual rather than the same exposure twice.
        open_exits, unmeasurable_exits = self._open_exit_relief(ctx.positions)
        if unmeasurable_exits:
            # An in-flight exit whose state cannot be read is the one case
            # where netting nothing would double the shed, and there is no
            # honest number to net. Leave the debt OWED — never silently
            # paid — so the next pass or the next session re-measures it.
            logger.warning(
                "Gross-exposure ceiling: an exit order from this run cannot "
                "be read — refusing to re-cut a book that may already be "
                "shedding, and leaving the ceiling recorded as still owed",
            )
            ctx.gross_ceiling_deferred = True
            return []
        if floor_only and not self._is_margin_floor_breach(ctx):
            # No genuine liquidation-proximity breach: leave an ordinary
            # §11.2 ceiling breach to the post-decision conviction pass — or,
            # if the run never reaches it, to the `finally` discharge.
            ctx.gross_ceiling_deferred = True
            return []
        if floor_only:
            # The floor trims to the ordinary ceiling, but a shortfall (an
            # unfillable name, a min-order clamp) can leave the book over it.
            # Keep the debt open; the discharge re-measures and no-ops when
            # the book did come under.
            ctx.gross_ceiling_deferred = True
        min_order_usd = _risk_number(
            getattr(getattr(self.config, "cash_sweep", None), "min_order_usd", None),
            500.0,
        )
        # PLANNED EXITS ALREADY DECIDED THIS SESSION. The preamble pass runs
        # before any agent, so there are none. The post-decision conviction
        # pass runs AFTER the risk stage, when the desk has already approved
        # SELL/COVER decisions the execution stage is about to submit — and
        # measuring the held book as though those names were staying counts
        # exposure that is leaving, overstates the breach, and reaches one
        # extra name. Under the weakest-first cut that extra name is by
        # construction among the STRONGEST convictions still standing, so
        # the overstatement sells exactly what the ordering exists to
        # protect. STEP 1 of `apply_gross_ceiling` subtracts these before it
        # judges anything, and its per-symbol `max()` means an exit that is
        # both decided and already working is counted once, not twice.
        # Exits ONLY: passing the session's BUYs here would let STEP 2
        # re-cut allocations the risk stage has already ruled on.
        decisions = list(open_exits) + [
            d for d in (planned_exits or [])
            if getattr(d, "action", None) in ("SELL", "COVER")
        ]
        outcome = apply_gross_ceiling(
            decisions, ctx.positions, ctx.total_value, ceiling,
            cash_park_symbol=self._sweep_symbol(),
            min_order_usd=min_order_usd,
            conviction_rank=conviction_rank,
        )
        orders = self._submit_gross_ceiling_trims(ctx, ceiling, outcome)
        if not floor_only:
            # The FULL ordinary ceiling has now been measured and acted on.
            # This is the only thing that clears the debt — a pass that
            # returned early above never marks it paid.
            ctx.gross_ceiling_deferred = False
        return orders

    def _is_margin_floor_breach(self, ctx: RunContext) -> bool:
        """Is the book levered beyond the margin buffer the BASE cap preserves?

        The ratified base leverage cap (`RiskConfig.max_gross_exposure_x`) is
        chosen to keep a fixed distance to a broker forced liquidation
        (`distance_to_forced_liquidation_pct`: 33.3% at the 2.0x/25% default).
        The floor fires when the book's ACTUAL distance is worse than that
        engineered distance — a true margin-proximity breach, defined off two
        already-ratified numbers (the base cap and the maintenance margin), so
        no new threshold is introduced. Returns False whenever either figure
        is unmeasurable, so an unreadable snapshot never provokes an emergency
        trim on the morning lane (the midday/close full-ceiling pass, which is
        not floor-scoped, still fails closed on its own terms).
        """
        leverage = ctx.leverage or {}
        distance = leverage.get("distance_to_forced_liquidation_pct")
        base_x = leverage.get("base_ceiling_x")
        equity = ctx.total_value if ctx.total_value else 0.0
        if not isinstance(distance, (int, float)) or not isinstance(base_x, (int, float)):
            return False
        if equity <= 0:
            return False
        maintenance_pct = _risk_number(
            getattr(getattr(self, "config", None), "risk", None)
            and getattr(self.config.risk, "maintenance_margin_pct", None),
            25.0,
        )
        base_distance = distance_to_forced_liquidation_pct(
            base_x * equity, equity, maintenance_margin_pct=maintenance_pct,
        )
        if not isinstance(base_distance, (int, float)):
            return False
        return distance < base_distance

    def _conviction_cut_order(self, ctx: RunContext) -> dict[str, tuple[int, int]]:
        """Weakest-conviction-FIRST sort key per held symbol (item 112).

        WHAT THIS RANKS, AND WHAT IT DELIBERATELY DOES NOT. It ranks SEAT
        CONVICTION ABOUT THE HOLDING. It must not rank ENTRY ELIGIBILITY,
        and an earlier version of this build did: it read
        `PortfolioManagerAgent.last_candidate_ranking`, which is the list of
        survivors AFTER `candidate_eligibility` (R2 neutral rating, R3 not
        BUY-eligible today, R6 constructor refusal) and AFTER the conviction
        bar. Every one of those is a reason not to BUY a name today. None is
        a reason to SELL it first. Worse, the conviction bar's STAY side is
        opposition-only by owner ruling (2026-09-25): a held name that fails
        the ENTRY bar on soft grounds — no technical read, a neutral read,
        support faded — is dropped from the survivors "with no cull reason;
        it earns its right to STAY". Ordering a cut by that list sold exactly
        the names the ruling protects. So this reads the RAW seat verdicts
        (`ctx.seat_verdicts`, everything the seats actually said, before any
        admission gate) and this session's evidence registry.

        Key 1 — the bucket. THREE states, not two, so "four seats argued
        against this" and "nobody looked at this" can never collapse into one
        decision to liquidate (the reason the graded score was retired at all,
        board item 66):

          0 OPPOSED      at least one seat argues AGAINST the side held. The
                         desk has a live objection, so this is the one state
                         that is itself a reason to shed — and it is the exact
                         test the owner's 2026-09-25 STAY ruling uses to cull
                         a holding ("opposition-only").
          1 NO COVERAGE  no seat read this name for this side at all. Cut
                         ahead of a live thesis, because there is none to
                         protect — but NEVER first, because a name the desk
                         could not read is not a name the desk decided
                         against, and refusing to BUY is not a decision to
                         SELL. A bar-fetch outage must not author a
                         liquidation order.
          2 SUPPORTED    at least one seat argues FOR the side held and none
                         against. Cut last.

        Counted with `count_opposing_sources` / `count_aligned_sources`
        against the side the position actually carries, with
        `ctx.evidence_stale_sources` excluded as `ignored_sources` exactly as
        every other caller does — which today means an over-age EARNINGS
        stance and nothing else, that being §9.4's only freshness rule.

        Key 2 — inside a bucket, the desk's own `rank_verdicts` ordering over
        those same raw verdicts, best first, counted only when the ranked
        direction MATCHES the side held (a top-ranked BEARISH read is not
        conviction in a long). Unranked scores 0.

        Sorted ASCENDING, so the cut starts with the names the desk has an
        argument against and reaches its live theses last.

        MISSING TECHNICAL READS ARE MADE VISIBLE, NOT FILLED IN. The
        technical seat's symbol set (`_run_tech`) is today's admitted names
        plus the configured universe; unlike News it is NOT passed the held
        book, and a symbol whose bars fail to fetch is skipped. A held name
        can therefore reach here with no technical verdict through no fault
        of its own. This build does not inject held symbols into the
        technical seat — that widens the paid research scope and is a
        separate, costed decision — it instead gives absence its own bucket
        and LOGS every uncovered holding by name, so the gap is reportable
        instead of silently scoring zero and sorting first.
        """
        registry = getattr(ctx, "evidence_registry", None) or {}
        stale = getattr(ctx, "evidence_stale_sources", None) or {}
        non_corroborating = (
            getattr(ctx, "evidence_non_corroborating_sources", None) or {}
        )
        verdicts = list(getattr(ctx, "seat_verdicts", None) or [])
        strength_of: dict[tuple[str, str], int] = {}
        if verdicts:
            try:
                ranked = rank_verdicts(verdicts)
            except Exception as exc:  # noqa: BLE001
                logger.warning("conviction cut order: ranking failed: %s", exc)
                ranked = []
            total = len(ranked)
            for index, candidate in enumerate(ranked):
                symbol = str(getattr(candidate, "symbol", "") or "").strip().upper()
                direction = str(getattr(candidate, "direction", "") or "").strip().lower()
                if symbol and direction:
                    # Best-first -> biggest number is the strongest conviction.
                    strength_of[(symbol, direction)] = total - index
        order: dict[str, tuple[int, int]] = {}
        uncovered: list[str] = []
        for position in ctx.positions or []:
            symbol = str(getattr(position, "symbol", "") or "").strip().upper()
            if not symbol:
                continue
            side = position_side(position)
            wanted = "bullish" if side == SECTOR_SIDE_LONG else "bearish"
            sources = registry.get(symbol) or {}
            ignored = stale.get(symbol)
            opposed = count_opposing_sources(
                symbol, sources, side, ignored_sources=ignored,
            )
            # ONE-SIDED (item 109, owner ruling 2026-09-25 "macro weighted,
            # never solo"): a macro stance BROADCAST onto a name whose sector
            # the macro read never mentioned may not be what saves that name
            # from the cut — that is macro protecting a holding on its own.
            # Its dissent above is untouched, so the removal can only ever
            # move a name EARLIER in the cut, never later.
            aligned = count_aligned_sources(
                symbol, sources, side,
                ignored_sources=(ignored or frozenset())
                | (non_corroborating.get(symbol) or frozenset()),
            )
            if opposed > 0:
                bucket = 0      # OPPOSED — cut first
            elif aligned > 0:
                bucket = 2      # SUPPORTED — cut last
            else:
                bucket = 1      # NO COVERAGE — between the two, never first
                uncovered.append(symbol)
            order[symbol] = (bucket, strength_of.get((symbol, wanted), 0))
        if uncovered:
            logger.warning(
                "Conviction cut order: no seat read %s for the side held — "
                "ranked as UNREAD, never as opposed. A holding the desk could "
                "not read is not a holding it decided against.",
                ", ".join(sorted(uncovered)),
            )
        return order

    def _enforce_gross_ceiling_by_conviction(self, ctx: RunContext) -> list[dict]:
        """Morning post-decision §11.2 de-lever, WEAKEST-conviction cut first.

        Runs after the risk stage and before execution, on the morning lane
        only. It ranks the held book with `_conviction_cut_order` — this
        session's fresh per-seat read as a yes/no, then the desk's own
        `rank_verdicts` candidate ordering — so a conviction-dead winner is
        sold before an intact-thesis loser.

        Absent registry (every PM-less lane) means nothing fresh was read, so
        it returns [] and the `finally` discharge enforces the ordinary
        ceiling with the unchanged biggest-loser ordering. It DELEGATES to
        `_enforce_gross_ceiling`, the single owner of held-book trimming.

        It does NOT touch `ctx.gross_ceiling_deferred`. Only the delegate
        clears that, and only after it has actually measured and acted on
        the full ordinary ceiling — an earlier version cleared it here
        unconditionally, including when the delegate had bailed, which
        marked the debt paid without anything being enforced.
        """
        risk_cfg = getattr(getattr(self, "config", None), "risk", None)
        if risk_cfg is None:
            return []
        if not getattr(ctx, "evidence_registry", None):
            return []
        decision = getattr(ctx, "portfolio_decision", None)
        planned_exits = [
            d for d in (getattr(decision, "decisions", None) or [])
            if getattr(d, "action", None) in ("SELL", "COVER")
            and float(getattr(d, "allocation_pct", 0.0) or 0.0) > 0
        ]
        return self._enforce_gross_ceiling(
            ctx,
            conviction_rank=self._conviction_cut_order(ctx),
            planned_exits=planned_exits,
        )

    def _discharge_deferred_gross_ceiling(self, ctx: RunContext) -> None:
        """Pay the `floor_only` debt on any lane the conviction pass missed.

        Item 112 defect 2: scoping the morning preamble to the margin floor
        left the ORDINARY §11.2 ceiling unenforced on every lane that never
        reaches the Portfolio Manager — the nine early returns, the resume
        lane, and an exception exit. That was a regression against the
        behaviour where the preamble always enforced the full ceiling.

        Called from the morning body's `finally`, which is the ONE place
        every exit path passes through, so a lane added later inherits the
        enforcement instead of silently losing it. A no-op when the
        conviction pass already discharged the debt, and it re-measures, so
        a book that came under its ceiling meanwhile sheds nothing. Never
        raises: it runs while another exception may be propagating.

        It does NOT clear the flag itself. The delegate clears it only when
        the full ordinary ceiling was actually measured and acted on, so a
        discharge that bails — or raises — leaves the debt visibly OWED
        rather than silently paid.
        """
        if not getattr(ctx, "gross_ceiling_deferred", False):
            return
        try:
            self._enforce_gross_ceiling(ctx)
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "Deferred gross-ceiling enforcement failed: %s — the book may "
                "remain over its §11.2 ceiling until the next session", exc,
            )

    def _submit_gross_ceiling_trims(
        self, ctx: RunContext, ceiling, outcome,
    ) -> list[dict]:
        """Submit, protect, refresh and record a resolved gross-ceiling trim.

        The shared body of both §11.2 de-lever paths — the preamble margin
        floor (`_enforce_gross_ceiling`) and the morning post-decision
        conviction pass (`_enforce_gross_ceiling_by_conviction`). ONE
        implementation of the live-price submit, per-symbol stop re-coverage,
        broker refresh, incomplete-de-lever alert and durable shortfall
        record, so the two paths can never diverge on how a trim is placed —
        only on which names `apply_gross_ceiling` chose and in what order.
        """
        if not outcome.trims:
            return []
        logger.warning(
            "GROSS-EXPOSURE DE-LEVER: the book owns $%.0f against a $%.0f "
            "ceiling (%.2fx equity). %s",
            outcome.held_gross, outcome.ceiling_usd, ceiling.ceiling_x,
            ceiling.reason,
        )
        # A resting entry order would deepen the breach the moment it fills.
        try:
            self.broker.cancel_open_entry_orders()
        except Exception as exc:  # noqa: BLE001
            logger.warning("gross-exposure de-lever: entry-order cancel failed: %s", exc)

        positions_by_symbol = {p.symbol: p for p in ctx.positions}
        # The equity the ceiling was measured against, kept for the item-112
        # shortfall record (ctx.total_value is overwritten by the refresh).
        equity_before = ctx.total_value
        orders: list[dict] = []
        pending_protections: list[dict] = []
        for trim in outcome.trims:
            position = positions_by_symbol.get(trim.symbol)
            if position is None or not position.current_price:
                continue
            held_qty = abs(position.qty)
            qty = held_qty * (trim.allocation_pct / 100.0)
            if float(position.qty).is_integer():
                qty = float(int(qty))
                if qty <= 0:
                    qty = 1.0
            if qty >= held_qty:
                qty = self._full_sell_qty(held_qty)
            if qty is None or qty <= 0:
                continue
            # Price the trim off the LIVE quote at submit time, not a fixed %
            # of a possibly-stale mark. This is emergency risk reduction: the
            # book already exceeds its gross ceiling and the whole point of the
            # trim is to shed that exposure NOW, regardless of how far a name
            # has gapped. A fixed % of a stale price rests ABOVE the falling
            # market on a gap day — precisely the conditions that trigger the
            # ladder — leaving the book OVER its ceiling exactly when it must
            # come down (docs/WORK.md item 118).
            #
            # `_live_delever_price` reads the CURRENT bid/ask and returns a
            # MARKETABLE limit that crosses it — a SELL at the live bid, a
            # COVER (BUY) at the live ask — so the order fills at ANY gap size,
            # because the gap is already IN the quote. When no live quote is
            # available it returns a limit of None, and the order becomes a
            # MARKET order: the guaranteed fill, and the correct fallback when
            # there is no live price to cross. No new % constant is introduced
            # on either branch, and the must-fill requirement (fill NOW over a
            # few bps of price) is met without resting on a stale reference.
            # The reference passed to the broker is the live mid, so the
            # fat-finger guard passes a legitimately gapped fill instead of
            # rejecting it for deviating from yesterday's mark.
            #
            # `FORCE_DELEVER` is already an EITHER-SIDE exit action in the
            # ledger (`_EITHER_SIDE_EXIT_ACTIONS`, src/storage/db.py) — "a
            # deterministic de-lever fires against whatever position is
            # open" — so the same label correctly retires a short chain
            # without inventing a second action name.
            is_cover = trim.action == "COVER"
            side = "buy" if is_cover else "sell"
            limit_price, quote_ref = self._live_delever_price(trim.symbol, side)
            exec_ref = (
                quote_ref if quote_ref is not None else position.current_price
            )
            sale = self._submit_protected_sell(
                symbol=trim.symbol, qty=qty, limit_price=limit_price,
                reference_price=exec_ref,
                position_qty_before_sell=abs(position.qty),
                label="FORCE_DELEVER",
                side=side,
                escalate_to_market_on_reject=True,
            )
            if sale is None:
                continue
            order, protection = sale
            pending_protections.append(protection)
            orders.append(order)
            logger.info(
                "GROSS-EXPOSURE DE-LEVER %s %s qty=%s @ limit=%s (%s)",
                trim.action, trim.symbol, self._format_qty(qty),
                (f"${limit_price:.2f}" if limit_price is not None else "market"),
                ceiling.reason,
            )
            try:
                self.db.insert_trade(
                    symbol=trim.symbol,
                    action="FORCE_DELEVER",
                    qty=qty,
                    price=position.current_price,
                    reasoning=trim.reasoning[:500],
                    run_id=ctx.run_id,
                    broker_order_id=order.get("id"),
                    fill_status="submitted",
                )
            except Exception as e:  # noqa: BLE001
                logger.error(
                    "GROSS-EXPOSURE DE-LEVER: trade row for %s failed: %s — "
                    "the order may still be live at the broker", trim.symbol, e,
                )
            # Rebuild THIS symbol's stop coverage on its actual fill before
            # the loop cancels the next symbol's stops (docs/WORK.md item
            # 111). Finalizing the batch once after the loop left every
            # earlier symbol with no protective stop while the later ones were
            # cancelled, submitted and waited on — and the ladder only fires
            # in a drawdown. The trims themselves (which names, how much, at
            # what limit) were all fixed by `apply_gross_ceiling` before the
            # loop began and are unchanged.
            protection["trim_action"] = trim.action
            protection["trim_qty"] = qty
            self._finalize_pending_protections(
                [protection], context="GROSS-EXPOSURE DE-LEVER",
            )
        # ASYNC-FILL RACE: any trim that did not reach a terminal state is
        # already in `_unsettled_exit_orders` (registered centrally by
        # `_finalize_pending_protections`, so every exit path is covered),
        # and the next gross re-measure nets its open quantity out.
        try:
            account = self.broker.get_account()
            ctx.positions = self.broker.get_positions()
            ctx.cash = account["cash"]
            ctx.deployable_cash = self._compute_deployable_cash(ctx.cash, ctx.positions)
            ctx.total_value = account["portfolio_value"]
            ctx.last_equity = account.get("last_equity", ctx.total_value)
            # Re-measure so the alert and the dashboard report the book that
            # now exists, not the one that triggered the de-lever.
            self._resolve_gross_ceiling(ctx)
            self._alert_owner_delever_incomplete(ctx)
            self._record_delever_shortfall(
                ctx, held_gross_before=outcome.held_gross,
                ceiling_usd_before=outcome.ceiling_usd,
                equity_before=equity_before, protections=pending_protections,
            )
        except Exception as e:  # noqa: BLE001
            logger.error("GROSS-EXPOSURE DE-LEVER: broker refresh failed: %s", e)
        return orders

    def _record_delever_shortfall(
        self, ctx: RunContext, *, held_gross_before: float,
        ceiling_usd_before: float | None,
        equity_before: float | None, protections: list[dict],
    ) -> None:
        """Durable record of a gross-exposure de-lever that finished with the
        book STILL over its ceiling (docs/WORK.md item 112).

        `_alert_owner_delever_incomplete` already sets the flag the session
        message reads; what nothing kept was the evidence — the book before
        and after, the ceiling, and what each order actually did — so a
        failed de-lever could not be reviewed after the log rotated. This
        writes ONE row in the desk's existing lifecycle-event stream
        (`specialist_evidence`, `agent_name='pipeline'`,
        `kind='pipeline_event'`, `scope='run'` — the same shape
        `_record_pipeline_event` and the stop-out reconciler use), NOT in
        `agent_logs`: that table is the paid-model ledger, and the cost
        circuit refuses a same-day `agent_logs` row whose run has no budget
        session, which a pre-agent preamble write could produce.

        Observability only: no order, alert, sizing or sequencing depends on
        it, it writes nothing when the book cleared its ceiling, and it never
        raises.
        """
        leverage = ctx.leverage or {}
        if not leverage.get("delever_incomplete"):
            return
        try:
            import json
            gross_before_x = (
                held_gross_before / equity_before
                if isinstance(equity_before, (int, float)) and equity_before > 0
                else None
            )
            order_rows = [
                {
                    "symbol": p.get("symbol"),
                    "action": p.get("trim_action"),
                    "qty_submitted": p.get("trim_qty"),
                    "broker_order_id": p.get("order_id"),
                    "terminal_status": p.get("terminal_status"),
                    "stop_coverage_confirmed": p.get("coverage_confirmed"),
                }
                for p in protections
            ]
            payload = {
                "stage": "gross_delever", "outcome": "still_over_ceiling",
                "reason": leverage.get("reason") or "",
                "rung": leverage.get("rung"),
                "gross_usd_before": held_gross_before,
                "gross_x_before": gross_before_x,
                "equity_before": equity_before,
                "ceiling_usd_before": ceiling_usd_before,
                "gross_usd_after": leverage.get("gross_usd"),
                "gross_x_after": leverage.get("gross_x"),
                "ceiling_x": leverage.get("ceiling_x"),
                "orders": order_rows,
            }
            self.db.insert_specialist_evidence(
                run_id=ctx.run_id, agent_name="pipeline", kind="pipeline_event",
                scope="run", symbol=None,
                decision_id=getattr(ctx, "decision_id", None),
                evidence_json=json.dumps(payload, sort_keys=True, default=str),
            )
        except Exception as exc:  # noqa: BLE001 — evidence is never trading authority
            logger.warning(
                "GROSS-EXPOSURE DE-LEVER: could not persist the shortfall "
                "record for run %s: %s", ctx.run_id, exc,
            )

    def _alert_owner_delever_incomplete(self, ctx: RunContext) -> None:
        """Flag AND page the owner when the gross-exposure de-lever did not work.

        `_enforce_gross_ceiling` already logs a warning the moment it
        *decides* to trim. What nothing checked before this is the OUTCOME:
        a trim can be submitted and still leave the book over the ceiling —
        an order that failed to place, a partial fill, integer-share
        rounding down, or the market moving between the plan and the fill
        can all produce this. That gap is unreportable, not silent: it was
        always in the log, just never in anything the owner actually reads
        (a Telegram message) or in the session result a test can assert on.
        This is a reporting-only check — it runs after every broker call in
        the de-lever is already done and changes no order, no sizing, and no
        sequencing.

        Sets `ctx.leverage["delever_incomplete"] = True` (which every
        session-result dict already threads through, since they all copy
        `ctx.leverage` verbatim) whenever we have a real, freshly-measured
        gross exposure that is still above the ceiling. Never guesses: both
        numbers come from the same post-refresh `_resolve_gross_ceiling`
        call used for the ordinary leverage line, and the check is skipped
        (not defaulted to False) when either is unmeasurable.

        Item 112: promoted from a one-line session bullet to a STANDALONE
        `send_owner_alert`, mirroring the sibling
        `_alert_owner_force_delever_incomplete` — a book left over its
        ceiling after the ladder ran is the same class of unattended
        margin risk. The page is guarded by a STATE CHANGE: it fires only on
        the transition INTO still-over (prior recorded state was cleared or
        absent), so a book that sits over the ceiling for days does not page
        every session. The flag, the log line and the shortfall evidence
        record are unchanged — they still happen every session it is over;
        only the owner PAGE is edge-triggered. No "keep selling" behaviour is
        added here (owner-escalated, out of scope), and no global throttle is
        added to `send_owner_alert`.
        """
        leverage = ctx.leverage or {}
        gross_x = leverage.get("gross_x")
        ceiling_x = leverage.get("ceiling_x")
        if not isinstance(gross_x, (int, float)) or not isinstance(ceiling_x, (int, float)):
            # Unmeasurable this session — record no state, so it neither pages
            # nor resets the transition edge for the next real measurement.
            return
        still_over = gross_x > ceiling_x

        # Read the prior session's recorded state BEFORE writing this one, so
        # the read sees only earlier sessions (transition detection). Both the
        # read and the write are best-effort: a persistence hiccup must not
        # break the de-lever path, and on a failed read we fall back to paging
        # (fail loud, never silence a real over-ceiling book).
        run_id = getattr(ctx, "run_id", None)
        was_over: bool | None
        try:
            was_over = self.db.get_last_delever_over_ceiling(exclude_run_id=run_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("delever ceiling-state read failed: %s", exc)
            was_over = None
        try:
            self.db.save_delever_ceiling_state(
                run_id=run_id or "", over_ceiling=still_over,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("delever ceiling-state write failed: %s", exc)

        if not still_over:
            return

        leverage["delever_incomplete"] = True
        logger.warning(
            "GROSS-EXPOSURE DE-LEVER: still over the ceiling after de-levering "
            "— gross exposure %.2fx equity vs a %.2fx ceiling.",
            gross_x, ceiling_x,
        )

        if was_over:
            # Already paged when the book first crossed into still-over; do not
            # repeat every session while it stays there.
            return

        try:
            from src import notifier as _notifier

            msg = (
                f"GROSS-EXPOSURE DE-LEVER INCOMPLETE: after de-levering, gross "
                f"exposure is still {gross_x:.2f}x equity against a "
                f"{ceiling_x:.2f}x ceiling. The book remains over its ceiling "
                f"until this is resolved."
            )
            _notifier.send_owner_alert(msg)
        except Exception as exc:  # noqa: BLE001
            logger.error("de-lever incomplete owner alert failed: %s", exc)
