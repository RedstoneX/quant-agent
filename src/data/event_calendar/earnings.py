"""Per-symbol earnings proximity: the status vocabulary, the result and the fetch."""

import logging
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# --- earnings proximity ----------------------------------------------------

EARNINGS_MEASURED = "measured"
EARNINGS_NO_FETCHED_DATE = "unavailable_no_fetched_date"
EARNINGS_LOOKUP_FAILED = "unavailable_lookup_failed"
EARNINGS_LOOKUP_TIMEOUT = "unavailable_lookup_timeout"
EARNINGS_DEADLINE_EXCEEDED = "unavailable_deadline_exceeded"

#: The complete status vocabulary, mirroring `pace_status`'s
#: measured / n/a_breakout / unavailable_no_pinned_horizon shape:
#: one value for a real figure, and a NAMED reason for every way the figure can
#: be absent. Never collapse these into a bare None — the whole point is that a
#: reader can tell "the source said nothing" from "the source never answered".
EARNINGS_STATUSES = (
    EARNINGS_MEASURED,
    EARNINGS_NO_FETCHED_DATE,
    EARNINGS_LOOKUP_FAILED,
    EARNINGS_LOOKUP_TIMEOUT,
    EARNINGS_DEADLINE_EXCEEDED,
)

_EARNINGS_ABSENCE_TEXT = {
    EARNINGS_NO_FETCHED_DATE: (
        "NO FETCHED DATE — the earnings-date source answered with nothing for "
        "this symbol. It does NOT mean no report is due: the source cannot "
        'distinguish "no scheduled date published" from "date unknown". '
        "Treat the earnings date as UNKNOWN"
    ),
    EARNINGS_LOOKUP_FAILED: (
        "LOOKUP FAILED — the earnings-date fetch raised. No date was obtained; treat the earnings date as UNKNOWN"
    ),
    EARNINGS_LOOKUP_TIMEOUT: (
        "LOOKUP TIMED OUT — the earnings-date fetch exceeded its per-symbol "
        "ceiling and was abandoned. Treat the earnings date as UNKNOWN"
    ),
    EARNINGS_DEADLINE_EXCEEDED: (
        "NOT ATTEMPTED — the earnings sweep's wall-clock budget was exhausted "
        "before this symbol. Treat the earnings date as UNKNOWN"
    ),
}


#: Sessions-to-earnings inside which a scheduled report is treated as
#: imminent. Not a new threshold: `EarningsProximity.describe()` has marked
#: this same window "INSIDE THE 3-SESSION EVENT WINDOW" for every seat that
#: reads it, and the evening owner report (src/trader_feed.py) now names the
#: same window rather than picking a second one of its own.
EARNINGS_EVENT_WINDOW_SESSIONS = 3


@dataclass
class EarningsProximity:
    """How far away one symbol's next scheduled earnings report is.

    `sessions_away` is populated ONLY when `status == "measured"`. On every
    other status it is None and `status` names which absence this is — the
    `pace_status` contract, applied to the same class of problem.
    """

    symbol: str
    sessions_away: int | None
    status: str

    @property
    def measured(self) -> bool:
        return self.status == EARNINGS_MEASURED and self.sessions_away is not None

    def describe(self) -> str:
        if self.measured:
            imminent = (
                (f" ** INSIDE THE {EARNINGS_EVENT_WINDOW_SESSIONS}-SESSION EVENT WINDOW **")
                if self.sessions_away <= EARNINGS_EVENT_WINDOW_SESSIONS
                else ""
            )
            unit = "session" if self.sessions_away == 1 else "sessions"
            return f"{self.symbol}: next earnings ~{self.sessions_away} {unit} away (fetched){imminent}"
        detail = _EARNINGS_ABSENCE_TEXT.get(
            self.status,
            "UNAVAILABLE — treat the earnings date as UNKNOWN",
        )
        return f"{self.symbol}: {detail} [{self.status}]"


def fetch_earnings_proximity(
    market_provider,
    symbols,
    *,
    per_symbol_timeout_s: float = 8.0,
    total_deadline_s: float = 20.0,
) -> list[EarningsProximity]:
    """Next-earnings proximity for each symbol, from real fetched data.

    This is the caller `MarketDataProvider.get_next_earnings_date` never had.

    `get_next_earnings_date` swallows its own exceptions and returns None for
    BOTH "unknown" and "nothing scheduled" — its own docstring says callers
    must treat None as *unknown*, never as *no earnings soon*. This function is
    where that instruction is actually honoured: None becomes the explicit
    `unavailable_no_fetched_date` label rather than an empty line the reader
    fills in for themselves.

    Bounded twice over, because that method has no timeout of its own: each
    symbol gets `per_symbol_timeout_s`, and the sweep as a whole gets
    `total_deadline_s` of wall clock. Symbols not reached inside the budget are
    returned labelled `unavailable_deadline_exceeded` — they are never dropped,
    because a symbol silently missing from this list would read to the seat as
    a symbol with nothing to report.
    """
    per_symbol_timeout_s = max(0.5, float(per_symbol_timeout_s))
    total_deadline_s = max(per_symbol_timeout_s, float(total_deadline_s))
    deadline = time.monotonic() + total_deadline_s

    ordered: list[str] = []
    for raw in symbols or []:
        symbol = str(raw or "").strip().upper()
        if symbol and symbol not in ordered:
            ordered.append(symbol)

    results: list[EarningsProximity] = []
    for symbol in ordered:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            results.append(
                EarningsProximity(
                    symbol,
                    None,
                    EARNINGS_DEADLINE_EXCEEDED,
                )
            )
            continue
        # Deliberately NOT a `with` block. `ThreadPoolExecutor.__exit__` calls
        # `shutdown(wait=True)`, which blocks until the worker finishes — so on
        # the timeout path the context-manager form waits out the very stall the
        # timeout exists to escape, and the per-symbol ceiling bounds nothing at
        # all. `shutdown(wait=False)` abandons the stuck worker and lets the
        # sweep move on, which is the whole point of having a ceiling. The
        # abandoned thread is bounded in number by `total_deadline_s` and by
        # yfinance's own socket timeouts underneath it.
        executor = ThreadPoolExecutor(max_workers=1)
        try:
            sessions = executor.submit(
                market_provider.get_next_earnings_date,
                symbol,
            ).result(timeout=min(per_symbol_timeout_s, remaining))
        except FuturesTimeout:
            logger.warning(
                "earnings-date lookup timed out for %s (>%.1fs)",
                symbol,
                per_symbol_timeout_s,
            )
            results.append(
                EarningsProximity(
                    symbol,
                    None,
                    EARNINGS_LOOKUP_TIMEOUT,
                )
            )
            continue
        except Exception as e:  # noqa: BLE001 — any provider shape degrades
            logger.warning("earnings-date lookup failed for %s: %s", symbol, e)
            results.append(
                EarningsProximity(
                    symbol,
                    None,
                    EARNINGS_LOOKUP_FAILED,
                )
            )
            continue
        finally:
            executor.shutdown(wait=False, cancel_futures=True)

        if sessions is None:
            results.append(
                EarningsProximity(
                    symbol,
                    None,
                    EARNINGS_NO_FETCHED_DATE,
                )
            )
            continue
        try:
            results.append(
                EarningsProximity(
                    symbol,
                    max(0, int(sessions)),
                    EARNINGS_MEASURED,
                )
            )
        except (TypeError, ValueError):
            results.append(
                EarningsProximity(
                    symbol,
                    None,
                    EARNINGS_LOOKUP_FAILED,
                )
            )
    return results
