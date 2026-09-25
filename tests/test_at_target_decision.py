"""Decision at the take-profit target — owner ruling 2026-09-25.

The owner's lean: "lean towards selling if it hits target; if the chart is
showing higher highs and higher lows you could move up the stop and reassess."
So reaching a real target DEFAULTS to selling (bank the win); the EXCEPTION is a
name clearly still making higher-highs-and-higher-lows, which is held under a
raised trailing stop. These pin all four required behaviours:

  (a) the target is re-derived from current bars each review and only ratchets
      AWAY from entry (see tests/test_target_revision.py's every-review section);
  (b) at-target while still clearly trending up -> HOLD (ride the raised stop);
  (c) at-target while NOT clearly trending up -> SELL (bank it);
  (d) no fixed "sell at X": the sell is conditional on the live swing read off
      the instrument, never the price alone, and never a fixed % gain.

The pipeline-level cases assemble the sell/hold state from REALISTIC daily bars
(a real higher-high/higher-low zig-zag vs. a rolled-over one), not by injecting
a boolean into the dataclass.
"""

from datetime import date, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

from src.models import OHLCV
from src.pipeline import TradingPipeline
from src.risk.exit_guard import (
    AT_TARGET_HOLD_STILL_TRENDING,
    AT_TARGET_INPUTS_UNREADABLE,
    AT_TARGET_NOT_REACHED,
    AT_TARGET_SELL_STALLED,
    decide_at_target,
)


def _bars_from_pivots(pivots, spacing=6):
    """Daily bars whose confirmed swing pivots are exactly `pivots` in order.

    Linear interpolation between pivots makes each a strict local extreme (so
    `_find_pivots` returns one pivot per swing, not a plateau), with a tail that
    trends AWAY from the last pivot so it is confirmed."""
    vals = []
    for a, b in zip(pivots, pivots[1:]):
        for s in range(spacing):
            vals.append(a + (b - a) * s / spacing)
    vals.append(pivots[-1])
    last, prev = pivots[-1], pivots[-2]
    step = -1 if last > prev else 1
    for k in range(1, spacing + 1):
        vals.append(last + step * abs(last - prev) * k / spacing)
    d = date(2026, 1, 1)
    return [
        OHLCV(date=d + timedelta(days=i), open=v, high=v, low=v, close=v,
              volume=1000)
        for i, v in enumerate(vals)
    ]


# A clear uptrend: highs 100->108->115, lows 95->100 (HH + HL).
UPTREND_BARS = _bars_from_pivots([90, 100, 95, 108, 100, 115])
# Rolled over: highs 115->108->103, lows 100->95 (LH + LL).
ROLLED_BARS = _bars_from_pivots([90, 115, 100, 108, 95, 103])


# ---------------------------------------------------------------------------
# Pure decision function
# ---------------------------------------------------------------------------


def test_below_target_is_not_reached_and_never_sells():
    d = decide_at_target(
        symbol="AAPL", is_short=False, close_price=104.0, target_price=110.0,
        still_making_new_highs=False,  # even a stalled chart cannot sell below target
    )
    assert d.reached is False and d.should_sell is False
    assert d.code == AT_TARGET_NOT_REACHED


def test_at_target_still_trending_holds():
    """(b): reached AND clearly still making higher-highs-and-higher-lows -> HOLD."""
    d = decide_at_target(
        symbol="AAPL", is_short=False, close_price=112.0, target_price=110.0,
        still_making_new_highs=True,
    )
    assert d.reached is True and d.should_sell is False
    assert d.code == AT_TARGET_HOLD_STILL_TRENDING
    assert "raises the stop" in d.reason.lower()


def test_at_target_not_trending_sells():
    """(c) + (d): reached AND NOT clearly trending -> SELL (bank it), on the
    trend read, not the number alone."""
    d = decide_at_target(
        symbol="AAPL", is_short=False, close_price=112.0, target_price=110.0,
        still_making_new_highs=False,
    )
    assert d.reached is True and d.should_sell is True
    assert d.code == AT_TARGET_SELL_STALLED
    assert "banking the win" in d.reason.lower()


def test_at_target_unclear_structure_defaults_to_sell():
    """The owner's lean: bank a win at a real target unless the chart is CLEARLY
    still running. Too little structure to tell -> SELL."""
    d = decide_at_target(
        symbol="AAPL", is_short=False, close_price=112.0, target_price=110.0,
        still_making_new_highs=None,
    )
    assert d.should_sell is True
    assert d.code == AT_TARGET_SELL_STALLED


def test_short_mirror():
    # A short's target sits BELOW; reached when the close falls to/through it.
    hold = decide_at_target(
        symbol="AAPL", is_short=True, close_price=90.0, target_price=92.0,
        still_making_new_highs=True,  # clearly still lower-highs/lower-lows
    )
    assert hold.reached is True and hold.should_sell is False
    sell = decide_at_target(
        symbol="AAPL", is_short=True, close_price=90.0, target_price=92.0,
        still_making_new_highs=False,
    )
    assert sell.should_sell is True
    not_yet = decide_at_target(
        symbol="AAPL", is_short=True, close_price=95.0, target_price=92.0,
        still_making_new_highs=False,
    )
    assert not_yet.reached is False and not_yet.should_sell is False


