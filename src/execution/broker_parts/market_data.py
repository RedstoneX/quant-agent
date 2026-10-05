"""Market-data reads, lifted verbatim from src/execution/broker.py.

Fourth (final) broker instalment. `LivePrice`, `_BROKER_HTTP_TIMEOUT` and
`_install_http_timeout` moved here as-is and are re-exported by
`src.execution.broker`. `MarketData` holds the former `AlpacaBroker` read methods.
"""
from __future__ import annotations

from dataclasses import dataclass
from src.execution.broker_parts.stop_place import _alpaca_symbol
from src.execution.broker_parts.stop_place import _internal_symbol
import logging

from src.execution.broker_parts.trade_stream import _OnState
from src.sentinel.counted import record_swallowed

# Same log channel as before the move: operators and tests filter on the
# broker's logger name, and the move must not change what they see.
logger = logging.getLogger("src.execution.broker")

# Default HTTP timeout for ALL Alpaca SDK calls (connect, read).
# Without this, a stalled TCP connection to the broker can hang the process
# for hours under launchd — observed 2026-04-17 when the evening job sat for
# 13+ hours at the very first broker call.
_BROKER_HTTP_TIMEOUT = 30.0


@dataclass(frozen=True)
class LivePrice:
    """A price WITH the provenance needed to decide whether to trust it.

    `get_latest_price` answers "what is it worth" but throws away how it
    knew: a real trade print, or a quote the tape has not confirmed. It also
    never looked at WHEN the trade happened, so a thinly-traded name that
    last printed yesterday, or any read outside market hours, came back
    looking exactly like a live price. Callers that place or move real orders
    need the difference; the reporting callers do not, which is why
    `get_latest_price` keeps its old shape and this is additive.

    `source` is one of `last_trade`, `quote_mid`, `quote_ask`, `quote_bid`.

    Two freshness answers, because two kinds of caller need different
    strictness and collapsing them into one flag would either block ordinary
    trading or wave through yesterday's price:
      - `is_today` — the provider stamped this value with the current ET
        date, whether it is a trade or a quote. A live quote mid-session is
        a legitimate fill reference; yesterday's anything is not.
      - `is_today_print` — additionally a REAL trade print. Only this proves
        the tape actually traded there, which is what deciding where a stop
        belongs requires.
    An unstamped or naive timestamp fails visible rather than passing as
    live — the same rule `src.trading_calendar.live_price_is_today` already
    applies to research snapshots.
    """

    price: float
    source: str
    trade_at: object | None
    is_today: bool
    is_today_print: bool


def _install_http_timeout(client, timeout: float = _BROKER_HTTP_TIMEOUT) -> None:
    """Inject a default timeout on an Alpaca SDK client's underlying requests.Session.

    The SDK (alpaca-py 0.43.2) uses a requests.Session with no default timeout; each
    call goes through RESTClient._one_request which just forwards opts. This patches
    session.request to set timeout=30s if the caller didn't specify one.
    """
    session = getattr(client, "_session", None)
    if session is None or getattr(session, "_quant_timeout_patched", False):
        return
    original_request = session.request

    def _request_with_timeout(method, url, **kwargs):
        kwargs.setdefault("timeout", timeout)
        return original_request(method, url, **kwargs)

    session.request = _request_with_timeout
    session._quant_timeout_patched = True


