import logging
from pathlib import Path

from pydantic import ValidationError

from src.agents.base import BaseAgent
from src.models import (
    NewsIntelligenceReport, PortfolioDecision, Position, RiskModification,
    RiskVerdict, TechAnalysisResult,
)
from src.risk.rules import RiskViolation

logger = logging.getLogger(__name__)

PROMPT_PATH = Path(__file__).parent.parent.parent / "config" / "prompts" / "risk_manager.md"


def _fmt_or_na(value, suffix: str = "") -> str:
    """Render a macro metric, falling back to 'N/A' when the provider
    returned None (FRED outage). The macro provider always ships every
    key with None values on failure, so `.get(key, 'N/A')` defaults never
    fire — the prompt was literally rendering 'VIX: None' / 'inverted:
    None' on outage days (audit round 2 #34)."""
    return "N/A" if value is None else f"{value}{suffix}"


class RiskManagerAgent(BaseAgent):
    @property
    def name(self) -> str:
        return "risk_manager"

    @property
    def system_prompt(self) -> str:
        if PROMPT_PATH.exists():
            return PROMPT_PATH.read_text()
        return "You are a risk manager. Respond with JSON."

    def build_user_message(self, **kwargs) -> str:
        portfolio_decision: PortfolioDecision = kwargs["portfolio_decision"]
        positions: list[Position] = kwargs["positions"]
        macro_summary: dict = kwargs["macro_summary"]
        rule_violations: list[RiskViolation] = kwargs["rule_violations"]
        tech_analyses: list[TechAnalysisResult] = kwargs.get("tech_analyses", []) or []
        news_intel: NewsIntelligenceReport | None = kwargs.get("news_intel")
        earnings_analyses: list[dict] = kwargs.get("earnings_analyses", []) or []
        total_value: float | None = kwargs.get("total_value")
        cash: float | None = kwargs.get("cash")
        # Cash-equivalent sweep reserve (SGOV), reported separately from
        # `cash` — 2026-08-19 SGOV/deployable-liquidity forensic. `cash` is
        # already the truthful immediately-deployable figure; never re-add
        # this to it (that is the exact bug that let the hard gate approve
        # BUYs execution couldn't fund).
        reserve_balance: float = kwargs.get("reserve_balance", 0.0) or 0.0
        # 2026-08-13 agent audit — "risk evidence completeness". RM's prompt
        # claims it enforces PM's holding-discipline and drawdown-halve rules,
        # but neither input reached it: Position carries no entry date, and
        # `in_drawdown` lived only in PM's facts block. Both are optional so
        # every existing call site keeps working; absent, the sections render
        # as "not provided" rather than silently reading as "no drawdown" /
        # "no position is young".
        position_history: dict = kwargs.get("position_history") or {}
        recent_performance: dict = kwargs.get("recent_performance") or {}

        # audit round 2 #6: allocation_pct has TWO meanings — %-of-portfolio
        # for BUY vs %-of-current-position for SELL (100 = full close,
        # 0 = skip). Rendering both with the same "% allocation" template
        # made the RM misread SELL fractions as portfolio weights and emit
        # allocation_pct mods that silently downgraded PM-sized exits.
        def _fmt_decision(d) -> str:  # d: TradeDecision
            if d.action == "SELL":
                alloc = (
                    f"sell {d.allocation_pct}% OF CURRENT POSITION "
                    f"(100 = full close; NOT a portfolio weight — never set to 0, 0 = skip)"
                )
            else:
                alloc = f"{d.allocation_pct}% of portfolio"
            return (
                f"- {d.action} {d.symbol}: {alloc} | Entry: ${d.entry_price} | "
                f"Stop: ${d.stop_loss} | Target: ${d.take_profit}\n  Reasoning: {d.reasoning}"
            )

        decisions_text = "\n".join(
            _fmt_decision(d) for d in portfolio_decision.decisions
        )

        # audit round 2 #5: RM's rr_audit / sizing_sanity / concentration
        # checks were running blind — no equity, no cash, no per-position
        # weights. When the caller doesn't pass total_value, approximate the
        # denominator with the sum of listed position values (understates
        # true equity by the cash balance — flagged in the header).
        approx_book = sum(p.market_value for p in positions) if positions else 0.0
        denom = total_value if (total_value or 0) > 0 else approx_book
        if (total_value or 0) > 0:
            cash_bit = ""
            if cash is not None:
                cash_pct = (cash / total_value * 100) if total_value else 0.0
                cash_bit = f" | Cash (deployable this session): ${cash:,.0f} ({cash_pct:.1f}%)"
            if reserve_balance > 0:
                cash_bit += (
                    f" (incl. ${reserve_balance:,.0f} sweep-parked, "
                    f"auto-liquidated before any BUY executes)"
                )
            account_section = (
                f"## Account\n- Total equity: ${total_value:,.0f}{cash_bit}\n"
            )
        elif approx_book > 0:
            account_section = (
                f"## Account\n- Total book (approx = sum of listed positions; "
                f"broker equity not provided, so weights below slightly "
                f"overstate true %-of-equity): ${approx_book:,.0f}\n"
            )
        else:
            # No equity and no positions — the drawdown line below still needs
            # a header to hang off, so open the section anyway.
            account_section = "## Account\n"

        # System-drawdown state. PM's sizing formula multiplies every new BUY
        # by 0.5 when `in_drawdown` is true; without this block RM had no way
        # to tell whether that halving happened, so a PM that skipped it was
        # unauditable. Rendered inside the Account section because it is a
        # property of the account, not of any one name.
        if recent_performance:
            r5 = recent_performance.get("rolling_5d_pct")
            r20 = recent_performance.get("rolling_20d_pct")
            in_dd = bool(recent_performance.get("in_drawdown"))
            trailing = recent_performance.get("trailing_days")
            sample_bit = (
                f", {trailing} trailing sessions" if trailing is not None else ""
            )
            account_section += (
                f"- System performance: 5d {_fmt_or_na(r5, '%')} | "
                f"20d {_fmt_or_na(r20, '%')} | "
                f"in_drawdown={str(in_dd).lower()}{sample_bit}\n"
            )
            if in_dd:
                account_section += (
                    "  ⚠️ in_drawdown=true — PM's sizing rule REQUIRES every new "
                    "BUY halved (×0.5) and the halving named in `sizing_logic`. "
                    "Verify it against the proposed sizes; this rule has no "
                    "deterministic enforcement, so you are the only check.\n"
                )
        else:
            account_section += (
                "- System performance: not provided "
                "(cannot audit the drawdown-halve rule this run)\n"
            )

        def _fmt_position(p: Position) -> str:
            weight_bit = ""
            if denom > 0:
                weight_bit = (
                    f" | Value: ${p.market_value:,.0f} "
                    f"({p.market_value / denom * 100:.1f}% of book)"
                )
            # days_held drives PM's tiered holding discipline (<5d protection
            # period, 5-15d maturity, >15d). RM has to see the tier to tell a
            # disciplined exit from a day-3 panic sell.
            hist = position_history.get(p.symbol) or {}
            days = hist.get("days_held")
            if days is None:
                age_bit = " | held: unknown"
            elif days < 5:
                age_bit = f" | held: {days}d (<5d PROTECTED — SELL needs a named trigger)"
            elif days <= 15:
                age_bit = f" | held: {days}d (5-15d maturity)"
            else:
                age_bit = f" | held: {days}d (>15d)"
            return (
                f"- {p.symbol}: {p.qty} shares @ ${p.avg_entry:.2f} | "
                f"Current: ${p.current_price:.2f} | P&L: ${p.unrealized_pnl:.2f}"
                f"{weight_bit}{age_bit} | Sector: {p.sector}"
            )

        positions_text = "\n".join(
            _fmt_position(p) for p in positions
        ) if positions else "No current positions."

        violations_text = "\n".join(
            f"- VIOLATION [{v.rule}]: {v.message} (value: {v.value}, limit: {v.limit})"
            for v in rule_violations
        ) if rule_violations else "No hard rule violations detected."

        vix = macro_summary.get("vix", {}) or {}
        treasury = macro_summary.get("treasury", {}) or {}
        fed_funds_obj = macro_summary.get("fed_funds_rate", {}) or {}
        # Backward-compat: fed_funds_rate was previously a float; now a dict.
        if isinstance(fed_funds_obj, (int, float)):
            fed_funds = fed_funds_obj
        else:
            fed_funds = fed_funds_obj.get("current")

        # PM reasoning chain (if available).
        #
        # 2026-08-13 agent audit — two findings land here.
        #
        # "risk evidence completeness": risk_manager.md tells RM it reads a
        # NINE-field chain and names `continuity_check` / `premortem_check`
        # explicitly. This renderer emitted seven. The two it dropped are the
        # two the schema defaults to "" (ReasoningChain, src/models.py), i.e.
        # exactly the two that can go missing without any parse error — so the
        # only reviewer positioned to notice was the one not being shown them.
        #
        # "premortem/observability": an absent field is rendered as an explicit
        # [MISSING] line rather than omitted. A silently absent section reads
        # to RM as "PM had nothing to say"; a [MISSING] marker reads as "the
        # mandatory step did not happen", which is a finding RM can act on.
        rc = portfolio_decision.reasoning_chain
        if rc:
            def _field(label: str, value: str, *, mandatory_prompt_only: bool = False) -> str:
                text = (value or "").strip()
                if text:
                    return f"- {label}: {text}"
                if mandatory_prompt_only:
                    return (
                        f"- {label}: [MISSING — this field is MANDATORY in PM's "
                        f"prompt but optional in the schema, so PM returning it "
                        f"empty raises no parse error. Treat the audit step as "
                        f"NOT PERFORMED and say so in `reasoning_chain.overall`.]"
                    )
                return f"- {label}: [EMPTY]"

            reasoning_section = "\n".join([
                "## PM Reasoning Chain — PM's CLAIMS about its own plan, not evidence",
                "",
                "Audit these against the blocks above. Where a claim cites a number,",
                "check it against the Account / Positions / Tech / Macro data you were",
                "given; where you cannot check it, say so rather than accepting it.",
                "",
                _field("Macro filter", rc.macro_filter),
                _field("News check", rc.news_check),
                _field("Earnings check", rc.earnings_check),
                _field("Signal conflicts", rc.signal_conflicts),
                _field("Sizing logic", rc.sizing_logic),
                _field("Portfolio balance", rc.portfolio_balance),
                _field("Cash target", rc.cash_target),
                _field("Continuity check", rc.continuity_check,
                       mandatory_prompt_only=True),
                _field("Pre-mortem check", rc.premortem_check,
                       mandatory_prompt_only=True),
                "",
            ])
        else:
            reasoning_section = (
                "## PM Reasoning Chain\n"
                "(not provided — PM emitted no audit trail at all. Its plan is "
                "unaudited by construction; weigh that in your verdict.)\n"
            )

        # Tech Analyst Signals — lets RM audit PM's fidelity AND enforce R/R discipline.
        if tech_analyses:
            tech_lines = []
            for a in tech_analyses:
                rr = getattr(a, "risk_reward", None)
                rr_str = f"R/R {rr:.2f}:1" if rr is not None else "R/R n/a"
                price_str = f"entry ${a.entry_price}, stop ${a.stop_loss}" if a.entry_price else "no prices"
                tech_lines.append(
                    f"- {a.symbol}: {a.rating} ({a.conviction}) | {rr_str} | {price_str} — {a.reasoning[:120]}"
                )
            tech_section = "## Tech Analyst Signals (cross-check PM's decisions + R/R discipline)\n" + "\n".join(tech_lines)
        else:
            tech_section = "## Tech Analyst Signals\n(not provided)"

        # News intelligence — RM needs it to catch silent contradictions between
        # PM's proposals and today's news (e.g., BUY energy on a ceasefire day).
        if news_intel:
            conv_order = {"high": 0, "medium": 1, "low": 2}
            state_lines = [
                f"- [{c.conviction.upper()}] {c.event}: {c.previous_state} → {c.new_state} "
                f"(impact: {c.market_impact}; affects: {', '.join(c.affected_symbols[:5]) or 'broad'})"
                for c in (news_intel.state_changes or [])[:5]
            ]
            state_text = "\n".join(state_lines) or "No HIGH/MED state changes today."
            # Alerts on symbols PM is trading
            trade_syms = {d.symbol for d in portfolio_decision.decisions}
            alert_lines = []
            for sym, alerts in (news_intel.stock_news or {}).items():
                if sym not in trade_syms:
                    continue
                for a in sorted(alerts, key=lambda x: conv_order.get(x.conviction, 9))[:2]:
                    alert_lines.append(
                        f"- {sym}: [{a.conviction.upper()}] {a.sentiment} — {a.impact_summary}"
                    )
            alerts_text = "\n".join(alert_lines) or "No alerts on traded symbols."
            news_section = f"""## News Intelligence (use to verify PM hasn't contradicted today's events)
PM Briefing: {news_intel.pm_briefing[:300]}

State changes today:
{state_text}

Alerts on PM's traded symbols:
{alerts_text}

Overall sentiment: {news_intel.market_sentiment} ({news_intel.confidence})
"""
        else:
            news_section = "## News Intelligence\n(not provided)\n"

        # Earnings — placeholders for queued filings flag event risk on those names.
        if earnings_analyses:
            earn_lines = []
            for ea in earnings_analyses:
                sym = ea.get("symbol", "?")
                if ea.get("queued"):
                    earn_lines.append(
                        f"- {sym}: [JUST FILED {ea.get('form_type','?')} {ea.get('filing_date','?')} — "
                        f"ANALYSIS PENDING; cap BUY ≤ 5%]"
                    )
                else:
                    analysis = ea.get("analysis") or {}
                    impl = analysis.get("investment_implications") or {}
                    earn_lines.append(
                        f"- {sym}: {impl.get('sentiment','?')} ({impl.get('conviction','?')}) — "
                        f"{impl.get('key_thesis','')[:120]}"
                    )
            earnings_section = "## Earnings (verify PM respected queued-filing cap)\n" + "\n".join(earn_lines) + "\n"
        else:
            earnings_section = ""

        # 2026-08-13 agent audit — "PM/Risk independence". PM's reasoning chain
        # used to be the FIRST thing in this message, so RM read PM's case for
        # the plan before it saw a single primary number and then graded the
        # story rather than the book. The blocks are now ordered
        #
        #   what PM proposes -> the account/market facts -> PM's claims about
        #   them -> the deterministic engine's findings -> verdict
        #
        # so RM forms its own read from primary data first, and the last input
        # before the verdict is the one input PM did not author. Nothing was
        # added to or removed from what RM may DO about a disagreement — no
        # threshold moved; only the order in which it learns things.
        return f"""## Proposed Trades
{decisions_text}

Portfolio View: {portfolio_decision.portfolio_view}

{account_section}
## Current Positions
{positions_text}

{tech_section}

{news_section}
{earnings_section}## Macro Context
- VIX: {_fmt_or_na(vix.get('current'))} (5d avg: {_fmt_or_na(vix.get('mean_5d'))}, trend: {_fmt_or_na(vix.get('trend'))})
- 2Y Treasury: {_fmt_or_na(treasury.get('us2y'), '%')}
- 10Y Treasury: {_fmt_or_na(treasury.get('us10y'), '%')}
- 2Y-10Y Spread: {_fmt_or_na(treasury.get('spread_2_10'), '%')} (inverted: {_fmt_or_na(treasury.get('inverted'))})
- Fed Funds Rate: {_fmt_or_na(fed_funds, '%')}

{reasoning_section}
## Hard Risk Rule Check Results
{violations_text}

Review these proposed trades and provide your verdict as JSON."""

    def review(self, portfolio_decision: PortfolioDecision, positions: list[Position],
               macro_summary: dict, rule_violations: list[RiskViolation],
               tech_analyses: list[TechAnalysisResult] | None = None,
               news_intel: NewsIntelligenceReport | None = None,
               earnings_analyses: list[dict] | None = None,
               total_value: float | None = None,
               cash: float | None = None,
               reserve_balance: float = 0.0,
               position_history: dict | None = None,
               recent_performance: dict | None = None) -> tuple[RiskVerdict | None, "AgentResult"]:
        # audit round 2 #5: total_value / cash are optional so existing call
        # sites keep working; when omitted, build_user_message approximates
        # the book denominator from the sum of position market values.
        # 2026-08-13 audit: position_history / recent_performance are optional
        # for the same reason — they carry the `days_held` and `in_drawdown`
        # evidence RM needs to audit PM's holding-discipline and drawdown-halve
        # rules, and their absence is rendered explicitly rather than assumed
        # benign.
        result = self.run(
            portfolio_decision=portfolio_decision,
            positions=positions,
            macro_summary=macro_summary,
            rule_violations=rule_violations,
            tech_analyses=tech_analyses or [],
            news_intel=news_intel,
            earnings_analyses=earnings_analyses or [],
            total_value=total_value,
            cash=cash,
            reserve_balance=reserve_balance,
            position_history=position_history or {},
            recent_performance=recent_performance or {},
        )
        parsed = result.parse_json()
        if parsed is None:
            logger.error("Risk manager returned non-JSON response")
            return None, result
        # Per-entry isolation for modifications: a single malformed
        # RiskModification (e.g. non-numeric original_value, wrong field
        # name) must not drop the whole RiskVerdict. The verdict carries
        # `approved`, `reasoning_chain`, `scale_all_buys`, `reason_category`,
        # plus the OTHER modifications — losing all of that because one
        # mod row is bad means execution stage has no RM guidance and
        # PM's calibration history loses a row. Mirrors PR #74 pattern.
        if isinstance(parsed, dict):
            parsed = self._drop_invalid_modifications(parsed)
        try:
            return RiskVerdict(**parsed), result
        except ValidationError as e:
            # 2026-08-18 incident: an APPROVING verdict with three sound
            # modifications died because two reasoning_chain prose fields
            # were omitted — recorded as "REJECTED: parse error", trading
            # day over. One bounded repair reprompt names the exact
            # validation errors; a second failure keeps the fail-closed
            # None → reject path exactly as before.
            repaired = self.repair_reprompt(result, e, "RiskVerdict")
            reparsed = repaired.parse_json()
            if isinstance(reparsed, dict):
                reparsed = self._drop_invalid_modifications(reparsed)
                try:
                    verdict = RiskVerdict(**reparsed)
                    logger.info(
                        "Risk verdict repair succeeded (approved=%s, %d mods)",
                        verdict.approved, len(verdict.modifications),
                    )
                    return verdict, repaired
                except Exception as e2:  # noqa: BLE001
                    logger.error(
                        "Failed to parse risk verdict after repair: %s", e2,
                    )
                    return None, repaired
            logger.error(
                "Risk verdict repair returned %s, not an object",
                type(reparsed).__name__,
            )
            return None, repaired
        except Exception as e:
            logger.error("Failed to parse risk verdict: %s", e)
            return None, result

    @staticmethod
    def _drop_invalid_modifications(parsed: dict) -> dict:
        """Pre-validate each RiskModification; drop malformed entries with a
        warning naming the symbol (or list index when missing).

        Mutates parsed in place for `modifications`. Non-list shapes
        normalize to []. Mirrors EveningAnalyst._drop_invalid_missed_opportunities
        (PR #73) and the news/position_reviewer/meta_reflector pattern (PR #74).
        """
        raw = parsed.get("modifications")
        if raw is None:
            return parsed
        if not isinstance(raw, list):
            logger.warning(
                "Risk manager: modifications is %s, not list — replacing with []",
                type(raw).__name__,
            )
            parsed["modifications"] = []
            return parsed
        valid: list[dict] = []
        for i, item in enumerate(raw):
            if not isinstance(item, dict):
                logger.warning(
                    "Risk manager: dropping non-dict modifications entry "
                    "at index %d: %r", i, item,
                )
                continue
            try:
                RiskModification(**item)
            except ValidationError as e:
                sym = item.get("symbol") or f"<idx {i}>"
                logger.warning(
                    "Risk manager: dropping malformed modification for %s: %s",
                    sym, e,
                )
                continue
            valid.append(item)
        parsed["modifications"] = valid
        return parsed
