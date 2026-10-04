"""Candidate accounting and heal records (split out of src/pipeline_stages.py).

Moved VERBATIM out of `src/pipeline_stages.py`: the durable record of what
happened to each portfolio-manager candidate (the accounting re-ask and its
outcome rows), the execution-skip and scale-in-window rows, the pipeline
event row, the soft-exit heal records, and the isolation of soft-exit
entries that came back empty. Every function takes the pipeline and the run
context as plain arguments, so each can be exercised with stand-ins and no
`TradingPipeline` is ever built here (`tests/test_boundary_pipeline_candidate_records.py`
is the clause-5 witness). Every moved name is re-exported from
`src.pipeline_stages` through its one lazy re-export table, so each original
import path and each test patch target is unchanged; the write-through mirror
on that module keeps a patched name the same object on both sides.

The shared helpers and constants below are imported FROM `src.pipeline_stages`
exactly as the stage modules do. `src.pipeline_stages` never imports this
module at import time (only lazily, by name), so the graph stays acyclic.

Not in `src.number_sources.SCOPED_PATHS` on purpose: scoping this file was
measured (2026-10-04) to register zero number sites, so the ledger guard loses
nothing and `number_sources.py` is not grown.

This module must not import `src.pipeline`.
"""
from __future__ import annotations

from src.pipeline_stages import (  # noqa: F401  shared helpers and module-level names
    PortfolioManagerAgent,
    SOFT_EXIT_HEAL_EVENT_REASON,
    SOFT_EXIT_MISSING_AFTER_RETRY,
    _book_risk_inputs,
    _persist_evidence,
    agent_log_kwargs,
    logger,
    missing_stated_falsifier,
    open_target_missing_falsifier,
    pipeline_event_fields,
    record_order_attempt_from_event,
    seat_acceptance_kwargs,
)


def _record_execution_skip(pipeline, ctx, symbol: str, reason: str,
                           detail: str) -> None:
    """Durable record of a deterministic BUY skip in the execution phase.

    Every skip path in the BUY loop used to be a log-only `continue`: the
    DB, funnel, Mission Control and the evening reflection all read a
    session whose approved BUYs were dropped here as a deliberate no-trade
    (2026-08-19: three risk-approved BUYs skipped as unfunded; the evening
    analyst concluded the system needed "proactive idea generation").
    Appends to ctx.execution_skips (drives the run's final status) and
    persists an `execution_skip` evidence row (drives the funnel/journal).
    Best-effort by construction — persistence failure never affects the
    skip decision itself (trading-core rule).
    """
    ctx.execution_skips.append(
        {"symbol": symbol, "reason": reason, "detail": detail},
    )
    import json as _json
    _persist_evidence(
        pipeline.db, run_id=ctx.run_id, agent_name="execution",
        kind="execution_skip", scope="symbol", symbol=symbol,
        decision_id=ctx.decision_id,
        evidence_json=_json.dumps(
            {"symbol": symbol, "reason": reason, "detail": detail},
        ),
    )


def _record_scale_in_window_closed(pipeline, ctx, spec: dict, *, covered: bool) -> None:
    """Close the scale-in unprotected window with a measured duration.

    Board item 193. A scale-in cancels the resting protective stop so the add
    can reach the broker, which leaves the WHOLE held position — not just the
    add — with no stop until the rearm lands. This emits one event per cancel
    at the moment the rearm attempt returns, carrying:

      * `window_seconds` — broker cancel acknowledgement to broker rearm
        acknowledgement, both `time.monotonic()` inside the one run, so it
        bounds real exposure and never reflects a row's write time;
      * `held_qty_before` and `exposed_notional` — the size of what was naked;
      * `covered` — False when the rearm did NOT land, which means the window
        is still open when the event is written and the fail-closed owner
        alert below it is the thing that matters.
    Nothing is emitted when no cancel happened: a naked add has no window.
    """
    from src.execution.scale_in import unprotected_window_seconds
    seconds = unprotected_window_seconds(spec.get("cancel_confirmed_at"))
    if seconds is None:
        return
    held = abs(float(spec.get("held_qty_before") or 0.0))
    price = float(spec.get("reference_price") or 0.0)
    _record_pipeline_event(
        pipeline, ctx, spec.get("symbol"), "scale_in",
        "unprotected_window_closed" if covered else "unprotected_window_still_open",
        "rearm_acknowledged" if covered else "rearm_did_not_land",
        window_seconds=seconds,
        held_qty_before=held,
        exposed_notional=round(held * price, 2) if price > 0 else None,
        wal_row_id=spec.get("wal_row_id"),
        stop_price=spec.get("stop_price"),
    )


