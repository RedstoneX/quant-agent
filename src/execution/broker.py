import asyncio
import functools
import fcntl
import json
import logging
import math
import os
import random
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout
from datetime import date
from dataclasses import dataclass
from pathlib import Path

import yfinance as yf
from alpaca.trading.client import TradingClient
try:
    from alpaca.trading.stream import TradingStream
except ImportError:  # pragma: no cover - optional dependency surface
    TradingStream = None
from alpaca.trading.requests import (
    MarketOrderRequest, LimitOrderRequest, StopLimitOrderRequest,
    StopOrderRequest,
    TakeProfitRequest, StopLossRequest, ReplaceOrderRequest,
)
from alpaca.trading.enums import OrderSide, TimeInForce, OrderClass, QueryOrderStatus

from src.models import Position, _ALLOWED_SECTORS, _SECTOR_ALIASES
# THE stop-value judgement (docs/WORK.md item 88). `src.execution.stop_records`
# imports nothing from this module, so this is a leaf dependency.
from src.execution.stop_records import STOP_USABLE, classify_stop_price
from src.execution.broker_parts.stop_amend import (  # noqa: F401 (re-exports keep patch targets)
    StopAmender, _AMEND_NOT_ATTEMPTED, _is_terminal_broker_rejection, _quantize_price,
)
from src.execution.broker_parts.stop_place import (  # noqa: F401 (re-exports keep patch targets)
    StopPlacer, _STOP_PLACEMENT_MAX_ATTEMPTS, _STOP_PLACEMENT_BACKOFF_S, _FRACTIONAL_QTY_EPSILON, PROTECTIVE_ORDER_ACTIVE_STATUSES, _is_held_for_orders_error, _is_unsupported_stop_market_rejection, _split_protective_qty, _derive_stop_tif, _alpaca_symbol, _internal_symbol, real_broker_order_id,
)
from src.execution.broker_parts.order_desk import (  # noqa: F401 (re-exports keep patch targets)
    OrderDesk, _PLAIN_PRICE_LABELS, _outlier_refusal_detail, _is_terminal_submission_rejection,
)
from src.execution.broker_parts.account_reads import AccountReads

logger = logging.getLogger(__name__)

# `_PLAIN_PRICE_LABELS` moved to src/execution/broker_parts/order_desk.py (re-exported above).


# `_outlier_refusal_detail` moved to src/execution/broker_parts/order_desk.py (re-exported above).

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


class _TradeUpdatesLease:
    """Account-wide exclusive right to open Alpaca's trade_updates websocket.

    flock is released when the fd closes, including process death, so a
    killed job cannot wedge the slot. The pid written into the file is
    diagnostic only — ownership is the lock, not the text. Not a new
    service, not IPC, not a second trading-memory system.
    """

    def __init__(self, path: Path):
        self.path = Path(path)
        self._fh = None

    def acquire(self, *, blocking: bool = False) -> bool:
        if self._fh is not None:
            return True
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fh = open(self.path, "a+")
        except Exception:
            logger.warning(
                "trade_updates lease: could not open %s — refusing to open a socket",
                self.path, exc_info=True,
            )
            return False
        flags = fcntl.LOCK_EX if blocking else fcntl.LOCK_EX | fcntl.LOCK_NB
        try:
            fcntl.flock(fh.fileno(), flags)
        except BlockingIOError:
            fh.close()
            return False
        except Exception:
            logger.warning(
                "trade_updates lease: flock failed on %s — refusing to open a socket",
                self.path, exc_info=True,
            )
            try:
                fh.close()
            except Exception:
                pass
            return False
        try:
            fh.seek(0)
            fh.truncate()
            fh.write(f"{os.getpid()}\n")
            fh.flush()
        except Exception:
            pass
        self._fh = fh
        return True

    def release(self) -> None:
        fh = self._fh
        self._fh = None
        if fh is None:
            return
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        except Exception:
            pass
        try:
            fh.close()
        except Exception:
            pass

    def held(self) -> bool:
        return self._fh is not None

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
        day = today or date.today().isoformat()
        with self._lock:
            self._roll(day)
            self._attempts += 1
            return self._attempts

    def day_exhausted(self, today: str | None = None) -> bool:
        day = today or date.today().isoformat()
        with self._lock:
            self._roll(day)
            return self._attempts >= _STREAM_ATTEMPT_CEILING_PER_DAY

    def attempts_today(self, today: str | None = None) -> int:
        day = today or date.today().isoformat()
        with self._lock:
            self._roll(day)
            return self._attempts

    def claim_alert(self, today: str | None = None) -> bool:
        """True exactly ONCE per day, for the caller that should page the owner."""
        day = today or date.today().isoformat()
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
    except Exception:  # noqa: BLE001 - never let the alert sink break execution
        logger.warning(
            "could not push the trade_updates give-up alert to the owner",
            exc_info=True,
        )


@dataclass(frozen=True)
class LivePrice:
    """A price WITH the provenance needed to decide whether to trust it.

    `get_latest_price` answers "what is it worth" but throws away how it
    knew: a real trade print, or a quote the tape has not confirmed. It also
    never looked at WHEN the trade happened, so a thinly-traded name that
    last printed yesterday, or any read outside market hours, came back
    looking exactly like a live price. Callers that place or move real orders
    need the difference; the reporting callers do not, which is why
    `get_latest_price` keeps its old shape and this is additive.

    `source` is one of `last_trade`, `quote_mid`, `quote_ask`, `quote_bid`.

    Two freshness answers, because two kinds of caller need different
    strictness and collapsing them into one flag would either block ordinary
    trading or wave through yesterday's price:
      - `is_today` — the provider stamped this value with the current ET
        date, whether it is a trade or a quote. A live quote mid-session is
        a legitimate fill reference; yesterday's anything is not.
      - `is_today_print` — additionally a REAL trade print. Only this proves
        the tape actually traded there, which is what deciding where a stop
        belongs requires.
    An unstamped or naive timestamp fails visible rather than passing as
    live — the same rule `src.trading_calendar.live_price_is_today` already
    applies to research snapshots.
    """

    price: float
    source: str
    trade_at: object | None
    is_today: bool
    is_today_print: bool


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
            except Exception:
                logger.warning(
                    "trade_updates hub handler error",
                    exc_info=True,
                )

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
            except Exception:
                pass
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
            except Exception:
                pass
            try:
                stream.stop()
            except Exception:
                pass
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
            except Exception:  # noqa: BLE001
                pass

        if callable(original_recv):
            async def _recv_once():
                raw = await original_recv()
                if "raw" not in captured:
                    captured["raw"] = raw
                    _restore()
                return raw

            try:
                ws.recv = _recv_once  # type: ignore[union-attr]
            except Exception:  # noqa: BLE001
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
    except Exception:  # noqa: BLE001
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
    except Exception:  # noqa: BLE001 - a diagnostic must not break the handshake
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


def _stream_http_status(exc: BaseException) -> int | None:
    """Best-effort HTTP status on a websocket handshake error. Never raises."""
    for attr in ("status_code", "status"):
        val = getattr(exc, attr, None)
        if isinstance(val, int):
            return val
    response = getattr(exc, "response", None)
    if response is not None:
        for attr in ("status_code", "status"):
            val = getattr(response, attr, None)
            if isinstance(val, int):
                return val
    match = re.search(r"\bHTTP\s*429\b|\bstatus(?:\s+code)?\s*[:=]?\s*429\b",
                      str(exc), re.IGNORECASE)
    if match:
        return 429
    return None


def _stream_retry_after_seconds(exc: BaseException) -> float | None:
    """Retry-After from a handshake 429, when the server sent one.

    Numeric seconds only (same restriction as `_retry_after_hint_seconds`
    in src/agents/base.py). The HTTP-date form is not worth parsing here:
    the fill wait already has its own wall-clock ceiling.
    """
    sources = [exc, getattr(exc, "response", None)]
    for src in sources:
        if src is None:
            continue
        headers = getattr(src, "headers", None)
        if headers is None:
            continue
        try:
            raw = headers.get("retry-after") or headers.get("Retry-After")
        except Exception:  # noqa: BLE001
            raw = None
        if raw is None:
            continue
        try:
            hint = float(raw)
        except (TypeError, ValueError):
            continue
        if hint > 0:
            return hint
    match = re.search(
        r'retry[_-]after["\']?\s*[:=]\s*"?(\d+(?:\.\d+)?)',
        str(exc), re.IGNORECASE,
    )
    if match:
        hint = float(match.group(1))
        if hint > 0:
            return hint
    return None


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
            except Exception:  # noqa: BLE001
                pass
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
                except Exception:  # noqa: BLE001
                    pass
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


# Index ETFs that have no single sector — bucket them as "Broad".
_INDEX_ETFS = {"SPY", "QQQ", "IWM", "DIA", "VTI", "VOO", "IVV"}

# Sector / thematic ETFs → their canonical sector bucket.
#
# WHY (2026-07-16 audit): yfinance's `.info` carries no `sector` key for ETFs,
# so _get_sector fell through to "Unknown" for every one of them. Two silent
# failures followed: (1) `max_sector_pct` is gated on `new_sector != "Unknown"`
# (risk/rules.py), so a BUY of XLV/SMH/... skipped the sector cap ENTIRELY;
# (2) a held ETF carries sector="Unknown", so it contributed $0 to the sector
# bucket of a same-sector single name — a book that is 30% XLV would let an
# LLY BUY through as if Healthcare exposure were zero. Both directions of the
# cap were dead for these symbols despite the universe being ~20% ETFs.
#
# Deterministic table, consulted BEFORE the network fetch: an ETF's sector is
# a fact about the product, not something to rediscover per process.
_ETF_SECTORS = {
    # SPDR sector suite
    "XLF": "Financial Services", "XLE": "Energy", "XLV": "Healthcare",
    "XLI": "Industrials", "XLP": "Consumer Defensive", "XLY": "Consumer Cyclical",
    "XLU": "Utilities", "XLRE": "Real Estate", "XLB": "Basic Materials",
    "XLK": "Technology", "XLC": "Communication Services",
    # Semiconductor / AI thematics
    "SMH": "Technology", "SOXX": "Technology", "DRAM": "Technology",
    "CHPX": "Technology",
    # Inverse / leveraged index ETFs track a BROAD index — they have no sector
    # of their own. (Their leverage is handled separately by the signed/gross
    # multipliers in risk/rules.py.)
    "SH": "Broad", "SDS": "Broad", "PSQ": "Broad", "SQQQ": "Broad",
}

# Default HTTP timeout for ALL Alpaca SDK calls (connect, read).
# Without this, a stalled TCP connection to the broker can hang the process
# for hours under launchd — observed 2026-04-17 when the evening job sat for
# 13+ hours at the very first broker call.
_BROKER_HTTP_TIMEOUT = 30.0
_SECTOR_LOOKUP_TIMEOUT_S = 10  # per-symbol ceiling on yfinance .info hang in _get_sector

# 2026-09-10: 15 -> 30 -> 90. `wait_for_order_terminal` now watches Alpaca's
# real-time trade_updates stream first (see that method) — a fill is
# detected the instant Alpaca reports it, not on the next poll tick, so this
# number no longer trades speed against safety in the common case. It is
# now purely the ceiling for the RARE path where the stream itself could
# not be used (import/auth/network failure) and the code falls back to REST
# polling exactly as before. 90 seconds is the originally-researched value
# (an ordinary marketable limit on a liquid US equity fills in seconds, but
# a stale/illiquid DAY order should be given real room before being pulled)
# — it was never actually shipped because the fallback-only framing didn't
# exist yet. Bounded well below a stale DAY order regardless. Later entries
# in a submission burst have already rested while earlier entries are
# finalized, so this is a conservative ceiling, not a blind per-order sleep
# added to every order.
_ENTRY_FILL_TIMEOUT_S = 90.0

# `_STOP_PLACEMENT_MAX_ATTEMPTS` moved to src/execution/broker_parts/stop_place.py (re-exported above).
# `_STOP_PLACEMENT_BACKOFF_S` moved to src/execution/broker_parts/stop_place.py (re-exported above).

# `_FRACTIONAL_QTY_EPSILON` moved to src/execution/broker_parts/stop_place.py (re-exported above).


# `_AMEND_NOT_ATTEMPTED` moved to src/execution/broker_parts/stop_amend.py (re-exported above).


# `_is_held_for_orders_error` moved to src/execution/broker_parts/stop_place.py (re-exported above).


