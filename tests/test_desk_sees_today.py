"""The desk must see TODAY during market hours (2026-09-14 incident).

`get_ohlcv` used `end=et_today()` with yfinance's EXCLUSIVE end, so every
session judged price against levels on yesterday's close. ORCL on
2026-09-10 traded 158.38 at the 09:30 ET open, below the desk's own 159.79
support, and no seat could see it. These tests pin the fix:

- completed daily bars stay completed (no in-progress bar mixed in),
- after the close today's bar is included,
- during market hours support/resistance is judged against the live price,
- a missing/stale live price is labelled STALE, never passed off as today.
"""

from datetime import date, datetime, timedelta
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from src.data.levels import find_structural_levels, format_levels_block
from src.data.market import MarketDataProvider
from src.models import OHLCV, TechnicalIndicators
from src.trading_calendar import (
    ET,
    in_regular_session,
    last_completed_bar_date,
    live_price_is_today,
)

# Thursday 2026-09-10, the ORCL session.
ORCL_OPEN = datetime(2026, 9, 10, 9, 30, tzinfo=ET)
ORCL_EVENING = datetime(2026, 9, 10, 20, 0, tzinfo=ET)
ORCL_SUPPORT = 159.79
ORCL_LIVE = 158.38


def _orcl_bars() -> list[OHLCV]:
    """Synthetic fixture: price repeatedly turns at 159.79 (lows) and 170.50
    (highs), last completed close 161.79 — i.e. 159.79 is support as of the
    9 Sept close."""
    closes: list[float] = []
    for _ in range(8):
        closes += [160.29 + (170 - 160.29) * i / 6 for i in range(6)]
        closes += [170 - (170 - 160.29) * i / 6 for i in range(6)]
    closes += [160.29 + i * 0.3 for i in range(6)]
    start = date(2026, 5, 1)
    return [
        OHLCV(date=start + timedelta(days=i), open=c, high=c + 0.5,
              low=c - 0.5, close=c, volume=1_000_000)
        for i, c in enumerate(closes)
    ]


# --- trading calendar -------------------------------------------------------

def test_regular_session_bounds():
    assert in_regular_session(ORCL_OPEN) is True
    assert in_regular_session(ORCL_OPEN.replace(hour=9, minute=29)) is False
    assert in_regular_session(ORCL_OPEN.replace(hour=15, minute=59)) is True
    assert in_regular_session(ORCL_OPEN.replace(hour=16, minute=0)) is False
    # Saturday
    assert in_regular_session(datetime(2026, 9, 12, 11, 0, tzinfo=ET)) is False


def test_last_completed_bar_date_moves_to_today_only_after_the_close():
    assert last_completed_bar_date(ORCL_OPEN) == date(2026, 9, 9)
    assert last_completed_bar_date(ORCL_OPEN.replace(hour=15, minute=59)) == date(2026, 9, 9)
    assert last_completed_bar_date(ORCL_OPEN.replace(hour=16, minute=0)) == date(2026, 9, 10)
    assert last_completed_bar_date(ORCL_EVENING) == date(2026, 9, 10)
    # Monday pre-market: bound is Sunday, i.e. Friday's bar is the latest.
    assert last_completed_bar_date(datetime(2026, 9, 14, 8, 0, tzinfo=ET)) == date(2026, 9, 13)


def test_live_price_is_today_rejects_prior_session_and_naive_timestamps():
    assert live_price_is_today(ORCL_OPEN.replace(minute=31), when=ORCL_OPEN) is True
    assert live_price_is_today(datetime(2026, 9, 9, 15, 59, tzinfo=ET), when=ORCL_OPEN) is False
    assert live_price_is_today(datetime(2026, 9, 10, 9, 31), when=ORCL_OPEN) is False
    assert live_price_is_today(None, when=ORCL_OPEN) is False


# --- get_ohlcv window -------------------------------------------------------

def _frame(days: list[date]) -> pd.DataFrame:
    n = len(days)
    return pd.DataFrame(
        {"Open": [1.0] * n, "High": [2.0] * n, "Low": [0.5] * n,
         "Close": [1.5] * n, "Volume": [100] * n},
        index=pd.DatetimeIndex([pd.Timestamp(d) for d in days]),
    )


