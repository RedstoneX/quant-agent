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

from src.pipeline import _HARD_TRIGGER_KEYWORDS, _reason_cites_hard_trigger
from src.risk.exit_refusal import classify_trigger_reason
from src.risk.exit_trigger import (
    CANONICAL_NAME_NOT_MATCHED_IN_PROSE,
    CANONICAL_TRIGGER_NAMES,
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


def _classify(reason, trigger=None):
    return classify_trigger_reason(
        reason, cites=_reason_cites_hard_trigger, trigger=trigger,
    )


def test_the_live_meta_reason_is_no_longer_refused():
    assert _classify(META_REASON) == "named"
    assert derive_trigger_from_reason(META_REASON) is ExitTrigger.BEARISH_STATE_CHANGE


@pytest.mark.parametrize("spelling", ["bearish_state_change", "bearish state change"])
def test_both_spellings_of_the_canonical_name_are_recognised(spelling):
    assert _reason_cites_hard_trigger(f"{spelling}: the 10y crossed 5% today")


@pytest.mark.parametrize("trigger", SANCTIONED, ids=lambda t: t.value)
def test_every_sanctioned_trigger_is_classifiable_by_the_structured_field(trigger):
    """No exclusions at all on this path. A seat that fills the typed field
    with a sanctioned trigger has named one."""
    assert _classify("no recognised wording here at all", trigger=trigger) == "named"
    assert _classify("", trigger=trigger.value) == "named"


@pytest.mark.parametrize("trigger", SANCTIONED, ids=lambda t: t.value)
def test_every_sanctioned_trigger_is_classifiable_by_its_canonical_name_in_prose(
    trigger,
):
    """...except the members on the EXPLICIT, NAMED exclusion list, and for
    those this test asserts the exclusion holds rather than skipping it."""
    reason = f"{trigger.value}: recorded today, see the evidence field"
    if trigger in CANONICAL_NAME_NOT_MATCHED_IN_PROSE:
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
    assert CANONICAL_NAME_NOT_MATCHED_IN_PROSE == frozenset({
        ExitTrigger.CANNOT_SUBSTANTIATE, ExitTrigger.EARNINGS,
    })


def test_the_honest_decline_never_passes_the_gate():
    assert _classify("cannot_substantiate: I cannot support this exit") == "unnamed"
    assert _classify("stalling", trigger=ExitTrigger.CANNOT_SUBSTANTIATE) == "unnamed"
    assert _classify("stalling", trigger="cannot_substantiate") == "unnamed"


def test_canonical_names_reached_the_phrase_tuple_and_nothing_else_did():
    """The gate's vocabulary is the enum's, not a second hand-kept list."""
    for name in CANONICAL_TRIGGER_NAMES:
        assert name in _HARD_TRIGGER_KEYWORDS
    assert "earnings" not in _HARD_TRIGGER_KEYWORDS
    assert "cannot_substantiate" not in _HARD_TRIGGER_KEYWORDS
    grouped = {p for phrases in TRIGGER_PHRASES.values() for p in phrases}
    assert grouped == set(_HARD_TRIGGER_KEYWORDS)


def test_no_soft_signal_was_admitted_by_the_widening():
    """The banned soft flags stay banned — this change must not have let one
    in through the enum."""
    joined = " ".join(_HARD_TRIGGER_KEYWORDS).lower()
    for banned in (
        "target", "stretched", "extended", "taking profits", "profit",
        "de-risk", "concentration", "drift", "correlation", "downgrade",
        "circuit breaker", "daily loss",
    ):
        assert banned not in joined


