"""A hard trigger is SPENT once the desk has acted on it — board item 74.

The defect
----------
`pipeline._midday_execute_llm_actions` gates every sell-side action on
"does the reason name A trigger", never on "is this the SAME trigger,
resting on the SAME recorded thing, that already cut this name today".
The file said so itself, in the comment that used to sit where this
module's call now lands:

    RESIDUAL GAP, deliberately not closed here: the old gate exempted
    hard triggers, and so does this one. A symbol trimmed at midday on
    "bearish earnings" can be trimmed again at close on the SAME
    "bearish earnings" — one event, two cuts. Closing it needs per-event
    dedup (has THIS trigger already been acted on for this symbol
    today?).

`config/prompts/position_reviewer.md` did not merely fail to forbid the
repeat, it *authorised* it: "You may override and REDUCE/SELL again ONLY
when one of these HARD triggers fires — ... Bearish earnings filing
analysis posted today". The filing that drove the midday cut is still
posted today at the close, so the permission was self-satisfying.

**Frequency, measured.** Zero — with a denominator worth stating. Every
executed sell-side row in both live databases
(`/home/qamc/quant-agent/data/quant_agent.db`, 2026-09-02 → 2026-09-25,
and its pre-reset predecessor, 2026-08-14 → 2026-09-02) was read
read-only on 2026-09-26: eleven sell-side rows in total and not one
symbol-day carrying two of them. But only THREE of those carried a named
`exit_trigger` at all — the field is newer than most of the history —
and one of those three executed. So "the general double-cut shape has
not occurred" is measured; "this mechanism's own behaviour is safe" is
NOT measured, in either direction. Nothing is known either way about how
often a second cut would cite the same record, and this docstring is not
entitled to imply otherwise.

The 2026-05-04 AMZN double cut the board cites predates both databases
and cannot be checked against either; the repo does not even agree with
itself about its reason (`QAMC_REMEDIATION_SPEC.md` records "concentration
drift; valuation stretched", the executor comment records `TARGET_BREACH`).
It is repo hearsay, recorded here as unverifiable rather than asserted as
one wording. If it happened as described it is the SOFT-signal shape the
2026-08-27 phrase gate already closed, which is a different route from
this one.

Where the line is drawn, and why it carries no number
-----------------------------------------------------
The board's own framing: *is a hard trigger spent once acted on, and if
not, what separates a worse reading from the same reading used twice?*
PR #654 answered "spent per symbol per day" and was closed for it: that
also blocks a genuinely NEW same-day event, which is a protective exit
the desk must still be able to take.

The line here is **the recorded thing the seat itself cites**, not the
magnitude of anything:

* Cut #1 executes. The desk durably records what authorised it — the
  named `exit_trigger` and the `trigger_evidence` naming the dated
  news / earnings / macro row it rests on.
* Cut #2 on the same name, same ET day, is asked one question: does it
  rest on the SAME recorded thing? Same trigger AND the same evidence
  (after case/whitespace/punctuation normalisation) means the desk has
  already acted on this; the trigger is spent and the cut is refused.
* A second cut that names a DIFFERENT recorded thing is new information
  by the seat's own testimony and executes normally. That is the
  "genuinely worse reading" half, and it is preserved deliberately.
* A second cut on the same trigger that cites nothing nameable beyond
  the trigger's own phrase is NOT spent — it is `unidentifiable`, which
  is recorded and allowed through. See "Why empty evidence is not spent".

Every branch is an identity test over text the desk already stores. No
cooldown period, no similarity score, no count, no window — a threshold
of any kind here would be exactly the invented number the desk forbids,
and there is no published or measured basis for one.

**What spends a trigger: a cut that actually reduced the position.** Not
a submission. The acted record carries the broker order id and is
believed only when today's trade row for that order passes
`TradingPipeline._trade_executed_or_pending` — the SAME contract the
sibling gate `_symbols_already_trimmed_today` uses, deliberately reused
rather than re-decided here, so the two gates cannot hold opposite views
of what a real fill is. A rejected, cancelled or expired zero-fill cut
therefore spends nothing and the name is fair game again, which is that
sibling's stated posture in its own words. This matters more than it
looks: the reviewer's sell is a limit half a percent under a possibly
stale mark, which this codebase's own de-lever docstring already names as
the wrong mechanism on a gapping name — so the non-fill is most likely on
exactly the bad-news gap-down day this feature governs. An acted record
whose order cannot be matched to an executed row is dropped, not
believed; uncertainty here must never cost a protective exit.

**Why empty evidence is not spent.** A second cut naming the same trigger
with no citable evidence is `unidentifiable`, not `spent`: it is recorded
and it goes through. An earlier draft blocked it, and that put this layer
in direct contradiction with the layer immediately upstream on the same
path — `exit_trigger.check_exit_trigger`, which writes `dropped=False` for
every unsubstantiated exit and states why: being stranded in a losing
position is strictly worse than an uncheckable claim passing. Two layers
on one path cannot disagree about the identical input. It was also
perverse in practice, because the seat could escape by answering
`cannot_substantiate`, which the prompt blesses as never penalised — so
only the seat that named the TRUE trigger tersely was punished. The
honest reading is the plain one: with no record named, this layer cannot
show the cut rests on the record already acted on, so it has no completed
"no" to give.

**The limit of the identity test, stated plainly.** This enforcement is
only as strong as the seat's own consistency in citing a record. Against
real production strings the same event was cited two different ways
inside a single run, and appending "(confirmed)" to a citation is enough
to read as a different record. So the block is evadable — by accident as
easily as on purpose — and the escape hatch is available in exactly the
same proportion, which is the honest way round: a seat that genuinely
found a new record is never trapped. Closing that gap would cost a
similarity threshold, i.e. an invented number, and that trade is refused.
What is bought instead is that the repeat is no longer AUTHORISED, the
spent records are shown to the seat verbatim, and every second cut is
durably recorded either way so the evening grade can see it.

The seat is not asked to guess what is spent: `position_reviewer` is
shown each spent trigger and its evidence verbatim, and told to cite a
different record or HOLD. Rewording a spent citation to slip past the
identity test is therefore an evasion of a stated instruction, and it
lands as a durable `trigger_superseded_by_new_evidence` row the evening
grade reads — visible, not silent. Softening the identity test into a
fuzzy match to catch it would cost a similarity threshold, which is the
worse trade.

What is NOT spendable
---------------------
`STOP_FIRED` — a stop firing is a price fact, not a reading of an event.
A stop that fires again fires on a new price, and blocking a protective
exit is the harm on the other side of this whole item.

`CANNOT_SUBSTANTIATE` — not a trigger at all; it is the seat's honest
declination (`exit_trigger.ExitTrigger`'s docstring).

Everything else is spendable, including `THESIS_INVALID`: a named
`thesis_invalid_if` condition occurs once, and a second cut reciting the
same satisfied condition is precisely the one-event-two-cuts shape.

Failure posture: OPEN, matching `exit_refusal.UNCERTAINTY_FAIL`. If the
acted-trigger record cannot be read, or an acted row cannot be matched to
an executed trade, this layer produces no judgment and the exit proceeds — stranding the desk in a losing position is worse
than an uncheckable repeat passing (owner-ratified 2026-08-27).
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any, Literal

from src.risk.exit_trigger import (
    ExitTrigger,
    _evidence_is_substantiation,
    normalize_trigger,
)

logger = logging.getLogger(__name__)

__all__ = [
    "ACTED_TRIGGER_KIND",
    "CODE_TRIGGER_ALREADY_SPENT",
    "CODE_TRIGGER_SUPERSEDED",
    "CODE_TRIGGER_UNIDENTIFIABLE",
    "NON_SPENDABLE_TRIGGERS",
    "SPENT_LAYER",
    "ActedTrigger",
    "SpentTriggerCheck",
    "evidence_fingerprint",
    "keep_executed_acted_triggers",
    "is_spendable",
    "spent_trigger_check",
    "acted_trigger_payload",
    "parse_acted_triggers",
    "format_spent_triggers_block",
]

#: `specialist_evidence.kind` for the row written when a sell-side action
#: is actually submitted. Append-only; this module is its only reader.
ACTED_TRIGGER_KIND = "exit_trigger_acted"

#: `exit_refusal` layer name, so a refusal from here is attributable.
SPENT_LAYER = "spent_trigger"

#: Completed refusal (dropped=True): the desk already acted on this
#: trigger, resting on this record, for this symbol today.
CODE_TRIGGER_ALREADY_SPENT = "trigger_already_spent"

#: NOT a refusal (dropped=False). A second same-trigger cut that names a
#: different record executes, and says so durably, so the evening grade
#: can audit whether the "new" record was genuinely new.
CODE_TRIGGER_SUPERSEDED = "trigger_superseded_by_new_evidence"

#: NOT a refusal (dropped=False). A second same-trigger cut that cites no
#: record at all: this layer cannot show it is the same one, and the
#: upstream substantiation layer already owns that input and lets it pass.
CODE_TRIGGER_UNIDENTIFIABLE = "trigger_record_unidentifiable"

#: See the module docstring. A stop firing is a price fact; declining to
#: substantiate is not a trigger. Everything else is spendable.
NON_SPENDABLE_TRIGGERS: frozenset[ExitTrigger] = frozenset({
    ExitTrigger.STOP_FIRED,
    ExitTrigger.CANNOT_SUBSTANTIATE,
})

_PUNCT = re.compile(r"[^0-9a-z]+")


def is_spendable(trigger: object) -> bool:
    """True when acting on this trigger uses it up for the ET day."""
    t = normalize_trigger(trigger)
    return t is not None and t not in NON_SPENDABLE_TRIGGERS


def evidence_fingerprint(evidence: object, trigger: object = None) -> str:
    """Identity of the recorded thing cited, or `""` when nothing is.

    Normalisation only — case, punctuation and whitespace are collapsed so
    that "Active News State Change 2026-09-16, COP(bearish)" and "active
    news state change 2026-09-16 COP bearish" are one record rather than
    two. It is an EXACT match after normalising, never a similarity score:
    there is no threshold in here and deliberately so.

    Returns `""` when the evidence says nothing beyond the trigger's own
    phrase, reusing `exit_trigger._evidence_is_substantiation` so the two
    layers cannot drift apart on what counts as evidence.
    """
    t = normalize_trigger(trigger)
    if not _evidence_is_substantiation(evidence, None, t):
        return ""
    return _PUNCT.sub(" ", str(evidence).lower()).strip()


@dataclass(frozen=True)
class ActedTrigger:
    """One sell-side action the desk actually submitted today."""

    symbol: str
    trigger: str
    evidence: str
    fingerprint: str
    action: str = ""
    run_id: str = ""
    #: The broker order this cut was submitted as. A trigger is spent only
    #: once this order is known to have executed — see the module
    #: docstring. Empty means unverifiable, which is never treated as spent.
    broker_order_id: str = ""

    def to_json(self) -> str:
        return json.dumps({
            "symbol": self.symbol, "trigger": self.trigger,
            "evidence": self.evidence, "fingerprint": self.fingerprint,
            "action": self.action, "run_id": self.run_id,
            "broker_order_id": self.broker_order_id,
        })


@dataclass(frozen=True)
class SpentTriggerCheck:
    """Three-valued, matching the rest of the exit path.

    ``spent``           — refuse and record (`dropped=True`).
    ``new_evidence``    — execute, and record that it was a second cut on
                          a different record (`dropped=False`).
    ``unidentifiable``  — a second cut on the same trigger that names no
                          record. Execute and record (`dropped=False`);
                          this layer cannot show it is the same one, and
                          the upstream substantiation layer already owns
                          that input. See the module docstring.
    ``not_applicable``  — nothing to say: not an exit, not a spendable
                          trigger, or no prior cut on this trigger today.
    ``uncertain``       — the record could not be read. Fails OPEN.
    """

    verdict: Literal["spent", "new_evidence", "unidentifiable",
                     "not_applicable", "uncertain"]
    detail: str = ""
    prior: ActedTrigger | None = None
    code: str = ""

    @property
    def blocks(self) -> bool:
        return self.verdict == "spent"


def acted_trigger_payload(*, symbol: str, trigger: object, evidence: object,
                          action: str, run_id: str,
                          broker_order_id: str = "") -> ActedTrigger | None:
    """The record to persist after a sell-side order is submitted.

    None when the action carried no spendable trigger — there is then
    nothing that can be spent, and writing a row would invite a later
    reader to treat an unnamed trigger as one.
    """
    t = normalize_trigger(trigger)
    if t is None or t in NON_SPENDABLE_TRIGGERS:
        return None
    text = str(evidence or "")
    return ActedTrigger(
        symbol=(symbol or "").upper(), trigger=t.value, evidence=text[:500],
        fingerprint=evidence_fingerprint(text, t), action=action or "",
        run_id=run_id or "", broker_order_id=str(broker_order_id or ""),
    )


def parse_acted_triggers(rows: Any) -> list[ActedTrigger]:
    """Rehydrate persisted rows; a malformed row is skipped, not fatal."""
    out: list[ActedTrigger] = []
    for raw in rows or []:
        try:
            d = raw if isinstance(raw, dict) else json.loads(raw)
            out.append(ActedTrigger(
                symbol=str(d.get("symbol") or "").upper(),
                trigger=str(d.get("trigger") or ""),
                evidence=str(d.get("evidence") or ""),
                fingerprint=str(d.get("fingerprint") or ""),
                action=str(d.get("action") or ""),
                run_id=str(d.get("run_id") or ""),
                broker_order_id=str(d.get("broker_order_id") or ""),
            ))
        except Exception:  # noqa: BLE001 — one bad row is not a judgment
            continue
    return out


def keep_executed_acted_triggers(
    acted: list[ActedTrigger] | None,
    *,
    executed_order_ids: set[str] | None,
) -> list[ActedTrigger] | None:
    """Believe only the cuts that actually reduced the position.

    The acted record is written when the order is SUBMITTED, because that
    is the only moment the trigger and its evidence are in hand. A
    submission is not a sale: a limit half a percent under a stale mark on
    a gapping name is rejected, cancelled or left unfilled precisely on the
    bad-news day this layer governs, and a trigger spent by a cut that sold
    nothing would refuse the close cut on a name the desk still holds.

    `executed_order_ids` is supplied by the caller from the SAME contract
    the sibling same-day-trim gate uses
    (`TradingPipeline._trade_executed_or_pending` over today's trade rows),
    so there is one opinion about what a real fill is, not two. `None`
    means the caller could not find out: that is uncertainty and the whole
    check is stood down (returns None → the layer fails OPEN), never
    silently treated as "nothing is spent" or "everything is".

    A record with no order id, or one whose order is not in the executed
    set, is dropped: unverifiable must never cost a protective exit.
    """
    if acted is None or executed_order_ids is None:
        return None
    return [r for r in acted
            if r.broker_order_id and r.broker_order_id in executed_order_ids]


def spent_trigger_check(*, action: object, symbol: str, trigger: object,
                        evidence: object,
                        acted_today: list[ActedTrigger] | None,
                        ) -> SpentTriggerCheck:
    """Has this trigger, on this record, already cut this name today?

    `acted_today` is every sell-side action already submitted for ANY
    symbol today; `None` means the read failed, which is uncertainty and
    fails OPEN. The caller supplies the whole day's rows and this filters
    by symbol so there is one place where "same name" is defined.
    """
    act = str(action or "").upper().split("(", 1)[0].strip()
    if act not in ("SELL", "REDUCE", "COVER"):
        return SpentTriggerCheck("not_applicable", "not a sell-side action")
    if acted_today is None:
        return SpentTriggerCheck(
            "uncertain",
            "today's acted-trigger record could not be read — this layer "
            "reaches no judgment and the exit proceeds",
        )
    t = normalize_trigger(trigger)
    if t is None:
        # No structured trigger. The named-trigger gate upstream owns that
        # judgment; nothing here can say which trigger would be spent.
        return SpentTriggerCheck("not_applicable", "no structured trigger named")
    if t in NON_SPENDABLE_TRIGGERS:
        return SpentTriggerCheck(
            "not_applicable", f"{t.value} is never spent — see module docstring",
        )
    sym = (symbol or "").upper()
    prior = [r for r in acted_today
             if r.symbol == sym and r.trigger == t.value]
    if not prior:
        return SpentTriggerCheck(
            "not_applicable", f"no earlier cut on {t.value} for {sym} today",
        )
    fp = evidence_fingerprint(evidence, t)
    if not fp:
        earlier = prior[0]
        return SpentTriggerCheck(
            "unidentifiable",
            (f"{sym} was already cut today on {t.value} "
             f"({earlier.action or 'sell-side'}, evidence: "
             f"{earlier.evidence[:160]!r}) and this second cut cites no "
             f"record beyond the trigger's own name — this layer cannot "
             f"show it is the same record, so it is recorded and allowed, "
             f"matching the unsubstantiated-exit posture upstream"),
            prior=earlier, code=CODE_TRIGGER_UNIDENTIFIABLE,
        )
    match = next((r for r in prior if r.fingerprint and r.fingerprint == fp), None)
    if match is not None:
        return SpentTriggerCheck(
            "spent",
            (f"{sym} was already cut today on {t.value} resting on this "
             f"same record ({match.action or 'sell-side'}, evidence: "
             f"{match.evidence[:160]!r}); the trigger is spent"),
            prior=match, code=CODE_TRIGGER_ALREADY_SPENT,
        )
    return SpentTriggerCheck(
        "new_evidence",
        (f"{sym} was already cut today on {t.value}, but this cut names a "
         f"different record ({str(evidence)[:160]!r}) — new information, "
         f"so it is allowed and recorded for the evening grade"),
        prior=prior[0], code=CODE_TRIGGER_SUPERSEDED,
    )


def format_spent_triggers_block(acted: list[ActedTrigger] | None,
                                symbols: set[str] | None = None) -> str:
    """Prompt text naming what is already spent, verbatim.

    The seat must be able to see what it may not re-cite; an invisible
    filter is the shape `position_reviewer.md` already calls out as unfair
    to the seat. Empty string when nothing is spent.
    """
    rows = [r for r in (acted or [])
            if r.trigger and (symbols is None or r.symbol in symbols)]
    if not rows:
        return ""
    lines = []
    for r in sorted(rows, key=lambda x: (x.symbol, x.trigger)):
        ev = r.evidence.strip() or "(no record cited)"
        lines.append(f"  - {r.symbol} · `{r.trigger}` · already acted on: {ev}")
    return (
        "**Triggers already SPENT today (the desk has acted on these):**\n"
        + "\n".join(lines)
        + "\n"
        "A SELL / REDUCE / COVER whose `exit_trigger` is one of the above "
        "for that symbol AND whose `trigger_evidence` is that same record is "
        "REFUSED by the executor and recorded as `trigger_already_spent` — "
        "the position HOLDS, protected by its broker-resident stop. "
        "If the position genuinely got worse, cite the DIFFERENT record that "
        "says so (a later filing, a new headline, a different metric) and the "
        "cut goes through. If there is no new record, HOLD: re-reading the "
        "same one is not new information.\n"
    )
