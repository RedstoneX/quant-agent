"""Item 99 — the analyst seats' falsifier contract, pinned mechanically.

Measured against the production database read-only on 2026-09-30:

* `tech_analyst` is the ONLY analyst seat that emits a falsifier. Over 3,845
  recorded `specialist_evidence` analysis rows its `thesis_invalid_if` is
  non-empty on 1,664 of 1,665 pre-2026-09-25 ACTIONABLE ratings and on 195 of
  195 actionable ratings since. It is empty on 100% of NEUTRAL ratings, which
  is what the prompt and `TechAnalystAnswerItem` both REQUIRE — a neutral is
  the absence of a call, so there is nothing to falsify. Any headline
  "~60% blank" rate is that neutral population and is not a defect.
* `news_analyst`, `earnings_analyst`, `macro_analyst` and `smart_money_analyst`
  emit NO falsifier at all: 0 of 103 recorded nominations across those seats
  carry the field, no answer schema defines one, and no prompt asks for one.
  Their invalidation is synthesised downstream instead of stated by the seat.

This file does not decide whether those four seats SHOULD state a falsifier —
that is the open half of item 99. It pins the current, measured truth so that:

1. the one seat that does state a falsifier cannot silently stop being asked
   for it (prompt rot: prompt text is code), and
2. a seat cannot quietly gain or lose the field without this registry — and
   therefore item 99 — being updated in the same change.
"""

from __future__ import annotations

import inspect
from datetime import date
from pathlib import Path

import pytest

from src import models

PROMPT_DIR = Path(__file__).resolve().parents[1] / "config" / "prompts"

#: seat prompt stem -> does the seat state its own falsifier today?
#: Measured 2026-09-30 against the production database (see module docstring).
ANALYST_SEATS_STATE_A_FALSIFIER: dict[str, bool] = {
    "tech_analyst": True,
    # Flipped 2026-09-30 (item 99, the build half): each of these four seats
    # now states its own falsifier at call time. News, Earnings and Macro
    # state it per nomination (`Nomination.thesis_invalid_if`); Smart Money
    # states it per finding (`SmartMoneyFinding.thesis_invalid_if`). Both use
    # the SAME field name and the same plain-string shape the technical seat
    # uses, so `exit_guard.check_thesis_invalid_if` consumes all five
    # unchanged. A seat that gives none leaves the slot empty and the gap is
    # recorded as a gap — nothing downstream fills it in.
    "news_analyst": True,
    "earnings_analyst": True,
    "macro_analyst": True,
    "smart_money_analyst": True,
}

#: Which model class carries the falsifier for each seat.
SEAT_FALSIFIER_CARRIER: dict[str, str] = {
    "tech_analyst": "TechAnalystAnswerItem",
    "news_analyst": "Nomination",
    "earnings_analyst": "Nomination",
    "macro_analyst": "Nomination",
    "smart_money_analyst": "SmartMoneyFinding",
}

#: The field name the desk uses for a stated falsifier everywhere.
FALSIFIER_FIELD = "thesis_invalid_if"


def _prompt_text(stem: str) -> str:
    path = PROMPT_DIR / f"{stem}.md"
    assert path.is_file(), f"analyst prompt missing: {path}"
    return path.read_text(encoding="utf-8")


@pytest.mark.parametrize("stem,states", sorted(ANALYST_SEATS_STATE_A_FALSIFIER.items()))
def test_prompt_matches_the_registry(stem: str, states: bool) -> None:
    """A seat's prompt asks for a falsifier exactly when the registry says so."""
    text = _prompt_text(stem)
    mentions = FALSIFIER_FIELD in text
    if states:
        assert mentions, (
            f"{stem}.md no longer mentions `{FALSIFIER_FIELD}`. This seat is "
            "recorded as stating its own falsifier; the prompt is the only "
            "place it is asked for. Either restore the instruction or flip "
            "the registry in this file AND update item 99 in docs/WORK.md."
        )
    else:
        assert not mentions, (
            f"{stem}.md now mentions `{FALSIFIER_FIELD}`, but this seat is "
            "registered as not stating a falsifier. If the seat is being "
            "given one, flip the registry here, give its answer schema the "
            "field, and close that part of item 99 in docs/WORK.md."
        )


def test_tech_prompt_still_requires_it_on_actionable_and_forbids_it_on_neutral() -> None:
    """The measured split (actionable non-empty, neutral empty) is instructed."""
    text = _prompt_text("tech_analyst").lower()
    assert "non-empty on every actionable rating" in text, (
        "tech_analyst.md no longer requires a non-empty falsifier on every "
        "actionable rating — the one enforcement this desk actually has on a "
        "stated falsifier. 0 of 195 actionable calls since 2026-09-25 were "
        "blank; that rate is held up by this sentence."
    )
    assert "leave `thesis_invalid_if` empty" in text, (
        "tech_analyst.md no longer tells the seat to leave the falsifier "
        "empty on a neutral rating. Without it the seat invents a falsifier "
        "for a call it did not make."
    )


