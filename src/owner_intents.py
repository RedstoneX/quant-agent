"""The owner intent record and the three flag-only actions (panel instalment 1).

DESK-SIDE ONLY. `src/api/` must never import this: it writes the database,
and tests/test_api_cannot_trade.py forbids that by construction. A future
raising door must live outside `src/api/`.

State is never stored twice: the flags are a replay of the intents that were
ACTED on, in order. Nothing here moves money or touches the broker; the
broker door reads `current_flags` (see src/execution/owner_flags_gate.py).

Staleness has no time constant. An intent is stale when the thing it named is
gone (HANDS_OFF on a symbol no longer held) or when the owner gave it an
`expires_at`. Pause/resume/never-touch name no position, so they are never
stale: replaying them in raised order is always correct.
"""
import json
import logging
import sqlite3
from datetime import datetime, timezone

from src.owner_flags import (  # noqa: F401
    HANDS_OFF, HANDS_ON, NEVER_TOUCH_ADD, NEVER_TOUCH_REMOVE, PAUSE, RESUME,
    Flags, current_flags, read_flags,
)

logger = logging.getLogger(__name__)

DESK_WIDE = {PAUSE, RESUME}
SYMBOL_ACTIONS = {NEVER_TOUCH_ADD, NEVER_TOUCH_REMOVE, HANDS_OFF, HANDS_ON}
ACTIONS = DESK_WIDE | SYMBOL_ACTIONS
POSITION_BOUND = {HANDS_OFF}  # names a held position, so can go stale


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def raise_intent(conn, action, *, symbol=None, params=None, reason=None,
                 expires_at=None, now=None) -> int:
    """Record what the owner asked for. Durable; acts on nothing."""
    if action not in ACTIONS:
        raise ValueError(f"unknown owner action {action!r}")
    sym = symbol.strip().upper() if isinstance(symbol, str) and symbol.strip() else None
    if action in SYMBOL_ACTIONS and sym is None:
        raise ValueError(f"{action} needs a symbol")
    if action in DESK_WIDE:
        sym = None
    cur = conn.execute(
        "INSERT INTO owner_intents (action, symbol, params_json, raised_at, reason, expires_at)"
        " VALUES (?,?,?,?,?,?)",
        (action, sym, json.dumps(params or {}, sort_keys=True), _iso(now or _now()),
         reason, _iso(expires_at) if expires_at else None),
    )
    conn.commit()
    return cur.lastrowid


def process_pending(conn, *, held_symbols, now=None) -> list:
    """Resolve every RAISED intent in raised order: acted, refused or expired.

    Runs at desk start and before each session, so an intent raised while the
    desk was down is acted on (or refused with its reason) when it starts.
    """
    now = now or _now()
    held = {s.upper() for s in held_symbols}
    done = []
    rows = conn.execute(
        "SELECT id, action, symbol, expires_at FROM owner_intents"
        " WHERE state='raised' ORDER BY id").fetchall()
    for rid, action, sym, exp in rows:
        if exp and datetime.fromisoformat(exp) <= now:
            state, outcome = "expired", f"expired at {exp} before the desk could act"
        elif action in POSITION_BOUND and sym not in held:
            state, outcome = "refused", f"stale: {sym} is no longer a held position"
        else:
            state, outcome = "acted", f"{action} now in force"
        conn.execute(
            "UPDATE owner_intents SET state=?, outcome=?, resolved_at=? WHERE id=?",
            (state, outcome, _iso(now), rid))
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
        try:
            held = {r[0] for r in conn.execute("SELECT symbol FROM positions WHERE qty != 0")}
        except sqlite3.OperationalError:
            held = set()
        done = process_pending(conn, held_symbols=held)
        for rid, action, sym, state, outcome in done:
            logger.info("owner intent #%s %s %s -> %s (%s)", rid, action, sym, state, outcome)
        return done
    finally:
        conn.close()
