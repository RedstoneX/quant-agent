"""THE one place a missing protective stop is re-placed.

Lifted out of `TradingPipeline._repair_stop_coverage` unchanged so that the
two callers that need it can share it rather than grow a second copy:

  * the in-session coverage sweep (`TradingPipeline._reconcile_stop_coverage`),
    which is where this behaviour has always lived, and
  * `src/coverage_watchdog.py`, which runs from the alert-heartbeat unit and
    is the only thing still running while the trading timers are stopped
    (docs/INCIDENT_HISTORY.md, item 53).

The pipeline method is now a thin delegate. A second order-placement path
was the alternative and is exactly how one behaviour ends up with two homes
that drift; the fractional tif rule alone (`_derive_stop_tif`) is a rule the
duplicate would have had to re-learn.

The DEPENDENCIES are passed in rather than reached for: `broker` supplies the
price and the retrying submit, `last_buy` supplies the recorded stop level.
The watchdog has no `TradingPipeline` and must never build one — that would
pull the agents, the LLM clients and the whole session machinery into a
06:15 heartbeat.
"""
from __future__ import annotations

import logging
import math
from typing import Any, Callable

from src.execution.stop_records import (
    STOP_ABSENT, STOP_USABLE, classify_stop_price,
)

logger = logging.getLogger(__name__)


def _refuse(outcome: dict | None, reason: str) -> bool:
    """Record WHY this repair did not happen, then report it as not repaired.

    docs/WORK.md item 88. Every `return False` in this function is one of
    two very different things — "the broker would not take the order" or
    "this janitor refuses to place this number" — and the caller used to
    receive only the bare bool. `coverage == 'none'` then paged the owner
    with "the automatic repair could not restore one" and no reason at
    all, and a 'partial' gap carried the refusal no further than a log
    line. `reason` is a plain sentence, owner-facing, stamped onto the
    caller's gap dict when it supplies one.
    """
    if isinstance(outcome, dict):
        outcome["repair_refusal"] = reason
    return False


