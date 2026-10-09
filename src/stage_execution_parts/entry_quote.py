"""The entry loop's quote / marketable-limit / ceiling phase, lifted out of `_run_session`.

From `ExecutionStage._run_session` (src/stage_execution.py). Body unchanged
apart from dedenting and a short unpack of the per-run
(`EntryRun`) and per-name (`EntryLeg`) facts at the top; where the loop body
said `continue` (the two defer-to-the-back-of-the-queue branches) it returns
`SKIP` and the caller continues. Returns the `(limit_price, sizing_price)`
pair the rest of the entry body reads. Live-money entry code: behaviour is
identical.
"""

from __future__ import annotations

from src.entry_slippage_bound import entry_bound
from src.pipeline_stages import (
    _entry_slippage_bps,
    _record_pipeline_event,
    logger,
)
from src.stage_execution_parts.state import SKIP


def entry_limit_from_quote(run, leg, limit_price, sizing_price):
    """Price one entry's limit off its live quote; `SKIP` where the loop skipped."""
    pipeline, ctx = run.pipeline, run.ctx
    submit_queue, deferred_far_through = run.submit_queue, run.deferred_far_through
    original_entry_count, budget_is_gross = run.original_entry_count, run.budget_is_gross
    decision, is_short, queue_index = leg.decision, leg.is_short, leg.queue_index
    market_price, ask, bid = leg.market_price, leg.ask, leg.bid
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
            pipeline,
            ctx,
            decision.symbol,
            market_price,
            ask,
            slippage_bps,
            is_short=False,
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
        if ask > cap and queue_index < original_entry_count and decision.symbol not in deferred_far_through:
            deferred_far_through.add(decision.symbol)
            submit_queue.append(decision)
            logger.info(
                "BUY %s deferred to the back of the submit queue "
                "— the displayed IEX offer $%.4f is through the "
                "%.0fbp ceiling $%.4f, so it draws the deployment "
                "pool after the names quoting inside theirs. Not "
                "a refusal: it is submitted below.",
                decision.symbol,
                ask,
                slippage_bps,
                cap,
            )
            _record_pipeline_event(
                pipeline,
                ctx,
                decision.symbol,
                "execution",
                "entry_deferred_behind_clean_quotes",
                "buy_ask_above_cap",
                detail=(
                    f"IEX ask ${ask:.4f} through the "
                    f"{slippage_bps:.0f}bp ceiling ${cap:.4f}; "
                    f"moved to the back of the submit queue so it "
                    f"draws the deployment pool last"
                ),
            )
            return SKIP

        if ask > cap:
            logger.warning(
                "BUY %s submitted anyway — the displayed IEX offer "
                "$%.4f is %.1fbp above the $%.4f reference and "
                "through the %.0fbp ceiling $%.4f. IEX is not the "
                "NBBO the order fills against; the limit cannot "
                "pay more than the ceiling either way.",
                decision.symbol,
                ask,
                ask_premium_bps,
                market_price,
                slippage_bps,
                cap,
            )
            _record_pipeline_event(
                pipeline,
                ctx,
                decision.symbol,
                "execution",
                "venue_quote_through_ceiling",
                "buy_ask_above_cap",
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
                offer_limit,
                slippage_bps,
                market_price,
                ask,
                ask_premium_bps,
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
                    pipeline,
                    ctx,
                    decision.symbol,
                    "execution",
                    "safety_net",
                    "catch_up_inside_ceiling",
                    detail="stall left the original entry unfillable; limit stays at the already-approved ceiling",
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
            pipeline,
            ctx,
            decision.symbol,
            market_price,
            bid,
            slippage_bps,
            is_short=True,
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
                decision.symbol,
                bid,
                slippage_bps,
                floor,
            )
            _record_pipeline_event(
                pipeline,
                ctx,
                decision.symbol,
                "execution",
                "entry_deferred_behind_clean_quotes",
                "short_bid_below_floor",
                detail=(
                    f"IEX bid ${bid:.4f} through the "
                    f"{slippage_bps:.0f}bp floor ${floor:.4f}; "
                    f"moved to the back of the submit queue so it "
                    f"draws the gross deployment pool last"
                ),
            )
            return SKIP

        if bid < floor:
            logger.warning(
                "SHORT %s submitted anyway — the displayed IEX bid "
                "$%.4f is %.1fbp below the $%.4f reference and "
                "through the %.0fbp floor $%.4f. IEX is not the "
                "NBBO the order fills against; the limit cannot "
                "sell below the floor either way.",
                decision.symbol,
                bid,
                bid_discount_bps,
                market_price,
                slippage_bps,
                floor,
            )
            _record_pipeline_event(
                pipeline,
                ctx,
                decision.symbol,
                "execution",
                "venue_quote_through_ceiling",
                "short_bid_below_floor",
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
                bid_limit,
                slippage_bps,
                market_price,
                bid,
                bid_discount_bps,
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
                    pipeline,
                    ctx,
                    decision.symbol,
                    "execution",
                    "safety_net",
                    "catch_up_inside_ceiling",
                    detail="stall left the original entry unfillable; limit stays at the already-approved floor",
                )
    return limit_price, sizing_price
