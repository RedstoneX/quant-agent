"""Structured exit trigger — the named trigger as a field, not as prose.

Why this module exists (2026-09-18).

Every SELL / REDUCE / COVER has to "cite a hard trigger", and until this
landed the whole of that requirement was a case-insensitive SUBSTRING
MATCH over free prose (`pipeline._reason_cites_hard_trigger` against
`pipeline._HARD_TRIGGER_KEYWORDS`). `PositionAction.reason` was a bare
`str`: no enum, no evidence field, and no way at all for the seat to say
"I want out and I cannot substantiate it".

That shape had a measured consequence on the live desk, not a
theoretical one. On 2026-09-16 the two real exits (COP SELL, EQNR
REDUCE, run `midday-d8996a51`) carried, verbatim and in their entirety,
the reason ``"adverse news"``. Two words. They passed the phrase gate
because the phrase is on the list, and they passed
`exit_guard.holding_discipline_claim_check` with verdict "ok" because
that function reads the PROSE for a claim it knows how to check
(`claims_regime_flip` / `claims_bearish_state_change`) and "adverse
news" makes neither — so there was nothing to check and nothing was
checked.

Meanwhile a reason that honestly described a stall ("stalling", "not
progressing") is exactly what `exit_guard.veto_contradicted_exit`
audits, and a reason that names no listed phrase at all is dropped
outright. So the gate was **selecting for bad paperwork**: phrase an
exit as an unverifiable external event and it always got its way;
describe the position's actual trajectory and it got audited or
refused. That selection effect was live.

The fix is at the PRODUCING step. The seat now names its trigger in a
typed field (`PositionAction.exit_trigger`), says what the trigger rests
on (`PositionAction.trigger_evidence`), and has a first-class,
recordable way to decline: `ExitTrigger.CANNOT_SUBSTANTIATE`. With the
claim in a field instead of in a sentence, "which claim is being made"
stops being a regex question, and the existing checker can be pointed at
the right verification for the trigger actually named.

**What this module does NOT do.** It does not make the gate stricter. It
moves no threshold and adds no trade-governing number. A legacy action
that carries only prose is classified by the SAME phrase vocabulary as
before (`derive_trigger_from_reason`), so behaviour on an un-upgraded
reason is unchanged. The call site's own reasoning is respected: being
stranded in a losing position is strictly worse than an uncheckable
claim passing, so an unsubstantiated exit is HEALED and RE-ASKED, and
only refused as a last resort with a durable reason.

**Three-valued, exactly as ratified 2026-09-04.** `false` blocks and
alerts; `unverifiable` logs only; `ok` passes. See
`exit_guard.HoldingDisciplineClaimCheck`'s docstring for why collapsing
the middle value into a boolean is wrong — this module follows it rather
than inventing a second posture.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Literal

__all__ = [
    "ExitTrigger",
    "TRIGGER_PHRASES",
    "CANONICAL_TRIGGER_NAMES",
    "CANONICAL_NAME_NOT_MATCHED_IN_PROSE",
    "canonical_prose_names",
    "EVENT_TRIGGERS",
    "VERIFIED_ON_CHART",
    "NO_VERIFIER_EXISTS",
    "derive_trigger_from_reason",
    "normalize_trigger",
    "ExitTriggerCheck",
    "check_exit_trigger",
    "CODE_UNSUBSTANTIATED_TRIGGER",
    "CODE_UNSUBSTANTIATED_AFTER_REASK",
    "REASK_DIRECTIVE",
]


class ExitTrigger(str, Enum):
    """The named trigger behind a SELL / REDUCE / COVER, as a value.

    One member per group `pipeline._HARD_TRIGGER_KEYWORDS` already
    accepts — deliberately not one member per phrase, because the phrases
    inside a group ("earnings miss" / "earnings missed" / "guidance cut")
    are wordings of the same event and the desk verifies them the same
    way. `test_exit_trigger.py` pins the grouping against that tuple so
    the two vocabularies cannot silently diverge.

    `CANNOT_SUBSTANTIATE` is not a trigger. It is the honest outcome, and
    it is a first-class member precisely so that the seat is never pushed
    into picking a trigger it cannot support. Recording it is a legitimate
    answer; see `check_exit_trigger`.
    """

    THESIS_INVALID = "thesis_invalid"
    BEARISH_STATE_CHANGE = "bearish_state_change"
    ADVERSE_NEWS = "adverse_news"
    SECTOR_SHOCK = "sector_shock"
    EARNINGS = "earnings"
    REGIME_SHIFT = "regime_shift"
    # `RISK_BREAKER` ("daily loss" / "circuit breaker") was REMOVED
    # 2026-09-20 with the account-level loss alarm itself (WORK.md item 32,
    # docs/INCIDENT_HISTORY.md). Nothing computes the event it named.
    STOP_FIRED = "stop_fired"
    TREND_ALIGNMENT_OVER = "trend_alignment_over"
    CANNOT_SUBSTANTIATE = "cannot_substantiate"


#: Trigger -> the prose phrases that name it. Ordered longest-first inside
#: each group only for readability — matching uses substring containment
#: exactly as the phrase gate does.
#:
#: `_LEGACY_TRIGGER_PHRASES` is the vocabulary as it stood before
#: 2026-09-30: the wordings an LLM produces in free prose. The CANONICAL
#: ENUM NAME of each trigger is added to it below by
#: `canonical_prose_names`, so `TRIGGER_PHRASES` (and therefore
#: `pipeline._HARD_TRIGGER_KEYWORDS`) cannot name a different set of
#: triggers from `ExitTrigger` itself.
_LEGACY_TRIGGER_PHRASES: dict[ExitTrigger, tuple[str, ...]] = {
    ExitTrigger.THESIS_INVALID: (
        "thesis_invalid",
        "thesis invalid",
        "invalidation triggered",
        "broken thesis",
        "thesis broken",
    ),
    ExitTrigger.BEARISH_STATE_CHANGE: (
        "high-conviction bearish",
        "high conviction bearish",
        "high bearish",
    ),
    ExitTrigger.ADVERSE_NEWS: ("adverse news", "material news"),
    ExitTrigger.SECTOR_SHOCK: ("sector shock",),
    ExitTrigger.EARNINGS: (
        "bearish earnings",
        "bearish filing",
        "earnings missed",
        "earnings miss",
        "guidance cut",
    ),
    ExitTrigger.REGIME_SHIFT: (
        "regime shift",
        "regime flip",
        "regime flipped",
        "risk-off",
        "risk off",
    ),
    ExitTrigger.STOP_FIRED: ("stop hit", "stopped out"),
    # The ALIGNMENT EXIT (owner ruling 2026-09-30, docs/WORK.md item 205) —
    # the desk's only sanctioned way to realise a GAIN. Substantiated by
    # `src.risk.alignment_exit.check_alignment_exit`, never by a price
    # target.
    ExitTrigger.TREND_ALIGNMENT_OVER: (
        "trend alignment over",
        "alignment exit",
    ),
}

#: THE ONE DIVERGENCE THIS MODULE STILL ALLOWS, AND WHY (2026-09-30).
#:
#: A sanctioned `ExitTrigger` named by its own canonical value is accepted
#: as naming that trigger. Two members are deliberately NOT matched that
#: way in free prose, and the exclusion is a named constant rather than an
#: accident so a test can pin it:
#:
#: * `CANNOT_SUBSTANTIATE` is not a trigger at all. It is the seat's
#:   honest "I cannot support this exit". Accepting it as a named trigger
#:   would turn the one safe way to decline into a way to pass the gate.
#: * `EARNINGS` has the bare word "earnings" as its canonical value, and
#:   that word occurs constantly in prose that names no event at all
#:   ("earnings in three days", "pre-earnings drift"). Substring-matching
#:   it would WIDEN the gate to a non-event, which the long comment above
#:   `pipeline._HARD_TRIGGER_KEYWORDS` forbids. The earnings EVENT is
#:   already named by "earnings miss" / "guidance cut" / "bearish
#:   earnings", and the STRUCTURED field (`PositionAction.exit_trigger`)
#:   carries `earnings` exactly, so nothing is unreachable — only the bare
#:   word in prose is not enough.
CANONICAL_NAME_NOT_MATCHED_IN_PROSE: frozenset[ExitTrigger] = frozenset(
    {
        ExitTrigger.CANNOT_SUBSTANTIATE,
        ExitTrigger.EARNINGS,
    }
)


def canonical_prose_names(trigger: ExitTrigger) -> tuple[str, ...]:
    """The canonical spellings of `trigger` accepted in free prose.

    Empty for a member in `CANONICAL_NAME_NOT_MATCHED_IN_PROSE`. Otherwise
    the enum value as written (`bearish_state_change`) and, when it has
    separators, its spaced form (`bearish state change`) — the same two
    spellings `normalize_trigger` already folds together, and the same
    spaced form `exit_guard.claims_bearish_state_change` already accepts.
    """
    if trigger in CANONICAL_NAME_NOT_MATCHED_IN_PROSE:
        return ()
    value = trigger.value
    if "_" not in value:
        return (value,)
    return (value, value.replace("_", " "))


#: Every canonical name the prose gate accepts, derived from the enum.
CANONICAL_TRIGGER_NAMES: tuple[str, ...] = tuple(
    name for trigger in ExitTrigger for name in canonical_prose_names(trigger)
)


def _phrases_for(trigger: ExitTrigger) -> tuple[str, ...]:
    legacy = _LEGACY_TRIGGER_PHRASES.get(trigger, ())
    return legacy + tuple(name for name in canonical_prose_names(trigger) if name not in legacy)


#: Iterated over `ExitTrigger` rather than over `_LEGACY_TRIGGER_PHRASES`
#: deliberately: a member added to the enum with no prose wording of its own
#: still gets its canonical name here, so it is namable the day it is
#: declared. That is the divergence this whole block exists to prevent.
TRIGGER_PHRASES: dict[ExitTrigger, tuple[str, ...]] = {
    trigger: phrases for trigger in ExitTrigger if (phrases := _phrases_for(trigger))
}

#: Triggers a verifier in this codebase can actually CONTRADICT today.
#:
#: Membership is not an aspiration and not a category: a member belongs here
#: only if some branch of `exit_guard.holding_discipline_claim_check` is
#: reached for it and can append a contradiction. Verified by grep, member by
#: member, 2026-09-30:
#:
#:   BEARISH_STATE_CHANGE - `_claims_bearish`; contradicted by the same-day
#:                          `state_change` rows for the symbol.
#:   ADVERSE_NEWS         - routed into that SAME `_claims_bearish` branch
#:                          deliberately (see its comment there), so the same
#:                          record can contradict it.
#:   REGIME_SHIFT         - `_claims_regime`; contradicted by the day's macro
#:                          regime read when the macro status is trusted.
#:
#: `SECTOR_SHOCK` was REMOVED from this set 2026-09-30. It had never been
#: verifiable: `holding_discipline_claim_check`'s own comment states it is
#: deliberately NOT routed to the state-change check because the desk records
#: no sector-shock row. The set asserted a verifier that the code next to it
#: said did not exist. Removing it changes no behaviour — see
#: `NO_VERIFIER_EXISTS` for why this set had drifted unnoticed.
#: Triggers verified by READING THE CHART rather than by looking up a
#: recorded event. Exactly one member: the alignment exit (owner ruling
#: 2026-09-30). Its verifier is `alignment_exit.check_alignment_exit`,
#: which answers "is this position's move over?" from the instrument's own
#: marks and its own noise band — so the trigger is neither an unverifiable
#: claim nor an external event, and belongs in neither of the two sets that
#: existed before it.
VERIFIED_ON_CHART: frozenset[ExitTrigger] = frozenset(
    {
        ExitTrigger.TREND_ALIGNMENT_OVER,
    }
)

EVENT_TRIGGERS: frozenset[ExitTrigger] = frozenset(
    {
        ExitTrigger.BEARISH_STATE_CHANGE,
        ExitTrigger.ADVERSE_NEWS,
        ExitTrigger.REGIME_SHIFT,
    }
)

#: The other side of the ledger: every `ExitTrigger` for which NO verifier
#: exists, with the reason, one line each.
#:
#: WHY THIS CONSTANT EXISTS (2026-09-30). The comment block above
#: `pipeline._HARD_TRIGGER_KEYWORDS` sets a bar — an accepted trigger must
#: name "something the desk records" and must not be re-added "without a
#: verifier that can answer 'did that happen today?'". That bar was prose,
#: and prose slips: `EVENT_TRIGGERS` was declared and exported here and read
#: by NOTHING (grep of `src/` and `tests/` returned only its own definition
#: and `__all__`), so the set that was supposed to encode which claims are
#: checkable did nothing at all and had gone wrong on `SECTOR_SHOCK` without
#: anything noticing.
#:
#: `tests/test_exit_trigger_canonical_names.py` now FAILS when an
#: `ExitTrigger` member is in neither set, so declaring a new trigger forces
#: whoever declares it to say, in code, whether anything can check it. The
#: bar is now a test instead of a paragraph. This does NOT make the four
#: members below verifiable — it records that they are not, which is the
#: true state, and four of eight is the measured answer, not a target.
#:
#: Being here does not make a trigger illegitimate. It makes the gap visible.
NO_VERIFIER_EXISTS: dict[ExitTrigger, str] = {
    # TREND_ALIGNMENT_OVER is NOT in this dict and NOT in EVENT_TRIGGERS.
    # It is the one trigger with a verifier of its own that answers "is it
    # true right now?" off the instrument rather than off a recorded event:
    # `src.risk.alignment_exit.check_alignment_exit`. See VERIFIED_ON_CHART.
    ExitTrigger.THESIS_INVALID: (
        "consulted, never judged: `check_structural_protection` is read with "
        "`advisory_only=True, persist=False` and its own comment states the "
        "branch cannot change which exits execute."
    ),
    ExitTrigger.SECTOR_SHOCK: (
        "the desk records no sector-scope row of any kind, so there is "
        "nothing a sector-level assertion could be checked against; "
        "`holding_discipline_claim_check` says so where it declines to route "
        "it."
    ),
    ExitTrigger.EARNINGS: (
        "no branch reads an earnings row on the exit path; the claim is "
        "accepted on its wording alone (and, since 2026-09-30, reachable "
        "through the structured field even though the bare word is excluded "
        "from prose)."
    ),
    ExitTrigger.STOP_FIRED: (
        "no branch asks the broker whether a stop actually filled; this is "
        "also the one member whose canonical spelling is a genuinely NEW "
        "wording rather than a re-spelling of an accepted phrase."
    ),
    ExitTrigger.CANNOT_SUBSTANTIATE: (
        "not a trigger at all — the seat's honest decline. There is nothing "
        "to verify, and it is never accepted as naming a trigger."
    ),
}

#: Refusal codes for `src.risk.exit_refusal.record_exit_refusal`.
CODE_UNSUBSTANTIATED_TRIGGER = "unsubstantiated_trigger"
CODE_UNSUBSTANTIATED_AFTER_REASK = "unsubstantiated_after_reask"

#: Appended to the re-ask user message. Names the symbols and says what is
#: missing. It does not tell the seat what to conclude and does not offer
#: it a phrase to recite — that would reproduce the defect being fixed.
REASK_DIRECTIVE = (
    "SUBSTANTIATION RE-ASK. Your previous answer proposed an exit on the "
    "symbol(s) below without substantiating the trigger: either no "
    "`exit_trigger` was named, or `trigger_evidence` was empty, or you "
    "recorded `cannot_substantiate`. Re-answer for these symbols ONLY, "
    "keeping every other action as it was.\n"
    "For each one, either (a) name the `exit_trigger` and fill "
    "`trigger_evidence` with the specific recorded thing it rests on — the "
    "dated news/earnings/macro row, or the metric and its two values — or "
    "(b) keep `cannot_substantiate` and change the action to HOLD. Do NOT "
    "restate the trigger phrase as its own evidence, and do NOT invent a "
    "news item, a date or a number. Recording `cannot_substantiate` with a "
    "HOLD is a correct and expected answer.\n"
    "Symbols needing substantiation: "
)


def normalize_trigger(value: object) -> ExitTrigger | None:
    """Coerce a raw field value to an `ExitTrigger`, or None.

    Tolerant of the case and separator drift an LLM produces
    ("Adverse News", "ADVERSE-NEWS"). An unrecognised value is None,
    which the caller treats as "no trigger named" — identical to the
    field being absent, and never as a recognised trigger.
    """
    if isinstance(value, ExitTrigger):
        return value
    if not isinstance(value, str):
        return None
    key = value.strip().lower().replace("-", "_").replace(" ", "_")
    if not key:
        return None
    for member in ExitTrigger:
        if key == member.value:
            return member
    return None


def derive_trigger_from_reason(reason: object) -> ExitTrigger | None:
    """MECHANICAL heal: read the trigger the prose already names.

    This is step 1 of the desk's heal order (owner 2026-09-16, see
    `src.seat_heal`): fix it in code before paying for a retry. It
    invents nothing — it only reads the SAME phrase vocabulary the
    executor has matched against since 2026-08-27, so an exit whose
    reason names a trigger keeps its trigger when the seat forgot to fill
    the new field.

    Returns None when the prose names no recognised trigger (that is the
    phrase gate's existing completed "no", unchanged) and when it names
    more than one group (ambiguous: the seat must say which, and nothing
    here is entitled to choose for it).
    """
    if not isinstance(reason, str) or not reason:
        return None
    lower = reason.lower()
    hits = [trigger for trigger, phrases in TRIGGER_PHRASES.items() if any(phrase in lower for phrase in phrases)]
    if len(hits) != 1:
        return None
    return hits[0]


def _evidence_is_substantiation(evidence: object, reason: object, trigger: ExitTrigger | None) -> bool:
    """True when `trigger_evidence` says something beyond the phrase itself.

    The failure mode this closes is the 2026-09-16 one restated in the new
    field: `trigger_evidence: "adverse news"` is not evidence of adverse
    news. Evidence that consists only of the trigger's own phrases carries
    no more information than the recital did.

    Not a length test and not a quality judgement — there is no threshold
    here. It strips the trigger's own phrases and asks whether anything
    is left.
    """
    del reason  # the reason is judged as a reason, not as its own evidence
    if not isinstance(evidence, str):
        return False
    text = evidence.strip()
    if not text:
        return False
    residual = text.lower()
    for phrase in TRIGGER_PHRASES.get(trigger, ()) if trigger else ():
        residual = residual.replace(phrase, " ")
    return bool(residual.strip(" \t\r\n.,;:-–—()[]'\"/"))


@dataclass(frozen=True)
class ExitTriggerCheck:
    """Three-valued verdict on a sell-side action's STRUCTURED trigger.

    Same three values and the same consequences as
    `exit_guard.HoldingDisciplineClaimCheck`, deliberately — one posture
    on the exit path, not two:

      "ok"           - a trigger is named and substantiated (or the action
                       is not an exit). Nothing logged, nothing blocked.
      "unverifiable" - an exit is proposed whose trigger is not
                       substantiated: no trigger named after the mechanical
                       heal, or a named trigger with no evidence behind it,
                       or the seat recorded `cannot_substantiate`. LOGGED
                       ONLY. It never blocks by itself — it asks for a
                       RE-ASK (`needs_reask`), because dropping the exit
                       silently is the thing this module exists to stop.
      "false"        - reserved for a claim recorded data affirmatively
                       CONTRADICTS. This module does not reach that verdict
                       on its own; verifying an event trigger against the
                       desk's records is `exit_guard`'s job and the
                       structured trigger is what tells it which record to
                       read. Kept in the type so the two checks compose
                       without a caller having to translate verdicts.

    `trigger` is the trigger in force after the mechanical heal — what a
    downstream verifier should check — and is None when none was named.
    """

    verdict: Literal["ok", "unverifiable", "false"]
    trigger: ExitTrigger | None = None
    finding: str | None = None
    healed: bool = False
    needs_reask: bool = False

    @property
    def blocks(self) -> bool:
        """True only for a PROVEN-FALSE claim. Deliberately not
        `finding is not None`, which is also true for the log-only
        unverifiable case — the same distinction
        `HoldingDisciplineClaimCheck.blocks` draws."""
        return self.verdict == "false"

    @property
    def substantiated(self) -> bool:
        return self.verdict == "ok" and self.trigger is not None


def check_exit_trigger(
    *,
    action: object,
    exit_trigger: object,
    trigger_evidence: object,
    reason: object,
    symbol: str = "",
) -> ExitTriggerCheck:
    """Judge whether a sell-side action substantiates the trigger it names.

    Order of operations is the desk's heal order and nothing else:

    1. Not a SELL/REDUCE/COVER -> "ok". Nothing to substantiate.
    2. `cannot_substantiate` -> "unverifiable", re-ask. The seat took the
       honest option; that is a recordable outcome, never an error, and
       never something to punish by dropping the position's exit.
    3. No usable trigger field -> MECHANICAL HEAL from the prose
       (`derive_trigger_from_reason`). A legacy reason that names a
       trigger keeps it, so this function changes nothing for the ~58
       existing tests and nothing for an un-upgraded seat.
    4. Trigger in force but no evidence behind it -> "unverifiable",
       re-ask.
    5. Otherwise -> "ok".

    No step here refuses an exit. "unverifiable" asks the caller to
    re-ask; what the caller does if the re-ask also comes back
    unsubstantiated is the caller's disclosed policy, not this
    function's.
    """
    if str(getattr(action, "value", action) or "").upper() not in ("SELL", "REDUCE", "COVER"):
        return ExitTriggerCheck("ok")

    act = str(getattr(action, "value", action)).upper()
    sym = (symbol or "").strip().upper() or "?"
    named = normalize_trigger(exit_trigger)

    if named is ExitTrigger.CANNOT_SUBSTANTIATE:
        return ExitTriggerCheck(
            "unverifiable",
            trigger=None,
            needs_reask=True,
            finding=(
                f"{sym}: {act} recorded exit_trigger=cannot_substantiate — "
                f"the seat states it cannot substantiate this exit. Recorded "
                f"as given (it is a legitimate answer, not an error) and "
                f"re-asked. Not treated as false and not blocked."
            ),
        )

    healed = False
    if named is None:
        named = derive_trigger_from_reason(reason)
        healed = named is not None

    if named is None:
        return ExitTriggerCheck(
            "unverifiable",
            trigger=None,
            needs_reask=True,
            finding=(
                f"{sym}: {act} names no recognised exit trigger in either "
                f"`exit_trigger` or `reason`. Re-asked for a named trigger "
                f"and its evidence."
            ),
        )

    if not _evidence_is_substantiation(trigger_evidence, reason, named):
        return ExitTriggerCheck(
            "unverifiable",
            trigger=named,
            healed=healed,
            needs_reask=True,
            finding=(
                f"{sym}: {act} names exit_trigger={named.value} but "
                f"`trigger_evidence` carries nothing beyond the trigger "
                f"phrase itself, so the claim is not checkable. This is the "
                f"2026-09-16 shape (reason was the two words 'adverse news' "
                f"and nothing else). Logged only, not blocked, re-asked."
            ),
        )

    return ExitTriggerCheck("ok", trigger=named, healed=healed)