def test_unreadable_inputs_never_sell():
    d = decide_at_target(
        symbol="AAPL", is_short=False, close_price=None, target_price=110.0,
        still_making_new_highs=False,
    )
    assert d.code == AT_TARGET_INPUTS_UNREADABLE and d.should_sell is False


# ---------------------------------------------------------------------------
# Pipeline wiring — the deterministic exit, driven by REAL bars
# ---------------------------------------------------------------------------


def _pipeline(position, *, buy, bars):
    p = TradingPipeline.__new__(TradingPipeline)
    p.db = MagicMock()
    p.db.get_symbol_last_buy.return_value = buy
    p.market = MagicMock()
    p.market.get_ohlcv.return_value = bars
    p.config = SimpleNamespace(trading=SimpleNamespace(lookback_days=200))
    p._voice_at_target_decision = MagicMock()
    p._submit_protected_sell = MagicMock(
        return_value=({"id": "o1", "action": "SELL"}, None)
    )
    p._finalize_pending_protections = MagicMock()
    return p


def _long(qty=100.0, entry=90.0, price=130.0):
    return SimpleNamespace(
        symbol="AAPL", qty=qty, avg_entry=entry, current_price=price,
    )


def test_pipeline_at_target_still_trending_does_not_sell():
    """(b) end to end from real bars: reached AND the bars make higher-highs-
    and-higher-lows -> HOLD, no order, no sell submitted."""
    close = UPTREND_BARS[-1].close
    p = _pipeline(
        _long(), buy={"take_profit": close - 1.0, "stop_loss": 80.0},
        bars=UPTREND_BARS,
    )
    order = p._decide_one_at_target(_long(), "AAPL", run_id="r", seat="s")
    assert order is None
    p._submit_protected_sell.assert_not_called()
    p._voice_at_target_decision.assert_called_once()
    assert p._voice_at_target_decision.call_args.kwargs["decision"].code == (
        AT_TARGET_HOLD_STILL_TRENDING
    )


def test_pipeline_at_target_rolled_over_sells():
    """(c) end to end from real bars: reached AND the bars are NO LONGER making
    higher-highs-and-higher-lows -> SELL is submitted."""
    close = ROLLED_BARS[-1].close
    p = _pipeline(
        _long(), buy={"take_profit": close - 1.0, "stop_loss": 80.0},
        bars=ROLLED_BARS,
    )
    order = p._decide_one_at_target(_long(), "AAPL", run_id="r", seat="s")
    assert order == {"id": "o1", "action": "SELL"}
    p._submit_protected_sell.assert_called_once()
    kwargs = p._submit_protected_sell.call_args.kwargs
    assert kwargs["label"] == "SELL" and kwargs["side"] == "sell"
    assert kwargs["qty"] == 100.0
    p.db.insert_trade.assert_called_once()
    assert p.db.insert_trade.call_args.kwargs["action"] == "SELL"
    assert p._voice_at_target_decision.call_args.kwargs["decision"].code == (
        AT_TARGET_SELL_STALLED
    )


def test_pipeline_below_target_never_sells():
    """(d): below the target there is nothing to decide, whatever the trend."""
    close = ROLLED_BARS[-1].close
    p = _pipeline(
        _long(), buy={"take_profit": close + 50.0, "stop_loss": 80.0},
        bars=ROLLED_BARS,
    )
    order = p._decide_one_at_target(_long(), "AAPL", run_id="r", seat="s")
    assert order is None
    p._submit_protected_sell.assert_not_called()


def test_pipeline_no_stored_target_is_a_no_op():
    p = _pipeline(_long(), buy={"stop_loss": 80.0}, bars=ROLLED_BARS)
    order = p._decide_one_at_target(_long(), "AAPL", run_id="r", seat="s")
    assert order is None
    p._submit_protected_sell.assert_not_called()


def test_pipeline_reuses_one_bars_fetch_across_the_review():
    """The re-derivation and the at-target decision must share ONE bars fetch
    per symbol per review, not download twice."""
    p = _pipeline(
        _long(), buy={"take_profit": 50.0, "stop_loss": 80.0}, bars=ROLLED_BARS,
    )
    p._review_bars_cache = {}
    first = p._review_ohlcv("AAPL")
    second = p._review_ohlcv("AAPL")
    assert first is second
    p.market.get_ohlcv.assert_called_once()


def test_at_target_sweep_skips_already_sold_symbols():
    p = _pipeline(
        _long(), buy={"take_profit": 50.0, "stop_loss": 80.0}, bars=ROLLED_BARS,
    )
    orders = p._decide_at_target_exits(
        [_long()], "r", seat="s", sold_symbols={"AAPL"},
    )
    assert orders == []
    p._submit_protected_sell.assert_not_called()
