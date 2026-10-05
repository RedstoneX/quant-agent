"""A deleted mechanism must not survive in prose that describes it as live.

WHY THIS FILE EXISTS (board item 185, 2026-09-30). This desk's own rule is
that when a mechanism is removed you search for its name across every
prompt, assembled string and comment, and fail if it still appears. Nothing
enforced that. Two deletions proved it:

* Board item 56 (2026-09-26) deleted the constructor's stop-WIDTH refusal
  `STOP_REFUSAL_WIDER_THAN_REACH`. Four comments and docstrings in the same
  module went on telling the reader that "the width gate" judges the stop,
  and they survived a further four days and a second review.
* Board item 185 (2026-09-30) deleted the no-ATR branch's
  `STOP_REFUSAL_STRUCTURAL_STOP_TOO_FAR` and the flat
  `STOP_SANITY_FLOOR_FRACTION`, and the same shape of residue appeared
  again in the same change.

Prose that describes a dead gate is worse than no prose: the next reader
believes a protection exists that does not, which is exactly how the item-56
entry in `docs/INCIDENT_HISTORY.md` came to cite a compensating control that
had never run.

HOW TO ADD A ROW when you delete a mechanism: put its name (and any phrase
the codebase used to describe it in the present tense) in `_DELETED`, with
the files that are allowed to mention it — the tombstone comment at the
deletion site, and nothing else. Then run this test; every hit it reports is
a sentence you have to correct or delete. Do NOT widen `allowed` to make the
test pass; widening it is the failure this file exists to catch.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from tests.residue_scan import residue_files

ROOT = Path(__file__).resolve().parent.parent
SEARCH_ROOTS = (ROOT / "src", ROOT / "config" / "prompts", ROOT / "scripts", ROOT / "ops")


class DeletedMechanism:
    def __init__(
        self, label: str, patterns: tuple[str, ...], allowed: tuple[str, ...],
        why: str,
    ) -> None:
        self.label = label
        self.patterns = patterns
        #: Repo-relative paths permitted to mention it — tombstones only.
        self.allowed = frozenset(allowed)
        self.why = why


_DELETED = (
    DeletedMechanism(
        label="the constructor's stop-WIDTH refusals",
        patterns=(
            # The live-voice phrase. A tombstone says "the width gate WAS
            # deleted"; live prose says "the width gate judges / refuses /
            # still judges", and only the latter is banned.
            r"width gate (?:below )?(?:still )?(?:judges|refuses|catches|applies|runs)",
            r"(?:falls through to|reaches) the width gate",
        ),
        allowed=(),
        why=(
            "`STOP_REFUSAL_WIDER_THAN_REACH` was deleted 2026-09-26 (board "
            "item 56 route (c)) and `STOP_REFUSAL_STRUCTURAL_STOP_TOO_FAR` "
            "on 2026-09-30 (board item 185). No width refusal remains in "
            "`portfolio_constructor`; width is answered by sizing down."
        ),
    ),
    DeletedMechanism(
        label="the flat 50%-of-price stop sanity floor",
        patterns=(r"STOP_SANITY_FLOOR_FRACTION",),
        allowed=(
            # The tombstone in the constant's own docstring block.
            "src/portfolio_constructor/config.py",
            "src/portfolio_constructor/stops.py",
            "src/portfolio_constructor/entry_stop/resolver.py",
            # `MAX_ARBITRARY_ENTRIES`'s running history IS the register of
            # retired ledger rows; naming a deleted one there is the point.
            "src/number_sources.py",
        ),
        why=(
            "Board item 185 (2026-09-30) deleted the literal. The midday "
            "path clamps to `widest_reachable_stop_atr_multiple` x the "
            "name's own ATR14 and the screen's ceiling is 1 / that "
            "multiple; neither borrows a fraction of price any more."
        ),
    ),
    DeletedMechanism(
        label="the midday TRAIL_STOP width REFUSAL",
        patterns=(
            # It must never come back as a refusal: on the branch it runs,
            # the live stop was unreadable, so refusing leaves the position
            # unprotected (owner ruling, board item 80).
            r"likely LLM error",
        ),
        # Same reason as above: the ledger-count history is a tombstone
        # register, and it quotes the exact grep that measured the old
        # guard's zero firings.
        allowed=("src/number_sources.py",),
        why=(
            "Board item 185 (2026-09-30): an over-wide midday trailing stop "
            "is CLAMPED to the widest legitimate stop and placed, never "
            "refused. A refusal there can only fire when the live broker "
            "stop was unreadable, i.e. exactly when skipping leaves the "
            "name naked -- board item 80's ruling in a different costume."
        ),
    ),
)


def _files() -> list[Path]:
    return residue_files(ROOT, SEARCH_ROOTS)


@pytest.mark.parametrize("mech", _DELETED, ids=lambda m: m.label)
def test_a_deleted_mechanism_is_not_described_as_live(mech: DeletedMechanism) -> None:
    hits: list[str] = []
    for path in _files():
        rel = path.relative_to(ROOT).as_posix()
        if rel in mech.allowed:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for pattern in mech.patterns:
            for m in re.finditer(pattern, text):
                line_no = text.count("\n", 0, m.start()) + 1
                line = text.splitlines()[line_no - 1].strip()
                hits.append(f"{rel}:{line_no}: {line}")
    assert not hits, (
        f"{mech.label} no longer exists, but {len(hits)} place(s) still name "
        f"it or describe it in the present tense.\n{mech.why}\n\n"
        + "\n".join(hits)
        + "\n\nCorrect or delete each line. Do NOT add it to `allowed` "
        "unless the line is a tombstone recording the deletion."
    )

