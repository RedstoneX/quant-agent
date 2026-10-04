"""Finished-but-still-open board items, measured against the trunk, stored nowhere.

``docs/WORK.md`` holds only OPEN work; a finished item belongs in
``docs/INCIDENT_HISTORY.md`` with its ``docs/board_notes/`` block deleted.
``scripts.status_board.find_finished_items_still_on_board`` finds items that
declare themselves finished -- in their headline or by having every one of
their own ``DONE WHEN`` boxes ticked -- while still sitting on the board.

NO STORED LIST OF CURRENT ITEMS
-------------------------------
``tests/test_status_board.py`` used to carry
``_KNOWN_CHECKBOX_FINISHED_ITEMS_2026_09_26``, a frozenset of the ten items
flagged on that date. That was a cached measurement committed to the repo --
the same collision engine as the JSON baselines, written in Python. Every
change that retired an item had to edit it, so unrelated changes jammed each
other, and the set could be quietly widened until the check passed.

So this stores nothing (docs/GUARDS_WITHOUT_STORED_STATE.md). At check time it
reads the flagged items in the working tree, reads them again from the board as
it stands on ``origin/main``, and reports the DELTA -- the items THIS change
made newly finished-but-still-open. Items already flagged on the trunk are
pre-existing rot, not this change's business, and retiring one simply makes it
disappear from both sides. If ``origin/main`` cannot be read it REFUSES; it
never passes by default.

Run it directly: ``python -m scripts.board_rot_guard``.
"""
from __future__ import annotations

import re
import sys
import tempfile
from pathlib import Path

from scripts import status_board as sb
from scripts.guard_reference import (
    ROOT,
    ReferenceUnavailable,
    TRUNK,
    trunk_blobs,
    trunk_paths,
)

WORK_MD = "docs/WORK.md"
BOARD_NOTES = "docs/board_notes"

#: Below this the board did not really parse, and an empty delta means nothing.
MIN_OPEN_ITEMS = 20

#: A flagged string starts with the item's own reference; the rest is the
#: retirement procedure, which is identical for every item and would make two
#: runs incomparable on wording alone.
_REF_PREFIX_RE = re.compile(r"^(gate item \d+|item \d+)")


def _refs(flagged: list[str]) -> set[str]:
    refs = set()
    for line in flagged:
        match = _REF_PREFIX_RE.match(line)
        if not match:
            raise ValueError(f"unrecognised flagged-item shape: {line!r}")
        refs.add(match.group(1))
    return refs


def _measure(root: Path) -> tuple[set[str], int]:
    """(flagged refs, open items parsed) for one copy of the board.

    The item count is the anti-vacuity half: a board that parses to nothing
    flags nothing, which would make the delta trivially empty forever.
    """
    work, notes_dir = root / WORK_MD, root / BOARD_NOTES
    notes = sb.load_board_notes(notes_dir)
    parsed = 0
    for items, _problem in (sb.load_funnel_queue(work, notes=notes),
                            sb.load_pm_gate(work, notes=notes)):
        parsed += sum(1 for item in items if not item.done)
    return _refs(sb.find_finished_items_still_on_board(work, notes_dir)), parsed


def working_flagged() -> set[str]:
    """Items this working tree declares finished while still on the board."""
    flagged, parsed = _measure(ROOT)
    if parsed < MIN_OPEN_ITEMS:
        raise ReferenceUnavailable(
            f"only {parsed} open board item(s) parsed out of {WORK_MD} in this "
            f"working tree; the measurement is vacuous, so this guard refuses "
            f"rather than report an empty delta."
        )
    return flagged


def trunk_board_paths() -> list[str]:
    """``docs/WORK.md`` and every ``docs/board_notes/`` file on the trunk."""
    paths = [p for p in trunk_paths()
             if p == WORK_MD or p.startswith(BOARD_NOTES + "/")]
    if WORK_MD not in paths:
        raise ReferenceUnavailable(
            f"{TRUNK} has no {WORK_MD}: the board this guard compares against is "
            f"not there, so it refuses rather than pass."
        )
    return paths


def trunk_flagged() -> set[str]:
    """The same measurement taken against the board as it stands on the trunk.

    The trunk's board is materialised into a throwaway directory because the
    parser takes paths, and reusing it unchanged is the whole point: a second
    parser could disagree with the real one and flag phantom rot.
    """
    paths = trunk_board_paths()
    blobs = trunk_blobs(paths)
    missing = [p for p in paths if p not in blobs]
    if missing:
        raise ReferenceUnavailable(
            f"could not read {len(missing)} board file(s) from {TRUNK} "
            f"(first: {missing[0]}); refusing rather than measuring a partial board."
        )
    with tempfile.TemporaryDirectory(prefix="trunk-board-") as tmp:
        root = Path(tmp)
        for path, text in blobs.items():
            dest = root / path
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(text, encoding="utf-8")
        flagged, parsed = _measure(root)
    if parsed < MIN_OPEN_ITEMS:
        raise ReferenceUnavailable(
            f"only {parsed} open board item(s) parsed out of {TRUNK}:{WORK_MD}; "
            f"the reference measurement is vacuous, so this guard refuses rather "
            f"than compare against nothing."
        )
    return flagged


def violations() -> list[str]:
    """Items newly finished-but-still-open on this branch versus the trunk."""
    now = working_flagged()
    before = trunk_flagged()
    return sorted(now - before)


def main(argv: list[str] | None = None) -> int:
    try:
        bad = violations()
    except ReferenceUnavailable as exc:
        print(f"REFUSING: {exc}", file=sys.stderr)
        return 2
    if bad:
        print(
            "this branch adds %d board item(s) that declare themselves finished "
            "(headline, or every DONE WHEN box ticked) while still sitting in "
            "%s, beyond what %s already carries:\n  %s\n"
            "Retire each one the normal way: write it up in "
            "docs/INCIDENT_HISTORY.md, delete its block from %s and its "
            "## item N block from %s/, and add its number to the retired line."
            % (len(bad), WORK_MD, TRUNK, "\n  ".join(bad), WORK_MD, BOARD_NOTES),
            file=sys.stderr,
        )
        return 1
    print(f"board-rot guard: this tree adds no newly finished-but-open item "
          f"against {TRUNK}.")
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())
