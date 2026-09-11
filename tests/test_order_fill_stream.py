"""Real-time order-fill detection via Alpaca's trade_updates websocket
(2026-09-10), replacing a fixed-interval REST poll as the primary mechanism.

Background: `wait_for_order_terminal` used to ask "did it fill yet?" once a
second in a loop for up to N seconds — the owner correctly pointed out this
is not how anyone using a real-time-capable broker API actually watches an
order, and that Alpaca's own documentation recommends its `trade_updates`
stream for exactly this. This suite proves the new dispatch logic without
any real network I/O, using a fake `TradingStream` double.
"""
import asyncio
import time
from unittest.mock import patch, MagicMock

import pytest

from src.execution.broker import AlpacaBroker


class _FakeOrder:
    def __init__(self, order_id, status):
        self.id = order_id
        self.status = status


class _FakeUpdate:
    def __init__(self, order_id, status):
        self.order = _FakeOrder(order_id, status)


class _FakeTradingStream:
    """Stands in for `alpaca.trading.stream.TradingStream`. `run()` feeds
    every canned update to the subscribed handler once, synchronously,
    mirroring the real class's blocking event-loop shape closely enough
    for this dispatch logic to be exercised honestly."""

    def __init__(self, *_args, updates=None, raise_on_subscribe=None,
                 raise_on_run=None, hang=False, **_kwargs):
        self._updates = updates or []
        self._handler = None
        self._raise_on_subscribe = raise_on_subscribe
        self._raise_on_run = raise_on_run
        self._hang = hang
        self.stopped = False
        self.stop_called = False

    def subscribe_trade_updates(self, handler):
        if self._raise_on_subscribe:
            raise self._raise_on_subscribe
        self._handler = handler

    def run(self):
        if self._raise_on_run:
            raise self._raise_on_run

        async def _go():
            for update in self._updates:
                if self.stopped:
                    return
                await self._handler(update)
            if self._hang:
                # Simulate a connection that authenticated and is idling
                # with no more updates — stays "running" until stop() is
                # called from the main thread, same as the real class.
                while not self.stopped:
                    await asyncio.sleep(0.01)

        asyncio.run(_go())

    def stop(self):
        self.stop_called = True
        self.stopped = True

    async def stop_ws(self):
        self.stopped = True


def _broker():
    with patch("src.execution.broker.TradingClient"):
        return AlpacaBroker(api_key="k", secret_key="s", paper=True)


# ---------- the fast path: a real terminal event for OUR order ----------

@patch("src.execution.broker.TradingStream")
def test_stream_returns_immediately_on_matching_fill(mock_stream_cls):
    mock_stream_cls.side_effect = lambda *a, **k: _FakeTradingStream(
        *a, updates=[_FakeUpdate("order-1", "filled")], **k,
    )
    broker = _broker()

    start = time.monotonic()
    status = broker.wait_for_order_terminal("order-1", timeout_seconds=10.0)
    elapsed = time.monotonic() - start

    assert status == "filled"
    # The whole point: no multi-second wait for a fill that already happened.
    assert elapsed < 2.0


@patch("src.execution.broker.TradingStream")
def test_stream_ignores_updates_for_other_orders(mock_stream_cls):
    """An update for a different order must not be mistaken for ours, and
    must still prove the connection is alive (so a genuine non-match falls
    to a single REST check, not the full polling fallback)."""
    mock_stream_cls.side_effect = lambda *a, **k: _FakeTradingStream(
        *a, updates=[_FakeUpdate("some-other-order", "filled")], hang=True, **k,
    )
    broker = _broker()
    broker.client.get_order_by_id.return_value = MagicMock(status="new")

    status = broker.wait_for_order_terminal("order-1", timeout_seconds=0.3)

    assert status == "new"
    # Exactly one REST call — the single "last known status" check, not a
    # polling loop.
    assert broker.client.get_order_by_id.call_count == 1


# ---------- stream unusable: must fall back to the old polling path ----

@patch("src.execution.broker.TradingStream")
def test_falls_back_to_polling_when_stream_never_connects(mock_stream_cls):
    mock_stream_cls.side_effect = lambda *a, **k: _FakeTradingStream(
        *a, raise_on_run=ConnectionError("no network"), **k,
    )
    broker = _broker()
    open_order = MagicMock(status="new")
    filled_order = MagicMock(status="filled")
    broker.client.get_order_by_id.side_effect = [open_order, filled_order]

    status = broker.wait_for_order_terminal(
        "order-1", timeout_seconds=2.0, poll_interval=0.0,
    )

    assert status == "filled"
    assert broker.client.get_order_by_id.call_count == 2


@patch("src.execution.broker.TradingStream")
def test_falls_back_to_polling_when_subscribe_raises(mock_stream_cls):
    mock_stream_cls.side_effect = lambda *a, **k: _FakeTradingStream(
        *a, raise_on_subscribe=RuntimeError("bad handler"), **k,
    )
    broker = _broker()
    broker.client.get_order_by_id.return_value = MagicMock(status="filled")

    status = broker.wait_for_order_terminal(
        "order-1", timeout_seconds=2.0, poll_interval=0.0,
    )

    assert status == "filled"


def test_falls_back_to_polling_when_stream_library_missing():
    with patch("src.execution.broker.TradingStream", None):
        broker = _broker()
        broker.client.get_order_by_id.return_value = MagicMock(status="filled")

        status = broker.wait_for_order_terminal(
            "order-1", timeout_seconds=2.0, poll_interval=0.0,
        )

    assert status == "filled"


# ---------- explicit opt-out ----------

def test_use_stream_false_skips_straight_to_polling():
    with patch("src.execution.broker.TradingStream") as mock_stream_cls:
        broker = _broker()
        broker.client.get_order_by_id.return_value = MagicMock(status="filled")

        status = broker.wait_for_order_terminal(
            "order-1", timeout_seconds=2.0, poll_interval=0.0, use_stream=False,
        )

        assert status == "filled"
        mock_stream_cls.assert_not_called()


# ---------- terminal-state set stays in sync between the two paths ------

def test_stream_and_polling_share_the_same_terminal_states():
    """A status the stream treats as terminal must be the same set the
    polling fallback treats as terminal — see `_ORDER_TERMINAL_STATES`."""
    assert AlpacaBroker._ORDER_TERMINAL_STATES == {
        "filled", "canceled", "cancelled", "expired", "rejected",
        "done_for_day", "replaced",
    }
