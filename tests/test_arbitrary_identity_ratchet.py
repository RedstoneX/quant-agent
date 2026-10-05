"""The arbitrary-number ratchet, proved case by case.

A guard with no failing case is not a guard, so every branch of the rule is
exercised here against a synthetic trunk: the dodge that must still be
refused, the honest discovery that must now be admitted, the parked number
that must not be, and the unreadable trunk that must refuse rather than pass.
"""

from __future__ import annotations

import pytest

from src.number_ledger_counts import (
    ratchet_violations,
    statuses_by_id,
    trunk_statuses,
)

ROUTED = {"settles_by": "a named observation", "open_question": "what backs it?"}


def _arb(**extra):
    return {"status": "arbitrary", "value": 1, **extra}


def _refused(ledger, trunk):
    return {site_id for site_id, _ in ratchet_violations(ledger, trunk)}


@pytest.mark.parametrize("was", ["sourced", "derived", "instrument"])
def test_a_reclassification_into_arbitrary_is_refused(was: str) -> None:
    """THE failing case. A row the trunk already backs, downgraded here to
    `arbitrary`, is the dodge the ratchet exists to refuse -- and carrying a
    settlement route does not buy it, because the row already had one.
    """
    ledger = {"src.a.X": _arb(**ROUTED)}
    assert _refused(ledger, {"src.a.X": was}) == {"src.a.X"}


def test_a_new_row_with_both_route_fields_is_allowed() -> None:
    """DISCOVERY. A sweep registering a number the ledger never held, saying
    honestly that nothing backs it and naming what would settle it, is the
    only way a number nobody had scoped can start moving toward zero.
    """
    ledger = {"src.new.Y": _arb(**ROUTED), "src.a.X": {"status": "sourced"}}
    assert _refused(ledger, {"src.a.X": "sourced"}) == set()


@pytest.mark.parametrize("drop", ["settles_by", "open_question"])
def test_a_new_row_missing_a_route_field_is_refused(drop: str) -> None:
    """A new unsourced number with no route by which it could ever leave is
    parked, not registered.
    """
    fields = {k: v for k, v in ROUTED.items() if k != drop}
    assert _refused({"src.new.Y": _arb(**fields)}, {}) == {"src.new.Y"}


def test_a_row_leaving_arbitrary_is_allowed() -> None:
    """The standing order is to drive the count to zero, so sourcing a row
    the trunk holds as `arbitrary` can never be a violation.
    """
    ledger = {"src.a.X": {"status": "sourced"}}
    assert _refused(ledger, {"src.a.X": "arbitrary"}) == set()


def test_a_net_zero_swap_does_not_satisfy_the_ratchet() -> None:
    """The hole a count leaves open. One row dodges down to `arbitrary` while
    an unrelated row is sourced up; the total is unchanged, so a count-based
    rule passes it and the new dodge lands unnoticed. Named identities do not.
    """
    trunk = {"src.a.X": "sourced", "src.b.Z": "arbitrary"}
    ledger = {"src.a.X": _arb(**ROUTED), "src.b.Z": {"status": "sourced"}}
    assert len([e for e in ledger.values() if e["status"] == "arbitrary"]) == 1
    assert _refused(ledger, trunk) == {"src.a.X"}


def test_an_unchanged_arbitrary_row_is_allowed_without_route_fields() -> None:
    """Rows the trunk already carries as `arbitrary` are judged by the rules
    that admitted them, not re-litigated here; the route demand binds the new.
    """
    assert _refused({"src.a.X": _arb()}, {"src.a.X": "arbitrary"}) == set()


def test_an_unreadable_trunk_refuses_rather_than_passes() -> None:
    """A ratchet with no reference is decoration."""
    from scripts import guard_reference

    def _blank(paths):
        return {}

    original = guard_reference.trunk_blobs
    guard_reference.trunk_blobs = _blank
    try:
        with pytest.raises(guard_reference.ReferenceUnavailable):
            trunk_statuses()
    finally:
        guard_reference.trunk_blobs = original


def test_every_arbitrary_row_in_the_ledger_carries_a_route() -> None:
    """The precondition for demanding a route of new rows: measured on the
    trunk, all 131 `arbitrary` rows already carry both fields, so the demand
    asks nothing of a new row that every existing one does not already meet.
    """
    from src.number_sources import load_ledger

    arbitrary = [e for e in load_ledger().values() if e.get("status") == "arbitrary"]
    assert arbitrary
    assert not [e for e in arbitrary if not (e.get("settles_by") and e.get("open_question"))]
    assert set(trunk_statuses()) and statuses_by_id("numbers: []") == {}


# --- RENAME RECONCILIATION -------------------------------------------------
# A row id is a dotted symbol path, so a module split rewrites the primary
# key. Without reconciliation a `sourced` number could be laundered into
# `arbitrary` by renaming its module. The proxy is the LEAF symbol name
# against DROPPED trunk rows only, and it errs STRICT; these prove both.


def test_a_renamed_row_that_stays_sourced_is_allowed() -> None:
    """A pure move. Nothing is `arbitrary`, so nothing is refused."""
    ledger = {"src.b.X": {"status": "sourced"}}
    assert _refused(ledger, {"src.a.X": "sourced"}) == set()


def test_a_rename_that_also_downgrades_is_refused() -> None:
    """THE LAUNDERING CASE. Renaming the module does not retire the source,
    and a settlement route does not buy the downgrade either.
    """
    ledger = {"src.b.X": _arb(**ROUTED)}
    assert _refused(ledger, {"src.a.X": "sourced"}) == {"src.b.X"}


def test_a_renamed_arbitrary_row_is_not_treated_as_a_new_discovery() -> None:
    """Already `arbitrary` on the trunk under the old path: unchanged, so it
    is not forced to carry route fields it never had.
    """
    ledger = {"src.b.X": _arb()}
    assert _refused(ledger, {"src.a.X": "arbitrary"}) == set()


def test_a_surviving_trunk_row_is_never_read_as_the_source_of_a_move() -> None:
    """`src.a.X` is still in the change under its own id, so it did not move.
    A genuinely new `src.b.X` is judged as new -- routed, so allowed.
    """
    ledger = {"src.a.X": {"status": "sourced"}, "src.b.X": _arb(**ROUTED)}
    assert _refused(ledger, {"src.a.X": "sourced"}) == set()


def test_an_ambiguous_leaf_inherits_the_strongest_status_and_is_refused() -> None:
    """Two DROPPED trunk rows share the leaf `X`, one sourced and one
    arbitrary. The arriving row inherits `sourced` -- the strict reading --
    so an ambiguous leaf costs a false REFUSAL, never a false pass.
    """
    ledger = {"src.c.X": _arb(**ROUTED)}
    trunk = {"src.a.X": "sourced", "src.b.X": "arbitrary"}
    assert _refused(ledger, trunk) == {"src.c.X"}
