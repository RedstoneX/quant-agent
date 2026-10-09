"""The owner intent record and the one flag-only action, pause/resume (panel instalment 1).

DESK-SIDE ONLY. `src/api/` must never import this: it writes the database,
and tests/test_api_cannot_trade.py forbids that by construction. A future
raising door must live outside `src/api/`.

State is never stored twice: the flags are a replay of the intents that were
ACTED on, in order. Nothing here moves money or touches the broker; the
broker door reads `current_flags` (see src/execution/owner_flags_gate.py).

Staleness has no time constant. An intent is stale when the owner gave it an
`expires_at` that has passed by the time the desk picks it up (EXPIRED, with
that reason). A row naming an action this desk does not know is REFUSED, with
its reason. Pause and resume name no position, so replaying them
in raised order is always correct.

There is no per-position "hands off" and no never-touch list: the desk manages
every position it holds. An owner action is an instruction the desk carries out
and keeps managing, recorded as his decision, never an exemption.
"""

import json
import logging
import sqlite3
from datetime import datetime, timezone

from src.owner_flags import (  # noqa: F401
    PAUSE,
    RESUME,
    Flags,
    current_flags,
    read_flags,
)

logger = logging.getLogger(__name__)

ACTIONS = {PAUSE, RESUME}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def raise_intent(conn, action, *, symbol=None, params=None, reason=None, expires_at=None, now=None) -> int:
    """Record what the owner asked for. Durable; acts on nothing."""
    if action not in ACTIONS:
        raise ValueError(f"unknown owner action {action!r}")
    sym = symbol.strip().upper() if isinstance(symbol, str) and symbol.strip() else None
    cur = conn.execute(
        "INSERT INTO owner_intents (action, symbol, params_json, raised_at, reason, expires_at) VALUES (?,?,?,?,?,?)",
        (
            action,
            sym,
            json.dumps(params or {}, sort_keys=True),
            _iso(now or _now()),
            reason,
            _iso(expires_at) if expires_at else None,
        ),
    )
    conn.commit()
    return cur.lastrowid


def process_pending(conn, *, now=None) -> list:
    """Resolve every RAISED intent in raised order: acted, refused or expired.

    Runs at desk start and before each session, so an intent raised while the
    desk was down is acted on (or refused with its reason) when it starts.
    """
    now = now or _now()
    done = []
    rows = conn.execute(
        "SELECT id, action, symbol, expires_at FROM owner_intents WHERE state='raised' ORDER BY id"
    ).fetchall()
    for rid, action, sym, exp in rows:
        if action not in ACTIONS:
            state, outcome = "refused", f"unknown action {action!r}"
        elif exp and datetime.fromisoformat(exp) <= now:
            state, outcome = "expired", f"expired at {exp} before the desk could act"
        else:
            state, outcome = "acted", f"{action} now in force"
        conn.execute(
            "UPDATE owner_intents SET state=?, outcome=?, resolved_at=? WHERE id=?", (state, outcome, _iso(now), rid)
        )
        done.append((rid, action, sym, state, outcome))
    conn.commit()
    return done


def intake(db_path) -> list:
    """Desk-side pickup: open its own connection, resolve pending intents."""
    if not db_path:
        return []
    conn = sqlite3.connect(db_path, timeout=30)
    try:
        from src.storage.schema.owner_intent_tables import apply

        apply(conn)
        done = process_pending(conn)
        for rid, action, sym, state, outcome in done:
            logger.info("owner intent #%s %s %s -> %s (%s)", rid, action, sym, state, outcome)
        return done
    finally:
        conn.close()
