#!/usr/bin/env python3
"""Which recorded settlement routes in the number ledger CANNOT settle a number.

Every row in config/number_ledger.yaml that is still `arbitrary` records a
`settles_by` route: the thing someone would have to measure, or the ruling
someone would have to find, before the number stops being a pick. A route is
only worth running if it is ADMISSIBLE under the standing owner rulings. This
report decides the two things that are mechanically decidable and says so
plainly; it judges nothing else, and it changes no value.

BAR 1 -- a route whose closing condition turns on the WORST or the most extreme
thing observed. "Worst observed" is a judgement wearing the word worst: the
sample's extreme moves every time the sample grows, so the number it yields is
never the same number twice and is not reproducible.

BAR 2 -- a route that closes by ASKING THE OWNER to name a loss, an appetite or
a dial. The owner ruled on 2026-09-30 that risk limits are read off each name's
own behaviour and the seats' conviction, and that a global risk constant is a
defect rather than a value to ratify; he has ruled repeatedly that the loss he
would accept while the exchange is shut is never to be asked at all.

DUPLICATE ROUTES -- YAML keeps the LAST of two identical mapping keys and drops
the first in silence. A row carrying two `settles_by` blocks therefore has one
live route and one dead one, and the dead one is whichever was written first.
When a newer routing pass was appended above an older block, the newer decision
is the one that was silently discarded.

Read-only. No network, no broker, no write to any desk state. Exit 0 always:
this REPORTS, it does not gate, because the rows it names are pre-existing and
a guard that arrives red teaches people to bypass guards.

Usage:
    python -m scripts.measure_ledger_route_admissibility
    python -m scripts.measure_ledger_route_admissibility --ledger path/to.yaml
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_LEDGER = ROOT / "config" / "number_ledger.yaml"

#: Closing conditions that name an extreme of the sample. Each pattern is
#: matched against the route's prose with case folded.
EXTREME_PATTERNS: tuple[str, ...] = (
    r"\bworst\b",
    r"\bmost extreme\b",
    r"\blargest observed\b",
    r"\bmaximum observed\b",
)

#: Closing conditions that hand the number back to the owner as an appetite.
APPETITE_PATTERNS: tuple[str, ...] = (
    r"\bthe owner states\b",
    r"\bthe owner sets\b",
    r"\bthe owner names\b",
    r"\bowner (?:appetite|ratifies|ratification)\b",
    r"\bhe will accept\b",
    r"\bwilling to (?:lose|accept)\b",
)

_ENTRY_SPLIT = re.compile(r"^  - id: (.+)$", re.MULTILINE)
_SETTLES_KEY = re.compile(r"^    settles_by:\s*$", re.MULTILINE)


class Finding:
    """One inadmissible or ambiguous route, ready to print."""

    def __init__(self, row_id: str, kind: str, detail: str) -> None:
        self.row_id = row_id
        self.kind = kind
        self.detail = detail

    def line(self) -> str:
        return f"  {self.kind:<16} {self.row_id}\n      {self.detail}"


def split_entries(text: str) -> list[tuple[str, str]]:
    """Ledger text -> [(row id, that row's raw block)].

    The ledger is parsed as TEXT rather than loaded, because the defect this
    report looks for is a duplicate mapping key, and every YAML loader has
    already thrown the duplicate away by the time a loaded document is in hand.
    """
    marks = list(_ENTRY_SPLIT.finditer(text))
    entries: list[tuple[str, str]] = []
    for index, mark in enumerate(marks):
        end = marks[index + 1].start() if index + 1 < len(marks) else len(text)
        entries.append((mark.group(1).strip(), text[mark.end():end]))
    return entries


def is_arbitrary(block: str) -> bool:
    """True when the row is still carrying the arbitrary classification."""
    return re.search(r"^    status: arbitrary\s*$", block, re.MULTILINE) is not None


def count_routes(block: str) -> int:
    """How many settles_by blocks the row carries in the file as written."""
    return len(_SETTLES_KEY.findall(block))


def matched_patterns(block: str, patterns: tuple[str, ...]) -> list[str]:
    """Which of the given patterns the row's prose contains, case folded."""
    folded = block.lower()
    return [p for p in patterns if re.search(p, folded)]


def inspect(text: str) -> list[Finding]:
    """Every mechanically decidable problem with the routes in this ledger."""
    findings: list[Finding] = []
    for row_id, block in split_entries(text):
        routes = count_routes(block)
        if routes > 1:
            findings.append(
                Finding(
                    row_id,
                    "DEAD-ROUTE",
                    f"{routes} settles_by blocks; YAML keeps only the last, "
                    f"so {routes - 1} recorded route(s) are silently dropped",
                )
            )
        if not is_arbitrary(block):
            continue
        extremes = matched_patterns(block, EXTREME_PATTERNS)
        if extremes:
            findings.append(
                Finding(
                    row_id,
                    "BARRED-EXTREME",
                    "closing condition turns on an extreme of the sample: "
                    + ", ".join(p.strip("\\b") for p in extremes),
                )
            )
        appetites = matched_patterns(block, APPETITE_PATTERNS)
        if appetites:
            findings.append(
                Finding(
                    row_id,
                    "BARRED-APPETITE",
                    "closing condition asks the owner to name a loss or a dial: "
                    + ", ".join(p.strip("\\b") for p in appetites),
                )
            )
    return findings


def report(findings: list[Finding]) -> str:
    """The printable report, grouped by kind, newest concern first."""
    if not findings:
        return "No inadmissible or duplicated settlement routes found.\n"
    lines = ["Ledger settlement routes that cannot settle a number:", ""]
    for kind in ("BARRED-EXTREME", "BARRED-APPETITE", "DEAD-ROUTE"):
        group = [f for f in findings if f.kind == kind]
        if not group:
            continue
        lines.append(f"{kind} ({len(group)})")
        lines.extend(f.line() for f in group)
        lines.append("")
    lines.append(f"{len(findings)} finding(s). This reports; it does not gate.")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ledger", default=str(DEFAULT_LEDGER))
    args = parser.parse_args(argv)
    text = Path(args.ledger).read_text()
    sys.stdout.write(report(inspect(text)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
