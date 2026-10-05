"""Stop-resolution test for the backtest engine, split out of test_backtest."""


def test_no_level_below_entry_reads_the_instrument_stop_instead_of_declining():
    """GAP 2 (engine vs live, docs/WORK.md item 54): the engine used to DROP a
    signal with no structural level on the stop side, while live reads the stop
    from the instrument. A dropped signal is a silent sample bias, so the engine
    now hands `None` to the same `_widen_stop_past_noise` the live constructor
    uses. Measured on 8 recorded symbols: 19 of 329 signal days (5.8%) were
    being discarded this way."""
    from src.backtest.engine import _resolve_stop_for_signal
    from src.portfolio_constructor import ConstructorConfig, PortfolioConstructor

    constructor = PortfolioConstructor(ConstructorConfig())
    stop = _resolve_stop_for_signal(
        constructor, symbol="TEST", direction="long", structural_stop=None,
        target=None, atr_14=2.0, setup_type="breakout", ref_entry=100.0,
        signal_bar_low=96.0, signal_bar_high=101.0,
        computed_levels=[], computed_level_touches={}, computed_level_bars={},
    )
    assert stop is not None and 0 < stop < 100.0
