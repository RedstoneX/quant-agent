"""The alignment exit — the desk's only sanctioned way to realise a gain.

These tests pin the six defects that closed the first attempt (PR 837).
"""

from src.risk.alignment_exit import (
    CODE_NO_ATR,
    CODE_NO_MARK,
    CODE_NO_SWING,
    ChartMark,
    check_alignment_exit,
    simple_moving_average,
    thesis_ma_period,
)
from src.risk.alignment_exit import _sessions_since_mark_lost


def test_the_made_up_give_back_is_gone() -> None:
    """2026-10-09: the 3.0 ATR give-back was never measured; the trigger is
    now a confirmed swing break (`tests/test_alignment_exit_higher_low.py`)."""
    import src.risk.alignment_exit as ae

    assert not hasattr(ae, "ALIGNMENT_GIVE_BACK_ATR_MULTIPLE")


def test_a_mark_given_up_far_beyond_three_atr_no_longer_sells_on_its_own() -> None:
    """Under the deleted rule this sold (10 ATR under the MA20). With no
    bars there is no swing to break, so it holds and says why."""
    v = check_alignment_exit(
        thesis_invalid_if="close below the MA20",
        closes=[100.0] * 25 + [80.0],
        atr=2.0,
    )
    assert v.status == "HOLD" and v.code == CODE_NO_SWING
    assert v.band_atrs is None
    assert "no swing reference" in v.reason


def test_parsed_period_and_thesis_text_are_recorded_on_every_verdict() -> None:
    """DEFECTS 3 and 4. The prose is model-written and unversioned, so the
    period that decided the sale must be pinned with the verdict — and a
    durable code must exist even where the chart cannot be read."""
    v = check_alignment_exit(
        thesis_invalid_if="thesis fails on a close below the MA50",
        closes=[100.0] * 60 + [50.0],
        atr=1.0,
    )
    assert v.thesis_ma_period == 50
    assert "MA50" in v.thesis_text
    unreadable = check_alignment_exit(
        thesis_invalid_if="fundamentals deteriorate",
        closes=[100.0],
        atr=1.0,
    )
    assert unreadable.status == "UNPARSEABLE"
    assert unreadable.code == CODE_NO_MARK
    assert unreadable.thesis_text == "fundamentals deteriorate"
    no_atr = check_alignment_exit(
        thesis_invalid_if="close below the MA20",
        closes=[100.0] * 25,
        atr=None,
    )
    assert no_atr.code == CODE_NO_ATR


def test_sessions_count_uses_the_average_as_it_stood_that_session() -> None:
    """DEFECT 6. A steadily falling series: every historical close sits far
    ABOVE today's MA20, so comparing history against TODAY's average counts
    them all as 'already lost'. Against the average as it stood at the time,
    only the genuinely-below sessions count."""
    closes = [float(200 - i) for i in range(60)]  # 200 down to 141
    mark = ChartMark(simple_moving_average(closes, 20), "SMA20")
    n = _sessions_since_mark_lost(closes, mark, (20, "SMA"), is_short=False)
    assert n < len(closes)


def test_a_broken_structural_mark_alone_no_longer_sells() -> None:
    """The marks are recorded, not decisive: a confirmed-broken level with
    no swing reference holds."""
    v = check_alignment_exit(
        thesis_invalid_if=None,
        closes=[100.0] * 5 + [80.0],
        atr=2.0,
        broken_structural_level=95.0,
    )
    assert v.status == "HOLD" and v.code == CODE_NO_SWING
    assert any("structural" in m.source for m in v.marks)


def test_helpers() -> None:
    assert simple_moving_average([1.0, 2.0, 3.0], 3) == 2.0
    assert simple_moving_average([1.0], 3) is None
    assert thesis_ma_period(None) is None
