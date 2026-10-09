"""Named macro-series levels for thesis_invalid_if, lifted verbatim from exit_guard.py."""

from __future__ import annotations

import re


# ---------------------------------------------------------------------------
# Named macro-series levels — the third checkable shape (item 99, 2026-09-30)
# ---------------------------------------------------------------------------
#
# WHY THIS EXISTS. The two shapes above were measured when `tech_analyst` was
# the only seat that stated a falsifier at all, and a technical falsifier is
# always about price. Four more seats — news, earnings, macro, smart_money —
# now state their own, and their prompts' own worked examples are not price:
# `config/prompts/macro_analyst.md` ships "HY OAS widens back above 420bps".
# Under the two price shapes that comes back UNPARSEABLE, which is a
# falsifier in name only.
#
# This adds NO new comparison and NO tolerance. It is the SAME strict
# above/below test already applied to price and to an MA, pointed at a level
# the desk already fetches every run: the FRED series in
# `src/data/macro.py::MacroCoverage.get_macro_summary()`, the same numbers the
# macro seat is shown when it writes the condition. Nothing new is computed,
# fetched or derived here — the caller passes values it already holds, exactly
# as it already does for `current_price` and the MAs.
#
# UNITS ARE NOT GUESSED. A spread series is stored in basis points and a rate
# series in percent. bps<->% is an exact definitional conversion (1% = 100bps),
# so it is applied; anything else is refused. For a bps series a bare number
# with no unit ("HY OAS above 420" — 420bps? 420%?) is UNPARSEABLE rather than
# assumed, and no threshold is invented to disambiguate it.
#
# A named macro series WINS over the bare-price shape and is checked before
# it. That also closes a latent mis-comparison: "HY OAS above 420.5bps" would
# otherwise have matched `_DIRECTION_PRICE_RE` and been compared against the
# stock's own price, producing a confident, wrong answer.

#: Canonical macro key -> the unit the desk stores that series in.
#: "bps" = basis points, "pct" = percent, "index" = bare index points.
#: The caller maps `get_macro_summary()` onto these keys; this module never
#: reaches for macro data itself, for the same reason it never fetches price.
_MACRO_SERIES_UNITS: dict[str, str] = {
    "vix": "index",
    "dollar_index": "index",
    "credit_spread": "bps",
    "ig_credit_spread": "bps",
    "fed_funds_rate": "pct",
    "inflation": "pct",
    "unemployment": "pct",
    "treasury_10y": "pct",
    "treasury_2y": "pct",
}

#: How each series is actually written in English by the seats that cite it.
#: Deliberately narrow: only spellings that cannot mean anything else. An
#: unrecognised macro-sounding phrase stays UNPARSEABLE rather than being
#: pattern-matched optimistically. Order matters: the investment-grade
#: spelling is tried before the high-yield one so "IG spread" cannot be
#: swallowed by a looser pattern.
_MACRO_SERIES_RE: tuple[tuple[str, "re.Pattern[str]"], ...] = (
    ("vix", re.compile(r"\bVIX\b", re.IGNORECASE)),
    ("dollar_index", re.compile(r"\b(?:DXY|dollar\s+index)\b", re.IGNORECASE)),
    (
        "ig_credit_spread",
        re.compile(
            r"\b(?:IG\s+(?:OAS|credit\s+spread|spread)"
            r"|investment[\s-]grade\s+(?:OAS|spread))\b",
            re.IGNORECASE,
        ),
    ),
    (
        "credit_spread",
        re.compile(
            r"\b(?:HY\s+(?:OAS|credit\s+spread|spread)"
            r"|high[\s-]yield\s+(?:OAS|spread))\b",
            re.IGNORECASE,
        ),
    ),
    ("fed_funds_rate", re.compile(r"\bfed\s+funds(?:\s+rate)?\b", re.IGNORECASE)),
    ("inflation", re.compile(r"\bcore\s+CPI\b", re.IGNORECASE)),
    ("unemployment", re.compile(r"\b(?:UNRATE|unemployment\s+rate)\b", re.IGNORECASE)),
    ("treasury_10y", re.compile(r"\b10[\s-]?(?:y|yr|year)\b", re.IGNORECASE)),
    ("treasury_2y", re.compile(r"\b2[\s-]?(?:y|yr|year)\b", re.IGNORECASE)),
)

#: A number with an optional explicit unit suffix. Integers ARE allowed here
#: (unlike the bare-price shapes) because the named series has already removed
#: the ambiguity that the decimal requirement existed to guard against.
_MACRO_NUMBER_RE = re.compile(
    r"([\d,]+(?:\.\d+)?)\s*(bps|bp|%|percent)?",
    re.IGNORECASE,
)


def _macro_threshold_in_series_unit(
    text: str,
    series_unit: str,
    series_span: "tuple[int, int]",
) -> "tuple[float | None, str]":
    """The stated threshold converted into `series_unit`, or (None, why).

    The number is looked for AFTER the series name first and only then
    before it, because several series names contain a digit of their own
    ("10y", "2y") and that digit is not a threshold.

    Only the exact bps<->percent definition is applied. No other conversion,
    no tolerance, and no assumption about a missing unit on a bps series.
    """
    start, end = series_span
    for window in (text[end:], text[:start]):
        m = _MACRO_NUMBER_RE.search(window)
        if m is None:
            continue
        value = _clean_number(m.group(1))
        raw_unit = (m.group(2) or "").lower()
        unit = {"bp": "bps", "bps": "bps", "%": "pct", "percent": "pct"}.get(raw_unit)
        if series_unit == "index":
            if unit is not None:
                return None, (f"index-valued series quoted in '{raw_unit}' — not converting")
            return value, ""
        if series_unit == "bps":
            if unit == "bps":
                return value, ""
            if unit == "pct":
                return value * 100.0, ""
            return None, ("spread threshold has no unit — 'bps' or '%' must be stated, the unit is not being guessed")
        if unit == "bps":
            return value / 100.0, ""
        return value, ""
    return None, "no numeric threshold found alongside the macro series"


def _clean_number(raw: str) -> float:
    return float(raw.replace(",", ""))
