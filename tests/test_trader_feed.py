import html
import json
import sqlite3
from unittest.mock import patch

from src import trader_feed
from src.data.company import CompanyProfile, CompanyProfileStore
from src.notifier import TelegramNotifier


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

    assert "who:" in msg
    assert "CCJ — Cameco Corporation · Uranium" in msg
    # Identities render after the real decision content, not instead of it.
    assert msg.index("who:") > msg.index("BUY CCJ")


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

    assert "CCJ — Cameco Corporation · Uranium" in msg


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

    assert "CCJ — Cameco Corporation · Uranium" in msg


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

    assert "CCJ — Cameco Corporation · Uranium" in msg


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


def test_length_pressure_drops_identities_before_decision_content(tmp_path, monkeypatch):
    """Telegram's real length budget (`TelegramNotifier.MAX_MESSAGE_CHARS`,
    `_build_payload`'s tail truncation) is exercised for real here — not
    reimplemented. `_append_identities` in src/trader_feed.py appends the
    `who:` block LAST in every formatter, after the footer, specifically so
    that when the aggregate message must be cut, the existing tail-cut in
    `_build_payload` removes identities first. Shrinking
    `MAX_MESSAGE_CHARS` down to exactly the length of everything BEFORE the
    `who:` section proves that: the cut must land at or before the `who:`
    boundary, never inside the PM/risk decision content that precedes it."""
    db = _make_db(tmp_path, monkeypatch)
    run = "run-tight-budget"
    _evidence(
        db, run, "portfolio_manager", "reasoning",
        {"portfolio_view": "Only one clean setup survives the morning screen"},
    )
    _evidence(
        db, run, "portfolio_manager", "proposed_order",
        {"action": "BUY", "symbol": "CCJ", "allocation_pct": 8,
         "reasoning": "Uranium demand tailwind, clean breakout"},
        symbol="CCJ",
    )
    _evidence(
        db, run, "risk_manager", "verdict",
        {"approved": True, "reason_category": "clean", "scale_all_buys": 1.0,
         "reasoning": "Sizing acceptable given current exposure"},
    )
    result = {"status": "executed", "run_id": run, "orders": [{"symbol": "CCJ"}]}

    with patch.object(
        CompanyProfileStore, "get_many",
        lambda self, symbols, allow_fetch=True: {"CCJ": CAMECO},
    ):
        msg = trader_feed.format_session_result("morning", result, 12.0)

    # Sanity: with a generous budget, identities really are in the message —
    # otherwise the truncation test below would trivially pass for the
    # wrong reason (nothing to drop).
    assert "Cameco" in msg
    core_text, _, _ = msg.partition("\nwho:")
    assert core_text != msg

    notifier = TelegramNotifier(token="t", chat_id="c")
    # Exactly the escaped length of everything before "who:" — no slack for
    # even one character of the identity section to survive.
    monkeypatch.setattr(
        TelegramNotifier, "MAX_MESSAGE_CHARS", len(html.escape(core_text)),
    )

    symbols = trader_feed.extract_alert_symbols(run, result)
    payload = notifier._build_payload(msg, symbols=symbols)
    final_text = payload["text"]

    assert "who:" not in final_text
    assert "Cameco" not in final_text
    # Real decision content — PM section, risk rationale — survives even
    # though the message as a whole had to be cut.
    assert "🧠 PM/Constructor" in final_text
    assert "Sizing acceptable" in final_text


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

def test_midday_hold_only_symbol_gets_linked_and_identified(tmp_path, monkeypatch):
    """The reported gap, reproduced: a HOLD that never became a broker
    trade must still surface in extract_alert_symbols — same identity and
    tap-through link treatment as a symbol that did trade."""
    db = _make_db(tmp_path, monkeypatch)
    run = "run-hold-only"
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

    assert "HOLD NVDA" in msg
    assert "NVDA — NVIDIA Corporation · Semiconductors" in msg

    symbols = trader_feed.extract_alert_symbols(run, result)
    assert symbols == ["NVDA"]

    notifier = TelegramNotifier(token="t", chat_id="c")
    payload = notifier._build_payload(msg, symbols=symbols)
    assert '<a href="https://finance.yahoo.com/quote/NVDA">NVDA</a>' in payload["text"]


def test_close_decided_but_unexecuted_sell_gets_linked_and_identified(tmp_path, monkeypatch):
    """A close-review SELL the reviewer decided on, where execution never
    completed (no broker order), must still be linked and identified —
    it is often the one the operator most wants to look up."""
    db = _make_db(tmp_path, monkeypatch)
    run = "run-sell-unexecuted"
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

    assert "SELL NVDA" in msg
    assert "NVDA — NVIDIA Corporation · Semiconductors" in msg

    symbols = trader_feed.extract_alert_symbols(run, result)
    assert symbols == ["NVDA"]

    notifier = TelegramNotifier(token="t", chat_id="c")
    payload = notifier._build_payload(msg, symbols=symbols)
    assert '<a href="https://finance.yahoo.com/quote/NVDA">NVDA</a>' in payload["text"]


def test_traded_symbol_named_in_both_orders_and_review_appears_once(tmp_path, monkeypatch):
    """A symbol that DID trade is present in both `result["orders"]` and
    `review["actions"]` (the reviewer's REDUCE led to the broker order) —
    it must appear once in the symbol list, not twice, and once in the
    identity block, not twice."""
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

    assert msg.count("CCJ — Cameco Corporation · Uranium") == 1

    notifier = TelegramNotifier(token="t", chat_id="c")
    payload = notifier._build_payload(msg, symbols=symbols)
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
