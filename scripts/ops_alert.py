"""The one way an ops script pushes a message to the owner.

Every monitor under ``scripts/`` used to build its own ``TelegramNotifier``
and call ``send`` once: no retry, no counted undelivered row, so a lost
alert read as a delivered one (docs/SPLIT_DEFERRED_FINDINGS.md, owner-alert
senders). Scripts call this instead; it hands the text to the same retry
funnel every ``send_owner_alert`` uses. No P&L header: that belongs on money
alerts, not ops reports. The category is left unclassified, exactly as the
bare sends were, so nothing is newly filtered.
"""
from __future__ import annotations

import sys


def build_notifier():
    """The same `TelegramNotifier` every alarm on this desk uses.

    Constructed with no arguments on purpose: it reads the environment
    exactly as the shutdown/hold alerts do, so a probe failure here is a
    real alarm failure and not an artifact of a differently-built notifier.
    """
    from src.notifier import TelegramNotifier

    return TelegramNotifier()


def push_ops_alert(
    message: str, *, kind: str, note: str = "alert printed above only", notifier=None,
) -> bool:
    """Deliver ``message`` with retry under ``kind``; False when not delivered.

    A notifier that is not configured is reported on stderr and is not a
    delivery, so the caller's own printed copy stays the record.
    """
    from src.notifier.owner_alert_delivery import deliver_with_retry

    notifier = build_notifier() if notifier is None else notifier
    if not notifier.enabled:
        print(f"{kind}: Telegram not configured; {note}", file=sys.stderr)
        return False
    return bool(deliver_with_retry(notifier, message, kind=kind))
