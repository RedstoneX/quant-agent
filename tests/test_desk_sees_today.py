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
        "live_price": ORCL_LIVE, "live_price_description": "last trade print",
        "prev_close": 161.79, "session_open": ORCL_LIVE,
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
    assert "NO PRICE FROM TODAY" in msg and "STALE" in msg
    assert "LOST" in msg  # a lost seat, not a low-confidence stain (item 120)
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
    assert out["ORCL"]["live_price"] == ORCL_LIVE
    assert "live_unavailable" in out["MSFT"]
    assert "live_unavailable" in out["AAPL"]
    assert "price unavailable" in caplog.text


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
    assert kw({"ORCL": {"live_price": ORCL_LIVE}}, "ORCL") == {"live_price": ORCL_LIVE}
    assert kw({"ORCL": {"live_unavailable": "x", "live_price": 1.0}}, "ORCL") == {}
    # The RAW provider field is not a live price — reading it was the defect.
    assert kw({"ORCL": {"last_price": ORCL_LIVE}}, "ORCL") == {}
    assert kw({}, "ORCL") == {}


# ---------------------------------------------------------------------------
# The same rule, applied to the ORDER path (2026-09-17)
#
# The 2026-09-14 fix made the RESEARCH path refuse a price that is not from
# today. The order path was never given the same rule: `_live_fill_price` and
# the entry-ceiling pin both took the bare price reader, which reports a
# prior session's last print and an unconfirmed quote midpoint identically to
# a live trade. These pin the order path to today's data.
# ---------------------------------------------------------------------------

def _stamped_broker(price, *, source="last_trade", is_today=True, is_today_print=True):
    from src.execution.broker import LivePrice

    broker = MagicMock()
    broker.get_latest_price_stamped.return_value = LivePrice(
        price=price, source=source, trade_at=None,
        is_today=is_today, is_today_print=is_today_print,
    )
    return broker


def test_order_path_accepts_a_price_stamped_today():
    from src.pipeline_stages import _today_order_price

    pipeline = MagicMock()
    pipeline.broker = _stamped_broker(181.25)
    assert _today_order_price(pipeline, "NVDA") == 181.25


def test_order_path_accepts_a_live_quote_mid_as_a_fill_reference():
    """A quote mid is not proof the tape traded there, but mid-session it IS
    the current market and a legitimate reference for a limit. Only the stop
    path demands a real print."""
    from src.pipeline_stages import _today_order_price

    pipeline = MagicMock()
    pipeline.broker = _stamped_broker(
        100.0, source="quote_mid", is_today=True, is_today_print=False,
    )
    assert _today_order_price(pipeline, "NVDA") == 100.0


def test_order_path_refuses_a_price_from_a_prior_session():
    from src.pipeline_stages import _today_order_price

    pipeline = MagicMock()
    pipeline.broker = _stamped_broker(181.25, is_today=False, is_today_print=False)
    assert _today_order_price(pipeline, "NVDA") is None


def test_order_path_refuses_a_price_whose_freshness_cannot_be_read():
    """Unknown freshness fails visible. The callers already treat None as
    "no verifiable live price" and skip the name rather than pricing off it."""
    from src.pipeline_stages import _today_order_price

    pipeline = MagicMock()
    pipeline.broker = _stamped_broker(181.25, is_today=False, is_today_print=False)
    assert _today_order_price(pipeline, "NVDA") is None


def test_entry_ceiling_falls_back_to_the_approved_entry_when_today_has_no_price():
    """The pinned ceiling must not be computed from a stale price. With no
    usable live price it comes from the entry the PM/RM actually approved."""
    from src.pipeline_stages import _pin_approved_entry_ceilings

    pipeline = MagicMock()
    pipeline.broker = _stamped_broker(500.0, is_today=False, is_today_print=False)
    pipeline.config.execution.entry_slippage_bps = 40
    ctx = MagicMock()
    ctx.approved_entry_ceiling = {}
    decision = MagicMock()
    decision.symbol = "NVDA"
    decision.action = "BUY"
    decision.entry_price = 100.0
    _pin_approved_entry_ceilings(pipeline, ctx, [decision])
    # 100 * (1 + 40bp) — from the approved entry, not the 500.0 stale price.
    assert abs(ctx.approved_entry_ceiling["NVDA"] - 100.4) < 0.01


