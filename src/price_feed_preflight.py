"""Prove the price feed reads before a session places any new entry.

Owner ruling 2026-10-08: a desk that cannot read price cannot trade. Before
the entry stage sizes anything, ONE read of a reference symbol -- a held
name when there is one, else the first approved entry -- must come back
(absence is fine: the feed answered). The read carries the broker's own
retry; when it still fails the session places no new entries, every approved
entry is recorded as skipped under ``price_feed_unreadable``, and the fault is
counted durably. Existing positions are not touched by any of this.

Both entry points are nested into existing statements of the entry stage so
that file does not grow: `price_feed_session_start` wraps the stage's run,
`preflight_price_feed` wraps the approved entries before the funding sweep.
"""
from __future__ import annotations

import logging

from src.pipeline_candidate_records import _record_execution_skip
from src.refusal_errors import PriceReadFailed
from src.sizing_refusal import (
    PRICE_FEED_UNREADABLE,
    declare_price_feed_fault,
    price_feed_fault,
    reset_price_feed,
)

logger = logging.getLogger(__name__)


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
    reset_price_feed(pipeline, reference_symbol(ctx),
                     opening_session=getattr(ctx, "session", None) == "morning")
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
