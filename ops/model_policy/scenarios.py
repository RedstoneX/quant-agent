"""Fixed benchmark scenarios for QAMC model selection.

Each scenario drives a REAL agent class (`src/agents/*`) with the REAL
system prompt (`config/prompts/*.md`) over a frozen, synthetic input, and
grades the result with deterministic Python assertions.

Why real agents rather than a standalone prompt harness: the thing we are
choosing a model for is not "can it write JSON" — it is "does it survive
`analyze_batch` / `decide` / `review`, including `parse_json()`'s candidate
scan, the per-entry isolation dropping malformed rows, and every Pydantic
validator in `src/models.py`". A model that scores well on a paraphrase of
the prompt but trips `_validate_rating_price_consistency` is not usable, and
only the real call path can tell us that.

Why synthetic (not recorded market) inputs: the grading has to be
deterministic and re-runnable by a reviewer months later, so every scenario
is constructed so that the *correct* answer is forced by arithmetic, not by
market opinion. `tech_uptrend` has an unambiguous uptrend; `risk_rr_breach`
contains a BUY whose reward/risk is 0.41 against a documented 1.5 floor.
Grading never asks "did the model agree with me about the market" — only
"did it apply the rule the prompt states".

No secrets live here: scenarios are prices and tickers.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Callable

from src.models import (
    OHLCV,
    Position,
    PortfolioDecision,
    ReasoningChain,
    TechAnalysisResult,
    TechnicalIndicators,
    TradeDecision,
)
from src.risk.rules import RiskViolation


# --------------------------------------------------------------------------
# Grading primitives
# --------------------------------------------------------------------------


@dataclass
class Check:
    """One graded assertion. `weight` is the share of the scenario's score."""

    name: str
    weight: float
    passed: bool
    detail: str = ""


def production_max_tokens(agent: str) -> int:
    """The `max_tokens` this agent actually runs with, read from settings.yaml.

    Hardcoding a benchmark-local ceiling here was a real bug, not a
    simplification. The first sweep capped the risk-manager scenario at
    16,000 while production allows 128,000, and two candidates were scored
    0.00 for "unparseable RiskVerdict" when their reasoning was in fact
    correct — they had simply spent the smaller budget on reasoning tokens
    and been cut off mid-JSON. `deepseek-v4-flash-0731` reported output
    tokens of exactly 16,000, which is the cap, not a coincidence.

    Reading the real value keeps the harness honest and self-maintaining: a
    model is now judged under the conditions it would actually run in, and
    retuning an agent's budget in settings.yaml retunes the benchmark too.
    """
    import yaml

    settings = Path(__file__).resolve().parents[2] / "config" / "settings.yaml"
    llm = (yaml.safe_load(settings.read_text()) or {}).get("llm") or {}
    value = llm.get(f"{agent}_max_tokens")
    if not isinstance(value, int) or value <= 0:
        value = llm.get("max_tokens")
    if not isinstance(value, int) or value <= 0:
        raise RuntimeError(f"no usable max_tokens for {agent} in {settings}")
    return value


@dataclass
class Scenario:
    """A benchmark case: build an agent, run it, grade the output.

    `agent_cls` is instantiated as `agent_cls(api_key, model, max_tokens,
    provider="openrouter")` by the runner — the same constructor the pipeline
    uses — and `invoke` calls the agent's real public entry point.
    """

    key: str
    role: str                      # which config/settings.yaml agent this informs
    agent_path: str                # "module:ClassName"
    invoke: Callable[[Any], Any]   # (agent) -> parsed output (or None)
    grade: Callable[[Any], list[Check]]
    description: str
    # Excluded from the default sweep. Used for the production-scale tech
    # batch, which is far too expensive to run against every candidate but
    # is the decisive latency measurement for the finalists.
    default: bool = True

    @property
    def max_tokens(self) -> int:
        return production_max_tokens(self.role)


def _bars(
    symbol: str,
    start: float,
    step: float,
    n: int = 60,
    noise: tuple[float, ...] = (0.0, 0.4, -0.3, 0.2, -0.1),
) -> list[OHLCV]:
    """Deterministic bar series: linear drift + a fixed repeating wiggle.

    Fixed (not random) so two runs of the benchmark see byte-identical input
    and any score difference is attributable to the model alone.
    """
    out: list[OHLCV] = []
    day = date(2026, 5, 4)
    for i in range(n):
        close = start + step * i + noise[i % len(noise)]
        out.append(
            OHLCV(
                date=day + timedelta(days=i),
                open=round(close - 0.3, 2),
                high=round(close + 0.8, 2),
                low=round(close - 0.9, 2),
                close=round(close, 2),
                volume=40_000_000 + (i % 7) * 1_500_000,
            )
        )
    return out


# --------------------------------------------------------------------------
# 1. tech_analyst — the highest-volume specialist call in the system
# --------------------------------------------------------------------------

_TECH_SYMBOLS = {
    # symbol: (indicators, bars) — one clean uptrend, one clean downtrend,
    # one genuinely rangebound name where "neutral" is the honest answer.
    "AAPL": (
        TechnicalIndicators(
            symbol="AAPL", ma_20=196.4, ma_50=188.1, ma_200=175.6, rsi_14=64.2,
            macd=2.81, macd_signal=1.94, macd_hist=0.87,
            bb_upper=204.2, bb_middle=196.4, bb_lower=188.6,
            atr_14=3.45, volume_change_pct=12.4,
        ),
        _bars("AAPL", 170.0, 0.55),
    ),
    "XLE": (
        TechnicalIndicators(
            symbol="XLE", ma_20=82.1, ma_50=86.9, ma_200=91.4, rsi_14=31.8,
            macd=-1.42, macd_signal=-0.88, macd_hist=-0.54,
            bb_upper=87.0, bb_middle=82.1, bb_lower=77.2,
            atr_14=1.62, volume_change_pct=28.9,
        ),
        _bars("XLE", 96.0, -0.28),
    ),
    "XLU": (
        TechnicalIndicators(
            symbol="XLU", ma_20=74.8, ma_50=74.6, ma_200=74.1, rsi_14=50.6,
            macd=0.04, macd_signal=0.06, macd_hist=-0.02,
            bb_upper=76.3, bb_middle=74.8, bb_lower=73.3,
            atr_14=0.71, volume_change_pct=-3.1,
        ),
        _bars("XLU", 74.5, 0.01),
    ),
}


