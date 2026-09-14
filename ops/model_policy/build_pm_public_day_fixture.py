#!/usr/bin/env python3
"""Build `fixtures/pm_public_day_pm_input.json` for the `pm_public_day` exam.

Runs the REAL analyst agent classes, fresh, over the existing raw-public-fact
exam fixtures (`yf_daily_bars_2026-08-28.json`, `fred_macro_2026-09-14.json`,
`rss_feeds_2026-09-14.json`, `sec_mrvl_10q_2026-08-28.json`,
`sec_form4_2026-08-28_to_31.json`), using ONLY the free Google-direct route
with `gemini-3.5-flash-lite` (owner rule, docs/architecture — no OpenRouter
or other paid call here). Every analyst-output section it writes carries a
`kind: "fresh_analyst_output"` provenance entry
(`ops/model_policy/fixture_policy.py`) naming the model, route, timestamp and
the raw-facts source fixture it was computed from.

Account/positions cannot come from either a public source or an analyst
seat — a real account is desk state. This fixture uses a flat, clearly
labelled SYNTHETIC ACCOUNT STATE (all cash, no positions), never an invented
price or news item.

Run:
    .venv/bin/python ops/model_policy/build_pm_public_day_fixture.py

Stops and reports on the first Google call failure or quota hit — never
substitutes another route or model.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from ops.model_policy import fixture_policy as fp
from ops.model_policy.benchmark_models import (
    PLACEHOLDER_KEY,
    build_benchmark_circuit,
    load_env_if_keys_missing,
    wire_from_onecli,
)
from ops.model_policy.scenarios import (
    _EARNINGS_FIXTURE,
    _MACRO_FIXTURE,
    _NEWS_FIXTURE,
    _SMART_MONEY_FIXTURE,
    _TECH_FIXTURE,
    _public_macro_summary,
    _public_news_report,
    earnings_exam_report,
    production_max_tokens,
    smart_money_exam_observations,
    tech_exam_symbols_data,
)

MODEL = "gemini-3.5-flash-lite"
ROUTE = "google-direct"
OUT_PATH = fp.FIXTURES_DIR / "pm_public_day_pm_input.json"


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _fresh_section(value, *, model: str, route: str, timestamp: str, source_fixture: str) -> dict:
    return {"value": value, "kind": "fresh_analyst_output", "model": model,
            "route": route, "timestamp": timestamp, "source_fixture": source_fixture}


def _agent(agent_path: str, role: str, cost_circuit):
    module_name, cls_name = agent_path.split(":")
    import importlib

    cls = getattr(importlib.import_module(module_name), cls_name)
    agent = cls(api_key=PLACEHOLDER_KEY, model=MODEL, max_tokens=production_max_tokens(role),
                provider="google")
    agent.set_cost_circuit(cost_circuit)
    return agent


def build() -> dict:
    load_env_if_keys_missing()
    print(f"[{_now()}] wiring OneCLI gateway...")
    wire_from_onecli()

    from src.config import load_config

    app_config = load_config(PROJECT_ROOT / "config" / "settings.yaml")
    # 5 free-tier google-direct calls, one per analyst seat. A tiny budget is
    # enough — gemini-3.5-flash-lite is a free model, and this is not a
    # sweep. Never raised above the live daily cap (the circuit itself
    # refuses that).
    cost_circuit = build_benchmark_circuit(
        app_config, budget_usd=min(1.0, float(app_config.llm_cost_circuit.daily_cost_limit_usd)),
        planned_trials=5, run_id=f"pm-public-day-{int(datetime.now(timezone.utc).timestamp())}",
    )
    cost_circuit.require_paid_analysis("pm_public_day_build_start")

    sections: dict[str, dict] = {}
    manifest: dict = {}

    # 1. tech_analyst -> analyses
    print(f"[{_now()}] tech_analyst over {_TECH_FIXTURE} ...")
    tech_agent = _agent("src.agents.tech_analyst:TechAnalystAgent", "tech_analyst", cost_circuit)
    analyses, _ = tech_agent.analyze_batch(symbols_data=tech_exam_symbols_data())
    resolved = {s: a for s, a in (analyses or {}).items() if a is not None}
    if not resolved:
        raise RuntimeError("tech_analyst returned no usable analyses — stopping, not substituting")
    ts = _now()
    manifest["analyses"] = [a.model_dump(mode="json") for a in resolved.values()]
    sections["analyses"] = {"kind": "fresh_analyst_output", "model": MODEL, "route": ROUTE,
                             "timestamp": ts, "source_fixture": _TECH_FIXTURE}
    print(f"    -> {len(resolved)} symbols resolved")

    # 2. macro_analyst -> macro_analysis
    print(f"[{_now()}] macro_analyst over {_MACRO_FIXTURE} ...")
    macro_agent = _agent("src.agents.macro_analyst:MacroAnalystAgent", "macro_analyst", cost_circuit)
    macro_analysis, _ = macro_agent.analyze(
        macro_summary=_public_macro_summary(),
        universe=["SPY", "QQQ", "XLE", "XLU", "XLP", "XLF", "SMH", "AAPL", "NVDA"],
        last_state=None, news_narrative=None,
    )
    if macro_analysis is None:
        raise RuntimeError("macro_analyst returned no analysis — stopping, not substituting")
    ts = _now()
    manifest["macro_analysis"] = macro_analysis.model_dump(mode="json")
    sections["macro_analysis"] = {"kind": "fresh_analyst_output", "model": MODEL, "route": ROUTE,
                                   "timestamp": ts, "source_fixture": _MACRO_FIXTURE}

    # 3. news_analyst -> news_intel
    print(f"[{_now()}] news_analyst over {_NEWS_FIXTURE} ...")
    news_universe = [
        "SPY", "QQQ", "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "TSLA", "META",
        "AMD", "MU", "AVGO", "XLE", "XLF", "XLU", "XLK",
    ]
    news_text, stock_mentions, coverage = _public_news_report()
    news_agent = _agent("src.agents.news_analyst:NewsAnalystAgent", "news_analyst", cost_circuit)
    news_report, _ = news_agent.analyze(
        news_text=news_text, universe=news_universe, stock_mentions=stock_mentions,
        previous_narrative=None, session="morning", prior_session_report=None,
        news_coverage=coverage,
    )
    if news_report is None:
        raise RuntimeError("news_analyst returned no report — stopping, not substituting")
    ts = _now()
    manifest["news_intel"] = news_report.model_dump(mode="json")
    sections["news_intel"] = {"kind": "fresh_analyst_output", "model": MODEL, "route": ROUTE,
                               "timestamp": ts, "source_fixture": _NEWS_FIXTURE}

    # 4. earnings_analyst -> earnings_analyses
    print(f"[{_now()}] earnings_analyst over {_EARNINGS_FIXTURE} ...")
    earnings_agent = _agent("src.agents.earnings_analyst:EarningsAnalystAgent", "earnings_analyst", cost_circuit)
    report = earnings_exam_report()
    results = earnings_agent.analyze_reports([report])
    analysis = results[0]["analysis"] if results else None
    if analysis is None:
        raise RuntimeError("earnings_analyst returned no analysis — stopping, not substituting")
    ts = _now()
    manifest["earnings_analyses"] = [analysis]
    sections["earnings_analyses"] = {"kind": "fresh_analyst_output", "model": MODEL, "route": ROUTE,
                                      "timestamp": ts, "source_fixture": _EARNINGS_FIXTURE}

    # 5. smart_money_analyst -> smart_money_findings
    print(f"[{_now()}] smart_money_analyst over {_SMART_MONEY_FIXTURE} ...")
    import tempfile

    sm_agent = _agent("src.agents.smart_money_analyst:SmartMoneyAnalystAgent", "smart_money_analyst", cost_circuit)
    sm_agent.synthesis_cache_path = (
        Path(tempfile.mkdtemp(prefix="pm-public-day-synthesis-")) / "synthesis_cache.json"
    )
    observations = smart_money_exam_observations()
    findings, _result, error = sm_agent.analyze(observations)
    if error:
        raise RuntimeError(f"smart_money_analyst error={error} — stopping, not substituting")
    ts = _now()
    manifest["smart_money_findings"] = [f.model_dump(mode="json") for f in (findings or [])]
    sections["smart_money_findings"] = {"kind": "fresh_analyst_output", "model": MODEL,
                                         "route": ROUTE, "timestamp": ts,
                                         "source_fixture": _SMART_MONEY_FIXTURE}
    print(f"    -> {len(findings or [])} findings")

    # 6. Synthetic account state — explicitly labelled, not desk data.
    manifest["account_state"] = {
        "label": "SYNTHETIC ACCOUNT STATE — not a recorded desk balance",
        "cash_balance": 100000.0,
        "reserve_balance": 0.0,
        "total_value": 100000.0,
        "positions": [],
        "allow_margin": False,
        "session_type": "morning",
    }
    sections["account_state"] = {
        "kind": "synthetic_account_state",
        "label": "SYNTHETIC ACCOUNT STATE — flat $100,000 cash, no positions; not a desk recording",
    }

    manifest["_provenance"] = {
        "note": (
            "pm_public_day exam fixture. analyses/macro_analysis/news_intel/"
            "earnings_analyses/smart_money_findings are FRESH analyst-seat output "
            "(gemini-3.5-flash-lite, google-direct route) computed at build time "
            "over the existing raw-public-fact exam fixtures named per section. "
            "account_state is a labelled synthetic starting state, not a desk "
            "recording. No desk data anywhere in this file."
        ),
        "sections": sections,
    }
    return manifest


def main() -> int:
    manifest = build()
    OUT_PATH.write_text(json.dumps(manifest, indent=2, sort_keys=True))
    verdict = fp.check_fixture(OUT_PATH)
    print(f"[{_now()}] wrote {OUT_PATH}")
    if not verdict.admissible:
        print("QUARANTINED:")
        for p in verdict.problems:
            print(f"  - {p}")
        return 1
    print("fixture_policy: admissible")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
