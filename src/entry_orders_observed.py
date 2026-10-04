"""Entry-order sites lifted out of ``src/pipeline_entry_orders.py`` so their
catch-alls can be made LOUD without that file gaining a line.

Same shape as ``src/sentinel/guarded.py`` and ``src/api/broker_reads_record.py``:
a swallowed fault logs a FULL traceback at ERROR plus a counted ``disagreed``
row; a clean pass writes its own ``agreed`` row; a site never reached writes
nothing, so the three states stay distinguishable. Behaviour is unchanged --
every handler still swallows exactly what it swallowed before, the returns and
defaults are verbatim, and nothing is stored here.

``_alert_unmeasurable_symbols`` holds no pipeline and so no ledger handle: it
passes ``db=None``, which logs the traceback and skips the row. That is honest
about what it can see rather than inventing a second channel.
"""
from __future__ import annotations

import logging

from src.pipeline_entry_text import _REPEG_OUTCOME_TEXT
from src.sentinel.entry_guard import (
    record_clean_pass,
    record_swallowed,
    record_swallowed_here,
)

logger = logging.getLogger("src.pipeline_entry_orders")

def _trade_updates_already_started(pipeline) -> bool:
    started = getattr(getattr(pipeline, "broker", None), "trade_updates_started", None)
    if not callable(started):
        return False
    try:
        answer = started() is True
    except Exception:  # noqa: BLE001
        record_swallowed_here(pipeline, "stream.already_started")
        return False
    record_clean_pass(pipeline, "stream.already_started")
    return answer

def _fill_stream_enabled(pipeline) -> bool:
    """Whether the desk may open the `trade_updates` socket at all.

    Defaults TRUE when the broker predates the switch (a test double with no
    such method), so this helper cannot silently remove a budget from a
    broker that really does handshake.
    """
    enabled = getattr(getattr(pipeline, "broker", None), "fill_stream_enabled", None)
    if not callable(enabled):
        return True
    try:
        answer = enabled() is not False
    except Exception:  # noqa: BLE001
        record_swallowed_here(pipeline, "stream.enabled_flag")
        return True
    record_clean_pass(pipeline, "stream.enabled_flag")
    return answer


def _alert_owner_entry_cancelled(pipeline, spec: dict, info: dict) -> None:
    """An entry was cancelled unfilled at the end of its session. PAGE.

    The owner's requirement in his own words: an order must never simply die
    with no trace, and if the automatic attempts are exhausted he wants to be
    told so he has a choice. Everything up to this point is deliberately
    hands-off — the ordinary stalling entry is repriced once and filled with
    no human involved. This fires only once the session has given up on it.

    This is ALSO the "repricing exhausted" alert: since 2026-09-12 an
    unfilled entry is cancelled rather than left working, so "the reprice
    ran out" and "the order was cancelled" are the same event and get ONE
    message, not two. It fires whether or not repricing is enabled — the
    cancel happens either way, and the text says what was and was not tried.

    A SEPARATE Telegram message via `send_owner_alert`, never a line inside
    the session summary, per this desk's standing alert-design rule (see
    `src/notifier.py`'s data-quality alert comment: "alerts get their OWN
    Telegram message, never bundled into a run summary"). Same path already
    used for a naked position with no stop and for a failed protective stop.

    WHAT DOES NOT PAGE, on purpose — each of these is a non-event, and an
    alert channel that fires on non-events is one the owner learns to swipe
    away:
      * the order FILLED (the whole point) — including a fill that landed
        mid-replace and a replacement the broker refused because it filled;
      * a PARTIAL fill — shares were acquired and the stop covers them (the
        unfilled remainder is cancelled by protection as before, silently);
      * an order that reached a terminal state on its own (expired,
        rejected) — that is a different failure with its own reporting.

    NOT deduplicated, matching the data-quality alert's stated reasoning: if
    the same symbol stalls again tomorrow that is a real repeated event, not
    noise.

    Never raises. An alerting bug must not break the execution path it is
    reporting on.
    """
    try:
        symbol = spec.get("symbol")
        attempted = list(spec.get("attempted_prices") or [])
        limit_price = spec.get("limit_price")
        ceiling = spec.get("ceiling")
        outcome = str(spec.get("repeg_outcome") or "")
        ending = _REPEG_OUTCOME_TEXT.get(
            outcome, "no automatic reprice was made",
        )
        prices = [p for p in [limit_price] if isinstance(p, (int, float))]
        prices += attempted
        tried = (
            " → ".join(f"${p:,.2f}" for p in prices)
            if prices else "(no limit price recorded — market order)"
        )
        ceiling_line = (
            f"Ceiling it may not cross: ${ceiling:,.2f}\n"
            if isinstance(ceiling, (int, float)) else ""
        )
        body = (
            "ENTRY DID NOT FILL — cancelled at the end of its session\n"
            f"{symbol}: the entry limit did not fill; {ending}.\n"
            f"Prices tried: {tried}\n"
            f"{ceiling_line}"
            f"Broker order {info.get('order_id')}: CANCELLED (last status "
            f"{info.get('status', 'unknown')}), filled 0. It was NOT left "
            "working at the broker.\n"
            "\n"
            "WHY CANCELLED: this desk re-analyses from scratch each session "
            "at current prices. An order resting past its own session would "
            "be acting on analysis the desk has already replaced. If the "
            "next session still wants this trade it will propose it again "
            "at real current prices.\n"
            "Nothing new was submitted automatically — the desk will not "
            "resubmit this one by itself. YOUR CHOICE: leave it to the next "
            "session, or place a fresh entry at a price you are willing to "
            "pay."
        )
        from src import notifier as _notifier
        _notifier.send_owner_alert(body, symbols=[str(symbol)])
    except Exception as exc:  # noqa: BLE001
        record_swallowed(pipeline, "alert.entry_cancelled", exc,
                         symbol=str(spec.get("symbol")))
        logger.warning(
            "entry-cancel alert for %s could not be sent: %s",
            spec.get("symbol"), exc,
        )
    else:
        record_clean_pass(pipeline, "alert.entry_cancelled",
                          symbol=str(spec.get("symbol")))

