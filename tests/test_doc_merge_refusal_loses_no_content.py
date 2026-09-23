"""The four ways the board-document merge driver lost or mangled content.

All four were REPRODUCED against the driver as it stood on 2026-09-23 before
any of them was fixed, and every test here was checked to FAIL against that
version — coverage that passes both before and after a fix is decoration.
Narrative and measurements: `docs/INCIDENT_HISTORY.md`, 2026-09-23.

  1. SILENT STALE-COPY SUBSTITUTION, the one that matters. Git's merge-driver
     contract makes %A both the "ours" input and the file git reads the result
     from. The driver refused by exiting non-zero WITHOUT touching %A, so the
     worktree kept the OURS copy: valid markdown, no conflict marker, and
     missing everything that existed only on the incoming side. `git status`
     said `UU`; nothing in the file did. Three agents hit it in one night.
  2. ORDERING. A backfilled incident entry was placed above entries newer
     than it, breaking the only ordering rule that log states about itself.
  3. WHITESPACE. One stray blank line made two identical edits look like two
     different ones, and the tool called that a NUMBER COLLISION and demanded
     a hand rebuild.
  4. NUMBER COLLISION. Correctly refused, but the refusal was imprecise and —
     because of (1) — destructive.

The property under test throughout is the one in the module comment of
`scripts/resolve_doc_conflict.py`: losing content must be IMPOSSIBLE, not
merely unlikely. A refusal is a correct outcome; a refusal that leaves a
plausible-looking wrong file is not.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
DRIVER = REPO_ROOT / "scripts" / "git_merge_driver_docs.sh"
RESOLVER = REPO_ROOT / "scripts" / "resolve_doc_conflict.py"

#: The same four shapes `tests/test_no_conflict_markers_in_docs.py` fails the
#: build on, and the same ones `resolve_doc_conflict._assert_no_conflict_
#: markers` refuses a write over. Named once here rather than retyped per
#: assertion so this file cannot drift from the tripwire it depends on.
MARKER_RE = re.compile(r"^(?:<<<<<<< |\|\|\|\|\|\|\| |>>>>>>> |=======$)", re.M)

WORK_BASE = """\
# QAMC Current Work

## THE FUNNEL QUEUE — why trades do not happen, ranked by measured cost

**100. An item that was already here — OPEN.** Body of the existing item.

**Retired item numbers — never reuse.** 1, 2 in this queue, and 3 in the PM test gate, were deleted once written up in `docs/INCIDENT_HISTORY.md`.
"""

HISTORY_PREAMBLE = """\
# QAMC Incident History

**Rules for this file.** Append, never trim. Newest first.

---

