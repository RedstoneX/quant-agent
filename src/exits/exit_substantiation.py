"""Exit-trigger substantiation: the position reviewer's second pass that must name a canonical trigger before a sell may proceed.

Lifted verbatim out of `ExitEngineMixin` (src/pipeline_exits.py) as a
standalone class: every collaborator is an explicit keyword-only
constructor argument, so it is built and exercised without a pipeline.
The mixin keeps a thin method of the same name that builds this object
and calls it, so every existing caller and patch target is unchanged.
"""

import logging

#: The lift out of `ExitEngineMixin` left these two behind: the bodies call
#: them, the mixin's module imported them, this module did not -- so the
#: agent-log write raised `NameError` into the broad catch below and the row
#: was never written. Imported here, as every other caller does.
from src.agents.base import agent_log_kwargs, seat_acceptance_kwargs

#: Logs under `src.pipeline`, as the bodies did before the move;
#: binding the name rather than `__name__` keeps log records byte-identical.
logger = logging.getLogger("src.pipeline")


class ExitSubstantiation:
    """Exit-trigger substantiation: the position reviewer's second pass that must name a canonical trigger before a sell may proceed."""

    def __init__(self, *,
                 record_heal,
                 require_paid_analysis,
                 db,
                 position_reviewer) -> None:
        self._record_heal = record_heal
        self._require_paid_analysis = require_paid_analysis
        self.db = db
        self.position_reviewer = position_reviewer

    def _substantiate_exit_triggers(self, review, *, ctx, run_id: str,
                                    review_kwargs: dict):
        """Heal, re-ask, then durably record an unsubstantiated exit trigger.

        The defect this closes (2026-09-18). Every SELL/REDUCE/COVER had to
        "cite a hard trigger" and the entire check was a substring match
        over `reason` prose. On 2026-09-16 the two real exits — COP SELL
        and EQNR REDUCE, run `midday-d8996a51` — carried the reason
        ``"adverse news"``, two words and nothing else, and passed every
        gate: the phrase is on the list, and
        `exit_guard.holding_discipline_claim_check` returned "ok" because
        it reads the PROSE for a claim it knows how to check and those two
        words make none. Meanwhile an exit that honestly described a stall
        is what the metric veto audits, and one naming no listed phrase is
        dropped. The gate was selecting for bad paperwork.

        The desk's standing heal order (owner 2026-09-16, `src.seat_heal`)
        applies, in order and with no step skipped:

        1. **Mechanical heal.** A trigger the prose already names is
           written into `PositionAction.exit_trigger` from the SAME phrase
           vocabulary the executor has matched against since 2026-08-27.
           Nothing is invented and nothing that used to execute stops
           executing.
        2. **One re-ask.** Anything still unsubstantiated — no trigger, no
           evidence behind the trigger, or an explicit
           `cannot_substantiate` — goes back to the seat naming those
           symbols and asking for the trigger plus the recorded thing it
           rests on, with keeping `cannot_substantiate` and HOLDing named
           as a correct answer. Bounded by the same one-paid-retry-per-
           seat-per-session cap the research seats use
           (`seat_heal.can_paid_retry`), so this can never loop or
           double-spend.
        3. **Durable reason.** Whatever is still unsubstantiated after the
           re-ask gets an append-only per-symbol `exit_refusal` row
           (`code=unsubstantiated_after_reask`, `layer=exit_trigger`) and
           a heal-FAILED owner alert, exactly as a failed research heal
           does.

        **What this does NOT do: it does not drop the exit.** `dropped` is
        False on every row this method writes. The call site's disclosed
        reasoning is that stranding the desk in a losing position is
        strictly worse than an uncheckable claim passing, so an
        unsubstantiated exit still reaches the remaining gates. What has
        changed is that it is no longer UNREPORTABLE, and that the trigger
        is now in a field — so `holding_discipline_claim_check` can be
        pointed at the record the claim is about and reach a PROVABLY
        FALSE verdict, which does block and does alert. Three-valued as
        ratified 2026-09-04: false blocks, unverifiable logs, ok passes.
        """
        from src.cost_circuit import PaidAnalysisSuspended
        from src.risk.exit_refusal import record_exit_refusal
        from src.risk.exit_trigger import (
            CODE_UNSUBSTANTIATED_AFTER_REASK, CODE_UNSUBSTANTIATED_TRIGGER,
            REASK_DIRECTIVE, check_exit_trigger,
        )
        from src.seat_heal import (
            HealResult, HEAL_CAP_BLOCKED, HEAL_FAILED, HEAL_PAID_RETRY,
            can_paid_retry, record_paid_retry,
        )
        SEAT = "position_reviewer_exit_trigger"

        def _classify(actions):
            """{SYMBOL: check} for every exit needing substantiation, and
            the count of actions the mechanical heal fixed."""
            pending, healed = {}, 0
            for a in actions or []:
                check = check_exit_trigger(
                    action=getattr(a, "action", None),
                    exit_trigger=getattr(a, "exit_trigger", None),
                    trigger_evidence=getattr(a, "trigger_evidence", ""),
                    reason=getattr(a, "reason", ""),
                    symbol=getattr(a, "symbol", "") or "",
                )
                if check.healed and check.trigger is not None:
                    # Persist the heal on the object the executor reads, so
                    # the downstream fact-check sees a named trigger rather
                    # than re-deriving it from prose a second time.
                    try:
                        a.exit_trigger = check.trigger
                        healed += 1
                    except Exception as e:  # noqa: BLE001
                        logger.warning(
                            "exit trigger: could not write healed trigger "
                            "on %s (%s)", getattr(a, "symbol", "?"), e,
                        )
                if check.needs_reask:
                    pending[(getattr(a, "symbol", "") or "").upper()] = check
            return pending, healed

        if review is None:
            return review
        pending, healed = _classify(getattr(review, "actions", None))
        if healed:
            logger.info(
                "exit trigger: mechanically healed %d exit trigger(s) from "
                "the reason prose — no trigger invented", healed,
            )
        if not pending:
            return review

        for sym, check in sorted(pending.items()):
            logger.warning("exit trigger: %s", check.finding)
            record_exit_refusal(
                self.db, symbol=sym, run_id=run_id, action="EXIT",
                code=CODE_UNSUBSTANTIATED_TRIGGER, dropped=False,
                detail=str(check.finding or "")[:400], layer="exit_trigger",
            )

        retries = dict(getattr(ctx, "heal_paid_retries", None) or {})
        if not can_paid_retry(retries, SEAT):
            logger.warning(
                "exit trigger: the one re-ask for this seat is already "
                "spent this session — %s stay(s) unsubstantiated and "
                "recorded", ", ".join(sorted(pending)),
            )
            return review
        try:
            self._require_paid_analysis("position_reviewer")
        except PaidAnalysisSuspended as exc:
            self._record_heal(ctx, HealResult(
                seat=SEAT, outcome=HEAL_CAP_BLOCKED,
                reason=f"spend cap blocked the exit-trigger re-ask: {exc}",
                details={"symbols": sorted(pending)},
            ), alert=True)
            return review

        ctx.heal_paid_retries = record_paid_retry(retries, SEAT)
        challenge = REASK_DIRECTIVE + ", ".join(sorted(pending))
        try:
            reasked, reask_result = self.position_reviewer.review(
                **{**review_kwargs, "substantiation_challenge": challenge},
            )
        except Exception as exc:  # noqa: BLE001
            self._record_heal(ctx, HealResult(
                seat=SEAT, outcome=HEAL_FAILED,
                reason=f"exit-trigger re-ask raised: {exc}",
                paid_retry=True, details={"symbols": sorted(pending)},
            ), alert=True)
            return review

        try:
            self.db.insert_agent_log(
                **seat_acceptance_kwargs(
                    "position_review_parse_error" if not reasked else None,
                    result=reask_result,
                ),
                agent_name="position_reviewer", run_id=run_id,
                input_summary=f"exit-trigger re-ask | {', '.join(sorted(pending))}",
                input_message=reask_result.user_message,
                output_summary=(
                    reasked.overall_assessment if reasked else "parse_error"
                ),
                full_response=reask_result.raw_text,
                model=reask_result.model,
                tokens_used=reask_result.tokens_used,
                input_tokens=reask_result.input_tokens,
                output_tokens=reask_result.output_tokens,
                cost_usd=reask_result.cost_usd,
                **agent_log_kwargs(reask_result),
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("exit trigger: re-ask log write failed: %s", e)

        if reasked is None:
            self._record_heal(ctx, HealResult(
                seat=SEAT, outcome=HEAL_FAILED,
                reason="exit-trigger re-ask returned no parseable review",
                paid_retry=True, details={"symbols": sorted(pending)},
            ), alert=True)
            return review

        # Merge the re-answered actions for the CHALLENGED symbols only.
        # Every other action stays exactly as first answered: the re-ask
        # asked one question and is not an opportunity to re-decide the
        # rest of the book.
        replacements = {
            (getattr(a, "symbol", "") or "").upper(): a
            for a in (getattr(reasked, "actions", None) or [])
            if (getattr(a, "symbol", "") or "").upper() in pending
        }
        merged = [
            replacements.get((getattr(a, "symbol", "") or "").upper(), a)
            for a in (getattr(review, "actions", None) or [])
        ]
        review.actions = merged
        still, _ = _classify(merged)
        logger.info(
            "exit trigger: re-ask answered %d of %d challenged symbol(s); "
            "%d still unsubstantiated",
            len(replacements), len(pending), len(still),
        )

        if not still:
            self._record_heal(ctx, HealResult(
                seat=SEAT, outcome=HEAL_PAID_RETRY,
                reason="exit-trigger re-ask substantiated every challenged exit",
                paid_retry=True, usable=True,
                details={"symbols": sorted(pending)},
            ), alert=False)
            return review

        for sym, check in sorted(still.items()):
            logger.error(
                "exit trigger: %s STILL unsubstantiated after the re-ask. "
                "The exit is NOT dropped on this ground — stranding the "
                "desk in a losing position is worse than an uncheckable "
                "claim passing — but it is recorded and the named trigger "
                "is now fact-checked against the desk's own records. %s",
                sym, check.finding,
            )
            record_exit_refusal(
                self.db, symbol=sym, run_id=run_id, action="EXIT",
                code=CODE_UNSUBSTANTIATED_AFTER_REASK, dropped=False,
                detail=str(check.finding or "")[:400], layer="exit_trigger",
            )
        self._record_heal(ctx, HealResult(
            seat=SEAT, outcome=HEAL_FAILED,
            reason=(
                "exit trigger still unsubstantiated after the one re-ask "
                "for: " + ", ".join(sorted(still)) + ". Exits not dropped "
                "on this ground; recorded per symbol."
            ),
            paid_retry=True, details={"symbols": sorted(still)},
        ), alert=True)
        return review
