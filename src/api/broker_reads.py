"""Read-only Alpaca broker access for the Mission Control API.

CRITICAL SAFETY INVARIANT: this module must NEVER call, import, or reference
any broker method capable of creating, modifying, or cancelling anything —
no `submit_order`, `cancel_order`, `cancel_order_by_id`, `cancel_open_orders`,
`cancel_protective_stops`, `cancel_snapshotted_stops`, `close_position`,
`place_entry_protection`, or similar. This is a paper-trading production
system's live read surface; the only broker calls made here are
`get_account()`, `get_positions()`, `is_trading_day()` (all pre-existing
read-only methods on `AlpacaBroker`) and a fresh, locally-implemented
`client.get_orders(...)` read (mirroring the pattern at
`src/execution/broker.py:905-961`, `list_recent_orders`, but scoped to "all
open/closed/recent orders" rather than one symbol/side).

Every public function here is designed to NEVER raise: each wraps its own
broker call in try/except and returns a plain dict (or bool/None) carrying
an `error` field on failure, so route handlers in `routes_live.py` can
always build a valid response model instead of crashing into a 500 that
might leak internal details.
"""

from __future__ import annotations

import logging
from functools import lru_cache

from src.api.deps import (
    INVERSE_ETF_SYMBOLS,
    get_alpaca_credentials,
    get_alpaca_paper,
    get_cash_sweep_symbol,
    get_risk_limits,
)
from src.execution.broker import AlpacaBroker

logger = logging.getLogger(__name__)


def _position_direction(symbol: str, sweep_symbol: str) -> str:
    if symbol == sweep_symbol:
        return "cash_equivalent"
    if symbol in INVERSE_ETF_SYMBOLS:
        return "bearish_hedge"
    return "long"


@lru_cache(maxsize=1)
def _get_broker() -> AlpacaBroker:
    """Lazily-built, process-wide singleton `AlpacaBroker`.

    Built once from the narrow `deps.get_alpaca_credentials()` /
    `deps.get_alpaca_paper()` accessors — never from the full `AppConfig` —
    so this module never holds a reference to `ApiKeysConfig` or any other
    plaintext-secret-bearing object beyond the two strings needed to
    construct the SDK client.
    """
    key, secret = get_alpaca_credentials()
    paper = get_alpaca_paper()
    return AlpacaBroker(api_key=key, secret_key=secret, paper=paper)


def read_account() -> dict:
    """Best-effort read of `{cash, portfolio_value, last_equity}`.

    Never raises. On success: `{"cash": ..., "portfolio_value": ...,
    "last_equity": ..., "error": None}`. On failure: all three numeric
    fields `None`, `"error"` set to a short message.
    """
    try:
        broker = _get_broker()
        acct = broker.get_account()
        return {
            "cash": acct.get("cash"),
            "portfolio_value": acct.get("portfolio_value"),
            "last_equity": acct.get("last_equity"),
            "error": None,
        }
    except Exception as exc:
        logger.warning("broker_reads.read_account failed: %s", exc)
        return {
            "cash": None,
            "portfolio_value": None,
            "last_equity": None,
            "error": str(exc),
        }


def read_positions() -> dict:
    """Best-effort read of all open positions as plain dicts.

    Never raises. `{"positions": [...], "error": None|str}`.
    """
    try:
        broker = _get_broker()
        positions = broker.get_positions()
        try:
            sweep_symbol = get_cash_sweep_symbol()
        except Exception as exc:
            # A config-read failure must degrade only the direction/
            # is_cash_equivalent labeling, never the whole positions read —
            # same "one subsystem's failure never masks the rest" posture
            # as every other broker_reads function.
            logger.warning("broker_reads.read_positions: could not read cash_sweep symbol: %s", exc)
            sweep_symbol = None
        out = []
        for p in positions:
            out.append({
                "symbol": p.symbol,
                "qty": p.qty,
                "avg_entry": p.avg_entry,
                "current_price": p.current_price,
                "market_value": p.market_value,
                "unrealized_pnl": p.unrealized_pnl,
                "unrealized_intraday_pnl": getattr(p, "unrealized_intraday_pnl", None),
                "sector": getattr(p, "sector", None),
                "is_cash_equivalent": p.symbol == sweep_symbol,
                "direction": _position_direction(p.symbol, sweep_symbol),
            })
        return {"positions": out, "error": None}
    except Exception as exc:
        logger.warning("broker_reads.read_positions failed: %s", exc)
        return {"positions": [], "error": str(exc)}


