"""The AI Risk seat's review of the proposed exits.

Lifted verbatim out of `ExitEngineMixin` (src/pipeline_exits.py) as a
standalone class: every collaborator is an explicit keyword-only
constructor argument, so it is built and exercised without a pipeline.
The mixin keeps a thin method of the same name that builds this object
and calls it, so every existing caller and patch target is unchanged.
"""

import logging
from src.models import TradeDecision

#: Logs under `src.pipeline`, as the bodies did before the move;
#: binding the name rather than `__name__` keeps log records byte-identical.
logger = logging.getLogger("src.pipeline")

#: The ONE lazy re-export this module carries. The body below calls the
#: hard-trigger phrase matcher by its module-global name, and tests patch it
#: at `src.pipeline_exits._reason_cites_hard_trigger`; resolving it at call
#: time keeps that patch target live instead of freezing the import-time binding.
def _reason_cites_hard_trigger(reason: str) -> bool:
    from src import pipeline_exits as _owner_module
    return _owner_module._reason_cites_hard_trigger(reason)


class RiskReviewExits:
    """The AI Risk seat's review of the proposed exits."""

    def __init__(self, *,
                 build_portfolio_heat,
                 build_position_history,
                 exit_event_risk_block,
                 record_exit_refusal,
                 record_exit_review_approvals,
                 db,
                 risk_manager) -> None:
        self._build_portfolio_heat = build_portfolio_heat
        self._build_position_history = build_position_history
        self._exit_event_risk_block = exit_event_risk_block
        self._record_exit_refusal = record_exit_refusal
        self._record_exit_review_approvals = record_exit_review_approvals
        self.db = db
        self.risk_manager = risk_manager

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
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "Exit review: position history rebuild failed — the seat sees "
                "holding ages as unknown: %s", e,
            )
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
        except Exception as e:  # noqa: BLE001
            logger.error(
                "AI Risk exit review RAISED (%s) — failing OPEN: %d exit(s) "
                "proceed unreviewed. Named-trigger exits already passed the "
                "deterministic owner; unnamed exits were not sent here.",
                e, len(decisions),
            )
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
        except Exception as e:  # noqa: BLE001
            logger.warning("AI Risk exit review: agent log write failed: %s", e)

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
            except Exception as e:  # noqa: BLE001
                logger.warning("AI Risk exit review: audit write failed: %s", e)
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
