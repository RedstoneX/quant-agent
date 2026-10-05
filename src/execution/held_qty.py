"""Broker-held quantity reads and the session lock, shared BELOW scale_in.

Lifted verbatim from ``src/execution/scale_in.py`` so that ``broker`` (which
sizes the post-fill stop with ``cover_qty_for_rearm``) and ``coverage_watchdog``
(which asks ``trading_session_lock_held`` / ``list_open_entry_ids``) no longer
import ``scale_in`` while ``scale_in`` imports them back. Nothing here knows
about the add sequence, the WAL, or the broker class; ``scale_in`` re-exports
every name so call sites and patch targets there keep working.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from src.execution.scale_in_loud import record_scale_in

logger = logging.getLogger(__name__)

_SESSION_LOCK_DIR = Path.home() / ".cache" / "quant-agent" / "active-session.lock"


def held_signed_qty(positions: list | None, symbol: str) -> float:
    """Broker-signed quantity for `symbol` from a positions list. 0 if absent."""
    want = str(symbol or "").upper()
    for pos in positions or []:
        if str(getattr(pos, "symbol", "") or "").upper() != want:
            continue
        try:
            return float(getattr(pos, "qty", 0) or 0)
        except (TypeError, ValueError):
            return 0.0
    return 0.0



def broker_position_qty(broker: Any, symbol: str) -> float | None:
    """Signed broker qty for `symbol`. 0 if flat. None if the broker could not be asked."""
    try:
        positions = broker.get_positions()
    except Exception as exc:  # noqa: BLE001
        record_scale_in(broker, "broker_position_qty", exc, symbol=symbol)
        return None
    else:
        record_scale_in(broker, "broker_position_qty")
    if not isinstance(positions, list):
        return None
    return held_signed_qty(positions, symbol)


def cover_qty_for_rearm(
    broker: Any, *, symbol: str, filled_qty: float, held_qty_before: float,
) -> float:
    """Quantity the post-fill protective stop must cover (magnitude).

    Broker full position is the authority (partial fill of the add must
    not size the stop to the add alone). If the broker cannot be asked,
    fall back to filled + held-before — the two quantities we already
    measured — rather than inventing a third number.

    Both quantities are taken as MAGNITUDES. `held_qty_before` is the
    broker-SIGNED quantity, which is NEGATIVE for a short. The old fallback
    ``filled + max(0.0, held)`` clamped that negative held-before to 0 and
    so covered only the ADD, leaving the ENTIRE existing short leg naked —
    the adversary's finding. `abs(filled) + abs(held)` covers the full
    enlarged position on either side; longs are unaffected because their
    filled and held are already >= 0.
    """
    current = broker_position_qty(broker, symbol)
    if current is not None:
        return max(0.0, abs(float(current)))
    try:
        filled = float(filled_qty or 0)
    except (TypeError, ValueError):
        filled = 0.0
    try:
        held = float(held_qty_before or 0)
    except (TypeError, ValueError):
        held = 0.0
    logger.warning(
        "scale-in: broker qty unreadable for %s — covering |filled| (%.4f) "
        "+ |held-before| (%.4f)", symbol, abs(filled), abs(held),
    )
    return abs(filled) + abs(held)


def list_open_entry_ids(broker: Any, symbol: str) -> list[str]:
    """Working non-stop entry order ids for `symbol`. Empty on failure."""
    lister = getattr(broker, "list_open_entry_order_ids", None)
    if callable(lister):
        try:
            ids = lister(symbol)
        except Exception as exc:  # noqa: BLE001
            record_scale_in(broker, "list_open_entry_ids", exc, symbol=symbol)
            return []
        else:
            record_scale_in(broker, "list_open_entry_ids")
        if isinstance(ids, list):
            return [str(i) for i in ids if i]
        return []
    return []


def trading_session_lock_held() -> bool:
    """True only when the wrapper's session lock directory is present.

    Used by the coverage watchdog so it does not ADD a stop during a live
    cancel-confirm-buy window. Absence is treated as no session — crash
    recovery may rearm. Unknown/OS error is also treated as no session:
    failing to repair after a crash is worse than a wash-trade reject of
    an add that then restores.
    """
    try:
        return _SESSION_LOCK_DIR.is_dir()
    except OSError:
        return False

