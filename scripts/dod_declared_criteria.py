"""CHECK 1 of the definition-of-done gate — the declared halves.

Lifted out of `scripts/definition_of_done.py`, which carries the reasoning
for the gate as a whole and still exposes `declared_criteria_problems` under
its own name. The split is mechanical: this module owns the three accounting
forms a closure may use for a criterion, and nothing else.
"""
from __future__ import annotations

import re

from scripts.board_locator import ReferenceUnavailable, tree_board
from scripts.definition_of_done import (
    Change, criteria, item_blocks, items_closed, items_filed, trailer,
)


#: `Done-criteria-deferred: 18/2 -> item 63 (2026-09-18)`. A deferred
#: criterion must name where it went and when it was deferred, because an
#: obligation with no date is the silent limbo this check exists to remove.
DEFERRAL = re.compile(
    r"(\d+)\s*/\s*(\d+)\s*(?:->|→)\s*items?\s*#?(\d+)"
    r".*?(\d{4}-\d{2}-\d{2})", re.I)
MET = re.compile(r"(\d+)\s*/\s*(\d+)", re.I)

#: `Done-criteria-withdrawn: 202/9 -> ruling 2026-10-04
#: (docs/board_notes/item-202.md)`. The third accounting, and the only one
#: that ends an obligation without anybody doing the work.
#:
#: WHY IT EXISTS. The two original accountings assume every declared
#: criterion is either delivered or still owed. A third case occurs and had
#: nowhere to go: the owner RULES that the work will not be done at all, so
#: the criterion is neither met nor owed — it is withdrawn. With no form for
#: it, the only ways past the gate were to mark it met (a lie) or to defer it
#: onto a freshly filed item nobody will ever work (fabricated bookkeeping of
#: exactly the kind this gate exists to stop).
#:
#: WHY IT IS NOT A WAIVER. A deferral is ungameable because it must name a
#: board item that EXISTS and is checked against the board. This form is held
#: to the same bar and not a lower one: it must cite a RULING by date and the
#: record file carrying it, that file must be one of the board's own record
#: files, and the gate reads the file and refuses unless a `RULING` line
#: bearing that date is really there. Free text alone withdraws nothing.
WITHDRAWAL = re.compile(
    r"(\d+)\s*/\s*(\d+)\s*(?:->|\u2192)\s*ruling\s*"
    r"(\d{4}-\d{2}-\d{2})\s*\(\s*([^)]+?)\s*\)", re.I)

#: The shape a recorded ruling already has in this repo's decisions record:
#: `**RULING 2026-10-01 — ...**`, `OWNER RULING 2026-10-02 (verbatim): ...`,
#: `2026-09-30 OWNER RULING APPLIED: ...`. Case-SENSITIVE on `RULING` on
#: purpose, so ordinary prose using the word in lower case is not a ruling.
def _ruling_line(text: str, date: str) -> bool:
    return any("RULING" in line and date in line
               for line in text.splitlines())


def _ruling_record_problem(change: Change, date: str, cited: str) -> str | None:
    """Why this withdrawal's cited ruling does not check out, or None.

    Three ways to fail, in the order a reader would ask them: the citation
    is not one of the board's own record files; the file is not in the tree
    after this change; the file carries no RULING bearing that date.
    """
    try:
        board, notes = tree_board(change.tree)
    except ReferenceUnavailable:
        return None  # same silence the rest of this module keeps when the
                     # board cannot be located: not evidence of a dropped half
    cited = cited.strip().lstrip("./")
    if cited != board and not (cited.startswith(notes + "/")
                               and cited.endswith(".md")):
        return (f"cites {cited!r}, which is not part of the decisions record "
                f"(the board file {board!r} or a note under {notes}/). A "
                f"ruling the board does not carry is not a ruling this gate "
                f"can check")
    path = change.tree / cited
    if ".." in cited.split("/") or not path.is_file():
        return (f"cites {cited!r}, which does not exist in the tree after "
                f"this change")
    if not _ruling_line(path.read_text(encoding="utf-8", errors="replace"),
                        date):
        return (f"cites a ruling dated {date} in {cited!r}, and that file "
                f"carries no RULING line bearing that date. Record the "
                f"ruling there first — a withdrawal is only as good as the "
                f"ruling behind it")
    return None


