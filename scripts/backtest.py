#!/usr/bin/env python3
"""Deterministic-layer backtester — CLI entry point.

Measures structural stop placement, noise-band stop widening, risk-based
position sizing, the portfolio risk budget / cluster caps, and the
trailing-stop rules against real history. It does NOT replay the LLM
agents — see `src/backtest/engine.py` for the full scope statement and the
declared simplifications. Read the caveats block this tool prints before
trusting a number out of it.

Usage:
  # single run, default universe (config/settings.yaml trading.universe)
  python scripts/backtest.py --config config/settings.yaml \\
      --start 2025-01-01 --end 2025-12-31

  # A/B: does widening the minimum stop distance change the outcome? This
  # is the tool's real purpose — point it at two config copies that differ
  # in exactly one parameter.
  python scripts/backtest.py --config config/settings_a.yaml \\
      --config-b config/settings_b.yaml --start 2025-01-01 --end 2025-12-31

  # smaller/faster run against a handful of names
  python scripts/backtest.py --config config/settings.yaml \\
      --symbols AAPL,MSFT,NVDA --start 2025-01-01 --end 2025-06-30

This tool loads the REAL `AppConfig` (src/config.py) via `load_config`, so
changing a parameter in the YAML — e.g. `risk.min_stop_atr_multiple` — *is*
the experiment. `load_config` unconditionally validates that every API key
(Alpaca, FRED, an LLM provider) is present, even though this tool calls none
of them — it only reads `config.risk` / `.trading` / `.execution`. Any of
those env vars left unset gets an obviously-fake placeholder filled in
below (never overriding a real value) so the tool runs standalone; a notice
is printed when that happens.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.backtest.data import fetch_universe_history
from src.backtest.engine import BacktestParams, BacktestRunResult, run_backtest
from src.backtest.metrics import (
    Metrics,
    compute_metrics,
    format_ab_table,
    format_caveats,
    format_metrics_report,
    write_trades_csv,
)
from src.config import AppConfig, load_config

#: This tool never calls Alpaca, FRED, or any LLM provider — it only reads
#: `AppConfig.risk` / `.trading` / `.execution`. `load_config` validates every
#: key unconditionally (src/config.py ApiKeysConfig / AppConfig), so loading
#: a real settings.yaml with unset ${ENV_VAR} substitutions fails outside an
#: environment that has live credentials configured. Filling ONLY the gaps
#: with an unmistakably-fake placeholder — never overwriting a real value —
#: lets the tool run standalone.
_PLACEHOLDER_ENV = {
    "ANTHROPIC_API_KEY": "backtest-tool-unused",
    "ALPACA_API_KEY": "backtest-tool-unused",
    "ALPACA_SECRET_KEY": "backtest-tool-unused",
    "FRED_API_KEY": "backtest-tool-unused",
    "OPENROUTER_API_KEY": "backtest-tool-unused",
    "GOOGLE_API_KEY": "backtest-tool-unused",
}


def _fill_placeholder_env() -> list[str]:
    filled = [k for k in _PLACEHOLDER_ENV if not os.environ.get(k)]
    for key in filled:
        os.environ[key] = _PLACEHOLDER_ENV[key]
    return filled


def _parse_date(text: str) -> date:
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"expected YYYY-MM-DD, got {text!r}") from exc


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--config", default="config/settings.yaml",
                     help="Settings YAML for config A (default: config/settings.yaml)")
    ap.add_argument("--config-b", default=None,
                     help="second Settings YAML — supplying this enables A/B mode")
    ap.add_argument("--start", required=True, type=_parse_date)
    ap.add_argument("--end", required=True, type=_parse_date)
    ap.add_argument("--symbols", default=None,
                     help="comma-separated override of trading.universe (default: use the config's universe)")
    ap.add_argument("--max-hold-days", type=int, default=20,
                     help="maximum holding horizon in trading sessions (default: 20)")
    ap.add_argument("--initial-equity", type=float, default=100_000.0)
    ap.add_argument("--slippage-bps", type=float, default=None,
                     help="basis points applied on both entry and exit fills "
                          "(default: the config's execution.max_entry_slippage_bps)")
    ap.add_argument("--out", default=None,
                     help="CSV path for the per-trade table (config A; "
                          "config B's table is written alongside with a _b suffix)")
    return ap


def _universe(config: AppConfig, override: str | None) -> list[str]:
    if override:
        return [s.strip().upper() for s in override.split(",") if s.strip()]
    return list(config.trading.universe)


def _run_one(
    config_path: str, args: argparse.Namespace, bars_cache: dict[tuple, tuple],
) -> tuple[AppConfig, BacktestRunResult, Metrics, float, str]:
    config = load_config(Path(config_path))
    symbols = tuple(sorted(_universe(config, args.symbols)))
    lookback_days = config.trading.lookback_days

    cache_key = (symbols, lookback_days)
    if cache_key not in bars_cache:
        print(f"Fetching history for {len(symbols)} symbol(s) "
              f"(lookback={lookback_days}d, source=yfinance via MarketDataProvider)...",
              file=sys.stderr)
        bars_cache[cache_key] = fetch_universe_history(list(symbols), lookback_days=lookback_days)
    bars_by_symbol, missing = bars_cache[cache_key]

    slippage_bps = (
        args.slippage_bps if args.slippage_bps is not None
        else config.execution.max_entry_slippage_bps
    )
    slippage_source = (
        "--slippage-bps override" if args.slippage_bps is not None
        else "config execution.max_entry_slippage_bps, reused as the flat-slippage "
             "estimate — there is no dedicated backtest slippage field in Settings"
    )

    params = BacktestParams(
        start=args.start, end=args.end, max_hold_days=args.max_hold_days,
        initial_equity=args.initial_equity, slippage_bps=slippage_bps,
    )
    result = run_backtest(config=config, bars_by_symbol=bars_by_symbol, params=params)
    metrics = compute_metrics(result.trades, params.initial_equity)
    return config, result, metrics, slippage_bps, slippage_source


def _report(label: str, config_path: str, result: BacktestRunResult, metrics: Metrics,
            args: argparse.Namespace, slippage_bps: float, slippage_source: str) -> None:
    meta = dict(
        start=args.start, end=args.end, n_symbols=len(result.symbols_used),
        data_source="yfinance (live network fetch via MarketDataProvider; no Alpaca "
                     "fallback wired, no local snapshot used)",
        initial_equity=args.initial_equity,
    )
    print(format_metrics_report(f"{label}: {config_path}", metrics, meta))
    print()
    print(format_caveats(
        slippage_bps=slippage_bps, slippage_source=slippage_source,
        skipped=result.skipped_symbol_days, min_bars=result.params.min_bars_for_signal,
        symbols_with_no_data=result.symbols_with_no_data,
    ))


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    filled = _fill_placeholder_env()
    if filled:
        print(f"(no live credentials found for {', '.join(filled)} — filled with "
              f"obviously-fake placeholders; this tool calls none of them)",
              file=sys.stderr)

    bars_cache: dict[tuple, tuple] = {}

    config_a, result_a, metrics_a, slip_a, slip_src_a = _run_one(args.config, args, bars_cache)
    print()
    _report("A", args.config, result_a, metrics_a, args, slip_a, slip_src_a)
    if args.out:
        write_trades_csv(result_a.trades, args.out)
        print(f"\nPer-trade table (A) written to {args.out}")

    if args.config_b:
        config_b, result_b, metrics_b, slip_b, slip_src_b = _run_one(args.config_b, args, bars_cache)
        print()
        _report("B", args.config_b, result_b, metrics_b, args, slip_b, slip_src_b)
        if args.out:
            out_b = str(Path(args.out).with_suffix("")) + "_b" + (Path(args.out).suffix or ".csv")
            write_trades_csv(result_b.trades, out_b)
            print(f"\nPer-trade table (B) written to {out_b}")

        print()
        print("=" * 74)
        print("A/B — did the parameter change help?")
        print("=" * 74)
        print(f"A = {args.config}")
        print(f"B = {args.config_b}")
        print()
        print(format_ab_table(
            f"A ({Path(args.config).name})", metrics_a,
            f"B ({Path(args.config_b).name})", metrics_b,
        ))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