def _tech_invoke(agent):
    symbols_data = [
        {"symbol": s, "bars": bars, "indicators": ind}
        for s, (ind, bars) in _TECH_SYMBOLS.items()
    ]
    analyses, _ = agent.analyze_batch(symbols_data=symbols_data)
    return analyses


def _tech_grade(analyses: dict[str, TechAnalysisResult] | None) -> list[Check]:
    checks: list[Check] = []
    analyses = analyses or {}

    checks.append(Check(
        "all_symbols_returned", 0.30,
        set(analyses) == set(_TECH_SYMBOLS),
        f"got {sorted(analyses)} want {sorted(_TECH_SYMBOLS)}",
    ))

    # Directional agreement. Not a market opinion: AAPL is above a rising
    # 20/50/200 stack with MACD positive, XLE is below a falling stack with
    # MACD negative. A model that calls AAPL bearish here has misread the
    # numbers it was handed.
    aapl = analyses.get("AAPL")
    checks.append(Check(
        "uptrend_not_bearish", 0.15,
        aapl is not None and aapl.rating in ("strong_buy", "buy", "neutral"),
        f"AAPL rating={getattr(aapl, 'rating', None)}",
    ))
    xle = analyses.get("XLE")
    checks.append(Check(
        "downtrend_not_bullish", 0.15,
        xle is not None and xle.rating in ("strong_sell", "sell", "neutral"),
        f"XLE rating={getattr(xle, 'rating', None)}",
    ))

    # ATR-based stop discipline is an explicit instruction in
    # config/prompts/tech_analyst.md. Grade it where it is checkable: an
    # actionable rating must place its stop a sane multiple of ATR away —
    # not 0.2 ATR (noise-stopped instantly) and not 12 ATR (no protection).
    atr_ok, atr_detail = True, []
    for sym, res in analyses.items():
        if res.rating == "neutral" or res.entry_price is None or res.stop_loss is None:
            continue
        atr = _TECH_SYMBOLS[sym][0].atr_14 or 0.0
        if atr <= 0:
            continue
        mult = abs(res.entry_price - res.stop_loss) / atr
        if not (0.8 <= mult <= 8.0):
            atr_ok = False
            atr_detail.append(f"{sym} stop={mult:.1f}xATR")
    checks.append(Check(
        "atr_stop_discipline", 0.20, atr_ok, "; ".join(atr_detail) or "ok",
    ))

    # thesis_invalid_if is what lets PM/midday exit before the broker stop.
    # config/prompts/tech_analyst.md asks for it on actionable ratings and
    # EMPTY on neutral, so both halves are graded: a model that fills it on
    # a neutral call has not read the instruction either.
    wrong: list[str] = []
    for sym, res in analyses.items():
        filled = bool((res.thesis_invalid_if or "").strip())
        if res.rating == "neutral" and filled:
            wrong.append(f"{sym} neutral-but-filled")
        elif res.rating != "neutral" and not filled:
            wrong.append(f"{sym} actionable-but-empty")
    checks.append(Check(
        "thesis_invalid_if_discipline", 0.20,
        not wrong and bool(analyses),
        "; ".join(wrong) or "ok",
    ))
    return checks


# --------------------------------------------------------------------------
# 2. macro_analyst — light input, structured regime output
# --------------------------------------------------------------------------

_MACRO_SUMMARY = {
    "vix": {"current": 27.4, "change_pct": 18.2, "percentile_1y": 88},
    "treasury": {"ten_year": 4.62, "two_year": 4.41, "spread": 0.21, "inverted": False},
    "fed_funds_rate": {"current": 4.25, "trend": "holding"},
    "inflation": {"core_cpi_yoy": 3.4, "headline_cpi_yoy": 3.1, "trend": "sticky"},
    "unemployment": {"current": 4.4, "trend": "rising"},
    "credit_spread": {"current_bps": 412, "change_bps": 63, "trend": "widening"},
    "consumer_sentiment": {"current": 58.2, "trend": "falling"},
}

_MACRO_UNIVERSE = ["SPY", "QQQ", "XLE", "XLU", "XLP", "XLF", "SMH", "AAPL", "NVDA"]


def _macro_invoke(agent):
    analysis, _ = agent.analyze(
        macro_summary=_MACRO_SUMMARY, universe=_MACRO_UNIVERSE,
    )
    return analysis


def _macro_grade(analysis) -> list[Check]:
    checks: list[Check] = []
    checks.append(Check("parsed", 0.35, analysis is not None, "MacroAnalysis validated"))
    if analysis is None:
        return checks

    # VIX at the 88th percentile, credit spreads widening 63bps, sentiment
    # falling, unemployment rising. "risk_on with 95% invested" is not a
    # defensible reading of that input — grade the direction, not a number.
    pos = getattr(analysis, "position_guidance", None)
    invested = getattr(pos, "target_invested_pct", None)
    checks.append(Check(
        "defensive_positioning", 0.35,
        invested is not None and invested <= 80.0,
        f"target_invested_pct={invested} (stress input; expected <= 80)",
    ))

    guidance = getattr(analysis, "sector_guidance", None) or []
    checks.append(Check(
        "sector_guidance_present", 0.15,
        len(guidance) >= 2,
        f"{len(guidance)} sector calls",
    ))

    chain = getattr(analysis, "reasoning_chain", None)
    steps = [] if chain is None else [
        v for v in chain.model_dump().values() if isinstance(v, str)
    ]
    checks.append(Check(
        "cot_complete", 0.15,
        bool(steps) and all(s.strip() for s in steps),
        f"{sum(1 for s in steps if s.strip())}/{len(steps)} steps",
    ))
    return checks


