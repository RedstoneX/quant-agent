"""Fixed translation from Nasdaq screener sector names to the desk's canonical sector names.

One table, no guessing. The desk speaks the Yahoo taxonomy (``src.sector_vocab``);
Nasdaq's listing speaks its own. Lookup is exact, case-insensitive on the Nasdaq name.

Nasdaq name            -> desk name
  Technology             -> Technology
  Finance                -> Financial Services
  Health Care            -> Healthcare
  Telecommunications     -> Communication Services
  Consumer Discretionary -> Consumer Cyclical
  Consumer Staples       -> Consumer Defensive
  Energy                 -> Energy
  Industrials            -> Industrials
  Utilities              -> Utilities
  Basic Materials        -> Basic Materials
  Real Estate            -> Real Estate
  Miscellaneous          -> Unknown   (NO honest match; deliberately unclassified)

UNCLASSIFIED ("Unknown", the desk's existing explicit unknown value) is returned, by name,
for "Miscellaneous". A name not in this table is not translated here at all
(``nasdaq_to_desk`` returns None) so the caller still falls through to its normal Unknown path.
"""

from __future__ import annotations

UNCLASSIFIED = "Unknown"

NASDAQ_TO_DESK: dict[str, str] = {
    "technology": "Technology",
    "finance": "Financial Services",
    "health care": "Healthcare",
    "telecommunications": "Communication Services",
    "consumer discretionary": "Consumer Cyclical",
    "consumer staples": "Consumer Defensive",
    "energy": "Energy",
    "industrials": "Industrials",
    "utilities": "Utilities",
    "basic materials": "Basic Materials",
    "real estate": "Real Estate",
    "miscellaneous": UNCLASSIFIED,
}


def nasdaq_to_desk(raw: str | None) -> str | None:
    """Desk sector for a Nasdaq sector name; UNCLASSIFIED for Miscellaneous; None if not a Nasdaq name."""
    if not raw:
        return None
    return NASDAQ_TO_DESK.get(str(raw).strip().lower())
