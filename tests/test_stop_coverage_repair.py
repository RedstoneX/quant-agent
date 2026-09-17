"""Naked-position stop repair — longs AND shorts.

The BUY-attached OTO stop inherited the parent's DAY tif and was expired by
the broker at 16:00 ET, so positions bought in the morning sat unprotected
overnight. The primary fix places a GTC stop post-fill; this reconciler is the
belt that (a) repairs anything the old bug left naked and (b) covers a crash
between an entry fill and the stop placement.

Repair uses the stop level RECORDED ON THE LAST OPENING ROW — BUY for a long,
SHORT for a short — the reviewed intent, not an invented one. A long's
sell-stop at/above the live price, or a short's buy-stop at/below it, would
fire instantly and turn a janitor into an exit decision, so those are refused.

A test that only checks "the gap is flagged" is not enough: a BUY-only lookup
or a sell-only place path can still leave a short naked while the long tests
stay green. The short cases below require the SHORT row's stop and side="buy".
"""
from unittest.mock import MagicMock, patch

import pytest

from src.pipeline import TradingPipeline
from src.storage.db import Database


def _pipeline(
    held_qty=31.0,
    covered=0.0,
    buy_stop=158.75,
    price=165.0,
    *,
    symbol="VST",
    last_buy=None,
):
    p = TradingPipeline.__new__(TradingPipeline)
    p.broker = MagicMock()
    p.broker.get_positions.return_value = [
        MagicMock(symbol=symbol, qty=held_qty),
    ]
    p.broker.snapshot_protective_stops.return_value = (
        True, ([{"qty": covered, "stop_price": 158.0}] if covered else []),
    )
    p.broker.get_latest_price.return_value = price
    p.broker.STOP_LIMIT_BUFFER_PCT = 0.03
    p.db = MagicMock()
    p.db.get_pending_protection_restores.return_value = []
    if last_buy is not None:
        p.db.get_symbol_last_buy.side_effect = last_buy
    else:
        p.db.get_symbol_last_buy.return_value = {"stop_loss": buy_stop}
    p.cash_sweeper = None
    return p


def _opening_lookup(**stops_by_action):
    """Return a last-open callable that only answers the actions it was given.

    A BUY-only half-fix fails the short tests because `action='SHORT'` returns
    None. A SHORT-only table cannot accidentally feed the long path.
    """
    def _lookup(symbol, include_in_flight=False, action="BUY"):
        stop = stops_by_action.get(action)
        if stop is None:
            return None
        return {"stop_loss": stop, "action": action, "symbol": symbol}
    return _lookup


def test_naked_long_is_repaired_from_the_recorded_buy_stop():
    """Spec §11.1 guard 1 extends to the repair belt: it now places the stop
    through the same retrying+fallback machinery the entry path uses
    (`_submit_protective_stop_retrying`), not a bare single-shot submit."""
    p = _pipeline()
    p.broker._submit_protective_stop_retrying.return_value = {"id": "stop-1"}
    gaps = p._reconcile_stop_coverage()
    assert len(gaps) == 1 and gaps[0]["repaired"] is True
    kwargs = p.broker._submit_protective_stop_retrying.call_args.kwargs
    assert kwargs["symbol"] == "VST"
    assert kwargs["qty"] == 31.0            # the whole uncovered position
    assert kwargs["stop_price"] == 158.75   # the level PM/RM actually approved
    assert abs(kwargs["limit_price"] - 158.75 * 0.97) < 0.01
    assert kwargs["side"] == "sell"


def test_partial_coverage_repairs_only_the_uncovered_shares():
    p = _pipeline(held_qty=31.0, covered=20.0)
    p.broker._submit_protective_stop_retrying.return_value = {"id": "stop-1"}
    gaps = p._reconcile_stop_coverage()
    assert gaps[0]["repaired"] is True
    assert p.broker._submit_protective_stop_retrying.call_args.kwargs["qty"] == 11.0


def test_repair_of_a_fractional_gap_that_only_partially_covers_keeps_escalating():
    """The retrying call fell back to a whole-share floor stop (the §11.1
    open question about a broker that won't carry a fractional-qty stop,
    resolved unfavourably) — real progress, but `repaired` must stay False
    so this pass keeps escalating rather than going quiet on a real gap."""
    p = _pipeline(held_qty=12.3456, covered=0.0)
    p.broker._submit_protective_stop_retrying.return_value = {
        "id": "stop-1", "covered_qty": 12.0, "uncovered_qty": 0.3456,
    }
    gaps = p._reconcile_stop_coverage()
    assert gaps[0]["repaired"] is False


