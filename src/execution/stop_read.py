"""Reading a position's live protective stop: three answers, never two.

"There is no stop" and "the broker did not answer" used to both come back as
None, so a transient read failure looked like an unprotected-by-design
position and the stop adjustment was skipped without a trace. `StopRead`
keeps them apart, and an unreadable stop is recorded and sent to the owner.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

FOUND = "found"
NONE = "none"
UNREADABLE = "unreadable"

_alerted: set[tuple[str, str]] = set()


class StopReadUnavailable(Exception):
    """The broker could not say what stop (if any) rests on a symbol."""


@dataclass(frozen=True)
class StopRead:
    state: str
    _price: float | None = None
    reason: str = ""

    @property
    def found(self) -> bool:
        return self.state == FOUND

    @property
    def absent(self) -> bool:
        return self.state == NONE

    @property
    def unreadable(self) -> bool:
        return self.state == UNREADABLE

    @property
    def price(self) -> float:
        """The stop level. Only a FOUND read has one; anything else raises."""
        if self.state != FOUND or self._price is None:
            raise LookupError(f"no stop price: read state is {self.state}")
        return self._price


def unreadable_stop_text(symbol: str, reason: str = "") -> str:
    """What the owner reads. It says the stop could not be READ, never that
    there is none."""
    sym = str(symbol or "").upper()
    return (
        f"the desk could not read the protective stop on {sym} from the "
        f"broker, so it does not know whether one is resting or at what price "
        f"- no stop adjustment was made on {sym} this run"
        + (f" (broker said: {reason})" if reason else "")
    )


def read_stop(broker: Any, symbol: str, *, db: Any = None,
              run_id: str | None = None, context: str = "") -> StopRead:
    """Ask the broker for the live stop and classify the answer.

    An unreadable stop is logged, written to the evidence table when `db` is
    given, and sent to the owner once per symbol per day.
    """
    try:
        raw = broker.get_current_stop_price(symbol)
    except Exception as exc:  # noqa: BLE001 - classified, recorded and alerted below
        _report_unreadable(symbol, str(exc) or type(exc).__name__,
                           db=db, run_id=run_id, context=context)
        return StopRead(UNREADABLE, None, str(exc))
    if raw is None:
        return StopRead(NONE)
    px = float(raw) if isinstance(raw, (int, float)) else float("nan")
    if not math.isfinite(px):
        reason = f"non-numeric stop answer {raw!r}"
        _report_unreadable(symbol, reason, db=db, run_id=run_id, context=context)
        return StopRead(UNREADABLE, None, reason)
    if px <= 0:
        return StopRead(NONE)
    return StopRead(FOUND, px)


def _report_unreadable(symbol: str, reason: str, *, db: Any,
                       run_id: str | None, context: str) -> None:
    sym = str(symbol or "").upper()
    logger.error("stop read UNREADABLE for %s (%s): %s", sym, context, reason)
    from src.execution.exit_path_records import record_stop_read_unreadable
    record_stop_read_unreadable(db, symbol=sym, reason=reason,
                                context=context, run_id=run_id)
    from src.trading_calendar import et_today
    key = (sym, et_today().isoformat())
    if key in _alerted:
        return
    try:
        from src.notifier.owner_alert import send_owner_alert
        if send_owner_alert(unreadable_stop_text(sym, reason), symbols=[sym]):
            _alerted.add(key)
        else:
            logger.warning("stop-read owner alert for %s was not delivered", sym)
    except Exception as exc:  # noqa: BLE001
        logger.warning("stop-read owner alert for %s failed: %s", sym, exc)