# ---------------------------------------------------------------------------
# BOARD ITEM 120 — a prior session's last trade worn as today's price
#
# 2026-09-17: 8 of 104 names, one of them a holding, carried YESTERDAY's last
# trade in the morning snapshot while today's forming bar in the SAME payload
# already held the open. Two defects, both fixed here and both pinned below:
#
#   (a) a name whose `latest_trade` was stale lost its technical seat even
#       though a real today print (minute bar, forming session bar) was in the
#       same response on the same entitled venue; and
#   (b) the `session_*` block was rendered as "CURRENT SESSION (TODAY)" with
#       nothing checking the bar's own date — Alpaca puts the PREVIOUS
#       session's daily bar in that slot for a name that has not printed.
#
# The freshness rule is ET-date equality and nothing else, so these tests
# introduce no threshold of their own. Several are deliberate attempts to
# BREAK the fix (timezone, session boundary, weekend/holiday) rather than to
# confirm it.
# ---------------------------------------------------------------------------

from src.data.live_price import (  # noqa: E402
    NO_PRICE_AT_ALL,
    ONLY_STALE,
    SOURCE_LAST_TRADE,
    SOURCE_MINUTE_BAR,
    SOURCE_SESSION_BAR,
    SOURCE_SESSION_BAR_OPEN,
    resolve_live_price,
)

# Thursday 2026-09-17, the session the board item measured.
SEP17_OPEN = datetime(2026, 9, 17, 9, 30, tzinfo=ET)
SEP16_CLOSE = datetime(2026, 9, 16, 15, 59, tzinfo=ET)
#: A daily bar's own timestamp is its OPENING stamp: 00:00 ET on the session.
SEP17_BAR_AT = datetime(2026, 9, 17, 0, 0, tzinfo=ET)
SEP16_BAR_AT = datetime(2026, 9, 16, 0, 0, tzinfo=ET)


def _snap(**kw) -> dict:
    """A `get_intraday_snapshots` payload with every field present."""
    base = {
        "last_price": None, "last_trade_at": None, "prev_close": 100.0,
        "session_bar_at": None, "minute_close": None, "minute_bar_at": None,
        "session_open": None, "session_close": None, "session_high": None,
        "session_low": None, "session_volume": None,
    }
    base.update(kw)
    return base


# --- the reported scenario --------------------------------------------------

def test_yesterdays_last_trade_at_the_open_is_never_returned_as_todays_price():
    """THE BUG. Last trade is yesterday's; nothing else is today either."""
    r = resolve_live_price(
        _snap(last_price=161.79, last_trade_at=SEP16_CLOSE),
        when=SEP17_OPEN,
    )
    assert r.price is None
    assert r.source is None
    assert r.is_today_print is False
    assert r.unavailable == ONLY_STALE
    assert r.session_bar_is_today is False


def test_a_stale_last_trade_is_rescued_by_todays_forming_bar_not_lost():
    """The measured 2026-09-17 case: stale print, today's bar already open.

    The seat is kept and the number is a real aggregation of today's prints —
    it is NOT the stale 161.79.
    """
    r = resolve_live_price(
        _snap(last_price=161.79, last_trade_at=SEP16_CLOSE,
              session_bar_at=SEP17_BAR_AT, session_open=158.38,
              session_close=158.55),
        when=SEP17_OPEN.replace(minute=31),
    )
    assert r.price == 158.55          # the forming bar's CLOSE, not its open
    assert r.source == SOURCE_SESSION_BAR
    assert r.session_bar_is_today is True
    assert "forming session bar" in r.describe()


def test_a_stale_last_trade_prefers_todays_minute_bar_over_the_daily_bar():
    """A 1-minute bar is finer-grained than the forming daily bar, so it wins
    when both are today's."""
    r = resolve_live_price(
        _snap(last_price=161.79, last_trade_at=SEP16_CLOSE,
              minute_close=158.60, minute_bar_at=SEP17_OPEN.replace(minute=32),
              session_bar_at=SEP17_BAR_AT, session_close=158.55),
        when=SEP17_OPEN.replace(minute=33),
    )
    assert r.price == 158.60
    assert r.source == SOURCE_MINUTE_BAR


