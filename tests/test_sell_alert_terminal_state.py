"""The rotation-close owner alert is sent at the sale's TERMINAL state.

2026-10-09 09:32 ET the owner was told "POSITION CLOSED AUTOMATICALLY" the
moment the broker accepted the sell; it never filled and was cancelled 15 s
later. These tests pin that the wording comes from the filled quantity, that
a restore is claimed only when finalize confirmed it, and that nothing is
sent at submit.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from src import notifier
from src.rotation_buy_leg_post import _alert_rotation_executed
from src.stage_execution_parts import sell_loop
from src.trader_feed.common import _traded_word


def _rotation(**extra) -> dict:
    base = {"held_symbol": "OLD", "new_symbol": "NEW", "held_reasons": ["R1"], "reason": "r"}
    base.update(extra)
    return base


@pytest.fixture
def sent(monkeypatch):
    out: list = []
    monkeypatch.setattr(notifier, "send_owner_alert", lambda text, symbols=None: out.append(text) or True)
    return out


def _alert(**kw):
    args = {"rotation": _rotation(), "qty": 10.0, "limit_price": 94.53, "order_id": "o-1"}
    args.update(kw)
    _alert_rotation_executed(**args)


def test_fully_filled_reads_closed_with_fill_qty_and_avg(sent):
    _alert(terminal_status="filled", filled_qty=10.0, avg_price=94.61, stops_restored=True)
    (text,) = sent
    assert text.startswith("POSITION CLOSED AUTOMATICALLY")
    assert "SOLD 10 share(s) at an average $94.61" in text


def test_partly_filled_reads_partly_sold(sent):
    _alert(terminal_status="canceled", filled_qty=4.0, avg_price=94.6, stops_restored=True)
    (text,) = sent
    assert text.startswith("PARTLY SOLD 4 of 10 share(s)")
    assert "6 share(s) are still held" in text
    assert "restored and confirmed" in text
    assert "CLOSED" not in text


def test_nothing_filled_reads_not_filled_position_kept(sent):
    _alert(terminal_status="canceled", filled_qty=0.0, stops_restored=True)
    (text,) = sent
    assert text.startswith("SELL NOT FILLED — position kept")
    assert "CLOSED" not in text and "SOLD" not in text


def test_no_terminal_status_reads_unknown(sent):
    _alert(terminal_status=None, filled_qty=0.0, stops_restored=True)
    (text,) = sent
    assert text.startswith("SELL OUTCOME UNKNOWN")
    assert "CLOSED" not in text


def test_no_outcome_at_all_never_reads_closed(sent):
    _alert()
    (text,) = sent
    assert text.startswith("SELL OUTCOME UNKNOWN")


@pytest.mark.parametrize("restored", [False, None])
def test_restore_not_claimed_unless_finalize_confirmed(sent, restored):
    _alert(terminal_status="canceled", filled_qty=0.0, stops_restored=restored)
    (text,) = sent
    assert "NOT confirmed restored" in text
    assert "restored and confirmed" not in text


class _Broker:
    def __init__(self, status, info):
        self.status, self.info = status, info

    def wait_for_order_terminal(self, order_id):
        return self.status

    def get_order_fill_info(self, order_id):
        return self.info


class _Pipeline:
    def __init__(self, status, info, confirmed):
        self.broker = _Broker(status, info)
        self.confirmed = confirmed

    def _finalize_pending_protections(self, prots, *, context, wait):
        for p in prots:
            p["coverage_confirmed"] = self.confirmed


def _submit(monkeypatch, ctx, pipeline):
    events: list = []
    monkeypatch.setattr(sell_loop, "_record_pipeline_event", lambda *a, **k: events.append(a))
    leg = SimpleNamespace(
        decision=SimpleNamespace(symbol="OLD"), qty=10.0, sell_limit=94.53, rotation_final_reason=None
    )
    sell_loop.record_rotation_close(pipeline, ctx, leg, {"id": "o-1"})
    return events


def test_nothing_is_sent_at_submit(sent, monkeypatch):
    ctx = SimpleNamespace(rotation=_rotation())
    events = _submit(monkeypatch, ctx, _Pipeline("canceled", None, True))
    assert events, "the submit is still recorded durably"
    assert sent == []


def test_unfilled_rotation_close_pages_the_true_state_once(sent, monkeypatch):
    ctx = SimpleNamespace(rotation=_rotation())
    pipeline = _Pipeline("canceled", {"status": "canceled", "filled_qty": 0.0, "filled_avg_price": 0.0}, True)
    _submit(monkeypatch, ctx, pipeline)
    statuses: dict = {}
    prot = {"order_id": "o-1"}
    sell_loop.await_sell_and_finalize(pipeline, prot, statuses, ctx)
    sell_loop.await_sell_and_finalize(pipeline, prot, statuses, ctx)
    (text,) = sent
    assert text.startswith("SELL NOT FILLED — position kept")
    assert "limit $94.53" in text and "o-1" in text
    assert "restored and confirmed" in text


def test_filled_rotation_close_pages_closed(sent, monkeypatch):
    ctx = SimpleNamespace(rotation=_rotation())
    pipeline = _Pipeline("filled", {"status": "filled", "filled_qty": 10.0, "filled_avg_price": 94.6}, True)
    _submit(monkeypatch, ctx, pipeline)
    sell_loop.await_sell_and_finalize(pipeline, {"order_id": "o-1"}, {}, ctx)
    (text,) = sent
    assert text.startswith("POSITION CLOSED AUTOMATICALLY")


def test_other_sells_page_nothing(sent):
    ctx = SimpleNamespace(rotation=_rotation(sell_order_id="o-1"))
    pipeline = _Pipeline("filled", {"filled_qty": 5.0}, True)
    sell_loop.await_sell_and_finalize(pipeline, {"order_id": "o-other"}, {}, ctx)
    assert sent == []


def test_run_header_reads_fills_not_submitted_orders():
    assert _traded_word([{"action": "SELL", "fill_status": "pending_new"}]) == "ORDERED"
    assert _traded_word([{"action": "SELL", "fill_status": "filled"}]) == "SOLD"
    assert (
        _traded_word(
            [{"action": "SELL", "fill_status": "partially_filled"}, {"action": "BUY", "fill_status": "submitted"}]
        )
        == "SOLD"
    )
