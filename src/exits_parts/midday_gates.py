"""Pre-gates of the midday exit loop, lifted verbatim out of
`ExitEngineMixin._midday_execute_llm_actions` (src/pipeline_exits.py).

The metric-contradiction veto (Phase 3.2) and the AI Risk veto (Phase 3.4).
The body is unchanged apart from `continue` -> `return SKIP`; `self` is bound
to the pipeline instance (`loop.owner`) so every line reads as before.
Sell-side code: behaviour is identical.
"""

import logging

from src.exits_parts.midday_state import SKIP, MiddayLoop
from src.sentinel.guarded_exit import record_exit_guard

#: The moved code logged under `src.pipeline` before the move and still does.
logger = logging.getLogger("src.pipeline")


def midday_pre_gates(loop: MiddayLoop, action_item: dict, act, symbol):
    """Veto one symbol's exit before the noise band; `SKIP` drops it."""
    self = loop.owner
    run_id = loop.run_id
    metric_deltas = loop.metric_deltas
    risk_vetoed_symbols = loop.risk_vetoed_symbols
    # Same-day trim discipline: a symbol that already had a sell-side
    # action TODAY (midday REDUCE, force-delever, etc.) is off-limits for
    # additional REDUCE / SELL on a SECOND session unless the LLM
    # explicitly cites a hard trigger in the reason. TRAIL_STOP is
    # exempt — adjusting a stop is not selling shares.
    #
    # 2026-05-04 AMZN: midday REDUCE 20 of 41 @ +12.4% on TARGET_BREACH,
    # then close REDUCE 10 of 21 @ +13.8% on the SAME TARGET_BREACH
    # flag = 73% one-day trim on a strengthening thesis. Mechanical
    # double-application of one signal violates "good stocks are meant
    # to be held".
    # Phase 3.2 — a deterioration verdict may not contradict the
    # reviewer's own recorded numbers. Vetoes ONLY a SELL/REDUCE/
    # COVER whose stated reason claims the position is stalling
    # while every metric that moved since the previous review
    # improved. Exits on new information (news, earnings, regime,
    # invalidation) are untouched, however good the numbers look —
    # see src/risk/exit_guard.py. metric_deltas is already sign-
    # corrected per symbol (see _build_position_facts), so COVER
    # needs no extra handling here.
    if act in ("SELL", "REDUCE", "COVER") and metric_deltas:
        from src.risk.exit_guard import veto_contradicted_exit

        deltas = metric_deltas.get(symbol)
        if deltas is not None:
            veto = veto_contradicted_exit(
                act,
                action_item.get("reason", ""),
                deltas,
            )
            if veto:
                logger.warning("Exit guard: %s", veto)
                try:
                    self.db.record_intraday_evaluation(
                        symbol=symbol,
                        run_id=run_id,
                        status="exit_vetoed_contradicts_own_metrics",
                        detail=veto[:500],
                    )
                    record_exit_guard(self, "exit_guard.metric_audit_write")
                except Exception as e:  # noqa: BLE001
                    record_exit_guard(
                        self,
                        "exit_guard.metric_audit_write",
                        e,
                        logger,
                        symbol=symbol,
                        effect="audit row not written",
                    )
                from src.risk.exit_refusal import CODE_CONTRADICTS_METRICS

                self._record_exit_refusal(
                    symbol=symbol,
                    run_id=run_id,
                    action=act,
                    code=CODE_CONTRADICTS_METRICS,
                    dropped=True,
                    detail=veto[:400],
                    layer="metric_contradiction",
                )
                return SKIP

    # Phase 3.3 — EVERY exit must name a trigger, not just the second
    # one on a symbol in a day.
    #
    # The gate below used to be conditioned on `symbol in
    # already_trimmed`, so a position's FIRST sale of the day executed
    # on soft reasoning entirely unchecked — and a first sale is almost
    # every sale. Both of the exits the evening review graded
    # "premature" on 2026-08-26 (EPD, MRVL) were first sales and sailed
    # straight through.
    #
    # Failing closed here means HOLDING, and every position carries a
    # broker-resident stop (AGENTS.md invariant 3), so the downside of
    # a wrongly-blocked exit is bounded by that stop. The downside of a
    # wrongly-allowed one is the pattern that emptied the book.
    # Phase 3.4 — the AI Risk Manager reviewed these exits and
    # rejected this one. Its authority over exits mirrors the veto it
    # has always had over entries.
    if act in ("SELL", "REDUCE", "COVER") and symbol in (risk_vetoed_symbols or set()):
        logger.warning(
            "Position reviewer: skipping %s %s — vetoed by AI Risk",
            act,
            symbol,
        )
        return SKIP
    return None
