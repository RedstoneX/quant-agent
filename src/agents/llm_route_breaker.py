"""Route ladder breakers and half-open return to the primary.

Moved VERBATIM out of src/agents/base.py; base.py re-exports every name.
"""

import os
import threading
import time
from src.agents.llm_retry import _RETRY_AFTER_CAP_S


# === Route ladder and half-open return to the primary =====================
#
# The owner's ruling, verbatim: "a backup is temporary; the third backup is
# temporary; it should ALWAYS be retrying the primary."
#
# Before this, one failed call moved a seat off the free Google-direct
# primary and NOTHING ever moved it back for the life of the process. A
# transient 503 therefore bought a permanent migration onto a paid route.
#
# The breaker is keyed on the PROVIDER ALONE and is process-wide, not
# per-agent-instance and NOT per (provider, model) pair.
#
# Keying it on the pair was the first draft and it was wrong. The failure
# domain of every limit that actually fires here is the ACCOUNT, not the
# model: Google AI Studio's free tier is a per-PROJECT RPM/TPM ceiling (see
# _GOOGLE_TOKENS_PER_MIN), and OpenRouter's limits are per-account. Three
# seats run three different OpenRouter models (openai/gpt-5.5,
# qwen/qwen3-235b-a22b-2507, google/gemini-2.5-flash-lite), so a pair-keyed
# breaker fragments ONE shared quota into three independent breakers that
# each have to learn the same outage separately and pay for it separately.
# Provider-keying makes the breaker's unit the same unit the provider
# rate-limits.
#
# Process-wide, because the morning fan-out is a ThreadPoolExecutor with five
# branches (src/pipeline_stages.py) against a single key, plus whatever
# scheduler job overlaps; a per-instance breaker would have every one of them
# discover the same outage independently and pay a full ladder for it.
#
# KNOWN LIMITATION, stated rather than hidden: this breaker lives in process
# memory, while the cost circuit is cross-process via SQLite. A demotion
# learned by the morning run is not known to the close run, and the 30-minute
# ceiling below can never be reached by a run shorter than that. Making it
# durable means another table and another set of staleness rules; the
# in-memory version already fixes the defect it was built for (a demotion
# that NEVER ends), and the durable version can be added without changing
# any call site here.
#
# COOLDOWN, and why neither number is invented here. Both are DERIVED from
# constants this repo already sources, and both are asserted against their
# bases by a test so a moving base cannot silently orphan them.
#
#   Base cooldown = `_RETRY_AFTER_CAP_S` (120s). That constant is the longest
#   wait the desk will honour when a provider explicitly TELLS it how long to
#   stay away. A provider that has told us nothing and failed every attempt
#   has given us strictly less reason to come back soon than one that named a
#   number, so the shortest defensible cooldown is the longest hint we would
#   have obeyed. Below it, the desk would be probing a door the same provider
#   might have said is shut for longer than that.
#
#   Ceiling = 60 * `src.config.INTRA_CHECK_TICK_MINUTES` (30 min -> 1800s).
#   That is the gap between consecutive PAID runs, already sourced in the
#   ledger off `src/scheduler.py`'s real trigger, and already used to derive
#   the cost circuit's own transient-latch cooldown. It is the right ceiling
#   for the same reason it is the right ceiling there: a breaker held down
#   longer than the gap between runs can never be probed by the next run, so
#   the "always be retrying the primary" half of the owner's ruling would
#   quietly stop being true. Not imported, because `src.agents.base` importing
#   `src.config` is circular; `tests/test_route_failover_ladder.py` asserts
#   the two agree instead.
#
# The doubling factor of 2 is conventional binary exponential backoff, the
# same factor `_retry_backoff_seconds` uses.
_ROUTE_COOLDOWN_S = float(os.environ.get("QUANT_AGENT_ROUTE_COOLDOWN_S", _RETRY_AFTER_CAP_S))
_ROUTE_COOLDOWN_MAX_S = float(os.environ.get("QUANT_AGENT_ROUTE_COOLDOWN_MAX_S", 60.0 * 30.0))


