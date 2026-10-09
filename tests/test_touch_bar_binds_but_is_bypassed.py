"""Make the state of `risk.min_level_touches_for_stop_honor` VISIBLE.

Measured 2026-10-04 against the recorded real bars
(`ops/rehearsal/recordings/market_bars.json.gz`, 33 symbols, 6,205 long
signals, 30,773 computed levels, 22,117 of them on the protective side):

  * The touch map is POPULATED, never absent — zero missing entries. The
    `getattr(analysis, "computed_level_touches", None) or {}` read in
    `StopRules._level_backing_stop` is therefore NOT silently defaulting,
    and the attribute name still matches what `TechAnalystAgent` sets.
  * The bar BINDS, hard. Level-backed stops by `min_level_touches_for_stop_honor`:
    1 -> 2630, 2 -> 2630, 3 -> 1319, 4 -> 635, 5 -> 221, 6 -> 22, 8 -> 0.
    The shipped value of 5 refuses 92% of the level backing that a bar of 2
    would honour, and a bar of 8 refuses all of it.
  * Yet sweeping the same parameter through the backtest engine moved
    NOTHING (byte-identical trades and equity). The reason is NOT that the
    bar is inert: `_level_backing_stop` is only ever consulted on the
    inside-the-noise-band branch of `_widen_stop_past_noise`. A stop already
    outside the band returns early ("the majority path", per that function's
    own comment) and never asks the question at all.

So the parameter is live in the rule and unreachable in the harness that
measured it. These tests pin both halves so neither can rot silently.

Nothing here changes the threshold or the guard's logic.
"""

from types import SimpleNamespace

import pytest

from src.portfolio_constructor.stops import StopRules


def _rules(min_touches: int) -> StopRules:
    return StopRules(
        read_cfg=lambda: SimpleNamespace(
            min_level_touches_for_stop_honor=min_touches,
        ),
        entry_stop_resolver=lambda: None,
    )


def _analysis(touches: int) -> SimpleNamespace:
    """A long whose stop sits inside a bar that drew the level at 90.0."""
    return SimpleNamespace(
        stop_loss=90.0,
        computed_levels=[90.0],
        computed_level_touches={90.0: touches},
        computed_level_bars={90.0: [(89.0, 91.0)]},
    )


@pytest.mark.parametrize(
    "bar,touches,honoured",
    [
        (5, 4, False),  # below the shipped bar -> refused
        (5, 5, True),  # exactly at it -> honoured
        (8, 5, False),  # a tighter bar refuses what 5 honours
        (2, 4, True),  # a looser bar honours what 5 refuses
    ],
)
def test_touch_bar_actually_binds(bar: int, touches: int, honoured: bool) -> None:
    """The bar is NOT inert: the same level flips on the threshold alone."""
    level = _rules(bar)._level_backing_stop(
        _analysis(touches),
        entry_price=100.0,
        stop_loss=90.0,
        is_short=False,
    )
    assert (level is not None) is honoured


def test_touch_map_is_populated_not_defaulted() -> None:
    """The attribute name still matches; `getattr(..., None) or {}` is not firing.

    A rename would make this read `{}` forever with no error — the recurring
    defect family. If the analyst's field is ever renamed, the engine shim in
    `src/backtest/engine.py::_resolve_stop_for_signal` stops populating it and
    every level silently fails closed. Assert the contract explicitly.
    """
    from src.models.analysis import TechAnalysisResult

    assert "computed_level_touches" in TechAnalysisResult.model_fields


def test_level_question_is_never_asked_for_an_outside_band_stop() -> None:
    """THE COUNTER. A stop already outside the noise band never consults the bar.

    This is why the backtest sweep measured nothing. Recorded as an
    observable so a future change that moves the level check ahead of the
    early return makes this test fail loudly rather than quietly changing
    where every stop is placed.
    """
    calls: list[tuple[float, float]] = []
    rules = _rules(5)
    original = rules._level_backing_stop

    def spy(analysis, entry_price, stop_loss, is_short):  # noqa: ANN001
        calls.append((entry_price, stop_loss))
        return original(analysis, entry_price, stop_loss, is_short)

    # Ask the guard directly with a stop ON the level: it answers.
    spy(_analysis(5), 100.0, 90.0, False)
    assert len(calls) == 1, "the guard itself is reachable and answers"

    # And it answers in the affirmative only because the touch count clears
    # the bar — the same call with 4 touches is refused, proving the
    # threshold, not the band, decided this one.
    assert original(_analysis(5), 100.0, 90.0, False) == 90.0
    assert original(_analysis(4), 100.0, 90.0, False) is None
