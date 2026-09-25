"""The 2026-09-25 owner ROLE-BASED conviction bar: earn the right to ENTER and STAY.

Mandate (verbatim intent):
  * The TECHNICAL (chart) seat is a TIMING VETO, not a weighted yes-vote. A
    broken/hostile chart blocks ENTRY even when the fundamental thesis is
    strong ("right name, wrong time"). Technical adds no positive weight; it
    only GATES.
  * The own-bar clears only when at least one SUPPORTIVE seat carries a
    SPECIFIC, FALSIFIABLE thesis (a real, backed directional call, not a bare
    neutral shrug) AND no seat is opposed.
  * ONE definition drives ENTRY (`candidate_eligibility` R7) and STAYING (the
    same `blocked` set, via rotation's `ineligible_hold` tier). A HELD name that
    misses the bar is not sold on one review's noise: it must miss two
    CONSECUTIVE reviews first (the sourced two-consecutive-close persistence
    discipline, Edwards & Magee).
"""

from src.agents.portfolio_manager import PortfolioManagerAgent
from src.models import AnalystVerdict, VerdictEvidence
from src.verdicts import RankedCandidate
from src.risk.rules import OWN_BAR_REASON_PREFIX, own_bar_block_reason
from src.rotation import (
    CONVICTION_BAR_REASON_PREFIX,
    CONVICTION_STAY_CONFIRMATION_REVIEWS,
    apply_stay_confirmation,
    holdings_below_entry_bar,
)


def _v(seat, symbol="X", *, direction="bullish", conviction="medium",
       invalidation="closes back below the breakout level", evidence=True):
    return AnalystVerdict(
        seat=seat, symbol=symbol, direction=direction,
        magnitude=0.0,  # the fundamental seats state no strength (NO_STATED_STRENGTH)
        conviction=conviction,
        evidence=(
            [VerdictEvidence(label="ev", text="a checkable observed fact")]
            if (evidence and direction != "neutral") else []
        ),
        invalidation=(invalidation if direction != "neutral" else ""),
    )


def _rank(symbol, direction="bullish"):
    return RankedCandidate(symbol=symbol, direction=direction, score=1.0)


def _apply(ranked, verdicts):
    return PortfolioManagerAgent._apply_conviction_bar(
        ranked=ranked, blocked={}, all_verdicts=verdicts,
    )


# ---------------------------------------------------------------------------
# The pure role-based bar
# ---------------------------------------------------------------------------

def test_reason_prefix_matches_rotation_gate():
    # The STAY streak and holdings_below_entry_bar recognise R7 by this prefix;
    # if the two ever drift, the streak stops gating the right reasons.
    assert OWN_BAR_REASON_PREFIX == CONVICTION_BAR_REASON_PREFIX


def test_clears_when_technical_confirms_one_supportive_thesis_none_opposed():
    verdicts = [_v("technical"), _v("news")]
    assert own_bar_block_reason(verdicts, direction="bullish") is None


def test_vlo_all_medium_two_supportive_but_no_technical_read_is_refused():
    """The VLO 2026-09-24 shape: two fundamental seats agree at MEDIUM, none
    opposed, but there is NO confirming technical read this review -> timing
    cannot be confirmed, so the name is refused (right name, wrong time)."""
    verdicts = [_v("news"), _v("macro")]  # no technical seat at all
    reason = own_bar_block_reason(verdicts, direction="bullish")
    assert reason is not None
    assert reason.startswith(OWN_BAR_REASON_PREFIX)
    assert "technical" in reason.lower()


def test_strong_fundamental_but_broken_chart_is_refused():
    """A genuinely convinced fundamental seat with a specific thesis, but the
    chart is HOSTILE (technical bearish) -> Technical timing veto. Right name,
    wrong time."""
    verdicts = [
        _v("news", conviction="high"),
        _v("earnings", conviction="high"),
        _v("technical", direction="bearish"),
    ]
    reason = own_bar_block_reason(verdicts, direction="bullish")
    assert reason is not None and reason.startswith(OWN_BAR_REASON_PREFIX)


def test_neutral_chart_does_not_confirm_timing_is_refused():
    verdicts = [_v("news"), _v("technical", direction="neutral")]
    reason = own_bar_block_reason(verdicts, direction="bullish")
    assert reason is not None and "confirm" in reason.lower()


def test_one_opposed_seat_blocks_even_with_confirming_chart_and_thesis():
    verdicts = [
        _v("technical"), _v("news"),
        _v("macro", direction="bearish"),
    ]
    reason = own_bar_block_reason(verdicts, direction="bullish")
    assert reason is not None and "opposed" in reason.lower()


def test_technical_alone_carries_no_positive_weight():
    """Technical confirming is necessary but NOT sufficient: with no supportive
    NON-technical seat carrying a thesis, the bar refuses. This is the exact
    'HIGH gate collapses to Technical-must-be-HIGH' failure the design avoids."""
    verdicts = [_v("technical", conviction="high")]
    reason = own_bar_block_reason(verdicts, direction="bullish")
    assert reason is not None
    assert "non-technical" in reason.lower()