# --------------------------------------------------------------------------
# 3. portfolio_manager — the synthesis step
# --------------------------------------------------------------------------

_PM_ANALYSES = [
    TechAnalysisResult(
        symbol="AAPL", rating="buy", conviction="high",
        entry_price=198.5, reference_target=214.0, stop_loss=191.5,
        reasoning="Uptrend intact above rising 20/50/200 stack.",
        reasoning_chain={
            "trend": "Above all MAs, 50 crossed 200 in March.",
            "momentum": "RSI 64, MACD histogram expanding.",
            "volatility": "ATR 3.45, price riding the upper band without piercing it.",
            "volume": "Volume +12% on advance days.",
            "support_resistance": "Support 191.5 at prior breakout shelf.",
        },
        thesis_invalid_if="Daily close below 191.5.",
        atr_14=3.45,
    ),
    TechAnalysisResult(
        symbol="XLE", rating="sell", conviction="medium",
        entry_price=81.2, reference_target=76.0, stop_loss=84.6,
        reasoning="Downtrend below falling MA stack.",
        reasoning_chain={
            "trend": "Below 20/50/200, all declining.",
            "momentum": "RSI 32, MACD below signal.",
            "volatility": "ATR 1.62, closes pinned to the lower band.",
            "volume": "Distribution volume rising.",
            "support_resistance": "Next support 76.0.",
        },
        thesis_invalid_if="Reclaims 84.6 on a daily close.",
        atr_14=1.62,
    ),
]

_PM_POSITIONS = [
    Position(symbol="NVDA", qty=120, avg_entry=142.0, current_price=151.3,
             market_value=18_156.0, unrealized_pnl=1_116.0, sector="Technology"),
    Position(symbol="XLU", qty=90, avg_entry=73.9, current_price=74.8,
             market_value=6_732.0, unrealized_pnl=81.0, sector="Utilities"),
]

# Macro says be defensive. Cash is thin. Margin is OFF. Together these force
# a checkable answer: the book cannot grow gross exposure here.
_PM_MACRO = {
    "regime": "risk_off",
    "equity_outlook": "bearish",
    "position_guidance": {
        "target_invested_pct": 55.0,
        "cash_recommendation_pct": 45.0,
        "reasoning": "VIX 88th percentile, credit spreads widening.",
    },
    "sector_guidance": [
        {"sector": "Energy", "stance": "underweight", "reason": "Crude breaking down."},
        {"sector": "Utilities", "stance": "overweight", "reason": "Defensive bid."},
    ],
    "reasoning_chain": {
        "rates_path": "Fed on hold, cuts priced out.",
        "growth_signal": "Unemployment rising, sentiment falling.",
        "inflation_read": "Core CPI sticky at 3.4%.",
        "risk_appetite": "Credit spreads +63bps.",
        "cross_asset": "Bid for duration and defensives.",
        "positioning": "Reduce gross, favour defensives.",
    },
}

_PM_TOTAL_VALUE = 42_000.0
_PM_CASH = 2_400.0  # ~5.7% cash, margin disabled


def _pm_invoke(agent):
    decision, _ = agent.decide(
        analyses=_PM_ANALYSES,
        positions=_PM_POSITIONS,
        macro_analysis=_PM_MACRO,
        cash_balance=_PM_CASH,
        total_value=_PM_TOTAL_VALUE,
        allow_margin=False,
    )
    return decision


def _pm_grade(decision: PortfolioDecision | None) -> list[Check]:
    checks: list[Check] = []
    checks.append(Check("parsed", 0.30, decision is not None, "PortfolioDecision validated"))
    if decision is None:
        return checks

    chain = decision.reasoning_chain.model_dump()
    required = [k for k in chain if k not in ("continuity_check", "premortem_check")]
    checks.append(Check(
        "cot_complete", 0.15,
        all(str(chain[k]).strip() for k in required),
        f"{sum(1 for k in required if str(chain[k]).strip())}/{len(required)} steps",
    ))

    targets = decision.targets
    # Hard constraint from config/settings.yaml: allow_margin=false and only
    # 5.7% cash. Every new/added weight has to be funded by trimming, so the
    # total ADDED weight cannot exceed available cash. This is arithmetic the
    # prompt states, not a market view.
    held = {"NVDA": 18_156.0 / _PM_TOTAL_VALUE * 100, "XLU": 6_732.0 / _PM_TOTAL_VALUE * 100}
    added = sum(
        max(0.0, t.target_weight_pct - held.get(t.symbol, 0.0)) for t in targets
    )
    freed = sum(
        max(0.0, held.get(t.symbol, 0.0) - t.target_weight_pct) for t in targets
    )
    cash_pct = _PM_CASH / _PM_TOTAL_VALUE * 100
    checks.append(Check(
        "respects_cash_no_margin", 0.30,
        added <= freed + cash_pct + 1.0,  # 1pt tolerance for rounding
        f"added={added:.1f}% vs funded={freed + cash_pct:.1f}% "
        f"(cash {cash_pct:.1f}% + trims {freed:.1f}%)",
    ))

    # Single-name cap is a schema bound (TargetPosition le=20), so a breach
    # is dropped by _drop_invalid_targets rather than raised. Check it here
    # so "silently lost a target" cannot read as a clean pass.
    checks.append(Check(
        "targets_emitted", 0.05, bool(targets), f"{len(targets)} targets",
    ))

    # Macro says underweight Energy; TA says short XLE. A PM that opens a
    # long XLE target has contradicted both inputs without a stated catalyst.
    xle = next((t for t in targets if t.symbol == "XLE"), None)
    checks.append(Check(
        "no_signal_contradiction", 0.10,
        xle is None or xle.target_weight_pct == 0 or bool(xle.catalyst.strip()),
        f"XLE target={getattr(xle, 'target_weight_pct', None)}",
    ))

    # The book is ~59% invested against a macro target of 55%, in a
    # documented risk-off regime, with the only actionable long being a
    # high-conviction AAPL buy. A PM that emits a single token target and
    # nothing else has not actually managed the book: it must either act on
    # the AAPL signal or say in portfolio_view why it is standing down.
    named = {t.symbol for t in targets}
    view = (decision.portfolio_view or "").upper()
    checks.append(Check(
        "acts_on_the_actionable_signal", 0.10,
        "AAPL" in named or "AAPL" in view,
        f"targets={sorted(named)} portfolio_view mentions AAPL={'AAPL' in view}",
    ))

    # Every target must carry a thesis — PortfolioConstructor and the
    # journal both render it, and an empty one is an unauditable order.
    checks.append(Check(
        "targets_have_thesis", 0.10,
        all((t.thesis or "").strip() for t in targets) and bool(targets),
        f"{sum(1 for t in targets if (t.thesis or '').strip())}/{len(targets)} with thesis",
    ))
    return checks


