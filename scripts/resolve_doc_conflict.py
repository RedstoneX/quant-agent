#!/usr/bin/env python3
"""Item-aware three-way resolver for the desk's board documents.

**Why this exists** (`docs/WORK.md` item 68). Every branch that closes a board
item edits the same three documents, so every parallel branch collides there.
The resolver that was in use lived in a session scratchpad and applied one
fixed rule to `docs/WORK.md` and `docs/BOARD_NOTES.md`: take the UNION OF THE
DELETIONS. That rule is right when each side deleted a DIFFERENT item and
wrong in every other case, and it cannot tell the two apart. It destroyed live
board items three times in one day, and every one of those failures left valid
markdown with no conflict marker in it, so
`tests/test_no_conflict_markers_in_docs.py` passed and nothing caught it. A
vanished item is indistinguishable from a finished one, which quietly reverses
the rule that a question survives until it is ANSWERED.

**What this does differently.** It merges ITEMS, not hunks:

  * both sides are parsed into numbered items, per numbering scheme — the
    section heading an item sits under IS its scheme, because `docs/WORK.md`
    carries several independently numbered lists and the same number
    legitimately appears once in each of them;
  * an item is dropped only when a side DELIBERATELY deleted it, which is
    knowable only from the merge base: present in base and absent on that side
    is a closure, absent from base and present on one side is an addition;
  * the same number carrying DIFFERENT text on the two sides is a renumber,
    never a delete, so the tool STOPS and prints both texts;
  * every post-condition is asserted against the merged text before anything
    is written, and a failed assertion REFUSES to write. Writing a
    plausible-looking file is the whole defect.

Stopping for a human is a correct outcome, not a failure.

**Reuse, not a fourth parser.** The per-scheme item numbers of the merged file
are read back with `scripts/status_board.py`'s own `load_funnel_queue` and
`load_pm_gate`, so this tool cannot silently disagree with the board that
renders the same file.

Usage::

    # during a real merge, straight off the index
    scripts/resolve_doc_conflict.py --from-index

    # explicit three-way, for tests and dry runs
    scripts/resolve_doc_conflict.py --kind work \
        --base BASE --ours OURS --theirs THEIRS --out MERGED
"""
from __future__ import annotations

import argparse
import datetime as dt
import importlib.util
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

#: The same tripwire `tests/test_status_board.py` enforces on the committed
#: file. Asserted here as well so a merge cannot be the thing that breaches it:
#: main went over this cap twice in one night that way. Not a new number — it
#: is that test's number, named once so the two cannot drift.
WORK_MD_BYTE_CAP = 100_000

#: The three documents every board-item branch edits, and the one the desk
#: treats as append-only.
KIND_BY_PATH = {
    "docs/WORK.md": "work",
    "docs/BOARD_NOTES.md": "notes",
    "docs/INCIDENT_HISTORY.md": "history",
}

#: Git writes these only at the start of a line.
CONFLICT_MARKERS = ("<<<<<<< ", "||||||| ", ">>>>>>> ")


class Refusal(Exception):
    """The merge cannot be completed safely and no file may be written.

    Every raise carries a plain-English reason and, where two versions of the
    same thing exist, both of them — so the human resolving it can see which
    is which instead of being told only that something went wrong.
    """


# ---------------------------------------------------------------------------
# status_board reuse
# ---------------------------------------------------------------------------

_STATUS_BOARD = None


def status_board():
    """`scripts/status_board.py`, loaded by path (it is a script, not a
    package module — the test suite loads it the same way)."""
    global _STATUS_BOARD
    if _STATUS_BOARD is None:
        path = Path(__file__).resolve().parent / "status_board.py"
        spec = importlib.util.spec_from_file_location("status_board", path)
        mod = importlib.util.module_from_spec(spec)
        # Registered before exec: `status_board` uses dataclasses, which look
        # their own module up in sys.modules while the class body runs.
        sys.modules.setdefault("status_board", mod)
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
        _STATUS_BOARD = mod
    return _STATUS_BOARD


# ---------------------------------------------------------------------------
# Document shapes
# ---------------------------------------------------------------------------

_HEADING_RE = re.compile(r"^#{1,6}\s")
#: The item-opening shape, deliberately the same one `status_board` reads with
#: (`_ITEM_OPEN_RE`): bold, optional strikethrough, number, dot.
_ITEM_OPEN_RE = re.compile(r"^\*\*(?:~~)?(\d+)\.\s*(.*)$")
_RETIRED_LINE_RE = re.compile(r"^\*\*Retired item numbers")

