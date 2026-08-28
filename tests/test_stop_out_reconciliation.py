"""Stop-out reconciliation — 2026-08-28 ONDS/CCJ accounting gap.

Both positions were closed by their broker-resident protective stop (a GTC
stop-limit order `AlpacaBroker.place_entry_protection` places on a fill but
never writes into `trades`). No SELL/exit row was ever recorded, both BUY
rows sat forever at `realized_pnl IS NULL`, and the `positions` table
(synced straight from broker truth) quietly diverged from what `trades`
claimed. Verified against the live paper account on 2026-08-28:

  ONDS: BUY 17 @ 8.53 (2026-08-27 14:31:55 UTC) → stop-limit order
        865a3187-af9d-4752-be45-f121dcb9a390 filled 17 @ 7.93
        (2026-08-28 16:16:07 UTC) → realized -$10.20
  CCJ:  BUY 2 @ 107.465 (2026-08-27 13:36:04 UTC) → stop-limit order
        c785ae7e-359d-49fc-9853-0930e879eae5 filled 2 @ 102.955
        (2026-08-28 14:05:17 UTC) → realized -$9.02

`_reconcile_stop_out_fills` (src/pipeline.py) closes the gap by diffing
what the ledger believes it holds (`Database.get_symbols_with_open_ledger_
qty`) against what the broker actually shows, then asking the broker
directly for filled SELL orders the ledger has never recorded
(`AlpacaBroker.list_filled_sell_orders`) and writing them back via
`Database.insert_stop_out_trade`.
"""

import types
from unittest.mock import MagicMock

from src.pipeline import TradingPipeline
from src.storage.db import Database


def _mk_pipeline(db: Database, broker, lookback_days: int = 7) -> TradingPipeline:
    pipeline = TradingPipeline.__new__(TradingPipeline)
    pipeline.db = db
    pipeline.broker = broker
    pipeline.config = types.SimpleNamespace(
        reconciliation=types.SimpleNamespace(stop_out_lookback_days=lookback_days),
    )
    return pipeline


def _stop_order(order_id, symbol, qty, price, filled_at="2026-08-28T16:16:07.476647+00:00"):
    return {
        "id": order_id, "symbol": symbol, "qty": qty, "price": price,
        "filled_at": filled_at, "order_type": "stop_limit",
    }


def _filled_buy(db, symbol, qty, price, order_id, run_id="r1", ts=None):
    """Insert a BUY the way production actually produces one: written
    'submitted' at order time, then flipped to 'filled' with REAL fill_qty
    / fill_price via update_trade_fill — exactly what `_reconcile_fills`
    does once the broker confirms the entry. `_realized_pnl_through_trade`
    (unlike `compute_trade_calibration`) has no requested-qty/price
    fallback, so a BUY row needs its fill_qty/fill_price actually populated
    for any exit to price against it — passing fill_status='filled'
    straight to insert_trade alone leaves those NULL and silently starves
    every downstream realized_pnl computation."""
    db.insert_trade(symbol, "BUY", qty, price, "entry", run_id,
                    broker_order_id=order_id, fill_status="submitted")
    db.update_trade_fill(broker_order_id=order_id, fill_status="filled",
                         fill_qty=qty, fill_price=price)
    if ts:
        db.conn.execute(
            "UPDATE trades SET timestamp = ? WHERE broker_order_id = ?",
            (ts, order_id),
        )
        db.conn.commit()


# ---------------------------------------------------------------------------
# Database.insert_stop_out_trade — the write-back primitive.
# ---------------------------------------------------------------------------

def test_insert_stop_out_trade_computes_realized_pnl_for_a_loss(tmp_path):
    """ONDS, exact real numbers: BUY 17 @ 8.53, stopped 17 @ 7.93 →
    realized_pnl must be NEGATIVE and equal to -$10.20 (not a guess, not a
    magnitude-only figure — the sign is the whole point of a P&L column)."""
    db = Database(str(tmp_path / "t.db"))
    db.initialize()
    _filled_buy(db, "ONDS", 17, 8.53, "entry-onds")

    row_id, created = db.insert_stop_out_trade(
        symbol="ONDS", qty=17.0, price=7.93,
        broker_order_id="865a3187-af9d-4752-be45-f121dcb9a390",
        filled_at="2026-08-28 16:16:07", run_id="r2",
    )
    assert created is True
    assert row_id > 0

    rows = db.get_trades(symbol="ONDS", executed_only=True)
    stop_out = next(r for r in rows if r["action"] == "STOP_OUT")
    assert stop_out["realized_pnl"] == -10.2
    assert stop_out["realized_pnl"] < 0
    assert stop_out["fill_status"] == "filled"
    assert stop_out["fill_qty"] == 17.0
    assert stop_out["fill_price"] == 7.93
    assert stop_out["broker_order_id"] == "865a3187-af9d-4752-be45-f121dcb9a390"
    assert stop_out["timestamp"].startswith("2026-08-28 16:16:07")


