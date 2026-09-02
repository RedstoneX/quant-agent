"""Per-run context + structured PM facts.

Previously `TradingPipeline` stashed cross-stage data on its own instance
(``self._last_symbols_bars``, ``self._bg_threads``). That conflated per-run
state with the long-lived service container, making runs non-reentrant,
hard to test, and hard to reason about when one stage's output is
another stage's input.

`RunContext` is an explicit container created at the start of each run.
Every stage reads from it and writes to it by field name. Stages become
functions of ``(ctx, deps) -> ctx-with-fields-filled-in`` rather than
methods that rely on implicit attributes of the enclosing instance.

This module ships the dataclass only — it does not (yet) refactor the
pipeline into explicit stages. That's Phase 2 of the architecture work.
For Phase 1 the goal is just to remove implicit state and give each run
its own mutable snapshot.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Literal

from src.models import parse_telemetry

if TYPE_CHECKING:
    from src.data.event_calendar import EventCalendarCoverage, FOMCCoverage
    from src.data.macro import MacroCoverage
    from src.models import NewsIntelligenceReport, PortfolioDecision, Position
    from src.risk.metrics import PortfolioHeat

logger = logging.getLogger(__name__)

SessionType = Literal["morning", "midday", "close", "evening", "intra_check", "earnings_preprocess"]


@dataclass
class RunContext:
    """Per-run snapshot of everything a session needs.

    Not frozen — stages populate fields as the run progresses. Discipline is
    "each field has one owning stage that writes it; other stages read only."
    """

    run_id: str
    session: SessionType
    started_at: datetime = field(default_factory=datetime.utcnow)

    # === Set at the start of each run (broker snapshot) ===
    account: dict = field(default_factory=dict)
    positions: list = field(default_factory=list)  # list[Position]
    cash: float = 0.0
    # What QAMC can deploy into equities WITHOUT borrowing: raw `cash`
    # plus the market value of the cash-equivalent sweep vehicle, which is
    # liquidated before the BUY phase. Both are assets already owned, so
    # this can never exceed equity and never implies margin.
    #
    # Verified Alpaca semantics (2026-08-19): `cash` is credited as soon as
    # a SELL FILLS, so filled sweep proceeds fund an equity BUY the same
    # session. `non_marginable_buying_power` is the settled/crypto figure
    # and lags a same-day equity sale by a business day — it is NOT the
    # right field for equity sizing. `buying_power`/`regt_buying_power` are
    # margin figures (~2x equity here) and must never be used.
    #
    # This is a PLANNING figure for PM / RM / the pre-trade gate.
    # ExecutionStage still re-reads raw broker `cash` after the funding
    # sale and skips any BUY that cash does not actually cover — that
    # deterministic backstop is unchanged and remains authoritative.
    # See TradingPipeline._compute_deployable_cash.
    deployable_cash: float = 0.0
    total_value: float = 0.0
    last_equity: float = 0.0

    # === Populated by the research stage (parallel fan-out) ===
    macro_summary: dict = field(default_factory=dict)
    macro_analysis: dict | None = None  # Macro Analyst's LLM output (model_dump)
    # How many of the configured FRED series actually returned data this run
    # (src.data.macro.MacroCoverage) — Phase 4.2 fix. Kept alongside
    # macro_summary rather than folded into data_status because it carries
    # the per-series detail (which series, why) that a single status word
    # can't; data_status["macro"] is the summary, this is the evidence
    # behind it.
    macro_coverage: "MacroCoverage | None" = None
    # Scheduled macro releases landing inside this run's event horizon, and how
    # much of the configured release set actually returned a schedule
    # (src.data.event_calendar). Fetched once by the research stage and read
    # again by RiskStage, so one session issues one calendar sweep rather than
    # two. The pair is load-bearing TOGETHER: an empty `macro_events` means
    # "nothing scheduled" ONLY when `macro_event_coverage.status == "ok"`;
    # with coverage None or degraded it means NOT FETCHED, and every renderer
    # must say which. Defaults (empty list / None) are the honest "not fetched
    # this run" state for every session that never populates them.
    macro_events: list = field(default_factory=list)  # list[MacroEvent]
    macro_event_coverage: "EventCalendarCoverage | None" = None
    # The forward FOMC meeting schedule and where it came from
    # (src.data.event_calendar.FOMCCalendarProvider). Same pairing rule and
    # same reason as the two fields above: an empty `fomc_meetings` means "no
    # meeting scheduled" ONLY when `fomc_coverage` says a published schedule
    # actually spans the horizon; with coverage None or degraded it means NOT
    # FETCHED. Fetched once by the research stage and read again by RiskStage
    # so one session issues one Fed calendar fetch, not two.
    fomc_meetings: list = field(default_factory=list)  # list[FOMCMeeting]
    fomc_coverage: "FOMCCoverage | None" = None
    news_intel: "NewsIntelligenceReport | None" = None
    analyses: list = field(default_factory=list)  # list[TechAnalysisResult]
    earnings_results: list[dict] = field(default_factory=list)
    smart_money_observations: list = field(default_factory=list)
    smart_money_findings: list = field(default_factory=list)
    smart_money_provider_error: str | None = None
    # Run-scoped BUY eligibility granted only by deterministic SEC Form 4
    # admission. Never written back to config.trading.universe and never
    # authored by an LLM.
    admitted_symbols: set[str] = field(default_factory=set)
    smart_money_admissions: dict[str, dict] = field(default_factory=dict)
    # Conviction ledger (spec §9.5): {SYMBOL: {seat: {"conviction", "observation"}}}
    # for every raw nomination this run produced, seat names already
    # canonicalized (`src.conviction_ledger.normalize_seat`). Written by
    # MorningResearchStage's nomination responder pass and read by
    # DecisionStage, which is where a decision_id finally exists to record
    # the stances against. Carried on the context rather than re-read from
    # the evidence table because the pass already holds the typed objects —
    # and because a read-back would make a forensic concern depend on a
    # write having succeeded. Empty for every session that runs no
    # nominations (intraday, close, evening), which is the honest state.
    nomination_convictions: dict[str, dict[str, dict]] = field(default_factory=dict)
    symbols_bars: dict = field(default_factory=dict)  # {sym: list[OHLCV]}
    valuations: dict = field(default_factory=dict)  # {sym: {trailing_pe, ...}}
    data_status: dict[str, str] = field(default_factory=dict)
    # What this session's LLM-response parsing lost or papered over
    # (src.models.parse_telemetry). Same relationship to `data_status` as
    # `macro_coverage` above: data_status carries the one-word verdict per
    # source, these carry the evidence behind it.
    #
    #   dropped_analyses    {(model, symbol): count} — a parsed item that was
    #                       discarded outright. The desk researched the name
    #                       and the Portfolio Manager never saw it. Recorded
    #                       even when a retry later recovers the symbol, which
    #                       is the case data_status cannot show at all.
    #   null_coerced_fields {(model, field): count} — a defaulted field
    #                       arrived as an explicit null and took its default.
    #                       The object survived; a real input did not. On
    #                       `thesis_invalid_if` that input is the soft-exit
    #                       signal, so the coercion is not free.
    #
    # WRITTEN BY RiskStage (not by the research stage): the Portfolio
    # Manager parses after research, so a reading taken any earlier would miss
    # every PM-side loss. RiskStage turns a non-empty pair into the
    # `analysis_parse_loss` / `analysis_field_nulled` advisories. The counters
    # behind them are zeroed at the top of MorningResearchStage.
    dropped_analyses: dict[tuple[str, str], int] = field(default_factory=dict)
    null_coerced_fields: dict[tuple[str, str], int] = field(default_factory=dict)

    # === Populated by the decision stage ===
    # Memory layers built for PM that the RiskStage also needs. Before the
    # 2026-08-13 agent audit these were DecisionStage locals, so the AI Risk
    # Manager was told (in its prompt) to enforce PM's holding-discipline and
    # drawdown-halve rules while receiving neither `days_held` nor
    # `in_drawdown`. RiskStage rebuilds them when they are absent, which is
    # the RC2 resume lane — there DecisionStage never runs at all.
    #   position_history:   {symbol: {entry_date, days_held, ...}}
    #   recent_performance: {rolling_5d_pct, rolling_20d_pct, in_drawdown, trailing_days}
    position_history: dict = field(default_factory=dict)
    recent_performance: dict = field(default_factory=dict)
    # Spec §11.2 — the session's gross-exposure state, resolved from ACCOUNT
    # STATE ONLY (equity, its high-water mark, the configured cap) in the run
    # preamble, before any LLM work. Deliberately not derived from anything
    # the Portfolio Manager produced: a blank PM response must not leave the
    # desk levered during a drawdown.
    #   {gross_usd, gross_x, ceiling_x, base_ceiling_x, drawdown_pct,
    #    distance_to_forced_liquidation_pct, alert_owner, reason}
    leverage: dict = field(default_factory=dict)

    portfolio_decision: "PortfolioDecision | None" = None
    # Transport-successful model output can still fail deterministic parsing,
    # schema, or grounding. Preserve the exact subtype for session status and
    # Telegram instead of collapsing every case to "unparseable".
    analysis_failure_status: str | None = None
    analysis_failure_error: str | None = None
    correlation_matrix: dict = field(default_factory=dict)
    daily_pnl: float = 0.0
    macro_target_pct: float | None = None
    # Stage 1 (QAMC correlation plumbing): set once by DecisionStage right
    # after a successful PM call. Threaded through to the risk_manager
    # agent_logs row and every trades row this run produces, so a single id
    # links "this PM proposal" -> "RM's review of it" -> "the orders/trades
    # it resulted in" without relying on (run_id, symbol) uniqueness holding
    # up under future control-flow changes (see DecisionStage.run()). None
    # when DecisionStage never reached a successful PM call this run.
    decision_id: str | None = None
    # Conviction ledger (spec §7.2): the ACTUAL model that answered this
    # run's portfolio_manager call (`AgentResult.model` — corrected Stage
    # 0.5 to mean the model that really responded, not merely the one
    # requested). Threaded to every entry `trades` row this run produces so
    # a later outcome can be traced to which model authored the decision —
    # training-data contamination is the dominant failure mode in this
    # literature (docs/RESEARCH_FINDINGS.md) and any evaluation must record
    # which model produced each result. None alongside decision_id=None.
    decision_model: str | None = None

    # === Populated by execution stage ===
    orders: list[dict] = field(default_factory=list)
    # Per-BUY skip records: {symbol, reason, detail}. Every deterministic
    # skip in the BUY loop lands here AND as an `execution_skip` evidence
    # row — before this, a risk-approved BUY could die on a log-only
    # `continue` and the funnel/journal/evening-reflection all read the
    # session as a deliberate no-trade (2026-08-19: three approved BUYs
    # skipped as unfunded; evening concluded "generate more ideas").
    execution_skips: list[dict] = field(default_factory=list)

    # === Structured facts for PM — Phase 4 #4 ===
    # Populated at the top of the DecisionStage so PM sees numbers, not
    # LLM-summarized-prose, for the quantitative stuff.
    facts: "PMFacts | None" = None

    @classmethod
    def start(cls, session: SessionType) -> "RunContext":
        """Build a fresh context for a new session.

        Run ID prefix matches legacy formatting so log greps like
        'run-abcd1234' and 'midday-abcd1234' keep working.
        """
        # Zero the parse counters here rather than in any one stage: this is
        # the single factory every session goes through, and RiskStage — which
        # reads them — also runs on the intraday scan path, which never
        # touches MorningResearchStage. Resetting in a stage would have made
        # the afternoon re-report the morning's losses in a long-lived
        # scheduler process.
        parse_telemetry.reset()
        rid_prefix = "run" if session == "morning" else session
        return cls(
            run_id=f"{rid_prefix}-{uuid.uuid4().hex[:8]}",
            session=session,
        )


@dataclass
class PMFacts:
    """Quantitative snapshot surfaced to PM as structured fields, not prose.

    Codex: 'Memory is LLM-summarizing-LLM — events, interpretations, and
    facts get mashed together in prose.' PMFacts carries pure numbers so
    PM can reference, compare, and reason against them directly instead
    of re-parsing prose that may have drifted.

    All values are POST-trade (i.e., current book state) unless tagged
    _pre_ (e.g., cash before executing). The snapshot is captured once
    at the top of DecisionStage and passed down — not recomputed.
    """

    # Calibration (realized outcomes)
    closed_trades_30d: int = 0
    win_rate_30d_pct: float | None = None
    avg_return_30d_pct: float | None = None
    avg_hold_days_30d: float | None = None

    # RM discipline (how often RM overrode PM lately)
    rm_verdicts_seen: int = 0
    rm_scale_downs_last5: int = 0   # count with scale_all_buys < 1.0
    rm_mods_last5: int = 0           # count with any modifications

    # Current book state
    invested_pct: float = 0.0
    #: Signed, leverage-aware net direction of the book, as a % of equity.
    #: NEGATIVE means net short. Deliberately separate from `invested_pct`:
    #: "is the money at work" and "which way does the book lean" are two
    #: questions and one number cannot answer both. Both come from the same
    #: `src.risk.rules.book_exposure` call, so they can never disagree about
    #: which positions they measured.
    net_exposure_pct: float = 0.0
    cash_pct: float = 100.0
    position_count: int = 0
    # Spec §12.2 (owner-ratified 2026-09-01) — SEPARATE long and short sector
    # budgets, each `{sector: % of equity}` as an UNSIGNED gross magnitude.
    #
    # This reverses the earlier, deliberate netting (a long 15% and a short
    # -5% in Technology used to render as one line, 10%). Owner's reasoning:
    # *"A long and a short in the same sector is not a hedge... We are
    # trading opportunities."* The PM must see the two sides separately or it
    # will reason about concentration differently from the engine that
    # enforces it — the same PM-sees-one-thing / gate-enforces-another defect
    # class as Phase 10.
    sector_weights_long: dict[str, float] = field(default_factory=dict)
    sector_weights_short: dict[str, float] = field(default_factory=dict)
    positions_under_5d: int = 0
    positions_5_to_15d: int = 0
    positions_over_15d: int = 0
    positions_drift_flagged: int = 0  # weight > 12% + P&L > 10%

    # Signal freshness (from TA output)
    tech_signals_count: int = 0
    tech_signals_median_age_days: int | None = None
    tech_signals_stale_count: int = 0  # age >= 8

    # System performance (existing; surfaced here as facts)
    rolling_5d_pct: float | None = None
    rolling_20d_pct: float | None = None
    in_drawdown: bool = False

    # RC3 (2026-07-16): deployment vs the macro target. Macro demanded
    # 72-75% invested for three months while realized invested% averaged
    # 39% and NOTHING forced the gap into PM's face — every layer shaved
    # sizes independently and no one reconciled the compound. None when
    # macro didn't provide a target this session.
    macro_target_invested_pct: float | None = None
    deployment_gap_pp: float | None = None  # invested - target (negative = under)

    # Phase 2 / audit §1.3-§1.4: the book's actual risk, computed in Python.
    # `heat` carries per-position at-risk dollars, open risk, the release flag
    # and R-multiples; `risk_ceiling_pct` is the owner-ratified total at-risk
    # ceiling the headroom is measured against. None when the heat build failed
    # — the prompt then says so rather than rendering a confident zero.
    heat: "PortfolioHeat | None" = None
    risk_ceiling_pct: float = 25.0
    #: Spec §2.2 — the most of that ceiling any ONE correlated cluster may
    #: take. Rendered with the clusters below so the cap the constructor
    #: enforces is a number the PM can size against, rather than a rule it is
    #: told about and then surprised by.
    cluster_risk_share_pct: float = 40.0

    # Audit §1.2: the correlation matrix has been computed every run and shown
    # only to the deterministic cluster check, which fires AFTER PM has already
    # chosen. PM's prompt told it to "avoid stacking highly correlated
    # positions" while `grep -i correlation src/agents/portfolio_manager.py`
    # returned zero hits. These are the clusters PM is now actually given.
    # Each entry is a list of mutually correlated symbols (|corr| >= 0.7),
    # sorted, largest cluster first. Empty when coverage is missing.
    correlation_clusters: list[list[str]] = field(default_factory=list)
    correlation_coverage: bool = True

    # Who the tickers actually are. Every layer above this one reasons about
    # a symbol as a price series with a sector tag, and "Utilities" covers
    # both a regulated water utility and a merchant power trader with
    # commodity exposure — a label alone lets the PM reach for the wrong
    # prior with complete confidence. `CompanyProfile` objects for the
    # symbols in scope for THIS decision (held + candidates), never the whole
    # universe. Empty when the lookup failed or the cache was cold and the
    # fetch degraded — the section then renders as nothing at all, which is
    # the correct failure direction: a missing profile must never cost a
    # session, and a heading over "no profile available" lines is worse than
    # silence.
    company_profiles: list = field(default_factory=list)

    def render(self) -> str:
        """Format as a compact markdown block for PM's prompt."""
        def _pct(v: float | None) -> str:
            return f"{v:+.2f}%" if v is not None else "n/a"

        def _num(v: float | int | None) -> str:
            return f"{v}" if v is not None else "n/a"

        # Spec §12.2 — the two sides are rendered as two lists, never summed.
        # Netting them here would show the PM a smaller number than the gate
        # enforces against, which is precisely the defect being removed.
        def _sector_lines(weights: dict[str, float]) -> str:
            return "\n".join(
                f"  - {s}: {w:.1f}%"
                for s, w in sorted(weights.items(), key=lambda kv: -kv[1])[:8]
            ) or "  (none)"

        long_sector_lines = _sector_lines(self.sector_weights_long)
        short_sector_lines = _sector_lines(self.sector_weights_short)

        # audit round 2 #35: the denominator is rm_verdicts_seen (the query
        # is limit=5 but can return 0-5 rows), not a hardcoded 5 — a fresh
        # deployment with 2 verdicts, both overrides, used to render "2/5"
        # (40%) when the true override rate was 2/2 (100%). PM must cite
        # these numbers verbatim, so the block itself has to be honest.
        if self.rm_verdicts_seen > 0:
            rm_block = (
                f"### RM Discipline (last {self.rm_verdicts_seen} verdicts)\n"
                f"- scale_all_buys<1.0 count: {self.rm_scale_downs_last5}/{self.rm_verdicts_seen}"
                f" · mods emitted: {self.rm_mods_last5}/{self.rm_verdicts_seen}"
            )
        else:
            rm_block = (
                "### RM Discipline\n"
                "- (no RM verdicts on record — cite as [UNSOURCED:no_rm_history])"
            )

        return f"""### Calibration (last 30d closed trades)
- n={self.closed_trades_30d} · win_rate={_pct(self.win_rate_30d_pct)} · avg_return={_pct(self.avg_return_30d_pct)} · avg_hold={_num(self.avg_hold_days_30d)}d

{rm_block}

### Book State (current)
- invested={self.invested_pct:.1f}% (capital at work, unsigned) · net direction={self.net_exposure_pct:+.1f}% (leverage-aware; negative = net short) · cash={self.cash_pct:.1f}% · positions={self.position_count}
- age buckets: <5d={self.positions_under_5d} · 5-15d={self.positions_5_to_15d} · >15d={self.positions_over_15d}
- drift-flagged (weight>12% + P&L>10%): {self.positions_drift_flagged}
- sector weights — LONG side (top 8, gross % of equity):
{long_sector_lines}
- sector weights — SHORT side (top 8, gross % of equity):
{short_sector_lines}
- (§12.2) the two sides carry SEPARATE budgets against the same sector
  limit and are NOT netted. A long and a short in the same sector is not a
  hedge — it is two opportunities that share a label.

### Signal Freshness (TA output this session)
- signals={self.tech_signals_count} · median_age={_num(self.tech_signals_median_age_days)}d · stale(≥8d)={self.tech_signals_stale_count}

### System Performance
- rolling 5d={_pct(self.rolling_5d_pct)} · 20d={_pct(self.rolling_20d_pct)} · in_drawdown={self.in_drawdown}{self._render_drawdown_gate()}

{self._render_risk()}
{self._render_correlation()}{self._render_deployment_gap()}{self._render_companies()}"""

    def _render_drawdown_gate(self) -> str:
        """State who applies the halving. Two halvings would quarter the size."""
        if not self.in_drawdown:
            return ""
        return (
            "\n- ⚠️ in_drawdown=true — the risk engine halves every BUY "
            "deterministically AFTER you submit. Do NOT pre-halve; size "
            "normally and the gate will apply once."
        )

    def _render_risk(self) -> str:
        from src.risk.metrics import format_heat_block
        if self.heat is None:
            return (
                "### Portfolio Risk\n"
                "- not computed this run (stop data unavailable) — treat total "
                "at-risk as UNKNOWN, cite as [UNSOURCED:no_risk_data], and do "
                "not assume headroom exists."
            )
        return format_heat_block(self.heat, self.risk_ceiling_pct).rstrip("\n")

    def _render_correlation(self) -> str:
        if not self.correlation_coverage:
            return (
                "\n### Correlation Clusters\n"
                "- coverage MISSING this run (insufficient bar history). The "
                "deterministic cluster check is disabled, so concentration you "
                "stack today will not be caught downstream. Diversify by theme "
                "manually and say so in `portfolio_balance`."
            )
        if not self.correlation_clusters:
            return (
                "\n### Correlation Clusters\n"
                "- none: no held or candidate pair correlates at |r| >= 0.7."
            )
        lines = "\n".join(
            f"  - {' / '.join(cluster)}" for cluster in self.correlation_clusters
        )
        cluster_cap = self.risk_ceiling_pct * self.cluster_risk_share_pct / 100.0
        return (
            "\n### Correlation Clusters (|r| >= 0.7 over the trailing window)\n"
            "- These names move together. Each cluster is ONE bet, however "
            "many tickers it holds; sizing two members full-size is one "
            "double-sized bet wearing a diversification costume.\n"
            f"- ENFORCED: the members of any one cluster may hold at most "
            f"{cluster_cap:.1f}% of equity at risk between them "
            f"({self.cluster_risk_share_pct:.0f}% of the "
            f"{self.risk_ceiling_pct:.1f}% total). Ask for more and the "
            f"constructor rations it deterministically, largest request "
            f"first — so size the theme yourself rather than discovering the "
            f"cap after the fact.\n"
            f"{lines}"
        )

    def _render_companies(self) -> str:
        """Business identities for the symbols in scope, or nothing at all.

        Profiles that came back with no identifying field (fetch failed, cold
        cache with `allow_fetch=False`, a symbol yfinance does not know) are
        dropped before rendering rather than printed as "no company profile
        available" — a heading followed by a list of shrugs teaches the PM
        that the section is noise. If nothing survives the filter the whole
        section disappears.
        """
        try:
            from src.data.company import format_profiles_block
            known = [
                p for p in self.company_profiles
                if p is not None and any((
                    getattr(p, "name", None),
                    getattr(p, "summary", None),
                    getattr(p, "industry", None),
                ))
            ]
            if not known:
                return ""
            block = format_profiles_block(
                known, title="Who These Companies Are",
            ).rstrip("\n")
        except Exception as e:  # noqa: BLE001 — never fail a render on prose
            logger.warning("pm_facts: company profile render failed: %s", e)
            return ""
        return f"\n\n{block}" if block else ""

    def _render_deployment_gap(self) -> str:
        if self.macro_target_invested_pct is None or self.deployment_gap_pp is None:
            return ""
        if self.deployment_gap_pp > 15:
            return (
                f"\n\n### Deployment vs Macro Target"
                f"\n- invested={self.invested_pct:.1f}% vs macro target="
                f"{self.macro_target_invested_pct:.0f}% — {self.deployment_gap_pp:.0f}pp OVER"
                f" the target. The RM advisory will flag this; trims/rotation"
                f" are a valid response, especially if macro is not risk-on."
            )
        if self.deployment_gap_pp >= -15:
            return (
                f"\n\n### Deployment vs Macro Target"
                f"\n- invested={self.invested_pct:.1f}% vs macro target="
                f"{self.macro_target_invested_pct:.0f}% (gap {self.deployment_gap_pp:+.0f}pp — within band)"
            )
        return (
            f"\n\n### ⚠️ DEPLOYMENT GAP (address in cash_target step)"
            f"\n- invested={self.invested_pct:.1f}% vs macro target="
            f"{self.macro_target_invested_pct:.0f}% — you are {-self.deployment_gap_pp:.0f}pp UNDER the target"
            f"\n- This gap has been the single largest P&L drag (idle cash in a"
            f" rising market). In `cash_target`, either (a) close it with"
            f" qualified candidates THIS session, or (b) name the concrete"
            f" blocker per unfilled slot (no-qualified-setups after filters /"
            f" regime gate / earnings-queue). \"Staying cautious\" without a"
            f" named blocker is not an answer."
        )
