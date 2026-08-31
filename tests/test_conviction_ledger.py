"""Phase 9 §9.5 — the conviction ledger's recording and scoring layer.

Covers:
  - src/conviction_ledger.py — score_position / aggregate_seat_records /
    summarize_closed_position (pure logic, no I/O)
  - src/storage/db.py — link_nominations_to_decision, record_seat_stances,
    resolve_conviction_ledger, get_conviction_credits
  - src/pipeline_stages.py — _link_nominations_to_decision /
    _record_seat_stances, and the guarantee that neither can move a
    trading decision

NOT covered here (out of scope by the brief): any UI, API route, or
consumption of the ledger by sizing/risk. §9.5's operator-facing view is a
later change.
"""

import json
from unittest.mock import MagicMock

import pytest

from src.conviction_ledger import (
    CONVICTION_WEIGHT,
    SeatCredit,
    SeatStance,
    aggregate_seat_records,
    conviction_weight,
    normalize_seat,
    score_position,
    summarize_closed_position,
)
from src.storage.db import Database


@pytest.fixture
def db(tmp_path):
    database = Database(str(tmp_path / "ledger.db"))
    database.initialize()
    yield database
    database.close()


# ============================================================================
# Pure logic — scoring
# ============================================================================

def test_supporter_scores_positive_r_on_a_winner():
    credits = score_position(
        symbol="NVDA", direction="long", r_multiple=2.0,
        stances=[SeatStance(seat="technical", symbol="NVDA", stance="buy",
                            conviction="high")],
    )
    assert len(credits) == 1
    assert credits[0].side == "supported"
    assert credits[0].credit == pytest.approx(2.0)


def test_dissenting_seat_is_credited_positively_when_the_trade_it_opposed_loses():
    """§9.5's whole point: being right to argue against a loser has to pay.

    Macro said underweight, the desk went long anyway, and the trade lost 1R.
    Macro must score POSITIVELY (+1R x its weight) while the seats that
    supported the trade score negatively — from the same realized outcome.
    """
    stances = [
        SeatStance(seat="technical", symbol="AAPL", stance="buy", conviction="high"),
        SeatStance(seat="macro", symbol="AAPL", stance="underweight", conviction="high"),
    ]
    credits = {c.seat: c for c in score_position(
        symbol="AAPL", direction="long", r_multiple=-1.0, stances=stances,
    )}

    assert credits["macro"].side == "opposed"
    assert credits["macro"].credit == pytest.approx(+1.0), (
        "a seat that opposed a losing trade must be credited positively"
    )
    assert credits["technical"].side == "supported"
    assert credits["technical"].credit == pytest.approx(-1.0)
    # And the mirror image: on a WINNER the same dissent is charged.
    on_winner = {c.seat: c for c in score_position(
        symbol="AAPL", direction="long", r_multiple=+1.0, stances=stances,
    )}
    assert on_winner["macro"].credit == pytest.approx(-1.0)


def test_conviction_weighting_scales_the_credit():
    """Same trade, same side, three declared convictions → three credits in
    the ratio high : medium : low, with the unweighted R preserved on every
    row so the scale can be revisited later."""
    stances = [
        SeatStance(seat="news", symbol="MSFT", stance="buy", conviction="high"),
        SeatStance(seat="earnings", symbol="MSFT", stance="buy", conviction="medium"),
        SeatStance(seat="smart_money", symbol="MSFT", stance="buy", conviction="low"),
    ]
    credits = {c.seat: c for c in score_position(
        symbol="MSFT", direction="long", r_multiple=2.0, stances=stances,
    )}

    assert credits["news"].credit == pytest.approx(2.0 * CONVICTION_WEIGHT["high"])
    assert credits["earnings"].credit == pytest.approx(2.0 * CONVICTION_WEIGHT["medium"])
    assert credits["smart_money"].credit == pytest.approx(2.0 * CONVICTION_WEIGHT["low"])
    assert (credits["news"].credit
            > credits["earnings"].credit
            > credits["smart_money"].credit)
    # Unweighted R is identical across seats — it is the trade's outcome.
    assert {c.r_multiple for c in credits.values()} == {2.0}


