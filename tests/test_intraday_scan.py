"""Intraday opportunity-discovery (2026-08-19 fix) invariants.

The full opportunity-generation chain (macro/news/tech/earnings -> PM -> RM
-> deterministic gate -> execution) previously ran once each morning;
`intra_check` (every ~30 min) was loss-protection only, so a material move
developing after the morning run could not generate a new trade.

This suite covers `TradingPipeline._run_intraday_opportunity_scan` and its
wiring into `run_intra_check`:
  1. Disabled by default — a config without `intraday_scan.enabled: true`
     leaves intra_check's existing behavior completely unchanged.
  2. A material move (bullish OR via an inverse ETF, bearish) that clears
     the threshold reaches the SAME DecisionStage -> RiskStage ->
     ExecutionStage chain morning uses.
  3. A move below threshold, or a symbol still in cooldown, is skipped.
  4. A daily-loss breach suppresses the scan even when there was nothing
     to force-liquidate.
  5. The scan backs off while another session is mid-flight. `intra_check`
     is deliberately exempt from the wrapper's cross-mode session lock
     (`scripts/run_if_et_window.sh`) so the circuit breaker always fires —
     an exemption justified on the grounds that intra_check's actions are
     all idempotent. Opening a new position is not, so this path needs its
     own fail-closed concurrency guard.
"""
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from src.config import IntradayScanConfig
from src.cost_circuit import PaidAnalysisSuspended
from src.models import TechAnalysisResult, TechReasoningChain
from src.pipeline import TradingPipeline
from src.pipeline_context import RunContext


def _ta_result(symbol, rating="buy"):
    return TechAnalysisResult(
        symbol=symbol, rating=rating, conviction="medium",
        entry_price=100.0, stop_loss=95.0, reference_target=110.0,
        support_levels=[95.0], resistance_levels=[110.0],
        setup_type="range", expected_horizon_sessions=10,
        reasoning_chain=TechReasoningChain(
            trend="x", momentum="x", volatility="x", volume="x",
            support_resistance="x",
        ),
        reasoning="test",
    )


def _intraday_pipeline(universe=("SPY", "SQQQ", "AAPL"), enabled=True,
                       move_threshold_pct=3.0, cooldown_hours=3.0,
                       max_candidates=5, cooldown_rows=None, db_path=None):
    pipeline = TradingPipeline.__new__(TradingPipeline)
    pipeline.config = SimpleNamespace(
        trading=SimpleNamespace(universe=list(universe), lookback_days=100),
        # The scan takes an advisory flock next to the DB file; point it at
        # a per-test temp dir so tests never contend with each other or
        # write into the repo.
        storage=SimpleNamespace(
            db_path=str(db_path or (Path(tempfile.mkdtemp()) / "t.db")),
        ),
        intraday_scan=IntradayScanConfig(
            enabled=enabled, move_threshold_pct=move_threshold_pct,
            cooldown_hours=cooldown_hours, max_candidates_per_scan=max_candidates,
        ),
    )
    pipeline.broker = MagicMock()
    pipeline.db = MagicMock()
    pipeline.db.get_trades.return_value = cooldown_rows or []
    pipeline.market = MagicMock()
    pipeline.market.get_ohlcv.return_value = [MagicMock()]  # non-empty; compute_indicators is patched
    pipeline.macro_store = MagicMock()
    pipeline.macro_store.load_last_state.return_value = {}
    pipeline.tech_store = MagicMock()
    pipeline.tech_store.load.return_value = {}
    pipeline.tech_store.compute_ages.return_value = {}
    pipeline.tech_analyst = MagicMock()
    pipeline.decision_stage = MagicMock()
    pipeline.risk_stage = MagicMock()
    pipeline.execution_stage = MagicMock()
    return pipeline


def _snapshot(last, prev):
    return {"last_price": last, "prev_close": prev}


# ---------- disabled by default ----------

def test_intraday_scan_config_defaults_disabled():
    cfg = IntradayScanConfig()
    assert cfg.enabled is False


def test_scan_is_inert_when_disabled():
    p = _intraday_pipeline(enabled=False)
    ctx = RunContext.start("intra_check")
    result = p._run_intraday_opportunity_scan(ctx)
    # 2026-08-31 visibility fix: an explicit status, not a bare None — see
    # tests/test_intraday_scan_crash_visibility.py for the full coverage of
    # this and the other two "everyday, healthy, no new activity" statuses.
    assert result == {"status": "intraday_scan_disabled", "run_id": ctx.run_id}
    p.broker.get_intraday_snapshots.assert_not_called()


