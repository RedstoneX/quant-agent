import json
import re
import sqlite3
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest

from src import trader_feed
from src.data.company import CompanyProfile, CompanyProfileStore
from src.notifier import TelegramNotifier

_ET = ZoneInfo("America/New_York")
# A deliberately NOT-top-of-hour, NOT-9:30 instant — `trader_feed._is_hourly_
# checkpoint` fires on intra_check's own first-tick-of-the-hour minute (or
# 9:30), and without pinning the clock a quiet-tick "no message" assertion
# is flaky once an hour in real time. Any test asserting `msg is None` for
# intra_check must pin the clock with this.
#
# 2026-09-17: intra_check's real systemd cadence is `*:15,45` (moved off
# `*:0/30` by #454), so :15 is now the hourly-checkpoint minute (#462) and
# :45 is the ordinary quiet tick — :15 no longer means "quiet" here.
_QUIET_TICK_TIME = datetime(2026, 9, 17, 10, 45, tzinfo=_ET)
# A top-of-hour instant, for the hourly-desk-check tests — the FIRST tick
# intra_check makes each hour under its real `*:15,45` cadence, i.e. :15,
# not :00 (intra_check never ticks at :00). Deliberately OUTSIDE the midday
# window (13:00-14:30 ET) — 13:15 is where the 2026-09-17 midday-suppression
# rule silences a quiet tick before the hourly-checkpoint check ever runs,
# which would defeat this test's purpose of exercising that check in
# isolation. The window-specific interaction has its own tests below (see
# `_MIDDAY_COLLISION_TICK_TIME`).
_TOP_OF_HOUR_TIME = datetime(2026, 9, 17, 15, 15, tzinfo=_ET)
# The ONE intra_check tick that collides with the midday report: midday
# runs once per ET date, on the first scheduler tick inside its 13:00-14:30
# window (13:00), and 13:15 is the first intra_check tick after it. This is
# the only instant the 2026-09-17 suppression rule silences. It is also the
# 13:00 hour's guaranteed-pulse tick under #462, which is exactly why the
# rule has to run before `_is_hourly_checkpoint`.
_MIDDAY_COLLISION_TICK_TIME = datetime(2026, 9, 17, 13, 15, tzinfo=_ET)
# A quiet top-of-hour instant LATER in the midday window (14:15) — the
# 14:00 hour's guaranteed pulse. Inside the window but not the colliding
# tick, so it must still send.
_MIDDAY_TOP_OF_HOUR_TIME = datetime(2026, 9, 17, 14, 15, tzinfo=_ET)
# A quiet, non-top-of-hour instant inside the midday window.
_MIDDAY_QUIET_TICK_TIME = datetime(2026, 9, 17, 13, 45, tzinfo=_ET)


