import json
import logging
import re
from datetime import date
from pathlib import Path

from pydantic import ValidationError

from src.agents.base import BaseAgent
from src.agents.prompt_limits import LiveLimitPrompt
from src.models import (
    AnalystVerdict, CandidateRejection, EarningsAnalysis, MacroAnalysis,
    NewsIntelligenceReport,
    PortfolioDecision, Position, TargetPosition, TechAnalysisResult,
    SmartMoneyFinding, news_verdict_for_symbol, normalize_sector_stance,
    open_target_missing_falsifier, parse_telemetry,
)
from src.data.news_store import ACTIVE_STATE_CHANGE_WINDOW_DAYS
from src.quantities import collapse_stances
from src.risk.constants import (
    REWARD_RISK_FLOOR,
    STARTER_POSITION_RISK_PCT,
    reward_risk_floor_applies,
)
from src.risk.budget import allocate_risk_budget
from src.risk.metrics import unrealized_pnl_pct
from src.risk.rules import (
    EARNINGS_STANCE_MAX_AGE_DAYS,
    _gross_multiplier,
    book_exposure as _book_exposure,
    own_bar_block_reason,
    own_bar_opposition_reason,
    count_aligned_sources,
    count_opposing_sources,
    position_weight_pct,
    signed_source_score,
    stance_is_aligned,
    weight_pct_of,
)
from src.rotation import (
    RotationOpportunity, RotationPrecheck,
    evaluate_rotation, funding_view_measured, holdings_below_entry_bar,
    rotation_binding_constraints,
)
from src.trading_calendar import et_today
from src.verdicts import RankedCandidate, rank_verdicts

logger = logging.getLogger(__name__)

PROMPT_PATH = Path(__file__).parent.parent.parent / "config" / "prompts" / "portfolio_manager.md"
SETTINGS_PATH = Path(__file__).parent.parent.parent / "config" / "settings.yaml"

# §9.3 — greppable status key for a target dropped over an unadjudicated
# seat conflict, matching the naming convention of Phase 3.3's
# `exit_blocked_no_named_trigger` (src/pipeline.py). Logs and tests key on
# this exact string.
CONFLICT_UNADJUDICATED_STATUS = "pm_conflict_unadjudicated"

# 2026-09-01 (measured 2026-09-02) — greppable status key for the sub-floor
# catalyst gate, same naming convention as the one above. See
# `_apply_subfloor_catalyst_rule` for what it means. Logs and tests key on
# this exact string.
SUBFLOOR_CATALYST_UNVERIFIED_STATUS = "pm_subfloor_catalyst_unverified"

#: One rendered `active_state_changes` row, as
#: `TradingPipeline._build_active_state_changes` emits it:
#:     - [2026-08-31] Anthropic signs a cloud deal with Lambda → NVDA(bullish)
#: The date and the affected-symbol+direction list are the fields the
#: catalyst gate resolves a citation against; the event prose is
#: deliberately NOT matched (see `_catalyst_cites_state_change`).
_STATE_CHANGE_ROW_RE = re.compile(r"^\s*-\s*\[(\d{4}-\d{2}-\d{2})\]\s*(?P<rest>.+)$")

#: One `SYMBOL(direction)` pair inside a state-change row's symbol list.
#: A symbol rendered without a parenthesized direction (legacy format, or
#: a stray comma) does not match and is treated as having no recorded
#: direction — see `_state_change_symbols_by_date`.
_SYMBOL_DIRECTION_RE = re.compile(r"^([A-Z0-9.\-]+)\((\w+)\)$")

#: Any ISO date appearing anywhere in a `catalyst` string. The PM cites a row
#: by its date; the symbol half of the pair is the target's own symbol, which
#: it cannot misstate without the target being about a different name.
_ISO_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")