def test_a_quote_mid_can_never_become_the_price():
    """`resolve_live_price` has no branch that reads a quote. A payload that
    carries only quote fields resolves to unavailable, not to a midpoint."""
    r = resolve_live_price(
        _snap(bid_price=158.0, ask_price=158.1),
        when=SEP17_OPEN,
    )
    assert r.price is None
    assert r.unavailable == NO_PRICE_AT_ALL


# --- the normal case must NOT be flagged ------------------------------------

def test_a_genuinely_fresh_print_is_not_flagged_stale():
    r = resolve_live_price(
        _snap(last_price=158.38, last_trade_at=SEP17_OPEN.replace(minute=30, second=2),
              session_bar_at=SEP17_BAR_AT, session_close=158.38),
        when=SEP17_OPEN.replace(minute=31),
    )
    assert r.price == 158.38
    assert r.source == SOURCE_LAST_TRADE
    assert r.unavailable is None
    assert r.describe() == "last trade print"


def test_a_fresh_print_late_in_the_session_is_not_flagged_stale():
    r = resolve_live_price(
        _snap(last_price=159.10, last_trade_at=datetime(2026, 9, 17, 15, 59, 59, tzinfo=ET),
              session_bar_at=SEP17_BAR_AT),
        when=datetime(2026, 9, 17, 16, 0, tzinfo=ET),
    )
    assert r.price == 159.10 and r.source == SOURCE_LAST_TRADE


def test_a_utc_stamped_print_from_today_is_not_flagged_stale():
    """Alpaca stamps in UTC. 13:31Z on 2026-09-17 is 09:31 ET the same day —
    a naive date comparison on the UTC value would agree here, so this test
    exists mainly as the partner to the 20:00-ET case below."""
    from src.trading_calendar import UTC

    r = resolve_live_price(
        _snap(last_price=158.38,
              last_trade_at=datetime(2026, 9, 17, 13, 31, tzinfo=UTC),
              session_bar_at=SEP17_BAR_AT),
        when=SEP17_OPEN.replace(minute=32),
    )
    assert r.price == 158.38


# --- the halted / no-print name is a DIFFERENT condition --------------------

def test_a_halted_name_with_no_print_for_days_is_a_lost_seat_not_a_guess():
    """A halt is not the reported bug. The name has no today print anywhere;
    the honest answer is no price, and the completed-bar structure stays
    valid. It must not be rescued by anything."""
    halted_since = datetime(2026, 9, 11, 10, 15, tzinfo=ET)
    r = resolve_live_price(
        _snap(last_price=44.10, last_trade_at=halted_since,
              session_bar_at=SEP16_BAR_AT, session_close=44.10,
              session_open=44.10, session_high=44.10, session_low=44.10),
        when=SEP17_OPEN.replace(hour=11),
    )
    assert r.price is None
    assert r.unavailable == ONLY_STALE
    assert r.session_bar_is_today is False


def test_a_name_the_feed_returned_nothing_for_is_distinguished_from_a_stale_one():
    """"the feed gave us nothing" and "the feed gave us yesterday" are
    different problems and the log has to tell them apart."""
    assert resolve_live_price(_snap(), when=SEP17_OPEN).unavailable == NO_PRICE_AT_ALL
    assert resolve_live_price({}, when=SEP17_OPEN).price is None
    assert resolve_live_price(None, when=SEP17_OPEN).price is None


# --- the 09:30 boundary and other deliberate breakage attempts --------------

def test_the_first_second_of_the_session_is_today_and_the_last_of_the_prior_is_not():
    """BREAKAGE ATTEMPT — off-by-one at the session boundary.

    The rule is ET-DATE equality, not "after 09:30", so a 04:00 ET pre-market
    print on the same date is today and 16:00 ET yesterday is not. An
    implementation that compared against the session OPEN instead of the date
    would fail the pre-market half of this.
    """
    at_open = resolve_live_price(
        _snap(last_price=158.38, last_trade_at=SEP17_OPEN),
        when=SEP17_OPEN,
    )
    assert at_open.price == 158.38

    # A pre-market print is NOT this session's price. Date equality alone
    # would let a 04:00 ET print be rendered at 09:30 as the current price
    # — the exact weakness the desk rejected a proposal over on 2026-09-18.
    premarket = resolve_live_price(
        _snap(last_price=158.00,
              last_trade_at=datetime(2026, 9, 17, 4, 0, tzinfo=ET)),
        when=SEP17_OPEN,
    )
    assert premarket.price is None

    # …and one second before the open is still pre-market.
    assert resolve_live_price(
        _snap(last_price=158.00,
              last_trade_at=datetime(2026, 9, 17, 9, 29, 59, tzinfo=ET)),
        when=SEP17_OPEN,
    ).price is None

    one_second_before_midnight = resolve_live_price(
        _snap(last_price=161.79,
              last_trade_at=datetime(2026, 9, 16, 23, 59, 59, tzinfo=ET)),
        when=SEP17_OPEN,
    )
    assert one_second_before_midnight.price is None


