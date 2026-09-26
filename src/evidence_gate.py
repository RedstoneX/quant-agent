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

WHICH SEATS CAN REFUSE — MANDATE DECISION, OWNER, 2026-09-18
------------------------------------------------------------
    "Only technical analysis can stop the desk."

Before that ruling the blocking set was every seat, which nobody had
decided: it was a side effect of which seat happened to write a status word
that classified as LOST, so any seat gaining a new failure word silently
gained the power to stop trading. It is now declared, in `BLOCKING_SEATS`
below, and that constant is the only place it may be widened.

The gate's PRINCIPLE is unchanged and still owner-ratified: the desk must
not decide on evidence that never arrived. What changed is the scope of the
refusal, not the standard. A lost advisory seat is still recorded, still
named to the owner, and still feeds the `data_degraded` advisory — it simply
no longer halts the desk on its own.

FRESHNESS DISCLOSURE — WHY IT SHIPS IN THE SAME CHANGE
-------------------------------------------------------
With the other seats advisory, a decision can rest on ONE freshly-read seat
plus a carried-forward book, and every status involved
(`carried_from_morning`, `not_run_intraday`, `chose_not_to_refetch`,
`remembered`) is integrity-clean, so such a decision reported five green
seats and nothing anywhere counted how many were actually READ ON THIS TICK.
That is silent degradation, which this desk bans independently of any
mandate.

So every decision now DISCLOSES its own evidence freshness: which seats were
read on this tick, which are carried from earlier, which are absent. It is
disclosure, not a threshold. No minimum number of fresh seats exists here
and none may be invented — that number is the owner's, same as the coverage
count above.

`expired` IS NOT A LOST ANSWER
------------------------------
It used to classify as lost, which contradicted this module's own
categorical line. `expired` means the desk HOLDS a good answer and knows a
newer one exists — neither "nothing to say" nor "the answer never arrived".
It now has its own category. It is not integrity-clean, so it still counts
toward the `data_degraded` advisory, and the freshness disclosure names it
explicitly as an answer known to be out of date.

What it does NOT do, since 2026-09-23, is raise the owner a separate red
page saying the session "ran on incomplete research" — that page is for a
seat whose answer never arrived, and the desk holds this one. See
`DISCLOSE_ONLY_STATUSES` and `warrants_data_quality_page` below for the
split and for what was measured before making it.

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
#: with no content. This, and only this, can refuse the decision — and then
#: only for a seat named in `BLOCKING_SEATS`.
CATEGORY_LOST = "lost"

#: The desk HOLDS a good answer for this kind of evidence and knows a newer
#: one exists (a newer material wire, a new Form 4 accession, a regime or
#: print change) that this tick did not fetch. That is neither absence nor a
#: lost answer, and classifying it as lost contradicted the categorical line
#: this whole module rests on. Split out 2026-09-18. It is deliberately NOT
#: in `INTEGRITY_CLEAN_STATUSES`, so it still feeds the `data_degraded`
#: advisory, and the freshness disclosure names it in plain words.
CATEGORY_EXPIRED = "expired"