def test_code_enforces_what_the_tech_prompt_promises() -> None:
    """The prompt's promise is backed by a validator, not by hope."""
    assert FALSIFIER_FIELD in models.TechAnalystAnswerItem.model_fields, (
        "TechAnalystAnswerItem no longer declares `thesis_invalid_if`; the "
        "tech_analyst prompt still asks for it. Prompt and schema have drifted."
    )
    source = inspect.getsource(models.TechAnalysisResult)
    assert "missing_stated_falsifier" in source, (
        "TechAnalysisResult no longer checks the stated falsifier on an "
        "actionable rating. The prompt's 'required and non-empty' sentence "
        "would then be unenforced prose — exactly the class of defect item 99 "
        "was filed for."
    )


@pytest.mark.parametrize(
    "stem",
    sorted(ANALYST_SEATS_STATE_A_FALSIFIER),
)
def test_each_seats_carrier_declares_the_field(stem: str) -> None:
    """Prompt and schema must agree: asked for AND stored, on every seat.

    This is the half of item 99 that was open until 2026-09-30. It fails if
    any of the five seats silently stops being asked, or is asked but has
    nowhere to put the answer.
    """
    cls = getattr(models, SEAT_FALSIFIER_CARRIER[stem])
    assert FALSIFIER_FIELD in cls.model_fields, (
        f"{SEAT_FALSIFIER_CARRIER[stem]} no longer declares "
        f"`{FALSIFIER_FIELD}`, but {stem}.md still asks for it. Prompt text "
        "is code: prompt and schema have drifted apart."
    )


def test_stored_falsifier_is_the_shape_the_exit_checker_reads() -> None:
    """A seat's stated condition goes into the exit checker unchanged.

    The point of storing it is that `check_thesis_invalid_if` can read it.
    A price-level condition must evaluate; a qualitative one must come back
    UNPARSEABLE with a reason rather than be treated as passed.
    """
    from src.risk.exit_guard import check_thesis_invalid_if

    priced = models.Nomination(
        symbol="JPM",
        conviction="medium",
        observation="regime turn",
        thesis_invalid_if="closes below the $180 level",
    )
    assert (
        check_thesis_invalid_if(
            priced.thesis_invalid_if,
            170.0,
        ).status
        == "TRIGGERED"
    )
    assert (
        check_thesis_invalid_if(
            priced.thesis_invalid_if,
            190.0,
        ).status
        == "NOT_TRIGGERED"
    )

    qualitative = models.Nomination(
        symbol="NVDA",
        conviction="high",
        observation="contract award",
        thesis_invalid_if="the award is rescinded",
    )
    result = check_thesis_invalid_if(qualitative.thesis_invalid_if, 100.0)
    assert result.status == "UNPARSEABLE", (
        "a qualitative falsifier must be reported as unevaluated, never as "
        "a passed check — an unevaluable condition that looks evaluated is "
        "worse than no condition at all"
    )
    assert result.detail


def test_missing_falsifier_is_recorded_as_missing_not_invented() -> None:
    """No template stands in for a seat that gave nothing."""
    silent = models.Nomination(
        symbol="AAPL",
        conviction="low",
        observation="in-line filing",
    )
    assert silent.thesis_invalid_if == ""
    assert models.missing_stated_falsifier(silent.thesis_invalid_if)
    from src.risk.exit_guard import check_thesis_invalid_if

    assert (
        check_thesis_invalid_if(
            silent.thesis_invalid_if,
            100.0,
        ).status
        == "UNPARSEABLE"
    )


def test_neutral_smart_money_finding_keeps_the_slot_empty() -> None:
    """A stance with no call has nothing to disprove (same rule as Tech)."""
    obs = models.SmartMoneyObservation(
        symbol="AAPL",
        stream="insider",
        actor="Example Officer",
        direction="buy",
        amount_range="$50,001-$100,000",
        transaction_date=date(2026, 8, 20),
        disclosure_date=date(2026, 8, 20),
        source_url="https://example.test/filing",
        lag_days=2,
        disclosure_age_days=1,
        freshness="fresh",
        economic_role="historical",
    )
    neutral = models.SmartMoneyFinding(
        symbol="AAPL",
        stance="neutral",
        economic_role="historical",
        summary="no directional read",
        why_now="single stale filing",
        observations=[obs],
        thesis_invalid_if="this should not survive",
    )
    assert neutral.thesis_invalid_if == ""
    assert "leave it empty" in _prompt_text("smart_money_analyst").lower()


def test_nomination_prompts_forbid_a_placeholder_falsifier() -> None:
    """Every nominating seat is told not to invent one."""
    for stem in ("news_analyst", "earnings_analyst", "macro_analyst", "smart_money_analyst"):
        text = _prompt_text(stem).lower()
        assert "never write a generic placeholder" in text, (
            f"{stem}.md no longer forbids a placeholder falsifier. Without "
            "that sentence the seat fills the slot with boilerplate and the "
            "desk records protection it does not have."
        )
