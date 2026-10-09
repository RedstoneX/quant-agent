"""LLM error classes and attempt budgets.

Moved VERBATIM out of src/agents/base.py; base.py re-exports every name.
"""

import os
from src.agents.llm_retry import _BACKOFF_CAP_S, _DEFAULT_MAX_RETRIES, _retry_deadline_s


_TRUNCATION_FINISH_REASONS = ("max_tokens", "length", "insufficient_system_resource")


class LLMEmptyResponseError(RuntimeError):
    """HTTP 200 whose body carries no usable content (choices empty /
    content None or ""). Previously returned as a *successful* '' — which
    parses to None downstream and masquerades as a deliberate no-signal,
    consuming the agent's one shot for the session while bypassing both the
    retry budget and the Anthropic failover. Raised instead, and classified
    retryable (a degenerate 200 from a relay is transient territory)."""


class LLMStreamInterruptedError(RuntimeError):
    """A streamed response ended without a finish_reason — the connection
    was cut mid-generation (relay/proxy drop, no error frame). Partial text
    is NOT a success: a half-emitted PM decision parses like 'no trades'.
    Retryable."""


class LLMStreamErrorChunk(RuntimeError):
    """OpenRouter reported a provider error INSIDE an already-started stream.

    Once the first byte is written the HTTP status is committed as 200 and
    cannot be changed, so OpenRouter documents a mid-stream error as an SSE
    chunk carrying a top-level `error` object plus
    `choices[0].finish_reason == "error"`:

        error: {code: 429, message: ..., metadata: {error_type: "rate_limit_exceeded"}}

    WHY THIS CLASS EXISTS. Before it, such a chunk produced an empty body with
    a non-truncation finish_reason, so `_call_openai` raised a generic
    `LLMEmptyResponseError` — which carries NO `status_code`. The cost
    circuit's `_is_known_zero_cost_failure` keys on `status_code`, finds none,
    fails closed, and charges the call its FULL pre-call reservation. A
    provider refusal that billed nothing was therefore booked as real spend:
    on 2026-08-31 that consumed $1.92 of a $2.75 daily cap and shut the desk
    down. Adding 429 to the zero-cost allow-list did not help, because the
    status never reached the classifier in the first place.

    This preserves the fail-closed rule exactly — it does not widen what
    counts as free. It stops DISCARDING the status code OpenRouter already
    sends, so an error the provider explicitly labels 429 is judged as the 429
    it is.
    """

    def __init__(self, message: str, status_code: int | None = None, error_type: str | None = None):
        super().__init__(message)
        #: Read by `_is_retryable` and by the cost circuit's zero-cost
        #: classifier, exactly as a provider SDK exception's own status is.
        self.status_code = status_code
        #: OpenRouter's stable typed code (e.g. "rate_limit_exceeded").
        self.error_type = error_type


def _max_retries() -> int:
    """Read at call time so tests can monkeypatch the env var per case
    without reloading the module."""
    raw = os.environ.get("QUANT_AGENT_MAX_RETRIES")
    if raw is None:
        return _DEFAULT_MAX_RETRIES
    try:
        n = int(raw)
    except ValueError:
        return _DEFAULT_MAX_RETRIES
    return max(1, n)


def capacity_max_attempts() -> int:
    """Primary attempts a CAPACITY refusal (429/5xx) may spend.

    DERIVED, not picked. The retry loop already owns two published numbers:
    the full-jitter schedule in `_retry_backoff_seconds` (upper bounds
    1, 2, 4, 8, ... capped at `_BACKOFF_CAP_S`) and the wall-clock
    `_retry_deadline_s()`. This returns the number of attempts whose
    worst-case cumulative sleep still fits inside that deadline. Nothing new
    is chosen here; change either published number and this moves with it.

    WHY IT IS NOT `_max_retries()`. Measured from this desk's own production
    log, 2026-09-29 and 2026-09-30: the tech seat's Google 503 "this model is
    currently experiencing high demand ... spikes in demand are usually
    temporary" spent BOTH permitted attempts two seconds apart, inside a 480s
    deadline that was never approached; the paid failover and tertiary routes
    were out of credit (402), the blocking seat produced nothing, and the
    morning session reported FAILED. The same model answered normally later in
    the same session, so the spike was transient and the desk simply did not
    wait for it. Two attempts two seconds apart does not measure whether a
    capacity spike has passed.

    Non-capacity failures keep `_max_retries()`: a degenerate 200 or a
    transport blip carries no provider statement that waiting helps.
    """
    deadline = _retry_deadline_s()
    total = 0.0
    attempts = 1
    while True:
        wait = min(_BACKOFF_CAP_S, float(2 ** (attempts - 1)))
        if total + wait > deadline:
            break
        total += wait
        attempts += 1
    return max(_max_retries(), attempts)


def provider_attempt_budget(*, failover_available: bool, tertiary_available: bool = False) -> int:
    """Worst-case provider attempts ONE logical agent call can make.

    This is the single source of truth for that number, and the reason it
    lives here rather than in configuration: the retry loop in ``run()`` is
    what actually spends the attempts, and it takes its budget from
    ``_max_retries()`` (env-overridable), not from ``settings.yaml``. Anything
    downstream that needs to bound the same call — notably the cost circuit's
    ``max_provider_attempts_per_call`` — must derive its ceiling from here
    instead of pinning an independent number.

    WHY THIS FUNCTION EXISTS (2026-08-31). The circuit's ceiling was pinned at
    2 by hand while this loop's worst case was 3: ``_max_retries()`` primary
    attempts, then one cross-provider failover. Any retryable primary failure
    — a 429, a 5xx, a timeout, i.e. precisely the outages failover exists for
    — therefore burned both permitted attempts on the primary and made the
    failover attempt number 3, which tripped the circuit instead of rescuing
    the session. On Monday 2026-08-31 an upstream rate-limit on the cheap
    primary did exactly that at 09:32 ET, two minutes after the open, and
    latched paid analysis off for the rest of the day over $0.05 of spend.
    Cross-provider failover had never once been able to succeed.

    The ``+ 1`` is the single-shot failover in ``run()`` (see ``_try_failover``:
    it deliberately gets no retry budget of its own). ``failover_available``
    mirrors that call site's own gate — a fallback key is configured AND the
    fallback (provider, model) pair differs from the primary's; failing over
    onto the exact (provider, model) pair that just failed is pointless (not
    just a Claude-to-Claude special case any more — see
    ``BaseAgent._failover_reachable``), so those agents never spend the extra
    attempt.

    ``tertiary_available`` adds the SECOND ``+ 1`` for route 3 (a different
    MODEL — see ``_DEFAULT_TERTIARY_PROVIDER``), on the same single-shot
    terms. This addend is the whole reason route 3 could not simply be bolted
    on: a third route that the circuit's ``max_provider_attempts_per_call``
    does not know about reproduces the 2026-08-31 incident exactly — the new
    rescue attempt becomes the attempt that trips the circuit, so the desk is
    latched by the very mechanism added to keep it running. The ceiling in
    ``config/settings.yaml`` moves with this function or the load-time check
    in ``AppConfig._check_provider_attempt_budget`` refuses to boot.

    NOTE the worst case is NOT made worse by the half-open breaker: while the
    primary is demoted the loop SKIPS its ``_max_retries()`` attempts
    entirely, so a demoted call spends at most 2 (secondary + tertiary),
    below this ceiling. The ceiling describes the undemoted worst case.
    """
    return capacity_max_attempts() + (1 if failover_available else 0) + (1 if tertiary_available else 0)
