import pytest
from datetime import datetime, date, time, timedelta
from src.storage.db import Database
from src.util.time import ET, UTC


@pytest.fixture
def db(tmp_path):
    db_path = tmp_path / "test.db"
    database = Database(str(db_path))
    database.initialize()
    yield database
    database.close()


def test_initialize_creates_tables(db):
    tables = db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    table_names = {row[0] for row in tables}
    assert "trades" in table_names
    assert "positions" in table_names
    assert "agent_logs" in table_names
    assert "daily_pnl" in table_names


def test_insert_and_query_trade(db):
    db.insert_trade(
        symbol="SPY",
        action="BUY",
        qty=10.0,
        price=500.0,
        reasoning="Test trade",
        run_id="run-001",
    )
    trades = db.get_trades(symbol="SPY")
    assert len(trades) == 1
    assert trades[0]["symbol"] == "SPY"
    assert trades[0]["qty"] == 10.0


def test_get_trades_today_only_uses_et_trading_day(db, monkeypatch):
    import src.storage.db as db_module

    utc_today = datetime.now(UTC).date()
    fake_et_day = utc_today - timedelta(days=1)
    monkeypatch.setattr(db_module, "et_today", lambda: fake_et_day)

    start_et = datetime.combine(fake_et_day, time.min, tzinfo=ET)
    within_early = start_et + timedelta(hours=12)
    within_late = start_et + timedelta(hours=23)
    outside_next = start_et + timedelta(days=1, hours=2)

    def _sqlite_ts(when: datetime) -> str:
        return when.astimezone(UTC).replace(tzinfo=None).strftime("%Y-%m-%d %H:%M:%S")

    rows = [
        ("EARLY", _sqlite_ts(within_early)),
        ("LATE", _sqlite_ts(within_late)),
        ("NEXT", _sqlite_ts(outside_next)),
    ]
    db.conn.executemany(
        "INSERT INTO trades (symbol, action, qty, price, reasoning, run_id, timestamp) "
        "VALUES (?, 'BUY', 1, 100, 'x', 'r1', ?)",
        rows,
    )
    db.conn.commit()

    trades = db.get_trades(today_only=True)
    assert [t["symbol"] for t in trades] == ["LATE", "EARLY"]


def test_upsert_position(db):
    db.upsert_position(
        symbol="SPY",
        qty=10.0,
        avg_entry=500.0,
        current_price=510.0,
        market_value=5100.0,
        unrealized_pnl=100.0,
        sector="ETF",
    )
    positions = db.get_positions()
    assert len(positions) == 1
    assert positions[0]["symbol"] == "SPY"

    # Update same symbol
    db.upsert_position(
        symbol="SPY",
        qty=20.0,
        avg_entry=505.0,
        current_price=510.0,
        market_value=10200.0,
        unrealized_pnl=100.0,
        sector="ETF",
    )
    positions = db.get_positions()
    assert len(positions) == 1
    assert positions[0]["qty"] == 20.0


def test_insert_agent_log(db):
    db.insert_agent_log(
        agent_name="tech_analyst",
        run_id="run-001",
        input_summary="SPY data",
        output_summary="Bullish",
        full_response='{"rating": "buy"}',
        model="claude-sonnet-4-6-20250514",
        tokens_used=1500,
    )
    logs = db.get_agent_logs(run_id="run-001")
    assert len(logs) == 1
    assert logs[0]["agent_name"] == "tech_analyst"


def test_insert_and_query_specialist_evidence(db):
    """Stage 4: additive, non-authoritative structured-evidence table.
    Round-trips a run-scoped and a symbol-scoped row, including decision_id
    correlation for the symbol-scoped one."""
    run_row_id = db.insert_specialist_evidence(
        run_id="run-001", agent_name="macro_analyst", kind="analysis",
        scope="run", evidence_json='{"regime": "risk-on"}',
    )
    assert run_row_id

    db.insert_specialist_evidence(
        run_id="run-001", agent_name="tech_analyst", kind="analysis",
        scope="symbol", symbol="SPY", evidence_json='{"rating": "buy"}',
    )
    db.insert_specialist_evidence(
        run_id="run-001", agent_name="portfolio_manager", kind="target",
        scope="symbol", symbol="SPY", decision_id="run-001-dec-abc123",
        evidence_json='{"target_weight_pct": 10.0}',
    )

    rows = db.execute(
        "SELECT * FROM specialist_evidence WHERE run_id = ? ORDER BY id",
        ("run-001",),
    ).fetchall()
    assert len(rows) == 3

    macro_row = rows[0]
    assert macro_row["agent_name"] == "macro_analyst"
    assert macro_row["scope"] == "run"
    assert macro_row["symbol"] is None
    assert macro_row["decision_id"] is None

    tech_row = rows[1]
    assert tech_row["scope"] == "symbol"
    assert tech_row["symbol"] == "SPY"
    assert tech_row["decision_id"] is None

    target_row = rows[2]
    assert target_row["decision_id"] == "run-001-dec-abc123"


def test_insert_daily_pnl(db):
    db.insert_daily_pnl(
        date="2026-04-07",
        total_value=10000.0,
        daily_pnl=150.0,
        daily_return_pct=1.5,
    )
    pnl = db.get_daily_pnl(limit=1)
    assert len(pnl) == 1
    assert pnl[0]["daily_pnl"] == 150.0


def test_get_daily_pnl_before_date_excludes_current_day(db):
    today_str = str(date.today())
    prev_day = str(date.today() - timedelta(days=1))

    db.insert_daily_pnl(date=prev_day, total_value=9500.0, daily_pnl=100.0, daily_return_pct=1.06)
    db.insert_daily_pnl(date=today_str, total_value=10000.0, daily_pnl=500.0, daily_return_pct=5.26)

    pnl = db.get_daily_pnl(limit=1, before_date=today_str)

    assert len(pnl) == 1
    assert pnl[0]["date"] == prev_day


def test_get_open_positions(db):
    db.upsert_position("SPY", 10.0, 500.0, 510.0, 5100.0, 100.0, "ETF")
    db.upsert_position("QQQ", 0.0, 400.0, 410.0, 0.0, 0.0, "ETF")
    open_pos = db.get_positions(open_only=True)
    assert len(open_pos) == 1
    assert open_pos[0]["symbol"] == "SPY"


