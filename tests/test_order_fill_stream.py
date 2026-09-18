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
import fcntl
import inspect
import subprocess
import sys
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
        return AlpacaBroker(
            api_key="k", secret_key="s", paper=True,
            # These tests exercise the websocket machinery itself, which is
            # dormant in production since 2026-09-17. ON here so the whole
            # file doubles as the proof that flipping the flag back restores
            # the old behaviour exactly.
            fill_stream_enabled=True,
        )


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
    """Retry-After is honoured verbatim for a NON-rate-limit failure.

    CHANGED 2026-09-18: this test used to assert that a 429 carrying
    Retry-After 12 waited exactly 12s. It no longer may. Alpaca publishes
    the limit as 200 requests per MINUTE, so a retry 12s into the same
    exhausted window cannot clear it — a 429 now floors at the full
    published window. The verbatim-hint contract still holds for every
    other failure, which is what this now pins.
    """
    from src.execution.broker import _trading_stream_reconnect_delay

    class _TransientWithHint(ConnectionError):
        def __init__(self):
            super().__init__("connection reset")
            self.headers = {"retry-after": "12"}

    exc = _TransientWithHint()
    assert _trading_stream_reconnect_delay(1, exc) == 12.0
    assert _trading_stream_reconnect_delay(9, exc) == 12.0


def test_rate_limit_backs_off_harder_than_a_transient_error():
    """A 429 must wait strictly longer than a generic network failure.

    This is the whole lesson of 2026-09-15: 32,666 of the day's 32,896
    handshakes were HTTP 429, and retrying a rate limit on transport
    timings is what produced them.
    """
    from src.execution.broker import (
        _trading_stream_reconnect_delay,
        _STREAM_RATE_LIMIT_STAND_DOWN_S,
        _ALPACA_STREAM_RECONNECT_MAX_S,
    )

    # The rate-limit floor must exceed the TRANSPORT cap, or "harder" is
    # only true on early attempts.
    assert _STREAM_RATE_LIMIT_STAND_DOWN_S > _ALPACA_STREAM_RECONNECT_MAX_S

    for attempt in (1, 2, 5, 9, 50):
        transient = _trading_stream_reconnect_delay(
            attempt, ConnectionError("connection reset"),
        )
        rate_limited = _trading_stream_reconnect_delay(attempt, _Fake429())
        assert rate_limited > transient, (
            f"attempt {attempt}: 429 waited {rate_limited}s but a generic "
            f"error waited {transient}s"
        )
        assert rate_limited >= _STREAM_RATE_LIMIT_STAND_DOWN_S

    # A server asking us back sooner than its own published window does not
    # shorten the stand-down.
    assert (
        _trading_stream_reconnect_delay(1, _Fake429(retry_after=5))
        == _STREAM_RATE_LIMIT_STAND_DOWN_S
    )
    # A server asking for LONGER always wins.
    assert _trading_stream_reconnect_delay(
        1, _Fake429(retry_after=_STREAM_RATE_LIMIT_STAND_DOWN_S + 90),
    ) == _STREAM_RATE_LIMIT_STAND_DOWN_S + 90


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


def test_guarded_handshake_does_not_tight_loop_on_429(monkeypatch):
    """Old SDK shape: except + 10ms sleep. With Retry-After 0.2s the
    guarded `_start_ws` itself waits, so a 0.5s window cannot issue
    dozens of handshakes."""
    from src.execution import broker as broker_mod
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
    # The 429 stand-down is now the published 60s minute-window, which this
    # 0.5s probe cannot wait out. Shrink the constant, not the assertion:
    # what is under test is that the guard SLEEPS AT ALL between handshakes.
    monkeypatch.setattr(broker_mod, "_STREAM_RATE_LIMIT_STAND_DOWN_S", 0.2)
    broker_mod._STREAM_ATTEMPT_BUDGET.reset()
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
    from src.execution import broker as broker_mod
    from src.execution.broker import _install_trading_stream_reconnect_guard

    class Stream:
        _should_run = True

        async def _start_ws(self):
            raise _Fake429(retry_after=30)

        async def stop_ws(self):
            self._should_run = False

    stream = Stream()
    broker_mod._STREAM_ATTEMPT_BUDGET.reset()
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


# ---------- account-wide lease (cross-process ownership) ----------
#
# A threading.Lock cannot stop morning + intra (separate systemd jobs)
# from each opening a trade_updates socket. The owner is an advisory
# flock. Frame-drain of hub._last_status is not this. Lengthening auth
# backoff is not this. Repegs stay off.


_HOLD_LEASE_SCRIPT = """
import fcntl, sys, time
from pathlib import Path
path, ready, done = sys.argv[1], sys.argv[2], sys.argv[3]
fh = open(path, "a+")
fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
Path(ready).write_text("ready")
deadline = time.monotonic() + 30
while not Path(done).exists() and time.monotonic() < deadline:
    time.sleep(0.05)
"""