def test_conviction_weighting_also_scales_dissent():
    """A high-conviction dissenter earns more for being right than a hedged one."""
    loud = score_position(
        symbol="X", direction="long", r_multiple=-2.0,
        stances=[SeatStance(seat="macro", symbol="X", stance="underweight",
                            conviction="high")],
    )[0]
    quiet = score_position(
        symbol="X", direction="long", r_multiple=-2.0,
        stances=[SeatStance(seat="macro", symbol="X", stance="underweight",
                            conviction="low")],
    )[0]
    assert loud.credit > quiet.credit > 0


def test_neutral_seat_takes_no_side_and_earns_no_credit():
    credits = score_position(
        symbol="T", direction="long", r_multiple=3.0,
        stances=[
            SeatStance(seat="news", symbol="T", stance="neutral"),
            SeatStance(seat="macro", symbol="T", stance=""),
            SeatStance(seat="technical", symbol="T", stance="buy"),
        ],
    )
    assert [c.seat for c in credits] == ["technical"]


def test_short_direction_flips_who_supported():
    """A bearish seat SUPPORTS a short; a bullish one opposes it."""
    credits = {c.seat: c for c in score_position(
        symbol="TSLA", direction="short", r_multiple=1.0,
        stances=[
            SeatStance(seat="technical", symbol="TSLA", stance="sell"),
            SeatStance(seat="news", symbol="TSLA", stance="positive"),
        ],
    )}
    assert credits["technical"].side == "supported"
    assert credits["news"].side == "opposed"


def test_unknown_conviction_gets_the_medium_weight_not_zero():
    assert conviction_weight(None) == CONVICTION_WEIGHT["medium"]
    assert conviction_weight("enormous") == CONVICTION_WEIGHT["medium"]


def test_seat_aliases_collapse_to_one_identity():
    """A seat is one analyst whether it nominated (`news_analyst`) or merely
    rated (`news`) — otherwise its record splits in two."""
    assert normalize_seat("news_analyst") == "news"
    assert normalize_seat("MACRO_ANALYST") == "macro"
    assert normalize_seat("earnings") == "earnings"
    assert normalize_seat("brand_new_seat") == "brand_new_seat"


# ============================================================================
# Pure logic — aggregation
# ============================================================================

def _credit(seat, credit, at, symbol="AAA"):
    return SeatCredit(
        seat=seat, symbol=symbol, side="supported" if credit >= 0 else "opposed",
        stance="buy", conviction="medium", weight=1.0, r_multiple=credit,
        credit=credit, resolved_at=at, position_id=f"pos-{at}",
    )


def test_aggregate_returns_correct_counts_averages_series_and_drawdown():
    """Known fixture, hand-computed expectations.

    macro:   +2, -1, +3, -4  →  4 calls, 2 right,
             avg_win = (2+3)/2 = +2.5, avg_loss = (-1 + -4)/2 = -2.5,
             cumulative series 2, 1, 4, 0 → peak 4, current 0, drawdown 4.
    news:    -1            →  1 call, 0 right, avg_win None, avg_loss -1,
             peak 0 (its own starting point), drawdown 1.
    """
    credits = [
        _credit("macro", 2.0, "2026-01-01"),
        _credit("macro", -1.0, "2026-01-02"),
        _credit("macro", 3.0, "2026-01-03"),
        _credit("macro", -4.0, "2026-01-04"),
        _credit("news", -1.0, "2026-01-02"),
    ]
    records = aggregate_seat_records(credits)

    macro = records["macro"]
    assert macro.resolved_calls == 4
    assert macro.calls_right == 2
    assert macro.win_rate_pct == 50.0
    assert macro.avg_win == pytest.approx(2.5)
    assert macro.avg_loss == pytest.approx(-2.5)
    assert [round(v, 4) for _, v in macro.cumulative] == [2.0, 1.0, 4.0, 0.0]
    assert macro.cumulative_credit == pytest.approx(0.0)
    assert macro.peak == pytest.approx(4.0)
    assert macro.current_drawdown == pytest.approx(4.0)

    news = records["news"]
    assert (news.resolved_calls, news.calls_right) == (1, 0)
    assert news.avg_win is None
    assert news.avg_loss == pytest.approx(-1.0)
    assert news.peak == pytest.approx(0.0)
    assert news.current_drawdown == pytest.approx(1.0)