def test_sync_positions_removes_closed_symbols(db):
    """sync_positions must drop rows for symbols no longer held."""
    from types import SimpleNamespace

    db.upsert_position("SPY", 10.0, 500.0, 510.0, 5100.0, 100.0, "ETF")
    db.upsert_position("QQQ", 5.0, 400.0, 410.0, 2050.0, 50.0, "ETF")
    assert len(db.get_positions()) == 2

    # Broker now reports only SPY — QQQ should be purged.
    snapshot = [SimpleNamespace(
        symbol="SPY", qty=12.0, avg_entry=502.0, current_price=515.0,
        market_value=6180.0, unrealized_pnl=156.0, sector="ETF",
    )]
    db.sync_positions(snapshot)

    remaining = db.get_positions()
    assert len(remaining) == 1
    assert remaining[0]["symbol"] == "SPY"
    assert remaining[0]["qty"] == 12.0


def test_sync_positions_empty_clears_table(db):
    from types import SimpleNamespace  # noqa: F401

    db.upsert_position("SPY", 10.0, 500.0, 510.0, 5100.0, 100.0, "ETF")
    db.sync_positions([])
    assert db.get_positions() == []


def test_prune_trades_respects_ttl(db):
    """Trades older than keep_days are dropped; recent ones are retained."""
    db.insert_trade("OLD", "BUY", 1.0, 100.0, "ancient", "r-old")
    db.conn.execute(
        "UPDATE trades SET timestamp = datetime('now', '-2000 days') WHERE symbol='OLD'"
    )
    db.conn.commit()
    db.insert_trade("RECENT", "BUY", 2.0, 200.0, "fresh", "r-new")

    deleted = db.prune_trades(keep_days=365 * 5)  # 5-year retention
    assert deleted == 1

    remaining = {r["symbol"] for r in db.get_trades()}
    assert remaining == {"RECENT"}


def test_has_pending_action_for_symbol_no_rows_returns_false(db):
    """Empty DB — nothing pending, safe to fire a fresh emergency sell."""
    assert db.has_pending_action_for_symbol("AMZN", "EMERGENCY_SELL") is False


def test_has_pending_action_for_symbol_matches_pending_row(db):
    """A submitted EMERGENCY_SELL with a broker_order_id is the exact case
    we need to detect: intra fired a -1% LIMIT, broker accepted it but the
    tape went through without filling, the row sits as 'submitted'. Next
    intra tick must see this and skip — no duplicate emergency sell."""
    db.insert_trade(
        symbol="AMZN", action="EMERGENCY_SELL", qty=51.0, price=230.0,
        reasoning="intra-session daily-loss breach", run_id="run-1",
        broker_order_id="alpaca-uuid-1", fill_status="submitted",
    )
    assert db.has_pending_action_for_symbol("AMZN", "EMERGENCY_SELL") is True


def test_has_pending_action_for_symbol_ignores_filled_row(db):
    """If the prior submission already terminal-filled, a new emergency sell
    is appropriate (residual position somehow grew, or we're on a
    different symbol). Don't block on completed history."""
    db.insert_trade(
        symbol="AMZN", action="EMERGENCY_SELL", qty=51.0, price=230.0,
        reasoning="prior fill", run_id="run-1",
        broker_order_id="alpaca-uuid-1", fill_status="filled",
    )
    assert db.has_pending_action_for_symbol("AMZN", "EMERGENCY_SELL") is False


def test_has_pending_action_for_symbol_ignores_row_without_broker_id(db):
    """A trade row without broker_order_id never reached Alpaca — no
    in-flight order to dedupe against. (Edge case: filter exists to
    keep the predicate symmetric with get_unreconciled_orders.)"""
    db.insert_trade(
        symbol="AMZN", action="EMERGENCY_SELL", qty=51.0, price=230.0,
        reasoning="never submitted", run_id="run-1",
        broker_order_id=None, fill_status="submitted",
    )
    assert db.has_pending_action_for_symbol("AMZN", "EMERGENCY_SELL") is False


def test_has_pending_action_for_symbol_scopes_by_symbol_and_action(db):
    """Another symbol's pending sell, or this symbol's pending REDUCE,
    must NOT block this symbol's EMERGENCY_SELL."""
    db.insert_trade(
        symbol="JPM", action="EMERGENCY_SELL", qty=10.0, price=300.0,
        reasoning="other symbol pending", run_id="run-1",
        broker_order_id="alpaca-jpm", fill_status="submitted",
    )
    db.insert_trade(
        symbol="AMZN", action="REDUCE", qty=10.0, price=230.0,
        reasoning="different action pending", run_id="run-1",
        broker_order_id="alpaca-amzn-reduce", fill_status="submitted",
    )
    assert db.has_pending_action_for_symbol("AMZN", "EMERGENCY_SELL") is False


def test_has_pending_action_for_symbol_today_only_drops_yesterday(db, monkeypatch):
    """Stale 'submitted' from a previous session shouldn't permanently
    block fresh exits today. today_only=True windows to current ET day."""
    import src.storage.db as db_module

    today = datetime.now(ET).date()
    yesterday = today - timedelta(days=1)
    monkeypatch.setattr(db_module, "et_today", lambda: today)

    yesterday_ts = (
        datetime.combine(yesterday, time(14, 0), tzinfo=ET)
        .astimezone(UTC).replace(tzinfo=None).strftime("%Y-%m-%d %H:%M:%S")
    )
    db.conn.execute(
        "INSERT INTO trades (symbol, action, qty, price, reasoning, run_id, "
        "broker_order_id, fill_status, timestamp) "
        "VALUES (?, 'EMERGENCY_SELL', 1, 100, 'stale', 'r-old', 'old-id', "
        "'submitted', ?)",
        ("AMZN", yesterday_ts),
    )
    db.conn.commit()

    assert db.has_pending_action_for_symbol("AMZN", "EMERGENCY_SELL") is False
    # Sanity: with today_only=False we DO see the stale row.
    assert (
        db.has_pending_action_for_symbol(
            "AMZN", "EMERGENCY_SELL", today_only=False,
        )
        is True
    )


def test_prune_agent_logs(db):
    """Old rows dropped; recent rows retained."""
    db.insert_agent_log(
        agent_name="old_agent", run_id="run-old", input_summary="old",
        output_summary="", full_response="", model="m", tokens_used=1,
    )
    # Force timestamp backdate on the just-inserted row.
    db.conn.execute(
        "UPDATE agent_logs SET timestamp = datetime('now', '-45 days') WHERE agent_name = 'old_agent'"
    )
    db.conn.commit()

    db.insert_agent_log(
        agent_name="recent_agent", run_id="run-new", input_summary="new",
        output_summary="", full_response="", model="m", tokens_used=1,
    )

    deleted = db.prune_agent_logs(keep_days=30)
    assert deleted == 1

    rows = db.conn.execute("SELECT agent_name FROM agent_logs").fetchall()
    names = {r[0] for r in rows}
    assert names == {"recent_agent"}


