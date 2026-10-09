"""The one definition of the structural-level touch bar, and its count.

`src.config.RiskConfig.min_level_touches_for_stop_honor` (5) decides how
many prior touches a computed structural level must carry before the desk
will rest a stop on it. It is filed `arbitrary` in `config/number_ledger.yaml`
and this module does not change that: it makes the gate COUNTABLE, which is
the precondition for ever settling it.

Why a counted recorder rather than a re-derivation.

* The table the 5 was originally read off (docs/RESEARCH_FINDINGS.md §7) was
  measured against levels clustered by a flat 1%-of-price rule. Board item 55
  deleted that rule and clusters on the measured overlap of the bars that drew
  each pivot instead, so the OBJECT the touch count counts is not the object
  that was measured. The ledger row records exactly that as the reason the
  number was downgraded from `sourced`.
* Re-run on the desk's own committed daily bars with the CURRENT level
  definition, the touch-count curve does not separate from a shuffled-returns
  control that preserves each symbol's return and bar-range distributions.
  That is a negative result, not a derivation, and it is reported as one.
* The desk's production database carries no row attributable to this gate at
  all, in any of its tables. The gate has therefore NEVER BEEN OBSERVED — which
  is not the same as never having bound, and the difference is exactly what
  this module exists to remove.

So: no behaviour change, no new number, no deletion. One shared predicate, and
a tally of how often it admitted a level, refused one for being under-touched,
and refused one whose touch count was not recorded at all.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

#: Where in the stop derivation the gate was consulted. Two call sites read
#: the same bar for different purposes, and a count that merged them could not
#: tell "no tight-stop exemption" (a wider stop) from "no structural anchor"
#: (a different stop rule entirely).
SITE_TIGHT_STOP_EXEMPTION = "tight_stop_exemption"
SITE_NO_ATR_STRUCTURAL_ANCHOR = "no_atr_structural_anchor"

#: Outcomes. `unverified` is kept apart from `under_touched` because they have
#: different causes: an under-touched level is a real level the bar rejected,
#: while an unverified one is a level whose touch count never reached the
#: constructor (an older stored analysis, or a fixture). Both fail closed.
OUTCOME_ADMITTED = "admitted"
OUTCOME_UNDER_TOUCHED = "under_touched"
OUTCOME_UNVERIFIED = "unverified"

#: Stage name for the counted summary, distinct from any per-name row so a
#: reader can count gate decisions without double-counting candidates.
LEVEL_TOUCH_GATE_STAGE = "level_touch_gate"


@dataclass
class LevelTouchTally:
    """Counts of every touch-bar decision, by call site and outcome.

    Stores no derived state and no thresholds: it holds raw counts and
    computes everything else on read, so there is nothing here that can go
    stale against the config.
    """

    outcomes: Counter = field(default_factory=Counter)
    #: Touch counts actually seen, so the distribution the bar is cutting can
    #: be read back rather than assumed. `None` (unrecorded) is not binned.
    touches_seen: Counter = field(default_factory=Counter)

    def note(self, site: str, outcome: str, touches: int | None) -> None:
        self.outcomes[(site, outcome)] += 1
        if touches is not None:
            self.touches_seen[int(touches)] += 1

    def count(self, site: str | None = None, outcome: str | None = None) -> int:
        """How many decisions matched. Either filter may be left open."""
        total = 0
        for (seen_site, seen_outcome), n in self.outcomes.items():
            if site is not None and seen_site != site:
                continue
            if outcome is not None and seen_outcome != outcome:
                continue
            total += n
        return total

    @property
    def refusals(self) -> int:
        """Every decision in which the bar cost the desk a level."""
        return self.count(outcome=OUTCOME_UNDER_TOUCHED) + self.count(outcome=OUTCOME_UNVERIFIED)

    def bound(self) -> bool:
        """Did the bar change any outcome? False means NEVER EXERCISED."""
        return self.refusals > 0

    def summary_row(self) -> dict:
        """The counted record, computed from the raw counts on every read."""
        return {
            "stage": LEVEL_TOUCH_GATE_STAGE,
            "decisions": self.count(),
            "admitted": self.count(outcome=OUTCOME_ADMITTED),
            "under_touched": self.count(outcome=OUTCOME_UNDER_TOUCHED),
            "unverified": self.count(outcome=OUTCOME_UNVERIFIED),
            "bound": self.bound(),
            "by_site": {
                site: {
                    "admitted": self.count(site=site, outcome=OUTCOME_ADMITTED),
                    "under_touched": self.count(site=site, outcome=OUTCOME_UNDER_TOUCHED),
                    "unverified": self.count(site=site, outcome=OUTCOME_UNVERIFIED),
                }
                for site in (
                    SITE_TIGHT_STOP_EXEMPTION,
                    SITE_NO_ATR_STRUCTURAL_ANCHOR,
                )
            },
            "touches_seen": dict(sorted(self.touches_seen.items())),
        }


#: The process-wide tally. A module-level counter is the whole point: the two
#: call sites sit on different objects and a per-call return value would be
#: discarded by every existing caller, which is how this gate came to have no
#: record in six weeks of running.
GATE_TALLY = LevelTouchTally()


def level_clears_touch_bar(
    touches: int | None,
    min_touches: int,
    *,
    site: str,
    tally: LevelTouchTally | None = None,
) -> bool:
    """Is this level touched often enough to rest a stop on? Count either way.

    Fails closed on an unrecorded touch count, exactly as both call sites did
    inline before: a level whose touch count cannot be shown to have cleared
    the bar is treated as below it, never as above it.
    """
    record = GATE_TALLY if tally is None else tally
    if touches is None:
        record.note(site, OUTCOME_UNVERIFIED, None)
        return False
    try:
        seen = int(touches)
    except (TypeError, ValueError):
        record.note(site, OUTCOME_UNVERIFIED, None)
        return False
    if seen < min_touches:
        record.note(site, OUTCOME_UNDER_TOUCHED, seen)
        return False
    record.note(site, OUTCOME_ADMITTED, seen)
    return True
