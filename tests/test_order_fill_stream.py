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
import threading
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


# ---------- one replace at a time (2026-09-11) ----------
#
# Alpaca refuses to replace an order whose status is `accepted`,
# `pending_new`, `pending_cancel` or `pending_replace`. A replacement leaves
# the order in `pending_replace` while the broker works, so a second PATCH
# fired into that window is rejected outright. `await_replacement_confirmed`
# is the gate: it watches the OLD order reach the terminal status `replaced`
# on this same stream before the caller is allowed to send another one.

@patch("src.execution.broker.TradingStream")
def test_replacement_confirmed_by_a_replaced_event_on_the_stream(mock_stream_cls):
    mock_stream_cls.side_effect = lambda *a, **k: _FakeTradingStream(
        *a, updates=[_FakeUpdate("old-1", "replaced")], **k,
    )
    broker = _broker()

    assert broker.await_replacement_confirmed(
        "old-1", "new-2", timeout_seconds=10.0,
    ) is True


@patch("src.execution.broker.TradingStream")
def test_a_fill_on_the_old_order_is_not_a_replacement_confirmation(mock_stream_cls):
    """`filled` is terminal but it is not `replaced`. Confirming on any
    terminal status would wave through exactly the case where the swap did
    NOT happen."""
    mock_stream_cls.side_effect = lambda *a, **k: _FakeTradingStream(
        *a, updates=[_FakeUpdate("old-1", "filled")], **k,
    )
    broker = _broker()
    broker.resolve_replacement_chain = MagicMock(return_value="old-1")

    assert broker.await_replacement_confirmed(
        "old-1", "new-2", timeout_seconds=10.0,
    ) is False


@patch("src.execution.broker.TradingStream")
def test_a_missed_event_falls_back_to_asking_the_broker(mock_stream_cls):
    """No `replaced` event inside the window is not proof of anything, so
    the chain is re-read rather than guessed at either way."""
    mock_stream_cls.side_effect = lambda *a, **k: _FakeTradingStream(
        *a, updates=[_FakeUpdate("someone-else", "filled")], **k,
    )
    broker = _broker()
    broker._get_order_status_once = MagicMock(return_value="pending_replace")
    broker.resolve_replacement_chain = MagicMock(return_value="new-2")

    assert broker.await_replacement_confirmed(
        "old-1", "new-2", timeout_seconds=1.0,
    ) is True


@patch("src.execution.broker.TradingStream")
def test_an_unreadable_broker_is_never_read_as_confirmed(mock_stream_cls):
    """"I could not confirm" and "it is safe to send another replace" are
    different statements."""
    mock_stream_cls.side_effect = lambda *a, **k: _FakeTradingStream(
        *a, updates=[], **k,
    )
    broker = _broker()
    broker._get_order_status_once = MagicMock(return_value=None)
    broker.resolve_replacement_chain = MagicMock(return_value=None)

    assert broker.await_replacement_confirmed(
        "old-1", "new-2", timeout_seconds=1.0,
    ) is False


def test_confirmation_never_raises_when_the_wait_blows_up():
    broker = _broker()
    broker.wait_for_order_terminal = MagicMock(
        side_effect=RuntimeError("websocket gone"))

    assert broker.await_replacement_confirmed("old-1", "new-2") is False


def test_confirmation_refuses_empty_ids():
    broker = _broker()
    assert broker.await_replacement_confirmed("", "new-2") is False
    assert broker.await_replacement_confirmed("old-1", "") is False


def test_replaced_is_a_terminal_state_the_stream_actually_reports():
    """The gate depends on this: if `replaced` were not in the terminal set
    the stream handler would ignore the very event being waited for."""
    assert "replaced" in AlpacaBroker._ORDER_TERMINAL_STATES


# ---------- 2026-09-12: "has the exchange got it yet?" on the same stream ----------
# The single-shot entry reprice may only be sent once the order has left
# Alpaca's not-yet-at-exchange statuses (`accepted`, `pending_new` — its own
# lifecycle reference). Same websocket, same fallback shape as the fill wait.

def test_pre_exchange_states_are_the_documented_ones():
    assert AlpacaBroker._ORDER_PRE_EXCHANGE_STATES == frozenset({"accepted", "pending_new"})
    assert AlpacaBroker._ORDER_REPLACEABLE_STATES == frozenset({"new"})
    assert not (AlpacaBroker._ORDER_PRE_EXCHANGE_STATES & AlpacaBroker._ORDER_TERMINAL_STATES)


