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
import functools
from unittest.mock import MagicMock

from src.execution.broker import AlpacaBroker
from src.pipeline import TradingPipeline


def _order(order_id, stop_price, status="new", qty=2.43):
    # `qty` MATTERS: the idempotency check requires an open stop to cover
    # the WHOLE residual before it counts as this position's protection (a
    # leftover sliver stop is not coverage). The residual in these tests is
    # 2.43 shares, so a stop that is genuinely protection carries that qty.
    o = MagicMock()
    o.id = order_id
    o.stop_price = stop_price
    o.status = status
    o.qty = qty
    return o


def _pipeline(existing):
    p = TradingPipeline.__new__(TradingPipeline)
    p.broker = MagicMock()
    p.broker._list_open_sell_stop_orders.return_value = existing
    p.broker._list_open_protective_stop_orders.return_value = existing
    p.broker._submit_stop_limit_order.return_value = {
        "id": "new-stop-1", "status": "accepted",
    }
    # The reprotect path submits through the desk's ONE protective-stop
    # submit, not the raw order call, so the test drives the real thing
    # (leg split, DAY-sliver-first ordering, retry burst) over a mocked
    # `_submit_stop_limit_order`. Binding the real functions rather than
    # stubbing them is what makes these tests able to see defect 4.
    p.broker._submit_stop_leg_retrying = functools.partial(
        AlpacaBroker._submit_stop_leg_retrying, p.broker,
    )
    p.broker._submit_protective_stop_retrying = functools.partial(
        AlpacaBroker._submit_protective_stop_retrying, p.broker,
    )
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
    """Ambiguity places a stop: identity could not be established."""
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


# ---------------------------------------------------------------------------
# The four defects the adversary pass found on this branch, 2026-09-30.
# ---------------------------------------------------------------------------


def test_freshly_placed_pending_new_stop_counts_as_protection():
    """DEFECT 1. A stop a PRIOR attempt submitted seconds ago can still
    report `pending_new`. That is a healthy just-submitted order, not a
    dying one, and this path reads freshly placed stops — counting it as
    'not protection' makes the replay submit a SECOND live stop, which
    nothing in this codebase reconciles."""
    specs = [{"id": "ord-A", "qty": 2.43, "stop_price": 323.74}]
    p = _pipeline([_order("ord-PREVIOUS", 323.74, status="pending_new")])

    assert p._reprotect_residual_after_partial_sell("AAPL", 2.43, specs) is True
    assert not p.broker._submit_stop_limit_order.called, (
        "a second stop was submitted over shares a pending_new stop from a "
        "previous attempt already covers — that is the duplicate"
    )


def test_pending_cancel_is_still_not_protection_in_the_wider_set():
    """DEFECT 1, the other direction: widening the set must not admit a
    DYING order. `pending_cancel` is excluded from both status sets."""
    from src.execution.broker import (
        PROTECTIVE_ORDER_ACTIVE_STATUSES,
        PROTECTIVE_ORDER_ALIVE_STATUSES,
    )

    assert "pending_cancel" not in PROTECTIVE_ORDER_ALIVE_STATUSES
    assert "pending_cancel" not in PROTECTIVE_ORDER_ACTIVE_STATUSES
    # One definition, extended by name — not a second copy of the literals.
    assert PROTECTIVE_ORDER_ACTIVE_STATUSES < PROTECTIVE_ORDER_ALIVE_STATUSES
    assert "pending_new" in PROTECTIVE_ORDER_ALIVE_STATUSES


def test_stringified_none_id_is_not_a_real_id():
    """DEFECT 2. `_snapshot_stop_order` stamps `str(order.id)`, so a
    missing id arrives as the TRUTHY string "None"; the old truthiness
    filter counted it as present, `ids_complete` stayed True, and it then
    matched no open order — so every replay submitted."""
    from src.execution.broker import real_broker_order_id

    assert real_broker_order_id(None) == ""
    assert real_broker_order_id("None") == ""
    assert real_broker_order_id("") == ""
    assert real_broker_order_id("ord-A") == "ord-A"

    specs = [{"id": str(None), "qty": 2.43, "stop_price": 323.74}]
    p = _pipeline([_order("ord-PREVIOUS", 323.74, status="new")])
    # The id is not real, so no open stop may satisfy the check: the run
    # cannot prove the open stop is not the one it just cancelled.
    assert p._reprotect_residual_after_partial_sell("AAPL", 2.43, specs) is True
    assert p.broker._submit_stop_limit_order.called


def test_partial_cover_reports_the_quantity_actually_covered():
    """DEFECT 3. The alert hardcoded `covered_qty` to 0, so a landed
    whole-share leg was reported to the owner as ZERO coverage — an untrue
    statement about how exposed the position is."""
    specs = [{"id": "ord-A", "qty": 2.43, "stop_price": 323.74}]
    p = _pipeline([])
    p._alert_owner_no_stop = MagicMock()

    calls = []

    def _submit(*, symbol, qty, stop_price, limit_price=None, side="sell"):
        calls.append(qty)
        if qty < 1:  # the DAY sub-share sliver is refused
            raise RuntimeError("sliver refused")
        return {"id": "gtc-1", "status": "accepted"}

    p.broker._submit_stop_limit_order = _submit

    assert p._reprotect_residual_after_partial_sell("AAPL", 2.43, specs) is False
    assert p._alert_owner_no_stop.called
    (naked,), _ = p._alert_owner_no_stop.call_args
    assert float(naked[0]["covered_qty"]) == 2.0, (
        "the whole-share leg landed; reporting 0 covered is a lie"
    )
    assert float(naked[0]["held_qty"]) == 2.43


def test_fractional_sliver_is_submitted_before_the_whole_share_gtc():
    """DEFECT 4. Measured 2026-09-16: placing the whole-share GTC leg
    first made the GTC hold reserve the position and Alpaca refused the
    sub-share DAY sliver with held_for_orders. The shared submit places
    the remainder FIRST; the old hand-rolled loop here did the opposite."""
    specs = [{"id": "ord-A", "qty": 2.43, "stop_price": 323.74}]
    p = _pipeline([])
    order = []

    def _submit(*, symbol, qty, stop_price, limit_price=None, side="sell"):
        order.append(qty)
        return {"id": f"leg-{len(order)}", "status": "accepted"}

    p.broker._submit_stop_limit_order = _submit

    assert p._reprotect_residual_after_partial_sell("AAPL", 2.43, specs) is True
    assert len(order) == 2
    assert order[0] < 1 < order[1], (
        f"sliver must be placed before the whole-share GTC leg, got {order}"
    )


def test_a_transient_refusal_is_retried_not_reported_naked():
    """DEFECT 4, second half: the raw call had no retry burst, so one
    transient refusal ended as a naked residual."""
    specs = [{"id": "ord-A", "qty": 3.0, "stop_price": 323.74}]
    p = _pipeline([])
    p._alert_owner_no_stop = MagicMock()
    attempts = []

    def _submit(*, symbol, qty, stop_price, limit_price=None, side="sell"):
        attempts.append(qty)
        if len(attempts) == 1:
            raise RuntimeError("transient broker blip")
        return {"id": "gtc-1", "status": "accepted"}

    p.broker._submit_stop_limit_order = _submit

    assert p._reprotect_residual_after_partial_sell("AAPL", 3.0, specs) is True
    assert len(attempts) == 2
    assert not p._alert_owner_no_stop.called