#: The retired-numbers line, parsed from ITS OWN two number lists rather than
#: by scraping integers out of the surrounding sentences. Scraping is not a
#: hypothetical failure: it pulled stray digits out of neighbouring prose and
#: corrupted the line badly enough that a separate session had to reconstruct
#: it from git history.
_RETIRED_STRUCT_RE = re.compile(
    r"^(?P<pre>.*?)"
    r"(?P<queue>\d[\d,\s]*\d|\d)"
    r"(?P<mid>\s+in this queue, and\s+)"
    r"(?P<gate>\d[\d,\s]*\d|\d)"
    r"(?P<post>\s+in the PM test gate\b.*)$",
    re.S,
)

_GATE_HEADING_PREFIX = "## PM TEST GATE"
_QUEUE_HEADING_PREFIX = "## THE FUNNEL QUEUE"

#: `## item 12`, `## gate item 4`, `## decision due 2026-09-16` — the keys
#: `status_board.load_board_notes` reads, so a note this tool keeps is a note
#: the board can still find.
_NOTE_HEADING_RE = re.compile(
    r"^#{1,6}\s+(item\s+\d+|gate\s+item\s+\d+|decision\s+due\s+\d{4}-\d{2}-\d{2})\s*$",
    re.I,
)

_ENTRY_HEADING_RE = re.compile(r"^###\s+(\d{4}-\d{2}-\d{2})\b")


@dataclass
class Section:
    """One heading and everything under it, split into the parts that merge by
    different rules: prose before the numbered items, the items themselves,
    and prose after them (which is where the retired-numbers line lives)."""

    heading: str  # "" for the text above the first heading
    pre: str = ""
    items: dict[int, str] = field(default_factory=dict)
    order: list[int] = field(default_factory=list)
    post: str = ""
    retired: str | None = None  # the retired line, lifted out of `post`

    @property
    def key(self) -> str:
        return self.heading.strip()


def _scheme_label(heading: str) -> str:
    """The owner-facing name of the numbering scheme a heading opens."""
    h = heading.strip()
    if h.startswith(_GATE_HEADING_PREFIX):
        return "the PM test gate"
    if h.startswith(_QUEUE_HEADING_PREFIX):
        return "the funnel queue"
    return h.lstrip("# ").strip() or "the top of the file"


def parse_sections(text: str) -> list[Section]:
    """Split a WORK.md-shaped document into sections.

    A section runs from one markdown heading to the next. Inside it, a line
    matching the item-open shape starts an item block that runs to the next
    item or the next heading. The heading an item sits under IS its numbering
    scheme: that is what lets 4 and 8 exist in two lists at once without this
    tool ever confusing one for the other.
    """
    lines = text.splitlines(keepends=True)
    sections: list[Section] = [Section(heading="")]
    cur = sections[0]
    state = "pre"  # pre -> items -> post
    buf: list[str] = []
    item_num: int | None = None

    def flush_item() -> None:
        nonlocal buf, item_num
        if item_num is not None:
            if item_num in cur.items:
                raise Refusal(
                    f"Item {item_num} appears twice under {cur.key or 'the top of the file'!r} "
                    "in one of the versions being merged. A number means one item; "
                    "two items under one number is a renumber a human has to make."
                )
            cur.items[item_num] = "".join(buf)
            cur.order.append(item_num)
        buf = []
        item_num = None

    def flush_text() -> None:
        nonlocal buf
        chunk = "".join(buf)
        if state == "pre":
            cur.pre += chunk
        else:
            cur.post += chunk
        buf = []

    for line in lines:
        if _HEADING_RE.match(line):
            if item_num is not None:
                flush_item()
            else:
                flush_text()
            cur = Section(heading=line)
            sections.append(cur)
            state = "pre"
            continue
        m = _ITEM_OPEN_RE.match(line)
        if m:
            if item_num is not None:
                flush_item()
            else:
                flush_text()
            state = "items"
            item_num = int(m.group(1))
            buf = [line]
            continue
        if _RETIRED_LINE_RE.match(line):
            # A hard boundary. Everything else after the last item is that
            # item's own body (which is how `status_board` reads it too), but
            # the retired-numbers line belongs to the section, not to whichever
            # item happens to be last — otherwise closing that item would take
            # the whole retired list with it.
            if item_num is not None:
                flush_item()
            else:
                flush_text()
            state = "post"
        buf.append(line)
    if item_num is not None:
        flush_item()
    else:
        flush_text()

    for s in sections:
        s.retired, s.post = _lift_retired(s.post)
    return sections


