"""Gate the decision on evidence that is ABSENT, not on evidence that is thin.

docs/WORK.md item 20, the owner's own design (2026-09-02):

    "the decision matrix is flawed — it's asking it to make a decision when
    it doesn't have enough information to make an informed logical
    decision."

A decision built on evidence the desk never received is not a degraded
decision, it is a fabricated one. This module decides, deterministically and
with no LLM call, whether the expensive Portfolio Manager call should run at
all — before it runs, so a run on absent evidence costs nothing instead of
~$0.55 on the seat that is 93% of the bill.

WHY THIS NEEDS NO THRESHOLD, AND DELIBERATELY HAS NONE
-------------------------------------------------------
The owner's design (b) is "below threshold → do not decide", and his own
ruling on that threshold is explicit: *"Threshold is NOT an agent's to
invent. It is a risk judgement. Propose a number with reasoning and have it
ratified; do not let a coding agent pick one, and do not ship a
placeholder."* No published source states how many of five research seats a
discretionary equity desk needs before a decision is sound, and the desk's
own trade history cannot supply one without fitting a number to it, which
docs/WORK.md forbids outright.

So this gate does not count anything. It rests on a distinction the desk
ALREADY draws and which is categorical:

    a seat that had NOTHING TO SAY is not the same as a seat whose
    ANSWER WAS LOST.

"No Form 4 filings today" is a real, complete answer: the evidence exists
and it says nothing is happening. "The macro report did not parse" is not an
answer at all — the desk asked, an answer existed to be had, and it never
arrived. Deciding on the first is ordinary. Deciding on the second is
deciding on evidence the desk does not have, which is the thing item 20
forbids. That line needs no number, cannot drift, and cannot be quietly
fitted.

The counting half of the owner's design — "how many earnings reports are
usable, how many technical reads survived validation" — is NOT built here,
because it cannot be built without the number he reserved to himself. Item
20 stays open naming only that remaining question. The intraday status
split is built: `not_run_intraday` is the intentional skip, and empty or
failed morning carry-forward is `carry_forward_empty` /
`carry_forward_failed`, which this gate treats as lost.

WHAT IT DOES NOT DO
--------------------
It never zeroes a target and never drops an individual candidate: a 0%
target is read by this system as "sell it" (docs/WORK.md), so a refusal here
returns BEFORE any target exists rather than emitting one. It refuses the
run, records why, and the next scheduled decision opportunity tries again.

An unknown status value is NOT treated as a loss. A gate that can stop the
desk trading must never acquire new bite by accident when some other seat
gains a status word — see docs/INCIDENT_HISTORY.md for the gate on this desk
that refused 100% of what it saw for weeks with nobody noticing. Unknown
values log at ERROR and pass; `tests/test_evidence_gate.py` enumerates the
vocabulary so a new value has to be classified deliberately, in a diff.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

#: A usable answer arrived. The seat may be weak, mixed, self-doubting or
#: even provably wrong, but the desk HAS its answer and can weigh it.
#: `figures_contradicted` sits here on purpose: a confident wrong number is
#: a different failure from an absent one (it already pages through
#: `notifier.maybe_alert_data_quality`), and item 20 is about absence.
#: `partial` and `symbol_dropped` likewise: judging how much partial is too
#: much is the counting question that needs the owner's ratified number.
CATEGORY_REPORTED = "reported"

#: The seat answered, and the honest answer is that there is nothing to
#: report. Evidence is not missing; it exists and it is empty. Deciding on
#: this is ordinary desk behaviour, not deciding on absent evidence.
CATEGORY_NOTHING_TO_REPORT = "nothing_to_report"

#: The desk asked, an answer existed to be had, and it never arrived — the
#: call raised, the response would not parse, the provider failed outright,
#: the generation was cut off mid-answer, or every analyzed filing came back
#: with no content. This, and only this, refuses the decision.
CATEGORY_LOST = "lost"

#: Every value any seat writes into `RunContext.data_status`, classified.
#: Sources, all in this repo: `MorningResearchStage.run` (macro / news /
#: tech / earnings / smart_money), `_classify_earnings_status`,
#: `_apply_sector_unresolved_alert`, and `TradingPipeline._run_intraday_scan`
#: (the carried-forward intraday dict).
STATUS_CATEGORY: dict[str, str] = {
    # --- a usable answer arrived ---
    "ok": CATEGORY_REPORTED,
    "partial": CATEGORY_REPORTED,
    "low_confidence": CATEGORY_REPORTED,
    "symbol_dropped": CATEGORY_REPORTED,
    "degraded": CATEGORY_REPORTED,
    "figures_contradicted": CATEGORY_REPORTED,
    "carried_from_morning": CATEGORY_REPORTED,
    # --- a real answer that is legitimately empty ---
    # `empty`: smart money found no material Form 4 activity, or tech was
    #   handed no symbols. A quiet day is a fact, not a gap.
    # `release_overdue`: every FRED series answered; a statistical agency
    #   has not published the next print. The world has nothing newer to
    #   give — that is the answer.
    # `not_run_intraday`: this tick chose not to re-fetch a seat (earnings
    #   on the intraday scan). Not asked is not lost. Empty or failed
    #   morning carry-forward is a different word, below.
    "empty": CATEGORY_NOTHING_TO_REPORT,
    "release_overdue": CATEGORY_NOTHING_TO_REPORT,
    "not_run_intraday": CATEGORY_NOTHING_TO_REPORT,
    # --- the answer was lost ---
    "failed": CATEGORY_LOST,
    "parse_error": CATEGORY_LOST,
    "provider_error": CATEGORY_LOST,
    "truncated": CATEGORY_LOST,
    "content_missing": CATEGORY_LOST,
    # Intraday carry-forward of this morning's macro/news: the lookup
    # came back empty (morning never wrote a today-dated answer) or the
    # lookup itself failed. Same owner rule as a morning `failed` —
    # deciding on it would be fabricating the missing seat.
    "carry_forward_empty": CATEGORY_LOST,
    "carry_forward_failed": CATEGORY_LOST,
}

#: Statuses that are NOT an upstream integrity problem for Risk's 2+
#: `data_degraded` advisory, or for the operator "degraded" banners that
#: share the same allow-list.
#:
#: `ok` / `empty` were the original pair. `carried_from_morning` is this
#: morning's already-paid answer reused on an intra tick — same session,
#: same objects, by design, not a lost seat. `not_run_intraday` is the
#: intentional skip (earnings on the scan): this tick chose not to re-pay
#: the seat. Measured 2026-09-16: treating those two as degraded made
#: Risk veto a whole intra plan ~40 minutes after a successful morning,
#: because the advisory always saw 2+ "failures" on a clean reuse tick.
#: Age-as-staleness is not the defect; empty/failed carry-forward still
#: refuses BEFORE the Portfolio Manager, via CATEGORY_LOST above.
INTEGRITY_CLEAN_STATUSES: frozenset[str] = frozenset({
    "ok",
    "empty",
    "carried_from_morning",
    "not_run_intraday",
})


def counts_as_degraded(status: str) -> bool:
    """True when this seat-status should feed the 2+ data_degraded advisory.

    Never raises. An unknown word counts as degraded so a new failure
    mode cannot silently drop out of the advisory; classify it in
    STATUS_CATEGORY *and* decide whether it belongs in
    INTEGRITY_CLEAN_STATUSES in the same diff.
    """
    return str(status) not in INTEGRITY_CLEAN_STATUSES


@dataclass(frozen=True)
class EvidenceGateVerdict:
    """Why the decision may or may not proceed. Machine-readable and durable
    — `to_evidence()` is what gets persisted, per symbol and per run."""

    lost: list[str] = field(default_factory=list)
    nothing_to_report: list[str] = field(default_factory=list)
    reported: list[str] = field(default_factory=list)
    unclassified: list[str] = field(default_factory=list)
    data_status: dict[str, str] = field(default_factory=dict)

    @property
    def skip(self) -> bool:
        """True when at least one seat's answer was lost outright."""
        return bool(self.lost)

    @property
    def reason(self) -> str:
        if not self.lost:
            return "every seat reported or was honestly empty"
        detail = ", ".join(
            f"{seat}={self.data_status.get(seat)}" for seat in self.lost
        )
        return (
            f"decision skipped: {len(self.lost)} seat(s) were asked and their "
            f"answer never arrived — {detail}. A decision resting on an answer "
            f"the desk never received is not a degraded decision, it is a "
            f"fabricated one (docs/WORK.md item 20)."
        )

    def to_evidence(self) -> dict:
        return {
            "gate": "evidence_coverage",
            "outcome": "skip" if self.skip else "proceed",
            "lost_seats": list(self.lost),
            "nothing_to_report_seats": list(self.nothing_to_report),
            "reported_seats": list(self.reported),
            "unclassified_seats": list(self.unclassified),
            "data_status": dict(self.data_status),
            "reason": self.reason,
        }


