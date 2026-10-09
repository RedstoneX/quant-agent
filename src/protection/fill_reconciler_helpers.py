"""Pure helpers lifted out of `fill_reconciler` so that file does not grow.

Re-exported there under the same names, so every caller and patch target is unchanged.
"""

import math


def _finite_float_or_none(value) -> float | None:
    """Coerce a broker fill field to a finite float, or None.

    Rejects None, bool, non-numeric types (a MagicMock exposes ``__float__``
    but is NOT an int/float instance — same defensive posture as
    ``_optional_risk_number``), and NaN/inf, so a non-numeric value can never
    reach a DB bind. ``update_trade_fill``'s ``fill_price`` column is nullable,
    so a None price is a safe "unknown, backfill later" that the next
    reconciliation pass replaces with the broker's numeric average.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    f = float(value)
    return f if math.isfinite(f) else None


def _reconciled_exit_action(order_type: str | None) -> str:
    """Map a broker fill's order_type to the HONEST action to record for an
    exit the reconciler recovered (item 173(a)).

    `_reconcile_stop_out_fills` writes back exits the broker made that the
    ledger never saw. It used to label every one STOP_OUT — a protective
    stop — even when the broker fill was an ordinary market/limit sell.
    That misattributes owner-facing realized-P&L cause. The broker already
    reports each fill's order_type (`AlpacaBroker.list_filled_sell_orders`);
    this decides the action from it and NEVER guesses STOP_OUT:

      - a genuine stop / stop-limit / trailing-stop  -> STOP_OUT
      - a market or limit sell                       -> SELL
      - anything missing or unrecognised             -> RECONCILED_EXIT
        (an honest 'the broker closed this, cause unattributed' marker —
        never a protective stop the broker record can't substantiate)
    """
    ot = (order_type or "").strip().lower()
    if not ot:
        return "RECONCILED_EXIT"
    # stop / stop_limit / trailing_stop all name a broker-resident protective
    # stop; substring match tolerates enum spellings like "OrderType.STOP".
    if "stop" in ot or "trailing" in ot:
        return "STOP_OUT"
    if ot in ("market", "limit") or ot.endswith(".market") or ot.endswith(".limit"):
        return "SELL"
    return "RECONCILED_EXIT"