# ---------- material move reaches the shared decision chain ----------

@patch("src.pipeline.compute_indicators")
def test_material_bullish_move_reaches_decision_chain(mock_compute_indicators):
    """A symbol that moved well past threshold since the last close must
    flow through tech_analyst and then the SAME DecisionStage -> RiskStage
    -> ExecutionStage objects morning uses — no separate/duplicated PM/RM
    logic for the intraday path."""
    mock_compute_indicators.return_value = MagicMock()
    p = _intraday_pipeline(universe=["SPY", "AAPL"])
    p.broker.get_intraday_snapshots.return_value = {
        "SPY": _snapshot(last=500.0, prev=499.0),      # 0.2% — below threshold
        "AAPL": _snapshot(last=110.0, prev=100.0),     # 10% — qualifies
    }
    analysis = _ta_result("AAPL", rating="buy")
    p.tech_analyst.analyze_batch.return_value = (
        {"AAPL": analysis},
        MagicMock(user_message="m", raw_text="{}", tokens_used=1,
                  input_tokens=1, output_tokens=1, cost_usd=0.0, model="t"),
    )
    p.decision_stage.run.side_effect = lambda ctx: setattr(
        ctx, "portfolio_decision",
        SimpleNamespace(decisions=[SimpleNamespace(action="BUY", symbol="AAPL")]),
    )
    p.risk_stage.run.return_value = None  # no early-exit -> proceed to execution
    p.execution_stage.run.return_value = [{"id": "o1", "action": "BUY", "symbol": "AAPL"}]

    ctx = RunContext.start("intra_check")
    result = p._run_intraday_opportunity_scan(ctx)

    assert result["status"] == "intraday_executed"
    assert result["candidates"] == ["AAPL"]
    # tech_analyst was only asked about the qualifying symbol, not SPY.
    submitted_symbols = [
        s["symbol"] for s in p.tech_analyst.analyze_batch.call_args.args[0]
    ]
    assert submitted_symbols == ["AAPL"]
    # The exact same stage objects morning uses were invoked — proves reuse,
    # not a parallel decision path.
    p.decision_stage.run.assert_called_once_with(ctx)
    p.risk_stage.run.assert_called_once_with(ctx)
    p.execution_stage.run.assert_called_once_with(ctx)
    assert ctx.analyses == [analysis]


@patch("src.pipeline.compute_indicators")
def test_bearish_move_surfaces_through_inverse_etf(mock_compute_indicators):
    """A broad-market decline shows up as a qualifying move in an approved
    inverse ETF (SQQQ) the same way a rally shows up in a long candidate —
    no separate bearish code path, no direct shorting."""
    mock_compute_indicators.return_value = MagicMock()
    p = _intraday_pipeline(universe=["SPY", "SQQQ"])
    p.broker.get_intraday_snapshots.return_value = {
        "SPY": _snapshot(last=498.0, prev=500.0),      # -0.4%, below threshold
        "SQQQ": _snapshot(last=112.0, prev=100.0),     # +12% (3x inverse Nasdaq)
    }
    analysis = _ta_result("SQQQ", rating="buy")
    p.tech_analyst.analyze_batch.return_value = (
        {"SQQQ": analysis},
        MagicMock(user_message="m", raw_text="{}", tokens_used=1,
                  input_tokens=1, output_tokens=1, cost_usd=0.0, model="t"),
    )
    p.decision_stage.run.side_effect = lambda ctx: setattr(
        ctx, "portfolio_decision",
        SimpleNamespace(decisions=[SimpleNamespace(action="BUY", symbol="SQQQ")]),
    )
    p.risk_stage.run.return_value = None
    p.execution_stage.run.return_value = [{"id": "o1", "action": "BUY", "symbol": "SQQQ"}]

    ctx = RunContext.start("intra_check")
    result = p._run_intraday_opportunity_scan(ctx)

    assert result["status"] == "intraday_executed"
    assert result["candidates"] == ["SQQQ"]
    submitted_symbols = [
        s["symbol"] for s in p.tech_analyst.analyze_batch.call_args.args[0]
    ]
    assert submitted_symbols == ["SQQQ"], "SPY's own sub-threshold move must not qualify"


