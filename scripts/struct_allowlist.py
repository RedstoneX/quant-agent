"""Fixed allow-lists for the structure checks: violations pinned by identity.

A structure check used to re-derive its limit from ``origin/main`` every time it
ran, so an unrelated merge moved the limit under waiting changes and reddened
them. Now each check names its existing violations in a committed file,
``config/check_allowlists/struct_<check>.txt``: one identity per line (a file
plus a symbol or site key; never a count, never a line number), sorted, shrink
only. A check fails on (a) any violation not listed and (b) any listed entry that
no longer occurs, so the list can only be edited down by fixing code, and every
addition shows in the diff with its ``Guard-rule-change:`` justification.
"""
from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Iterable

ROOT = Path(__file__).resolve().parent.parent
ALLOWLIST_DIR = ROOT / "config" / "check_allowlists"

HEADER = (
    "# Fixed allow-list for the {check} check: one existing violation identity per line.\n"
    "# Shrink-only. Sorted. A new entry needs a Guard-rule-change: line in the commit.\n"
    "# A stale entry (no longer occurring) fails the check; delete it.\n"
)


def path_for(check: str, directory: Path | None = None) -> Path:
    return (directory or ALLOWLIST_DIR) / f"struct_{check}.txt"


def load(check: str, directory: Path | None = None) -> list[str]:
    """The listed identities (comments and blanks dropped), repeats kept."""
    text = path_for(check, directory).read_text(encoding="utf-8")
    return [ln for ln in text.splitlines() if ln.strip() and not ln.startswith("#")]


def write(check: str, entries: Iterable[str], directory: Path | None = None) -> Path:
    """Write a sorted list with the standard header (seeds a list, or builds a test fixture)."""
    target = path_for(check, directory)
    target.parent.mkdir(parents=True, exist_ok=True)
    body = "".join(f"{e}\n" for e in sorted(entries))
    target.write_text(HEADER.format(check=check) + body, encoding="utf-8")
    return target


def compare(found: Iterable[str], listed: Iterable[str]) -> tuple[list[str], list[str]]:
    """(new, stale): found identities not listed, and listed ones no longer found.

    Multiset on purpose: a second copy of an already-listed identity is new.
    """
    have, want = Counter(found), Counter(listed)
    return sorted((have - want).elements()), sorted((want - have).elements())


def problems(check: str, found: Iterable[str], fix: str, directory: Path | None = None) -> list[str]:
    """Human-readable failures for one check, empty when found == listed."""
    name = path_for(check, directory).name
    new, stale = compare(found, load(check, directory))
    out = [f"NEW violation not in {name}: {n}. {fix}" for n in new]
    out += [f"STALE entry in {name} no longer occurs: {s}. Delete it (the list only shrinks)." for s in stale]
    return out
