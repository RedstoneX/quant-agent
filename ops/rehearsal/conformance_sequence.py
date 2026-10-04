"""Call sequence and answer-normalising helpers for the broker conformance check.

Split out of conformance.py so neither file passes the size floor."""
from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone
from enum import Enum
from types import SimpleNamespace

SYMBOL = "SPY"


def _plain(value):
    """A JSON-able description of an SDK/stand-in answer."""
    if isinstance(value, Enum):
        return f"enum:{value.value}"
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (datetime, date)):
        return f"datetime:{value.isoformat()}"
    if isinstance(value, uuid.UUID):
        return str(value)
    if hasattr(value, "model_dump"):
        return {k: _plain(v) for k, v in value.model_dump().items()}
    if isinstance(value, SimpleNamespace):
        return {k: _plain(v) for k, v in vars(value).items()}
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    return f"<{type(value).__name__}>"


def _fields(plain) -> set[str]:
    if isinstance(plain, dict) and isinstance(plain.get(SYMBOL), dict):
        return set(plain[SYMBOL])  # symbol-keyed data answers
    if isinstance(plain, dict):
        return set(plain)
    if isinstance(plain, list) and plain and isinstance(plain[0], dict):
        return set(plain[0])
    return set()


def _status_of(plain) -> str | None:
    if isinstance(plain, dict):
        s = plain.get("status")
        return s.replace("enum:", "") if isinstance(s, str) else s
    return None


def _exercise(client, name: str, fn) -> dict:
    try:
        raw = fn(client)
    except Exception as exc:  # noqa: BLE001 — the error SHAPE is the finding
        return {"call": name, "ok": False, "error_type": type(exc).__name__,
                "error": str(exc)[:200],
                "status_code": getattr(exc, "status_code", None), "raw": None}
    plain = _plain(raw)
    return {"call": name, "ok": True, "raw": raw, "plain": plain,
            "fields": sorted(_fields(plain)), "status": _status_of(plain),
            "kind": type(raw).__name__}


# ------------------------------------------------------------- stand-in