def test_supportive_thesis_must_be_directional_not_a_neutral_shrug():
    """A non-technical seat that is merely NEUTRAL is not a specific falsifiable
    thesis, so a confirming chart plus only-neutral fundamentals refuses."""
    verdicts = [_v("technical"), _v("news", direction="neutral")]
    reason = own_bar_block_reason(verdicts, direction="bullish")
    assert reason is not None


def test_short_side_uses_bearish_as_supportive():
    verdicts = [
        _v("technical", direction="bearish"),
        _v("news", direction="bearish"),
    ]
    assert own_bar_block_reason(verdicts, direction="bearish") is None
    # a bullish seat is OPPOSED to a short
    verdicts.append(_v("macro", direction="bullish"))
    assert own_bar_block_reason(verdicts, direction="bearish") is not None


# ---------------------------------------------------------------------------
# ENTRY overlay (_apply_conviction_bar, consumed by the PM ranking)
# ---------------------------------------------------------------------------

def test_entry_vlo_shape_is_not_admitted():
    ranked = [_rank("VLO")]
    verdicts = [_v("news", "VLO"), _v("macro", "VLO")]  # no confirming chart
    survivors, blocked = _apply(ranked, verdicts)
    assert survivors == []
    assert any(r.startswith(CONVICTION_BAR_REASON_PREFIX) for r in blocked["VLO"])


def test_entry_confirmed_thesis_is_admitted():
    ranked = [_rank("STRG")]
    verdicts = [_v("technical", "STRG"), _v("news", "STRG")]
    survivors, blocked = _apply(ranked, verdicts)
    assert [c.symbol for c in survivors] == ["STRG"]
    assert blocked == {}


def test_entry_broken_chart_is_refused():
    ranked = [_rank("RITE")]
    verdicts = [
        _v("news", "RITE", conviction="high"),
        _v("earnings", "RITE", conviction="high"),
        _v("technical", "RITE", direction="bearish"),
    ]
    survivors, blocked = _apply(ranked, verdicts)
    assert survivors == []
    assert any(r.startswith(CONVICTION_BAR_REASON_PREFIX) for r in blocked["RITE"])


def test_entry_thinly_covered_single_seat_refused():
    ranked = [_rank("SOLO")]
    verdicts = [_v("technical", "SOLO")]  # confirming chart, no fundamental thesis
    survivors, blocked = _apply(ranked, verdicts)
    assert survivors == []


# ---------------------------------------------------------------------------
# STAY (two-consecutive-review confirmation streak, ported unchanged)
# ---------------------------------------------------------------------------

def test_stay_window_is_two_reviews():
    assert CONVICTION_STAY_CONFIRMATION_REVIEWS == 2


def _blocked_r7(sym):
    return {sym: [f"{CONVICTION_BAR_REASON_PREFIX} — technical does not confirm timing"]}


def test_stay_first_miss_does_not_cull():
    adjusted, this_missed = apply_stay_confirmation(
        _blocked_r7("HELD"), held_symbols={"HELD"},
        evaluated_symbols={"HELD"}, prior_missed={},
    )
    assert holdings_below_entry_bar(adjusted, {"HELD"}) == ()
    assert this_missed == {"HELD": True}


def test_stay_two_consecutive_misses_becomes_cull_candidate():
    adjusted, this_missed = apply_stay_confirmation(
        _blocked_r7("HELD"), held_symbols={"HELD"},
        evaluated_symbols={"HELD"}, prior_missed={"HELD": True},
    )
    assert holdings_below_entry_bar(adjusted, {"HELD"}) == ("HELD",)
    assert this_missed == {"HELD": True}


def test_stay_one_review_recovery_resets_streak():
    adjusted, this_missed = apply_stay_confirmation(
        {}, held_symbols={"HELD"}, evaluated_symbols={"HELD"},
        prior_missed={"HELD": True},
    )
    assert holdings_below_entry_bar(adjusted, {"HELD"}) == ()
    assert this_missed == {"HELD": False}


def test_stay_confirmation_only_gates_the_r7_reason():
    blocked = {"HELD": ["R5 net evidence +0 if long — no rung"]}
    adjusted, _ = apply_stay_confirmation(
        blocked, held_symbols={"HELD"}, evaluated_symbols={"HELD"},
        prior_missed={},
    )
    assert holdings_below_entry_bar(adjusted, {"HELD"}) == ("HELD",)


def test_intact_thesis_held_name_is_not_a_cull_candidate():
    """A held name that CLEARS the bar this review is absent from `blocked`, so
    it is never offered to the ineligible_hold cull tier — an intact thesis is
    protected from force-sell at the bar itself."""
    survivors, blocked = _apply([_rank("KEEP")], [_v("technical", "KEEP"), _v("news", "KEEP")])
    assert [c.symbol for c in survivors] == ["KEEP"]
    assert holdings_below_entry_bar(blocked, {"KEEP"}) == ()