# Production-sized PM regression derived from the observed 11/17-target
# failures: enough candidates, holdings and memory text to exercise the real
# prompt shape rather than a toy two-symbol schema check.
_PM_PRODUCTION_SYMBOLS = [
    "SPY", "QQQ", "IWM", "AAPL", "MSFT", "GOOGL", "AMZN", "NVDA",
    "META", "AVGO", "AMD", "ORCL", "MU", "JPM", "GS", "V", "MA",
    "UNH", "LLY", "XOM", "CVX", "COST", "WMT", "CAT", "GE", "BA",
    "NEE", "VST", "CEG", "BRK-B",
]
_PM_PRODUCTION_ANALYSES = [
    TechAnalysisResult(
        symbol=symbol, rating="buy", conviction="medium",
        entry_price=100.0, stop_loss=94.0, reference_target=112.0,
        reasoning="Validated uptrend with positive momentum and volume.",
        reasoning_chain={
            "trend": "Above rising 20/50-day averages.",
            "momentum": "RSI and MACD positive.",
            "volatility": "ATR supports a bounded stop.",
            "volume": "Accumulation on advance days.",
            "support_resistance": "Support at 94, target at 112.",
        },
    )
    for symbol in _PM_PRODUCTION_SYMBOLS
]
_PM_PRODUCTION_POSITIONS = [
    Position(
        symbol=symbol, qty=10, avg_entry=90.0, current_price=100.0,
        market_value=1_000.0, unrealized_pnl=100.0, sector="Diversified",
    )
    for symbol in _PM_PRODUCTION_SYMBOLS[:15]
]


def _pm_production_invoke(agent):
    decision, _ = agent.decide(
        analyses=_PM_PRODUCTION_ANALYSES,
        positions=_PM_PRODUCTION_POSITIONS,
        macro_analysis={"regime": "risk_on", "equity_outlook": "bullish"},
        cash_balance=45_000.0, total_value=100_000.0, allow_margin=False,
        weekly_narrative="Seven-day portfolio narrative. " * 80,
        macro_trajectory="Regime trajectory evidence. " * 80,
        active_state_changes="Current state change. " * 80,
        pm_recent_decisions="Prior grounded target. " * 80,
        rm_recent_verdicts="Prior risk verdict. " * 80,
    )
    return decision


def _pm_production_grade(decision: PortfolioDecision | None) -> list[Check]:
    checks = [Check("parsed_and_grounded", 0.55, decision is not None,
                    "PortfolioDecision passed live grounding validation")]
    if decision is None:
        return checks
    held = {p.symbol for p in _PM_PRODUCTION_POSITIONS}
    phantom_exits = [
        target.symbol for target in decision.targets
        if target.target_weight_pct == 0 and target.symbol not in held
    ]
    checks.append(Check(
        "no_phantom_exits", 0.20, not phantom_exits,
        f"phantom exits={phantom_exits}",
    ))
    checks.append(Check(
        "actionable_book", 0.15, len(decision.targets) >= 3,
        f"{len(decision.targets)} grounded targets",
    ))
    checks.append(Check(
        "provenance_present", 0.10,
        bool(decision.targets) and all(target.provenance for target in decision.targets),
        f"{sum(bool(t.provenance) for t in decision.targets)}/{len(decision.targets)} targets",
    ))
    return checks


# --------------------------------------------------------------------------
# 4. risk_manager — the last LLM gate before deterministic Python
# --------------------------------------------------------------------------


def _pm_reasoning_chain() -> ReasoningChain:
    return ReasoningChain(
        macro_filter="Risk-off but selective longs still warranted.",
        news_check="No single-name headline risk found.",
        earnings_check="No earnings inside 3 sessions.",
        signal_conflicts="TA and macro broadly aligned.",
        sizing_logic="Sized to conviction.",
        portfolio_balance="Tech-heavy but within cap.",
        cash_target="Holding 12% cash.",
    )


# The BUY below is deliberately indefensible on the arithmetic the RM prompt
# tells it to audit:
#   MU:  entry 118.0, stop 112.0 (risk 6.0), target 120.5 (reward 2.5) => 0.42R
#        against the prompt's >= 1.5 floor, with no catalyst field set, AND
#        sized at 18% of the book — the largest line in the plan.
# A competent risk manager must not approve this untouched. Everything else
# in the plan is clean, so a model that rejects the whole plan for the wrong
# reason still has to name MU in its chain to score the rr_audit check.
_RISK_DECISION = PortfolioDecision(
    reasoning_chain=_pm_reasoning_chain(),
    decisions=[
        TradeDecision(
            action="BUY", symbol="MU", allocation_pct=18.0,
            entry_price=118.0, stop_loss=112.0, take_profit=120.5,
            reasoning="Memory cycle turning; adding aggressively.",
        ),
        TradeDecision(
            action="BUY", symbol="XLU", allocation_pct=5.0,
            entry_price=74.8, stop_loss=72.4, take_profit=80.2,
            reasoning="Defensive ballast, 2.25R.",
        ),
        TradeDecision(
            action="SELL", symbol="XLE", allocation_pct=100.0,
            entry_price=81.2, stop_loss=84.6, take_profit=76.0,
            reasoning="Close the energy position entirely.",
        ),
    ],
    portfolio_view="Rotating from energy into memory and defensives.",
)