def test_insert_stop_out_trade_ccj_realized_pnl_exact(tmp_path):
    """CCJ, exact real numbers: BUY 2 @ 107.465, stopped 2 @ 102.955 →
    realized_pnl == -$9.02 exactly."""
    db = Database(str(tmp_path / "t.db"))
    db.initialize()
    _filled_buy(db, "CCJ", 2, 107.465, "entry-ccj")

    db.insert_stop_out_trade(
        symbol="CCJ", qty=2.0, price=102.955,
        broker_order_id="c785ae7e-359d-49fc-9853-0930e879eae5",
        filled_at="2026-08-28 14:05:17", run_id="r2",
    )

    row = next(
        r for r in db.get_trades(symbol="CCJ", executed_only=True)
        if r["action"] == "STOP_OUT"
    )
    assert row["realized_pnl"] == -9.02


def test_insert_stop_out_trade_is_idempotent_across_repeated_calls(tmp_path):
    """The reconciler re-runs every session (morning / intra_check / midday
    / close / evening) — the SAME broker order id must produce exactly ONE
    trades row no matter how many times insert_stop_out_trade is called
    for it."""
    db = Database(str(tmp_path / "t.db"))
    db.initialize()
    _filled_buy(db, "ONDS", 17, 8.53, "entry-onds")

    results = [
        db.insert_stop_out_trade(
            symbol="ONDS", qty=17.0, price=7.93,
            broker_order_id="865a3187-af9d-4752-be45-f121dcb9a390",
            filled_at="2026-08-28 16:16:07", run_id=f"pass-{i}",
        )
        for i in range(3)
    ]

    row_ids = [r[0] for r in results]
    created_flags = [r[1] for r in results]
    assert row_ids == [row_ids[0]] * 3, "all three calls must resolve to the SAME row"
    assert created_flags == [True, False, False]

    stop_out_rows = [
        r for r in db.get_trades(symbol="ONDS", executed_only=True)
        if r["action"] == "STOP_OUT"
    ]
    assert len(stop_out_rows) == 1


def test_insert_stop_out_trade_requires_broker_order_id(tmp_path):
    """A falsy broker_order_id breaks the idempotency key — refuse loudly
    rather than insert a row that could be double-recorded on replay."""
    import pytest

    db = Database(str(tmp_path / "t.db"))
    db.initialize()
    with pytest.raises(ValueError):
        db.insert_stop_out_trade(
            symbol="ONDS", qty=17.0, price=7.93,
            broker_order_id="", filled_at=None,
        )


def test_insert_stop_out_trade_unmatched_pnl_is_null_not_guessed(tmp_path):
    """The ledger only recorded a 10-share BUY, but the broker's stop
    filled 15 (e.g. a corporate action / untracked prior BUY inflated the
    real position). The exit is still RECORDED — never dropped — but
    realized_pnl must stay NULL rather than pricing 5 phantom shares."""
    db = Database(str(tmp_path / "t.db"))
    db.initialize()
    _filled_buy(db, "XYZ", 10, 50.0, "entry-xyz")

    db.insert_stop_out_trade(
        symbol="XYZ", qty=15.0, price=45.0,
        broker_order_id="stop-xyz", filled_at="2026-08-28 10:00:00",
    )

    row = next(
        r for r in db.get_trades(symbol="XYZ", executed_only=True)
        if r["action"] == "STOP_OUT"
    )
    assert row["fill_qty"] == 15.0  # the REAL broker fill, recorded as-is
    assert row["realized_pnl"] is None  # not guessed


# ---------------------------------------------------------------------------
# Database.get_symbols_with_open_ledger_qty / get_known_broker_order_ids
# ---------------------------------------------------------------------------

