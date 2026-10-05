"""Entry limit bound, and a counted row for every slippage check.

`execution.max_entry_slippage_bps` (40bp) is a FLAT tolerance: the same
allowance for a name that moves 1% a day and one that moves 6%. The owner's
standing ruling (2026-10-04) is that a money number must be expressed against
the stock's own movement, so 40 is filed `arbitrary` in the number ledger.

It cannot be re-derived yet. The production DB holds no record of this check
ever binding — nothing persists its outcome, so the count of entries it has
turned away or priced away from the book is not zero, it is UNKNOWN. Two
earlier re-derivations were spent tuning paths that had never run.

So this module changes no behaviour. It is the arithmetic that already lived
inline in the BUY and SHORT entry blocks, lifted out unchanged, plus one
best-effort evidence row per check carrying what a derivation needs:

* the slippage actually observed (how far the displayed quote sat from the
  reference, in bp),
* the cap in force,
* whether the cap BOUND (observed beyond it),
* and that name's own most recent daily range as a percent of its close,
  which is the scale a per-name expression would be measured against.

Persistence is best-effort by construction (trading-core rule): a failed write
never affects the order. Read the rows back with
``kind = 'entry_slippage_check'`` in `specialist_evidence`.
"""
from __future__ import annotations

from src.sentinel.guarded import NO_LEDGER, record_guarded_pass
import json
import logging
from typing import Any, NamedTuple

logger = logging.getLogger(__name__)


class EntryBound(NamedTuple):
    """The limit price bound, its rounded limit, and the observed slippage."""

    bound_price: float
    limit_price: float
    observed_bps: float


def latest_daily_range_pct(bars: Any) -> float | None:
    """Most recent bar's high-low range as a percent of its close.

    The name's own movement, read off the bars the session already loaded —
    not a second data call and not a new number. Returns None when the bars
    are missing or malformed; the recorder then stores a null rather than a
    guess.
    """
    try:
        last = (bars or [])[-1]
    except Exception:  # noqa: BLE001
        return None
    try:
        high = float(last["high"] if isinstance(last, dict) else last.high)
        low = float(last["low"] if isinstance(last, dict) else last.low)
        close = float(last["close"] if isinstance(last, dict) else last.close)
    except Exception as exc:  # noqa: BLE001
        record_guarded_pass(NO_LEDGER, "entry_slippage_bound.bar_shape", exc)
        return None
    if not close > 0 or high < low:
        return None
    return (high - low) / close * 100.0


def record_slippage_check(
    pipeline: Any,
    ctx: Any,
    symbol: str,
    *,
    is_short: bool,
    reference_price: float,
    quote_price: float | None,
    observed_bps: float,
    cap_bps: float,
    bound_price: float,
    pinned: bool,
) -> None:
    """One counted row per slippage check. Never raises, never blocks a trade."""
    try:
        from src.pipeline_stages import _persist_evidence

        bars = getattr(ctx, "symbols_bars", None)
        bars = bars.get(symbol) if isinstance(bars, dict) else None
        payload = {
            "direction": "short" if is_short else "long",
            "reference_price": reference_price,
            "quote_price": quote_price,
            "observed_slippage_bps": round(observed_bps, 2),
            "cap_bps": cap_bps,
            "cap_bound": bool(observed_bps > cap_bps),
            "bound_price": bound_price,
            "cap_was_pinned": pinned,
            "daily_range_pct": latest_daily_range_pct(bars),
        }
        _persist_evidence(
            pipeline.db, run_id=getattr(ctx, "run_id", None),
            agent_name="execution", kind="entry_slippage_check",
            scope="symbol", symbol=symbol,
            decision_id=getattr(ctx, "decision_id", None),
            evidence_json=json.dumps(payload),
        )
    except Exception as e:  # noqa: BLE001
        logger.debug("slippage check not recorded for %s: %s", symbol, e)


def entry_bound(
    pipeline: Any,
    ctx: Any,
    symbol: str,
    market_price: float,
    quote_price: float | None,
    slippage_bps: float,
    *,
    is_short: bool,
) -> EntryBound:
    """The BUY ceiling / SHORT floor, unchanged, with the check recorded.

    A ceiling pinned at approval wins over a freshly computed one exactly as
    it did inline: the approved entry ceiling is the number risk signed off.
    """
    pinned_raw = (getattr(ctx, "approved_entry_ceiling", None) or {}).get(symbol)
    pinned = isinstance(pinned_raw, (int, float)) and pinned_raw > 0
    if pinned:
        bound_price = float(pinned_raw)
    elif is_short:
        bound_price = market_price * (1 - slippage_bps / 10_000.0)
    else:
        bound_price = market_price * (1 + slippage_bps / 10_000.0)
    limit_price = round(bound_price, 2 if bound_price >= 1 else 4)
    if is_short:
        observed_bps = (market_price - float(quote_price)) / market_price * 10_000.0
    else:
        observed_bps = (float(quote_price) - market_price) / market_price * 10_000.0
    record_slippage_check(
        pipeline, ctx, symbol, is_short=is_short,
        reference_price=market_price, quote_price=quote_price,
        observed_bps=observed_bps, cap_bps=slippage_bps,
        bound_price=bound_price, pinned=pinned,
    )
    return EntryBound(bound_price, limit_price, observed_bps)
