"""Pins the PM prompt's definition of `supports` for exits to the grounding checker."""

from pathlib import Path

from src.agents.portfolio_manager.decision_grounding import _order_is_buy_side
from types import SimpleNamespace

_PROMPT = (Path(__file__).resolve().parents[1] / "config/prompts/portfolio_manager.md").read_text()


def _pos(qty):
    return SimpleNamespace(qty=qty)


def test_prompt_defines_supports_by_order_side():
    assert "What `supports` means" in _PROMPT
    assert "a cover of\na held short, is a buy-side order: only a bullish source `supports` it" in _PROMPT
    assert "A neutral or mixed source supports nothing" in _PROMPT
    assert "mark it\n`context`, never `supports`" in _PROMPT


def test_invented_example_numbers_removed():
    assert "size down 50%" not in _PROMPT
    assert "5-day max hold" not in _PROMPT


def test_definition_matches_checker_side():
    assert _order_is_buy_side("sell", _pos(-5)) is True  # cover of a short
    assert _order_is_buy_side("sell", _pos(5)) is False  # sell of a long
    assert _order_is_buy_side("buy", None) is True
