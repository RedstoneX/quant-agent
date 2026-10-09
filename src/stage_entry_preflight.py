"""The entry-viability preflight of the execution stage, lifted out verbatim.

Moved out of ``ExecutionStage._run_session`` in ``src/stage_execution.py`` on
2026-10-08 so the stage could host the price-feed preflight without growing.
Same code, same scope (it is listed in ``src/number_scope.py``). The only
change on the way out is the one this PR makes: a name with NO today print
is no longer refused on the spot -- it joins a waiting set and is re-asked
for, in ONE batched request for every waiting name, until it prints or the
session's own slot ends (``src.price_feed_preflight.wait_for_today_prints``).
A name that prints late runs through this SAME pass, so it is sized, risk-
bounded and funded exactly like a name that printed first time.
"""

from __future__ import annotations

from src.pipeline_stages import (
    _fractional_sizing_allowed,
    _live_fill_price,
    _qty_by_risk_budget,
    _record_execution_skip,
    _size_shares,
    _today_sizing_price,
)
from src.price_feed_preflight import wait_for_today_prints
from src.sizing_refusal import NO_SIZING_PRINT, classified_no_price, sizing_price_or_refusal
from src import pipeline_stages as _pipeline_stages

# Tests patch the helpers above on `src.pipeline_stages` and its write-through
# mirror re-binds them in every registered stage module; this module binds the
# same names, so it registers itself (the mirror reads the table at call time).
_pipeline_stages._STAGE_CLASS_MODULES["entry_viability_preflight"] = __name__


def entry_viability_preflight(
    pipeline, ctx, buy_decisions: list, *, total_value: float, price_map: dict, fundable_notional: dict
) -> list:
    """The approved entries that will survive the submit loop, in order, with
    ``fundable_notional`` filled for each BUY. Names with no today print wait
    for the batched re-ask and, once printed, take the same pass."""
    waiting: list = []
    survivors = _viability_pass(
        pipeline,
        ctx,
        buy_decisions,
        total_value=total_value,
        price_map=price_map,
        fundable_notional=fundable_notional,
        waiting=waiting,
    )
    if not waiting:
        return survivors
    printed, skipped = wait_for_today_prints(pipeline, ctx, waiting)
    for decision, reason, detail in skipped:
        _record_execution_skip(pipeline, ctx, decision.symbol, reason, detail)
    late: list = []
    survivors += _viability_pass(
        pipeline,
        ctx,
        printed,
        total_value=total_value,
        price_map=price_map,
        fundable_notional=fundable_notional,
        waiting=late,
    )
    for decision in late:  # printed in the batch, then not through the sizing chain
        _record_execution_skip(
            pipeline,
            ctx,
            decision.symbol,
            NO_SIZING_PRINT,
            "printed in the batched re-ask but the sizing read found no today print",
        )
    return survivors


def _unpriced(pipeline, ctx, symbol: str, market_price) -> bool:
    """Record the no-price skip (classified when the read was) and say so."""
    if isinstance(market_price, (int, float)) and market_price > 0:
        return False
    classified = classified_no_price(pipeline, symbol)
    if classified:
        _record_execution_skip(pipeline, ctx, symbol, *classified)
    else:
        _record_execution_skip(
            pipeline,
            ctx,
            symbol,
            "no_price",
            "no verifiable live price (daily bar close is not a fill reference)",
        )
    return True


def _stale_entry_detail(decision, market_price: float) -> str | None:
    """The stale-entry refusal detail when the decision's entry is more than
    5% from the market, else None (same threshold as before the lift)."""
    if decision.entry_price <= 0:
        return None
    deviation = abs(decision.entry_price - market_price) / market_price
    if deviation <= 0.05:
        return None
    return f"entry ${decision.entry_price:.2f} is {deviation * 100:.1f}% from market ${market_price:.2f} (threshold 5%)"


