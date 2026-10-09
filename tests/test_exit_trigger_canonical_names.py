"""A sanctioned `ExitTrigger` must be namable by its own canonical value.

THE LIVE DEFECT THIS PINS (2026-09-25 17:06:23, META).

The position reviewer emitted a REDUCE on META whose reason began, verbatim
from the live DB row:

    bearish_state_change: [HIGH] U.S. 10-year Treasury yield crosses 5%
    (2026-09-25), bearish for long-duration growth assets SPY/QQQ/DIA; macro
    bullish entry thesis context reversed; weight 35.9% (oversized);
    to_target 4.8% signals profit-taking inflection

The desk refused it with `exit_blocked_no_named_trigger` — "the reason names
no recognised trigger" — while `ExitTrigger.BEARISH_STATE_CHANGE` was a
sanctioned trigger and `exit_guard.claims_bearish_state_change` accepted the
phrase. Three modules, two answers. `pipeline._HARD_TRIGGER_KEYWORDS` carried
only the WORDINGS "high bearish" / "high(-)conviction bearish", and the seat
had written the canonical name.

These tests make that class of divergence impossible to reintroduce
silently: the gate's vocabulary is now derived from the enum, and the only
members not matched by their canonical name in prose are the ones named in
`CANONICAL_NAME_NOT_MATCHED_IN_PROSE`, which is asserted member-by-member
below rather than left to whatever the code happens to do.
"""

import pytest

from src.pipeline_exits import _HARD_TRIGGER_KEYWORDS, _reason_cites_hard_trigger
from src.risk.exit_refusal import classify_trigger_reason
from src.risk.exit_trigger import (
    CANONICAL_NAME_NOT_MATCHED_IN_PROSE,
    CANONICAL_TRIGGER_NAMES,
    EVENT_TRIGGERS,
    NO_VERIFIER_EXISTS,
    VERIFIED_ON_CHART,
    ExitTrigger,
    TRIGGER_PHRASES,
    derive_trigger_from_reason,
)

#: The verbatim live reason. Not paraphrased — the point is the real row.
META_REASON = (
    "bearish_state_change: [HIGH] U.S. 10-year Treasury yield crosses 5% "
    "(2026-09-25), bearish for long-duration growth assets SPY/QQQ/DIA; "
    "macro bullish entry thesis context reversed; weight 35.9% (oversized); "
    "to_target 4.8% signals profit-taking inflection"
)

#: Every sanctioned trigger — everything except the honest decline, which is
#: not a trigger and must never be accepted as one.
SANCTIONED = tuple(t for t in ExitTrigger if t is not ExitTrigger.CANNOT_SUBSTANTIATE)


def _classify(reason, trigger=None, trigger_evidence=None):
    return classify_trigger_reason(
        reason,
        cites=_reason_cites_hard_trigger,
        trigger=trigger,
        trigger_evidence=trigger_evidence,
    )


def test_a_sanctioned_trigger_name_in_the_live_meta_reason_is_recognised():
    """PINS THE NAMING DEFECT ONLY. THIS EXIT WAS NOT CORRECT.

    Read literally, this asserts one thing: a reason that opens with the
    canonical value of a sanctioned `ExitTrigger` is no longer judged to
    name no trigger. That is the defect #791 fixes.

    IT DOES NOT ASSERT THAT THE META EXIT SHOULD HAVE EXECUTED, and the
    earlier name of this test (`..._is_no_longer_refused`) did, which would
    have made a partly-banned rationale a permanent fixture of correctness.
    Three things are wrong with that reason on the desk's own doctrine:

    * the 10-year yield level it cites names SPY/QQQ/DIA — index proxies —
      and not META, so the state change it asserts is not about the symbol;
    * "weight 35.9% (oversized)" is a BALANCE rationale, and the owner has
      ruled conviction outranks balance: the desk never trades to balance
      or diversify;
    * "to_target 4.8% signals profit-taking inflection" is a TARGET
      rationale, and the owner has ruled a target is a made-up number and
      never a trigger. Exit on ALIGNMENT, never on a target.

    The gate is an OR over substrings, so the one sanctioned phrase carries
    the other two through. Refusing this exit for the RIGHT reason and
    refusing it for the WRONG reason are different failures; #791 fixes
    only the second. Whether the remaining three rationales should be
    caught is a separate, unclosed question — do not read this test as
    saying they are fine.
    """
    assert _classify(META_REASON) == "named"
    assert derive_trigger_from_reason(META_REASON) is ExitTrigger.BEARISH_STATE_CHANGE


