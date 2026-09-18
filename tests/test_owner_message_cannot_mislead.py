"""Guards for the owner-facing defects under `docs/WORK.md` item 89 that
can mislead a trading decision — not the thirteen clarity ones.

The owner has a standing preference against test-writing. These exist
anyway, and only these, because each one pins a message that could have
moved real money: a plan discarded with no notification, a header that
disagreed with the list beneath it, an entry reason reported as missing
while it sat in the records, and a rule cited by a number whose text said
the opposite.

Every test here drives the real formatter over a real SQLite file. No
broker is involved; where one would be, the check is on `isinstance`, not
on truthiness, because ~58 tests in this suite drive a MagicMock broker
and a MagicMock is always truthy.
"""

import re
import json
import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo

from src import trader_feed
from tests.test_trader_feed import (
    _agent_log,
    _evidence,
    _make_db,
    _pin_clock,
    _trade,
)

_ET = ZoneInfo("America/New_York")
# Deliberately not intra_check's hourly-checkpoint minute and not 9:30 —
# see `tests/test_trader_feed.py`'s `_QUIET_TICK_TIME` for why every
# "sends nothing" assertion must pin the clock. Do not unfreeze this.
_QUIET_TICK_TIME = datetime(2026, 9, 17, 10, 45, tzinfo=_ET)
_DECISION_TIME = datetime(2026, 9, 17, 9, 40, tzinfo=_ET)


def _pipeline_event(db, run_id, symbol, payload):
    """One `pipeline_event` evidence row, exactly as
    `src.pipeline_stages._record_pipeline_event` writes it."""
    _evidence(db, run_id, "pipeline", "pipeline_event", payload, symbol=symbol)


def _position(db, symbol, qty=10, entry=100.0, price=110.0):
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO positions"
        "(symbol, qty, avg_entry, current_price, market_value, unrealized_pnl) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (symbol, qty, entry, price, qty * price, qty * (price - entry)),
    )
    conn.commit()
    conn.close()


# --- defect 6: a whole trade plan discarded, and no message sent at all ---


def test_a_plan_the_constructor_dropped_is_reported_not_silently_lost(
    tmp_path, monkeypatch,
):
    """Item 89 defect 6. The constructor ended a plan on size alone before
    any order existed. It left a durable per-symbol row and reached NO
    message: no trade row, no execution skip, no risk verdict, so the
    session read "orders: 0" with nothing to explain it and the owner
    could not know a decision had been made."""
    db = _make_db(tmp_path, monkeypatch)
    _pin_clock(monkeypatch, _DECISION_TIME)
    run = "run-silent-drop"
    _agent_log(db, run, "portfolio_manager", "1 target")
    _evidence(
        db, run, "portfolio_manager", "proposed_order",
        {"action": "BUY", "symbol": "NVDA", "allocation_pct": 0.2},
        symbol="NVDA",
    )
    _pipeline_event(db, run, "NVDA", {
        "stage": "deterministic_gate",
        "outcome": "blocked",
        "reason": "constructor_refused",
        "refusal": "delta_below_min_trade_weight",
        "detail": (
            "the desk decided to open this but the position it asked for "
            "was 0.20% of the account, and the desk does not place a new "
            "trade smaller than 0.50% of the account."
        ),
    })

    msg = trader_feed.format_session_result(
        "morning", {"status": "no_trades", "run_id": run, "orders": []}, 12.0,
    )

    assert "BLOCKED / FAILED" in msg
    assert "NVDA" in msg
    assert "Stopped by the desk before an order was placed" in msg
    assert "0.20% of the account" in msg
    # The internal refusal code is data, not language.
    assert "delta_below_min_trade_weight" not in msg


