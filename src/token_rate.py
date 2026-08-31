"""A hard ceiling on how fast we are willing to send tokens to a provider.

WHY THIS EXISTS
---------------
On 2026-08-31 the desk was rate-limited out of its morning session. Every one
of the eleven rate limits in three weeks of logs hit the SAME agent — the
Technical Analyst — and no other agent was ever declined. It is the only one
that sends a burst: ~314,000 tokens inside 80 seconds, about 252,000 tokens
per minute, while every other agent on the desk sends 20-30k in a single call.

The provider was not misbehaving. It was declining a burst, correctly.

Nothing in the system bounded that burst. Concurrency was capped (three
requests at once) and per-call context was capped, but a rate limit is
measured in tokens per minute, and three concurrent 80,000-token requests
clear both caps while being exactly the thing that gets refused. The desk
relied on the provider to say no, and then treated being told no as an
emergency.

This is the owner's instruction, made mechanical: never flood the provider.
What the code can honestly promise is a ceiling on what WE send — the
provider's own pooled limits are unpublished and move, so a promise about
their side would be a promise we cannot keep.

HOW IT BEHAVES
--------------
A sliding one-minute window of tokens actually sent. Before a request, its
estimated size is charged against the window; if that would breach the
ceiling, the caller WAITS until the window drains enough to fit. After the
response, the estimate is reconciled to the provider's real usage count, so
the window self-corrects and a bad estimator cannot silently inflate or
deflate the governor.

Waiting is bounded (`max_wait_s`). A trading session has a hard outer kill,
and a governor that could stall indefinitely would trade one outage for
another. If the wait budget is exhausted the request proceeds anyway and the
breach is logged at CRITICAL with the numbers — loud, auditable, and still
alive. Silence would be the worse failure: nobody would learn the ceiling is
set wrong.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque

logger = logging.getLogger(__name__)

WINDOW_S = 60.0


class TokenRateGovernor:
    """Sliding-window tokens-per-minute ceiling for one provider domain."""

    def __init__(
        self,
        name: str,
        tokens_per_minute: int,
        *,
        max_wait_s: float = 120.0,
        sleep=time.sleep,
        clock=time.monotonic,
    ):
        if tokens_per_minute <= 0:
            raise ValueError(
                f"{name}: tokens_per_minute must be positive, got {tokens_per_minute}"
            )
        self.name = name
        self.tokens_per_minute = int(tokens_per_minute)
        self.max_wait_s = float(max_wait_s)
        self._sleep = sleep
        self._clock = clock
        self._events: deque[tuple[float, int]] = deque()
        self._lock = threading.Lock()
        self.waits = 0
        self.breaches = 0
        self.total_wait_s = 0.0

    # ------------------------------------------------------------- internal

    def _trim_locked(self, now: float) -> int:
        cutoff = now - WINDOW_S
        while self._events and self._events[0][0] <= cutoff:
            self._events.popleft()
        return sum(tokens for _, tokens in self._events)

    def _wait_needed_locked(self, now: float, tokens: int) -> float:
        """Seconds until `tokens` would fit under the ceiling, 0 if it fits now."""
        in_window = self._trim_locked(now)
        if in_window + tokens <= self.tokens_per_minute:
            return 0.0
        # Drain oldest-first until enough room exists; the wait is however
        # long until that many tokens age out of the window.
        need = in_window + tokens - self.tokens_per_minute
        freed = 0
        for stamp, amount in self._events:
            freed += amount
            if freed >= need:
                return max(0.0, (stamp + WINDOW_S) - now)
        # Even an empty window cannot fit this single request: it is larger
        # than the whole per-minute budget. Waiting cannot help, so don't.
        return 0.0

    # --------------------------------------------------------------- public

    def charge(self, tokens: int) -> float:
        """Block until `tokens` fit under the ceiling, then record them.

        Returns the seconds spent waiting. A single request larger than the
        entire per-minute budget cannot ever fit and is let through rather
        than stalled forever — the ceiling is a rate limit, not a size limit,
        and refusing outright would strand the session with no analysis.
        """
        tokens = max(0, int(tokens))
        waited = 0.0
        deadline = self._clock() + self.max_wait_s
        while True:
            with self._lock:
                now = self._clock()
                wait = self._wait_needed_locked(now, tokens)
                if wait <= 0.0:
                    self._events.append((now, tokens))
                    if waited > 0:
                        self.waits += 1
                        self.total_wait_s += waited
                        logger.warning(
                            "%s token-rate governor held a request for %.1fs to "
                            "stay under %d tokens/min (request ~%d tokens). This "
                            "is the desk pacing itself, not a provider error.",
                            self.name, waited, self.tokens_per_minute, tokens,
                        )
                    return waited
                if now >= deadline:
                    in_window = self._trim_locked(now)
                    self._events.append((now, tokens))
                    self.breaches += 1
                    logger.critical(
                        "%s token-rate governor EXCEEDED its ceiling: sending "
                        "~%d tokens with %d already in the last minute, over the "
                        "%d/min limit, after waiting the full %.0fs budget. "
                        "Proceeding so the session is not stranded, but the "
                        "ceiling or the request size is wrong — this is the "
                        "condition the governor exists to prevent.",
                        self.name, tokens, in_window, self.tokens_per_minute,
                        self.max_wait_s,
                    )
                    return waited
                nap = min(wait, max(0.05, deadline - now))
            self._sleep(nap)
            waited += nap

    def reconcile(self, estimated: int, actual: int) -> None:
        """Replace an estimate with the provider's real usage count.

        The window must reflect what was really sent, not what an estimator
        guessed. Without this, a systematically wrong estimator would quietly
        make the ceiling mean something other than it says.
        """
        delta = int(actual) - int(estimated)
        if not delta:
            return
        with self._lock:
            now = self._clock()
            self._trim_locked(now)
            if self._events:
                stamp, amount = self._events[-1]
                self._events[-1] = (stamp, max(0, amount + delta))
            elif delta > 0:
                self._events.append((now, delta))

    def snapshot(self) -> dict:
        with self._lock:
            now = self._clock()
            return {
                "name": self.name,
                "tokens_per_minute_limit": self.tokens_per_minute,
                "tokens_in_window": self._trim_locked(now),
                "waits": self.waits,
                "breaches": self.breaches,
                "total_wait_s": round(self.total_wait_s, 2),
            }
