"""Shared runner for the hermetic MIDDAY and EVENING end-to-end tests.

Same production objects (the pipeline is built through tests/pipeline_factory), seat seam, broker stand-in and network wall as
tests/test_e2e_close_existing_book.py, parametrised by session, clock,
resting stop and calendar so the two session files stay small.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import tests.test_e2e_morning_session as morning
from tests.test_e2e_close_existing_book import (
    CASH, ENTRY, INITIAL_STOP, QTY, SYMBOL, _market, _seed_open_position,
)
from tests.test_e2e_morning_protection import _seed_company_profile_cache
from tests.test_e2e_morning_session import (
    SESSION_AT, _build_config, _earnings_feed_stub, _macro_feed_stub,
    _news_feed_stub, _scripted_model_seats,
)

NO_STOP = None


def run_held_book(tmp_path: Path, monkeypatch, *, session: str, hour: int,
                  bars, answers: dict, standing_stop=INITIAL_STOP,
                  trading_day: bool = True, session_close=None):
    from ops.rehearsal.broker import BrokerSnapshot, install_rehearsal_broker
    from ops.rehearsal.broker_amend import give_amend_endpoint
    from ops.rehearsal.clock import frozen_clock
    from ops.rehearsal.network_wall import no_network
    from ops.rehearsal.runner import _sentinel_credentials
    from src.agents.base import reset_route_breakers
    from src.trading_calendar import ET

    price = bars[-1].close
    reset_route_breakers()
    monkeypatch.chdir(tmp_path)
    _seed_company_profile_cache(tmp_path)
    config = _build_config(tmp_path)
    now = SESSION_AT.replace(hour=hour, minute=0, tzinfo=ET)
    trace: list = []
    monkeypatch.setattr(morning, "_scripted_answers", lambda: answers)
    attempts: list[str] = []
    with no_network(attempts), _sentinel_credentials(), \
         frozen_clock(now, run_id=f"e2e-{session}"), \
         _scripted_model_seats(trace):
        from src.execution.broker import AlpacaBroker
        from tests.pipeline_factory import build_pipeline

        # The real broker class, built as production builds it, so the
        # rehearsal broker below is installed over a genuine instance.
        kill_switch = Path(config.risk.kill_switch_path)
        if not kill_switch.is_absolute():
            kill_switch = Path(__file__).resolve().parent.parent / kill_switch
        broker = AlpacaBroker(
            api_key=config.api_keys.alpaca_key,
            secret_key=config.api_keys.alpaca_secret,
            paper=config.alpaca.paper,
            max_position_pct=config.risk.max_position_pct,
            kill_switch_path=str(kill_switch),
            trade_updates_lease_path=str(tmp_path / "data" / ".trade_updates.lock"),
            fill_stream_enabled=config.execution.fill_stream_enabled,
        )
        pipeline = build_pipeline(
            config, broker=broker, market=_market(bars),
            macro=_macro_feed_stub(), news_provider=_news_feed_stub(),
            earnings_provider=_earnings_feed_stub(),
        )
        _seed_open_position(pipeline.db)
        snapshot = BrokerSnapshot(
            as_of=now.date(), cash=CASH,
            portfolio_value=CASH + QTY * price, last_equity=CASH + QTY * price,
            positions=[{
                "symbol": SYMBOL, "qty": QTY, "avg_entry": ENTRY,
                "current_price": price, "market_value": QTY * price,
                "unrealized_pnl": QTY * (price - ENTRY), "sector": "ETF",
            }],
            prices={SYMBOL: price},
            standing_stops=({} if standing_stop is None
                            else {SYMBOL: standing_stop}),
        )
        trading = give_amend_endpoint(
            install_rehearsal_broker(pipeline.broker, snapshot, now=now),
        )
        symbols_of = pipeline.broker._data_client._symbols
        pipeline.broker._data_client.get_stock_latest_trade = lambda request: {
            sym: SimpleNamespace(price=price, timestamp=now)
            for sym in symbols_of(request)
        }
        pipeline.broker.get_intraday_snapshots = lambda symbols, *a, **k: {
            s: {"last_price": price, "last_trade_at": now} for s in symbols
        }
        pipeline.broker.is_trading_day = lambda *a, **k: trading_day
        if session_close is not None:
            pipeline.broker.get_session_close = lambda *a, **k: session_close
        result = getattr(pipeline, f"run_{session}")()
    return result, trace, trading, attempts, pipeline