@patch("src.pipeline.compute_indicators")
def test_below_threshold_move_never_calls_tech_analyst(mock_compute_indicators):
    p = _intraday_pipeline(universe=["AAPL"], move_threshold_pct=3.0)
    p.broker.get_intraday_snapshots.return_value = {
        "AAPL": _snapshot(last=101.0, prev=100.0),  # 1% — below 3% threshold
    }
    ctx = RunContext.start("intra_check")
    result = p._run_intraday_opportunity_scan(ctx)
    assert result == {"status": "intraday_scan_no_opportunity", "run_id": ctx.run_id}
    p.tech_analyst.analyze_batch.assert_not_called()
    p.decision_stage.run.assert_not_called()


# ---------- cooldown / dedup ----------

@patch("src.pipeline.compute_indicators")
def test_cooldown_suppresses_repeat_scan_of_same_symbol(mock_compute_indicators):
    """A symbol already evaluated by an intra_check-triggered scan within
    the cooldown window must not be re-submitted to tech_analyst on the
    very next tick, even though it's still moved past threshold —
    otherwise a slow-developing move churns the same setup every 30 min."""
    from datetime import datetime, timedelta, timezone

    mock_compute_indicators.return_value = MagicMock()
    # Aged 30 min (not "now"): the fixture's `db.get_trades` mock backs BOTH
    # `_another_session_recently_active`'s 15-min concurrency guard and
    # `_recently_intraday_evaluated`'s legacy-fallback cooldown lookup — a
    # "just now" timestamp would trip the concurrency guard FIRST and return
    # "intraday_scan_lock_contended" without ever reaching the cooldown logic
    # this test claims to isolate (2026-08-31 finding, surfaced only once the
    # two outcomes got distinct statuses — same isolation pattern already
    # used below by test_non_intra_check_trades_do_not_count_toward_cooldown).
    # 30 min clears the 15-min concurrency window while staying well inside
    # the 3h cooldown window under test.
    aged_ts = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()
    p = _intraday_pipeline(
        universe=["AAPL"], cooldown_hours=3.0,
        cooldown_rows=[{
            "symbol": "AAPL", "action": "HOLD", "run_id": "intra_check-abcd1234",
            "timestamp": aged_ts, "reasoning": "prior intraday scan",
        }],
    )
    p.broker.get_intraday_snapshots.return_value = {
        "AAPL": _snapshot(last=110.0, prev=100.0),  # still 10% — well past threshold
    }

    ctx = RunContext.start("intra_check")
    result = p._run_intraday_opportunity_scan(ctx)

    assert result == {"status": "intraday_scan_no_opportunity", "run_id": ctx.run_id}
    p.tech_analyst.analyze_batch.assert_not_called()


@patch("src.pipeline.compute_indicators")
def test_cooldown_expired_allows_rescan(mock_compute_indicators):
    """A prior intraday-scan row OLDER than the cooldown window must not
    suppress a fresh scan — the dedup is time-bounded, not permanent."""
    from datetime import datetime, timedelta, timezone

    mock_compute_indicators.return_value = MagicMock()
    stale_ts = (datetime.now(timezone.utc) - timedelta(hours=6)).isoformat()
    p = _intraday_pipeline(
        universe=["AAPL"], cooldown_hours=3.0,
        cooldown_rows=[{
            "symbol": "AAPL", "action": "HOLD", "run_id": "intra_check-abcd1234",
            "timestamp": stale_ts, "reasoning": "prior intraday scan",
        }],
    )
    p.broker.get_intraday_snapshots.return_value = {
        "AAPL": _snapshot(last=110.0, prev=100.0),
    }
    p.tech_analyst.analyze_batch.return_value = ({}, None)

    ctx = RunContext.start("intra_check")
    p._run_intraday_opportunity_scan(ctx)

    p.tech_analyst.analyze_batch.assert_called_once()


@patch("src.pipeline.compute_indicators")
def test_non_intra_check_trades_do_not_count_toward_cooldown(mock_compute_indicators):
    """A morning-run HOLD/BUY for the symbol must not suppress the
    intraday scan — the symbol cooldown is specifically about repeat
    INTRADAY scans, not about any historical trade row.

    The row is aged 30 min so it sits OUTSIDE the 15-min concurrent-session
    guard but well INSIDE the 3h symbol cooldown window — isolating the
    cooldown behavior under test from the separate concurrency guard."""
    from datetime import datetime, timedelta, timezone

    mock_compute_indicators.return_value = MagicMock()
    morning_ts = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()
    p = _intraday_pipeline(
        universe=["AAPL"],
        cooldown_rows=[{
            "symbol": "AAPL", "action": "HOLD", "run_id": "run-abcd1234",
            "timestamp": morning_ts, "reasoning": "morning run",
        }],
    )
    p.broker.get_intraday_snapshots.return_value = {
        "AAPL": _snapshot(last=110.0, prev=100.0),
    }
    p.tech_analyst.analyze_batch.return_value = ({}, None)

    ctx = RunContext.start("intra_check")
    p._run_intraday_opportunity_scan(ctx)

    p.tech_analyst.analyze_batch.assert_called_once()


