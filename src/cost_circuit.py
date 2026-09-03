"""Persistent, cross-process circuit breaker for paid LLM analysis.

The breaker is deliberately independent from the trading decision path.  It
can suspend model requests, but it cannot stop broker reconciliation,
broker-resident protective orders, deterministic loss checks, P&L capture,
or the read-only API.

Every provider request is authorized in SQLite under ``BEGIN IMMEDIATE``.
That matters because the morning and intraday systemd jobs are separate
processes and can otherwise both observe the same remaining budget and spend
it.

=== docs/WORK.md item 14 (OWNER-APPROVED 2026-08-31, replaced 2026-09-02) ===

This module used to reserve a conservative, worst-case dollar estimate for
every call BEFORE it was made, and replace that estimate with the real cost
once the provider responded. That was deleted. Real calls settle at a
median 0.38x of the pinned worst-case rate, so the reservation held ~2.6x
what was ever really spent and stopped the desk on money that was never
spent -- three times in one hour on 2026-09-02, against ~$1/day of actual
spend on a $2.75 ceiling. The guard was causing more outages than it
prevented losses.

The replacement is exactly three things, per the owner's decision, and
nothing else:

  (a) A spend cap on the OpenRouter API key itself, OUTSIDE this codebase,
      so no bug here can defeat it. NOT IMPLEMENTED HERE -- it is not code.
      See docs/WORK.md item 14(a) for the exact provider-side limit to
      configure; do not let this fall through the cracks just because it
      has no corresponding diff.
  (b) Stop when REAL SETTLED cost actually spent today (or this session)
      hits its cap. No pre-call estimate, so nothing to be wrong about --
      `complete_call`/`fail_call` record the provider's ACTUAL returned
      cost, and `_enforce_settled_limits_locked` checks that recorded total
      against the cap, both before a call starts and immediately after one
      settles.
  (c) Stop when one session exceeds `max_calls_per_session` calls -- a
      count-based runaway-loop guard, independent of price, because a
      loop is defined by call COUNT and counting cannot be wrong about a
      rate. See `begin_call`.

The maximum overshoot of (b) is one call's real cost -- under a dollar,
measured. That is worth it against a desk switched off for a day.
"""

from __future__ import annotations

import logging
import json
import os
import fcntl
import random
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import date, datetime, time as dt_time, timezone
from pathlib import Path
from typing import Any, Callable, TypeVar
from zoneinfo import ZoneInfo

from src.cost_table import PRICING

logger = logging.getLogger(__name__)

_T = TypeVar("_T")

_ET = ZoneInfo("America/New_York")

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
_SESSION_QUOTA_TRIGGERS = frozenset({
    "session_cost_limit",
    "session_call_count_limit",
    # Bounds provider attempts WITHIN one logical call (retry/failover),
    # independent of the item-14c call-count backstop above, which bounds
    # logical calls across a whole session. Session-scoped, not hard: a
    # transient provider fault should not need an operator reset (Defect 5,
    # 2026-08-31 -- see `max_provider_attempts_per_call`).
    "provider_attempt_limit",
})


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
_PRE_SEND_TRANSPORT_EXC_NAMES = frozenset({
    "ConnectError", "ConnectTimeout", "ConnectionRefusedError",
    "gaierror", "SSLError", "SSLCertVerificationError",
    "SSLZeroReturnError", "SSLWantReadError", "SSLWantWriteError",
})


def _is_known_zero_cost_failure(error: BaseException) -> bool:
    """True only for a provider failure PROVEN to have cost $0.

    Fail closed: anything not explicitly matched here -- an unrecognized
    exception, a 5xx, a timeout waiting for a response, a truncated
    stream, a missing/unexpected status code -- returns False (ambiguous,
    today's conservative accounting, unchanged by this function). This is
    an allow-list of what is safe to call zero-cost; it must never grow
    into (or be replaced by) an allow-list of what counts as ambiguous.
    """
    status = getattr(error, "status_code", None)
    if isinstance(status, int) and not isinstance(status, bool):
        return status in _KNOWN_ZERO_COST_STATUS_CODES
    # No HTTP status code was ever received: this is either a genuine
    # pre-send transport failure or something ambiguous (a read/write
    # timeout, a connection dropped mid-response, ...). Walk the
    # exception's cause chain -- SDKs wrap the concrete httpx/socket/ssl
    # exception (`raise APIConnectionError(...) from exc`) rather than
    # replacing it, so the original, specific failure is almost always
    # still reachable even though the top-level wrapper's own class name
    # ("APIConnectionError") is, by itself, ambiguous about which side of
    # the connection failed.
    seen: set[int] = set()
    node: BaseException | None = error
    for _ in range(6):  # generous bound against a pathological chain; never expected to matter
        if node is None or id(node) in seen:
            break
        seen.add(id(node))
        if type(node).__name__ in _PRE_SEND_TRANSPORT_EXC_NAMES:
            return True
        node = node.__cause__ or node.__context__
    return False


def _all_attempts_provably_free(
    error: BaseException, attempt_errors: list[BaseException] | None,
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
            "may have billed nothing in reality.", shapes,
        )
    return free


class PaidAnalysisSuspended(RuntimeError):
    """Raised before a paid provider request when the circuit is open."""

    def __init__(self, trigger: str, state: dict[str, Any] | None = None):
        self.trigger = trigger
        self.state = state or {}
        super().__init__(f"paid analysis suspended: {trigger}")


class OptionalPaidAnalysisRetrySkipped(RuntimeError):
    """An optional repair was not reserved because its retry budget was spent.

    This is not a circuit failure: no provider request was attempted and the
    caller may safely retain already-completed primary analysis. All mandatory
    limits and every non-retry trigger continue to use ``PaidAnalysisSuspended``.
    """

    def __init__(self, trigger: str, state: dict[str, Any] | None = None):
        self.trigger = trigger
        self.state = state or {}
        super().__init__(f"optional paid-analysis retry skipped: {trigger}")


@dataclass
class CallReservation:
    """A logical-call handle. NOT a dollar reservation (item 14, 2026-09-02):
    it carries the call's identity (run/mode/agent/model) through the retry
    loop so `before_provider_attempt`/`complete_call`/`fail_call` can find
    the right session row, and `attempt_count` tracks provider attempts
    WITHIN this one logical call purely in-process (this object's lifetime
    is exactly one `BaseAgent._execute` call, all on one process/thread) so
    `max_provider_attempts_per_call` still bounds a retry/failover storm.
    """

    reservation_id: str
    run_id: str
    mode: str
    agent_name: str
    model: str
    attempt_count: int = 0


