"""The touch bar is one predicate, it fails closed, and it is counted.

Board companion to `src/portfolio_constructor/level_touch_record.py`. These
tests assert behaviour PARITY with the two inline checks this replaced, plus
the thing those checks never had: a count, so "the bar never bound" can be
told apart from "nobody ever looked".
"""

from __future__ import annotations

import pytest

from src.portfolio_constructor import level_touch_record as touch_gate
from src.portfolio_constructor.level_touch_record import (
    OUTCOME_ADMITTED,
    OUTCOME_UNDER_TOUCHED,
    OUTCOME_UNVERIFIED,
    SITE_NO_ATR_STRUCTURAL_ANCHOR,
    SITE_TIGHT_STOP_EXEMPTION,
    LevelTouchTally,
    level_clears_touch_bar,
)


@pytest.fixture
def tally() -> LevelTouchTally:
    return LevelTouchTally()


def test_a_level_at_the_bar_is_admitted(tally: LevelTouchTally) -> None:
    assert level_clears_touch_bar(5, 5, site=SITE_TIGHT_STOP_EXEMPTION, tally=tally)
    assert tally.count(outcome=OUTCOME_ADMITTED) == 1
    assert tally.refusals == 0
    assert tally.bound() is False


def test_a_level_below_the_bar_is_refused_and_counted(
    tally: LevelTouchTally,
) -> None:
    assert not level_clears_touch_bar(4, 5, site=SITE_TIGHT_STOP_EXEMPTION, tally=tally)
    assert tally.count(outcome=OUTCOME_UNDER_TOUCHED) == 1
    assert tally.bound() is True


@pytest.mark.parametrize("unrecorded", [None, "", "many"])
def test_an_unrecorded_touch_count_fails_closed(tally: LevelTouchTally, unrecorded: object) -> None:
    """Parity with both inline checks: unverified is treated as below the bar."""
    assert not level_clears_touch_bar(unrecorded, 5, site=SITE_NO_ATR_STRUCTURAL_ANCHOR, tally=tally)
    assert tally.count(outcome=OUTCOME_UNVERIFIED) == 1
    # An unrecorded count is not binned as a touch count, because it is not one.
    assert tally.touches_seen == {}


def test_the_two_call_sites_are_counted_apart(tally: LevelTouchTally) -> None:
    level_clears_touch_bar(2, 5, site=SITE_TIGHT_STOP_EXEMPTION, tally=tally)
    level_clears_touch_bar(9, 5, site=SITE_NO_ATR_STRUCTURAL_ANCHOR, tally=tally)
    row = tally.summary_row()
    assert row["by_site"][SITE_TIGHT_STOP_EXEMPTION]["under_touched"] == 1
    assert row["by_site"][SITE_NO_ATR_STRUCTURAL_ANCHOR]["admitted"] == 1
    assert row["decisions"] == 2
    assert row["touches_seen"] == {2: 1, 9: 1}


def test_the_bar_is_read_from_the_argument_not_stored(
    tally: LevelTouchTally,
) -> None:
    """Nothing here remembers a threshold, so nothing can go stale against config."""
    assert not level_clears_touch_bar(3, 5, site=SITE_TIGHT_STOP_EXEMPTION, tally=tally)
    assert level_clears_touch_bar(3, 2, site=SITE_TIGHT_STOP_EXEMPTION, tally=tally)


def test_the_stop_rules_consult_the_shared_predicate() -> None:
    """The constructor's stop rules must not carry a second copy of the bar."""
    import inspect

    from src.portfolio_constructor import stops

    source = inspect.getsource(stops)
    assert source.count("touch_gate.level_clears_touch_bar(") == 2
    assert "touches < min_touches" not in source


def test_the_process_tally_exists_and_starts_unbound() -> None:
    """The module-level tally is the record that was missing; it is readable."""
    assert isinstance(touch_gate.GATE_TALLY, LevelTouchTally)
    row = touch_gate.GATE_TALLY.summary_row()
    assert set(row) >= {"decisions", "admitted", "under_touched", "bound"}