_RISK_POSITIONS = [
    Position(symbol="NVDA", qty=120, avg_entry=142.0, current_price=151.3,
             market_value=18_156.0, unrealized_pnl=1_116.0, sector="Technology"),
    Position(symbol="XLE", qty=95, avg_entry=88.4, current_price=81.2,
             market_value=7_714.0, unrealized_pnl=-684.0, sector="Energy"),
]


# Holding ages and system-performance state. RiskStage always passes both
# (rebuilding them on the resume lane), so a scenario that omitted them would
# be measuring the seat in a configuration production never runs. Neither
# position is inside the <5d protection period and `in_drawdown` is false, so
# the arithmetic this scenario grades is unchanged — the extra evidence is
# present and simply gives nothing away.
_RISK_POSITION_HISTORY = {
    "NVDA": {"days_held": 30, "entry_date": "2026-07-13"},
    "XLE": {"days_held": 46, "entry_date": "2026-06-27"},
}
_RISK_RECENT_PERFORMANCE = {
    "rolling_5d_pct": -0.8,
    "rolling_20d_pct": 1.4,
    "in_drawdown": False,
    "trailing_days": 24,
}


def _risk_invoke(agent):
    verdict, _ = agent.review(
        portfolio_decision=_RISK_DECISION,
        positions=_RISK_POSITIONS,
        macro_summary=_MACRO_SUMMARY,
        rule_violations=[],
        total_value=42_000.0,
        cash=5_040.0,
        position_history=_RISK_POSITION_HISTORY,
        recent_performance=_RISK_RECENT_PERFORMANCE,
    )
    return verdict


def _risk_grade(verdict) -> list[Check]:
    checks: list[Check] = []
    checks.append(Check("parsed", 0.25, verdict is not None, "RiskVerdict validated"))
    if verdict is None:
        return checks

    chain = verdict.reasoning_chain.model_dump()
    checks.append(Check(
        "cot_complete", 0.10,
        all(str(v).strip() for v in chain.values()),
        f"{sum(1 for v in chain.values() if str(v).strip())}/{len(chain)} steps",
    ))

    # THE discriminating check: did it act on the 0.42R, 18%-of-book BUY?
    # Acting means any of: not approving, modifying MU, or scaling buys down.
    mu_mods = [m for m in verdict.modifications if m.symbol.upper() == "MU"]
    acted = (
        (not verdict.approved)
        or bool(mu_mods)
        or verdict.scale_all_buys < 1.0
    )
    checks.append(Check(
        "catches_rr_breach", 0.40, acted,
        f"approved={verdict.approved} mu_mods={len(mu_mods)} "
        f"scale_all_buys={verdict.scale_all_buys}",
    ))

    # And did it say WHY in the audit trail — the rr_audit step must actually
    # mention the offending symbol, not just assert "all fine". Word-boundary
    # match: a substring test would score "must"/"multiple"/"cumulative" as a
    # hit and hand every model a free point.
    rr_text = str(chain.get("rr_audit", ""))
    checks.append(Check(
        "names_offender_in_rr_audit", 0.15,
        re.search(r"\bMU\b", rr_text) is not None,
        f"rr_audit={rr_text[:110]!r}",
    ))

    checks.append(Check(
        "reason_category_not_clean", 0.10,
        verdict.reason_category != "clean" or verdict.approved is False,
        f"reason_category={verdict.reason_category}",
    ))
    return checks


# --------------------------------------------------------------------------
# 4b. risk_manager — the two rules the 2026-08-13 audit gave it the evidence
#     for. Separate scenario, not extra checks on `risk_rr_breach`: that one
#     grades the R/R gate and mixing a second rule into it would make a
#     failure unattributable.
#
#     Both breaches below are forced by arithmetic the prompts already state,
#     the same discipline as every other scenario here:
#
#       - `in_drawdown=true` (5d -4.6% is past the -3% trigger) requires PM
#         to halve every new BUY. PM sized MSFT at the full 12% high-
#         conviction base and SAID SO in `sizing_logic`. Nothing
#         deterministic enforces this rule, so RM is the only check on it.
#       - AMD is 2 days held, inside the <5d protection period, and the SELL
#         cites a Tech downgrade — which portfolio_manager.md names
#         explicitly as NOT sufficient.
#
#     MSFT's R/R is 2.5 (entry 100 / stop 94 / target 115), comfortably above
#     the 1.5 floor and below the 3.0 "don't nick it" band, so a model cannot
#     score here by re-running the R/R check from `risk_rr_breach`.
# --------------------------------------------------------------------------

_DRAWDOWN_PM_CHAIN = ReasoningChain(
    macro_filter="Regime risk-on, target invested 75%.",
    news_check="No HIGH state changes on held or proposed names.",
    earnings_check="Nothing queued inside the window.",
    signal_conflicts="Tech and macro aligned on MSFT.",
    sizing_logic=(
        "MSFT at the full 12% high-conviction base — 4/4 alignment and "
        "R/R 2.5, so no reduction applied."
    ),
    portfolio_balance="Technology 34%, inside the 40% cap.",
    cash_target="Cash 14% after the adds, inside the risk-on band.",
    continuity_check="Consistent with the week's constructive stance.",
    premortem_check=(
        "Bear case on MSFT: the AI capex trade is crowded and a soft "
        "Azure print would take the multiple down. Falsifier: a close "
        "below the 20-day on rising volume."
    ),
)

_DRAWDOWN_DECISION = PortfolioDecision(
    reasoning_chain=_DRAWDOWN_PM_CHAIN,
    decisions=[
        TradeDecision(
            action="BUY", symbol="MSFT", allocation_pct=12.0,
            entry_price=100.0, stop_loss=94.0, take_profit=115.0,
            reasoning="High conviction, 4/4 aligned, R/R 2.5.",
        ),
        TradeDecision(
            action="SELL", symbol="AMD", allocation_pct=100.0,
            entry_price=162.0, stop_loss=155.0, take_profit=180.0,
            reasoning="Tech rating downgraded to neutral today.",
        ),
    ],
    portfolio_view="Adding quality tech, cutting the weak AMD entry.",
)