def test_prune_specialist_evidence(db):
    """Stage 4 table needs the same retention discipline as agent_logs —
    old rows dropped, recent rows retained."""
    db.insert_specialist_evidence(
        run_id="run-old", agent_name="macro_analyst", kind="analysis",
        scope="run", evidence_json='{"regime": "risk-on"}',
    )
    db.conn.execute(
        "UPDATE specialist_evidence SET timestamp = datetime('now', '-45 days') "
        "WHERE run_id = 'run-old'"
    )
    db.conn.commit()

    db.insert_specialist_evidence(
        run_id="run-new", agent_name="tech_analyst", kind="analysis",
        scope="symbol", symbol="AAPL", evidence_json='{"rating": "buy"}',
    )

    deleted = db.prune_specialist_evidence(keep_days=30)
    assert deleted == 1

    rows = db.conn.execute("SELECT run_id FROM specialist_evidence").fetchall()
    run_ids = {r[0] for r in rows}
    assert run_ids == {"run-new"}


def test_initialize_sets_synchronous_normal_under_wal(db):
    """WAL + synchronous=NORMAL is the trading-appropriate fsync mode:
    WAL synced on every commit, main DB synced only at checkpoint.
    Pin: pragma is actually applied at initialize() time."""
    val = db.conn.execute("PRAGMA synchronous").fetchone()[0]
    # SQLite returns 1 for NORMAL, 2 for FULL, 0 for OFF, 3 for EXTRA.
    assert val == 1, f"expected synchronous=NORMAL (1), got {val}"


def test_initialize_creates_timestamp_indexes_for_prune(db):
    """prune_trades / prune_agent_logs / prune_pending_protection_restores
    all scan WHERE <ts_col> < ?. Indexes turn full-table scans into
    O(log n). Pin: indexes exist after init."""
    rows = db.conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'idx_%'"
    ).fetchall()
    names = {r[0] for r in rows}
    assert "idx_trades_timestamp" in names
    assert "idx_agent_logs_timestamp" in names
    assert "idx_pending_protection_restores_created_at" in names
    assert "idx_specialist_evidence_run_id" in names


def test_prune_pending_protection_restores_drops_stale_rows(db):
    """A drain row that survives ~30 calendar days is operationally
    stuck (broker GC'd the order, position liquidated elsewhere, or
    malformed specs). Pin: prune deletes rows older than keep_days
    and logs each at INFO; rows within window are retained."""
    import json as _json

    # Insert two rows.
    fresh_id = db.insert_pending_protection_restore(
        symbol="NVDA", sell_order_id="ord-fresh",
        position_qty_before_sell=100.0,
        specs_json=_json.dumps([{"id": "s1", "qty": 100, "stop_price": 95.0}]),
    )
    stale_id = db.insert_pending_protection_restore(
        symbol="AAPL", sell_order_id="ord-stale",
        position_qty_before_sell=50.0,
        specs_json=_json.dumps([{"id": "s2", "qty": 50, "stop_price": 170.0}]),
    )
    # Backdate the stale row by 45 days.
    db.conn.execute(
        "UPDATE pending_protection_restores "
        "SET created_at = datetime('now', '-45 days') WHERE id = ?",
        (stale_id,),
    )
    db.conn.commit()

    deleted = db.prune_pending_protection_restores(keep_days=30)
    assert deleted == 1

    remaining = db.get_pending_protection_restores()
    assert len(remaining) == 1
    assert remaining[0]["id"] == fresh_id
    assert remaining[0]["symbol"] == "NVDA"


def test_prune_pending_protection_restores_is_noop_when_table_empty(db):
    """Defensive: empty table → 0 deleted, no SQL errors."""
    deleted = db.prune_pending_protection_restores(keep_days=30)
    assert deleted == 0


def test_prune_pending_protection_restores_keeps_rows_within_window(db):
    """Rows newer than keep_days survive prune untouched."""
    import json as _json

    db.insert_pending_protection_restore(
        symbol="NVDA", sell_order_id="ord-1",
        position_qty_before_sell=100.0,
        specs_json=_json.dumps([{"id": "s1", "qty": 100, "stop_price": 95.0}]),
    )
    db.insert_pending_protection_restore(
        symbol="AAPL", sell_order_id="ord-2",
        position_qty_before_sell=50.0,
        specs_json=_json.dumps([{"id": "s2", "qty": 50, "stop_price": 170.0}]),
    )

    deleted = db.prune_pending_protection_restores(keep_days=30)
    assert deleted == 0
    assert len(db.get_pending_protection_restores()) == 2


def test_sum_session_cost_aggregates_per_run_id(db):
    """Per-call costs land in agent_logs.cost_usd. The per-session sum
    feeds the Telegram push and any cost-monitoring tools."""
    db.insert_agent_log(
        agent_name="tech_analyst", run_id="run-sum",
        input_summary="x", output_summary="y", full_response="",
        model="claude-opus-4-7", tokens_used=110_000,
        input_tokens=80_000, output_tokens=30_000, cost_usd=3.45,
    )
    db.insert_agent_log(
        agent_name="portfolio_manager", run_id="run-sum",
        input_summary="x", output_summary="y", full_response="",
        model="claude-opus-4-7", tokens_used=52_000,
        input_tokens=50_000, output_tokens=2_000, cost_usd=0.90,
    )
    # Different run_id — must not be included.
    db.insert_agent_log(
        agent_name="risk_manager", run_id="run-other",
        input_summary="x", output_summary="y", full_response="",
        model="claude-opus-4-7", tokens_used=10_000,
        input_tokens=8_000, output_tokens=2_000, cost_usd=0.27,
    )
    total, count = db.sum_session_cost("run-sum")
    assert count == 2
    assert abs(total - 4.35) < 0.001


def test_sum_session_cost_returns_none_when_any_row_has_null(db):
    """If any agent in the session ran on a model not in cost_table.PRICING,
    its row stored NULL. Summing the known-only rows would silently
    understate — return None instead so the caller flags the gap."""
    db.insert_agent_log(
        agent_name="tech_analyst", run_id="run-mixed",
        input_summary="x", output_summary="y", full_response="",
        model="claude-opus-4-7", tokens_used=100_000,
        input_tokens=80_000, output_tokens=20_000, cost_usd=2.70,
    )
    db.insert_agent_log(
        agent_name="portfolio_manager", run_id="run-mixed",
        input_summary="x", output_summary="y", full_response="",
        model="some-future-model", tokens_used=52_000,
        input_tokens=50_000, output_tokens=2_000, cost_usd=None,
    )
    total, count = db.sum_session_cost("run-mixed")
    assert total is None
    assert count == 2


