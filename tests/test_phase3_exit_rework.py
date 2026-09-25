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
    # Item 165: `sessions_held` is now sourced from
    # `broker.trading_sessions_held` (holiday-aware), not the pure
    # `trading_calendar` weekday function. None of the windows these tests
    # construct cross a market holiday, so delegating the mock to the real
    # weekday counter reproduces the exact same numbers these tests already
    # assert on, while keeping the pipeline wired the way production is.
    from src.trading_calendar import trading_sessions_held as _weekday_sessions_held

    p = TradingPipeline.__new__(TradingPipeline)
    p.db = MagicMock()
    p.broker = MagicMock()
    p.broker.get_current_stop_price.return_value = None
    p.broker.trading_sessions_held.side_effect = _weekday_sessions_held
    p._atr_for_symbol = MagicMock(return_value=2.0)
    return p


def _buy_row(days_ago=6, horizon=15, setup="range", stop=90.0, target=140.0,
             entry_date=None):
    # `entry_date`, when given, is an explicit `date` — used by tests that
    # need a specific SESSION count rather than whatever `days_ago` happens
    # to work out to against the real wall-clock date (board item 91: a
    # weekend in the range makes days_ago != sessions elapsed).
    ed = entry_date if entry_date is not None else (et_today() - timedelta(days=days_ago))
    return {
        "stop_loss": stop, "take_profit": target,
        "timestamp": f"{ed.isoformat()} 14:00:00",
        "expected_horizon_sessions": horizon, "setup_type": setup,
    }


def _facts(pipeline, position, buy_row, today=None):
    pipeline.db.get_symbol_last_buy.return_value = buy_row
    if today is not None:
        with patch("src.pipeline.et_today", return_value=today):
            return pipeline._build_position_facts(
                positions=[position], morning_trades=[], total_value=100_000.0,
            )[position.symbol]
    return pipeline._build_position_facts(
        positions=[position], morning_trades=[], total_value=100_000.0,
    )[position.symbol]


# ===========================================================================
# 3.1 — pace is measured against the horizon pinned at entry
# ===========================================================================

def test_pace_uses_the_horizon_pinned_at_entry():
    """Entry 100, target 140, now 120 → 50% progress. 6 TRADING SESSIONS
    (Mon 8/24 -> Tue 9/1, one weekend crossed) of a 12-session thesis is
    half the time for half the progress → pace 1.0x, on schedule.

    Board item 91: pinned to explicit dates (rather than a relative
    `days_ago` off the real wall-clock date) because the calendar range
    spans a weekend — `days_ago=6` calendar days does not reliably give 6
    *sessions*, and the point of this test is sessions, not calendar days.
    """
    from datetime import date as _date
    facts = _facts(
        _pipeline(), _position(current_price=120.0),
        _buy_row(entry_date=_date(2026, 8, 24), horizon=12),
        today=_date(2026, 9, 1),
    )
    assert facts["sessions_held"] == 6
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
    """Exactly 5 TRADING SESSIONS (Mon 8/24 -> Mon 8/31, one weekend
    crossed) of a 15-session horizon meets the one-third (5-session)
    threshold. Pinned to explicit dates for the same reason as above —
    `days_ago=5` calendar days across a weekend is only 3-4 sessions on
    most real-world run dates, which would wrongly stay `too_early`."""
    from datetime import date as _date
    facts = _facts(
        _pipeline(), _position(current_price=120.0),
        _buy_row(entry_date=_date(2026, 8, 24), horizon=15),
        today=_date(2026, 8, 31),
    )
    assert facts["sessions_held"] == 5
    assert facts["pace"] is not None
    assert facts["pace_status"] == "measured"


