"""src.cost_circuit.classification -- moved verbatim from src/cost_circuit.py; see the package docstring."""

from __future__ import annotations
import logging
import sqlite3
from typing import Any, Callable, TypeVar

logger = logging.getLogger(__name__)


_T = TypeVar("_T")

# Expected budget exhaustion is not an infrastructure incident.  Keep those
# stops scoped to the budget window that owns them, while every unknown or
# integrity-related trigger defaults to the durable operator-reset latch.
#
# 2026-09-02 (docs/WORK.md item 14): every projection-based trigger this
# module used to raise ("projected_*_cost_limit", "provider_projected_*",
# "outstanding_projected_*", "mode_daily_spend_limit", the morning reserve,
# the free-failure-session backstop, "session_retry_attempt_limit",
# "provider_attempt_limit") is gone along with the reservation layer that
# computed them. Only two non-hard triggers remain: "daily_cost_limit" and
# "session_cost_limit" fire on REAL SETTLED spend and self-heal at the next
# ET-day rollover / a fresh run_id respectively -- see
# `_enforce_settled_limits_locked`. "session_call_count_limit" is the new
# runaway-loop backstop (item 14c) and is session-scoped for the same reason.
_DAY_QUOTA_TRIGGERS = frozenset({"daily_cost_limit"})

_SESSION_QUOTA_TRIGGERS = frozenset(
    {
        "session_cost_limit",
        "session_call_count_limit",
        # Bounds provider attempts WITHIN one logical call (retry/failover),
        # independent of the item-14c call-count backstop above, which bounds
        # logical calls across a whole session. Session-scoped, not hard: a
        # transient provider fault should not need an operator reset (Defect 5,
        # 2026-08-31 -- see `max_provider_attempts_per_call`).
        "provider_attempt_limit",
    }
)

# === Defect B (2026-09-22): a transient provider fault must not need a human ===
#
# These two hard triggers, and ONLY these two, describe the same thing: a
# provider request that FAILED and whose (therefore bounded, single-call)
# cost could not be proven. They are the class that has actually taken the
# desk down -- four operator resets on this pair and not one of them
# followed a budget breach [measured, llm_circuit_events on the production
# DB: 2026-08-31 x2 and 2026-09-22 `failed_call_unknown_cost`, 2026-09-16
# `legacy_unknown_cost`]. `_auto_clear_transient_latch_locked` lets them
# expire, under every guard documented there.
#
# EVERYTHING ELSE STAYS OPERATOR-ONLY, on purpose:
#   * `unknown_actual_cost` -- a call that SUCCEEDED and returned output
#     with no usage telemetry. Real tokens were generated; the unknown is
#     real spend, not a failed attempt.
#   * `non_monotonic_quota_hold_day` -- clock regression or accounting
#     corruption. An integrity fault, never transient.
#   * `daily_cost_limit` / `session_cost_limit` -- real breaches. They are
#     not hard latches at all; they expire with their own budget window and
#     nothing here touches them.
#   * the durable emergency file latch (`mark_unavailable`) -- the circuit's
#     own infrastructure failed, so nothing it computes can be trusted.
#     Checked explicitly and separately below.
#   * any unrecognized future code -- `_trigger_scope` already defaults it
#     to hard, and it is not in this set, so it stays hard.
#
# `legacy_unknown_cost` is in the set but is NOT unconditionally
# self-clearing: it also fires for pre-deployment log rows and for a
# completed call with no telemetry. The guard that separates them is the
# day's `failed_call_unknown_rows`, which must account for EVERY unknown row
# on the day before anything is forgiven.
#
# THE ALTERNATIVE THAT WAS REJECTED (adversary, 2026-09-23). Rather than
# expire the latch, compute a conservative worst-case dollar bound for the
# failed call from `src/cost_table.py` and each seat's `max_tokens`, book it
# as settled spend, and let `daily_cost_limit` do the rest -- one mechanism
# instead of two, and no cooldown or allowance to justify. It is a real
# argument and it is what this module used to do. Item 14 (OWNER-APPROVED
# 2026-09-02, see `LLMCostCircuitConfig`) deleted exactly that machinery,
# measuring it holding ~2.6x what was ever spent and stopping the desk on
# money that was never spent. Re-deriving a worst-case charge here is the
# reservation layer under a new name, and it books a number nobody paid --
# which is the failure mode the desk has ruled out twice over. The latch
# still refuses to clear while settled spend is at cap, so the real ceiling
# is unchanged; what is bounded instead of priced is the COUNT of unproven
# failed calls a day may forgive.
_SELF_CLEARING_HARD_TRIGGERS = frozenset(
    {
        "failed_call_unknown_cost",
        # Same latch, named honestly when the cause is known (see
        # OUT_OF_CREDIT_TRIGGER_CODE): a payment refusal is still a failed call
        # of unproven cost, so it self-clears on exactly the same terms. Renaming
        # the cause must not quietly turn a self-clearing latch into a permanent
        # one.
        "provider_out_of_credit",
        "legacy_unknown_cost",
    }
)

