"""READ-ONLY owner flags (panel instalment 1). SELECT only, safe for the broker door.

Flags are a replay of the intents that were ACTED on, in order; nothing is
stored twice. The writer lives in src/owner_intents.py (desk-side only).
"""

import json
import logging
import os
import sqlite3
import time
from dataclasses import dataclass, replace

logger = logging.getLogger(__name__)

PAUSE, RESUME = "PAUSE", "RESUME"


@dataclass(frozen=True)
class Flags:
    paused: bool = False
    unknown: bool = False  # cannot tell whether the owner paused: treated as paused
    stale: bool = False  # read failed; `paused` is the last saved copy (still UNKNOWN)


def current_flags(conn) -> Flags:
    paused = False
    for action, sym in conn.execute("SELECT action, symbol FROM owner_intents WHERE state='acted' ORDER BY id"):
        if action == PAUSE:
            paused = True
        elif action == RESUME:
            paused = False
    return Flags(paused)


_CACHE: dict = {}


def _cache_file(db_path) -> str:
    return f"{db_path}.owner_flags.json"


def _remember(db_path, flags: Flags) -> None:
    """Keep a durable copy of the last flag set read, for when a read fails."""
    if _CACHE.get(db_path) == flags:
        return
    _CACHE[db_path] = flags
    try:
        tmp = _cache_file(db_path) + ".tmp"
        with open(tmp, "w") as fh:
            json.dump({"paused": flags.paused}, fh)
        os.replace(tmp, _cache_file(db_path))
    except OSError as exc:
        logger.warning("owner flag cache not written: %s", exc)


def _recall(db_path):
    if db_path in _CACHE:
        return _CACHE[db_path]
    try:
        with open(_cache_file(db_path)) as fh:
            d = json.load(fh)
        return Flags(bool(d["paused"]))
    except (OSError, ValueError, KeyError):
        return None


def _read_once(db_path) -> Flags:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
    try:
        return current_flags(conn)
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc):
            return Flags()  # no intent ever recorded: a KNOWN empty state
        raise
    finally:
        conn.close()


def read_flags(db_path, attempts=3, retry_pause_s=0.2) -> Flags:
    """Flags for the broker door, escalating when the database cannot be read.

    1. retry; 2. on failure the state is UNKNOWN (owner ruling 2026-10-09):
    the last saved copy is reported (`stale`, its `paused` kept for the log)
    but never trusted as "not paused" -- a pause raised after that copy was
    written would otherwise be missed. A desk with no database path cannot
    read the flag at all, so it is UNKNOWN too. The door answers UNKNOWN
    exactly as it answers a pause.
    """
    if not db_path:
        logger.error("owner flags: no intent database path: UNKNOWN")
        return Flags(unknown=True)
    last = None
    for attempt in range(attempts):
        try:
            flags = _read_once(db_path)
            _remember(db_path, flags)
            return flags
        except Exception as exc:  # noqa: BLE001
            last = exc
            if attempt + 1 < attempts:
                time.sleep(retry_pause_s)
    cached = _recall(db_path)
    if cached is not None:
        logger.error("owner flags unreadable (%s); last saved copy paused=%s, state UNKNOWN", last, cached.paused)
        return replace(cached, stale=True, unknown=True)
    logger.error("owner flags unreadable (%s) and no last known set: UNKNOWN", last)
    return Flags(unknown=True)
