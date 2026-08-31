import json
import sqlite3

from src import trader_feed


def _make_db(tmp_path, monkeypatch):
    db = tmp_path / "quant_agent.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE specialist_evidence (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL,
            decision_id TEXT,
            agent_name TEXT NOT NULL,
            kind TEXT NOT NULL,
            scope TEXT NOT NULL,
            symbol TEXT,
            evidence_json TEXT NOT NULL,
            timestamp TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL,
            action TEXT NOT NULL,
            qty REAL NOT NULL,
            price REAL NOT NULL,
            reasoning TEXT,
            run_id TEXT,
            broker_order_id TEXT,
            fill_status TEXT,
            fill_qty REAL,
            fill_price REAL,
            timestamp TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE positions (
            symbol TEXT PRIMARY KEY,
            qty REAL NOT NULL,
            avg_entry REAL NOT NULL,
            current_price REAL NOT NULL,
            market_value REAL NOT NULL,
            unrealized_pnl REAL NOT NULL
        );
        CREATE TABLE agent_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            agent_name TEXT NOT NULL,
            run_id TEXT NOT NULL,
            output_summary TEXT,
            cost_usd REAL
        );
        """
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr(trader_feed, "_DB_PATH", db)
    return db


def _evidence(db, run_id, agent, kind, data, symbol=None):
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO specialist_evidence "
        "(run_id, agent_name, kind, scope, symbol, evidence_json) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (
            run_id,
            agent,
            kind,
            "symbol" if symbol else "run",
            symbol,
            json.dumps(data),
        ),
    )
    conn.commit()
    conn.close()


def _trade(db, run_id, symbol, action, qty=1, price=100, status="filled"):
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO trades "
        "(symbol, action, qty, price, reasoning, run_id, fill_status, fill_qty, fill_price) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (symbol, action, qty, price, "test", run_id, status, qty, price),
    )
    conn.commit()
    conn.close()


def _agent_log(db, run_id, agent, summary, cost=0.001):
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO agent_logs(agent_name, run_id, output_summary, cost_usd) "
        "VALUES (?, ?, ?, ?)",
        (agent, run_id, summary, cost),
    )
    conn.commit()
    conn.close()


def test_morning_feed_surfaces_market_signal_pm_risk_cash_and_execution(tmp_path, monkeypatch):
    db = _make_db(tmp_path, monkeypatch)
    run = "run-rich"
    _evidence(
        db, run, "macro_analyst", "analysis",
        {
            "regime": "risk-off", "equity_outlook": "bearish", "confidence": "high",
            "position_guidance": {"target_invested_pct": 40},
        },
    )
    _evidence(
        db, run, "tech_analyst", "analysis",
        {
            "symbol": "SQQQ", "rating": "strong_buy", "conviction": "high",
            "risk_reward": 2.4, "reasoning": "NASDAQ downside acceleration",
        },
        symbol="SQQQ",
    )
    _evidence(
        db, run, "portfolio_manager", "reasoning",
        {"portfolio_view": "Bearish tape; express downside selectively."},
    )
    _evidence(
        db, run, "portfolio_manager", "proposed_order",
        {"action": "BUY", "symbol": "SQQQ", "allocation_pct": 8,
         "reasoning": "Defined-risk bearish expression"},
        symbol="SQQQ",
    )
    _evidence(
        db, run, "portfolio_manager", "proposed_order",
        {"action": "HOLD", "symbol": "NVDA", "allocation_pct": 0,
         "reasoning": "No clean entry after the move"},
        symbol="NVDA",
    )
    _evidence(
        db, run, "risk_manager", "verdict",
        {"approved": True, "reason_category": "clean", "scale_all_buys": 1.0,
         "reasoning": "Sizing acceptable."},
    )
    _trade(db, run, "SGOV", "SWEEP_SELL", qty=3, price=100.5)
    _trade(db, run, "SQQQ", "BUY", qty=4, price=25.02)
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO positions VALUES ('SGOV', 90, 100, 100.5, 9045, 5)")
    conn.commit()
    conn.close()
    _agent_log(db, run, "portfolio_manager", "Bearish tape", 0.006)
    _agent_log(db, run, "risk_manager", "Approved: True", 0.006)

    msg = trader_feed.format_session_result(
        "morning",
        {"status": "executed", "run_id": run, "orders": [{"symbol": "SQQQ"}],
         "data_status": {"macro": "ok", "tech": "ok", "news": "ok", "earnings": "ok"}},
        61.0,
    )

    assert "📊 Market: risk-off / bearish / high" in msg
    assert "SQQQ: STRONG_BUY/high" in msg
    assert "BUY SQQQ 8%" in msg
    assert "PASS NVDA" in msg
    assert "🛡️ Risk: APPROVED" in msg
    assert "T-bill cash release" in msg
    assert "BUY SQQQ" in msg and "filled" in msg
    assert "LLM $0.01/2 provider requests" in msg


def test_morning_hold_explains_no_trade_in_investment_terms(tmp_path, monkeypatch):
    db = _make_db(tmp_path, monkeypatch)
    run = "run-hold"
    _evidence(db, run, "portfolio_manager", "reasoning", {"portfolio_view": "No setup clears the bar."})
    _evidence(
        db, run, "portfolio_manager", "proposed_order",
        {"action": "HOLD", "symbol": "AAPL", "allocation_pct": 0,
         "reasoning": "R/R insufficient after the opening move"},
        symbol="AAPL",
    )
    _evidence(
        db, run, "risk_manager", "verdict",
        {"approved": True, "reason_category": "clean", "scale_all_buys": 1.0,
         "reasoning": "No risk objection."},
    )

    msg = trader_feed.format_session_result(
        "morning", {"status": "executed", "run_id": run, "orders": []}, 30.0,
    )
    assert "PASS AAPL" in msg
    assert "NO TRADE — PM/constructor produced HOLD only" in msg
    assert "orders: 0" not in msg


def test_morning_pm_no_change_uses_agent_summary_when_structured_evidence_absent(tmp_path, monkeypatch):
    db = _make_db(tmp_path, monkeypatch)
    run = "run-pm-none"
    _agent_log(db, run, "portfolio_manager", "no trades")
    msg = trader_feed.format_session_result(
        "morning", {"status": "executed", "run_id": run, "orders": []}, 20.0,
    )
    assert "PM produced no executable portfolio change" in msg
    assert "detailed PM evidence unavailable" not in msg


def test_risk_veto_is_distinguished_from_pm_no_trade(tmp_path, monkeypatch):
    db = _make_db(tmp_path, monkeypatch)
    run = "run-veto"
    _evidence(db, run, "portfolio_manager", "reasoning", {"portfolio_view": "One candidate qualifies."})
    _evidence(
        db, run, "portfolio_manager", "proposed_order",
        {"action": "BUY", "symbol": "NVDA", "allocation_pct": 10,
         "reasoning": "Setup qualifies"},
        symbol="NVDA",
    )
    _evidence(
        db, run, "risk_manager", "verdict",
        {"approved": False, "reason_category": "event_risk", "scale_all_buys": 1.0,
         "reasoning": "Earnings event risk is too close."},
    )
    msg = trader_feed.format_session_result(
        "morning",
        {"status": "rejected", "run_id": run, "orders": [],
         "reason": "Earnings event risk is too close."},
        30.0,
    )
    assert "Risk: REJECTED" in msg
    assert "NO TRADE — Risk vetoed the plan" in msg


def test_execution_skip_is_not_misreported_as_investment_hold(tmp_path, monkeypatch):
    db = _make_db(tmp_path, monkeypatch)
    run = "run-unfunded"
    _evidence(
        db, run, "portfolio_manager", "proposed_order",
        {"action": "BUY", "symbol": "AMD", "allocation_pct": 8, "reasoning": "Qualified setup"},
        symbol="AMD",
    )
    _evidence(
        db, run, "risk_manager", "verdict",
        {"approved": True, "reason_category": "clean", "scale_all_buys": 1.0,
         "reasoning": "Approved."},
    )
    msg = trader_feed.format_session_result(
        "morning",
        {"status": "buys_unfunded", "run_id": run, "orders": [],
         "execution_skips": [{"symbol": "AMD", "reason": "insufficient_cash",
                               "detail": "funding sale not confirmed"}]},
        50.0,
    )
    assert "Execution gate: 1 skip" in msg
    assert "insufficient_cash" in msg
    assert "decision(s) survived review but execution could not complete" in msg


def test_intraday_scan_result_is_not_hidden_behind_outer_ok(tmp_path, monkeypatch):
    db = _make_db(tmp_path, monkeypatch)
    run = "intra_check-demo"
    _evidence(
        db, run, "tech_analyst", "analysis",
        {"symbol": "SDS", "rating": "buy", "conviction": "medium", "risk_reward": 1.8,
         "reasoning": "Broad downside move triggered scan"},
        symbol="SDS",
    )
    _evidence(db, run, "portfolio_manager", "reasoning", {"portfolio_view": "Review hedge; do not chase."})
    _evidence(
        db, run, "portfolio_manager", "proposed_order",
        {"action": "HOLD", "symbol": "SDS", "allocation_pct": 0,
         "reasoning": "Move too extended"},
        symbol="SDS",
    )
    _evidence(
        db, run, "risk_manager", "verdict",
        {"approved": True, "reason_category": "clean", "scale_all_buys": 1.0,
         "reasoning": "No action to veto."},
    )
    outer = {
        "status": "ok", "run_id": run, "daily_pnl": -42.0, "daily_return_pct": -0.42,
        "positions": 0,
        "intraday_scan": {"status": "intraday_no_trades", "run_id": run,
                          "candidates": ["SDS"], "orders": []},
    }
    msg = trader_feed.format_session_result("intra_check", outer, 12.0)
    assert "⚡ INTRADAY OPPORTUNITY" in msg
    assert "SDS: BUY/medium" in msg
    assert "PASS SDS" in msg
    assert "NO TRADE" in msg


def test_intraday_no_new_activity_statuses_remain_silent(tmp_path, monkeypatch):
    """2026-08-31 visibility fix: disabled / lock-contended / no-opportunity
    now attach a real `intraday_scan` dict (previously no key at all) so the
    rehearsal rig and DB-backed evidence can tell them apart. The live
    Telegram feed must stay exactly as quiet about them as it was when they
    left no key — none of the three needs an operator's attention."""
    _make_db(tmp_path, monkeypatch)
    for status in (
        "intraday_scan_disabled", "intraday_scan_lock_contended",
        "intraday_scan_no_opportunity",
    ):
        outer = {
            "status": "ok", "run_id": "intra_check-quiet", "daily_pnl": 10.0,
            "intraday_scan": {"status": status, "run_id": "intra_check-quiet"},
        }
        msg = trader_feed.format_session_result("intra_check", outer, 4.0)
        assert msg is None, f"{status} must stay silent on the trader feed"