def test_get_symbols_with_open_ledger_qty_nets_buys_and_exits(tmp_path):
    db = Database(str(tmp_path / "t.db"))
    db.initialize()
    db.insert_trade("AAPL", "BUY", 10, 100.0, "x", "r1",
                    broker_order_id="b1", fill_status="filled")
    db.insert_trade("AAPL", "SELL", 4, 110.0, "x", "r2",
                    broker_order_id="s1", fill_status="filled")
    # A canceled order contributes nothing (never executed).
    db.insert_trade("AAPL", "SELL", 100, 999.0, "x", "r3",
                    broker_order_id="canceled-1", fill_status="canceled")
    db.insert_trade("MSFT", "BUY", 5, 200.0, "x", "r1", fill_status="filled")

    net = db.get_symbols_with_open_ledger_qty()
    assert net["AAPL"] == 6.0
    assert net["MSFT"] == 5.0


def test_get_known_broker_order_ids_scoped_to_symbol(tmp_path):
    db = Database(str(tmp_path / "t.db"))
    db.initialize()
    db.insert_trade("ONDS", "BUY", 17, 8.53, "x", "r1",
                    broker_order_id="entry-onds", fill_status="filled")
    db.insert_trade("CCJ", "BUY", 2, 107.465, "x", "r1",
                    broker_order_id="entry-ccj", fill_status="filled")

    assert db.get_known_broker_order_ids("ONDS") == {"entry-onds"}
    assert db.get_known_broker_order_ids("CCJ") == {"entry-ccj"}
    assert db.get_known_broker_order_ids("NFLX") == set()


# ---------------------------------------------------------------------------
# TradingPipeline._reconcile_stop_out_fills — the session-level reconciler.
# ---------------------------------------------------------------------------

def test_reconcile_stop_out_fills_records_ondsccj_with_correct_pnl(tmp_path):
    """End-to-end against the two real 2026-08-28 incidents, using the
    ACTUAL order ids / prices / timestamps verified at the broker."""
    db = Database(str(tmp_path / "t.db"))
    db.initialize()
    _filled_buy(db, "ONDS", 17, 8.53, "entry-onds", ts="2026-08-27 14:31:55")
    _filled_buy(db, "CCJ", 2, 107.465, "entry-ccj", ts="2026-08-27 13:36:04")

    broker = MagicMock()
    broker.get_positions.return_value = []  # both flat at the broker

    def _fills(symbol, after):
        if symbol == "ONDS":
            return [_stop_order(
                "865a3187-af9d-4752-be45-f121dcb9a390", "ONDS", 17.0, 7.93,
                "2026-08-28T16:16:07.476647+00:00",
            )]
        if symbol == "CCJ":
            return [_stop_order(
                "c785ae7e-359d-49fc-9853-0930e879eae5", "CCJ", 2.0, 102.955,
                "2026-08-28T14:05:17.636316+00:00",
            )]
        return []
    broker.list_filled_sell_orders.side_effect = _fills

    pipeline = _mk_pipeline(db, broker)
    results = pipeline._reconcile_stop_out_fills(run_id="r-reconcile")

    by_symbol = {r["symbol"]: r for r in results}
    assert by_symbol["ONDS"]["matched"] is True
    assert by_symbol["ONDS"]["recorded"] == 1
    assert by_symbol["CCJ"]["matched"] is True
    assert by_symbol["CCJ"]["recorded"] == 1

    onds = next(r for r in db.get_trades(symbol="ONDS", executed_only=True)
                if r["action"] == "STOP_OUT")
    ccj = next(r for r in db.get_trades(symbol="CCJ", executed_only=True)
               if r["action"] == "STOP_OUT")
    assert onds["realized_pnl"] == -10.2
    assert ccj["realized_pnl"] == -9.02
    assert onds["broker_order_id"] == "865a3187-af9d-4752-be45-f121dcb9a390"
    assert ccj["broker_order_id"] == "c785ae7e-359d-49fc-9853-0930e879eae5"
    # Backdated to the ACTUAL fill time, not "now" (detection time).
    assert onds["timestamp"].startswith("2026-08-28 16:16:07")
    assert ccj["timestamp"].startswith("2026-08-28 14:05:17")


