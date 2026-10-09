"""AI Risk review of the reviewer's exits, lifted verbatim out of `ExitEngineMixin` (src/pipeline_exits.py).

The body is unchanged apart from `self` -> `owner` (the pipeline instance is
now the first argument); `ExitEngineMixin._risk_review_exits` is a one-line shim with the
same signature. Sell-side code. Since 2026-10-09 the seat is advisory on
exits: it records objections and never removes an exit (see the docstring).
"""

import importlib
import logging

from src.sentinel.guarded_exit import record_exit_guard

logger = logging.getLogger("src.pipeline")


def _risk_review_exits(
    owner,
    review,
    positions,
    *,
    run_id: str,
    total_value: float,
    macro_summary: dict | None = None,
    position_facts: dict | None = None,
    news_intel=None,
    earnings_analyses: list | None = None,
    cash: float | None = None,
    reserve_balance: float = 0.0,
    recent_performance: dict | None = None,
):
    """Put the reviewer's exits in front of the AI Risk Manager — Phase 3.4.

    `AGENTS.md` states the chain as `Specialists -> Portfolio Manager ->
    AI Risk -> deterministic Python -> broker`, **for exits as well as
    entries**. Until this landed, `run_position_review` called only
    `position_reviewer` and then executed, so the entire sell side skipped
    the veto layer the buy side has always had.

    Returns `(vetoed_symbols, verdict_or_None)`. The set is now ALWAYS
    empty: the shape is kept so the caller is unchanged.

    **The seat is ADVISORY on exits (decided 2026-09-19, adversary-approved
    again 2026-10-09).** PR #634 made it advisory on entries and left this
    path vetoing; this closes that gap. A whole-book reject or a per-name
    rejection no longer removes any exit: each objection is recorded per
    stock with the seat's reason as `CODE_AI_RISK_OBJECTION`
    (`dropped=False`) and logged, and the sell proceeds to the
    deterministic fact gates. Measured before the change: in production
    the seat never blocked an exit (5 reviews, all approvals).

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
    pair. AI Risk remains a challenge seat but only an advisory one: a
    parseable reject is recorded, not a drop, and an approval cannot
    override a deterministic drop. Every drop, every objection and every
    uncertainty fail-open writes an append-only per-symbol reason
    (`src/risk/exit_refusal.py`).

    The fact gates — the noise band, the metric-contradiction veto and
    `holding_discipline_claim_check` — are the LAST LINE, on data,
    not on word-recognition. Each abstains somewhere: the trigger
    gate checks the words, not the truth of the claim; the noise
    band is bypassed by any reason citing external information (which
    the trigger gate all but requires); the metric veto needs recorded
    prior metrics for that symbol or it does not run; and the claim
    check looks only at a regime-flip or HIGH-conviction-bearish
    claim, only on a still-protected position, passing every
    unverifiable claim by design. A plausibly-worded, deterministically-
    clean, wrong exit passes those. The seat's objection to such an exit
    is now a durable per-stock record for review, not a block.

    **Ordering.** Named-trigger filtering now happens in this method
    before the model is called, so a dead Risk Manager cannot
    fail-open an exit the owner already refused. The other fact gates
    still live in `_midday_execute_llm_actions`, which the caller
    invokes AFTER this method; they still run on every surviving exit
    before any order can reach the broker.

    **The verdict's only effect here is the record it leaves**
    (`approved` / `rejected_symbols` become objection or approval rows).
    `modifications` and `scale_all_buys` are applied by
    `_apply_risk_modifications`, which is called ONLY from the morning
    `RiskStage` (`src/pipeline_stages.py`); this method reads neither.

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

    # Looked up through the original module at call time, so a test that patches
    # `src.pipeline_exits._reason_cites_hard_trigger` still reaches this body
    # (a static import would also make an import cycle: pipeline_exits -> here).
    _reason_cites_hard_trigger = importlib.import_module("src.pipeline_exits")._reason_cites_hard_trigger
    from src.models import (
        ExitReviewChain,
        PortfolioDecision,
        TradeDecision,
    )
    from src.risk.exit_refusal import (
        CODE_AI_RISK_OBJECTION,
        CODE_AI_RISK_UNAVAILABLE,
        CODE_HARD_TRIGGER_UNCERTAIN,
        CODE_UNRECOGNIZED_TRIGGER,
        classify_trigger_reason,
    )

    # COVER is the short-side twin of SELL/REDUCE (Stage 3 shorts gap
    # fix): a short's exit must reach the AI Risk Manager exactly like a
    # long's does, not skip it.
    exits = [a for a in (review.actions if review else []) if a.action in ("SELL", "COVER")]
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
            action.reason,
            cites=_reason_cites_hard_trigger,
            trigger=getattr(action, "exit_trigger", None),
            trigger_evidence=getattr(action, "trigger_evidence", None),
        )
        if judgment == "unnamed":
            logger.info(
                "AI Risk exit review: not sending %s %s — reason names "
                "no recognised trigger; deterministic owner refuses "
                "before the challenge seat.",
                action.action,
                symbol,
            )
            owner._record_exit_refusal(
                symbol=symbol,
                run_id=run_id,
                action=action.action,
                code=CODE_UNRECOGNIZED_TRIGGER,
                dropped=True,
                detail=str(action.reason or "")[:400],
                layer="hard_trigger",
            )
            continue
        if judgment == "uncertain":
            logger.error(
                "AI Risk exit review: hard-trigger recogniser raised "
                "on %s %s — failing OPEN on that gate, sending the "
                "exit to the challenge seat. Reason was: %r",
                action.action,
                symbol,
                str(action.reason)[:200],
            )
            owner._record_exit_refusal(
                symbol=symbol,
                run_id=run_id,
                action=action.action,
                code=CODE_HARD_TRIGGER_UNCERTAIN,
                dropped=False,
                detail=str(action.reason or "")[:400],
                layer="hard_trigger",
            )
        original_action_by_symbol[symbol] = action.action
        # A COVER must be presented to the RM as a COVER, not relabeled
        # SELL — TradeDecision has a real "COVER" literal (the PM/
        # ExecutionStage decision path already uses it), and mislabeling
        # a short's exit as a stock sale is exactly the "reads a winning
        # short as a loser" failure this fix exists to close.
        decisions.append(
            TradeDecision(
                action="SELL" if action.action == "SELL" else "COVER",
                symbol=symbol,
                # 100 = full exit (SELL and COVER are both full closes on
                # this path; REDUCE was removed 2026-10-09 — whole exits only).
                # The RM is being asked to judge WHETHER the exit is sound,
                # not to re-size it.
                allocation_pct=100.0,
                entry_price=0.0,
                stop_loss=0.0,
                take_profit=0.0,
                reasoning=str(action.reason or "")[:500],
            )
        )
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
        exit_position_history = owner._build_position_history(positions)
        record_exit_guard(owner, "exit_review.position_history")
    except Exception as e:  # noqa: BLE001
        record_exit_guard(
            owner,
            "exit_review.position_history",
            e,
            logger,
            effect="seat sees holding ages as unknown",
        )
        exit_position_history = {}

    try:
        verdict, rm_result = owner.risk_manager.review(
            portfolio_decision=proposal,
            positions=positions,
            macro_summary=macro_summary or {},
            rule_violations=[],
            total_value=total_value,
            heat=owner._build_portfolio_heat(positions, total_value),
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
            event_risk_block=owner._exit_event_risk_block(sorted({d.symbol for d in decisions})),
            # Tells the renderer which review this is. Without it the
            # exit path is rendered as a morning plan and the seat is told
            # two audit steps were skipped that do not exist here.
            review_mode=risk_review_mode.EXIT_REVIEW,
        )
        record_exit_guard(owner, "exit_review.risk_review")
    except Exception as e:  # noqa: BLE001
        record_exit_guard(owner, "exit_review.risk_review", e, logger, effect="fails OPEN: exits proceed unreviewed")
        for d in decisions:
            owner._record_exit_refusal(
                symbol=d.symbol,
                run_id=run_id,
                action=original_action_by_symbol.get(d.symbol, d.action),
                code=CODE_AI_RISK_UNAVAILABLE,
                dropped=False,
                detail=f"risk manager raised: {e}"[:400],
                layer="ai_risk",
            )
        return set(), None

    try:
        owner.db.insert_agent_log(
            agent_name="risk_manager",
            run_id=run_id,
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
        record_exit_guard(owner, "exit_review.agent_log_write")
    except Exception as e:  # noqa: BLE001
        record_exit_guard(owner, "exit_review.agent_log_write", e, logger, effect="agent log row not written")

    if verdict is None:
        logger.error(
            "AI Risk exit review returned no verdict — failing OPEN: %d exit(s) proceed unreviewed.",
            len(decisions),
        )
        for d in decisions:
            owner._record_exit_refusal(
                symbol=d.symbol,
                run_id=run_id,
                action=original_action_by_symbol.get(d.symbol, d.action),
                code=CODE_AI_RISK_UNAVAILABLE,
                dropped=False,
                detail="risk manager returned no verdict",
                layer="ai_risk",
            )
        return set(), None

    # 2026-09-19 decision (adversary-approved again 2026-10-09): the seat
    # is ADVISORY on exits, as PR #634 already made it on entries. A
    # whole-book reject (`approved=False`) or a per-symbol rejection no
    # longer removes any exit; each objection is recorded durably per
    # stock, with the seat's reason, as `CODE_AI_RISK_OBJECTION`
    # (`dropped=False`), so a reader can tell an objection that did not
    # stop the sell from a refusal that did.
    rejections = verdict.rejections_by_symbol()
    if verdict.approved:
        objection_reasons = {
            d.symbol: rejections[d.symbol.strip().upper()] for d in decisions if d.symbol.strip().upper() in rejections
        }
    else:
        objection_reasons = {d.symbol: (verdict.reasoning or "") for d in decisions}

    if not objection_reasons:
        logger.info(
            "AI Risk approved %d exit(s): %s",
            len(decisions),
            (verdict.reasoning or "")[:200],
        )
    else:
        objected = sorted(objection_reasons)
        logger.warning(
            "AI Risk OBJECTED to %d of %d exit(s) %s — advisory only, the "
            "sell(s) proceed to the fact gates. Reason: %s",
            len(objected),
            len(decisions),
            objected,
            (verdict.reasoning or "")[:300],
        )
        for symbol in objected:
            owner._record_exit_refusal(
                symbol=symbol,
                run_id=run_id,
                action=original_action_by_symbol.get(symbol, "SELL"),
                code=CODE_AI_RISK_OBJECTION,
                dropped=False,
                detail=(objection_reasons[symbol] or "")[:400],
                layer="ai_risk",
            )
    # The exits the seat approved, beside the ones it objected to (an
    # objection is not an approval, so those are excluded here).
    owner._record_exit_review_approvals(
        decisions,
        set(objection_reasons),
        verdict,
        run_id=run_id,
        original_action_by_symbol=original_action_by_symbol,
    )
    return set(), verdict