_DRAWDOWN_POSITIONS = [
    Position(symbol="NVDA", qty=120, avg_entry=142.0, current_price=151.3,
             market_value=18_156.0, unrealized_pnl=1_116.0, sector="Technology"),
    Position(symbol="AMD", qty=40, avg_entry=162.0, current_price=159.4,
             market_value=6_376.0, unrealized_pnl=-104.0, sector="Technology"),
]

_DRAWDOWN_POSITION_HISTORY = {
    "NVDA": {"days_held": 30, "entry_date": "2026-07-13"},
    "AMD": {"days_held": 2, "entry_date": "2026-08-10"},
}

_DRAWDOWN_RECENT_PERFORMANCE = {
    "rolling_5d_pct": -4.6,     # past the -3% trigger
    "rolling_20d_pct": -2.1,
    "in_drawdown": True,
    "trailing_days": 24,
}


def _risk_drawdown_invoke(agent):
    verdict, _ = agent.review(
        portfolio_decision=_DRAWDOWN_DECISION,
        positions=_DRAWDOWN_POSITIONS,
        macro_summary=_MACRO_SUMMARY,
        rule_violations=[],
        total_value=42_000.0,
        cash=8_400.0,
        position_history=_DRAWDOWN_POSITION_HISTORY,
        recent_performance=_DRAWDOWN_RECENT_PERFORMANCE,
    )
    return verdict


def _risk_drawdown_grade(verdict) -> list[Check]:
    checks: list[Check] = []
    checks.append(Check("parsed", 0.20, verdict is not None, "RiskVerdict validated"))
    if verdict is None:
        return checks

    chain = verdict.reasoning_chain.model_dump()
    checks.append(Check(
        "cot_complete", 0.10,
        all(str(v).strip() for v in chain.values()),
        f"{sum(1 for v in chain.values() if str(v).strip())}/{len(chain)} steps",
    ))

    # Did it ACT on a BUY that ignored the halving requirement? Same shape as
    # the rr_breach check: a mod on the name, a portfolio-wide scale-down, or
    # a rejection all count.
    msft_mods = [m for m in verdict.modifications if m.symbol.upper() == "MSFT"]
    acted = (
        (not verdict.approved)
        or bool(msft_mods)
        or verdict.scale_all_buys < 1.0
    )
    checks.append(Check(
        "acts_on_unhalved_drawdown_buy", 0.30, acted,
        f"approved={verdict.approved} msft_mods={len(msft_mods)} "
        f"scale_all_buys={verdict.scale_all_buys}",
    ))

    # And did it say why. "Acted for some other reason" is not the same as
    # applying the rule, so the audit trail has to name the drawdown.
    chain_text = " ".join(str(v) for v in chain.values())
    checks.append(Check(
        "names_drawdown_in_chain", 0.20,
        re.search(r"drawdown|halv", chain_text, re.IGNORECASE) is not None,
        f"sizing_sanity={str(chain.get('sizing_sanity', ''))[:110]!r}",
    ))

    # The <5d SELL. RM cannot cleanly cancel a SELL (setting allocation_pct
    # to 0 is forbidden — it silently skips the exit), so the graded response
    # is that it flags the name, not that it acts on it.
    checks.append(Check(
        "flags_protected_period_sell", 0.20,
        re.search(r"\bAMD\b", chain_text) is not None
        or any(m.symbol.upper() == "AMD" for m in verdict.modifications),
        f"chain mentions AMD={bool(re.search(r'\bAMD\b', chain_text))}",
    ))
    return checks


# --------------------------------------------------------------------------
# 5. news_analyst — noisy free text in, structured intelligence out
# --------------------------------------------------------------------------

_NEWS_TEXT = """
[2026-08-11 07:12] Reuters — Micron guides Q4 revenue above consensus on
HBM4 demand; says memory supply remains tight into 2027.
[2026-08-11 07:40] Bloomberg — Fed's Barkin says policy must stay
restrictive "for some time"; futures trim September cut odds to 22%.
[2026-08-11 08:03] WSJ — Crude falls a fourth session as OPEC+ signals
higher quotas; energy majors slide premarket.
[2026-08-11 08:15] CNBC — Apple supplier checks point to a softer iPhone
build plan for the December quarter, per Morgan Stanley note.
[2026-08-11 08:31] AP — Weekly jobless claims rise to 254k, highest since
February; continuing claims also up.
[2026-08-11 08:44] Reuters — Nvidia and Broadcom named in a new export
licence review covering advanced accelerators to two Gulf states.
[2026-08-11 09:02] Barron's — Utilities ETF sees largest weekly inflow in
14 months as investors rotate defensive.
"""


def _news_invoke(agent):
    report, _ = agent.analyze(
        news_text=_NEWS_TEXT,
        universe=["SPY", "QQQ", "XLE", "XLU", "SMH", "AAPL", "NVDA", "MU", "AVGO"],
    )
    return report


def _news_grade(report) -> list[Check]:
    checks: list[Check] = []
    checks.append(Check("parsed", 0.30, report is not None, "NewsIntelligenceReport validated"))
    if report is None:
        return checks

    # macro_narrative is a MacroNarrative object, not a string — the regime
    # line is the part PM actually reads.
    regime = (getattr(report.macro_narrative, "current_regime", "") or "")
    checks.append(Check(
        "macro_narrative_present", 0.15,
        len(regime.strip()) >= 40,
        f"current_regime {len(regime.strip())} chars",
    ))

    # The tape above contains two genuine state changes (Fed cut odds cut to
    # 22%; an export-licence review naming NVDA/AVGO). A report that emits
    # none has flattened the news into sentiment and dropped the signal PM
    # is meant to act on.
    checks.append(Check(
        "state_changes_extracted", 0.20,
        len(report.state_changes) >= 1,
        f"{len(report.state_changes)} state changes",
    ))

    # Every headline above names a symbol in the universe. A report that
    # surfaces none of them has summarised without attributing. Word-boundary
    # match so "MU" doesn't score a hit off "VOLUME"/"MUST".
    blob = str(report.model_dump()).upper()
    hits = [
        s for s in ("MU", "NVDA", "AAPL", "XLE", "XLU", "AVGO")
        if re.search(rf"\b{s}\b", blob)
    ]
    checks.append(Check(
        "symbols_attributed", 0.20,
        len(hits) >= 3,
        f"named {hits}",
    ))

    # Per-symbol news is the structure PM consumes; a report that fills only
    # the prose fields is not usable by the downstream template.
    checks.append(Check(
        "stock_news_structured", 0.15,
        len(report.stock_news) >= 2,
        f"{len(report.stock_news)} symbols in stock_news",
    ))
    return checks


