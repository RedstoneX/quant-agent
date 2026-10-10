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
whose sale was refused for a reason that can clear today, deferred behind a
working exit order, or interrupted by an error -- those are retried next tick.
A refusal that cannot clear today (bought today, already cut today) is final
for that bar (`refused_final`).

Overlap with a running review (the tick is exempt from the session lock, and
that lock lapses): a holding with ANY working non-stop order at the broker is
not routed to the sell path; it is marked `deferred`, still trailed, and
retried next tick. An unreadable order listing defers every firing holding
(fail closed).

An exception from the sell path may come AFTER an order went out, so every
firing holding is marked `error`, kept out of this tick's trail, and the
failure is recorded durably; the retry next tick passes the open-order check
first.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace

from src.sentinel.guarded_site import record_site

#: Same logger the intra check itself writes under (see src/intraday/session.py).
logger = logging.getLogger("src.pipeline")

__all__ = [
    "OUTCOME_DEFERRED",
    "OUTCOME_ERROR",
    "OUTCOME_HOLD",
    "OUTCOME_REFUSED",
    "OUTCOME_REFUSED_FINAL",
    "OUTCOME_SOLD",
    "SellPathError",
    "trend_exit_on_tick",
    "wire_trend_exit",
    "working_exit_symbols",
]

OUTCOME_HOLD = "hold"
OUTCOME_SOLD = "sold"
OUTCOME_REFUSED = "refused"
#: Refused for a reason that cannot clear today (bought today, already cut today): not retried this bar.
OUTCOME_REFUSED_FINAL = "refused_final"
#: A working non-stop order already exists (e.g. a review's sale): retried next tick.
OUTCOME_DEFERRED = "deferred"
#: The sell path raised; an order may have gone out. Retried next tick, after the open-order check.
OUTCOME_ERROR = "error"
_RETRY = frozenset({OUTCOME_REFUSED, OUTCOME_DEFERRED, OUTCOME_ERROR})


class SellPathError(RuntimeError):
    """The guarded sell path raised; `no_trail` names every holding that may have an order out."""

    def __init__(self, message: str, no_trail: list):
        super().__init__(message)
        self.no_trail = list(no_trail)


def _symbol(position) -> str:
    return (getattr(position, "symbol", "") or "").strip().upper()


def _order_symbol(order) -> str:
    raw = order.get("symbol") if isinstance(order, dict) else getattr(order, "symbol", "")
    return (raw or "").strip().upper()


def _enum_text(value) -> str:
    return str(getattr(value, "value", value) or "").strip().lower()


def working_exit_symbols(orders) -> set:
    """Symbols with a working NON-stop buy or sell order (a sale or cover may be in flight)."""
    out = set()
    for order in orders or []:
        kind = _enum_text(getattr(order, "order_type", None) or getattr(order, "type", None))
        if "stop" in kind:
            continue
        # A cancel is asynchronous at the broker: an order already being cancelled
        # (e.g. by the freeze sweep in the same tick) is not a sale in flight.
        if _enum_text(getattr(order, "status", None)) in ("pending_cancel", "canceled", "cancelled"):
            continue
        if _enum_text(getattr(order, "side", None)) in ("sell", "buy"):
            out.add(_order_symbol(order))
    return out


def _due(marker: dict | None, bar_date: str) -> bool:
    """Run on a new completed bar, or retry a retryable outcome on this one."""
    if marker is None or marker.get("bar_date") != bar_date:
        return True
    return marker.get("outcome") in _RETRY


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
    open_orders_checked,
    final_today,
) -> dict:
    """Check each due holding's trend exit on completed daily bars; sell the cleared ones via the guarded path.

    Returns `{status, bar_date, checked, sold, refused, refused_final, deferred, no_trail}`;
    `no_trail` names every holding the trail must leave alone this tick. If the
    sell path raises, the firing holdings are marked `error` and `SellPathError`
    carries them to the caller.
    """
    summary: dict = {
        "status": "ran",
        "bar_date": completed_bar_date,
        "checked": [],
        "sold": [],
        "refused": [],
        "refused_final": [],
        "deferred": [],
        "no_trail": [],
    }

    def mark(symbol: str, outcome: str) -> None:
        write_marker(symbol, bar_date=completed_bar_date, outcome=outcome, run_id=run_id)

    due = [p for p in positions or [] if _holding(p) and _due(read_marker(_symbol(p)), completed_bar_date)]
    if not due:
        return summary
    facts = position_facts_for(due) or {}
    cleared = []
    for position in due:
        verdict = _verdict(alignment_exit_cached, position, facts, run_id)
        summary["checked"].append(_symbol(position))
        if getattr(verdict, "exit_cleared", False):
            cleared.append(position)
        else:
            mark(_symbol(position), OUTCOME_HOLD)
    firing = _not_busy(cleared, open_orders_checked, mark, summary, completed_bar_date) if cleared else []
    if not firing:
        return summary
    symbols = [_symbol(p) for p in firing]
    try:
        orders = execute_guarded_exits(firing, run_id=run_id, position_facts=facts) or []
    except Exception as exc:
        for symbol in symbols:
            mark(symbol, OUTCOME_ERROR)
        raise SellPathError(f"sell path raised for {symbols}: {exc}", symbols) from exc
    _settle(symbols, orders, set(final_today(set(symbols)) or set()), mark, summary, completed_bar_date)
    summary["no_trail"] = list(summary["sold"])
    return summary


