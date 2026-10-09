"""Retry a live-price read, then make its FAILURE distinct from ABSENCE.

Owner ruling 2026-10-08: "if price can't be read, that's a fundamental
absolute deal breaker". The broker's read used to catch every error, log a
warning and return the same `None` a measured "nothing quotable" returns, so
a timed-out feed looked exactly like a name with no print. Here a read is
retried first (most failures are momentary) and, when every attempt fails,
raises `PriceReadFailed` -- never `None`. `None` stays what it means: the API
answered and there is nothing quotable for that name.

The retry is the SAME read against the SAME provider. It is a retry, not a
second source, and nothing here describes it as one.

The attempt count and backoff are the desk's ledgered transient-fault policy
(`src.infra_retry_policy`), so this module adds no number of its own.
"""

from __future__ import annotations

import logging
import time
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Callable, Iterator

from src.infra_retry_policy import (  # noqa: F401 (re-exported for callers and tests)
    BACKOFF_BASE_S,
    BACKOFF_MAX_S,
    MAX_RETRIES,
    _backoff_s,
    read_worst_case_s,
)
from src.refusal_errors import PriceReadFailed

logger = logging.getLogger(__name__)
_sleep = time.sleep  # module attribute so a test can stub the backoff

# Set by `single_attempt_reads` for a caller that already runs its OWN retry
# loop (the stop repair): nesting this retry inside it would multiply the
# reads and the waits before naked shares are protected.
_SINGLE_ATTEMPT: ContextVar[bool] = ContextVar("price_read_single_attempt", default=False)


@contextmanager
def single_attempt_reads() -> Iterator[None]:
    """Inside this block every `read_price_with_retry` reads ONCE (no backoff)
    and still raises `PriceReadFailed` on failure. For a caller whose own loop
    is the retry; the caller's worst case is then exactly its own."""
    token = _SINGLE_ATTEMPT.set(True)
    try:
        yield
    finally:
        _SINGLE_ATTEMPT.reset(token)


def read_price_with_retry(
    read_once: Callable[[str], object],
    symbol: str,
    *,
    log: logging.Logger | None = None,
    sleep: Callable[[float], None] | None = None,
):
    """`read_once(symbol)` up to `1 + MAX_RETRIES` times; the first answer wins.

    An answer of `None` is a measured absence and is returned as such. An
    exception is retried after the policy's backoff; when the last attempt
    also raises, `PriceReadFailed` is raised with the final error as its
    cause, so the caller can tell "could not read" from "nothing to read".
    """
    log = log or logger
    sleep = sleep or _sleep
    attempts = 1 if _SINGLE_ATTEMPT.get() else 1 + MAX_RETRIES
    last: BaseException | None = None
    for attempt in range(1, attempts + 1):
        try:
            return read_once(symbol)
        except PriceReadFailed:
            raise
        except Exception as exc:  # noqa: BLE001 -- retried, then raised typed below
            last = exc
            log.warning("price read for %s failed (attempt %d of %d): %s", symbol, attempt, attempts, exc)
            if attempt < attempts:
                sleep(_backoff_s(attempt))
    raise PriceReadFailed(f"{symbol}: price read failed on all {attempts} attempts ({last!r})") from last
