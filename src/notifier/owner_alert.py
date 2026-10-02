"""The out-of-band owner alert.

Moved verbatim from the former src/notifier.py; the package re-exports it.
"""

from __future__ import annotations

from src.notifier.base import (
    logger,
)
from src.notifier.owner_alert_delivery import deliver_with_retry
from src.notifier.transport import (
    TelegramNotifier,
)

# === Out-of-band owner alert ===

#: The P&L stand-in every standalone owner alert carries directly under its
#: heading. Owner, 2026-09-18, verbatim: "all the P&L information has to go
#: at the very top of every telegram alert, right after the first line,
#: which is really the heading."
#:
#: A standalone alert genuinely CANNOT carry a figure. It fires the instant
#: a problem is found — from the credential check, the stop-coverage audit,
#: a reconciliation mismatch — on paths that have done no account read, and
#: a page about a naked position must never block on a broker round-trip or
#: be able to fail inside one. So the line says exactly that, in one
#: sentence, rather than being dropped (an absent block reads as a broken
#: one) or filled with a fabricated zero.
_ALERT_NO_PNL_LINE = (
    "\U0001f4c8 P&L: not available in this alert — it is sent the moment a "
    "problem is found, before any account is read."
)


def _with_pnl_header(text: str) -> str:
    """Insert the P&L block directly under an alert's heading line.

    Enforced HERE, in the one funnel every standalone alert already goes
    through, rather than in each of the eighteen callers that build one.
    The rule has been restated by the owner more than once and drifts every
    time it depends on the next author remembering it; a single choke point
    is the only version of it that holds.

    Never raises — an alerting bug must not be able to break the thing it
    reports on. On any fault the original text goes out unchanged.
    """
    try:
        if _ALERT_NO_PNL_LINE in text:
            return text
        heading, sep, rest = text.partition("\n")
        if not sep:
            return f"{heading}\n{_ALERT_NO_PNL_LINE}"
        return f"{heading}\n{_ALERT_NO_PNL_LINE}\n{rest}"
    except Exception:  # noqa: BLE001
        logger.exception("could not attach the P&L line to an owner alert")
        return text


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
    if not text:
        return False
    text = _with_pnl_header(text)
    logger.critical("OWNER ALERT\n%s", text)
    try:
        return deliver_with_retry(
            TelegramNotifier(), text,
            symbols=symbols, kind="owner_alert", category=category,
        )
    except Exception:  # noqa: BLE001
        logger.exception("owner alert delivery failed")
        return False