def _record_pipeline_event(pipeline, ctx, symbol: str | None, stage: str,
                           outcome: str, reason: str = "", **details) -> None:
    """Append one typed lifecycle fact to the existing evidence stream.
    Conversion step 6: shim over `EventJournal.record_pipeline_event`. Routes
    through this module's `_persist_evidence` on purpose, so a test that
    patches that name still sees every event, exactly as before.
    """
    if stage == "order": record_order_attempt_from_event(db=pipeline.db, symbol=symbol, outcome=outcome, reason=reason, run_id=ctx.run_id, details=details)  # one Sentinel attempt row per order event
    _persist_evidence(pipeline.db, **pipeline_event_fields(
        run_id=ctx.run_id, decision_id=ctx.decision_id, symbol=symbol,
        stage=stage, outcome=outcome, reason=reason, details=details,
    ))


#: The seat this accounting spends its one paid retry under. Distinct from
#: `portfolio_manager` itself so a bookkeeping re-ask can never consume the
#: retry another PM heal may need in the same session, and vice versa.
_PM_ACCOUNTING_SEAT = "portfolio_manager_candidate_accounting"


def _record_accounted_candidate(pipeline, ctx, accounted) -> None:
    """One durable per-symbol row for one accounted candidate.

    `refusal` carries the comparable CODE and `note` the reader-facing
    prose. That split is deliberate: `refusal_signature.signature_key`
    reads `refusal` and does not read `note`, so two sessions are compared
    on the named ground rather than on the seat's choice of words.
    """
    payload = accounted.event_kwargs()
    _record_pipeline_event(
        pipeline, ctx, accounted.symbol,
        payload["stage"], payload["outcome"], payload["reason"],
        refusal=payload["refusal"], note=payload["note"],
    )


