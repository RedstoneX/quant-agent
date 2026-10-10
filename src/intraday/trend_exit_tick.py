"""src.intraday.trend_exit_tick -- the trend exit, run at the 30-minute intra check on DAILY closes only.

Owner ruling 2026-10-09: run the trend exit (sell on a confirmed break of the
last rising low; `src/exits/alignment_exit.py`) at every 30-minute check, on
DAILY closes only -- never intraday bars. Before this it ran only in the
midday and close position reviews.

This module adds NO exit maths, NO number and NO sell door:
  * the verdict is the review's own memoised one (`_alignment_exit_cached`),
    read on the price feed's COMPLETED daily bars (`MarketData.get_ohlcv`
    drops today's unfinished bar in market hours; nothing intraday is read);
  * a cleared verdict is executed by the review's OWN guarded executor
    (`_midday_execute_llm_actions`, with no model review) -- its alignment
    scan re-raises the sale from the same memoised verdict and every gate it
    applies to a review-raised sale applies here unchanged: same-day trim,
    spent trigger, exit veto, AI Risk objection list, share-count sign, and
    the fallback that restores a displaced action on refusal. No AI seat is
    called: the review's AI Risk call happens before that executor and only
    sees the model's own exits, so a scan-raised sale never reaches it.

What this adds is WHEN it runs. A new completed bar appears once a day, so a
holding is checked once per new completed bar (in practice at the first tick,
09:45 ET); the bar date checked is persisted per holding
(`trend_exit_tick_checks`). Later ticks that day skip it, EXCEPT a holding
whose sale the guarded path refused, which is retried at the next tick.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace

from src.sentinel.guarded_site import record_site

#: Same logger the intra check itself writes under (see src/intraday/session.py).
logger = logging.getLogger("src.pipeline")

__all__ = ["OUTCOME_HOLD", "OUTCOME_REFUSED", "OUTCOME_SOLD", "trend_exit_on_tick", "wire_trend_exit"]

OUTCOME_HOLD = "hold"
OUTCOME_SOLD = "sold"
OUTCOME_REFUSED = "refused"


def _symbol(position) -> str:
    return (getattr(position, "symbol", "") or "").strip().upper()


def _order_symbol(order) -> str:
    raw = order.get("symbol") if isinstance(order, dict) else getattr(order, "symbol", "")
    return (raw or "").strip().upper()


def _due(marker: dict | None, bar_date: str) -> bool:
    """Run on a new completed bar, or retry a sale refused on this one."""
    if marker is None or marker.get("bar_date") != bar_date:
        return True
    return marker.get("outcome") == OUTCOME_REFUSED


def trend_exit_on_tick(
    *,
    positions,
    run_id: str,
    completed_bar_date: str,
    read_marker,
    write_marker,
    alignment_exit_cached,
    position_facts_for,
    execute_guarded_exits,
) -> dict:
    """Check each due holding's trend exit on completed daily bars; sell the cleared ones via the guarded path.

    Returns `{status, bar_date, checked, sold, refused}`; `sold` names every
    holding a sell order was placed for this tick (the trail must leave them
    alone). Raises on a broken collaborator; the caller logs and records it.
    """
    summary: dict = {"status": "ran", "bar_date": completed_bar_date, "checked": [], "sold": [], "refused": []}
    due = []
    for position in positions or []:
        symbol = _symbol(position)
        try:
            qty = float(getattr(position, "qty", 0) or 0)
        except (TypeError, ValueError):
            qty = 0.0
        if symbol and qty != 0 and _due(read_marker(symbol), completed_bar_date):
            due.append(position)
    if not due:
        return summary
    facts = position_facts_for(due) or {}
    firing = []
    for position in due:
        symbol = _symbol(position)
        own = facts.get(symbol, {}) or {}
        # Same arguments the review's alignment scan passes, so the executor's
        # scan below reads this verdict back from the memo (one chart read).
        verdict = alignment_exit_cached(
            symbol=symbol,
            thesis_invalid_if=getattr(position, "thesis_invalid_if", None) or own.get("thesis_invalid_if"),
            is_short=float(position.qty) < 0,
            entry_price=getattr(position, "avg_entry", None),
            stop_loss=getattr(position, "stop_loss", None) or own.get("stop_loss"),
            run_id=run_id,
        )
        summary["checked"].append(symbol)
        if getattr(verdict, "exit_cleared", False):
            firing.append(position)
        else:
            write_marker(symbol, bar_date=completed_bar_date, outcome=OUTCOME_HOLD, run_id=run_id)
    if not firing:
        return summary
    orders = execute_guarded_exits(firing, run_id=run_id, position_facts=facts) or []
    ordered = {_order_symbol(order) for order in orders}
    for position in firing:
        symbol = _symbol(position)
        outcome = OUTCOME_SOLD if symbol in ordered else OUTCOME_REFUSED
        summary["sold" if outcome == OUTCOME_SOLD else "refused"].append(symbol)
        write_marker(symbol, bar_date=completed_bar_date, outcome=outcome, run_id=run_id)
        if outcome == OUTCOME_REFUSED:
            logger.warning(
                "tick trend exit: %s cleared the trend exit on the %s close but the guarded sell path "
                "placed no order; retried at the next tick",
                symbol,
                completed_bar_date,
            )
    return summary


def wire_trend_exit(host):
    """Bind `trend_exit_on_tick` to the pipeline's own collaborators, read through `host(name)`.

    Returns `callable(positions, *, run_id, total_value) -> dict`, or None when
    the host lacks any piece (the tick then reports the step unavailable).
    """
    from src.storage.trades import trend_tick_store as store
    from src.trading_calendar import last_completed_bar_date

    db = host("db")
    cached = host("_alignment_exit_cached")
    execute = host("_midday_execute_llm_actions")
    build_facts = host("_build_position_facts")
    trimmed_today = host("_symbols_already_trimmed_today")
    metric_deltas_for = host("_build_review_metric_deltas")
    if None in (db, cached, execute, build_facts, trimmed_today, metric_deltas_for):
        return None

    def execute_guarded_exits(firing, *, run_id: str, position_facts: dict) -> list:
        symbols = {_symbol(p) for p in firing}
        return execute(
            firing,
            None,
            run_id,
            already_trimmed_today=set(trimmed_today() or set()) & symbols,
            metric_deltas=metric_deltas_for(position_facts, run_id=run_id),
            position_facts=position_facts,
        )

    def run(positions, *, run_id: str, total_value=None) -> dict:
        try:
            return _run_once(positions, run_id=run_id, total_value=total_value)
        except Exception as exc:  # noqa: BLE001 — a broken trend read must not stop the stops trailing
            # Recorded durably (guarded-outcome journal), then the trail runs; nothing was sold.
            record_site(SimpleNamespace(db=db), "intraday.trend_exit_tick", exc, log=logger)
            return {"status": "error", "reason": str(exc), "sold": [], "refused": [], "checked": []}

    def _run_once(positions, *, run_id: str, total_value) -> dict:
        ledger = db._trades()
        morning_trades = db.get_trades(today_only=True, executed_only=True)
        return trend_exit_on_tick(
            positions=positions,
            run_id=run_id,
            completed_bar_date=last_completed_bar_date().isoformat(),
            read_marker=lambda symbol: store.get(ledger, symbol),
            write_marker=lambda symbol, **kw: store.record(ledger, symbol, **kw),
            alignment_exit_cached=cached,
            position_facts_for=lambda due: build_facts(due, morning_trades, total_value),
            execute_guarded_exits=execute_guarded_exits,
        )

    return run