def test_a_utc_stamp_that_is_still_yesterday_in_et_is_stale():
    """BREAKAGE ATTEMPT — timezone handling.

    2026-09-18T00:30Z is 2026-09-17 20:30 ET: the SAME ET session as a
    09:30 ET reference, even though the UTC DATE has already rolled over. An
    implementation that compared UTC dates would call this tomorrow's print;
    one that compared ET dates calls it today's, which is correct. The mirror
    case — 2026-09-17T01:00Z is 2026-09-16 21:00 ET, YESTERDAY — must be
    stale even though its UTC date reads 09-17.
    """
    from src.trading_calendar import UTC

    evening_et_same_session = resolve_live_price(
        _snap(last_price=159.0,
              last_trade_at=datetime(2026, 9, 18, 0, 30, tzinfo=UTC)),
        when=datetime(2026, 9, 17, 20, 35, tzinfo=ET),
    )
    assert evening_et_same_session.price == 159.0  # 20:30 ET, after the open

    utc_date_matches_but_et_date_does_not = resolve_live_price(
        _snap(last_price=161.79,
              last_trade_at=datetime(2026, 9, 17, 1, 0, tzinfo=UTC)),
        when=SEP17_OPEN,
    )
    assert utc_date_matches_but_et_date_does_not.price is None


def test_a_naive_timestamp_is_never_trusted_as_today():
    """BREAKAGE ATTEMPT — a timestamp with no timezone.

    Unknown freshness fails VISIBLE. A naive value that happens to spell
    today's date must not pass: there is no way to know which zone it is in,
    and guessing is how a stale price gets worn as a live one.
    """
    r = resolve_live_price(
        _snap(last_price=158.38, last_trade_at=datetime(2026, 9, 17, 9, 31)),
        when=SEP17_OPEN.replace(minute=32),
    )
    assert r.price is None


def test_a_monday_open_does_not_accept_fridays_print_or_bar():
    """BREAKAGE ATTEMPT — weekend confusion.

    Monday 2026-09-21's open must not price off Friday 2026-09-18, and the
    Friday daily bar sitting in the `daily_bar` slot must not be rendered as
    this session's range. 'Previous session' is not 'previous calendar day'
    and the date test does not need to know that — it only ever asks whether
    the stamp IS today.
    """
    monday_open = datetime(2026, 9, 21, 9, 30, tzinfo=ET)
    r = resolve_live_price(
        _snap(last_price=160.0,
              last_trade_at=datetime(2026, 9, 18, 15, 59, tzinfo=ET),
              session_bar_at=datetime(2026, 9, 18, 0, 0, tzinfo=ET),
              session_close=160.0, session_open=159.0),
        when=monday_open,
    )
    assert r.price is None
    assert r.session_bar_is_today is False


def test_a_holiday_resolves_to_no_today_print_with_no_holiday_calendar():
    """BREAKAGE ATTEMPT — holiday confusion.

    On a market holiday nothing prints, so every field is a prior session's
    and the name correctly resolves to no-today-print. The right answer is
    reached without any holiday calendar, which is why none is needed here.
    """
    thanksgiving = datetime(2026, 11, 26, 10, 0, tzinfo=ET)
    r = resolve_live_price(
        _snap(last_price=160.0,
              last_trade_at=datetime(2026, 11, 25, 15, 59, tzinfo=ET),
              session_bar_at=datetime(2026, 11, 25, 0, 0, tzinfo=ET),
              session_close=160.0),
        when=thanksgiving,
    )
    assert r.price is None


