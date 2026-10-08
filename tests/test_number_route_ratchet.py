"""The settlement-route ratchet: identity-keyed against a committed allow-list."""

from __future__ import annotations

from pathlib import Path

from src import number_ledger_counts as counts
from src.number_sources import audit


def test_the_live_tree_passes_and_no_history_file_exists() -> None:
    root = Path(__file__).resolve().parent.parent
    assert not (root / "config" / "number_ledger_route_history.yaml").exists()
    assert not [p for p in audit() if p.kind == "route-ratchet"]
    from src.number_sources import load_ledger

    allowed = counts.read_allowlist(counts.ROUTELESS_ALLOWLIST)
    assert counts.route_ratchet_violations(load_ledger(), allowed) == []


def test_new_listed_and_stale_cases() -> None:
    bare = {"status": "arbitrary"}
    routed = {"status": "arbitrary", "settles_by": {"kind": "recording"}}
    ledger = {
        "old_bare": bare,  # listed -> allowed
        "was_routed": bare,  # route removed, unlisted -> refused
        "was_sourced": bare,  # dodged down, unlisted -> refused
        "new_routed": routed,  # has a route -> allowed
        "gained_route": routed,  # listed but now routed -> stale
    }
    allowed = {"old_bare", "gained_route", "deleted_row"}
    refused = {i for i, _ in counts.route_ratchet_violations(ledger, allowed)}
    assert refused == {"was_routed", "was_sourced", "gained_route", "deleted_row"}
