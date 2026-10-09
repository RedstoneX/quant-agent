"""One appended, idempotent migration step: the owner intent record.

Same shape as the other `_migrate` steps in `manager.py`: CREATE ... IF NOT
EXISTS, own try/except so a hiccup here can never stop the desk starting.
Never reordered or altered once shipped (production databases apply it).
"""

import logging

logger = logging.getLogger(__name__)

# state: raised | acted | refused | expired. `outcome` carries the reason for
# refused/expired and the effect for acted. `symbol` is NULL for desk-wide
# actions. `expires_at` is set only by whoever raised the intent (nullable).
_DDL = """
    CREATE TABLE IF NOT EXISTS owner_intents (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        action TEXT NOT NULL,
        symbol TEXT,
        params_json TEXT NOT NULL DEFAULT '{}',
        raised_at TEXT NOT NULL,
        reason TEXT,
        expires_at TEXT,
        state TEXT NOT NULL DEFAULT 'raised'
            CHECK (state IN ('raised','acted','refused','expired')),
        outcome TEXT,
        resolved_at TEXT
    )
"""


def apply(conn) -> None:
    try:
        conn.execute(_DDL)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_owner_intents_state ON owner_intents(state)")
        conn.commit()
    except Exception as e:  # noqa: BLE001 - never block startup on a migration hiccup
        logger.error("Schema migration failed for owner_intents: %s", e)