# ---------- bounded cost ----------

@patch("src.pipeline.compute_indicators")
def test_candidates_capped_at_max_per_scan(mock_compute_indicators):
    """Even on a broad market-wide move day, only the top N (by move size)
    candidates get a real tech_analyst call — bounded, not high-frequency."""
    mock_compute_indicators.return_value = MagicMock()
    universe = [f"SYM{i}" for i in range(10)]
    p = _intraday_pipeline(universe=universe, max_candidates=2, move_threshold_pct=3.0)
    p.broker.get_intraday_snapshots.return_value = {
        sym: _snapshot(last=100.0 + i, prev=100.0) for i, sym in enumerate(universe)
    }
    p.tech_analyst.analyze_batch.return_value = ({}, None)

    ctx = RunContext.start("intra_check")
    p._run_intraday_opportunity_scan(ctx)

    submitted = p.tech_analyst.analyze_batch.call_args.args[0]
    assert len(submitted) == 2
    # Largest movers first: SYM9 (+9) and SYM8 (+8).
    assert {s["symbol"] for s in submitted} == {"SYM9", "SYM8"}


# ---------- run_intra_check wiring ----------

def test_run_intra_check_skips_scan_on_daily_loss_breach_even_with_no_positions():
    """A daily-loss breach must suppress the intraday scan even in the
    branch where there's nothing to force-liquidate (no positions) —
    a breach day must never add new risk."""
    p = TradingPipeline.__new__(TradingPipeline)
    p.config = SimpleNamespace(
        trading=SimpleNamespace(universe=["AAPL"], lookback_days=100),
        intraday_scan=IntradayScanConfig(enabled=True),
    )
    p.broker = MagicMock()
    p.broker.is_trading_day.return_value = True
    p.broker.get_account.return_value = {
        "cash": 1000.0, "portfolio_value": 9600.0, "last_equity": 10000.0,
        "non_marginable_buying_power": 1000.0,
    }
    p.broker.get_positions.return_value = []  # nothing to force-liquidate
    p.db = MagicMock()
    p._drain_pending_protection_restores = MagicMock()
    p._reconcile_stop_coverage = MagicMock(return_value=[])
    p._reconcile_orphan_pending_submits = MagicMock()
    p._is_trading_day = MagicMock(return_value=True)
    p.risk_engine = MagicMock()
    p.risk_engine.check_daily_loss.return_value = MagicMock(message="breach")
    p._run_intraday_opportunity_scan = MagicMock(return_value={"status": "intraday_executed"})

    result = p.run_intra_check()

    assert result["status"] == "ok"
    assert "intraday_scan" not in result
    p._run_intraday_opportunity_scan.assert_not_called()


def test_run_intra_check_runs_scan_when_no_breach():
    p = TradingPipeline.__new__(TradingPipeline)
    p.config = SimpleNamespace(
        trading=SimpleNamespace(universe=["AAPL"], lookback_days=100),
        intraday_scan=IntradayScanConfig(enabled=True),
    )
    p.broker = MagicMock()
    p.broker.is_trading_day.return_value = True
    p.broker.get_account.return_value = {
        "cash": 10000.0, "portfolio_value": 10100.0, "last_equity": 10000.0,
        "non_marginable_buying_power": 10000.0,
    }
    p.broker.get_positions.return_value = []
    p.db = MagicMock()
    p._drain_pending_protection_restores = MagicMock()
    p._reconcile_stop_coverage = MagicMock(return_value=[])
    p._reconcile_orphan_pending_submits = MagicMock()
    p._is_trading_day = MagicMock(return_value=True)
    p.risk_engine = MagicMock()
    p.risk_engine.check_daily_loss.return_value = None
    p._run_intraday_opportunity_scan = MagicMock(
        return_value={"status": "intraday_no_trades", "candidates": []},
    )

    result = p.run_intra_check()

    assert result["status"] == "ok"
    assert result["intraday_scan"]["status"] == "intraday_no_trades"
    p._run_intraday_opportunity_scan.assert_called_once()


