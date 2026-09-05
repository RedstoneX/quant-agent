"""Spec item 25 — deterministic check on the RM's "Holding-discipline
compliance" checklist item (config/prompts/risk_manager.md).

The checklist item was 100% prompt-only: it asked the RM to verify, for a
SELL/REDUCE/COVER on a PROTECTED position, that the reasoning names one of
three allowed triggers — (a) thesis_invalid_if, (b) a regime flip to
risk-off TODAY, or (c) a same-day HIGH-conviction bearish state_change — and
nothing in Python checked the RM actually did that, or that the trigger
claimed is real. `holding_discipline_false_claim` covers ONLY (b) and (c):
it flags a claim that is POSITIVELY CONTRADICTED by real data, and never
flags anything just because it cannot be verified (which is the honest
posture for (a), never evaluated here at all).

"Protected" used to mean a flat `days_held < 5` window (no backtest behind
it, owner-rejected as arbitrary). It is now a plain `protected: bool` the
caller computes via `check_structural_protection` — this file tests
`holding_discipline_false_claim` in isolation with `protected` passed
directly; `test_structural_protection.py` covers the data-driven decision
itself (thesis_invalid_if, structural levels, the two-cycle confirmation
gate, and the noise-band fallback).

Each test names the exact hand-computed scenario it proves.
"""

from src.risk.exit_guard import (
    claims_bearish_state_change,
    claims_regime_flip,
    holding_discipline_false_claim,
)

TODAY = "2026-09-03"
YESTERDAY = "2026-09-02"


def _asc(date_str: str, event: str, symbols: dict[str, str]) -> str:
    """Build one `active_state_changes` row in the exact format
    `TradingPipeline._build_active_state_changes` renders, so the parser
    under test (`PortfolioManagerAgent._state_change_symbols_by_date`) is
    exercised against real production formatting, not an invented shape.
    """
    syms = ", ".join(f"{sym}({direction})" for sym, direction in symbols.items())
    return f"- [{date_str}] {event} → {syms}"


# ---------------------------------------------------------------------------
# 1. A claimed regime flip that real macro data CONTRADICTS -> caught.
# ---------------------------------------------------------------------------

def test_false_regime_flip_claim_is_caught():
    finding = holding_discipline_false_claim(
        action="SELL",
        reason="Selling ACME — macro regime flipped to risk-off today, "
               "de-risking ahead of the weekend.",
        symbol="ACME",
        protected=True,
        macro_regime_today="risk-on",   # real, trusted reading: NOT risk-off
        macro_status="ok",
        active_state_changes="",
    )
    assert finding is not None
    assert "ACME" in finding
    assert "risk-on" in finding
    assert "not risk-off" in finding


def test_false_bearish_state_change_claim_is_caught():
    # A same-day row exists for ACME, but it is recorded BULLISH, not
    # bearish — the claim is checkable and wrong. `asof` is pinned rather
    # than left to default to et_today() so the test does not depend on the
    # real clock.
    active = _asc(TODAY, "Guidance raise", {"ACME": "bullish"})
    from datetime import date
    finding = holding_discipline_false_claim(
        action="SELL",
        reason="Selling ACME on a high-conviction bearish state change "
               "reversing the entry thesis.",
        symbol="ACME",
        protected=True,
        macro_regime_today="risk-on",
        macro_status="ok",
        active_state_changes=active,
        asof=date.fromisoformat(TODAY),
    )
    assert finding is not None
    assert "ACME" in finding
    assert "bullish" in finding
    assert "not bearish" in finding or "instead of bearish" in finding


# ---------------------------------------------------------------------------
# 2. A SELL correctly citing a REAL, verifiable trigger -> NOT flagged.
# ---------------------------------------------------------------------------

def test_true_regime_flip_claim_is_not_flagged():
    finding = holding_discipline_false_claim(
        action="SELL",
        reason="Regime flipped to risk-off today per Macro; cutting risk.",
        symbol="ACME",
        protected=True,
        macro_regime_today="risk-off",   # matches the claim
        macro_status="ok",
        active_state_changes="",
    )
    assert finding is None


def test_true_bearish_state_change_claim_is_not_flagged():
    from datetime import date
    active = _asc(TODAY, "Regulatory crackdown announced", {"ACME": "bearish"})
    finding = holding_discipline_false_claim(
        action="SELL",
        reason="High-conviction bearish state change on ACME today directly "
               "reverses the entry thesis — exiting.",
        symbol="ACME",
        protected=True,
        macro_regime_today="risk-on",
        macro_status="ok",
        active_state_changes=active,
        asof=date.fromisoformat(TODAY),
    )
    assert finding is None


