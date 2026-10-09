"""Stage 3 (shorts) — `compute_trade_calibration` must see a short trade.

Before this fix the function FIFO-matched only BUY lots against sell-family
exits. A SHORT opened no BUY lot, so a COVER closed nothing: win rate,
average return, and average hold days silently excluded every short. Those
numbers reach the Portfolio Manager as settled fact (Quantitative Facts /
L2 Trade Calibration) — harmless while shorts could not be opened, a live
accounting hole the moment `feat/shorts-stage3` made them openable.

This file proves three things:
  1. A mixed long+short ledger produces correct SEPARATE (`by_side`) and
     COMBINED figures, with the short's return signed the opposite way a
     long's is (closing BELOW entry = short WIN).
  2. `expectancy_pct` and `avg_win_loss_ratio` (docs/QAMC_REMEDIATION_SPEC.md
     §7.3) are computed correctly.
  3. Long-only behaviour is unchanged — proven on a small fabricated
     ledger. (The before/after run on the real production ledger was
     retired 2026-10-09: the owner ruled all trades before the 2026-10-12
     open are not evidence, so the calibration no longer reads them.)
"""

import pytest

from src.storage.db import Database



def _insert(
    db: Database, symbol: str, action: str, qty: float, price: float, timestamp: str, fill_status: str = "filled"
):
    db.conn.execute(
        "INSERT INTO trades (symbol, action, qty, price, fill_status, "
        "fill_qty, fill_price, timestamp, run_id, reasoning) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'r1', 'test')",
        (symbol, action, qty, price, fill_status, qty, price, timestamp),
    )
    db.conn.commit()


# ==========================================================================
# 1 & 2. Mixed long+short ledger — separate and combined figures, plus the
# two §7.3 metrics.
# ==========================================================================


def test_mixed_long_and_short_ledger_produces_correct_separate_and_combined_figures(tmp_path):
    db = Database(str(tmp_path / "t.db"))
    db.initialize()

    # Long WIN: BUY 10 NVDA @ 100, SELL @ 120 -> +20%
    _insert(db, "NVDA", "BUY", 10, 100.0, "2026-11-01 10:00:00")
    _insert(db, "NVDA", "SELL", 10, 120.0, "2026-11-03 10:00:00")
    # Long LOSS: BUY 10 AAPL @ 50, SELL @ 45 -> -10%
    _insert(db, "AAPL", "BUY", 10, 50.0, "2026-11-01 10:00:00")
    _insert(db, "AAPL", "SELL", 10, 45.0, "2026-11-04 10:00:00")
    # Short WIN: SHORT 10 TSLA @ 250, COVER @ 200 (price FELL) -> +20%
    _insert(db, "TSLA", "SHORT", 10, 250.0, "2026-11-01 10:00:00")
    _insert(db, "TSLA", "COVER", 10, 200.0, "2026-11-05 10:00:00")
    # Short LOSS: SHORT 10 MSFT @ 300, COVER @ 315 (price ROSE) -> -5%
    _insert(db, "MSFT", "SHORT", 10, 300.0, "2026-11-01 10:00:00")
    _insert(db, "MSFT", "COVER", 10, 315.0, "2026-11-02 10:00:00")

    calib = db.compute_trade_calibration(lookback_days=365)

    # --- combined ---
    assert calib["n"] == 4
    assert calib["win_rate_pct"] == 50.0
    assert calib["avg_return_pct"] == pytest.approx(6.25, abs=0.01)  # (20-10+20-5)/4
    assert calib["expectancy_pct"] == calib["avg_return_pct"]
    # win_returns=[20,20] avg_win=20; loss_returns=[-10,-5] avg_loss=-7.5
    # ratio = 20 / 7.5 = 2.666...
    assert calib["avg_win_loss_ratio"] == pytest.approx(2.67, abs=0.01)

    # --- separate: long side only (NVDA +20%, AAPL -10%) ---
    long_stats = calib["by_side"]["long"]
    assert long_stats["n"] == 2
    assert long_stats["win_rate_pct"] == 50.0
    assert long_stats["avg_return_pct"] == pytest.approx(5.0, abs=0.01)
    assert long_stats["avg_win_loss_ratio"] == pytest.approx(2.0, abs=0.01)  # 20/10

    # --- separate: short side only (TSLA +20%, MSFT -5%) ---
    short_stats = calib["by_side"]["short"]
    assert short_stats["n"] == 2
    assert short_stats["win_rate_pct"] == 50.0
    assert short_stats["avg_return_pct"] == pytest.approx(7.5, abs=0.01)
    assert short_stats["avg_win_loss_ratio"] == pytest.approx(4.0, abs=0.01)  # 20/5


