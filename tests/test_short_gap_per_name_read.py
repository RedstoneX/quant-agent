"""Board item 186 — a short's gap haircut is READ, never chosen.

A short's sized risk-per-share is the distance to its stop PLUS that name's
own gap-inclusive volatility: Wilder's ATR, whose true range is
max(H-L, |H-Cprev|, |L-Cprev|) and therefore contains the overnight gap by
construction. There is no window length to pick and no outlier statistic.
With no read the name is REFUSED — the flat x1.5 it replaced is disowned,
so there is nothing to fall back onto.
"""
from types import SimpleNamespace

import pytest

from src.data.technical import ATR_PERIOD, atr_series
from src.portfolio_constructor import (
    STOP_REFUSAL_NO_GAP_VOLATILITY_READ,
    ConstructorConfig,
    PortfolioConstructor,
)


def _bar(o: float, h: float, lo: float, c: float) -> SimpleNamespace:
    return SimpleNamespace(open=o, high=h, low=lo, close=c)


def _flat_bars(n: int = 60, span: float = 1.0):
    return [_bar(100, 100 + span, 100 - span, 100) for _ in range(n)]


def test_the_read_is_the_desks_own_atr_off_that_names_bars():
    bars = _flat_bars()
    c = PortfolioConstructor(ConstructorConfig(), bars_fn=lambda s: bars)
    expected = float(atr_series(bars)[-1])
    assert c._short_gap_volatility("XYZ") == pytest.approx(expected)
    sized, mult, read = c._short_risk_per_share("XYZ", 100.0, 10.0)
    assert read == pytest.approx(expected)
    assert sized == pytest.approx(10.0 + expected)
    assert mult == pytest.approx((10.0 + expected) / 10.0)


def test_an_overnight_gap_is_inside_the_read_without_being_hunted_for():
    """A name that gaps reads WIDER than the same name that did not."""
    quiet = _flat_bars()
    gapped = _flat_bars()
    # one session opens 8 away from the prior close and closes back: the
    # H-L range is unchanged, only |H-Cprev| moves.
    gapped[-1] = _bar(108, 108, 100, 100)
    c = PortfolioConstructor(ConstructorConfig(), bars_fn=lambda s: quiet)
    g = PortfolioConstructor(ConstructorConfig(), bars_fn=lambda s: gapped)
    assert g._short_gap_volatility("G") > c._short_gap_volatility("Q")


def test_the_multiple_is_always_strictly_above_one():
    """The schema's declared floor holds STRUCTURALLY, not by clamping.

    A never-gapped name used to read exactly 1.0 and size a short like a
    long, while `RiskConfig.short_gap_risk_multiple` next to it declared
    `gt=1.0`. A positive ATR cannot produce 1.0.
    """
    c = PortfolioConstructor(ConstructorConfig(), bars_fn=lambda s: _flat_bars())
    _, mult, _ = c._short_risk_per_share("FLAT", 100.0, 10.0)
    assert mult > 1.0


def test_no_bars_is_a_refusal_not_a_fallback_number():
    for bars_fn in (None, lambda s: [], lambda s: _flat_bars(ATR_PERIOD - 1)):
        c = PortfolioConstructor(ConstructorConfig(), bars_fn=bars_fn)
        assert c._short_gap_volatility("NODATA") is None
        assert c._short_risk_per_share("NODATA", 100.0, 10.0) is None


def test_a_provider_error_is_a_refusal_too():
    def boom(symbol):
        raise RuntimeError("provider down")

    c = PortfolioConstructor(ConstructorConfig(), bars_fn=boom)
    assert c._short_risk_per_share("DOWN", 100.0, 10.0) is None


def test_the_constructor_holds_no_haircut_dial_any_more():
    assert not hasattr(ConstructorConfig(), "short_gap_risk_multiple")


def test_the_refusal_code_exists_and_is_named():
    assert STOP_REFUSAL_NO_GAP_VOLATILITY_READ == "no_gap_inclusive_volatility_read"


def test_the_read_is_fetched_once_per_symbol_per_call():
    calls = []

    def counting(symbol):
        calls.append(symbol)
        return _flat_bars()

    c = PortfolioConstructor(ConstructorConfig(), bars_fn=counting)
    c._short_risk_per_share("X", 100.0, 10.0)
    c._short_risk_per_share("X", 100.0, 5.0)
    assert calls == ["X"]
