import json
import logging
import re
from pathlib import Path

from pydantic import ValidationError

from src.agents.base import BaseAgent
from src.models import (
    NewsIntelligenceReport, PortfolioDecision, Position, TargetPosition,
    TechAnalysisResult,
)
from src.risk.rules import _effective_multiplier, _gross_multiplier

logger = logging.getLogger(__name__)

PROMPT_PATH = Path(__file__).parent.parent.parent / "config" / "prompts" / "portfolio_manager.md"


class PortfolioManagerAgent(BaseAgent):
    @property
    def name(self) -> str:
        return "portfolio_manager"

    @property
    def system_prompt(self) -> str:
        if PROMPT_PATH.exists():
            return PROMPT_PATH.read_text()
        return "You are a portfolio manager. Respond with JSON."

    @staticmethod
    def _collapse_stances(values) -> str | None:
        cleaned = {
            str(value).strip().lower().replace(" ", "_")
            for value in values if str(value).strip()
        }
        if not cleaned:
            return None
        if len(cleaned) == 1:
            return next(iter(cleaned))
        positive = {"strong_buy", "buy", "bullish", "positive", "risk_on", "overweight", "favorable"}
        negative = {"strong_sell", "sell", "bearish", "negative", "risk_off", "underweight", "unfavorable"}
        if cleaned <= positive:
            return "bullish"
        if cleaned <= negative:
            return "bearish"
        if cleaned <= {"neutral", "mixed"}:
            return "neutral" if cleaned == {"neutral"} else "mixed"
        return "mixed"

    @classmethod
    def build_evidence_registry(
        cls,
        *,
        analyses: list[TechAnalysisResult],
        positions: list[Position],
        news_intel: NewsIntelligenceReport | None,
        earnings_analyses: list[dict],
        macro_analysis: dict | None,
        symbol_sectors: dict[str, str] | None = None,
        session_type: str = "morning",
    ) -> dict[str, dict[str, str]]:
        """Canonical source/stance records shared by prompt and validator.

        Display decorations such as conviction and signal age never enter the
        stance. Historical narrative/memory is intentionally excluded. The
        intraday path has only current-session Tech evidence, so it cannot
        cite yesterday's macro/news/earnings as if they ran this tick.
        """

        registry: dict[str, dict[str, str]] = {}

        def put(symbol: str, source: str, stance: str | None) -> None:
            if symbol and stance:
                registry.setdefault(symbol.strip().upper(), {})[source] = stance

        for analysis in analyses:
            put(analysis.symbol, "technical", cls._collapse_stances([analysis.rating]))

        if session_type == "intra_check":
            return {symbol: sources for symbol, sources in registry.items() if sources}

        if news_intel is not None:
            for symbol, items in news_intel.stock_news.items():
                put(symbol, "news", cls._collapse_stances(i.sentiment for i in items))

        for item in earnings_analyses:
            analysis = item.get("analysis") or {}
            sentiment = (analysis.get("investment_implications") or {}).get("sentiment")
            put(str(item.get("symbol") or ""), "earnings", cls._collapse_stances([sentiment]))

        if macro_analysis:
            sectors = {str(k).upper(): str(v) for k, v in (symbol_sectors or {}).items()}
            for position in positions:
                if position.sector:
                    sectors.setdefault(position.symbol.upper(), position.sector)
            guidance: dict[str, list[str]] = {}
            for row in macro_analysis.get("sector_guidance", []) or []:
                sector = str(row.get("sector") or "").strip().lower()
                stance = row.get("stance")
                if sector and stance:
                    guidance.setdefault(sector, []).append(str(stance))
            broad = cls._collapse_stances([
                macro_analysis.get("equity_outlook") or macro_analysis.get("regime")
            ])
            symbols = set(registry) | {p.symbol.upper() for p in positions}
            for symbol in symbols:
                sector = sectors.get(symbol, "").strip().lower()
                stance = cls._collapse_stances(guidance.get(sector, [])) if sector else None
                put(symbol, "macro", stance or broad)

        return {symbol: sources for symbol, sources in registry.items() if sources}

    def build_user_message(self, **kwargs) -> str:
        analyses: list[TechAnalysisResult] = kwargs["analyses"]
        positions: list[Position] = kwargs["positions"]
        macro_analysis: dict | None = kwargs.get("macro_analysis")
        cash_balance: float = kwargs["cash_balance"]
        # Short-term reserve (SGOV/cash-equivalent sweep parking), reported
        # separately from cash_balance — 2026-08-19 SGOV/deployable-
        # liquidity forensic. Never fold this into cash_balance: it is not
        # reliably spendable same-day (Alpaca T+1 equity settlement), so
        # sizing against it produces BUYs execution can't actually fund.
        reserve_balance: float = kwargs.get("reserve_balance", 0.0) or 0.0
        total_value: float = kwargs["total_value"]
        news_intel: NewsIntelligenceReport | None = kwargs.get("news_intel")
        earnings_analyses: list[dict] = kwargs.get("earnings_analyses", [])
        evidence_registry = self.build_evidence_registry(
            analyses=analyses,
            positions=positions,
            news_intel=news_intel,
            earnings_analyses=earnings_analyses,
            macro_analysis=macro_analysis,
            symbol_sectors=kwargs.get("symbol_sectors") or {},
            session_type=kwargs.get("session_type") or "morning",
        )
        evidence_registry_text = json.dumps(
            evidence_registry, sort_keys=True, indent=2,
        )

        def _fmt_tech(a):
            rr = a.risk_reward
            rr_str = f"R/R {rr:.2f}:1" if rr is not None else "R/R n/a"
            invalid = a.thesis_invalid_if or "(not specified)"
            age = getattr(a, "signal_age_days", None)
            age_str = f", age {age}d" if age is not None and age > 0 else ""
            return (
                f"- {a.symbol}: {a.rating} ({a.conviction}{age_str}) | {rr_str} | "
                f"Entry: {a.entry_price} | Stop: {a.stop_loss} | Target: {a.reference_target}\n"
                f"  Invalid if: {invalid}\n"
                f"  Reasoning: {a.reasoning}"
            )
        analyses_text = "\n".join(_fmt_tech(a) for a in analyses)

        # L2 memory: each position line also gets entry context + Tech rating trajectory
        # so PM can anchor "when bought / for what reason / how signal has evolved".
        position_history: dict = kwargs.get("position_history") or {}

        def _fmt_position(p: Position) -> str:
            # audit round 2 #22: show the GROSS weight — the same basis
            # PortfolioConstructor uses when comparing target_weight_pct to
            # current weights (leveraged/inverse ETF market value × |mult|).
            # Rendering the raw weight made PM restate e.g. a 3x SQQQ's 6%
            # as its target, which the constructor read as "cut from 18% to
            # 6%" and emitted a 67% SELL the PM never intended.
            gross_mul = _gross_multiplier(p.symbol)
            weight_pct = (
                (p.market_value * gross_mul / total_value * 100)
                if total_value > 0 else 0.0
            )
            lev_note = f" (gross, {gross_mul:g}x leveraged)" if gross_mul != 1.0 else ""
            # Flag drift candidates directly in the line so PM can't miss them.
            # P&L% tells PM whether the weight came from price appreciation (drift)
            # or a large entry.
            cost_basis = p.avg_entry * p.qty if p.avg_entry and p.qty else 0
            pnl_pct = (p.unrealized_pnl / cost_basis * 100) if cost_basis > 0 else 0.0
            drift_flag = " ⚠️DRIFT" if weight_pct > 12 and pnl_pct > 10 else ""
            core = (
                f"- {p.symbol}: {p.qty} shares @ ${p.avg_entry:.2f} | "
                f"Current: ${p.current_price:.2f} | P&L: ${p.unrealized_pnl:.2f} ({pnl_pct:+.1f}%) | "
                f"Weight: {weight_pct:.1f}%{lev_note} | Sector: {p.sector}{drift_flag}"
            )
            hist = position_history.get(p.symbol) or {}
            lines = [core]
            entry_date = hist.get("entry_date")
            days_held = hist.get("days_held")
            if entry_date or days_held is not None:
                label = f"entry {entry_date or 'unknown'}"
                if days_held is not None:
                    label += f", held {days_held}d"
                reasoning = (hist.get("entry_reasoning") or "").strip()
                if reasoning:
                    label += f' — "{reasoning}"'
                lines.append(f"  Bought: {label}")
            tech_hist = hist.get("tech_history") or []
            if tech_hist:
                trail = " → ".join(
                    f"{h.get('rating', '?')}({h.get('conviction', '?')[0]})"
                    for h in tech_hist
                )
                lines.append(f"  Tech history (last {len(tech_hist)}d): {trail}")
            return "\n".join(lines)

        positions_text = "\n".join(_fmt_position(p) for p in positions) if positions else "No current positions."

        # Format macro analysis section
        if macro_analysis:
            observations_text = "\n".join(
                f"- {o['indicator']}: {o['reading']} — {o['interpretation']}"
                for o in macro_analysis.get("key_observations", [])
            ) if macro_analysis.get("key_observations") else "No observations."

            sector_guidance_text = "\n".join(
                f"- {s['sector']}: {s['stance']} — {s['reason']}"
                for s in macro_analysis.get("sector_guidance", [])
            ) if macro_analysis.get("sector_guidance") else "No sector guidance."

            risk_factors_text = "\n".join(
                f"- {r}" for r in macro_analysis.get("risk_factors", [])
            ) if macro_analysis.get("risk_factors") else "None identified."

            pos_guidance = macro_analysis.get("position_guidance", {}) or {}
            rc = macro_analysis.get("reasoning_chain", {}) or {}

            shift_line = ""
            if macro_analysis.get("regime_shift"):
                shift_line = f"\n- **REGIME SHIFT TODAY**: {macro_analysis.get('shift_reason', 'reason unspecified')}"

            alignment = macro_analysis.get("alignment_with_news", "")
            alignment_line = f"\n- News alignment: {alignment}" if alignment else ""

            reasoning_section = ""
            if rc:
                reasoning_section = f"""

### Macro Reasoning Chain (audit these for logic errors)
- Volatility: {rc.get('volatility_analysis', 'N/A')}
- Yield curve: {rc.get('yield_curve_analysis', 'N/A')}
- Monetary policy: {rc.get('monetary_policy_analysis', 'N/A')}
- Inflation/labor/credit: {rc.get('inflation_labor_credit', 'N/A')}
- Cross-signal synthesis: {rc.get('cross_signal_synthesis', 'N/A')}
- Sector implications: {rc.get('sector_implications', 'N/A')}"""

            bull_triggers = macro_analysis.get("bull_triggers", []) or []
            bear_triggers = macro_analysis.get("bear_triggers", []) or []
            triggers_section = ""
            if bull_triggers or bear_triggers:
                bull_text = "\n".join(f"  + {t}" for t in bull_triggers) or "  (none)"
                bear_text = "\n".join(f"  - {t}" for t in bear_triggers) or "  (none)"
                triggers_section = f"""

### View-Change Triggers
Bull triggers (would turn more constructive):
{bull_text}
Bear triggers (would turn defensive):
{bear_text}"""

            target_inv = pos_guidance.get('target_invested_pct', 'N/A')
            cash_rec = pos_guidance.get('cash_recommendation_pct', 'N/A')

            macro_section = f"""## Macro Analysis
- Regime: {macro_analysis.get('regime', 'N/A')} | Outlook: {macro_analysis.get('equity_outlook', 'N/A')} | Confidence: {macro_analysis.get('confidence', 'N/A')}{shift_line}{alignment_line}
- Summary: {macro_analysis.get('summary', 'N/A')}{reasoning_section}

### Key Observations
{observations_text}

### Sector Guidance
{sector_guidance_text}

### Risk Factors
{risk_factors_text}{triggers_section}

### Position Guidance
- Target invested: {target_inv}%
- Cash recommendation: {cash_rec}%
- Reasoning: {pos_guidance.get('reasoning', 'N/A')}"""
        else:
            macro_section = "## Macro Analysis\nNo macro data available."

        # Format news intelligence section (3-layer)
        if news_intel:
            # Layer 1: Macro narrative
            mn = news_intel.macro_narrative
            era_text = "; ".join(mn.era_themes) if mn.era_themes else "N/A"
            state_items = "\n".join(f"  - {k}: {v}" for k, v in mn.key_state_tracker.items()) if mn.key_state_tracker else "  No tracked states."

            # Layer 2: State changes
            if news_intel.state_changes:
                changes_text = "\n".join(
                    f"- [{c.conviction.upper()}] {c.event}\n  Was: {c.previous_state} → Now: {c.new_state}\n  Impact: {c.market_impact}"
                    for c in news_intel.state_changes
                )
            else:
                changes_text = "No significant state changes today."

            # Layer 3: Stock-specific (sorted by conviction, top 3 per symbol)
            _conv_order = {"high": 0, "medium": 1, "low": 2}
            stock_items = []
            for sym, alerts in news_intel.stock_news.items():
                sorted_alerts = sorted(alerts, key=lambda a: _conv_order.get(a.conviction, 9))
                for a in sorted_alerts[:3]:
                    stock_items.append(f"- {sym}: [{a.conviction.upper()}] {a.sentiment} — {a.impact_summary}")
            stock_text = "\n".join(stock_items) if stock_items else "No stock-specific news."

            news_section = f"""## News Intelligence
### PM Briefing
{news_intel.pm_briefing}

### Macro Narrative (Grand Backdrop)
- Regime: {mn.current_regime}
- Era themes: {era_text}
- State tracker:
{state_items}

### State Changes (What Changed Today)
{changes_text}

### Stock-Specific News
{stock_text}

Overall sentiment: {news_intel.market_sentiment} (confidence: {news_intel.confidence})"""
        else:
            news_section = "## News Intelligence\nNo news data available."

        # Format earnings analysis section
        if earnings_analyses:
            earnings_items = []
            for ea in earnings_analyses:
                sym = ea.get("symbol", "?")
                # Queued placeholder — new filing dropped today, LLM still analyzing.
                if ea.get("queued") and not ea.get("analysis"):
                    earnings_items.append(
                        f"### {sym} — {ea.get('form_type', '?')} ({ea.get('filing_date', '?')}) "
                        f"[JUST FILED — analysis in progress, not yet ready for this run]\n"
                        f"- Discount any prior-quarter cached data for {sym} accordingly. "
                        f"New filing's numbers and guidance will be available next session."
                    )
                    continue
                analysis = ea.get("analysis")
                if not analysis:
                    continue
                impl = analysis.get("investment_implications", {})
                rev = analysis.get("revenue", {})
                prof = analysis.get("profitability", {})
                guidance = analysis.get("guidance", "N/A")
                filing_label = f"{ea.get('form_type', '?')} ({ea.get('filing_date', '?')})"
                source_note = " [from cache]" if not ea.get("is_new") else " [new filing]"

                # Strategic direction
                strat = analysis.get("strategic_direction", {})
                initiatives = strat.get("key_initiatives", [])
                initiatives_text = "; ".join(initiatives[:3]) if initiatives else "not disclosed"
                competitive = strat.get("competitive_positioning", "not disclosed")

                # Risk flags (structured or legacy list)
                risks = analysis.get("risk_flags", {})
                if isinstance(risks, dict):
                    strat_risks = risks.get("strategic_risks", [])
                    ops_risks = risks.get("operational_risks", [])
                    strat_risks_text = "; ".join(strat_risks[:2]) if strat_risks else "none flagged"
                    ops_risks_text = "; ".join(ops_risks[:2]) if ops_risks else "none flagged"
                    risk_line = f"- Strategic risks: {strat_risks_text}\n- Operational risks: {ops_risks_text}"
                else:
                    risk_line = f"- Risk flags: {'; '.join(risks[:3]) if risks else 'none flagged'}"

                consistency = analysis.get("strategy_consistency", "")
                consistency_line = f"\n- Strategy consistency: {consistency}" if consistency else ""

                earnings_items.append(
                    f"### {sym} — {filing_label}{source_note}\n"
                    f"- Filing metrics: Revenue {rev.get('total', 'N/A')} (YoY: {rev.get('yoy_growth', 'N/A')}), "
                    f"Gross margin {prof.get('gross_margin', 'N/A')}, Operating margin {prof.get('operating_margin', 'N/A')}, "
                    f"EPS {prof.get('eps', 'N/A')}\n"
                    f"- Filing guidance: {guidance}\n"
                    f"- Strategy: {initiatives_text}\n"
                    f"- Competitive positioning: {competitive}\n"
                    f"{risk_line}{consistency_line}\n"
                    f"- Analyst synthesis: {impl.get('sentiment', 'N/A')} ({impl.get('conviction', 'N/A')}) — {impl.get('key_thesis', 'N/A')}\n"
                    f"- Data quality: {analysis.get('data_quality', 'N/A')}"
                )
            earnings_section = "## Earnings Analysis (from SEC Filings)\n\n" + "\n\n".join(earnings_items)
        else:
            earnings_section = "## Earnings Analysis\nNo recent earnings filings available."

        invested = total_value - cash_balance
        invested_pct = (invested / total_value * 100) if total_value else 0

        # Margin policy — when allow_margin is False and cash is already
        # negative, de-lever SELLs are mandatory this session. The risk
        # engine will hard-block any new BUY that doesn't fit in cash, so
        # surfacing the mandate here gives the LLM the chance to pick
        # which positions to trim rather than having every BUY rejected
        # without context.
        allow_margin: bool = bool(kwargs.get("allow_margin", True))
        from src.risk.constants import MARGIN_DEFICIT_FLOOR_USD
        if not allow_margin and cash_balance < -MARGIN_DEFICIT_FLOOR_USD:
            deficit = -cash_balance
            margin_section = (
                "## ⚠️ DE-LEVER MANDATE (margin disabled, cash is negative)\n"
                f"- Current cash: ${cash_balance:,.2f} (deficit ${deficit:,.2f})\n"
                f"- Policy: this account runs cash-only — new BUYs cannot draw margin.\n"
                f"- **You MUST emit SELL targets summing to at least ${deficit:,.2f} of "
                f"market value this session.** Pick the weakest-conviction / most-extended "
                f"positions per your usual rules.\n"
                "- Any BUY you propose will be hard-blocked until cash is ≥ 0 after the "
                "session's SELLs clear."
            )
        elif not allow_margin:
            margin_section = (
                "## Margin Policy\n"
                "- Cash-only account: BUYs are capped at available cash after prior "
                "BUYs this session. Margin is disabled."
            )
        else:
            margin_section = ""

        # Recent system performance (drawdown awareness).
        recent_perf = kwargs.get("recent_performance") or {}
        if recent_perf:
            r5 = recent_perf.get("rolling_5d_pct")
            r20 = recent_perf.get("rolling_20d_pct")
            dd = recent_perf.get("in_drawdown")
            trailing = recent_perf.get("trailing_days") or 0
            dd_marker = " ⚠️ SYSTEM IN DRAWDOWN" if dd else ""
            perf_section = (
                f"## Recent System Performance (drawdown check){dd_marker}\n"
                f"- Trailing 5-day return: {r5}%\n"
                f"- Trailing 20-day return: {r20}%\n"
                f"- Drawdown threshold: 5d < −3% OR 20d < −8% flags in_drawdown\n"
                f"- History length: {trailing} days recorded\n"
            )
        else:
            perf_section = "## Recent System Performance\nNo history yet."

        # Yesterday's insights section
        yesterday_insights: dict | None = kwargs.get("yesterday_insights")
        if yesterday_insights and yesterday_insights.get("tomorrow_outlook"):
            actions = yesterday_insights.get("suggested_actions", "")
            if isinstance(actions, str):
                try:
                    actions = json.loads(actions)
                except (json.JSONDecodeError, TypeError):
                    pass
            actions_text = "\n".join(f"  - {a}" for a in actions) if isinstance(actions, list) else f"  - {actions}"
            key_risks = yesterday_insights.get("tomorrow_key_risks", "[]")
            if isinstance(key_risks, str):
                try:
                    key_risks = json.loads(key_risks)
                except (json.JSONDecodeError, TypeError):
                    key_risks = []
            risks_text = (
                "\n".join(f"  - {r}" for r in key_risks)
                if isinstance(key_risks, list) and key_risks
                else "  (none named)"
            )
            insights_date = yesterday_insights.get("date", "unknown")
            insights_ts = yesterday_insights.get("timestamp", "")
            freshness = f" (from {insights_date}"
            if insights_ts:
                freshness += f", written {insights_ts}"
            freshness += ")"
            bias = yesterday_insights.get("tomorrow_bias") or "neutral"
            conviction = yesterday_insights.get("tomorrow_conviction") or "medium"
            sell_grade = (yesterday_insights.get("sell_decisions_assessment") or "").strip()
            sell_line = (
                f"- **SELL discipline grade** (previous run): {sell_grade[:400]}"
                if sell_grade else ""
            )
            insights_section = f"""## Prior Evening Insights{freshness}
- **Tilt for today**: bias={bias}, conviction={conviction}
- Outlook (prose): {yesterday_insights.get('tomorrow_outlook', 'N/A')}
- Key risks to watch today:
{risks_text}
- Lessons: {yesterday_insights.get('lessons', 'N/A')}
- Risk Rating: {yesterday_insights.get('risk_rating', 'N/A')}
- Suggested Actions:
{actions_text}
{sell_line}"""
        else:
            insights_section = "## Yesterday's Evening Insights\nNo prior session insights available."

        # L3 memory layers — past environment trajectory
        weekly_narrative: str = kwargs.get("weekly_narrative") or ""
        macro_trajectory: str = kwargs.get("macro_trajectory") or ""
        active_state_changes: str = kwargs.get("active_state_changes") or ""
        # Phase-1 evening-upgrade feedback:
        # L3d — themes evening flagged as missed ≥ 2 times in last 14 days.
        # L3f — loss root-causes evening classified on wrong BUYs repeatedly.
        # Both empty strings when no recurring pattern; section shows defaults.
        recent_missed_lessons: str = kwargs.get("recent_missed_lessons") or ""
        recent_loss_pits: str = kwargs.get("recent_loss_pits") or ""

        narrative_section = (
            f"## Portfolio Narrative (last 7 trading days)\n{weekly_narrative}"
            if weekly_narrative else
            "## Portfolio Narrative\nNo prior narrative yet (fresh table)."
        )
        trajectory_section = (
            f"## Macro Regime Trajectory (last 7 days)\n{macro_trajectory}"
            if macro_trajectory else
            "## Macro Regime Trajectory\nNo prior snapshots yet."
        )
        active_changes_section = (
            f"## Active News State Changes (HIGH conviction, last 14d)\n{active_state_changes}"
            if active_state_changes else
            "## Active News State Changes\n(none surfaced in the rolling 14-day window)"
        )
        missed_lessons_section = (
            f"## Recurring Missed Themes (last 14d — themes evening repeatedly "
            f"flagged as misses)\n{recent_missed_lessons}\n\n"
            "If a theme has appeared 2+ times here, it's a coverage or "
            "timing blind-spot, not random noise. Take a fresh look at it "
            "today before it runs further away."
            if recent_missed_lessons else
            "## Recurring Missed Themes\n(no recurring missed themes in the "
            "last 14 days)"
        )
        loss_pits_section = (
            f"## Recent Loss Pits (last 14d — repeat failure modes on losing "
            f"BUYs)\n{recent_loss_pits}\n\n"
            "If a root-cause has 2+ occurrences, it's a discipline gap, not "
            "bad luck. Lean against it today — tighten entries / respect "
            "warnings / cut concentration before you do the same thing again."
            if recent_loss_pits else
            "## Recent Loss Pits\n(no repeat failure modes in the last 14 days)"
        )

        # Self-calibration layers: PM reads RM's recent verdicts on it + its own
        # recent decisions, to avoid oversizing repeatedly and to spot flip-flops.
        rm_recent_verdicts: str = kwargs.get("rm_recent_verdicts") or ""
        pm_recent_decisions: str = kwargs.get("pm_recent_decisions") or ""
        projected_portfolio: str = kwargs.get("projected_portfolio") or ""
        calibration_note: str = kwargs.get("calibration_note") or ""
        macro_tech_alignment: str = kwargs.get("macro_tech_alignment") or ""
        facts = kwargs.get("facts")  # PMFacts | None

        rm_verdicts_section = (
            f"## Risk Manager Verdicts (last 5 sessions — self-calibrate)\n{rm_recent_verdicts}"
            if rm_recent_verdicts else
            "## Risk Manager Verdicts\n(no prior RM verdicts on record)"
        )
        pm_decisions_section = (
            f"## Your Recent Decisions (last 3 sessions — avoid flip-flops)\n{pm_recent_decisions}"
            if pm_recent_decisions else
            "## Your Recent Decisions\n(no prior PM decisions on record)"
        )
        projected_section = (
            f"## Projected Book Preview (if you rubber-stamp TA's BUYs at 5% each)\n{projected_portfolio}"
            if projected_portfolio else
            "## Projected Book Preview\n(no projection available — empty book or no BUY candidates)"
        )
        calibration_section = (
            f"## Trade Calibration (your actual realized outcomes)\n{calibration_note}"
            if calibration_note else
            "## Trade Calibration\n(not enough closed trades yet for calibration — <3 in window)"
        )
        alignment_section = (
            f"## Macro-Tech Alignment Advisory\n{macro_tech_alignment}"
            if macro_tech_alignment else ""
        )
        # Phase 4 #4: structured facts block — numbers, not prose. PM should
        # prefer these over the derived narrative sections below for quantitative
        # questions (win rate, sector weight, age distribution).
        facts_section = (
            f"## Quantitative Facts (read these first for numbers)\n{facts.render()}"
            if facts is not None else ""
        )

        reserve_line = (
            f"\n  (of which ${reserve_balance:,.2f} is parked in the "
            f"cash-equivalent sweep vehicle and is auto-liquidated before "
            f"any BUY executes — already included in Cash Balance above, "
            f"do not add it again)"
            if reserve_balance > 0 else ""
        )
        return f"""## Account Status
- Total Value: ${total_value:,.2f}
- Cash Balance: ${cash_balance:,.2f} (deployable this session, no margin){reserve_line}
- Invested: ${invested:,.2f} ({invested_pct:.1f}%)

## Current Positions (with entry context + signal trajectory)
{positions_text}

{margin_section}

{facts_section}

{projected_section}

{perf_section}

{calibration_section}

{alignment_section}

{pm_decisions_section}

{rm_verdicts_section}

{narrative_section}

{trajectory_section}

{active_changes_section}

{missed_lessons_section}

{loss_pits_section}

{insights_section}

{macro_section}

{news_section}

{earnings_section}

## Technical Analysis Reports
{analyses_text}

## Canonical Current Evidence Registry (authoritative for provenance)
{evidence_registry_text}

For every target, cite only source/stance pairs present for that exact symbol
in this registry and copy the stance string exactly. Omit unavailable sources.
Memory and narrative sections are context, never current specialist coverage.

Based on all the above (memory of past decisions + environment trajectory + today's signals), what trades should we execute? Respond as JSON."""

    @staticmethod
    def _semantic_failure(result, status: str, error: object):
        result.semantic_status = status
        result.semantic_error = str(error)
        return None, result

    def decide(self, analyses: list[TechAnalysisResult], positions: list[Position],
               macro_analysis: dict | None = None, cash_balance: float = 0,
               reserve_balance: float = 0.0,
               total_value: float = 0,
               news_intel: NewsIntelligenceReport | None = None,
               earnings_analyses: list[dict] | None = None,
               yesterday_insights: dict | None = None,
               recent_performance: dict | None = None,
               position_history: dict | None = None,
               weekly_narrative: str = "",
               macro_trajectory: str = "",
               active_state_changes: str = "",
               rm_recent_verdicts: str = "",
               pm_recent_decisions: str = "",
               projected_portfolio: str = "",
               calibration_note: str = "",
               macro_tech_alignment: str = "",
               recent_missed_lessons: str = "",
               recent_loss_pits: str = "",
               facts=None,
               allow_margin: bool = True,
               symbol_sectors: dict[str, str] | None = None,
               session_type: str = "morning") -> tuple[PortfolioDecision | None, "AgentResult"]:
        result = self.run(
            analyses=analyses,
            positions=positions,
            macro_analysis=macro_analysis,
            cash_balance=cash_balance,
            reserve_balance=reserve_balance,
            total_value=total_value,
            news_intel=news_intel,
            earnings_analyses=earnings_analyses or [],
            yesterday_insights=yesterday_insights,
            recent_performance=recent_performance or {},
            position_history=position_history or {},
            weekly_narrative=weekly_narrative,
            macro_trajectory=macro_trajectory,
            active_state_changes=active_state_changes,
            rm_recent_verdicts=rm_recent_verdicts,
            pm_recent_decisions=pm_recent_decisions,
            projected_portfolio=projected_portfolio,
            calibration_note=calibration_note,
            macro_tech_alignment=macro_tech_alignment,
            recent_missed_lessons=recent_missed_lessons,
            recent_loss_pits=recent_loss_pits,
            facts=facts,
            allow_margin=allow_margin,
            symbol_sectors=symbol_sectors or {},
            session_type=session_type,
        )
        parsed = result.parse_json()
        if parsed is None:
            logger.error("Portfolio manager returned non-JSON response")
            return self._semantic_failure(
                result, "pm_parse_error", "response did not contain a valid decision JSON object",
            )
        if not isinstance(parsed, dict):
            # A PortfolioDecision is an OBJECT. A bare list here means the
            # candidate scan surfaced a fragment (historically: the plan's own
            # `targets` array) instead of the decision — treat as a parse
            # failure so the session retries, never as a deliberate hold.
            # `PortfolioDecision(**list)` below would raise anyway; this makes
            # the failure mode explicit and greppable.
            logger.error(
                "Portfolio manager parse produced %s, not a decision object — "
                "treating as parse failure (fragment selected over full plan?)",
                type(parsed).__name__,
            )
            return self._semantic_failure(
                result, "pm_parse_error", f"parsed {type(parsed).__name__}, expected object",
            )
        # Per-entry isolation for targets: a single malformed TargetPosition
        # (e.g. target_weight_pct=30 violating the 0-25 range, or empty
        # thesis on a Field with no min_length but PortfolioConstructor's
        # contract assumes non-empty) must not drop the WHOLE PortfolioDecision.
        # Highest blast radius of any per-entry isolation gap: losing the
        # decision means losing reasoning_chain + portfolio_view + every
        # OTHER target → entire morning session is silenced. The
        # PortfolioConstructor downstream still has remaining valid targets
        # to translate into orders; better to fire 4 of 5 trades than 0 of 5.
        # Mirrors PR #73/#74 pattern.
        parsed_target_count = (
            len(parsed.get("targets", []))
            if isinstance(parsed, dict) and isinstance(parsed.get("targets", []), list)
            else 0
        )
        if isinstance(parsed, dict):
            parsed = self._drop_invalid_targets(parsed)
        try:
            decision = PortfolioDecision(**parsed)
            if parsed_target_count > 0 and not decision.targets:
                logger.error(
                    "Portfolio manager emitted %d target(s), but all were invalid; "
                    "treating as agent failure, not a no-action decision",
                    parsed_target_count,
                )
                return self._semantic_failure(
                    result, "pm_schema_error",
                    f"all {parsed_target_count} emitted targets were invalid",
                )
            errors = self.validate_grounding(
                decision, analyses=analyses, positions=positions,
                news_intel=news_intel,
                earnings_analyses=earnings_analyses or [],
                macro_analysis=macro_analysis, total_value=total_value,
                symbol_sectors=symbol_sectors or {}, session_type=session_type,
            )
            if errors:
                logger.error(
                    "Portfolio decision failed deterministic grounding: %s",
                    "; ".join(errors),
                )
                return self._semantic_failure(
                    result, "pm_grounding_error", "; ".join(errors),
                )
            return decision, result
        except ValidationError as e:
            # Mirror of the RiskManager repair path (2026-08-18 incident
            # class): a decision that parsed as JSON but failed schema
            # validation (typically an omitted mandatory reasoning_chain
            # field) costs a FULL research re-run 30 minutes later via
            # analysis_error. One immediate ~$0.006 repair call naming the
            # validation errors is strictly cheaper; a second failure keeps
            # today's fail-closed None → analysis_error path.
            #
            # External review (post-implementation): a schema repair must
            # never become a re-decision. `targets` is the decision — if
            # the validation failure is rooted there, repair can't fix it
            # without the model re-deciding, so skip repair and fail
            # closed. Otherwise, after repair, the target set (symbol +
            # weight) must be byte-identical to the pre-repair parse; any
            # drift fails closed too.
            if self.validation_error_touches(e, self._DECISION_FIELDS):
                logger.error(
                    "Portfolio decision validation failure is rooted in a "
                    "decision-bearing field (%s) — not schema-repairable; "
                    "failing closed: %s",
                    ", ".join(self._DECISION_FIELDS), e,
                )
                return self._semantic_failure(result, "pm_schema_error", e)
            repaired = self.repair_reprompt(result, e, "PortfolioDecision")
            reparsed = repaired.parse_json()
            if isinstance(reparsed, dict):
                repaired_target_count = (
                    len(reparsed.get("targets", []))
                    if isinstance(reparsed.get("targets", []), list) else 0
                )
                reparsed = self._drop_invalid_targets(reparsed)
                if not self._decision_fields_unchanged(parsed, reparsed):
                    logger.error(
                        "Portfolio decision repair changed target symbols/"
                        "weights instead of only completing the schema — "
                        "treating as an unauthorized re-decision and "
                        "failing closed.",
                    )
                    return self._semantic_failure(
                        repaired, "pm_repair_changed_decision",
                        "schema repair changed target symbols or weights",
                    )
                try:
                    decision = PortfolioDecision(**reparsed)
                    if repaired_target_count > 0 and not decision.targets:
                        logger.error(
                            "Portfolio repair emitted %d target(s), but all were "
                            "invalid; failing closed",
                            repaired_target_count,
                        )
                        return self._semantic_failure(
                            repaired, "pm_schema_error",
                            f"all {repaired_target_count} repaired targets were invalid",
                        )
                    errors = self.validate_grounding(
                        decision, analyses=analyses, positions=positions,
                        news_intel=news_intel,
                        earnings_analyses=earnings_analyses or [],
                        macro_analysis=macro_analysis, total_value=total_value,
                        symbol_sectors=symbol_sectors or {}, session_type=session_type,
                    )
                    if errors:
                        logger.error(
                            "Repaired portfolio decision failed deterministic "
                            "grounding: %s", "; ".join(errors),
                        )
                        return self._semantic_failure(
                            repaired, "pm_grounding_error", "; ".join(errors),
                        )
                    logger.info(
                        "Portfolio decision repair succeeded (%d targets)",
                        len(decision.targets),
                    )
                    return decision, repaired
                except Exception as e2:  # noqa: BLE001
                    logger.error(
                        "Failed to parse portfolio decision after repair: %s", e2,
                    )
                    return self._semantic_failure(repaired, "pm_schema_error", e2)
            logger.error(
                "Portfolio decision repair returned %s, not an object",
                type(reparsed).__name__,
            )
            return self._semantic_failure(
                repaired, "pm_parse_error",
                f"repair parsed {type(reparsed).__name__}, expected object",
            )
        except Exception as e:
            logger.error("Failed to parse portfolio decision: %s", e)
            return self._semantic_failure(result, "pm_schema_error", e)

    @classmethod
    def validate_grounding(
        cls, decision: PortfolioDecision, *, analyses: list[TechAnalysisResult],
        positions: list[Position], news_intel: NewsIntelligenceReport | None,
        earnings_analyses: list[dict], macro_analysis: dict | None,
        total_value: float, symbol_sectors: dict[str, str] | None = None,
        session_type: str = "morning",
    ) -> list[str]:
        """Validate only machine-readable claims against the prompt registry.

        Prompt and validator now consume the exact same canonical records.
        This removes the former impossible contract (decorated display text
        versus undecorated validation values) and brittle regex interpretation
        of free-form narrative while retaining phantom-exit, source-existence,
        exact-stance, uniqueness, relationship, and alignment checks.
        """

        errors: list[str] = []
        if decision.decisions:
            errors.append(
                "portfolio manager supplied concrete decisions; only grounded targets "
                "may cross the PM boundary"
            )
        held = {p.symbol.upper(): p for p in positions}
        registry = cls.build_evidence_registry(
            analyses=analyses, positions=positions, news_intel=news_intel,
            earnings_analyses=earnings_analyses, macro_analysis=macro_analysis,
            symbol_sectors=symbol_sectors or {}, session_type=session_type,
        )
        bullish = {
            "strong_buy", "buy", "bullish", "positive", "risk_on",
            "overweight", "favorable",
        }
        bearish = {
            "strong_sell", "sell", "bearish", "negative", "risk_off",
            "underweight", "unfavorable",
        }
        reasoning_text = "\n".join(
            str(value) for value in decision.reasoning_chain.model_dump().values()
        )

        for target in decision.targets:
            symbol = target.symbol.upper()
            pos = held.get(symbol)
            if target.target_weight_pct <= 0 and pos is None:
                errors.append(f"{symbol}: close/exit target is not an actual holding")
            if not target.provenance:
                errors.append(f"{symbol}: target has no structured specialist provenance")
                continue

            current_weight = 0.0
            if pos is not None and total_value > 0:
                current_weight = pos.market_value * _gross_multiplier(symbol) / total_value * 100
            intent = "buy" if target.target_weight_pct > current_weight + 0.01 else "sell"
            expected_sources = registry.get(symbol, {})
            seen_sources: set[str] = set()
            supporting_sources: set[str] = set()
            for claim in target.provenance:
                source = claim.source
                stance = claim.observed_stance.strip().lower().replace(" ", "_")
                expected = expected_sources.get(source)
                if expected is None:
                    errors.append(f"{symbol}: claims {source} coverage that does not exist")
                    continue
                if stance != expected:
                    errors.append(
                        f"{symbol}: claims {source} stance {stance!r}; canonical "
                        f"stance is {expected!r}"
                    )
                    continue
                if source in seen_sources:
                    errors.append(f"{symbol}: duplicate {source} provenance claim")
                    continue
                seen_sources.add(source)

                stance_is_bullish = stance in bullish
                stance_is_bearish = stance in bearish
                if source == "macro" and _effective_multiplier(symbol) < 0:
                    # A risk-off macro view supports owning an inverse ETF;
                    # the technical rating still describes the ETF itself.
                    stance_is_bullish, stance_is_bearish = stance_is_bearish, stance_is_bullish
                polarity_supports = (
                    (intent == "buy" and stance_is_bullish)
                    or (intent == "sell" and stance_is_bearish)
                )
                if claim.relationship == "supports":
                    if not polarity_supports:
                        errors.append(
                            f"{symbol}: {source} stance {stance!r} does not support "
                            f"the proposed {intent}; record a conflict or context"
                        )
                    else:
                        supporting_sources.add(source)
                elif claim.relationship == "conflicts" and polarity_supports:
                    errors.append(
                        f"{symbol}: {source} stance {stance!r} supports the proposed "
                        f"{intent}; it cannot be labelled conflicts"
                    )
                elif claim.relationship == "context" and stance not in {"neutral", "mixed"}:
                    errors.append(
                        f"{symbol}: directional {source} stance {stance!r} must be "
                        "marked supports or conflicts, not context"
                    )

            # Dynamic N/M alignment is valid only when M is the number of
            # sources actually available for this symbol in the registry.
            texts = [target.thesis]
            texts.extend(
                m.group(0) for m in re.finditer(
                    rf"\b{re.escape(symbol)}\b[^.\n]{{0,240}}\b\d+/\d+\b",
                    reasoning_text, flags=re.IGNORECASE,
                )
            )
            for text in texts:
                for match in re.finditer(r"\b(\d+)/(\d+)\b", text):
                    stated_support, stated_available = map(int, match.groups())
                    available_sources = set(expected_sources)
                    if stated_available != len(available_sources):
                        errors.append(
                            f"{symbol}: claims denominator {stated_available}, but "
                            f"{len(available_sources)} current source(s) are available"
                        )
                    elif seen_sources != available_sources:
                        errors.append(
                            f"{symbol}: alignment shorthand requires provenance for all "
                            f"available sources {sorted(available_sources)!r}"
                        )
                    elif stated_support != len(supporting_sources):
                        errors.append(
                            f"{symbol}: claims {stated_support}/{stated_available} aligned "
                            f"but provenance proves {len(supporting_sources)}/{stated_available}"
                        )
        return errors

    @staticmethod
    def _drop_invalid_targets(parsed: dict) -> dict:
        """Pre-validate each TargetPosition; drop malformed entries with a
        warning naming the symbol (or list index when missing).

        Mutates parsed in place for `targets`. Non-list shapes normalize to
        []. The TargetPosition validators stay strict (target_weight_pct
        must be in [0, 25], symbol normalised) — we just stop letting one
        bad row weaponize that strictness against the rest of the book.
        """
        raw = parsed.get("targets")
        if raw is None:
            return parsed
        if not isinstance(raw, list):
            logger.warning(
                "Portfolio manager: targets is %s, not list — replacing with []",
                type(raw).__name__,
            )
            parsed["targets"] = []
            return parsed
        valid: list[dict] = []
        for i, item in enumerate(raw):
            if not isinstance(item, dict):
                logger.warning(
                    "Portfolio manager: dropping non-dict targets entry "
                    "at index %d: %r", i, item,
                )
                continue
            try:
                TargetPosition(**item)
            except ValidationError as e:
                sym = item.get("symbol") or f"<idx {i}>"
                logger.warning(
                    "Portfolio manager: dropping malformed target for %s: %s",
                    sym, e,
                )
                continue
            valid.append(item)
        parsed["targets"] = valid
        return parsed

    _DECISION_FIELDS = ("targets",)

    @staticmethod
    def _canonical_targets(targets) -> list[tuple] | None:
        """Full TargetPosition decision payload (symbol, target_weight_pct,
        conviction, thesis, thesis_invalid_if, suggested_stop_price,
        catalyst), order-insensitive. Built by re-validating each entry
        through the `TargetPosition` model itself — its own field
        normalization (symbol case, conviction case, numeric coercion)
        is the single source of truth for what "the same value" means,
        rather than a second, ad-hoc coercion path that can drift out of
        sync with the schema (or hide a real change behind a bug, as the
        prior `round(float(...))`-only / symbol+weight-only comparison
        did). Returns None — never `==` to anything, including itself —
        when the shape doesn't validate, so a malformed side fails closed
        instead of comparing (incorrectly) equal.
        """
        if targets is None:
            targets = []
        if not isinstance(targets, list):
            return None
        models: list[TargetPosition] = []
        for t in targets:
            if not isinstance(t, dict):
                return None
            try:
                models.append(TargetPosition(**t))
            except Exception:  # noqa: BLE001 — any shape failure fails closed
                return None
        return sorted(
            (
                (
                    m.symbol, m.target_weight_pct, m.conviction, m.thesis,
                    m.thesis_invalid_if, m.suggested_stop_price, m.catalyst,
                )
                for m in models
            ),
            key=lambda row: row[0],
        )

    @classmethod
    def _decision_fields_unchanged(cls, original: dict, repaired: dict) -> bool:
        """True iff the ENTIRE target set — every field of every
        TargetPosition, not just symbol/weight — survived a schema repair
        unchanged. `original` and `repaired` are both already post-
        `_drop_invalid_targets` for a fair comparison."""
        orig = cls._canonical_targets(original.get("targets"))
        rep = cls._canonical_targets(repaired.get("targets"))
        if orig is None or rep is None:
            return False
        return orig == rep