# === docs/WORK.md item 147 (2026-09-26): a NULL cost that is not unknown ===
#
# `_seed_day_locked` counts every `agent_logs` row with `cost_usd IS NULL`
# as an unknown-cost row, seeds the ET day inexact, and so arms
# `legacy_unknown_cost` -- an operator-only hard latch that refuses all paid
# analysis for the rest of the day. That is right for a row whose provider
# request happened and could not be priced. It is wrong for a row where NO
# PROVIDER REQUEST WAS EVER ISSUED.
#
# MEASURED, production DB read-only 2026-09-26: `agent_logs` holds 667 rows
# over 2026-08-14..2026-09-26, of which exactly 7 have `cost_usd IS NULL`.
# All 7 are the same thing -- `smart_money_analyst` synthesis-cache hits
# (`provider_requests=0`, `latency_s=0.0`, `input_message='[cached evidence
# hash]'`, 0 input and 0 output tokens, `status='success'`), six on
# 2026-08-31 and one on 2026-09-17. Not one of them cost anything, because
# not one of them called a provider. Zero rows have tokens > 0 with a NULL
# cost, so the unpriceable-model case has never occurred in production.
#
# The proof of $0 is the row's own recorded provider-request count, in the
# same spirit as `_KNOWN_ZERO_COST_STATUS_CODES` below: forgive only what
# is provably free, never what is merely probably free. All three
# conditions are required, and the third is not redundant --
# `src/pipeline.py`'s evening exception path also synthesises an
# `AgentResult` with `provider_requests=0` and no cost after a call that
# may well have reached the provider, and that row must stay unknown. It is
# excluded because its status is not `success`.
#
# `provider_requests` is NULL on 194 pre-column legacy rows. SQL's
# `= 0` never matches NULL, so those keep counting as unknown, which is the
# safe direction: their request count was never recorded, so nothing about
# them is proven.
_PROVEN_ZERO_ROW_SQL = "cost_usd IS NULL AND provider_requests = 0 AND status = 'success'"


def _unknown_cost_row_expr(conn: sqlite3.Connection) -> str:
    """SQL scoring 1 for an `agent_logs` row of genuinely unknown cost.

    Falls back to the plain `cost_usd IS NULL` test when the columns that
    carry the proof are absent, because a standalone breaker DB may hold an
    older `agent_logs` shape (the caller already tolerates the table being
    missing outright). Without the proof there is no proof, so the row
    counts as unknown -- fail closed.
    """

    try:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(agent_logs)")}
    except sqlite3.OperationalError:
        columns = set()
    if not {"provider_requests", "status"} <= columns:
        return "CASE WHEN cost_usd IS NULL THEN 1 ELSE 0 END"
    # Item 203: a provider request that HAPPENED and came back with no token
    # counts at all is recorded by `src/agents/base.py:usage_telemetry_word`
    # as `telemetry='no_usage'`. Such a row's `cost_usd` may be a literal 0.0
    # rather than NULL -- written because nothing was reported, not because
    # anything was measured -- and the plain `cost_usd IS NULL` test would
    # bank it as a proven free call. It is not proven free and it is not
    # priced: this desk refuses to substitute a list rate, an average or any
    # other invented price for a number the provider did not return, so the
    # row is counted as UNKNOWN and the day loses `costs_exact`. Disjoint from
    # `_PROVEN_ZERO_ROW_SQL` by construction (that needs provider_requests=0,
    # this word is only written when a request was made), and the clause is
    # dropped entirely on an older `agent_logs` that has no `telemetry`
    # column, where the absence of the proof leaves only the NULL test.
    no_usage_sql = "WHEN telemetry = 'no_usage' THEN 1 " if "telemetry" in columns else ""
    return f"CASE WHEN ({_PROVEN_ZERO_ROW_SQL}) THEN 0 {no_usage_sql}WHEN cost_usd IS NULL THEN 1 ELSE 0 END"


def _trigger_scope(code: Any) -> str:
    if not isinstance(code, str):
        return "hard"
    if code in _DAY_QUOTA_TRIGGERS:
        return "day"
    if code in _SESSION_QUOTA_TRIGGERS:
        return "session"
    return "hard"


