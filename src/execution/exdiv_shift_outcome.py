"""What the ex-dividend stop shift leaves behind: the leg rows, the owner alert, the naked window.

Lifted out of `ExDividends` in `src/pipeline_protection.py` and out of
`src/execution/exit_path_records.py`, which re-exports every name here so each
existing importer keeps working.
"""

from __future__ import annotations

import logging
from typing import Any, Callable

logger = logging.getLogger(__name__)


def stop_shift_incomplete_text(symbol: str, status: str, shifted: int, total: int) -> str:
    """The plain sentence the owner reads when a shift did not fully land."""
    sym = str(symbol or "").upper()
    if status == "naked":
        return (
            f"a protective stop on {sym} is GONE: the broker was re-read after "
            f"a dead order replacement and shows no resting stop for it, so "
            f"the position is UNPROTECTED until coverage repair places one"
        )
    if status == "unknown":
        return (
            f"the ex-dividend stop shift on {sym} got no answer from the broker "
            f"for at least one of its {total} protective stop(s), so the desk "
            f"does not know which price they are resting at — nothing was "
            f"cancelled and nothing was written down as moved"
        )
    return (
        f"only {shifted} of {total} protective stop(s) on {sym} moved down by "
        f"the dividend; the rest are still at the pre-dividend level, which the "
        f"ex-dividend opening gap can trigger on its own — nothing was cancelled"
    )


def record_shift_outcome(
    db: Any,
    symbol: str,
    amount: float,
    order: dict,
    run_id: str | None,
    on_fault: Callable[[str, Exception], None],
    record_legs: Callable[..., Any],
) -> None:
    """Item 201: the per-leg outcome is a ROW whatever the outcome, and a
    shift that did not fully land is an owner-visible change in protection.

    `record_legs` is `exit_path_records.record_stop_shift_legs`, passed in so this
    module never imports its re-exporter (that would be an import cycle)."""
    status = str(order.get("status") or "")
    shifted = int(order.get("shifted") or 0)
    total = int(order.get("total") or 0)
    record_legs(
        db,
        symbol=symbol,
        amount=amount,
        mode=str(order.get("mode") or ""),
        status=status,
        shifted=shifted,
        total=total,
        legs=order.get("legs"),
        run_id=run_id,
    )
    if status in ("partial", "refused", "unknown", "naked", "market_closed"):
        try:
            from src.notifier import send_owner_alert

            send_owner_alert(
                stop_shift_incomplete_text(symbol, status, shifted, total),
                symbols=[symbol],
            )
        except Exception as e:  # noqa: BLE001
            on_fault("exdiv.owner_alert", e)
            logger.warning("ex-div: owner alert failed for %s: %s", symbol, e)
