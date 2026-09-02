"""An explicit `null` must never cost the desk a whole analysis — and must
never buy one either.

Background (measured against the production agent_logs snapshot covering
2026-08-14..2026-09-01):

    TechAnalysisResult.thesis_invalid_if          42 nulls / 2,021 occurrences
    MissedOpportunity.theme_durability            25 nulls /    50 occurrences
    MissedOpportunity.universe_addition_reason    11 nulls /    50 occurrences

All three declare a default and none accepted `None`, so pydantic rejected the
whole object before any mode="after" validator ran.

Scope, stated honestly: all 42 tech items carrying the null were rated
`neutral` (verified from the raw JSON, independent of the models), and the
2026-09-01 batch log reads `Batch: 58/58 symbols analyzed` — the retry
recovered every one. So this defect has not yet cost the desk a tradeable
candidate; what it has cost is paid retry round-trips and a class of loss that
would be invisible if it ever landed on a `buy`. The exposure is what justifies
the fix, not a proven lost trade.

The two halves of this file are equally load-bearing:

  * Fields WITH a default treat null as an absent key — and the coercion is
    counted, because silently blanking `thesis_invalid_if` throws away the
    soft-exit signal.
  * Fields that decide RISK still reject null. `test_load_bearing_*` is the
    test that must fail if that ever stops being true.
"""

import json
from unittest.mock import MagicMock, patch

import pytest
from pydantic import BaseModel, TypeAdapter, ValidationError

import src.models as models
from src.models import (
    LLMOutputModel,
    MissedOpportunity,
    RiskVerdict,
    SellGrade,
    TargetPosition,
    TechAnalysisResult,
    TradeDecision,
    parse_telemetry,
)


@pytest.fixture(autouse=True)
def _clean_telemetry():
    parse_telemetry.reset()
    yield
    parse_telemetry.reset()


def _trc() -> dict:
    return dict(trend="t", momentum="m", volatility="v", volume="vol",
                support_resistance="sr")


def _tech(**over) -> dict:
    base = dict(
        symbol="SPY", rating="buy", conviction="high",
        entry_price=500.0, stop_loss=490.0, reference_target=525.0,
        support_levels=[490.0], resistance_levels=[525.0],
        setup_type="range", expected_horizon_sessions=10,
        reasoning="x", reasoning_chain=_trc(), thesis_invalid_if="MA50 breaks",
    )
    base.update(over)
    return base


def _risk_rc() -> dict:
    return dict(rr_audit="x", signal_fidelity="x", correlation_check="x",
                event_risk="x", sizing_sanity="x", overall="x")


# ---------------------------------------------------------------------------
# Half 1 — a null on a DEFAULTED field must not cost us the object
# ---------------------------------------------------------------------------

def test_null_thesis_invalid_if_keeps_the_whole_analysis():
    """The exact production payload shape from 2026-09-01 13:33 UTC."""
    r = TechAnalysisResult(**_tech(thesis_invalid_if=None))
    assert r.thesis_invalid_if == ""
    # Everything else on the analysis survived — this is the point.
    assert r.rating == "buy"
    assert r.entry_price == 500.0
    assert r.stop_loss == 490.0
    assert r.reference_target == 525.0
    assert r.risk_reward is not None


@pytest.mark.parametrize("field_name, expected", [
    ("thesis_invalid_if", ""),
    ("conviction", "medium"),
    ("support_levels", []),          # rejected later by the after-validator
    ("computed_levels", []),
])
def test_null_on_defaulted_tech_field_takes_the_default(field_name, expected):
    payload = _tech(**{field_name: None})
    if field_name == "support_levels":
        # Keep the analysis valid: an actionable rating still needs A level.
        payload["resistance_levels"] = [525.0]
    r = TechAnalysisResult(**payload)
    assert getattr(r, field_name) == expected


@pytest.mark.parametrize("field_name, expected", [
    ("theme_durability", "unknown"),
    ("universe_addition_reason", ""),
])
def test_null_on_defaulted_missed_opportunity_field(field_name, expected):
    """Production emitted a null here on 25 of 50 `theme_durability` slots.

    Before this rule the entry was dropped by the evening pre-filter and the
    quarterly theme aggregation silently lost it.
    """
    mo = MissedOpportunity(
        symbol="XOM", move_pct=6.2, miss_category="noise_rally",
        lesson="no signal, legitimate hold", **{field_name: None},
    )
    assert getattr(mo, field_name) == expected


