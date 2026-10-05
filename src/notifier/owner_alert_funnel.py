"""The widened owner-alert funnel: one door, carrying what honest callers need.

`send_owner_alert` built its own `TelegramNotifier`, hardcoded the record
`kind`, and threw away the suppression outcome. Two legitimate callers — the
cost circuit's alert-outcome helper and the naked-position alert — therefore
could not use it: they are handed a notifier, they label their own rows, and
one of them must distinguish a deliberate drop from a failure. They went
straight to the delivery layer instead, which is exactly the side door the
no-side-door guard exists to refuse.

Narrowing the guard would reopen the defect it closed (a failed direct send
vanishing with no trace). So the funnel grows instead: an injected notifier,
a per-alert `kind`, a `run_id` that reaches the durable row, an explicit
attempt budget, and the full (delivered, suppressed) outcome returned rather
than collapsed to a bool.

Every property of the narrow funnel is preserved: the CRITICAL journal line
goes out before any network call, the P&L block is attached at this one
choke point, retries stay bounded with a timeout-bearing transport, a send
that never lands still writes one counted durable row, and nothing here
raises.
"""

from __future__ import annotations

from src.notifier.base import logger
from src.notifier.owner_alert_delivery import MAX_ATTEMPTS, deliver_with_outcome

DEFAULT_KIND = "owner_alert"

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


def build_default_notifier(*, factory=None, **kwargs):
    """The ONE place a notifier is built for any code outside this module.

    A guard (tests/test_no_side_door_owner_alert_send.py) refuses a direct
    `TelegramNotifier(...)` anywhere else in src/, so every holder of a
    notifier is built here and every alert it sends is funnelled. The class
    is looked up at call time so tests that patch `src.notifier.TelegramNotifier`
    keep working; `factory` lets a module keep its own patch point.
    """
    if factory is None:
        import src.notifier as notifier_pkg

        factory = notifier_pkg.TelegramNotifier
    return factory(**kwargs)


def send_owner_alert_with_outcome(
    text: str,
    *,
    notifier=None,
    symbols: list[str] | None = None,
    category: str | None = None,
    kind: str = DEFAULT_KIND,
    run_id: str | None = None,
    max_attempts: int = MAX_ATTEMPTS,
    pnl_header: bool = True,
) -> tuple[bool, bool]:
    """Push an owner alert NOW and report BOTH halves of what happened.

    Returns (delivered, suppressed). (False, False) is a real failure: it was
    retried up to `max_attempts` and then recorded undelivered, counted, under
    this call's own `kind` and `run_id`. (False, True) is a deliberate drop by
    the mute or the category filter — settled, never retried, never recorded
    as delivered. The two are kept apart all the way to the caller because
    collapsing them is how a dropped alert came to read as a sent one.

    `notifier` defaults to a fresh `TelegramNotifier`, so the ~30 existing
    callers are unaffected; a caller that already holds one passes it in
    rather than opening a second.

    Never raises. An alerting bug must not break the money path it reports on.
    """
    if not text:
        return False, False
    if pnl_header:
        text = _with_pnl_header(text)
    logger.critical("OWNER ALERT [%s]\n%s", kind, text)
    try:
        if notifier is None:
            from src.notifier.transport import TelegramNotifier

            notifier = TelegramNotifier()
        return deliver_with_outcome(
            notifier,
            text,
            max_attempts=max_attempts,
            symbols=symbols,
            kind=kind,
            run_id=run_id,
            category=category,
        )
    except Exception:  # noqa: BLE001
        logger.exception("owner alert delivery failed")
        return False, False
