"""Live-risk recognition for the muted-backlog read (moved out of db_reads.py to keep it from growing).

Pure: reads no database, needs no connection; `src.log_health` is imported lazily
exactly as the original did.
"""

from __future__ import annotations


def _live_risk_headlines() -> tuple[str, ...]:
    """The owner-facing headlines that mean "protection is gone or absent".

    NOT a new classification. These are the literal first lines the desk's
    own live-risk alerts write — the three per-symbol-per-day siblings of
    item 211 plus their relatives in `src/coverage_watchdog.py` and
    `src/notifier.py`. A headline announcing that a gap CLOSED ("A MISSING
    STOP WAS PUT BACK", "PROTECTION RESTORED") is deliberately absent: it is
    good news about a gap, not an open one.
    """

    return (
        "EXIT NOT PLACED",
        "PROTECTIVE STOP UNREADABLE",
        "STOP UNREADABLE",
        "POSITION UNGUARDED LONGER THAN EVER MEASURED",
        "UNPROTECTED SHARES, AND THE DESK IS NOT RUNNING",
        "COULD NOT PUT THE PROTECTIVE STOP BACK",
    )


def is_live_risk_message(text: str | None) -> bool:
    """True when this muted message was about unprotected money.

    Two sources, both of them the desk's existing idea of the class, neither
    invented here: the `MONEY_UNPROTECTED` fault families in
    `src/log_health.py` (the module that already owns "a position was left
    without the protective stop the desk believes is on it"), and the
    owner-facing headlines those same alerts print. The log-health patterns
    are written against log lines and the headlines against owner prose, so
    both are needed to cover a record that holds owner prose written from
    the same events.
    """

    body = (text or "").strip()
    if not body:
        return False
    upper = body.upper()
    if any(head in upper for head in _live_risk_headlines()):
        return True
    try:
        from src.log_health import FAMILIES, MONEY_UNPROTECTED
    except Exception:
        return False
    for family in FAMILIES:
        if getattr(family, "reason", None) != MONEY_UNPROTECTED:
            continue
        for pattern in getattr(family, "patterns", ()) or ():
            if pattern.search(body):
                return True
    return False
