import hashlib
import json
import logging
import math
import os
import random
import re
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass

from src.cost_table import estimate_cost, fmt_cost
from src.token_rate import TokenRateGovernor
from src.cost_circuit import (
    OptionalPaidAnalysisRetrySkipped,
    PaidAnalysisSuspended,
    UnavailableLLMCostCircuit,
)

logger = logging.getLogger(__name__)

# Model prefixes that route to OpenAI
_OPENAI_PREFIXES = ("gpt-", "o1-", "o3-", "o4-")

# DeepSeek is OpenAI-API-compatible: identical chat.completions wire format,
# reached through the openai SDK with a custom base_url + the DeepSeek key.
# Routed as a DISTINCT provider (not folded into _OPENAI_PREFIXES) because it
# needs (a) that base_url, (b) its own key, (c) the legacy `max_tokens` field —
# DeepSeek does NOT honor OpenAI's newer `max_completion_tokens`, so sending the
# latter is silently dropped and output falls back to a ~4096 default and
# truncates — and (d) a per-model output ceiling it REJECTS (does not clamp)
# values above. Verified against api-docs.deepseek.com 2026-06-05.
_DEEPSEEK_PREFIXES = ("deepseek-",)
_DEEPSEEK_BASE_URL = "https://api.deepseek.com"  # no /v1, no trailing slash

# OpenRouter: also OpenAI-API-compatible (same chat.completions wire format),
# reached through the openai SDK with a custom base_url + the OpenRouter key —
# same shape as the DeepSeek branch above. Unlike OpenAI/DeepSeek/Anthropic,
# OpenRouter model ids are themselves "vendor/model" strings (e.g.
# "anthropic/claude-3.5-sonnet", "google/gemini-2.5-pro") that collide with
# native prefixes and can't be disambiguated by string inspection alone —
# routing to OpenRouter is therefore EXPLICIT-ONLY (see resolve_provider()
# below), never inferred from a prefix. Stage 1 (QAMC provider/model plumbing).
_OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"

# Google AI Studio's OpenAI-compatible endpoint (2026-08-31 owner decision:
# gemini-3.5-flash-lite direct becomes the PRIMARY route for the eight
# specialist/review seats, free tier). Verified end-to-end from the box with
# streaming + usage — this is why `_call_openai`'s streamed OpenAI-wire path
# is reused (see _openai_wire_call) rather than a native Gemini client being
# written. The credential is injected as `Authorization: Bearer {value}`,
# which the OpenAI SDK sends natively via `api_key=` — no custom header work
# needed. NOTE: this is the COMPAT endpoint; the native Gemini REST endpoint
# uses `x-goog-api-key` instead and will NOT accept this same credential.
_GOOGLE_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai/"

# Bare Google-direct model ids (e.g. "gemini-3.5-flash-lite") are inferable
# from a prefix, unlike OpenRouter's "vendor/model" ids above — a config that
# names the model with no explicit `provider` must still route to Google
# rather than silently falling through to the Anthropic default.
_GOOGLE_PREFIXES = ("gemini-",)

# Per-model max-OUTPUT-token ceilings. We clamp client-side because DeepSeek
# rejects an over-ceiling max_tokens (HTTP 400/422 "Invalid max_tokens value")
# rather than silently clamping. The legacy deepseek-chat / deepseek-reasoner
# names now alias deepseek-v4-flash (1M ctx / 384K out) and are DEPRECATED
# 2026-07-24 — prefer configuring deepseek-v4-flash directly. An unknown
# deepseek-* id gets a conservative cap so a typo can't blow the ceiling.
# Source: official /pricing (v4 = 384K out) + create-chat-completion reference.
_DEEPSEEK_MAX_OUTPUT = {
    "deepseek-v4-flash": 384000,
    "deepseek-v4-pro":   384000,
    "deepseek-chat":     384000,  # legacy alias -> v4-flash (current routing)
    "deepseek-reasoner": 384000,  # legacy alias -> v4-flash (current routing)
}
_DEEPSEEK_DEFAULT_CEILING = 8192  # unknown deepseek-* id -> conservative cap

# One initial request plus one transient retry. The former seven-attempt loop
# amplified provider and validation failures into multi-dollar sessions. The
# persistent circuit below this layer also enforces a session-wide retry cap.
_DEFAULT_MAX_RETRIES = 2


def _retry_backoff_seconds(attempt: int) -> float:
    """Exponential base + full positive jitter on top.

    Returns a sleep duration in [2**attempt, 2 * 2**attempt). The
    deterministic floor preserves exponential spacing (so retries
    don't bunch right at the start), while the random ceiling
    decorrelates retries within a single sequence and across
    concurrent callers.

    Sequence for attempt 0..5 (the 6 between-attempt sleeps with N=7):
      [1, 2), [2, 4), [4, 8), [8, 16), [16, 32), [32, 64)
    """
    base = 2 ** attempt
    return base + random.uniform(0, base)

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
# for the rest of the session. And 480s still allows 2-4 real primary attempts
# even in the slow-failure mode (~120-380s each), so a transient blip is
# ridden out before the failover engages.
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


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return max(1, int(raw))
    except ValueError:
        return default


# Per-provider in-flight caps around the LLM HTTP call itself (NOT around
# run() — building the user message / parsing must never hold a slot).
#
# Why: the relay enforces a per-user concurrency cap, and morning fans out
# macro + news + tech (multi-chunk) + earnings through a
# ThreadPoolExecutor(max_workers=4) on one relay account — the fan-out
# self-inflicted "Concurrency limit exceeded" 429 storms (175 occurrences in
# the 06-16..06-29 logs), and each kill-looped morning re-spawned the full
# team into the already-limited relay. A module-level semaphore serializes
# the excess instead of bouncing it off the server. Anthropic is direct
# (no relay) so it gets a looser, independent cap — failover calls must not
# queue behind a wedged relay slot.
_OPENAI_MAX_CONCURRENT = _int_env("QUANT_AGENT_MAX_CONCURRENT_LLM", 3)
_OPENAI_LLM_SEMAPHORE = threading.Semaphore(_OPENAI_MAX_CONCURRENT)
_ANTHROPIC_MAX_CONCURRENT = 4
_ANTHROPIC_LLM_SEMAPHORE = threading.Semaphore(_ANTHROPIC_MAX_CONCURRENT)
# OpenRouter is a distinct account/rate-limit domain from the OpenAI relay —
# it must not share (and be starved by, or starve) the relay's cap.
_OPENROUTER_MAX_CONCURRENT = _int_env("QUANT_AGENT_MAX_CONCURRENT_OPENROUTER", 3)
_OPENROUTER_LLM_SEMAPHORE = threading.Semaphore(_OPENROUTER_MAX_CONCURRENT)
# Google AI Studio direct is a third distinct account/rate-limit domain (its
# own free-tier RPM/TPM/RPD ceiling — see _GOOGLE_TOKENS_PER_MIN below) and
# must not share the OpenAI relay's or OpenRouter's cap. Default of 3 mirrors
# OpenRouter's; the free tier's 15 RPM ceiling leaves ample headroom for 3
# concurrent in-flight requests at the call latencies this desk sees.
_GOOGLE_MAX_CONCURRENT = _int_env("QUANT_AGENT_MAX_CONCURRENT_GOOGLE", 3)
_GOOGLE_LLM_SEMAPHORE = threading.Semaphore(_GOOGLE_MAX_CONCURRENT)

# --- tokens per minute, the limit the semaphores above never bounded -------
#
# A concurrency cap counts REQUESTS. A provider rate limit counts TOKENS per
# minute. Three concurrent 80,000-token requests satisfy a cap of three and
# are exactly what gets declined, which is how the Technical Analyst could
# send ~314,000 tokens in 80 seconds (~252k/min) while every cap in the
# system read as green. It was the only agent ever rate-limited: eleven times
# in three weeks, with no other agent declined once.
#
# This is a BACKSTOP, not the mechanism. The actual fix is that requests are
# now built to a size budget rather than cut to a fixed item count (see
# src/token_budget.py): the largest request a morning pass can produce fell
# from ~136,000 tokens to ~44,700, so even all three concurrent slots full
# is ~134k/min — under this ceiling by construction, not by luck.
#
# The ceiling exists for what the budget cannot see: a new agent, a prompt
# that grows, a retry storm. 150k/min sits below the ~252k/min burst that
# was actually being refused and above anything the packer can now emit, so
# in normal operation it must never fire. If it does, that is a bug report
# about something having grown — and it says exactly that, at CRITICAL.
#
# Deliberately NOT the primary control: pacing against a guessed ceiling
# means discovering the real limit by being refused, and every refusal is
# paid for. Sizing the request before it leaves costs nothing.
_OPENROUTER_TOKENS_PER_MIN = _int_env("QUANT_AGENT_OPENROUTER_TPM", 150_000)
_OPENAI_TOKENS_PER_MIN = _int_env("QUANT_AGENT_OPENAI_TPM", 150_000)
_ANTHROPIC_TOKENS_PER_MIN = _int_env("QUANT_AGENT_ANTHROPIC_TPM", 150_000)

# Google AI Studio direct (project qamc-gemini) free tier, READ OFF THE
# OWNER'S OWN AI STUDIO DASHBOARD 2026-08-31: 15 RPM / 250,000 TPM / 500 RPD.
# Published blog figures (1,000-1,500 RPD) are WRONG for this project; the
# dashboard is authoritative. TPM is the BINDING constraint, not RPD: a
# morning pass is ~215,000 tokens — 86% of the per-minute ceiling in a single
# burst — while RPD at ~76 requests/day against 500 is a non-issue.
#
# 200,000 is 80% of that measured 250,000 TPM ceiling, not a guessed number —
# the owner's explicit objection to an earlier ceiling was that it was chosen
# by taste rather than derived from a published/measured limit. A 6-request
# test at ~253k tokens BREACHED the real ceiling (recorded on the dashboard,
# though the calls happened to still succeed) — "no error" is not "no
# limit", so this stays a genuine safety margin, not a number tuned to the
# last incident.
_GOOGLE_TOKENS_PER_MIN = _int_env("QUANT_AGENT_GOOGLE_TPM", 200_000)
_GOVERNOR_MAX_WAIT_S = float(_int_env("QUANT_AGENT_TPM_MAX_WAIT_S", 120))

