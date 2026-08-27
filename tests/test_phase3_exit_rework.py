"""Phase 3.1 + 3.2 — the pace feedback loop and the reviewer's missing memory.

These two defects produced the same live failure twice in one week: a position
with an intact thesis sold for "not progressing", graded **premature** by the
system's own evening review hours later (EPD and MRVL, 2026-08-26).

  3.1  `pace` was measured against the system's OWN rolling average hold time,
       so every early sale shortened the average, which made every surviving
       position look stalled, which drove more early sales.
  3.2  the Position Reviewer had no memory of its own prior numbers, so it
       could report deterioration while everything it measured six hours
       earlier had improved.

Each test names the defect it pins.
"""

from datetime import timedelta
from unittest.mock import MagicMock, patch

import pytest

from src.models import Position
from src.pipeline import TradingPipeline
from src.risk.exit_guard import (
    compute_deltas,
    is_deterioration_claim,
    veto_contradicted_exit,
)
from src.trading_calendar import et_today


def _position(symbol="AAA", qty=10, avg_entry=100.0, current_price=110.0):
    return Position(
        symbol=symbol, qty=qty, avg_entry=avg_entry, current_price=current_price,
        market_value=qty * current_price,
        unrealized_pnl=qty * (current_price - avg_entry), sector="Technology",
    )


def _pipeline():
    p = TradingPipeline.__new__(TradingPipeline)
    p.db = MagicMock()
    p.broker = MagicMock()
    p.broker.get_current_stop_price.return_value = None
    p._atr_for_symbol = MagicMock(return_value=2.0)
    return p


def _buy_row(days_ago=6, horizon=15, setup="range", stop=90.0, target=140.0):
    entry_date = (et_today() - timedelta(days=days_ago)).isoformat()
    return {
        "stop_loss": stop, "take_profit": target,
        "timestamp": f"{entry_date} 14:00:00",
        "expected_horizon_sessions": horizon, "setup_type": setup,
    }


def _facts(pipeline, position, buy_row):
    pipeline.db.get_symbol_last_buy.return_value = buy_row
    return pipeline._build_position_facts(
        positions=[position], morning_trades=[], total_value=100_000.0,
    )[position.symbol]


# ===========================================================================
# 3.1 — pace is measured against the horizon pinned at entry
# ===========================================================================

def test_pace_uses_the_horizon_pinned_at_entry():
    """Entry 100, target 140, now 120 → 50% progress. Day 6 of a 12-session
    thesis is half the time for half the progress → pace 1.0x, on schedule."""
    facts = _facts(
        _pipeline(), _position(current_price=120.0),
        _buy_row(days_ago=6, horizon=12),
    )
    assert facts["thesis_progress_pct"] == pytest.approx(50.0)
    assert facts["pace"] == pytest.approx(1.0)
    assert facts["pace_status"] == "measured"
    assert facts["expected_horizon_sessions"] == 12


def test_pace_is_not_measurable_before_one_third_of_the_horizon():
    """A thesis given 15 sessions cannot be behind schedule on day 2. Reading
    it as such is how a healthy young position gets sold for 'not
    progressing'."""
    facts = _facts(
        _pipeline(), _position(current_price=102.0),
        _buy_row(days_ago=2, horizon=15),
    )
    assert facts["pace"] is None
    assert facts["pace_status"] == "too_early"


def test_pace_becomes_measurable_once_a_third_of_the_horizon_elapses():
    facts = _facts(
        _pipeline(), _position(current_price=120.0),
        _buy_row(days_ago=5, horizon=15),
    )
    assert facts["pace"] is not None
    assert facts["pace_status"] == "measured"


def test_progress_and_pace_are_disabled_for_breakout_setups():
    """A breakout's target is a measured-move reference, not a level anyone
    defends — there is nothing to progress toward, so progress against it
    measures nothing and pace against that nothing is worse."""
    facts = _facts(
        _pipeline(), _position(current_price=120.0),
        _buy_row(days_ago=8, horizon=10, setup="breakout"),
    )
    assert facts["thesis_progress_pct"] is None
    assert facts["pace"] is None
    assert facts["pace_status"] == "n/a_breakout"
    assert facts["setup_type"] == "breakout"