#: WHICH CATEGORIES THE SEAT-HEAL PATH IS ALLOWED TO TRY TO REFRESH.
#:
#: THIS IS NOT A TRADING CONSEQUENCE AND MUST NEVER BECOME ONE. Nothing in
#: this module reads it: not `evaluate`, not `skip`, not `counts_as_degraded`,
#: not the freshness disclosure. It answers exactly one question, asked by
#: `TradingPipeline._heal_lost_research_seats` — "is it worth re-asking this
#: seat?" — and the answer changes only whether the desk spends money to go
#: and look again, never what the desk does with what it already has.
#:
#: Why it exists, 2026-09-23. PR #511 (2026-09-18 10:11 ET) taught the
#: intraday news carry-forward to hand the wire text it had already fetched
#: to the heal path, so an `expired` news seat could be re-asked with data
#: the tick had already paid for. PR #535 (2026-09-18 17:24 ET, seven hours
#: later) split `expired` out of `CATEGORY_LOST` — correctly, because the
#: desk HOLDS an answer and holding an older answer is not the same as
#: having none. But the heal dispatcher selected its work by testing for
#: `CATEGORY_LOST`, so the split silently orphaned the refresh that had
#: shipped that morning. It stayed dead until 2026-09-23, and the DURABLE
#: RECORD is what shows that, not a log grep — the heal's success path writes
#: no matchable log string, so "zero hits" would have proved nothing.
#: `specialist_evidence` holds 22 `seat_heal` rows, ALL dated 2026-09-18, the
#: last at 19:46 UTC, and #535 merged at 21:24 UTC that same day. Meanwhile
#: the owner received 15 `news=expired` alerts: 11 on 2026-09-21 and 4 on
#: 2026-09-22. [Measured 2026-09-23 — sqlite over a read-only copy of the
#: production DB, and a date-bucketed scan of the retained production log.]
#:
#: The lesson is the coupling was implicit. A category membership test in
#: one module decided whether a paid refresh in another module ever ran, and
#: nothing named the dependency, so re-categorising was invisibly a
#: behaviour change. It is named here now, and `tests/test_seat_heal_wiring.py`
#: fails if any category that can carry a heal input drops out of this set.
HEALABLE_CATEGORIES: frozenset[str] = frozenset({CATEGORY_LOST, CATEGORY_EXPIRED})

#: WHICH SEATS MAY REFUSE THE WHOLE DECISION.
#:
#: MANDATE DECISION. Owner, 2026-09-18, made with the argument against it in
#: front of him (a second blocking seat for earnings was proposed and
#: declined): **"Only technical analysis can stop the desk."**
#:
#: This is a risk mandate, not an engineering default. It is NOT an agent's
#: to widen, narrow, or "make safer" — adding a seat here changes what the
#: desk refuses to trade on and needs the owner's ruling, in the same way
#: the coverage threshold does. A seat that is not listed here is ADVISORY:
#: its loss is recorded, reported to the owner and counted as degraded, but
#: it does not halt trading by itself.
#:
#: Declaring it also closes the accident it replaces. Until this constant
#: existed the blocking set was "whichever seat happens to write a word that
#: classifies as LOST", so any seat gaining a new failure word silently
#: gained the power to stop the desk.
BLOCKING_SEATS: frozenset[str] = frozenset({"tech"})

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
    # News (board item 152): the seat answered and the answer is usable, but
    # one top-level field came back in a word the desk cannot read and was
    # dropped so the rest of the report survives. REPORTED, not LOST — the
    # seat DID answer. Its own word rather than `partial` (a coverage fact)
    # or `low_confidence` (the model's self-report): this is a confirmed,
    # named loss inside an otherwise-good answer, and the dropped field reads
    # ABSENT everywhere, never as a neutral verdict.
    "field_unreadable": CATEGORY_REPORTED,
    "degraded": CATEGORY_REPORTED,
    "figures_contradicted": CATEGORY_REPORTED,
    # Smart money: the market-wide Form 4 pass read ZERO filings while
    # unread ones were outstanding. REPORTED rather than LOST because the
    # seat DID answer — the watched-name drain ran and the answer covers the
    # desk's own book — but the answer cannot speak for any insider outside
    # it. Not in INTEGRITY_CLEAN_STATUSES below, so it feeds the degraded
    # advisory and pages. Its own word rather than `partial` because
    # `partial` is what this seat says on an ordinary day.
    "market_wide_blind": CATEGORY_REPORTED,
    "carried_from_morning": CATEGORY_REPORTED,
    # Cross-day remember of a GOOD payload whose kind has not expired
    # (macro regime, earnings write-up, Form 4). Same-session reuse stays
    # `carried_from_morning` (PR #430). Holding-discipline must not treat
    # `remembered` as proof about *today* — that checker has its own allow
    # list. The evidence gate's question is "do we have an answer?", and
    # we do.
    "remembered": CATEGORY_REPORTED,
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
    # We have a GOOD remembered answer and chose not to pay again because
    # the kind has not expired. Honesty label, not a lost seat, not stale.
    "chose_not_to_refetch": CATEGORY_NOTHING_TO_REPORT,
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
    # --- a good answer the desk knows has been superseded ---
    # Kind expired (newer wire, new filing, regime/print change) and this
    # tick did not replace it. The desk HAS an answer; it is simply not the
    # newest one. See CATEGORY_EXPIRED.
    "expired": CATEGORY_EXPIRED,
}