def test_reconcile_stop_out_fills_written_exactly_once_across_three_passes(tmp_path):
    """The reconciler runs at every session entry point (morning,
    intra_check every ~30 min, midday, close, evening). Simulating three
    separate passes over the SAME unresolved gap must leave exactly one
    STOP_OUT row — not three."""
    db = Database(str(tmp_path / "t.db"))
    db.initialize()
    _filled_buy(db, "ONDS", 17, 8.53, "entry-onds")

    broker = MagicMock()
    broker.get_positions.return_value = []
    broker.list_filled_sell_orders.return_value = [
        _stop_order("865a3187-af9d-4752-be45-f121dcb9a390", "ONDS", 17.0, 7.93),
    ]

    pipeline = _mk_pipeline(db, broker)
    for i in range(3):
        pipeline._reconcile_stop_out_fills(run_id=f"pass-{i}")

    stop_out_rows = [
        r for r in db.get_trades(symbol="ONDS", executed_only=True)
        if r["action"] == "STOP_OUT"
    ]
    assert len(stop_out_rows) == 1
    assert stop_out_rows[0]["realized_pnl"] == -10.2
    # After the first pass the ledger and broker agree (both flat) — later
    # passes must not even re-query the broker for a symbol with no gap.
    assert broker.list_filled_sell_orders.call_count == 1


def test_reconcile_stop_out_fills_flags_unresolved_gap_without_guessing(tmp_path):
    """Ledger believes ONDS still has 17 sh open, broker shows 0, but the
    broker's own order history has NOTHING that explains it (e.g. outside
    the lookback window, or a genuine anomaly). Must NOT invent a trades
    row — only flag, loudly, for manual review."""
    db = Database(str(tmp_path / "t.db"))
    db.initialize()
    db.insert_trade("ONDS", "BUY", 17, 8.53, "entry", "r1", fill_status="filled")

    broker = MagicMock()
    broker.get_positions.return_value = []
    broker.list_filled_sell_orders.return_value = []  # broker found nothing

    pipeline = _mk_pipeline(db, broker)
    results = pipeline._reconcile_stop_out_fills(run_id="r1")

    assert results == [{
        "symbol": "ONDS", "ledger_qty": 17.0, "broker_qty": 0.0,
        "matched": False, "recorded": 0,
    }]
    # Nothing fabricated in the ledger.
    stop_out_rows = [
        r for r in db.get_trades(symbol="ONDS", executed_only=True)
        if r["action"] == "STOP_OUT"
    ]
    assert stop_out_rows == []
    # But the anomaly is NOT silently dropped — it's visible in evidence.
    event = db.conn.execute(
        "SELECT evidence_json FROM specialist_evidence "
        "WHERE run_id='r1' AND symbol='ONDS' AND kind='pipeline_event'"
    ).fetchone()
    assert event is not None
    assert '"outcome": "stop_out_gap_unexplained"' in event["evidence_json"]


def test_reconcile_stop_out_fills_flags_unmatched_pnl_without_guessing(tmp_path):
    """The broker fill IS found and IS recorded (never dropped), but the
    ledger's own BUY history can't cover the exited quantity — realized_pnl
    must stay NULL and the anomaly must be flagged, not silently accepted
    as if it were a normal, fully-priced exit."""
    db = Database(str(tmp_path / "t.db"))
    db.initialize()
    _filled_buy(db, "XYZ", 10, 50.0, "entry-xyz")

    broker = MagicMock()
    broker.get_positions.return_value = []  # ledger sees 10 open, broker sees 0 → gap
    broker.list_filled_sell_orders.return_value = [
        _stop_order("stop-xyz", "XYZ", 15.0, 45.0),  # broker actually sold 15
    ]

    pipeline = _mk_pipeline(db, broker)
    results = pipeline._reconcile_stop_out_fills(run_id="r1")

    assert results == [{
        "symbol": "XYZ", "ledger_qty": 10.0, "broker_qty": 0.0,
        "matched": True, "recorded": 1,
    }]
    row = next(r for r in db.get_trades(symbol="XYZ", executed_only=True)
               if r["action"] == "STOP_OUT")
    assert row["fill_qty"] == 15.0
    assert row["realized_pnl"] is None

    event = db.conn.execute(
        "SELECT evidence_json FROM specialist_evidence "
        "WHERE run_id='r1' AND symbol='XYZ' AND kind='pipeline_event'"
    ).fetchone()
    assert event is not None
    assert '"outcome": "stop_out_pnl_unmatched"' in event["evidence_json"]