def test_repair_refuses_a_stop_at_or_above_the_live_price():
    """Recorded stop $158.75 but the stock is now $150 — placing it would fire
    instantly. That's an exit decision; flag, don't act."""
    p = _pipeline(price=150.0)
    gaps = p._reconcile_stop_coverage()
    assert gaps[0]["repaired"] is False
    p.broker._submit_protective_stop_retrying.assert_not_called()


def test_repair_skipped_when_the_buy_row_has_no_stop():
    p = _pipeline(buy_stop=0.0)
    gaps = p._reconcile_stop_coverage()
    assert gaps[0]["repaired"] is False
    p.broker._submit_protective_stop_retrying.assert_not_called()


def test_repair_failure_still_reports_the_gap():
    """`_submit_protective_stop_retrying` never raises — it exhausts its own
    retries and reports failure as a `None` return, not an exception. This
    was `side_effect = RuntimeError` against the old bare single-shot call;
    the new boundary reports the same real-world failure as `None`."""
    p = _pipeline()
    p.broker._submit_protective_stop_retrying.return_value = None
    gaps = p._reconcile_stop_coverage()
    assert len(gaps) == 1 and gaps[0]["repaired"] is False   # no raise


def test_covered_long_needs_no_repair():
    p = _pipeline(covered=31.0)
    assert p._reconcile_stop_coverage() == []
    p.broker._submit_protective_stop_retrying.assert_not_called()


def test_fractional_remainder_on_a_short_is_repaired_with_a_buy_stop():
    """The session sweep has two repair call sites: whole-gap and the
    fractional remainder. Naked-short tests only hit the first. Dropping
    ``is_short`` on the fractional caller would read the BUY row, place a
    SELL stop against a short, and stamp repaired=True (silent). This is
    that branch: GTC buy-stop intact, sub-share remainder missing, market
    open."""
    p = _pipeline(
        held_qty=-40.4, covered=40.0, price=200.0, symbol="TSLA",
        last_buy=_opening_lookup(BUY=180.0, SHORT=220.0),
    )
    p.broker._submit_protective_stop_retrying.return_value = {"id": "buy-stop-frac"}
    with patch("src.pipeline._market_is_open_now", return_value=True):
        gaps = p._reconcile_stop_coverage()
    assert len(gaps) == 1 and gaps[0]["repaired"] is True
    assert gaps[0]["coverage"] == "fractional_replaced"
    kwargs = p.broker._submit_protective_stop_retrying.call_args.kwargs
    assert kwargs["side"] == "buy"
    assert kwargs["stop_price"] == 220.0
    assert abs(kwargs["qty"] - 0.4) < 1e-9
    assert p.db.get_symbol_last_buy.call_args.kwargs.get("action") == "SHORT"


def test_naked_short_is_repaired_from_the_recorded_short_stop():
    """The load-bearing short case: uncovered short → BUY stop at the SHORT
    row's recorded level. A BUY-only lookup returns None here, so a half-fix
    that only un-skips shorts cannot pass."""
    p = _pipeline(
        held_qty=-40.0, covered=0.0, price=200.0, symbol="TSLA",
        last_buy=_opening_lookup(SHORT=220.0),
    )
    p.broker._submit_protective_stop_retrying.return_value = {"id": "buy-stop-1"}
    gaps = p._reconcile_stop_coverage()
    assert len(gaps) == 1 and gaps[0]["repaired"] is True
    kwargs = p.broker._submit_protective_stop_retrying.call_args.kwargs
    assert kwargs["symbol"] == "TSLA"
    assert kwargs["qty"] == 40.0
    assert kwargs["stop_price"] == 220.0
    assert abs(kwargs["limit_price"] - 220.0 * 1.03) < 0.01
    assert kwargs["side"] == "buy"
    p.broker.snapshot_protective_stops.assert_called_with("TSLA", side="buy")
    p.db.get_symbol_last_buy.assert_called()
    assert p.db.get_symbol_last_buy.call_args.kwargs.get("action") == "SHORT"


def test_short_repair_does_not_use_a_stale_buy_stop():
    """A prior long on the same ticker must not donate its sell-stop to a
    later short. The BUY row's $80 would sit *below* $200 and, placed as a
    buy-stop, would fire immediately — or as a sell-stop would be the wrong
    side. Repair must read the SHORT row."""
    p = _pipeline(
        held_qty=-10.0, covered=0.0, price=200.0, symbol="TSLA",
        last_buy=_opening_lookup(BUY=80.0, SHORT=220.0),
    )
    p.broker._submit_protective_stop_retrying.return_value = {"id": "buy-stop-1"}
    gaps = p._reconcile_stop_coverage()
    assert gaps[0]["repaired"] is True
    kwargs = p.broker._submit_protective_stop_retrying.call_args.kwargs
    assert kwargs["side"] == "buy"
    assert kwargs["stop_price"] == 220.0
    assert kwargs["stop_price"] != 80.0


