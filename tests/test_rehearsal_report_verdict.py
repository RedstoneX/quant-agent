"""Regression: the rehearsal verdict must not call a healthy session a FAIL.

Additional regression: for intra_check sessions, the nested
result["intraday_scan"]["status"] must be extracted and used as the report
status, not the top-level "ok" status. This mirrors production's
src/trader_feed.py behavior and makes the nested statuses intraday_no_trades
and intraday_executed actually reachable in the rig's reporting.

Found running the rig against real production history (2026-08-29): plain,
uneventful `midday`, `close` and `intra_check` rehearsals — no crash, no
missing recording, no blocked agent, nothing stopped — all came back
`VERDICT: FAIL` because `report.py`'s status vocabulary had not kept up with
`src/pipeline.py`'s. `run_position_review` (shared by midday/close) returns
"reviewed" for both "no positions to review" and "reviewed them successfully"
— only "position_review_parse_error" is the real failure. `run_intra_check`
returns "ok" when there is no loss violation or no positions — a normal,
majority-of-the-time outcome for a session that runs every 30 minutes.
`run_evening` returns "analyzed" on success. Production's own
`src/trader_feed.py` (groups "reviewed", "intraday_no_trades", "no_trades",
"ok") and `src/notifier.py` (groups "executed", "analyzed", "reviewed",
"preprocessed", "reflected") already treat these as healthy completions, not
failures — the rig disagreeing with production about what counts as "the
session worked" is exactly the kind of dishonest output this harness exists
to avoid producing itself.
"""

from __future__ import annotations

from ops.rehearsal.report import RehearsalReport, STATUS_PLAIN, _verdict


def _report(status: str) -> RehearsalReport:
    return RehearsalReport(
        session="midday", rehearsed_date="2026-08-31", run_id="r",
        source_run_id=None, status=status, completed=True,
    )


def test_reviewed_with_no_positions_is_not_a_failure():
    """run_midday / run_close's normal outcome when there is nothing to
    review — not a crash, not a degraded result."""
    assert _verdict(_report("reviewed")) == "PASS"


def test_intra_check_ok_is_not_a_failure():
    """run_intra_check's normal outcome absent a loss-limit breach — the
    common case on a 30-minute cadence, not an error."""
    assert _verdict(_report("ok")) == "PASS"


def test_evening_analyzed_is_not_a_failure():
    assert _verdict(_report("analyzed")) == "PASS"


def test_intraday_no_trades_and_intraday_executed_are_not_failures():
    assert _verdict(_report("intraday_no_trades")) == "PASS"
    assert _verdict(_report("intraday_executed")) == "PASS"


def test_nested_intraday_no_trades_status_is_treated_as_healthy():
    """An intra_check report with the nested intraday_no_trades status
    should be treated as a healthy completion, matching production behavior.
    """
    # The status would be extracted by collect() from the nested result
    report = RehearsalReport(
        session="intra_check", rehearsed_date="2026-08-31", run_id="r",
        source_run_id=None, status="intraday_no_trades", completed=True,
    )
    assert _verdict(report) == "PASS"


def test_nested_intraday_executed_status_is_treated_as_healthy():
    """An intra_check report with the nested intraday_executed status
    should be treated as a healthy completion, matching production behavior.
    """
    # The status would be extracted by collect() from the nested result
    report = RehearsalReport(
        session="intra_check", rehearsed_date="2026-08-31", run_id="r",
        source_run_id=None, status="intraday_executed", completed=True,
    )
    assert _verdict(report) == "PASS"


def test_genuine_failure_statuses_are_still_fail():
    """The fix must not swallow real failures alongside the healthy ones."""
    for status in (
        "position_review_parse_error", "evening_analysis_error",
        "evening_parse_error", "broker_error", "pm_agent_failure",
    ):
        assert _verdict(_report(status)) == "FAIL", status


def test_every_newly_recognized_status_has_a_plain_english_entry():
    for status in ("reviewed", "ok", "analyzed", "intraday_no_trades", "intraday_executed"):
        assert status in STATUS_PLAIN
        assert STATUS_PLAIN[status]  # non-empty


