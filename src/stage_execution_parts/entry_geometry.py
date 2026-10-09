"""The entry loop's execution-time stop geometry, lifted out of `ExecutionStage._run_session` (src/stage_execution.py).

The ATR stop-floor belt and the geometry/payoff check that follows it, body
unchanged apart from dedenting; `_run_session` binds the returned
`stop_price` at the exact point the block used to run. Live-money entry
code: behaviour is identical.
"""

from __future__ import annotations

from src.pipeline_stages import (
    LEVEL_BACKED_STOP_RULES,
    _execution_payoff_skip_reason,
    logger,
)


def entry_stop_price(ctx, decision, is_short, sizing_price):
    """The stop this entry is protected with, after the execution-time belt."""
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
    return stop_price
