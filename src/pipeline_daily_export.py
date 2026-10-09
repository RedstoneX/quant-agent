"""The daily P&L CSV export, lifted whole from `TradingPipeline.run_daily`.

Pure data export: fetch the broker's full portfolio history, build a CSV and
hand it to the one notifier factory. No LLM calls, no orders. Lives here so
the pipeline module shrinks rather than widens when the notifier is built
through the funnel.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def run_daily_export(pipeline) -> dict:
    """Fetch full portfolio history from Alpaca, build a CSV, and send
    via Telegram. No LLM calls — pure data export. Runs on weekdays.

    Returns {"status": "sent", "rows": N, "filename": ...} on delivery,
    {"status": "skipped", ...} when Telegram is disabled (CSV built but no
    sink), {"status": "error", ...} on a real failure. The status must be
    honest: previously it reported "sent" even when the upload failed or
    the notifier was disabled, so the operator couldn't tell a delivered
    export from a silently-dropped one.
    """
    from src.notifier import build_daily_csv, build_default_notifier
    from src.trading_calendar import et_today

    try:
        closes = pipeline.broker.get_full_portfolio_history()
        if not closes:
            logger.warning("run_daily: no portfolio history returned")
            return {"status": "error", "error": "no data from portfolio_history"}
        csv_bytes = build_daily_csv(closes)
        date_str = et_today().strftime("%Y-%m-%d")
        filename = f"pnl_history_{date_str}.csv"
        caption = f"📊 P&L History export — {date_str} ({len(closes)} trading days)"
        notifier = build_default_notifier()
        delivered = notifier.send_document(csv_bytes, filename, caption)
        base = {"rows": len(closes), "filename": filename}
        if delivered:
            logger.info("run_daily: sent %d rows as %s", len(closes), filename)
            return {"status": "sent", **base}
        if not notifier.enabled:
            # CSV built fine; Telegram simply isn't configured — not a
            # failure, just nowhere to deliver it.
            logger.info(
                "run_daily: built %d-row CSV %s but Telegram is disabled",
                len(closes),
                filename,
            )
            return {"status": "skipped", **base}
        # Enabled but the upload failed (network / API / rate limit).
        logger.error("run_daily: Telegram delivery failed for %s", filename)
        return {"status": "error", "error": "telegram delivery failed", **base}
    except Exception as exc:
        logger.error("run_daily failed: %s", exc, exc_info=True)
        return {"status": "error", "error": str(exc)}
