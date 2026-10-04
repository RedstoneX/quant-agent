"""The stop-level mismatch record and its owner report, moved out of
`stop_records.py`.

Neither piece touches the broker or the database: the dataclass is a value
and the report only logs and pages. Both can be exercised with hand-built
mismatches, which is the boundary test for a split.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class StopLevelMismatch:
    """One symbol whose archive stop_loss is not the broker's live stop."""

    symbol: str
    recorded: float | None
    live: float | None
    is_short: bool
    reason: str


def report_stop_level_mismatches(mismatches: list[StopLevelMismatch]) -> None:
    """Log and page every remaining mismatch. Do not mute; do not invent.

    Write-back of the live protective order happens first (see
    `write_back_live_protective_stops`). Anything still listed here is an
    unfixed record. 2026-09-16 once-per-day paging cleared the COP/EQNR
    alerts without fixing the archive; that mute is gone.
    """
    if not mismatches:
        return
    for item in mismatches:
        logger.error(
            "STOP RECORD MISMATCH: %s — %s (short=%s)",
            item.symbol, item.reason, item.is_short,
        )
    lines = "\n".join(
        f"  {item.symbol}: {item.reason}" for item in mismatches
    )
    body = (
        "STOP RECORD DOES NOT MATCH THE BROKER\n"
        "The desk's own opening-row stop_loss is not the live protective "
        "stop. Analysis drawn from the archive would be stale. The broker "
        "stop was NOT changed by this check.\n"
        f"{lines}\n"
        "Write-back covers in-code replace/trail/repair/rearm/ex-div and "
        "a live protective order found at reconcile. A mismatch after "
        "that is an out-of-band move, or a write-back that failed. Do not "
        "treat a 'traded through its stop' reading from the archive as "
        "real until these match."
    )
    try:
        from src import notifier as _notifier
        _notifier.send_owner_alert(
            body, symbols=[item.symbol for item in mismatches],
        )
    except Exception as exc:  # noqa: BLE001
        logger.error("stop-record mismatch owner alert failed: %s", exc)
