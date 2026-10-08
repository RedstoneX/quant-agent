"""The out-of-band owner alert.

Moved verbatim from the former src/notifier.py; the package re-exports it.
"""

from __future__ import annotations

from src.notifier.base import (
    logger,
)
from src.notifier.owner_alert_funnel import (  # noqa: F401  (re-exported)
    _ALERT_NO_PNL_LINE, _with_pnl_header, build_default_notifier,
    send_owner_alert_with_outcome,
)
from src.notifier.transport import (  # noqa: F401
    TelegramNotifier,  # the funnel's default notifier; patched by tests HERE
)

# === Out-of-band owner alert ===


def send_owner_alert(
    text: str, *, symbols: list[str] | None = None, category: str | None = None,
) -> bool:
    """Push an alert to the owner NOW, outside the session-result message.

    Spec §11.1 guard 2. Some conditions cannot wait for a session to finish
    and be summarised: a position that is open at the broker with no
    protective stop on it is the canonical one. The end-of-session Telegram
    message is the wrong vehicle — an `intra_check` tick is silent unless it
    liquidates, so a naked position found at 12:30 would produce no message
    at all, and a session that crashes after the failure never sends one.

    Deliberately mirrors `src/cost_circuit.py`'s escalation shape, which is
    this desk's established owner-alert path: log at CRITICAL first so a
    Telegram outage cannot hide the event from the journal or Mission
    Control, then send. Returns whether the send succeeded; callers treat
    that as information, never as a reason to abort.

    Never raises. An alerting bug must not be able to break the trading path
    it is reporting on — see `alert_watchdog`'s "a watchdog that can break
    the thing it watches is worse than no watchdog".
    """
    notifier = None
    try:
        notifier = build_default_notifier(factory=TelegramNotifier)
    except Exception:  # noqa: BLE001
        from src.sentinel.counted import record_swallowed_here
        record_swallowed_here("notifier.owner_alert.send_owner_alert", log=logger)
        logger.exception("owner alert could not build its notifier")
        return False
    return send_owner_alert_with_outcome(
        text, notifier=notifier, symbols=symbols, category=category,
    )[0]