def test_run_intra_check_scan_crash_does_not_fail_the_tick():
    """A scan exception must never turn a routine intra_check run into a
    failed/errored result — the loss-protection check it wraps around
    already succeeded — but it must NOT disappear silently either.

    2026-08-30 operator-honesty fix: before this, the exception handler set
    scan_result to None, which meant no `intraday_scan` key was attached at
    all — byte-identical to a scan that ran and correctly found nothing.
    Now a crash attaches an explicit error-status dict (see
    tests/test_intraday_scan_crash_visibility.py for the full coverage of
    that fix); this test only re-asserts the still-non-negotiable half of
    the contract — the tick itself completes as "ok", never as a failure.
    """
    p = TradingPipeline.__new__(TradingPipeline)
    p.config = SimpleNamespace(
        trading=SimpleNamespace(universe=["AAPL"], lookback_days=100),
        intraday_scan=IntradayScanConfig(enabled=True),
    )
    p.broker = MagicMock()
    p.broker.is_trading_day.return_value = True
    p.broker.get_account.return_value = {
        "cash": 10000.0, "portfolio_value": 10100.0, "last_equity": 10000.0,
        "non_marginable_buying_power": 10000.0,
    }
    p.broker.get_positions.return_value = []
    p.db = MagicMock()
    p._drain_pending_protection_restores = MagicMock()
    p._reconcile_stop_coverage = MagicMock(return_value=[])
    p._reconcile_orphan_pending_submits = MagicMock()
    p._is_trading_day = MagicMock(return_value=True)
    p.risk_engine = MagicMock()
    p.risk_engine.check_daily_loss.return_value = None
    p._run_intraday_opportunity_scan = MagicMock(side_effect=RuntimeError("boom"))

    result = p.run_intra_check()

    assert result["status"] == "ok"
    assert result["intraday_scan"]["status"] == "intraday_scan_crashed"
    assert "boom" in result["intraday_scan"]["error"]


# ---------- concurrency guard (intra_check is session-lock exempt) ----------

@patch("src.pipeline.compute_indicators")
def test_scan_skips_when_another_session_is_mid_flight(mock_compute_indicators):
    """`scripts/run_if_et_window.sh` deliberately exempts intra_check from
    the cross-mode session lock so the circuit breaker always fires — an
    exemption it justifies on the grounds that intra_check's actions are
    all IDEMPOTENT. Opening a new position is not. So the scan (not the
    loss check) must back off when another session wrote a trade row
    recently, or a concurrent morning run and this scan could both size
    against the same pre-fill snapshot and breach position/cash caps."""
    from datetime import datetime, timezone

    mock_compute_indicators.return_value = MagicMock()
    recent_ts = datetime.now(timezone.utc).isoformat()
    p = _intraday_pipeline(universe=["AAPL"])
    p.db.get_trades.return_value = [{
        "symbol": "MSFT", "action": "BUY", "run_id": "run-morning1",
        "timestamp": recent_ts, "reasoning": "morning run mid-flight",
    }]
    p.broker.get_intraday_snapshots.return_value = {
        "AAPL": _snapshot(last=110.0, prev=100.0),
    }

    ctx = RunContext.start("intra_check")
    result = p._run_intraday_opportunity_scan(ctx)

    assert result == {"status": "intraday_scan_lock_contended", "run_id": ctx.run_id}
    p.broker.get_intraday_snapshots.assert_not_called()
    p.tech_analyst.analyze_batch.assert_not_called()


@patch("src.pipeline.compute_indicators")
def test_scan_proceeds_when_other_session_activity_is_old(mock_compute_indicators):
    """The concurrency guard is time-bounded — a morning run that finished
    hours ago must not suppress the scan for the rest of the day (a resting
    order or an old trade row is not an in-flight session)."""
    from datetime import datetime, timedelta, timezone

    mock_compute_indicators.return_value = MagicMock()
    old_ts = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    p = _intraday_pipeline(universe=["AAPL"])
    p.db.get_trades.return_value = [{
        "symbol": "MSFT", "action": "BUY", "run_id": "run-morning1",
        "timestamp": old_ts, "reasoning": "morning run, long finished",
    }]
    p.broker.get_intraday_snapshots.return_value = {
        "AAPL": _snapshot(last=110.0, prev=100.0),
    }
    p.tech_analyst.analyze_batch.return_value = ({}, None)

    ctx = RunContext.start("intra_check")
    p._run_intraday_opportunity_scan(ctx)

    p.tech_analyst.analyze_batch.assert_called_once()


