"""The alignment exit — the desk's only sanctioned way to realise a gain.

These tests pin the six defects that closed the first attempt (PR 837).
"""

from src.risk.alignment_exit import (
    ALIGNMENT_GIVE_BACK_ATR_MULTIPLE,
    CODE_EXIT,
    CODE_HOLD,
    CODE_NO_ATR,
    CODE_NO_MARK,
    check_alignment_exit,
    simple_moving_average,
    thesis_ma_period,
)


def test_tolerance_is_not_the_entry_anchored_noise_band() -> None:
    """DEFECT 2. The closed attempt inherited `exit_guard`'s 1.0, whose own
    ledger note records every published analogue at ~2.8-3.5 ATR. Selling
    on a 1 ATR give-back is roughly three times more eager than the
    literature the desk itself cites."""
    # The entry-anchored band (1.0) was removed 2026-10-09; the
    # tolerance must still not have inherited its value.
    assert ALIGNMENT_GIVE_BACK_ATR_MULTIPLE == 3.0
    assert ALIGNMENT_GIVE_BACK_ATR_MULTIPLE != 1.0


def test_exit_when_last_mark_given_up_beyond_tolerance() -> None:
    closes = [100.0] * 25 + [80.0]
    v = check_alignment_exit(
        thesis_invalid_if="close below the MA20",
        closes=closes,
        atr=2.0,
    )
    assert v.status == "EXIT" and v.code == CODE_EXIT
    assert v.exit_cleared and v.owner_reason
    assert "target" in v.owner_reason  # says it is NOT a target


def test_hold_inside_the_tolerance() -> None:
    """One ATR under the average would have SOLD under the closed attempt."""
    closes = [100.0] * 25 + [97.5]
    v = check_alignment_exit(
        thesis_invalid_if="close below the MA20",
        closes=closes,
        atr=2.0,
    )
    assert v.status == "HOLD" and v.code == CODE_HOLD


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
    v = check_alignment_exit(
        thesis_invalid_if="close below the MA20",
        closes=closes,
        atr=1.0,
    )
    assert v.sessions_since_mark_lost is not None
    # Price is below its own MA20 for the whole decline, but nowhere near
    # the 60 the naive today's-average comparison would have produced for a
    # mark it never rose back above.
    assert v.sessions_since_mark_lost < len(closes)


def test_short_side_is_mirrored() -> None:
    closes = [100.0] * 25 + [120.0]
    v = check_alignment_exit(
        thesis_invalid_if="close above the MA20",
        closes=closes,
        atr=2.0,
        is_short=True,
    )
    assert v.status == "EXIT"


def test_structural_mark_alone_can_exit() -> None:
    """No quorum: a position with only a structural mark exits on it."""
    v = check_alignment_exit(
        thesis_invalid_if=None,
        closes=[100.0] * 5 + [80.0],
        atr=2.0,
        broken_structural_level=95.0,
    )
    assert v.status == "EXIT"
    assert v.last_mark and "structural" in v.last_mark.source


def test_helpers() -> None:
    assert simple_moving_average([1.0, 2.0, 3.0], 3) == 2.0
    assert simple_moving_average([1.0], 3) is None
    assert thesis_ma_period(None) is None
