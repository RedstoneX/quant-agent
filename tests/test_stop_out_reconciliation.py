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


# ---------------------------------------------------------------------------
# TRAIL_STOP is protection, not a sale — live corruption found 2026-09-23.
#
# `get_symbols_with_open_ledger_qty` signed every non-BUY/non-HOLD executed
# row -1, so a TRAIL_STOP row — written at PLACEMENT by the stop-placement
# paths, and left at `fill_status IS NULL` on the legacy rows that
# `_executed_trade_predicate` nevertheless treats as executed — subtracted
# the whole protected position from the ledger's own belief.
#
# Measured against the production DB on 2026-09-23 (read-only): three such
# rows existed (COP id 11, EQNR id 12, AMD id 50; all fill_status NULL,
# fill_qty NULL, broker_order_id NULL) and the ledger read AMD 0.0 while
# the broker held 1.7662, COP -5.3194 and EQNR -8.5962 while both were
# flat. AMD is the dangerous one: `_reconcile_stop_out_fills` skips any
# symbol whose ledger qty is <= 0, so an untracked broker sale on that
# symbol was undetectable by construction.
#
# These probe the CLASS — what a TRAIL_STOP in each fill state does to a
# pure share-count ledger, long side and short side, next to the other
# actions the signing rule enumerates.
# ---------------------------------------------------------------------------

def _rest_the_stops(db: Database, *, clear_order_id: bool = False):
    """Force every TRAIL_STOP row into the production legacy shape:
    fill_status NULL and fill_qty NULL. `insert_trade` cannot express this
    together with a broker_order_id, and it is exactly the shape the three
    live rows carry."""
    extra = ", broker_order_id = NULL" if clear_order_id else ""
    db.conn.execute(
        f"UPDATE trades SET fill_status = NULL, fill_qty = NULL{extra} "
        "WHERE action = 'TRAIL_STOP'")
    db.conn.commit()


def _set_fill(db: Database, broker_order_id: str, *, status=None, qty=None):
    """Force one row into a fill state `insert_trade` cannot express (it
    takes no fill_qty) — a legacy NULL status carrying a real executed
    quantity, or a partial fill that was then canceled."""
    db.conn.execute(
        "UPDATE trades SET fill_status = ?, fill_qty = ? WHERE broker_order_id = ?",
        (status, qty, broker_order_id))
    db.conn.commit()


def test_unfilled_trail_stop_with_null_fill_status_is_not_an_exit(tmp_path):
    """THE LIVE DEFECT (AMD id 50): a resting stop must not zero the book."""
    db = Database(str(tmp_path / "t.db"))
    db.initialize()
    db.insert_trade("AMD", "BUY", 1.7662, 549.11, "entry", "r1",
                    broker_order_id="amd-buy", fill_status="filled")
    db.insert_trade("AMD", "TRAIL_STOP", 1.7662, 549.11, "protect", "r1")
    _rest_the_stops(db, clear_order_id=True)

    # Precondition: the row really is one the executed predicate admits —
    # otherwise this test would pass for the wrong reason.
    admitted = db.conn.execute(
        "SELECT COUNT(*) FROM trades WHERE action = 'TRAIL_STOP' AND "
        + Database._executed_trade_predicate()).fetchone()[0]
    assert admitted == 1

    assert db.get_symbols_with_open_ledger_qty()["AMD"] == 1.7662


def test_resting_trail_stop_does_not_drive_a_flat_symbol_negative(tmp_path):
    """COP id 11: entry, resting stop, then the real sale that flattened
    the book. The stop must not double-count that exit."""
    db = Database(str(tmp_path / "t.db"))
    db.initialize()
    db.insert_trade("COP", "BUY", 5.3194, 100.0, "entry", "r1",
                    broker_order_id="cop-buy", fill_status="filled")
    db.insert_trade("COP", "TRAIL_STOP", 5.3194, 95.0, "protect", "r1")
    _rest_the_stops(db, clear_order_id=True)
    db.insert_trade("COP", "SELL", 5.3194, 99.0, "exit", "r2",
                    broker_order_id="cop-sell", fill_status="filled")
    _set_fill(db, "cop-sell", status="filled", qty=5.3194)

    assert db.get_symbols_with_open_ledger_qty()["COP"] == 0.0


