"""The body of `MarketData.get_intraday_snapshots`, lifted verbatim from
src/execution/broker_parts/market_data.py.

The data client and the snapshot request model are built in market_data.py and
passed in, so this module imports no provider client: the replay seam stays
where the client is built (board item 202)."""

from __future__ import annotations

import logging

from src.execution.broker_parts.stop_place import _alpaca_symbol
from src.sentinel.counted import record_swallowed

logger = logging.getLogger("src.execution.broker")


def _fetch_batch(data_client, snapshot_request, batch: list[str], ok_batches: list) -> dict:
    """Bulk first; isolate a bad symbol only when Alpaca rejects a batch."""
    if not batch:
        return {}
    try:
        result = data_client.get_stock_snapshot(snapshot_request(symbol_or_symbols=batch))
        ok_batches[0] += 1
        return result if isinstance(result, dict) else {}
    except Exception as exc:
        status_code = getattr(exc, "status_code", None)
        symbol_error = status_code in (400, 404, 422) or "invalid symbol" in str(exc).lower()
        if len(batch) == 1:
            record_swallowed("broker.intraday_snapshots_single", exc, log=logger, symbol=batch[0])
            return {}
        if not symbol_error:
            record_swallowed("broker.intraday_snapshots_bulk", exc, log=logger, symbols=len(batch))
            return {}
        midpoint = len(batch) // 2
        logger.warning(
            "get_intraday_snapshots: batch of %d rejected; isolating bad symbol(s): %s",
            len(batch),
            exc,
        )
        return {
            **_fetch_batch(data_client, snapshot_request, batch[:midpoint], ok_batches),
            **_fetch_batch(data_client, snapshot_request, batch[midpoint:], ok_batches),
        }


def snapshots_from_client(data_client, symbols: list[str], snapshot_request) -> dict[str, dict]:
    """Bulk current-session move data for the intraday opportunity scan.

    One Alpaca snapshot call for the whole symbol list (not one call
    per symbol — the same `symbol_or_symbols` bulk parameter
    `get_latest_price` already uses for a single symbol) — cheap
    enough to run every intra_check tick, unlike re-fetching daily
    bars for the whole universe.

    Returns, for every requested symbol, a dict of the current-session
    facts needed both to detect a material move and to give Tech
    truthful intraday evidence:

        {"last_price", "last_trade_at", "prev_close",
         "session_bar_at", "minute_close", "minute_bar_at",
         "session_open", "session_close", "session_high",
         "session_low", "session_volume"}

    `last_trade_at` is the raw provider datetime (or None) for the
    latest trade's own `timestamp` field — used by `broker_reads.py`
    to tell a stale last_price from a live one (docs/WORK.md item 15).

    The `session_*` fields come from Alpaca's `daily_bar`, which during
    the session is an INCOMPLETE, still-forming bar — callers must
    present it as such and must never append it to a series of completed
    daily bars. **It is not guaranteed to be TODAY's**: for a name that
    has not printed today, Alpaca returns the previous session's daily
    bar in that slot. `session_bar_at` is that bar's own opening
    timestamp so a caller can check the date before calling it "today"
    (docs/WORK.md item 120). `minute_close` / `minute_bar_at` are the
    snapshot's 1-minute bar and carry the same caveat.

    NONE of these fields is freshness-checked here. Use
    `src.data.live_price.resolve_live_price` to turn this payload into a
    price that is known to come from today — this method deliberately
    reports what the provider said, and the judgement about what counts
    as today lives in one place.

    Any field is `None` when unavailable. Never raises — broker/network
    failure degrades to an empty dict (caller treats that as "no signal
    this tick", not a crash).
    """
    if not symbols:
        return {}
    requested = [(symbol, _alpaca_symbol(symbol)) for symbol in symbols]
    alpaca_symbols = list(dict.fromkeys(mapped for _, mapped in requested))

    ok_batches = [0]
    snapshots = _fetch_batch(data_client, snapshot_request, alpaca_symbols, ok_batches)
    if ok_batches[0] == 0:
        return {}

    def _num(obj, attr):
        if obj is None:
            return None
        try:
            v = float(getattr(obj, attr, 0) or 0)
        except (TypeError, ValueError):
            return None
        return v if v > 0 else None

    out: dict[str, dict] = {}
    for symbol, alpaca_symbol in requested:
        snap = snapshots.get(alpaca_symbol) if isinstance(snapshots, dict) else None
        trade = getattr(snap, "latest_trade", None) if snap is not None else None
        prev_bar = getattr(snap, "previous_daily_bar", None) if snap is not None else None
        # TODAY's still-forming bar. Deliberately kept in its own
        # `session_*` namespace so no caller can mistake it for a
        # completed daily bar (2026-08-19 intraday-evidence fix).
        today_bar = getattr(snap, "daily_bar", None) if snap is not None else None
        # Alpaca's Trade model DOES carry its own `timestamp` field
        # (verified against the installed SDK, 2026-09-13) — this is
        # the provider's own market timestamp for the last print, not
        # a guess. Kept as the raw datetime (or None); broker_reads.py
        # serializes it and derives freshness from it.
        last_trade_at = getattr(trade, "timestamp", None) if trade is not None else None
        # board item 120: the `session_*` block was returned with no way
        # to tell WHICH session it belongs to. Alpaca's snapshot carries
        # the previous session's daily bar in `daily_bar` for a name that
        # has not printed today, so a caller rendering "CURRENT SESSION
        # (TODAY)" off these fields could be showing yesterday. `Bar
        # .timestamp` is a required field on the installed SDK's model
        # (`alpaca/data/models/bars.py`, verified 2026-09-20) and is the
        # bar's OPENING timestamp, so its ET date is the session date.
        session_bar_at = getattr(today_bar, "timestamp", None) if today_bar is not None else None
        # The 1-minute bar is an aggregation of REAL PRINTS on the same
        # entitled venue — not a quote. It is the finest-grained today
        # print the snapshot carries, and it exists for names whose
        # `latest_trade` is still yesterday's (item 120, 2026-09-17).
        minute_bar = getattr(snap, "minute_bar", None) if snap is not None else None
        minute_bar_at = getattr(minute_bar, "timestamp", None) if minute_bar is not None else None
        out[symbol] = {
            "last_price": _num(trade, "price"),
            "last_trade_at": last_trade_at,
            "prev_close": _num(prev_bar, "close"),
            "session_bar_at": session_bar_at,
            "minute_close": _num(minute_bar, "close"),
            "minute_bar_at": minute_bar_at,
            "session_open": _num(today_bar, "open"),
            "session_close": _num(today_bar, "close"),
            "session_high": _num(today_bar, "high"),
            "session_low": _num(today_bar, "low"),
            "session_volume": _num(today_bar, "volume"),
        }
    return out
