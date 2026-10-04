"""Pinned entry evidence for a row the execution stage is about to write.

THE DEFECT THIS EXISTS TO CLOSE
-------------------------------
A scale-in ADD is the SAME position as its original entry, so item 82
had execution carry the position's own pinned `setup_type` and
`structural_ceiling` forward instead of re-classifying it against
today's technical read. Re-classifying mid-position is a real bug: it
can silently flip pace/progress on or off for a position nobody made a
new entry decision about.

But "carry the pinned value forward" was implemented as "use the prior
row's value", full stop — including when the prior row has NO value.
Every position opened before a column existed therefore carries NULL
forever: each ADD copies the NULL from the row before it, and the
verdict the constructor computed on THIS decision is thrown away. That
is how three 2026-10-01 entries were written with a fresh `entry_atr`
and a fresh `stop_level_basis` beside a NULL `structural_ceiling` — the
evidence was in hand and discarded.

The rule here keeps both halves honest:

  * a pinned value on the prior row WINS — an add never reclassifies;
  * a MISSING pinned value falls back to the decision's own freshly
    computed value — absence is not a classification, and recording
    the constructor's answer can only add evidence, never overwrite a
    verdict the position already holds.

Deliberately generic in the field it resolves, so the whole class is
closed rather than this one column: any entry-pinned fact added later
gets the same behaviour by being resolved through `pinned_or_fresh`.
"""
from __future__ import annotations

from typing import Any

__all__ = ["pinned_or_fresh", "pinned_setup_type", "pinned_structural_ceiling"]

_SENTINEL = object()


def pinned_or_fresh(
    prior_row: dict | None,
    decision: Any,
    field: str,
    *,
    cast=None,
    is_scale_in: bool,
):
    """The value to write for an entry-pinned `field`.

    `prior_row` is the position's own previous open row (None on a fresh
    entry). `decision` is the constructor's `TradeDecision`. `cast`, when
    given, normalises whichever value wins (the ledger stores
    `structural_ceiling` as 0/1, so the stored int becomes a bool again).
    """
    fresh = getattr(decision, field, None)
    if not is_scale_in:
        return None if fresh is None else (cast(fresh) if cast else fresh)
    pinned = (prior_row or {}).get(field, _SENTINEL)
    if pinned is _SENTINEL or pinned is None or pinned == "":
        # No verdict on the position yet: record the one in hand rather
        # than propagating the hole. This is the fix — see module docstring.
        return None if fresh is None else (cast(fresh) if cast else fresh)
    return cast(pinned) if cast else pinned


def pinned_setup_type(prior_row, decision, *, is_scale_in: bool) -> str | None:
    value = pinned_or_fresh(
        prior_row, decision, "setup_type", is_scale_in=is_scale_in,
    )
    return value or None


def pinned_structural_ceiling(prior_row, decision, *, is_scale_in: bool) -> bool | None:
    return pinned_or_fresh(
        prior_row, decision, "structural_ceiling",
        cast=bool, is_scale_in=is_scale_in,
    )
