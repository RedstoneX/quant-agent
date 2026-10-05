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
    """`status: arbitrary` rows in a raw ledger document.

    The ratchet no longer judges by this total -- it judges row by row, by
    identity. The total survives because it is the number the standing order
    drives to zero, and it is REPORTED, on the audit CLI's success line. An
    unreported total is progress nobody can see.
    """
    raw = yaml.safe_load(text) or {}
    return sum(
        1 for entry in (raw.get("numbers") or []) if entry.get("status") == "arbitrary"
    )


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


def _leaf(site_id: str) -> str:
    """The bare symbol name at the end of a dotted row id."""
    return site_id.rsplit(".", 1)[-1]


def dropped_statuses_by_leaf(ledger, trunk):
    """Trunk rows this change no longer holds, grouped by LEAF SYMBOL NAME.

    THE PROXY, named outright: a row id is a dotted symbol path, so splitting
    a module rewrites the primary key of every number in it -- 93 such
    rewrites in the four days to 2026-10-05, with the splits ongoing. True
    identity is not statically available for a renamed symbol, so a MOVE is
    recognised here by the leaf symbol name alone, and only against a trunk
    row the change has DROPPED: a trunk row still present under its own id
    cannot have moved anywhere.

    WHICH WAY IT ERRS: STRICT, deliberately. When several dropped trunk rows
    share one leaf, the arriving row inherits the STRONGEST status among them
    (any non-arbitrary beats arbitrary), so an ambiguous leaf is REFUSED
    rather than waved through. Two unrelated numbers sharing a leaf name
    therefore cost a false refusal -- undone by naming one of them
    differently -- and never a false pass. The failure this closes is the
    opposite one: without it a `sourced` row could be laundered into
    `arbitrary` by renaming its module, and nothing would notice.
    """
    out: dict[str, set[str]] = {}
    for site_id, status in trunk.items():
        if site_id in ledger:
            continue
        out.setdefault(_leaf(site_id), set()).add(status)
    return out


def _trunk_status_of(site_id, ledger, trunk, dropped):
    """`(status_on_trunk, was_renamed)` for a row, moves reconciled."""
    was = trunk.get(site_id)
    if was is not None:
        return was, False
    candidates = dropped.get(_leaf(site_id))
    if not candidates:
        return None, False
    stronger = sorted(s for s in candidates if s != "arbitrary")
    return (stronger[0] if stronger else "arbitrary"), True


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
    dropped = dropped_statuses_by_leaf(ledger, trunk)
    for site_id, entry in sorted(ledger.items()):
        if entry.get("status") != "arbitrary":
            continue  # leaving `arbitrary` is always allowed
        was, renamed = _trunk_status_of(site_id, ledger, trunk, dropped)
        if was is not None and was != "arbitrary":
            where = (
                "under another module path (matched on its leaf symbol name) "
                if renamed
                else ""
            )
            out.append((
                site_id,
                f"is `{was}` on the trunk {where}and `arbitrary` here. "
                "Reclassifying a row that already had a source is the dodge "
                "this ratchet exists to refuse, and renaming its module does "
                "not retire the source: restore it or revert the row.",
            ))
            continue
        if was is not None:
            continue  # already arbitrary on the trunk, moved or not; unchanged
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