def test_every_known_pipeline_terminal_status_is_classified():
    """Guard against vocabulary drift: every terminal status a session entry
    point in src/pipeline.py can return must be classified by report.py — in
    STATUS_PLAIN, whether as a healthy completion or a named failure — so a
    pipeline status this rig has never seen prints a real explanation
    instead of the generic "The session ended with status 'X'." fallback in
    RehearsalReport.render().

    Discovering these dynamically (walking src/pipeline.py's AST for every
    dict literal keyed "status") was tried and rejected: several real
    statuses are not string literals at their return site at all.
    run_morning returns `{"status": failure_status, ...}` where
    `failure_status = ctx.analysis_failure_status or "pm_agent_failure"`
    (src/pipeline.py:7322) — the four "pm_*" values it can hold are set on
    `AgentResult.semantic_status` three call frames away, inside
    `src/agents/portfolio_manager.py`'s `_semantic_failure`. A naive
    literal-string AST walk over src/pipeline.py alone would silently miss
    exactly the kind of drift this test exists to catch, which would defeat
    the point of having it. So: a hardcoded list below, built from a manual,
    line-by-line read of every `run_*` session function in src/pipeline.py
    on 2026-08-29 (see the file:line citations in this file's STATUS_PLAIN
    comments). MUST be updated by hand whenever pipeline.py adds, renames or
    removes a terminal status.
    """
    from ops.rehearsal import runner

    # If runner.SESSIONS ever grows beyond these five, the list below needs
    # a matching audit before this test can be trusted again.
    unexpected_sessions = set(runner.SESSIONS) - {
        "morning", "midday", "close", "evening", "intra_check",
    }
    assert not unexpected_sessions, (
        f"runner.SESSIONS grew {sorted(unexpected_sessions)} — audit its "
        "terminal statuses in src/pipeline.py and extend known_statuses "
        "in this test before trusting it again"
    )

    # Every terminal status actually reachable as `report.status` today
    # (the top-level "status" key of a SESSIONS-mapped run_* function's
    # return value, or the nested status from result["intraday_scan"]["status"]
    # for intra_check sessions — this is exactly what ops/rehearsal/report.py's
    # `collect()` reads), plus run_earnings_preprocess's (a real, scheduled,
    # LLM-calling session with the same shape as the five supported modes,
    # not yet wired into runner.SESSIONS — see the STATUS_PLAIN comment
    # above "fetch_error"). Deliberately excludes run_quarterly_meta_
    # reflection and run_daily (neither is a trading session: run_daily is
    # a CSV export with no LLM calls and isn't in SESSIONS either).
    known_statuses = {
        # run_morning (src/pipeline.py:7094-7441)
        "market_holiday", "broker_error", "emergency_sold",
        "paid_analysis_suspended", "no_data", "pm_agent_failure",
        "pm_parse_error", "pm_schema_error", "pm_grounding_error",
        "pm_repair_changed_decision", "no_trades", "symbol_block",
        "hard_risk_block", "agent_failure", "rejected", "buys_unfunded",
        "no_orders", "executed",
        # run_midday / run_close -> run_position_review (7757-8228)
        "early_close", "reviewed", "position_review_parse_error",
        # run_intra_check (8402-8594)
        "ok",
        # run_evening (9142-9641)
        "analyzed", "evening_analysis_error", "evening_parse_error",
        # run_earnings_preprocess (8229-8401) — not yet in runner.SESSIONS
        "fetch_error", "nothing_new", "analysis_error", "preprocessed",
    }
    missing = sorted(s for s in known_statuses if s not in STATUS_PLAIN)
    assert not missing, (
        f"{missing} would print as a raw, unexplained status to the "
        "account owner — add a STATUS_PLAIN entry (and, if it is a "
        "healthy completion, add it to _verdict's `healthy` set)"
    )


# Integration tests for nested intraday_scan status extraction
# ================================================================

