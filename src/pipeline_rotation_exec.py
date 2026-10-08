"""Rotation EXECUTION — the order-placing half of opportunity-cost rotation.

Split out of ``src/pipeline_stages.py`` (pipeline split, step 12). Every
function below is the VERBATIM body from that file; nothing was renamed,
reordered or changed. ``src.pipeline_stages`` re-exports each name lazily,
so every existing import path and every ``src.pipeline_stages.<name>``
patch target keeps working.
"""

from __future__ import annotations

from src.sentinel.guarded import record_guarded_pass
from src.sizing_refusal import sizing_price_or_refusal
from src.pipeline_stages import (  # noqa: F401  shared helpers and module-level names
    _IN_FLIGHT_FILL_STATUSES,
    _entry_deployment_budget,
    _fractional_sizing_allowed,
    _live_fill_price,
    _qty_by_risk_budget,
    _record_execution_skip,
    _record_pipeline_event,
    _single_name_execution_cap,
    _size_shares,
    _today_sizing_price,
    logger,
    replace,
)

#: Sentinel returned by `_rotation_ranked_margin_sell_reason` when the SELL
#: must NOT be submitted. Distinct from `None`, which means "not a
#: ranked-margin rotation close — carry on as before".
_ROTATION_SELL_REFUSED = object()

def _rotation_execution_enabled(pipeline) -> bool:
    """Phase 14b — is automatic opportunity-cost rotation ON?

    True for an explicit `execution.rotation_enabled is True` and nothing
    else, the same convention as `_repeg_settings` below and for the same
    reason: many tests build the pipeline with a MagicMock config whose
    auto-attributes are truthy, and a MagicMock must never read as "yes,
    close a real position on your own".
    """
    execution_cfg = getattr(getattr(pipeline, "config", None), "execution", None)
    return getattr(execution_cfg, "rotation_enabled", None) is True

def _rotation_ranked_margin_enabled(pipeline) -> bool:
    """Board item 39 — is the RANKED-MARGIN rotation tier executable?

    Requires `execution.rotation_ranked_margin_enabled is True` IN ADDITION
    to `_rotation_execution_enabled`, and the same `is True` convention for
    the same reason (a MagicMock config must never read as "yes, close a
    real position on your own").

    This flag decides whether the tier is ASKED about. It does not decide
    whether a sale can be built: `src/rotation.py::rotation_sell_reason`
    raises unconditionally without a `RotationClearance`, which only
    `_rotation_buy_leg_projected_refusal` below mints, and only after the
    replacement BUY has survived every gate in `REQUIRED_BUY_LEG_GATES`
    against PROJECTED POST-SALE state.
    """
    if not _rotation_execution_enabled(pipeline):
        return False
    execution_cfg = getattr(getattr(pipeline, "config", None), "execution", None)
    return getattr(execution_cfg, "rotation_ranked_margin_enabled", None) is True

def _rotation_skip(pipeline, ctx, opportunity, reason: str, **details) -> None:
    """One durable `rotation` / `skipped` audit row. Every refusal to act
    lands here so the evening review can see WHY a surfaced comparison did
    not become a trade, rather than inferring it from a log line."""
    logger.info(
        "Rotation: not acting on %s -> %s (%s)",
        opportunity.held_symbol, opportunity.new_symbol, reason,
    )
    _record_pipeline_event(
        pipeline, ctx, opportunity.held_symbol, "rotation", "skipped", reason,
        new_symbol=opportunity.new_symbol, tier=opportunity.tier, **details,
    )