def test_sum_session_cost_zero_rows_returns_none(db):
    total, count = db.sum_session_cost("no-such-run")
    assert total is None
    assert count == 0


def test_prune_methods_reject_keep_days_zero_or_negative(db):
    """`datetime('now', '-0 days')` == 'now', which deletes EVERY row.
    A keep_days=0 typo would wipe years of trade history. All three
    prune methods must refuse non-positive values rather than silently
    nuking the table."""
    import pytest as _pytest

    # Seed a row in each table so we can confirm nothing was deleted.
    db.insert_trade(symbol="SPY", action="BUY", qty=1, price=500,
                    reasoning="seed", run_id="r0")
    db.insert_agent_log(agent_name="x", run_id="r0", input_summary="",
                        output_summary="", full_response="", model="m", tokens_used=0)
    import json as _json
    db.insert_pending_protection_restore(
        symbol="X", sell_order_id="o0", position_qty_before_sell=1.0,
        specs_json=_json.dumps([{"qty": 1, "stop_price": 1.0}]),
    )
    db.insert_specialist_evidence(
        run_id="r0", agent_name="macro_analyst", kind="analysis",
        scope="run", evidence_json="{}",
    )

    for kd in (0, -1, -365):
        with _pytest.raises(ValueError):
            db.prune_trades(keep_days=kd)
        with _pytest.raises(ValueError):
            db.prune_agent_logs(keep_days=kd)
        with _pytest.raises(ValueError):
            db.prune_pending_protection_restores(keep_days=kd)
        with _pytest.raises(ValueError):
            db.prune_specialist_evidence(keep_days=kd)

    # Seeded rows must still be there.
    assert len(db.get_trades(symbol="SPY")) == 1
    assert len(db.get_pending_protection_restores()) == 1
    assert len(db.execute("SELECT * FROM specialist_evidence").fetchall()) == 1


def test_initialize_sets_busy_timeout_pragma(db):
    """PRAGMA busy_timeout must be set so concurrent writes don't
    immediately raise OperationalError. Specifically: 09:30 ET morning
    + intra_check run as separate Python processes (intra is exempt
    from the bash session lock per CLAUDE.md). Both contend at the
    SQLite WAL level; threading.Lock in Database serializes within a
    process but does nothing across processes. busy_timeout=5000 gives
    a 5-second wait window for the loser to acquire the lock — covers
    the observed WAL→checkpoint stall plus headroom.
    """
    row = db.conn.execute("PRAGMA busy_timeout").fetchone()
    # PRAGMA busy_timeout returns the current timeout in ms.
    assert row[0] >= 5000, (
        f"busy_timeout must be >= 5000ms for cross-process contention "
        f"resilience; got {row[0]}"
    )


# ===========================================================================
# Write-ahead intent for BUY submission — audit F4
# ===========================================================================

def test_confirm_trade_submitted_updates_pending_row(db):
    """The write-ahead pattern inserts a pending_submit row BEFORE
    broker.submit_order; confirm_trade_submitted flips to submitted and
    attaches the broker_order_id once submit succeeds. This closes the
    BUY-side phantom-fill window — pre-fix, a crash between
    submit_order returning and insert_trade landing left broker with
    an accepted order and DB with no row, and _reconcile_fills had no
    way to find it (it queries by broker_order_id)."""
    row_id = db.insert_trade(
        symbol="NVDA", action="BUY", qty=10, price=150.0,
        reasoning="write-ahead", run_id="r1",
        fill_status="pending_submit",
        broker_order_id=None,
    )
    rows = db.conn.execute(
        "SELECT fill_status, broker_order_id FROM trades WHERE id = ?", (row_id,)
    ).fetchall()
    assert rows[0]["fill_status"] == "pending_submit"
    assert rows[0]["broker_order_id"] is None

    n = db.confirm_trade_submitted(row_id, broker_order_id="alpaca-12345")
    assert n == 1

    rows = db.conn.execute(
        "SELECT fill_status, broker_order_id FROM trades WHERE id = ?", (row_id,)
    ).fetchall()
    assert rows[0]["fill_status"] == "submitted"
    assert rows[0]["broker_order_id"] == "alpaca-12345"


def test_mark_trade_submit_failed_flags_pending_row(db):
    """When broker.submit_order raises OR _order_accepted returns False,
    the pending row gets flagged submit_failed (not 'rejected', which
    implies the broker accepted then rejected). Operator / reconcile
    sweeps these against the broker's order list by symbol + time."""
    row_id = db.insert_trade(
        symbol="NVDA", action="BUY", qty=10, price=150.0,
        reasoning="write-ahead", run_id="r1",
        fill_status="pending_submit",
        broker_order_id=None,
    )
    n = db.mark_trade_submit_failed(row_id)
    assert n == 1

    rows = db.conn.execute(
        "SELECT fill_status FROM trades WHERE id = ?", (row_id,)
    ).fetchall()
    assert rows[0]["fill_status"] == "submit_failed"


def test_pending_submit_row_distinguishable_from_orphan_terminal_states(db):
    """Reconcile needs to distinguish pending_submit (no broker_order_id
    yet, may or may not have reached broker) from submitted (has
    broker_order_id, broker accepted) from terminal states. This pins
    the four states the reconciler depends on:
        pending_submit  + broker_order_id IS NULL   → orphan to sweep
        submit_failed   + broker_order_id IS NULL   → known failed, may need broker check
        submitted       + broker_order_id IS NOT NULL → reconcile by broker_order_id
        filled/canceled/rejected/expired            → terminal, no further action
    """
    pending = db.insert_trade(
        "NVDA", "BUY", 10, 150.0, "x", "r1",
        fill_status="pending_submit", broker_order_id=None,
    )
    failed = db.insert_trade(
        "AAPL", "BUY", 10, 180.0, "x", "r1",
        fill_status="submit_failed", broker_order_id=None,
    )
    submitted = db.insert_trade(
        "TSLA", "BUY", 10, 200.0, "x", "r1",
        fill_status="submitted", broker_order_id="alpaca-1",
    )
    filled = db.insert_trade(
        "META", "BUY", 10, 500.0, "x", "r1",
        fill_status="filled", broker_order_id="alpaca-2",
    )

    # pending_submit + broker_order_id IS NULL is the orphan signature.
    orphans = db.conn.execute(
        "SELECT id FROM trades WHERE fill_status = 'pending_submit' "
        "AND broker_order_id IS NULL"
    ).fetchall()
    assert len(orphans) == 1 and orphans[0]["id"] == pending


