"""Prompt text naming the triggers already SPENT today, lifted out of
``spent_trigger.py`` (at its size ratchet) so that module's catch-all could be
made LOUD. Pure rendering over the rows it is handed; ``spent_trigger``
re-exports it under the same name.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.risk.spent_trigger import ActedTrigger


def format_spent_triggers_block(acted: list[ActedTrigger] | None, symbols: set[str] | None = None) -> str:
    """Prompt text naming what is already spent, verbatim.

    The seat must be able to see what it may not re-cite; an invisible
    filter is the shape `position_reviewer.md` already calls out as unfair
    to the seat. Empty string when nothing is spent.
    """
    rows = [r for r in (acted or []) if r.trigger and (symbols is None or r.symbol in symbols)]
    if not rows:
        return ""
    lines = []
    for r in sorted(rows, key=lambda x: (x.symbol, x.trigger)):
        ev = r.evidence.strip() or "(no record cited)"
        lines.append(f"  - {r.symbol} · `{r.trigger}` · already acted on: {ev}")
    return (
        "**Triggers already SPENT today (the desk has acted on these):**\n" + "\n".join(lines) + "\n"
        "A SELL / REDUCE / COVER whose `exit_trigger` is one of the above "
        "for that symbol AND whose `trigger_evidence` is that same record is "
        "REFUSED by the executor and recorded as `trigger_already_spent` — "
        "the position HOLDS, protected by its broker-resident stop. "
        "If the position genuinely got worse, cite the DIFFERENT record that "
        "says so (a later filing, a new headline, a different metric) and the "
        "cut goes through. If there is no new record, HOLD: re-reading the "
        "same one is not new information.\n"
    )
