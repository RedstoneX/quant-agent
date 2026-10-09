"""Boundary witness: the entry/stop resolver builds and runs with no host and no pipeline."""

from __future__ import annotations

import functools
import inspect
from unittest.mock import MagicMock

from src.portfolio_constructor import PortfolioConstructor
from src.portfolio_constructor.entry_stop.resolver import EntryStopResolver
from src.portfolio_constructor.shim_guard import _is_class_shim
from tests.boundary_harness import check_boundary

LIFTED = ("_resolve_entry_and_stop", "real_reward_risk_preview", "_widen_stop_past_noise")


def _build(**overrides):
    kwargs = {n: MagicMock(name=n) for n in inspect.signature(EntryStopResolver).parameters}
    kwargs["widen_stop_past_noise"] = None
    kwargs.update(overrides)
    return EntryStopResolver(**kwargs)


def test_resolver_passes_boundary_check():
    verdict = check_boundary("src.portfolio_constructor.entry_stop.resolver")
    assert verdict.passed, verdict.failures


def test_resolver_builds_from_stubs_alone():
    resolver = _build()
    assert all(callable(getattr(resolver, n)) for n in LIFTED)


def test_host_shims_are_recognised_through_partial():
    for name in LIFTED:
        shim = getattr(PortfolioConstructor, name)
        assert _is_class_shim(shim, name, PortfolioConstructor)
        assert _is_class_shim(functools.partial(shim, MagicMock()), name, PortfolioConstructor)
        assert _is_class_shim(functools.partial(functools.partial(shim)), name, PortfolioConstructor)
    assert not _is_class_shim(lambda *a, **k: None, LIFTED[0], PortfolioConstructor)


def test_shim_does_not_overwrite_resolver_method_with_itself():
    host = PortfolioConstructor()
    resolver = host._entry_stop_resolver()
    assert resolver._widen_stop_past_noise.__func__ is EntryStopResolver._widen_stop_past_noise


def test_non_shim_override_is_what_the_body_sees():
    host = PortfolioConstructor()
    host._widen_stop_past_noise = MagicMock(return_value=1.23)
    assert host._entry_stop_resolver()._widen_stop_past_noise() == 1.23