def _account_for_pm_candidates(
    pipeline, ctx, *, run_id, analyses, positions, decision, pm_decide_kwargs,
) -> None:
    """Make the portfolio manager account for every candidate it was shown.

    Board item 133 (2026-09-18). Replaces the loop that recorded every
    analysed candidate missing from `targets` as
    `omitted / candidate_not_selected_for_target` — one unvarying string
    that was not a reason, because the seat was never asked for one. See
    `src/pm_accounting.py` for why that defeated the jam detector by
    construction.

    The desk's standing heal order, no step skipped and no new retry
    invented: mechanical heal from what the seat did say, ONE re-ask under
    the existing per-seat paid-retry cap, then a durable per-symbol reason.

    Decides nothing. Every candidate's trading fate was already settled by
    `decide()` before this function runs; all that changes here is whether
    the desk can say WHY. It never raises — a bookkeeping failure must not
    take a live session with it.
    """
    from src.pm_accounting import (
        REASK_DIRECTIVE, account_for_candidates, unaccounted_row,
    )
    from src.seat_heal import (
        HealResult, HEAL_CAP_BLOCKED, HEAL_FAILED, HEAL_MECHANICAL,
        HEAL_PAID_RETRY, can_paid_retry, record_paid_retry,
    )

    result = account_for_candidates(
        analyses=analyses, decision=decision, positions=positions,
    )
    for accounted in result.accounted:
        _record_accounted_candidate(pipeline, ctx, accounted)
    if not result.unaccounted:
        if result.accounted:
            logger.info(
                "PM candidate accounting: every one of the %d non-targeted "
                "candidate(s) carries a named ground", len(result.accounted),
            )
        return

    pending = sorted(result.unaccounted)
    logger.warning(
        "PM candidate accounting: the seat dropped %s without naming a "
        "ground. Re-asking once (bookkeeping only).", ", ".join(pending),
    )

    def _finish(symbols, *, asked: bool) -> None:
        for symbol in sorted(symbols):
            _record_accounted_candidate(
                pipeline, ctx, unaccounted_row(symbol, asked=asked),
            )

    retries = dict(getattr(ctx, "heal_paid_retries", None) or {})
    if not can_paid_retry(retries, _PM_ACCOUNTING_SEAT):
        logger.warning(
            "PM candidate accounting: the one re-ask for this seat is "
            "already spent this session — %s stay(s) unaccounted and "
            "recorded", ", ".join(pending),
        )
        _finish(pending, asked=False)
        return

    from src.cost_circuit import PaidAnalysisSuspended
    try:
        pipeline._require_paid_analysis("portfolio_manager")
    except PaidAnalysisSuspended as exc:
        _record_heal_safely(pipeline, ctx, HealResult(
            seat=_PM_ACCOUNTING_SEAT, outcome=HEAL_CAP_BLOCKED,
            reason=(
                "spend cap blocked the candidate-accounting re-ask: "
                f"{exc}"
            ),
            details={"symbols": pending},
        ))
        _finish(pending, asked=False)
        return

    ctx.heal_paid_retries = record_paid_retry(retries, _PM_ACCOUNTING_SEAT)
    challenge = REASK_DIRECTIVE + ", ".join(pending)
    try:
        reasked, reask_result = pipeline.portfolio_manager.decide(
            **{**pm_decide_kwargs, "accounting_challenge": challenge},
        )
    except Exception as exc:  # noqa: BLE001
        _record_heal_safely(pipeline, ctx, HealResult(
            seat=_PM_ACCOUNTING_SEAT, outcome=HEAL_FAILED,
            reason=f"candidate-accounting re-ask raised: {exc}",
            paid_retry=True, details={"symbols": pending},
        ))
        _finish(pending, asked=True)
        return

    try:
        pipeline.db.insert_agent_log(
            **seat_acceptance_kwargs(
                "no_valid_grounded_decision" if not reasked else None,
                result=reask_result,
            ),
            agent_name="portfolio_manager", run_id=run_id,
            input_summary=(
                f"candidate-accounting re-ask | {', '.join(pending)}"
            ),
            input_message=reask_result.user_message,
            output_summary=(
                reasked.portfolio_view if reasked else "parse_error"
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
        logger.warning(
            "PM candidate accounting: re-ask log write failed: %s", e,
        )

    if reasked is None:
        _record_heal_safely(pipeline, ctx, HealResult(
            seat=_PM_ACCOUNTING_SEAT, outcome=HEAL_FAILED,
            reason="candidate-accounting re-ask returned no parseable decision",
            paid_retry=True, details={"symbols": pending},
        ))
        _finish(pending, asked=True)
        return

    # BOOKKEEPING ONLY. The re-ask's targets, sizes and reasoning_chain are
    # DISCARDED: the trade decision was made on the first call and a paid
    # retry must never become a way to re-decide the book. Only rejections
    # naming a CHALLENGED symbol are taken, and only where the first answer
    # had none — so the re-ask cannot overwrite a ground the seat already
    # stated, nor invent an accounting for a name it was not asked about.
    challenged = set(pending)
    already = {
        str(getattr(r, "symbol", "") or "").strip().upper()
        for r in (getattr(decision, "rejections", None) or [])
    }
    gained = [
        r for r in (getattr(reasked, "rejections", None) or [])
        if str(getattr(r, "symbol", "") or "").strip().upper() in challenged
        and str(getattr(r, "symbol", "") or "").strip().upper() not in already
    ]
    if gained:
        try:
            decision.rejections = list(
                getattr(decision, "rejections", None) or [],
            ) + gained
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "PM candidate accounting: could not attach the re-asked "
                "rejections to the decision (%s) — they are still recorded "
                "per symbol below", e,
            )

    second = account_for_candidates(
        analyses=[a for a in analyses
                  if str(getattr(a, "symbol", "") or "").strip().upper()
                  in challenged],
        decision=decision, positions=positions,
    )
    for accounted in second.accounted:
        _record_accounted_candidate(pipeline, ctx, accounted)
    still = sorted(second.unaccounted)
    logger.info(
        "PM candidate accounting: the re-ask accounted for %d of %d "
        "challenged candidate(s); %d still unaccounted",
        len(second.accounted), len(pending), len(still),
    )

    if not still:
        _record_heal_safely(pipeline, ctx, HealResult(
            seat=_PM_ACCOUNTING_SEAT, outcome=HEAL_PAID_RETRY,
            reason=(
                "the candidate-accounting re-ask named a ground for every "
                "candidate it was asked about"
            ),
            paid_retry=True, usable=True, details={"symbols": pending},
        ), alert=False)
        return

    _finish(still, asked=True)
    _record_heal_safely(pipeline, ctx, HealResult(
        seat=_PM_ACCOUNTING_SEAT, outcome=HEAL_FAILED,
        reason=(
            "the portfolio manager would not name a ground for: "
            + ", ".join(still) + ". Nothing was bought or sold differently "
            "because of this — what is lost is the desk's ability to say "
            "why these candidates were dropped. Recorded per symbol."
        ),
        paid_retry=True, details={"symbols": still},
        owner_consequence=(
            "NOTHING was bought, sold or held differently because of this — "
            "the decision itself was already made and stands. What is "
            "missing is the desk's account of why it passed on these names."
        ),
    ))
    # Mechanical-heal bookkeeping, so a reader can tell a session where the
    # seat answered from one where the code recovered the answer.
    if second.accounted:
        _record_heal_safely(pipeline, ctx, HealResult(
            seat=_PM_ACCOUNTING_SEAT, outcome=HEAL_MECHANICAL,
            reason=(
                f"{len(second.accounted)} candidate(s) accounted for after "
                "the re-ask"
            ),
            mechanical=True, paid_retry=True, usable=True,
        ), alert=False)


def _record_heal_safely(pipeline, ctx, result, alert: bool = True) -> None:
    """`TradingPipeline._record_heal`, but never fatal to the session.

    The accounting path is bookkeeping; a failure to WRITE the bookkeeping
    must not be able to end a live trading session.
    """
    try:
        pipeline._record_heal(ctx, result, alert=alert)
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "PM candidate accounting: could not record the heal result "
            "(%s): %s", getattr(result, "outcome", "?"), e,
        )