# Pre-request size estimate. The dense numeric payload that actually trips
# rate limits tokenizes at roughly ONE token per character (measured: 314,366
# tokens for ~355,000 characters of OHLCV rows), nothing like the ~4 that
# prose gives. Estimating at 1.5 stays conservative for that case rather than
# under-charging the very requests the governor exists to bound; prose is
# over-charged, which only costs a little headroom on agents that send a
# twentieth as much. Every charge is reconciled to the provider's real usage
# the moment the response lands, so this constant only ever affects one
# request's wait decision, never the window's accuracy.
_GOVERNOR_CHARS_PER_TOKEN = 1.5

_TOKEN_GOVERNORS = {
    "openrouter": TokenRateGovernor(
        "OpenRouter", _OPENROUTER_TOKENS_PER_MIN, max_wait_s=_GOVERNOR_MAX_WAIT_S,
    ),
    "openai": TokenRateGovernor(
        "OpenAI", _OPENAI_TOKENS_PER_MIN, max_wait_s=_GOVERNOR_MAX_WAIT_S,
    ),
    "anthropic": TokenRateGovernor(
        "Anthropic", _ANTHROPIC_TOKENS_PER_MIN, max_wait_s=_GOVERNOR_MAX_WAIT_S,
    ),
    "deepseek": TokenRateGovernor(
        "DeepSeek", _int_env("QUANT_AGENT_DEEPSEEK_TPM", 150_000),
        max_wait_s=_GOVERNOR_MAX_WAIT_S,
    ),
    "google": TokenRateGovernor(
        "Google", _GOOGLE_TOKENS_PER_MIN, max_wait_s=_GOVERNOR_MAX_WAIT_S,
    ),
}


def token_governor_snapshots() -> list[dict]:
    """Every governor's current state, for status reporting."""
    return [g.snapshot() for g in _TOKEN_GOVERNORS.values()]


# finish/stop reasons that mean "output hit a ceiling mid-generation".
# Shared by the truncation flag in _execute() and the empty-content guards:
# an empty body WITH one of these reasons is a legitimate truncation (e.g. a
# reasoner burning the whole budget on CoT) that must surface as
# truncated=True, NOT trigger retry/failover (truncation never fails over —
# see CLAUDE.md). insufficient_system_resource is DeepSeek-specific: the
# inference system ran out of resources and returned a cut-off body on a 200.
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

    def __init__(self, message: str, status_code: int | None = None,
                 error_type: str | None = None):
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


