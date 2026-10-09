"""The exit-trigger vocabulary and the small helpers the exit engine shares.

Moved verbatim out of `src/pipeline_exits.py` (the held-position exit engine),
which re-exports every name below, so `from src.pipeline_exits import ...` and
`from src.pipeline import ...` keep working. Nothing here may import
`src.pipeline` or `src.pipeline_exits`.
"""

import logging

#: The moved code logged under `src.pipeline` before the move and still does.
logger = logging.getLogger("src.pipeline")


# The canonical names of the sanctioned exit triggers, so the phrase gate
# below cannot name a different set of triggers from `ExitTrigger` itself.
# `src.risk.exit_trigger` imports only the stdlib, so this cannot cycle.
from src.risk.exit_trigger import (  # noqa: E402
    CANONICAL_TRIGGER_NAMES as _CANONICAL_TRIGGER_NAMES,
)
from src.risk.exit_trigger import (  # noqa: E402
    VERIFIED_ON_CHART as _VERIFIED_ON_CHART,
    canonical_prose_names as _canonical_prose_names,
)

#: Canonical prose spellings of the triggers whose truth is decided by
#: READING THE CHART. Never hard-trigger keywords — see the note below.
_CHART_VERIFIED_TRIGGER_NAMES: frozenset[str] = frozenset(
    n for t in _VERIFIED_ON_CHART for n in _canonical_prose_names(t)
) | {"trend alignment over", "alignment exit"}


# Named exit triggers — the vocabulary of NEW INFORMATION.
#
# Spec Phase 3.8: the reviewer retains full authority to exit on new
# information — adverse news, an earnings miss, a macro regime shift, a sector
# shock, a thesis invalidation. Price movement alone is
# not new information. This tuple is that list, expressed as prose the LLM
# actually emits. (Spec 3.8 also listed "a correlation breach"; that one was
# removed 2026-09-13 — see the note inside the tuple.)
#
# Soft signals — "TARGET_BREACH", "stretched", "extended", "macro noise",
# "taking profits", "de-risking" — are deliberately ABSENT and must stay
# absent. They are recurring flags, not events, and mechanically
# re-applying them is what produced the repeated same-day double-trims.
#
# Phase 3.3 (2026-08-27) widened where this gate applies. It used to guard
# only the SECOND sell-side action on a symbol in one day, so a position's
# FIRST sale — which is almost every sale — executed on soft reasoning
# entirely unchecked. It now guards every exit. Two categories were added at
# the same time, because gating every exit on a list that did not cover the
# whole of 3.8 would have blocked legitimate exits: macro regime shifts and
# sector shocks are sanctioned by 3.8 but were unrepresented here.
#
# Concentration and drift were considered for inclusion and deliberately
# REJECTED. "Concentration drift; valuation stretched" is the verbatim shape
# of the reason behind the 2026-05-04 AMZN double-trim, and drift trims belong
# to the Portfolio Manager (its rule-priority rows 4 and 5), not to this seat.
# A Tech-rating downgrade alone is likewise excluded: the Risk Manager prompt
# already states it is not sufficient grounds for an exit.
_HARD_TRIGGER_KEYWORDS: tuple[str, ...] = (
    # Thesis invalidation
    "thesis_invalid",
    "thesis invalid",
    "invalidation triggered",
    "broken thesis",
    "thesis broken",
    # Adverse company/sector news and state changes
    "high bearish",
    "high-conviction bearish",
    "high conviction bearish",
    "adverse news",
    "material news",
    "sector shock",
    # Earnings and filings
    "bearish earnings",
    "bearish filing",
    "earnings missed",
    "earnings miss",
    "guidance cut",
    # Macro regime — sanctioned by spec 3.8, previously unrepresented
    "regime shift",
    "regime flip",
    "regime flipped",
    "risk-off",
    "risk off",
    # "daily loss" / "daily-loss" / "circuit breaker" were REMOVED
    # 2026-09-20 (WORK.md item 32), for the same reason and by the same
    # precedent as the correlation phrases below: the owner deleted the
    # entire account-level loss alarm, so no part of the desk computes a
    # daily-loss or circuit-breaker EVENT any more and the claim is not
    # checkable against anything. Leaving them accepted would have been
    # strictly worse than never having had them: `cites_external_information`
    # waves a SELL/REDUCE/COVER past the noise-band and ratchet clamps when
    # the reason cites one, so a seat writing "circuit breaker" would have
    # bought itself a clamp bypass with an unverifiable phrase. There is no
    # exchange-halt (LULD) detection in this codebase either, so the
    # generous reading of "circuit breaker" has nothing behind it.
    # "correlation breach" / "correlation cluster breach" were REMOVED
    # 2026-09-13 (WORK.md item 44). They were the only accepted triggers with
    # nothing behind them: no part of the desk computes a correlation-breach
    # EVENT, `holding_discipline_claim_check` has no branch for the claim (it
    # returns "ok" — not even the log-only "unverifiable"), and a published
    # operational definition with a stated window and threshold was searched
    # for and not found (see docs/INCIDENT_HISTORY.md). Every other keyword
    # here names something the desk records: a news row, an earnings row, a
    # macro regime read, a broker fill. (This sentence used to end "a
    # deterministic circuit breaker" — that one went the same way on
    # 2026-09-20, see above.) The correlation phrase named nothing, so it
    # passed on the wording alone. Do NOT
    # re-add it without a verifier that can answer "did that happen today?".
    # Protection already fired
    "stop hit",
    "stopped out",
)

