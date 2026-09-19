"""The AI Risk Manager seat is ADVISORY — owner ruling 2026-09-19.

What the seat may no longer do
------------------------------
Block or change a trade. On the morning plan (`pipeline_stages.RiskStage`)
and on the midday/close exit review (`pipeline._risk_review_exits`), every
lever the seat's verdict carries is RECORDED with its reason and NOT applied:

- `approved=false` (the whole-plan veto),
- `rejected_symbols` (per-name refusal),
- `modifications` (allocation / entry / stop / target edits),
- `scale_all_buys` (including 0.0, which used to drop every entry).

Why: the owner asked "should the risk seat be allowed to block whole plans
over guidelines, or only over hard limits?" and answered "it shouldn't have
to" — hard limits are enforced by code (`src/risk/rules.py` hard-block path,
the risk budget, the evidence gate), and those run exactly as before. The
audit behind board items 134 and 162 found the seat's size edits were
invented numbers (the briefing's own worked example reproduced verbatim) and
that 2 of its 3 whole-plan vetoes cited ADVISORY limits as "hard rules",
because its briefing told it to.

What is NOT touched here
------------------------
Code-owned drops stay. The morning path's holding-discipline check drops a
SELL/REDUCE/COVER whose claimed trigger is PROVABLY FALSE, and the exit path
drops an exit that names no recognised trigger, fails the noise band, the
metric-contradiction veto or the holding-discipline fact check. Those are
deterministic Python, not the seat, and they keep their own drop paths.

This module holds only the wording and the pure bookkeeping, so the morning
path, the exit path, the Telegram feed and the dashboard all describe a
recorded-but-not-applied objection in the same words.
"""

from __future__ import annotations

#: The one sentence every surface appends to a recorded objection.
RULING = "not applied — owner ruling 2026-09-19"

#: `pipeline_event` outcome for a leg the seat objected to. Not in
#: `refusal_signature.SURVIVED_OUTCOMES` on purpose: it is not the terminal
#: event for the leg (the `deterministic_gate`/execution events after it are),
#: and it must never read as the seat having let something through or killed it.
OUTCOME_OBJECTION = "objection_not_applied"

#: `gate` detail on those events, and the marker key written into the seat's
#: own evidence rows (`verdict`, `modification`, `rejection`) so a reader can
#: tell a recorded-but-not-applied row from a pre-ruling one that WAS applied.
GATE_ADVISORY = "risk_manager_advisory"
APPLIED_KEY = "applied"


def objection_text(reason: str) -> str:
    """How an objection is described to the owner, everywhere."""
    reason = str(reason or "").strip() or "no reason stated"
    return f"the risk reviewer objected: {reason}; {RULING}"


def _fmt(value) -> str:
    try:
        return f"{float(value):g}"
    except (TypeError, ValueError):
        return str(value)


def objections_by_symbol(verdict, decisions) -> tuple[dict[str, list[str]], list[str]]:
    """Every lever the verdict pulled, as per-symbol objection sentences.

    Returns `(per_symbol, unmatched)`:
    - `per_symbol` maps each UPPER-CASE symbol in `decisions` to the list of
      objections that apply to it (empty symbols are omitted);
    - `unmatched` lists objections that named a symbol not in `decisions`
      (recorded run-scoped by the caller — a symbol-scoped row for a name the
      run never considered would read as a candidate to the refusal alarm).

    Pure: reads the verdict and the decisions, changes neither. Tolerates the
    exit-path verdict shape, which has no `modifications` / `scale_all_buys`.
    """
    per_symbol: dict[str, list[str]] = {}
    unmatched: list[str] = []
    in_plan: dict[str, str] = {}
    for d in decisions or []:
        if d is None:
            continue
        sym = str(getattr(d, "symbol", "") or "").strip().upper()
        if sym:
            in_plan.setdefault(sym, str(getattr(d, "action", "") or ""))

    def _add(sym: str, text: str) -> None:
        sym = str(sym or "").strip().upper()
        if sym in in_plan:
            per_symbol.setdefault(sym, []).append(text)
        else:
            unmatched.append(f"{sym or '?'}: {text}")

    if getattr(verdict, "approved", True) is False:
        book = str(getattr(verdict, "reasoning", "") or "").strip() or "no reason stated"
        for sym in in_plan:
            _add(sym, f"whole-plan veto — {book}")

    rejections = {}
    by_symbol = getattr(verdict, "rejections_by_symbol", None)
    if callable(by_symbol):
        rejections = by_symbol()
    for sym, reason in rejections.items():
        _add(sym, f"refuse this trade — {reason}")

    for mod in getattr(verdict, "modifications", None) or []:
        _add(
            mod.symbol,
            f"change {mod.field} {_fmt(mod.original_value)} -> "
            f"{_fmt(mod.new_value)} — {mod.reason}",
        )

    scale_raw = getattr(verdict, "scale_all_buys", None)
    if scale_raw is not None:
        try:
            scale = float(scale_raw)
        except (TypeError, ValueError):
            scale = None
        if scale is not None and scale != 1.0:
            category = getattr(verdict, "reason_category", None)
            for sym, action in in_plan.items():
                if action in ("BUY", "SHORT"):
                    _add(
                        sym,
                        f"scale every new entry by {_fmt(scale)} "
                        f"(category {category!r})",
                    )
    return per_symbol, unmatched
