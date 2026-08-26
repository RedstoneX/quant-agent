"""Persistent, cross-process circuit breaker for paid LLM analysis.

The breaker is deliberately independent from the trading decision path.  It
can suspend model requests, but it cannot stop broker reconciliation,
broker-resident protective orders, deterministic loss checks, P&L capture,
or the read-only API.

Every provider request is authorized in SQLite under ``BEGIN IMMEDIATE``.
That matters because the morning and intraday systemd jobs are separate
processes and can otherwise both observe the same remaining budget and spend
it.  A conservative pre-call reservation is made before network I/O and is
replaced with provider-reported cost after a successful response.
"""

from __future__ import annotations

import logging
import json
import os
import fcntl
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import date, datetime, time as dt_time, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from src.cost_table import PRICING

logger = logging.getLogger(__name__)

_ET = ZoneInfo("America/New_York")

# Expected budget exhaustion is not an infrastructure incident.  Keep those
# stops scoped to the budget window that owns them, while every unknown or
# integrity-related trigger defaults to the durable operator-reset latch.
_DAY_QUOTA_TRIGGERS = frozenset({
    "daily_cost_limit",
    "projected_daily_cost_limit",
    "provider_projected_daily_cost_limit",
    "outstanding_projected_daily_cost_limit",
})
_MODE_DAY_QUOTA_TRIGGERS = frozenset({"session_retry_limit"})
_SESSION_QUOTA_TRIGGERS = frozenset({
    "session_cost_limit",
    "projected_session_cost_limit",
    "provider_projected_session_cost_limit",
    "outstanding_projected_session_cost_limit",
    "session_retry_attempt_limit",
})


def _trigger_scope(code: Any) -> str:
    if not isinstance(code, str):
        return "hard"
    if code in _DAY_QUOTA_TRIGGERS:
        return "day"
    if code in _MODE_DAY_QUOTA_TRIGGERS:
        return "mode_day"
    if code in _SESSION_QUOTA_TRIGGERS:
        return "session"
    return "hard"


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


@dataclass(frozen=True)
class CallReservation:
    reservation_id: str
    run_id: str
    mode: str
    agent_name: str
    model: str
    input_tokens_estimate: int
    max_output_tokens: int