def run_sequence(trading, data, *, live: bool) -> list[dict]:
    from alpaca.data.requests import (
        StockBarsRequest, StockLatestQuoteRequest, StockLatestTradeRequest,
        StockSnapshotRequest,
    )
    from alpaca.data.timeframe import TimeFrame
    from alpaca.trading.enums import OrderSide, QueryOrderStatus, TimeInForce
    from alpaca.trading.requests import (
        GetCalendarRequest, GetOrdersRequest, LimitOrderRequest,
        ReplaceOrderRequest, StopOrderRequest,
    )

    rows: list[dict] = []
    today = date.today()

    def rec(name, fn):
        row = _exercise(trading, name, fn)
        rows.append(row)
        return row

    rec("get_account", lambda c: c.get_account())
    pos = rec("get_all_positions", lambda c: c.get_all_positions())
    held = [p for p in (pos.get("plain") or []) if isinstance(p, dict)]
    held_symbol = held[0]["symbol"] if held else None
    held_qty = int(float(held[0]["qty"])) if held else 0
    rec("get_portfolio_history", lambda c: c.get_portfolio_history())
    rec("get_calendar", lambda c: c.get_calendar(
        GetCalendarRequest(start=today, end=today + timedelta(days=7))))
    rec("get_asset", lambda c: c.get_asset(SYMBOL))
    rec("get(/assets/{sym})", lambda c: c.get(f"/assets/{SYMBOL}"))
    rec("get(/account/activities)", lambda c: c.get(
        "/account/activities", {"page_size": 5}))
    rec("get(/account/activities/INT)", lambda c: c.get(
        "/account/activities/INT", {"page_size": 5}))

    # A resting entry: a limit far below the market so it can never fill.
    entry = rec("submit_order", lambda c: c.submit_order(LimitOrderRequest(
        symbol=SYMBOL, qty=1, side=OrderSide.BUY, time_in_force=TimeInForce.DAY,
        limit_price=1.00)))
    entry_id = (entry.get("plain") or {}).get("id") if entry["ok"] else None

    rec("get_order_by_id", lambda c: c.get_order_by_id(entry_id))
    rec("get_orders", lambda c: c.get_orders(GetOrdersRequest(
        status=QueryOrderStatus.OPEN, symbols=[SYMBOL])))
    rep = rec("replace_order_by_id", lambda c: c.replace_order_by_id(
        entry_id, ReplaceOrderRequest(limit_price=1.50)))
    rep_id = (rep.get("plain") or {}).get("id") if rep["ok"] else None
    rec("get_order_by_id (after replace)", lambda c: c.get_order_by_id(entry_id))
    rec("cancel_order_by_id", lambda c: c.cancel_order_by_id(rep_id or entry_id))
    rec("get_order_by_id (after cancel)",
        lambda c: c.get_order_by_id(rep_id or entry_id))
    rec("replace_order_by_id (on cancelled)", lambda c: c.replace_order_by_id(
        rep_id or entry_id, ReplaceOrderRequest(limit_price=1.75)))
    rec("cancel_order_by_id (already cancelled)",
        lambda c: c.cancel_order_by_id(rep_id or entry_id))
    rec("get_order_by_id (unknown id)",
        lambda c: c.get_order_by_id(str(uuid.uuid4())))

    # The protective-stop path needs a held position.
    if held_symbol and held_qty > 0:
        stop = rec("submit_order (protective stop)",
                   lambda c: c.submit_order(StopOrderRequest(
                       symbol=held_symbol, qty=held_qty, side=OrderSide.SELL,
                       time_in_force=TimeInForce.GTC, stop_price=0.50)))
        stop_id = (stop.get("plain") or {}).get("id") if stop["ok"] else None
        srep = rec("replace_order_by_id (stop_price)",
                   lambda c: c.replace_order_by_id(
                       stop_id, ReplaceOrderRequest(stop_price=0.75)))
        srep_id = (srep.get("plain") or {}).get("id") if srep["ok"] else None
        rec("get_orders (stops after amend)", lambda c: c.get_orders(
            GetOrdersRequest(status=QueryOrderStatus.OPEN,
                             symbols=[held_symbol], side=OrderSide.SELL)))
        rec("cancel_order_by_id (stop)",
            lambda c: c.cancel_order_by_id(srep_id or stop_id))
    else:
        # No position (and after hours nothing can fill), so exercise the SAME
        # amend call on a buy-stop far above the market: identical request
        # type, identical answer shape, and it needs no shares.
        stop = rec("submit_order (protective stop)",
                   lambda c: c.submit_order(StopOrderRequest(
                       symbol=SYMBOL, qty=1, side=OrderSide.BUY,
                       time_in_force=TimeInForce.GTC, stop_price=9000.0)))
        stop_id = (stop.get("plain") or {}).get("id") if stop["ok"] else None
        srep = rec("replace_order_by_id (stop_price)",
                   lambda c: c.replace_order_by_id(
                       stop_id, ReplaceOrderRequest(stop_price=9100.0)))
        srep_id = (srep.get("plain") or {}).get("id") if srep["ok"] else None
        rec("get_orders (stops after amend)", lambda c: c.get_orders(
            GetOrdersRequest(status=QueryOrderStatus.OPEN, symbols=[SYMBOL])))
        rec("get_order_by_id (old stop id after amend)",
            lambda c: c.get_order_by_id(stop_id))
        rec("cancel_order_by_id (stop)",
            lambda c: c.cancel_order_by_id(srep_id or stop_id))
    if held_symbol:
        rec("close_position", lambda c: c.close_position(held_symbol))
    else:
        rows.append({"call": "close_position", "ok": False,
                     "error_type": "UNTESTED", "error": "no held position"})
    rec("cancel_orders", lambda c: c.cancel_orders())
    rec("get_orders (after cancel_orders)", lambda c: c.get_orders(
        GetOrdersRequest(status=QueryOrderStatus.OPEN)))

    def drec(name, fn):
        rows.append(_exercise(data, name, fn))

    drec("get_stock_latest_trade", lambda d: d.get_stock_latest_trade(
        StockLatestTradeRequest(symbol_or_symbols=[SYMBOL])))
    drec("get_stock_latest_quote", lambda d: d.get_stock_latest_quote(
        StockLatestQuoteRequest(symbol_or_symbols=[SYMBOL])))
    drec("get_stock_snapshot", lambda d: d.get_stock_snapshot(
        StockSnapshotRequest(symbol_or_symbols=[SYMBOL])))
    drec("get_stock_bars", lambda d: d.get_stock_bars(StockBarsRequest(
        symbol_or_symbols=[SYMBOL], timeframe=TimeFrame.Day,
        start=datetime.now(timezone.utc) - timedelta(days=10))))
    return rows


# ------------------------------------------------------------- compare

