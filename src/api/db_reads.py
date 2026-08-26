"""Read-only SQLite access for the Mission Control API's history routes.

CRITICAL SAFETY INVARIANT: this module never imports or instantiates
`src.storage.db.Database` (the trading process's read/WRITE class). Instead
every function here opens its own independent connection with SQLite's
`mode=ro` URI flag — a structural, OS-enforced guarantee that even a future
bug that accidentally added an INSERT/UPDATE/DELETE call in this file would
fail loudly at the database layer rather than silently corrupt trading
state. Every query below is a plain parameterized `SELECT`; nothing here
ever calls `.commit()` or executes DDL/DML.

The trading process runs in WAL mode (`src/storage/db.py` sets
`PRAGMA journal_mode=WAL` at `initialize()`), which is exactly what makes a
second, independent read-only connection to the same file safe to use
concurrently with the writer — no locking coordination needed here.

Two functions (`session_prefixes_logged_on`, `get_recent_daily_pnl`) are a
shared interface also imported by `src.api.routes_live` (built by another
worker against these exact names/signatures) — do not rename or change
their signatures.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from src.api.deps import get_db_path
from src.trading_calendar import et_today

ET = ZoneInfo("America/New_York")


def _connect() -> sqlite3.Connection:
    path = get_db_path()
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=2000")
    return conn


def _sqlite_utc_timestamp(when: datetime) -> str:
    """Format a datetime the same way SQLite stores `datetime('now')`.

    Mirrors `src/storage/db.py:Database._sqlite_utc_timestamp` exactly —
    trades/agent_logs timestamps are naive UTC strings, so converting ET
    day boundaries into this format is what lets an ET-day filter work
    regardless of host timezone.
    """
    from datetime import UTC
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return when.astimezone(UTC).replace(tzinfo=None).strftime("%Y-%m-%d %H:%M:%S")


def _et_day_utc_bounds(trading_day: date | None = None) -> tuple[str, str]:
    """UTC timestamp bounds [start, end) for an ET trading-day date.

    Mirrors `src/storage/db.py:Database._et_day_utc_bounds` exactly.
    """
    day = trading_day or et_today()
    start_et = datetime.combine(day, time.min, tzinfo=ET)
    end_et = start_et + timedelta(days=1)
    return _sqlite_utc_timestamp(start_et), _sqlite_utc_timestamp(end_et)


def _executed_trade_predicate() -> str:
    """SQL predicate for trades that executed at least some quantity.

    Mirrors `src/storage/db.py:Database._executed_trade_predicate` exactly.
    """
    return (
        "((fill_status IS NULL AND action != 'HOLD') OR fill_status = 'filled' "
        "OR COALESCE(fill_qty, 0) > 0)"
    )


def is_executed_trade(row: dict) -> bool:
    """Python-side equivalent of `_executed_trade_predicate()` for callers
    that already have a trades row in hand (e.g. `get_run_funnel`'s
    route-layer grouping) rather than filtering via SQL. Keep the two
    definitions in sync — this mirrors the SQL predicate exactly."""
    fill_status = row.get("fill_status")
    action = row.get("action")
    fill_qty = row.get("fill_qty") or 0
    return (fill_status is None and action != "HOLD") or fill_status == "filled" or fill_qty > 0


def get_llm_circuit_health() -> dict:
    """Read decision-path/cost-circuit health without acquiring write access."""

    conn = None
    try:
        conn = _connect()
        tables = {
            row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        result = {
            "available": "llm_circuit_state" in tables,
            "suspended": None,
            "suspension_class": None,
            "hold_scope": None,
            "requires_operator_reset": False,
            "auto_rearm": False,
            "trigger": None,
            "trigger_code": None,
            "suspended_at": None,
            "daily_cost_usd": None,
            "daily_limit_usd": None,
            "active_quota_holds": [],
            "recent_recovery": None,
            "recent_pm_status": None,
            "recent_pm_run_id": None,
            "recent_pm_timestamp": None,
            "recent_agent_statuses": {},
        }
        if "llm_circuit_state" in tables:
            state = conn.execute(
                "SELECT suspended, trigger_code, trigger_detail, suspended_at, "
                "daily_limit_usd "
                "FROM llm_circuit_state WHERE singleton=1"
            ).fetchone()
            if state:
                result.update(
                    available=True,
                    suspended=bool(state["suspended"]),
                    suspension_class="hard" if state["suspended"] else None,
                    hold_scope="global" if state["suspended"] else None,
                    requires_operator_reset=bool(state["suspended"]),
                    trigger=state["trigger_detail"],
                    trigger_code=state["trigger_code"],
                    suspended_at=state["suspended_at"],
                    daily_limit_usd=state["daily_limit_usd"],
                )
            else:
                result["available"] = False
        if "llm_quota_holds" in tables:
            current_day = et_today().isoformat()
            holds = [
                dict(row) for row in conn.execute(
                    "SELECT scope, day, trigger_code, trigger_detail, run_id, mode, "
                    "agent_name, session_cost_usd, daily_cost_usd, daily_limit_usd, "
                    "created_at FROM llm_quota_holds WHERE active=1 AND day=? "
                    "ORDER BY CASE scope WHEN 'day' THEN 1 WHEN 'mode_day' THEN 2 "
                    "ELSE 3 END, id",
                    (current_day,),
                ).fetchall()
            ]
            result["active_quota_holds"] = holds
            global_day_hold = next(
                (hold for hold in holds if hold["scope"] == "day"), None,
            )
            representative = global_day_hold or (holds[0] if holds else None)
            if not result["suspended"] and representative is not None:
                result.update(
                    suspended=bool(global_day_hold),
                    suspension_class="quota",
                    hold_scope=representative["scope"],
                    requires_operator_reset=False,
                    auto_rearm=representative["scope"] in {"day", "mode_day"},
                    trigger=representative["trigger_detail"],
                    trigger_code=representative["trigger_code"],
                    suspended_at=representative["created_at"],
                    daily_limit_usd=(
                        representative["daily_limit_usd"]
                        if representative["daily_limit_usd"] is not None
                        else result["daily_limit_usd"]
                    ),
                )
            recovery = conn.execute(
                "SELECT scope, day, trigger_code, mode, released_at, release_reason "
                "FROM llm_quota_holds WHERE active=0 AND released_at IS NOT NULL "
                "ORDER BY released_at DESC, id DESC LIMIT 1"
            ).fetchone()
            if recovery is not None:
                result["recent_recovery"] = dict(recovery)
        # Accounting failures use an atomic sidecar because SQLite itself may
        # be the failed component.  The API is a separate process, so reading
        # only llm_circuit_state could otherwise report OK while every trading
        # process is correctly fail-closed on this durable marker.
        db_file = Path(get_db_path())
        emergency_latch = db_file.with_name(
            f"{db_file.name}.llm-circuit-unavailable"
        )
        if emergency_latch.exists():
            try:
                payload = json.loads(emergency_latch.read_text(encoding="utf-8"))
                detail = str(payload.get("error") or "persistent accounting failure")
            except Exception as exc:
                detail = (
                    "durable accounting-failure latch is unreadable: "
                    f"{type(exc).__name__}: {str(exc)[:200]}"
                )
            result.update(
                available=False,
                suspended=True,
                suspension_class="hard",
                hold_scope="global",
                requires_operator_reset=True,
                auto_rearm=False,
                trigger_code="circuit_infrastructure_unavailable",
                trigger=f"cost-circuit infrastructure unavailable: {detail}",
            )
        if "llm_budget_days" in tables:
            row = conn.execute(
                "SELECT baseline_cost_usd + incremental_cost_usd AS cost "
                "FROM llm_budget_days WHERE day=?",
                (et_today().isoformat(),),
            ).fetchone()
            if row:
                result["daily_cost_usd"] = row["cost"]
        if "agent_logs" in tables:
            utc_start, utc_end = _et_day_utc_bounds()
            columns = {
                row[1] for row in conn.execute("PRAGMA table_info(agent_logs)").fetchall()
            }
            status_expr = "status" if "status" in columns else "NULL"
            row = conn.execute(
                f"SELECT run_id, timestamp, {status_expr} AS status "
                "FROM agent_logs WHERE agent_name='portfolio_manager' "
                "AND timestamp >= ? AND timestamp < ? "
                "ORDER BY timestamp DESC, id DESC LIMIT 1"
                , (utc_start, utc_end)
            ).fetchone()
            if row:
                result.update(
                    recent_pm_status=row["status"],
                    recent_pm_run_id=row["run_id"],
                    recent_pm_timestamp=row["timestamp"],
                )
            for agent_name in (
                "portfolio_manager", "risk_manager",
                "position_reviewer", "evening_analyst",
            ):
                latest = conn.execute(
                    f"SELECT run_id, timestamp, {status_expr} AS status "
                    "FROM agent_logs WHERE agent_name=? "
                    "AND timestamp >= ? AND timestamp < ? "
                    "ORDER BY timestamp DESC, id DESC LIMIT 1",
                    (agent_name, utc_start, utc_end),
                ).fetchone()
                if latest:
                    result["recent_agent_statuses"][agent_name] = {
                        "status": latest["status"],
                        "run_id": latest["run_id"],
                        "timestamp": latest["timestamp"],
                    }
        return result
    finally:
        if conn is not None:
            conn.close()


def get_trades(
    symbol: str | None = None,
    run_id: str | None = None,
    decision_id: str | None = None,
    today_only: bool = False,
    executed_only: bool = False,
    limit: int = 100,
) -> list[dict]:
    """SELECT * FROM trades with optional AND-ed filters, newest first.

    `today_only` is implemented via the same ET-day-to-UTC-bounds math as
    `Database.get_trades` (correct, not skipped) — see `_et_day_utc_bounds`.
    """
    conditions: list[str] = []
    params: list = []
    if symbol:
        conditions.append("symbol = ?")
        params.append(symbol)
    if run_id:
        conditions.append("run_id = ?")
        params.append(run_id)
    if decision_id:
        conditions.append("decision_id = ?")
        params.append(decision_id)
    if today_only:
        start_utc, end_utc = _et_day_utc_bounds()
        conditions.append("timestamp >= ? AND timestamp < ?")
        params.extend([start_utc, end_utc])
    if executed_only:
        conditions.append(_executed_trade_predicate())
    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    conn = None
    try:
        conn = _connect()
        rows = conn.execute(
            f"SELECT * FROM trades {where} ORDER BY timestamp DESC, id DESC LIMIT ?",
            (*params, limit),
        ).fetchall()
        return [dict(row) for row in rows]
    except sqlite3.Error:
        return []
    finally:
        if conn is not None:
            conn.close()


def _cost_rows_to_total(rows) -> float | None:
    """Any-null-means-None convention, matching `Database.sum_session_cost`."""
    if not rows:
        return None
    if any(r["cost_usd"] is None for r in rows):
        return None
    return sum(float(r["cost_usd"]) for r in rows)


def _canonical_run_cost(conn: sqlite3.Connection, run_id: str, agent_cost_rows) -> float | None:
    """Prefer the exact mandatory-circuit ledger over agent-log attribution.

    A paid call can settle successfully and then the enclosing stage can stop
    before its merged ``agent_logs`` row is written.  Summing those rows would
    report a precise-looking partial cost.  The circuit session is the shared
    accounting authority and already includes every settled provider call.
    Legacy databases/runs without that row retain the prior agent-log behavior.
    """
    try:
        row = conn.execute(
            "SELECT actual_cost_usd, costs_exact FROM llm_budget_sessions "
            "WHERE run_id = ?",
            (run_id,),
        ).fetchone()
    except sqlite3.OperationalError:
        row = None
    if row is None:
        return _cost_rows_to_total(agent_cost_rows)
    if not bool(row["costs_exact"]):
        return None
    return float(row["actual_cost_usd"])


def get_recent_runs(limit: int = 20) -> list[dict]:
    """Per-run_id summary from agent_logs, newest-last-activity first.

    total_cost_usd follows Database.sum_session_cost's any-unknown-means-
    unknown convention: if any agent_logs row for that run_id has a NULL
    cost_usd, the reported total for that run is None (never a misleading
    partial sum).
    """
    conn = None
    try:
        conn = _connect()
        rows = conn.execute(
            "SELECT run_id, MIN(timestamp) as first_timestamp, "
            "MAX(timestamp) as last_timestamp, COUNT(*) as agent_count, "
            "MAX(decision_id) as decision_id FROM agent_logs "
            "GROUP BY run_id ORDER BY MAX(timestamp) DESC LIMIT ?",
            (limit,),
        ).fetchall()
        out: list[dict] = []
        for row in rows:
            d = dict(row)
            run_id = d["run_id"] or ""
            d["session_prefix"] = run_id.rsplit("-", 1)[0] if "-" in run_id else run_id
            cost_rows = conn.execute(
                "SELECT cost_usd FROM agent_logs WHERE run_id = ?",
                (d["run_id"],),
            ).fetchall()
            d["total_cost_usd"] = _canonical_run_cost(
                conn, d["run_id"], cost_rows,
            )
            out.append(d)
        return out
    except sqlite3.Error:
        return []
    finally:
        if conn is not None:
            conn.close()


def get_run_detail(run_id: str) -> dict:
    """All agent_logs + trades rows for one run_id, plus derived summary
    fields. Returns empty lists / None fields (not an exception) when the
    run_id matches nothing — the route layer decides whether that's a 404.
    """
    conn = None
    try:
        conn = _connect()
        agent_logs = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM agent_logs WHERE run_id = ? ORDER BY timestamp",
                (run_id,),
            ).fetchall()
        ]
        trades = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM trades WHERE run_id = ? ORDER BY timestamp",
                (run_id,),
            ).fetchall()
        ]
        decision_id = None
        for log in agent_logs:
            if log.get("decision_id"):
                decision_id = log["decision_id"]
                break
        cost_rows = conn.execute(
            "SELECT cost_usd FROM agent_logs WHERE run_id = ?", (run_id,),
        ).fetchall()
        total_cost_usd = _canonical_run_cost(conn, run_id, cost_rows)
        return {
            "agent_logs": agent_logs,
            "trades": trades,
            "decision_id": decision_id,
            "total_cost_usd": total_cost_usd,
        }
    except sqlite3.Error:
        return {"agent_logs": [], "trades": [], "decision_id": None, "total_cost_usd": None}
    finally:
        if conn is not None:
            conn.close()


def get_decision_detail(decision_id: str) -> dict:
    """The PM/RM agent_logs rows and all trades tied to one decision_id.

    `hard_risk_block` (Stage 2 Checkpoint C) is the forensic `agent_logs`
    row `TradingPipeline._persist_hard_risk_block` writes with the
    sentinel `agent_name="risk_gate"` when the deterministic hard-risk
    gate blocked every candidate before `risk_manager` was ever called —
    `risk_manager` stays `None` in exactly that case, since no LLM call
    happened. It is `None` for every ordinary (RM-reached) decision.
    """
    conn = None
    try:
        conn = _connect()
        pm_row = conn.execute(
            "SELECT * FROM agent_logs WHERE decision_id = ? AND agent_name = 'portfolio_manager' "
            "LIMIT 1",
            (decision_id,),
        ).fetchone()
        rm_row = conn.execute(
            "SELECT * FROM agent_logs WHERE decision_id = ? AND agent_name = 'risk_manager' "
            "LIMIT 1",
            (decision_id,),
        ).fetchone()
        hard_risk_block_row = conn.execute(
            "SELECT * FROM agent_logs WHERE decision_id = ? AND agent_name = 'risk_gate' "
            "LIMIT 1",
            (decision_id,),
        ).fetchone()
        trades = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM trades WHERE decision_id = ? ORDER BY timestamp",
                (decision_id,),
            ).fetchall()
        ]
        return {
            "portfolio_manager": dict(pm_row) if pm_row is not None else None,
            "risk_manager": dict(rm_row) if rm_row is not None else None,
            "hard_risk_block": dict(hard_risk_block_row) if hard_risk_block_row is not None else None,
            "trades": trades,
        }
    except sqlite3.Error:
        return {
            "portfolio_manager": None, "risk_manager": None,
            "hard_risk_block": None, "trades": [],
        }
    finally:
        if conn is not None:
            conn.close()


def get_recent_agent_logs(agent_name: str, limit: int = 20) -> list[dict]:
    conn = None
    try:
        conn = _connect()
        rows = conn.execute(
            "SELECT * FROM agent_logs WHERE agent_name = ? ORDER BY timestamp DESC LIMIT ?",
            (agent_name, limit),
        ).fetchall()
        return [dict(row) for row in rows]
    except sqlite3.Error:
        return []
    finally:
        if conn is not None:
            conn.close()


def get_recent_insights(limit: int = 7) -> list[dict]:
    conn = None
    try:
        conn = _connect()
        rows = conn.execute(
            "SELECT * FROM insights ORDER BY date DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(row) for row in rows]
    except sqlite3.Error:
        return []
    finally:
        if conn is not None:
            conn.close()


def get_watchlist_candidates(lookback_days: int = 30) -> list[dict]:
    """Symbols the evening analyst has repeatedly flagged as `add`/`watch`
    for universe expansion — same canonical output as
    `TradingPipeline._build_watchlist_candidates` (Stage 2 Checkpoint C
    candidates gap).

    Computed from the read-only `get_recent_insights` query above plus the
    pure aggregator in `src.watchlist_candidates` — `TradingPipeline`/
    trading-execution code is never imported into `src/api`.
    """
    rows = get_recent_insights(limit=lookback_days + 5)
    if not rows:
        return []
    from src.watchlist_candidates import build_watchlist_candidates
    return build_watchlist_candidates(rows, lookback_days)


def get_recent_daily_pnl(limit: int = 30) -> list[dict]:
    """SELECT * FROM daily_pnl ORDER BY date DESC LIMIT ?.

    Shared name/signature — imported directly by `src.api.routes_live`.
    """
    conn = None
    try:
        conn = _connect()
        rows = conn.execute(
            "SELECT * FROM daily_pnl ORDER BY date DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(row) for row in rows]
    except sqlite3.Error:
        return []
    finally:
        if conn is not None:
            conn.close()


def session_prefixes_logged_on() -> list[str]:
    """Distinct run_id prefixes (e.g. "run", "midday") logged in agent_logs
    today (ET trading day). Mirrors `Database.session_prefixes_logged_on`'s
    default-today behavior.

    Shared name — imported directly by `src.api.routes_live`'s /health
    route, which calls this expecting it may legitimately return empty.
    MUST NEVER RAISE: every failure mode collapses to `[]`.
    """
    try:
        start_utc, end_utc = _et_day_utc_bounds()
        conn = _connect()
        try:
            rows = conn.execute(
                "SELECT DISTINCT run_id FROM agent_logs WHERE timestamp >= ? AND timestamp < ?",
                (start_utc, end_utc),
            ).fetchall()
        finally:
            conn.close()
        prefixes: set[str] = set()
        for r in rows:
            rid = r[0] or ""
            prefixes.add(rid.rsplit("-", 1)[0] if "-" in rid else rid)
        return sorted(prefixes)
    except Exception:
        return []


def get_specialist_evidence(
    run_id: str | None = None,
    decision_id: str | None = None,
    symbol: str | None = None,
    scope: str | None = None,
    agent_name: str | None = None,
    kind: str | None = None,
) -> list[dict]:
    """SELECT * FROM specialist_evidence with optional AND-ed filters
    (Stage 4). Same shape/limits-free convention as get_trades — callers
    scope with run_id/symbol so result sets stay small (one run's worth
    of specialist calls, at most tens of rows)."""
    conditions: list[str] = []
    params: list = []
    if run_id:
        conditions.append("run_id = ?")
        params.append(run_id)
    if decision_id:
        conditions.append("decision_id = ?")
        params.append(decision_id)
    if symbol:
        conditions.append("symbol = ?")
        params.append(symbol)
    if scope:
        conditions.append("scope = ?")
        params.append(scope)
    if agent_name:
        conditions.append("agent_name = ?")
        params.append(agent_name)
    if kind:
        conditions.append("kind = ?")
        params.append(kind)
    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    conn = None
    try:
        conn = _connect()
        rows = conn.execute(
            f"SELECT * FROM specialist_evidence {where} ORDER BY id",
            params,
        ).fetchall()
        return [dict(row) for row in rows]
    except sqlite3.Error:
        return []
    finally:
        if conn is not None:
            conn.close()


def get_run_candidates(run_id: str) -> list[str]:
    """Distinct symbols "considered" in a run (Stage 4 per-candidate view):
    the union of every symbol-scoped `specialist_evidence` row and every
    `trades` row for this run_id. Natural-scope union, not a re-derived
    universe/watchlist concept (see get_watchlist_candidates, a distinct
    Stage 2 feature over `insights`, not this table)."""
    conn = None
    try:
        conn = _connect()
        ev_rows = conn.execute(
            "SELECT DISTINCT symbol FROM specialist_evidence "
            "WHERE run_id = ? AND scope = 'symbol' AND symbol IS NOT NULL",
            (run_id,),
        ).fetchall()
        trade_rows = conn.execute(
            "SELECT DISTINCT symbol FROM trades WHERE run_id = ?",
            (run_id,),
        ).fetchall()
        symbols = {r[0] for r in ev_rows} | {r[0] for r in trade_rows}
        return sorted(symbols)
    except sqlite3.Error:
        return []
    finally:
        if conn is not None:
            conn.close()


def get_run_funnel(run_id: str) -> dict:
    """Raw rows for Stage 6's decision-funnel aggregation: every
    `specialist_evidence` row for this run (both symbol- and run-scoped —
    the route layer separates them), every `trades` row, whether the
    forensic `agent_name="risk_gate"` hard-risk-block row is present (same
    sentinel as `RunDetailResponse.hard_risk_block_recorded`), and the
    run's first `agent_logs` timestamp. Pure read aggregation over
    existing Stage-4 tables — the route layer (`routes_evidence.py`) does
    all typed validation/grouping, this function only fetches."""
    conn = None
    try:
        conn = _connect()
        evidence_rows = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM specialist_evidence WHERE run_id = ? ORDER BY id",
                (run_id,),
            ).fetchall()
        ]
        trade_rows = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM trades WHERE run_id = ? ORDER BY timestamp",
                (run_id,),
            ).fetchall()
        ]
        hard_risk_block = conn.execute(
            "SELECT 1 FROM agent_logs WHERE run_id = ? AND agent_name = 'risk_gate' LIMIT 1",
            (run_id,),
        ).fetchone() is not None
        ts_row = conn.execute(
            "SELECT MIN(timestamp) as first_ts FROM agent_logs WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        first_timestamp = ts_row["first_ts"] if ts_row else None
        # A hard-risk-block run (blocked before risk_manager ever ran) can
        # legitimately have zero specialist_evidence rows and zero trades,
        # and conversely a run can have specialist_evidence with no
        # agent_logs row pinned to that exact run_id in a test fixture —
        # "run exists at all" must come from any of the three tables, never
        # from having candidates, or the exact "why no trade?" state this
        # endpoint exists to surface would 404 instead of rendering.
        run_exists = bool(evidence_rows) or bool(trade_rows) or first_timestamp is not None
        return {
            "specialist_evidence": evidence_rows,
            "trades": trade_rows,
            "hard_risk_block": hard_risk_block,
            "first_timestamp": first_timestamp,
            "run_exists": run_exists,
        }
    except sqlite3.Error:
        return {
            "specialist_evidence": [], "trades": [],
            "hard_risk_block": False, "first_timestamp": None,
            "run_exists": False,
        }
    finally:
        if conn is not None:
            conn.close()


def get_journal_dates(limit: int = 60) -> list[str]:
    """ET trading-day dates with any journal-worthy activity (Stage 5),
    newest first: the union of `insights.date` (evening reflection ran),
    `daily_pnl.date` (an equity snapshot was recorded) — both already
    ET-day-keyed strings (see `Database.save_evening_snapshot` /
    `session_date_key`), so no UTC->ET conversion is needed for those two —
    and every ET calendar date that has at least one recorded run
    (`agent_logs`, converted from its naive-UTC `timestamp`).

    The run-derived third source is not optional (2026-08-21 Mission
    Control correctness finding): a day with real runs/candidates but
    neither an evening reflection nor an equity snapshot yet was
    previously invisible here even though `/journal/{date}` already
    rendered it correctly once selected directly — a trading run must
    stay discoverable in history regardless of whether evening reflection
    or the daily equity snapshot ran."""
    conn = None
    try:
        conn = _connect()
        dates: set[str] = set()
        rows = conn.execute("SELECT date FROM insights UNION SELECT date FROM daily_pnl").fetchall()
        dates.update(r[0] for r in rows if r[0])

        run_rows = conn.execute(
            "SELECT MIN(timestamp) as first_ts FROM agent_logs "
            "WHERE run_id IS NOT NULL GROUP BY run_id"
        ).fetchall()
        for r in run_rows:
            ts = r["first_ts"]
            if not ts:
                continue
            try:
                naive_utc = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")
            except ValueError:
                continue
            dates.add(naive_utc.replace(tzinfo=UTC).astimezone(ET).date().isoformat())

        return sorted(dates, reverse=True)[:limit]
    except sqlite3.Error:
        return []
    finally:
        if conn is not None:
            conn.close()


def get_journal_day(date_str: str) -> dict:
    """Everything Mission Control has about one ET trading day (Stage 5
    journal): the evening reflection, the equity snapshot, every run's
    summary, every trade, and the union of candidate symbols considered
    that day. Read-only aggregation over existing/Stage-4 tables — no new
    authoritative state. Returns all-empty fields (not an exception) when
    the date has no data; the route layer decides whether that's a 404."""
    try:
        day = date.fromisoformat(date_str)
    except (TypeError, ValueError):
        return {
            "daily_pnl": None, "insights": None, "runs": [],
            "trades": [], "candidates": [],
        }
    conn = None
    try:
        conn = _connect()
        start_utc, end_utc = _et_day_utc_bounds(day)

        daily_pnl_row = conn.execute(
            "SELECT * FROM daily_pnl WHERE date = ?", (date_str,),
        ).fetchone()
        insights_row = conn.execute(
            "SELECT * FROM insights WHERE date = ?", (date_str,),
        ).fetchone()

        run_rows = conn.execute(
            "SELECT run_id, MIN(timestamp) as first_timestamp, "
            "MAX(timestamp) as last_timestamp, COUNT(*) as agent_count, "
            "MAX(decision_id) as decision_id FROM agent_logs "
            "WHERE timestamp >= ? AND timestamp < ? "
            "GROUP BY run_id ORDER BY MAX(timestamp)",
            (start_utc, end_utc),
        ).fetchall()
        runs: list[dict] = []
        for r in run_rows:
            d = dict(r)
            run_id = d["run_id"] or ""
            d["session_prefix"] = run_id.rsplit("-", 1)[0] if "-" in run_id else run_id
            cost_rows = conn.execute(
                "SELECT cost_usd FROM agent_logs WHERE run_id = ?", (d["run_id"],),
            ).fetchall()
            d["total_cost_usd"] = _canonical_run_cost(
                conn, d["run_id"], cost_rows,
            )
            runs.append(d)

        trade_rows = conn.execute(
            "SELECT * FROM trades WHERE timestamp >= ? AND timestamp < ? "
            "ORDER BY timestamp",
            (start_utc, end_utc),
        ).fetchall()
        trades = [dict(r) for r in trade_rows]

        candidate_rows = conn.execute(
            "SELECT DISTINCT symbol FROM specialist_evidence "
            "WHERE timestamp >= ? AND timestamp < ? "
            "AND scope = 'symbol' AND symbol IS NOT NULL",
            (start_utc, end_utc),
        ).fetchall()
        candidates = sorted({r[0] for r in candidate_rows} | {t["symbol"] for t in trades})

        return {
            "daily_pnl": dict(daily_pnl_row) if daily_pnl_row is not None else None,
            "insights": dict(insights_row) if insights_row is not None else None,
            "runs": runs,
            "trades": trades,
            "candidates": candidates,
        }
    except sqlite3.Error:
        return {
            "daily_pnl": None, "insights": None, "runs": [],
            "trades": [], "candidates": [],
        }
    finally:
        if conn is not None:
            conn.close()