def test_null_equals_omitted_for_every_defaulted_field():
    """The rule's whole claim: null and absent must produce the same object."""
    omitted = TechAnalysisResult(**{
        k: v for k, v in _tech().items() if k != "thesis_invalid_if"
    })
    nulled = TechAnalysisResult(**_tech(thesis_invalid_if=None))
    assert omitted.model_dump() == nulled.model_dump()


# ---------------------------------------------------------------------------
# Half 2 — a null on a LOAD-BEARING field must still reject the object
#
# If any case here starts passing, an analysis with a missing stop, a missing
# entry, a missing side or an unscaled risk verdict has become tradeable.
# ---------------------------------------------------------------------------

LOAD_BEARING_NULLS = [
    # (label, callable that must raise)
    ("tech.stop_loss",        lambda: TechAnalysisResult(**_tech(stop_loss=None))),
    ("tech.entry_price",      lambda: TechAnalysisResult(**_tech(entry_price=None))),
    ("tech.reference_target", lambda: TechAnalysisResult(**_tech(reference_target=None))),
    ("tech.setup_type",       lambda: TechAnalysisResult(**_tech(setup_type=None))),
    ("tech.expected_horizon", lambda: TechAnalysisResult(**_tech(expected_horizon_sessions=None))),
    # Nulling BOTH level lists leaves an actionable rating with no structure.
    ("tech.all_levels",       lambda: TechAnalysisResult(
        **_tech(support_levels=None, resistance_levels=None))),
    # Required fields: no default exists, so there is nothing safe to fall back to.
    ("tech.rating",           lambda: TechAnalysisResult(**_tech(rating=None))),
    ("tech.symbol",           lambda: TechAnalysisResult(**_tech(symbol=None))),
    ("tech.reasoning",        lambda: TechAnalysisResult(**_tech(reasoning=None))),
    ("tech.reasoning_chain",  lambda: TechAnalysisResult(**_tech(reasoning_chain=None))),
    # Deny-listed: a default exists but it is an affirmative instruction.
    ("target.direction",      lambda: TargetPosition(
        symbol="SPY", thesis="t", risk_allocation_pct=1.0, direction=None)),
    ("verdict.scale_all_buys", lambda: RiskVerdict(
        approved=True, reasoning="r", scale_all_buys=None,
        reasoning_chain=_risk_rc())),
    ("verdict.approved",      lambda: RiskVerdict(
        approved=None, reasoning="r", reasoning_chain=_risk_rc())),
    ("trade.stop_loss",       lambda: TradeDecision(
        action="BUY", symbol="SPY", allocation_pct=2.0, entry_price=500.0,
        stop_loss=None, take_profit=525.0, reasoning="r")),
    ("trade.entry_price",     lambda: TradeDecision(
        action="BUY", symbol="SPY", allocation_pct=2.0, entry_price=None,
        stop_loss=490.0, take_profit=525.0, reasoning="r")),
    ("sellgrade.sell_price",  lambda: SellGrade(
        symbol="SPY", sell_date="2026-09-01", sell_price=None,
        current_price=510.0, pct_move_since_sell=1.0, grade="correct",
        reason="r")),
]


@pytest.mark.parametrize(
    "label, build", LOAD_BEARING_NULLS, ids=[c[0] for c in LOAD_BEARING_NULLS],
)
def test_load_bearing_null_is_never_silently_accepted(label, build):
    with pytest.raises(ValidationError):
        build()


def test_neutral_rating_still_clears_prices_rather_than_inventing_them():
    """Null tolerance must not resurrect stale numbers on a no-trade read."""
    r = TechAnalysisResult(
        symbol="SPY", rating="neutral", reasoning="x", reasoning_chain=_trc(),
        thesis_invalid_if=None,
    )
    assert r.thesis_invalid_if == ""
    assert r.entry_price is None and r.stop_loss is None
    assert r.reference_target is None and r.risk_reward is None


# ---------------------------------------------------------------------------
# The coercion is COUNTED — recovering the object quietly is not enough
# ---------------------------------------------------------------------------

def test_null_coercion_is_recorded_in_parse_telemetry():
    assert parse_telemetry.total_null_coercions() == 0
    TechAnalysisResult(**_tech(thesis_invalid_if=None))
    snap = parse_telemetry.snapshot()
    assert snap.get(("TechAnalysisResult", "thesis_invalid_if")) == 1
    assert "TechAnalysisResult.thesis_invalid_if" in parse_telemetry.describe_null_coercions()