def _record_rotation_precheck(pipeline, ctx) -> None:
    """One durable `rotation` / `precheck` row per session, whatever the
    pre-check concluded — including when it concluded nothing.

    The gap this closes: `_apply_rotation_execution` returns silently when
    `precheck.opportunity is None`, and that silent return is the desk's
    COMMONEST rotation outcome — the book is full, every candidate was
    still ranked against what is held, and none of them won. It left no log
    line, no durable row and nothing in the owner's report, so a session
    that did the comparison looked identical to one that never made it.

    Runs regardless of `execution.rotation_enabled`: the comparison happens
    in the PM's own prompt either way, and whether the desk may ACT on it is
    a separate fact this row records rather than a reason to stay silent.
    Never raises — bookkeeping must not take a live session with it.
    """
    from src.rotation import RotationPrecheck, precheck_record

    try:
        precheck = getattr(
            getattr(pipeline, "portfolio_manager", None),
            "last_rotation_precheck", None,
        )
        if not isinstance(precheck, RotationPrecheck):
            return
        record = precheck_record(
            precheck,
            execute_enabled=_rotation_execution_enabled(pipeline),
            ranked_margin_enabled=_rotation_ranked_margin_enabled(pipeline),
        )
        logger.info(
            "Rotation pre-check: %s (headroom %.2f%% of a %.2f%% ceiling, "
            "binding [%s], %s vs %s at ratio %s%s)",
            record["outcome"], record["headroom_pct"], record["ceiling_pct"],
            record.get("binding", ""), record.get("held_symbol"),
            record.get("new_symbol"), record.get("ratio"),
            f", refused at {record['refusal_point']}"
            if record.get("refusal_point") else "",
        )
        # RUN-scoped, with the symbols in the payload. Scoping it to the
        # holding was tried and reverted on adversary review: this repo has
        # ruled three times (`src/execution/exit_path_records.py`, board
        # item 164, and the plan-edit rows in this file) that
        # `src/refusal_signature.py` reads EVERY symbol-scoped
        # `pipeline_event` as "this session considered that stock as a new
        # idea". A weakest HOLDING is not such a candidate, and this row
        # fires every session — it would have broken the monomorphic-refusal
        # streak on essentially every run and silently disarmed the jam
        # alarm. The near-miss fields are just as queryable in the payload.
        _record_pipeline_event(
            pipeline, ctx, None, "rotation", "precheck",
            record["outcome"], **{
                k: v for k, v in record.items() if k != "outcome"
            },
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Rotation pre-check record failed: %s", exc)

def _apply_rotation_execution(pipeline, ctx, portfolio_decision, positions,
                              position_history: dict | None) -> None:
    """Phase 14b — turn the CATEGORICAL rotation comparison into an ordinary
    zero-size PM target, when the desk's own data says it is safe to.

    Runs after the PM's plan is parsed and grounded and BEFORE
    `PortfolioConstructor.construct_orders`, so the close it proposes is
    built, risk-checked, reviewed and executed by exactly the machinery a
    PM-decided close goes through — `_build_sell`, the hard risk rules, the
    AI Risk Manager (which can refuse it by symbol), the holding-discipline
    claim check in `RiskStage`, and `_submit_protected_sell` in
    `ExecutionStage`. This function adds a target to the plan and records
    why; it never places an order and never removes anything the PM asked
    for. See `src/rotation.py` for the doctrine and the owner request.

    Every guard below fails CLOSED — an unanswerable question means no
    sale — and every refusal is recorded as a `rotation`/`skipped` event:

      1. `execution.rotation_enabled` must be explicitly True (else this
         function is a no-op and the run is byte-for-byte Phase 14).
      2. The PM's own prompt must have surfaced a comparison this session,
         and it must be the categorical tier. The ranked-margin tier is
         information only (see `src/rotation.py`).
      3. The held name must be a LONG the broker actually shows. KEPT, and
         re-examined under the 2026-10-01 ruling rather than inherited: this
         function only appends a zero-size `TargetPosition`, which
         `_build_sell` turns into a SELL, and the SELL execution loop
         hard-refuses a SELL on a short (`existing[0].qty <= 0: continue`).
         A short needs the separate COVER path, whose caps, protective
         BUY-stop cancel/replace and partial-fill restore have never been
         exercised from here. Removing the restriction today would therefore
         either do nothing or put an unverified order shape on live capital
         with the remainder's protection unproven — which the one-way
         protection rule forbids. It is a REPORTED GAP, not a silent one:
         below-bar SHORTS are recorded every session by
         `holdings_below_entry_bar` and skipped under their own audited
         reason, and automating the cover leg is its own piece of work.
      4. RANKED-MARGIN TIER ONLY: the PM must itself have targeted the new
         candidate with a real size this session. The CATEGORICAL tier no
         longer requires it (owner ruling 2026-10-01) — a holding below the
         desk's own entry bar is closed whether or not the book is full and
         whether or not anything is queued to replace it.
      5. The PM must not already be targeting the held symbol (closing it
         itself, or adding to it). Either way the model has spoken and
         this function does not override a decision the PM made.
      6. Nothing may be in flight on the held symbol: no trades row for
         it today still `submitted`/`pending_submit`, no BUY of it today at
         all (a day-zero exit "never given a single day's normal range to
         breathe" is exactly what the exit noise band exists to prevent —
         OKLO 2026-08-26), no pending protection-restore WAL row (a sell
         already mid-flight), no pending re-peg row (an entry mid-chase).
      7. RANKED-MARGIN TIER ONLY: its structural protection must ALREADY be
         broken under the item-25 holding-discipline check. That tier sells
         a still-ELIGIBLE name for opportunity cost, which is not one of the
         three real triggers. On the CATEGORICAL tier the holding is by
         definition no longer eligible, and the owner ruled on 2026-10-01
         that failing the fresh-entry bar IS the real trigger, so protection
         is measured and reported but does not veto.
    """
    if not _rotation_execution_enabled(pipeline):
        return
    from src.models import TargetPosition
    from src.rotation import RotationPrecheck, rotation_proposal_reason

    precheck = getattr(
        getattr(pipeline, "portfolio_manager", None), "last_rotation_precheck", None,
    )
    if not isinstance(precheck, RotationPrecheck) or precheck.opportunity is None:
        return  # nothing was surfaced this session — nothing to act on
    opportunity = precheck.opportunity
    held_symbol = opportunity.held_symbol.strip().upper()
    # OWNER RULING 2026-10-01. The categorical tier surfaces a below-bar
    # holding with NO replacement candidate, so `new_symbol` is None on
    # exactly the case that ruling created. Normalised to "" here: the
    # only reader below is the RANKED-MARGIN buy-leg precondition, which
    # matches it against target symbols, and "" matches none of them.
    new_symbol = (opportunity.new_symbol or "").strip().upper()

    if opportunity.tier == "ranked_margin":
        # Board item 39. Executable only behind its own second switch, and
        # even then this only PROPOSES the close: the sale is withdrawn
        # again in `ExecutionStage._run_session` unless the replacement BUY
        # clears every gate in `REQUIRED_BUY_LEG_GATES` against projected
        # post-sale state. Nothing here can put it on the wire.
        if not _rotation_ranked_margin_enabled(pipeline):
            _rotation_skip(
                pipeline, ctx, opportunity, "ranked_margin_tier_not_enabled",
            )
            return
    elif opportunity.tier != "ineligible_hold":
        _rotation_skip(
            pipeline, ctx, opportunity, "unknown_rotation_tier",
            tier_seen=str(opportunity.tier),
        )
        return

    targets = list(getattr(portfolio_decision, "targets", None) or [])
    # OWNER RULING 2026-10-01. The buy leg is NO LONGER a precondition for a
    # categorical sale. MEASURED: this tier fired 8 times (24-25 Sep) and
    # died 8 of 8 right here, because the tier only ran on a full book and
    # the prompt then told the model there was no room to buy — so the model
    # never wrote the buy and the sell was never proposed. A holding below
    # the entry bar is now closed on its own merits. The RANKED-MARGIN tier
    # keeps the precondition: it sells a still-eligible name purely to fund a
    # replacement, so without the replacement it has no reason at all.
    if opportunity.tier == "ranked_margin":
        new_targeted = any(
            t.symbol.upper() == new_symbol and not t.is_close for t in targets
        )
        if not new_targeted:
            _rotation_skip(
                pipeline, ctx, opportunity, "pm_did_not_target_new_candidate",
            )
            return

    def _sellable_this_run(cand_symbol: str, cand_reasons):
        """Every per-holding sell guard for ONE below-bar candidate.

        Returns `(held_position, protection, history, cand_opportunity)` when
        this name may be closed this run, or `None` after recording exactly
        why it may not — so the caller advances to the next-worst below-bar
        holding instead of abandoning the rotation (board item 39). Every
        guard here is a fact about THIS name only; the buy-leg precondition
        is checked once, above, because it does not depend on which held name
        makes the room.
        """
        cand_opp = replace(
            opportunity, held_symbol=cand_symbol, reasons=tuple(cand_reasons),
        )
        held_pos = next(
            (p for p in (positions or [])
             if (p.symbol or "").upper() == cand_symbol),
            None,
        )
        if held_pos is None or held_pos.qty <= 0:
            _rotation_skip(
                pipeline, ctx, cand_opp, "held_symbol_is_not_a_long_position",
                qty=getattr(held_pos, "qty", None),
            )
            return None
        if any(t.symbol.upper() == cand_symbol for t in targets):
            _rotation_skip(
                pipeline, ctx, cand_opp, "pm_already_targets_held_symbol",
            )
            return None

        # In flight? Read from the desk's own durable state machine. Any
        # failure to answer is a refusal to act on THIS name, never an
        # assumption of "clear".
        try:
            today_rows = pipeline.db.get_trades(
                symbol=cand_symbol, limit=50, today_only=True,
            )
            bought_today = any(
                str(r.get("action") or "").upper() == "BUY" for r in today_rows
            )
            in_flight_rows = [
                r for r in today_rows
                if str(r.get("fill_status") or "").lower()
                in _IN_FLIGHT_FILL_STATUSES
                and str(r.get("action") or "").upper() != "HOLD"
            ]
            pending_restores = [
                r for r in pipeline.db.get_pending_protection_restores()
                if str(r.get("symbol") or "").upper() == cand_symbol
            ]
            pending_repegs = [
                r for r in pipeline.db.get_pending_repegs()
                if str(r.get("symbol") or "").upper() == cand_symbol
            ]
            record_guarded_pass(pipeline, "rotation_exec.in_flight_check")
        except Exception as exc:  # noqa: BLE001
            record_guarded_pass(pipeline, "rotation_exec.in_flight_check", exc, log=logger)
            _rotation_skip(
                pipeline, ctx, cand_opp, "in_flight_check_failed",
                detail=str(exc),
            )
            return None
        if bought_today:
            _rotation_skip(
                pipeline, ctx, cand_opp, "held_symbol_bought_today",
            )
            return None
        if in_flight_rows:
            _rotation_skip(
                pipeline, ctx, cand_opp, "order_in_flight_on_held_symbol",
                detail="; ".join(
                    f"{r.get('action')}:{r.get('fill_status')}:"
                    f"{r.get('broker_order_id')}"
                    for r in in_flight_rows
                )[:400],
            )
            return None
        if pending_restores:
            _rotation_skip(
                pipeline, ctx, cand_opp, "sell_already_in_flight_wal_row",
                detail=str(pending_restores[0].get("sell_order_id")),
            )
            return None
        if pending_repegs:
            _rotation_skip(
                pipeline, ctx, cand_opp, "entry_repeg_in_flight",
                detail=str(pending_repegs[0].get("old_order_id")),
            )
            return None

        # Item-25 holding discipline: is the position still structurally
        # protected? Same method, same inputs `RiskStage` uses. A protected
        # (thesis-intact) name is NEVER sold — the walk passes OVER it to the
        # next below-bar name; it never overrides the discipline.
        cand_hist = (position_history or {}).get(cand_symbol) or (
            position_history or {}
        ).get(getattr(held_pos, "symbol", cand_symbol)) or {}
        try:
            cand_protection = pipeline._structural_protection_for_holding(
                symbol=cand_symbol,
                thesis_invalid_if=cand_hist.get("thesis_invalid_if"),
                entry_price=cand_hist.get("entry_price"),
                stop_loss=cand_hist.get("stop_loss"),
                is_short=False,
                run_id=ctx.run_id,
            )
            record_guarded_pass(pipeline, "rotation_exec.protection_check")
        except Exception as exc:  # noqa: BLE001
            record_guarded_pass(pipeline, "rotation_exec.protection_check", exc, log=logger)
            _rotation_skip(
                pipeline, ctx, cand_opp, "protection_check_failed",
                detail=str(exc),
            )
            return None
        if cand_protection.protected and cand_opp.tier == "ranked_margin":
            # RANKED-MARGIN ONLY. That tier sells a name that still PASSES the
            # desk's own entry gates, purely to fund something else, and
            # opportunity cost is not one of item 25's real triggers — so a
            # thesis-intact name is surfaced there, never sold.
            #
            # CATEGORICAL tier, owner ruling 2026-10-01: item 25 protects a
            # "still-eligible, thesis-intact" position. A holding in `blocked`
            # is NOT still-eligible — it fails the identical bar a fresh buy
            # must clear — and the owner has now ruled that failing that bar
            # IS a real trigger ("it has to earn its right to be there").
            # Keeping this conjunct would have meant a name could fail the
            # entry bar indefinitely with its stop intact and never be sold,
            # which is the state the ruling exists to end. The protection
            # state is still measured and is still stated in the sale's
            # reason and on the owner-facing surfaces; it no longer vetoes.
            # Downstream is consistent: `holding_discipline_claim_check`
            # blocks only a regime-flip or bearish-state-change CLAIM proven
            # false, and a rotation reason claims neither.
            _rotation_skip(
                pipeline, ctx, cand_opp, "held_symbol_structurally_protected",
                protection_basis=cand_protection.basis,
                protection_detail=str(cand_protection.detail)[:400],
            )
            return None
        return held_pos, cand_protection, cand_hist, cand_opp

    # Board item 39. Walk the below-bar cull set worst-first and close the
    # FIRST name that clears every per-holding guard. The rotation is
    # abandoned only when EVERY below-bar holding is unsellable this run —
    # not, as before, when the single worst name happened to be structurally
    # protected. `ineligible_candidates` is empty on the ranked-margin tier
    # and on a directly-constructed opportunity, so both fall back to the one
    # `held_symbol` and behave exactly as before.
    cull_set = (
        opportunity.ineligible_candidates
        if (opportunity.tier == "ineligible_hold"
            and opportunity.ineligible_candidates)
        else ((held_symbol, opportunity.reasons),)
    )
    chosen = None
    for cand_symbol, cand_reasons in cull_set:
        chosen = _sellable_this_run(
            str(cand_symbol).strip().upper(), cand_reasons,
        )
        if chosen is not None:
            break
    if chosen is None:
        # Every below-bar holding was unsellable this run; each was recorded
        # under its own reason above.
        return
    _held_pos, protection, hist, opportunity = chosen
    held_symbol = opportunity.held_symbol

    # A PROPOSAL, not an authorisation — see `rotation_proposal_reason`.
    # For the categorical tier this is byte-for-byte the string
    # `rotation_sell_reason` produced before; for the ranked-margin tier it
    # says in the Risk Manager's own input that the close is contingent on
    # the replacement BUY clearing its execution gates.
    reason = rotation_proposal_reason(
        opportunity,
        protection_basis=protection.basis,
        protection_detail=str(protection.detail),
        headroom_pct=precheck.headroom_pct,
        ceiling_pct=precheck.ceiling_pct,
        floor_pct=precheck.floor_pct,
        # 2026-09-23: so the clause naming why there was no room states the
        # limit that actually bound. Without this the sale's own reason
        # claims 14.50% is "under the 0.50% minimum" on a funding-bound
        # rotation — false, on the record the Risk Manager reads.
        binding=tuple(precheck.binding or ()),
        entry_budget_usd=precheck.entry_budget_usd,
        min_order_usd=precheck.min_order_usd,
    )
    # A zero-size target IS this desk's "close it" instruction
    # (`TargetPosition.is_close`; `_build_sell` turns it into a full SELL).
    # `thesis_invalid_if` is carried from the position's own entry record
    # so the built order's reasoning shows the condition the desk was
    # holding it against, exactly as a PM-authored close would.
    portfolio_decision.targets.append(TargetPosition(
        symbol=held_symbol,
        direction="long",
        risk_allocation_pct=0.0,
        conviction="high",
        thesis=reason,
        thesis_invalid_if=str(hist.get("thesis_invalid_if") or ""),
    ))
    # `None` when the ruling sold a below-bar holding with nothing queued to
    # replace it. Never a placeholder score — see `RotationOpportunity`.
    new_symbol = opportunity.new_symbol or None
    new_score = (
        float(opportunity.new_score)
        if opportunity.new_score is not None else None
    )
    ctx.rotation = {
        "held_symbol": held_symbol,
        "new_symbol": new_symbol,
        # Board item 39: the execution stage branches on this. A rotation
        # dict without it is treated as the categorical tier, which is what
        # every pre-item-39 caller meant.
        "tier": opportunity.tier,
        # Minted (or not) by `_rotation_sell_gate`, immediately before
        # the close is submitted. `None` here is not a
        # default that decays open: the SELL loop refuses a ranked-margin
        # rotation sale outright unless a real `RotationClearance` is
        # sitting in this slot.
        "clearance": None,
        "opportunity": opportunity,
        "protection_basis_text": protection.basis,
        "protection_detail_text": str(protection.detail),
        "floor_pct": float(precheck.floor_pct),
        # Carried onto the context so the SELL built at the wire states the
        # same binding constraint the PROPOSAL did — the two must not
        # disagree about why the room was gone.
        "binding": tuple(precheck.binding or ()),
        "entry_budget_usd": precheck.entry_budget_usd,
        "min_order_usd": precheck.min_order_usd,
        "new_score": new_score,
        "held_reasons": list(opportunity.reasons),
        "protection_basis": protection.basis,
        "protection_detail": str(protection.detail),
        "headroom_pct": float(precheck.headroom_pct),
        "ceiling_pct": float(precheck.ceiling_pct),
        "reason": reason,
    }
    logger.warning(
        "Rotation: proposing a full close of %s (below the desk's own entry "
        "bar; replacement candidate: %s) — %s",
        held_symbol, new_symbol or "none", reason,
    )
    _record_pipeline_event(
        pipeline, ctx, held_symbol, "rotation", "proposed", reason,
        new_symbol=new_symbol, new_score=new_score,
        held_reasons=list(opportunity.reasons),
        protection_basis=protection.basis,
        protection_detail=str(protection.detail)[:400],
        headroom_pct=float(precheck.headroom_pct),
        ceiling_pct=float(precheck.ceiling_pct),
        tier=opportunity.tier,
    )

def _projected_sale_qty(decision, position) -> float:
    """How many shares `decision` will actually take off `position`.

    Mirrors `ExecutionStage._run_session`'s own SELL sizing exactly —
    whole-share flooring on a whole-share position, the `>= position` full-
    exit promotion, the `allocation_pct == 0` ambiguity skip — because a
    projection that sized a sale differently from the loop that places it
    would be describing a book that never exists. Returns 0.0 for anything
    the loop would skip.
    """
    try:
        held_qty = float(getattr(position, "qty", 0.0) or 0.0)
    except (TypeError, ValueError):
        return 0.0
    # The action and the position's SIGN have to agree, exactly as the two
    # execution loops require. The SELL loop refuses a SELL on a short
    # (`existing[0].qty <= 0: continue`) and the COVER loop refuses a COVER
    # on a long — so a projection that closed either one would remove
    # exposure the real session keeps, and a book that keeps a losing
    # position the projection dropped is more negative than the projection
    # said. Both directions are unsafe; both are refused here.
    #
    # A blanket `abs()` was the over-correction of the opposite bug, where
    # sizing off the SIGNED quantity made every COVER a no-op.
    covering = str(getattr(decision, "action", "") or "").upper() == "COVER"
    if covering and held_qty >= 0:
        return 0.0  # a COVER on a long: the COVER loop skips it
    if not covering and held_qty <= 0:
        return 0.0  # a SELL on a short: the SELL loop skips it
    held_qty = abs(held_qty)
    if held_qty <= 0:
        return 0.0
    try:
        pct = float(getattr(decision, "allocation_pct", 0.0) or 0.0)
    except (TypeError, ValueError):
        return 0.0
    if pct == 0:
        # The loop logs this as ambiguous and skips it, so nothing is sold.
        return 0.0
    if 0 < pct < 100:
        qty = held_qty * (pct / 100.0)
        if float(held_qty).is_integer():
            qty = max(1.0, float(int(qty)))
        if qty <= 0:
            return 0.0
        return min(qty, held_qty)
    return held_qty

def _projected_post_sale_book(positions, total_value: float,
                              sell_decisions: list, cover_decisions: list):
    """The held book and the equity as they will be once THIS SESSION'S
    exits have gone through — the state the downstream gates will actually
    read, projected before any of them has been submitted.

    Board item 39. The gates that can refuse a rotation's replacement BUY
    are measured from the book the desk will be holding once the sale has
    gone through, not the one it holds while proposing it: the deployment
    budget reads the remaining positions' gross exposure and the remaining
    settled cash, and the sizing reads the equity those are measured
    against. A check fed PRE-sale state is blind to the state change it
    depends on, which is exactly why attempt 2 on this item was unsafe by
    construction, and why this builds the post-sale book instead of
    re-implementing the gates against the pre-sale one.

    **Exactly the exits it is handed are applied, and no others.** The
    rotation gate hands it ONE decision — the rotation's own close — and
    relies on a fresh `_refresh_account_state()` read for everything else
    the session has already done. Projecting the other exits would be
    guessing at fills nobody controls; measuring them is free, because the
    rotation's close is ordered last.

    Returns `(projected_positions, equity_for_weights)`.

      * `equity_for_weights` is `total_value` LESS the concession the
        marketable limits give up against the marks (0.995 for a SELL, its
        1.005 COVER mirror). A sale is otherwise mark-to-market neutral: it
        converts a marked position into the cash that position was already
        marked at, so the book's composition changes and its total does
        not. The concession is the one real equity effect of executing, it
        is knowable, and it is subtracted rather than ignored.
        **Direction, measured rather than assumed (adversary review,
        2026-09-23).** `config/settings.yaml` ships `allow_margin: true`, so
        `_entry_deployment_budget` returns `ceiling_x * equity - held_gross`
        and never reads cash at all. The order ceiling therefore falls by
        between 0.65 and 2.0 times any reduction in equity (the §11.2 rung
        and `max_position_pct: 65`), while the replacement's estimated cost
        falls only by the allocation percentage of it. Subtracting the
        concession TIGHTENS this gate; leaving it out loosens it. An
        earlier version of this docstring asserted the opposite and was
        wrong — it reasoned about the settled-cash branch, which the
        shipped configuration does not execute.

    Cover decisions are applied the same way: a COVER closes a short, which
    also removes that name from the held book.
    """
    from src.rotation import ROTATION_MARGIN_PCT  # noqa: F401  (module sanity)

    by_symbol = {}
    for decision in list(sell_decisions or []) + list(cover_decisions or []):
        symbol = str(getattr(decision, "symbol", "") or "").strip().upper()
        if symbol:
            by_symbol.setdefault(symbol, decision)

    projected = []
    concession = 0.0
    for position in positions or []:
        symbol = str(getattr(position, "symbol", "") or "").strip().upper()
        decision = by_symbol.get(symbol)
        if decision is None:
            projected.append(position)
            continue
        held_qty = float(getattr(position, "qty", 0.0) or 0.0)
        sold = _projected_sale_qty(decision, position)
        if sold <= 0:
            projected.append(position)
            continue
        try:
            price = float(getattr(position, "current_price", 0.0) or 0.0)
        except (TypeError, ValueError):
            price = 0.0
        if price > 0:
            # What the marketable limit gives up against the mark if it
            # fills at the limit. A SELL rests BELOW the mark and a COVER
            # BUYS back ABOVE it, so the cushion is applied in opposite
            # directions and costs the account in both. Rounded the same
            # way the loops round it so the two cannot drift.
            covering = str(getattr(decision, "action", "") or "").upper() == "COVER"
            # The SAME two factors `ExecutionStage._run_session` prices its
            # own exits at — 0.995 for a SELL, its 1.005 mirror for a COVER
            # (board item 138, `config/number_ledger.yaml`). No new number:
            # a projection that priced an exit differently from the loop
            # that places it would be describing a fill that never happens.
            limit = round(price * (1.005 if covering else 0.995), 2)
            concession += abs(limit - price) * abs(sold)
        remaining = abs(held_qty) - abs(sold)
        if remaining <= 0 or held_qty == 0:
            continue  # position gone
        projected.append(_scaled_position(position, remaining / abs(held_qty)))

    return projected, max(0.0, float(total_value) - concession)

def _scaled_position(position, remaining_fraction: float):
    """A copy of `position` holding `remaining_fraction` of what it holds.

    Used only for a PARTIAL exit. Quantity and market value scale by the
    fraction, because selling half a position leaves half of it on the
    books, and `gross_exposure` — which is what the deployment budget
    measures the remaining book with — reads market value. The P&L fields
    are scaled with them for consistency of the object rather than because
    any gate now reads them: the projected account day-change that used to
    read `unrealized_intraday_pnl` went with the account-level loss halt
    (PR #584, retired-ok). Any field this does not know about is carried
    through unchanged.

    `copy.copy` rather than a constructor call: `Position` is not stable
    across this repo's fixtures (several tests use simple stand-ins), and a
    projection helper must not be the thing that decides what a position
    class looks like.
    """
    import copy
    clone = copy.copy(position)
    for field_name in ("qty", "market_value", "unrealized_intraday_pnl",
                       "unrealized_pnl", "cost_basis"):
        value = getattr(clone, field_name, None)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        try:
            setattr(clone, field_name, float(value) * remaining_fraction)
        except Exception:  # noqa: BLE001
            # A frozen or property-backed field: leave it. The numerator
            # reads `unrealized_intraday_pnl`, and a field that could not
            # be scaled down is left at its FULL value, which overstates
            # the remaining book rather than understating it.
            continue
    return clone

def _projected_post_sale_cash(cash: float, positions, sell_decisions,
                              cover_decisions) -> float:
    """Settled cash once this session's exits have gone through — a LOWER
    bound, deliberately.

    Called with the rotation's own close and nothing else — every other
    exit is already reflected in the `cash` this is handed, because that
    number comes from a broker read taken after they ran.

    A SELL adds its limit proceeds; a COVER SPENDS cash to buy the borrowed
    shares back, so it is subtracted. The cash sweep is not modelled at
    all: it can only liquidate the park INTO cash, never out of it, so
    leaving it out can only understate what is deployable. Understating
    refuses a rotation that would have worked; overstating sells a position
    to fund an order that is then refused.

    This is live on the settled-cash branch of `_entry_deployment_budget`
    only — the ladder branch compares gross exposure and never reads cash.
    That branch is reached whenever the gross ceiling cannot be resolved,
    and whenever `allow_margin` is set back to false.
    """
    try:
        cash = float(cash or 0.0)
    except (TypeError, ValueError):
        cash = 0.0
    by_symbol = {}
    for decision in list(sell_decisions or []) + list(cover_decisions or []):
        symbol = str(getattr(decision, "symbol", "") or "").strip().upper()
        if symbol:
            by_symbol.setdefault(symbol, decision)
    for position in positions or []:
        symbol = str(getattr(position, "symbol", "") or "").strip().upper()
        decision = by_symbol.get(symbol)
        if decision is None:
            continue
        sold = _projected_sale_qty(decision, position)
        if sold <= 0:
            continue
        try:
            price = float(getattr(position, "current_price", 0.0) or 0.0)
        except (TypeError, ValueError):
            continue
        if price <= 0:
            continue
        covering = str(getattr(decision, "action", "") or "").upper() == "COVER"
        limit = round(price * (1.005 if covering else 0.995), 2)
        cash += (-1.0 if covering else 1.0) * limit * abs(sold)
    return cash

def _projected_entry_cost(decision, equity: float, *,
                          budget_is_gross: bool) -> float:
    """What an entry earlier in the same session will take out of the
    deployment pool before the rotation's own buy reaches it.

    A SHORT is never SIZED by the entry budget (D11), but it still DRAWS
    the pool whenever that pool is the ladder's gross headroom rather than
    settled cash — the submit loop's own rule is
    `if budget_is_gross or not is_short: entry_budget -= estimated_cost`,
    because a short occupies gross exactly as a long does. Excluding it
    outright over-stated the pool by the whole short, which is the unsafe
    direction: the projection clears, reality refuses, and the position has
    already been sold.

    The charge is the full allocation, which is an UPPER bound on the real
    draw (the submit loop takes `min(qty_by_alloc, qty_by_risk)` and every
    later adjustment moves the quantity down, and it only draws at all once
    the broker accepts). Over-charging shrinks the pool, which refuses a
    rotation that would have worked — the side to be wrong on.
    """
    if (str(getattr(decision, "action", "") or "").upper() == "SHORT"
            and not budget_is_gross):
        return 0.0
    try:
        allocation_pct = float(getattr(decision, "allocation_pct", 0.0) or 0.0)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, float(equity) * allocation_pct / 100.0)