def get_research_day(date_str: str) -> dict:
    """One consistent read-only snapshot for the Research Intelligence desk.

    Unlike the older convenience readers, this function does not conflate a
    SQLite failure with a genuinely quiet day: `read_error` is explicit and
    sanitized.  Raw agent rows are returned only to the typed route projection;
    that projection deliberately excludes prompts and `full_response`.
    """
    try:
        day = date.fromisoformat(date_str)
    except (TypeError, ValueError):
        return {"invalid_date": True, "read_error": None}

    conn = None
    try:
        conn = _connect()
        start_utc, end_utc = _et_day_utc_bounds(day)
        agent_logs = [dict(r) for r in conn.execute(
            "SELECT * FROM agent_logs WHERE timestamp >= ? AND timestamp < ? "
            "ORDER BY timestamp, id", (start_utc, end_utc),
        ).fetchall()]
        evidence = [dict(r) for r in conn.execute(
            "SELECT * FROM specialist_evidence WHERE timestamp >= ? AND timestamp < ? "
            "ORDER BY timestamp, id", (start_utc, end_utc),
        ).fetchall()]
        trades = [dict(r) for r in conn.execute(
            "SELECT * FROM trades WHERE timestamp >= ? AND timestamp < ? "
            "ORDER BY timestamp, id", (start_utc, end_utc),
        ).fetchall()]
        daily_pnl = conn.execute(
            "SELECT * FROM daily_pnl WHERE date = ?", (date_str,),
        ).fetchone()
        insights = conn.execute(
            "SELECT * FROM insights WHERE date = ?", (date_str,),
        ).fetchone()
        return {
            "invalid_date": False,
            "read_error": None,
            "agent_logs": agent_logs,
            "specialist_evidence": evidence,
            "trades": trades,
            "daily_pnl": dict(daily_pnl) if daily_pnl is not None else None,
            "insights": dict(insights) if insights is not None else None,
        }
    except sqlite3.Error:
        return {
            "invalid_date": False,
            "read_error": "research data unavailable",
            "agent_logs": [], "specialist_evidence": [], "trades": [],
            "daily_pnl": None, "insights": None,
        }
    finally:
        if conn is not None:
            conn.close()