#: HOW MUCH OF THE EVIDENCE BEHIND THIS DECISION WAS READ ON THIS TICK.
#:
#: Separate question from "did an answer arrive?", and nothing in this
#: codebase asked it before 2026-09-18. It exists because the seats that
#: report green on a carried-forward book (`carried_from_morning`,
#: `remembered`, `chose_not_to_refetch`, `not_run_intraday`) are
#: indistinguishable, at the point of decision, from seats that were just
#: read — so a decision resting on one fresh seat and four carried ones
#: looked exactly like a decision resting on five fresh ones.
#:
#: This map CLASSIFIES; it does not judge. There is no minimum fresh count
#: here and none may be added — that number is the owner's.
FRESHNESS_FRESH = "fresh"
FRESHNESS_CARRIED = "carried"
FRESHNESS_ABSENT = "absent"
FRESHNESS_UNKNOWN = "unknown"

STATUS_FRESHNESS: dict[str, str] = {
    # --- asked on this tick AND came back with something usable ---
    "ok": FRESHNESS_FRESH,
    "partial": FRESHNESS_FRESH,
    "low_confidence": FRESHNESS_FRESH,
    "symbol_dropped": FRESHNESS_FRESH,
    # Asked on this tick and answered; one field of that answer was
    # unreadable and dropped. A coverage/quality fact, not a freshness one.
    "field_unreadable": FRESHNESS_FRESH,
    "degraded": FRESHNESS_FRESH,
    "figures_contradicted": FRESHNESS_FRESH,
    # Asked on this tick and answered; what it could see was narrower than
    # usual, which is a coverage fact, not a freshness one.
    "market_wide_blind": FRESHNESS_FRESH,
    # A seat that was asked and honestly answered "nothing" was still READ.
    "empty": FRESHNESS_FRESH,
    "release_overdue": FRESHNESS_FRESH,
    # --- an answer the desk holds, but not one it read on this tick ---
    # `not_run_intraday` sits here because "not asked" is not "read": this
    # tick chose not to re-pay the seat and whatever it holds from earlier
    # is what the decision is standing on. Deliberately not counted fresh —
    # overstating freshness is the exact failure this disclosure exists to
    # prevent.
    "carried_from_morning": FRESHNESS_CARRIED,
    "remembered": FRESHNESS_CARRIED,
    "chose_not_to_refetch": FRESHNESS_CARRIED,
    "not_run_intraday": FRESHNESS_CARRIED,
    # Carried AND known to be out of date. Reported inside `carried`, and
    # named separately as well, because it is the sharpest case.
    "expired": FRESHNESS_CARRIED,
    # --- no usable evidence from this seat at all ---
    "failed": FRESHNESS_ABSENT,
    "parse_error": FRESHNESS_ABSENT,
    "provider_error": FRESHNESS_ABSENT,
    "truncated": FRESHNESS_ABSENT,
    "content_missing": FRESHNESS_ABSENT,
    "carry_forward_empty": FRESHNESS_ABSENT,
    "carry_forward_failed": FRESHNESS_ABSENT,
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
    "remembered",
    "chose_not_to_refetch",
})