def _broker_with_lease(lease_path):
    with patch("src.execution.broker.TradingClient"):
        return AlpacaBroker(
            api_key="k", secret_key="s", paper=True,
            trade_updates_lease_path=str(lease_path),
            fill_stream_enabled=True,
        )


@patch("src.execution.broker.TradingStream")
def test_second_process_cannot_open_competing_socket_while_lease_held(
    mock_stream_cls, tmp_path,
):
    """Pin: while another process holds the account lease, this process
    must not construct a TradingStream at all."""
    lease_path = tmp_path / ".trade_updates.lock"
    ready = tmp_path / "ready"
    done = tmp_path / "done"
    holder = subprocess.Popen(
        [sys.executable, "-c", _HOLD_LEASE_SCRIPT,
         str(lease_path), str(ready), str(done)],
    )
    try:
        deadline = time.monotonic() + 5
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert ready.exists(), "second process never acquired the lease"
        mock_stream_cls.side_effect = lambda *a, **k: _FakeTradingStream(
            *a, hang=True, **k,
        )
        broker = _broker_with_lease(lease_path)
        try:
            warmup = broker.start_trade_updates()
            assert warmup.ready is False
            assert broker.trade_updates_started() is False
            assert mock_stream_cls.call_count == 0
        finally:
            broker.stop_trade_updates()
    finally:
        done.write_text("done")
        holder.wait(timeout=5)