def test_pace_divides_sessions_by_sessions_a_weekend_does_not_move_it():
    """Board item 91: `pace`'s `too_early`/`time_fraction` arithmetic read
    the calendar-day `days_held` while `expected_horizon_sessions` is
    denominated in SESSIONS — a `sessions_held` companion was computed
    right above it and went unused. A Friday buy reviewed the following
    Monday is 3 CALENDAR days but only 1 TRADING SESSION old; under the bug
    that 1-session-old position (1/6 of a 6-session horizon, well under the
    1/3 too_early floor) was reported as "measured" with pace 0.5x — a
    false stalling signal on a position that has seen a single session of
    real price action. This pins the fix: `too_early`/`pace` must be
    computed from `sessions_held`, and the Friday->Monday weekend must not
    move either one.
    """
    from datetime import date as _date
    friday = _date(2026, 9, 11)
    monday = _date(2026, 9, 14)
    facts = _facts(
        _pipeline(), _position(current_price=110.0),
        _buy_row(entry_date=friday, horizon=6, target=140.0),
        today=monday,
    )
    assert facts["days_held"] == 3
    assert facts["sessions_held"] == 1
    # 1 of 6 sessions is under the 1/3 floor (2 sessions) -> too_early, not
    # a measured (and falsely stalled-looking) pace.
    assert facts["pace_status"] == "too_early"
    assert facts["pace"] is None

    # Same review, but a horizon short enough that 1 session clears the
    # 1/3 floor (1 >= max(1, 3/3)) -- confirms the arithmetic is genuinely
    # sessions-over-sessions, not just always "too_early" for a 3-day-old
    # position.
    facts2 = _facts(
        _pipeline(), _position(current_price=110.0),
        _buy_row(entry_date=friday, horizon=3, target=140.0),
        today=monday,
    )
    assert facts2["sessions_held"] == 1
    assert facts2["pace_status"] == "measured"
    # thesis_progress = (110-100)/(140-100)*100 = 25%; time_fraction =
    # sessions_held/horizon = 1/3 -> pace = 25/(100/3) = 0.75. Under the old
    # calendar-day bug this would instead have used days_held=3 (a full
    # horizon's worth of calendar time against a 3-session horizon),
    # giving time_fraction=1.0 and pace=0.25 -- a materially slower-looking
    # position purely because a weekend sat in the date range.
    assert facts2["pace"] == pytest.approx(0.75)


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


def test_measured_breakout_labelled_range_still_disables_progress_and_pace():
    """Item 82 residue. Construction keys the breakout verdict off the analyst
    label OR the MEASURED `structural_ceiling` (either sufficient). A trade the
    analyst labelled "range" while the desk's own level computation found
    nothing overhead (`structural_ceiling=False`, stored 0) IS a breakout by
    construction's verdict, and the pace/progress read path — flagged as the
    single largest P&L defect — must reach that SAME verdict from the pinned
    row, not the label alone."""
    row = _buy_row(days_ago=8, horizon=10, setup="range")
    row["structural_ceiling"] = 0  # measured: no overhead level
    facts = _facts(_pipeline(), _position(current_price=120.0), row)
    assert facts["thesis_progress_pct"] is None
    assert facts["pace"] is None
    assert facts["pace_status"] == "n/a_breakout"


def test_measured_ceiling_range_row_keeps_progress_and_pace():
    """The mirror: a "range" label whose measurement DID find a ceiling
    (`structural_ceiling=True`, stored 1) is a genuine range trade — progress
    and pace still apply, exactly as a NULL/legacy row falls back to the label
    and does."""
    row = _buy_row(days_ago=8, horizon=10, setup="range")
    row["structural_ceiling"] = 1  # measured: a ceiling exists
    facts = _facts(_pipeline(), _position(current_price=120.0), row)
    assert facts["pace_status"] == "measured"
    assert facts["thesis_progress_pct"] == pytest.approx(50.0)


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

    # `run_position_review` became a thin persistence wrapper on 2026-09-18
    # (see `Database.save_session_report`); the real body — where this
    # guard actually bites — is `_run_position_review_body`.
    source = inspect.getsource(TradingPipeline._run_position_review_body)
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
    """The EPD case: PRICE moved away from a FIXED stop, so distance-to-stop
    improving is a real improvement and still counts as one.

    `stop_loss` / `current_price` are carried on the snapshot since
    2026-09-18 so the rise can be ATTRIBUTED (see
    `test_a_wider_stop_is_not_an_improvement`). Stop unchanged at 24.00,
    price 25.00 -> 25.32: (25.32 - 24.00) / 25.32 = 5.2%.
    """
    d = compute_deltas(
        "EPD",
        prior={"thesis_progress_pct": 16.0, "distance_to_stop_pct": 4.0,
               "stop_loss": 24.0, "current_price": 25.0},
        current={"thesis_progress_pct": 20.0, "distance_to_stop_pct": 5.2,
                 "stop_loss": 24.0, "current_price": 25.32},
    )
    assert d.has_prior
    assert d.improved == ["distance_to_stop_pct", "thesis_progress_pct"]
    assert d.stop_driven == []
    assert d.worsened == []
    assert d.net_improved is True


