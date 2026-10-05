"""Intraday snapshot-health tracking, lifted out of `pipeline_intraday.py`.

Nothing here may import `src.pipeline`: it is a base of the intraday mixin.
Every handler logs a full traceback AND records a counted row through the
pipeline's own `db` (always present in production; `_site` skips the row when
a bare test owner has none).
"""
import logging

from src.sentinel.guarded_site import record_site

logger = logging.getLogger("src.pipeline")


class SnapshotHealthMixin:
    def _track_intraday_snapshot_ok(self, symbol: str) -> None:
        """Reset a symbol's consecutive-miss streak. Never raises — a
        monitoring bug must not be able to break the scan it watches."""
        try:
            self.db.record_intraday_symbol_snapshot_result(symbol, ok=True)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "intraday snapshot health: failed to record OK for %s", symbol,
                exc_info=True,
            )
            record_site(self, "snapshot_ok_record", exc, context={"symbol": symbol})

    def _track_intraday_snapshot_miss(self, symbol: str) -> None:
        """Record a missed snapshot for `symbol` and alert the owner once
        it has failed 3 consecutive ticks (~90 min) — see
        `Database.record_intraday_symbol_snapshot_result`'s docstring for
        the threshold/cooldown reasoning. Never raises."""
        try:
            result = self.db.record_intraday_symbol_snapshot_result(symbol, ok=False)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "intraday snapshot health: failed to record miss for %s", symbol,
                exc_info=True,
            )
            record_site(self, "snapshot_miss_record", exc, context={"symbol": symbol})
            return
        if not result.get("should_alert"):
            return
        try:
            from src import notifier as _notifier

            misses = result.get("consecutive_misses", 0)
            _notifier.send_owner_alert(
                "INTRADAY SNAPSHOT UNAVAILABLE\n"
                f"{symbol} has failed to return snapshot data for "
                f"{misses} consecutive scans (~{misses * 30} min). It is being "
                "silently excluded from intraday move detection until this "
                "resolves — check whether the ticker is still valid/tradable "
                "on Alpaca. Will not re-alert on this symbol for 24h.", category=_notifier.CATEGORY_OPERATIONAL,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "intraday snapshot health: alert failed for %s", symbol,
                exc_info=True,
            )
            record_site(self, "snapshot_miss_alert", exc, context={"symbol": symbol})