def ensure_cost_circuit_schema(conn: sqlite3.Connection) -> None:
    """Create the additive breaker schema on an existing SQLite connection."""

    expected_breaker_tables = {
        "llm_budget_days", "llm_budget_sessions", "llm_budget_reservations",
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

        CREATE TABLE IF NOT EXISTS llm_budget_reservations (
            reservation_id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL,
            day TEXT NOT NULL,
            mode TEXT NOT NULL,
            agent_name TEXT NOT NULL,
            model TEXT NOT NULL,
            input_tokens_estimate INTEGER NOT NULL,
            max_output_tokens INTEGER NOT NULL,
            attempt_count INTEGER NOT NULL DEFAULT 0,
            reserved_cost_usd REAL NOT NULL,
            actual_cost_usd REAL,
            status TEXT NOT NULL DEFAULT 'active',
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            expires_at TEXT NOT NULL,
            completed_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_llm_reservations_active
            ON llm_budget_reservations(day, status, expires_at);

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
        self._context_value: ContextVar[tuple[str, str]] = ContextVar(
            f"qamc_unavailable_cost_session_{id(self)}",
            default=(run_id, mode),
        )
        self._alert_lock = threading.Lock()
        self._alert_delivered = False
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
        try:
            sent = bool(self.notifier.send(message))
            if sent:
                with self._alert_lock:
                    self._alert_delivered = True
            else:
                logger.critical("cost-circuit unavailable alert was not delivered to Telegram")
        except Exception:
            logger.exception("cost-circuit unavailable Telegram alert failed")

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
            self._initialize()
        except Exception as exc:
            self.mark_unavailable(exc)

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
                day_reserve_row = conn.execute(
                    "SELECT COALESCE(SUM(reserved_cost_usd), 0) AS cost "
                    "FROM llm_budget_reservations WHERE day=? AND status='active'",
                    (day,),
                ).fetchone()
                session_reserve_row = conn.execute(
                    "SELECT COALESCE(SUM(reserved_cost_usd), 0) AS cost "
                    "FROM llm_budget_reservations WHERE run_id=? AND status='active'",
                    (run_id,),
                ).fetchone()
        except Exception:
            return {}

        day_reserve = float(day_reserve_row["cost"] or 0.0)
        session_reserve = float(session_reserve_row["cost"] or 0.0)
        snapshot: dict[str, Any] = {
            "daily_cost_usd": float(day_row["cost"] or 0.0) + day_reserve,
            "daily_costs_exact": bool(day_row["costs_exact"]) and not bool(day_reserve),
        }
        if session_row is not None:
            snapshot.update(
                session_cost_usd=(
                    float(session_row["actual_cost_usd"] or 0.0) + session_reserve
                ),
                attempts=int(session_row["provider_attempts"] or 0),
                attempts_exact=True,
                session_costs_exact=(
                    bool(session_row["costs_exact"]) and not bool(session_reserve)
                ),
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

    @contextmanager
    def _emergency_file_lock(self):
        """Serialize infrastructure-latch writers with operator reset."""

        path = self._emergency_lock_path
        if path is None:
            yield
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)

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

        bad_session = conn.execute(
            "SELECT s.run_id FROM llm_budget_sessions s "
            "LEFT JOIN llm_budget_reservations r ON r.run_id=s.run_id "
            "WHERE s.day=? AND s.status<>'legacy' GROUP BY s.run_id "
            "HAVING s.logical_calls<>COUNT(r.reservation_id) "
            "OR s.provider_attempts<>COALESCE(SUM(r.attempt_count), 0) LIMIT 1",
            (day,),
        ).fetchone()
        if bad_session is not None:
            raise RuntimeError(
                "cost-circuit reservation/attempt ledger is inconsistent for run "
                f"{bad_session['run_id']}"
            )

        orphan = conn.execute(
            "SELECT r.reservation_id FROM llm_budget_reservations r "
            "LEFT JOIN llm_budget_sessions s ON s.run_id=r.run_id "
            "WHERE r.day=? AND (s.run_id IS NULL OR s.day<>r.day OR s.mode<>r.mode) "
            "LIMIT 1",
            (day,),
        ).fetchone()
        if orphan is not None:
            raise RuntimeError(
                "cost-circuit reservation has no matching session: "
                f"{orphan['reservation_id']}"
            )

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
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._seed_today(conn)
            conn.execute(
                "INSERT OR IGNORE INTO llm_budget_sessions(run_id, day, mode) "
                "VALUES (?, ?, ?)",
                (run_id, day, mode),
            )
            self._expire_reservations(conn)
            self._reconcile_quota_holds_locked(conn, current_day=day)
            conn.commit()
        self._notify_if_needed()
        # Existing overspend must latch immediately, but never raises here:
        # callers still need to run deterministic/broker safety functions.
        self.enforce_current_limits(agent_name="session_start")
        return self.status()

    def _context(self) -> tuple[str, str]:
        return self._session_context.get()

    def _expire_reservations(self, conn: sqlite3.Connection) -> None:
        """Release never-started reservations; charge/latch abandoned calls.

        A wrapper SIGKILL can land after ``before_provider_attempt`` but
        before ``complete_call``/``fail_call``.  Such a request may have been
        billed.  Its reserve therefore becomes conservative spend when the
        lease expires.  Only a reservation with attempt_count=0 is known to
        have made no provider request and may be released for free.
        """

        current_day, _, _ = _et_day_and_utc_bounds()
        current_date = date.fromisoformat(current_day)
        active_rows = conn.execute(
            "SELECT * FROM llm_budget_reservations WHERE status='active' "
            "ORDER BY created_at, reservation_id"
        ).fetchall()
        for row in active_rows:
            try:
                reservation_date = date.fromisoformat(str(row["day"]))
            except ValueError:
                reservation_date = None
            if reservation_date is None or reservation_date > current_date:
                reserved = float(row["reserved_cost_usd"] or 0.0)
                self._trip_locked(
                    conn,
                    code="non_monotonic_reservation_day",
                    detail=(
                        f"active reservation {row['reservation_id']} has budget day "
                        f"{row['day']!s} while the current ET day is {current_day}; "
                        "clock regression or accounting corruption requires operator review"
                    ),
                    run_id=str(row["run_id"]),
                    mode=str(row["mode"]),
                    agent_name=str(row["agent_name"]),
                    attempts=int(row["attempt_count"] or 0),
                    costs_exact=False,
                    session_cost=reserved,
                    daily_cost=reserved,
                )
                return

        rows = conn.execute(
            "SELECT * FROM llm_budget_reservations "
            "WHERE status='active' AND expires_at <= datetime('now')"
        ).fetchall()
        for row in rows:
            attempts = int(row["attempt_count"] or 0)
            accounted = float(row["reserved_cost_usd"] or 0) if attempts > 0 else 0.0
            updated = conn.execute(
                "UPDATE llm_budget_reservations SET status=?, actual_cost_usd=?, "
                "reserved_cost_usd=0, completed_at=datetime('now') "
                "WHERE reservation_id=? AND status='active'",
                (
                    "expired_attempted" if attempts else "expired_unattempted",
                    accounted,
                    row["reservation_id"],
                ),
            )
            # Another process may have completed/expired this reservation
            # between selection and update unless the caller already owns the
            # write lock. Never charge a reservation whose state transition we
            # did not win.
            if updated.rowcount != 1:
                continue
            if not accounted:
                continue
            conn.execute(
                "UPDATE llm_budget_sessions SET actual_cost_usd=actual_cost_usd+?, "
                "costs_exact=0, status='abandoned_call', "
                "updated_at=datetime('now') WHERE run_id=?",
                (accounted, row["run_id"]),
            )
            if conn.execute("SELECT changes()").fetchone()[0] != 1:
                raise RuntimeError(
                    f"cost-circuit session row missing while expiring {row['reservation_id']}"
                )
            updated_day = conn.execute(
                "UPDATE llm_budget_days SET incremental_cost_usd=incremental_cost_usd+?, "
                "costs_exact=0, updated_at=datetime('now') WHERE day=?",
                (accounted, row["day"]),
            )
            if updated_day.rowcount != 1:
                raise RuntimeError(
                    f"cost-circuit day row missing while expiring {row['reservation_id']}"
                )
            daily, session_cost, _ = self._totals(conn, row["day"], row["run_id"])
            session_row = conn.execute(
                "SELECT provider_attempts FROM llm_budget_sessions WHERE run_id=?",
                (row["run_id"],),
            ).fetchone()
            session_attempts = int(session_row["provider_attempts"] if session_row else attempts)
            self._trip_locked(
                conn,
                code="expired_attempted_reservation",
                detail=(
                    f"{row['agent_name']} had {attempts} started provider request(s) "
                    "whose process ended before usage telemetry; the unresolved "
                    f"${accounted:.4f} reserve was charged to the safety budget"
                ),
                run_id=row["run_id"],
                mode=row["mode"],
                agent_name=row["agent_name"],
                attempts=session_attempts,
                session_cost=session_cost,
                daily_cost=daily,
                costs_exact=False,
            )
        self._refresh_latched_snapshot_locked(conn)

    @staticmethod
    def _totals(conn: sqlite3.Connection, day: str, run_id: str) -> tuple[float, float, float]:
        day_row = conn.execute(
            "SELECT baseline_cost_usd + incremental_cost_usd AS cost "
            "FROM llm_budget_days WHERE day = ?", (day,)
        ).fetchone()
        session_row = conn.execute(
            "SELECT actual_cost_usd FROM llm_budget_sessions WHERE run_id = ?", (run_id,)
        ).fetchone()
        reserved_day = conn.execute(
            "SELECT COALESCE(SUM(reserved_cost_usd), 0) AS cost "
            "FROM llm_budget_reservations WHERE day = ? AND status='active'",
            (day,),
        ).fetchone()
        if day_row is None:
            raise RuntimeError(f"cost-circuit day accounting row is missing for {day}")
        daily = float(day_row["cost"] if day_row else 0.0)
        session = float(session_row["actual_cost_usd"] if session_row else 0.0)
        reserved = float(reserved_day["cost"] if reserved_day else 0.0)
        return daily, session, reserved

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

        cross_day_reservations = conn.execute(
            "SELECT * FROM llm_budget_reservations "
            "WHERE status='active' AND day<>? ORDER BY created_at, reservation_id",
            (current_day,),
        ).fetchall()
        current_date = date.fromisoformat(current_day)
        prior_reservations = []
        for row in cross_day_reservations:
            try:
                reservation_date = date.fromisoformat(str(row["day"]))
            except ValueError:
                reservation_date = None
            if reservation_date is None or reservation_date > current_date:
                reserved = float(row["reserved_cost_usd"] or 0.0)
                self._trip_locked(
                    conn,
                    code="non_monotonic_reservation_day",
                    detail=(
                        f"active reservation {row['reservation_id']} has budget day "
                        f"{row['day']!s} while the current ET day is {current_day}; "
                        "clock regression or accounting corruption requires operator review"
                    ),
                    run_id=str(row["run_id"]),
                    mode=str(row["mode"]),
                    agent_name=str(row["agent_name"]),
                    attempts=int(row["attempt_count"] or 0),
                    costs_exact=False,
                    session_cost=reserved,
                    daily_cost=reserved,
                )
                return
            prior_reservations.append(row)
        for row in prior_reservations:
            attempts = int(row["attempt_count"] or 0)
            if attempts == 0:
                updated = conn.execute(
                    "UPDATE llm_budget_reservations SET "
                    "status='expired_cross_day_unattempted', reserved_cost_usd=0, "
                    "actual_cost_usd=0, completed_at=datetime('now') "
                    "WHERE reservation_id=? AND status='active'",
                    (row["reservation_id"],),
                )
                if updated.rowcount != 1:
                    raise RuntimeError(
                        f"cross-day reservation {row['reservation_id']} could not be released"
                    )
                continue
            daily, session_cost, reserved_day = self._totals(
                conn, str(row["day"]), str(row["run_id"]),
            )
            self._trip_locked(
                conn,
                code="cross_day_started_reservation",
                detail=(
                    f"{row['agent_name']} retained {attempts} started provider "
                    "request(s) across the ET daily-budget boundary; unresolved "
                    "prior-day exposure requires operator review"
                ),
                run_id=str(row["run_id"]),
                mode=str(row["mode"]),
                agent_name=str(row["agent_name"]),
                attempts=attempts,
                session_cost=session_cost + float(row["reserved_cost_usd"] or 0),
                daily_cost=daily + reserved_day,
                costs_exact=False,
            )
            return

        day_row = conn.execute(
            "SELECT unknown_cost_rows, costs_exact FROM llm_budget_days WHERE day=?",
            (current_day,),
        ).fetchone()
        if day_row is None:
            raise RuntimeError(
                f"cost-circuit day accounting row is missing for {current_day}"
            )
        if int(day_row["unknown_cost_rows"] or 0) or not bool(day_row["costs_exact"]):
            raise RuntimeError(
                f"cost-circuit cannot rearm {current_day}: current-day accounting "
                "is not exact"
            )

        cross_day_holds = conn.execute(
            "SELECT * FROM llm_quota_holds WHERE active=1 AND day<>? ORDER BY id",
            (current_day,),
        ).fetchall()
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
                "exact ledger, accounting invariants, and prior-day reservation "
                "checks passed"
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
        or several abandoned reservations are charged in the same sweep.
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
        session_reserve_row = conn.execute(
            "SELECT COALESCE(SUM(reserved_cost_usd), 0) AS cost "
            "FROM llm_budget_reservations WHERE run_id=? AND status='active'",
            (run_id,),
        ).fetchone()
        day_reserve_row = conn.execute(
            "SELECT COALESCE(SUM(reserved_cost_usd), 0) AS cost "
            "FROM llm_budget_reservations WHERE day=? AND status='active'",
            (day,),
        ).fetchone()
        session_reserve = float(session_reserve_row["cost"] or 0.0)
        day_reserve = float(day_reserve_row["cost"] or 0.0)
        session_cost = float(session_row["actual_cost_usd"] or 0.0) + session_reserve
        daily_cost = float(day_row["cost"] if day_row else 0.0) + day_reserve
        costs_exact = (
            bool(state.get("costs_exact", 1))
            and bool(session_row["costs_exact"])
            and bool(day_row["costs_exact"] if day_row else 1)
            and not (session_reserve or day_reserve)
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
                "accounting and reservation checks pass"
            )
            affected = "all paid analysis for this ET budget day"
        elif scope == "mode_day":
            recovery = (
                "recovery: this mode is eligible again next ET budget day after "
                "exact accounting and reservation checks pass"
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
            "status: paid analysis is eligible again; session, retry, attempt, "
            "reservation, and daily limits remain enforced."
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
                detail=(f"{unknown_cost_rows} same-day legacy agent log row(s) "
                        "have unknown cost; daily spend cannot be bounded safely"),
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
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._seed_today(conn)
            self._expire_reservations(conn)
            self._reconcile_quota_holds_locked(conn, current_day=day)
            daily, session, _ = self._totals(conn, day, run_id)
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

    def _attempt_reserve(self, model: str, input_tokens: int, output_tokens: int) -> float | None:
        rates = PRICING.get(model)
        if not rates:
            return None
        return float(getattr(self.config, "reservation_multiplier", 1.05)) * (
            input_tokens * float(rates["input"]) / 1_000_000
            + output_tokens * float(rates["output"]) / 1_000_000
        )

    def _session_exposure_limit(self) -> float:
        return float(getattr(
            self.config,
            "session_reserved_exposure_limit_usd",
            self.config.session_cost_limit_usd,
        ))

    def _daily_exposure_limit(self) -> float:
        return float(getattr(
            self.config,
            "daily_reserved_exposure_limit_usd",
            self.config.daily_cost_limit_usd,
        ))

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
        """Atomically reserve one conservatively estimated provider request."""

        if not self.enabled:
            run_id, mode = self._context()
            return CallReservation("disabled", run_id, mode, agent_name, model, 0, max_output_tokens)
        self._raise_if_unavailable(agent_name)
        run_id, mode = self._context()
        day, _, _ = _et_day_and_utc_bounds()
        # A chars/token average is useful for dashboards but is not a safe
        # financial upper bound: dense JSON, ticker strings and Unicode can
        # tokenize much more heavily.  Every token represents at least one
        # source byte for the provider tokenizers in scope, so UTF-8 byte
        # length plus fixed message-framing headroom is conservative without
        # importing nine model-specific tokenizers into the trading process.
        input_est = max(
            1,
            len((system_prompt + user_message).encode("utf-8")) + 256,
        )
        reserve = self._attempt_reserve(model, input_est, max_output_tokens)
        reservation_id = uuid.uuid4().hex

        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._seed_today(conn)
            self._expire_reservations(conn)
            self._reconcile_quota_holds_locked(conn, current_day=day)
            state = self._effective_state_locked(
                conn, day=day, run_id=run_id, mode=mode,
            )
            daily, session, reserved_day = self._totals(conn, day, run_id)
            session_row = conn.execute(
                "SELECT provider_attempts, logical_calls, retry_attempts "
                "FROM llm_budget_sessions WHERE run_id=?",
                (run_id,),
            ).fetchone()
            attempts = int(session_row["provider_attempts"] if session_row else 0)
            session_retries = int(session_row["retry_attempts"] if session_row else 0)

            if int(state.get("suspended") or 0):
                conn.commit()
                self._notify_if_needed()
                raise PaidAnalysisSuspended(str(state.get("trigger_detail") or "circuit open"), state)

            if session_row is None:
                raise RuntimeError(
                    f"cost-circuit session accounting row is missing for run {run_id}"
                )

            # Reset clears the latch, not historical spend.  Recheck the hard
            # settled-cost ceilings in this same write transaction so neither
            # a direct caller nor an already-running job can spend through the
            # gap between reset and a later pipeline preflight.
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

            if reserve is None:
                self._trip_locked(
                    conn, code="unknown_model_price",
                    detail=f"no pinned price is available for model {model}; cost cannot be bounded",
                    run_id=run_id, mode=mode, agent_name=agent_name, attempts=attempts,
                    session_cost=session, daily_cost=daily,
                )
            else:
                active_session_reserve_row = conn.execute(
                    "SELECT COALESCE(SUM(reserved_cost_usd), 0) AS cost "
                    "FROM llm_budget_reservations WHERE run_id=? AND status='active'",
                    (run_id,),
                ).fetchone()
                active_session_reserve = float(active_session_reserve_row["cost"] or 0)
                paid_sessions_row = conn.execute(
                    "SELECT COUNT(*) AS n FROM llm_budget_sessions "
                    "WHERE day=? AND mode=? AND run_id<>? AND "
                    "(logical_calls>0 OR provider_attempts>0)",
                    (day, mode, run_id),
                ).fetchone()
                paid_sessions = int(paid_sessions_row["n"] or 0)
                current_has_attempt = attempts > 0
                max_sessions = int(self.config.max_paid_sessions_per_mode_per_day)
                max_session_retries = int(self.config.max_retry_attempts_per_session)

                if retry_kind and session_retries + 1 > max_session_retries:
                    detail = (
                        f"session retry {retry_kind!r} would be attempt "
                        f"{session_retries + 1}, above safe limit "
                        f"{max_session_retries}"
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
                                "retry_attempts": session_retries,
                                "max_retry_attempts": max_session_retries,
                                "trigger_code": "optional_retry_budget_exhausted",
                            },
                        )
                    self._trip_locked(
                        conn, code="session_retry_attempt_limit",
                        detail=detail,
                        run_id=run_id, mode=mode, agent_name=agent_name,
                        attempts=attempts, session_cost=session, daily_cost=daily,
                        costs_exact=False,
                    )
                elif not current_has_attempt and paid_sessions >= max_sessions:
                    self._trip_locked(
                        conn, code="session_retry_limit",
                        detail=(f"{mode} paid session attempt {paid_sessions + 1} exceeds "
                                f"daily safe limit {max_sessions}"),
                        run_id=run_id, mode=mode, agent_name=agent_name, attempts=attempts,
                        session_cost=session, daily_cost=daily,
                        costs_exact=False,
                    )
                elif session + active_session_reserve + reserve > self._session_exposure_limit():
                    projected = session + active_session_reserve + reserve
                    self._trip_locked(
                        conn, code="projected_session_cost_limit",
                        detail=(f"next {agent_name} call would project session cost to "
                                f"${projected:.4f}, above reserved-exposure ceiling "
                                f"${self._session_exposure_limit():.2f}"),
                        run_id=run_id, mode=mode, agent_name=agent_name, attempts=attempts,
                        session_cost=session + active_session_reserve,
                        daily_cost=daily + reserved_day,
                        costs_exact=False,
                    )
                elif daily + reserved_day + reserve > self._daily_exposure_limit():
                    projected = daily + reserved_day + reserve
                    self._trip_locked(
                        conn, code="projected_daily_cost_limit",
                        detail=(f"next {agent_name} call would project daily cost to "
                                f"${projected:.4f}, above reserved-exposure ceiling "
                                f"${self._daily_exposure_limit():.2f}"),
                        run_id=run_id, mode=mode, agent_name=agent_name, attempts=attempts,
                        session_cost=session + active_session_reserve,
                        daily_cost=daily + reserved_day,
                        costs_exact=False,
                    )

            state = self._effective_state_locked(
                conn, day=day, run_id=run_id, mode=mode,
            )
            if int(state.get("suspended") or 0):
                conn.commit()
                self._notify_if_needed()
                raise PaidAnalysisSuspended(str(state.get("trigger_detail") or "circuit open"), state)

            expires_min = int(getattr(self.config, "reservation_ttl_minutes", 30))
            conn.execute(
                "INSERT INTO llm_budget_reservations "
                "(reservation_id, run_id, day, mode, agent_name, model, "
                "input_tokens_estimate, max_output_tokens, reserved_cost_usd, expires_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now', ?))",
                (
                    reservation_id, run_id, day, mode, agent_name, model,
                    input_est, max_output_tokens, reserve, f"+{expires_min} minutes",
                ),
            )
            updated_session = conn.execute(
                "UPDATE llm_budget_sessions SET logical_calls=logical_calls+1, "
                "retry_attempts=retry_attempts+?, updated_at=datetime('now') "
                "WHERE run_id=?", (1 if retry_kind else 0, run_id)
            )
            if updated_session.rowcount != 1:
                raise RuntimeError(
                    f"cost-circuit session row disappeared while reserving call for {run_id}"
                )
            conn.commit()
        return CallReservation(
            reservation_id, run_id, mode, agent_name, model, input_est, max_output_tokens
        )

    def before_provider_attempt(self, reservation: CallReservation, *, model: str) -> int:
        """Authorize an actual request and return this call's attempt number."""

        if not self.enabled or reservation.reservation_id == "disabled":
            return 1
        self._raise_if_unavailable(reservation.agent_name)
        current_day, _, _ = _et_day_and_utc_bounds()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            # A reservation can wait behind the provider semaphore while a
            # different process settles spend or accounting is damaged.  The
            # provider authorization boundary must therefore re-seed/validate
            # the complete current-day ledger in the same write transaction;
            # trusting only the reservation created earlier would leave a
            # fail-open window immediately before network I/O.
            self._seed_today(conn)
            self._expire_reservations(conn)
            self._reconcile_quota_holds_locked(conn, current_day=current_day)
            state = self._effective_state_locked(
                conn,
                day=current_day,
                run_id=reservation.run_id,
                mode=reservation.mode,
            )
            row = conn.execute(
                "SELECT * FROM llm_budget_reservations WHERE reservation_id=?",
                (reservation.reservation_id,),
            ).fetchone()
            if row is None:
                raise RuntimeError(
                    f"cost-circuit reservation {reservation.reservation_id} is missing"
                )
            if row["status"] != "active":
                conn.commit()
                self._notify_if_needed()
                if row["status"] == "expired_cross_day_unattempted":
                    raise PaidAnalysisSuspended(
                        "call reservation crossed the ET daily-budget boundary; "
                        "a new current-day reservation is required",
                        state,
                    )
                raise PaidAnalysisSuspended("call reservation expired or is unavailable", state)
            day = str(row["day"])
            current_attempts = int(row["attempt_count"] or 0)
            next_attempt = current_attempts + 1
            session = conn.execute(
                "SELECT * FROM llm_budget_sessions WHERE run_id=?", (reservation.run_id,)
            ).fetchone()
            if session is None:
                raise RuntimeError(
                    "cost-circuit session accounting row is missing for run "
                    f"{reservation.run_id}"
                )
            session_attempts = int(session["provider_attempts"] or 0)
            session_retries = int(session["retry_attempts"] or 0)
            daily, session_cost, reserved_day = self._totals(conn, day, reservation.run_id)
            if day != current_day:
                if current_attempts == 0:
                    released = conn.execute(
                        "UPDATE llm_budget_reservations SET "
                        "status='expired_cross_day_unattempted', reserved_cost_usd=0, "
                        "actual_cost_usd=0, completed_at=datetime('now') "
                        "WHERE reservation_id=? AND status='active'",
                        (reservation.reservation_id,),
                    )
                    if released.rowcount != 1:
                        raise RuntimeError(
                            f"cross-day reservation {reservation.reservation_id} "
                            "could not be released"
                        )
                    conn.commit()
                    raise PaidAnalysisSuspended(
                        "call reservation crossed the ET daily-budget boundary; "
                        "a new current-day reservation is required",
                        state,
                    )
                self._trip_locked(
                    conn,
                    code="cross_day_started_reservation",
                    detail=(
                        f"{reservation.agent_name} retry crossed the ET daily-budget "
                        "boundary after a provider attempt; unresolved prior-day "
                        "exposure must be reconciled before paid analysis continues"
                    ),
                    run_id=reservation.run_id,
                    mode=reservation.mode,
                    agent_name=reservation.agent_name,
                    attempts=session_attempts,
                    session_cost=(
                        session_cost + float(row["reserved_cost_usd"] or 0.0)
                    ),
                    daily_cost=daily + reserved_day,
                    costs_exact=False,
                )
                state = self._effective_state_locked(
                    conn,
                    day=current_day,
                    run_id=reservation.run_id,
                    mode=reservation.mode,
                )
                conn.commit()
                self._notify_if_needed()
                raise PaidAnalysisSuspended(
                    str(state.get("trigger_detail") or "circuit open"), state
                )
            # If a request has already started but returned no usage yet, its
            # conservative reserve is the only honest cost exposure available.
            # Include those attempted reservations in any trip snapshot so
            # the immediate Telegram alert does not claim $0 after two paid
            # requests merely because both streams failed before telemetry.
            attempted_session_row = conn.execute(
                "SELECT COALESCE(SUM(reserved_cost_usd), 0) AS cost "
                "FROM llm_budget_reservations WHERE run_id=? AND status='active' "
                "AND attempt_count>0",
                (reservation.run_id,),
            ).fetchone()
            attempted_day_row = conn.execute(
                "SELECT COALESCE(SUM(reserved_cost_usd), 0) AS cost "
                "FROM llm_budget_reservations WHERE day=? AND status='active' "
                "AND attempt_count>0",
                (day,),
            ).fetchone()
            exposed_session_cost = session_cost + float(attempted_session_row["cost"] or 0)
            exposed_daily_cost = daily + float(attempted_day_row["cost"] or 0)

            if int(state.get("suspended") or 0):
                conn.commit()
                self._notify_if_needed()
                raise PaidAnalysisSuspended(str(state.get("trigger_detail") or "circuit open"), state)

            # A reservation may outlive an operator reset or wait while another
            # request settles.  Enforce hard settled caps again immediately at
            # the network boundary; exposure ceilings below are a separate,
            # forward-looking guard.
            self._enforce_settled_limits_locked(
                conn, day=day, run_id=reservation.run_id,
                mode=reservation.mode, agent_name=reservation.agent_name,
                attempts=session_attempts, attempts_exact=True,
                daily=daily, session=session_cost,
            )
            state = self._effective_state_locked(
                conn,
                day=current_day,
                run_id=reservation.run_id,
                mode=reservation.mode,
            )
            if int(state.get("suspended") or 0):
                conn.commit()
                self._notify_if_needed()
                raise PaidAnalysisSuspended(
                    str(state.get("trigger_detail") or "circuit open"), state
                )

            max_per_call = int(self.config.max_provider_attempts_per_call)
            is_retry = current_attempts >= 1
            pending_retry_reserve = 0.0
            if next_attempt > max_per_call:
                self._trip_locked(
                    conn, code="provider_attempt_limit",
                    detail=(f"{reservation.agent_name} provider attempt {next_attempt} "
                            f"exceeds per-call safe limit {max_per_call}"),
                    run_id=reservation.run_id, mode=reservation.mode,
                    agent_name=reservation.agent_name, attempts=session_attempts,
                    session_cost=exposed_session_cost, daily_cost=exposed_daily_cost,
                    costs_exact=False,
                )
            elif is_retry and session_retries + 1 > int(self.config.max_retry_attempts_per_session):
                self._trip_locked(
                    conn, code="session_retry_attempt_limit",
                    detail=(f"session retry attempt {session_retries + 1} exceeds safe limit "
                            f"{int(self.config.max_retry_attempts_per_session)}"),
                    run_id=reservation.run_id, mode=reservation.mode,
                    agent_name=reservation.agent_name, attempts=session_attempts,
                    session_cost=exposed_session_cost, daily_cost=exposed_daily_cost,
                    costs_exact=False,
                )
            elif is_retry:
                extra = self._attempt_reserve(
                    model, int(row["input_tokens_estimate"]), int(row["max_output_tokens"])
                )
                if extra is None:
                    self._trip_locked(
                        conn, code="unknown_model_price",
                        detail=f"no pinned price is available for retry/failover model {model}",
                        run_id=reservation.run_id, mode=reservation.mode,
                        agent_name=reservation.agent_name, attempts=session_attempts,
                        session_cost=exposed_session_cost, daily_cost=exposed_daily_cost,
                        costs_exact=False,
                    )
                else:
                    pending_retry_reserve = extra

            # Revalidate aggregate exposure for *every* attempt at the actual
            # network boundary.  A reservation can wait behind a provider
            # semaphore while an earlier call settles above its estimate; an
            # old reservation is not a blank cheque to spend past the cap.
            state = self._effective_state_locked(
                conn,
                day=current_day,
                run_id=reservation.run_id,
                mode=reservation.mode,
            )
            if not int(state.get("suspended") or 0):
                session_reserved_row = conn.execute(
                    "SELECT COALESCE(SUM(reserved_cost_usd), 0) AS cost "
                    "FROM llm_budget_reservations WHERE run_id=? AND status='active'",
                    (reservation.run_id,),
                ).fetchone()
                session_reserved = float(session_reserved_row["cost"] or 0.0)
                reserved_day_row = conn.execute(
                    "SELECT COALESCE(SUM(reserved_cost_usd), 0) AS cost "
                    "FROM llm_budget_reservations WHERE day=? AND status='active'",
                    (day,),
                ).fetchone()
                current_reserved_day = float(reserved_day_row["cost"] or 0.0)
                projected_session = session_cost + session_reserved + pending_retry_reserve
                projected_daily = daily + current_reserved_day + pending_retry_reserve
                if projected_session > self._session_exposure_limit():
                    self._trip_locked(
                        conn, code="provider_projected_session_cost_limit",
                        detail=(f"{reservation.agent_name} request would expose session cost "
                                f"of ${projected_session:.4f}, above "
                                f"reserved-exposure ceiling "
                                f"${self._session_exposure_limit():.2f}"),
                        run_id=reservation.run_id, mode=reservation.mode,
                        agent_name=reservation.agent_name, attempts=session_attempts,
                        session_cost=projected_session, daily_cost=projected_daily,
                        costs_exact=False,
                    )
                elif projected_daily > self._daily_exposure_limit():
                    self._trip_locked(
                        conn, code="provider_projected_daily_cost_limit",
                        detail=(f"{reservation.agent_name} request would expose daily cost "
                                f"of ${projected_daily:.4f}, above "
                                f"reserved-exposure ceiling "
                                f"${self._daily_exposure_limit():.2f}"),
                        run_id=reservation.run_id, mode=reservation.mode,
                        agent_name=reservation.agent_name, attempts=session_attempts,
                        session_cost=projected_session, daily_cost=projected_daily,
                        costs_exact=False,
                    )

            state = self._effective_state_locked(
                conn,
                day=current_day,
                run_id=reservation.run_id,
                mode=reservation.mode,
            )
            if not int(state.get("suspended") or 0) and pending_retry_reserve:
                updated_retry = conn.execute(
                    "UPDATE llm_budget_reservations "
                    "SET reserved_cost_usd=reserved_cost_usd+? "
                    "WHERE reservation_id=? AND status='active'",
                    (pending_retry_reserve, reservation.reservation_id),
                )
                if updated_retry.rowcount != 1:
                    raise RuntimeError(
                        f"cost-circuit reservation {reservation.reservation_id} "
                        "disappeared while reserving retry"
                    )

            state = self._effective_state_locked(
                conn,
                day=current_day,
                run_id=reservation.run_id,
                mode=reservation.mode,
            )
            if int(state.get("suspended") or 0):
                conn.commit()
                self._notify_if_needed()
                raise PaidAnalysisSuspended(str(state.get("trigger_detail") or "circuit open"), state)

            updated_reservation = conn.execute(
                "UPDATE llm_budget_reservations SET attempt_count=? WHERE reservation_id=?",
                (next_attempt, reservation.reservation_id),
            )
            if updated_reservation.rowcount != 1:
                raise RuntimeError(
                    f"cost-circuit reservation {reservation.reservation_id} disappeared"
                )
            updated_session = conn.execute(
                "UPDATE llm_budget_sessions SET provider_attempts=provider_attempts+1, "
                "retry_attempts=retry_attempts+?, updated_at=datetime('now') WHERE run_id=?",
                (1 if is_retry else 0, reservation.run_id),
            )
            if updated_session.rowcount != 1:
                raise RuntimeError(
                    f"cost-circuit session {reservation.run_id} disappeared at authorization"
                )
            conn.commit()
        return next_attempt

    def complete_call(
        self,
        reservation: CallReservation,
        actual_cost_usd: float | None,
        *,
        actual_model: str | None = None,
    ) -> None:
        """Release a reservation and account the completed provider response."""

        if not self.enabled or reservation.reservation_id == "disabled":
            return
        # Another process can persist the emergency sidecar while this
        # request is in flight.  Observe it before releasing an unaccounted
        # response into the decision pipeline; the reservation remains as
        # conservative exposure for later reconciliation/expiry.
        self._raise_if_unavailable(reservation.agent_name)
        with self._infrastructure_lock:
            sentinel = self._unavailable_sentinel
        if sentinel is not None:
            # Never let an unaccounted paid response flow into the decision
            # pipeline after another thread/process has declared accounting
            # unavailable.  Its active reservation remains as conservative
            # exposure until it can be reconciled or expires.
            sentinel.require_paid_analysis(reservation.agent_name)
        day, _, _ = _et_day_and_utc_bounds()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM llm_budget_reservations WHERE reservation_id=?",
                (reservation.reservation_id,),
            ).fetchone()
            if row is None:
                raise RuntimeError(
                    f"cost-circuit reservation {reservation.reservation_id} is missing at completion"
                )
            if row["status"] != "active":
                state = self._effective_state_locked(
                    conn,
                    day=day,
                    run_id=reservation.run_id,
                    mode=reservation.mode,
                )
                conn.commit()
                self._notify_if_needed()
                raise PaidAnalysisSuspended(
                    "call reservation expired or was already reconciled", state
                )
            day = str(row["day"])
            # Unknown provider usage is accounted at the conservative reserve
            # and latches the circuit: continuing would make the budget fiction.
            # If this logical call retried, provider usage for the failed
            # attempts is unavailable. Retain their worst-case reservations
            # instead of pretending retries were free.
            unknown = actual_cost_usd is None
            total_reserved = float(row["reserved_cost_usd"] or 0)
            current_attempt_reserve = self._attempt_reserve(
                actual_model or reservation.model,
                int(row["input_tokens_estimate"]),
                int(row["max_output_tokens"]),
            ) or 0.0
            failed_attempt_reserve = max(0.0, total_reserved - current_attempt_reserve)
            accounted = total_reserved if unknown else float(actual_cost_usd) + failed_attempt_reserve
            call_cost_exact = not unknown and not bool(failed_attempt_reserve)
            updated_reservation = conn.execute(
                "UPDATE llm_budget_reservations SET status='complete', actual_cost_usd=?, "
                "reserved_cost_usd=0, completed_at=datetime('now') WHERE reservation_id=?",
                (accounted, reservation.reservation_id),
            )
            if updated_reservation.rowcount != 1:
                raise RuntimeError(
                    f"cost-circuit reservation {reservation.reservation_id} "
                    "could not be completed exactly once"
                )
            updated_session = conn.execute(
                "UPDATE llm_budget_sessions SET actual_cost_usd=actual_cost_usd+?, "
                "costs_exact=CASE WHEN ? THEN costs_exact ELSE 0 END, "
                "updated_at=datetime('now') WHERE run_id=?",
                (accounted, int(call_cost_exact), reservation.run_id),
            )
            if updated_session.rowcount != 1:
                raise RuntimeError(
                    f"cost-circuit session {reservation.run_id} is missing at completion"
                )
            updated_day = conn.execute(
                "UPDATE llm_budget_days SET incremental_cost_usd=incremental_cost_usd+?, "
                "costs_exact=CASE WHEN ? THEN costs_exact ELSE 0 END, "
                "updated_at=datetime('now') WHERE day=?",
                (accounted, int(call_cost_exact), day),
            )
            if updated_day.rowcount != 1:
                raise RuntimeError(
                    f"cost-circuit day {day} is missing at completion"
                )
            daily, session_cost, _ = self._totals(conn, day, reservation.run_id)
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
            elif session_cost >= float(self.config.session_cost_limit_usd):
                self._trip_locked(
                    conn, code="session_cost_limit",
                    detail=(f"session LLM spend ${session_cost:.4f} reached safe limit "
                            f"${float(self.config.session_cost_limit_usd):.2f}"),
                    run_id=reservation.run_id, mode=reservation.mode,
                    agent_name=reservation.agent_name, attempts=attempts,
                    session_cost=session_cost, daily_cost=daily,
                    costs_exact=not bool(failed_attempt_reserve),
                )
            elif daily >= float(self.config.daily_cost_limit_usd):
                self._trip_locked(
                    conn, code="daily_cost_limit",
                    detail=(f"daily LLM spend ${daily:.4f} reached safe limit "
                            f"${float(self.config.daily_cost_limit_usd):.2f}"),
                    run_id=reservation.run_id, mode=reservation.mode,
                    agent_name=reservation.agent_name, attempts=attempts,
                    session_cost=session_cost, daily_cost=daily,
                    costs_exact=not bool(failed_attempt_reserve),
                )
            else:
                active_session_row = conn.execute(
                    "SELECT COALESCE(SUM(reserved_cost_usd), 0) AS cost "
                    "FROM llm_budget_reservations WHERE run_id=? AND status='active'",
                    (reservation.run_id,),
                ).fetchone()
                active_day_row = conn.execute(
                    "SELECT COALESCE(SUM(reserved_cost_usd), 0) AS cost "
                    "FROM llm_budget_reservations WHERE day=? AND status='active'",
                    (day,),
                ).fetchone()
                projected_session = session_cost + float(active_session_row["cost"] or 0)
                projected_daily = daily + float(active_day_row["cost"] or 0)
                if projected_session > self._session_exposure_limit():
                    self._trip_locked(
                        conn, code="outstanding_projected_session_cost_limit",
                        detail=(f"settled {reservation.agent_name} call plus outstanding "
                                f"requests expose session cost of ${projected_session:.4f}, "
                                f"above reserved-exposure ceiling "
                                f"${self._session_exposure_limit():.2f}"),
                        run_id=reservation.run_id, mode=reservation.mode,
                        agent_name=reservation.agent_name, attempts=attempts,
                        session_cost=projected_session, daily_cost=projected_daily,
                        costs_exact=False,
                    )
                elif projected_daily > self._daily_exposure_limit():
                    self._trip_locked(
                        conn, code="outstanding_projected_daily_cost_limit",
                        detail=(f"settled {reservation.agent_name} call plus outstanding "
                                f"requests expose daily cost of ${projected_daily:.4f}, "
                                f"above reserved-exposure ceiling "
                                f"${self._daily_exposure_limit():.2f}"),
                        run_id=reservation.run_id, mode=reservation.mode,
                        agent_name=reservation.agent_name, attempts=attempts,
                        session_cost=projected_session, daily_cost=projected_daily,
                        costs_exact=False,
                    )
            self._refresh_latched_snapshot_locked(conn)
            conn.commit()
        self._notify_if_needed()

    def fail_call(self, reservation: CallReservation, error: BaseException) -> None:
        """Conservatively account an unfinished paid request and fail closed.

        A transport/provider exception does not prove that the provider billed
        nothing: the prompt may already have been accepted and a streamed
        response may have been cut off before usage telemetry arrived.  Moving
        the reservation into spend prevents a failed request from silently
        restoring budget.  The exact amount is unknowable, so the global latch
        opens after accounting the conservative reserved exposure.
        """

        if not self.enabled or reservation.reservation_id == "disabled":
            return
        self._sync_emergency_latch()
        with self._infrastructure_lock:
            if self._unavailable_sentinel is not None:
                return
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM llm_budget_reservations WHERE reservation_id=?",
                (reservation.reservation_id,),
            ).fetchone()
            if row is None:
                raise RuntimeError(
                    f"cost-circuit reservation {reservation.reservation_id} is missing at failure"
                )
            if row["status"] != "active":
                conn.commit()
                return
            day = str(row["day"])
            attempted = int(row["attempt_count"] or 0) > 0
            accounted = float(row["reserved_cost_usd"] or 0) if attempted else 0.0
            updated_reservation = conn.execute(
                "UPDATE llm_budget_reservations SET status='failed', actual_cost_usd=?, "
                "reserved_cost_usd=0, completed_at=datetime('now') "
                "WHERE reservation_id=? AND status='active'",
                (accounted, reservation.reservation_id),
            )
            if updated_reservation.rowcount != 1:
                raise RuntimeError(
                    f"cost-circuit reservation {reservation.reservation_id} "
                    "could not be failed exactly once"
                )
            updated_session = conn.execute(
                "UPDATE llm_budget_sessions SET status='call_failed', "
                "actual_cost_usd=actual_cost_usd+?, "
                "costs_exact=CASE WHEN ? THEN 0 ELSE costs_exact END, "
                "updated_at=datetime('now') WHERE run_id=?",
                (accounted, int(attempted), reservation.run_id),
            )
            if updated_session.rowcount != 1:
                raise RuntimeError(
                    f"cost-circuit session {reservation.run_id} is missing at failure"
                )
            if accounted:
                updated_day = conn.execute(
                    "UPDATE llm_budget_days SET "
                    "incremental_cost_usd=incremental_cost_usd+?, "
                    "costs_exact=0, updated_at=datetime('now') WHERE day=?",
                    (accounted, day),
                )
                if updated_day.rowcount != 1:
                    raise RuntimeError(
                        f"cost-circuit day {day} is missing at failure"
                    )
            daily, session_cost, _ = self._totals(conn, day, reservation.run_id)
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
            if accounted:
                self._trip_locked(
                    conn,
                    code="failed_call_unknown_cost",
                    detail=(
                        f"{reservation.agent_name} failed after {attempts} provider "
                        "attempt(s) without final usage telemetry; reserved exposure "
                        f"${accounted:.4f} was charged to the safety budget"
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
            self._expire_reservations(conn)
            self._reconcile_quota_holds_locked(conn, current_day=day)
            state = self._effective_state_locked(
                conn, day=day, run_id=run_id, mode=mode,
            )
            daily, session, reserved = self._totals(conn, day, run_id)
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
                active_reserved_cost_usd=reserved,
                logical_calls=int(session_row["logical_calls"] if session_row else 0),
                provider_attempts=int(session_row["provider_attempts"] if session_row else 0),
                retry_attempts=int(session_row["retry_attempts"] if session_row else 0),
            )
        self._notify_if_needed()
        return result

    def reset(self, reason: str) -> None:
        """Operator-only manual reset. A reason is mandatory and audited."""

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
                if not int(state.get("suspended") or 0) and not emergency_latched:
                    raise ValueError(
                        "no operator-resettable hard circuit is active; scoped quota "
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
        pricing_ok = refresh_openrouter_pricing()
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
