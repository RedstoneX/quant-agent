"""Entry take-profit target scorecard: how often does price REACH the target?

RECORD ONLY. Owner ruling 2026-10-09: the entry take-profit target is NOT a
sell rule. This module only counts; its output must never feed ranking,
sizing, exits or any threshold.

Counts positions opened on/after the clean-record start (calibration's
`CLEAN_RECORD_START_UTC`); earlier ones are excluded and counted by reason.

"Reached" uses `max_favourable_excursion`, a snapshot-based LOWER BOUND on the
best move (spikes between snapshots are missed). So "reached" is trustworthy;
"not reached" only means "not observed to reach". The target is the FROZEN
entry target (`initial_take_profit`), never the live `take_profit`.
"""

from __future__ import annotations

from src.storage.analytics.calibration import (
    _LONG_EXIT_ACTIONS,
    _POSITION_OPEN_ACTIONS,
    _is_filled_trail_stop,
    _opened_before_clean_record,
)

_COLUMNS = (
    "symbol, action, qty, price, fill_qty, fill_price, fill_status, timestamp, "
    "position_id, initial_take_profit, max_favourable_excursion"
)


def _is_exit(action: str, row) -> bool:
    return (
        action.startswith(("SELL", "PARTIAL_SELL", "PARTIAL_COVER"))
        or action in _LONG_EXIT_ACTIONS
        or action in ("COVER", "EMERGENCY_COVER")
        or _is_filled_trail_stop(row, action)
    )


def _qty(row) -> float:
    return float(row["fill_qty"] if row["fill_qty"] else row["qty"] or 0)


def _score_position(pid: str, prows: list) -> tuple[str, dict | None]:
    """Return (outcome, per-position record). Outcome is a status or an exclusion reason."""
    opening = next((r for r in prows if (r["action"] or "") in _POSITION_OPEN_ACTIONS), None)
    if opening is None:
        return "skip", None
    if _opened_before_clean_record(opening["timestamp"]):
        return "opened_before_clean_record", None
    side = _POSITION_OPEN_ACTIONS[opening["action"]]
    entry = float(opening["fill_price"] if opening["fill_price"] else opening["price"] or 0)
    target = opening["initial_take_profit"]
    if target is None or target <= 0 or entry <= 0 or (target <= entry if side == "long" else target >= entry):
        return "no_entry_target", None
    figures = [r["max_favourable_excursion"] for r in prows if r["max_favourable_excursion"] is not None]
    if not figures:
        return "no_best_move_figure", None
    net = 0.0
    for r in prows:
        act = r["action"] or ""
        if act in _POSITION_OPEN_ACTIONS:
            net += _qty(r)
        elif _is_exit(act, r):
            net -= _qty(r)
    distance = abs(target - entry)
    mfe = max(figures)
    if net > 1e-9:
        status = "still_open"
    else:
        status = "closed_reached" if mfe >= distance else "closed_not_reached"
    return status, {
        "position_id": pid,
        "symbol": opening["symbol"],
        "side": side,
        "status": status,
        "fraction_of_target_reached_lower_bound": mfe / distance,
    }


def compute_target_scorecard(rows) -> dict:
    """`rows`: trade rows (mapping access), chronological, with position_id."""
    by_pos: dict[str, list] = {}
    rows_without_position = 0
    for row in rows:
        if row["position_id"]:
            by_pos.setdefault(row["position_id"], []).append(row)
        else:
            rows_without_position += 1
    out: dict = {
        "closed_reached": 0,
        "closed_not_reached": 0,
        "still_open": 0,
        "excluded": {
            "opened_before_clean_record": 0,
            "no_entry_target": 0,
            "no_best_move_figure": 0,
            "trade_rows_without_position": rows_without_position,
        },
        "positions": [],
        "best_move_is_lower_bound": True,
    }
    for pid, prows in by_pos.items():
        outcome, record = _score_position(pid, prows)
        if record is not None:
            out[outcome] += 1
            out["positions"].append(record)
        else:
            out["excluded"][outcome] += 1  # an unknown outcome raises KeyError rather than vanishing
    return out


def target_scorecard(conn) -> dict:
    """Read the trades table and score it. Record only."""
    cur = conn.execute(f"SELECT {_COLUMNS} FROM trades ORDER BY timestamp, id")  # noqa: S608 - literal
    return compute_target_scorecard(cur.fetchall())
