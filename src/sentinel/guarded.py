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

WHERE THE HANDLE COMES FROM when the handler has no obvious owner -- a
static helper, a module-level function, a cluster built from a client -- is
settled ONCE in ``guarded_reach.py``: pass whatever is already in scope and
``ledger_in_reach`` walks it at call time; a site with nothing to walk passes
``NO_LEDGER`` and is a declared, greppable exemption. Read that module first.
"""

from __future__ import annotations

import logging
import sys

from src.sentinel.guarded_reach import (  # noqa: F401 (re-export)
    NO_LEDGER,
    RECON_DB_ATTR,
    _LazyLedger,
    ledger_in_reach,
)
from src.sentinel.reconciliation import record_guarded_outcome

logger = logging.getLogger(__name__)


def origin_area(module_name: str) -> str:
    """Where a row came from, read off the module that recorded it.

    ``src.execution.broker_parts.order_desk`` -> ``execution.broker_parts``;
    a top-level ``src.pipeline_sizing`` -> ``pipeline_sizing``. Derived from
    the caller at record time, so a new site is labelled correctly without
    anyone writing a label. Rows written before this carry the legacy shared
    ``broker.`` prefix whatever their origin; they are left as written (a
    migration would have to guess origins and production is read-only here).
    """
    parts = module_name.split(".")
    if parts and parts[0] == "src":
        parts = parts[1:]
    return ".".join(parts[:-1]) or ".".join(parts) or "unknown"


def attach_reconciliation_db(broker, conn_getter) -> None:
    """Lend `broker` the ledger connection its guarded sites count rows through.

    Observability only: nothing on the order path reads this, and a broker
    without it takes exactly the same decisions.
    """
    setattr(broker, RECON_DB_ATTR, _LazyLedger(conn_getter))


def record_guarded_pass(
    owner, where: str, exc: BaseException | None = None, *, context: dict | None = None, log=None
) -> None:
    """Record ONE pass through one of the broker's broad catch-alls.

    `owner` is whatever the site has in scope (or a tuple of such things, or
    ``NO_LEDGER``); see ``guarded_reach.ledger_in_reach`` for the walk.

    `exc` set means the pass swallowed a fault: full traceback at ERROR and a
    ``disagreed`` row carrying the exception type, its message and `context`.
    `exc` left as ``None`` means the pass ran clean: an ``agreed`` row, so that
    a site which ran fine is never mistaken for one that was never reached.

    Never re-raises and never returns anything a caller can branch on, so a
    fault in the observer cannot take the money path down with it.
    """
    try:
        area = origin_area(sys._getframe(1).f_globals.get("__name__", ""))
        if where.split(".")[0] == area.split(".")[-1]:
            area = ".".join(area.split(".")[:-1])  # the site already names its package
        record_guarded_outcome(
            db=ledger_in_reach(*(owner if isinstance(owner, tuple) else (owner,))),
            where=f"{area}.{where}" if area else where,
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