def test_aggregate_series_order_is_independent_of_input_order():
    forward = [_credit("macro", 2.0, "2026-01-01"), _credit("macro", -1.0, "2026-01-02")]
    assert (aggregate_seat_records(forward)["macro"].cumulative
            == aggregate_seat_records(list(reversed(forward)))["macro"].cumulative)


def test_aggregate_applies_no_sample_size_gate():
    """Owner rejected any minimum-n threshold: one call returns one call,
    with real numbers, not a 'insufficient data' placeholder."""
    record = aggregate_seat_records([_credit("earnings", 1.5, "2026-01-01")])["earnings"]
    assert record.resolved_calls == 1
    assert record.avg_win == pytest.approx(1.5)


def test_aggregate_of_nothing_is_empty_not_zeroed():
    assert aggregate_seat_records([]) == {}


# ============================================================================
# Pure logic — round-trip reduction
# ============================================================================

def _row(action, qty, price, **kw):
    row = {
        "action": action, "qty": qty, "price": price, "symbol": "AAPL",
        "fill_qty": None, "fill_price": None, "fill_status": "filled",
        "stop_loss": 0, "decision_id": None, "position_id": "pos-1",
        "timestamp": "2026-01-01 10:00:00",
    }
    row.update(kw)
    return row


def test_summarize_returns_none_for_a_still_open_chain():
    assert summarize_closed_position([
        _row("BUY", 10, 100.0, stop_loss=95.0),
        _row("PARTIAL_SELL(50%)", 5, 110.0),
    ]) is None


def test_summarize_reduces_a_scaled_in_and_scaled_out_chain():
    closed = summarize_closed_position([
        _row("BUY", 10, 100.0, stop_loss=90.0, decision_id="dec-1"),
        _row("BUY", 10, 110.0, stop_loss=95.0),
        _row("PARTIAL_SELL(50%)", 10, 130.0),
        _row("SELL", 10, 130.0, timestamp="2026-01-09 10:00:00"),
    ])
    assert closed is not None
    assert closed.entry_price == pytest.approx(105.0)
    assert closed.exit_price == pytest.approx(130.0)
    assert closed.initial_stop == pytest.approx(90.0), "the FIRST entry's stop"
    assert closed.decision_id == "dec-1"
    assert closed.direction == "long"
    assert closed.closed_at == "2026-01-09 10:00:00"


def test_summarize_ignores_an_unfilled_trail_stop_placement():
    """A placed-but-never-filled TRAIL_STOP is protection, not an exit — the
    same distinction `_is_filled_trail_stop` already draws."""
    assert summarize_closed_position([
        _row("BUY", 10, 100.0, stop_loss=95.0),
        _row("TRAIL_STOP", 10, 96.0, fill_status="submitted"),
    ]) is None


def test_summarize_counts_a_filled_trail_stop_as_the_exit():
    closed = summarize_closed_position([
        _row("BUY", 10, 100.0, stop_loss=95.0),
        _row("TRAIL_STOP", 10, 96.0, fill_status="filled", fill_qty=10,
             fill_price=95.5),
    ])
    assert closed is not None and closed.exit_price == pytest.approx(95.5)


def test_summarize_keeps_a_stopless_chain_but_marks_it_unscorable():
    closed = summarize_closed_position([
        _row("BUY", 10, 100.0, stop_loss=0),
        _row("SELL", 10, 120.0),
    ])
    assert closed is not None and closed.initial_stop is None


# ============================================================================
# The join — a nomination can be traced to the trade it produced
# ============================================================================

def _write_nomination(db, *, run_id, symbol, seat, conviction="high"):
    """Exactly the row `_record_pipeline_event` writes during the nomination
    responder pass: decision_id NULL, because DecisionStage has not run yet."""
    db.insert_specialist_evidence(
        run_id=run_id, agent_name="pipeline", kind="pipeline_event",
        scope="symbol", symbol=symbol, decision_id=None,
        evidence_json=json.dumps({
            "conviction": conviction, "observation": "clustered insider buying",
            "outcome": "nominated", "reason": "research_seat_nomination",
            "seat": seat, "stage": "opportunity",
        }, sort_keys=True),
    )