def _lift_retired(post: str) -> tuple[str | None, str]:
    """Pull the retired-numbers line out of a section's trailing prose so it
    merges by its own rule (union of the two parsed lists) instead of as
    ordinary text."""
    out: list[str] = []
    retired: str | None = None
    for line in post.splitlines(keepends=True):
        if _RETIRED_LINE_RE.match(line):
            if retired is not None:
                raise Refusal(
                    "Two retired-item-numbers lines in one section. There is "
                    "exactly one such line; a human has to say which is real."
                )
            retired = line
            out.append("\x00RETIRED\x00\n")
            continue
        out.append(line)
    return retired, "".join(out)


def render_sections(sections: list[Section]) -> str:
    parts: list[str] = []
    for s in sections:
        parts.append(s.heading)
        parts.append(s.pre)
        for n in s.order:
            parts.append(s.items[n])
        post = s.post
        if s.retired is not None:
            post = post.replace("\x00RETIRED\x00\n", s.retired)
        parts.append(post)
    return "".join(parts)


# ---------------------------------------------------------------------------
# The three-way primitives
# ---------------------------------------------------------------------------


def merge_text(base: str, ours: str, theirs: str, what: str) -> str:
    """Three-way merge of one prose chunk.

    Unchanged on a side means that side has no opinion. Both sides changing it
    to different things is a genuine editorial conflict and stops for a human —
    after one attempt at git's own line-level merge, which resolves the common
    case of two edits to different paragraphs.
    """
    if ours == theirs:
        return ours
    if ours == base:
        return theirs
    if theirs == base:
        return ours
    merged, clean = _git_merge_file(base, ours, theirs)
    if clean:
        return merged
    raise Refusal(
        f"{what} was edited differently on both sides and the two edits "
        "overlap, so no merge of them is safe.\n"
        f"--- ours ---\n{ours}\n--- theirs ---\n{theirs}\n"
        "Resolve this chunk by hand."
    )


def _git_merge_file(base: str, ours: str, theirs: str) -> tuple[str, bool]:
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        (d / "base").write_text(base)
        (d / "ours").write_text(ours)
        (d / "theirs").write_text(theirs)
        proc = subprocess.run(
            ["git", "merge-file", "-p", "--diff3",
             str(d / "ours"), str(d / "base"), str(d / "theirs")],
            capture_output=True, text=True,
        )
        return proc.stdout, proc.returncode == 0


def merge_keyed(base: dict, ours: dict, theirs: dict,
                base_order: list, ours_order: list, theirs_order: list,
                what: str, append_only: bool = False):
    """Merge two versions of a keyed, ordered collection — board items, board
    notes, incident entries — by KEY, using the merge base to tell a deliberate
    deletion from an addition on the other side.

    Returns ``(merged, order)``.
    """
    keys = set(base) | set(ours) | set(theirs)
    merged: dict = {}
    for k in keys:
        in_b, in_o, in_t = k in base, k in ours, k in theirs
        if append_only and in_b and not (in_o and in_t):
            raise Refusal(
                f"{what} {k!r} is in the merge base but was deleted on one "
                "side. This document is append-only: entries are never removed."
            )
        if in_o and in_t:
            if ours[k] == theirs[k]:
                merged[k] = ours[k]
            elif in_b and ours[k] == base[k]:
                merged[k] = theirs[k]
            elif in_b and theirs[k] == base[k]:
                merged[k] = ours[k]
            else:
                raise Refusal(
                    f"NUMBER COLLISION on {what} {k!r}: the two sides carry "
                    "DIFFERENT text under the same identifier. That is a "
                    "renumber, never a delete, and this tool will not pick "
                    "one silently.\n"
                    f"--- ours ---\n{ours[k]}\n--- theirs ---\n{theirs[k]}\n"
                    "Renumber one of them by hand (see the retired-numbers "
                    "line for the next free number) and re-run the merge."
                )
        elif in_o and not in_t:
            # Absent on theirs. A deletion only if theirs HAD it.
            if in_b:
                continue  # theirs closed it deliberately
            merged[k] = ours[k]
        elif in_t and not in_o:
            if in_b:
                continue  # ours closed it deliberately
            merged[k] = theirs[k]
        # in neither side: both closed it. Drop.
    order = _merge_order(base_order, ours_order, theirs_order, set(merged))
    return merged, order