def test_buy_only_lookup_cannot_repair_a_short():
    """Half-fix tripwire: un-skipping shorts while still reading action=BUY
    finds no row and must leave the gap flagged, not invent a level."""
    p = _pipeline(
        held_qty=-40.0, covered=0.0, price=200.0, symbol="TSLA",
        last_buy=_opening_lookup(BUY=180.0),
    )
    gaps = p._reconcile_stop_coverage()
    assert len(gaps) == 1 and gaps[0]["repaired"] is False
    p.broker._submit_protective_stop_retrying.assert_not_called()


def test_short_repair_refuses_a_stop_at_or_below_the_live_price():
    """Recorded buy-stop $180 but the stock is now $200 — placing it would
    cover the short instantly. That's an exit decision; flag, don't act."""
    p = _pipeline(
        held_qty=-40.0, covered=0.0, price=200.0, symbol="TSLA",
        last_buy=_opening_lookup(SHORT=180.0),
    )
    gaps = p._reconcile_stop_coverage()
    assert gaps[0]["repaired"] is False
    p.broker._submit_protective_stop_retrying.assert_not_called()


def test_short_repair_skipped_when_the_short_row_has_no_stop():
    p = _pipeline(
        held_qty=-40.0, covered=0.0, price=200.0, symbol="TSLA",
        last_buy=_opening_lookup(SHORT=0.0),
    )
    gaps = p._reconcile_stop_coverage()
    assert gaps[0]["repaired"] is False
    p.broker._submit_protective_stop_retrying.assert_not_called()


def test_long_path_ignores_a_short_row_on_the_same_symbol():
    """Mirror of the stale-BUY case: a currently-held long must not pick up
    a later SHORT row's stop above the tape."""
    p = _pipeline(
        held_qty=10.0, covered=0.0, price=150.0, symbol="NVDA",
        last_buy=_opening_lookup(BUY=140.0, SHORT=170.0),
    )
    p.broker._submit_protective_stop_retrying.return_value = {"id": "sell-stop-1"}
    gaps = p._reconcile_stop_coverage()
    assert gaps[0]["repaired"] is True
    kwargs = p.broker._submit_protective_stop_retrying.call_args.kwargs
    assert kwargs["side"] == "sell"
    assert kwargs["stop_price"] == 140.0


def test_get_symbol_last_buy_default_does_not_see_a_short_row(tmp_path):
    """PM-memory callers keep the BUY-only contract. A SHORT on the same
    ticker is a different position and must not overwrite last-buy."""
    db = Database(str(tmp_path / "t.db"))
    db.initialize()
    db.insert_trade(
        symbol="TSLA", action="BUY", qty=5, price=180.0,
        reasoning="old long", run_id="r0", stop_loss=160.0,
        fill_status="filled",
    )
    db.insert_trade(
        symbol="TSLA", action="SHORT", qty=8, price=200.0,
        reasoning="new short", run_id="r1", stop_loss=220.0,
        fill_status="filled",
    )
    last_buy = db.get_symbol_last_buy("TSLA")
    last_short = db.get_symbol_last_buy("TSLA", action="SHORT")
    assert last_buy is not None and last_buy["stop_loss"] == 160.0
    assert last_buy["action"] == "BUY"
    assert last_short is not None and last_short["stop_loss"] == 220.0
    assert last_short["action"] == "SHORT"
    assert db.get_symbol_last_buy("TSLA", action="COVER") is None


def test_get_symbol_last_buy_short_include_in_flight(tmp_path):
    """Same-session in-flight SHORT is the row repair wants, mirroring the
    BUY audit-round-2 belt."""
    db = Database(str(tmp_path / "t.db"))
    db.initialize()
    db.insert_trade(
        symbol="TSLA", action="SHORT", qty=8, price=190.0,
        reasoning="old short", run_id="r0", stop_loss=210.0,
        fill_status="filled",
    )
    db.insert_trade(
        symbol="TSLA", action="SHORT", qty=8, price=200.0,
        reasoning="today", run_id="r1", stop_loss=222.0,
        broker_order_id="s9", fill_status="submitted",
    )
    strict = db.get_symbol_last_buy("TSLA", action="SHORT")
    in_flight = db.get_symbol_last_buy(
        "TSLA", include_in_flight=True, action="SHORT",
    )
    assert strict["stop_loss"] == 210.0
    assert in_flight["stop_loss"] == 222.0


