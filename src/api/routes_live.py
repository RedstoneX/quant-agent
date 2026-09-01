"""Live (broker-backed) read-only routes for the Mission Control API.

`/health`, `/account`, `/positions`, `/orders` — everything here reads
either the Alpaca broker (via `src.api.broker_reads`, which never raises
and never touches a write-capable broker call) or, for `/health`'s
`db_reachable`/`sessions_logged_today` fields and `/account`'s `history`
field, the SQLite-backed helpers the other Stage 2 worker exposes from
`src.api.db_reads`.

GET-only by construction: this router registers nothing but `@router.get`
handlers. That is a hard project invariant for Stage 2 (verified by an
automated test) — do not add POST/PUT/PATCH/DELETE routes here.

Every handler is defense-in-depth: the functions it calls
(`broker_reads.*`) are already designed to never raise, but each handler
still wraps its body in `try/except Exception` and degrades to a response
model with `error` set rather than ever letting FastAPI surface an
unhandled 500 (which could leak an internal stack trace / file path).
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, HTTPException, Query

from src.api.broker_reads import (
    check_broker_reachable,
    read_account,
    read_live_quotes,
    read_orders,
    read_positions,
    read_price_bars,
)
from src.api.deps import (
    get_alpaca_paper,
    get_cash_sweep_enabled,
    get_cash_sweep_reserve_pct,
    get_cash_sweep_symbol,
    get_risk_limits,
)
from src.api.schemas import (
    AccountResponse,
    DailyPnlPoint,
    HealthResponse,
    LiquidityBreakdown,
    LiveQuote,
    LiveQuotesResponse,
    OrderItem,
    OrdersResponse,
    PositionItem,
    PositionsResponse,
    PriceBar,
    PriceBarsResponse,
    RiskLimits,
)

_MAX_QUOTE_SYMBOLS = 25

logger = logging.getLogger(__name__)

router = APIRouter()

_LAST_RUN_MODES = [
    "morning", "midday", "close", "evening", "intra_check", "earnings_preprocess",
]

_ORDER_STATUS_VALUES = {"open", "closed", "all"}


def _cache_dir() -> Path:
    # Same directory scripts/run_if_et_window.sh writes last-run files and
    # the cross-mode session lock into — see CLAUDE.md "时区" section.
    return Path(os.path.expanduser("~/.cache/quant-agent"))


def _last_run_files() -> dict[str, str | None]:
    out: dict[str, str | None] = {mode: None for mode in _LAST_RUN_MODES}
    try:
        base = _cache_dir()
        for mode in _LAST_RUN_MODES:
            try:
                p = base / f"last-{mode}"
                if p.exists():
                    mtime = p.stat().st_mtime
                    out[mode] = datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat()
            except OSError:
                out[mode] = None
    except Exception:
        # Cache dir missing entirely, or some other unexpected OS-level
        # failure — never raise, just report "nothing known" for every mode.
        return {mode: None for mode in _LAST_RUN_MODES}
    return out


def _session_lock_active() -> bool | None:
    try:
        return os.path.isdir(_cache_dir() / "active-session.lock")
    except OSError:
        return None


@router.get("/health", response_model=HealthResponse)
def get_health() -> HealthResponse:
    try:
        try:
            from src.api.db_reads import get_llm_circuit_health, session_prefixes_logged_on
            sessions_logged_today = session_prefixes_logged_on()
            llm_health = get_llm_circuit_health()
            db_reachable = True
        except Exception:
            sessions_logged_today = []
            llm_health = None
            db_reachable = False

        # Read separately from the block above: the alert channel's history
        # living in a database that predates this feature is a perfectly
        # ordinary state, and must not be reported as "database unreachable".
        try:
            from src.api.db_reads import get_alert_channel_health
            alert_channel = get_alert_channel_health()
        except Exception:
            alert_channel = {"status": "unknown", "error": "health read failed"}

        broker_reachable = check_broker_reachable()
        recent_pm_status = (llm_health or {}).get("recent_pm_status")
        circuit_suspended = bool((llm_health or {}).get("suspended"))
        circuit_available = bool((llm_health or {}).get("available"))
        scoped_quota_holds = bool((llm_health or {}).get("active_quota_holds"))

        def _failed_status(value) -> bool:
            if not value:
                return False
            status = str(value).lower()
            return (
                status.startswith("pm_")
                or status in {"agent_failure", "failed"}
                or "parse_error" in status
                or "analysis_error" in status
            )

        recent_agent_statuses = (llm_health or {}).get("recent_agent_statuses") or {}
        failed_agents = sorted(
            name for name, item in recent_agent_statuses.items()
            if _failed_status((item or {}).get("status"))
        )
        # Compatibility with a database populated before the per-agent health
        # map existed in this API process.
        if _failed_status(recent_pm_status) and "portfolio_manager" not in failed_agents:
            failed_agents.append("portfolio_manager")
        if not db_reachable:
            decision_path_status = "unknown_database_unreachable"
        elif not circuit_available:
            decision_path_status = "degraded_cost_circuit_unavailable"
        elif circuit_suspended:
            decision_path_status = "paid_analysis_suspended"
        elif scoped_quota_holds:
            decision_path_status = "paid_analysis_scoped_quota_hold"
        elif failed_agents:
            decision_path_status = "degraded_recent_agent_failure:" + ",".join(failed_agents)
        else:
            decision_path_status = "ok"
        # A desk that cannot raise an alarm is degraded whatever else is
        # green: every other fault on this board is reported to the operator
        # over the same Telegram path, so this one hides all the others.
        # `unknown` (no check recorded yet — a fresh database, a deploy that
        # has not run a session) is surfaced in the payload and rendered
        # amber, but does not flip the whole board red: a missing
        # measurement is not a detected fault, and a permanently-red board
        # teaches the operator to ignore red.
        alert_channel_degraded = str(
            (alert_channel or {}).get("status") or "unknown"
        ) in ("broken", "stale")
        overall_status = (
            "degraded"
            if (not db_reachable or broker_reachable is False
                or decision_path_status != "ok"
                or alert_channel_degraded)
            else "ok"
        )

        return HealthResponse(
            status=overall_status,
            db_reachable=db_reachable,
            broker_reachable=broker_reachable,
            paper=get_alpaca_paper(),
            sessions_logged_today=list(sessions_logged_today or []),
            last_run_files=_last_run_files(),
            session_lock_active=_session_lock_active(),
            decision_path_status=decision_path_status,
            llm_circuit=llm_health,
            alert_channel=alert_channel,
            timestamp=datetime.now(timezone.utc).isoformat(),
        )
    except Exception:
        # Outermost guard: even /health itself must never 500. Report the
        # process as up but degraded; everything else is explicitly unknown.
        # unknown, rather than leaking a stack trace.
        return HealthResponse(
            status="degraded",
            db_reachable=False,
            broker_reachable=None,
            paper=None,
            sessions_logged_today=[],
            last_run_files={mode: None for mode in _LAST_RUN_MODES},
            session_lock_active=None,
            decision_path_status="unknown_health_exception",
            llm_circuit=None,
            alert_channel={"status": "unknown", "error": "health read failed"},
            timestamp=datetime.now(timezone.utc).isoformat(),
        )


def _compute_liquidity(cash: float | None, portfolio_value: float | None) -> LiquidityBreakdown:
    """Honest raw-cash / sweep-parked / deployable split (2026-08-18 soak
    finding: SGOV must never read like an ordinary position or an invented
    risk posture). Reads positions independently so a positions-read
    failure degrades only the sweep_parked_value/total_liquidity fields,
    never silently zeroes them. A config-read failure degrades this whole
    breakdown to an honest empty object — it must never take down the rest
    of the /account response (see AccountResponse.cash etc)."""
    try:
        sweep_enabled = get_cash_sweep_enabled()
        sweep_symbol = get_cash_sweep_symbol()
        reserve_pct = get_cash_sweep_reserve_pct()
    except Exception as exc:
        logger.warning("routes_live._compute_liquidity: could not read cash_sweep config: %s", exc)
        return LiquidityBreakdown()

    sweep_parked_value: float | None = None
    positions_result = read_positions()
    if positions_result.get("error") is None:
        sweep_parked_value = sum(
            p.get("market_value") or 0.0
            for p in positions_result.get("positions", [])
            if p.get("is_cash_equivalent")
        )

    reserve_usd = (
        portfolio_value * reserve_pct / 100.0
        if portfolio_value is not None
        else None
    )
    deployable_cash = (
        max(cash - reserve_usd, 0.0)
        if cash is not None and reserve_usd is not None
        else None
    )
    total_liquidity = (
        cash + sweep_parked_value
        if cash is not None and sweep_parked_value is not None
        else None
    )

    return LiquidityBreakdown(
        sweep_enabled=sweep_enabled,
        sweep_symbol=sweep_symbol,
        raw_cash=cash,
        sweep_parked_value=sweep_parked_value,
        reserve_usd=reserve_usd,
        deployable_cash=deployable_cash,
        total_liquidity=total_liquidity,
    )


def _compute_risk_limits() -> RiskLimits:
    """Degrades to an honest empty RiskLimits() on any config read
    failure — never a guessed/default limit standing in for the real
    configured one. Mirrors _compute_liquidity's fail-closed-to-empty
    posture."""
    try:
        limits = get_risk_limits()
    except Exception as exc:
        logger.warning("routes_live._compute_risk_limits: could not read risk config: %s", exc)
        return RiskLimits()
    return RiskLimits(
        max_position_pct=limits.max_position_pct,
        max_total_position_pct=limits.max_total_position_pct,
        max_daily_loss_pct=limits.max_daily_loss_pct,
        max_sector_pct=limits.max_sector_pct,
        # Spec §11.2 — the standing gross-exposure cap. Distinct from
        # max_total_position_pct, which bounds NET exposure.
        max_gross_exposure_x=getattr(limits, "max_gross_exposure_x", None),
    )


# NOTE (§11.2): Mission Control does NOT compute gross exposure, the
# de-levering ladder, or distance-to-forced-liquidation. Doing so requires
# `src.risk.rules`, and `src/api/` is forbidden by a ratified structural
# guardrail (tests/test_api_safety.py) from importing the trading/risk stack
# at all. Re-implementing the arithmetic here instead was explicitly rejected:
# a second definition of "how much does the book own" is the exact sprawl
# §12.2 cleaned up, and a dashboard that drifts from the gate is worse than a
# dashboard that stays quiet. The standing cap is still reported on
# `RiskLimits.max_gross_exposure_x` above (config read, no risk import), and
# the live ladder state reaches the operator on the session alert
# (`src/notifier.py::_append_leverage_line`). Showing it here needs the pure
# measurement functions extracted to a module outside `src.risk` first.


@router.get("/account", response_model=AccountResponse)
def get_account() -> AccountResponse:
    try:
        acct = read_account()
        cash = acct.get("cash")
        portfolio_value = acct.get("portfolio_value")
        last_equity = acct.get("last_equity")

        daily_pnl = None
        daily_pnl_pct = None
        if portfolio_value is not None and last_equity is not None and last_equity != 0:
            daily_pnl = portfolio_value - last_equity
            daily_pnl_pct = daily_pnl / last_equity * 100

        history: list[DailyPnlPoint] = []
        try:
            from src.api.db_reads import get_recent_daily_pnl
            rows = get_recent_daily_pnl(limit=30) or []
            for row in rows:
                history.append(DailyPnlPoint(
                    date=row.get("date"),
                    total_value=row.get("total_value"),
                    daily_pnl=row.get("daily_pnl"),
                    daily_return_pct=row.get("daily_return_pct"),
                    equity_close=row.get("equity_close"),
                ))
        except Exception:
            history = []

        liquidity = _compute_liquidity(cash, portfolio_value) if acct.get("error") is None else None
        risk_limits = _compute_risk_limits()

        return AccountResponse(
            cash=cash,
            portfolio_value=portfolio_value,
            last_equity=last_equity,
            daily_pnl=daily_pnl,
            daily_pnl_pct=daily_pnl_pct,
            paper=get_alpaca_paper(),
            history=history,
            liquidity=liquidity,
            risk_limits=risk_limits,
            error=acct.get("error"),
        )
    except Exception as exc:
        return AccountResponse(error=str(exc))


@router.get("/positions", response_model=PositionsResponse)
def get_positions() -> PositionsResponse:
    try:
        result = read_positions()
        items = [
            PositionItem(
                symbol=p.get("symbol"),
                qty=p.get("qty"),
                avg_entry=p.get("avg_entry"),
                current_price=p.get("current_price"),
                market_value=p.get("market_value"),
                unrealized_pnl=p.get("unrealized_pnl"),
                unrealized_intraday_pnl=p.get("unrealized_intraday_pnl"),
                sector=p.get("sector"),
                is_cash_equivalent=p.get("is_cash_equivalent", False),
                direction=p.get("direction", "long"),
            )
            for p in result.get("positions", [])
        ]
        return PositionsResponse(positions=items, error=result.get("error"))
    except Exception as exc:
        return PositionsResponse(positions=[], error=str(exc))


@router.get("/orders", response_model=OrdersResponse)
def get_orders(
    status: Literal["open", "closed", "all"] = Query("open"),
    limit: int = Query(50, ge=1, le=500),
) -> OrdersResponse:
    try:
        if status not in _ORDER_STATUS_VALUES:
            # Defense in depth: FastAPI's Literal type already rejects
            # anything else at the request-validation layer, but never let
            # an unvalidated status string reach the broker call.
            raise HTTPException(status_code=400, detail=f"invalid status: {status!r}")

        result = read_orders(status=status, limit=limit)
        items = [
            OrderItem(
                id=o.get("id"),
                symbol=o.get("symbol"),
                side=o.get("side"),
                qty=o.get("qty"),
                order_type=o.get("order_type"),
                status=o.get("status"),
                limit_price=o.get("limit_price"),
                stop_price=o.get("stop_price"),
                filled_qty=o.get("filled_qty"),
                filled_avg_price=o.get("filled_avg_price"),
                submitted_at=o.get("submitted_at"),
                filled_at=o.get("filled_at"),
            )
            for o in result.get("orders", [])
            if o.get("id") is not None and o.get("symbol") is not None
        ]
        return OrdersResponse(orders=items, error=result.get("error"))
    except HTTPException:
        raise
    except Exception as exc:
        return OrdersResponse(orders=[], error=str(exc))


@router.get("/quotes", response_model=LiveQuotesResponse)
def get_quotes(symbols: str = Query(..., description="Comma-separated symbols, e.g. AAPL,MSFT")) -> LiveQuotesResponse:
    """Current-session quote facts (last trade, previous close, today's
    still-forming session range) for one or more symbols — market-data
    read only, never account/order/trading state. Distinct from
    `/positions`' broker-marked current_price (held positions only) and
    `/prices`' historical daily bars; lets a chart/candidate view label a
    true current price instead of implying a historical bar is "now."
    """
    now = datetime.now(timezone.utc).isoformat()
    try:
        syms = [s.strip().upper() for s in symbols.split(",") if s.strip()]
        syms = syms[:_MAX_QUOTE_SYMBOLS]
        if not syms:
            return LiveQuotesResponse(quotes=[], as_of=now, error="no symbols requested")
        result = read_live_quotes(syms)
        result_quotes = result.get("quotes", {})
        quotes = [
            LiveQuote(symbol=sym, **(result_quotes.get(sym) or {}))
            for sym in syms
        ]
        return LiveQuotesResponse(quotes=quotes, as_of=now, error=result.get("error"))
    except Exception as exc:
        return LiveQuotesResponse(quotes=[], as_of=now, error=str(exc))


@router.get("/prices/{symbol}", response_model=PriceBarsResponse)
def get_prices(
    symbol: str,
    lookback_days: int = Query(120, ge=1, le=500),
    timeframe: Literal["5m", "15m", "1h", "1d"] = Query("1d"),
) -> PriceBarsResponse:
    """OHLCV bars for one symbol/timeframe — market-data read only (Alpaca's
    historical data client), never account/order/trading state. Powers
    the cockpit's price chart panel; never places, cancels or references
    an order."""
    try:
        symbol = symbol.strip().upper()
        result = read_price_bars(
            symbol, lookback_days=lookback_days, timeframe=timeframe
        )
        bars = [PriceBar(**b) for b in result.get("bars", [])]
        return PriceBarsResponse(
            symbol=symbol, timeframe=timeframe, bars=bars,
            error=result.get("error"),
        )
    except Exception as exc:
        return PriceBarsResponse(
            symbol=symbol, timeframe=timeframe, bars=[], error=str(exc)
        )
