"""Owner ruling 2026-10-04: a short is sized like a long.

Verbatim: "A short is not riskier than long. 1.5 is a made up number so
throw that out completely. ATR measurements should be the same. Support and
resistance measurements should be the same. A short should be treated the
same as a long, no different math no different behavior."

The short-side gap haircut (`short_gap_risk_multiple`, 1.5) and its one
application site (`gap_adjusted_risk_per_share`) are DELETED, not neutered
to 1.0, so there is no dial left for anyone to re-tune. This test is the
mechanical guard on that: a short and an otherwise identical long must get
the SAME risk budget allocation and the SAME stop distance. Offline — no
network, no broker, no database.
"""

import pytest

from src.risk.constants import risk_budget_allocation_pct

_ENTRY = 100.0
_DISTANCE = 4.0  # stop distance, identical in magnitude both directions
_EQUITY = 50_000.0
_BUDGET = 5.0


def test_short_and_long_get_identical_risk_budget_and_stop_distance():
    """Same geometry, opposite direction, SAME size and SAME stop distance."""
    long_stop = _ENTRY - _DISTANCE  # a long's stop sits BELOW entry
    short_stop = _ENTRY + _DISTANCE  # a short's stop sits ABOVE entry

    # Stop distance parity: the unsigned magnitude the sizer uses.
    assert abs(_ENTRY - long_stop) == abs(_ENTRY - short_stop)

    long_alloc = risk_budget_allocation_pct(
        entry_price=_ENTRY,
        stop_price=long_stop,
        total_value=_EQUITY,
        risk_budget_pct=_BUDGET,
    )
    short_alloc = risk_budget_allocation_pct(
        entry_price=_ENTRY,
        stop_price=short_stop,
        total_value=_EQUITY,
        risk_budget_pct=_BUDGET,
    )
    assert long_alloc is not None and long_alloc > 0
    assert short_alloc == pytest.approx(long_alloc), (
        f"a short must get the same risk budget as an identical long (long {long_alloc}, short {short_alloc})"
    )


def test_no_direction_argument_survives_on_the_sizer():
    """The plumbing is gone, so a neutral dial cannot be reintroduced."""
    import inspect

    params = inspect.signature(risk_budget_allocation_pct).parameters
    assert "is_short" not in params
    assert "short_gap_risk_multiple" not in params


def test_the_deleted_names_are_really_gone():
    """No constant, no config field, no application site anywhere."""
    import src.risk.constants as rc
    from src.config import RiskConfig
    from src.portfolio_constructor.config import ConstructorConfig

    assert not hasattr(rc, "SHORT_GAP_RISK_MULTIPLE_DEFAULT")
    assert not hasattr(rc, "gap_adjusted_risk_per_share")
    assert "short_gap_risk_multiple" not in RiskConfig.model_fields
    assert not hasattr(ConstructorConfig, "short_gap_risk_multiple")
