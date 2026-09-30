"""Decision at the take-profit target — owner ruling 2026-09-25.

The owner's lean: "lean towards selling if it hits target; if the chart is
showing higher highs and higher lows you could move up the stop and reassess."
Reaching a real target DEFAULTS to a full sell; the exception is a target that
has DECISIVELY BROKEN on the desk's existing level-break standard (two
consecutive closes beyond it by the one break margin), in which case the desk
holds, the target extends, and the trailing stop carries the runner.

These pin:

  (a) the pure rule, both sides;
  (b) the two failures that killed three earlier cuts, as REVIEW SEQUENCES:
      a bleed cannot be held through, and a single print cannot produce a
      hold past the target;
  (c) the pipeline wiring on REAL daily bars through the real break test —
      the sell path, the reach latch, the shared reads, the voicing rule.
"""

from datetime import date, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src.models import OHLCV
from src.pipeline import TradingPipeline
from src.risk.exit_guard import (
    AT_TARGET_HOLD_BREAK_CONFIRMED,
    AT_TARGET_HOLD_BREAK_PENDING,
    AT_TARGET_INPUTS_UNREADABLE,
    AT_TARGET_NOT_REACHED,
    AT_TARGET_SELL_STALLED,
    BREAK_CONFIRMATION_ATR_MULTIPLE,
    decide_at_target,
)
from src.risk.target_revision import target_level_broken


# ---------------------------------------------------------------------------
# (a) The pure rule
# ---------------------------------------------------------------------------


def _d(**kw):
    args = dict(
        symbol="aapl", is_short=False, close_price=100.0, target_price=100.0,
        reached_before=False, broken_today=False, broken_prior_close=False,
    )
    args.update(kw)
    return decide_at_target(**args)


def test_below_target_never_reached_is_nothing_to_decide():
    d = _d(close_price=95.0, target_price=100.0)
    assert d.code == AT_TARGET_NOT_REACHED
    assert d.reached is False and d.should_sell is False
    assert d.symbol == "AAPL"


def test_reached_and_not_broken_sells_by_default():
    d = _d(close_price=100.5, target_price=100.0, broken_today=False)
    assert d.code == AT_TARGET_SELL_STALLED
    assert d.should_sell is True and d.reached is True
    assert "banking the win" in d.reason


def test_first_decisive_close_holds_one_session_pending_confirmation():
    d = _d(close_price=104.0, target_price=100.0, broken_today=True)
    assert d.code == AT_TARGET_HOLD_BREAK_PENDING
    assert d.should_sell is False
    assert "ONE more session" in d.reason


def test_two_consecutive_decisive_closes_hold_the_runner():
    d = _d(close_price=105.0, target_price=100.0, broken_today=True,
           broken_prior_close=True)
    assert d.code == AT_TARGET_HOLD_BREAK_CONFIRMED
    assert d.should_sell is False
    assert "second consecutive session" in d.reason


def test_a_decisive_close_that_fails_to_confirm_is_banked():
    d = _d(close_price=100.4, target_price=100.0, broken_today=False,
           broken_prior_close=True)
    assert d.code == AT_TARGET_SELL_STALLED and d.should_sell is True
    assert "did not confirm" in d.reason


def test_a_poke_through_the_target_that_closed_back_inside_is_banked():
    """Reach is one-way for a target: touched it earlier, now closed back
    below it -> that is a failure to hold beyond the target, a SELL, not a
    return to 'nothing to decide'."""
    d = _d(close_price=98.0, target_price=100.0, reached_before=True,
           broken_today=False)
    assert d.code == AT_TARGET_SELL_STALLED and d.should_sell is True
    assert "closed back below it" in d.reason


def test_short_mirror():
    below = _d(is_short=True, close_price=80.0, target_price=80.5,
               broken_today=False)
    assert below.code == AT_TARGET_SELL_STALLED
    above = _d(is_short=True, close_price=81.0, target_price=80.5)
    assert above.code == AT_TARGET_NOT_REACHED
    run = _d(is_short=True, close_price=75.0, target_price=80.5,
             broken_today=True, broken_prior_close=True)
    assert run.code == AT_TARGET_HOLD_BREAK_CONFIRMED
    back = _d(is_short=True, close_price=82.0, target_price=80.5,
              reached_before=True, broken_today=False)
    assert back.should_sell is True and "closed back above it" in back.reason