def test_collect_extracts_nested_intraday_no_trades_status():
    """Test that collect() properly extracts the nested intraday_no_trades
    status from result["intraday_scan"]["status"] and uses it as the report
    status, making it reachable in the verdict logic.
    """
    import sqlite3
    import tempfile

    # Create a minimal test database
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
        db_path = tmp.name

    try:
        conn = sqlite3.connect(db_path)
        # Create minimal schema that collect() expects
        conn.execute("""
            CREATE TABLE agent_logs (
                id INTEGER PRIMARY KEY,
                run_id TEXT,
                agent_name TEXT,
                timestamp TEXT,
                status TEXT,
                cost_usd REAL
            )
        """)
        conn.execute("""
            CREATE TABLE specialist_evidence (
                id INTEGER PRIMARY KEY,
                run_id TEXT,
                agent_name TEXT,
                kind TEXT,
                symbol TEXT,
                evidence_json TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE trades (
                id INTEGER PRIMARY KEY,
                run_id TEXT,
                symbol TEXT,
                action TEXT,
                qty INTEGER
            )
        """)
        conn.execute("""
            CREATE TABLE llm_circuit_events (
                id INTEGER PRIMARY KEY,
                run_id TEXT,
                trigger_code TEXT,
                detail TEXT,
                agent_name TEXT,
                event_type TEXT
            )
        """)
        conn.commit()
        conn.close()

        # Now test collect() with nested status
        from ops.rehearsal.report import collect

        result = {
            "status": "ok",  # top-level status
            "intraday_scan": {
                "status": "intraday_no_trades",
                "candidates": ["AAPL"],
                "run_id": "test-run",
            },
        }
        report = collect(
            session="intra_check",
            rehearsed_date="2026-08-31",
            run_id="test-run",
            source_run_id=None,
            result=result,
            db_path=db_path,
            library=None,
            trading_stub=None,
            isolation_checks=[],
            unavailable=[],
            network_attempts=[],
            notes=[],
            fill_model="immediate",
            duration_s=0.1,
        )
        assert report.status == "intraday_no_trades"
        assert _verdict(report) == "PASS"
    finally:
        import os
        try:
            os.unlink(db_path)
        except Exception:
            pass


def test_collect_extracts_nested_intraday_executed_status():
    """Test that collect() extracts nested intraday_executed status and
    makes it reachable in the verdict logic.
    """
    import sqlite3
    import tempfile

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
        db_path = tmp.name

    try:
        conn = sqlite3.connect(db_path)
        # Create minimal schema
        conn.execute("""
            CREATE TABLE agent_logs (
                id INTEGER PRIMARY KEY,
                run_id TEXT,
                agent_name TEXT,
                timestamp TEXT,
                status TEXT,
                cost_usd REAL
            )
        """)
        conn.execute("""
            CREATE TABLE specialist_evidence (
                id INTEGER PRIMARY KEY,
                run_id TEXT,
                agent_name TEXT,
                kind TEXT,
                symbol TEXT,
                evidence_json TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE trades (
                id INTEGER PRIMARY KEY,
                run_id TEXT,
                symbol TEXT,
                action TEXT,
                qty INTEGER
            )
        """)
        conn.execute("""
            CREATE TABLE llm_circuit_events (
                id INTEGER PRIMARY KEY,
                run_id TEXT,
                trigger_code TEXT,
                detail TEXT,
                agent_name TEXT,
                event_type TEXT
            )
        """)
        conn.commit()
        conn.close()

        from ops.rehearsal.report import collect

        result = {
            "status": "ok",  # top-level status
            "intraday_scan": {
                "status": "intraday_executed",
                "candidates": ["AAPL"],
                "orders": [{"symbol": "AAPL", "qty": 10}],
                "run_id": "test-run",
            },
        }
        report = collect(
            session="intra_check",
            rehearsed_date="2026-08-31",
            run_id="test-run",
            source_run_id=None,
            result=result,
            db_path=db_path,
            library=None,
            trading_stub=None,
            isolation_checks=[],
            unavailable=[],
            network_attempts=[],
            notes=[],
            fill_model="immediate",
            duration_s=0.1,
        )
        assert report.status == "intraday_executed"
        assert _verdict(report) == "PASS"
    finally:
        import os
        try:
            os.unlink(db_path)
        except Exception:
            pass


def test_collect_ignores_nested_scan_for_non_intra_check_sessions():
    """Non-intra_check sessions should use the top-level status, even if
    an intraday_scan key exists (which would be unusual but possible).
    This ensures backwards compatibility.
    """
    import sqlite3
    import tempfile

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
        db_path = tmp.name

    try:
        conn = sqlite3.connect(db_path)
        # Create minimal schema
        conn.execute("""
            CREATE TABLE agent_logs (
                id INTEGER PRIMARY KEY,
                run_id TEXT,
                agent_name TEXT,
                timestamp TEXT,
                status TEXT,
                cost_usd REAL
            )
        """)
        conn.execute("""
            CREATE TABLE specialist_evidence (
                id INTEGER PRIMARY KEY,
                run_id TEXT,
                agent_name TEXT,
                kind TEXT,
                symbol TEXT,
                evidence_json TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE trades (
                id INTEGER PRIMARY KEY,
                run_id TEXT,
                symbol TEXT,
                action TEXT,
                qty INTEGER
            )
        """)
        conn.execute("""
            CREATE TABLE llm_circuit_events (
                id INTEGER PRIMARY KEY,
                run_id TEXT,
                trigger_code TEXT,
                detail TEXT,
                agent_name TEXT,
                event_type TEXT
            )
        """)
        conn.commit()
        conn.close()

        from ops.rehearsal.report import collect

        # A morning session with an intraday_scan key (should be ignored)
        result = {
            "status": "no_trades",
            "intraday_scan": {
                "status": "intraday_executed",  # This should be ignored
                "run_id": "test-run",
            },
        }
        report = collect(
            session="morning",
            rehearsed_date="2026-08-31",
            run_id="test-run",
            source_run_id=None,
            result=result,
            db_path=db_path,
            library=None,
            trading_stub=None,
            isolation_checks=[],
            unavailable=[],
            network_attempts=[],
            notes=[],
            fill_model="immediate",
            duration_s=0.1,
        )
        # Should use top-level status, not nested
        assert report.status == "no_trades"
        assert _verdict(report) == "PASS"
    finally:
        import os
        try:
            os.unlink(db_path)
        except Exception:
            pass