@pytest.mark.parametrize("now, expected_end, expected_last", [
    (ORCL_OPEN, "2026-09-10", date(2026, 9, 9)),      # in session: through yesterday
    (ORCL_EVENING, "2026-09-11", date(2026, 9, 10)),  # after close: today included
])
def test_get_ohlcv_end_follows_completed_bar_date(monkeypatch, now, expected_end, expected_last):
    monkeypatch.setattr("src.trading_calendar.et_now", lambda: now)
    monkeypatch.setattr("src.data.market.et_today", lambda: now.date())
    with patch("src.data.market.yf.download") as dl:
        # Source hands back an in-progress 09-10 row even in session.
        dl.return_value = _frame([date(2026, 9, 8), date(2026, 9, 9), date(2026, 9, 10)])
        bars = MarketDataProvider().get_ohlcv("ORCL", lookback_days=10)
    assert dl.call_args.kwargs["end"] == expected_end
    assert bars[-1].date == expected_last


def test_get_ohlcv_drops_in_progress_bar_from_alpaca_fallback(monkeypatch):
    monkeypatch.setattr("src.trading_calendar.et_now", lambda: ORCL_OPEN)
    partial = OHLCV(date=date(2026, 9, 10), open=160, high=160, low=158.38,
                    close=158.38, volume=10)
    done = OHLCV(date=date(2026, 9, 9), open=161, high=162, low=160,
                 close=161.79, volume=10)
    provider = MarketDataProvider(fallback_bars=lambda s, n: [done, partial])
    with patch("src.data.market.yf.download", return_value=pd.DataFrame()):
        bars = provider.get_ohlcv("ORCL", lookback_days=10)
    assert [b.date for b in bars] == [date(2026, 9, 9)]


# --- ORCL 2026-09-10 09:30: price vs support ---------------------------------

def test_orcl_open_below_support_is_seen_with_live_price():
    bars = _orcl_bars()
    assert bars[-1].close > ORCL_SUPPORT  # the stale view

    stale_sup, _ = find_structural_levels(bars)
    assert ORCL_SUPPORT in [lv.price for lv in stale_sup]  # the defect, reproduced

    live_sup, live_res = find_structural_levels(bars, reference_price=ORCL_LIVE)
    assert ORCL_SUPPORT not in [lv.price for lv in live_sup]
    assert ORCL_SUPPORT in [lv.price for lv in live_res]  # now overhead
    block = format_levels_block(live_sup, live_res, bars[-1].close, live_price=ORCL_LIVE)
    assert "LIVE $158.38" in block and "IN PROGRESS" in block
    assert "last completed close $161.79" in block


def _tech_msg(intraday_context):
    from src.agents.tech_analyst import TechAnalystAgent
    bars = _orcl_bars()
    ind = TechnicalIndicators(symbol="ORCL", ma_20=165.0, rsi_14=50.0, atr_14=3.0)
    with patch("anthropic.Anthropic"):
        agent = TechAnalystAgent(api_key="test", model="claude-sonnet-4-6-20250514")
        return agent.build_user_message(
            symbols_data=[{"symbol": "ORCL", "bars": bars, "indicators": ind}],
            intraday_context=intraday_context,
        )


def test_tech_prompt_at_orcl_open_puts_support_above_live_price():
    msg = _tech_msg({"ORCL": {
        "last_price": ORCL_LIVE, "prev_close": 161.79, "session_open": ORCL_LIVE,
        "session_high": 160.0, "session_low": ORCL_LIVE, "session_volume": 1000,
    }})
    levels = msg.split("Structural levels")[1].split("Price (last")[0]
    resistance, support = levels.split(">>>")[0], levels.split("<<<")[1]
    assert "$159.79" in resistance and "$159.79" not in support
    assert "CURRENT SESSION" in msg and "INCOMPLETE" in msg
    # Live price never enters the completed-bar series.
    completed = msg.split("Price (last")[1].split("Indicators:")[0]
    assert "158.38" not in completed


def test_tech_prompt_labels_unavailable_live_price_as_stale():
    msg = _tech_msg({"ORCL": {"live_unavailable": "no live trade price returned"}})
    assert "LIVE PRICE UNAVAILABLE" in msg and "STALE" in msg
    assert ">>> last close $161.79 <<<" in msg  # no live marker invented