def test_naked_short_repair_through_a_real_short_row(tmp_path):
    """End-to-end: the pipeline's last_buy lambda actually queries action=
    SHORT on a real Database. Mocking get_symbol_last_buy.return_value would
    let a BUY-only SQL still look repaired."""
    db = Database(str(tmp_path / "t.db"))
    db.initialize()
    db.insert_trade(
        symbol="TSLA", action="SHORT", qty=40, price=200.0,
        reasoning="opened short", run_id="r1", stop_loss=220.0,
        fill_status="filled",
    )
    p = TradingPipeline.__new__(TradingPipeline)
    p.broker = MagicMock()
    p.broker.get_positions.return_value = [MagicMock(symbol="TSLA", qty=-40.0)]
    p.broker.snapshot_protective_stops.return_value = (True, [])
    p.broker.get_latest_price.return_value = 200.0
    p.broker.STOP_LIMIT_BUFFER_PCT = 0.03
    p.broker._submit_protective_stop_retrying.return_value = {"id": "buy-stop-1"}
    p.db = db
    p.cash_sweeper = None
    gaps = p._reconcile_stop_coverage()
    assert len(gaps) == 1 and gaps[0]["repaired"] is True
    kwargs = p.broker._submit_protective_stop_retrying.call_args.kwargs
    assert kwargs["side"] == "buy"
    assert kwargs["stop_price"] == 220.0
    assert kwargs["qty"] == 40.0


def test_one_arg_last_buy_cannot_repair_a_short_from_a_stale_long_stop():
    """A last_buy that does not accept action= must fail closed, not fall
    back to a long's stop and place it as a buy-stop on a short. A $270
    long stop against a $200 short would pass the short-side price guard
    (it sits above the tape) and stamp the gap repaired."""
    from src.execution.stop_repair import repair_stop_coverage

    broker = MagicMock()
    broker.get_latest_price.return_value = 200.0
    broker.STOP_LIMIT_BUFFER_PCT = 0.03

    def _one_arg(_symbol):
        return {"stop_loss": 270.0}

    out = repair_stop_coverage(
        broker=broker, last_buy=_one_arg, symbol="TSLA",
        uncovered_qty=40.0, is_short=True,
    )
    assert out is False
    broker._submit_protective_stop_retrying.assert_not_called()


def test_repair_stop_coverage_refuses_to_guess_direction():
    """Direction is not a default. Omitting is_short must fail closed
    rather than silently take the long path."""
    from src.execution.stop_repair import repair_stop_coverage

    broker = MagicMock()
    with pytest.raises(TypeError):
        repair_stop_coverage(
            broker=broker,
            last_buy=lambda s, action="BUY": {"stop_loss": 140.0},
            symbol="NVDA",
            uncovered_qty=10.0,
        )
    broker._submit_protective_stop_retrying.assert_not_called()


# ---------------------------------------------------------------------------
# Price provenance (2026-09-17)
#
# The wrong-side test above decides whether putting a stop back would fire it
# instantly — i.e. sell the position at market. It is only as good as the
# price it runs against, and the price reader used to throw away both the
# trade time and whether a trade happened at all: yesterday's last print on a
# thin name, and an unconfirmed quote midpoint, arrived looking exactly like
# a live price. Against a stale number the wrong-side test can read "safe"
# while today's real price is already through the stop.
# ---------------------------------------------------------------------------

def _stamped(price, *, source="last_trade", is_today=True, is_today_print=True):
    from src.execution.broker import LivePrice

    return LivePrice(
        price=price, source=source, trade_at=None,
        is_today=is_today, is_today_print=is_today_print,
    )


def test_repair_uses_a_today_trade_print_when_one_is_available():
    p = _pipeline()
    p.broker.get_latest_price_stamped.return_value = _stamped(165.0)
    p.broker._submit_protective_stop_retrying.return_value = {"id": "stop-1"}
    gaps = p._reconcile_stop_coverage()
    assert len(gaps) == 1 and gaps[0]["repaired"] is True
    assert p.broker._submit_protective_stop_retrying.call_args.kwargs["stop_price"] == 158.75


def test_repair_refuses_a_price_that_is_not_from_today():
    """A stop placed off a prior session's print could fire immediately. The
    gap stays flagged for the next sweep — the same outcome this belt already
    produces for every other unverifiable input."""
    p = _pipeline()
    p.broker.get_latest_price_stamped.return_value = _stamped(
        165.0, is_today=False, is_today_print=False,
    )
    gaps = p._reconcile_stop_coverage()
    assert len(gaps) == 1 and gaps[0]["repaired"] is not True
    p.broker._submit_protective_stop_retrying.assert_not_called()


def test_repair_refuses_a_quote_midpoint_because_the_tape_never_traded_there():
    p = _pipeline()
    p.broker.get_latest_price_stamped.return_value = _stamped(
        165.0, source="quote_mid", is_today=True, is_today_print=False,
    )
    gaps = p._reconcile_stop_coverage()
    assert len(gaps) == 1 and gaps[0]["repaired"] is not True
    p.broker._submit_protective_stop_retrying.assert_not_called()