def test_clean_payload_records_nothing():
    TechAnalysisResult(**_tech())
    assert parse_telemetry.total_null_coercions() == 0
    assert parse_telemetry.describe_null_coercions() == ""


def test_nulls_on_optional_fields_are_not_counted_as_coercions():
    """`X | None` nulls are the schema working, not degrading.

    A `neutral` read nulls entry/target/stop BY DESIGN — production emits
    1,111 such nulls per ~2,000 tech items. Counting those would fire the
    `analysis_field_nulled` advisory on every session with a four-figure
    number and make the signal worthless, which is the failure mode this
    whole change exists to avoid.
    """
    TechAnalysisResult(
        symbol="SPY", rating="neutral", reasoning="x", reasoning_chain=_trc(),
        entry_price=None, stop_loss=None, reference_target=None,
        setup_type=None, expected_horizon_sessions=None, signal_age_days=None,
        atr_14=None,
    )
    assert parse_telemetry.snapshot() == {}, (
        "an Optional field's null must never reach the coercion ledger"
    )


def test_droppable_sets_never_include_an_optional_field():
    for _name, cls in _model_classes():
        if not issubclass(cls, LLMOutputModel):
            continue
        for field_name in models._null_droppable_fields(cls):
            assert _rejects_none(cls.model_fields[field_name]), (
                f"{cls.__name__}.{field_name} already accepts None — it must "
                f"not be treated as droppable"
            )


def test_dropped_item_is_counted_separately_from_a_coercion():
    parse_telemetry.record_dropped_item("TechAnalysisResult", "NVDA")
    assert parse_telemetry.total_dropped() == 1
    assert parse_telemetry.total_null_coercions() == 0
    assert "NVDA" in parse_telemetry.describe_dropped()


def test_suspended_blocks_the_tally_but_not_the_coercion():
    with parse_telemetry.suspended():
        r = TechAnalysisResult(**_tech(thesis_invalid_if=None))
    assert r.thesis_invalid_if == ""          # still recovered
    assert parse_telemetry.snapshot() == {}   # but not counted
    # and the suspension is not sticky
    TechAnalysisResult(**_tech(thesis_invalid_if=None))
    assert parse_telemetry.total_null_coercions() == 1


def test_evening_prevalidation_does_not_double_count():
    """`_drop_invalid_missed_opportunities` validates, then EveningReport
    validates the same dicts again. One affected entry must count once."""
    from src.agents.evening_analyst import EveningAnalystAgent

    parsed = {
        "missed_opportunities": [{
            "symbol": "XOM", "move_pct": 6.2, "miss_category": "noise_rally",
            "lesson": "no signal", "theme_durability": None,
        }],
    }
    EveningAnalystAgent._drop_invalid_missed_opportunities(parsed)
    assert parse_telemetry.snapshot() == {}, "the dry run must not tally"
    MissedOpportunity(**parsed["missed_opportunities"][0])
    assert parse_telemetry.snapshot() == {("MissedOpportunity", "theme_durability"): 1}


def test_pm_prevalidation_does_not_double_count_and_records_its_drops():
    from src.agents.portfolio_manager import PortfolioManagerAgent

    parsed = {"targets": [
        {"symbol": "SPY", "thesis": "t", "risk_allocation_pct": 1.0,
         "thesis_invalid_if": None},
        {"symbol": "BAD", "thesis": "t"},          # sizes to nothing -> dropped
    ]}
    PortfolioManagerAgent._drop_invalid_targets(parsed)
    assert len(parsed["targets"]) == 1
    assert parse_telemetry.snapshot() == {}, "the dry run must not tally"
    assert parse_telemetry.dropped_snapshot() == {("TargetPosition", "BAD"): 1}
    TargetPosition(**parsed["targets"][0])
    assert parse_telemetry.snapshot() == {
        ("TargetPosition", "thesis_invalid_if"): 1
    }


