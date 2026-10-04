"""Make the stop-protection path's broad catch-alls LOUD, without changing them.

Sibling of `src/sentinel/guarded.py`, which did the same job for the broker
seam. The same audit that found 98 broad handlers across the money-path files
counted roughly 30 in `src/pipeline_protection.py` -- the module that places,
cancels, restores and reconciles the protective stop standing under a live
position. Each logged one bland line with no traceback, so a programming error
(a renamed field, an argument passed twice) was indistinguishable from an
expected broker miss.

The handlers STAY. Removing a catch-all here can turn a recoverable miss into a
crash mid-repair and leave shares naked, and protection changes are one-way.
What changes is only what the handler RECORDS: the full traceback at ERROR plus
one counted row on the EXISTING reconciliation channel.

Three states must stay distinguishable, and a counter that only fires on
failure cannot tell the first from the second:

  * ``not_run``   -- the site was never reached: no row at all
  * ``agreed``    -- it ran and swallowed nothing: the clean pass writes its row
  * ``disagreed`` -- it ran and swallowed a fault: traceback plus a detail row

Nothing is stored here: no module-level tally, no file, no counter object. The
only durable write is the reconciliation row, computed fresh from the single
pass being reported.

The ledger handle is read off the owner with ``getattr(.., "db", None)``: the
protection mixin and the standalone classes under `src/protection/` all carry
one, and an object built without one in an isolated unit test still gets the
full traceback -- only the row is skipped, which ``record_reconciliation``
already reports at debug.
"""
from __future__ import annotations

import logging

from src.sentinel.reconciliation import record_guarded_outcome

logger = logging.getLogger("src.pipeline_protection")


def guarded_pass(owner, where: str, exc: BaseException | None = None, **context) -> None:
    """Record ONE pass through one of the protection path's broad catch-alls.

    `exc` set means the pass swallowed a fault: full traceback at ERROR and a
    ``disagreed`` row carrying the exception type, its message and `context`.
    `exc` left as ``None`` means the pass ran clean: an ``agreed`` row, so a
    site that ran fine is never mistaken for one that was never reached.

    Never re-raises and never returns anything a caller can branch on, so a
    fault in the observer cannot take the protection path down with it.
    """
    try:
        record_guarded_outcome(
            db=getattr(owner, "db", None),
            where=f"protection.{where}",
            exc=exc,
            log=logger,
            context=context or None,
        )
    except Exception:  # noqa: BLE001
        # An observer must never break the thing it observes: this call sits
        # inside a handler that is itself protecting a position, so a locked
        # database or a missing table must not abort the re-protect, the
        # coverage sweep or the ex-dividend stop shift already in trouble.
        logger.error("guarded_pass could not record %s", where, exc_info=True)