# THE ENUM IS THE SINGLE SOURCE OF TRUTH FOR WHICH TRIGGERS EXIST
# (2026-09-30, live defect on META).
#
# On 2026-09-25 17:06:23 the position reviewer emitted a REDUCE on META whose
# reason began, verbatim, "bearish_state_change: [HIGH] U.S. 10-year Treasury
# yield crosses 5% ...". `src/risk/exit_trigger.py` DECLARES
# `ExitTrigger.BEARISH_STATE_CHANGE` as a sanctioned trigger and
# `src/risk/exit_guard.py::claims_bearish_state_change` accepts the phrase,
# but the tuple above only ever carried the WORDINGS "high bearish" /
# "high(-)conviction bearish" — so the seat naming a sanctioned trigger by its
# own canonical name was refused with `exit_blocked_no_named_trigger` for
# "naming no recognised trigger". Two modules disagreed about whether the same
# sanctioned trigger existed.
#
# The fix is structural rather than another hand-maintained phrase: the
# canonical `ExitTrigger` values are appended here, derived from the enum, so
# the two vocabularies cannot diverge again without the enum itself changing.
#
# WHAT THIS DOES AND DOES NOT CLAIM ABOUT THE BAR ABOVE. Every name added
# here is a trigger this tuple already accepted under another wording, with
# ONE exception, and the bar the comment above sets — "names something the
# desk records" — is met by THREE of the six, not by all of them. Measured
# member by member 2026-09-30, and kept true mechanically by
# `exit_trigger.EVENT_TRIGGERS` / `exit_trigger.NO_VERIFIER_EXISTS`, which
# `tests/test_exit_trigger_canonical_names.py` requires every enum member to
# appear in exactly one of:
#
#   VERIFIER EXISTS — some branch of `holding_discipline_claim_check` is
#   reached for the claim and can CONTRADICT it:
#     bearish_state_change - the same-day `state_change` rows for the symbol.
#     adverse_news         - routed into that same branch deliberately.
#     regime_shift         - the day's macro regime read, when trusted.
#
#   NO VERIFIER — accepted on its wording alone. Recorded, not excused:
#     thesis_invalid - `check_structural_protection` is CONSULTED, with
#                      `advisory_only=True, persist=False`, and its own
#                      comment says it cannot change which exits execute.
#                      Consulted is not judged.
#     sector_shock   - the desk records no sector-scope row;
#                      `holding_discipline_claim_check` says so where it
#                      declines to route it.
#     stop_fired     - nothing asks the broker whether a stop filled. This is
#                      also the one genuinely NEW spelling here rather than a
#                      re-spelling of a phrase already accepted above.
#
# `earnings` is deliberately NOT added to the prose vocabulary: its canonical
# spelling is a bare common word that occurs in prose naming no event, and
# admitting it would be the widening this comment block forbids. It has no
# verifier either, and it stays reachable through the structured field.
# `cannot_substantiate` is not a trigger and is never accepted. Both prose
# exclusions are the named constant
# `exit_trigger.CANONICAL_NAME_NOT_MATCHED_IN_PROSE`, pinned by
# `tests/test_exit_trigger_canonical_names.py`.
#
# An earlier draft of this comment asserted a verifier for all six. That was
# untrue of four of them, in the one comment block whose entire job is to
# record that bar. Overstating a finding is the same failure as understating
# one, so the claim now lives in a constant a test checks.
#
# CHART-VERIFIED TRIGGERS ARE EXCLUDED, AND THIS IS LOAD-BEARING. A name in
# this tuple is a BYPASS: `_reason_cites_hard_trigger` waves the reason past
# the SELL/REDUCE noise band AND past the TRAIL_STOP ratchet cooldown and the
# 1.25xATR trail clamp, on the strength of prose alone. The alignment exit is
# the one trigger whose whole point is that prose is NOT enough — it is
# granted its bypass by `_alignment_exit_for_holding` reading the chart, and
# by nothing else. Letting its canonical name in here would hand a model a
# second, unverified way to buy the same bypass on the trail path, which runs
# no chart check at all.
_HARD_TRIGGER_KEYWORDS = _HARD_TRIGGER_KEYWORDS + tuple(
    name
    for name in _CANONICAL_TRIGGER_NAMES
    if name not in _HARD_TRIGGER_KEYWORDS and name not in _CHART_VERIFIED_TRIGGER_NAMES
)


