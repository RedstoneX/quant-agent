"""THE ONE ANSWER to "where does this catch-all get its ledger handle from?"

Three agents converting money-path catch-alls (see ``guarded.py``) each hit a
site with no obvious handle in reach -- a module-level function, a static
helper, a cluster built fresh from a trading client -- and each answered it
differently: one lent the handle to the shared client, one logged a traceback
with no row and a comment, one restructured a success path. Three shapes for
one question means the next batch invents a fourth. This module settles it.

THE RULE. A catch-all passes ``record_guarded_pass`` whatever it ALREADY has in
scope, and ``ledger_in_reach`` finds the ledger on it at call time:

  * an owner lent a handle by the pipeline     -> the ``_recon_db`` attribute
  * a protection object or a ``Database``       -> its ``.db`` / its ``.conn``
  * a trading client wrapped at the one wiring
    site in ``cancel_attempts``                 -> the client's ``_conn_getter``
  * a broker-shaped object                      -> one hop into ``.client``
  * several things in scope                     -> pass them as a tuple; the
                                                   first that carries a ledger wins

Nothing is stored to make this work: no module global, no singleton, no cache,
no second channel. Every lookup is a fresh ``getattr`` walk over objects the
caller was handed anyway, so an isolated unit test that builds the object bare
behaves exactly as before. Change a SIGNATURE to thread a handle through? No:
that moves code outside the handler, which is not plumbing.

THE EXEMPTION. A site with NOTHING ledger-bearing in scope -- primitives only,
or a client that was never wrapped -- passes ``NO_LEDGER`` as the owner. It
still gets the full traceback at ERROR; only the row is skipped, and
``record_reconciliation`` says so at debug. ``NO_LEDGER`` is a greppable
declaration, not a comment: ``git grep NO_LEDGER`` lists every exempt site, so
"no row here" is a visible, counted decision instead of an ad-hoc variant.
Prefer finding a handle over declaring exemption; declare it only when the
walk above genuinely has nothing to walk.
"""
from __future__ import annotations

import logging
import sqlite3

logger = logging.getLogger(__name__)

#: Attribute the lent ledger handle lives on. Read with ``getattr`` and a
#: default everywhere, so an owner that never had one behaves identically.
RECON_DB_ATTR = "_recon_db"

#: The declared exemption: "this site has nothing ledger-bearing in scope".
NO_LEDGER = object()


class _LazyLedger:
    """What ``record_reconciliation`` wants -- an object with a ``.conn`` --
    built fresh from a connection getter on every read, so nothing about the
    ledger is cached or stored on the object it is lent to."""

    def __init__(self, conn_getter):
        self._conn_getter = conn_getter

    @property
    def conn(self):
        try:
            return self._conn_getter()
        except Exception:  # noqa: BLE001
            logger.error("guarded rows cannot reach the ledger", exc_info=True)
            return None


def _ledger_on(obj, hops: int):
    if obj is None or obj is NO_LEDGER:
        return None
    lent = getattr(obj, RECON_DB_ATTR, None)
    if lent is not None:
        return lent
    if isinstance(getattr(obj, "conn", None), sqlite3.Connection):
        return obj
    db = getattr(obj, "db", None)
    if isinstance(getattr(db, "conn", None), sqlite3.Connection):
        return db
    getter = getattr(obj, "_conn_getter", None)
    if callable(getter):
        return _LazyLedger(getter)
    if hops > 0:
        for hop in ("client", "broker"):
            found = _ledger_on(getattr(obj, hop, None), hops - 1)
            if found is not None:
                return found
    return None


def ledger_in_reach(*candidates):
    """The first ledger handle reachable from `candidates`, or ``None``.

    Computed at call time from the objects given; never caches, never writes
    back onto them. ``None`` means the traceback is still logged and only the
    counted row is skipped. Any exception here is the caller's to swallow --
    ``record_guarded_pass`` already does, so an observer cannot take the
    money path down.
    """
    for candidate in candidates:
        found = _ledger_on(candidate, hops=1)
        if found is not None:
            return found
    return None
