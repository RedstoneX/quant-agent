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
from src.trading_calendar import session_date_key
from pathlib import Path
import asyncio
import json
import logging
import random
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


# Fallback reconnect bounds, used only when the installed TradingStream
# does not expose `_reconnect_min_backoff` / `_reconnect_max_backoff`.
# They are alpaca-py's own values for this exact storm (TradingStream.__init__
# on versions that shipped `reconnect_delay`), not a desk-invented constant.
_ALPACA_STREAM_RECONNECT_MIN_S = 1.0
_ALPACA_STREAM_RECONNECT_MAX_S = 30.0

# Handshake ceiling for trade_updates auth. Reuses alpaca-py's own reconnect
# max so we do not invent a second clock. Auth must not consume a fill /
# funding timeout (measured 2026-09-16: ~4 min of `_auth` retries after
# Risk approved, before the first BUY hit the tape).
_ALPACA_STREAM_AUTH_DEADLINE_S = _ALPACA_STREAM_RECONNECT_MAX_S

# THE trade_updates AUTH FORMAT, AND WHY THIS MODULE SENDS THE FRAME.
#
# alpaca-py builds the auth payload itself, in
# `alpaca/trading/stream.py::TradingStream._auth`, as
#   {"action":"authenticate","data":{"key_id":K,"secret_key":S}}
# Alpaca's own authorization reply says that form is being DEPRECATED in
# favour of
#   {"action":"auth","key":K,"secret":S}
# Measured against the live paper broker 2026-09-18: both forms return
# `status: authorized`, and the deprecated one carries the notice.
#
# A dependency bump would be the right fix and there is nothing to bump to:
# alpaca-py 0.44.0, the newest release on PyPI on that date, still sends the
# deprecated form. So the choice is between sending a frame the counterparty
# has told us it is retiring, or sending the current one ourselves. The desk
# sends the current one, on the smallest possible surface: ONE frame, with
# alpaca-py's own verdict rule (`data.status == "authorized"`) unchanged, on
# a per-instance wrapper. Nothing in site-packages is edited.
#
# THE FALLBACK IS LOAD-BEARING AND STAYS. The current form is proven on
# `paper-api.alpaca.markets` and nowhere else; the desk has exactly one
# account and cannot show it is accepted everywhere the old one is. So a
# refusal of the current form marks the stream and the NEXT handshake sends
# the deprecated form on a fresh socket — see `_fell_back_to_deprecated_auth`
# for why it is not re-sent on the same socket. Either way the reconnect
# ceiling bounds the sequence, the owner is told once in plain English, and
# fills fall back to the bounded REST path. Retire the fallback only when
# the current form is proven on every host the desk authenticates against.
_STREAM_AUTH_DEPRECATION_MARKER = "deprecat"
_stream_auth_deprecation_logged = False
_stream_current_auth_format_logged = False


# ---------------------------------------------------------------------------
# Reconnect CEILINGS. Why these exist (2026-09-18 audit of the retained logs).
#
# On 2026-09-15 this socket logged 32,896 handshake attempts in ONE day
# [measured: `grep -c "starting trading websocket connection"` across
# quant_agent.log.2/.3], 32,666 of which Alpaca rejected with HTTP 429
# [measured, same grep on "restarting connection: server rejected WebSocket
# connection: HTTP 429"]. The first twelve attempts are stamped
# 13:38:30.698 -> 13:38:31.076 — about THIRTY handshakes per second.
#
# The retry loop was NOT ours. `alpaca.trading.stream.TradingStream.
# _run_forever` (alpaca-py 0.43.5, the installed version) catches every
# exception and closes with `finally: await asyncio.sleep(0.01)` — a flat
# 10ms, no backoff of any kind, and no attempt ceiling. That is why six
# previous pull requests adjusted OUR timing and changed nothing: our
# timing was never in that loop.
#
# `_install_trading_stream_reconnect_guard` (below) fixed the RATE on
# 2026-09-18 and is measurably working: the 2026-09-17 14:24:54 burst is
# spaced 0.9s, 1.3s, 3.3s, 7.8s, 14.2s, 30.0s, and the whole day logged 50
# attempts instead of 32,896 [measured]. What it did NOT add is a ceiling.
# The attempt counter is a closure local, so it resets to zero on every new
# hub, and a socket that can never authenticate retries at the 30s cap
# forever. That is still an unbounded account-level liability, just a
# slower one. These three constants close it.
# ---------------------------------------------------------------------------

