"""Rollback for a claimed-but-undelivered owner alert."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Iterable

from src.coverage_watchdog import (
    _elected_unfilled_alerted_symbols,
    load_state,
    repair_failure_alert_day,
    save_state,
)


def release_elected_unfilled_alert(
    symbols: Iterable[str],
    *,
    now: datetime | None = None,
    path: Path | None = None,
) -> bool:
    """Undo a `claim_elected_unfilled_alert` whose send FAILED so the next
    run retries. False when the state could not be written (the caller must
    say so loudly, not let the rollback fail silently)."""
    day = repair_failure_alert_day(now)
    state = load_state(path)
    drop = {str(s).strip().upper() for s in symbols if str(s).strip()}
    remaining = _elected_unfilled_alerted_symbols(state, day) - drop
    state["elected_unfilled_alerted_symbols"] = {
        "day": day,
        "symbols": sorted(remaining),
    }
    return bool(save_state(state, path))