def _escape_like(term: str) -> str:
    """Escape SQLite LIKE metacharacters in a user-supplied search term so
    it's matched literally (paired with `ESCAPE '\\'` below). This is what
    keeps the search endpoint from ever needing (or accepting) arbitrary
    SQL — the term is always a bound parameter, never interpolated into
    the query text; this only prevents a literal `%`/`_` in the SEARCH
    TERM from acting as an unintended wildcard."""
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def search(q: str, limit: int = 50) -> dict:
    """Forensic search (Stage 5) over trades + agent_logs via parameterized
    `LIKE` matching only — every value is a bound parameter; no query text
    is ever built from `q`. Returns `{"trades": [...], "agent_logs": [...]}`,
    each newest-first and independently capped at `limit`."""
    term = (q or "").strip()
    if not term:
        return {"trades": [], "agent_logs": []}
    like = f"%{_escape_like(term)}%"
    conn = None
    try:
        conn = _connect()
        trade_rows = conn.execute(
            "SELECT * FROM trades WHERE symbol LIKE ? ESCAPE '\\' "
            "OR reasoning LIKE ? ESCAPE '\\' "
            "ORDER BY timestamp DESC LIMIT ?",
            (like, like, limit),
        ).fetchall()
        agent_rows = conn.execute(
            "SELECT * FROM agent_logs WHERE agent_name LIKE ? ESCAPE '\\' "
            "OR model LIKE ? ESCAPE '\\' OR output_summary LIKE ? ESCAPE '\\' "
            "OR input_summary LIKE ? ESCAPE '\\' "
            "ORDER BY timestamp DESC LIMIT ?",
            (like, like, like, like, limit),
        ).fetchall()
        return {
            "trades": [dict(r) for r in trade_rows],
            "agent_logs": [dict(r) for r in agent_rows],
        }
    except sqlite3.Error:
        return {"trades": [], "agent_logs": []}
    finally:
        if conn is not None:
            conn.close()


def list_meta_periods() -> list[dict]:
    """Existence-only summary of each `data/evolution/{period}/` directory.

    Never parses digest.json/reflection.json/proposed_edits.json contents —
    those are LLM-generated free text, not this layer's concern. Returns
    `[]` if `data/evolution/` doesn't exist at all.
    """
    base = Path("data/evolution")
    if not base.is_dir():
        return []
    out: list[dict] = []
    for entry in sorted(base.iterdir()):
        if not entry.is_dir():
            continue
        out.append({
            "period": entry.name,
            "has_digest": (entry / "digest.json").is_file(),
            "has_reflection": (entry / "reflection.json").is_file(),
            "has_proposed_edits": (entry / "proposed_edits.json").is_file(),
        })
    return out
