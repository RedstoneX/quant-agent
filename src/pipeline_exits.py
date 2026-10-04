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
from src.sentinel.guarded_exit import record_exit_guard
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
            record_exit_guard(self, "structural.board_reason_write")
        except Exception as e:  # noqa: BLE001
            record_exit_guard(self, "structural.board_reason_write", e, logger, symbol=symbol_u, effect="Telegram send still attempted")

        # Telegram: the same standalone owner-alert path the holding-discipline
        # block uses. `send_owner_alert` does NOT raise on a failed send — it
        # RETURNS False — so a surface only counts as reached when the return is
        # truthy (and, as a backstop, when it does not raise).
        try:
            from src import notifier as _notifier

            ok = _notifier.send_owner_alert(message, symbols=[symbol_u])
            any_surface_ok |= bool(ok)
            record_exit_guard(self, "structural.owner_alert_send")
        except Exception as e:  # noqa: BLE001
            record_exit_guard(self, "structural.owner_alert_send", e, logger, symbol=symbol_u, effect="owner not told; dedup slot stays free")

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

    def _apply_deterministic_trails(self, positions, *, run_id: str) -> list[dict]:
        """Raise stops arithmetically, before the LLM is asked anything — 3.7.

        Trailing is arithmetic. Running it here means the reviewer's
        discretionary `TRAIL_STOP` becomes an override for the unusual case
        rather than the only mechanism, and the stop a winner rides up behind
        no longer depends on a model remembering to propose it.

        Every proposal is bounded by `src/risk/trailing.py`: ratchet upward
        only, a minimum move worth an order, and never inside one ordinary
        day's range. Returns the broker orders placed.

        Every evaluation also names WHY its position did or did not trail,
        and that reason is written to `specialist_evidence`
        (`kind='trail_state'`) whenever it differs from the last one on file
        for that stock — so a stop that has never trailed has a findable
        reason, without a row per stock per tick. Recording only: nothing
        here reads the record back to decide anything but whether to write.
        """
        from src.execution.stop_records import (
            recorded_initial_stop, replace_stop_and_record,
        )
        from src.execution.exit_path_records import (
            last_trail_states, record_trail_code_census,
            record_trail_state_if_changed,
        )
        from src.risk.trailing import TRAIL_CODE_TRAILED, evaluate_trailing_stop

        orders: list[dict] = []
        # Item 196: the per-stock record above is deduplicated by code, so
        # it cannot answer how OFTEN an outcome occurs. This counts every
        # evaluation this run, written once at the end of the pass.
        from collections import Counter as _Counter
        code_census: _Counter = _Counter()
        last_codes = last_trail_states(
            self.db, [getattr(p, "symbol", "") for p in positions],
        )

        def _note(symbol: str, code: str, detail: str = "", **facts) -> None:
            code_census[str(code)] += 1
            record_trail_state_if_changed(
                self.db, last_codes, run_id=run_id, symbol=symbol,
                code=code, detail=detail, **facts,
            )

        try:
            from src.execution.scale_in import pending_protection_symbols
            pending_syms = pending_protection_symbols(self.db)
            record_exit_guard(self, "trail.pending_protection_symbols")
        except Exception as exc:  # noqa: BLE001
            record_exit_guard(self, "trail.pending_protection_symbols", exc, logger, effect="treated as no in-flight restore rows")
            pending_syms = set()
        for position in positions:
            symbol = position.symbol
            if symbol in pending_syms:
                logger.info(
                    "trail: skipping %s — a protection-restore WAL row is "
                    "in flight (scale-in or sell); replacing the stop now "
                    "would race the cancel/rearm sequence",
                    symbol,
                )
                _note(symbol, "protection_restore_in_flight")
                continue
            try:
                buy = self.db.get_symbol_last_buy(symbol)
                record_exit_guard(self, "trail.last_buy_lookup")
            except Exception as e:  # noqa: BLE001
                record_exit_guard(self, "trail.last_buy_lookup", e, logger, symbol=symbol, effect="symbol skipped this pass")
                _note(symbol, "opening_row_lookup_failed", str(e))
                continue
            if not buy:
                _note(symbol, "no_opening_buy_row")
                continue

            # Every field read off `buy` below is pinned AT ENTRY — the
            # reference target (`take_profit`), the denominator of R
            # (`initial_stop_loss`, via `recorded_initial_stop`), the
            # setup label and the measured breakout verdict — and a
            # scale-in writes a SECOND opening row. Reading them off the
            # newest add let the reference target sit above current price
            # and measured R from a stop this trade never opened with.
            # Item 195 fixed only the bar window; these read the same
            # wrong row. `get_position_open_row` resolves the chain by
            # `position_id` and returns None when it cannot, so an
            # unchainable or legacy row keeps exactly today's behaviour.
            try:
                _open_row = self.db.get_position_open_row(buy)
                # Same `isinstance` discipline the bar-window lookup above
                # uses: anything that is not a real row leaves `buy` alone.
                if isinstance(_open_row, dict):
                    buy = _open_row
                record_exit_guard(self, "trail.position_open_row")
            except Exception as e:  # noqa: BLE001
                record_exit_guard(self, "trail.position_open_row", e, logger, symbol=symbol, effect="falls back to last opening row")
            from src.execution.stop_read import read_stop, repair_for
            _stop_read = read_stop(self.broker, symbol, db=self.db,
                                   context="deterministic trail", establish=repair_for(self._repair_stop_coverage, position))
            if _stop_read.unreadable:
                _note(symbol, "live_stop_lookup_failed", _stop_read.reason)
                continue
            current_stop = None if _stop_read.absent else _stop_read.price

            # Only bars SINCE ENTRY matter: a swing low from before the
            # position existed is not a level this trade ever defended.
            #
            # "Since entry" means since the POSITION opened, not since the
            # most recent add. `get_symbol_last_buy` returns the LATEST
            # opening row, so slicing from it made a scale-in erase the
            # trade's whole bar history — while the entry PRICE handed to
            # the trail below is `position.avg_entry`, blended across every
            # add. The window and the price disagreed by construction.
            #
            # Measured 2026-09-30 against the live DB: the structural pivot
            # has produced ZERO of the 9 deterministic stops ever placed
            # (all 9 came from the chandelier or the breakeven ratchet),
            # and in all 11 recorded `no_structure_and_no_usable_chandelier`
            # refusals the window held 0-6 bars against the 7 that
            # `src/risk/trailing.py::_swing_lows` needs before it can
            # confirm a single pivot. MRVL on 2026-09-23 is the clearest
            # case: a position opened 2026-09-17 was evaluated with zero
            # bars because it had been added to that morning.
            #
            # This is NOT a risk-free change, and an earlier version of
            # this comment claimed it was. A longer window can only RAISE
            # `highest`, which raises `chandelier = highest - 3*ATR`; a
            # higher candidate can rise THROUGH the noise floor, and
            # `evaluate_trailing_stop` then refuses OUTRIGHT
            # (`inside_noise_band`) rather than falling back to a lower
            # candidate the shorter window would have accepted. Worked
            # case: price 100, ATR 4, live stop 90. A window whose high is
            # 106 proposes 94 and the stop tightens 90 -> 94; a longer
            # window that sees a pre-add high of 108 proposes 96, which is
            # above the 95 noise floor, so nothing is placed and the stop
            # stays at 90. The wider window LOSES a tighten the narrower
            # one took.
            #
            # The justification is therefore consistency, not safety: the
            # old window disagreed BY CONSTRUCTION with the entry price the
            # same call uses (`position.avg_entry`, blended across every
            # add). Measured 2026-09-30 against all 21 recorded refusals,
            # the exposure is currently zero — see `_swing_lows` in
            # `src/risk/trailing.py` for that measurement. No new constant.
            bars = []
            try:
                all_bars = self.market.get_ohlcv(symbol, 120) or []
                try:
                    opened_ts = self.db.get_position_open_timestamp(buy)
                    record_exit_guard(self, "trail.position_open_timestamp")
                except Exception as e:  # noqa: BLE001
                    record_exit_guard(self, "trail.position_open_timestamp", e, logger, symbol=symbol, effect="falls back to opening row date")
                    opened_ts = None
                if not isinstance(opened_ts, str):
                    opened_ts = None
                entry_ts = opened_ts or (buy or {}).get("timestamp") or ""
                entry_day = entry_ts[:10]
                bars = [
                    b for b in all_bars
                    if not entry_day or str(getattr(b, "date", ""))[:10] >= entry_day
                ]
                record_exit_guard(self, "trail.bar_fetch")
            except Exception as e:  # noqa: BLE001
                record_exit_guard(self, "trail.bar_fetch", e, logger, symbol=symbol, effect="trail evaluated without bars")

            # Item 82: the MEASURED half of the breakout verdict, pinned at
            # entry alongside `setup_type` (stored 0/1/NULL). Present → this
            # path reaches construction's OWN verdict so a measured breakout
            # the analyst mislabelled "range" is trailed as Type B, not Type
            # A; NULL (legacy row, or a pre-item-82 entry) → `is_trend_trade`
            # inside `evaluate_trailing_stop` falls back to the label alone,
            # exactly the pre-item-82 `!= "breakout"` behaviour. Read the
            # SAME way the pace/progress path does (#652).
            _sc_raw = (buy or {}).get("structural_ceiling")
            structural_ceiling = None if _sc_raw is None else bool(_sc_raw)

            evaluation = evaluate_trailing_stop(
                symbol=symbol,
                setup_type=(buy or {}).get("setup_type"),
                structural_ceiling=structural_ceiling,
                entry=position.avg_entry,
                current_price=position.current_price,
                current_stop=current_stop,
                # THE ENTRY TARGET, never the live `take_profit` (item
                # 194, 2026-10-01). The comment above says every field read
                # off `buy` is pinned at entry; `take_profit` stopped being
                # so the moment `update_open_take_profit` existed. That
                # mattered once the re-derivation swept the whole book: a
                # target revised DOWN crosses a range trade from the
                # below-target breakeven/+2R ratchets into the structural
                # trail, the trail only ever ratchets toward price, and so
                # restoring the target on the next session does NOT give
                # the stop back — the tightening accumulated instead of
                # cancelling.
                #
                # THIS MOVES PROTECTION IN BOTH DIRECTIONS AND BOTH ARE
                # INTENDED. It removes a ratchet that could never be given
                # back; it also LOOSENS the boundary case, because where a
                # revised target sits below the entry target, a fall to
                # just under the old boundary used to hand the stop to the
                # structural trail and now leaves it in the earlier
                # ratchets. Less tightening there is a real loosening of
                # future protection, accepted because the ratchet it
                # replaces was irreversible and this one is not.
                #
                # "PINNED" IS THE INTENT OF THE COLUMN, NOT A VERIFIED
                # PROPERTY OF EVERY ROW: the `initial_take_profit`
                # migration backfilled it FROM `take_profit` for every
                # legacy row carrying a target, so a row revised before
                # that migration ran was backfilled with an already-revised
                # number. Whether any such row exists is UNVERIFIED. The
                # null fallback below is safe either way — it only applies
                # to rows the migration left empty.
                reference_target=(
                    (buy or {}).get("initial_take_profit")
                    or (buy or {}).get("take_profit")
                ),
                bars=bars,
                atr=self._atr_for_symbol(symbol),
                # Shorts-safe (Stage 2): `qty` supplies only the side so a
                # short's trail mirrors instead of running the long formula
                # backwards. `get_symbol_last_buy` above only ever returns a
                # BUY row, so a short is filtered out before this point
                # regardless — this is forward-compatible plumbing, not a
                # behaviour change on today's long-only book.
                qty=position.qty,
                # Fix #3 (2026-09-04 audit): the ENTRY stop, never the live
                # one. `initial_stop_loss` is frozen at insert / first
                # write-back; `stop_loss` itself is the live recorded level
                # after a trail. Powers the Type A +1R breakeven ratchet.
                initial_stop=recorded_initial_stop(buy),
            )
            proposal = evaluation.proposal
            if proposal is None:
                _note(
                    symbol, evaluation.code,
                    # Item 212 follow-up: on a range name the R-ratchet leg
                    # supplies the code, so the STRUCTURAL leg's own refusal
                    # reason would otherwise never be recorded again. It is
                    # both a recorded field and part of the dedupe identity.
                    structural_code=evaluation.structural_code,
                    current_stop=current_stop,
                    current_price=position.current_price,
                    entry=position.avg_entry,
                    setup_type=(buy or {}).get("setup_type"),
                )
                continue
            code_census[TRAIL_CODE_TRAILED] += 1
            logger.info("Deterministic trail: %s", proposal.reason)
            try:
                from src.execution.stop_records import accepted_stop_order
                order = replace_stop_and_record(
                    self.broker, self.db, symbol, proposal.new_stop,
                )
                record_exit_guard(self, "trail.replace_stop")
            except Exception as e:  # noqa: BLE001
                record_exit_guard(self, "trail.replace_stop", e, logger, symbol=symbol, effect="old stop remains in force")
                _note(
                    symbol, "replace_raised", str(e),
                    proposed_stop=proposal.new_stop, current_stop=current_stop,
                )
                continue
            if isinstance(order, dict) and order.get("legs"):
                # Item 201: the trailing path now amends every resting leg in
                # place, so the per-leg outcome is recorded here too. This is
                # the evidence that settles whether a fractional position's two
                # hybrid legs both amend — the ex-dividend shift alone would
                # never produce it (0 of 80 production trades between
                # 2026-09-02 and 2026-09-30 were ex-dividend shifts).
                from src.execution.exit_path_records import record_stop_shift_legs
                _legs = order.get("legs") or []
                _ok = [l for l in _legs if l.get("outcome") == "amended"]
                # `amend_status` is the AMEND's own verdict. The broker status
                # on a live replacement is "new"/"accepted"/..., so reading
                # that would call an ordinary success a failure.
                _astatus = str(order.get("amend_status") or (
                    "accepted" if len(_ok) == len(_legs) else "partial"))
                record_stop_shift_legs(
                    self.db, symbol=symbol, amount=0.0, mode="trail_amend",
                    status=_astatus, shifted=len(_ok), total=len(_legs),
                    legs=_legs, run_id=run_id,
                )
                if _astatus in ("partial", "refused", "unknown", "naked", "market_closed"):
                    # Telegram is muted, so this row and this alert are the
                    # whole evidence that a leg did not move.
                    try:
                        from src.notifier import send_owner_alert
                        from src.execution.exit_path_records import (
                            stop_shift_incomplete_text,
                        )
                        send_owner_alert(
                            stop_shift_incomplete_text(
                                symbol, _astatus, len(_ok), len(_legs)),
                            symbols=[symbol],
                        )
                        record_exit_guard(self, "trail.owner_alert")
                    except Exception as e:  # noqa: BLE001
                        record_exit_guard(self, "trail.owner_alert", e, logger, symbol=symbol, effect="owner not told of incomplete stop shift")
            if not order or (
                isinstance(order, dict) and not accepted_stop_order(order)
            ):
                _detail = str((order or {}).get("status") or "") if isinstance(order, dict) else ""
                if isinstance(order, dict) and order.get("legs"):
                    # `record_trail_state_if_changed` writes nothing when the
                    # code repeats, and a bare status names no leg, no level
                    # and no order id. Spell the outcome out here as well.
                    _detail = "; ".join(
                        [_detail or "amend did not fully land"]
                        + [
                            f"leg {l.get('id')} qty {l.get('qty')} "
                            f"{l.get('old_stop')}->{l.get('new_stop')} "
                            f"{l.get('outcome')}"
                            + (f" (new id {l.get('new_id')})" if l.get("new_id") else "")
                            + (f": {l.get('detail')}" if l.get("detail") else "")
                            for l in (order.get("legs") or [])
                        ]
                    )
                _note(
                    symbol, "replace_not_accepted", _detail,
                    proposed_stop=proposal.new_stop, current_stop=current_stop,
                )
                continue
            _note(
                symbol, TRAIL_CODE_TRAILED, proposal.reason,
                # Carried on the SUCCESS branch too: the case this field
                # exists for is the R-ratchet leg winning, which is a
                # trailed row, not a refusal row.
                structural_code=evaluation.structural_code,
                proposed_stop=proposal.new_stop, current_stop=current_stop,
            )
            if isinstance(order, dict):
                order.setdefault("action", "TRAIL_STOP")
            orders.append(order)
            try:
                self.db.insert_trade(
                    symbol=symbol, action="TRAIL_STOP", qty=position.qty,
                    price=proposal.new_stop, reasoning=proposal.reason,
                    run_id=run_id,
                    stop_loss=proposal.new_stop,
                )
                record_exit_guard(self, "trail.trade_row_write")
            except Exception as e:  # noqa: BLE001
                record_exit_guard(self, "trail.trade_row_write", e, logger, symbol=symbol, effect="trail not recorded in trade rows")

        record_trail_code_census(
            self.db, run_id=run_id, counts=dict(code_census),
        )
        return orders

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

    def _risk_review_exits(
        self, review, positions, *, run_id: str, total_value: float,
        macro_summary: dict | None = None, position_facts: dict | None = None,
        news_intel=None, earnings_analyses: list | None = None,
        cash: float | None = None, reserve_balance: float = 0.0,
        recent_performance: dict | None = None,
    ):
        """Put the reviewer's exits in front of the AI Risk Manager — Phase 3.4.

        `AGENTS.md` states the chain as `Specialists -> Portfolio Manager ->
        AI Risk -> deterministic Python -> broker`, **for exits as well as
        entries**. Until this landed, `run_position_review` called only
        `position_reviewer` and then executed, so the entire sell side skipped
        the veto layer the buy side has always had.

        Returns `(vetoed_symbols, verdict_or_None)`. Symbols in the returned
        set are dropped by the caller.

        **Failure posture: FAIL OPEN on uncertainty.** An unparseable or
        errored Risk Manager lets the exits through, logged loudly. This
        deliberately differs from the entry path, which fails closed with
        zero orders (`RiskStage`). The asymmetry is intentional and
        owner-ratified (2026-08-27):
        - failing closed on an ENTRY means not buying, which costs nothing;
        - failing closed on an EXIT means a thesis-invalidated position cannot
          be closed because a language model is unavailable, and the loss is
          then bounded only by the broker stop.

        **Item 60 (2026-09-16) — one owner, one uncertainty direction.**
        Deterministic Python owns refusal. A completed "no named trigger"
        is that owner's drop, not an uncertainty fail; those exits are
        not sent to this seat (the executor still enforces). Uncertainty
        — this seat unavailable/unparseable/verdict-less, or the
        hard-trigger recogniser itself unable to run — fails OPEN on
        both layers, which is the 2026-08-27 ratification applied to the
        pair. AI Risk remains a challenge seat: a parseable reject still
        drops, and an approval cannot override a deterministic drop.
        Every drop and every uncertainty fail-open writes an append-only
        per-symbol reason (`src/risk/exit_refusal.py`).

        The fact gates — the noise band, the metric-contradiction veto and
        `holding_discipline_claim_check` — remain the LAST LINE on data,
        not on word-recognition. Each abstains somewhere: the trigger
        gate checks the words, not the truth of the claim; the noise
        band is bypassed by any reason citing external information (which
        the trigger gate all but requires); the metric veto needs recorded
        prior metrics for that symbol or it does not run; and the claim
        check looks only at a regime-flip or HIGH-conviction-bearish
        claim, only on a still-protected position, passing every
        unverifiable claim by design. A plausibly-worded, deterministically-
        clean, wrong exit passes those. That gap is what this seat is for.

        **Ordering.** Named-trigger filtering now happens in this method
        before the model is called, so a dead Risk Manager cannot
        fail-open an exit the owner already refused. The other fact gates
        still live in `_midday_execute_llm_actions`, which the caller
        invokes AFTER this method; they still run on every surviving exit
        before any order can reach the broker.

        **The verdict's only live effect here is `rejected_symbols`.**
        `modifications` and `scale_all_buys` are applied by
        `_apply_risk_modifications`, which is called ONLY from the morning
        `RiskStage` (`src/pipeline_stages.py`); this method returns a veto set
        and reads neither.

        **Since 2026-09-14 they are no longer emitted at all here.** The seat
        answers `ExitRiskVerdict`, which is `RiskVerdict` without those two
        fields, and `ExitRiskReasoningChain`, which drops the `min_length=1`
        demand from the three chain steps this path's own prompt already
        stands down or inverts (`rr_audit`, `sizing_sanity`, `event_risk`).
        Telling the seat a lever is discarded still spent its judgement on the
        lever; the fix is to stop asking. Nothing about the morning BUY path
        changed — `RiskVerdict` and `RiskReasoningChain` are untouched and all
        six morning chain steps remain mandatory.

        **What this seat is shown (2026-09-13).** It is told explicitly that it
        is on the EXIT path (`review_mode`), so the renderer no longer stamps
        the Portfolio Manager's `continuity_check` / `premortem_check` with a
        NOT-PERFORMED banner for a chain that has never had those fields, and
        no longer asks it to verify a claim against a Tech block that no call
        on this loop produces. Everything the loop genuinely has — news,
        earnings, deployable cash and the parked reserve, drawdown state,
        holding ages, and a fetched earnings-proximity sweep — is now passed.
        See `src/agents/risk_review_mode.py`.
        """
        from src.agents import risk_review_mode
        from src.models import (
            ExitReviewChain, PortfolioDecision, TradeDecision,
        )
        from src.risk.exit_refusal import (
            CODE_AI_RISK_REJECT,
            CODE_AI_RISK_UNAVAILABLE,
            CODE_HARD_TRIGGER_UNCERTAIN,
            CODE_UNRECOGNIZED_TRIGGER,
            classify_trigger_reason,
        )

        # COVER is the short-side twin of SELL/REDUCE (Stage 3 shorts gap
        # fix): a short's exit must reach the AI Risk Manager exactly like a
        # long's does, not skip it.
        exits = [
            a for a in (review.actions if review else [])
            if a.action in ("SELL", "REDUCE", "COVER")
        ]
        if not exits:
            return set(), None

        held = {p.symbol.upper(): p for p in positions}
        decisions: list[TradeDecision] = []
        original_action_by_symbol: dict[str, str] = {}
        for action in exits:
            symbol = action.symbol.upper()
            if symbol not in held:
                continue
            # Item 60: unnamed-trigger exits are the deterministic owner's
            # completed refusal. Do not spend a Risk Manager call on them,
            # and do not let a dead/unparseable model fail-OPEN a sale the
            # owner already refused. Record here so the skip is durable if
            # execute is not reached; the executor still drops.
            judgment = classify_trigger_reason(
                action.reason, cites=_reason_cites_hard_trigger,
                trigger=getattr(action, "exit_trigger", None),
                trigger_evidence=getattr(action, "trigger_evidence", None),
            )
            if judgment == "unnamed":
                logger.info(
                    "AI Risk exit review: not sending %s %s — reason names "
                    "no recognised trigger; deterministic owner refuses "
                    "before the challenge seat.",
                    action.action, symbol,
                )
                self._record_exit_refusal(
                    symbol=symbol, run_id=run_id, action=action.action,
                    code=CODE_UNRECOGNIZED_TRIGGER, dropped=True,
                    detail=str(action.reason or "")[:400],
                    layer="hard_trigger",
                )
                continue
            if judgment == "uncertain":
                logger.error(
                    "AI Risk exit review: hard-trigger recogniser raised "
                    "on %s %s — failing OPEN on that gate, sending the "
                    "exit to the challenge seat. Reason was: %r",
                    action.action, symbol, str(action.reason)[:200],
                )
                self._record_exit_refusal(
                    symbol=symbol, run_id=run_id, action=action.action,
                    code=CODE_HARD_TRIGGER_UNCERTAIN, dropped=False,
                    detail=str(action.reason or "")[:400],
                    layer="hard_trigger",
                )
            original_action_by_symbol[symbol] = action.action
            # A COVER must be presented to the RM as a COVER, not relabeled
            # SELL — TradeDecision has a real "COVER" literal (the PM/
            # ExecutionStage decision path already uses it), and mislabeling
            # a short's exit as a stock sale is exactly the "reads a winning
            # short as a loser" failure this fix exists to close.
            decisions.append(TradeDecision(
                action="SELL" if action.action in ("SELL", "REDUCE") else "COVER",
                symbol=symbol,
                # 100 = full exit (SELL and COVER are both full closes on
                # this path); REDUCE is a partial whose exact fraction the
                # executor derives. The RM is being asked to judge WHETHER the
                # exit is sound, not to re-size it.
                allocation_pct=100.0 if action.action in ("SELL", "COVER") else 50.0,
                entry_price=0.0, stop_loss=0.0, take_profit=0.0,
                reasoning=str(action.reason or "")[:500],
            ))
        if not decisions:
            return set(), None

        summary = (review.overall_assessment or "")[:400]
        # The position reviewer's chain travels in the PM's `ReasoningChain`
        # container because that is the container the Risk Manager reads. Only
        # the five fields the reviewer actually authors are populated, and
        # `risk_review_mode` renders them under the reviewer's OWN labels.
        #
        # No `or "n/a"` and no cross-reference strings. Both were placeholder
        # content invented at this call site for fields the reviewer never
        # filled, and a fabricated "n/a" reads to the seat as a real answer —
        # which is how this whole defect started. `ReasoningChain` still
        # requires a non-empty string in each core field, so the substitute is
        # `risk_review_mode.NOT_AUTHORED`, which says exactly what happened.
        rc = review.reasoning_chain

        def _step(value: str) -> str:
            return (value or "").strip()[:800] or risk_review_mode.NOT_AUTHORED

        proposal = PortfolioDecision(
            # `ExitReviewChain`, not `ReasoningChain`: `news_check` is a
            # PM-schema field with no counterpart here and is not rendered to
            # the seat on this path, but the parent makes it `min_length=1`,
            # so it was being filled with a placeholder string that existed
            # only to satisfy the constraint. The subclass relaxes that one
            # field for this path alone; the morning chain is untouched.
            reasoning_chain=ExitReviewChain(
                macro_filter=_step(rc.macro_continuity_check),
                # Slot reuse, not a category claim: `earnings_check` is PM's
                # field name, and `risk_review_mode` labels this row
                # "Thesis progress check" — the reviewer's own field — in the
                # rendered message. Nothing about earnings is implied.
                earnings_check=_step(rc.thesis_progress_check),
                signal_conflicts=_step(rc.thesis_integrity_check),
                sizing_logic=_step(rc.execution_rationale),
                portfolio_balance=_step(rc.winners_discipline_check),
                cash_target=_step(rc.session_disposition_check),
            ),
            decisions=decisions,
            portfolio_view=f"EXIT REVIEW (position reviewer): {summary}",
        )

        # Holding ages. Informational on this path (protection is decided by
        # `check_structural_protection`, not by age) but the renderer prints
        # "held: unknown" without it, and unknown-by-omission is exactly the
        # kind of silent gap this fix exists to remove.
        try:
            exit_position_history = self._build_position_history(positions)
            record_exit_guard(self, "exit_review.position_history")
        except Exception as e:  # noqa: BLE001
            record_exit_guard(self, "exit_review.position_history", e, logger, effect="seat sees holding ages as unknown")
            exit_position_history = {}

        try:
            verdict, rm_result = self.risk_manager.review(
                portfolio_decision=proposal,
                positions=positions,
                macro_summary=macro_summary or {},
                rule_violations=[],
                total_value=total_value,
                heat=self._build_portfolio_heat(positions, total_value),
                # Everything below was available at this call site all along
                # and simply was not passed. The seat was being asked to audit
                # exits against news, earnings, drawdown state and event risk
                # while being shown none of them.
                news_intel=news_intel,
                earnings_analyses=earnings_analyses or [],
                cash=cash,
                reserve_balance=reserve_balance or 0.0,
                recent_performance=recent_performance or {},
                position_history=exit_position_history,
                event_risk_block=self._exit_event_risk_block(
                    sorted({d.symbol for d in decisions})
                ),
                # Tells the renderer which review this is. Without it the
                # exit path is rendered as a morning plan and the seat is told
                # two audit steps were skipped that do not exist here.
                review_mode=risk_review_mode.EXIT_REVIEW,
            )
            record_exit_guard(self, "exit_review.risk_review")
        except Exception as e:  # noqa: BLE001
            record_exit_guard(self, "exit_review.risk_review", e, logger, effect="fails OPEN: exits proceed unreviewed")
            for d in decisions:
                self._record_exit_refusal(
                    symbol=d.symbol, run_id=run_id,
                    action=original_action_by_symbol.get(d.symbol, d.action),
                    code=CODE_AI_RISK_UNAVAILABLE, dropped=False,
                    detail=f"risk manager raised: {e}"[:400],
                    layer="ai_risk",
                )
            return set(), None

        try:
            self.db.insert_agent_log(
                agent_name="risk_manager", run_id=run_id,
                input_summary=f"exit review: {len(decisions)} exit(s)",
                input_message=rm_result.user_message,
                output_summary=f"Approved: {verdict.approved if verdict else 'error'}",
                full_response=rm_result.raw_text,
                model=rm_result.model,
                tokens_used=rm_result.tokens_used,
                input_tokens=rm_result.input_tokens,
                output_tokens=rm_result.output_tokens,
                cost_usd=rm_result.cost_usd,
                status="agent_failure" if verdict is None else "ok",
            )
            record_exit_guard(self, "exit_review.agent_log_write")
        except Exception as e:  # noqa: BLE001
            record_exit_guard(self, "exit_review.agent_log_write", e, logger, effect="agent log row not written")

        if verdict is None:
            logger.error(
                "AI Risk exit review returned no verdict — failing OPEN: "
                "%d exit(s) proceed unreviewed.", len(decisions),
            )
            for d in decisions:
                self._record_exit_refusal(
                    symbol=d.symbol, run_id=run_id,
                    action=original_action_by_symbol.get(d.symbol, d.action),
                    code=CODE_AI_RISK_UNAVAILABLE, dropped=False,
                    detail="risk manager returned no verdict",
                    layer="ai_risk",
                )
            return set(), None

        # Phase 10.1 — the same granularity split as the morning plan, on the
        # exit side: `approved=False` still vetoes EVERY exit (the book is
        # what failed), while a per-symbol refusal vetoes only the exit it
        # names and lets the other exits through. Empty `rejected_symbols`
        # (every historical verdict, and any model that never emits the
        # field) reproduces the previous behaviour exactly.
        rejections = verdict.rejections_by_symbol()
        if verdict.approved:
            veto_reasons = {
                d.symbol: rejections[d.symbol.strip().upper()]
                for d in decisions if d.symbol.strip().upper() in rejections
            }
            if not veto_reasons:
                logger.info(
                    "AI Risk approved %d exit(s): %s",
                    len(decisions), (verdict.reasoning or "")[:200],
                )
                self._record_exit_review_approvals(
                    decisions, set(), verdict, run_id=run_id,
                    original_action_by_symbol=original_action_by_symbol,
                )
                return set(), verdict
        else:
            veto_reasons = {d.symbol: (verdict.reasoning or "") for d in decisions}

        vetoed = set(veto_reasons)
        logger.warning(
            "AI Risk REJECTED %d of %d exit(s) %s — holding instead. Reason: %s",
            len(vetoed), len(decisions), sorted(vetoed),
            (verdict.reasoning or "")[:300],
        )
        for symbol in sorted(vetoed):
            try:
                self.db.record_intraday_evaluation(
                    symbol=symbol, run_id=run_id,
                    status="exit_vetoed_by_ai_risk",
                    detail=(veto_reasons[symbol] or "")[:400],
                )
                record_exit_guard(self, "exit_review.veto_audit_write")
            except Exception as e:  # noqa: BLE001
                record_exit_guard(self, "exit_review.veto_audit_write", e, logger, symbol=symbol, effect="veto audit row not written")
            self._record_exit_refusal(
                symbol=symbol, run_id=run_id,
                action=original_action_by_symbol.get(symbol, "SELL"),
                code=CODE_AI_RISK_REJECT, dropped=True,
                detail=(veto_reasons[symbol] or "")[:400],
                layer="ai_risk",
            )
        # The exits the seat let through beside the ones it vetoed.
        self._record_exit_review_approvals(
            decisions, vetoed, verdict, run_id=run_id,
            original_action_by_symbol=original_action_by_symbol,
        )
        return vetoed, verdict

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
            record_exit_guard(self, "spent_trigger.acted_today_read")
        except Exception as _e:  # noqa: BLE001 — a failed read is uncertainty
            record_exit_guard(self, "spent_trigger.acted_today_read", _e, logger, effect="layer fails OPEN for this pass")
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
                            record_exit_guard(self, "exit_guard.metric_audit_write")
                        except Exception as e:  # noqa: BLE001
                            record_exit_guard(self, "exit_guard.metric_audit_write", e, logger, symbol=symbol, effect="audit row not written")
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
                        record_exit_guard(self, "alignment_exit.audit_write")
                    except Exception as e:  # noqa: BLE001
                        record_exit_guard(self, "alignment_exit.audit_write", e, logger, symbol=symbol, effect="audit row not written")
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
                        record_exit_guard(self, "noise_band.audit_write")
                    except Exception as e:  # noqa: BLE001
                        record_exit_guard(self, "noise_band.audit_write", e, logger, symbol=symbol, effect="audit row not written")
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
                        record_exit_guard(self, "exit_gate.audit_write")
                    except Exception as e:  # noqa: BLE001
                        record_exit_guard(self, "exit_gate.audit_write", e, logger, symbol=symbol, effect="audit row not written")
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
                        record_exit_guard(self, "holding_discipline.position_history")
                    except Exception as e:  # noqa: BLE001
                        record_exit_guard(self, "holding_discipline.position_history", e, logger, symbol=symbol, effect="protection read without entry context")
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
                    record_exit_guard(self, "holding_discipline.check")
                except Exception as e:  # noqa: BLE001
                    record_exit_guard(self, "holding_discipline.check", e, logger, symbol=symbol, effect="claim goes unverified rather than blocking")
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
                        record_exit_guard(self, "holding_discipline.audit_write_blocked")
                    except Exception as e:  # noqa: BLE001
                        record_exit_guard(self, "holding_discipline.audit_write_blocked", e, logger, symbol=symbol, effect="audit row not written")
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
                        record_exit_guard(self, "holding_discipline.audit_write_unverified")
                    except Exception as e:  # noqa: BLE001
                        record_exit_guard(self, "holding_discipline.audit_write_unverified", e, logger, symbol=symbol, effect="audit row not written")

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
                    record_exit_guard(self, "spent_trigger.audit_write")
                except Exception as e:  # noqa: BLE001
                    record_exit_guard(self, "spent_trigger.audit_write", e, logger, symbol=symbol, effect="audit row not written")
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
                        record_exit_guard(self, "midday_trail.pending_protection_check")
                    except Exception as exc:  # noqa: BLE001
                        record_exit_guard(self, "midday_trail.pending_protection_check", exc, logger, symbol=symbol, effect="restore-in-flight check skipped")
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
                        record_exit_guard(self, "spent_trigger.record_acted")
                    except Exception as e:  # noqa: BLE001
                        record_exit_guard(self, "spent_trigger.record_acted", e, logger, symbol=symbol, effect="a second cut today would not be caught")
                    if acted_today is not None:
                        acted_today.append(_acted)
                logger.info(
                    "Midday action: %s %s %s — %s",
                    act, self._format_qty(qty),
                    symbol, action_item.get("reason"),
                )
                record_exit_guard(self, "midday.order")
            except Exception as e:
                record_exit_guard(self, "midday.order", e, logger, symbol=symbol, effect="order failed; exit not placed")
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