# ---------------------------------------------------------------------------
# 3. A SELL relying on (a) thesis_invalid_if, which cannot be verified here,
#    must NOT be flagged just because it can't be checked.
# ---------------------------------------------------------------------------

def test_thesis_invalid_if_reliance_is_never_flagged():
    finding = holding_discipline_false_claim(
        action="SELL",
        reason="thesis_invalid_if triggered: ACME closed below the $142 "
               "support level named at entry.",
        symbol="ACME",
        protected=True,
        macro_regime_today="risk-on",   # no regime-flip claim made
        macro_status="ok",
        active_state_changes="",        # no state-change claim made
    )
    assert finding is None


def test_unverifiable_regime_claim_is_not_flagged():
    """Macro data untrusted this run (e.g. failed) -> the claim cannot be
    checked, so it must not be treated as false."""
    finding = holding_discipline_false_claim(
        action="SELL",
        reason="Regime flipped to risk-off today; cutting risk.",
        symbol="ACME",
        protected=True,
        macro_regime_today=None,
        macro_status="failed",
        active_state_changes="",
    )
    assert finding is None


def test_unverifiable_state_change_claim_is_not_flagged():
    """No same-day row names the symbol at all — the news pipeline may
    simply not have logged it yet. Absence is not proof of falsity."""
    finding = holding_discipline_false_claim(
        action="SELL",
        reason="High-conviction bearish state change on ACME today.",
        symbol="ACME",
        protected=True,
        macro_regime_today="risk-on",
        macro_status="ok",
        active_state_changes="",   # nothing recorded at all
    )
    assert finding is None


# ---------------------------------------------------------------------------
# Scope guards
# ---------------------------------------------------------------------------

def test_unprotected_position_is_out_of_scope():
    """Owner replacement for the old flat day-count boundary test: a
    position that is NOT (structurally) protected needs no special
    justification for a plain exit, so nothing here is worth checking even
    though the reasoning names a checkable-and-false trigger."""
    finding = holding_discipline_false_claim(
        action="SELL",
        reason="Regime flipped to risk-off today.",
        symbol="ACME",
        protected=False,
        macro_regime_today="risk-on",
        macro_status="ok",
        active_state_changes="",
    )
    assert finding is None


def test_non_exit_action_is_out_of_scope():
    finding = holding_discipline_false_claim(
        action="HOLD",
        reason="Regime flipped to risk-off today.",
        symbol="ACME",
        protected=True,
        macro_regime_today="risk-on",
        macro_status="ok",
        active_state_changes="",
    )
    assert finding is None


def test_reason_naming_no_recognized_trigger_is_not_flagged():
    """No (b)/(c) claim in the text at all -> nothing to contradict."""
    finding = holding_discipline_false_claim(
        action="SELL",
        reason="Taking profits, thesis played out.",
        symbol="ACME",
        protected=True,
        macro_regime_today="risk-on",
        macro_status="ok",
        active_state_changes="",
    )
    assert finding is None


# ---------------------------------------------------------------------------
# Claim-detection helpers
# ---------------------------------------------------------------------------

def test_claims_regime_flip_detects_both_phrasings():
    assert claims_regime_flip("the regime flipped to risk-off")
    assert claims_regime_flip("macro is risk-off now")
    assert not claims_regime_flip("taking profits on a strong thesis")


def test_claims_bearish_state_change_detects_phrasing():
    assert claims_bearish_state_change("a high-conviction bearish state change fired")
    assert claims_bearish_state_change("high conviction bearish reversal")
    assert not claims_bearish_state_change("bullish state change reversal")


def test_a_denied_regime_flip_is_not_read_as_a_claim():
    """Adversarial review, 2026-09-03: this exact sentence was matched as
    CLAIMING a regime flip before the negation guard existed, which would
    have produced a wrong 'contradiction' finding against reasoning that
    never actually asserted one."""
    reason = "No regime shift to risk-off has occurred; exiting purely on thesis_invalid_if."
    assert not claims_regime_flip(reason)


def test_a_denied_bearish_state_change_is_not_read_as_a_claim():
    reason = "This is not a high-conviction bearish state change; unrelated exit."
    assert not claims_bearish_state_change(reason)


def test_holding_discipline_false_claim_does_not_fire_on_a_denied_claim():
    """End-to-end: the same denied-regime-flip sentence, run through the
    full check with real contradicting macro data, must NOT produce a
    finding — the reasoning agrees with the data (both say no flip); only
    an actual assertion contradicted by real data should ever be flagged."""
    finding = holding_discipline_false_claim(
        action="SELL",
        reason="No regime shift to risk-off has occurred; exiting purely on thesis_invalid_if.",
        symbol="AAPL",
        protected=True,
        macro_regime_today="risk-on",
        macro_status="ok",
    )
    assert finding is None
