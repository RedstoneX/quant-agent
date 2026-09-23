"""`correlation breach` is no longer an accepted exit trigger — WORK.md item 44.

THE DEFECT (found 2026-09-11 by audit, fixed 2026-09-13). The hard-trigger
phrase gate (`pipeline._reason_cites_hard_trigger`) accepted the phrases
"correlation breach" and "correlation cluster breach", and
`exit_guard.cites_external_information` classified them as a claim of
information originating OUTSIDE the price series — which additionally bought
them a bypass of the 1×ATR noise band.

Nothing in the desk ever computed a correlation-breach EVENT.
`src/data/correlation.py` measures |r| >= 0.7 CLUSTERS at decision time; it
has no notion of a break. And `holding_discipline_claim_check` — the function
that adjudicates a regime-flip or bearish-state-change claim against the day's
real recorded data — has no branch for a correlation claim at all: it returns
verdict "ok", not even the log-only "unverifiable". So the phrase was not
merely unchecked, it was unrecorded: a protected position could be sold on
those two words with no audit-trail entry of any kind.

The published literature was searched for an operational definition (a stated
window, a stated threshold) that could have grounded a verifier. None was
found — the contagion literature tests a correlation CHANGE around an
exogenously-given crisis date, retrospectively, which is not a same-day
per-position exit trigger. See docs/INCIDENT_HISTORY.md for the citations.
Rather than invent the window and the threshold, the phrase was removed.

WHAT MUST NOT REGRESS
  1. The phrase must not re-enter the hard-trigger vocabulary.
  2. It must not re-enter the external-information list (a correlation is a
     function of the price series; it is the tape, not news about the tape).
  3. Every OTHER accepted trigger must still be accepted — this fix must not
     have narrowed the gate beyond the one phrase.
  4. The exact case that used to slip through — a SELL on a protected
     position whose only named trigger is a correlation breach — must now be
     dropped by the midday/close executor and recorded.
"""

import pytest

from src.pipeline import _HARD_TRIGGER_KEYWORDS, _reason_cites_hard_trigger
from src.risk.exit_guard import (
    EXTERNAL_INFORMATION_PATTERNS,
    cites_external_information,
    holding_discipline_claim_check,
)


CORRELATION_PHRASINGS = (
    "correlation breach",
    "correlation cluster breach",
    "Correlation Breach across the book",
    "exiting on correlation breach: three names now move as one",
    "CORRELATION CLUSTER BREACH — the AI theme cracked",
)


# ---------------------------------------------------------------------------
# 1 + 2. The phrase is gone from both vocabularies
# ---------------------------------------------------------------------------

def test_phrase_is_absent_from_the_hard_trigger_vocabulary():
    joined = " ".join(_HARD_TRIGGER_KEYWORDS).lower()
    assert "correlation" not in joined


def test_phrase_is_absent_from_the_external_information_patterns():
    joined = " ".join(EXTERNAL_INFORMATION_PATTERNS).lower()
    assert "correlation" not in joined


@pytest.mark.parametrize("reason", CORRELATION_PHRASINGS)
def test_correlation_reason_no_longer_cites_a_hard_trigger(reason):
    assert _reason_cites_hard_trigger(reason) is False


@pytest.mark.parametrize("reason", CORRELATION_PHRASINGS)
def test_correlation_reason_no_longer_bypasses_the_noise_band(reason):
    """A correlation is computed FROM the price series, so it is price-derived
    by construction and never qualified as external information."""
    assert cites_external_information(reason) is False


def test_correlation_claim_is_invisible_to_the_holding_discipline_checker():
    """Pins WHY removal was the fix rather than wiring, and corrects the
    item's own description: the checker returns "ok", not "unverifiable".
    There is no branch to make verifiable, so nothing was ever logged."""
    check = holding_discipline_claim_check(
        action="SELL",
        reason="correlation breach across the book",
        symbol="AAA",
        protected=True,
        macro_regime_today="risk-on",
        macro_status="ok",
        active_state_changes="",
    )
    assert check.verdict == "ok"
    assert check.blocks is False
    assert check.finding is None