def _rotation_buy_leg_projected_refusal(pipeline, ctx, *, rotation,
                                        buy_decision, positions,
                                        total_value: float,
                                        rotation_sell,
                                        cash: float = 0.0,
                                        buy_decisions_before: list | None = None):
    """Would the rotation's replacement BUY be refused downstream, judged
    against the book as it will be AFTER this session's exits?

    Returns `(clearance, reason, detail)`: exactly one of `clearance` (a
    `src.rotation.RotationClearance`) and `reason` is not None.

    Board item 39. Every gate below is the SAME computation the execution
    stage runs later, fed projected post-sale inputs — not a second
    implementation of it.

    The list below is FOUR long and so is `REQUIRED_BUY_LEG_GATES`; a
    `gate_coverage_incomplete` refusal at the bottom of this function is
    what keeps the two from drifting, and this paragraph is the third copy
    of the count, so change all three together. It was six until
    2026-09-23, when the owner's removal of the account-level loss halt
    (PR #584) deleted the `daily_loss_recheck` refusal (retired-ok) the
    first gate anticipated, and five until 2026-09-24, when `below_min_notional`
    (retired-ok) was deleted the same way: the flat $500 `min_order_usd`
    notional floor it named was arbitrary, not a broker minimum, and Alpaca
    charges no stock commission, so `_prevent_rotation_naked_sale` no
    longer refuses a rotation's replacement buy for re-sizing small but
    nonzero — only a genuine zero still refuses, via `insufficient_cash`.
    Nothing replaced either deleted gate: the gross exposure the loss halt
    shared with the §11.2 ladder is still gated, by `insufficient_cash`.

      * `no_price` / `stale_entry` — `_live_fill_price` and the same 5%
        deviation test the preflight applies. Neither depends on the sale
        at all, so these are exact now, not projected.
      * `qty_zero` — the preflight's own sizing helpers (`_size_shares`,
        `_qty_by_risk_budget`, `_fractional_sizing_allowed`) at the
        projected equity.
      * `insufficient_cash` — `_entry_deployment_budget` over the
        projected post-sale positions, equity and settled cash, drained by
        every entry earlier in the same session exactly as the submit loop
        drains it. This is what still carries the §11.2 gross ladder: the
        budget's ladder-backed branch measures the headroom the sale
        frees, so a rotation that would breach gross is refused here even
        though the account-level alarm is gone.

    **What this does NOT close, stated plainly.** The BUY submit loop can
    still refuse an entry for reasons no pre-check can evaluate in advance:
    the latency window (`latency_window`), the borrow gate on a SHORT, and
    any broker rejection. (Board item 183, 2026-09-30: a displayed quote
    through the entry ceiling is no longer one of them — it is recorded as
    `venue_quote_through_ceiling` and the order is still sent, because the
    limit is its own protection.) Those are not knowable before the sale, and the tape
    can also move between this projection and the real check. This gate
    removes the deterministic, knowable refusals — the ones that made the
    naked-sale outcome reproducible — and the residual is recorded in the
    PR and in `docs/INCIDENT_HISTORY.md` rather than papered over.
    """
    from src.rotation import REQUIRED_BUY_LEG_GATES, RotationClearance

    symbol = str(getattr(buy_decision, "symbol", "") or "").strip().upper()
    checked: list[str] = []

    # ONE projection, over the rotation's own close and nothing else.
    #
    # An earlier version projected the other exits this session too, and
    # then tried to bound the uncertainty by evaluating two books — "all
    # exits fill" and "only the rotation's fills". That is not a bound: the
    # worst case is per-position (an exit carrying an intraday LOSS fails
    # to fill while one carrying a GAIN fills), and that mixed book is
    # neither of the two. Sampling two points of 2^N and calling it
    # conservative is the same mistake as attempt 2, one level up.
    #
    # It is also unnecessary. This gate now runs from inside the SELL loop,
    # immediately before the rotation's own close is submitted, and the
    # rotation's close is ordered LAST among this session's exits. By the
    # time it is reached every other exit has a terminal status and the
    # account has been refreshed, so the other exits are a MEASUREMENT in
    # `positions`, not an assumption. The only thing left to project is the
    # one sale that has not happened yet — which is the thing a projection
    # is actually good for.
    # `positions` is a broker read taken after every other SELL this
    # session reached a terminal status, so everything else the session did
    # is already in it. A session with a COVER still pending never reaches
    # this function at all (`_pending_cover_symbols`), so the only thing
    # left to project is the one sale that has not happened yet.
    projected_positions, equity_for_weights = _projected_post_sale_book(
        positions, total_value,
        [rotation_sell] if rotation_sell is not None else [], [],
    )

    # --- gates 1/2: price and entry staleness, exact now -----------------
    checked.append("no_price")
    market_price = _live_fill_price(pipeline, symbol)
    if not isinstance(market_price, (int, float)) or isinstance(
        market_price, bool,
    ) or market_price <= 0:
        return None, "no_price", (
            "no verifiable live price for the replacement buy (daily bar "
            "close is not a fill reference)"
        )
    market_price = float(market_price)
    # docs/WORK.md item 120: the SHARE COUNT divides the dollar allocation by
    # the sizing price, so it must be a real TODAY PRINT, never a quote mid
    # or a prior-session trade. `market_price` above (the fill reference) may
    # be a quote mid by design; the sizing divisor may not. Folded into the
    # `no_price` gate so `REQUIRED_BUY_LEG_GATES` coverage is unchanged.
    # An UNREADABLE price is refused too, its detail naming
    # `sizing_price_unreadable` so it never reads as a measured absence.
    sizing_print, _why, detail = sizing_price_or_refusal(
        _today_sizing_price, pipeline, symbol, "replacement buy",
    )
    if sizing_print is None:
        return None, "no_price", detail

    checked.append("stale_entry")
    try:
        entry_price = float(getattr(buy_decision, "entry_price", 0.0) or 0.0)
    except (TypeError, ValueError):
        entry_price = 0.0
    if entry_price > 0:
        deviation = abs(entry_price - market_price) / market_price
        if deviation > 0.05:
            return None, "stale_entry", (
                f"entry ${entry_price:.2f} is {deviation * 100:.1f}% from "
                f"market ${market_price:.2f} (threshold 5%)"
            )

    # --- gate 3: does the replacement round to a tradeable size? ---------
    checked.append("qty_zero")
    # Size off the TODAY PRINT (item 120), bounded conservatively by the
    # already-approved entry — never off the fill-reference mid.
    sizing_price = max(sizing_print, entry_price or 0.0)
    is_short = getattr(buy_decision, "action", "BUY") == "SHORT"
    fractional = _fractional_sizing_allowed(
        pipeline, symbol, is_short=is_short,
    )
    try:
        allocation_pct = float(
            getattr(buy_decision, "allocation_pct", 0.0) or 0.0,
        )
    except (TypeError, ValueError):
        allocation_pct = 0.0
    qty = _size_shares(
        pipeline,
        (float(equity_for_weights) * allocation_pct / 100.0) / sizing_price,
        fractional=fractional,
    )
    if qty <= 0:
        return None, "qty_zero", (
            f"allocation {allocation_pct:.2f}% at ${sizing_price:.2f} "
            f"rounds to zero shares"
        )
    risk_qty = _qty_by_risk_budget(
        pipeline, total_value=float(equity_for_weights),
        sizing_price=sizing_price,
        stop_price=getattr(buy_decision, "stop_loss", 0.0),
        is_short=is_short, fractional=fractional,
    )
    if risk_qty is not None and risk_qty < qty:
        qty = risk_qty
    if qty <= 0:
        return None, "qty_zero", (
            f"risk budget at ${sizing_price:.2f} entry / "
            f"${getattr(buy_decision, 'stop_loss', 0.0)} stop rounds to zero "
            f"shares"
        )

    # --- gates 4/5: can the post-sale book actually FUND the replacement? -
    # Added after adversary review of attempt 3. These are the refusals a
    # rotation is MOST likely to hit, because a rotation only surfaces when
    # the risk headroom is already under the floor — and risk-based sizing
    # can ask for more notional than the sale frees whenever the new name's
    # stop is tighter than the old one's. Both are deterministic functions
    # of the post-sale book, so both belong here rather than in the
    # "unknowable" residual.
    #
    # The budget is deliberately a LOWER BOUND: it is measured on the
    # projected book with the projected cash and WITHOUT the cash sweep's
    # help. The sweep can only add deployable cash, never remove it, so a
    # replacement that fits here fits the real budget too. Being wrong in
    # this direction refuses a rotation that would have worked, which costs
    # an opportunity; being wrong the other way sells a position to fund an
    # order that is then refused, which costs the position.
    checked.append("insufficient_cash")
    projected_cash = _projected_post_sale_cash(
        cash, positions,
        [rotation_sell] if rotation_sell is not None else [], [],
    )
    try:
        entry_budget, ladder_backed, budget_note = _entry_deployment_budget(
            pipeline, ctx, projected_positions, float(equity_for_weights),
            projected_cash,
        )
    except Exception as exc:  # noqa: BLE001
        return None, "insufficient_cash", (
            f"the post-sale entry budget could not be measured ({exc}), so "
            f"the replacement buy cannot be cleared"
        )
    # Earlier entries in the same session drain the pool before this one
    # reaches it — the submit loop subtracts each order's cost as it goes,
    # so the projection walks the same order.
    for earlier in buy_decisions_before or []:
        entry_budget -= _projected_entry_cost(
            earlier, float(equity_for_weights),
            budget_is_gross=bool(ladder_backed),
        )
    single_name_cap = _single_name_execution_cap(
        pipeline, float(equity_for_weights),
    )
    order_ceiling = min(entry_budget, single_name_cap)
    estimated_cost = qty * sizing_price
    if not is_short and estimated_cost > order_ceiling:
        affordable_qty = _size_shares(
            pipeline, order_ceiling / sizing_price, fractional=fractional,
        )
        if affordable_qty <= 0:
            return None, "insufficient_cash", (
                f"estimated cost ${estimated_cost:.2f} exceeds the "
                f"${order_ceiling:.2f} deployable on the post-sale book "
                f"({budget_note})"
            )
        # Fixed 2026-09-24 (retired the `below_min_notional` gate outright,
        # see `REQUIRED_BUY_LEG_GATES`): this used to refuse the rotation
        # whenever re-sizing to the post-sale budget landed under the flat
        # `min_order_usd` floor — an arbitrary $500 with no broker minimum
        # behind it, and Alpaca charges no stock commission. A rotation
        # whose replacement buy re-sizes small but nonzero
        # (`affordable_qty > 0`, already checked above) is cleared, not
        # refused; the real "no shares fit" case is `insufficient_cash`
        # above.

    missing = [g for g in REQUIRED_BUY_LEG_GATES if g not in checked]
    if missing:
        # Unreachable while this function and `REQUIRED_BUY_LEG_GATES` agree.
        # It is here so that they cannot silently stop agreeing: a gate added
        # to the list and not to this function refuses the sale rather than
        # clearing it on a check that was never run.
        return None, "gate_coverage_incomplete", (
            f"gates not evaluated: {', '.join(missing)}"
        )
    clearance = RotationClearance(
        held_symbol=str(rotation.get("held_symbol") or ""),
        new_symbol=symbol,
        gates_checked=tuple(checked),
        projected_entry_budget=float(order_ceiling),
        # Truncated: this string reaches `rotation_sell_reason`, whose text
        # is cut at `ROTATION_REASON_MAX_CHARS` with the clearance clause
        # LAST, so an over-long note would delete the very thing it
        # documents.
        projected_budget_basis=str(budget_note)[:60],
        projected_positions=tuple(
            str(getattr(p, "symbol", "") or "").strip().upper()
            for p in projected_positions
        ),
        projected_equity=float(equity_for_weights),
    )
    return clearance, None, None