@patch("src.execution.broker.TradingStream")
def test_at_exchange_wait_returns_new_the_instant_the_stream_says_so(mock_stream_cls):
    mock_stream_cls.side_effect = lambda *a, **k: _FakeTradingStream(
        updates=[
            _FakeUpdate("other", "new"),           # someone else's order
            _FakeUpdate("ord-1", "pending_new"),   # ours, still pre-venue
            _FakeUpdate("ord-1", "new"),           # ours, acknowledged
        ],
    )
    b = _broker()

    t0 = time.monotonic()
    status = b.wait_for_order_at_exchange("ord-1", timeout_seconds=5.0)

    assert status == "new"
    assert time.monotonic() - t0 < 2.0, "should not have waited out the window"
    b.client.get_order_by_id.assert_not_called()


@patch("src.execution.broker.TradingStream")
def test_at_exchange_wait_reports_a_fill_that_arrives_instead(mock_stream_cls):
    mock_stream_cls.side_effect = lambda *a, **k: _FakeTradingStream(
        updates=[_FakeUpdate("ord-1", "filled")],
    )
    assert _broker().wait_for_order_at_exchange("ord-1", timeout_seconds=2.0) == "filled"


@patch("src.execution.broker.TradingStream")
def test_at_exchange_wait_does_a_single_rest_read_when_nothing_arrives(mock_stream_cls):
    mock_stream_cls.side_effect = lambda *a, **k: _FakeTradingStream(
        updates=[_FakeUpdate("ord-1", "accepted")], hang=True,
    )
    b = _broker()
    b.client.get_order_by_id.return_value = MagicMock(status=MagicMock(value="accepted"))

    status = b.wait_for_order_at_exchange("ord-1", timeout_seconds=0.3)

    assert status == "accepted"                       # honest: still pre-venue
    assert b.client.get_order_by_id.call_count == 1   # one read, not a poll loop


def test_at_exchange_wait_falls_back_to_rest_polling_without_the_stream():
    b = _broker()
    b.client.get_order_by_id.side_effect = [
        MagicMock(status=MagicMock(value="accepted")),
        MagicMock(status=MagicMock(value="pending_new")),
        MagicMock(status=MagicMock(value="new")),
    ]
    with patch("src.execution.broker.TradingStream", None):
        status = b.wait_for_order_at_exchange(
            "ord-1", timeout_seconds=3.0, poll_interval=0.01,
        )

    assert status == "new"
    assert b.client.get_order_by_id.call_count == 3


def test_at_exchange_wait_reports_last_known_when_polling_times_out():
    b = _broker()
    b.client.get_order_by_id.return_value = MagicMock(status=MagicMock(value="accepted"))
    with patch("src.execution.broker.TradingStream", None):
        status = b.wait_for_order_at_exchange(
            "ord-1", timeout_seconds=0.05, poll_interval=0.01,
        )
    assert status == "accepted"


def test_the_terminal_wait_still_ignores_a_mere_new_event():
    """Sharing the stream primitive must not have changed the fill wait:
    `new` is not terminal for it."""
    with patch("src.execution.broker.TradingStream") as mock_stream_cls:
        mock_stream_cls.side_effect = lambda *a, **k: _FakeTradingStream(
            updates=[_FakeUpdate("ord-1", "new")], hang=True,
        )
        b = _broker()
        b.client.get_order_by_id.return_value = MagicMock(status=MagicMock(value="new"))
        status = b.wait_for_order_terminal("ord-1", timeout_seconds=0.2)
    assert status == "new"
    assert b.client.get_order_by_id.call_count == 1


# ---------- HTTP 429 reconnect storm (alpaca-py #740) ----------
#
# Older TradingStream._run_forever retries a failed handshake every 10ms.
# Alpaca allows one trade_updates connection per account, so a 429 then
# logs thousands of times during a fill wait. The guard backs off; the
# lock keeps a second wait from opening another socket.

class _Fake429(Exception):
    def __init__(self, retry_after=None):
        super().__init__("HTTP 429 Too Many Requests")
        self.status_code = 429
        if retry_after is not None:
            self.headers = {"retry-after": str(retry_after)}


def test_reconnect_delay_honors_server_retry_after():
    from src.execution.broker import _trading_stream_reconnect_delay
    exc = _Fake429(retry_after=12)
    assert _trading_stream_reconnect_delay(1, exc) == 12.0
    assert _trading_stream_reconnect_delay(9, exc) == 12.0


