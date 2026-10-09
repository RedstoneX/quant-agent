"""One durable row per protective-stop placement, so the stop-LIMIT buffer stops being unmeasurable.

WHY THIS EXISTS. `AlpacaBroker.STOP_LIMIT_BUFFER_PCT` (3%) governs how far
past the trigger a protective stop-LIMIT's limit sits. Since the 2026-09-25
ruling the primary protective order is a stop-MARKET, so that buffer now
governs only the SAFETY FALLBACK taken when the broker refuses a stop-market
for an unsupported order-type/tif combo. The number ledger's row for the
constant cannot close either way until something counts how often that
fallback leg is actually taken -- its own DELETE outcome depends on that
count -- and nothing in the repo recorded it.

WHAT A ROW CARRIES, and why it is not just "it fired". The ledger's open
question is how far past a trigger a stop-LIMIT has to sit to stay marketable
on the gap days a stop exists for. A bare fired/not-fired count answers "is
this path alive" and nothing about whether 3% is the right distance. So each
row carries the trigger, the limit the buffer produced, the absolute distance
between them, the buffer in force, and whether the limit came from the buffer
or was supplied by the caller -- enough for a later reader to join a placement
to its fill and ask whether the limit was crossed.

THREE FACTS STAY DISTINGUISHABLE, exactly as `ReconciliationLog.status` keeps
them: no row at all means the site was NEVER REACHED (never "zero"); a
`primary` row means the stop-market leg was taken; a `fallback` row means the
stop-limit leg was. Never-exercised and never-fallen-back are different
answers and must not collapse.

NO RUNNING TOTALS. Nothing here increments, caches or stores an aggregate.
Every question ("how often has the fallback been taken?") is a count over the
rows at read time. An accumulator guard enforces that and is correct to.

NO HANDLE IS THREADED. Per `src/sentinel/guarded_reach.py`: the caller passes
whatever it ALREADY has in scope and `ledger_in_reach` walks it at call time
(a `StopPlacer` carries `.client`, which carries the lent connection getter).
No signature changes, nothing written back onto the objects walked.

NOTHING HERE CAN BREAK A PLACEMENT. The whole body is wrapped: a failure logs
a full traceback at ERROR and returns, and the stop proceeds unchanged. This
is observation only -- it changes no order, no stop, no sizing and no buffer.
"""

from __future__ import annotations

import json
import logging

from src.sentinel.guarded_reach import ledger_in_reach
from src.sentinel.reconciliation import ReconciliationLog

logger = logging.getLogger("src.execution.broker")

#: Row kind prefix; a reader counts rows by these two kinds and by their absence.
KIND_PREFIX = "stop_limit_buffer"

#: The stop-MARKET leg -- the primary order since the 2026-09-25 ruling.
LEG_PRIMARY = "primary"

#: The stop-LIMIT safety fallback -- the only leg the buffer still governs.
LEG_FALLBACK = "fallback"

#: The limit came from `STOP_LIMIT_BUFFER_PCT`, i.e. this placement is one the
#: constant actually governed.
LIMIT_FROM_BUFFER = "buffer"

#: The caller supplied an explicit limit, so the buffer governed nothing here.
LIMIT_FROM_CALLER = "caller"


def stop_leg_measurement(
    *, leg: str, symbol: str, qty, side: str, stop_price, limit_price, buffer_pct, limit_source: str
) -> dict:
    """The per-placement facts a later reader needs to judge the buffer's size.

    Pure: builds the payload and computes the trigger-to-limit distance. Kept
    separate from the write so a test can assert the measurement without a
    database, and so no arithmetic sits inside the swallowing wrapper.
    """
    distance = None
    try:
        if stop_price is not None and limit_price is not None:
            distance = abs(float(limit_price) - float(stop_price))
    except (TypeError, ValueError):
        distance = None
    return {
        "leg": leg,
        "fallback_taken": leg == LEG_FALLBACK,
        "symbol": symbol,
        "qty": qty,
        "side": side,
        "stop_price": stop_price,
        "limit_price": limit_price,
        "limit_distance": distance,
        "buffer_pct": buffer_pct,
        "limit_source": limit_source,
    }


def record_stop_leg(
    owner, *, leg: str, symbol: str, qty, side: str, stop_price, limit_price, buffer_pct, limit_source: str
) -> None:
    """Write ONE row for ONE protective-stop placement. Never raises.

    `owner` is whatever the call site already holds; the ledger is found on it
    at call time. With no ledger in reach the write is skipped at debug and
    the placement is untouched -- a skipped row is indistinguishable from a
    never-reached site, which is why the skip is logged.
    """
    try:
        measurement = stop_leg_measurement(
            leg=leg,
            symbol=symbol,
            qty=qty,
            side=side,
            stop_price=stop_price,
            limit_price=limit_price,
            buffer_pct=buffer_pct,
            limit_source=limit_source,
        )
        ledger = ledger_in_reach(owner)
        conn = getattr(ledger, "conn", None)
        if conn is None:
            logger.debug(
                "stop-leg row for %s (%s) not recorded: no ledger in reach",
                symbol,
                leg,
            )
            return
        ReconciliationLog(conn=conn).record(
            kind=f"{KIND_PREFIX}:{leg}",
            agreed=True,
            detail=json.dumps(measurement, default=str),
        )
    except Exception:  # noqa: BLE001 - an observer must never break a stop
        logger.error(
            "stop-leg row for %s (%s) could not be recorded",
            symbol,
            leg,
            exc_info=True,
        )


def record_leg_for(placer, leg, symbol, qty, side, stop_price_q, limit_price_q, limit_source) -> None:
    """The row for one placement, from the stop placer's own call site.

    The buffer in force is read off the placer, so a swapped buffer is
    recorded as the one that actually governed the limit rather than the
    class default. Positional, because the call site may not grow.
    """
    record_stop_leg(
        placer,
        leg=leg,
        symbol=symbol,
        qty=qty,
        side=side,
        stop_price=stop_price_q,
        limit_price=limit_price_q,
        buffer_pct=getattr(placer, "STOP_LIMIT_BUFFER_PCT", None),
        limit_source=limit_source,
    )
