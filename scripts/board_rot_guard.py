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
from scripts.board_locator import trunk_board, working_board
from scripts.guard_reference import (
    ROOT,
    ReferenceUnavailable,
    TRUNK,
    trunk_blobs,
    trunk_paths,
)


#: The board parser's own stop markers for the funnel queue, in the order it
#: tries them. The queue is the last numbered section of the board, so a copy
#: whose queue is never closed by one of these was cut off part-way: the parser
#: would still return every item ABOVE the cut and the delta would look clean.
#: (Until 2026-10-04 this guard inferred "did not parse" from a picked floor on
#: the open-item count, which made a board that legitimately shrank below the
#: floor indistinguishable from one the parser could not read.)
_QUEUE_STOPS = ("### Re-measure gate", "\n## ", "\n### ")

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


def _board_structure_problems(work: Path) -> list[str]:
    """Plain-English reasons this copy of the board is not structurally whole.

    Direct checks only, each on the file's own shape: the parser's own
    "problem" sentences for both numbered sections, plus whether the funnel
    queue is closed by one of the parser's stop markers rather than running
    off the end of the file. The number of items is deliberately not asked
    about -- an empty gate or a short queue is a legitimate state of a board
    that has had its work finished, and must stay representable.
    """
    problems: list[str] = []
    notes = sb.load_board_notes(work.parent / 'board_notes')
    for loader in (sb.load_funnel_queue, sb.load_pm_gate):
        _items, problem = loader(work, notes=notes)
        if problem:
            problems.append(problem)
    if work.exists():
        text = work.read_text(encoding="utf-8")
        if sb._QUEUE_HEADING in text:
            tail = text.split(sb._QUEUE_HEADING, 1)[1]
            if not any(stop in tail for stop in _QUEUE_STOPS):
                problems.append(
                    f"the {sb._QUEUE_HEADING.lstrip('# ')!r} section is never "
                    f"closed by a following heading, so the file is truncated "
                    f"part-way through the queue."
                )
    return problems


def _measure(root: Path, located: tuple[str, str]) -> tuple[set[str], list[str]]:
    """(flagged refs, structure problems) for one copy of the board.

    The problems are the anti-vacuity half: a board the parser could not read
    flags nothing, which would make the delta trivially empty forever, so the
    caller refuses whenever this list is non-empty.
    """
    work, notes_dir = root / located[0], root / located[1]
    problems = _board_structure_problems(work)
    if problems:
        return set(), problems
    return _refs(sb.find_finished_items_still_on_board(work, notes_dir)), []


def working_flagged() -> set[str]:
    """Items this working tree declares finished while still on the board."""
    located = working_board()
    flagged, problems = _measure(ROOT, located)
    if problems:
        raise ReferenceUnavailable(
            f"{located[0]} in this working tree did not parse as a board "
            f"({' '.join(problems)}); the measurement is vacuous, so this guard "
            f"refuses rather than report an empty delta."
        )
    return flagged


def trunk_board_paths() -> list[str]:
    """The board (found by shape) and every file in its notes directory, on the trunk."""
    work, notes = trunk_board()
    return [p for p in trunk_paths() if p == work or p.startswith(notes + "/")]


def trunk_flagged() -> set[str]:
    """The same measurement taken against the board as it stands on the trunk.

    The trunk's board is materialised into a throwaway directory because the
    parser takes paths, and reusing it unchanged is the whole point: a second
    parser could disagree with the real one and flag phantom rot.
    """
    work, notes = trunk_board()
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
        flagged, problems = _measure(root, (work, notes))
    if problems:
        raise ReferenceUnavailable(
            f"{TRUNK}:{work} did not parse as a board ({' '.join(problems)}); "
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
        board_path, notes_path = working_board()
        print(
            "this branch adds %d board item(s) that declare themselves finished "
            "(headline, or every DONE WHEN box ticked) while still sitting in "
            "%s, beyond what %s already carries:\n  %s\n"
            "Retire each one the normal way: write it up in "
            "docs/INCIDENT_HISTORY.md, delete its block from %s and its "
            "## item N block from %s/, and add its number to the retired line."
            % (len(bad), board_path, TRUNK, "\n  ".join(bad), board_path, notes_path),
            file=sys.stderr,
        )
        return 1
    print(f"board-rot guard: this tree adds no newly finished-but-open item "
          f"against {TRUNK}.")
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())