def test_get_recent_agent_outputs_unparseable_before_date_skips_filter(db, caplog):
    """An unparseable before_date must NOT fall back to a timezone-naive
    `date(timestamp) < before_date` comparison (UTC-date vs ET-key — the
    documented bug). It skips the date filter and returns the most-recent
    rows instead. All production callers pass session_date_key() so this is
    defensive, but the old wrong comparison could silently drop every row."""
    import logging
    for i in range(2):
        db.insert_agent_log(
            agent_name="portfolio_manager", run_id=f"r{i}",
            input_summary="in", output_summary="out",
            full_response="{}", model="x", tokens_used=1,
        )
    # "0000-99-99": fromisoformat rejects it (→ fallback). A naive
    # `date(timestamp) < '0000-99-99'` is False for any real timestamp, so
    # the buggy fallback dropped ALL rows. Correct behavior keeps them.
    with caplog.at_level(logging.WARNING, logger="src.storage.db"):
        rows = db.get_recent_agent_outputs(
            "portfolio_manager", limit=5, before_date="0000-99-99",
        )
    assert len(rows) == 2, "unparseable before_date must not drop rows via a wrong filter"
    assert any("skipping the date filter" in r.getMessage() for r in caplog.records)


class _ConnProxy:
    """Wraps a real sqlite3 connection so a test can inject failures on
    .execute (the C method itself can't be monkeypatched)."""
    def __init__(self, real, on_execute):
        self._real = real
        self._on_execute = on_execute

    def execute(self, sql, *a, **k):
        return self._on_execute(self._real, sql, *a, **k)

    def commit(self):
        return self._real.commit()

    def __getattr__(self, name):
        return getattr(self._real, name)


def test_locked_write_retries_then_succeeds(db, monkeypatch):
    """A transient 'database is locked' (cross-process WAL contention that
    outlasts busy_timeout) must be retried, not lost. insert_agent_log used
    to silently drop the row on OperationalError."""
    import sqlite3 as _sql
    calls = {"n": 0}

    def flaky(real, sql, *a, **k):
        if sql.strip().upper().startswith("INSERT INTO AGENT_LOGS"):
            calls["n"] += 1
            if calls["n"] == 1:
                raise _sql.OperationalError("database is locked")
        return real.execute(sql, *a, **k)

    monkeypatch.setattr(db, "conn", _ConnProxy(db.conn, flaky))
    monkeypatch.setattr("time.sleep", lambda s: None)

    db.insert_agent_log(
        agent_name="portfolio_manager", run_id="rlock",
        input_summary="i", output_summary="o", full_response="{}",
        model="x", tokens_used=1,
    )
    # The row landed despite the first attempt hitting a lock.
    rows = db.get_recent_agent_outputs("portfolio_manager", limit=5)
    assert len(rows) == 1
    assert calls["n"] == 2, f"expected one retry after the lock; got {calls['n']} attempts"


def test_locked_write_reraises_non_lock_operational_error(db, monkeypatch):
    """A non-lock OperationalError (e.g. a real SQL/schema fault) must NOT be
    swallowed by the lock-retry path — it should propagate immediately."""
    import sqlite3 as _sql

    def boom(real, sql, *a, **k):
        if sql.strip().upper().startswith("INSERT INTO TRADES"):
            raise _sql.OperationalError("no such column: bogus")
        return real.execute(sql, *a, **k)

    monkeypatch.setattr(db, "conn", _ConnProxy(db.conn, boom))
    monkeypatch.setattr("time.sleep", lambda s: None)
    with pytest.raises(_sql.OperationalError, match="no such column"):
        db.insert_trade(
            symbol="NVDA", action="BUY", qty=1, price=1.0,
            reasoning="x", run_id="r",
        )


def test_session_prefixes_logged_on_extracts_run_id_prefixes(db):
    """The dead-man's check maps agent_logs run_id prefixes to sessions:
    'run-...'=morning, 'midday-...', 'close-...', etc."""
    db.insert_agent_log("tech_analyst", "run-aaaa1111", "i", "o", "{}", "m", 1)
    db.insert_agent_log("position_reviewer", "midday-bbbb2222", "i", "o", "{}", "m", 1)
    prefixes = db.session_prefixes_logged_on()
    assert "run" in prefixes      # morning ran
    assert "midday" in prefixes   # midday ran
    assert "close" not in prefixes  # close did NOT run today


def test_daily_pnl_equity_close_roundtrips(db):
    """equity_close (today's official 4pm close) persists + reads back."""
    db.insert_daily_pnl("2026-05-28", 100_400.0, -600.0, -0.59, equity_close=100_500.0)
    rows = db.get_daily_pnl(limit=1)
    assert rows[0]["equity_close"] == 100_500.0
    # legacy path (no equity_close) stores NULL, not a crash
    db.insert_daily_pnl("2026-05-29", 100_000.0, 0.0, 0.0)
    rows = db.get_daily_pnl(limit=1)
    assert rows[0]["equity_close"] is None


def test_daily_pnl_reinsert_preserves_equity_close_when_none(db):
    """[D] A same-day re-run whose 4pm fetch failed (equity_close=None) must NOT
    wipe the value captured by the first run; a real new value still overwrites."""
    db.insert_daily_pnl("2026-05-28", 100_400.0, -600.0, -0.59, equity_close=100_500.0)
    db.insert_daily_pnl("2026-05-28", 100_450.0, -550.0, -0.55, equity_close=None)
    row = db.get_daily_pnl(limit=1)[0]
    assert row["equity_close"] == 100_500.0   # preserved
    assert row["daily_pnl"] == -550.0         # other columns still updated
    db.insert_daily_pnl("2026-05-28", 100_450.0, -550.0, -0.55, equity_close=100_600.0)
    assert db.get_daily_pnl(limit=1)[0]["equity_close"] == 100_600.0  # real value overwrites


def test_backfill_equity_close_fills_null(db):
    """[API-lag self-heal] A NULL equity_close (yesterday's portfolio_history
    fetch hit the lag gap) gets filled once the API has caught up."""
    db.insert_daily_pnl("2026-05-29", 100_000.0, -50.0, -0.05)  # equity_close NULL
    assert db.backfill_equity_close("2026-05-29", 100_053.18) is True
    row = [r for r in db.get_daily_pnl(limit=5) if r["date"] == "2026-05-29"][0]
    assert row["equity_close"] == 100_053.18


def test_backfill_equity_close_never_overwrites_existing(db):
    """[API-lag self-heal] Must not clobber an already-captured 4pm close —
    backfill is a gap-filler, not a corrector."""
    db.insert_daily_pnl("2026-05-28", 100_400.0, -600.0, -0.59, equity_close=100_500.0)
    assert db.backfill_equity_close("2026-05-28", 999_999.0) is False
    row = [r for r in db.get_daily_pnl(limit=5) if r["date"] == "2026-05-28"][0]
    assert row["equity_close"] == 100_500.0  # untouched