def _preflight_price(pipeline, ctx, decision, price_map: dict, waiting: list) -> float | None:
    """The price the funding preflight sizes this name at, or None when it waits or is skipped."""
    market_price = _live_fill_price(pipeline, decision.symbol)
    if market_price is not None:
        price_map[decision.symbol] = market_price
    if _unpriced(pipeline, ctx, decision.symbol, market_price):
        return None
    stale = _stale_entry_detail(decision, market_price)
    if stale:
        _record_execution_skip(pipeline, ctx, decision.symbol, "stale_entry", stale)
        return None
    # docs/WORK.md item 120: the funding preflight must size off the
    # same TODAY PRINT the submit loop will, never the fill-reference
    # mid. No print -> the submit loop will refuse this name, so the
    # sweep must not sell SGOV to fund it.
    sizing_print, why, detail = sizing_price_or_refusal(
        _today_sizing_price,
        pipeline,
        decision.symbol,
        "buy",
    )
    if sizing_print is None and why == NO_SIZING_PRINT:
        # No today print YET: the name waits for the batched re-ask
        # below, it is not refused (owner ruling 2026-10-08: a swing
        # desk keeps asking until a price is received).
        waiting.append(decision)
        return None
    if sizing_print is None:
        _record_execution_skip(pipeline, ctx, decision.symbol, why, detail)
        return None
    return max(sizing_print, decision.entry_price or 0)


def _viability_pass(
    pipeline, ctx, buy_decisions: list, *, total_value: float, price_map: dict, fundable_notional: dict, waiting: list
) -> list:
    # Run the cheap deterministic entry-viability checks BEFORE selling
    # SGOV. Production evidence showed the sweep funding names that were
    # guaranteed to die moments later on stale-entry / no-price / qty-zero
    # checks, creating avoidable sell/re-park churn. The full checks remain
    # in the submit loop below; this preflight only removes names whose
    # failure is already knowable and computes the actual quantized
    # notional that funding should cover.
    preflight_survivors = []
    for decision in buy_decisions:
        preflight_price = _preflight_price(pipeline, ctx, decision, price_map, waiting)
        if preflight_price is None:
            continue
        # Spec §11.1: quantized the SAME way the submit loop below will,
        # or the sweep funds a whole-share notional for an order that is
        # about to be placed fractionally — under-funding it, and letting
        # the cash gate re-impose the rounding tax this phase removes.
        # It is also the difference between skipping a sub-one-share
        # position as `qty_zero` and taking it, which under exact sizing
        # is a legitimate position rather than nothing.
        preflight_short = decision.action == "SHORT"
        preflight_fractional = _fractional_sizing_allowed(
            pipeline,
            decision.symbol,
            is_short=preflight_short,
        )
        preflight_qty = _size_shares(
            pipeline,
            (total_value * decision.allocation_pct / 100) / preflight_price,
            fractional=preflight_fractional,
        )
        if preflight_qty <= 0:
            _record_execution_skip(
                pipeline,
                ctx,
                decision.symbol,
                "qty_zero",
                f"allocation {decision.allocation_pct:.2f}% at ${preflight_price:.2f} rounds to zero shares",
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
            pipeline,
            total_value=total_value,
            sizing_price=preflight_price,
            stop_price=decision.stop_loss,
            is_short=preflight_short,
            fractional=preflight_fractional,
        )
        if preflight_risk_qty is not None and preflight_risk_qty < preflight_qty:
            preflight_qty = preflight_risk_qty
        if preflight_qty <= 0:
            # The risk budget alone cannot carry one orderable unit. The
            # submit loop will reach the same conclusion and skip; there
            # is nothing here for the sweep to fund.
            _record_execution_skip(
                pipeline,
                ctx,
                decision.symbol,
                "qty_zero",
                f"risk budget at ${preflight_price:.2f} entry / ${decision.stop_loss:.2f} stop rounds to zero shares",
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
    return preflight_survivors