# ---------------------------------------------------------------------------
# 3. Nothing else was narrowed
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("reason", [
    "thesis_invalid triggered: closed below MA50",
    "thesis broken on the daily chart",
    "adverse news: FDA rejection this morning",
    "material news landed after the open",
    "sector shock — the whole group gapped down",
    "high-conviction bearish state change posted today",
    "bearish earnings, revenue missed by 8%",
    "earnings miss on both lines",
    "guidance cut for the full year",
    "macro regime shift to risk-off today",
    "regime flip confirmed this morning",
    # "daily loss circuit breaker fired" was in this list until 2026-09-20,
    # when the account-level loss alarm it named was removed in full
    # (WORK.md item 32). It moved to the rejected list below, for the first
    # of the two reasons this file's own docstring gives for correlation
    # breach: nothing in the desk computes that event any more.
    "stopped out at the broker",
])
def test_every_other_trigger_still_passes(reason):
    assert _reason_cites_hard_trigger(reason) is True, reason


@pytest.mark.parametrize("reason", [
    "adverse news: FDA rejection this morning",
    "bearish earnings, revenue missed",
    "macro regime flip to risk-off",
    "sector shock hit the whole group",
    "stopped out at the broker",
])
def test_other_external_information_still_bypasses_the_noise_band(reason):
    assert cites_external_information(reason) is True, reason


# ---------------------------------------------------------------------------
# 4. The case that used to slip through, end to end
# ---------------------------------------------------------------------------

def test_midday_sell_on_correlation_breach_alone_is_now_dropped():
    """Verbatim the free exit item 44 describes. Before this change the
    reason cleared the phrase gate, cleared the external-information test
    (so the noise band did not apply either), and reached the broker."""
    from tests.test_pipeline import _mk_midday_pipeline, _review_rc
    from src.models import Position, PositionAction, PositionReview

    position = Position(
        symbol="AMZN", qty=20.0, avg_entry=180.0, current_price=210.0,
        market_value=4200.0, unrealized_pnl=600.0,
        unrealized_intraday_pnl=0.0, sector="Consumer Cyclical",
    )
    pipeline = _mk_midday_pipeline(position)
    review = PositionReview(
        reasoning_chain=_review_rc(),
        actions=[PositionAction(
            action="SELL", symbol="AMZN",
            reason="correlation breach: three names now move as one cluster",
        )],
        overall_assessment="de-risking the cluster",
        risk_level="high",
    )

    orders = pipeline._midday_execute_llm_actions(
        positions=[position], review=review, run_id="r-44",
    )

    assert orders == []
    pipeline.broker.submit_order.assert_not_called()
    pipeline.db.insert_trade.assert_not_called()
    statuses = [
        c.kwargs.get("status")
        for c in pipeline.db.record_intraday_evaluation.call_args_list
    ]
    assert "exit_blocked_no_named_trigger" in statuses


def test_midday_sell_still_executes_on_a_verifiable_trigger():
    """The control. Same position, same surface, a trigger that names
    something the desk actually records — the exit must still go through, or
    this fix has broken exiting rather than tightened it."""
    from tests.test_pipeline import _mk_midday_pipeline, _review_rc
    from src.models import Position, PositionAction, PositionReview

    position = Position(
        symbol="AMZN", qty=20.0, avg_entry=180.0, current_price=210.0,
        market_value=4200.0, unrealized_pnl=600.0,
        unrealized_intraday_pnl=0.0, sector="Consumer Cyclical",
    )
    pipeline = _mk_midday_pipeline(position)
    review = PositionReview(
        reasoning_chain=_review_rc(),
        actions=[PositionAction(
            action="SELL", symbol="AMZN",
            reason="thesis_invalid_if triggered — guidance cut on the call",
        )],
        overall_assessment="exit on broken thesis",
        risk_level="high",
    )

    pipeline._midday_execute_llm_actions(
        positions=[position], review=review, run_id="r-44b",
    )

    pipeline.broker.submit_order.assert_called()
