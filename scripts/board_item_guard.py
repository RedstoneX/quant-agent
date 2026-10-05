"""Per-item budget and note-pointer rule for ``docs/WORK.md``, stored nowhere.

The board's design is a one-line item plus a pointer, with the detail in
``docs/board_notes/item-NNN.md``. Two rules enforce that half:

  over_budget   an item block larger than the per-item budget -- the file's
                own byte cap divided by ``MAX_OPEN_ITEMS``.
  pointerless   an item block that carries no ``detail: docs/board_notes/
                item-NNN.md`` pointer to a note of its own, or whose pointer
                names a file that is not there.

NO GRANDFATHERED LIST
---------------------
``tests/test_status_board.py`` used to carry ``_WORK_MD_OVERSIZE_ON_ARRIVAL``
(eight items with their byte size on 2026-10-01) and
``_WORK_MD_POINTERLESS_ON_ARRIVAL`` (four item numbers). Both were cached
measurements committed to the repo -- the same collision engine as the JSON
baselines, written in Python. Every change that touched a listed item had to
edit the list, so unrelated changes jammed each other, and the dict could be
quietly widened until the check passed.

So this stores nothing (docs/GUARDS_WITHOUT_STORED_STATE.md). At check time it
names every offending item in the working tree, names them again on the board
as it stands on ``origin/main``, and fails on any IDENTITY (item number, rule)
the tree holds that the trunk does not -- never a total, so retiring one
offender and filing a different one still fails. An item already offending on
the trunk is pre-existing rot, not this change's business; retiring it, or
fixing it, simply makes it disappear from both sides. If ``origin/main``
cannot be read it REFUSES; it never passes by default.

The budget's divisor below is the one hand-written POLICY number here; it is
not a measurement and nothing measures it.

Run it directly: ``PYTHONPATH=. .venv/bin/python -m scripts.board_item_guard``.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

from scripts.board_locator import trunk_board, working_board
from scripts.guard_reference import (
    ROOT,
    ReferenceUnavailable,
    TRUNK,
    added_sites,
    trunk_blobs,
    trunk_paths,
)


#: THIS IS A CHOSEN WORKING FIGURE, NOT A MEASURED ONE. It is not derived from
#: anything and nothing measures it; 25 items were open when the per-item
#: budget landed [measured 2026-10-01], and 40 is a round allowance for growth
#: picked by hand. That is acceptable here only because this number governs
#: the length of a documentation file and no trade, position, stop or order
#: whatsoever. Do not copy this pattern into anything that spends.
MAX_OPEN_ITEMS = 40

#: Same shape as `_QUEUE_ITEM_RE`/`_ITEM_OPEN_RE` in scripts/status_board.py:
#: an item block starts at its bold `**N. ` heading and runs to the next
#: heading, or to the retired-numbers paragraph that closes the list.
ITEM_HEADING_RE = re.compile(r"^\*\*(?:~~)?(\d+)\.\s", re.M)
RETIRED_PARA = "**Retired item numbers"

#: How the board spells a pointer: `detail: docs/board_notes/item-177.md`.
#: Zero-padded to three digits, which is why the number is compared as an int.
NOTE_POINTER_RE = re.compile(r"board_notes/item-(\d+)\.md")
NOTE_FILE_RE = re.compile(r"item-(\d+)\.md")

#: One offending item: (item number as written, rule).
Site = tuple[str, str]


def item_budget_bytes(root: Path = ROOT) -> int:
    """The per-item budget: the board's own byte cap over ``MAX_OPEN_ITEMS``."""
    from scripts.check_board_hygiene import read_cap_bytes

    cap, error = read_cap_bytes(root)
    if error is not None or cap is None:
        raise ReferenceUnavailable(f"cannot read the board's byte cap: {error}")
    return cap // MAX_OPEN_ITEMS


def item_blocks(text: str) -> list[tuple[str, str]]:
    """``[(number, block_text)]`` for every numbered item block in ``text``."""
    stop = text.find(RETIRED_PARA)
    if stop == -1:
        stop = len(text)
    heads = [m for m in ITEM_HEADING_RE.finditer(text) if m.start() < stop]
    blocks = []
    for i, m in enumerate(heads):
        end = heads[i + 1].start() if i + 1 < len(heads) else stop
        blocks.append((m.group(1), text[m.start():end].rstrip() + "\n"))
    return blocks