# === Defect 2 (2026-08-28): provider failures that provably cost $0 ===
#
# `fail_call` charges the full conservative reservation for every failed
# attempt, on the reasoning that a stream can be cut off after the provider
# already generated (and billed for) tokens. That reasoning is right for a
# response that started and wrong for a request the provider rejected
# before generating anything, or one that never reached the provider at
# all. On 2026-08-28 a single tech_analyst HTTP 429 -- by definition zero
# tokens billed, since a 429 means the provider refused the request before
# any generation started -- was accounted as `unknown_cost_rows`, made the
# day inexact, and hard-latched trading for three-plus hours until an
# operator reset it by hand.
#
# `_is_known_zero_cost_failure` below is the ONLY thing trusted to prove
# $0 cost, and it is deliberately narrow. This module does not import the
# anthropic/openai/httpx SDKs (see the module docstring -- the breaker has
# to stay independent of every provider client), so, exactly like
# `src/agents/base.py:_is_retryable`, classification is done by matching
# `status_code` / exception class name rather than `isinstance` against a
# provider SDK class.

# The provider explicitly rejected the request before generating a
# response. 429 (rate limited), 400 (bad request), 401 (bad/expired key),
# 403 (forbidden) and 404 (not found/deprecated model) all happen at the
# provider's request-validation boundary, strictly before any output (and,
# per every provider's own billing model, any input) tokens are metered.
# A 402 ("insufficient balance" -- handled elsewhere in base.py as a
# fast-fail-to-failover signal, not here) is deliberately NOT included:
# it is not in the design's enumerated zero-cost list, and adding an
# entry for it on our own initiative would be exactly the "allow-list of
# ambiguous errors" inversion this fix must not become.
_KNOWN_ZERO_COST_STATUS_CODES = frozenset({400, 401, 403, 404, 429})

# === Defect A (2026-09-22): 503 UNAVAILABLE ===
#
# A 503 is NOT a validation rejection, so it does not belong in the set
# above, and it is the one status whose zero-cost-ness depends on WHEN it
# arrived rather than on the number alone. Two genuinely different things
# carry it:
#
#   (a) BEFORE generation. The provider refuses the request for capacity:
#       Google AI Studio's "This model is currently experiencing high
#       demand. Spikes in demand are usually temporary." The SDK raises an
#       HTTP status error off the response headers, no body was ever
#       generated, and nothing is metered -- the same billing position as
#       a 429, which is already allow-listed. On 2026-09-22 that exact
#       error arrived 17 times [measured: production log], the last
#       tech_analyst attempt failed on primary and failover, and because
#       503 was missing here the call was booked ambiguous and hard-latched
#       the desk for over nine hours on $0.7883 of a $2.75 day.
#
#   (b) MID-STREAM. OpenRouter cannot change an HTTP status once the first
#       byte is out, so it reports an upstream failure as an in-band SSE
#       error chunk whose `code` is the status the response WOULD have
#       carried (see `LLMStreamErrorChunk` in src/agents/base.py). Tokens
#       may already have been generated and billed before that chunk. That
#       is ambiguous, and it must stay ambiguous.
#
# The two are distinguishable, and only by the exception's class -- not by
# the status code, which is 503 either way. So 503 is allow-listed subject
# to a mid-stream veto rather than unconditionally.
#
# Left deliberately unchanged: a MID-STREAM 429 is still treated as free.
# That is not an oversight here, it is the ratified 2026-08-31 fix that
# `LLMStreamErrorChunk` exists to deliver, and re-litigating it is not this
# change's job. The veto is applied only to the status this change adds.
_PRE_GENERATION_CAPACITY_STATUS_CODES = frozenset({503})

# Exception class names that mean "this status was reported from inside an
# already-started response body". Matched by NAME, not `isinstance`: this
# module must not import src.agents.base (see the module docstring -- the
# breaker stays independent of every provider client and of the agent
# layer), exactly as `_PRE_SEND_TRANSPORT_EXC_NAMES` below does.
_MID_STREAM_EXC_NAMES = frozenset({"LLMStreamErrorChunk"})

# Exception class names that can ONLY occur while establishing a TCP/TLS
# connection -- i.e. strictly before a single byte of the request could
# have been written to the socket: DNS resolution failure, a refused/reset
# connection, and a failed (or timed-out) TLS handshake. Deliberately
# narrow, and deliberately excludes read/write/protocol-level failures
# (`ReadTimeout`, `ReadError`, `WriteError`, `RemoteProtocolError`,
# `APITimeoutError`, ...), which can only happen AFTER the request was
# already sent and are left ambiguous below -- that is the same pre-send/
# post-send line the design draws between "timeout after send" (ambiguous)
# and "pre-send transport failure" (zero-cost).
_PRE_SEND_TRANSPORT_EXC_NAMES = frozenset(
    {
        "ConnectError",
        "ConnectTimeout",
        "ConnectionRefusedError",
        "gaierror",
        "SSLError",
        "SSLCertVerificationError",
        "SSLZeroReturnError",
        "SSLWantReadError",
        "SSLWantWriteError",
    }
)


