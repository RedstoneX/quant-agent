"""What counts as a usable protective stop price — ONE definition.

Lifted out of `src/execution/stop_records.py` unchanged (2026-10-02) so the
ledger can refuse an entry row with no stop WITHOUT importing the broker
seam (`scripts/import_graph.py`, rule "broker-seam"). `stop_records`
re-exports every name below, so every existing importer is untouched.
"""

from __future__ import annotations

import math
from typing import Any

STOP_ABSENT = "absent"
STOP_UNUSABLE = "unusable"
STOP_USABLE = "usable"


def classify_stop_price(value: Any) -> tuple[str, float]:
    """Name what a caller is holding: an absent stop, an unusable one, or a price.

    Returns ``(STOP_ABSENT | STOP_UNUSABLE | STOP_USABLE, price)`` where
    `price` is the usable float and 0.0 otherwise.

    * ``STOP_ABSENT``   — nothing was supplied (None, or an empty string
      from a JSON/DB round-trip). The caller decides whether a stopless
      order is legal on its path; only the cash-sweep park says yes.
    * ``STOP_UNUSABLE`` — something WAS supplied and it cannot be a stop:
      zero, negative, NaN, ±Inf, or unparseable. Never silently treated
      as absence.
    * ``STOP_USABLE``   — a finite, positive price.
    """
    if value is None:
        return STOP_ABSENT, 0.0
    if isinstance(value, str) and not value.strip():
        return STOP_ABSENT, 0.0
    try:
        price = float(value)
    except (TypeError, ValueError):
        return STOP_UNUSABLE, 0.0
    if not math.isfinite(price) or price <= 0:
        return STOP_UNUSABLE, 0.0
    return STOP_USABLE, price


def entry_stop_for_insert(action: Any, symbol: Any, stop_loss: Any, opening: Any) -> float | None:
    """The pinned entry stop for a new trade row — and the refusal.

    An entry the desk itself opened can NEVER be recorded without the level
    it decided to protect it at. `stop_loss REAL DEFAULT 0` in the schema,
    plus a `float(stop_loss or 0)` coercion at the write, used to accept one
    silently; the coverage repair then found a held position with no
    reviewed level and left it unprotected (`no_recorded_stop`). Opening
    actions now raise; every other action keeps the old coercion, including
    the cash-sweep park, which is legitimately stopless.
    """
    state, price = classify_stop_price(stop_loss)
    if str(action or "").strip().upper() in opening and state != STOP_USABLE:
        raise ValueError(
            f"refusing to record a {action} entry for {symbol}: "
            f"stop_loss={stop_loss!r} is not a usable protective level. An "
            f"entry the desk opened must carry the stop it was sized "
            f"against, or the coverage repair has nothing to restore."
        )
    return price if state == STOP_USABLE else None
