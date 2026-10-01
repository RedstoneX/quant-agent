"""Three of the four helpers that lived only on `TradingPipeline` are usable
WITHOUT building a pipeline (conversion step 8, 2026-10-01).

`_sweeper` stays on the pipeline on purpose: its body is a broad
except-returns-None, and moving it into `src/execution/cash_sweep.py` (its
only honest home, a money module) registers as a NEW silent swallow under
`tests/test_silent_swallow_guard.py`. It moves when that swallow is fixed.

Each test imports the helper from its real home and calls it with plain
collaborators. `src.pipeline` is deliberately never imported here: the point
is that none of these needs the giant object any more.
"""
import sys
import types
from datetime import date

import pytest


@pytest.fixture(autouse=True)
def _pipeline_never_imported():
    before = "src.pipeline" in sys.modules
    yield
    assert ("src.pipeline" in sys.modules) == before, (
        "a helper home pulled in src.pipeline -- the extraction is not real"
    )


def test_atr_for_symbol_needs_no_pipeline():
    from src.data.technical import atr_for_symbol
    from src.models import OHLCV

    class _Market:
        def __init__(self, bars):
            self.bars = bars
        def get_ohlcv(self, symbol, days):
            return self.bars

    bars = [
        OHLCV(date=date(2026, 1, d), open=100.0, high=102.0,
              low=98.0, close=100.0, volume=1_000)
        for d in range(1, 31)
    ]
    atr = atr_for_symbol(_Market(bars), "SYN")
    assert atr is not None and atr > 0
    assert atr_for_symbol(_Market(bars[:10]), "SYN") is None  # < 15 bars
    assert atr_for_symbol(_Market(None), "SYN") is None

    class _Broken:
        def get_ohlcv(self, symbol, days):
            raise RuntimeError("provider down")
    assert atr_for_symbol(_Broken(), "SYN") is None  # never raises


def test_live_constructor_cfg_or_none_needs_no_pipeline():
    from src.risk.constants import live_constructor_cfg_or_none

    assert live_constructor_cfg_or_none(None) is None
    assert live_constructor_cfg_or_none(object()) is None
    cfg = object()
    assert live_constructor_cfg_or_none(types.SimpleNamespace(cfg=cfg)) is cfg


def test_parse_logged_agent_response_needs_no_pipeline():
    from src.agents.logged_response import parse_logged_agent_response

    row = {"full_response": 'Here you go:\n```json\n{"action": "HOLD", "n": 2}\n```',
           "model": "synthetic"}
    assert parse_logged_agent_response(row) == {"action": "HOLD", "n": 2}
    assert parse_logged_agent_response({}) is None