def _pending_cover_symbols(cover_decisions, positions) -> tuple[str, ...]:
    """The symbols this session will actually COVER when the rotation's
    close is gated, in the order they will be covered.

    **A cover that cannot move the book does not count.** The COVER loop
    refuses two classes outright — a COVER against a symbol that is not
    held short (`existing[0].qty >= 0`), and one with
    `allocation_pct == 0` — and both take zero shares off the book, change
    no intraday P&L and move no gross. Counting them would refuse the
    rotation for a cause with no effect, every session the PM keeps
    proposing that dead cover, which is a statement about the desk rather
    than about the market. `_projected_sale_qty` applies the same sign and
    allocation agreement the COVER loop itself applies, so the filter here
    and the loop there cannot drift apart.

    Board item 39. The COVER loop runs AFTER the SELL loop, so at gate time
    no cover has happened and none can be measured — unlike the other
    SELLs, which the reorder makes measurable.

    **This refusal has outlived the argument that produced it, and that is
    recorded here rather than papered over (adversary review, 2026-09-23).**
    It was adopted because one projected book could not be the worst case
    for three disagreeing consumers: the daily-loss numerator, the
    volatility-relative threshold, and gross exposure. The owner's removal
    of the account-level loss halt (PR #584) deleted the first two. The
    survivor, `_entry_deployment_budget`, turns out to be cheaply boundable
    in both of its regimes: with `allow_margin: true` — what ships — it
    returns `ceiling_x * equity - held_gross` and never reads cash, a COVER
    lowers held gross, so simply NOT applying any cover is already the
    minimum headroom; with margin off it returns `min(headroom, cash)` and
    both terms are monotone in the set of covers assumed, so the lower
    bound over every fill outcome is `min(headroom with no covers, cash
    with all covers)` — two scalars, no enumeration.

    The refusal is kept anyway, and on one ground only: it is strictly the
    more conservative posture, `execution.rotation_ranked_margin_enabled`
    ships FALSE, and loosening a live-selling gate is not something to do
    in the same change that resolves a merge. Whoever turns the flag on
    should take the bound above instead of inheriting this. What must NOT
    be inherited is the old justification, which claimed the bound was
    impossible; it is not, and saying so kept a refusal standing on a
    reason that no longer exists.

    So today the desk does not guess: a rotation is refused outright on any
    session with a cover pending. That costs a rotation on cover days and
    fails toward not trading, which is the direction every other guard on
    this path fails in.
    """
    by_symbol = {
        str(getattr(p, "symbol", "") or "").strip().upper(): p
        for p in (positions or [])
    }
    symbols = []
    for decision in cover_decisions or []:
        symbol = str(getattr(decision, "symbol", "") or "").strip().upper()
        if not symbol or symbol in symbols:
            continue
        position = by_symbol.get(symbol)
        if position is None:
            continue  # nothing held: the COVER loop skips it
        if _projected_sale_qty(decision, position) <= 0:
            continue  # a no-op cover: it cannot move anything to bound
        symbols.append(symbol)
    return tuple(symbols)