@patch("anthropic.Anthropic")
def test_tech_analyst_records_the_analysis_it_drops(mock_cls):
    """A candidate the analysts researched and PM never saw is now countable.

    Drives the real `analyze_batch` parse loop with a response whose only
    defect is a malformed load-bearing field, so the item is genuinely
    discarded. Before this, the only trace was one ERROR log line — and none
    at all once the bounded retry recovered the symbol, which is what
    production did on 2026-09-01 (`Batch: 58/58 symbols analyzed`).
    """
    from datetime import date

    from src.agents.tech_analyst import TechAnalystAgent
    from src.models import OHLCV, TechnicalIndicators

    bad = json.dumps([{
        "symbol": "SPY", "rating": "buy", "conviction": "high",
        "entry_price": 507.0, "reference_target": 530.0,
        "stop_loss": 999.0,                      # above entry on a BUY — fatal
        "support_levels": [494.0], "resistance_levels": [530.0],
        "setup_type": "range", "expected_horizon_sessions": 10,
        "reasoning_chain": {
            "trend": "t", "momentum": "m", "volatility": "v",
            "volume": "vol", "support_resistance": "sr",
        },
        "reasoning": "r",
    }])
    mock_client = MagicMock()
    resp = MagicMock()
    resp.content = [MagicMock(text=bad)]
    resp.usage.input_tokens = 10
    resp.usage.output_tokens = 10
    mock_client.messages.create.return_value = resp
    mock_cls.return_value = mock_client

    agent = TechAnalystAgent(api_key="test", model="claude-sonnet-4-6-20250514")
    bars = [OHLCV(date=date(2026, 4, 7), open=503.0, high=510.0, low=500.0,
                  close=507.0, volume=1_000_000)]
    ind = TechnicalIndicators(symbol="SPY", ma_20=505.0, atr_14=8.5)
    results, _ = agent.analyze_batch(
        [{"symbol": "SPY", "bars": bars, "indicators": ind}],
    )

    assert results == {"SPY": None}
    assert parse_telemetry.dropped_snapshot().get(("TechAnalysisResult", "SPY")), (
        "a discarded analysis must be counted, not only logged"
    )


# ---------------------------------------------------------------------------
# The mechanical guard — this is what stops the class of bug reopening
# ---------------------------------------------------------------------------

# Models NOT parsed from LLM output. A null in one of these comes from our own
# code, and a loud failure is the correct response to our own bug.
NON_LLM_MODELS = {
    "OHLCV", "TechnicalIndicators", "Position", "MissedOpportunitySnapshot",
    "AgentLog", "LLMOutputModel",
}


def _rejects_none(field) -> bool:
    try:
        TypeAdapter(field.annotation).validate_python(None)
    except Exception:
        return True
    return False


def _model_classes():
    for name in dir(models):
        obj = getattr(models, name, None)
        if (isinstance(obj, type) and issubclass(obj, BaseModel)
                and obj is not BaseModel and obj.__module__ == "src.models"):
            yield name, obj


def test_every_llm_parsed_model_with_a_defaulted_field_has_null_tolerance():
    """A new model that forgets the mixin fails HERE, not in production.

    The original bug was fixed one `field_validator` at a time. There are 119
    fields with the same shape, so that approach loses by attrition; this test
    is the mechanical replacement. If you add a model that is parsed from an
    LLM response, inherit `LLMOutputModel`. If it genuinely is not, name it in
    `NON_LLM_MODELS` and say why.
    """
    offenders = []
    for name, cls in _model_classes():
        if name in NON_LLM_MODELS or issubclass(cls, LLMOutputModel):
            continue
        exposed = [
            f for f, fld in cls.model_fields.items()
            if not fld.is_required() and _rejects_none(fld)
        ]
        if exposed:
            offenders.append((name, exposed))
    assert not offenders, (
        "these models can lose a whole object to an explicit null on a field "
        f"that declares a default: {offenders}"
    )


def test_deny_list_names_only_real_fields():
    """A typo in `_NULL_MUST_FAIL` would silently un-protect a risk field."""
    for cls_name, field_name in models._NULL_MUST_FAIL:
        cls = getattr(models, cls_name)
        assert field_name in cls.model_fields, f"{cls_name}.{field_name} does not exist"
        fld = cls.model_fields[field_name]
        assert not fld.is_required(), (
            f"{cls_name}.{field_name} is required — it needs no deny-list entry"
        )
        assert field_name not in models._null_droppable_fields(cls)


def test_required_fields_are_never_treated_as_droppable():
    for _name, cls in _model_classes():
        if not issubclass(cls, LLMOutputModel):
            continue
        droppable = models._null_droppable_fields(cls)
        for field_name in droppable:
            assert not cls.model_fields[field_name].is_required(), (
                f"{cls.__name__}.{field_name} is required but marked droppable"
            )