def _holding(position) -> bool:
    try:
        qty = float(getattr(position, "qty", 0) or 0)
    except (TypeError, ValueError):
        return False
    return bool(_symbol(position)) and qty != 0


def _verdict(alignment_exit_cached, position, facts: dict, run_id: str):
    """Same arguments the review's alignment scan passes, so the executor's
    scan reads this verdict back from the memo (one chart read)."""
    own = facts.get(_symbol(position), {}) or {}
    return alignment_exit_cached(
        symbol=_symbol(position),
        thesis_invalid_if=getattr(position, "thesis_invalid_if", None) or own.get("thesis_invalid_if"),
        is_short=float(position.qty) < 0,
        entry_price=getattr(position, "avg_entry", None),
        stop_loss=getattr(position, "stop_loss", None) or own.get("stop_loss"),
        run_id=run_id,
    )


def _not_busy(cleared: list, open_orders_checked, mark, summary: dict, bar_date: str) -> list:
    """Drop (and mark `deferred`) every cleared holding with a working non-stop order; fail closed."""
    listed, open_orders = open_orders_checked()
    busy = working_exit_symbols(open_orders) if listed else {_symbol(p) for p in cleared}
    firing = []
    for position in cleared:
        symbol = _symbol(position)
        if symbol not in busy:
            firing.append(position)
            continue
        summary["deferred"].append(symbol)
        mark(symbol, OUTCOME_DEFERRED)
        logger.warning(
            "tick trend exit: %s cleared on the %s close but %s; deferred to the next tick",
            symbol,
            bar_date,
            "a working order exists for it" if listed else "the open-order listing failed",
        )
    return firing


def _settle(symbols: list, orders: list, final: set, mark, summary: dict, bar_date: str) -> None:
    """Mark each routed holding sold, refused (retried next tick) or refused_final (not retried this bar)."""
    ordered = {_order_symbol(order) for order in orders}
    for symbol in symbols:
        if symbol in ordered:
            outcome = OUTCOME_SOLD
        elif symbol in final:
            outcome = OUTCOME_REFUSED_FINAL
        else:
            outcome = OUTCOME_REFUSED
        summary[outcome].append(symbol)
        mark(symbol, outcome)
        if outcome == OUTCOME_REFUSED:
            logger.warning(
                "tick trend exit: %s cleared the trend exit on the %s close but the guarded sell path "
                "placed no order; retried at the next tick",
                symbol,
                bar_date,
            )


def wire_trend_exit(host):
    """Bind `trend_exit_on_tick` to the pipeline's own collaborators, read through `host(name)`.

    Returns `callable(positions, *, run_id, total_value) -> dict` that never
    raises, or None when the host lacks any piece (the tick then reports the
    step unavailable).
    """
    from src.storage.trades import trend_tick_store as store
    from src.trading_calendar import last_completed_bar_date

    db = host("db")
    broker = host("broker")
    cached = host("_alignment_exit_cached")
    execute = host("_midday_execute_llm_actions")
    build_facts = host("_build_position_facts")
    trimmed_today = host("_symbols_already_trimmed_today")
    opened_today = host("_position_opened_today")
    metric_deltas_for = host("_build_review_metric_deltas")
    if None in (db, broker, cached, execute, build_facts, trimmed_today, opened_today, metric_deltas_for):
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

    def final_today(symbols: set) -> set:
        """Refusals that cannot clear today: bought in today's session, or already cut today."""
        return {s for s in symbols if opened_today(s)} | (set(trimmed_today() or set()) & symbols)

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
            open_orders_checked=broker.list_open_orders_checked,
            final_today=final_today,
        )

    def run(positions, *, run_id: str, total_value=None) -> dict:
        try:
            return _run_once(positions, run_id=run_id, total_value=total_value)
        except Exception as exc:  # noqa: BLE001 — a broken trend step must not stop the other stops trailing
            # Recorded durably (guarded-outcome journal). If the SELL PATH raised, an order may
            # already be out: those holdings were marked `error` and are kept out of this tick's trail.
            record_site(SimpleNamespace(db=db), "intraday.trend_exit_tick", exc, log=logger)
            return {
                "status": "error",
                "reason": str(exc),
                "sold": [],
                "refused": [],
                "checked": [],
                "no_trail": list(getattr(exc, "no_trail", []) or []),
            }

    return run
