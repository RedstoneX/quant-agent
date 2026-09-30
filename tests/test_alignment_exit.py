"""The alignment exit — ONE reading of the chart, not a three-way vote.

Owner ruling 2026-09-30 plus the same-day correction: structure, volatility
and trend do not each hold a veto; the chart alone can say the move is over.
"""
from src.pipeline import _HARD_TRIGGER_KEYWORDS
from src.risk.alignment_exit import (
    SMA_LADDER, check_alignment_exit, simple_moving_average, thesis_ma_period,
)
from src.risk.exit_trigger import TRIGGER_PHRASES, ExitTrigger

THESIS = "closes below the 20-day moving average"


def _rollover(n_down: int = 20, step: float = 6.0) -> list[float]:
    up = [100.0 + i for i in range(60)]
    return up + [up[-1] - step * (i + 1) for i in range(n_down)]


def test_ladder_has_no_invented_pair():
    assert SMA_LADDER == {20: 50, 50: 200}


def test_sma_is_a_plain_mean_and_refuses_a_short_series():
    assert simple_moving_average([1.0, 2.0, 3.0], 3) == 2.0
    assert simple_moving_average([1.0, 2.0], 3) is None


def test_thesis_ma_period_reads_the_positions_own_thesis():
    assert thesis_ma_period(THESIS) == 20
    assert thesis_ma_period("closes below $42") is None


def test_chart_alone_can_exit_on_averages_with_no_structural_break():
    """NOT a quorum: no structural mark at all, and the read still fires."""
    r = check_alignment_exit(
        thesis_invalid_if=THESIS, closes=_rollover(), atr=1.0,
        broken_structural_level=None,
    )
    assert r.status == "EXIT" and r.exit_cleared
    assert "not because price reached any target" in (r.owner_reason or "")


def test_chart_alone_can_exit_on_structure_with_no_thesis_average():
    """The mirror case: only a structural mark, no thesis-named average."""
    closes = [100.0] * 60 + [80.0]
    r = check_alignment_exit(
        thesis_invalid_if="closes below $99 support", closes=closes, atr=1.0,
        broken_structural_level=99.0,
    )
    assert r.status == "EXIT"
    assert r.last_mark is not None and "structural" in r.last_mark.source


def test_intact_trend_holds():
    r = check_alignment_exit(
        thesis_invalid_if=THESIS, closes=[100.0 + i for i in range(60)],
        atr=1.0, broken_structural_level=None,
    )
    assert r.status == "HOLD" and r.last_mark is None


def test_a_slip_inside_the_names_own_noise_holds():
    """Same chart, a violent name: the identical breach is noise for it."""
    closes = _rollover(n_down=1, step=1.0)
    r = check_alignment_exit(
        thesis_invalid_if=THESIS, closes=closes, atr=500.0,
    )
    assert r.status == "HOLD"


def test_no_mark_at_all_is_unparseable_not_an_exit():
    r = check_alignment_exit(
        thesis_invalid_if="management execution disappoints",
        closes=_rollover(), atr=1.0, broken_structural_level=None,
    )
    assert r.status == "UNPARSEABLE" and not r.exit_cleared
    assert "refusing to invent" in r.reason


def test_no_atr_is_unparseable_not_an_exit():
    r = check_alignment_exit(
        thesis_invalid_if=THESIS, closes=_rollover(), atr=None,
    )
    assert r.status == "UNPARSEABLE" and not r.exit_cleared


def test_short_side_is_mirrored():
    down = [200.0 - i for i in range(60)]
    closes = down + [down[-1] + 6.0 * (i + 1) for i in range(20)]
    r = check_alignment_exit(
        thesis_invalid_if="closes above the 20-day moving average",
        closes=closes, atr=1.0, is_short=True,
    )
    assert r.status == "EXIT"


def test_trigger_phrases_are_accepted_by_the_hard_trigger_gate():
    for phrase in TRIGGER_PHRASES[ExitTrigger.TREND_ALIGNMENT_OVER]:
        assert phrase in _HARD_TRIGGER_KEYWORDS


def test_entry_anchored_blanket_gate_is_gone_from_the_sell_path():
    import inspect

    from src.pipeline import TradingPipeline

    assert "adverse_move_is_noise" not in inspect.getsource(TradingPipeline)