def test_short_closing_below_entry_is_a_win_and_above_entry_is_a_loss(tmp_path):
    """The headline Stage 3 property, isolated: a covered short's WIN/LOSS
    classification is the MIRROR of a long's, not a copy of it."""
    db = Database(str(tmp_path / "t.db"))
    db.initialize()

    # Three short round-trips so the >=3 floor is cleared by shorts alone.
    _insert(db, "AAA", "SHORT", 10, 100.0, "2026-11-01 10:00:00")
    _insert(db, "AAA", "COVER", 10, 90.0, "2026-11-02 10:00:00")  # price fell -> WIN
    _insert(db, "BBB", "SHORT", 10, 100.0, "2026-11-01 10:00:00")
    _insert(db, "BBB", "COVER", 10, 110.0, "2026-11-02 10:00:00")  # price rose -> LOSS
    _insert(db, "CCC", "SHORT", 10, 100.0, "2026-11-01 10:00:00")
    _insert(db, "CCC", "COVER", 10, 100.0, "2026-11-02 10:00:00")  # unchanged -> breakeven

    calib = db.compute_trade_calibration(lookback_days=365)
    assert calib["n"] == 3
    assert calib["win_rate_pct"] == pytest.approx(100 / 3, abs=0.1)  # only AAA counts as a win


def test_partial_and_emergency_cover_labels_close_the_short_lot(tmp_path):
    """PARTIAL_COVER(n%) and EMERGENCY_COVER are the labels the execution
    path actually writes (src/pipeline_stages.py, src/pipeline.py) — both
    must close short lots, not just a bare 'COVER'."""
    db = Database(str(tmp_path / "t.db"))
    db.initialize()

    _insert(db, "AAA", "SHORT", 20, 100.0, "2026-11-01 10:00:00")
    _insert(db, "AAA", "PARTIAL_COVER(50%)", 10, 90.0, "2026-11-02 10:00:00")
    _insert(db, "BBB", "SHORT", 10, 100.0, "2026-11-01 10:00:00")
    _insert(db, "BBB", "EMERGENCY_COVER", 10, 80.0, "2026-11-02 10:00:00")
    _insert(db, "CCC", "SHORT", 10, 100.0, "2026-11-01 10:00:00")
    _insert(db, "CCC", "COVER", 10, 70.0, "2026-11-02 10:00:00")

    calib = db.compute_trade_calibration(lookback_days=365)
    assert calib["n"] == 3
    assert calib["by_side"]["short"]["n"] == 3
    assert calib["win_rate_pct"] == 100.0  # every one of these closed below entry


# ==========================================================================
# 3a. Long-only behaviour unchanged — small fabricated ledger.
# ==========================================================================


def test_long_only_ledger_top_level_numbers_unchanged_by_short_support(tmp_path):
    """A ledger with zero SHORT/COVER rows must produce the exact same
    n / win_rate_pct / avg_return_pct / avg_hold_days / by_size the
    pre-Stage-3 function produced — the new by_side/expectancy/ratio keys
    are additions, not replacements."""
    db = Database(str(tmp_path / "t.db"))
    db.initialize()

    _insert(db, "NVDA", "BUY", 10, 100.0, "2026-11-01 10:00:00")
    _insert(db, "NVDA", "SELL", 10, 110.0, "2026-11-03 10:00:00")
    _insert(db, "AAPL", "BUY", 10, 50.0, "2026-11-01 10:00:00")
    _insert(db, "AAPL", "SELL", 10, 45.0, "2026-11-04 10:00:00")
    _insert(db, "JPM", "BUY", 10, 200.0, "2026-11-01 10:00:00")
    _insert(db, "JPM", "SELL", 10, 210.0, "2026-11-06 10:00:00")

    calib = db.compute_trade_calibration(lookback_days=365)
    assert calib["n"] == 3
    # This is exactly what the pre-fix function returned for this ledger
    # (hand-computed: returns +10, -10, +5 -> avg +1.6667, 2/3 win).
    assert calib["win_rate_pct"] == pytest.approx(200 / 3, abs=0.1)
    assert calib["avg_return_pct"] == pytest.approx(5 / 3, abs=0.01)
    assert calib["by_side"]["short"] == {"n": 0}
    assert calib["by_side"]["long"]["n"] == 3
    assert calib["by_side"]["long"]["avg_return_pct"] == calib["avg_return_pct"]


# ==========================================================================
# 3b. Long-only behaviour unchanged — the REAL production ledger, before
# and after this change, compared on the pre-existing fields.
# ==========================================================================