def test_concurrency_guard_fails_closed_on_query_error():
    """An unknowable concurrency state must skip the scan, never proceed."""
    p = _intraday_pipeline(universe=["AAPL"])
    p.db.get_trades.side_effect = RuntimeError("db locked")

    ctx = RunContext.start("intra_check")
    assert p._another_session_recently_active(ctx.run_id) is True
    assert p._run_intraday_opportunity_scan(ctx) == {
        "status": "intraday_scan_lock_contended", "run_id": ctx.run_id,
    }


def test_own_run_id_rows_do_not_trip_the_concurrency_guard():
    """The scan's own writes (e.g. a HOLD recorded earlier this same tick)
    must not make it think a different session is running."""
    from datetime import datetime, timezone

    p = _intraday_pipeline(universe=["AAPL"])
    ctx = RunContext.start("intra_check")
    p.db.get_trades.return_value = [{
        "symbol": "AAPL", "action": "HOLD", "run_id": ctx.run_id,
        "timestamp": datetime.now(timezone.utc).isoformat(), "reasoning": "self",
    }]

    assert p._another_session_recently_active(ctx.run_id) is False


@patch("src.pipeline.compute_indicators")
def test_process_lock_excludes_a_second_concurrent_scan(mock_compute_indicators, tmp_path):
    """Independent-review finding: the DB-row concurrency guard can only see
    a rival session AFTER it has written a trade row, so two intra_check
    processes starting at nearly the same instant could both slip through
    and size against the same pre-fill snapshot.

    `scripts/run_if_et_window.sh` makes that impossible in practice (1800s
    ticks vs a ~1230s hard kill), but that lives in a deployment config
    this code cannot read. The advisory flock closes the class outright:
    while one scan holds it, a second pipeline sharing the same db_path
    must decline rather than scan.
    """
    mock_compute_indicators.return_value = MagicMock()
    db_path = tmp_path / "shared.db"

    holder = _intraday_pipeline(universe=["AAPL"], db_path=db_path)
    rival = _intraday_pipeline(universe=["AAPL"], db_path=db_path)
    rival.broker.get_intraday_snapshots.return_value = {
        "AAPL": _snapshot(last=110.0, prev=100.0),
    }

    ctx = RunContext.start("intra_check")
    with holder._intraday_scan_process_lock() as acquired:
        assert acquired is True, "first holder must acquire the lock"
        # A second process on the same db_path must decline...
        with rival._intraday_scan_process_lock() as second:
            assert second is False
        # ...and the full scan entrypoint must therefore do nothing at all.
        assert rival._run_intraday_opportunity_scan(ctx) == {
            "status": "intraday_scan_lock_contended", "run_id": ctx.run_id,
        }
        rival.broker.get_intraday_snapshots.assert_not_called()
        rival.tech_analyst.analyze_batch.assert_not_called()


def test_process_lock_is_released_for_the_next_tick(tmp_path):
    """The lock must not wedge: once a scan finishes, the next 30-min tick
    (a fresh process on the same db_path) must be able to acquire it."""
    db_path = tmp_path / "shared.db"
    first = _intraday_pipeline(universe=["AAPL"], db_path=db_path)
    second = _intraday_pipeline(universe=["AAPL"], db_path=db_path)

    with first._intraday_scan_process_lock() as acquired:
        assert acquired is True
    # First released on context exit; the next tick gets it.
    with second._intraday_scan_process_lock() as acquired_next:
        assert acquired_next is True


def test_process_lock_propagates_scan_exception_and_releases(tmp_path):
    """The lock manager owns acquisition only; it must never swallow or
    replace an exception raised by the protected scan body."""
    db_path = tmp_path / "shared.db"
    first = _intraday_pipeline(universe=["AAPL"], db_path=db_path)
    second = _intraday_pipeline(universe=["AAPL"], db_path=db_path)
    suspended = PaidAnalysisSuspended("prelatched", {"suspended": True})

    with pytest.raises(PaidAnalysisSuspended) as caught:
        with first._intraday_scan_process_lock() as acquired:
            assert acquired is True
            raise suspended

    assert caught.value is suspended
    with second._intraday_scan_process_lock() as acquired_next:
        assert acquired_next is True


