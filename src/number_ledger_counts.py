"""Count `arbitrary` rows in the trunk's ledger, at check time.

Two counters:
1. routeless: `arbitrary` with no `settles_by` (route-ratchet reference)
2. arbitrary: all `status: arbitrary` rows (arbitrary-count ratchet)

Lifted out of `src/number_sources.py` with no change in behaviour.
If the trunk cannot be read, callers are REFUSED (``ReferenceUnavailable``).
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


def count_arbitrary(text: str) -> int:
    """`status: arbitrary` rows in a raw ledger document."""
    raw = yaml.safe_load(text) or {}
    return sum(
        1 for entry in (raw.get("numbers") or []) if entry.get("status") == "arbitrary"
    )


def trunk_arbitrary_count() -> int:
    """The `arbitrary` count on the trunk, read fresh at check time."""
    from scripts.guard_reference import ReferenceUnavailable, trunk_blobs

    blobs = trunk_blobs([LEDGER_RELATIVE])
    if LEDGER_RELATIVE not in blobs:
        raise ReferenceUnavailable(
            f"{LEDGER_RELATIVE} is absent from the trunk, so the arbitrary "
            "count has no reference to be judged against"
        )
    return count_arbitrary(blobs[LEDGER_RELATIVE])