def test_legacy_position_without_a_pinned_horizon_gets_no_pace_at_all():
    """The fix is not 'use a different average' — it is that a trade's horizon
    must never be derived from the system's own past behaviour. With nothing
    pinned, the honest answer is no number."""
    row = _buy_row(days_ago=9)
    row["expected_horizon_sessions"] = None
    row["setup_type"] = None
    facts = _facts(_pipeline(), _position(current_price=120.0), row)
    assert facts["pace"] is None
    assert facts["pace_status"] == "unavailable_no_pinned_horizon"
    # Progress still works — it needs only entry and target.
    assert facts["thesis_progress_pct"] == pytest.approx(50.0)


def test_build_position_facts_no_longer_accepts_a_calibration_average():
    """The feedback loop's input is gone from the signature entirely, so it
    cannot be reconnected by accident."""
    import inspect

    params = inspect.signature(TradingPipeline._build_position_facts).parameters
    assert "avg_hold_days" not in params


def test_pipeline_does_not_feed_calibration_hold_time_into_the_review_path():
    """Guard against a future edit quietly restoring the loop."""
    import inspect

    source = inspect.getsource(TradingPipeline.run_position_review)
    # Comments explaining WHY the loop was removed are welcome; a live
    # reference is not. Strip comment text before checking.
    code = "\n".join(
        line.split("#", 1)[0] for line in source.splitlines()
    )
    assert "avg_hold_days" not in code


def test_reviewer_renders_why_pace_is_absent_rather_than_omitting_it():
    """A missing number that reads as 'nothing to see' is how a day-2 position
    got called stalled."""
    from src.agents.position_reviewer import PositionReviewerAgent

    with patch("anthropic.Anthropic"):
        agent = PositionReviewerAgent(api_key="test", model="claude-sonnet-4-6")
    msg = agent.build_user_message(
        positions=[_position()], macro_summary={}, cash_balance=1000.0,
        total_value=100_000.0,
        position_facts={"AAA": {
            "days_held": 2, "expected_horizon_sessions": 15,
            "pace": None, "pace_status": "too_early",
        }},
    )
    assert "not-yet-measurable" in msg
    assert "NOT 'stalled'" in msg


def test_reviewer_prompt_documents_the_pinned_horizon_not_average_hold_time():
    from pathlib import Path

    text = (Path(__file__).resolve().parents[1]
            / "config" / "prompts" / "position_reviewer.md").read_text()
    assert "expected_horizon_sessions` is the horizon the Technical Analyst" in text
    assert "feedback loop" in text


# ===========================================================================
# 3.2 — the reviewer remembers its own numbers
# ===========================================================================

def test_deltas_detect_improvement():
    d = compute_deltas(
        "EPD",
        prior={"thesis_progress_pct": 16.0, "distance_to_stop_pct": 4.0},
        current={"thesis_progress_pct": 20.0, "distance_to_stop_pct": 5.2},
    )
    assert d.has_prior
    assert d.improved == ["distance_to_stop_pct", "thesis_progress_pct"]
    assert d.worsened == []
    assert d.net_improved is True


def test_deltas_detect_deterioration():
    d = compute_deltas(
        "AAA",
        prior={"thesis_progress_pct": 40.0},
        current={"thesis_progress_pct": 12.0},
    )
    assert d.worsened == ["thesis_progress_pct"]
    assert d.net_improved is False


def test_a_mixed_picture_is_not_net_improvement():
    """Progress up but distance-to-stop down is a real judgment call and stays
    the reviewer's to make."""
    d = compute_deltas(
        "AAA",
        prior={"thesis_progress_pct": 16.0, "distance_to_stop_pct": 8.0},
        current={"thesis_progress_pct": 22.0, "distance_to_stop_pct": 3.0},
    )
    assert d.improved == ["thesis_progress_pct"]
    assert d.worsened == ["distance_to_stop_pct"]
    assert d.net_improved is False


def test_movement_inside_the_noise_floor_is_not_improvement():
    d = compute_deltas(
        "AAA",
        prior={"thesis_progress_pct": 20.0},
        current={"thesis_progress_pct": 20.2},
    )
    assert d.improved == []
    assert d.net_improved is False


def test_no_prior_snapshot_yields_no_deltas():
    d = compute_deltas("AAA", prior=None, current={"thesis_progress_pct": 20.0})
    assert d.has_prior is False
    assert d.net_improved is False


