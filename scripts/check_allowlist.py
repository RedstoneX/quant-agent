"""Fixed allow-lists for home-made code-pattern checks.

A check that used to compare the working tree with ``origin/main`` compares with
a COMMITTED file instead: ``config/check_allowlists/code_<check>.txt``. One line
per existing violation IDENTITY (a JSON array of strings: file, symbol or scope,
the site's own text -- never a count, never a line number), sorted. A site that
occurs twice is listed twice; that is the identity repeated, not a count.

The check fails on (a) any violation not in the list and (b) any list entry that
no longer occurs (a stale entry), so the list can only shrink without a visible,
justified diff. Nothing here reads git or the trunk.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Iterable

ROOT = Path(__file__).resolve().parent.parent
ALLOWLIST_DIR = ROOT / "config" / "check_allowlists"

HEADER = (
    "# Fixed allow-list for the {check} check: one existing violation identity per line.\n"
    "# SHRINK-ONLY. Never add a line without a `Guard-rule-change:` line in the commit\n"
    "# message saying why the new violation is acceptable. Stale lines fail the check.\n"
)


def encode(identity: Iterable[object]) -> str:
    """One identity as one physical line."""
    return json.dumps([str(part) for part in identity], ensure_ascii=True, separators=(",", ":"))


def load(path: Path) -> Counter[str]:
    """The listed identities as a multiset; comments and blank lines are ignored."""
    lines = path.read_text(encoding="utf-8").splitlines()
    return Counter(ln for ln in lines if ln.strip() and not ln.startswith("#"))


def compare(found: Iterable[Iterable[object]], path: Path) -> tuple[list[str], list[str]]:
    """``(unlisted, stale)`` encoded identities: found-but-not-listed, listed-but-not-found."""
    have = Counter(encode(identity) for identity in found)
    listed = load(path)
    unlisted = sorted((have - listed).elements())
    stale = sorted((listed - have).elements())
    return unlisted, stale


def render(check: str, found: Iterable[Iterable[object]]) -> str:
    """The file text for ``found`` (used once to generate a list from today's tree)."""
    return HEADER.format(check=check) + "".join(line + "\n" for line in sorted(encode(identity) for identity in found))


def report(unlisted: list[str], stale: list[str], path: Path) -> str:
    out = []
    if unlisted:
        out.append(f"violation(s) not in {path.name}:\n" + "\n".join(f"  + {u}" for u in unlisted))
    if stale:
        out.append(
            f"stale entr(ies) in {path.name} (the violation is gone; delete the line):\n"
            + "\n".join(f"  - {s}" for s in stale)
        )
    return "\n".join(out)
