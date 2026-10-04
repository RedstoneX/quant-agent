"""One durable row per no-ATR structural stop placement (board item 90).

`ConstructorConfig.structural_stop_buffer_pct` (0.5%) is `arbitrary`: it is
owner appetite, not doctrine, and the ledger's settlement route for it asks
for the distribution of what that flat percentage actually produced at
placement — the level the stop was read from, the half-width of the price
cluster that formed that level, and the distance the buffer put the stop at.

An earlier attempt kept those as counts in memory. The accumulator guard
refused that, correctly: a running total is state the desk has to be trusted
to maintain, and it cannot be re-read, re-sliced or audited afterwards. This
module stores NOTHING. It appends one evidence row per placement decision and
every total is computed from the rows at read time.

Three states stay distinguishable, which is the whole point:

  * no row at all          -- the no-ATR branch was NEVER REACHED
  * a row with outcome     -- the branch ran and placed a stop (`level`
    `level`/`prior_bar`       means tier 1, `prior_bar` means tier 2)
  * a row with outcome     -- the branch ran and found no usable structure
    `none`

Writing a row can never break a placement: every path here is wrapped and a
failure logs a traceback and returns.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from src.storage.event_journal import DatabaseEventJournal

logger = logging.getLogger(__name__)

__all__ = [
    "NO_ATR_BUFFER_KIND",
    "OUTCOME_LEVEL",
    "OUTCOME_NONE",
    "OUTCOME_PRIOR_BAR",
    "buffer_row",
    "placement_recorder",
    "record_no_atr_buffer",
]

NO_ATR_BUFFER_KIND = "no_atr_structural_buffer"

OUTCOME_LEVEL = "level"
OUTCOME_PRIOR_BAR = "prior_bar"
OUTCOME_NONE = "none"


def _zone_halfwidth(analysis: Any, level: float | None) -> tuple[float | None, bool]:
    """The measured half-width of the cluster that formed `level`.

    Returns `(halfwidth, measured)`. `measured` is False when the analysis
    carries no span for that level, which is itself the finding the ledger
    row waits on: a half-width that is routinely absent cannot replace the
    flat percentage.
    """
    if level is None:
        return None, False
    zones = getattr(analysis, "computed_level_zones", None)
    if not isinstance(zones, dict):
        return None, False
    span = zones.get(level)
    if not isinstance(span, (tuple, list)) or len(span) != 2:
        return None, False
    try:
        low = float(span[0])
        high = float(span[1])
    except (TypeError, ValueError):
        return None, False
    if not high >= low:
        return None, False
    return (high - low) / 2.0, True


def buffer_row(
    *,
    outcome: str,
    symbol: str | None,
    is_short: bool,
    entry_price: float,
    buffer_pct: float,
    level: float | None,
    stop_price: float | None,
    touches: Any,
    min_touches: Any,
    candidate_levels: int,
    halfwidth: float | None,
    halfwidth_measured: bool,
) -> dict:
    """The payload for ONE placement decision. Pure; holds no state."""
    distance = None
    if stop_price is not None and entry_price:
        distance = abs(float(entry_price) - float(stop_price))
    buffer_distance = None
    if level is not None and stop_price is not None:
        buffer_distance = abs(float(level) - float(stop_price))
    return {
        "outcome": outcome,
        "symbol": symbol,
        "direction": "short" if is_short else "long",
        "entry_price": float(entry_price) if entry_price else None,
        "buffer_pct": float(buffer_pct),
        "level": float(level) if level is not None else None,
        "stop_price": float(stop_price) if stop_price is not None else None,
        "stop_distance": distance,
        "buffer_distance": buffer_distance,
        "level_zone_halfwidth": halfwidth,
        "level_zone_halfwidth_measured": bool(halfwidth_measured),
        "level_touches": touches,
        "min_touches_required": min_touches,
        "candidate_levels": int(candidate_levels),
    }


def record_no_atr_buffer(
    db: Any,
    analysis: Any,
    *,
    outcome: str,
    is_short: bool,
    entry_price: float,
    buffer_pct: float,
    level: float | None = None,
    stop_price: float | None = None,
    touches: Any = None,
    min_touches: Any = None,
    candidate_levels: int = 0,
) -> None:
    """Append one row. Never raises — a recording failure is not a placement failure."""
    try:
        if db is None:
            return
        halfwidth, measured = _zone_halfwidth(analysis, level)
        payload = buffer_row(
            outcome=outcome,
            symbol=getattr(analysis, "symbol", None),
            is_short=is_short,
            entry_price=entry_price,
            buffer_pct=buffer_pct,
            level=level,
            stop_price=stop_price,
            touches=touches,
            min_touches=min_touches,
            candidate_levels=candidate_levels,
            halfwidth=halfwidth,
            halfwidth_measured=measured,
        )
        # The journal port directly, NOT the `_persist_evidence` shim in
        # `src.pipeline_stages`: that module pulls in the whole agent stack
        # and importing it from inside the constructor -- even lazily, which
        # the layering guard walks too -- closes a real import cycle.
        DatabaseEventJournal(db).persist_evidence(
            run_id=getattr(analysis, "run_id", None),
            agent_name="portfolio_constructor",
            kind=NO_ATR_BUFFER_KIND,
            scope="symbol",
            symbol=payload["symbol"],
            decision_id=None,
            evidence_json=json.dumps(payload, sort_keys=True),
        )
    except Exception:  # noqa: BLE001
        logger.exception("no-ATR structural buffer row not recorded")


def placement_recorder(
    read_db, analysis, is_short, entry_price, buffer_pct, min_touches, levels,
):
    """A one-call row writer bound to ONE no-ATR derivation.

    Positional deliberately: the caller is a single site inside
    `StopRules._derive_structural_stop_no_atr`, which the file-size ratchet
    holds at its current width, so the wiring is one line there and the
    naming lives here.

    `read_db` is `StopRules._read_db` -- the owner's zero-argument db-handle
    callable, or None when no database was wired. It is read per placement
    and never cached, and a failure to read it is a recording failure, never
    a placement failure. `levels` is how many candidate levels the tier-1
    scan had to choose from, the denominator for "how often is there any
    structure at all".
    """
    def _write(outcome, level=None, stop_price=None, touches=None) -> None:
        db = None
        try:
            db = read_db() if read_db is not None else None
        except Exception:  # noqa: BLE001
            logger.exception("no-ATR buffer row could not read the db handle")
            return
        record_no_atr_buffer(
            db, analysis, outcome=outcome, is_short=is_short,
            entry_price=entry_price, buffer_pct=buffer_pct, level=level,
            stop_price=stop_price, touches=touches, min_touches=min_touches,
            candidate_levels=levels,
        )

    return _write
