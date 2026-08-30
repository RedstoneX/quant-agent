"""Operator-honesty fix (2026-08-30): a crashed intraday opportunity scan
must be visible, not silently indistinguishable from a healthy tick.

Before this fix, `run_intra_check`'s `except Exception` handler around
`_run_intraday_opportunity_scan` set `scan_result = None` on a crash. Since
`result["intraday_scan"]` is only attached `if scan_result is not None`, a
crash left NO key in the result at all — byte-identical to a scan that ran
and correctly found nothing, and to a scan that never ran because it was
disabled, process-locked, or another session was mid-flight. The top-level
session status stayed "ok" either way, so `src/trader_feed.py` and
`src/notifier.py` (both of which key off `result["intraday_scan"]`) reported
the session as fine even when the scan had crashed. The rehearsal harness
(`ops/rehearsal/report.py`, added in PR #156) inherited the same blind spot
and could only flag it, not resolve it.

The fix: on a crash, `scan_result` is now an explicit dict — mirroring the
`except PaidAnalysisSuspended` branch immediately above it — with status
"intraday_scan_crashed", so it IS attached to the result and flows through
the exact same nested-status path `paid_analysis_suspended` and
`intraday_analysis_error` already use. Nothing about *whether* a crash can
fail the tick changes: the deterministic loss-protection check above the
scan already ran and is untouched; only what gets *reported* changes.

This suite asserts the three cases are now distinguishable:
  - the scan ran and proposed nothing (healthy, e.g. "intraday_no_trades")
  - the scan never ran at all (healthy — config disabled, process lock
    held, or another session recently active — no `intraday_scan` key)
  - the scan ran and crashed (unhealthy — "intraday_scan_crashed")
and that the non-negotiable guarantee survives: a crash never fails the
tick, and the deterministic loss check runs regardless.
"""
from __future__ import annotations

import fcntl
import os
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from src import trader_feed
from src.config import IntradayScanConfig
from src.pipeline import TradingPipeline
from tests.test_intraday_scan import _ta_result
from tests.test_trader_feed import _make_db


def _pipeline(*, enabled=True, universe=("AAPL",), move_threshold_pct=3.0,
              other_session_rows=None):
    """A TradingPipeline wired for a real, end-to-end `run_intra_check()`
    call — the intra_check preamble (account/position read, reconcilers,
    risk-engine loss check) AND the intraday scan's own dependencies
    (broker snapshots, tech analyst, decision/risk/execution stages) are
    mocked, but the scan's real wrapper/body/guard logic runs unmocked, so
    the early-return paths under test are the genuine production code
    paths, not a stubbed return value.
    """
    p = TradingPipeline.__new__(TradingPipeline)
    p.config = SimpleNamespace(
        trading=SimpleNamespace(universe=list(universe), lookback_days=100),
        storage=SimpleNamespace(
            db_path=str(Path(tempfile.mkdtemp()) / "t.db"),
        ),
        intraday_scan=IntradayScanConfig(
            enabled=enabled, move_threshold_pct=move_threshold_pct,
            cooldown_hours=3.0, max_candidates_per_scan=5,
        ),
    )
    p.broker = MagicMock()
    p.broker.get_account.return_value = {
        "cash": 10_000.0, "portfolio_value": 10_100.0, "last_equity": 10_000.0,
        "non_marginable_buying_power": 10_000.0,
    }
    p.broker.get_positions.return_value = []
    p.db = MagicMock()
    p.db.get_trades.return_value = other_session_rows or []
    p.market = MagicMock()
    p.market.get_ohlcv.return_value = [MagicMock()]
    p.macro_store = MagicMock()
    p.macro_store.load_last_state.return_value = {}
    p.news_store = MagicMock()
    p.news_store.load_daily_report.return_value = None
    p.tech_store = MagicMock()
    p.tech_store.load.return_value = {}
    p.tech_store.compute_ages.return_value = {}
    p.tech_analyst = MagicMock()
    p.decision_stage = MagicMock()
    p.risk_stage = MagicMock()
    p.execution_stage = MagicMock()

    p._activate_cost_session = MagicMock()
    p._drain_pending_protection_restores = MagicMock()
    p._drain_pending_repegs = MagicMock()
    p._reconcile_stop_coverage = MagicMock(return_value=[])
    p._reconcile_orphan_pending_submits = MagicMock()
    p._reconcile_stop_out_fills = MagicMock()
    p._is_trading_day = MagicMock(return_value=True)
    p.risk_engine = MagicMock()
    p.risk_engine.check_daily_loss.return_value = None
    return p