#: Handshake attempts one socket session may spend before it gives up for good.
#: SOURCE (derived, not chosen): the equal-jitter curve below runs off
#: alpaca-py's own reconnect bounds, 1.0s min / 30.0s max, so the capped
#: term goes 1, 2, 4, 8, 16, 30 — it SATURATES at attempt 6. Past saturation
#: every further attempt waits the identical interval and has already failed
#: identically, so it can learn nothing new; it only spends the account's
#: rate-limit budget. Cross-checked against the measured 2026-09-17 14:24
#: burst, which reached the 30s plateau at attempt 6.
_STREAM_ATTEMPT_CEILING_PER_SESSION = 6

#: Handshake attempts this PROCESS may spend across all sessions in one day.
#: SOURCE: Alpaca publishes the trading API limit as "200 requests per
#: minute, per account" (alpaca.markets/support/usage-limit-api-calls). The
#: limit is account-wide, so a websocket storm spends the same budget the
#: order path needs. One single minute's published allowance is therefore
#: the whole DAY's budget for this socket, which is optional comfort — the
#: bounded REST fill path does the same job more slowly. Cross-checked
#: against measured behaviour so it cannot fire spuriously: 2026-09-16 spent
#: 56 attempts and 2026-09-17 spent 50, both comfortably inside it, while
#: the 2026-09-15 storm of 32,896 is 164x over it.
_STREAM_ATTEMPT_CEILING_PER_DAY = 200

#: Stand-down after the broker answers HTTP 429, in seconds.
#: SOURCE: Alpaca states the limit as 200 requests per MINUTE. A retry
#: inside the same minute that produced the 429 is asking the identical
#: question of the identical exhausted window, so it cannot succeed — it can
#: only deepen the throttle. A rate-limit rejection therefore stands down
#: for the full published window rather than the 30s TRANSPORT cap, which is
#: sized for a dropped connection and is the wrong instrument here. A
#: server-sent Retry-After always wins over this when it is longer.
_STREAM_RATE_LIMIT_STAND_DOWN_S = 60.0


