"""Counted rows for the smart-money text caps (ledger ids
`src.agents.smart_money_analyst._MAX_CONTEXT_TEXT_CHARS`, value 96, and
`src.agents.smart_money_analyst._MAX_REASON_TEXT_CHARS`, value 220).

Nothing recorded whether either cap ever cut text. `emit_row` writes ONE
`SM_CAP_ROW` line every time a cap is EVALUATED, carrying `bound` true or
false, so "never binds" and "never ran" are distinguishable: zero rows means
never ran, rows that are all `bound: false` means ran and never cut.

Nothing is stored: no module-level tally, no file. `summarise` computes the
counts from rows read back at the moment something asks.
"""
from __future__ import annotations

import json
import logging

logger = logging.getLogger(__name__)

#: Stable prefix every counted row carries.
ROW_TAG = "SM_CAP_ROW "


def emit_row(cap: str, limit: int, length: int, bound: bool) -> str:
    """Write one counted row for one evaluation of a cap and return it."""
    row = json.dumps(
        {"cap": cap, "limit": limit, "length_before": length,
         "kept": min(length, limit), "bound": bound},
        sort_keys=True,
    )
    logger.info("%s%s", ROW_TAG, row)
    return row


def bounded(value: str, limit: int, cap: str) -> str:
    """Truncate `value` to `limit` (same rule as before) and count the evaluation."""
    bound = len(value) > limit
    emit_row(cap, limit, len(value), bound)
    return value[:limit - 3] + "..." if bound else value


def rows_from(lines) -> list[dict]:
    """Every counted row in `lines`, in order. Unparseable rows are skipped."""
    out: list[dict] = []
    for line in lines:
        _head, sep, tail = str(line).partition(ROW_TAG)
        if not sep:
            continue
        try:
            out.append(json.loads(tail.strip()))
        except ValueError:
            continue
    return out


def summarise(rows) -> dict[str, dict[str, object]]:
    """Per cap: evaluations, binds, clears and the longest text seen."""
    out: dict[str, dict[str, object]] = {}
    for r in rows:
        s = out.setdefault(str(r.get("cap")), {"evaluated": 0, "bound": 0,
                                               "not_bound": 0, "longest_seen": 0})
        s["evaluated"] += 1
        if r.get("bound"):
            s["bound"] += 1
        else:
            s["not_bound"] += 1
        s["longest_seen"] = max(s["longest_seen"], int(r.get("length_before") or 0))
    return out
