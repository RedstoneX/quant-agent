"""Ledger row-identity ratchets, judged against COMMITTED allow-lists.

Lifted out of `src/number_sources.py` (file-size ratchet). The limit is fixed in
the repo: `config/check_allowlists/ledger_arbitrary_rows.txt` names every row
that is `arbitrary`, and `config/check_allowlists/ledger_routeless_rows.txt`
every `arbitrary` row with no `settles_by`. Nothing is read from the trunk, so
an unrelated merge cannot redden waiting work. A row not on its list fails; a
listed row that no longer qualifies is a stale entry and fails too (shrink-only).
"""

from __future__ import annotations

from pathlib import Path

import yaml

LEDGER_RELATIVE = "config/number_ledger.yaml"
ALLOWLIST_DIR = Path(__file__).resolve().parent.parent / "config" / "check_allowlists"
ARBITRARY_ALLOWLIST = ALLOWLIST_DIR / "ledger_arbitrary_rows.txt"
ROUTELESS_ALLOWLIST = ALLOWLIST_DIR / "ledger_routeless_rows.txt"

#: A new `arbitrary` row is a number REGISTERED; without these it is a number
#: PARKED, with no route by which it could ever leave.
ROUTE_FIELDS = ("settles_by", "open_question")


def read_allowlist(path: Path) -> set[str]:
    """Row ids named in a committed allow-list; `#` lines and blanks are comments."""
    return {
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }


def count_arbitrary(text: str) -> int:
    """`status: arbitrary` rows in a raw ledger document."""
    raw = yaml.safe_load(text) or {}
    return sum(1 for entry in (raw.get("numbers") or []) if entry.get("status") == "arbitrary")


def statuses_by_id(text: str) -> dict[str, str]:
    """Every ledger row's status, keyed by row id, from a raw document."""
    raw = yaml.safe_load(text) or {}
    return {entry["id"]: entry.get("status") for entry in (raw.get("numbers") or []) if entry.get("id")}


def ratchet_violations(ledger, allowed):
    """The arbitrary ratchet, keyed on row IDENTITY against a committed list.

    A count cannot tell DODGING from DISCOVERY and passes a net-zero swap; named
    identities do neither. Every `arbitrary` row must be on `allowed`; every
    entry of `allowed` must still be an `arbitrary` row (a stale entry fails, so
    the list can only shrink). Adding a row needs a `Guard-rule-change:` line.

    Returns `(site_id, detail)` for every row that must be refused.
    """
    out = []
    for site_id, entry in sorted(ledger.items()):
        if entry.get("status") != "arbitrary" or site_id in allowed:
            continue
        missing = [f for f in ROUTE_FIELDS if not entry.get(f)]
        route = (
            f" It also carries no {' and no '.join(missing)}: a new unsourced "
            "number is admitted only with a route to settlement."
            if missing
            else ""
        )
        out.append(
            (
                site_id,
                "is `arbitrary` but not on config/check_allowlists/"
                "ledger_arbitrary_rows.txt. Source it, or (with a Guard-rule-change "
                "line) list it." + route,
            )
        )
    for site_id in sorted(allowed):
        if ledger.get(site_id, {}).get("status") != "arbitrary":
            out.append(
                (
                    site_id,
                    "is on ledger_arbitrary_rows.txt but is no longer `arbitrary` "
                    "in the ledger: stale entry, delete it from the list.",
                )
            )
    return out


def routeless_by_id(text: str) -> dict[str, bool]:
    """Per row id: is it `arbitrary` with no `settles_by` block at all?"""
    raw = yaml.safe_load(text) or {}
    return {
        entry["id"]: entry.get("status") == "arbitrary" and entry.get("settles_by") is None
        for entry in (raw.get("numbers") or [])
        if entry.get("id")
    }


def route_ratchet_violations(ledger, allowed):
    """The settlement-route ratchet, keyed on row IDENTITY against a committed list.

    An `arbitrary` row with no `settles_by` must be on `allowed`; an `allowed`
    entry that is no longer routeless (gained a route, was sourced, or is gone)
    is stale and fails. Gaining a route is therefore always allowed but shrinks
    the list in the same change.
    """
    out = []
    for site_id, entry in sorted(ledger.items()):
        if entry.get("status") != "arbitrary" or entry.get("settles_by") is not None:
            continue
        if site_id in allowed:
            continue
        out.append(
            (
                site_id,
                "is `arbitrary` with no `settles_by` and not on config/"
                "check_allowlists/ledger_routeless_rows.txt. A routeless number may "
                "not be created or regressed: give it a `settles_by` route, or "
                "restore its source.",
            )
        )
    for site_id in sorted(allowed):
        entry = ledger.get(site_id)
        if not (entry and entry.get("status") == "arbitrary" and entry.get("settles_by") is None):
            out.append(
                (
                    site_id,
                    "is on ledger_routeless_rows.txt but is no longer a routeless "
                    "`arbitrary` row: stale entry, delete it from the list.",
                )
            )
    return out
