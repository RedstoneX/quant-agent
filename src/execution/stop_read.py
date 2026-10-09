"""Reading a position's live protective stop: three answers, never two.

"There is no stop" and "the broker did not answer" used to both come back as
None, so a transient read failure looked like an unprotected-by-design
position and the stop adjustment was skipped without a trace. `StopRead`
keeps them apart.

An unreadable stop is escalated, never just reported (owner ruling
2026-10-02): (1) retry the per-symbol read with a short bounded backoff,
(2) ask a different way - the broker's full open-orders list, (3) ACT: treat
the position as unprotected and establish protection, then (4) record and
alert as the audit trail of what the desk did.
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass
from typing import Any, Callable

from src.sentinel.guarded import record_guarded_pass

logger = logging.getLogger(__name__)

FOUND = "found"
NONE = "none"
UNREADABLE = "unreadable"

_sleep: Callable[[float], None] = time.sleep

_alerted: set[tuple[str, str]] = set()


class StopReadUnavailable(Exception):
    """The broker could not say what stop (if any) rests on a symbol."""


@dataclass(frozen=True)
class StopRead:
    state: str
    _price: float | None = None
    reason: str = ""
    #: What step 3 did: "" (not reached), "established", or "not_acted: ...".
    action: str = ""

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


def classify_stop_orders(symbol: str, orders: Any) -> float | None:
    """The live stop among `orders` for one symbol; None = none rests.

    Raises StopReadUnavailable when both sides carry stops (ambiguous).
    """
    # Post-#102 a position can legitimately carry SEVERAL stops on its
    # protective side (one GTC stop per entry BUY, plus coverage-repair
    # top-ups). The old first-match return made "the current stop"
    # depend on Alpaca's ordering (audit round 2). Consumers want the
    # level that fires FIRST; qty-weighting would blur two real levels
    # into a price nobody set.
    sell_stops: list[float] = []
    buy_stops: list[float] = []
    for order in orders or []:
        order_type = str(getattr(getattr(order, "order_type", None), "value", getattr(order, "order_type", ""))).lower()
        order_side = str(getattr(getattr(order, "side", None), "value", getattr(order, "side", ""))).lower()
        if "stop" not in order_type:
            continue
        try:
            px = float(getattr(order, "stop_price", 0) or 0)
        except (TypeError, ValueError):
            continue
        if px <= 0:
            continue
        if order_side == "sell":
            sell_stops.append(px)
        elif order_side == "buy":
            buy_stops.append(px)
    if sell_stops and buy_stops:
        # A single symbol can't legitimately be both long and short at
        # once, so seeing both sides means stale orders survived a
        # direction flip. Reporting either price would be a guess about
        # which one is "the" stop — fail closed instead so the caller
        # treats this as needing attention rather than trusting a number
        # that might belong to a position that no longer exists.
        logger.error(
            "get_current_stop_price: %s carries BOTH sell-stops %s and "
            "buy-stops %s — direction is ambiguous, refusing to report "
            "a stop",
            symbol,
            sorted(sell_stops),
            sorted(buy_stops),
        )
        raise StopReadUnavailable(f"{symbol}: both sell and buy stops rest")
    if sell_stops:
        if len(sell_stops) > 1:
            logger.info(
                "get_current_stop_price: %s carries %d sell-stops %s — "
                "reporting the highest (first to trigger on the way "
                "down)",
                symbol,
                len(sell_stops),
                sorted(sell_stops),
            )
        return max(sell_stops)
    if buy_stops:
        if len(buy_stops) > 1:
            logger.info(
                "get_current_stop_price: %s carries %d buy-stops %s — "
                "reporting the lowest (first to trigger on the way up)",
                symbol,
                len(buy_stops),
                sorted(buy_stops),
            )
        return min(buy_stops)
    return None


def repair_for(repair: Callable[..., Any], position: Any) -> Callable[[str], Any]:
    """An `establish` that re-places the stop for the WHOLE position through
    the caller's coverage-repair (`repair(symbol, qty, is_short=...)`)."""
    qty = float(position.qty)
    return lambda sym: repair(sym, abs(qty), is_short=qty < 0)


def _norm(sym: Any) -> str:
    return "".join(ch for ch in str(sym or "").upper() if ch.isalnum())


def _bulk_stop(broker: Any, symbol: str) -> float | None:
    """Step 2: the same fact asked a different way - ALL open orders at once."""
    from alpaca.trading.enums import QueryOrderStatus
    from alpaca.trading.requests import GetOrdersRequest

    orders = broker.client.get_orders(filter=GetOrdersRequest(status=QueryOrderStatus.OPEN, nested=True))
    mine = [o for o in list(orders or []) if _norm(getattr(o, "symbol", "")) == _norm(symbol)]
    return classify_stop_orders(symbol, mine)