# `_is_terminal_broker_rejection` moved to src/execution/broker_parts/stop_amend.py (re-exported above).


# `_is_terminal_submission_rejection` moved to src/execution/broker_parts/order_desk.py (re-exported above).


# `_is_unsupported_stop_market_rejection` moved to src/execution/broker_parts/stop_place.py (re-exported above).


# `_split_protective_qty` moved to src/execution/broker_parts/stop_place.py (re-exported above).


# `_derive_stop_tif` moved to src/execution/broker_parts/stop_place.py (re-exported above).


# `_alpaca_symbol` moved to src/execution/broker_parts/stop_place.py (re-exported above).


# `_internal_symbol` moved to src/execution/broker_parts/stop_place.py (re-exported above).


# `_quantize_price` moved to src/execution/broker_parts/stop_amend.py (re-exported above).


def _install_http_timeout(client, timeout: float = _BROKER_HTTP_TIMEOUT) -> None:
    """Inject a default timeout on an Alpaca SDK client's underlying requests.Session.

    The SDK (alpaca-py 0.43.2) uses a requests.Session with no default timeout; each
    call goes through RESTClient._one_request which just forwards opts. This patches
    session.request to set timeout=30s if the caller didn't specify one.
    """
    session = getattr(client, "_session", None)
    if session is None or getattr(session, "_quant_timeout_patched", False):
        return
    original_request = session.request

    def _request_with_timeout(method, url, **kwargs):
        kwargs.setdefault("timeout", timeout)
        return original_request(method, url, **kwargs)

    session.request = _request_with_timeout
    session._quant_timeout_patched = True

# Cache sector lookups to avoid repeated API calls
_sector_cache: dict[str, str] = {}
_sector_lock = threading.Lock()

# WHY (2026-09-01 audit): a symbol whose sector never resolves reads
# identically to one with no exception at all — both come back "Unknown"
# from `_get_sector` with no further detail. That is fine for the two
# existing consumers (they only needed a sector string), but it is not
# enough for an owner-facing alert: "the network is having a bad day, this
# will self-heal" and "this instrument has no sector to find" are different
# situations and should not read the same. Best-effort, advisory only —
# NOT part of the caching contract above (an unresolved symbol is still
# never cached; see `_get_sector`'s docstring), keyed the same way as
# `_sector_cache`, and simply absent/stale when `_get_sector` itself is
# mocked out wholesale (tests) — `_sector_resolution_status_for` defaults
# to "unknown_reason" rather than guessing.
_sector_resolution_status: dict[str, str] = {}


def _sector_resolution_status_for(symbol: str) -> str:
    """Best-effort reason the last `_get_sector(symbol)` call in THIS
    process came back "Unknown". One of:

      "resolved"       - moot; the symbol has a real sector.
      "lookup_failed"  - network error, timeout, or an empty response with
                          no error — yfinance returning nothing for a real
                          symbol is usually transient (see
                          test_sector_canonicalization.py). Will retry.
      "no_sector"      - the fetch itself succeeded and returned real data,
                          just no `sector` field — this symbol may
                          genuinely be unclassifiable (e.g. an ETF outside
                          `_ETF_SECTORS`), not a network problem.
      "unknown_reason" - no attempt recorded yet for this symbol in this
                          process (fresh process, or a test/caller mocked
                          `_get_sector` directly instead of going through
                          the real fetch below).
    """
    with _sector_lock:
        return _sector_resolution_status.get(symbol, "unknown_reason")


def _canonicalize_sector(raw: str | None) -> str:
    """Normalize yfinance / LLM sector strings to the 12-value canonical enum.

    Returns "Unknown" for anything that can't be mapped — callers must decide
    whether to skip or fall back. The MacroAnalysis pydantic model uses the
    same alias table to self-heal LLM output.
    """
    if not raw:
        return "Unknown"
    s = str(raw).strip()
    if s in _ALLOWED_SECTORS:
        return s
    canon = _SECTOR_ALIASES.get(s.lower())
    if canon in _ALLOWED_SECTORS:
        return canon
    return "Unknown"


def _get_sector(symbol: str) -> str:
    """Look up sector for a symbol using yfinance. Thread-safe, cached per process.

    Output is canonicalized to the 12-value MacroSectorGuidance enum (or "Unknown"
    for un-classifiable names), so macro sector_guidance and position.sector share
    a namespace.

    Caching policy: only KNOWN sectors are cached. "Unknown" is returned but
    NOT cached, so a transient yfinance outage gets re-diagnosed on every
    call instead of freezing a stale verdict. Codex r11 P1: a one-shot
    lookup miss in --mode live used to leave the symbol cap-exempt until
    process restart. Re-querying yfinance on every call for an unresolved
    symbol is a small overhead vs. silently disabling a hard risk rule.

    2026-09-01 audit: "Unknown" used to mean EXEMPT from
    `RiskRuleEngine.check`'s sector cap (rule 5 skipped the check outright).
    80 of 101 universe symbols depend on this lookup with no offline
    fallback, so a network blip silently switched the sector cap off for
    most of the book. The gate now pools "Unknown" like any other sector
    (`sector_side_gross(..., include_unknown=True)`) and checks it against
    the same soft/hard cap pair instead of skipping it — see
    `_sector_resolution_status_for` below for WHY a given call came back
    "Unknown", which the gate surfaces as an owner-visible alert.
    """
    # _sector_lock guards ONLY the cache dict — never a network call.
    # audit F3: the old code held _sector_lock for the entire function
    # including the yfinance fetch, so one stuck symbol froze every
    # sector lookup process-wide (risk/position sizing all serialize
    # through _get_sector).
    with _sector_lock:
        cached = _sector_cache.get(symbol)
    if cached is not None:
        return cached
    if symbol.upper() in _INDEX_ETFS:
        with _sector_lock:
            _sector_cache[symbol] = "Broad"
        return "Broad"
    # Sector/thematic ETFs: yfinance .info has no `sector` for ETFs, so
    # without this table they resolve to "Unknown" and silently switch the
    # sector cap OFF (see _ETF_SECTORS). Deterministic, offline, before the fetch.
    etf_sector = _ETF_SECTORS.get(symbol.upper())
    if etf_sector is not None:
        with _sector_lock:
            _sector_cache[symbol] = etf_sector
        return etf_sector

    # Set from inside the worker thread when the fetch itself raises — read
    # back on the calling thread only after `.result()` returns (timeout
    # aside, where we already know the answer without consulting this).
    # Best-effort/advisory like the status table it feeds; not a
    # correctness dependency of the timeout/lock guarantees below.
    fetch_error = {"raised": False}

    def _fetch():
        try:
            return yf.Ticker(symbol).info or {}
        except Exception as e:
            logger.warning("yfinance sector fetch raised for %s: %s", symbol, e)
            fetch_error["raised"] = True
            return {}

    # yfinance .info has no hard upper bound — a stuck socket can hang
    # for far longer than _SECTOR_LOOKUP_TIMEOUT_S. audit F3: do NOT use
    # `with ThreadPoolExecutor(...)`; its __exit__ calls
    # shutdown(wait=True), which re-blocks on the hung worker after the
    # .result() timeout fires, making the ceiling illusory.
    # shutdown(wait=False, cancel_futures=True) returns immediately. A
    # still-running fetch leaks one worker thread — accepted vs. the
    # prior behaviour of stalling the whole session.
    ex = ThreadPoolExecutor(max_workers=1)
    timed_out = False
    try:
        info = ex.submit(_fetch).result(timeout=_SECTOR_LOOKUP_TIMEOUT_S)
    except FuturesTimeout:
        logger.warning("yfinance sector lookup timed out for %s", symbol)
        info = {}
        timed_out = True
    finally:
        ex.shutdown(wait=False, cancel_futures=True)

    raw = info.get("sector", "") if isinstance(info, dict) else ""
    canonical = _canonicalize_sector(raw)
    if canonical != "Unknown":
        with _sector_lock:
            _sector_cache[symbol] = canonical
        return canonical

    # Unresolved. Record WHY — never cached (see docstring above), same as
    # the "Unknown" return itself, so a self-heal on the next call is
    # re-diagnosed fresh rather than repeating a stale verdict.
    # `not info` (fetch technically completed, no exception, but returned
    # nothing) is bucketed with "lookup_failed": per
    # test_sector_canonicalization.py's own finding, an empty response for
    # a real symbol is usually transient, not proof the symbol lacks a
    # sector. "no_sector" is reserved for a fetch that came back with real
    # data and simply had no `sector` field in it.
    status = "lookup_failed" if (timed_out or fetch_error["raised"] or not info) else "no_sector"
    with _sector_lock:
        _sector_resolution_status[symbol] = status
    return canonical


#: Entry sides `place_entry_protection` will derive a protective side from.
#: Anything else is refused rather than guessed — see the fail-closed note in
#: `place_entry_protection`. "sell" and "sell_short" both open/extend a short.
_ENTRY_SIDES = frozenset({"buy", "sell", "sell_short"})

# `PROTECTIVE_ORDER_ACTIVE_STATUSES` moved to src/execution/broker_parts/stop_place.py (re-exported above).

#: Broker order states in which an order has been ACCEPTED BY US to the
#: broker but is not yet working on the book. Alpaca's own enum names them:
#: `pending_new` (received, not yet routed) and `accepted_for_bidding`.
#:
#: These are deliberately NOT in the set above. That set answers "is this
#: resting order real protection right now?" and its reader
#: (`replace_stop_loss`'s failure path) is looking at the AGED order book —
#: an order that has been sitting there and is still `pending_new` is a
#: stuck order, not coverage.
PROTECTIVE_ORDER_PLACEMENT_PENDING_STATUSES = frozenset(
    {"pending_new", "accepted_for_bidding"}
)

#: The set for the OTHER question: "would submitting another stop here
#: create a SECOND live order against the same shares?"
#:
#: Its reader (`TradingPipeline._reprotect_residual`'s idempotency check)
#: is looking at a stop this desk placed SECONDS ago on a prior attempt of
#: the same reprotect, after excluding by order id every stop this run
#: itself cancelled. Nothing in this codebase reconciles a duplicate
#: protective stop (see `src/coverage_watchdog.py`, which states it never
#: cancels or modifies; the only duplicate handling anywhere is a message
#: asking the owner to cancel one by hand), so the duplicate must be
#: prevented rather than cleaned up.
#:
#: CORRECTED 2026-10-01 (adversary round 2, defect 2): that reader no
#: longer treats this union as one answer. A `pending_new` stop can still
#: become `rejected`, so it is neither protection to bank nor an order to
#: place a second stop over; the reprotect path reads the two member sets
#: SEPARATELY and gives the in-flight case its own outcome — no write-back,
#: no drain of the recovery intent, re-read on the next pass. The union is
#: kept as the vocabulary for "neither terminal nor dying".
#:
#: `pending_cancel` stays OUT of both sets: a dying order is never
#: protection, whichever question is being asked.
PROTECTIVE_ORDER_ALIVE_STATUSES = (
    PROTECTIVE_ORDER_ACTIVE_STATUSES | PROTECTIVE_ORDER_PLACEMENT_PENDING_STATUSES
)


# `real_broker_order_id` moved to src/execution/broker_parts/stop_place.py (re-exported above).


def _is_broker_class_shim(obj, attr: str) -> bool:
    """True when `obj` is AlpacaBroker's own thin shim for `attr`, however it was
    bound: a bound method (`__func__`), a `functools.partial` over the plain
    function (`func`, unwrapped through nested partials), or the plain function."""
    target = getattr(AlpacaBroker, attr, None)
    if target is None:
        return False
    seen = obj
    for _ in range(8):
        if seen is target:
            return True
        if isinstance(seen, functools.partial):
            seen = seen.func
            continue
        bound = getattr(seen, "__func__", None)
        if bound is None:
            return False
        seen = bound
    return False