def _pin_clock(monkeypatch, when: datetime) -> None:
    monkeypatch.setattr(trader_feed, "et_now", lambda: when)


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
    # 2026-09-17: the footer dropped run_id and the raw provider-request
    # count (engineering detail) — kept: duration and a plain AI cost figure.
    assert "AI cost $0.01" in msg
    assert "provider request" not in msg
    assert f"run {run}" not in msg


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
    # Board item 89 defect 5: this used to assert the internal reason CODE
    # appeared in the owner's message. It must not; the plain-English label
    # that `_SKIP_WHO_LABELS` already held for it must.
    assert "insufficient_cash" not in msg
    assert "Blocked by the desk — insufficient cash" in msg
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
    # 2026-09-17 cadence change: a quiet "intraday_no_trades" tick (nothing
    # actionable at all) no longer sends its own message — it is folded
    # into the top-of-hour summary instead. Give this tick a real execution
    # skip so it stays actionable and this test keeps guarding its original
    # regression: the nested `intraday_scan` result must render, never get
    # masked by the outer "ok" status.
    _evidence(
        db, run, "execution", "execution_skip",
        {"symbol": "SDS", "reason": "insufficient_cash", "detail": "n/a"},
        symbol="SDS",
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
    _pin_clock(monkeypatch, _QUIET_TICK_TIME)  # not the top-of-hour tick
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


def test_intraday_evidence_gate_skip_is_not_silent(tmp_path, monkeypatch):
    """A refused decision is the opposite of a quiet 'nothing to scan'
    tick. The feed must name the skip, not swallow it with the everyday
    no-new-activity statuses."""
    _make_db(tmp_path, monkeypatch)
    outer = {
        "status": "ok", "run_id": "intra_check-skip", "daily_pnl": 10.0,
        "intraday_scan": {
            "status": "evidence_gate_skip",
            "run_id": "intra_check-skip",
            "lost_seats": ["news"],
            "reason": "decision skipped: 1 seat(s) were asked and their "
                      "answer never arrived — news=carry_forward_empty.",
            "candidates": ["AAPL"],
        },
    }
    msg = trader_feed.format_session_result("intra_check", outer, 4.0)
    assert msg is not None
    assert "DECISION SKIPPED" in msg
    assert "news" in msg


def test_normal_intraday_ok_tick_remains_silent(tmp_path, monkeypatch):
    _make_db(tmp_path, monkeypatch)
    _pin_clock(monkeypatch, _QUIET_TICK_TIME)  # not the top-of-hour tick
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
    # Pinned to a morning ET instant: the header's own 12-hour "AM" marker
    # must not collide with the "PM" substring this test checks for — that
    # substring means Portfolio Manager, not the header's time-of-day
    # designator (an unpinned clock made this flaky in the PM hours).
    _pin_clock(monkeypatch, datetime(2026, 9, 17, 11, 0, tzinfo=_ET))
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
    assert "status: Early close" in msg  # humanize_status("early_close")
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


# === extract_alert_symbols (feeds TelegramNotifier's per-symbol links) ===

def test_extract_alert_symbols_collects_pm_orders_trades_and_skips(tmp_path, monkeypatch):
    db = _make_db(tmp_path, monkeypatch)
    run = "run-symbols"
    _evidence(
        db, run, "portfolio_manager", "proposed_order",
        {"action": "BUY", "symbol": "SQQQ", "allocation_pct": 8}, symbol="SQQQ",
    )
    _trade(db, run, "CCJ", "BUY", qty=40, price=58.10)
    _evidence(
        db, run, "execution", "execution_skip",
        {"symbol": "MSFT", "reason": "no_cash"}, symbol="MSFT",
    )

    symbols = trader_feed.extract_alert_symbols(run, {"status": "executed", "run_id": run})

    assert symbols == ["SQQQ", "CCJ", "MSFT"]


def test_extract_alert_symbols_includes_result_level_orders_and_gaps(tmp_path, monkeypatch):
    """The `result` dict itself (not just the DB) is a source: covers the
    base formatter's own `orders` list and stop-coverage-gap alerts, which
    don't necessarily have a run_id worth reading from the DB."""
    _make_db(tmp_path, monkeypatch)  # empty DB is fine; run_id is None below
    symbols = trader_feed.extract_alert_symbols(
        None,
        {
            "orders": [{"symbol": "AAPL"}],
            "stop_coverage_gaps": [{"symbol": "TSLA", "covered_qty": 0, "held_qty": 10}],
        },
    )
    assert symbols == ["AAPL", "TSLA"]


def test_extract_alert_symbols_dedupes_and_caps_at_ten(tmp_path, monkeypatch):
    db = _make_db(tmp_path, monkeypatch)
    run = "run-many"
    for i in range(12):
        _trade(db, run, f"SYM{i}", "BUY", qty=1, price=10)
    # A repeat of an already-seen symbol must not create a second entry.
    _trade(db, run, "SYM0", "BUY", qty=1, price=10)

    symbols = trader_feed.extract_alert_symbols(run, {"status": "executed", "run_id": run})

    assert len(symbols) == 10
    assert len(symbols) == len(set(symbols))


def test_extract_alert_symbols_handles_missing_run_and_result(tmp_path, monkeypatch):
    _make_db(tmp_path, monkeypatch)
    assert trader_feed.extract_alert_symbols(None, None) == []
    assert trader_feed.extract_alert_symbols("no-such-run", {}) == []


# === Company identities in the real alerts the operator receives ===
#
# The dead-code defect (2026-09-01): `src/notifier.py::_append_company_identities`
# was only ever wired into the BASE formatter's `_append_trade_session_body`,
# which real trading sessions never reach — `run_morning`/`run_position_review`
# emit "executed"/"no_trades"/"reviewed", none of which are in
# `_BASE_ONLY_STATUSES`, start with "pm_", or equal "paid_analysis_suspended",
# so `format_session_result` above always routed to the richer
# `_format_decision_session` / `_format_position_review` / `_format_intraday`
# formatters instead — and none of those ever called `CompanyProfileStore`.
# A test that only calls `_append_company_identities` (or
# `_append_trade_session_body`) directly — see tests/test_company_profiles.py
# — would pass while this bug shipped for good; every test below goes through
# `trader_feed.format_session_result`, the exact function `src/scheduler.py`
# calls to build a live alert.

CAMECO = CompanyProfile(symbol="CCJ", name="Cameco Corporation", industry="Uranium")
NVIDIA = CompanyProfile(symbol="NVDA", name="NVIDIA Corporation", industry="Semiconductors")


def test_morning_alert_names_the_company_it_traded(tmp_path, monkeypatch):
    """2026-09-17 redesign: no separate 'who:' block any more — the company
    name sits inline next to the ticker the first time it appears, inside
    the scan-first <b>✅ DONE</b> section (see NEW LAYOUT item 3/7)."""
    db = _make_db(tmp_path, monkeypatch)
    run = "run-identity-morning"
    _evidence(
        db, run, "portfolio_manager", "proposed_order",
        {"action": "BUY", "symbol": "CCJ", "allocation_pct": 8,
         "reasoning": "Uranium demand tailwind"},
        symbol="CCJ",
    )
    _trade(db, run, "CCJ", "BUY", qty=40, price=58.10)
    result = {"status": "executed", "run_id": run, "orders": [{"symbol": "CCJ"}]}

    with patch.object(
        CompanyProfileStore, "get_many",
        lambda self, symbols, allow_fetch=True: {"CCJ": CAMECO},
    ):
        msg = trader_feed.format_session_result("morning", result, 12.0)

    assert "who:" not in msg
    assert "<b>✅ DONE</b>" in msg
    assert "BUY CCJ (Cameco Corporation)" in msg
    # The identity renders inside DONE, not a separate trailing block.
    assert msg.index("BUY CCJ (Cameco Corporation)") > msg.index("<b>✅ DONE</b>")


def test_midday_alert_names_the_company_it_traded(tmp_path, monkeypatch):
    db = _make_db(tmp_path, monkeypatch)
    run = "run-identity-midday"
    _trade(db, run, "CCJ", "REDUCE", qty=5, price=60.0)
    result = {
        "status": "reviewed", "run_id": run, "positions": 1,
        "orders": [{"symbol": "CCJ"}],
        "review": {"actions": [{"action": "REDUCE", "symbol": "CCJ",
                                 "reason": "trim the winner"}]},
    }

    with patch.object(
        CompanyProfileStore, "get_many",
        lambda self, symbols, allow_fetch=True: {"CCJ": CAMECO},
    ):
        msg = trader_feed.format_session_result("midday", result, 9.0)

    assert "who:" not in msg
    assert "REDUCE CCJ (Cameco Corporation)" in msg


def test_close_alert_names_the_company_it_traded(tmp_path, monkeypatch):
    db = _make_db(tmp_path, monkeypatch)
    run = "run-identity-close"
    _trade(db, run, "CCJ", "SELL", qty=10, price=61.0)
    result = {
        "status": "reviewed", "run_id": run, "positions": 0,
        "orders": [{"symbol": "CCJ"}], "review": None,
    }

    with patch.object(
        CompanyProfileStore, "get_many",
        lambda self, symbols, allow_fetch=True: {"CCJ": CAMECO},
    ):
        msg = trader_feed.format_session_result("close", result, 7.0)

    assert "who:" not in msg
    assert "SELL CCJ (Cameco Corporation)" in msg


def test_position_review_now_shows_todays_and_total_pnl(tmp_path, monkeypatch):
    """Owner request 2026-09-17: replace the unlabelled 'Session P&L' line
    with today's P&L and a dated total P&L, in the same spot — and, unlike
    the old line, `run_position_review` (midday/close) must actually show
    it. Before this change `run_position_review`'s own returned dict never
    set `daily_pnl` at all, so this line silently never rendered on a
    midday/close message — the operator was never shown a number here,
    whatever "session" was assumed to mean."""
    db = _make_db(tmp_path, monkeypatch)
    run = "run-review-pnl"
    result = {
        "status": "reviewed", "run_id": run, "positions": 1,
        "orders": [], "review": None,
        "daily_pnl": 12.34, "daily_return_pct": 0.13,
        "total_pnl": 44.70, "total_return_pct": 0.46,
        "total_pnl_since": "2026-09-02",
    }
    msg = trader_feed.format_session_result("midday", result, 5.0)
    assert msg is not None
    assert "Session P&L" not in msg
    assert "📈 Today's P&L: +$12.34 (+0.13%)" in msg
    assert "📊 Total P&L since 2026-09-02: +$44.70 (+0.46%)" in msg


def test_position_review_missing_pnl_says_not_available_never_zero(tmp_path, monkeypatch):
    """A midday/close run with no P&L figures at all (e.g. an early
    halt-path result) must say so honestly — never a fabricated $0.00,
    which would read as 'flat today' rather than 'unknown'."""
    db = _make_db(tmp_path, monkeypatch)
    run = "run-review-no-pnl"
    result = {
        "status": "reviewed", "run_id": run, "positions": 0,
        "orders": [], "review": None,
    }
    msg = trader_feed.format_session_result("midday", result, 5.0)
    assert msg is not None
    assert "📈 Today's P&L: not available" in msg
    assert "📊 Total P&L: not available" in msg
    assert "$0.00" not in msg


def test_intraday_alert_names_the_company_it_traded(tmp_path, monkeypatch):
    db = _make_db(tmp_path, monkeypatch)
    run = "run-identity-intra"
    _trade(db, run, "CCJ", "BUY", qty=15, price=59.0)
    outer = {
        "status": "ok", "run_id": run, "daily_pnl": -5.0, "daily_return_pct": -0.05,
        "intraday_scan": {
            "status": "intraday_executed", "run_id": run,
            "candidates": ["CCJ"], "orders": [{"symbol": "CCJ"}],
        },
    }

    with patch.object(
        CompanyProfileStore, "get_many",
        lambda self, symbols, allow_fetch=True: {"CCJ": CAMECO},
    ):
        msg = trader_feed.format_session_result("intra_check", outer, 4.0)

    assert "who:" not in msg
    assert "BUY CCJ (Cameco Corporation)" in msg


def test_missing_profile_degrades_cleanly_through_the_real_formatter(tmp_path, monkeypatch):
    """A symbol the cache has never seen must not break, blank, or shrink
    the rest of the alert — it is simply absent from a `who:` section that
    itself may not appear at all."""
    db = _make_db(tmp_path, monkeypatch)
    run = "run-identity-unknown"
    _evidence(
        db, run, "portfolio_manager", "proposed_order",
        {"action": "BUY", "symbol": "ZZZZ", "allocation_pct": 5,
         "reasoning": "Speculative small-cap entry"},
        symbol="ZZZZ",
    )
    _trade(db, run, "ZZZZ", "BUY", qty=100, price=2.10)
    result = {"status": "executed", "run_id": run, "orders": [{"symbol": "ZZZZ"}]}

    # Cold cache: get_many still returns an entry per requested symbol (real
    # CompanyProfileStore.get_many behaviour with allow_fetch=False) but with
    # every field None — the identity-worthy `bits` list ends up empty.
    with patch.object(
        CompanyProfileStore, "get_many",
        lambda self, symbols, allow_fetch=True: {
            s: CompanyProfile(symbol=s) for s in symbols
        },
    ):
        msg = trader_feed.format_session_result("morning", result, 11.0)

    assert "who:" not in msg
    assert "BUY ZZZZ" in msg


def test_missing_profile_lookup_exception_still_ships_the_alert(tmp_path, monkeypatch):
    """Mirrors the existing `_append_company_identities` failure posture
    (see its try/except) end-to-end: a broken profile store must not cost
    the operator the whole rich alert, and must not fall back to the old,
    plainer base formatter either — only the `who:` garnish is lost."""
    db = _make_db(tmp_path, monkeypatch)
    run = "run-identity-explode"
    _trade(db, run, "CCJ", "BUY", qty=40, price=58.10)
    result = {"status": "executed", "run_id": run, "orders": [{"symbol": "CCJ"}]}

    def _explode(self, symbols, allow_fetch=True):
        raise RuntimeError("cache exploded")

    with patch.object(CompanyProfileStore, "get_many", _explode):
        msg = trader_feed.format_session_result("morning", result, 5.0)

    assert "who:" not in msg
    assert "Cameco" not in msg
    # Still the rich trader-feed formatter, not the old base fallback.
    assert "MORNING" in msg
    assert "BUY CCJ" in msg


def test_length_pressure_drops_details_before_scan_first_content(tmp_path, monkeypatch):
    """2026-09-17 redesign: length pressure must clip inside `<b>DETAILS</b>`
    (`trader_feed._wrap_details`, sized against
    `TelegramNotifier.MAX_MESSAGE_CHARS`) and NEVER the scan-first sections
    above it (header, DONE, BLOCKED, LOOKED AT) — the hard rule from the
    brief: "if clipping, clip inside DETAILS, never the top sections."
    A very long PM rationale (well past every per-field clip) forces the
    formatter's own DETAILS budgeting to trim, proven against the real
    `_build_payload` length budget, not reimplemented."""
    db = _make_db(tmp_path, monkeypatch)
    run = "run-tight-budget"
    long_reasoning = "Uranium demand tailwind, clean breakout. " * 60  # ~2500 chars
    _evidence(
        db, run, "portfolio_manager", "reasoning",
        {"portfolio_view": "Only one clean setup survives the morning screen"},
    )
    _evidence(
        db, run, "portfolio_manager", "proposed_order",
        {"action": "BUY", "symbol": "CCJ", "allocation_pct": 8,
         "reasoning": long_reasoning},
        symbol="CCJ",
    )
    _evidence(
        db, run, "risk_manager", "verdict",
        {"approved": True, "reason_category": "clean", "scale_all_buys": 1.0,
         "reasoning": "Sizing acceptable given current exposure"},
    )
    _trade(db, run, "CCJ", "BUY", qty=40, price=58.10)
    result = {"status": "executed", "run_id": run, "orders": [{"symbol": "CCJ"}]}

    # A tight budget — comfortably fits the scan-first sections (header,
    # P&L-less morning header, DONE) but not the full ~2500-char DETAILS
    # payload plus its wrapper tags.
    monkeypatch.setattr(TelegramNotifier, "MAX_MESSAGE_CHARS", 900)

    with patch.object(
        CompanyProfileStore, "get_many",
        lambda self, symbols, allow_fetch=True: {"CCJ": CAMECO},
    ):
        msg = trader_feed.format_session_result("morning", result, 12.0)

    # The scan-first DONE line survives intact, company name and all.
    assert "BUY CCJ (Cameco Corporation)" in msg
    assert "<b>✅ DONE</b>" in msg
    # DETAILS is present but visibly truncated — the long reasoning does not
    # survive in full.
    assert "<b>DETAILS</b>" in msg
    assert "[details truncated" in msg
    assert long_reasoning.strip() not in msg

    notifier = TelegramNotifier(token="t", chat_id="c")
    symbols = trader_feed.extract_alert_symbols(run, result)
    payload = notifier._build_payload(msg, symbols=symbols, preserve_structural_markup=True)
    final_text = payload["text"]

    # The real Telegram-bound payload also fits, and the same scan-first
    # content survives all the way through escaping/linkification.
    assert len(final_text) <= TelegramNotifier.MAX_MESSAGE_CHARS + 200
    assert "DONE" in final_text
    assert "Cameco Corporation" in final_text


# === Review-only symbols (2026-09-01 gap fix) ===
#
# `extract_alert_symbols` used to pull only from `result["orders"]`,
# `result["stop_coverage_gaps"]`, and the run snapshot's `pm_orders`/
# `trades`/`skips` — never `result["review"]["actions"]`. On a midday/close
# alert that meant a HOLD, or a decided-but-unexecuted SELL, showed up in
# the `_format_position_review` decision list as bare text: no company
# identity line, no tap-through link — while symbols that did trade got
# both. Every test below goes through `trader_feed.format_session_result`
# for the identity line, and separately through `extract_alert_symbols` fed
# into `TelegramNotifier._build_payload` for the real link — the exact two
# consumers `src/scheduler.py`/`main.py` wire together for a live alert.

def _insert_position(db, symbol, qty=10, avg_entry=100.0, current_price=105.0):
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO positions VALUES (?, ?, ?, ?, ?, ?)",
        (symbol, qty, avg_entry, current_price, qty * current_price,
         qty * (current_price - avg_entry)),
    )
    conn.commit()
    conn.close()


def test_midday_hold_only_symbol_gets_linked_and_identified(tmp_path, monkeypatch):
    """The reported gap, reproduced: a HOLD that never became a broker
    trade must still surface in extract_alert_symbols — same tap-through
    link treatment as a symbol that did trade. 2026-09-17 redesign: HELD
    is read from broker-truth `positions` (see `_held_symbols` — the fix
    for the "7 hold(s) / 2 listed" defect), so a realistic fixture holds
    the position; the identity now renders inline in <b>HELD</b>, and the
    reviewer's own HOLD reasoning is kept, unchanged, inside DETAILS."""
    db = _make_db(tmp_path, monkeypatch)
    run = "run-hold-only"
    _insert_position(db, "NVDA")
    result = {
        "status": "reviewed", "run_id": run, "positions": 1,
        "orders": [],
        "review": {
            "risk_level": "low",
            "overall_assessment": "Thesis intact, no action needed.",
            "actions": [{"action": "HOLD", "symbol": "NVDA", "reason": "Thesis intact"}],
        },
    }

    with patch.object(
        CompanyProfileStore, "get_many",
        lambda self, symbols, allow_fetch=True: {"NVDA": NVIDIA},
    ):
        msg = trader_feed.format_session_result("midday", result, 9.0)

    assert "who:" not in msg
    assert "<b>HELD (1)</b>" in msg
    assert "NVDA (NVIDIA Corporation)" in msg
    assert "HOLD NVDA — Thesis intact" in msg  # unchanged, inside DETAILS

    symbols = trader_feed.extract_alert_symbols(run, result)
    assert symbols == ["NVDA"]

    notifier = TelegramNotifier(token="t", chat_id="c")
    payload = notifier._build_payload(msg, symbols=symbols, preserve_structural_markup=True)
    assert '<a href="https://finance.yahoo.com/quote/NVDA">NVDA</a>' in payload["text"]