class RouteBreaker:
    """Half-open breaker for ONE provider (the account-level failure domain).

    Three states, in the standard circuit-breaker vocabulary:

      CLOSED    — `demoted_until is None`. The primary is used normally.
      OPEN      — now < demoted_until. The primary is SKIPPED; calls start at
                  the secondary. This is the state that saves time and, as it
                  happens, also saves the cost latch (see `should_probe`).
      HALF_OPEN — now >= demoted_until. Exactly ONE caller is handed the
                  right to probe the primary; everyone else keeps skipping it
                  until that probe reports back. Without the single-probe
                  rule a four-way fan-out arriving one millisecond after the
                  cooldown expires would send four probes at a provider that
                  is very likely still saturated, which is the thundering
                  herd the cooldown exists to prevent.
    """

    def __init__(self, key: str):
        self.key = key
        self._lock = threading.Lock()
        self._demoted_until: float | None = None
        self._cooldown_s: float = _ROUTE_COOLDOWN_S
        self._probe_in_flight = False

    # --- state queries ----------------------------------------------------

    def primary_available(self) -> bool:
        """True when a caller may use the primary as its first route.

        True in CLOSED, and true for the single caller that wins the
        half-open probe. False while OPEN and for every loser of the probe
        race.
        """
        with self._lock:
            if self._demoted_until is None:
                return True
            if time.monotonic() < self._demoted_until:
                return False
            if self._probe_in_flight:
                return False
            self._probe_in_flight = True
            return True

    def is_probing(self) -> bool:
        with self._lock:
            return self._probe_in_flight

    def demoted(self) -> bool:
        with self._lock:
            return self._demoted_until is not None and time.monotonic() < self._demoted_until

    # --- state transitions ------------------------------------------------

    def record_success(self) -> bool:
        """The primary answered. Returns True if this CLEARED a demotion."""
        with self._lock:
            was_demoted = self._demoted_until is not None
            self._demoted_until = None
            self._cooldown_s = _ROUTE_COOLDOWN_S
            self._probe_in_flight = False
            return was_demoted

    def record_failure(self) -> float:
        """The primary failed. Demote it and return the cooldown applied.

        A failure while probing extends the cooldown (exponential, capped);
        a first failure applies the base cooldown. The probe right is always
        released, or a crashed probe would wedge the breaker permanently
        half-open and the primary would never be tried again — the exact
        never-comes-back defect this class exists to remove.
        """
        with self._lock:
            if self._demoted_until is not None:
                self._cooldown_s = min(self._cooldown_s * 2.0, _ROUTE_COOLDOWN_MAX_S)
            else:
                self._cooldown_s = _ROUTE_COOLDOWN_S
            self._demoted_until = time.monotonic() + self._cooldown_s
            self._probe_in_flight = False
            return self._cooldown_s

    def reset(self) -> None:
        with self._lock:
            self._demoted_until = None
            self._cooldown_s = _ROUTE_COOLDOWN_S
            self._probe_in_flight = False


_ROUTE_BREAKERS: dict[str, RouteBreaker] = {}
_ROUTE_BREAKERS_LOCK = threading.Lock()


def route_breaker_for(provider: str) -> RouteBreaker:
    """The process-wide breaker for this PROVIDER (created on demand).

    Keyed on the provider alone — see the note above `_ROUTE_COOLDOWN_S` for
    why the (provider, model) pair is the wrong unit.
    """
    key = provider
    with _ROUTE_BREAKERS_LOCK:
        breaker = _ROUTE_BREAKERS.get(key)
        if breaker is None:
            breaker = RouteBreaker(key)
            _ROUTE_BREAKERS[key] = breaker
        return breaker


def reset_route_breakers() -> None:
    """Forget every route demotion this process is currently holding.

    The breakers are process-wide and deliberately outlive one session, so a
    provider that just failed is not re-attempted by the next caller. That is
    right in production, where the process IS the desk, and wrong for anything
    that runs two independent sessions back to back in one process: the second
    session then starts at the secondary route because of something that
    happened in the first, and its verdict is a function of its predecessor
    rather than of the code under test. The rehearsal harness calls this at
    the start of every run for exactly that reason
    (`ops/rehearsal/runner.py`); nothing in the live pipeline calls it.
    """
    with _ROUTE_BREAKERS_LOCK:
        _ROUTE_BREAKERS.clear()


def _reset_route_breakers_for_tests() -> None:
    reset_route_breakers()
