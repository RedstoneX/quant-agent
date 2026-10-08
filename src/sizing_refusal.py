"""Turn a sizing-price read into a price or a NAMED refusal, long or short.

The share count of an entry is `dollars / price`, so an entry may only be sized
on a price that was actually read. Three different states all mean "do not size":

* ``no_sizing_print`` -- MEASURED: the read worked and there is no today print
  (or no usable one). Per-name skip; other names keep trading.
* ``price_read_failed`` -- UNKNOWN, ONE NAME: the read itself failed after the
  broker's retry, but a read of the session's reference symbol (a held or
  liquid name, the one the entry-stage preflight used) succeeded. The fault is
  that name's; its entry is refused, the fault is recorded and counted, and
  other names keep trading.
* ``price_feed_unreadable`` -- UNKNOWN, THE DESK: the reference read failed
  too, so the feed is down. Owner ruling 2026-10-08: a desk that cannot read
  price cannot trade. Every further NEW entry this session is refused with
  this reason. Existing positions are not touched: their broker-held stops
  protect them and nothing here cancels or moves one.

A fourth state is not a refusal at all: no today print YET. Owner ruling
2026-10-08: "this is a swing trading desk, not scalping. The desk can keep
re-asking until a price is received. It should not be hammered." The entry
preflight (`src.stage_entry_preflight`) holds such names in a waiting set and
`src.price_feed_preflight.wait_for_today_prints` re-asks for ALL of them in
one batched request on a rising backoff until each prints or the session's
own slot ends; only then is a name skipped, under ``no_print_by_window_end``.

The fault state lives on the pipeline for ONE run of the entry stage
(`src.price_feed_preflight` resets it at the start of each). The reader is
passed in rather than imported so this module never imports the pipeline (no
import cycle) and a test can hand it any reader. Direction is not an argument:
a short is sized and refused exactly like a long.
"""
from __future__ import annotations

import logging
import sys
from typing import Callable

from src.refusal_errors import PriceReadFailed, SizingPriceUnavailable
from src.sentinel.entry_guard import record_swallowed_here
from src.sentinel.reconciliation import record_guarded_outcome

logger = logging.getLogger(__name__)

NO_SIZING_PRINT = "no_sizing_print"
SIZING_PRICE_UNREADABLE = "sizing_price_unreadable"  # kept: the detail prefix tests read
PRICE_READ_FAILED = "price_read_failed"
PRICE_FEED_UNREADABLE = "price_feed_unreadable"
NO_PRINT_BY_WINDOW_END = "no_print_by_window_end"


def price_feed_fault(pipeline) -> str | None:
    """The session's desk-level fault detail, or None. A string only: a
    MagicMock pipeline's auto-attribute must never read as a fault."""
    fault = getattr(pipeline, "price_feed_fault", None)
    return fault if isinstance(fault, str) and fault else None


def reset_price_feed(pipeline, reference_symbol: str | None) -> None:
    """Entry-stage start: no fault carries over, and this is the session's
    reference symbol for classifying a later single-name failure."""
    pipeline.price_feed_fault = None
    pipeline.price_feed_reference = reference_symbol
    pipeline.price_read_failures = {}


def _record_fault(pipeline, where: str, exc: BaseException, **context) -> None:
    """Durable, counted ``disagreed`` row with the traceback; never raises."""
    try:
        record_guarded_outcome(db=getattr(pipeline, "db", None),
                               where=f"price_feed.{where}", exc=exc, log=logger,
                               context=context or None)
    except Exception:  # noqa: BLE001 - an observer must never break the refusal
        logger.error("price_feed fault could not be recorded at %s", where, exc_info=True)


def declare_price_feed_fault(pipeline, where: str, exc: BaseException, *,
                             symbol: str | None = None) -> str:
    """The feed is down: record it durably and refuse every further new entry
    this session. Returns the detail every refusal from now on carries."""
    detail = (
        f"{PRICE_FEED_UNREADABLE}: the live price feed could not be read after "
        f"retry ({where}, {symbol or 'reference'}: {exc}) -- a desk fault, not a "
        "per-name skip: no new entries for the rest of this session; existing "
        "positions keep their broker-held stops"
    )
    logger.error("PRICE FEED UNREADABLE -- %s", detail)
    _record_fault(pipeline, where, exc, symbol=symbol)
    pipeline.price_feed_fault = detail
    return detail


