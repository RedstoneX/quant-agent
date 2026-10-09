"""Retry/backoff policy, retry-after hints, error classification.

Moved VERBATIM out of src/agents/base.py; base.py re-exports every name.
"""

import os
import random
import re


_DEFAULT_MAX_RETRIES = 2


# Ceiling on one exponential-backoff sleep, seconds. Full jitter without a
# cap is unbounded in the attempt index; the cap is what makes the published
# formula usable. 60s sits well inside the 480s `_DEFAULT_RETRY_DEADLINE_S`
# so a single sleep can never consume the whole primary window on its own.
_BACKOFF_CAP_S = 60.0


def _retry_backoff_seconds(attempt: int) -> float:
    """AWS "Full Jitter": ``random_between(0, min(cap, base * 2**attempt))``.

    SOURCE: Marc Brooker, "Exponential Backoff And Jitter", AWS Architecture
    Blog (https://aws.amazon.com/blogs/architecture/exponential-backoff-and-jitter/).
    Quoted formula: ``sleep = random(0, min(cap, base * 2 ** attempt))``.

    WHY THE DETERMINISTIC FLOOR WENT AWAY. The previous helper returned
    ``2**attempt + uniform(0, 2**attempt)`` — i.e. a guaranteed minimum wait
    equal to the full exponential term, with jitter only ADDED on top. Its
    docstring defended that floor as "preserving exponential spacing". The
    AWS article is a direct measurement of exactly that trade-off, and its
    result is the opposite: schemes that keep a deterministic floor complete
    the same work in MORE total time and with MORE competing calls than Full
    Jitter, because the floor re-synchronises every client that failed at the
    same instant onto the same next instant. A morning fan-out of four agents
    against one saturated free-tier Google key is precisely that case.

    The floor also bought nothing here that the cap does not: with only two
    primary attempts (`_DEFAULT_MAX_RETRIES`) the "retries bunch at the
    start" failure mode the floor guarded against is a single sleep, and the
    error-aware dispatcher below independently honours a server's own
    Retry-After when the server states one — which is a far better lower
    bound than a self-invented constant.

    THE ONE THING FULL JITTER ALONE GETS WRONG HERE, and how it is fixed.
    With `_DEFAULT_MAX_RETRIES = 2` there is exactly ONE sleep in the whole
    primary loop, so pure Full Jitter can return a few milliseconds and
    re-hit a 15-requests-per-minute free-tier ceiling immediately. The
    provider that actually fails on this desk says so itself: Google's Gemini
    API troubleshooting page (https://ai.google.dev/gemini-api/docs/
    troubleshooting, verified 2026-09-23) recommends, for 429
    RESOURCE_EXHAUSTED and 503 UNAVAILABLE, "Wait a short time before the
    first retry (for example, 1 second), then increase the delay
    exponentially (for example, 2s, 4s, 8s)" and "Add random 'jitter'".
    `_MIN_CAPACITY_BACKOFF_S` below is that 1 second, and it is applied by
    the error-aware dispatcher to the rate-limit/capacity classes ONLY — the
    classes Google's page is actually about. It is a floor read off a
    provider's own published guidance, not a constant invented here.

    Sequence of upper bounds for attempt 0..5: 1, 2, 4, 8, 16, 32 (then
    capped at 60). Each returned value is uniform in [0, bound).
    """
    return random.uniform(0.0, min(_BACKOFF_CAP_S, float(2**attempt)))


# Google's own documented "wait a short time before the first retry (for
# example, 1 second)" for 429/503 — see `_retry_backoff_seconds`. Applied
# only to the capacity/rate-limit classes, and only when the server did NOT
# state a Retry-After of its own (a stated hint always wins; this is the
# floor for when there is no hint, which on Google is ALWAYS, because that
# same page documents no Retry-After header and no RetryInfo).
_MIN_CAPACITY_BACKOFF_S = 1.0