class AlpacaBroker:
    #: Set in __init__. Declared here so an instance built without __init__
    #: reads None rather than raising; `_kill_switch_active` already treats
    #: None as "no switch configured", i.e. inert.
    _kill_switch_path: "Path | None" = None
    #: Called with the facts of every protective stop the kill switch
    #: refuses (see `_submit_stop_limit_order`). The broker holds no
    #: database, so the owner of one wires this — `TradingPipeline` does, to
    #: `src/execution/exit_path_records.record_protective_stop_blocked`.
    #: None (the default) records nothing, exactly as before. Recording
    #: only: its result and any exception it raises are ignored.
    protective_stop_block_recorder: "object | None" = None
    #: Live `trade_updates` websocket feed. Declared here, FALSE, for the
    #: same reason as `_kill_switch_path` above: an instance built without
    #: __init__ must read the safe value rather than raise. Fail-closed
    #: direction is OFF — a construction site that never threads the flag
    #: through (the read-only broker in src/api/broker_reads.py, the
    #: heartbeat, one-off scripts, isolated unit tests) must not be able to
    #: open the account's single socket by omission. See
    #: `ExecutionConfig.fill_stream_enabled` for the flag's history (off
    #: 2026-09-17, on in production again since 2026-09-18);
    #: src/pipeline.py is the only site that passes it.
    _fill_stream_enabled: bool = False
    def __init__(self, api_key: str, secret_key: str, paper: bool = True,
                 kill_switch_path: str | None = None,
                 trade_updates_lease_path: str | None = None,
                 fill_stream_enabled: bool = False):
        self.api_key = api_key
        self.secret_key = secret_key
        self._paper = paper
        self.client = TradingClient(api_key, secret_key, paper=paper)
        _install_http_timeout(self.client)
        self._data_client = None
        # In-memory cache for completed (closed) price bars only — see
        # get_bars / get_intraday_chart_bars. Keyed so that the current,
        # still-forming trading day is never stored and always refetched
        # fresh; only prior, fully-closed days are ever served from here.
        self._closed_bars_cache: dict = {}
        self._closed_bars_cache_lock = threading.Lock()
        # Guard 1 (2026-09-02 operational safety guard — see
        # RiskConfig.kill_switch_path). `None` leaves the guard disabled,
        # which is only reachable from a construction site that predates
        # this parameter and never threads a path through (e.g. an isolated
        # unit test building `AlpacaBroker` directly) — `src/pipeline.py`
        # always passes the configured path. See `_kill_switch_active`.
        self._kill_switch_path = (
            Path(kill_switch_path) if kill_switch_path else None
        )
        # Per-date cache for is_trading_day. Trading-day status is set by
        # the exchange calendar months in advance — invariant within the
        # day — so a per-date dict that grows unbounded over a multi-year
        # process lifetime is still fine (1 entry per calendar day ≈ a
        # few KB / year).
        self._trading_day_cache: dict[date, bool] = {}
        # Per-date cache for get_session_open, same lifetime/invariance
        # argument as `_trading_day_cache` above — the exchange's regular
        # session open (including early-close-day exceptions) is fixed by
        # the calendar in advance. Backs `broker_reads._quote_freshness`
        # (docs/WORK.md item 15), which may run on every /quotes read.
        self._session_open_cache: dict[date, "datetime | None"] = {}
        # Stage 3 (shorts, D6). Per-run cache, same shape/lifetime as
        # `_trading_day_cache` above — a symbol's shortable/easy_to_borrow
        # flags don't change intra-session, so one asset-directory lookup
        # per symbol per process is enough.
        self._shortable_cache: dict[str, dict] = {}
        # Spec §11.1. Same shape/lifetime and same reasoning as
        # `_shortable_cache`: `fractionable` is an asset-directory fact that
        # does not change intra-session.
        self._fractionable_cache: dict[str, dict] = {}
        self._last_stream_warmup: TradeStreamWarmup | None = None
        self._trade_hub: _TradeUpdatesHub | None = None
        self._trade_hub_lock = threading.Lock()
        self._trade_lease = _TradeUpdatesLease(
            Path(trade_updates_lease_path)
            if trade_updates_lease_path
            else _default_trade_updates_lease_path()
        )
        self._trade_slot_held = False
        self._trade_lease_contended = False
        self._fill_stream_enabled = bool(fill_stream_enabled)
        self._fill_stream_off_logged = False

    def _kill_switch_active(self) -> bool:
        """Guard 1: True once ops has halted the desk by `touch`-ing the
        configured flag file.

        `path.exists()` and NOTHING else — no read, no parse, no schema —
        so a zero-byte file, a file full of garbage, and a file the operator
        can no longer remember the format of all halt identically. The
        check cannot fail open on bad content because it never looks at any
        content.

        Called at the top of every method on this class that places or
        replaces an order at the broker (`submit_order`,
        `_submit_stop_limit_order`, `replace_entry_limit`) — deliberately
        including the exit and protective-stop paths. See
        RiskConfig.kill_switch_path for why this is the one guard in the
        codebase that also blocks a risk-reducing order.
        """
        return self._kill_switch_path is not None and self._kill_switch_path.exists()

    def _account_reads(self) -> AccountReads:
        """Thin shim: builds the standalone reads object from this broker's collaborators
        (bodies moved to src/execution/broker_parts/account_reads.py). Built per call so a
        client swapped after construction is what the body sees; the caches are this
        broker's own dicts, mutated in place."""
        return AccountReads(
            client=self.client,
            shortable_cache=self._shortable_cache,
            fractionable_cache=self._fractionable_cache,
            trading_day_cache=self._trading_day_cache,
            session_open_cache=self._session_open_cache,
            # `_session_edge` is itself a moved body -- same recursion guard as
            # `_stop_placer`: pass it ONLY when it is NOT this broker's shim.
            **{
                kw: getattr(self, attr)
                for kw, attr in (("session_edge", "_session_edge"),)
                if not _is_broker_class_shim(getattr(self, attr, None), attr)
            },
        )

    def get_account(self, *args, **kwargs):
        """Thin shim: body moved to src/execution/broker_parts/account_reads.py."""
        return self._account_reads().get_account(*args, **kwargs)

    def get_margin_interest_activities(self, *args, **kwargs):
        """Thin shim: body moved to src/execution/broker_parts/account_reads.py."""
        return self._account_reads().get_margin_interest_activities(*args, **kwargs)

    def get_all_account_activities(self, *args, **kwargs):
        """Thin shim: body moved to src/execution/broker_parts/account_reads.py."""
        return self._account_reads().get_all_account_activities(*args, **kwargs)

    def get_transient_equity_eligibility(self, *args, **kwargs):
        """Thin shim: body moved to src/execution/broker_parts/account_reads.py."""
        return self._account_reads().get_transient_equity_eligibility(*args, **kwargs)

    def list_assets(self, *args, **kwargs):
        """Thin shim: body moved to src/execution/broker_parts/account_reads.py."""
        return self._account_reads().list_assets(*args, **kwargs)

    def get_asset_record(self, *args, **kwargs):
        """Thin shim: body moved to src/execution/broker_parts/account_reads.py."""
        return self._account_reads().get_asset_record(*args, **kwargs)

    def get_shortability(self, *args, **kwargs):
        """Thin shim: body moved to src/execution/broker_parts/account_reads.py."""
        return self._account_reads().get_shortability(*args, **kwargs)

    def get_fractionability(self, *args, **kwargs):
        """Thin shim: body moved to src/execution/broker_parts/account_reads.py."""
        return self._account_reads().get_fractionability(*args, **kwargs)

    def get_recent_daily_closes(self, *args, **kwargs):
        """Thin shim: body moved to src/execution/broker_parts/account_reads.py."""
        return self._account_reads().get_recent_daily_closes(*args, **kwargs)

    def get_full_portfolio_history(self, *args, **kwargs):
        """Thin shim: body moved to src/execution/broker_parts/account_reads.py."""
        return self._account_reads().get_full_portfolio_history(*args, **kwargs)

    def get_positions(self) -> list[Position]:
        raw_positions = self.client.get_all_positions()
        positions = []
        for p in raw_positions:
            symbol = _internal_symbol(p.symbol)
            positions.append(Position(
                symbol=symbol,
                qty=float(p.qty),
                avg_entry=float(p.avg_entry_price),
                current_price=float(p.current_price),
                market_value=float(p.market_value),
                unrealized_pnl=float(p.unrealized_pl),
                unrealized_intraday_pnl=float(getattr(p, "unrealized_intraday_pl", 0) or 0),
                sector=_get_sector(symbol),
            ))
        return positions

    def is_trading_day(self, *args, **kwargs):
        """Thin shim: body moved to src/execution/broker_parts/account_reads.py."""
        return self._account_reads().is_trading_day(*args, **kwargs)

    def trading_sessions_held(self, *args, **kwargs):
        """Thin shim: body moved to src/execution/broker_parts/account_reads.py."""
        return self._account_reads().trading_sessions_held(*args, **kwargs)

    def is_last_trading_day_of_quarter(self, *args, **kwargs):
        """Thin shim: body moved to src/execution/broker_parts/account_reads.py."""
        return self._account_reads().is_last_trading_day_of_quarter(*args, **kwargs)

    def get_session_close(self, *args, **kwargs):
        """Thin shim: body moved to src/execution/broker_parts/account_reads.py."""
        return self._account_reads().get_session_close(*args, **kwargs)

    def get_session_open(self, *args, **kwargs):
        """Thin shim: body moved to src/execution/broker_parts/account_reads.py."""
        return self._account_reads().get_session_open(*args, **kwargs)

    def _session_edge(self, *args, **kwargs):
        """Thin shim: body moved to src/execution/broker_parts/account_reads.py."""
        return self._account_reads()._session_edge(*args, **kwargs)


    def get_top_movers(self, n: int = 15) -> list[dict]:
        """Return today's top-`n` gainers from Alpaca's screener.

        Output shape: ``[{"symbol": str, "percent_change": float, "price": float}, ...]``,
        sorted by `percent_change` descending as Alpaca returns them.
        Returns `[]` on any failure (SDK error, auth issue, empty response) —
        the missed-opportunity digest falls back to universe-only when the
        top-movers signal is unavailable, so a degraded screener must never
        crash an evening run. Caller treats [] as "no top-mover augmentation".
        """
        if n <= 0:
            return []
        try:
            # Lazy import + lazy-construct so the extra SDK client is only
            # instantiated the first time evening actually runs a digest.
            from alpaca.data.historical.screener import ScreenerClient
            from alpaca.data.requests import MarketMoversRequest
        except ImportError as exc:
            logger.warning("get_top_movers: screener SDK unavailable: %s", exc)
            return []

        if not hasattr(self, "_screener_client") or self._screener_client is None:
            try:
                self._screener_client = ScreenerClient(
                    api_key=self.api_key, secret_key=self.secret_key,
                )
                _install_http_timeout(self._screener_client)
            except Exception as exc:
                logger.warning("get_top_movers: ScreenerClient init failed: %s", exc)
                self._screener_client = None
                return []

        try:
            movers = self._screener_client.get_market_movers(
                MarketMoversRequest(top=n)
            )
        except Exception as exc:
            logger.warning("get_top_movers: screener API call failed: %s", exc)
            return []

        gainers = getattr(movers, "gainers", None) or []
        out: list[dict] = []
        # Suffix filter — Alpaca's screener returns warrants (.WS, .WSA,
        # .WSB), units (.U, .UN), and rights (.RT) alongside common stock.
        # None of these are tradable as equities in our system, and
        # yfinance 404s on them later — flooding logs with errors. Drop
        # them at the boundary instead. Class shares (e.g. BRK.B) keep
        # the dot but are legitimate; the universe uses the dash form
        # (BRK-B), so any .A/.B from the screener would also be skipped
        # if we filtered too aggressively. So we only filter the
        # non-equity-instrument suffixes explicitly.
        _NON_EQUITY_SUFFIXES = (".WS", ".WSA", ".WSB", ".U", ".UN", ".RT")
        for m in gainers:
            sym = getattr(m, "symbol", None)
            if not sym:
                continue
            alpaca_sym_upper = str(sym).upper()
            if alpaca_sym_upper.endswith(_NON_EQUITY_SUFFIXES):
                continue
            sym_upper = _internal_symbol(alpaca_sym_upper)
            try:
                out.append({
                    "symbol": sym_upper,
                    "percent_change": float(getattr(m, "percent_change", 0) or 0),
                    "price": float(getattr(m, "price", 0) or 0),
                })
            except (TypeError, ValueError):
                continue
            if len(out) >= n:
                break
        return out

    def get_bars(self, symbol: str, lookback_days: int = 120) -> list:
        """Fetch daily OHLCV bars from Alpaca as a list[OHLCV].

        Used by MarketDataProvider as a fallback when yfinance returns empty.
        Same shape as MarketDataProvider.get_ohlcv so the caller is oblivious
        to which source answered. Returns [] on any error.
        """
        from datetime import timedelta as _td
        from src.models import OHLCV
        from src.util.time import et_today

        try:
            if self._data_client is None:
                from alpaca.data.historical.stock import StockHistoricalDataClient
                self._data_client = StockHistoricalDataClient(
                    self.api_key, self.secret_key
                )
                _install_http_timeout(self._data_client)

            from alpaca.data.requests import StockBarsRequest
            from alpaca.data.timeframe import TimeFrame

            end = et_today()
            start = end - _td(days=lookback_days)
            alpaca_symbol = _alpaca_symbol(symbol)

            def _fetch(range_start, range_end) -> list[OHLCV]:
                req = StockBarsRequest(
                    symbol_or_symbols=alpaca_symbol,
                    timeframe=TimeFrame.Day,
                    start=range_start,
                    end=range_end,
                )
                raw = self._data_client.get_stock_bars(req)
                # SDK returns a BarSet-like object with .data = {symbol: [Bar, ...]}
                bars_list = None
                if hasattr(raw, "data") and isinstance(raw.data, dict):
                    bars_list = raw.data.get(alpaca_symbol)
                elif isinstance(raw, dict):
                    bars_list = raw.get(alpaca_symbol)
                if not bars_list:
                    return []
                parsed: list[OHLCV] = []
                for b in bars_list:
                    ts = getattr(b, "timestamp", None)
                    d = ts.date() if ts is not None else None
                    if d is None:
                        continue
                    try:
                        parsed.append(OHLCV(
                            date=d,
                            open=float(getattr(b, "open", 0) or 0),
                            high=float(getattr(b, "high", 0) or 0),
                            low=float(getattr(b, "low", 0) or 0),
                            close=float(getattr(b, "close", 0) or 0),
                            volume=int(getattr(b, "volume", 0) or 0),
                        ))
                    except (TypeError, ValueError):
                        continue
                return parsed

            # Caching: only the portion of the range up to (and including)
            # yesterday can possibly be closed/complete daily bars — Alpaca
            # doesn't publish a daily bar for a session that hasn't closed
            # yet, but we still never trust "today" to a cache: today's
            # entry is always fetched fresh, never cached. Keyed so a new
            # calendar day naturally invalidates the historical portion.
            hist_end = end - _td(days=1)
            cache_key = ("daily", alpaca_symbol, start, hist_end)
            with self._closed_bars_cache_lock:
                cached_hist = self._closed_bars_cache.get(cache_key)

            if cached_hist is None:
                # Cache miss: one fetch over the whole range, exactly as
                # before caching existed. Split the result so only the
                # closed (pre-today) portion is stored.
                all_bars = _fetch(start, end)
                cached_hist = [b for b in all_bars if b.date <= hist_end]
                with self._closed_bars_cache_lock:
                    self._closed_bars_cache[cache_key] = cached_hist
                return all_bars

            # Cache hit: reuse the closed history, only refetch today.
            today_bars = _fetch(end, end)
            out = cached_hist + [b for b in today_bars if b.date not in {c.date for c in cached_hist}]
            out.sort(key=lambda b: b.date)
            return out
        except Exception as e:
            logger.warning("broker.get_bars failed for %s: %s", symbol, e)
            return []

    def get_intraday_chart_bars(
        self, symbol: str, timeframe: str, lookback_days: int
    ) -> list[dict]:
        """Fetch read-only intraday OHLCV bars for Mission Control.

        This deliberately does not participate in trading decisions or
        execution. It uses the same Alpaca historical-data client as
        ``get_bars`` but preserves each bar's timestamp so Lightweight
        Charts can render 5m/15m/1h candles and align execution markers.
        Returns [] on any failure, matching the broker's other market-data
        degradation contracts.
        """
        from datetime import datetime as _dt, timedelta as _td, timezone as _tz
        from src.util.time import ET

        try:
            if self._data_client is None:
                from alpaca.data.historical.stock import StockHistoricalDataClient
                self._data_client = StockHistoricalDataClient(self.api_key, self.secret_key)
                _install_http_timeout(self._data_client)

            from alpaca.data.enums import DataFeed
            from alpaca.data.requests import StockBarsRequest
            from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

            timeframe_value = {
                "5m": TimeFrame(5, TimeFrameUnit.Minute),
                "15m": TimeFrame(15, TimeFrameUnit.Minute),
                "1h": TimeFrame.Hour,
            }.get(timeframe)
            if timeframe_value is None:
                return []

            now = _dt.now(_tz.utc)
            start = now - _td(days=lookback_days)
            alpaca_symbol = _alpaca_symbol(symbol)

            def _fetch(range_start, range_end) -> list[dict]:
                req = StockBarsRequest(
                    symbol_or_symbols=alpaca_symbol,
                    timeframe=timeframe_value,
                    start=range_start,
                    end=range_end,
                    # This account's market-data plan is entitled to IEX, not
                    # SIP. Leaving feed unset resolves to SIP server-side for
                    # sub-daily bars and comes back with zero bars for every
                    # symbol/range — silently, since Alpaca doesn't error, it
                    # just returns nothing. Daily bars (get_bars, above) aren't
                    # feed-gated the same way, which is why only this intraday
                    # path needs it.
                    feed=DataFeed.IEX,
                )
                raw = self._data_client.get_stock_bars(req)
                if hasattr(raw, "data") and isinstance(raw.data, dict):
                    bars_list = raw.data.get(alpaca_symbol)
                elif isinstance(raw, dict):
                    bars_list = raw.get(alpaca_symbol)
                else:
                    bars_list = None
                if not bars_list:
                    return []

                parsed: list[dict] = []
                for bar in bars_list:
                    ts = getattr(bar, "timestamp", None)
                    if ts is None:
                        continue
                    try:
                        parsed.append(
                            {
                                "date": ts.astimezone(ET).date().isoformat(),
                                "timestamp": ts.isoformat(),
                                "open": float(getattr(bar, "open", 0) or 0),
                                "high": float(getattr(bar, "high", 0) or 0),
                                "low": float(getattr(bar, "low", 0) or 0),
                                "close": float(getattr(bar, "close", 0) or 0),
                                "volume": int(getattr(bar, "volume", 0) or 0),
                            }
                        )
                    except (TypeError, ValueError):
                        continue
                return parsed

            # Caching: only prior, fully-closed trading days are cacheable.
            # Today (including its still-forming candle) is always fetched
            # fresh, never cached. The historical portion is cached keyed
            # by the (rounded-to-day) start and today's date, so a new
            # calendar day naturally invalidates it. The start is rounded
            # down to ET midnight of its day (a superset of the exact
            # `start` instant) purely so repeated calls with the same
            # lookback_days share one cache key — outside trading hours
            # Alpaca simply returns nothing extra, so this never fabricates
            # data, only makes the cache key stable.
            today_et = now.astimezone(ET).date()
            today_midnight_et = _dt.combine(today_et, _dt.min.time(), tzinfo=ET)
            start_day_et = start.astimezone(ET).date()
            cache_key = ("intraday", alpaca_symbol, timeframe, start_day_et, today_et)

            with self._closed_bars_cache_lock:
                cached_hist = self._closed_bars_cache.get(cache_key)

            if cached_hist is None:
                # Cache miss: one fetch over the whole range, exactly as
                # before caching existed. Split the result so only the
                # portion from before today is stored.
                all_bars = _fetch(start, now)
                cached_hist = [b for b in all_bars if b["date"] < today_et.isoformat()]
                with self._closed_bars_cache_lock:
                    self._closed_bars_cache[cache_key] = cached_hist
                return all_bars

            # Cache hit: reuse the closed history, only refetch today.
            today_bars = _fetch(today_midnight_et, now)
            out = cached_hist + today_bars
            out.sort(key=lambda b: b["timestamp"])
            return out
        except Exception as exc:
            logger.warning(
                "broker.get_intraday_chart_bars failed for %s/%s: %s",
                symbol, timeframe, exc,
            )
            return []

    def get_current_stop_price(self, *args, **kwargs):
        """Thin shim: body moved to src/execution/broker_parts/account_reads.py."""
        return self._account_reads().get_current_stop_price(*args, **kwargs)

    def get_latest_price_stamped(self, symbol: str) -> "LivePrice | None":
        """Latest price for `symbol` with its source and freshness attached.

        Order of preference is unchanged from `get_latest_price`: a real
        trade print first, then the quote midpoint, then a single side. What
        is new is that the answer says which one it was and whether the trade
        print is from today's ET date, so a caller about to place or move an
        order can refuse a stale number instead of silently acting on it.
        """
        try:
            if self._data_client is None:
                from alpaca.data.historical.stock import StockHistoricalDataClient

                self._data_client = StockHistoricalDataClient(self.api_key, self.secret_key)
                _install_http_timeout(self._data_client)

            from alpaca.data.requests import StockLatestQuoteRequest, StockLatestTradeRequest

            alpaca_symbol = _alpaca_symbol(symbol)

            trade_data = self._data_client.get_stock_latest_trade(
                StockLatestTradeRequest(symbol_or_symbols=alpaca_symbol)
            )
            trade = self._extract_symbol_payload(trade_data, alpaca_symbol)
            trade_price = float(getattr(trade, "price", 0) or 0)
            if trade_price > 0:
                trade_at = getattr(trade, "timestamp", None)
                from src.trading_calendar import live_price_is_today

                fresh = bool(live_price_is_today(trade_at))
                return LivePrice(
                    price=trade_price, source="last_trade", trade_at=trade_at,
                    is_today=fresh, is_today_print=fresh,
                )

            quote_data = self._data_client.get_stock_latest_quote(
                StockLatestQuoteRequest(symbol_or_symbols=alpaca_symbol)
            )
            quote = self._extract_symbol_payload(quote_data, alpaca_symbol)
            ask_price = float(getattr(quote, "ask_price", 0) or 0)
            bid_price = float(getattr(quote, "bid_price", 0) or 0)
            quote_at = getattr(quote, "timestamp", None)
            from src.trading_calendar import live_price_is_today

            quote_today = bool(live_price_is_today(quote_at))
            if ask_price > 0 and bid_price > 0:
                return LivePrice(
                    price=(ask_price + bid_price) / 2, source="quote_mid",
                    trade_at=quote_at, is_today=quote_today, is_today_print=False,
                )
            if ask_price > 0:
                return LivePrice(
                    price=ask_price, source="quote_ask", trade_at=quote_at,
                    is_today=quote_today, is_today_print=False,
                )
            if bid_price > 0:
                return LivePrice(
                    price=bid_price, source="quote_bid", trade_at=quote_at,
                    is_today=quote_today, is_today_print=False,
                )
        except Exception as exc:
            logger.warning("Failed to fetch latest price for %s: %s", symbol, exc)

        return None

    def get_latest_price(self, symbol: str) -> float | None:
        """Latest price as a bare number — unchanged behaviour.

        Reporting and grading callers ("how far has this moved since we sold
        it") do not care where the number came from, and they already degrade
        to a last close when it is missing. They keep this. Anything that
        places or moves an order should call `get_latest_price_stamped` and
        check `is_today_print`.
        """
        stamped = self.get_latest_price_stamped(symbol)
        return stamped.price if stamped is not None else None

    def get_latest_quote(self, symbol: str) -> dict[str, float | None]:
        """Return the current bid/ask without inventing a side of the book.

        Execution uses the ask to construct a bounded marketable BUY limit
        and the bid to construct the mirrored SHORT floor. Missing or failed
        quote data returns explicit ``None`` fields so the caller can retain
        its existing last-trade behavior without guessing.
        """
        out = {"bid_price": None, "ask_price": None}
        try:
            if self._data_client is None:
                from alpaca.data.historical.stock import StockHistoricalDataClient

                self._data_client = StockHistoricalDataClient(self.api_key, self.secret_key)
                _install_http_timeout(self._data_client)

            from alpaca.data.requests import StockLatestQuoteRequest

            alpaca_symbol = _alpaca_symbol(symbol)
            quote_data = self._data_client.get_stock_latest_quote(
                StockLatestQuoteRequest(symbol_or_symbols=alpaca_symbol)
            )
            quote = self._extract_symbol_payload(quote_data, alpaca_symbol)
            for field in out:
                try:
                    value = float(getattr(quote, field, 0) or 0)
                except (TypeError, ValueError):
                    value = 0.0
                out[field] = value if value > 0 else None
        except Exception as exc:
            logger.warning("Failed to fetch latest quote for %s: %s", symbol, exc)
        return out

    def get_intraday_snapshots(self, symbols: list[str]) -> dict[str, dict]:
        """Bulk current-session move data for the intraday opportunity scan.

        One Alpaca snapshot call for the whole symbol list (not one call
        per symbol — the same `symbol_or_symbols` bulk parameter
        `get_latest_price` already uses for a single symbol) — cheap
        enough to run every intra_check tick, unlike re-fetching daily
        bars for the whole universe.

        Returns, for every requested symbol, a dict of the current-session
        facts needed both to detect a material move and to give Tech
        truthful intraday evidence:

            {"last_price", "last_trade_at", "prev_close",
             "session_bar_at", "minute_close", "minute_bar_at",
             "session_open", "session_close", "session_high",
             "session_low", "session_volume"}

        `last_trade_at` is the raw provider datetime (or None) for the
        latest trade's own `timestamp` field — used by `broker_reads.py`
        to tell a stale last_price from a live one (docs/WORK.md item 15).

        The `session_*` fields come from Alpaca's `daily_bar`, which during
        the session is an INCOMPLETE, still-forming bar — callers must
        present it as such and must never append it to a series of completed
        daily bars. **It is not guaranteed to be TODAY's**: for a name that
        has not printed today, Alpaca returns the previous session's daily
        bar in that slot. `session_bar_at` is that bar's own opening
        timestamp so a caller can check the date before calling it "today"
        (docs/WORK.md item 120). `minute_close` / `minute_bar_at` are the
        snapshot's 1-minute bar and carry the same caveat.

        NONE of these fields is freshness-checked here. Use
        `src.data.live_price.resolve_live_price` to turn this payload into a
        price that is known to come from today — this method deliberately
        reports what the provider said, and the judgement about what counts
        as today lives in one place.

        Any field is `None` when unavailable. Never raises — broker/network
        failure degrades to an empty dict (caller treats that as "no signal
        this tick", not a crash).
        """
        if not symbols:
            return {}
        if self._data_client is None:
            try:
                from alpaca.data.historical.stock import StockHistoricalDataClient

                self._data_client = StockHistoricalDataClient(self.api_key, self.secret_key)
                _install_http_timeout(self._data_client)
            except Exception as exc:
                logger.warning("get_intraday_snapshots: data client init failed: %s", exc)
                return {}

        from alpaca.data.requests import StockSnapshotRequest

        requested = [(symbol, _alpaca_symbol(symbol)) for symbol in symbols]
        alpaca_symbols = list(dict.fromkeys(mapped for _, mapped in requested))
        successful_batches = 0

        def _fetch_batch(batch: list[str]) -> dict:
            """Bulk first; isolate a bad symbol only when Alpaca rejects a batch."""
            nonlocal successful_batches
            if not batch:
                return {}
            try:
                result = self._data_client.get_stock_snapshot(
                    StockSnapshotRequest(symbol_or_symbols=batch)
                )
                successful_batches += 1
                return result if isinstance(result, dict) else {}
            except Exception as exc:
                status_code = getattr(exc, "status_code", None)
                symbol_error = (
                    status_code in (400, 404, 422)
                    or "invalid symbol" in str(exc).lower()
                )
                if len(batch) == 1:
                    logger.warning(
                        "get_intraday_snapshots: symbol %s unavailable: %s",
                        batch[0], exc,
                    )
                    return {}
                if not symbol_error:
                    logger.warning(
                        "get_intraday_snapshots: bulk snapshot fetch failed "
                        "for %d symbols: %s",
                        len(batch), exc,
                    )
                    return {}
                midpoint = len(batch) // 2
                logger.warning(
                    "get_intraday_snapshots: batch of %d rejected; isolating bad symbol(s): %s",
                    len(batch), exc,
                )
                return {
                    **_fetch_batch(batch[:midpoint]),
                    **_fetch_batch(batch[midpoint:]),
                }

        snapshots = _fetch_batch(alpaca_symbols)
        if successful_batches == 0:
            return {}

        def _num(obj, attr):
            if obj is None:
                return None
            try:
                v = float(getattr(obj, attr, 0) or 0)
            except (TypeError, ValueError):
                return None
            return v if v > 0 else None

        out: dict[str, dict] = {}
        for symbol, alpaca_symbol in requested:
            snap = snapshots.get(alpaca_symbol) if isinstance(snapshots, dict) else None
            trade = getattr(snap, "latest_trade", None) if snap is not None else None
            prev_bar = getattr(snap, "previous_daily_bar", None) if snap is not None else None
            # TODAY's still-forming bar. Deliberately kept in its own
            # `session_*` namespace so no caller can mistake it for a
            # completed daily bar (2026-08-19 intraday-evidence fix).
            today_bar = getattr(snap, "daily_bar", None) if snap is not None else None
            # Alpaca's Trade model DOES carry its own `timestamp` field
            # (verified against the installed SDK, 2026-09-13) — this is
            # the provider's own market timestamp for the last print, not
            # a guess. Kept as the raw datetime (or None); broker_reads.py
            # serializes it and derives freshness from it.
            last_trade_at = getattr(trade, "timestamp", None) if trade is not None else None
            # board item 120: the `session_*` block was returned with no way
            # to tell WHICH session it belongs to. Alpaca's snapshot carries
            # the previous session's daily bar in `daily_bar` for a name that
            # has not printed today, so a caller rendering "CURRENT SESSION
            # (TODAY)" off these fields could be showing yesterday. `Bar
            # .timestamp` is a required field on the installed SDK's model
            # (`alpaca/data/models/bars.py`, verified 2026-09-20) and is the
            # bar's OPENING timestamp, so its ET date is the session date.
            session_bar_at = getattr(today_bar, "timestamp", None) if today_bar is not None else None
            # The 1-minute bar is an aggregation of REAL PRINTS on the same
            # entitled venue — not a quote. It is the finest-grained today
            # print the snapshot carries, and it exists for names whose
            # `latest_trade` is still yesterday's (item 120, 2026-09-17).
            minute_bar = getattr(snap, "minute_bar", None) if snap is not None else None
            minute_bar_at = getattr(minute_bar, "timestamp", None) if minute_bar is not None else None
            out[symbol] = {
                "last_price": _num(trade, "price"),
                "last_trade_at": last_trade_at,
                "prev_close": _num(prev_bar, "close"),
                "session_bar_at": session_bar_at,
                "minute_close": _num(minute_bar, "close"),
                "minute_bar_at": minute_bar_at,
                "session_open": _num(today_bar, "open"),
                "session_close": _num(today_bar, "close"),
                "session_high": _num(today_bar, "high"),
                "session_low": _num(today_bar, "low"),
                "session_volume": _num(today_bar, "volume"),
            }
        return out

    @staticmethod
    def _extract_symbol_payload(payload, symbol: str):
        if isinstance(payload, dict):
            return payload.get(symbol)
        try:
            return payload[symbol]
        except Exception:
            return getattr(payload, symbol, None)

    def _order_desk(self) -> OrderDesk:
        """Thin shim: builds the standalone order desk from this broker's collaborators
        (bodies moved to src/execution/broker_parts/order_desk.py). Built per call so a
        client or cluster method swapped after construction is what the body sees."""
        return OrderDesk(
            client=self.client,
            kill_switch_active=self._kill_switch_active,
            kill_switch_path=self._kill_switch_path,
            wait_for_order_status=self._wait_for_order_status,
            wait_for_order_status_via_stream=self._wait_for_order_status_via_stream,
            get_latest_price=self.get_latest_price,
            order_terminal_states=self._ORDER_TERMINAL_STATES,
            order_replaceable_states=self._ORDER_REPLACEABLE_STATES,
            max_replacement_hops=self._MAX_REPLACEMENT_HOPS,
            # Four collaborators below are themselves moved bodies, so the desk
            # already owns them. Passing this broker's same-named shim would
            # overwrite the desk's own method with a function that calls
            # straight back into the desk -- infinite recursion. Same guard
            # as `_stop_placer`: pass one ONLY when it is NOT that shim.
            **{
                kw: getattr(self, attr)
                for kw, attr in (
                    ("wait_for_order_terminal", "wait_for_order_terminal"),
                    ("resolve_replacement_chain", "resolve_replacement_chain"),
                    ("wait_for_order_status_via_polling", "_wait_for_order_status_via_polling"),
                    ("list_open_entry_orders_checked", "list_open_entry_orders_checked"),
                )
                if not _is_broker_class_shim(getattr(self, attr, None), attr)
            },
        )

    def cancel_open_orders(self, *args, **kwargs):
        """Thin shim: body moved to src/execution/broker_parts/order_desk.py."""
        return self._order_desk().cancel_open_orders(*args, **kwargs)

    def snapshot_protective_stops(
        self, symbol: str, *, side: str = "sell",
    ) -> tuple[bool, list[dict]]:
        """List + snapshot open protective stop orders WITHOUT cancelling them.

        audit F1 (review #1): the write-ahead recovery row must be
        persisted BEFORE any broker mutation. Splitting the read
        (snapshot) from the write (cancel) lets the pipeline do
        snapshot → persist WAL → cancel, so a process kill anywhere
        from the cancel onward is recoverable. Previously the WAL insert
        ran AFTER cancel_protective_stops had already cancelled the
        stops at the broker — a kill in that window left a naked
        position with no recovery intent.

        `side` is the STOP order's own side: "sell" (default) finds the
        stops protecting a long; "buy" finds the stops protecting a short.
        Every existing caller cancels/restores/re-protects a long being
        SOLD, so the default is unchanged; the coverage reconciler is the
        one caller that passes `side="buy"` to check a short.

        Returns ``(ok, specs)``. ``ok`` is FALSE when the broker's own
        order listing failed — board item 172, and this used to be the
        single most dangerous lie on the read path.

        It was documented as "always True", because a listing API error was
        swallowed by `_list_open_protective_stop_orders` and surfaced as an
        empty list. An empty list means "this position has no protective
        stop", so a broker outage was reported to the desk as a CONFIRMED
        NAKED POSITION — and the coverage reconciler then repaired against
        it, placing a full-size stop on top of a live stop it could not
        see. Both the strongest possible false statement about loss
        protection and a duplicate-protection write, from one swallowed
        exception.

        Callers that ignore `ok` are no worse off than before: `specs` is
        still empty in that case. Callers that read it can tell "there is
        no stop" from "I could not ask", which is the whole distinction
        item 172 exists for.

        NOT true of every caller, and the first version of this docstring
        said it was. `TradingPipeline._cancel_stops_with_write_ahead` reads
        `ok` and skips the SELL on False, so making this return False where
        it previously always returned True changed the EXIT path as well as
        the read path — five exit call sites, none of them reviewed when
        that change was made. The test pinning that skip
        (`test_cancel_stops_with_write_ahead_skips_on_snapshot_failure`,
        added 2026-05-16) pinned unreachable code for four months, because
        until board item 172 `ok` could not be False [measured from
        `git log -S`, 2026-09-23]. Nobody chose that behaviour; it was
        inherited. What it does now is decided at that call site and
        documented there.

        THE READ IS RETRIED before it reports UNKNOWN. The listers have had
        no retry at all: one exception and the answer was "I cannot ask",
        which now costs a skipped exit. The desk's own derived retry shape
        for the stop path — `_STOP_PLACEMENT_MAX_ATTEMPTS` attempts with
        `_STOP_PLACEMENT_BACKOFF_S` backoff — is justified on the grounds
        that "every failure worth retrying is transient: a 429, a 5xx, a
        dropped connection". That argument is STRONGER for a read than for
        the write it was written for: a retried read cannot double-place
        anything. Same constants, so there is no new number here.
        """
        errors: list = []
        stops: list = []
        for attempt in range(_STOP_PLACEMENT_MAX_ATTEMPTS):
            errors = []
            stops = self._list_open_protective_stop_orders(
                symbol, side=side, errors=errors,
            )
            if not errors:
                break
            if attempt + 1 < _STOP_PLACEMENT_MAX_ATTEMPTS:
                delay = _STOP_PLACEMENT_BACKOFF_S[
                    min(attempt, len(_STOP_PLACEMENT_BACKOFF_S) - 1)
                ]
                logger.warning(
                    "snapshot_protective_stops: listing %s's protective "
                    "stops failed (%s) — retrying in %.1fs (attempt %d of "
                    "%d).",
                    symbol, "; ".join(errors), delay,
                    attempt + 2, _STOP_PLACEMENT_MAX_ATTEMPTS,
                )
                time.sleep(delay)
        if errors:
            logger.error(
                "snapshot_protective_stops: could not READ %s's protective "
                "stops after %d attempts (%s) — reporting UNKNOWN, not "
                "'no stop'.",
                symbol, _STOP_PLACEMENT_MAX_ATTEMPTS, "; ".join(errors),
            )
            return False, []
        if not stops:
            return True, []
        specs: list[dict] = []
        for order in stops:
            spec = self._snapshot_stop_order(order)
            if spec:
                specs.append(spec)
        return True, specs

    def cancel_snapshotted_stops(
        self, symbol: str, specs: list[dict],
    ) -> bool:
        """Cancel pre-snapshotted protective stops by id.

        Same partial-failure discipline as the original
        cancel_protective_stops: if any cancel raises, the ones that
        did cancel are restored and False is returned (the caller won't
        proceed with the SELL, so leaving coverage shrunk for no gain
        would be strictly worse). Returns True iff every stop was
        cancelled (or there were none).
        """
        if not specs:
            return True
        cancelled: list[dict] = []
        failed = 0
        for spec in specs:
            sid = spec.get("id")
            if not sid:
                continue
            try:
                self.client.cancel_order_by_id(sid)
                cancelled.append(spec)
            except Exception as exc:
                logger.warning(
                    "cancel_snapshotted_stops: cancel failed for %s order "
                    "%s: %s", symbol, sid, exc,
                )
                failed += 1
        if failed > 0:
            restored = 0
            rollback_failed: list[dict] = []
            if cancelled:
                # _restore_stop_orders returns (restored_count, failed_specs)
                # — the old code DISCARDED it, so a rollback that itself
                # failed left the position with shrunk coverage and reported
                # only a bare False. The SELL is skipped either way, but the
                # operator (and the next session's coverage reconcile, which
                # now auto-repairs) must be able to see it (2026-07-16 audit).
                restored, rollback_failed = self._restore_stop_orders(symbol, cancelled)
            if rollback_failed:
                logger.error(
                    "cancel_snapshotted_stops: %d/%d cancel(s) failed for %s AND "
                    "the rollback could not restore %d of %d cancelled stop(s) — "
                    "%s is now UNDER-PROTECTED; next session's coverage reconcile "
                    "must repair it. SELL won't proceed.",
                    failed, len(specs), symbol, len(rollback_failed), len(cancelled),
                    symbol,
                )
            else:
                logger.warning(
                    "cancel_snapshotted_stops: %d/%d cancel(s) failed for %s "
                    "(rolled back %d/%d that succeeded); SELL won't proceed",
                    failed, len(specs), symbol, restored, len(cancelled),
                )
            return False
        if cancelled:
            logger.info(
                "Cancelled %d protective stop(s) for %s",
                len(cancelled), symbol,
            )
        return True

    def cancel_protective_stops(self, symbol: str) -> tuple[bool, list[dict]]:
        """Cancel all open SELL stop orders for one symbol so a fresh exit
        order has free shares to work with.

        Returns ``(success, cancelled_specs)``:
          - ``success`` is True iff every stop was cancelled cleanly (or
            none existed). Caller should skip the SELL on False.
          - ``cancelled_specs`` is the list of stop snapshots (qty,
            stop_price, limit_price) that were successfully cancelled.
            Caller uses this to:
              1. ``_restore_stop_orders`` if the SELL is rejected by
                 the broker (rollback the cancellation so coverage is
                 preserved).
              2. ``_submit_stop_limit_order`` on the residual qty after
                 a *partial* exit (TAKE_PROFIT / REDUCE / PARTIAL_SELL)
                 — without this, the residual position rides naked
                 until the next session re-attaches an OTO stop.

        Why this exists: Alpaca rejects new SELL orders when shares are
        held_for_orders by an existing protective stop — the OTO stop-loss
        leg attached to a morning BUY, or a TRAIL_STOP placed by midday.
        Without clearing those holds first, REDUCE / SELL / EMERGENCY_SELL
        / TAKE_PROFIT all surface as 'insufficient qty available' rejects
        (2026-04-25 AMZN incident, related_orders=[<TRAIL_STOP id>]).

        On partial cancel failure (some succeed, then one raises) the
        already-cancelled stops are restored before returning False —
        same rollback discipline as ``replace_stop_loss``. The caller
        won't proceed with the SELL anyway, so leaving partial-cancelled
        state at the broker would just shrink coverage for no gain.

        Now composed from snapshot_protective_stops +
        cancel_snapshotted_stops (audit F1 review #1). The external
        contract is unchanged: no stops -> (True, []); all cancelled ->
        (True, specs); partial failure -> rolled back, (False, []).
        Direct callers/tests are unaffected; SELL paths use the
        pipeline's write-ahead orchestrator instead so the recovery row
        lands before the cancel.
        """
        ok, specs = self.snapshot_protective_stops(symbol)
        if not ok:
            return False, []
        if not specs:
            return True, []
        if not self.cancel_snapshotted_stops(symbol, specs):
            return False, []
        return True, specs

    def cancel_stray_protective_stops(
        self, symbol: str, *, side: str = "sell",
    ) -> int:
        """Cancel every protective stop still resting on a symbol that is
        now FLAT. Returns the count cancelled.

        Board item 127(b), owner ruling 2026-09-25: a forced/emergency exit
        fires IMMEDIATELY and never waits on stop-work, so a concurrent
        stop-repair can re-add a protective stop inside the cancel-then-sell
        window. Once the exit takes the position to zero shares that stop is
        a stray — it protects nothing, the reprotect path never sees it (it
        was placed AFTER the pre-sell snapshot, so it is not in the sell's
        ``cancelled_specs``), and ``_reconcile_stop_coverage`` skips flat
        symbols outright — so nothing else would ever clear it, and a stop
        left resting on zero shares can later elect into an unintended
        short. This is the cheap cleanup the ruling assumes in place of the
        rejected lock-wait.

        Unlike ``cancel_snapshotted_stops`` there is NO rollback: the
        position is flat, so there is nothing to protect and a "restore"
        would only re-place the very stray order being removed. Best-effort
        and side-correct (``side="buy"`` finds the buy-stops that had
        protected a short); a cancel that raises is logged and never blocks
        the others, and the whole thing degrades to a no-op — the exit has
        already succeeded and must not be undone by a housekeeping error.
        """
        try:
            ok, specs = self.snapshot_protective_stops(symbol, side=side)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "cancel_stray_protective_stops: could not list %s-stops for "
                "now-flat %s: %s — a stray stop may still rest; the operator "
                "should confirm it is gone", side, symbol, exc,
            )
            return 0
        if not ok or not specs:
            return 0
        cancelled = 0
        for spec in specs:
            sid = spec.get("id")
            if not sid:
                continue
            try:
                self.client.cancel_order_by_id(sid)
                cancelled += 1
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "cancel_stray_protective_stops: cancel of stray %s-stop "
                    "%s on now-flat %s failed: %s — a stop may still rest on "
                    "a flat position; the operator should clear it by hand",
                    side, sid, symbol, exc,
                )
        if cancelled:
            logger.info(
                "Cancelled %d stray protective %s-stop(s) on now-flat %s "
                "(item 127(b): a repair re-added protection inside the "
                "cancel-then-sell window)", cancelled, side, symbol,
            )
        return cancelled

    def cancel_open_entry_orders(self, *args, **kwargs):
        """Thin shim: body moved to src/execution/broker_parts/order_desk.py."""
        return self._order_desk().cancel_open_entry_orders(*args, **kwargs)

    def list_open_entry_order_ids(self, *args, **kwargs):
        """Thin shim: body moved to src/execution/broker_parts/order_desk.py."""
        return self._order_desk().list_open_entry_order_ids(*args, **kwargs)

    def list_open_entry_orders_checked(self, *args, **kwargs):
        """Thin shim: body moved to src/execution/broker_parts/order_desk.py."""
        return self._order_desk().list_open_entry_orders_checked(*args, **kwargs)

    def open_buy_notional(self, *args, **kwargs):
        """Thin shim: body moved to src/execution/broker_parts/order_desk.py."""
        return self._order_desk().open_buy_notional(*args, **kwargs)

    def list_recent_orders(self, *args, **kwargs):
        """Thin shim: body moved to src/execution/broker_parts/order_desk.py."""
        return self._order_desk().list_recent_orders(*args, **kwargs)

    def list_filled_sell_orders(self, *args, **kwargs):
        """Thin shim: body moved to src/execution/broker_parts/order_desk.py."""
        return self._order_desk().list_filled_sell_orders(*args, **kwargs)

    def get_order_fill_info(self, *args, **kwargs):
        """Thin shim: body moved to src/execution/broker_parts/order_desk.py."""
        return self._order_desk().get_order_fill_info(*args, **kwargs)

    #: `OrderStatus`/`TradeEvent` values that mean "this order will not
    #: change again" — shared between the stream and polling paths so the
    #: two mechanisms can never quietly disagree about what "terminal" means.
    _ORDER_TERMINAL_STATES = frozenset({
        "filled", "canceled", "cancelled", "expired", "rejected",
        "done_for_day", "replaced",
    })

    #: Statuses in which Alpaca has the order but the EXECUTION VENUE does
    #: not yet. Source: Alpaca's own order-lifecycle reference
    #: (docs.alpaca.markets/docs/orders-at-alpaca, "Order Lifecycle"):
    #:   accepted    — "received by Alpaca, but hasn't yet been routed to the
    #:                  execution venue"
    #:   pending_new — "received by Alpaca, and routed to the exchanges, but
    #:                  has not yet been accepted"
    #: `new` is the first status that means "routed to exchanges for
    #: execution". A replace PATCH against an order still in one of these
    #: states is rejected by the broker ("unable to replace order, order
    #: isn't sent to exchange yet" / "cannot replace order in accepted
    #: status" in Alpaca's own community forum) — and the two transitional
    #: statuses `pending_cancel`/`pending_replace` are likewise listed by
    #: Alpaca's Replace-Order reference as non-replaceable. These are a
    #: distinct set from `_ORDER_TERMINAL_STATES`: not "done", just "not
    #: there yet". At the market open — the slowest acknowledgement and the
    #: time this desk trades most — an order can sit here for seconds.
    _ORDER_PRE_EXCHANGE_STATES = frozenset({"accepted", "pending_new"})

    #: The only working status a replace is documented AND observed to
    #: succeed against. `partially_filled` is deliberately excluded: it is
    #: replaceable at the broker, but this desk never replaces a partially
    #: filled order (see `_repeg_entry_order`'s partial-fill guard).
    _ORDER_REPLACEABLE_STATES = frozenset({"new"})

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
                except Exception:
                    pass
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
            except Exception as exc:
                logger.warning("trade_updates hub failed to start: %s", exc)
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
                except Exception:
                    pass
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

    def wait_for_order_at_exchange(self, *args, **kwargs):
        """Thin shim: body moved to src/execution/broker_parts/order_desk.py."""
        return self._order_desk().wait_for_order_at_exchange(*args, **kwargs)

    def wait_for_order_terminal(self, *args, **kwargs):
        """Thin shim: body moved to src/execution/broker_parts/order_desk.py."""
        return self._order_desk().wait_for_order_terminal(*args, **kwargs)

    def _get_order_status_once(self, *args, **kwargs):
        """Thin shim: body moved to src/execution/broker_parts/order_desk.py."""
        return self._order_desk()._get_order_status_once(*args, **kwargs)

    def _wait_for_order_terminal_via_stream(self, *args, **kwargs):
        """Thin shim: body moved to src/execution/broker_parts/order_desk.py."""
        return self._order_desk()._wait_for_order_terminal_via_stream(*args, **kwargs)

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
            except Exception:
                logger.warning(
                    "order-fill stream handler error for %s", order_id,
                    exc_info=True,
                )

        try:
            stream.subscribe_trade_updates(_handler)
        except Exception as exc:
            logger.warning("order-fill stream subscribe failed: %s", exc)
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
                except Exception:
                    pass
                thread.join(timeout=2.0)
                return None, False
        remaining = max(0.0, deadline - time.monotonic())
        match_wake.wait(timeout=remaining)
        try:
            stream.stop()
        except Exception:
            pass  # best-effort; the background thread is a daemon regardless
        thread.join(timeout=5.0)

        if not matched.is_set() and run_error and not connected.is_set():
            # Never reached a live, authenticated connection — treat as
            # "stream unusable", not "order still open".
            return None, False
        return result["status"], True

    def _wait_for_order_terminal_via_polling(self, *args, **kwargs):
        """Thin shim: body moved to src/execution/broker_parts/order_desk.py."""
        return self._order_desk()._wait_for_order_terminal_via_polling(*args, **kwargs)

    def _wait_for_order_status_via_polling(self, *args, **kwargs):
        """Thin shim: body moved to src/execution/broker_parts/order_desk.py."""
        return self._order_desk()._wait_for_order_status_via_polling(*args, **kwargs)

    def submit_order(self, *args, **kwargs):
        """Thin shim: body moved to src/execution/broker_parts/order_desk.py."""
        return self._order_desk().submit_order(*args, **kwargs)

    # SCOPE (owner ratified 2026-09-25): primary PROTECTIVE stops are now
    # stop-MARKET (guaranteed exit), so this buffer NO LONGER governs the
    # protective stop the desk normally places. It governs only (a) the
    # stop-LIMIT FALLBACK taken when the broker refuses a stop-market for an
    # unsupported type/tif combo, and (b) the force-de-lever must-fill SELL.
    #
    # 3% beyond the stop: a stop-MARKET fills at whatever the book has on a
    # gap (10%+ worse than the stop); a stop-limit caps the worst-case fill.
    # The buffer must be wide enough that routine volatility clears it
    # ("prioritize fill over price"). Trade-off on those fallback/de-lever
    # legs: on gaps beyond 3% the limit won't fill and the position stays
    # open until a session can act — which is exactly why the primary
    # protective stop is now market and not subject to this trade-off.
    #
    # "Beyond", not "below": a long's protective order is a SELL stop, so
    # its limit sits 3% BELOW the trigger (a SELL needs its floor under the
    # stop to have room to fill on the way down). A short's protective order
    # is a BUY stop, so its limit must sit 3% ABOVE the trigger — a BUY
    # needs headroom over the stop to fill on the way up. Getting this
    # backwards for a short is silent: the order still submits, but the
    # limit sits on the wrong side of the trigger, so it can never fill.
    # The stop then "fires" and does nothing, and the position runs
    # unprotected in the one direction that matters.
    STOP_LIMIT_BUFFER_PCT = 0.03

    # Order states that mean "this order can never fill another share".
    _TERMINAL_ORDER_STATES = frozenset({
        "filled", "canceled", "cancelled", "expired", "rejected",
        "done_for_day", "stopped", "suspended",
    })

    # Bounded number of `replaced_by` hops to follow when resolving what a
    # replaced order became. Each re-peg adds exactly one hop and re-pegs are
    # capped in the low single digits, so 8 is generous; the bound exists so a
    # broker-side cycle or a pathological chain can never spin this forever.
    _MAX_REPLACEMENT_HOPS = 8

    def cancel_entry_order(self, *args, **kwargs):
        """Thin shim: body moved to src/execution/broker_parts/order_desk.py."""
        return self._order_desk().cancel_entry_order(*args, **kwargs)

    def resolve_replacement_chain(self, *args, **kwargs):
        """Thin shim: body moved to src/execution/broker_parts/order_desk.py."""
        return self._order_desk().resolve_replacement_chain(*args, **kwargs)

    def replace_entry_limit(self, *args, **kwargs):
        """Thin shim: body moved to src/execution/broker_parts/order_desk.py."""
        return self._order_desk().replace_entry_limit(*args, **kwargs)

    def await_replacement_confirmed(self, *args, **kwargs):
        """Thin shim: body moved to src/execution/broker_parts/order_desk.py."""
        return self._order_desk().await_replacement_confirmed(*args, **kwargs)

    def place_entry_protection(
        self, symbol: str, order_id: str, stop_price: float,
        *, requested_qty: float | None = None, side: str = "buy",
        superseded_filled_qty: float = 0.0,
        on_unfilled_cancel=None,
        cover_full_position: bool = False,
        held_qty_before: float = 0.0,
    ) -> dict | None:
        """Wait for an entry order to reach terminal, then place a GTC
        protective stop (stop-MARKET, guaranteed exit) for the ACTUAL filled
        qty.

        If the entry is STILL WORKING after the wait (slow tape, wide limit),
        the unfilled remainder is CANCELLED first — audit round 2: the 15s
        wait treated "still live" identically to "terminal 0-fill" and walked
        away, so a DAY entry limit could fill hours later with no stop
        watching it (and a resting BUY could even re-buy into a crash after an
        emergency liquidation). Cancelling converges the order; whatever DID
        fill by then gets its stop from the post-cancel re-read. Losing the
        unfilled remainder is the accepted cost of protection-first.

        THIS CANCEL IS THE END-OF-CYCLE CANCEL (owner-approved 2026-09-12),
        and its timing is derived, not chosen. This desk does not run
        continuously: it runs as separate scheduled SESSIONS — see
        `SESSION_WINDOWS` in `src/trading_calendar.py` — each a single
        process that analyses at the prices and levels of that moment,
        proposes entries, submits them, protects the fills, and EXITS. New
        entries come only from the morning session; the midday and close
        sessions review positions. (The systemd/launchd timer ticks every 30
        minutes, but that tick only asks `scripts/run_if_et_window.sh`
        whether a session is due; it is not a re-scan.) So "the decision
        cycle that created the order" is this very process, and its boundary
        is the point where this process stops waiting for the fill and moves
        on — which is exactly here. An entry that outlived its own session
        would be resting on a thesis nobody is still holding: the next
        session re-analyses from scratch at real current prices and will
        re-propose the trade if it still wants it. Cancelling here — rather
        than leaving a DAY order resting until 16:00 ET — is therefore
        binding the order's life to the desk's own heartbeat, not to a
        timeout somebody picked. There is deliberately no separate "cancel
        after N minutes" constant: the boundary IS the end of this stage, and
        stays correct if the session schedule ever changes. The only number
        in play is `_ENTRY_FILL_TIMEOUT_S`, the in-cycle patience for a fill,
        which predates this and is unchanged.

        `on_unfilled_cancel`, when given, is called with a small dict
        (`order_id`, `status`, `filled_qty`) after a still-working entry was
        cancelled here and the post-cancel re-read shows NOTHING filled under
        any id in its chain. The caller uses it to page the owner with the
        prices that were tried — this method does not know them. Not invoked
        for a partial fill (shares were acquired and the stop covers them)
        or for an order that reached terminal on its own. Never allowed to
        raise into this method.

        `side` is the ENTRY order's own side — "buy" opens or adds to a long
        (the only side any order path in this repo has ever submitted, hence
        the default), "sell"/"sell_short" opens a short. The protective stop
        is always the OPPOSITE side, at the opposite buffer: a SELL stop
        below a long, a BUY stop above a short. See `STOP_LIMIT_BUFFER_PCT`.

        `superseded_filled_qty` is shares this entry already acquired under a
        DIFFERENT order id — the ancestors of a re-peg chain. `order_id` is
        the last order in that chain, and Alpaca's fill counters do not carry
        across a replacement, so the shares an ancestor filled are invisible
        here. They are real shares in a real position, and a stop sized to
        only the last order's fill would leave them naked. Adding them is what
        keeps the invariant "every filled share is under a stop" true across a
        re-peg. Default 0.0: for every caller that never re-pegs, this method
        behaves exactly as it did before.

        `cover_full_position` (long scale-in path B, 2026-09-15): after a
        positive fill, size the protective sell to the broker's FULL
        position quantity, not this order's fill. A partial add on a name
        that already held shares would otherwise rearm a stop over the
        add alone and leave the original lot naked. `held_qty_before` is
        the fallback if the broker position cannot be read: fill + what
        was held, the two quantities already measured, not a third number.

        Returns the stop order dict, or None when nothing was placed (entry
        filled 0 / stop submit failed). Never raises — a failure here must not
        abort the session.

        Spec §11.1 guard 1: the stop submission now RETRIES immediately and
        hard before giving up (`_submit_protective_stop_retrying`). A None
        return therefore means the retries were exhausted, and the position is
        naked — the CALLER owes an owner alert on it (guard 2); the
        coverage-reconcile auto-repair belt remains the backstop, not the
        first line.
        """
        # Fail closed on a side we do not recognise, BEFORE touching the
        # broker. `"sell" if side == "buy" else "buy"` reads harmlessly but is
        # fail-OPEN: a typo, a None, or some future side string falls into the
        # short branch, and a LONG then gets a BUY stop placed ABOVE it — not
        # weak protection, but a standing order to buy more of a position that
        # is already losing.
        #
        # This returns rather than raising, because the contract above is that
        # this function never aborts a session. Returning None is the same
        # outcome as any other protection failure: logged at ERROR, position
        # left naked-but-KNOWN, and picked up by the coverage-reconcile
        # auto-repair belt. Naked-and-believed-covered is the state that
        # actually costs money, and refusing here is what prevents it.
        normalized = (side or "").strip().lower()
        if normalized not in _ENTRY_SIDES:
            logger.error(
                "entry protection: %s refusing to guess a protective side for "
                "entry side %r (expected one of %s) — NO stop placed, position "
                "will be left uncovered and must be repaired by reconcile",
                symbol, side, sorted(_ENTRY_SIDES),
            )
            return None

        try:
            status = self.wait_for_order_terminal(
                order_id, timeout_seconds=_ENTRY_FILL_TIMEOUT_S,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("entry protection: wait failed for %s (%s): %s",
                           symbol, order_id, exc)
            status = None

        cancelled_here = False
        if (status or "").lower() not in self._TERMINAL_ORDER_STATES:
            # Still working at the end of its cycle — cancel the remainder so
            # it can't fill unwatched and so it stops resting on a thesis
            # this session is about to walk away from (see the docstring).
            # A fill can land during cancel propagation; the post-cancel
            # re-read below protects whatever landed.
            logger.warning(
                "entry protection: %s entry %s still working at the end of "
                "its session (status=%s) — cancelling the unfilled remainder "
                "so no share can fill without a stop watching it and no "
                "order outlives the analysis that created it",
                symbol, order_id, status or "unknown",
            )
            try:
                self.client.cancel_order_by_id(order_id)
                cancelled_here = True
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "entry protection: cancel of still-working entry %s (%s) "
                    "failed: %s — a later fill will be UNPROTECTED until the "
                    "next coverage reconcile", symbol, order_id, exc,
                )
            try:
                status = self.wait_for_order_terminal(
                    order_id, timeout_seconds=10.0,
                ) or status
            except Exception:  # noqa: BLE001
                pass
            if (status or "").lower() not in self._TERMINAL_ORDER_STATES:
                # Fill confirmation has genuinely DEGRADED: the bounded
                # window closed, the cancel-and-recheck closed too, and the
                # broker still has not said what happened to a live order.
                # The desk proceeds on filled_qty=0 below — the safe
                # assumption, possibly a wrong one — so the owner has to be
                # told, not just the log. This is NOT "the websocket is
                # off": it is reachable identically with the socket on, and
                # is exactly the outcome the REST path is supposed to
                # prevent. See src/notifier.py's fill-confirmation block.
                try:
                    from src.notifier import alert_order_outcome_unconfirmed
                    alert_order_outcome_unconfirmed(
                        symbol, order_id,
                        waited_seconds=_ENTRY_FILL_TIMEOUT_S,
                        last_status=(status or "").lower() or None,
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "entry protection: unconfirmed-outcome alert for %s "
                        "could not be sent: %s", symbol, exc,
                    )

        try:
            info = self.get_order_fill_info(order_id) or {}
        except Exception as exc:  # noqa: BLE001
            logger.warning("entry protection: fill info failed for %s: %s", symbol, exc)
            info = {}
        try:
            filled_qty = float(info.get("filled_qty") or 0)
        except (TypeError, ValueError):
            filled_qty = 0.0
        try:
            carried = float(superseded_filled_qty or 0)
        except (TypeError, ValueError):
            carried = 0.0
        if carried > 0:
            logger.info(
                "entry protection: %s carries %.4f share(s) filled under a "
                "superseded order id; stop will cover %.4f + %.4f",
                symbol, carried, filled_qty, carried,
            )
            filled_qty += carried
        if filled_qty > 0 and cover_full_position:
            from src.execution.scale_in import cover_qty_for_rearm
            full_qty = cover_qty_for_rearm(
                self, symbol=symbol, filled_qty=filled_qty,
                held_qty_before=held_qty_before,
            )
            if full_qty > filled_qty + 1e-9:
                logger.info(
                    "entry protection: %s scale-in fill %.4f — stop sized to "
                    "broker full position %.4f, not the add alone",
                    symbol, filled_qty, full_qty,
                )
            if full_qty > 0:
                filled_qty = full_qty

        if filled_qty <= 0:
            logger.warning(
                "entry protection: %s entry %s filled 0 (status=%s) — no stop "
                "placed (nothing to protect)", symbol, order_id, status or "unknown",
            )
            if cancelled_here and on_unfilled_cancel is not None:
                try:
                    on_unfilled_cancel({
                        "order_id": order_id,
                        "status": (status or "").lower() or "unknown",
                        "filled_qty": 0.0,
                    })
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "entry protection: unfilled-cancel callback for %s "
                        "raised: %s", symbol, exc,
                    )
            return None
        if (
            requested_qty and filled_qty < requested_qty
            and not cover_full_position
        ):
            logger.warning(
                "entry protection: %s partially filled %.4f/%.4f — stop sized to "
                "the ACTUAL fill", symbol, filled_qty, requested_qty,
            )
        # The protective order's side is the OPPOSITE of the entry's: a BUY
        # entry (long) is protected by a SELL stop below it; a SELL/SELL_SHORT
        # entry (short) is protected by a BUY stop above it. The buffer
        # mirrors the same way — see STOP_LIMIT_BUFFER_PCT above. Getting
        # this backwards is THE most dangerous bug in shorts-safe: the order
        # still submits without error, it just sits on the wrong side of the
        # trigger and can never fill, so the position runs unprotected in
        # exactly the direction it needed protecting.
        protective_side = "sell" if normalized == "buy" else "buy"
        buffer_mult = (
            (1 - self.STOP_LIMIT_BUFFER_PCT) if protective_side == "sell"
            else (1 + self.STOP_LIMIT_BUFFER_PCT)
        )
        stop_order = self._submit_protective_stop_retrying(
            symbol=symbol, qty=filled_qty, stop_price=stop_price,
            limit_price=stop_price * buffer_mult, side=protective_side,
        )
        if stop_order is None:
            logger.error(
                "entry protection FAILED for %s (%.4f shares held, stop $%.2f) "
                "after %d attempt(s) — position is UNPROTECTED; the caller must "
                "raise an OWNER alert (spec §11.1 guard 2) and the coverage "
                "reconcile must repair it",
                symbol, filled_qty, stop_price, _STOP_PLACEMENT_MAX_ATTEMPTS,
            )
            return None
        return stop_order

    def _stop_placer(self) -> StopPlacer:
        """Thin shim: builds the standalone placer from this broker's collaborators
        (bodies moved to src/execution/broker_parts/stop_place.py). Built per call so a
        client or cluster method swapped after construction is what the body sees."""
        return StopPlacer(
            client=self.client,
            list_open_stop_orders_by_side=self._list_open_stop_orders_by_side,
            list_open_protective_stop_orders=self._list_open_protective_stop_orders,
            list_open_sell_stop_orders=self._list_open_sell_stop_orders,
            snapshot_stop_order=self._snapshot_stop_order,
            amend_one_stop_price=self._amend_one_stop_price,
            amend_resting_stop_price=self._amend_resting_stop_price,
            stop_order_amendable_in_place=self._stop_order_amendable_in_place,
            cancel_snapshotted_stops=self.cancel_snapshotted_stops,
            get_positions=self.get_positions,
            kill_switch_active=self._kill_switch_active,
            kill_switch_path=self._kill_switch_path,
            protective_stop_block_recorder=self.protective_stop_block_recorder,
            stop_limit_buffer_pct=self.STOP_LIMIT_BUFFER_PCT,
            # Six collaborators below are themselves moved bodies, so the
            # placer already owns them. Passing this broker's same-named shim
            # would overwrite the placer's own method with a function that
            # calls straight back into the placer -- infinite recursion. Pass
            # one ONLY when it is NOT that shim: a replacement bound on this
            # instance, or a stand-in on a test host that is not a broker at
            # all. Those are exactly the cases the placer cannot see itself.
            # The shim is recognised through a bound method (`__func__`) AND
            # through a `functools.partial` (`func`): tests bind the class
            # function onto a non-broker host with partial, and that is still
            # the shim.
            **{
                kw: getattr(self, attr)
                for kw, attr in (
                    ("submit_protective_stop_retrying", "_submit_protective_stop_retrying"),
                    ("submit_stop_leg_retrying", "_submit_stop_leg_retrying"),
                    ("existing_stop_covering_qty", "_existing_stop_covering_qty"),
                    ("submit_stop_limit_order", "_submit_stop_limit_order"),
                    ("submit_stop_legs", "_submit_stop_legs"),
                    ("restore_stop_orders", "_restore_stop_orders"),
                )
                if not _is_broker_class_shim(getattr(self, attr, None), attr)
            },
        )

    def _existing_stop_covering_qty(self, *args, **kwargs):
        """Thin shim: body moved to src/execution/broker_parts/stop_place.py."""
        return self._stop_placer()._existing_stop_covering_qty(*args, **kwargs)

    def _submit_stop_leg_retrying(self, *args, **kwargs):
        """Thin shim: body moved to src/execution/broker_parts/stop_place.py."""
        return self._stop_placer()._submit_stop_leg_retrying(*args, **kwargs)

    def _submit_protective_stop_retrying(self, *args, **kwargs):
        """Thin shim: body moved to src/execution/broker_parts/stop_place.py."""
        return self._stop_placer()._submit_protective_stop_retrying(*args, **kwargs)

    def close_position(self, *args, **kwargs):
        """Thin shim: body moved to src/execution/broker_parts/order_desk.py."""
        return self._order_desk().close_position(*args, **kwargs)

    def _list_open_stop_orders_by_side(
        self, symbol: str, *, errors: list | None = None,
    ) -> tuple[list, list]:
        """Single order-book fetch for `symbol`, split into (sell_stops, buy_stops).

        A long's protective stop is a SELL stop; a short's is a BUY stop.
        `replace_stop_loss` needs to know WHICH side a symbol's live stop is
        on before it can decide what to list/cancel/ratchet-check — but it
        can't yet know the position's direction without a second API call.
        Fetching once and filtering both ways here answers "which side has a
        stop" from a single snapshot, rather than two separate fetches that
        could each see a different broker state.
        """
        try:
            from alpaca.trading.requests import GetOrdersRequest

            orders = self.client.get_orders(
                filter=GetOrdersRequest(
                    status=QueryOrderStatus.OPEN,
                    symbols=[_alpaca_symbol(symbol)],
                    nested=True,
                )
            )
        except Exception as exc:
            logger.warning("replace_stop_loss: failed to list open orders for %s: %s", symbol, exc)
            # Board item 172 — same contract as the sell-side lister above.
            if errors is not None:
                errors.append(f"open-order listing failed: {exc}")
            return [], []

        sell_orders: list = []
        buy_orders: list = []
        for order in orders or []:
            order_type = str(getattr(getattr(order, "order_type", None), "value",
                                    getattr(order, "order_type", ""))).lower()
            if "stop" not in order_type:
                continue
            order_side = str(getattr(getattr(order, "side", None), "value",
                                    getattr(order, "side", ""))).lower()
            if order_side == "sell":
                sell_orders.append(order)
            elif order_side == "buy":
                buy_orders.append(order)
        return sell_orders, buy_orders

    def _list_open_protective_stop_orders(
        self, symbol: str, *, side: str = "sell", errors: list | None = None,
    ) -> list:
        """List open stop orders on `side` for `symbol`.

        `side="sell"` (default) finds the stops protecting a long — the only
        case that existed before shorts were countable, and delegates to
        `_list_open_sell_stop_orders` (the name a long list of tests and
        call sites pin) rather than duplicating it. `side="buy"` finds the
        stops protecting a short: a short's protective order is a BUY stop,
        so a filter hardcoded to "sell" made a short's live stop invisible
        to every caller (coverage reconcile would report a perfectly
        protected short as NAKED and try to "repair" over it).
        """
        if side.lower() == "buy":
            _, buy_orders = self._list_open_stop_orders_by_side(
                symbol, errors=errors,
            )
            return buy_orders
        return self._list_open_sell_stop_orders(symbol, errors=errors)

    def _list_open_sell_stop_orders(self, symbol: str, *, errors: list | None = None) -> list:
        """Board item 172: `errors`, when given, receives the listing
        failure instead of it being swallowed into an empty list.

        The empty-list return is UNCHANGED for every caller that does not
        pass `errors`, because `replace_stop_loss` and its tests depend on
        it. What changes is that a caller who needs to tell "no stops" from
        "could not ask" can now do so — and `snapshot_protective_stops` is
        exactly that caller.
        """
        try:
            from alpaca.trading.requests import GetOrdersRequest

            orders = self.client.get_orders(
                filter=GetOrdersRequest(
                    status=QueryOrderStatus.OPEN,
                    symbols=[_alpaca_symbol(symbol)],
                    nested=True,
                )
            )
        except Exception as exc:
            logger.warning("replace_stop_loss: failed to list open orders for %s: %s", symbol, exc)
            if errors is not None:
                errors.append(f"open-order listing failed: {exc}")
            return []

        stop_orders = []
        for order in orders or []:
            order_type = str(getattr(getattr(order, "order_type", None), "value",
                                    getattr(order, "order_type", ""))).lower()
            order_side = str(getattr(getattr(order, "side", None), "value",
                                    getattr(order, "side", ""))).lower()
            if "stop" in order_type and order_side == "sell":
                stop_orders.append(order)
        return stop_orders

    @staticmethod
    def _snapshot_stop_order(order) -> dict | None:
        try:
            qty = float(getattr(order, "qty", 0) or 0)
        except (TypeError, ValueError):
            qty = 0.0
        try:
            stop_price = float(getattr(order, "stop_price", 0) or 0)
        except (TypeError, ValueError):
            stop_price = 0.0
        try:
            limit_price = float(getattr(order, "limit_price", 0) or 0)
        except (TypeError, ValueError):
            limit_price = 0.0
        if qty <= 0 or stop_price <= 0:
            return None
        return {
            "id": str(order.id),
            "qty": qty,
            "stop_price": stop_price,
            "limit_price": limit_price or None,
        }

    def _submit_stop_limit_order(self, *args, **kwargs):
        """Thin shim: body moved to src/execution/broker_parts/stop_place.py."""
        return self._stop_placer()._submit_stop_limit_order(*args, **kwargs)

    def _submit_stop_legs(self, *args, **kwargs):
        """Thin shim: body moved to src/execution/broker_parts/stop_place.py."""
        return self._stop_placer()._submit_stop_legs(*args, **kwargs)

    def _restore_stop_orders(self, *args, **kwargs):
        """Thin shim: body moved to src/execution/broker_parts/stop_place.py."""
        return self._stop_placer()._restore_stop_orders(*args, **kwargs)

    def shift_stops_down(self, *args, **kwargs):
        """Thin shim: body moved to src/execution/broker_parts/stop_place.py."""
        return self._stop_placer().shift_stops_down(*args, **kwargs)

    _AMEND_DEAD_STATES = StopAmender._AMEND_DEAD_STATES

    def _stop_amender(self) -> StopAmender:
        """Thin shim: builds the standalone amender from this broker's collaborators
        (bodies moved to src/execution/broker_parts/stop_amend.py)."""
        return StopAmender(
            client=self.client,
            list_open_stop_orders_by_side=self._list_open_stop_orders_by_side,
            snapshot_stop_order=self._snapshot_stop_order,
        )

    _failed_amend_payload = staticmethod(StopAmender._failed_amend_payload)

    def _classify_after_dead_replacement(self, *, symbol: str, spec: dict, new_price: float, leg: dict) -> str:
        """Thin shim: body moved to src/execution/broker_parts/stop_amend.py."""
        return self._stop_amender()._classify_after_dead_replacement(symbol=symbol, spec=spec, new_price=new_price, leg=leg)

    def _amend_one_stop_price(self, *, symbol: str, spec: dict, new_price: float) -> dict:
        """Thin shim: body moved to src/execution/broker_parts/stop_amend.py."""
        return self._stop_amender()._amend_one_stop_price(symbol=symbol, spec=spec, new_price=new_price)

    _stop_order_amendable_in_place = staticmethod(StopAmender._stop_order_amendable_in_place)

    def _amend_resting_stop_price(self, *, symbol: str, live_orders: list, stop_specs: list[dict], new_stop_price: float, position_qty: float):
        """Thin shim: body moved to src/execution/broker_parts/stop_amend.py."""
        return self._stop_amender()._amend_resting_stop_price(symbol=symbol, live_orders=live_orders, stop_specs=stop_specs, new_stop_price=new_stop_price, position_qty=position_qty)

    def replace_stop_loss(self, *args, **kwargs):
        """Thin shim: body moved to src/execution/broker_parts/stop_place.py."""
        return self._stop_placer().replace_stop_loss(*args, **kwargs)