from src.rotation_sell_order import _rotation_sell_last  # noqa: E402,F401

def _rotation_sell_gate(pipeline, ctx, decision, buy_decisions, positions,
                        total_value: float, cash: float,
                        cover_decisions: list | None = None):
    """Board item 39 — may this RANKED-MARGIN rotation close be submitted?

    Returns `None` for any SELL that is not a ranked-margin rotation close;
    every pre-item-39 path lands there and is unaffected. Otherwise returns
    `(cleared, positions, total_value, cash)`, with a REFRESHED book the
    caller must adopt — clearing the gate against a fresh book while the
    loop then sizes and prices off a stale one would reintroduce, one
    statement later, exactly the divergence `_projected_sale_qty` exists to
    prevent.

    **Why it lives inside the SELL loop rather than ahead of it.** The
    rotation's close is ordered last (`_rotation_sell_last`), so by the
    time this runs every other SELL this session has been submitted and
    waited on to a terminal status. Re-reading the account here therefore
    MEASURES what those did instead of assuming it.

    COVERs are the exception: their loop runs AFTER this one, so no cover
    has happened, none can be measured, and — see `_pending_cover_symbols`
    — none can honestly be bounded either. A session with a cover pending
    refuses the rotation outright.

    An earlier version ran ahead of the whole loop and projected the other
    exits too, bounding the uncertainty by evaluating two books ("all exits
    fill" and "only this one fills"). That is not a bound — the worst case
    is per-position and is neither book — and it also put this gate in
    direct conflict with `apply_gross_ceiling`, which nets out EVERY
    planned exit when it sizes the replacement. Measuring removes both
    problems.

    `cleared=False` means: do not submit this close. The replacement BUY
    then dies on the existing `_drop_rotation_buy_if_room_not_freed` path,
    because `rotation["sell_order_id"]` is never set — the room it was
    granted was never freed. Both legs fall together and the desk simply
    keeps the position.

    Scope: the RANKED-MARGIN tier only. The categorical tier
    (`ineligible_hold`, already live) sells a holding that fails the desk's
    own entry rules today — on that ground alone. OWNER RULING 2026-10-01
    removed the broken-protection conjunct: a name may now be cut with its
    structural protection still intact, because a position that would not
    be bought today has to earn its place regardless of where its stop is.
    The protection state is still measured, still carried in the sale's
    reason and still said to the owner; it no longer vetoes. That sale
    stands on its own and nothing here touches it.
    """
    rotation = getattr(ctx, "rotation", None)
    if not isinstance(rotation, dict) or rotation.get("tier") != "ranked_margin":
        return None
    held_symbol = str(rotation.get("held_symbol") or "").strip().upper()
    new_symbol = str(rotation.get("new_symbol") or "").strip().upper()
    if str(getattr(decision, "symbol", "") or "").strip().upper() != held_symbol:
        return None

    def _withdraw(reason: str, detail: str):
        logger.warning(
            "Rotation withdrawn BEFORE the sell (%s): %s. %s is NOT sold — "
            "the desk keeps the position rather than going naked.",
            reason, detail, held_symbol,
        )
        _record_pipeline_event(
            pipeline, ctx, held_symbol, "rotation", "withdrawn", reason,
            new_symbol=new_symbol, detail=str(detail)[:400],
            tier="ranked_margin",
        )
        _record_execution_skip(
            pipeline, ctx, held_symbol, "rotation_withdrawn", str(detail)[:400],
        )
        # `ctx.rotation` is MARKED withdrawn, not cleared. Clearing it would
        # make `_drop_rotation_buy_if_room_not_freed` a no-op, and the
        # replacement BUY — which the constructor sized on the premise that
        # this close frees room — would then go out against room that was
        # never freed, putting the book over the risk ceiling. Leaving the
        # dict in place with no `sell_order_id` is exactly the state that
        # function already reads as "not submitted, no room freed".
        rotation["withdrawn"] = reason
        rotation["clearance"] = None

    pending_covers = _pending_cover_symbols(cover_decisions, positions)
    if pending_covers:
        _withdraw(
            "cover_pending",
            f"this session still intends to cover "
            f"{', '.join(pending_covers)}, and the COVER loop runs after "
            f"this one — their effect on the daily-loss limit cannot be "
            f"measured yet and cannot be bounded either (the numerator, the "
            f"volatility-relative threshold and gross exposure each have a "
            f"different worst case). The desk does not guess: "
            f"{held_symbol} is kept.",
        )
        return False, positions, total_value, cash

    buy_leg = next(
        (d for d in (buy_decisions or [])
         if str(getattr(d, "symbol", "") or "").strip().upper() == new_symbol),
        None,
    )
    if buy_leg is None:
        _withdraw(
            "buy_leg_absent",
            f"the replacement buy of {new_symbol} is not in this session's "
            f"orders, so closing {held_symbol} would free room for nothing",
        )
        return False, positions, total_value, cash

    # Re-read the account. This is a measurement of everything the session
    # has already done, and the book the projection below starts from. A
    # read it cannot make is a refusal to act — the posture every other
    # guard on this path takes.
    try:
        account, positions, price_map = pipeline._refresh_account_state()
        total_value = float(
            account["portfolio_value"] if isinstance(account, dict)
            else getattr(account, "portfolio_value", total_value)
        )
        cash = float(
            account["cash"] if isinstance(account, dict)
            else getattr(account, "cash", cash)
        )
    except Exception as exc:  # noqa: BLE001
        _withdraw(
            "account_refresh_failed",
            f"close withheld: the post-sale book could not be projected "
            f"from a current account state ({exc})",
        )
        return False, positions, total_value, cash

    held = next(
        (p for p in (positions or [])
         if str(getattr(p, "symbol", "") or "").strip().upper() == held_symbol),
        None,
    )
    if held is None or float(getattr(held, "qty", 0.0) or 0.0) <= 0:
        # The refreshed book no longer holds it (a stop filled, a
        # broker-side close). The SELL loop would drop it silently two
        # statements below; a candidate must not leave this pipeline
        # without a durable, per-symbol reason.
        _withdraw(
            "held_position_gone",
            f"{held_symbol} is no longer held on a current broker read, so "
            f"there is nothing to close and no room to free for {new_symbol}",
        )
        return False, positions, total_value, cash

    clearance, reason, detail = _rotation_buy_leg_projected_refusal(
        pipeline, ctx, rotation=rotation, buy_decision=buy_leg,
        positions=positions, total_value=total_value, cash=cash,
        rotation_sell=decision,
        # The submit loop walks `buy_decisions` in order and subtracts each
        # order's cost from the pool as it goes, so everything ahead of the
        # rotation's own buy has already drawn the budget down by the time
        # it is reached.
        buy_decisions_before=list(
            buy_decisions[:buy_decisions.index(buy_leg)],
        ),
    )
    if clearance is None:
        _record_execution_skip(
            pipeline, ctx, new_symbol, str(reason),
            f"rotation buy leg refused on projected post-sale state: {detail}",
        )
        _withdraw(
            f"buy_leg_would_be_refused:{reason}",
            f"close withheld because the replacement buy of {new_symbol} "
            f"would be refused ({reason}: {detail})",
        )
        return False, positions, total_value, cash

    rotation["clearance"] = clearance
    _record_pipeline_event(
        pipeline, ctx, held_symbol, "rotation", "buy_leg_cleared",
        "projected_post_sale_gates_passed", new_symbol=new_symbol,
        gates=list(clearance.gates_checked),
        projected_entry_budget=clearance.projected_entry_budget,
        projected_budget_basis=clearance.projected_budget_basis,
        projected_positions=list(clearance.projected_positions),
        projected_equity=clearance.projected_equity,
        tier="ranked_margin",
    )
    return True, positions, total_value, cash

