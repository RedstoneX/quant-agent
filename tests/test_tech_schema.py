"""Item 157 (docs/WORK.md; from #538's write-up): the technical seat now
declares a `result_model` (`TechAnalystAnswer`) and sends a strict schema on
both routes. WHAT THIS ACTUALLY BUYS TODAY, honestly stated: pydantic still
validates every row exactly as before (a semantically-invalid row is caught
at construction, same as pre-existing behaviour), and the wrapper lets the
#538 per-row salvage keep working after unwrapping `results`. What it does
NOT yet buy — because it has never been confirmed live (see
tests/test_tech_schema_live.py) — is a provider that refuses to emit a
malformed row IN THE FIRST PLACE via constrained decoding; that half of
item 157 stays open on the board. This file covers the new pieces:

  - `TechAnalystAnswerItem` carries only what the model fills in — the eight
    desk-filled (Python-set) fields from `TechAnalysisResult` are absent.
  - `TechAnalystAnswer` is the wrapper object (`{"results": [...]}`) sent as
    the schema root, since a strict json_schema response format requires an
    object, not the seat's old bare list.
  - `_response_format_for(TechAnalystAnswer)` — verified, not assumed — comes
    out strict=true: excluding the eight desk-filled fields also excludes
    `computed_level_touches`, the one free-form map that used to force
    strict=false on the old (unused) full-model schema.
  - `AgentResult.parse_json_rows(list_field="results")` unwraps the wrapper
    before the existing #538 per-row salvage runs, and still accepts a bare
    list (a legacy stored answer, or a route that ignores the schema) so
    nothing that worked before regresses.
  - The old "one malformed row costs only itself" scenario, replayed under
    the NEW wrapper-object answer shape.
"""
import json
from datetime import date
from unittest.mock import MagicMock, patch

import pytest

from src.agents.base import AgentResult, _response_format_for
from src.agents.tech_analyst import TechAnalystAgent
from src.models import (
    OHLCV, TechAnalysisResult, TechAnalystAnswer, TechAnalystAnswerItem,
    TechnicalIndicators,
)

# The eight fields item 157 / #538's write-up names as desk-filled
# (Python-set after the call, never emitted by the model).
_DESK_FILLED_FIELDS = {
    "atr_14", "computed_levels", "computed_level_touches", "levels_coverage",
    "signal_bar_low", "signal_bar_high", "bars_available", "signal_age_days",
}

_VALID_ITEM = {
    "symbol": "SPY",
    "rating": "buy",
    "conviction": "high",
    "entry_price": 505.0,
    "reference_target": 530.0,
    "stop_loss": 494.0,
    "support_levels": [500.0],
    "resistance_levels": [520.0],
    "setup_type": "range",
    "expected_horizon_sessions": 10,
    "reasoning_chain": {
        "trend": "up", "momentum": "up", "volatility": "calm",
        "volume": "confirming", "support_resistance": "clear",
    },
    "reasoning": "clean setup",
    "thesis_invalid_if": "closes below 494",
}


def _result(text: str) -> AgentResult:
    return AgentResult(raw_text=text, tokens_used=0, model="m")


# --- the model split ---------------------------------------------------------

def test_the_eight_desk_filled_fields_are_not_on_the_model_facing_schema():
    model_fields = set(TechAnalystAnswerItem.model_fields)
    assert not (model_fields & _DESK_FILLED_FIELDS), (
        f"leaked desk-filled fields into the model-facing schema: "
        f"{model_fields & _DESK_FILLED_FIELDS}"
    )
    # Every field TechAnalysisResult carries beyond the model-facing subset
    # must be exactly the eight named ones -- nothing else quietly moved.
    extra = set(TechAnalysisResult.model_fields) - model_fields
    assert extra == _DESK_FILLED_FIELDS


def test_tech_analysis_result_still_builds_from_a_model_answer_plus_desk_fields():
    """TechAnalysisResult is still what the rest of the codebase consumes;
    splitting the schema must not change how a row is constructed."""
    r = TechAnalysisResult(**_VALID_ITEM)
    assert r.symbol == "SPY"
    assert r.atr_14 is None and r.computed_levels == []
    r.atr_14 = 8.5
    r.computed_levels = [494.0, 530.0]
    assert r.atr_14 == 8.5