def test_reconnect_delay_grows_then_caps_without_retry_after():
    from src.execution.broker import _equal_jitter_backoff
    d1 = _equal_jitter_backoff(1, 1.0, 30.0)
    d2 = _equal_jitter_backoff(2, 1.0, 30.0)
    d_hi = _equal_jitter_backoff(8, 1.0, 30.0)
    assert 0.5 <= d1 <= 1.0
    assert 1.0 <= d2 <= 2.0
    assert 15.0 <= d_hi <= 30.0


def test_stream_http_status_reads_429_from_exc_and_message():
    from src.execution.broker import _stream_http_status
    typed = _Fake429()
    assert _stream_http_status(typed) == 429
    assert _stream_http_status(Exception("HTTP 429 Too Many Requests")) == 429
    assert _stream_http_status(ConnectionError("timed out")) is None


def test_guarded_handshake_does_not_tight_loop_on_429():
    """Old SDK shape: except + 10ms sleep. With Retry-After 0.2s the
    guarded `_start_ws` itself waits, so a 0.5s window cannot issue
    dozens of handshakes."""
    from src.execution.broker import _install_trading_stream_reconnect_guard

    attempts = {"n": 0}

    class Stream:
        _should_run = True

        async def _start_ws(self):
            attempts["n"] += 1
            raise _Fake429(retry_after=0.2)

        async def stop_ws(self):
            self._should_run = False

    stream = Stream()
    _install_trading_stream_reconnect_guard(stream)

    async def _old_sdk_loop(duration: float) -> None:
        deadline = time.monotonic() + duration
        while time.monotonic() < deadline and stream._should_run:
            try:
                await stream._start_ws()
                return
            except _Fake429:
                await asyncio.sleep(0.01)

    asyncio.run(_old_sdk_loop(0.5))
    # Unguarded: ~50 attempts. Guarded: one handshake, ~0.2s wait, another.
    assert 1 <= attempts["n"] <= 4


def test_guard_stop_interrupts_backoff_wait():
    from src.execution.broker import _install_trading_stream_reconnect_guard

    class Stream:
        _should_run = True

        async def _start_ws(self):
            raise _Fake429(retry_after=30)

        async def stop_ws(self):
            self._should_run = False

    stream = Stream()
    _install_trading_stream_reconnect_guard(stream)

    async def _fail_then_stop():
        task = asyncio.create_task(stream._start_ws())
        await asyncio.sleep(0.05)
        await stream.stop_ws()
        with pytest.raises(_Fake429):
            await task

    start = time.monotonic()
    asyncio.run(_fail_then_stop())
    assert time.monotonic() - start < 2.0, "stop() must not wait out Retry-After"


@patch("src.execution.broker.TradingStream")
def test_fill_still_arrives_when_reconnect_guard_wraps_start_ws(mock_stream_cls):
    """Installing the guard on a stream that actually has `_start_ws` must
    not swallow a matching fill."""

    class StreamWithStart(_FakeTradingStream):
        async def _start_ws(self):
            return

    mock_stream_cls.side_effect = lambda *a, **k: StreamWithStart(
        *a, updates=[_FakeUpdate("order-1", "filled")], **k,
    )
    status = _broker().wait_for_order_terminal("order-1", timeout_seconds=10.0)
    assert status == "filled"


@patch("src.execution.broker.TradingStream")
def test_concurrent_wait_does_not_open_a_second_stream(mock_stream_cls):
    """A second fill-wait while the first stream is live must REST-poll
    rather than handshake another trade_updates socket."""
    started = threading.Event()

    class HangStream(_FakeTradingStream):
        def run(self):
            started.set()
            super().run()

    mock_stream_cls.side_effect = lambda *a, **k: HangStream(
        *a, hang=True, **k,
    )
    broker = _broker()
    broker.client.get_order_by_id.return_value = MagicMock(status="new")

    first = threading.Thread(
        target=lambda: broker.wait_for_order_terminal(
            "order-a", timeout_seconds=1.0,
        ),
        daemon=True,
    )
    first.start()
    assert started.wait(timeout=2.0), "first stream never started"
    status = broker.wait_for_order_terminal(
        "order-b", timeout_seconds=2.0, poll_interval=0.0,
    )
    first.join(timeout=3.0)

    assert mock_stream_cls.call_count == 1
    assert status == "new"
    assert broker.client.get_order_by_id.called