def test_filled_trail_stop_is_an_exit(tmp_path):
    """The stop actually sold the shares: it MUST still subtract them."""
    db = Database(str(tmp_path / "t.db"))
    db.initialize()
    db.insert_trade("LLY", "BUY", 8, 900.0, "entry", "r1",
                    broker_order_id="lly-buy", fill_status="filled")
    db.insert_trade("LLY", "TRAIL_STOP", 8, 850.0, "protect", "r1",
                    broker_order_id="lly-stop", fill_status="submitted")
    _set_fill(db, "lly-stop", status="filled", qty=8.0)

    assert db.get_symbols_with_open_ledger_qty()["LLY"] == 0.0


def test_submitted_trail_stop_is_not_an_exit(tmp_path):
    """A stop resting with an explicit 'submitted' status. Excluded by the
    SQL predicate today, but the Python rule must agree independently —
    the SQL and Python copies of this contract have drifted before."""
    db = Database(str(tmp_path / "t.db"))
    db.initialize()
    db.insert_trade("NVDA", "BUY", 10, 500.0, "entry", "r1",
                    broker_order_id="nv-buy", fill_status="filled")
    db.insert_trade("NVDA", "TRAIL_STOP", 10, 450.0, "protect", "r1",
                    broker_order_id="nv-stop", fill_status="submitted")

    assert db.get_symbols_with_open_ledger_qty()["NVDA"] == 10.0


def test_trail_stop_with_fill_qty_but_null_status_is_an_exit(tmp_path):
    """Legacy shape: no fill_status ever written back, but the broker's
    executed quantity WAS recorded. Those shares are gone."""
    db = Database(str(tmp_path / "t.db"))
    db.initialize()
    db.insert_trade("KO", "BUY", 20, 60.0, "entry", "r1",
                    broker_order_id="ko-buy", fill_status="filled")
    db.insert_trade("KO", "TRAIL_STOP", 20, 55.0, "protect", "r1",
                    broker_order_id="ko-stop")
    _set_fill(db, "ko-stop", status=None, qty=20.0)

    assert db.get_symbols_with_open_ledger_qty()["KO"] == 0.0


def test_partially_filled_trail_stop_subtracts_only_what_traded(tmp_path):
    """Part of the protected size traded. The ledger must lose exactly
    that part — not the whole position, and not nothing."""
    db = Database(str(tmp_path / "t.db"))
    db.initialize()
    db.insert_trade("PEP", "BUY", 30, 170.0, "entry", "r1",
                    broker_order_id="pep-buy", fill_status="filled")
    db.insert_trade("PEP", "TRAIL_STOP", 30, 160.0, "protect", "r1",
                    broker_order_id="pep-stop", fill_status="submitted")
    _set_fill(db, "pep-stop", status="partially_filled", qty=12.0)

    assert db.get_symbols_with_open_ledger_qty()["PEP"] == 18.0


def test_canceled_trail_stop_that_never_traded_is_not_an_exit(tmp_path):
    """A stop pulled before it fired moves no shares."""
    db = Database(str(tmp_path / "t.db"))
    db.initialize()
    db.insert_trade("WMT", "BUY", 12, 80.0, "entry", "r1",
                    broker_order_id="wmt-buy", fill_status="filled")
    db.insert_trade("WMT", "TRAIL_STOP", 12, 75.0, "protect", "r1",
                    broker_order_id="wmt-stop", fill_status="canceled")

    assert db.get_symbols_with_open_ledger_qty()["WMT"] == 12.0


def test_expired_trail_stop_that_never_traded_is_not_an_exit(tmp_path):
    db = Database(str(tmp_path / "t.db"))
    db.initialize()
    db.insert_trade("CVX", "BUY", 9, 150.0, "entry", "r1",
                    broker_order_id="cvx-buy", fill_status="filled")
    db.insert_trade("CVX", "TRAIL_STOP", 9, 140.0, "protect", "r1",
                    broker_order_id="cvx-stop", fill_status="expired")

    assert db.get_symbols_with_open_ledger_qty()["CVX"] == 9.0