def test_a_dropped_plan_with_no_recorded_reason_says_so_and_invents_nothing(
    tmp_path, monkeypatch,
):
    db = _make_db(tmp_path, monkeypatch)
    _pin_clock(monkeypatch, _DECISION_TIME)
    run = "run-drop-no-reason"
    _agent_log(db, run, "portfolio_manager", "1 target")
    _pipeline_event(db, run, "AMD", {
        "stage": "deterministic_gate",
        "outcome": "blocked",
        "reason": "constructor_dropped",
        "detail": "",
    })

    msg = trader_feed.format_session_result(
        "morning", {"status": "no_trades", "run_id": run, "orders": []}, 9.0,
    )

    assert "did not record why" in msg


def test_a_data_fault_is_not_reported_as_a_dropped_plan(tmp_path, monkeypatch):
    """`unmeasurable` is not a judgement on a trade — it is an input the
    desk failed to obtain, and it pages the owner down its own path. It
    must not appear as though the desk decided something."""
    db = _make_db(tmp_path, monkeypatch)
    _pin_clock(monkeypatch, _DECISION_TIME)
    run = "run-fault"
    _agent_log(db, run, "portfolio_manager", "1 target")
    _pipeline_event(db, run, "TSLA", {
        "stage": "deterministic_gate",
        "outcome": "unmeasurable",
        "reason": "data_fault",
        "fault": "no_atr",
        "detail": "no volatility reading available",
    })

    msg = trader_feed.format_session_result(
        "morning", {"status": "no_trades", "run_id": run, "orders": []}, 9.0,
    )

    assert "Stopped by the desk before an order was placed" not in msg


def test_a_dropped_plan_does_not_make_a_quiet_half_hour_tick_speak(
    tmp_path, monkeypatch,
):
    """The owner's silence rule is ratified: one message an hour for
    oversight, and a quiet half-hour tick sends NOTHING. A constructor
    refusal may only add lines to a message that was already going out —
    if it causes one, the fix is worse than the defect it closes."""
    db = _make_db(tmp_path, monkeypatch)
    _pin_clock(monkeypatch, _QUIET_TICK_TIME)
    run = "run-quiet"
    _pipeline_event(db, run, "NVDA", {
        "stage": "deterministic_gate",
        "outcome": "blocked",
        "reason": "constructor_refused",
        "refusal": "delta_below_min_trade_weight",
        "detail": "below the minimum trade size",
    })

    msg = trader_feed.format_session_result(
        "intra_check",
        {
            "status": "ok", "run_id": run,
            "intraday_scan": {"status": "no_opportunity", "run_id": run},
        },
        3.0,
    )

    assert msg is None


# --- defect 5: raw internal tokens printed to the owner word for word ---


def test_an_order_that_never_filled_is_explained_in_words_not_a_status_token(
    tmp_path, monkeypatch,
):
    db = _make_db(tmp_path, monkeypatch)
    _pin_clock(monkeypatch, _DECISION_TIME)
    run = "run-stalled"
    _agent_log(db, run, "portfolio_manager", "1 order")
    _trade(db, run, "AMD", "BUY", qty=3, price=550.0, status="canceled")

    msg = trader_feed.format_session_result(
        "morning", {"status": "no_trades", "run_id": run, "orders": []}, 11.0,
    )

    assert "the order was cancelled before anything filled" in msg
    assert "status: canceled" not in msg


def test_an_unrecognised_status_is_described_never_pasted_or_guessed_at():
    """Inventing a friendly meaning for a token nobody mapped would be
    inventing a trading fact. Saying there is no plain wording for it is
    the honest answer."""
    text = trader_feed._order_end_plain("some_new_alpaca_state")
    assert "some_new_alpaca_state" not in text
    assert "no plain wording" in text


def test_the_underlying_fault_text_is_kept_but_labelled_as_machine_output():
    text = trader_feed._machine_detail("KeyError: 'targets'")
    assert "KeyError: 'targets'" in text
    assert "Machine fault text" in text


# --- defect 2: a header computed independently of the list beneath it ---