# Per-request HTTP timeout for LLM clients. OpenAI/Anthropic SDKs default to
# 600s, which means a single stalled SSE stream could hang the morning
# window. We pin an explicit ceiling below that default so one bad call
# can't eat the whole session, but the ceiling has to sit above the
# *legitimate* response latency of the slowest agent — otherwise a
# normally-succeeding call gets axed mid-flight and retry-spirals.
#
# tech_analyst is the outlier: max_tokens=128K and 25-symbol batched
# chunks. Historical happy-path chunks took 60-180s (2026-04-21/22),
# and 2026-04-24 showed OpenAI running slower with single chunks
# exceeding 180s — the initial 60s pin axed those calls even though
# they'd have returned successfully, triggering retry loops that blew
# past launchd's 600s outer kill. 300s covers that tail with buffer,
# stays below the SDK default, and still bounds worst-case single-call
# hang at 5 min. Mirrors the _BROKER_HTTP_TIMEOUT discipline in
# src/execution/broker.py.
_LLM_HTTP_TIMEOUT = 300.0

# Wall-clock deadline for the PRIMARY retry loop in _execute(), seconds.
#
# Why a deadline at all: the attempt budget alone doesn't bound time. Under
# the relay's Cloudflare 524 mode each attempt burned 120-380s, so exhausting
# 7 attempts needed 40+ minutes — the wrapper SIGKILLed the session at 1200s
# mid-loop and the Anthropic failover (which fires only AFTER the loop) never
# ran in exactly the sustained-outage scenario it was built for (2026-06-08/09:
# two days of mornings died with a funded failover key sitting idle).
#
# Why 480: it must leave room for one full failover call inside the wrapper's
# 1200s kill. Worst-case failover = one Anthropic call bounded by
# _LLM_HTTP_TIMEOUT (300s), so 480 + 300 = 780s per agent, ~420s of headroom
# for the rest of the session. How many primary attempts 480s buys depends on
# the failure mode and is NOT a fixed 2-4 (an earlier version of this comment
# claimed that while the loop was hard-capped at _max_retries()=2 regardless).
# In the slow-failure mode each attempt burns ~120-380s, so the deadline is
# what stops the loop; on a fast capacity refusal the attempts are cheap and
# `capacity_max_attempts()` derives how many the backoff schedule fits inside
# this same deadline. Either way the deadline, not a hand-picked count, is the
# bound.
#
# Overridable via QUANT_AGENT_RETRY_DEADLINE_S (read at call time, like
# _max_retries, so tests can monkeypatch per case).
_DEFAULT_RETRY_DEADLINE_S = 480.0


def _retry_deadline_s() -> float:
    raw = os.environ.get("QUANT_AGENT_RETRY_DEADLINE_S")
    if raw is None:
        return _DEFAULT_RETRY_DEADLINE_S
    try:
        v = float(raw)
    except ValueError:
        return _DEFAULT_RETRY_DEADLINE_S
    return max(1.0, v)


# Server-provided retry hints. The relay's 429 ("Concurrency limit exceeded")
# and 524 payloads carry retry-after semantics (a Retry-After header and/or a
# "retry_after": N field in the JSON body) that pure exponential jitter
# ignored — agents retried in 2-15s against a server that said "come back in
# 120s", burning attempts for nothing. We sleep max(backoff, hint), capped so
# a hostile/buggy hint can't park an agent past the session window.
_RETRY_AFTER_CAP_S = 120.0