def test_distance_improvement_without_provenance_is_not_counted():
    """A snapshot pair written before 2026-09-18 carries no stop or price,
    so a distance-to-stop rise cannot be attributed to either term.

    It is NOT counted as an improvement. That is the fail-open direction on
    this path: an uncounted improvement can only make
    `veto_contradicted_exit` fire LESS, and a veto that strands the desk in
    a losing position is the worse failure. Self-heals after one session,
    once both snapshots carry the two fields.
    """
    d = compute_deltas(
        "EPD",
        prior={"thesis_progress_pct": 16.0, "distance_to_stop_pct": 4.0},
        current={"thesis_progress_pct": 20.0, "distance_to_stop_pct": 5.2},
    )
    assert d.stop_driven == ["distance_to_stop_pct"]
    assert d.improved == ["thesis_progress_pct"]
    assert "provenance unknown" in d.render()


def test_deltas_short_price_driven_improvement_still_counts():
    """SHORT twin of `test_deltas_detect_improvement`. Stop fixed at 110
    (above price, protecting the short); price falls 100 -> 90, a real
    favourable move. `distance_to_stop_pct` (side-mirrored:
    `(stop - current) / current * 100`) rises 10.0 -> 22.22, and — because
    `qty` is carried and negative on both snapshots — the provenance
    recomputation must recognise this as PRICE-driven, not stop-driven,
    the same as it would for a long."""
    d = compute_deltas(
        "XYZ",
        prior={"distance_to_stop_pct": 10.0, "stop_loss": 110.0,
               "current_price": 100.0, "qty": -10.0},
        current={"distance_to_stop_pct": 22.22, "stop_loss": 110.0,
                 "current_price": 90.0, "qty": -10.0},
    )
    assert d.stop_driven == []
    assert d.improved == ["distance_to_stop_pct"]
    assert d.net_improved is True


def test_deltas_short_widened_stop_is_not_an_improvement():
    """SHORT twin of the V/CMCSA/DIS real-data case: the stop is loosened
    (moved further ABOVE price, from 110 to 130) while price actually moves
    ADVERSELY for the short (100 -> 105, i.e. up). `distance_to_stop_pct`
    still rises 10.0 -> 23.8 because both terms moved, but holding the stop
    at its prior value (110) with the new price (105) gives
    (110-105)/105*100 = 4.76 — LOWER than the prior 10.0, so price alone
    does not carry the rise. Must land in `stop_driven`, excluded from
    `improved`, exactly like the long case — not the sign-flipped opposite
    conclusion a long-only recomputation would reach."""
    d = compute_deltas(
        "XYZ",
        prior={"distance_to_stop_pct": 10.0, "stop_loss": 110.0,
               "current_price": 100.0, "qty": -10.0},
        current={"distance_to_stop_pct": 23.8, "stop_loss": 130.0,
                 "current_price": 105.0, "qty": -10.0},
    )
    assert d.stop_driven == ["distance_to_stop_pct"]
    assert d.improved == []
    assert "wider stop, NOT an improvement" in d.render()


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
    "sector shock — the whole group gapped down together",
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


# ===========================================================================
# 3.3 — every exit names a trigger, not just the second one that day
# ===========================================================================