def _alert_unmeasurable_symbols(faults: dict[str, dict]) -> None:
    """Page the owner: these symbols could not be MEASURED this session.

    2026-09-12. A data fault — no price, no ATR, no usable bars, no
    analysis at all — used to be filed as a trade the constructor rejected,
    so a dead feed and a trade that failed its rules were the same class
    of outcome in the record and nobody could count either. The symbol was
    simply, silently, not traded. This is the alert half of the split:
    `PortfolioConstructor.last_data_faults` is recorded under its own
    `data_fault` reason (see `_record_constructor_drops`), and paged here.

    ONE message per session listing every unmeasurable symbol, not one per
    symbol: an outage hits the whole universe at once and sixty pages are
    less readable than one list. Same standalone `send_owner_alert` path
    and same rules as the entry-cancel alert above — its own Telegram
    message, never a line in the run summary; severity in TEXT, never
    colour; NOT deduplicated, because a feed that is still broken tomorrow
    should page again.

    Never raises. An alerting bug must not break the decision path.
    """
    try:
        if not faults:
            return
        symbols = sorted(faults)
        lines = []
        for sym in symbols:
            entry = faults.get(sym) or {}
            lines.append(f"  {sym}: {entry.get('fault', 'unknown')} — {entry.get('detail', '')}")
        body = (
            "DATA FAULT — symbols UNMEASURABLE this session, not judged\n"
            f"{len(symbols)} symbol(s) could not be measured because an input "
            "a real market always has (a price, volatility, usable bars, an "
            "analysis) was not obtained by the desk:\n"
            + "\n".join(lines) + "\n"
            "\n"
            "WHAT HAPPENED: none of these was traded (fail-closed, "
            "unchanged). They are recorded as data faults, NOT as trades "
            "the desk rejected, so the 'why didn't we trade' statistics are "
            "not contaminated. No trade judgement was made on any of them.\n"
            "WHAT TO CHECK: the market data provider and the bar history "
            "for these names before trusting today's no-trade outcome on "
            "them."
        )
        from src import notifier as _notifier
        _notifier.send_owner_alert(body, symbols=symbols)
    except Exception as exc:  # noqa: BLE001
        record_swallowed(None, "alert.unmeasurable_symbols", exc)
        logger.warning("unmeasurable-symbols alert could not be sent: %s", exc)
    else:
        record_clean_pass(None, "alert.unmeasurable_symbols")