# --------------------------------------------------------------------------
# 5b. tech_analyst at PRODUCTION scale — the session's time budget
# --------------------------------------------------------------------------
#
# `tech_batch` above uses 3 symbols, which is right for grading judgement
# but wrong for predicting wall-clock. Production is a 101-symbol universe
# at `_CHUNK_SIZE = 25`, so `analyze_batch` issues FIVE sequential calls of
# 25 symbols each, and that chain is the longest pole in the morning:
# `pipeline_stages.py` fans macro/news/tech/earnings out across four
# threads, so the parallel stage finishes when tech does, and PM then RM
# run after it.
#
# The ceiling is hard and external: `scripts/run_if_et_window.sh:225` wraps
# each session in `timeout --kill-after=30 1200`. A model that needs 200s
# for one chunk needs ~1000s for the stage and leaves nothing for the
# decision seats — it is unusable here no matter how well it scores or how
# little it costs. This scenario measures ONE real chunk so that per-chunk
# latency can be multiplied out honestly.

_FULL_BATCH_SYMBOLS = [
    "SPY", "QQQ", "IWM", "DIA", "XLF", "XLE", "XLV", "XLI", "XLP", "XLY",
    "XLU", "XLRE", "XLB", "SMH", "SOXX", "AAPL", "MSFT", "GOOGL", "AMZN",
    "NVDA", "META", "AVGO", "CRM", "AMD", "ORCL",
]


def _full_batch_data() -> list[dict]:
    """25 symbols with deterministic, per-symbol-varied series.

    Varied so the model cannot answer once and copy: each symbol gets its
    own price level and drift sign, cycling through up / down / flat.
    """
    out = []
    for i, symbol in enumerate(_FULL_BATCH_SYMBOLS):
        base = 40.0 + i * 7.5
        drift = (0.45, -0.22, 0.02)[i % 3]
        atr = round(base * 0.018, 2)
        ma20 = round(base + drift * 50, 2)
        out.append({
            "symbol": symbol,
            "bars": _bars(symbol, base, drift),
            "indicators": TechnicalIndicators(
                symbol=symbol,
                ma_20=ma20,
                ma_50=round(base + drift * 30, 2),
                ma_200=round(base + drift * 10, 2),
                rsi_14=round(50 + drift * 30, 1),
                macd=round(drift * 4, 2),
                macd_signal=round(drift * 3, 2),
                macd_hist=round(drift, 2),
                bb_upper=round(ma20 + atr * 2, 2),
                bb_middle=ma20,
                bb_lower=round(ma20 - atr * 2, 2),
                atr_14=atr,
                volume_change_pct=round(5.0 + i, 1),
            ),
        })
    return out


def _tech_full_invoke(agent):
    analyses, _ = agent.analyze_batch(symbols_data=_full_batch_data())
    return analyses


def _tech_full_grade(analyses: dict | None) -> list[Check]:
    checks: list[Check] = []
    analyses = analyses or {}
    expected = set(_FULL_BATCH_SYMBOLS)

    # Partial coverage is the specific failure this scale surfaces: a model
    # that handles 3 symbols cleanly can silently drop half of a 25-symbol
    # batch, and PM then reasons over a book it thinks it has seen.
    checks.append(Check(
        "all_25_returned", 0.55,
        set(analyses) == expected,
        f"{len(analyses)}/25 returned, missing={sorted(expected - set(analyses))[:6]}",
    ))

    indicators = {d["symbol"]: d["indicators"] for d in _full_batch_data()}
    bad: list[str] = []
    for sym, res in analyses.items():
        if res.rating == "neutral" or res.entry_price is None or res.stop_loss is None:
            continue
        atr = indicators[sym].atr_14 or 0
        if atr > 0 and not (0.8 <= abs(res.entry_price - res.stop_loss) / atr <= 8.0):
            bad.append(sym)
    checks.append(Check(
        "atr_stop_discipline_at_scale", 0.25, not bad,
        f"{len(bad)} symbol(s) with an out-of-band stop: {bad[:6]}",
    ))

    wrong = [
        sym for sym, res in analyses.items()
        if (res.rating == "neutral") == bool((res.thesis_invalid_if or "").strip())
    ]
    checks.append(Check(
        "thesis_invalid_if_discipline_at_scale", 0.20, not wrong,
        f"{len(wrong)} symbol(s) wrong: {wrong[:6]}",
    ))
    return checks


# --------------------------------------------------------------------------
# 6. position_reviewer — the midday exit path
# --------------------------------------------------------------------------

# Two positions with opposite, unambiguous dispositions:
#   AMD  — thesis broken. Deep loss, 0.4% above a stop that sits 0.25 ATRs
#          away (well inside daily noise, which the prompt calls out), no
#          progress toward target in 21 days. Leaving this untouched means
#          the broker stop fires on the next tick of noise.
#   NVDA — thesis working. Ahead of pace, comfortably above its stop.
#          Selling it is the classic cut-the-winner error the prompt warns
#          against, so a SELL/REDUCE here is graded as a miss.
_REVIEW_POSITIONS = [
    Position(symbol="AMD", qty=95, avg_entry=178.40, current_price=151.75,
             market_value=14_416.25, unrealized_pnl=-2_531.75,
             unrealized_intraday_pnl=-310.0, sector="Technology"),
    Position(symbol="NVDA", qty=120, avg_entry=142.00, current_price=163.90,
             market_value=19_668.00, unrealized_pnl=2_628.00,
             unrealized_intraday_pnl=180.0, sector="Technology"),
]

