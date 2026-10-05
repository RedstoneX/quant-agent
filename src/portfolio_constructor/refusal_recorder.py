"""Writes the constructor's durable refusal rows to `trade_refusals`.

Lives OUTSIDE the package `__init__` on purpose: the row writer is a
trade-table write, and the read-only API guard must not see a write-capable
name just because a module imported a sibling from the package. The
constructor never imports this module; the composition root builds one
recorder around its database and hands it in (`recorder=`).
"""
from __future__ import annotations

import logging

from src.portfolio_constructor.config import (
    STOP_REFUSAL_REWARD_BELOW_RISK,
    SUBFLOOR_RISK_OBSERVED,
    _SUBFLOOR_RISK_STAGE,
)
from src.risk.constants import REWARD_RISK_PARITY

#: One row per computed-vs-analyst target comparison (ledger row
#: `target_divergence_warn_pct`). An OBSERVATION, never a refusal: it carries
#: stage `_SUBFLOOR_RISK_STAGE`, the signed gap in `observed_gap_pct` and the
#: warn threshold in `threshold`.
TARGET_DIVERGENCE_OBSERVED = "target_divergence_observed"

# The constructor's own logger, so log capture and filters keep matching.
logger = logging.getLogger("src.portfolio_constructor")


class TradeRefusalRecorder:
    """Bodies moved verbatim from `PortfolioConstructor`; neither ever raises."""

    def __init__(self, db) -> None:
        self.db = db

    def record_parity_refusal(
        self, symbol, direction, entry, stop, level, ratio, *, stage="construction",
    ):
        """Write ONE parity refusal to the durable table. Never raises."""
        db = self.db
        if db is None:
            return
        try:
            db.insert_trade_refusal(
                symbol=symbol, direction=direction,
                refusal=STOP_REFUSAL_REWARD_BELOW_RISK,
                entry_price=float(entry), stop_price=float(stop),
                level_used=float(level), reward_risk=float(ratio),
                threshold=float(REWARD_RISK_PARITY),
                level_was_measured=True, stage=stage,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "Constructor: parity refusal row write failed for %s: %s",
                symbol, e,
            )

    def record_subfloor_risk_target(
        self, symbol: str, direction: str | None, requested_pct: float,
        min_risk_pct: float,
    ) -> None:
        """Record a positive sub-floor PM risk request. Never raises."""
        db = self.db
        if db is None:
            return
        try:
            db.insert_trade_refusal(
                symbol=symbol, direction=direction,
                refusal=SUBFLOOR_RISK_OBSERVED,
                stage=_SUBFLOOR_RISK_STAGE,
                requested_risk_pct=float(requested_pct),
                threshold=float(min_risk_pct),
            )
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "Constructor: sub-floor risk observation write failed "
                "for %s: %s", symbol, e,
            )

    def record_target_divergence(
        self, symbol: str, gap_pct: float, threshold_pct: float,
    ) -> None:
        """Record ONE computed-vs-analyst target comparison. Never raises."""
        db = self.db
        if db is None:
            return
        try:
            db.insert_trade_refusal(
                symbol=symbol, direction=None,
                refusal=TARGET_DIVERGENCE_OBSERVED,
                stage=_SUBFLOOR_RISK_STAGE,
                observed_gap_pct=float(gap_pct),
                threshold=float(threshold_pct),
            )
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "Constructor: target divergence row write failed for %s: %s",
                symbol, e,
            )
