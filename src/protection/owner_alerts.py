"""Owner alerts for stop coverage: the Telegram/feed messages the desk files when a protective stop is missing, unfilled, unreadable, pending, repaired or declined.

Lifted verbatim out of `ProtectionMixin` (src/pipeline_protection.py) as a
standalone class: every collaborator is an explicit keyword-only
constructor argument, so it is built and exercised without a pipeline.
The mixin keeps a thin method of the same name that builds this object
and calls it, so every existing caller and patch target is unchanged.
"""

import logging

from src.protection.owner_alert_repair_resolved import alert_owner_repair_resolved
from src.sentinel.guarded import record_guarded_pass

#: Logs under `src.pipeline`, as the bodies did before the move;
#: binding the name rather than `__name__` keeps log records byte-identical.
logger = logging.getLogger("src.pipeline")


class OwnerAlerts:
    """Owner alerts for stop coverage: the Telegram/feed messages the desk files when a protective stop is missing, unfilled, unreadable, pending, repaired or declined."""

    def __init__(self, *, broker, still_uncovered, alert_owner_no_stop, format_qty) -> None:
        self.broker = broker
        self._still_uncovered = still_uncovered
        self._alert_owner_no_stop = alert_owner_no_stop
        self._format_qty = format_qty

    def _still_uncovered(self, gap: dict) -> bool:
        """Is this position STILL short of stop coverage, read fresh from
        the broker? Unreadable answers True — an unprotected position is
        the one thing this desk cannot go quiet about on a bad read.
        """
        symbol = str(gap.get("symbol") or "").strip()
        if not symbol:
            return True
        try:
            held = abs(float(gap.get("held_qty") or 0))
            _ok, specs = self.broker.snapshot_protective_stops(
                symbol,
                side=("buy" if gap.get("is_short") else "sell"),
            )
            covered = sum(float(s.get("qty", 0) or 0) for s in (specs or []))
            record_guarded_pass(self.broker, "owner_alerts.still_uncovered_reread")
        except Exception as exc:  # noqa: BLE001
            record_guarded_pass(
                self.broker,
                "owner_alerts.still_uncovered_reread",
                exc,
                log=logger,
                context={"symbol": symbol, "effect": "alerting anyway"},
            )
            logger.warning(
                "could not re-read stops for %s before alerting (%s) — alerting anyway",
                symbol,
                exc,
            )
            return True
        if covered + 1e-6 >= held:
            logger.info(
                "%s is fully stop-covered (%.4f of %.4f) by the time the "
                "alert was about to go out — another process placed it. Not "
                "paging the owner about a failure that succeeded.",
                symbol,
                covered,
                held,
            )
            return False
        return True

    @staticmethod
    def _alert_owner_elected_unfilled(rows: list[dict]) -> None:
        """Tell the owner a protective stop FIRED and did NOT fill. Never
        raises.

        Distinct from PR #514's alert (a stop the desk could not PLACE):
        this stop exists, is sized, and did not execute. It takes its own
        per-position per-day claim; a shared key would let either
        condition silence the other.

        Wording: `src.trader_feed.format_coverage_gap_line`."""
        try:
            from src import notifier as _notifier
            from src.coverage_alert_release import release_elected_unfilled_alert
            from src.coverage_watchdog import claim_elected_unfilled_alert
            from src.trader_feed import _profiles, format_coverage_gap_line

            symbols = [str(r.get("symbol")).strip() for r in rows if str(r.get("symbol") or "").strip()]
            fresh = set(claim_elected_unfilled_alert(symbols))
            if not fresh:
                logger.info(
                    "Elected-but-unfilled protective stop on %s already "
                    "reported to the owner today \u2014 not paging again.",
                    ", ".join(symbols) or "(unnamed)",
                )
                return
            send = [r for r in rows if str(r.get("symbol") or "").strip().upper() in fresh]
            try:
                profiles = _profiles(send)
            except Exception:  # noqa: BLE001
                profiles = None
            detail = "\n".join(format_coverage_gap_line(row, profiles) for row in send)
            sent_ok = _notifier.send_owner_alert(
                "\U0001f534 A PROTECTIVE STOP FIRED AND DID NOT FILL\n"
                f"{len(send)} position(s) have traded past their protective "
                "stop while that stop's order is still sitting unfilled at "
                "the broker. Those shares have nothing standing watch over "
                "them right now, even though a stop still shows as live. "
                "This is not a missing stop and not the expected overnight "
                "lapse on a part-share.\n"
                f"{detail}\n"
                "Nothing was sold, cancelled or replaced. Sell by hand, or "
                "move the stop, if you want out of those shares. Each "
                "position is reported at most once per trading day.",
                symbols=sorted(fresh),
            )
            if not sent_ok:
                # Claimed BEFORE the send: roll back so the next run retries.
                released = release_elected_unfilled_alert(fresh)
                logger.critical(
                    "Stop-unfilled alert for %s NOT delivered; claim %s",
                    sorted(fresh),
                    "rolled back" if released else "ROLLBACK FAILED",
                )
        except Exception as exc:  # noqa: BLE001
            logger.error("elected-but-unfilled stop owner alert failed: %s", exc)

    def _alert_owner_session_repair_failed(self, failures: list[dict]) -> None:
        """Tell the owner a protective stop could not be put back while the
        market was OPEN. Never raises.

        Deliberately NOT the overnight fractional lapse. That one is owner-
        ratified, bounded and happens to every fractional position every
        night — the broker accepts fractional orders only on DAY
        time-in-force, so the sub-share stop dies at 16:00 by design. It is
        stamped 'fractional_overnight' well before here, is reported as a
        measured number rather than an interruption, and must stay silent: a
        quiet expected state that starts paging is how a channel gets tuned
        out.

        The wording is `src.trader_feed.format_coverage_gap_line` — the same
        bullet the session feed renders — so the alert and the feed cannot
        describe one position two ways.

        The broker is re-read for each failing position immediately before
        sending, and one that turns out to be covered after all is dropped.
        Two processes have already been seen running this same repair
        concurrently (2026-09-16, BRK-B: orders 23 ms apart) and the loser's
        retry loop reported FAILURE on an order that had in fact landed.
        Paging the owner about a failure that succeeded is its own defect,
        and the standalone watchdog already re-reads before it ACTS for the
        same reason. Same epsilon, no new threshold, no retry.
        """
        try:
            from src import notifier as _notifier
            from src.coverage_watchdog import claim_repair_failure_alert
            from src.trader_feed import _profiles, format_coverage_gap_line

            still_open = [g for g in failures if self._still_uncovered(g)]
            if not still_open:
                return
            symbols = [str(g.get("symbol")).strip() for g in still_open if str(g.get("symbol") or "").strip()]
            fresh = set(claim_repair_failure_alert(symbols))
            if not fresh:
                logger.info(
                    "Session stop-repair failure on %s already reported to the owner today — not paging again.",
                    ", ".join(symbols) or "(unnamed)",
                )
                return
            rows = [g for g in still_open if str(g.get("symbol") or "").strip().upper() in fresh]
            try:
                profiles = _profiles(rows)
            except Exception:  # noqa: BLE001
                profiles = None
            detail = "\n".join(format_coverage_gap_line(row, profiles) for row in rows)
            _notifier.send_owner_alert(
                "🔴 COULD NOT PUT THE PROTECTIVE STOP BACK\n"
                f"{len(rows)} position(s) lost part of their protective stop "
                "while the market was OPEN, and the desk tried to place the "
                "missing stop and failed. Those shares have nothing standing "
                "watch over them right now. This is not the expected "
                "overnight lapse on a part-share.\n"
                f"{detail}\n"
                "Nothing was sold, resized or cancelled. Place the missing "
                "stop by hand — a stop over a part-share has to be a "
                "day-only order — or close the position. Each position is "
                "reported at most once per trading day.",
                symbols=sorted(fresh),
            )
        except Exception as exc:  # noqa: BLE001
            record_guarded_pass(self.broker, "owner_alerts.session_repair_alert", exc, log=logger)
            logger.error("session stop-repair owner alert failed: %s", exc)

    @staticmethod
    def _alert_owner_repair_resolved(symbols: list[str]) -> None:
        """Retract a stop-placement alarm that has cleared (src/protection/owner_alert_repair_resolved.py)."""
        alert_owner_repair_resolved(symbols)

    @staticmethod
    def _alert_owner_no_stop(naked: list[dict]) -> None:
        """Push the NO-STOP-AT-ALL escalation to the owner. Never raises."""
        try:
            from src import notifier as _notifier
            from src.coverage_watchdog import (
                claim_typed_alert,
                release_typed_alert,
            )

            fresh: set[str] = set()
            # DEFECT 6 (adversary round 3). This escalation had no claim of
            # its own while every neighbouring one -- unreadable stop,
            # repair failure, elected-but-unfilled, kill-switch block --
            # claims the symbol for the trading day on the shared state
            # file. Four callers reach it (the coverage sweep at every
            # session entry, the standalone watchdog every thirty minutes,
            # the reprotect drain, and the in-flight branch added for defect
            # 3), `send_owner_alert` has no throttle of its own, and a
            # position that stays naked stays naked -- so the same true
            # statement could be sent without limit until the owner stops
            # reading it. Same `kind`-scoped claim, same per-symbol
            # per-trading-day discipline, same fail-towards-telling-him-
            # twice behaviour when the state file cannot be read. A gap
            # carrying no symbol cannot be claimed and is always sent.
            claimable = sorted(
                {str(g.get("symbol", "")).strip().upper() for g in naked if str(g.get("symbol", "")).strip()}
            )
            if claimable:
                fresh = set(claim_typed_alert("no_stop_at_all", claimable))
                if not fresh:
                    return
                naked = [
                    g
                    for g in naked
                    if str(g.get("symbol", "")).strip().upper() in fresh or not str(g.get("symbol", "")).strip()
                ]

            # The refusal REASON, not just the shortfall (docs/WORK.md item
            # 88). "The automatic repair could not restore one" was true of a
            # corrupt recorded stop, a level the tape has already passed and
            # an exhausted broker retry alike — three states with three
            # different owner actions. `repair_refusal` is stamped by
            # `repair_stop_coverage` and omitted when it has nothing to say.
            detail = "\n".join(
                f"  {g.get('symbol', '?')}: held {g.get('held_qty')}, "
                f"covered {g.get('covered_qty')}" + (f" — {g['repair_refusal']}" if g.get("repair_refusal") else "")
                for g in naked
            )
            landed = _notifier.send_owner_alert(
                # Item 21b: shape, not colour — the owner is red-green
                # colour blind, so severity is carried by HOW MANY marks
                # there are, not which one. This alert used a single 🔴
                # while the unreadable-stop alert added alongside it uses
                # 🛑🛑, which ranked the strictly worse condition (there is
                # NOTHING standing watch) below the weaker one (we could not
                # find out whether anything is). Three marks here, matching
                # the top tier `src/notifier.py` already renders.
                "🛑🛑🛑 NO STOP AT ALL\n"
                f"{len(naked)} position(s) are open at the broker with ZERO "
                "protective-stop coverage, and the automatic repair could not "
                "restore one. This is not a mis-sized stop — there is nothing "
                "standing watch.\n"
                f"{detail}\n"
                "Place a protective stop manually or flatten the position.",
                symbols=[str(g.get("symbol")) for g in naked if g.get("symbol")],
            )
            # FAULT 2 (adversary round 4). The claim above is saved before
            # the send is attempted and was kept whether or not it landed,
            # so a muted or failed delivery burned the symbol's one page for
            # the whole trading day and nothing retried. Telegram is MUTED
            # on this desk today, which turns that from a rare case into the
            # normal one. `send_owner_alert` logs at CRITICAL before it
            # sends, so the journal record survives either way -- but the
            # CLAIM must not, or the 30-minute watchdog and the next session
            # entry both find the symbol already spoken for and say nothing.
            if claimable and not landed:
                release_typed_alert("no_stop_at_all", sorted(fresh))
        except Exception as exc:  # noqa: BLE001
            logger.error("no-stop owner alert failed: %s", exc)

    @staticmethod
    def _alert_owner_stop_pending_acceptance(
        symbol: str,
        residual_qty: str,
        order_id: str,
        status: str,
        stop_price: float,
    ) -> None:
        """Page the owner that a replacement protective stop has been
        RECEIVED by the broker but is not yet working. Never raises.

        FAULTS 2 and 3 (adversary round 4). This condition first borrowed
        `_alert_owner_no_stop`, and that was wrong twice over.

        It was UNTRUE. That message reads "NO STOP AT ALL ... there is
        nothing standing watch", and here the broker holds the order -- it
        simply has not routed it yet, and it may still be rejected. The
        desk's standing rule is to report the true state, never an
        approximation of it that sounds more urgent; a false alert is a
        root-cause defect in its own right.

        And it SILENCED the page that matters. `no_stop_at_all` is claimed
        per symbol per trading day. A benign pending stop at 09:35 took the
        symbol's only claim, so when that same order was later rejected and
        the position really was naked, the coverage sweep and the
        thirty-minute watchdog both found the claim held and told nobody.
        The two conditions therefore get two keys: an unconfirmed stop can
        never consume the claim belonging to no stop at all.

        Like every other page here it claims the symbol first so two
        processes cannot both send, and hands the claim back when the send
        does not land -- which on this desk today is every time, Telegram
        being muted. `send_owner_alert` logs at CRITICAL before sending, so
        the journal keeps the record regardless.
        """
        try:
            from src import notifier as _notifier
            from src.coverage_watchdog import (
                claim_typed_alert,
                release_typed_alert,
            )

            sym = str(symbol).strip().upper()
            if not sym:
                return
            if not claim_typed_alert("stop_pending_acceptance", [sym]):
                return
            # Two marks, not three: `_alert_owner_no_stop` uses three for
            # "there is nothing standing watch", and this is the strictly
            # weaker condition -- an order exists and may yet work. Severity
            # is carried by how many marks there are (item 21b, the owner is
            # red-green colour blind), so ranking this below the real thing
            # is the point.
            landed = _notifier.send_owner_alert(
                "🛑🛑 PROTECTIVE STOP NOT YET WORKING\n"
                f"{sym}: a replacement protective stop for {residual_qty} "
                f"share(s) at ${stop_price:.2f} (order {order_id}) has been "
                f"RECEIVED by the broker and is still {status} — accepted "
                "into the book but not yet routed, and an order in that "
                "state can still be rejected.\n"
                "This is NOT a confirmed naked position and it is NOT "
                "confirmed protection: the desk did not place a second stop "
                "over it, because two live stops on one position sell the "
                "shares twice. The recovery intent is kept and the next "
                "pass re-reads the order's status.\n"
                "Check that the order reached working state; if it was "
                "rejected, place a protective stop manually or flatten.",
                symbols=[sym],
            )
            if not landed:
                release_typed_alert("stop_pending_acceptance", [sym])
        except Exception as exc:  # noqa: BLE001
            logger.error("pending-stop owner alert failed: %s", exc)

    @staticmethod
    def _alert_owner_unreadable_stop(rows: list[dict]) -> None:
        """Page the owner, BY SYMBOL, about positions whose protective stops
        could not be READ at the broker. Board item 172. Never raises.

        Same path a missing stop uses (`notifier.send_owner_alert` with the
        symbols attached), because it is the same question — does this
        position have loss protection — with the answer "unknown" instead of
        "no". Removing the account-level loss alarm made per-position stops
        the only protection the desk has, so an unanswerable question about
        one of them is worth the owner's attention, not a log line.

        Deduped per symbol per trading day on the SAME state file and the
        SAME claim discipline as the placement-failure and elected-unfilled
        alerts, and shared with the standalone coverage watchdog: this sweep
        runs at every session entry and the watchdog every thirty minutes,
        both can find the identical condition, and `send_owner_alert` has no
        throttle of its own. Whichever process sees the symbol first is the
        one that tells him.
        """
        try:
            from src import notifier as _notifier
            from src.coverage_watchdog import (
                UnreadableStop,
                claim_unreadable_stop_alert,
                unreadable_stop_text,
            )

            by_symbol = {str(r.get("symbol", "")).strip().upper(): r for r in rows if str(r.get("symbol", "")).strip()}
            fresh = claim_unreadable_stop_alert(list(by_symbol))
            if not fresh:
                return
            described = []
            for sym in fresh:
                row = by_symbol.get(sym, {})
                try:
                    held = abs(float(row.get("held_qty") or 0))
                except (TypeError, ValueError):
                    held = 0.0
                described.append(
                    UnreadableStop(
                        symbol=sym,
                        held_qty=held,
                        reason=str(row.get("read_error") or "reason not recorded"),
                        is_short=bool(row.get("is_short")),
                    )
                )
            delivered = _notifier.send_owner_alert(
                unreadable_stop_text(described),
                symbols=fresh,
            )
            if not delivered:
                # The claim was already recorded, so these symbols are now
                # silent for the rest of the trading day. Releasing the
                # claim would trade one lost message for a page on every
                # 30-minute tick, so the delivery failure is made loud in
                # the journal instead of being a discarded return value.
                logger.error(
                    "UNREADABLE-STOP ALERT NOT DELIVERED for %s — the "
                    "finding stands and is claimed for today; read it here.",
                    ", ".join(fresh),
                )
        except Exception as exc:  # noqa: BLE001
            logger.error("unreadable-stop owner alert failed: %s", exc)

    @staticmethod
    def _alert_owner_exit_declined(symbol: str, *, side: str, why: str) -> None:
        """Page the owner, BY SYMBOL, about an exit the desk decided on and
        then did not place. Never raises.

        The skip itself is old behaviour made reachable by board item 172:
        `snapshot_protective_stops` could not return `ok=False` before it,
        so the branch that consumes it had never executed in production.
        What is new is that it no longer disappears. Every one of the five
        upstream call sites does `if sale is None: continue`, with no trade
        row, no session-result field and no message — so the desk could
        decide to leave a position, fail, and report a quiet day. One of
        those call sites is the gross-exposure de-levering ladder, which
        after the account-level halt's removal is one of the few remaining
        account-wide protections; silently trimming less than it reports is
        the failure this closes.

        Same notifier path, same claim discipline and the same
        once-per-symbol-per-trading-day bound as the unreadable-stop alert,
        on its own state key so neither condition can silence the other.
        """
        try:
            from src import notifier as _notifier
            from src.coverage_watchdog import (
                claim_exit_declined_alert,
                exit_declined_text,
            )

            name = str(symbol or "").strip().upper()
            if not name:
                return
            if not claim_exit_declined_alert([name]):
                return
            delivered = _notifier.send_owner_alert(
                exit_declined_text(name, side=side, why=why),
                symbols=[name],
            )
            if not delivered:
                # The claim is already recorded, so this symbol is silent
                # for the rest of the day. Same trade-off the unreadable
                # alert documents: releasing it would page on every tick.
                logger.error(
                    "EXIT-DECLINED ALERT NOT DELIVERED for %s (%s %s) — the "
                    "finding stands and is claimed for today; read it here.",
                    name,
                    side.upper(),
                    why,
                )
        except Exception as exc:  # noqa: BLE001
            logger.error("exit-declined owner alert failed: %s", exc)

    def _alert_owner_reprotect_left_naked(
        self,
        symbol: str,
        residual_qty: float,
        covered_qty: float,
        reason: str,
    ) -> None:
        """Page the owner when reprotect ends with the residual UNPROTECTED.

        INCIDENT 2026-09-30: reprotect wrongly skipped as an "idempotent
        re-run", the recovery intent was deleted, and the only trace was a
        single INFO log line. The desk's rule is that a position left
        without a stop is never a log line only, so every path out of
        `_reprotect_residual` that does NOT end with a live stop now goes
        down the SAME `_alert_owner_no_stop` escalation the coverage sweep
        uses — no new channel, no new throttle, and the `repair_refusal`
        field carries the reason so the owner knows which of the three
        different actions to take. Never raises: the SELL already
        succeeded and a failed page must not unwind it.

        `covered_qty` is what the broker is ACTUALLY watching, passed in by
        the caller. It was hardcoded to 0 when this helper was written,
        which told the owner a whole-share leg that HAD landed did not
        exist -- an untrue statement about how exposed he is, on the one
        alert whose entire job is to state that exposure. Nothing here
        assumes; it reports the number the submit path returned.
        """
        try:
            held = float(residual_qty or 0.0)
            covered = max(0.0, min(float(covered_qty or 0.0), held))
            self._alert_owner_no_stop(
                [
                    {
                        "symbol": symbol,
                        "held_qty": self._format_qty(held),
                        "covered_qty": self._format_qty(covered),
                        "repair_refusal": f"re-protect after partial exit: {reason}",
                    }
                ]
            )
        except Exception as exc:  # noqa: BLE001
            record_guarded_pass(
                self.broker, "owner_alerts.reprotect_naked_alert", exc, log=logger, context={"symbol": symbol}
            )
            logger.error(
                "reprotect naked-position alert failed for %s: %s",
                symbol,
                exc,
            )