def read_margin_interest(cash: float | None) -> dict:
    """Margin interest ESTIMATE (spec §11.2) — MEASURES only, never a risk
    decision, and never gates anything in this read-only module.

    Takes the account's already-read `cash` figure (the caller — `/account`
    — has just fetched it via `read_account()`; this avoids a second broker
    round-trip for the same number). Returns
    `{"debit_balance", "rate_pct", "daily_usd", "annual_usd", "label",
    "broker_check_note", "error"}`, every numeric field `None` when there
    is nothing to report — a zero/no debit balance (today's actual state:
    the account has never carried a negative cash balance) degrades to an
    all-`None` dict, never a fabricated zero-cost line. Never raises.

    Deliberately does NOT gate on `limits.allow_margin` before looking at
    `cash` — interest is a broker-side fact about the account's actual
    overnight cash balance, not a consequence of QAMC's own risk toggle.
    `cash_only` (src/risk/rules.py) hard-blocks a plain BUY from taking
    cash negative when `allow_margin` is `False`, but a COVER is exempt
    from that rule by design (D10 — a COVER can never be hard-blocked;
    see `src/agents/portfolio_manager.py`'s DE-LEVER MANDATE, which
    already handles "cash is negative AND allow_margin is False" as a
    real, live state). `overnight_debit_balance()` reads `cash` alone and
    already degrades cleanly to `0.0` when nothing was borrowed, so it is
    always safe — and now correct in the COVER-driven case too — to call
    unconditionally.
    """
    empty = {
        "debit_balance": None, "rate_pct": None, "daily_usd": None,
        "annual_usd": None, "label": None, "broker_check_note": None,
        "error": None,
    }
    try:
        rate_pct = get_risk_limits().margin_interest_rate_pct
    except Exception as exc:
        logger.warning("broker_reads.read_margin_interest: config read failed: %s", exc)
        return {**empty, "error": str(exc)}

    try:
        from src.margin_interest import build_estimate, overnight_debit_balance
        debit_balance = overnight_debit_balance(cash)
        estimate = build_estimate(debit_balance, rate_pct)
    except Exception as exc:
        logger.warning("broker_reads.read_margin_interest: estimate failed: %s", exc)
        return {**empty, "error": str(exc)}

    if estimate is None:
        return empty  # below the noise floor — silent, per spec's noise policy

    broker_check_note = None
    try:
        from src.margin_interest import compare_estimate_to_broker_activity
        broker = _get_broker()
        activities = broker.get_margin_interest_activities()
        comparison = compare_estimate_to_broker_activity(estimate, activities)
        if comparison is not None:
            broker_check_note = comparison.note
    except Exception as exc:
        # The INT-activity check is a nicety layered on top of the
        # ESTIMATE — its failure must never hide the estimate itself.
        logger.warning("broker_reads.read_margin_interest: INT-activity check failed: %s", exc)

    return {
        "debit_balance": estimate.debit_balance,
        "rate_pct": estimate.rate_pct,
        "daily_usd": estimate.daily_usd,
        "annual_usd": estimate.annual_usd,
        "label": estimate.label,
        "broker_check_note": broker_check_note,
        "error": None,
    }


_STATUS_MAP_NAMES = {"open": "OPEN", "closed": "CLOSED", "all": "ALL"}


def _extract_order_field(getter, default=None):
    """Run a single per-field extraction; on any error return `default`
    instead of propagating — one malformed field must never take down the
    whole order line item (and one malformed order must never take down
    the whole response)."""
    try:
        return getter()
    except Exception:
        return default


