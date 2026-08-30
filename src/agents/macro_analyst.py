import json
import logging
from pathlib import Path

from pydantic import ValidationError

from src.agents.base import BaseAgent, AgentResult
from src.models import MacroAnalysis, MacroObservation

logger = logging.getLogger(__name__)

PROMPT_PATH = Path(__file__).parent.parent.parent / "config" / "prompts" / "macro_analyst.md"


class MacroAnalystAgent(BaseAgent):
    @property
    def name(self) -> str:
        return "macro_analyst"

    @property
    def system_prompt(self) -> str:
        if PROMPT_PATH.exists():
            return PROMPT_PATH.read_text()
        return "You are a macro analyst. Respond with JSON."

    def build_user_message(self, **kwargs) -> str:
        macro_summary: dict = kwargs["macro_summary"]
        universe: list[str] = kwargs.get("universe", [])
        last_state: dict | None = kwargs.get("last_state")
        news_narrative: dict | None = kwargs.get("news_narrative")
        macro_coverage = kwargs.get("macro_coverage")

        vix = macro_summary.get("vix", {}) or {}
        treasury = macro_summary.get("treasury", {}) or {}
        fed = macro_summary.get("fed_funds_rate", {}) or {}
        infl = macro_summary.get("inflation", {}) or {}
        une = macro_summary.get("unemployment", {}) or {}
        hy = macro_summary.get("credit_spread", {}) or {}
        # Phase 4.2 additions (docs/AGENT_ROLE_AUDIT.md §2.3) — see the
        # "Guardrails" note below on how these are scoped relative to the
        # six PRIMARY confidence-calibration indicators above.
        real_rates = macro_summary.get("real_rates", {}) or {}
        dollar = macro_summary.get("dollar_index", {}) or {}
        ig = macro_summary.get("ig_credit_spread", {}) or {}
        claims = macro_summary.get("jobless_claims", {}) or {}

        def _stale(d: dict, monthly: bool = False, weekly: bool = False) -> str:
            """Per-cadence staleness label.

            Daily series (VIX, yields, DFF, HY OAS): >3 business days
            without a print is genuinely stale. Monthly series (CPI/PCE,
            UNRATE) are indexed at the reference-month START and released
            weeks later — their staleness_days runs 20-51 business days
            when the data is the freshest print that EXISTS. Labeling
            that "(stale 36d)" taught the model to treat normal BLS/BEA
            cadence as degraded data: 2026-08-18..20 production runs
            cited "stale inflation figures" among the reasons for
            low-confidence / 55%-cash guidance. Monthly series are only
            flagged once a release cycle has actually been missed.

            weekly=True (ICSA, and DTWEXBGS which publishes with a similar
            lag despite being a daily-index series) uses a >10 business-day
            threshold — live-verified 2026-08-30: ICSA's latest print
            trailed by ~6 business days and DTWEXBGS's by ~7, both NORMAL
            release cadence, not staleness.
            """
            s = d.get("staleness_days")
            if not isinstance(s, int):
                return ""
            if monthly:
                return f" (stale {s}d — release cycle missed)" if s > 55 else ""
            if weekly:
                return f" (stale {s}d)" if s > 10 else ""
            return f" (stale {s}d)" if s > 3 else ""

        universe_text = ", ".join(universe) if universe else "N/A"

        prior_state_section = "## Yesterday's Macro State\nNo prior state on file (first run)."
        if last_state:
            prior_state_section = f"""## Yesterday's Macro State (for shift detection)
- Date: {last_state.get('date', 'N/A')}
- Regime: {last_state.get('regime', 'N/A')}
- Confidence: {last_state.get('confidence', 'N/A')}
- Equity outlook: {last_state.get('equity_outlook', 'N/A')}
- Prior summary: {last_state.get('summary', 'N/A')}"""

        news_section = "## Yesterday's News Narrative\nNot available."
        if news_narrative:
            tracker = news_narrative.get("key_state_tracker", {}) or {}
            tracker_text = "\n".join(f"  - {k}: {v}" for k, v in tracker.items()) or "  (empty)"
            news_section = f"""## Yesterday's News Narrative (cross-reference)
- Regime: {news_narrative.get('current_regime', 'N/A')}
- Era themes: {'; '.join(news_narrative.get('era_themes', []) or []) or 'N/A'}
- State tracker:
{tracker_text}"""

        coverage_section = "## Macro Data Coverage\nUNKNOWN (caller did not report FRED coverage). Treat with the same caution as a reported gap."
        if macro_coverage is not None:
            coverage_section = f"## Macro Data Coverage\n{macro_coverage.describe()}"

        return f"""{coverage_section}

## Current Macro Indicators

### VIX (CBOE Volatility Index){_stale(vix)}
- Current: {vix.get('current', 'N/A')}
- 5-day Average: {vix.get('mean_5d', 'N/A')}
- Trend: {vix.get('trend', 'N/A')}

### Treasury Yields{_stale(treasury)}
- 3-Month: {treasury.get('us3mo', 'N/A')}%
- 2-Year: {treasury.get('us2y', 'N/A')}%
- 10-Year: {treasury.get('us10y', 'N/A')}%
- 2Y-10Y Spread: {treasury.get('spread_2_10', 'N/A')}%
- Inverted (2Y/10Y): {treasury.get('inverted', 'N/A')}
- 3M-10Y Spread: {treasury.get('spread_3m_10y', 'N/A')}%
- Inverted (3M/10Y): {treasury.get('inverted_3m_10y', 'N/A')}

### Fed Funds Rate (DFF, daily){_stale(fed)}
- Current: {fed.get('current', 'N/A')}%
- 30-day change: {fed.get('change_30d', 'N/A')}

### Inflation{_stale(infl, monthly=True)}
- Headline CPI YoY: {infl.get('headline_cpi_yoy', 'N/A')}% (MoM: {infl.get('headline_cpi_mom', 'N/A')}%)
- Core CPI YoY: {infl.get('core_cpi_yoy', 'N/A')}% (MoM: {infl.get('core_cpi_mom', 'N/A')}%)
- PCE YoY: {infl.get('pce_yoy', 'N/A')}%

### Real 10Y Yield & Breakeven Inflation (DFII10, T10YIE){_stale(real_rates)}
- Real 10Y Yield: {real_rates.get('real_10y', 'N/A')}%
- 10Y Breakeven Inflation: {real_rates.get('breakeven_10y', 'N/A')}%

### Unemployment (UNRATE){_stale(une, monthly=True)}
- Current: {une.get('current', 'N/A')}%
- Change 3m: {une.get('change_3m', 'N/A')}pp
- Change 12m: {une.get('change_12m', 'N/A')}pp

### Initial Jobless Claims (ICSA, weekly){_stale(claims, weekly=True)}
- Current: {claims.get('current', 'N/A')}
- 4-week change: {claims.get('change_4w', 'N/A')}
- Trend: {claims.get('trend', 'N/A')}

### HY Credit Spread (BAMLH0A0HYM2){_stale(hy)}
- Current: {hy.get('current_bps', 'N/A')}bps
- 30-day change: {hy.get('change_30d_bps', 'N/A')}bps

### IG Credit Spread (BAMLC0A0CM){_stale(ig)}
- Current: {ig.get('current_bps', 'N/A')}bps
- 30-day change: {ig.get('change_30d_bps', 'N/A')}bps

### Dollar Index (DTWEXBGS, Fed Broad Nominal){_stale(dollar, weekly=True)}
- Current: {dollar.get('current', 'N/A')}
- 30-day change: {dollar.get('change_30d', 'N/A')}

{prior_state_section}

{news_section}

## Trading Universe
{universe_text}

Walk through the 6-step reasoning chain, then emit the full JSON schema (including reasoning_chain, regime_shift, triggers, alignment_with_news)."""

    def analyze(
        self,
        macro_summary: dict,
        universe: list[str] | None = None,
        last_state: dict | None = None,
        news_narrative: dict | None = None,
        macro_coverage=None,
    ) -> tuple[MacroAnalysis | None, AgentResult]:
        """Run LLM, validate via Pydantic, return the typed object.

        Phase 4 #7: returns MacroAnalysis instead of dict. Consumers that
        need dict form (PM's rendering, macro_store serialization) call
        .model_dump() at their boundary.

        `macro_coverage` (src.data.macro.MacroCoverage, optional) is
        Phase 4.2: how many of the configured FRED series actually
        returned data this run. Threaded into the prompt's "Macro Data
        Coverage" section — mirrors news_analyst's `news_coverage` kwarg
        exactly. Optional/untyped here (rather than importing
        MacroCoverage) to avoid a src.agents -> src.data import for a
        value only ever used for its .describe() string.
        """
        result = self.run(
            macro_summary=macro_summary,
            universe=universe or [],
            last_state=last_state,
            news_narrative=news_narrative,
            macro_coverage=macro_coverage,
        )
        parsed = result.parse_json()
        if parsed is None:
            logger.error("Macro analyst returned non-JSON response")
            return None, result
        if not isinstance(parsed, dict):
            logger.error("Macro analyst expected object, got %s", type(parsed).__name__)
            return None, result
        # Per-entry isolation for key_observations: a single malformed
        # MacroObservation (e.g. missing `interpretation` field) must not
        # drop the whole MacroAnalysis. The core fields PM relies on
        # (regime / position_guidance / sector_guidance / equity_outlook)
        # are typically clean even when one observation row is mangled.
        # Mirrors EveningAnalyst._drop_invalid_missed_opportunities (PR #73)
        # and the news_analyst / position_reviewer / meta_reflector pattern
        # (PR #74). sector_guidance is already protected by the existing
        # _sanitize_sector_guidance @model_validator on MacroAnalysis.
        parsed = self._drop_invalid_key_observations(parsed)
        try:
            analysis = MacroAnalysis(**parsed)
        except ValidationError as e:
            logger.error("Macro analysis failed validation: %s", e)
            return None, result
        analysis = self._apply_sanity_checks(analysis, macro_summary)
        return analysis, result

    @staticmethod
    def _apply_sanity_checks(
        analysis: MacroAnalysis,
        macro_summary: dict,
    ) -> MacroAnalysis:
        """Soft Python-side floor for two `macro_analyst.md` discipline
        rules the LLM occasionally violates by self-inflating.

        Prompt and code now share the same PER-CADENCE staleness
        semantics (see the Confidence Calibration section of the prompt
        and `_stale()` above): daily series are stale past 3 business
        days; monthly series (CPI/PCE, UNRATE) only once a release cycle
        has been missed (> 55 business days) — their staleness_days runs
        20-51 on perfectly-normal BLS/BEA cadence. We enforce only the
        two most flagrant violations:

        1. `confidence == "high"` requires ALL six primary indicators
           non-null and fresh BY THEIR OWN CADENCE. Any null / stale →
           downgrade to "medium". ("high" is the LLM's most-impactful
           confidence call; PM's Step 1 evening-tilt scales sizing by
           it, so a self-inflated "high" with stale data leaks into
           position size.)

        2. `regime_shift == True` requires ≥ 2 primary indicators with
           `staleness_days <= 1` per the prompt's "Regime-Shift
           Detection" rule. Below that, clear `regime_shift` and
           `shift_reason` — calling a flip on stale data is guessing,
           and PM treats `regime_shift=true` as a "size appropriately
           and name the flip" trigger.

        Logs a warning on each override so the operator can see WHICH
        side of the gate misbehaved (LLM ignored prompt rule vs the
        sanity check fired correctly).
        """
        primary_keys = (
            "vix", "treasury", "fed_funds_rate",
            "inflation", "unemployment", "credit_spread",
        )

        # Build staleness map. Missing dict / non-int staleness → None
        # (treated as "not provably fresh", which fails the high/shift
        # gates). Mirrors the user-message builder's `_stale()` helper
        # which uses the same `isinstance(s, int)` check.
        staleness: dict[str, int | None] = {}
        for key in primary_keys:
            d = macro_summary.get(key)
            if not isinstance(d, dict) or not d:
                staleness[key] = None
                continue
            s = d.get("staleness_days")
            staleness[key] = s if isinstance(s, int) else None

        # Per-cadence staleness thresholds, matching `_stale()` in the
        # user-message builder and the prompt's Confidence Calibration
        # section. Monthly series (CPI/PCE, UNRATE) are indexed at the
        # reference-month start and print weeks later, so their
        # staleness_days runs 20-51 business days when the data is the
        # freshest print that EXISTS. The old flat `> 3` gate therefore
        # made confidence='high' UNREACHABLE in production — every 'high'
        # was silently downgraded on normal BLS/BEA cadence, and PM's
        # evening-tilt sizing never saw a high-confidence macro call.
        _MONTHLY = {"inflation", "unemployment"}
        _monthly_stale_after = 55

        def _is_stale(key: str, v: int | None) -> bool:
            if v is None:
                return True
            return v > (_monthly_stale_after if key in _MONTHLY else 3)

        null_or_stale = [
            k for k, v in staleness.items() if _is_stale(k, v)
        ]

        # Rule 1: high confidence requires every indicator present and
        # fresh by its own cadence.
        if analysis.confidence == "high" and null_or_stale:
            logger.warning(
                "Macro sanity-check: LLM emitted confidence='high' but "
                "indicator(s) %s are stale by their own cadence or null/"
                "missing — downgrading to 'medium' per macro_analyst.md "
                "Confidence Calibration rule.",
                ", ".join(null_or_stale),
            )
            analysis.confidence = "medium"

        # Rule 2: regime_shift requires >= 2 fresh indicators.
        if analysis.regime_shift:
            fresh = [
                k for k, v in staleness.items()
                if isinstance(v, int) and v <= 1
            ]
            if len(fresh) < 2:
                logger.warning(
                    "Macro sanity-check: LLM set regime_shift=True but only "
                    "%d indicator(s) are fresh (staleness_days <= 1)%s — "
                    "clearing regime_shift per macro_analyst.md Regime-Shift "
                    "Detection rule ('shift requires >= 2 fresh indicators').",
                    len(fresh),
                    f" ({', '.join(fresh)})" if fresh else "",
                )
                analysis.regime_shift = False
                analysis.shift_reason = ""

        return analysis

    @staticmethod
    def _drop_invalid_key_observations(parsed: dict) -> dict:
        """Pre-validate each MacroObservation; drop malformed entries with a
        warning naming the indicator (or list index when missing).

        Mutates parsed in place for `key_observations`. Non-list shapes
        normalize to []. The schema's required-field discipline stays —
        we just stop letting one bad row weaponize that strictness against
        the rest of the analysis.
        """
        raw = parsed.get("key_observations")
        if raw is None:
            return parsed
        if not isinstance(raw, list):
            logger.warning(
                "Macro analyst: key_observations is %s, not list — replacing with []",
                type(raw).__name__,
            )
            parsed["key_observations"] = []
            return parsed
        valid: list[dict] = []
        for i, item in enumerate(raw):
            if not isinstance(item, dict):
                logger.warning(
                    "Macro analyst: dropping non-dict key_observations entry "
                    "at index %d: %r", i, item,
                )
                continue
            try:
                MacroObservation(**item)
            except ValidationError as e:
                indicator = item.get("indicator") or f"<idx {i}>"
                logger.warning(
                    "Macro analyst: dropping malformed key_observation %r: %s",
                    indicator, e,
                )
                continue
            valid.append(item)
        parsed["key_observations"] = valid
        return parsed