def _cause_chain(error: BaseException, limit: int = 6) -> list[BaseException]:
    """`error` plus its wrapped causes, de-duplicated and length-bounded.

    SDKs wrap the concrete failure (`raise APIConnectionError(...) from exc`)
    rather than replacing it, so the specific original is usually still
    reachable even when the outermost class name is uninformative.
    """
    seen: set[int] = set()
    chain: list[BaseException] = []
    node: BaseException | None = error
    for _ in range(limit):  # generous bound against a pathological chain
        if node is None or id(node) in seen:
            break
        seen.add(id(node))
        chain.append(node)
        node = node.__cause__ or node.__context__
    return chain


def _is_mid_stream_failure(error: BaseException) -> bool:
    """True when this failure was reported from inside a started response.

    Fail closed in the ambiguous direction: the whole cause chain is
    checked, so a mid-stream error merely WRAPPED by something else still
    vetoes the zero-cost claim. `__context__` can attach an unrelated
    earlier exception, which can only ever make this return True when it
    might have returned False -- i.e. charge a call that may have been
    free. That is the conservative side, and it is the side this module
    errs on everywhere else too.
    """
    return any(type(node).__name__ in _MID_STREAM_EXC_NAMES for node in _cause_chain(error))


def _is_known_zero_cost_failure(error: BaseException) -> bool:
    """True only for a provider failure PROVEN to have cost $0.

    Fail closed: anything not explicitly matched here -- an unrecognized
    exception, a 5xx other than a pre-generation 503, a 503 reported from
    inside a started stream, a timeout waiting for a response, a truncated
    stream, a missing/unexpected status code -- returns False (ambiguous,
    today's conservative accounting, unchanged by this function). This is
    an allow-list of what is safe to call zero-cost; it must never grow
    into (or be replaced by) an allow-list of what counts as ambiguous.
    """
    status = getattr(error, "status_code", None)
    if isinstance(status, int) and not isinstance(status, bool):
        if status in _KNOWN_ZERO_COST_STATUS_CODES:
            return True
        if status in _PRE_GENERATION_CAPACITY_STATUS_CODES:
            # Free only when the provider refused BEFORE generating. The
            # same number reported from inside a started stream may already
            # have billed tokens -- see the note on
            # `_PRE_GENERATION_CAPACITY_STATUS_CODES`.
            return not _is_mid_stream_failure(error)
        return False
    # No HTTP status code was ever received: this is either a genuine
    # pre-send transport failure or something ambiguous (a read/write
    # timeout, a connection dropped mid-response, ...). Walk the
    # exception's cause chain -- the top-level wrapper's own class name
    # ("APIConnectionError") is, by itself, ambiguous about which side of
    # the connection failed.
    return any(type(node).__name__ in _PRE_SEND_TRANSPORT_EXC_NAMES for node in _cause_chain(error))


def _all_attempts_provably_free(
    error: BaseException,
    attempt_errors: list[BaseException] | None,
) -> bool:
    """True only when EVERY provider attempt on this call provably cost $0.

    Callers that cannot enumerate their attempts pass nothing and get the
    original single-exception behaviour, so this can only ever recognise more
    genuinely-free failures — never fewer.

    Ambiguity is contagious on purpose: one attempt that might have been
    billed makes the whole reservation chargeable, because the reservation
    covers the whole call and there is no per-attempt figure to fall back on.
    """
    if not attempt_errors:
        return _is_known_zero_cost_failure(error)
    free = all(_is_known_zero_cost_failure(exc) for exc in attempt_errors)
    if not free:
        # Which attempt made this chargeable, and what shape was it? A
        # provider that rejects with an unclassified error costs the desk its
        # conservative reserve every time, and there is no way to know that is
        # happening without printing the shape. Cheap, and it turns the next
        # occurrence into a measurement instead of another inference.
        shapes = ", ".join(
            f"{type(exc).__name__}"
            f"(status={getattr(exc, 'status_code', None)!r})"
            f"{'' if _is_known_zero_cost_failure(exc) else ' <-CHARGED'}"
            for exc in attempt_errors
        )
        logger.warning(
            "cost-circuit charging a failed call: not every attempt is "
            "provably $0 — [%s]. An attempt marked CHARGED with a status the "
            "zero-cost allow-list does not carry is worth investigating: it "
            "may have billed nothing in reality.",
            shapes,
        )
    return free
