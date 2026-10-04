"""Make the execution broker's broad catch-alls LOUD, without changing them.

Why this module exists: a defect resolved on 2026-10-04 had an argument
passed twice, which raised a ``TypeError`` inside a broad ``except
Exception`` that recorded one bland line and swallowed it — so a record was
never stored and nothing complained. An audit then counted 98 broad handlers
across the money-path files, none of them logging a traceback.

The handlers STAY. Removing a catch-all on the order path can turn a
recoverable miss into a crash mid-order, and protection changes here are
one-way. What changes is only what the handler RECORDS: the full traceback at
ERROR plus one counted row on the EXISTING reconciliation channel (the same
channel PR 1321 used for the protection-restore drain — there is deliberately
no second logging channel).

Three states must stay distinguishable, and a counter that only fires on
failure cannot tell the first from the second:

  * ``not_run``   — the site was never reached: no row at all
  * ``agreed``    — it ran and swallowed nothing: the clean pass writes its row
  * ``disagreed`` — it ran and swallowed a fault: traceback plus a detail row

Nothing is stored here: no module-level tally, no file, no counter object.
The only durable write is the reconciliation row, which is computed fresh from
the single pass being reported.

The reconciliation connection is NOT something ``AlpacaBroker`` has ever
held — it is a ledger handle owned by the pipeline. ``attach_reconciliation_db``
lets the pipeline lend it to the broker for observability only; when no handle
has been lent (an isolated unit test building ``AlpacaBroker`` directly, say)
the traceback is still logged in full and the row is simply skipped, which
``record_reconciliation`` already reports at debug.
"""
from __future__ import annotations

import logging

from src.sentinel.reconciliation import record_guarded_outcome

logger = logging.getLogger(__name__)

#: Attribute the lent ledger handle lives on. Read with ``getattr`` and a
#: default everywhere, so a broker that never had one behaves identically.
RECON_DB_ATTR = "_recon_db"


class _LazyLedger:
    """What ``record_reconciliation`` wants — an object with a ``.conn`` — built
    fresh from the pipeline's connection getter on every read, so nothing about
    the ledger is cached or stored on the broker."""

    def __init__(self, conn_getter):
        self._conn_getter = conn_getter

    @property
    def conn(self):
        try:
            return self._conn_getter()
        except Exception:  # noqa: BLE001
            logger.error("guarded rows cannot reach the ledger", exc_info=True)
            return None


def attach_reconciliation_db(broker, conn_getter) -> None:
    """Lend `broker` the ledger connection its guarded sites count rows through.

    Observability only: nothing on the order path reads this, and a broker
    without it takes exactly the same decisions.
    """
    setattr(broker, RECON_DB_ATTR, _LazyLedger(conn_getter))


def record_guarded_pass(owner, where: str, exc: BaseException | None = None, *,
               context: dict | None = None, log=None) -> None:
    """Record ONE pass through one of the broker's broad catch-alls.

    `exc` set means the pass swallowed a fault: full traceback at ERROR and a
    ``disagreed`` row carrying the exception type, its message and `context`.
    `exc` left as ``None`` means the pass ran clean: an ``agreed`` row, so that
    a site which ran fine is never mistaken for one that was never reached.

    Never re-raises and never returns anything a caller can branch on, so a
    fault in the observer cannot take the money path down with it.
    """
    try:
        record_guarded_outcome(
            db=getattr(owner, RECON_DB_ATTR, None),
            where=f"broker.{where}",
            exc=exc,
            log=log or logger,
            context=context,
        )
    except Exception:  # noqa: BLE001
        # An observer must never break the thing it observes: this call sits
        # inside a handler that is itself protecting an order, so a locked
        # database or a missing table here must not abort the cancel, the
        # replace or the fill read that was already in trouble.
        logger.error("record_guarded_pass could not record %s", where, exc_info=True)
