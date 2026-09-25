"""Decision at the take-profit target — owner ruling 2026-09-25.

Reaching the target is a REASSESS point, never an automatic "sell at X". The
desk sells ONLY when the target is reached AND the chart's own trend structure
has broken and confirmed; while the trend is intact it holds and lets the
ratcheting trailing stop carry the position. These pin all four required
behaviours end to end:

  (a) the target is RE-DERIVED from current bars every review, not frozen
      (see also tests/test_target_revision.py::the every-review section);
  (b) at-target with an INTACT trend does NOT sell (rides the raised stop);
  (c) at-target with a CONFIRMED breakdown DOES sell;
  (d) there is no fixed "sell at X" path — a reached target with no structural
      breakdown never sells on the number alone.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

from src.pipeline import TradingPipeline
from src.risk.exit_guard import (
    AT_TARGET_HOLD_TREND_INTACT,
    AT_TARGET_INPUTS_UNREADABLE,
    AT_TARGET_NOT_REACHED,
    AT_TARGET_SELL_TREND_ROLLED,
    decide_at_target,
)


def _bar(date, close):
    return SimpleNamespace(date=date, close=close)


# ---------------------------------------------------------------------------
# Pure decision function
# ---------------------------------------------------------------------------


def test_below_target_is_not_reached_and_never_sells():
    d = decide_at_target(
        symbol="AAPL", is_short=False, close_price=104.0, target_price=110.0,
        protection_broken=True,  # even a broken chart cannot sell below target
    )
    assert d.reached is False
    assert d.should_sell is False
    assert d.code == AT_TARGET_NOT_REACHED


def test_at_target_with_intact_trend_holds_not_sells():
    """(b) + (d): the number alone never sells. Trend intact -> HOLD."""
    d = decide_at_target(
        symbol="AAPL", is_short=False, close_price=112.0, target_price=110.0,
        protection_broken=False,
    )
    assert d.reached is True
    assert d.should_sell is False
    assert d.code == AT_TARGET_HOLD_TREND_INTACT
    assert "not selling on the number alone" in d.reason.lower()


def test_at_target_with_confirmed_breakdown_sells():
    """(c): target reached AND trend structure broken+confirmed -> SELL."""
    d = decide_at_target(
        symbol="AAPL", is_short=False, close_price=112.0, target_price=110.0,
        protection_broken=True, protection_detail="support closed through",
    )
    assert d.reached is True
    assert d.should_sell is True
    assert d.code == AT_TARGET_SELL_TREND_ROLLED
    assert "taking profit" in d.reason.lower()


def test_at_target_with_unreadable_structure_holds():
    """A None (chart unreadable) is treated as NOT rolled over — never sell
    into missing data on the number alone."""
    d = decide_at_target(
        symbol="AAPL", is_short=False, close_price=112.0, target_price=110.0,
        protection_broken=None,
    )
    assert d.should_sell is False
    assert d.code == AT_TARGET_HOLD_TREND_INTACT


def test_short_mirror_reaches_below_target():
    # A short's target sits BELOW; reached when the close falls to/through it.
    intact = decide_at_target(
        symbol="AAPL", is_short=True, close_price=90.0, target_price=92.0,
        protection_broken=False,
    )
    assert intact.reached is True and intact.should_sell is False
    rolled = decide_at_target(
        symbol="AAPL", is_short=True, close_price=90.0, target_price=92.0,
        protection_broken=True,
    )
    assert rolled.should_sell is True
    # Still above the short's target -> not reached.
    not_yet = decide_at_target(
        symbol="AAPL", is_short=True, close_price=95.0, target_price=92.0,
        protection_broken=True,
    )
    assert not_yet.reached is False and not_yet.should_sell is False


def test_unreadable_inputs_never_sell():
    d = decide_at_target(
        symbol="AAPL", is_short=False, close_price=None, target_price=110.0,
        protection_broken=True,
    )
    assert d.code == AT_TARGET_INPUTS_UNREADABLE
    assert d.should_sell is False


# ---------------------------------------------------------------------------
# Pipeline wiring — the deterministic exit
# ---------------------------------------------------------------------------


def _pipeline_with(position, *, buy, close, protected):
    p = TradingPipeline.__new__(TradingPipeline)
    p.db = MagicMock()
    p.db.get_symbol_last_buy.return_value = buy
    p.market = MagicMock()
    p.market.get_ohlcv.return_value = [_bar("2026-09-24", close - 1), _bar("2026-09-25", close)]
    p.config = SimpleNamespace(trading=SimpleNamespace(lookback_days=200))
    # Structural protection is stubbed: the pure decide_at_target logic is
    # tested above; here we only verify the pipeline acts on protected.
    p._structural_protection_for_holding = MagicMock(
        return_value=SimpleNamespace(
            protected=protected, detail="d", basis="b",
        )
    )
    p._voice_at_target_decision = MagicMock()
    p._submit_protected_sell = MagicMock(
        return_value=({"id": "o1", "action": "SELL"}, None)
    )
    p._finalize_pending_protections = MagicMock()
    return p


def _long(qty=100.0, entry=100.0, price=130.0):
    return SimpleNamespace(
        symbol="AAPL", qty=qty, avg_entry=entry, current_price=price,
    )


def test_pipeline_at_target_intact_trend_does_not_sell():
    """(b) end to end: reached, trend intact -> no order, no sell submitted."""
    p = _pipeline_with(
        _long(), buy={"take_profit": 120.0, "stop_loss": 95.0}, close=121.0,
        protected=True,
    )
    order = p._decide_one_at_target(_long(), "AAPL", run_id="r", seat="s")
    assert order is None
    p._submit_protected_sell.assert_not_called()
    p._voice_at_target_decision.assert_called_once()


def test_pipeline_at_target_confirmed_breakdown_sells():
    """(c) end to end: reached AND trend rolled over -> SELL is submitted."""
    p = _pipeline_with(
        _long(), buy={"take_profit": 120.0, "stop_loss": 95.0}, close=121.0,
        protected=False,
    )
    order = p._decide_one_at_target(_long(), "AAPL", run_id="r", seat="s")
    assert order == {"id": "o1", "action": "SELL"}
    p._submit_protected_sell.assert_called_once()
    kwargs = p._submit_protected_sell.call_args.kwargs
    assert kwargs["label"] == "SELL" and kwargs["side"] == "sell"
    assert kwargs["qty"] == 100.0
    p.db.insert_trade.assert_called_once()
    assert p.db.insert_trade.call_args.kwargs["action"] == "SELL"


def test_pipeline_below_target_never_consults_structure_or_sells():
    """(d): below the target there is nothing to decide — no structural read,
    no sell. The trailing stop remains the only exit until the target."""
    p = _pipeline_with(
        _long(), buy={"take_profit": 120.0, "stop_loss": 95.0}, close=110.0,
        protected=False,
    )
    order = p._decide_one_at_target(_long(), "AAPL", run_id="r", seat="s")
    assert order is None
    p._structural_protection_for_holding.assert_not_called()
    p._submit_protected_sell.assert_not_called()


def test_pipeline_no_stored_target_is_a_no_op():
    p = _pipeline_with(
        _long(), buy={"stop_loss": 95.0}, close=121.0, protected=False,
    )
    order = p._decide_one_at_target(_long(), "AAPL", run_id="r", seat="s")
    assert order is None
    p._submit_protected_sell.assert_not_called()


def test_at_target_sweep_skips_already_sold_symbols():
    p = _pipeline_with(
        _long(), buy={"take_profit": 120.0, "stop_loss": 95.0}, close=121.0,
        protected=False,
    )
    orders = p._decide_at_target_exits(
        [_long()], "r", seat="s", sold_symbols={"AAPL"},
    )
    assert orders == []
    p._submit_protected_sell.assert_not_called()