@pytest.mark.parametrize("reason", [
    "position is stalling",
    "not progressing after 5 days",
    "no progress toward target",
    "momentum has faded",
    "thesis deteriorating",
    "dead money at this point",
    "going nowhere",
    "behind schedule vs typical hold",
])
def test_deterioration_claims_are_recognised(reason):
    assert is_deterioration_claim(reason) is True


@pytest.mark.parametrize("reason", [
    "thesis_invalid triggered: closed below MA50",
    "bearish earnings — revenue missed by 8%",
    "macro regime flipped to risk-off today",
    "high-conviction bearish state change on the sector",
    "correlation breach: now 3 names in one cluster",
])
def test_new_information_exits_are_not_deterioration_claims(reason):
    assert is_deterioration_claim(reason) is False


def test_the_epd_defect_is_vetoed():
    """The exact 2026-08-26 failure: sold for 'not progressing' when progress
    had risen 16% -> 20% and distance-to-stop had improved."""
    d = compute_deltas(
        "EPD",
        prior={"thesis_progress_pct": 16.0, "distance_to_stop_pct": 4.0},
        current={"thesis_progress_pct": 20.0, "distance_to_stop_pct": 5.2},
    )
    veto = veto_contradicted_exit("SELL", "stalled winner, not progressing", d)
    assert veto is not None
    assert "EPD" in veto
    assert "improved" in veto


def test_an_exit_on_new_information_is_never_vetoed():
    """However good the numbers look. This is the reviewer's retained
    authority (spec 3.8) and the guard must not touch it."""
    d = compute_deltas(
        "AAA",
        prior={"thesis_progress_pct": 16.0, "distance_to_stop_pct": 4.0},
        current={"thesis_progress_pct": 30.0, "distance_to_stop_pct": 9.0},
    )
    assert veto_contradicted_exit(
        "SELL", "thesis_invalid triggered: closed below MA50 on volume", d,
    ) is None
    assert veto_contradicted_exit(
        "SELL", "bearish earnings, revenue missed by 8%", d,
    ) is None


def test_a_deterioration_claim_backed_by_the_numbers_is_not_vetoed():
    d = compute_deltas(
        "AAA",
        prior={"thesis_progress_pct": 40.0, "distance_to_stop_pct": 9.0},
        current={"thesis_progress_pct": 11.0, "distance_to_stop_pct": 2.0},
    )
    assert veto_contradicted_exit("SELL", "stalling badly", d) is None


def test_hold_and_trail_stop_are_never_vetoed():
    d = compute_deltas(
        "AAA", prior={"thesis_progress_pct": 16.0}, current={"thesis_progress_pct": 25.0},
    )
    assert veto_contradicted_exit("HOLD", "stalling", d) is None
    assert veto_contradicted_exit("TRAIL_STOP", "stalling", d) is None


def test_veto_requires_a_prior_snapshot():
    """A first look at a position has nothing to contradict."""
    d = compute_deltas("AAA", prior=None, current={"thesis_progress_pct": 25.0})
    assert veto_contradicted_exit("SELL", "not progressing", d) is None


# ---------------------------------------------------------------------------
# memory plumbing
# ---------------------------------------------------------------------------

def test_deltas_are_built_against_the_previous_run_not_this_one():
    pipeline = _pipeline()
    pipeline.db.get_prior_position_review_metrics.return_value = {
        "AAA": {
            "evidence_json": '{"thesis_progress_pct": 16.0}',
            "timestamp": "2026-08-26 13:00:00",
        },
    }
    deltas = pipeline._build_review_metric_deltas(
        {"AAA": {"thesis_progress_pct": 20.0}}, run_id="midday-xyz",
    )
    pipeline.db.get_prior_position_review_metrics.assert_called_once()
    assert pipeline.db.get_prior_position_review_metrics.call_args.kwargs[
        "exclude_run_id"
    ] == "midday-xyz"
    assert deltas["AAA"].improved == ["thesis_progress_pct"]


