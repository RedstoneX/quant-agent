"""The settlement-route ratchet: identity-keyed against the trunk, no stored file."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.number_sources import audit


def test_the_settlement_route_ratchet_is_identity_keyed_against_the_trunk() -> None:
    """The route ratchet stores nothing: no history file, no summed deltas.

    A routeless row must be routeless on the trunk too. The live tree passes;
    each way of cheating (a new routeless row, a sourced row regressed to
    routeless) still fails, and a missing trunk REFUSES rather than passes.
    """

    from scripts.guard_reference import ReferenceUnavailable
    from src import number_ledger_counts as counts

    root = Path(__file__).resolve().parent.parent
    assert not (root / "config" / "number_ledger_route_history.yaml").exists()
    assert not [p for p in audit() if p.kind == "route-ratchet"]

    bare = {"status": "arbitrary"}
    routed = {"status": "arbitrary", "settles_by": {"kind": "recording"}}
    trunk = {"old_bare": True, "was_routed": False, "was_sourced": False}
    ledger = {
        "old_bare": bare,
        "was_routed": bare,  # route removed -> refused
        "was_sourced": bare,  # dodged down -> refused
        "brand_new": bare,  # discovery with no route -> refused
        "new_routed": routed,  # discovery with a route -> allowed
    }
    refused = {i for i, _ in counts.route_ratchet_violations(ledger, trunk)}
    assert refused == {"was_routed", "was_sourced", "brand_new"}

    import scripts.guard_reference as ref

    original = ref.trunk_blobs
    ref.trunk_blobs = lambda paths: {}
    try:
        with pytest.raises(ReferenceUnavailable):
            counts.trunk_routeless()
    finally:
        ref.trunk_blobs = original