def test_the_review_header_count_matches_the_positions_it_lists(
    tmp_path, monkeypatch,
):
    """Item 89 defect 2. `run_position_review` reports `positions` as the
    count from the START of the session, before its own exits ran and
    before the closing broker re-sync; the HELD block reads the book
    AFTER all of it. On any session that sold something the header
    asserted one number over a list naming a different set."""
    db = _make_db(tmp_path, monkeypatch)
    _pin_clock(monkeypatch, _DECISION_TIME)
    run = "run-review"
    _agent_log(db, run, "position_reviewer", "reviewed")
    for symbol in ("AAPL", "MSFT"):
        _position(db, symbol)

    msg = trader_feed.format_session_result(
        "midday",
        {
            "status": "reviewed", "run_id": run, "orders": [],
            # Three at the start of the session; one was sold.
            "positions": 3,
            "review": {"risk_level": "moderate", "actions": []},
        },
        20.0,
    )

    held_block = msg.split("HELD (")[1]
    assert held_block.startswith("2)")
    assert "2 position(s) held now" in msg
    # The pre-session number is not discarded; it is explained.
    assert "3 at the start of this session" in msg
    assert "3 position(s)" not in msg


def test_the_review_header_says_nothing_extra_when_nothing_changed(
    tmp_path, monkeypatch,
):
    db = _make_db(tmp_path, monkeypatch)
    _pin_clock(monkeypatch, _DECISION_TIME)
    run = "run-review-quiet"
    _agent_log(db, run, "position_reviewer", "reviewed")
    _position(db, "AAPL")

    msg = trader_feed.format_session_result(
        "midday",
        {
            "status": "reviewed", "run_id": run, "orders": [],
            "positions": 1,
            "review": {"risk_level": "low", "actions": []},
        },
        20.0,
    )

    assert "1 position(s) held now" in msg
    assert "at the start of this session" not in msg


# --- defect 4: a rule cited by number whose text says the opposite ---


def test_no_spec_section_number_is_cited_in_the_refusal_the_owner_reads():
    """Item 89 defect 4. The agreement-net refusal told the owner "§9.4
    refuses a net at or below zero". `docs/QAMC_REMEDIATION_SPEC.md` §9.4
    is about agreement EARNING SIZE, states no refusal, and the sizing
    half it does state was retired on 2026-09-14. A section number in
    owner-facing prose is a promise about a separate document that
    nothing checks, so the rule is now stated rather than cited."""
    import inspect

    from src.portfolio_constructor import PortfolioConstructor

    source = inspect.getsource(PortfolioConstructor._plan_risk_targets)
    refusal_strings = [
        line for line in source.splitlines()
        if "net at or below" in line or "net out in favour" in line
    ]
    assert refusal_strings, "the agreement-net refusal text moved; re-point this guard"
    for line in refusal_strings:
        assert "§9.4" not in line
        assert "§" not in line


def test_the_dropped_plan_sentence_names_no_internal_bookkeeping():
    import inspect

    from src.portfolio_constructor import PortfolioConstructor

    source = inspect.getsource(PortfolioConstructor._construct_orders_impl)
    assert "No existing position to record as a HOLD" not in source


# --- defect 3: an entry reason reported as unavailable while it is on file ---


def test_the_entry_reason_for_an_overnight_position_is_read_from_its_entry_row(
    tmp_path, monkeypatch,
):
    """Item 89 defect 3 (and item 104's eighth prompt defect — one root
    cause). The reviewer's only source for an entry thesis was a lookup
    bounded to TODAY, so every position held overnight rendered
    "unavailable". The reason was never missing; it was never searched
    for."""
    from src.agents.position_reviewer import PositionReviewerAgent
    from src.models import Position

    position = Position(
        symbol="RSG", qty=12, avg_entry=240.0, current_price=252.0,
        market_value=3024.0, unrealized_pnl=144.0, sector="Industrials",
    )
    agent = PositionReviewerAgent.__new__(PositionReviewerAgent)
    prompt = PositionReviewerAgent.build_user_message(
        agent,
        positions=[position],
        macro_summary={},
        cash_balance=1000.0,
        total_value=10000.0,
        # Bounded to today, and this position was not opened today.
        morning_trades=[],
        entry_context={
            "RSG": {
                "symbol": "RSG", "action": "BUY",
                "reasoning": "Cascade Investment open-market purchase",
            },
        },
    )

    assert "Cascade Investment open-market purchase" in prompt
    assert "position opened before today" not in prompt