def test_the_wrapper_object_holds_a_list_of_the_model_facing_item():
    wrapper = TechAnalystAnswer(results=[_VALID_ITEM])
    assert len(wrapper.results) == 1
    assert isinstance(wrapper.results[0], TechAnalystAnswerItem)


def test_the_seat_declares_the_wrapper_as_its_result_model():
    assert TechAnalystAgent.result_model is TechAnalystAnswer


# --- the schema itself: strict, and object-rooted ----------------------------

def test_the_schema_roots_at_an_object_not_a_list():
    schema = TechAnalystAnswer.model_json_schema()
    assert schema.get("type") == "object"
    assert "results" in schema.get("properties", {})


def test_the_schema_is_strict_now_that_the_free_form_map_is_excluded():
    """Measured, not assumed (2026-09-20): removing the eight desk-filled
    fields from the model-facing schema also removes `computed_level_touches`
    — the only free-form map anywhere near this seat's answer — so
    `_response_format_for` no longer needs to fall back to strict=false."""
    response_format = _response_format_for(TechAnalystAnswer)
    assert response_format["json_schema"]["strict"] is True
    schema = response_format["json_schema"]["schema"]
    assert schema["type"] == "object"
    assert schema["additionalProperties"] is False


# --- parse_json_rows: wrapper-aware, backward-compatible ---------------------

def test_parse_json_rows_unwraps_the_results_key():
    text = json.dumps({"results": [{"symbol": "A"}, {"symbol": "B"}]})
    salvage = _result(text).parse_json_rows(list_field="results")
    assert [r["symbol"] for r in salvage.rows] == ["A", "B"]
    assert salvage.malformed == []


def test_parse_json_rows_still_accepts_a_bare_list_with_list_field_set():
    """A route that ignores the schema (or a legacy stored answer) must not
    regress just because the caller now passes list_field."""
    text = json.dumps([{"symbol": "A"}, {"symbol": "B"}])
    salvage = _result(text).parse_json_rows(list_field="results")
    assert [r["symbol"] for r in salvage.rows] == ["A", "B"]


def test_parse_json_rows_without_list_field_is_unchanged():
    """Every other opt-in caller (there are none yet, but the contract
    matters) must see byte-identical behaviour to before this change."""
    text = json.dumps([{"symbol": "A"}])
    assert _result(text).parse_json_rows().rows == [{"symbol": "A"}]


def test_a_dict_answer_missing_the_list_field_falls_back_to_one_row():
    text = json.dumps({"symbol": "A"})
    salvage = _result(text).parse_json_rows(list_field="results")
    assert salvage.rows == [{"symbol": "A"}]


def test_a_sibling_array_after_results_does_not_win_over_the_real_rows():
    """Adversary review, item 157 (2026-09-20): before this test existed, a
    broken answer with ANY array-of-objects appearing after `results` in the
    raw text — here, a stray self-correction fragment — would win outright
    under the old "last array-of-objects anywhere" rule, silently discarding
    the real rows entirely. The scan must prefer the array that is actually
    the VALUE of `results`."""
    text = (
        '{"results": [\n'
        '  {"symbol": "AAA", "reasoning": "fine"},\n'
        '  {"symbol": "BBB", "reasoning": "fine"}\n'
        '],\n'
        '"unrelated_note": "actually let me redo that as"}\n'
        '[{"symbol": "ZZZ-not-a-real-row", "reasoning": "junk"}]'
    )
    salvage = _result(text).parse_json_rows(list_field="results")
    assert [r["symbol"] for r in salvage.rows] == ["AAA", "BBB"]


# --- the old "malformed row needs salvaging" scenario, under the NEW wrapper -