def test_a_zero_or_negative_or_nan_price_is_not_a_price():
    """BREAKAGE ATTEMPT — a provider glitch dressed as a number."""
    for bad in (0.0, -1.0, float("nan"), float("inf"), True, "158.38", None):
        r = resolve_live_price(
            _snap(last_price=bad, last_trade_at=SEP17_OPEN,
                  session_bar_at=SEP17_BAR_AT, session_close=158.55),
            when=SEP17_OPEN.replace(minute=31),
        )
        assert r.source == SOURCE_SESSION_BAR, f"{bad!r} was accepted as a price"


def test_todays_bar_with_no_close_falls_back_to_its_open_and_SAYS_SO():
    """A bar one minute old may not have published a close yet.

    The fallback carries its OWN source label. Telling a paid seat "close of
    today's still-forming session bar" when the number is the 09:30 open is
    a false provenance string, and at 15:30 it would be badly false.
    """
    r = resolve_live_price(
        _snap(session_bar_at=SEP17_BAR_AT, session_open=158.38, session_close=None),
        when=SEP17_OPEN.replace(minute=31),
    )
    assert r.price == 158.38
    assert r.source == SOURCE_SESSION_BAR_OPEN
    assert "open of today's session bar" in r.describe()
    assert "close of" not in r.describe()


def test_the_most_recent_usable_print_wins_not_the_purest():
    """BREAKAGE ATTEMPT — same-day staleness.

    Precedence by purity alone prices a name off its 09:31 last trade all
    afternoon while its own minute bars keep updating. That is item 120's
    defect in same-day form and no date test can catch it.
    """
    r = resolve_live_price(
        _snap(last_price=158.38, last_trade_at=SEP17_OPEN.replace(minute=31),
              minute_close=162.10,
              minute_bar_at=datetime(2026, 9, 17, 15, 59, tzinfo=ET),
              session_bar_at=SEP17_BAR_AT, session_close=162.10),
        when=datetime(2026, 9, 17, 16, 0, tzinfo=ET),
    )
    assert r.price == 162.10
    assert r.source == SOURCE_MINUTE_BAR

    # …and purity still breaks a genuine tie.
    same_instant = SEP17_OPEN.replace(minute=45)
    tied = resolve_live_price(
        _snap(last_price=158.38, last_trade_at=same_instant,
              minute_close=158.40, minute_bar_at=same_instant),
        when=same_instant,
    )
    assert tied.source == SOURCE_LAST_TRADE


# --- the pipeline and the prompt, end to end --------------------------------

def test_live_session_context_rescues_the_stale_name_and_blanks_yesterdays_bar(
    monkeypatch, caplog,
):
    now = SEP17_OPEN.replace(minute=31)
    monkeypatch.setattr("src.trading_calendar.et_now", lambda: now)
    p = _pipeline({
        # stale print, today's bar present — rescued
        "ORCL": _snap(last_price=161.79, last_trade_at=SEP16_CLOSE,
                      session_bar_at=SEP17_BAR_AT, session_open=158.38,
                      session_close=158.55, session_high=158.9,
                      session_low=158.2, session_volume=4000),
        # stale everywhere — lost seat, and yesterday's bar must be blanked
        "AAPL": _snap(last_price=230.0, last_trade_at=SEP16_CLOSE,
                      session_bar_at=SEP16_BAR_AT, session_open=229.0,
                      session_close=230.0, session_high=231.0,
                      session_low=228.0, session_volume=9000),
    })
    with caplog.at_level("INFO"):
        out = p._live_session_context(["ORCL", "AAPL"])

    assert out["ORCL"]["live_price"] == 158.55
    assert out["ORCL"]["live_price_source"] == SOURCE_SESSION_BAR
    assert out["ORCL"]["session_high"] == 158.9      # today's bar survives
    assert "item 120" in caplog.text

    assert "live_unavailable" in out["AAPL"]
    assert out["AAPL"].get("live_price") is None


def test_live_session_context_blanks_a_prior_sessions_bar_even_when_priced(
    monkeypatch,
):
    """BREAKAGE ATTEMPT — the two halves must not be coupled.

    A name CAN have a fresh trade print while Alpaca still carries the
    previous session's daily bar (the first print of the day, before the new
    daily bar is published). The price is good; the session range is not, and
    rendering it as today's is defect (b).
    """
    now = SEP17_OPEN.replace(second=5)
    monkeypatch.setattr("src.trading_calendar.et_now", lambda: now)
    p = _pipeline({"ORCL": _snap(
        last_price=158.38, last_trade_at=now,
        session_bar_at=SEP16_BAR_AT, session_open=160.0, session_close=161.79,
        session_high=162.0, session_low=159.5, session_volume=8_000_000,
    )})
    out = p._live_session_context(["ORCL"])
    assert out["ORCL"]["live_price"] == 158.38
    assert out["ORCL"]["live_price_source"] == SOURCE_LAST_TRADE
    for field in ("session_open", "session_close", "session_high",
                  "session_low", "session_volume"):
        assert out["ORCL"][field] is None, f"{field} kept a prior session's value"


