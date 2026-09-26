"""Board item 74 — a hard trigger is SPENT once the desk has acted on it.

The defect these pin: `_midday_execute_llm_actions` gated a sell-side
action on "does the reason name A trigger", never on "is this the SAME
trigger resting on the SAME record that already cut this name today", so
one piece of news could trim a holding twice in a day. The line drawn is
the RECORD the seat cites — never a cooldown, a window or a score. See
`src/risk/spent_trigger.py`.
"""

from unittest.mock import MagicMock

from src.models import Position, PositionAction, PositionReview
from src.risk.spent_trigger import (
    ACTED_TRIGGER_KIND,
    CODE_TRIGGER_ALREADY_SPENT,
    CODE_TRIGGER_SUPERSEDED,
    CODE_TRIGGER_UNIDENTIFIABLE,
    ActedTrigger,
    acted_trigger_payload,
    evidence_fingerprint,
    format_spent_triggers_block,
    is_spendable,
    keep_executed_acted_triggers,
    parse_acted_triggers,
    spent_trigger_check,
)
from tests.test_pipeline import _mk_midday_pipeline, _review_rc

_EARNINGS_ROW = "Q3 filing posted 2026-09-26: EPS 1.02 vs 1.31 est, guidance cut"


def _acted(symbol="AMZN", trigger="earnings", evidence=_EARNINGS_ROW,
           action="REDUCE", broker_order_id="ord-mid"):
    return ActedTrigger(
        symbol=symbol, trigger=trigger, evidence=evidence,
        fingerprint=evidence_fingerprint(evidence, trigger),
        action=action, run_id="midday-1", broker_order_id=broker_order_id,
    )


# ---------------------------------------------------------------------
# The line itself: same record = spent, different record = new.
# ---------------------------------------------------------------------

def test_the_same_trigger_on_the_same_record_cannot_cut_twice_in_a_day():
    check = spent_trigger_check(
        action="SELL", symbol="AMZN", trigger="earnings",
        evidence=_EARNINGS_ROW, acted_today=[_acted()],
    )
    assert check.verdict == "spent"
    assert check.blocks is True
    assert check.code == CODE_TRIGGER_ALREADY_SPENT


def test_rewrapped_punctuation_and_case_is_still_the_same_record():
    """Identity, not similarity: normalising case/punctuation/whitespace is
    not a threshold, and it stops a trivially re-typed citation counting as
    a new event."""
    check = spent_trigger_check(
        action="REDUCE", symbol="AMZN", trigger="EARNINGS",
        evidence="  q3 FILING posted 2026-09-26 -- eps 1.02 vs 1.31 est; GUIDANCE CUT  ",
        acted_today=[_acted()],
    )
    assert check.verdict == "spent"


def test_a_genuinely_worse_reading_on_a_different_record_still_cuts():
    check = spent_trigger_check(
        action="SELL", symbol="AMZN", trigger="earnings",
        evidence=("8-K filed 2026-09-26 16:05: CFO resigns and the FY guide "
                  "is withdrawn entirely"),
        acted_today=[_acted()],
    )
    assert check.verdict == "new_evidence"
    assert check.blocks is False
    assert check.code == CODE_TRIGGER_SUPERSEDED


def test_a_second_cut_citing_nothing_is_unidentifiable_not_spent():
    """The layer immediately upstream lets every unsubstantiated exit
    through (`dropped=False`) because stranding the desk in a losing
    position is worse than an uncheckable claim passing. Two layers on one
    path must not disagree about the identical input — and the seat could
    otherwise escape by answering `cannot_substantiate`, punishing only the
    seat that named the TRUE trigger tersely."""
    prior = _acted(trigger="adverse_news",
                   evidence="Active News State Change 2026-09-26, AMZN bearish: FTC suit")
    check = spent_trigger_check(
        action="REDUCE", symbol="AMZN", trigger="adverse_news",
        evidence="adverse news", acted_today=[prior],
    )
    assert check.verdict == "unidentifiable"
    assert check.blocks is False
    assert check.code == CODE_TRIGGER_UNIDENTIFIABLE


def test_a_different_trigger_on_an_already_cut_name_is_untouched():
    check = spent_trigger_check(
        action="SELL", symbol="AMZN", trigger="thesis_invalid",
        evidence="thesis_invalid_if: closed below $168 support on 3x volume",
        acted_today=[_acted()],
    )
    assert check.verdict == "not_applicable"


def test_a_different_symbol_is_untouched():
    check = spent_trigger_check(
        action="SELL", symbol="XOM", trigger="earnings",
        evidence=_EARNINGS_ROW, acted_today=[_acted()],
    )
    assert check.verdict == "not_applicable"


