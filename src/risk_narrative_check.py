"""Cross-check the PM's whole-book sizing NARRATIVE against its own numbers.

Board item 163. The portfolio manager writes one free-prose `sizing_logic`
field (`ReasoningChain.sizing_logic`) that narrates how the WHOLE book is
sized and routinely names several symbols in one breath -- and it also emits a
structured `risk_allocation_pct` per symbol (`TargetPosition`). Nothing checked
that the words and the numbers agreed. The filed case: run 601011e0's
`sizing_logic` said "RSG and AAPL get 2.5% risk each" while RSG's emitted
`risk_allocation_pct` was 0.5.

There is already a per-symbol check on `TargetPosition` itself
(`models._flag_risk_narrative_mismatch`), but it reads each position's OWN
`thesis`. It structurally cannot see `sizing_logic`, which lives on
`ReasoningChain` and speaks about many symbols at once -- which is exactly
where the filed defect sat. This module closes that surface.

RECORD AND SURFACE -- IT DOES NOT BLOCK, AND THAT WAS MEASURED, NOT ASSUMED.
A mismatch is written to the evidence stream and reported exactly as found. No
number is corrected toward the other one and no target is refused.

The obvious argument for blocking is that a size the seat's own reasoning
contradicts is fabricated rather than degraded. Replaying the one filed case
against the production database on 2026-09-26 shows why that argument does not
hold HERE. In run `run-601011e0` the model's raw response contains no 0.5 at
all: it asked for RSG 2.5% and ZS 1.75%, exactly as the narrative says. The
0.5 was written afterwards by the desk's own deterministic sub-floor size cap
(`PortfolioManagerAgent._apply_subfloor_catalyst_rule` as it stood that day --
RSG was a RANGE setup at reward:risk 0.81), whose own log line calls the result
"Deterministic, not PM inconsistency". RSG was then traded, correctly, at the
capped size. A gate that refused the name on this mismatch would have killed a
legitimately sized trade over a NARRATION LAG -- the narrative is written
before the mechanical adjustment runs, which is the same ordering problem
`PortfolioDecision.constructor_dropped` exists to explain to the Risk Manager.

So the disagreement is real and worth surfacing -- an owner-facing story that
says 2.5% beside a position taken at 0.5% is a false statement about a live
trade -- but it is not by itself evidence that the emitted number is wrong,
and it cannot be allowed to veto the number. Raising this to a block needs a
way to tell "the seat contradicted itself" from "a deterministic rule moved
the number after the seat wrote about it", which nothing here has.

It reuses the exact narrow risk-% matcher the `TargetPosition` check uses, so
both surfaces agree on what counts as "an explicit risk claim". It does NOT
reuse that check's tolerance -- see `_half_ulp`.

FEASIBILITY / FALSE-POSITIVE POSTURE. `sizing_logic` is free prose, so a
reliable check has to refuse to guess. Two guards keep it honest:

  1. A risk-% number is only a claim when the narrow `_RISK_PCT_CLAIM_PATTERN`
     (shared with `models`) says so -- it anchors on the word "risk" next to
     the number, so a target weight, a stop distance, a price move or a macro
     figure is never read as a risk allocation.

  2. A claim is only paired with a symbol when BOTH sit in the same sentence
     AND that sentence carries exactly ONE distinct risk-% value. A sentence
     naming two different risk percentages next to two symbols cannot be paired
     with confidence, so it is skipped (a MISS), never flagged. This is the
     "honest minimal" reading item 163 asks for: when in doubt, stay quiet.

The measured case ("RSG and AAPL get 2.5% risk each") is one sentence, one
distinct value (2.5%), two named symbols -- so RSG (field 0.5%) is flagged and
AAPL (field 2.5%) is not. Re-confirmed 2026-09-26 by replaying that stored run
read-only out of the production database: its `portfolio_manager` / `target`
evidence rows carry RSG 0.5 and ZS 0.5 while the same run's stored
`sizing_logic` narrates 2.5% and 1.75%. (ZS is a deliberate MISS -- its clause
never puts the word "risk" beside the number.)
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from src.models import _RISK_PCT_CLAIM_PATTERN

logger = logging.getLogger(__name__)

def _explicit_risk_pct_claim_texts(text: str) -> list[str]:
    """Every explicit risk-% claim as the seat WROTE it, digits and all.

    The float-returning `models._explicit_risk_pct_claims` throws away the
    written form, and the written form is the whole point here: "2.5" and
    "2.50" are the same value stated to different precision, and the
    precision is what decides whether a difference is a real disagreement or
    a rounding of the same number (see `_half_ulp`).
    """
    out: list[str] = []
    for match in _RISK_PCT_CLAIM_PATTERN.finditer(text or ""):
        out.append(next(g for g in match.groups() if g is not None))
    return out


def _half_ulp(field_pct: float) -> Decimal | None:
    """Half the last place of `risk_allocation_pct` AS IT WAS EMITTED.

    NOT a chosen tolerance -- there is no number to choose here. A decimal
    figure carries its own precision: a field emitted as ``0.5`` asserts a
    tenth-of-a-point value, so every quantity in [0.45, 0.55) rounds to it
    and is the SAME number said differently, while anything outside that
    band is a different number. Half the unit of the field's own last place
    is exactly that band's half-width, so it is read off the emitted value
    rather than picked.

    This deliberately replaces the earlier borrowed tolerance
    (`models.RISK_NARRATIVE_MISMATCH_TOLERANCE_PCT`, one
    `min_position_risk_pct` increment = 0.5pp). That figure is a risk-BUDGET
    floor, not a statement about how precisely the field is written, and at
    0.5pp it declared prose "2.5% risk" and an emitted 2.0 to be the same
    risk -- a 25% sizing difference passing unseen. The per-symbol
    `TargetPosition.thesis` check still uses its own constant; that surface
    is not touched here.

    Returns ``None`` when the emitted value has no readable decimal form, in
    which case the pair is left alone rather than judged on a guess.
    """
    try:
        emitted = Decimal(str(field_pct))
    except (InvalidOperation, ValueError):
        return None
    exponent = emitted.as_tuple().exponent
    if not isinstance(exponent, int):
        return None
    return Decimal(1).scaleb(exponent) / Decimal(2)


#: Split prose into sentences WITHOUT breaking a decimal: the delimiter must be
#: followed by whitespace (or be a line break), so the "." inside "2.5%" -- a
#: dot followed by a digit -- never splits a sentence.
_SENTENCE_SPLIT = re.compile(r"(?<=[.;!?])\s+|\n+")


@dataclass(frozen=True)
class SizingNarrativeMismatch:
    """One symbol whose `sizing_logic` prose risk-% contradicts its field."""

    symbol: str
    prose_pct: float
    field_pct: float
    detail: str


def _authoritative_risk_by_symbol(targets: list[Any]) -> dict[str, float]:
    """Map UPPERCASE symbol -> emitted `risk_allocation_pct`.

    Only targets that actually carry a `risk_allocation_pct` are included: a
    legacy `target_weight_pct`-only target has no authoritative risk number to
    check the prose against, exactly as the `TargetPosition` check skips it.
    """
    out: dict[str, float] = {}
    for t in targets:
        symbol = getattr(t, "symbol", None)
        risk_pct = getattr(t, "risk_allocation_pct", None)
        if symbol and risk_pct is not None:
            out[str(symbol).upper()] = float(risk_pct)
    return out


def check_sizing_narrative(decision: Any) -> list[SizingNarrativeMismatch]:
    """Flag symbols whose `sizing_logic` narrative states a risk % that
    materially differs from the symbol's own emitted `risk_allocation_pct`.

    Pure and side-effect-free apart from a warning log per finding (the same
    surface the `TargetPosition` check uses). Returns the findings so a caller
    can also record them to the evidence stream. Never raises on shape: a
    missing chain, empty prose or no priced targets returns ``[]``.
    """
    chain = getattr(decision, "reasoning_chain", None)
    text = getattr(chain, "sizing_logic", "") if chain is not None else ""
    if not text or not text.strip():
        return []

    field_by_symbol = _authoritative_risk_by_symbol(
        list(getattr(decision, "targets", None) or [])
    )
    if not field_by_symbol:
        return []

    findings: list[SizingNarrativeMismatch] = []
    flagged: set[str] = set()

    for sentence in _SENTENCE_SPLIT.split(text):
        # Keyed by VALUE, not by spelling: "2.5" and "2.50" are one claim.
        distinct = {
            Decimal(t): t for t in _explicit_risk_pct_claim_texts(sentence)
        }
        # 0 claims -> nothing to check. 2+ distinct claims in one sentence ->
        # cannot pair a value to a symbol with confidence, so MISS rather than
        # false-flag. Only the unambiguous single-value sentence is checked.
        if len(distinct) != 1:
            continue
        prose_text = next(iter(distinct.values()))
        prose_pct = float(prose_text)

        for symbol, field_pct in field_by_symbol.items():
            if symbol in flagged:
                continue
            # Whole-word, case-sensitive: PM sizing prose names the real ticker
            # in caps, and requiring the uppercase token keeps a lowercase
            # English word from being read as a symbol (a MISS, never a false
            # flag).
            if not re.search(rf"\b{re.escape(symbol)}\b", sentence):
                continue
            half_ulp = _half_ulp(field_pct)
            if half_ulp is None:
                continue
            gap = abs(Decimal(prose_text) - Decimal(str(field_pct)))
            if gap > half_ulp:
                detail = (
                    f"{symbol}: sizing_logic narrative states risk "
                    f"{prose_pct:g}% but emitted risk_allocation_pct="
                    f"{field_pct:g}%"
                )
                findings.append(
                    SizingNarrativeMismatch(symbol, prose_pct, field_pct, detail)
                )
                flagged.add(symbol)
                logger.warning("sizing_narrative_mismatch: %s", detail)

    return findings
