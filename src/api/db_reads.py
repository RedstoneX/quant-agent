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

import sqlite3
from datetime import date, datetime, time, timedelta
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
            d["total_cost_usd"] = _cost_rows_to_total(cost_rows)
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
        total_cost_usd = _cost_rows_to_total(cost_rows)
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
