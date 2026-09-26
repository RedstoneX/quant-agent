"""The schema a seat is SENT must not contain fields the desk fills itself.

`BaseAgent._response_format_for` renders `result_model.model_json_schema()`
into the OpenRouter / OpenAI `response_format`, and `_strictify_schema` then
forces every property in it to be `required`. So any desk-owned field left in
that schema is a field the model is COMPELLED to invent, and whose invented
value the pipeline throws away (or, for `PortfolioDecision.decisions`,
refuses the whole answer over).

These tests are the mechanical check for that: they fail if a field named
below reappears on the model-facing surface, and they separately prove the
desk can still set, validate and persist every one of them.
"""

import json

import pytest

from src.agents.base import _RESPONSE_FORMAT_CACHE, _response_format_for
from src.models import (
    NewsIntelligenceReport,
    PortfolioDecision,
    SmartMoneyFinding,
    SmartMoneySynthesis,
)

#: model class -> the properties the DESK owns and the seat must never see.
#:
#: This list is NOT the whole class of defect, and a green run here does not
#: mean it is closed. Deliberately absent: `BuyGrade.buy_price` /
#: `current_price` / `pct_move_since_buy` / `market_relative_move_pct`,
#: `MissedOpportunity.move_pct` and `LossPattern.occurrences` /
#: `total_loss_pct`. Those are a DIFFERENT defect — the desk computes the
#: number, renders it into the prompt, and asks the model to hand it back,
#: and unlike everything below the returned value is NOT overwritten, so
#: `src/evolution/quarterly_digest.py` sums a model-transcribed copy of a
#: figure the desk already holds. Hiding those from the schema without first
#: re-injecting the desk's own value would zero that aggregate. They need
#: their own board item, not an entry here.

DESK_ONLY_FIELDS: dict[type, tuple[str, ...]] = {
    PortfolioDecision: ("decisions", "constructor_dropped"),
    SmartMoneySynthesis: (
        "observations",
        "evidence_hash",
        "support_eligible",
        "transient_admission_eligible",
    ),
    NewsIntelligenceReport: ("dropped_news_symbols",),
}


def _property_names(node: object, found: set[str]) -> set[str]:
    """Every `properties` key anywhere in a JSON-Schema document."""
    if isinstance(node, dict):
        props = node.get("properties")
        if isinstance(props, dict):
            found.update(props)
        for value in node.values():
            _property_names(value, found)
    elif isinstance(node, list):
        for item in node:
            _property_names(item, found)
    return found


@pytest.fixture(autouse=True)
def _clear_response_format_cache():
    # `_response_format_for` memoises on the bare class name for the life of
    # the process; these tests must read the schema this tree renders.
    _RESPONSE_FORMAT_CACHE.clear()
    yield
    _RESPONSE_FORMAT_CACHE.clear()


@pytest.mark.parametrize(
    ("model_cls", "desk_fields"),
    [(cls, fields) for cls, fields in DESK_ONLY_FIELDS.items()],
    ids=[cls.__name__ for cls in DESK_ONLY_FIELDS],
)
def test_rendered_response_format_has_no_desk_only_property(model_cls, desk_fields):
    schema = _response_format_for(model_cls)["json_schema"]["schema"]
    present = _property_names(schema, set())
    leaked = sorted(set(desk_fields) & present)
    assert not leaked, (
        f"{model_cls.__name__} sends the model desk-owned field(s) {leaked}; "
        "strict structured output makes every property required, so the seat "
        "is being ordered to invent a value the desk immediately overwrites"
    )


@pytest.mark.parametrize(
    ("model_cls", "desk_fields"),
    [(cls, fields) for cls, fields in DESK_ONLY_FIELDS.items()],
    ids=[cls.__name__ for cls in DESK_ONLY_FIELDS],
)
def test_desk_only_fields_still_exist_on_the_storage_model(model_cls, desk_fields):
    """Hidden from the wire is not the same as deleted.

    Every field above must still be a real, settable, serialisable field —
    the pipeline writes each one after parsing and downstream stages read it.
    """
    if model_cls is SmartMoneySynthesis:
        model_cls = SmartMoneyFinding
    for field in desk_fields:
        assert field in model_cls.model_fields, (
            f"{model_cls.__name__}.{field} was removed, not hidden"
        )


def test_smart_money_observation_rows_are_no_longer_demanded_of_the_seat():
    """The 45-field internal row was the bulk of what the seat had to emit."""
    schema = _response_format_for(SmartMoneySynthesis)["json_schema"]["schema"]
    rendered = json.dumps(schema)
    assert "SmartMoneyObservation" not in rendered
    # Sanity on the scale: the whole synthesis schema is now small enough that
    # it cannot be hiding the row under another name.
    assert len(rendered) < 3000, len(rendered)


def test_smart_money_schema_now_matches_the_contract_its_prompt_states():
    """`config/prompts/smart_money_analyst.md` line 6 states the whole output
    contract as five keys. The strict schema used to demand nine, four of
    them desk-owned — the prompt and the schema disagreed."""
    schema = _response_format_for(SmartMoneySynthesis)["json_schema"]["schema"]
    finding = schema["$defs"]["SmartMoneyFinding"]
    assert list(finding["properties"]) == [
        "symbol", "stance", "economic_role", "summary", "why_now",
    ]
    assert sorted(finding["required"]) == sorted(finding["properties"])


def test_portfolio_decision_still_round_trips_pipeline_written_fields():
    decision = PortfolioDecision(
        reasoning_chain={
            "macro_filter": "x", "news_check": "x", "earnings_check": "x",
            "signal_conflicts": "x", "sizing_logic": "x",
            "portfolio_balance": "x", "cash_target": "x",
        },
        portfolio_view="x",
    )
    decision.constructor_dropped = ["AAPL"]
    dumped = decision.model_dump()
    assert dumped["constructor_dropped"] == ["AAPL"]
    assert "decisions" in dumped
    assert PortfolioDecision(**dumped).constructor_dropped == ["AAPL"]


def test_news_report_still_stores_dropped_symbols_set_after_parse():
    report = NewsIntelligenceReport(
        macro_narrative={
            "last_updated": "2026-09-23", "era_themes": ["AI capex"],
            "current_regime": "risk-on", "key_state_tracker": {},
        },
        pm_briefing="No material overnight change.",
        market_sentiment="neutral",
        confidence="low",
    )
    report.dropped_news_symbols = ["MSFT"]
    assert report.model_dump()["dropped_news_symbols"] == ["MSFT"]
