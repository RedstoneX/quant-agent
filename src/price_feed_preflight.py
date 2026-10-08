"""Prove the price feed reads before a session places any new entry.

Owner ruling 2026-10-08: a desk that cannot read price cannot trade. Before
the entry stage sizes anything, ONE read of a reference symbol -- a held
name when there is one, else the first approved entry -- must come back
(absence is fine: the feed answered). The read carries the broker's own
retry; when it still fails the session places no new entries, every approved
entry is recorded as skipped under ``price_feed_unreadable``, and the fault is
counted durably. Existing positions are not touched by any of this.

`price_feed_session_start` runs at the stage's start, `preflight_price_feed`
on the approved entries before the viability preflight.

`wait_for_today_prints` is the other half of the same ruling, restated by the
owner the same day: "this is a swing trading desk, not scalping. The desk can
keep re-asking until a price is received. It should not be hammered because
that will get us banned." Names with no today print are held in a waiting set
and re-asked for in ONE batched latest-trades request for every waiting name,
on a rising backoff, until each prints or the session's own slot ends.
"""
from __future__ import annotations

import logging
import math
import time
from datetime import timedelta

from src.config.llm_cost import INTRA_CHECK_TICK_MINUTES
from src.execution.broker_parts.trade_prints import read_latest_trade_prints
from src.infra_retry_policy import BACKOFF_BASE_S, BACKOFF_MAX_S
from src.pipeline_candidate_records import _record_execution_skip
from src.refusal_errors import PriceReadFailed
from src.sizing_refusal import (
    NO_PRINT_BY_WINDOW_END,
    NO_SIZING_PRINT,
    PRICE_FEED_UNREADABLE,
    declare_price_feed_fault,
    price_feed_fault,
    reset_price_feed,
)
from src.trading_calendar import SESSION_WINDOWS, et_now

logger = logging.getLogger(__name__)
_sleep = time.sleep  # module attribute so a test can stub the backoff


def _symbol_of(item) -> str | None:
    sym = item.get("symbol") if isinstance(item, dict) else getattr(item, "symbol", None)
    return str(sym) if isinstance(sym, str) and sym else None


def reference_symbol(ctx) -> str | None:
    """A held name first (its stop is live, its price must read), else the
    first approved entry; None when the session holds and approves nothing."""
    for position in getattr(ctx, "positions", None) or []:
        sym = _symbol_of(position)
        if sym:
            return sym
    decisions = getattr(getattr(ctx, "portfolio_decision", None), "decisions", None) or []
    for decision in decisions:
        if getattr(decision, "action", None) in ("BUY", "SHORT"):
            sym = _symbol_of(decision)
            if sym:
                return sym
    return None


def price_feed_session_start(pipeline, ctx):
    """Entry-stage start: last session's fault does not carry over, and this
    session's reference symbol is pinned. Returns `ctx` so it nests in place."""
    reset_price_feed(pipeline, reference_symbol(ctx))
    return ctx


def preflight_price_feed(pipeline, ctx, decisions: list) -> list:
    """The approved entries, or `[]` (each recorded) when the feed does not read."""
    if not decisions:
        return decisions
    fault = price_feed_fault(pipeline)
    if fault is None:
        reference = getattr(pipeline, "price_feed_reference", None)
        getter = getattr(getattr(pipeline, "broker", None), "get_latest_price_stamped", None)
        if isinstance(reference, str) and reference and callable(getter):
            try:
                getter(reference)  # None is absence: the feed answered
            except PriceReadFailed as exc:
                fault = declare_price_feed_fault(pipeline, "preflight", exc, symbol=reference)
    if fault is None:
        return decisions
    for decision in decisions:
        _record_execution_skip(pipeline, ctx, decision.symbol, PRICE_FEED_UNREADABLE, fault)
    return []


