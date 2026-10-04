"""The yfinance<->Alpaca class-share spelling, at the broker's edge.

Lifted verbatim out of ``stop_place.py`` (which was over the size ceiling and
may not grow) so the stop-leg recorder could be wired in. Two pure functions
with no collaborators; ``stop_place`` and ``src.execution.broker`` re-export
both, so every existing importer and patch target still resolves here.
"""
from __future__ import annotations

import re


def _alpaca_symbol(symbol: str) -> str:
    """Translate the universe's yfinance class-share spelling at Alpaca's edge.

    BRK-B/BF-B are valid yfinance symbols while Alpaca expects BRK.B/BF.B.
    Only the terminal one-letter class suffix is translated; ordinary hyphenated
    symbols are left untouched instead of applying a broad, unsafe replacement.
    """
    value = str(symbol).strip().upper()
    return re.sub(r"^([A-Z]+)-([A-Z])$", r"\1.\2", value)


def _internal_symbol(symbol: str) -> str:
    """Map Alpaca class-share spelling back to QAMC/yfinance canonical form."""
    value = str(symbol).strip().upper()
    return re.sub(r"^([A-Z]+)\.([A-Z])$", r"\1-\2", value)