def test_a_stop_is_never_spent():
    """A stop firing is a price fact, not a reading of an event — blocking a
    protective exit is the harm on the other side of this item."""
    prior = _acted(trigger="stop_fired", evidence="stop hit at $168.00")
    check = spent_trigger_check(
        action="SELL", symbol="AMZN", trigger="stop_fired",
        evidence="stop hit at $168.00", acted_today=[prior],
    )
    assert check.verdict == "not_applicable"
    assert is_spendable("stop_fired") is False
    assert is_spendable("cannot_substantiate") is False
    assert is_spendable("earnings") is True
    assert is_spendable("adverse_news") is True
    assert is_spendable("thesis_invalid") is True


def test_an_unreadable_record_fails_open_and_says_so():
    check = spent_trigger_check(
        action="SELL", symbol="AMZN", trigger="earnings",
        evidence=_EARNINGS_ROW, acted_today=None,
    )
    assert check.verdict == "uncertain"
    assert check.blocks is False


def test_a_trail_stop_is_not_a_sell_side_action_here():
    check = spent_trigger_check(
        action="TRAIL_STOP", symbol="AMZN", trigger="earnings",
        evidence=_EARNINGS_ROW, acted_today=[_acted()],
    )
    assert check.verdict == "not_applicable"


def test_a_non_spendable_trigger_writes_no_acted_record():
    assert acted_trigger_payload(
        symbol="AMZN", trigger="stop_fired", evidence="stop hit at $168",
        action="SELL", run_id="r",
    ) is None
    assert acted_trigger_payload(
        symbol="AMZN", trigger=None, evidence="whatever", action="SELL",
        run_id="r",
    ) is None
    payload = acted_trigger_payload(
        symbol="amzn", trigger="Adverse-News", evidence=_EARNINGS_ROW,
        action="REDUCE", run_id="r", broker_order_id="ord-1",
    )
    assert payload is not None
    assert payload.symbol == "AMZN" and payload.trigger == "adverse_news"
    assert parse_acted_triggers([payload.to_json()])[0] == payload


def test_the_prompt_block_names_the_spent_record_verbatim():
    block = format_spent_triggers_block([_acted()], {"AMZN"})
    assert _EARNINGS_ROW in block
    assert "earnings" in block
    assert "trigger_already_spent" in block
    # A name the seat is not reviewing is not shown to it.
    assert format_spent_triggers_block([_acted()], {"XOM"}) == ""
    assert format_spent_triggers_block([], {"AMZN"}) == ""


# ---------------------------------------------------------------------
# End to end through the executor.
# ---------------------------------------------------------------------

def _position(symbol="AMZN", price=170.0):
    return Position(
        symbol=symbol, qty=20.0, avg_entry=180.0, current_price=price,
        market_value=price * 20, unrealized_pnl=-200.0,
        unrealized_intraday_pnl=0.0, sector="Consumer Cyclical",
    )


def _review(action="SELL", symbol="AMZN", trigger="earnings",
            evidence=_EARNINGS_ROW,
            reason="bearish earnings: the Q3 filing missed and guidance was cut"):
    return PositionReview(
        reasoning_chain=_review_rc(),
        actions=[PositionAction(
            action=action, symbol=symbol, reason=reason,
            exit_trigger=trigger, trigger_evidence=evidence,
        )],
        overall_assessment="acting on the filing",
        risk_level="high",
    )


def _pipeline_with_acted(position, acted_rows, trades=None):
    pipeline = _mk_midday_pipeline(position)
    pipeline.db.get_acted_exit_triggers_today.return_value = acted_rows
    # By default the earlier cut FILLED — the spent path. Tests that care
    # about a rejected or unfilled earlier order override `trades`.
    pipeline.db.get_trades.return_value = trades if trades is not None else [
        {"symbol": position.symbol, "action": "REDUCE",
         "broker_order_id": "ord-mid", "fill_status": "filled",
         "fill_qty": 10.0},
    ]
    pipeline._record_exit_refusal = MagicMock()
    return pipeline


def test_executor_refuses_the_second_cut_and_records_the_refusal():
    position = _position()
    pipeline = _pipeline_with_acted(position, [_acted().to_json()])
    orders = pipeline._midday_execute_llm_actions(
        positions=[position], review=_review(), run_id="close-1",
        already_trimmed_today={"AMZN"},
    )
    assert orders == []
    pipeline.broker.submit_order.assert_not_called()
    pipeline.db.insert_trade.assert_not_called()
    # Visibly refused, with the reason recorded — never silently dropped.
    kwargs = pipeline._record_exit_refusal.call_args.kwargs
    assert kwargs["code"] == CODE_TRIGGER_ALREADY_SPENT
    assert kwargs["dropped"] is True
    assert kwargs["layer"] == "spent_trigger"
    assert "already cut today" in kwargs["detail"]
    status = pipeline.db.record_intraday_evaluation.call_args.kwargs["status"]
    assert status == "exit_blocked_trigger_already_spent"


