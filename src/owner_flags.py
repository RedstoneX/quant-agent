"""READ-ONLY owner flags (panel instalment 1). SELECT only, safe for the broker door.

Flags are a replay of the intents that were ACTED on, in order; nothing is
stored twice. The writer lives in src/owner_intents.py (desk-side only).
"""
import logging
import sqlite3
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

PAUSE, RESUME = "PAUSE", "RESUME"
NEVER_TOUCH_ADD, NEVER_TOUCH_REMOVE = "NEVER_TOUCH_ADD", "NEVER_TOUCH_REMOVE"
HANDS_OFF, HANDS_ON = "HANDS_OFF", "HANDS_ON"


@dataclass(frozen=True)
class Flags:
    paused: bool = False
    never_touch: frozenset = field(default_factory=frozenset)
    hands_off: frozenset = field(default_factory=frozenset)

    @property
    def untouchable(self) -> frozenset:
        return self.never_touch | self.hands_off


def current_flags(conn) -> Flags:
    paused, never, hands = False, set(), set()
    for action, sym in conn.execute(
            "SELECT action, symbol FROM owner_intents WHERE state='acted' ORDER BY id"):
        if action == PAUSE:
            paused = True
        elif action == RESUME:
            paused = False
        elif action == NEVER_TOUCH_ADD:
            never.add(sym)
        elif action == NEVER_TOUCH_REMOVE:
            never.discard(sym)
        elif action == HANDS_OFF:
            hands.add(sym)
        elif action == HANDS_ON:
            hands.discard(sym)
    return Flags(paused, frozenset(never), frozenset(hands))


def read_flags(db_path) -> Flags:
    """Read-only flags for the broker door. No table yet means no flags."""
    if not db_path:
        return Flags()
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
        try:
            return current_flags(conn)
        finally:
            conn.close()
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc):
            return Flags()
        logger.error("owner flags unreadable (%s); treating as none", exc)
    except Exception as exc:  # noqa: BLE001
        logger.error("owner flags unreadable (%s); treating as none", exc)
    return Flags()
