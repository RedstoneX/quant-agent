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

from src.data.levels import _find_pivots, making_higher_highs_and_lows
from src.models import OHLCV
from src.pipeline import TradingPipeline
from src.risk.exit_guard import (
    AT_TARGET_HOLD_STILL_TRENDING,
    AT_TARGET_INPUTS_UNREADABLE,
    AT_TARGET_NOT_REACHED,
    AT_TARGET_SELL_STALLED,
    AT_TARGET_TRAILING_STOP_ONLY,
    AT_TARGET_TRAILING_STOP_ONLY_LATCHED,
    decide_at_target,
    target_cannot_extend,
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


# ---------------------------------------------------------------------------
# The swing read itself: confirmation lag, and a real Dow sequence
# ---------------------------------------------------------------------------


def _zig(pivots, spacing=6):
    """The raw price path of `_bars_from_pivots`, so a tail can be appended."""
    vals = []
    for a, b in zip(pivots, pivots[1:]):
        for s in range(spacing):
            vals.append(a + (b - a) * s / spacing)
    vals.append(pivots[-1])
    last, prev = pivots[-1], pivots[-2]
    step = -1 if last > prev else 1
    for k in range(1, spacing + 1):
        vals.append(last + step * abs(last - prev) * k / spacing)
    return vals


def _bars(vals):
    d = date(2026, 1, 1)
    return [
        OHLCV(date=d + timedelta(days=i), open=v, high=v, low=v, close=v,
              volume=1000)
        for i, v in enumerate(vals)
    ]


# A base that has ROLLED OVER on confirmed pivots alone: highs 108 -> 106.
_CONSOLIDATION = _zig([90, 108, 100, 106, 101])
# The same chart with a fresh thrust clean through 108 on the last four bars —
# too recent for any pivot to confirm.
_FRESH_BREAKOUT = _CONSOLIDATION + [104, 108, 112, 115]


def test_confirmed_only_read_calls_a_fresh_breakout_not_trending():
    """The defect this guards, stated as the pre-condition: on the CONFIRMED
    pivots alone this chart is making lower highs."""
    assert making_higher_highs_and_lows(_bars(_CONSOLIDATION)) is False


def test_an_in_progress_higher_high_counts_before_it_is_confirmed():
    """A long that reaches a resistance TARGET does so on a breakout with no
    bars after it yet, so the newest CONFIRMED swing high is the pre-breakout
    peak. The thrust that just hit the target must count, or the gate sells the
    cleanest breakouts for want of five more sessions."""
    assert making_higher_highs_and_lows(_bars(_FRESH_BREAKOUT)) is True


def test_the_in_progress_extreme_must_actually_exceed_the_last_swing():
    """No threshold and no tolerance: a thrust that stops SHORT of the last
    confirmed swing high is not a higher high."""
    stops_short = _CONSOLIDATION + [104, 105, 105.5, 105.9]
    assert making_higher_highs_and_lows(_bars(stops_short)) is False


def test_only_the_thrusting_side_is_read_in_progress():
    """For a long the HIGH side may be in progress; the LOW side stays
    confirmed-only, because a pullback low that has not held is not evidence.
    Here the fresh high is real but the confirmed lows are FALLING."""
    falling_lows = _zig([90, 108, 100, 106, 95]) + [104, 110, 114, 118]
    assert making_higher_highs_and_lows(_bars(falling_lows)) is False


def test_short_mirror_counts_an_in_progress_lower_low():
    inverted = [-v for v in _FRESH_BREAKOUT]
    assert making_higher_highs_and_lows(_bars(inverted), is_short=True) is True
    assert making_higher_highs_and_lows(_bars(inverted)) is not True


def test_compared_swings_are_a_real_alternating_sequence():
    """Two swing highs with no swing low between them are ONE leg, not two
    comparable highs. The reduction keeps the more extreme of the pair, so what
    is compared always interleaves in time — which is what makes it Dow."""
    from src.data.levels import _alternating_swings

    seq = _alternating_swings([
        (0, 100.0, "R"), (10, 105.0, "R"), (20, 95.0, "S"), (30, 110.0, "R"),
    ])
    assert [k for (_i, _p, k) in seq] == ["R", "S", "R"]
    assert [p for (_i, p, _k) in seq] == [105.0, 95.0, 110.0]
    # And it alternates for real bars too, on both sides.
    for bars in (UPTREND_BARS, ROLLED_BARS, _bars(_FRESH_BREAKOUT)):
        kinds = [k for (_i, _p, k) in _alternating_swings(_find_pivots(bars, 5))]
        assert all(a != b for a, b in zip(kinds, kinds[1:]))


def test_a_bar_that_is_both_a_high_and_a_low_carries_no_swing_information():
    from src.data.levels import _alternating_swings

    seq = _alternating_swings([
        (5, 100.0, "R"), (5, 100.0, "S"), (10, 90.0, "S"), (20, 110.0, "R"),
    ])
    assert [(i, k) for (i, _p, k) in seq] == [(10, "S"), (20, "R")]


# ---------------------------------------------------------------------------
# Leaving the vote when the target can no longer move
# ---------------------------------------------------------------------------


def test_hold_with_no_ceiling_in_reach_leaves_the_at_target_vote():
    d = decide_at_target(
        symbol="AAPL", is_short=False, close_price=120.0, target_price=115.0,
        still_making_new_highs=True, target_can_extend=False,
    )
    assert d.code == AT_TARGET_TRAILING_STOP_ONLY
    assert d.should_sell is False
    assert "trailing stop" in d.reason


def test_a_hold_with_a_target_that_can_still_extend_stays_in_the_vote():
    d = decide_at_target(
        symbol="AAPL", is_short=False, close_price=120.0, target_price=115.0,
        still_making_new_highs=True, target_can_extend=True,
    )
    assert d.code == AT_TARGET_HOLD_STILL_TRENDING


def test_no_ceiling_in_reach_can_never_turn_a_sell_into_a_hold():
    """It only ever applies on a HOLD. A stalled chart at its target is still
    banked, ceiling or no ceiling."""
    d = decide_at_target(
        symbol="AAPL", is_short=False, close_price=120.0, target_price=115.0,
        still_making_new_highs=False, target_can_extend=False,
    )
    assert d.code == AT_TARGET_SELL_STALLED and d.should_sell is True


def test_a_latched_position_is_not_re_put_to_the_vote_and_is_not_re_voiced():
    d = decide_at_target(
        symbol="AAPL", is_short=False, close_price=120.0, target_price=115.0,
        still_making_new_highs=False, already_trailing_only=True,
    )
    assert d.code == AT_TARGET_TRAILING_STOP_ONLY_LATCHED
    assert d.should_sell is False
    assert d.reason == ""


def test_only_named_refusals_mean_the_target_cannot_extend():
    assert target_cannot_extend("REFUSAL_NO_STRUCTURE_LEFT_IN_DIRECTION") is True
    assert target_cannot_extend("REFUSAL_DERIVED_TARGET_BEHIND_PRICE") is True
    # A re-derivation that MOVED the target, a blank, and an unknown code all
    # leave the position in the vote.
    assert target_cannot_extend("EACH_REVIEW_STRUCTURAL_REREAD") is False
    assert target_cannot_extend("") is False
    assert target_cannot_extend(None) is False


def test_pipeline_records_a_durable_reason_when_it_leaves_the_vote():
    close = UPTREND_BARS[-1].close
    p = _pipeline(
        _long(), buy={"take_profit": close - 1.0, "stop_loss": 80.0},
        bars=UPTREND_BARS,
    )
    p.db.get_at_target_management.return_value = {}
    p._review_target_outcomes = {
        "AAPL": {"code": "REFUSAL_NO_STRUCTURE_LEFT_IN_DIRECTION"},
    }
    order = p._decide_one_at_target(_long(), "AAPL", run_id="r", seat="s")
    assert order is None
    p._submit_protected_sell.assert_not_called()
    p.db.record_at_target_management.assert_called_once()
    kwargs = p.db.record_at_target_management.call_args.kwargs
    assert kwargs["trailing_only"] is True
    assert kwargs["code"] == AT_TARGET_TRAILING_STOP_ONLY
    assert kwargs["detail"]


def test_pipeline_does_not_leave_the_vote_while_the_target_can_still_extend():
    close = UPTREND_BARS[-1].close
    p = _pipeline(
        _long(), buy={"take_profit": close - 1.0, "stop_loss": 80.0},
        bars=UPTREND_BARS,
    )
    p.db.get_at_target_management.return_value = {}
    p._review_target_outcomes = {"AAPL": {"code": "EACH_REVIEW_STRUCTURAL_REREAD"}}
    p._decide_one_at_target(_long(), "AAPL", run_id="r", seat="s")
    p.db.record_at_target_management.assert_not_called()
    assert p._voice_at_target_decision.call_args.kwargs["decision"].code == (
        AT_TARGET_HOLD_STILL_TRENDING
    )


def test_pipeline_latched_position_never_reaches_the_full_close_vote():
    """The defect: `reached` never goes back to False, so a rolled-over read on
    ANY later review would otherwise close the whole position."""
    close = ROLLED_BARS[-1].close
    p = _pipeline(
        _long(), buy={"take_profit": close - 1.0, "stop_loss": 80.0,
                      "timestamp": "2026-01-01 10:00:00"},
        bars=ROLLED_BARS,
    )
    p.db.get_at_target_management.return_value = {
        "AAPL": {"trailing_only": True, "timestamp": "2026-02-01 10:00:00"},
    }
    order = p._decide_one_at_target(_long(), "AAPL", run_id="r", seat="s")
    assert order is None
    p._submit_protected_sell.assert_not_called()


def test_a_latch_from_before_this_position_opened_does_not_count():
    """A re-entry in the same ticker is a different position and starts in the
    vote."""
    close = ROLLED_BARS[-1].close
    p = _pipeline(
        _long(), buy={"take_profit": close - 1.0, "stop_loss": 80.0,
                      "timestamp": "2026-03-01 10:00:00"},
        bars=ROLLED_BARS,
    )
    p.db.get_at_target_management.return_value = {
        "AAPL": {"trailing_only": True, "timestamp": "2026-02-01 10:00:00"},
    }
    order = p._decide_one_at_target(_long(), "AAPL", run_id="r", seat="s")
    assert order == {"id": "o1", "action": "SELL"}


def test_an_extended_target_re_arms_the_vote():
    p = _pipeline(_long(), buy={}, bars=ROLLED_BARS)
    p.db.get_at_target_management.return_value = {
        "AAPL": {"trailing_only": True, "timestamp": "2026-02-01 10:00:00"},
    }
    p._rearm_at_target_vote(sym="AAPL", run_id="r")
    p.db.record_at_target_management.assert_called_once()
    assert p.db.record_at_target_management.call_args.kwargs["trailing_only"] is False


def test_re_arm_writes_nothing_when_there_is_nothing_to_clear():
    p = _pipeline(_long(), buy={}, bars=ROLLED_BARS)
    p.db.get_at_target_management.return_value = {}
    p._rearm_at_target_vote(sym="AAPL", run_id="r")
    p.db.record_at_target_management.assert_not_called()