def _rehearsal_collect(result: dict):
    """Run the real `ops/rehearsal/report.py` `collect()` against a minimal
    on-disk sqlite DB, matching the schema/pattern established in
    tests/test_rehearsal_report_verdict.py's own `collect()` integration
    tests."""
    from ops.rehearsal.report import collect

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
        db_path = tmp.name
    try:
        conn = sqlite3.connect(db_path)
        conn.executescript(
            """
            CREATE TABLE agent_logs (
                id INTEGER PRIMARY KEY, run_id TEXT, agent_name TEXT,
                timestamp TEXT, status TEXT, cost_usd REAL
            );
            CREATE TABLE specialist_evidence (
                id INTEGER PRIMARY KEY, run_id TEXT, agent_name TEXT,
                kind TEXT, symbol TEXT, evidence_json TEXT
            );
            CREATE TABLE trades (
                id INTEGER PRIMARY KEY, run_id TEXT, symbol TEXT,
                action TEXT, qty INTEGER
            );
            CREATE TABLE llm_circuit_events (
                id INTEGER PRIMARY KEY, run_id TEXT, trigger_code TEXT,
                detail TEXT, agent_name TEXT, event_type TEXT
            );
            """
        )
        conn.commit()
        conn.close()

        return collect(
            session="intra_check", rehearsed_date="2026-08-28",
            run_id=result.get("run_id", "test-run"), source_run_id=None,
            result=result, db_path=db_path, library=None, trading_stub=None,
            isolation_checks=[], unavailable=[], network_attempts=[],
            notes=[], fill_model="immediate", duration_s=0.1,
        )
    finally:
        try:
            os.unlink(db_path)
        except OSError:
            pass


# ---------------------------------------------------------------- crash


def test_scan_crash_attaches_error_status_and_tick_completes():
    """The crash must reach `run_intra_check`'s result as an explicit
    marker, mirroring the `paid_analysis_suspended` shape — not disappear
    into `scan_result = None`."""
    p = _pipeline(enabled=True)
    p.broker.get_intraday_snapshots.side_effect = RuntimeError(
        "snapshot feed unavailable"
    )

    result = p.run_intra_check()

    assert result["status"] == "ok"  # the tick itself did not fail
    assert "intraday_scan" in result
    nested = result["intraday_scan"]
    assert nested["status"] == "intraday_scan_crashed"
    assert nested["run_id"] == result["run_id"]
    assert nested["error_type"] == "RuntimeError"
    assert "snapshot feed unavailable" in nested["error"]
    assert nested["preserved"] == "intraday deterministic loss protection"


def test_scan_crash_does_not_dump_a_raw_traceback():
    """Enough of the exception to diagnose, not a full traceback pasted
    into what becomes an operator-facing message."""
    p = _pipeline(enabled=True)
    p.broker.get_intraday_snapshots.side_effect = ValueError("bad payload")

    result = p.run_intra_check()

    nested = result["intraday_scan"]
    assert nested["error"] == "bad payload"
    assert "Traceback" not in nested["error"]
    assert "line" not in nested["error"]  # no "File ..., line N" frame text


def test_scan_crash_reaches_operator_via_trader_feed(tmp_path, monkeypatch):
    """The crash marker must flow through the SAME nested
    `result["intraday_scan"]` path production already uses for
    `paid_analysis_suspended` / `intraday_analysis_error` — not a new
    notification mechanism."""
    _make_db(tmp_path, monkeypatch)
    outer = {
        "status": "ok", "run_id": "intra_check-crash1", "daily_pnl": 12.0,
        "daily_return_pct": 0.12, "positions": 1,
        "intraday_scan": {
            "status": "intraday_scan_crashed", "run_id": "intra_check-crash1",
            "error": "snapshot feed unavailable", "error_type": "RuntimeError",
            "preserved": "intraday deterministic loss protection",
        },
    }

    msg = trader_feed.format_session_result("intra_check", outer, 5.0)

    assert msg is not None  # not silenced like a normal "ok" tick
    assert "🔴" in msg
    assert "crashed" in msg.lower()
    assert "RuntimeError" in msg
    assert "snapshot feed unavailable" in msg


def test_deterministic_loss_protection_still_runs_when_scan_crashes():
    """The scan crashing must not skip or undo the reconciliation / daily
    loss check that runs before it in the same tick."""
    p = _pipeline(enabled=True)
    p.broker.get_intraday_snapshots.side_effect = RuntimeError("boom")

    result = p.run_intra_check()

    p._reconcile_stop_coverage.assert_called_once()
    p._reconcile_orphan_pending_submits.assert_called_once()
    p._reconcile_stop_out_fills.assert_called_once()
    p.risk_engine.check_daily_loss.assert_called_once()
    assert result["status"] == "ok"
    assert result["positions"] == 0
    assert result["intraday_scan"]["status"] == "intraday_scan_crashed"


# ------------------------------------------------------------ never ran


def test_scan_never_ran_because_disabled_stays_healthy_and_silent(tmp_path, monkeypatch):
    _make_db(tmp_path, monkeypatch)
    p = _pipeline(enabled=False)

    result = p.run_intra_check()

    assert result["status"] == "ok"
    assert "intraday_scan" not in result
    assert trader_feed.format_session_result("intra_check", result, 5.0) is None