class MarketData:
    """Market-data reads (movers, bars, quotes, snapshots), lifted verbatim from
    `AlpacaBroker`. Built per call by the broker's thin shims, so `_data_client`
    -- created lazily, after construction -- is read and written on the broker
    (`state`) itself."""

    _data_client = _OnState()
    _screener_client = _OnState()

    def __init__(
        self, *,
        state,
        api_key,
        secret_key,
        closed_bars_cache,
        closed_bars_cache_lock,
        get_latest_price_stamped=None,
        _extract_symbol_payload=None,
    ):
        self._state = state
        self.api_key = api_key
        self.secret_key = secret_key
        self._closed_bars_cache = closed_bars_cache
        self._closed_bars_cache_lock = closed_bars_cache_lock
        if get_latest_price_stamped is not None:
            self.get_latest_price_stamped = get_latest_price_stamped
        if _extract_symbol_payload is not None:
            self._extract_symbol_payload = _extract_symbol_payload


    def get_top_movers(self, n: int = 15) -> list[dict]:
        """Return today's top-`n` gainers from Alpaca's screener.

        Output shape: ``[{"symbol": str, "percent_change": float, "price": float}, ...]``,
        sorted by `percent_change` descending as Alpaca returns them.
        Returns `[]` on any failure (SDK error, auth issue, empty response) —
        the missed-opportunity digest falls back to universe-only when the
        top-movers signal is unavailable, so a degraded screener must never
        crash an evening run. Caller treats [] as "no top-mover augmentation".
        """
        if n <= 0:
            return []
        try:
            # Lazy import + lazy-construct so the extra SDK client is only
            # instantiated the first time evening actually runs a digest.
            from alpaca.data.historical.screener import ScreenerClient
            from alpaca.data.requests import MarketMoversRequest
        except ImportError as exc:
            logger.warning("get_top_movers: screener SDK unavailable: %s", exc)
            return []

        if not hasattr(self, "_screener_client") or self._screener_client is None:
            try:
                self._screener_client = ScreenerClient(
                    api_key=self.api_key, secret_key=self.secret_key,
                )
                _install_http_timeout(self._screener_client)
            except Exception as exc:
                record_swallowed("broker.screener_client_init", exc, log=logger)
                self._screener_client = None
                return []

        try:
            movers = self._screener_client.get_market_movers(
                MarketMoversRequest(top=n)
            )
        except Exception as exc:
            record_swallowed("broker.get_top_movers", exc, log=logger)
            return []

        gainers = getattr(movers, "gainers", None) or []
        out: list[dict] = []
        # Suffix filter — Alpaca's screener returns warrants (.WS, .WSA,
        # .WSB), units (.U, .UN), and rights (.RT) alongside common stock.
        # None of these are tradable as equities in our system, and
        # yfinance 404s on them later — flooding logs with errors. Drop
        # them at the boundary instead. Class shares (e.g. BRK.B) keep
        # the dot but are legitimate; the universe uses the dash form
        # (BRK-B), so any .A/.B from the screener would also be skipped
        # if we filtered too aggressively. So we only filter the
        # non-equity-instrument suffixes explicitly.
        _NON_EQUITY_SUFFIXES = (".WS", ".WSA", ".WSB", ".U", ".UN", ".RT")
        for m in gainers:
            sym = getattr(m, "symbol", None)
            if not sym:
                continue
            alpaca_sym_upper = str(sym).upper()
            if alpaca_sym_upper.endswith(_NON_EQUITY_SUFFIXES):
                continue
            sym_upper = _internal_symbol(alpaca_sym_upper)
            try:
                out.append({
                    "symbol": sym_upper,
                    "percent_change": float(getattr(m, "percent_change", 0) or 0),
                    "price": float(getattr(m, "price", 0) or 0),
                })
            except (TypeError, ValueError):
                continue
            if len(out) >= n:
                break
        return out

    def get_bars(self, symbol: str, lookback_days: int = 120) -> list:
        """Fetch daily OHLCV bars from Alpaca as a list[OHLCV].

        Used by MarketDataProvider as a fallback when yfinance returns empty.
        Same shape as MarketDataProvider.get_ohlcv so the caller is oblivious
        to which source answered. Returns [] on any error.
        """
        from datetime import timedelta as _td
        from src.models import OHLCV
        from src.util.time import et_today

        try:
            if self._data_client is None:
                from alpaca.data.historical.stock import StockHistoricalDataClient
                self._data_client = StockHistoricalDataClient(
                    self.api_key, self.secret_key
                )
                _install_http_timeout(self._data_client)

            from alpaca.data.requests import StockBarsRequest
            from alpaca.data.timeframe import TimeFrame

            end = et_today()
            start = end - _td(days=lookback_days)
            alpaca_symbol = _alpaca_symbol(symbol)

            def _fetch(range_start, range_end) -> list[OHLCV]:
                req = StockBarsRequest(
                    symbol_or_symbols=alpaca_symbol,
                    timeframe=TimeFrame.Day,
                    start=range_start,
                    end=range_end,
                )
                raw = self._data_client.get_stock_bars(req)
                # SDK returns a BarSet-like object with .data = {symbol: [Bar, ...]}
                bars_list = None
                if hasattr(raw, "data") and isinstance(raw.data, dict):
                    bars_list = raw.data.get(alpaca_symbol)
                elif isinstance(raw, dict):
                    bars_list = raw.get(alpaca_symbol)
                if not bars_list:
                    return []
                parsed: list[OHLCV] = []
                for b in bars_list:
                    ts = getattr(b, "timestamp", None)
                    d = ts.date() if ts is not None else None
                    if d is None:
                        continue
                    try:
                        parsed.append(OHLCV(
                            date=d,
                            open=float(getattr(b, "open", 0) or 0),
                            high=float(getattr(b, "high", 0) or 0),
                            low=float(getattr(b, "low", 0) or 0),
                            close=float(getattr(b, "close", 0) or 0),
                            volume=int(getattr(b, "volume", 0) or 0),
                        ))
                    except (TypeError, ValueError):
                        continue
                return parsed

            # Caching: only the portion of the range up to (and including)
            # yesterday can possibly be closed/complete daily bars — Alpaca
            # doesn't publish a daily bar for a session that hasn't closed
            # yet, but we still never trust "today" to a cache: today's
            # entry is always fetched fresh, never cached. Keyed so a new
            # calendar day naturally invalidates the historical portion.
            hist_end = end - _td(days=1)
            cache_key = ("daily", alpaca_symbol, start, hist_end)
            with self._closed_bars_cache_lock:
                cached_hist = self._closed_bars_cache.get(cache_key)

            if cached_hist is None:
                # Cache miss: one fetch over the whole range, exactly as
                # before caching existed. Split the result so only the
                # closed (pre-today) portion is stored.
                all_bars = _fetch(start, end)
                cached_hist = [b for b in all_bars if b.date <= hist_end]
                with self._closed_bars_cache_lock:
                    self._closed_bars_cache[cache_key] = cached_hist
                return all_bars

            # Cache hit: reuse the closed history, only refetch today.
            today_bars = _fetch(end, end)
            out = cached_hist + [b for b in today_bars if b.date not in {c.date for c in cached_hist}]
            out.sort(key=lambda b: b.date)
            return out
        except Exception as e:
            record_swallowed("broker.get_bars", e, log=logger, symbol=symbol)
            return []

    def get_intraday_chart_bars(
        self, symbol: str, timeframe: str, lookback_days: int
    ) -> list[dict]:
        """Fetch read-only intraday OHLCV bars for Mission Control.

        This deliberately does not participate in trading decisions or
        execution. It uses the same Alpaca historical-data client as
        ``get_bars`` but preserves each bar's timestamp so Lightweight
        Charts can render 5m/15m/1h candles and align execution markers.
        Returns [] on any failure, matching the broker's other market-data
        degradation contracts.
        """
        from datetime import datetime as _dt, timedelta as _td, timezone as _tz
        from src.util.time import ET

        try:
            if self._data_client is None:
                from alpaca.data.historical.stock import StockHistoricalDataClient
                self._data_client = StockHistoricalDataClient(self.api_key, self.secret_key)
                _install_http_timeout(self._data_client)

            from alpaca.data.enums import DataFeed
            from alpaca.data.requests import StockBarsRequest
            from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

            timeframe_value = {
                "5m": TimeFrame(5, TimeFrameUnit.Minute),
                "15m": TimeFrame(15, TimeFrameUnit.Minute),
                "1h": TimeFrame.Hour,
            }.get(timeframe)
            if timeframe_value is None:
                return []

            now = _dt.now(_tz.utc)
            start = now - _td(days=lookback_days)
            alpaca_symbol = _alpaca_symbol(symbol)

            def _fetch(range_start, range_end) -> list[dict]:
                req = StockBarsRequest(
                    symbol_or_symbols=alpaca_symbol,
                    timeframe=timeframe_value,
                    start=range_start,
                    end=range_end,
                    # This account's market-data plan is entitled to IEX, not
                    # SIP. Leaving feed unset resolves to SIP server-side for
                    # sub-daily bars and comes back with zero bars for every
                    # symbol/range — silently, since Alpaca doesn't error, it
                    # just returns nothing. Daily bars (get_bars, above) aren't
                    # feed-gated the same way, which is why only this intraday
                    # path needs it.
                    feed=DataFeed.IEX,
                )
                raw = self._data_client.get_stock_bars(req)
                if hasattr(raw, "data") and isinstance(raw.data, dict):
                    bars_list = raw.data.get(alpaca_symbol)
                elif isinstance(raw, dict):
                    bars_list = raw.get(alpaca_symbol)
                else:
                    bars_list = None
                if not bars_list:
                    return []

                parsed: list[dict] = []
                for bar in bars_list:
                    ts = getattr(bar, "timestamp", None)
                    if ts is None:
                        continue
                    try:
                        parsed.append(
                            {
                                "date": ts.astimezone(ET).date().isoformat(),
                                "timestamp": ts.isoformat(),
                                "open": float(getattr(bar, "open", 0) or 0),
                                "high": float(getattr(bar, "high", 0) or 0),
                                "low": float(getattr(bar, "low", 0) or 0),
                                "close": float(getattr(bar, "close", 0) or 0),
                                "volume": int(getattr(bar, "volume", 0) or 0),
                            }
                        )
                    except (TypeError, ValueError):
                        continue
                return parsed

            # Caching: only prior, fully-closed trading days are cacheable.
            # Today (including its still-forming candle) is always fetched
            # fresh, never cached. The historical portion is cached keyed
            # by the (rounded-to-day) start and today's date, so a new
            # calendar day naturally invalidates it. The start is rounded
            # down to ET midnight of its day (a superset of the exact
            # `start` instant) purely so repeated calls with the same
            # lookback_days share one cache key — outside trading hours
            # Alpaca simply returns nothing extra, so this never fabricates
            # data, only makes the cache key stable.
            today_et = now.astimezone(ET).date()
            today_midnight_et = _dt.combine(today_et, _dt.min.time(), tzinfo=ET)
            start_day_et = start.astimezone(ET).date()
            cache_key = ("intraday", alpaca_symbol, timeframe, start_day_et, today_et)

            with self._closed_bars_cache_lock:
                cached_hist = self._closed_bars_cache.get(cache_key)

            if cached_hist is None:
                # Cache miss: one fetch over the whole range, exactly as
                # before caching existed. Split the result so only the
                # portion from before today is stored.
                all_bars = _fetch(start, now)
                cached_hist = [b for b in all_bars if b["date"] < today_et.isoformat()]
                with self._closed_bars_cache_lock:
                    self._closed_bars_cache[cache_key] = cached_hist
                return all_bars

            # Cache hit: reuse the closed history, only refetch today.
            today_bars = _fetch(today_midnight_et, now)
            out = cached_hist + today_bars
            out.sort(key=lambda b: b["timestamp"])
            return out
        except Exception as exc:
            logger.warning(
                "broker.get_intraday_chart_bars failed for %s/%s: %s",
                symbol, timeframe, exc,
            )
            return []

    def get_latest_price_stamped(self, symbol: str) -> "LivePrice | None":
        """Latest price for `symbol` with its source and freshness attached.

        Order of preference is unchanged from `get_latest_price`: a real
        trade print first, then the quote midpoint, then a single side. What
        is new is that the answer says which one it was and whether the trade
        print is from today's ET date, so a caller about to place or move an
        order can refuse a stale number instead of silently acting on it.
        """
        try:
            if self._data_client is None:
                from alpaca.data.historical.stock import StockHistoricalDataClient

                self._data_client = StockHistoricalDataClient(self.api_key, self.secret_key)
                _install_http_timeout(self._data_client)

            from alpaca.data.requests import StockLatestQuoteRequest, StockLatestTradeRequest

            alpaca_symbol = _alpaca_symbol(symbol)

            trade_data = self._data_client.get_stock_latest_trade(
                StockLatestTradeRequest(symbol_or_symbols=alpaca_symbol)
            )
            trade = self._extract_symbol_payload(trade_data, alpaca_symbol)
            trade_price = float(getattr(trade, "price", 0) or 0)
            if trade_price > 0:
                trade_at = getattr(trade, "timestamp", None)
                from src.trading_calendar import live_price_is_today

                fresh = bool(live_price_is_today(trade_at))
                return LivePrice(
                    price=trade_price, source="last_trade", trade_at=trade_at,
                    is_today=fresh, is_today_print=fresh,
                )

            quote_data = self._data_client.get_stock_latest_quote(
                StockLatestQuoteRequest(symbol_or_symbols=alpaca_symbol)
            )
            quote = self._extract_symbol_payload(quote_data, alpaca_symbol)
            ask_price = float(getattr(quote, "ask_price", 0) or 0)
            bid_price = float(getattr(quote, "bid_price", 0) or 0)
            quote_at = getattr(quote, "timestamp", None)
            from src.trading_calendar import live_price_is_today

            quote_today = bool(live_price_is_today(quote_at))
            if ask_price > 0 and bid_price > 0:
                return LivePrice(
                    price=(ask_price + bid_price) / 2, source="quote_mid",
                    trade_at=quote_at, is_today=quote_today, is_today_print=False,
                )
            if ask_price > 0:
                return LivePrice(
                    price=ask_price, source="quote_ask", trade_at=quote_at,
                    is_today=quote_today, is_today_print=False,
                )
            if bid_price > 0:
                return LivePrice(
                    price=bid_price, source="quote_bid", trade_at=quote_at,
                    is_today=quote_today, is_today_print=False,
                )
        except Exception as exc:
            logger.warning("Failed to fetch latest price for %s: %s", symbol, exc)

        return None

    def get_latest_price(self, symbol: str) -> float | None:
        """Latest price as a bare number — unchanged behaviour.

        Reporting and grading callers ("how far has this moved since we sold
        it") do not care where the number came from, and they already degrade
        to a last close when it is missing. They keep this. Anything that
        places or moves an order should call `get_latest_price_stamped` and
        check `is_today_print`.
        """
        stamped = self.get_latest_price_stamped(symbol)
        return stamped.price if stamped is not None else None

    def get_latest_quote(self, symbol: str) -> dict[str, float | None]:
        """Return the current bid/ask without inventing a side of the book.

        Execution uses the ask to construct a bounded marketable BUY limit
        and the bid to construct the mirrored SHORT floor. Missing or failed
        quote data returns explicit ``None`` fields so the caller can retain
        its existing last-trade behavior without guessing.
        """
        out = {"bid_price": None, "ask_price": None}
        try:
            if self._data_client is None:
                from alpaca.data.historical.stock import StockHistoricalDataClient

                self._data_client = StockHistoricalDataClient(self.api_key, self.secret_key)
                _install_http_timeout(self._data_client)

            from alpaca.data.requests import StockLatestQuoteRequest

            alpaca_symbol = _alpaca_symbol(symbol)
            quote_data = self._data_client.get_stock_latest_quote(
                StockLatestQuoteRequest(symbol_or_symbols=alpaca_symbol)
            )
            quote = self._extract_symbol_payload(quote_data, alpaca_symbol)
            for field in out:
                try:
                    value = float(getattr(quote, field, 0) or 0)
                except (TypeError, ValueError):
                    value = 0.0
                out[field] = value if value > 0 else None
        except Exception as exc:
            logger.warning("Failed to fetch latest quote for %s: %s", symbol, exc)
        return out

    def get_intraday_snapshots(self, symbols: list[str]) -> dict[str, dict]:
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
        if self._data_client is None:
            try:
                from alpaca.data.historical.stock import StockHistoricalDataClient

                self._data_client = StockHistoricalDataClient(self.api_key, self.secret_key)
                _install_http_timeout(self._data_client)
            except Exception as exc:
                record_swallowed("broker.intraday_snapshots_client_init", exc, log=logger)
                return {}

        from alpaca.data.requests import StockSnapshotRequest

        requested = [(symbol, _alpaca_symbol(symbol)) for symbol in symbols]
        alpaca_symbols = list(dict.fromkeys(mapped for _, mapped in requested))
        successful_batches = 0

        def _fetch_batch(batch: list[str]) -> dict:
            """Bulk first; isolate a bad symbol only when Alpaca rejects a batch."""
            nonlocal successful_batches
            if not batch:
                return {}
            try:
                result = self._data_client.get_stock_snapshot(
                    StockSnapshotRequest(symbol_or_symbols=batch)
                )
                successful_batches += 1
                return result if isinstance(result, dict) else {}
            except Exception as exc:
                status_code = getattr(exc, "status_code", None)
                symbol_error = (
                    status_code in (400, 404, 422)
                    or "invalid symbol" in str(exc).lower()
                )
                if len(batch) == 1:
                    logger.warning(
                        "get_intraday_snapshots: symbol %s unavailable: %s",
                        batch[0], exc,
                    )
                    return {}
                if not symbol_error:
                    logger.warning(
                        "get_intraday_snapshots: bulk snapshot fetch failed "
                        "for %d symbols: %s",
                        len(batch), exc,
                    )
                    return {}
                midpoint = len(batch) // 2
                logger.warning(
                    "get_intraday_snapshots: batch of %d rejected; isolating bad symbol(s): %s",
                    len(batch), exc,
                )
                return {
                    **_fetch_batch(batch[:midpoint]),
                    **_fetch_batch(batch[midpoint:]),
                }

        snapshots = _fetch_batch(alpaca_symbols)
        if successful_batches == 0:
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

    @staticmethod
    def _extract_symbol_payload(payload, symbol: str):
        if isinstance(payload, dict):
            return payload.get(symbol)
        try:
            return payload[symbol]
        except Exception:
            return getattr(payload, symbol, None)