def test_backfill_equity_close_no_row_for_date(db):
    """[API-lag self-heal] Backfilling a date with no daily_pnl row is a
    no-op, not a crash (e.g. lookback window predates the account's history)."""
    assert db.backfill_equity_close("2020-01-01", 100_000.0) is False


# === Stage 1 (QAMC provider/model/correlation plumbing) ===

def test_insert_agent_log_new_columns_roundtrip(db):
    db.insert_agent_log(
        agent_name="portfolio_manager",
        run_id="run-001",
        input_summary="s",
        output_summary="o",
        full_response="{}",
        model="claude-opus-4-7",
        tokens_used=100,
        requested_provider="openai",
        requested_model="gpt-5.5",
        actual_provider="anthropic",
        prompt_version="abc123",
        latency_s=12.5,
        status="fallback",
        finish_reason="end_turn",
        truncated=False,
        decision_id="run-001-dec-abc123",
    )
    logs = db.get_agent_logs(run_id="run-001")
    assert len(logs) == 1
    row = logs[0]
    assert row["requested_provider"] == "openai"
    assert row["requested_model"] == "gpt-5.5"
    assert row["actual_provider"] == "anthropic"
    assert row["prompt_version"] == "abc123"
    assert row["latency_s"] == 12.5
    assert row["status"] == "fallback"
    assert row["finish_reason"] == "end_turn"
    assert row["truncated"] == 0
    assert row["decision_id"] == "run-001-dec-abc123"


def test_insert_agent_log_new_columns_default_null_when_omitted(db):
    """A caller that doesn't pass the Stage 1 kwargs (any pre-Stage-1 caller,
    or a caller whose result had no attribution) persists NULL, not a
    fabricated value — per DECISION #12 / the 'unknown stays unknown' rule."""
    db.insert_agent_log(
        agent_name="macro_analyst", run_id="run-002", input_summary="s",
        output_summary="o", full_response="{}", model="m", tokens_used=1,
    )
    row = db.get_agent_logs(run_id="run-002")[0]
    for col in ("requested_provider", "requested_model", "actual_provider",
                "prompt_version", "latency_s", "status", "finish_reason",
                "truncated", "decision_id"):
        assert row[col] is None, f"{col} should default to NULL, got {row[col]!r}"


def test_insert_trade_decision_id_roundtrips(db):
    db.insert_trade(
        symbol="SPY", action="BUY", qty=1.0, price=500.0,
        reasoning="x", run_id="run-003", decision_id="run-003-dec-xyz",
    )
    trades = db.get_trades(symbol="SPY")
    assert trades[0]["decision_id"] == "run-003-dec-xyz"


def test_insert_trade_decision_id_defaults_null(db):
    """A trade outside the PM/RM decision chain (e.g. a midday sell, an
    emergency sell, a cash-sweep order) legitimately carries no decision_id."""
    db.insert_trade(
        symbol="SPY", action="SELL", qty=1.0, price=500.0,
        reasoning="x", run_id="midday-001",
    )
    trades = db.get_trades(symbol="SPY")
    assert trades[0]["decision_id"] is None