def test_normal_intraday_ok_tick_remains_silent(tmp_path, monkeypatch):
    _make_db(tmp_path, monkeypatch)
    msg = trader_feed.format_session_result(
        "intra_check",
        {"status": "ok", "run_id": "intra_check-quiet", "daily_pnl": 10.0},
        4.0,
    )
    assert msg is None


def test_midday_review_surfaces_actions_and_holds(tmp_path, monkeypatch):
    db = _make_db(tmp_path, monkeypatch)
    run = "midday-demo"
    _trade(db, run, "AAPL", "REDUCE", qty=2, price=210.0)
    review = {
        "risk_level": "moderate",
        "overall_assessment": "One oversized winner merits a trim; the rest remain intact.",
        "actions": [
            {"action": "REDUCE", "symbol": "AAPL", "reason": "Weight drifted too high"},
            {"action": "HOLD", "symbol": "NVDA", "reason": "Thesis and momentum remain intact"},
        ],
    }
    msg = trader_feed.format_session_result(
        "midday",
        {"status": "reviewed", "run_id": run, "positions": 2, "orders": [{}], "review": review},
        20.0,
    )
    assert "MIDDAY REVIEW" in msg
    assert "risk moderate" in msg
    assert "REDUCE AAPL" in msg
    assert "HOLD NVDA" in msg
    assert "Execution: 1 broker action" in msg