@pytest.mark.parametrize("spelling", ["bearish_state_change", "bearish state change"])
def test_both_spellings_of_the_canonical_name_are_recognised(spelling):
    assert _reason_cites_hard_trigger(f"{spelling}: the 10y crossed 5% today")


#: Evidence that says something beyond the trigger's own phrases. Not a
#: length or quality test at the gate — see `_evidence_is_substantiation`.
EVIDENCE = "state_change row 2026-09-25 for the symbol, direction bearish"


@pytest.mark.parametrize("trigger", SANCTIONED, ids=lambda t: t.value)
def test_every_sanctioned_trigger_is_classifiable_by_the_structured_field(trigger):
    """No PROSE exclusions on this path — a seat that fills the typed field
    with a sanctioned trigger AND says what it rests on has named one."""
    assert (
        _classify(
            "no recognised wording here at all",
            trigger=trigger,
            trigger_evidence=EVIDENCE,
        )
        == "named"
    )
    assert _classify("", trigger=trigger.value, trigger_evidence=EVIDENCE) == "named"


def test_the_bare_structured_field_does_not_settle_the_judgment():
    """THE ONLY GATE THAT DROPS AN EXIT MUST NOT BE SETTLED BY AN ENUM VALUE
    WITH NOTHING BEHIND IT.

    `check_exit_trigger` refuses nothing (its docstring says so) and the
    pipeline files its post-re-ask finding with `dropped=False`, so this
    judgment is the only step on the exit path that actually drops an exit
    for paperwork. The first cut of #791 let the typed field settle it
    alone. The reason below is the verbatim shape the comment block above
    `pipeline._HARD_TRIGGER_KEYWORDS` records as DELIBERATELY REJECTED (the
    2026-05-04 AMZN double-trim), plus a target rationale the owner has
    ruled is never a trigger — three banned rationales, one empty field.
    """
    laundered = "Concentration drift; valuation stretched; taking profits at target."
    assert _classify(laundered) == "unnamed"
    assert _classify(laundered, trigger="adverse_news") == "unnamed"
    assert _classify(laundered, trigger="adverse_news", trigger_evidence="") == "unnamed"
    # Evidence that only recites the trigger's own phrases is not evidence.
    assert (
        _classify(
            laundered,
            trigger="adverse_news",
            trigger_evidence="adverse news",
        )
        == "unnamed"
    )
    # With a real record behind it, the seat has named and supported one.
    assert (
        _classify(
            laundered,
            trigger="adverse_news",
            trigger_evidence=EVIDENCE,
        )
        == "named"
    )


def test_a_field_without_evidence_still_falls_through_to_the_prose_gate():
    """Declining to settle is not the same as refusing. The prose gate runs
    exactly as it did, which is why the live META reason still passes with
    no evidence field at all."""
    assert _classify(META_REASON, trigger="adverse_news") == "named"


@pytest.mark.parametrize("trigger", SANCTIONED, ids=lambda t: t.value)
def test_every_sanctioned_trigger_is_classifiable_by_its_canonical_name_in_prose(
    trigger,
):
    """...except the members on the EXPLICIT, NAMED exclusion list, and for
    those this test asserts the exclusion holds rather than skipping it."""
    reason = f"{trigger.value}: recorded today, see the evidence field"
    if trigger in CANONICAL_NAME_NOT_MATCHED_IN_PROSE or trigger in VERIFIED_ON_CHART:
        # A CHART-VERIFIED TRIGGER IS DELIBERATELY NOT NAMABLE IN PROSE.
        # `_reason_cites_hard_trigger` is a BYPASS: it waves a reason past
        # the SELL/REDUCE noise band and past the TRAIL_STOP ratchet
        # cooldown and 1.25xATR clamp, and the trail path runs no chart
        # check at all. Letting the alignment exit's own name buy that
        # bypass would mean model prose alone loosened a live stop. It
        # stays classifiable via the STRUCTURED field (asserted above),
        # which is the path the desk actually fills.
        assert _classify(reason) == "unnamed"
    else:
        assert _classify(reason) == "named"
        assert derive_trigger_from_reason(reason) is trigger


