"""Board item 186 — the short-side sizing haircut is read per name.

A short's sized risk-per-share is the distance to its stop PLUS the worst
upward overnight gap that NAME actually printed over the lookback the desk
already fetches. The old flat x1.5 appetite constant survives only as the
fail-closed fallback when no bars are available.
"""
from types import SimpleNamespace

from src.portfolio_constructor import ConstructorConfig, PortfolioConstructor


def _bar(o: float, c: float) -> SimpleNamespace:
    return SimpleNamespace(open=o, high=max(o, c), low=min(o, c), close=c)


def test_worst_upward_gap_is_read_off_that_names_own_bars():
    # closes 100, 100, 100; opens gap +2%, then +5% (the worst), then -1%.
    bars = [_bar(100, 100), _bar(102, 100), _bar(105, 100), _bar(99, 100)]
    c = PortfolioConstructor(ConstructorConfig(), bars_fn=lambda s: bars)
    assert c._short_gap_fraction("XYZ") == 0.05
    # entry 200, stop 210 -> base risk 10; gap adds 0.05 * 200 = 10.
    sized, mult, read = c._short_risk_per_share("XYZ", 200.0, 10.0)
    assert read is True
    assert sized == 20.0
    assert mult == 2.0
    # and it is NOT the configured dial.
    assert mult != c.cfg.short_gap_risk_multiple


def test_a_name_that_never_gapped_up_adds_nothing():
    bars = [_bar(100, 100), _bar(99, 100), _bar(98, 100)]
    c = PortfolioConstructor(ConstructorConfig(), bars_fn=lambda s: bars)
    assert c._short_gap_fraction("FLAT") == 0.0
    sized, mult, read = c._short_risk_per_share("FLAT", 200.0, 10.0)
    assert (sized, mult, read) == (10.0, 1.0, True)


def test_missing_bars_size_exactly_as_before_this_item():
    """Fail closed: no provider at all -> the old flat haircut, unchanged."""
    cfg = ConstructorConfig()
    c = PortfolioConstructor(cfg)  # no bars_fn, as every existing caller does
    assert c._short_gap_fraction("NOBARS") is None
    sized, mult, read = c._short_risk_per_share("NOBARS", 200.0, 10.0)
    assert read is False
    assert mult == cfg.short_gap_risk_multiple
    assert sized == 10.0 * cfg.short_gap_risk_multiple


def test_provider_raising_or_returning_nothing_also_fails_closed():
    cfg = ConstructorConfig()

    def boom(symbol):
        raise RuntimeError("provider down")

    c = PortfolioConstructor(cfg, bars_fn=boom)
    assert c._short_gap_fraction("DOWN") is None
    assert c._short_risk_per_share("DOWN", 200.0, 10.0)[1] == cfg.short_gap_risk_multiple

    empty = PortfolioConstructor(cfg, bars_fn=lambda s: [])
    assert empty._short_gap_fraction("EMPTY") is None
    # a single bar has no overnight transition in it
    one = PortfolioConstructor(cfg, bars_fn=lambda s: [_bar(100, 100)])
    assert one._short_gap_fraction("ONE") is None


def test_the_read_is_fetched_once_per_symbol():
    calls = []

    def counting(symbol):
        calls.append(symbol)
        return [_bar(100, 100), _bar(103, 100)]

    c = PortfolioConstructor(ConstructorConfig(), bars_fn=counting)
    c._short_gap_fraction("DUP")
    c._short_gap_fraction("DUP")
    c._short_risk_per_share("DUP", 100.0, 5.0)
    assert calls == ["DUP"]