#: DEGRADED-AND-DISCLOSED vs DEGRADED-AND-WORTH-A-RED-PAGE.
#:
#: These are two questions and until 2026-09-23 one predicate answered
#: both. `counts_as_degraded` is a DISCLOSURE test — "was this session's
#: evidence less than clean?" — and three consumers use it for exactly
#: that: Risk's ">= 2 sources degraded" advisory, the "degraded:" line in
#: the session report, and the postmortem log line. A fourth consumer,
#: `notifier.maybe_alert_data_quality`, used the same answer to decide
#: whether to send the owner a SEPARATE standalone alert saying the
#: session "ran on incomplete research". That is a different question and
#: it has a different right answer for `expired`.
#:
#: `expired` means the desk HOLDS a good answer and knows a newer one
#: exists (see CATEGORY_EXPIRED). It is not absence, and this module's
#: whole categorical line — "a seat that had NOTHING TO SAY is not the
#: same as a seat whose ANSWER WAS LOST" — puts it on the holding side.
#: The alert text tells the owner his Portfolio Manager and Risk Manager
#: "may have sized or decided this session on incomplete or unreadable
#: input". For a carried-over news read that sentence is not true: the
#: input was readable and complete, it was simply read earlier in the
#: session.
#:
#: MEASURED, 2026-09-23, from the retained production log: 18 data-quality
#: alerts survive in the log and its five rotations; 16 of them name
#: `news=expired` (15 alone, 1 alongside `tech=partial`), and the other
#: two are `macro=partial` and `macro=release_overdue`. Every one of the
#: `expired` ticks was an `intra_check`, and every one of those ticks
#: ALSO sent the owner, in its own session report in the same second, the
#: freshness line "carried over from earlier, not re-read: the news
#: research" — which `describe_evidence_freshness` produces from
#: STATUS_FRESHNESS above and which this change does not touch. So the
#: red page was a second push duplicating a line the owner already had,
#: for the desk's designed intraday carry-forward behaviour.
#:
#: WHAT THIS IS NOT. It is not a reclassification: `expired` stays out of
#: INTEGRITY_CLEAN_STATUSES, stays degraded, stays in the Risk advisory,
#: stays in the report's "degraded:" line and stays named in the freshness
#: disclosure. Nothing about the session gets quieter except the separate
#: red push.
#:
#: MEMBERSHIP RULE, AND THE ONE THING THAT MUST NEVER HAPPEN HERE: a
#: status may only sit in this set if STATUS_CATEGORY maps it to
#: CATEGORY_EXPIRED. Every CATEGORY_LOST word — `failed`, `parse_error`,
#: `provider_error`, `truncated`, `content_missing`, `carry_forward_empty`,
#: `carry_forward_failed` — is a seat whose answer never arrived, is
#: exactly the hazard this alert exists for, and must keep paging.
#: `tests/test_expired_is_not_a_page.py` fails if any non-EXPIRED status
#: is ever added here.
DISCLOSE_ONLY_STATUSES: frozenset[str] = frozenset({"expired"})


def warrants_data_quality_page(status: str) -> bool:
    """True when this seat-status deserves its OWN red owner alert.

    Strictly narrower than `counts_as_degraded`: everything that pages is
    degraded, but a degraded seat the desk still HOLDS an answer for is
    disclosed in the session report rather than pushed separately. Never
    raises, and an unknown word pages — same fail-loud default as
    `counts_as_degraded`, for the same reason.
    """
    return counts_as_degraded(status) and str(status) not in DISCLOSE_ONLY_STATUSES


def page_worthy_statuses(data_status: dict | None) -> dict:
    """The `{seat: status}` subset that may raise a standalone red alert.

    Returned rather than a bare bool so the caller hands the alert the
    seats it should NAME, and so a page raised by a genuinely lost seat
    cannot be padded with seats that were never worth a page. Seat-level
    exemptions (tech's per-symbol `low_confidence`) are NOT duplicated
    here — they stay in the notifier, which applies them to whatever this
    returns. Never raises.
    """
    if not isinstance(data_status, dict):
        return {}
    return {
        seat: value for seat, value in data_status.items()
        if warrants_data_quality_page(value)
    }


def data_quality_page_input(result):
    """The session result as the standalone data-quality alert should see it.

    The alert's own code is unchanged and still decides, from the
    `data_status` it is handed, whether to fire and which seats to name.
    This narrows what it is handed to the seats that warrant a page at
    all, so the decision lives here — beside the categories — rather than
    being a second copy of them inside the notifier.

    Returns the SAME object when nothing is filtered out, so the ordinary
    clean-session path allocates nothing and the alert sees byte-identical
    input to before. Never raises: an alerting refinement must not be able
    to break the session it reports on.
    """
    try:
        if not isinstance(result, dict):
            return result
        current = result.get("data_status")
        pageable = page_worthy_statuses(current)
        if pageable == (current if isinstance(current, dict) else {}):
            return result
        return {**result, "data_status": pageable}
    except Exception:  # noqa: BLE001
        logger.exception("data-quality page filter failed; alerting unfiltered")
        return result


