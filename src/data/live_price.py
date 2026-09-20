"""One place that decides whether a broker snapshot holds TODAY's price.

WHY THIS EXISTS (docs/WORK.md item 120)
---------------------------------------
On 2026-09-17, 8 of 104 names — one of them a holding — carried YESTERDAY's
last trade in the morning snapshot while today's forming bar in the same
payload already held the open. Two separate failures sat behind that one
observation:

  * a name whose `latest_trade` was stale lost its technical seat outright,
    even though a real today print (the 1-minute bar, the forming session
    bar) was sitting in the same response on the same entitled venue; and
  * the `session_*` block was rendered to the analyst as "CURRENT SESSION
    (TODAY)" with nothing checking the bar's own date, so a name that had
    not printed today could show YESTERDAY's open/high/low/volume labelled
    as today's.

This module is the single resolver both defects are fixed through. It is
PURE — no network, no clock beyond the one injected — so the thing that
decides what "today" means is testable without a broker.

THE RULE
--------
A point-in-time price counts as this session's when BOTH hold:

  * its ET date equals the current ET date
    (`trading_calendar.live_price_is_today`), and
  * it is stamped at or after today's regular-session open, 09:30 ET
    (`trading_calendar.REGULAR_SESSION_OPEN_MIN`).

**No new threshold is introduced.** Both bounds already exist in this repo
and neither is fitted: the date test is the desk's ratified freshness rule
(stop-repair ruling, 2026-09-19), and 09:30 ET is the NYSE core-session
open, a fact the exchange publishes months ahead. `src/api/broker_reads.py
::_quote_freshness` already uses that same boundary for this same field and
says why in its own docstring. There is no elapsed-minutes "recent enough"
cutoff anywhere here — that WOULD be the arbitrary number this desk
refuses, and it is deliberately absent.

The second bound was added after the first version of this module shipped
with date-equality alone. `docs/INCIDENT_HISTORY.md` records the desk
rejecting a proposal on 2026-09-18 for precisely that weakness: a price
"sitting from before the market opened would still count as 'today's' and
could sit on the wrong side of the real price on a gap day". A 04:12 ET
pre-market print rendered to a seat at 09:30:05 as the current price is
that defect, one field over from the one this module was written for.

The open bound needs no holiday or early-close calendar: 09:30 is the open
on a half-day too, and on a holiday nothing prints at all.

A DAILY BAR IS DATED, NOT STAMPED. Today's forming session bar carries its
OPENING timestamp (00:00 ET), which is before the open by construction, so
the 09:30 bound cannot apply to it. It is dated by date-equality alone —
correctly, because a bar dated today IS today's session by definition.

AMONG FRESH CANDIDATES, THE MOST RECENT WINS. Precedence by purity alone
would price a name off its 09:31 last trade all afternoon while its own
minute bars kept updating — item 120's defect in same-day form, and one no
date test can ever catch. Purity breaks ties, recency does not lose to it.

PRECEDENCE, and why
-------------------
Among candidates that pass the rule above, the most recent wins; where two
share a stamp, this order breaks the tie:

1. `last_trade`   — the actual print. What the desk wants.
2. `minute_bar`   — an aggregation of real prints on the same entitled
                    venue. A print, one minute coarse.
3. `session_bar`  — today's still-forming daily bar. Coarser again, and
                    considered only when no point-in-time candidate is
                    usable, because its stamp cannot be compared for
                    recency against theirs.
4. nothing        — the name has no usable print from this session. A LOST
                    SEAT, named, not a number worn as if it were current.

A QUOTE MID IS NEVER USED HERE. A quote is what somebody is willing to do,
not what was done; the board item says so in as many words, and no branch
in this module can reach one.

That is a rule about the RESEARCH price, not a desk-wide rule, and the
distinction matters because the repo contains the opposite decision one
layer over. `src/execution/broker.py::get_latest_price_stamped` returns a
quote midpoint, and `src/pipeline_stages.py::_today_order_price` accepts
one as a FILL REFERENCE — deliberately, and covered by its own test. The
two are not in conflict: bounding an order you are about to send against
the current book is a different act from telling an analyst what a company
is worth. Nothing here changes the order path.

WHAT THIS DOES NOT DO
---------------------
It cannot tell a halted name from a thin one — both look like "no print
this session" and both are correctly a lost seat rather than a guess.

It does not pin the market-data feed. `get_intraday_snapshots` sends no
`feed` argument, so the venue is whatever the account defaults to. The
claim that the minute bar and the session bar are prints "on the same
entitled venue" is true because they come from the same response as the
last trade — not because anything here enforces it. Pinning the feed was
part of what board item 120 described and is NOT done here.

It does not know about early closes. `trading_calendar.in_regular_session`
treats every weekday as running to 16:00, so on a 13:00 half-day a price
from 12:59 is still rendered between 13:00 and 16:00 as though the session
were open. That is a pre-existing property of the session window, not
something this module introduces, and it is not fixed here.

**One thing in this module is asserted and not yet observed:** that Alpaca
stamps a DAILY bar at a time whose ET date equals the session date. The
SDK documents `Bar.timestamp` as the bar's opening timestamp and the
published daily-bar convention is 00:00 ET normalised to UTC, but no live
snapshot call has been made from this desk to confirm it for this account.
If that is wrong the comparison inverts and every name loses its session
range — which fails VISIBLE (a blanked range and a warning), never toward
showing a prior session as this one. Confirming it is the first thing to
look at after a real session.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from src.trading_calendar import (
    REGULAR_SESSION_OPEN_MIN,
    live_price_is_today,
    to_et,
)

#: Source labels, in precedence order. Exported so callers and tests name
#: the same strings rather than retyping them.
SOURCE_LAST_TRADE = "last_trade"
SOURCE_MINUTE_BAR = "minute_bar"
SOURCE_SESSION_BAR = "session_bar"
#: The forming bar's OPEN, used only when it has published no close yet.
#: A separate label because saying "close" when the number is the open is a
#: false provenance string reaching a paid seat.
SOURCE_SESSION_BAR_OPEN = "session_bar_open"

#: Human-readable rendering for each source, for prompts and logs. A seat
#: told "last trade" when it is really a one-minute aggregate has been
#: misled about precision, which is the same class of defect as being told
#: yesterday is today.
SOURCE_DESCRIPTION = {
    SOURCE_LAST_TRADE: "last trade print",
    SOURCE_MINUTE_BAR: "close of today's latest 1-minute bar",
    SOURCE_SESSION_BAR: "close of today's still-forming session bar",
    SOURCE_SESSION_BAR_OPEN: "open of today's session bar (no close published yet)",
}

#: Reasons a name resolves to no price. Distinct strings because "the feed
#: gave us nothing at all" and "the feed gave us yesterday" are different
#: conditions and get counted separately.
NO_SNAPSHOT = "no snapshot returned for this symbol"
NO_PRICE_AT_ALL = "no live trade price returned"
ONLY_STALE = "no print from this session (latest is from before today's open)"


@dataclass(frozen=True)
class ResolvedPrice:
    """A price known to come from today, or an explicit refusal.

    `price` and `source` are both set, or both None. There is no third
    state where a number is carried without saying where it came from —
    that ambiguity is what board item 120 is about.
    """

    price: float | None
    source: str | None
    as_of: datetime | None
    unavailable: str | None
    #: True when the snapshot's `session_*` block belongs to TODAY. False
    #: means the caller must not render those fields as this session's.
    session_bar_is_today: bool

    @property
    def is_today_print(self) -> bool:
        return self.price is not None

    def describe(self) -> str:
        if self.source is None:
            return self.unavailable or ONLY_STALE
        return SOURCE_DESCRIPTION.get(self.source, self.source)


def _positive(value) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    if value != value or value in (float("inf"), float("-inf")):  # NaN / inf
        return None
    return value if value > 0 else None


def _is_from_this_session(stamp, when) -> bool:
    """True when a point-in-time stamp is today AND at/after today's open.

    Both bounds are named in this module's docstring; neither is fitted.
    A missing or naive stamp is not from this session — unknown freshness
    fails visible.
    """
    if not live_price_is_today(stamp, when):
        return False
    et = to_et(stamp)
    return et.hour * 60 + et.minute >= REGULAR_SESSION_OPEN_MIN


def resolve_live_price(snapshot, when: datetime | None = None) -> ResolvedPrice:
    """Resolve one `get_intraday_snapshots` payload into today's price.

    `snapshot` is the per-symbol dict that method returns (or None / {} when
    the symbol came back empty). `when` overrides the clock for tests and is
    passed straight through to the freshness test, so there is exactly one
    definition of "today" in play.

    Never raises: a malformed payload resolves to unavailable, which is the
    fail-visible direction.
    """
    if not isinstance(snapshot, dict) or not snapshot:
        return ResolvedPrice(None, None, None, NO_SNAPSHOT, False)

    session_bar_at = snapshot.get("session_bar_at")
    session_bar_is_today = bool(live_price_is_today(session_bar_at, when))

    # The forming session bar's CLOSE is the latest print inside it, which
    # is what "the current price" means. Its OPEN is the fallback only
    # because a bar one minute old may not have published a close yet —
    # DEFENSIVE, not observed: no Alpaca daily bar with a valid open and no
    # close has been seen here. It gets its own source label so a seat
    # priced off the open is never told it was priced off the close.
    session_price = _positive(snapshot.get("session_close"))
    session_source = SOURCE_SESSION_BAR
    if session_price is None:
        session_price = _positive(snapshot.get("session_open"))
        session_source = SOURCE_SESSION_BAR_OPEN

    # Point-in-time candidates: date-equal AND at or after today's open.
    point_in_time = (
        (SOURCE_LAST_TRADE,
         _positive(snapshot.get("last_price")),
         snapshot.get("last_trade_at")),
        (SOURCE_MINUTE_BAR,
         _positive(snapshot.get("minute_close")),
         snapshot.get("minute_bar_at")),
    )

    saw_any_price = session_price is not None
    usable: list[tuple] = []
    for rank, (source, price, stamp) in enumerate(point_in_time):
        if price is None:
            continue
        saw_any_price = True
        if not _is_from_this_session(stamp, when):
            continue
        usable.append((to_et(stamp), -rank, source, price, stamp))

    if usable:
        # Most recent first; purity (the negated rank) breaks a tie.
        usable.sort(reverse=True)
        _, _, source, price, stamp = usable[0]
        return ResolvedPrice(price, source, stamp, None, session_bar_is_today)

    if session_price is not None and session_bar_is_today:
        return ResolvedPrice(session_price, session_source, session_bar_at,
                             None, session_bar_is_today)

    # Distinguish "the feed had nothing" from "the feed had only a prior
    # session". A thin name and a broken feed are different problems and the
    # log has to be able to tell them apart.
    reason = ONLY_STALE if saw_any_price else NO_PRICE_AT_ALL
    return ResolvedPrice(None, None, None, reason, session_bar_is_today)