def test_migration_adds_new_columns_on_legacy_db(tmp_path):
    """A DB file created with the PRE-Stage-1 agent_logs/trades schema (no
    new columns at all) must open and migrate safely via _ensure_column —
    new columns appear, pre-existing rows read back with NULL in them, and
    nothing about the old rows is touched."""
    import sqlite3

    db_path = tmp_path / "legacy.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript("""
        CREATE TABLE trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL,
            action TEXT NOT NULL,
            qty REAL NOT NULL,
            price REAL NOT NULL,
            reasoning TEXT,
            run_id TEXT,
            timestamp TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE TABLE agent_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            agent_name TEXT NOT NULL,
            run_id TEXT NOT NULL,
            input_summary TEXT,
            output_summary TEXT,
            full_response TEXT,
            model TEXT,
            tokens_used INTEGER,
            timestamp TEXT NOT NULL DEFAULT (datetime('now'))
        );
    """)
    conn.execute(
        "INSERT INTO trades (symbol, action, qty, price, reasoning, run_id) "
        "VALUES ('SPY', 'BUY', 1, 500.0, 'legacy row', 'old-run')"
    )
    conn.execute(
        "INSERT INTO agent_logs (agent_name, run_id, input_summary, output_summary, "
        "full_response, model, tokens_used) "
        "VALUES ('tech_analyst', 'old-run', 's', 'o', '{}', 'claude-opus-4-6', 10)"
    )
    conn.commit()
    conn.close()

    database = Database(str(db_path))
    database.initialize()  # runs _migrate() against the pre-existing tables
    try:
        trades_cols = {r[1] for r in database.conn.execute("PRAGMA table_info(trades)")}
        agent_logs_cols = {r[1] for r in database.conn.execute("PRAGMA table_info(agent_logs)")}
        for col in ("decision_id", "realized_pnl"):
            assert col in trades_cols
        for col in ("requested_provider", "requested_model", "actual_provider",
                    "prompt_version", "latency_s", "status", "finish_reason",
                    "truncated", "decision_id"):
            assert col in agent_logs_cols

        legacy_trade = database.get_trades(symbol="SPY")[0]
        assert legacy_trade["reasoning"] == "legacy row"
        assert legacy_trade["decision_id"] is None

        legacy_log = database.get_agent_logs(run_id="old-run")[0]
        assert legacy_log["model"] == "claude-opus-4-6"
        assert legacy_log["requested_provider"] is None
        assert legacy_log["decision_id"] is None
    finally:
        database.close()


def test_migration_is_idempotent_on_already_migrated_db(db):
    """Calling initialize() (and therefore _migrate()) again on an
    already-current-schema DB must not raise or duplicate columns."""
    db.initialize()
    db.initialize()
    cols = [r[1] for r in db.conn.execute("PRAGMA table_info(agent_logs)")]
    assert cols.count("decision_id") == 1


def test_reconciled_exit_persists_deterministic_realized_pnl(db):
    db.insert_trade(
        "AAPL", "BUY", 10, 100, "entry", "run-entry",
        broker_order_id="buy-1", fill_status="submitted",
    )
    db.update_trade_fill("buy-1", "filled", fill_qty=10, fill_price=101)
    db.insert_trade(
        "AAPL", "SELL", 4, 110, "trim", "run-exit",
        broker_order_id="sell-1", fill_status="submitted",
    )
    db.update_trade_fill("sell-1", "filled", fill_qty=4, fill_price=111)

    trade = db.get_trades(symbol="AAPL")[0]
    assert trade["fill_status"] == "filled"
    assert trade["realized_pnl"] == 40.0


def test_realized_pnl_stays_unknown_without_confirmed_cost_basis(db):
    db.insert_trade(
        "MSFT", "SELL", 3, 200, "legacy exit", "run-exit",
        broker_order_id="sell-no-basis", fill_status="submitted",
    )
    db.update_trade_fill(
        "sell-no-basis", "filled", fill_qty=3, fill_price=201,
    )
    assert db.get_trades(symbol="MSFT")[0]["realized_pnl"] is None


def test_terminal_partial_fill_books_only_confirmed_exit_quantity(db):
    db.insert_trade(
        "NVDA", "BUY", 10, 100, "entry", "run-entry",
        broker_order_id="nv-buy", fill_status="submitted",
    )
    db.update_trade_fill("nv-buy", "filled", fill_qty=10, fill_price=100)
    db.insert_trade(
        "NVDA", "SELL", 10, 110, "exit attempt", "run-exit",
        broker_order_id="nv-sell", fill_status="submitted",
    )
    db.update_trade_fill("nv-sell", "canceled", fill_qty=3, fill_price=110)
    assert db.get_trades(symbol="NVDA")[0]["realized_pnl"] == 30.0


# ---------------------------------------------------------------------------
# Phase 6 (§6.2a/e): position_id chain linking + exit_reason_category
# ---------------------------------------------------------------------------

def test_position_id_minted_on_buy_from_flat(db):
    db.insert_trade("AAPL", "BUY", 10, 150.0, "clean setup", "run-1", stop_loss=140.0)
    trade = db.get_trades(symbol="AAPL")[0]
    assert trade["position_id"] is not None
    assert trade["position_id"].startswith("pos-")


def test_position_id_inherited_by_sell_reduce_trail_stop(db):
    db.insert_trade("AAPL", "BUY", 10, 150.0, "entry", "run-1", stop_loss=140.0)
    db.insert_trade("AAPL", "REDUCE", 3, 160.0, "risk-off macro shift", "run-1")
    db.insert_trade("AAPL", "TRAIL_STOP", 7, 155.0, "trailing tighter", "run-1",
                     broker_order_id="ts-1", fill_status="submitted")
    db.insert_trade("AAPL", "SELL", 7, 165.0, "stop hit", "run-1")
    trades = db.get_trades(symbol="AAPL")
    position_ids = {t["position_id"] for t in trades}
    assert len(position_ids) == 1
    assert None not in position_ids


def test_position_id_remints_after_position_goes_flat(db):
    db.insert_trade("AAPL", "BUY", 10, 150.0, "entry 1", "run-1")
    db.insert_trade("AAPL", "SELL", 10, 160.0, "thesis_invalid", "run-1")
    db.insert_trade("AAPL", "BUY", 5, 170.0, "entry 2, unrelated", "run-2")
    trades = db.get_trades(symbol="AAPL")  # newest first
    by_action = {t["action"]: t["position_id"] for t in trades}
    # trades are newest-first with duplicate actions across chains, so index
    # explicitly by insertion order instead
    ordered = sorted(trades, key=lambda t: t["id"])
    first_chain = ordered[0]["position_id"]
    second_buy = ordered[2]["position_id"]
    assert ordered[0]["position_id"] == ordered[1]["position_id"]  # entry1 == sell
    assert second_buy != first_chain
    assert second_buy is not None


def test_position_id_scale_in_buy_inherits_same_chain(db):
    db.insert_trade("MSFT", "BUY", 10, 50.0, "initial", "run-1")
    db.insert_trade("MSFT", "BUY", 5, 52.0, "adding to winner", "run-1")
    trades = db.get_trades(symbol="MSFT")
    assert trades[0]["position_id"] == trades[1]["position_id"]


def test_position_id_unfilled_trail_stop_inherits_but_stays_uncategorised_until_fill(db):
    db.insert_trade("NVDA", "BUY", 10, 100.0, "entry", "run-1", stop_loss=90.0)
    db.insert_trade("NVDA", "TRAIL_STOP", 10, 95.0, "trail tighter", "run-1",
                     broker_order_id="ts-nvda", fill_status="submitted")
    trades = db.get_trades(symbol="NVDA")
    trail = next(t for t in trades if t["action"] == "TRAIL_STOP")
    buy = next(t for t in trades if t["action"] == "BUY")
    assert trail["position_id"] == buy["position_id"]     # inherits regardless of fill
    assert trail["exit_reason_category"] is None           # not confirmed fired yet

    db.update_trade_fill("ts-nvda", "filled", fill_qty=10, fill_price=95.0)
    trail_after = db.get_trades(symbol="NVDA")[0]
    assert trail_after["exit_reason_category"] == "broker_stop_fill"
    assert trail_after["position_id"] == buy["position_id"]  # unchanged by the fill update


def test_position_id_left_null_when_no_open_chain_to_attach_to(db):
    """A SELL with no prior BUY on record (a position that predates this
    ledger) must not be guessed into a fabricated chain."""
    db.insert_trade("LEGACY", "SELL", 5, 50.0, "closing an old position", "run-1")
    trade = db.get_trades(symbol="LEGACY")[0]
    assert trade["position_id"] is None


def test_position_id_hold_and_sweep_rows_never_get_a_position_id(db):
    db.insert_trade("SPY", "HOLD", 0, 0.0, "no action", "run-1")
    db.insert_trade("SGOV", "SWEEP_BUY", 100, 100.0, "park idle cash", "run-1")
    db.insert_trade("SGOV", "SWEEP_SELL", 100, 100.0, "redeploy cash", "run-1")
    for sym in ("SPY", "SGOV"):
        for t in db.get_trades(symbol=sym):
            assert t["position_id"] is None


def test_exit_reason_category_take_profit_gated_on_confirmed_fill(db):
    db.insert_trade("AMZN", "BUY", 10, 100.0, "entry", "run-1")
    db.insert_trade("AMZN", "TAKE_PROFIT", 2, 135.0,
                     "Auto take-profit: +35.0% >= 30.0%, trimming 15%", "run-1",
                     broker_order_id="tp-1", fill_status="submitted")
    submitted = db.get_trades(symbol="AMZN")[0]
    assert submitted["exit_reason_category"] is None
    db.update_trade_fill("tp-1", "filled", fill_qty=2, fill_price=135.0)
    filled = db.get_trades(symbol="AMZN")[0]
    assert filled["exit_reason_category"] == "take_profit_target"


@pytest.mark.parametrize("reasoning,expected", [
    ("thesis_invalid: support broke", "thesis_invalidated"),
    ("high-conviction bearish news on the sector", "adverse_news_or_state_change"),
    ("bearish earnings, guidance cut", "earnings_or_filing"),
    ("macro regime flip to risk-off", "macro_regime_shift"),
    ("daily loss circuit breaker tripped", "risk_management_hard_stop"),
    ("stopped out per broker fill", "broker_stop_fill"),
    ("feels stretched, taking some off", "uncategorised"),
])
def test_exit_reason_category_derived_from_hard_trigger_vocabulary(db, reasoning, expected):
    db.insert_trade("XOM", "BUY", 10, 100.0, "entry", "run-1")
    db.insert_trade("XOM", "SELL", 10, 110.0, reasoning, "run-1")
    sell = db.get_trades(symbol="XOM")[0]
    assert sell["exit_reason_category"] == expected


def test_exit_reason_category_stop_out_is_broker_stop_fill(db):
    db.insert_trade("ONDS", "BUY", 17, 8.53, "entry", "run-1", broker_order_id="buy-1", fill_status="filled")
    db.insert_stop_out_trade(symbol="ONDS", qty=17, price=7.93, broker_order_id="stop-1", filled_at=None)
    stop_out = db.get_trades(symbol="ONDS")[0]
    assert stop_out["action"] == "STOP_OUT"
    assert stop_out["exit_reason_category"] == "broker_stop_fill"
    assert stop_out["position_id"] is not None


def test_exit_reason_category_none_for_buy_and_hold(db):
    db.insert_trade("KO", "BUY", 10, 60.0, "entry", "run-1")
    db.insert_trade("KO", "HOLD", 0, 0.0, "steady", "run-1")
    for t in db.get_trades(symbol="KO"):
        assert t["exit_reason_category"] is None


# ---------------------------------------------------------------------------
# Phase 6 (§6.2a): backfill_position_ids
# ---------------------------------------------------------------------------

def test_backfill_position_ids_assigns_confident_chains(db):
    db.conn.execute(
        "INSERT INTO trades (symbol, action, qty, price, reasoning, run_id, timestamp) "
        "VALUES ('IBM', 'BUY', 10, 100.0, 'legacy entry', 'run-x', '2026-01-01 10:00:00')"
    )
    db.conn.execute(
        "INSERT INTO trades (symbol, action, qty, price, reasoning, run_id, timestamp) "
        "VALUES ('IBM', 'SELL', 10, 110.0, 'thesis_invalid', 'run-x', '2026-01-05 10:00:00')"
    )
    db.conn.commit()
    result = db.backfill_position_ids(dry_run=False)
    assert result["assigned"] == 2
    assert result["left_null_ambiguous"] == 0
    trades = db.get_trades(symbol="IBM")
    assert trades[0]["position_id"] == trades[1]["position_id"]
    assert trades[0]["position_id"] is not None


def test_backfill_position_ids_refuses_to_guess_orphan_exit(db):
    """A SELL with no prior BUY (a position that predates the ledger) must
    be left NULL, not assigned a fabricated chain, and counted separately
    from rows that were never eligible (HOLD/SWEEP_*)."""
    db.conn.execute(
        "INSERT INTO trades (symbol, action, qty, price, reasoning, run_id, timestamp) "
        "VALUES ('ORPHAN', 'SELL', 5, 50.0, 'closing a pre-existing position', "
        "'run-x', '2026-01-01 09:00:00')"
    )
    db.conn.execute(
        "INSERT INTO trades (symbol, action, qty, price, reasoning, run_id, timestamp) "
        "VALUES ('ORPHAN', 'HOLD', 0, 0.0, 'no action', 'run-x', '2026-01-02 09:00:00')"
    )
    db.conn.commit()
    result = db.backfill_position_ids(dry_run=False)
    assert result["left_null_ambiguous"] == 1
    assert result["not_applicable"] == 1
    assert result["assigned"] == 0
    trades = db.get_trades(symbol="ORPHAN")
    assert all(t["position_id"] is None for t in trades)


def test_backfill_position_ids_dry_run_writes_nothing(db):
    db.conn.execute(
        "INSERT INTO trades (symbol, action, qty, price, reasoning, run_id, timestamp) "
        "VALUES ('DIS', 'BUY', 10, 90.0, 'legacy entry', 'run-x', '2026-01-01 10:00:00')"
    )
    db.conn.commit()
    dry = db.backfill_position_ids(dry_run=True)
    assert dry["assigned"] == 1
    assert db.get_trades(symbol="DIS")[0]["position_id"] is None


def test_backfill_position_ids_is_idempotent(db):
    db.conn.execute(
        "INSERT INTO trades (symbol, action, qty, price, reasoning, run_id, timestamp) "
        "VALUES ('GE', 'BUY', 10, 90.0, 'legacy entry', 'run-x', '2026-01-01 10:00:00')"
    )
    db.conn.execute(
        "INSERT INTO trades (symbol, action, qty, price, reasoning, run_id, timestamp) "
        "VALUES ('GE', 'SELL', 10, 95.0, 'thesis_invalid', 'run-x', '2026-01-05 10:00:00')"
    )
    db.conn.commit()
    first = db.backfill_position_ids(dry_run=False)
    assigned_id = db.get_trades(symbol="GE")[0]["position_id"]
    second = db.backfill_position_ids(dry_run=False)
    assert second["assigned"] == 0
    assert second["already_assigned"] == first["assigned"]
    assert db.get_trades(symbol="GE")[0]["position_id"] == assigned_id  # unchanged


def test_backfill_position_ids_respects_rows_already_assigned_by_live_trading(db):
    """Simulates the real deploy sequence: live trading (insert_trade) has
    already minted a position_id for a NEW row while older history is still
    NULL. The backfill must treat the live-assigned id as ground truth and
    link the older row into the SAME chain, not mint a second one."""
    db.conn.execute(
        "INSERT INTO trades (symbol, action, qty, price, reasoning, run_id, timestamp) "
        "VALUES ('BA', 'BUY', 10, 200.0, 'legacy entry, pre-migration', 'run-x', "
        "'2026-01-01 10:00:00')"
    )
    db.conn.commit()
    # live insert AFTER the legacy row, for the same still-open position
    db.insert_trade("BA", "TRAIL_STOP", 10, 190.0, "trail placed live", "run-2")
    live_row = db.get_trades(symbol="BA", executed_only=False)[0]
    assert live_row["position_id"] is not None

    db.backfill_position_ids(dry_run=False)
    trades = db.get_trades(symbol="BA")
    position_ids = {t["position_id"] for t in trades}
    assert len(position_ids) == 1
    assert live_row["position_id"] in position_ids