def counts_as_degraded(status: str) -> bool:
    """True when this seat-status should feed the 2+ data_degraded advisory.

    DISCLOSURE test, not a paging test. "Should the owner and the Risk
    Manager be told this session's evidence was not clean?" — not "should
    this interrupt him with its own red message?". That second question is
    `warrants_data_quality_page` above, and it is strictly narrower.

    Never raises. An unknown word counts as degraded so a new failure
    mode cannot silently drop out of the advisory; classify it in
    STATUS_CATEGORY *and* decide whether it belongs in
    INTEGRITY_CLEAN_STATUSES in the same diff.
    """
    return str(status) not in INTEGRITY_CLEAN_STATUSES


@dataclass(frozen=True)
class EvidenceFreshness:
    """How much of this decision's evidence was read on THIS tick.

    Disclosure only. It carries no verdict, refuses nothing, and holds no
    threshold — see `STATUS_FRESHNESS`.
    """

    fresh: list[str] = field(default_factory=list)
    carried: list[str] = field(default_factory=list)
    absent: list[str] = field(default_factory=list)
    unknown: list[str] = field(default_factory=list)
    #: The subset of `carried` the desk KNOWS is superseded (`expired`).
    known_out_of_date: list[str] = field(default_factory=list)
    data_status: dict[str, str] = field(default_factory=dict)

    @property
    def seats(self) -> int:
        return len(self.data_status)

    @property
    def summary(self) -> str:
        """Machine-side one-liner for the durable record and the log."""
        parts = [
            f"{len(self.fresh)} of {self.seats} research seat(s) were read on "
            f"this tick ({', '.join(self.fresh) or 'none'})",
            f"carried from earlier without being re-read: "
            f"{', '.join(self.carried) or 'none'}",
            f"no answer at all: {', '.join(self.absent) or 'none'}",
        ]
        if self.known_out_of_date:
            parts.append(
                "carried answers the desk knows are superseded: "
                + ", ".join(self.known_out_of_date)
            )
        if self.unknown:
            parts.append(
                "seats whose state this desk cannot classify (NOT counted as "
                "read): " + ", ".join(self.unknown)
            )
        return "; ".join(parts)

    def to_evidence(self) -> dict:
        return {
            "fresh_seats": list(self.fresh),
            "carried_seats": list(self.carried),
            "absent_seats": list(self.absent),
            "unknown_freshness_seats": list(self.unknown),
            "known_out_of_date_seats": list(self.known_out_of_date),
            "seats_total": self.seats,
            "seats_read_this_tick": len(self.fresh),
            "summary": self.summary,
        }


def freshness(data_status: dict | None) -> EvidenceFreshness:
    """Classify each seat by whether its answer was read on THIS tick.

    NEVER raises, and never counts an unrecognised state as fresh: an
    unknown word means the desk does not know how fresh that seat is, and
    claiming freshness it cannot prove is the failure this exists to stop.
    """
    if not isinstance(data_status, dict):
        return EvidenceFreshness()
    fresh: list[str] = []
    carried: list[str] = []
    absent: list[str] = []
    unknown: list[str] = []
    stale: list[str] = []
    clean: dict[str, str] = {}
    for seat, value in data_status.items():
        seat_name = str(seat)
        text = str(value)
        clean[seat_name] = text
        bucket = STATUS_FRESHNESS.get(text)
        if bucket == FRESHNESS_FRESH:
            fresh.append(seat_name)
        elif bucket == FRESHNESS_CARRIED:
            carried.append(seat_name)
            if STATUS_CATEGORY.get(text) == CATEGORY_EXPIRED:
                stale.append(seat_name)
        elif bucket == FRESHNESS_ABSENT:
            absent.append(seat_name)
        else:
            unknown.append(seat_name)
            logger.error(
                "evidence freshness: data_status[%r]=%r is not in "
                "STATUS_FRESHNESS — reporting it as unknown freshness, NOT "
                "as read-this-tick. Classify it in src/evidence_gate.py.",
                seat_name, text,
            )
    return EvidenceFreshness(
        fresh=sorted(fresh), carried=sorted(carried), absent=sorted(absent),
        unknown=sorted(unknown), known_out_of_date=sorted(stale),
        data_status=clean,
    )


