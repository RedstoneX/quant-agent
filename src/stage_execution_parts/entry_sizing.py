"""The entry loop's share sizing, lifted out of `ExecutionStage._run_session` (src/stage_execution.py).

Spec 11.1 sizing (allocation quantity, capped by the risk-budget quantity),
body unchanged apart from dedenting and a short unpack of the per-run
`EntryRun` facts at the top; the zero-quantity `continue` returns `SKIP` and
the caller continues. Live-money entry code: behaviour is identical.
"""

from __future__ import annotations

from src.pipeline_stages import (
    _fmt_shares,
    _fractional_sizing_allowed,
    _qty_by_risk_budget,
    _record_execution_skip,
    _risk_budget_pct,
    _size_shares,
    logger,
)
from src.stage_execution_parts.state import SKIP


def entry_qty(run, decision, is_short, sizing_price, risk_sizing_price, stop_price):
    """Size one entry; `SKIP` where the loop skipped a zero quantity."""
    pipeline, ctx, total_value = run.pipeline, run.ctx, run.total_value
    # Spec §11.1. Exact sizing when the flag is on AND the broker
    # confirms the symbol is fractionable; whole shares otherwise.
    # Resolved ONCE per symbol here so every share count below —
    # allocation, risk budget, cash re-size — is quantized the
    # same way. Two different roundings inside one sizing decision
    # is how a stop ends up covering a different number of shares
    # than the entry bought.
    fractional = _fractional_sizing_allowed(
        pipeline,
        decision.symbol,
        is_short=is_short,
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
        pipeline,
        total_value=total_value,
        sizing_price=risk_sizing_price,
        stop_price=stop_price,
        is_short=is_short,
        fractional=fractional,
    )
    if qty_by_risk is not None and qty_by_risk < qty_by_alloc:
        _risk_pct = _risk_budget_pct(pipeline)
        logger.info(
            "Vol-adjusted sizing for %s: qty_by_alloc=%s → qty_by_risk=%s "
            "(risk %.2f/share, budget $%.0f = %.1f%% of equity)",
            decision.symbol,
            _fmt_shares(qty_by_alloc),
            _fmt_shares(qty_by_risk),
            abs(risk_sizing_price - stop_price),
            total_value * _risk_pct / 100,
            _risk_pct,
        )
        qty = qty_by_risk
    else:
        qty = qty_by_alloc
    if qty <= 0:
        logger.warning("Calculated qty=0 for %s, skipping", decision.symbol)
        _record_execution_skip(
            pipeline,
            ctx,
            decision.symbol,
            "qty_zero",
            f"allocation {decision.allocation_pct:.2f}% at ${sizing_price:.2f} rounds to zero shares",
        )
        return SKIP
    return qty, fractional, qty_by_alloc, qty_by_risk
