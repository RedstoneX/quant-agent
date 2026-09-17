"""THE write-back and reconcile for the desk's recorded protective stop.

`trades.stop_loss` on the opening row (BUY or SHORT) is the archive's
record of the live protective level. Until this module existed it was
written once at entry and never again, so a trail, an ex-div shift, a
scale-in rearm or an out-of-band broker edit left the archive days stale.
That manufactured the false "traded through its own stop" filings on Visa
and Disney (WORK.md items 35 and 69).

Two jobs, both fail-closed on missing data, neither invents a price:

1. Write the level the broker just accepted back onto the opening row.
   Replacements funnel through `replace_stop_and_record` (which is
   `AlpacaBroker.replace_stop_loss` plus this write-back). Coverage repair
   writes the level it actually placed. Other price-changing paths
   (ex-div shift, scale-in rearm, residual reprotect) call
   `write_back_stop_loss` after the broker accepts.

2. Reconcile that recorded level against the broker's live stop. When
   the live protective order exists, write THAT price back onto the
   opening row (COP/EQNR: archive lagged the desk's own stop). That is
   not an invented level. Remaining mismatches still page — never mute
   an alert without fixing the record.

`initial_stop_loss` on the same opening row is the entry bet, frozen on
the first write-back (and set at insert). R-multiple and the Type A
breakeven ratchet still read THAT number, never the live one.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable

logger = logging.getLogger(__name__)

# Alpaca's published stock ticks, the same split `_quantize_price` in
# `src/execution/broker.py` already uses: $0.01 at or above $1, $0.0001
# below. A mismatch smaller than one tick is the SDK round-trip, not a
# different stop. Not a trading threshold.
_ALPACA_TICK_AT_OR_ABOVE_DOLLAR = 0.01
_ALPACA_TICK_BELOW_DOLLAR = 0.0001


def _finite_price(value: Any) -> float:
    try:
        price = float(value or 0)
    except (TypeError, ValueError):
        return 0.0
    if price != price or price <= 0:  # NaN or non-positive
        return 0.0
    return price


def _prices_match(recorded: float, live: float) -> bool:
    """True when the two prints are the same stop at Alpaca's round-trip.

    Tick size is Alpaca's published split (`_quantize_price`): $0.01 at or
    above $1, $0.0001 below. The allowed delta is half a tick — this
    repo's own stated float<->Decimal round-trip (the reprotect path's
    half-penny), not a trading threshold. A full-tick difference is a
    different stop.
    """
    tick = (
        _ALPACA_TICK_AT_OR_ABOVE_DOLLAR
        if min(recorded, live) >= 1.0
        else _ALPACA_TICK_BELOW_DOLLAR
    )
    return abs(recorded - live) <= (tick / 2.0)


def accepted_stop_order(order: Any) -> bool:
    """True only when the broker payload carries a real order id.

    Kill-switch and some reject paths return a dict with `id=None`
    without raising. Treating those as success would write a stop the
    broker does not hold.
    """
    if not isinstance(order, dict):
        return False
    oid = order.get("id")
    return bool(oid) and str(oid) not in {"None", "kill_switch_halted"}


def _holding_is_short(broker: Any, symbol: str) -> bool | None:
    """True/False from a real positions list; None when direction is unknown.

    `get_positions()` on a test double is often a MagicMock — iterating that
    is not a list of holdings. Fail closed to "unknown" rather than guess.
    """
    try:
        positions = broker.get_positions()
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(positions, list):
        return None
    for position in positions:
        if getattr(position, "symbol", None) != symbol:
            continue
        try:
            return float(getattr(position, "qty", 0) or 0) < 0
        except (TypeError, ValueError):
            return None
    return None


def recorded_initial_stop(row: dict | None) -> float:
    """The stop AT ENTRY, even after later write-backs of the live level.

    Prefers `initial_stop_loss` when that column is present on the row.
    A present-but-empty value is 0 — it must not fall back to `stop_loss`,
    because after a write-back that column is the live level, and a row
    that opened with no stop must not mint an entry bet from a later
    repair. Test doubles that omit the key entirely still fall back to
    `stop_loss` (legacy shape, entry == live). Never invents a price.
    """
    if not row:
        return 0.0
    if "initial_stop_loss" in row:
        return _finite_price(row.get("initial_stop_loss"))
    return _finite_price(row.get("stop_loss"))


def write_back_stop_loss(
    db: Any, symbol: str, stop_price: float, *, is_short: bool | None = None,
) -> bool:
    """Persist `stop_price` onto the symbol's latest BUY/SHORT row.

    `is_short=True` updates the SHORT row; `False` the BUY row. Omit only
    when the caller cannot tell — then the latest of either is used.
    Never raises: a failed archive write must not unwind a broker stop
    that already landed. Returns True only when the DB method reports it
    wrote.
    """
    price = _finite_price(stop_price)
    if price <= 0 or db is None:
        return False
    updater = getattr(db, "update_open_stop_loss", None)
    if not callable(updater):
        logger.error(
            "stop write-back: %s has no update_open_stop_loss — live stop "
            "$%.4f was NOT recorded", symbol, price,
        )
        return False
    kwargs: dict[str, Any] = {}
    if is_short is True:
        kwargs["action"] = "SHORT"
    elif is_short is False:
        kwargs["action"] = "BUY"
    try:
        return bool(updater(symbol, price, **kwargs))
    except TypeError:
        # A test double / older signature that only takes (symbol, price).
        try:
            return bool(updater(symbol, price))
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "stop write-back FAILED for %s @ $%.4f: %s — broker holds "
                "the live level; the archive is stale until the next "
                "successful write-back or reconcile report",
                symbol, price, exc,
            )
            return False
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "stop write-back FAILED for %s @ $%.4f: %s — broker holds the "
            "live level; the archive is stale until the next successful "
            "write-back or reconcile report",
            symbol, price, exc,
        )
        return False


def replace_stop_and_record(
    broker: Any,
    db: Any,
    symbol: str,
    new_stop_price: float,
    **kwargs: Any,
) -> dict | None:
    """The replacement funnel: broker replace, then archive write-back.

    Callers that used to talk to `AlpacaBroker.replace_stop_loss` directly
    (deterministic trail, midday TRAIL_STOP) go through here so a successful
    replace cannot silently leave `trades.stop_loss` on the entry level.
    A failed or refused replace writes nothing.
    """
    order = broker.replace_stop_loss(symbol, new_stop_price, **kwargs)
    if accepted_stop_order(order):
        recorded = write_back_stop_loss(
            db, symbol, new_stop_price,
            is_short=_holding_is_short(broker, symbol),
        )
        if not recorded:
            logger.error(
                "stop replace accepted for %s @ $%.4f but archive write-back "
                "did not persist — reconcile will surface the mismatch",
                symbol, new_stop_price,
            )
    return order


@dataclass(frozen=True)
class StopLevelMismatch:
    """One symbol whose archive stop_loss is not the broker's live stop."""

    symbol: str
    recorded: float | None
    live: float | None
    is_short: bool
    reason: str


