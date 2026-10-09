"""The `trade_updates` reconnect guard and attempt budget, lifted verbatim from trade_stream.py."""

from __future__ import annotations

import asyncio
import logging
import random
import threading
import time

from src.execution.broker_parts.trade_stream_auth import TradeStreamAuthRejected
from src.execution.broker_parts.trade_stream_bounds import (
    _ALPACA_STREAM_RECONNECT_MAX_S,
    _ALPACA_STREAM_RECONNECT_MIN_S,
)
from src.execution.broker_parts.trade_stream_errors import _stream_http_status, _stream_retry_after_seconds
from src.execution.broker_parts.trade_stream_flags import _signal_event
from src.sentinel.guarded import record_guarded_pass
from src.trading_calendar import session_date_key

logger = logging.getLogger("src.execution.broker")

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
        _STREAM_ATTEMPT_BUDGET.attempts_today(),
        reason,
        message,
    )
    try:
        from src.notifier import send_owner_alert

        send_owner_alert(message)
    except Exception as exc:  # noqa: BLE001 - never let the alert sink break execution
        record_guarded_pass(
            None, "trade_stream.give_up_alert", exc, log=logger, context={"effect": "owner not told the stream gave up"}
        )


class TradeStreamGaveUp(Exception):
    """The socket has stopped retrying for the day and will not reopen.

    Distinct from `TradeStreamAuthRejected`: that says the broker refused
    a credential, this says the DESK refused to keep asking. Callers treat
    it like any other handshake failure and fall through to the bounded
    REST fill path; it exists so the log and the tests can tell "we gave
    up" apart from "it failed again".
    """


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
    attempt: int,
    exc: BaseException,
    stream: object | None = None,
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
                record_guarded_pass(
                    None,
                    "trade_stream.reconnect.stop_flag_exhausted",
                    exc,
                    log=logger,
                    context={"effect": "retry loop may keep running"},
                )
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
            _signal_event(stream, "_qamc_authed", "set")
        except Exception as exc:
            _signal_event(stream, "_qamc_authed", "clear")
            _signal_event(stream, "_qamc_connected", "clear")
            failures += 1
            spent_today = _STREAM_ATTEMPT_BUDGET.record_attempt()
            status = _stream_http_status(exc)
            # CEILINGS. Either one being reached ends the socket for good --
            # the session ceiling because the backoff curve has saturated and
            # further attempts cannot learn anything new, the daily ceiling
            # because the account's published rate-limit budget belongs to
            # the order path. Both are checked BEFORE the sleep so an
            # exhausted budget never buys another wait.
            if failures >= _STREAM_ATTEMPT_CEILING_PER_SESSION or _STREAM_ATTEMPT_BUDGET.day_exhausted():
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
                    record_guarded_pass(
                        None,
                        "trade_stream.reconnect.stop_flag_giveup",
                        exc,
                        log=logger,
                        context={"effect": "retry loop may keep running"},
                    )
                event.set()
                logger.warning(
                    "trade_updates websocket give-up: %d attempts this "
                    "session (ceiling %d), %d today (ceiling %d), last "
                    "status=%s",
                    failures,
                    _STREAM_ATTEMPT_CEILING_PER_SESSION,
                    spent_today,
                    _STREAM_ATTEMPT_CEILING_PER_DAY,
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
                        failures,
                        delay,
                    )
                else:
                    logger.warning(
                        "trade_updates websocket handshake failed (status=%s, attempt %d); reconnect in %.1fs",
                        status if status is not None else "unknown",
                        failures,
                        delay,
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
