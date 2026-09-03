"""Phase 13 — `SmartMoneyFinding.to_verdict()`.

This mapping is HARDER than Technical's (`tests/test_analyst_verdict.py`):
the finding carries no confidence/magnitude field to restate, and its
`stance` includes a fourth value ("mixed") the shared `AnalystVerdict`
shape does not allow. Two things pinned here are genuine NEW judgment, not
restatement — see `_SMART_MONEY_ROLE_CONVICTION` / `_SMART_MONEY_ROLE_MAGNITUDE`
in `src/models.py`:

1. `economic_role` -> `conviction` (actionable=high, confirmatory=medium,
   contradictory/historical=low).
2. `economic_role` -> `magnitude` for a directional stance (equal-spaced,
   same ordering as (1)).

Also pinned, but this one IS an existing desk convention, not new judgment:
"mixed" stance collapses onto "neutral" (see `PortfolioManagerAgent.
_collapse_stances` and `_stance_matches_source`, which already treat mixed
and neutral as the same non-directional bucket).
"""

from __future__ import annotations

from datetime import date

import pytest
from pydantic import ValidationError

from src.models import SmartMoneyFinding, SmartMoneyObservation


def _obs(
    *, actor="Example Member", direction="buy", amount_range="$50,001-$100,000",
    transaction_date=date(2026, 8, 20), lag=2, age=1, freshness="fresh",
    economic_role="actionable",
):
    # stream="insider" (not the default "congressional") deliberately: a
    # single congressional observation is force-downgraded to
    # economic_role="historical" by SmartMoneyFinding.deterministic_eligibility
    # (the conservative congressional contract needs >=2 actors to count).
    # These fixtures are testing the to_verdict() role->conviction/magnitude
    # mapping, not that unrelated eligibility rule, so use the insider
    # stream, which is not subject to it.
    return SmartMoneyObservation(
        symbol="NVDA", stream="insider", actor=actor, direction=direction,
        amount_range=amount_range,
        transaction_date=transaction_date,
        disclosure_date=transaction_date,
        source_url="https://example.test/filing",
        lag_days=lag, disclosure_age_days=age, freshness=freshness,
        economic_role=economic_role,
    )


def _finding(
    *, stance="bullish", economic_role="actionable",
    summary="cluster of insider buys", why_now="fresh Form 4 purchases ahead of earnings",
    observations=None,
):
    return SmartMoneyFinding(
        symbol="NVDA", stance=stance, economic_role=economic_role,
        summary=summary, why_now=why_now,
        observations=observations or [_obs(economic_role=economic_role)],
    )


# ==========================================================================
# direction: stance -> AnalystVerdict.direction, "mixed" folds to "neutral"
# ==========================================================================

def test_bullish_stance_maps_to_bullish_direction():
    v = _finding(stance="bullish").to_verdict()
    assert v.direction == "bullish"


def test_bearish_stance_maps_to_bearish_direction():
    v = _finding(stance="bearish", observations=[_obs(direction="sell")]).to_verdict()
    assert v.direction == "bearish"


def test_neutral_stance_maps_to_neutral_direction_with_zero_magnitude():
    v = _finding(stance="neutral").to_verdict()
    assert v.direction == "neutral"
    assert v.magnitude == 0.0
    assert v.invalidation == ""


def test_mixed_stance_folds_onto_neutral_not_a_fifth_direction():
    """'mixed' is not a legal AnalystVerdict direction. The desk already
    treats it as equivalent to neutral (non-directional) elsewhere —
    PortfolioManagerAgent._collapse_stances and _stance_matches_source both
    bucket 'mixed' with 'neutral', never with a directional call."""
    v = _finding(
        stance="mixed",
        observations=[_obs(direction="buy"), _obs(direction="sell", actor="B")],
    ).to_verdict()
    assert v.direction == "neutral"
    assert v.magnitude == 0.0
    assert v.invalidation == ""


# ==========================================================================
# conviction: economic_role -> conviction (NEW JUDGMENT)
# ==========================================================================