def provider_attempt_budget(*, failover_available: bool) -> int:
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
    """
    return _max_retries() + (1 if failover_available else 0)


def _is_openai_model(model: str) -> bool:
    return any(model.startswith(p) for p in _OPENAI_PREFIXES)


def _governor_domain_for(model: str, agent, *, is_failover: bool = False) -> str:
    """Which provider's token budget this request will actually consume.

    Keyed on which PATH this attempt is taking, not on sniffing the model
    string: a cross-provider failover spends the FALLBACK provider's rate
    limit, not the primary's, and charging it to the wrong governor would let
    a failover storm slip past the ceiling it is supposed to be bounded by.

    ``is_failover`` is threaded explicitly from the call site (the dedicated
    failover authorize-closure in ``_execute()``) rather than inferred from
    the model text. Model-text sniffing was only ever safe because the old
    fallback model (``claude-opus-4-7``) had a distinctive "claude" prefix no
    primary model would ever share; now that the fallback (provider, model)
    pair is configurable, nothing guarantees the fallback model's spelling is
    distinctive — a Google-direct primary (bare id "gemini-3.5-flash-lite")
    and an OpenRouter-vendor-prefixed fallback of the "same" model
    ("google/gemini-3.5-flash-lite") happen to differ as strings, but that is
    incidental and nothing should depend on it.
    """
    if is_failover:
        fallback_provider = getattr(agent, "_fallback_provider", None)
        return fallback_provider if fallback_provider in _TOKEN_GOVERNORS else "openrouter"
    provider = getattr(agent, "_provider", None)
    return provider if provider in _TOKEN_GOVERNORS else "openai"


def _is_deepseek_model(model: str) -> bool:
    return any(model.startswith(p) for p in _DEEPSEEK_PREFIXES)


def _is_google_model(model: str) -> bool:
    return any(model.startswith(p) for p in _GOOGLE_PREFIXES)


# Providers explicit config / _check_llm_provider_keys / pipeline.py are
# allowed to name. Anything else is treated as "no explicit override" so a
# typo can't silently misroute a live agent.
VALID_PROVIDERS = frozenset({"anthropic", "openai", "deepseek", "openrouter", "google"})


def _provider_for(model: str) -> str:
    """Provider implied by a model-id PREFIX alone (no explicit override).
    This is the pre-Stage-1 inference chain, extended (not otherwise changed)
    for Google-direct bare ids ("gemini-*") so every existing config keeps
    routing exactly as it did before Stage 1 / before Google was added."""
    if _is_openai_model(model):
        return "openai"
    if _is_deepseek_model(model):
        return "deepseek"
    if _is_google_model(model):
        return "google"
    return "anthropic"


def resolve_provider(model: str, explicit_provider: str | None = None) -> str:
    """Single source of truth for provider selection.

    An explicit provider always wins over prefix inference — required for
    OpenRouter, whose "vendor/model" ids (e.g. "anthropic/claude-3.5-sonnet")
    collide with native prefixes and cannot be told apart from the model
    string alone. When no (valid) explicit provider is given, falls back to
    the existing prefix chain unchanged, so every pre-Stage-1 config (no
    `provider` field set) routes identically to before.

    Reused by BaseAgent.__init__ (client construction), AppConfig's
    per-provider API-key validation, and pipeline.py's agent-key lookup, so
    those three call sites can never disagree about which provider a given
    (model, explicit_provider) pair means — a prior triplication risk this
    helper closes.
    """
    if explicit_provider:
        p = explicit_provider.strip().lower()
        if p in VALID_PROVIDERS:
            return p
    return _provider_for(model)


# Cross-provider failover target — CONFIGURABLE (2026-08-31 owner decision),
# not hardcoded. When a primary call ultimately fails — quota exhausted (the
# 2026-05-11 incident), DeepSeek 402 insufficient balance, a rate limit (the
# 2026-08-31 incident), dead key, sustained outage — the agent retries ONCE
# on the configured fallback (provider, model) so the trading session
# survives instead of dying (see BaseAgent._try_failover).
#
# These are process-wide DEFAULTS, threaded through pipeline.py from
# `config.llm.fallback_provider` / `fallback_model` (settings.yaml) into
# every BaseAgent.__init__ — not per-agent, so the fallback target can't
# silently drift seat-by-seat. The default pairs OpenRouter (paid, backup)
# with the SAME model Google AI Studio direct serves as the primary
# (gemini-3.5-flash-lite) for the eight specialist/review seats: a failover
# changes the ROAD, not the REASONING, which was the owner's explicit
# objection to the inherited claude-opus-4-7 Anthropic fallback (an
# upstream `yebof` remnant, commit d237f9b 2026-06-04, that nobody at QAMC
# chose) — a different model answering under stress is itself a source of
# surprise, on top of whatever the primary's outage already was.
_DEFAULT_FALLBACK_PROVIDER = "openrouter"
_DEFAULT_FALLBACK_MODEL = "google/gemini-3.5-flash-lite"


# Exception class names that are always transient regardless of any status
# code (connection resets, DNS blackouts, read timeouts, provider 5xx /
# rate-limit). Matched by name so we don't have to import both SDKs.
_RETRYABLE_EXC_NAMES = frozenset({
    "APIConnectionError", "APITimeoutError", "APIConnectionTimeoutError",
    "InternalServerError", "RateLimitError", "APIError",
    "Timeout", "ConnectionError", "ConnectTimeout", "ReadTimeout",
    # Our own degenerate-response classes (see definitions above): explicit
    # here so they stay retryable even if the unknown-exception fallback in
    # _is_retryable is ever tightened.
    "LLMEmptyResponseError", "LLMStreamInterruptedError",
    # LLMStreamErrorChunk is DELIBERATELY absent. This set is checked BEFORE
    # status_code below, so listing it would make a mid-stream 400/401 retry
    # for the full backoff budget. It carries the provider's own status, so
    # the status_code branch classifies it correctly: 429 and 5xx retry, other
    # 4xx fast-fail to the failover — which is the whole point of surfacing
    # that code instead of discarding it.
})


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


@dataclass
class AgentResult:
    raw_text: str
    tokens_used: int
    model: str
    user_message: str = ""
    # Per-call cost tracking — populated by `run()` when the model's
    # pricing is known in `src/cost_table.py`. None when the model name
    # isn't in the pricing table; callers must NOT default to 0 in that
    # case (would silently understate aggregate cost). Split input/output
    # token counts retained so cost can be recomputed if pricing changes
    # post-hoc.
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float | None = None
    # Provider stop/finish reason + a derived flag. `truncated` is True when
    # the model hit the token ceiling mid-output (Anthropic stop_reason
    # 'max_tokens' / OpenAI finish_reason 'length'). This is distinct from a
    # clean "no signal" answer: a PM decision cut off at max_tokens parses to
    # None and looks identical to "chose not to trade" — callers/notifier can
    # now tell a swallowed truncation from a real silence.
    finish_reason: str | None = None
    truncated: bool = False
    # Stage 1 (QAMC provider/model/correlation plumbing) attribution fields.
    # requested_* is what THIS call was configured to use; model/actual_provider
    # is what actually answered (may differ on cross-provider failover — see
    # used_fallback). Never conflate the two: DECISION #12 forbids counting a
    # fallback as the requested provider/model.
    requested_model: str = ""
    requested_provider: str = ""
    actual_provider: str = ""
    used_fallback: bool = False
    # First 12 hex chars of sha256(system_prompt) — a cheap, stable "did the
    # prompt text change between two calls" signal, not a semantic version.
    prompt_version: str = ""
    latency_s: float = 0.0
    # Provider HTTP requests represented by this logical result. Normally one;
    # retry/failover and Tech chunk aggregation can be greater.
    provider_requests: int = 1
    # Transport-valid output may still fail a deterministic semantic contract.
    semantic_status: str | None = None
    semantic_error: str | None = None

    # Top-level keys we recognize as "this looks like a real agent output."
    # When the LLM prose includes an extra JSON fragment (self-correction,
    # partial thinking-out-loud, or a tool-like object), these anchors let us
    # pick the actual output instead of the largest stray fragment.
    _EXPECTED_AGENT_KEY_WEIGHTS = {
        "decisions": 50,           # PortfolioDecision (legacy pre-constructor key)
        # Phase-2 constructor refactor renamed PM's actionable output from
        # `decisions` to `targets` but this table was never updated, so a
        # full PortfolioDecision scored only 40 (portfolio_view +
        # reasoning_chain) while its own inner `targets` ARRAY scored
        # 5/symbol — any plan with ≥8 targets lost to a fragment of itself
        # and the whole morning collapsed to "no trades" (2026-08-17/20
        # production: 10 of 13 decision runs died here; reproduced from
        # recorded payloads in tests/fixtures/pm_response_*).
        "targets": 50,             # PortfolioDecision (current key)
        "approved": 50,            # RiskVerdict
        "actions": 50,             # MiddayReview
        "daily_summary": 40,       # EveningReport
        "tomorrow_outlook": 40,    # EveningReport alt anchor
        "regime": 40,              # MacroAnalysis
        "investment_implications": 40,  # EarningsAnalysis
        "macro_narrative": 40,     # NewsIntelligenceReport
        "analyses": 40,            # TechAnalyst batch wrapper
        "findings": 50,            # SmartMoney synthesis wrapper
        "portfolio_view": 20,      # PortfolioDecision summary
        "reasoning_chain": 20,     # nested rationale wrapper
        "symbol": 5,               # TechAnalysisResult single
        "rating": 5,               # TechAnalysisResult single
    }

    @staticmethod
    def _shape_score(parsed) -> int:
        """How 'agent-output shaped' a JSON candidate looks. Higher is better."""
        # A top-level LIST is a first-class agent shape: tech_analyst returns
        # an array of per-symbol analyses (tech_analyst.py: `items = parsed if
        # isinstance(parsed, list) else [parsed]`). Scoring it 0 meant that
        # whenever the model wrapped the array in ANY prose (so the clean
        # json.loads happy path missed), the candidate scan compared the array
        # (score 0) against each of its own elements (score > 0) and returned
        # the LAST ELEMENT — silently discarding every other symbol's analysis
        # in the chunk. Score the container by the SUM of its elements so it
        # strictly outranks any single element it contains (2026-07-16 audit;
        # reproduced: a 3-analysis array returned 1 dict).
        if isinstance(parsed, list):
            return sum(AgentResult._shape_score(item) for item in parsed)
        if not isinstance(parsed, dict):
            return 0
        keys = set(parsed.keys())
        return sum(
            weight
            for key, weight in AgentResult._EXPECTED_AGENT_KEY_WEIGHTS.items()
            if key in keys
        )

    def parse_json(self) -> dict | list | None:
        text = self.raw_text.strip()
        try:
            parsed = json.loads(text)
            # Full-text parse wins outright if it's a dict/list; no candidate
            # search needed. This is the happy path — LLM returned clean JSON.
            return parsed
        except json.JSONDecodeError:
            pass

        # Each candidate carries its source SPAN (start, end in raw_text) so
        # nested fragments can be recognized. (score, size, idx, span, parsed)
        candidates: list[tuple[int, int, int, tuple[int, int], dict | list]] = []
        # idx preserves source order so we can break ties predictably.
        idx = 0
        # Fenced ```json blocks — highest trust.
        for match in re.finditer(r"```(?:json)?\s*\n(.*?)\n```", self.raw_text, re.DOTALL):
            try:
                parsed = json.loads(match.group(1).strip())
            except json.JSONDecodeError:
                continue
            candidates.append((
                self._shape_score(parsed), len(json.dumps(parsed)), idx,
                match.span(1), parsed,
            ))
            idx += 1

        decoder = json.JSONDecoder()
        for i, ch in enumerate(self.raw_text):
            if ch not in "{[":
                continue
            try:
                parsed, end = decoder.raw_decode(self.raw_text[i:])
            except json.JSONDecodeError:
                continue
            candidates.append((
                self._shape_score(parsed), len(json.dumps(parsed)), idx,
                (i, i + end), parsed,
            ))
            idx += 1

        # Nested-fragment filter: a candidate STRICTLY contained inside a
        # larger candidate is part of that candidate's content, not a later
        # "correction" of it — the recency tie-break below was designed for
        # DISJOINT draft-then-fix fragments. Without this, the PM's inner
        # `targets` array (5 pts/symbol) outranked the very object that
        # contained it once the plan reached ≥8 names, and the entire
        # morning decision was destroyed by a fragment of itself
        # (2026-08-17/20 production incident; see _EXPECTED_AGENT_KEY_WEIGHTS
        # note). A container that itself looks like agent output (score > 0)
        # therefore always wins over its own fragments, REGARDLESS of the
        # fragments' scores. The only nested fragment worth keeping is one
        # inside a score-0 container — the prose-wrapper case
        # (e.g. {"thinking": ..., "answer": {...}}), where the wrapper has
        # no recognizable agent shape and the payload is the real output.
        def _strictly_inside(inner: tuple[int, int], outer: tuple[int, int]) -> bool:
            return (
                outer[0] <= inner[0] and inner[1] <= outer[1]
                and (outer[0] < inner[0] or inner[1] < outer[1])
            )

        filtered = [
            c for c in candidates
            if not any(
                other is not c
                and other[0] > 0
                and _strictly_inside(c[3], other[3])
                for other in candidates
            )
        ]
        candidates = filtered or candidates

        if candidates:
            max_shape = max(item[0] for item in candidates)
            if max_shape > 0:
                # Once something looks like a real agent output, prefer the
                # latest correction over an earlier larger draft.
                shaped = [item for item in candidates if item[0] == max_shape]
                return max(shaped, key=lambda item: (item[2], item[1]))[4]

            # If nothing has recognizable agent keys, fall back to the largest
            # valid JSON fragment and use recency only as a tiebreaker.
            return max(candidates, key=lambda item: (item[1], item[2]))[4]

        logger.warning("Failed to parse agent response as JSON: %s", self.raw_text[:200])
        return None


def agent_log_kwargs(result: AgentResult) -> dict:
    """Common Stage 1 telemetry kwargs for Database.insert_agent_log(),
    derived from an AgentResult. Callers add agent_name/run_id/decision_id
    and the summary/response fields on top. Centralized so all nine call
    sites stay consistent instead of re-deriving `status` etc. independently."""
    def text_or_none(value):
        return value if isinstance(value, str) and value else None

    provider_requests = getattr(result, "provider_requests", 1)
    if (not isinstance(provider_requests, int)
            or isinstance(provider_requests, bool)
            or provider_requests < 0):
        # Compatibility for old test/replay fixtures built as open-ended
        # MagicMocks and for any legacy caller that predates this field.
        provider_requests = 1
    semantic_status = text_or_none(getattr(result, "semantic_status", None))
    used_fallback = getattr(result, "used_fallback", False) is True
    latency = getattr(result, "latency_s", None)
    if not isinstance(latency, (int, float)) or isinstance(latency, bool):
        latency = None
    truncated = getattr(result, "truncated", None)
    if not isinstance(truncated, bool):
        truncated = None
    return dict(
        requested_provider=text_or_none(getattr(result, "requested_provider", None)),
        requested_model=text_or_none(getattr(result, "requested_model", None)),
        actual_provider=text_or_none(getattr(result, "actual_provider", None)),
        prompt_version=text_or_none(getattr(result, "prompt_version", None)),
        latency_s=latency,
        provider_requests=provider_requests,
        status=(semantic_status or ("fallback" if used_fallback else "success")),
        finish_reason=text_or_none(getattr(result, "finish_reason", None)),
        truncated=truncated,
    )


def _build_llm_client(provider: str, api_key: str):
    """Construct the SDK client for `provider`, given its API key.

    Shared by BaseAgent.__init__ (the primary client) and _try_failover (the
    fallback client) so the two can never diverge in how a provider's client
    is built — before this helper existed, __init__ had its own inline
    if/elif chain and a hardcoded Anthropic construction lived separately
    inside _try_failover, and the two had already started drifting (the
    failover path never got DeepSeek/OpenRouter/relay/CA-bundle handling).

    max_retries=0 on EVERY client built here: both SDKs default to 2 internal
    retries on 429/5xx (incl. the relay's CF 524) with their own backoff,
    silently turning each _execute() attempt into ~3 HTTP calls and
    invalidating the retry-budget math documented at _DEFAULT_MAX_RETRIES.
    The agent-level loop in _execute() is the SINGLE owner of retry policy.
    """
    if provider == "deepseek":
        # OpenAI-compatible endpoint at a custom base_url with the DeepSeek key.
        from openai import OpenAI
        return OpenAI(api_key=api_key, base_url=_DEEPSEEK_BASE_URL,
                      timeout=_LLM_HTTP_TIMEOUT, max_retries=0)
    if provider == "openrouter":
        # Also OpenAI-API-compatible — identical shape to the DeepSeek branch
        # above, just a different base_url + key. Reuses _openai_wire_call()
        # unmodified (see there): zero new call code.
        from openai import OpenAI
        return OpenAI(api_key=api_key, base_url=_OPENROUTER_BASE_URL,
                      timeout=_LLM_HTTP_TIMEOUT, max_retries=0)
    if provider == "google":
        # Google AI Studio's OpenAI-compatible endpoint — same shape again.
        # The credential is injected as `Authorization: Bearer {value}`,
        # which the OpenAI SDK's `api_key=` already sends natively.
        from openai import OpenAI
        return OpenAI(api_key=api_key, base_url=_GOOGLE_BASE_URL,
                      timeout=_LLM_HTTP_TIMEOUT, max_retries=0)
    if provider == "openai":
        from openai import OpenAI
        # OPENAI_BASE_URL lets OpenAI traffic go through an OpenAI-compatible
        # relay/proxy (a "中转站") instead of api.openai.com — same chat/
        # completions wire format, just a different host + key. Empty/unset
        # => the SDK's default (api.openai.com). Read explicitly (not via the
        # SDK's own env magic) so it's visible + testable. The base_url must
        # include the API path prefix the relay serves (e.g. .../v1).
        base_url = os.environ.get("OPENAI_BASE_URL", "").strip() or None
        # OPENAI_CA_BUNDLE: trust a private CA for the relay's HTTPS (e.g. a
        # relay behind Caddy's internal CA, whose self-signed chain the
        # public trust store rejects). Path to a PEM of trust anchors (the
        # relay's root CA). We still FULLY verify — cert chain + hostname/IP
        # (the relay's leaf carries the IP in its SAN) — just against this CA
        # instead of certifi. Scoped to the OpenAI client ONLY; every other
        # provider's client keeps the public trust store. Unset => default
        # public CAs.
        ca_bundle = os.environ.get("OPENAI_CA_BUNDLE", "").strip()
        if ca_bundle:
            import httpx
            return OpenAI(
                api_key=api_key, base_url=base_url,
                http_client=httpx.Client(verify=ca_bundle, timeout=_LLM_HTTP_TIMEOUT),
                max_retries=0,
            )
        return OpenAI(api_key=api_key, base_url=base_url,
                      timeout=_LLM_HTTP_TIMEOUT, max_retries=0)
    # anthropic (default)
    from anthropic import Anthropic
    return Anthropic(api_key=api_key, timeout=_LLM_HTTP_TIMEOUT, max_retries=0)


class BaseAgent(ABC):
    # Unit tests that exercise isolated provider parsing may opt out through
    # tests/conftest.py. Production code never changes this flag: a paid call
    # without the mandatory persistent breaker is a fail-closed error.
    _allow_unmetered_for_tests = False

    def __init__(self, api_key: str, model: str, max_tokens: int = 4096,
                 fallback_api_key: str = "", provider: str | None = None,
                 provider_order: list[str] | None = None,
                 fallback_provider: str = _DEFAULT_FALLBACK_PROVIDER,
                 fallback_model: str = _DEFAULT_FALLBACK_MODEL):
        self.model = model
        self.max_tokens = max_tokens
        # OpenRouter endpoint preference for this seat — see
        # LLMConfig.<agent>_provider_order. Ordered most-preferred first, with
        # fallbacks left ENABLED: every endpoint for a given model id serves
        # the same weights, so falling through to a pricier one costs money,
        # not correctness, whereas pinning `only` would fail the seat closed
        # over a price tier. Cost stays truthful because OpenRouter reports
        # the actual charge per call (see _openai_wire_call).
        self._provider_order = list(provider_order) if provider_order else None
        # `provider` is the Stage 1 explicit override (config.llm.<agent>_provider);
        # None (the default for every pre-Stage-1 config) falls through to the
        # unchanged prefix-inference chain — see resolve_provider().
        self._provider = resolve_provider(model, provider)
        self._use_deepseek = self._provider == "deepseek"
        self._use_openai = self._provider == "openai"
        self._use_openrouter = self._provider == "openrouter"
        self._use_google = self._provider == "google"
        # Cross-provider failover target — configurable (see
        # _DEFAULT_FALLBACK_PROVIDER/_DEFAULT_FALLBACK_MODEL above). Resolved
        # through the same resolve_provider() helper as the primary so a
        # typo'd `fallback_provider` can't silently misroute, exactly like
        # every other provider field in this codebase.
        self._fallback_provider = resolve_provider(fallback_model, fallback_provider)
        self._fallback_model = fallback_model
        # Failover credential. Empty => failover disabled outright regardless
        # of the pair check below. Passing it when the fallback pair happens
        # to equal the primary's is harmless, NOT an error — run() no-ops the
        # failover attempt in that case (see _failover_reachable).
        self._fallback_api_key = (fallback_api_key or "").strip()
        # The failover gate, computed once: a fallback key is configured AND
        # the fallback (provider, model) pair is not identical to the
        # primary's — never fail over onto the exact thing that just failed.
        # This generalizes the old "primary is not itself Anthropic" special
        # case (which only made sense when the ONLY fallback target was
        # Anthropic): a Claude primary can now legitimately fail over too, as
        # long as the configured fallback isn't Claude-to-Claude. Mirrored
        # exactly in AppConfig._check_provider_attempt_budget /
        # _check_llm_provider_keys (src/config.py) — those two independently
        # computing this and drifting apart is what caused the 2026-08-31
        # outage (see provider_attempt_budget's docstring).
        self._failover_reachable = bool(self._fallback_api_key) and (
            (self._fallback_provider, self._fallback_model)
            != (self._provider, self.model)
        )
        # Attached after TradingPipeline initializes the shared SQLite DB.
        self._cost_circuit = None

        self.client = _build_llm_client(self._provider, api_key)

    def set_cost_circuit(self, circuit) -> None:
        """Attach the mandatory process-shared paid-analysis gate."""

        self._cost_circuit = circuit

    @property
    @abstractmethod
    def name(self) -> str:
        ...

    @property
    @abstractmethod
    def system_prompt(self) -> str:
        ...

    @abstractmethod
    def build_user_message(self, **kwargs) -> str:
        ...

    def run(self, **kwargs) -> AgentResult:
        user_message = self.build_user_message(**kwargs)
        return self._execute(user_message)

    def repair_reprompt(self, failed: AgentResult, error, schema_name: str) -> AgentResult:
        """One bounded re-ask after a response parsed as JSON but failed
        schema validation (e.g. a mandatory reasoning_chain field omitted).

        Production incident 2026-08-18 15:03: the Risk Manager APPROVED the
        day's plan with three sensible halving modifications, but omitted
        `sizing_sanity` and `overall` from its reasoning_chain — validation
        failed, the verdict became None, and RiskStage recorded
        "REJECTED: parse error". A prose omission silently destroyed an
        approving verdict and ended the trading day. One corrective call
        (~$0.002) names the exact validation errors and asks the model to
        complete the object; if the second attempt also fails, callers
        keep their existing fail-closed path (None → reject/retry).

        This is SCHEMA COMPLETION, never re-decision. The coda explicitly
        scopes the model to filling missing/invalid narrative fields and
        forbids touching any decision-bearing value. That instruction is
        advisory, not enforcement — callers MUST additionally verify the
        repaired parse's decision-bearing fields are byte-identical to the
        pre-repair parse (see `RiskManagerAgent`/`PortfolioManagerAgent`)
        and fail closed if the model changed them or the original failure
        was itself rooted in a decision-bearing field (in which case
        callers should skip repair entirely — see
        `validation_error_touches`).

        Deliberately built on `_execute` (not `run`) so the original
        user message is replayed verbatim with a repair coda. The
        returned AgentResult's `user_message` includes the coda, so the
        agent_logs row is self-describing about being a repair call.
        """
        coda = (
            f"\n\n## SCHEMA REPAIR REQUIRED — NOT A RE-DECISION\n"
            f"Your previous response was parseable JSON but failed "
            f"{schema_name} schema validation.\n\n"
            f"Validation errors:\n{error}\n\n"
            f"Your previous response was:\n{failed.raw_text}\n\n"
            f"Re-emit the COMPLETE JSON object, filling in ONLY the "
            f"missing or invalid schema field(s) named in the validation "
            f"errors above — typically an empty or omitted narrative "
            f"field. Do NOT reconsider, add, remove, or change the value "
            f"of any decision-bearing field: every target/symbol/weight, "
            f"every approved/modifications/rejected_symbols/scale_all_buys/"
            f"reason_category value must be returned EXACTLY as in your "
            f"previous response. "
            f"This is schema completion only. Respond ONLY with the JSON "
            f"object."
        )
        logger.warning(
            "Agent %s: %s validation failed — attempting one repair reprompt",
            self.name, schema_name,
        )
        return self._execute(failed.user_message + coda, retry_kind="schema_repair")

    @staticmethod
    def validation_error_touches(error, field_names: tuple[str, ...]) -> bool:
        """True if a pydantic ValidationError is rooted at one of the
        named top-level fields.

        Used to skip `repair_reprompt` entirely when the validation
        failure concerns DECISION-bearing content (e.g. `approved` has
        the wrong type, `modifications[0].new_value` isn't numeric) rather
        than a narrative field — a repair call cannot fix that without
        re-deciding, so the caller should fail closed immediately instead
        of spending a call the fix-closed comparison would reject anyway.
        """
        try:
            errors = error.errors()
        except AttributeError:
            return False
        return any(
            err.get("loc", (None,))[:1] and err["loc"][0] in field_names
            for err in errors
        )

    def _execute(
        self,
        user_message: str,
        *,
        retry_kind: str | None = None,
        optional_retry: bool = False,
        single_provider_attempt: bool = False,
    ) -> AgentResult:
        """The retry / cross-provider-failover / cost / parse loop, decoupled
        from build_user_message so a stored historical `input_message` can be
        replayed through the CURRENT prompt + model without rebuilding context
        (see src/replay.py / scripts/replay_decision.py). `run()` = build +
        `_execute`; behavior is identical to the pre-extraction loop."""
        logger.info("Agent %s running with model %s", self.name, self.model)
        logger.info("Agent %s input:\n%s", self.name, user_message)

        max_retries = 1 if single_provider_attempt else _max_retries()
        deadline_s = _retry_deadline_s()
        loop_start = time.monotonic()
        finish_reason: str | None = None
        # What the provider says it actually charged, when it says so at all
        # (OpenRouter only). None everywhere else, and the pinned-rate
        # estimate below stands.
        reported_cost: float | None = None
        primary_error: Exception | None = None
        # Captured once, before the retry loop, so they reflect what THIS call
        # was CONFIGURED to use regardless of how the loop below resolves —
        # never mutated by retries/failover (see actual_provider below, which
        # is derived from actual_model AFTER the loop and can legitimately
        # differ on fallback).
        requested_model = self.model
        requested_provider = self._provider
        prompt_version = hashlib.sha256(self.system_prompt.encode("utf-8")).hexdigest()[:12]
        reservation = None
        provider_requests = 0
        attempt_errors: list[BaseException] = []
        governed_estimate = 0
        governed_provider: str | None = None

        def _mark_circuit_unavailable(exc: BaseException) -> dict:
            marker = getattr(self._cost_circuit, "mark_unavailable", None)
            if callable(marker):
                try:
                    return marker(
                        exc,
                        run_id=getattr(reservation, "run_id", None),
                        mode=getattr(reservation, "mode", None),
                        agent_name=self.name,
                        attempts=provider_requests,
                    )
                except Exception as marker_exc:  # the fallback alert must not leak
                    logger.critical(
                        "Cost-circuit failure marker also failed: %s",
                        marker_exc, exc_info=True,
                    )
            sentinel = UnavailableLLMCostCircuit(
                exc, notifier=getattr(self._cost_circuit, "notifier", None),
            )
            return sentinel.activate_session(
                getattr(reservation, "run_id", "unscoped"),
                getattr(reservation, "mode", "unknown"),
            )

        def _record_attempt_failure(exc: BaseException) -> None:
            """Keep every attempt's failure, not just the one re-raised.

            A logical call can make several provider attempts against
            DIFFERENT providers, and only the primary's error survives to the
            caller — `run()` re-raises `primary_error` and discards whatever
            the failover hit. The cost circuit then judges whether the call
            provably cost nothing from that single exception, so a failover
            rejected with 401 (billed nothing, by definition) was invisible to
            it and the whole call was charged its conservative reserve at the
            FAILOVER model's price. On 2026-08-31 that put $0.62 on the ledger
            for two refusals and a missing credential that together cost $0,
            and the unexplained spend then latched the desk.
            """
            attempt_errors.append(exc)

        def _safe_fail_reservation(exc: BaseException) -> None:
            if self._cost_circuit is None or reservation is None:
                return
            if exc not in attempt_errors:
                attempt_errors.append(exc)
            try:
                self._cost_circuit.fail_call(
                    reservation, exc, attempt_errors=list(attempt_errors),
                )
            except Exception as accounting_exc:
                logger.critical(
                    "Cost-circuit failure accounting failed closed: %s",
                    accounting_exc, exc_info=True,
                )
                _mark_circuit_unavailable(accounting_exc)

        def _govern(model: str, *, is_failover: bool = False) -> None:
            """Pace this request so the desk never floods a provider.

            Deliberately placed AFTER the cost circuit has authorized the
            attempt and immediately before the request leaves: money is
            checked first, then speed, and there is exactly one path through
            here — every transport and the failover all authorize through
            this closure, so nothing can send without being counted.

            `is_failover` is threaded explicitly (not sniffed from `model`)
            so a failover call is always charged to the FALLBACK provider's
            governor, never the primary's — see _governor_domain_for.
            """
            nonlocal governed_estimate, governed_provider
            governed_provider = _governor_domain_for(model, self, is_failover=is_failover)
            governor = _TOKEN_GOVERNORS.get(governed_provider)
            if governor is None:
                governed_estimate = 0
                return
            chars = len(self.system_prompt) + len(user_message)
            governed_estimate = int(chars / _GOVERNOR_CHARS_PER_TOKEN) + self.max_tokens
            governor.charge(governed_estimate)

        def _authorize(model: str, *, is_failover: bool = False) -> None:
            nonlocal provider_requests
            if self._cost_circuit is None:
                _govern(model, is_failover=is_failover)
                provider_requests += 1
                return
            try:
                self._cost_circuit.before_provider_attempt(reservation, model=model)
            except PaidAnalysisSuspended:
                raise
            except Exception as exc:
                state = _mark_circuit_unavailable(exc)
                raise PaidAnalysisSuspended(
                    "mandatory cost-circuit authorization failed",
                    state,
                ) from exc
            _govern(model, is_failover=is_failover)
            provider_requests += 1

        def _authorize_failover(model: str) -> None:
            _authorize(model, is_failover=True)

        if self._cost_circuit is None and not self._allow_unmetered_for_tests:
            raise PaidAnalysisSuspended(
                "mandatory paid-analysis cost circuit is not attached",
                {
                    "available": False,
                    "suspended": True,
                    "trigger_code": "cost_circuit_not_attached",
                    "agent_name": self.name,
                },
            )
        if self._cost_circuit is not None:
            try:
                reservation = self._cost_circuit.begin_call(
                    agent_name=self.name,
                    model=self.model,
                    system_prompt=self.system_prompt,
                    user_message=user_message,
                    max_output_tokens=self.max_tokens,
                    retry_kind=retry_kind,
                    optional_retry=optional_retry,
                )
            except OptionalPaidAnalysisRetrySkipped:
                raise
            except PaidAnalysisSuspended:
                raise
            except Exception as exc:
                state = _mark_circuit_unavailable(exc)
                raise PaidAnalysisSuspended(
                    "mandatory cost-circuit reservation failed",
                    state,
                ) from exc
        for attempt in range(max_retries):
            try:
                if self._use_deepseek:
                    (raw_text, input_tokens, output_tokens, finish_reason,
                     reported_cost) = self._call_deepseek(
                        user_message, authorize=_authorize,
                    )
                elif self._use_openrouter or self._use_google:
                    # OpenRouter and Google AI Studio's compat endpoint are
                    # both OpenAI-wire-compatible — reuse _call_openai
                    # unmodified rather than duplicating the streaming/usage/
                    # empty-content logic.
                    (raw_text, input_tokens, output_tokens, finish_reason,
                     reported_cost) = self._call_openai(
                        user_message, authorize=_authorize,
                    )
                elif self._use_openai:
                    (raw_text, input_tokens, output_tokens, finish_reason,
                     reported_cost) = self._call_openai(
                        user_message, authorize=_authorize,
                    )
                else:
                    (raw_text, input_tokens, output_tokens, finish_reason,
                     reported_cost) = self._call_anthropic(
                        user_message, authorize=_authorize,
                    )
                primary_error = None
                break
            except PaidAnalysisSuspended as exc:
                _safe_fail_reservation(exc)
                raise
            except Exception as e:
                primary_error = e
                _record_attempt_failure(e)
                # Non-retryable (auth / bad-request / 4xx / context-length):
                # stop retrying — sleeping won't help. (Was: raise. Now we
                # break so the cross-provider failover below can still try.)
                if not _is_retryable(e):
                    logger.warning(
                        "Agent %s attempt %d hit a non-retryable error: %s. "
                        "No more retries.", self.name, attempt + 1, e,
                    )
                    break
                # Last attempt: stop — sleeping then giving up wastes the
                # final backoff on nothing.
                if attempt == max_retries - 1:
                    logger.warning("Agent %s attempt %d failed: %s. Primary exhausted.",
                                   self.name, attempt + 1, e)
                    break
                # Wall-clock deadline: the attempt budget alone doesn't bound
                # time (each attempt can burn 120-380s in the relay-524 mode),
                # so exhausting it can collide with the wrapper's 1200s kill —
                # which is where the failover below became unreachable
                # (2026-06-08/09). Past the deadline, abandon the primary NOW
                # so failover fires while the session window still has room.
                elapsed = time.monotonic() - loop_start
                if elapsed >= deadline_s:
                    logger.warning(
                        "Agent %s attempt %d failed: %s. Retry deadline %.0fs "
                        "exceeded (elapsed %.0fs) — abandoning primary, "
                        "proceeding to failover if configured.",
                        self.name, attempt + 1, e, deadline_s, elapsed,
                    )
                    break
                wait = _retry_backoff_seconds(attempt)
                # Honor a server retry-after hint (429/5xx): sleeping shorter
                # than the server asked just burns attempts against a closed
                # door. Capped so a hostile hint can't stall the session.
                hint = _retry_after_hint_seconds(e)
                if hint is not None:
                    wait = min(max(wait, hint), _RETRY_AFTER_CAP_S)
                logger.warning("Agent %s attempt %d failed: %s. Retrying in %.1fs...",
                               self.name, attempt + 1, e, wait)
                time.sleep(wait)

        # Model that actually produced the output — primary unless failover wins.
        actual_model = self.model
        if primary_error is not None:
            # Primary failed after retries. Try ONE call on the configured
            # fallback (provider, model) so a quota/balance/auth/rate-limit/
            # outage on the primary keeps the session alive (DeepSeek 402
            # "Insufficient Balance" and the 2026-08-31 OpenRouter rate-limit
            # are both instances of the same shape this was built for).
            # Single-shot (no retry) to stay inside the session window. Only
            # when the fallback pair actually differs from the primary's
            # (see _failover_reachable — never fail over onto the exact
            # thing that just failed); otherwise re-raise.
            failover = None
            if not single_provider_attempt and self._failover_reachable:
                try:
                    failover = self._try_failover(
                        user_message, primary_error, authorize=_authorize_failover,
                        on_failure=_record_attempt_failure,
                    )
                except PaidAnalysisSuspended as exc:
                    _safe_fail_reservation(exc)
                    raise
            if failover is None:
                _safe_fail_reservation(primary_error)
                raise primary_error
            (raw_text, input_tokens, output_tokens, finish_reason,
             reported_cost) = failover
            actual_model = self._fallback_model

        # Truncation detection: a max_tokens / length cutoff means the output
        # is incomplete, NOT a deliberate "no action". Flag + log loudly so a
        # truncated decision isn't silently collapsed into "no trades".
        # max_tokens (Anthropic) / length (OpenAI+DeepSeek) = hit the ceiling;
        # insufficient_system_resource = DeepSeek cut-off-on-200. Shared
        # constant with the empty-content guards in the _call_* paths.
        truncated = (isinstance(finish_reason, str)
                     and finish_reason.lower() in _TRUNCATION_FINISH_REASONS)
        if truncated:
            logger.warning(
                "Agent %s response was TRUNCATED (finish_reason=%s) — output is "
                "incomplete, likely hit max_tokens=%d. Treat downstream None as "
                "'cut off', not 'no signal'.",
                self.name, finish_reason, self.max_tokens,
            )

        # The governor's window must reflect what was really sent, not what
        # the pre-request estimate guessed, or the ceiling quietly means
        # something other than it says.
        if governed_provider and governed_estimate:
            governor = _TOKEN_GOVERNORS.get(governed_provider)
            if governor is not None:
                governor.reconcile(governed_estimate, input_tokens + output_tokens)

        tokens = input_tokens + output_tokens
        # Cost computation — uses src.cost_table.PRICING. Returns None
        # when model is unknown so the operator sees `$?.??` and knows
        # to update the table (vs silently understating with $0.00).
        # Also returns None when token counts are both 0 — that
        # represents "we got a response but no usage data", which the
        # operator should investigate rather than see logged as a
        # confident $0.00 entry that gets summed into daily totals.
        if input_tokens == 0 and output_tokens == 0:
            cost = None
            logger.warning(
                "Agent %s completed with zero tokens reported — flagging cost as unknown. "
                "Either the SDK didn't return usage data, or the call somehow consumed nothing. "
                "Check the LLM response and update _extract_*_usage if there's a new shape.",
                self.name,
            )
        elif reported_cost is not None:
            # The provider billed this call and told us what it charged. Prefer
            # it over the pinned per-model rate, which cannot be right for a
            # model OpenRouter serves from endpoints at different prices — it
            # would over-report on the cheap endpoint (starving the daily
            # budget of headroom it actually has) and under-report on the dear
            # one. Only the estimate is a guess; this is the invoice.
            cost = reported_cost
            estimated = estimate_cost(actual_model, input_tokens, output_tokens)
            if estimated is not None and estimated > 0:
                ratio = cost / estimated
                # A large gap is not necessarily wrong — it is exactly what a
                # half-price endpoint looks like — but it means the pinned
                # table no longer describes what this seat pays, and every
                # projection built on that table (project_session_cost.py, the
                # circuit's worst-case reservation) is off by this factor.
                if ratio < 0.5 or ratio > 1.5:
                    logger.warning(
                        "Agent %s: provider-reported cost %s differs from the "
                        "pinned-rate estimate %s (%.2fx) for %s — the reported "
                        "figure is authoritative and is what was recorded, but "
                        "the pinned rate for this model no longer reflects the "
                        "endpoint serving it.",
                        self.name, fmt_cost(cost), fmt_cost(estimated),
                        ratio, actual_model,
                    )
        else:
            cost = estimate_cost(actual_model, input_tokens, output_tokens)
        if self._cost_circuit is not None and reservation is not None:
            try:
                self._cost_circuit.complete_call(
                    reservation, cost, actual_model=actual_model,
                    # Every attempt that did NOT produce this response.
                    # Empty on a clean first-attempt success; on a retry or a
                    # successful failover it names what the earlier attempts
                    # failed with, which is the only evidence the circuit has
                    # that those attempts billed nothing (see complete_call's
                    # `failed_attempt_reserve` note -- 2026-09-02).
                    failed_attempt_errors=list(attempt_errors),
                )
            except PaidAnalysisSuspended:
                raise
            except Exception as exc:
                state = _mark_circuit_unavailable(exc)
                raise PaidAnalysisSuspended(
                    "mandatory cost-circuit completion accounting failed",
                    state,
                ) from exc
        logger.info(
            "Agent %s completed | tokens in=%d out=%d total=%d | cost=%s | model=%s",
            self.name, input_tokens, output_tokens, tokens,
            fmt_cost(cost), actual_model,
        )
        logger.info("Agent %s output:\n%s", self.name, raw_text)
        used_fallback = primary_error is not None
        # A vendor/model OpenRouter id is not inferable from its text. Primary
        # success therefore uses the configured provider; only a failover
        # changes attribution, to whichever provider is actually CONFIGURED
        # as the fallback (never a fabricated/hardcoded "anthropic" string).
        actual_provider = self._fallback_provider if used_fallback else requested_provider
        latency_s = time.monotonic() - loop_start
        return AgentResult(
            raw_text=raw_text,
            tokens_used=tokens,
            model=actual_model,
            user_message=user_message,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=cost,
            finish_reason=finish_reason,
            truncated=truncated,
            requested_model=requested_model,
            requested_provider=requested_provider,
            actual_provider=actual_provider,
            used_fallback=used_fallback,
            prompt_version=prompt_version,
            latency_s=latency_s,
            provider_requests=provider_requests,
        )

    def _anthropic_call(
        self, client, model: str, user_message: str, *, authorize=None,
    ) -> tuple[str, int, int, str | None, float | None]:
        """One Anthropic messages.create against an arbitrary client+model.

        Shared by the primary path (_call_anthropic) and a cross-provider
        failover that lands on Anthropic (_try_failover, when
        `fallback_provider: anthropic` is configured) so both use the
        identical request shape + usage/finish-reason extraction. Prompt
        caching is intentionally not enabled: the breaker uses the pinned
        ordinary-input rates, so enabling vendor-specific cache write/read
        pricing would make reservations and reported cost incomparable until
        that pricing is modeled explicitly.
        """
        with _ANTHROPIC_LLM_SEMAPHORE:
            if authorize is not None:
                authorize(model)
            response = client.messages.create(
                model=model,
                max_tokens=self.max_tokens,
                system=self.system_prompt,
                messages=[{"role": "user", "content": user_message}],
            )
        in_tok, out_tok = _extract_anthropic_usage(response, self.name)
        finish_reason = getattr(response, "stop_reason", None)
        if not isinstance(finish_reason, str):
            finish_reason = None
        if not response.content or not hasattr(response.content[0], "text"):
            if finish_reason in _TRUNCATION_FINISH_REASONS:
                # Legit truncation (whole budget burned before any text) —
                # surface as truncated '', don't retry/fail over.
                logger.warning("Anthropic returned empty content (stop_reason=%s)", finish_reason)
                return ("", in_tok, out_tok, finish_reason, None)
            raise LLMEmptyResponseError(
                f"Anthropic returned empty content (stop_reason={finish_reason})"
            )
        return (response.content[0].text, in_tok, out_tok, finish_reason, None)

    def _call_anthropic(
        self, user_message: str, *, authorize=None,
    ) -> tuple[str, int, int, str | None, float | None]:
        return self._anthropic_call(
            self.client, self.model, user_message, authorize=authorize,
        )

    def _try_failover(self, user_message: str, primary_error: Exception, *,
                      authorize=None, on_failure=None):
        """Primary provider exhausted its retries → attempt ONE call on the
        configured fallback (provider, model). Returns the (text, in_tok,
        out_tok, finish_reason, reported_cost) tuple on success, or None on
        failure (caller re-raises the original primary error). Single-shot:
        the primary already burned its retry budget, so a second full budget
        here could blow the session window. Loud logging either way — a
        provider failover is an event the operator must see.

        Builds its own client via `_build_llm_client` (the same helper
        __init__ uses for the primary) rather than duplicating client-
        construction logic, and dispatches to the Anthropic SDK path when the
        fallback provider is Anthropic, or the shared OpenAI-wire path
        (`_openai_wire_call`) for everything else — OpenAI, DeepSeek,
        OpenRouter, or Google all speak that same chat.completions shape.
        """
        logger.error(
            "Agent %s: primary %s/%s failed after retries (%s) — failing over "
            "to %s/%s.", self.name, self._provider, self.model, primary_error,
            self._fallback_provider, self._fallback_model,
        )
        try:
            client = _build_llm_client(self._fallback_provider, self._fallback_api_key)
            if self._fallback_provider == "anthropic":
                result = self._anthropic_call(
                    client, self._fallback_model, user_message, authorize=authorize,
                )
            else:
                result = self._openai_wire_call(
                    client, self._fallback_model, self._fallback_provider,
                    user_message, authorize=authorize,
                )
            logger.warning(
                "Agent %s: FAILOVER to %s/%s SUCCEEDED (in=%d out=%d) — "
                "session continues.", self.name, self._fallback_provider,
                self._fallback_model, result[1], result[2],
            )
            return result
        except PaidAnalysisSuspended:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "Agent %s: failover to %s/%s also FAILED: %s. Re-raising the "
                "original primary error.", self.name, self._fallback_provider,
                self._fallback_model, exc,
            )
            if on_failure is not None:
                on_failure(exc)
            return None

    def _openai_wire_call(
        self, client, model: str, provider: str, user_message: str, *,
        provider_order: list[str] | None = None, authorize=None,
    ) -> tuple[str, int, int, str | None, float | None]:
        """One streamed chat.completions call against an arbitrary
        client+model+provider. Shared by every OpenAI-wire-compatible
        provider — OpenAI, DeepSeek's cousin OpenRouter, Google's compat
        endpoint — and by a cross-provider failover landing on any of them
        (_try_failover). `_call_openai` is a thin wrapper over this for the
        primary path.

        STREAMED on purpose. The OPENAI_BASE_URL relay sits behind
        Cloudflare, whose ~120s Proxy Read Timeout (HTTP 524) kills any call
        that sends zero bytes until the model finishes — and PM / tech /
        evening generations legitimately run 120s+, so non-streaming could
        never succeed through the relay (the 2026-06-08/09 outage: every long
        call 524'd, book froze sell-only). Streaming keeps bytes flowing so
        the proxy window never trips; _LLM_HTTP_TIMEOUT becomes a per-chunk
        read timeout, so a long *healthy* generation isn't axed either.

        The semaphore covers create + iteration: for a streamed response the
        request is in flight (and counts against a relay's per-user
        concurrency cap) until the last chunk is read. OpenRouter and Google
        are each a distinct account/rate-limit domain, so each gets its own
        semaphore rather than contending with the OpenAI relay's cap.
        """
        semaphore = {
            "openrouter": _OPENROUTER_LLM_SEMAPHORE,
            "google": _GOOGLE_LLM_SEMAPHORE,
        }.get(provider, _OPENAI_LLM_SEMAPHORE)
        # OpenRouter-only request extras. `usage.include` makes OpenRouter
        # return what it ACTUALLY charged for the call, which is the only
        # honest way to price a model served from endpoints at different
        # rates; `provider.order` expresses this seat's endpoint preference.
        extra_body: dict = {}
        if provider == "openrouter":
            extra_body["usage"] = {"include": True}
            if provider_order:
                extra_body["provider"] = {
                    "order": list(provider_order),
                    "allow_fallbacks": True,
                }
        with semaphore:
            if authorize is not None:
                authorize(model)
            stream = client.chat.completions.create(
                model=model,
                max_completion_tokens=self.max_tokens,
                messages=[
                    {"role": "system", "content": self.system_prompt},
                    {"role": "user", "content": user_message},
                ],
                stream=True,
                stream_options={"include_usage": True},
                **({"extra_body": extra_body} if extra_body else {}),
            )
            parts: list[str] = []
            finish_reason: str | None = None
            usage = None
            # The id OpenRouter's /generation lookup is keyed on (see
            # _recover_openrouter_generation below) — every chunk on a real
            # stream repeats the same generation id, so keep the last
            # non-empty one seen. isinstance-guarded (not just truthy) so a
            # test double's auto-attribute MagicMock — truthy but not a str —
            # can never be mistaken for a real id.
            generation_id: str | None = None
            for chunk in stream:
                chunk_id = getattr(chunk, "id", None)
                if isinstance(chunk_id, str) and chunk_id:
                    generation_id = chunk_id
                # A mid-stream provider error. OpenRouter cannot change the
                # HTTP status once the first byte is out, so it reports the
                # failure as a top-level `error` object on a chunk (with
                # finish_reason "error"). Checked BEFORE content/usage: this
                # chunk carries no usable output, and letting it fall through
                # produced an empty body whose exception had no status_code —
                # which the cost circuit then charged in full. See
                # LLMStreamErrorChunk. Read defensively via model_extra too,
                # since the OpenAI SDK models the field as an extra.
                chunk_error = getattr(chunk, "error", None)
                if chunk_error is None:
                    extra = getattr(chunk, "model_extra", None) or {}
                    chunk_error = extra.get("error") if isinstance(extra, dict) else None
                if chunk_error:
                    if isinstance(chunk_error, dict):
                        code = chunk_error.get("code")
                        message = chunk_error.get("message") or ""
                        meta = chunk_error.get("metadata") or {}
                        etype = meta.get("error_type") if isinstance(meta, dict) else None
                    else:
                        code = getattr(chunk_error, "code", None)
                        message = getattr(chunk_error, "message", "") or ""
                        meta = getattr(chunk_error, "metadata", None)
                        etype = getattr(meta, "error_type", None) if meta else None
                    # `code` is documented as the HTTP status the response
                    # would have carried had the headers not already gone.
                    status = code if isinstance(code, int) and not isinstance(code, bool) else None
                    logger.warning(
                        "Agent %s: provider error INSIDE the stream "
                        "(code=%s type=%s): %s — surfacing with its status so "
                        "the cost circuit can judge whether it billed.",
                        self.name, status, etype, message,
                    )
                    raise LLMStreamErrorChunk(
                        f"provider error mid-stream (code={status}, "
                        f"type={etype}): {message}",
                        status_code=status, error_type=etype,
                    )
                # include_usage delivers usage on a final extra chunk whose
                # choices list is empty.
                chunk_usage = getattr(chunk, "usage", None)
                if chunk_usage is not None:
                    usage = chunk_usage
                choices = getattr(chunk, "choices", None) or []
                if not choices:
                    continue
                choice = choices[0]
                delta = getattr(choice, "delta", None)
                piece = getattr(delta, "content", None) if delta is not None else None
                if piece:
                    parts.append(piece)
                fr = getattr(choice, "finish_reason", None)
                if isinstance(fr, str):
                    finish_reason = fr
        content = "".join(parts)
        if finish_reason is None:
            # Stream ended without a finish_reason = connection cut
            # mid-generation (relay drop, no error frame). Partial text must
            # NOT be returned as success — a half-emitted PM decision parses
            # like 'no trades'. Raise retryable instead.
            raise LLMStreamInterruptedError(
                f"OpenAI-wire stream ended without finish_reason after {len(content)} "
                "chars — connection cut mid-generation; partial output discarded"
            )
        if not content and finish_reason not in _TRUNCATION_FINISH_REASONS:
            # Degenerate 200 (empty body / refusal / stripped relay response):
            # entering the retry → failover machinery beats masquerading as a
            # clean no-signal. Truncation-family reasons are exempt — an empty
            # body there is a legit ceiling hit, flagged via truncated=True.
            raise LLMEmptyResponseError(
                f"OpenAI-wire call returned empty content (finish_reason={finish_reason})"
            )
        if usage is not None:
            in_tok = _coerce_token_count(getattr(usage, "prompt_tokens", 0))
            out_tok = _coerce_token_count(getattr(usage, "completion_tokens", 0))
            reported_cost = _reported_cost_usd(usage, self.name)
        else:
            # Character heuristics are not billing telemetry. The default,
            # UNLESS the OpenRouter recovery below finds a real figure, is
            # still 0/0/unknown — deliberately routing this success through
            # the breaker's unknown-actual-cost path: retain the full
            # reservation and latch instead of releasing it against a soft
            # estimate.
            in_tok, out_tok, reported_cost = 0, 0, None
            recovered = None
            if self._use_openrouter and generation_id:
                recovered = _recover_openrouter_generation(
                    generation_id, self.client.api_key, self.name,
                )
            if recovered is not None:
                in_tok, out_tok, reported_cost = recovered
                logger.warning(
                    "Agent %s: stream for generation %s carried no usage "
                    "chunk, but OpenRouter's /generation endpoint recovered "
                    "the real billing after the fact — cost=%s tokens_in=%d "
                    "tokens_out=%d. This REPLACES what would otherwise have "
                    "settled as a full conservative reservation charge.",
                    self.name, generation_id, fmt_cost(reported_cost),
                    in_tok, out_tok,
                )
            else:
                # Either not OpenRouter, no generation id was ever seen on
                # the stream, or the bounded recovery gave up (its own
                # warning states which, and why) — either way the cost stays
                # unknown and this settles at the conservative
                # full-reservation charge, exactly as before this path
                # existed.
                logger.warning(
                    "OpenAI-wire stream for %s carried no usage chunk — token "
                    "counts and actual cost are unknown; paid analysis will "
                    "suspend.",
                    self.name,
                )
        return (content, in_tok, out_tok, finish_reason, reported_cost)

    def _call_openai(
        self, user_message: str, *, authorize=None,
    ) -> tuple[str, int, int, str | None, float | None]:
        """Primary-path entry point for every OpenAI-wire-compatible
        provider (OpenAI, OpenRouter, Google) — delegates to
        `_openai_wire_call` with this agent's own client/model/provider."""
        return self._openai_wire_call(
            self.client, self.model, self._provider, user_message,
            provider_order=self._provider_order, authorize=authorize,
        )

    def _deepseek_max_output(self) -> int:
        """Clamp ceiling for this DeepSeek model. DeepSeek REJECTS (does not
        clamp) a max_tokens above the model limit, so we cap client-side."""
        return _DEEPSEEK_MAX_OUTPUT.get(self.model, _DEEPSEEK_DEFAULT_CEILING)

    def _call_deepseek(
        self, user_message: str, *, authorize=None,
    ) -> tuple[str, int, int, str | None, float | None]:
        """DeepSeek via the OpenAI SDK (custom base_url). Three deltas vs
        _call_openai:
          1. Sends `max_tokens` (DeepSeek ignores OpenAI's `max_completion_tokens`
             → output would silently fall back to a ~4096 default and truncate).
          2. Clamps to the per-model output ceiling (DeepSeek 400s on over-ceiling
             values instead of clamping).
          3. Reads the non-standard reasoning_content defensively. We DISCARD the
             chain-of-thought (every agent parses JSON from `content`), but log its
             presence so an empty-content / full-CoT truncation is visible rather
             than looking like a clean "no signal".
        Usage is OpenAI-shaped (prompt_tokens / completion_tokens) → reuse
        _extract_openai_usage.
        """
        if authorize is not None:
            authorize(self.model)
        response = self.client.chat.completions.create(
            model=self.model,
            max_tokens=min(self.max_tokens, self._deepseek_max_output()),
            messages=[
                {"role": "system", "content": self.system_prompt},
                {"role": "user", "content": user_message},
            ],
        )
        choice = response.choices[0]
        content = choice.message.content or ""
        finish_reason = getattr(choice, "finish_reason", None)
        if not isinstance(finish_reason, str):
            finish_reason = None
        if not content:
            reasoning = getattr(choice.message, "reasoning_content", None)
            if finish_reason not in _TRUNCATION_FINISH_REASONS:
                # Same guard as _call_openai: a degenerate 200 must enter the
                # retry/failover machinery, not pass as a clean no-signal.
                raise LLMEmptyResponseError(
                    f"DeepSeek returned empty content (finish_reason={finish_reason}, "
                    f"reasoning_content present={bool(reasoning)})"
                )
            # Truncation-family: reasoner burned the whole budget on CoT —
            # legit empty, surfaced via truncated=True downstream.
            logger.warning(
                "DeepSeek returned empty content (finish_reason=%s, reasoning_content present=%s)",
                finish_reason, bool(reasoning),
            )
        in_tok, out_tok = _extract_openai_usage(response, self.name)
        return (content, in_tok, out_tok, finish_reason, None)


# === Provider-specific usage extraction ===
# Both helpers (a) handle the rare case where `response.usage` is missing
# (some SDK error paths), (b) emit a WARNING when usage data is absent
# so the operator notices instead of silently logging $0 cost, and
# (c) for Anthropic, fold in the cache_creation / cache_read token
# fields. Currently we don't use prompt caching, so cache_* fields are
# always 0 and the sum equals input_tokens. If caching is ever enabled,
# this layer would need a corresponding rate adjustment in cost_table
# (cache writes = 1.25x input rate, cache reads = 0.1x) — until then
# the simple sum is harmless and forward-compatible.

def _coerce_token_count(value) -> int:
    """Return value as int iff it really IS an int (numpy.int64 subclasses
    int, so those work too). Anything else — None, MagicMock auto-attrs,
    a stray dict, a string — coerces to 0.

    This is defensive against two cases that have actually shown up:
      (1) tests using ``MagicMock`` without an explicit spec — attribute
          access auto-creates a child MagicMock whose ``__int__`` returns
          1, which would silently add +1 to every uncovered token field
          (caught by the R7 self-audit: existing tests started failing
          with 'assert 2502 == 2500' after we began summing the cache
          fields, because the cache fields weren't set in the mocks).
      (2) future SDK changes that turn a numeric field into a string
          or object — better to under-count than crash, since the
          run() layer flags 0+0 tokens as cost=unknown anyway.
    """
    # bool is a subclass of int but we never want to treat True/False as
    # token counts of 1/0.
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value
    return 0


def _reported_cost_usd(usage, agent_name: str) -> float | None:
    """The USD OpenRouter says it actually charged for a call, or None.

    Present only when the request asked for it (`usage: {include: true}`,
    OpenRouter-only) — every other provider returns None here and keeps the
    pinned-rate estimate. It matters because OpenRouter serves one model id
    from endpoints at different prices (`openai/flex` at half `openai`'s rate
    for the same `gpt-5.5` weights), so a table keyed on the model id alone
    cannot price the call: it is right for one endpoint and wrong for the
    other. Under-reporting is the dangerous direction — the daily cost
    circuit spends against these numbers — so anything that is not a finite,
    non-negative real number degrades to None and the estimate stands.

    `bool` is excluded for the same reason as in _coerce_token_count: it is a
    subclass of int and True would silently price a call at $1.
    """
    if usage is None:
        return None
    value = getattr(usage, "cost", None)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value) or value < 0:
        logger.warning(
            "Agent %s: OpenRouter reported an unusable cost (%r) — falling "
            "back to the pinned-rate estimate.", agent_name, value,
        )
        return None
    return float(value)


# Bounded retry budget for the post-hoc OpenRouter generation lookup below.
# Measured live 2026-08-31: querying a generation id immediately after its
# stream closed 404'd twice in a row, and the SAME id then resolved cleanly
# minutes later — OpenRouter's billing pipeline is asynchronous, not
# instant. Seconds of retrying here will therefore rarely out-wait a genuine
# not-yet-indexed 404; the budget exists to absorb transient blips (a slow
# DNS lookup, a dropped packet, an id that happens to land quickly) without
# meaningfully delaying a live trading session. 3 attempts x 1.0s timeout +
# 2 sleeps x 1.0s between them bounds the worst case (every attempt times
# out) at 5s, comfortably under the ~6s ceiling — well short of blocking the
# session, and if it fails the call still settles via the existing
# conservative fallback exactly as it did before this path existed.
_OPENROUTER_GENERATION_LOOKUP_ATTEMPTS = 3
_OPENROUTER_GENERATION_LOOKUP_TIMEOUT_S = 1.0
_OPENROUTER_GENERATION_LOOKUP_SLEEP_S = 1.0
_OPENROUTER_GENERATION_URL = "https://openrouter.ai/api/v1/generation"


def _recover_openrouter_generation(
    generation_id: str, api_key: str, agent_name: str,
) -> tuple[int, int, float] | None:
    """Best-effort recovery of the real cost/tokens for a streamed
    OpenRouter call whose stream carried no usage chunk (e.g. a relay that
    strips stream_options include_usage).

    OpenRouter retains the true billed cost for every generation — streamed
    calls included — behind GET /generation?id=..., but see the module
    comment above _OPENROUTER_GENERATION_LOOKUP_ATTEMPTS: the record is not
    immediately queryable, so this makes a small bounded number of short
    attempts and gives up rather than blocking a live session on an
    endpoint that may genuinely take minutes to catch up.

    Returns (tokens_prompt, tokens_completion, total_cost_usd) on success.
    Returns None on ANY failure — 404, timeout, network error, malformed
    body, non-finite/negative numbers — so the caller falls back to today's
    unknown-cost behavior. NEVER raises; this must never make a call worse
    than it already was.
    """
    import requests  # lazy import — same pattern as src/cost_table.py

    last_reason = "no attempts made"
    for attempt in range(_OPENROUTER_GENERATION_LOOKUP_ATTEMPTS):
        try:
            resp = requests.get(
                _OPENROUTER_GENERATION_URL,
                params={"id": generation_id},
                headers={"Authorization": f"Bearer {api_key}"},
                timeout=_OPENROUTER_GENERATION_LOOKUP_TIMEOUT_S,
            )
            if resp.status_code == 404:
                last_reason = "404 (generation not yet indexed)"
            else:
                resp.raise_for_status()
                body = resp.json()
                data = body.get("data") if isinstance(body, dict) else None
                if not isinstance(data, dict):
                    last_reason = f"malformed response body: {body!r}"
                else:
                    cost = data.get("total_cost")
                    tok_in = data.get("tokens_prompt")
                    tok_out = data.get("tokens_completion")
                    cost_ok = (
                        isinstance(cost, (int, float)) and not isinstance(cost, bool)
                        and math.isfinite(cost) and cost >= 0
                    )
                    tok_in_ok = (
                        isinstance(tok_in, int) and not isinstance(tok_in, bool)
                        and tok_in >= 0
                    )
                    tok_out_ok = (
                        isinstance(tok_out, int) and not isinstance(tok_out, bool)
                        and tok_out >= 0
                    )
                    if cost_ok and tok_in_ok and tok_out_ok:
                        return (tok_in, tok_out, float(cost))
                    last_reason = f"unusable fields in response: {data!r}"
        except Exception as exc:
            last_reason = f"{type(exc).__name__}: {exc}"
        if attempt < _OPENROUTER_GENERATION_LOOKUP_ATTEMPTS - 1:
            time.sleep(_OPENROUTER_GENERATION_LOOKUP_SLEEP_S)
    logger.warning(
        "Agent %s: OpenRouter generation lookup for %s did not recover "
        "billing after %d attempt(s) — %s. Cost stays unknown; this call "
        "will settle at the full conservative reservation.",
        agent_name, generation_id, _OPENROUTER_GENERATION_LOOKUP_ATTEMPTS,
        last_reason,
    )
    return None


def _extract_anthropic_usage(response, agent_name: str) -> tuple[int, int]:
    usage = getattr(response, "usage", None)
    if usage is None:
        logger.warning(
            "Anthropic response for %s missing usage object — cost will be flagged as unknown",
            agent_name,
        )
        return (0, 0)
    in_tok = _coerce_token_count(getattr(usage, "input_tokens", 0))
    cache_create = _coerce_token_count(getattr(usage, "cache_creation_input_tokens", 0))
    cache_read = _coerce_token_count(getattr(usage, "cache_read_input_tokens", 0))
    out_tok = _coerce_token_count(getattr(usage, "output_tokens", 0))
    # Sum across cache fields so token COUNT is correct even with caching.
    # Cost rates will need a separate refactor if caching is enabled
    # (cache write = 1.25x input rate, cache read = 0.1x).
    return (in_tok + cache_create + cache_read, out_tok)


def _extract_openai_usage(response, agent_name: str) -> tuple[int, int]:
    usage = getattr(response, "usage", None)
    if usage is None:
        logger.warning(
            "OpenAI response for %s missing usage object — cost will be flagged as unknown",
            agent_name,
        )
        return (0, 0)
    prompt_tokens = _coerce_token_count(getattr(usage, "prompt_tokens", 0))

    # Cache accounting, measured rather than assumed (2026-08-31). Splitting a
    # batch into more, smaller requests repeats the system prompt more often,
    # and whether that costs anything depends entirely on whether the provider
    # is serving it from cache. On this seat's route a cached prompt token is
    # billed at $0.01/M against $0.10/M — a 10x discount that decides the
    # trade-off outright. Nobody knew which way it went because nothing ever
    # read the field. Reported, not acted on: this only ever logs.
    cached = 0
    details = getattr(usage, "prompt_tokens_details", None)
    if details is not None:
        cached = _coerce_token_count(getattr(details, "cached_tokens", 0))
    if cached:
        logger.info(
            "Agent %s: %d of %d prompt tokens served from the provider's cache "
            "(%.0f%%) — repeated system prompts are being discounted.",
            agent_name, cached, prompt_tokens,
            (cached / prompt_tokens * 100) if prompt_tokens else 0.0,
        )
    return (
        prompt_tokens,
        _coerce_token_count(getattr(usage, "completion_tokens", 0)),
    )
