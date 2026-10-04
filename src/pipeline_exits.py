"""The held-position exit engine: everything that decides whether an open
position is SOLD, and the one method that executes the resulting actions.

Step 4 of `docs/PIPELINE_SPLIT_PLAN.md` (board item 210), clusters K and L,
re-measured against the step-3 branch before anything was touched. Moved
verbatim out of `src/pipeline.py` as a mixin, so `TradingPipeline` keeps every
one of these as its own attribute and every test that patches or calls them is
untouched.

This is the live-money SELL path: target-revision adjudication, structural
protection for a holding and the voicing of its break, exit-trigger
substantiation, the holding-discipline fact-check, the deterministic trails and
their ratchet cooldown, the event-risk block, the alignment exit (scan, cache,
per-holding verdict and reading record), the AI risk review of exits, the
refusal and approval records, and `_midday_execute_llm_actions`.

The module-level exit-trigger vocabulary travels with it: the
`_HARD_TRIGGER_KEYWORDS` tuple and `_reason_cites_hard_trigger`, plus
`_actions_with_scan_fallback` and `_reason_claims_alignment_exit`, whose only
caller is `_midday_execute_llm_actions`. All are re-exported from `src.pipeline`
so `from src.pipeline import ...` keeps working -- but a test that PATCHES one
of them on `src.pipeline` no longer reaches this module's code and must patch it
here instead (plan S5, silent-behaviour risk 1).

`_atr_for_symbol` and `_constructor_cfg_or_none` sit inside this cluster's range
and deliberately did NOT move: `_atr_for_symbol` is read by the base class's
`_evening_stop_proximity` and by `PromptFactsMixin`, and `_constructor_cfg_or_none`
is read by the admission shell (`AdmissionMixin`), not `__init__`. Both stay in
`src/pipeline.py` as thin delegations, which lends them back to this mixin.

Nothing here may import `src.pipeline`: this module is one of its bases.
"""

import json as _json
import logging
import math
from datetime import datetime, timedelta

from src.models import ReasoningChain, TradeDecision
from src.trading_calendar import et_today

#: The moved code logged under `src.pipeline` before the move and still does;
#: binding the name rather than `__name__` keeps log records byte-identical.
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
    name for name in _CANONICAL_TRIGGER_NAMES
    if name not in _HARD_TRIGGER_KEYWORDS
    and name not in _CHART_VERIFIED_TRIGGER_NAMES
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
            item.get("action"), item.get("symbol"), fallback.get("action"),
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