@pytest.mark.parametrize("role,expected", [
    ("actionable", "high"),
    ("confirmatory", "medium"),
    ("contradictory", "low"),
    ("historical", "low"),
])
def test_economic_role_maps_to_conviction(role, expected):
    v = _finding(stance="bullish", economic_role=role).to_verdict()
    assert v.conviction == expected


def test_conviction_mapping_reflects_role_even_for_a_neutral_call():
    """Conviction is how sure the seat is, separate from what it thinks —
    a neutral/mixed finding still carries a conviction about its own
    (non-)directionality."""
    v = _finding(stance="neutral", economic_role="actionable").to_verdict()
    assert v.conviction == "high"


# ==========================================================================
# magnitude: economic_role -> magnitude for a directional stance (NEW JUDGMENT)
# ==========================================================================

@pytest.mark.parametrize("role,expected", [
    ("actionable", 1.0),
    ("confirmatory", 0.6),
    ("contradictory", 0.3),
    ("historical", 0.3),
])
def test_economic_role_maps_to_magnitude_when_directional(role, expected):
    v = _finding(stance="bullish", economic_role=role).to_verdict()
    assert v.magnitude == expected


def test_neutral_magnitude_is_always_zero_regardless_of_role():
    for role in ("actionable", "confirmatory", "contradictory", "historical"):
        v = _finding(stance="neutral", economic_role=role).to_verdict()
        assert v.magnitude == 0.0


# ==========================================================================
# evidence: summary + why_now + up to 5 most-recent observations
# ==========================================================================

def test_evidence_carries_summary_why_now_and_observations():
    v = _finding(
        stance="bullish",
        summary="three insiders bought this week",
        why_now="cluster of Form 4 purchases within 48 hours of guidance",
    ).to_verdict()
    labels = [e.label for e in v.evidence]
    assert "summary" in labels
    assert "why_now" in labels
    assert "observation" in labels
    summary_item = next(e for e in v.evidence if e.label == "summary")
    assert summary_item.text == "three insiders bought this week"


def test_evidence_caps_observations_at_five_most_recent():
    obs = [
        _obs(actor=f"Actor {i}", transaction_date=date(2026, 8, 1 + i))
        for i in range(8)
    ]
    v = _finding(stance="bullish", observations=obs).to_verdict()
    obs_items = [e for e in v.evidence if e.label == "observation"]
    assert len(obs_items) == 5
    # Most recent (Actor 7, 2026-08-08) first.
    assert "Actor 7" in obs_items[0].text
    assert "2026-08-08" in obs_items[0].text
    assert "Actor 3" in obs_items[4].text


# ==========================================================================
# invalidation: "" for neutral (allowed); constructed from why_now otherwise
# ==========================================================================

def test_directional_invalidation_is_constructed_from_why_now():
    v = _finding(
        stance="bullish", why_now="cluster buying ahead of the earnings print",
    ).to_verdict()
    assert v.invalidation != ""
    assert "cluster buying ahead of the earnings print" in v.invalidation


def test_neutral_invalidation_is_blank():
    v = _finding(stance="neutral").to_verdict()
    assert v.invalidation == ""


# ==========================================================================
# identity + validity
# ==========================================================================

def test_seat_is_smart_money():
    v = _finding(stance="bullish").to_verdict()
    assert v.seat == "smart_money"


def test_symbol_is_normalized():
    v = _finding(stance="bullish").to_verdict()
    assert v.symbol == "NVDA"


def test_a_directional_verdict_from_this_seat_still_validates_the_shared_shape():
    """The base AnalystVerdict validator requires evidence + invalidation
    for any directional call — prove smart_money's mapping satisfies it
    rather than relying on a permissive default."""
    v = _finding(stance="bearish", observations=[_obs(direction="sell")]).to_verdict()
    assert v.evidence
    assert v.invalidation


def test_a_bearish_finding_has_negative_signed_magnitude():
    v = _finding(
        stance="bearish", economic_role="actionable",
        observations=[_obs(direction="sell")],
    ).to_verdict()
    assert v.signed_magnitude == -1.0
