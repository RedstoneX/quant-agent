"""Witness: each lifted event-calendar module stands alone, and the old import path still works."""

import dataclasses
import importlib
import types

import pytest

import src.data.event_calendar as package
from src.data.event_calendar import (
    earnings,
    fomc,
    fomc_parse,
    fomc_provider,
    macro,
    macro_cache,
    macro_fetch,
    macro_provider,
    macro_types,
    rendering,
)
from tests.boundary_harness import check_boundary

MODULES = (
    "macro",
    "macro_cache",
    "macro_types",
    "macro_fetch",
    "macro_provider",
    "fomc",
    "fomc_parse",
    "fomc_provider",
    "earnings",
    "rendering",
)


@pytest.mark.parametrize("name", MODULES)
def test_part_passes_the_boundary_harness(name):
    """Clauses 2-5 must hold outright; clause 1 may only flag value types.

    The harness wants a hand-written `__init__` on every class, so it flags
    `@dataclass` records and exception types, which get theirs generated. Any
    other class without one still fails here.
    """
    module = f"src.data.event_calendar.{name}"
    verdict = check_boundary(module)
    assert set(verdict.failures) <= {1}, verdict.failures
    part = importlib.import_module(module)
    for message in verdict.failures.get(1, []):
        cls = getattr(part, message.split(":")[0])
        assert dataclasses.is_dataclass(cls) or issubclass(cls, Exception), message


def test_package_reexports_every_public_name_of_each_part():
    for part in (
        macro, macro_cache, macro_types, macro_fetch, macro_provider,
        fomc, fomc_parse, fomc_provider, earnings, rendering,
    ):
        for attr, value in vars(part).items():
            if attr.startswith("__") or attr == "logger" or isinstance(value, types.ModuleType):
                continue
            if getattr(value, "__module__", part.__name__) != part.__name__:
                continue
            assert getattr(package, attr) is value, attr


def test_parts_are_built_without_a_pipeline():
    assert macro_provider.MacroEventCalendarProvider is package.MacroEventCalendarProvider
    assert fomc_provider.FOMCCalendarProvider is package.FOMCCalendarProvider
    assert earnings.fetch_earnings_proximity is package.fetch_earnings_proximity
    assert rendering.format_event_risk_block is package.format_event_risk_block


def test_rendering_with_nothing_known_still_renders_text():
    text = rendering.format_fomc_section(None, None, 14)
    assert isinstance(text, str)