def test_executor_still_cuts_on_a_genuinely_new_record_and_records_that():
    position = _position()
    pipeline = _pipeline_with_acted(position, [_acted().to_json()])
    orders = pipeline._midday_execute_llm_actions(
        positions=[position], review=_review(
            evidence="8-K filed 16:05 2026-09-26: FY guide withdrawn, CFO out",
        ),
        run_id="close-1", already_trimmed_today={"AMZN"},
    )
    assert len(orders) == 1
    pipeline.broker.submit_order.assert_called_once()
    kwargs = pipeline._record_exit_refusal.call_args.kwargs
    assert kwargs["code"] == CODE_TRIGGER_SUPERSEDED
    assert kwargs["dropped"] is False


def test_the_first_cut_of_the_day_spends_its_trigger():
    position = _position()
    pipeline = _pipeline_with_acted(position, [])
    orders = pipeline._midday_execute_llm_actions(
        positions=[position], review=_review(), run_id="midday-1",
        already_trimmed_today=set(),
    )
    assert len(orders) == 1
    written = pipeline.db.record_acted_exit_trigger.call_args.kwargs
    assert written["symbol"] == "AMZN"
    assert "earnings" in written["payload_json"]
    assert _EARNINGS_ROW in written["payload_json"]


def test_an_unreadable_acted_record_lets_the_exit_through():
    position = _position()
    pipeline = _pipeline_with_acted(position, None)
    orders = pipeline._midday_execute_llm_actions(
        positions=[position], review=_review(), run_id="close-1",
        already_trimmed_today={"AMZN"},
    )
    assert len(orders) == 1
    pipeline.broker.submit_order.assert_called_once()


# ---------------------------------------------------------------------
# The record survives a round trip through the real table.
# ---------------------------------------------------------------------

def test_acted_triggers_round_trip_through_the_database(tmp_path):
    from src.storage.db import Database
    db = Database(str(tmp_path / "t.db"))
    db.initialize()
    assert db.get_acted_exit_triggers_today() == []
    payload = acted_trigger_payload(
        symbol="AMZN", trigger="earnings", evidence=_EARNINGS_ROW,
        action="REDUCE", run_id="midday-1",
    )
    db.record_acted_exit_trigger(
        run_id="midday-1", payload_json=payload.to_json(), symbol="AMZN",
    )
    rows = db.get_acted_exit_triggers_today()
    assert len(rows) == 1
    assert parse_acted_triggers(rows)[0] == payload
    kind = db.conn.execute(
        "SELECT kind FROM specialist_evidence WHERE symbol = 'AMZN'"
    ).fetchone()[0]
    assert kind == ACTED_TRIGGER_KIND


# ---------------------------------------------------------------------
# Prompt and enforcement must not rot apart.
# ---------------------------------------------------------------------

def test_the_prompt_no_longer_permits_the_repeat():
    from src.agents.position_reviewer import PROMPT_PATH
    text = PROMPT_PATH.read_text(encoding="utf-8")
    assert "A trigger is SPENT once the desk has acted on it" in text
    assert "trigger_already_spent" in text
    assert "trigger_superseded_by_new_evidence" in text
    # Reflow-robust: the guard must not be defeated by re-wrapping or
    # re-wording the same permission back in.
    flat = " ".join(text.split()).lower()
    assert "only when one of these hard triggers fires" not in flat
    assert "a trigger is spent once the desk has acted on it" in flat
    # The enum offered to the seat must not include a trigger the code
    # deleted: `risk_breaker` normalises to nothing and can never be spent.
    assert "risk_breaker" not in text
    # The fill-not-submission rule and the honest limit are both stated.
    assert "actually sold shares spends its trigger" in flat
    assert "trigger_record_unidentifiable" in text


def test_the_reviewer_prompt_renders_the_spent_block():
    from src.agents.position_reviewer import PositionReviewerAgent
    agent = PositionReviewerAgent.__new__(PositionReviewerAgent)
    block = format_spent_triggers_block([_acted()], {"AMZN"})
    message = agent.build_user_message(
        positions=[_position()], macro_summary={}, cash_balance=0.0,
        reserve_balance=0.0, total_value=1000.0, session_type="close",
        already_trimmed_today={"AMZN"}, spent_triggers_block=block,
    )
    assert _EARNINGS_ROW in message


# ---------------------------------------------------------------------
# A submitted-but-unfilled cut must NOT spend the trigger. This is the
# money defect: the reviewer's sell is a limit under a possibly stale
# mark, so the non-fill lands on exactly the bad-news gap-down day this
# layer governs, and a trigger spent by a cut that sold nothing would
# refuse the close cut on a name the desk still holds in full.
# ---------------------------------------------------------------------