def notes_in(paths: list[str]) -> set[int]:
    """Item numbers that have a note file, from a list of tracked paths."""
    out: set[int] = set()
    for p in paths:
        head, _, name = p.rpartition("/")
        m = NOTE_FILE_RE.fullmatch(name)
        if head.rpartition("/")[2] == "board_notes" and m:
            out.add(int(m.group(1)))
    return out


def offences(text: str, budget: int, notes: set[int]) -> dict[Site, int]:
    """Every offending (item, rule) in one copy of the board, with its size.

    ``over_budget`` carries the block's byte size; ``pointerless`` carries 0.
    """
    out: dict[Site, int] = {}
    for n, block in item_blocks(text):
        if len(block) > budget:
            out[(n, "over_budget")] = len(block)
        targets = {int(x) for x in NOTE_POINTER_RE.findall(block)}
        if int(n) not in targets or int(n) not in notes:
            out[(n, "pointerless")] = 0
    return out


def working_notes(root: Path = ROOT) -> set[int]:
    """Note files the working tree's ``docs/board_notes/`` actually holds --
    the same directory listing ``scripts.status_board.load_board_notes`` uses,
    so a pointer resolves here exactly as it resolves there."""
    notes_dir = working_board()[1]
    directory = root / notes_dir
    if not directory.is_dir():
        return set()
    return notes_in(sorted(f"{notes_dir}/{p.name}" for p in directory.glob("item-*.md")))


def working_offences(root: Path = ROOT) -> dict[Site, int]:
    work_md = root / working_board()[0]
    return offences(work_md.read_text(encoding="utf-8"), item_budget_bytes(root), working_notes(root))


def trunk_offences(budget: int) -> dict[Site, int]:
    """Every offending item on ``origin/main``'s board, held to the SAME budget."""
    work = trunk_board()[0]
    return offences(trunk_blobs([work])[work], budget, notes_in(trunk_paths(".md")))


def violations() -> list[str]:
    """Every (item, rule) offence this working tree holds that ``origin/main`` does not."""
    work_md_path, notes_dir = working_board()
    budget = item_budget_bytes()
    now = working_offences()
    before = trunk_offences(budget)
    bad: list[str] = []
    # Identities only: the dict VALUES are byte sizes, not copy counts, so they
    # must not reach `added_sites` (it reads a mapping's values as occurrences).
    for (n, rule), _copies, _was in added_sites(now.keys(), before.keys()):
        if rule == "over_budget":
            bad.append(
                f"item {n} is {now[(n, rule)]:,} bytes, over the {budget:,}-byte per-item "
                f"budget, and was not over it on {TRUNK}. The budget is the file's own byte "
                f"cap divided by {MAX_OPEN_ITEMS}, a CHOSEN allowance for open items, not a "
                "measured one -- it bounds a documentation file and nothing that trades. "
                "TO FIX, and do NOT delete anything: move the item's prose into "
                f"`{notes_dir}/item-NNN.md` under its `## item N` heading -- create that file "
                "if it is not there -- and leave behind only the bold title line, the DONE "
                "WHEN checkboxes in short form, and the `detail: docs/board_notes/item-NNN.md` "
                "pointer. Never raise this number to make room, never shorten somebody else's "
                "item to make room for yours, and never retire a live item to get under it."
            )
        else:
            bad.append(
                f"item {n} does not resolve to a note of its own, and was not pointerless on {TRUNK}. Add a "
                f"`detail: {notes_dir}/item-NNN.md` line to the block and put the prose in "
                "that file under a `## item N` heading."
            )
    return bad


def main(argv: list[str] | None = None) -> int:
    try:
        bad = violations()
    except ReferenceUnavailable as exc:
        print(f"REFUSING: {exc}", file=sys.stderr)
        return 2
    if bad:
        work_md_path = working_board()[0]
        print(f"{work_md_path} item(s) newly offending against {TRUNK}:\n" + "\n".join(bad), file=sys.stderr)
        return 1
    print(f"board-item guard: this tree adds no over-budget or pointerless item against {TRUNK}.")
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())