def slot_seconds_left(ctx, now=None) -> float:
    """Seconds until this pass's own slot ends: the earlier of the session's
    published window end (`SESSION_WINDOWS`, ET minutes) and the desk's next
    scheduled pass (`INTRA_CHECK_TICK_MINUTES` from now), because the next
    pass carries the stop checks and must not find this one still waiting.
    Both numbers are already ledgered; nothing here is new. Zero when the
    session has no window or it has already ended."""
    now = now or et_now()
    tick_end = now + timedelta(minutes=INTRA_CHECK_TICK_MINUTES)
    window = SESSION_WINDOWS.get(str(getattr(ctx, "session", "")))
    if window is None:
        return 0.0
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    window_end = midnight + timedelta(minutes=window[1])
    return max(0.0, (min(window_end, tick_end) - now).total_seconds())


def wait_for_today_prints(pipeline, ctx, waiting: list) -> tuple[list, list]:
    """Re-ask for every waiting name in ONE batched request per ask until each
    has a today print or the slot ends. Returns ``(printed, skipped)``: the
    decisions that printed, in their original order, and ``(decision,
    reason, detail)`` for each that did not, for the caller to record. A
    batched read that FAILS after its own retry is the desk fault.

    Pacing: the first batched ask is immediate (it also proves the batched
    path answers before any wait is spent); each later one waits
    `BACKOFF_BASE_S`, doubling to `BACKOFF_MAX_S` (the desk's ledgered
    transient-fault policy). At the cap that is 60 / BACKOFF_MAX_S = 7.5
    requests a minute, 3.75% of Alpaca's published 200 requests/minute
    market-data limit (the limit is ledgered at
    `src.execution.broker_parts.trade_stream._STREAM_ATTEMPT_CEILING_PER_DAY`).
    The ask count is bounded by the slot: one immediate ask plus at most
    `ceil(slot_seconds / BACKOFF_MAX_S)` on backoff, and the count is logged.
    """
    broker = getattr(pipeline, "broker", None)
    budget_s = slot_seconds_left(ctx)
    ask_ceiling = math.ceil(budget_s / BACKOFF_MAX_S)  # backoff asks; the first ask is immediate
    pending = {d.symbol: d for d in waiting}
    printed: dict[str, object] = {}
    asks = 0
    deadline = time.monotonic() + budget_s
    wait_s = BACKOFF_BASE_S
    fault = None
    while pending and broker is not None:
        if asks:  # the first ask is immediate; every later one waits, rising
            if asks > ask_ceiling or time.monotonic() + wait_s > deadline:
                break
            _sleep(wait_s)
            wait_s = min(wait_s * 2, BACKOFF_MAX_S)
        asks += 1
        try:
            prints = read_latest_trade_prints(broker, list(pending))
        except PriceReadFailed as exc:
            fault = declare_price_feed_fault(pipeline, "batched_reask", exc,
                                             symbol=",".join(pending))
            break
        if not isinstance(prints, dict):
            # No real batched reader on this broker (a stub): nothing to
            # wait on, so the plain measured-absence refusal stands.
            return [], [
                (d, NO_SIZING_PRINT, "no today trade print to size against and "
                 "no batched re-ask available on this broker")
                for d in pending.values()
            ]
        for symbol in [s for s in pending if s in prints]:
            printed[symbol] = pending.pop(symbol)
    logger.info(
        "today-print wait: %d batched ask(s) (one immediate, then at most %d on "
        "backoff inside the %.0fs slot); printed %s, still waiting %s",
        asks, ask_ceiling, budget_s, sorted(printed) or "-", sorted(pending) or "-",
    )
    skipped = []
    for decision in pending.values():
        if fault:
            skipped.append((decision, PRICE_FEED_UNREADABLE, fault))
            continue
        skipped.append((decision, NO_PRINT_BY_WINDOW_END, (
            f"{NO_PRINT_BY_WINDOW_END}: no today trade print after {asks} batched "
            f"ask(s) across this pass's slot ({budget_s:.0f}s); not sized -- the "
            "desk's next pass re-decides the name"
        )))
    return [d for d in waiting if d.symbol in printed], skipped