def test_the_prose_exclusion_list_is_exactly_these_two_and_says_why():
    """Pinned by value. Widening it is a deliberate act, not a drift.

    `cannot_substantiate` is not a trigger; `earnings` is a bare common word
    that occurs in prose naming no event ("earnings in three days"), and
    admitting it as a substring would widen the gate to a non-event.
    """
    assert CANONICAL_NAME_NOT_MATCHED_IN_PROSE == frozenset(
        {
            ExitTrigger.CANNOT_SUBSTANTIATE,
            ExitTrigger.EARNINGS,
        }
    )


def test_the_honest_decline_never_passes_the_gate():
    assert _classify("cannot_substantiate: I cannot support this exit") == "unnamed"
    assert _classify("stalling", trigger=ExitTrigger.CANNOT_SUBSTANTIATE) == "unnamed"
    assert _classify("stalling", trigger="cannot_substantiate") == "unnamed"


#: THE SINGLE COPY OF THIS INVARIANT. It lived here AND, unsubtracted and
#: therefore stale, in `tests/test_exit_trigger_substantiation.py`; the
#: chart-verified carve-out was added to this copy only and the other copy
#: failed the next branch that touched the vocabulary. Two hand-kept copies
#: of one rule is how that happens, so there is now one function and the
#: other file imports it. Do not inline it back.
def assert_trigger_vocabulary_matches_executor_gate() -> None:
    """`TRIGGER_PHRASES` is a REGROUPING of `pipeline._HARD_TRIGGER_KEYWORDS`,
    never a second list -- except for the CHART-VERIFIED names, which are
    namable in the phrase table but must never be hard-trigger keywords,
    because a keyword is a bypass bought with prose alone."""
    from src.pipeline_exits import _CHART_VERIFIED_TRIGGER_NAMES

    for name in CANONICAL_TRIGGER_NAMES:
        if name in _CHART_VERIFIED_TRIGGER_NAMES:
            continue  # see the prose-exclusion note above: a bypass, not a name
        assert name in _HARD_TRIGGER_KEYWORDS
    assert "earnings" not in _HARD_TRIGGER_KEYWORDS
    assert "cannot_substantiate" not in _HARD_TRIGGER_KEYWORDS
    assert not (_CHART_VERIFIED_TRIGGER_NAMES & set(_HARD_TRIGGER_KEYWORDS))
    grouped = {p for phrases in TRIGGER_PHRASES.values() for p in phrases} - _CHART_VERIFIED_TRIGGER_NAMES
    assert grouped == set(_HARD_TRIGGER_KEYWORDS)


def test_canonical_names_reached_the_phrase_tuple_and_nothing_else_did():
    """The gate's vocabulary is the enum's, not a second hand-kept list."""
    assert_trigger_vocabulary_matches_executor_gate()


@pytest.mark.parametrize("trigger", list(ExitTrigger), ids=lambda t: t.value)
def test_every_trigger_is_recorded_as_verifiable_or_explicitly_not(trigger):
    """THE BAR, AS A TEST INSTEAD OF AS A PARAGRAPH.

    The comment block above `pipeline._HARD_TRIGGER_KEYWORDS` says an
    accepted trigger must name "something the desk records" and must not be
    re-added "without a verifier that can answer 'did that happen today?'".
    That bar was prose, and `EVENT_TRIGGERS` — the set meant to encode it —
    was read by NOTHING: a grep of `src/` and `tests/` on 2026-09-30
    returned only its own definition and `__all__`. It had drifted wrong on
    `sector_shock` with nothing to notice.

    Declaring a new `ExitTrigger` now fails this test until whoever
    declares it says, in code, whether anything can check it. Being in
    `NO_VERIFIER_EXISTS` is a legitimate answer and records the gap; being
    in neither is not an answer at all.
    """
    in_event = trigger in EVENT_TRIGGERS or trigger in VERIFIED_ON_CHART
    in_none = trigger in NO_VERIFIER_EXISTS
    assert in_event != in_none, (
        f"{trigger.value} is in "
        f"{'both' if in_event else 'neither'} EVENT_TRIGGERS and "
        f"NO_VERIFIER_EXISTS. Say which, in code, with a reason."
    )
    if in_none:
        assert len(NO_VERIFIER_EXISTS[trigger].split()) >= 8, (
            f"{trigger.value} needs a stated reason, not a placeholder."
        )