@patch("src.pipeline.compute_indicators")
def test_prelatched_real_intraday_scan_reports_suspension_before_agent_call(
    mock_compute_indicators, tmp_path,
):
    """Regression for the 2026-08-25 production tick: a suspension thrown
    inside the real process-lock wrapper must reach run_intra_check's specific
    handler and remain visible in the structured result."""
    mock_compute_indicators.return_value = MagicMock()
    p = _intraday_pipeline(universe=["AAPL"], db_path=tmp_path / "shared.db")
    p._is_trading_day = MagicMock(return_value=True)
    p._drain_pending_protection_restores = MagicMock()
    p._reconcile_stop_coverage = MagicMock(return_value=[])
    p._reconcile_orphan_pending_submits = MagicMock()
    p._another_session_recently_active = MagicMock(return_value=False)
    p.risk_engine = MagicMock()
    p.risk_engine.check_daily_loss.return_value = None
    p.broker.get_account.return_value = {
        "cash": 10_000.0,
        "portfolio_value": 10_100.0,
        "last_equity": 10_000.0,
        "non_marginable_buying_power": 10_000.0,
    }
    p.broker.get_positions.return_value = []
    p.broker.get_intraday_snapshots.return_value = {
        "AAPL": _snapshot(last=110.0, prev=100.0),
    }
    p.cost_circuit = MagicMock()
    p.cost_circuit.activate_session.return_value = {"suspended": True}
    p.cost_circuit.require_paid_analysis.side_effect = PaidAnalysisSuspended(
        "prelatched", {"suspended": True},
    )

    result = p.run_intra_check()

    assert result["status"] == "ok"
    assert result["intraday_scan"]["status"] == "paid_analysis_suspended"
    assert result["intraday_scan"]["suspended"] == "intraday opportunity discovery only"
    p.tech_analyst.analyze_batch.assert_not_called()
    p.cost_circuit.require_paid_analysis.assert_called_once_with(
        "intraday_tech_analyst",
    )


# ---------- Blocker 2: Tech receives truthful CURRENT-SESSION evidence ----------
#
# The scan detects candidates on live current-session prices. Before this
# fix Tech was then handed only COMPLETED daily bars ending at yesterday's
# close — so the very move that triggered the scan was invisible to the
# analyst asked to judge it. Tech now also receives an explicitly-labelled
# INCOMPLETE current-session block. An incomplete day is never presented
# as a finished daily bar.

def _session_snapshot(last, prev, o=None, h=None, lo=None, v=None):
    return {
        "last_price": last, "prev_close": prev,
        "session_open": o, "session_high": h,
        "session_low": lo, "session_volume": v,
    }


@patch("src.pipeline.compute_indicators")
def test_todays_move_is_passed_to_tech_as_current_session_context(mock_compute_indicators):
    """The live figures the scan triggered on must reach tech_analyst."""
    mock_compute_indicators.return_value = MagicMock()
    p = _intraday_pipeline(universe=["AAPL"])
    snap = _session_snapshot(last=110.0, prev=100.0, o=101.0, h=111.0,
                             lo=100.5, v=9_100_000)
    p.broker.get_intraday_snapshots.return_value = {"AAPL": snap}
    p.tech_analyst.analyze_batch.return_value = ({}, None)

    p._run_intraday_opportunity_scan(RunContext.start("intra_check"))

    ctx_arg = p.tech_analyst.analyze_batch.call_args.kwargs["intraday_context"]
    assert ctx_arg["AAPL"]["last_price"] == 110.0
    assert ctx_arg["AAPL"]["prev_close"] == 100.0
    assert ctx_arg["AAPL"]["session_volume"] == 9_100_000


def test_tech_prompt_renders_todays_move_without_faking_a_daily_bar():
    """The rendered prompt must show today's price and % move, label the
    session INCOMPLETE, and keep it out of the completed-bar series."""
    from datetime import date
    from src.agents.tech_analyst import TechAnalystAgent
    from src.models import OHLCV, TechnicalIndicators

    bars = [OHLCV(date=date(2026, 8, 18), open=99.0, high=101.0, low=98.0,
                  close=100.0, volume=5_000_000)]
    indicators = TechnicalIndicators(
        symbol="AAPL", ma_20=99.0, ma_50=98.0, ma_200=95.0, rsi_14=55.0,
        macd=0.5, macd_signal=0.4, macd_hist=0.1, bb_upper=104.0,
        bb_middle=100.0, bb_lower=96.0, atr_14=2.0, volume_change_pct=5.0,
    )
    with patch("anthropic.Anthropic"):
        agent = TechAnalystAgent(api_key="t", model="claude-sonnet-4-6-20250514")
        msg = agent.build_user_message(
            symbols_data=[{"symbol": "AAPL", "bars": bars, "indicators": indicators}],
            intraday_context={"AAPL": _session_snapshot(
                last=110.0, prev=100.0, o=101.0, h=111.0, lo=100.5, v=9_100_000,
            )},
        )

    assert "CURRENT SESSION" in msg and "INCOMPLETE" in msg
    assert "110.00" in msg                    # today's live price
    assert "+10.00%" in msg                   # move vs prior close
    # 100.0 now renders as "100" — see _px in src/agents/tech_analyst.py.
    # Same number, fewer tokens; assert the value, not its spelling.
    assert "Last completed close: 100\n" in msg or "Last completed close: 100" in msg
    # The completed-bar series must still contain ONLY the finished day.
    completed = msg.split("CURRENT SESSION")[0]
    assert "2026-08-18" in completed
    assert "110.0" not in completed, "today's live price must not appear as a daily bar"
    # And Tech must be told the indicators predate the move.
    assert "do NOT yet reflect this move" in msg


