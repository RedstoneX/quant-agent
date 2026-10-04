"""The never-blank falsifier path: heal from the model's own words, then count.

Board item 78, box "the never-blank path is live". The desk may not put a
BUY/SHORT in the ticket book without a real `thesis_invalid_if` (the
"I'll sell if" sentence). The standing order is: heal mechanically from
what the model already wrote, re-ask the seat at most ONCE and pay for
it, then REFUSE the name. Never invent a sentence, and never let the
refusal be a silent skip.

This module holds the two pieces that were missing from that path.

1. `heal_targets_from_raw` — the mechanical heal of LAST resort, run on
   the PM's constructed targets against the RAW JSON the model returned,
   immediately before the desk would pay for a re-ask.

   `src.seat_heal.restore_stated_soft_exits` already heals a wipe that
   happens inside `BaseModel._explicit_null_means_absent`, because it
   runs inside that same validator and can see the dict as it arrived.
   It cannot see a blank produced ANYWHERE ELSE — a later assignment
   that re-runs the validator and leaves the field unset, a rebuild of
   the target list, or a parse that never carried the key onto the
   canonical field at all. In every one of those the sentence the model
   wrote is still sitting in the raw JSON on the agent result, and the
   desk's only remaining move was to spend money asking for a sentence
   it already had — or, when no retry was available, to refuse a name
   the seat had in fact answered.

   So this runs the same mechanical restore one level up, against the
   raw payload, before any spend. It copies a STATED string and nothing
   else: no placeholder, no "unknown", no invented text, and it never
   overwrites a falsifier that is already stated.

2. `refusal_tally` — the counted record of the refusals. Every
   money-path attempt on this desk gets a counted row, and a refusal is
   a decision about money. The per-name `pipeline_event` rows say which
   names were refused; this says HOW MANY and WHY, so "skip and
   continue" cannot be the silent product: a run that refused names
   carries a count of them with the heal outcome that preceded each.
"""

from __future__ import annotations

from collections import Counter

#: Stage name for the counted summary row. Distinct from the per-name
#: `deterministic_gate` rows so a reader can count refusals without
#: double-counting the names.
REFUSAL_COUNT_STAGE = "soft_exit_refusal_count"
REFUSAL_COUNT_REASON = "soft-exit refusals counted for this run"

#: The only source this heal can ever have. Named so a reader of the
#: recorded rows can prove no text was invented.
HEAL_SOURCE_RAW = "raw_model_output"


def _raw_targets(raw) -> dict:
    """Index the raw payload's targets by upper-case symbol. Never raises."""
    out: dict[str, dict] = {}
    if not isinstance(raw, dict):
        return out
    items = raw.get("targets")
    if not isinstance(items, list):
        return out
    for item in items:
        if not isinstance(item, dict):
            continue
        symbol = str(item.get("symbol") or "").strip().upper()
        if symbol:
            out.setdefault(symbol, item)
    return out


def heal_targets_from_raw(targets, raw) -> tuple[list, list[str]]:
    """Put back a stated falsifier from the raw model payload.

    Only fills a target whose `thesis_invalid_if` is blank or `unknown`,
    and only from a non-empty, non-`unknown` string the model itself
    wrote on the matching raw target. Never invents, never overwrites a
    stated string, never raises. Returns `(targets, symbols filled)`.
    """
    from src.models.base import stated_soft_exit
    from src.seat_heal import (
        _set_target_falsifier, _target_field, _target_symbol,
    )

    if not targets:
        return targets, []
    by_symbol = _raw_targets(raw)
    if not by_symbol:
        return targets, []
    filled: list[str] = []
    out = []
    for target in targets:
        current = _target_field(target, "thesis_invalid_if")
        if stated_soft_exit(current):
            out.append(target)
            continue
        symbol = _target_symbol(target)
        raw_target = by_symbol.get(symbol) if symbol else None
        raw_value = (
            raw_target.get("thesis_invalid_if")
            if isinstance(raw_target, dict) else None
        )
        # A non-string raw value is not a sentence the model wrote. It is
        # ignored rather than coerced: stringifying a number would be
        # inventing a falsifier out of punctuation.
        stated = (
            stated_soft_exit(raw_value) if isinstance(raw_value, str) else None
        )
        if not stated:
            out.append(target)
            continue
        out.append(_set_target_falsifier(target, stated))
        filled.append(symbol)
    return out, filled


def refusal_tally(symbols, heals: dict | None = None) -> dict:
    """The counted record for a run's blank-falsifier refusals.

    `symbols` are the names refused before the book; `heals` is
    `ctx.soft_exit_heals` (per-name heal outcome). Returns a flat dict of
    counts plus the per-outcome breakdown, so the reason a name was
    refused is counted alongside the refusal itself. A name with no heal
    record is counted under `none_recorded` rather than dropped — an
    absent record is a fact about the run, not a zero.
    """
    unique = list(dict.fromkeys(
        str(s).strip().upper() for s in (symbols or []) if str(s).strip()
    ))
    lookup = heals if isinstance(heals, dict) else {}
    outcomes: Counter[str] = Counter()
    for symbol in unique:
        entry = lookup.get(symbol)
        outcome = ""
        if isinstance(entry, dict):
            outcome = str(entry.get("outcome") or "").strip()
        outcomes[outcome or "none_recorded"] += 1
    return {
        "refused_count": len(unique),
        "refused_symbols": unique,
        "by_heal_outcome": dict(sorted(outcomes.items())),
    }