def _rotation_ranked_margin_sell_reason(pipeline, ctx, decision):
    """The last barrier in front of a RANKED-MARGIN rotation close.

    Returns:
      * `None` — this SELL is not a ranked-margin rotation close. Every
        pre-item-39 path lands here and is unaffected.
      * a `str` — the close is permitted, and this is the reason built from
        the `RotationClearance` that permitted it.
      * `_ROTATION_SELL_REFUSED` — do not submit this SELL.

    Board item 39, and the reason it is written this way. Attempt 1 on this
    item took the structural barrier that made a ranked-margin sale
    impossible to BUILD and replaced it with a config boolean, so one truthy
    value anywhere in the settings chain was sufficient to put a real sale
    on the wire. The barrier here is `src/rotation.py::rotation_sell_reason`
    raising `ValueError` without a `RotationClearance` — an object carrying
    the projected post-sale numbers the replacement buy was cleared on, that
    no configuration can produce. This function reads no flag at all; it
    only asks whether the evidence exists, and refuses the sale when the
    answer raises.
    """
    from src.rotation import RotationOpportunity, rotation_sell_reason

    rotation = getattr(ctx, "rotation", None)
    if not isinstance(rotation, dict) or rotation.get("tier") != "ranked_margin":
        return None
    symbol = str(getattr(decision, "symbol", "") or "").strip().upper()
    if symbol != str(rotation.get("held_symbol") or "").strip().upper():
        return None
    opportunity = rotation.get("opportunity")
    if not isinstance(opportunity, RotationOpportunity):
        logger.error(
            "Rotation SELL of %s REFUSED: the run carries no rotation "
            "opportunity to build a reason from. The position is kept.",
            symbol,
        )
        _record_pipeline_event(
            pipeline, ctx, symbol, "rotation", "sell_refused",
            "no_opportunity_on_context", new_symbol=rotation.get("new_symbol"),
        )
        return _ROTATION_SELL_REFUSED
    try:
        return rotation_sell_reason(
            opportunity,
            protection_basis=str(rotation.get("protection_basis_text") or ""),
            protection_detail=str(rotation.get("protection_detail_text") or ""),
            headroom_pct=float(rotation.get("headroom_pct") or 0.0),
            ceiling_pct=float(rotation.get("ceiling_pct") or 0.0),
            floor_pct=float(rotation.get("floor_pct") or 0.0),
            clearance=rotation.get("clearance"),
            binding=tuple(rotation.get("binding") or ()),
            entry_budget_usd=rotation.get("entry_budget_usd"),
            min_order_usd=rotation.get("min_order_usd"),
        )
    except ValueError as exc:
        logger.error(
            "Rotation SELL of %s REFUSED at the wire: %s. The position is "
            "kept — the desk does not sell into an uncleared replacement.",
            symbol, exc,
        )
        _record_pipeline_event(
            pipeline, ctx, symbol, "rotation", "sell_refused",
            "no_clearance", new_symbol=rotation.get("new_symbol"),
            detail=str(exc)[:400],
        )
        _record_execution_skip(
            pipeline, ctx, symbol, "rotation_sell_refused", str(exc)[:400],
        )
        return _ROTATION_SELL_REFUSED