def _order_to_dict(o) -> dict:
    """Defensively flatten one Alpaca order SDK object into a plain dict.

    Mirrors the enum-vs-string / TypeError-vs-ValueError defensive style
    already used at `src/execution/broker.py:905-961`
    (`AlpacaBroker.list_recent_orders`), reimplemented fresh here rather
    than imported since this module must not touch write-capable broker
    internals. Every field is extracted independently so one bad field
    degrades to `None` rather than raising out of the whole row.
    """
    def _str_or_none(val):
        if val is None:
            return None
        return str(val)

    def _float_or_none(val):
        if val is None:
            return None
        try:
            return float(val)
        except (TypeError, ValueError):
            return None

    def _enum_value(attr_name):
        raw = getattr(o, attr_name, None)
        return str(getattr(raw, "value", raw)).lower() if raw is not None else None

    def _dt_str(attr_name):
        raw = getattr(o, attr_name, None)
        if raw is None:
            return None
        iso = getattr(raw, "isoformat", None)
        if callable(iso):
            try:
                return iso()
            except Exception:
                return str(raw)
        return str(raw)

    order_id = _extract_order_field(lambda: _str_or_none(getattr(o, "id", None)))
    symbol = _extract_order_field(lambda: _str_or_none(getattr(o, "symbol", None)))
    return {
        "id": order_id,
        "symbol": symbol,
        "side": _extract_order_field(lambda: _enum_value("side")),
        "qty": _extract_order_field(lambda: _float_or_none(getattr(o, "qty", None))),
        "order_type": _extract_order_field(
            lambda: _enum_value("order_type") or _enum_value("type")
        ),
        "status": _extract_order_field(lambda: _enum_value("status")),
        "limit_price": _extract_order_field(lambda: _float_or_none(getattr(o, "limit_price", None))),
        "stop_price": _extract_order_field(lambda: _float_or_none(getattr(o, "stop_price", None))),
        "filled_qty": _extract_order_field(lambda: _float_or_none(getattr(o, "filled_qty", None))),
        "filled_avg_price": _extract_order_field(
            lambda: _float_or_none(getattr(o, "filled_avg_price", None))
        ),
        "submitted_at": _extract_order_field(lambda: _dt_str("submitted_at")),
        "filled_at": _extract_order_field(lambda: _dt_str("filled_at")),
    }


def read_orders(status: str = "open", limit: int = 50) -> dict:
    """Best-effort read of recent/open orders across all symbols.

    `status` should be one of "open" / "closed" / "all" (route-level
    validation is expected to happen before this is called, but an
    unrecognized value here just falls back to "open" rather than raising).

    Never raises. `{"orders": [...], "error": None|str}`. A single
    malformed order object is skipped/degraded rather than aborting the
    whole read.
    """
    try:
        from alpaca.trading.enums import QueryOrderStatus
        from alpaca.trading.requests import GetOrdersRequest

        status_name = _STATUS_MAP_NAMES.get((status or "").lower(), "OPEN")
        query_status = getattr(QueryOrderStatus, status_name)

        broker = _get_broker()
        raw_orders = broker.client.get_orders(
            filter=GetOrdersRequest(status=query_status, limit=limit, nested=False)
        )

        out = []
        for o in raw_orders or []:
            try:
                out.append(_order_to_dict(o))
            except Exception as exc:
                logger.warning("broker_reads.read_orders: skipping malformed order row: %s", exc)
                continue
        return {"orders": out, "error": None}
    except Exception as exc:
        logger.warning("broker_reads.read_orders failed: %s", exc)
        return {"orders": [], "error": str(exc)}


