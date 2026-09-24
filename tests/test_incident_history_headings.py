"""docs/INCIDENT_HISTORY.md heading-depth lint.

The merge driver (`scripts/resolve_doc_conflict.py::parse_history`) only
recognizes an incident entry when its heading is exactly `### YYYY-MM-DD -
...` -- a leading ISO date behind exactly three `#`. Anything else is
invisible to it: its text gets silently glued onto whichever entry came
before it, so two branches that both touch the file can falsely collide, or
a fix can falsely appear to merge cleanly while quietly losing content.
Item 93 (2026-09-24) found SEVENTEEN entries that had slipped past this gate
in two different shapes, all fixed by hand in that same change:

  (a) the heading is at the wrong hash depth (## or ####+) but still leads
      with the date -- the driver's own `_MISWRITTEN_ENTRY_RE` names this
      shape, but that regex is only ever consulted INSIDE a merge conflict
      (`_new_miswritten_entries`), so it never runs on an ordinary commit;
  (b) the heading is at the right depth (###) but the date trails the title
      instead of leading it, so `_ENTRY_HEADING_RE` never matches it even
      though the hash count looks correct. `docs/INCIDENT_HISTORY.md`'s own
      de-levering-ladder entry (PR #502) was written this way.

These two tests scan the real, committed file -- not a fixture -- so both
shapes fail an ordinary commit's test run, not only a merge.
"""
from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
_DOC = REPO_ROOT / "docs" / "INCIDENT_HISTORY.md"
_SCRIPT = REPO_ROOT / "scripts" / "resolve_doc_conflict.py"

_LEADING_DATE_RE = re.compile(r"^#{1,6}\s+\d{4}-\d{2}-\d{2}\b")
_TRAILING_DATE_RE = re.compile(r"\(\d{4}-\d{2}-\d{2}\)\s*$")
_HEADING_RE = re.compile(r"^#{1,6}\s+.*$")


def _load_resolve_doc_conflict():
    spec = importlib.util.spec_from_file_location("resolve_doc_conflict", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    # dataclasses resolves annotations through sys.modules; register before exec
    sys.modules["resolve_doc_conflict"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_no_dated_entry_sits_at_the_wrong_hash_depth():
    """Defect (a): a dated heading at ## or ####+ is invisible to the merge
    driver's ###-only parser. Reuses the driver's own
    `_MISWRITTEN_ENTRY_RE` so this can never drift from what the driver
    itself considers wrong -- the gap being closed is only that this now
    also runs on an ordinary commit, not only inside a merge conflict."""
    rdc = _load_resolve_doc_conflict()
    text = _DOC.read_text(encoding="utf-8")
    bad = [
        line.strip()
        for line in text.splitlines()
        if rdc._MISWRITTEN_ENTRY_RE.match(line)
    ]
    assert not bad, (
        "docs/INCIDENT_HISTORY.md has entry heading(s) at the wrong hash "
        "depth -- the merge driver cannot see them (item 93):\n  "
        + "\n  ".join(bad)
    )


def test_no_top_level_entry_has_its_date_trailing_instead_of_leading():
    """Defect (b): a heading correctly at ### can still be invisible to
    `_ENTRY_HEADING_RE` if its date trails the title instead of leading it.

    Scoped to headings that open a new entry -- the line right after a
    `---` separator, this file's own documented entry boundary ("a span
    runs from its heading to just before the next one, so it carries its
    own trailing --- separator with it") -- so a legitimate internal
    sub-heading that merely mentions a date in passing, e.g. this file's
    own `### Fixed the next day (2026-09-02)`, is not flagged: it is nested
    prose inside its parent entry, not a slipped-through entry of its own.
    """
    lines = _DOC.read_text(encoding="utf-8").splitlines()
    bad = []
    prev_nonblank = None
    for line in lines:
        stripped = line.strip()
        if stripped == "":
            continue
        if (
            _HEADING_RE.match(line)
            and prev_nonblank == "---"
            and _TRAILING_DATE_RE.search(line)
            and not _LEADING_DATE_RE.match(line)
        ):
            bad.append(line.strip())
        prev_nonblank = stripped
    assert not bad, (
        "docs/INCIDENT_HISTORY.md has entry heading(s) whose date trails "
        "the title instead of leading it -- the merge driver cannot see "
        "them (item 93):\n  " + "\n  ".join(bad)
    )
