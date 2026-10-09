"""format_session_result: routes each session mode to its formatter.

Moved verbatim from src/trader_feed.py; see src/trader_feed/__init__.py.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from pathlib import Path
from typing import Any

from src.notifier import (
    _actionable_coverage_gaps,
    _attr_or_key,
    _clip_text,
    _DB_PATH as _NOTIFIER_DB_PATH,
    _fmt_signed_money,
    _lookup_company_profiles,
    _margin_interest_lines,
    _new_block,
    _new_section,
    _seal_section,
    company_name,
    describe_ai_cost,
    describe_data_status,
    describe_skipped_decision,
    fmt_time_12h,
    humanize_status,
    format_session_result as _base_format_session_result,
    TelegramNotifier,
)
from src.trading_calendar import SESSION_WINDOWS, et_now, in_session_window, to_et
from src.trader_feed.common import (
    _BASE_ONLY_STATUSES,
    logger,
)
from src.trader_feed.decision import (
    _format_decision_session,
)
from src.trader_feed.evening import (
    _format_earnings,
    _format_evening,
    _format_position_review,
)
from src.trader_feed.intraday import (
    _format_intra_check,
)


def format_session_result(
    mode: str,
    result: dict | None,
    elapsed_seconds: float,
    error: BaseException | None = None,
) -> str | None:
    """Build the Telegram message without affecting trading.

    Enrichment is intentionally fail-soft: if the read-only evidence lookup or
    formatter has any problem, the existing notifier format is used instead.
    """
    if error is not None or not isinstance(result, dict):
        return _base_format_session_result(mode, result, elapsed_seconds, error=error)

    status = str(result.get("status", "unknown"))
    # The pre-market earnings pass (2026-09-18): rendered here for the two
    # outcomes that speak — filings read, or the reader failing — so the
    # message names each company instead of counting them. Every silent
    # status (nothing_new / fetch_error / market_holiday) and the paid-
    # analysis latch keep the base formatter's noise policy unchanged.
    if mode == "earnings_preprocess" and status in ("preprocessed", "analysis_error"):
        try:
            return _format_earnings(result, elapsed_seconds)
        except Exception as exc:  # noqa: BLE001
            logger.warning("trader-feed earnings render failed: %s", exc)
            return _base_format_session_result(mode, result, elapsed_seconds, error=None)
    if status in _BASE_ONLY_STATUSES or status.startswith("pm_") or status == "paid_analysis_suspended":
        return _base_format_session_result(mode, result, elapsed_seconds, error=None)

    try:
        if mode == "intra_check":
            return _format_intra_check(result, elapsed_seconds)

        if mode in ("midday", "close"):
            return _format_position_review(mode, result, elapsed_seconds)

        if mode in ("morning", "once"):
            return _format_decision_session(mode, result, elapsed_seconds)

        if mode == "evening":
            return _format_evening(result, elapsed_seconds)

        # earnings/meta/daily have their own noise policy — base formatter.
        return _base_format_session_result(mode, result, elapsed_seconds, error=None)
    except Exception as exc:  # noqa: BLE001
        logger.warning("trader-feed enrichment failed for %s: %s", mode, exc)
        return _base_format_session_result(mode, result, elapsed_seconds, error=None)