def test_scan_never_ran_because_process_lock_held_stays_healthy_and_silent(
    tmp_path, monkeypatch,
):
    _make_db(tmp_path, monkeypatch)
    p = _pipeline(enabled=True)
    lock_path = Path(p.config.storage.db_path).parent / ".intraday_scan.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    holder = open(lock_path, "w")
    fcntl.flock(holder.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        result = p.run_intra_check()
    finally:
        holder.close()

    assert result["status"] == "ok"
    assert "intraday_scan" not in result
    assert trader_feed.format_session_result("intra_check", result, 5.0) is None


def test_scan_never_ran_because_another_session_active_stays_healthy_and_silent(
    tmp_path, monkeypatch,
):
    _make_db(tmp_path, monkeypatch)
    p = _pipeline(
        enabled=True,
        other_session_rows=[{
            "symbol": "MSFT", "action": "BUY", "run_id": "run-morning1",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "reasoning": "morning run mid-flight",
        }],
    )

    result = p.run_intra_check()

    assert result["status"] == "ok"
    assert "intraday_scan" not in result
    assert trader_feed.format_session_result("intra_check", result, 5.0) is None


# ------------------------------------------------------- ran, nothing found


@patch("src.pipeline.compute_indicators")
def test_normal_scan_with_no_opportunities_stays_healthy(
    mock_compute_indicators, tmp_path, monkeypatch,
):
    """The scan genuinely runs — a candidate clears the move threshold and
    gets a real tech_analyst call — but the portfolio manager proposes no
    trades. This is healthy, and distinct from both the crash case (an
    explicit error status) and the never-ran case (no key at all): it
    attaches a real "intraday_no_trades" marker."""
    _make_db(tmp_path, monkeypatch)
    mock_compute_indicators.return_value = MagicMock()
    p = _pipeline(enabled=True, universe=("AAPL",))
    p.broker.get_intraday_snapshots.return_value = {
        "AAPL": {"last_price": 110.0, "prev_close": 100.0},  # 10% move
    }
    analysis = _ta_result("AAPL", rating="neutral")
    p.tech_analyst.analyze_batch.return_value = (
        {"AAPL": analysis},
        MagicMock(user_message="m", raw_text="{}", tokens_used=1,
                  input_tokens=1, output_tokens=1, cost_usd=0.0, model="t"),
    )
    p.decision_stage.run.side_effect = lambda ctx: setattr(
        ctx, "portfolio_decision", SimpleNamespace(decisions=[]),
    )

    result = p.run_intra_check()

    assert result["status"] == "ok"
    nested = result["intraday_scan"]
    assert nested["status"] == "intraday_no_trades"
    msg = trader_feed.format_session_result("intra_check", result, 5.0)
    assert msg is not None  # the scan DID engage a real candidate this tick
    assert "🔴" not in msg  # but it is not an error/alert banner


# --------------------------------------------------------- rehearsal rig


def test_rig_reports_crash_as_fail():
    from ops.rehearsal.report import _verdict

    result = {
        "status": "ok", "run_id": "r1",
        "intraday_scan": {
            "status": "intraday_scan_crashed", "run_id": "r1",
            "error": "boom", "error_type": "RuntimeError",
            "preserved": "intraday deterministic loss protection",
        },
    }

    report = _rehearsal_collect(result)

    assert report.status == "intraday_scan_crashed"
    assert _verdict(report) == "FAIL"
    assert report.verdict == "FAIL"
    assert report.error == "boom"


def test_rig_reports_never_ran_tick_as_pass():
    from ops.rehearsal.report import _verdict

    result = {"status": "ok", "run_id": "r2"}  # no intraday_scan key at all

    report = _rehearsal_collect(result)

    assert report.status == "ok"
    assert _verdict(report) == "PASS"
    assert report.verdict == "PASS"


def test_rig_reports_no_opportunities_tick_as_pass():
    from ops.rehearsal.report import _verdict

    result = {
        "status": "ok", "run_id": "r3",
        "intraday_scan": {
            "status": "intraday_no_trades", "run_id": "r3",
            "candidates": ["AAPL"],
        },
    }

    report = _rehearsal_collect(result)

    assert report.status == "intraday_no_trades"
    assert _verdict(report) == "PASS"
    assert report.verdict == "PASS"


def test_crashed_status_is_in_the_rigs_vocabulary():
    """The new status must have a plain-English explanation, like every
    other status this rig recognizes — not the generic 'ended with status
    X' fallback."""
    from ops.rehearsal.report import STATUS_PLAIN

    assert "intraday_scan_crashed" in STATUS_PLAIN
    assert STATUS_PLAIN["intraday_scan_crashed"]


def test_crashed_status_is_not_in_the_verdicts_healthy_set():
    """Regression guard for the recorded incident where the rig's healthy
    set once disagreed with production's and flipped normal sessions to
    FAIL: this new status must be the opposite mistake's guard — it must
    NOT be added to the healthy set, ever, since it is a genuine failure."""
    import inspect

    from ops.rehearsal.report import _verdict

    src = inspect.getsource(_verdict)
    # A crude but effective guard: the literal status string must not
    # appear anywhere inside _verdict's healthy-set construction.
    healthy_line_start = src.index("healthy = {")
    healthy_block = src[healthy_line_start: src.index("}", healthy_line_start)]
    assert "intraday_scan_crashed" not in healthy_block