def _target_increase_missing_falsifier(
    target, *, positions=None, total_value: float = 0.0,
    existing_risk_pct=None,
) -> bool:
    """True iff this target is an open/increase still missing a real falsifier.

    Classification reuses `PortfolioManagerAgent._target_intent` (current
    size/risk vs the proposed target). Reductions and closes are never
    this check — a blank `thesis_invalid_if` on a checkable size-down
    is not a missing open falsifier. The constructor still must name a
    mechanical live-book warrant rather than PM thesis; that is not
    this function. Fail-safe matches `_target_intent`: unknown current
    risk is treated as an increase, the stricter gate. Never invents a
    string.
    """
    held = {
        str(getattr(p, "symbol", "")).upper(): p
        for p in list(positions or [])
        if getattr(p, "symbol", None)
    }
    intent = PortfolioManagerAgent._target_intent(
        target, held, total_value, existing_risk_pct=existing_risk_pct,
    )
    return open_target_missing_falsifier(target, intent=intent)


def _targets_admitted_to_book(
    targets, *, positions=None, total_value: float = 0.0,
    existing_risk_pct=None,
) -> tuple[list, list[str]]:
    """Open/increase names still missing a real falsifier never reach the constructor.

    Permanent never-blank path: heal + one paid retry already ran on the
    PM seat so it actually produces the field. Remaining blanks on
    opens/increases are refused here so they do not consume risk budget
    or become tickets. Reductions and closes with a blank
    `thesis_invalid_if` are admitted only when `_target_intent`
    classifies a checkable size-down vs the live book (risk/weight).
    Those are not soft-exits: the constructor stamps a mechanical
    size-down warrant as the named trigger. PM thesis free text cannot
    create the sell. Classification reuses `_target_intent`.
    That label can disagree with the constructor's order side when a
    lower risk request meets a tighter stop (more shares). The RiskStage
    isolate still drops any constructed BUY/SHORT whose falsifier is
    blank, so a mis-labelled add cannot be ticketed. That refuse is
    last-resort after the producing step was asked, not skip-and-continue
    as the product (owner 2026-09-17). Targets stay on the proposal so
    Risk is told why the narrative names a symbol that is not in the
    list. Never invents a falsifier or catalyst string.
    """
    admitted: list = []
    refused: list[str] = []
    for target in list(targets or []):
        if _target_increase_missing_falsifier(
            target, positions=positions, total_value=total_value,
            existing_risk_pct=existing_risk_pct,
        ):
            refused.append(str(target.symbol).upper())
        else:
            admitted.append(target)
    return admitted, refused


