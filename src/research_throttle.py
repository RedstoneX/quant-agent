"""Stop paying to hunt for new trades when the book has no room for one.

Owner ask, 2026-09-30: the desk runs several sessions a day looking for new
trades; when the portfolio is already full that hunting is largely wasted
paid model and paid search spend. It must still be "a balancing act" —
every holding must keep justifying its place on the NORMAL cadence.

So this module throttles the SEARCH, never the REVIEW:

* When the book is full, the research fan-out is narrowed to the names the
  desk already holds (plus this run's free, deterministic admissions —
  see below). Every holding still gets its full five-seat read on every
  normal session, because doctrine requires all five seats to be right to
  STAY, not only to enter.
* The midday / close position-review sessions and the intraday check are
  untouched by this module. They do not hunt.

"Full" is NOT a picked number. It is read off the desk's own ratified
ceilings, so the throttle turns itself off the moment the book stops being
full — no schedule, no cadence dial, nothing to tune:

* `deployable_cash <= 0`   — nothing to buy with without borrowing.
* `deployed_pct >= risk.max_total_position_pct` — the invested ceiling.
* `gross_pct >= risk.max_gross_exposure_x * 100` — the §11.2 gross ceiling.

THE URGENCY LANE. A throttled session is not a blind session:

1. Every deterministic safety step — stop-coverage audit, protection
   restores, forced de-lever, gross-ceiling enforcement, fill and stop-out
   reconciliation — runs BEFORE the paid boundary and is not touched here.
2. Held names keep their full research, so a holding whose thesis has
   broken is still found, voiced and sold on the normal cadence.
3. Run-scoped admissions (`ctx.admitted_symbols`: SEC Form 4 smart-money
   and the universe screen) survive the narrowing. Those come from free,
   deterministic signals, so a genuinely urgent NEW name still reaches the
   seats even while the hunt is throttled.
4. The moment anything frees capital — a stop-out, a de-lever, a sale from
   the review — the ceilings above stop being breached and the very next
   session hunts normally again.

Fails OPEN: any uncertainty here returns "not full" and the full hunt runs.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def _f(value) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if out != out or out in (float("inf"), float("-inf")):
        return None
    return out


def full_book_reason(
    *,
    deployable_cash,
    deployed_pct,
    gross_pct,
    max_total_position_pct,
    max_gross_exposure_x,
) -> str | None:
    """Plain-English reason the book is full, or None if it has room.

    Every threshold is an existing ratified ceiling read from config. This
    function invents no number of its own.
    """
    cash = _f(deployable_cash)
    if cash is not None and cash <= 0:
        return "no deployable cash — a new position would have to be borrowed"

    deployed = _f(deployed_pct)
    invested_cap = _f(max_total_position_pct)
    if deployed is not None and invested_cap is not None and deployed >= invested_cap:
        return (
            f"invested {deployed:.1f}% of the account against its own "
            f"{invested_cap:.1f}% ceiling"
        )

    gross = _f(gross_pct)
    gross_x = _f(max_gross_exposure_x)
    if gross is not None and gross_x is not None and gross >= gross_x * 100:
        return (
            f"gross exposure {gross / 100:.2f}x against its own "
            f"{gross_x:.2f}x ceiling"
        )

    return None


def narrow_to_held(effective_symbols, held_symbols, admitted_symbols=None):
    """Research surface for a full book: held names + free admissions.

    Order is preserved from `effective_symbols` so the existing chunking and
    recovery budget behave exactly as before on the names that remain.
    """
    keep = {str(s).strip().upper() for s in (held_symbols or []) if str(s).strip()}
    keep |= {str(s).strip().upper() for s in (admitted_symbols or []) if str(s).strip()}
    if not keep:
        # Nothing held and nothing admitted cannot be a full book; refuse to
        # hand the seats an empty universe.
        return list(effective_symbols)
    return [s for s in effective_symbols if s in keep]