def test_reconcile_stop_out_fills_no_gap_is_a_no_op(tmp_path):
    """The common case: the broker still holds what the ledger expects.
    No broker order query, no writes, no flags."""
    db = Database(str(tmp_path / "t.db"))
    db.initialize()
    db.insert_trade("AAPL", "BUY", 10, 180.0, "entry", "r1", fill_status="filled")

    broker = MagicMock()
    from src.models import Position
    broker.get_positions.return_value = [
        Position(symbol="AAPL", qty=10, avg_entry=180.0, current_price=185.0,
                 market_value=1850.0, unrealized_pnl=50.0, sector="Tech"),
    ]

    pipeline = _mk_pipeline(db, broker)
    results = pipeline._reconcile_stop_out_fills(run_id="r1")

    assert results == []
    broker.list_filled_sell_orders.assert_not_called()


def test_reconcile_stop_out_fills_broker_positions_query_failure_is_non_fatal(tmp_path):
    """A broker outage during reconciliation must not raise — leave the
    gap for the next pass."""
    db = Database(str(tmp_path / "t.db"))
    db.initialize()
    db.insert_trade("ONDS", "BUY", 17, 8.53, "entry", "r1", fill_status="filled")

    broker = MagicMock()
    broker.get_positions.side_effect = RuntimeError("alpaca 503")

    pipeline = _mk_pipeline(db, broker)
    assert pipeline._reconcile_stop_out_fills(run_id="r1") == []


def test_reconcile_stop_out_fills_broker_fill_query_none_leaves_gap_for_next_pass(tmp_path):
    """list_filled_sell_orders returning None means the QUERY FAILED, not
    'no fills' — must retry next time, not flag a false anomaly."""
    db = Database(str(tmp_path / "t.db"))
    db.initialize()
    db.insert_trade("ONDS", "BUY", 17, 8.53, "entry", "r1", fill_status="filled")

    broker = MagicMock()
    broker.get_positions.return_value = []
    broker.list_filled_sell_orders.return_value = None

    pipeline = _mk_pipeline(db, broker)
    results = pipeline._reconcile_stop_out_fills(run_id="r1")

    assert results == []
    stop_out_rows = [
        r for r in db.get_trades(symbol="ONDS", executed_only=True)
        if r["action"] == "STOP_OUT"
    ]
    assert stop_out_rows == []


def test_reconcile_stop_out_fills_noop_without_config(tmp_path):
    """A pipeline with no `.config` (unit-test double, or a settings.yaml
    that somehow predates ReconciliationConfig's default_factory) must
    bail out cleanly rather than raising — mirrors _force_delever's same
    defensive pattern."""
    db = Database(str(tmp_path / "t.db"))
    db.initialize()
    db.insert_trade("ONDS", "BUY", 17, 8.53, "entry", "r1", fill_status="filled")

    pipeline = TradingPipeline.__new__(TradingPipeline)
    pipeline.db = db
    pipeline.broker = MagicMock()
    # No pipeline.config at all.

    assert pipeline._reconcile_stop_out_fills(run_id="r1") == []
    pipeline.broker.get_positions.assert_not_called()


# ---------------------------------------------------------------------------
# Existing system-initiated exits must be UNCHANGED by this fix — hard
# literals, not just "still passes". SELL / REDUCE / TRAIL_STOP / SWEEP_SELL
# already had a working write-back path (insert_trade at submission +
# update_trade_fill at reconciliation); adding STOP_OUT to the SELL-family
# tuple in compute_trade_calibration and to _EXIT_AUDIT_ACTIONS must not
# change what those four compute.
# ---------------------------------------------------------------------------

def test_existing_sell_realized_pnl_unchanged(tmp_path):
    db = Database(str(tmp_path / "t.db"))
    db.initialize()
    _filled_buy(db, "AAPL", 10, 100.0, "b1")
    db.insert_trade("AAPL", "SELL", 10, 0, "x", "r2",
                    broker_order_id="s1", fill_status="submitted")
    db.update_trade_fill(broker_order_id="s1", fill_status="filled",
                         fill_qty=10.0, fill_price=110.0)

    row = next(r for r in db.get_trades(symbol="AAPL", executed_only=True)
               if r["action"] == "SELL")
    assert row["realized_pnl"] == 100.0


def test_existing_reduce_realized_pnl_unchanged(tmp_path):
    db = Database(str(tmp_path / "t.db"))
    db.initialize()
    _filled_buy(db, "MSFT", 5, 200.0, "b2")
    db.insert_trade("MSFT", "REDUCE", 5, 0, "x", "r2",
                    broker_order_id="red1", fill_status="submitted")
    db.update_trade_fill(broker_order_id="red1", fill_status="filled",
                         fill_qty=5.0, fill_price=220.0)

    row = next(r for r in db.get_trades(symbol="MSFT", executed_only=True)
               if r["action"] == "REDUCE")
    assert row["realized_pnl"] == 100.0