def _retry_after_hint_seconds(exc: Exception) -> float | None:
    """Best-effort extraction of a server retry-after hint from an SDK error.

    Looks in (a) the Retry-After header of the attached httpx response
    (numeric-seconds form only — the HTTP-date form isn't worth parsing for
    a hint), (b) a retry_after field in the error body dict, (c) the message
    text (relay 524 bodies embed '"retry_after": 120'). Returns None when no
    usable hint exists; never raises.
    """
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None)
    if headers is not None:
        try:
            raw = headers.get("retry-after")
        except Exception:  # noqa: BLE001 — a weird headers object must not mask the real error
            raw = None
        if raw is not None:
            try:
                return max(0.0, float(raw))
            except (TypeError, ValueError):
                pass
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        val = body.get("retry_after", body.get("retry-after"))
        if isinstance(val, (int, float)) and not isinstance(val, bool):
            return max(0.0, float(val))
    m = re.search(r'retry[_-]after["\']?\s*[:=]\s*"?(\d+(?:\.\d+)?)', str(exc), re.IGNORECASE)
    if m:
        return float(m.group(1))
    return None


# === Error-aware backoff taxonomy =========================================
#
# The owner's ruling: "back off by a sensible number of minutes or retry
# randomly within a short window, DEPENDING ON WHAT THE ERROR SAYS." Three
# classes, each read from a provider's own published error reference rather
# than invented here. Every claim below carries its source; the ones we could
# NOT verify are called out explicitly as unverified, not quietly assumed.
#
# CLASS "retry_after" — a 429 that states when to come back.
#   OpenAI: "Follow the `Retry-After` header when it's present, then retry
#   your request." — developers.openai.com/api/docs/guides/error-codes
#   [verified 2026-09-23], which documents the header on both 429 and 503.
#   Anthropic: "The official SDKs automatically retry transient failures ...
#   honoring the `retry-after` header when present."
#   — platform.claude.com/docs/en/api/errors [verified 2026-09-23]. That page
#   also documents one 429 that carries NO retry-after (the usage-tier spend
#   cap) and keeps failing until access resumes — so the absence of a header
#   on a 429 is a real, documented case, not a parsing failure, and it falls
#   through to the jitter class below rather than to a guessed constant.
#   We honour a stated hint verbatim, floor 0, capped at _RETRY_AFTER_CAP_S.
#
# CLASS "jitter" — capacity/transient: 429 with no hint, 500, 502, 503, 504,
#   529, and every transient transport class in _RETRYABLE_EXC_NAMES.
#   OpenAI documents 500 ("server errors") and 503 ("model temporarily
#   overloaded") [verified 2026-09-23]. Anthropic documents 500 `api_error`
#   with the explicit instruction "Retry the request with exponential
#   backoff", 504 `timeout_error`, and 529 `overloaded_error` "The API is
#   temporarily overloaded" [verified 2026-09-23]. Google AI Studio's
#   "This model is currently experiencing high demand" 503 is recorded in
#   src/cost_circuit.py from THIS desk's own 2026-09-22 production log
#   [measured, 17 occurrences] — that one is a first-party measurement, not
#   a doc citation.
#   NOT VERIFIED: 502. No provider reference consulted here documents a 502;
#   it is included because an intermediary (Cloudflare, a load balancer) can
#   emit one in front of any of them, which is an inference from HTTP
#   semantics, not from a provider's published taxonomy. Treated as transient
#   because the conservative direction for an unknown 5xx is to retry.
#   NOT VERIFIED: OpenRouter publishes no error-code reference we fetched;
#   its statuses are assumed to be the upstream provider's, passed through.
#
# CLASS "fatal" — never retried: 400, 401, 403, 404 (and 402, handled by
#   _is_retryable already). Both references above describe these as request-
#   validation / auth / not-found rejections; a repeat of the identical
#   request cannot change the answer. This matches what `_is_retryable`
#   already does (`status == 429 or status >= 500`); the class is named here
#   so the behaviour is asserted by a test instead of being an emergent
#   property of an inequality.
_FATAL_STATUS_CODES = frozenset({400, 401, 403, 404})
_CAPACITY_STATUS_CODES = frozenset({500, 502, 503, 504, 529})

BACKOFF_FATAL = "fatal"
BACKOFF_RETRY_AFTER = "retry_after"
BACKOFF_JITTER = "jitter"