@patch("src.execution.broker.TradingStream")
def test_start_trade_updates_returns_without_waiting_for_auth(mock_stream_cls):
    started = threading.Event()

    class HangAuth(_FakeTradingStream):
        async def _start_ws(self):
            await asyncio.sleep(60)

        def run(self):
            started.set()
            super().run()

    mock_stream_cls.side_effect = lambda *a, **k: HangAuth(
        *a, hang=True, **k,
    )
    broker = _broker()
    t0 = time.monotonic()
    try:
        warmup = broker.start_trade_updates()
        elapsed = time.monotonic() - t0
        assert elapsed < 2.0, "start_trade_updates must not block on handshake"
        assert started.wait(timeout=2.0), "hub thread never started"
        assert warmup.handshake_failed is False
        assert broker.trade_updates_started() is True
        assert mock_stream_cls.call_count == 1
    finally:
        broker.stop_trade_updates()


@patch("src.execution.broker.TradingStream")
def test_fill_wait_reuses_prestarted_hub(mock_stream_cls):
    mock_stream_cls.side_effect = lambda *a, **k: _FakeTradingStream(
        *a, updates=[_FakeUpdate("order-1", "filled")], hang=True, **k,
    )
    broker = _broker()
    try:
        broker.start_trade_updates()
        t0 = time.monotonic()
        status = broker.wait_for_order_terminal("order-1", timeout_seconds=5.0)
        assert status == "filled"
        assert time.monotonic() - t0 < 2.0
        assert mock_stream_cls.call_count == 1
    finally:
        broker.stop_trade_updates()


@patch("src.execution.broker.TradingStream")
def test_exhausted_auth_budget_falls_to_rest_without_waiting_again(mock_stream_cls):
    class NeverAuth(_FakeTradingStream):
        async def _start_ws(self):
            await asyncio.sleep(60)

    mock_stream_cls.side_effect = lambda *a, **k: NeverAuth(
        *a, hang=True, **k,
    )
    broker = _broker()
    broker.client.get_order_by_id.return_value = MagicMock(status="filled")
    try:
        broker.start_trade_updates()
        assert broker._trade_hub is not None
        broker._trade_hub.started_mono = time.monotonic() - 31.0
        t0 = time.monotonic()
        status = broker.wait_for_order_terminal(
            "order-1", timeout_seconds=10.0, poll_interval=0.0,
        )
        assert time.monotonic() - t0 < 2.0
        assert status == "filled"
        assert broker._last_stream_warmup is not None
        assert broker._last_stream_warmup.handshake_failed is True
        assert broker._last_stream_warmup.rest_fallback
        assert "temporary REST" in broker._last_stream_warmup.rest_fallback
        assert "Not REST-only" in broker._last_stream_warmup.rest_fallback
        assert mock_stream_cls.call_count == 1
    finally:
        broker.stop_trade_updates()


@patch("src.execution.broker.TradingStream")
def test_dead_hub_falls_to_rest_polling_not_one_snapshot(mock_stream_cls):
    """If the kept socket dies, fill waits REST-poll the window. They must
    not report the stream as still fine (one snapshot) and must not open a
    second handshake while the process lock is held."""
    mock_stream_cls.side_effect = lambda *a, **k: _FakeTradingStream(
        *a, hang=True, **k,
    )
    broker = _broker()
    broker.client.get_order_by_id.return_value = MagicMock(status="filled")
    try:
        broker.start_trade_updates()
        hub = broker._trade_hub
        assert hub is not None
        hub.stop()
        assert hub.is_alive() is False
        t0 = time.monotonic()
        status = broker.wait_for_order_terminal(
            "order-1", timeout_seconds=2.0, poll_interval=0.0,
        )
        assert time.monotonic() - t0 < 2.0
        assert status == "filled"
        assert mock_stream_cls.call_count == 1
        assert broker.client.get_order_by_id.called
    finally:
        broker.stop_trade_updates()


@patch("src.execution.broker.TradingStream")
def test_wait_trade_updates_auth_does_not_invent_a_longer_deadline(mock_stream_cls):
    """Open Risk must not sit in handshake after the encoded budget from
    hub open is gone. REST is labelled temporary, not the product."""
    class NeverAuth(_FakeTradingStream):
        async def _start_ws(self):
            await asyncio.sleep(60)

    mock_stream_cls.side_effect = lambda *a, **k: NeverAuth(
        *a, hang=True, **k,
    )
    broker = _broker()
    try:
        broker.start_trade_updates()
        assert broker._trade_hub is not None
        broker._trade_hub.started_mono = time.monotonic() - 31.0
        t0 = time.monotonic()
        ok = broker.wait_trade_updates_auth()
        assert time.monotonic() - t0 < 2.0
        assert ok is False
        assert broker._last_stream_warmup is not None
        assert broker._last_stream_warmup.handshake_failed is True
        assert "temporary REST" in (broker._last_stream_warmup.rest_fallback or "")
    finally:
        broker.stop_trade_updates()