def test_close_decided_but_unexecuted_sell_gets_linked_and_identified(tmp_path, monkeypatch):
    """A close-review SELL the reviewer decided on, where execution never
    completed (no broker order — still held), must still be linked and
    identified — it is often the one the operator most wants to look up."""
    db = _make_db(tmp_path, monkeypatch)
    run = "run-sell-unexecuted"
    _insert_position(db, "NVDA")
    result = {
        "status": "reviewed", "run_id": run, "positions": 1,
        "orders": [],
        "review": {
            "risk_level": "elevated",
            "overall_assessment": "Thesis broken, exit recommended.",
            "actions": [{"action": "SELL", "symbol": "NVDA", "reason": "Thesis broken"}],
        },
    }

    with patch.object(
        CompanyProfileStore, "get_many",
        lambda self, symbols, allow_fetch=True: {"NVDA": NVIDIA},
    ):
        msg = trader_feed.format_session_result("close", result, 9.0)

    assert "who:" not in msg
    assert "NVDA (NVIDIA Corporation)" in msg
    assert "SELL NVDA — Thesis broken" in msg  # unchanged, inside DETAILS

    symbols = trader_feed.extract_alert_symbols(run, result)
    assert symbols == ["NVDA"]

    notifier = TelegramNotifier(token="t", chat_id="c")
    payload = notifier._build_payload(msg, symbols=symbols, preserve_structural_markup=True)
    assert '<a href="https://finance.yahoo.com/quote/NVDA">NVDA</a>' in payload["text"]


def test_traded_symbol_named_in_both_orders_and_review_appears_once(tmp_path, monkeypatch):
    """A symbol that DID trade is present in both `result["orders"]` and
    `review["actions"]` (the reviewer's REDUCE led to the broker order) —
    it must appear once in the symbol list, not twice, and once in the
    <b>✅ DONE</b> line, not twice."""
    db = _make_db(tmp_path, monkeypatch)
    run = "run-dedupe"
    _trade(db, run, "CCJ", "REDUCE", qty=5, price=60.0)
    result = {
        "status": "reviewed", "run_id": run, "positions": 1,
        "orders": [{"symbol": "CCJ"}],
        "review": {
            "actions": [{"action": "REDUCE", "symbol": "CCJ", "reason": "trim the winner"}],
        },
    }

    symbols = trader_feed.extract_alert_symbols(run, result)
    assert symbols == ["CCJ"]
    assert symbols.count("CCJ") == 1

    with patch.object(
        CompanyProfileStore, "get_many",
        lambda self, symbols, allow_fetch=True: {"CCJ": CAMECO},
    ):
        msg = trader_feed.format_session_result("midday", result, 9.0)

    assert msg.count("CCJ (Cameco Corporation)") == 1

    notifier = TelegramNotifier(token="t", chat_id="c")
    payload = notifier._build_payload(msg, symbols=symbols, preserve_structural_markup=True)
    assert "<a href" in payload["text"]


def test_symbol_order_is_stable_across_repeated_calls(tmp_path, monkeypatch):
    """Same alert, called repeatedly, must produce the same symbol list in
    the same order every time — a set/dict-iteration reorder would make
    both the tests and the live messages flaky."""
    db = _make_db(tmp_path, monkeypatch)
    run = "run-order-stable"
    result = {
        "status": "reviewed", "run_id": run, "positions": 3,
        "orders": [{"symbol": "AAPL"}],
        "review": {
            "actions": [
                {"action": "HOLD", "symbol": "MSFT", "reason": "steady"},
                {"action": "SELL", "symbol": "NVDA", "reason": "thesis broken"},
            ],
        },
    }

    first = trader_feed.extract_alert_symbols(run, result)
    second = trader_feed.extract_alert_symbols(run, result)
    third = trader_feed.extract_alert_symbols(run, result)

    assert first == ["AAPL", "MSFT", "NVDA"]
    assert first == second == third


def test_extract_alert_symbols_direct_call_includes_review_actions(tmp_path, monkeypatch):
    """Direct unit test as a supplement — NOT the proof on its own (see the
    end-to-end tests above), because a direct-call-only test is exactly how
    this gap shipped unnoticed the first time."""
    db = _make_db(tmp_path, monkeypatch)
    run = "run-direct"
    result = {
        "status": "reviewed", "run_id": run,
        "orders": [{"symbol": "aapl"}],
        "review": {"actions": [
            {"action": "hold", "symbol": "msft"},
            {"action": "SELL", "symbol": "nvda"},
        ]},
    }
    assert trader_feed.extract_alert_symbols(run, result) == ["AAPL", "MSFT", "NVDA"]


# === 2026-09-17: section spacing, plain status, signed money, trimmed
# footer, PM-view label, analyzed-signal identities, intra_check cadence ===

VISTRA = CompanyProfile(symbol="VST", name="Vistra Corp", industry="Utilities")


def test_intraday_no_trade_message_is_readable_and_sectioned(tmp_path, monkeypatch):
    """Reproduces the example from the readability complaint end to end:
    an intraday tick that analyzed a few names and found nothing to trade
    (here, execution had a real skip, so the tick is actionable and sends
    its own message immediately — the exact "found nothing to trade but a
    decision WAS in play" case the original wall-of-text example came
    from). Checks every readability change at once against the real
    formatter output — blank lines between sections, no double/leading/
    trailing blank line, plain-English status, signed money, a trimmed
    footer, the "PM view (this check)" label, and a company name for an
    analyzed (not just traded) signal."""
    db = _make_db(tmp_path, monkeypatch)
    run = "run-readability"
    _evidence(
        db, run, "tech_analyst", "analysis",
        {"symbol": "VST", "rating": "buy", "conviction": "low", "risk_reward": 1.83,
         "reasoning": "Momentum continuation but extended near-term."},
        symbol="VST",
    )
    _evidence(
        db, run, "tech_analyst", "analysis",
        {"symbol": "AVGO", "rating": "neutral", "conviction": "low",
         "reasoning": "No clean setup."},
        symbol="AVGO",
    )
    _evidence(db, run, "portfolio_manager", "reasoning", {"portfolio_view": "No trades today."})
    _evidence(
        db, run, "execution", "execution_skip",
        {"symbol": "VST", "reason": "insufficient_cash", "detail": "funding sale pending"},
        symbol="VST",
    )
    _agent_log(db, run, "portfolio_manager", "no trades", cost=0.10)
    outer = {
        "status": "ok", "run_id": run, "daily_pnl": 0.13, "daily_return_pct": 0.001,
        "total_pnl": 44.70, "total_return_pct": 0.46, "total_pnl_since": "2026-09-02",
        "intraday_scan": {
            "status": "intraday_no_trades", "run_id": run,
            "candidates": ["VST", "AVGO"], "orders": [],
        },
    }
    with patch.object(
        CompanyProfileStore, "get_many",
        lambda self, symbols, allow_fetch=True: {"VST": VISTRA},
    ):
        # Not the top-of-hour tick — isolates `_format_intraday`'s own
        # rendering from the separate hourly-digest message.
        _pin_clock(monkeypatch, _QUIET_TICK_TIME)
        msg = trader_feed.format_session_result("intra_check", outer, 1.1)

    assert msg is not None

    # --- plain outcome word in the header, not the raw status code ---
    assert "· FAILED" in msg.splitlines()[0]
    assert "intraday_no_trades" not in msg

    # --- signed money, sign before '$', true minus for negatives ---
    assert "+$0.13" in msg

    # --- "Session P&L" is gone; today's + dated total P&L replace it ---
    assert "Session P&L" not in msg
    assert "📈 Today's P&L: +$0.13 (+0.00%)" in msg
    assert "📊 Total P&L since 2026-09-02: +$44.70 (+0.46%)" in msg

    # --- scan-first sections: VST blocked (desk-side, insufficient cash),
    # AVGO looked at and passed (neutral) ---
    assert "<b>❌ BLOCKED / FAILED</b>" in msg
    assert "VST (Vistra Corp)" in msg
    assert "Blocked by the desk — insufficient cash: funding sale pending" in msg
    assert "<b>👀 LOOKED AT, NO TRADE</b>" in msg
    assert "AVGO NEUTRAL/low — PM passed" in msg

    # --- PM view label, PM's own text untouched, inside DETAILS ---
    assert "<b>DETAILS</b>" in msg
    assert "<blockquote expandable>" in msg and "</blockquote>" in msg
    assert "PM view (this check): No trades today." in msg
    assert "View: No trades today." not in msg  # old bare label is gone

    # --- footer: duration + plain AI cost, no run_id, no raw call count ---
    assert "AI cost $0.10" in msg
    assert "provider request" not in msg
    assert f"run {run}" not in msg

    # --- no separate 'who:' identity block any more ---
    assert "who:" not in msg

    # --- section spacing: no leading/trailing blank line, no double blank ---
    lines = msg.split("\n")
    assert lines[0].strip() != "" and lines[-1].strip() != ""
    assert "\n\n\n" not in msg
    # A blank line separates the header from the P&L lines, and the
    # scan-first sections from the footer.
    pnl_idx = next(i for i, l in enumerate(lines) if l.startswith("📈 Today's P&L"))
    assert lines[pnl_idx - 1] == ""
    footer_idx = next(i for i, l in enumerate(lines) if l.startswith("🧾"))
    assert lines[footer_idx - 1] == ""


def test_every_formatter_header_uses_a_12_hour_clock(tmp_path, monkeypatch):
    """Owner ratified 2026-09-17: no 24-hour clock anywhere in a Telegram
    message. Checks all four header-producing paths at a genuinely
    ambiguous instant (13:05 ET — "13:05" under the old %H:%M format,
    "1:05 PM" under the new one) so a regression back to 24-hour can't
    hide behind an AM hour that looks the same either way."""
    db = _make_db(tmp_path, monkeypatch)
    when = datetime(2026, 9, 17, 13, 5, tzinfo=_ET)
    _pin_clock(monkeypatch, when)

    morning_msg = trader_feed.format_session_result(
        "morning", {"status": "executed", "run_id": "run-clock-morning", "orders": []}, 1.0,
    )
    midday_msg = trader_feed.format_session_result(
        "midday",
        {"status": "reviewed", "run_id": "run-clock-midday", "positions": 0,
         "orders": [], "review": None},
        1.0,
    )
    # Called directly (not through format_session_result's cadence
    # gating) — this test is about the header format, not the quiet-tick
    # rules a non-top-of-hour, non-actionable instant would otherwise
    # collapse to `None` on.
    intraday_msg = trader_feed._format_intraday(
        {"run_id": "run-clock-intra"},
        {
            "status": "intraday_no_trades", "run_id": "run-clock-intra",
            "candidates": [], "orders": [],
        },
        1.0,
    )
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO agent_logs(agent_name, run_id, output_summary, cost_usd) VALUES (?, ?, ?, ?)",
                 ("x", "run-clock-hour", "x", 0.0))
    conn.commit()
    conn.close()
    hourly_msg = trader_feed._format_hourly_desk_check(
        {"run_id": "run-clock-hour", "daily_pnl": None}, None, 1.0,
    )

    for msg in (morning_msg, midday_msg, intraday_msg, hourly_msg):
        assert msg is not None
        header = msg.splitlines()[0]
        assert "1:05 PM ET" in header, header
        assert "13:05" not in header, header