def _merge_order(base_order, ours_order, theirs_order, keep: set) -> list:
    """Order the survivors: the base's order for everything that was already
    there, then each side's new entries anchored after the entry they follow on
    that side. Deterministic, and it never reorders the queue, which the
    backlog is explicit about being read in order."""
    result = [k for k in base_order if k in keep]
    inserted: set = set()
    for side in (ours_order, theirs_order):
        seen_before: list = []
        for k in side:
            if k in keep and k not in result:
                anchor = None
                for prev in reversed(seen_before):
                    if prev in result:
                        anchor = prev
                        break
                idx = 0 if anchor is None else result.index(anchor) + 1
                # Step over entries already placed at this same anchor, so
                # theirs land AFTER ours instead of pushing ours down.
                while idx < len(result) and result[idx] in inserted:
                    idx += 1
                result.insert(idx, k)
                inserted.add(k)
            seen_before.append(k)
    return result


# ---------------------------------------------------------------------------
# The retired-item-numbers line
# ---------------------------------------------------------------------------


def parse_retired(line: str) -> tuple[list[int], list[int], str, str, str]:
    m = _RETIRED_STRUCT_RE.match(line)
    if not m:
        raise Refusal(
            "The retired-item-numbers line no longer has the shape this tool "
            "reads (`<numbers> in this queue, and <numbers> in the PM test "
            "gate`), so its two lists cannot be parsed from their own source "
            "text. Refusing rather than scraping integers out of the prose — "
            "that is what corrupted the line last time.\n"
            f"--- line ---\n{line}"
        )
    nums = lambda s: [int(x) for x in re.findall(r"\d+", s)]
    return (nums(m.group("queue")), nums(m.group("gate")),
            m.group("pre"), m.group("mid"), m.group("post"))


def merge_retired(base: str | None, ours: str | None, theirs: str | None) -> str | None:
    """Rebuild the retired-numbers line as the UNION of both sides' lists,
    parsed from the two source lists — never regex-scraped out of the prose
    around them."""
    if ours is None and theirs is None:
        return None
    if ours is None:
        return theirs
    if theirs is None:
        return ours
    if ours == theirs:
        return ours
    o_q, o_g, o_pre, o_mid, o_post = parse_retired(ours)
    t_q, t_g, t_pre, t_mid, t_post = parse_retired(theirs)
    b_q, b_g, b_pre, b_mid, b_post = ([], [], o_pre, o_mid, o_post)
    if base is not None:
        b_q, b_g, b_pre, b_mid, b_post = parse_retired(base)

    queue = sorted(set(o_q) | set(t_q))
    gate = sorted(set(o_g) | set(t_g))
    pre = merge_text(b_pre, o_pre, t_pre, "the retired-numbers line's opening")
    mid = merge_text(b_mid, o_mid, t_mid, "the retired-numbers line's middle")
    post = merge_text(b_post, o_post, t_post, "the retired-numbers line's tail")
    return (f"{pre}{', '.join(str(n) for n in queue)}{mid}"
            f"{', '.join(str(n) for n in gate)}{post}")


# ---------------------------------------------------------------------------
# docs/WORK.md
# ---------------------------------------------------------------------------


def _by_key(sections: list[Section]) -> dict[str, Section]:
    out: dict[str, Section] = {}
    for s in sections:
        if s.key in out:
            raise Refusal(
                f"Two sections headed {s.key!r} in one of the versions being "
                "merged. This tool identifies a numbering scheme by its "
                "heading, so a duplicate heading makes the schemes ambiguous."
            )
        out[s.key] = s
    return out


