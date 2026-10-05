"""Explicit risk-% claims in thesis prose, and the precision band that decides
whether a claim and the emitted field are the same number.

Lifted out of `portfolio.py` unchanged; `src.models` re-exports every name.
"""

import re
from decimal import Decimal, InvalidOperation

#: Matches an EXPLICIT risk-allocation percentage claim in free prose:
#: "risking 2%", "risk of 2%", "2% risk". Deliberately narrow on purpose
#: (item 163) -- it anchors on the word "risk"/"risking"/"risks" sitting
#: directly next to the number (only "up to" may sit between them), so it
#: does not fire on an incidental percentage elsewhere in the thesis: a
#: target weight, a stop distance, a price gain, a macro figure. The
#: `(?!-)` after the third alternative's "risk" excludes a hyphenated
#: compound right after it ("risk-adjusted", "risk-reward") so "12%
#: risk-adjusted return" is not misread as a 12% risk claim. Where this
#: matcher cannot tell a risk-% claim from another percentage with
#: reasonable confidence, it is built to MISS the claim rather than
#: false-flag one -- see tests/test_models.py for the cases this covers.
_RISK_PCT_CLAIM_PATTERN = re.compile(
    r"\brisk(?:ing|s)?\s+(?:up\s+to\s+)?(\d+(?:\.\d+)?)\s*%"
    r"|\brisk\s+of\s+(\d+(?:\.\d+)?)\s*%"
    r"|(\d+(?:\.\d+)?)\s*%\s+risk(?!-)\b",
    re.IGNORECASE,
)


def _explicit_risk_pct_claim_texts(text: str) -> list[str]:
    """Every explicit risk-% claim AS WRITTEN, digits and trailing zeros kept.

    The written form is load-bearing: "2.5" and "2.50" are the same value
    stated to different precision, and the precision is what decides whether
    a gap is a real disagreement or the same number said differently (see
    `risk_pct_half_ulp`). Parsing to `float` first throws that away.
    """
    return [
        next(g for g in m.groups() if g is not None)
        for m in _RISK_PCT_CLAIM_PATTERN.finditer(text or "")
    ]


def _explicit_risk_pct_claims(text: str) -> list[float]:
    """Every explicit risk-percentage claim `_RISK_PCT_CLAIM_PATTERN` finds."""
    return [float(raw) for raw in _explicit_risk_pct_claim_texts(text)]


def risk_pct_half_ulp(field_pct: float) -> Decimal | None:
    """Half the last place of a risk-% figure AS IT WAS EMITTED.

    NOT a chosen tolerance -- there is no number to choose here. A decimal
    figure carries its own precision: a field emitted as ``0.5`` asserts a
    tenth-of-a-point value, so every quantity in [0.45, 0.55) rounds to it
    and is the SAME number said differently, while anything outside that
    band is a different number. Half the unit of the field's own last place
    is exactly that band's half-width, so it is read off the emitted value
    rather than picked.

    This is the single definition of the question "are the prose number and
    the emitted number the same number?". Both risk-narrative surfaces ask
    it -- this module's per-symbol `TargetPosition.thesis` check and
    `src/risk_narrative_check.py`'s whole-book `sizing_logic` check -- and
    they must not answer it differently about the same pair of figures.

    It replaces the earlier ABSOLUTE constant here (one
    `RiskConfig.min_position_risk_pct` increment, 0.5 percentage points).
    That figure is a risk-BUDGET floor, not a statement about how precisely
    the field is written, and because it is absolute while the quantity it
    judges is itself a percentage, its slack grows without limit in relative
    terms as the position shrinks: it called prose "2.5% risk" beside an
    emitted 2.0 the same risk (a 25% sizing difference) and prose "0.6%
    risk" beside an emitted 0.2 the same risk (a 3x sizing difference), both
    measured.

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