def test_base_formatter_header_timestamp_is_also_12_hour(monkeypatch):
    """The base (non-trader-feed) formatter's own header timestamp — used
    for market_holiday/broker_error/analysis_error/kill_switch_halted and
    every other status trader_feed.py routes straight through — must use
    the same 12-hour clock, not just the rich formatters."""
    # notifier.py's base `format_session_result` imports `et_now` LOCALLY
    # inside the function body (`from src.trading_calendar import
    # et_now`), fetched fresh on every call — unlike trader_feed.py's
    # module-level import, patching `src.notifier.et_now` would do
    # nothing here. Patch the source it actually re-imports from.
    import src.trading_calendar as _trading_calendar
    monkeypatch.setattr(
        _trading_calendar, "et_now", lambda: datetime(2026, 9, 17, 13, 5, tzinfo=_ET),
    )
    msg = trader_feed.format_session_result(
        "morning", {"status": "market_holiday"}, 1.0,
    )
    assert msg is not None
    assert "1:05 PM ET" in msg
    assert "13:05" not in msg


def test_mission_control_link_has_exactly_one_blank_line_before_it():
    """The section-spacing change must not touch `TelegramNotifier`'s own
    link append — still exactly one blank line before the Mission Control
    anchor, per `_build_payload`."""
    notifier = TelegramNotifier(
        token="t", chat_id="c", mission_control_url="https://mc.example/run/1",
    )
    payload = notifier._build_payload("⚪ intra_check · body\n\nsecond section")
    assert payload["text"].endswith(
        '\n\n<a href="https://mc.example/run/1">🔗 Open Mission Control</a>'
    )
    assert "\n\n\n" not in payload["text"]


def test_quiet_intraday_no_trades_tick_sends_nothing(tmp_path, monkeypatch):
    """The core cadence change: a :30 tick that analyzed candidates and
    genuinely found nothing to do — no order, no skip, no coverage problem
    — sends NO message at all any more (it used to always send the
    'INTRADAY OPPORTUNITY ... NO TRADE' message). It is folded into the
    next top-of-hour summary instead."""
    db = _make_db(tmp_path, monkeypatch)
    run = "run-quiet-half-hour"
    _evidence(
        db, run, "tech_analyst", "analysis",
        {"symbol": "AVGO", "rating": "neutral", "conviction": "low",
         "reasoning": "No clean setup."},
        symbol="AVGO",
    )
    _pin_clock(monkeypatch, _QUIET_TICK_TIME)  # NOT the top-of-hour tick
    outer = {
        "status": "ok", "run_id": run, "daily_pnl": 3.0, "daily_return_pct": 0.01,
        "intraday_scan": {
            "status": "intraday_no_trades", "run_id": run,
            "candidates": ["AVGO"], "orders": [],
        },
    }
    msg = trader_feed.format_session_result("intra_check", outer, 2.0)
    assert msg is None


def test_actionable_intraday_tick_sends_immediately_at_any_time(tmp_path, monkeypatch):
    """Anything actionable — here, a real execution skip on a :30 tick —
    still sends its own message right away, exactly as before, regardless
    of the clock/cadence change."""
    db = _make_db(tmp_path, monkeypatch)
    run = "run-actionable-half-hour"
    _evidence(
        db, run, "tech_analyst", "analysis",
        {"symbol": "AMD", "rating": "buy", "conviction": "medium", "risk_reward": 1.5,
         "reasoning": "Breakout above resistance."},
        symbol="AMD",
    )
    _evidence(
        db, run, "execution", "execution_skip",
        {"symbol": "AMD", "reason": "insufficient_cash", "detail": "funding sale pending"},
        symbol="AMD",
    )
    _pin_clock(monkeypatch, _QUIET_TICK_TIME)  # NOT the top-of-hour tick
    outer = {
        "status": "ok", "run_id": run, "daily_pnl": -1.0, "daily_return_pct": -0.01,
        "intraday_scan": {
            "status": "intraday_no_trades", "run_id": run,
            "candidates": ["AMD"], "orders": [],
        },
    }
    msg = trader_feed.format_session_result("intra_check", outer, 3.0)
    assert msg is not None
    assert "⚡ INTRADAY OPPORTUNITY" in msg
    # Board item 89 defect 5 — plain words, never the internal code.
    assert "insufficient_cash" not in msg
    assert "Blocked by the desk — insufficient cash" in msg
    # A half-hour tick is not the hourly checkpoint — no desk-check banner.
    assert "🕐 DESK CHECK" not in msg


def test_top_of_hour_quiet_tick_sends_hourly_summary_with_half_hour_signals(
    tmp_path, monkeypatch,
):
    """The guaranteed hourly pulse: the :00 tick itself is quiet (no
    candidates moved this tick), but the :30 tick earlier in the hour DID
    analyze a symbol under a DIFFERENT run_id — the hourly summary must
    still surface it, built from DB records rather than this tick's own
    (empty) run.
    """
    db = _make_db(tmp_path, monkeypatch)
    half_hour_run = "run-half-hour-earlier"
    _evidence(
        db, half_hour_run, "tech_analyst", "analysis",
        {"symbol": "CEG", "rating": "neutral", "conviction": "low",
         "reasoning": "Range-bound, no signal."},
        symbol="CEG",
    )
    top_of_hour_run = "run-top-of-hour"
    _pin_clock(monkeypatch, _TOP_OF_HOUR_TIME)
    outer = {
        "status": "ok", "run_id": top_of_hour_run,
        "daily_pnl": 5.5, "daily_return_pct": 0.02,
        "total_pnl": 44.70, "total_return_pct": 0.46, "total_pnl_since": "2026-09-02",
        # No `intraday_scan` key at all — this tick's own scan did not run
        # (e.g. nothing moved enough to qualify).
    }
    msg = trader_feed.format_session_result("intra_check", outer, 1.5)

    assert msg is not None
    assert "🕐 DESK CHECK" in msg
    assert "No action this hour" in msg
    assert "CEG" in msg  # the earlier :30 tick's signal, different run_id
    assert "Session P&L" not in msg
    assert "📈 Today's P&L: +$5.50 (+0.02%)" in msg
    assert "📊 Total P&L since 2026-09-02: +$44.70 (+0.46%)" in msg
    assert "who:" in msg or "CEG" in msg
    # No leading/trailing/double blank line in the synthetic summary either.
    assert not msg.startswith("\n") and not msg.endswith("\n")
    assert "\n\n\n" not in msg


# === 2026-09-17 regression guard: the hourly checkpoint must track
# intra_check's REAL timer cadence, not a hard-coded minute ===
#
# PR #454 moved intra_check's systemd timer from `*:0/30` to `*:15,45` the
# same night, and `_is_hourly_checkpoint` kept checking `minute == 0` — a
# minute intra_check never ticks on any more. The guaranteed once-per-hour
# message silently stopped firing on any quiet day. This block (a) proves
# that would have been caught, and (b) pins the fix so the NEXT cadence
# move can't repeat it silently: `_is_hourly_checkpoint` now reads its
# checkpoint minute from the unit file itself, and this test independently
# re-parses that same file and fails the build the moment the two disagree.

_INTRA_CHECK_TIMER = (
    Path(trader_feed.__file__).resolve().parent.parent
    / "scripts" / "systemd" / "quant-agent-intra_check.timer"
)


def _real_intra_check_oncalendar_minutes() -> tuple[int, ...]:
    """Independently re-derives intra_check's per-hour tick minutes straight
    from the unit file's `OnCalendar=` line — deliberately NOT calling
    `trader_feed._intra_check_tick_minutes`, so a bug in that function's own
    parsing can't hide from this test."""
    text = _INTRA_CHECK_TIMER.read_text()
    spec = next(
        line.split("=", 1)[1].strip()
        for line in text.splitlines()
        if line.strip().startswith("OnCalendar=")
    )
    match = re.fullmatch(r"\*:(\d{1,2}(?:,\d{1,2})*)", spec)
    assert match, f"unexpected OnCalendar spec {spec!r} in {_INTRA_CHECK_TIMER}"
    return tuple(sorted({int(m) for m in match.group(1).split(",")}))


def test_hourly_checkpoint_minute_matches_intra_check_timer_cadence():
    """The regression guard itself: if a future PR moves intra_check's
    timer again without updating (or correctly re-deriving) the
    hourly-checkpoint minute, this fails — instead of the owner's
    guaranteed hourly message silently going quiet in production again."""
    real_minutes = _real_intra_check_oncalendar_minutes()
    assert trader_feed._intra_check_tick_minutes() == real_minutes
    assert trader_feed._hourly_checkpoint_minute() == min(real_minutes)


def test_is_hourly_checkpoint_follows_a_synthetic_cadence_change(monkeypatch):
    """`_is_hourly_checkpoint` must track WHATEVER cadence intra_check's
    timer reports, not a number frozen at write-time — simulate the timer
    moving to `*:20,50` and confirm the checkpoint minute moves with it."""
    monkeypatch.setattr(trader_feed, "_intra_check_tick_minutes", lambda: (20, 50))
    assert trader_feed._hourly_checkpoint_minute() == 20
    assert trader_feed._is_hourly_checkpoint(datetime(2026, 9, 17, 10, 20, tzinfo=_ET))
    assert not trader_feed._is_hourly_checkpoint(datetime(2026, 9, 17, 10, 50, tzinfo=_ET))
    assert not trader_feed._is_hourly_checkpoint(datetime(2026, 9, 17, 10, 0, tzinfo=_ET))
    # The 9:30 session-open exception survives any cadence.
    assert trader_feed._is_hourly_checkpoint(datetime(2026, 9, 17, 9, 30, tzinfo=_ET))


def test_intra_check_ticks_send_exactly_one_guaranteed_message_per_hour(
    tmp_path, monkeypatch,
):
    """Walk every real intra_check tick across a full trading session
    (09:30-16:00 ET, `*:15,45` cadence) on a completely quiet day and
    confirm the guaranteed hourly pulse fires EXACTLY once per clock hour
    — not zero, not two."""
    _make_db(tmp_path, monkeypatch)
    minutes = trader_feed._intra_check_tick_minutes()
    # The real intra_check schedule: 9:30 (session open, no earlier :15/:45
    # tick that day), then every configured minute from 9:45 through 15:45.
    ticks = [(9, 30)] + [
        (hour, minute)
        for hour in range(9, 16)
        for minute in minutes
        if not (hour == 9 and minute < 30)
    ]
    checkpoint_hits = [
        (hour, minute) for hour, minute in ticks
        if trader_feed._is_hourly_checkpoint(datetime(2026, 9, 17, hour, minute, tzinfo=_ET))
    ]
    # One session-open exception, plus one per ordinary hour 10 through 15.
    assert checkpoint_hits == [(9, 30)] + [(h, min(minutes)) for h in range(10, 16)]


