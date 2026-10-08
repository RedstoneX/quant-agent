"""The `trade_updates` websocket plumbing, lifted verbatim from src/execution/broker.py.

Fourth (final) broker instalment. Every module-level name below moved here as-is;
`src.execution.broker` re-exports each one (and mirrors writes back here) so
existing importers and patch targets on the broker module still resolve.
`TradeStreamWaits` holds the former `AlpacaBroker` stream methods; its
collaborators are keyword-only, and the per-broker state the bodies mutate
(`_trade_hub`, `_trade_slot_held`, ...) is reached through the `state` collaborator.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import logging
import re
import threading
import time

from src.execution.broker_parts.trade_stream_errors import _stream_http_status, _stream_retry_after_seconds  # noqa: F401
from src.sentinel.guarded import record_guarded_pass
from src.execution.broker_parts.trade_stream_lease import _TradeUpdatesLease  # noqa: F401

try:
    from alpaca.trading.stream import TradingStream
except ImportError:  # pragma: no cover - optional dependency surface
    TradingStream = None

# Same log channel as before the move: operators and tests filter on the
# broker's logger name, and the move must not change what they see.
from src.execution.broker_parts.trade_stream_identity import checked_socket_identity
from src.session_identity import _credential_fingerprint  # noqa: F401 re-export
logger = logging.getLogger("src.execution.broker")

# Alpaca allows one `trade_updates` websocket per account. Each fill wait
# used to construct its own TradingStream; a second handshake while the
# first socket was still registered is HTTP 429, and older alpaca-py
# `_run_forever` loops retried that handshake every 10ms (alpacahq/alpaca-py
# #740). A threading.Lock serializes waiters inside one process. Morning
# and intra (and other) systemd jobs are separate processes; the account
# slot is an advisory flock (`_TradeUpdatesLease`) so only one desk
# process may open the socket. Anyone else attaches to that process's
# hub or REST-polls — never a second handshake.
_TRADE_UPDATES_STREAM_LOCK = threading.Lock()


def _default_trade_updates_lease_path() -> Path:
    """Cwd-relative so an isolated test cwd cannot flock production data/.

    TradingPipeline passes the absolute path next to the DB — the same
    convention as `.intraday_scan.lock`. The constraint is the Alpaca
    account, not the database; the file sits beside the DB because that
    directory is already the desk's on-box coordination point.
    """
    return Path("data") / ".trade_updates.lock"


from src.execution.broker_parts.trade_stream_bounds import (  # noqa: F401
    _ALPACA_STREAM_AUTH_DEADLINE_S,
    _ALPACA_STREAM_RECONNECT_MAX_S,
    _ALPACA_STREAM_RECONNECT_MIN_S,
)
from src.execution.broker_parts.trade_stream_auth import (  # noqa: F401
    TradeStreamAuthRejected,
    _STREAM_AUTH_DEPRECATION_MARKER,
    _fell_back_to_deprecated_auth,
    _install_trading_stream_auth_diagnostics,
    _note_current_auth_format_accepted,
    _note_stream_auth_deprecation,
    _parse_stream_auth_reply,
)
from src.execution.broker_parts.trade_stream_reconnect import (  # noqa: F401
    TradeStreamGaveUp,
    _STREAM_ATTEMPT_BUDGET,
    _STREAM_ATTEMPT_CEILING_PER_DAY,
    _STREAM_ATTEMPT_CEILING_PER_SESSION,
    _STREAM_RATE_LIMIT_STAND_DOWN_S,
    _StreamAttemptBudget,
    _alert_stream_gave_up,
    _equal_jitter_backoff,
    _install_trading_stream_reconnect_guard,
    _stream_giveup_owner_message,
    _trading_stream_reconnect_delay,
)





@dataclass(frozen=True)
class TradeStreamWarmup:
    """Result of starting the kept trade_updates socket (handshake may still be in flight)."""

    ready: bool
    handshake_failed: bool
    retried: bool


class _HubWaiter:
    """One fill/status wait attached to the kept trade_updates hub."""

    def __init__(self, stop_states: frozenset):
        self.stop_states = stop_states
        self.event = threading.Event()
        self.status: str | None = None


class _TradeUpdatesHub:
    """One trade_updates websocket kept across Risk → funding → fills.

    Alpaca allows one trade_updates socket per account. Opening it only
    inside `wait_for_order_*` made handshake serial after Risk and burned
    the fill/funding timeout (measured 2026-09-16). Start during Risk so
    auth overlaps the review; fill waits attach here instead of opening
    a second socket. Auth budget starts at `started_mono` and uses
    alpaca-py's reconnect max — not a second fitted clock.

    Cross-process ownership is the account lease on the broker, not this
    object. Frame-drain of `_last_status` is a same-process attach aid,
    not the ownership fix. A wait on this hub is bounded by the caller's
    timeout (the REST path's ceiling) — auth leftover is not added on
    top, and a dead thread falls to REST instead of sitting out the
    window.
    """

    def __init__(self, broker: "AlpacaBroker"):
        self._broker = broker
        self._stream = None
        self._thread: threading.Thread | None = None
        self._connected = threading.Event()
        self._authed = threading.Event()
        self._waiters_lock = threading.Lock()
        self._waiters: dict[str, list[_HubWaiter]] = {}
        self._last_status: dict[str, str] = {}
        self.started_mono = time.monotonic()
        self.handshake_hook = False
        self._run_error: list[Exception] = []
        self._stopped = False
        self._dead = threading.Event()
        self._gate = threading.Condition()

    def _kick(self) -> None:
        with self._gate:
            self._gate.notify_all()

    def start(self) -> None:
        logger.info("trade_updates identity: %s", checked_socket_identity(self._broker))
        stream = TradingStream(self._broker.api_key, self._broker.secret_key,
                               paper=self._broker._paper)
        stream._qamc_authed = self._authed
        stream._qamc_connected = self._connected
        _orig_authed_set = self._authed.set
        _orig_authed_clear = self._authed.clear

        def _authed_set() -> None:
            _orig_authed_set()
            self._kick()

        def _authed_clear() -> None:
            _orig_authed_clear()
            self._kick()

        self._authed.set = _authed_set  # type: ignore[method-assign]
        self._authed.clear = _authed_clear  # type: ignore[method-assign]
        _install_trading_stream_auth_diagnostics(stream)
        _install_trading_stream_reconnect_guard(stream)
        self.handshake_hook = callable(getattr(stream, "_start_ws", None))
        self._stream = stream

        async def _handler(update) -> None:
            try:
                self._connected.set()
                self._kick()
                order = getattr(update, "order", None)
                oid = str(getattr(order, "id", "") or "")
                if not oid:
                    return
                status = str(getattr(getattr(order, "status", None), "value",
                                     getattr(order, "status", ""))).lower()
                to_signal: list[_HubWaiter] = []
                with self._waiters_lock:
                    self._last_status[oid] = status
                    for waiter in self._waiters.get(oid, []):
                        if status in waiter.stop_states:
                            to_signal.append(waiter)
                for waiter in to_signal:
                    waiter.status = status
                    waiter.event.set()
                if to_signal:
                    self._kick()
                record_guarded_pass(self._broker, "trade_stream.hub_handler", context={})
            except Exception as exc:
                record_guarded_pass(self._broker, "trade_stream.hub_handler", exc, log=logger,
                                    context={"effect": "a fill update was dropped; waiters fall back to REST"})

        stream.subscribe_trade_updates(_handler)

        def _run() -> None:
            try:
                stream.run()
            except Exception as exc:
                self._run_error.append(exc)
            finally:
                self._dead.set()
                with self._waiters_lock:
                    for bucket in self._waiters.values():
                        for waiter in bucket:
                            waiter.event.set()
                self._kick()

        self._thread = threading.Thread(
            target=_run, name="trade-updates-hub", daemon=True,
        )
        self._thread.start()

    def is_alive(self) -> bool:
        thread = self._thread
        return (
            not self._stopped
            and not self._dead.is_set()
            and thread is not None
            and thread.is_alive()
        )

    def authed(self) -> bool:
        return self._authed.is_set() or self._connected.is_set()

    def auth_remaining_s(self) -> float:
        if not self.handshake_hook:
            return float("inf")
        left = _ALPACA_STREAM_AUTH_DEADLINE_S - (
            time.monotonic() - self.started_mono
        )
        return max(0.0, float(left))

    def wait(
        self, order_id: str, timeout_seconds: float, *,
        stop_states: frozenset,
        poll_interval: float = 1.0,
    ) -> tuple[str | None, bool]:
        oid = str(order_id)
        deadline = time.monotonic() + max(0.0, float(timeout_seconds))
        # Liveness cadence is the REST path's own poll interval — not a
        # second clock. poll_interval<=0 waits the remaining window in
        # one shot (the tests that drive REST with a zero sleep).
        interval = (
            float(poll_interval)
            if poll_interval and float(poll_interval) > 0
            else None
        )

        def _mark_auth_failed() -> None:
            self._broker._last_stream_warmup = TradeStreamWarmup(
                ready=False, handshake_failed=True, retried=True,
            )

        if self.handshake_hook and not self.authed():
            remaining_auth = self.auth_remaining_s()
            remaining_wait = deadline - time.monotonic()
            if remaining_auth <= 0 or remaining_wait <= 0:
                logger.warning(
                    "trade_updates websocket did not authenticate within the "
                    "encoded %.1fs budget (started at hub open) — REST for %s",
                    _ALPACA_STREAM_AUTH_DEADLINE_S, oid,
                )
                _mark_auth_failed()
                return None, False
            try:
                with self._gate:
                    self._gate.wait_for(
                        lambda: self.authed() or self._dead.is_set(),
                        timeout=min(remaining_auth, remaining_wait),
                    )
                record_guarded_pass(self._broker, "trade_stream.hub_wait.auth_gate", context={})
            except Exception as exc:
                record_guarded_pass(self._broker, "trade_stream.hub_wait.auth_gate", exc, log=logger, context={"effect": "auth wait aborted; caller falls back to REST"})
            if not self.authed():
                logger.warning(
                    "trade_updates websocket did not authenticate within "
                    "the wait ceiling — REST for %s",
                    oid,
                )
                _mark_auth_failed()
                return None, False

        waiter = _HubWaiter(stop_states)
        with self._waiters_lock:
            last = self._last_status.get(oid)
            if last in stop_states:
                return last, True
            self._waiters.setdefault(oid, []).append(waiter)
        try:
            while waiter.status is None:
                if not self.is_alive():
                    break
                if self.handshake_hook and not self.authed():
                    # Handshake dropped after the auth gate. Do not sit the
                    # rest of the fill window on a dead reconnect loop.
                    _mark_auth_failed()
                    return None, False
                left = deadline - time.monotonic()
                if left <= 0:
                    break
                slice_s = left if interval is None else min(left, interval)
                with self._gate:
                    self._gate.wait_for(
                        lambda: (
                            waiter.status is not None
                            or self._dead.is_set()
                            or (self.handshake_hook and not self.authed())
                        ),
                        timeout=slice_s,
                    )
        finally:
            with self._waiters_lock:
                bucket = self._waiters.get(oid, [])
                if waiter in bucket:
                    bucket.remove(waiter)
        if waiter.status is not None:
            return waiter.status, True
        if not self.is_alive():
            # Thread died mid-wait: do not report the stream as fine.
            # Callers REST-poll for the rest of the window.
            return None, False
        if self._run_error and not self._connected.is_set() and not self.authed():
            return None, False
        return None, True if (self._connected.is_set() or self.authed()) else False

    def stop(self) -> None:
        self._stopped = True
        self._dead.set()
        with self._waiters_lock:
            for bucket in self._waiters.values():
                for waiter in bucket:
                    waiter.event.set()
        self._kick()
        stream = self._stream
        if stream is not None:
            try:
                setattr(stream, "_should_run", False)
                record_guarded_pass(self._broker, "trade_stream.hub_stop.should_run", context={})
            except Exception as exc:
                record_guarded_pass(self._broker, "trade_stream.hub_stop.should_run", exc, log=logger, context={"effect": "socket may keep reconnecting after stop"})
            try:
                stream.stop()
                record_guarded_pass(self._broker, "trade_stream.hub_stop.stream_stop", context={})
            except Exception as exc:
                record_guarded_pass(self._broker, "trade_stream.hub_stop.stream_stop", exc, log=logger, context={"effect": "socket may stay open after stop"})
        thread = self._thread
        if thread is not None:
            thread.join(timeout=5.0)

    def thread_still_running(self) -> bool:
        thread = self._thread
        return thread is not None and thread.is_alive()


class _OnState:
    """A read or write of this attribute goes to the `state` collaborator (the
    object that owned the lifted bodies -- an `AlpacaBroker` in production), so
    the moved bodies, verbatim, keep mutating the SAME flags, hub and warmup
    record across calls, and `getattr(self, name, default)` still falls back to
    the default when the host never set the attribute."""

    def __init__(self):
        self.name = None

    def __set_name__(self, owner, name):
        self.name = name

    def __get__(self, obj, objtype=None):
        if obj is None:
            return self
        return getattr(obj._state, self.name)

    def __set__(self, obj, value):
        setattr(obj._state, self.name, value)


class TradeStreamWaits:
    """The `trade_updates` slot, hub lifecycle and stream-backed order waits, lifted
    verbatim from `AlpacaBroker`. Built per call by the broker's thin shims."""

    _trade_hub = _OnState()
    _trade_slot_held = _OnState()
    _trade_lease_contended = _OnState()
    _last_stream_warmup = _OnState()
    _fill_stream_off_logged = _OnState()
    _fill_stream_enabled = _OnState()
    _trade_lease = _OnState()
    _trade_hub_lock = _OnState()
    _paper = _OnState()
    api_key = _OnState()
    secret_key = _OnState()

    def __init__(
        self, *,
        state,
        get_order_status_once,
        wait_for_order_status_via_polling,
        start_trade_updates=None,
        _wait_for_order_status_via_stream=None,
        _wait_for_order_status_via_stream_locked=None,
        fill_stream_enabled=None,
        _release_trade_updates_slot=None,
        _acquire_trade_updates_slot=None,
        _hub_warmup=None,
        _fill_stream_off_warmup=None,
    ):
        self._state = state
        self._get_order_status_once = get_order_status_once
        self._wait_for_order_status_via_polling = wait_for_order_status_via_polling
        if start_trade_updates is not None:
            self.start_trade_updates = start_trade_updates
        if _wait_for_order_status_via_stream is not None:
            self._wait_for_order_status_via_stream = _wait_for_order_status_via_stream
        if _wait_for_order_status_via_stream_locked is not None:
            self._wait_for_order_status_via_stream_locked = _wait_for_order_status_via_stream_locked
        if fill_stream_enabled is not None:
            self.fill_stream_enabled = fill_stream_enabled
        if _release_trade_updates_slot is not None:
            self._release_trade_updates_slot = _release_trade_updates_slot
        if _acquire_trade_updates_slot is not None:
            self._acquire_trade_updates_slot = _acquire_trade_updates_slot
        if _hub_warmup is not None:
            self._hub_warmup = _hub_warmup
        if _fill_stream_off_warmup is not None:
            self._fill_stream_off_warmup = _fill_stream_off_warmup


    def _acquire_trade_updates_slot(self) -> bool:
        """Non-blocking owner of the account-wide trade_updates socket.

        In-process threading lock plus the cross-process flock. Either
        failing means someone already owns the Alpaca slot — the caller
        must REST, not handshake.
        """
        if self._trade_slot_held:
            return True
        if not _TRADE_UPDATES_STREAM_LOCK.acquire(blocking=False):
            return False
        if not self._trade_lease.acquire(blocking=False):
            try:
                _TRADE_UPDATES_STREAM_LOCK.release()
            except RuntimeError:
                pass
            return False
        self._trade_slot_held = True
        return True

    def _release_trade_updates_slot(self) -> None:
        held = self._trade_slot_held
        self._trade_slot_held = False
        self._trade_lease.release()
        if held:
            try:
                _TRADE_UPDATES_STREAM_LOCK.release()
            except RuntimeError:
                pass

    def fill_stream_enabled(self) -> bool:
        """True when the desk is allowed to open the `trade_updates` socket.

        Public because the submit-window budget needs it: `src/pipeline_stages.py`
        adds the handshake budget only when a handshake can actually happen.
        """
        return bool(getattr(self, "_fill_stream_enabled", False))

    def _fill_stream_off_warmup(self) -> TradeStreamWarmup:
        """The warmup an intentionally-disabled socket reports.

        `handshake_failed` and `retried` are both FALSE on purpose. They are
        what `_start_trade_updates_early` / `_warm_trade_updates` /
        `_adopt_stream_stall` read to set `ctx.desk_latency_stall` — i.e. "the
        desk lost time to a broken socket". A socket that was never opened
        because the owner switched it off cost no time and is not a stall, so
        reporting a failure here would rebuild the exact noise this switch
        removes, just in a different field.
        """
        if not self._fill_stream_off_logged:
            # Once per process, at INFO — ops still needs to be able to see
            # WHY fills are REST-confirmed. This deliberately replaces the
            # ~150 auth-failure WARNINGs a day, and must never become a
            # per-order line.
            logger.info(
                "trade_updates websocket disabled by configuration "
                "(execution.fill_stream_enabled) — fills are confirmed by "
                "the bounded REST path; no socket, no lease, no reconnect loop",
            )
            self._fill_stream_off_logged = True
        return TradeStreamWarmup(ready=False, handshake_failed=False, retried=False)

    def ensure_trade_updates(self) -> TradeStreamWarmup:
        """Start (or reuse) the kept trade_updates socket. Does not wait for auth.

        A probe-then-teardown handshake duplicated auth. This starts the
        real socket and leaves it up for fill waits. Call from Risk so the
        handshake overlaps the review; Execution calling again is a no-op.
        """
        return self.start_trade_updates()

    def trade_updates_lease_contended(self) -> bool:
        """True when another process owns the account-wide trade_updates slot."""
        return bool(getattr(self, "_trade_lease_contended", False))

    def trade_updates_started(self) -> bool:
        hub = getattr(self, "_trade_hub", None)
        return hub is not None and hub.is_alive()

    def trade_updates_authed(self) -> bool:
        hub = getattr(self, "_trade_hub", None)
        return bool(hub is not None and hub.authed())

    def trade_updates_auth_remaining_s(self) -> float | None:
        """Seconds left on the encoded auth budget, or None when it does not apply.

        None: no hub, or a test double with no handshake hook (no auth wait).
        0: the reconnect-max budget started at hub open is gone and we must
        not sit in `_auth` after Risk.
        """
        hub = getattr(self, "_trade_hub", None)
        if hub is None or not hub.is_alive():
            return None
        if not hub.handshake_hook:
            return None
        return hub.auth_remaining_s()

    def _hub_warmup(self, hub: _TradeUpdatesHub) -> TradeStreamWarmup:
        failed = (
            hub.handshake_hook
            and hub.auth_remaining_s() <= 0
            and not hub.authed()
        )
        return TradeStreamWarmup(
            ready=hub.is_alive() and not failed,
            handshake_failed=failed,
            retried=failed,
        )

    def start_trade_updates(self) -> TradeStreamWarmup:
        """Open the kept trade_updates socket without blocking on handshake.

        No-op reuse when this process already owns a live hub. Refuses to
        open a socket when another process holds the account lease.

        Refuses outright, before the lease is even attempted, when
        `execution.fill_stream_enabled` is off — see that flag for why.
        """
        if not self.fill_stream_enabled():
            warmup = self._fill_stream_off_warmup()
            self._last_stream_warmup = warmup
            return warmup
        if TradingStream is None:
            warmup = TradeStreamWarmup(
                ready=False, handshake_failed=True, retried=False,
            )
            self._last_stream_warmup = warmup
            return warmup
        with self._trade_hub_lock:
            hub = self._trade_hub
            if hub is not None and hub.is_alive():
                warmup = self._hub_warmup(hub)
                self._last_stream_warmup = warmup
                return warmup
            if hub is not None:
                try:
                    hub.stop()
                    record_guarded_pass(self._state, "trade_stream.start_trade_updates.stop_old_hub", context={})
                except Exception as exc:
                    record_guarded_pass(self._state, "trade_stream.start_trade_updates.stop_old_hub", exc, log=logger, context={"effect": "old hub may linger; thread check follows"})
                if hub.thread_still_running():
                    logger.warning(
                        "previous trade_updates thread still running — "
                        "not opening another socket",
                    )
                    warmup = TradeStreamWarmup(
                        ready=False, handshake_failed=False, retried=False,
                    )
                    self._last_stream_warmup = warmup
                    return warmup
                self._trade_hub = None
                self._release_trade_updates_slot()
            if not self._acquire_trade_updates_slot():
                logger.info(
                    "trade_updates lease held by another process — "
                    "not opening a competing socket",
                )
                self._trade_lease_contended = True
                warmup = TradeStreamWarmup(
                    ready=False, handshake_failed=False, retried=False,
                )
                self._last_stream_warmup = warmup
                return warmup
            self._trade_lease_contended = False
            try:
                hub = _TradeUpdatesHub(self)
                hub.start()
                self._trade_hub = hub
                record_guarded_pass(self._state, "trade_stream.hub_start", context={})
            except Exception as exc:
                record_guarded_pass(self._state, "trade_stream.hub_start", exc, log=logger,
                                    context={"effect": "no fill stream; REST confirms fills"})
                self._release_trade_updates_slot()
                warmup = TradeStreamWarmup(
                    ready=False, handshake_failed=True, retried=False,
                )
                self._last_stream_warmup = warmup
                return warmup
            warmup = self._hub_warmup(hub)
            self._last_stream_warmup = warmup
            return warmup

    def stop_trade_updates(self) -> None:
        """Tear down the kept trade_updates socket and release the lease.

        The lease stays held if the stream thread is still running after
        stop() — releasing it would let another process handshake while
        Alpaca still has this socket registered.
        """
        with self._trade_hub_lock:
            hub = self._trade_hub
            if hub is not None:
                try:
                    hub.stop()
                    record_guarded_pass(self._state, "trade_stream.stop_trade_updates.stop_hub", context={})
                except Exception as exc:
                    record_guarded_pass(self._state, "trade_stream.stop_trade_updates.stop_hub", exc, log=logger, context={"effect": "hub may linger; thread check follows"})
                if hub.thread_still_running():
                    logger.warning(
                        "trade_updates thread still running after stop — "
                        "keeping the account lease so a second process cannot "
                        "open a competing socket",
                    )
                    return
            self._trade_hub = None
            self._release_trade_updates_slot()

    def _wait_for_order_status(
        self,
        order_id: str,
        timeout_seconds: float,
        poll_interval: float,
        *,
        stop_states: frozenset,
        use_stream: bool,
        unavailable_log: str,
    ) -> str | None:
        """Stream first, REST as the bound — never a stacked second window.

        A live-but-silent hub (alpaca-py reconnecting inside `run()`) is
        sliced at the REST poll interval so a fill cannot hide longer than
        the REST path would have taken to see it.

        `execution.fill_stream_enabled` off takes the SAME branch as an
        explicit `use_stream=False` — deliberately the identical code path,
        not a parallel one, so the configuration change cannot alter a
        bounded wait. Same timeout, same poll interval, same ceiling.
        """
        if not use_stream or not self.fill_stream_enabled():
            return self._wait_for_order_status_via_polling(
                order_id, timeout_seconds, poll_interval,
                stop_states=stop_states,
            )
        deadline = time.monotonic() + max(0.0, float(timeout_seconds))
        interval = (
            float(poll_interval)
            if poll_interval and float(poll_interval) > 0
            else 0.0
        )
        last: str | None = None
        logged_unavailable = False
        hub = getattr(self, "_trade_hub", None)
        slice_stream = bool(hub is not None and hub.is_alive() and interval > 0)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return last if last is not None else self._get_order_status_once(
                    order_id,
                )
            slice_s = remaining if not slice_stream else min(remaining, interval)
            status, connected = self._wait_for_order_status_via_stream(
                order_id, slice_s, stop_states=stop_states,
                poll_interval=poll_interval,
            )
            if status is not None:
                return status
            last = self._get_order_status_once(order_id)
            if last and last in stop_states:
                return last
            if not connected:
                if not logged_unavailable:
                    logger.warning(unavailable_log, order_id)
                    logged_unavailable = True
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return last
                polled = self._wait_for_order_status_via_polling(
                    order_id, remaining, poll_interval,
                    stop_states=stop_states,
                )
                return polled if polled is not None else last
            if not slice_stream:
                return last

    def _wait_for_order_status_via_stream(
        self, order_id: str, timeout_seconds: float, *,
        stop_states: frozenset,
        poll_interval: float = 1.0,
    ) -> tuple[str | None, bool]:
        """Block until Alpaca's `trade_updates` websocket reports an event
        for `order_id` whose status is in `stop_states`, or `timeout_seconds`
        elapses. With `stop_states=_ORDER_TERMINAL_STATES` this is the
        original fill wait (PR #287); with the terminal set plus `new`, it
        is the "has the venue acknowledged it yet" wait that gates a replace.

        Returns `(status, connected)`:
          - `(status, True)` — a terminal event for this order arrived;
            `status` is one of `_ORDER_TERMINAL_STATES`.
          - `(None, True)` — the stream connected and ran cleanly for the
            full window but nothing terminal arrived for this order (it is
            genuinely still open, or belongs to a different account feed
            entirely — either way the stream itself is not at fault).
          - `(None, False)` — the stream could not be used at all: the
            `alpaca-py` streaming extra is not installed, or the websocket
            never reached a running/authenticated state within the window.
            Callers must fall back to REST polling on this outcome, not on
            `(None, True)`.

        Runs Alpaca's own `TradingStream` (the same class its own docs
        recommend for order-fill notification instead of polling) on a
        background thread via its public `run()`/`stop()` API. Handshake
        retries are guarded (`_install_trading_stream_reconnect_guard`) so
        an HTTP 429 cannot tight-loop. Only the process that holds the
        account-wide lease may open a socket (`_TradeUpdatesLease` plus
        `_TRADE_UPDATES_STREAM_LOCK`). A second process, or a second
        concurrent wait that lost the slot, falls through to REST polling
        rather than opening another socket. Same-process fill waits attach
        to the kept hub. Never raises: any failure here is reported as
        `(None, False)` so the caller's fallback path is the only thing
        that can fail loudly.

        The wait itself is the caller's timeout — the REST path's ceiling.
        Auth leftover is not added on top. A dead hub does not sit out a
        second full window before REST.
        """
        # Belt and braces with `_wait_for_order_status`'s own gate: this is
        # the ONLY function that can construct a TradingStream or take the
        # account lease for a fill wait, so the "never opens a socket"
        # guarantee is enforced here too and does not depend on every
        # caller routing through the wrapper above.
        if not self.fill_stream_enabled():
            return None, False
        if TradingStream is None:
            return None, False

        hub = getattr(self, "_trade_hub", None)
        if hub is not None and hub.is_alive():
            return hub.wait(
                order_id, timeout_seconds, stop_states=stop_states,
                poll_interval=poll_interval,
            )

        # A kept hub that died still owns the account slot until
        # stop_trade_updates releases it. Do not handshake a second socket
        # into that slot — REST for this wait.
        if hub is not None or getattr(self, "_trade_slot_held", False):
            logger.info(
                "trade_updates hub unusable — REST polling for %s",
                order_id,
            )
            return None, False

        # Non-blocking: fill monitoring continues on REST for the waiter
        # that lost the race, instead of stacking a second websocket onto
        # the one-connection slot (that is the 429 storm).
        if not self._acquire_trade_updates_slot():
            hub = getattr(self, "_trade_hub", None)
            if hub is not None and hub.is_alive():
                return hub.wait(
                    order_id, timeout_seconds, stop_states=stop_states,
                    poll_interval=poll_interval,
                )
            logger.info(
                "trade_updates stream already in use — REST polling for %s",
                order_id,
            )
            return None, False

        try:
            return self._wait_for_order_status_via_stream_locked(
                order_id, timeout_seconds, stop_states=stop_states,
            )
        finally:
            self._release_trade_updates_slot()

    def _wait_for_order_status_via_stream_locked(
        self, order_id: str, timeout_seconds: float, *,
        stop_states: frozenset,
    ) -> tuple[str | None, bool]:
        result: dict = {"status": None}
        matched = threading.Event()
        connected = threading.Event()
        auth_wake = threading.Event()
        match_wake = threading.Event()
        run_error: list[Exception] = []
        stream = TradingStream(self.api_key, self.secret_key, paper=self._paper)
        stream._qamc_authed = threading.Event()
        _orig_authed_set = stream._qamc_authed.set

        def _authed_set() -> None:
            _orig_authed_set()
            auth_wake.set()

        stream._qamc_authed.set = _authed_set  # type: ignore[method-assign]
        _install_trading_stream_auth_diagnostics(stream)
        _install_trading_stream_reconnect_guard(stream)

        async def _handler(update) -> None:
            try:
                connected.set()
                auth_wake.set()
                order = getattr(update, "order", None)
                if str(getattr(order, "id", "") or "") != str(order_id):
                    return
                status = str(getattr(getattr(order, "status", None), "value",
                                     getattr(order, "status", ""))).lower()
                if status in stop_states:
                    result["status"] = status
                    matched.set()
                    match_wake.set()
                    await stream.stop_ws()
                record_guarded_pass(self._state, "trade_stream.order_fill_handler", context={})
            except Exception as exc:
                record_guarded_pass(self._state, "trade_stream.order_fill_handler", exc, log=logger,
                                    context={"order": order_id, "effect": "a fill update was dropped; wait falls back to REST"})

        try:
            stream.subscribe_trade_updates(_handler)
            record_guarded_pass(self._state, "trade_stream.order_fill_subscribe", context={})
        except Exception as exc:
            record_guarded_pass(self._state, "trade_stream.order_fill_subscribe", exc, log=logger,
                                context={"order": order_id, "effect": "no stream; caller uses REST"})
            return None, False

        finished = threading.Event()

        def _run() -> None:
            try:
                stream.run()
            except Exception as exc:
                run_error.append(exc)
            finally:
                finished.set()
                auth_wake.set()
                match_wake.set()

        thread = threading.Thread(
            target=_run, name=f"order-fill-stream-{order_id}", daemon=True,
        )
        started = time.monotonic()
        deadline = started + max(0.0, float(timeout_seconds))
        thread.start()
        handshake_hook = callable(getattr(stream, "_start_ws", None))
        if handshake_hook:
            auth_left = min(
                _ALPACA_STREAM_AUTH_DEADLINE_S,
                max(0.0, deadline - time.monotonic()),
            )
            auth_wake.wait(timeout=auth_left)
            authed = bool(stream._qamc_authed.is_set() or connected.is_set())
            if not authed:
                logger.warning(
                    "trade_updates websocket did not authenticate within %.1fs — "
                    "falling back to REST for %s",
                    auth_left, order_id,
                )
                self._last_stream_warmup = TradeStreamWarmup(
                    ready=False, handshake_failed=True, retried=True,
                )
                try:
                    stream.stop()
                    record_guarded_pass(self._state, "trade_stream.order_fill_stream.stop_unauthed", context={})
                except Exception as exc:
                    record_guarded_pass(self._state, "trade_stream.order_fill_stream.stop_unauthed", exc, log=logger, context={"effect": "stream may linger; thread joined regardless"})
                thread.join(timeout=2.0)
                return None, False
        remaining = max(0.0, deadline - time.monotonic())
        match_wake.wait(timeout=remaining)
        try:
            stream.stop()
            record_guarded_pass(self._state, "trade_stream.order_fill_stream.stop_final", context={})
        except Exception as exc:
            record_guarded_pass(self._state, "trade_stream.order_fill_stream.stop_final", exc, log=logger, context={"effect": "stream may linger; daemon thread"})
        thread.join(timeout=5.0)

        if not matched.is_set() and run_error and not connected.is_set():
            # Never reached a live, authenticated connection — treat as
            # "stream unusable", not "order still open".
            return None, False
        return result["status"], True
