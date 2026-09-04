"""The override algebra for a sizing intent (`docs/WORK.md` item 13).

**The problem this exists to kill.** A target weight of 0% used to mean three
incompatible things and the plumbing could not tell them apart:

  1. "Do not open this" — a refusal. Nothing should happen.
  2. "Close what is held" — a real exit instruction.
  3. "Open a short" — a new position in the other direction.

A zero-weight entry read to the delta loop in `PortfolioConstructor` as
meaning #2 by default. So a rule that refuses to BUY something could silently
SELL a position nobody asked to sell — it did not error, it just liquidated.
The 2026-09-02 "signed-dissent rule" (`PortfolioConstructor._plan_risk_targets`)
worked around this in its one call site by DROPPING a refused target rather
than sizing it at zero, but that made the fix procedural: every future caller
that can refuse a target has to remember to do the same thing, and nothing
stops a new one from forgetting.

**The fix, borrowed from `pysystemtrade`.** Rob Carver's open-source
systematic trading framework solves exactly this with an "override algebra":
an override is not a plain number, it is a value in a small ORDERED set, and
combining two overrides is defined by a rule, not by arithmetic. The order is
absorbing —

    no_trading  >  close  >  reduce_only  >  (a plain multiplier)

— so combining any two overrides always yields the MORE restrictive one.
"Do not trade" can never be diluted back into "trade a bit" by multiplying it
with something else, the way `min(0.0, anything)` would still just be a float
that looks exactly like a deliberate zero-weight close.

This module makes the four intents DISTINCT VALUES with a defined combination
rule, so the item-13 mistake stops being expressible in the type itself
instead of relying on every caller remembering a special case. A `SizeOverride`
of kind `no_trading` has no numeric `.value` at all — reading `.value` off one
raises, on purpose, rather than silently handing back a `0.0` a downstream
consumer could read as "close".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import reduce
from typing import Literal

SizeOverrideKind = Literal["no_trading", "close", "reduce_only", "multiplier"]

#: Absorbing order, most restrictive first. `combine()` always keeps the
#: operand with the LOWER rank — this dict IS the algebra from item 13's
#: `no_trading > close > reduce_only > multiplier` — never re-derive it
#: ad hoc at a call site.
_RANK: dict[SizeOverrideKind, int] = {
    "no_trading": 0,
    "close": 1,
    "reduce_only": 2,
    "multiplier": 3,
}


@dataclass(frozen=True)
class SizeOverride:
    """One sizing intent, drawn from the item-13 / pysystemtrade algebra.

    Construct via the classmethods (`no_trading`, `close`, `reduce_only`,
    `sized`) rather than the constructor directly — they are what keep an
    invalid combination (e.g. a numeric value on a `no_trading` override)
    from being expressible at all.

    - `no_trading` — refuse the target outright. Nothing should happen to
      the position, held or not. This is what a symbol whose agreement score
      nets at or below zero (`agreement_ceiling_for_score`) now produces,
      where before it produced a bare `0.0` float.
    - `close` — a real exit instruction: flatten whatever is held. This is
      the PM's own `TargetPosition.risk_allocation_pct == 0.0` / `is_close`,
      an intentional, sourced instruction — not a computed refusal.
    - `reduce_only` — the position may shrink, never grow. Not yet driven by
      a live caller; included because it is part of the borrowed algebra and
      a future rule (e.g. "this cluster may only de-risk today") should reach
      for this rather than inventing another ambiguous float.
    - `multiplier` — an ordinary resolved weight/risk-pct. This is the "real
      trade" case; `.value` is only ever defined here.
    """

    kind: SizeOverrideKind
    _value: float | None = field(default=None, repr=False)

    @classmethod
    def no_trading(cls) -> "SizeOverride":
        return cls("no_trading")

    @classmethod
    def close(cls) -> "SizeOverride":
        return cls("close")

    @classmethod
    def reduce_only(cls) -> "SizeOverride":
        return cls("reduce_only")

    @classmethod
    def sized(cls, value: float) -> "SizeOverride":
        """A plain multiplier: `value` is the resolved weight/risk-pct this
        override permits. Negative sizes are not a sizing concept — a caller
        with nothing to allocate should reach for `no_trading()`, not a
        negative multiplier."""
        if value < 0:
            raise ValueError(
                f"SizeOverride.sized() got a negative value ({value!r}) — "
                "use SizeOverride.no_trading() to refuse a target, not a "
                "negative multiplier"
            )
        return cls("multiplier", float(value))

    @property
    def value(self) -> float:
        """The resolved weight/risk-pct this override permits.

        Defined ONLY for kind == "multiplier", and deliberately so: the whole
        point of this type is that there is no numeric fallback for
        `no_trading` / `close` / `reduce_only`. A `0.0` fallback here would
        just recreate the original item-13 bug one layer down — a caller
        reading `.value` off a refusal and silently treating it as a
        zero-weight close. Check `.kind` (or `.is_tradeable`) first.
        """
        if self.kind != "multiplier":
            raise ValueError(
                f"SizeOverride.value is undefined for kind={self.kind!r} — "
                "check `.kind` (or `.is_tradeable`) before reading a magnitude"
            )
        return self._value

    @property
    def is_tradeable(self) -> bool:
        """True only for a plain multiplier — the one kind that should reach
        `RiskPlan` / the delta loop as an actual size."""
        return self.kind == "multiplier"

    @property
    def is_refusal(self) -> bool:
        """True for `no_trading` — the caller should drop the target, exactly
        as the 2026-09-02 workaround did, but now via the type rather than a
        `requested_pct <= 0.0` check that a `close` could also satisfy."""
        return self.kind == "no_trading"

    def combine(self, other: "SizeOverride") -> "SizeOverride":
        """Combine two overrides per the absorbing order from item 13:

            no_trading  >  close  >  reduce_only  >  multiplier

        The more restrictive operand always wins, so stacking any number of
        overrides can only ever tighten the outcome, never loosen it back
        toward "trade a bit" — that is the structural guarantee this whole
        module exists for. Two `multiplier` overrides combine as the smaller
        (more restrictive) of the two values, matching the existing
        envelope-then-agreement-ceiling `min()` this replaces.
        """
        my_rank, other_rank = _RANK[self.kind], _RANK[other.kind]
        if my_rank < other_rank:
            return self
        if other_rank < my_rank:
            return other
        if self.kind == "multiplier":
            return SizeOverride.sized(min(self.value, other.value))
        # no_trading/close/reduce_only combined with their own kind is
        # idempotent — there is no magnitude to reconcile.
        return self


def combine_overrides(*overrides: SizeOverride) -> SizeOverride:
    """Combine any number of overrides left-to-right via `SizeOverride.combine`.

    With zero arguments there is nothing to restrict, so this returns the
    least restrictive override (an unbounded multiplier) — combining it with
    anything else just yields that other thing, per the algebra above.
    """
    if not overrides:
        return SizeOverride.sized(float("inf"))
    return reduce(SizeOverride.combine, overrides)
