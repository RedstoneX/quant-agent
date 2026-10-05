"""The fourth ledger state: unsettled, with no route yet.

These tests hold the line the state was built against. The defect it fixes
is real and was measured on 2026-10-05: five live trade-governing numbers
could not be entered in `config/number_ledger.yaml` AT ALL, because every
`arbitrary` row must name a settlement recording and the count of rows
without one is ratcheted to an equality. The only way past that was to write
down a route nobody intended to build, so the rule pressured its writers to
fabricate and the headline count flattered itself.

The risk in fixing it is that the new state becomes an escape hatch for rows
that already have a route. Tests 4 to 6 are the ones that close it, and
test 1 is the case that failed before and must still fail.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.number_sources import MIN_ROUTE_PROSE_CHARS, REPO_ROOT, audit, load_ledger
from src.number_unsettled import (
    drift_problems,
    is_unsettled,
    shape_problems,
    unanswered_sites,
    unsettled_count,
)

LONG_ENOUGH = (
    "Nothing in this desk records the evidence that would settle this number, and no "
    "such recording is specified anywhere."
)


def _arbitrary(**extra: object) -> dict[str, object]:
    entry: dict[str, object] = {
        "value": 0.5,
        "status": "arbitrary",
        "site": "src/example.py",
        "note": "A live number with nothing behind it.",
        "open_question": "What would settle it.",
        "cost_while_unanswered": "What it costs meanwhile.",
    }
    entry.update(extra)
    return entry


def _routed(**extra: object) -> dict[str, object]:
    return _arbitrary(
        settles_by={
            "kind": "recording",
            "state": "built",
            "records": "Every refusal, with the quantity that was refused.",
            "closes_when": "Thirty sessions of refusals exist to read the floor off.",
        },
        **extra,
    )


def _unsettled(why: str = LONG_ENOUGH, **extra: object) -> dict[str, object]:
    return _arbitrary(unsettled={"why_no_route": why}, **extra)


def _shape(ledger: dict[str, object]) -> list[str]:
    return [p.why for p in shape_problems(ledger, MIN_ROUTE_PROSE_CHARS)]


# 1. THE CASE THAT FAILED BEFORE AND MUST STILL FAIL. A route-less row that
#    says nothing at all is not excused by the existence of this state: it is
#    still in the population rule 8 of src/number_sources.py ratchets, and
#    adding the state did not give it a way out.
def test_a_silent_route_less_row_is_still_route_less() -> None:
    silent = {"mod.silent": _arbitrary()}
    assert not is_unsettled(silent["mod.silent"])
    assert unanswered_sites(silent) == {"mod.silent"}
    assert unsettled_count(silent) == 0
    assert _shape(silent) == []  # it is not a SHAPE error; it is a counted debt.


# 2. The state is for numbers with no source. A row that states a source and
#    then declares itself unsettled is claiming both at once.
def test_unsettled_on_a_sourced_row_fails() -> None:
    ledger = {"mod.sourced": _unsettled(status="sourced")}
    assert any("only an `arbitrary` row" in why for why in _shape(ledger))


def test_unsettled_beside_a_settlement_route_fails() -> None:
    ledger = {"mod.both": _routed(unsettled={"why_no_route": LONG_ENOUGH})}
    assert any("BOTH" in why for why in _shape(ledger))


# 3. A one-word reason is the same failure as a one-word settlement route:
#    it reads as a justification and is not one.
@pytest.mark.parametrize("why", ["", "none", "no route", "n/a"])
def test_a_reason_too_short_to_weigh_fails(why: str) -> None:
    ledger = {"mod.thin": _unsettled(why)}
    assert any("under" in text for text in _shape(ledger))


def test_an_unsettled_block_that_is_not_a_mapping_fails() -> None:
    ledger = {"mod.bare": _arbitrary(unsettled=True)}
    assert any("not a mapping" in why for why in _shape(ledger))


# 4. THE ESCAPE HATCH, closed. A row that already carries a settlement route
#    on trunk may not move into this state. Measured against trunk at check
#    time; no list of exempt rows is stored anywhere.
def test_a_routed_row_cannot_drift_into_unsettled() -> None:
    trunk = {"mod.routed": _routed()}
    now = {"mod.routed": _unsettled()}
    problems = drift_problems(now, trunk)
    assert "unsettled-drift" in [p.kind for p in problems]
    assert any("never lose one" in p.why for p in problems)


def test_a_sourced_row_cannot_drift_into_unsettled() -> None:
    trunk = {"mod.answered": {"value": 1.0, "status": "sourced", "source": "docs/OUTCOME.md"}}
    now = {"mod.answered": _unsettled()}
    assert "unsettled-drift" in [p.kind for p in drift_problems(now, trunk)]


# 5. The improvement direction is allowed: trunk's route-less residue --
#    arbitrary, no route, no written reason -- may gain a written reason.
def test_a_silent_route_less_row_may_become_recorded_unsettled() -> None:
    trunk = {"mod.silent": _arbitrary()}
    now = {"mod.silent": _unsettled()}
    assert drift_problems(now, trunk) == []


# 6. THE DOWNWARD RATCHET, over rows trunk already carries. Nothing stored:
#    both sides are counted from the two trees at check time.
def test_the_unanswered_population_may_not_rise_on_existing_rows() -> None:
    trunk = {"mod.a": _routed(), "mod.b": _arbitrary()}
    now = {"mod.a": _routed(), "mod.b": _arbitrary(), "mod.c": _arbitrary()}
    # `mod.c` is new to the register, so it is exempt and the count holds.
    assert drift_problems(now, trunk) == []
    # Take `mod.a`'s route away and the population of known rows rises.
    worse = {"mod.a": _arbitrary(), "mod.b": _arbitrary()}
    kinds = [p.kind for p in drift_problems(worse, trunk)]
    assert "unsettled-ratchet" in kinds


def test_the_population_falling_is_always_allowed() -> None:
    trunk = {"mod.a": _unsettled(), "mod.b": _arbitrary()}
    now = {"mod.a": _routed(), "mod.b": _arbitrary()}
    assert drift_problems(now, trunk) == []


# 7. THE ROW THAT COULD NOT BE WRITTEN BEFORE. A genuinely unsettled number,
#    new to the register, with its reason written out: no problems at all.
def test_a_genuinely_unsettled_new_number_can_now_be_recorded() -> None:
    now = {"mod.new": _unsettled()}
    assert _shape(now) == []
    assert drift_problems(now, {}) == []
    assert unsettled_count(now) == 1


# 8. The live register. Green, and the five rows are really there.
def test_the_live_register_is_green_and_records_the_five() -> None:
    assert audit() == []
    ledger = load_ledger()
    unsettled = sorted(site for site, entry in ledger.items() if is_unsettled(entry))
    assert len(unsettled) == 5, unsettled
    for site in unsettled:
        assert ledger[site]["status"] == "arbitrary", site
        assert ledger[site].get("settles_by") is None, site
        assert (REPO_ROOT / Path(str(ledger[site]["site"]))).exists(), site


# 9. RULE 3, which replaced the stored count. A row trunk does not carry may
#    never arrive SILENTLY route-less: that is the case the deleted equality
#    in config/number_ledger_route_history.yaml used to catch, and it still
#    fails -- while the honest declaration it blocked now passes.
def test_a_new_silent_route_less_row_is_refused() -> None:
    from src.number_unsettled import silence_problems

    trunk = {"mod.old": _routed()}
    now = {"mod.old": _routed(), "mod.new": _arbitrary()}
    problems = silence_problems(now, trunk)
    assert [p.kind for p in problems] == ["unsettled-silence"]
    assert problems[0].site_id == "mod.new"


def test_a_new_row_declaring_itself_unsettled_is_allowed() -> None:
    from src.number_unsettled import silence_problems

    trunk = {"mod.old": _routed()}
    now = {"mod.old": _routed(), "mod.new": _unsettled()}
    assert silence_problems(now, trunk) == []


def test_a_pre_existing_silent_row_is_grandfathered_but_may_not_multiply() -> None:
    from src.number_unsettled import silence_problems

    trunk = {"mod.silent": _arbitrary()}
    assert silence_problems({"mod.silent": _arbitrary()}, trunk) == []
    worse = {"mod.silent": _arbitrary(), "mod.second": _arbitrary()}
    assert [p.site_id for p in silence_problems(worse, trunk)] == ["mod.second"]