def test_a_genuinely_blank_entry_reason_is_reported_as_not_recorded(
    tmp_path, monkeypatch,
):
    from src.agents.position_reviewer import PositionReviewerAgent
    from src.models import Position

    position = Position(
        symbol="RSG", qty=12, avg_entry=240.0, current_price=252.0,
        market_value=3024.0, unrealized_pnl=144.0, sector="Industrials",
    )
    agent = PositionReviewerAgent.__new__(PositionReviewerAgent)
    prompt = PositionReviewerAgent.build_user_message(
        agent,
        positions=[position],
        macro_summary={},
        cash_balance=1000.0,
        total_value=10000.0,
        morning_trades=[],
        entry_context={"RSG": {"symbol": "RSG", "action": "BUY", "reasoning": ""}},
    )

    assert "not recorded on this position's entry row" in prompt
    assert "do not invent one" in prompt


def test_the_entry_lookup_the_reviewer_is_given_is_not_bounded_to_today():
    """The fix is the LOOKUP, not the wording. `get_symbol_last_buy` is
    the existing date-unrestricted query — the same one the evening
    thesis-health context and the cockpit's "why do we hold this"
    endpoint already use — so no second lookup was written."""
    import inspect

    from src.storage.db import Database

    source = inspect.getsource(Database.get_symbol_last_buy)
    assert "today_only" not in source
    assert "_et_day_utc_bounds" not in source


def test_the_reviewer_reads_levels_from_live_data_not_from_the_entry_row():
    """Scope guard. The entry row also carries the stop and the target
    PINNED at entry; the take-profit became revisable on 2026-09-18, so
    letting a pinned level win over the live one would change what the
    reviewer DECIDES, not what the owner is told. `entry_context` must
    answer only "why is this held"."""
    import inspect

    from src.agents.position_reviewer import PositionReviewerAgent

    source = inspect.getsource(PositionReviewerAgent.build_user_message)
    assert 'entry_row.get("reasoning")' in source
    assert 'entry_row.get("stop_loss")' not in source
    assert 'entry_row.get("take_profit")' not in source


# ===========================================================================
# The pre-market filings message — owner review of the live 08:03 ET copy,
# 18 September 2026. "analyzed: 1  confirmed: 1  failed: 0" told him
# nothing; he wants WHICH company and WHAT was found. And a failure must
# say who, what, why, when and where, plus what it means for him.
# ===========================================================================

def _earnings(**over):
    base = {
        "status": "preprocessed", "run_id": "run-ep",
        "analyzed": 1, "confirmed": 1, "failed": 0,
        "filings": [], "failures": [],
    }
    base.update(over)
    return base


def test_filings_message_names_the_company_and_what_was_found():
    from src.trader_feed import format_session_result

    msg = format_session_result("earnings_preprocess", _earnings(filings=[{
        "symbol": "NVDA", "form_type": "10-Q", "filing_date": "2026-09-17",
        "sentiment": "bearish", "takeaway": "Gross margin fell for a third quarter.",
        "confirmed": True,
    }]), 185.0)
    assert msg is not None
    assert "NVDA" in msg
    assert "quarterly report" in msg and "filed 17 September 2026" in msg
    assert "bad news for the shares" in msg
    assert "Gross margin fell for a third quarter." in msg
    # None of the things the owner rejected.
    assert "analyzed:" not in msg and "confirmed:" not in msg
    assert "run-ep" not in msg and "run_id" not in msg
    assert "provider request" not in msg
    assert "$0.0000" not in msg
    assert "10-Q" not in msg  # the form code itself is jargon
    # No 24-hour clock, and the timezone is kept.
    assert " ET" in msg
    assert not re.search(r"\b(1[3-9]|2[0-3]):[0-5][0-9]\b", msg)