def reference_read_ok(pipeline) -> bool | None:
    """Read the session's reference symbol once (its own retry inside).

    True: the feed answers (absence included) -- a failing name is alone.
    False: the reference read failed too -- the feed is down.
    None: no reference symbol or no stamped reader is known, so nothing can
    be classified; the caller treats the failure as that name's only.
    """
    reference = getattr(pipeline, "price_feed_reference", None)
    getter = getattr(getattr(pipeline, "broker", None), "get_latest_price_stamped", None)
    if not isinstance(reference, str) or not reference or not callable(getter):
        return None
    try:
        getter(reference)
    except PriceReadFailed:
        return False
    return True


def sizing_price_or_refusal(
    reader: Callable[[object, str], float | None], pipeline, symbol: str, what: str,
) -> tuple[float | None, str, str]:
    """``(price, "", "")`` when sizeable, else ``(None, reason, detail)``.

    ``what`` names the thing being sized ("buy", "order", "replacement buy")
    and only changes the wording of the detail.
    """
    fault = price_feed_fault(pipeline)
    if fault:
        return None, PRICE_FEED_UNREADABLE, fault
    try:
        price = reader(pipeline, symbol)
    except SizingPriceUnavailable as exc:
        logger.error(
            "%s sizing price UNREADABLE -- %s refused: %s", symbol, what, exc,
            exc_info=True,
        )
        return (None, *classify_price_read_failure(pipeline, exc, symbol=symbol, what=what))
    if isinstance(price, (int, float)) and not isinstance(price, bool) and price > 0:
        return float(price), "", ""
    return None, NO_SIZING_PRINT, (
        f"no today trade print to size the {what} against (a quote mid or a "
        "prior-session price is not a sizing reference) — refused rather than "
        "sized on a bad price"
    )


def classify_price_read_failure(pipeline, exc: BaseException, *, symbol: str,
                                what: str) -> tuple[str, str]:
    """One name's read failed after retry: is it the name or the desk?

    The reference symbol is read once. If that fails too the feed is down
    and the desk-level fault is declared; otherwise the fault is this name's
    alone, recorded and counted, and the refusal names it as such. When NO
    reference is available (nothing held, no stamped reader) the failure
    cannot be classified: it is still counted as a DESK-level row
    (``price_feed.desk_unclassified``) so a genuine outage is never visible
    only as N single-name rows, and the entry is refused as this name's.
    """
    verdict = reference_read_ok(pipeline)
    if verdict is False:
        return PRICE_FEED_UNREADABLE, declare_price_feed_fault(
            pipeline, "sizing_read", exc, symbol=symbol,
        )
    if verdict is None:
        _record_fault(pipeline, "desk_unclassified", exc, symbol=symbol, what=what)
    _record_fault(pipeline, "single_name", exc, symbol=symbol, what=what)
    return PRICE_READ_FAILED, (
        f"{SIZING_PRICE_UNREADABLE}: the price read for {symbol} failed after "
        f"retry ({exc}) while the reference symbol still reads -- this name's "
        f"{what} is refused rather than priced on an unverified number; other "
        "names keep trading"
    )


def record_price_read_swallowed(pipeline, where: str, *, symbol: str) -> None:
    """For the fill-price read's catch-all (`_today_order_price`): a typed
    read failure is classified and remembered so the stage's skip names it;
    anything else is recorded as the swallow it always was."""
    exc = sys.exc_info()[1]
    if not isinstance(exc, PriceReadFailed):
        record_swallowed_here(pipeline, where, symbol=symbol)
        return
    verdict = classify_price_read_failure(pipeline, exc, symbol=symbol, what="entry")
    failures = getattr(pipeline, "price_read_failures", None)
    if not isinstance(failures, dict):
        failures = pipeline.price_read_failures = {}
    failures[symbol] = verdict


def no_price_skip(pipeline, symbol: str, detail: str = (
    "no verifiable live price (daily bar close is not a fill reference)"
)) -> tuple[str, str]:
    """``(reason, detail)`` for a stage's no-price skip: the classified read
    failure for this name when there is one, the desk fault when one is
    declared, else the plain ``no_price`` the stage always recorded."""
    failures = getattr(pipeline, "price_read_failures", None)
    if isinstance(failures, dict) and symbol in failures:
        return failures.pop(symbol)
    fault = price_feed_fault(pipeline)
    if fault:
        return PRICE_FEED_UNREADABLE, fault
    return "no_price", detail
