"""Boundary witnesses: the lifted prompt-facts signal helpers build and run with no pipeline behind them.

Every collaborator is an explicit keyword-only constructor argument, so the class is
built from stubs alone (clause 5 of tests/boundary_harness.py).
"""
from __future__ import annotations

import inspect
from unittest.mock import MagicMock

import pytest

from src.prompt_facts.missed_ops_signals import MissedOpsSignals
from tests.boundary_harness import check_boundary


def _build(cls, **overrides):
    params = inspect.signature(cls).parameters
    kwargs = {name: MagicMock(name=name) for name in params}
    kwargs.update(overrides)
    return cls(**kwargs)


LIFTED = [MissedOpsSignals]


@pytest.mark.parametrize("cls", LIFTED)
def test_every_lifted_piece_is_constructible_from_stubs(cls):
    _build(cls)
    params = inspect.signature(cls).parameters
    assert all(p.kind is inspect.Parameter.KEYWORD_ONLY for p in params.values())


@pytest.mark.parametrize("module", ["src.prompt_facts.missed_ops_signals"])
def test_every_lifted_module_passes_the_boundary_check(module):
    verdict = check_boundary(module)
    assert verdict.passed, verdict.failures


def test_held_set_reads_only_the_db_collaborator():
    db = MagicMock(name="db")
    db.get_positions_snapshot = MagicMock(return_value=[])
    sig = _build(MissedOpsSignals, db=db)
    assert sig.db is db and sig._parse_logged_agent_response is not None


def test_macro_sector_map_tolerates_an_empty_macro_store():
    macro_store = MagicMock(name="macro_store")
    macro_store.latest = MagicMock(return_value=None)
    macro_store.get_latest = MagicMock(return_value=None)
    sig = _build(MissedOpsSignals, macro_store=macro_store)
    out = sig._missed_ops_macro_sector_map()
    assert isinstance(out, dict)