def test_trigger_vocabulary_covers_every_category_spec_38_sanctions():
    """Gating every exit against a list that did not cover the whole of 3.8
    would block legitimate exits. Each category must have a representative."""
    from src.pipeline import _reason_cites_hard_trigger

    for reason in (
        "thesis_invalid triggered: closed below MA50",
        "thesis broken — the catalyst was priced in",
        "HIGH-conviction bearish state change on the name",
        "adverse news: FDA rejected the filing",
        "material news broke after the open",
        "sector shock — the whole group gapped down",
        "bearish earnings, revenue missed",
        "earnings miss on both lines",
        "guidance cut for the full year",
        "macro regime shift to defensive today",
        "regime flip confirmed this morning",
        "macro flipped risk-off today",
        # "daily loss circuit breaker fired" left this list 2026-09-20 with
        # the account-level loss alarm itself (WORK.md item 32); nothing
        # computes that event now.
        "stopped out at the broker",
    ):
        assert _reason_cites_hard_trigger(reason) is True, reason


def test_soft_flags_are_still_not_triggers():
    """These are recurring flags, not events. Mechanically acting on them is
    what produced the repeated trims of strengthening theses."""
    from src.pipeline import _reason_cites_hard_trigger

    for reason in (
        "TARGET_BREACH at 150% thesis progress",
        "Concentration drift; valuation stretched at 28x forward",
        "momentum cooling, take some off",
        "prudent to harvest into strength",
        "position looks extended here",
        "de-risking ahead of the weekend",
        "",
    ):
        assert _reason_cites_hard_trigger(reason) is False, reason


def test_concentration_is_deliberately_not_a_trigger():
    """Drift trims belong to the Portfolio Manager (rule-priority rows 4-5),
    and 'Concentration drift; valuation stretched' is the verbatim shape of
    the reason behind the 2026-05-04 AMZN double-trim."""
    from src.pipeline import _HARD_TRIGGER_KEYWORDS

    for banned in ("concentration", "drift trim", "downgrade", "target_breach"):
        assert banned not in _HARD_TRIGGER_KEYWORDS


# ===========================================================================
# 3.4 — exits go through AI Risk
# ===========================================================================

def _review_with(action="SELL", symbol="AAA", reason="thesis_invalid triggered"):
    from src.models import PositionAction, PositionReasoningChain, PositionReview

    return PositionReview(
        reasoning_chain=PositionReasoningChain(
            macro_continuity_check="stable", thesis_progress_check="broken",
            thesis_integrity_check="invalidation hit", winners_discipline_check="n/a",
            session_disposition_check="midday", execution_rationale="exit",
        ),
        actions=[PositionAction(action=action, symbol=symbol, reason=reason)],
        overall_assessment="one exit", risk_level="moderate",
    )


def _verdict(approved: bool, reasoning="because"):
    from src.models import RiskReasoningChain, RiskVerdict

    return RiskVerdict(
        approved=approved,
        reasoning_chain=RiskReasoningChain(
            rr_audit="n/a", signal_fidelity="ok", correlation_check="ok",
            event_risk="none", sizing_sanity="ok", overall="ok",
        ),
        reasoning=reasoning,
    )


def _risk_pipeline(verdict=None, raises=False):
    pipeline = _pipeline()
    pipeline.risk_manager = MagicMock()
    if raises:
        pipeline.risk_manager.review.side_effect = RuntimeError("provider down")
    else:
        pipeline.risk_manager.review.return_value = (verdict, MagicMock(
            user_message="u", raw_text="r", model="m", tokens_used=1,
            input_tokens=1, output_tokens=1, cost_usd=0.0,
        ))
    pipeline._build_portfolio_heat = MagicMock(return_value=None)
    return pipeline


def test_ai_risk_can_veto_an_exit():
    pipeline = _risk_pipeline(_verdict(False, "thesis is not actually broken"))
    vetoed, verdict = pipeline._risk_review_exits(
        _review_with(), [_position("AAA")], run_id="r1", total_value=100_000.0,
    )
    assert vetoed == {"AAA"}
    assert verdict is not None and verdict.approved is False


