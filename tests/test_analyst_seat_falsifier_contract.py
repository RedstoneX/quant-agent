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
from pathlib import Path

import pytest

from src import models

PROMPT_DIR = Path(__file__).resolve().parents[1] / "config" / "prompts"

#: seat prompt stem -> does the seat state its own falsifier today?
#: Measured 2026-09-30 against the production database (see module docstring).
ANALYST_SEATS_STATE_A_FALSIFIER: dict[str, bool] = {
    "tech_analyst": True,
    "news_analyst": False,
    "earnings_analyst": False,
    "macro_analyst": False,
    "smart_money_analyst": False,
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
    "class_name",
    ["MacroAnalysis", "EarningsAnalysis", "SmartMoneySynthesis"],
)
def test_uncovered_seats_have_not_silently_gained_the_field(class_name: str) -> None:
    """These seats state no falsifier today; gaining one must be deliberate."""
    cls = getattr(models, class_name)
    assert FALSIFIER_FIELD not in getattr(cls, "model_fields", {}), (
        f"{class_name} gained `{FALSIFIER_FIELD}`. That closes part of item "
        "99: flip this seat's entry in ANALYST_SEATS_STATE_A_FALSIFIER, make "
        "the prompt ask for it, and record the change in docs/WORK.md."
    )


def test_nomination_carries_no_stated_falsifier() -> None:
    """Nominations are how the four uncovered seats reach the conviction bar.

    0 of 103 recorded nominations carry a falsifier (production DB,
    2026-09-30). If one ever does, the downstream synthesis that stands in
    for it must stop overwriting the seat's own words.
    """
    assert FALSIFIER_FIELD not in getattr(models.Nomination, "model_fields", {}), (
        "Nomination gained a stated falsifier. Check that the downstream "
        "synthesised invalidation no longer overwrites it before flipping "
        "this test."
    )
