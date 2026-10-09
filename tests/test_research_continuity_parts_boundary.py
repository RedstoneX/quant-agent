"""Boundary witnesses: the lifted research change detectors build and run with no pipeline behind them.

Every collaborator is an explicit keyword-only constructor argument, so the class is
built from stubs alone (clause 5 of tests/boundary_harness.py). Nothing here names
the pipeline class, imports its module, or patches anything by module path.
"""

from __future__ import annotations

import inspect
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src.research_continuity.change_detectors import ResearchChangeDetectors
from tests.boundary_harness import check_boundary


def _build(cls, **overrides):
    params = inspect.signature(cls).parameters
    kwargs = {name: MagicMock(name=name) for name in params}
    kwargs.update(overrides)
    return cls(**kwargs)


def _detectors(**overrides):
    base = dict(macro_store=None, macro=None, news_provider=None, news_store=None, config=None)
    base.update(overrides)
    return ResearchChangeDetectors(**base)


class _Provider:
    def __init__(self, titles, *, fail=False):
        self.titles = titles
        self.fail = fail
        self.calls = 0

    def fetch_news(self, symbols=None):
        self.calls += 1
        if self.fail:
            raise RuntimeError("wire fetch failed")
        return [SimpleNamespace(title=t, summary="") for t in self.titles], None

    def format_for_prompt(self, items, max_items=50):
        return "\n".join(f"- {i.title}" for i in items[:max_items])


@pytest.mark.parametrize("cls", [ResearchChangeDetectors])
def test_every_lifted_piece_is_constructible_from_stubs(cls):
    _build(cls)
    params = inspect.signature(cls).parameters
    assert all(p.kind is inspect.Parameter.KEYWORD_ONLY for p in params.values())


@pytest.mark.parametrize("module", ["src.research_continuity.change_detectors"])
def test_every_lifted_module_passes_the_boundary_check(module):
    verdict = check_boundary(module)
    assert verdict.passed, verdict.failures


def test_macro_regime_change_is_read_from_the_macro_store_collaborator_only():
    store = SimpleNamespace(
        load_history=lambda days: [
            {"date": "2026-10-02", "regime": "risk-off"},
        ]
    )
    det = _detectors(macro_store=store)
    stored = {"regime": "risk-on", "date": "2026-10-01"}
    assert det._macro_regime_or_print_changed(stored) is True
    # Same regime, newer date: not a change. No regime stored: never a change.
    same = SimpleNamespace(load_history=lambda days: [{"date": "2026-10-02", "regime": "risk-on"}])
    assert _detectors(macro_store=same)._macro_regime_or_print_changed(stored) is False
    assert det._macro_regime_or_print_changed({"date": "2026-10-01"}) is False


def test_a_failing_detector_is_not_a_change():
    def boom(days):
        raise RuntimeError("store down")

    det = _detectors(macro_store=SimpleNamespace(load_history=boom))
    assert det._macro_regime_or_print_changed({"regime": "risk-on", "date": "2026-10-01"}) is False


def test_series_prints_changed_needs_a_recorded_fingerprint_and_a_live_fetch():
    det = _detectors()
    assert det._macro_series_prints_changed({"series_prints": {}}) is False
    assert det._macro_series_prints_changed({"series_prints": {"values": {"X": 1}}}) is False  # no provider


def test_live_prints_restore_the_providers_side_channel():
    provider = SimpleNamespace(
        last_coverage="morning",
        _run_freshness="fresh",
        get_macro_summary=lambda: {"series": {}},
    )
    det = _detectors(macro=provider)
    det._live_macro_series_prints()
    assert provider.last_coverage == "morning"
    assert provider._run_freshness == "fresh"


def test_watched_symbols_merge_universe_report_and_findings():
    config = SimpleNamespace(trading=SimpleNamespace(universe=["aapl", " msft "]))
    ctx = SimpleNamespace(smart_money_findings=[SimpleNamespace(symbol="nvda"), {"symbol": "tsla"}])
    report = {"stock_news": {"amzn": []}}
    det = _detectors(config=config)
    assert det._watched_research_symbols(ctx=ctx, report=report) == ["AAPL", "AMZN", "MSFT", "NVDA", "TSLA"]
    assert _detectors()._watched_research_symbols() == []


def test_news_peek_keeps_only_watched_titles_and_remembers_the_wire():
    config = SimpleNamespace(trading=SimpleNamespace(universe=["AAPL"]), news=SimpleNamespace(max_prompt_items=50))
    provider = _Provider(["AAPL beats", "Unrelated macro chatter"])
    det = _detectors(config=config, news_provider=provider)
    assert det._peek_news_headlines(report=None) == ["AAPL beats"]
    assert det._peeked_news_wire_text() == "- AAPL beats\n- Unrelated macro chatter"
    # A failed fetch leaves nothing to re-ask with.
    failing = _detectors(config=config, news_provider=_Provider(["x"], fail=True))
    assert failing._peek_news_headlines(report=None) == []
    assert failing._peeked_news_wire_text() == ""


def test_newer_material_wire_only_on_an_uncovered_watched_title():
    config = SimpleNamespace(trading=SimpleNamespace(universe=["AAPL"]))
    report = SimpleNamespace(stock_news={}, headlines=[])
    store = SimpleNamespace(load_raw_headlines=lambda: [{"title": "AAPL beats"}])
    covered = _detectors(config=config, news_provider=_Provider(["AAPL beats"]), news_store=store)
    assert covered._news_has_newer_material_wire(report) is False
    fresh = _detectors(config=config, news_provider=_Provider(["AAPL misses"]), news_store=store)
    assert fresh._news_has_newer_material_wire(report) is True
    down = _detectors(config=config, news_provider=_Provider(["AAPL misses"], fail=True), news_store=store)
    assert down._news_has_newer_material_wire(report) is False


def test_peek_items_live_where_the_host_keeps_them():
    slot = {}
    config = SimpleNamespace(trading=SimpleNamespace(universe=["AAPL"]), news=SimpleNamespace(max_prompt_items=50))
    det = _detectors(
        config=config,
        news_provider=_Provider(["AAPL beats"]),
        peek_items_get=lambda: slot.get("items"),
        peek_items_set=lambda items: slot.__setitem__("items", items),
    )
    det._peek_news_headlines(report=None)
    assert [i.title for i in slot["items"]] == ["AAPL beats"]


def test_a_host_override_of_a_lifted_body_is_honoured():
    det = _detectors(
        macro_store=SimpleNamespace(load_history=lambda days: []),
        macro_history_regime_changed=lambda state, stored: True,
    )
    assert det._macro_regime_or_print_changed({"regime": "risk-on"}) is True
