"""Parts of the coverage repair: how a refusal is recorded, and where a
protective level comes from when the desk holds no opening row.

Split out of `src/execution/stop_repair.py` on 2026-10-02 to keep that
module inside its size baseline; both names are reached only from there.
"""

from __future__ import annotations

import logging
from typing import Any

from src.stop_price_classification import STOP_USABLE, classify_stop_price

logger = logging.getLogger(__name__)


def _refuse(
    outcome: dict | None,
    reason: str,
    *,
    code: str = "",
    record: dict | None = None,
    **extra,
) -> bool:
    """Record WHY this repair did not happen, then report it as not repaired.

    docs/WORK.md item 88. Every `return False` in this function is one of
    two very different things — "the broker would not take the order" or
    "this janitor refuses to place this number" — and the caller used to
    receive only the bare bool. `coverage == 'none'` then paged the owner
    with "the automatic repair could not restore one" and no reason at
    all, and a 'partial' gap carried the refusal no further than a log
    line. `reason` is a plain sentence, owner-facing, stamped onto the
    caller's gap dict when it supplies one.

    `record` carries what the durable row needs (db, symbol, qty, what the
    broker already held). Until 2026-09-19 a refusal lived only in that
    in-memory dict, a log line and a one-shot Telegram, so the refusal that
    left ~$223 of shares unprotected on 2026-09-18 could not be counted or
    audited afterwards. Every refusal now also writes one
    `kind='stop_repair_refusal'` row (`src/execution/exit_path_records.py`).
    The write never changes the return value.
    """
    if isinstance(outcome, dict):
        outcome["repair_refusal"] = reason
        # The machine-readable half of the same fact. The sentence above is
        # for the owner; `code` is what the alerting path needs in order to
        # tell an EXPECTED refusal apart from a fault, and until now it went
        # only to the durable row — so the caller could render the reason
        # and could not classify it. Adding it here changes no refusal and
        # no return value; `_refuse` still returns False for every code.
        outcome["repair_refusal_code"] = code
    if record is not None:
        from src.execution.exit_path_records import record_stop_repair_refusal

        record_stop_repair_refusal(
            record.get("db"),
            code=code,
            reason=reason,
            **{k: v for k, v in record.items() if k != "db"},
            **extra,
        )
    return False


def derive_protective_level(
    *,
    broker: Any,
    market: Any,
    symbol: str,
    is_short: bool,
) -> tuple[float | None, str]:
    """A protective level for a position the desk has NO opening row for.

    Owner ruling 2026-10-02: the desk must act rather than flag a gap for a
    human, after it has exhausted retries and different asks. The archive
    has already been asked (`last_buy`); this is the DIFFERENT ask — the
    broker's own position record, whose average entry price is a second,
    independent source for where the position was opened.

    The width is NOT a new number. It is `stop_atr_multiple` — the same
    function the entry path uses — asked with no setup and no regime, which
    returns the declared base `src.config.RiskConfig.min_stop_atr_multiple`
    (`config/number_ledger.yaml`), times the name's own ATR(14) from
    `src.data.technical.atr_for_symbol`. Risk is read off the name's own
    behaviour, never off a global percentage.

    Returns `(None, "")` whenever any input is missing: no anchor, no bar
    history, no ATR. Refusing is then the honest outcome and the caller
    keeps its existing refusal.
    """
    anchor = None
    try:
        for pos in broker.get_positions() or []:
            if str(getattr(pos, "symbol", "") or "").upper() != symbol.upper():
                continue
            state, price = classify_stop_price(getattr(pos, "avg_entry_price", None))
            if state == STOP_USABLE:
                anchor = price
            break
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "coverage repair: broker position read failed for %s: %s",
            symbol,
            exc,
        )
    if anchor is None:
        return None, ""
    source = market if market is not None else broker
    if not callable(getattr(source, "get_ohlcv", None)):
        return None, ""
    try:
        from src.data.technical import atr_for_symbol
        from src.portfolio_constructor.config import ConstructorConfig
        from src.portfolio_constructor.stops import stop_atr_multiple

        atr = atr_for_symbol(source, symbol)
        if not atr or atr <= 0:
            return None, ""
        multiple = float(stop_atr_multiple(ConstructorConfig(), None, None))
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "coverage repair: could not derive a stop width for %s: %s",
            symbol,
            exc,
        )
        return None, ""
    level = anchor + multiple * atr if is_short else anchor - multiple * atr
    state, level = classify_stop_price(level)
    if state != STOP_USABLE:
        return None, ""
    sign = "+" if is_short else "-"
    return level, (
        f"derived from the broker's own average entry ${anchor:,.2f} {sign} "
        f"{multiple:g} x ATR(14) ${atr:,.2f} "
        f"(src.config.RiskConfig.min_stop_atr_multiple, config/number_ledger.yaml) "
        f"because the desk holds no opening row for this position"
    )
