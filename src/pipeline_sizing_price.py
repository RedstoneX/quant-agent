"""The price an entry's share count divides by (lifted verbatim from pipeline_stages)."""
from __future__ import annotations

import logging

from src.data.live_price import resolve_live_price
from src.pipeline_stage_helpers import record_stage

#: Logged under the module the code lived in before the lift, so records are unchanged.
logger = logging.getLogger("src.pipeline_stages")


def _today_sizing_price(pipeline, symbol) -> float | None:
    """A price the SHARE COUNT may divide the dollar allocation by, or None.

    docs/WORK.md item 120. The number of shares an entry buys is
    `dollars / price`, so this price is the DIVISOR of the allocation and a
    wrong one mis-sizes the position PROPORTIONALLY. It must therefore be a
    real TODAY PRINT (`LivePrice.is_today_print`), never a quote MID and
    never a prior-session last trade.

    This is deliberately stricter than `_today_order_price`, the FILL
    reference, where a live quote mid IS a legitimate marketable-limit
    reference mid-session (owner 2026-09-12) — bounding an order you are
    about to cross against the current book is a different act from setting
    how many shares to buy. `_today_order_price` accepts a quote mid; this
    refuses it. When this returns None the caller must refuse the name as
    unmeasurable rather than size it on a bad price.

    It accepts the SAME today prices the constructor sizes off, so the two
    stages agree: a real last-trade print, and — when there is none — today's
    forming SESSION or minute bar via `resolve_live_price` (a real intraday
    price on the entitled venue, never a quote mid). Without this second
    branch an IEX-thin name with a today bar but no print — item 120's exact
    population — would be sized and approved by the paid PM/Risk seats and
    then silently skipped here, wasting those seats.

    Test compatibility: when the broker is a MagicMock whose
    `get_latest_price_stamped` does not return a real `LivePrice` and whose
    `get_intraday_snapshots` does not return a usable payload, this falls
    through to the bare `get_latest_price` exactly as `_today_order_price`
    does, so the many MagicMock-broker execution tests keep their behaviour.
    The freshness gate only bites on a real stamped price / real snapshot.
    """
    broker = getattr(pipeline, "broker", None)

    # 1. A real today PRINT from the stamped getter is the best sizing ref.
    stamped_getter = getattr(broker, "get_latest_price_stamped", None)
    stamped_is_real = False
    if callable(stamped_getter):
        try:
            from src.execution.broker import LivePrice

            candidate = stamped_getter(symbol)
            if isinstance(candidate, LivePrice):
                stamped_is_real = True
                if candidate.price and candidate.price > 0 and candidate.is_today_print:
                    return float(candidate.price)
        except Exception as exc:  # noqa: BLE001
            record_stage(pipeline, "sizing_price.stamped_read", exc)
            return None

    # 2. No today print: accept today's forming SESSION/minute bar through the
    #    same resolver the constructor uses (never a quote mid), so both
    #    stages agree on a thin name that has a bar but no print.
    snap_getter = getattr(broker, "get_intraday_snapshots", None)
    if callable(snap_getter):
        snap_is_real = False
        try:
            snaps = snap_getter([symbol])
            if isinstance(snaps, dict):
                snap_is_real = True
                resolved = resolve_live_price(snaps.get(symbol))
                if resolved.is_today_print:
                    return float(resolved.price)
        except Exception:  # noqa: BLE001
            snap_is_real = False
        # A REAL stamped price (real broker) that was a quote mid or stale,
        # and no usable today bar either: refuse rather than fall through to
        # the mid-capable bare getter.
        if stamped_is_real or snap_is_real:
            if stamped_is_real:
                logger.warning(
                    "%s sizing price refused: no today print and no today "
                    "session/minute bar — a quote mid or stale price cannot "
                    "set the share count", symbol,
                )
            return None
    elif stamped_is_real:
        return None

    # 3. Test / back-compat: a MagicMock broker that returned neither a real
    #    LivePrice nor a real snapshot dict. Keep existing behaviour.
    getter = getattr(broker, "get_latest_price", None)
    if not callable(getter):
        return None
    try:
        live = getter(symbol)
    except Exception as exc:  # noqa: BLE001
        record_stage(pipeline, "sizing_price.latest_read", exc)
        return None
    if isinstance(live, (int, float)) and not isinstance(live, bool) and live > 0:
        return float(live)
    return None
