"""Pure aggregation logic for evening-analyst-flagged universe-expansion
candidates ("watchlist candidates").

Extracted from `TradingPipeline._build_watchlist_candidates` (Stage 2
Checkpoint C candidates gap) so `src/api/db_reads.py` can compute the same
canonical output from its own read-only `insights` query, without importing
`TradingPipeline` or any trading-execution code — the isolation boundary
`src/api/` exists to hold (see `docs/architecture/MISSION_CONTROL_API.md`).

Takes already-fetched `insights` rows (the shape `Database.get_recent_insights`
/ `src.api.db_reads.get_recent_insights` both return) and returns the
aggregated list. No I/O, no DB/broker access — safe to import from both the
trading pipeline and the read-only API. `TradingPipeline._build_watchlist_candidates`
is now a thin wrapper around `build_watchlist_candidates` below; its
docstring (contract, sort order, symbol semantics) is the authoritative
description and is not repeated here.
"""

from __future__ import annotations

import json


def build_watchlist_candidates(rows: list[dict], lookback_days: int) -> list[dict]:
    by_symbol: dict[str, dict] = {}
    for row in rows[:lookback_days]:
        row_date = row.get("date") or ""
        raw = row.get("missed_opportunities_json")
        if not raw:
            continue
        try:
            items = json.loads(raw)
        except (TypeError, ValueError):
            continue
        if not isinstance(items, list):
            continue
        for m in items:
            if not isinstance(m, dict):
                continue
            rec = (m.get("universe_addition_recommendation") or "no").strip()
            if rec not in ("add", "watch"):
                continue
            sym = (m.get("symbol") or "").strip().upper()
            if not sym:
                continue
            bucket = by_symbol.setdefault(sym, {
                "symbol": sym,
                "add_count": 0,
                "watch_count": 0,
                "dates": [],
                "themes": set(),
                "latest_reason": "",
                "latest_miss_category": "",
            })
            if rec == "add":
                bucket["add_count"] += 1
            else:
                bucket["watch_count"] += 1
            if row_date:
                bucket["dates"].append(row_date)
            theme = (m.get("theme_if_any") or "").strip()
            if theme:
                bucket["themes"].add(theme)
            # Rows come newest-first from get_recent_insights, so the
            # first non-empty reason/category we see is the freshest.
            reason = (m.get("universe_addition_reason") or "").strip()
            if reason and not bucket["latest_reason"]:
                bucket["latest_reason"] = reason[:240]
            cat = (m.get("miss_category") or "").strip()
            if cat and not bucket["latest_miss_category"]:
                bucket["latest_miss_category"] = cat

    results: list[dict] = []
    for sym, bucket in by_symbol.items():
        bucket["themes"] = sorted(bucket["themes"])
        bucket["total_flags"] = bucket["add_count"] + bucket["watch_count"]
        # Dates were appended newest-first (rows iteration), but belt them
        # by sorting desc in case the source rows are ever out of order.
        bucket["dates"] = sorted(set(bucket["dates"]), reverse=True)
        results.append(bucket)
    results.sort(
        key=lambda b: (
            -b["add_count"], -b["watch_count"], -b["total_flags"],
            b["symbol"],
        ),
    )
    return results