def test_nomination_is_written_with_no_decision_id_before_the_backfill(db):
    """The defect this change closes, asserted directly."""
    _write_nomination(db, run_id="run-1", symbol="NVDA", seat="news_analyst")
    row = db.execute(
        "SELECT decision_id FROM specialist_evidence WHERE kind='pipeline_event'"
    ).fetchone()
    assert row["decision_id"] is None


def test_a_nomination_can_be_traced_to_the_trade_it_produced(db):
    """End-to-end join: nomination → decision → trade.

    Before this change the first hop did not exist — the nomination row
    carried decision_id NULL, so no query could connect the seat that raised
    NVDA to the position the desk actually opened in it.
    """
    _write_nomination(db, run_id="run-1", symbol="NVDA", seat="news_analyst")
    _write_nomination(db, run_id="run-1", symbol="AMD", seat="macro_analyst")

    linked = db.link_nominations_to_decision(
        run_id="run-1", decision_id="run-1-dec-abc123",
    )
    assert linked == 2

    db.insert_trade(
        symbol="NVDA", action="BUY", qty=10, price=100.0, reasoning="acting on it",
        run_id="run-1", stop_loss=95.0, fill_status="filled",
        decision_id="run-1-dec-abc123", conviction="high",
    )

    # The join a reader can now actually make.
    joined = db.execute(
        "SELECT t.symbol, t.qty, se.evidence_json FROM trades t "
        "JOIN specialist_evidence se "
        "  ON se.decision_id = t.decision_id AND se.symbol = t.symbol "
        "WHERE se.kind = 'pipeline_event' AND t.action = 'BUY'",
    ).fetchall()
    assert len(joined) == 1
    assert joined[0]["symbol"] == "NVDA"
    assert json.loads(joined[0]["evidence_json"])["seat"] == "news_analyst"

    # And the convenience reader over the same join.
    noms = db.get_nominations_for_decision("run-1-dec-abc123")
    assert {n["symbol"] for n in noms} == {"NVDA", "AMD"}


def test_backfill_touches_only_unjoined_nomination_rows(db):
    """It must never rewrite a row that already carries a decision_id, and
    never touch a non-nomination evidence row."""
    _write_nomination(db, run_id="run-1", symbol="NVDA", seat="news_analyst")
    db.insert_specialist_evidence(
        run_id="run-1", agent_name="pipeline", kind="pipeline_event",
        scope="symbol", symbol="IBM", decision_id="dec-OLD",
        evidence_json=json.dumps({"outcome": "nominated", "seat": "macro_analyst"},
                                 sort_keys=True),
    )
    db.insert_specialist_evidence(
        run_id="run-1", agent_name="macro_analyst", kind="analysis", scope="run",
        evidence_json="{}",
    )
    db.insert_specialist_evidence(
        run_id="run-OTHER", agent_name="pipeline", kind="pipeline_event",
        scope="symbol", symbol="TSLA",
        evidence_json=json.dumps({"outcome": "nominated", "seat": "news_analyst"},
                                 sort_keys=True),
    )

    assert db.link_nominations_to_decision(run_id="run-1", decision_id="dec-NEW") == 1
    rows = {
        (r["symbol"], r["kind"]): r["decision_id"]
        for r in db.execute("SELECT symbol, kind, decision_id FROM specialist_evidence")
    }
    assert rows[("NVDA", "pipeline_event")] == "dec-NEW"
    assert rows[("IBM", "pipeline_event")] == "dec-OLD"     # not rewritten
    assert rows[(None, "analysis")] is None                  # not a nomination
    assert rows[("TSLA", "pipeline_event")] is None          # other run


def test_backfill_is_idempotent(db):
    _write_nomination(db, run_id="run-1", symbol="NVDA", seat="news_analyst")
    assert db.link_nominations_to_decision(run_id="run-1", decision_id="dec-1") == 1
    assert db.link_nominations_to_decision(run_id="run-1", decision_id="dec-1") == 0


# ============================================================================
# Persistence — stances in, credits out
# ============================================================================

