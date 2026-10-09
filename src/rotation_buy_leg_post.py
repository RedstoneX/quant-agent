"""Rotation buy-leg post-processing: alerts, drops and outcome records (lifted VERBATIM from pipeline_rotation_exec)."""

from __future__ import annotations

from src.pipeline_stages import (
    _record_execution_skip,
    _record_pipeline_event,
    logger,
)


def _rotation_sell_outcome(*, qty: float, filled_qty: float | None, terminal_status: str | None) -> str:
    """Which TRUE thing happened to the sale: 'filled', 'partial', 'unfilled'
    or 'unknown'. Read off the FILLED QUANTITY, never the status word alone:
    an order cancelled after a part fill still sold shares, and a submit-time
    'pending_new' sold nothing. No terminal status from the wait, or no fill
    quantity from the broker, is 'unknown' — never a guess either way.
    """
    if not terminal_status or filled_qty is None:
        return "unknown"
    if filled_qty <= 0:
        return "unfilled"
    if filled_qty < qty:
        return "partial"
    return "filled"


def _alert_rotation_executed(
    *,
    rotation: dict,
    qty: float,
    limit_price: float,
    order_id: str | None,
    terminal_status: str | None = None,
    filled_qty: float | None = None,
    avg_price: float | None = None,
    stops_restored: bool | None = None,
) -> None:
    """Standalone owner alert for the desk's own rotation close.

    Same path and shape as the naked-position, re-peg-exhausted and
    holding-discipline-block alerts (`notifier.send_owner_alert`): its own
    Telegram message, never bundled into the run summary; severity in plain
    words, never colour alone. Sent at the sale's TERMINAL state, never at
    submit — 2026-10-09 the owner was told a position was "CLOSED" while the
    sell sat unfilled and was cancelled 15 s later. The headline is read off
    the filled quantity (`_rotation_sell_outcome`); a restore is claimed only
    when the finalize step confirmed it (`stops_restored`). Called with no
    outcome it says UNKNOWN, never CLOSED. Never raises.
    """
    try:
        held = rotation["held_symbol"]
        new = rotation.get("new_symbol") or None
        rules = "; ".join(rotation.get("held_reasons") or []) or "entry rules"
        protection = str(rotation.get("protection_basis") or "not measured")
        room_for = f" to free room for {new}" if new else ""
        order = f"limit ${limit_price:,.2f}, broker order {order_id}"
        outcome = _rotation_sell_outcome(qty=qty, filled_qty=filled_qty, terminal_status=terminal_status)
        filled = float(filled_qty or 0.0)
        avg = f"an average ${avg_price:,.2f}" if avg_price else "an unreported average price"
        if outcome == "filled":
            body = (
                "POSITION CLOSED AUTOMATICALLY — OPPORTUNITY ROTATION\n"
                f"{held}: the desk SOLD {filled:g} share(s) at {avg} ({order}){room_for}.\n"
            )
        elif outcome == "partial":
            body = (
                f"PARTLY SOLD {filled:g} of {qty:g} share(s) — OPPORTUNITY ROTATION\n"
                f"{held}: the desk sold {filled:g} of {qty:g} share(s) at {avg} ({order}){room_for}; "
                f"the order ended '{terminal_status}' and {qty - filled:g} share(s) are still held.\n"
            )
        elif outcome == "unfilled":
            body = (
                "SELL NOT FILLED — position kept — OPPORTUNITY ROTATION\n"
                f"{held}: the desk offered {qty:g} share(s) for sale ({order}){room_for}; "
                f"nothing filled and the order ended '{terminal_status}'. The position is still held.\n"
            )
        else:
            body = (
                "SELL OUTCOME UNKNOWN — OPPORTUNITY ROTATION\n"
                f"{held}: the desk offered {qty:g} share(s) for sale ({order}){room_for}, "
                "but the broker never confirmed how the order ended or how much filled. "
                "Whether the position is still held is NOT known.\n"
            )
        body += (
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
                "is not funding anything — it was placed purely because the "
                "position no longer clears the bar it was bought on "
                "(owner ruling, 2026-10-01).\n"
            )
        body += "This sale went through the normal Risk Manager review and the protected-sell discipline. "
        if outcome == "filled":
            body += "No shares remain, so there are no stops to restore."
        elif stops_restored:
            body += "Stop coverage on the shares still held was restored and confirmed."
        else:
            body += (
                "Stop coverage on the shares still held was NOT confirmed restored; "
                "the recovery intent is kept and the desk retries it next session."
            )
        if new:
            body += (
                f" The BUY of {new} follows in this session only if the sale "
                "fills; a second alert follows if it does not happen."
            )
        from src import notifier as _notifier

        _notifier.send_owner_alert(
            body,
            symbols=[str(held)] + ([str(new)] if new else []),
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
        sold_this_session = (
            str(
                rotation.get("held_symbol") or "",
            )
            .strip()
            .upper()
        )
    try:
        sold_today = pipeline.db.get_rotation_sell_symbols_today()
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "rotation re-buy guard could not read its own record (%s) — buys proceed through their ordinary gates",
            exc,
        )
        # Fail-open is the ruling (a bookkeeping hiccup must never block an
        # independently approved buy), but a SILENT fail-open is invisible.
        # One durable per-symbol row per buy that went unchecked, so a churn
        # round trip leaves something countable afterwards.
        for d in buy_decisions:
            _record_pipeline_event(
                pipeline,
                ctx,
                getattr(d, "symbol", None),
                "rotation",
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
            pipeline,
            ctx,
            sold_this_session,
            "rotation",
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
                pipeline,
                ctx,
                d.symbol,
                "sold_today_below_entry_bar",
                "the desk closed this name earlier today because it no "
                "longer cleared the desk's own entry bar; buying it back in "
                "the same session would crystallise that loss and pay two "
                "spreads for a position the desk has already declined to "
                "open today",
            )
            continue
        kept.append(d)
    return kept


def _drop_rotation_buy_if_room_not_freed(pipeline, ctx, buy_decisions: list, sell_status_by_id: dict) -> list:
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
                pipeline,
                ctx,
                d.symbol,
                "rotation_room_not_freed",
                block_detail,
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
            pipeline,
            ctx,
            rotation.get("held_symbol"),
            "rotation",
            "no_replacement_leg",
            "the holding was closed on its own merits; nothing un-held "
            "ranked well enough to buy, so there was no replacement leg",
        )
        return
    buy_submitted = any(
        str(o.get("symbol") or "").upper() == new_symbol and str(o.get("action") or "").upper() in ("BUY", "SHORT")
        for o in orders
        if isinstance(o, dict)
    )
    if buy_submitted:
        _record_pipeline_event(
            pipeline,
            ctx,
            new_symbol,
            "rotation",
            "buy_submitted",
            "replacement_entry_submitted",
            held_symbol=rotation.get("held_symbol"),
        )
        return
    skip = next(
        (s for s in reversed(ctx.execution_skips or []) if str(s.get("symbol") or "").upper() == new_symbol),
        None,
    )
    detail = (
        f"{skip.get('reason')}: {skip.get('detail')}"
        if skip
        else "no BUY order for it reached the broker this session (dropped "
        "before execution — see its risk / deterministic_gate / "
        "execution_skip events)"
    )
    _record_pipeline_event(
        pipeline,
        ctx,
        new_symbol,
        "rotation",
        "buy_not_submitted",
        detail,
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
            body,
            symbols=[str(held)] + ([str(new)] if new else []),
        )
    except Exception as exc:  # noqa: BLE001
        logger.error("rotation buy-leg owner alert failed: %s", exc)