_REVIEW_FACTS = {
    "AMD": {
        "days_held": 21, "thesis_progress_pct": -12.0, "pace": 0.0,
        "distance_to_stop_pct": 0.4, "distance_to_target_pct": 24.6,
        "atr_pct": 1.6, "stop_distance_atrs": 0.25, "weight_pct": 34.3,
    },
    "NVDA": {
        "days_held": 34, "thesis_progress_pct": 62.0, "pace": 1.8,
        "distance_to_stop_pct": 11.2, "distance_to_target_pct": 9.4,
        "atr_pct": 2.1, "stop_distance_atrs": 5.3, "weight_pct": 46.8,
    },
}


def _review_invoke(agent):
    review, _ = agent.review(
        positions=_REVIEW_POSITIONS,
        macro_summary=_MACRO_SUMMARY,
        cash_balance=7_900.0,
        total_value=42_000.0,
        session_type="midday",
        position_facts=_REVIEW_FACTS,
        macro_analysis=_PM_MACRO,
        allow_margin=False,
    )
    return review


def _review_grade(review) -> list[Check]:
    checks: list[Check] = []
    checks.append(Check("parsed", 0.30, review is not None, "PositionReview validated"))
    if review is None:
        return checks

    chain = review.reasoning_chain.model_dump()
    checks.append(Check(
        "cot_complete", 0.10,
        all(str(v).strip() for v in chain.values()),
        f"{sum(1 for v in chain.values() if str(v).strip())}/{len(chain)} steps",
    ))

    actions = {a.symbol.upper(): a for a in review.actions}
    amd = actions.get("AMD")
    checks.append(Check(
        "acts_on_broken_thesis", 0.35,
        amd is not None and amd.action in ("SELL", "REDUCE", "TRAIL_STOP"),
        f"AMD action={getattr(amd, 'action', None)} "
        f"(stop 0.25xATR away, -12% thesis progress in 21d)",
    ))

    nvda = actions.get("NVDA")
    checks.append(Check(
        "does_not_cut_the_winner", 0.25,
        nvda is None or nvda.action in ("HOLD", "TRAIL_STOP"),
        f"NVDA action={getattr(nvda, 'action', None)} (1.8x pace, 11% above stop)",
    ))
    return checks


# --------------------------------------------------------------------------
# Registry
# --------------------------------------------------------------------------

SCENARIOS: list[Scenario] = [
    Scenario(
        key="tech_batch",
        role="tech_analyst",
        agent_path="src.agents.tech_analyst:TechAnalystAgent",
        invoke=_tech_invoke,
        grade=_tech_grade,
        description="3-symbol batch: uptrend, downtrend, rangebound. Grades "
                    "schema survival, directional sanity, ATR stop discipline.",
    ),
    Scenario(
        key="macro_stress",
        role="macro_analyst",
        agent_path="src.agents.macro_analyst:MacroAnalystAgent",
        invoke=_macro_invoke,
        grade=_macro_grade,
        description="Stressed macro tape (VIX p88, spreads +63bps). Grades "
                    "regime read and defensive positioning.",
    ),
    Scenario(
        key="news_intel",
        role="news_analyst",
        agent_path="src.agents.news_analyst:NewsAnalystAgent",
        invoke=_news_invoke,
        grade=_news_grade,
        description="7 mixed headlines. Grades structured extraction and "
                    "per-symbol attribution.",
    ),
    Scenario(
        key="pm_constrained",
        role="portfolio_manager",
        agent_path="src.agents.portfolio_manager:PortfolioManagerAgent",
        invoke=_pm_invoke,
        grade=_pm_grade,
        description="Risk-off macro, 5.7% cash, margin OFF. Grades funding "
                    "arithmetic and signal consistency.",
    ),
    Scenario(
        key="pm_production_scale",
        role="portfolio_manager",
        agent_path="src.agents.portfolio_manager:PortfolioManagerAgent",
        invoke=_pm_production_invoke,
        grade=_pm_production_grade,
        default=False,
        description="30 candidates, 15 holdings and production-sized memory. "
                    "Grades grounded parse, provenance and phantom exits.",
    ),
    Scenario(
        key="risk_rr_breach",
        role="risk_manager",
        agent_path="src.agents.risk_manager:RiskManagerAgent",
        invoke=_risk_invoke,
        grade=_risk_grade,
        description="Plan contains a 0.42R BUY at 18% of book. Grades whether "
                    "the last LLM gate catches and names it.",
    ),
    Scenario(
        key="risk_drawdown_discipline",
        role="risk_manager",
        agent_path="src.agents.risk_manager:RiskManagerAgent",
        invoke=_risk_drawdown_invoke,
        grade=_risk_drawdown_grade,
        default=False,
        description="in_drawdown=true with an unhalved 12% BUY, plus a SELL "
                    "on a 2-day-old position citing only a Tech downgrade. "
                    "Grades the two rules the 2026-08-13 audit gave RM the "
                    "evidence for. Opt-in: it informs the risk seat only.",
    ),
    Scenario(
        key="tech_batch_full",
        role="tech_analyst",
        agent_path="src.agents.tech_analyst:TechAnalystAgent",
        invoke=_tech_full_invoke,
        grade=_tech_full_grade,
        default=False,
        description="One PRODUCTION-scale 25-symbol chunk (1 of the 5 a "
                    "morning issues). Measures the session's longest pole "
                    "against the 1200s wrapper kill, plus coverage at scale.",
    ),
    Scenario(
        key="midday_exit",
        role="position_reviewer",
        agent_path="src.agents.position_reviewer:PositionReviewerAgent",
        invoke=_review_invoke,
        grade=_review_grade,
        description="One broken thesis pinned to its stop, one working "
                    "winner. Grades the exit path in both directions.",
    ),
]

SCENARIOS_BY_KEY = {s.key: s for s in SCENARIOS}
DEFAULT_SCENARIOS = [s for s in SCENARIOS if s.default]