def test_one_broken_row_inside_the_wrapper_still_costs_only_itself():
    """The #538 scenario (one garbled stock must not sink the whole answer)
    replayed with the model actually answering in the new wrapper shape.
    The malformed row is caught by the per-row salvage as before; the schema
    change does not remove that safety net, it adds a stricter one in front
    of it."""
    text = (
        '{"results": [\n'
        '  {"symbol": "AAA", "reasoning": "fine"},\n'
        '  {"symbol": "BBB", "reasoning": "unterminated,\n},\n'
        '  {"symbol": "CCC", "reasoning": "fine"}\n'
        ']}'
    )
    salvage = _result(text).parse_json_rows(list_field="results")
    assert [r["symbol"] for r in salvage.rows] == ["AAA", "CCC"]
    assert [m.key for m in salvage.malformed] == ["BBB"]


def test_null_thesis_invalid_if_is_still_tallied_in_parse_telemetry():
    """Regression, caught by the full test suite (2026-09-20): retyping
    `thesis_invalid_if` to `str | None` (so the wire schema can express the
    null the model legitimately sends) moved it out of `LLMOutputModel`'s
    generic null-droppable-field telemetry, since that mechanism only
    catches fields whose annotation still rejects None. The field's own
    validator now records the coercion directly so nothing about the null
    goes uncounted."""
    from src.models import parse_telemetry

    parse_telemetry.reset()
    item = dict(_VALID_ITEM)
    item["thesis_invalid_if"] = None
    item["rating"] = "neutral"
    TechAnalystAnswerItem(**item)
    assert parse_telemetry.snapshot().get(("TechAnalystAnswerItem", "thesis_invalid_if")) == 1


def test_a_schema_valid_wrapper_with_a_semantically_broken_row_is_caught_at_validation():
    """A row that parses as JSON but violates the pydantic schema (bad enum
    value) must be dropped as validation-failed, not silently accepted —
    this is the "caught before/during parsing, not patched after" guarantee
    item 157 asked for, exercised through the real construction path
    `_analyze_chunk` uses."""
    item = dict(_VALID_ITEM)
    item["rating"] = "extremely_buy"  # not in the Literal enum
    with pytest.raises(Exception):
        TechAnalystAnswerItem(**item)
    with pytest.raises(Exception):
        TechAnalysisResult(**item)


# --- end to end: the real seat, answering in the new wrapper shape -----------

def _symbols_data(symbols):
    bars = [OHLCV(date=date(2026, 9, 16), open=1.0, high=2.0, low=0.5,
                  close=1.5, volume=1_000)]
    return [
        {"symbol": s, "bars": bars,
         "indicators": TechnicalIndicators(symbol=s, atr_14=1.0)}
        for s in symbols
    ]


def _wrapped_response_for(symbol: str) -> str:
    item = dict(_VALID_ITEM)
    item["symbol"] = symbol
    return json.dumps({"results": [item]})


@patch("anthropic.Anthropic")
def test_analyze_batch_parses_the_new_wrapper_shape_end_to_end(mock_cls):
    """The seat's prompt now asks for `{"results": [...]}`; a well-behaved
    answer in that shape must resolve exactly as a bare-array one used to."""
    resp = MagicMock()
    resp.content = [MagicMock(text=_wrapped_response_for("SPY"))]
    resp.usage.input_tokens = 1
    resp.usage.output_tokens = 1
    mock_cls.return_value.messages.create.return_value = resp

    agent = TechAnalystAgent(api_key="test", model="claude-sonnet-4-6-20250514")
    results, _ = agent.analyze_batch(_symbols_data(["SPY"]))

    assert results["SPY"] is not None
    assert results["SPY"].symbol == "SPY"
    assert results["SPY"].rating == "buy"


@patch("anthropic.Anthropic")
def test_analyze_batch_still_parses_a_bare_list_answer(mock_cls):
    """Backward compatibility: a route that ignores the schema, or a replay
    of a legacy stored answer, must not regress."""
    resp = MagicMock()
    resp.content = [MagicMock(text=json.dumps([_VALID_ITEM]))]
    resp.usage.input_tokens = 1
    resp.usage.output_tokens = 1
    mock_cls.return_value.messages.create.return_value = resp

    agent = TechAnalystAgent(api_key="test", model="claude-sonnet-4-6-20250514")
    results, _ = agent.analyze_batch(_symbols_data(["SPY"]))

    assert results["SPY"] is not None
    assert results["SPY"].rating == "buy"