def test_seat_stances_round_trip_through_the_evidence_table(db):
    stances = [
        SeatStance(seat="technical", symbol="NVDA", stance="buy", conviction="high"),
        SeatStance(seat="macro", symbol="NVDA", stance="underweight",
                   conviction="medium", nominated=False),
        SeatStance(seat="news", symbol="NVDA", stance="positive", conviction="high",
                   nominated=True, observation="catalyst"),
    ]
    assert db.record_seat_stances(
        run_id="run-1", decision_id="dec-1", stances=stances,
    ) == 3

    read_back = {s.seat: s for s in db.get_seat_stances(decision_id="dec-1")}
    assert read_back["macro"].stance == "underweight", "dissent must survive the round trip"
    assert read_back["news"].nominated is True
    assert read_back["news"].observation == "catalyst"
    assert read_back["technical"].conviction == "high"


def _closed_losing_long(db, *, symbol="AAPL", decision_id="dec-1"):
    """A BUY at 100 with a 90 stop, sold at 95 → -0.5R."""
    db.insert_trade(
        symbol=symbol, action="BUY", qty=10, price=100.0, reasoning="entry",
        run_id="run-1", stop_loss=90.0, fill_status="filled",
        decision_id=decision_id, conviction="high",
    )
    db.insert_trade(
        symbol=symbol, action="SELL", qty=10, price=95.0,
        reasoning="thesis_invalid", run_id="run-2", fill_status="filled",
    )


def test_resolve_scores_a_closed_position_and_persists_it(db):
    """Score on close, end to end through the DB, including the dissent case:
    macro opposed a trade that lost, and is credited positively for it."""
    _closed_losing_long(db)
    db.record_seat_stances(run_id="run-1", decision_id="dec-1", stances=[
        SeatStance(seat="technical", symbol="AAPL", stance="buy", conviction="high"),
        SeatStance(seat="macro", symbol="AAPL", stance="underweight", conviction="high"),
        SeatStance(seat="news", symbol="AAPL", stance="neutral"),
    ])

    result = db.resolve_conviction_ledger()
    assert result["closed_positions"] == 1
    assert result["scored_positions"] == 1
    assert result["credits_written"] == 2  # neutral news took no side

    credits = {c.seat: c for c in db.get_conviction_credits()}
    assert credits["technical"].r_multiple == pytest.approx(-0.5)
    assert credits["technical"].credit == pytest.approx(-0.5)
    assert credits["macro"].side == "opposed"
    assert credits["macro"].credit == pytest.approx(+0.5)
    assert "news" not in credits


def test_resolve_is_idempotent_and_does_not_double_credit(db):
    _closed_losing_long(db)
    db.record_seat_stances(run_id="run-1", decision_id="dec-1", stances=[
        SeatStance(seat="technical", symbol="AAPL", stance="buy"),
    ])
    first = db.resolve_conviction_ledger()
    second = db.resolve_conviction_ledger()
    assert first["scored_positions"] == 1
    assert second["scored_positions"] == 0
    assert second["skipped_already_scored"] == 1
    assert len(db.get_conviction_credits()) == 1


def test_resolve_never_scores_a_position_with_no_entry_stop(db):
    """No stop at entry means no honest R denominator. Counted, never guessed."""
    db.insert_trade(
        symbol="IBM", action="BUY", qty=10, price=100.0, reasoning="entry",
        run_id="run-1", stop_loss=0, fill_status="filled", decision_id="dec-1",
    )
    db.insert_trade(
        symbol="IBM", action="SELL", qty=10, price=120.0, reasoning="target",
        run_id="run-2", fill_status="filled",
    )
    db.record_seat_stances(run_id="run-1", decision_id="dec-1", stances=[
        SeatStance(seat="technical", symbol="IBM", stance="buy"),
    ])
    result = db.resolve_conviction_ledger()
    assert result["skipped_no_r"] == 1
    assert db.get_conviction_credits() == []


def test_resolve_leaves_an_open_position_unscored(db):
    db.insert_trade(
        symbol="MSFT", action="BUY", qty=10, price=100.0, reasoning="entry",
        run_id="run-1", stop_loss=90.0, fill_status="filled", decision_id="dec-1",
    )
    db.record_seat_stances(run_id="run-1", decision_id="dec-1", stances=[
        SeatStance(seat="technical", symbol="MSFT", stance="buy"),
    ])
    result = db.resolve_conviction_ledger()
    assert result["closed_positions"] == 0
    assert db.get_conviction_credits() == []