def test_unparseable_prior_snapshot_degrades_to_no_prior():
    """A corrupt blob must never produce a wrong comparison."""
    pipeline = _pipeline()
    pipeline.db.get_prior_position_review_metrics.return_value = {
        "AAA": {"evidence_json": "{not json", "timestamp": "2026-08-26 13:00:00"},
    }
    deltas = pipeline._build_review_metric_deltas(
        {"AAA": {"thesis_progress_pct": 20.0}}, run_id="midday-xyz",
    )
    assert deltas["AAA"].has_prior is False


def test_a_failed_prior_read_does_not_break_the_review():
    pipeline = _pipeline()
    pipeline.db.get_prior_position_review_metrics.side_effect = RuntimeError("db down")
    deltas = pipeline._build_review_metric_deltas(
        {"AAA": {"thesis_progress_pct": 20.0}}, run_id="midday-xyz",
    )
    assert deltas["AAA"].has_prior is False


def test_snapshot_persistence_never_raises():
    pipeline = _pipeline()
    pipeline.db.save_position_review_metrics.side_effect = RuntimeError("disk full")
    pipeline._persist_review_metrics(
        {"AAA": {"thesis_progress_pct": 20.0}}, run_id="midday-xyz",
    )   # must not raise


def test_snapshot_writes_only_the_metrics_that_exist():
    pipeline = _pipeline()
    pipeline._persist_review_metrics(
        {"AAA": {"thesis_progress_pct": 20.0, "pace": None, "irrelevant": 5}},
        run_id="midday-xyz",
    )
    payload = pipeline.db.save_position_review_metrics.call_args.kwargs["metrics_json"]
    assert "thesis_progress_pct" in payload
    assert "pace" not in payload
    assert "irrelevant" not in payload


def test_reviewer_prompt_warns_about_positions_that_improved():
    from src.agents.position_reviewer import PositionReviewerAgent

    with patch("anthropic.Anthropic"):
        agent = PositionReviewerAgent(api_key="test", model="claude-sonnet-4-6")
    deltas = {
        "EPD": compute_deltas(
            "EPD",
            prior={"thesis_progress_pct": 16.0, "distance_to_stop_pct": 4.0},
            current={"thesis_progress_pct": 20.0, "distance_to_stop_pct": 5.2},
        ),
    }
    msg = agent.build_user_message(
        positions=[_position("EPD")], macro_summary={}, cash_balance=1000.0,
        total_value=100_000.0, position_facts={"EPD": {}}, metric_deltas=deltas,
    )
    assert "IMPROVED SINCE YOUR LAST REVIEW" in msg
    assert "EPD" in msg
    assert "will be VETOED" in msg


def test_reviewer_prompt_says_so_when_there_is_no_prior():
    from src.agents.position_reviewer import PositionReviewerAgent

    with patch("anthropic.Anthropic"):
        agent = PositionReviewerAgent(api_key="test", model="claude-sonnet-4-6")
    msg = agent.build_user_message(
        positions=[_position()], macro_summary={}, cash_balance=1000.0,
        total_value=100_000.0, position_facts={"AAA": {}},
    )
    assert "No prior snapshot on record" in msg


def test_executor_drops_a_vetoed_sell_and_records_it():
    """End to end through the real action dispatcher."""
    from src.models import PositionAction, PositionReasoningChain, PositionReview

    pipeline = _pipeline()
    review = PositionReview(
        reasoning_chain=PositionReasoningChain(
            macro_continuity_check="stable", thesis_progress_check="stalled",
            thesis_integrity_check="soft", winners_discipline_check="n/a",
            session_disposition_check="midday", execution_rationale="cut it",
        ),
        actions=[PositionAction(
            action="SELL", symbol="EPD", reason="stalled winner, not progressing",
        )],
        overall_assessment="trimming", risk_level="moderate",
    )
    deltas = {
        "EPD": compute_deltas(
            "EPD",
            prior={"thesis_progress_pct": 16.0, "distance_to_stop_pct": 4.0},
            current={"thesis_progress_pct": 20.0, "distance_to_stop_pct": 5.2},
        ),
    }
    orders = pipeline._midday_execute_llm_actions(
        positions=[_position("EPD")], review=review, run_id="midday-xyz",
        metric_deltas=deltas,
    )
    assert orders == []
    pipeline.broker.submit_order.assert_not_called()
    status = pipeline.db.record_intraday_evaluation.call_args.kwargs["status"]
    assert status == "exit_vetoed_contradicts_own_metrics"