def test_the_recorded_verifier_gap_is_four_of_the_seven_triggers():
    """The measured state as of 2026-09-30, pinned so a silent change shows.

    Not a target and not a budget — four is what the code does. Moving a
    member OUT of `NO_VERIFIER_EXISTS` means a verifier was built; moving
    one IN means a claim was found unverifiable. Either is a deliberate act
    and updates this list.
    """
    assert set(NO_VERIFIER_EXISTS) == {
        ExitTrigger.THESIS_INVALID,  # consulted advisory-only, never judged
        ExitTrigger.SECTOR_SHOCK,  # desk records no sector-scope row
        ExitTrigger.EARNINGS,  # no branch reads an earnings row
        ExitTrigger.STOP_FIRED,  # nothing asks the broker for the fill
        ExitTrigger.CANNOT_SUBSTANTIATE,  # not a trigger; nothing to verify
    }
    assert EVENT_TRIGGERS == frozenset(
        {
            ExitTrigger.BEARISH_STATE_CHANGE,
            ExitTrigger.ADVERSE_NEWS,
            ExitTrigger.REGIME_SHIFT,
        }
    )


def test_clamp_bypass_divergence_is_pinned_per_trigger():
    """THE FOURTH VOCABULARY, AND THE FACT THAT THIS PR WIDENS THE SPLIT.

    `exit_guard.EXTERNAL_INFORMATION_PATTERNS` is a FOURTH hand-written
    list, and it is the one that matters most: `cites_external_information`
    waves a SELL/REDUCE/COVER past the noise band and the ratchet clamp
    when the reason matches it. #791 does not touch that list, so deriving
    the canonical enum names into the phrase gate splits the two further
    apart: `"adverse news"` bypasses the clamps, `"adverse_news"` passes the
    named-trigger gate and does not. Same claim, two behaviours, decided by
    an underscore.

    Unifying them was considered and NOT done here: adding the canonical
    spellings to `EXTERNAL_INFORMATION_PATTERNS` would WIDEN a clamp
    bypass, which is a live-money change and a separate, ratifiable
    decision — not a side effect of fixing a naming gate.

    So the divergence is RECORDED instead, per trigger, and any NEW
    divergence fails here. Every count in this test is recomputed from the
    code; no figure is written in prose, which is how the "26 hard-trigger
    keywords" in `pipeline.py` came to be wrong twice over.
    """
    import re

    from src.risk.exit_guard import EXTERNAL_INFORMATION_PATTERNS

    def bypasses(phrase: str) -> bool:
        return any(re.search(p, phrase) for p in EXTERNAL_INFORMATION_PATTERNS)

    split = {
        t.value
        for t, phrases in TRIGGER_PHRASES.items()
        if any(bypasses(p) for p in phrases) and not all(bypasses(p) for p in phrases)
    }
    assert split == {
        "bearish_state_change",
        "adverse_news",
        "sector_shock",
        "regime_shift",
        "stop_fired",
    }, (
        "A trigger's phrases must be all-clamp-bypassing or none, or the "
        "divergence must be recorded here on purpose. New entry means a "
        "keyword now behaves differently from its own synonyms."
    )
    # Triggers whose phrases agree with themselves, either way.
    coherent = {t.value for t in TRIGGER_PHRASES} - split
    assert coherent == {"thesis_invalid", "earnings", "trend_alignment_over"}


def test_no_soft_signal_was_admitted_by_the_widening():
    """The banned soft flags stay banned — this change must not have let one
    in through the enum."""
    joined = " ".join(_HARD_TRIGGER_KEYWORDS).lower()
    for banned in (
        "target",
        "stretched",
        "extended",
        "taking profits",
        "profit",
        "de-risk",
        "concentration",
        "drift",
        "correlation",
        "downgrade",
        "circuit breaker",
        "daily loss",
    ):
        assert banned not in joined