def test_midday_without_structured_review_is_not_mislabelled_as_pm_failure(tmp_path, monkeypatch):
    _make_db(tmp_path, monkeypatch)
    msg = trader_feed.format_session_result(
        "midday",
        {"status": "reviewed", "run_id": "midday-empty", "positions": 0,
         "orders": [], "review": None},
        8.0,
    )
    assert "MIDDAY REVIEW" in msg
    assert "NO ACTION — no market-risk positions required review" in msg
    assert "PM" not in msg


def test_emergency_position_review_has_explicit_circuit_breaker_banner(tmp_path, monkeypatch):
    db = _make_db(tmp_path, monkeypatch)
    run = "close-emergency"
    _trade(db, run, "AAPL", "EMERGENCY_SELL", qty=5, price=180.0)
    msg = trader_feed.format_session_result(
        "close",
        {"status": "emergency_sold", "run_id": run, "positions": 1,
         "orders": [{"symbol": "AAPL"}], "review": None},
        6.0,
    )
    assert "DAILY-LOSS CIRCUIT BREAKER" in msg
    assert "EMERGENCY_SELL AAPL" in msg


def test_early_close_uses_established_formatter_not_trader_review(tmp_path, monkeypatch):
    _make_db(tmp_path, monkeypatch)
    msg = trader_feed.format_session_result(
        "close",
        {"status": "early_close", "run_id": "close-early", "positions": 0, "orders": []},
        1.0,
    )
    assert "status: early_close" in msg
    assert "CLOSE REVIEW" not in msg