def _reason_cites_hard_trigger(reason: str) -> bool:
    """True when the reason NAMES a recognised new-information trigger.

    Substring match, case-insensitive — the LLM emits prose, so variation is
    tolerated. The point is not to be clever about language; it is to force
    the reason to make a CLAIM ("X happened") rather than express a feeling
    ("it looks tired"). A claim is auditable, gradeable by the evening review,
    and cross-checkable against the reviewer's own metrics by
    `src/risk/exit_guard.py`. A feeling is none of those things.

    A False return on a string is a completed content judgment, not
    uncertainty: the deterministic owner refuses (see
    `src/risk/exit_refusal.py`). Callers that need to distinguish "the
    matcher could not run" from "the matcher ran and found nothing" must
    use `classify_trigger_reason`, not this boolean.
    """
    if not reason:
        return False
    lower = reason.lower()
    return any(kw in lower for kw in _HARD_TRIGGER_KEYWORDS)


def _actions_with_scan_fallback(items, displaced: dict, orders: list):
    """The midday action queue, with the SAFETY FALLBACK for scan-raised
    sales.

    A sale the alignment scan raised REPLACED whatever the review proposed
    for that symbol. Every layer below may refuse it — an unnamed trigger,
    the metric-contradiction veto, the AI Risk seat, the qty-sign gate, the
    confirmer's own verdict. If that happens the symbol must not be left
    with nothing: the action the scan displaced (in practice a TRAIL_STOP,
    the only thing besides HOLD it may overwrite) goes back on the queue and
    is executed normally, so a REFUSED scan sale leaves the position exactly
    as well protected as the scan found it — never worse.

    "Refused" is read off the only durable evidence available at this level:
    the sale appended no order to `orders`. A submitted sale always appends
    one; were it somehow not to, the fallback re-protects a position that is
    closing, which is the harmless direction to be wrong in.

    A generator so the executor loop is unchanged: it resumes here after the
    body has run, whichever `continue` the body took to get out.
    """
    queue = list(items)
    while queue:
        item = queue.pop(0)
        orders_before = len(orders)
        yield item
        if not item.get("_alignment_scan_raised"):
            continue
        if len(orders) > orders_before:
            continue
        fallback = displaced.pop((item.get("symbol") or "").strip().upper(), None)
        if fallback is None:
            continue
        logger.warning(
            "Alignment scan: the %s it raised for %s was refused downstream "
            "— restoring the %s the review asked for, so the position is not "
            "left unprotected",
            item.get("action"),
            item.get("symbol"),
            fallback.get("action"),
        )
        queue.append(fallback)


def _reason_claims_alignment_exit(reason: str, exit_trigger: object = None) -> bool:
    """Does this sale claim the TREND IS OVER (the alignment exit)?

    Read from the STRUCTURED trigger first and the prose only as a
    fallback, same precedence the holding-discipline fact-check uses.
    """
    from src.risk.exit_trigger import ExitTrigger

    t = getattr(exit_trigger, "value", exit_trigger)
    if isinstance(t, str) and t.strip().lower() == ExitTrigger.TREND_ALIGNMENT_OVER.value:
        return True
    low = (reason or "").lower()
    return "trend alignment over" in low or "alignment exit" in low


def _collab_of(obj, name: str):
    """A collaborator for a lifted exit object: `obj._collab(name)` when the host
    offers the deferred-error stand-in (TradingPipeline does), else the attribute."""
    collab = getattr(obj, "_collab", None)
    if collab is not None:
        return collab(name)
    return getattr(obj, name)
