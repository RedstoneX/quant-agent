import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout
from datetime import date

import yfinance as yf
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import (
    MarketOrderRequest, LimitOrderRequest, StopLimitOrderRequest,
    TakeProfitRequest, StopLossRequest, ReplaceOrderRequest,
)
from alpaca.trading.enums import OrderSide, TimeInForce, OrderClass, QueryOrderStatus

from src.models import Position, _ALLOWED_SECTORS, _SECTOR_ALIASES

logger = logging.getLogger(__name__)

# Index ETFs that have no single sector — bucket them as "Broad".
_INDEX_ETFS = {"SPY", "QQQ", "IWM", "DIA", "VTI", "VOO", "IVV"}

# Sector / thematic ETFs → their canonical sector bucket.
#
# WHY (2026-07-16 audit): yfinance's `.info` carries no `sector` key for ETFs,
# so _get_sector fell through to "Unknown" for every one of them. Two silent
# failures followed: (1) `max_sector_pct` is gated on `new_sector != "Unknown"`
# (risk/rules.py), so a BUY of XLV/SMH/... skipped the sector cap ENTIRELY;
# (2) a held ETF carries sector="Unknown", so it contributed $0 to the sector
# bucket of a same-sector single name — a book that is 30% XLV would let an
# LLY BUY through as if Healthcare exposure were zero. Both directions of the
# cap were dead for these symbols despite the universe being ~20% ETFs.
#
# Deterministic table, consulted BEFORE the network fetch: an ETF's sector is
# a fact about the product, not something to rediscover per process.
_ETF_SECTORS = {
    # SPDR sector suite
    "XLF": "Financial Services", "XLE": "Energy", "XLV": "Healthcare",
    "XLI": "Industrials", "XLP": "Consumer Defensive", "XLY": "Consumer Cyclical",
    "XLU": "Utilities", "XLRE": "Real Estate", "XLB": "Basic Materials",
    "XLK": "Technology", "XLC": "Communication Services",
    # Semiconductor / AI thematics
    "SMH": "Technology", "SOXX": "Technology", "DRAM": "Technology",
    "CHPX": "Technology",
    # Inverse / leveraged index ETFs track a BROAD index — they have no sector
    # of their own. (Their leverage is handled separately by the signed/gross
    # multipliers in risk/rules.py.)
    "SH": "Broad", "SDS": "Broad", "PSQ": "Broad", "SQQQ": "Broad",
}

# Default HTTP timeout for ALL Alpaca SDK calls (connect, read).
# Without this, a stalled TCP connection to the broker can hang the process
# for hours under launchd — observed 2026-04-17 when the evening job sat for
# 13+ hours at the very first broker call.
_BROKER_HTTP_TIMEOUT = 30.0
_SECTOR_LOOKUP_TIMEOUT_S = 10  # per-symbol ceiling on yfinance .info hang in _get_sector

# A marketable limit on a liquid US equity should normally fill immediately.
# This is deliberately longer than the old 15-second guard (which production
# evidence showed canceling ordinary accepted entries) but bounded well below
# a stale DAY order. Later entries in a submission burst have already rested
# while earlier entries are finalized, so 30 seconds is a conservative floor,
# not a blind per-order sleep added to every order.
_ENTRY_FILL_TIMEOUT_S = 30.0

# Spec §11.1, guard 1: "stop placement retries immediately and hard on
# failure". IMMEDIATELY — at the point of failure, inside the same call,
# not queued for the next sweep. The position is already open by the time
# this runs; a retry that waits for the 30-minute reconcile is exactly the
# indefinite gap the guard exists to prevent.
#
# THREE ATTEMPTS, ~2 SECONDS TOTAL, and both halves of that are deliberate:
#
#   * Three, because every failure worth retrying is transient — a 429 rate
#     limit, a 5xx, a dropped connection, an eventual-consistency blip
#     between the fill and the order being placeable. Those clear in under a
#     second. A failure that survives three attempts is a REJECTION (a bad
#     price, an unsupported qty, a closed venue), and retrying a rejection
#     forever just delays the owner alert that is the real remedy.
#   * ~2 seconds, because the owner's own standard for this feature is that
#     "the gap is brief upon entry". A retry loop long enough to matter
#     would itself become the exposure it was added to close. Escalating to
#     a human inside two seconds beats a fourth doomed attempt.
_STOP_PLACEMENT_MAX_ATTEMPTS = 3
_STOP_PLACEMENT_BACKOFF_S = (0.5, 1.5)


def _alpaca_symbol(symbol: str) -> str:
    """Translate the universe's yfinance class-share spelling at Alpaca's edge.

    BRK-B/BF-B are valid yfinance symbols while Alpaca expects BRK.B/BF.B.
    Only the terminal one-letter class suffix is translated; ordinary hyphenated
    symbols are left untouched instead of applying a broad, unsafe replacement.
    """
    value = str(symbol).strip().upper()
    return re.sub(r"^([A-Z]+)-([A-Z])$", r"\1.\2", value)


def _internal_symbol(symbol: str) -> str:
    """Map Alpaca class-share spelling back to QAMC/yfinance canonical form."""
    value = str(symbol).strip().upper()
    return re.sub(r"^([A-Z]+)\.([A-Z])$", r"\1-\2", value)


def _quantize_price(price: float | None) -> float | None:
    """Round to Alpaca's minimum tick size: $0.01 for stocks ≥ $1, $0.0001 below.

    The quote-midpoint in `get_latest_price` can produce sub-penny values like
    $106.515; submitting that raw triggers Alpaca error 42210000 and the order
    is rejected. Observed 2026-04-17 morning: UPS BUY @ $106.515 rejected.

    NaN/Inf handling: NaN comparisons all return False, so the original
    `price <= 0` guard fell through to `round(nan, ...)` = nan. The NaN
    then propagated all the way to Alpaca's submit_order, which silently
    broker-rejects the order and corrupts audit logs. Treat NaN/Inf as
    None (no quotable price) — callers' existing
    `price is not None and price > 0` checks then skip the order or
    fall back to market. Zero/negative values are preserved unchanged
    (pre-existing semantics: caller decides what to do with them).
    """
    if price is None:
        return None
    import math as _math
    if not _math.isfinite(price):
        return None
    if price <= 0:
        return price
    return round(price, 2 if price >= 1.0 else 4)


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

# Cache sector lookups to avoid repeated API calls
_sector_cache: dict[str, str] = {}
_sector_lock = threading.Lock()


def _canonicalize_sector(raw: str | None) -> str:
    """Normalize yfinance / LLM sector strings to the 12-value canonical enum.

    Returns "Unknown" for anything that can't be mapped — callers must decide
    whether to skip or fall back. The MacroAnalysis pydantic model uses the
    same alias table to self-heal LLM output.
    """
    if not raw:
        return "Unknown"
    s = str(raw).strip()
    if s in _ALLOWED_SECTORS:
        return s
    canon = _SECTOR_ALIASES.get(s.lower())
    if canon in _ALLOWED_SECTORS:
        return canon
    return "Unknown"


def _get_sector(symbol: str) -> str:
    """Look up sector for a symbol using yfinance. Thread-safe, cached per process.

    Output is canonicalized to the 12-value MacroSectorGuidance enum (or "Unknown"
    for un-classifiable names), so macro sector_guidance and position.sector share
    a namespace.

    Caching policy: only KNOWN sectors are cached. "Unknown" is returned but
    NOT cached so a transient yfinance outage doesn't permanently exempt the
    symbol from RiskRuleEngine.max_sector_pct (the engine skips the cap when
    sector=="Unknown"). Codex r11 P1: a one-shot lookup miss in --mode live
    used to leave the symbol cap-exempt until process restart. Re-querying
    yfinance on every call for an unresolved symbol is a small overhead vs.
    silently disabling a hard risk rule.
    """
    # _sector_lock guards ONLY the cache dict — never a network call.
    # audit F3: the old code held _sector_lock for the entire function
    # including the yfinance fetch, so one stuck symbol froze every
    # sector lookup process-wide (risk/position sizing all serialize
    # through _get_sector).
    with _sector_lock:
        cached = _sector_cache.get(symbol)
    if cached is not None:
        return cached
    if symbol.upper() in _INDEX_ETFS:
        with _sector_lock:
            _sector_cache[symbol] = "Broad"
        return "Broad"
    # Sector/thematic ETFs: yfinance .info has no `sector` for ETFs, so
    # without this table they resolve to "Unknown" and silently switch the
    # sector cap OFF (see _ETF_SECTORS). Deterministic, offline, before the fetch.
    etf_sector = _ETF_SECTORS.get(symbol.upper())
    if etf_sector is not None:
        with _sector_lock:
            _sector_cache[symbol] = etf_sector
        return etf_sector

    def _fetch():
        try:
            return yf.Ticker(symbol).info or {}
        except Exception:
            return {}

    # yfinance .info has no hard upper bound — a stuck socket can hang
    # for far longer than _SECTOR_LOOKUP_TIMEOUT_S. audit F3: do NOT use
    # `with ThreadPoolExecutor(...)`; its __exit__ calls
    # shutdown(wait=True), which re-blocks on the hung worker after the
    # .result() timeout fires, making the ceiling illusory.
    # shutdown(wait=False, cancel_futures=True) returns immediately. A
    # still-running fetch leaks one worker thread — accepted vs. the
    # prior behaviour of stalling the whole session.
    ex = ThreadPoolExecutor(max_workers=1)
    try:
        info = ex.submit(_fetch).result(timeout=_SECTOR_LOOKUP_TIMEOUT_S)
    except FuturesTimeout:
        logger.warning("yfinance sector lookup timed out for %s", symbol)
        info = {}
    finally:
        ex.shutdown(wait=False, cancel_futures=True)

    raw = info.get("sector", "") if isinstance(info, dict) else ""
    canonical = _canonicalize_sector(raw)
    if canonical != "Unknown":
        with _sector_lock:
            _sector_cache[symbol] = canonical
    return canonical


#: Entry sides `place_entry_protection` will derive a protective side from.
#: Anything else is refused rather than guessed — see the fail-closed note in
#: `place_entry_protection`. "sell" and "sell_short" both open/extend a short.
_ENTRY_SIDES = frozenset({"buy", "sell", "sell_short"})


