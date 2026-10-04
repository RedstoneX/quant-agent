"""Boundary witnesses: the de-levering parts under src/delever/ build and run with no pipeline behind them.

Every collaborator is an explicit keyword-only constructor argument, so each part is
built from stubs alone (clause 5 of tests/boundary_harness.py); this file names no owner object.
"""
from __future__ import annotations

import inspect
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src.delever.conviction import DeleverConviction
from src.delever.enforce import DeleverEnforce
from src.delever.forced import DeleverForced
from src.delever.ladder import DeleverLadder
from src.delever.risk_number import _optional_risk_number, _risk_number
from src.delever.trims import DeleverTrims
from tests.boundary_harness import check_boundary

PARTS = [DeleverLadder, DeleverConviction, DeleverEnforce, DeleverForced, DeleverTrims]
MODULES = [f"src.delever.{m}" for m in ("ladder", "conviction", "enforce", "forced", "trims", "risk_number")]


def _build(cls, **overrides):
    params = inspect.signature(cls).parameters
    kwargs = {name: MagicMock(name=name) for name in params}
    kwargs.update(overrides)
    return cls(**kwargs)


@pytest.mark.parametrize("cls", PARTS)
def test_every_delever_part_is_constructible_from_stubs(cls):
    _build(cls)
    params = inspect.signature(cls).parameters
    assert all(p.kind is inspect.Parameter.KEYWORD_ONLY for p in params.values())


@pytest.mark.parametrize("module", MODULES)
def test_every_delever_module_passes_the_boundary_check(module):
    verdict = check_boundary(module)
    assert verdict.passed, verdict.failures



def test_margin_floor_breach_runs_against_a_stub_config():
    """Exercised, not just built: a book deeper into margin than the base cap engineers is a breach."""
    config = SimpleNamespace(risk=SimpleNamespace(maintenance_margin_pct=25.0))
    part = _build(DeleverLadder, config=config)
    ctx = SimpleNamespace(total_value=100_000.0,
                          leverage={"distance_to_forced_liquidation_pct": 5.0, "base_ceiling_x": 2.0})
    assert part._is_margin_floor_breach(ctx) is True
    ctx.leverage["distance_to_forced_liquidation_pct"] = 90.0
    assert part._is_margin_floor_breach(ctx) is False
    assert part._is_margin_floor_breach(SimpleNamespace(total_value=0.0, leverage={})) is False


def test_live_delever_price_reads_the_broker_collaborator():
    """The quote comes from the handed-in broker, never from a pipeline."""
    broker = MagicMock(name="broker")
    broker.get_latest_quote = MagicMock(return_value={})
    part = _build(DeleverLadder, broker=broker)
    out = part._live_delever_price("AAPL", "sell")
    assert isinstance(out, tuple) and len(out) == 2
    broker.get_latest_quote.assert_called_once_with("AAPL")


def test_deferred_discharge_calls_the_handed_in_enforcer_not_its_own():
    """`_enforce_gross_ceiling` is a sibling body handed in live; the trims part never defines it."""
    assert not hasattr(DeleverTrims, "_enforce_gross_ceiling") and hasattr(DeleverEnforce, "_enforce_gross_ceiling")
    seen = []
    part = _build(DeleverTrims, enforce_gross_ceiling=lambda ctx: seen.append(ctx))
    ctx = SimpleNamespace(gross_ceiling_deferred=True)
    part._discharge_deferred_gross_ceiling(ctx)
    assert seen == [ctx]
    part._discharge_deferred_gross_ceiling(SimpleNamespace(gross_ceiling_deferred=False))
    assert seen == [ctx]


def test_deferred_discharge_never_raises_when_the_enforcer_does():
    def boom(ctx):
        raise RuntimeError("enforcer failed")
    part = _build(DeleverTrims, enforce_gross_ceiling=boom)
    part._discharge_deferred_gross_ceiling(SimpleNamespace(gross_ceiling_deferred=True))


def test_forced_part_is_handed_the_sibling_bodies_not_owning_them():
    """`_force_delever` reads `_live_delever_price` and the owner alert; the forced part owns neither."""
    assert not hasattr(DeleverForced, "_live_delever_price") and hasattr(DeleverLadder, "_live_delever_price")
    assert not hasattr(DeleverForced, "_alert_owner_force_delever_incomplete")
    part = _build(DeleverForced, live_delever_price=lambda *a, **k: "HANDED IN")
    assert part._live_delever_price() == "HANDED IN"


def test_risk_number_helpers_stand_alone():
    assert _optional_risk_number(True) is None and _optional_risk_number(2) == 2.0 and _optional_risk_number(-1) is None
    assert _risk_number(None, 5.0) == 5.0 and _risk_number(3, 5.0) == 3.0