@patch("src.execution.broker.TradingStream")
def test_wait_trade_updates_auth_requires_authorized_not_merely_connected(
    mock_stream_cls,
):
    """A trade_updates handler fire is not the handshake. Open Risk must
    wait for authorized."""
    class NeverAuth(_FakeTradingStream):
        async def _start_ws(self):
            await asyncio.sleep(60)

    mock_stream_cls.side_effect = lambda *a, **k: NeverAuth(
        *a, hang=True, **k,
    )
    broker = _broker()
    try:
        broker.start_trade_updates()
        hub = broker._trade_hub
        assert hub is not None
        assert hub.handshake_hook is True
        hub._connected.set()
        assert hub.authed() is True
        assert hub.authorized() is False
        hub.started_mono = time.monotonic() - 31.0
        t0 = time.monotonic()
        ok = broker.wait_trade_updates_auth()
        assert time.monotonic() - t0 < 2.0
        assert ok is False
    finally:
        broker.stop_trade_updates()


def test_auth_hook_drains_non_auth_frames_then_authorizes():
    """alpaca-py's `_auth` treats the first websocket frame as the
    handshake. A hello/listening frame before `authorized` used to raise
    `failed to authenticate` and retry through the Risk window."""
    import json

    from src.execution.broker import _install_trading_stream_reconnect_guard

    class WS:
        def __init__(self):
            self.sent = []
            self._frames = [
                json.dumps({"stream": "listening", "data": {"streams": ["trade_updates"]}}),
                json.dumps({"stream": "authorization", "data": {"status": "authorized"}}),
            ]

        async def send(self, payload):
            self.sent.append(payload)

        async def recv(self):
            return self._frames.pop(0)

    class Stream:
        async def _start_ws(self):
            await self._auth()

        async def _auth(self):
            raise AssertionError("original _auth must not run once wrapped")

        async def stop_ws(self):
            pass

    stream = Stream()
    stream._ws = WS()
    stream._api_key = "k"
    stream._secret_key = "s"
    stream._endpoint = "wss://paper-api.alpaca.markets/stream"
    stream._qamc_authed = threading.Event()
    _install_trading_stream_reconnect_guard(stream)
    asyncio.run(stream._start_ws())
    assert stream._qamc_authed.is_set()
    sent = json.loads(stream._ws.sent[0])
    assert sent["action"] == "authenticate"
    assert sent["data"]["key_id"] == "k"


def test_auth_hook_drains_list_payload_then_authorizes():
    """Some Alpaca sockets wrap frames in a JSON list. The first-frame
    handshake still has to find `authorized` inside that list."""
    import json

    from src.execution.broker import _install_trading_stream_reconnect_guard

    class WS:
        def __init__(self):
            self.sent = []
            self._frames = [
                json.dumps([{"stream": "listening"}]),
                json.dumps([{
                    "stream": "authorization",
                    "data": {"status": "authorized"},
                }]),
            ]

        async def send(self, payload):
            self.sent.append(payload)

        async def recv(self):
            return self._frames.pop(0)

    class Stream:
        async def _start_ws(self):
            await self._auth()

        async def _auth(self):
            raise AssertionError("original _auth must not run once wrapped")

        async def stop_ws(self):
            pass

    stream = Stream()
    stream._ws = WS()
    stream._api_key = "k"
    stream._secret_key = "s"
    stream._endpoint = "wss://paper-api.alpaca.markets/stream"
    stream._qamc_authed = threading.Event()
    _install_trading_stream_reconnect_guard(stream)
    asyncio.run(stream._start_ws())
    assert stream._qamc_authed.is_set()


def test_auth_hook_raises_named_failure_on_unauthorized():
    import json

    from src.execution.broker import _install_trading_stream_reconnect_guard

    class WS:
        async def send(self, payload):
            return None

        async def recv(self):
            return json.dumps({
                "data": {"status": "unauthorized", "message": "already connected"},
            })

    class Stream:
        async def _start_ws(self):
            await self._auth()

        async def _auth(self):
            raise AssertionError("original _auth must not run once wrapped")

        async def stop_ws(self):
            pass

    stream = Stream()
    stream._ws = WS()
    stream._api_key = "k"
    stream._secret_key = "s"
    stream._qamc_authed = threading.Event()
    _install_trading_stream_reconnect_guard(stream)
    with pytest.raises(ValueError, match="failed to authenticate: already connected"):
        asyncio.run(stream._start_ws())
    assert stream._qamc_authed.is_set() is False