def classify_backoff(exc: Exception) -> tuple[str, float | None]:
    """Which backoff class this error falls in, and the hint if it stated one.

    Returns ``(BACKOFF_FATAL, None)`` for a request the provider will reject
    identically forever, ``(BACKOFF_RETRY_AFTER, seconds)`` when the server
    told us when to come back, and ``(BACKOFF_JITTER, None)`` otherwise.

    Deliberately consults `_is_retryable` for the final word on retryability
    so the two can never disagree — a second, independent status test is how
    the 2026-08-31 outage's budget mismatch happened.
    """
    if not _is_retryable(exc):
        return BACKOFF_FATAL, None
    status = getattr(exc, "status_code", None)
    if isinstance(status, int) and not isinstance(status, bool):
        if status in _FATAL_STATUS_CODES:
            return BACKOFF_FATAL, None
    hint = _retry_after_hint_seconds(exc)
    if hint is not None:
        return BACKOFF_RETRY_AFTER, min(max(0.0, hint), _RETRY_AFTER_CAP_S)
    return BACKOFF_JITTER, None


def is_capacity_refusal(exc: Exception) -> bool:
    """True when the provider refused because IT was busy, not because the
    request was bad: an explicit 429 or one of `_CAPACITY_STATUS_CODES`.

    This is the narrow class in which a refused attempt is provably unbilled
    — the provider produced no tokens — and in which the provider's own
    statement ("spikes in demand are usually temporary") says waiting is the
    remedy. A transport blip, a stream cut or a degenerate 200 is NOT in this
    class: the request may well have been charged, and nothing in it says
    waiting helps.
    """
    status = getattr(exc, "status_code", None)
    if not isinstance(status, int) or isinstance(status, bool):
        return False
    return status == 429 or status in _CAPACITY_STATUS_CODES


def error_aware_backoff_seconds(attempt: int, exc: Exception) -> float | None:
    """Seconds to sleep before the next attempt, or None to STOP retrying.

    A stated Retry-After is honoured as stated (capped), NOT max()'d against
    the exponential term. The previous loop took ``max(backoff, hint)``,
    which means a server saying "come back in 1s" was still made to wait the
    full exponential — the desk ignoring the one authoritative number in the
    exchange. The cap still protects against a hostile hint.
    """
    kind, hint = classify_backoff(exc)
    if kind == BACKOFF_FATAL:
        return None
    if kind == BACKOFF_RETRY_AFTER and hint is not None:
        return hint
    wait = _retry_backoff_seconds(attempt)
    # No stated hint. If this is a rate-limit / capacity refusal, apply the
    # provider-documented 1s floor (see _MIN_CAPACITY_BACKOFF_S). Everything
    # else — a transport blip, a degenerate 200, a stream cut — gets pure
    # Full Jitter, because no provider publishes a minimum for those and
    # inventing one would be exactly the arbitrary number this desk forbids.
    if is_capacity_refusal(exc):
        return max(_MIN_CAPACITY_BACKOFF_S, wait)
    return wait


# Exception class names that are always transient regardless of any status
# code (connection resets, DNS blackouts, read timeouts, provider 5xx /
# rate-limit). Matched by name so we don't have to import both SDKs.
_RETRYABLE_EXC_NAMES = frozenset(
    {
        "APIConnectionError",
        "APITimeoutError",
        "APIConnectionTimeoutError",
        "InternalServerError",
        "RateLimitError",
        "APIError",
        "Timeout",
        "ConnectionError",
        "ConnectTimeout",
        "ReadTimeout",
        # Our own degenerate-response classes (see definitions above): explicit
        # here so they stay retryable even if the unknown-exception fallback in
        # _is_retryable is ever tightened.
        "LLMEmptyResponseError",
        "LLMStreamInterruptedError",
        # LLMStreamErrorChunk is DELIBERATELY absent. This set is checked BEFORE
        # status_code below, so listing it would make a mid-stream 400/401 retry
        # for the full backoff budget. It carries the provider's own status, so
        # the status_code branch classifies it correctly: 429 and 5xx retry, other
        # 4xx fast-fail to the failover — which is the whole point of surfacing
        # that code instead of discarding it.
    }
)