def test_hourly_checkpoint_unparseable_oncalendar_fails_loudly(tmp_path, monkeypatch):
    """A cadence syntax `_intra_check_tick_minutes` doesn't understand (a
    day restriction, a `/` step, ...) must raise, not silently fall back to
    a guessed minute — the whole point of this fix is that a mismatch
    between the timer and the checkpoint logic is never quiet again."""
    fake_timer = tmp_path / "quant-agent-intra_check.timer"
    fake_timer.write_text("[Timer]\nOnCalendar=Mon..Fri *:15,45\n")
    monkeypatch.setattr(trader_feed, "_INTRA_CHECK_TIMER_PATH", fake_timer)
    with pytest.raises(RuntimeError):
        trader_feed._intra_check_tick_minutes()


# === Owner decision, 2026-09-17: suppress the routine intra_check message
# that collides with the midday position review ===
#
# The midday report and intra_check's routine "nothing to report" ping must
# not fire minutes apart. The ratified fix silences that routine ping ONCE,
# at the single colliding tick (13:15 ET) — not for the whole 13:00-14:30
# window. Everything actionable (an order, a stop-coverage gap, a crashed
# scan, ...) still sends immediately, in or out of the window, and the
# guaranteed hourly pulse survives everywhere except that one tick.


def test_collision_tick_is_derived_not_hard_coded(monkeypatch, tmp_path):
    """The suppressed minute must come from the midday window constant and
    intra_check's own timer unit — never a third hard-coded number. Move the
    cadence and the suppressed tick must move with it."""
    from src.trading_calendar import SESSION_WINDOWS

    assert trader_feed._midday_collision_tick_minute() == 13 * 60 + 15

    fake_timer = tmp_path / "quant-agent-intra_check.timer"
    fake_timer.write_text("[Timer]\nOnCalendar=*:05,35\n")
    monkeypatch.setattr(trader_feed, "_INTRA_CHECK_TIMER_PATH", fake_timer)
    assert trader_feed._midday_collision_tick_minute() == 13 * 60 + 5

    lo, _hi = SESSION_WINDOWS["midday"]
    assert trader_feed._midday_collision_tick_minute() >= lo


def test_quiet_collision_tick_suppressed(tmp_path, monkeypatch):
    """13:15 is the 13:00 hour's guaranteed-pulse tick, so without this rule
    it would send a desk check ~15 minutes after the midday report. A quiet
    tick there is suppressed, before `_is_hourly_checkpoint` is consulted."""
    db = _make_db(tmp_path, monkeypatch)
    _evidence(
        db, "run-half-hour-earlier", "tech_analyst", "analysis",
        {"symbol": "CEG", "rating": "neutral", "conviction": "low",
         "reasoning": "Range-bound, no signal."},
        symbol="CEG",
    )
    _pin_clock(monkeypatch, _MIDDAY_COLLISION_TICK_TIME)  # 13:15 ET
    outer = {
        "status": "ok", "run_id": "run-midday-collision",
        "daily_pnl": 2.0, "daily_return_pct": 0.01,
        "intraday_scan": {
            "status": "intraday_scan_no_opportunity",
            "run_id": "run-midday-collision",
        },
    }
    assert trader_feed.format_session_result("intra_check", outer, 2.0) is None


def test_later_hourly_pulse_in_window_still_sends(tmp_path, monkeypatch):
    """Regression guard on the scope of this rule. `midday` runs ONCE per ET
    date (run_if_et_window.sh writes a last-run marker for every mode except
    intra_check), so only 13:00 carries a midday report — suppressing every
    quiet tick across 13:00-14:30 would swallow the 14:00 hour's guaranteed
    pulse (14:15) and leave a quiet day with nothing between 13:00 and
    15:15. 14:15 must still send."""
    db = _make_db(tmp_path, monkeypatch)
    _evidence(
        db, "run-earlier", "tech_analyst", "analysis",
        {"symbol": "CEG", "rating": "neutral", "conviction": "low",
         "reasoning": "Range-bound, no signal."},
        symbol="CEG",
    )
    _pin_clock(monkeypatch, _MIDDAY_TOP_OF_HOUR_TIME)  # 14:15 ET
    outer = {
        "status": "ok", "run_id": "run-midday-later-hour",
        "daily_pnl": 1.0, "daily_return_pct": 0.005,
    }
    msg = trader_feed.format_session_result("intra_check", outer, 1.0)
    assert msg is not None
    assert "DESK CHECK" in msg


def test_ordinary_quiet_tick_in_window_unchanged(tmp_path, monkeypatch):
    """13:45 is inside the window but is neither the colliding tick nor an
    hour-owning one — it is silent for the pre-existing "ordinary quiet tick
    sends nothing" reason, not because of this rule."""
    _make_db(tmp_path, monkeypatch)
    _pin_clock(monkeypatch, _MIDDAY_QUIET_TICK_TIME)  # 13:45 ET
    assert trader_feed._is_midday_collision_tick(_MIDDAY_QUIET_TICK_TIME) is False
    outer = {
        "status": "ok", "run_id": "run-midday-quiet",
        "daily_pnl": 2.0, "daily_return_pct": 0.01,
        "intraday_scan": {
            "status": "intraday_scan_no_opportunity", "run_id": "run-midday-quiet",
        },
    }
    assert trader_feed.format_session_result("intra_check", outer, 2.0) is None


def test_same_hourly_pulse_minute_outside_window_still_sends(tmp_path, monkeypatch):
    """The identical quiet payload at the identical minute-past-the-hour,
    from outside the midday window, is untouched by this rule — this is the
    control for `test_quiet_collision_tick_suppressed`."""
    db = _make_db(tmp_path, monkeypatch)
    _evidence(
        db, "run-outside", "tech_analyst", "analysis",
        {"symbol": "CEG", "rating": "neutral", "conviction": "low",
         "reasoning": "Range-bound, no signal."},
        symbol="CEG",
    )
    _pin_clock(monkeypatch, _MIDDAY_COLLISION_TICK_TIME.replace(hour=11))  # 11:15 ET
    outer = {
        "status": "ok", "run_id": "run-outside-collision-minute",
        "daily_pnl": 2.0, "daily_return_pct": 0.01,
        "intraday_scan": {
            "status": "intraday_scan_no_opportunity",
            "run_id": "run-outside-collision-minute",
        },
    }
    msg = trader_feed.format_session_result("intra_check", outer, 2.0)
    assert msg is not None
    assert "DESK CHECK" in msg


def test_weekend_is_never_a_collision_tick():
    """`in_session_window` short-circuits on non-weekdays; the rule must not
    claim a collision on a day midday never runs."""
    saturday = datetime(2026, 9, 19, 13, 15, tzinfo=_ET)
    assert saturday.weekday() == 5
    assert trader_feed._is_midday_collision_tick(saturday) is False


def test_order_placed_still_sends_at_collision_tick(tmp_path, monkeypatch):
    db = _make_db(tmp_path, monkeypatch)
    run = "run-midday-order"
    _trade(db, run, "CCJ", "BUY", qty=15, price=59.0)
    _pin_clock(monkeypatch, _MIDDAY_COLLISION_TICK_TIME)  # 13:15 ET
    outer = {
        "status": "ok", "run_id": run, "daily_pnl": -5.0, "daily_return_pct": -0.05,
        "intraday_scan": {
            "status": "intraday_executed", "run_id": run,
            "candidates": ["CCJ"], "orders": [{"symbol": "CCJ"}],
        },
    }
    msg = trader_feed.format_session_result("intra_check", outer, 4.0)
    assert msg is not None
    assert "CCJ" in msg


def test_stop_coverage_gap_still_sends_at_collision_tick(tmp_path, monkeypatch):
    _make_db(tmp_path, monkeypatch)
    _pin_clock(monkeypatch, _MIDDAY_COLLISION_TICK_TIME)  # 13:15 ET
    outer = {
        "status": "ok", "run_id": "run-midday-gap",
        "daily_pnl": -1.0, "daily_return_pct": -0.01,
        "stop_coverage_gaps": [{"symbol": "TSLA", "covered_qty": 0, "held_qty": 10}],
    }
    msg = trader_feed.format_session_result("intra_check", outer, 3.0)
    assert msg is not None
    assert "TSLA" in msg


def test_crashed_scan_still_sends_at_collision_tick(tmp_path, monkeypatch):
    """An error carve-out, pinned at the one suppressed instant."""
    _make_db(tmp_path, monkeypatch)
    _pin_clock(monkeypatch, _MIDDAY_COLLISION_TICK_TIME)  # 13:15 ET
    outer = {
        "status": "ok", "run_id": "run-midday-crash",
        "daily_pnl": 0.0, "daily_return_pct": 0.0,
        "intraday_scan": {
            "status": "intraday_scan_crashed", "run_id": "run-midday-crash",
        },
    }
    assert trader_feed.format_session_result("intra_check", outer, 3.0) is not None


# Not tested here: the daily-loss circuit breaker itself
# (`TradingPipeline._alert_owner_daily_loss_halt`, src/pipeline.py). It
# pushes its own alert via a direct `send_owner_alert` call at the moment
# the halt happens, entirely independent of `format_session_result`/
# `trader_feed.py` — this suppression rule cannot reach it regardless of
# window, and its shape is already pinned in tests/test_pipeline.py
# (search `daily_loss_halted`).


def test_signals_list_never_drops_an_analyzed_symbol(tmp_path, monkeypatch):
    """Regression: a live message once read '5 analyzed' but listed only 4
    bullets, silently dropping the 5th (SOXX) — the old per-tick renderer
    capped the bullet list at 4 while the header count stayed uncapped.
    Every analyzed symbol must appear, however many there are."""
    db = _make_db(tmp_path, monkeypatch)
    run = "run-five-signals"
    symbols = ["SOXX", "NVDA", "AMD", "AVGO", "QCOM"]
    for sym in symbols:
        _evidence(
            db, run, "tech_analyst", "analysis",
            {"symbol": sym, "rating": "neutral", "conviction": "low",
             "reasoning": f"{sym}: no clean setup."},
            symbol=sym,
        )
    _evidence(
        db, run, "execution", "execution_skip",
        {"symbol": "NVDA", "reason": "insufficient_cash", "detail": "n/a"},
        symbol="NVDA",
    )
    _pin_clock(monkeypatch, _QUIET_TICK_TIME)  # not the top-of-hour tick
    outer = {
        "status": "ok", "run_id": run, "daily_pnl": 1.0, "daily_return_pct": 0.01,
        "intraday_scan": {
            "status": "intraday_no_trades", "run_id": run,
            "candidates": symbols, "orders": [],
        },
    }
    msg = trader_feed.format_session_result("intra_check", outer, 2.0)
    assert msg is not None
    assert "🔎 Signals: 5 analyzed" in msg
    for sym in symbols:
        assert f"   • {sym}:" in msg, f"{sym} missing from the signals list"