def test_morning_pm_rationale_survives_past_old_105_char_clip(tmp_path, monkeypatch):
    """This is the reported defect, reproduced through the real formatter:
    a BUY CRM alert whose PM rationale read "...strong heavy accumulation
    volume" and just stopped there. `_append_pm` used to clip per-symbol
    reasoning at 105 chars with a raw slice (no ellipsis, could — and did
    — land mid-word). A rationale well past the old limit must now render
    intact rather than being chopped mid-sentence."""
    db = _make_db(tmp_path, monkeypatch)
    run = "run-crm-rationale"
    long_reasoning = (
        "CRM is showing strong heavy accumulation volume over the past "
        "three sessions, with block prints clustering just above the "
        "20-day moving average and options open interest skewing "
        "meaningfully toward calls into next week's print."
    )
    assert len(long_reasoning) > 105
    _evidence(
        db, run, "portfolio_manager", "proposed_order",
        {"action": "BUY", "symbol": "CRM", "allocation_pct": 6,
         "reasoning": long_reasoning},
        symbol="CRM",
    )

    msg = trader_feed.format_session_result(
        "morning",
        {"status": "executed", "run_id": run, "orders": [{"symbol": "CRM"}]},
        20.0,
    )

    assert long_reasoning in msg


def test_morning_pm_rationale_past_new_limit_clips_on_word_boundary(tmp_path, monkeypatch):
    """Even past the new (much larger) 420-char ceiling, a clip must still
    land on a word boundary with a visible ellipsis — never a bare
    mid-word chop like the original defect."""
    db = _make_db(tmp_path, monkeypatch)
    run = "run-crm-overlong"
    very_long_reasoning = "accumulation volume confirms the breakout thesis. " * 15
    assert len(very_long_reasoning) > 420
    _evidence(
        db, run, "portfolio_manager", "proposed_order",
        {"action": "BUY", "symbol": "CRM", "allocation_pct": 6,
         "reasoning": very_long_reasoning},
        symbol="CRM",
    )

    msg = trader_feed.format_session_result(
        "morning",
        {"status": "executed", "run_id": run, "orders": [{"symbol": "CRM"}]},
        20.0,
    )

    assert very_long_reasoning not in msg  # it DID need clipping this time
    assert "…" in msg  # and says so
    # `_clip` collapses whitespace before clipping (see its docstring) —
    # compare against that collapsed form, not the raw repeated string.
    collapsed = " ".join(very_long_reasoning.split())
    line = next(line for line in msg.splitlines() if "BUY CRM" in line)
    reasoning_part = line.split(" — ", 1)[1]
    core = reasoning_part[: -len("…")].rstrip() if reasoning_part.endswith("…") else reasoning_part
    assert collapsed.startswith(core)
    # A prefix check ALONE is trivially true for any left-truncation,
    # including the old buggy hard character cut — it proves nothing about
    # boundary-awareness. The real test: the character immediately after
    # `core` in the source must be whitespace or end-of-string, never a
    # letter, which is what a mid-word chop would leave behind.
    tail = collapsed[len(core):len(core) + 1]
    assert tail in ("", " ")


def test_trader_feed_reads_database_without_mutating_it(tmp_path, monkeypatch):
    db = _make_db(tmp_path, monkeypatch)
    run = "run-ro"
    _evidence(db, run, "portfolio_manager", "reasoning", {"portfolio_view": "Nothing to do."})
    conn = sqlite3.connect(db)
    before = conn.execute("SELECT COUNT(*) FROM specialist_evidence").fetchone()[0]
    conn.close()

    trader_feed.format_session_result(
        "morning", {"status": "executed", "run_id": run, "orders": []}, 5.0,
    )

    conn = sqlite3.connect(db)
    after = conn.execute("SELECT COUNT(*) FROM specialist_evidence").fetchone()[0]
    conn.close()
    assert after == before