def test_ai_risk_approval_lets_the_exit_through():
    pipeline = _risk_pipeline(_verdict(True))
    vetoed, verdict = pipeline._risk_review_exits(
        _review_with(), [_position("AAA")], run_id="r1", total_value=100_000.0,
    )
    assert vetoed == set()
    assert verdict is not None and verdict.approved is True


def test_ai_risk_failure_fails_OPEN_for_exits():
    """Owner-ratified asymmetry (2026-08-27). The entry path fails CLOSED with
    zero orders; the exit path must not, because failing closed on an exit
    means a thesis-invalidated position cannot be closed while a language
    model is unavailable. The deterministic gates have already run."""
    pipeline = _risk_pipeline(verdict=None)
    vetoed, verdict = pipeline._risk_review_exits(
        _review_with(), [_position("AAA")], run_id="r1", total_value=100_000.0,
    )
    assert vetoed == set()
    assert verdict is None


def test_ai_risk_exception_also_fails_open():
    pipeline = _risk_pipeline(raises=True)
    vetoed, _ = pipeline._risk_review_exits(
        _review_with(), [_position("AAA")], run_id="r1", total_value=100_000.0,
    )
    assert vetoed == set()


def test_no_exits_means_no_paid_risk_call():
    """A HOLD-only review must not buy a Risk Manager call."""
    pipeline = _risk_pipeline(_verdict(True))
    vetoed, verdict = pipeline._risk_review_exits(
        _review_with(action="HOLD", reason="on track"),
        [_position("AAA")], run_id="r1", total_value=100_000.0,
    )
    assert vetoed == set()
    assert verdict is None
    pipeline.risk_manager.review.assert_not_called()


def test_an_exit_for_a_symbol_not_held_is_not_sent_to_risk():
    pipeline = _risk_pipeline(_verdict(True))
    vetoed, verdict = pipeline._risk_review_exits(
        _review_with(symbol="ZZZ"), [_position("AAA")],
        run_id="r1", total_value=100_000.0,
    )
    assert verdict is None
    pipeline.risk_manager.review.assert_not_called()


def test_executor_drops_a_symbol_vetoed_by_ai_risk():
    pipeline = _pipeline()
    orders = pipeline._midday_execute_llm_actions(
        positions=[_position("AAA")], review=_review_with(), run_id="r1",
        risk_vetoed_symbols={"AAA"},
    )
    assert orders == []
    pipeline.broker.submit_order.assert_not_called()


# ===========================================================================
# 3.6 — an adverse move inside one ATR of entry is noise, not thesis failure
# ===========================================================================
#
# 2026-09-04 audit fix #1: the band used to be a FLAT 1.0x ATR regardless of
# how long the position had been held. It now widens with sqrt(days_held),
# the exact convention `src/data/levels.py::derive_structural_target` already
# uses for target projection (`ATR * sqrt(sessions)`), on the same
# random-walk basis — expected dispersion grows with sqrt(time), not
# linearly. `days_held` floors at 1 session, so day-zero/day-one behaviour
# above is UNCHANGED.

def test_noise_band_widens_with_sqrt_days_held():
    from src.risk.exit_guard import noise_band_atr

    assert noise_band_atr(None) == 1.0     # missing -> floors at 1 session
    assert noise_band_atr(0) == 1.0        # day zero -> floors at 1 session
    assert noise_band_atr(1) == 1.0
    assert noise_band_atr(4) == pytest.approx(2.0)    # 1.0 * sqrt(4)
    assert noise_band_atr(10) == pytest.approx(3.1623, abs=1e-4)  # 1.0*sqrt(10)


def test_same_raw_adverse_move_treated_differently_by_days_held():
    """A position held 1 day and one held 10 days, hit by the SAME raw
    1.2xATR adverse move, must now be judged differently: on day 1 that move
    is a real break (bigger than the day-one 1.0xATR band); by day 10 the
    same absolute move is within the position's own, much wider, still-
    plausible noise band (1.0xATR * sqrt(10) = 3.16xATR)."""
    from src.risk.exit_guard import adverse_move_is_noise

    entry, atr = 100.0, 2.0
    adverse_move = 1.2 * atr  # 2.4 -> 1.2xATR adverse, same for both cases
    current = entry - adverse_move

    day_1_result = adverse_move_is_noise(entry, current, atr, days_held=1)
    day_10_result = adverse_move_is_noise(entry, current, atr, days_held=10)

    assert day_1_result is False   # 1.2xATR clears the day-one 1.0xATR band
    assert day_10_result is True   # but sits well inside the day-ten band
    assert day_1_result != day_10_result


