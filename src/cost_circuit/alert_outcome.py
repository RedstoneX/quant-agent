"""Three-way outcome of one operational cost-circuit alert: delivered, suppressed, failed (PR #978)."""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

#: `alert_state` / `recovery_alert_state` / `suspension_alert_state` value for
#: "the desk deliberately DROPPED this alert" -- TELEGRAM_RISK_ONLY filtered an
#: operational message, or TELEGRAM_DISABLED muted everything. A THIRD state,
#: never folded into 1 (delivered): a dropped message was not delivered, and
#: saying it was is a false statement on the owner's own surface. 0 is "not
#: sent, still retryable", -1 is "in flight", and 2 is already taken on
#: `recovery_alert_state` for "unpaired", so suppression is 3.
ALERT_STATE_SUPPRESSED = 3


def _send_alert_outcome(
    notifier: Any,
    message: str,
    log_label: str,
) -> tuple[bool, bool]:
    """Send one OPERATIONAL cost-circuit alert. Returns (delivered, suppressed).

    Both False means a real, RETRYABLE failure. A deliberate drop is
    SETTLED -- never retried (retrying a filter never succeeds and writes a
    fresh suppression row each time) and never recorded as delivered. The
    drop itself is already durably recorded in `notifier_sends`.

    Never raises: a notifier fault must not affect trading or safety.
    """

    try:
        from src.notifier import CATEGORY_OPERATIONAL
        from src.notifier.owner_alert import send_owner_alert_with_outcome

        return send_owner_alert_with_outcome(
            message,
            notifier=notifier,
            category=CATEGORY_OPERATIONAL,
            kind="cost_circuit",
            max_attempts=1,
            pnl_header=False,
        )
    except Exception:
        logger.exception(log_label)
        return False, False


def _alert_state_value(delivered: bool, suppressed: bool) -> int:
    """The three-way `alert_state` for one send outcome."""

    if delivered:
        return 1
    if suppressed:
        return ALERT_STATE_SUPPRESSED
    return 0
