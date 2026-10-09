"""Live-context resolution step (moved verbatim from TradingPipeline)."""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


class LiveContextResolveSession:
    """Live-context resolution step (moved verbatim from TradingPipeline)."""

    def __init__(
        self,
    ) -> None:
        pass

    def run(self, snapshots: dict, symbols: list) -> tuple:
        """Freshness-resolve a bulk snapshot reply into per-symbol context.

        Shared by the morning Tech pass and the intraday opportunity scan,
        because both hand the SAME payload to the SAME seat and only one of
        them used to check it (docs/WORK.md item 120). Returns
        `(context, missing, stale, rescued)`.

        `context[sym]` is either `{"live_unavailable": reason}` or the raw
        snapshot decorated with `live_price` / `live_price_source` /
        `live_price_at` / `live_price_description`, with the `session_*`
        block blanked when the daily bar in it belongs to a prior session.
        """
        from src.data.live_price import (
            NO_PRICE_AT_ALL,
            SOURCE_LAST_TRADE,
            resolve_live_price,
        )

        out: dict[str, dict] = {}
        missing: list[str] = []
        stale: list[str] = []
        rescued: dict[str, str] = {}
        blanked: list[str] = []
        for sym in symbols:
            snap = snapshots.get(sym) or {}
            resolved = resolve_live_price(snap)
            if resolved.price is None:
                (missing if resolved.unavailable == NO_PRICE_AT_ALL else stale).append(sym)
                out[sym] = {"live_unavailable": resolved.unavailable}
                continue
            # The RAW provider price is deliberately NOT republished here.
            # It sits a key away from the resolved one, still carrying a
            # prior session's number, and the next reader picking the wrong
            # one is this bug returning. What no consumer can reach, no
            # consumer can misread.
            entry = {k: v for k, v in snap.items() if k not in ("last_price", "minute_close")}
            entry["live_price"] = resolved.price
            entry["live_price_source"] = resolved.source
            entry["live_price_at"] = resolved.as_of
            entry["live_price_description"] = resolved.describe()
            if not resolved.session_bar_is_today:
                # The daily bar in this payload belongs to a PRIOR session.
                # Blank it rather than let a caller render yesterday's
                # open/high/low/volume under a "today" heading.
                blanked.append(sym)
                for field in ("session_open", "session_close", "session_high", "session_low", "session_volume"):
                    entry[field] = None
            if resolved.source != SOURCE_LAST_TRADE:
                rescued[sym] = resolved.source
            out[sym] = entry
        # Blanking every priced name at once is the signature of the one
        # assumption in `resolve_live_price` that has never been checked
        # against a live call: that a daily bar's timestamp carries the
        # session's ET date. If that is wrong this fires on day one instead
        # of the session range vanishing silently.
        priced = len(symbols) - len(missing) - len(stale)
        if blanked and priced and len(blanked) == priced:
            logger.error(
                "live session context: EVERY priced symbol (%d) had a daily "
                "bar dated to a prior session. One name is ordinary; all of "
                "them means the daily-bar timestamp convention is not what "
                "`src/data/live_price.py` assumes — check it before trusting "
                "any session range",
                len(blanked),
            )
        elif blanked:
            logger.info(
                "live session context: %d symbol(s) carried a PRIOR session's "
                "daily bar; their session range is blanked rather than shown "
                "as today's (item 120): %s",
                len(blanked),
                blanked[:10],
            )
        return out, missing, stale, rescued