def test_tech_prompt_omits_session_block_when_no_intraday_context():
    """Morning runs pass no intraday context — the prompt must be unchanged
    for them (no empty/misleading session block)."""
    from datetime import date
    from src.agents.tech_analyst import TechAnalystAgent
    from src.models import OHLCV, TechnicalIndicators

    bars = [OHLCV(date=date(2026, 8, 18), open=99.0, high=101.0, low=98.0,
                  close=100.0, volume=5_000_000)]
    indicators = TechnicalIndicators(
        symbol="AAPL", ma_20=99.0, ma_50=98.0, ma_200=95.0, rsi_14=55.0,
        macd=0.5, macd_signal=0.4, macd_hist=0.1, bb_upper=104.0,
        bb_middle=100.0, bb_lower=96.0, atr_14=2.0, volume_change_pct=5.0,
    )
    with patch("anthropic.Anthropic"):
        agent = TechAnalystAgent(api_key="t", model="claude-sonnet-4-6-20250514")
        msg = agent.build_user_message(
            symbols_data=[{"symbol": "AAPL", "bars": bars, "indicators": indicators}],
        )
    assert "CURRENT SESSION" not in msg


@patch("src.pipeline.compute_indicators")
def test_todays_move_propagates_through_the_full_decision_chain(mock_compute_indicators):
    """End-to-end for Blocker 2: a move that develops TODAY is detected on
    live prices, handed to Tech WITH that live evidence, and flows through
    Tech -> PM -> Risk -> deterministic gate -> execution."""
    mock_compute_indicators.return_value = MagicMock()
    p = _intraday_pipeline(universe=["AAPL"])
    p.broker.get_intraday_snapshots.return_value = {
        "AAPL": _session_snapshot(last=110.0, prev=100.0, o=101.0,
                                  h=111.0, lo=100.5, v=9_100_000),
    }
    analysis = _ta_result("AAPL", rating="buy")
    p.tech_analyst.analyze_batch.return_value = (
        {"AAPL": analysis},
        MagicMock(user_message="m", raw_text="{}", tokens_used=1,
                  input_tokens=1, output_tokens=1, cost_usd=0.0, model="t"),
    )
    p.decision_stage.run.side_effect = lambda ctx: setattr(
        ctx, "portfolio_decision",
        SimpleNamespace(decisions=[SimpleNamespace(action="BUY", symbol="AAPL")]),
    )
    p.risk_stage.run.return_value = None
    p.execution_stage.run.return_value = [{"id": "o1", "action": "BUY", "symbol": "AAPL"}]

    ctx = RunContext.start("intra_check")
    result = p._run_intraday_opportunity_scan(ctx)

    # Tech saw today's live move...
    assert p.tech_analyst.analyze_batch.call_args.kwargs[
        "intraday_context"]["AAPL"]["last_price"] == 110.0
    # ...and it reached execution through the shared chain.
    assert ctx.analyses == [analysis]
    p.decision_stage.run.assert_called_once_with(ctx)
    p.risk_stage.run.assert_called_once_with(ctx)
    p.execution_stage.run.assert_called_once_with(ctx)
    assert result["status"] == "intraday_executed"
    lifecycle = [
        call.kwargs for call in p.db.insert_specialist_evidence.call_args_list
        if call.kwargs.get("kind") == "pipeline_event"
    ]
    assert any('"stage": "opportunity"' in row["evidence_json"] for row in lifecycle)
    assert any('"stage": "specialist"' in row["evidence_json"] for row in lifecycle)
