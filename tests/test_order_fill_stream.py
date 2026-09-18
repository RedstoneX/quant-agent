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
