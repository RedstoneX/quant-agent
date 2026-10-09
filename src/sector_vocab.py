"""Sector taxonomy and stance vocabulary shared by models and the seat heal.

Lives below `src.models.base` and `src.seat_heal` so the heal can speak the
same sector names and directions without importing the model base.
"""

# yfinance sector taxonomy (matches what broker._get_sector returns).
# "Broad" covers index ETFs (SPY/QQQ/IWM/DIA) that have no single sector tag.
_ALLOWED_SECTORS = (
    "Technology",
    "Financial Services",
    "Healthcare",
    "Consumer Cyclical",
    "Consumer Defensive",
    "Energy",
    "Industrials",
    "Communication Services",
    "Utilities",
    "Basic Materials",
    "Real Estate",
    "Broad",
)

# Common LLM-emitted aliases → canonical name. Applied before the Literal check
# so a single bad label doesn't discard the whole MacroAnalysis.
_SECTOR_ALIASES = {
    "tech": "Technology",
    "technology": "Technology",
    "financials": "Financial Services",
    "financial": "Financial Services",
    "banks": "Financial Services",
    "consumer discretionary": "Consumer Cyclical",
    "consumer staples": "Consumer Defensive",
    "materials": "Basic Materials",
    "comm services": "Communication Services",
    "communication": "Communication Services",
    "telecom": "Communication Services",
    "reits": "Real Estate",
    "real-estate": "Real Estate",
    "index": "Broad",
    "broad market": "Broad",
    "etf": "Broad",
}


# The macro analyst speaks TILTS (overweight/underweight); every consumer of a
# sector stance — MacroStore's persisted snapshot, the evening thesis-health
# block, `PositionSnapshot.macro_sector_tailwind` below, and the PM's evidence
# registry — speaks DIRECTIONS (bullish/bearish). One macro view described in
# two vocabularies is how a provenance mismatch gets debugged twice, so the
# translation lives here, next to the Literal that defines the tilt side of it,
# and every consumer imports it rather than re-spelling the pairs.
SECTOR_STANCE_TO_DIRECTION: dict[str, str] = {
    "overweight": "bullish",
    "neutral": "neutral",
    "underweight": "bearish",
}

# Directions are idempotent under the map: a stance that already arrived
# normalized (MacroStore's shape) must survive a second pass unchanged.
SECTOR_DIRECTIONS: frozenset[str] = frozenset(SECTOR_STANCE_TO_DIRECTION.values())


def normalize_sector_stance(value) -> str | None:
    """overweight|underweight|neutral (or a direction already) → direction.

    Returns None for anything unrecognized so callers can drop it rather
    than propagate a stance no validator will accept.
    """
    stance = str(value or "").strip().lower()
    if stance in SECTOR_DIRECTIONS:
        return stance
    return SECTOR_STANCE_TO_DIRECTION.get(stance)


# Reverse of SECTOR_STANCE_TO_DIRECTION for restoring the live model shape
# from MacroStore's {sector: bullish|neutral|bearish} snapshot. Not an
# invented stance — it is the same map, run backwards.
# Built in one expression and never mutated: the reverse map fills the gaps
# (first stance wins, as setdefault would), the explicit entries override.
_DIRECTION_TO_STANCE: dict[str, str] = {
    **{_d: _s for _s, _d in reversed(SECTOR_STANCE_TO_DIRECTION.items())},
    "bullish": "overweight",
    "bearish": "underweight",
    "neutral": "neutral",
}
