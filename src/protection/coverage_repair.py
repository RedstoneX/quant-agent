"""src.protection.coverage_repair -- the blind stop-coverage repair and the kill-switch block recorder.

Bodies moved verbatim from src/pipeline_protection.py (`ProtectionMixin`), which keeps
same-named thin shims built per call. Every collaborator is an explicit keyword-only
constructor argument, so this builds and runs with no pipeline behind it. Collaborators
named after a sibling body are the HOST's shim, handed in, never a body this part owns.
"""

import logging

#: The moved code logged under `src.pipeline` before the move and still does;
#: binding the name rather than `__name__` keeps log records byte-identical.
logger = logging.getLogger("src.pipeline")


class CoverageRepair:
    """The blind stop-coverage repair and the kill-switch block recorder; standalone, built from explicit collaborators."""

    def __init__(
        self, *,
        broker=None,
        db=None,
        alert_owner_kill_switch_blocked=None,
    ) -> None:
        self.broker = broker
        self.db = db
        if alert_owner_kill_switch_blocked is not None:
            self._alert_owner_kill_switch_blocked = alert_owner_kill_switch_blocked  # else: this part's own body

    def _repair_stop_coverage(
        self, symbol: str, uncovered_qty: float, *, is_short: bool,
        outcome: dict | None = None, resting_stops: list | None = None,
    ) -> bool:
        """Best-effort: re-place protective stop coverage on an uncovered
        position using the stop level recorded on its last opening row
        (BUY for a long, SHORT for a short). Returns True when the gap
        was actually closed.

        `outcome`, when given, is the caller's gap dict: a refusal stamps
        `repair_refusal` on it with a plain sentence saying WHY nothing was
        placed (docs/WORK.md item 88). Before that, the caller received a
        bare False and the owner alert could only say the repair "could not
        restore one" — a corrupt recorded stop, a level already through the
        tape and three exhausted broker retries all read identically.

        THE BODY MOVED to `src.execution.stop_repair.repair_stop_coverage`
        and this is now a delegate — see that module for the whole design,
        including why the recorded opening level is not a policy invention and
        why the fractional split needs no special case here. It moved
        because `src/coverage_watchdog.py` needs the SAME re-placement when
        it finds an uncovered sub-share remainder while the trading timers
        are stopped (docs/INCIDENT_HISTORY.md, item 53), and a second copy
        of an order-placement path is how one behaviour ends up with two
        homes. The caller still owns the decision of WHETHER to call this at
        the current hour.
        """
        from src.execution.stop_repair import repair_stop_coverage

        opening = "SHORT" if is_short else "BUY"
        return repair_stop_coverage(
            broker=self.broker,
            # include_in_flight: a same-session open still at fill_status=
            # 'submitted' is the row whose stop we want — under the strict
            # executed predicate the repair either no-op'd or read a months-
            # old prior row's stop level (audit round 2).
            last_buy=lambda sym, action=opening: self.db.get_symbol_last_buy(
                sym, include_in_flight=True, action=action,
            ),
            symbol=symbol,
            uncovered_qty=uncovered_qty,
            is_short=is_short,
            db=self.db,
            outcome=outcome,
            resting_stops=resting_stops,
            caller="session_coverage_reconcile",
        )

    def _wire_protective_stop_block_recorder(self) -> None:
        """The broker holds no database, so a protective stop its kill
        switch refuses is recorded through this pipeline's one
        (`kind='protective_stop_blocked'`, `src/execution/exit_path_records.py`).
        Also pages the owner (`_alert_owner_kill_switch_blocked`) — a
        recorded row nobody reads is not an alert, and until this was
        wired a kill-switch refusal left the position naked with no owner
        notice at all. `self.db` is read at call time, not captured, so a
        later swap of the handle is honoured."""
        from src.execution.exit_path_records import record_protective_stop_blocked

        def _on_blocked(**facts) -> None:
            record_protective_stop_blocked(self.db, **facts)
            self._alert_owner_kill_switch_blocked(**facts)

        self.broker.protective_stop_block_recorder = _on_blocked

    @staticmethod
    def _alert_owner_kill_switch_blocked(
        *, symbol: str, qty: float = 0.0, stop_price: float = 0.0,
        side: str = "", kill_switch_path: str = "", **_ignored,
    ) -> None:
        """Page the owner the first time today the desk's own kill switch
        blocks a protective stop for `symbol`. Never raises — see
        `AlpacaBroker._submit_stop_limit_order`, which already swallows
        whatever this callback does.

        Deduped per symbol per trading day (`claim_kill_switch_block_alert`)
        the same way the repair-failure and elected-unfilled alerts are: a
        kill switch left on all session would otherwise page once per
        retry of every symbol it touches.
        """
        try:
            from src import notifier as _notifier
            from src.coverage_watchdog import claim_kill_switch_block_alert
            from src.execution.exit_path_records import kill_switch_blocked_text

            fresh = claim_kill_switch_block_alert([symbol])
            if not fresh:
                return
            _notifier.send_owner_alert(
                "🔴 KILL SWITCH BLOCKED A PROTECTIVE STOP\n"
                f"{kill_switch_blocked_text(symbol)}\n"
                f"qty={qty} side={side} stop=${stop_price}\n"
                "Nothing was sent to the broker, so nothing is standing "
                "watch over this position right now. Turn off the kill "
                "switch and place the stop by hand, or flatten the "
                "position. Reported at most once per trading day.",
                symbols=fresh,
            )
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "kill-switch-block owner alert failed for %s: %s", symbol, exc,
            )
