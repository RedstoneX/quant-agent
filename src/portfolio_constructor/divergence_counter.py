"""Records every computed-target vs analyst-target comparison, one durable row
each, and logs it.

Why this exists. The ledger row for
`src.config.RiskConfig.target_divergence_warn_pct` (= 25) routes to a
recording that was SPECIFIED and never built: nothing in the repo kept the
gap, so the threshold could only be argued about.

Each comparison writes ONE row (symbol, signed observed gap, the threshold it
was compared against) through the constructor's refusal recorder. Nothing is
accumulated here: the distribution, the share past the threshold and the sign
bias are queries over those rows at READ time. With no recorder wired the
comparison is logged only and nothing is counted. This changes no value,
gates nothing and places no order. A recording is not a settlement.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def log_divergence(
    *,
    symbol: str,
    derivation,
    threshold_pct: float,
    recorder=None,
) -> None:
    """Record where the model's guess and the computed level disagree.

    Lifted from `PortfolioConstructor._log_target_divergence`. The model's
    target is no longer arithmetic, but it is still the only read available
    on whether the model's chart-reading is worth anything. A large,
    one-directional gap across many symbols is a finding about the seat; a
    large gap on one symbol is a finding about that symbol.
    """
    if derivation.price is None or derivation.model_target is None:
        return
    gap = derivation.divergence_pct
    if gap is None:
        return
    if recorder is not None:
        recorder.record_target_divergence(symbol, gap, threshold_pct)
    message = "Constructor: %s target -- computed $%.2f (%s) vs analyst's reference_target $%.2f: %+.1f%%"
    args = (
        symbol,
        derivation.price,
        derivation.basis,
        derivation.model_target,
        gap,
    )
    if abs(gap) >= threshold_pct:
        logger.warning(message + " -- the model and the chart disagree sharply", *args)
    else:
        logger.info(message, *args)