class AlpacaBroker:
    def __init__(self, api_key: str, secret_key: str, paper: bool = True):
        self.api_key = api_key
        self.secret_key = secret_key
        self.client = TradingClient(api_key, secret_key, paper=paper)
        _install_http_timeout(self.client)
        self._data_client = None
        # Per-date cache for is_trading_day. Trading-day status is set by
        # the exchange calendar months in advance — invariant within the
        # day — so a per-date dict that grows unbounded over a multi-year
        # process lifetime is still fine (1 entry per calendar day ≈ a
        # few KB / year).
        self._trading_day_cache: dict[date, bool] = {}
        # Stage 3 (shorts, D6). Per-run cache, same shape/lifetime as
        # `_trading_day_cache` above — a symbol's shortable/easy_to_borrow
        # flags don't change intra-session, so one asset-directory lookup
        # per symbol per process is enough.
        self._shortable_cache: dict[str, dict] = {}
        # Spec §11.1. Same shape/lifetime and same reasoning as
        # `_shortable_cache`: `fractionable` is an asset-directory fact that
        # does not change intra-session.
        self._fractionable_cache: dict[str, dict] = {}

    def get_account(self) -> dict:
        acct = self.client.get_account()
        portfolio_value = float(acct.portfolio_value)
        # last_equity = equity at previous trading-day close (Alpaca-provided).
        # Fall back to current portfolio value for brand-new accounts where
        # Alpaca hasn't stamped a prior close yet.
        raw_last = getattr(acct, "last_equity", None)
        last_equity = float(raw_last) if raw_last else portfolio_value
        if last_equity <= 0:
            last_equity = portfolio_value
        # `cash` can include same-day sale proceeds that are not yet
        # settled (T+1 for equities) and therefore not safely spendable on
        # a new BUY without implicitly drawing broker margin — Alpaca does
        # not offer a true cash-account product; every account is a margin
        # account, and accounts >= $2,000 equity get no unsettled-funds
        # allowance. `non_marginable_buying_power` is Alpaca's own settled,
        # non-margin-eligible buying-power figure — the correct "safe to
        # spend right now, no margin" number for a cash-only design (2026-
        # 08-19 SGOV/deployable-liquidity forensic).
        raw_nmbp = getattr(acct, "non_marginable_buying_power", None)
        non_marginable_buying_power = (
            float(raw_nmbp) if raw_nmbp is not None else float(acct.cash)
        )
        return {
            "cash": float(acct.cash),
            "portfolio_value": portfolio_value,
            "last_equity": last_equity,
            "non_marginable_buying_power": non_marginable_buying_power,
        }

    def get_transient_equity_eligibility(self, symbol: str) -> dict:
        """Fail-closed broker eligibility for an out-of-universe candidate.

        This is a read-only asset-directory lookup. It grants no trading
        permission by itself; the Smart Money admission reducer combines it
        with SEC provenance, price/history/liquidity checks, and a per-run cap.
        """
        canonical = _internal_symbol(_alpaca_symbol(symbol))
        alpaca_symbol = _alpaca_symbol(canonical)
        non_equity_suffixes = (".WS", ".WSA", ".WSB", ".U", ".UN", ".RT")
        if alpaca_symbol.endswith(non_equity_suffixes):
            return {"eligible": False, "reason": "unsupported_security_suffix"}
        try:
            asset = self.client.get_asset(alpaca_symbol)
        except Exception as exc:
            logger.warning("asset eligibility lookup failed for %s: %s", canonical, exc)
            return {"eligible": False, "reason": "asset_lookup_failed"}

        def _field(name, default=None):
            if isinstance(asset, dict):
                return asset.get(name, default)
            return getattr(asset, name, default)

        def _enum_text(value) -> str:
            return str(getattr(value, "value", value) or "").strip().lower()

        status = _enum_text(_field("status"))
        asset_class = _enum_text(_field("asset_class", _field("class")))
        exchange = _enum_text(_field("exchange"))
        tradable = bool(_field("tradable", False))
        name = str(_field("name", "") or "").strip()
        name_lower = name.casefold()
        unsupported_name_terms = (
            " exchange traded fund", " etf", "fund shares", "warrant",
            "preferred", "depositary", " american deposit", " unit", " rights",
        )

        reason = None
        if status != "active":
            reason = "asset_not_active"
        elif asset_class not in {"us_equity", "assetclass.us_equity"}:
            reason = "not_us_equity"
        elif not tradable:
            reason = "asset_not_tradable"
        elif exchange not in {
            "nyse", "nasdaq", "amex", "arca", "bats",
            "assetexchange.nyse", "assetexchange.nasdaq",
            "assetexchange.amex", "assetexchange.arca", "assetexchange.bats",
        }:
            reason = "unsupported_exchange"
        elif any(term in name_lower for term in unsupported_name_terms):
            reason = "not_common_stock"

        return {
            "eligible": reason is None,
            "reason": reason or "eligible",
            "symbol": canonical,
            "name": name,
            "exchange": exchange,
        }

    def get_shortability(self, symbol: str) -> dict:
        """D6 (Stage 3): the borrow gate. Alpaca's per-asset `shortable` and
        `easy_to_borrow` flags, cached for the life of this broker instance
        exactly like `get_transient_equity_eligibility` is (a read-only
        asset-directory fact that does not change intra-session).

        A short may open ONLY when BOTH flags are true. This is paper
        trading against IEX data: a hard-to-borrow name fills unrealistically
        in paper and its borrow cost is not modeled anywhere in this system,
        so restricting to easy-to-borrow names is what keeps measured paper
        results transferable to live capital. `reason` distinguishes the two
        ways a short can be refused ("not_shortable" vs "hard_to_borrow") so
        the caller can log which one fired.

        Fails CLOSED: an API error or an unreadable/unknown symbol reports
        shortable=False / easy_to_borrow=False — a short is refused, never
        guessed open.
        """
        canonical = _internal_symbol(_alpaca_symbol(symbol))
        cached = self._shortable_cache.get(canonical)
        if cached is not None:
            return cached

        alpaca_symbol = _alpaca_symbol(canonical)
        try:
            asset = self.client.get_asset(alpaca_symbol)
        except Exception as exc:
            logger.warning("shortability lookup failed for %s: %s", canonical, exc)
            result = {
                "shortable": False, "easy_to_borrow": False,
                "reason": "asset_lookup_failed", "symbol": canonical,
            }
            self._shortable_cache[canonical] = result
            return result

        def _field(name, default=None):
            if isinstance(asset, dict):
                return asset.get(name, default)
            return getattr(asset, name, default)

        shortable = bool(_field("shortable", False))
        easy_to_borrow = bool(_field("easy_to_borrow", False))
        if shortable and easy_to_borrow:
            reason = "eligible"
        elif not shortable and not easy_to_borrow:
            reason = "not_shortable"  # the more specific/common of the two
        elif not shortable:
            reason = "not_shortable"
        else:
            reason = "hard_to_borrow"
        result = {
            "shortable": shortable, "easy_to_borrow": easy_to_borrow,
            "reason": reason, "symbol": canonical,
        }
        self._shortable_cache[canonical] = result
        return result

    def get_fractionability(self, symbol: str) -> dict:
        """Spec §11.1: is this symbol tradeable in fractional quantities?

        Alpaca publishes a per-asset `fractionable` flag in the same
        asset-directory record `get_shortability` reads, and it is cached the
        same way for the same reason — it does not change intra-session.

        **Fails CLOSED, and that is the whole point.** An API error, an
        unknown symbol, an asset record with no `fractionable` field at all —
        every one of those reports `fractionable=False`, and the caller sizes
        in whole shares. Fractional-by-assumption is the failure this guard
        exists to prevent: a fractional order on a non-fractionable name is
        rejected outright by the broker, which turns an approved trade into
        no trade at all and hides the reason in an order-rejection log.

        `reason` distinguishes the three ways the answer can be no, so the
        caller can log which one fired rather than a bare False.
        """
        canonical = _internal_symbol(_alpaca_symbol(symbol))
        cached = self._fractionable_cache.get(canonical)
        if cached is not None:
            return cached

        alpaca_symbol = _alpaca_symbol(canonical)
        try:
            asset = self.client.get_asset(alpaca_symbol)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "fractionability lookup failed for %s: %s — sizing in WHOLE "
                "shares (fail closed)", canonical, exc,
            )
            result = {
                "fractionable": False, "reason": "asset_lookup_failed",
                "symbol": canonical,
            }
            self._fractionable_cache[canonical] = result
            return result

        def _field(name, default=None):
            if isinstance(asset, dict):
                return asset.get(name, default)
            return getattr(asset, name, default)

        raw = _field("fractionable", None)
        if raw is None:
            # The record came back but carries no flag — an older API shape,
            # a stub, a mock. "Absent" is not "true".
            result = {
                "fractionable": False, "reason": "fractionable_unknown",
                "symbol": canonical,
            }
        elif bool(raw):
            result = {
                "fractionable": True, "reason": "fractionable",
                "symbol": canonical,
            }
        else:
            result = {
                "fractionable": False, "reason": "not_fractionable",
                "symbol": canonical,
            }
        self._fractionable_cache[canonical] = result
        return result

    def get_recent_daily_closes(self, lookback_days: int = 10) -> list[tuple[str, float]]:
        """Official regular-session daily CLOSE equity for recent trading days.

        Source: Alpaca portfolio_history at 1D timeframe with
        ``extended_hours=False`` — the broker-side source of truth for
        end-of-regular-session equity. Crucially, unlike ``account.last_equity``
        (which is the PRIOR trading day's close, and so is one day stale at the
        20:00 ET evening run), the LAST point here is TODAY's 4pm close. That
        lets the evening report show a true close-to-close ("4pm-to-4pm") P&L
        instead of a close-to-8pm-after-hours broker diff.

        Returns ``[(et_date_str, close_equity), ...]`` oldest-first, or ``[]``
        on any failure (caller falls back to the real-time P&L). Best-effort —
        never raises. ET-date mapping mirrors scripts/export_alpaca_trades.py.
        """
        from datetime import datetime, timedelta, timezone
        from src.util.time import ET
        try:
            from alpaca.trading.requests import GetPortfolioHistoryRequest
            now = datetime.now(timezone.utc)
            req = GetPortfolioHistoryRequest(
                timeframe="1D", extended_hours=False,
                start=now - timedelta(days=lookback_days * 2 + 10), end=now,
            )
            history = self.client.get_portfolio_history(history_filter=req)
        except Exception as exc:
            logger.warning("get_recent_daily_closes: portfolio_history failed: %s", exc)
            return []
        timestamps = getattr(history, "timestamp", None) or []
        equities = getattr(history, "equity", None) or []
        out: list[tuple[str, float]] = []
        for i, ts in enumerate(timestamps):
            if i >= len(equities) or equities[i] is None:
                continue
            try:
                d = datetime.fromtimestamp(int(ts), tz=timezone.utc).astimezone(ET).strftime("%Y-%m-%d")
                eq = float(equities[i])
            except (TypeError, ValueError, OSError):
                continue
            out.append((d, eq))
        return out

    def get_full_portfolio_history(self) -> list[tuple[str, float]]:
        """All available 1D equity history from Alpaca portfolio_history.

        Returns [(et_date_str, equity), ...] oldest-first, skipping zero
        rows (pre-funding). Best-effort — never raises.
        """
        from datetime import datetime, timedelta, timezone
        from src.util.time import ET
        try:
            from alpaca.trading.requests import GetPortfolioHistoryRequest
            now = datetime.now(timezone.utc)
            req = GetPortfolioHistoryRequest(
                timeframe="1D", extended_hours=False,
                start=now - timedelta(days=365 * 5), end=now,
            )
            history = self.client.get_portfolio_history(history_filter=req)
        except Exception as exc:
            logger.warning("get_full_portfolio_history failed: %s", exc)
            return []
        timestamps = getattr(history, "timestamp", None) or []
        equities = getattr(history, "equity", None) or []
        out: list[tuple[str, float]] = []
        for i, ts in enumerate(timestamps):
            if i >= len(equities) or equities[i] is None:
                continue
            try:
                d = datetime.fromtimestamp(int(ts), tz=timezone.utc).astimezone(ET).strftime("%Y-%m-%d")
                eq = float(equities[i])
            except (TypeError, ValueError, OSError):
                continue
            if eq == 0.0:
                continue  # skip pre-funding rows
            out.append((d, eq))
        return out

    def get_positions(self) -> list[Position]:
        raw_positions = self.client.get_all_positions()
        positions = []
        for p in raw_positions:
            symbol = _internal_symbol(p.symbol)
            positions.append(Position(
                symbol=symbol,
                qty=float(p.qty),
                avg_entry=float(p.avg_entry_price),
                current_price=float(p.current_price),
                market_value=float(p.market_value),
                unrealized_pnl=float(p.unrealized_pl),
                unrealized_intraday_pnl=float(getattr(p, "unrealized_intraday_pl", 0) or 0),
                sector=_get_sector(symbol),
            ))
        return positions

    def is_trading_day(self, on_date: date | None = None) -> bool:
        from src.util.time import et_today
        target_date = on_date or et_today()  # ET trading-day, not host-local
        # Per-date result cache. is_trading_day is hit on every session
        # entry, in scheduler `_run_safe`, in some agent helpers — easily
        # 20+ Alpaca calendar lookups per session for a fact that's
        # invariant within the day. The result is also stable: a date
        # either is or isn't a trading day, decided by the exchange
        # calendar months in advance, so per-date cache is safe.
        cached = self._trading_day_cache.get(target_date)
        if cached is not None:
            return cached
        try:
            from alpaca.trading.requests import GetCalendarRequest

            calendar = self.client.get_calendar(
                GetCalendarRequest(start=target_date, end=target_date)
            )
            result = bool(calendar)
        except Exception as exc:
            logger.warning(
                "Failed to confirm trading calendar for %s; assuming market closed: %s",
                target_date, exc,
            )
            # Do NOT cache a failed lookup — caller's session is already
            # aborted (we returned False) but a transient API hiccup
            # shouldn't poison the cache for the rest of the day.
            return False
        self._trading_day_cache[target_date] = result
        return result

    def is_last_trading_day_of_quarter(self, on_date: date | None = None) -> bool:
        """True when `on_date` (default today-ET) is the last OPEN session
        of the current quarter — respects holidays and early closes.

        Uses Alpaca's calendar. For Mar/Jun/Sep/Dec only (other months
        short-circuit to False, saving the API call). Queries the
        calendar from today through month-end; we're the last trading
        day iff no later entry exists.

        The quarterly meta-reflector launchd wrapper relies on this:
        Dec 31 is often Sunday, and the real "last trading day" can be
        Dec 29 or Dec 30 depending on the calendar. Weekday heuristic
        alone gets this wrong.
        """
        from src.trading_calendar import _QUARTER_END_MONTHS, et_today
        from datetime import date as _date, timedelta as _td
        target = on_date or et_today()
        if target.month not in _QUARTER_END_MONTHS:
            return False
        # Build month-end date for range query (last day of target.month).
        if target.month == 12:
            next_month_start = _date(target.year + 1, 1, 1)
        else:
            next_month_start = _date(target.year, target.month + 1, 1)
        month_end = next_month_start - _td(days=1)
        try:
            from alpaca.trading.requests import GetCalendarRequest
            calendar = self.client.get_calendar(
                GetCalendarRequest(start=target, end=month_end)
            ) or []
        except Exception as exc:
            logger.warning(
                "is_last_trading_day_of_quarter: calendar query failed (%s → %s): %s",
                target, month_end, exc,
            )
            return False
        if not calendar:
            return False
        # Alpaca returns one entry per trading day in [start, end]. We are the
        # last iff the LAST entry's date equals target.
        last_entry = calendar[-1]
        last_date = getattr(last_entry, "date", None)
        if last_date is None:
            return False
        return last_date == target

    def get_session_close(self, on_date: date | None = None):
        """Return the ET-aware datetime when the regular cash session closes
        today, or None if today is not a trading day (weekend / holiday) or
        the calendar lookup fails.

        Distinct from `is_trading_day` because it answers a different
        question: "WHEN does today close?" — needed to detect early-close
        days (Thanksgiving Friday 13:00, July 3 half-day) where the
        launchd-scheduled midday (13:00-14:30 ET) and close (15:30-15:55 ET)
        sessions would otherwise keep running against an already-shut market.
        """
        from src.trading_calendar import ET, et_today
        from datetime import datetime as _dt
        target_date = on_date or et_today()
        try:
            from alpaca.trading.requests import GetCalendarRequest

            calendar = self.client.get_calendar(
                GetCalendarRequest(start=target_date, end=target_date)
            )
        except Exception as exc:
            logger.warning(
                "get_session_close: calendar query failed for %s: %s",
                target_date, exc,
            )
            return None
        if not calendar:
            return None
        entry = calendar[0]
        entry_date = getattr(entry, "date", None)
        entry_close = getattr(entry, "close", None)
        if entry_date is None or entry_close is None:
            return None
        try:
            # alpaca-py's Calendar.close is a full naive DATETIME (already
            # carrying the session date + ET wall clock), NOT a time. The old
            # code called datetime.combine(date, datetime), which ALWAYS
            # raised TypeError → logged → returned None → the early-close
            # guard never fired and midday/close ran against a shut market on
            # half-days, submitting orders that can only be rejected
            # (2026-07-16 audit: dead code since it was written; the test that
            # was supposed to cover it used a MagicMock with a `time`).
            # Keep the `time` branch for the older SDK shape.
            if isinstance(entry_close, _dt):
                return entry_close.replace(tzinfo=ET)
            return _dt.combine(entry_date, entry_close).replace(tzinfo=ET)
        except Exception as exc:
            logger.warning(
                "get_session_close: failed to resolve date=%s close=%s: %s",
                entry_date, entry_close, exc,
            )
            return None

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
                logger.warning("get_top_movers: ScreenerClient init failed: %s", exc)
                self._screener_client = None
                return []

        try:
            movers = self._screener_client.get_market_movers(
                MarketMoversRequest(top=n)
            )
        except Exception as exc:
            logger.warning("get_top_movers: screener API call failed: %s", exc)
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
            req = StockBarsRequest(
                symbol_or_symbols=alpaca_symbol,
                timeframe=TimeFrame.Day,
                start=start,
                end=end,
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
            out: list[OHLCV] = []
            for b in bars_list:
                ts = getattr(b, "timestamp", None)
                d = ts.date() if ts is not None else None
                if d is None:
                    continue
                try:
                    out.append(OHLCV(
                        date=d,
                        open=float(getattr(b, "open", 0) or 0),
                        high=float(getattr(b, "high", 0) or 0),
                        low=float(getattr(b, "low", 0) or 0),
                        close=float(getattr(b, "close", 0) or 0),
                        volume=int(getattr(b, "volume", 0) or 0),
                    ))
                except (TypeError, ValueError):
                    continue
            return out
        except Exception as e:
            logger.warning("broker.get_bars failed for %s: %s", symbol, e)
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
            if timeframe == "5m":
                # "5m" is explicitly today's session. Starting at ET
                # midnight naturally includes the full regular session
                # without relying on the host's timezone.
                start = _dt.combine(
                    now.astimezone(ET).date(), _dt.min.time(), tzinfo=ET
                )
            else:
                start = now - _td(days=lookback_days)
            alpaca_symbol = _alpaca_symbol(symbol)
            req = StockBarsRequest(
                symbol_or_symbols=alpaca_symbol,
                timeframe=timeframe_value,
                start=start,
                end=now,
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

            out: list[dict] = []
            for bar in bars_list:
                ts = getattr(bar, "timestamp", None)
                if ts is None:
                    continue
                try:
                    out.append(
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
            return out
        except Exception as exc:
            logger.warning(
                "broker.get_intraday_chart_bars failed for %s/%s: %s",
                symbol, timeframe, exc,
            )
            return []

    def get_current_stop_price(self, symbol: str) -> float | None:
        """Return the price of the current open protective stop for a symbol.

        Used by ex-dividend / trailing-stop logic that needs to read the
        existing stop before replacing it. Returns None if no protective stop
        exists or the query fails.

        A long's protective stop is a SELL stop (fires as price falls); a
        short's is a BUY stop (fires as price rises) — Alpaca has no notion
        of "protective" on the order itself, only a side. Pre-shorts this
        method only ever looked for SELL stops, so a short's live BUY stop
        was invisible here: every downstream caller (ex-div shift,
        deterministic trailing, coverage repair) would treat a perfectly
        protected short as unprotected. This reads BOTH sides and reports
        whichever one is actually present, rather than asking the caller to
        already know the position's direction.
        """
        try:
            from alpaca.trading.requests import GetOrdersRequest
            orders = self.client.get_orders(
                filter=GetOrdersRequest(
                    status=QueryOrderStatus.OPEN,
                    symbols=[_alpaca_symbol(symbol)], nested=True,
                )
            )
        except Exception as exc:
            logger.warning("get_current_stop_price failed for %s: %s", symbol, exc)
            return None
        # Post-#102 a position can legitimately carry SEVERAL stops on its
        # protective side (one GTC stop per entry BUY, plus coverage-repair
        # top-ups). The old first-match return made "the current stop"
        # depend on Alpaca's ordering (audit round 2). Consumers want the
        # level that fires FIRST; qty-weighting would blur two real levels
        # into a price nobody set.
        sell_stops: list[float] = []
        buy_stops: list[float] = []
        for order in orders or []:
            order_type = str(getattr(getattr(order, "order_type", None), "value",
                                    getattr(order, "order_type", ""))).lower()
            order_side = str(getattr(getattr(order, "side", None), "value",
                                    getattr(order, "side", ""))).lower()
            if "stop" not in order_type:
                continue
            try:
                px = float(getattr(order, "stop_price", 0) or 0)
            except (TypeError, ValueError):
                continue
            if px <= 0:
                continue
            if order_side == "sell":
                sell_stops.append(px)
            elif order_side == "buy":
                buy_stops.append(px)
        if sell_stops and buy_stops:
            # A single symbol can't legitimately be both long and short at
            # once, so seeing both sides means stale orders survived a
            # direction flip. Reporting either price would be a guess about
            # which one is "the" stop — fail closed instead so the caller
            # treats this as needing attention rather than trusting a number
            # that might belong to a position that no longer exists.
            logger.error(
                "get_current_stop_price: %s carries BOTH sell-stops %s and "
                "buy-stops %s — direction is ambiguous, refusing to report "
                "a stop", symbol, sorted(sell_stops), sorted(buy_stops),
            )
            return None
        if sell_stops:
            if len(sell_stops) > 1:
                logger.info(
                    "get_current_stop_price: %s carries %d sell-stops %s — "
                    "reporting the highest (first to trigger on the way "
                    "down)", symbol, len(sell_stops), sorted(sell_stops),
                )
            return max(sell_stops)
        if buy_stops:
            if len(buy_stops) > 1:
                logger.info(
                    "get_current_stop_price: %s carries %d buy-stops %s — "
                    "reporting the lowest (first to trigger on the way up)",
                    symbol, len(buy_stops), sorted(buy_stops),
                )
            return min(buy_stops)
        return None

    def get_latest_price(self, symbol: str) -> float | None:
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
                return trade_price

            quote_data = self._data_client.get_stock_latest_quote(
                StockLatestQuoteRequest(symbol_or_symbols=alpaca_symbol)
            )
            quote = self._extract_symbol_payload(quote_data, alpaca_symbol)
            ask_price = float(getattr(quote, "ask_price", 0) or 0)
            bid_price = float(getattr(quote, "bid_price", 0) or 0)
            if ask_price > 0 and bid_price > 0:
                return (ask_price + bid_price) / 2
            if ask_price > 0:
                return ask_price
            if bid_price > 0:
                return bid_price
        except Exception as exc:
            logger.warning("Failed to fetch latest price for %s: %s", symbol, exc)

        return None

    def get_latest_quote(self, symbol: str) -> dict[str, float | None]:
        """Return the current bid/ask without inventing a side of the book.

        Execution uses the ask to construct a bounded marketable BUY limit.
        Missing or failed quote data returns explicit ``None`` fields so the
        caller can retain its existing last-trade behavior without guessing.
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

            {"last_price", "prev_close",
             "session_open", "session_high", "session_low", "session_volume"}

        The `session_*` fields come from Alpaca's TODAY bar, which is an
        INCOMPLETE, still-forming bar — callers must present it as such and
        must never append it to a series of completed daily bars. Any field
        is `None` when unavailable. Never raises — broker/network failure
        degrades to an empty dict (caller treats that as "no signal this
        tick", not a crash).
        """
        if not symbols:
            return {}
        if self._data_client is None:
            try:
                from alpaca.data.historical.stock import StockHistoricalDataClient

                self._data_client = StockHistoricalDataClient(self.api_key, self.secret_key)
                _install_http_timeout(self._data_client)
            except Exception as exc:
                logger.warning("get_intraday_snapshots: data client init failed: %s", exc)
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
            out[symbol] = {
                "last_price": _num(trade, "price"),
                "prev_close": _num(prev_bar, "close"),
                "session_open": _num(today_bar, "open"),
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

    def cancel_open_orders(self) -> int:
        """Cancel all open orders. Returns count of cancelled orders."""
        try:
            cancelled = self.client.cancel_orders()
            count = len(cancelled) if cancelled else 0
            if count:
                logger.info("Cancelled %d open order(s)", count)
            return count
        except Exception as exc:
            logger.warning("Failed to cancel open orders: %s", exc)
            return 0

    def snapshot_protective_stops(
        self, symbol: str, *, side: str = "sell",
    ) -> tuple[bool, list[dict]]:
        """List + snapshot open protective stop orders WITHOUT cancelling them.

        audit F1 (review #1): the write-ahead recovery row must be
        persisted BEFORE any broker mutation. Splitting the read
        (snapshot) from the write (cancel) lets the pipeline do
        snapshot → persist WAL → cancel, so a process kill anywhere
        from the cancel onward is recoverable. Previously the WAL insert
        ran AFTER cancel_protective_stops had already cancelled the
        stops at the broker — a kill in that window left a naked
        position with no recovery intent.

        `side` is the STOP order's own side: "sell" (default) finds the
        stops protecting a long; "buy" finds the stops protecting a short.
        Every existing caller cancels/restores/re-protects a long being
        SOLD, so the default is unchanged; the coverage reconciler is the
        one caller that passes `side="buy"` to check a short.

        Returns ``(ok, specs)``. ``ok`` is kept for call-site symmetry
        with cancel_protective_stops; a pure read can't "fail to clear"
        so it is always True (a listing API error is swallowed by
        _list_open_protective_stop_orders and surfaces as no stops, exactly
        as in the pre-split behaviour).
        """
        stops = self._list_open_protective_stop_orders(symbol, side=side)
        if not stops:
            return True, []
        specs: list[dict] = []
        for order in stops:
            spec = self._snapshot_stop_order(order)
            if spec:
                specs.append(spec)
        return True, specs

    def cancel_snapshotted_stops(
        self, symbol: str, specs: list[dict],
    ) -> bool:
        """Cancel pre-snapshotted protective stops by id.

        Same partial-failure discipline as the original
        cancel_protective_stops: if any cancel raises, the ones that
        did cancel are restored and False is returned (the caller won't
        proceed with the SELL, so leaving coverage shrunk for no gain
        would be strictly worse). Returns True iff every stop was
        cancelled (or there were none).
        """
        if not specs:
            return True
        cancelled: list[dict] = []
        failed = 0
        for spec in specs:
            sid = spec.get("id")
            if not sid:
                continue
            try:
                self.client.cancel_order_by_id(sid)
                cancelled.append(spec)
            except Exception as exc:
                logger.warning(
                    "cancel_snapshotted_stops: cancel failed for %s order "
                    "%s: %s", symbol, sid, exc,
                )
                failed += 1
        if failed > 0:
            restored = 0
            rollback_failed: list[dict] = []
            if cancelled:
                # _restore_stop_orders returns (restored_count, failed_specs)
                # — the old code DISCARDED it, so a rollback that itself
                # failed left the position with shrunk coverage and reported
                # only a bare False. The SELL is skipped either way, but the
                # operator (and the next session's coverage reconcile, which
                # now auto-repairs) must be able to see it (2026-07-16 audit).
                restored, rollback_failed = self._restore_stop_orders(symbol, cancelled)
            if rollback_failed:
                logger.error(
                    "cancel_snapshotted_stops: %d/%d cancel(s) failed for %s AND "
                    "the rollback could not restore %d of %d cancelled stop(s) — "
                    "%s is now UNDER-PROTECTED; next session's coverage reconcile "
                    "must repair it. SELL won't proceed.",
                    failed, len(specs), symbol, len(rollback_failed), len(cancelled),
                    symbol,
                )
            else:
                logger.warning(
                    "cancel_snapshotted_stops: %d/%d cancel(s) failed for %s "
                    "(rolled back %d/%d that succeeded); SELL won't proceed",
                    failed, len(specs), symbol, restored, len(cancelled),
                )
            return False
        if cancelled:
            logger.info(
                "Cancelled %d protective stop(s) for %s",
                len(cancelled), symbol,
            )
        return True

    def cancel_protective_stops(self, symbol: str) -> tuple[bool, list[dict]]:
        """Cancel all open SELL stop orders for one symbol so a fresh exit
        order has free shares to work with.

        Returns ``(success, cancelled_specs)``:
          - ``success`` is True iff every stop was cancelled cleanly (or
            none existed). Caller should skip the SELL on False.
          - ``cancelled_specs`` is the list of stop snapshots (qty,
            stop_price, limit_price) that were successfully cancelled.
            Caller uses this to:
              1. ``_restore_stop_orders`` if the SELL is rejected by
                 the broker (rollback the cancellation so coverage is
                 preserved).
              2. ``_submit_stop_limit_order`` on the residual qty after
                 a *partial* exit (TAKE_PROFIT / REDUCE / PARTIAL_SELL)
                 — without this, the residual position rides naked
                 until the next session re-attaches an OTO stop.

        Why this exists: Alpaca rejects new SELL orders when shares are
        held_for_orders by an existing protective stop — the OTO stop-loss
        leg attached to a morning BUY, or a TRAIL_STOP placed by midday.
        Without clearing those holds first, REDUCE / SELL / EMERGENCY_SELL
        / TAKE_PROFIT all surface as 'insufficient qty available' rejects
        (2026-04-25 AMZN incident, related_orders=[<TRAIL_STOP id>]).

        On partial cancel failure (some succeed, then one raises) the
        already-cancelled stops are restored before returning False —
        same rollback discipline as ``replace_stop_loss``. The caller
        won't proceed with the SELL anyway, so leaving partial-cancelled
        state at the broker would just shrink coverage for no gain.

        Now composed from snapshot_protective_stops +
        cancel_snapshotted_stops (audit F1 review #1). The external
        contract is unchanged: no stops -> (True, []); all cancelled ->
        (True, specs); partial failure -> rolled back, (False, []).
        Direct callers/tests are unaffected; SELL paths use the
        pipeline's write-ahead orchestrator instead so the recovery row
        lands before the cancel.
        """
        ok, specs = self.snapshot_protective_stops(symbol)
        if not ok:
            return False, []
        if not specs:
            return True, []
        if not self.cancel_snapshotted_stops(symbol, specs):
            return False, []
        return True, specs

    def cancel_open_entry_orders(self, symbol: str | None = None) -> int:
        """Cancel open entry orders on EITHER side — BUY-to-open-long and
        SELL-to-open-short — while preserving protective stop legs on
        either side.

        `symbol` scopes the cancel to one name — used by the full-exit
        SELL/COVER discipline (audit round 2: a fully-exited symbol could
        still carry the same day's resting DAY entry BUY, which would
        silently re-open the position — or, in the emergency-liquidation
        case, re-buy into the crash the breaker just sold). Stage 3 (shorts)
        gap fix: an EMERGENCY_COVER used to leave a resting SELL-to-open
        entry order untouched, which could fill and re-open the exact short
        exposure the emergency close just cleared — the short-side mirror
        of the BUY case above.

        Pre-shorts this only ever needed to filter by SIDE: entries were
        always BUY and protective legs were always SELL, so a bare
        `side == "buy"` filter could never touch a stop. Now that BUY-side
        protective covers (a short's stop) and SELL-side entries (a
        short-open) both exist, side alone stopped being a safe proxy for
        "is this an entry" — the discriminator has to be ORDER TYPE. Any
        *stop* order (stop / stop_limit / trailing_stop — the same
        `"stop" in order_type` test `_list_open_sell_stop_orders` /
        `_list_open_stop_orders_by_side` already use to find a protective
        leg) is left alone regardless of side; every other BUY or SELL
        order is a plain entry and gets cancelled.
        """
        try:
            from alpaca.trading.requests import GetOrdersRequest

            req_kwargs = dict(status=QueryOrderStatus.OPEN, nested=True)
            if symbol:
                req_kwargs["symbols"] = [_alpaca_symbol(symbol)]
            orders = self.client.get_orders(filter=GetOrdersRequest(**req_kwargs))
            count = 0
            for order in orders or []:
                order_id = getattr(order, "id", None)
                order_side = str(getattr(getattr(order, "side", None), "value",
                                        getattr(order, "side", ""))).lower()
                order_type = str(getattr(getattr(order, "order_type", None), "value",
                                        getattr(order, "order_type", ""))).lower()
                if order_side not in ("buy", "sell") or not order_id:
                    continue
                if "stop" in order_type:
                    continue  # protective leg on either side — preserve it
                self.client.cancel_order_by_id(order_id)
                count += 1
            if count:
                logger.info("Cancelled %d open entry order(s)", count)
            return count
        except Exception as exc:
            logger.warning("Failed to cancel open entry orders: %s", exc)
            return 0

    def open_buy_notional(self) -> float | None:
        """Dollar notional of all OPEN BUY orders, or None when the query fails.

        Used by the cash sweeper: Alpaca's `cash` field does not subtract
        open-order holds, so parking must leave room for still-working BUY
        limits. The None-vs-0.0 distinction matters — a transient API failure
        must read as "unknowable" (caller skips parking), never as "no
        pending buys" (caller would sweep cash a pending fill needs).
        """
        try:
            from alpaca.trading.requests import GetOrdersRequest

            orders = self.client.get_orders(
                filter=GetOrdersRequest(
                    status=QueryOrderStatus.OPEN,
                    side=OrderSide.BUY,
                    nested=True,
                )
            )
            total = 0.0
            for order in orders or []:
                order_side = getattr(getattr(order, "side", None), "value", getattr(order, "side", ""))
                if str(order_side).lower() != "buy":
                    continue
                try:
                    qty = float(getattr(order, "qty", 0) or 0)
                except (TypeError, ValueError):
                    qty = 0.0
                price = None
                for attr in ("limit_price", "stop_price"):
                    raw = getattr(order, attr, None)
                    if raw is not None:
                        try:
                            candidate = float(raw)
                        except (TypeError, ValueError):
                            continue
                        if candidate > 0:
                            price = candidate
                            break
                if price is None:
                    # Market order with no price attached — estimate from the
                    # live quote; on failure treat the whole answer as
                    # unknowable rather than under-counting the hold.
                    live = self.get_latest_price(getattr(order, "symbol", ""))
                    if not live or live <= 0:
                        return None
                    price = live
                total += qty * price
            return total
        except Exception as exc:
            logger.warning("open_buy_notional query failed: %s", exc)
            return None

    def list_recent_orders(
        self, symbol: str, side: str, after,
    ) -> list[dict] | None:
        """All of `symbol`'s orders (any status) on `side` since `after`.

        audit F4: used by the orphan-pending_submit sweep to match a DB
        write-ahead row to a broker order whose id we lost to a crash
        between submit_order() and confirm_trade_submitted(). Returns
        light dicts {id, symbol, side, qty, status}.

        audit F4 (review #2): the return distinguishes "query succeeded,
        zero orders" ([]) from "query FAILED" (None). The caller must
        NOT treat a transient Alpaca/API failure as "submit never
        landed" — doing so would mark a possibly-real / already-filled
        BUY as submit_failed. None ⇒ leave the row and retry next
        session; [] ⇒ genuinely no such order.
        """
        try:
            from alpaca.trading.requests import GetOrdersRequest

            want = side.lower()
            req_side = OrderSide.BUY if want == "buy" else OrderSide.SELL
            orders = self.client.get_orders(
                filter=GetOrdersRequest(
                    status=QueryOrderStatus.ALL,
                    symbols=[_alpaca_symbol(symbol)],
                    side=req_side, after=after, nested=False,
                )
            )
            out: list[dict] = []
            for o in orders or []:
                o_side = str(getattr(getattr(o, "side", None), "value",
                                     getattr(o, "side", ""))).lower()
                if o_side != want:
                    continue
                try:
                    oqty = float(getattr(o, "qty", 0) or 0)
                except (TypeError, ValueError):
                    oqty = 0.0
                oid = str(getattr(o, "id", "") or "")
                if not oid:
                    continue
                out.append({
                    "id": oid,
                    "symbol": _internal_symbol(getattr(o, "symbol", "") or ""),
                    "side": o_side,
                    "qty": oqty,
                    "status": str(getattr(getattr(o, "status", None), "value",
                                          getattr(o, "status", ""))).lower(),
                })
            return out
        except Exception as exc:
            logger.warning(
                "list_recent_orders failed for %s %s: %s — returning None "
                "so the caller retries rather than misjudging the order "
                "absent", side, symbol, exc,
            )
            return None

    def list_filled_sell_orders(self, symbol: str, after) -> list[dict] | None:
        """Every FILLED sell-side order for `symbol` whose FILL happened at
        or after `after` — broker truth, independent of anything this
        process itself submitted or remembers.

        2026-08-28 ONDS/CCJ: both positions were closed by their broker-
        resident protective stop (a GTC stop-limit order placed by
        `place_entry_protection` / `_repair_stop_coverage` /
        `shift_stops_down`), and none of those paths ever write the STOP
        ORDER ITSELF into `trades` — only every system-DECIDED exit (SELL /
        REDUCE / TRAIL_STOP / SWEEP_SELL) does that, at submission time.
        `_reconcile_stop_out_fills` (src/pipeline.py) uses this method to
        ask the broker directly rather than trusting the ledger's own
        opinion of what happened, then diffs the result against
        `Database.get_known_broker_order_ids` to find fills the ledger has
        never recorded.

        `after` is applied CLIENT-SIDE against each order's `filled_at`,
        deliberately NOT passed to Alpaca's own `after=` query parameter
        (unlike `list_recent_orders`, which correctly uses it that way for
        its own purpose). Alpaca's `after`/`until` filter on `submitted_at`
        — when it was ACCEPTED, not when it EXECUTED — and a GTC protective
        stop is typically submitted at entry and can rest for a long time
        before firing. Verified 2026-08-28 against the real paper account
        (MRVL): the stop was submitted 2026-08-21 13:35 and filled
        2026-08-24 13:48 — a naive `after=now-7d` broker-side query anchored
        4 days before "now" would have excluded it entirely (its
        submitted_at sat 7h before that cutoff) even though the FILL was
        comfortably inside the 7-day window everyone actually cares about.
        Silently missing a stop-out because the underlying order happened
        to be placed slightly outside an arbitrary lookback is exactly the
        failure mode this reconciler exists to prevent, so the broker query
        below is intentionally unbounded on symbol+side and every date
        filtering happens here, against the field that actually means
        "when did this become a real exit".

        Distinct from `list_recent_orders`: that method returns orders of
        ANY status and is used by the orphan-BUY sweep to match a KNOWN
        write-ahead row by qty, submitted within a tight recent window —
        `submitted_at` is exactly the right anchor there. This method is
        scoped to already-FILLED sells and is used to discover fills the
        ledger has NEVER SEEN, including ones this process itself placed at
        the broker (a protective stop) but never logged — `filled_at` is
        the only anchor that means what the caller needs it to mean.

        Returns None on a query failure — the caller must retry on the
        next reconciliation pass rather than concluding "no fills" and
        risking a missed exit (same None-means-retry contract as
        `list_recent_orders`). On success, a list of lightweight dicts:
        {id, symbol, qty (the ACTUAL filled qty), price (the ACTUAL filled
        avg price), filled_at (ISO-8601 UTC string, or None if the broker
        didn't report one), order_type} — orders with no filled_at at all
        are KEPT (never silently excluded by the date filter; None means
        "unknown timing", not "too old").
        """
        try:
            from alpaca.trading.requests import GetOrdersRequest

            orders = self.client.get_orders(
                filter=GetOrdersRequest(
                    status=QueryOrderStatus.ALL,
                    symbols=[_alpaca_symbol(symbol)],
                    side=OrderSide.SELL, nested=False,
                )
            )
            out: list[dict] = []
            for o in orders or []:
                status = str(getattr(getattr(o, "status", None), "value",
                                     getattr(o, "status", ""))).lower()
                if status != "filled":
                    continue
                oid = str(getattr(o, "id", "") or "")
                if not oid:
                    continue
                try:
                    filled_qty = float(getattr(o, "filled_qty", 0) or 0)
                except (TypeError, ValueError):
                    filled_qty = 0.0
                try:
                    filled_avg_price = float(getattr(o, "filled_avg_price", 0) or 0)
                except (TypeError, ValueError):
                    filled_avg_price = 0.0
                if filled_qty <= 0 or filled_avg_price <= 0:
                    # "filled" with no actual qty/price is not a real fill
                    # to reconstruct a ledger row from — nothing to record.
                    continue
                filled_at = getattr(o, "filled_at", None)
                if filled_at is not None and after is not None:
                    cutoff = after if getattr(after, "tzinfo", None) else after.replace(
                        tzinfo=filled_at.tzinfo,
                    )
                    if filled_at < cutoff:
                        continue
                order_type = getattr(o, "type", None) or getattr(o, "order_type", None)
                out.append({
                    "id": oid,
                    "symbol": _internal_symbol(getattr(o, "symbol", "") or ""),
                    "qty": filled_qty,
                    "price": filled_avg_price,
                    "filled_at": filled_at.isoformat() if hasattr(filled_at, "isoformat") else None,
                    "order_type": str(getattr(order_type, "value", order_type)) if order_type else None,
                })
            return out
        except Exception as exc:
            logger.warning(
                "list_filled_sell_orders failed for %s: %s — returning None "
                "so the caller retries rather than concluding there was no "
                "fill (a missed stop-out is a money-relevant accounting "
                "gap, not just a stale read)", symbol, exc,
            )
            return None

    def get_order_fill_info(self, order_id: str) -> dict | None:
        """Return {status, filled_qty, filled_avg_price} for an order, or None.

        Used by Phase 3 reconciliation. The caller decides whether the
        returned status is terminal; this method does not block / poll.
        """
        try:
            order = self.client.get_order_by_id(order_id)
        except Exception as exc:
            logger.warning("get_order_fill_info failed for %s: %s", order_id, exc)
            return None
        status = str(
            getattr(getattr(order, "status", None), "value",
                    getattr(order, "status", ""))
        ).lower()
        try:
            filled_qty = float(getattr(order, "filled_qty", 0) or 0)
        except (TypeError, ValueError):
            filled_qty = 0.0
        try:
            filled_avg_price = float(getattr(order, "filled_avg_price", 0) or 0)
        except (TypeError, ValueError):
            filled_avg_price = 0.0
        return {
            "status": status,
            "filled_qty": filled_qty,
            "filled_avg_price": filled_avg_price,
        }

    def wait_for_order_terminal(
        self,
        order_id: str,
        timeout_seconds: float = 15.0,
        poll_interval: float = 1.0,
    ) -> str | None:
        """Wait for an order to reach a terminal state and return its last known status."""
        deadline = time.monotonic() + timeout_seconds
        terminal_states = {
            "filled",
            "canceled",
            "cancelled",
            "expired",
            "rejected",
            "done_for_day",
            "replaced",
        }
        last_status = None

        while time.monotonic() < deadline:
            try:
                order = self.client.get_order_by_id(order_id)
                status = str(getattr(getattr(order, "status", None), "value", getattr(order, "status", ""))).lower()
            except Exception as exc:
                logger.warning("Failed to poll order %s: %s", order_id, exc)
                return last_status

            last_status = status or last_status
            if status in terminal_states:
                return status
            time.sleep(poll_interval)

        return last_status

    def submit_order(self, symbol: str, qty: float, side: str,
                     limit_price: float | None = None,
                     stop_loss_price: float | None = None,
                     take_profit_price: float | None = None,
                     reference_price: float | None = None) -> dict:
        order_side = OrderSide.BUY if side.lower() == "buy" else OrderSide.SELL
        internal_symbol = _internal_symbol(symbol)
        alpaca_symbol = _alpaca_symbol(internal_symbol)

        # Normalize to Alpaca's tick size — sub-penny values from quote-midpoint
        # math or LLM outputs get Alpaca error 42210000 and a rejected order.
        limit_price = _quantize_price(limit_price)
        stop_loss_price = _quantize_price(stop_loss_price)
        take_profit_price = _quantize_price(take_profit_price)

        # Fat-finger / outlier price guardrail. If the caller passed a
        # reference_price (typically today's quote or last bar close) and any
        # of our prices is more than 20% away from it, the number is almost
        # certainly garbage — a data-source glitch ($0.01 quote on a $300
        # stock, or an LLM hallucinated entry). Submitting would turn qty
        # sizing into nonsense (5% alloc / $0.01 = 500× expected shares) and
        # blow through every risk check. Refuse the order.
        OUTLIER_MAX_DEVIATION = 0.20
        if reference_price and reference_price > 0:
            for label, candidate in (
                ("limit_price", limit_price),
                ("stop_loss_price", stop_loss_price),
                ("take_profit_price", take_profit_price),
            ):
                if candidate is None or candidate <= 0:
                    continue
                deviation = abs(candidate - reference_price) / reference_price
                if deviation > OUTLIER_MAX_DEVIATION:
                    logger.error(
                        "Fat-finger guard: %s %s — %s=$%.4f deviates %.1f%% from reference $%.2f. "
                        "Order REJECTED (likely data glitch or LLM hallucination).",
                        side.upper(), symbol, label, candidate, deviation * 100, reference_price,
                    )
                    return {"id": None, "status": "rejected_outlier", "symbol": internal_symbol}

        # Protective stop for a BUY is placed as a SEPARATE GTC stop-limit
        # AFTER the entry fills — NOT as an OTO leg.
        #
        # WHY (2026-07-16 audit, CRITICAL): `StopLossRequest` carries no
        # time_in_force of its own, so an OTO child leg inherits the PARENT's
        # TIF. The parent must be DAY (an unfilled entry limit must die at the
        # close, never fill into a stale thesis the next morning) — which
        # silently made every BUY-attached stop a DAY order too. Alpaca expired
        # it at 16:00 ET the same session, so any position bought in the
        # morning and not later given a midday/close TRAIL_STOP (which uses the
        # GTC `_submit_stop_limit_order` path) sat NAKED overnight — precisely
        # when gap risk is the reason the stop exists. Confirmed in production:
        # VST bought 2026-06-26 09:47 ET with SL=$158.75; the same evening's
        # coverage reconcile logged `VST held=31.0000 but only 0.0000 covered`;
        # it was ultimately exited at $152.77 for ~$185 more loss than the stop
        # would have capped. This also contradicted the close-session prompt,
        # which tells the reviewer to hold overnight *because* the broker stop
        # is standing watch.
        #
        # Placing the stop post-fill also fixes a second latent bug: the OTO
        # leg was sized to the REQUESTED qty, so a partial entry fill left a
        # stop covering more shares than we own. `_place_entry_protection`
        # keys the stop to the ACTUAL filled qty.
        # Stage 3 (shorts, D7): a SHORT entry (side='sell_short') owes a
        # protective stop exactly the way a BUY entry does — it just gets
        # placed on the opposite side by `place_entry_protection`. 'sell'
        # deliberately stays OUT of this: that's this codebase's convention
        # for REDUCING/closing a long (`_submit_protected_sell`'s default),
        # which never passes `stop_loss_price` and so never reaches here
        # regardless — 'sell_short' is the only sell-side string an ENTRY
        # ever uses.
        use_stop = (stop_loss_price is not None and stop_loss_price > 0
                    and side.lower() in ("buy", "sell_short"))

        if limit_price is not None:
            request = LimitOrderRequest(
                symbol=alpaca_symbol, qty=qty, side=order_side,
                time_in_force=TimeInForce.DAY, limit_price=limit_price,
            )
        else:
            request = MarketOrderRequest(
                symbol=alpaca_symbol, qty=qty, side=order_side,
                time_in_force=TimeInForce.DAY,
            )

        order = self.client.submit_order(request)
        bracket_info = f" [SL=${stop_loss_price} to be placed on fill]" if use_stop else ""
        logger.info("Order submitted: %s %s %s @ %s%s — status: %s",
                     side, qty, symbol, limit_price or "market", bracket_info,
                     str(getattr(order.status, "value", order.status)))
        return {
            "id": str(order.id),
            # alpaca-py OrderStatus is `(str, Enum)`. Plain `str(enum)`
            # returns 'OrderStatus.REJECTED' (the repr), not 'rejected'
            # (the value). `_order_accepted`'s rejection filter
            # lowercases and checks for the *value* form, so without
            # the .value unwrap a real broker rejection would slip past
            # as "accepted" and proceed through the pipeline (audit
            # 2026-05-27).
            "status": str(getattr(order.status, "value", order.status)),
            "symbol": _internal_symbol(order.symbol),
            # Echo back the parameters so downstream consumers (notifier,
            # audit log, finalize) can render orders without having to
            # join against the trades table for what was JUST submitted.
            # Pre-2026-05-12 this dict was {id, status, symbol} only and
            # the notifier could only show "BUY NVDA qty=?" — now it can
            # show "BUY NVDA qty=27 @$238.63 SL=$230".
            "side": side.lower(),
            "qty": qty,
            "limit_price": limit_price,
            "stop_loss_price": stop_loss_price if use_stop else None,
            # Signals the caller that this entry still OWES a protective stop
            # (see _place_entry_protection). Absent/None => nothing to place.
            "pending_stop_price": stop_loss_price if use_stop else None,
        }

    # 3% beyond the stop: a stop-MARKET fills at whatever the book has on a
    # gap (10%+ worse than the stop); a stop-limit caps the worst-case fill.
    # The buffer must be wide enough that routine volatility clears it
    # ("prioritize fill over price"). Trade-off: on gaps beyond 3% the limit
    # won't fill and the position stays open until a session can act.
    #
    # "Beyond", not "below": a long's protective order is a SELL stop, so
    # its limit sits 3% BELOW the trigger (a SELL needs its floor under the
    # stop to have room to fill on the way down). A short's protective order
    # is a BUY stop, so its limit must sit 3% ABOVE the trigger — a BUY
    # needs headroom over the stop to fill on the way up. Getting this
    # backwards for a short is silent: the order still submits, but the
    # limit sits on the wrong side of the trigger, so it can never fill.
    # The stop then "fires" and does nothing, and the position runs
    # unprotected in the one direction that matters.
    STOP_LIMIT_BUFFER_PCT = 0.03

    # Order states that mean "this order can never fill another share".
    _TERMINAL_ORDER_STATES = frozenset({
        "filled", "canceled", "cancelled", "expired", "rejected",
        "done_for_day", "stopped", "suspended",
    })

    # Bounded number of `replaced_by` hops to follow when resolving what a
    # replaced order became. Each re-peg adds exactly one hop and re-pegs are
    # capped in the low single digits, so 8 is generous; the bound exists so a
    # broker-side cycle or a pathological chain can never spin this forever.
    _MAX_REPLACEMENT_HOPS = 8

    def cancel_entry_order(self, order_id: str) -> bool:
        """Cancel one order by id. True when the broker accepted the cancel.

        A named seam rather than a raw `client.cancel_order_by_id` call so the
        re-peg race path — "the superseded order filled, kill the replacement
        before it buys the same idea again" — is explicit, mockable, and
        cannot be confused with `cancel_open_entry_orders`, which cancels
        every working entry for a symbol.
        """
        try:
            self.client.cancel_order_by_id(order_id)
            return True
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "cancel_entry_order: cancel of %s FAILED: %s — if this was a "
                "re-peg replacement racing a partial fill, the position may "
                "end up larger than intended; the next coverage reconcile "
                "must be checked", order_id, exc,
            )
            return False

    def resolve_replacement_chain(self, order_id: str) -> str | None:
        """Follow Alpaca's `replaced_by` links to the order that is live now.

        A replaced order keeps its own identity forever: status 'replaced',
        `filled_qty` frozen at whatever it filled before the swap, and
        `replaced_by` pointing at its successor. This walks that chain and
        returns the id at the end of it — which is the only id worth polling
        for a fill.

        Returns the input id unchanged when the order was never replaced.
        Returns None when the broker read FAILED, which callers must treat as
        "unknown, retry later" and never as "no replacement" — repointing a
        trades row on a failed read would be inventing a fact.
        """
        current = str(order_id)
        for _ in range(self._MAX_REPLACEMENT_HOPS):
            try:
                order = self.client.get_order_by_id(current)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "resolve_replacement_chain: broker read failed for %s: %s "
                    "— returning None so the caller retries rather than "
                    "concluding the order was never replaced", current, exc,
                )
                return None
            status = str(
                getattr(getattr(order, "status", None), "value",
                        getattr(order, "status", ""))
            ).lower()
            successor = getattr(order, "replaced_by", None)
            successor = str(successor) if successor else ""
            if status != "replaced" or not successor or successor == current:
                return current
            current = successor
        logger.error(
            "resolve_replacement_chain: %s exceeded %d hops — refusing to "
            "keep walking", order_id, self._MAX_REPLACEMENT_HOPS,
        )
        return None

    def replace_entry_limit(
        self, order_id: str, new_limit_price: float, *, qty: float | None = None,
    ) -> dict:
        """PATCH a working entry limit to a new price. Returns the NEW order id.

        This is the only place in the codebase that calls Alpaca's replace
        endpoint, and the reason it is wrapped rather than inlined is the
        footgun: **the replacement is a different order**. The response
        carries a new id; the id passed in is dead from that moment.

        `qty` is passed through explicitly rather than left to the broker's
        default. The caller only ever re-pegs an order that has filled ZERO
        shares, so "remaining" and "original" are the same number here — but
        stating it removes any dependence on how the endpoint interprets an
        omitted qty against a partially filled order, which is exactly the
        ambiguity that turns a re-peg into an over-buy.

        Never raises. Failure shapes, all with `id=None`:
          - 'replace_invalid_price' — nothing was sent to the broker.
          - 'replace_rejected'      — the broker refused. The overwhelmingly
            likely cause is that the order reached a terminal state (it
            FILLED) between the caller's check and this call. The caller must
            re-read the ORIGINAL id, which is still authoritative in that
            case, and must not retry blindly.
        """
        price = _quantize_price(new_limit_price)
        if price is None or price <= 0:
            logger.warning(
                "replace_entry_limit refused for %s: non-quotable price %r",
                order_id, new_limit_price,
            )
            return {"id": None, "status": "replace_invalid_price"}

        # Spec §11.1: a FRACTIONAL entry cannot be re-pegged. Alpaca's
        # ReplaceOrderRequest types `qty` as an int, and the two ways out of
        # that are both worse than refusing: truncating 1.5625 to 1 silently
        # SHRINKS a position the risk math already sized, and omitting qty
        # reintroduces exactly the "how does the endpoint read an omitted qty"
        # ambiguity this wrapper documents itself as removing. Refusing means
        # the original order stays authoritative and simply does not chase —
        # the caller's existing `id=None` path, and the safe direction.
        try:
            is_fractional = qty is not None and not float(qty).is_integer()
        except (TypeError, ValueError):
            is_fractional = False
        if is_fractional:
            logger.info(
                "replace_entry_limit refused for %s: fractional qty %s cannot "
                "be re-pegged — the original order remains authoritative",
                order_id, qty,
            )
            return {"id": None, "status": "replace_unsupported_fractional_qty"}

        kwargs: dict = {"limit_price": price}
        if qty is not None:
            try:
                int_qty = int(qty)
            except (TypeError, ValueError):
                int_qty = 0
            if int_qty > 0:
                kwargs["qty"] = int_qty

        try:
            order = self.client.replace_order_by_id(
                order_id, ReplaceOrderRequest(**kwargs),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "replace_entry_limit: broker refused replacement of %s at "
                "$%.4f: %s — the order most likely reached a terminal state "
                "(filled) first; the ORIGINAL id remains authoritative",
                order_id, price, exc,
            )
            return {"id": None, "status": "replace_rejected", "detail": str(exc)}

        new_id = str(getattr(order, "id", "") or "")
        if not new_id:
            logger.error(
                "replace_entry_limit: broker accepted the replacement of %s "
                "but returned no order id — treating as rejected so the "
                "caller keeps polling the original", order_id,
            )
            return {"id": None, "status": "replace_rejected"}
        status = str(
            getattr(getattr(order, "status", None), "value",
                    getattr(order, "status", ""))
        ).lower()
        logger.info(
            "replace_entry_limit: %s → %s @ $%.4f (status %s)",
            order_id, new_id, price, status or "unknown",
        )
        return {
            "id": new_id, "status": status or "accepted",
            "limit_price": price, "replaces": str(order_id),
        }

    def place_entry_protection(
        self, symbol: str, order_id: str, stop_price: float,
        *, requested_qty: float | None = None, side: str = "buy",
        superseded_filled_qty: float = 0.0,
    ) -> dict | None:
        """Wait for an entry order to reach terminal, then place a GTC
        protective stop-limit for the ACTUAL filled qty.

        If the entry is STILL WORKING after the wait (slow tape, wide limit),
        the unfilled remainder is CANCELLED first — audit round 2: the 15s
        wait treated "still live" identically to "terminal 0-fill" and walked
        away, so a DAY entry limit could fill hours later with no stop
        watching it (and a resting BUY could even re-buy into a crash after an
        emergency liquidation). Cancelling converges the order; whatever DID
        fill by then gets its stop from the post-cancel re-read. Losing the
        unfilled remainder is the accepted cost of protection-first.

        `side` is the ENTRY order's own side — "buy" opens or adds to a long
        (the only side any order path in this repo has ever submitted, hence
        the default), "sell"/"sell_short" opens a short. The protective stop
        is always the OPPOSITE side, at the opposite buffer: a SELL stop
        below a long, a BUY stop above a short. See `STOP_LIMIT_BUFFER_PCT`.

        `superseded_filled_qty` is shares this entry already acquired under a
        DIFFERENT order id — the ancestors of a re-peg chain. `order_id` is
        the last order in that chain, and Alpaca's fill counters do not carry
        across a replacement, so the shares an ancestor filled are invisible
        here. They are real shares in a real position, and a stop sized to
        only the last order's fill would leave them naked. Adding them is what
        keeps the invariant "every filled share is under a stop" true across a
        re-peg. Default 0.0: for every caller that never re-pegs, this method
        behaves exactly as it did before.

        Returns the stop order dict, or None when nothing was placed (entry
        filled 0 / stop submit failed). Never raises — a failure here must not
        abort the session.

        Spec §11.1 guard 1: the stop submission now RETRIES immediately and
        hard before giving up (`_submit_protective_stop_retrying`). A None
        return therefore means the retries were exhausted, and the position is
        naked — the CALLER owes an owner alert on it (guard 2); the
        coverage-reconcile auto-repair belt remains the backstop, not the
        first line.
        """
        # Fail closed on a side we do not recognise, BEFORE touching the
        # broker. `"sell" if side == "buy" else "buy"` reads harmlessly but is
        # fail-OPEN: a typo, a None, or some future side string falls into the
        # short branch, and a LONG then gets a BUY stop placed ABOVE it — not
        # weak protection, but a standing order to buy more of a position that
        # is already losing.
        #
        # This returns rather than raising, because the contract above is that
        # this function never aborts a session. Returning None is the same
        # outcome as any other protection failure: logged at ERROR, position
        # left naked-but-KNOWN, and picked up by the coverage-reconcile
        # auto-repair belt. Naked-and-believed-covered is the state that
        # actually costs money, and refusing here is what prevents it.
        normalized = (side or "").strip().lower()
        if normalized not in _ENTRY_SIDES:
            logger.error(
                "entry protection: %s refusing to guess a protective side for "
                "entry side %r (expected one of %s) — NO stop placed, position "
                "will be left uncovered and must be repaired by reconcile",
                symbol, side, sorted(_ENTRY_SIDES),
            )
            return None

        try:
            status = self.wait_for_order_terminal(
                order_id, timeout_seconds=_ENTRY_FILL_TIMEOUT_S,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("entry protection: wait failed for %s (%s): %s",
                           symbol, order_id, exc)
            status = None

        if (status or "").lower() not in self._TERMINAL_ORDER_STATES:
            # Still working — cancel the remainder so it can't fill unwatched.
            # A fill can land during cancel propagation; the post-cancel
            # re-read below protects whatever landed.
            logger.warning(
                "entry protection: %s entry %s still working after wait "
                "(status=%s) — cancelling the unfilled remainder so no share "
                "can fill without a stop watching it",
                symbol, order_id, status or "unknown",
            )
            try:
                self.client.cancel_order_by_id(order_id)
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "entry protection: cancel of still-working entry %s (%s) "
                    "failed: %s — a later fill will be UNPROTECTED until the "
                    "next coverage reconcile", symbol, order_id, exc,
                )
            try:
                self.wait_for_order_terminal(order_id, timeout_seconds=10.0)
            except Exception:  # noqa: BLE001
                pass

        try:
            info = self.get_order_fill_info(order_id) or {}
        except Exception as exc:  # noqa: BLE001
            logger.warning("entry protection: fill info failed for %s: %s", symbol, exc)
            info = {}
        try:
            filled_qty = float(info.get("filled_qty") or 0)
        except (TypeError, ValueError):
            filled_qty = 0.0
        try:
            carried = float(superseded_filled_qty or 0)
        except (TypeError, ValueError):
            carried = 0.0
        if carried > 0:
            logger.info(
                "entry protection: %s carries %.4f share(s) filled under a "
                "superseded order id; stop will cover %.4f + %.4f",
                symbol, carried, filled_qty, carried,
            )
            filled_qty += carried
        if filled_qty <= 0:
            logger.warning(
                "entry protection: %s entry %s filled 0 (status=%s) — no stop "
                "placed (nothing to protect)", symbol, order_id, status or "unknown",
            )
            return None
        if requested_qty and filled_qty < requested_qty:
            logger.warning(
                "entry protection: %s partially filled %.4f/%.4f — stop sized to "
                "the ACTUAL fill", symbol, filled_qty, requested_qty,
            )
        # The protective order's side is the OPPOSITE of the entry's: a BUY
        # entry (long) is protected by a SELL stop below it; a SELL/SELL_SHORT
        # entry (short) is protected by a BUY stop above it. The buffer
        # mirrors the same way — see STOP_LIMIT_BUFFER_PCT above. Getting
        # this backwards is THE most dangerous bug in shorts-safe: the order
        # still submits without error, it just sits on the wrong side of the
        # trigger and can never fill, so the position runs unprotected in
        # exactly the direction it needed protecting.
        protective_side = "sell" if normalized == "buy" else "buy"
        buffer_mult = (
            (1 - self.STOP_LIMIT_BUFFER_PCT) if protective_side == "sell"
            else (1 + self.STOP_LIMIT_BUFFER_PCT)
        )
        stop_order = self._submit_protective_stop_retrying(
            symbol=symbol, qty=filled_qty, stop_price=stop_price,
            limit_price=stop_price * buffer_mult, side=protective_side,
        )
        if stop_order is None:
            logger.error(
                "entry protection FAILED for %s (%.4f shares held, stop $%.2f) "
                "after %d attempt(s) — position is UNPROTECTED; the caller must "
                "raise an OWNER alert (spec §11.1 guard 2) and the coverage "
                "reconcile must repair it",
                symbol, filled_qty, stop_price, _STOP_PLACEMENT_MAX_ATTEMPTS,
            )
            return None
        return stop_order

    def _submit_protective_stop_retrying(
        self, *, symbol: str, qty: float, stop_price: float,
        limit_price: float | None, side: str,
    ) -> dict | None:
        """Spec §11.1 guard 1 — submit a protective stop, retrying immediately
        and hard on failure. Returns the stop order dict, or None when every
        attempt failed.

        The retry happens HERE, in the same call, milliseconds after the
        failure — not queued, not deferred to the next 30-minute sweep. The
        position is already open; a deferred retry is an open position with
        no stop for however long the defer lasts, which is the exact failure
        mode §11.1 was required to bound. See `_STOP_PLACEMENT_MAX_ATTEMPTS`
        for why the budget is three attempts over ~2 seconds and not more.

        Never raises. Returning None is the signal the CALLER must escalate
        on — a naked position that nobody is told about is strictly worse
        than one that fails loudly.

        On a fractional fill whose exact qty the broker refuses (see the
        §11.1 open question about whether Alpaca carries a stop for a
        fractional quantity at all), the final attempt drops to the
        WHOLE-SHARE floor of the fill: a stop over `floor(qty)` shares leaves
        a sub-share residual uncovered, which is a far smaller and strictly
        bounded exposure than leaving the entire position naked. ONLY in that
        fallback does the returned dict carry `covered_qty`/`uncovered_qty` —
        the ordinary success path returns the broker's response untouched, so
        nothing downstream sees a new shape on the common path. The caller
        alerts the owner on a non-zero `uncovered_qty` exactly as it would on
        an outright failure.
        """
        attempts = max(1, int(_STOP_PLACEMENT_MAX_ATTEMPTS))
        for attempt in range(1, attempts + 1):
            try:
                order = self._submit_stop_limit_order(
                    symbol=symbol, qty=qty, stop_price=stop_price,
                    limit_price=limit_price, side=side,
                )
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "protective stop attempt %d/%d FAILED for %s (qty=%.4f, "
                    "stop $%.2f): %s", attempt, attempts, symbol, qty,
                    stop_price, exc,
                )
                if attempt < attempts:
                    delay = _STOP_PLACEMENT_BACKOFF_S[
                        min(attempt - 1, len(_STOP_PLACEMENT_BACKOFF_S) - 1)
                    ]
                    time.sleep(delay)
                continue
            if attempt > 1:
                logger.warning(
                    "protective stop placed for %s on attempt %d/%d — the "
                    "position was briefly unprotected and is now covered",
                    symbol, attempt, attempts,
                )
            else:
                logger.info(
                    "entry protection: GTC %s stop-limit placed for %s "
                    "qty=%.4f @ stop $%.2f", side, symbol, qty, stop_price,
                )
            return order

        # Every attempt at the exact quantity failed. If that quantity was
        # FRACTIONAL, one cause is a broker that will not carry a stop for a
        # fractional qty at all — in which case the whole-share floor is a
        # request it can accept, and covering floor(qty) beats covering none.
        whole = float(int(qty))
        if whole >= 1 and whole < qty:
            logger.critical(
                "protective stop: %s exhausted %d attempt(s) at the exact "
                "fractional qty %.4f — falling back to a WHOLE-SHARE stop for "
                "%.0f share(s). If this succeeds, %.4f share(s) remain "
                "UNCOVERED and the owner is alerted.",
                symbol, attempts, qty, whole, qty - whole,
            )
            try:
                order = self._submit_stop_limit_order(
                    symbol=symbol, qty=whole, stop_price=stop_price,
                    limit_price=limit_price, side=side,
                )
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "protective stop: whole-share fallback ALSO failed for %s "
                    "(qty=%.0f, stop $%.2f): %s", symbol, whole, stop_price, exc,
                )
                return None
            if isinstance(order, dict):
                # A COPY — never annotate the broker's own response object in
                # place; a caller holding that dict must not have its shape
                # changed underneath it.
                order = {**order, "covered_qty": whole,
                         "uncovered_qty": qty - whole}
            return order
        return None

    def close_position(self, symbol: str) -> dict:
        order = self.client.close_position(_alpaca_symbol(symbol))
        logger.info("Closed position: %s", symbol)
        # Unwrap OrderStatus enum value (see submit_order — same reason).
        return {"id": str(order.id),
                "status": str(getattr(order.status, "value", order.status))}

    def _list_open_stop_orders_by_side(self, symbol: str) -> tuple[list, list]:
        """Single order-book fetch for `symbol`, split into (sell_stops, buy_stops).

        A long's protective stop is a SELL stop; a short's is a BUY stop.
        `replace_stop_loss` needs to know WHICH side a symbol's live stop is
        on before it can decide what to list/cancel/ratchet-check — but it
        can't yet know the position's direction without a second API call.
        Fetching once and filtering both ways here answers "which side has a
        stop" from a single snapshot, rather than two separate fetches that
        could each see a different broker state.
        """
        try:
            from alpaca.trading.requests import GetOrdersRequest

            orders = self.client.get_orders(
                filter=GetOrdersRequest(
                    status=QueryOrderStatus.OPEN,
                    symbols=[_alpaca_symbol(symbol)],
                    nested=True,
                )
            )
        except Exception as exc:
            logger.warning("replace_stop_loss: failed to list open orders for %s: %s", symbol, exc)
            return [], []

        sell_orders: list = []
        buy_orders: list = []
        for order in orders or []:
            order_type = str(getattr(getattr(order, "order_type", None), "value",
                                    getattr(order, "order_type", ""))).lower()
            if "stop" not in order_type:
                continue
            order_side = str(getattr(getattr(order, "side", None), "value",
                                    getattr(order, "side", ""))).lower()
            if order_side == "sell":
                sell_orders.append(order)
            elif order_side == "buy":
                buy_orders.append(order)
        return sell_orders, buy_orders

    def _list_open_protective_stop_orders(self, symbol: str, *, side: str = "sell") -> list:
        """List open stop orders on `side` for `symbol`.

        `side="sell"` (default) finds the stops protecting a long — the only
        case that existed before shorts were countable, and delegates to
        `_list_open_sell_stop_orders` (the name a long list of tests and
        call sites pin) rather than duplicating it. `side="buy"` finds the
        stops protecting a short: a short's protective order is a BUY stop,
        so a filter hardcoded to "sell" made a short's live stop invisible
        to every caller (coverage reconcile would report a perfectly
        protected short as NAKED and try to "repair" over it).
        """
        if side.lower() == "buy":
            _, buy_orders = self._list_open_stop_orders_by_side(symbol)
            return buy_orders
        return self._list_open_sell_stop_orders(symbol)

    def _list_open_sell_stop_orders(self, symbol: str) -> list:
        try:
            from alpaca.trading.requests import GetOrdersRequest

            orders = self.client.get_orders(
                filter=GetOrdersRequest(
                    status=QueryOrderStatus.OPEN,
                    symbols=[_alpaca_symbol(symbol)],
                    nested=True,
                )
            )
        except Exception as exc:
            logger.warning("replace_stop_loss: failed to list open orders for %s: %s", symbol, exc)
            return []

        stop_orders = []
        for order in orders or []:
            order_type = str(getattr(getattr(order, "order_type", None), "value",
                                    getattr(order, "order_type", ""))).lower()
            order_side = str(getattr(getattr(order, "side", None), "value",
                                    getattr(order, "side", ""))).lower()
            if "stop" in order_type and order_side == "sell":
                stop_orders.append(order)
        return stop_orders

    @staticmethod
    def _snapshot_stop_order(order) -> dict | None:
        try:
            qty = float(getattr(order, "qty", 0) or 0)
        except (TypeError, ValueError):
            qty = 0.0
        try:
            stop_price = float(getattr(order, "stop_price", 0) or 0)
        except (TypeError, ValueError):
            stop_price = 0.0
        try:
            limit_price = float(getattr(order, "limit_price", 0) or 0)
        except (TypeError, ValueError):
            limit_price = 0.0
        if qty <= 0 or stop_price <= 0:
            return None
        return {
            "id": str(order.id),
            "qty": qty,
            "stop_price": stop_price,
            "limit_price": limit_price or None,
        }

    def _submit_stop_limit_order(
        self,
        symbol: str,
        qty: float,
        stop_price: float,
        limit_price: float | None = None,
        *,
        side: str = "sell",
    ) -> dict:
        """Submit a GTC stop-limit order. `side` is the STOP ORDER's own
        side — "sell" (default) protects a long and fires as price falls;
        "buy" protects a short and fires as price rises. Defaults to "sell"
        so every pre-shorts call site (none of which pass `side`) submits
        byte-identical orders to before.

        When `limit_price` is not supplied, the fallback buffer must sit on
        the correct side of the trigger too: a SELL's limit belongs BELOW
        the stop (same STOP_LIMIT_BUFFER_PCT the entry-protection path
        uses), a BUY's belongs ABOVE it. A SELL limit placed above its stop,
        or a BUY limit placed below its, can never fill — the order looks
        accepted but is dead on arrival.
        """
        order_side = OrderSide.BUY if side.lower() == "buy" else OrderSide.SELL
        stop_price_q = _quantize_price(stop_price)
        if limit_price and limit_price > 0:
            limit_price_q = _quantize_price(limit_price)
        else:
            buffer_mult = (
                (1 + self.STOP_LIMIT_BUFFER_PCT) if order_side == OrderSide.BUY
                else (1 - self.STOP_LIMIT_BUFFER_PCT)
            )
            limit_price_q = _quantize_price(stop_price * buffer_mult)
        req = StopLimitOrderRequest(
            symbol=_alpaca_symbol(symbol),
            qty=qty,
            side=order_side,
            time_in_force=TimeInForce.GTC,
            stop_price=stop_price_q,
            limit_price=limit_price_q,
        )
        order = self.client.submit_order(req)
        # Unwrap OrderStatus enum value (see submit_order — same reason).
        return {"id": str(order.id),
                "status": str(getattr(order.status, "value", order.status)),
                "symbol": _internal_symbol(symbol)}

    def _restore_stop_orders(
        self, symbol: str, stop_specs: list[dict],
        *,
        check_idempotency: bool = False,
        side: str = "sell",
    ) -> tuple[int, list[dict]]:
        """Re-submit a set of cancelled stop specs. Best-effort per-spec —
        a single broker rejection doesn't abort the loop.

        `side` is the specs' own side — "sell" (default) restores stops that
        protect a long. `replace_stop_loss` is the one caller that can pass
        `side="buy"`, when the position it's trailing is a short; every
        other caller only ever restores a long's SELL stops, so the default
        keeps them unchanged.

        ``check_idempotency`` controls whether we first query broker for
        already-alive stops and skip matching specs:

        - **False (default; in-line rollback path)** — the caller just
          cancelled these specs moments ago in the same method
          (cancel_protective_stops or replace_stop_loss partial-cancel
          rollback). The cancelled stops are not alive anymore at the
          broker by construction; checking would just slow the rollback
          and risk false-positives on Alpaca's eventual-consistency
          window (pending_cancel orders sometimes still appear in
          get_orders briefly).

        - **True (drain path)** — the caller is replaying a recovery
          intent persisted from a previous session. Specs that landed
          successfully in an earlier drain pass are alive at the broker;
          re-submitting them now would trigger held_for_orders /
          duplicate-protection rejections. Query open sell-stops first
          and skip specs whose (qty, stop_price) match a live stop
          within 1¢ tolerance. This closes the drain re-submission race
          documented in the design audit — finalize's
          reprotect-raised / restore-raised paths return the full
          cancelled_specs list, and drain's length-equality narrowing
          (pipeline.py:1039) can't distinguish "all failed" from
          "partial succeeded then raised", so it leaves the row's
          specs unchanged. Idempotency defends against the resulting
          re-submit dupes at the broker layer.

        Returns ``(restored_count, failed_specs)``. With idempotency on,
        ``restored_count`` includes already-alive-skipped specs (from
        the caller's perspective, coverage is intact either way).
        """
        existing_alive: list[dict] = []
        if check_idempotency:
            try:
                for order in self._list_open_protective_stop_orders(symbol, side=side):
                    snap = self._snapshot_stop_order(order)
                    if snap is not None:
                        existing_alive.append(snap)
            except Exception as exc:
                # If we can't see existing stops, fall through to the
                # non-idempotent behavior — broker's own duplicate
                # detection is the last line.
                logger.warning(
                    "_restore_stop_orders: failed to list existing stops for %s "
                    "(idempotency check skipped): %s",
                    symbol, exc,
                )

        def _spec_matches(spec: dict, alive: dict) -> bool:
            """Two specs match when qty and stop_price are within rounding."""
            try:
                if abs(float(spec.get("qty", 0)) - float(alive.get("qty", 0))) > 1e-6:
                    return False
                spec_stop = float(spec.get("stop_price", 0))
                alive_stop = float(alive.get("stop_price", 0))
                # 1 cent tolerance covers _quantize_price rounding.
                return abs(spec_stop - alive_stop) <= 0.01
            except (TypeError, ValueError):
                return False

        restored = 0
        skipped_already_alive = 0
        failed_specs: list[dict] = []
        for spec in stop_specs:
            if existing_alive and any(_spec_matches(spec, alive) for alive in existing_alive):
                # Already alive at broker (likely landed in a prior
                # drain pass). Treat as restored from the caller's
                # perspective; do NOT re-submit.
                skipped_already_alive += 1
                restored += 1
                logger.info(
                    "_restore_stop_orders: %s @ $%.2f qty=%s already alive "
                    "at broker — skipping re-submit (idempotent)",
                    symbol, float(spec.get("stop_price", 0)), spec.get("qty"),
                )
                continue
            try:
                self._submit_stop_limit_order(
                    symbol=symbol,
                    qty=spec["qty"],
                    stop_price=spec["stop_price"],
                    limit_price=spec.get("limit_price"),
                    side=side,
                )
                restored += 1
            except Exception as exc:
                logger.error(
                    "replace_stop_loss: failed to restore prior stop for %s @ $%.2f: %s",
                    symbol, spec["stop_price"], exc,
                )
                failed_specs.append(spec)
        if restored:
            new_submits = restored - skipped_already_alive
            if skipped_already_alive:
                logger.warning(
                    "replace_stop_loss rollback: restored %d/%d prior stop order(s) "
                    "for %s (%d newly submitted, %d already alive)",
                    restored, len(stop_specs), symbol, new_submits, skipped_already_alive,
                )
            else:
                logger.warning(
                    "replace_stop_loss rollback: restored %d/%d prior stop order(s) for %s",
                    restored, len(stop_specs), symbol,
                )
        return restored, failed_specs

    def shift_stops_down(self, symbol: str, amount: float) -> dict | None:
        """Lower EVERY open sell-stop for `symbol` by `amount`, preserving
        each stop's own level and qty.

        Ex-dividend flow (audit round 2): the old path read ONE stop level
        (first-match) and replace_stop_loss'd ALL stops with a single
        consolidated order — with per-BUY GTC stops now the steady state,
        that collapsed distinct per-lot levels into one and could TIGHTEN a
        wide lot's stop to the tightest lot's level. Shifting each spec
        keeps the per-lot geometry and just absorbs the mechanical gap.

        Returns {"id", "status", "symbol", "shifted", "total"} (id of the
        first re-placed stop) or None when nothing was shifted. Best-effort
        with rollback: cancel failures roll back already-cancelled stops;
        re-place failures restore the ORIGINAL spec for that stop.

        SELL-stops only, deliberately not generalised to a short's BUY-stop
        (shorts-safe, Stage 2): the caller (`pipeline._handle_ex_dividends`)
        already excludes shorts before reaching this method, because the
        economics are genuinely different, not just the arithmetic sign — a
        long owns the shares and receives the dividend (the mechanical
        gap-down needs absorbing); a short instead OWES the dividend to the
        lender, a cash liability with no corresponding price-gap-absorption
        logic here. Mirroring the sign without modelling that liability
        would be a guess, not a fix.
        """
        if amount <= 0:
            return None
        specs: list[dict] = []
        for order in self._list_open_sell_stop_orders(symbol):
            spec = self._snapshot_stop_order(order)
            if spec is None:
                logger.warning(
                    "shift_stops_down: cannot snapshot stop %s for %s — aborting",
                    getattr(order, "id", "<unknown>"), symbol,
                )
                return None
            specs.append(spec)
        if not specs:
            return None
        if not self.cancel_snapshotted_stops(symbol, specs):
            return None   # rollback already handled inside
        shifted = [{
            **spec,
            "stop_price": _quantize_price(spec["stop_price"] - amount),
            "limit_price": (_quantize_price(spec["limit_price"] - amount)
                            if spec.get("limit_price") else None),
        } for spec in specs]
        restored, failed = self._restore_stop_orders(symbol, shifted)
        if failed:
            # Put the ORIGINAL levels back for whatever couldn't be shifted —
            # protection at the old level beats no protection.
            originals = [s for s in specs if any(
                f.get("qty") == s["qty"] and abs(f.get("stop_price", 0) -
                (s["stop_price"] - amount)) < 0.02 for f in failed)]
            if originals:
                self._restore_stop_orders(symbol, originals)
            logger.error(
                "shift_stops_down: %d/%d stop(s) failed to shift for %s — "
                "originals restored where possible", len(failed), len(specs), symbol,
            )
        if restored <= 0:
            return None
        return {"id": f"shift-{symbol}", "status": "accepted", "symbol": symbol,
                "shifted": restored, "total": len(specs)}

    def replace_stop_loss(
        self,
        symbol: str,
        new_stop_price: float,
        *,
        allow_lowering: bool = False,
    ) -> dict | None:
        """Replace an existing protective stop with rollback so protection is preserved on failure.

        Used by the midday trailing-stop logic. Alpaca's OTO stop-loss leg cannot be edited
        in place, so we cancel + resubmit. Because that sequence is not atomic, this method
        snapshots existing stops and best-effort restores them if the replacement submit fails.
        Returns {id, status, symbol} on successful replacement, else None.

        A short's protective stop is a BUY stop above the market, and
        "trailing" for a short means ratcheting it DOWN — the mirror of a
        long's stop-only-rises rule. Direction is read from whichever side
        ALREADY has a live stop (`_list_open_stop_orders_by_side` checks
        both with one fetch, since a long-only "sell" listing would make a
        short's BUY stop invisible); the position's own qty sign — read
        below to confirm the position exists, same as before shorts were
        possible — is the authoritative vote once there's something to cross-
        check it against, and the sole vote when there was no live stop yet
        to infer direction from.
        """
        if new_stop_price <= 0:
            logger.warning("replace_stop_loss ignored: non-positive new_stop_price=%s", new_stop_price)
            return None

        sell_orders, buy_orders = self._list_open_stop_orders_by_side(symbol)
        if sell_orders and buy_orders:
            # A single symbol can't legitimately be both long and short at
            # once, so live stops on both sides means stale orders survived
            # a direction flip. Reporting/acting on either would be a guess
            # — fail closed instead.
            logger.error(
                "replace_stop_loss: %s carries BOTH sell-stops and buy-stops "
                "— direction is ambiguous, refusing to trail", symbol,
            )
            return None
        # "sell" is also the default when NEITHER side has a live stop yet;
        # the position check below is what actually decides direction in
        # that case (see the qty_side cross-check).
        side = "buy" if buy_orders else "sell"

        stop_specs: list[dict] = []
        for order in (buy_orders or sell_orders):
            spec = self._snapshot_stop_order(order)
            if spec is None:
                logger.warning(
                    "replace_stop_loss: cannot safely snapshot existing stop %s for %s; aborting replacement",
                    getattr(order, "id", "<unknown>"), symbol,
                )
                return None
            stop_specs.append(spec)

        # Direction check: "trailing" means the stop moves toward less risk
        # — UP for a long, DOWN for a short — never the other way. If the
        # LLM hallucinates a stop on the wrong side (or the caller passes the
        # wrong value), accepting it would weaken existing protection. Ex-
        # dividend adjustments intentionally lower a LONG's stop to absorb
        # tomorrow's mechanical dividend gap, so that caller opts in via
        # allow_lowering=True — ex-div's own position loop never reaches a
        # short (see pipeline.py's `_handle_ex_dividends`), so this flag is
        # not something a short's trail can accidentally trip.
        if stop_specs and not allow_lowering:
            if side == "buy":
                tightest_existing = min(spec["stop_price"] for spec in stop_specs)
                if new_stop_price >= tightest_existing:
                    logger.warning(
                        "replace_stop_loss rejected for %s: new_stop $%.4f is "
                        "not below lowest existing buy-stop $%.4f — a "
                        "short's trailing stop must ratchet down only "
                        "(protection would weaken).",
                        symbol, new_stop_price, tightest_existing,
                    )
                    return None
            else:
                tightest_existing = max(spec["stop_price"] for spec in stop_specs)
                if new_stop_price <= tightest_existing:
                    logger.warning(
                        "replace_stop_loss rejected for %s: new_stop $%.4f is not "
                        "above highest existing stop $%.4f — trailing stops must "
                        "ratchet up only (protection would weaken).",
                        symbol, new_stop_price, tightest_existing,
                    )
                    return None

        positions = [p for p in self.get_positions() if p.symbol == symbol]
        if not positions or positions[0].qty == 0:
            logger.warning("replace_stop_loss: no open position in %s, nothing to protect", symbol)
            return None
        qty_side = "buy" if positions[0].qty < 0 else "sell"
        if stop_specs and qty_side != side:
            # Live stops on one side, but the held position is on the other
            # — the same stale-order shape as the both-sides check above,
            # just caught against the position instead of the order book.
            logger.error(
                "replace_stop_loss: %s has live %s-stop(s) but qty=%.4f says "
                "the opposite side — refusing to trail an ambiguous position",
                symbol, side, positions[0].qty,
            )
            return None
        side = qty_side  # authoritative now that a position confirms direction

        cancelled_specs: list[dict] = []
        for spec in stop_specs:
            try:
                self.client.cancel_order_by_id(spec["id"])
                cancelled_specs.append(spec)
            except Exception as exc:
                logger.warning("replace_stop_loss: cancel failed for order %s: %s", spec["id"], exc)
                # Always restore whatever we already cancelled. The previous
                # "if no open stops remain" gate was wrong for partial
                # failures: with [A, B, C], if A and B cancel cleanly and C
                # fails, the broker now shows [C] — the gate sees something
                # open and skips restore, leaving A's and B's qty
                # unprotected. Restore is safe even when C is still live;
                # at worst we end up with slightly more stops than minimal,
                # but full original coverage is preserved.
                if cancelled_specs:
                    restored, _failed = self._restore_stop_orders(symbol, cancelled_specs, side=side)
                    logger.warning(
                        "replace_stop_loss: rolled back %d/%d already-cancelled "
                        "stop(s) for %s after partial cancel failure",
                        restored, len(cancelled_specs), symbol,
                    )
                return None

        # Re-read position right before submit — in the sub-second window
        # between our cancel-stops and this submit, the position may have
        # been closed (liquidated by another path, or market-sold into a
        # fill). If it's gone, the new-stop submit would fail with a qty
        # mismatch AND our rollback would then re-attach a phantom stop to
        # a non-existent position. Bail cleanly in that case.
        fresh_positions = [p for p in self.get_positions() if p.symbol == symbol]
        if not fresh_positions or fresh_positions[0].qty == 0:
            logger.warning(
                "replace_stop_loss: %s was closed between cancel and submit; "
                "NOT restoring old stops (position no longer exists)",
                symbol,
            )
            return None
        # Order qty is always the unsigned share count — the SIDE parameter
        # carries direction. `fresh_positions[0].qty` is negative for a
        # short; submitting that raw would hand Alpaca a negative qty.
        qty = abs(fresh_positions[0].qty)
        try:
            order = self._submit_stop_limit_order(
                symbol=symbol, qty=qty, stop_price=new_stop_price, side=side,
            )
            logger.info(
                "Trailing stop placed for %s: replaced %d old stop(s), new %s stop @ $%.2f",
                symbol, len(cancelled_specs), side, new_stop_price,
            )
            return order
        except Exception as exc:
            logger.error("replace_stop_loss: failed to submit new stop for %s: %s", symbol, exc)
            # The Alpaca QueryOrderStatus.OPEN filter INCLUDES transitional
            # statuses (pending_cancel / pending_replace), so the orders we
            # just cancelled can still appear in this list for ~1s after the
            # cancel call returns AND a *different* stop placed by another
            # path could itself be in pending_cancel. Three things must all
            # be true for "visible" to count as real protection:
            #   1. the order's id is NOT in cancelled_specs (PR #75)
            #   2. the order's status is in an active state, not pending_*
            #   3. the *sum* of active stop qtys covers the current position
            # Miss any of those and `cancelled_specs` must be restored.
            ACTIVE_STATUSES = {"new", "accepted", "held", "partially_filled"}

            def _is_live_protection(order) -> bool:
                if str(getattr(order, "id", "")) in cancelled_ids:
                    return False
                status_attr = getattr(order, "status", None)
                status = str(getattr(status_attr, "value", status_attr) or "").lower()
                return status in ACTIVE_STATUSES

            def _stop_qty(order) -> float:
                try:
                    return float(getattr(order, "qty", 0) or 0)
                except (TypeError, ValueError):
                    return 0.0

            cancelled_ids = {
                str(spec.get("id")) for spec in cancelled_specs if spec.get("id")
            }
            visible = self._list_open_protective_stop_orders(symbol, side=side)
            live_stops = [o for o in visible if _is_live_protection(o)]
            covered_qty = sum(_stop_qty(o) for o in live_stops)
            position_qty = qty  # captured pre-submit above; the position
                                # cannot have grown between then and now (this
                                # path doesn't BUY/SELL_SHORT to open), so this
                                # is an upper bound for required coverage.
            if live_stops and covered_qty >= position_qty:
                logger.warning(
                    "replace_stop_loss: %d active stop(s) cover %.4f >= position %.4f for %s after submit failure; leaving stop state unchanged",
                    len(live_stops), covered_qty, position_qty, symbol,
                )
                return None
            if live_stops:
                logger.warning(
                    "replace_stop_loss: %d active stop(s) cover only %.4f of %.4f shares for %s; restoring cancelled specs to close the gap",
                    len(live_stops), covered_qty, position_qty, symbol,
                )
            restored, _failed = self._restore_stop_orders(symbol, cancelled_specs, side=side)
            if restored == 0:
                logger.error(
                    "replace_stop_loss: %s has no confirmed stop protection after replacement failure",
                    symbol,
                )
            return None
