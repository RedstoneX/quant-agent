"""Was the closure argued against? Split out of scripts/work_queue.py.

The backlog stop hook blocks a session that closes a board item without an
adversary argument in the pull request. Moved here unchanged on 2026-10-08 so
the hook could gain the finish-before-you-start check without growing.
"""
from __future__ import annotations

import re
from typing import Any

from src.inflight import RETIRED_LINE, WORK_MD, OpenPR, read_open_pull_requests



#: The owner's rule: no board item is closed until the adversary agent
#: (`.claude/agents/qamc-adversary.md`) has argued against closing it. It was
#: skipped on five closures in a row on 2026-09-13, and the reason it keeps
#: being skipped is that work coming back green produces no signal that it
#: was never challenged. Nothing about "remember to run the adversary" has
#: held; this is the mechanical version.
#:
#: The evidence is a line in the pull request description beginning
#: `Adversary:` and carrying at least a sentence. A bare `Adversary: yes` is
#: not evidence of an argument and does not count.
ADVERSARY_LINE = re.compile(r"^\s*Adversary\s*:\s*(.+)$", re.I | re.M)

#: What counts as "at least a sentence" after the label. Deliberately low —
#: this is a presence check, not a quality one; judging the argument is the
#: reader's job, and a threshold high enough to judge would be gamed by
#: padding anyway.
MIN_ADVERSARY_WORDS = 6

#: "item 12", "items 30/31/32" — the convention this repository's pull
#: request titles already use for the item they close.
ITEM_REF = re.compile(r"\bitems?\s+#?(\d+)\b", re.I)

#: In the BODY the same words are usually a citation, not a claim: PR 351
#: ("docs: make README match the desk that actually exists") mentions
#: "item 44" only to explain where a deleted rule came from, and demanding
#: an adversary line for that is the cried wolf. So a body reference counts
#: only when a closing verb is attached to it.
ITEM_CLOSED = re.compile(
    r"\b(?:closes?|closing|closed|resolves?|resolved|retires?|retired"
    r"|completes?|completed|finishes?|finished|fixes)\b[^.\n]{0,40}?"
    r"\bitems?\s+#?(\d+)\b", re.I)


def closes_a_board_item(pr: OpenPR) -> str | None:
    """Why this change looks like a board closure, or None.

    Two independent signals, either of which is enough:

      * it edits the backlog's "Retired item numbers" line — that line is
        only ever touched to retire an item, so editing it IS a closure
        whatever the description says; and
      * its TITLE names a board item by number — this repository's own
        convention for the item a change closes — or its description says
        in words that it closes one.

    A change whose files could not be read still gets the second test. It
    never gets the first, because a read that failed is not evidence.
    """
    patch = pr.work_md_patch
    if patch:
        for line in patch.splitlines():
            if line[:1] in "+-" and line[1:2] != line[:1] and RETIRED_LINE in line:
                return f"it edits the {WORK_MD} line that retires board items"
    match = ITEM_REF.search(pr.title or "")
    if match:
        return f"its title names board item {match.group(1)}"
    match = ITEM_CLOSED.search(pr.body or "")
    if match:
        return f"its description says it closes board item {match.group(1)}"
    return None


def has_adversary_evidence(body: str) -> bool:
    """Does this description carry a real `Adversary:` line?"""
    for match in ADVERSARY_LINE.finditer(body or ""):
        argument = match.group(1).strip()
        if len(argument.split()) >= MIN_ADVERSARY_WORDS:
            return True
    return False


def unreviewed_closures(prs: list[OpenPR]) -> list[str]:
    """One plain sentence per open change that closes an item unchallenged."""
    out: list[str] = []
    for pr in prs:
        why = closes_a_board_item(pr)
        if why and not has_adversary_evidence(pr.body):
            out.append(f"PR {pr.number} ({pr.title or 'untitled'}) closes a "
                       f"board item — {why} — but its description carries no "
                       f"'Adversary:' line, so nothing argued against closing "
                       f"it.")
    return out


def adversary_gaps(fetch: Any = None) -> list[str]:
    """The unreviewed closures, or an empty list if GitHub could not be read.

    A failed read must NEVER manufacture work. "I could not see the pull
    requests" is not evidence that a review is missing, and a hook that
    treats it as such would hold sessions open every time GitHub rate-limits
    this address — which for an unauthenticated reader is routine.
    """
    try:
        prs, problem = (read_open_pull_requests(fetch) if fetch
                        else read_open_pull_requests())
    except Exception:  # noqa: BLE001 - this check never breaks a session
        return []
    if problem:
        return []
    return unreviewed_closures(prs)