def _alert_rotation_executed(*, rotation: dict, qty: float, limit_price: float,
                             order_id: str | None) -> None:
    """Standalone owner alert: the desk closed a position ON ITS OWN.

    Same path and shape as the naked-position, re-peg-exhausted and
    holding-discipline-block alerts (`notifier.send_owner_alert`): its own
    Telegram message, never bundled into the run summary; severity in plain
    words, never colour alone. Fired the moment the sale is broker-accepted
    — that is the irreversible act, and a position sold automatically must
    never be silent. Never raises.
    """
    try:
        held = rotation["held_symbol"]
        new = rotation.get("new_symbol") or None
        rules = "; ".join(rotation.get("held_reasons") or []) or "entry rules"
        protection = str(rotation.get("protection_basis") or "not measured")
        room_for = f" to free room for {new}" if new else ""
        body = (
            "POSITION CLOSED AUTOMATICALLY — OPPORTUNITY ROTATION\n"
            f"{held}: the desk submitted a SELL of {qty:g} share(s) at limit "
            f"${limit_price:,.2f} (broker order {order_id}){room_for}.\n"
            f"Why {held}: it fails the desk's own entry rules today ({rules}) "
            "— it would not be bought now, so it has not earned its place. "
            f"Its structural protection reads: {protection}.\n"
        )
        if new:
            body += (
                f"Why {new}: best-ranked eligible new candidate (score "
                f"{rotation.get('new_score') or 0.0:.2f}) that the Portfolio "
                "Manager asked to buy, with "
                f"{rotation.get('headroom_pct', 0.0):.2f}% risk headroom left "
                f"against the {rotation.get('ceiling_pct', 0.0):.2f}% "
                "ceiling.\n"
            )
        else:
            body += (
                "There is NO replacement: nothing un-held ranked well enough "
                "to buy this session. The cash stays in the book. This sale "
                "is not funding anything — the position was closed purely "
                "because it no longer clears the bar it was bought on "
                "(owner ruling, 2026-10-01).\n"
            )
        body += (
            "This sale went through the normal Risk Manager review and the "
            "protected-sell discipline (stops cancelled write-ahead, restored "
            "if the sale does not fill)."
        )
        if new:
            body += (
                f" The BUY of {new} follows in this session only if the sale "
                "fills; a second alert follows if it does not happen."
            )
        from src import notifier as _notifier

        _notifier.send_owner_alert(
            body, symbols=[str(held)] + ([str(new)] if new else []),
        )
    except Exception as exc:  # noqa: BLE001
        logger.error("rotation owner alert failed: %s", exc)

