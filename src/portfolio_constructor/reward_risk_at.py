"""Reward:risk at the stop that will actually ship (lifted out of stops.py).

Moved VERBATIM out of `StopRules._reward_risk_at`: same body, same
docstring, same delegation to the one shared definition of this ratio.
Nothing about the ratio changed; the method there now calls through.
"""
from __future__ import annotations

from src.models import reward_to_risk


def reward_risk_at(
    entry_price: float,
    stop_price: float,
    target_price: float | None,
    is_short: bool,
) -> float | None:
    """Reward:risk measured against the stop that will actually ship.

    A thin alias for `models.reward_to_risk` — the ONE definition of
    this ratio in the codebase, shared with
    `TechAnalysisResult.risk_reward`, `TradeDecision.reward_risk` and
    the execution-time re-check in `src/pipeline_stages.py`. It used to
    be a fourth private copy, and the copies disagreed in ways that
    rejected real trades (see that function's docstring for the XLE
    1.67-vs-1.18 rejection).

    None means "this is not a measurable entry geometry", including
    every non-finite input. **A caller that had a target and got None
    back must refuse, not permit** — a NaN makes every `ratio < floor`
    comparison False, so treating None as "no opinion" there would wave
    a malformed trade straight through the floor.
    """
    return reward_to_risk(
        entry_price, stop_price, target_price, is_short=is_short,
    )