def test_unreadable_inputs_never_sell():
    for kw in ({"close_price": None}, {"target_price": None},
               {"target_price": 0.0}, {"close_price": -1.0}):
        d = _d(**kw)
        assert d.code == AT_TARGET_INPUTS_UNREADABLE and d.should_sell is False
    # Reached, but the break test had no ATR: a data fault, filed with a
    # reason, never a sell and never read as "not broken".
    d = _d(close_price=101.0, target_price=100.0, broken_today=None)
    assert d.code == AT_TARGET_INPUTS_UNREADABLE
    assert d.reached is True and d.should_sell is False
    assert "no volatility reading" in d.reason


# ---------------------------------------------------------------------------
# (b) The two failures that killed the swing read, as review sequences.
#
# Each review re-runs the rule on that day's completed close with the real
# break test (`target_level_broken`: beyond the target by one break margin)
# and the prior day's flag, exactly as the pipeline feeds it. `reached` is
# latched once true, as the pipeline persists it.
# ---------------------------------------------------------------------------


def _reviews(closes, *, target, atr, is_short=False):
    """Run the rule over consecutive daily closes; return the codes."""
    codes = []
    reached_before = False
    prior = False
    for close in closes:
        broken = target_level_broken(
            target_level=target, close_price=close, atr=atr, is_short=is_short,
        )
        d = decide_at_target(
            symbol="X", is_short=is_short, close_price=close,
            target_price=target, reached_before=reached_before,
            broken_today=broken, broken_prior_close=prior,
        )
        codes.append(d.code)
        reached_before = reached_before or d.reached
        prior = bool(broken)
        if d.should_sell:
            break
    return codes


def test_a_fifteen_day_bleed_cannot_be_held_through():
    """The bleed objection. A long reaches and breaks its 100 target (ATR 2,
    margin 2), runs to 108, then bleeds for fifteen sessions. The rule must
    SELL on the first close that is no longer decisively beyond the target,
    never sit on HOLD through the bleed."""
    atr = 2.0
    run_up = [103.0, 104.0, 108.0]
    bleed = [108.0 - 0.6 * k for k in range(1, 16)]  # 107.4 ... 99.0
    codes = _reviews(run_up + bleed, target=100.0, atr=atr)
    assert codes[:3] == [
        AT_TARGET_HOLD_BREAK_PENDING, AT_TARGET_HOLD_BREAK_CONFIRMED,
        AT_TARGET_HOLD_BREAK_CONFIRMED,
    ]
    assert codes[-1] == AT_TARGET_SELL_STALLED
    # Sold at the first close inside the margin (101.4 is under 100 + 2).
    first_inside = next(
        i for i, c in enumerate(run_up + bleed)
        if c < 100.0 + BREAK_CONFIRMATION_ATR_MULTIPLE * atr
    )
    assert len(codes) - 1 == first_inside
    assert len(codes) < len(run_up) + 15


def test_a_straight_drop_through_the_target_after_a_touch_is_banked():
    """Touch the target once, then fall straight through it: the latched
    reach puts every later review to the decision, and the first one sells."""
    codes = _reviews([100.2, 99.0, 97.0, 95.0], target=100.0, atr=2.0)
    assert codes == [AT_TARGET_SELL_STALLED]  # sold on the touch itself
    # And if the touch was itself decisive but the next close is not:
    codes = _reviews([103.0, 99.0, 97.0], target=100.0, atr=2.0)
    assert codes == [AT_TARGET_HOLD_BREAK_PENDING, AT_TARGET_SELL_STALLED]


def test_a_single_print_cannot_produce_a_hold_past_the_target():
    """The bad-print objection. One spurious close far beyond the target buys
    at most one session of HOLD (pending); the next real close decides. It
    can never produce the confirmed hold, and a print that vanishes leaves
    the position banked at its target, not held."""
    # Real closes sit just at the target; one review sees a phantom 110.
    codes = _reviews([99.0, 110.0, 100.3], target=100.0, atr=2.0)
    assert codes == [
        AT_TARGET_NOT_REACHED, AT_TARGET_HOLD_BREAK_PENDING,
        AT_TARGET_SELL_STALLED,
    ]
    # The confirmed hold needs two consecutive decisive closes by construction.
    for prior in (False, True):
        d = decide_at_target(
            symbol="X", is_short=False, close_price=110.0, target_price=100.0,
            reached_before=False, broken_today=True, broken_prior_close=prior,
        )
        assert (d.code == AT_TARGET_HOLD_BREAK_CONFIRMED) is prior
    # A phantom print that is BELOW the target changes nothing at all.
    assert _reviews([99.0, 90.0, 99.5], target=100.0, atr=2.0) == [
        AT_TARGET_NOT_REACHED] * 3


