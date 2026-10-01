"""Do not pay the portfolio manager to decide nothing (board item 177).

The portfolio-manager seat is the desk's single largest model expense
($20.51 of $22.66 lifetime across 143 calls, measured 2026-10-01 against
the production DB) and 16 of its last 20 calls produced no trade at all.
Every input it is shown is computed DETERMINISTICALLY before the call, so
a session with nothing to decide is knowable without paying for it.

**This module decides only whether to ASK.** It never decides a trade, it
never changes what the seat is asked, and it holds no threshold, no
interval, no cost ceiling and no "skip at most N in a row" — it is a pure
emptiness test over the decisions the seat is the decider for.

THE ENUMERATION. The seat's output (`PortfolioDecision`) is `targets`,
`rejections`, `portfolio_view` and `reasoning_chain`. Every ACT it can
cause is a `TargetPosition` (see `src/models.py`), and there are exactly
four shapes of one, plus two pieces of bookkeeping only this seat does:

  1. OPEN   target_weight_pct > 0 on an unheld symbol.
  2. ADD    target_weight_pct > current weight on a held symbol.
            1 and 2 are the same precondition: `validate_grounding`
            refuses ANY increase outside the BUY-eligible set, so both
            are impossible unless `candidate_eligibility` admitted a
            name (an entry in `blocked` with an empty reason list).
  3. CLOSE  target_weight_pct == 0 on a held symbol. The owner ruled on
            2026-10-01 that a holding failing the desk's own fresh-entry
            bar must be sold. That set is already computed deterministically
            as `RotationPrecheck.held_below_entry_bar`
            (`rotation.holdings_below_entry_bar`). **A gate keyed only on
            "no buy candidates" would suppress the call in exactly the
            session where this sell was due. That is the whole danger and
            this condition is the answer to it.**
  4. TRIM   0 < target_weight_pct < current weight on a held symbol.
            `validate_grounding` accepts a decrease ONLY on bearish-polarity
            evidence for that name, so a trim is impossible unless some seat
            reported a non-bullish read on a held name this session. Read off
            the SAME evidence registry the prompt is rendered from.
  5. ROTATION — the sell-to-fund-a-better-name comparison, already decided
            deterministically as `RotationPrecheck.opportunity`.
  6. REJECTION BOOKKEEPING — the seat must account for every candidate it
            was SHOWN (board item 133). If it was shown any ranked name,
            there is bookkeeping only it can do.

FAIL OPEN, ALWAYS. Any input missing, unreadable, of an unexpected type,
or any exception at all, returns "call". A skipped call costs about 16
cents; a missed sell costs real money.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

#: Stance words that CANNOT ground a decrease. Anything else a seat may
#: emit — including a word this desk has not seen yet — is treated as
#: possibly bearish and forces the call. Deliberately an allow-list.
_NON_BEARISH_STANCES = frozenset({
    "bullish", "long", "buy", "neutral", "none", "no_call", "no-call", "",
})


@dataclass(frozen=True)
class GateCondition:
    """One decision the seat is the decider for, and whether it is empty."""

    name: str
    empty: bool
    detail: str

    def as_record(self) -> dict[str, Any]:
        return {"name": self.name, "empty": self.empty, "detail": self.detail}


@dataclass(frozen=True)
class PMGateVerdict:
    skip: bool
    reason: str
    conditions: tuple[GateCondition, ...] = field(default_factory=tuple)

    def as_record(self) -> dict[str, Any]:
        """The durable record. A skipped call must never be indistinguishable
        from a call that ran and did nothing, so this carries the reason AND
        the emptiness of every single condition, not just the verdict."""
        return {
            "gate": "portfolio_manager_no_op",
            "outcome": "skipped" if self.skip else "called",
            "reason": self.reason,
            "conditions": [c.as_record() for c in self.conditions],
        }

    def summary_line(self) -> str:
        shape = "skipped" if self.skip else "called"
        parts = ", ".join(
            f"{c.name}={'empty' if c.empty else 'present'}"
            for c in self.conditions
        )
        return f"portfolio manager {shape}: {self.reason} [{parts}]"


def _call(reason: str, conditions: tuple[GateCondition, ...] = ()) -> PMGateVerdict:
    return PMGateVerdict(skip=False, reason=reason, conditions=conditions)


def _eligible_names(blocked: Any) -> list[str] | None:
    """Names `candidate_eligibility` ADMITTED. `None` means unreadable."""
    if not isinstance(blocked, dict):
        return None
    out: list[str] = []
    for symbol, reasons in blocked.items():
        if not isinstance(reasons, (list, tuple, set)):
            return None
        if not reasons:
            out.append(str(symbol).strip().upper())
    return sorted(out)


def _bearish_held(
    evidence_registry: Any, held_symbols: Any,
) -> list[str] | None:
    """Held names carrying any read that is not plainly non-bearish — the
    only grounds `validate_grounding` accepts for a trim. `None` = unreadable."""
    if not isinstance(evidence_registry, dict):
        return None
    try:
        held = {str(s).strip().upper() for s in held_symbols if str(s).strip()}
    except TypeError:
        return None
    flagged: set[str] = set()
    for symbol, sources in evidence_registry.items():
        key = str(symbol).strip().upper()
        if key not in held:
            continue
        if not isinstance(sources, dict):
            return None
        for stance in sources.values():
            if str(stance).strip().lower() not in _NON_BEARISH_STANCES:
                flagged.add(key)
                break
    return sorted(flagged)


def evaluate_pm_gate(
    *,
    blocked: Any,
    ranked: Any,
    precheck: Any,
    held_symbols: Any,
    evidence_registry: Any,
    pending_soft_exit_heals: Any = None,
) -> PMGateVerdict:
    """Skip the paid portfolio-manager call only when EVERY decision the
    seat is the decider for is provably empty. See the module docstring for
    the enumeration. Never raises."""
    try:
        if precheck is None:
            return _call("rotation pre-check did not run this session")
        if not getattr(precheck, "telemetry_available", False):
            return _call("the book's risk was not visible this session")

        eligible = _eligible_names(blocked)
        if eligible is None:
            return _call("the eligibility map was unreadable")

        if not isinstance(ranked, (list, tuple)):
            return _call("the candidate ranking was unreadable")

        held_below = getattr(precheck, "held_below_entry_bar", None)
        if not isinstance(held_below, (list, tuple)):
            return _call("the below-entry-bar set was unreadable")
        held_below = [str(s).strip().upper() for s in held_below]

        bearish = _bearish_held(evidence_registry, held_symbols)
        if bearish is None:
            return _call("the evidence registry was unreadable")

        if pending_soft_exit_heals is None:
            heals: list[str] = []
        elif isinstance(pending_soft_exit_heals, dict):
            heals = sorted(str(s) for s in pending_soft_exit_heals)
        elif isinstance(pending_soft_exit_heals, (list, tuple, set)):
            heals = sorted(str(s) for s in pending_soft_exit_heals)
        else:
            return _call("the pending soft-exit heals were unreadable")

        opportunity = getattr(precheck, "opportunity", None)

        conditions = (
            GateCondition(
                "open_or_add", not eligible,
                "no name clears the desk's own entry bar today"
                if not eligible else f"eligible to buy: {', '.join(eligible)}",
            ),
            GateCondition(
                "close_held_failing_entry_bar", not held_below,
                "every holding still clears the bar it was bought on"
                if not held_below
                else f"below the entry bar: {', '.join(held_below)}",
            ),
            GateCondition(
                "trim_held", not bearish,
                "no seat reported a non-bullish read on a holding"
                if not bearish else f"non-bullish reads on: {', '.join(bearish)}",
            ),
            GateCondition(
                "rotation", opportunity is None,
                "no rotation opportunity qualified"
                if opportunity is None else "a rotation opportunity qualified",
            ),
            GateCondition(
                "rejection_bookkeeping", not ranked,
                "no candidate was shown, so there is nothing to account for"
                if not ranked else f"{len(ranked)} candidate(s) shown",
            ),
            GateCondition(
                "soft_exit_heals", not heals,
                "no soft-exit heal is queued"
                if not heals else f"queued heals: {', '.join(heals)}",
            ),
        )

        if all(c.empty for c in conditions):
            return PMGateVerdict(
                skip=True,
                reason=(
                    "nothing to decide: no eligible buy, no holding below the "
                    "desk's own entry bar, no trim grounds, no rotation "
                    "opportunity, no candidate to account for and no queued heal"
                ),
                conditions=conditions,
            )
        present = [c.name for c in conditions if not c.empty]
        return PMGateVerdict(
            skip=False,
            reason="there is something to decide: " + ", ".join(present),
            conditions=conditions,
        )
    except Exception as exc:  # noqa: BLE001 - fail open, always
        return _call(f"the emptiness test could not be evaluated ({exc!r})")
