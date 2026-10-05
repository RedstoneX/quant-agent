"""The owner-facing retraction of a stop-placement alarm that has cleared.

Lifted out of `OwnerAlerts` unchanged; the static method there delegates here.
"""

import logging

logger = logging.getLogger("src.pipeline")


def alert_owner_repair_resolved(symbols: list[str]) -> None:
        """Tell the owner a red stop-placement alarm he was sent today has
        CLEARED. Never raises.

        THE HALF THAT WAS MISSING. On 2026-09-23 the desk paged at 13:30:45
        — "COULD NOT PUT THE PROTECTIVE STOP BACK ... Those shares have
        nothing standing watch over them right now ... Place the missing
        stop by hand" — and put the stop back itself at 13:45:45. Nothing
        retracted it. The alarm was true for fifteen minutes and false for
        the rest of the day, and the owner's standing instruction was to go
        and do by hand a thing that was already done. An alarm that cannot
        clear is worse than one that never fired, because the next one is
        read as a stale one.

        Sent through `send_owner_alert`, the same path the alarm itself
        used, because a retraction that arrives somewhere else is not a
        retraction. Gated on `claim_repair_resolution_notice`, which returns
        only names the owner was ACTUALLY paged about today and has not
        already been told about: a position that never alerted produces no
        notice, so the ordinary daily re-placement of every fractional
        remainder — which happens to every such position every morning —
        stays silent.

        Deliberately does NOT release the placement-failure claim. That
        claim is what makes "Each position is reported at most once per
        trading day" true, and releasing it would let a name that fails,
        succeeds and fails again send two messages a cycle.
        """
        try:
            from src import notifier as _notifier
            from src.coverage_watchdog import (
                claim_repair_resolution_notice, repair_resolution_text,
            )

            fresh = claim_repair_resolution_notice(symbols)
            if not fresh:
                return
            _notifier.send_owner_alert(
                repair_resolution_text(fresh), symbols=sorted(fresh),
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("stop-repair resolution notice failed: %s", exc)
