"""Turn a sizing-price read into a price or a NAMED refusal, long or short.

The share count of an entry is `dollars / price`, so an entry may only be sized
on a price that was actually read. Two different states both mean "do not size":

* ``no_sizing_print`` -- MEASURED: the read worked and there is no today print
  (or no usable one). Behaviour and wording unchanged from before.
* ``sizing_price_unreadable`` -- UNKNOWN: the read itself raised
  `SizingPriceUnavailable`. The entry is refused (fail closed) and the
  traceback is logged, so the record says the desk could not find out rather
  than claiming there was no price.

The reader is passed in rather than imported so this module never imports the
pipeline (no import cycle) and a test can hand it any reader. Direction is not
an argument: a short is sized and refused exactly like a long.
"""
from __future__ import annotations

import logging
from typing import Callable

from src.refusal_errors import SizingPriceUnavailable

logger = logging.getLogger(__name__)

NO_SIZING_PRINT = "no_sizing_print"
SIZING_PRICE_UNREADABLE = "sizing_price_unreadable"


def sizing_price_or_refusal(
    reader: Callable[[object, str], float | None], pipeline, symbol: str, what: str,
) -> tuple[float | None, str, str]:
    """``(price, "", "")`` when sizeable, else ``(None, reason, detail)``.

    ``what`` names the thing being sized ("buy", "order", "replacement buy")
    and only changes the wording of the detail.
    """
    try:
        price = reader(pipeline, symbol)
    except SizingPriceUnavailable as exc:
        logger.error(
            "%s sizing price UNREADABLE -- %s refused: %s", symbol, what, exc,
            exc_info=True,
        )
        return None, SIZING_PRICE_UNREADABLE, (
            f"{SIZING_PRICE_UNREADABLE}: the sizing-price read itself failed "
            f"({exc}) -- the desk does not know whether a today print exists, "
            f"so the {what} is refused rather than sized on an unverified price"
        )
    if isinstance(price, (int, float)) and not isinstance(price, bool) and price > 0:
        return float(price), "", ""
    return None, NO_SIZING_PRINT, (
        f"no today trade print to size the {what} against (a quote mid or a "
        "prior-session price is not a sizing reference) — refused rather than "
        "sized on a bad price"
    )