@patch("src.execution.broker.TradingStream")
def test_consumer_uses_rest_when_lease_held_by_another_process(
    mock_stream_cls, tmp_path,
):
    """Pin: a fill wait that does not own the lease REST-polls with the
    caller's timeout and never opens a socket."""
    lease_path = tmp_path / ".trade_updates.lock"
    lease_path.parent.mkdir(parents=True, exist_ok=True)
    holder = open(lease_path, "a+")
    fcntl.flock(holder.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        mock_stream_cls.side_effect = lambda *a, **k: _FakeTradingStream(
            *a, hang=True, **k,
        )
        broker = _broker_with_lease(lease_path)
        broker.client.get_order_by_id.return_value = MagicMock(status="filled")
        t0 = time.monotonic()
        status = broker.wait_for_order_terminal(
            "order-1", timeout_seconds=0.4, poll_interval=0.0,
        )
        elapsed = time.monotonic() - t0
        assert status == "filled"
        assert mock_stream_cls.call_count == 0
        assert broker.client.get_order_by_id.called
        assert elapsed < 2.0
    finally:
        holder.close()


@patch("src.execution.broker.TradingStream")
def test_fill_wait_on_unauthed_hub_does_not_exceed_rest_timeout(mock_stream_cls):
    """Pin: remaining auth budget is not added on top of the fill wait.
    Protective stops use this same wait_for_order_terminal path."""
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
        timeout = 0.4
        t0 = time.monotonic()
        status = broker.wait_for_order_terminal(
            "order-1", timeout_seconds=timeout, poll_interval=0.0,
        )
        elapsed = time.monotonic() - t0
        assert status == "filled"
        assert elapsed < timeout + 1.5, (
            f"unauthed hub wait {elapsed:.2f}s exceeded REST ceiling "
            f"{timeout}s — auth leftover was stacked on the fill wait"
        )
        assert elapsed < 5.0, "must not sit out the 30s reconnect-max"
    finally:
        broker.stop_trade_updates()


@patch("src.execution.broker.TradingStream")
def test_silent_authed_hub_rest_polls_within_the_rest_interval(mock_stream_cls):
    """alpaca-py reconnects inside run() without killing the thread. A
    live-but-silent hub must not hide a fill longer than REST would."""
    class AuthedHang(_FakeTradingStream):
        async def _start_ws(self):
            return

        def run(self):
            super().run()

    mock_stream_cls.side_effect = lambda *a, **k: AuthedHang(
        *a, hang=True, **k,
    )
    broker = _broker()
    filled = MagicMock(status="filled")
    broker.client.get_order_by_id.return_value = filled
    try:
        broker.start_trade_updates()
        # Let the hub handshake mark authed.
        deadline = time.monotonic() + 2.0
        while not broker.trade_updates_authed() and time.monotonic() < deadline:
            time.sleep(0.01)
        t0 = time.monotonic()
        status = broker.wait_for_order_terminal(
            "order-1", timeout_seconds=0.8, poll_interval=0.2,
        )
        elapsed = time.monotonic() - t0
        assert status == "filled"
        assert elapsed < 0.8 + 1.5
        assert broker.client.get_order_by_id.called
    finally:
        broker.stop_trade_updates()
    """A hang with no auth hook waits the caller's timeout then one REST
    snapshot — not a second full polling window stacked on the first."""
    mock_stream_cls.side_effect = lambda *a, **k: _FakeTradingStream(
        *a, hang=True, **k,
    )
    broker = _broker()
    broker.client.get_order_by_id.return_value = MagicMock(status="new")
    timeout = 0.4
    t0 = time.monotonic()
    status = broker.wait_for_order_terminal(
        "order-1", timeout_seconds=timeout, poll_interval=0.2,
    )
    elapsed = time.monotonic() - t0
    assert status == "new"
    assert elapsed < timeout + 1.5
    # At least the last-known-status read. A dead one-shot may then REST
    # the remainder; it must not stack a second full window.
    assert broker.client.get_order_by_id.call_count >= 1
    assert elapsed < 2.0


def test_protective_fill_wait_is_the_bounded_terminal_wait():
    """place_entry_protection has no private stream wait. Its fill wait
    is wait_for_order_terminal, so the REST ceiling applies to stops."""
    src = inspect.getsource(AlpacaBroker.place_entry_protection)
    assert "wait_for_order_terminal" in src
    assert "TradingStream(" not in src
    assert "_wait_for_order_status_via_stream_locked" not in src


def test_frame_drain_is_not_the_ownership_fix():
    """_last_status lets a same-process waiter see an already-seen fill.
    The account owner is the lease, not that dict."""
    from src.execution import broker as broker_mod
    start_src = inspect.getsource(broker_mod.AlpacaBroker.start_trade_updates)
    wait_src = inspect.getsource(
        broker_mod.AlpacaBroker._wait_for_order_status_via_stream,
    )
    assert "_acquire_trade_updates_slot" in start_src
    assert "_acquire_trade_updates_slot" in wait_src
    assert "_TradeUpdatesLease" in inspect.getsource(broker_mod)
    assert "fcntl.flock" in inspect.getsource(broker_mod._TradeUpdatesLease)
    assert "_wait_for_first_event" not in inspect.getsource(broker_mod)



# === Auth-rejection diagnostics (2026-09-18) ===================================
#
# The socket was dark for a fortnight and the logs could not say why. Two
# layers hid the reason, and both are now covered here:
#
#   * the installed SDK's `_auth` compares `data.status != "authorized"` and
#     raises a bare `ValueError("failed to authenticate")`, DISCARDING
#     Alpaca's `{"data":{"message":"code=401, message=Unauthorized", ...}}`;
#   * a `ValueError` has no HTTP-status attribute, so the desk's own guard
#     logged `status=unknown` — which reads as a transport fault, and is
#     what sent six pull requests at the connection's timing instead of at
#     the placeholder credential that was actually being sent as a password.
#
# The rejection payload Alpaca really returns, verbatim:
_ALPACA_AUTH_REJECTION = (
    '{"stream":"authorization","data":'
    '{"message":"code=401, message=Unauthorized","status":"unauthorized"}}'
)

# The credential the process really held until 2026-09-18: 29 characters,
# containing the literal word "placeholder". Not a secret — that is the
# whole point of it.
_PLACEHOLDER_KEY = "placeholder-alpaca-key-29chr!"
_PLACEHOLDER_SECRET = "placeholder-alpaca-secret-value"


class _FakeAuthSocket:
    """The websocket object the SDK's `_auth` sends to and recvs from."""

    def __init__(self, reply):
        self._reply = reply
        self.sent = []

    async def send(self, payload):
        self.sent.append(payload)

    async def recv(self):
        return self._reply


class _FakeAuthStream:
    """Mirrors `alpaca.trading.stream.TradingStream`'s auth shape exactly.

    `_start_ws` -> `_connect` then `_auth`; `_auth` sends the in-band
    credential frame, reads ONE reply, and throws that reply away behind a
    bare ValueError. `_wait_before_reconnect` exists so the desk's guard
    does not add a sleep of its own (the real SDK also has it).
    """

    def __init__(self, api_key, secret_key, reply):
        import json as _json
        self._json = _json
        self._api_key = api_key
        self._secret_key = secret_key
        self._reply = reply
        self._endpoint = "wss://paper-api.alpaca.markets/stream"
        self._ws = None
        self._should_run = True
        self._reconnect_min_backoff = 1.0
        self._reconnect_max_backoff = 30.0

    async def _connect(self):
        self._ws = _FakeAuthSocket(self._reply)

    async def _auth(self):
        await self._ws.send(self._json.dumps({
            "action": "authenticate",
            "data": {"key_id": self._api_key, "secret_key": self._secret_key},
        }))
        msg = self._json.loads(await self._ws.recv())
        if msg.get("data").get("status") != "authorized":
            raise ValueError("failed to authenticate")

    async def _start_ws(self):
        await self._connect()
        await self._auth()

    async def _wait_before_reconnect(self, retries):
        return


def _rejected_stream(reply=_ALPACA_AUTH_REJECTION):
    from src.execution import broker as broker_mod

    stream = _FakeAuthStream(_PLACEHOLDER_KEY, _PLACEHOLDER_SECRET, reply)
    # getattr, not an import: the log-shape assertions below must fail on
    # the pre-fix code by printing the MISLEADING line, not by failing to
    # import the fix. That misleading line is the defect.
    install_diagnostics = getattr(
        broker_mod, "_install_trading_stream_auth_diagnostics", None,
    )
    if callable(install_diagnostics):
        install_diagnostics(stream)
    broker_mod._install_trading_stream_reconnect_guard(stream)
    return stream


def test_auth_rejection_carries_alpacas_own_words():
    """The broker's `message` and `status` survive the SDK's bare ValueError."""
    from src.execution.broker import TradeStreamAuthRejected

    stream = _rejected_stream()
    with pytest.raises(TradeStreamAuthRejected) as excinfo:
        asyncio.run(stream._start_ws())
    exc = excinfo.value
    assert exc.broker_message == "code=401, message=Unauthorized"
    assert exc.broker_status == "unauthorized"
    # The original, information-free SDK error is preserved as the cause.
    assert isinstance(exc.__cause__, ValueError)
    assert "failed to authenticate" in str(exc.__cause__)


def test_auth_rejection_is_not_logged_as_a_handshake_failure(caplog):
    """`status=unknown` is what made this look like a transport problem.

    An application-level credential refusal must say so in words, and must
    carry the broker's own message into the log line.
    """
    import logging

    stream = _rejected_stream()
    with caplog.at_level(logging.WARNING, logger="src.execution.broker"):
        with pytest.raises(Exception):
            asyncio.run(stream._start_ws())
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "trade_updates authentication REJECTED by broker" in text
    assert "code=401, message=Unauthorized" in text
    assert "unauthorized" in text
    # The old, misleading rendering must NOT be what an auth refusal prints.
    assert "handshake failed" not in text
    assert "status=unknown" not in text


def test_auth_rejection_never_logs_the_credential(caplog):
    """Length and first two characters only. Never the key, never the secret.

    The fingerprint exists because it is the one fact that would have ended
    the investigation on day one: a 29-character key starting `pl` is not an
    Alpaca key.
    """
    import logging

    stream = _rejected_stream()
    with caplog.at_level(logging.WARNING, logger="src.execution.broker"):
        with pytest.raises(Exception):
            asyncio.run(stream._start_ws())
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert _PLACEHOLDER_KEY not in text
    assert _PLACEHOLDER_SECRET not in text
    assert "secret" not in text.lower()
    assert f"length {len(_PLACEHOLDER_KEY)}" in text
    assert "starts 'pl'" in text


def test_auth_diagnostics_degrade_rather_than_replace_the_failure():
    """An unparseable reply must still raise the refusal, without a message.

    A diagnostic that can itself fail would hide the very failure it was
    added to explain.
    """
    from src.execution.broker import TradeStreamAuthRejected

    stream = _rejected_stream(reply='{"data": {"status": "nope"}}')
    with pytest.raises(TradeStreamAuthRejected) as excinfo:
        asyncio.run(stream._start_ws())
    assert excinfo.value.broker_status == "nope"
    assert excinfo.value.broker_message is None


def test_auth_diagnostics_are_a_noop_without_an_auth_method():
    """The fake streams in this suite have no `_auth`; nothing must break."""
    from src.execution.broker import _install_trading_stream_auth_diagnostics

    double = _FakeTradingStream()
    _install_trading_stream_auth_diagnostics(double)
    assert not hasattr(double, "_auth")


def test_successful_handshake_leaves_an_affirmative_log_line(caplog):
    """There was no line saying the socket EVER worked — only 1,017 saying
    it did not. The SDK's own success line is on the alpaca logger, which
    the desk's log does not carry, so the desk records its own."""
    import logging

    from src.execution.broker import _install_trading_stream_reconnect_guard

    authorized = (
        '{"stream":"authorization","data":'
        '{"message":"authorized","status":"authorized"}}'
    )
    stream = _FakeAuthStream(_PLACEHOLDER_KEY, _PLACEHOLDER_SECRET, authorized)
    _install_trading_stream_reconnect_guard(stream)
    with caplog.at_level(logging.INFO, logger="src.execution.broker"):
        asyncio.run(stream._start_ws())
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "trade_updates websocket authenticated" in text


def test_shipped_config_has_the_fill_stream_on():
    """Change 1: the flag is the only remaining gate, and it is now on.

    Asserted against the shipped `config/settings.yaml`, not a fixture —
    the flag being true in a test's own construction proves nothing about
    what production loads.
    """
    from pathlib import Path

    import yaml

    root = Path(__file__).resolve().parent.parent
    raw = yaml.safe_load((root / "config" / "settings.yaml").read_text())
    assert raw["execution"]["fill_stream_enabled"] is True


# ---------------------------------------------------------------------------
# Reconnect CEILINGS (2026-09-18). The backoff guard fixed the RATE of the
# 2026-09-15 storm but not its BOUND: the attempt counter lived in a closure
# and reset on every new hub, so a socket that can never authenticate
# retried at the 30s cap forever. These pin the ceiling that ends it.
# ---------------------------------------------------------------------------


class _CeilingStream:
    """Old-SDK shape: `_start_ws` that always fails, driven by a 10ms loop."""

    def __init__(self, exc_factory):
        self._should_run = True
        self._exc_factory = exc_factory
        self.attempts = 0

    async def _start_ws(self):
        self.attempts += 1
        raise self._exc_factory()

    async def stop_ws(self):
        self._should_run = False


async def _drive_sdk_loop(stream, max_iterations: int = 500) -> None:
    """Reproduce `alpaca.trading.stream._run_forever` (alpaca-py 0.43.5).

    Verbatim in the part that matters: catch everything, then
    `finally: await asyncio.sleep(0.01)`, then re-check `_should_run` at the
    top. No backoff and no ceiling of its own — which is the defect.
    """
    for _ in range(max_iterations):
        if not stream._should_run:
            return
        try:
            await stream._start_ws()
            return
        except Exception:
            pass
        finally:
            await asyncio.sleep(0)


def test_session_ceiling_stops_the_reconnect_loop(monkeypatch):
    """The loop must STOP, not merely slow down."""
    from src.execution import broker as broker_mod

    broker_mod._STREAM_ATTEMPT_BUDGET.reset()
    monkeypatch.setattr(broker_mod, "_STREAM_RATE_LIMIT_STAND_DOWN_S", 0.0)
    monkeypatch.setattr(broker_mod, "_ALPACA_STREAM_RECONNECT_MIN_S", 0.0)
    monkeypatch.setattr(broker_mod, "_ALPACA_STREAM_RECONNECT_MAX_S", 0.0)
    monkeypatch.setattr(broker_mod, "send_owner_alert", None, raising=False)
    sent: list[str] = []
    monkeypatch.setattr(
        broker_mod, "_alert_stream_gave_up", lambda reason: sent.append(reason),
    )

    stream = _CeilingStream(_Fake429)
    broker_mod._install_trading_stream_reconnect_guard(stream)
    asyncio.run(_drive_sdk_loop(stream))

    ceiling = broker_mod._STREAM_ATTEMPT_CEILING_PER_SESSION
    assert stream.attempts == ceiling, (
        f"expected the loop to stop at the {ceiling}-attempt session "
        f"ceiling, got {stream.attempts}"
    )
    # The SDK's own loop is told to exit, which is what actually ends it.
    assert stream._should_run is False
    assert sent == ["it rate-limited us"], "exactly one give-up, once"


def test_daily_ceiling_survives_a_fresh_session(monkeypatch):
    """A new hub must NOT hand the socket a fresh unbounded budget.

    This is the hole the closure-local counter left: every `start()` reset
    `failures` to zero, so nothing ever accumulated across a day.
    """
    from src.execution import broker as broker_mod

    broker_mod._STREAM_ATTEMPT_BUDGET.reset()
    monkeypatch.setattr(broker_mod, "_STREAM_RATE_LIMIT_STAND_DOWN_S", 0.0)
    monkeypatch.setattr(broker_mod, "_ALPACA_STREAM_RECONNECT_MIN_S", 0.0)
    monkeypatch.setattr(broker_mod, "_ALPACA_STREAM_RECONNECT_MAX_S", 0.0)
    monkeypatch.setattr(broker_mod, "_STREAM_ATTEMPT_CEILING_PER_DAY", 10)
    monkeypatch.setattr(broker_mod, "_alert_stream_gave_up", lambda reason: None)

    total = 0
    for _ in range(20):  # twenty fresh sockets in one day
        stream = _CeilingStream(_Fake429)
        broker_mod._install_trading_stream_reconnect_guard(stream)
        asyncio.run(_drive_sdk_loop(stream))
        total += stream.attempts

    assert total <= 10 + broker_mod._STREAM_ATTEMPT_CEILING_PER_SESSION, (
        f"twenty sessions spent {total} handshakes against a daily ceiling "
        "of 10 — the budget is not shared across sessions"
    )
    assert broker_mod._STREAM_ATTEMPT_BUDGET.day_exhausted()


def test_budget_rolls_over_to_a_new_day():
    """Yesterday's exhaustion must not keep the socket shut forever."""
    from src.execution.broker import _StreamAttemptBudget

    budget = _StreamAttemptBudget()
    for _ in range(500):
        budget.record_attempt("2026-09-15")
    assert budget.day_exhausted("2026-09-15")
    assert budget.claim_alert("2026-09-15") is True
    assert budget.claim_alert("2026-09-15") is False, "alert is once a day"

    assert not budget.day_exhausted("2026-09-16")
    assert budget.attempts_today("2026-09-16") == 0
    assert budget.claim_alert("2026-09-16") is True


def test_giveup_alert_is_plain_english_and_says_fills_still_work():
    """The owner reads this on a phone and must not need to act on it."""
    from src.execution.broker import _stream_giveup_owner_message

    text = _stream_giveup_owner_message("it rate-limited us")
    lowered = text.lower()
    assert "instant fill alerts switched off" in lowered
    assert "slower way" in lowered
    assert "nothing for you to do" in lowered
    for jargon in (
        "websocket", "trade_updates", "429", "http", "handshake",
        "backoff", "rest", "auth", "socket",
    ):
        assert jargon not in lowered, f"owner text leaked the term {jargon!r}"


def test_auth_rejection_also_hits_the_ceiling(monkeypatch):
    """A wrong credential must give up too, not just a rate limit.

    2026-09-15 was a credential refusal that PRESENTED as 429s. Bounding
    only the rate-limit path would leave the original fault unbounded.
    """
    from src.execution import broker as broker_mod

    broker_mod._STREAM_ATTEMPT_BUDGET.reset()
    monkeypatch.setattr(broker_mod, "_ALPACA_STREAM_RECONNECT_MIN_S", 0.0)
    monkeypatch.setattr(broker_mod, "_ALPACA_STREAM_RECONNECT_MAX_S", 0.0)
    sent: list[str] = []
    monkeypatch.setattr(
        broker_mod, "_alert_stream_gave_up", lambda reason: sent.append(reason),
    )

    def _rejected():
        return broker_mod.TradeStreamAuthRejected(
            broker_message="code=401, message=Unauthorized",
            broker_status="unauthorized",
            credential="PKplaceholderplaceholder1234",
        )

    stream = _CeilingStream(_rejected)
    broker_mod._install_trading_stream_reconnect_guard(stream)
    asyncio.run(_drive_sdk_loop(stream))

    assert stream.attempts == broker_mod._STREAM_ATTEMPT_CEILING_PER_SESSION
    assert stream._should_run is False
    assert sent == ["it rejected our credential"]


# ---------------------------------------------------------------------------
# AUTH FORMAT DEPRECATION (2026-09-18). Alpaca's authorization reply says the
# payload we send is being deprecated in favour of
# {"action":"auth","key":K,"secret":S}. That payload is built by the VENDOR,
# in alpaca-py's `TradingStream._auth`, so the migration is upstream's — and
# alpaca-py 0.44.0, the newest release on PyPI on 2026-09-18, still sends the
# old form, so there is no version to bump to. These tests exist so that fact
# is a mechanical check rather than a remembered promise: the first one goes
# RED the day upstream migrates.
# ---------------------------------------------------------------------------

_ALPACA_DEPRECATION_REPLY = (
    '{"stream":"authorization","data":{"action":"authenticate",'
    '"message":"this authentication format is being deprecated. Please use '
    'the format: {\\"action\\": \\"auth\\", \\"key\\": \\"x\\", '
    '\\"secret\\": \\"x\\"}","status":"authorized"}}'
)


def test_the_installed_sdk_still_builds_the_deprecated_auth_payload():
    """Pins WHOSE payload this is, and WHICH form it is, against the real SDK.

    Read this failing test as good news, not a defect: if it goes red
    because the payload is now `{"action":"auth","key":...,"secret":...}`,
    alpaca-py has done the migration and the desk can DELETE its own frame
    (`_send_current_auth_format`) and hand the job back to the vendor. If
    it goes red some other way, the SDK changed shape and the wrapper that
    stands in front of it needs a look before the socket is trusted.

    It pins the SDK INSTALLED HERE, which is the CI box, not production —
    `pyproject.toml` has no upper bound and there is no lockfile. It is a
    trigger to look, not a statement about what the desk is running.
    """
    import json as _json

    alpaca_stream = pytest.importorskip("alpaca.trading.stream")

    sent: list[str] = []

    class _RecordingSocket:
        async def send(self, payload):
            sent.append(payload)

        async def recv(self):
            return '{"stream":"authorization","data":{"status":"authorized"}}'

    stream = alpaca_stream.TradingStream("key-not-real", "secret-not-real")
    stream._ws = _RecordingSocket()
    asyncio.run(stream._auth())

    assert len(sent) == 1
    payload = _json.loads(sent[0])
    assert payload["action"] == "authenticate", (
        "alpaca-py now sends a different auth action "
        f"({payload.get('action')!r}). If it is 'auth', the vendor has "
        "migrated: retire `_send_current_auth_format` and this test."
    )
    assert set(payload["data"]) == {"key_id", "secret_key"}
    # The new form's keys must NOT be at the top level yet — that is the
    # exact signal this test exists to catch.
    assert "key" not in payload and "secret" not in payload


def test_the_brokers_deprecation_notice_reaches_the_desks_own_log(caplog):
    """The notice arrived on every successful handshake and was discarded.

    A broker telling us our auth format is going away must not be
    swallowed: the alternative record is a handshake that silently stops
    working one day.
    """
    import logging

    from src.execution import broker as broker_mod

    broker_mod._stream_auth_deprecation_logged = False
    stream = _rejected_stream(reply=_ALPACA_DEPRECATION_REPLY)
    with caplog.at_level(logging.WARNING, logger="src.execution.broker"):
        asyncio.run(stream._start_ws())
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "trade_updates auth format is DEPRECATED by the broker" in text
    assert "this authentication format is being deprecated" in text
    # Says whose problem it is, so nobody re-derives it a third time.
    assert "alpaca-py" in text


def test_the_deprecation_notice_is_logged_once_not_once_per_handshake(caplog):
    """Once per process. A per-handshake line is what the 2026-09-15 storm
    cost: 32,896 attempts would have printed 32,896 of these."""
    import logging

    from src.execution import broker as broker_mod

    broker_mod._stream_auth_deprecation_logged = False
    with caplog.at_level(logging.WARNING, logger="src.execution.broker"):
        for _ in range(4):
            asyncio.run(_rejected_stream(
                reply=_ALPACA_DEPRECATION_REPLY,
            )._start_ws())
    hits = [
        r for r in caplog.records
        if "auth format is DEPRECATED" in r.getMessage()
    ]
    assert len(hits) == 1


def test_no_deprecation_notice_is_invented_when_the_broker_sends_none(caplog):
    """An authorized reply with no notice must print nothing.

    A warning the broker did not send is an inference dressed as a
    measurement, and would survive long after the migration landed.
    """
    import logging

    from src.execution import broker as broker_mod

    broker_mod._stream_auth_deprecation_logged = False
    authorized = (
        '{"stream":"authorization","data":'
        '{"action":"authenticate","status":"authorized"}}'
    )
    with caplog.at_level(logging.WARNING, logger="src.execution.broker"):
        asyncio.run(_rejected_stream(reply=authorized)._start_ws())
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "DEPRECATED" not in text


def test_the_deprecation_notice_never_carries_a_credential(caplog):
    """Same rule as every other line this module prints about auth."""
    import logging

    from src.execution import broker as broker_mod

    broker_mod._stream_auth_deprecation_logged = False
    with caplog.at_level(logging.WARNING, logger="src.execution.broker"):
        asyncio.run(_rejected_stream(
            reply=_ALPACA_DEPRECATION_REPLY,
        )._start_ws())
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert _PLACEHOLDER_KEY not in text
    assert _PLACEHOLDER_SECRET not in text


class _SilentAuthSocket:
    """A broker that accepts the auth frame and then never answers.

    This is the shape a silent retirement of the deprecated auth format
    takes, and it is NOT the rejection path: there is no reply to parse,
    so nothing raises on its own.
    """

    def __init__(self):
        self.sent = []

    async def send(self, payload):
        self.sent.append(payload)

    async def recv(self):
        await asyncio.sleep(3600)


class _SilentAuthStream(_FakeAuthStream):
    def __init__(self):
        super().__init__(_PLACEHOLDER_KEY, _PLACEHOLDER_SECRET, reply="")

    async def _connect(self):
        self._ws = _SilentAuthSocket()

    async def _auth(self):
        await self._ws.send("{}")
        await self._ws.recv()


def test_a_broker_that_never_answers_auth_does_not_park_the_thread(monkeypatch):
    """alpaca-py awaits the auth reply with no timeout; its own `_consume`
    bounds the same call. Without a bound here, a silent broker means no
    exception, so the reconnect ceiling never counts and never gives up —
    the socket would report itself live forever. The bound is the hub's
    existing auth budget, not a new number."""
    from src.execution import broker as broker_mod

    monkeypatch.setattr(broker_mod, "_ALPACA_STREAM_AUTH_DEADLINE_S", 0.2)
    stream = _SilentAuthStream()
    asyncio.run(stream._connect())
    broker_mod._install_trading_stream_auth_diagnostics(stream)

    started = time.monotonic()
    with pytest.raises(broker_mod.TradeStreamAuthRejected) as excinfo:
        asyncio.run(stream._auth())
    elapsed = time.monotonic() - started

    assert elapsed < 5, "the handshake was not bounded"
    assert excinfo.value.broker_status == "no reply"
    assert "no reply to the authentication frame" in excinfo.value.broker_message
    assert isinstance(excinfo.value.__cause__, asyncio.TimeoutError)


def test_the_silent_broker_is_counted_by_the_reconnect_ceiling(monkeypatch):
    """The timeout must arrive as the same kind of failure the ceiling
    already ends on — otherwise bounding the wait just moves the unbounded
    loop one level out."""
    from src.execution import broker as broker_mod

    monkeypatch.setattr(broker_mod, "_ALPACA_STREAM_AUTH_DEADLINE_S", 0.05)
    monkeypatch.setattr(broker_mod, "_STREAM_ATTEMPT_CEILING_PER_SESSION", 2)
    monkeypatch.setattr(
        broker_mod, "_trading_stream_reconnect_delay",
        lambda *a, **k: 0.0,
    )
    broker_mod._STREAM_ATTEMPT_BUDGET.reset()
    monkeypatch.setattr(broker_mod, "_alert_stream_gave_up", lambda reason: None)

    stream = _SilentAuthStream()
    broker_mod._install_trading_stream_auth_diagnostics(stream)
    broker_mod._install_trading_stream_reconnect_guard(stream)

    for _ in range(2):
        with pytest.raises(broker_mod.TradeStreamAuthRejected):
            asyncio.run(stream._start_ws())
    assert stream._should_run is False
    assert broker_mod._STREAM_ATTEMPT_BUDGET.attempts_today() == 2
    broker_mod._STREAM_ATTEMPT_BUDGET.reset()


# ---------------------------------------------------------------------------
# AUTH FORMAT MIGRATION (2026-09-18). The desk now sends the format Alpaca
# asks for, with the deprecated one kept as a fallback because the current
# form is proven on exactly one host.
# ---------------------------------------------------------------------------

_AUTHORIZED_NEW_FORM = (
    '{"stream":"authorization","data":'
    '{"action":"authenticate","status":"authorized"}}'
)


def test_the_handshake_sends_the_format_the_broker_asks_for():
    """The frame on the wire is the CURRENT one, not alpaca-py's."""
    import json as _json

    stream = _rejected_stream(reply=_AUTHORIZED_NEW_FORM)
    asyncio.run(stream._start_ws())

    assert len(stream._ws.sent) == 1
    frame = _json.loads(stream._ws.sent[0])
    assert frame["action"] == "auth"
    assert frame["key"] == _PLACEHOLDER_KEY
    assert frame["secret"] == _PLACEHOLDER_SECRET
    # The deprecated envelope must be gone, not merely accompanied.
    assert "data" not in frame


def test_a_refused_current_format_falls_back_on_the_NEXT_socket(caplog):
    """Never a second frame on a socket the broker just refused.

    Alpaca closes a refused connection, so re-sending there would fail for
    a reason unrelated to the format and would read as a bad credential.
    The mark is what makes the next handshake — on a fresh socket — send
    the deprecated form.
    """
    import json as _json
    import logging

    from src.execution import broker as broker_mod

    stream = _rejected_stream()  # refuses everything
    with caplog.at_level(logging.WARNING, logger="src.execution.broker"):
        with pytest.raises(broker_mod.TradeStreamAuthRejected):
            asyncio.run(stream._start_ws())

    assert stream._qamc_auth_fallback is True
    assert len(stream._ws.sent) == 1, "a refused socket must not be re-sent on"
    assert _json.loads(stream._ws.sent[0])["action"] == "auth"
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "the CURRENT format" in text and "was refused" in text
    assert "falls back to the deprecated format" in text

    # Second handshake on a fresh socket: now the vendor's frame.
    asyncio.run(stream._connect())
    with pytest.raises(broker_mod.TradeStreamAuthRejected):
        asyncio.run(stream._auth())
    assert _json.loads(stream._ws.sent[0])["action"] == "authenticate"


def test_a_successful_current_format_handshake_says_so_once(caplog):
    """Positive evidence in the desk's own log that the migration took.

    The absence of a failure is what let a socket that never authenticated
    look healthy for three days; it must not be the evidence again.
    """
    import logging

    from src.execution import broker as broker_mod

    broker_mod._stream_current_auth_format_logged = False
    with caplog.at_level(logging.INFO, logger="src.execution.broker"):
        for _ in range(3):
            asyncio.run(_rejected_stream(
                reply=_AUTHORIZED_NEW_FORM,
            )._start_ws())
    hits = [
        r for r in caplog.records
        if "authenticated with the CURRENT auth format" in r.getMessage()
    ]
    assert len(hits) == 1


def test_the_fallback_still_reaches_the_ceiling_and_gives_up(monkeypatch):
    """Adding a format attempt must not add an unbounded one.

    The fallback costs handshakes out of the SAME budget; a refusal
    sequence still ends at the session ceiling.
    """
    from src.execution import broker as broker_mod

    monkeypatch.setattr(broker_mod, "_STREAM_ATTEMPT_CEILING_PER_SESSION", 3)
    monkeypatch.setattr(
        broker_mod, "_trading_stream_reconnect_delay", lambda *a, **k: 0.0,
    )
    monkeypatch.setattr(broker_mod, "_alert_stream_gave_up", lambda reason: None)
    broker_mod._STREAM_ATTEMPT_BUDGET.reset()

    stream = _rejected_stream()
    for _ in range(3):
        with pytest.raises(broker_mod.TradeStreamAuthRejected):
            asyncio.run(stream._start_ws())
    assert stream._should_run is False
    assert broker_mod._STREAM_ATTEMPT_BUDGET.attempts_today() == 3
    broker_mod._STREAM_ATTEMPT_BUDGET.reset()