def test_a_real_breakout_is_held_and_a_reclaim_resets_the_count():
    """Two decisive closes hold; a reclaim between two decisive closes does
    not count toward confirmation (Edwards & Magee's reclaim = spring), and
    the reclaim itself is a SELL because the target was reached."""
    assert _reviews([103.0, 103.5, 104.0], target=100.0, atr=2.0) == [
        AT_TARGET_HOLD_BREAK_PENDING, AT_TARGET_HOLD_BREAK_CONFIRMED,
        AT_TARGET_HOLD_BREAK_CONFIRMED,
    ]
    assert _reviews([103.0, 101.0, 103.5], target=100.0, atr=2.0) == [
        AT_TARGET_HOLD_BREAK_PENDING, AT_TARGET_SELL_STALLED,
    ]


# ---------------------------------------------------------------------------
# (c) Pipeline wiring — the deterministic exit, driven by REAL bars through
#     the real break test (ATR from `compute_indicators`)
# ---------------------------------------------------------------------------


def _bars(closes, *, half_range=1.0):
    """Daily bars with a constant true range of ~2*half_range, so ATR(14) is
    readable and known (about 2.0 for the default)."""
    d = date(2026, 1, 1)
    out = []
    for i, c in enumerate(closes):
        out.append(OHLCV(
            date=d + timedelta(days=i), open=c, high=c + half_range,
            low=c - half_range, close=c, volume=1000,
        ))
    return out


# Sixty flat-ish sessions ending on the close under test; ATR(14) ~ 2.0.
def _history(last_close, *, prior_close=None):
    closes = [100.0 + (i % 3) * 0.2 for i in range(58)]
    closes.append(prior_close if prior_close is not None else last_close)
    closes.append(last_close)
    return _bars(closes)


def _pipeline(*, buy, bars, prior_break=None, reached_row=None,
              last_decision=None):
    p = TradingPipeline.__new__(TradingPipeline)
    p.db = MagicMock()
    p.db.AT_TARGET_DECISION_KIND = "at_target_decision"
    p.db.get_symbol_last_buy.return_value = buy
    p.db.get_prior_target_level_break.return_value = (
        {"AAPL": prior_break} if prior_break is not None else {}
    )
    p.db.get_at_target_reached.return_value = (
        {"AAPL": reached_row} if reached_row else {}
    )
    p.db.get_last_at_target_decision.return_value = (
        {"AAPL": last_decision} if last_decision else {}
    )
    p.market = MagicMock()
    p.market.get_ohlcv.return_value = bars
    p.config = SimpleNamespace(trading=SimpleNamespace(lookback_days=200))
    p._submit_protected_sell = MagicMock(
        return_value=({"id": "o1", "action": "SELL"}, None)
    )
    p._finalize_pending_protections = MagicMock()
    p._reset_review_caches()
    return p


def _long(qty=100.0, entry=90.0, price=101.0):
    return SimpleNamespace(
        symbol="AAPL", qty=qty, avg_entry=entry, current_price=price,
    )


def _short(qty=-100.0, entry=110.0, price=99.0):
    return SimpleNamespace(
        symbol="AAPL", qty=qty, avg_entry=entry, current_price=price,
    )


BUY = {"take_profit": 100.0, "stop_loss": 80.0, "timestamp": "2026-01-01T15:00:00"}


def _voiced_code(p):
    rows = [
        c.kwargs for c in p.db.insert_specialist_evidence.call_args_list
        if c.kwargs.get("kind") == "at_target_decision"
    ]
    assert rows, "no at-target decision row was filed"
    import json
    return json.loads(rows[-1]["evidence_json"])["code"]


