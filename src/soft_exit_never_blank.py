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


def soft_exit_heal_detail(ctx, symbol: str) -> str:
    """The TRUE per-name heal outcome, for the refusal's durable reason.

    Board item 78 / owner 2026-09-25 ("untrue is a lie"): the refusal used
    to assert "after mechanical heal and one paid retry" for every name,
    including names whose retry was never attempted. It now states what the
    heal record says, and says plainly when there is no heal record at all.
    """
    heal = (getattr(ctx, "soft_exit_heals", None) or {}).get(
        str(symbol).strip().upper()
    )
    if isinstance(heal, dict) and (heal.get("detail") or heal.get("outcome")):
        return (
            f"heal outcome '{heal.get('outcome') or 'unknown'}': "
            f"{heal.get('detail') or ''}".strip()
        )
    return (
        "no soft-exit heal was recorded for this name — the mechanical "
        "restore did not fill it and no paid retry outcome was filed"
    )


def apply_mechanical_heal(decision, result, missing, record_heal, log) -> list:
    """Heal before any spend; return the names still missing. Never invents.

    Moved verbatim out of the PM's fill step. `record_heal` and `log` are
    handed in by the caller at call time (the owner's bound recorder, its
    module logger), so nothing here holds a frozen copy of either.
    """
    from src.seat_heal import HEAL_MECHANICAL

    try:
        raw_payload = result.parse_json() if result is not None else None
    except Exception:  # noqa: BLE001 — an unparseable raw just means no heal
        raw_payload = None
    healed, healed_symbols = heal_targets_from_raw(
        list(getattr(decision, "targets", None) or []), raw_payload,
    )
    if not healed_symbols:
        return missing
    decision.targets = healed
    log.info(
        "Soft-exit mechanical heal restored thesis_invalid_if from "
        "the model's own raw output for %s — not invented and not "
        "paid for", healed_symbols,
    )
    record_heal(
        healed_symbols, HEAL_MECHANICAL,
        "the falsifier the model itself already wrote was restored "
        "from the raw seat output after a later wipe blanked it; no "
        "text was invented and no retry was bought",
    )
    healed_set = {str(s).strip().upper() for s in healed_symbols}
    return [s for s in missing if str(s).strip().upper() not in healed_set]


def soft_exit_retry_targets(retried) -> list:
    """The raw targets list of a retry result, or [] when there is none."""
    reparsed = retried.parse_json() if retried is not None else None
    retry_targets = []
    if isinstance(reparsed, dict) and isinstance(reparsed.get("targets"), list):
        retry_targets = reparsed["targets"]
    return retry_targets


def soft_exit_fill_coda(missing) -> str:
    """The completion request appended to the replayed user message."""
    return (
        "\n\n## SOFT-EXIT COMPLETION REQUIRED — NOT A RE-DECISION\n"
        "These open/increase targets are missing a real thesis_invalid_if "
        "(I'll sell if). Fill ONLY that field on the named symbols with "
        "one concrete observable. Do NOT invent a catalyst unless you are "
        "citing a dated Active News State Change for the unmeasurable-"
        "range exception. Do NOT change symbol, direction, conviction, "
        "thesis, risk_allocation_pct, target_weight_pct, or "
        "suggested_stop_price. Do NOT add or remove targets. If you "
        "cannot state a real falsifier, leave that name's "
        "thesis_invalid_if empty — Python will refuse that name; do not "
        "invent text.\n"
        f"Symbols: {', '.join(missing)}\n"
        "Respond ONLY with the complete JSON object.\n"
    )


def add_constructor_dropped(portfolio_decision, symbols) -> None:
    """Add refused names to `constructor_dropped`, keeping order, no repeats."""
    existing = list(
        getattr(portfolio_decision, "constructor_dropped", None) or []
    )
    for symbol in symbols:
        if symbol not in existing:
            existing.append(symbol)
    portfolio_decision.constructor_dropped = existing


def record_refusal_count(record_event, log, pipeline, ctx, symbols) -> None:
    """COUNT the blank-falsifier refusals this run made. Never raises.

    Board item 78. One counted row per run carries the count and the heal
    outcome that preceded each refusal. Recording only: nothing reads it
    back into a decision and it may never be swept for a threshold.
    `record_event` is the caller's `_record_pipeline_event`, handed in at
    call time so a patch on the caller's module is still honoured.
    """
    try:
        tally = refusal_tally(
            symbols, getattr(ctx, "soft_exit_heals", None),
        )
        if not tally["refused_count"]:
            return
        record_event(
            pipeline, ctx, None, REFUSAL_COUNT_STAGE,
            str(tally["refused_count"]), REFUSAL_COUNT_REASON,
            **tally,
        )
    except Exception as exc:  # noqa: BLE001 — a recording never blocks a trade
        log.error("soft-exit refusal count recording failed: %s", exc)