def declared_criteria_problems(change: Change) -> list[str]:
    """Items filed without criteria, and closures that drop one.

    Filing: a new item must carry a `DONE WHEN:` label and at least one
    criterion bullet. An item whose whole content is a question for the
    owner is exempt via `NO CRITERIA:` and a reason, because "he rules or he
    does not" has no half to leave behind — and the exemption is itself in
    the diff, which is the point.

    Closing: every criterion the item carried AT THE BASE COMMIT must appear
    in this change's `Done-criteria-met`, `Done-criteria-deferred` or
    `Done-criteria-withdrawn` trailers. A deferral must name a target item
    that exists in the board after this change and a date. A withdrawal must
    cite an owner RULING, by date, in a board record file that really carries
    it — see `WITHDRAWAL` for why a third form was needed and why it is not a
    waiver. An item that carried no criteria at the
    base is not held to this — that is the grandfathering, and it is why
    this check has almost no bite on today's board.
    """
    problems: list[str] = []
    after = item_blocks(change.work_md_after)
    before = item_blocks(change.work_md_before)

    for number in sorted(items_filed(change), key=int):
        block = after[number]
        if re.search(r"^\s*NO CRITERIA:\s*\S.{15,}", block, re.M):
            continue
        if not criteria(block):
            problems.append(
                f"board item {number} is filed by this change with no "
                f"completion criteria. Add a `DONE WHEN:` line to its block "
                f"in the board file followed by one `- [ ] ...` bullet per half "
                f"of the work, so closing it later has something to verify "
                f"against. If the item is a question only the owner can "
                f"answer, say so with a `NO CRITERIA: <reason>` line instead."
            )

    met = {(m.group(1), m.group(2)) for v in trailer(change.messages, "Done-criteria-met")
           for m in MET.finditer(v)}
    deferred = {(m.group(1), m.group(2)): (m.group(3), m.group(4))
                for v in trailer(change.messages, "Done-criteria-deferred")
                for m in DEFERRAL.finditer(v)}
    withdrawn = {(m.group(1), m.group(2)): (m.group(3), m.group(4))
                 for v in trailer(change.messages, "Done-criteria-withdrawn")
                 for m in WITHDRAWAL.finditer(v)}

    for number in sorted(items_closed(change), key=int):
        declared = criteria(before.get(number, ""))
        if not declared:
            continue
        for ordinal, _was_met, text in declared:
            key = (number, str(ordinal))
            if key in met:
                continue
            if key in deferred:
                target, _date = deferred[key]
                if target not in after:
                    problems.append(
                        f"board item {number} criterion {ordinal} "
                        f"({text[:60]!r}) is deferred onto item {target}, "
                        f"which does not exist in the board file after this "
                        f"change. File the item, or account for the "
                        f"criterion as met."
                    )
                continue
            if key in withdrawn:
                date, cited = withdrawn[key]
                reason = _ruling_record_problem(change, date, cited)
                if reason:
                    problems.append(
                        f"board item {number} criterion {ordinal} "
                        f"({text[:60]!r}) is withdrawn by a ruling, and that "
                        f"ruling {reason}."
                    )
                continue
            problems.append(
                f"board item {number} is retired by this change but "
                f"criterion {ordinal} ({text[:60]!r}), declared when the "
                f"item was filed, is accounted for neither way. Add "
                f"`Done-criteria-met: {number}/{ordinal}` to a commit "
                f"message if it shipped, or "
                f"`Done-criteria-deferred: {number}/{ordinal} -> item N "
                f"(YYYY-MM-DD)` naming the item that now carries it. If the "
                f"owner has RULED that the work will not be done, add "
                f"`Done-criteria-withdrawn: {number}/{ordinal} -> ruling "
                f"YYYY-MM-DD (<board record file>)` citing where that ruling "
                f"is written down."
            )
    return problems
