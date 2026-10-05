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

HOW IT RUNS, AND WHY IT IS NOT A MERGE GATE
-------------------------------------------
On a timer, `quant-agent-stored-target-check.timer`, through
`scripts/run_stored_target_check.sh` — the same shape as every other
read-only scheduled check on this box, and it pushes to the SAME Telegram
channel they do rather than inventing a reporting path of its own.

It is deliberately NOT a required CI check. It is legitimately red today,
on real findings about real held positions that the desk cannot correct,
and a permanently-red required check blocks every unrelated merge and gets
switched off inside a day. A check that is switched off reports nothing;
one that speaks once a day reports the truth.

Usage:
    scripts/check_stored_targets.py
    scripts/check_stored_targets.py --db data/quant_agent.db
    scripts/check_stored_targets.py --symbol UPS --symbol META
    scripts/check_stored_targets.py --json
    scripts/check_stored_targets.py --no-telegram

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


# `walls_between` was MOVED to `src.risk.target_revision` and is imported
# here rather than re-declared. It is now the trigger test the live
# revision path runs as well as the test this report runs, and two copies
# would let the scheduled report and the live path disagree about the same
# chart. Re-exported under this module's name because that is where the
# desk's tests and callers already reach for it.
from src.risk.target_revision import walls_between  # noqa: E402


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


def format_message(results: list[dict]) -> str:
    """The owner-facing sentence(s) for the scheduled run. Plain English,
    no file paths, no machine codes, no jargon — he reads this on a phone
    and is not a developer.

    THE WORDING RULE THIS OBEYS: a finding the desk cannot correct is
    reported as exactly that — the true state, named. So the two cases are
    written differently on purpose.

    * A target with a wall in front of it AND a derivable replacement is
      reported with both numbers, because the desk knows what the right
      answer is and the reader can check it.
    * A target with a wall in front of it and NO derivable replacement is
      reported as a number that is wrong and cannot be corrected. It is
      not softened into "drift", not dressed up as an error in the check,
      and not left out. No number is substituted — the derivation refused,
      and a substituted number would be the made-up target this whole
      module exists to keep out of the book.

    Returns "" when there is nothing to say, so the caller stays silent
    rather than sending a daily all-clear nobody reads.
    """
    bad = [r for r in results if r.get("finding") == FINDING_AIMS_PAST_WALL]
    if not bad:
        return ""

    correctable = [r for r in bad if r.get("derived_target") is not None]
    stuck = [r for r in bad if r.get("derived_target") is None]

    parts: list[str] = []
    if correctable:
        lines = "; ".join(
            f"{r['symbol']} is quoted at ${r['stored_target']:,.2f} but the "
            f"nearest level it has to get through is "
            f"${r['derived_target']:,.2f}"
            for r in correctable
        )
        parts.append(
            f"{len(correctable)} held position(s) are quoting a profit "
            f"target with a price level still standing in front of it, so "
            f"the number on screen is further away than the chart says: "
            f"{lines}."
        )
    if stuck:
        lines = "; ".join(
            f"{r['symbol']} (quoted ${r['stored_target']:,.2f})"
            for r in stuck
        )
        parts.append(
            f"{len(stuck)} held position(s) are quoting a target with a "
            f"level still in front of it that the desk CANNOT recompute: "
            f"{lines}. Those numbers are wrong and there is no correct "
            f"replacement to put in their place — the position has already "
            f"run past everything the chart offers within reach of its "
            f"entry. Reporting it, not fixing it, and not inventing a "
            f"number to fill the gap."
        )
    return " ".join(parts)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=DEFAULT_DB)
    parser.add_argument("--symbol", action="append", default=[])
    parser.add_argument("--lookback-days", type=int, default=1800,
                        help="bar history for the level scan; matches "
                             "config/settings.yaml trading.lookback_days")
    parser.add_argument("--json", action="store_true")
    parser.add_argument(
        "--no-telegram", action="store_true",
        help="Print findings but don't push a Telegram alert.",
    )
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

    # The scheduled run's whole point: a false target on screen becomes
    # something the desk SAYS, not something someone has to remember to
    # look for. Silence when there is nothing to say — a check that speaks
    # every session is a check nobody reads.
    message = format_message(results)
    if message and not args.no_telegram:
        print(message)
        from src.notifier import TelegramNotifier
        from src.notifier.owner_alert_funnel import send_owner_alert_with_outcome

        notifier = TelegramNotifier()
        if notifier.enabled:
            send_owner_alert_with_outcome(message, notifier=notifier, kind="stored_targets", pnl_header=False)
        else:
            print(
                "check_stored_targets: Telegram not configured; message "
                "printed above only",
                file=sys.stderr,
            )

    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
