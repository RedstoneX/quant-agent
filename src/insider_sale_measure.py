"""Board item 63 — join the insider SALE census to the price that followed.

The census (`src.data.smart_money._sale_census`, written per fetch as the
`insider_sale_census` evidence row) records, for each sampled sale, the
symbol, the transaction date and the reference price. It deliberately does
NOT record a forward return, because the return is forward. This module is
the join: census rows in, realized forward returns out.

Three things this module is NOT, and must never become:

* It is not a gate, a weight or a rank. Nothing in the trading path imports
  it; it produces a measurement of the INSTRUMENT (do insider sales precede
  weaker forward returns?) and nothing else.
* It picks no horizon. `horizon_sessions` is supplied by the caller. The
  desk's own existing forward-return measurement windows are the evening
  analyst's next-day and five-session scorecard (`src/agents/evening_analyst.py`)
  and the parameter-free "to latest close" default of
  `src.sentiment_measure.forward_return`; nothing new is invented here.
* It never substitutes a return. A symbol missing from the bar set, a
  transaction date outside the bars, or a window that has not resolved
  yields `forward_return_pct: None` and is EXCLUDED from every summary,
  counted by reason so the exclusion is visible rather than silent.

The entry/exit convention is reused wholesale from
`src.sentiment_measure.forward_return` so this cannot drift from how the
desk already measures a realized forward move.
"""

from __future__ import annotations

import statistics
from typing import Iterable, Optional, Sequence

from src.sentiment_measure import forward_return

#: Why a census row produced no forward return. Every excluded row carries
#: exactly one of these, and the counts are reported alongside any summary.
EXCLUDED_NO_SYMBOL = "symbol_absent_from_bars"
EXCLUDED_NO_DATE = "no_transaction_date"
EXCLUDED_UNRESOLVED = "window_unresolved_in_bars"
EXCLUDED_BEFORE_BARS = "transaction_predates_bars"


def join_forward_returns(
    rows: Iterable[dict],
    bars_by_symbol: dict,
    horizon_sessions: Optional[int] = None,
) -> dict:
    """Attach a realized forward return to each census row it can resolve.

    `rows` are census rows: dicts carrying at least `symbol` and
    `transaction_date`, optionally `reference_price` and
    `holdings_fraction_band`.

    `bars_by_symbol` maps SYMBOL -> a sequence of daily bars carrying
    `.date` and `.close` (the OHLCV shape the market-data provider returns).

    Returns {records, n_rows, n_resolved, n_excluded, excluded_by_reason,
    horizon_sessions}. Every record keeps its row fields plus
    `forward_return_pct` (None when unresolved) and `excluded_reason`.
    """
    records: list[dict] = []
    excluded: dict[str, int] = {}

    for row in rows:
        symbol = str(row.get("symbol") or "").strip().upper()
        txn_date = str(row.get("transaction_date") or "").strip()
        rec = {
            "symbol": symbol,
            "transaction_date": txn_date,
            "holdings_fraction_band": row.get("holdings_fraction_band"),
            "reference_price": row.get("reference_price"),
            "forward_return_pct": None,
            "entry_date": None,
            "exit_date": None,
            "sessions_forward": None,
            "excluded_reason": None,
        }
        bars: Sequence = bars_by_symbol.get(symbol) or []
        if not txn_date:
            rec["excluded_reason"] = EXCLUDED_NO_DATE
        elif not bars:
            rec["excluded_reason"] = EXCLUDED_NO_SYMBOL
        elif txn_date < str(min(b.date for b in bars)):
            # `src.sentiment_measure.forward_return` takes the first bar on
            # or AFTER the date, so a transaction older than the bar set
            # would silently collapse onto the first bar and report a move
            # that began years later. That is a substituted return, which
            # this module forbids: exclude it and count it.
            rec["excluded_reason"] = EXCLUDED_BEFORE_BARS
        else:
            fr = forward_return(bars, txn_date, horizon_sessions)
            if fr is None:
                rec["excluded_reason"] = EXCLUDED_UNRESOLVED
            else:
                rec.update(
                    {
                        "forward_return_pct": fr["forward_return_pct"],
                        "entry_date": fr["entry_date"],
                        "exit_date": fr["exit_date"],
                        "sessions_forward": fr["sessions_forward"],
                    }
                )
        if rec["excluded_reason"]:
            excluded[rec["excluded_reason"]] = excluded.get(rec["excluded_reason"], 0) + 1
        records.append(rec)

    resolved = [r for r in records if r["forward_return_pct"] is not None]
    return {
        "records": records,
        "n_rows": len(records),
        "n_resolved": len(resolved),
        "n_excluded": len(records) - len(resolved),
        "excluded_by_reason": excluded,
        "horizon_sessions": horizon_sessions,
    }


def summarize(values: Sequence[float]) -> dict:
    """Mean, median, spread and a standard error for a set of returns.

    The standard error is reported so the mean is never read as a point
    fact: a difference smaller than the errors around it is not a finding.
    No threshold, band or cut-off is computed anywhere in this module, and
    none may be added — doctrine bars fitting a number to this desk's own
    record.
    """
    vals = [float(v) for v in values if v is not None]
    n = len(vals)
    if n == 0:
        return {
            "n": 0,
            "mean_pct": None,
            "median_pct": None,
            "stdev_pct": None,
            "stderr_pct": None,
            "share_negative_pct": None,
        }
    mean = sum(vals) / n
    stdev = statistics.stdev(vals) if n > 1 else None
    return {
        "n": n,
        "mean_pct": round(mean, 4),
        "median_pct": round(statistics.median(vals), 4),
        "stdev_pct": round(stdev, 4) if stdev is not None else None,
        "stderr_pct": round(stdev / (n**0.5), 4) if stdev is not None else None,
        "share_negative_pct": round(100.0 * sum(1 for v in vals if v < 0) / n, 2),
    }


def summarize_joined(joined: dict, group_key: str = "holdings_fraction_band") -> dict:
    """Overall and per-group summaries over the RESOLVED rows only.

    Excluded rows never enter a summary and never receive a substituted
    return; `joined["excluded_by_reason"]` is carried through so any reader
    of the summary also sees what did not make it in.
    """
    resolved = [r for r in joined.get("records", []) if r["forward_return_pct"] is not None]
    groups: dict[str, list[float]] = {}
    for r in resolved:
        key = str(r.get(group_key) or "unknown")
        groups.setdefault(key, []).append(r["forward_return_pct"])
    return {
        "horizon_sessions": joined.get("horizon_sessions"),
        "overall": summarize([r["forward_return_pct"] for r in resolved]),
        "by_group": {k: summarize(v) for k, v in sorted(groups.items())},
        "n_excluded": joined.get("n_excluded"),
        "excluded_by_reason": joined.get("excluded_by_reason", {}),
    }
