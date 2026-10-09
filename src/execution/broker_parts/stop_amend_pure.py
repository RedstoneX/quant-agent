"""Pure helpers for the stop amend path, moved out of `stop_amend.py`.

Both are functions of their arguments alone: no broker, no client, no
database. They can be exercised with a bare exception or a bare float, which
is the boundary test for a split.
"""

from __future__ import annotations

import math


def _is_terminal_broker_rejection(exc: BaseException) -> bool:
    """True when the broker's OWN answer says retrying is pointless.

    Board item 129: the retry burst below used to catch every exception
    identically, so a deterministic rejection (bad price, unsupported qty,
    a closed/unknown symbol — Alpaca's `APIError.status_code` 400/404/422,
    same classification `get_asset_record` and `get_intraday_snapshots`
    already use for exactly these codes) burned the full attempt budget
    and backoff before alerting, exactly the "delays the owner alert"
    outcome the retry ceiling was written to avoid. A 429/5xx/timeout/
    dropped-connection failure has no such status (or a 429/5xx one) and
    is genuinely worth another try, so only these codes short-circuit.
    """
    status_code = getattr(exc, "status_code", None)
    return status_code in (400, 404, 422)


def _quantize_price(price: float | None) -> float | None:
    """Round to Alpaca's minimum tick size: $0.01 for stocks ≥ $1, $0.0001 below.

    The quote-midpoint in `get_latest_price` can produce sub-penny values like
    $106.515; submitting that raw triggers Alpaca error 42210000 and the order
    is rejected. Observed 2026-04-17 morning: UPS BUY @ $106.515 rejected.

    NaN/Inf handling: NaN comparisons all return False, so the original
    `price <= 0` guard fell through to `round(nan, ...)` = nan. The NaN
    then propagated all the way to Alpaca's submit_order, which silently
    broker-rejects the order and corrupts audit logs. Treat NaN/Inf as
    None (no quotable price) — callers' existing
    `price is not None and price > 0` checks then skip the order or
    fall back to market. Zero/negative values are preserved unchanged
    (pre-existing semantics: caller decides what to do with them).
    """
    if price is None:
        return None
    if not math.isfinite(price):
        return None
    if price <= 0:
        return price
    return round(price, 2 if price >= 1.0 else 4)