def repair_stop_coverage(
    *,
    broker: Any,
    last_buy: Callable[..., dict | None],
    symbol: str,
    uncovered_qty: float,
    is_short: bool,
    db: Any = None,
    outcome: dict | None = None,
) -> bool:
    """Best-effort: re-place protective stop coverage on an uncovered
    position using the stop level recorded on its last opening row (BUY for
    a long, SHORT for a short). Returns True when the gap was actually
    closed.

    Spec §11.1 hybrid fractional stops: this is also THE re-placement path
    for a sub-share DAY stop that lapsed at yesterday's close. It needs no
    fractional special-case of its own — it routes through
    `_submit_protective_stop_retrying`, which splits a fractional
    `uncovered_qty` into its GTC and DAY legs, so re-placing 0.3456 share(s)
    at the open and protecting a whole fresh 12.3456-share entry are the same
    one code path. THE CALLER owns the decision of WHETHER to call this at
    the current hour; this function does not consult the clock.

    Why this is safe to auto-repair (it deliberately wasn't before): the old
    objection was "the original protective level is unknown for a position
    with no live stop, so picking one is a policy decision". It isn't
    unknown — the opening row carries the `stop_loss` the PM/RM agreed and
    the constructor sized against. Repairing to THAT level restores the
    reviewed intent rather than inventing a new one. The same is true of a
    SHORT row: `insert_trade` writes `stop_loss` on that action the same way
    it does on BUY.

    This is the belt for the 2026-07-16 CRITICAL (BUY-attached OTO stops
    inherited a DAY tif and expired at the close, leaving positions naked
    overnight) — both for any position that bug left uncovered, and for a
    crash between an entry fill and `place_entry_protection`.

    Guards: never place a stop that would instantly fire (a long's sell-stop
    at/above the live price, a short's buy-stop at/below it — that would
    turn a repair into a market-order exit, a decision for the reviewer, not
    for a janitor), and never invent a level when the opening row has none.
    After a trail, `stop_loss` on that row is the LIVE recorded level, not
    the frozen entry stop. `would_fire` therefore uses the trailed price.
    Restoring the entry stop instead would be a widen; this janitor does
    not widen. A stop cancelled after a trail, with the tape already through
    the trailed level, stays flagged for the reviewer.
    The stop-limit buffer is the broker's existing `STOP_LIMIT_BUFFER_PCT`,
    mirrored the same way `place_entry_protection` already does: below a
    sell-stop, above a buy-stop. No new constant.

    It ADDS an order and nothing else. There is no path here that sells,
    resizes, closes, zeroes a position, or re-pegs an entry: the only broker
    call it makes is `_submit_protective_stop_retrying`, and the stop level
    comes from a recorded row, never from a target.

    When the broker accepts an order id it writes that same recorded level
    back onto the opening row (`db.update_open_stop_loss` when `db` is
    supplied), including a partial cover whose id landed. For a first-time
    restore this is a no-op on the number; after a trail it is what keeps
    a later repair from re-placing a stale entry stop. A kill-switch or
    missing-id payload is not written back — the broker does not hold it.
    Partial cover still returns False so the pass stays loud until the
    gap is gone.
    """
    if uncovered_qty <= 0:
        return False
    opening = "SHORT" if is_short else "BUY"
    protective_side = "buy" if is_short else "sell"
    try:
        entry = last_buy(symbol, action=opening) or {}
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "coverage repair: last-%s lookup failed for %s: %s",
            opening, symbol, exc,
        )
        return _refuse(
            outcome,
            f"the archive could not be read for the recorded {opening} stop",
        )
    # docs/WORK.md item 88 — the two cases this used to collapse into one
    # sentence. "No stop was ever recorded" and "a stop was recorded and its
    # value is garbage" are different facts: the first is a row this janitor
    # has no reviewed level for, the second is a corrupt archive on a live
    # position, and an owner reading "has no recorded stop_loss" about a row
    # that DOES carry one would go looking in the wrong place. Neither is
    # repaired — inventing a level was never this function's job — but they
    # are no longer reported as the same thing. The classification also
    # catches a recorded ±Inf, which the old `<= 0` test passed straight
    # through to the broker (NaN cannot survive SQLite, which stores it as
    # NULL; Inf round-trips as a real number).
    recorded = entry.get("stop_loss")
    stop_state, stop_price = classify_stop_price(recorded)
    if stop_state == STOP_ABSENT:
        logger.warning(
            "coverage repair: %s has no recorded %s stop_loss — leaving the "
            "gap flagged for manual review", symbol, opening,
        )
        return _refuse(
            outcome,
            f"no stop level was ever recorded on the {opening} row, so "
            f"there is no reviewed level to restore",
        )
    if stop_state != STOP_USABLE:
        logger.error(
            "coverage repair REFUSED for %s: the recorded %s stop_loss is "
            "%r, which cannot be a stop price. NOT treated as 'no stop "
            "needed' — the gap stays flagged for manual review and the "
            "archive row needs a look.", symbol, opening, recorded,
        )
        return _refuse(
            outcome,
            f"the recorded {opening} stop level is {recorded!r}, which "
            f"cannot be a stop price — the archive row is corrupt",
        )
    # The wrong-side test below decides whether putting this stop back would
    # fire it instantly — i.e. sell the position at market. That test is only
    # as good as the price it runs against, and a price with no trade time on
    # it can be yesterday's last print on a thin name, or a quote the tape
    # never confirmed. Against a stale number the test can read "safe" while
    # today's real price is already through the stop, and the repair becomes
    # an unintended market exit. So a repair requires a trade print stamped
    # today; anything else leaves the gap flagged for the next sweep, which
    # is the same outcome this function already produces for every other
    # unverifiable input.
    stamped = None
    try:
        from src.execution.broker import LivePrice

        getter = getattr(broker, "get_latest_price_stamped", None)
        if callable(getter):
            candidate = getter(symbol)
            # isinstance, not truthiness: most tests drive this with a
            # MagicMock broker whose auto-attributes are callable and whose
            # return value is another MagicMock. Only a real reading is
            # allowed to carry the freshness verdict; anything else falls
            # back to the bare price exactly as before.
            if isinstance(candidate, LivePrice):
                stamped = candidate
        price = stamped.price if stamped is not None else broker.get_latest_price(symbol)
    except Exception as exc:  # noqa: BLE001
        logger.warning("coverage repair: price lookup failed for %s: %s", symbol, exc)
        return _refuse(
            outcome, "the live price could not be read, so the recorded stop "
            "could not be checked against the tape",
        )
    if not (isinstance(price, (int, float)) and price > 0 and math.isfinite(price)):
        return _refuse(
            outcome, "the broker returned no usable live price, so the "
            "recorded stop could not be checked against the tape",
        )
    if stamped is not None and not stamped.is_today_print:
        logger.warning(
            "coverage repair: %s has no trade print from today (price $%.2f came "
            "from %s) — a stop placed off an unconfirmed price could fire "
            "immediately. Leaving the gap flagged for the next sweep.",
            symbol, price, stamped.source,
        )
        return _refuse(
            outcome, "there is no trade print from today to check the "
            "recorded stop against",
        )
    # Long sell-stop must sit strictly below the tape; short buy-stop must
    # sit strictly above it. The wrong-side test is the one that would turn
    # this janitor into an immediate marketable exit.
    would_fire = stop_price <= price if is_short else stop_price >= price
    if would_fire:
        logger.warning(
            "coverage repair: %s recorded %s-stop $%.2f is on the live-price "
            "side of $%.2f — a repair would fire immediately. Leaving the "
            "gap flagged; the reviewer owns this exit decision.",
            symbol, protective_side, stop_price, price,
        )
        return _refuse(
            outcome,
            f"the recorded stop ${stop_price:,.2f} is already on the live-"
            f"price side of ${price:,.2f}, so restoring it would exit the "
            f"position at market — that is the reviewer's decision",
        )
    # Spec §11.1 guard 1 belongs here too. This was a single bare
    # `_submit_stop_limit_order` call with NO retry burst at all — a
    # transient failure (429, dropped connection) cost the position a full
    # 30-minute cycle instead of clearing in ~2 seconds the way the entry
    # path does, and for a FRACTIONAL `uncovered_qty` (this repair is
    # reached with one whenever the gapped position is itself fractional)
    # there was also no whole-share fallback — the exact gap guard 1 exists
    # to close on the entry side, left open on the belt that is supposed to
    # be its backstop. Route through the same retrying+fallback machinery
    # instead of a second, weaker copy of it.
    buffer_mult = (
        (1 + broker.STOP_LIMIT_BUFFER_PCT) if protective_side == "buy"
        else (1 - broker.STOP_LIMIT_BUFFER_PCT)
    )
    result = broker._submit_protective_stop_retrying(
        symbol=symbol, qty=uncovered_qty, stop_price=stop_price,
        limit_price=stop_price * buffer_mult,
        side=protective_side,
    )
    from src.execution.stop_records import accepted_stop_order, write_back_stop_loss
    if result is None or not accepted_stop_order(result):
        logger.error(
            "coverage repair FAILED for %s (%.4f uncovered, stop $%.2f) — "
            "retries exhausted or broker did not accept an order id",
            symbol, uncovered_qty, stop_price,
        )
        return _refuse(
            outcome,
            f"the broker did not accept a protective stop at "
            f"${stop_price:,.2f} after every retry",
        )
    residual = 0.0
    try:
        residual = float(result.get("uncovered_qty") or 0)
    except (TypeError, ValueError):
        residual = 0.0
    if db is not None:
        write_back_stop_loss(db, symbol, stop_price, is_short=is_short)
    if residual > 0:
        # A whole-share floor stop landed but a sub-share sliver is still
        # gapped. Reported as NOT repaired — not because nothing happened,
        # but because the gap is real and smaller, not gone. The broker
        # snapshot the NEXT sweep takes reflects the partial cover on its own
        # and reclassifies this symbol "partial" rather than "none"; this
        # pass keeps escalating instead of going quiet on a still-real gap.
        logger.warning(
            "coverage repair PARTIAL for %s: covered %.4f of %.4f "
            "uncovered share(s) at stop $%.2f — %.4f share(s) still "
            "gapped; next sweep will re-check", symbol,
            uncovered_qty - residual, uncovered_qty, stop_price, residual,
        )
        return _refuse(
            outcome,
            f"only {uncovered_qty - residual:.4f} of {uncovered_qty:.4f} "
            f"uncovered share(s) could be covered",
        )
    logger.warning(
        "COVERAGE REPAIRED: %s — placed protective %s stop-limit coverage "
        "for %.4f uncovered share(s) at the recorded %s stop $%.2f (GTC over "
        "the whole shares, DAY over any sub-share remainder)",
        symbol, protective_side, uncovered_qty, opening, stop_price,
    )
    return True