class _StreamAttemptBudget:
    """Process-wide, day-keyed ceiling on trade_updates handshake attempts.

    Deliberately module-level rather than per-hub. The 2026-09-18 backoff
    guard kept its attempt counter in a closure, so every new hub started
    again from zero and nothing ever accumulated across a day — which is
    exactly how an unbounded loop hides behind a bounded-looking one.

    Fail-CLOSED on the socket, not on the desk: exhausting the budget shuts
    the OPTIONAL fast path and leaves the bounded REST fill-confirmation
    path, which is what runs whenever this socket is off anyway.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._day: str | None = None
        self._attempts = 0
        self._alerted_day: str | None = None

    def _roll(self, today: str) -> None:
        if self._day != today:
            self._day = today
            self._attempts = 0

    def record_attempt(self, today: str | None = None) -> int:
        """Count one handshake failure; return attempts spent today."""
        day = today or session_date_key()
        with self._lock:
            self._roll(day)
            self._attempts += 1
            return self._attempts

    def day_exhausted(self, today: str | None = None) -> bool:
        day = today or session_date_key()
        with self._lock:
            self._roll(day)
            return self._attempts >= _STREAM_ATTEMPT_CEILING_PER_DAY

    def attempts_today(self, today: str | None = None) -> int:
        day = today or session_date_key()
        with self._lock:
            self._roll(day)
            return self._attempts

    def claim_alert(self, today: str | None = None) -> bool:
        """True exactly ONCE per day, for the caller that should page the owner."""
        day = today or session_date_key()
        with self._lock:
            self._roll(day)
            if self._alerted_day == day:
                return False
            self._alerted_day = day
            return True

    def reset(self) -> None:
        with self._lock:
            self._day = None
            self._attempts = 0
            self._alerted_day = None


#: One budget per process. The socket is account-wide and so is the limit
#: it spends, so a per-broker-instance budget would not bound anything.
_STREAM_ATTEMPT_BUDGET = _StreamAttemptBudget()


def _stream_giveup_owner_message(reason: str) -> str:
    """The owner-facing wording. Plain English, no jargon, no identifiers.

    Says WHAT stopped, WHAT still works, and that nothing is required of
    him — the desk keeps trading either way. He reads this on a phone.
    """
    return (
        "Instant fill alerts switched off for today. "
        f"The broker kept refusing the live connection ({reason}), so the desk "
        "has stopped retrying it to avoid being rate-limited on the account. "
        "Trading is unaffected: fills are being confirmed the slower way "
        "instead, by checking with the broker on a timer. "
        "It retries automatically tomorrow. Nothing for you to do."
    )


def _alert_stream_gave_up(reason: str) -> None:
    """Loud, ONCE a day, on the log and to the owner. Never per attempt."""
    if not _STREAM_ATTEMPT_BUDGET.claim_alert():
        return
    message = _stream_giveup_owner_message(reason)
    logger.error(
        "trade_updates websocket GIVING UP for today after %d handshake "
        "attempts (%s) — falling back to the bounded REST fill path. %s",
        _STREAM_ATTEMPT_BUDGET.attempts_today(), reason, message,
    )
    try:
        from src.notifier import send_owner_alert

        send_owner_alert(message)
    except Exception as exc:  # noqa: BLE001 - never let the alert sink break execution
        record_guarded_pass(None, "trade_stream.give_up_alert", exc, log=logger,
                            context={"effect": "owner not told the stream gave up"})





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
        stream = TradingStream(
            self._broker.api_key, self._broker.secret_key,
            paper=self._broker._paper,
        )
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


class TradeStreamGaveUp(Exception):
    """The socket has stopped retrying for the day and will not reopen.

    Distinct from `TradeStreamAuthRejected`: that says the broker refused
    a credential, this says the DESK refused to keep asking. Callers treat
    it like any other handshake failure and fall through to the bounded
    REST fill path; it exists so the log and the tests can tell "we gave
    up" apart from "it failed again".
    """


class TradeStreamAuthRejected(Exception):
    """The broker REFUSED the trade_updates credential, in the broker's own words.

    Why this class exists (2026-09-18). For a fortnight this failure was
    indistinguishable from a transport fault in the desk's logs, and that
    is what cost six pull requests of connection re-sequencing:

      * The installed SDK's `TradingStream._auth` compares
        `msg["data"]["status"] != "authorized"` and then raises a bare
        `ValueError("failed to authenticate")` — it DISCARDS the reply.
        Alpaca actually sends
        `{"stream":"authorization","data":{"message":"code=401, message=Unauthorized","status":"unauthorized"}}`
        and that `message` never reached any log line.
      * A `ValueError` carries no HTTP-status attribute, so
        `_stream_http_status` returned None and the desk logged
        `status=unknown` — which reads as "the socket would not open",
        not "the password was wrong".

    This exception carries Alpaca's own `message`/`status` plus a
    NON-REVEALING credential fingerprint (length and first two characters
    only). The secret is never touched, and the key is never logged.
    """

    def __init__(
        self,
        *,
        broker_message: str | None,
        broker_status: str | None,
        credential: str | None,
        cause: BaseException | None = None,
    ) -> None:
        self.broker_message = broker_message
        self.broker_status = broker_status
        self.credential_fingerprint = _credential_fingerprint(credential)
        super().__init__(
            "broker refused the trade_updates credential "
            f"(broker said: {broker_message or 'no message returned'}; "
            f"broker status: {broker_status or 'not stated'}; "
            f"api key {self.credential_fingerprint})"
        )
        self.__cause__ = cause


def _credential_fingerprint(credential: str | None) -> str:
    """Length + first two characters of a key. NEVER the value, never a secret.

    Enough to tell a real Alpaca key (26 chars, `PK`/`AK` prefix) from the
    29-character literal containing the word `placeholder` that the process
    actually held until 2026-09-18 — which is the single fact that would
    have ended this investigation on day one. Two characters cannot
    identify an account and cannot be replayed.
    """
    if not credential:
        return "absent (empty)"
    text = str(credential)
    return f"length {len(text)}, starts '{text[:2]}'"


def _parse_stream_auth_reply(raw: object) -> tuple[str | None, str | None]:
    """(message, status) out of Alpaca's authorization frame. Never raises.

    Shape per Alpaca's streaming docs and observed rejections:
    `{"stream":"authorization","data":{"message":...,"status":...}}`.
    Anything unparseable degrades to (None, None) — a missing diagnostic
    must never replace the failure it was added to explain.
    """
    if raw is None:
        return None, None
    payload: object = raw
    if isinstance(raw, (bytes, bytearray)):
        try:
            payload = raw.decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            return None, None
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except Exception:  # noqa: BLE001
            # Not JSON: the body itself is the most informative thing we have.
            text = str(raw).strip()
            return (text[:200] or None), None
    if not isinstance(payload, dict):
        return None, None
    data = payload.get("data")
    if not isinstance(data, dict):
        data = payload
    message = data.get("message")
    status = data.get("status")
    return (
        str(message) if message is not None else None,
        str(status) if status is not None else None,
    )


def _install_trading_stream_auth_diagnostics(stream: object) -> None:
    """Keep Alpaca's authorization reply so a refusal can be logged verbatim.

    Deliberately NOT a monkeypatch of the vendor SDK and NOT a
    reimplementation of the auth protocol: the SDK's own `_auth` still
    sends the frame and still decides the outcome. We only (a) record the
    first frame the socket hands back during auth, via a one-shot wrapper
    on this instance's `recv`, (b) translate the SDK's information-free
    `ValueError` into `TradeStreamAuthRejected` carrying that frame, and
    (c) on SUCCESS, surface any deprecation notice the broker put in that
    same frame instead of discarding it — see
    `_note_stream_auth_deprecation` and `_STREAM_AUTH_DEPRECATION_MARKER`.

    Both wrappers are per-instance attributes on objects this module
    constructed. Nothing in site-packages is edited. No timeout, retry
    count or backoff is introduced — the reconnect guard owns all of those
    and is untouched.

    No-op when the object has no `_auth` (the test doubles).
    """
    original_auth = getattr(stream, "_auth", None)
    if not callable(original_auth):
        return

    async def _send_current_auth_format() -> None:
        """Send the format Alpaca asks for, and apply the SDK's own verdict.

        Identical decision rule to alpaca-py's `_auth` — `data.status` must
        read `authorized`, otherwise `ValueError`, which is what every
        caller and test in this module already expects. Only the frame
        differs, because only the frame is what the broker deprecated.
        """
        ws_now = getattr(stream, "_ws", None)
        await ws_now.send(json.dumps({
            "action": "auth",
            "key": getattr(stream, "_api_key", None),
            "secret": getattr(stream, "_secret_key", None),
        }))
        raw = await ws_now.recv()
        msg = json.loads(raw)
        data = msg.get("data") or {}
        if data.get("status") != "authorized":
            raise ValueError("failed to authenticate")

    async def _auth():
        captured: dict[str, object] = {}
        ws = getattr(stream, "_ws", None)
        original_recv = getattr(ws, "recv", None) if ws is not None else None
        restored = False

        def _restore() -> None:
            nonlocal restored
            if restored or ws is None or original_recv is None:
                return
            restored = True
            try:
                ws.recv = original_recv  # type: ignore[union-attr]
            except Exception as exc:  # noqa: BLE001
                record_guarded_pass(None, "trade_stream.auth_capture.restore_recv", exc, log=logger,
                                    context={"effect": "recv wrapper stays on the socket"})

        if callable(original_recv):
            async def _recv_once():
                raw = await original_recv()
                if "raw" not in captured:
                    captured["raw"] = raw
                    _restore()
                return raw

            try:
                ws.recv = _recv_once  # type: ignore[union-attr]
            except Exception as exc:  # noqa: BLE001
                record_guarded_pass(None, "trade_stream.auth_capture.wrap_recv", exc, log=logger,
                                    context={"effect": "auth reply not captured"})
                original_recv = None

        # WHICH FRAME THIS HANDSHAKE SENDS. New form unless a previous
        # handshake on THIS stream was refused with it — see
        # `_fell_back_to_deprecated_auth`.
        use_deprecated = bool(getattr(stream, "_qamc_auth_fallback", False))
        if getattr(stream, "_ws", None) is None:
            # No socket to send our own frame on. Hand the whole handshake
            # back to the vendor rather than raise a shape error the
            # reconnect guard would report as a broker fault.
            use_deprecated = True
        attempt_auth = original_auth if use_deprecated else _send_current_auth_format
        try:
            # BOUND THE HANDSHAKE. alpaca-py's `_auth` awaits `self._ws.recv()`
            # with NO timeout (verified in 0.43.5 and 0.44.0), while its own
            # `_consume` bounds the identical call at 5s. So a broker that
            # stops ANSWERING — which is one way the deprecated auth format
            # could be retired — parks this thread in `_auth` forever: no
            # exception, so the reconnect guard never counts a failure, the
            # session ceiling never engages, the owner is never told, and
            # `trade_updates_started()` keeps reporting a live hub. Fill waits
            # would still fall to REST on their own deadline, so the desk
            # survives; the socket would just lie about being up.
            #
            # Same budget the hub already gives the handshake
            # (`_ALPACA_STREAM_AUTH_DEADLINE_S`), not a second clock. A
            # timeout raises out of `_start_ws`, which is exactly the shape
            # the ceiling already counts and gives up on.
            await asyncio.wait_for(
                attempt_auth(), timeout=_ALPACA_STREAM_AUTH_DEADLINE_S,
            )
        except asyncio.TimeoutError as exc:
            message, status = _parse_stream_auth_reply(captured.get("raw"))
            raise TradeStreamAuthRejected(
                broker_message=(
                    message
                    or "no reply to the authentication frame within "
                       f"{_ALPACA_STREAM_AUTH_DEADLINE_S:.0f}s"
                ),
                broker_status=status or "no reply",
                credential=getattr(stream, "_api_key", None),
                cause=exc,
            ) from exc
        except ValueError as exc:
            if not use_deprecated:
                # The CURRENT format was refused. Do NOT re-send on this
                # socket: Alpaca closes a connection it refused, so a second
                # frame here would fail for a reason that has nothing to do
                # with the format and would look like a credential problem.
                # Mark the stream instead; the reconnect guard's next
                # handshake opens a fresh socket and sends the deprecated
                # form, which is the one measured to work today. The ceiling
                # still bounds the whole sequence.
                _fell_back_to_deprecated_auth(stream, captured.get("raw"))
            message, status = _parse_stream_auth_reply(captured.get("raw"))
            raise TradeStreamAuthRejected(
                broker_message=message,
                broker_status=status,
                credential=getattr(stream, "_api_key", None),
                cause=exc,
            ) from exc
        else:
            if not use_deprecated:
                _note_current_auth_format_accepted()
            _note_stream_auth_deprecation(captured.get("raw"))
        finally:
            _restore()

    stream._auth = _auth


def _fell_back_to_deprecated_auth(stream: object, raw: object) -> None:
    """Mark a stream so its NEXT handshake sends the deprecated auth frame.

    Loud, because this is the fail-visible half of the migration: the desk
    is now sending a format the broker has said it is retiring, and the
    only alternative to saying so is silently degrading.

    Marked per-stream rather than per-process: a refusal is evidence about
    the socket in front of us, and a process-wide latch would pin every
    later socket to the old form on one bad handshake.
    """
    try:
        setattr(stream, "_qamc_auth_fallback", True)
    except Exception as exc:  # noqa: BLE001
        record_guarded_pass(None, "trade_stream.auth_fallback.mark", exc, log=logger,
                            context={"effect": "fallback flag not set"})
        return
    message, status = _parse_stream_auth_reply(raw)
    logger.warning(
        "trade_updates auth: the CURRENT format {\"action\":\"auth\"} was "
        "refused (broker said: %s; broker status: %s) — the next handshake "
        "falls back to the deprecated format on a fresh socket. If the "
        "credential is good, this means the current format is not accepted "
        "here and the fallback is load-bearing.",
        message or "no message returned",
        status or "not stated",
    )


def _note_current_auth_format_accepted() -> None:
    """Record, once per process, that the non-deprecated frame was accepted.

    Without this line there is no positive evidence in the desk's own log
    that the migration took — only the absence of a failure, which is what
    let a socket that never authenticated look healthy for three days.
    """
    global _stream_current_auth_format_logged
    if _stream_current_auth_format_logged:
        return
    _stream_current_auth_format_logged = True
    logger.info(
        "trade_updates authenticated with the CURRENT auth format "
        "({\"action\":\"auth\"}) — the deprecated format alpaca-py builds "
        "was not used",
    )


def _note_stream_auth_deprecation(raw: object) -> None:
    """Log Alpaca's deprecation notice once per process, in its own words.

    The authorization reply to a SUCCESSFUL handshake was captured for the
    refusal path and then thrown away, so the broker telling us our auth
    format is going away reached the desk and left no trace. That is a
    silent degradation waiting to happen: the day Alpaca enforces it, the
    only record would be a handshake that stopped working.

    WARNING, not ERROR: nothing is broken yet and nothing is required of
    anyone today. Once per process, never per attempt — the 2026-09-15
    storm is what a per-attempt line costs. Never raises, never carries a
    credential (the reply frame contains neither).
    """
    global _stream_auth_deprecation_logged
    if _stream_auth_deprecation_logged:
        return
    try:
        message, _status = _parse_stream_auth_reply(raw)
    except Exception as exc:  # noqa: BLE001 - a diagnostic must not break the handshake
        record_guarded_pass(None, "trade_stream.auth_deprecation.parse", exc, log=logger,
                            context={"effect": "deprecation notice not logged"})
        return
    if not message or _STREAM_AUTH_DEPRECATION_MARKER not in message.lower():
        return
    _stream_auth_deprecation_logged = True
    logger.warning(
        "trade_updates auth format is DEPRECATED by the broker — broker "
        "said: %s. The payload is built by alpaca-py "
        "(TradingStream._auth), not by this desk, and the newest release "
        "still sends the old form; the handshake is accepted today. See "
        "_STREAM_AUTH_DEPRECATION_MARKER in this module.",
        message,
    )


def _equal_jitter_backoff(attempt: int, min_backoff: float, max_backoff: float) -> float:
    """Equal-jitter exponential backoff.

    Same shape as `alpaca.common.utils.reconnect_delay`, which exists to
    stop this exact reconnect/HTTP 429 storm. Copied so an older alpaca-py
    that still sleeps 10ms in `_run_forever` still backs off.
    """
    if min_backoff <= 0:
        min_backoff = _ALPACA_STREAM_RECONNECT_MIN_S
    if max_backoff < min_backoff:
        max_backoff = min_backoff
    capped = min_backoff
    for _ in range(max(0, int(attempt) - 1)):
        if capped >= max_backoff / 2:
            capped = max_backoff
            break
        capped *= 2
    capped = min(max_backoff, capped)
    return capped / 2 + random.uniform(0, capped / 2)


def _trading_stream_reconnect_delay(
    attempt: int, exc: BaseException, stream: object | None = None,
) -> float:
    """Seconds to wait before the next trade_updates handshake.

    Prefer the server's Retry-After. Otherwise the stream's own reconnect
    bounds (alpaca-py's fix for this storm), else that library's documented
    1s/30s equal-jitter curve.
    """
    hint = _stream_retry_after_seconds(exc)
    rate_limited = _stream_http_status(exc) == 429
    if hint is not None:
        # A 429 never waits LESS than the published rate-limit window, even
        # when the server asks for less: retrying inside the window that
        # produced it cannot clear it. See _STREAM_RATE_LIMIT_STAND_DOWN_S.
        return max(hint, _STREAM_RATE_LIMIT_STAND_DOWN_S) if rate_limited else hint
    if rate_limited:
        # Rate limiting is an ACCOUNT-level fault, not a transport one. The
        # transport curve below is sized for a dropped connection and is the
        # wrong instrument: retrying fast is precisely what produced the
        # 32,666 rejections of 2026-09-15.
        return _STREAM_RATE_LIMIT_STAND_DOWN_S
    min_b = float(getattr(stream, "_reconnect_min_backoff", 0) or 0) if stream else 0.0
    max_b = float(getattr(stream, "_reconnect_max_backoff", 0) or 0) if stream else 0.0
    if min_b <= 0:
        min_b = _ALPACA_STREAM_RECONNECT_MIN_S
    if max_b < min_b:
        max_b = _ALPACA_STREAM_RECONNECT_MAX_S
    return _equal_jitter_backoff(max(1, int(attempt)), min_b, max_b)


def _install_trading_stream_reconnect_guard(stream: object) -> None:
    """Stop older alpaca-py TradingStream clients tight-looping on HTTP 429.

    Public `run()`/`stop()` stay the entry points. We wrap `_start_ws` so a
    failed handshake waits (Retry-After, else equal-jitter backoff) before
    the SDK's `_run_forever` retries. Newer alpaca-py already waits in
    `_wait_before_reconnect`; wrapping then would double the delay, so we
    only insert the sleep when that helper is missing. Logging is throttled
    to once per backoff interval either way.

    No-op when the object has no `_start_ws` (the test double).
    """
    original_start = getattr(stream, "_start_ws", None)
    if not callable(original_start):
        return
    original_stop_ws = getattr(stream, "stop_ws", None)
    sdk_backs_off = callable(getattr(stream, "_wait_before_reconnect", None))
    failures = 0
    last_log_mono = 0.0

    async def stop_ws() -> None:
        event = getattr(stream, "_reconnect_stop", None)
        if event is None:
            event = asyncio.Event()
            setattr(stream, "_reconnect_stop", event)
        event.set()
        if callable(original_stop_ws):
            await original_stop_ws()

    async def start_ws():
        nonlocal failures, last_log_mono
        event = getattr(stream, "_reconnect_stop", None)
        if event is None:
            event = asyncio.Event()
            setattr(stream, "_reconnect_stop", event)
        if _STREAM_ATTEMPT_BUDGET.day_exhausted():
            # The day's budget is already gone, so do not spend a handshake
            # to rediscover that. Checked BEFORE the attempt: without this a
            # new hub still costs one rejection every time it opens, which
            # is how a "bounded" loop stays unbounded in aggregate.
            try:
                setattr(stream, "_should_run", False)
            except Exception as exc:  # noqa: BLE001
                record_guarded_pass(None, "trade_stream.reconnect.stop_flag_exhausted", exc, log=logger,
                                    context={"effect": "retry loop may keep running"})
            event.set()
            _alert_stream_gave_up("the daily retry budget is spent")
            raise TradeStreamGaveUp(
                "trade_updates retry budget for today is spent "
                f"({_STREAM_ATTEMPT_CEILING_PER_DAY} handshake attempts) — "
                "fills are confirmed by the bounded REST path"
            )
        try:
            await original_start()
            failures = 0
            # The SDK's own post-auth line (`connected to: wss...`) is on the
            # alpaca logger, which the desk's log does not carry — so across
            # all retained production logs there is no line that says the
            # socket ever worked, only 1,017 that say it did not. This is the
            # desk's own affirmative record of a successful handshake.
            logger.info(
                "trade_updates websocket authenticated (endpoint=%s)",
                getattr(stream, "_endpoint", "unknown"),
            )
            authed = getattr(stream, "_qamc_authed", None)
            if authed is not None:
                try:
                    authed.set()
                except Exception:
                    pass
        except Exception as exc:
            authed = getattr(stream, "_qamc_authed", None)
            if authed is not None:
                try:
                    authed.clear()
                except Exception:
                    pass
            connected = getattr(stream, "_qamc_connected", None)
            if connected is not None:
                try:
                    connected.clear()
                except Exception:
                    pass
            failures += 1
            spent_today = _STREAM_ATTEMPT_BUDGET.record_attempt()
            status = _stream_http_status(exc)
            # CEILINGS. Either one being reached ends the socket for good --
            # the session ceiling because the backoff curve has saturated and
            # further attempts cannot learn anything new, the daily ceiling
            # because the account's published rate-limit budget belongs to
            # the order path. Both are checked BEFORE the sleep so an
            # exhausted budget never buys another wait.
            if (
                failures >= _STREAM_ATTEMPT_CEILING_PER_SESSION
                or _STREAM_ATTEMPT_BUDGET.day_exhausted()
            ):
                if isinstance(exc, TradeStreamAuthRejected):
                    reason = "it rejected our credential"
                elif status == 429:
                    reason = "it rate-limited us"
                else:
                    reason = "the connection would not open"
                # Stop the SDK's own loop. `TradingStream._run_forever`
                # re-checks `_should_run` at the top of every iteration and
                # returns when it is false, so this ends the retry loop
                # without abandoning the SDK's public run()/stop() contract.
                try:
                    setattr(stream, "_should_run", False)
                except Exception as exc:  # noqa: BLE001
                    record_guarded_pass(None, "trade_stream.reconnect.stop_flag_giveup", exc, log=logger,
                                        context={"effect": "retry loop may keep running"})
                event.set()
                logger.warning(
                    "trade_updates websocket give-up: %d attempts this "
                    "session (ceiling %d), %d today (ceiling %d), last "
                    "status=%s",
                    failures, _STREAM_ATTEMPT_CEILING_PER_SESSION,
                    spent_today, _STREAM_ATTEMPT_CEILING_PER_DAY,
                    status if status is not None else "unknown",
                )
                _alert_stream_gave_up(reason)
                raise
            delay = _trading_stream_reconnect_delay(failures, exc, stream)
            now = time.monotonic()
            # Log at most once per wait: a 10ms loop otherwise reprints the
            # same HTTP 429 thousands of times during one fill window.
            if last_log_mono == 0.0 or now - last_log_mono >= delay:
                if isinstance(exc, TradeStreamAuthRejected):
                    # An application-level credential refusal, NOT a
                    # handshake/transport fault. Reporting it as
                    # "handshake failed (status=unknown)" is what sent six
                    # previous pull requests at the connection's timing.
                    logger.warning(
                        "trade_updates authentication REJECTED by broker — "
                        "broker said: %s (broker status: %s; api key %s); "
                        "attempt %d, reconnect in %.1fs",
                        exc.broker_message or "no message returned",
                        exc.broker_status or "not stated",
                        exc.credential_fingerprint,
                        failures, delay,
                    )
                else:
                    logger.warning(
                        "trade_updates websocket handshake failed "
                        "(status=%s, attempt %d); reconnect in %.1fs",
                        status if status is not None else "unknown",
                        failures, delay,
                    )
                last_log_mono = now
            if not sdk_backs_off and delay > 0 and getattr(stream, "_should_run", True):
                try:
                    await asyncio.wait_for(event.wait(), timeout=delay)
                except asyncio.TimeoutError:
                    pass
            raise

    stream._start_ws = start_ws
    if callable(original_stop_ws):
        stream.stop_ws = stop_ws


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