def _record_mechanical_soft_exit_restores(pipeline, ctx) -> None:
    """Write down what the MECHANICAL soft-exit heal did this run.

    Board item 78. `src.seat_heal.restore_stated_soft_exits` puts back a
    `thesis_invalid_if` that a later null-wipe blanked, using the sentence
    the model itself already wrote, and it never invents one. It runs
    inside a Pydantic validator, so it has no run id and no database
    handle and has never recorded a single thing. Two of item 78's three
    removal criteria are claims about this heal, so they could not be
    judged at all.

    RECORDING ONLY. Nothing reads these rows back into a trading
    decision and they may never be swept for a threshold. Never raises.
    """
    try:
        from src.seat_heal import drain_restore_observations

        observations, dropped = drain_restore_observations()
        if not observations:
            return
        db = getattr(pipeline, "db", None)
        writer = getattr(db, "record_soft_exit_heal_restores", None)
        if not callable(writer):
            return
        writer(
            observations=observations,
            run_id=getattr(ctx, "run_id", None),
            dropped=dropped,
        )
    except Exception as exc:  # noqa: BLE001 — a recording never blocks a trade
        logger.error("mechanical soft-exit heal recording failed: %s", exc)


def _record_soft_exit_heals(pipeline, ctx) -> None:
    """Drain the PM's per-name soft-exit heal outcomes onto ctx and to disk.

    Board item 78. The heal itself (`PortfolioManagerAgent.
    _fill_missing_open_falsifiers`, the desk's standing heal order:
    mechanical restore, then ONE paid seat retry under the existing
    per-seat cap) has four exits that used to leave nothing but a log
    line — never attempted for want of a replayable message, blocked by
    the spend cap, errored, or answered without a falsifier. A name can
    therefore be refused for a blank falsifier without any durable record
    of whether the seat was ever actually re-asked.

    One `pipeline_event` row per name fixes that, and `ctx.soft_exit_heals`
    carries the same fact forward so the refusal can quote what really
    happened instead of asserting a retry. Recording only; never raises.
    """
    _record_mechanical_soft_exit_restores(pipeline, ctx)
    try:
        agent = getattr(pipeline, "portfolio_manager", None)
        drain = getattr(agent, "drain_soft_exit_heals", None)
        heals = dict(drain() if callable(drain) else {})
    except Exception as exc:  # noqa: BLE001
        logger.error("soft-exit heal drain failed: %s", exc)
        return
    if not heals:
        return
    try:
        ctx.soft_exit_heals = {**(getattr(ctx, "soft_exit_heals", None) or {}), **heals}
    except Exception:  # noqa: BLE001
        pass
    for symbol, heal in heals.items():
        _record_pipeline_event(
            pipeline, ctx, symbol, "soft_exit_heal",
            str(heal.get("outcome") or "unknown"), SOFT_EXIT_HEAL_EVENT_REASON,
            detail=str(heal.get("detail") or ""),
        )


def _soft_exit_heal_detail(ctx, symbol: str) -> str:
    """The TRUE per-name heal outcome, for the refusal's durable reason.

    Board item 78 / owner 2026-09-25 ("untrue is a lie"): the refusal used
    to assert "after mechanical heal and one paid retry" for every name,
    including names whose retry was never attempted. It now states what the
    heal record says, and says plainly when there is no heal record at all.
    """
    heal = (getattr(ctx, "soft_exit_heals", None) or {}).get(
        str(symbol).strip().upper()
    )
    if isinstance(heal, dict) and (heal.get("detail") or heal.get("outcome")):
        return (
            f"heal outcome '{heal.get('outcome') or 'unknown'}': "
            f"{heal.get('detail') or ''}".strip()
        )
    return (
        "no soft-exit heal was recorded for this name — the mechanical "
        "restore did not fill it and no paid retry outcome was filed"
    )


def _record_soft_exit_missing_after_retry(
    pipeline, ctx, symbol: str, *, action: str | None = None,
) -> None:
    _record_pipeline_event(
        pipeline, ctx, symbol, "deterministic_gate",
        "blocked", SOFT_EXIT_MISSING_AFTER_RETRY,
        detail=(
            "thesis_invalid_if still empty or unknown; refusing this "
            "name before the book. No falsifier was invented. "
            + _soft_exit_heal_detail(ctx, symbol)
        ),
        heal_outcome=str(
            (
                (getattr(ctx, "soft_exit_heals", None) or {}).get(
                    str(symbol).strip().upper()
                )
                or {}
            ).get("outcome")
            or "none_recorded"
        ),
        **({"action": action} if action else {}),
    )