def unreadable_stop_text(symbol: str, reason: str = "", action: str = "") -> str:
    """What the owner reads. It says the stop could not be READ, never that
    there is none, and what the desk did about it."""
    sym = str(symbol or "").upper()
    return (
        f"the desk could not read the protective stop on {sym} from the "
        f"broker after retrying and a second look at the full "
        f"open-orders list, so it does not know whether one is resting or at "
        f"what price" + (f" (broker said: {reason})" if reason else "") + (f". Then: {action}" if action else "")
    )


def _classify(raw: Any) -> StopRead:
    if raw is None:
        return StopRead(NONE)
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        # Our own read returns float | None; anything else is a broken answer.
        return StopRead(UNREADABLE, None, f"non-numeric stop answer {raw!r}")
    px = float(raw)
    if not math.isfinite(px):
        return StopRead(UNREADABLE, None, f"non-finite stop answer {raw!r}")
    return StopRead(FOUND, px) if px > 0 else StopRead(NONE)


def read_stop(
    broker: Any,
    symbol: str,
    *,
    db: Any,
    run_id: str | None = None,
    context: str = "",
    establish: Callable[[str], Any] | None = None,
    retry_pauses: tuple = (0.5, 1.0),
) -> StopRead:
    """Ask the broker for the live stop, escalating before giving up.

    `retry_pauses` is the bounded backoff: one retry after each pause.
    """
    last = ""
    for attempt in range(len(retry_pauses) + 1):
        if attempt:
            _sleep(retry_pauses[attempt - 1])
        try:
            res = _classify(broker.get_current_stop_price(symbol))
            record_guarded_pass((broker, db), "stop_read.per_symbol", context={"symbol": symbol})
        except Exception as exc:  # noqa: BLE001 - escalated below, never swallowed
            record_guarded_pass((broker, db), "stop_read.per_symbol", exc, context={"symbol": symbol})
            last = str(exc) or type(exc).__name__
            continue
        if not res.unreadable:
            return res
        last = res.reason
    try:
        res = _classify(_bulk_stop(broker, symbol))
        record_guarded_pass((broker, db), "stop_read.bulk", context={"symbol": symbol})
        if not res.unreadable:
            logger.warning(
                "stop read for %s answered by the bulk open-orders read after the per-symbol read failed (%s)",
                symbol,
                last,
            )
            return res
        last = res.reason
    except Exception as exc:  # noqa: BLE001
        record_guarded_pass((broker, db), "stop_read.bulk", exc, context={"symbol": symbol})
        last = f"{last}; bulk read also failed: {exc}"
    return _act(symbol, last, db=db, run_id=run_id, context=context, establish=establish)


def _act(
    symbol: str, reason: str, *, db: Any, run_id: str | None, context: str, establish: Callable[[str], Any] | None
) -> StopRead:
    """Step 3, then step 4 (record + alert exactly what was done).

    `establish` is the caller's coverage-repair placement. Placement is
    idempotent (derived client_order_id), so an unconfirmed stop is treated as
    missing; a stop that really was there may be duplicated, which the owner
    has accepted. A caller that only REPORTS passes None and nothing is placed.
    """
    sym = str(symbol or "").upper()
    if establish is None:
        action = (
            "not_acted: this was a reporting read and places nothing; the protection paths act on an unreadable stop"
        )
    else:
        try:
            closed = establish(sym)
            record_guarded_pass(db, "stop_read.establish", context={"symbol": sym})
        except Exception as exc:  # noqa: BLE001
            record_guarded_pass(db, "stop_read.establish", exc, context={"symbol": sym})
            closed, why = False, f"placement raised: {exc}"
        else:
            why = "the repair refused or placed nothing"
        action = (
            "established protection by placing a protective stop (may duplicate one that was already there)"
            if closed
            else f"not_acted: tried to place a protective stop but {why}"
        )
    _report_unreadable(sym, reason, action, db=db, run_id=run_id, context=context)
    return StopRead(UNREADABLE, None, reason, action)


def _report_unreadable(sym: str, reason: str, action: str, *, db: Any, run_id: str | None, context: str) -> None:
    logger.error("stop read UNREADABLE for %s (%s): %s | %s", sym, context, reason, action)
    from src.execution.exit_path_records import record_stop_read_unreadable

    record_stop_read_unreadable(db, symbol=sym, reason=reason, action=action, context=context, run_id=run_id)
    from src.trading_calendar import et_today

    key = (sym, et_today().isoformat())
    if key in _alerted:
        return
    try:
        from src.notifier.owner_alert import send_owner_alert

        if send_owner_alert(unreadable_stop_text(sym, reason, action), symbols=[sym]):
            _alerted.add(key)
        else:
            logger.warning("stop-read owner alert for %s was not delivered", sym)
        record_guarded_pass(db, "stop_read.owner_alert", context={"symbol": sym})
    except Exception as exc:  # noqa: BLE001
        record_guarded_pass(db, "stop_read.owner_alert", exc, context={"symbol": sym})
        logger.warning("stop-read owner alert for %s failed: %s", sym, exc)