class PortfolioManagerAgent(LiveLimitPrompt, BaseAgent):
    # This seat SIZES under the desk's limits, so it is shown them rather
    # than told them: `{{risk.*}}` placeholders in
    # `config/prompts/portfolio_manager.md`, rendered from the live config at
    # construction time. Same mechanism as the Risk Manager — see
    # `src/agents/prompt_limits.py`. PM's sheet was CORRECT when the
    # reviewer's went wrong on 2026-09-11, and the reason is NOT that anyone
    # was more careful: `tests/test_prompts_anchors.py` pinned the literal
    # "capped at 65% single-name" here and had no equivalent anchor on the
    # reviewer's sheet. The mechanical check is what held. It held by keeping
    # a THIRD hand-maintained copy of the value, which is what this change
    # removes — the anchor is retargeted to the placeholder.
    _prompt_path = PROMPT_PATH
    _settings_path = SETTINGS_PATH
    _fallback_prompt = "You are a portfolio manager. Respond with JSON."
    # See decide(): parsed JSON is validated as PortfolioDecision(**parsed).
    result_model = PortfolioDecision

    #: Phase 14b. The rotation comparison this agent's LAST prompt was
    #: rendered from (`rotation_precheck`), reset at the top of every
    #: `build_user_message`. `DecisionStage._apply_rotation_execution`
    #: reads it so the desk acts on exactly what the model was shown.
    last_rotation_precheck: RotationPrecheck | None = None

    #: retired board item 49, owner decision 2026-09-12 (`docs/INCIDENT_HISTORY.md`, 2026-09-14) ("best-ranked first").
    #: The candidate ranking this agent's LAST prompt was rendered from, in
    #: `rank_verdicts` order, best first. Reset at the top of every
    #: `build_user_message` exactly like `last_rotation_precheck` above, and
    #: for the same reason: `pipeline_stages.DecisionStage` reads it to tell
    #: the constructor which order to spend the risk budget in, and a stale
    #: ranking from a previous session must never leak into this one. None
    #: means "no ranking view this session" — the allocator then falls back
    #: to its pre-decision ordering rather than having one invented for it.
    last_candidate_ranking: list[RankedCandidate] | None = None

    @property
    def name(self) -> str:
        return "portfolio_manager"

    @staticmethod
    def _collapse_stances(values) -> str | None:
        """Thin wrapper — the reduction itself lives in `src.quantities.
        collapse_stances` now, so `src/models.py::news_verdict_for_symbol`
        (Phase 13) can share the exact same rule without this module
        importing that one (this module already imports `src.models`, so
        the reverse would be circular). See that function's docstring for
        the full rule; this wrapper exists only so every existing call site
        here (`build_evidence_registry`, `_earnings_stance_rows`, ...) keeps
        working unchanged.
        """
        return collapse_stances(values)

    @staticmethod
    def _sector_guidance_rows(raw) -> list[dict]:
        """`sector_guidance`, in either shape, as [{sector, stance, reason}].

        Two shapes reach the PM. The live macro agent emits
        [{sector, stance, reason}, ...] with stance ∈ overweight|neutral|
        underweight; `MacroStore` persists the normalized {sector: direction}
        form (see `macro_store._normalize_sector_guidance`, which drops the
        bulky reasons). Both arrive in normal operation now that an intraday
        tick carries the morning's STORED regime forward, so every reader of
        this field has to handle both — iterating the dict shape as though it
        were a list yields bare strings, and indexing those took the whole PM
        call down.

        Stances come out in ONE vocabulary, the bullish/neutral/bearish
        directions the rest of the system persists and grades against (see
        `SECTOR_STANCE_TO_DIRECTION`). Without that the same macro view
        reached the evidence registry as "overweight" in the morning and
        "bullish" at 14:00, purely by which session read it. Unrecognized
        stances are dropped rather than passed through: a stance no polarity
        set knows can only produce a grounding error the model cannot fix.
        """
        if isinstance(raw, dict):
            pairs = list(raw.items())
        elif isinstance(raw, list):
            pairs = [
                (row.get("sector"), row.get("stance"))
                for row in raw if isinstance(row, dict)
            ]
        else:
            return []
        rows: list[dict] = []
        for sector, stance in pairs:
            direction = normalize_sector_stance(stance)
            if sector and direction:
                rows.append({"sector": str(sector), "stance": direction})
        return rows

    @classmethod
    def _earnings_stance_rows(
        cls, earnings_analyses: list[dict],
    ) -> list[tuple[str, str, str]]:
        """`(SYMBOL, stance, filing_date)` for every earnings entry that
        produces a registry stance, in input order.

        The filter is exactly the one `build_evidence_registry`'s `put`
        applies — a dict `analysis`, a non-empty collapsed sentiment, a
        non-empty symbol — extracted so the freshness gate below and the
        registry itself cannot drift apart about WHICH entry a symbol's
        earnings stance came from. Order is preserved because the registry
        is last-wins per symbol.

        `filing_date` is read from the pipeline wrapper first (the shape
        `run_earnings_preprocess` / `EarningsAnalystAgent.analyze_reports`
        emit) and from the validated analysis second. Empty string when
        neither carries one — an unknowable age, which the gate treats as
        stale.
        """
        rows: list[tuple[str, str, str]] = []
        for item in earnings_analyses:
            analysis = item.get("analysis")
            if not isinstance(analysis, dict):
                continue
            sentiment = (analysis.get("investment_implications") or {}).get("sentiment")
            stance = cls._collapse_stances([sentiment])
            symbol = str(item.get("symbol") or "").strip().upper()
            if not symbol or not stance:
                continue
            filing_date = str(
                item.get("filing_date") or analysis.get("filing_date") or ""
            ).strip()
            rows.append((symbol, stance, filing_date))
        return rows

    @staticmethod
    def _render_earnings_verdict(
        *, sym: str, analysis: dict, filing_label: str, source_note: str,
        analysis_path: str | None,
    ) -> str:
        """Item 18 (2026-09-04): earnings used to courier its whole eight-
        field extraction form into this prompt (~1,400 chars/filing, 70% of
        a 200k-char PM prompt across dozens of filings/day) for one line of
        actual judgement. This renders the SHORT verdict instead — call,
        conviction, a 2-3 sentence thesis, and a pointer to the full record.

        Reuses `EarningsAnalysis.to_verdict()` (Phase 13, `src/verdicts.py`
        ranking shape) rather than inventing a second short-form
        representation — see that method for why direction/conviction are
        verbatim and why the falsifier comes from bear_case/bull_case.

        `to_verdict()` (via `AnalystVerdict`'s own validator) REFUSES to
        construct a directional call with no stated invalidation — a real
        gap surfaced during item 18: PM's evidence needs are lower-stakes
        than the ranking machinery's, so a directional read with no
        disclosed falsifier still belongs in the PM prompt (marked as
        such) rather than being dropped from PM's picture entirely, which
        is what happened to a *ranking* candidate in this situation. Fall
        back to the raw `investment_implications` fields when that happens.
        """
        impl = (analysis or {}).get("investment_implications") or {}
        sentiment = impl.get("sentiment", "neutral")
        conviction = impl.get("conviction", "N/A")
        thesis = (impl.get("key_thesis") or "").strip() or "not disclosed"

        pointer = (
            f"data/earnings/{sym}/... ({filing_label})" if not analysis_path
            else analysis_path
        )

        verdict = None
        try:
            verdict = EarningsAnalysis.model_validate(analysis).to_verdict()
        except Exception:  # noqa: BLE001 — see docstring: fall back to the
            # raw fields rather than dropping this filing from PM's prompt.
            verdict = None

        if verdict is not None:
            falsifier_line = (
                f"- Invalidated if: {verdict.invalidation}" if verdict.invalidation
                else "- Invalidated if: not disclosed by the analyst"
            )
        else:
            falsifier = impl.get("bear_case") if sentiment == "bullish" else impl.get("bull_case")
            falsifier = (falsifier or "").strip()
            falsifier_line = (
                f"- Invalidated if: {falsifier}" if falsifier and falsifier.lower() != "not disclosed"
                else "- Invalidated if: not disclosed by the analyst"
            )

        return (
            f"### {sym} — {filing_label}{source_note}\n"
            f"- Call: {sentiment} ({conviction})\n"
            f"- Thesis: {thesis}\n"
            f"{falsifier_line}\n"
            f"- Full 8-field extraction (metrics, guidance, strategy, risks, "
            f"data quality): {pointer}"
        )

    @staticmethod
    def _render_earnings_no_call_rollup(rows: list[dict]) -> str:
        """One line per filing the earnings seat read WITHOUT reaching a call.

        PM TEST GATE item 7 (2026-09-13). Measured on the frozen
        `run_64290730` fixture rendered through this very method's caller:
        38 of the 65 analysed filings came back `sentiment: neutral`. A
        neutral earnings sentiment is not a quiet bearish read — it is the
        seat declining to conclude, and `EarningsAnalysis.to_verdict()`
        renders no invalidation for one, so the four-line verdict block for
        such a filing carries a direction the PM cannot trade, a thesis the
        seat did not write, and the literal words "not disclosed by the
        analyst". Those 38 blocks were 16,885 of the prompt's 100,968
        characters — 16.7% of everything the decision seat reads, saying
        nothing it can act on.

        WHAT THE CUT IS, AND WHY IT IS NOT A TRUNCATION LIMIT. There is no
        length threshold here and no cap on how many filings survive. The
        partition is structural and read from the data: a filing is rolled
        up if and only if its collapsed sentiment is non-directional. If
        every filing on a given day carries a call, this roll-up is empty
        and nothing is shortened.

        WHAT SURVIVES. Every rolled-up symbol is still named, on its own
        line, with its form, its filing date, its conviction, its cache
        provenance and its staleness marker — so coverage is never
        silently absent and a reader can always tell "read, no call" from
        "never read". The stance itself continues to reach the PM through
        the Canonical Evidence Registry and the Independent Source
        Agreement block unchanged: a neutral earnings seat still counts as
        a non-aligned source and still subtracts from the net score that
        ceilings size. Nothing about dissent or low conviction moves.
        """
        if not rows:
            return ""
        lines = [
            "### Read, no call — {n} filing(s)".format(n=len(rows)),
            "The earnings seat read each of these and did not reach a "
            "direction, so there is no thesis and no falsifier to show. They "
            "are listed rather than omitted so that \"covered, concluded "
            "nothing\" is never mistaken for \"not covered\". Each still "
            "carries a `neutral` earnings stance in the registry below and "
            "is counted there against any direction you propose.",
        ]
        for row in rows:
            lines.append(
                f"- {row['symbol']} | {row['filing_label']} | "
                f"conviction {row['conviction']}{row['source_note']}"
            )
        return "\n".join(lines)

    @classmethod
    def stale_evidence_sources(
        cls,
        *,
        earnings_analyses: list[dict],
        asof: date | None = None,
    ) -> dict[str, frozenset[str]]:
        """`{SYMBOL: {"earnings"}}` for stances too old to earn size.

        §9.4 pays for agreement, and before this gate it paid the same for a
        view formed yesterday and one formed six months ago: the registry
        recorded only `investment_implications.sentiment` and dropped
        `filing_date` and `is_new` on the floor, so a cached bullish earnings
        stance was a full live corroborating source forever. Nothing in
        `src/risk/rules.py` or `src/portfolio_constructor.py` looked at age.
        That was reachable, not theoretical: when a symbol has no filing
        inside the provider's 45-day SEC scan window,
        `EarningsProvider._check_symbol` fell back to
        `_get_existing_analysis`, which re-served whatever was on disk with
        no age bound of its own (the store prunes at 1000 days).
        `_get_existing_analysis` now carries this same bound (2026-09-02,
        same constant, `src/data/earnings.py`), so that specific route to a
        stale stance is closed at the source — an over-age analysis is no
        longer handed to a session at all. This gate stays regardless: it is
        what actually governs the TALLY for a stance from ANY source, so a
        stale view that reaches the registry some other way is still caught
        here rather than relying on every producer to self-police age.

        Threshold: `EARNINGS_STANCE_MAX_AGE_DAYS` (90) — the number the
        earnings seat's own prompt and the missed-opportunity scan already
        use. See that constant for why it is reused rather than invented.

        A stale stance is REMOVED FROM THE TALLY ONLY. It stays in the
        canonical registry, so `validate_grounding` still recognises the
        coverage and a PM that cites it does not fail the session — this is
        a size reduction, not a new hard block. The prompt marks it so the
        PM cannot read it as corroborating.

        An absent or unparseable `filing_date` is treated as stale: an
        unknowable age is not evidence of freshness, and the same call is
        already made in `TradingPipeline._missed_ops_earnings_signal`.
        """
        today = asof or et_today()
        stale: dict[str, frozenset[str]] = {}
        for symbol, _stance, filing_date in cls._earnings_stance_rows(earnings_analyses):
            try:
                age_days = (today - date.fromisoformat(filing_date)).days
                is_stale = age_days > EARNINGS_STANCE_MAX_AGE_DAYS
            except (TypeError, ValueError):
                is_stale = True
            # Last-wins, exactly as the registry resolves the stance itself:
            # a later entry for the same symbol replaces the verdict rather
            # than merging with it.
            if is_stale:
                stale[symbol] = frozenset({"earnings"})
            else:
                stale.pop(symbol, None)
        return stale

    @classmethod
    def build_evidence_registry(
        cls,
        *,
        analyses: list[TechAnalysisResult],
        positions: list[Position],
        news_intel: NewsIntelligenceReport | None,
        earnings_analyses: list[dict],
        macro_analysis: dict | None,
        smart_money_findings: list[SmartMoneyFinding] | None = None,
        symbol_sectors: dict[str, str] | None = None,
    ) -> dict[str, dict[str, str]]:
        """Canonical source/stance records shared by prompt and validator.

        Display decorations such as conviction and signal age never enter the
        stance. Historical narrative/memory is intentionally excluded.

        The intraday path used to return TECH ONLY, on the reasoning that it
        "cannot cite yesterday's macro/news/earnings as if they ran this
        tick". The grounding concern is real; the remedy was too broad. What
        must never happen is stale evidence being cited AS FRESH — not the PM
        reasoning about a 14:00 move with no idea what regime it is happening
        in. Today's macro and news are now carried forward explicitly (see
        `TradingPipeline._carry_forward_macro` / `_carry_forward_news`, which
        refuse anything not from today) and marked `carried_from_morning` in
        `data_status`, so the staleness travels with the evidence instead of
        being handled by deleting it. Earnings stay excluded: an intraday
        filing genuinely has not been read this tick.
        """

        registry: dict[str, dict[str, str]] = {}

        def put(symbol: str, source: str, stance: str | None) -> None:
            if symbol and stance:
                registry.setdefault(symbol.strip().upper(), {})[source] = stance

        for analysis in analyses:
            put(analysis.symbol, "technical", cls._collapse_stances([analysis.rating]))

        if news_intel is not None:
            for symbol, items in news_intel.stock_news.items():
                put(symbol, "news", cls._collapse_stances(i.sentiment for i in items))

        # One rule, two readers: `_earnings_stance_rows` decides which
        # earnings entries produce a stance at all, and `put`'s last-wins
        # ordering is preserved exactly. `stale_evidence_sources` walks the
        # SAME rows so the freshness verdict can never attach to a different
        # filing than the one whose stance actually landed in the registry.
        for symbol, stance, _filing_date in cls._earnings_stance_rows(earnings_analyses):
            put(symbol, "earnings", stance)

        smart_money_stances: dict[str, list[str]] = {}
        for finding in smart_money_findings or []:
            smart_money_stances.setdefault(finding.symbol.upper(), []).append(finding.stance)
        for symbol, stances in smart_money_stances.items():
            put(symbol, "smart_money", cls._collapse_stances(stances))

        if macro_analysis:
            sectors = {str(k).upper(): str(v) for k, v in (symbol_sectors or {}).items()}
            for position in positions:
                if position.sector:
                    sectors.setdefault(position.symbol.upper(), position.sector)
            guidance: dict[str, list[str]] = {}
            for row in cls._sector_guidance_rows(macro_analysis.get("sector_guidance")):
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
        smart_money_findings: list[SmartMoneyFinding] = kwargs.get("smart_money_findings", [])
        evidence_registry = self.build_evidence_registry(
            analyses=analyses,
            positions=positions,
            news_intel=news_intel,
            earnings_analyses=earnings_analyses,
            macro_analysis=macro_analysis,
            smart_money_findings=smart_money_findings,
            symbol_sectors=kwargs.get("symbol_sectors") or {},
        )
        # §9.4 freshness — which registry entries are real coverage but too
        # old to EARN size. Computed from the same earnings list the registry
        # was built from, so the prompt and the constructor gate the same
        # stances (`pipeline_stages` recomputes both from identical inputs).
        stale_sources = self.stale_evidence_sources(
            earnings_analyses=earnings_analyses,
        )
        evidence_registry_text = json.dumps(
            evidence_registry, sort_keys=True, indent=2,
        )
        if stale_sources:
            # The registry values themselves stay undecorated — the PM must
            # copy the stance string EXACTLY for `validate_grounding`, so the
            # staleness is carried alongside rather than inside them.
            stale_registry_note = (
                "\n\nSTALE (still real coverage, still citable as provenance, "
                "but NOT counted toward the agreement score below — the "
                f"filing is more than {EARNINGS_STANCE_MAX_AGE_DAYS} days old):\n"
                + "\n".join(
                    f"- {symbol}: {', '.join(sorted(sources))}"
                    for symbol, sources in sorted(stale_sources.items())
                    if symbol in evidence_registry
                )
            )
            if not stale_registry_note.rstrip().endswith(":"):
                evidence_registry_text += stale_registry_note
        # §9.4 "agreement earns size" — tell the PM the count BEFORE it
        # sizes, not after. Rendered for both directions since the PM has
        # not chosen one yet when it reads this: a name it takes long
        # counts bullish-aligned sources, one it shorts counts bearish.
        # This is the exact registry the deterministic ceiling in
        # `PortfolioConstructor` re-derives the count from — not a preview
        # of a different number. See 2026-08-20/Phase 2b's incident class:
        # a silent clamp the PM's own stated reasoning disagreed with.
        #
        # The NET of the two counts is what sizes the trade (2026-09-02, see
        # `src/risk/rules.py::signed_source_score`). Both halves are shown
        # anyway: "3 aligned, 1 opposed" and "net +2" are different facts, and
        # a PM that only saw the net could not tell a thin unanimous idea from
        # a broad contested one. Showing the net is not optional — a ceiling
        # the PM cannot predict is the 2026-08-20 incident class, where the
        # constructor silently sized against the PM's own stated reasoning.
        def _agreement_line(symbol: str, sources: dict[str, str]) -> str:
            ignored = stale_sources.get(symbol)
            stale_note = (
                f"; {', '.join(sorted(ignored))} stance NOT counted — filing "
                f"older than {EARNINGS_STANCE_MAX_AGE_DAYS}d"
                if ignored and any(s in sources for s in ignored) else ""
            )
            long_for = count_aligned_sources(symbol, sources, "long", ignored_sources=ignored)
            long_against = count_opposing_sources(symbol, sources, "long", ignored_sources=ignored)
            short_for = count_aligned_sources(symbol, sources, "short", ignored_sources=ignored)
            short_against = count_opposing_sources(symbol, sources, "short", ignored_sources=ignored)
            long_net = signed_source_score(symbol, sources, "long", ignored_sources=ignored)
            short_net = signed_source_score(symbol, sources, "short", ignored_sources=ignored)
            return (
                f"- {symbol}: {long_for} aligned / {long_against} opposed = "
                f"net {long_net:+d} if long, "
                f"{short_for} aligned / {short_against} opposed = "
                f"net {short_net:+d} if short "
                f"(of {len(sources)} source(s) with current coverage{stale_note})"
            )

        agreement_lines = [
            _agreement_line(symbol, sources)
            for symbol, sources in sorted(evidence_registry.items())
        ]
        agreement_text = (
            "\n".join(agreement_lines) if agreement_lines
            else "No symbols with current coverage."
        )
        allowed_buy_symbols = sorted({
            str(symbol).strip().upper()
            for symbol in (kwargs.get("allowed_buy_symbols") or [])
            if str(symbol).strip()
        })
        transient_admitted_symbols = sorted({
            str(symbol).strip().upper()
            for symbol in (kwargs.get("transient_admitted_symbols") or [])
            if str(symbol).strip()
        })
        permanent_symbols = [
            symbol for symbol in allowed_buy_symbols
            if symbol not in set(transient_admitted_symbols)
        ]
        eligibility_section = (
            "## Deterministic BUY Eligibility\n"
            f"- Permanent configured universe: {', '.join(permanent_symbols) or 'none'}\n"
            "- Temporary SEC Form 4 admissions for THIS RUN only: "
            f"{', '.join(transient_admitted_symbols) or 'none'}\n"
            "Temporary admission permits evaluation; it is not a recommendation, "
            "does not waive Technical/Risk requirements, and does not permanently "
            "change the universe. Do not target any other new symbol."
        )

        def _fmt_tech(a):
            rr = a.risk_reward
            rr_str = f"R/R {rr:.2f}:1" if rr is not None else "R/R n/a"
            invalid = a.thesis_invalid_if or "(not specified)"
            age = getattr(a, "signal_age_days", None)
            age_str = f", age {age}d" if age is not None and age > 0 else ""
            # PM TEST GATE item 7 — a `neutral` technical read has, by
            # construction, no entry, no stop, no reference target and
            # therefore no reward/risk: `TechAnalysisResult.risk_reward` is
            # None for one. Measured on the frozen run_64290730 fixture: 21
            # of the 59 reads were neutral, and each spent a full four-field
            # geometry line printing the word "None" three times plus an
            # "Invalid if: (not specified)" line — 21 symbols' worth of
            # fields that exist only to say the field is absent. The
            # analyst's own one-sentence conclusion is the whole content of
            # such a read, so that is what is rendered, verbatim and
            # untruncated. This is a shape change, not a cap: no report is
            # dropped, no text is shortened, and a read that HAS geometry
            # still renders every field it has.
            if a.rating == "neutral" and rr is None:
                return (
                    f"- {a.symbol}: neutral ({a.conviction}{age_str}) | "
                    f"no entry/stop/target, not sizeable this session\n"
                    f"  Reasoning: {a.reasoning}"
                )
            return (
                f"- {a.symbol}: {a.rating} ({a.conviction}{age_str}) | {rr_str} | "
                f"Entry: {a.entry_price} | Stop: {a.stop_loss} | Target: {a.reference_target}\n"
                f"  Invalid if: {invalid}\n"
                f"  Reasoning: {a.reasoning}"
            )
        analyses_text = "\n".join(_fmt_tech(a) for a in analyses)

        # Phase 13 — the missing ordering step. Every gate above admits or
        # refuses; none of them ranks. The verdicts of the seats that have
        # been moved onto the shared shape (Technical only, so far) are
        # scored at the ratified equal weight and the eligible names are
        # shown IN ORDER, so "which of the twelve" is a stated rule rather
        # than whatever the model defaults toward. Names a gate refuses
        # are listed with the gate that refused them and are NOT ordered.
        # Item 49: reset FIRST, so a raise inside `rank_candidates` leaves no
        # previous session's ranking behind for the constructor to spend this
        # session's budget against.
        self.last_candidate_ranking = None
        ranked, blocked = self.rank_candidates(
            analyses=analyses,
            evidence_registry=evidence_registry,
            stale_sources=stale_sources,
            allowed_buy_symbols=set(allowed_buy_symbols),
            active_state_changes=kwargs.get("active_state_changes") or "",
            rr_floor=float(kwargs.get("rr_floor", REWARD_RISK_FLOOR)),
            news_intel=news_intel,
            macro_analysis=macro_analysis,
            earnings_analyses=earnings_analyses,
            smart_money_findings=smart_money_findings,
            real_reward_risk_by_symbol=kwargs.get("real_reward_risk_by_symbol"),
            constructor_refusals_by_symbol=kwargs.get("constructor_refusals_by_symbol"),
            # The same mapping `build_evidence_registry` is given a few lines
            # above, so macro's ranked verdict and macro's registry stance
            # resolve one symbol's sector identically (item 31, 2026-09-13).
            symbol_sectors=kwargs.get("symbol_sectors") or {},
        )

        # R7 — the 2026-09-25 owner conviction bar, applied ONCE here to the
        # candidate_eligibility result and consumed by BOTH sides below: the
        # PM prompt (ENTRY) and, through the same `blocked` set,
        # `holdings_below_entry_bar` / rotation's `ineligible_hold` tier
        # (STAYING). One definition, both sides. It rides on TOP of the R2-R6
        # pre-decision gates rather than inside `candidate_eligibility`, so the
        # audit shadow (`ops/model_policy/deterministic_selection.py`) still
        # mirrors those gates exactly; R7 is a NEW governance overlay on seat
        # AGREEMENT, separate from the §9.4 net-evidence floor and from the
        # continuous ranking score. `_apply_conviction_bar` moves any ENTRY
        # candidate that fails the bar out of `ranked` and into `blocked`, so the
        # constructor's budget ordering (`last_candidate_ranking`) can never
        # fund a name the bar refused. For a currently-HELD name the STAY side
        # is OPPOSITION-ONLY (owner ruling 2026-09-25): it is culled into
        # `blocked` only when a seat is actively opposed; a held name that fails
        # the ENTRY bar on soft grounds (no technical read, neutral technical,
        # support faded to neutral) is simply dropped from the ranked survivors
        # with no cull reason — it earns its right to STAY.
        _held_now = {
            (p.symbol or "").strip().upper()
            for p in positions if (p.symbol or "").strip()
        }
        ranked, blocked = self._apply_conviction_bar(
            ranked=ranked, blocked=blocked, held_symbols=_held_now,
            all_verdicts=self._collect_seat_verdicts(
                analyses=analyses,
                news_intel=news_intel,
                macro_analysis=macro_analysis,
                earnings_analyses=earnings_analyses or [],
                smart_money_findings=smart_money_findings,
                symbol_sectors=kwargs.get("symbol_sectors") or {},
            ),
        )
        self.last_candidate_ranking = list(ranked)

        ranking_section = self._render_candidate_ranking(ranked, blocked)

        # Phase 14 — opportunity-cost rotation. The ranking above orders
        # eligible names; it never asks whether capital tied up in a weak
        # holding is the actual reason a stronger new idea has no room.
        # Silent unless the book's EXISTING risk (before anything this
        # session proposes) already leaves less headroom than the desk's
        # own minimum tradeable size — see `src/rotation.py` for the
        # citations behind the margin and why the check is silent
        # otherwise. `existing_risk_pct` is None when the book's risk is
        # not visible this session (facts unavailable) — same fail-open
        # posture as every other consumer of it, never a fabricated view.
        existing_risk_pct: dict[str, float] | None = kwargs.get("existing_risk_pct")
        max_portfolio_risk_pct = float(
            kwargs.get("max_portfolio_risk_pct", 25.0) or 25.0
        )
        held_symbols = {
            p.symbol.upper() for p in positions if getattr(p, "qty", 0)
        }
        # Phase 14b: the precheck is computed ONCE and kept on the agent so
        # `DecisionStage._apply_rotation_execution` acts on exactly the
        # numbers the model was shown — never a second evaluation against
        # inputs that may have moved in between. Reset first so a stale
        # value from a previous session can never leak into this one.
        self.last_rotation_precheck = None
        # 2026-09-23. The funding half of the rotation precondition, from
        # the SAME `_entry_deployment_budget` figure the Margin Capacity
        # section above is rendered from and execution sizes entries
        # against — never a second computation of "is the book full".
        # `margin_ladder_backed=False` means that computation could not
        # resolve this session, and `None` then switches the funding test
        # off rather than letting an unreadable input read as "there is
        # room". See `src/rotation.py` for why this constraint, and not the
        # risk budget alone, is what has bound this desk.
        _entry_budget_usd = kwargs.get("margin_headroom_usd")
        if not bool(kwargs.get("margin_ladder_backed", False)) or not isinstance(
            _entry_budget_usd, (int, float),
        ) or isinstance(_entry_budget_usd, bool):
            _entry_budget_usd = None
        _min_order_usd = kwargs.get("min_order_usd")
        if not isinstance(_min_order_usd, (int, float)) or isinstance(
            _min_order_usd, bool,
        ):
            _min_order_usd = None
        rotation_precheck = self.rotation_precheck(
            ranked=ranked, blocked=blocked, held_symbols=held_symbols,
            existing_risk_pct=existing_risk_pct,
            ceiling_pct=max_portfolio_risk_pct,
            entry_budget_usd=(
                None if _entry_budget_usd is None else float(_entry_budget_usd)
            ),
            min_order_usd=(
                None if _min_order_usd is None else float(_min_order_usd)
            ),
        )
        self.last_rotation_precheck = rotation_precheck
        rotation_section = self._render_rotation_section(
            ranked=ranked, blocked=blocked, held_symbols=held_symbols,
            existing_risk_pct=existing_risk_pct,
            ceiling_pct=max_portfolio_risk_pct,
            precheck=rotation_precheck,
            execute_enabled=bool(kwargs.get("rotation_execute_enabled", False)),
            ranked_margin_enabled=bool(
                kwargs.get("rotation_ranked_margin_enabled", False),
            ),
        )

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
            weight_pct = position_weight_pct(p, total_value)
            lev_note = f" (gross, {gross_mul:g}x leveraged)" if gross_mul != 1.0 else ""
            # Flag drift candidates directly in the line so PM can't miss them.
            # P&L% tells PM whether the weight came from price appreciation (drift)
            # or a large entry.
            # `unrealized_pnl_pct` is the single definition (see
            # src/risk/metrics.py). The `cost_basis > 0` guard this replaces
            # printed a literal +0.0% for every short — a winning short
            # rendered `P&L: $1000.00 (+0.0%)`, self-contradicting on one
            # line. None means genuinely unknowable, and must not drift-flag.
            pnl_pct = unrealized_pnl_pct(p)
            pnl_pct_str = f"{pnl_pct:+.1f}%" if pnl_pct is not None else "n/a"
            drift_flag = (
                " ⚠️DRIFT"
                if weight_pct > 12 and pnl_pct is not None and pnl_pct > 10
                else ""
            )
            core = (
                f"- {p.symbol}: {p.qty} shares @ ${p.avg_entry:.2f} | "
                f"Current: ${p.current_price:.2f} | P&L: ${p.unrealized_pnl:.2f} ({pnl_pct_str}) | "
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

            # Rendered through the same normalizer the evidence registry uses:
            # the model is told to copy the validated stance exactly, so a
            # Macro section speaking a different vocabulary than the registry
            # is an invitation to cite a stance the validator will reject.
            # `reason` survives only in the live shape — MacroStore drops it.
            guidance_rows = self._sector_guidance_rows(
                macro_analysis.get("sector_guidance")
            )
            reasons = {
                str(row.get("sector")): str(row.get("reason") or "")
                for row in (macro_analysis.get("sector_guidance") or [])
                if isinstance(row, dict)
            }

            def _fmt_guidance(row: dict) -> str:
                reason = reasons.get(row["sector"], "")
                return (
                    f"- {row['sector']}: {row['stance']}"
                    + (f" — {reason}" if reason else "")
                )
            sector_guidance_text = "\n".join(
                _fmt_guidance(row) for row in guidance_rows
            ) if guidance_rows else "No sector guidance."

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

### Macro Reasoning Chain (audit these for logic errors — report in `reasoning_chain.macro_audit`)
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

            # No invested / cash numbers: owner mandate 2026-09-17, the book
            # is fully invested and macro informs direction only. Older
            # snapshots still carry `target_invested_pct` /
            # `cash_recommendation_pct`; they are deliberately not rendered.
            macro_section = f"""## Macro Analysis
- Regime: {macro_analysis.get('regime', 'N/A')} | Outlook: {macro_analysis.get('equity_outlook', 'N/A')} | Confidence: {macro_analysis.get('confidence', 'N/A')}{shift_line}{alignment_line}
- Summary: {macro_analysis.get('summary', 'N/A')}{reasoning_section}

### Key Observations
{observations_text}

### Sector Guidance
{sector_guidance_text}

### Risk Factors
{risk_factors_text}{triggers_section}

### Directional Lean (the book stays fully invested — this is which way, not how much)
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
            lost_text = news_intel.format_dropped_symbols_block()

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
{stock_text}{lost_text}

Overall sentiment: {news_intel.market_sentiment} (confidence: {news_intel.confidence})"""
        else:
            news_section = "## News Intelligence\nNo news data available."

        if smart_money_findings:
            smart_money_section = "## Smart Money Evidence\n" + "\n".join(
                f"- {f.symbol}: stance={f.stance}; role={f.economic_role}; {f.summary} Why now: {f.why_now}"
                for f in smart_money_findings
            )
        else:
            smart_money_section = "## Smart Money Evidence\nNo material source-backed finding available. Do not claim coverage."

        # Format earnings analysis section
        if earnings_analyses:
            earnings_items = []
            # PM TEST GATE item 7 — filings the seat read without reaching a
            # direction are collected here and rendered as one line each at
            # the end of the section, instead of a four-line verdict block
            # whose direction, thesis and falsifier are all absent. See
            # `_render_earnings_no_call_rollup` for the measurement and for
            # what is guaranteed to survive.
            earnings_no_call: list[dict] = []
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
                filing_label = f"{ea.get('form_type', '?')} ({ea.get('filing_date', '?')})"
                source_note = " [from cache]" if not ea.get("is_new") else " [new filing]"
                # §9.4 freshness: `[from cache]` and a filing date were
                # already here, so the model COULD see the age — but the same
                # stance was simultaneously being counted as a live
                # corroborating source in the agreement block below. Say
                # plainly which way it is, in the section the PM actually
                # reads the view from.
                if "earnings" in stale_sources.get(str(sym).strip().upper(), frozenset()):
                    source_note += (
                        f" [STALE >{EARNINGS_STANCE_MAX_AGE_DAYS}d — context only; "
                        "does NOT count toward the agreement score]"
                    )

                impl = (analysis or {}).get("investment_implications") or {}
                # Roll up ONLY a non-directional read. "mixed" is a
                # disagreement, not an absence, and stays a full block: a
                # summary that hides a split between seats is worse for the
                # decision seat than the prose it replaces.
                if self._collapse_stances([impl.get("sentiment")]) in (None, "neutral"):
                    earnings_no_call.append({
                        "symbol": sym,
                        "filing_label": filing_label,
                        "conviction": impl.get("conviction", "N/A"),
                        "source_note": source_note,
                    })
                    continue

                earnings_items.append(
                    self._render_earnings_verdict(
                        sym=sym, analysis=analysis, filing_label=filing_label,
                        source_note=source_note, analysis_path=ea.get("analysis_path"),
                    )
                )
            rollup = self._render_earnings_no_call_rollup(earnings_no_call)
            if rollup:
                earnings_items.append(rollup)
            earnings_section = "## Earnings Analysis (from SEC Filings)\n\n" + "\n\n".join(earnings_items)
        else:
            earnings_section = "## Earnings Analysis\nNo recent earnings filings available."

        # Account Status "Invested" reads the SAME `book_exposure` the
        # PMFacts Book State block and the pre-trade `deployment_gap`
        # advisory read. It used to be `total_value - cash_balance`, a third
        # definition of the same quantity inside this one prompt.
        #
        # That subtraction is not merely a different basis, it is wrong in a
        # specific direction: equity is `cash + sum(market_value)` and a held
        # short's `market_value` is NEGATIVE, so every short made the book
        # look LESS invested to the PM — which then deployed more. Deployment
        # is unsigned: shorting is capital put to work.
        book = _book_exposure(positions, total_value)
        invested = book.deployed_usd
        invested_pct = book.deployed_pct
        net_exposure_pct = book.net_pct

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
            # Margin is enabled. Do NOT tell the model cash is the limit —
            # the account may run to the §11.2 de-levering ladder's ceiling,
            # not just to settled cash (2026-09-17 CRM incident: the prompt
            # said "no margin is deployable" while $11.4k of real ladder
            # headroom existed and execution had already spent margin three
            # hours earlier that same session).
            #
            # `margin_headroom_usd` / `margin_ladder_multiple` /
            # `margin_ladder_rung` are threaded in from the SAME §11.2
            # computation execution's submit loop uses
            # (`_entry_deployment_budget` / `_session_gross_ceiling` in
            # `src/pipeline_stages.py`) — this section never derives its own
            # number. `margin_ladder_backed=False` means that computation
            # could not resolve this session (e.g. equity/ceiling unreadable
            # at prompt-build time), and the section says so rather than
            # guessing a figure.
            headroom_usd = kwargs.get("margin_headroom_usd")
            ladder_multiple = kwargs.get("margin_ladder_multiple")
            ladder_rung = kwargs.get("margin_ladder_rung")
            ladder_backed = bool(kwargs.get("margin_ladder_backed", False))
            if (
                ladder_backed
                and isinstance(headroom_usd, (int, float))
                and isinstance(ladder_multiple, (int, float))
            ):
                margin_section = (
                    "## Margin Capacity (margin is ENABLED)\n"
                    f"- This account may run gross exposure up to "
                    f"{ladder_multiple:.2f}x equity (the §11.2 de-levering "
                    f"ladder's current ceiling, rung {ladder_rung}) — NOT just "
                    f"up to available cash.\n"
                    f"- Ladder headroom remaining this session: "
                    f"${headroom_usd:,.2f}. A BUY or SHORT may still draw on "
                    f"this even when Cash Balance above is negative.\n"
                    f"- This is the same figure execution sizes new entries "
                    f"against — not a separate estimate."
                )
            else:
                margin_section = (
                    "## Margin Policy\n"
                    "- Margin is ENABLED for this account, but this session's "
                    "ladder headroom could not be resolved for this prompt. "
                    "Do NOT read the raw Cash Balance above as your spending "
                    "limit — a BUY may still be able to draw margin. Treat "
                    "the ladder as unknown, not as zero."
                )
            # Board item 95. Capacity without its price is half the picture:
            # this block has named the spending limit since 2026-09-17 and
            # never once named the cost, while the PM's own sheet says "You
            # may borrow". The cost lines go on BOTH branches — the
            # unresolved-ladder branch is exactly where a seat is most
            # likely to reach for margin on a guess — and are silent when
            # there is no debit and no headroom to price. Rate read here
            # rather than threaded through kwargs so an older caller that
            # predates this cannot silently drop the price; a config read
            # that fails must never break the prompt, so it degrades to
            # saying nothing extra rather than to a fabricated figure.
            # The rate is THREADED IN from the caller's already-loaded
            # `config.risk.margin_interest_rate_pct`, never re-read here:
            # loading `AppConfig` inside a prompt renderer validates API
            # keys and fails in every context that has none, which would
            # make the price silently vanish exactly where it is hardest
            # to notice. Absent rate -> no cost lines, never a guessed one.
            try:
                from src.margin_interest import format_borrowing_cost_lines
                _rate_pct = kwargs.get("margin_interest_rate_pct")
                _cost_lines = format_borrowing_cost_lines(
                    cash_balance,
                    _rate_pct if isinstance(_rate_pct, (int, float))
                    and not isinstance(_rate_pct, bool) else None,
                    headroom_usd if isinstance(headroom_usd, (int, float))
                    and not isinstance(headroom_usd, bool) else None,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "Could not render the borrowing-cost lines for the PM "
                    "prompt (%s); the Margin Capacity block is being sent "
                    "WITHOUT its cost of carry.", exc,
                )
                _cost_lines = []
            if _cost_lines:
                margin_section += "\n\n### What borrowing costs\n" + "\n".join(
                    _cost_lines
                )

        # Recent system performance, REPORTING ONLY. The `in_drawdown`  # retired-ok
        # flag and its two thresholds used to live here and halved every new
        # BUY; that brake was removed 2026-09-20 on the owner's instruction
        # (retired item 32, docs/INCIDENT_HISTORY.md). These numbers now
        # inform the seat and gate nothing.
        recent_perf = kwargs.get("recent_performance") or {}
        if recent_perf:
            r5 = recent_perf.get("rolling_5d_pct")
            r20 = recent_perf.get("rolling_20d_pct")
            trailing = recent_perf.get("trailing_days") or 0

            # A window with too little equity history to evaluate used to
            # render as "None%", which reads to a model as a number near
            # zero. The rolling windows need 6 and 21 recorded sessions
            # respectively (`_compute_recent_performance` reads rows[5] /
            # rows[20]), and rows are only written by an evening run, so a
            # paused desk does not accrue them. Say so instead of printing
            # a null.
            def _window(value, needed: int) -> str:
                if value is not None:
                    return f"{value}%"
                return (
                    f"NOT YET MEASURABLE — needs {needed} recorded sessions, "
                    f"{trailing} on record. Do not read it as zero or as an "
                    f"all-clear."
                )

            perf_section = (
                f"## Recent System Performance\n"
                f"- Trailing 5-day return: {_window(r5, 6)}\n"
                f"- Trailing 20-day return: {_window(r20, 21)}\n"
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

            # Defect (d) fix: evening's structured "lesson categories" —
            # thesis_updates / selection_rules / discipline_notes — were
            # produced by the LLM every night and asked for in the evening
            # prompt, but never made it past `save_evening_snapshot` into
            # the DB, so Step 6 ("Yesterday's lessons: apply any relevant
            # learnings") had nothing to read. Wired here the same way the
            # rest of this section already is — date-labeled by `freshness`
            # above, with a labelled absence (not silence, not a fabricated
            # note) when evening didn't fill a category that day.
            def _parse_str_list(raw) -> list[str]:
                if isinstance(raw, str):
                    try:
                        parsed = json.loads(raw)
                    except (json.JSONDecodeError, TypeError):
                        return []
                else:
                    parsed = raw
                return [str(x) for x in parsed] if isinstance(parsed, list) else []

            def _lesson_bullets(items: list[str], empty_label: str) -> str:
                if not items:
                    return f"  ({empty_label})"
                # Defensive per-item cap — evening's prompt already asks for
                # 0-5/0-3 short items, this just bounds a runaway one.
                return "\n".join(f"  - {item[:220]}" for item in items)

            thesis_updates = _parse_str_list(yesterday_insights.get("thesis_updates_json", "[]"))
            selection_rules = _parse_str_list(yesterday_insights.get("selection_rules_json", "[]"))
            discipline_notes = _parse_str_list(yesterday_insights.get("discipline_notes_json", "[]"))
            thesis_text = _lesson_bullets(thesis_updates, "no thesis updates carried from last night")
            selection_text = _lesson_bullets(selection_rules, "no new selection rules carried from last night")
            discipline_text = _lesson_bullets(discipline_notes, "no discipline notes carried from last night")

            insights_section = f"""## Prior Evening Insights{freshness}
- **Tilt for today**: bias={bias}, conviction={conviction}
- Outlook (prose): {yesterday_insights.get('tomorrow_outlook', 'N/A')}
- Key risks to watch today:
{risks_text}
- Lessons: {yesterday_insights.get('lessons', 'N/A')}
- Risk Rating: {yesterday_insights.get('risk_rating', 'N/A')}
- Suggested Actions:
{actions_text}
{sell_line}
- Thesis updates on held positions (apply at Step 6):
{thesis_text}
- New selection rules (apply when sizing new BUYs):
{selection_text}
- Discipline notes (apply at Step 6 holding discipline):
{discipline_text}"""
        else:
            insights_section = (
                "## Yesterday's Evening Insights\n"
                "No prior session insights available "
                "(no outlook, lessons, or thesis/selection/discipline notes from last night)."
            )

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
        # The `[date]` prefix on each row is not decoration: it is the
        # citation key the sub-floor catalyst gate resolves against
        # (`_apply_subfloor_catalyst_rule`). Saying so HERE, next to the rows
        # themselves, is what makes the requirement actionable — the rule
        # itself is enforced in Python after submission either way.
        active_changes_section = (
            "## Active News State Changes (HIGH conviction, last 14d)\n"
            "Cite a row by its `[date]` in a target's `catalyst` field. That is "
            "the ONLY way to claim the sub-floor R/R exception, and the row must "
            "name the symbol WITH a direction that supports the trade — "
            "`SYMBOL(bullish)` for a long, `SYMBOL(bearish)` for a short. "
            "`(neutral)` or `(unknown)` does not qualify either direction.\n"
            f"{active_state_changes}"
            if active_state_changes else
            "## Active News State Changes\n(none surfaced in the rolling 14-day "
            "window — with no rows to cite, the sub-floor R/R exception is "
            "unavailable today)"
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

        # What you asked for and never got. Diagnostic only — nothing here
        # blocks a name; it tells you which of your asks the machinery keeps
        # refusing, and with what stored reason.
        blocked_proposals: str = kwargs.get("blocked_proposals") or ""
        blocked_section = (
            f"## Proposal Conversion (last 21d — what you asked for vs what "
            f"you got)\n{blocked_proposals}\n\n"
            "A block is cleaner evidence than a loss: it comes with its cause "
            "attached. If a name is listed here, re-proposing it unchanged "
            "will fail the same way again — either fix what the reason names "
            "(geometry, sizing, cash) or drop the name. This is information, "
            "not a prohibition: none of these symbols is barred."
            if blocked_proposals else
            "## Proposal Conversion\n(no proposals on record in the last 21 days)"
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
        # 2026-09-17 fix: this used to hardcode "no margin" regardless of
        # `allow_margin`. When margin is enabled, cash is still raw cash —
        # real, and can go negative — but it is NOT the spending limit, so
        # the label must not claim the account has none. See the Margin
        # Capacity / Margin Policy section below for what may still be spent.
        cash_status = (
            "deployable this session, no margin" if not allow_margin
            else "raw cash — see Margin Capacity below for what may still be spent"
        )
        # Accounting re-ask (board item 133, 2026-09-18). Non-empty ONLY on
        # the one bookkeeping re-ask `DecisionStage` may make in a session,
        # and it names the candidates this seat dropped without saying why.
        # Empty string on every first call, so the prompt this seat normally
        # sees is byte-for-byte unchanged. It asks for the missing
        # `rejections` entries and nothing else — see
        # `src/pm_accounting.REASK_DIRECTIVE` for why it must not re-open
        # the decision.
        accounting_challenge: str = kwargs.get("accounting_challenge") or ""
        accounting_section = (
            f"### ⚠️ {accounting_challenge}\n\n"
            if accounting_challenge else ""
        )
        return f"""{accounting_section}## Account Status
- Total Value: ${total_value:,.2f}
- Cash Balance: ${cash_balance:,.2f} ({cash_status}){reserve_line}
- Invested: ${invested:,.2f} ({invested_pct:.1f}% of equity — capital at work, unsigned and un-leveraged; a short counts its notional, not a credit)
- Net direction: {net_exposure_pct:+.1f}% of equity (leverage-aware and signed; negative = net short). This is NOT the number macro's target is set against — `Invested` is.

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

{blocked_section}

{insights_section}

{macro_section}

{news_section}

{earnings_section}

{smart_money_section}

{eligibility_section}

## Technical Analysis Reports
{analyses_text}

{ranking_section}

{rotation_section}

## Canonical Current Evidence Registry (authoritative for provenance)
{evidence_registry_text}

For every target, cite only source/stance pairs present for that exact symbol
in this registry and copy the stance string exactly. Omit unavailable sources.
Memory and narrative sections are context, never current specialist coverage.

## Independent Source Agreement (deterministic refusal — Step 5)
{agreement_text}
The NET score above — independent sources ALIGNED with the direction you
propose, MINUS those opposed to it, computed from this registry and not from
what you write in provenance — is a GO/NO-GO, not a size dial. A source whose
stance is marked stale is in neither count: an old filing is still worth
reading, but it has not confirmed anything about today, and it has not
contradicted anything either.

A seat arguing the OTHER way SUBTRACTS from the net.
**A net score of zero or below produces NO ORDER AT ALL** — not a small
position, no position. Anything already held is left alone; refusing to open
is not a decision to sell.

**A net of +1 or more imposes no size restriction of its own** (the graduated
ceiling was retired 2026-09-14: it scaled size by the square root of the seat
count, which is the statistics of INDEPENDENT estimates, and these seats read
overlapping evidence). Size the idea on its own merits and the risk side,
within the ratified per-trade envelope. Do NOT shrink an idea because it has
fewer agreeing seats — but a name with one seat for and one against is not
tradeable today at any size. If you believe a dissenting seat is wrong, say
why in your reasoning; the constructor computes this from the registry and
cannot read your argument.

Based on all the above (memory of past decisions + environment trajectory + today's signals), what trades should we execute? Respond as JSON."""

    # ------------------------------------------------------------------
    # Phase 13 — candidate eligibility + ranking over the shared verdict shape
    # ------------------------------------------------------------------

    @classmethod
    def candidate_eligibility(
        cls, *,
        analyses: list[TechAnalysisResult],
        evidence_registry: dict[str, dict[str, str]],
        stale_sources: dict[str, set[str]] | None = None,
        allowed_buy_symbols: set[str] | None,
        active_state_changes: str,
        rr_floor: float = REWARD_RISK_FLOOR,
        asof: date | None = None,
        real_reward_risk_by_symbol: dict[str, float | None] | None = None,
        constructor_refusals_by_symbol: dict[str, dict[str, str]] | None = None,
    ) -> dict[str, list[str]]:
        """Which analysed names the desk's own rules ADMIT, before the PM
        decides — `{SYMBOL: [reasons it is blocked]}`, empty list = eligible.

        These are the pre-decision halves of the gates this class and the
        constructor already enforce after submission, restated so the prompt
        can order the survivors (item 18b, `ops/model_policy/
        deterministic_selection.py::evaluate`, now in production code):

          R2  rating actionable (neutral is not a candidate)
          R3  a long must be in the BUY-eligible set — the same set
              `validate_grounding` refuses increases outside of
          R4  **retired 2026-09-17.** Used to read "R/R ≥ `rr_floor`", then
              "R/R must be measurable or a catalyst row names the symbol".
              Neither is a block. A range ratio, including None, is ranking
              information. A breakout is not measured on reward:risk. The
              `rr_floor` argument is accepted and ignored so callers do not
              silently re-default a retired number. Catalyst is not an
              admission door.
          R5  net independent source score ≥ 1 for the proposed direction
              (`signed_source_score`; §9.4 refuses net ≤ 0 outright —
              `agreement_refuses_trade`, a refusal gate with no schedule
              and no config behind it since 2026-09-14)
          R6  the constructor's own preview REFUSED this name by code
              (`constructor_refusals_by_symbol`, a snapshot of
              `PortfolioConstructor.last_refusals` taken after
              `real_reward_risk_preview` ran over every analysis) — today
              `no_structural_stop_and_no_volatility_reading` (docs/WORK.md
              item 180 dropped the young-listing bar-count refusal, and
              item 56 deleted the stop-WIDTH refusal
              `stop_wider_than_instrument_reach` on 2026-09-26 — a wide
              stop is answered by a smaller position, never a refusal).
              The enforcing check is one stage later, in the ONE funnel
              construction shares with the preview; this only stops the PM
              being shown a name that funnel has already refused. Absent
              structure is NOT a reason — the earlier "no floor, no trade"
              R6 was replaced the day it shipped, on sourced research.

        R1 (current technical coverage) is implied: only symbols with an
        analysis in `analyses` are considered at all. Nothing here removes or
        weakens a gate — a name this admits can still be dropped after
        submission by the stricter post-decision checks.

        **R4's number, 2026-09-04 fix.** `real_reward_risk_by_symbol` — when
        supplied, keyed by upper-case symbol — is
        `PortfolioConstructor.real_reward_risk_preview`'s output: the SAME
        derived-target, noise-floor-widened reward:risk `construct_orders`
        gates on, not `TechAnalysisResult.risk_reward` (the analyst's own
        guessed target, real arithmetic but never checked against
        structure). A 2026-09-04 audit found this gate and the
        constructor's real one passing DISJOINT sets on a real day — zero
        overlap — because this gate ran first and screened candidates on a
        different number than the one that would decide them one stage
        later. A symbol absent from the map is treated as `None` (fail
        closed, same as an unmeasurable ratio always has been here).
        Omitted entirely (`None`), this falls back to `analysis.risk_reward`
        for callers that have not wired a constructor preview through
        (rare — pre-existing tests, and any harness with no
        `PortfolioConstructor` instance to hand); production always
        supplies it.
        """
        _ = (active_state_changes, asof, rr_floor, real_reward_risk_by_symbol)
        allowed = {
            str(s).strip().upper() for s in (allowed_buy_symbols or set())
            if str(s).strip()
        }
        stale = stale_sources or {}

        verdicts: dict[str, list[str]] = {}
        for analysis in analyses:
            symbol = analysis.symbol.upper()
            blocked: list[str] = []
            if analysis.rating == "neutral":
                blocked.append("R2 neutral rating")
                verdicts[symbol] = blocked
                continue
            direction = "short" if analysis.rating in ("sell", "strong_sell") else "long"
            if direction == "long" and symbol not in allowed:
                blocked.append("R3 not BUY-eligible")
            # R4 is retired. Reward:risk — computed, thin, or unmeasurable —
            # does not admit or refuse. Ranking still consumes the number
            # when it exists. `rr_floor` and catalyst rows are not a door.
            sources = evidence_registry.get(symbol, {})
            net = signed_source_score(
                symbol, sources, direction, ignored_sources=stale.get(symbol),
            ) if sources else 0
            if net <= 0:
                blocked.append(f"R5 net evidence {net:+d} if {direction} — no rung")
            # R6 — the constructor's preview refused this name by code
            # (item 54). Read from the snapshot, never recomputed here: the
            # width gate and the history gate live in the one funnel the
            # preview and construction share, and a second copy could
            # drift. What R6 adds over R4 is the case R4 cannot see — a
            # breakout (no reward:risk number at all) whose stop is wider
            # than the instrument's own noise band, or a listing too young
            # to measure.
            refusal = (constructor_refusals_by_symbol or {}).get(symbol)
            if refusal and refusal.get("refusal"):
                blocked.append(
                    f"R6 constructor refused [{refusal['refusal']}] — "
                    f"{refusal.get('detail') or 'no detail recorded'}"
                )
            verdicts[symbol] = blocked
        return verdicts

    @staticmethod
    def _collect_seat_verdicts(
        *,
        analyses: list[TechAnalysisResult],
        news_intel: "NewsIntelligenceReport | None",
        macro_analysis: dict | None,
        earnings_analyses: list[dict],
        smart_money_findings: list[SmartMoneyFinding] | None,
        symbol_sectors: dict[str, str] | None = None,
    ) -> list[AnalystVerdict]:
        """Every seat's Phase 13 verdict, best-effort, one bad entry never
        drops another's or the run's.

        2026-09-03: Technical was the only seat on this shape when ranking
        first shipped. This extends it to the other four — news, macro,
        earnings, smart_money — each via its own `to_verdict()` (or, for
        news, the module-level `news_verdict_for_symbol`, since News files
        several items per symbol with no single object to call it on).

        Two of the four arrive in a DIFFERENT shape here than their own
        agent modules use: `macro_analysis` and each `earnings_analyses`
        entry's `"analysis"` key are plain dicts (already `model_dump()`'d
        for the pipeline), not live `MacroAnalysis`/`EarningsAnalysis`
        objects — re-parsed via `model_validate` before `to_verdict()` can
        be called. `smart_money_findings` and `news_intel` are already the
        real objects.

        Every per-seat, per-symbol step is wrapped: a single malformed dict,
        an `EarningsAnalysis` whose disclosed bull/bear case is the literal
        "not disclosed" placeholder (which `to_verdict()` deliberately
        refuses to construct from — see that method), or any other
        unexpected shape must drop ONLY that one seat's contribution for
        that one symbol, never the whole ranking. Mirrors the
        never-block-the-run posture already established by
        `_record_seat_stances` and `_check_levels_coverage`.

        **2026-09-13, retired item 31 — macro is now sector-adjusted.**
        It used to be applied via its plain `equity_outlook`, the same for
        every symbol, while `build_evidence_registry` — the same macro read,
        rendered into the same prompt — already resolved a per-symbol stance
        from `sector_guidance`. The two could and did disagree for any symbol
        whose sector view differed from the broad market view: one belief,
        two answers, in one prompt.

        `symbol_sectors` (the same mapping `build_evidence_registry` takes,
        from the same `pipeline._last_symbol_sectors` cache) is now passed
        per symbol into `MacroAnalysis.to_verdict`, which resolves the sector
        stance through the identical `collapse_stances` reduction the
        registry uses and falls back to `equity_outlook` when the read stated
        nothing for that sector. See that method for why the old objection
        ("nothing sector-specific to attach") only held for conviction, and
        what is done about it.

        Sectors arrive keyed however the caller had them; matching is
        case-insensitive on the symbol, so a lower-case key still resolves.
        A symbol absent from the mapping simply gets the broad read, exactly
        as before — this can only ever make a verdict agree with the
        registry, never introduce a stance neither of them held.
        """
        verdicts: list[AnalystVerdict] = []

        for analysis in analyses:
            try:
                verdicts.append(analysis.to_verdict())
            except Exception:
                logger.warning(
                    "Phase 13: technical verdict failed for %s", analysis.symbol,
                    exc_info=True,
                )

        if news_intel is not None:
            for symbol, items in (news_intel.stock_news or {}).items():
                if not items:
                    continue
                try:
                    verdict = news_verdict_for_symbol(symbol, items)
                    if verdict is not None:
                        verdicts.append(verdict)
                except Exception:
                    logger.warning(
                        "Phase 13: news verdict failed for %s", symbol, exc_info=True,
                    )

        if macro_analysis:
            try:
                payload = (
                    macro_analysis if isinstance(macro_analysis, dict)
                    else macro_analysis.model_dump()
                )
                from src.seat_heal import coerce_macro_shape, describe_macro_parse_failure
                payload, _fixes = coerce_macro_shape(payload)
                macro = MacroAnalysis.model_validate(payload)
            except Exception as exc:
                macro = None
                from src.seat_heal import describe_macro_parse_failure
                reason = describe_macro_parse_failure(
                    macro_analysis if isinstance(macro_analysis, dict) else {},
                    exc,
                )
                logger.error("Phase 13: macro_analysis failed to parse: %s", reason, exc_info=True)
                failures = getattr(PortfolioManagerAgent, "_macro_parse_failures", None)
                if not isinstance(failures, list):
                    PortfolioManagerAgent._macro_parse_failures = []
                    failures = PortfolioManagerAgent._macro_parse_failures
                failures.append(reason)
            if macro is not None:
                sectors = {
                    str(k).strip().upper(): str(v)
                    for k, v in (symbol_sectors or {}).items()
                }
                symbols = {a.symbol.upper() for a in analyses}
                for symbol in symbols:
                    try:
                        verdicts.append(
                            macro.to_verdict(symbol, sector=sectors.get(symbol)),
                        )
                    except Exception:
                        logger.warning(
                            "Phase 13: macro verdict failed for %s", symbol, exc_info=True,
                        )

        for item in earnings_analyses or []:
            raw = item.get("analysis") if isinstance(item, dict) else None
            if not isinstance(raw, dict):
                continue
            wrapper_symbol = str(item.get("symbol") or "").strip().upper()
            if not wrapper_symbol:
                # No ground truth to check against at all — dropped rather
                # than trusting the LLM's own `EarningsAnalysis.symbol`
                # unverified. Matches `_earnings_stance_rows` above, which
                # drops on the identical missing-wrapper-symbol case rather
                # than falling back to the embedded one. Found in adversarial
                # review 2026-09-03: the mismatch check below only fires
                # when BOTH sides are present, which silently let an
                # unverified symbol through when the wrapper's was blank.
                continue
            try:
                earnings = EarningsAnalysis.model_validate(raw)
                # The wrapper's symbol is pipeline-set ground truth (the
                # filing this analysis was actually run against);
                # `EarningsAnalysis.symbol` is part of the LLM's own JSON
                # response and could in principle disagree (a hallucinated
                # or misread ticker) — `_earnings_stance_rows` above already
                # trusts the wrapper's symbol for exactly this reason. A
                # mismatch here is treated as malformed input, not silently
                # resolved either way, matching this codebase's fail-closed
                # posture on divergent ground-truth sources.
                if earnings.symbol.upper() != wrapper_symbol:
                    logger.warning(
                        "Phase 13: earnings symbol mismatch, wrapper=%s "
                        "analysis=%s — dropped", wrapper_symbol, earnings.symbol,
                    )
                    continue
                verdicts.append(earnings.to_verdict())
            except Exception:
                logger.warning(
                    "Phase 13: earnings verdict failed for %s",
                    wrapper_symbol, exc_info=True,
                )

        for finding in smart_money_findings or []:
            try:
                verdicts.append(finding.to_verdict())
            except Exception:
                logger.warning(
                    "Phase 13: smart_money verdict failed for %s",
                    getattr(finding, "symbol", "?"), exc_info=True,
                )

        return verdicts

    @classmethod
    def rank_candidates(
        cls, *,
        analyses: list[TechAnalysisResult],
        evidence_registry: dict[str, dict[str, str]],
        stale_sources: dict[str, set[str]] | None = None,
        allowed_buy_symbols: set[str] | None,
        active_state_changes: str,
        rr_floor: float = REWARD_RISK_FLOOR,
        asof: date | None = None,
        news_intel: "NewsIntelligenceReport | None" = None,
        macro_analysis: dict | None = None,
        earnings_analyses: list[dict] | None = None,
        smart_money_findings: list[SmartMoneyFinding] | None = None,
        real_reward_risk_by_symbol: dict[str, float | None] | None = None,
        constructor_refusals_by_symbol: dict[str, dict[str, str]] | None = None,
        symbol_sectors: dict[str, str] | None = None,
    ) -> tuple[list[RankedCandidate], dict[str, list[str]]]:
        """The eligible names in ranked order, plus the blocked names with
        their reasons. Ordering is `src/verdicts.py::rank_verdicts` over
        every seat's `AnalystVerdict` (2026-09-03: extended from Technical
        alone to all five — see `_collect_seat_verdicts`), at the
        research-informed prior weight (`src/verdicts.py::SEAT_WEIGHT`).
        Blocked names are never ordered: they are reported, not ranked.

        Eligibility itself is still decided from Technical's own analyses
        only (`candidate_eligibility` — the R/R floor, structural-level, and
        catalyst gates all key off Technical's numbers) — the other seats
        only ever ADD a second, third, fourth, or fifth vote onto a symbol
        Technical already cleared. A symbol only news/macro/earnings/
        smart_money covered, with no Technical read, can never appear here;
        it was never eligible in the first place.

        `real_reward_risk_by_symbol` is passed straight through to
        `candidate_eligibility` — see that method's docstring — AND, as of
        2026-09-11 (docs/WORK.md item 1(d)), into `rank_verdicts` as the
        ordering key, alongside each candidate's setup type. That is the
        substantive half of item 1(d): the real per-trade reward:risk stopped
        being a pass/fail cutoff and became a real input to the already-
        ratified weighted ranking, and a breakout carries no such number at
        all rather than a zero.
        """
        eligibility = cls.candidate_eligibility(
            analyses=analyses,
            evidence_registry=evidence_registry,
            stale_sources=stale_sources,
            allowed_buy_symbols=allowed_buy_symbols,
            active_state_changes=active_state_changes,
            rr_floor=rr_floor,
            real_reward_risk_by_symbol=real_reward_risk_by_symbol,
            constructor_refusals_by_symbol=constructor_refusals_by_symbol,
            asof=asof,
        )
        all_verdicts = cls._collect_seat_verdicts(
            analyses=analyses,
            news_intel=news_intel,
            macro_analysis=macro_analysis,
            earnings_analyses=earnings_analyses or [],
            smart_money_findings=smart_money_findings,
            symbol_sectors=symbol_sectors,
        )
        eligible_verdicts = [
            v for v in all_verdicts
            if not eligibility.get(v.symbol.upper(), ["no eligibility row"])
        ]
        # The desk's own real, structure-derived ratio and each candidate's
        # setup type both reach the ranking (2026-09-11, item 1(d)): a range
        # trade is ordered on the REAL number rather than the analyst's
        # guessed one, and a breakout carries no reward:risk key at all.
        # This is where the removed hard floor's information went.
        ranked = rank_verdicts(
            eligible_verdicts,
            real_reward_risk=real_reward_risk_by_symbol,
            setup_types={a.symbol.upper(): a.setup_type for a in analyses},
        )
        blocked = {s: why for s, why in eligibility.items() if why}
        return ranked, blocked

    @staticmethod
    def _apply_conviction_bar(
        *,
        ranked: list[RankedCandidate],
        blocked: dict[str, list[str]],
        held_symbols: set[str],
        all_verdicts: list[AnalystVerdict],
    ) -> tuple[list[RankedCandidate], dict[str, list[str]]]:
        """The 2026-09-25 owner conviction bar (R7), applied to the ranking.

        A NEW governance overlay on top of `candidate_eligibility`'s R2-R6
        pre-decision gates: the ROLE-BASED conviction bar
        (`own_bar_block_reason`). A name is admitted only if the technical seat
        confirms timing (a timing VETO, no positive weight), NO seat is opposed,
        and at least one NON-technical seat took a supported directional side
        (`_has_supported_directional_thesis` — not a genuine specificity or
        falsifiability test; News and Smart-money always synthesise their
        invalidation). Deliberately OUTSIDE
        `candidate_eligibility` so the audit shadow
        (`ops/model_policy/deterministic_selection.py`) keeps mirroring those
        gates exactly.

        ENTRY vs STAY — two verdicts from ONE bar (owner ruling 2026-09-25):

          * ENTRY (a candidate NOT in `held_symbols`): full-strict. Any R7
            failure — technical veto, opposition, OR a soft miss (no supported
            directional thesis) — moves the name OUT of `ranked` and INTO
            `blocked` under `CONVICTION_BAR_REASON_PREFIX`. Unchanged.
          * STAY (a candidate IN `held_symbols`): OPPOSITION-ONLY. A held name
            is culled into `blocked` (which rotation's `ineligible_hold` tier
            reads) ONLY when a seat is ACTIVELY OPPOSED to the held direction
            (`own_bar_opposition_reason` non-None). A held name that fails the
            entry bar on SOFT grounds — no technical read this review, a
            neutral/non-confirming technical read, or support that faded to
            neutral — is dropped from the ranked survivors (it does not belong
            in the fresh-entry budget order) but gets NO cull reason: it earns
            its right to STAY. The price-thesis-break exit
            (`src.risk.exit_guard`) handles genuine deterioration separately.

        Supportive/opposed are read from the SEAT VERDICTS
        (`AnalystVerdict`), one source of "which seat took which side" — a long
        is supported by a bullish verdict, a short by a bearish one — so this
        never introduces a second notion of "aligned".

        SAFETY INVARIANT this relies on (not enforced by the type system): every
        symbol in `ranked` is expected to already carry a technical verdict in
        `by_symbol`, because ranking itself is derived from technical reads (see
        `_render_candidate_ranking`: "no Technical reads this session -> nothing
        to rank"). So the "absent technical read" branch is expected to never
        fire for a real ranked name — a ranked name with no technical verdict at
        all would mean that upstream invariant broke. It is logged loudly below
        when it does; the name is still handled safely (an entry candidate is
        refused, a held name drops from survivors without a spurious cull).
        """
        by_symbol: dict[str, list[AnalystVerdict]] = {}
        for v in all_verdicts:
            by_symbol.setdefault(v.symbol.upper(), []).append(v)
        held = {str(s).strip().upper() for s in held_symbols if str(s).strip()}
        blocked = {s: list(why) for s, why in blocked.items()}
        survivors: list[RankedCandidate] = []
        for c in ranked:
            sym = c.symbol.upper()
            sym_verdicts = by_symbol.get(sym, [])
            if not any(v.seat == "technical" for v in sym_verdicts):
                logger.warning(
                    "R7 conviction bar: ranked candidate %s carries no "
                    "technical verdict in by_symbol — the ranked-implies-"
                    "technical invariant broke upstream; handling %s per the "
                    "absent-technical branch (not a silent cull, not a crash)",
                    sym, sym,
                )
            reason = own_bar_block_reason(
                sym_verdicts, direction=c.direction,
            )
            if reason is None:
                survivors.append(c)
            elif sym in held:
                # STAY: opposition-only. A soft miss drops the name from the
                # entry budget order but never adds a cull reason.
                opposition = own_bar_opposition_reason(
                    sym_verdicts, direction=c.direction,
                )
                if opposition is not None:
                    blocked.setdefault(sym, []).append(opposition)
            else:
                # ENTRY: full-strict.
                blocked.setdefault(sym, []).append(reason)
        return survivors, blocked

    @staticmethod
    def _render_candidate_ranking(
        ranked: list[RankedCandidate], blocked: dict[str, list[str]],
    ) -> str:
        """The prompt section. Order first, arithmetic beside each row, then
        the refused names with the gate that refused them."""
        lines = ["## Candidate Ranking (deterministic, Phase 13)"]
        if not ranked and not blocked:
            lines.append("(no Technical reads this session — nothing to rank)")
            return "\n".join(lines)
        lines += [
            "The names below passed every rule that can be checked before you "
            "decide (actionable rating; longs BUY-eligible; R/R at or above "
            "the floor, or a current state-change row naming the symbol; net "
            "independent evidence ≥ 1). They are ORDERED by the SUM across "
            "every seat that reported a direction on the name, of that "
            "seat's stated strength plus its stated conviction — technical "
            "and earnings weighted 1.2x, news at 1.0x baseline, smart_money "
            "and macro at 0.8x, a research-informed prior (2026-09-03), not "
            "a measurement of THIS desk's own analysts. A SUM, so more "
            "agreeing seats always score higher: breadth is the point, and "
            "an agreeing seat can never pull a name down. Only technical "
            "states a strength of its own (its rating rungs); the other four "
            "seats have no strength scale, so they contribute their "
            "conviction only. The score is therefore NOT capped at 2.0 and "
            "not comparable across sessions with different coverage. "
            "This is the tiebreak among equally eligible names: to take a "
            "lower-ranked name over a higher one, say what the ranking does "
            "not see. It is not a size, and it does not waive any rule "
            "below.",
        ]
        if ranked:
            for i, c in enumerate(ranked, 1):
                seats = ", ".join(c.seats)
                convictions = "/".join(v.conviction for v in c.verdicts)
                invalidation = "; ".join(
                    f"{v.seat}: {v.invalidation}" for v in c.verdicts if v.invalidation
                )
                # A seat that looked and came back with no lean is stated,
                # not omitted (2026-09-13). Otherwise "macro's own sector
                # rows contradicted each other on this name" is invisible
                # here and reads exactly like "macro never covered it".
                no_lean = (
                    f" | no lean from: {', '.join(c.neutral_seats)}"
                    if c.neutral_seats else ""
                )
                lines.append(
                    f"{i}. {c.symbol} — {c.direction} | score {c.score:.2f} "
                    f"(strength {c.components['magnitude']:.2f} + conviction "
                    f"{c.components['conviction_score']:.2f}, summed over "
                    f"{len(c.verdicts)} seat(s)) | seats: {seats} "
                    f"({convictions}){no_lean} | invalid if — {invalidation}"
                )
        else:
            lines.append("(no name passes every pre-decision rule today)")
        if blocked:
            lines.append("")
            lines.append("Not ranked — refused by a rule, with the rule:")
            for symbol in sorted(blocked):
                lines.append(f"- {symbol}: {'; '.join(blocked[symbol])}")
        return "\n".join(lines)

    @staticmethod
    def rotation_precheck(
        *,
        ranked: list[RankedCandidate],
        blocked: dict[str, list[str]],
        held_symbols: set[str],
        existing_risk_pct: dict[str, float] | None,
        ceiling_pct: float,
        entry_budget_usd: float | None = None,
        min_order_usd: float | None = None,
    ) -> RotationPrecheck:
        """Phase 14 — run the opportunity-cost comparison once and keep its
        inputs. `_render_rotation_section` renders this for the prompt;
        `DecisionStage` reads the same object to decide whether to act
        (Phase 14b). See `src/rotation.py`.

        `existing_risk_pct` is the book's risk BEFORE anything this session
        proposes — the same map `PortfolioConstructor` rations against
        (`src/pipeline_stages.py::_book_risk_inputs`). `None` means the
        book's risk is not visible this session (facts unavailable); the
        check is skipped rather than run against a fabricated "book is
        empty" view, the same fail-open posture `allocate_risk_budget`
        itself already requires of every caller.

        `entry_budget_usd` is `_entry_deployment_budget`'s own figure — the
        dollars EXECUTION will size this session's entries against, already
        carrying the §11.2 gross ladder, settled cash and the min of the two
        — and `min_order_usd` the §10.3 `cash_sweep.min_order_usd` floor
        under the smallest order the desk will place. Together they are the
        funding half of the precondition (2026-09-23; see
        `src/rotation.py`'s docstring for the measurement that added it).
        Both `None` means the funding view was not resolvable this session,
        and the funding test is then simply absent — this agent never
        derives either number itself, for the same reason the Margin
        Capacity section does not.
        """
        held_below = holdings_below_entry_bar(blocked, held_symbols)
        if existing_risk_pct is None:
            return RotationPrecheck(
                opportunity=None, headroom_pct=0.0, ceiling_pct=ceiling_pct,
                floor_pct=STARTER_POSITION_RISK_PCT, telemetry_available=False,
                entry_budget_usd=entry_budget_usd, min_order_usd=min_order_usd,
                held_below_entry_bar=held_below,
            )
        headroom_pct = allocate_risk_budget(
            [], existing_pct=existing_risk_pct, clusters=None,
            ceiling_pct=ceiling_pct, floor_pct=STARTER_POSITION_RISK_PCT,
        ).headroom_pct
        outcome = evaluate_rotation(
            ranked=ranked, blocked=blocked, held_symbols=held_symbols,
            headroom_pct=headroom_pct, floor_pct=STARTER_POSITION_RISK_PCT,
            entry_budget_usd=entry_budget_usd, min_order_usd=min_order_usd,
        )
        opportunity: RotationOpportunity | None = outcome.opportunity
        return RotationPrecheck(
            opportunity=opportunity, headroom_pct=headroom_pct,
            ceiling_pct=ceiling_pct, floor_pct=STARTER_POSITION_RISK_PCT,
            refusal=outcome.refusal,
            entry_budget_usd=entry_budget_usd, min_order_usd=min_order_usd,
            binding=(
                () if outcome.refusal is None else outcome.refusal.binding
            ) or rotation_binding_constraints(
                headroom_pct=headroom_pct,
                floor_pct=STARTER_POSITION_RISK_PCT,
                entry_budget_usd=entry_budget_usd, min_order_usd=min_order_usd,
            ),
            held_below_entry_bar=held_below,
        )

    @staticmethod
    def _rotation_constraint_line(
        precheck: RotationPrecheck, *, ceiling_pct: float,
    ) -> str:
        """The one sentence naming WHICH limit has the book pinned.

        2026-09-23. The old wording said "capital is constrained" and then
        quoted only the risk budget, because the risk budget was the only
        thing the pre-check looked at. Now that the funding view can be the
        binding one — and on this book it usually is, while the risk budget
        almost never is — the sentence has to say which, or the model reads
        a number that is not the one stopping it and plans around the wrong
        limit. That mis-statement is the same class of defect as the
        2026-09-17 CRM incident, where the prompt said no margin was
        deployable while $11.4k of ladder headroom existed.
        """
        parts: list[str] = []
        if "risk_budget" in precheck.binding:
            parts.append(
                f"only {precheck.headroom_pct:.2f}% risk headroom against "
                f"the {ceiling_pct:.2f}% ceiling (existing book only, "
                "before anything you propose today), under the "
                f"{precheck.floor_pct:.2f}% minimum tradeable size"
            )
        if "funding" in precheck.binding:
            budget = precheck.entry_budget_usd
            floor = precheck.min_order_usd
            parts.append(
                f"only ${budget:,.2f} still deployable for new entries (the §11.2 "
                "ladder-and-cash budget execution sizes entries against), "
                f"under the ${floor:,.0f} minimum order worth placing — so "
                "no new position can be funded at all without freeing "
                "capital first"
                if isinstance(budget, (int, float))
                and isinstance(floor, (int, float))
                else "the deployable budget will not fund a new order"
            )
        if not parts:
            # Unreachable while callers check `precheck.binding` first; here
            # so a future caller that does not gets a true sentence rather
            # than an assertion of constraint that was never established.
            return (
                f"{precheck.headroom_pct:.2f}% risk headroom against the "
                f"{ceiling_pct:.2f}% ceiling; no constraint is binding."
            )
        return "Capital is constrained — " + " and ".join(parts) + "."

    @classmethod
    def _render_rotation_section(
        cls,
        *,
        ranked: list[RankedCandidate],
        blocked: dict[str, list[str]],
        held_symbols: set[str],
        existing_risk_pct: dict[str, float] | None,
        ceiling_pct: float,
        precheck: RotationPrecheck | None = None,
        execute_enabled: bool = False,
        #: Board item 39 — `execution.rotation_ranked_margin_enabled`, the
        #: SECOND switch. `execute_enabled` alone still means the
        #: categorical tier only, exactly as before.
        ranked_margin_enabled: bool = False,
    ) -> str:
        """Phase 14 — the opportunity-cost comparison, surfaced as
        information. See `src/rotation.py` for the rule, the margin and the
        citations behind it.

        `precheck` is the already-computed comparison (`rotation_precheck`);
        omitted, it is computed here from the same inputs. `execute_enabled`
        (Phase 14b, `execution.rotation_enabled`) only changes the WORDING:
        when the desk itself may act on the categorical tier, the model is
        told so, so its own plan can account for it — the decision to act is
        made in `DecisionStage`, never here.
        """
        header = "## Opportunity Rotation (deterministic pre-check, Phase 14)"
        if precheck is None:
            precheck = cls.rotation_precheck(
                ranked=ranked, blocked=blocked, held_symbols=held_symbols,
                existing_risk_pct=existing_risk_pct, ceiling_pct=ceiling_pct,
            )
        if not precheck.telemetry_available:
            return (
                f"{header}\n"
                "(book risk telemetry unavailable this session — rotation "
                "check skipped, same as every other consumer of this data)"
            )
        headroom_pct = precheck.headroom_pct
        opportunity = precheck.opportunity
        constraint_line = cls._rotation_constraint_line(
            precheck, ceiling_pct=ceiling_pct,
        )
        if opportunity is None:
            if precheck.binding:
                return (
                    f"{header}\n"
                    f"{constraint_line} But no eligible new candidate "
                    "outranks a held position by enough to recommend "
                    "trimming one for the other. Nothing to surface."
                )
            if funding_view_measured(
                precheck.entry_budget_usd, precheck.min_order_usd,
            ):
                return (
                    f"{header}\n"
                    f"{headroom_pct:.2f}% risk headroom left against the "
                    f"{ceiling_pct:.2f}% ceiling, and "
                    f"${precheck.entry_budget_usd:,.2f} is still deployable "
                    f"for new entries against a ${precheck.min_order_usd:,.0f} "
                    "minimum order — real room exists on every constraint, "
                    "so there is nothing to rotate for."
                )
            # Adversary review 2026-09-23: do NOT tell a seat that can sell
            # that room exists on a constraint that was never measured.
            return (
                f"{header}\n"
                f"{headroom_pct:.2f}% risk headroom left against the "
                f"{ceiling_pct:.2f}% ceiling. The session's deployable-entry "
                "budget could NOT be read, so the funding constraint was not "
                "tested and this check covers the risk budget only."
            )
        lines = [header, constraint_line]
        if opportunity.tier == "ineligible_hold":
            reasons = "; ".join(opportunity.reasons)
            lines.append(
                f"{opportunity.held_symbol} is currently held but would "
                f"NOT be bought today — it fails this desk's own entry "
                f"rules ({reasons}). {opportunity.new_symbol} ranks "
                f"{opportunity.new_score:.2f} and clears every rule, but "
                "there is no room to buy it without freeing capital first."
            )
        else:
            # `docs/INCIDENT_HISTORY.md` 2026-09-14: the composite score
            # is a weighted sum
            # over whichever seats cover each name today, so the two
            # totals are NOT comparable term-for-term. The margin was
            # cleared on both the totals and on the seats that scored both
            # names; state the second one, because it is the comparison
            # that is actually like-for-like.
            lines.append(
                f"{opportunity.held_symbol} is the weakest still-eligible "
                f"held position (score {opportunity.held_score:.2f}). "
                f"{opportunity.new_symbol} ranks {opportunity.new_score:.2f}, "
                f"at least {opportunity.margin_pct:.0%} higher — a real "
                "margin, not a noise-level difference (cross-sectional "
                "replacement-rule convention; see src/rotation.py) — but "
                "there is no room to buy it without freeing capital first."
            )
        if (
            opportunity.tier == "ranked_margin"
            and opportunity.held_shared_score is not None
            and opportunity.new_shared_score is not None
        ):
            shared = ", ".join(opportunity.shared_seats)
            lines.append(
                "Like-for-like check (this is the load-bearing one): those "
                "totals sum whichever seats cover each name TODAY, so a "
                "held name whose earnings or flow coverage has lapsed "
                "scores lower for that reason alone. On the seats that "
                f"scored BOTH names ({shared}), "
                f"{opportunity.held_symbol} is "
                f"{opportunity.held_shared_score:.2f} and "
                f"{opportunity.new_symbol} is "
                f"{opportunity.new_shared_score:.2f} — the same "
                f"{opportunity.margin_pct:.0%} margin clears there too, so "
                "this gap is not an artefact of coverage. Had it not, "
                "nothing would have been surfaced."
            )
        # Board item 39: the RANKED-MARGIN tier only, deliberately.
        #
        # "Doing nothing is another reasonable call" is not true once the
        # desk can close the name itself, and two adjacent paragraphs
        # claiming opposite things is a prompt that has rotted. That is
        # equally true of the CATEGORICAL tier, which is live today — but
        # rewording a live seat's prompt is a behaviour change on a path
        # that is trading, it is not required by this item, and it belongs
        # in a change a reviewer can judge on its own merits rather than
        # as a footnote in a sequencing fix. The categorical text below is
        # therefore byte-for-byte unchanged.
        desk_may_act = (
            ranked_margin_enabled and opportunity.tier == "ranked_margin"
        )
        if desk_may_act:
            lines.append(
                "This names the weakest thing currently using the room and "
                "the strongest thing there is no room for. Read the "
                "paragraph below before you plan: on this comparison the "
                "desk may close the held name ITSELF, so leaving it alone "
                "is not one of the outcomes. An edit to a held position "
                "still needs the same substantive justification any other "
                "exit does — this note is not one."
            )
        else:
            lines.append(
                "This is a comparison, not an instruction: it names the "
                "weakest thing currently using the room and the strongest "
                "thing there is no room for. Trimming or exiting "
                f"{opportunity.held_symbol} to fund "
                f"{opportunity.new_symbol} is one reasonable call; doing "
                "nothing is another. Either way, an edit to a held "
                "position needs the same substantive justification any "
                "other exit does — this note is not one."
            )
        if ranked_margin_enabled and opportunity.tier == "ranked_margin":
            # Board item 39. The desk can now act on THIS tier too, behind
            # its own second switch. The model must be told, or it sizes a
            # plan as though no room is being freed — and the rotation's
            # own arithmetic then depends on that plan. A prompt that is
            # silent about what the desk will do on its own is wrong in the
            # same way stale code is.
            lines.append(
                "AUTOMATIC ROTATION IS ENABLED for this ranked-margin case: "
                f"if you include a BUY target for {opportunity.new_symbol} "
                f"and do not yourself close {opportunity.held_symbol}, the "
                f"desk will propose a full close of {opportunity.held_symbol}"
                " on its own — but ONLY if its structural protection has "
                "already broken under the holding-discipline check, it was "
                "not bought today, nothing is in flight on it, AND the "
                f"replacement buy of {opportunity.new_symbol} still clears "
                "every gate that can be KNOWN before the sale (the "
                "daily-loss limit, a usable price, a fresh entry, a "
                "tradeable size, and the funding), measured against the "
                "book as it would be AFTER the sale. Some refusals are not "
                "knowable in advance — a stale quote at the moment of "
                "submission, a broker rejection — and those are not "
                "covered. If the buy would be refused on anything that IS "
                "knowable, NEITHER leg happens and the holding stays. That "
                "proposal goes through the Risk Manager like any other "
                "exit.\n"
                "Do NOT size your other BUYs against the room this would "
                f"free. Size {opportunity.new_symbol} for the position you "
                "want and size everything else against the book as it "
                "stands. If the session's entries together ask for more "
                "than the freed room can fund, the funding gate refuses "
                "this replacement and BOTH legs are withdrawn — so "
                "spending the same room twice does not get you a bigger "
                "trade, it gets you no rotation."
            )
        elif execute_enabled and opportunity.tier == "ineligible_hold":
            # Phase 14b. Wording only — the act itself is decided in
            # `DecisionStage._apply_rotation_execution` from the desk's own
            # data, after this prompt returns.
            lines.append(
                "AUTOMATIC ROTATION IS ENABLED for this categorical case: "
                f"if you include a BUY target for {opportunity.new_symbol} "
                f"and do not yourself close {opportunity.held_symbol}, the "
                f"desk will propose a full close of {opportunity.held_symbol} "
                "on its own — but ONLY if its structural protection has "
                "already broken under the holding-discipline check, it was "
                "not bought today and nothing is in flight on it. That "
                "proposal then goes through the Risk Manager like any other "
                "exit. Size your plan for the room it would free; do not "
                "assume it will happen."
            )
        return "\n".join(lines)

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
               smart_money_findings: list[SmartMoneyFinding] | None = None,
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
               blocked_proposals: str = "",
               facts=None,
               allow_margin: bool = True,
               # §11.2 ladder headroom, threaded from the SAME computation
               # execution's submit loop uses (`_entry_deployment_budget` /
               # `_session_gross_ceiling` in `src/pipeline_stages.py`) so the
               # Margin Capacity section never derives its own number.
               # `margin_ladder_backed=False` means that computation could
               # not resolve this session — the section says so rather than
               # showing a stale or invented figure.
               margin_headroom_usd: float | None = None,
               margin_ladder_backed: bool = False,
               margin_ladder_multiple: float | None = None,
               margin_ladder_rung: str | None = None,
               # Board item 95: the annual margin-interest rate the account
               # is actually charged on its OVERNIGHT debit, threaded from
               # the caller's already-loaded `config.risk`. `None` means the
               # cost of carry is simply not stated — never guessed.
               margin_interest_rate_pct: float | None = None,
               # 2026-09-23: the §10.3 `cash_sweep.min_order_usd` floor, the
               # smallest order this desk will place. Threaded rather than
               # defaulted to a literal so the rotation pre-check tests the
               # DEPLOYED floor, not a second copy of it. `None` switches
               # the funding half of the rotation precondition off.
               min_order_usd: float | None = None,
               symbol_sectors: dict[str, str] | None = None,
               session_type: str = "morning",
               allowed_buy_symbols: set[str] | None = None,
               transient_admitted_symbols: set[str] | None = None,
               # The sub-floor catalyst gate's two thresholds. Defaults are
               # the shared constants `RiskConfig` itself defaults to, so a
               # caller that does not thread config (the model-policy
               # harness, most tests) gates on exactly the production
               # numbers rather than on a second opinion about them.
               rr_floor: float = REWARD_RISK_FLOOR,
               starter_risk_pct: float = STARTER_POSITION_RISK_PCT,
               # Phase 14 (opportunity-cost rotation): the EXISTING book's
               # per-symbol risk (before anything this session proposes) and
               # the total-risk ceiling it is rationed against — the same
               # inputs `PortfolioConstructor` rations orders against
               # (`src/pipeline_stages.py::_book_risk_inputs`). `None` for
               # `existing_risk_pct` disables the rotation check for this
               # session rather than running it against a fabricated
               # "book is empty" view — see `_render_rotation_section`.
               existing_risk_pct: dict[str, float] | None = None,
               max_portfolio_risk_pct: float = 25.0,
               # Phase 14b: whether `execution.rotation_enabled` is on.
               # Wording only — tells the model the desk may itself close
               # a categorically-ineligible holding this session; the act
               # is decided in `DecisionStage`, never in this prompt.
               rotation_execute_enabled: bool = False,
               rotation_ranked_margin_enabled: bool = False,
               # 2026-09-04 fix: the SAME real derived reward:risk
               # `PortfolioConstructor.construct_orders` gates on,
               # keyed by upper-case symbol — see `candidate_eligibility`
               # and `_apply_subfloor_catalyst_rule` for why this replaces
               # `TechAnalysisResult.risk_reward` at both eligibility gates.
               # `None` (the default) falls back to that field, for the rare
               # caller with no `PortfolioConstructor` to preview from.
               real_reward_risk_by_symbol: dict[str, float | None] | None = None,
               # Item 54 (2026-09-12): the constructor's structured refusals
               # from the same preview pass, so eligibility rule R6 can name
               # a candidate the one shared funnel has already refused.
               constructor_refusals_by_symbol: dict[str, dict[str, str]] | None = None,
               # Board item 133 (2026-09-18): the ONE bookkeeping re-ask
               # `DecisionStage` may make when this seat dropped a candidate
               # without naming a ground. Empty on every ordinary call.
               accounting_challenge: str = "",
               ) -> tuple[PortfolioDecision | None, "AgentResult"]:
        # One fill retry per decide() call — the agent is long-lived across
        # morning/midday/close. A morning miss must not spend the close's
        # shot, and a spent flag must not skip a later session.
        self._soft_exit_retry_used = False
        # Board item 164 (2026-09-19): every target this call removes after
        # the model answered — malformed, or carrying an unadjudicated seat
        # conflict — with the gate and the reason, for `DecisionStage` to
        # persist per symbol. Reset per call; recording only.
        self.last_dropped_targets = []
        # Board item 78 (2026-09-26): what the soft-exit heal ACTUALLY did
        # to each open/increase name that arrived without a falsifier —
        # filled, blocked by the spend cap, never attempted for want of a
        # replayable message, errored, or answered without one. Reset per
        # call; recording only. `DecisionStage` drains it and files one
        # durable row per symbol, and the blank-falsifier refusal quotes it
        # instead of asserting a retry that may never have run.
        self.last_soft_exit_heals: dict[str, dict[str, str]] = {}
        result = self.run(
            analyses=analyses,
            positions=positions,
            macro_analysis=macro_analysis,
            cash_balance=cash_balance,
            reserve_balance=reserve_balance,
            total_value=total_value,
            news_intel=news_intel,
            earnings_analyses=earnings_analyses or [],
            smart_money_findings=smart_money_findings or [],
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
            blocked_proposals=blocked_proposals,
            facts=facts,
            allow_margin=allow_margin,
            margin_headroom_usd=margin_headroom_usd,
            margin_ladder_backed=margin_ladder_backed,
            margin_ladder_multiple=margin_ladder_multiple,
            margin_ladder_rung=margin_ladder_rung,
            margin_interest_rate_pct=margin_interest_rate_pct,
            min_order_usd=min_order_usd,
            symbol_sectors=symbol_sectors or {},
            session_type=session_type,
            allowed_buy_symbols=allowed_buy_symbols or set(),
            transient_admitted_symbols=transient_admitted_symbols or set(),
            # Phase 13: the candidate ranking shown in the prompt gates on
            # the same floor `_apply_subfloor_catalyst_rule` enforces after
            # submission, so the PM is ranked on the rule it is held to.
            rr_floor=rr_floor,
            # Phase 14: opportunity-cost rotation pre-check inputs.
            existing_risk_pct=existing_risk_pct,
            max_portfolio_risk_pct=max_portfolio_risk_pct,
            rotation_execute_enabled=rotation_execute_enabled,
            rotation_ranked_margin_enabled=rotation_ranked_margin_enabled,
            real_reward_risk_by_symbol=real_reward_risk_by_symbol,
            constructor_refusals_by_symbol=constructor_refusals_by_symbol,
            accounting_challenge=accounting_challenge,
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
            parsed = self._drop_invalid_targets(
                parsed, dropped=self.last_dropped_targets,
            )
            parsed = self._drop_invalid_rejections(parsed)
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
            decision, result = self._fill_missing_open_falsifiers(
                decision, result, positions=positions, total_value=total_value,
                existing_risk_pct=existing_risk_pct,
            )
            # §9.3 — drop any target that OPENS/INCREASES exposure while
            # carrying an unadjudicated seat conflict, before grounding is
            # even checked. This is a per-target prune, not an error: it
            # must never join `validate_grounding`'s list (see that
            # method's non-empty-error contract — it fails the ENTIRE
            # session, not one target).
            decision = self._drop_unadjudicated_conflicts(
                decision, positions=positions, total_value=total_value,
                existing_risk_pct=existing_risk_pct,
                dropped=self.last_dropped_targets,
            )
            # The sub-floor catalyst gate. Same per-target-prune contract as
            # the conflict drop above and applied in the same place, before
            # grounding: a target this rule removes must not be able to fail
            # the whole session on its way out.
            decision = self._apply_subfloor_catalyst_rule(
                decision, analyses=analyses, positions=positions,
                total_value=total_value,
                active_state_changes=active_state_changes,
                rr_floor=rr_floor, starter_risk_pct=starter_risk_pct,
                real_reward_risk_by_symbol=real_reward_risk_by_symbol,
                existing_risk_pct=existing_risk_pct,
            )
            errors = self.validate_grounding(
                decision, analyses=analyses, positions=positions,
                news_intel=news_intel,
                earnings_analyses=earnings_analyses or [],
                macro_analysis=macro_analysis, total_value=total_value,
                smart_money_findings=smart_money_findings or [],
                symbol_sectors=symbol_sectors or {},
                allowed_buy_symbols=allowed_buy_symbols,
                existing_risk_pct=existing_risk_pct,
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
                # The repaired answer replaces the first one, so its drops
                # replace the first attempt's in the record too.
                self.last_dropped_targets = []
                reparsed = self._drop_invalid_targets(
                    reparsed, dropped=self.last_dropped_targets,
                )
                reparsed = self._drop_invalid_rejections(reparsed)
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
                    decision, repaired = self._fill_missing_open_falsifiers(
                        decision, repaired, positions=positions,
                        total_value=total_value,
                        existing_risk_pct=existing_risk_pct,
                    )
                    # §9.3 — same per-target conflict prune as the
                    # first-attempt path, applied before grounding here too.
                    decision = self._drop_unadjudicated_conflicts(
                        decision, positions=positions, total_value=total_value,
                        existing_risk_pct=existing_risk_pct,
                        dropped=self.last_dropped_targets,
                    )
                    # Same sub-floor catalyst gate as the first-attempt path.
                    # A schema repair must not be a way around it.
                    decision = self._apply_subfloor_catalyst_rule(
                        decision, analyses=analyses, positions=positions,
                        total_value=total_value,
                        active_state_changes=active_state_changes,
                        rr_floor=rr_floor, starter_risk_pct=starter_risk_pct,
                        real_reward_risk_by_symbol=real_reward_risk_by_symbol,
                        existing_risk_pct=existing_risk_pct,
                    )
                    errors = self.validate_grounding(
                        decision, analyses=analyses, positions=positions,
                        news_intel=news_intel,
                        earnings_analyses=earnings_analyses or [],
                        macro_analysis=macro_analysis, total_value=total_value,
                        smart_money_findings=smart_money_findings or [],
                        symbol_sectors=symbol_sectors or {},
                        allowed_buy_symbols=allowed_buy_symbols,
                        existing_risk_pct=existing_risk_pct,
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

    @staticmethod
    def _target_intent(
        target: TargetPosition, held: dict[str, Position], total_value: float,
        existing_risk_pct: dict[str, float] | None = None,
    ) -> str:
        """"buy" / "short" (opens or increases exposure) vs "sell" (exits or
        reduces it).

        The single definition both `validate_grounding` (does this claim's
        polarity support the action?) and §9.3's
        `_drop_unadjudicated_conflicts` (is this target even in scope for
        conflict adjudication?) classify a target by — factored out so the
        two can never disagree about what counts as an increase.

        Risk-based targets (spec §2.1) state risk, not weight, so they are
        compared against the holding's CURRENT risk: `existing_risk_pct`,
        the per-symbol stop-based budget risk (% of equity) from the heat
        roll-up (`src/risk/metrics.py`) — the same "equity at-risk" figure
        the PM is shown per holding and the constructor rations against
        (`src/pipeline_stages.py::_book_risk_inputs`). A request strictly
        BELOW that figure, on a name held on the SAME side, is a trim
        ("sell"). Anything else — not held, held on the other side, or a
        request at or above current risk — is an increase.

        Fail safe: when current risk cannot be determined for this holding
        (no map, or no entry for the symbol), the target is classified as an
        INCREASE, the stricter treatment in both callers. 2026-09-17
        incident (intra_check-44594a05): before current risk was consulted,
        EVERY non-zero risk target was an increase, so an intraday funding
        trim of a held name the scan had not analysed failed "increase lacks
        a current-run Technical analysis" and rejected the whole plan,
        including a valid new entry. `is_close` (zero risk, or a legacy zero
        weight) is always a full exit.
        """
        symbol = target.symbol.upper()
        pos = held.get(symbol)
        current_weight = 0.0
        if pos is not None and total_value > 0:
            current_weight = weight_pct_of(pos.market_value, symbol, total_value)
        if target.risk_allocation_pct is not None:
            if target.is_close:
                return "sell"
            increase = "short" if target.direction == "short" else "buy"
            if pos is None or existing_risk_pct is None or not pos.qty:
                return increase
            held_side = "short" if pos.qty < 0 else "long"
            if held_side != target.direction:
                return increase
            current_risk = {
                str(k).upper(): v for k, v in existing_risk_pct.items()
            }.get(symbol)
            if current_risk is None:
                return increase
            return "sell" if target.risk_allocation_pct < current_risk else increase
        return "buy" if (target.target_weight_pct or 0.0) > current_weight + 0.01 else "sell"

    @classmethod
    def validate_grounding(
        cls, decision: PortfolioDecision, *, analyses: list[TechAnalysisResult],
        positions: list[Position], news_intel: NewsIntelligenceReport | None,
        earnings_analyses: list[dict], macro_analysis: dict | None,
        total_value: float, symbol_sectors: dict[str, str] | None = None,
        smart_money_findings: list[SmartMoneyFinding] | None = None,
        allowed_buy_symbols: set[str] | None = None,
        existing_risk_pct: dict[str, float] | None = None,
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
            smart_money_findings=smart_money_findings or [],
            symbol_sectors=symbol_sectors or {},
        )
        # 2026-09-11 redesign: `support_eligible` is now purely STRUCTURAL
        # (real, single-direction, legally-disclosed evidence) — it no
        # longer expires on a calendar. Whether an aged smart-money finding
        # may actually support a target is instead decided per-target,
        # below, by whether it CORRELATES with at least one other CURRENT
        # source's stance on the same symbol — one piece of information
        # among several, weighted by agreement, never an island judged on
        # its own age. See `SmartMoneyFinding.deterministic_eligibility`.
        smart_money_structurally_eligible: dict[str, bool] = {}
        for finding in smart_money_findings or []:
            symbol = finding.symbol.upper()
            smart_money_structurally_eligible[symbol] = (
                smart_money_structurally_eligible.get(symbol, False)
                or finding.support_eligible
            )
        reasoning_text = "\n".join(
            str(value) for value in decision.reasoning_chain.model_dump().values()
        )

        for target in decision.targets:
            symbol = target.symbol.upper()
            pos = held.get(symbol)
            if target.is_close and pos is None:
                errors.append(f"{symbol}: close/exit target is not an actual holding")
            if not target.provenance:
                errors.append(f"{symbol}: target has no structured specialist provenance")
                continue

            # Risk-based targets (spec §2.1) state risk, not weight.
            # `_target_intent` compares them against the holding's current
            # stop-based risk (`existing_risk_pct`): below it on the same
            # side is a trim; everything else — and any holding whose current
            # risk is unknown — is an INCREASE, a BUY for a long and a SHORT
            # for a short (Stage 3). The increase branch below applies the
            # STRICTER checks (universe membership, an actual technical
            # analysis backing the name) to both sides alike. §9.3's conflict
            # adjudication (`_drop_unadjudicated_conflicts`) reuses this same
            # classification for its own "opens or increases" scope, so the
            # two never disagree about what counts as an increase.
            intent = cls._target_intent(
                target, held, total_value, existing_risk_pct=existing_risk_pct,
            )
            if intent in ("buy", "short"):
                if allowed_buy_symbols is not None and symbol not in {
                    str(item).strip().upper() for item in allowed_buy_symbols
                }:
                    errors.append(
                        f"{symbol}: increase is outside the configured universe and "
                        "the deterministic temporary-admission allowlist"
                    )
                # Current-run Technical is a hard gate on increases. Missing
                # Tech is a producing-step defect (the intraday scan must
                # include held names in the paid batch), not a reason to
                # drop the name here and keep the rest of the book.
                if symbol not in {analysis.symbol.upper() for analysis in analyses}:
                    errors.append(
                        f"{symbol}: increase lacks a current-run Technical analysis"
                    )
            expected_sources = registry.get(symbol, {})
            # Correlation, not calendar age: smart_money may support THIS
            # target only if it is structurally real evidence AND at least
            # one OTHER current source (technical/news/earnings/macro)
            # already in the registry for this symbol independently points
            # the same direction. An insider trade with nothing else
            # backing it right now is still shown as context — it simply
            # doesn't get to count as support on its own, regardless of
            # whether it happened yesterday or three months ago.
            smart_money_correlates = smart_money_structurally_eligible.get(
                symbol, False,
            ) and any(
                stance_is_aligned(
                    other_source, symbol, other_stance,
                    wants_bullish=(intent == "buy"),
                )
                for other_source, other_stance in expected_sources.items()
                if other_source != "smart_money"
            )
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

                # Stage 3: "short" (opening/adding a short, direction=="short")
                # needs the same bearish-polarity evidence a "sell" (trimming
                # a long) does — both are bearish-direction actions on the
                # symbol. Only "buy" (opening/adding a long) needs bullish
                # evidence. `stance_is_aligned` (src/risk/rules.py) is the
                # SAME polarity rule §9.4's agreement-count ceiling uses —
                # one definition, not a second one that could quietly drift
                # from this one.
                polarity_supports = stance_is_aligned(
                    source, symbol, stance, wants_bullish=(intent == "buy"),
                )
                # A PARTIAL trim keeps a position. Evidence aligned with the
                # side still held supports HOLDING that remainder — trimming
                # a bullish long for concentration is coherent, and must not
                # need bearish evidence to be grounded (2026-09-17,
                # intra_check-44594a05: AAPL trimmed with bullish earnings
                # tagged "supports" rejected the whole plan). Either polarity
                # may therefore SUPPORT a partial trim. Full closes, opens and
                # increases are untouched, and the `conflicts` check below
                # still reads the reduction's own polarity.
                supports_retained = (
                    intent == "sell" and pos is not None and not target.is_close
                    and pos.qty != 0
                    and stance_is_aligned(
                        source, symbol, stance, wants_bullish=pos.qty > 0,
                    )
                )
                if claim.relationship == "supports":
                    if source == "smart_money" and not smart_money_correlates:
                        errors.append(
                            f"{symbol}: smart-money evidence with nothing else "
                            "currently corroborating it cannot support a target "
                            "on its own; use context"
                        )
                        continue
                    if not (polarity_supports or supports_retained):
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
                elif (
                    claim.relationship == "context"
                    and stance not in {"neutral", "mixed"}
                    and source != "macro"
                    and not (
                        source == "smart_money"
                        and not smart_money_correlates
                    )
                ):
                    errors.append(
                        f"{symbol}: directional {source} stance {stance!r} must be "
                        "marked supports or conflicts, not context"
                    )

            # Dynamic N/M alignment covers the core evidence sources actually
            # available for this symbol. Optional smart-money context remains
            # explicit provenance but does not dilute the established
            # technical/news/earnings/macro denominator.
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
                    available_sources = set(expected_sources) - {"smart_money"}
                    seen_alignment_sources = seen_sources & available_sources
                    supporting_alignment_sources = supporting_sources & available_sources
                    if stated_available != len(available_sources):
                        errors.append(
                            f"{symbol}: claims denominator {stated_available}, but "
                            f"{len(available_sources)} current source(s) are available"
                        )
                    elif seen_alignment_sources != available_sources:
                        errors.append(
                            f"{symbol}: alignment shorthand requires provenance for all "
                            f"available sources {sorted(available_sources)!r}"
                        )
                    elif stated_support != len(supporting_alignment_sources):
                        errors.append(
                            f"{symbol}: claims {stated_support}/{stated_available} aligned "
                            f"but provenance proves "
                            f"{len(supporting_alignment_sources)}/{stated_available}"
                        )
        return errors

    # §9.3 "disagreement must be adjudicated" ------------------------------
    #
    # `source` values that need a plainer English alias to be recognised in
    # free-form prose. The four other sources (technical/news/earnings/
    # macro) are themselves ordinary words; `smart_money` is normally
    # written "smart money" by a model composing a sentence, so it is
    # aliased explicitly rather than guessed at by a second rule.
    _CONFLICT_SOURCE_ALIASES = {
        "smart_money": ("smart_money", "smart money", "smart-money"),
    }

    @classmethod
    def _conflict_is_named(cls, signal_conflicts: str, symbol: str, source: str) -> bool:
        """Whether `signal_conflicts` names BOTH `symbol` and `source`.

        SPECIFICITY OF REFERENCE ONLY — this is what `_drop_unadjudicated_
        conflicts` below checks for, and it proves the PM's text names the
        symbol and the source, NOT that its reasoning about the conflict is
        any good. A bland-but-specific sentence ("NVDA: macro is bearish
        but we are buying on the earnings beat") satisfies it. That is a
        strictly lower bar than "the desk resolved the disagreement," and
        it must never be described as more than that — this is still
        stronger than today, where a recorded conflict can go entirely
        unmentioned and the trade proceeds unchanged.

        Word-boundary, case-insensitive match on the symbol so a substring
        can't accidentally satisfy it (e.g. "V" inside "INVALID", or "DE"
        inside "TRADE"). `source` matches case-insensitively by substring
        against its alias list.
        """
        text = signal_conflicts or ""
        if not re.search(rf"\b{re.escape(symbol)}\b", text, flags=re.IGNORECASE):
            return False
        text_lower = text.lower()
        aliases = cls._CONFLICT_SOURCE_ALIASES.get(source, (source,))
        return any(alias in text_lower for alias in aliases)

    @classmethod
    def _drop_unadjudicated_conflicts(
        cls, decision: PortfolioDecision, *, positions: list[Position], total_value: float,
        existing_risk_pct: dict[str, float] | None = None,
        dropped: list[dict] | None = None,
    ) -> PortfolioDecision:
        """An unresolved seat conflict on a target that OPENS or INCREASES
        exposure drops THAT ONE TARGET; it never fails the whole session.

        This is deliberately NOT implemented by appending to
        `validate_grounding`'s error list: `decide()` treats ANY non-empty
        error list as total session failure via `_semantic_failure`,
        discarding every target and the whole book. That is the right
        penalty for a decision that fabricates evidence, but the wrong one
        for a single candidate carrying one unaddressed disagreement — the
        punishment has to fit the offence. This mirrors two existing
        precedents instead: per-target isolation
        (`_drop_invalid_targets`/PR #73-#74) and Phase 3.3's exit gate,
        which drops one exit and logs `exit_blocked_no_named_trigger`
        rather than failing the run (see `src/pipeline.py`,
        `_reason_cites_hard_trigger`).

        HONESTY NOTE — read `_conflict_is_named`'s docstring before
        touching this. It enforces SPECIFICITY OF REFERENCE, not QUALITY
        OF REASONING. Do not describe this method's effect as "the desk
        resolves its disagreements" anywhere it is discussed.

        Scope, deliberately asymmetric (mirrors §3.4's exit-side
        asymmetry): only targets classified `_target_intent in ("buy",
        "short")` — opening or increasing — are subject to this. Exits
        and reductions are exempt; this desk must never find it harder to
        cut risk than to add it.
        """
        held = {p.symbol.upper(): p for p in positions}
        signal_conflicts = decision.reasoning_chain.signal_conflicts
        kept: list[TargetPosition] = []
        for target in decision.targets:
            intent = cls._target_intent(
                target, held, total_value, existing_risk_pct=existing_risk_pct,
            )
            if intent not in ("buy", "short"):
                kept.append(target)  # exits/reductions are exempt on purpose
                continue
            conflicting_sources = sorted({
                claim.source for claim in target.provenance
                if claim.relationship == "conflicts"
            })
            unaddressed = [
                source for source in conflicting_sources
                if not cls._conflict_is_named(signal_conflicts, target.symbol, source)
            ]
            if unaddressed:
                logger.warning(
                    "%s: dropping %s (%s) — signal_conflicts does not name "
                    "both the symbol and %s. A recorded conflict on a name "
                    "being opened/increased must be individually addressed "
                    "in signal_conflicts (symbol + source) or the target is "
                    "dropped, not traded; the rest of this session's "
                    "decision is unaffected. signal_conflicts was: %r",
                    CONFLICT_UNADJUDICATED_STATUS, target.symbol, intent,
                    unaddressed, signal_conflicts[:300],
                )
                # Board item 164: `dropped` is the optional per-symbol sink
                # `decide()` hands in so the drop is persisted, not only
                # logged. Recording only — the drop above is unchanged.
                if dropped is not None:
                    dropped.append({
                        "symbol": target.symbol,
                        "gate": CONFLICT_UNADJUDICATED_STATUS,
                        "intent": intent,
                        "unaddressed_sources": list(unaddressed),
                        "reason": (
                            f"{target.symbol} target ({intent}) DROPPED: the "
                            f"target records a conflict from "
                            f"{', '.join(unaddressed)} that signal_conflicts "
                            f"does not address by naming both the symbol and "
                            f"the source; a name being opened or increased "
                            f"must address every recorded conflict."
                        ),
                    })
                continue
            kept.append(target)
        decision.targets = kept
        return decision

    # --- The sub-floor catalyst gate (2026-09-02) -------------------------
    #
    # WHAT WENT WRONG. The prompt sets a reward:risk floor and permits a
    # below-floor pick that names a catalyst. Benchmarked 2026-09-01 on the
    # real opportunity set of the zero-trade day (`run-64290730`), both
    # candidate models picked NVDA at R/R 1.03 in 9 of 9 runs and passed over
    # GEV, which cleared the floor. THE MODELS DID NOT DISOBEY: every
    # sub-floor pick named a catalyst, cut size, and said in plain text that
    # the ratio was below floor. The rule-compliance grader passed them and
    # the risk manager agreed.
    #
    # The hole is in the RULE. For any mega-cap the news feed always carries
    # a concrete catalyst, so an assertable exception is a null constraint on
    # exactly the names it most needs to bind. Worse, the desk's own
    # `active_state_changes` block fed the PM two HIGH-conviction bullish
    # NVDA items that morning, which became the catalyst justifying the
    # exception — the accountability machinery supplying the key to its own
    # lock. The live run's recorded NVDA catalyst was a $3B SB Energy
    # investment that appears in NO state-change row at all.
    #
    # So this is a code problem, not a model problem, and a tenth firmly
    # worded sentence in a 52KB prompt whose ninth was obeyed is not a
    # design. Two deterministic changes, both AFTER the PM submits:
    #   1. the catalyst must RESOLVE to a specific `active_state_changes`
    #      row (this table already carries dates and symbols), or the
    #      exception does not apply and the target is dropped;
    #   2. a sub-floor pick that does resolve is capped at the smallest
    #      starter size the desk can hold.
    # Costs nothing when the catalyst is real; costs the slot when it is
    # decorative.

    @staticmethod
    def _state_change_symbols_by_date(
        active_state_changes: str, asof: date | None = None,
    ) -> dict[str, dict[str, set[str]]]:
        """Parse the rendered `active_state_changes` block into
        `{iso_date: {SYMBOL: {direction, ...}, ...}}`.

        The block the PM is shown is built by
        `TradingPipeline._build_active_state_changes`, which is the only
        producer of this format, so parsing its own output back is a
        round-trip over a format this repo owns end to end — not an attempt
        to read arbitrary prose. A line that does not match is skipped
        rather than raising: an unparseable row must narrow what can be
        cited, never fail the session.

        DIRECTION. Each symbol is rendered as `SYMBOL(direction)` (Phase 13
        catalyst-gate fix — see `StateChange.symbol_direction` in
        src/models.py). A symbol rendered without a recognized
        `(bullish|bearish|neutral)` suffix — including the `(unknown)` the
        producer writes for a symbol with no recorded direction — is still
        recorded (so the "row exists and names this symbol" fact is not
        lost) but with an empty direction set, which cannot satisfy
        `_catalyst_cites_state_change`'s directional check. Fail closed:
        an undirected mention proves the row exists, not that it agrees
        with the trade.

        Rows sharing a date are UNIONED per symbol (a symbol's direction
        set can pick up entries from more than one same-day row). A
        citation therefore proves "a HIGH-conviction state change affecting
        this symbol in THIS direction was recorded on this date", which is
        the checkable claim; it does not distinguish two same-day rows
        about the same name, and it does not need to.

        RECENCY. A row older than `ACTIVE_STATE_CHANGE_WINDOW_DAYS`, or dated
        in the future, is dropped. This is redundant TODAY — the producer
        scans exactly that window, so it cannot render an older row — and it
        is here so it stays true: the age bound is currently a property of
        one function in `pipeline.py`, and if that ever drifts, the thing
        that silently widens is what counts as a catalyst. It reuses the
        producer's own constant rather than choosing a second number.
        `asof` defaults to the trading calendar's today; if that cannot be
        read the block resolves to NOTHING, so the exception becomes
        unavailable rather than unbounded.
        """
        if asof is None:
            try:
                asof = et_today()
            except Exception as exc:  # pragma: no cover - clock/tz failure
                logger.warning(
                    "%s: cannot read today's date (%s) — no catalyst citation "
                    "can be aged, so none is honoured this session.",
                    SUBFLOOR_CATALYST_UNVERIFIED_STATUS, exc,
                )
                return {}
        by_date: dict[str, dict[str, set[str]]] = {}
        for line in (active_state_changes or "").splitlines():
            match = _STATE_CHANGE_ROW_RE.match(line)
            if match is None:
                continue
            try:
                row_date = date.fromisoformat(match.group(1))
            except ValueError:
                continue
            age = (asof - row_date).days
            if age < 0 or age > ACTIVE_STATE_CHANGE_WINDOW_DAYS:
                # Stale, or dated ahead of the session. Either way it cannot
                # be what a trade taken today is reacting to.
                continue
            # Split on the LAST arrow: the event prose can contain one, the
            # symbol list cannot.
            rest = match.group("rest")
            if "→" not in rest:
                continue
            _event, _, symbol_text = rest.rpartition("→")
            row_symbols: dict[str, str | None] = {}
            for part in symbol_text.split(","):
                part = part.strip()
                if not part or part == "—":
                    continue
                m = _SYMBOL_DIRECTION_RE.match(part)
                if m:
                    row_symbols[m.group(1).upper()] = m.group(2).lower()
                else:
                    # Legacy row with no `(direction)` suffix at all — the
                    # symbol is still recorded (existence), just with no
                    # direction to offer the gate.
                    row_symbols[part.upper()] = None
            if not row_symbols:
                # `_build_active_state_changes` writes an em dash when the
                # news analyst attached no affected symbols. A market-wide
                # row names nobody, so it can back nobody.
                continue
            date_bucket = by_date.setdefault(match.group(1), {})
            for symbol, direction in row_symbols.items():
                symbol_directions = date_bucket.setdefault(symbol, set())
                if direction in ("bullish", "bearish", "neutral"):
                    symbol_directions.add(direction)
        return by_date

    @classmethod
    def _catalyst_cites_state_change(
        cls, catalyst: str, symbol: str, required_direction: str,
        by_date: dict[str, dict[str, set[str]]],
    ) -> bool:
        """Does `catalyst` resolve to a state-change row that names `symbol`
        with a direction that actually supports this trade?

        The citation is a DATE + SYMBOL pair, because that is what the table
        already carries — there is no row id to cite (`news_store.
        recent_state_changes` dedupes on the event string and has never
        emitted one). The symbol half is the target's own symbol, which a
        target cannot misstate without being about a different name, so the
        model only has to supply the date.

        `required_direction` is `"bullish"` for a long and `"bearish"` for a
        short (see the call site in `_apply_subfloor_catalyst_rule`, which
        derives it from `_target_intent`). A `"neutral"` direction, or a
        symbol with no recorded direction at all, does NOT satisfy either
        requirement — fail closed, same posture as everything else in this
        gate.

        HONESTY NOTE, and read it before describing this anywhere (Phase 13
        catalyst-gate fix, 2026-09-03 — this replaced an EXISTENCE-only
        check): this now proves the cited row EXISTS, COVERS THIS NAME, and
        is recorded in the DIRECTION this trade needs. It still does not
        prove the PM's prose about the row is any good, or that the news
        analyst's direction call was correct — same posture as
        `_conflict_is_named`: specificity and substance of the checkable
        claim, not quality of reasoning or ground truth. What it removes is
        the free-text assertion that no reader could ever check, and — as
        of this fix — the ability for a stock moving on genuinely bad news
        to walk through the door meant for good news (or vice versa for a
        short).
        """
        text = (catalyst or "").strip()
        if not text:
            return False
        symbol = symbol.strip().upper()
        return any(
            required_direction in by_date.get(cited, {}).get(symbol, set())
            for cited in _ISO_DATE_RE.findall(text)
        )

    @classmethod
    def _apply_subfloor_catalyst_rule(
        cls, decision: PortfolioDecision, *,
        analyses: list[TechAnalysisResult],
        positions: list[Position],
        total_value: float,
        active_state_changes: str,
        rr_floor: float,
        starter_risk_pct: float,
        asof: date | None = None,
        real_reward_risk_by_symbol: dict[str, float | None] | None = None,
        existing_risk_pct: dict[str, float] | None = None,
    ) -> PortfolioDecision:
        """Keep every target at the size the PM asked for.

        **Rewritten 2026-09-17 — owner: residual invented R/R is a defect.
        Overnight bind: unmeasurable payoff honesty is a recorded fact /
        ranking hint with zero refuse, zero size floor, zero sub-floor
        branch. Catalyst-exception theater around a dead floor is gone.**

          Type B / breakout          -> untouched. No drop, no cap.
          Type A, ratio measurable   -> untouched. Any computed ratio is
                                        ranking information.
          Type A, ratio unmeasurable -> kept at the asked size. Python
                                        logs the missing ratio; it does
                                        not drop, shrink, or require a
                                        catalyst.

        `rr_floor` is accepted and ignored. Starter size is not assigned
        here. This is not an entry in `validate_grounding`'s error list.
        """
        _ = (rr_floor, starter_risk_pct, asof, active_state_changes)
        if real_reward_risk_by_symbol is not None:
            rr_by_symbol = {
                a.symbol.upper(): real_reward_risk_by_symbol.get(a.symbol.upper())
                for a in analyses
            }
        else:
            rr_by_symbol = {a.symbol.upper(): a.risk_reward for a in analyses}
        setup_by_symbol = {a.symbol.upper(): a.setup_type for a in analyses}
        held = {p.symbol.upper(): p for p in positions}
        for target in decision.targets:
            intent = cls._target_intent(
                target, held, total_value, existing_risk_pct=existing_risk_pct,
            )
            if intent not in ("buy", "short"):
                continue
            symbol = target.symbol.upper()
            if not reward_risk_floor_applies(setup_by_symbol.get(symbol)):
                continue
            if rr_by_symbol.get(symbol) is None:
                logger.info(
                    "%s: %s (%s) payoff geometry cannot be computed — "
                    "recorded as unknown ranking hint, not dropped and "
                    "not size-capped. Invented reward:risk floors are "
                    "retired.",
                    SUBFLOOR_CATALYST_UNVERIFIED_STATUS, target.symbol, intent,
                )
        return decision

    @staticmethod
    def _drop_invalid_rejections(parsed: dict) -> dict:
        """Same per-entry isolation as `_drop_invalid_targets`, for the
        candidate-accounting list (board item 133, 2026-09-18).

        `rejections` is BOOKKEEPING. A malformed entry in it must never
        destroy `reasoning_chain`, `portfolio_view` and every target — the
        seat explaining itself badly is not a reason to lose a whole
        session's decision. A dropped entry is not lost either: the symbol
        it named is then simply unaccounted for, and the accounting step in
        `DecisionStage` re-asks and records it exactly as it does for a name
        the seat never mentioned. So the failure mode is one paid re-ask,
        never a silent hole and never a dead session.
        """
        raw = parsed.get("rejections")
        if raw is None:
            return parsed
        if not isinstance(raw, list):
            logger.warning(
                "Portfolio manager: rejections is %s, not list — replacing "
                "with []", type(raw).__name__,
            )
            parsed["rejections"] = []
            return parsed
        valid: list = []
        for i, item in enumerate(raw):
            try:
                CandidateRejection.model_validate(item)
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    "Portfolio manager: dropping unusable rejections entry at "
                    "index %d (%s) — the symbol it named will be re-asked "
                    "for, not silently omitted: %r", i, e, item,
                )
                continue
            valid.append(item)
        parsed["rejections"] = valid
        return parsed

    @staticmethod
    def _drop_invalid_targets(parsed: dict, dropped: list[dict] | None = None) -> dict:
        """Pre-validate each TargetPosition; drop malformed entries with a
        warning naming the symbol (or list index when missing).

        `dropped` (board item 164) is an optional sink: each discarded entry
        is appended with its symbol, the gate and the validation error, so
        the caller can persist it per symbol instead of the only trace being
        a log line and an in-memory counter. Recording only.

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
                if dropped is not None:
                    dropped.append({
                        "symbol": None, "index": i,
                        "gate": "pm_target_malformed",
                        "reason": (
                            f"targets entry at index {i} DROPPED: it is a "
                            f"{type(item).__name__}, not an object, so it "
                            f"names no symbol: {item!r}"
                        ),
                    })
                continue
            try:
                # Dry run: the surviving dicts are validated again by
                # PortfolioDecision, so tallying here would double-count.
                with parse_telemetry.suspended():
                    TargetPosition(**item)
            except ValidationError as e:
                sym = item.get("symbol") or f"<idx {i}>"
                # A target the PM proposed and the desk then discarded is a
                # position that will not be opened. Counted for the same
                # reason the tech-side drop is: an idea lost at parse looks
                # identical to an idea nobody had.
                parse_telemetry.record_dropped_item("TargetPosition", str(sym))
                logger.warning(
                    "Portfolio manager: dropping malformed target for %s: %s",
                    sym, e,
                )
                if dropped is not None:
                    errors = "; ".join(
                        f"{'.'.join(str(p) for p in err.get('loc', ()))}: "
                        f"{err.get('msg', '')}"
                        for err in e.errors()
                    )
                    raw_symbol = item.get("symbol")
                    dropped.append({
                        "symbol": (
                            str(raw_symbol).strip().upper()
                            if isinstance(raw_symbol, str) and raw_symbol.strip()
                            else None
                        ),
                        "index": i,
                        "gate": "pm_target_malformed",
                        "reason": (
                            f"{sym} target DROPPED at parse: it fails the "
                            f"target schema ({errors}), so no position is "
                            f"built from it; the rest of the decision stands."
                        ),
                    })
                continue
            valid.append(item)
        parsed["targets"] = valid
        return parsed

    def drain_soft_exit_heals(self) -> dict[str, dict[str, str]]:
        """Per-symbol soft-exit heal outcomes from the last `decide()`, cleared.

        Same hand-over contract as `PortfolioConstructor.drain_refusals`: a
        fresh dict the caller can hold, and the agent forgets it so a later
        session cannot re-file a stale outcome. Board item 78.
        """
        heals = dict(getattr(self, "last_soft_exit_heals", None) or {})
        self.last_soft_exit_heals = {}
        return heals

    def _record_soft_exit_heal(self, symbols, outcome: str, detail: str) -> None:
        """File one heal outcome per named symbol. Recording only; never raises."""
        try:
            store = getattr(self, "last_soft_exit_heals", None)
            if store is None:
                store = {}
                self.last_soft_exit_heals = store
            for symbol in symbols or []:
                key = str(symbol).strip().upper()
                if key:
                    store[key] = {"outcome": outcome, "detail": detail}
        except Exception:  # noqa: BLE001 - bookkeeping must not break a decision
            pass

    def _fill_missing_open_falsifiers(
        self, decision, result, *, positions=None, total_value: float = 0.0,
        existing_risk_pct=None,
    ):
        """One paid retry to fill a missing thesis_invalid_if. Never invents.

        Mechanical heal already restored a stated string the null-wipe
        dropped. This asks the seat to actually write the falsifier on
        open/increase names that still have empty/`unknown`. Reductions
        and closes are not asked — a blank field must not spend a retry
        on a size drop. Catalyst is not filled here. If the retry still
        leaves an open name blank, the book-entry refuse records
        `soft-exit missing after retry`.
        """
        from src.cost_circuit import PaidAnalysisSuspended
        from src.seat_heal import (
            HEAL_CAP_BLOCKED, HEAL_FAILED, HEAL_NOT_ATTEMPTED, HEAL_PAID_RETRY,
            merge_retry_falsifiers,
        )

        if decision is None:
            return decision, result
        retry_already_used = bool(getattr(self, "_soft_exit_retry_used", False))
        held = {
            str(getattr(p, "symbol", "")).upper(): p
            for p in list(positions or [])
            if getattr(p, "symbol", None)
        }
        missing = [
            t.symbol for t in list(getattr(decision, "targets", None) or [])
            if open_target_missing_falsifier(
                t,
                intent=self._target_intent(
                    t, held, total_value, existing_risk_pct=existing_risk_pct,
                ),
            )
        ]
        if not missing:
            return decision, result
        if retry_already_used:
            self._record_soft_exit_heal(
                missing, HEAL_NOT_ATTEMPTED,
                "the seat's one soft-exit fill retry was already spent on an "
                "earlier attempt in this decide() call; no second retry was "
                "bought and no falsifier was invented",
            )
            return decision, result
        user_message = getattr(result, "user_message", None) or ""
        if not str(user_message).strip():
            logger.warning(
                "Open target(s) missing thesis_invalid_if (%s) — no user "
                "message to replay for a fill retry", missing,
            )
            self._record_soft_exit_heal(
                missing, HEAL_NOT_ATTEMPTED,
                "no replayable user message survived, so the paid soft-exit "
                "fill retry was NEVER ATTEMPTED for this name",
            )
            return decision, result
        self._soft_exit_retry_used = True
        coda = (
            "\n\n## SOFT-EXIT COMPLETION REQUIRED — NOT A RE-DECISION\n"
            "These open/increase targets are missing a real thesis_invalid_if "
            "(I'll sell if). Fill ONLY that field on the named symbols with "
            "one concrete observable. Do NOT invent a catalyst unless you are "
            "citing a dated Active News State Change for the unmeasurable-"
            "range exception. Do NOT change symbol, direction, conviction, "
            "thesis, risk_allocation_pct, target_weight_pct, or "
            "suggested_stop_price. Do NOT add or remove targets. If you "
            "cannot state a real falsifier, leave that name's "
            "thesis_invalid_if empty — Python will refuse that name; do not "
            "invent text.\n"
            f"Symbols: {', '.join(missing)}\n"
            "Respond ONLY with the complete JSON object.\n"
        )
        try:
            retried = self._execute(
                str(user_message) + coda, retry_kind="soft_exit_fill",
                optional_retry=True,
            )
        except PaidAnalysisSuspended as exc:
            logger.warning(
                "Soft-exit fill retry blocked by spend cap for %s: %s",
                missing, exc,
            )
            self._record_soft_exit_heal(
                missing, HEAL_CAP_BLOCKED,
                f"the paid soft-exit fill retry was blocked by the spend cap "
                f"({exc}); the seat was never re-asked for this name",
            )
            return decision, result
        except Exception as exc:
            logger.warning(
                "Soft-exit fill retry failed for %s: %s", missing, exc,
            )
            self._record_soft_exit_heal(
                missing, HEAL_FAILED,
                f"the paid soft-exit fill retry was attempted and errored "
                f"({type(exc).__name__}: {exc})",
            )
            return decision, result
        reparsed = retried.parse_json() if retried is not None else None
        retry_targets = []
        if isinstance(reparsed, dict) and isinstance(reparsed.get("targets"), list):
            retry_targets = reparsed["targets"]
        merged, filled = merge_retry_falsifiers(
            list(decision.targets), retry_targets,
        )
        if filled:
            decision.targets = merged
            logger.info(
                "Soft-exit fill retry stated thesis_invalid_if for %s — "
                "not invented", filled,
            )
        else:
            logger.warning(
                "Soft-exit fill retry did not produce a stated falsifier "
                "for %s", missing,
            )
        filled_keys = {str(s).strip().upper() for s in (filled or [])}
        self._record_soft_exit_heal(
            filled, HEAL_PAID_RETRY,
            "the seat stated a real thesis_invalid_if on the one paid "
            "soft-exit fill retry; the string is the seat's, not invented",
        )
        self._record_soft_exit_heal(
            [s for s in missing if str(s).strip().upper() not in filled_keys],
            HEAL_FAILED,
            "the one paid soft-exit fill retry WAS attempted and the seat "
            "still did not state a falsifier for this name",
        )
        return decision, retried

    _DECISION_FIELDS = ("targets",)

    @staticmethod
    def _canonical_targets(targets) -> list[tuple] | None:
        """Full TargetPosition decision payload (symbol, target_weight_pct,
        risk_allocation_pct, direction, conviction, thesis,
        thesis_invalid_if, suggested_stop_price, catalyst),
        order-insensitive. Built by re-validating each entry
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
                    m.symbol, m.target_weight_pct, m.risk_allocation_pct,
                    m.direction, m.conviction, m.thesis,
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