def resolve_work(base: str, ours: str, theirs: str) -> str:
    b, o, t = (parse_sections(x) for x in (base, ours, theirs))
    bk, ok, tk = _by_key(b), _by_key(o), _by_key(t)

    if [s.key for s in o] != [s.key for s in t]:
        only_o = [k for k in ok if k not in tk]
        only_t = [k for k in tk if k not in ok]
        raise Refusal(
            "The two sides no longer agree on this document's section "
            "headings, so which numbering scheme an item belongs to is "
            f"ambiguous.\n  only on ours: {only_o}\n  only on theirs: {only_t}\n"
            "Reconcile the headings by hand first."
        )

    out: list[Section] = []
    expected: dict[str, set[int]] = {}
    for s_o in o:
        s_t = tk[s_o.key]
        s_b = bk.get(s_o.key, Section(heading=s_o.heading))
        merged_items, order = merge_keyed(
            s_b.items, s_o.items, s_t.items,
            s_b.order, s_o.order, s_t.order,
            what=f"item under {_scheme_label(s_o.heading)!r}",
        )
        sec = Section(
            heading=s_o.heading,
            pre=merge_text(s_b.pre, s_o.pre, s_t.pre,
                           f"the prose under {_scheme_label(s_o.heading)!r}"),
            items=merged_items,
            order=order,
            post=merge_text(s_b.post, s_o.post, s_t.post,
                            f"the prose after the items under "
                            f"{_scheme_label(s_o.heading)!r}"),
            retired=merge_retired(s_b.retired, s_o.retired, s_t.retired),
        )
        if sec.retired is not None and "\x00RETIRED\x00" not in sec.post:
            raise Refusal(
                "The retired-item-numbers line survived the merge but its "
                "place in the surrounding prose did not. Refusing rather than "
                "guessing where to put it back."
            )
        out.append(sec)
        expected[s_o.key] = set(merged_items)

    text = render_sections(out)
    _assert_work_postconditions(text, base, ours, theirs, expected)
    return text


def _expected_survivors(base: str, ours: str, theirs: str) -> dict[str, set[int]]:
    b, o, t = (_by_key(parse_sections(x)) for x in (base, ours, theirs))
    out: dict[str, set[int]] = {}
    for key in set(o) | set(t):
        bs = b.get(key, Section(heading=key)).items
        os_ = o.get(key, Section(heading=key)).items
        ts = t.get(key, Section(heading=key)).items
        keep = set()
        for n in set(os_) | set(ts):
            in_b, in_o, in_t = n in bs, n in os_, n in ts
            if (in_o and in_t) or (in_o and not in_b) or (in_t and not in_b):
                keep.add(n)
        out[key] = keep
    return out


def _assert_work_postconditions(text: str, base: str, ours: str, theirs: str,
                                expected: dict[str, set[int]]) -> None:
    """Everything that must be true of the merged `docs/WORK.md`. A failure
    here refuses the write. This is the half the old resolver did not have:
    it wrote a plausible file and nothing ever looked at it again."""
    _assert_no_conflict_markers(text, "docs/WORK.md")

    want = _expected_survivors(base, ours, theirs)
    if want != expected:
        raise Refusal(
            "The merged item set is not the set the two sides imply.\n"
            f"  expected: { {k: sorted(v) for k, v in want.items()} }\n"
            f"  produced: { {k: sorted(v) for k, v in expected.items()} }"
        )

    # Post-condition 1 and 2: every expected number present EXACTLY ONCE in
    # its own scheme, read back out of the rendered text.
    got = _by_key(parse_sections(text))
    for key, nums in want.items():
        sec = got.get(key)
        present = list(sec.order) if sec else []
        if sorted(present) != sorted(nums):
            missing = sorted(nums - set(present))
            extra = sorted(set(present) - nums)
            raise Refusal(
                f"Under {_scheme_label(key)!r} the merged file does not carry "
                "the items it must.\n"
                f"  vanished: {missing}\n  unexpected: {extra}\n"
                "An item that vanishes here looks exactly like an item that "
                "was answered. Refusing to write."
            )
        if len(present) != len(set(present)):
            dupes = sorted({n for n in present if present.count(n) > 1})
            raise Refusal(
                f"Under {_scheme_label(key)!r} these numbers appear more than "
                f"once: {dupes}. One number is one item."
            )

    # Post-condition 3: the repo's OWN parsers must read the same numbers, so
    # this tool cannot quietly disagree with the board that renders the file.
    _assert_status_board_agrees(text, want)

    # Post-condition 4: a number cannot be live and retired in the same scheme.
    _assert_retired_disjoint(text, want)

    # Post-condition 5: the byte cap main breached twice in one night.
    size = len(text.encode())
    if size > WORK_MD_BYTE_CAP:
        raise Refusal(
            f"The merged docs/WORK.md is {size} bytes, over the "
            f"{WORK_MD_BYTE_CAP:,}-byte cap that tests/test_status_board.py "
            "enforces. Move finished content into docs/INCIDENT_HISTORY.md "
            "first — writing this would redden main."
        )


