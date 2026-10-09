"""`src.pipeline_prompt_facts_pure` is a boundary: constructed and exercised
without a pipeline (tests/boundary_harness.py clauses 1-5)."""

from src.pipeline_prompt_facts_pure import (
    _actualize_trade_row,
    _build_macro_tech_alignment,
    _valuation_signal_from,
)
from tests.boundary_harness import check_boundary


def test_the_pure_prompt_facts_module_is_a_boundary():
    v = check_boundary("src.pipeline_prompt_facts_pure")
    assert v.passed, v.failures


def test_the_helpers_run_on_plain_arguments():
    assert _valuation_signal_from(None) == "no_data"
    assert _actualize_trade_row({"action": "SELL"})["action"] == "SELL"
    assert _build_macro_tech_alignment(None, {}) == ""