def test_no_days_held_argument_reproduces_the_old_flat_band_exactly():
    """Backward compatibility: every pre-existing call site that doesn't pass
    `days_held` (or passes it as None) must see EXACTLY the old flat 1.0xATR
    behaviour — sqrt(1) == 1 — so this fix changes nothing for a day-zero
    position like the OKLO case it was already tuned against."""
    from src.risk.exit_guard import adverse_move_is_noise

    assert adverse_move_is_noise(42.59, 41.51, atr=1.6) is True
    assert adverse_move_is_noise(42.59, 41.51, atr=1.6, days_held=None) is True
    assert adverse_move_is_noise(42.59, 41.51, atr=1.6, days_held=1) is True



def test_noise_band_uses_trading_sessions_not_calendar_days_over_a_weekend():
    """2026-09-04 audit follow-up: a position bought Friday 2026-08-28 and
    reviewed Monday 2026-08-31 has 3 CALENDAR days_held but only 1 real
    TRADING SESSION behind it — the market was closed all weekend, so no
    price action happened on days 2-3. Under the bug, `_build_position_facts`
    fed the raw calendar count into the sqrt() scaling, giving a band of
    1.0xATR * sqrt(3) =~ 1.73xATR — nearly double what the position's actual
    one session of price history justifies (1.0xATR * sqrt(1) = 1.0xATR).

    A 1.2xATR adverse move sits INSIDE the buggy 1.73xATR band (wrongly
    treated as noise, blocking a legitimate exit) but OUTSIDE the correct
    1.0xATR session-based band (correctly treated as a real break). This
    test pins the correct, session-based answer — it fails under the old
    calendar-day scaling and passes once `sessions_held` (weekend-aware,
    see `trading_calendar.trading_sessions_held`) is what feeds the sqrt().
    """
    from datetime import date as _date
    from src.risk.exit_guard import adverse_move_is_noise

    pipeline = _pipeline()
    monday = _date(2026, 8, 31)
    friday = _date(2026, 8, 28)
    buy_row = {
        "stop_loss": 90.0, "take_profit": 140.0,
        "timestamp": f"{friday.isoformat()} 14:00:00",
        "expected_horizon_sessions": 15, "setup_type": "range",
    }
    pipeline.db.get_symbol_last_buy.return_value = buy_row
    position = _position("AAA", avg_entry=100.0, current_price=97.6)  # 1.2xATR adverse (ATR=2.0)

    with patch("src.pipeline.et_today", return_value=monday):
        facts = pipeline._build_position_facts(
            positions=[position], morning_trades=[], total_value=100_000.0,
        )["AAA"]

    # Sanity: calendar days_held really is 3 (Fri->Mon), the trap the old
    # code fell into — but sessions_held (what the noise band must use) is 1.
    assert facts["days_held"] == 3
    assert facts["sessions_held"] == 1

    # The band computed from the CORRECT (session) count must NOT treat a
    # 1.2xATR move as noise...
    pipeline._atr_for_symbol = MagicMock(return_value=2.0)
    correct_result = adverse_move_is_noise(
        100.0, 97.6, 2.0, days_held=facts["sessions_held"],
    )
    assert correct_result is False

    # ...whereas the OLD, buggy calendar-day count would have wrongly called
    # it noise — pinning exactly what regressed.
    buggy_result = adverse_move_is_noise(
        100.0, 97.6, 2.0, days_held=facts["days_held"],
    )
    assert buggy_result is True
    assert correct_result != buggy_result

    # End-to-end: the executor, wired through position_facts, must let this
    # exit proceed rather than block it as noise.
    orders = pipeline._midday_execute_llm_actions(
        positions=[position],
        review=_review_with(
            symbol="AAA", reason="thesis_invalid triggered — lost the level",
        ),
        run_id="r1",
        position_facts={"AAA": facts},
    )
    status = pipeline.db.record_intraday_evaluation.call_args.kwargs.get("status") \
        if pipeline.db.record_intraday_evaluation.call_args else None
    assert status != "exit_blocked_inside_atr_noise_band"