def _assert_status_board_agrees(text: str, want: dict[str, set[int]]) -> None:
    sb = status_board()
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "WORK.md"
        p.write_text(text)
        for loader, prefix in ((sb.load_funnel_queue, _QUEUE_HEADING_PREFIX),
                               (sb.load_pm_gate, _GATE_HEADING_PREFIX)):
            keys = [k for k in want if k.startswith(prefix)]
            if not keys:
                continue
            items, problem = loader(p)
            if problem:
                raise Refusal(
                    "The merged file cannot be read by the board's own "
                    f"parser: {problem}"
                )
            got = sorted(i.rank for i in items)
            if got != sorted(want[keys[0]]):
                raise Refusal(
                    f"The board's own parser reads {got} under "
                    f"{_scheme_label(keys[0])!r}, but the merge produced "
                    f"{sorted(want[keys[0]])}. The two must never disagree."
                )


def _assert_retired_disjoint(text: str, want: dict[str, set[int]]) -> None:
    line = None
    for s in parse_sections(text):
        if s.retired is not None:
            line = s.retired
    if line is None:
        return
    queue, gate, *_ = parse_retired(line)
    live = {
        _QUEUE_HEADING_PREFIX: set(queue),
        _GATE_HEADING_PREFIX: set(gate),
    }
    for prefix, retired in live.items():
        for key, nums in want.items():
            if not key.startswith(prefix):
                continue
            clash = sorted(retired & nums)
            if clash:
                raise Refusal(
                    f"{clash} are listed as retired under "
                    f"{_scheme_label(key)!r} and are also still live items "
                    "there. A number is one or the other, never both."
                )


def _assert_no_conflict_markers(text: str, what: str) -> None:
    bad = [n for n, line in enumerate(text.splitlines(), 1)
           if line.startswith(CONFLICT_MARKERS) or line.rstrip() == "======="]
    if bad:
        raise Refusal(
            f"The merge of {what} left conflict markers at lines {bad}. Two "
            "versions of a fact side by side is worse than untidy."
        )


# ---------------------------------------------------------------------------
# docs/BOARD_NOTES.md
# ---------------------------------------------------------------------------


def parse_notes(text: str) -> tuple[str, dict[str, str], list[str]]:
    """Split BOARD_NOTES into its preamble and one block per `## item N`,
    keyed exactly as `status_board.load_board_notes` keys them."""
    lines = text.splitlines(keepends=True)
    preamble: list[str] = []
    blocks: dict[str, str] = {}
    order: list[str] = []
    key: str | None = None
    buf: list[str] = []

    def flush() -> None:
        nonlocal buf, key
        if key is None:
            preamble.extend(buf)
        else:
            if key in blocks:
                raise Refusal(
                    f"BOARD_NOTES carries two {key!r} blocks in one of the "
                    "versions being merged. One item, one note."
                )
            blocks[key] = "".join(buf)
            order.append(key)
        buf = []

    for line in lines:
        m = _NOTE_HEADING_RE.match(line.strip())
        if m:
            flush()
            key = " ".join(m.group(1).lower().split())
            buf = [line]
            continue
        buf.append(line)
    flush()
    return "".join(preamble), blocks, order


def resolve_notes(base: str, ours: str, theirs: str) -> str:
    b_pre, b_blocks, b_order = parse_notes(base)
    o_pre, o_blocks, o_order = parse_notes(ours)
    t_pre, t_blocks, t_order = parse_notes(theirs)
    pre = merge_text(b_pre, o_pre, t_pre, "the BOARD_NOTES preamble")
    blocks, order = merge_keyed(b_blocks, o_blocks, t_blocks,
                                b_order, o_order, t_order,
                                what="board note")
    text = pre + "".join(blocks[k] for k in order)
    _assert_no_conflict_markers(text, "docs/BOARD_NOTES.md")

    got = parse_notes(text)[1]
    if set(got) != set(blocks):
        raise Refusal(
            "The merged BOARD_NOTES does not carry the notes the merge "
            f"decided on. missing={sorted(set(blocks) - set(got))} "
            f"unexpected={sorted(set(got) - set(blocks))}"
        )
    return text


