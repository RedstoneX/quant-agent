"""2026-07-16 audit CRITICAL — belt: naked longs get their stop re-placed.

The BUY-attached OTO stop inherited the parent's DAY tif and was expired by
the broker at 16:00 ET, so positions bought in the morning sat unprotected
overnight. The primary fix places a GTC stop post-fill; this reconciler is the
belt that (a) repairs anything the old bug left naked and (b) covers a crash
between an entry fill and the stop placement.

Repair uses the stop level RECORDED ON THE LAST BUY — the reviewed intent, not
an invented one — and refuses to place a stop at/above the live price (that
would fire instantly and turn a janitor into an exit decision).
"""
from unittest.mock import MagicMock

from src.pipeline import TradingPipeline


def _pipeline(held_qty=31.0, covered=0.0, buy_stop=158.75, price=165.0):
    p = TradingPipeline.__new__(TradingPipeline)
    p.broker = MagicMock()
    p.broker.get_positions.return_value = [
        MagicMock(symbol="VST", qty=held_qty),
    ]
    p.broker.snapshot_protective_stops.return_value = (
        True, ([{"qty": covered, "stop_price": 158.0}] if covered else []),
    )
    p.broker.get_latest_price.return_value = price
    p.broker.STOP_LIMIT_BUFFER_PCT = 0.03
    p.db = MagicMock()
    p.db.get_pending_protection_restores.return_value = []
    p.db.get_symbol_last_buy.return_value = {"stop_loss": buy_stop}
    p.cash_sweeper = None
    return p


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
