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


def statuses_by_id(text: str) -> dict[str, str]:
    """Every ledger row's status, keyed by row id, from a raw document."""
    raw = yaml.safe_load(text) or {}
    return {
        entry["id"]: entry.get("status")
        for entry in (raw.get("numbers") or [])
        if entry.get("id")
    }


def trunk_statuses() -> dict[str, str]:
    """The trunk's status-per-row-id, read fresh at check time.

    REFUSES (``ReferenceUnavailable``) when the trunk cannot be read: a
    ratchet with no reference is decoration.
    """
    from scripts.guard_reference import ReferenceUnavailable, trunk_blobs

    blobs = trunk_blobs([LEDGER_RELATIVE])
    if LEDGER_RELATIVE not in blobs:
        raise ReferenceUnavailable(
            f"{LEDGER_RELATIVE} is absent from the trunk, so the arbitrary "
            "rows have no reference to be judged against"
        )
    return statuses_by_id(blobs[LEDGER_RELATIVE])


#: A new `arbitrary` row is a number REGISTERED; without these it is a number
#: PARKED, with no route by which it could ever leave.
ROUTE_FIELDS = ("settles_by", "open_question")


def ratchet_violations(ledger, trunk):
    """The arbitrary ratchet, keyed on row IDENTITY rather than on a total.

    A count cannot tell DODGING from DISCOVERY. Downgrading a row the trunk
    already sources is the dishonest act the ratchet exists to refuse; a sweep
    bringing a number into the ledger for the first time and declaring it
    unsourced is the only way the standing order to drive the count to zero
    can ever start on a number nobody had scoped. A total refuses both, and
    is also satisfied by a net-zero swap (one row dodges down while an
    unrelated row is sourced up), which identities are not.

    Returns `(site_id, detail)` for every row that must be refused.
    """
    out = []
    for site_id, entry in sorted(ledger.items()):
        if entry.get("status") != "arbitrary":
            continue  # leaving `arbitrary` is always allowed
        was = trunk.get(site_id)
        if was is not None and was != "arbitrary":
            out.append((
                site_id,
                f"is `{was}` on the trunk and `arbitrary` here. Reclassifying "
                "a row that already had a source is the dodge this ratchet "
                "exists to refuse: restore the source or revert the row.",
            ))
            continue
        if site_id in trunk:
            continue  # already arbitrary on the trunk; unchanged
        missing = [f for f in ROUTE_FIELDS if not entry.get(f)]
        if missing:
            out.append((
                site_id,
                "is new to the ledger and `arbitrary` but carries no "
                f"{' and no '.join(missing)}. A new unsourced number is "
                "admitted only with a route to settlement; without one it is "
                "parked, not registered.",
            ))
    return out


def routeless_by_id(text: str) -> dict[str, bool]:
    """Per row id: is it `arbitrary` with no `settles_by` block at all?"""
    raw = yaml.safe_load(text) or {}
    return {
        entry["id"]: entry.get("status") == "arbitrary"
        and entry.get("settles_by") is None
        for entry in (raw.get("numbers") or [])
        if entry.get("id")
    }


def trunk_routeless() -> dict[str, bool]:
    """The trunk's routeless-per-row-id, read fresh at check time.

    REFUSES (``ReferenceUnavailable``) when the trunk cannot be read.
    """
    from scripts.guard_reference import ReferenceUnavailable, trunk_blobs

    blobs = trunk_blobs([LEDGER_RELATIVE])
    if LEDGER_RELATIVE not in blobs:
        raise ReferenceUnavailable(
            f"{LEDGER_RELATIVE} is absent from the trunk, so the routeless "
            "rows have no reference to be judged against"
        )
    return routeless_by_id(blobs[LEDGER_RELATIVE])


def route_ratchet_violations(ledger, trunk_route):
    """The settlement-route ratchet, keyed on row IDENTITY against the trunk.

    A row that is `arbitrary` with no `settles_by` here must also have been
    exactly that on the trunk. A row the trunk sourced or routed, or a row new
    to the ledger, may not arrive routeless. Gaining a route is always allowed.
    """
    out = []
    for site_id, entry in sorted(ledger.items()):
        if entry.get("status") != "arbitrary" or entry.get("settles_by") is not None:
            continue
        if trunk_route.get(site_id) is True:
            continue  # routeless on the trunk too; unchanged
        where = "new to the ledger" if site_id not in trunk_route else (
            "sourced or routed on the trunk"
        )
        out.append((
            site_id,
            f"is `arbitrary` with no `settles_by` here but is {where}. "
            "A routeless number may not be created or regressed: give it a "
            "`settles_by` route, or restore its source.",
        ))
    return out