def assert_notes_agree_with_work(work_text: str, notes_text: str) -> None:
    """Every surviving `## item N` block must still have an item to explain.

    `tests/test_status_board.py::test_no_board_note_is_orphaned_in_the_real_repository`
    fails the build on an orphan, and an orphan is also the only reason two of
    the three destroyed-item incidents were noticed at all.
    """
    sb = status_board()
    with tempfile.TemporaryDirectory() as td:
        w = Path(td) / "WORK.md"
        n = Path(td) / "BOARD_NOTES.md"
        w.write_text(work_text)
        n.write_text(notes_text)
        notes = sb.load_board_notes(n)
        queue, _ = sb.load_funnel_queue(w, notes)
        gate, _ = sb.load_pm_gate(w, notes)
        decisions = sb.load_pending_decisions(w, notes=notes)
        real = {x.ref for x in (*queue, *gate, *decisions)}
    orphans = sorted(k for k in notes
                     if k not in real and not k.lower().startswith("item n"))
    if orphans:
        raise Refusal(
            f"The merge would leave these board notes explaining items that "
            f"no longer exist: {orphans}. Either the item was deleted and its "
            "note was not, or the note is keyed to a number the item no "
            "longer has. Either way the board's own test fails on it."
        )


# ---------------------------------------------------------------------------
# docs/INCIDENT_HISTORY.md
# ---------------------------------------------------------------------------


def parse_history(text: str) -> tuple[str, dict[str, str], list[str]]:
    """Split the incident log into its preamble and one span per dated entry.

    A span runs from its `### <date> — ...` heading to just before the next
    one, so it carries its own trailing `---` separator with it and is put back
    byte-for-byte.
    """
    lines = text.splitlines(keepends=True)
    preamble: list[str] = []
    entries: dict[str, str] = {}
    order: list[str] = []
    key: str | None = None
    buf: list[str] = []

    def flush() -> None:
        nonlocal buf, key
        if key is None:
            preamble.extend(buf)
        else:
            if key in entries:
                raise Refusal(
                    "Two incident entries share the heading "
                    f"{key!r} in one of the versions being merged."
                )
            entries[key] = "".join(buf)
            order.append(key)
        buf = []

    for line in lines:
        if _ENTRY_HEADING_RE.match(line):
            flush()
            key = line.strip()
            buf = [line]
            continue
        buf.append(line)
    flush()
    return "".join(preamble), entries, order


def _entry_date(key: str) -> dt.date:
    m = _ENTRY_HEADING_RE.match(key)
    return dt.date.fromisoformat(m.group(1)) if m else dt.date.min


def resolve_history(base: str, ours: str, theirs: str) -> str:
    b_pre, b_e, b_order = parse_history(base)
    o_pre, o_e, o_order = parse_history(ours)
    t_pre, t_e, t_order = parse_history(theirs)
    pre = merge_text(b_pre, o_pre, t_pre, "the incident log's preamble")
    entries, _order = merge_keyed(b_e, o_e, t_e, b_order, o_order, t_order,
                                  what="incident entry", append_only=True)

    # Newest first, and NOTHING already in the log moves. The committed file
    # is not perfectly date-sorted (fifteen adjacent pairs are out of order as
    # of 2026-09-14), so globally re-sorting it would rewrite years of history
    # as a side effect of one merge. Entries that were already there keep the
    # base's order exactly; only the new ones are placed, at the top, newest
    # first among themselves. Two sides cannot be ranked against each other, so
    # a same-date tie goes to ours — the same way every time, never arbitrarily.
    retained = [k for k in b_order if k in entries]
    new_ours = [k for k in o_order if k in entries and k not in b_e]
    new_theirs = [k for k in t_order if k in entries and k not in b_e
                  and k not in new_ours]
    fresh = sorted(new_ours + new_theirs, key=_entry_date, reverse=True)
    order = fresh + retained

    text = pre + "".join(entries[k] for k in order)
    _assert_no_conflict_markers(text, "docs/INCIDENT_HISTORY.md")

    got_pre, got_entries, got_order = parse_history(text)
    if set(got_entries) != set(entries):
        raise Refusal(
            "The merged incident log lost entries the merge kept: "
            f"{sorted(set(entries) - set(got_entries))}"
        )
    for k, span in got_entries.items():
        if span != entries[k]:
            raise Refusal(f"Incident entry {k!r} was not preserved verbatim.")
    if [k for k in got_order if k in b_e] != retained:
        raise Refusal(
            "The merge reordered entries that were already in the incident "
            "log. Existing history must come out of a merge untouched."
        )
    fresh_dates = [_entry_date(k) for k in got_order if k not in b_e]
    if fresh_dates != sorted(fresh_dates, reverse=True):
        raise Refusal(
            "The entries this merge adds are not newest-first, which is the "
            "one ordering rule the file states about itself."
        )
    for side_entries in (o_e, t_e):
        for k in side_entries:
            if k not in got_entries:
                raise Refusal(
                    f"Incident entry {k!r} was on one side and is not in the "
                    "merged file. This log is append-only."
                )
    return text


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

