"""Account margin-interest view, carved out of routes_live.py."""
from __future__ import annotations

import logging

from src.api.broker_reads import read_margin_interest
from src.api.schemas_margin import MarginInterestCumulative, MarginInterestEstimate

logger = logging.getLogger("src.api.routes_live")


def _compute_margin_interest(cash: float | None) -> MarginInterestEstimate:
    """Degrades to an honest all-`None` `MarginInterestEstimate()` on any
    read failure — mirrors `_compute_liquidity`/`_compute_risk_limits`'s
    fail-closed-to-empty posture, and is also the correct rendering of
    today's actual state (no debit balance, `allow_margin` is `False`)."""
    try:
        data = read_margin_interest(cash)
    except Exception as exc:
        logger.warning("routes_live._compute_margin_interest failed: %s", exc)
        return MarginInterestEstimate(error=str(exc))
    cumulative_data = data.get("cumulative")
    cumulative = (
        MarginInterestCumulative(
            this_week_usd=cumulative_data.get("this_week_usd"),
            current_month_usd=cumulative_data.get("current_month_usd"),
            current_month_label=cumulative_data.get("current_month_label"),
            prior_months=cumulative_data.get("prior_months") or [],
            all_time_usd=cumulative_data.get("all_time_usd"),
            all_time_since=cumulative_data.get("all_time_since"),
            is_estimate=cumulative_data.get("is_estimate"),
            source=cumulative_data.get("source"),
        )
        if cumulative_data else None
    )
    return MarginInterestEstimate(
        debit_balance=data.get("debit_balance"),
        rate_pct=data.get("rate_pct"),
        daily_usd=data.get("daily_usd"),
        annual_usd=data.get("annual_usd"),
        label=data.get("label"),
        broker_check_note=data.get("broker_check_note"),
        days_charged=data.get("days_charged"),
        period_usd=data.get("period_usd"),
        error=data.get("error"),
        cumulative=cumulative,
    )
