"""src.protection.ex_dividends -- the ex-dividend stop shift.

Bodies moved verbatim from src/pipeline_protection.py (`ProtectionMixin`), which keeps
same-named thin shims built per call. Every collaborator is an explicit keyword-only
constructor argument, so this builds and runs with no pipeline behind it. Collaborators
named after a sibling body are the HOST's shim, handed in, never a body this part owns.
"""

import logging
from src.trading_calendar import et_today

#: The moved code logged under `src.pipeline` before the move and still does;
#: binding the name rather than `__name__` keeps log records byte-identical.
logger = logging.getLogger("src.pipeline")


class ExDividends:
    """The ex-dividend stop shift; standalone, built from explicit collaborators."""

    def __init__(
        self, *,
        broker=None,
        db=None,
        market=None,
        repair_stop_coverage=None,
    ) -> None:
        self.broker = broker
        self.db = db
        self.market = market
        self._repair_stop_coverage = repair_stop_coverage

    def _handle_ex_dividends(self, positions, run_id: str) -> list[dict]:
        """Lower stops by the upcoming dividend amount the day before ex-div.

        On ex-div day, the stock's open drops by approximately the dividend
        per share — a mechanical move, not a thesis break. A tight stop set
        against normal price action can trigger for no real reason and kick
        us out of a winner. This runs at midday the day BEFORE ex-div and
        lowers each relevant position's stop by the dividend amount so the
        mechanical gap doesn't touch it.

        Idempotent per ET date: if we already adjusted this symbol today
        (tagged 'ex-div' in reasoning), skip. Detects "tomorrow is ex-div"
        in ET.
        """
        from datetime import timedelta as _td
        orders: list[dict] = []
        today = et_today()
        # NEXT TRADING day, not calendar tomorrow (2026-07-16 audit): sessions
        # only run Mon-Fri, so `today + 1 day` can never BE a Monday — every
        # Monday ex-div silently went unadjusted, and Friday's sessions (the
        # last chance to act) computed Saturday. Same hole for any ex-div the
        # day after a holiday. Fall back to calendar+1 if the calendar lookup
        # fails — degrading to today's behavior beats crashing the session.
        next_trading_day = today + _td(days=1)
        for _ in range(7):
            try:
                if self.broker.is_trading_day(next_trading_day):
                    break
            except Exception as e:  # noqa: BLE001
                logger.warning("ex-div: is_trading_day failed (%s) — falling back "
                               "to calendar+1", e)
                next_trading_day = today + _td(days=1)
                break
            next_trading_day += _td(days=1)

        for p in positions:
            # Deliberately long-only, not just "not yet generalised" — a
            # short OWES the dividend to the share lender (a cash liability)
            # rather than receiving it, so there is no mechanical gap-down
            # here for a stop-shift to absorb. See broker.shift_stops_down's
            # docstring for the fuller reasoning (shorts-safe, Stage 2).
            if p.qty <= 0:
                continue
            # Check today's trades for a prior ex-div adjustment — idempotent
            try:
                today_trades = self.db.get_trades(
                    symbol=p.symbol, today_only=True, limit=20,
                )
            except Exception as e:
                logger.warning("ex-div: today trades lookup failed for %s: %s", p.symbol, e)
                continue
            already = any(
                (t.get("action") or "").upper() == "TRAIL_STOP"
                and "ex-div" in (t.get("reasoning") or "").lower()
                for t in today_trades
            )
            if already:
                continue

            try:
                div = self.market.get_upcoming_ex_dividend(p.symbol)
            except Exception as e:
                logger.warning("ex-div: fetch failed for %s: %s", p.symbol, e)
                continue
            if not div:
                continue
            div_date = div.get("date")
            if not (div_date and today < div_date <= next_trading_day):
                # Only act on the session BEFORE ex-div. On ex-div day itself
                # the gap has already happened at open — adjustment is too
                # late — and "day after" is wrong (the stock is re-pricing
                # back to normal vol). The window is (today, next_trading_day]
                # so a Monday ex-div is caught by Friday's sessions.
                continue
            amount = div.get("amount") or 0
            if amount <= 0:
                continue

            from src.execution.stop_read import read_stop, repair_for
            stop_read = read_stop(self.broker, p.symbol, db=self.db,
                                  run_id=run_id, context="ex-div shift", establish=repair_for(self._repair_stop_coverage, p))
            if stop_read.unreadable or stop_read.absent:
                continue  # unreadable was recorded+alerted; absent = nothing to adjust
            current_stop = stop_read.price
            new_stop = round(current_stop - amount, 2)
            if new_stop <= 0 or new_stop >= p.current_price:
                logger.warning(
                    "ex-div: %s skipped — new_stop $%.2f not protective vs current $%.2f",
                    p.symbol, new_stop, p.current_price,
                )
                continue
            try:
                # Shift EVERY stop down by the dividend, preserving per-lot
                # levels/qty (audit round 2: with per-BUY GTC stops a
                # consolidating replace could TIGHTEN a wide lot's stop to
                # the tightest lot's level minus the dividend).
                order = self.broker.shift_stops_down(p.symbol, amount)
            except Exception as e:
                logger.error("ex-div: stop shift failed for %s: %s", p.symbol, e)
                continue
            from src.execution.stop_records import accepted_stop_order, write_back_stop_loss
            if isinstance(order, dict):
                # Item 201: the per-leg outcome is a ROW, not a log line, and it
                # is written whatever the outcome — a shift that refused is the
                # case that most needs to survive the session.
                from src.execution.exit_path_records import (
                    record_stop_shift_legs, stop_shift_incomplete_text,
                )
                shift_status = str(order.get("status") or "")
                record_stop_shift_legs(
                    self.db, symbol=p.symbol, amount=amount,
                    mode=str(order.get("mode") or ""), status=shift_status,
                    shifted=int(order.get("shifted") or 0),
                    total=int(order.get("total") or 0),
                    legs=order.get("legs"), run_id=run_id,
                )
                if shift_status in ("partial", "refused", "unknown", "naked", "market_closed"):
                    # An un-shifted stop across an ex-dividend open is wrong by
                    # exactly the dividend IN THE DIRECTION THAT TRIGGERS IT, so
                    # this is an owner-visible change in protection, not a nit.
                    try:
                        from src.notifier import send_owner_alert
                        send_owner_alert(
                            stop_shift_incomplete_text(
                                p.symbol, shift_status,
                                int(order.get("shifted") or 0),
                                int(order.get("total") or 0),
                            ),
                            symbols=[p.symbol],
                        )
                    except Exception as e:  # noqa: BLE001
                        logger.warning("ex-div: owner alert failed for %s: %s", p.symbol, e)
            if not order or (
                isinstance(order, dict) and not accepted_stop_order(order)
            ):
                # A partial, a refusal or an unknown carries no order id, so no
                # stop level is written back and no TRAIL_STOP row is filed —
                # the desk must not record a stop it did not confirm moving.
                continue
            try:
                write_back_stop_loss(self.db, p.symbol, new_stop, is_short=False)
            except Exception as e:  # noqa: BLE001
                logger.warning("ex-div: stop write-back failed for %s: %s", p.symbol, e)
            try:
                self.db.insert_trade(
                    symbol=p.symbol, action="TRAIL_STOP", qty=p.qty,
                    price=new_stop,
                    reasoning=(
                        f"ex-div adjustment: ex-div {div['date']}, div ${amount:.4f}/share. "
                        f"Shifted {order.get('shifted', '?')} stop(s) down by the dividend "
                        f"(highest {current_stop:.2f} → {new_stop:.2f}) to absorb the "
                        f"mechanical open gap."
                    ),
                    run_id=run_id,
                    stop_loss=new_stop,
                    broker_order_id=order.get("id"),
                    fill_status="submitted",
                )
            except Exception as e:
                logger.warning("ex-div: audit log failed for %s: %s", p.symbol, e)
            if isinstance(order, dict):
                order.setdefault("action", "TRAIL_STOP")  # audit F5
            orders.append(order)
            logger.info(
                "Ex-div adjust: %s ex-div %s div $%.4f → stop $%.2f → $%.2f",
                p.symbol, div["date"], amount, current_stop, new_stop,
            )
        return orders
