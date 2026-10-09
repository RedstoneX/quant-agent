"""Make the dashboard's broad catch-alls LOUD, without changing them.

The dashboard side is read-only and holds no ledger handle, so there is no
counted row to write here: the full traceback at ERROR is the whole record,
and a site that is never reached logs nothing. A clean pass logs nothing
either, because a row would need a database write the dashboard may not make.
Nothing is stored and nothing is returned for a caller to branch on.
"""

from __future__ import annotations

import logging

logger = logging.getLogger("src.api.routes_live")


def record_dashboard_fault(where: str, exc: BaseException | None = None) -> None:
    """Log ONE swallowed dashboard fault with its full traceback at ERROR.

    Call it from inside the handler: with `exc` left out it reads the
    exception currently being handled, so an unbound ``except Exception:``
    can stay unbound.
    """
    logger.error("dashboard read swallowed a fault at %s", where, exc_info=exc if exc is not None else True)