def test_tech_prompt_never_shows_a_session_range_it_does_not_have():
    msg = _tech_msg({"ORCL": {
        "live_price": ORCL_LIVE, "live_price_description": "last trade print",
        "prev_close": 161.79, "session_open": None, "session_close": None,
        "session_high": None, "session_low": None, "session_volume": None,
    }})
    assert "Session so far: NOT AVAILABLE" in msg
    assert "158.38" in msg  # the price itself is still shown


def test_tech_prompt_names_the_source_when_the_price_is_not_a_last_trade():
    msg = _tech_msg({"ORCL": {
        "live_price": ORCL_LIVE,
        "live_price_description": "close of today's still-forming session bar",
        "prev_close": 161.79, "session_open": ORCL_LIVE,
        "session_high": 160.0, "session_low": ORCL_LIVE, "session_volume": 1000,
    }})
    assert "still-forming session bar" in msg
    assert "Current price: $158.38" in msg


def test_the_intraday_mover_scan_does_not_buy_a_paid_look_on_yesterdays_move(
    monkeypatch,
):
    """A prior session's print against `prev_close` is not today's move, and
    it must not trigger a paid intraday look."""
    from src.pipeline import TradingPipeline

    now = SEP17_OPEN.replace(hour=11)
    monkeypatch.setattr("src.trading_calendar.et_now", lambda: now)
    p = TradingPipeline.__new__(TradingPipeline)
    p.broker = MagicMock()
    p.broker.get_intraday_snapshots.return_value = {
        "ORCL": _snap(last_price=140.0, last_trade_at=SEP16_CLOSE,
                      prev_close=161.79),
        "MSFT": _snap(last_price=400.0, last_trade_at=now, prev_close=440.0),
    }
    p.config = MagicMock()
    p.config.intraday_scan.move_threshold_pct = 3.0
    p.config.intraday_scan.cooldown_hours = 4
    p.config.trading.universe = ["ORCL", "MSFT"]
    p._track_intraday_snapshot_miss = MagicMock()
    p._track_intraday_snapshot_ok = MagicMock()
    p._recently_intraday_evaluated = MagicMock(return_value=False)

    movers, _ = p._intraday_scan_mover_candidates(MagicMock())
    assert [m[0] for m in movers] == ["MSFT"]
    # A thin name that simply has not printed today is QUIET, not BROKEN.
    # The miss counter pages the owner with "check whether the ticker is
    # still valid/tradable on Alpaca" after three consecutive scans, and
    # item 120's own filing names two IEX-thin names in exactly this state
    # — paging on them would be a false alarm.
    p._track_intraday_snapshot_miss.assert_not_called()
    assert ("ORCL",) in [c.args for c in p._track_intraday_snapshot_ok.call_args_list]


def test_a_symbol_the_feed_returned_nothing_for_is_still_a_snapshot_miss():
    """The counter must keep firing for a genuinely absent symbol — the
    fix above must not disable the broken-ticker alarm outright."""
    from src.pipeline import TradingPipeline

    now = SEP17_OPEN.replace(hour=11)
    p = TradingPipeline.__new__(TradingPipeline)
    p.broker = MagicMock()
    p.broker.get_intraday_snapshots.return_value = {
        "MSFT": _snap(last_price=400.0, last_trade_at=now, prev_close=440.0),
        "BADTIX": {},
        # The harder case: the previous close IS known, so the symbol
        # cannot be dismissed on a missing prev_close — the feed simply
        # returned no price of any kind for it. That is broken, not quiet.
        "DEADTIX": _snap(prev_close=101.0),
    }
    p.config = MagicMock()
    p.config.intraday_scan.move_threshold_pct = 3.0
    p.config.intraday_scan.cooldown_hours = 4
    p.config.trading.universe = ["MSFT", "BADTIX", "DEADTIX"]
    p._track_intraday_snapshot_miss = MagicMock()
    p._track_intraday_snapshot_ok = MagicMock()
    p._recently_intraday_evaluated = MagicMock(return_value=False)

    p._intraday_scan_mover_candidates(MagicMock())
    missed = {c.args[0] for c in p._track_intraday_snapshot_miss.call_args_list}
    assert missed == {"BADTIX", "DEADTIX"}