def test_canceled_trail_stop_that_partially_traded_still_subtracts(tmp_path):
    """The regression trap inside this fix. `_is_filled_trail_stop` answers
    a REALIZED-EXIT question and returns False here (terminal status that
    is not 'filled'), but 5 shares genuinely left the broker's book.
    Deferring blindly to that helper would have made the ledger OVER-report
    by the traded size — a fresh instance of the same bug class, pointing
    the other way."""
    db = Database(str(tmp_path / "t.db"))
    db.initialize()
    db.insert_trade("TGT", "BUY", 12, 80.0, "entry", "r1",
                    broker_order_id="tgt-buy", fill_status="filled")
    db.insert_trade("TGT", "TRAIL_STOP", 12, 75.0, "protect", "r1",
                    broker_order_id="tgt-stop", fill_status="submitted")
    _set_fill(db, "tgt-stop", status="canceled", qty=5.0)

    assert db.get_symbols_with_open_ledger_qty()["TGT"] == 7.0


def test_resting_stop_alongside_a_real_partial_sale(tmp_path):
    """Both at once (EQNR id 12): a REDUCE that really traded, and
    protection still resting over the remainder. Only the REDUCE may move
    the number."""
    db = Database(str(tmp_path / "t.db"))
    db.initialize()
    db.insert_trade("EQNR", "BUY", 17.1924, 25.0, "entry", "r1",
                    broker_order_id="eqnr-buy", fill_status="filled")
    db.insert_trade("EQNR", "TRAIL_STOP", 17.1924, 23.0, "protect", "r1")
    _rest_the_stops(db, clear_order_id=True)
    db.insert_trade("EQNR", "REDUCE", 8.5962, 24.0, "trim", "r2",
                    broker_order_id="eqnr-reduce", fill_status="filled")
    _set_fill(db, "eqnr-reduce", status="filled", qty=8.5962)

    assert db.get_symbols_with_open_ledger_qty()["EQNR"] == 8.5962


def test_short_position_resting_stop_does_not_move_the_count(tmp_path):
    """Short side. A SHORT signs negative and its protective stop is a
    BUY-to-cover resting at the broker — still protection, still no
    quantity effect. The caller reads negatives as shorts and skips them,
    so a wrong number here would be silent."""
    db = Database(str(tmp_path / "t.db"))
    db.initialize()
    db.insert_trade("FLNC", "SHORT", 36, 7.39, "short entry", "r1",
                    broker_order_id="flnc-short", fill_status="filled")
    db.insert_trade("FLNC", "TRAIL_STOP", 36, 8.2, "protect", "r1")
    _rest_the_stops(db, clear_order_id=True)

    assert db.get_symbols_with_open_ledger_qty()["FLNC"] == -36.0


def test_other_enumerated_actions_keep_their_existing_signs(tmp_path):
    """Guard the rest of the signing rule against collateral damage:
    BUY/SWEEP_BUY add, SWEEP_SELL/STOP_OUT/REDUCE subtract, and HOLD plus a
    never-sent submit_failed row contribute nothing."""
    db = Database(str(tmp_path / "t.db"))
    db.initialize()
    db.insert_trade("SGOV", "SWEEP_BUY", 65, 100.0, "park", "r1",
                    broker_order_id="sw-b", fill_status="filled")
    db.insert_trade("SGOV", "SWEEP_SELL", 25, 100.1, "release", "r2",
                    broker_order_id="sw-s", fill_status="filled")
    db.insert_trade("OXY", "BUY", 31.3451, 45.0, "entry", "r1",
                    broker_order_id="oxy-buy", fill_status="filled")
    db.insert_trade("OXY", "STOP_OUT", 31.3451, 41.0, "stopped out", "r2",
                    broker_order_id="oxy-stop", fill_status="filled")
    db.insert_trade("XOM", "BUY", 10, 110.0, "entry", "r1",
                    broker_order_id="xom-buy", fill_status="filled")
    db.insert_trade("XOM", "REDUCE", 4, 112.0, "trim", "r2",
                    broker_order_id="xom-red", fill_status="filled")
    db.insert_trade("XOM", "HOLD", 0, 0, "hold", "r2")
    db.insert_trade("XOM", "BUY", 99, 110.0, "never sent", "r2",
                    broker_order_id="xom-fail", fill_status="submit_failed")

    net = db.get_symbols_with_open_ledger_qty()
    assert net["SGOV"] == 40.0
    assert net["OXY"] == 0.0
    assert net["XOM"] == 6.0


