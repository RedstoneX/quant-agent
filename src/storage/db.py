import logging
import sqlite3
import threading
from datetime import date, datetime, time, timedelta

from src.util.time import ET, UTC, et_today

logger = logging.getLogger(__name__)


def _is_filled_trail_stop(row, action: str) -> bool:
    """True for a TRAIL_STOP row the broker actually EXECUTED.

    A TRAIL_STOP row is written fill_status='submitted' at placement and only
    flipped to 'filled' by _reconcile_fills when the broker reports a fill, so
    the status is what distinguishes "protection sitting there" from "the stop
    sold our shares". Mirrors the same distinction in
    pipeline._build_post_exit_reality. Legacy rows can carry a NULL
    fill_status; those only count when a real fill_qty was recorded, so a
    never-filled stop can't book a phantom exit at its stop price.
    """
    if action != "TRAIL_STOP":
        return False
    try:
        status = (row["fill_status"] or "").lower()
    except (KeyError, IndexError, TypeError):
        status = ""
    if status == "filled":
        return True
    try:
        return status == "" and float(row["fill_qty"] or 0) > 0
    except (KeyError, IndexError, TypeError, ValueError):
        return False


class Database:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self.conn: sqlite3.Connection | None = None
        self._lock = threading.Lock()

    def initialize(self):
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        # WAL allows concurrent readers alongside the writer — avoids occasional
        # "database is locked" when parallel agent threads each insert logs.
        # No-op for :memory: databases (stays "memory" journal).
        try:
            self.conn.execute("PRAGMA journal_mode=WAL")
        except sqlite3.DatabaseError:
            pass
        # synchronous=NORMAL is the trading-appropriate fsync mode under
        # WAL: WAL file is synced on every commit, main DB is synced at
        # checkpoint. SQLite default (FULL) syncs both on every commit
        # which is overkill for our workload (15-25 trades / day; agent
        # logs are best-effort observability — losing the last few rows
        # on a hard power loss would be acceptable). NORMAL also reduces
        # commit latency that becomes noticeable during evening's
        # multi-write transaction. Safe under WAL because corruption
        # requires both a hard power loss AND a torn write to the WAL
        # itself (extremely rare).
        try:
            self.conn.execute("PRAGMA synchronous=NORMAL")
        except sqlite3.DatabaseError:
            pass
        # busy_timeout — the default is 0 (raise OperationalError instantly
        # on any lock contention). At 09:30 ET, the morning session and
        # intra_check fire simultaneously; intra_check is exempt from the
        # bash-level session lock (CLAUDE.md "Cross-mode session lock" —
        # intra is the flash-crash circuit breaker and must run every tick).
        # Both Python processes contend at the SQLite WAL level. The
        # threading.Lock above serializes within a single process but does
        # nothing across processes. A 5000ms wait window covers the
        # observed worst-case WAL→checkpoint stall (~1-2s on a busy day)
        # plus headroom. Set BEFORE _create_tables so the CREATE statements
        # also benefit if a concurrent reader is active during first init.
        try:
            self.conn.execute("PRAGMA busy_timeout=5000")
        except sqlite3.DatabaseError:
            pass
        self._create_tables()

    def _locked_write(self, do, *, label: str = "write"):
        """Run a write closure under the process lock with bounded retry on
        cross-process SQLite lock contention.

        busy_timeout (5s) only covers the lock-WAIT; a WAL checkpoint stall
        longer than that surfaces as `OperationalError: database is locked`
        AFTER the wait expires. The bare execute path would then either raise
        (trade / recovery-queue inserts) or silently lose the row
        (agent_logs). intra_check is explicitly exempt from the cross-mode
        session lock (CLAUDE.md), so it WILL write concurrently with a long
        morning — the in-process threading.Lock serializes only within THIS
        process; this retry is what protects the write across processes.

        ~1.55s of extra backoff on top of the 5s busy_timeout; if still
        locked after that, re-raise (a stuck DB is a real problem worth
        surfacing, not silently dropping).
        """
        import time as _time
        last_exc: sqlite3.OperationalError | None = None
        for attempt in range(5):
            try:
                with self._lock:
                    return do()
            except sqlite3.OperationalError as exc:
                msg = str(exc).lower()
                if "locked" not in msg and "busy" not in msg:
                    raise
                last_exc = exc
                logger.warning(
                    "DB %s contended (attempt %d/5): %s — retrying",
                    label, attempt + 1, exc,
                )
                _time.sleep(0.05 * (2 ** attempt))  # 0.05,0.1,0.2,0.4,0.8s
        logger.error("DB %s still locked after retries — giving up: %s", label, last_exc)
        raise last_exc

    def _create_tables(self):
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                action TEXT NOT NULL,
                qty REAL NOT NULL,
                price REAL NOT NULL,
                reasoning TEXT,
                run_id TEXT,
                broker_order_id TEXT,
                fill_status TEXT,                      -- submitted | filled | canceled | rejected | expired | done_for_day | NULL(legacy)
                fill_qty REAL,                         -- actual qty filled (may differ from requested)
                fill_price REAL,                       -- actual avg fill price
                realized_pnl REAL,                     -- average-cost realized P&L for confirmed exits
                fill_reconciled_at TEXT,               -- when we confirmed the terminal status
                timestamp TEXT NOT NULL DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS positions (
                symbol TEXT PRIMARY KEY,
                qty REAL NOT NULL,
                avg_entry REAL NOT NULL,
                current_price REAL NOT NULL,
                market_value REAL NOT NULL,
                unrealized_pnl REAL NOT NULL,
                sector TEXT,
                updated_at TEXT NOT NULL DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS agent_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                agent_name TEXT NOT NULL,
                run_id TEXT NOT NULL,
                input_summary TEXT,
                input_message TEXT,
                output_summary TEXT,
                full_response TEXT,
                model TEXT,
                tokens_used INTEGER,
                -- Per-call cost tracking (added 2026-05-13). NULL when the
                -- agent's model isn't in src.cost_table.PRICING or when
                -- the SDK didn't return usage data. tokens_used is kept
                -- for backward-compat readers; the input/output split is
                -- the authoritative source for cost recomputation if
                -- pricing changes after-the-fact.
                input_tokens INTEGER,
                output_tokens INTEGER,
                cost_usd REAL,
                -- Actual provider HTTP requests represented by this logical
                -- row. Tech chunk aggregation can be >1; legacy rows are
                -- NULL and readers conservatively count them as one.
                provider_requests INTEGER,
                timestamp TEXT NOT NULL DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS daily_pnl (
                date TEXT PRIMARY KEY,
                total_value REAL NOT NULL,
                daily_pnl REAL NOT NULL,
                daily_return_pct REAL NOT NULL,
                equity_close REAL,
                timestamp TEXT NOT NULL DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS insights (
                date TEXT PRIMARY KEY,
                tomorrow_outlook TEXT,
                lessons TEXT,
                suggested_actions TEXT,
                risk_rating TEXT,
                tomorrow_bias TEXT DEFAULT 'neutral',
                tomorrow_conviction TEXT DEFAULT 'medium',
                tomorrow_key_risks TEXT DEFAULT '[]',
                sell_decisions_assessment TEXT DEFAULT '',
                sell_grades_json TEXT DEFAULT '[]',
                buy_grades_json TEXT DEFAULT '[]',
                missed_opportunities_json TEXT DEFAULT '[]',
                timestamp TEXT NOT NULL DEFAULT (datetime('now'))
            );

            -- Stage 4 (QAMC): additive, non-authoritative persistence of the
            -- already-VALIDATED structured evidence each specialist/decision
            -- agent produces (never raw LLM prose — see docs/architecture/
            -- MISSION_CONTROL_API.md). Lets Mission Control show per-candidate
            -- fidelity without the client ever re-parsing agent_logs.full_response.
            -- Purely a forensic display cache: losing this table has zero
            -- effect on trading (nothing here is read by the trading pipeline).
            CREATE TABLE IF NOT EXISTS specialist_evidence (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL,
                decision_id TEXT,               -- set only for PM/RM evidence rows
                agent_name TEXT NOT NULL,       -- macro_analyst | news_analyst | tech_analyst
                                                 -- | earnings_analyst | portfolio_manager | risk_manager
                kind TEXT NOT NULL,             -- analysis | reasoning | target | proposed_order
                                                 -- | verdict | modification
                scope TEXT NOT NULL,            -- 'run' (broader/non-symbol-specific) | 'symbol'
                symbol TEXT,                    -- NULL for scope='run'
                evidence_json TEXT NOT NULL,    -- model_dump_json() of the validated Pydantic object
                timestamp TEXT NOT NULL DEFAULT (datetime('now'))
            );

            -- Orphaned protective stops awaiting follow-up restore.
            -- Written by _finalize_protection_after_sell when the lingering
            -- SELL couldn't be cancelled cleanly (or didn't reach terminal
            -- after cancel). Drained at the start of every session: each
            -- row's sell_order_id is re-queried; if now terminal, we
            -- finalize protection from the persisted specs and delete the
            -- row. Without persistence, the bail branches' "next session
            -- reconcile rebuilds coverage" promise was a lie — _reconcile_fills
            -- only updates fill columns, not stop coverage.
            CREATE TABLE IF NOT EXISTS pending_protection_restores (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                sell_order_id TEXT NOT NULL,
                position_qty_before_sell REAL NOT NULL,
                specs_json TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                run_id TEXT
            );

            -- Explicit intraday evaluation ledger. A candidate is recorded
            -- before paid analysis, so HOLD/RM-reject/parse-failure outcomes
            -- still enforce cooldown even though no trades row exists.
            CREATE TABLE IF NOT EXISTS intraday_evaluations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                run_id TEXT NOT NULL,
                status TEXT NOT NULL,
                detail TEXT,
                timestamp TEXT NOT NULL DEFAULT (datetime('now')),
                UNIQUE(symbol, run_id)
            );
            CREATE INDEX IF NOT EXISTS idx_intraday_evaluations_symbol_time
                ON intraday_evaluations(symbol, timestamp);
        """)
        self.conn.commit()
        self._migrate()
        # The cost circuit also initializes itself independently because it
        # must work before the main Database object exists. Creating the same
        # additive schema here makes read-only API health available after any
        # normal DB initialization and keeps migrations explicit.
        from src.cost_circuit import ensure_cost_circuit_schema
        try:
            ensure_cost_circuit_schema(self.conn)
            self.conn.commit()
        except Exception:
            # Cost-accounting corruption must suspend paid analysis, but must
            # not prevent construction of the main DB used by broker stop/fill
            # reconciliation and deterministic loss protection.  The breaker
            # independently retries initialization and persists its emergency
            # fail-closed marker.
            self.conn.rollback()
            logger.critical(
                "Cost-circuit schema initialization failed; paid analysis will "
                "fail closed while non-LLM safety remains available",
                exc_info=True,
            )

    def _migrate(self):
        """Add columns that may be missing in older databases.

        Each ALTER is independent and wrapped in try/except so one partial
        migration (e.g., stop_loss added but take_profit ALTER crashed on the
        prior run) can still be recovered by the next startup. The old pattern
        of bundling both ALTERs under a single 'if stop_loss not in columns'
        guard would permanently skip take_profit if it wasn't added together.
        """
        import logging as _logging
        _log = _logging.getLogger(__name__)

        def _ensure_column(table: str, column: str, ddl: str) -> None:
            try:
                cursor = self.conn.execute(f"PRAGMA table_info({table})")
                existing = {row[1] for row in cursor.fetchall()}
                if column in existing:
                    return
                self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")
                self.conn.commit()
                _log.info("Schema migration: added %s.%s", table, column)
            except Exception as e:
                # Don't bring down initialization on a migration hiccup — the
                # table is still usable with the old schema, just missing this
                # one column. Caller will see reduced functionality, not a crash.
                _log.error("Schema migration failed for %s.%s: %s", table, column, e)

        _ensure_column("agent_logs", "input_message", "input_message TEXT DEFAULT ''")
        # Today's official regular-session (4pm) close equity, captured from
        # Alpaca portfolio_history — enables true close-to-close evening P&L
        # instead of the close-to-8pm-AH broker diff. NULL for legacy rows.
        _ensure_column("daily_pnl", "equity_close", "equity_close REAL")
        _ensure_column("trades", "stop_loss", "stop_loss REAL DEFAULT 0")
        _ensure_column("trades", "take_profit", "take_profit REAL DEFAULT 0")
        # Phase 3.1 — the thesis horizon and setup type PINNED AT ENTRY.
        # `pace` used to be measured against `avg_hold_days` from the system's
        # OWN rolling 30-day realized-trade calibration (~2.0 days), so selling
        # quickly shrank the average, which made every remaining position look
        # stalled, which drove more selling. A self-tightening noose. The
        # horizon must come from the analyst's stated thesis at entry and never
        # be recomputed. NULL on legacy rows — those positions get no pace
        # figure at all rather than a fabricated one.
        _ensure_column(
            "trades", "expected_horizon_sessions", "expected_horizon_sessions INTEGER",
        )
        _ensure_column("trades", "setup_type", "setup_type TEXT")
        _ensure_column("insights", "tomorrow_bias", "tomorrow_bias TEXT DEFAULT 'neutral'")
        _ensure_column("insights", "tomorrow_conviction", "tomorrow_conviction TEXT DEFAULT 'medium'")
        _ensure_column("insights", "tomorrow_key_risks", "tomorrow_key_risks TEXT DEFAULT '[]'")
        _ensure_column("insights", "sell_decisions_assessment", "sell_decisions_assessment TEXT DEFAULT ''")
        # Phase 3: fill reconciliation — tells memory readers which 'trades'
        # rows actually executed vs which were just submitted. Legacy rows
        # default to NULL and are treated as 'filled' by the calibration
        # query (backward compat — those predate the reconciliation path).
        _ensure_column("trades", "broker_order_id", "broker_order_id TEXT")
        _ensure_column("trades", "fill_status", "fill_status TEXT")
        _ensure_column("trades", "fill_qty", "fill_qty REAL")
        _ensure_column("trades", "fill_price", "fill_price REAL")
        _ensure_column("trades", "fill_reconciled_at", "fill_reconciled_at TEXT")
        # Evening v2 structured per-trade grades. Stored as JSON arrays so
        # position_reviewer can aggregate counts (correct/premature/wrong)
        # without parsing prose. NULL for pre-v2 rows → treated as [].
        _ensure_column("insights", "sell_grades_json", "sell_grades_json TEXT")
        _ensure_column("insights", "buy_grades_json", "buy_grades_json TEXT")
        # Phase-1 evening-upgrade: structured missed_opportunities persist
        # here so next-day PM's L3d memory + quarterly meta-reflection's
        # theme_coverage_report can aggregate without re-running the LLM.
        # NULL for pre-upgrade rows → downstream readers default to [].
        _ensure_column(
            "insights",
            "missed_opportunities_json",
            "missed_opportunities_json TEXT DEFAULT '[]'",
        )
        # Per-call LLM cost tracking (2026-05-13). input_tokens /
        # output_tokens stored separately so cost can be recomputed if
        # pricing changes; cost_usd is the snapshot at insert time.
        # cost_usd is REAL (not cent integers) — SQLite handles small
        # floats fine and per-call costs span 4 orders of magnitude
        # ($0.0001 / macro to $1.00+ / tech full chunk).
        _ensure_column("agent_logs", "input_tokens", "input_tokens INTEGER")
        _ensure_column("agent_logs", "output_tokens", "output_tokens INTEGER")
        _ensure_column("agent_logs", "cost_usd", "cost_usd REAL")
        _ensure_column("agent_logs", "provider_requests", "provider_requests INTEGER")
        # Stage 1 (QAMC provider/model/correlation plumbing). All nullable —
        # legacy rows read back as NULL/None, never a fabricated value (per
        # DECISION #12 / ACCEPTANCE_CRITERIA "unknown stays unknown"). Sourced
        # from the matching new AgentResult fields (src/agents/base.py); see
        # docs/architecture/MODEL_PROVIDER_ARCHITECTURE.md "Required contract".
        _ensure_column("agent_logs", "requested_provider", "requested_provider TEXT")
        _ensure_column("agent_logs", "requested_model", "requested_model TEXT")
        # `model` (existing column, corrected Stage 0.5) already holds the
        # ACTUAL model; actual_provider is its provider, derived the same way.
        _ensure_column("agent_logs", "actual_provider", "actual_provider TEXT")
        # sha256(system_prompt)[:12] at call time — a cheap "did the prompt
        # text change" signal, not a semantic version.
        _ensure_column("agent_logs", "prompt_version", "prompt_version TEXT")
        _ensure_column("agent_logs", "latency_s", "latency_s REAL")
        # 'success' | 'fallback' | 'failed'. NULL for pre-Stage-1 rows.
        _ensure_column("agent_logs", "status", "status TEXT")
        # finish_reason/truncated were already computed on AgentResult
        # (Stage 0 audit F-2: computed but never persisted) — closing that
        # gap here costs nothing extra since the values already exist.
        _ensure_column("agent_logs", "finish_reason", "finish_reason TEXT")
        _ensure_column("agent_logs", "truncated", "truncated INTEGER")
        # Decision-level correlation (links a portfolio_manager/risk_manager
        # agent_logs row to the trades row(s) its decision produced). NULL
        # for every other agent and for all pre-Stage-1 rows.
        _ensure_column("agent_logs", "decision_id", "decision_id TEXT")
        _ensure_column("trades", "decision_id", "decision_id TEXT")
        _ensure_column("trades", "realized_pnl", "realized_pnl REAL")
        # codex r7 P1 #3: pending_protection_restores table for older DBs
        # that pre-date the orphaned-stop-restore queue. Idempotent.
        try:
            self.conn.execute("""
                CREATE TABLE IF NOT EXISTS pending_protection_restores (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol TEXT NOT NULL,
                    sell_order_id TEXT NOT NULL,
                    position_qty_before_sell REAL NOT NULL,
                    specs_json TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT (datetime('now')),
                    run_id TEXT
                )
            """)
            self.conn.commit()
        except Exception as e:
            _log.error("Schema migration failed for pending_protection_restores: %s", e)

        # Stage 4 (QAMC): specialist_evidence table for older DBs that
        # pre-date it. Idempotent, mirrors the pending_protection_restores
        # pattern above.
        try:
            self.conn.execute("""
                CREATE TABLE IF NOT EXISTS specialist_evidence (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL,
                    decision_id TEXT,
                    agent_name TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    scope TEXT NOT NULL,
                    symbol TEXT,
                    evidence_json TEXT NOT NULL,
                    timestamp TEXT NOT NULL DEFAULT (datetime('now'))
                )
            """)
            self.conn.commit()
        except Exception as e:
            _log.error("Schema migration failed for specialist_evidence: %s", e)

        # Indexes for prune queries. Both prune_trades and prune_agent_logs
        # scan WHERE timestamp < ?. 5-year retention on trades (~10-20k rows
        # before pruning) and 2-year retention on agent_logs (~15-25k rows
        # with full_response 20-40KB each) make these scans slow without
        # an index — write lock is held for the full delete duration.
        # IDX_IF_NOT_EXISTS is idempotent so existing DBs gain the index
        # on the next initialize().
        for table, col in (
            ("trades", "timestamp"),
            ("agent_logs", "timestamp"),
            ("pending_protection_restores", "created_at"),
            ("specialist_evidence", "run_id"),
            ("specialist_evidence", "symbol"),
            ("specialist_evidence", "decision_id"),
        ):
            try:
                self.conn.execute(
                    f"CREATE INDEX IF NOT EXISTS idx_{table}_{col} ON {table}({col})"
                )
            except Exception as e:
                _log.warning("Index creation failed for %s.%s: %s", table, col, e)
        self.conn.commit()

    def execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            return self.conn.execute(sql, params)

    def save_evening_snapshot(
        self,
        *,
        date: str,
        total_value: float,
        daily_pnl: float,
        daily_return_pct: float,
        equity_close: float | None = None,
        tomorrow_outlook: str,
        lessons: str,
        suggested_actions,
        risk_rating: str,
        tomorrow_bias: str = "neutral",
        tomorrow_conviction: str = "medium",
        tomorrow_key_risks=(),
        sell_decisions_assessment: str = "",
        sell_grades=(),
        buy_grades=(),
        missed_opportunities=(),
    ) -> None:
        """Atomically write the evening's daily_pnl + insights rows.

        Phase 4 #5: transaction boundary. These two writes are two sides
        of the same fact ("here's today's P&L; here's the narrative I
        wrote about it") — if the process crashes between them, next
        morning's PM reads inconsistent state. Doing both in one BEGIN /
        COMMIT prevents that split-brain.

        All writes happen under the same _lock acquisition, matching the
        pattern used by the single-write insert methods. Callers should
        treat this as the sanctioned way to persist evening output.

        sell_grades / buy_grades are stored as JSON-serialized lists
        (list[dict] or list[Pydantic]). `_build_sell_calibration_summary`
        aggregates them into counts for position_reviewer's prompt.
        """
        import json

        def _to_json_list(val) -> str:
            if isinstance(val, str):
                return val or "[]"
            if not val:
                return "[]"
            out = []
            for item in val:
                if hasattr(item, "model_dump"):
                    out.append(item.model_dump())
                elif isinstance(item, dict):
                    out.append(item)
            return json.dumps(out)

        actions_json = (
            json.dumps(suggested_actions) if isinstance(suggested_actions, list)
            else suggested_actions
        )
        risks_json = (
            json.dumps(list(tomorrow_key_risks))
            if not isinstance(tomorrow_key_risks, str) else tomorrow_key_risks
        )
        sell_grades_json = _to_json_list(sell_grades)
        buy_grades_json = _to_json_list(buy_grades)
        missed_opportunities_json = _to_json_list(missed_opportunities)
        with self._lock:
            try:
                self.conn.execute("BEGIN")
                # Upsert that PRESERVES a previously-stored equity_close when
                # this write carries None — a documented same-day evening re-run
                # whose portfolio_history fetch failed must not wipe the 4pm
                # close captured by the first run. COALESCE keeps the old value.
                self.conn.execute(
                    "INSERT INTO daily_pnl "
                    "(date, total_value, daily_pnl, daily_return_pct, equity_close) "
                    "VALUES (?, ?, ?, ?, ?) "
                    "ON CONFLICT(date) DO UPDATE SET "
                    "total_value=excluded.total_value, "
                    "daily_pnl=excluded.daily_pnl, "
                    "daily_return_pct=excluded.daily_return_pct, "
                    "equity_close=COALESCE(excluded.equity_close, daily_pnl.equity_close)",
                    (date, total_value, daily_pnl, daily_return_pct, equity_close),
                )
                self.conn.execute(
                    "INSERT OR REPLACE INTO insights "
                    "(date, tomorrow_outlook, lessons, suggested_actions, risk_rating, "
                    "tomorrow_bias, tomorrow_conviction, tomorrow_key_risks, "
                    "sell_decisions_assessment, sell_grades_json, buy_grades_json, "
                    "missed_opportunities_json) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (date, tomorrow_outlook, lessons, actions_json, risk_rating,
                     tomorrow_bias, tomorrow_conviction, risks_json,
                     sell_decisions_assessment or "",
                     sell_grades_json, buy_grades_json,
                     missed_opportunities_json),
                )
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                raise

    def insert_trade(self, symbol: str, action: str, qty: float, price: float,
                     reasoning: str, run_id: str,
                     stop_loss: float = 0, take_profit: float = 0,
                     broker_order_id: str | None = None,
                     fill_status: str | None = None,
                     decision_id: str | None = None,
                     expected_horizon_sessions: int | None = None,
                     setup_type: str | None = None) -> int:
        """Insert a trade record. Returns the new row's id.

        `fill_status` semantics:
          - 'submitted'  — sent to broker, terminal status pending
          - 'filled'     — broker confirmed execution (full or partial)
          - 'canceled' / 'rejected' / 'expired' / 'done_for_day' — terminal broker
                           status; may still carry fill_qty/fill_price for partial fills
          - None         — legacy row or non-executed audit row (currently HOLD).
                           Legacy BUY/SELL rows still count as executed for back-compat;
                           synthetic HOLD rows are explicitly excluded from executed_only.
        """
        def _do():
            cur = self.conn.execute(
                "INSERT INTO trades (symbol, action, qty, price, reasoning, run_id, "
                "stop_loss, take_profit, broker_order_id, fill_status, decision_id, "
                "expected_horizon_sessions, setup_type) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (symbol, action, qty, price, reasoning, run_id,
                 stop_loss, take_profit, broker_order_id, fill_status, decision_id,
                 expected_horizon_sessions, setup_type),
            )
            self.conn.commit()
            return cur.lastrowid
        return self._locked_write(_do, label="insert_trade")

    def confirm_trade_submitted(
        self, row_id: int, broker_order_id: str | None,
    ) -> int:
        """Flip a pending_submit row to submitted after broker accepted.

        Part of the write-ahead-intent pattern for BUY submission (audit
        F4). The flow is:

            insert_trade(..., fill_status='pending_submit', broker_order_id=NULL)
            broker.submit_order(...)
            confirm_trade_submitted(row_id, broker_order_id)  ← this method

        On the crash window between submit_order returning and this call
        landing, the row stays as pending_submit with broker_order_id
        unset. Reconcile can detect orphans by (fill_status='pending_submit'
        AND broker_order_id IS NULL) and decide how to reconcile against
        the broker's order list.
        """
        with self._lock:
            cur = self.conn.execute(
                "UPDATE trades SET broker_order_id = ?, fill_status = 'submitted' "
                "WHERE id = ?",
                (broker_order_id, row_id),
            )
            self.conn.commit()
            return cur.rowcount

    def mark_trade_submit_failed(self, row_id: int) -> int:
        """Flag a pending_submit row as submit_failed.

        Used when broker.submit_order raised (broker may or may not have
        the order) OR when broker rejected the order (_order_accepted
        returned False). Distinct from rejected/canceled because those
        statuses imply the broker accepted then rejected; submit_failed
        means we don't know what the broker saw. Operator / reconcile
        sweeps these against the broker's order list by symbol + time.
        """
        with self._lock:
            cur = self.conn.execute(
                "UPDATE trades SET fill_status = 'submit_failed' "
                "WHERE id = ?",
                (row_id,),
            )
            self.conn.commit()
            return cur.rowcount

    def update_trade_fill(
        self, broker_order_id: str, fill_status: str,
        fill_qty: float | None = None, fill_price: float | None = None,
    ) -> int:
        """Update a trade row's fill reconciliation after broker terminal status.

        Matches on broker_order_id. Returns row count updated.
        """
        with self._lock:
            cur = self.conn.execute(
                "UPDATE trades SET fill_status = ?, fill_qty = ?, fill_price = ?, "
                "fill_reconciled_at = datetime('now') "
                "WHERE broker_order_id = ?",
                (fill_status, fill_qty, fill_price, broker_order_id),
            )
            try:
                has_fill = float(fill_qty or 0) > 0
            except (TypeError, ValueError):
                has_fill = False
            if has_fill and fill_price is not None:
                row = self.conn.execute(
                    "SELECT id, symbol, action FROM trades WHERE broker_order_id = ?",
                    (broker_order_id,),
                ).fetchone()
                if row is not None and row["action"] not in {"BUY", "SWEEP_BUY", "HOLD"}:
                    realized = self._realized_pnl_through_trade(row["symbol"], row["id"])
                    self.conn.execute(
                        "UPDATE trades SET realized_pnl = ? WHERE id = ?",
                        (realized, row["id"]),
                    )
            self.conn.commit()
            return cur.rowcount or 0

    def _realized_pnl_through_trade(self, symbol: str, through_id: int) -> float | None:
        """Average-cost P&L for one confirmed exit; caller holds ``_lock``."""
        rows = self.conn.execute(
            "SELECT id, action, qty, price, fill_status, fill_qty, fill_price "
            "FROM trades WHERE symbol = ? AND id <= ? ORDER BY id",
            (symbol, through_id),
        ).fetchall()
        inventory = 0.0
        average_cost = 0.0
        target_pnl: float | None = None
        for row in rows:
            status = str(row["fill_status"] or "").lower()
            actual_qty = float(row["fill_qty"] or 0)
            actual_price = row["fill_price"]
            # Only broker-confirmed execution facts are safe cost basis.
            if actual_qty <= 0 or actual_price is None or status in {
                "submitted", "pending_submit", "submit_failed",
            }:
                continue
            actual_price = float(actual_price)
            if row["action"] in {"BUY", "SWEEP_BUY"}:
                new_inventory = inventory + actual_qty
                average_cost = (
                    (inventory * average_cost + actual_qty * actual_price) / new_inventory
                    if new_inventory > 0 else 0.0
                )
                inventory = new_inventory
                continue
            if row["action"] == "HOLD":
                continue
            if inventory + 1e-9 < actual_qty:
                pnl = None  # incomplete canonical cost basis; unknown stays unknown
                inventory = max(0.0, inventory - actual_qty)
            else:
                pnl = round((actual_price - average_cost) * actual_qty, 6)
                inventory -= actual_qty
                if inventory <= 1e-9:
                    inventory = 0.0
                    average_cost = 0.0
            if row["id"] == through_id:
                target_pnl = pnl
        return target_pnl

    def get_symbols_with_open_ledger_qty(self) -> dict[str, float]:
        """Per-symbol net share count the `trades` ledger BELIEVES it holds.

        BUY / SWEEP_BUY add executed qty; every other non-HOLD executed
        action subtracts it — mirrors the accounting `_realized_pnl_
        through_trade` and `compute_trade_calibration` already do,
        collapsed to a running total per symbol instead of per-lot detail,
        because this function only needs to know WHETHER the ledger and
        the broker still agree, not how a mismatch would price out.

        This is the ledger's own, self-contained belief — it has no idea
        the broker did anything it was never told about. Comparing this
        number against `AlpacaBroker.get_positions()` is exactly how the
        2026-08-28 ONDS/CCJ gap was found: both BUY rows left this
        function reporting 17 and 2 shares respectively long after the
        broker's own book had gone to zero, because the protective stop
        that closed them was never written back to `trades`.
        `_reconcile_stop_out_fills` (src/pipeline.py) is the caller that
        acts on a mismatch.
        """
        with self._lock:
            rows = self.conn.execute(
                "SELECT symbol, action, qty, fill_qty FROM trades "
                f"WHERE {self._executed_trade_predicate()} ORDER BY id",
            ).fetchall()
        net: dict[str, float] = {}
        for row in rows:
            action = (row["action"] or "").upper()
            if action == "HOLD":
                continue
            qty = float(row["fill_qty"] if row["fill_qty"] else row["qty"] or 0)
            if qty <= 0:
                continue
            sign = 1.0 if action in ("BUY", "SWEEP_BUY") else -1.0
            symbol = row["symbol"]
            net[symbol] = net.get(symbol, 0.0) + sign * qty
        return net

    def get_known_broker_order_ids(self, symbol: str) -> set[str]:
        """Every `broker_order_id` already recorded in `trades` for `symbol`.

        The dedup key `_reconcile_stop_out_fills` uses to tell "the broker
        already told us about this order" apart from "this fill has never
        touched the ledger". The reconciler re-runs every session
        (morning / intra_check / midday / close / evening), so this set is
        what keeps recording a stop-out an exactly-once operation no
        matter how many passes see the same gap.
        """
        with self._lock:
            rows = self.conn.execute(
                "SELECT broker_order_id FROM trades "
                "WHERE symbol = ? AND broker_order_id IS NOT NULL",
                (symbol,),
            ).fetchall()
        return {r[0] for r in rows}

    def insert_stop_out_trade(
        self, *, symbol: str, qty: float, price: float,
        broker_order_id: str, filled_at: str | None,
        run_id: str | None = None, action: str = "STOP_OUT",
        reasoning: str | None = None,
    ) -> tuple[int, bool]:
        """Idempotently record a broker-initiated exit the ledger never saw.

        2026-08-28: ONDS (17 @ 8.53, stopped 7.93 → realized -$10.20) and
        CCJ (2 @ 107.465, stopped 102.955 → realized -$9.02) were both
        closed by their broker-resident protective stop with NO row ever
        written to `trades`. The stop order was placed by
        `AlpacaBroker.place_entry_protection` / `_repair_stop_coverage` /
        `shift_stops_down`, none of which log the ORDER ITSELF as a ledger
        row — unlike every system-DECIDED exit (SELL / REDUCE / TRAIL_STOP
        / SWEEP_SELL), which all call `insert_trade` at submission time and
        get picked up by `_reconcile_fills` once terminal. This is the
        write-back for that other class of order. There is no 'submitted'
        phase for a row created here: by the time `_reconcile_stop_out_
        fills` learns the order exists, the broker has already reported it
        as terminally filled.

        Idempotency: keyed on `broker_order_id`, checked and inserted
        under the SAME lock, so no matter how many times a session's
        reconciliation pass runs — or how many overlapping sessions
        observe the same gap — one broker order id can only ever produce
        ONE row. Mirrors `update_trade_fill`'s realized_pnl write, just
        for a row that does not exist yet rather than one already
        'submitted'.

        `realized_pnl` is computed the instant the row exists, via the
        SAME average-cost walk every other exit uses
        (`_realized_pnl_through_trade`) — it stays NULL, not a guess, when
        the ledger's own BUY history can't cover the exited quantity (an
        unmatched exit; `_reconcile_stop_out_fills` flags that case rather
        than silently accepting an unpriced row).

        Returns `(row_id, created)`. `created=False` means the order was
        already recorded — the existing row's id is returned so a caller
        never needs a second lookup to stay idempotent-safe.
        """
        if not broker_order_id:
            # Every caller constructs this from a REAL broker order dict
            # that is only ever produced with a non-empty id (see
            # AlpacaBroker.list_filled_sell_orders) — a falsy id here means
            # a caller bug, not a legitimate row. Refusing loudly beats
            # silently inserting a row the idempotency key can never find
            # again (broker_order_id IS NULL would never match on replay,
            # and this exit could get double-recorded on the next pass —
            # exactly the failure mode this function exists to prevent).
            raise ValueError(
                "insert_stop_out_trade requires a non-empty broker_order_id "
                "— it is the idempotency key that makes a stop-out record "
                "exactly-once across repeated reconciliation passes"
            )

        def _do():
            existing = self.conn.execute(
                "SELECT id FROM trades WHERE broker_order_id = ?",
                (broker_order_id,),
            ).fetchone()
            if existing is not None:
                return existing["id"], False
            ts = filled_at or self._sqlite_utc_timestamp(datetime.now(UTC))
            cur = self.conn.execute(
                "INSERT INTO trades (symbol, action, qty, price, reasoning, "
                "run_id, broker_order_id, fill_status, fill_qty, fill_price, "
                "fill_reconciled_at, timestamp) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 'filled', ?, ?, datetime('now'), ?)",
                (
                    symbol, action, qty, price,
                    reasoning or (
                        "Broker-initiated protective-stop fill — the system "
                        "never submitted this order as a decision; written "
                        "back by the stop-out reconciler (2026-08-28 "
                        "ONDS/CCJ gap; see ReconciliationConfig)."
                    ),
                    run_id, broker_order_id, qty, price, ts,
                ),
            )
            row_id = cur.lastrowid
            realized = self._realized_pnl_through_trade(symbol, row_id)
            self.conn.execute(
                "UPDATE trades SET realized_pnl = ? WHERE id = ?",
                (realized, row_id),
            )
            self.conn.commit()
            return row_id, True
        return self._locked_write(_do, label="insert_stop_out_trade")

    def get_unreconciled_orders(self, run_id: str | None = None) -> list[dict]:
        """Trade rows with broker_order_id set but fill_status still 'submitted'.

        Pipeline's reconciliation step fetches these and asks the broker for
        their terminal status. Scoping to run_id lets per-run reconciliation
        not touch stragglers from other runs.
        """
        conditions = ["fill_status = 'submitted'", "broker_order_id IS NOT NULL"]
        params: list = []
        if run_id:
            conditions.append("run_id = ?")
            params.append(run_id)
        where = " AND ".join(conditions)
        with self._lock:
            rows = self.conn.execute(
                f"SELECT * FROM trades WHERE {where}", tuple(params),
            ).fetchall()
        return [dict(r) for r in rows]

    def get_orphaned_pending_submits(
        self, min_age_seconds: int = 120,
    ) -> list[dict]:
        """BUY write-ahead rows the broker may or may not have received:
        fill_status 'pending_submit' with broker_order_id still NULL —
        a crash between submit_order() returning and
        confirm_trade_submitted() landing.

        audit F4: confirm_trade_submitted's docstring promised reconcile
        could detect orphans by exactly this predicate, but nothing swept
        them — a real broker fill could go forever untracked. Age-gated
        (timestamp older than min_age_seconds) so a same-process in-flight
        submit — converted to submitted/submit_failed within microseconds
        — is never misread as an orphan; real orphans are from a prior
        crashed session and are minutes-to-days old. The cutoff uses
        SQLite's own clock on both sides (datetime('now', ?)) so there's
        no host-TZ / format skew.
        """
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM trades WHERE fill_status = 'pending_submit' "
                "AND broker_order_id IS NULL "
                "AND timestamp < datetime('now', ?) "
                "ORDER BY timestamp ASC",
                (f"-{int(min_age_seconds)} seconds",),
            ).fetchall()
        return [dict(r) for r in rows]

    def has_pending_action_for_symbol(
        self, symbol: str, action: str, today_only: bool = True,
    ) -> bool:
        """True if a (symbol, action) trade row exists with fill_status
        'submitted' and a broker_order_id — i.e., a previous submission
        is still in flight at the broker.

        Used to keep consecutive intra_check ticks from re-firing the same
        EMERGENCY_SELL while the first limit order is still pending fill.
        Without this, intra at T submits a -1% LIMIT EMERGENCY_SELL, the
        tape goes through it without filling, and intra at T+30min sees
        the position still on book and submits a duplicate — risking
        double-exit on a partial fill of the first order.

        today_only restricts the lookup to the current ET trading day so
        a stale 'submitted' row from a previous session can't permanently
        block a fresh exit. If your reconciliation pass updated the row
        to a terminal status, this returns False as expected.
        """
        conditions = [
            "fill_status = 'submitted'",
            "broker_order_id IS NOT NULL",
            "symbol = ?",
            "action = ?",
        ]
        params: list = [symbol, action]
        if today_only:
            start, end = self._et_day_utc_bounds()
            conditions.append("timestamp >= ?")
            conditions.append("timestamp < ?")
            params.extend([start, end])
        where = " AND ".join(conditions)
        with self._lock:
            row = self.conn.execute(
                f"SELECT 1 FROM trades WHERE {where} LIMIT 1", tuple(params),
            ).fetchone()
        return row is not None

    def insert_pending_protection_restore(
        self, *, symbol: str, sell_order_id: str,
        position_qty_before_sell: float, specs_json: str,
        run_id: str | None = None,
    ) -> int:
        """Persist an orphaned protection-restore intent.

        Written when _finalize_protection_after_sell can't act now —
        either cancel of the lingering SELL raised, or the order didn't
        converge to terminal within the short post-cancel wait. Drained
        at session start: the pending row's sell_order_id is re-queried
        for terminal status, and if now terminal, the persisted specs
        drive a fresh finalize attempt.
        """
        def _do():
            cur = self.conn.execute(
                "INSERT INTO pending_protection_restores "
                "(symbol, sell_order_id, position_qty_before_sell, specs_json, run_id) "
                "VALUES (?, ?, ?, ?, ?)",
                (symbol, sell_order_id, position_qty_before_sell, specs_json, run_id),
            )
            self.conn.commit()
            return cur.lastrowid or 0
        return self._locked_write(_do, label="insert_pending_protection_restore")

    def get_pending_protection_restores(self) -> list[dict]:
        """All currently-pending protection-restore rows, oldest first."""
        with self._lock:
            rows = self.conn.execute(
                "SELECT id, symbol, sell_order_id, position_qty_before_sell, "
                "specs_json, created_at, run_id FROM pending_protection_restores "
                "ORDER BY created_at ASC"
            ).fetchall()
        return [dict(r) for r in rows]

    def delete_pending_protection_restore(self, row_id: int) -> int:
        """Remove a row by its primary key (after successful drain)."""
        def _do():
            cur = self.conn.execute(
                "DELETE FROM pending_protection_restores WHERE id = ?",
                (row_id,),
            )
            self.conn.commit()
            return cur.rowcount or 0
        return self._locked_write(_do, label="delete_pending_protection_restore")

    def update_pending_protection_restore(
        self, row_id: int, *,
        sell_order_id: str | None = None,
        position_qty_before_sell: float | None = None,
        specs_json: str | None = None,
    ) -> int:
        """Partial-update a recovery row (only the provided fields).

        audit F1 write-ahead lifecycle: a row is inserted BEFORE
        cancel_protective_stops with a sentinel sell_order_id; this flips
        it to the real broker order id once the SELL is accepted, and
        finalize-on-bail uses it to UPDATE the existing row (instead of
        INSERTing a duplicate alongside the write-ahead row).
        """
        sets: list[str] = []
        params: list = []
        if sell_order_id is not None:
            sets.append("sell_order_id = ?")
            params.append(sell_order_id)
        if position_qty_before_sell is not None:
            sets.append("position_qty_before_sell = ?")
            params.append(position_qty_before_sell)
        if specs_json is not None:
            sets.append("specs_json = ?")
            params.append(specs_json)
        if not sets:
            return 0
        params.append(row_id)
        with self._lock:
            cur = self.conn.execute(
                f"UPDATE pending_protection_restores SET {', '.join(sets)} "
                "WHERE id = ?",
                tuple(params),
            )
            self.conn.commit()
            return cur.rowcount or 0

    def update_pending_protection_restore_specs(
        self, row_id: int, specs_json: str,
    ) -> int:
        """Replace the specs_json of an existing recovery row.

        Used by the drain path's partial-restore handling: when 1 of N
        specs landed on this drain attempt, the next drain should only
        retry the N-1 that failed (re-submitting the already-alive stop
        either creates a duplicate or hits held_for_orders, neither
        productive). Codex r10 #1.
        """
        with self._lock:
            cur = self.conn.execute(
                "UPDATE pending_protection_restores SET specs_json = ? WHERE id = ?",
                (specs_json, row_id),
            )
            self.conn.commit()
            return cur.rowcount or 0

    @staticmethod
    def _executed_trade_predicate() -> str:
        """SQL predicate for trades that executed at least some quantity."""
        return (
            "((fill_status IS NULL AND action != 'HOLD') OR fill_status = 'filled' "
            "OR COALESCE(fill_qty, 0) > 0)"
        )

    @staticmethod
    def _sqlite_utc_timestamp(when: datetime) -> str:
        """Format a datetime the same way SQLite stores `datetime('now')`.

        Trades are stored as naive UTC strings. Converting ET day boundaries
        into this format lets `today_only=True` mean "this ET trading day"
        regardless of the host timezone.
        """
        if when.tzinfo is None:
            when = when.replace(tzinfo=UTC)
        return when.astimezone(UTC).replace(tzinfo=None).strftime("%Y-%m-%d %H:%M:%S")

    @classmethod
    def _et_day_utc_bounds(cls, trading_day: date | None = None) -> tuple[str, str]:
        """UTC timestamp bounds [start, end) for an ET trading-day date."""
        day = trading_day or et_today()
        start_et = datetime.combine(day, time.min, tzinfo=ET)
        end_et = start_et + timedelta(days=1)
        return cls._sqlite_utc_timestamp(start_et), cls._sqlite_utc_timestamp(end_et)

    def get_trades(self, symbol: str | None = None, limit: int = 100,
                    today_only: bool = False,
                    executed_only: bool = False) -> list[dict]:
        conditions = []
        params: list = []
        if symbol:
            conditions.append("symbol = ?")
            params.append(symbol)
        if today_only:
            start_utc, end_utc = self._et_day_utc_bounds()
            conditions.append("timestamp >= ? AND timestamp < ?")
            params.extend([start_utc, end_utc])
        if executed_only:
            conditions.append(self._executed_trade_predicate())
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        with self._lock:
            # Secondary order-by on id ensures tie-break ordering is
            # deterministic — SQLite's timestamp precision is 1 second, so
            # a BUY inserted at T0 and TAKE_PROFIT inserted at T0+0.01 both
            # carry the same timestamp string. Without id DESC, duplicate-
            # timestamp rows come back in indeterminate order and logic
            # that scans "trades newer than the most recent BUY" can miss
            # the newer row.
            rows = self.conn.execute(
                f"SELECT * FROM trades {where} ORDER BY timestamp DESC, id DESC LIMIT ?",
                (*params, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def upsert_position(self, symbol: str, qty: float, avg_entry: float,
                        current_price: float, market_value: float,
                        unrealized_pnl: float, sector: str):
        with self._lock:
            self.conn.execute(
                """INSERT INTO positions (symbol, qty, avg_entry, current_price, market_value, unrealized_pnl, sector, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'))
                   ON CONFLICT(symbol) DO UPDATE SET
                     qty=excluded.qty, avg_entry=excluded.avg_entry,
                     current_price=excluded.current_price, market_value=excluded.market_value,
                     unrealized_pnl=excluded.unrealized_pnl, sector=excluded.sector,
                     updated_at=datetime('now')""",
                (symbol, qty, avg_entry, current_price, market_value, unrealized_pnl, sector),
            )
            self.conn.commit()

    def sync_positions(self, positions) -> None:
        """Replace positions table with a fresh broker snapshot.

        Upserts rows for currently-held symbols and deletes rows for any symbol
        no longer present. Prevents stale closed positions from lingering in the DB.

        Wraps DELETE + INSERT loop in an explicit BEGIN/COMMIT transaction so
        a crash between the DELETE and the first INSERT cannot leave the table
        in a half-state (would otherwise leave the next session's reviewer
        reading an empty positions snapshot while the broker still holds them).
        Mirrors the atomic-write discipline used in `save_evening_snapshot`.
        """
        current_symbols = {p.symbol for p in positions}
        with self._lock:
            try:
                self.conn.execute("BEGIN")
                if current_symbols:
                    placeholders = ",".join("?" for _ in current_symbols)
                    self.conn.execute(
                        f"DELETE FROM positions WHERE symbol NOT IN ({placeholders})",
                        tuple(current_symbols),
                    )
                else:
                    self.conn.execute("DELETE FROM positions")
                for p in positions:
                    self.conn.execute(
                        """INSERT INTO positions (symbol, qty, avg_entry, current_price, market_value, unrealized_pnl, sector, updated_at)
                           VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'))
                           ON CONFLICT(symbol) DO UPDATE SET
                             qty=excluded.qty, avg_entry=excluded.avg_entry,
                             current_price=excluded.current_price, market_value=excluded.market_value,
                             unrealized_pnl=excluded.unrealized_pnl, sector=excluded.sector,
                             updated_at=datetime('now')""",
                        (p.symbol, p.qty, p.avg_entry, p.current_price, p.market_value,
                         p.unrealized_pnl, p.sector),
                    )
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                raise

    def get_positions(self, open_only: bool = False) -> list[dict]:
        with self._lock:
            if open_only:
                # qty != 0, not qty > 0: a short carries a negative qty
                # (Alpaca convention) and is an OPEN position. `qty > 0` hid
                # every short from the operator's open-position view.
                rows = self.conn.execute(
                    "SELECT * FROM positions WHERE qty != 0"
                ).fetchall()
            else:
                rows = self.conn.execute("SELECT * FROM positions").fetchall()
        return [dict(row) for row in rows]

    def insert_agent_log(self, agent_name: str, run_id: str, input_summary: str,
                         output_summary: str, full_response: str, model: str,
                         tokens_used: int, input_message: str = "",
                         input_tokens: int | None = None,
                         output_tokens: int | None = None,
                         cost_usd: float | None = None,
                         provider_requests: int | None = None,
                         requested_provider: str | None = None,
                         requested_model: str | None = None,
                         actual_provider: str | None = None,
                         prompt_version: str | None = None,
                         latency_s: float | None = None,
                         status: str | None = None,
                         finish_reason: str | None = None,
                         truncated: bool | None = None,
                         decision_id: str | None = None):
        """`model` remains the ACTUAL responding model (Stage 0.5 contract —
        unchanged). The Stage 1 kwargs below are additive and all default to
        None so every pre-Stage-1 caller keeps working unmodified; omitting
        them persists NULL, never a fabricated value."""
        def _do():
            self.conn.execute(
                """INSERT INTO agent_logs (agent_name, run_id, input_summary, input_message,
                   output_summary, full_response, model, tokens_used,
                   input_tokens, output_tokens, cost_usd,
                   provider_requests,
                   requested_provider, requested_model, actual_provider,
                   prompt_version, latency_s, status, finish_reason, truncated,
                   decision_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (agent_name, run_id, input_summary, input_message, output_summary,
                 full_response, model, tokens_used,
                 input_tokens, output_tokens, cost_usd,
                 provider_requests,
                 requested_provider, requested_model, actual_provider,
                 prompt_version, latency_s, status,
                 finish_reason, None if truncated is None else int(truncated),
                 decision_id),
            )
            self.conn.commit()
        self._locked_write(_do, label="insert_agent_log")

    def insert_specialist_evidence(
        self, *, run_id: str, agent_name: str, kind: str, scope: str,
        evidence_json: str, symbol: str | None = None,
        decision_id: str | None = None,
    ) -> int:
        """Persist one already-VALIDATED structured evidence row (Stage 4).

        Purely additive/observational — see the table's CREATE comment.
        Callers (pipeline_stages.py) are expected to wrap this in their own
        try/except so a persistence hiccup here can never affect the
        research/decision/risk flow it's recording; this method itself does
        not swallow errors (matches every other insert_* method's contract),
        it just never touches trading-critical state.
        """
        def _do():
            cur = self.conn.execute(
                "INSERT INTO specialist_evidence "
                "(run_id, decision_id, agent_name, kind, scope, symbol, evidence_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (run_id, decision_id, agent_name, kind, scope, symbol, evidence_json),
            )
            self.conn.commit()
            return cur.lastrowid or 0
        return self._locked_write(_do, label="insert_specialist_evidence")

    # --- Position-reviewer memory (spec Phase 3.2 / audit §1.5) -----------
    #
    # `_build_own_recent_decisions` replays past ACTIONS and explicitly drops
    # HOLDs, so the reviewer rebuilt its view of every position from scratch
    # twice a day with no idea what it had measured six hours earlier. On
    # 2026-08-26 it sold EPD for "not progressing" when progress had risen
    # 16% -> 20% and distance-to-stop had improved since its own midday read.
    # These two methods give the seat a memory of its own numbers.

    POSITION_REVIEW_METRIC_KIND = "review_metrics"

    def save_position_review_metrics(
        self, *, run_id: str, symbol: str, metrics_json: str,
    ) -> int:
        """Snapshot one position's deterministic metrics for the next review."""
        return self.insert_specialist_evidence(
            run_id=run_id, agent_name="position_reviewer",
            kind=self.POSITION_REVIEW_METRIC_KIND, scope="symbol",
            symbol=symbol.upper(), evidence_json=metrics_json,
        )

    def get_prior_position_review_metrics(
        self, symbols, *, exclude_run_id: str | None = None,
    ) -> dict[str, dict]:
        """Most recent prior metric snapshot per symbol, as {symbol: row}.

        `exclude_run_id` drops the current run's own rows so a re-entrant or
        retried review compares against the LAST session, never against the
        snapshot it just wrote. Each returned row carries `evidence_json` and
        `timestamp`; parsing is the caller's job so a single malformed blob
        cannot take down the read.
        """
        wanted = [str(s).strip().upper() for s in symbols if str(s).strip()]
        if not wanted:
            return {}
        placeholders = ",".join("?" for _ in wanted)
        sql = (
            "SELECT symbol, evidence_json, timestamp, run_id FROM specialist_evidence "
            f"WHERE agent_name='position_reviewer' AND kind=? AND symbol IN ({placeholders})"
        )
        params: list = [self.POSITION_REVIEW_METRIC_KIND, *wanted]
        if exclude_run_id:
            sql += " AND run_id != ?"
            params.append(exclude_run_id)
        sql += " ORDER BY timestamp DESC, id DESC"
        with self._lock:
            rows = self.conn.execute(sql, tuple(params)).fetchall()
        latest: dict[str, dict] = {}
        for row in rows:
            row = dict(row)
            # Rows arrive newest-first, so the first sighting of a symbol wins.
            latest.setdefault(row["symbol"], row)
        return latest

    def record_intraday_evaluation(
        self, *, symbol: str, run_id: str, status: str, detail: str = "",
    ) -> None:
        def _do():
            self.conn.execute(
                "INSERT INTO intraday_evaluations(symbol, run_id, status, detail) "
                "VALUES (?, ?, ?, ?) ON CONFLICT(symbol, run_id) DO UPDATE SET "
                "status=excluded.status, detail=excluded.detail",
                (symbol.upper(), run_id, status, detail),
            )
            self.conn.commit()
        self._locked_write(_do, label="record_intraday_evaluation")

    def get_recent_intraday_evaluations(
        self, symbol: str, *, cooldown_hours: float,
    ) -> list[dict]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM intraday_evaluations WHERE symbol=? "
                "AND timestamp >= datetime('now', ?) ORDER BY timestamp DESC",
                (symbol.upper(), f"-{float(cooldown_hours):g} hours"),
            ).fetchall()
        return [dict(row) for row in rows]

    def session_prefixes_logged_on(self, trading_day: date | None = None) -> set[str]:
        """Set of session run_id PREFIXES that produced agent_logs on the given
        ET trading day (default today).

        run_id is formatted '{prefix}-{8hex}' where prefix is 'run' for the
        morning session and the session name otherwise (midday / close /
        evening / intra_check / earnings_preprocess / meta — see
        RunContext.start). A session that ran its LLM work leaves >=1 row; a
        session that silently never fired leaves none. Used by the evening
        dead-man's-switch check to detect a missing session — the one failure
        mode push-on-completion observability structurally cannot see.
        """
        start_utc, end_utc = self._et_day_utc_bounds(trading_day)
        with self._lock:
            rows = self.conn.execute(
                "SELECT DISTINCT run_id FROM agent_logs "
                "WHERE timestamp >= ? AND timestamp < ?",
                (start_utc, end_utc),
            ).fetchall()
        prefixes: set[str] = set()
        for r in rows:
            rid = r[0] or ""
            prefixes.add(rid.rsplit("-", 1)[0] if "-" in rid else rid)
        return prefixes

    def agent_names_logged_on(self, run_id_prefix: str,
                              trading_day: date | None = None) -> set[str]:
        """Distinct agent_name values logged on the given ET trading day for
        run_ids starting with `run_id_prefix` (e.g. 'run-' for morning).

        RC5 (2026-07-16): the prefix check above can't tell a COMPLETED
        morning from one killed mid-flight — research rows land before the
        kill, so 'run' shows present while PM/RM never ran. This lets the
        dead-man's check ask "did the pipeline actually reach the decision
        stage?"
        """
        start_utc, end_utc = self._et_day_utc_bounds(trading_day)
        with self._lock:
            rows = self.conn.execute(
                "SELECT DISTINCT agent_name FROM agent_logs "
                "WHERE timestamp >= ? AND timestamp < ? AND run_id LIKE ?",
                (start_utc, end_utc, f"{run_id_prefix}%"),
            ).fetchall()
        return {r[0] for r in rows if r[0]}

    def sum_session_cost(self, run_id: str) -> tuple[float | None, int]:
        """Total cost + per-call count for a session's run_id.

        Returns (cost_usd_or_none, num_calls). cost is None when ANY
        agent in the session had an unknown-model cost — better to
        flag the gap than report a partial sum that looks correct.
        """
        with self._lock:
            rows = self.conn.execute(
                "SELECT cost_usd FROM agent_logs WHERE run_id = ?",
                (run_id,),
            ).fetchall()
        if not rows:
            return (None, 0)
        if any(r[0] is None for r in rows):
            # Partial coverage — return None so caller renders '$?.??'
            # rather than a misleading sum-of-known-only.
            return (None, len(rows))
        return (sum(float(r[0]) for r in rows), len(rows))

    def get_agent_logs(self, run_id: str) -> list[dict]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM agent_logs WHERE run_id = ? ORDER BY timestamp", (run_id,)
            ).fetchall()
        return [dict(row) for row in rows]

    def prune_trades(self, keep_days: int = 365 * 5) -> int:
        """Delete trades rows older than keep_days. Default retention 5 years.

        Kept long for audit purposes — still finite to bound table size over a
        decade-plus horizon. Returns count deleted.
        """
        if keep_days <= 0:
            # `datetime('now', '-0 days')` == 'now' → deletes the entire
            # trades audit log. Refuse rather than silently destroy
            # potentially years of broker history.
            raise ValueError(f"prune_trades: keep_days must be > 0, got {keep_days}")
        with self._lock:
            cursor = self.conn.execute(
                "DELETE FROM trades WHERE timestamp < datetime('now', ?)",
                (f"-{keep_days} days",),
            )
            self.conn.commit()
            return cursor.rowcount or 0

    def prune_pending_protection_restores(self, keep_days: int = 30) -> int:
        """Delete pending_protection_restores rows older than keep_days.

        Drain re-attempts these rows every session; a row that survives
        ~30 calendar days (~20 trading sessions) means either:
          - broker forgot the sell_order_id (deep history GC),
          - the underlying position is gone via other paths (manual
            close, EMERGENCY_SELL during a separate session), or
          - the row's specs_json is malformed in a way drain can't
            recover from automatically.
        In any of these cases, indefinite retention is just operational
        noise — drain can't help. Logs the symbols pruned at INFO so
        manual review remains possible. Returns count deleted.
        """
        if keep_days <= 0:
            # `datetime('now', '-0 days')` == 'now' → deletes EVERYTHING.
            # Caller almost certainly passed a typo / config bug. Refuse
            # rather than silently wipe a recovery queue.
            raise ValueError(
                f"prune_pending_protection_restores: keep_days must be > 0, got {keep_days}"
            )
        with self._lock:
            stale = self.conn.execute(
                "SELECT id, symbol, sell_order_id, created_at "
                "FROM pending_protection_restores "
                "WHERE created_at < datetime('now', ?)",
                (f"-{keep_days} days",),
            ).fetchall()
            if not stale:
                return 0
            for row in stale:
                logger.info(
                    "Pruning stale pending_protection_restore row %d: "
                    "symbol=%s sell_order_id=%s created_at=%s (>%dd old)",
                    row["id"], row["symbol"], row["sell_order_id"],
                    row["created_at"], keep_days,
                )
            cursor = self.conn.execute(
                "DELETE FROM pending_protection_restores "
                "WHERE created_at < datetime('now', ?)",
                (f"-{keep_days} days",),
            )
            self.conn.commit()
            return cursor.rowcount or 0

    def prune_agent_logs(self, keep_days: int = 730) -> int:
        """Delete agent_logs rows older than keep_days. Returns count deleted.

        Default is 2 years — long enough for quarter-over-quarter learning loops
        on what decisions worked while still bounding table size. agent_logs.full_response
        runs ~20-40KB per row with ~15-25 rows/day, so 730 days is ~200-300MB total.
        """
        if keep_days <= 0:
            raise ValueError(f"prune_agent_logs: keep_days must be > 0, got {keep_days}")
        with self._lock:
            cursor = self.conn.execute(
                "DELETE FROM agent_logs WHERE timestamp < datetime('now', ?)",
                (f"-{keep_days} days",),
            )
            self.conn.commit()
            return cursor.rowcount or 0

    def prune_specialist_evidence(self, keep_days: int = 730) -> int:
        """Delete specialist_evidence rows older than keep_days. Returns
        count deleted.

        Stage 4 (QAMC) added this table alongside agent_logs without a
        retention path — an ordinary trading day inserts a dozen-plus rows
        (per-symbol tech/earnings, run-scoped macro/news/PM-reasoning/RM-
        verdict, per-symbol PM-target/proposed-order/RM-modification) with
        no cap, on what's meant to be a long-running VPS-deployed bot.
        Default matches prune_agent_logs's 730-day (2 year) retention since
        this table is forensic-display detail for the same agent calls.
        """
        if keep_days <= 0:
            raise ValueError(f"prune_specialist_evidence: keep_days must be > 0, got {keep_days}")
        with self._lock:
            cursor = self.conn.execute(
                "DELETE FROM specialist_evidence WHERE timestamp < datetime('now', ?)",
                (f"-{keep_days} days",),
            )
            self.conn.commit()
            return cursor.rowcount or 0

    def insert_daily_pnl(self, date: str, total_value: float, daily_pnl: float,
                         daily_return_pct: float, equity_close: float | None = None):
        with self._lock:
            # COALESCE preserves a previously-stored equity_close when this
            # write carries None (e.g. an LLM-failed evening re-run on a day the
            # first run already captured the 4pm close).
            self.conn.execute(
                """INSERT INTO daily_pnl
                   (date, total_value, daily_pnl, daily_return_pct, equity_close)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(date) DO UPDATE SET
                     total_value=excluded.total_value,
                     daily_pnl=excluded.daily_pnl,
                     daily_return_pct=excluded.daily_return_pct,
                     equity_close=COALESCE(excluded.equity_close, daily_pnl.equity_close)""",
                (date, total_value, daily_pnl, daily_return_pct, equity_close),
            )
            self.conn.commit()

    def backfill_equity_close(self, date: str, equity_close: float) -> bool:
        """Fill in a still-NULL equity_close on an existing daily_pnl row.

        Self-heal for the API-lag gap: portfolio_history doesn't have a
        trading day's official close yet at the 20:00 ET evening run (it
        lands hours later), so equity_close is stored NULL that night — but
        it's available by the FOLLOWING evening's lookback fetch. Only
        touches rows that are still NULL; never overwrites an already-
        captured close. Returns True if a row was updated.
        """
        with self._lock:
            cursor = self.conn.execute(
                "UPDATE daily_pnl SET equity_close = ? "
                "WHERE date = ? AND equity_close IS NULL",
                (equity_close, date),
            )
            self.conn.commit()
            return cursor.rowcount > 0

    def get_daily_pnl(self, limit: int = 30, before_date: str | None = None) -> list[dict]:
        conditions = []
        params: list = []
        if before_date:
            conditions.append("date < ?")
            params.append(before_date)
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        with self._lock:
            rows = self.conn.execute(
                f"SELECT * FROM daily_pnl {where} ORDER BY date DESC LIMIT ?",
                (*params, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def save_insights(self, date: str, tomorrow_outlook: str, lessons: str,
                      suggested_actions: str, risk_rating: str,
                      tomorrow_bias: str = "neutral",
                      tomorrow_conviction: str = "medium",
                      tomorrow_key_risks: list | str = (),
                      sell_decisions_assessment: str = ""):
        import json
        actions_json = json.dumps(suggested_actions) if isinstance(suggested_actions, list) else suggested_actions
        risks_json = (
            json.dumps(list(tomorrow_key_risks))
            if not isinstance(tomorrow_key_risks, str) else tomorrow_key_risks
        )
        with self._lock:
            self.conn.execute(
                """INSERT OR REPLACE INTO insights
                   (date, tomorrow_outlook, lessons, suggested_actions, risk_rating,
                    tomorrow_bias, tomorrow_conviction, tomorrow_key_risks,
                    sell_decisions_assessment)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (date, tomorrow_outlook, lessons, actions_json, risk_rating,
                 tomorrow_bias, tomorrow_conviction, risks_json,
                 sell_decisions_assessment or ""),
            )
            self.conn.commit()

    def get_symbol_last_buy(self, symbol: str,
                            include_in_flight: bool = False) -> dict | None:
        """Most recent executed BUY row for a symbol.

        Submitted-but-never-filled BUYs must not show up in PM memory, but a
        partial fill that later ended canceled or expired still created real
        exposure and should be surfaced.

        `include_in_flight=True` also accepts fill_status in
        ('submitted', 'pending_submit') — used by the stop-coverage repair
        (audit round 2): a same-session BUY whose fill hasn't been reconciled
        yet is invisible under the executed predicate, so the repair either
        no-op'd or read a MONTHS-OLD prior BUY's stop level in exactly the
        crash/late-fill scenarios the belt exists for. An in-flight BUY's
        recorded stop_loss is precisely the reviewed intent the repair wants.
        PM-memory callers keep the strict default.
        """
        predicate = self._executed_trade_predicate()
        if include_in_flight:
            predicate = f"({predicate} OR fill_status IN ('submitted', 'pending_submit'))"
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM trades WHERE symbol = ? AND action = 'BUY' "
                f"AND {predicate} "
                "ORDER BY timestamp DESC, id DESC LIMIT 1",
                (symbol,),
            ).fetchone()
        return dict(row) if row else None

    def get_recent_insights(self, limit: int = 7) -> list[dict]:
        """Last N evening insights, newest first. PM reads to build 7-day narrative."""
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM insights ORDER BY date DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    def compute_trade_calibration(self, lookback_days: int = 45) -> dict:
        """Win rate + avg realized return on BUYs that closed in the window.

        Matches each BUY to the next SELL-family action (SELL, PARTIAL_SELL%,
        EMERGENCY_SELL, FORCE_DELEVER, REDUCE, TAKE_PROFIT, STOP_OUT, and a
        FILLED TRAIL_STOP) for the same symbol, FIFO. Open positions are
        excluded because their outcome isn't known yet.

        Bucketed by allocation size (proxy for conviction): a larger dollar
        commitment implies higher conviction when PM sized it. Lets PM see
        "my high-conviction bets have been winning / losing" without an
        explicit conviction column in trades.

        Returns:
            {"n": int, "win_rate_pct": float, "avg_return_pct": float,
             "avg_hold_days": float,
             "by_size": {
                "large": {...},  # $ entry >= 10k
                "medium": {...}, # 5-10k
                "small": {...},  # <5k
             }}
            or {} when there are too few closed trades to be meaningful.
        """
        with self._lock:
            # Skip orders that never executed. Legacy rows with NULL fill_status
            # pre-date reconciliation and are treated as filled for backward
            # compatibility.
            # BUY lots seed from FULL history; only the window bound on
            # EXITS below decides what counts as a "recent closed trade"
            # (audit round 2: windowing both sides made a SELL that closed a
            # pre-window lot FIFO-match an unrelated newer in-window BUY —
            # wrong entry price, wrong hold time, phantom remainder).
            rows = self.conn.execute(
                "SELECT symbol, action, qty, price, timestamp, fill_qty, "
                "fill_price, fill_status "
                "FROM trades "
                f"WHERE {self._executed_trade_predicate()} "
                "ORDER BY timestamp",
            ).fetchall()
        # FIFO queue of open BUY lots per symbol
        from collections import defaultdict
        open_lots: dict[str, list[dict]] = defaultdict(list)
        closed: list[dict] = []
        for row in rows:
            sym = row["symbol"]
            act = row["action"] or ""
            # Prefer actual fill data when present; fall back to requested.
            qty = float(row["fill_qty"] if row["fill_qty"] else row["qty"] or 0)
            price = float(row["fill_price"] if row["fill_price"] else row["price"] or 0)
            ts = row["timestamp"]
            if qty <= 0 or price <= 0:
                continue
            if act == "BUY":
                open_lots[sym].append({"qty": qty, "price": price, "ts": ts})
            elif (act.startswith("SELL") or act.startswith("PARTIAL_SELL")
                  or act in ("EMERGENCY_SELL", "FORCE_DELEVER",
                             "REDUCE", "TAKE_PROFIT", "STOP_OUT")
                  or _is_filled_trail_stop(row, act)):
                # STOP_OUT (added 2026-08-28, ONDS/CCJ) is written by
                # _reconcile_stop_out_fills ONLY once the broker has already
                # confirmed the fill — unlike TRAIL_STOP, which is written
                # at placement and might never fire — so it needs no
                # analogous "_is_filled_stop_out" guard: every STOP_OUT row
                # that exists at all is, by construction, a realized exit.
                #
                # A FILLED TRAIL_STOP is a realized exit — the broker sold the
                # shares. Omitting it (2026-07-16 audit) left phantom open lots
                # for every stop-out and no close at all: LLY BUY8 → stop-filled
                # 8 → BUY6 → stop-filled 6 read as 14 shares still held and zero
                # LLY trades closed, while the position was flat. The win_rate /
                # avg_return / avg_hold_days this function produces feed PM as
                # facts and the reviewer as calibration_note — on the real
                # ledger the omission moved win_rate 22.2% → 30.0% and
                # avg_return −2.79% → −2.18% for a typical window. The
                # filled-guard mirrors _build_post_exit_reality: a placed-but-
                # unfilled TRAIL_STOP is protection, not an exit.
                # Close from oldest lot first
                remaining = qty
                lots = open_lots[sym]
                while remaining > 0 and lots:
                    lot = lots[0]
                    closed_qty = min(lot["qty"], remaining)
                    try:
                        buy_dt = datetime.fromisoformat(lot["ts"].replace(" ", "T"))
                        sell_dt = datetime.fromisoformat(ts.replace(" ", "T"))
                        hold_days = max(0, (sell_dt - buy_dt).days)
                    except (ValueError, TypeError):
                        hold_days = 0
                    ret_pct = (price / lot["price"] - 1) * 100 if lot["price"] > 0 else 0
                    entry_usd = closed_qty * lot["price"]
                    # Window applies to the EXIT date only: lots seed from
                    # full history (see the SELECT above), FIFO state always
                    # advances, but only exits inside the lookback count as
                    # "recent closed trades".
                    try:
                        sell_age_days = (datetime.utcnow() - sell_dt).days
                    except (TypeError, ValueError, UnboundLocalError):
                        sell_age_days = 0
                    if sell_age_days > lookback_days:
                        lot["qty"] -= closed_qty
                        remaining -= closed_qty
                        if lot["qty"] <= 1e-9:
                            lots.pop(0)
                        continue
                    closed.append({
                        "symbol": sym,
                        "return_pct": ret_pct,
                        "hold_days": hold_days,
                        "entry_usd": entry_usd,
                    })
                    lot["qty"] -= closed_qty
                    if lot["qty"] <= 1e-9:
                        lots.pop(0)
                    remaining -= closed_qty
        if len(closed) < 3:
            return {}

        def _bucket_stats(bucket: list[dict]) -> dict:
            if not bucket:
                return {"n": 0}
            n = len(bucket)
            wins = sum(1 for c in bucket if c["return_pct"] > 0)
            avg_ret = sum(c["return_pct"] for c in bucket) / n
            avg_hold = sum(c["hold_days"] for c in bucket) / n
            return {
                "n": n,
                "win_rate_pct": round(wins / n * 100, 1),
                "avg_return_pct": round(avg_ret, 2),
                "avg_hold_days": round(avg_hold, 1),
            }

        large = [c for c in closed if c["entry_usd"] >= 10_000]
        medium = [c for c in closed if 5_000 <= c["entry_usd"] < 10_000]
        small = [c for c in closed if c["entry_usd"] < 5_000]

        overall = _bucket_stats(closed)
        return {
            **overall,
            "by_size": {
                "large (≥$10k)": _bucket_stats(large),
                "medium ($5-10k)": _bucket_stats(medium),
                "small (<$5k)": _bucket_stats(small),
            },
            "lookback_days": lookback_days,
        }

    def get_recent_agent_outputs(self, agent_name: str, limit: int = 5,
                                 before_date: str | None = None) -> list[dict]:
        """Last N agent_logs rows for agent_name, newest first.

        Used by PM for self-calibration: reading its own recent decisions and
        reading RM's recent verdicts on those decisions. `before_date` (ISO
        'YYYY-MM-DD') skips the in-progress run so PM doesn't accidentally
        read a log it just wrote in the same pipeline tick.

        `before_date` is interpreted as an ET trading-day key (the rest of the
        system uses ET day boundaries — see `session_date_key`). It's converted
        to the UTC instant for "00:00 ET on that date" before comparing
        against `timestamp`, because SQLite's default `datetime('now')` writes
        UTC. A naive `date(timestamp) < before_date` compares UTC-date against
        ET-date and drops rows whose UTC date has ticked over ahead of ET —
        specifically, logs written within the last few hours of ET-today that
        already carry a UTC-tomorrow timestamp.
        """
        conditions = ["agent_name = ?"]
        params: list = [agent_name]
        if before_date:
            from datetime import datetime as _dt, timezone as _tz
            try:
                et_midnight = _dt.fromisoformat(before_date).replace(tzinfo=ET)
                utc_cutoff = et_midnight.astimezone(_tz.utc).strftime(
                    "%Y-%m-%d %H:%M:%S"
                )
                conditions.append("timestamp < ?")
                params.append(utc_cutoff)
            except (ValueError, TypeError) as exc:
                # before_date couldn't be parsed as an ISO date, so we
                # cannot convert it to the ET→UTC cutoff the main path uses.
                # The old fallback (`date(timestamp) < before_date`) compared
                # a UTC calendar date against an ET key — the exact bug this
                # docstring warns about — and could silently drop/keep the
                # wrong rows. All production callers pass session_date_key()
                # (always valid ISO), so this branch is unreachable in
                # practice; degrade by skipping the date filter entirely
                # rather than applying a known-wrong comparison.
                logger.warning(
                    "get_recent_agent_outputs: unparseable before_date=%r (%s); "
                    "skipping the date filter (returning most-recent rows "
                    "unfiltered) to avoid a UTC-vs-ET mismatch",
                    before_date, exc,
                )
        where = "WHERE " + " AND ".join(conditions)
        with self._lock:
            rows = self.conn.execute(
                f"SELECT agent_name, timestamp, full_response, output_summary "
                f"FROM agent_logs {where} ORDER BY timestamp DESC LIMIT ?",
                (*params, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def get_latest_insights(self, before_date: str | None = None) -> dict | None:
        if before_date:
            sql = "SELECT * FROM insights WHERE date < ? ORDER BY date DESC LIMIT 1"
            params: tuple = (before_date,)
        else:
            sql = "SELECT * FROM insights ORDER BY date DESC LIMIT 1"
            params = ()
        with self._lock:
            row = self.conn.execute(sql, params).fetchone()
        return dict(row) if row else None

    def close(self):
        if self.conn:
            self.conn.close()
