#!/usr/bin/env python3
"""Project QAMC's LLM spend under the accepted policy vs the baseline.

    python ops/model_policy/project_session_cost.py
    python ops/model_policy/project_session_cost.py --json

Answers the work contract's "expected cost reduction versus the all-
`gpt-5.5` baseline" with a number a reviewer can re-derive rather than
take on trust.

## Where the token counts come from, and what they are not

QAMC has never run a live trading session — timers are disabled and
`/health` reports no logged sessions — so there is no `agent_logs` history
to average. Every per-call token figure below therefore comes from one of
two places, and each row says which:

  measured   — the benchmark actually observed it (`results/*.json`,
               `input_tokens`/`output_tokens` straight off `AgentResult`)
  structural — derived from the code and config, not from a model's
               behaviour: how many calls a session makes, how many symbols
               a chunk carries, how many chunks the universe needs

The projection is deliberately arithmetic on top of those two, with no
smoothing and no allowance for prompt caching (OpenRouter discounts cached
input on several policy models; ignoring that makes BOTH sides of the
comparison conservative in the same direction).

Treat the absolute dollars as an estimate with a real error bar. Treat the
RATIO as the load-bearing result: it is dominated by published per-token
rates, which are exact, and by a token profile applied identically to both
policies.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import yaml  # noqa: E402

from src.cost_table import _PRICING_OPENROUTER  # noqa: E402

BASELINE_MODEL = "openai/gpt-5.5"
SETTINGS = PROJECT_ROOT / "config" / "settings.yaml"

# Trading days per month (NYSE averages ~21).
TRADING_DAYS_PER_MONTH = 21


class Load:
    """One agent's per-trading-day LLM load."""

    def __init__(self, agent: str, calls: float, input_tokens: int,
                 output_tokens: int, basis: str) -> None:
        self.agent = agent
        self.calls = calls
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.basis = basis

    def cost(self, model: str) -> float:
        r = _PRICING_OPENROUTER[model]
        per_call = (self.input_tokens * r["input"] + self.output_tokens * r["output"]) / 1e6
        return per_call * self.calls


# Per-call token counts are the benchmark's measured medians rounded to the
# nearest 500; call counts are structural (read off pipeline.py / settings).
#
# tech_analyst dominates and deserves its own note: the universe is 101
# symbols, `_CHUNK_SIZE = 25`, so `analyze_batch` issues 5 calls per morning
# session, each carrying 25 symbols x `_BARS_PER_SYMBOL = 20` bars plus
# indicators. Its per-call tokens are MEASURED at that real chunk size by
# the `tech_batch_full` scenario (33,328 in / 9,960 out, mean of two runs)
# rather than extrapolated from the 3-symbol case — this row is over half
# the projection, so it is the one worth measuring properly.
DAILY_LOAD = [
    Load("tech_analyst",      5.0, 33_328, 9_960,
         "measured at production chunk size (tech_batch_full); 5 chunks "
         "structural (101 symbols / _CHUNK_SIZE=25)"),
    Load("news_analyst",      1.0,  6_000, 2_500, "measured"),
    Load("macro_analyst",     1.0,  4_500, 2_500, "measured"),
    Load("earnings_analyst",  1.5,  8_000, 2_500,
         "measured (news_analyst as the shape analogue); call count structural, event-driven"),
    Load("portfolio_manager", 1.0, 20_000, 4_000, "measured"),
    Load("risk_manager",      1.0,  9_000, 3_000, "measured"),
    Load("position_reviewer", 1.0,  8_000, 3_000, "measured"),
    Load("evening_analyst",   1.0, 18_000, 5_000,
         "structural (PM as the shape analogue — both emit long multi-step reports)"),
    Load("meta_reflector",    1 / 63, 30_000, 8_000,
         "structural; quarterly (~1 run per 63 trading days)"),
]


def policy_from_settings() -> dict[str, str]:
    llm = (yaml.safe_load(SETTINGS.read_text()) or {}).get("llm") or {}
    return {load.agent: llm[f"{load.agent}_model"] for load in DAILY_LOAD}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    policy = policy_from_settings()
    rows = []
    policy_total = baseline_total = 0.0
    for load in DAILY_LOAD:
        model = policy[load.agent]
        p = load.cost(model)
        b = load.cost(BASELINE_MODEL)
        policy_total += p
        baseline_total += b
        rows.append({
            "agent": load.agent, "model": model, "calls_per_day": load.calls,
            "input_tokens": load.input_tokens, "output_tokens": load.output_tokens,
            "policy_usd_per_day": round(p, 4),
            "baseline_usd_per_day": round(b, 4),
            "reduction_pct": round((1 - p / b) * 100, 1) if b else 0.0,
            "basis": load.basis,
        })

    summary = {
        "policy_usd_per_day": round(policy_total, 4),
        "baseline_usd_per_day": round(baseline_total, 4),
        "policy_usd_per_month": round(policy_total * TRADING_DAYS_PER_MONTH, 2),
        "baseline_usd_per_month": round(baseline_total * TRADING_DAYS_PER_MONTH, 2),
        "reduction_pct": round((1 - policy_total / baseline_total) * 100, 1),
        "cheaper_by_x": round(baseline_total / policy_total, 1) if policy_total else None,
    }

    if args.json:
        print(json.dumps({"rows": rows, "summary": summary}, indent=2))
        return 0

    print(f"{'agent':<19} {'model':<32} {'baseline/day':>13} {'policy/day':>11} {'cut':>7}")
    print("-" * 87)
    for r in rows:
        print(f"{r['agent']:<19} {r['model']:<32} "
              f"${r['baseline_usd_per_day']:>12.4f} ${r['policy_usd_per_day']:>10.4f} "
              f"{r['reduction_pct']:>6.1f}%")
    print("-" * 87)
    print(f"{'TOTAL / trading day':<52} "
          f"${summary['baseline_usd_per_day']:>12.4f} "
          f"${summary['policy_usd_per_day']:>10.4f} "
          f"{summary['reduction_pct']:>6.1f}%")
    print(f"{'TOTAL / month (' + str(TRADING_DAYS_PER_MONTH) + ' trading days)':<52} "
          f"${summary['baseline_usd_per_month']:>12.2f} "
          f"${summary['policy_usd_per_month']:>10.2f}")
    print(f"\nPolicy is {summary['cheaper_by_x']}x cheaper than the "
          f"all-{BASELINE_MODEL} baseline.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