def test_pipeline_close_at_target_not_broken_sells_in_full(monkeypatch):
    monkeypatch.setattr("src.notifier.send_owner_alert", MagicMock(return_value=True))
    p = _pipeline(buy=BUY, bars=_history(100.5))
    order = p._decide_one_at_target(_long(), "AAPL", run_id="r", seat="s")
    assert order == {"id": "o1", "action": "SELL"}
    kwargs = p._submit_protected_sell.call_args.kwargs
    assert kwargs["label"] == "SELL" and kwargs["side"] == "sell"
    assert kwargs["qty"] == 100.0 and kwargs["position_qty_before_sell"] == 100.0
    # Priced off the broker's live quote, never the close print.
    assert kwargs["reference_price"] == 101.0
    assert kwargs["limit_price"] == round(101.0 * 0.995, 2)
    assert p.db.insert_trade.call_args.kwargs["action"] == "SELL"
    assert "At-target reassessment" in p.db.insert_trade.call_args.kwargs["reasoning"]
    assert _voiced_code(p) == AT_TARGET_SELL_STALLED
    # The reach is filed durably, at this target, dated by the bar.
    p.db.record_at_target_reached.assert_called_once()
    rec = p.db.record_at_target_reached.call_args.kwargs
    assert rec["target_price"] == 100.0 and rec["bar_date"] == "2026-03-01"
    # Today's break flag is filed against the target for tomorrow to confirm.
    p.db.save_target_level_break.assert_called_once()
    assert p.db.save_target_level_break.call_args.kwargs["raw_broken"] is False


def test_pipeline_first_decisive_close_holds_pending(monkeypatch):
    monkeypatch.setattr("src.notifier.send_owner_alert", MagicMock(return_value=True))
    p = _pipeline(buy=BUY, bars=_history(104.0, prior_close=100.2))
    order = p._decide_one_at_target(_long(price=104.5), "AAPL", run_id="r", seat="s")
    assert order is None
    p._submit_protected_sell.assert_not_called()
    assert _voiced_code(p) == AT_TARGET_HOLD_BREAK_PENDING
    assert p.db.save_target_level_break.call_args.kwargs["raw_broken"] is True


def test_pipeline_confirmed_break_holds_the_runner(monkeypatch):
    monkeypatch.setattr("src.notifier.send_owner_alert", MagicMock(return_value=True))
    p = _pipeline(buy=BUY, bars=_history(105.0, prior_close=104.0), prior_break=True)
    order = p._decide_one_at_target(_long(price=105.5), "AAPL", run_id="r", seat="s")
    assert order is None
    p._submit_protected_sell.assert_not_called()
    assert _voiced_code(p) == AT_TARGET_HOLD_BREAK_CONFIRMED


def test_pipeline_below_target_with_no_latch_is_silent():
    p = _pipeline(buy=BUY, bars=_history(97.0))
    order = p._decide_one_at_target(_long(price=97.0), "AAPL", run_id="r", seat="s")
    assert order is None
    p._submit_protected_sell.assert_not_called()
    p.db.record_at_target_reached.assert_not_called()
    assert not [
        c for c in p.db.insert_specialist_evidence.call_args_list
        if c.kwargs.get("kind") == "at_target_decision"
    ]


def test_pipeline_a_latched_reach_sells_after_a_close_back_inside(monkeypatch):
    """Touched the target on an earlier close (durable row, same target,
    newer than the opening row); today's close is back below it -> SELL."""
    monkeypatch.setattr("src.notifier.send_owner_alert", MagicMock(return_value=True))
    p = _pipeline(
        buy=BUY, bars=_history(98.0),
        reached_row={"target_price": 100.0, "bar_date": "2026-02-26",
                     "timestamp": "2026-02-26T21:00:00"},
    )
    order = p._decide_one_at_target(_long(price=98.0), "AAPL", run_id="r", seat="s")
    assert order == {"id": "o1", "action": "SELL"}
    assert _voiced_code(p) == AT_TARGET_SELL_STALLED
    # Already latched: not filed twice.
    p.db.record_at_target_reached.assert_not_called()


