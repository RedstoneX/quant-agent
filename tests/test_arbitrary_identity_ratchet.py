"""The arbitrary-number ratchet, proved case by case against a committed allow-list.

A guard with no failing case is not a guard: a new arbitrary row not on the list
fails, a listed one passes, a stale entry fails, and the real ledger matches the
real list. Nothing here reads the trunk.
"""

from __future__ import annotations

from src.number_ledger_counts import (
    ARBITRARY_ALLOWLIST,
    ratchet_violations,
    read_allowlist,
    statuses_by_id,
)

ROUTED = {"settles_by": "a named observation", "open_question": "what backs it?"}


def _arb(**extra):
    return {"status": "arbitrary", "value": 1, **extra}


def _refused(ledger, allowed):
    return {site_id for site_id, _ in ratchet_violations(ledger, allowed)}


def test_an_arbitrary_row_not_on_the_list_is_refused() -> None:
    """Covers the dodge (a sourced row downgraded) and a new discovery alike."""
    assert _refused({"src.a.X": _arb(**ROUTED)}, set()) == {"src.a.X"}


def test_a_listed_arbitrary_row_passes() -> None:
    assert _refused({"src.a.X": _arb()}, {"src.a.X"}) == set()


def test_a_stale_entry_is_refused_when_the_row_was_sourced_or_removed() -> None:
    assert _refused({"src.a.X": {"status": "sourced"}}, {"src.a.X"}) == {"src.a.X"}
    assert _refused({}, {"src.a.X"}) == {"src.a.X"}


def test_a_net_zero_swap_does_not_satisfy_the_ratchet() -> None:
    """One row dodges down while another is sourced up: the total is unchanged,
    but the new row is unlisted and the old entry is stale, so both are named."""
    ledger = {"src.a.X": _arb(**ROUTED), "src.b.Z": {"status": "sourced"}}
    assert _refused(ledger, {"src.b.Z"}) == {"src.a.X", "src.b.Z"}


def test_an_unlisted_row_without_route_fields_says_so() -> None:
    ((_, detail),) = ratchet_violations({"src.n.Y": _arb()}, set())
    assert "settles_by" in detail and "open_question" in detail


def test_the_real_ledger_matches_the_committed_list() -> None:
    from src.number_sources import load_ledger

    assert ratchet_violations(load_ledger(), read_allowlist(ARBITRARY_ALLOWLIST)) == []


def test_the_list_file_is_sorted_and_statuses_parse() -> None:
    lines = [ln for ln in ARBITRARY_ALLOWLIST.read_text().splitlines() if ln and not ln.startswith("#")]
    assert lines == sorted(lines) and len(lines) == len(set(lines))
    assert statuses_by_id("numbers: []") == {}


def test_every_arbitrary_row_in_the_ledger_carries_a_route() -> None:
    from src.number_sources import load_ledger

    arbitrary = [e for e in load_ledger().values() if e.get("status") == "arbitrary"]
    assert arbitrary
    assert not [e for e in arbitrary if not (e.get("settles_by") and e.get("open_question"))]