def test_the_oklo_case_is_blocked():
    """OKLO was bought at 42.59 and sold at 41.51 on 2026-08-26 — a 2.5% loss,
    0.67 ATR, on day zero. The position was never given one day's normal range
    to breathe."""
    from src.risk.exit_guard import adverse_move_is_noise

    assert adverse_move_is_noise(42.59, 41.51, atr=1.6) is True


def test_a_real_break_beyond_one_atr_is_not_noise():
    from src.risk.exit_guard import adverse_move_is_noise

    assert adverse_move_is_noise(42.59, 38.00, atr=1.6) is False


def test_a_winning_position_is_not_this_guards_business():
    from src.risk.exit_guard import adverse_move_is_noise

    assert adverse_move_is_noise(100.0, 120.0, atr=2.0) is False
    assert adverse_move_is_noise(100.0, 100.0, atr=2.0) is False


def test_missing_atr_never_manufactures_a_block():
    """This guard stops premature exits; it must never strand a position the
    reviewer has real reason to leave."""
    from src.risk.exit_guard import adverse_move_is_noise

    assert adverse_move_is_noise(100.0, 99.0, atr=None) is False
    assert adverse_move_is_noise(100.0, 99.0, atr=0.0) is False
    assert adverse_move_is_noise(float("nan"), 99.0, atr=2.0) is False


def test_external_information_bypasses_the_noise_band():
    """An earnings miss is an earnings miss whether the stock has moved 0.2
    ATR or 3 ATR. Waiting for price confirmation before acting on information
    sells the bottom instead of the top."""
    from src.risk.exit_guard import cites_external_information

    for reason in (
        "bearish earnings, revenue missed",
        "adverse news: FDA rejection",
        "sector shock hit the whole group",
        "macro regime flip to risk-off",
        "stopped out at the broker",
    ):
        assert cites_external_information(reason) is True, reason

    for reason in (
        "thesis_invalid triggered: closed below MA50",
        "thesis broken on the chart",
    ):
        assert cites_external_information(reason) is False, reason


def test_executor_blocks_a_price_derived_exit_inside_the_noise_band():
    pipeline = _pipeline()
    pipeline._atr_for_symbol = MagicMock(return_value=1.6)
    orders = pipeline._midday_execute_llm_actions(
        positions=[_position("OKLO", qty=25, avg_entry=42.59, current_price=41.51)],
        review=_review_with(
            symbol="OKLO", reason="thesis_invalid triggered — lost the level",
        ),
        run_id="r1",
    )
    assert orders == []
    status = pipeline.db.record_intraday_evaluation.call_args.kwargs["status"]
    assert status == "exit_blocked_inside_atr_noise_band"


def test_executor_widens_the_noise_band_for_an_aged_position():
    """End-to-end version of `test_same_raw_adverse_move_treated_differently_by_days_held`
    through `_midday_execute_llm_actions`: a position held 10 sessions, hit by
    a 1.2xATR adverse move, now gets BLOCKED as noise (band = 1.0xATR *
    sqrt(10) = 3.16xATR) — under the old flat 1.0xATR band this same move
    would have cleared it and gone through untouched."""
    pipeline = _pipeline()
    pipeline._atr_for_symbol = MagicMock(return_value=2.0)
    position = _position("AAA", avg_entry=100.0, current_price=97.6)  # 1.2xATR adverse
    facts = _facts(pipeline, position, _buy_row(days_ago=10))
    assert facts["days_held"] == 10

    orders = pipeline._midday_execute_llm_actions(
        positions=[position],
        review=_review_with(
            symbol="AAA", reason="thesis_invalid triggered — lost the level",
        ),
        run_id="r1",
        position_facts={"AAA": facts},
    )
    assert orders == []
    status = pipeline.db.record_intraday_evaluation.call_args.kwargs["status"]
    assert status == "exit_blocked_inside_atr_noise_band"


