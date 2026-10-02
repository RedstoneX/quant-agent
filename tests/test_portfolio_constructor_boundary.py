"""Boundary witnesses: the lifted portfolio-constructor pieces build and run with no pipeline behind them.

Every collaborator is an explicit keyword-only constructor argument, so each
class is built from stubs alone (clause 5 of tests/boundary_harness.py).
"""
from __future__ import annotations

import inspect
from unittest.mock import MagicMock

import pytest

from src.portfolio_constructor.order_builders import OrderBuilders
from src.portfolio_constructor.stop_geometry import StopGeometry
from tests.boundary_harness import check_boundary


def _build(cls, **overrides):
    params = inspect.signature(cls).parameters
    kwargs = {name: MagicMock(name=name) for name in params}
    kwargs.update(overrides)
    return cls(**kwargs)


LIFTED = [OrderBuilders, StopGeometry]


@pytest.mark.parametrize("cls", LIFTED)
def test_every_lifted_piece_is_constructible_from_stubs(cls):
    _build(cls)
    params = inspect.signature(cls).parameters
    assert all(p.kind is inspect.Parameter.KEYWORD_ONLY for p in params.values())


@pytest.mark.parametrize("module", [
    "src.portfolio_constructor.order_builders",
    "src.portfolio_constructor.stop_geometry",
])
def test_every_lifted_module_passes_the_boundary_check(module):
    verdict = check_boundary(module)
    assert verdict.passed, verdict.failures


def test_order_builders_calls_collaborators_not_itself():
    derive = MagicMock(name="derive_target")
    ob = _build(OrderBuilders, derive_target=derive)
    assert ob._derive_target is derive


def test_shim_builds_the_object_per_call_from_the_host():
    """A collaborator swapped on the host after construction is what the body sees."""
    from src.portfolio_constructor.orders import _OrderBuildMixin

    class Host(_OrderBuildMixin):
        pass

    host = Host()
    for _, attr in _OrderBuildMixin._ORDER_BUILDER_COLLABORATORS:
        setattr(host, attr, MagicMock(name=attr))
    first = host._order_builders()
    host.cfg = MagicMock(name="swapped_cfg")
    second = host._order_builders()
    assert second.cfg is host.cfg and first.cfg is not second.cfg


def test_no_shim_collaborator_is_itself_a_lifted_method():
    from src.portfolio_constructor.orders import _OrderBuildMixin

    lifted = {n for n, _ in inspect.getmembers(OrderBuilders, inspect.isfunction)} - {"__init__"}
    passed = {attr for _, attr in _OrderBuildMixin._ORDER_BUILDER_COLLABORATORS}
    assert not (lifted & passed), lifted & passed


def test_static_shims_delegate_to_the_lifted_bodies():
    from src.portfolio_constructor import PortfolioConstructor

    for name in ("_hold_decision", "_build_sell", "_build_cover"):
        assert isinstance(inspect.getattr_static(PortfolioConstructor, name), staticmethod)
        assert isinstance(inspect.getattr_static(OrderBuilders, name), staticmethod)


def test_stop_geometry_shim_builds_the_object_per_call_from_the_host():
    """cfg swapped on the host after construction is what the stop bodies see."""
    from src.portfolio_constructor.stops import _StopMixin

    class Host(_StopMixin):
        pass

    host = Host()
    host.cfg = MagicMock(name="cfg")
    first = host._stop_geometry()
    host.cfg = MagicMock(name="swapped_cfg")
    second = host._stop_geometry()
    assert isinstance(second, StopGeometry)
    assert second.cfg is host.cfg and first.cfg is not second.cfg


def test_no_stop_geometry_collaborator_is_itself_a_lifted_method():
    from src.portfolio_constructor.stops import _StopMixin

    lifted = {n for n, _ in inspect.getmembers(StopGeometry, inspect.isfunction)} - {"__init__"}
    passed = {attr for _, attr in _StopMixin._STOP_GEOMETRY_COLLABORATORS}
    assert not (lifted & passed), lifted & passed


def test_stop_shims_delegate_to_the_lifted_bodies():
    from src.portfolio_constructor import PortfolioConstructor

    lifted = {n for n, _ in inspect.getmembers(StopGeometry, inspect.isfunction)} - {"__init__"}
    assert lifted, "nothing lifted"
    for name in lifted:
        shim = inspect.getattr_static(PortfolioConstructor, name)
        assert (shim.__doc__ or "").startswith("Thin shim"), name
        assert inspect.getattr_static(StopGeometry, name) is not shim
