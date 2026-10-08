"""Boundary witnesses: the lifted order-build pieces build and run with no pipeline behind them.

Every collaborator is an explicit keyword-only constructor argument, so each
entry builder is built from stubs alone (clause 5 of tests/boundary_harness.py);
the exit builders take no collaborators at all.
"""
from __future__ import annotations

import inspect
from unittest.mock import MagicMock

import pytest

from src.portfolio_constructor.order_build.exits import ExitOrderBuilders
from src.portfolio_constructor.order_build.long_entry import LongEntryBuilder
from src.portfolio_constructor.order_build.short_entry import ShortEntryBuilder
from tests.boundary_harness import check_boundary


def _build(cls, **overrides):
    params = inspect.signature(cls).parameters
    kwargs = {name: MagicMock(name=name) for name in params}
    kwargs.update(overrides)
    return cls(**kwargs)


LIFTED = [ExitOrderBuilders, LongEntryBuilder, ShortEntryBuilder]
ENTRY = [LongEntryBuilder, ShortEntryBuilder]
MODULES = [
    "src.portfolio_constructor.order_build.exits",
    "src.portfolio_constructor.order_build.long_entry",
    "src.portfolio_constructor.order_build.short_entry",
]


@pytest.mark.parametrize("cls", LIFTED)
def test_every_lifted_piece_is_constructible_from_stubs(cls):
    _build(cls)
    params = inspect.signature(cls).parameters
    assert all(p.kind is inspect.Parameter.KEYWORD_ONLY for p in params.values())


@pytest.mark.parametrize("module", MODULES)
def test_every_lifted_module_passes_the_boundary_check(module):
    verdict = check_boundary(module)
    assert verdict.passed, verdict.failures


def test_each_module_is_under_the_new_file_floor():
    from tests.boundary_harness import _mod_path
    floor = 400  # the new-file cap these lifted modules were built under
    for module in MODULES:
        assert len(_mod_path(module).read_text().splitlines()) <= floor, module


@pytest.mark.parametrize("cls", ENTRY)
def test_entry_builders_call_collaborators_not_themselves(cls):
    derive = MagicMock(name="derive_target")
    ob = _build(cls, derive_target=derive)
    assert ob._derive_target is derive


def test_exit_builders_need_no_collaborators():
    assert not inspect.signature(ExitOrderBuilders).parameters
    for name in ("_hold_decision", "_build_sell", "_build_cover"):
        assert isinstance(inspect.getattr_static(ExitOrderBuilders, name), staticmethod)


@pytest.mark.parametrize("factory, cls", [("_long_entry_builder", LongEntryBuilder), ("_short_entry_builder", ShortEntryBuilder)])
def test_held_part_builds_the_object_per_call_from_the_owner(factory, cls):
    """A collaborator swapped on the owner after construction is what the body sees."""
    from src.portfolio_constructor.assembly import order_builder_collaborators
    from src.portfolio_constructor.orders import _ORDER_BUILDER_COLLABORATORS, OrderBuilders

    class Host:
        pass

    host = Host()
    for _, attr in _ORDER_BUILDER_COLLABORATORS:
        setattr(host, attr, MagicMock(name=attr))
    part = OrderBuilders(collaborators=lambda: order_builder_collaborators(host))
    first = getattr(part, factory)()
    host.cfg = MagicMock(name="swapped_cfg")
    second = getattr(part, factory)()
    assert isinstance(second, cls)
    assert second.cfg is host.cfg and first.cfg is not second.cfg


def test_no_shim_collaborator_is_itself_a_lifted_method():
    from src.portfolio_constructor.orders import _ORDER_BUILDER_COLLABORATORS

    lifted = {n for cls in LIFTED for n, _ in inspect.getmembers(cls, inspect.isfunction)} - {"__init__"}
    passed = {attr for _, attr in _ORDER_BUILDER_COLLABORATORS}
    assert not (lifted & passed), lifted & passed


def test_static_shims_delegate_to_the_lifted_bodies():
    from src.portfolio_constructor import PortfolioConstructor

    for name in ("_hold_decision", "_build_sell", "_build_cover"):
        assert isinstance(inspect.getattr_static(PortfolioConstructor, name), staticmethod)
        assert (inspect.getattr_static(PortfolioConstructor, name).__func__.__doc__ or "").startswith("Thin shim")


def test_every_lifted_body_has_a_thin_shim_on_the_host():
    from src.portfolio_constructor import PortfolioConstructor

    lifted = {n for cls in LIFTED for n, _ in inspect.getmembers(cls, inspect.isfunction)} - {"__init__"}
    assert lifted == {"_hold_decision", "_build_sell", "_build_cover", "_build_buy", "_build_short"}
    for name in lifted:
        shim = inspect.getattr_static(PortfolioConstructor, name)
        fn = getattr(shim, "__func__", shim)
        assert (fn.__doc__ or "").startswith("Thin shim"), name
