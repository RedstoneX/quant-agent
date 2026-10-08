"""The retired cash-park vehicle: exempting it from the stop audit and selling it.

Former `TradingPipeline._retired_cash_park_symbol` / `_release_retired_cash_park`
(bodies moved here unchanged). The cash sweeper is HANDED IN as a getter
rather than imported: `src.execution` is the broker seam and the set of
modules reaching it must not widen (`scripts/import_graph.LAYER_RULES`).

The getter is called on EVERY use, never captured once. Tests build the
owner via ``__new__`` and assign ``cash_sweeper`` afterwards, and the owner
may replace it; a snapshot taken at construction would silently keep the
original forever. The getter also owns the type gate (a MagicMock or None
sweeper must read as "no sweeper"), because the type to gate against lives
behind the broker seam this module may not import.
"""
from __future__ import annotations

import logging
from typing import Callable
from src.sentinel.counted import record_swallowed_here

logger = logging.getLogger(__name__)

# Returns the owner's real CashSweeper, or None when absent / not a real one.
SweeperGetter = Callable[[], object | None]


def retired_cash_park_symbol(get_sweeper: SweeperGetter) -> str | None:
    """The configured sweep vehicle when the sweep is DISABLED, else None.

    Owner mandate 2026-09-17 turned the sweep off. A vehicle bought
    before that is still a deliberately stopless holding until
    `release_retired_cash_park` sells it, so the stop-coverage audit
    must keep exempting it rather than raising a naked-position banner
    (its opening row is SWEEP_BUY, so the repair could not rebuild a
    stop anyway).
    """
    sweeper = get_sweeper()
    if sweeper is None:
        return None
    try:
        if sweeper.enabled():
            return None
        sym = sweeper.symbol
    except Exception:  # noqa: BLE001
        record_swallowed_here("cash_park_retired.retired_cash_park_symbol", log=logger)
        return None
    return sym if isinstance(sym, str) and sym.strip() else None


def release_retired_cash_park(get_sweeper: SweeperGetter, run_id: str | None) -> None:
    """Sell any sweep vehicle still held after the sweep was disabled.

    Called at the start of every market-hours session (morning, midday/
    close review, intra_check), right after the stop-coverage audit and
    before any seat reads the book, so the release lands in cash the
    same session. Non-fatal by design; see
    `CashSweeper.release_retired_vehicle`.
    """
    sweeper = get_sweeper()
    if sweeper is None:
        return
    try:
        sweeper.release_retired_vehicle(run_id=run_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("cash sweep retired: release failed (non-fatal): %s", exc)