# === 2026-09-17 scan-first redesign: reproduces the real 13:05 message ===
#
# Owner feedback on the pre-redesign live message: "unless I read
# everything word for word, I have no idea what was actually really done
# and what just failed or was killed... there has to be a better way of
# creating a logical format so it's easier to read or scan." This fixture
# reproduces that exact 13:05 intraday tick's data (AMD BUY submitted-not-
# filled, FLNC SHORT blocked by QAMC's own fat-finger guard — not the
# broker, CHPX/OKLO/RKLB analyzed and passed) and asserts the new
# scan-first section placement, not just substring presence.

AMD_PROFILE = CompanyProfile(symbol="AMD", name="Advanced Micro Devices", industry="Semiconductors")
FLNC_PROFILE = CompanyProfile(symbol="FLNC", name="Fluence Energy", industry="Energy Storage")
CHPX_PROFILE = CompanyProfile(symbol="CHPX", name="Global X AI Semiconductor ETF", industry="ETF")
OKLO_PROFILE = CompanyProfile(symbol="OKLO", name="Oklo Inc", industry="Nuclear Power")
RKLB_PROFILE = CompanyProfile(symbol="RKLB", name="Rocket Lab", industry="Aerospace")
_1305_PROFILES = {
    "AMD": AMD_PROFILE, "FLNC": FLNC_PROFILE, "CHPX": CHPX_PROFILE,
    "OKLO": OKLO_PROFILE, "RKLB": RKLB_PROFILE,
}


def test_1305_intraday_message_is_scan_first_sectioned(tmp_path, monkeypatch):
    """Reproduces the real 13:05 message end to end and checks the new
    layout mechanically, not just by substring:
      - header carries ONE outcome word (PARTIAL: one done, one blocked)
      - AMD is DONE but 'placed, waiting to fill' — never implying a fill
        the DB doesn't confirm (trades.fill_status='submitted')
      - FLNC is BLOCKED/FAILED and says the DESK's own fat-finger guard
        stopped it — not "broker rejected" (the operator-reported defect:
        the live message blamed the broker for QAMC's own price-sanity
        check; see journalctl quant-agent-intra_check.service ~17:05:30
        UTC, "Fat-finger guard: ... Order REJECTED")
      - CHPX/OKLO/RKLB are LOOKED AT, NO TRADE — analyzed and passed, not
        silently absent (the "5 actionable" vs "2 acted on" defect)
      - DONE / BLOCKED / LOOKED AT all render before DETAILS, and the full
        per-stock reasoning survives unchanged inside DETAILS
      - no separate 'who:' block; company names are inline instead
    """
    db = _make_db(tmp_path, monkeypatch)
    run = "run-1305"

    for sym, rating, conviction, rr, reason in [
        ("AMD", "buy", "high", 0.23, "Breakout above the 50-day with volume confirmation."),
        ("CHPX", "buy", "medium", 1.6, "AI semiconductor basket tracking the group move."),
        ("FLNC", "sell", "medium", 1.4, "Storage names rolling over; breakdown below the 20-day."),
        ("OKLO", "buy", "medium", 1.8, "SMR nuclear theme continuation, extended."),
        ("RKLB", "buy", "medium", 1.5, "Launch cadence news flow supportive."),
    ]:
        _evidence(
            db, run, "tech_analyst", "analysis",
            {"symbol": sym, "rating": rating, "conviction": conviction,
             "risk_reward": rr, "reasoning": reason},
            symbol=sym,
        )

    _evidence(
        db, run, "portfolio_manager", "reasoning",
        {"portfolio_view": "AMD and FLNC clear the bar this check; CHPX/OKLO/RKLB are "
                            "tracking their group moves without an idiosyncratic edge."},
    )
    _evidence(
        db, run, "portfolio_manager", "proposed_order",
        {"action": "BUY", "symbol": "AMD", "allocation_pct": 19.81,
         "stop_loss": 493.24, "reasoning": "Breakout confirmation, cleanest expression in the book."},
        symbol="AMD",
    )
    _evidence(
        db, run, "portfolio_manager", "proposed_order",
        {"action": "SHORT", "symbol": "FLNC", "allocation_pct": 3.46,
         "stop_loss": 9.66, "reasoning": "Short expression of storage-sector weakness."},
        symbol="FLNC",
    )
    _evidence(
        db, run, "risk_manager", "verdict",
        {"approved": True, "reason_category": "rr_fail", "scale_all_buys": 1.0,
         "reasoning": "AMD's structural R/R is thin but sizing is inside policy."},
    )

    # AMD: order accepted by the broker, still working (never implies a fill).
    _trade(db, run, "AMD", "BUY", qty=1.7662, price=551.20, status="submitted")
    # FLNC: the pending row a submit attempt always writes first — this one
    # never went live (see the execution_skip below for WHY).
    _trade(db, run, "FLNC", "SHORT", qty=43, price=7.75, status="submit_failed")

    # The real defect: FLNC's own price-sanity check (fat-finger guard)
    # blocked it BEFORE the broker ever saw the order — reason code
    # 'fat_finger_guard', not 'broker_rejected', and the DETAIL is plain
    # words (src/execution/broker.py's `_PLAIN_PRICE_LABELS`), never the
    # internal field name/precision a log line carries.
    _evidence(
        db, run, "execution", "execution_skip",
        {"symbol": "FLNC", "reason": "fat_finger_guard",
         "detail": "stop $9.66 is 24% from price $7.79"},
        symbol="FLNC",
    )
    _agent_log(db, run, "portfolio_manager", "2 changes", cost=0.21)

    outer = {
        "status": "ok", "run_id": run, "daily_pnl": 34.34, "daily_return_pct": 0.35,
        "intraday_scan": {
            "status": "intraday_executed", "run_id": run,
            "candidates": ["AMD", "CHPX", "FLNC", "OKLO", "RKLB"],
            "orders": [{"symbol": "AMD"}, {"symbol": "FLNC"}],
        },
    }

    with patch.object(
        CompanyProfileStore, "get_many",
        lambda self, symbols, allow_fetch=True: {
            s: _1305_PROFILES[s] for s in symbols if s in _1305_PROFILES
        },
    ):
        _pin_clock(monkeypatch, datetime(2026, 9, 17, 13, 5, tzinfo=_ET))
        msg = trader_feed.format_session_result("intra_check", outer, 300.0)

    assert msg is not None

    # --- header: ONE outcome word, computed (one done, one blocked) ---
    header = msg.splitlines()[0]
    # 12-hour clock, no 24-hour time anywhere (owner ratified 2026-09-17).
    assert header.startswith("⚡ INTRADAY OPPORTUNITY · 1:05 PM ET")
    assert header.endswith("PARTIAL")

    # --- section placement: DONE, then BLOCKED, then LOOKED AT, then
    # DETAILS, then the footer — in that order, each present exactly once ---
    for marker in (
        "<b>✅ DONE</b>", "<b>❌ BLOCKED / FAILED</b>",
        "<b>👀 LOOKED AT, NO TRADE</b>", "<b>DETAILS</b>", "🧾 AI cost",
    ):
        assert msg.count(marker) == 1, f"{marker!r} should appear exactly once"
    done_idx = msg.index("<b>✅ DONE</b>")
    blocked_idx = msg.index("<b>❌ BLOCKED / FAILED</b>")
    looked_idx = msg.index("<b>👀 LOOKED AT, NO TRADE</b>")
    details_idx = msg.index("<b>DETAILS</b>")
    footer_idx = msg.index("🧾 AI cost")
    assert done_idx < blocked_idx < looked_idx < details_idx < footer_idx

    # --- AMD: DONE, submitted but NOT implied filled ---
    done_section = msg[done_idx:blocked_idx]
    assert "BUY AMD (Advanced Micro Devices)" in done_section
    assert "placed, waiting to fill" in done_section
    assert "filled" not in done_section.replace("waiting to fill", "")
    assert "stop $493.24" in done_section

    # --- FLNC: SHORT (not SELL), BLOCKED/FAILED, blamed on the DESK, not
    # the broker, in plain words (no internal field name, no "hallucinated") ---
    blocked_section = msg[blocked_idx:looked_idx]
    assert "SHORT FLNC (Fluence Energy)" in blocked_section
    assert "SELL FLNC" not in blocked_section
    assert "Blocked by desk safety check (not the broker)" in blocked_section
    assert "stop $9.66 is 24% from price $7.79" in blocked_section
    assert "stop_loss_price" not in blocked_section
    assert "hallucinat" not in blocked_section.lower()
    # The old defect: this used to read "broker rejected" — must not any more.
    assert "broker rejected" not in blocked_section.lower()
    assert "broker_rejected" not in blocked_section.lower()

    # --- CHPX/OKLO/RKLB: analyzed and passed, not silently dropped ---
    looked_section = msg[looked_idx:details_idx]
    assert "CHPX (Global X AI Semiconductor ETF) BUY/medium — PM passed" in looked_section
    assert "OKLO (Oklo Inc) BUY/medium — PM passed" in looked_section
    assert "RKLB (Rocket Lab) BUY/medium — PM passed" in looked_section

    # --- DETAILS: the full existing per-stock reasoning, unchanged,
    # collapsed behind a real Telegram HTML expandable blockquote ---
    details_section = msg[details_idx:footer_idx]
    assert details_section.startswith("<b>DETAILS</b>\n<blockquote expandable>")
    assert details_section.rstrip().endswith("</blockquote>")
    assert "🔎 Signals: 5 analyzed · 5 actionable" in details_section
    for sym, reason in [
        ("AMD", "Breakout above the 50-day with volume confirmation."),
        ("CHPX", "AI semiconductor basket tracking the group move."),
        ("FLNC", "Storage names rolling over; breakdown below the 20-day."),
        ("OKLO", "SMR nuclear theme continuation, extended."),
        ("RKLB", "Launch cadence news flow supportive."),
    ]:
        assert f"{sym}:" in details_section and reason in details_section
    assert "🧠 PM/Constructor: 2 change(s) · 0 hold(s)" in details_section
    # 2026-09-18: the internal category token ("rr_fail") is rendered in
    # words; the token itself no longer reaches the owner.
    assert "🛡️ Risk: APPROVED · reward too thin for the risk" in details_section
    assert "rr_fail" not in msg
    # Board item 89 defect 5: the DETAILS block used to paste the internal
    # reason code through verbatim. The guard's own plain sentence (the
    # `detail`) is what carries the evidence, unabridged; the code does not
    # appear anywhere in the message.
    assert "fat_finger_guard" not in msg
    assert "Blocked by desk safety check (not the broker)" in details_section

    # --- no separate 'who:' identity block anywhere ---
    assert "who:" not in msg

    # --- real Telegram payload: still fits, markup survives escaping,
    # linkification does not corrupt the structural tags ---
    notifier = TelegramNotifier(token="t", chat_id="c", mission_control_url="https://mc.example/run/1")
    symbols = trader_feed.extract_alert_symbols(run, outer["intraday_scan"])
    payload = notifier._build_payload(msg, symbols=symbols, preserve_structural_markup=True)
    final_text = payload["text"]
    assert len(final_text) <= TelegramNotifier.MAX_MESSAGE_CHARS + 300
    assert final_text.count("<blockquote expandable>") == 1
    assert final_text.count("</blockquote>") == 1
    assert "<b>✅ DONE</b>" in final_text


# === Evening report, 2026-09-18 owner redesign ===
#
# Owner review of the live 2026-09-17 evening message. Each assertion below
# pins one of his points so the structure cannot silently drift back.


