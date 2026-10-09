"""A stock's stop width comes from that stock's own behaviour -- its ATR -- only.

Owner ruling 2026-10-04: an unbacked stop is 2.5 ATR. Owner mandate
2026-10-09: market mood is one weighted input elsewhere, never a stop-width
scaler. These pin that the regime scalers (risk-off / transitional /
risk-on) and the range-setup scaler are gone, and that the widest stop the
desk can place is the plain ATR width.
"""

from __future__ import annotations

import inspect
from types import SimpleNamespace

import pytest

from src.portfolio_constructor import (
    ConstructorConfig,
    PortfolioConstructor,
    widest_reachable_stop_atr_multiple,
)
from src.portfolio_constructor.stop_width import stop_atr_multiple

RULED_MULTIPLE = 2.5  # owner ruling 2026-10-04


def test_width_is_identical_across_market_regimes_for_the_same_atr() -> None:
    """No regime can reach the width: the function takes no regime at all,
    and the config carries no regime scaler to apply."""
    params = list(inspect.signature(stop_atr_multiple).parameters)
    assert params == ["cfg"], params
    assert not hasattr(ConstructorConfig(), "stop_atr_regime_scale")
    atr = 2.0
    widths = {tape: stop_atr_multiple(ConstructorConfig()) * atr for tape in ("risk-off", "transitional", "risk-on")}
    assert set(widths.values()) == {RULED_MULTIPLE * atr}


def test_range_setups_are_no_longer_narrowed() -> None:
    assert not hasattr(ConstructorConfig(), "stop_atr_setup_scale")
    constructor = PortfolioConstructor()
    assert constructor._stop_atr_multiple() == pytest.approx(RULED_MULTIPLE)
    # The setup label on an analysis has nothing to act on.
    assert stop_atr_multiple(SimpleNamespace(min_stop_atr_multiple=RULED_MULTIPLE)) == pytest.approx(RULED_MULTIPLE)


def test_widest_stop_equals_the_atr_width() -> None:
    assert widest_reachable_stop_atr_multiple() == pytest.approx(RULED_MULTIPLE)
    assert widest_reachable_stop_atr_multiple(ConstructorConfig().min_stop_atr_multiple) == pytest.approx(
        ConstructorConfig().min_stop_atr_multiple
    )
