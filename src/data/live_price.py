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
Freshness is ET-DATE EQUALITY against the current ET date, and nothing
else. That is `trading_calendar.live_price_is_today`, which is already the
desk's ratified test (the stop-repair freshness ruling, 2026-09-19: "the
freshness test is date-equality only"). **No new threshold is introduced
here** — there is no "recent enough" minutes cutoff to source or to file in
`config/number_ledger.yaml`, deliberately, because an elapsed-minutes bound
would be exactly the arbitrary number this desk refuses.

PRECEDENCE, and why
-------------------
1. `last_trade`   — the actual print. What the desk wants.
2. `minute_bar`   — an aggregation of real prints on the same entitled
                    venue. A print, one minute coarse.
3. `session_bar`  — today's still-forming daily bar close; also an
                    aggregation of real prints, coarser again.
4. nothing        — the name has no today print. A LOST SEAT, named, not a
                    number worn as if it were current.

A QUOTE MID IS NEVER USED. A quote is what somebody is willing to do, not
what was done; the board item says so in as many words, and no branch here
can reach one.

WHAT THIS DOES NOT DO
---------------------
It cannot tell a halted name from a thin one — both look like "no today
print" and both are correctly a lost seat rather than a guess. It does not
know about early closes or holidays; on a holiday nothing prints, so every
name resolves to no-today-print, which is the right answer arrived at
without a holiday calendar.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from src.trading_calendar import live_price_is_today

#: Source labels, in precedence order. Exported so callers and tests name
#: the same strings rather than retyping them.
SOURCE_LAST_TRADE = "last_trade"
SOURCE_MINUTE_BAR = "minute_bar"
SOURCE_SESSION_BAR = "session_bar"

#: Human-readable rendering for each source, for prompts and logs. A seat
#: told "last trade" when it is really a one-minute aggregate has been
#: misled about precision, which is the same class of defect as being told
#: yesterday is today.
SOURCE_DESCRIPTION = {
    SOURCE_LAST_TRADE: "last trade print",
    SOURCE_MINUTE_BAR: "close of today's latest 1-minute bar",
    SOURCE_SESSION_BAR: "close of today's still-forming session bar",
}

#: Reasons a name resolves to no price. Distinct strings because "the feed
#: gave us nothing at all" and "the feed gave us yesterday" are different
#: conditions and get counted separately.
NO_SNAPSHOT = "no snapshot returned for this symbol"
NO_PRICE_AT_ALL = "no live trade price returned"
ONLY_STALE = "no print from today's session (latest is from a prior session)"


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
    # is what "the current price" means; `session_open` is the fallback only
    # because a bar one minute old may not have published a close yet. The
    # bar is dated by `session_bar_at`, its OPENING stamp — the only
    # timestamp the provider gives for it.
    session_price = _positive(snapshot.get("session_close"))
    if session_price is None:
        session_price = _positive(snapshot.get("session_open"))

    candidates = (
        (SOURCE_LAST_TRADE,
         _positive(snapshot.get("last_price")),
         snapshot.get("last_trade_at"),
         None),
        (SOURCE_MINUTE_BAR,
         _positive(snapshot.get("minute_close")),
         snapshot.get("minute_bar_at"),
         None),
        (SOURCE_SESSION_BAR, session_price, session_bar_at, session_bar_is_today),
    )

    saw_any_price = False
    for source, price, stamp, precomputed_fresh in candidates:
        if price is None:
            continue
        saw_any_price = True
        fresh = (precomputed_fresh if precomputed_fresh is not None
                 else bool(live_price_is_today(stamp, when)))
        if not fresh:
            continue
        return ResolvedPrice(price, source, stamp, None, session_bar_is_today)

    # Distinguish "the feed had nothing" from "the feed had only yesterday".
    # A thin name and a broken feed are different problems and the log has
    # to be able to tell them apart.
    reason = ONLY_STALE if saw_any_price else NO_PRICE_AT_ALL
    return ResolvedPrice(None, None, None, reason, session_bar_is_today)