def evaluate(data_status: dict | None) -> EvidenceGateVerdict:
    """Classify this run's seat statuses. NEVER raises.

    A gate that can stop the desk trading must not be able to stop it by
    crashing either: anything unexpected in `data_status` is reported and
    passed, never converted into a refusal.
    """
    if not isinstance(data_status, dict):
        return EvidenceGateVerdict(data_status={})

    lost: list[str] = []
    nothing: list[str] = []
    reported: list[str] = []
    unclassified: list[str] = []
    clean: dict[str, str] = {}

    for seat, value in data_status.items():
        seat_name = str(seat)
        text = str(value)
        clean[seat_name] = text
        category = STATUS_CATEGORY.get(text)
        if category == CATEGORY_LOST:
            lost.append(seat_name)
        elif category == CATEGORY_NOTHING_TO_REPORT:
            nothing.append(seat_name)
        elif category == CATEGORY_REPORTED:
            reported.append(seat_name)
        else:
            unclassified.append(seat_name)
            logger.error(
                "evidence gate: data_status[%r]=%r is not in STATUS_CATEGORY "
                "— treating it as a reported answer and NOT refusing the "
                "decision. Classify it in src/evidence_gate.py deliberately; "
                "a refusal gate must never gain bite by accident.",
                seat_name, text,
            )

    return EvidenceGateVerdict(
        lost=sorted(lost),
        nothing_to_report=sorted(nothing),
        reported=sorted(reported),
        unclassified=sorted(unclassified),
        data_status=clean,
    )
