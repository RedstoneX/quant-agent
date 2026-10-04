"""Boundary witnesses: the lifted cash-park helpers build and run with no pipeline behind them.

The one collaborator is an explicit keyword-only constructor argument, so `CashPark` is
built from a stub alone (clause 5 of tests/boundary_harness.py). The host's `_sweeper`
callable is handed in, never lifted, so the bodies see whatever the host (or a test)
currently binds under that name.
"""
from __future__ import annotations

import inspect
from types import SimpleNamespace
from unittest.mock import MagicMock

from src.cash_park import CashPark
from src.quantities import deployable_cash
from tests.boundary_harness import check_boundary


def _build(**overrides):
    params = inspect.signature(CashPark).parameters
    kwargs = {name: MagicMock(name=name) for name in params}
    kwargs.update(overrides)
    return CashPark(**kwargs)


def test_cash_park_is_constructible_from_stubs_with_keyword_only_collaborators():
    _build()
    params = inspect.signature(CashPark).parameters
    assert all(p.kind is inspect.Parameter.KEYWORD_ONLY for p in params.values())


def test_cash_park_module_passes_the_boundary_check():
    verdict = check_boundary("src.cash_park")
    assert verdict.passed, verdict.failures


def test_deployable_cash_without_a_sweeper_is_raw_cash():
    part = _build(sweeper=lambda: None)
    assert part._compute_deployable_cash(1_000.0, []) == deployable_cash(1_000.0, 0.0)


def test_deployable_cash_adds_the_parked_value_the_handed_in_sweeper_reports():
    sweeper = SimpleNamespace(parked_value=lambda positions: 250.0)
    part = _build(sweeper=lambda: sweeper)
    assert part._compute_deployable_cash(1_000.0, ["book"]) == deployable_cash(1_000.0, 250.0)


def test_deployable_cash_treats_an_unreadable_parked_value_as_zero():
    def _boom(positions):
        raise RuntimeError("sweep state unreadable")
    part = _build(sweeper=lambda: SimpleNamespace(parked_value=_boom))
    assert part._compute_deployable_cash(1_000.0, []) == deployable_cash(1_000.0, 0.0)


def test_news_held_symbols_upper_cases_and_drops_zero_qty_without_a_sweeper():
    part = _build(sweeper=lambda: None)
    positions = [
        SimpleNamespace(symbol=" abc ", qty=3),
        SimpleNamespace(symbol="zero", qty=0),
        SimpleNamespace(symbol="", qty=1),
    ]
    assert part._news_held_symbols(positions) == ["ABC"]


def test_news_held_symbols_uses_only_the_investable_split_from_the_sweeper():
    investable = [SimpleNamespace(symbol="keep", qty=1)]
    sweeper = SimpleNamespace(split_positions=lambda positions: (investable, ["parked"]))
    part = _build(sweeper=lambda: sweeper)
    assert part._news_held_symbols(["anything"]) == ["KEEP"]