def test_the_cockpit_does_not_render_a_prior_sessions_range_as_this_session(
    monkeypatch,
):
    from src.api import broker_reads

    now = SEP17_OPEN.replace(minute=31)
    monkeypatch.setattr("src.trading_calendar.et_now", lambda: now)
    broker = MagicMock()
    broker.get_intraday_snapshots.return_value = {
        "AAPL": _snap(last_price=230.0, last_trade_at=SEP16_CLOSE,
                      session_bar_at=SEP16_BAR_AT, session_open=229.0,
                      session_high=231.0, session_low=228.0),
    }
    broker.get_session_open.return_value = SEP17_OPEN
    monkeypatch.setattr(broker_reads, "_get_broker", lambda: broker)

    out = broker_reads.read_live_quotes(["AAPL"])["quotes"]["AAPL"]
    assert out["session_bar_is_today"] is False
    assert out["session_open"] is None
    assert out["session_high"] is None
    assert out["session_low"] is None
    assert out["quote"]["freshness"] == "stale"


def test_a_prior_sessions_minute_bar_is_not_used_either():
    """BREAKAGE ATTEMPT — the minute bar needs its OWN date check.

    The `minute_bar` slot carries the last minute bar that EXISTS, which for
    a name that has not printed today is one from a prior session. Skipping
    its freshness check reintroduces the whole defect one field over, and
    every other test still passed when that check was removed.
    """
    r = resolve_live_price(
        _snap(last_price=161.79, last_trade_at=SEP16_CLOSE,
              minute_close=161.79, minute_bar_at=SEP16_CLOSE),
        when=SEP17_OPEN.replace(minute=31),
    )
    assert r.price is None
    assert r.unavailable == ONLY_STALE

    # …and with a today session bar present it is the SESSION BAR that
    # rescues the name, never the prior session's minute bar.
    rescued = resolve_live_price(
        _snap(last_price=161.79, last_trade_at=SEP16_CLOSE,
              minute_close=161.79, minute_bar_at=SEP16_CLOSE,
              session_bar_at=SEP17_BAR_AT, session_close=158.55),
        when=SEP17_OPEN.replace(minute=31),
    )
    assert rescued.price == 158.55
    assert rescued.source == SOURCE_SESSION_BAR


def test_the_tech_prompt_never_falls_back_to_the_raw_provider_field():
    """BREAKAGE ATTEMPT — the `live_price` / `last_price` trap.

    Both keys are present on a rescued symbol's context: `last_price` is the
    raw provider field, still a PRIOR session's print, and `live_price` is
    the resolved one. A reader that prefers `last_price`, or falls back to
    it, prints yesterday. Nothing else in this file caught that.
    """
    msg = _tech_msg({"ORCL": {
        "last_price": 161.79,                      # yesterday, raw
        "last_trade_at": SEP16_CLOSE,
        "live_price": ORCL_LIVE,                   # today, resolved
        "live_price_description": "close of today's still-forming session bar",
        "prev_close": 161.79, "session_open": ORCL_LIVE,
        "session_high": 160.0, "session_low": ORCL_LIVE, "session_volume": 1000,
    }})
    price_line = [
        line for line in msg.splitlines() if "Current price:" in line
    ]
    assert len(price_line) == 1
    # 161.79 is legitimately present on this line as the PRIOR CLOSE the
    # move is measured against; what must not appear is the raw field in
    # the price slot itself.
    assert price_line[0].strip().startswith("Current price: $158.38"), price_line[0]
    assert "Current price: $161.79" not in msg, (
        "the prior session's raw last_price reached the live-price line"
    )

    # And with NO resolved price the section must refuse outright rather
    # than reaching for the raw field.
    lost = _tech_msg({"ORCL": {
        "last_price": 161.79, "last_trade_at": SEP16_CLOSE,
        "live_unavailable": ONLY_STALE,
    }})
    assert "NO PRICE FROM TODAY" in lost
    assert "CURRENT SESSION" not in lost