def test_pipeline_a_latch_for_a_different_target_or_older_position_is_ignored():
    # Extended target: the row carries the OLD price -> starts unreached.
    p = _pipeline(
        buy=BUY, bars=_history(98.0),
        reached_row={"target_price": 95.0, "bar_date": "2026-02-26",
                     "timestamp": "2026-02-26T21:00:00"},
    )
    assert p._decide_one_at_target(_long(price=98.0), "AAPL", run_id="r", seat="s") is None
    # Same price but from before this position opened: a re-entry starts clean.
    p = _pipeline(
        buy=BUY, bars=_history(98.0),
        reached_row={"target_price": 100.0, "bar_date": "2025-12-20",
                     "timestamp": "2025-12-20T21:00:00"},
    )
    assert p._decide_one_at_target(_long(price=98.0), "AAPL", run_id="r", seat="s") is None
    p._submit_protected_sell.assert_not_called()


def test_pipeline_short_mirror_covers_in_full(monkeypatch):
    monkeypatch.setattr("src.notifier.send_owner_alert", MagicMock(return_value=True))
    closes = [110.0 - (i % 3) * 0.2 for i in range(59)] + [99.6]
    p = _pipeline(buy={"take_profit": 100.0, "timestamp": "2026-01-01T15:00:00"},
                  bars=_bars(closes))
    p._submit_protected_sell.return_value = ({"id": "o2", "action": "COVER"}, None)
    order = p._decide_one_at_target(_short(price=99.5), "AAPL", run_id="r", seat="s")
    assert order == {"id": "o2", "action": "COVER"}
    kwargs = p._submit_protected_sell.call_args.kwargs
    assert kwargs["label"] == "COVER" and kwargs["side"] == "buy"
    assert kwargs["qty"] == 100.0
    assert kwargs["limit_price"] == round(99.5 * 1.005, 2)


def test_pipeline_no_stored_target_is_a_no_op():
    p = _pipeline(buy={"stop_loss": 80.0}, bars=_history(100.5))
    assert p._decide_one_at_target(_long(), "AAPL", run_id="r", seat="s") is None
    p._submit_protected_sell.assert_not_called()


def test_at_target_sweep_skips_already_sold_symbols():
    p = _pipeline(buy=BUY, bars=_history(100.5))
    orders = p._decide_at_target_exits(
        [_long()], "r", seat="s", sold_symbols={"AAPL"},
    )
    assert orders == []
    p._submit_protected_sell.assert_not_called()


def test_at_target_sweep_files_a_durable_reason_when_a_symbol_raises():
    p = _pipeline(buy=BUY, bars=_history(100.5))
    p._decide_one_at_target = MagicMock(side_effect=RuntimeError("boom"))
    assert p._decide_at_target_exits([_long()], "r", seat="s") == []
    rows = [
        c.kwargs for c in p.db.insert_specialist_evidence.call_args_list
        if c.kwargs.get("kind") == "at_target_decision"
    ]
    assert rows and "FAULT_AT_TARGET_RAISED" in rows[-1]["evidence_json"]


def test_pipeline_reuses_one_bars_fetch_and_the_rederivations_reads():
    """One download per symbol per review, and the decision tests the SAME
    close / ATR / break flags the re-derivation read and filed."""
    p = _pipeline(buy=BUY, bars=_history(100.5))
    first = p._review_ohlcv("AAPL")
    second = p._review_ohlcv("AAPL")
    assert first is second
    p.market.get_ohlcv.assert_called_once()

    # The re-derivation left its reads for this target: used as-is, with no
    # second break-flag write and no second prior-close read.
    p._review_target_reads["AAPL"] = {
        "close_price": 100.5, "bar_date": "2026-02-28", "atr": 2.0,
        "target_before": 100.0, "target_after": 100.0, "break_ref": 100.0,
        "broken_today": False, "broken_prior_close": False,
        "opened_at": BUY["timestamp"],
    }
    read = p._at_target_read(
        "AAPL", buy=BUY, is_short=False, target_price=100.0, run_id="r",
    )
    assert read is p._review_target_reads["AAPL"]
    p.db.save_target_level_break.assert_not_called()
    p.db.get_prior_target_level_break.assert_not_called()

    # A target that moved this review is NOT served the stale reads.
    read = p._at_target_read(
        "AAPL", buy=BUY, is_short=False, target_price=120.0, run_id="r",
    )
    assert read is not p._review_target_reads["AAPL"]
    assert read["target_after"] == 120.0
    p.market.get_ohlcv.assert_called_once()  # still one fetch