def _evening_result(**overrides):
    """A realistic evening result dict, shaped like `run_evening`'s return."""
    result = {
        "status": "analyzed",
        "run_id": "evening-testrun",
        "total_value": 9734.50,
        "daily_pnl": 38.73,
        "daily_return_pct": 0.3994,
        "equity_close": None,
        "pnl_4pm": None,
        "total_pnl": -83.54,
        "total_return_pct": -0.85,
        "total_pnl_since": "2026-09-02",
        "risk_capital_dollars": 720.60,
        "max_daily_loss_pct": 6.7,
        "missing_sessions": [],
        "auto_meta": None,
        "stop_coverage_gaps": [],
        "stop_proximity": [],
        "earnings_proximity": [],
        "analysis": {
            "daily_summary": "Tech led a recovery day.",
            "tomorrow_outlook": "Momentum likely continues.",
            "risk_rating": "moderate",
            "tomorrow_bias": "bullish",
            "tomorrow_conviction": "medium",
            "tomorrow_key_risks": ["VIX complacency near 17.7"],
            "suggested_actions": ["Monitor AMD support at $495"],
        },
    }
    result.update(overrides)
    return result


def _expected_fractional_gap(symbol="AAPL", covered=9.0):
    """The overnight state the hybrid-stop design produces every night."""
    return {
        "symbol": symbol, "held_qty": covered + 0.76, "covered_qty": covered,
        "coverage": "fractional_overnight", "uncovered_qty": 0.76,
        "unprotected_value": 256.44, "repaired": False,
    }


def test_evening_leads_with_todays_and_total_pnl(tmp_path, monkeypatch):
    """Owner request: both P&L figures at the VERY top, above everything
    except a banner that needs reading first."""
    db = _make_db(tmp_path, monkeypatch)
    _insert_position(db, "NVDA")
    msg = trader_feed.format_session_result("evening", _evening_result(), 41.6)

    lines = [line for line in msg.split("\n") if line.strip()]
    assert lines[1].startswith("📈 Today's P&L: +$38.73")
    assert lines[2].startswith("📊 Total P&L since 2026-09-02: −$83.54")
    assert lines[3] == "   Account value: $9,734.50"
    # ...and above the book, which used to come first.
    assert msg.index("Today's P&L") < msg.index("POSITIONS")


def test_evening_drops_run_id_and_provider_request_count(tmp_path, monkeypatch):
    """Both are internal identifiers with no action attached to them."""
    db = _make_db(tmp_path, monkeypatch)
    _insert_position(db, "NVDA")
    _agent_log(db, "evening-testrun", "evening_analyst", "done", cost=0.0)
    msg = trader_feed.format_session_result("evening", _evening_result(), 41.6)

    assert "run_id" not in msg
    assert "evening-testrun" not in msg
    assert "provider request" not in msg


def test_evening_says_analyzed_in_plain_words(tmp_path, monkeypatch):
    """'status: analyzed' only ever meant "the review produced a parseable
    answer". The success case becomes one header word; the failure case —
    which the owner CAN act on — becomes a sentence."""
    db = _make_db(tmp_path, monkeypatch)
    _insert_position(db, "NVDA")

    ok = trader_feed.format_session_result("evening", _evening_result(), 41.6)
    assert ok.split("\n")[0].endswith("REVIEWED")
    assert "analyzed" not in ok.lower()
    assert "status:" not in ok

    failed = trader_feed.format_session_result(
        "evening",
        _evening_result(status="evening_parse_error", analysis=None),
        41.6,
    )
    assert failed.split("\n")[0].endswith("REVIEW FAILED")
    assert "The evening review did not complete" in failed


def test_evening_cost_is_words_not_a_row_of_zeros(tmp_path, monkeypatch):
    """Every seat the evening session runs is on a free model, so the true
    cost is zero — which read as broken rendered as '$0.0000'."""
    db = _make_db(tmp_path, monkeypatch)
    _insert_position(db, "NVDA")
    _agent_log(db, "evening-testrun", "evening_analyst", "done", cost=0.0)
    _agent_log(db, "evening-testrun", "news_analyst_evening", "done", cost=0.0)
    msg = trader_feed.format_session_result("evening", _evening_result(), 41.6)

    assert "$0.0000" not in msg
    assert "AI cost tonight: none — the evening review runs on free models" in msg


def test_evening_cost_says_not_available_when_a_price_is_missing(tmp_path, monkeypatch):
    """An unpriced model must never render as a confident zero."""
    db = _make_db(tmp_path, monkeypatch)
    _insert_position(db, "NVDA")
    _agent_log(db, "evening-testrun", "evening_analyst", "done", cost=None)
    msg = trader_feed.format_session_result("evening", _evening_result(), 41.6)

    assert "AI cost tonight: not available" in msg


def test_evening_is_silent_about_the_expected_overnight_fractional_state(
    tmp_path, monkeypatch,
):
    """The sub-share DAY stop lapsing at the close happens to every
    fractional position every night. A line that never varies is not
    information — the owner called it redundant and it now says nothing."""
    db = _make_db(tmp_path, monkeypatch)
    _insert_position(db, "NVDA")
    result = _evening_result(stop_coverage_gaps=[
        _expected_fractional_gap("AAPL"), _expected_fractional_gap("AMD", 1.0),
    ])
    msg = trader_feed.format_session_result("evening", result, 41.6)

    assert "unprotected" not in msg
    assert "by design" not in msg
    assert "NO STOP" not in msg


def test_evening_speaks_when_a_sub_one_share_holding_has_no_stop(tmp_path, monkeypatch):
    """The one overnight state that is NOT expected: a holding of less than
    one whole share has no whole-share GTC leg, so the ENTIRE position is
    stopless overnight — the classifier still calls it 'fractional'."""
    db = _make_db(tmp_path, monkeypatch)
    _insert_position(db, "NVDA")
    result = _evening_result(stop_coverage_gaps=[{
        "symbol": "BRK-B", "held_qty": 0.44, "covered_qty": 0.0,
        "coverage": "fractional_overnight", "uncovered_qty": 0.44,
        "unprotected_value": 223.74, "repaired": False,
    }])
    msg = trader_feed.format_session_result("evening", result, 41.6)

    assert "🛑 NO STOP OVERNIGHT" in msg
    assert "BRK-B" in msg
    assert "$223.74" in msg


def test_evening_speaks_when_a_remainder_was_not_recovered_in_session(
    tmp_path, monkeypatch,
):
    """'fractional_replaced' means the session's sweep put the stop back.
    A row that claims that without having repaired anything is a fault."""
    db = _make_db(tmp_path, monkeypatch)
    _insert_position(db, "NVDA")
    result = _evening_result(stop_coverage_gaps=[{
        "symbol": "NET", "held_qty": 3.48, "covered_qty": 3.0,
        "coverage": "fractional_replaced", "uncovered_qty": 0.48,
        "unprotected_value": 160.30, "repaired": False,
    }])
    msg = trader_feed.format_session_result("evening", result, 41.6)

    assert "🛑 NO STOP OVERNIGHT" in msg
    assert "NET" in msg


def test_evening_still_raises_a_real_uncovered_position(tmp_path, monkeypatch):
    """Suppressing the nightly line must not suppress the banner that
    matters: a whole-share position with zero coverage."""
    db = _make_db(tmp_path, monkeypatch)
    _insert_position(db, "NVDA")
    result = _evening_result(stop_coverage_gaps=[
        {"symbol": "MRVL", "held_qty": 2.0, "covered_qty": 0.0, "coverage": "none"},
        _expected_fractional_gap("AAPL"),
    ])
    msg = trader_feed.format_session_result("evening", result, 41.6)

    assert "🛑🛑🛑 NO STOP AT ALL" in msg
    assert "MRVL" in msg
    # ...and the expected fractional rows are still not counted into it.
    assert "1 position(s)" in msg


def test_evening_positions_line_reads_as_a_heading(tmp_path, monkeypatch):
    """Owner review item 8 — "Positions: 9 invested $10,650" was a section
    title that did not look like one."""
    db = _make_db(tmp_path, monkeypatch)
    _insert_position(db, "NVDA")
    _insert_position(db, "AMD", qty=2, avg_entry=500.0, current_price=480.0)
    msg = trader_feed.format_session_result("evening", _evening_result(), 41.6)

    assert "<b>POSITIONS (2)</b>" in msg
    assert "📈 Top winners:" in msg
    assert "📉 Underwater:" in msg


def test_evening_tomorrow_carries_a_scale_and_a_consequence(tmp_path, monkeypatch):
    """Owner review item 9 — "moderate" with no scale and "bullish" with no
    consequence both said nothing."""
    db = _make_db(tmp_path, monkeypatch)
    _insert_position(db, "NVDA")
    msg = trader_feed.format_session_result("evening", _evening_result(), 41.6)

    assert "<b>TOMORROW</b>" in msg
    assert "step 2 of 4 (low · moderate · elevated · high)" in msg
    assert "Leaning bullish, medium confidence — tomorrow morning's decisions start from this" in msg
    assert "risk=moderate" not in msg


def test_evening_reports_stop_proximity_and_earnings(tmp_path, monkeypatch):
    """The two additions: what is close to its stop, and what reports
    earnings imminently. An unknown is labelled, never rendered as calm."""
    db = _make_db(tmp_path, monkeypatch)
    _insert_position(db, "NVDA")
    result = _evening_result(
        stop_proximity=[
            {"symbol": "ETN", "status": "near", "price": 409.46,
             "stop": 400.12, "gap": 9.34, "atr": 11.2},
            {"symbol": "BRK-B", "status": "unknown"},
        ],
        earnings_proximity=[
            {"symbol": "AAPL", "sessions_away": 1, "status": "measured"},
            {"symbol": "NOK", "sessions_away": 14, "status": "measured"},
        ],
    )
    msg = trader_feed.format_session_result("evening", result, 41.6)

    assert "<b>WORTH KNOWING</b>" in msg
    assert "ETN" in msg and "one ordinary day's move of its stop" in msg
    assert "Could not check the stop distance on" in msg and "BRK-B" in msg
    assert "AAPL" in msg and "reports earnings tomorrow" in msg
    # A report two weeks out is not news tonight.
    assert "NOK" not in msg.split("<b>WORTH KNOWING</b>")[1].split("<b>")[0]


def test_evening_says_nothing_when_nothing_is_worth_knowing(tmp_path, monkeypatch):
    db = _make_db(tmp_path, monkeypatch)
    _insert_position(db, "NVDA")
    msg = trader_feed.format_session_result("evening", _evening_result(), 41.6)

    assert "WORTH KNOWING" not in msg


def test_evening_details_block_uses_the_shared_collapsible_layout(tmp_path, monkeypatch):
    db = _make_db(tmp_path, monkeypatch)
    _insert_position(db, "NVDA")
    _agent_log(db, "evening-testrun", "evening_analyst", "done", cost=0.0)
    msg = trader_feed.format_session_result("evening", _evening_result(), 41.6)

    assert "<b>DETAILS</b>\n<blockquote expandable>" in msg
    assert msg.count("</blockquote>") == 1
    details = msg.split("<b>DETAILS</b>")[1]
    assert "Tech led a recovery day." in details
    assert "Monitor AMD support at $495" in details

    notifier = TelegramNotifier(token="t", chat_id="c")
    payload = notifier._build_payload(msg, symbols=["NVDA"], preserve_structural_markup=True)
    assert len(payload["text"]) <= TelegramNotifier.MAX_MESSAGE_CHARS + 300
    assert "<b>POSITIONS (1)</b>" in payload["text"]