def test_a_failed_filing_says_who_what_why_when_and_what_it_means():
    from src.trader_feed import format_session_result

    msg = format_session_result("earnings_preprocess", _earnings(
        analyzed=0, confirmed=0, failed=1,
        failures=[{"symbol": "F", "form_type": "10-K",
                   "filing_date": "2026-09-16", "reason": None,
                   "error": "JSONDecodeError: Expecting value"}],
    ), 200.0)
    assert msg is not None
    assert "F" in msg                                   # who
    assert "annual report" in msg                       # what it was
    assert "What was being attempted" in msg            # what
    assert "Why it failed" in msg                       # why
    assert "16 September 2026" in msg                   # when
    assert "What this means for you" in msg
    assert "nothing is unprotected and nothing needs doing" in msg
    # The raw fault is kept but LABELLED, never addressed to him as prose.
    assert "Machine fault text, kept for the record" in msg
    assert "JSONDecodeError" in msg


def test_a_failure_with_no_recorded_reason_says_so_rather_than_inventing():
    from src.trader_feed import format_session_result

    msg = format_session_result("earnings_preprocess", _earnings(
        analyzed=0, confirmed=0, failed=1,
        failures=[{"symbol": "F", "form_type": "10-K",
                   "filing_date": "2026-09-16", "reason": None, "error": None}],
    ), 200.0)
    assert msg is not None
    assert "recorded no reason at all" in msg
    assert "gap in what it writes down" in msg


def test_a_filing_with_no_recorded_verdict_does_not_get_one_invented():
    from src.trader_feed import format_session_result

    msg = format_session_result("earnings_preprocess", _earnings(filings=[{
        "symbol": "NVDA", "form_type": "10-Q", "filing_date": "2026-09-17",
        "sentiment": None, "takeaway": None, "confirmed": True,
    }]), 185.0)
    assert msg is not None
    assert "recorded no view either way" in msg
    for word in ("good news", "bad news", "neither good nor bad"):
        assert word not in msg


def test_a_whole_batch_failure_still_names_the_companies():
    """`analysis_error` is a base-only status and used to render
    "analyzed: 0  confirmed: 0  failed: 0", naming nobody — exactly the
    case the owner complained about, at the moment it matters most."""
    from src.trader_feed import format_session_result

    msg = format_session_result("earnings_preprocess", {
        "status": "analysis_error", "run_id": "r", "error": "RateLimitError",
        "failures": [{"symbol": "F", "form_type": "10-K",
                      "filing_date": "2026-09-16", "reason": None,
                      "error": "RateLimitError: 429"}],
    }, 20.0)
    assert msg is not None
    assert "F" in msg and "annual report" in msg
    assert "analyzed:" not in msg


def test_filings_message_stays_silent_when_there_is_nothing_to_report():
    """The silence rule is absolute: most pre-market days have no fresh
    filing, and a row of zeros is not a reason to speak."""
    from src.trader_feed import format_session_result

    for status in ("nothing_new", "fetch_error", "market_holiday"):
        assert format_session_result(
            "earnings_preprocess", {"status": status, "run_id": "r"}, 5.0,
        ) is None


def test_filings_message_admits_when_the_desk_recorded_no_names():
    """The counts say something ran and the per-name record is absent. Say
    THAT, rather than falling back on the counts he rejected."""
    from src.trader_feed import format_session_result

    msg = format_session_result("earnings_preprocess", _earnings(), 185.0)
    assert msg is not None
    assert "did not record which companies" in msg
    assert "analyzed:" not in msg


def test_a_session_crash_says_what_it_was_doing_and_what_it_means():
    from src.trader_feed import format_session_result

    msg = format_session_result(
        "earnings_preprocess", None, 4.0, error=ValueError("bad filing text"),
    )
    assert msg is not None
    assert "Pre-market filings did not finish" in msg
    assert "What it was doing:" in msg
    assert "Why it stopped:" in msg
    assert "What this means for you:" in msg
    assert "Machine fault text, kept for the record" in msg
    # It does not guess a company it never recorded.
    assert "did not record which companies it had reached" in msg