# --- pipeline live-session context -----------------------------------------

def _pipeline(snapshots):
    from src.pipeline import TradingPipeline
    p = TradingPipeline.__new__(TradingPipeline)
    p.broker = MagicMock()
    p.broker.get_intraday_snapshots.return_value = snapshots
    return p


def test_live_session_context_empty_outside_market_hours(monkeypatch):
    monkeypatch.setattr("src.trading_calendar.et_now", lambda: ORCL_EVENING)
    p = _pipeline({"ORCL": {"last_price": 1.0}})
    assert p._live_session_context(["ORCL"]) == {}
    p.broker.get_intraday_snapshots.assert_not_called()


def test_live_session_context_in_session_live_missing_and_stale(monkeypatch, caplog):
    now = ORCL_OPEN.replace(minute=31)
    monkeypatch.setattr("src.trading_calendar.et_now", lambda: now)
    p = _pipeline({
        "ORCL": {"last_price": ORCL_LIVE, "last_trade_at": now},
        "MSFT": {"last_price": None},
        "AAPL": {"last_price": 230.0, "last_trade_at": datetime(2026, 9, 9, 15, 59, tzinfo=ET)},
    })
    with caplog.at_level("WARNING"):
        out = p._live_session_context(["ORCL", "MSFT", "AAPL"])
    assert out["ORCL"]["last_price"] == ORCL_LIVE
    assert "live_unavailable" in out["MSFT"]
    assert "live_unavailable" in out["AAPL"]
    assert "price unavailable" in caplog.text
    assert "IEX" in (out["AAPL"].get("live_unavailable") or "")
    assert "IEX" in (out["MSFT"].get("live_unavailable") or "")


def test_live_session_context_rereads_open_print_once(monkeypatch):
    """09:30 first snapshot can still be yesterday; one re-read must land
    today's print. Do not invent a price and do not drop the name."""
    now = ORCL_OPEN.replace(minute=31)
    monkeypatch.setattr("src.trading_calendar.et_now", lambda: now)
    from src.pipeline import TradingPipeline
    p = TradingPipeline.__new__(TradingPipeline)
    p.broker = MagicMock()
    p.broker.get_intraday_snapshots.side_effect = [
        {"ORCL": {"last_price": 161.79, "last_trade_at": datetime(2026, 9, 9, 15, 59, tzinfo=ET)}},
        {"ORCL": {"last_price": ORCL_LIVE, "last_trade_at": now, "session_open": ORCL_LIVE}},
    ]
    out = p._live_session_context(["ORCL"])
    assert out["ORCL"]["last_price"] == ORCL_LIVE
    assert "live_unavailable" not in out["ORCL"]
    assert p.broker.get_intraday_snapshots.call_count == 2


def test_live_session_context_broker_failure_marks_every_symbol_stale(monkeypatch):
    monkeypatch.setattr("src.trading_calendar.et_now", lambda: ORCL_OPEN)
    p = _pipeline({})
    p.broker.get_intraday_snapshots.side_effect = RuntimeError("down")
    out = p._live_session_context(["ORCL"])
    assert "live_unavailable" in out["ORCL"]


def test_prefilter_band_proximity_uses_live_price():
    from src.pipeline import TradingPipeline
    ind = TechnicalIndicators(symbol="ORCL", rsi_14=50.0, bb_upper=170.0,
                              bb_lower=158.0, volume_change_pct=0.0)
    # Last completed close far from both bands; live price at the lower band.
    far = [MagicMock(close=164.0) for _ in range(5)]
    assert TradingPipeline._has_actionable_signal_fn(ind, "ORCL", far, []) is False
    assert TradingPipeline._has_actionable_signal_fn(
        ind, "ORCL", far, [], live_price=ORCL_LIVE,
    ) is True


def test_stage_live_price_kwarg_only_for_usable_live_price():
    from src.pipeline_stages import MorningResearchStage
    kw = MorningResearchStage._live_price_kwarg
    assert kw({"ORCL": {"last_price": ORCL_LIVE}}, "ORCL") == {"live_price": ORCL_LIVE}
    assert kw({"ORCL": {"live_unavailable": "x", "last_price": 1.0}}, "ORCL") == {}
    assert kw({}, "ORCL") == {}
