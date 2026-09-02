"""Spec §9.4 — the signed sum, and the pin that keeps its weights at one.

Two jobs, and they are different jobs.

**The acceptance criterion.** Signing the sum was allowed to change how a
CONTESTED name is sized. It was not allowed to change anything else. So the
first half of this file pins the property that made the change shippable: with
nothing opposed, the signed score IS the aligned count, and every rung of
`risk.agreement_ceiling_pct` must therefore price exactly what it priced
before. The ratified risk envelope is unchanged for unanimous evidence, which
is nearly all of it — measured over the 12 most recent runs in the local
snapshot, 25 of 28 sized targets are untouched.

**The weight pin.** The second half is a mechanical guard, not a comment.
`src/conviction_ledger.py` records the desk's standing rule (owner,
2026-08-31): a confidence weight may only be DERIVED from an analyst's own
measured history, never chosen up front. `_CONVICTION_OUTCOME_MIN_N`
(`src/storage/db.py`) puts the minimum sample at 20 resolved calls. The book
has 7 closed equity round-trips, all carrying conviction NULL, so there is no
history to derive from and any weight introduced today would be a chosen one.

That pin is *why* the dissent change could ship before the conviction question
is settled: with every seat at unit magnitude there is no constant for the
dissent rule to inherit, so "how much does a dissenter subtract?" has exactly
one answer and it needs no owner ruling. Introduce a per-seat weight and that
stops being true — a `-1` becomes a `-0.3` or a `-1.5` and the signed sum
silently acquires an unratified parameter.

Everything mechanically enforced holds; everything relying on remembering a
rule slips. So this is enforced two ways: BEHAVIOURALLY (every seat must move
the score by exactly one, whatever the implementation looks like and wherever
a weight might be read from — including config, which no static scan would
see) and STRUCTURALLY (an AST walk for a per-seat weight table, which catches
a table added but not yet wired). Precedent for the AST half:
`tests/test_one_definition_guard.py` and
`tests/test_gross_exposure_ladder.py::test_trimming_the_held_book_has_exactly_one_owner`.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from src.risk.rules import (
    SEAT_WEIGHT,
    agreement_ceiling_for_score,
    count_aligned_sources,
    count_opposing_sources,
    signed_source_score,
)
from src.storage.db import _CONVICTION_OUTCOME_MIN_N

REPO = Path(__file__).parent.parent

#: The five independent evidence seats §9.4 scores. Not imported from a
#: constant because there isn't one — `build_evidence_registry` writes these
#: keys literally, and this list is what a weight table would be keyed by.
SEATS = ("technical", "news", "earnings", "macro", "smart_money")

#: `config/settings.yaml::risk.agreement_ceiling_pct`, the live schedule.
SCHEDULE = [3.0, 4.0, 5.0, 5.0, 5.0]


# ==========================================================================
# The acceptance criterion: unanimous cases price exactly as they did
# ==========================================================================

def _old_agreement_ceiling_for_count(schedule, count: int) -> float:
    """`agreement_ceiling_for_count` as it stood at 840d783, verbatim.

    Kept here — and ONLY here — as the reference the new rule is measured
    against. Deleting it would leave "unanimous cases are unchanged" as a
    claim in a docstring rather than an assertion in a test.
    """
    if not schedule:
        return float("inf")
    index = max(0, min(count, len(schedule)) - 1)
    return schedule[index]


@pytest.mark.parametrize("count", [1, 2, 3, 4, 5, 6, 99])
def test_unanimous_cases_reproduce_the_old_aligned_count_ceiling_exactly(count):
    """S=1 gives what count=1 gave, S=2 what count=2 gave, up the schedule
    and past its end. This is the property that made the change shippable:
    the existing risk envelope moves for contested names and for nothing
    else."""
    assert (agreement_ceiling_for_score(SCHEDULE, count)
            == _old_agreement_ceiling_for_count(SCHEDULE, count))


def test_a_unanimous_registry_scores_exactly_its_aligned_count():
    """The other half of the same property, at the registry level: with
    nothing opposed, S and the aligned count are the same integer."""
    for n in range(1, len(SEATS) + 1):
        sources = {seat: "bullish" for seat in SEATS[:n]}
        assert count_opposing_sources("AAPL", sources, "long") == 0
        assert signed_source_score("AAPL", sources, "long") == n
        assert count_aligned_sources("AAPL", sources, "long") == n


def test_the_only_case_the_old_rule_priced_differently_is_a_zero_count():
    """Honest about what DID move. The old rule deliberately priced a zero
    aligned count at the strictest rung ("not punished any harder than one");
    a signed sum cannot, because zero net evidence and one net seat are
    different numbers. Documented rather than hidden — and measured: across
    the 28 sized targets in the 2026-08-28..2026-09-02 snapshot, no target
    ever had zero aligned sources, so every case this reaches in practice is
    a real dissent, not a silent registry."""
    assert _old_agreement_ceiling_for_count(SCHEDULE, 0) == SCHEDULE[0]
    assert agreement_ceiling_for_score(SCHEDULE, 0) == 0.0


# ==========================================================================
# The weight pin — behavioural
# ==========================================================================

@pytest.mark.parametrize("seat", SEATS)
@pytest.mark.parametrize("direction", ["long", "short"])
def test_every_seat_enters_the_signed_sum_at_unit_magnitude(seat, direction):
    """Each seat, alone, aligned, must move the score by exactly +1; opposed,
    by exactly -1. No seat is worth more than another and none is worth a
    fraction.

    This is the guard. It fails for ANY non-unit weighting — a literal in
    `rules.py`, a table keyed by seat, a multiplier read from settings, a
    factor pulled from the conviction ledger — because it checks the number
    that comes out, not the code that produced it.

    A weight may be introduced only when it is DERIVED from that seat's own
    measured history: at least `_CONVICTION_OUTCOME_MIN_N` resolved calls
    (see the module docstring). Until then any weight is a chosen one, and a
    chosen weight is exactly what the owner ruled out on 2026-08-31.
    """
    bullish, bearish = "bullish", "bearish"
    aligned, opposed = (
        (bullish, bearish) if direction == "long" else (bearish, bullish)
    )
    assert signed_source_score("AAPL", {seat: aligned}, direction) == 1, (
        f"seat {seat!r} aligned with a {direction} must score exactly +1; a "
        "non-unit seat weight needs >= "
        f"{_CONVICTION_OUTCOME_MIN_N} resolved calls for that seat to derive "
        "it from, and the book has 7 closed equity round-trips"
    )
    assert signed_source_score("AAPL", {seat: opposed}, direction) == -1, (
        f"seat {seat!r} opposed to a {direction} must score exactly -1 — see "
        "the message above; dissent and agreement carry the same magnitude "
        "by construction, so neither can be tuned without the other"
    )


def test_the_seat_weight_constant_is_pinned_at_one_and_is_not_a_table():
    """The named pin itself. A dict here would be a per-seat weight table
    wearing the constant's name."""
    assert SEAT_WEIGHT == 1
    assert isinstance(SEAT_WEIGHT, int)
    assert not isinstance(SEAT_WEIGHT, dict)