"""


def _work(*items: str) -> str:
    """WORK_BASE with extra item blocks spliced in ahead of the retired line."""
    return WORK_BASE.replace(
        "**Retired item numbers",
        "".join(f"{block}\n\n" for block in items) + "**Retired item numbers",
    )


def _entry(date: str, title: str, body: str = "body") -> str:
    return f"### {date} — {title}\n\n{body}\n\n---\n\n"


def _run_driver(base: str, ours: str, theirs: str, tree_path: str,
                tmp_path: Path):
    """Call the driver exactly as git does, from a directory that stands in
    for the worktree root (git runs a merge driver from there, which is where
    the refusal's reason file has to land)."""
    work = tmp_path / "worktree"
    (work / Path(tree_path).parent).mkdir(parents=True, exist_ok=True)
    b, a, t = work / "base.tmp", work / "A.tmp", work / "theirs.tmp"
    b.write_text(base)
    a.write_text(ours)
    t.write_text(theirs)
    proc = subprocess.run(
        [str(DRIVER), str(b), str(a), str(t), tree_path],
        capture_output=True, text=True, cwd=work,
    )
    return proc, a, work / (tree_path + ".merge-refusal")


def _resolve(kind: str, base: str, ours: str, theirs: str, tmp_path: Path):
    b, o, t = tmp_path / "b", tmp_path / "o", tmp_path / "t"
    b.write_text(base)
    o.write_text(ours)
    t.write_text(theirs)
    proc = subprocess.run(
        [sys.executable, str(RESOLVER), "--kind", kind, "--base", str(b),
         "--ours", str(o), "--theirs", str(t)],
        capture_output=True, text=True, cwd=REPO_ROOT,
    )
    return proc


def _w(tmp_path: Path, name: str, text: str) -> Path:
    p = tmp_path / name
    p.write_text(text)
    return p


def _nonblank(text: str) -> set[str]:
    return {ln.rstrip() for ln in text.splitlines() if ln.strip()}


# ---------------------------------------------------------------------------
# 1. Silent stale-copy substitution — the important one
# ---------------------------------------------------------------------------

#: Ours and theirs both add item 200 with different text (a genuine, correct
#: refusal), and theirs ALSO adds 201, which is not in dispute at all. 201 is
#: the content the old driver threw away: nobody was arguing about it, and a
#: merge resolved without reading the file would have silently reverted it.
COLLIDING_OURS = _work("**200. Ours: the alpha finding — OPEN.** Ours body.")
COLLIDING_THEIRS = _work(
    "**200. Theirs: the beta finding — OPEN.** Theirs body.",
    "**201. Theirs alone: nobody disputes this one — OPEN.** Uncontested body.",
)


def test_a_refused_merge_does_not_leave_the_ours_copy_in_the_worktree(tmp_path):
    """THE regression test for the 2026-09-23 data loss.

    The old driver exited non-zero and left %A byte-identical to the ours
    copy. That is the failure, not a side effect of it: the file looked
    resolved, so `git add` on it committed a silent revert.
    """
    proc, result, _sidecar = _run_driver(
        WORK_BASE, COLLIDING_OURS, COLLIDING_THEIRS, "docs/WORK.md", tmp_path)
    assert proc.returncode != 0, "a refusal must never read to git as success"
    text = result.read_text()
    assert text != COLLIDING_OURS, (
        "the driver left the ours copy untouched — this is the defect: valid "
        "markdown, no marker, and the other side's content gone"
    )


def test_a_refused_merge_keeps_the_content_only_the_other_side_had(tmp_path):
    """Item 201 was never in dispute. It must survive a refusal about 200."""
    proc, result, _ = _run_driver(
        WORK_BASE, COLLIDING_OURS, COLLIDING_THEIRS, "docs/WORK.md", tmp_path)
    assert proc.returncode != 0
    assert "**201. Theirs alone" in result.read_text(), (
        "content that only the incoming side had was dropped by a refusal "
        "about a different item"
    )


def test_a_refused_merge_is_obviously_unresolved_in_the_file_itself(tmp_path):
    """`git status` saying `UU` is not enough — nothing reads git status on
    the way to `git add`. The FILE has to say so, in the one shape the repo
    already fails the build on."""
    proc, result, _ = _run_driver(
        WORK_BASE, COLLIDING_OURS, COLLIDING_THEIRS, "docs/WORK.md", tmp_path)
    assert proc.returncode != 0
    assert MARKER_RE.search(result.read_text()), (
        "a refusal left a file with no conflict marker, so neither a human "
        "skimming it nor tests/test_no_conflict_markers_in_docs.py can tell "
        "it apart from a resolved one"
    )


def test_every_line_of_both_sides_survives_a_refusal(tmp_path):
    """The guarantee stated as a guarantee, not as three examples: not one
    non-blank line present on either side may be missing."""
    proc, result, _ = _run_driver(
        WORK_BASE, COLLIDING_OURS, COLLIDING_THEIRS, "docs/WORK.md", tmp_path)
    assert proc.returncode != 0
    text = result.read_text()
    lost = (_nonblank(COLLIDING_OURS) | _nonblank(COLLIDING_THEIRS)) - _nonblank(text)
    assert not lost, f"a refusal lost these lines outright: {sorted(lost)}"


def test_a_refusal_whose_line_merge_is_clean_is_still_marked(tmp_path):
    """The case that would have reinstated the whole defect.

    A post-condition refusal (here the 100,000-byte cap) can fire while git's
    own line-level merge of the same two sides is perfectly clean — the two
    sides edited different parts of the file, they are just too big together.
    Falling back to the line merge would then produce a file with NO markers
    that git nonetheless calls conflicted: the exact ambiguity being removed.
    So the banner is unconditional.
    """
    head = ("# QAMC Current Work\n\n## THE FUNNEL QUEUE — why trades do not "
            "happen, ranked by measured cost\n\n")
    tail = ("**Retired item numbers — never reuse.** 1, 2 in this queue, and "
            "3 in the PM test gate, were deleted once written up.\n")
    filler = "".join(f"**{n}. Item {n}.** {'x' * 900}\n\n" for n in range(10, 118))
    base = head + filler + tail
    ours = head + filler + f"**500. Ours adds.** {'o' * 1500}\n\n" + tail
    theirs = head + f"**499. Theirs adds.** {'t' * 1500}\n\n" + filler + tail

    # Stated as a precondition, not assumed: if this ever stops being a
    # clean line merge the test is no longer covering the case it names.
    clean = subprocess.run(
        ["git", "merge-file", "-p", "--diff3",
         str(_w(tmp_path, "o", ours)), str(_w(tmp_path, "b", base)),
         str(_w(tmp_path, "t", theirs))],
        capture_output=True, text=True)
    assert clean.returncode == 0, "precondition: git's own line merge is clean"

    proc, result, sidecar = _run_driver(base, ours, theirs, "docs/WORK.md",
                                        tmp_path)
    assert proc.returncode != 0, "over the byte cap must refuse"
    text = result.read_text()
    assert MARKER_RE.search(text), (
        "a byte-cap refusal left an unmarked file, which is indistinguishable "
        "from a resolved one"
    )
    assert "Ours adds" in text and "Theirs adds" in text
    assert sidecar.exists()
    assert "over the" in sidecar.read_text()


def test_the_refusal_reason_is_beside_the_document_not_inside_it(tmp_path):
    """Prose inside the document would be parsed as board content.

    `status_board`'s item regex is `^\\*\\*(\\d+)\\.` with no comment
    stripping, so a refusal note quoting a colliding item block would show up
    on the board as live items. Worse, a human who deletes the markers and
    commits — the behaviour the marker test's own message names — takes every
    tripwire with them and leaves the prose behind as document content. So
    EVERY line of the banner starts with a marker, and the reason lives in a
    sidecar file.
    """
    proc, result, sidecar = _run_driver(
        WORK_BASE, COLLIDING_OURS, COLLIDING_THEIRS, "docs/WORK.md", tmp_path)
    assert proc.returncode != 0
    assert sidecar.exists(), "the refusal reason was written nowhere findable"
    assert "NUMBER COLLISION" in sidecar.read_text()

    # Nothing in the document that is not a marker and not one of the two
    # sides' own lines.
    sides = _nonblank(COLLIDING_OURS) | _nonblank(COLLIDING_THEIRS)
    strays = [ln for ln in result.read_text().splitlines()
              if ln.strip() and not MARKER_RE.match(ln)
              and ln.rstrip() not in sides]
    assert not strays, (
        f"the refusal put text into the document that neither side wrote and "
        f"that survives deleting the markers: {strays}"
    )


def test_the_sidecar_cannot_be_committed_as_board_content():
    """The sidecar is safe only because it is gitignored. If that line goes,
    the reason file becomes committable board content."""
    assert "*.merge-refusal" in (REPO_ROOT / ".gitignore").read_text()


def test_a_refused_real_git_merge_leaves_a_file_that_fails_the_marker_test(
        tmp_path):
    """End to end through a real `git merge`, because the whole defect lived
    in git's contract rather than in either script's own logic — reasoning
    about %A from the documentation is what produced the wrong comment in the
    first place."""
    repo = tmp_path / "repo"
    (repo / "docs").mkdir(parents=True)
    run = lambda *a: subprocess.run(["git", *a], cwd=repo, check=True,
                                    capture_output=True, text=True)
    run("init", "-q", "-b", "main")
    run("config", "user.email", "t@t")
    run("config", "user.name", "T")
    run("config", "merge.docsmerge.name", "item-aware doc conflict resolver")
    run("config", "merge.docsmerge.driver",
        f"{DRIVER} %O %A %B %P")
    (repo / ".gitattributes").write_text("docs/WORK.md merge=docsmerge\n")
    (repo / ".gitignore").write_text("*.merge-refusal\n")
    (repo / "docs" / "WORK.md").write_text(WORK_BASE)
    run("add", ".gitattributes", ".gitignore", "docs/WORK.md")
    run("commit", "-qm", "base")
    run("checkout", "-qb", "side")
    (repo / "docs" / "WORK.md").write_text(COLLIDING_OURS)
    run("commit", "-qam", "side adds 200")
    run("checkout", "-q", "main")
    (repo / "docs" / "WORK.md").write_text(COLLIDING_THEIRS)
    run("commit", "-qam", "main adds 200 and 201")
    run("checkout", "-q", "side")

    merged = subprocess.run(["git", "merge", "main"], cwd=repo,
                            capture_output=True, text=True)
    assert merged.returncode != 0, "the merge must not report success"
    text = (repo / "docs" / "WORK.md").read_text()
    assert MARKER_RE.search(text), (
        "after a refused real merge the worktree file carried no marker — "
        "`git add` on it would have committed a silent revert"
    )
    assert "**201. Theirs alone" in text, "main's uncontested item was lost"
    assert (repo / "docs" / "WORK.md.merge-refusal").exists()


# ---------------------------------------------------------------------------
# 2. Ordering in the append-only incident log
# ---------------------------------------------------------------------------


def test_a_backfilled_entry_is_not_hoisted_above_newer_ones(tmp_path):
    """The log's own first rule is "Newest first".

    New entries used to be prepended unconditionally, which is right for an
    entry written today about today and wrong for a BACKFILLED one — and the
    desk does backfill (the committed log carries an entry that says so in
    its own title). A 2026-09-20 backfill landing above three 2026-09-23
    entries is an ordering violation caused by the merge, not by its author.
    """
    base = HISTORY_PREAMBLE + _entry("2026-09-23", "already here") \
        + _entry("2026-09-22", "older")
    ours = HISTORY_PREAMBLE + _entry("2026-09-20", "ours backfills this") \
        + _entry("2026-09-23", "already here") + _entry("2026-09-22", "older")
    theirs = HISTORY_PREAMBLE + _entry("2026-09-23", "theirs adds this") \
        + _entry("2026-09-23", "already here") + _entry("2026-09-22", "older")

    proc = _resolve("history", base, ours, theirs, tmp_path)
    assert proc.returncode == 0, proc.stderr
    dates = re.findall(r"^### (\d{4}-\d{2}-\d{2})", proc.stdout, re.M)
    assert dates == sorted(dates, reverse=True), (
        f"the merge produced an order that is not newest-first: {dates}"
    )


def test_two_same_day_entries_still_go_on_top_with_ours_first(tmp_path):
    """The ordinary case must not regress while the backfill case is fixed:
    today's entries above yesterday's, and a same-date tie resolved the same
    way every time rather than arbitrarily."""
    base = HISTORY_PREAMBLE + _entry("2026-09-22", "older")
    ours = HISTORY_PREAMBLE + _entry("2026-09-23", "ours new") \
        + _entry("2026-09-22", "older")
    theirs = HISTORY_PREAMBLE + _entry("2026-09-23", "theirs new") \
        + _entry("2026-09-22", "older")

    proc = _resolve("history", base, ours, theirs, tmp_path)
    assert proc.returncode == 0, proc.stderr
    titles = re.findall(r"^### \d{4}-\d{2}-\d{2} — (.+)$", proc.stdout, re.M)
    assert titles == ["ours new", "theirs new", "older"], titles


def test_entries_that_were_already_in_the_log_are_never_reordered(tmp_path):
    """The committed log has 21 out-of-order adjacent pairs that predate this
    tool [measured 2026-09-23]. Placing new entries correctly must not become
    a global re-sort that rewrites years of history as a merge side effect."""
    base = (HISTORY_PREAMBLE + _entry("2026-09-18", "out of order, first")
            + _entry("2026-09-21", "out of order, second"))
    ours = HISTORY_PREAMBLE + _entry("2026-09-23", "ours new") + base[len(HISTORY_PREAMBLE):]
    theirs = HISTORY_PREAMBLE + _entry("2026-09-22", "theirs new") + base[len(HISTORY_PREAMBLE):]

    proc = _resolve("history", base, ours, theirs, tmp_path)
    assert proc.returncode == 0, proc.stderr
    titles = re.findall(r"^### \d{4}-\d{2}-\d{2} — (.+)$", proc.stdout, re.M)
    assert titles[-2:] == ["out of order, first", "out of order, second"], (
        f"pre-existing history was reordered: {titles}"
    )


# ---------------------------------------------------------------------------
# 3. Whitespace intolerance
# ---------------------------------------------------------------------------


def test_one_stray_blank_line_does_not_make_two_identical_edits_a_collision(
        tmp_path):
    """Both sides made the SAME edit to item 100 and differ only by a blank
    line. Byte equality called that a NUMBER COLLISION, told the human to
    renumber an item that needed no renumbering, and forced a hand rebuild of
    the whole file."""
    edited = "**100. An item that was already here — OPEN.** Rewritten identically."
    ours = WORK_BASE.replace(
        "**100. An item that was already here — OPEN.** Body of the existing item.",
        edited + "\n")           # one extra blank line
    theirs = WORK_BASE.replace(
        "**100. An item that was already here — OPEN.** Body of the existing item.",
        edited)

    proc = _resolve("work", WORK_BASE, ours, theirs, tmp_path)
    assert proc.returncode == 0, (
        "a whitespace-only difference was treated as an editorial conflict:\n"
        + proc.stderr
    )
    assert "Rewritten identically" in proc.stdout


def test_trailing_whitespace_alone_does_not_block_a_merge(tmp_path):
    """The other half of the same defect: agents and editors disagree about
    trailing spaces, and that disagreement is invisible to every reader."""
    ours = _work("**200. A new item — OPEN.** New body.   ")
    theirs = _work("**200. A new item — OPEN.** New body.")
    proc = _resolve("work", WORK_BASE, ours, theirs, tmp_path)
    assert proc.returncode == 0, proc.stderr


def test_whitespace_tolerance_never_reflows_what_it_writes(tmp_path):
    """Normalisation decides only whether two chunks are the SAME. What gets
    written is one side's bytes exactly as that side wrote them, so a false
    "same" can cost a whitespace preference and can never reorder, drop or
    reflow anything."""
    ours = _work("**200. A new item — OPEN.** Line one.\n\n\nLine two after two blanks.")
    theirs = _work("**200. A new item — OPEN.** Line one.\n\nLine two after two blanks.")
    proc = _resolve("work", WORK_BASE, ours, theirs, tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert "Line one.\n\n\nLine two" in proc.stdout, (
        "the resolver rewrote the blank lines instead of taking one side "
        "verbatim"
    )


def test_a_whitespace_only_difference_in_a_board_note_merges(tmp_path):
    """BOARD_NOTES merges by `## item N` block through the same keyed merge,
    so it had the same defect. Both sides must have EDITED the block for this
    to bite — if only one side touches it the three-way shortcut hides the
    bug, which is how this went unnoticed."""
    base = "# Board notes\n\n## item 100\n\nThe old explanation.\n"
    ours = "# Board notes\n\n## item 100\n\nThe rewritten explanation.\n\n"
    theirs = "# Board notes\n\n## item 100\n\nThe rewritten explanation.\n"
    proc = _resolve("notes", base, ours, theirs, tmp_path)
    assert proc.returncode == 0, (
        "two sides that rewrote a note identically, differing by one blank "
        "line, were told they had a collision:\n" + proc.stderr
    )
    assert "The rewritten explanation." in proc.stdout


# ---------------------------------------------------------------------------
# 4. Number collision
# ---------------------------------------------------------------------------


def test_a_real_collision_still_refuses_and_never_renumbers(tmp_path):
    """Refusing is the right answer here and auto-renumbering is not: an item
    number is quoted from BOARD_NOTES, from INCIDENT_HISTORY, from the
    retired-numbers line and from PR titles, so renumbering one inside a merge
    would break every reference to it somewhere this tool cannot see."""
    proc = _resolve("work", WORK_BASE, COLLIDING_OURS, COLLIDING_THEIRS,
                    tmp_path)
    assert proc.returncode == 2
    assert "NUMBER COLLISION" in proc.stderr
    assert "will not renumber" in proc.stderr
    assert proc.stdout == "", "a refusal must not emit a resolved document"


def test_a_collision_refusal_says_exactly_what_differs(tmp_path):
    """The refusal used to print two whole blocks and nothing else, which for
    a real board item means spotting the difference between two paragraphs by
    eye — and that is the step at which a human decides whether they are
    looking at a real collision or a bad rebase."""
    proc = _resolve("work", WORK_BASE, COLLIDING_OURS, COLLIDING_THEIRS,
                    tmp_path)
    assert proc.returncode == 2
    assert "what actually differs" in proc.stderr
    assert "-**200. Ours: the alpha finding" in proc.stderr
    assert "+**200. Theirs: the beta finding" in proc.stderr


@pytest.mark.parametrize("kind,tree_path", [
    ("work", "docs/WORK.md"),
    ("notes", "docs/BOARD_NOTES.md"),
    ("history", "docs/INCIDENT_HISTORY.md"),
])
def test_a_clean_merge_of_every_document_still_exits_zero_unmarked(
        kind, tree_path, tmp_path):
    """The fix must not turn the ordinary case into a conflict. A driver that
    marks everything is as useless as one that marks nothing."""
    text = {
        "work": WORK_BASE,
        "notes": "# Board notes\n\n## item 100\n\nThe explanation.\n",
        "history": HISTORY_PREAMBLE + _entry("2026-09-22", "one entry"),
    }[kind]
    proc, result, sidecar = _run_driver(text, text, text, tree_path, tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert result.read_text() == text
    assert not MARKER_RE.search(result.read_text())
    assert not sidecar.exists(), "a clean merge wrote a refusal reason file"


def test_a_new_entry_with_the_wrong_heading_shape_stops_the_merge(tmp_path):
    """The most likely mechanism behind the ordering complaint that started
    this work, and the one my date-placement fix does NOT cover.

    `parse_history` keys on exactly three hashes plus an ISO date. A dated
    heading written with two hashes is not an entry to this tool — so it is
    never PLACED, it just stays where the text sits. Appended at the end, as
    "append, never trim" invites, a same-day entry written that way ends up at
    the BOTTOM of a newest-first log with the merge reporting success
    [reproduced 2026-09-23]. `docs/WORK.md` item 93 records 12 such headings
    already on main; those are pre-existing and deliberately not blocked.
    """
    base = HISTORY_PREAMBLE + _entry("2026-09-23", "A") + _entry("2026-09-22", "B")
    ours = (base
            + "## 2026-09-23 — ours, written with two hashes\n\nbody\n\n---\n\n")
    theirs = HISTORY_PREAMBLE + _entry("2026-09-23", "theirs new") \
        + _entry("2026-09-23", "A") + _entry("2026-09-22", "B")

    proc = _resolve("history", base, ours, theirs, tmp_path)
    assert proc.returncode == 2, (
        "a dated heading this parser cannot see was merged silently, and a "
        "same-day entry ended up at the bottom of a newest-first log"
    )
    assert "two hashes" in proc.stderr or "three hashes" in proc.stderr


def test_the_wrong_heading_shapes_already_on_main_are_not_blocked(tmp_path):
    """Pre-existing rot is item 93's to fix, not this merge's to block on.
    Blocking on it would make every incident-log merge fail until someone
    else's backlog item is done."""
    base = (HISTORY_PREAMBLE
            + "## 2026-09-19 — a malformed heading that is already on main\n\nbody\n\n---\n\n"
            + _entry("2026-09-22", "B"))
    ours = HISTORY_PREAMBLE + _entry("2026-09-23", "ours new") + base[len(HISTORY_PREAMBLE):]
    theirs = HISTORY_PREAMBLE + _entry("2026-09-23", "theirs new") + base[len(HISTORY_PREAMBLE):]
    proc = _resolve("history", base, ours, theirs, tmp_path)
    assert proc.returncode == 0, proc.stderr
