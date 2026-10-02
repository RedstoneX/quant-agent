"""In-place protective-stop amendment, lifted verbatim from AlpacaBroker.

A `StopAmender` is built from its collaborators alone (keyword-only), so the
amend path can be exercised without an `AlpacaBroker` or a TradingPipeline.
`AlpacaBroker` keeps same-named thin shims that build one per call.
"""
from __future__ import annotations

import logging

from alpaca.trading.requests import ReplaceOrderRequest

# Same log channel as before the move: operators and tests filter on the
# broker's logger name, and the move must not change what they see.
logger = logging.getLogger("src.execution.broker")


# Sentinel returned by `_amend_resting_stop_price` to mean "the in-place
# amend was NOT attempted (or its outcome is unknown), so the caller must run
# the legacy cancel+resubmit path". It is deliberately distinct from `None`,
# which means "the broker REFUSED the amend, the original stop is still
# resting, and cancelling it now would open a naked window for nothing".
_AMEND_NOT_ATTEMPTED = object()


def _is_terminal_broker_rejection(exc: BaseException) -> bool:
    """True when the broker's OWN answer says retrying is pointless.

    Board item 129: the retry burst below used to catch every exception
    identically, so a deterministic rejection (bad price, unsupported qty,
    a closed/unknown symbol — Alpaca's `APIError.status_code` 400/404/422,
    same classification `get_asset_record` and `get_intraday_snapshots`
    already use for exactly these codes) burned the full attempt budget
    and backoff before alerting, exactly the "delays the owner alert"
    outcome the retry ceiling was written to avoid. A 429/5xx/timeout/
    dropped-connection failure has no such status (or a 429/5xx one) and
    is genuinely worth another try, so only these codes short-circuit.
    """
    status_code = getattr(exc, "status_code", None)
    return status_code in (400, 404, 422)


def _quantize_price(price: float | None) -> float | None:
    """Round to Alpaca's minimum tick size: $0.01 for stocks ≥ $1, $0.0001 below.

    The quote-midpoint in `get_latest_price` can produce sub-penny values like
    $106.515; submitting that raw triggers Alpaca error 42210000 and the order
    is rejected. Observed 2026-04-17 morning: UPS BUY @ $106.515 rejected.

    NaN/Inf handling: NaN comparisons all return False, so the original
    `price <= 0` guard fell through to `round(nan, ...)` = nan. The NaN
    then propagated all the way to Alpaca's submit_order, which silently
    broker-rejects the order and corrupts audit logs. Treat NaN/Inf as
    None (no quotable price) — callers' existing
    `price is not None and price > 0` checks then skip the order or
    fall back to market. Zero/negative values are preserved unchanged
    (pre-existing semantics: caller decides what to do with them).
    """
    if price is None:
        return None
    import math as _math
    if not _math.isfinite(price):
        return None
    if price <= 0:
        return price
    return round(price, 2 if price >= 1.0 else 4)