def reconcile_recorded_stop_levels(
    *,
    broker: Any,
    last_buy: Callable[..., dict | None],
    positions: list,
    sweep_symbol: str | None = None,
    skip_symbols: set[str] | None = None,
) -> list[StopLevelMismatch]:
    """Compare each holding's recorded stop to the broker's live stop.

    Reports only. Does not write, does not place, does not invent a level.
    A missing live stop is coverage's job (`_reconcile_stop_coverage` /
    the watchdog) and is skipped here — there is no price to compare.
    A live stop with no recorded row, or a recorded row whose price is
    not the live one (beyond the broker's own round-trip epsilon), is
    the mismatch this exists to surface.

    `last_buy(symbol, action='BUY'|'SHORT')` is the same lookup coverage
    repair uses, so a long and a short cannot read each other's row.
    """
    mismatches: list[StopLevelMismatch] = []
    skip = set(skip_symbols or ())
    if sweep_symbol:
        skip.add(sweep_symbol)
    if not callable(last_buy):
        return mismatches
    for position in positions or []:
        symbol = getattr(position, "symbol", None)
        try:
            qty = float(getattr(position, "qty", 0) or 0)
        except (TypeError, ValueError):
            continue
        if not symbol or qty == 0 or symbol in skip:
            continue
        is_short = qty < 0
        try:
            live = broker.get_current_stop_price(symbol)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "stop-level reconcile: live stop lookup failed for %s: %s",
                symbol, exc,
            )
            continue
        live_px = _finite_price(live)
        if live_px <= 0:
            continue
        opening = "SHORT" if is_short else "BUY"
        try:
            row = last_buy(symbol, action=opening) or {}
        except TypeError:
            # A last_buy that still only takes the symbol (legacy tests /
            # purchase-memory callables). Direction is then whatever row
            # that callable returns; a BUY-only lookup on a short is a
            # mismatch of "no recorded SHORT stop", which is the honest
            # report.
            try:
                row = last_buy(symbol) or {}
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "stop-level reconcile: last-open lookup failed for %s: %s",
                    symbol, exc,
                )
                continue
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "stop-level reconcile: last-open lookup failed for %s: %s",
                symbol, exc,
            )
            continue
        recorded_px = _finite_price((row or {}).get("stop_loss"))
        if recorded_px <= 0:
            mismatches.append(StopLevelMismatch(
                symbol=str(symbol), recorded=None, live=live_px,
                is_short=is_short,
                reason=(
                    f"broker stop ${live_px:.4f} has no recorded "
                    f"{opening} stop_loss"
                ),
            ))
            continue
        if not _prices_match(recorded_px, live_px):
            mismatches.append(StopLevelMismatch(
                symbol=str(symbol), recorded=recorded_px, live=live_px,
                is_short=is_short,
                reason=(
                    f"recorded {opening} stop_loss ${recorded_px:.4f} "
                    f"!= broker stop ${live_px:.4f}"
                ),
            ))
    return mismatches


def write_back_live_protective_stops(
    db: Any, mismatches: list[StopLevelMismatch],
) -> list[StopLevelMismatch]:
    """Copy the desk's live protective order onto the opening row.

    `live` came from `broker.get_current_stop_price` — an order we already
    hold, not a number we invented. Returns mismatches that could not be
    written (those still page). Successful write-back is the fix; it is
    not a mute.
    """
    remaining: list[StopLevelMismatch] = []
    for item in mismatches or []:
        live = _finite_price(item.live)
        if live <= 0:
            remaining.append(item)
            continue
        recorded = write_back_stop_loss(
            db, item.symbol, live, is_short=item.is_short,
        )
        if not recorded:
            remaining.append(item)
            continue
        logger.info(
            "stop-level reconcile: wrote live protective stop $%.4f back "
            "onto %s archive (was %s)",
            live, item.symbol, item.recorded,
        )
    return remaining


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
