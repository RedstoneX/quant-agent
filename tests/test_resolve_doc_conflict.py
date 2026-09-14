"""`scripts/resolve_doc_conflict.py` — the item-aware board-document resolver.

Every test here is a shape that actually destroyed live board content, or a
post-condition that would have caught it. The resolver it replaces lived in a
session scratchpad and applied "union of the deletions", which is right only
when each side deleted a DIFFERENT item; it deleted five live items once and
both halves of a number collision twice, and each time it left valid markdown
with no conflict marker, so nothing failed.

Refusing is a PASS here. A merge that cannot be made safely must stop for a
human — writing a plausible-looking file is the defect.
"""
from __future__ import annotations

import importlib.util
import sys
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
_SCRIPT = REPO_ROOT / "scripts" / "resolve_doc_conflict.py"


def _load():
    spec = importlib.util.spec_from_file_location("resolve_doc_conflict", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    # dataclasses resolves annotations through sys.modules; register before exec
    sys.modules["resolve_doc_conflict"] = mod
    spec.loader.exec_module(mod)
    return mod


rdc = _load()


# ---------------------------------------------------------------------------
# Fixtures — the real headings, because the resolver reads the merged file
# back with the board's own parsers and those key off these exact strings.
# ---------------------------------------------------------------------------

WORK_BASE = """\
# QAMC Current Work

## PM TEST GATE — garbage in, garbage out

**7. A gate item — OPEN.** Gate body seven.

**8. A second gate item — OPEN.** Gate body eight.

## THE FUNNEL QUEUE — why trades do not happen, ranked by measured cost

**1. The first queue item — OPEN.** Queue body one.

**2. The second queue item — OPEN.** Queue body two.

**3. The third queue item — OPEN.** Queue body three.

**8. The queue's own item eight, unrelated to the gate's — OPEN.** Queue body eight.

**Retired item numbers — never reuse.** 4, 5 in this queue, and 1, 2 in the PM test gate, were deleted once written up in `docs/INCIDENT_HISTORY.md`.

## Evidence-only follow-ups

- nothing here.
"""


def _drop_item(text: str, headline_prefix: str) -> str:
    """Remove one item block, the way a branch that closes an item does."""
    out, skipping = [], False
    for line in text.splitlines(keepends=True):
        if line.startswith(headline_prefix):
            skipping = True
            continue
        if skipping:
            if line.startswith("**") or line.startswith("#"):
                skipping = False
            else:
                continue
        out.append(line)
    return "".join(out)


def _add_queue_item(text: str, block: str) -> str:
    """Append an item to the end of the funnel queue, above the retired line."""
    return text.replace("\n**Retired item numbers", f"\n{block}\n**Retired item numbers")


def _set_retired(text: str, queue: str, gate: str) -> str:
    for line in text.splitlines():
        if line.startswith("**Retired item numbers"):
            new = (f"**Retired item numbers — never reuse.** {queue} in this "
                   f"queue, and {gate} in the PM test gate, were deleted once "
                   "written up in `docs/INCIDENT_HISTORY.md`.")
            return text.replace(line, new)
    raise AssertionError("fixture has no retired line")


# ---------------------------------------------------------------------------
# The shape the old resolver got RIGHT, which must keep working
# ---------------------------------------------------------------------------


def test_two_branches_closing_different_items_keep_every_other_item():
    """Each side deleted a different item. Both closures stand; nothing else
    moves. This is the one case "union of the deletions" handled, and it is
    the common case, so the replacement must not regress it."""
    ours = _set_retired(_drop_item(WORK_BASE, "**1. The first"), "1, 4, 5", "1, 2")
    theirs = _set_retired(_drop_item(WORK_BASE, "**2. The second"), "2, 4, 5", "1, 2")

    merged = rdc.resolve_work(WORK_BASE, ours, theirs)

    queue = [s for s in rdc.parse_sections(merged)
             if s.key.startswith("## THE FUNNEL QUEUE")][0]
    assert sorted(queue.order) == [3, 8]
    gate = [s for s in rdc.parse_sections(merged)
            if s.key.startswith("## PM TEST GATE")][0]
    assert sorted(gate.order) == [7, 8], "the gate's own numbering is untouched"
    assert "Queue body three" in merged


def test_two_branches_adding_different_items_keep_both():
    """The other everyday case: two parallel findings filed as 9 and 10.
    Line-level merge conflicts on adjacent additions; item-level does not."""
    ours = _add_queue_item(WORK_BASE, "**9. Ours, a new finding — OPEN.** Nine body.\n")
    theirs = _add_queue_item(WORK_BASE, "**10. Theirs, a new finding — OPEN.** Ten body.\n")

    merged = rdc.resolve_work(WORK_BASE, ours, theirs)

    queue = [s for s in rdc.parse_sections(merged)
             if s.key.startswith("## THE FUNNEL QUEUE")][0]
    assert sorted(queue.order) == [1, 2, 3, 8, 9, 10]
    assert "Nine body" in merged and "Ten body" in merged
    # Deterministic placement: both new items anchor after the same existing
    # item, and ours goes first every time rather than whichever way the last
    # insertion happened to push.
    assert queue.order == [1, 2, 3, 8, 9, 10]


def test_the_same_number_in_two_schemes_is_not_a_collision():
    """`docs/WORK.md` carries independently numbered lists, so 8 is a real
    item in the gate AND a different real item in the queue. A resolver that
    keyed on the bare number would merge one into the other."""
    sections = rdc.parse_sections(WORK_BASE)
    gate = [s for s in sections if s.key.startswith("## PM TEST GATE")][0]
    queue = [s for s in sections if s.key.startswith("## THE FUNNEL QUEUE")][0]
    assert 8 in gate.items and 8 in queue.items
    assert gate.items[8] != queue.items[8]


# ---------------------------------------------------------------------------
# The shapes that destroyed live items — every one must REFUSE
# ---------------------------------------------------------------------------


def test_a_number_collision_stops_for_a_human_and_shows_both_texts():
    """Two agents independently numbered a new finding the same. The old
    resolver deleted BOTH. A collision is a renumber, never a delete."""
    ours = _add_queue_item(WORK_BASE, "**9. Ours: signal_weight cannot invert — OPEN.** Ours body.\n")
    theirs = _add_queue_item(WORK_BASE, "**9. Theirs: the backtest rations alphabetically — OPEN.** Theirs body.\n")

    with pytest.raises(rdc.Refusal) as exc:
        rdc.resolve_work(WORK_BASE, ours, theirs)

    msg = str(exc.value)
    assert "NUMBER COLLISION" in msg
    assert "signal_weight cannot invert" in msg, "the human must see ours"
    assert "rations alphabetically" in msg, "the human must see theirs"
    assert "renumber" in msg.lower()


def test_an_item_that_vanishes_from_the_merged_text_refuses_the_write():
    """The post-condition, tested directly on a doctored result: an item that
    neither side retired is missing. A vanished item is indistinguishable from
    an answered one, so this must never be written."""
    ours = _add_queue_item(WORK_BASE, "**9. A new finding — OPEN.** Nine body.\n")
    theirs = WORK_BASE
    expected = rdc._expected_survivors(WORK_BASE, ours, theirs)
    good = rdc.resolve_work(WORK_BASE, ours, theirs)
    doctored = _drop_item(good, "**3. The third")

    with pytest.raises(rdc.Refusal) as exc:
        rdc._assert_work_postconditions(doctored, WORK_BASE, ours, theirs, expected)

    msg = str(exc.value)
    assert "vanished: [3]" in msg
    assert "Refusing to write" in msg


def test_a_duplicated_item_number_refuses_the_write():
    """The repair after the first destruction appended a second byte-identical
    copy of each deleted item instead of reinstating it; the duplicate keys
    read to the board parser as five MISSING items."""
    duped = WORK_BASE.replace(
        "**3. The third queue item — OPEN.** Queue body three.\n",
        "**3. The third queue item — OPEN.** Queue body three.\n\n"
        "**3. The third queue item — OPEN.** Queue body three.\n",
    )
    with pytest.raises(rdc.Refusal) as exc:
        rdc.resolve_work(WORK_BASE, duped, WORK_BASE)
    assert "appears twice" in str(exc.value)


def test_a_merge_that_would_breach_the_byte_cap_refuses():
    """Main went over the 100,000-byte cap twice in one night. The cap is a
    post-condition of the merge, not something to discover in CI."""
    filler = "x" * 60_000
    ours = _add_queue_item(WORK_BASE, f"**9. Ours — OPEN.** {filler}\n")
    theirs = _add_queue_item(WORK_BASE, f"**10. Theirs — OPEN.** {filler}\n")

    with pytest.raises(rdc.Refusal) as exc:
        rdc.resolve_work(WORK_BASE, ours, theirs)
    msg = str(exc.value)
    assert "100,000-byte cap" in msg
    assert "redden main" in msg


def test_a_number_cannot_be_live_and_retired_in_the_same_scheme():
    """One side retired item 2 in the list but left the item standing. The
    file would then say the number is both finished and open."""
    ours = _set_retired(WORK_BASE, "2, 4, 5", "1, 2")
    with pytest.raises(rdc.Refusal) as exc:
        rdc.resolve_work(WORK_BASE, ours, WORK_BASE)
    assert "still live" in str(exc.value)


def test_a_section_heading_that_differs_between_the_sides_refuses():
    """Which scheme an item belongs to is decided by the heading above it. If
    the two sides disagree about the headings, the schemes are ambiguous."""
    theirs = WORK_BASE.replace("## Evidence-only follow-ups", "## Evidence follow-ups")
    with pytest.raises(rdc.Refusal) as exc:
        rdc.resolve_work(WORK_BASE, WORK_BASE, theirs)
    assert "section headings" in str(exc.value)


# ---------------------------------------------------------------------------
# The retired-item-numbers line
# ---------------------------------------------------------------------------


def test_the_retired_line_is_the_union_of_both_sides_lists():
    ours = _set_retired(_drop_item(WORK_BASE, "**1. The first"), "1, 4, 5", "1, 2")
    theirs = _set_retired(_drop_item(WORK_BASE, "**2. The second"), "2, 4, 5", "1, 2")

    merged = rdc.resolve_work(WORK_BASE, ours, theirs)
    line = [s.retired for s in rdc.parse_sections(merged) if s.retired][0]
    queue, gate, *_ = rdc.parse_retired(line)

    assert queue == [1, 2, 4, 5]
    assert gate == [1, 2]


def test_the_retired_line_is_parsed_from_its_own_lists_not_scraped_from_prose():
    """Scraping integers out of the surrounding sentences pulled stray digits
    in and corrupted the line badly enough that a separate session had to
    rebuild it from git history. The tail of the real line is full of numbers
    that are NOT retired numbers; none may end up in either list."""
    line = ("**Retired item numbers — never reuse.** 4, 5 in this queue, and "
            "1, 2 in the PM test gate, were deleted once written up. Items 62, "
            "63 and 65 were renumbered on 2026-09-14 and 90, 101, 200 never "
            "existed.\n")
    queue, gate, *_ = rdc.parse_retired(line)
    assert queue == [4, 5]
    assert gate == [1, 2]


def test_a_retired_line_whose_shape_changed_refuses_rather_than_guessing():
    with pytest.raises(rdc.Refusal) as exc:
        rdc.parse_retired("**Retired item numbers — never reuse.** none yet.\n")
    assert "cannot be parsed" in str(exc.value)


def test_closing_an_item_on_one_side_survives_the_merge_end_to_end():
    """The whole point: a branch closes item 3 (deletes it, adds 3 to the
    retired list) while another branch files item 9. Both land."""
    ours = _set_retired(_drop_item(WORK_BASE, "**3. The third"), "3, 4, 5", "1, 2")
    theirs = _add_queue_item(WORK_BASE, "**9. A parallel finding — OPEN.** Nine body.\n")

    merged = rdc.resolve_work(WORK_BASE, ours, theirs)
    queue = [s for s in rdc.parse_sections(merged)
             if s.key.startswith("## THE FUNNEL QUEUE")][0]
    assert sorted(queue.order) == [1, 2, 8, 9]
    assert rdc.parse_retired(queue.retired)[0] == [3, 4, 5]


# ---------------------------------------------------------------------------
# docs/BOARD_NOTES.md
# ---------------------------------------------------------------------------

NOTES_BASE = """\
# Board notes

Plain-language explanations, keyed by item number.

## item 1

**Plain language:** the first one.

## item 2

**Plain language:** the second one.

## item 3

**Plain language:** the third one.
"""


def test_board_notes_merge_by_item_and_keep_both_new_blocks():
    ours = NOTES_BASE + "\n## item 9\n\n**Plain language:** ours.\n"
    theirs = NOTES_BASE + "\n## item 10\n\n**Plain language:** theirs.\n"
    merged = rdc.resolve_notes(NOTES_BASE, ours, theirs)
    keys = rdc.parse_notes(merged)[1]
    assert sorted(keys) == ["item 1", "item 10", "item 2", "item 3", "item 9"]


def test_two_different_notes_under_one_item_number_stop_for_a_human():
    ours = NOTES_BASE + "\n## item 9\n\n**Plain language:** ours.\n"
    theirs = NOTES_BASE + "\n## item 9\n\n**Plain language:** theirs.\n"
    with pytest.raises(rdc.Refusal) as exc:
        rdc.resolve_notes(NOTES_BASE, ours, theirs)
    assert "NUMBER COLLISION" in str(exc.value)


def test_a_note_left_behind_by_a_deleted_item_refuses():
    """An orphaned note fails the board's own test, and it is also the only
    reason two of the three destructions were noticed at all."""
    work = _set_retired(_drop_item(WORK_BASE, "**3. The third"), "3, 4, 5", "1, 2")
    with pytest.raises(rdc.Refusal) as exc:
        rdc.assert_notes_agree_with_work(work, NOTES_BASE)
    assert "item 3" in str(exc.value)


def test_the_notes_and_the_work_file_agree_when_both_close_the_same_item():
    work = _set_retired(_drop_item(WORK_BASE, "**3. The third"), "3, 4, 5", "1, 2")
    notes = rdc.resolve_notes(
        NOTES_BASE,
        NOTES_BASE.replace("\n## item 3\n\n**Plain language:** the third one.\n", "\n"),
        NOTES_BASE,
    )
    rdc.assert_notes_agree_with_work(work, notes)  # must not raise


# ---------------------------------------------------------------------------
# docs/INCIDENT_HISTORY.md — append-only
# ---------------------------------------------------------------------------

HISTORY_BASE = """\
# QAMC Incident History

Newest first.

---

### 2026-09-13 — the older thing that broke

Body of the older entry.

---

### 2026-09-12 — the oldest thing that broke

Body of the oldest entry.
"""


def test_both_sides_dated_entries_survive_newest_first_with_separators():
    ours = HISTORY_BASE.replace(
        "### 2026-09-13",
        "### 2026-09-14 — ours closed an item\n\nOurs body.\n\n---\n\n### 2026-09-13",
        1,
    )
    theirs = HISTORY_BASE.replace(
        "### 2026-09-13",
        "### 2026-09-14 — theirs closed a different item\n\nTheirs body.\n\n---\n\n### 2026-09-13",
        1,
    )
    merged = rdc.resolve_history(HISTORY_BASE, ours, theirs)

    assert "Ours body" in merged and "Theirs body" in merged
    order = rdc.parse_history(merged)[2]
    assert [rdc._entry_date(k).isoformat() for k in order] == [
        "2026-09-14", "2026-09-14", "2026-09-13", "2026-09-12"]
    # Four entries, so three separators between them plus the preamble's own.
    assert merged.count("\n---\n") == 4
    assert "Body of the oldest entry." in merged


def test_deleting_an_entry_from_the_append_only_log_refuses():
    ours = HISTORY_BASE.replace(
        "### 2026-09-12 — the oldest thing that broke\n\nBody of the oldest entry.\n", "")
    with pytest.raises(rdc.Refusal) as exc:
        rdc.resolve_history(HISTORY_BASE, ours, HISTORY_BASE)
    assert "append-only" in str(exc.value)


def test_existing_history_is_never_reordered_by_a_merge():
    """The committed log is not perfectly date-sorted. Re-sorting it would
    rewrite years of history as a side effect of one merge."""
    unsorted = HISTORY_BASE.replace("2026-09-13", "2026-09-11")
    ours = unsorted.replace(
        "### 2026-09-11",
        "### 2026-09-14 — new\n\nNew body.\n\n---\n\n### 2026-09-11", 1)
    merged = rdc.resolve_history(unsorted, ours, unsorted)
    order = [rdc._entry_date(k).isoformat() for k in rdc.parse_history(merged)[2]]
    assert order == ["2026-09-14", "2026-09-11", "2026-09-12"]


# ---------------------------------------------------------------------------
# The real files
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("kind,rel", [
    ("work", "docs/WORK.md"),
    ("notes", "docs/BOARD_NOTES.md"),
    ("history", "docs/INCIDENT_HISTORY.md"),
])
def test_the_real_documents_round_trip_byte_for_byte(kind: str, rel: str):
    """Merging a document with itself must return it unchanged, byte for byte.

    This is what proves the parser understands the REAL files rather than the
    tidy fixtures above: any span it fails to account for shows up here as a
    diff, and a resolver that reshapes the board while merging it is its own
    kind of silent damage.
    """
    path = REPO_ROOT / rel
    if not path.exists():
        pytest.skip(f"{rel} is not present in this checkout")
    text = path.read_text()
    assert rdc.RESOLVERS[kind](text, text, text) == text


def test_the_real_documents_agree_with_each_other_through_this_tool():
    work = REPO_ROOT / "docs" / "WORK.md"
    notes = REPO_ROOT / "docs" / "BOARD_NOTES.md"
    if not (work.exists() and notes.exists()):
        pytest.skip("board documents are not present in this checkout")
    rdc.assert_notes_agree_with_work(work.read_text(), notes.read_text())


WORK_EMPTY_GATE = """\
# QAMC Current Work

## PM TEST GATE — garbage in, garbage out

**The gate is EMPTY as of 2026-09-14. Every item closed.**

## THE FUNNEL QUEUE — why trades do not happen, ranked by measured cost

**1. The first queue item — OPEN.** Queue body one.

**2. The second queue item — OPEN.** Queue body two.
"""


def test_resolver_round_trips_a_declared_empty_gate():
    """The gate can legitimately have zero items (declared EMPTY). The
    resolver's own post-conditions must not assert a non-empty gate — a
    merge of an empty gate with itself must succeed and the board's own
    parser must read it back as ([], None), i.e. clear, not a problem."""
    merged = rdc.resolve_work(WORK_EMPTY_GATE, WORK_EMPTY_GATE, WORK_EMPTY_GATE)
    assert merged == WORK_EMPTY_GATE
    sb = rdc.status_board()
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "WORK.md"
        p.write_text(merged)
        gate, gate_problem = sb.load_pm_gate(p)
    assert gate == []
    assert gate_problem is None


def test_the_merged_file_is_read_back_with_the_boards_own_parsers():
    """Not a fourth way to read WORK.md: the post-condition is asserted with
    `status_board.load_funnel_queue` / `load_pm_gate`, so this tool and the
    page the owner reads can never disagree about what is on the board."""
    sb = rdc.status_board()
    merged = rdc.resolve_work(
        WORK_BASE, _add_queue_item(WORK_BASE, "**9. New — OPEN.** Nine.\n"), WORK_BASE)
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "WORK.md"
        p.write_text(merged)
        queue, problem = sb.load_funnel_queue(p)
        gate, gate_problem = sb.load_pm_gate(p)
    assert problem is None and gate_problem is None
    assert sorted(i.rank for i in queue) == [1, 2, 3, 8, 9]
    assert sorted(i.rank for i in gate) == [7, 8]