def ensure_cost_circuit_schema(conn: sqlite3.Connection) -> None:
    """Create the additive breaker schema on an existing SQLite connection."""

    expected_breaker_tables = {
        "llm_budget_days", "llm_budget_sessions",
        "llm_circuit_state", "llm_circuit_events",
    }
    existing_breaker_tables = {
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        if str(row[0]) in expected_breaker_tables
    }
    quota_table_exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='llm_quota_holds'"
    ).fetchone() is not None
    if quota_table_exists and existing_breaker_tables != expected_breaker_tables:
        raise RuntimeError(
            "cost-circuit schema is partial: quota holds exist without the complete "
            "base accounting schema"
        )
    if existing_breaker_tables and existing_breaker_tables != expected_breaker_tables:
        missing = sorted(expected_breaker_tables - existing_breaker_tables)
        raise RuntimeError(
            "cost-circuit schema is partial; missing table(s): " + ", ".join(missing)
        )
    state_table_existed = "llm_circuit_state" in existing_breaker_tables
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS llm_budget_days (
            day TEXT PRIMARY KEY,
            baseline_cost_usd REAL NOT NULL DEFAULT 0,
            incremental_cost_usd REAL NOT NULL DEFAULT 0,
            unknown_cost_rows INTEGER NOT NULL DEFAULT 0,
            costs_exact INTEGER NOT NULL DEFAULT 1,
            seeded_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS llm_budget_sessions (
            run_id TEXT PRIMARY KEY,
            day TEXT NOT NULL,
            mode TEXT NOT NULL,
            actual_cost_usd REAL NOT NULL DEFAULT 0,
            logical_calls INTEGER NOT NULL DEFAULT 0,
            provider_attempts INTEGER NOT NULL DEFAULT 0,
            retry_attempts INTEGER NOT NULL DEFAULT 0,
            costs_exact INTEGER NOT NULL DEFAULT 1,
            status TEXT NOT NULL DEFAULT 'active',
            started_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_llm_budget_sessions_day_mode
            ON llm_budget_sessions(day, mode);

        CREATE TABLE IF NOT EXISTS llm_circuit_state (
            singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
            suspended INTEGER NOT NULL DEFAULT 0,
            trigger_code TEXT,
            trigger_detail TEXT,
            run_id TEXT,
            mode TEXT,
            agent_name TEXT,
            session_attempts INTEGER NOT NULL DEFAULT 0,
            attempts_exact INTEGER NOT NULL DEFAULT 1,
            costs_exact INTEGER NOT NULL DEFAULT 1,
            session_cost_usd REAL NOT NULL DEFAULT 0,
            daily_cost_usd REAL NOT NULL DEFAULT 0,
            session_limit_usd REAL,
            daily_limit_usd REAL,
            suspended_at TEXT,
            alert_state INTEGER NOT NULL DEFAULT 0,
            reset_at TEXT,
            reset_reason TEXT,
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS llm_circuit_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_type TEXT NOT NULL,
            trigger_code TEXT,
            detail TEXT,
            run_id TEXT,
            mode TEXT,
            agent_name TEXT,
            attempts INTEGER,
            session_cost_usd REAL,
            daily_cost_usd REAL,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS llm_quota_holds (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            scope TEXT NOT NULL CHECK (scope IN ('day', 'mode_day', 'session')),
            scope_key TEXT NOT NULL,
            day TEXT NOT NULL,
            trigger_code TEXT NOT NULL,
            trigger_detail TEXT NOT NULL,
            run_id TEXT NOT NULL,
            mode TEXT NOT NULL,
            agent_name TEXT NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0,
            attempts_exact INTEGER NOT NULL DEFAULT 1,
            costs_exact INTEGER NOT NULL DEFAULT 1,
            session_cost_usd REAL NOT NULL DEFAULT 0,
            daily_cost_usd REAL NOT NULL DEFAULT 0,
            session_limit_usd REAL,
            daily_limit_usd REAL,
            active INTEGER NOT NULL DEFAULT 1,
            alert_state INTEGER NOT NULL DEFAULT 0,
            alert_updated_at TEXT NOT NULL DEFAULT (datetime('now')),
            recovery_alert_state INTEGER NOT NULL DEFAULT 0,
            recovery_alert_updated_at TEXT NOT NULL DEFAULT (datetime('now')),
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            released_at TEXT,
            release_reason TEXT
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_llm_quota_holds_active_scope
            ON llm_quota_holds(scope, scope_key, day) WHERE active=1;
        CREATE INDEX IF NOT EXISTS idx_llm_quota_holds_active_day
            ON llm_quota_holds(day, active);
        """
    )
    if not existing_breaker_tables:
        conn.execute("INSERT INTO llm_circuit_state(singleton) VALUES (1)")
    elif not state_table_existed:
        raise RuntimeError(
            "cost-circuit schema is partial: state table is missing while other "
            "budget tables already exist"
        )
    elif conn.execute(
        "SELECT COUNT(*) FROM llm_circuit_state WHERE singleton=1"
    ).fetchone()[0] != 1:
        # Recreating an absent state row as open would erase a durable latch.
        # Treat loss/corruption of the singleton as infrastructure failure.
        raise RuntimeError("cost-circuit singleton state row is missing or corrupt")
    # Additive migration for databases initialized by an earlier breaker
    # build during rollout.
    state_columns = {
        row[1] for row in conn.execute("PRAGMA table_info(llm_circuit_state)")
    }
    if "attempts_exact" not in state_columns:
        conn.execute(
            "ALTER TABLE llm_circuit_state ADD COLUMN attempts_exact INTEGER "
            "NOT NULL DEFAULT 1"
        )
    if "costs_exact" not in state_columns:
        conn.execute(
            "ALTER TABLE llm_circuit_state ADD COLUMN costs_exact INTEGER "
            "NOT NULL DEFAULT 1"
        )
    day_columns = {
        row[1] for row in conn.execute("PRAGMA table_info(llm_budget_days)")
    }
    if "unknown_cost_rows" not in day_columns:
        conn.execute(
            "ALTER TABLE llm_budget_days ADD COLUMN unknown_cost_rows INTEGER "
            "NOT NULL DEFAULT 0"
        )
    if "costs_exact" not in day_columns:
        conn.execute(
            "ALTER TABLE llm_budget_days ADD COLUMN costs_exact INTEGER "
            "NOT NULL DEFAULT 1"
        )
    session_columns = {
        row[1] for row in conn.execute("PRAGMA table_info(llm_budget_sessions)")
    }
    if "costs_exact" not in session_columns:
        conn.execute(
            "ALTER TABLE llm_budget_sessions ADD COLUMN costs_exact INTEGER "
            "NOT NULL DEFAULT 1"
        )

    # Migrate a latch created by the original one-state implementation.  Known
    # quota triggers retain their full audit snapshot but cease to masquerade
    # as hard infrastructure incidents.  Unknown codes deliberately remain
    # hard/operator-reset-only.
    state = conn.execute(
        "SELECT * FROM llm_circuit_state WHERE singleton=1"
    ).fetchone()
    if state is not None and int(state["suspended"] or 0):
        code = str(state["trigger_code"] or "")
        scope = _trigger_scope(code)
        if scope != "hard":
            run_id = str(state["run_id"] or "unscoped")
            mode = str(state["mode"] or "unknown")
            session_day_row = conn.execute(
                "SELECT day FROM llm_budget_sessions WHERE run_id=?", (run_id,)
            ).fetchone()
            if session_day_row is None:
                # Scope/date provenance is part of the safety decision.  A
                # partially migrated incident must remain hard instead of
                # guessing that an old quota belongs to today's budget.
                raise RuntimeError(
                    f"legacy quota latch {code!r} has no session/day provenance "
                    f"for run {run_id}"
                )
            hold_day = str(session_day_row["day"])
            scope_key = (
                hold_day if scope == "day" else
                f"{hold_day}:{mode}" if scope == "mode_day" else run_id
            )
            conn.execute(
                "INSERT OR IGNORE INTO llm_quota_holds "
                "(scope, scope_key, day, trigger_code, trigger_detail, run_id, mode, "
                "agent_name, attempts, attempts_exact, costs_exact, session_cost_usd, "
                "daily_cost_usd, session_limit_usd, daily_limit_usd, alert_state) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    scope, scope_key, hold_day, code,
                    str(state["trigger_detail"] or code), run_id, mode,
                    str(state["agent_name"] or "unknown"),
                    int(state["session_attempts"] or 0),
                    int(state["attempts_exact"] or 0),
                    int(state["costs_exact"] or 0),
                    float(state["session_cost_usd"] or 0),
                    float(state["daily_cost_usd"] or 0),
                    state["session_limit_usd"], state["daily_limit_usd"],
                    int(state["alert_state"] or 0),
                ),
            )
            conn.execute(
                "UPDATE llm_circuit_state SET suspended=0, trigger_code=NULL, "
                "trigger_detail=NULL, run_id=NULL, mode=NULL, agent_name=NULL, "
                "session_attempts=0, session_cost_usd=0, daily_cost_usd=0, "
                "attempts_exact=1, costs_exact=1, suspended_at=NULL, alert_state=0, "
                "updated_at=datetime('now') WHERE singleton=1"
            )
    # Schema creation and legacy-latch migration are one self-contained
    # initialization unit.  Callers intentionally start their operational
    # BEGIN IMMEDIATE transaction only after this durable migration commits.
    conn.commit()


def _et_day_and_utc_bounds(now: datetime | None = None) -> tuple[str, str, str]:
    now = now or datetime.now(timezone.utc)
    local = now.astimezone(_ET)
    day = local.date()
    start = datetime.combine(day, dt_time.min, tzinfo=_ET).astimezone(timezone.utc)
    end = datetime.combine(day, dt_time.max, tzinfo=_ET).astimezone(timezone.utc)
    # SQLite timestamps in this project use UTC without an offset.
    return (
        day.isoformat(),
        start.strftime("%Y-%m-%d %H:%M:%S"),
        end.strftime("%Y-%m-%d %H:%M:%S"),
    )


def _legacy_mode(run_id: str) -> str:
    if run_id.startswith("intra_check-"):
        return "intra_check"
    if run_id.startswith("midday-"):
        return "midday"
    if run_id.startswith("close-"):
        return "close"
    if run_id.startswith("evening-"):
        return "evening"
    if run_id.startswith("earnings-"):
        return "earnings_preprocess"
    if run_id.startswith("meta-"):
        return "meta"
    return "morning"


@contextmanager
def _file_lock(lock_path: Path | None):
    """Serialize concurrent writers to one filesystem sidecar.

    Shared by `LLMCostCircuitBreaker`'s own emergency-latch writer/reset and
    `UnavailableLLMCostCircuit`'s alert-outcome bookkeeping below -- the same
    lock file, so a latch write/reset can never race an alert-outcome update.
    """

    if lock_path is None:
        yield
        return
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _read_alert_outcome(latch_path: Path | None) -> tuple[bool, int]:
    """Best-effort read of (alert_delivered, alert_attempts) from the latch.

    Never raises: an unreadable/missing/corrupt marker reads as "not yet
    delivered, zero attempts" rather than blocking anything.
    """

    if latch_path is None or not latch_path.exists():
        return False, 0
    try:
        payload = json.loads(latch_path.read_text(encoding="utf-8"))
    except Exception:
        return False, 0
    if not isinstance(payload, dict):
        return False, 0
    delivered = payload.get("alert_delivered") is True
    try:
        attempts = int(payload.get("alert_attempts") or 0)
    except (TypeError, ValueError):
        attempts = 0
    return delivered, max(0, attempts)


def _record_alert_attempt(
    latch_path: Path | None,
    lock_path: Path | None,
    *,
    delivered: bool,
    now: datetime | None = None,
) -> int:
    """Durably fold one alert-send outcome into the emergency latch file.

    docs/WORK.md item 17(b): a Telegram send failure for the "paid analysis
    suspended" alert used to be tracked only in the in-process sentinel
    (`UnavailableLLMCostCircuit._alert_delivered`) -- if the process exited
    (or was never touched again) before a retry succeeded, the failure was
    both terminal and invisible: no record survived to let a later run
    retry it or to let anything else notice. This writes the outcome into
    the SAME durable JSON marker `mark_unavailable` already writes for the
    latch itself (the one filesystem fact guaranteed to exist and to be
    independent of whatever database fault caused the latch in the first
    place), under the shared `_file_lock`, so:
      * a later process re-reading this file (`_read_alert_outcome`) knows
        immediately whether the operator was ever actually told, and keeps
        retrying until `alert_delivered` is true;
      * `alert_attempts` is a durable, cross-restart count exposed through
        `UnavailableLLMCostCircuit._state()` / `status()` -- a visible
        surface (Mission Control / session summaries already read
        `status()`) that keeps climbing for as long as delivery keeps
        failing, rather than the failure being silently dropped after one
        in-memory attempt.

    Never raises and never invents a marker: if the latch file does not
    exist yet (e.g. this is running against a ":memory:" breaker with no
    filesystem sidecar), this is a no-op -- there is nothing durable to
    update, matching how `mark_unavailable` itself degrades for that case.
    Returns the resulting attempt count on success, or -1 if the update
    could not be made durable (caller logs that distinctly).
    """

    if latch_path is None:
        return -1
    with _file_lock(lock_path):
        try:
            if not latch_path.exists():
                return -1
            payload = json.loads(latch_path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                return -1
            attempts = int(payload.get("alert_attempts") or 0) + 1
            payload["alert_attempts"] = attempts
            # Once delivered, stays delivered -- a later failed retry of a
            # SECOND, unrelated alert attempt (there should not be one, but
            # never regress a true fact back to false).
            payload["alert_delivered"] = bool(payload.get("alert_delivered")) or bool(
                delivered
            )
            payload["last_alert_attempt_at"] = (
                now or datetime.now(timezone.utc)
            ).isoformat()
            tmp = latch_path.with_name(
                f".{latch_path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
            )
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    handle.write(json.dumps(payload, sort_keys=True) + "\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(tmp, latch_path)
            finally:
                try:
                    tmp.unlink(missing_ok=True)
                except OSError:
                    pass
            return attempts
        except Exception:
            logger.exception(
                "Could not durably record cost-circuit alert-delivery outcome "
                "at %s", latch_path,
            )
            return -1


class UnavailableLLMCostCircuit:
    """Fail-closed sentinel when persistent breaker infrastructure is broken.

    It deliberately never raises during session activation, so broker and
    deterministic safety work can proceed.  Every paid boundary raises.
    """

    def __init__(
        self,
        error: BaseException,
        notifier: Any | None = None,
        *,
        run_id: str = "unscoped",
        mode: str = "unknown",
        agent_name: str = "circuit_infrastructure",
        attempts: int | None = None,
        attempts_exact: bool = False,
        session_cost_usd: float | None = None,
        daily_cost_usd: float | None = None,
        costs_exact: bool = False,
        emergency_latch_path: Path | None = None,
        emergency_lock_path: Path | None = None,
    ):
        self.error = error
        if notifier is None:
            from src.notifier import TelegramNotifier
            notifier = TelegramNotifier()
        self.notifier = notifier
        self.agent_name = agent_name
        self.trigger_run_id = run_id
        self.trigger_mode = mode
        self.attempts = attempts
        self.attempts_exact = attempts_exact
        self.session_cost_usd = session_cost_usd
        self.daily_cost_usd = daily_cost_usd
        self.costs_exact = costs_exact
        # item 17(b): the filesystem marker `mark_unavailable` already wrote
        # for THIS incident -- the one durable fact this sentinel can lean
        # on to remember whether the operator has actually been told, across
        # both same-process retries and a fresh process after a restart.
        # None (e.g. a ":memory:" breaker with no filesystem sidecar, or one
        # of the defensive fallback sentinels pipeline.py builds without a
        # breaker instance) degrades to the old in-memory-only behaviour.
        self._emergency_latch_path = emergency_latch_path
        self._emergency_lock_path = emergency_lock_path
        self._context_value: ContextVar[tuple[str, str]] = ContextVar(
            f"qamc_unavailable_cost_session_{id(self)}",
            default=(run_id, mode),
        )
        self._alert_lock = threading.Lock()
        durably_delivered, durable_attempts = _read_alert_outcome(
            self._emergency_latch_path
        )
        self._alert_delivered = durably_delivered
        self._alert_attempts = durable_attempts
        self._last_alert_attempt = 0.0

    def _trigger(self) -> str:
        return (
            "mandatory paid-analysis circuit is unavailable: "
            f"{type(self.error).__name__}: {str(self.error)[:300]}"
        )

    def _state(self) -> dict[str, Any]:
        current_run_id, current_mode = self._context_value.get()
        return {
            "enabled": True,
            "available": False,
            "suspended": True,
            "trigger_code": "circuit_infrastructure_unavailable",
            "trigger_detail": self._trigger(),
            "run_id": self.trigger_run_id,
            "mode": self.trigger_mode,
            "current_run_id": current_run_id,
            "current_mode": current_mode,
            "agent_name": self.agent_name,
            "session_attempts": self.attempts,
            "attempts_exact": self.attempts_exact,
            "costs_exact": self.costs_exact,
            "costs_available": (
                self.session_cost_usd is not None or self.daily_cost_usd is not None
            ),
            "session_cost_usd": self.session_cost_usd or 0.0,
            "daily_cost_usd": self.daily_cost_usd or 0.0,
            # item 17(b): visible, durable proof of whether the operator has
            # actually been told about this latch -- read by anything that
            # consumes `status()` (Mission Control, session summaries), not
            # just this process's own logs.
            "alert_delivered": self._alert_delivered,
            "alert_attempts": self._alert_attempts,
        }

    def _alert(self) -> None:
        now = time.monotonic()
        with self._alert_lock:
            if self._alert_delivered:
                return
            # Telegram/network outages are retryable, but do not hammer the
            # endpoint on every agent boundary in a parallel fan-out.
            if self._last_alert_attempt and now - self._last_alert_attempt < 120:
                return
            self._last_alert_attempt = now
        state = self._state()
        if self.attempts is None:
            attempts_line = "attempts: unavailable because a mandatory circuit prerequisite failed"
        elif self.attempts_exact:
            attempts_line = (
                f"attempts: {self.attempts} provider "
                f"attempt{'s' if self.attempts != 1 else ''}"
            )
        else:
            attempts_line = (
                f"attempts: at least {self.attempts} locally observed provider "
                f"attempt{'s' if self.attempts != 1 else ''}"
            )
        if self.session_cost_usd is None and self.daily_cost_usd is None:
            cost_line = (
                "cost: unavailable; the failed mandatory circuit prerequisite "
                "prevented a trustworthy snapshot"
            )
        else:
            session_text = (
                f"${self.session_cost_usd:.4f}" if self.session_cost_usd is not None
                else "unavailable"
            )
            daily_text = (
                f"${self.daily_cost_usd:.4f}" if self.daily_cost_usd is not None
                else "unavailable"
            )
            qualifier = "" if self.costs_exact else " (known/conservative snapshot)"
            cost_line = (
                f"cost: {session_text} this run · {daily_text} today{qualifier}"
            )
        message = (
            "🔴 QAMC PAID ANALYSIS SUSPENDED\n"
            f"trigger: {state['trigger_detail']}\n"
            f"affected run: {state['run_id']} ({state['mode']} / {self.agent_name})\n"
            f"{attempts_line}\n"
            f"{cost_line}\n"
            "suspended: all paid LLM analysis, repairs, retries, and provider failover\n"
            "preserved: broker-resident stops, order/fill reconciliation, deterministic "
            "loss protection, close/P&L jobs, and the read-only API\n"
            "operator reset is required after restoring the failed prerequisite; "
            "restart any long-lived worker that observed this emergency latch."
        )
        logger.critical("\n%s", message)
        sent = False
        try:
            sent = bool(self.notifier.send(message))
        except Exception:
            logger.exception("cost-circuit unavailable Telegram alert failed")
        # item 17(b): fold this outcome into the SAME durable marker the
        # latch itself lives in, so it survives this process exiting before
        # a retry succeeds -- see `_record_alert_attempt`'s docstring.
        durable_attempts = _record_alert_attempt(
            self._emergency_latch_path, self._emergency_lock_path, delivered=sent,
        )
        with self._alert_lock:
            if sent:
                self._alert_delivered = True
            if durable_attempts >= 0:
                self._alert_attempts = durable_attempts
            else:
                self._alert_attempts += 1
        if sent:
            return
        if durable_attempts < 0:
            logger.critical(
                "cost-circuit unavailable alert was not delivered to Telegram, "
                "AND the durable delivery-outcome record could not be updated "
                "-- this failure is tracked only in this process's memory "
                "(attempt %d) until a future call succeeds in writing it",
                self._alert_attempts,
            )
        else:
            logger.critical(
                "cost-circuit unavailable alert was not delivered to Telegram "
                "(durable attempt %d recorded at %s); will keep retrying every "
                "~120s in this process, and again from scratch on any future "
                "process/session that touches this circuit, until it succeeds "
                "or an operator intervenes",
                durable_attempts, self._emergency_latch_path,
            )

    def activate_session(self, run_id: str, mode: str) -> dict[str, Any]:
        self._context_value.set((run_id, mode))
        self._alert()
        return self._state()

    def status(self) -> dict[str, Any]:
        return self._state()

    def enforce_current_limits(self, agent_name: str = "preflight") -> dict[str, Any]:
        self._alert()
        return self._state()

    def require_paid_analysis(self, agent_name: str = "analysis") -> None:
        self._alert()
        state = self._state()
        raise PaidAnalysisSuspended(self._trigger(), state)

    def begin_call(self, **_kwargs) -> CallReservation:
        self.require_paid_analysis(str(_kwargs.get("agent_name") or "analysis"))
        raise AssertionError("unreachable")

    def before_provider_attempt(self, *_args, **_kwargs) -> int:
        self.require_paid_analysis("provider_request")
        raise AssertionError("unreachable")

    def complete_call(self, *_args, **_kwargs) -> None:
        return None

    def fail_call(self, *_args, **_kwargs) -> None:
        return None


class LLMCostCircuitBreaker:
    """Mandatory cost/retry breaker shared by every agent in one pipeline."""

    def __init__(self, db_path: str, config: Any, notifier: Any | None = None):
        self._memory_keeper: sqlite3.Connection | None = None
        self._emergency_latch_path: Path | None = None
        self._emergency_lock_path: Path | None = None
        if db_path == ":memory:":
            # Separate sqlite ``:memory:`` connections are separate databases.
            # Tests construct the pipeline this way, while the breaker needs a
            # short-lived connection per atomic transaction.  A named shared
            # memory DB plus a keeper preserves those semantics without
            # weakening production's file-backed cross-process behavior.
            self.db_path = f"file:qamc-cost-{uuid.uuid4().hex}?mode=memory&cache=shared"
            self._memory_keeper = sqlite3.connect(self.db_path, uri=True)
        else:
            self.db_path = str(Path(db_path))
            # SQLite is normally the durable source of truth.  This sidecar is
            # the independent fail-closed path for the one case where SQLite
            # itself fails while a paid request is in flight.  A new systemd
            # process must see that failure immediately instead of treating a
            # recovered DB connection as permission to spend again.
            db_file = Path(self.db_path)
            self._emergency_latch_path = db_file.with_name(
                f"{db_file.name}.llm-circuit-unavailable"
            )
            self._emergency_lock_path = db_file.with_name(
                f"{db_file.name}.llm-circuit.lock"
            )
        self.config = config
        if notifier is None:
            from src.notifier import TelegramNotifier
            notifier = TelegramNotifier()
        self.notifier = notifier
        # ContextVar keeps overlapping APScheduler job threads isolated.  The
        # morning research ThreadPool explicitly copies this context into its
        # workers (pipeline_stages.py); a mutable process-global tuple allowed
        # an intra tick to relabel an in-flight morning request.
        self._session_context: ContextVar[tuple[str, str]] = ContextVar(
            f"qamc_cost_session_{id(self)}", default=("unscoped", "unknown")
        )
        self._infrastructure_lock = threading.Lock()
        self._infrastructure_error: BaseException | None = None
        self._unavailable_sentinel: UnavailableLLMCostCircuit | None = None
        self._sync_emergency_latch()
        if self._unavailable_sentinel is not None:
            return
        if (getattr(self.config, "require_telegram_alerts", True) is True
                and getattr(self.notifier, "enabled", True) is not True):
            self.mark_unavailable(
                RuntimeError(
                    "mandatory cost-circuit Telegram alerts are not configured/enabled"
                )
            )
            return
        try:
            self._run_with_infra_retry(
                self._initialize, agent_name="circuit_startup",
            )
        except Exception as exc:
            self.mark_unavailable(exc, agent_name="circuit_startup")

    @property
    def enabled(self) -> bool:
        return bool(getattr(self.config, "enabled", True))

    @classmethod
    def fail_closed(
        cls,
        db_path: str,
        config: Any,
        error: BaseException,
        *,
        notifier: Any | None = None,
        run_id: str = "unscoped",
        mode: str = "unknown",
        agent_name: str = "circuit_startup",
        attempts: int | None = None,
    ) -> "LLMCostCircuitBreaker":
        """Build a durable sentinel even when normal construction explodes."""

        self = cls.__new__(cls)
        self._memory_keeper = None
        self.db_path = str(db_path)
        if db_path == ":memory:":
            # There is no durable filesystem location for a true in-memory
            # database.  The in-process sentinel below still fails closed;
            # importantly, do not create literal ``:memory:*`` sidecars.
            self._emergency_latch_path = None
            self._emergency_lock_path = None
        else:
            self.db_path = str(Path(db_path))
            db_file = Path(self.db_path)
            self._emergency_latch_path = db_file.with_name(
                f"{db_file.name}.llm-circuit-unavailable"
            )
            self._emergency_lock_path = db_file.with_name(
                f"{db_file.name}.llm-circuit.lock"
            )
        self.config = config
        if notifier is None:
            try:
                from src.notifier import TelegramNotifier
                notifier = TelegramNotifier()
            except Exception:
                class _LocalOnlyNotifier:
                    enabled = False

                    @staticmethod
                    def send(_message: str) -> bool:
                        return False

                notifier = _LocalOnlyNotifier()
        self.notifier = notifier
        self._session_context = ContextVar(
            f"qamc_cost_session_{id(self)}", default=(run_id, mode)
        )
        self._infrastructure_lock = threading.Lock()
        self._infrastructure_error = None
        self._unavailable_sentinel = None
        self.mark_unavailable(
            error,
            run_id=run_id,
            mode=mode,
            agent_name=agent_name,
            attempts=attempts,
        )
        return self

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            self.db_path, timeout=10.0, uri=self.db_path.startswith("file:qamc-cost-")
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=10000")
        return conn

    def _best_effort_emergency_snapshot(self, run_id: str) -> dict[str, Any]:
        """Read honest known spend for an emergency alert without mutating DB."""

        day, _, _ = _et_day_and_utc_bounds()
        try:
            with self._connect() as conn:
                day_row = conn.execute(
                    "SELECT baseline_cost_usd + incremental_cost_usd AS cost, "
                    "costs_exact FROM llm_budget_days WHERE day=?",
                    (day,),
                ).fetchone()
                if day_row is None:
                    return {}
                session_row = conn.execute(
                    "SELECT actual_cost_usd, provider_attempts, costs_exact "
                    "FROM llm_budget_sessions WHERE run_id=?",
                    (run_id,),
                ).fetchone()
        except Exception:
            return {}

        snapshot: dict[str, Any] = {
            "daily_cost_usd": float(day_row["cost"] or 0.0),
            "daily_costs_exact": bool(day_row["costs_exact"]),
        }
        if session_row is not None:
            snapshot.update(
                session_cost_usd=float(session_row["actual_cost_usd"] or 0.0),
                attempts=int(session_row["provider_attempts"] or 0),
                attempts_exact=True,
                session_costs_exact=bool(session_row["costs_exact"]),
            )
        return snapshot

    def _read_emergency_latch(self) -> tuple[BaseException, dict[str, Any]] | None:
        path = self._emergency_latch_path
        if path is None or not path.exists():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            detail = str(payload.get("error") or "persistent accounting failure")
            recorded = str(payload.get("recorded_at") or "unknown time")
            return (
                RuntimeError(
                    f"durable cost-circuit emergency latch from {recorded}: {detail}"
                ),
                payload,
            )
        except Exception as exc:
            # A corrupt/unreadable marker is still a marker.  Never interpret
            # an observability problem as permission to make a paid request.
            return (
                RuntimeError(
                    "durable cost-circuit emergency latch exists but is unreadable: "
                    f"{type(exc).__name__}: {str(exc)[:240]}"
                ),
                {},
            )

    def _sync_emergency_latch(self) -> None:
        latched = self._read_emergency_latch()
        if latched is None:
            return
        error, payload = latched
        with self._infrastructure_lock:
            if self._unavailable_sentinel is None:
                self._infrastructure_error = error
                self._unavailable_sentinel = UnavailableLLMCostCircuit(
                    error,
                    notifier=self.notifier,
                    run_id=str(payload.get("run_id") or "unscoped"),
                    mode=str(payload.get("mode") or "unknown"),
                    agent_name=str(
                        payload.get("agent_name") or "circuit_infrastructure"
                    ),
                    attempts=self._safe_optional_int(payload.get("attempts")),
                    attempts_exact=payload.get("attempts_exact") is True,
                    session_cost_usd=self._safe_optional_float(
                        payload.get("session_cost_usd")
                    ),
                    daily_cost_usd=self._safe_optional_float(
                        payload.get("daily_cost_usd")
                    ),
                    costs_exact=payload.get("costs_exact") is True,
                    emergency_latch_path=self._emergency_latch_path,
                    emergency_lock_path=self._emergency_lock_path,
                )

    @staticmethod
    def _safe_optional_int(value: Any) -> int | None:
        if value is None or isinstance(value, bool):
            return None
        try:
            parsed = int(value)
        except (TypeError, ValueError, OverflowError):
            return None
        return parsed if parsed >= 0 else None

    @staticmethod
    def _safe_optional_float(value: Any) -> float | None:
        if value is None or isinstance(value, bool):
            return None
        try:
            parsed = float(value)
        except (TypeError, ValueError, OverflowError):
            return None
        return parsed if parsed >= 0 and parsed < float("inf") else None

    def _emergency_file_lock(self):
        """Serialize infrastructure-latch writers with operator reset."""

        return _file_lock(self._emergency_lock_path)

    def _write_emergency_latch(
        self,
        error: BaseException,
        *,
        run_id: str,
        mode: str,
        agent_name: str,
        attempts: int | None,
        attempts_exact: bool,
        session_cost_usd: float | None,
        daily_cost_usd: float | None,
        costs_exact: bool,
    ) -> None:
        """Atomically persist an accounting-infrastructure shutdown marker."""

        path = self._emergency_latch_path
        if path is None:
            return
        with self._emergency_file_lock():
            # Preserve the original shutdown trigger/affected run. Later
            # failures in other workers must not rewrite incident identity.
            if path.exists():
                return
            payload = json.dumps(
                {
                    "recorded_at": datetime.now(timezone.utc).isoformat(),
                    "error": f"{type(error).__name__}: {str(error)[:500]}",
                    "run_id": run_id,
                    "mode": mode,
                    "agent_name": agent_name,
                    "attempts": attempts,
                    "attempts_exact": attempts_exact,
                    "session_cost_usd": session_cost_usd,
                    "daily_cost_usd": daily_cost_usd,
                    "costs_exact": costs_exact,
                    # item 17(b): durable alert-delivery bookkeeping, folded
                    # in place by `_record_alert_attempt` as
                    # `UnavailableLLMCostCircuit._alert()` runs. Starts
                    # undelivered/zero -- nothing has attempted to notify
                    # the operator about THIS incident yet.
                    "alert_delivered": False,
                    "alert_attempts": 0,
                    "last_alert_attempt_at": None,
                },
                sort_keys=True,
            ) + "\n"
            tmp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
            try:
                fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                try:
                    with os.fdopen(fd, "w", encoding="utf-8") as handle:
                        handle.write(payload)
                        handle.flush()
                        os.fsync(handle.fileno())
                    os.replace(tmp, path)
                    # Persist the directory entry when supported.
                    try:
                        directory_fd = os.open(path.parent, os.O_RDONLY)
                        try:
                            os.fsync(directory_fd)
                        finally:
                            os.close(directory_fd)
                    except OSError:
                        logger.warning(
                            "Could not fsync cost-circuit latch directory %s",
                            path.parent,
                        )
                except Exception:
                    try:
                        os.close(fd)
                    except OSError:
                        pass
                    raise
            finally:
                try:
                    tmp.unlink(missing_ok=True)
                except OSError:
                    logger.warning("Could not remove temporary cost-circuit latch %s", tmp)

    def _infra_retry_backoff_s(self, attempt: int) -> float:
        """Exponential backoff with jitter for one retried infra operation.

        Same shape as `MacroDataProvider._next_backoff` (src/data/macro.py):
        doubles from `infra_fault_retry_backoff_base_s`, capped at
        `infra_fault_retry_backoff_max_s`, plus uniform jitter up to
        `infra_fault_retry_backoff_jitter_s` so concurrent processes hitting
        the same transient fault don't retry in lockstep.
        """

        base = max(
            0.0, float(getattr(self.config, "infra_fault_retry_backoff_base_s", 2.0))
        )
        cap = max(
            base, float(getattr(self.config, "infra_fault_retry_backoff_max_s", 8.0))
        )
        jitter = max(
            0.0, float(getattr(self.config, "infra_fault_retry_backoff_jitter_s", 1.0))
        )
        delay = min(base * (2 ** attempt), cap)
        return delay + (random.uniform(0, jitter) if jitter > 0 else 0.0)

    def _run_with_infra_retry(
        self,
        operation: Callable[[], _T],
        *,
        agent_name: str,
        run_id: str | None = None,
        mode: str | None = None,
    ) -> _T:
        """Retry a transient cost-circuit infrastructure fault with backoff.

        docs/WORK.md item 17(a): distinguishes "I cannot read the budget"
        (a transient I/O or DB-open failure raised by `operation` -- worth
        retrying) from two things it must NOT be confused with:

          * "I am over budget" -- a real, measured breach, which never
            raises here at all: `_trip_locked` records it directly in the
            `llm_circuit_state` row and returns normally. Its signal
            (`PaidAnalysisSuspended` / `OptionalPaidAnalysisRetrySkipped`,
            when it surfaces through `operation`, e.g. from `begin_call`)
            is control flow, not an infrastructure fault -- it passes
            straight through, unretried and unlatched-by-this-method,
            exactly as before.
          * "the ledger IS open and readable, and it is provably wrong" --
            `_validate_accounting_invariants` / `_reconcile_quota_holds_
            locked` raise a plain `RuntimeError` for a detected accounting
            corruption (mismatched settled totals, an inexact day that
            cannot re-arm a hold). That is a deterministic finding, not a
            flake: retrying it produces the exact same answer every time,
            and `scripts/cost_circuit.py` deliberately catches this raw
            exception at several commands to behave differently (`reset`
            tolerates it, `status`/`check` propagate it). Retrying and then
            converting it into the generic sentinel state would both waste
            time on a fault backoff can never fix and break that existing
            distinction. So only `sqlite3.Error` / `OSError` -- the actual
            "cannot open/read the database" shape -- are treated as the
            transient fault this method retries; every other exception
            (including these invariant `RuntimeError`s) passes straight
            through unretried, exactly as before this method existed.

        Only after `infra_fault_max_retries` extra attempts have ALL failed
        does this escalate to the durable file latch via `mark_unavailable`,
        then re-raise the last error so existing callers' own fail-closed
        handling (which every call site already had) is unchanged -- it
        just now only fires once persistence is confirmed, not on the
        first blip.
        """

        max_retries = max(0, int(getattr(self.config, "infra_fault_max_retries", 2)))
        last_exc: BaseException | None = None
        for attempt in range(max_retries + 1):
            try:
                return operation()
            except (PaidAnalysisSuspended, OptionalPaidAnalysisRetrySkipped):
                raise
            except (sqlite3.Error, OSError) as exc:
                last_exc = exc
                if attempt < max_retries:
                    backoff = self._infra_retry_backoff_s(attempt)
                    logger.warning(
                        "Cost-circuit infrastructure fault in %s (attempt "
                        "%d/%d): %s -- retrying in %.1fs",
                        agent_name, attempt + 1, max_retries + 1, exc, backoff,
                    )
                    if backoff > 0:
                        time.sleep(backoff)
                    continue
                logger.critical(
                    "Cost-circuit infrastructure fault in %s persisted past "
                    "%d attempt(s); latching paid analysis closed: %s",
                    agent_name, max_retries + 1, exc, exc_info=True,
                )
        assert last_exc is not None  # loop always returns or sets this
        self.mark_unavailable(
            last_exc, run_id=run_id, mode=mode, agent_name=agent_name,
        )
        raise last_exc

    def mark_unavailable(
        self,
        error: BaseException,
        *,
        run_id: str | None = None,
        mode: str | None = None,
        agent_name: str = "circuit_infrastructure",
        attempts: int | None = None,
    ) -> dict[str, Any]:
        """Permanently fail this process closed after accounting failure.

        Provider code calls this when a breaker DB operation itself raises.
        The shared object then blocks every agent in this process, and the
        no-DB sentinel provides the mandatory Telegram/local alert path.
        """

        current_run, current_mode = self._context()
        affected_run = run_id or current_run
        affected_mode = mode or current_mode
        durable_error: BaseException = error
        snapshot = self._best_effort_emergency_snapshot(affected_run)
        effective_attempts = attempts
        attempts_exact = False
        if snapshot.get("attempts") is not None:
            persisted_attempts = int(snapshot["attempts"])
            if effective_attempts is None or effective_attempts <= persisted_attempts:
                effective_attempts = persisted_attempts
                attempts_exact = bool(snapshot.get("attempts_exact"))
            else:
                # A local request crossed the boundary after the durable
                # snapshot; max() is the honest lower bound, not an exact sum.
                effective_attempts = max(effective_attempts, persisted_attempts)
        elif agent_name == "pricing_preflight" and effective_attempts == 0:
            # Pricing verification precedes every provider request.
            attempts_exact = True
        session_cost = snapshot.get("session_cost_usd")
        if (
            session_cost is None
            and agent_name == "pricing_preflight"
            and effective_attempts == 0
        ):
            session_cost = 0.0
        daily_cost = snapshot.get("daily_cost_usd")
        costs_exact = bool(
            session_cost is not None
            and daily_cost is not None
            and snapshot.get("daily_costs_exact", False)
            and (
                snapshot.get("session_costs_exact", True)
                if snapshot.get("session_cost_usd") is not None else True
            )
        )
        try:
            self._write_emergency_latch(
                error,
                run_id=affected_run,
                mode=affected_mode,
                agent_name=agent_name,
                attempts=effective_attempts,
                attempts_exact=attempts_exact,
                session_cost_usd=session_cost,
                daily_cost_usd=daily_cost,
                costs_exact=costs_exact,
            )
        except Exception as latch_exc:
            # Keep this process stopped even if both persistence mechanisms
            # are impaired, and make the alert explicit that cross-process
            # durability could not be guaranteed.
            durable_error = RuntimeError(
                f"{type(error).__name__}: {str(error)[:300]}; durable emergency "
                f"latch write also failed: {type(latch_exc).__name__}: "
                f"{str(latch_exc)[:240]}"
            )
            logger.critical("Cost-circuit emergency latch write failed", exc_info=True)

        with self._infrastructure_lock:
            if self._infrastructure_error is None:
                self._infrastructure_error = durable_error
                self._unavailable_sentinel = UnavailableLLMCostCircuit(
                    durable_error,
                    notifier=self.notifier,
                    run_id=affected_run,
                    mode=affected_mode,
                    agent_name=agent_name,
                    attempts=effective_attempts,
                    attempts_exact=attempts_exact,
                    session_cost_usd=session_cost,
                    daily_cost_usd=daily_cost,
                    costs_exact=costs_exact,
                    emergency_latch_path=self._emergency_latch_path,
                    emergency_lock_path=self._emergency_lock_path,
                )
            sentinel = self._unavailable_sentinel
        return sentinel.activate_session(
            affected_run,
            affected_mode,
        )

    def _raise_if_unavailable(self, agent_name: str) -> None:
        self._sync_emergency_latch()
        with self._infrastructure_lock:
            sentinel = self._unavailable_sentinel
        if sentinel is not None:
            sentinel.require_paid_analysis(agent_name)

    def _initialize(self) -> None:
        with self._connect() as conn:
            ensure_cost_circuit_schema(conn)
            conn.execute("BEGIN IMMEDIATE")
            self._seed_today(conn)
            conn.commit()

    def _validate_accounting_invariants(self, conn: sqlite3.Connection, day: str) -> None:
        """Reject missing/cross-linked rows instead of interpreting them as $0."""

        day_row = conn.execute(
            "SELECT incremental_cost_usd FROM llm_budget_days WHERE day=?", (day,)
        ).fetchone()
        if day_row is None:
            raise RuntimeError(f"cost-circuit day accounting row is missing for {day}")

        session_total = conn.execute(
            "SELECT COALESCE(SUM(actual_cost_usd), 0) AS cost "
            "FROM llm_budget_sessions WHERE day=? AND status<>'legacy'",
            (day,),
        ).fetchone()
        if abs(
            float(day_row["incremental_cost_usd"] or 0.0)
            - float(session_total["cost"] or 0.0)
        ) > 1e-8:
            raise RuntimeError(
                "cost-circuit day/session settled-cost ledgers disagree for " + day
            )

        try:
            missing_log_session = conn.execute(
                "SELECT a.run_id FROM agent_logs a "
                "LEFT JOIN llm_budget_sessions s ON s.run_id=a.run_id "
                "WHERE a.timestamp BETWEEN ? AND ? AND s.run_id IS NULL LIMIT 1",
                _et_day_and_utc_bounds()[1:],
            ).fetchone()
        except sqlite3.OperationalError as exc:
            if "no such table: agent_logs" not in str(exc).lower():
                raise
            missing_log_session = None
        if missing_log_session is not None:
            raise RuntimeError(
                "same-day paid agent log has no cost-circuit session: "
                f"{missing_log_session['run_id']}"
            )

    def _seed_today(self, conn: sqlite3.Connection) -> None:
        """Seed pre-deployment spend and attempts exactly once per ET day."""

        day, utc_start, utc_end = _et_day_and_utc_bounds()
        exists = conn.execute(
            "SELECT 1 FROM llm_budget_days WHERE day = ?", (day,)
        ).fetchone()
        if exists:
            self._validate_accounting_invariants(conn, day)
            return

        # agent_logs predates the breaker.  Seed its actual reported spend so
        # deploying mid-day cannot reset the budget to zero.
        try:
            row = conn.execute(
                "SELECT COALESCE(SUM(cost_usd), 0) AS cost, "
                "COALESCE(SUM(CASE WHEN cost_usd IS NULL THEN 1 ELSE 0 END), 0) "
                "AS unknown_cost_rows "
                "FROM agent_logs WHERE timestamp BETWEEN ? AND ?",
                (utc_start, utc_end),
            ).fetchone()
            baseline = float(row["cost"] or 0.0)
            unknown_cost_rows = int(row["unknown_cost_rows"] or 0)
            legacy = conn.execute(
                "SELECT run_id, COALESCE(SUM(cost_usd), 0) AS cost, "
                "COUNT(*) AS calls, "
                "COALESCE(SUM(CASE WHEN cost_usd IS NULL THEN 1 ELSE 0 END), 0) "
                "AS unknown_cost_rows FROM agent_logs "
                "WHERE timestamp BETWEEN ? AND ? GROUP BY run_id",
                (utc_start, utc_end),
            ).fetchall()
        except sqlite3.OperationalError as exc:
            # A brand-new standalone breaker DB may legitimately predate the
            # application's agent_logs table. Locks, I/O failures, malformed
            # schema, and every other OperationalError are accounting failures
            # and must fail closed rather than seed a fictitious $0 day.
            if "no such table: agent_logs" not in str(exc).lower():
                raise
            baseline, unknown_cost_rows, legacy = 0.0, 0, []

        conn.execute(
            "INSERT OR IGNORE INTO llm_budget_days"
            "(day, baseline_cost_usd, unknown_cost_rows, costs_exact) "
            "VALUES (?, ?, ?, ?)",
            (day, baseline, unknown_cost_rows, int(unknown_cost_rows == 0)),
        )
        for row in legacy:
            run_id = str(row["run_id"])
            calls = int(row["calls"] or 0)
            conn.execute(
                "INSERT OR IGNORE INTO llm_budget_sessions "
                "(run_id, day, mode, actual_cost_usd, logical_calls, "
                "provider_attempts, costs_exact, status) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 'legacy')",
                (
                    run_id, day, _legacy_mode(run_id), float(row["cost"] or 0),
                    calls, calls, int(int(row["unknown_cost_rows"] or 0) == 0),
                ),
            )
        self._validate_accounting_invariants(conn, day)

    def activate_session(self, run_id: str, mode: str) -> dict[str, Any]:
        """Set call context and register a run without blocking safety work."""

        self._session_context.set((run_id, mode))
        self._sync_emergency_latch()
        with self._infrastructure_lock:
            sentinel = self._unavailable_sentinel
        if sentinel is not None:
            return sentinel.activate_session(run_id, mode)
        if not self.enabled:
            return {"suspended": False, "enabled": False}
        day, _, _ = _et_day_and_utc_bounds()

        def _activate() -> None:
            with self._connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                self._seed_today(conn)
                conn.execute(
                    "INSERT OR IGNORE INTO llm_budget_sessions(run_id, day, mode) "
                    "VALUES (?, ?, ?)",
                    (run_id, day, mode),
                )
                self._reconcile_quota_holds_locked(conn, current_day=day)
                conn.commit()

        try:
            self._run_with_infra_retry(
                _activate, agent_name="circuit_activation", run_id=run_id, mode=mode,
            )
        except Exception:
            # `_run_with_infra_retry` already latched durably after
            # exhausting retries. Fall through to the sentinel path below
            # (deterministic/broker safety work must still proceed) instead
            # of raising out of session activation.
            with self._infrastructure_lock:
                sentinel = self._unavailable_sentinel
            if sentinel is not None:
                return sentinel.activate_session(run_id, mode)
            raise
        self._notify_if_needed()
        # Existing overspend must latch immediately, but never raises here:
        # callers still need to run deterministic/broker safety functions.
        self.enforce_current_limits(agent_name="session_start")
        return self.status()

    def set_session_context(self, run_id: str, mode: str) -> None:
        """Bind this call's run/mode WITHOUT the validating seed-and-check path.

        `activate_session` re-seeds and validates the current day's ledger
        before returning, which is exactly right for every normal caller:
        a broken ledger latches immediately, before any paid call can be
        authorized against it.  `scripts/cost_circuit.py reset` is the one
        legitimate exception.  On 2026-08-28 an operator ran that script to
        clear a hard latch and `main()`'s unconditional `activate_session()`
        call re-validated the day's ledger and raised before `reset` was
        ever dispatched -- the tool meant to clear the emergency was itself
        blocked by it, and the reset had to be done by hand from a Python
        shell instead. `reset()` never depends on the seeded/validated
        state (it reads the `llm_circuit_state` singleton row and the
        emergency-latch file directly, not `_seed_today`), so all this needs
        to do is set the run/mode context `reset()`'s audit trail reads --
        deliberately nothing else.
        """
        self._session_context.set((run_id, mode))

    def _context(self) -> tuple[str, str]:
        return self._session_context.get()

    @staticmethod
    def _totals(conn: sqlite3.Connection, day: str, run_id: str) -> tuple[float, float]:
        """Real settled (daily, session) spend. No reservation component --
        item 14 (2026-09-02) deleted the reservation layer entirely."""

        day_row = conn.execute(
            "SELECT baseline_cost_usd + incremental_cost_usd AS cost "
            "FROM llm_budget_days WHERE day = ?", (day,)
        ).fetchone()
        session_row = conn.execute(
            "SELECT actual_cost_usd FROM llm_budget_sessions WHERE run_id = ?", (run_id,)
        ).fetchone()
        if day_row is None:
            raise RuntimeError(f"cost-circuit day accounting row is missing for {day}")
        daily = float(day_row["cost"] if day_row else 0.0)
        session = float(session_row["actual_cost_usd"] if session_row else 0.0)
        return daily, session

    def _state_row(self, conn: sqlite3.Connection) -> dict[str, Any]:
        row = conn.execute(
            "SELECT * FROM llm_circuit_state WHERE singleton=1"
        ).fetchone()
        if row is None:
            raise RuntimeError("cost-circuit singleton state row is missing")
        return dict(row)

    @staticmethod
    def _scope_key(scope: str, *, day: str, run_id: str, mode: str) -> str:
        if scope == "day":
            return day
        if scope == "mode_day":
            return f"{day}:{mode}"
        if scope == "session":
            return run_id
        raise ValueError(f"unsupported quota scope: {scope}")

    def _active_quota_hold_locked(
        self,
        conn: sqlite3.Connection,
        *,
        day: str,
        run_id: str,
        mode: str,
    ) -> dict[str, Any] | None:
        row = conn.execute(
            "SELECT * FROM llm_quota_holds WHERE active=1 AND day=? AND ("
            "scope='day' OR (scope='mode_day' AND scope_key=?) OR "
            "(scope='session' AND scope_key=?)) "
            "ORDER BY CASE scope WHEN 'day' THEN 1 WHEN 'mode_day' THEN 2 ELSE 3 END, "
            "id LIMIT 1",
            (day, f"{day}:{mode}", run_id),
        ).fetchone()
        return dict(row) if row is not None else None

    def _effective_state_locked(
        self,
        conn: sqlite3.Connection,
        *,
        day: str,
        run_id: str,
        mode: str,
    ) -> dict[str, Any]:
        state = self._state_row(conn)
        if int(state.get("suspended") or 0):
            state.update(
                suspended=True,
                suspension_class="hard",
                hold_scope="global",
                requires_operator_reset=True,
                auto_rearm=False,
            )
            return state
        hold = self._active_quota_hold_locked(
            conn, day=day, run_id=run_id, mode=mode,
        )
        if hold is None:
            state.update(
                suspended=False,
                suspension_class=None,
                hold_scope=None,
                requires_operator_reset=False,
                auto_rearm=False,
            )
            return state
        state.update(
            suspended=True,
            suspension_class="quota",
            hold_scope=hold["scope"],
            hold_day=hold["day"],
            trigger_code=hold["trigger_code"],
            trigger_detail=hold["trigger_detail"],
            run_id=hold["run_id"],
            mode=hold["mode"],
            agent_name=hold["agent_name"],
            session_attempts=hold["attempts"],
            attempts_exact=hold["attempts_exact"],
            costs_exact=hold["costs_exact"],
            session_cost_usd=hold["session_cost_usd"],
            daily_cost_usd=hold["daily_cost_usd"],
            session_limit_usd=hold["session_limit_usd"],
            daily_limit_usd=hold["daily_limit_usd"],
            suspended_at=hold["created_at"],
            requires_operator_reset=False,
            auto_rearm=hold["scope"] in {"day", "mode_day"},
        )
        return state

    def _reconcile_quota_holds_locked(
        self,
        conn: sqlite3.Connection,
        *,
        current_day: str,
    ) -> None:
        """Rearm completed ET-day quota windows without weakening hard faults."""

        hard_state = self._state_row(conn)
        if int(hard_state.get("suspended") or 0):
            return

        # Item 14 (2026-09-02): the reservation layer, and every cross-day
        # reservation-reconciliation check that used to live here, is gone.
        # There is no in-flight dollar exposure that can outlive a day
        # boundary any more -- only settled cost, which the day/session rows
        # already carry forward correctly across rollover.
        day_row = conn.execute(
            "SELECT unknown_cost_rows, costs_exact FROM llm_budget_days WHERE day=?",
            (current_day,),
        ).fetchone()
        if day_row is None:
            raise RuntimeError(
                f"cost-circuit day accounting row is missing for {current_day}"
            )
        current_date = date.fromisoformat(current_day)

        cross_day_holds = conn.execute(
            "SELECT * FROM llm_quota_holds WHERE active=1 AND day<>? ORDER BY id",
            (current_day,),
        ).fetchall()

        # The exactness precondition guards ONE operation: releasing a hold
        # carried over from an earlier ET day. Rearming yesterday's stop while
        # today's books are unproven is what it exists to prevent, and it
        # still does.
        #
        # It used to be checked before this query, so it fired even when there
        # was no cross-day hold to rearm -- gating an operation that was not
        # being performed. That made it a booby trap on the ordinary path,
        # because this reconciler runs on EVERY `begin_call`, and `fail_call`
        # stamps the day inexact whenever it charges a conservative reserve
        # for a request whose true cost it never learned. So the FIRST such
        # failure in a day poisoned every paid call after it: the raise is
        # read as the circuit's own infrastructure failing, which writes the
        # emergency latch and stops the desk until an operator clears it.
        #
        # One rate-limited request, and the trading day was over. That is the
        # 2026-08-26/27/28/31 pattern, and it survived four rounds of fixing
        # limits because nobody was looking at the reconciler -- the earlier
        # hard latches masked it, since this function returns early whenever
        # one is set. Removing the last of those masks (see the NOTE by
        # _SESSION_QUOTA_TRIGGERS) is what finally showed it, on the live desk
        # at 10:36 ET on 2026-08-31, as a crash instead of a suspension.
        #
        # Scope restored to what it protects: no cross-day hold, nothing to
        # rearm, nothing to be exact about.
        if cross_day_holds and (
            int(day_row["unknown_cost_rows"] or 0)
            or not bool(day_row["costs_exact"])
        ):
            raise RuntimeError(
                f"cost-circuit cannot rearm {current_day}: current-day accounting "
                "is not exact"
            )
        holds = []
        for hold in cross_day_holds:
            try:
                hold_date = date.fromisoformat(str(hold["day"]))
            except ValueError:
                hold_date = None
            if hold_date is None or hold_date > current_date:
                self._trip_locked(
                    conn,
                    code="non_monotonic_quota_hold_day",
                    detail=(
                        f"active {hold['scope']} quota hold {hold['id']} has budget "
                        f"day {hold['day']!s} while the current ET day is {current_day}; "
                        "clock regression or accounting corruption requires operator review"
                    ),
                    run_id=str(hold["run_id"]),
                    mode=str(hold["mode"]),
                    agent_name="budget_rollover",
                    attempts=int(hold["attempts"] or 0),
                    attempts_exact=bool(hold["attempts_exact"]),
                    costs_exact=False,
                    session_cost=float(hold["session_cost_usd"] or 0.0),
                    daily_cost=float(hold["daily_cost_usd"] or 0.0),
                )
                return
            holds.append(hold)
        for hold in holds:
            reason = (
                f"ET budget window advanced from {hold['day']} to {current_day}; "
                "exact ledger and accounting invariants passed"
            )
            updated = conn.execute(
                "UPDATE llm_quota_holds SET active=0, released_at=datetime('now'), "
                "release_reason=?, recovery_alert_state=? WHERE id=? AND active=1",
                (
                    reason,
                    0 if hold["scope"] in {"day", "mode_day"} else 1,
                    hold["id"],
                ),
            )
            if updated.rowcount != 1:
                continue
            conn.execute(
                "INSERT INTO llm_circuit_events "
                "(event_type, trigger_code, detail, run_id, mode, agent_name, attempts, "
                "session_cost_usd, daily_cost_usd) VALUES "
                "('quota_rearmed', ?, ?, ?, ?, 'budget_rollover', ?, ?, ?)",
                (
                    hold["trigger_code"], reason, hold["run_id"], hold["mode"],
                    hold["attempts"], hold["session_cost_usd"],
                    hold["daily_cost_usd"],
                ),
            )

    def _hold_quota_locked(
        self,
        conn: sqlite3.Connection,
        *,
        scope: str,
        code: str,
        detail: str,
        day: str,
        run_id: str,
        mode: str,
        agent_name: str,
        attempts: int,
        attempts_exact: bool,
        costs_exact: bool,
        session_cost: float,
        daily_cost: float,
    ) -> bool:
        scope_key = self._scope_key(
            scope, day=day, run_id=run_id, mode=mode,
        )
        existing = conn.execute(
            "SELECT id FROM llm_quota_holds WHERE active=1 AND scope=? "
            "AND scope_key=? AND day=?",
            (scope, scope_key, day),
        ).fetchone()
        if existing is not None:
            return False
        conn.execute(
            "INSERT INTO llm_quota_holds "
            "(scope, scope_key, day, trigger_code, trigger_detail, run_id, mode, "
            "agent_name, attempts, attempts_exact, costs_exact, session_cost_usd, "
            "daily_cost_usd, session_limit_usd, daily_limit_usd) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                scope, scope_key, day, code, detail, run_id, mode, agent_name,
                attempts, int(attempts_exact), int(costs_exact), session_cost,
                daily_cost, float(self.config.session_cost_limit_usd),
                float(self.config.daily_cost_limit_usd),
            ),
        )
        conn.execute(
            "INSERT INTO llm_circuit_events "
            "(event_type, trigger_code, detail, run_id, mode, agent_name, attempts, "
            "session_cost_usd, daily_cost_usd) VALUES "
            "('quota_held', ?, ?, ?, ?, ?, ?, ?, ?)",
            (code, detail, run_id, mode, agent_name, attempts, session_cost, daily_cost),
        )
        updated_session = conn.execute(
            "UPDATE llm_budget_sessions SET status='quota_held', "
            "updated_at=datetime('now') WHERE run_id=?", (run_id,),
        )
        if updated_session.rowcount != 1:
            raise RuntimeError(
                f"cost-circuit could not mark missing session {run_id} quota-held"
            )
        return True

    def _refresh_latched_snapshot_locked(self, conn: sqlite3.Connection) -> None:
        """Refresh alert totals after concurrent/in-flight accounting settles.

        The first failure owns the trigger identity, but its dollar snapshot
        must not remain frozen while another already-authorized request settles
        its real cost in the same sweep.
        """

        state = self._state_row(conn)
        if not int(state.get("suspended") or 0):
            return
        run_id = str(state.get("run_id") or "")
        session_row = conn.execute(
            "SELECT day, actual_cost_usd, provider_attempts, costs_exact "
            "FROM llm_budget_sessions "
            "WHERE run_id=?",
            (run_id,),
        ).fetchone()
        if session_row is None:
            raise RuntimeError(
                f"cost-circuit session row is missing for latched run {run_id}"
            )
        day = str(session_row["day"])
        day_row = conn.execute(
            "SELECT baseline_cost_usd + incremental_cost_usd AS cost, costs_exact "
            "FROM llm_budget_days WHERE day=?",
            (day,),
        ).fetchone()
        if day_row is None:
            raise RuntimeError(
                f"cost-circuit day row is missing for latched run {run_id}"
            )
        session_cost = float(session_row["actual_cost_usd"] or 0.0)
        daily_cost = float(day_row["cost"] if day_row else 0.0)
        costs_exact = (
            bool(state.get("costs_exact", 1))
            and bool(session_row["costs_exact"])
            and bool(day_row["costs_exact"] if day_row else 1)
        )
        if bool(state.get("attempts_exact", 1)):
            attempts = int(session_row["provider_attempts"] or 0)
        else:
            attempts = int(state.get("session_attempts") or 0)
        updated = conn.execute(
            "UPDATE llm_circuit_state SET session_attempts=?, session_cost_usd=?, "
            "daily_cost_usd=?, costs_exact=? "
            "WHERE singleton=1 AND suspended=1",
            (attempts, session_cost, daily_cost, int(costs_exact)),
        )
        if updated.rowcount != 1:
            raise RuntimeError("cost-circuit latched snapshot could not be updated")

    def _trip_locked(
        self,
        conn: sqlite3.Connection,
        *,
        code: str,
        detail: str,
        run_id: str,
        mode: str,
        agent_name: str,
        attempts: int,
        attempts_exact: bool = True,
        costs_exact: bool = True,
        session_cost: float,
        daily_cost: float,
    ) -> bool:
        """Apply the narrowest safe stop. Unknown triggers are hard latches."""

        scope = _trigger_scope(code)
        if scope != "hard":
            session_row = conn.execute(
                "SELECT day FROM llm_budget_sessions WHERE run_id=?", (run_id,)
            ).fetchone()
            if session_row is None:
                raise RuntimeError(
                    f"cost-circuit session row is missing while holding {run_id}"
                )
            return self._hold_quota_locked(
                conn,
                scope=scope,
                code=code,
                detail=detail,
                day=str(session_row["day"]),
                run_id=run_id,
                mode=mode,
                agent_name=agent_name,
                attempts=attempts,
                attempts_exact=attempts_exact,
                costs_exact=costs_exact,
                session_cost=session_cost,
                daily_cost=daily_cost,
            )

        current = self._state_row(conn)
        if int(current.get("suspended") or 0):
            return False
        updated_state = conn.execute(
            "UPDATE llm_circuit_state SET suspended=1, trigger_code=?, "
            "trigger_detail=?, run_id=?, mode=?, agent_name=?, "
            "session_attempts=?, attempts_exact=?, costs_exact=?, "
            "session_cost_usd=?, daily_cost_usd=?, "
            "session_limit_usd=?, daily_limit_usd=?, suspended_at=datetime('now'), "
            "alert_state=0, updated_at=datetime('now') WHERE singleton=1",
            (
                code, detail, run_id, mode, agent_name, attempts,
                int(attempts_exact), int(costs_exact),
                session_cost, daily_cost,
                float(self.config.session_cost_limit_usd),
                float(self.config.daily_cost_limit_usd),
            ),
        )
        if updated_state.rowcount != 1:
            raise RuntimeError("cost-circuit singleton could not be latched")
        conn.execute(
            "INSERT INTO llm_circuit_events "
            "(event_type, trigger_code, detail, run_id, mode, agent_name, attempts, "
            "session_cost_usd, daily_cost_usd) VALUES "
            "('suspended', ?, ?, ?, ?, ?, ?, ?, ?)",
            (code, detail, run_id, mode, agent_name, attempts, session_cost, daily_cost),
        )
        updated_session = conn.execute(
            "UPDATE llm_budget_sessions SET status='suspended', updated_at=datetime('now') "
            "WHERE run_id=?", (run_id,)
        )
        if updated_session.rowcount != 1:
            raise RuntimeError(
                f"cost-circuit could not mark missing session {run_id} suspended"
            )
        return True

    def _notify_if_needed(self) -> None:
        if not self.enabled:
            return
        with self._infrastructure_lock:
            sentinel = self._unavailable_sentinel
        if sentinel is not None:
            sentinel._alert()
            return
        claimed = False
        state: dict[str, Any] = {}
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._refresh_latched_snapshot_locked(conn)
            state = self._state_row(conn)
            if int(state.get("suspended") or 0):
                cur = conn.execute(
                    "UPDATE llm_circuit_state SET alert_state=-1, updated_at=datetime('now') "
                    "WHERE singleton=1 AND suspended=1 AND (alert_state=0 OR "
                    "(alert_state=-1 AND updated_at <= datetime('now', '-2 minutes')))"
                )
                claimed = cur.rowcount == 1
            conn.commit()
        if claimed:
            text = self.format_alert(state)
            # A Telegram outage must not hide the shutdown from local operators.
            # The DB lease remains retryable when send() returns false.
            logger.critical("\n%s", text)
            sent = False
            try:
                sent = bool(self.notifier.send(text))
            except Exception:  # notifier must never affect trading/safety
                logger.exception("cost circuit Telegram alert failed")
            with self._connect() as conn:
                conn.execute(
                    "UPDATE llm_circuit_state SET alert_state=?, "
                    "updated_at=datetime('now') "
                    "WHERE singleton=1 AND alert_state=-1",
                    (1 if sent else 0,),
                )
                conn.commit()
        self._notify_quota_holds_if_needed()
        self._notify_quota_recoveries_if_needed()

    def _notify_quota_holds_if_needed(self) -> None:
        while True:
            hold: dict[str, Any] | None = None
            with self._connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute(
                    "SELECT * FROM llm_quota_holds WHERE "
                    "(alert_state=0 OR (alert_state=-1 AND "
                    "alert_updated_at <= datetime('now', '-2 minutes'))) "
                    "ORDER BY id LIMIT 1"
                ).fetchone()
                if row is not None:
                    claimed = conn.execute(
                        "UPDATE llm_quota_holds SET alert_state=-1, "
                        "alert_updated_at=datetime('now') "
                        "WHERE id=? AND (alert_state=0 OR alert_state=-1)",
                        (row["id"],),
                    ).rowcount == 1
                    if claimed:
                        hold = dict(row)
                conn.commit()
            if hold is None:
                return
            message = self.format_quota_alert(hold)
            logger.critical("\n%s", message)
            sent = False
            try:
                sent = bool(self.notifier.send(message))
            except Exception:
                logger.exception("cost quota Telegram alert failed")
            with self._connect() as conn:
                conn.execute(
                    "UPDATE llm_quota_holds SET alert_state=?, "
                    "alert_updated_at=datetime('now') "
                    "WHERE id=? AND alert_state=-1",
                    (1 if sent else 0, hold["id"]),
                )
                conn.commit()
            if not sent:
                return

    def _notify_quota_recoveries_if_needed(self) -> None:
        while True:
            hold: dict[str, Any] | None = None
            with self._connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute(
                    "SELECT * FROM llm_quota_holds WHERE active=0 AND alert_state=1 AND "
                    "(recovery_alert_state=0 OR (recovery_alert_state=-1 AND "
                    "recovery_alert_updated_at <= datetime('now', '-2 minutes'))) "
                    "ORDER BY id LIMIT 1"
                ).fetchone()
                if row is not None:
                    claimed = conn.execute(
                        "UPDATE llm_quota_holds SET recovery_alert_state=-1, "
                        "recovery_alert_updated_at=datetime('now') "
                        "WHERE id=? AND active=0 AND alert_state=1 AND "
                        "(recovery_alert_state=0 OR "
                        "recovery_alert_state=-1)",
                        (row["id"],),
                    ).rowcount == 1
                    if claimed:
                        hold = dict(row)
                conn.commit()
            if hold is None:
                return
            message = self.format_recovery_alert(hold)
            logger.info("\n%s", message)
            sent = False
            try:
                sent = bool(self.notifier.send(message))
            except Exception:
                logger.exception("cost quota recovery Telegram alert failed")
            with self._connect() as conn:
                conn.execute(
                    "UPDATE llm_quota_holds SET recovery_alert_state=?, "
                    "recovery_alert_updated_at=datetime('now') "
                    "WHERE id=? AND recovery_alert_state=-1",
                    (1 if sent else 0, hold["id"]),
                )
                conn.commit()
            if not sent:
                return

    @staticmethod
    def format_quota_alert(hold: dict[str, Any]) -> str:
        scope = str(hold.get("scope") or "session")
        if scope == "day":
            recovery = (
                "recovery: automatic at the next ET budget day after exact "
                "accounting checks pass"
            )
            affected = "all paid analysis for this ET budget day"
        elif scope == "mode_day":
            recovery = (
                "recovery: this mode is eligible again next ET budget day after "
                "exact accounting checks pass"
            )
            affected = f"{hold.get('mode') or 'this mode'} paid sessions today"
        else:
            recovery = "recovery: later independent sessions remain eligible"
            affected = f"run {hold.get('run_id') or 'unknown'} only"
        attempts = int(hold.get("attempts") or 0)
        session_cost = float(hold.get("session_cost_usd") or 0.0)
        daily_cost = float(hold.get("daily_cost_usd") or 0.0)
        qualifier = "" if bool(hold.get("costs_exact", 1)) else " (conservative exposure)"
        return (
            "🟠 QAMC PAID ANALYSIS QUOTA HOLD\n"
            f"trigger: {hold.get('trigger_detail') or hold.get('trigger_code')}\n"
            f"scope: {affected}\n"
            f"affected run: {hold.get('run_id') or 'unknown'} "
            f"({hold.get('mode') or 'unknown'} / {hold.get('agent_name') or 'unknown'})\n"
            f"attempts: {attempts} provider attempt{'s' if attempts != 1 else ''}\n"
            f"cost: ${session_cost:.4f} this run · ${daily_cost:.4f} "
            f"on ET day {hold.get('day')}{qualifier}\n"
            f"{recovery}\n"
            "preserved: broker-resident stops, reconciliation, deterministic loss "
            "protection, close/P&L jobs, and the read-only API; no operator reset is required."
        )

    @staticmethod
    def format_recovery_alert(hold: dict[str, Any]) -> str:
        scope = str(hold.get("scope") or "day")
        released = (
            "all paid modes"
            if scope == "day"
            else str(hold.get("mode") or "unknown mode")
        )
        return (
            "🟢 QAMC PAID ANALYSIS REARMED\n"
            f"previous hold: {hold.get('trigger_code')} on ET day {hold.get('day')}\n"
            f"scope released: {scope} / {released}\n"
            f"checks passed: {hold.get('release_reason') or 'new ET budget window is exact'}\n"
            "status: paid analysis is eligible again; session, call-count, "
            "attempt, and daily limits remain enforced."
        )

    @staticmethod
    def format_alert(state: dict[str, Any]) -> str:
        attempts = int(state.get("session_attempts") or 0)
        attempts_exact = bool(state.get("attempts_exact", 1))
        session_cost = float(state.get("session_cost_usd") or 0.0)
        daily_cost = float(state.get("daily_cost_usd") or 0.0)
        trigger_code = str(state.get("trigger_code") or "")
        costs_exact = bool(state.get("costs_exact", 1))
        if trigger_code == "legacy_unknown_cost":
            cost_note = " (known minimum; legacy rows have unknown cost)"
        else:
            cost_note = (
                " (includes conservative or unresolved-request accounting)"
                if not costs_exact else ""
            )
        attempts_line = (
            f"attempts: {attempts} provider attempt{'s' if attempts != 1 else ''}"
            if attempts_exact else
            f"attempts: {attempts} logged agent record{'s' if attempts != 1 else ''} "
            "(legacy; exact provider-request count unavailable)"
        )
        return (
            "🔴 QAMC PAID ANALYSIS SUSPENDED\n"
            f"trigger: {state.get('trigger_detail') or state.get('trigger_code') or 'safety limit'}\n"
            f"affected run: {state.get('run_id') or 'unknown'} "
            f"({state.get('mode') or 'unknown'} / {state.get('agent_name') or 'unknown'})\n"
            f"{attempts_line}\n"
            f"cost: ${session_cost:.4f} this run · ${daily_cost:.4f} today{cost_note}\n"
            "suspended: all paid LLM analysis, repairs, retries, and provider failover\n"
            "preserved: broker-resident stops, order/fill reconciliation, deterministic "
            "loss protection, close/P&L jobs, and the read-only API\n"
            "operator reset with a recorded reason is required before paid analysis resumes."
        )

    def _enforce_settled_limits_locked(
        self,
        conn: sqlite3.Connection,
        *,
        day: str,
        run_id: str,
        mode: str,
        agent_name: str,
        attempts: int,
        attempts_exact: bool,
        daily: float,
        session: float,
    ) -> None:
        """Latch already-consumed/unknown spend inside the caller's transaction.

        This check belongs at every authorization boundary, not just pipeline
        preflight. In particular, an operator reset does not erase settled
        spend, so a direct or already-running caller must not get a brief
        opportunity to spend above the unchanged cap.
        """

        day_row = conn.execute(
            "SELECT unknown_cost_rows, costs_exact FROM llm_budget_days WHERE day=?",
            (day,),
        ).fetchone()
        session_row = conn.execute(
            "SELECT costs_exact FROM llm_budget_sessions WHERE run_id=?",
            (run_id,),
        ).fetchone()
        unknown_cost_rows = int(day_row["unknown_cost_rows"] if day_row else 0)
        daily_exact = bool(day_row["costs_exact"] if day_row else True)
        session_exact = bool(session_row["costs_exact"] if session_row else True)

        if unknown_cost_rows > 0:
            self._trip_locked(
                conn, code="legacy_unknown_cost",
                detail=(f"{unknown_cost_rows} same-day row(s) (pre-deployment agent "
                        "logs, or a call that failed ambiguously -- see fail_call/"
                        "complete_call) have unknown cost; daily spend cannot be "
                        "bounded safely"),
                run_id=run_id, mode=mode, agent_name=agent_name,
                attempts=attempts, attempts_exact=attempts_exact,
                costs_exact=False, session_cost=session, daily_cost=daily,
            )
        elif daily >= float(self.config.daily_cost_limit_usd):
            self._trip_locked(
                conn, code="daily_cost_limit",
                detail=(f"daily LLM spend ${daily:.4f} reached safe limit "
                        f"${float(self.config.daily_cost_limit_usd):.2f}"),
                run_id=run_id, mode=mode, agent_name=agent_name,
                attempts=attempts, attempts_exact=attempts_exact,
                session_cost=session, daily_cost=daily,
                costs_exact=daily_exact,
            )
        elif session >= float(self.config.session_cost_limit_usd):
            self._trip_locked(
                conn, code="session_cost_limit",
                detail=(f"session LLM spend ${session:.4f} reached safe limit "
                        f"${float(self.config.session_cost_limit_usd):.2f}"),
                run_id=run_id, mode=mode, agent_name=agent_name,
                attempts=attempts, attempts_exact=attempts_exact,
                session_cost=session, daily_cost=daily,
                costs_exact=session_exact,
            )

    def enforce_current_limits(self, agent_name: str = "preflight") -> dict[str, Any]:
        """Latch on already-consumed daily/session budgets; never raises."""

        if not self.enabled:
            return {"suspended": False, "enabled": False}
        self._sync_emergency_latch()
        with self._infrastructure_lock:
            sentinel = self._unavailable_sentinel
        if sentinel is not None:
            return sentinel.enforce_current_limits(agent_name)
        run_id, mode = self._context()
        day, _, _ = _et_day_and_utc_bounds()

        def _enforce() -> None:
            with self._connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                self._seed_today(conn)
                self._reconcile_quota_holds_locked(conn, current_day=day)
                daily, session = self._totals(conn, day, run_id)
                row = conn.execute(
                    "SELECT provider_attempts, status FROM llm_budget_sessions WHERE run_id=?",
                    (run_id,),
                ).fetchone()
                attempts = int(row["provider_attempts"] if row else 0)
                attempts_exact = not (row and row["status"] == "legacy")
                self._enforce_settled_limits_locked(
                    conn, day=day, run_id=run_id, mode=mode,
                    agent_name=agent_name, attempts=attempts,
                    attempts_exact=attempts_exact, daily=daily, session=session,
                )
                conn.commit()

        try:
            self._run_with_infra_retry(
                _enforce, agent_name=agent_name, run_id=run_id, mode=mode,
            )
        except Exception:
            # Latched durably already by `_run_with_infra_retry`. This
            # method's contract is "never raises" -- defer to the sentinel
            # it just installed rather than propagate.
            with self._infrastructure_lock:
                sentinel = self._unavailable_sentinel
            if sentinel is not None:
                return sentinel.enforce_current_limits(agent_name)
            raise
        self._notify_if_needed()
        return self.status()

    def require_paid_analysis(self, agent_name: str = "analysis") -> None:
        """Raise if paid analysis is suspended; safe to call after broker work."""

        if not self.enabled:
            return
        self.enforce_current_limits(agent_name=agent_name)
        state = self.status()
        if state.get("suspended"):
            self._notify_if_needed()
            raise PaidAnalysisSuspended(str(state.get("trigger_detail") or "circuit open"), state)

    def begin_call(
        self,
        *,
        agent_name: str,
        model: str,
        system_prompt: str,
        user_message: str,
        max_output_tokens: int,
        retry_kind: str | None = None,
        optional_retry: bool = False,
    ) -> CallReservation:
        """Authorize one logical call.

        Item 14 (2026-09-02): no reservation is computed or held. `system_
        prompt`/`user_message`/`max_output_tokens` are accepted purely for
        call-site compatibility -- nothing here prices them. Two checks
        gate the call: (b) real settled cost already recorded today/this
        session against the configured caps (`_enforce_settled_limits_
        locked`, using ONLY what `complete_call`/`fail_call` have already
        recorded -- no projection), and (c) this session's call count
        against `max_calls_per_session`, the runaway-loop backstop.
        """

        if not self.enabled:
            run_id, mode = self._context()
            return CallReservation("disabled", run_id, mode, agent_name, model)
        self._raise_if_unavailable(agent_name)
        run_id, mode = self._context()
        day, _, _ = _et_day_and_utc_bounds()
        reservation_id = uuid.uuid4().hex

        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._seed_today(conn)
            self._reconcile_quota_holds_locked(conn, current_day=day)
            state = self._effective_state_locked(
                conn, day=day, run_id=run_id, mode=mode,
            )
            daily, session = self._totals(conn, day, run_id)
            session_row = conn.execute(
                "SELECT provider_attempts, logical_calls, retry_attempts "
                "FROM llm_budget_sessions WHERE run_id=?",
                (run_id,),
            ).fetchone()
            attempts = int(session_row["provider_attempts"] if session_row else 0)
            logical_calls = int(session_row["logical_calls"] if session_row else 0)

            if int(state.get("suspended") or 0):
                conn.commit()
                self._notify_if_needed()
                raise PaidAnalysisSuspended(str(state.get("trigger_detail") or "circuit open"), state)

            if session_row is None:
                raise RuntimeError(
                    f"cost-circuit session accounting row is missing for run {run_id}"
                )

            # (b): Reset clears the latch, not historical spend.  Recheck the
            # REAL settled-cost ceilings in this same write transaction so
            # neither a direct caller nor an already-running job can spend
            # through the gap between reset and a later pipeline preflight.
            self._enforce_settled_limits_locked(
                conn, day=day, run_id=run_id, mode=mode,
                agent_name=agent_name, attempts=attempts,
                attempts_exact=True, daily=daily, session=session,
            )
            state = self._effective_state_locked(
                conn, day=day, run_id=run_id, mode=mode,
            )
            if int(state.get("suspended") or 0):
                conn.commit()
                self._notify_if_needed()
                raise PaidAnalysisSuspended(
                    str(state.get("trigger_detail") or "circuit open"), state
                )

            # (c): runaway-loop backstop by call COUNT, independent of price.
            max_calls = int(self.config.max_calls_per_session)
            if logical_calls + 1 > max_calls:
                detail = (
                    f"session has already made {logical_calls} call(s); the next "
                    f"{agent_name} call would be call {logical_calls + 1}, above "
                    f"the runaway-loop backstop of {max_calls} calls/session"
                )
                if optional_retry:
                    conn.commit()
                    raise OptionalPaidAnalysisRetrySkipped(
                        detail,
                        {
                            "run_id": run_id,
                            "mode": mode,
                            "agent_name": agent_name,
                            "retry_kind": retry_kind,
                            "logical_calls": logical_calls,
                            "max_calls_per_session": max_calls,
                            "trigger_code": "optional_retry_budget_exhausted",
                        },
                    )
                self._trip_locked(
                    conn, code="session_call_count_limit",
                    detail=detail,
                    run_id=run_id, mode=mode, agent_name=agent_name,
                    attempts=attempts, session_cost=session, daily_cost=daily,
                    costs_exact=True,
                )
                state = self._effective_state_locked(
                    conn, day=day, run_id=run_id, mode=mode,
                )
                conn.commit()
                self._notify_if_needed()
                raise PaidAnalysisSuspended(str(state.get("trigger_detail") or detail), state)

            updated_session = conn.execute(
                "UPDATE llm_budget_sessions SET logical_calls=logical_calls+1, "
                "retry_attempts=retry_attempts+?, updated_at=datetime('now') "
                "WHERE run_id=?", (1 if retry_kind else 0, run_id)
            )
            if updated_session.rowcount != 1:
                raise RuntimeError(
                    f"cost-circuit session row disappeared while authorizing call for {run_id}"
                )
            conn.commit()
        return CallReservation(reservation_id, run_id, mode, agent_name, model)

    def before_provider_attempt(self, reservation: CallReservation, *, model: str) -> int:
        """Authorize one actual provider request; return this call's attempt number.

        Item 14 (2026-09-02): no reservation to look up or extend. Attempt
        counting WITHIN this one logical call is tracked on `reservation`
        itself (in-process; see `CallReservation`'s docstring) purely to
        bound `max_provider_attempts_per_call` -- a retry/failover-storm
        guard independent of the item-14c per-session call-count backstop.
        """

        if not self.enabled or reservation.reservation_id == "disabled":
            return 1
        self._raise_if_unavailable(reservation.agent_name)
        day, _, _ = _et_day_and_utc_bounds()
        next_attempt = reservation.attempt_count + 1
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            # A call can wait behind the provider semaphore while a
            # different process settles spend or accounting is damaged.
            # The provider authorization boundary must therefore re-seed/
            # validate the complete current-day ledger in the same write
            # transaction -- trusting only state read at begin_call would
            # leave a fail-open window immediately before network I/O.
            self._seed_today(conn)
            self._reconcile_quota_holds_locked(conn, current_day=day)
            state = self._effective_state_locked(
                conn, day=day, run_id=reservation.run_id, mode=reservation.mode,
            )
            if int(state.get("suspended") or 0):
                conn.commit()
                self._notify_if_needed()
                raise PaidAnalysisSuspended(str(state.get("trigger_detail") or "circuit open"), state)

            session = conn.execute(
                "SELECT * FROM llm_budget_sessions WHERE run_id=?", (reservation.run_id,)
            ).fetchone()
            if session is None:
                raise RuntimeError(
                    "cost-circuit session accounting row is missing for run "
                    f"{reservation.run_id}"
                )
            session_attempts = int(session["provider_attempts"] or 0)
            daily, session_cost = self._totals(conn, day, reservation.run_id)

            # (b): real settled caps, rechecked immediately before network
            # I/O -- an earlier call in this same session/day can have
            # settled its real cost while this one waited.
            self._enforce_settled_limits_locked(
                conn, day=day, run_id=reservation.run_id,
                mode=reservation.mode, agent_name=reservation.agent_name,
                attempts=session_attempts, attempts_exact=True,
                daily=daily, session=session_cost,
            )
            state = self._effective_state_locked(
                conn, day=day, run_id=reservation.run_id, mode=reservation.mode,
            )
            if int(state.get("suspended") or 0):
                conn.commit()
                self._notify_if_needed()
                raise PaidAnalysisSuspended(
                    str(state.get("trigger_detail") or "circuit open"), state
                )

            max_per_call = int(self.config.max_provider_attempts_per_call)
            if next_attempt > max_per_call:
                self._trip_locked(
                    conn, code="provider_attempt_limit",
                    detail=(f"{reservation.agent_name} provider attempt {next_attempt} "
                            f"exceeds per-call safe limit {max_per_call}"),
                    run_id=reservation.run_id, mode=reservation.mode,
                    agent_name=reservation.agent_name, attempts=session_attempts,
                    session_cost=session_cost, daily_cost=daily,
                    costs_exact=True,
                )
                state = self._effective_state_locked(
                    conn, day=day, run_id=reservation.run_id, mode=reservation.mode,
                )
                conn.commit()
                self._notify_if_needed()
                raise PaidAnalysisSuspended(str(state.get("trigger_detail") or "circuit open"), state)

            updated_session = conn.execute(
                "UPDATE llm_budget_sessions SET provider_attempts=provider_attempts+1, "
                "updated_at=datetime('now') WHERE run_id=?",
                (reservation.run_id,),
            )
            if updated_session.rowcount != 1:
                raise RuntimeError(
                    f"cost-circuit session {reservation.run_id} disappeared at authorization"
                )
            conn.commit()
        reservation.attempt_count = next_attempt
        return next_attempt

    def complete_call(
        self,
        reservation: CallReservation,
        actual_cost_usd: float | None,
        *,
        actual_model: str | None = None,
        failed_attempt_errors: list[BaseException] | None = None,
    ) -> None:
        """Account the ACTUAL provider-reported cost of a completed call.

        Item 14 (2026-09-02): no reservation to release and no estimate to
        reconcile -- only the real number the provider returned is ever
        added to the ledger. `actual_cost_usd is None` means no usable
        cost/token telemetry came back at all; that is recorded as unknown
        spend and latches the circuit outright (continuing would make the
        daily/session totals fiction), same as before.

        `failed_attempt_errors` -- attempts on THIS logical call that did
        NOT produce the response being settled here (a retried primary, a
        failed primary whose failover then succeeded) -- no longer adds a
        dollar charge: there is no reservation left to estimate one from.
        If every one of them is provably a $0 failure
        (`_is_known_zero_cost_failure`), nothing changes. If even one is
        ambiguous, the day/session is marked inexact so
        `_enforce_settled_limits_locked` below fails closed on it -- the
        same fail-closed posture as before, just without inventing a
        figure for what an ambiguous failed attempt might have cost.
        """

        if not self.enabled or reservation.reservation_id == "disabled":
            return
        # Another process can persist the emergency sidecar while this
        # request is in flight.  Observe it before releasing an unaccounted
        # response into the decision pipeline.
        self._raise_if_unavailable(reservation.agent_name)
        with self._infrastructure_lock:
            sentinel = self._unavailable_sentinel
        if sentinel is not None:
            sentinel.require_paid_analysis(reservation.agent_name)
        day, _, _ = _et_day_and_utc_bounds()
        unknown = actual_cost_usd is None
        accounted = 0.0 if unknown else float(actual_cost_usd)
        prior_failures_ambiguous = bool(failed_attempt_errors) and not (
            _all_attempts_provably_free(failed_attempt_errors[-1], failed_attempt_errors)
        )
        exact = not unknown and not prior_failures_ambiguous
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            updated_session = conn.execute(
                "UPDATE llm_budget_sessions SET actual_cost_usd=actual_cost_usd+?, "
                "costs_exact=CASE WHEN ? THEN costs_exact ELSE 0 END, "
                "updated_at=datetime('now') WHERE run_id=?",
                (accounted, int(exact), reservation.run_id),
            )
            if updated_session.rowcount != 1:
                raise RuntimeError(
                    f"cost-circuit session {reservation.run_id} is missing at completion"
                )
            updated_day = conn.execute(
                "UPDATE llm_budget_days SET incremental_cost_usd=incremental_cost_usd+?, "
                "unknown_cost_rows=unknown_cost_rows+?, "
                "costs_exact=CASE WHEN ? THEN costs_exact ELSE 0 END, "
                "updated_at=datetime('now') WHERE day=?",
                (
                    accounted,
                    # Either "no telemetry at all" or "an earlier attempt on
                    # this call was ambiguous" makes the day's real total
                    # unprovable -- both fail closed the same way, via
                    # `_enforce_settled_limits_locked`'s unknown_cost_rows
                    # check, immediately below and at the next boundary.
                    int(unknown or prior_failures_ambiguous),
                    int(exact), day,
                ),
            )
            if updated_day.rowcount != 1:
                raise RuntimeError(
                    f"cost-circuit day {day} is missing at completion"
                )
            daily, session_cost = self._totals(conn, day, reservation.run_id)
            session_row = conn.execute(
                "SELECT provider_attempts FROM llm_budget_sessions WHERE run_id=?",
                (reservation.run_id,),
            ).fetchone()
            attempts = int(session_row["provider_attempts"] or 0)
            if unknown:
                self._trip_locked(
                    conn, code="unknown_actual_cost",
                    detail=(f"{reservation.agent_name} returned no usable token/cost telemetry; "
                            "continuing cannot be budgeted safely"),
                    run_id=reservation.run_id, mode=reservation.mode,
                    agent_name=reservation.agent_name, attempts=attempts,
                    session_cost=session_cost, daily_cost=daily,
                    costs_exact=False,
                )
            else:
                # (b): stop the instant REAL SETTLED spend -- this call's
                # actual reported cost included -- reaches either cap. Also
                # catches the ambiguous-prior-attempt case above, via the
                # day's now-nonzero unknown_cost_rows.
                self._enforce_settled_limits_locked(
                    conn, day=day, run_id=reservation.run_id, mode=reservation.mode,
                    agent_name=reservation.agent_name, attempts=attempts,
                    attempts_exact=True, daily=daily, session=session_cost,
                )
            self._refresh_latched_snapshot_locked(conn)
            conn.commit()
        self._notify_if_needed()

    def fail_call(
        self,
        reservation: CallReservation,
        error: BaseException,
        attempt_errors: list[BaseException] | None = None,
    ) -> None:
        """Account a failed provider request -- no reservation to convert
        into spend any more (item 14, 2026-09-02).

        A failure PROVEN to have cost $0 (`_is_known_zero_cost_failure`: an
        HTTP 429/400/401/403/404 rejection, or a pre-send transport failure
        -- DNS, connection refused, TLS handshake) is recorded as exactly
        that, $0, and changes nothing else. A provably-$0 rejection is not
        evidence the SESSION went wrong; it is evidence this one attempt
        cost nothing and the caller is free to retry.

        An AMBIGUOUS failure -- a cut stream, an unclassified error, a 5xx
        after generation may have started -- has a real, unknowable cost.
        This module no longer estimates one: it marks the day/session
        inexact (`unknown_cost_rows`) so `_enforce_settled_limits_locked`
        fails closed on it at the very next authorization boundary. Same
        fail-closed posture as before, without inventing a dollar figure
        for a request that may or may not have billed anything.

        Nothing is accounted at all if no provider attempt was ever made
        (`reservation.attempt_count == 0`, e.g. `before_provider_attempt`
        itself raised) -- there is nothing ambiguous about a request that
        never reached the network.

        `attempt_errors` is every provider attempt's failure on this
        logical call, not just the one the caller re-raised -- see
        `_all_attempts_provably_free`'s docstring for why that matters.
        """

        if not self.enabled or reservation.reservation_id == "disabled":
            return
        self._sync_emergency_latch()
        with self._infrastructure_lock:
            if self._unavailable_sentinel is not None:
                return
        attempted = reservation.attempt_count > 0
        ambiguous = attempted and not _all_attempts_provably_free(error, attempt_errors)
        day, _, _ = _et_day_and_utc_bounds()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if ambiguous:
                updated_session = conn.execute(
                    "UPDATE llm_budget_sessions SET status='call_failed', "
                    "costs_exact=0, updated_at=datetime('now') WHERE run_id=?",
                    (reservation.run_id,),
                )
            else:
                updated_session = conn.execute(
                    "UPDATE llm_budget_sessions SET updated_at=datetime('now') "
                    "WHERE run_id=?",
                    (reservation.run_id,),
                )
            if updated_session.rowcount != 1:
                raise RuntimeError(
                    f"cost-circuit session {reservation.run_id} is missing at failure"
                )
            if ambiguous:
                updated_day = conn.execute(
                    "UPDATE llm_budget_days SET "
                    "unknown_cost_rows=unknown_cost_rows+1, "
                    "costs_exact=0, updated_at=datetime('now') WHERE day=?",
                    (day,),
                )
                if updated_day.rowcount != 1:
                    raise RuntimeError(
                        f"cost-circuit day {day} is missing at failure"
                    )
            daily, session_cost = self._totals(conn, day, reservation.run_id)
            session = conn.execute(
                "SELECT provider_attempts FROM llm_budget_sessions WHERE run_id=?",
                (reservation.run_id,),
            ).fetchone()
            attempts = int(session["provider_attempts"] if session else 0)
            conn.execute(
                "INSERT INTO llm_circuit_events "
                "(event_type, detail, run_id, mode, agent_name, attempts, "
                "session_cost_usd, daily_cost_usd) VALUES "
                "('call_failed', ?, ?, ?, ?, ?, ?, ?)",
                (
                    type(error).__name__, reservation.run_id, reservation.mode,
                    reservation.agent_name, attempts, session_cost, daily,
                ),
            )
            if ambiguous:
                self._trip_locked(
                    conn,
                    code="failed_call_unknown_cost",
                    detail=(
                        f"{reservation.agent_name} failed after {attempts} provider "
                        "attempt(s) with no provable-zero-cost telemetry; the real "
                        "cost is unknown and cannot be bounded safely"
                    ),
                    run_id=reservation.run_id,
                    mode=reservation.mode,
                    agent_name=reservation.agent_name,
                    attempts=attempts,
                    session_cost=session_cost,
                    daily_cost=daily,
                    costs_exact=False,
                )
            self._refresh_latched_snapshot_locked(conn)
            conn.commit()
        self._notify_if_needed()

    def status(self) -> dict[str, Any]:
        if not self.enabled:
            return {"enabled": False, "suspended": False}
        self._sync_emergency_latch()
        with self._infrastructure_lock:
            sentinel = self._unavailable_sentinel
        if sentinel is not None:
            return sentinel.status()
        day, _, _ = _et_day_and_utc_bounds()
        run_id, mode = self._context()
        with self._connect() as conn:
            ensure_cost_circuit_schema(conn)
            conn.execute("BEGIN IMMEDIATE")
            self._seed_today(conn)
            self._reconcile_quota_holds_locked(conn, current_day=day)
            state = self._effective_state_locked(
                conn, day=day, run_id=run_id, mode=mode,
            )
            daily, session = self._totals(conn, day, run_id)
            session_row = conn.execute(
                "SELECT logical_calls, provider_attempts, retry_attempts FROM llm_budget_sessions "
                "WHERE run_id=?", (run_id,),
            ).fetchone()
            result = dict(state)
            result.update(
                enabled=True,
                suspended=bool(state.get("suspended")),
                current_day=day,
                current_run_id=run_id,
                current_mode=mode,
                current_session_cost_usd=session,
                current_daily_cost_usd=daily,
                logical_calls=int(session_row["logical_calls"] if session_row else 0),
                provider_attempts=int(session_row["provider_attempts"] if session_row else 0),
                retry_attempts=int(session_row["retry_attempts"] if session_row else 0),
            )
        self._notify_if_needed()
        return result

    def reset(self, reason: str) -> None:
        """Operator-only manual reset. A reason is mandatory and audited.

        Also clears an INEXACT current ET day, which is the other fault only
        an operator can resolve. `fail_call` stamps `costs_exact=0` when it
        charges a conservative reserve for a request whose true cost it could
        not learn, and nothing sets it back within the day -- the flag clears
        only when the next ET day seeds a fresh row. Meanwhile
        `_reconcile_quota_holds_locked` REFUSES to rearm over an inexact day,
        by design.

        Until 2026-08-31 those two facts never met, because an inexact day
        was always accompanied by a hard latch, and the reconciler returns
        early whenever one is set -- the latch was, in effect, masking the
        refusal. Scoping `provider_attempt_limit` to the session removed that
        latch and exposed the real shape of it: an inexact day with no latch
        made every subsequent `activate_session` raise, with no operator
        action able to clear it, until ET rollover. A crash loop is a worse
        failure than the suspension it replaced.

        What this does NOT do is erase settled spend. The day's recorded
        amount is left exactly as it stands -- including the conservative
        reserve charged for the unresolved request, which over-states cost
        rather than under-stating it. Only the "we could not prove this
        figure" flag is cleared, and only by a named operator giving a
        reason. Deciding that a conservative figure is good enough to
        continue on is precisely an operator's call; recomputing what the
        provider really charged is not something this code can honestly do.
        """

        reason = reason.strip()
        if not reason:
            raise ValueError("a non-empty reset reason is required")
        run_id, mode = self._context()
        # The OS lock serializes reset against infrastructure-failure writers.
        # If a new failure starts after this reset, it acquires the lock next
        # and its marker survives; reset can never delete a concurrent marker.
        with self._emergency_file_lock():
            with self._connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                state = self._state_row(conn)
                emergency_latched = bool(
                    self._emergency_latch_path is not None
                    and self._emergency_latch_path.exists()
                )
                current_day, _, _ = _et_day_and_utc_bounds()
                day_row = conn.execute(
                    "SELECT unknown_cost_rows, costs_exact FROM llm_budget_days "
                    "WHERE day=?",
                    (current_day,),
                ).fetchone()
                day_inexact = day_row is not None and (
                    int(day_row["unknown_cost_rows"] or 0)
                    or not bool(day_row["costs_exact"])
                )
                if (
                    not int(state.get("suspended") or 0)
                    and not emergency_latched
                    and not day_inexact
                ):
                    raise ValueError(
                        "no operator-resettable hard circuit is active and the "
                        "current ET day's accounting is exact; scoped quota "
                        "holds expire only with their budget window"
                    )
                conn.execute(
                    "INSERT INTO llm_circuit_events "
                    "(event_type, trigger_code, detail, run_id, mode, agent_name, attempts, "
                    "session_cost_usd, daily_cost_usd) VALUES "
                    "('reset', ?, ?, ?, ?, 'operator', ?, ?, ?)",
                    (
                        state.get("trigger_code"), reason, run_id, mode,
                        int(state.get("session_attempts") or 0),
                        float(state.get("session_cost_usd") or 0),
                        float(state.get("daily_cost_usd") or 0),
                    ),
                )
                updated_state = conn.execute(
                    "UPDATE llm_circuit_state SET suspended=0, trigger_code=NULL, "
                    "trigger_detail=NULL, run_id=NULL, mode=NULL, agent_name=NULL, "
                    "session_attempts=0, session_cost_usd=0, daily_cost_usd=0, "
                    "attempts_exact=1, costs_exact=1, "
                    "suspended_at=NULL, alert_state=0, reset_at=datetime('now'), "
                    "reset_reason=?, updated_at=datetime('now') WHERE singleton=1",
                    (reason,),
                )
                if updated_state.rowcount != 1:
                    raise RuntimeError("cost-circuit singleton could not be reset")
                if day_inexact:
                    # The amount is untouched on purpose -- see the docstring.
                    # Only the unprovable-figure flag is cleared, so the
                    # reconciler can rearm and the desk can continue on a
                    # conservative number the operator has accepted.
                    conn.execute(
                        "UPDATE llm_budget_days SET unknown_cost_rows=0, "
                        "costs_exact=1, updated_at=datetime('now') WHERE day=?",
                        (current_day,),
                    )
                conn.commit()
            # DB opens first while the marker still blocks every process; the
            # unlink is the final operator-authorized transition.
            if self._emergency_latch_path is not None:
                self._emergency_latch_path.unlink(missing_ok=True)
                if self._emergency_latch_path.exists():
                    raise RuntimeError(
                        f"could not clear durable circuit latch {self._emergency_latch_path}"
                    )
        with self._infrastructure_lock:
            self._infrastructure_error = None
            self._unavailable_sentinel = None


def activate_paid_call_session(
    app_config: Any,
    *,
    run_id: str,
    mode: str,
    notifier: Any | None = None,
    db_path: str | Path | None = None,
) -> LLMCostCircuitBreaker:
    """Construct and activate the mandatory breaker for operator paid tools.

    Application services construct their breaker inside ``TradingPipeline``.
    Standalone replays, benchmarks, smoke checks, and commissioning probes do
    not instantiate that pipeline, so they must use this common boundary
    instead of silently calling an SDK or BaseAgent without accounting.
    """

    raw_db_path = str(db_path or app_config.storage.db_path)
    if raw_db_path == ":memory:":
        resolved_db_path = raw_db_path
    else:
        resolved = Path(raw_db_path)
        if not resolved.is_absolute():
            resolved = Path(__file__).resolve().parent.parent / resolved
        resolved_db_path = str(resolved)
    try:
        breaker = LLMCostCircuitBreaker(
            resolved_db_path, app_config.llm_cost_circuit, notifier=notifier,
        )
    except Exception as exc:
        breaker = LLMCostCircuitBreaker.fail_closed(
            resolved_db_path,
            app_config.llm_cost_circuit,
            exc,
            notifier=notifier,
            run_id=run_id,
            mode=mode,
            agent_name="circuit_startup",
        )
    try:
        from src.cost_table import refresh_openrouter_pricing
        # Pricing-staleness SPOF fix (2026-08-28): pass the configured grace
        # window/multiplier through explicitly so a stale-but-recent cache
        # is used (widened, logged loudly) instead of latching this whole
        # process the moment openrouter.ai is briefly unreachable -- see the
        # long note above `refresh_openrouter_pricing` in src/cost_table.py.
        pricing_ok = refresh_openrouter_pricing(
            grace_period_hours=float(
                getattr(
                    app_config.llm_cost_circuit,
                    "openrouter_pricing_grace_period_hours", 0.0,
                )
            ),
            max_stale_multiplier=float(
                getattr(
                    app_config.llm_cost_circuit,
                    "openrouter_pricing_stale_multiplier_max", 1.0,
                )
            ),
        )
    except Exception as exc:
        breaker.mark_unavailable(
            exc,
            run_id=run_id,
            mode=mode,
            agent_name="pricing_preflight",
            attempts=0,
        )
    else:
        if not pricing_ok:
            breaker.mark_unavailable(
                RuntimeError(
                    "current official OpenRouter pricing is unavailable; "
                    "paid calls cannot be bounded safely"
                ),
                run_id=run_id,
                mode=mode,
                agent_name="pricing_preflight",
                attempts=0,
            )
    try:
        breaker.activate_session(run_id, mode)
    except Exception as exc:
        breaker.mark_unavailable(
            exc,
            run_id=run_id,
            mode=mode,
            agent_name="circuit_activation",
        )
    return breaker


def protect_paid_agent(
    agent: Any,
    app_config: Any,
    *,
    run_id: str,
    mode: str,
    notifier: Any | None = None,
    db_path: str | Path | None = None,
) -> LLMCostCircuitBreaker:
    """Activate one operator-tool session and attach it to a BaseAgent."""

    breaker = activate_paid_call_session(
        app_config, run_id=run_id, mode=mode,
        notifier=notifier, db_path=db_path,
    )
    agent.set_cost_circuit(breaker)
    breaker.require_paid_analysis(f"{mode}_start")
    return breaker