def test_existing_filled_trail_stop_realized_pnl_unchanged(tmp_path):
    db = Database(str(tmp_path / "t.db"))
    db.initialize()
    _filled_buy(db, "LLY", 8, 50.0, "b3")
    db.insert_trade("LLY", "TRAIL_STOP", 8, 45.0, "x", "r2",
                    broker_order_id="trail1", fill_status="submitted")
    db.update_trade_fill(broker_order_id="trail1", fill_status="filled",
                         fill_qty=8.0, fill_price=45.0)

    row = next(r for r in db.get_trades(symbol="LLY", executed_only=True)
               if r["action"] == "TRAIL_STOP")
    assert row["realized_pnl"] == -40.0


def test_existing_sweep_sell_realized_pnl_unchanged(tmp_path):
    db = Database(str(tmp_path / "t.db"))
    db.initialize()
    db.insert_trade("SGOV", "SWEEP_BUY", 100, 100.0, "x", "r1",
                    broker_order_id="sb1", fill_status="submitted")
    db.update_trade_fill(broker_order_id="sb1", fill_status="filled",
                         fill_qty=100.0, fill_price=100.0)
    db.insert_trade("SGOV", "SWEEP_SELL", 100, 0, "x", "r2",
                    broker_order_id="ss1", fill_status="submitted")
    db.update_trade_fill(broker_order_id="ss1", fill_status="filled",
                         fill_qty=100.0, fill_price=100.02)

    row = next(r for r in db.get_trades(symbol="SGOV", executed_only=True)
               if r["action"] == "SWEEP_SELL")
    assert row["realized_pnl"] == 2.0


def test_compute_trade_calibration_counts_stop_out_as_a_closed_trade(tmp_path):
    """Before this fix, compute_trade_calibration had NO action name that
    represented 'the broker's stop fired' — a STOP_OUT row, even once
    written, would have been silently excluded from win_rate / avg_return
    exactly like the pre-2026-07-16 TRAIL_STOP gap. This pins the fix."""
    db = Database(str(tmp_path / "t.db"))
    db.initialize()

    # Three closed pairs needed to cross compute_trade_calibration's n>=3
    # reporting threshold. Two ordinary SELLs plus one STOP_OUT loss.
    db.insert_trade("AAA", "BUY", 10, 100.0, "x", "r1",
                    broker_order_id="b1", fill_status="filled")
    db.conn.execute("UPDATE trades SET timestamp = datetime('now', '-10 days') WHERE broker_order_id='b1'")
    db.insert_trade("AAA", "SELL", 10, 110.0, "x", "r2",
                    broker_order_id="s1", fill_status="filled")
    db.conn.execute("UPDATE trades SET timestamp = datetime('now', '-9 days') WHERE broker_order_id='s1'")

    db.insert_trade("BBB", "BUY", 10, 100.0, "x", "r1",
                    broker_order_id="b2", fill_status="filled")
    db.conn.execute("UPDATE trades SET timestamp = datetime('now', '-8 days') WHERE broker_order_id='b2'")
    db.insert_trade("BBB", "SELL", 10, 110.0, "x", "r2",
                    broker_order_id="s2", fill_status="filled")
    db.conn.execute("UPDATE trades SET timestamp = datetime('now', '-7 days') WHERE broker_order_id='s2'")

    db.insert_trade("ONDS", "BUY", 17, 8.53, "x", "r1",
                    broker_order_id="entry-onds", fill_status="filled")
    db.conn.execute("UPDATE trades SET timestamp = '2026-08-27 14:31:55' WHERE broker_order_id='entry-onds'")
    db.insert_stop_out_trade(
        symbol="ONDS", qty=17.0, price=7.93,
        broker_order_id="865a3187-af9d-4752-be45-f121dcb9a390",
        filled_at="2026-08-28 16:16:07",
    )
    db.conn.commit()

    stats = db.compute_trade_calibration(lookback_days=3650)
    assert stats["n"] == 3
    # 2 wins (AAA, BBB) out of 3 closed trades — the STOP_OUT loss counts.
    assert stats["win_rate_pct"] == round(2 / 3 * 100, 1)
