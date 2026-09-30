#!/usr/bin/env python3
"""Flag any held position whose STORED take-profit disagrees with what the
current derivation produces from that position's own pinned inputs.

Deterministic and READ-ONLY. No LLM call, no write of any kind, no new
alert path, no new constant.

WHY THIS EXISTS
---------------
Commit 8f4f77c4 fixed a real derivation bug: `derive_structural_target`
used the noise floor to FILTER its candidate levels, so a structural level
inside one ATR of entry left the candidate set entirely and `min(...)`
promoted the target to the NEXT level out — past the very wall price had
just been rejected from. The closer the wall, the further past it the desk
aimed.

The fix corrected the code. It could not correct the numbers already
frozen on `trades.take_profit` for positions opened before it, and nothing
in the desk compared a stored target against what the code would say
today. Those rows sat on Telegram and the dashboard for as long as the
positions were held, and a reader had no way to tell a good number from
one the desk would never compute again.

A one-off backfill fixes the rows that exist. It does not stop the next
derivation bug spending a week invisible. This script is the mechanical
version of the question the backfill asked by hand, and it is meant to run
on a schedule on the box.

THE TWO FINDINGS, AND WHY ONLY ONE OF THEM IS AN ERROR
------------------------------------------------------
1. AIMING PAST A WALL (exit 1). A structural level still in the way stands
   BETWEEN the position and its stored target. That is this bug's exact
   signature and it is a doctrine violation in its own right — "the target
   is the nearest wall, never the level past it". It is also stable: a
   target with no wall in front of it does not acquire one because ATR
   moved a little, so this finding does not fire on ordinary drift.

2. DRIFT FROM THE CURRENT DERIVATION (reported, exit 0). The stored number
   simply is not what the derivation returns today. This is NORMAL and
   usually means nothing is wrong: the derivation reads today's bars, so
   new pivots, a re-clustered level and a moved ATR all change the answer
   without anything being broken, and the desk deliberately does NOT
   re-derive a target on a price move. It is printed because it is the
   literal comparison an operator wants to see, and it is never an error
   because a check that fires every session is a check nobody reads.

A refusal from the derivation — no structure left in the trade's
direction, a derivable target the price has already passed, no pinned
horizon — is NOT a finding either way. It is a correct answer about a
position, and it is printed as one.

WHAT IS HELD FIXED, and it is not negotiable: the ENTRY PRICE, the PINNED
HORIZON (`trades.expected_horizon_sessions`) and the PINNED SETUP TYPE.
Only levels, ATR and coverage come from today's bars. Re-deriving from the
current price would make the comparison a function of the price move,
which is the one thing a target must never be. The derivation itself is
`src.risk.target_revision.assess_bugfix_backfill`, which is the same body
the live revision path runs — this script implements no derivation of its
own, on purpose.

Usage:
    scripts/check_stored_targets.py
    scripts/check_stored_targets.py --db data/quant_agent.db
    scripts/check_stored_targets.py --symbol UPS --symbol META
    scripts/check_stored_targets.py --json

Exit codes:
    0  no position aims past a wall (drift and refusals may still be
       printed), or the check could not run for want of bars
    1  at least one held position's stored target sits beyond a structural
       level that is still in the way
    3  the database could not be read at all — an operator problem, not a
       finding about any position
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

DEFAULT_DB = str(PROJECT_ROOT / "data" / "quant_agent.db")

#: Findings, by name so a caller never matches on prose.
FINDING_AIMS_PAST_WALL = "TARGET_AIMS_PAST_A_STANDING_WALL"
FINDING_DRIFT = "TARGET_DIFFERS_FROM_TODAYS_DERIVATION"
FINDING_REFUSED = "DERIVATION_REFUSED"
FINDING_AGREES = "TARGET_AGREES_WITH_TODAYS_DERIVATION"
FINDING_NO_BARS = "NO_BARS_FOR_SYMBOL"


def walls_between(
    *,
    stored_target: float | None,
    reference_price: float | None,
    surviving_levels: list[float] | tuple[float, ...] | None,
    is_short: bool,
) -> list[float]:
    """Every structural level standing BETWEEN the position and its stored
    target, nearest first. Empty is the healthy answer.

    PURE, and deliberately separate from the derivation so it can be
    tested without bars. `surviving_levels` must already have been put
    through `src.risk.target_revision.levels_still_in_the_way`, because a
    level price has closed decisively beyond is not a wall any more and
    counting it would manufacture a finding out of a broken ceiling.

    `reference_price` is where the position is measured FROM, and the
    caller passes the ENTRY, not the current price. That is the bug's own
    geometry: the noise filter dropped levels sitting close to ENTRY, so a
    wall between the entry and the stored target is exactly what it left
    behind. It is also what keeps this finding stable — measuring from the
    latest close would flag every position that has moved away from its
    entry, which is a statement about the price, not about the target, and
    a target must never be a function of the price move.

    Whether a level still counts at all IS a question about today: the
    caller filters with `levels_still_in_the_way` first, so a ceiling price
    has closed decisively through is not counted as a wall.

    A level exactly ON the target is not between anything and is excluded
    — that is the target sitting on its own wall, which is the correct
    outcome, not a finding. Strict inequalities on both ends do that.
    """
    try:
        target = float(stored_target)  # type: ignore[arg-type]
        ref = float(reference_price)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return []
    if not target > 0 or not ref > 0:
        return []
    out: list[float] = []
    for raw in surviving_levels or ():
        try:
            level = float(raw)
        except (TypeError, ValueError):
            continue
        if not level > 0:
            continue
        if is_short:
            # A short's target sits below; a wall is a floor it must get
            # through on the way down.
            if target < level < ref:
                out.append(level)
        elif ref < level < target:
            out.append(level)
    return sorted(out, reverse=bool(is_short))


def _open_positions(conn, wanted: set[str] | None) -> list[dict]:
    """Held symbols with the broker's own entry, from the `positions`
    table — broker truth, and the same source the live revision path reads
    direction and entry from."""
    rows = [dict(r) for r in conn.execute(
        "SELECT symbol, qty, avg_entry FROM positions ORDER BY symbol",
    )]
    if wanted:
        rows = [r for r in rows if str(r["symbol"]).upper() in wanted]
    return rows


def _opening_row(conn, symbol: str, action: str) -> dict | None:
    """The row carrying this position's live take_profit — the same
    `get_symbol_last_buy` predicate and ordering the desk itself uses, so
    this check and the pipeline can never read different rows."""
    predicate = (
        "((fill_status IS NULL AND action != 'HOLD') OR fill_status = 'filled' "
        "OR COALESCE(fill_qty, 0) > 0)"
    )
    row = conn.execute(
        "SELECT * FROM trades WHERE symbol = ? AND action = ? "
        f"AND {predicate} ORDER BY timestamp DESC, id DESC LIMIT 1",
        (symbol, action),
    ).fetchone()
    return dict(row) if row else None


def assess_position(*, symbol, is_short, entry_price, row, bars):
    """One position's verdict. Returns a plain dict so `--json` and the
    printed report cannot disagree about what was found."""
    from src.data.levels import find_structural_levels, structure_coverage
    from src.data.technical import compute_indicators
    from src.risk.target_revision import (
        assess_bugfix_backfill,
        levels_still_in_the_way,
    )

    stored = None
    try:
        stored = float(row.get("take_profit") or 0) or None
    except (TypeError, ValueError):
        stored = None

    out = {
        "symbol": symbol,
        "direction": "short" if is_short else "long",
        "stored_target": stored,
        "entry_price": entry_price,
        "pinned_horizon_sessions": row.get("expected_horizon_sessions"),
        "setup_type": row.get("setup_type"),
    }
    if not bars:
        out["finding"] = FINDING_NO_BARS
        out["detail"] = (
            "no completed daily bars could be read for this symbol, so "
            "nothing about its target can be measured — a data fault, not "
            "a finding about the position"
        )
        return out

    close = float(bars[-1].close)
    bar_date = str(bars[-1].date)
    atr = compute_indicators(symbol, bars).atr_14
    supports, resistances = find_structural_levels(bars)
    levels = sorted(lv.price for lv in (*supports, *resistances))
    coverage = structure_coverage(bars)
    surviving = levels_still_in_the_way(
        computed_levels=levels, close_price=close, atr=atr, is_short=is_short,
    )
    out.update({"close": close, "bar_date": bar_date, "atr": atr})

    walls = walls_between(
        stored_target=stored, reference_price=entry_price,
        surviving_levels=surviving, is_short=is_short,
    )

    outcome = assess_bugfix_backfill(
        symbol=symbol, direction="short" if is_short else "long",
        entry_price=entry_price, stored_target=stored,
        pinned_horizon_sessions=row.get("expected_horizon_sessions"),
        setup_type=row.get("setup_type") or None,
        levels=levels, atr=atr, close_price=close, levels_coverage=coverage,
    )
    out["derivation_code"] = outcome.code
    out["derived_target"] = outcome.new_price
    out["derivation_detail"] = outcome.detail

    if walls:
        out["finding"] = FINDING_AIMS_PAST_WALL
        out["walls_in_the_way"] = walls
        out["detail"] = (
            f"the stored target ${stored:,.2f} sits beyond "
            f"{len(walls)} structural level(s) still in the way "
            f"({', '.join(f'${w:,.2f}' for w in walls)}) between the "
            f"${entry_price:,.2f} entry and that target, none of which the "
            f"{bar_date} close of ${close:,.2f} has cleared — the target is "
            f"the nearest wall, never the level past it"
        )
        return out

    if outcome.new_price is None:
        out["finding"] = FINDING_REFUSED
        out["detail"] = outcome.detail
        return out

    if round(outcome.new_price, 2) == round(stored or 0.0, 2):
        out["finding"] = FINDING_AGREES
        out["detail"] = (
            f"today's derivation returns the stored ${stored:,.2f} from this "
            f"position's own pinned inputs"
        )
        return out

    out["finding"] = FINDING_DRIFT
    out["detail"] = (
        f"today's bars derive ${outcome.new_price:,.2f} from the pinned "
        f"${entry_price:,.2f} entry and "
        f"{row.get('expected_horizon_sessions')}-session horizon, against the "
        f"stored ${stored:,.2f} — no wall stands in front of the stored "
        f"target, so this is ordinary movement in the measurement and not an "
        f"error"
    )
    return out


def run(*, db_path: str, symbols: set[str] | None, lookback_days: int) -> list[dict]:
    import sqlite3

    from src.data.market import MarketDataProvider

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        positions = _open_positions(conn, symbols)
        market = MarketDataProvider()
        results: list[dict] = []
        for pos in positions:
            symbol = str(pos["symbol"]).upper()
            is_short = float(pos["qty"] or 0) < 0
            row = _opening_row(conn, symbol, "SHORT" if is_short else "BUY")
            if row is None:
                results.append({
                    "symbol": symbol,
                    "finding": FINDING_REFUSED,
                    "detail": (
                        "no executed opening row was found for a symbol the "
                        "broker shows as held, so there is no stored target "
                        "to compare"
                    ),
                })
                continue
            try:
                bars = market.get_ohlcv(symbol, lookback_days) or []
            except Exception as exc:  # noqa: BLE001
                bars = []
                print(f"check_stored_targets: bars failed for {symbol}: {exc}",
                      file=sys.stderr)
            results.append(assess_position(
                symbol=symbol, is_short=is_short,
                entry_price=float(pos["avg_entry"] or 0) or None,
                row=row, bars=bars,
            ))
        return results
    finally:
        conn.close()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=DEFAULT_DB)
    parser.add_argument("--symbol", action="append", default=[])
    parser.add_argument("--lookback-days", type=int, default=1800,
                        help="bar history for the level scan; matches "
                             "config/settings.yaml trading.lookback_days")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    wanted = {s.strip().upper() for s in args.symbol if s.strip()} or None
    try:
        results = run(db_path=args.db, symbols=wanted,
                      lookback_days=args.lookback_days)
    except Exception as exc:  # noqa: BLE001
        print(f"check_stored_targets: could not read {args.db}: {exc}",
              file=sys.stderr)
        return 3

    if args.json:
        print(json.dumps(results, indent=2, default=str))
    else:
        for r in results:
            print(f"{r['symbol']:<6} {r.get('finding', '?'):<38} "
                  f"{r.get('detail', '')}")

    bad = [r for r in results if r.get("finding") == FINDING_AIMS_PAST_WALL]
    if not args.json:
        print(f"\nchecked {len(results)} held position(s); "
              f"{len(bad)} aiming past a standing wall")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