RESOLVERS = {
    "work": resolve_work,
    "notes": resolve_notes,
    "history": resolve_history,
}


def _stage(path: str, n: int) -> str | None:
    proc = subprocess.run(["git", "show", f":{n}:{path}"],
                          capture_output=True, text=True, cwd=REPO_ROOT)
    return proc.stdout if proc.returncode == 0 else None


def _conflicted_paths() -> list[str]:
    proc = subprocess.run(["git", "diff", "--name-only", "--diff-filter=U"],
                          capture_output=True, text=True, cwd=REPO_ROOT,
                          check=True)
    return [p for p in proc.stdout.split() if p in KIND_BY_PATH]


def _from_index(apply: bool) -> int:
    paths = _conflicted_paths()
    if not paths:
        print("No board document is conflicted.")
        return 0
    merged: dict[str, str] = {}
    for path in paths:
        base, ours, theirs = (_stage(path, 1), _stage(path, 2), _stage(path, 3))
        if ours is None or theirs is None:
            raise Refusal(
                f"{path} is conflicted but one side has no version of it "
                "(added on one side, or deleted). A human has to decide that."
            )
        if base is None:
            raise Refusal(
                f"{path} has no merge base in the index. Without it a deletion "
                "cannot be told from an addition, which is the whole defect "
                "this tool exists to fix."
            )
        merged[path] = RESOLVERS[KIND_BY_PATH[path]](base, ours, theirs)

    work = merged.get("docs/WORK.md")
    notes = merged.get("docs/BOARD_NOTES.md")
    if work is None and notes is not None:
        p = REPO_ROOT / "docs" / "WORK.md"
        work = p.read_text() if p.exists() else None
    if notes is None and work is not None:
        p = REPO_ROOT / "docs" / "BOARD_NOTES.md"
        notes = p.read_text() if p.exists() else None
    if work is not None and notes is not None:
        assert_notes_agree_with_work(work, notes)

    for path, text in merged.items():
        if apply:
            (REPO_ROOT / path).write_text(text)
            subprocess.run(["git", "add", "--", path], cwd=REPO_ROOT, check=True)
        print(f"{'resolved' if apply else 'would resolve'} {path} "
              f"({len(text.encode()):,} bytes)")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--from-index", action="store_true",
                    help="resolve every conflicted board document off the "
                         "git index of an in-progress merge")
    ap.add_argument("--dry-run", action="store_true",
                    help="with --from-index, check but write nothing")
    ap.add_argument("--kind", choices=sorted(RESOLVERS))
    ap.add_argument("--base")
    ap.add_argument("--ours")
    ap.add_argument("--theirs")
    ap.add_argument("--out")
    args = ap.parse_args(argv)

    try:
        if args.from_index:
            return _from_index(apply=not args.dry_run)
        if not (args.kind and args.base and args.ours and args.theirs):
            ap.error("--kind, --base, --ours and --theirs are all required "
                     "without --from-index")
        text = RESOLVERS[args.kind](
            Path(args.base).read_text(),
            Path(args.ours).read_text(),
            Path(args.theirs).read_text(),
        )
        if args.out:
            Path(args.out).write_text(text)
            print(f"wrote {args.out} ({len(text.encode()):,} bytes)")
        else:
            sys.stdout.write(text)
        return 0
    except Refusal as exc:
        print("REFUSING TO WRITE — this merge needs a human.\n", file=sys.stderr)
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
