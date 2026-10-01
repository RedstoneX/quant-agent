"""Board item 219 — the pruning pass is reported to the owner, every session.

A silent pass is indistinguishable from a pass that never ran; that is the
whole defect. These tests are the specification for the four things the
report must always say.
"""
from src.rotation import (
    ROTATION_FULL_NOTHING_BETTER,
    ROTATION_ROOM_AVAILABLE,
    ROTATION_TELEMETRY_UNAVAILABLE,
    pruning_pass_lines,
)


def _record(**over) -> dict:
    record = {
        "outcome": ROTATION_ROOM_AVAILABLE,
        "held_examined": "AAA,BBB,CCC",
        "held_examined_count": 3,
        "held_below_entry_bar": "",
        "held_below_entry_bar_count": 0,
        "ranked_margin_enabled": False,
        "execute_enabled": True,
    }
    record.update(over)
    return record


def test_the_pass_says_it_ran_and_over_how_many_holdings():
    text = " ".join(pruning_pass_lines(_record()))
    assert "ran" in text
    assert "all 3 holdings" in text
    assert "AAA" in text and "BBB" in text and "CCC" in text


def test_the_kept_holdings_are_named_as_considered_and_kept():
    text = " ".join(pruning_pass_lines(_record()))
    assert "Considered and kept" in text
    assert "AAA, BBB, CCC" in text


def test_a_cut_is_reported_on_its_conviction_reason_not_on_pnl():
    lines = pruning_pass_lines(_record(
        outcome=ROTATION_FULL_NOTHING_BETTER,
        tier="ineligible_hold",
        held_symbol="BBB",
        held_reasons="rating fell below the entry bar",
        held_below_entry_bar="BBB",
        held_below_entry_bar_count=1,
    ))
    text = " ".join(lines)
    assert "BBB" in text
    assert "rating fell below the entry bar" in text
    assert "conviction reason" in text
    # The ruling: pruning is conviction-based, never P&L-based.
    assert "loss" not in text.lower().replace("profit-or-loss", "")
    # And the kept names must still be named, separately from the cut one.
    assert "Considered and kept: AAA, CCC" in text


def test_every_session_states_that_the_score_margin_tier_is_off():
    text = " ".join(pruning_pass_lines(_record()))
    assert "score-margin" in text
    assert "OFF" in text


def test_the_report_tells_the_truth_if_the_second_tier_is_ever_turned_on():
    text = " ".join(pruning_pass_lines(_record(ranked_margin_enabled=True)))
    assert "switched on" in text
    assert "OFF" not in text


def test_no_pruning_block_when_the_pass_did_not_run():
    """Claiming an examined count for a session that never compared
    anything would be the untrue owner line this item exists to remove."""
    assert pruning_pass_lines(
        _record(outcome=ROTATION_TELEMETRY_UNAVAILABLE)
    ) == []


def test_an_empty_book_says_so_rather_than_claiming_a_count():
    text = " ".join(pruning_pass_lines(
        _record(held_examined="", held_examined_count=0)
    ))
    assert "no holdings to examine" in text
    assert "score-margin" in text
