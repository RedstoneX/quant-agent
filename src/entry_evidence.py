"""Entry-pinned evidence for a row the execution stage is about to write.

THE DEFECT THIS EXISTS TO CLOSE
-------------------------------
A scale-in ADD is the SAME position as its original entry, so item 82
had execution carry the position's own pinned `setup_type` and
`structural_ceiling` forward instead of re-classifying it against
today's technical read. Re-classifying mid-position is a real bug: it
can silently flip pace/progress on or off for a position nobody made a
new entry decision about.

The hole that leaves is also real. A position opened before a column
existed carries NULL on that column forever, every later ADD copies the
NULL, and the verdict the constructor computed on THAT decision is
thrown away. The desk ends up unable to say what it knew at the moment
it added.

Both halves are kept honest by recording ALONGSIDE, never over the top:

  * the position's entry verdict is whatever its own prior row holds —
    including NULL. An ADD never writes a verdict onto a position that
    does not have one, because "we do not know why this was bought" is
    the true answer for those rows and the desk must be able to say it;
  * the ADD's own freshly computed verdict is recorded as the ADD's own
    evidence, attributed to the top-up, carrying its own run id and
    timestamp — see `scale_in_own_verdict`. Nothing that reads a
    position's entry verdict reads that row.

Deliberately generic in the field it resolves, so the whole class is
closed rather than this one column: any entry-pinned fact added later
gets the same behaviour by being resolved through `pinned_value`.
"""
from __future__ import annotations

import json
from typing import Any

__all__ = [
    "pinned_value",
    "pinned_setup_type",
    "pinned_structural_ceiling",
    "scale_in_own_verdict",
    "SCALE_IN_EVIDENCE_AGENT",
    "SCALE_IN_EVIDENCE_KIND",
    "resolve_entry_pins",
    "record_scale_in_own_verdict",
]

_SENTINEL = object()

# How a top-up's own verdict is filed in `specialist_evidence`. That table
# is an explicitly non-trading forensic record (see its CREATE comment), so
# writing here cannot change what the desk buys, sells, sizes or protects.
SCALE_IN_EVIDENCE_AGENT = "execution_scale_in"
SCALE_IN_EVIDENCE_KIND = "verdict"

# The entry-pinned fields this module resolves. `cast` normalises a value
# read back out of the ledger (`structural_ceiling` is stored 0/1).
_PINNED_FIELDS: dict[str, Any] = {
    "setup_type": None,
    "structural_ceiling": bool,
}


def pinned_value(
    prior_row: dict | None,
    decision: Any,
    field: str,
    *,
    cast=None,
    is_scale_in: bool,
):
    """The value to write for an entry-pinned `field`.

    `prior_row` is the position's own previous open row (None on a fresh
    entry). `decision` is the constructor's `TradeDecision`.

    On a scale-in the position's OWN stored value wins, and a position
    that holds no value keeps holding none: absence is reported as
    absence, never backfilled from a later top-up's reasoning.
    """
    if not is_scale_in:
        fresh = getattr(decision, field, None)
        return None if fresh is None else (cast(fresh) if cast else fresh)
    pinned = (prior_row or {}).get(field, _SENTINEL)
    if pinned is _SENTINEL or pinned is None or pinned == "":
        return None
    return cast(pinned) if cast else pinned


def pinned_setup_type(prior_row, decision, *, is_scale_in: bool) -> str | None:
    value = pinned_value(
        prior_row, decision, "setup_type", is_scale_in=is_scale_in,
    )
    return value or None


def pinned_structural_ceiling(prior_row, decision, *, is_scale_in: bool) -> bool | None:
    return pinned_value(
        prior_row, decision, "structural_ceiling",
        cast=bool, is_scale_in=is_scale_in,
    )


def scale_in_own_verdict(prior_row, decision) -> str | None:
    """The top-up's OWN verdict, as a JSON evidence payload, or None.

    This is what the constructor classified for THIS add — the value the
    held position deliberately does not take on. For each entry-pinned
    field it records the add's own answer and whether the position
    already held one, so a later reader can see both that the position's
    verdict is absent and what was known on the day of the add.

    Returns None when the add carries no computed value at all: an empty
    evidence row would be a record of nothing.
    """
    fields: dict[str, Any] = {}
    for field, cast in _PINNED_FIELDS.items():
        fresh = getattr(decision, field, None)
        if fresh is None or fresh == "":
            continue
        held = (prior_row or {}).get(field, _SENTINEL)
        fields[field] = {
            "add_verdict": cast(fresh) if cast else fresh,
            "position_already_held_a_verdict": (
                held is not _SENTINEL and held is not None and held != ""
            ),
        }
    if not fields:
        return None
    return json.dumps(
        {
            "source": SCALE_IN_EVIDENCE_AGENT,
            "note": (
                "the add's own classification; the held position's entry "
                "verdict is unchanged by this row"
            ),
            "fields": fields,
        },
        sort_keys=True,
    )


def resolve_entry_pins(db, decision, *, is_short: bool, is_scale_in: bool):
    """(prior_row, setup_type, structural_ceiling) for the row about to be written.

    `db` only needs `get_symbol_last_buy(symbol, action=...)`; `prior_row`
    is None on a fresh entry.
    """
    # Item 82: carry the position's OWN pinned classification
    # forward on a scale-in ADD rather than re-reading today's
    # technical, which would silently reclassify a position
    # nobody made a new entry decision about. A short add reads
    # the last SHORT open (item 82 mirror) — get_symbol_last_buy
    # defaults to BUY rows and would otherwise miss the short's
    # original entry. Where the prior row holds NO pinned value
    # the position keeps holding none — the add's own verdict is
    # recorded ALONGSIDE as its own evidence row, never over
    # the top. See `record_scale_in_own_verdict`.
    _existing_buy = (
        db.get_symbol_last_buy(
            decision.symbol,
            action="SHORT" if is_short else "BUY",
        ) if is_scale_in else None
    )
    setup_type = pinned_setup_type(
        _existing_buy, decision, is_scale_in=is_scale_in,
    )
    structural_ceiling = pinned_structural_ceiling(
        _existing_buy, decision, is_scale_in=is_scale_in,
    )
    return _existing_buy, setup_type, structural_ceiling


def record_scale_in_own_verdict(
    db, logger, *, run_id, decision_id, decision, prior_row, is_scale_in: bool,
) -> None:
    """File a top-up's own verdict in `specialist_evidence`; no-op otherwise.

    `db` only needs `insert_specialist_evidence(...)`; a failure is logged
    through `logger` and never raised, because this row is forensic only.
    """
    if is_scale_in:
        # The add's OWN verdict, recorded ALONGSIDE the entry row
        # and never over the top of it: the held position's entry
        # verdict is whatever its own entry row says, including
        # nothing. Forensic only — `specialist_evidence` is read
        # by no trading path (see its CREATE comment), so this
        # cannot change what the desk buys, sizes or protects.
        _own_verdict = scale_in_own_verdict(prior_row, decision)
        if _own_verdict is not None:
            try:
                db.insert_specialist_evidence(
                    run_id=run_id,
                    decision_id=decision_id,
                    agent_name=SCALE_IN_EVIDENCE_AGENT,
                    kind=SCALE_IN_EVIDENCE_KIND,
                    scope="symbol",
                    symbol=decision.symbol,
                    evidence_json=_own_verdict,
                )
            except Exception as exc:  # pragma: no cover - forensic only
                logger.warning(
                    "scale-in evidence not recorded for %s: %s",
                    decision.symbol, exc,
                )