def test_seats_are_interchangeable_in_the_score():
    """Equal weighting stated as a symmetry: which seats hold a view cannot
    change the score, only how many and on which side. Catches a weight
    table that happens to average to one."""
    scores = {
        seat: signed_source_score("AAPL", {seat: "bullish"}, "long")
        for seat in SEATS
    }
    assert len(set(scores.values())) == 1, (
        f"seats scored differently: {scores} — §9.4 weights every seat "
        "equally, and no measured history exists to justify anything else"
    )


def test_the_score_is_always_an_exact_integer():
    """A fractional weight would show up here first. `int` and not merely
    integral-valued, because `1.0 * n` is integral and still fractional in
    kind — a float score means someone has multiplied."""
    for sources in (
        {"technical": "bullish"},
        {"technical": "bullish", "macro": "bearish"},
        {seat: "bullish" for seat in SEATS},
        {seat: "bearish" for seat in SEATS},
        {},
    ):
        score = signed_source_score("AAPL", sources, "long")
        assert isinstance(score, int) and not isinstance(score, bool)


# ==========================================================================
# The weight pin — structural
# ==========================================================================

_WEIGHT_SCAN_FILES = (
    "src/risk/rules.py",
    "src/portfolio_constructor.py",
)


def _seat_keyed_numeric_dicts(tree: ast.AST) -> list[tuple[int, list[str]]]:
    """Every dict literal in `tree` that maps a canonical seat name to a
    number. That shape — and only that shape — is a per-seat weight table.

    Written to recognise the WRONG shape rather than any dict, following
    `tests/test_one_definition_guard.py`'s rule for staying trustworthy: a
    guard that flags correct code gets deleted within a week. A dict keyed by
    seat with STRING values (a stance map, a label table) is not flagged.
    """
    found: list[tuple[int, list[str]]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        seat_keys = [
            k.value for k in node.keys
            if isinstance(k, ast.Constant) and k.value in SEATS
        ]
        if not seat_keys:
            continue
        numeric = any(
            isinstance(v, ast.Constant)
            and isinstance(v.value, (int, float))
            and not isinstance(v.value, bool)
            for v in node.values
        )
        if numeric:
            found.append((node.lineno, seat_keys))
    return found


@pytest.mark.parametrize("relpath", _WEIGHT_SCAN_FILES)
def test_no_per_seat_weight_table_exists_in_the_sizing_path(relpath):
    """No dict mapping a seat name to a number, anywhere the §9.4 score is
    computed or applied.

    Catches the table BEFORE it is wired — the behavioural guard above only
    fires once a weight actually reaches the score, and a table sitting
    unused for a release is how a chosen constant becomes load-bearing
    without anyone deciding it should.
    """
    path = REPO / relpath
    hits = _seat_keyed_numeric_dicts(ast.parse(path.read_text()))
    assert not hits, (
        f"{relpath} contains a per-seat numeric table at line(s) "
        f"{[line for line, _ in hits]} (seats {[k for _, k in hits]}). §9.4 "
        "weights every seat at unit magnitude (src/risk/rules.py::SEAT_WEIGHT). "
        "A confidence weight may only be DERIVED from an analyst's measured "
        f"history — at least {_CONVICTION_OUTCOME_MIN_N} resolved calls per "
        "seat (src/storage/db.py::_CONVICTION_OUTCOME_MIN_N). The book has 7 "
        "closed equity round-trips, all conviction NULL, so there is nothing "
        "to derive from and this weight was chosen. See the owner decision "
        "recorded in src/conviction_ledger.py (2026-08-31)."
    )


def test_the_derivation_rule_this_guard_cites_still_exists():
    """The guard's failure message points at two recorded facts. If either
    moves, the message becomes a lie and the guard stops being actionable —
    so pin them here rather than discovering it during a failure."""
    assert _CONVICTION_OUTCOME_MIN_N == 20
    ledger = (REPO / "src/conviction_ledger.py").read_text()
    assert "never one chosen up front" in ledger
    assert "There is deliberately NO weight table here" in ledger


# ==========================================================================
# No second veto
# ==========================================================================

def test_the_block_comes_from_the_ceiling_and_nothing_else():
    """S <= 0 must be refused by the SAME schedule lookup that sizes the
    trade. If a standalone dissent veto were ever added on top, the
    dissenting seat would be charged twice — netted off the score AND used
    to reject the trade — and this test is where that shows up: with the
    schedule switched off, an opposed-majority registry must still produce a
    (merely unenforced) ceiling rather than a block."""
    assert agreement_ceiling_for_score([], -3) == float("inf")
    contested = {"technical": "bullish", "earnings": "bearish", "macro": "bearish"}
    assert signed_source_score("AAPL", contested, "long") == -1
    assert agreement_ceiling_for_score(SCHEDULE, -1) == 0.0


def test_dissent_and_agreement_are_the_same_magnitude():
    """Symmetry, stated directly: N aligned and N opposed cancel exactly, for
    every N. An asymmetric penalty would be a second rule hiding inside the
    first one."""
    for n in range(1, len(SEATS) // 2 + 1):
        sources = {seat: "bullish" for seat in SEATS[:n]}
        sources.update({seat: "bearish" for seat in SEATS[n:2 * n]})
        assert signed_source_score("AAPL", sources, "long") == 0
