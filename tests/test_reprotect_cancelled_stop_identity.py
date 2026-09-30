"""INCIDENT 2026-09-30 (AAPL) — the reprotect idempotency check must not be
satisfied by the stop THIS run just cancelled.

Production log, 14:47:23: the WAL intent was written, 2 protective stops
were cancelled, the SELL went out, and 486ms later the idempotency check
listed those same just-cancelled orders as still open (Alpaca's cancel is
asynchronous and its OPEN filter includes `pending_cancel`), matched
`best_stop`, logged "idempotent re-run", returned True — so the drain
caller deleted the recovery row and ~$2,500 sat with no stop and no record
that one was owed.

The distinction is by broker order IDENTITY, not by timing and not by a
tolerance. These tests pin both directions: the just-cancelled order must
NOT satisfy the check (a stop is placed), and a genuinely live stop from a
PREVIOUS attempt must still satisfy it (the duplicate-stop protection the
2026-05-27 audit note describes is not weakened).
"""
from unittest.mock import MagicMock

from src.pipeline import TradingPipeline


def _order(order_id, stop_price, status="new"):
    o = MagicMock()
    o.id = order_id
    o.stop_price = stop_price
    o.status = status
    return o


def _pipeline(existing):
    p = TradingPipeline.__new__(TradingPipeline)
    p.broker = MagicMock()
    p.broker._list_open_sell_stop_orders.return_value = existing
    p.broker._list_open_protective_stop_orders.return_value = existing
    p.broker._submit_stop_limit_order.return_value = {
        "id": "new-stop-1", "status": "accepted",
    }
    p.db = None
    return p


def test_just_cancelled_stop_does_not_satisfy_idempotency():
    """THE INCIDENT. The open stop the broker still lists is the very order
    whose id is in `cancelled_specs` — a stop MUST be submitted."""
    specs = [{"id": "ord-A", "qty": 2.43, "stop_price": 323.74}]
    p = _pipeline([_order("ord-A", 323.74, status="pending_cancel")])

    assert p._reprotect_residual_after_partial_sell("AAPL", 2.43, specs) is True
    assert p.broker._submit_stop_limit_order.called, (
        "reprotect skipped on the stop it had just cancelled — this is the "
        "2026-09-30 naked-position incident"
    )


def test_live_stop_from_a_previous_attempt_still_skips():
    """The duplicate-stop protection the check exists for is UNCHANGED: a
    live stop at the same price that this run did NOT cancel still wins."""
    specs = [{"id": "ord-A", "qty": 2.43, "stop_price": 323.74}]
    p = _pipeline([_order("ord-PREVIOUS", 323.74, status="new")])

    assert p._reprotect_residual_after_partial_sell("AAPL", 2.43, specs) is True
    assert not p.broker._submit_stop_limit_order.called


def test_pending_cancel_order_is_never_protection():
    """Belt and braces: a foreign order that is dying is not coverage."""
    specs = [{"id": "ord-A", "qty": 2.43, "stop_price": 323.74}]
    p = _pipeline([_order("ord-OTHER", 323.74, status="pending_cancel")])

    assert p._reprotect_residual_after_partial_sell("AAPL", 2.43, specs) is True
    assert p.broker._submit_stop_limit_order.called


def test_specs_without_ids_fail_toward_submitting():
    """Ambiguity places a stop: a duplicate is recoverable, naked is not."""
    specs = [{"qty": 2.43, "stop_price": 323.74}]
    p = _pipeline([_order("ord-UNKNOWN", 323.74, status="new")])

    assert p._reprotect_residual_after_partial_sell("AAPL", 2.43, specs) is True
    assert p.broker._submit_stop_limit_order.called


def test_submit_failure_pages_the_owner():
    """The silence was its own defect: a refusal that leaves the residual
    unprotected goes down the existing NO-STOP escalation."""
    specs = [{"id": "ord-A", "qty": 2.43, "stop_price": 323.74}]
    p = _pipeline([])
    p.broker._submit_stop_limit_order.side_effect = RuntimeError("boom")
    p._alert_owner_no_stop = MagicMock()

    assert p._reprotect_residual_after_partial_sell("AAPL", 2.43, specs) is False
    assert p._alert_owner_no_stop.called
    (naked,), _ = p._alert_owner_no_stop.call_args
    assert naked[0]["symbol"] == "AAPL"
    assert "re-protect" in naked[0]["repair_refusal"]


def test_unusable_spec_price_pages_the_owner():
    specs = [{"id": "ord-A", "qty": 2.43, "stop_price": 0}]
    p = _pipeline([])
    p._alert_owner_no_stop = MagicMock()

    assert p._reprotect_residual_after_partial_sell("AAPL", 2.43, specs) is False
    assert p._alert_owner_no_stop.called