def test_persisted_credits_aggregate_without_recomputation(db):
    """The read path §9.5 needs: credits come back off disk and go straight
    into the pure aggregate — no outcome is recomputed."""
    _closed_losing_long(db, symbol="AAPL", decision_id="dec-1")
    db.record_seat_stances(run_id="run-1", decision_id="dec-1", stances=[
        SeatStance(seat="technical", symbol="AAPL", stance="buy", conviction="high"),
        SeatStance(seat="macro", symbol="AAPL", stance="underweight", conviction="high"),
    ])
    db.resolve_conviction_ledger()

    records = aggregate_seat_records(db.get_conviction_credits())
    assert records["macro"].resolved_calls == 1
    assert records["macro"].calls_right == 1
    assert records["technical"].calls_right == 0
    assert records["technical"].current_drawdown == pytest.approx(0.5)


# ============================================================================
# The invariant — ledger recording cannot move a trading decision
# ============================================================================

def _decision_stage_pipeline(db):
    """A DecisionStage wired to a REAL PortfolioConstructor, so what it
    returns is the real construction output rather than a mock's."""
    from src.portfolio_constructor import PortfolioConstructor
    from src.pipeline import TradingPipeline

    p = TradingPipeline.__new__(TradingPipeline)
    p.db = db
    p.db_mock_guard = None
    for name in (
        "_build_weekly_narrative", "_build_macro_trajectory",
        "_build_active_state_changes", "_build_rm_recent_verdicts",
        "_build_pm_recent_decisions", "_build_projected_portfolio",
        "_build_calibration_note", "_build_macro_tech_alignment",
        "_build_recent_missed_lessons", "_build_recent_loss_pits",
    ):
        setattr(p, name, MagicMock(return_value=""))
    p._sweeper = MagicMock(return_value=None)
    p._compute_recent_performance = MagicMock(return_value={})
    p._build_position_history = MagicMock(return_value={})
    p._build_pm_facts = MagicMock(return_value=None)
    p._ensure_correlation_matrix = MagicMock(return_value={})
    p.config = MagicMock()
    p.config.risk.allow_margin = False
    p.config.trading.universe = ["NVDA"]
    p._last_symbol_sectors = {}
    p.broker = MagicMock()
    p.broker.get_latest_price.return_value = 100.0
    p.portfolio_constructor = PortfolioConstructor()
    p.portfolio_manager = MagicMock()
    return p


def _pm_decision():
    from src.models import (
        PortfolioDecision, ReasoningChain, TargetPosition,
    )
    return PortfolioDecision(
        portfolio_view="one idea",
        reasoning_chain=ReasoningChain(
            macro_filter="x", news_check="x", earnings_check="x",
            signal_conflicts="x", sizing_logic="x", portfolio_balance="x",
            cash_target="x",
        ),
        targets=[TargetPosition(
            symbol="NVDA", risk_allocation_pct=3.0, conviction="high",
            thesis="breakout with volume", direction="long",
        )],
    )


def _decision_ctx():
    from src.models import TechAnalysisResult, TechReasoningChain
    from src.pipeline_context import RunContext

    ctx = RunContext.start("morning")
    ctx.positions = []
    ctx.analyses = [TechAnalysisResult(
        symbol="NVDA", rating="buy", conviction="high", entry_price=100.0,
        stop_loss=95.0, reference_target=120.0, support_levels=[95.0],
        resistance_levels=[120.0], setup_type="breakout",
        expected_horizon_sessions=10, reasoning="x",
        reasoning_chain=TechReasoningChain(
            trend="x", momentum="x", volatility="x", volume="x",
            support_resistance="x",
        ),
    )]
    ctx.macro_analysis = None
    ctx.total_value = 100_000.0
    ctx.last_equity = 100_000.0
    ctx.cash = 50_000.0
    ctx.deployable_cash = 50_000.0
    ctx.admitted_symbols = set()
    ctx.nomination_convictions = {
        "NVDA": {"news": {"conviction": "high", "observation": "catalyst"}},
    }
    return ctx