class ExitEngineMixin:
    """Cluster K + L of `docs/PIPELINE_SPLIT_PLAN.md`: the sell-side engine."""

    def _symbols_already_trimmed_today(self, *args, **kwargs):
        """Thin shim: builds the standalone ExitRecords and calls it (body moved to src/exits/exit_records.py)."""
        from src.exits.exit_records import ExitRecords
        return ExitRecords(
            trade_executed_or_pending=_collab_of(self, "_trade_executed_or_pending"),
            record_exit_refusal=_collab_of(self, "_record_exit_refusal"),
            db=_collab_of(self, "db"),
            market=_collab_of(self, "market"),
        )._symbols_already_trimmed_today(*args, **kwargs)

    # --- lifted to src/exits/target_revision.py (TargetRevision); thin shims follow ---
    def _adjudicate_target_revision_flags(self, *args, **kwargs):
        """Thin shim: builds the standalone TargetRevision and calls it (body moved to src/exits/target_revision.py)."""
        from src.exits.target_revision import TargetRevision
        return TargetRevision(
            file_target_revision=_collab_of(self, "_file_target_revision"),
            broker=_collab_of(self, "broker"),
            config=_collab_of(self, "config"),
            db=_collab_of(self, "db"),
            market=_collab_of(self, "market"),
            risk_engine=getattr(self, "risk_engine", None),
        )._adjudicate_target_revision_flags(*args, **kwargs)

    def _file_target_revision(self, *args, **kwargs):
        """Thin shim: builds the standalone ExitRecords and calls it (body moved to src/exits/exit_records.py)."""
        from src.exits.exit_records import ExitRecords
        return ExitRecords(
            trade_executed_or_pending=_collab_of(self, "_trade_executed_or_pending"),
            record_exit_refusal=_collab_of(self, "_record_exit_refusal"),
            db=_collab_of(self, "db"),
            market=_collab_of(self, "market"),
        )._file_target_revision(*args, **kwargs)

    # --- lifted to src/exits/structural_protection.py (StructuralProtection); thin shims follow ---
    def _structural_protection_for_holding(self, *args, **kwargs):
        """Thin shim: builds the standalone StructuralProtection and calls it (body moved to src/exits/structural_protection.py)."""
        from src.exits.structural_protection import StructuralProtection
        return StructuralProtection(
            voice_structural_protection_break=_collab_of(self, "_voice_structural_protection_break"),
            config=_collab_of(self, "config"),
            db=_collab_of(self, "db"),
            market=_collab_of(self, "market"),
            risk_engine=getattr(self, "risk_engine", None),
        )._structural_protection_for_holding(*args, **kwargs)

    def _voice_structural_protection_break(
        self, *, symbol: str, run_id: str, check,
    ) -> None:
        """Push a decisive structural-protection break's plain-language reason
        to BOTH owner surfaces (Telegram + board journal). Never raises.

        Reuses the desk's established durable-reason trail rather than adding a
        new one: the same `notifier.send_owner_alert` standalone-alert path
        `_alert_holding_discipline_block` uses for Telegram, and a
        `specialist_evidence` row (the same table the board journal and
        Mission Control forensic views read) for the dashboard. Deduplicated
        per (run, symbol, basis) via a run-scoped set.

        SILENT-ACTION GUARD (#5): the dedup slot is consumed only AFTER at least
        one surface write (board OR Telegram) SUCCEEDS. If BOTH fail, the slot is
        left free so a later cycle retries — the desk must never act on a break
        without the why reaching at least one surface.
        """
        from src.risk.exit_guard import render_owner_break_message

        message = render_owner_break_message(symbol, check)
        if not message:
            return
        symbol_u = (symbol or "").strip().upper()
        dedup_key = (run_id, symbol_u, check.basis)
        seen = getattr(self, "_voiced_structural_breaks", None)
        if seen is None:
            seen = set()
            self._voiced_structural_breaks = seen
        if dedup_key in seen:
            return

        any_surface_ok = False

        # Board / dashboard: a durable, machine-readable row carrying the SAME
        # sentence, on the specialist_evidence table the journal reads.
        try:
            self.db.insert_specialist_evidence(
                run_id=run_id, agent_name="risk_manager",
                kind="structural_break_trend_context", scope="symbol",
                symbol=symbol_u,
                evidence_json=_json.dumps({
                    "protected": bool(check.protected),
                    "basis": check.basis,
                    "trend_context": check.trend_context,
                    "confirming_closes_needed": check.confirming_closes_needed,
                    "confirming_closes_seen": check.confirming_closes_seen,
                    "owner_reason": message,
                }),
            )
            any_surface_ok = True
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "structural protection: board reason write failed for %s "
                "(%s) — Telegram send still attempted", symbol_u, e,
            )

        # Telegram: the same standalone owner-alert path the holding-discipline
        # block uses. `send_owner_alert` does NOT raise on a failed send — it
        # RETURNS False — so a surface only counts as reached when the return is
        # truthy (and, as a backstop, when it does not raise).
        try:
            from src import notifier as _notifier

            ok = _notifier.send_owner_alert(message, symbols=[symbol_u])
            any_surface_ok |= bool(ok)
        except Exception as e:  # noqa: BLE001
            logger.error(
                "structural protection: owner alert send failed for %s (%s)",
                symbol_u, e,
            )

        # Consume the dedup slot only if the why reached at least one surface;
        # otherwise leave it free so a later cycle retries rather than the desk
        # acting silently.
        if any_surface_ok:
            seen.add(dedup_key)

    # --- lifted to src/exits/exit_substantiation.py (ExitSubstantiation); thin shims follow ---
    def _substantiate_exit_triggers(self, *args, **kwargs):
        """Thin shim: builds the standalone ExitSubstantiation and calls it (body moved to src/exits/exit_substantiation.py)."""
        from src.exits.exit_substantiation import ExitSubstantiation
        return ExitSubstantiation(
            record_heal=_collab_of(self, "_record_heal"),
            require_paid_analysis=_collab_of(self, "_require_paid_analysis"),
            db=_collab_of(self, "db"),
            position_reviewer=_collab_of(self, "position_reviewer"),
        )._substantiate_exit_triggers(*args, **kwargs)

    # --- lifted to src/exits/holding_discipline.py (HoldingDiscipline); thin shims follow ---
    def _holding_discipline_check_for_exit(self, *args, **kwargs):
        """Thin shim: builds the standalone HoldingDiscipline and calls it (body moved to src/exits/holding_discipline.py)."""
        from src.exits.holding_discipline import HoldingDiscipline
        return HoldingDiscipline(
            build_active_state_changes=_collab_of(self, "_build_active_state_changes"),
            carry_forward_macro=_collab_of(self, "_carry_forward_macro"),
            structural_protection_for_holding=_collab_of(self, "_structural_protection_for_holding"),
            db=_collab_of(self, "db"),
        )._holding_discipline_check_for_exit(*args, **kwargs)

    def _trail_tightened_recently(self, *args, **kwargs):
        """Thin shim: builds the standalone ExitRecords and calls it (body moved to src/exits/exit_records.py)."""
        from src.exits.exit_records import ExitRecords
        return ExitRecords(
            trade_executed_or_pending=_collab_of(self, "_trade_executed_or_pending"),
            record_exit_refusal=_collab_of(self, "_record_exit_refusal"),
            db=_collab_of(self, "db"),
            market=_collab_of(self, "market"),
        )._trail_tightened_recently(*args, **kwargs)

    def _apply_deterministic_trails(self, *args, **kwargs):
        """Thin shim: builds the standalone DeterministicTrails and calls it (body moved to src/exits/deterministic_trails.py)."""
        from src.exits.deterministic_trails import DeterministicTrails
        return DeterministicTrails(
            atr_for_symbol=_collab_of(self, "_atr_for_symbol"),
            repair_stop_coverage=_collab_of(self, "_repair_stop_coverage"),
            broker=_collab_of(self, "broker"),
            db=_collab_of(self, "db"),
            market=_collab_of(self, "market"),
        )._apply_deterministic_trails(*args, **kwargs)

    def _exit_event_risk_block(self, *args, **kwargs):
        """Thin shim: builds the standalone ExitRecords and calls it (body moved to src/exits/exit_records.py)."""
        from src.exits.exit_records import ExitRecords
        return ExitRecords(
            trade_executed_or_pending=_collab_of(self, "_trade_executed_or_pending"),
            record_exit_refusal=_collab_of(self, "_record_exit_refusal"),
            db=_collab_of(self, "db"),
            market=_collab_of(self, "market"),
        )._exit_event_risk_block(*args, **kwargs)


    #: Per-run memo for the alignment verdict, keyed
    #: (run_id, symbol, is_short). The scan below and the confirmer inside
    #: `_midday_execute_llm_actions` ask the SAME question about the same
    #: position in the same pass; the chart read behind it costs bars plus a
    #: structural-protection evaluation, so it is computed once. Same inputs,
    #: same deterministic answer — this changes no verdict, only the count of
    #: reads. Declared at class level so an instance built without __init__
    #: (tests do this) still reads a value rather than raising.
    _alignment_exit_memo: dict | None = None

    def _alignment_exit_cached(
        self, *, symbol: str, thesis_invalid_if: str | None, is_short: bool,
        entry_price: float | None, stop_loss: float | None, run_id: str,
    ):
        """`_alignment_exit_for_holding`, computed at most once per
        (run, symbol, side). Never raises: a memo failure just recomputes."""
        key = (run_id, symbol, bool(is_short))
        memo = self._alignment_exit_memo
        if not isinstance(memo, dict):
            memo = {}
            self._alignment_exit_memo = memo
        if key in memo:
            return memo[key]
        verdict = self._alignment_exit_for_holding(
            symbol=symbol, thesis_invalid_if=thesis_invalid_if,
            is_short=is_short, entry_price=entry_price, stop_loss=stop_loss,
            run_id=run_id,
        )
        memo[key] = verdict
        return verdict

    # --- lifted to src/exits/alignment_exit.py (AlignmentExit); thin shims follow ---
    def _alignment_exit_scan(self, *args, **kwargs):
        """Thin shim: builds the standalone AlignmentExit and calls it (body moved to src/exits/alignment_exit.py)."""
        from src.exits.alignment_exit import AlignmentExit
        return AlignmentExit(
            alignment_exit_cached=_collab_of(self, "_alignment_exit_cached"),
            structural_protection_for_holding=_collab_of(self, "_structural_protection_for_holding"),
            config=_collab_of(self, "config"),
            db=_collab_of(self, "db"),
            market=_collab_of(self, "market"),
        )._alignment_exit_scan(*args, **kwargs)

    def _record_alignment_reading(self, *args, **kwargs):
        """Thin shim: builds the standalone AlignmentExit and calls it (body moved to src/exits/alignment_exit.py)."""
        from src.exits.alignment_exit import AlignmentExit
        return AlignmentExit(
            alignment_exit_cached=_collab_of(self, "_alignment_exit_cached"),
            structural_protection_for_holding=_collab_of(self, "_structural_protection_for_holding"),
            config=_collab_of(self, "config"),
            db=_collab_of(self, "db"),
            market=_collab_of(self, "market"),
        )._record_alignment_reading(*args, **kwargs)

    def _position_opened_today(self, *args, **kwargs):
        """Thin shim: builds the standalone AlignmentExit and calls it (body moved to src/exits/alignment_exit.py)."""
        from src.exits.alignment_exit import AlignmentExit
        return AlignmentExit(
            alignment_exit_cached=_collab_of(self, "_alignment_exit_cached"),
            structural_protection_for_holding=_collab_of(self, "_structural_protection_for_holding"),
            config=_collab_of(self, "config"),
            db=_collab_of(self, "db"),
            market=_collab_of(self, "market"),
        )._position_opened_today(*args, **kwargs)

    def _alignment_exit_for_holding(self, *args, **kwargs):
        """Thin shim: builds the standalone AlignmentExit and calls it (body moved to src/exits/alignment_exit.py)."""
        from src.exits.alignment_exit import AlignmentExit
        return AlignmentExit(
            alignment_exit_cached=_collab_of(self, "_alignment_exit_cached"),
            structural_protection_for_holding=_collab_of(self, "_structural_protection_for_holding"),
            config=_collab_of(self, "config"),
            db=_collab_of(self, "db"),
            market=_collab_of(self, "market"),
        )._alignment_exit_for_holding(*args, **kwargs)

    def _record_exit_refusal(
        self, *, symbol: str, run_id: str, action: str, code: str,
        dropped: bool, detail: str, layer: str,
    ) -> None:
        """Append-only per-symbol refusal/uncertainty record. Never raises."""
        from src.risk.exit_refusal import record_exit_refusal
        record_exit_refusal(
            self.db, symbol=symbol, run_id=run_id, action=action,
            code=code, dropped=dropped, detail=detail, layer=layer,
        )

    def _risk_review_exits(self, *args, **kwargs):
        """Thin shim: builds the standalone RiskReviewExits and calls it (body moved to src/exits/risk_review_exits.py)."""
        from src.exits.risk_review_exits import RiskReviewExits
        return RiskReviewExits(
            build_portfolio_heat=_collab_of(self, "_build_portfolio_heat"),
            build_position_history=_collab_of(self, "_build_position_history"),
            exit_event_risk_block=_collab_of(self, "_exit_event_risk_block"),
            record_exit_refusal=_collab_of(self, "_record_exit_refusal"),
            record_exit_review_approvals=_collab_of(self, "_record_exit_review_approvals"),
            db=_collab_of(self, "db"),
            risk_manager=_collab_of(self, "risk_manager"),
        )._risk_review_exits(*args, **kwargs)

    def _record_exit_review_approvals(self, *args, **kwargs):
        """Thin shim: builds the standalone ExitRecords and calls it (body moved to src/exits/exit_records.py)."""
        from src.exits.exit_records import ExitRecords
        return ExitRecords(
            trade_executed_or_pending=_collab_of(self, "_trade_executed_or_pending"),
            record_exit_refusal=_collab_of(self, "_record_exit_refusal"),
            db=_collab_of(self, "db"),
            market=_collab_of(self, "market"),
        )._record_exit_review_approvals(*args, **kwargs)

    def _midday_execute_llm_actions(
        self, positions, review, run_id: str,
        already_trimmed_today: set[str] | None = None,
        metric_deltas: dict | None = None,
        risk_vetoed_symbols: set[str] | None = None,
        position_facts: dict | None = None,
    ) -> list[dict]:
        """Dispatch LLM-recommended SELL / REDUCE / TRAIL_STOP / COVER actions
        to broker.

        Dedups same-symbol conflicting actions by priority (SELL/COVER >
        REDUCE > TRAIL_STOP > HOLD) to avoid the broker seeing two orders
        fighting each other on one position. (A `blocked_symbols` argument
        used to suppress LLM exits on a symbol whose midday auto-take-profit
        sell was still in flight; that rule was deleted 2026-09-12 and no
        other system sell runs ahead of the reviewer in the same session.)

        COVER is the short-side twin of SELL/REDUCE (Stage 3 shorts gap
        fix): it is the ONLY lever the reviewer has on a held short (never
        SELL — the executor requires the action to match the held side,
        see the qty-sign gate below) and it routes through every protection
        a SELL/REDUCE gets — the named-trigger phrase gate, the exit
        guard's metric-contradiction veto, the noise band, the same-day-trim
        discipline, and (further down `run_position_review`) the AI Risk
        routing via `_risk_review_exits`. It always executes as a FULL
        close (`_full_sell_qty`, mirroring SELL) — the schema
        (`PositionAction`) carries no allocation fraction for it, unlike the
        PM's `TradeDecision.allocation_pct`, so there is no partial-COVER
        signal for this path to act on.
        """
        orders: list[dict] = []
        _priority = {"SELL": 0, "COVER": 0, "REDUCE": 1, "TRAIL_STOP": 2, "HOLD": 3}
        best_by_symbol: dict[str, dict] = {}
        actions_raw = review.actions if review else []
        actions_list = [a.model_dump() for a in actions_raw]
        for ai in actions_list:
            sym = (ai.get("symbol") or "").strip().upper()
            if not sym:
                continue
            curr = best_by_symbol.get(sym)
            if curr is None or _priority.get(ai.get("action"), 99) < _priority.get(curr.get("action"), 99):
                best_by_symbol[sym] = ai
        if len(best_by_symbol) < len(actions_list):
            dropped = len(actions_list) - len(best_by_symbol)
            logger.info(
                "Midday: collapsed %d duplicate same-symbol actions "
                "(priority SELL/COVER>REDUCE>TRAIL_STOP>HOLD)", dropped,
            )

        # THE ALIGNMENT SCAN — every held position is read against the
        # owner-ratified alignment exit here, before the early return below,
        # because a review that proposed nothing at all is exactly the
        # session in which the chart must still be allowed to speak.
        _scan_displaced: dict[str, dict] = {}
        self._alignment_exit_scan(
            positions, best_by_symbol, run_id=run_id,
            position_facts=position_facts, priority=_priority,
            displaced=_scan_displaced,
        )

        if not best_by_symbol:
            return orders

        already_trimmed = {
            symbol.strip().upper()
            for symbol in (already_trimmed_today or set())
            if symbol and symbol.strip()
        }
        # Board item 74 — what the desk has ALREADY acted on today, so a
        # trigger cannot authorise a second cut of the same name on the same
        # record. Read ONCE per execution pass and appended to in-process as
        # cuts submit, so two actions inside THIS pass cannot double-cut
        # either. `None` means the read failed: that is uncertainty and the
        # layer fails OPEN (src/risk/spent_trigger.py).
        from src.risk.spent_trigger import (
            SPENT_LAYER, acted_trigger_payload, keep_executed_acted_triggers,
            parse_acted_triggers, spent_trigger_check,
        )
        try:
            _raw_acted = self.db.get_acted_exit_triggers_today()
            acted_today = (
                None if _raw_acted is None else parse_acted_triggers(_raw_acted)
            )
            # A trigger is spent by a cut that actually REDUCED the position,
            # never by one merely submitted. The executed set is built from
            # the same `_trade_executed_or_pending` contract the sibling
            # same-day-trim gate uses, so the two gates cannot hold opposite
            # views of what a real fill is: a rejected / cancelled / expired
            # zero-fill cut spends nothing and the name is fair game again.
            _executed_order_ids: set[str] | None = {
                str(r.get("broker_order_id"))
                for r in (self.db.get_trades(today_only=True, limit=200) or [])
                if r.get("broker_order_id")
                and self._trade_executed_or_pending(r)
            }
            acted_today = keep_executed_acted_triggers(
                acted_today, executed_order_ids=_executed_order_ids,
            )
        except Exception as _e:  # noqa: BLE001 — a failed read is uncertainty
            logger.warning(
                "spent trigger: today's acted-trigger record could not be "
                "read (%s) — this layer fails OPEN for this pass", _e,
            )
            acted_today = None
        # Entry context (thesis_invalid_if / entry price / entry stop) for the
        # holding-discipline claim check below. Built ONCE and only if some
        # exit actually reaches that gate — a HOLD-only or TRAIL_STOP-only
        # review must not buy the DB reads.
        hd_position_history: dict | None = None

        for action_item in _actions_with_scan_fallback(
            best_by_symbol.values(), _scan_displaced, orders,
        ):
            act = action_item.get("action")
            if act not in ("SELL", "REDUCE", "TRAIL_STOP", "COVER"):
                continue
            symbol = action_item.get("symbol", "")
            # Same-day trim discipline: a symbol that already had a sell-side
            # action TODAY (midday REDUCE, force-delever, etc.) is off-limits for
            # additional REDUCE / SELL on a SECOND session unless the LLM
            # explicitly cites a hard trigger in the reason. TRAIL_STOP is
            # exempt — adjusting a stop is not selling shares.
            #
            # 2026-05-04 AMZN: midday REDUCE 20 of 41 @ +12.4% on TARGET_BREACH,
            # then close REDUCE 10 of 21 @ +13.8% on the SAME TARGET_BREACH
            # flag = 73% one-day trim on a strengthening thesis. Mechanical
            # double-application of one signal violates "good stocks are meant
            # to be held".
            # Phase 3.2 — a deterioration verdict may not contradict the
            # reviewer's own recorded numbers. Vetoes ONLY a SELL/REDUCE/
            # COVER whose stated reason claims the position is stalling
            # while every metric that moved since the previous review
            # improved. Exits on new information (news, earnings, regime,
            # invalidation) are untouched, however good the numbers look —
            # see src/risk/exit_guard.py. metric_deltas is already sign-
            # corrected per symbol (see _build_position_facts), so COVER
            # needs no extra handling here.
            if act in ("SELL", "REDUCE", "COVER") and metric_deltas:
                from src.risk.exit_guard import veto_contradicted_exit
                deltas = metric_deltas.get(symbol)
                if deltas is not None:
                    veto = veto_contradicted_exit(
                        act, action_item.get("reason", ""), deltas,
                    )
                    if veto:
                        logger.warning("Exit guard: %s", veto)
                        try:
                            self.db.record_intraday_evaluation(
                                symbol=symbol, run_id=run_id,
                                status="exit_vetoed_contradicts_own_metrics",
                                detail=veto[:500],
                            )
                        except Exception as e:  # noqa: BLE001
                            logger.warning("exit guard: audit write failed: %s", e)
                        from src.risk.exit_refusal import CODE_CONTRADICTS_METRICS
                        self._record_exit_refusal(
                            symbol=symbol, run_id=run_id, action=act,
                            code=CODE_CONTRADICTS_METRICS, dropped=True,
                            detail=veto[:400], layer="metric_contradiction",
                        )
                        continue

            # Phase 3.3 — EVERY exit must name a trigger, not just the second
            # one on a symbol in a day.
            #
            # The gate below used to be conditioned on `symbol in
            # already_trimmed`, so a position's FIRST sale of the day executed
            # on soft reasoning entirely unchecked — and a first sale is almost
            # every sale. Both of the exits the evening review graded
            # "premature" on 2026-08-26 (EPD, MRVL) were first sales and sailed
            # straight through.
            #
            # Failing closed here means HOLDING, and every position carries a
            # broker-resident stop (AGENTS.md invariant 3), so the downside of
            # a wrongly-blocked exit is bounded by that stop. The downside of a
            # wrongly-allowed one is the pattern that emptied the book.
            # Phase 3.4 — the AI Risk Manager reviewed these exits and
            # rejected this one. Its authority over exits mirrors the veto it
            # has always had over entries.
            if act in ("SELL", "REDUCE", "COVER") and symbol in (risk_vetoed_symbols or set()):
                logger.warning(
                    "Position reviewer: skipping %s %s — vetoed by AI Risk",
                    act, symbol,
                )
                continue

            # Phase 3.6 — noise band on exits. A PRICE-DERIVED failure inside
            # one ATR of entry has not distinguished itself from one ordinary
            # day's range. OKLO was bought and sold on 2026-08-26 at 0.67 ATR,
            # on day zero, never given a single day's normal range to breathe.
            #
            # Triggers originating outside the tape — earnings, news, regime,
            # sector, a fired stop — bypass this entirely. ("correlation" and
            # "circuit breaker" were in this sentence until they were removed
            # from the accepted list, 2026-09-13 and 2026-09-20; neither
            # bypasses anything now.) An earnings miss is an earnings miss whether the stock
            # has moved 0.2 ATR or 3 ATR, and waiting for price confirmation
            # before acting on information sells the bottom instead of the top.
            if act in ("SELL", "REDUCE", "COVER"):
                from src.risk.exit_guard import (
                    adverse_move_is_noise, cites_external_information,
                )
                held_now = next((p for p in positions if p.symbol == symbol), None)
                reason_for_band = action_item.get("reason", "")
                # COVER's adverse direction is the mirror of SELL/REDUCE's —
                # a short is hurt by price RISING, not falling — so the
                # noise band is measured against the CLOSING side, same
                # convention as _submit_protected_sell's `side` param.
                close_side = "buy" if act == "COVER" else "sell"

                # THE ALIGNMENT EXIT (owner ruling 2026-09-30, "exit on
                # ALIGNMENT, never on a target") — the desk's only sanctioned
                # way to realise a GAIN, and the one non-news sale allowed
                # past the entry-anchored noise band below.
                #
                # A closed first attempt (PR 837) DELETED that band and
                # shipped `check_alignment_exit` with no caller anywhere in
                # src/ — the brake gone and nothing computing the reading
                # meant to replace it, which is strictly worse than doing
                # nothing. The band therefore stays, and this is the caller.
                #
                # A sale claiming the trend is over is now VERIFIED, not trusted:
                # only a chart that confirms the last mark has been given up by
                # more than the give-back tolerance gets through. An unconfirmed
                # or unreadable chart DROPS the sale — the opposite posture to
                # the fail-open gates below, and deliberately so, because this is
                # the one exit the desk takes with no external event behind it
                # and possibly with the other seats still positive.
                #
                # THE READING IS TAKEN ON EVERY EXIT OF A HELD POSITION,
                # not only on the ones whose prose happens to name it. A
                # sale the model wanted for some other reason still leaves
                # a durable record of what the chart said about that
                # position's trend at that moment; without it, a position
                # the desk exited has no alignment record at all and the
                # evening review cannot tell an unread chart from a chart
                # that said hold. Only a sale that CLAIMS the alignment
                # exit is GATED by the verdict.
                alignment_verdict = None
                alignment_claimed = _reason_claims_alignment_exit(
                    reason_for_band, action_item.get("exit_trigger"),
                )
                if held_now is not None:
                    facts = (position_facts or {}).get(symbol, {}) or {}
                    verdict = self._alignment_exit_cached(
                        symbol=symbol,
                        thesis_invalid_if=getattr(held_now, "thesis_invalid_if", None)
                        or facts.get("thesis_invalid_if"),
                        is_short=(act == "COVER"),
                        entry_price=getattr(held_now, "avg_entry", None),
                        # IDENTICAL to the scan's inputs, including the
                        # position-facts fallback. `stop_loss` decides
                        # whether a broken-level mark exists, and both
                        # callers key the SAME memo — resolving it
                        # differently would let one of them read a verdict
                        # built from a stop the other never passed.
                        stop_loss=getattr(held_now, "stop_loss", None)
                        or facts.get("stop_loss"),
                        run_id=run_id,
                    )
                    # EVERY verdict leaves a durable, machine-readable, per-symbol
                    # record — INCLUDING the "could not read the chart" states.
                    # Without them the desk cannot tell a position it HELD from
                    # one it failed to read, and neither the other seats nor the
                    # owner can see that an exit was considered at all. The parsed
                    # thesis MA period and the prose it came from are recorded
                    # with it: that text is model-written and unversioned, so a
                    # reword silently changes which price decides a sale, and
                    # without pinning it the record would not say which average
                    # actually decided this one.
                    det = (
                        f"{act}: {verdict.status} "
                        f"claimed={alignment_claimed} "
                        f"ma={verdict.thesis_ma_kind}{verdict.thesis_ma_period} "
                        f"breach_atr={verdict.breach_atrs} "
                        f"tolerance_atr={verdict.band_atrs} "
                        f"sessions_since_mark_lost={verdict.sessions_since_mark_lost} "
                        f"thesis={verdict.thesis_text!r} "
                        f"| {verdict.reason}"
                    )[:1200]
                    self._record_exit_refusal(
                        symbol=symbol, run_id=run_id, action=act,
                        code=verdict.code,
                        dropped=alignment_claimed and not verdict.exit_cleared,
                        detail=det,
                        layer=(
                            "alignment_exit" if alignment_claimed
                            else "alignment_exit_observed"
                        ),
                    )
                    try:
                        self.db.record_intraday_evaluation(
                            symbol=symbol, run_id=run_id,
                            status=f"alignment_exit_{verdict.status.lower()}",
                            detail=det[:400],
                        )
                    except Exception as e:  # noqa: BLE001
                        logger.warning("alignment exit: audit write failed: %s", e)
                    logger.info(
                        "Alignment exit %s %s: %s (claimed=%s) — %s", act, symbol,
                        verdict.status, alignment_claimed, verdict.reason,
                    )
                    if alignment_claimed:
                        alignment_verdict = verdict
                        if not verdict.exit_cleared:
                            continue
                        # Carry the chart's own words into the order reason
                        # so the owner and the other seats read WHY, not
                        # just THAT.
                        if verdict.owner_reason:
                            action_item["reason"] = (
                                f"{action_item.get('reason', '')} | "
                                f"{verdict.owner_reason}"
                            )[:2000]

                # The ALIGNMENT EXIT above is the one non-news sale allowed
                # past this band. The band STAYS for everything else: it is a
                # real brake on premature exits, and deleting it while
                # shipping a verdict nothing in src/ ever called (the closed
                # PR 837) would leave the desk with neither. A chart-verified
                # alignment exit is simply not judged by its distance from
                # what the desk PAID, because what the desk paid says nothing
                # about whether a trend has ended.
                if held_now is not None and alignment_verdict is None and not cites_external_information(reason_for_band):
                    from src.risk.exit_guard import noise_band_atr
                    from src.risk.noise_band_record import midday_payload as midday_band_payload

                    atr = self._atr_for_symbol(symbol)
                    # Phase 3.6 audit follow-up (2026-09-04, fix #1): the band
                    # widens with sqrt(sessions_held) — same convention as
                    # levels.py's target projection — not a flat 1.0x ATR
                    # however long the position has aged; see
                    # `exit_guard.noise_band_atr`. Second pass: this MUST
                    # be `sessions_held` (weekend-aware trading-session count,
                    # `trading_calendar.trading_sessions_held`), NOT the plain
                    # calendar-day `days_held` — levels.py's own precedent
                    # scales by sqrt(TRADING sessions), and a calendar-day
                    # count silently over-widens the band by sqrt(3/1) after
                    # every weekend (Friday entry reviewed Monday shows 3
                    # calendar days but only 1 real session of price action).
                    sessions_held_for_band = (position_facts or {}).get(symbol, {}).get("sessions_held")
                    # BOARD ITEM 70, 2026-10-04: the band used to be recorded
                    # ONLY when it BLOCKED, and a sample truncated at the
                    # threshold under examination can never locate it. Both
                    # outcomes now go to `src/risk/noise_band_record.py`.
                    _band_blocks = adverse_move_is_noise(
                        held_now.avg_entry, held_now.current_price, atr,
                        side=close_side, days_held=sessions_held_for_band,
                    )
                    adverse_move = (
                        held_now.current_price - held_now.avg_entry
                        if close_side == "buy"
                        else held_now.avg_entry - held_now.current_price
                    )
                    band_multiple = noise_band_atr(sessions_held_for_band)
                    # Board item 70, 2026-09-30 — TRUTH OF THE RECORD.
                    # `noise_band_atr` SILENTLY FLOORS a missing or sub-1
                    # session count to 1, so a width quoted beside
                    # `sessions_held=None` asserted what the record could not
                    # support. Say which it was.
                    try:
                        _sess = float(sessions_held_for_band) if sessions_held_for_band is not None else None
                    except (TypeError, ValueError):
                        _sess = None
                    sessions_measured = (
                        _sess is not None and math.isfinite(_sess) and _sess >= 1.0
                    )
                    sessions_text = (
                        f"{_sess:g} (measured)" if sessions_measured
                        else f"{sessions_held_for_band!r} unusable — floored to 1 session"
                    )
                    _atr_f = float(atr or 0.0)
                    band_width = band_multiple * _atr_f
                    band_detail = midday_band_payload(
                        close_side=close_side, blocked=_band_blocks,
                        adverse=adverse_move, entry=held_now.avg_entry,
                        price=held_now.current_price, atr=_atr_f,
                        band_multiple=band_multiple,
                        sessions_held=_sess if sessions_measured else 1.0,
                        sessions_measured=sessions_measured,
                        tail=f"{act}: {reason_for_band[:400]}",
                    )
                    try:
                        self.db.record_intraday_evaluation(
                            symbol=symbol, run_id=run_id,
                            status=(
                                "exit_blocked_inside_atr_noise_band"
                                if _band_blocks
                                else "exit_noise_band_evaluated_not_blocked"
                            ),
                            detail=band_detail,
                        )
                    except Exception as e:  # noqa: BLE001
                        logger.warning("noise band: audit write failed: %s", e)
                    if _band_blocks:
                        logger.warning(
                            "Position reviewer: blocking %s %s — adverse "
                            "$%.2f move from entry $%.2f is smaller than "
                            "$%.2f, which is %.2f x ATR14 $%.2f with "
                            "sessions_held=%s. That comparison, and nothing "
                            "else, is what refused this exit. "
                            "External-information triggers bypass this. "
                            "Reason: %r",
                            act, symbol, adverse_move,
                            held_now.avg_entry, band_width, band_multiple,
                            _atr_f, sessions_text,
                            reason_for_band[:160],
                        )
                        from src.risk.exit_refusal import CODE_NOISE_BAND
                        self._record_exit_refusal(
                            symbol=symbol, run_id=run_id, action=act,
                            code=CODE_NOISE_BAND, dropped=True,
                            detail=band_detail,
                            layer="noise_band",
                        )
                        continue

            reason_text = action_item.get("reason", "")
            if act in ("SELL", "REDUCE", "COVER"):
                from src.risk.exit_refusal import (
                    CODE_HARD_TRIGGER_UNCERTAIN,
                    CODE_UNRECOGNIZED_TRIGGER,
                    classify_trigger_reason,
                )
                trigger_judgment = classify_trigger_reason(
                    reason_text, cites=_reason_cites_hard_trigger,
                    trigger=action_item.get("exit_trigger"),
                    trigger_evidence=action_item.get("trigger_evidence"),
                )
                if trigger_judgment == "unnamed":
                    logger.warning(
                        "Position reviewer: blocking %s %s — the reason names no "
                        "recognised trigger. Exits require NEW INFORMATION "
                        "(thesis invalidation, adverse news, earnings, regime "
                        "shift, sector shock, stop hit); "
                        "price action and soft flags are not triggers. Reason "
                        "was: %r",
                        act, symbol, str(reason_text)[:200],
                    )
                    try:
                        self.db.record_intraday_evaluation(
                            symbol=symbol, run_id=run_id,
                            status="exit_blocked_no_named_trigger",
                            detail=f"{act}: {str(reason_text)[:400]}",
                        )
                    except Exception as e:  # noqa: BLE001
                        logger.warning("exit gate: audit write failed: %s", e)
                    self._record_exit_refusal(
                        symbol=symbol, run_id=run_id, action=act,
                        code=CODE_UNRECOGNIZED_TRIGGER, dropped=True,
                        detail=f"{act}: {str(reason_text)[:400]}",
                        layer="hard_trigger",
                    )
                    continue
                if trigger_judgment == "uncertain":
                    logger.error(
                        "Position reviewer: hard-trigger recogniser raised "
                        "on %s %s — failing OPEN on that gate (agent "
                        "application of the 2026-08-27 dead-model posture, "
                        "not a new owner ratification). Reason was: %r",
                        act, symbol, str(reason_text)[:200],
                    )
                    self._record_exit_refusal(
                        symbol=symbol, run_id=run_id, action=act,
                        code=CODE_HARD_TRIGGER_UNCERTAIN, dropped=False,
                        detail=f"{act}: {str(reason_text)[:400]}",
                        layer="hard_trigger",
                    )
                reason_text = (
                    reason_text if isinstance(reason_text, str) else str(reason_text or "")
                )

            # 2026-09-11 — and now: is the named trigger actually TRUE?
            #
            # The gate immediately above only proves the reason SAYS the
            # words. Until this landed that was the whole of the midday /
            # close check: "regime shift to risk-off; correlation breach
            # across the book" executed a SELL on a structurally protected
            # position on the strength of the phrasing, with no part of the
            # system ever asking whether a regime shift had happened. The
            # deterministic answer to that question already existed —
            # `exit_guard.holding_discipline_claim_check` — but was wired
            # only to the morning Portfolio-Manager path in
            # `pipeline_stages.RiskStage`. Same function here, same
            # semantics, assembled by `_holding_discipline_check_for_exit`.
            #
            # PROVABLY FALSE drops the exit (the morning path's own
            # response, mirroring the existing gates on this loop).
            # UNVERIFIABLE is recorded and ALLOWED THROUGH, unchanged from
            # the morning path and deliberately: absence of proof is not
            # proof, and refusing an exit on a claim we merely cannot check
            # would trap the desk in a losing position — a far worse
            # failure than the one being fixed. An infrastructure failure
            # inside the check fails OPEN for the same reason, matching
            # `_risk_review_exits`' disclosed posture on this path.
            if act in ("SELL", "REDUCE", "COVER"):
                if hd_position_history is None:
                    try:
                        hd_position_history = self._build_position_history(positions)
                    except Exception as e:  # noqa: BLE001
                        logger.warning(
                            "holding discipline: entry-context lookup failed "
                            "(%s) — protection is read without it this run", e,
                        )
                        hd_position_history = {}
                try:
                    hd_check = self._holding_discipline_check_for_exit(
                        symbol=symbol, action=act, reason=reason_text,
                        positions=positions, run_id=run_id,
                        position_history=hd_position_history,
                        # The STRUCTURED trigger, so the fact-check reads
                        # the claim from the field the seat filled rather
                        # than guessing it from the sentence. This is what
                        # makes the 2026-09-16 "adverse news" shape
                        # adjudicable at all.
                        exit_trigger=action_item.get("exit_trigger"),
                    )
                except Exception as e:  # noqa: BLE001
                    logger.warning(
                        "holding discipline: check failed for %s %s (%s) — "
                        "the claim goes unverified rather than blocking the "
                        "exit", act, symbol, e,
                    )
                    hd_check = None
                if hd_check is not None and hd_check.blocks:
                    logger.warning(
                        "Position reviewer: blocking %s %s — holding-"
                        "discipline claim PROVEN FALSE. %s",
                        act, symbol, hd_check.finding,
                    )
                    try:
                        self.db.record_intraday_evaluation(
                            symbol=symbol, run_id=run_id,
                            status="exit_blocked_holding_discipline_claim_false",
                            detail=(hd_check.finding or "")[:500],
                        )
                    except Exception as e:  # noqa: BLE001
                        logger.warning(
                            "holding discipline: audit write failed: %s", e,
                        )
                    from src.risk.exit_refusal import CODE_HOLDING_DISCIPLINE_FALSE
                    self._record_exit_refusal(
                        symbol=symbol, run_id=run_id, action=act,
                        code=CODE_HOLDING_DISCIPLINE_FALSE, dropped=True,
                        detail=(hd_check.finding or "")[:400],
                        layer="holding_discipline",
                    )
                    continue
                if hd_check is not None and hd_check.verdict == "unverifiable":
                    # Audit trail only. NOT a block — see above.
                    logger.warning("Holding discipline: %s", hd_check.finding)
                    try:
                        self.db.record_intraday_evaluation(
                            symbol=symbol, run_id=run_id,
                            status="holding_discipline_claim_unverified",
                            detail=(hd_check.finding or "")[:500],
                        )
                    except Exception as e:  # noqa: BLE001
                        logger.warning(
                            "holding discipline: audit write failed: %s", e,
                        )

            # The same-day-trim gate that used to sit here is GONE, not
            # relaxed: it read `symbol in already_trimmed and not
            # _reason_cites_hard_trigger(...)`, and a completed unnamed-
            # trigger judgment above now `continue`s on every untriggered
            # SELL/REDUCE before control ever reaches it. (A recogniser that
            # cannot run fails OPEN instead — that is uncertainty, not a
            # completed "no".) Leaving the old gate in place would have been
            # dead code wearing the costume of a safety check, which is worse
            # than no check at all.
            #
            # That residual gap — hard triggers exempt, so a symbol trimmed
            # at midday on "bearish earnings" could be trimmed again at close
            # on the SAME "bearish earnings" — is CLOSED below by the
            # per-event dedup it called for (board item 74, 2026-09-26). The
            # warning here stays: a second sell-side action is still worth
            # seeing in the log even when it is legitimate.
            if act in ("SELL", "REDUCE", "COVER") and symbol in already_trimmed:
                logger.warning(
                    "Position reviewer: %s %s is a SECOND sell-side action "
                    "today. Reason: %r",
                    act, symbol, (action_item.get("reason") or "")[:160],
                )
            # Board item 74 — the RESIDUAL GAP above, now closed. The line is
            # the RECORD the seat cites, never a cooldown or a score: same
            # trigger + same cited record = spent, refuse; a different record
            # = new information, execute and say so.
            spent = spent_trigger_check(
                action=act, symbol=symbol,
                trigger=action_item.get("exit_trigger"),
                evidence=action_item.get("trigger_evidence"),
                acted_today=acted_today,
            )
            if spent.verdict == "uncertain":
                logger.error(
                    "Spent-trigger check: today's acted-trigger record is "
                    "unreadable — failing OPEN on %s %s. %s",
                    act, symbol, spent.detail,
                )
            elif spent.blocks:
                logger.warning(
                    "Position reviewer: REFUSING %s %s — the trigger is "
                    "SPENT. %s", act, symbol, spent.detail,
                )
                try:
                    self.db.record_intraday_evaluation(
                        symbol=symbol, run_id=run_id,
                        status="exit_blocked_trigger_already_spent",
                        detail=spent.detail[:500],
                    )
                except Exception as e:  # noqa: BLE001
                    logger.warning("spent trigger: audit write failed: %s", e)
                self._record_exit_refusal(
                    symbol=symbol, run_id=run_id, action=act,
                    code=spent.code, dropped=True,
                    detail=spent.detail[:400], layer=SPENT_LAYER,
                )
                continue
            elif spent.verdict in ("new_evidence", "unidentifiable"):
                # Allowed, NOT silent. `new_evidence` is the "genuinely worse
                # reading" this item preserves; `unidentifiable` is a second
                # cut naming no record, which this layer cannot prove is the
                # same one and which the upstream substantiation layer
                # already lets through — the two must not disagree about the
                # identical input. Both are recorded for the evening grade.
                logger.warning(
                    "Position reviewer: %s %s is a second cut on the same "
                    "trigger — allowed (%s). %s",
                    act, symbol, spent.verdict, spent.detail,
                )
                self._record_exit_refusal(
                    symbol=symbol, run_id=run_id, action=act,
                    code=spent.code, dropped=False,
                    detail=spent.detail[:400], layer=SPENT_LAYER,
                )
            existing = [p for p in positions if p.symbol == symbol]
            # COVER only matches a held SHORT (qty < 0); SELL / REDUCE /
            # TRAIL_STOP only match a held LONG (qty > 0) — same "the order
            # must match the held side" rule ExecutionStage's COVER loop
            # enforces for the PM's decision path (mirrors it here, not a
            # new rule). A COVER proposed against a long/flat position, or
            # a SELL/REDUCE/TRAIL_STOP proposed against a short, is dropped.
            if act == "COVER":
                if not existing or existing[0].qty >= 0:
                    logger.warning(
                        "Midday: skipping COVER %s — no matching short "
                        "position", symbol,
                    )
                    continue
            elif not existing or existing[0].qty <= 0:
                logger.warning("Midday: skipping %s %s — no matching position",
                               act, symbol)
                continue
            prot = None
            try:
                if act == "TRAIL_STOP":
                    try:
                        from src.execution.scale_in import pending_protection_symbols
                        if symbol in pending_protection_symbols(self.db):
                            logger.info(
                                "Midday: TRAIL_STOP %s skipped — a "
                                "protection-restore WAL row is in flight",
                                symbol,
                            )
                            continue
                    except Exception:  # noqa: BLE001
                        pass
                    try:
                        new_stop = float(action_item.get("new_stop_price") or 0)
                    except (TypeError, ValueError):
                        new_stop = 0.0
                    if new_stop <= 0:
                        logger.warning(
                            "Midday: TRAIL_STOP %s skipped — missing/invalid new_stop_price",
                            symbol,
                        )
                        continue
                    if new_stop >= existing[0].current_price:
                        logger.warning(
                            "Midday: TRAIL_STOP %s skipped — new_stop $%.2f >= current $%.2f",
                            symbol, new_stop, existing[0].current_price,
                        )
                        continue
                    # Minimum-ratchet floor: a raise must land on a
                    # DIFFERENT stop price than the one resting at the
                    # broker -- at least one venue tick above it. Until
                    # 2026-10-02 this was 2% (`MIN_RATCHET_PCT`), a picked
                    # churn-appetite number that existed only because a
                    # stop move meant cancel-then-resubmit. The in-place
                    # amend is on main, so the floor is now read off the
                    # instrument: see `src.risk.trailing.min_ratchet_floor`
                    # and the ledger entry `src.risk.trailing.MIN_RATCHET_TICKS`.
                    # A rejection here keeps the existing (valid, looser) stop
                    # in place: protection is never removed, only left as-is.
                    # Old stop is broker truth; if it is missing/unreadable the
                    # floor cannot be computed, so this establishes protection
                    # rather than blocking it (the RC1 clamps still apply).
                    from src.execution.stop_read import read_stop
                    _old_read = read_stop(self.broker, symbol, db=self.db,
                                          context="midday min-ratchet floor")
                    old_stop = _old_read.price if _old_read.found else None
                    if old_stop is not None and old_stop > 0:
                        from src.risk.trailing import (
                            min_ratchet_floor, venue_tick,
                        )
                        min_new_stop = min_ratchet_floor(old_stop)
                        if new_stop < min_new_stop - venue_tick(old_stop) / 2.0:
                            logger.warning(
                                "Midday: TRAIL_STOP %s skipped — new_stop "
                                "$%.4f does not clear the live stop $%.4f by "
                                "one venue tick (floor $%.4f); it is the same "
                                "stop after quantization. Old stop kept.",
                                symbol, new_stop, old_stop, min_new_stop,
                            )
                            continue
                    # WIDTH IS ANSWERED BY ADJUSTING THE STOP, NEVER BY
                    # PLACING NONE (board item 185, 2026-09-30; board item
                    # 80's ruling; board item 56 route (c)'s shape).
                    #
                    # What used to be here. A flat refusal: a proposed stop
                    # under 50% of current price was dropped as a model
                    # typo, and the routine moved on -- placing nothing.
                    # Nothing fixed the 50%; it was picked, and the
                    # universe screen then DERIVED its volatility ceiling
                    # from it, so each end of the pair was justified only
                    # by the other.
                    #
                    # Why a refusal is the wrong answer here whatever the
                    # bound is. This check can only bind where the live
                    # broker stop was unreadable or absent -- where the
                    # stop IS readable the min-ratchet floor above has
                    # already refused anything that does not clear it, so
                    # a typo far below price is long gone. "The live stop
                    # could not be read" is precisely the case where the
                    # position may be carrying NO protection at all, and a
                    # refusal there ends the loop with the name still
                    # naked. That is the owner's board-item-80 failure in
                    # a different costume -- its ruling, quoted at
                    # `portfolio_constructor.
                    # STOP_REFUSAL_NO_STOP_NO_VOLATILITY`, is that "a
                    # missing volatility reading is never a reason to skip
                    # protection", and the general shape of it is that the
                    # desk does not answer a stop it dislikes by placing
                    # nothing. It is also the same ruling board item 56
                    # route (c) made about stop WIDTH specifically: a wide
                    # stop is answered by adjusting the trade (there, by
                    # sizing down), never by a refusal. There is no sizing
                    # lever on this path, so the adjustment available is
                    # the stop price itself.
                    #
                    # What happens instead. A proposal further below price
                    # than any stop this desk's own rules can produce is
                    # CLAMPED to that widest legitimate stop and PLACED.
                    # The bound is read off the instrument, not chosen:
                    # the widest multiple `PortfolioConstructor.
                    # _stop_atr_multiple` can actually return (the base
                    # `min_stop_atr_multiple` times the largest setup and
                    # regime scalers, 3.00 at today's settings) against
                    # THIS name's live ATR14. The clamped price is below
                    # the 1.25 x ATR noise floor by construction, so the
                    # noise-band clamp below cannot then reject it. If the
                    # name is so volatile that even that widest stop lands
                    # at or below zero, there is no legitimate stop to
                    # clamp to, so the proposal stands -- the same
                    # "something beats nothing" direction, and the case
                    # the universe screen's ceiling exists to keep out.
                    #
                    # The desk, not the model, chose that price, so it is
                    # recorded per-symbol and durably rather than only
                    # logged (`dropped=False` -- nothing was dropped).
                    #
                    # `atr` is fetched once here and reused by the
                    # noise-band clamp below. Unreadable ATR degrades to no
                    # clamp, the same rule the noise band already used;
                    # the proposal then stands, because placing the model's
                    # stop still beats placing none.
                    atr = self._atr_for_symbol(symbol)
                    if (old_stop is None or old_stop <= 0) and atr is not None:
                        from src.portfolio_constructor import (
                            widest_reachable_stop_atr_multiple,
                        )
                        _cfg = self.portfolio_constructor.cfg
                        widest = widest_reachable_stop_atr_multiple(
                            _cfg.min_stop_atr_multiple,
                            _cfg.stop_atr_setup_scale,
                            _cfg.stop_atr_regime_scale,
                        )
                        widest_stop = existing[0].current_price - widest * atr
                        if widest_stop > 0 and new_stop < widest_stop:
                            from src.risk.exit_refusal import (
                                CODE_TRAIL_CLAMPED_TO_WIDEST,
                            )
                            detail = (
                                f"TRAIL_STOP {symbol}: no live stop was "
                                f"readable, and the proposed ${new_stop:,.2f} "
                                f"sits further below the "
                                f"${existing[0].current_price:,.2f} price than "
                                f"the widest stop this desk can place "
                                f"({widest:.2f} x ATR14 ${atr:,.2f} = "
                                f"${widest_stop:,.2f}). Read as a model typo "
                                f"and CLAMPED to ${widest_stop:,.2f} -- the "
                                f"position may be unprotected, so a stop is "
                                f"placed, never skipped (board item 80)."
                            )
                            logger.warning("Midday: %s", detail)
                            self._record_exit_refusal(
                                symbol=symbol, run_id=run_id,
                                action=act,
                                code=CODE_TRAIL_CLAMPED_TO_WIDEST,
                                dropped=False, detail=detail[:400],
                                layer="midday_trail_width",
                            )
                            new_stop = widest_stop
                    # RC1 exit-quality clamps (2026-07-16 forensics: 5 trail
                    # fills missed avg +30.7% post-exit; LLY was whipsawed
                    # twice identically). A hard-trigger citation in the
                    # reason bypasses both — mirroring the SELL/REDUCE gate.
                    if not _reason_cites_hard_trigger(action_item.get("reason", "")):
                        # (a) Ratchet cooldown: at most one accepted tighten
                        # per 4-calendar-day window per symbol (~2-4 trading
                        # sessions depending on weekday).
                        if self._trail_tightened_recently(symbol):
                            logger.warning(
                                "Midday: TRAIL_STOP %s skipped — a trail was "
                                "already tightened within the last 4 calendar "
                                "days (~2-4 trading sessions depending on "
                                "weekday; ratchet cooldown; cite a hard "
                                "trigger to bypass)", symbol,
                            )
                            continue
                        # (b) Noise-band clamp: a stop inside 1.25×ATR14 of
                        # the current price sits inside one day's normal
                        # range — it converts routine volatility into a
                        # realized exit. Keep the old stop instead.
                        # `atr` was read above for the typo guard; the
                        # fetch is not repeated.
                        if atr is not None:
                            noise_floor = existing[0].current_price - 1.25 * atr
                            if new_stop > noise_floor:
                                logger.warning(
                                    "Midday: TRAIL_STOP %s skipped — new_stop "
                                    "$%.2f is inside the 1.25×ATR noise band "
                                    "(floor $%.2f, ATR14 $%.2f); routine "
                                    "volatility would fill it. Old stop kept; "
                                    "cite a hard trigger to bypass.",
                                    symbol, new_stop, noise_floor, atr,
                                )
                                continue
                    from src.execution.stop_records import (
                        accepted_stop_order, replace_stop_and_record,
                    )
                    order = replace_stop_and_record(
                        self.broker, self.db, symbol, new_stop,
                    )
                    if order and not (
                        isinstance(order, dict) and not accepted_stop_order(order)
                    ):
                        if isinstance(order, dict):
                            order.setdefault("action", "TRAIL_STOP")  # audit F5
                        orders.append(order)
                        self.db.insert_trade(
                            symbol=symbol, action="TRAIL_STOP",
                            qty=existing[0].qty, price=new_stop,
                            reasoning=action_item.get("reason", "midday trailing stop"),
                            run_id=run_id,
                            stop_loss=new_stop,
                            broker_order_id=order.get("id"),
                            fill_status="submitted",
                        )
                        logger.info(
                            "Midday action: TRAIL_STOP %s → $%.2f — %s",
                            symbol, new_stop, action_item.get("reason"),
                        )
                    continue

                if act == "COVER":
                    # COVER is always a FULL close here — see the docstring
                    # for why (no allocation fraction on this schema).
                    # `existing[0].qty` is the NEGATIVE broker qty; every
                    # downstream qty (WAL specs, fill_qty, insert_trade) is
                    # an absolute magnitude, never the signed qty.
                    qty = self._full_sell_qty(abs(existing[0].qty))
                    if qty is None:
                        continue
                    # Buy-to-cover needs headroom ABOVE the reference to
                    # fill on the way up — the mirror of the SELL limit
                    # sitting 0.5% BELOW (same reasoning as
                    # _EMERGENCY_LIMIT_CUSHION_PCT; matches ExecutionStage's
                    # COVER loop in src/pipeline_stages.py).
                    order_limit = round(existing[0].current_price * 1.005, 2)
                    position_qty = abs(existing[0].qty)
                    close_side = "buy"
                else:
                    if act == "REDUCE":
                        qty = self._reduce_sell_qty(existing[0].qty)
                    else:
                        qty = self._full_sell_qty(existing[0].qty)
                    if qty is None:
                        continue
                    order_limit = round(existing[0].current_price * 0.995, 2)
                    position_qty = existing[0].qty
                    close_side = "sell"
                # audit F1 review #1: snapshot -> persist WAL -> cancel.
                sale = self._submit_protected_sell(
                    symbol=symbol, qty=qty, limit_price=order_limit,
                    reference_price=existing[0].current_price,
                    position_qty_before_sell=position_qty, label=act,
                    side=close_side,
                )
                if sale is None:
                    continue
                order, prot = sale
                orders.append(order)
                self.db.insert_trade(
                    symbol=symbol, action=act, qty=qty,
                    price=existing[0].current_price,
                    reasoning=action_item.get("reason", "midday review"),
                    run_id=run_id,
                    broker_order_id=order.get("id"),
                    fill_status="submitted",
                )
                # Board item 74 — record what authorised this cut. Written
                # at SUBMIT, the only moment the trigger and its evidence
                # are in hand; it does not by itself spend the trigger. The
                # reader believes this row only once the order is known to
                # have executed (see `keep_executed_acted_triggers`), so a
                # rejected or unfilled cut spends nothing. The in-process
                # list is appended too: a later action in THIS same pass
                # sees it without a second DB read, and inside one pass the
                # order is as live as it will get.
                _acted = acted_trigger_payload(
                    symbol=symbol, trigger=action_item.get("exit_trigger"),
                    evidence=action_item.get("trigger_evidence"),
                    action=act, run_id=run_id,
                    broker_order_id=str(order.get("id") or ""),
                )
                if _acted is not None:
                    try:
                        self.db.record_acted_exit_trigger(
                            run_id=run_id, payload_json=_acted.to_json(),
                            symbol=_acted.symbol,
                        )
                    except Exception as e:  # noqa: BLE001
                        logger.warning(
                            "spent trigger: could not record the acted "
                            "trigger for %s (%s) — a second cut on this "
                            "same record today would not be caught",
                            symbol, e,
                        )
                    if acted_today is not None:
                        acted_today.append(_acted)
                logger.info(
                    "Midday action: %s %s %s — %s",
                    act, self._format_qty(qty),
                    symbol, action_item.get("reason"),
                )
            except Exception as e:
                logger.error("Midday order failed for %s: %s", symbol, e)
            # Rebuild THIS symbol's stop coverage on its actual fill before
            # the loop cancels the next symbol's stops — the same per-name
            # discipline the de-lever loops got (docs/WORK.md item 111).
            # Finalizing the batch once after the loop left every earlier
            # symbol with no protective stop while later symbols were
            # cancelled, submitted and waited on. Runs even when the ledger
            # write above raised: the stops are off and the order is live.
            # Which names exit, how much and at what limit are unchanged.
            if prot is not None:
                self._finalize_pending_protections(
                    [prot], context="Midday reviewer",
                )
        return orders
