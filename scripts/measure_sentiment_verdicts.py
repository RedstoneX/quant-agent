#!/usr/bin/env python3
"""Board item 125 — print how well the earnings seat's sentiment verdicts
held up against the price that followed.

Read-only. No LLM call, no broker write, no sizing. It reads the earnings
verdicts the desk already wrote to disk and the daily bars the desk already
fetches, then scores each directional verdict by the SIGN of the realized
move — a bullish call is wrong only if the stock fell, a bearish one only if
it rose. Neutral verdicts make no directional claim and are shown but not
scored. There is no tuned threshold anywhere: this MEASURES the seat, it does
not gate or size any trade (item 125's whole point is to measure before
trusting).

Usage:
    python -m scripts.measure_sentiment_verdicts [--horizon N] [--data-dir DIR]

--horizon N   score the return over exactly N trading sessions after the
              filing (a measurement window; omit it to measure the return
              from the filing to the latest available close).
"""

from __future__ import annotations

import argparse
import sys

from src.sentiment_measure import build_report


def _build_fetch_bars(lookback_days: int):
    from src.api.deps import get_alpaca_credentials, get_alpaca_paper
    from src.execution.broker import AlpacaBroker

    key, secret = get_alpaca_credentials()
    broker = AlpacaBroker(api_key=key, secret_key=secret, paper=get_alpaca_paper())

    def _fetch(symbol: str):
        return broker.get_bars(symbol, lookback_days=lookback_days)

    return _fetch


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--horizon", type=int, default=None,
                    help="trading sessions after the filing to measure (default: to latest close)")
    ap.add_argument("--data-dir", default="data/earnings")
    ap.add_argument("--lookback-days", type=int, default=400,
                    help="calendar days of daily bars to pull per symbol")
    args = ap.parse_args(argv)

    report = build_report(
        args.data_dir,
        _build_fetch_bars(args.lookback_days),
        horizon_sessions=args.horizon,
    )

    window = (f"{args.horizon} sessions" if args.horizon is not None
              else "filing -> latest close")
    print(f"Sentiment-verdict accuracy  (window: {window})")
    print(f"  verdicts on disk : {report['n_verdicts']}")
    print(f"  resolved (scored): {report['n_resolved']}")
    print(f"  contradicted     : {report['n_wrong']}")
    print()
    print("  by sentiment:")
    for name, b in sorted(report["by_sentiment"].items()):
        print(f"    {name:8s} resolved={b['resolved']:3d} "
              f"wrong={b['wrong']:3d} hit_rate={b['hit_rate_pct']}")
    print("  by conviction:")
    for name, b in sorted(report["by_conviction"].items()):
        print(f"    {name:8s} resolved={b['resolved']:3d} "
              f"wrong={b['wrong']:3d} hit_rate={b['hit_rate_pct']}")

    if report["n_verdicts"] == 0:
        print("\n  (no earnings verdicts on disk yet — nothing to score)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