def _isolate_empty_soft_exit_entries(pipeline, ctx, portfolio_decision) -> list[str]:
    """Refuse BUY/SHORT names still missing a real falsifier after heal+retry.

    TEMPORARY last-resort (#432 isolate-name). Owner 2026-09-17: skip/drop
    is not the product — the producing step must fill. The permanent path
    is schema + prompt + mechanical heal + one paid seat retry, then
    refuse before construct_orders. This filter does not invent a
    thesis_invalid_if or catalyst string. It does not delete the target
    — Risk must still be told the name was proposed and refused.

    EXACTLY WHAT WOULD JUSTIFY DELETING IT (board item 78, 2026-09-26).
    It stays until a LIVE session record shows all three. None of the
    three can be shown offline: each is a claim about what the seats
    really emit when real money is at stake.
      1. `specialist_evidence` holds ZERO `pipeline_event` rows with
         reason `soft-exit missing after retry`, over a window of live
         sessions that actually produced BUY/SHORT targets — not a window
         in which the desk simply proposed nothing. Zero refusals across
         zero opens proves nothing. [measured 2026-09-26: 0 such rows in
         4,709 pipeline_event rows spanning 2026-09-02 to 2026-09-26, so
         criterion 1 alone is already met and is NOT sufficient.]
      2. Over that same window no `soft_exit_heal` row carries outcome
         `paid_retry`: the heal being needed and working is not the same
         as the producing step producing. This is the criterion item 78
         actually names — Tech and the PM emitting a real falsifier
         unaided.
      3. No `soft_exit_heal` row in that window carries `not_attempted`,
         `cap_blocked` or `failed`. Each of those says the desk does not
         yet know whether the seat can fill the field, so a zero refusal
         count in their presence is silence, not evidence.
    Until all three hold, this stays. The heal and the durable heal record
    above are what keep it unreachable in normal operation.

    Catches empty AND `unknown` on BUY/SHORT — omitted empty is missing,
    not "the analyst had nothing to say". Neutrals, reductions and closes
    are not this filter. A constructed BUY/SHORT with a blank falsifier
    is still dropped even when `_target_intent` labelled the target a
    reduction (risk-down / weight-up from a tighter stop).
    """
    if portfolio_decision is None:
        return []
    existing_risk_pct, _ = _book_risk_inputs(
        ctx, getattr(ctx, "total_value", 0.0) or 0.0,
    )
    missing_symbols = {
        str(target.symbol).upper()
        for target in list(getattr(portfolio_decision, "targets", None) or [])
        if _target_increase_missing_falsifier(
            target,
            positions=getattr(ctx, "positions", None),
            total_value=getattr(ctx, "total_value", 0.0) or 0.0,
            existing_risk_pct=existing_risk_pct,
        )
    }
    isolated: list[str] = []
    kept = []
    for decision in list(getattr(portfolio_decision, "decisions", None) or []):
        symbol = str(decision.symbol).upper()
        missing_here = (
            symbol in missing_symbols
            or (
                decision.action in ("BUY", "SHORT")
                and missing_stated_falsifier(
                    getattr(decision, "thesis_invalid_if", None)
                )
            )
        )
        if decision.action in ("BUY", "SHORT") and missing_here:
            isolated.append(symbol)
            _record_soft_exit_missing_after_retry(
                pipeline, ctx, decision.symbol, action=decision.action,
            )
            continue
        kept.append(decision)
    if not isolated:
        return []
    unique = list(dict.fromkeys(isolated))
    logger.warning(
        "Refusing %d BUY/SHORT name(s) %s: %s",
        len(unique), SOFT_EXIT_MISSING_AFTER_RETRY, unique,
    )
    portfolio_decision.decisions = kept
    existing = list(getattr(portfolio_decision, "constructor_dropped", None) or [])
    for symbol in unique:
        if symbol not in existing:
            existing.append(symbol)
    portfolio_decision.constructor_dropped = existing
    return unique