def test_reconciler_now_sees_a_stop_out_masked_by_a_resting_stop(tmp_path):
    """End-to-end consequence of the live defect. AMD's resting stop made
    the ledger read 0, and `_reconcile_stop_out_fills` skips any symbol it
    believes is flat — so an untracked broker sale there was undetectable
    by construction. With the fix the gap is seen and written back."""
    db = Database(str(tmp_path / "t.db"))
    db.initialize()
    db.insert_trade("AMD", "BUY", 2.0, 549.11, "entry", "r1",
                    broker_order_id="amd-buy", fill_status="filled")
    db.insert_trade("AMD", "TRAIL_STOP", 2.0, 500.0, "protect", "r1")
    _rest_the_stops(db, clear_order_id=True)

    broker = MagicMock()
    broker.get_positions.return_value = []  # broker flat: the stop fired
    broker.list_filled_sell_orders.return_value = [
        {"id": "untracked-stop-fill", "qty": 2.0, "price": 500.0,
         "filled_at": "2026-09-22 14:00:00"},
    ]

    pipeline = _mk_pipeline(db, broker)
    results = pipeline._reconcile_stop_out_fills(run_id="r9")

    assert [r["symbol"] for r in results] == ["AMD"]
    assert results[0]["ledger_qty"] == 2.0
    assert results[0]["recorded"] == 1
    assert any(r["action"] == "STOP_OUT" for r in db.get_trades(symbol="AMD"))


def test_reconciler_stays_a_no_op_on_a_genuinely_flat_symbol(tmp_path):
    """The mirror case: COP went NEGATIVE, which the caller also skips, so
    the defect was invisible from the reconciler's results either way.
    After the fix the number is a true 0, the pass is still a no-op, and
    no broker query or owner page happens."""
    db = Database(str(tmp_path / "t.db"))
    db.initialize()
    db.insert_trade("COP", "BUY", 5.3194, 100.0, "entry", "r1",
                    broker_order_id="cop-buy", fill_status="filled")
    db.insert_trade("COP", "TRAIL_STOP", 5.3194, 95.0, "protect", "r1")
    _rest_the_stops(db, clear_order_id=True)
    db.insert_trade("COP", "SELL", 5.3194, 99.0, "exit", "r2",
                    broker_order_id="cop-sell", fill_status="filled")
    _set_fill(db, "cop-sell", status="filled", qty=5.3194)

    broker = MagicMock()
    broker.get_positions.return_value = []

    pipeline = _mk_pipeline(db, broker)
    assert pipeline._reconcile_stop_out_fills(run_id="r9") == []
    broker.list_filled_sell_orders.assert_not_called()


def test_calibration_and_ledger_qty_agree_on_trail_stop_fill_state(tmp_path):
    """Cross-check the two accountings that read the same rows. A
    TRAIL_STOP that closes a lot in `compute_trade_calibration` must also
    be one that removes shares here, and one the calibration leaves open
    must still be counted as held. Drift between them is what produced
    this defect in the first place."""
    db = Database(str(tmp_path / "t.db"))
    db.initialize()
    # Three symbols whose protective stop really fired...
    for sym in ("AAA", "CCC", "DDD"):
        db.insert_trade(sym, "BUY", 10, 100.0, "entry", "r1",
                        broker_order_id=f"{sym}-buy", fill_status="filled")
        db.insert_trade(sym, "TRAIL_STOP", 10, 90.0, "protect", "r1",
                        broker_order_id=f"{sym}-stop", fill_status="submitted")
        db.conn.execute(
            "UPDATE trades SET fill_price = 90.0, fill_qty = 10.0, "
            "fill_status = 'filled' WHERE broker_order_id = ?", (f"{sym}-stop",))
    # ...and one whose stop is still resting, in the production row shape.
    db.insert_trade("BBB", "BUY", 10, 100.0, "entry", "r1",
                    broker_order_id="bbb-buy", fill_status="filled")
    db.insert_trade("BBB", "TRAIL_STOP", 10, 90.0, "protect", "r1")
    db.conn.execute(
        "UPDATE trades SET fill_status = NULL, fill_qty = NULL "
        "WHERE action = 'TRAIL_STOP' AND broker_order_id IS NULL")
    db.conn.commit()

    net = db.get_symbols_with_open_ledger_qty()
    assert net["AAA"] == net["CCC"] == net["DDD"] == 0.0  # the stops sold them
    assert net["BBB"] == 10.0  # this stop is still resting

    stats = db.compute_trade_calibration(lookback_days=3650)
    # Exactly the three stops that fired closed a round trip — BBB is still
    # open to BOTH accountings, which is the agreement being asserted.
    assert stats["n"] == 3