def read_price_bars(
    symbol: str, lookback_days: int = 120, timeframe: str = "1d"
) -> dict:
    """Best-effort read of chart OHLCV bars for one symbol/timeframe.

    Wraps `AlpacaBroker.get_bars` — a market-data read (Alpaca's
    `StockHistoricalDataClient.get_stock_bars`), not a trading/account
    call. `get_bars` itself already never raises and returns `[]` on any
    failure; this wrapper only adds the same `{"bars": [...], "error":
    None|str}` degradation contract every other function in this module
    uses, so a chart panel can distinguish "no data" from "read failed."
    """
    try:
        broker = _get_broker()
        if timeframe == "1d":
            bars = broker.get_bars(symbol, lookback_days=lookback_days)
        else:
            bars = broker.get_intraday_chart_bars(
                symbol, timeframe=timeframe, lookback_days=lookback_days
            )
        out = [
            {
                "date": b.date.isoformat() if timeframe == "1d" else b["date"],
                "timestamp": None if timeframe == "1d" else b["timestamp"],
                "open": b.open if timeframe == "1d" else b["open"],
                "high": b.high if timeframe == "1d" else b["high"],
                "low": b.low if timeframe == "1d" else b["low"],
                "close": b.close if timeframe == "1d" else b["close"],
                "volume": b.volume if timeframe == "1d" else b["volume"],
            }
            for b in bars
        ]
        return {"bars": out, "error": None}
    except Exception as exc:
        logger.warning(
            "broker_reads.read_price_bars failed for %s/%s: %s",
            symbol, timeframe, exc,
        )
        return {"bars": [], "error": str(exc)}


def read_live_quotes(symbols: list[str]) -> dict:
    """Best-effort read of current-session quote facts for one or more
    symbols — last trade price, previous session close, and today's
    still-forming session range.

    Wraps `AlpacaBroker.get_intraday_snapshots` — the SAME read-only bulk
    Alpaca snapshot call (`StockHistoricalDataClient.get_stock_snapshot`,
    a market-data read, never an account/order call) the already-accepted,
    already-enabled intraday opportunity scanner uses (`src/pipeline.py`'s
    `_run_intraday_opportunity_scan`). No new broker capability, no new
    external dependency — this only gives Mission Control's read side a
    second consumer of an existing, already-safety-reviewed method.

    Distinct from `read_positions`' `current_price` (held positions only,
    from the trading client) and `read_price_bars`' historical daily bars
    (`/prices`) — this is what lets a chart/candidate view label a true
    current price instead of a historical close implying "now" (2026-08-21
    Mission Control correctness finding).

    Never raises. `{"quotes": {SYMBOL: {...}}, "error": None|str}`. A
    symbol Alpaca couldn't price at all still comes back with every field
    `None` (never dropped), so the caller can tell "no data for this
    symbol" from "didn't ask."
    """
    try:
        broker = _get_broker()
        raw = broker.get_intraday_snapshots(symbols)
        quotes = {}
        any_data = False
        for sym in symbols:
            snap = raw.get(sym) or {}
            if snap.get("last_price") is not None or snap.get("prev_close") is not None:
                any_data = True
            quotes[sym] = {
                "last_price": snap.get("last_price"),
                "prev_close": snap.get("prev_close"),
                "session_open": snap.get("session_open"),
                "session_high": snap.get("session_high"),
                "session_low": snap.get("session_low"),
            }
        # get_intraday_snapshots itself never raises — a total read failure
        # (bad/absent credentials, market-data outage) degrades to `{}` for
        # every symbol, identical on the wire to "Alpaca genuinely has no
        # snapshot for any of these." That ambiguity is a real degraded
        # state ("Preserve explicit stale/error states" — do not let it
        # render as a silent absence of a live-quote caption): every
        # requested symbol coming back completely empty is the honest
        # signal to surface, since one bad symbol in an otherwise-healthy
        # batch would not zero out every other symbol too.
        error = None if any_data or not symbols else "no quote data returned for any requested symbol"
        return {"quotes": quotes, "error": error}
    except Exception as exc:
        logger.warning("broker_reads.read_live_quotes failed for %d symbol(s): %s", len(symbols), exc)
        return {"quotes": {sym: {} for sym in symbols}, "error": str(exc)}


def check_broker_reachable() -> bool | None:
    """Best-effort connectivity ping.

    Returns `True`/`False` when credentials are present (successful vs.
    failed `get_account()` call), or `None` when credentials are entirely
    absent/empty so a caller can distinguish "not configured" from
    "configured but currently down".
    """
    try:
        key, secret = get_alpaca_credentials()
    except Exception as exc:
        logger.warning("broker_reads.check_broker_reachable: could not read credentials: %s", exc)
        return None
    if not key or not secret:
        return None
    try:
        broker = _get_broker()
        broker.get_account()
        return True
    except Exception as exc:
        logger.warning("broker_reads.check_broker_reachable: get_account failed: %s", exc)
        return False
