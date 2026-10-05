"""Settlement-route ratchet reference: read from the trunk, stored nowhere.

The route ratchet was the sum of deltas in an append-only file, and a sum
accepts a positive term, so the change it refused could raise its own limit in
the same commit. The reference is now the trunk's own ledger at check time;
past grounds stay in git. Lifted out of src/number_sources.py to keep it from
growing.
"""

from __future__ import annotations

import yaml

LEDGER_RELATIVE = "config/number_ledger.yaml"


def count_routeless(text: str) -> int:
    """`arbitrary` rows with no `settles_by` in a raw ledger document."""
    raw = yaml.safe_load(text) or {}
    return sum(
        1
        for entry in (raw.get("numbers") or [])
        if entry.get("status") == "arbitrary" and entry.get("settles_by") is None
    )


def trunk_routeless_count() -> int:
    """The routeless-`arbitrary` count on the trunk, read fresh at check time.

    If the trunk cannot be read the guard REFUSES (``ReferenceUnavailable``).
    """
    from scripts.guard_reference import ReferenceUnavailable, trunk_blobs

    blobs = trunk_blobs([LEDGER_RELATIVE])
    if LEDGER_RELATIVE not in blobs:
        raise ReferenceUnavailable(f"{LEDGER_RELATIVE} is absent from the trunk")
    return count_routeless(blobs[LEDGER_RELATIVE])