def test_executor_allows_an_external_information_exit_inside_the_noise_band():
    """Same tiny move, different kind of reason — must go through."""
    pipeline = _pipeline()
    pipeline._atr_for_symbol = MagicMock(return_value=1.6)
    pipeline._midday_execute_llm_actions(
        positions=[_position("OKLO", qty=25, avg_entry=42.59, current_price=41.51)],
        review=_review_with(
            symbol="OKLO", reason="bearish earnings — revenue missed by 8%",
        ),
        run_id="r1",
    )
    # The claim under test is that the NOISE BAND did not stop it. Whatever
    # happens further down the executor is another test's business.
    blocked = [
        call.kwargs.get("status")
        for call in pipeline.db.record_intraday_evaluation.call_args_list
    ]
    assert "exit_blocked_inside_atr_noise_band" not in blocked


# ===========================================================================
# 3.7 — deterministic trailing runs before the LLM is asked
# ===========================================================================

def test_deterministic_trail_places_a_broker_order():
    pipeline = _pipeline()
    pipeline.db.get_symbol_last_buy.return_value = {
        "setup_type": "breakout", "take_profit": None,
        "timestamp": "2026-08-01 14:00:00",
    }
    pipeline.broker.get_current_stop_price.return_value = 95.0
    pipeline._atr_for_symbol = MagicMock(return_value=2.0)

    class _B:
        def __init__(self, hi, lo, d):
            self.high, self.low, self.date = hi, lo, d

    lows = [110, 108, 106, 100, 106, 108, 110,
            118, 116, 114, 110, 114, 116, 118, 125]
    pipeline.market = MagicMock()
    pipeline.market.get_ohlcv.return_value = [
        _B(lo + 2, lo, "2026-08-10") for lo in lows
    ]
    pipeline.broker.replace_stop_loss.return_value = {"id": "o1"}

    orders = pipeline._apply_deterministic_trails(
        [_position("AAA", qty=10, avg_entry=100.0, current_price=125.0)],
        run_id="r1",
    )
    assert len(orders) == 1
    pipeline.broker.replace_stop_loss.assert_called_once()
    assert pipeline.broker.replace_stop_loss.call_args[0][1] == 110.0


def test_deterministic_trail_keeps_the_old_stop_when_the_broker_call_fails():
    """A failed replace must never leave the position less protected than it
    was."""
    pipeline = _pipeline()
    pipeline.db.get_symbol_last_buy.return_value = {
        "setup_type": "breakout", "take_profit": None,
        "timestamp": "2026-08-01 14:00:00",
    }
    pipeline.broker.get_current_stop_price.return_value = 95.0
    pipeline._atr_for_symbol = MagicMock(return_value=2.0)
    pipeline.broker.replace_stop_loss.side_effect = RuntimeError("broker down")

    class _B:
        def __init__(self, hi, lo, d):
            self.high, self.low, self.date = hi, lo, d

    lows = [110, 108, 106, 100, 106, 108, 110,
            118, 116, 114, 110, 114, 116, 118, 125]
    pipeline.market = MagicMock()
    pipeline.market.get_ohlcv.return_value = [
        _B(lo + 2, lo, "2026-08-10") for lo in lows
    ]
    orders = pipeline._apply_deterministic_trails(
        [_position("AAA", qty=10, avg_entry=100.0, current_price=125.0)],
        run_id="r1",
    )
    assert orders == []


def test_deterministic_trail_skips_a_position_with_no_recorded_buy():
    pipeline = _pipeline()
    pipeline.db.get_symbol_last_buy.return_value = None
    pipeline.market = MagicMock()
    orders = pipeline._apply_deterministic_trails(
        [_position("AAA")], run_id="r1",
    )
    assert orders == []
    pipeline.broker.replace_stop_loss.assert_not_called()
