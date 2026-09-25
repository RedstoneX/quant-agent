"""Item 93 recurrence lint.

docs/INCIDENT_HISTORY.md's merge driver keys off `###` to identify individual
incident entries. An entry heading accidentally written as `##` (two hashes)
is invisible to that driver, so concurrent branches can collide without the
merge tool noticing.

This test parses the heading tree and fails if any *dated entry* heading
(one whose text starts with a `YYYY-MM-DD` date, e.g.
``### 2026-09-10 — ...``) sits at `##` while having no `###` (or deeper)
child heading nested under it. A `##` heading that starts with a date but
legitimately groups real `###` sub-entries (e.g. an incident writeup broken
into "Finding 1" / "Finding 2" children) is a container, not a mis-leveled
entry, and is allowed to stay at `##`.

Kept deterministic and dependency-free: no markdown library, just a regex
over heading lines.
"""

import re
from pathlib import Path

INCIDENT_HISTORY = (
    Path(__file__).resolve().parent.parent / "docs" / "INCIDENT_HISTORY.md"
)

HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
DATE_ENTRY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}\b")


def _parse_headings(text: str):
    """Return a list of (line_no, level, title) for every markdown heading."""
    headings = []
    for i, line in enumerate(text.splitlines(), start=1):
        m = HEADING_RE.match(line)
        if m:
            headings.append((i, len(m.group(1)), m.group(2).strip()))
    return headings


def _misleveled_entries(headings):
    """Find level-2 (##) headings that look like a dated entry but have no
    genuine level-3 CHILD nested under them before the next level<=2 heading.

    A genuine child is a thematic sub-section of the same incident (e.g.
    "Finding 1", "Bug 1", "Fixed the next day (2026-09-02)") — its title
    does not itself start with a bare date. A level-3 heading that DOES
    start with a bare date is another independent, dated incident entry
    that merely follows in the file; it is a SIBLING wrongly read as a
    child by level alone, not proof that the ## heading above it is a
    legitimate container.
    """
    bad = []
    for idx, (line_no, level, title) in enumerate(headings):
        if level != 2:
            continue
        if not DATE_ENTRY_RE.match(title):
            # Not a dated entry (e.g. "Archive", "OPEN FINDINGS raised..."),
            # so it is a legitimate top-level section header.
            continue

        has_child = False
        for _, sub_level, sub_title in headings[idx + 1 :]:
            if sub_level <= 2:
                break
            if sub_level == 3 and not DATE_ENTRY_RE.match(sub_title):
                has_child = True
                break
        if not has_child:
            bad.append((line_no, title))
    return bad


def test_incident_history_file_exists():
    assert INCIDENT_HISTORY.is_file(), INCIDENT_HISTORY


def test_no_misleveled_incident_entry_headings():
    text = INCIDENT_HISTORY.read_text(encoding="utf-8")
    headings = _parse_headings(text)
    assert headings, "expected to find at least one heading in INCIDENT_HISTORY.md"

    bad = _misleveled_entries(headings)
    assert not bad, (
        "found incident entry heading(s) at '##' that should be '###' "
        "(the merge driver only recognizes '###' entries): "
        + "; ".join(f"line {ln}: {title!r}" for ln, title in bad)
    )
