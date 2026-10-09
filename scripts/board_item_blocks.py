"""Read one board item's block and its DONE WHEN criteria out of the board text."""

from __future__ import annotations

import re

ITEM_HEADING = re.compile(r"^\*\*(\d+)\.\s", re.M)

#: A filed item's own completion criteria. One label, then one bullet per
#: criterion, checkbox-style so "met" is a one-character edit and a diff
#: shows it. The identifier is the bullet's ordinal within its item.
DONE_WHEN = re.compile(r"^\s*DONE WHEN:\s*$", re.M)
CRITERION = re.compile(r"^\s*[-*]\s*\[( |x|X)\]\s*(.+?)\s*$", re.M)


def item_blocks(work_md: str | None) -> dict[str, str]:
    """Item number -> the text from its heading to the next item's heading."""
    if not work_md:
        return {}
    marks = [(m.group(1), m.start()) for m in ITEM_HEADING.finditer(work_md)]
    out: dict[str, str] = {}
    for i, (number, start) in enumerate(marks):
        end = marks[i + 1][1] if i + 1 < len(marks) else len(work_md)
        out[number] = work_md[start:end]
    return out


def criteria(block: str) -> list[tuple[int, bool, str]]:
    """`(ordinal, met, text)` for each criterion under this item's DONE WHEN.

    Reads the bullets that follow the label and stops at the first line that
    is neither a criterion bullet nor blank, so ordinary item prose below the
    criteria is not swept in.
    """
    match = DONE_WHEN.search(block)
    if not match:
        return []
    out: list[tuple[int, bool, str]] = []
    ordinal = 0
    for line in block[match.end() :].splitlines():
        if not line.strip():
            continue
        bullet = CRITERION.match(line)
        if not bullet:
            break
        ordinal += 1
        out.append((ordinal, bullet.group(1).lower() == "x", bullet.group(2)))
    return out
