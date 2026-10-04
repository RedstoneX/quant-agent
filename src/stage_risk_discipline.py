"""Rejection of a decision whose stated reason was proven false (split out of
``src/stage_risk.py`` so that file does not grow).

Records the two pipeline events and sends the standalone owner alert, which
needs the ``pipeline`` so its own failure is counted rather than lost.
"""

from __future__ import annotations


def reject_false_claim(pipeline, ctx, decision, symbol_u, check) -> None:
    from src.pipeline_stages import (
        _alert_holding_discipline_block,
        _record_pipeline_event,
    )

    _record_pipeline_event(
        pipeline, ctx, decision.symbol, "risk", "rejected", check.finding,
    )
    _record_pipeline_event(
        pipeline, ctx, decision.symbol, "risk",
        "holding_discipline_claim_false", check.finding,
    )
    _alert_holding_discipline_block(
        pipeline,
        symbol=symbol_u,
        action=decision.action,
        reasons=check.reasons,
    )