class StopAmender:
    """Amend resting stop orders in place. Every collaborator is explicit."""

    def __init__(self, *, client, list_open_stop_orders_by_side, snapshot_stop_order):
        self.client = client
        self._list_open_stop_orders_by_side = list_open_stop_orders_by_side
        self._snapshot_stop_order = snapshot_stop_order

    #: Order statuses that mean the amended order is NOT resting at the new
    #: level. Anything else coming back from a replace is treated as live.
    _AMEND_DEAD_STATES = frozenset({"rejected", "canceled", "cancelled", "expired", "done_for_day"})

    @staticmethod
    def _failed_amend_payload(symbol: str, legs: list[dict]) -> dict:
        """The non-success return of an in-place amend.

        A bare `None` preserved protection and threw the evidence away: the
        caller could not tell a partial from a refusal, could not record which
        leg moved, and could not alert. `id` is None so `accepted_stop_order`
        still rejects it — nothing is written back — but the legs travel.
        """
        amended = [l for l in legs if l.get("outcome") == "amended"]
        if any(l.get("outcome") == "naked" for l in legs):
            status = "naked"
        elif any(l.get("outcome") == "unknown" for l in legs):
            status = "unknown"
        elif amended:
            status = "partial"
        elif legs and all(l.get("outcome") == "flat" for l in legs):
            status = "flat"
        else:
            status = "refused"
        return {"id": None, "status": status, "amend_status": status,
                "symbol": symbol, "legs": legs,
                "shifted": len(amended), "total": len(legs)}

    def _classify_after_dead_replacement(
        self, *, symbol: str, spec: dict, new_price: float, leg: dict,
    ) -> str:
        """Read the book after a replacement came back dead. Never guess.

        ASSUMED, NOT VERIFIED: that a replace moves the original order to
        REPLACED before the replacement is accepted. If that is how it works,
        a rejected or cancelled REPLACEMENT can mean the symbol has NO
        protective stop at all — so "the original is still resting" must be
        read off the broker, not inferred. What would settle the assumption:
        a rehearsal that forces a replacement to be rejected and then lists
        the symbol's open orders.

        Returns "refused" (the original is confirmed still resting),
        "amended" (something IS resting at the new level), "naked" (the book
        shows no protective stop for this symbol) or "unknown" (the book
        could not be read, or shows a level that is neither).
        """
        errors: list = []
        try:
            sells, buys = self._list_open_stop_orders_by_side(symbol, errors=errors)
            live_orders = list(sells or []) + list(buys or [])
        except Exception as exc:  # noqa: BLE001
            leg["detail"] += f"; the book could not be re-read ({exc})"
            return "unknown"
        if errors:
            leg["detail"] += "; the book could not be re-read"
            return "unknown"
        live = [spec_ for spec_ in (self._snapshot_stop_order(o) for o in live_orders)
                if spec_ is not None]
        if not live:
            # A replacement is also rejected when the ORIGINAL already
            # triggered: the book is then empty because the position is gone,
            # and calling that UNPROTECTED would alert about a flat symbol.
            try:
                held = [
                    abs(float(getattr(pos, "qty", 0) or 0))
                    for pos in (self.client.get_all_positions() or [])
                    if getattr(pos, "symbol", None) == symbol
                ]
            except Exception as exc:  # noqa: BLE001
                leg["detail"] += (
                    f"; the book is empty and the position could not be "
                    f"re-read ({exc}) — treating it as UNPROTECTED"
                )
                return "naked"
            if not held or sum(held) <= 0:
                leg["detail"] += (
                    "; the book is empty because the position is FLAT — the "
                    "original stop most likely filled, so there is nothing "
                    "left to protect"
                )
                return "flat"
            # HONEST LIMIT, no settle window: a replace that is still pending
            # at the broker can also present as an empty book for a moment.
            # The two are indistinguishable from one read, so this reports the
            # LOUD direction — a false UNPROTECTED alert costs attention, a
            # missed one costs the position. What would settle it: a rehearsal
            # that lists open orders during a pending replace.
            leg["detail"] += (
                "; the broker shows NO resting protective stop while the "
                "position is still open — UNPROTECTED (a replace still "
                "pending at the broker can look the same from one read)"
            )
            return "naked"
        if any(str(s["id"]) == str(spec.get("id")) for s in live):
            leg["detail"] += "; the ORIGINAL order was read back, still resting"
            return "refused"
        at_new = [s for s in live if abs(s["stop_price"] - new_price) <= 1e-9]
        if at_new:
            leg["new_id"] = at_new[0]["id"]
            leg["detail"] += (
                "; a stop was read back at the NEW level despite the dead "
                "replacement"
            )
            return "amended"
        leg["detail"] += (
            "; a stop is resting but at neither the old nor the new level"
        )
        return "unknown"

    def _amend_one_stop_price(self, *, symbol: str, spec: dict, new_price: float,
                              new_qty: int | None = None) -> dict:
        """Amend ONE resting stop's price and report what is KNOWN afterwards.

        The single place both the trailing path and the ex-dividend shift
        classify an amend's outcome. Sharing only the shape test and leaving
        the failure classification to each caller is sharing the half that does
        not lose money. Returns a leg record whose `outcome` is one of:

          "amended" — the broker answered with a live order id (confirmed);
          "refused" — the broker ANSWERED no (400/404/422, or a dead status),
                      so the ORIGINAL stop is still resting at its old level;
          "unknown" — no broker answer, or an answer with no id. The amend MAY
                      have been applied. Nothing may be cancelled on this
                      outcome and nothing may be STATED about where the stop is.
        """
        leg = {
            "id": str(spec.get("id") or ""), "qty": spec.get("qty"),
            "old_stop": spec.get("stop_price"), "new_stop": new_price,
            "new_id": None, "outcome": "unknown", "detail": "",
        }
        request_fields: dict = {"stop_price": new_price}
        # A stop-LIMIT leg keeps its own limit distance from the trigger.
        old_limit = spec.get("limit_price")
        if old_limit and spec.get("stop_price"):
            new_limit = _quantize_price(new_price + (old_limit - spec["stop_price"]))
            if new_limit is not None and new_limit > 0:
                request_fields["limit_price"] = leg["new_limit"] = new_limit
        if new_qty is not None:
            request_fields["qty"] = leg["new_qty"] = int(new_qty)
        try:
            replaced = self.client.replace_order_by_id(
                leg["id"], ReplaceOrderRequest(**request_fields),
            )
        except Exception as exc:  # noqa: BLE001
            if _is_terminal_broker_rejection(exc):
                leg["outcome"] = "refused"
                leg["detail"] = f"broker refused the amend: {exc}"
            else:
                leg["outcome"] = "unknown"
                leg["detail"] = (
                    f"no broker status on the amend ({exc}) — it may have been "
                    f"applied before the answer was lost"
                )
            return leg
        new_id = str(getattr(replaced, "id", "") or "")
        status_attr = getattr(replaced, "status", None)
        status = str(getattr(status_attr, "value", status_attr) or "accepted").lower()
        if not new_id:
            leg["detail"] = "broker accepted the amend but returned no order id"
            return leg
        leg["new_id"] = new_id
        leg["status"] = status
        if status in self._AMEND_DEAD_STATES:
            leg["detail"] = f"the replacement order came back {status}"
            leg["outcome"] = self._classify_after_dead_replacement(
                symbol=symbol, spec=spec, new_price=new_price, leg=leg,
            )
            return leg
        leg["outcome"] = "amended"
        return leg

    @staticmethod
    def _stop_order_amendable_in_place(order) -> bool:
        """True when `order` is the SHAPE the 2026-09-30 rehearsal measurement
        covered for `replace_order_by_id(stop_price=...)`: one plain, parentless
        stop-MARKET order with no legs.

        Shared by `_amend_resting_stop_price` (trailing) and `shift_stops_down`
        (ex-dividend) so the two paths cannot drift on what "measured-safe"
        means. Every rejection here routes to a cancel+resubmit fallback, which
        is the measured-safe outcome for an unrecognised shape.
        """
        # A bracket/OTO PARENT (it carries `legs`) is not a stop. A CHILD stop
        # leg amends: one `PATCH /orders/{id}` serves every order and the
        # request model excludes no class, while cancelling a child would also
        # pull its OCO sibling. UNMEASURED; a refusal leaves the original resting.
        if getattr(order, "legs", None):
            return False
        # A stop-LIMIT leg amends stop AND limit, so both must be readable.
        otype = getattr(order, "order_type", None) or getattr(order, "type", None)
        otype = str(getattr(otype, "value", otype) or "").lower()
        if otype == "stop_limit":
            try:
                return float(getattr(order, "limit_price", 0) or 0) > 0
            except (TypeError, ValueError):
                return False
        return otype == "stop"

    def _amend_resting_stop_price(
        self,
        *,
        symbol: str,
        live_orders: list,
        stop_specs: list[dict],
        new_stop_price: float,
        position_qty: float,
    ):
        """Amend EVERY resting protective stop's price in place, at one level.

        Returns a {id, status, symbol} dict on success, `None` when the broker
        REFUSED the amend (the original stop is still resting, protection is
        intact, and the caller must NOT cancel it), or `_AMEND_NOT_ATTEMPTED`
        when this path does not apply and the caller should run the legacy
        cancel+resubmit fallback.

        A whole-share QUANTITY amend was measured working 2026-09-30; a
        FRACTIONAL one is refused, so only that still takes the fallback.

        Measured against the broker on rehearsal account <redacted-rehearsal-account> on
        2026-09-30: `replace_order_by_id(id, ReplaceOrderRequest(stop_price=X))`
        moves a resting protective stop atomically (old order -> REPLACED, new
        id issued, exactly one open stop on the symbol at every instant), and a
        refused amend leaves the original `new` at its old price. Only the
        shapes that measurement covered take this path; everything else falls
        back.
        """
        if not stop_specs or len(stop_specs) != len(live_orders):
            return _AMEND_NOT_ATTEMPTED
        if not all(self._stop_order_amendable_in_place(o) for o in live_orders):
            return _AMEND_NOT_ATTEMPTED
        # MULTI-LEG (item 201): 9 of 11 positions are fractional [measured
        # 2026-10-01]; that each carries the two-leg pair is ASSUMED.
        try:
            covered = sum(abs(float(spec["qty"])) for spec in stop_specs)
        except (TypeError, ValueError, KeyError):
            return _AMEND_NOT_ATTEMPTED
        # A price-only amend cannot fix a coverage gap: if the resting stops do
        # not already cover exactly the position, the fallback (which resubmits
        # at the position's qty) is the path that repairs it. Compare at the
        # broker's own fractional resolution and SAY why when it does not match,
        # so a persistently-skipped atomic path is visible instead of invisible.
        new_qty = None
        if abs(covered - position_qty) > 1e-9:
            # One whole-share leg on a whole-share position: the quantity amend
            # was measured working (2026-09-30), so repair it in place.
            if (len(stop_specs) == 1 and covered == int(covered)
                    and position_qty == int(position_qty) and position_qty >= 1):
                new_qty = int(position_qty)
            else:
                logger.info("replace_stop_loss: %s stops cover %s of %s shares; no "
                            "in-place amend applies, cancel+resubmit (window "
                            "recorded)", symbol, covered, position_qty)
                return _AMEND_NOT_ATTEMPTED
        price = _quantize_price(new_stop_price)
        if price is None or price <= 0:
            return _AMEND_NOT_ATTEMPTED

        legs = [self._amend_one_stop_price(symbol=symbol, spec=spec, new_price=price,
                                           new_qty=new_qty)
                for spec in stop_specs]
        amended = [l for l in legs if l["outcome"] == "amended"]
        unknown = [l for l in legs if l["outcome"] == "unknown"]
        if any(l["outcome"] == "naked" for l in legs):
            # Read off the broker, not inferred: the symbol has no resting
            # protective stop. Say UNPROTECTED and let coverage repair place
            # one; cancelling or resubmitting from here would race it.
            # Coverage repair has ALREADY run this session — `_reconcile_stop_
            # coverage` executes earlier in the same position review than the
            # trails — so the gap is NOT closed by this session. It is closed
            # by the next intra sweep, which is why the owner is alerted.
            logger.error(
                "replace_stop_loss: after a dead replacement %s has NO resting "
                "protective stop — the position is UNPROTECTED, nothing was "
                "cancelled, and this session's coverage repair has already "
                "run, so the gap persists until the NEXT intra sweep",
                symbol,
            )
            return self._failed_amend_payload(symbol, legs)
        if len(amended) == len(legs):
            logger.info(
                "Trailing stop AMENDED IN PLACE for %s: %d leg(s) moved to "
                "$%.4f, quantities unchanged (no cancel, no unprotected window)",
                symbol, len(legs), price,
            )
            return {"id": amended[0]["new_id"],
                    "status": amended[0].get("status", "accepted"),
                    "amend_status": "accepted", "symbol": symbol, "legs": legs}
        if unknown:
            # The amend MAY have landed. Cancelling now could cancel a stop the
            # broker already moved, so the fallback must NOT run: take the
            # "refused, do not cancel" channel and let the next pass re-read.
            logger.error(
                "replace_stop_loss: %d of %d in-place amends for %s came back "
                "with NO broker answer — the desk does not know which level "
                "each leg is at; nothing cancelled, nothing written back, "
                "re-read the book before trailing %s again",
                len(unknown), len(legs), symbol, symbol,
            )
            return self._failed_amend_payload(symbol, legs)
        if amended:
            # NARROW HEAL (item 201, adversary round 3). Only the legs THIS
            # amend failed to move, and only to THIS proposal's intended
            # level. Deliberately NOT "the most protective level already
            # resting": per-lot stop levels are a design choice the desk
            # maintains (see `shift_stops_down`'s docstring on the audit-round-2
            # fix), so collapsing them would tighten a lot the desk chose to
            # keep wide — a worse failure than the straddle. A leg whose
            # outcome is unknown or naked is never retried, because the desk
            # does not know where it is.
            #
            # Exactly ONE retry, inside the trailing proposal's own gates
            # (ratchet floor, tightening cooldown, no-proposal-on-a-stalled-
            # price): nothing here re-attempts an unchanged outcome on a later
            # pass, so a leg the broker keeps refusing cannot spin.
            laggards = [l for l in legs if l["outcome"] == "refused"]
            if laggards:
                retried = {}
                for lag in laggards:
                    again = self._amend_one_stop_price(
                        symbol=symbol,
                        spec={"id": lag["id"], "qty": lag["qty"],
                              "stop_price": lag["old_stop"]},
                        new_price=price,
                        new_qty=new_qty,
                    )
                    again["retry_of"] = lag["id"]
                    retried[lag["id"]] = again
                legs = [retried.get(l["id"], l) for l in legs]
                amended = [l for l in legs if l["outcome"] == "amended"]
                unknown = [l for l in legs if l["outcome"] == "unknown"]
                if len(amended) == len(legs):
                    logger.info(
                        "replace_stop_loss: %s's lagging stop leg(s) came up to "
                        "$%.4f on one retry — all %d leg(s) now at the intended "
                        "level, nothing cancelled", symbol, price, len(legs),
                    )
                    return {"id": amended[0]["new_id"],
                            "status": amended[0].get("status", "accepted"),
                            "amend_status": "accepted", "symbol": symbol,
                            "legs": legs}
            if not amended:
                return self._failed_amend_payload(symbol, legs)
            logger.error(
                "replace_stop_loss: only %d of %d stop legs for %s moved to "
                "$%.4f; the rest were REFUSED and are still resting at their "
                "old levels even after one retry. Coverage is intact and "
                "nothing was cancelled, and the straddle is LEFT IN PLACE: "
                "pulling the laggards to another resting level would collapse "
                "per-lot geometry the desk maintains on purpose. The next "
                "accepted proposal moves them all together.",
                len(amended), len(legs), symbol, price,
            )
            return self._failed_amend_payload(symbol, legs)
        logger.warning(
            "replace_stop_loss: the broker REFUSED the in-place amend of all "
            "%d stop leg(s) for %s to $%.4f — the ORIGINAL stops are still "
            "resting, so protection is intact and nothing is cancelled",
            len(legs), symbol, price,
        )
        return self._failed_amend_payload(symbol, legs)