def test_the_per_review_caches_are_reset_at_the_call_site():
    p = TradingPipeline.__new__(TradingPipeline)
    p._review_bars_cache = {"AAPL": [1]}
    p._review_target_reads = {"AAPL": {"x": 1}}
    p._reset_review_caches()
    assert p._review_bars_cache == {} and p._review_target_reads == {}
    assert p._review_bars_date is None


def test_voicing_files_every_review_but_pages_only_on_news(monkeypatch):
    """A confirmed hold repeated on the same target is a board row, not a
    second Telegram page; a SELL always pages; a changed state pages."""
    sent = MagicMock(return_value=True)
    monkeypatch.setattr("src.notifier.send_owner_alert", sent)
    from src.risk.exit_guard import AtTargetDecision

    hold = AtTargetDecision(
        symbol="AAPL", code=AT_TARGET_HOLD_BREAK_CONFIRMED, reached=True,
        should_sell=False, reason="still beyond", target_price=100.0,
        close_price=105.0,
    )
    p = _pipeline(buy=BUY, bars=_history(105.0),
                  last_decision={"code": AT_TARGET_HOLD_BREAK_CONFIRMED,
                                 "target_price": 100.0})
    p._voice_at_target_decision(sym="AAPL", run_id="r1", decision=hold)
    assert _voiced_code(p) == AT_TARGET_HOLD_BREAK_CONFIRMED
    sent.assert_not_called()

    # Same code, but the target has since been extended: that is news.
    p = _pipeline(buy=BUY, bars=_history(105.0),
                  last_decision={"code": AT_TARGET_HOLD_BREAK_CONFIRMED,
                                 "target_price": 95.0})
    p._voice_at_target_decision(sym="AAPL", run_id="r2", decision=hold)
    assert sent.call_count == 1

    sell = AtTargetDecision(
        symbol="AAPL", code=AT_TARGET_SELL_STALLED, reached=True,
        should_sell=True, reason="banking", target_price=100.0,
        close_price=100.2,
    )
    p = _pipeline(buy=BUY, bars=_history(100.2),
                  last_decision={"code": AT_TARGET_SELL_STALLED,
                                 "target_price": 100.0})
    p._voice_at_target_decision(sym="AAPL", run_id="r3", decision=sell)
    assert sent.call_count == 2


def test_the_rederivation_files_the_break_flag_against_a_measured_move_target():
    """A measured-move target sits on no structural level, so until now no
    break flag was ever filed for it and the at-target rule would have had
    no confirmation to read. The worker now files against the target itself
    when no level backs it."""
    p = _pipeline(buy={**BUY, "expected_horizon_sessions": 10,
                       "setup_type": "breakout"},
                  bars=_history(104.0, prior_close=104.0))
    p.broker = MagicMock()
    p.broker.trading_sessions_held.return_value = 5
    p.risk_engine = SimpleNamespace(config=SimpleNamespace())
    p.db.update_open_take_profit.return_value = True
    out = p._assess_one_target_revision(
        sym="AAPL", position=_long(price=104.0), seat="s", evidence="e",
        run_id="r", target_cfg={}, require_trigger=False,
    )
    assert isinstance(out, dict)
    # A flat 100 history holds no level at 100.0, so the break reference is
    # the target price itself, and 104 clears it by more than one ATR (~2).
    p.db.save_target_level_break.assert_called_once()
    assert p.db.save_target_level_break.call_args.kwargs["raw_broken"] is True
    reads = p._review_target_reads["AAPL"]
    assert reads["break_ref"] == 100.0 and reads["broken_today"] is True
    assert reads["close_price"] == 104.0 and reads["bar_date"] == "2026-03-01"


def test_no_fixed_gain_and_no_swing_read_in_the_at_target_path():
    """The rule is the level-break standard, not a swing read and not a
    percentage gain: nothing in the decision path names either."""
    import inspect

    import src.risk.exit_guard as eg

    src = inspect.getsource(eg.decide_at_target)
    for banned in ("pct", "percent", "higher_high", "pivot", "swing"):
        assert banned not in src, banned
    assert not hasattr(eg, "making_higher_highs_and_lows")
    with pytest.raises(ImportError):
        from src.data.levels import making_higher_highs_and_lows  # noqa: F401