# --- 402 "fewer max_tokens" shrink-retry -------------------------------------
#
# MEASURED 2026-09-30, production log: OpenRouter refuses a call on a
# near-empty balance with HTTP 402 and a message that NAMES the output
# allowance it would still serve, e.g.
#   "This request requires more credits, or fewer max_tokens. You requested
#    up to 16000 tokens, but can only afford 775."
# Observed affordable figures on the same day: 10125, 5062, 4655, 1622,
# 1551, 811, 775 — every one of them against the same 16000 ask. The desk
# asked for 16000 every time and never re-asked, so a balance that could
# have answered refused outright.
#
# We retry ONCE with the provider's OWN stated figure. We never invent a
# fallback size: no figure in the message means no shrink-retry, and the
# call fails exactly as it does today (the caller logs the refusal).
#
# This does NOT weaken the "refuse to decide on unusable evidence"
# doctrine. A smaller allowance can cut the answer off; the existing
# truncation detection (`_TRUNCATION_FINISH_REASONS`) still sees the
# max_tokens/length finish reason and the answer is still discarded
# unused. A seat whose prompt cannot fit the affordable allowance — the
# tech_analyst 25-symbol batch is the known case — therefore fails
# HONESTLY on truncation rather than being salvaged, and we deliberately
# do NOT shrink its batch to fit: re-cutting the batch to whatever a
# balance can afford would make the work depend on the wallet, and the
# batch size is the caller's decision, not this retry loop's.
_INSUFFICIENT_CREDIT_STATUS = 402

_AFFORDABLE_MAX_TOKENS_RE = re.compile(
    r"can only afford\s+(\d+)",
    re.IGNORECASE,
)


def _affordable_max_tokens(exc: Exception) -> int | None:
    """The output allowance a credit-refusal says it WOULD have served.

    Returns the provider's own stated figure, or None when the refusal is
    not a credit refusal or names no figure. Never guesses.
    """
    if getattr(exc, "status_code", None) != _INSUFFICIENT_CREDIT_STATUS:
        return None
    m = _AFFORDABLE_MAX_TOKENS_RE.search(str(exc))
    if not m:
        return None
    try:
        value = int(m.group(1))
    except ValueError:
        return None
    return value if value > 0 else None


def _is_retryable(exc: Exception) -> bool:
    """Decide whether an LLM-call exception is worth retrying.

    The old loop retried EVERY exception identically, so a non-transient
    failure — a 401 (dead key), a 400 (bad request), a 429-vs-quota-
    exhausted, a context-length-exceeded — burned the full ~140s backoff
    budget per agent for something that can never succeed, and with 4-5
    agents/session could push the run toward the 1200s outer kill. It also
    blurred the distinction the operator most needs: 'network blipped' vs
    'your key is dead' (exactly the 2026-05-11 quota-exhaustion case).

    Retry on: transient connection/timeout classes, HTTP 429, and 5xx.
    Fast-fail on: any other 4xx (auth / bad-request / not-found / context
    length). Unknown exceptions with no status code retry conservatively
    (preserves the prior catch-all behavior for genuinely unexpected
    local/network errors).
    """
    if type(exc).__name__ in _RETRYABLE_EXC_NAMES:
        return True
    status = getattr(exc, "status_code", None)
    if isinstance(status, int):
        # DeepSeek 402 "Insufficient Balance" — a dead-money error like a dead
        # key; retrying only burns the backoff budget. Fast-fail so the
        # cross-provider failover takes over (mirrors the 4xx fast-fail
        # philosophy and the OpenAI-quota case the failover was built for).
        if status == 402:
            return False
        return status == 429 or status >= 500
    # No status code and not a recognized transient class. Could be a local
    # network hiccup — retry rather than fail a session on something we
    # haven't classified.
    return True