def _run_decision_stage(db):
    from src.pipeline_stages import DecisionStage

    p = _decision_stage_pipeline(db)
    p.portfolio_manager.decide.return_value = (
        _pm_decision(),
        MagicMock(user_message="m", raw_text="{}", tokens_used=1, input_tokens=1,
                  output_tokens=1, cost_usd=0.0, model="test-model",
                  semantic_status=None, semantic_error=None),
    )
    ctx = _decision_ctx()
    DecisionStage(pipeline=p).run(ctx)
    return ctx


def test_ledger_recording_does_not_change_a_single_trading_decision(tmp_path, monkeypatch):
    """The load-bearing safety assertion for §9.5.

    Run DecisionStage twice against identical inputs — once with the ledger
    recording live, once with both of its entry points replaced by no-ops —
    and require the constructed orders to serialize BYTE-IDENTICALLY. If the
    bookkeeping could move a decision, this is where it shows.
    """
    import src.pipeline_stages as stages

    db_on = Database(str(tmp_path / "on.db"))
    db_on.initialize()
    db_off = Database(str(tmp_path / "off.db"))
    db_off.initialize()
    try:
        ctx_on = _run_decision_stage(db_on)

        monkeypatch.setattr(
            stages, "_link_nominations_to_decision", lambda *a, **k: None,
        )
        monkeypatch.setattr(stages, "_record_seat_stances", lambda *a, **k: None)
        ctx_off = _run_decision_stage(db_off)

        def _orders(ctx):
            return [d.model_dump_json() for d in ctx.portfolio_decision.decisions]

        assert _orders(ctx_on), "the fixture must actually produce an order"
        assert _orders(ctx_on) == _orders(ctx_off), (
            "conviction-ledger recording changed the constructed orders — it is "
            "bookkeeping and must be inert with respect to what the desk trades"
        )
        # Same for the targets the constructor was given.
        assert (
            [t.model_dump_json() for t in ctx_on.portfolio_decision.targets]
            == [t.model_dump_json() for t in ctx_off.portfolio_decision.targets]
        )
        # ...and the recording genuinely happened on the "on" side, so the
        # comparison above is not two no-ops agreeing with each other.
        assert db_on.get_seat_stances(decision_id=ctx_on.decision_id)
        assert db_off.get_seat_stances(decision_id=ctx_off.decision_id) == []
    finally:
        db_on.close()
        db_off.close()


def test_ledger_recording_failure_never_propagates(tmp_path):
    """A dead DB must degrade the ledger, not the session — same contract as
    every other evidence write (`_persist_evidence`)."""
    from src.pipeline_stages import _link_nominations_to_decision, _record_seat_stances

    p = MagicMock()
    p.db.link_nominations_to_decision.side_effect = RuntimeError("disk full")
    p.db.record_seat_stances.side_effect = RuntimeError("disk full")
    ctx = _decision_ctx()
    ctx.decision_id = "dec-1"

    _link_nominations_to_decision(p, ctx)          # must not raise
    _record_seat_stances(p, ctx, {"NVDA": {"technical": "buy"}}, ["NVDA"])
    p.db.record_seat_stances.assert_called_once()


def test_seat_stances_recorded_carry_dissent_and_declared_conviction(tmp_path):
    """§9.5's dissent requirement at the pipeline seam: an opposing macro
    stance on a bought name is recorded, with the nominating seat's own
    declared conviction attached."""
    from src.pipeline_stages import _record_seat_stances

    db = Database(str(tmp_path / "stances.db"))
    db.initialize()
    try:
        p = MagicMock()
        p.db = db
        ctx = _decision_ctx()
        ctx.decision_id = "dec-1"
        _record_seat_stances(
            p, ctx,
            {"NVDA": {"technical": "buy", "macro": "underweight", "news": "positive"}},
            ["NVDA"],
        )
        recorded = {s.seat: s for s in db.get_seat_stances(decision_id="dec-1")}
        assert recorded["macro"].stance == "underweight"
        assert recorded["news"].nominated is True
        assert recorded["news"].conviction == "high"   # what NEWS declared
        assert recorded["technical"].conviction == "high"  # TechAnalysisResult
        assert recorded["macro"].nominated is False
        assert recorded["macro"].conviction == "medium"    # declared nothing
    finally:
        db.close()
