"""Count the `arbitrary` rows in the trunk's own ledger, at check time.

Lifted out of `src/number_sources.py` (file-size ratchet) with no change in
behaviour. Nothing is stored and nothing is cached. If the trunk cannot be
read the caller is REFUSED (``ReferenceUnavailable``) rather than passed: a
ratchet with no reference is decoration.
"""

from __future__ import annotations

import yaml

LEDGER_RELATIVE = "config/number_ledger.yaml"


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
