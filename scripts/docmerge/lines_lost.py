"""Lines a merge dropped (moved out of resolve_doc_conflict.py to keep it from growing).

Pure: counts lines, touches no git and no file; loaded by path from
`scripts/resolve_doc_conflict.py` the way `status_board.py` is.
"""

from __future__ import annotations


def _lines_lost(text: str, ours: str, theirs: str, base: str = "") -> list[str]:
    """Non-blank lines present on either side and absent from `text`.

    Counted, not just set-tested: a line that appears three times on one side
    and once in the result has lost two copies, and for a document whose
    entries are paragraphs of prose that is a real loss.

    `base` is what stops this from crying wolf on every ordinary merge. A
    three-way merge is SUPPOSED to drop a line that one side deleted relative
    to the base and the other side left untouched — that deletion is the
    change being merged, not data loss. Without the base this function counted
    every such line as missing, so a branch merging a fast-moving trunk
    forward tripped it on the trunk's own edits and had both whole copies of
    the document appended to it [measured 2026-10-04: 20 "lost" lines and a
    376-line document turned into 1145 lines, on a merge git itself resolved
    down to a single conflict region].
    """
    from collections import Counter

    def counts(s: str) -> Counter:
        return Counter(ln.rstrip() for ln in s.splitlines() if ln.strip())

    have = counts(text)
    in_base = counts(base)
    sides = (counts(ours), counts(theirs))
    lost: list[str] = []
    for mine, other in (sides, sides[::-1]):
        for ln, n in mine.items():
            # Copies of this line the OTHER side deleted relative to the base:
            # a correct merge honours that deletion, so they are not losses.
            deleted_by_other = max(0, min(in_base[ln], n) - other[ln])
            want = n - deleted_by_other
            if have[ln] < want:
                lost.extend([ln] * (want - have[ln]))
    return lost