# === 2026-09-18: substance in every message — a count is not information ===
#
# Owner's own words on the pre-earnings message ("analyzed: 1 confirmed: 1
# failed: 0"): "which one? what's the symbol? what's the company?". These
# pin the fixes to board item 89's clarity defects across the messages
# that are NOT the evening report (which was redesigned separately).

NVDA_PROFILE = CompanyProfile(symbol="NVDA", name="NVIDIA", industry="Semiconductors")
MRVL_PROFILE = CompanyProfile(symbol="MRVL", name="Marvell Technology", industry="Semiconductors")


def _profiles_patch(profiles: dict):
    return patch.object(
        CompanyProfileStore, "get_many",
        lambda self, symbols, allow_fetch=False: {
            s: profiles[s] for s in symbols if s in profiles
        },
    )


def test_earnings_message_names_each_company_and_what_it_concluded(tmp_path, monkeypatch):
    _make_db(tmp_path, monkeypatch)
    _pin_clock(monkeypatch, _QUIET_TICK_TIME)
    result = {
        "status": "preprocessed", "run_id": "ep-1",
        "analyzed": 1, "confirmed": 1, "failed": 1,
        "filings": [
            {"symbol": "NVDA", "form_type": "10-Q", "filing_date": "2026-09-17",
             "outcome": "analyzed", "sentiment": "bullish", "conviction": "high",
             "key_thesis": "Data-centre revenue accelerated again."},
            {"symbol": "OKLO", "form_type": "10-K", "filing_date": "2026-09-16",
             "outcome": "failed"},
        ],
    }
    with _profiles_patch({"NVDA": NVDA_PROFILE, "OKLO": OKLO_PROFILE}):
        msg = trader_feed.format_session_result("earnings_preprocess", result, 18.5)

    assert msg is not None
    assert msg.startswith("📄 PRE-MARKET EARNINGS · 10:45 AM ET · PARTLY READ")
    assert "NVDA (NVIDIA) — quarterly report (10-Q) filed 2026-09-17: bullish, high conviction" in msg
    assert "Data-centre revenue accelerated again." in msg
    assert "OKLO (Oklo Inc) — annual report (10-K) filed 2026-09-16" in msg
    assert "COULD NOT BE READ" in msg
    assert "Nothing was bought or sold" in msg
    # The old count line, the run id and the raw status are gone.
    assert "analyzed: 1" not in msg
    assert "run_id" not in msg and "ep-1" not in msg
    assert "preprocessed" not in msg


def test_earnings_message_says_when_the_companies_were_not_recorded(tmp_path, monkeypatch):
    """A result from before the run recorded its filings must say so —
    never invent a name, never drop the figures it did record."""
    _make_db(tmp_path, monkeypatch)
    _pin_clock(monkeypatch, _QUIET_TICK_TIME)
    result = {"status": "preprocessed", "run_id": "ep-old", "analyzed": 2, "confirmed": 2, "failed": 0}
    msg = trader_feed.format_session_result("earnings_preprocess", result, 3.0)
    assert "did not record which companies" in msg
    assert "2 read" in msg


def test_earnings_fault_names_the_filings_left_waiting(tmp_path, monkeypatch):
    _make_db(tmp_path, monkeypatch)
    _pin_clock(monkeypatch, _QUIET_TICK_TIME)
    result = {
        "status": "analysis_error", "run_id": "ep-2", "error": "RateLimitError: 429",
        "filings": [{"symbol": "NVDA", "form_type": "10-Q", "filing_date": "2026-09-17",
                     "outcome": "waiting"}],
    }
    with _profiles_patch({"NVDA": NVDA_PROFILE}):
        msg = trader_feed.format_session_result("earnings_preprocess", result, 2.0)
    assert "· FAILED" in msg
    assert "no filing below was read" in msg
    assert "WAITING TO BE READ" in msg
    assert "NVDA (NVIDIA) — quarterly report (10-Q) filed 2026-09-17" in msg
    # The exception text is kept, but labelled as machine output.
    assert "Machine fault text, kept for the record" in msg
    assert "RateLimitError: 429" in msg


def test_earnings_silent_statuses_stay_silent(tmp_path, monkeypatch):
    """The noise policy is untouched: nothing_new / fetch_error / holiday."""
    _make_db(tmp_path, monkeypatch)
    for status in ("nothing_new", "fetch_error", "market_holiday"):
        assert trader_feed.format_session_result(
            "earnings_preprocess", {"status": status, "run_id": "x"}, 1.0,
        ) is None


def test_hourly_desk_check_names_the_orders_and_the_holdings(tmp_path, monkeypatch):
    db = _make_db(tmp_path, monkeypatch)
    _trade(db, "run-earlier", "AMD", "SELL", qty=2, price=150.0, status="filled")
    _insert_position(db, "NVDA")
    _pin_clock(monkeypatch, _TOP_OF_HOUR_TIME)
    outer = {"status": "ok", "run_id": "run-top", "daily_pnl": 1.0, "daily_return_pct": 0.01}
    with _profiles_patch({"AMD": AMD_PROFILE, "NVDA": NVDA_PROFILE}):
        msg = trader_feed.format_session_result("intra_check", outer, 1.0)

    assert msg is not None
    # A run that only sold does not read TRADED.
    assert msg.startswith("🕐 DESK CHECK · 3:15 PM ET · SOLD")
    assert "⚡ 1 order(s) this hour" in msg
    assert "   • SELL AMD (Advanced Micro Devices) 2 @ $150.00 — filled" in msg
    assert "💼 Positions held: 1" in msg
    assert "   • NVDA (NVIDIA)" in msg


def test_quiet_hourly_check_still_sends_nothing_off_the_hour(tmp_path, monkeypatch):
    """Naming holdings must not make an idle tick speak: the silence rule
    is unchanged."""
    db = _make_db(tmp_path, monkeypatch)
    _insert_position(db, "NVDA")
    _pin_clock(monkeypatch, _QUIET_TICK_TIME)
    outer = {"status": "ok", "run_id": "run-quiet", "daily_pnl": 1.0}
    assert trader_feed.format_session_result("intra_check", outer, 1.0) is None


def test_degraded_research_is_named_in_words_not_component_names(tmp_path, monkeypatch):
    _make_db(tmp_path, monkeypatch)
    _pin_clock(monkeypatch, _QUIET_TICK_TIME)
    result = {
        "status": "no_trades", "run_id": "run-deg", "orders": [],
        "data_status": {"macro": "failed", "tech": "partial", "news": "ok",
                        "smart_money": "wobbly_new_token"},
    }
    msg = trader_feed.format_session_result("morning", result, 1.0)
    assert "Research was incomplete this session" in msg
    assert "the market-backdrop research did not return an answer" in msg
    assert "the chart research returned only part of an answer" in msg
    # An unmapped state is described and the raw token labelled, not guessed.
    assert "no plain wording for (kept for the record: wobbly_new_token)" in msg
    assert "Data degraded: macro" not in msg


def test_coverage_gap_names_the_company_and_says_what_to_do(tmp_path, monkeypatch):
    _make_db(tmp_path, monkeypatch)
    _pin_clock(monkeypatch, _QUIET_TICK_TIME)
    result = {
        "status": "no_trades", "run_id": "run-gap", "orders": [],
        "stop_coverage_gaps": [
            {"symbol": "MRVL", "held_qty": 2.0, "covered_qty": 0.0, "coverage": "none",
             "unprotected_value": 151.30, "repair_refusal": "no recorded stop level to rebuild from"},
            {"symbol": "NVDA", "held_qty": 10.0, "covered_qty": 4.0, "coverage": "partial"},
        ],
    }
    with _profiles_patch({"NVDA": NVDA_PROFILE, "MRVL": MRVL_PROFILE}):
        msg = trader_feed.format_session_result("morning", result, 1.0)
    assert "🚨 NO STOP AT ALL: 1 position(s) with nothing protecting them" in msg
    assert "MRVL (Marvell Technology), holding 2, stop covers 0, $151.30 unprotected — no recorded stop level to rebuild from" in msg
    assert "Place a protective stop by hand or close the position." in msg
    assert "🚨 STOP MIS-SIZED: 1 position(s) only partly protected" in msg
    assert "NVDA (NVIDIA), holding 10, stop covers 4" in msg


def test_looked_at_carries_the_pm_reason_or_says_none_recorded(tmp_path, monkeypatch):
    db = _make_db(tmp_path, monkeypatch)
    run = "run-pass"
    for sym in ("OKLO", "RKLB"):
        _evidence(db, run, "tech_analyst", "analysis",
                  {"symbol": sym, "rating": "buy", "conviction": "medium", "risk_reward": 1.8},
                  symbol=sym)
    _evidence(db, run, "portfolio_manager", "proposed_order",
              {"action": "HOLD", "symbol": "OKLO", "reasoning": "Extended after a 30% run."},
              symbol="OKLO")
    _pin_clock(monkeypatch, _QUIET_TICK_TIME)
    with _profiles_patch({"OKLO": OKLO_PROFILE, "RKLB": RKLB_PROFILE}):
        msg = trader_feed.format_session_result(
            "morning", {"status": "no_trades", "run_id": run, "orders": []}, 1.0,
        )
    assert "OKLO (Oklo Inc) BUY/medium — PM passed — Extended after a 30% run." in msg
    assert "RKLB (Rocket Lab) BUY/medium — PM passed — no reason recorded" in msg
    # The reward figure carries its unit.
    assert "reward 1.8× the risk" in msg
    assert "R/R 1.8" not in msg


def test_position_review_risk_rating_carries_its_scale(tmp_path, monkeypatch):
    db = _make_db(tmp_path, monkeypatch)
    _insert_position(db, "NVDA")
    _pin_clock(monkeypatch, _QUIET_TICK_TIME)
    result = {
        "status": "reviewed", "run_id": "run-rev", "positions": 1,
        "review": {"risk_level": "elevated", "overall_assessment": "x", "actions": []},
        "orders": [], "daily_pnl": 0.0,
    }
    msg = trader_feed.format_session_result("midday", result, 1.0)
    assert "risk elevated — step 3 of 4 (low · moderate · elevated · high)" in msg


def test_company_names_are_not_cut_off_after_the_twelfth_name(tmp_path, monkeypatch):
    """Board item 89 clarity defect: bare tickers after the twelfth name."""
    db = _make_db(tmp_path, monkeypatch)
    symbols = [f"S{i:02d}" for i in range(15)]
    for sym in symbols:
        _insert_position(db, sym)
    profiles = {s: CompanyProfile(symbol=s, name=f"Company {s}", industry="x") for s in symbols}
    _pin_clock(monkeypatch, _QUIET_TICK_TIME)
    result = {"status": "reviewed", "run_id": "run-many", "positions": 15,
              "review": {"actions": []}, "orders": [], "daily_pnl": 0.0}
    with _profiles_patch(profiles):
        msg = trader_feed.format_session_result("close", result, 1.0)
    for sym in symbols:
        assert f"{sym} (Company {sym})" in msg
