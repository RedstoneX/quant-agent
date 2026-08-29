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
  3. Long-only behaviour is unchanged — proven both on a small fabricated
     ledger and on a READ-ONLY copy of the real production ledger, run
     through the function BEFORE and AFTER this change.
"""

import subprocess
import sys
import types
from pathlib import Path

import pytest

from src.storage.db import Database

PROD_DB = "/home/qamc/quant-agent/data/quant_agent.db"
# The commit this worktree started from — the last point at which
# `compute_trade_calibration` had NO short-side handling at all. Used to
# fetch the pre-fix implementation for a true before/after comparison on
# the real ledger, regardless of what gets committed on top of it later.
PRE_FIX_REV = "1c4492a"


def _insert(db: Database, symbol: str, action: str, qty: float, price: float,
            timestamp: str, fill_status: str = "filled"):
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
    _insert(db, "NVDA", "BUY", 10, 100.0, "2026-08-01 10:00:00")
    _insert(db, "NVDA", "SELL", 10, 120.0, "2026-08-03 10:00:00")
    # Long LOSS: BUY 10 AAPL @ 50, SELL @ 45 -> -10%
    _insert(db, "AAPL", "BUY", 10, 50.0, "2026-08-01 10:00:00")
    _insert(db, "AAPL", "SELL", 10, 45.0, "2026-08-04 10:00:00")
    # Short WIN: SHORT 10 TSLA @ 250, COVER @ 200 (price FELL) -> +20%
    _insert(db, "TSLA", "SHORT", 10, 250.0, "2026-08-01 10:00:00")
    _insert(db, "TSLA", "COVER", 10, 200.0, "2026-08-05 10:00:00")
    # Short LOSS: SHORT 10 MSFT @ 300, COVER @ 315 (price ROSE) -> -5%
    _insert(db, "MSFT", "SHORT", 10, 300.0, "2026-08-01 10:00:00")
    _insert(db, "MSFT", "COVER", 10, 315.0, "2026-08-02 10:00:00")

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
    _insert(db, "AAA", "SHORT", 10, 100.0, "2026-08-01 10:00:00")
    _insert(db, "AAA", "COVER", 10, 90.0, "2026-08-02 10:00:00")   # price fell -> WIN
    _insert(db, "BBB", "SHORT", 10, 100.0, "2026-08-01 10:00:00")
    _insert(db, "BBB", "COVER", 10, 110.0, "2026-08-02 10:00:00")  # price rose -> LOSS
    _insert(db, "CCC", "SHORT", 10, 100.0, "2026-08-01 10:00:00")
    _insert(db, "CCC", "COVER", 10, 100.0, "2026-08-02 10:00:00")  # unchanged -> breakeven

    calib = db.compute_trade_calibration(lookback_days=365)
    assert calib["n"] == 3
    assert calib["win_rate_pct"] == pytest.approx(100 / 3, abs=0.1)  # only AAA counts as a win


def test_partial_and_emergency_cover_labels_close_the_short_lot(tmp_path):
    """PARTIAL_COVER(n%) and EMERGENCY_COVER are the labels the execution
    path actually writes (src/pipeline_stages.py, src/pipeline.py) — both
    must close short lots, not just a bare 'COVER'."""
    db = Database(str(tmp_path / "t.db"))
    db.initialize()

    _insert(db, "AAA", "SHORT", 20, 100.0, "2026-08-01 10:00:00")
    _insert(db, "AAA", "PARTIAL_COVER(50%)", 10, 90.0, "2026-08-02 10:00:00")
    _insert(db, "BBB", "SHORT", 10, 100.0, "2026-08-01 10:00:00")
    _insert(db, "BBB", "EMERGENCY_COVER", 10, 80.0, "2026-08-02 10:00:00")
    _insert(db, "CCC", "SHORT", 10, 100.0, "2026-08-01 10:00:00")
    _insert(db, "CCC", "COVER", 10, 70.0, "2026-08-02 10:00:00")

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

    _insert(db, "NVDA", "BUY", 10, 100.0, "2026-08-01 10:00:00")
    _insert(db, "NVDA", "SELL", 10, 110.0, "2026-08-03 10:00:00")
    _insert(db, "AAPL", "BUY", 10, 50.0, "2026-08-01 10:00:00")
    _insert(db, "AAPL", "SELL", 10, 45.0, "2026-08-04 10:00:00")
    _insert(db, "JPM", "BUY", 10, 200.0, "2026-08-01 10:00:00")
    _insert(db, "JPM", "SELL", 10, 210.0, "2026-08-06 10:00:00")

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

def _prod_db_copy(tmp_path) -> Path | None:
    """A READ-ONLY copy of the real production ledger, made via `sudo -n -u
    qamc cat` (never opening/writing the original). Returns None (test
    skips) when the source is unreachable in this environment — this proof
    is a nice-to-have on top of the unit tests above, not a hard gate on
    environments without qamc sudo access."""
    dest = tmp_path / "prod_quant_agent.db"
    try:
        result = subprocess.run(
            ["sudo", "-n", "-u", "qamc", "cat", PROD_DB],
            capture_output=True, timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0 or not result.stdout:
        return None
    dest.write_bytes(result.stdout)
    return dest


def _load_prefix_compute_trade_calibration():
    """The `compute_trade_calibration` method as it existed at PRE_FIX_REV
    (before ANY Stage-3 short-side handling), loaded as a standalone
    function so it can be bound to a fresh Database instance pointed at the
    SAME sqlite file the current implementation runs against. Returns None
    when the revision can't be fetched (e.g. shallow clone / detached
    environment) — the caller skips in that case."""
    try:
        old_source = subprocess.run(
            ["git", "show", f"{PRE_FIX_REV}:src/storage/db.py"],
            capture_output=True, text=True, timeout=30, cwd=str(Path(__file__).resolve().parent.parent),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if old_source.returncode != 0 or not old_source.stdout:
        return None
    module = types.ModuleType("_prefix_db_module")
    try:
        exec(compile(old_source.stdout, f"<{PRE_FIX_REV}:src/storage/db.py>", "exec"),
             module.__dict__)
    except Exception:
        return None
    return getattr(module.Database, "compute_trade_calibration", None)


def test_real_production_ledger_long_only_fields_are_byte_identical_before_and_after(tmp_path):
    """Runs the REAL production-shaped ledger through BOTH the pre-fix
    (PRE_FIX_REV) and the current `compute_trade_calibration`, on a
    READ-ONLY copy, and asserts every field that existed BEFORE this change
    is identical. New fields (`expectancy_pct`, `avg_win_loss_ratio`,
    `by_side`) are additions the task itself requires, so full dict
    equality is not the right invariant here — equality on the ORIGINAL
    keys is, and that's what proves long-only behaviour didn't move."""
    db_path = _prod_db_copy(tmp_path)
    if db_path is None:
        pytest.skip("real production ledger not reachable via sudo -n -u qamc in this environment")

    old_fn = _load_prefix_compute_trade_calibration()
    if old_fn is None:
        pytest.skip(f"could not load pre-fix compute_trade_calibration from {PRE_FIX_REV}")

    db = Database(str(db_path))
    db.initialize()

    new_result = db.compute_trade_calibration(lookback_days=45)
    old_result = old_fn(db, lookback_days=45)

    print(f"\n[real ledger] BEFORE (pre-fix, {PRE_FIX_REV}): {old_result}")
    print(f"[real ledger] AFTER  (this change):            {new_result}")

    if not old_result and not new_result:
        # Too few closed trades in this copy to be meaningful either way —
        # the byte-identical claim holds trivially (both are {}), but
        # report it so a human reading test output sees why.
        assert new_result == old_result == {}
        return

    original_keys = ("n", "win_rate_pct", "avg_return_pct", "avg_hold_days")
    for key in original_keys:
        assert new_result.get(key) == old_result.get(key), (
            f"{key} changed: before={old_result.get(key)!r} "
            f"after={new_result.get(key)!r}"
        )
    # By-size sub-buckets go through the SAME `_bucket_stats` helper as the
    # top level, so they ALSO gain `expectancy_pct` / `avg_win_loss_ratio` —
    # compare only the fields that existed pre-fix, same reasoning as above.
    for size_bucket in ("large (≥$10k)", "medium ($5-10k)", "small (<$5k)"):
        new_bucket = new_result.get("by_size", {}).get(size_bucket) or {}
        old_bucket = old_result.get("by_size", {}).get(size_bucket) or {}
        for key in original_keys:
            assert new_bucket.get(key) == old_bucket.get(key), (
                f"by_size[{size_bucket!r}][{key!r}] changed: "
                f"before={old_bucket.get(key)!r} after={new_bucket.get(key)!r}"
            )

    # And confirm the new fields exist and are internally consistent, even
    # though they have no "before" counterpart to compare against.
    assert "expectancy_pct" in new_result
    assert new_result["expectancy_pct"] == new_result["avg_return_pct"]
    assert "by_side" in new_result
    assert new_result["by_side"]["long"]["n"] + new_result["by_side"]["short"]["n"] == new_result["n"]