def _drop_buys_sold_today_below_bar(pipeline, ctx, buy_decisions: list) -> list:
    """The BUY-side mirror of the `held_symbol_bought_today` anti-churn rule.

    The SELL side already caps oscillation at one round trip per name per
    day: `_apply_rotation_execution` refuses to rotate OUT of a name that
    was bought today. Nothing stopped the other half — sell at 10:00 on an
    entry-bar failure and buy the same name back at 11:00 — which
    crystallises the loss and pays two spreads for a position the desk has
    already said, this same day, it would not open.

    NO new number. The window is exactly the EXCHANGE day the sale happened
    on, read off the desk's own durable `rotation` / `sell_submitted`
    record — the identical day boundary the SELL-side guard uses. There is
    no cooldown and no holding period here, and none is implied: tomorrow
    the name is an ordinary candidate again.

    Fails OPEN. A bookkeeping read that cannot answer must never be the
    thing that stops a trade; the buy then proceeds through its ordinary
    gates.
    """
    if not buy_decisions:
        return buy_decisions
    rotation = getattr(ctx, "rotation", None)
    sold_this_session = ""
    if isinstance(rotation, dict) and rotation.get("sell_order_id"):
        sold_this_session = str(
            rotation.get("held_symbol") or "",
        ).strip().upper()
    try:
        sold_today = pipeline.db.get_rotation_sell_symbols_today()
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "rotation re-buy guard could not read its own record (%s) — "
            "buys proceed through their ordinary gates", exc,
        )
        # Fail-open is the ruling (a bookkeeping hiccup must never block an
        # independently approved buy), but a SILENT fail-open is invisible.
        # One durable per-symbol row per buy that went unchecked, so a churn
        # round trip leaves something countable afterwards.
        for d in buy_decisions:
            _record_pipeline_event(
                pipeline, ctx, getattr(d, "symbol", None), "rotation",
                "rebuy_guard_failed_open",
                "the buy-side anti-churn guard could not read the desk's "
                f"own rotation sell record ({exc}), so this buy was checked "
                "only against the rotation sale this session itself made, "
                "and otherwise proceeded through its ordinary gates",
                failure="record_unreadable",
            )
        sold_today = ()
    blocked = {str(s or "").strip().upper() for s in (sold_today or ())}
    blocked.discard("")
    if sold_this_session and sold_this_session not in blocked:
        # The other half of the same hole: `_persist_evidence` swallows
        # write failures, so the `sell_submitted` row can be LOST as well as
        # unreadable. 2026-10-01 — this branch already PROVES, from the
        # session's own rotation result, the one fact the durable row would
        # have carried: this name was sold today. So it CLOSES the guard
        # rather than only reporting it open. No new state and no new
        # number — the same fact over the same exchange day, read from the
        # session instead of from disk.
        blocked.add(sold_this_session)
        _record_pipeline_event(
            pipeline, ctx, sold_this_session, "rotation",
            "rebuy_guard_closed_from_session_fact",
            "the rotation closed this name this session but no durable "
            "sell record for it could be read back, so the buy-side "
            "anti-churn guard used the session's own rotation result "
            "instead; a same-day re-buy of it is still stopped",
            failure="record_unwritten",
        )
    if not blocked:
        return buy_decisions
    kept: list = []
    for d in buy_decisions:
        sym = str(getattr(d, "symbol", "") or "").strip().upper()
        if sym and sym in blocked:
            _record_execution_skip(
                pipeline, ctx, d.symbol, "sold_today_below_entry_bar",
                "the desk closed this name earlier today because it no "
                "longer cleared the desk's own entry bar; buying it back in "
                "the same session would crystallise that loss and pay two "
                "spreads for a position the desk has already declined to "
                "open today",
            )
            continue
        kept.append(d)
    return kept

def _drop_rotation_buy_if_room_not_freed(pipeline, ctx, buy_decisions: list,
                                         sell_status_by_id: dict) -> list:
    """Phase 14b — the rotation's BUY leg may only proceed on room that is
    REAL. Returns the BUY list with the new candidate removed when it is not.

    The constructor granted the new candidate its risk on the premise that
    the held name closes. If that close was refused upstream (Risk Manager,
    hard rules, protected-sell skip) or was accepted but did not fill,
    buying anyway would put the book over the portfolio risk ceiling by the
    new name's risk — a side door around the ceiling this feature must never
    open. Uses the same `_record_execution_skip` path every other
    deterministic BUY skip uses, so the funnel and the evening review see it.
    Every other BUY in the plan is untouched.
    """
    rotation = ctx.rotation
    if not isinstance(rotation, dict) or not buy_decisions:
        return buy_decisions
    sell_id = rotation.get("sell_order_id")
    sell_status = sell_status_by_id.get(sell_id) if sell_id else None
    if sell_id is None:
        block_detail = (
            f"the rotation close of {rotation.get('held_symbol')} was not "
            "submitted this session (removed before execution — see the "
            "risk / deterministic_gate events for it), so no room was freed"
        )
    elif sell_status != "filled":
        block_detail = (
            f"the rotation close of {rotation.get('held_symbol')} (order "
            f"{sell_id}) ended {sell_status or 'unknown'}, not filled, so no "
            "room was freed"
        )
    else:
        return buy_decisions
    new_symbol = rotation.get("new_symbol")
    kept: list = []
    for d in buy_decisions:
        if d.symbol.upper() == new_symbol:
            _record_execution_skip(
                pipeline, ctx, d.symbol, "rotation_room_not_freed", block_detail,
            )
            continue
        kept.append(d)
    return kept

def _record_rotation_buy_leg_outcome(pipeline, ctx, orders: list) -> None:
    """Phase 14b — record both legs' outcome durably once the buy phase has
    run. A sale that freed room for a BUY that then did not happen is the
    exact churn the anti-rotation rules exist to prevent, so that case is
    also paged (`_alert_rotation_buy_leg_missing`). No-op unless a rotation
    SELL was actually broker-accepted this run."""
    rotation = ctx.rotation
    if not isinstance(rotation, dict) or not rotation.get("sell_order_id"):
        return
    new_symbol = rotation.get("new_symbol")
    if not new_symbol:
        # OWNER RULING 2026-10-01: a categorical cull with no replacement.
        # There is no buy leg to miss, so paging "SOLD BUT THE REPLACEMENT
        # WAS NOT BOUGHT" would be a false alarm about a trade that was
        # never planned. Recorded, not paged.
        _record_pipeline_event(
            pipeline, ctx, rotation.get("held_symbol"), "rotation",
            "no_replacement_leg",
            "the holding was closed on its own merits; nothing un-held "
            "ranked well enough to buy, so there was no replacement leg",
        )
        return
    buy_submitted = any(
        str(o.get("symbol") or "").upper() == new_symbol
        and str(o.get("action") or "").upper() in ("BUY", "SHORT")
        for o in orders if isinstance(o, dict)
    )
    if buy_submitted:
        _record_pipeline_event(
            pipeline, ctx, new_symbol, "rotation", "buy_submitted",
            "replacement_entry_submitted", held_symbol=rotation.get("held_symbol"),
        )
        return
    skip = next(
        (
            s for s in reversed(ctx.execution_skips or [])
            if str(s.get("symbol") or "").upper() == new_symbol
        ),
        None,
    )
    detail = (
        f"{skip.get('reason')}: {skip.get('detail')}"
        if skip else
        "no BUY order for it reached the broker this session (dropped "
        "before execution — see its risk / deterministic_gate / "
        "execution_skip events)"
    )
    _record_pipeline_event(
        pipeline, ctx, new_symbol, "rotation", "buy_not_submitted", detail,
        held_symbol=rotation.get("held_symbol"),
    )
    _alert_rotation_buy_leg_missing(rotation=rotation, detail=detail)

def _alert_rotation_buy_leg_missing(*, rotation: dict, detail: str) -> None:
    """Standalone owner alert: the rotation SOLD but did not BUY.

    This is the one outcome the anti-churn rules exist to prevent — capital
    freed for a named trade that then did not happen — so it is paged, not
    just logged. Never raises.
    """
    try:
        held = rotation["held_symbol"]
        new = rotation["new_symbol"]
        body = (
            "ROTATION INCOMPLETE — SOLD BUT THE REPLACEMENT WAS NOT BOUGHT\n"
            f"{held} was closed this session to make room for {new}, but no "
            f"BUY of {new} was submitted.\n"
            f"Reason recorded: {detail}\n"
            "OUTCOME: the freed cash is sitting in the book. Nothing further "
            "was done automatically. The next morning session will see "
            f"{new} again as a fresh candidate with room available."
        )
        from src import notifier as _notifier

        _notifier.send_owner_alert(
            body, symbols=[str(held)] + ([str(new)] if new else []),
        )
    except Exception as exc:  # noqa: BLE001
        logger.error("rotation buy-leg owner alert failed: %s", exc)