def test_only_an_executed_cut_spends_the_trigger():
    acted = [_acted(broker_order_id="ord-mid")]
    assert keep_executed_acted_triggers(
        acted, executed_order_ids={"ord-mid"}) == acted
    # Rejected / cancelled / expired with zero fill — the sibling gate's
    # own posture: the symbol is fair game for re-trying.
    assert keep_executed_acted_triggers(
        acted, executed_order_ids=set()) == []
    # Unverifiable never costs a protective exit.
    assert keep_executed_acted_triggers(
        [_acted(broker_order_id="")], executed_order_ids={"ord-mid"}) == []
    # The caller could not find out at all -> stand the whole check down.
    assert keep_executed_acted_triggers(
        acted, executed_order_ids=None) is None
    assert keep_executed_acted_triggers(
        None, executed_order_ids={"ord-mid"}) is None


def test_executor_still_cuts_when_the_earlier_order_filled_nothing():
    """The midday limit sell was rejected at zero fill. The position was
    never reduced, so the close cut on the same record must go through."""
    position = _position()
    pipeline = _pipeline_with_acted(position, [_acted().to_json()])
    pipeline.db.get_trades.return_value = [
        {"symbol": "AMZN", "action": "REDUCE", "broker_order_id": "ord-mid",
         "fill_status": "rejected", "fill_qty": 0},
    ]
    orders = pipeline._midday_execute_llm_actions(
        positions=[position], review=_review(), run_id="close-1",
        already_trimmed_today=set(),
    )
    assert len(orders) == 1
    pipeline.broker.submit_order.assert_called_once()


def test_executor_refuses_when_the_earlier_order_partially_filled():
    """Shares DID leave the book, so the trigger is spent — the same rule
    the sibling same-day-trim gate applies to a partial fill."""
    position = _position()
    pipeline = _pipeline_with_acted(position, [_acted().to_json()])
    pipeline.db.get_trades.return_value = [
        {"symbol": "AMZN", "action": "REDUCE", "broker_order_id": "ord-mid",
         "fill_status": "canceled", "fill_qty": 4.0},
    ]
    orders = pipeline._midday_execute_llm_actions(
        positions=[position], review=_review(), run_id="close-1",
        already_trimmed_today=set(),
    )
    assert orders == []
    assert (pipeline._record_exit_refusal.call_args.kwargs["code"]
            == CODE_TRIGGER_ALREADY_SPENT)


def test_executor_records_an_unidentifiable_second_cut_and_lets_it_through():
    position = _position()
    pipeline = _pipeline_with_acted(position, [_acted().to_json()])
    pipeline.db.get_trades.return_value = [
        {"symbol": "AMZN", "action": "REDUCE", "broker_order_id": "ord-mid",
         "fill_status": "filled", "fill_qty": 10.0},
    ]
    orders = pipeline._midday_execute_llm_actions(
        positions=[position],
        review=_review(evidence="bearish earnings"),
        run_id="close-1", already_trimmed_today=set(),
    )
    assert len(orders) == 1
    kwargs = pipeline._record_exit_refusal.call_args.kwargs
    assert kwargs["code"] == CODE_TRIGGER_UNIDENTIFIABLE
    assert kwargs["dropped"] is False


def test_the_first_cut_records_the_order_it_was_submitted_as():
    position = _position()
    pipeline = _pipeline_with_acted(position, [])
    pipeline.db.get_trades.return_value = []
    pipeline._midday_execute_llm_actions(
        positions=[position], review=_review(), run_id="midday-1",
        already_trimmed_today=set(),
    )
    payload = pipeline.db.record_acted_exit_trigger.call_args.kwargs["payload_json"]
    assert '"broker_order_id": "ord-1"' in payload


# ---------------------------------------------------------------------
# The known limit, pinned so it is never mistaken for strength.
# ---------------------------------------------------------------------

def test_the_identity_test_cannot_see_the_seat_rewording_its_own_citation():
    """DOCUMENTED LIMIT, not a bug to fix here. The enforcement is only as
    strong as the seat's own consistency in citing a record; closing this
    would cost a similarity threshold, i.e. an invented number. The escape
    hatch and the block are weak in the same proportion, which is the
    honest way round — a seat that genuinely found a new record is never
    trapped. See `src/risk/spent_trigger.py`'s module docstring."""
    check = spent_trigger_check(
        action="SELL", symbol="AMZN", trigger="earnings",
        evidence=_EARNINGS_ROW + " (confirmed)", acted_today=[_acted()],
    )
    assert check.verdict == "new_evidence"