def test_collect_handles_malformed_nested_intraday_scan():
    """collect() should degrade gracefully when intraday_scan is malformed:
    None, not a dict, or missing the status key.
    """
    import sqlite3
    import tempfile

    def make_db():
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
            db_path = tmp.name
        conn = sqlite3.connect(db_path)
        # Create minimal schema
        conn.execute("""
            CREATE TABLE agent_logs (
                id INTEGER PRIMARY KEY,
                run_id TEXT,
                agent_name TEXT,
                timestamp TEXT,
                status TEXT,
                cost_usd REAL
            )
        """)
        conn.execute("""
            CREATE TABLE specialist_evidence (
                id INTEGER PRIMARY KEY,
                run_id TEXT,
                agent_name TEXT,
                kind TEXT,
                symbol TEXT,
                evidence_json TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE trades (
                id INTEGER PRIMARY KEY,
                run_id TEXT,
                symbol TEXT,
                action TEXT,
                qty INTEGER
            )
        """)
        conn.execute("""
            CREATE TABLE llm_circuit_events (
                id INTEGER PRIMARY KEY,
                run_id TEXT,
                trigger_code TEXT,
                detail TEXT,
                agent_name TEXT,
                event_type TEXT
            )
        """)
        conn.commit()
        conn.close()
        return db_path

    from ops.rehearsal.report import collect
    import os

    # Case 1: intraday_scan is None
    db_path1 = make_db()
    try:
        result1 = {
            "status": "ok",
            "intraday_scan": None,
        }
        report1 = collect(
            session="intra_check",
            rehearsed_date="2026-08-31",
            run_id="test-run",
            source_run_id=None,
            result=result1,
            db_path=db_path1,
            library=None,
            trading_stub=None,
            isolation_checks=[],
            unavailable=[],
            network_attempts=[],
            notes=[],
            fill_model="immediate",
            duration_s=0.1,
        )
        # Should fall back to top-level status
        assert report1.status == "ok"
        assert _verdict(report1) == "PASS"
    finally:
        try:
            os.unlink(db_path1)
        except Exception:
            pass

    # Case 2: intraday_scan is a dict but has no status key
    db_path2 = make_db()
    try:
        result2 = {
            "status": "ok",
            "intraday_scan": {
                "candidates": ["AAPL"],
                "run_id": "test-run",
            },
        }
        report2 = collect(
            session="intra_check",
            rehearsed_date="2026-08-31",
            run_id="test-run",
            source_run_id=None,
            result=result2,
            db_path=db_path2,
            library=None,
            trading_stub=None,
            isolation_checks=[],
            unavailable=[],
            network_attempts=[],
            notes=[],
            fill_model="immediate",
            duration_s=0.1,
        )
        # Should fall back to top-level status
        assert report2.status == "ok"
        assert _verdict(report2) == "PASS"
    finally:
        try:
            os.unlink(db_path2)
        except Exception:
            pass

    # Case 3: intraday_scan is not a dict
    db_path3 = make_db()
    try:
        result3 = {
            "status": "ok",
            "intraday_scan": "not a dict",
        }
        report3 = collect(
            session="intra_check",
            rehearsed_date="2026-08-31",
            run_id="test-run",
            source_run_id=None,
            result=result3,
            db_path=db_path3,
            library=None,
            trading_stub=None,
            isolation_checks=[],
            unavailable=[],
            network_attempts=[],
            notes=[],
            fill_model="immediate",
            duration_s=0.1,
        )
        # Should fall back to top-level status
        assert report3.status == "ok"
        assert _verdict(report3) == "PASS"
    finally:
        try:
            os.unlink(db_path3)
        except Exception:
            pass