@dataclass(frozen=True)
class EvidenceGateVerdict:
    """Why the decision may or may not proceed. Machine-readable and durable
    — `to_evidence()` is what gets persisted, per symbol and per run."""

    lost: list[str] = field(default_factory=list)
    nothing_to_report: list[str] = field(default_factory=list)
    reported: list[str] = field(default_factory=list)
    expired: list[str] = field(default_factory=list)
    unclassified: list[str] = field(default_factory=list)
    data_status: dict[str, str] = field(default_factory=dict)
    freshness: EvidenceFreshness = field(default_factory=EvidenceFreshness)

    @property
    def blocking_lost(self) -> list[str]:
        """Lost seats that are ALLOWED to stop the desk (`BLOCKING_SEATS`)."""
        return [seat for seat in self.lost if seat in BLOCKING_SEATS]

    @property
    def advisory_lost(self) -> list[str]:
        """Lost seats that are recorded and reported but do not halt trading."""
        return [seat for seat in self.lost if seat not in BLOCKING_SEATS]

    @property
    def skip(self) -> bool:
        """True when a BLOCKING seat's answer was lost outright.

        Owner mandate 2026-09-18, "Only technical analysis can stop the
        desk" — see `BLOCKING_SEATS`. A lost advisory seat still appears in
        `lost`, in the durable record and in the owner's message.
        """
        return bool(self.blocking_lost)

    @property
    def reason(self) -> str:
        blocking = self.blocking_lost
        if not blocking:
            if self.lost:
                detail = ", ".join(
                    f"{seat}={self.data_status.get(seat)}" for seat in self.lost
                )
                return (
                    f"decision proceeded: {len(self.lost)} advisory seat(s) "
                    f"were asked and their answer never arrived — {detail}. "
                    f"Owner mandate 2026-09-18: only the technical seat can "
                    f"stop the desk. {self.freshness.summary}."
                )
            return f"every seat reported or was honestly empty. {self.freshness.summary}."
        detail = ", ".join(
            f"{seat}={self.data_status.get(seat)}" for seat in blocking
        )
        return (
            f"decision skipped: {len(blocking)} blocking seat(s) were asked "
            f"and their answer never arrived — {detail}. A decision resting "
            f"on an answer the desk never received is not a degraded "
            f"decision, it is a fabricated one (docs/WORK.md item 20)."
        )

    def to_evidence(self) -> dict:
        evidence = {
            "gate": "evidence_coverage",
            "outcome": "skip" if self.skip else "proceed",
            "lost_seats": list(self.lost),
            "blocking_lost_seats": list(self.blocking_lost),
            "advisory_lost_seats": list(self.advisory_lost),
            "blocking_seats": sorted(BLOCKING_SEATS),
            "nothing_to_report_seats": list(self.nothing_to_report),
            "reported_seats": list(self.reported),
            "expired_seats": list(self.expired),
            "unclassified_seats": list(self.unclassified),
            "data_status": dict(self.data_status),
            "reason": self.reason,
        }
        evidence.update(self.freshness.to_evidence())
        return evidence


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
    expired: list[str] = []
    unclassified: list[str] = []
    clean: dict[str, str] = {}

    for seat, value in data_status.items():
        seat_name = str(seat)
        text = str(value)
        clean[seat_name] = text
        category = STATUS_CATEGORY.get(text)
        if category == CATEGORY_LOST:
            lost.append(seat_name)
        elif category == CATEGORY_EXPIRED:
            expired.append(seat_name)
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
        expired=sorted(expired),
        unclassified=sorted(unclassified),
        data_status=clean,
        freshness=freshness(data_status),
    )
