"""Prompt-facts builders: read-only DB/broker reads turned into LLM context.

Step 1 of `docs/PIPELINE_SPLIT_PLAN.md` (board item 210). Moved verbatim out of
`src/pipeline.py` as a mixin, so `TradingPipeline` keeps every one of these as
its own attribute and every test that patches or calls them is untouched.

The defining property of this module: it places no orders, cancels nothing and
amends no stop. `_handle_ex_dividends` sat in this cluster's line range and does
move live stops, so it is NOT here — it goes to the protection module in step 2
(plan §1 correction, 2026-10-01).

Step 10 (second half): `PromptFacts` is now a composite over the six fact
families in `src/prompt_facts_*.py`; it keeps the full builder surface so the
46 call sites and the delegating mixin below are untouched.

Nothing here may import `src.pipeline`: this module is one of its bases.
"""

import functools
import logging
import sys as _sys
import types as _types

from src.prompt_facts_book import BookFacts, _PM_PROFILE_SYMBOL_CAP  # noqa: F401
from src.prompt_facts_candidates import (  # noqa: F401
    CandidateFacts,
    _missed_ops_quality_metrics,
    _valuation_signal_from,
)
from src.prompt_facts_grading import GradingFacts
from src.prompt_facts_market_context import MarketContextFacts
from src.prompt_facts_ports import _ABSENT, bind_ports
from src.prompt_facts_projection import ProjectionFacts
from src.prompt_facts_seat_memory import SeatMemoryFacts
from src.trading_calendar import et_today, session_date_key  # noqa: F401

#: The moved code logged under `src.pipeline` before the move and still does;
#: binding the name rather than `__name__` keeps log records byte-identical.


#: The moved code logged under `src.pipeline` before the move and still does;
#: binding the name rather than `__name__` keeps log records byte-identical.
logger = logging.getLogger("src.pipeline")


class PromptFacts:
    """Composite over the six prompt-fact families (step 10, second half).

    Same keyword-only constructor as before: it hands each family ONLY the
    collaborators that family's bodies read (`src/prompt_facts_*.py`, measured
    per family in each module's docstring) and keeps every builder name on this
    object as a one-line delegator, so the 46 call sites, the class-level test
    patches and `PromptFactsMixin` below are untouched. The ports are also kept
    on the composite itself, unset when `_ABSENT`, so `facts.db` and the
    `getattr(facts, "config", None)` guards read exactly as they did.
    """

    def __init__(
        self,
        *,
        db=_ABSENT,
        broker=_ABSENT,
        market=_ABSENT,
        news_store=_ABSENT,
        macro_store=_ABSENT,
        earnings_provider=_ABSENT,
        config=_ABSENT,
        tech_store=_ABSENT,
        sweeper=_ABSENT,
        parse_logged_agent_response=_ABSENT,
        atr_for_symbol=_ABSENT,
        exit_audit_actions=_ABSENT,
        portfolio_constructor=None,
        risk_engine=None,
        last_symbol_sectors=_ABSENT,
    ) -> None:
        bind_ports(self, {
            "db": db,
            "broker": broker,
            "market": market,
            "news_store": news_store,
            "macro_store": macro_store,
            "earnings_provider": earnings_provider,
            "config": config,
            "tech_store": tech_store,
            "_sweeper": sweeper,
            "_parse_logged_agent_response": parse_logged_agent_response,
            "_atr_for_symbol": atr_for_symbol,
            "_EXIT_AUDIT_ACTIONS": exit_audit_actions,
            "_last_symbol_sectors": last_symbol_sectors,
        })
        self.portfolio_constructor = portfolio_constructor
        self.risk_engine = risk_engine
        self._book = BookFacts(
            db=db, broker=broker, tech_store=tech_store, config=config, sweeper=sweeper,
            parse_logged_agent_response=parse_logged_agent_response, atr_for_symbol=atr_for_symbol,
        )
        self._projection = ProjectionFacts(
            market=market, config=config, last_symbol_sectors=last_symbol_sectors,
            portfolio_constructor=portfolio_constructor, risk_engine=risk_engine,
        )
        self._seat_memory = SeatMemoryFacts(
            db=db, parse_logged_agent_response=parse_logged_agent_response,
        )
        self._grading = GradingFacts(
            db=db, broker=broker, market=market, sweeper=sweeper,
            exit_audit_actions=exit_audit_actions,
        )
        self._market_context = MarketContextFacts(
            macro_store=macro_store, news_store=news_store,
        )
        self._candidates = CandidateFacts(
            db=db, broker=broker, market=market, earnings_provider=earnings_provider,
            news_store=news_store, macro_store=macro_store, config=config,
            parse_logged_agent_response=parse_logged_agent_response,
        )

    def _bind_override(self, name: str, fn) -> None:
        """Install an overriding builder (a test patch of the OLD name on the
        pipeline) on the composite AND on the family that owns the name, so an
        internal `self._build_x(...)` inside that family hits the override
        exactly as it did when every body shared one `self`."""
        setattr(self, name, fn)
        setattr(getattr(self, _FAMILY_ATTR_OF[name]), name, fn)

    # --- BookFacts (prompt_facts_book) ---
    @functools.wraps(BookFacts._build_position_history)
    def _build_position_history(self, *args, **kwargs):
        return self._book._build_position_history(*args, **kwargs)

    @functools.wraps(BookFacts._build_stop_map)
    def _build_stop_map(self, *args, **kwargs):
        return self._book._build_stop_map(*args, **kwargs)

    @functools.wraps(BookFacts._build_portfolio_heat)
    def _build_portfolio_heat(self, *args, **kwargs):
        return self._book._build_portfolio_heat(*args, **kwargs)

    @functools.wraps(BookFacts._build_pm_facts)
    def _build_pm_facts(self, *args, **kwargs):
        return self._book._build_pm_facts(*args, **kwargs)

    @functools.wraps(BookFacts._build_position_facts)
    def _build_position_facts(self, *args, **kwargs):
        return self._book._build_position_facts(*args, **kwargs)

    @functools.wraps(BookFacts._build_review_metric_deltas)
    def _build_review_metric_deltas(self, *args, **kwargs):
        return self._book._build_review_metric_deltas(*args, **kwargs)

    @functools.wraps(BookFacts._compute_recent_performance)
    def _compute_recent_performance(self, *args, **kwargs):
        return self._book._compute_recent_performance(*args, **kwargs)

    # --- ProjectionFacts (prompt_facts_projection) ---
    @functools.wraps(ProjectionFacts._build_projected_portfolio)
    def _build_projected_portfolio(self, *args, **kwargs):
        try:
            return self._projection._build_projected_portfolio(*args, **kwargs)
        finally:
            # The one cache a builder writes: the family rebinds it, the
            # composite's __dict__ copies it, the mixin writes it through.
            if "_last_symbol_sectors" in self._projection.__dict__:
                self.__dict__["_last_symbol_sectors"] = self._projection._last_symbol_sectors

    @functools.wraps(ProjectionFacts._ensure_correlation_matrix)
    def _ensure_correlation_matrix(self, *args, **kwargs):
        return self._projection._ensure_correlation_matrix(*args, **kwargs)

    # --- SeatMemoryFacts (prompt_facts_seat_memory) ---
    @functools.wraps(SeatMemoryFacts._build_weekly_narrative)
    def _build_weekly_narrative(self, *args, **kwargs):
        return self._seat_memory._build_weekly_narrative(*args, **kwargs)

    @functools.wraps(SeatMemoryFacts._build_rm_recent_verdicts)
    def _build_rm_recent_verdicts(self, *args, **kwargs):
        return self._seat_memory._build_rm_recent_verdicts(*args, **kwargs)

    @functools.wraps(SeatMemoryFacts._build_pm_recent_decisions)
    def _build_pm_recent_decisions(self, *args, **kwargs):
        return self._seat_memory._build_pm_recent_decisions(*args, **kwargs)

    @functools.wraps(SeatMemoryFacts._build_own_recent_decisions)
    def _build_own_recent_decisions(self, *args, **kwargs):
        return self._seat_memory._build_own_recent_decisions(*args, **kwargs)

    # --- GradingFacts (prompt_facts_grading) ---
    @functools.wraps(GradingFacts._build_recent_sells_for_grading)
    def _build_recent_sells_for_grading(self, *args, **kwargs):
        return self._grading._build_recent_sells_for_grading(*args, **kwargs)

    @functools.wraps(GradingFacts._build_recent_buys_for_grading)
    def _build_recent_buys_for_grading(self, *args, **kwargs):
        return self._grading._build_recent_buys_for_grading(*args, **kwargs)

    @functools.wraps(GradingFacts._build_recent_outlook_calibration)
    def _build_recent_outlook_calibration(self, *args, **kwargs):
        return self._grading._build_recent_outlook_calibration(*args, **kwargs)

    @functools.wraps(GradingFacts._build_trade_grade_summary)
    def _build_trade_grade_summary(self, *args, **kwargs):
        return self._grading._build_trade_grade_summary(*args, **kwargs)

    @functools.wraps(GradingFacts._build_post_exit_reality)
    def _build_post_exit_reality(self, *args, **kwargs):
        return self._grading._build_post_exit_reality(*args, **kwargs)

    @functools.wraps(GradingFacts._build_recent_missed_lessons)
    def _build_recent_missed_lessons(self, *args, **kwargs):
        return self._grading._build_recent_missed_lessons(*args, **kwargs)

    @functools.wraps(GradingFacts._persist_evening_replay_inputs)
    def _persist_evening_replay_inputs(self, *args, **kwargs):
        return self._grading._persist_evening_replay_inputs(*args, **kwargs)

    @functools.wraps(GradingFacts._build_recent_loss_pits)
    def _build_recent_loss_pits(self, *args, **kwargs):
        return self._grading._build_recent_loss_pits(*args, **kwargs)

    _actualize_trade_row = staticmethod(GradingFacts._actualize_trade_row)

    _log_conviction_outcome_for_operator = staticmethod(GradingFacts._log_conviction_outcome_for_operator)

    @functools.wraps(GradingFacts._build_calibration_note)
    def _build_calibration_note(self, *args, **kwargs):
        return self._grading._build_calibration_note(*args, **kwargs)

    # --- MarketContextFacts (prompt_facts_market_context) ---
    @functools.wraps(MarketContextFacts._build_macro_trajectory)
    def _build_macro_trajectory(self, *args, **kwargs):
        return self._market_context._build_macro_trajectory(*args, **kwargs)

    @functools.wraps(MarketContextFacts._build_active_state_changes)
    def _build_active_state_changes(self, *args, **kwargs):
        return self._market_context._build_active_state_changes(*args, **kwargs)

    _build_macro_tech_alignment = staticmethod(MarketContextFacts._build_macro_tech_alignment)

    # --- CandidateFacts (prompt_facts_candidates) ---
    @functools.wraps(CandidateFacts._build_thesis_health_context)
    def _build_thesis_health_context(self, *args, **kwargs):
        return self._candidates._build_thesis_health_context(*args, **kwargs)

    @functools.wraps(CandidateFacts._thesis_tech_trajectory_map)
    def _thesis_tech_trajectory_map(self, *args, **kwargs):
        return self._candidates._thesis_tech_trajectory_map(*args, **kwargs)

    @functools.wraps(CandidateFacts._thesis_news_events_map)
    def _thesis_news_events_map(self, *args, **kwargs):
        return self._candidates._thesis_news_events_map(*args, **kwargs)

    @functools.wraps(CandidateFacts._build_watchlist_candidates)
    def _build_watchlist_candidates(self, *args, **kwargs):
        return self._candidates._build_watchlist_candidates(*args, **kwargs)

    @functools.wraps(CandidateFacts._build_blocked_proposals)
    def _build_blocked_proposals(self, *args, **kwargs):
        return self._candidates._build_blocked_proposals(*args, **kwargs)

    @functools.wraps(CandidateFacts._build_missed_opportunities_digest)
    def _build_missed_opportunities_digest(self, *args, **kwargs):
        return self._candidates._build_missed_opportunities_digest(*args, **kwargs)

    @functools.wraps(CandidateFacts._missed_ops_held_set)
    def _missed_ops_held_set(self, *args, **kwargs):
        return self._candidates._missed_ops_held_set(*args, **kwargs)

    @functools.wraps(CandidateFacts._missed_ops_tech_signal)
    def _missed_ops_tech_signal(self, *args, **kwargs):
        return self._candidates._missed_ops_tech_signal(*args, **kwargs)

    @functools.wraps(CandidateFacts._missed_ops_news_signal)
    def _missed_ops_news_signal(self, *args, **kwargs):
        return self._candidates._missed_ops_news_signal(*args, **kwargs)

    @functools.wraps(CandidateFacts._missed_ops_theme_tags)
    def _missed_ops_theme_tags(self, *args, **kwargs):
        return self._candidates._missed_ops_theme_tags(*args, **kwargs)

    @functools.wraps(CandidateFacts._missed_ops_earnings_signal)
    def _missed_ops_earnings_signal(self, *args, **kwargs):
        return self._candidates._missed_ops_earnings_signal(*args, **kwargs)

    @functools.wraps(CandidateFacts._missed_ops_macro_sector_map)
    def _missed_ops_macro_sector_map(self, *args, **kwargs):
        return self._candidates._missed_ops_macro_sector_map(*args, **kwargs)


#: Builder name -> the composite attribute holding the family that owns it.
_FAMILY_ATTR_OF: dict[str, str] = {
    "_build_position_history": "_book",
    "_build_stop_map": "_book",
    "_build_portfolio_heat": "_book",
    "_build_pm_facts": "_book",
    "_build_position_facts": "_book",
    "_build_review_metric_deltas": "_book",
    "_compute_recent_performance": "_book",
    "_build_projected_portfolio": "_projection",
    "_ensure_correlation_matrix": "_projection",
    "_build_weekly_narrative": "_seat_memory",
    "_build_rm_recent_verdicts": "_seat_memory",
    "_build_pm_recent_decisions": "_seat_memory",
    "_build_own_recent_decisions": "_seat_memory",
    "_build_recent_sells_for_grading": "_grading",
    "_build_recent_buys_for_grading": "_grading",
    "_build_recent_outlook_calibration": "_grading",
    "_build_trade_grade_summary": "_grading",
    "_build_post_exit_reality": "_grading",
    "_build_recent_missed_lessons": "_grading",
    "_persist_evening_replay_inputs": "_grading",
    "_build_recent_loss_pits": "_grading",
    "_actualize_trade_row": "_grading",
    "_log_conviction_outcome_for_operator": "_grading",
    "_build_calibration_note": "_grading",
    "_build_macro_trajectory": "_market_context",
    "_build_active_state_changes": "_market_context",
    "_build_macro_tech_alignment": "_market_context",
    "_build_thesis_health_context": "_candidates",
    "_thesis_tech_trajectory_map": "_candidates",
    "_thesis_news_events_map": "_candidates",
    "_build_watchlist_candidates": "_candidates",
    "_build_blocked_proposals": "_candidates",
    "_build_missed_opportunities_digest": "_candidates",
    "_missed_ops_held_set": "_candidates",
    "_missed_ops_tech_signal": "_candidates",
    "_missed_ops_news_signal": "_candidates",
    "_missed_ops_theme_tags": "_candidates",
    "_missed_ops_earnings_signal": "_candidates",
    "_missed_ops_macro_sector_map": "_candidates",
}


#: Every builder `PromptFactsMixin` forwards to `PromptFacts`, in source order.
PROMPT_FACTS_METHODS: tuple[str, ...] = (
    "_build_position_history",
    "_build_weekly_narrative",
    "_build_macro_trajectory",
    "_build_active_state_changes",
    "_build_rm_recent_verdicts",
    "_build_pm_recent_decisions",
    "_build_projected_portfolio",
    "_build_recent_sells_for_grading",
    "_build_recent_buys_for_grading",
    "_build_recent_outlook_calibration",
    "_build_trade_grade_summary",
    "_build_post_exit_reality",
    "_build_recent_missed_lessons",
    "_persist_evening_replay_inputs",
    "_build_thesis_health_context",
    "_thesis_tech_trajectory_map",
    "_thesis_news_events_map",
    "_build_watchlist_candidates",
    "_build_recent_loss_pits",
    "_build_blocked_proposals",
    "_build_missed_opportunities_digest",
    "_missed_ops_held_set",
    "_missed_ops_tech_signal",
    "_missed_ops_news_signal",
    "_missed_ops_theme_tags",
    "_missed_ops_earnings_signal",
    "_missed_ops_macro_sector_map",
    "_actualize_trade_row",
    "_build_macro_tech_alignment",
    "_ensure_correlation_matrix",
    "_build_stop_map",
    "_build_portfolio_heat",
    "_build_pm_facts",
    "_log_conviction_outcome_for_operator",
    "_build_calibration_note",
    "_compute_recent_performance",
    "_build_position_facts",
    "_build_review_metric_deltas",
    "_build_own_recent_decisions",
)

#: The ports `PromptFactsMixin` reads off the pipeline: pipeline attribute ->
#: constructor keyword. Absent attributes are passed as `_ABSENT` (see above).
_PIPELINE_PORTS: tuple[tuple[str, str], ...] = (
    ("db", "db"),
    ("broker", "broker"),
    ("market", "market"),
    ("news_store", "news_store"),
    ("macro_store", "macro_store"),
    ("earnings_provider", "earnings_provider"),
    ("config", "config"),
    ("tech_store", "tech_store"),
    ("_sweeper", "sweeper"),
    ("_parse_logged_agent_response", "parse_logged_agent_response"),
    ("_atr_for_symbol", "atr_for_symbol"),
    ("_EXIT_AUDIT_ACTIONS", "exit_audit_actions"),
    ("_last_symbol_sectors", "last_symbol_sectors"),
)


def prompt_facts_for(owner) -> PromptFacts:
    """Build a `PromptFacts` from whatever `owner` currently holds.

    `owner` is normally the `TradingPipeline`, but the unbound-call tests pass a
    stub or a MagicMock as `self`, exactly as they did to the mixin's methods;
    reading the ports with `getattr` keeps those working unchanged. On a real
    pipeline any builder name overridden on it (a `patch.object(TradingPipeline,
    '_build_stop_map')`, or an instance attribute) is bound onto the service
    too, so a patch of the OLD name still intercepts the internal call it used to.
    """
    kwargs = {kw: getattr(owner, attr, _ABSENT) for attr, kw in _PIPELINE_PORTS}
    kwargs["portfolio_constructor"] = getattr(owner, "portfolio_constructor", None)
    kwargs["risk_engine"] = getattr(owner, "risk_engine", None)
    service = PromptFacts(**kwargs)
    if isinstance(owner, PromptFactsMixin):
        for name in PROMPT_FACTS_METHODS:
            if name in owner.__dict__ or (
                getattr(type(owner), name) is not getattr(PromptFactsMixin, name)
            ):
                service._bind_override(name, getattr(owner, name))
    return service


class PromptFactsMixin:
    """Thin delegating mixin: keeps every builder reachable as a `TradingPipeline`
    attribute (46 call sites in six modules call `self._build_...` on the pipeline,
    and tests patch these names on the class) while the bodies live on `PromptFacts`.

    The service is built per call from the pipeline's CURRENT attributes rather
    than cached, so a collaborator swapped after construction (every test that
    sets `pipeline.db = ...`) is seen, exactly as `self.db` was under the mixin.
    Each delegator is `functools.wraps`-ed onto the `PromptFacts` method so
    `inspect.getsource(TradingPipeline._build_x)` still reads the moved body.
    """

    def _prompt_facts_service(self) -> PromptFacts:
        return prompt_facts_for(self)

    @functools.wraps(PromptFacts._build_position_history)
    def _build_position_history(self, *args, **kwargs):
        return prompt_facts_for(self)._build_position_history(*args, **kwargs)

    @functools.wraps(PromptFacts._build_weekly_narrative)
    def _build_weekly_narrative(self, *args, **kwargs):
        return prompt_facts_for(self)._build_weekly_narrative(*args, **kwargs)

    @functools.wraps(PromptFacts._build_macro_trajectory)
    def _build_macro_trajectory(self, *args, **kwargs):
        return prompt_facts_for(self)._build_macro_trajectory(*args, **kwargs)

    @functools.wraps(PromptFacts._build_active_state_changes)
    def _build_active_state_changes(self, *args, **kwargs):
        return prompt_facts_for(self)._build_active_state_changes(*args, **kwargs)

    @functools.wraps(PromptFacts._build_rm_recent_verdicts)
    def _build_rm_recent_verdicts(self, *args, **kwargs):
        return prompt_facts_for(self)._build_rm_recent_verdicts(*args, **kwargs)

    @functools.wraps(PromptFacts._build_pm_recent_decisions)
    def _build_pm_recent_decisions(self, *args, **kwargs):
        return prompt_facts_for(self)._build_pm_recent_decisions(*args, **kwargs)

    @functools.wraps(PromptFacts._build_projected_portfolio)
    def _build_projected_portfolio(self, *args, **kwargs):
        service = prompt_facts_for(self)
        try:
            return service._build_projected_portfolio(*args, **kwargs)
        finally:
            # The one cache a builder writes; the decision stage reads it off
            # the pipeline, so the service's rebinding is written through.
            if "_last_symbol_sectors" in service.__dict__:
                self._last_symbol_sectors = service._last_symbol_sectors

    @functools.wraps(PromptFacts._build_recent_sells_for_grading)
    def _build_recent_sells_for_grading(self, *args, **kwargs):
        return prompt_facts_for(self)._build_recent_sells_for_grading(*args, **kwargs)

    @functools.wraps(PromptFacts._build_recent_buys_for_grading)
    def _build_recent_buys_for_grading(self, *args, **kwargs):
        return prompt_facts_for(self)._build_recent_buys_for_grading(*args, **kwargs)

    @functools.wraps(PromptFacts._build_recent_outlook_calibration)
    def _build_recent_outlook_calibration(self, *args, **kwargs):
        return prompt_facts_for(self)._build_recent_outlook_calibration(*args, **kwargs)

    @functools.wraps(PromptFacts._build_trade_grade_summary)
    def _build_trade_grade_summary(self, *args, **kwargs):
        return prompt_facts_for(self)._build_trade_grade_summary(*args, **kwargs)

    @functools.wraps(PromptFacts._build_post_exit_reality)
    def _build_post_exit_reality(self, *args, **kwargs):
        return prompt_facts_for(self)._build_post_exit_reality(*args, **kwargs)

    @functools.wraps(PromptFacts._build_recent_missed_lessons)
    def _build_recent_missed_lessons(self, *args, **kwargs):
        return prompt_facts_for(self)._build_recent_missed_lessons(*args, **kwargs)

    @functools.wraps(PromptFacts._persist_evening_replay_inputs)
    def _persist_evening_replay_inputs(self, *args, **kwargs):
        return prompt_facts_for(self)._persist_evening_replay_inputs(*args, **kwargs)

    @functools.wraps(PromptFacts._build_thesis_health_context)
    def _build_thesis_health_context(self, *args, **kwargs):
        return prompt_facts_for(self)._build_thesis_health_context(*args, **kwargs)

    @functools.wraps(PromptFacts._thesis_tech_trajectory_map)
    def _thesis_tech_trajectory_map(self, *args, **kwargs):
        return prompt_facts_for(self)._thesis_tech_trajectory_map(*args, **kwargs)

    @functools.wraps(PromptFacts._thesis_news_events_map)
    def _thesis_news_events_map(self, *args, **kwargs):
        return prompt_facts_for(self)._thesis_news_events_map(*args, **kwargs)

    @functools.wraps(PromptFacts._build_watchlist_candidates)
    def _build_watchlist_candidates(self, *args, **kwargs):
        return prompt_facts_for(self)._build_watchlist_candidates(*args, **kwargs)

    @functools.wraps(PromptFacts._build_recent_loss_pits)
    def _build_recent_loss_pits(self, *args, **kwargs):
        return prompt_facts_for(self)._build_recent_loss_pits(*args, **kwargs)

    @functools.wraps(PromptFacts._build_blocked_proposals)
    def _build_blocked_proposals(self, *args, **kwargs):
        return prompt_facts_for(self)._build_blocked_proposals(*args, **kwargs)

    @functools.wraps(PromptFacts._build_missed_opportunities_digest)
    def _build_missed_opportunities_digest(self, *args, **kwargs):
        return prompt_facts_for(self)._build_missed_opportunities_digest(*args, **kwargs)

    @functools.wraps(PromptFacts._missed_ops_held_set)
    def _missed_ops_held_set(self, *args, **kwargs):
        return prompt_facts_for(self)._missed_ops_held_set(*args, **kwargs)

    @functools.wraps(PromptFacts._missed_ops_tech_signal)
    def _missed_ops_tech_signal(self, *args, **kwargs):
        return prompt_facts_for(self)._missed_ops_tech_signal(*args, **kwargs)

    @functools.wraps(PromptFacts._missed_ops_news_signal)
    def _missed_ops_news_signal(self, *args, **kwargs):
        return prompt_facts_for(self)._missed_ops_news_signal(*args, **kwargs)

    @functools.wraps(PromptFacts._missed_ops_theme_tags)
    def _missed_ops_theme_tags(self, *args, **kwargs):
        return prompt_facts_for(self)._missed_ops_theme_tags(*args, **kwargs)

    @functools.wraps(PromptFacts._missed_ops_earnings_signal)
    def _missed_ops_earnings_signal(self, *args, **kwargs):
        return prompt_facts_for(self)._missed_ops_earnings_signal(*args, **kwargs)

    @functools.wraps(PromptFacts._missed_ops_macro_sector_map)
    def _missed_ops_macro_sector_map(self, *args, **kwargs):
        return prompt_facts_for(self)._missed_ops_macro_sector_map(*args, **kwargs)

    @staticmethod
    @functools.wraps(PromptFacts._actualize_trade_row)
    def _actualize_trade_row(*args, **kwargs):
        return PromptFacts._actualize_trade_row(*args, **kwargs)

    @staticmethod
    @functools.wraps(PromptFacts._build_macro_tech_alignment)
    def _build_macro_tech_alignment(*args, **kwargs):
        return PromptFacts._build_macro_tech_alignment(*args, **kwargs)

    @functools.wraps(PromptFacts._ensure_correlation_matrix)
    def _ensure_correlation_matrix(self, *args, **kwargs):
        return prompt_facts_for(self)._ensure_correlation_matrix(*args, **kwargs)

    @functools.wraps(PromptFacts._build_stop_map)
    def _build_stop_map(self, *args, **kwargs):
        return prompt_facts_for(self)._build_stop_map(*args, **kwargs)

    @functools.wraps(PromptFacts._build_portfolio_heat)
    def _build_portfolio_heat(self, *args, **kwargs):
        return prompt_facts_for(self)._build_portfolio_heat(*args, **kwargs)

    @functools.wraps(PromptFacts._build_pm_facts)
    def _build_pm_facts(self, *args, **kwargs):
        return prompt_facts_for(self)._build_pm_facts(*args, **kwargs)

    @staticmethod
    @functools.wraps(PromptFacts._log_conviction_outcome_for_operator)
    def _log_conviction_outcome_for_operator(*args, **kwargs):
        return PromptFacts._log_conviction_outcome_for_operator(*args, **kwargs)

    @functools.wraps(PromptFacts._build_calibration_note)
    def _build_calibration_note(self, *args, **kwargs):
        return prompt_facts_for(self)._build_calibration_note(*args, **kwargs)

    @functools.wraps(PromptFacts._compute_recent_performance)
    def _compute_recent_performance(self, *args, **kwargs):
        return prompt_facts_for(self)._compute_recent_performance(*args, **kwargs)

    @functools.wraps(PromptFacts._build_position_facts)
    def _build_position_facts(self, *args, **kwargs):
        return prompt_facts_for(self)._build_position_facts(*args, **kwargs)

    @functools.wraps(PromptFacts._build_review_metric_deltas)
    def _build_review_metric_deltas(self, *args, **kwargs):
        return prompt_facts_for(self)._build_review_metric_deltas(*args, **kwargs)

    @functools.wraps(PromptFacts._build_own_recent_decisions)
    def _build_own_recent_decisions(self, *args, **kwargs):
        return prompt_facts_for(self)._build_own_recent_decisions(*args, **kwargs)


# --- Patch mirroring. The family modules (`src/prompt_facts_*.py`) hold their
# own binding of `et_today` and the other module-level names the bodies call,
# and tests patch those names HERE (`patch("src.pipeline_prompt_facts.et_today")`)
# expecting the moved code to see the patched object. Mirroring every
# assignment into any family module that already holds that name keeps the
# patched object the SAME object on both sides. Exactly ONE write-through
# `__setattr__` serves every moved name; a second mirror block would cancel it.
_FAMILY_MODULES: tuple[str, ...] = (
    "src.prompt_facts_book",
    "src.prompt_facts_projection",
    "src.prompt_facts_seat_memory",
    "src.prompt_facts_grading",
    "src.prompt_facts_market_context",
    "src.prompt_facts_candidates",
)


class _FamilyMirroringModule(_types.ModuleType):
    def __setattr__(self, name: str, value) -> None:
        super().__setattr__(name, value)
        for module_path in _FAMILY_MODULES:
            family_module = _sys.modules.get(module_path)
            if family_module is not None and name in vars(family_module):
                setattr(family_module, name, value)

    def __delattr__(self, name: str) -> None:
        super().__delattr__(name)
        for module_path in _FAMILY_MODULES:
            family_module = _sys.modules.get(module_path)
            if family_module is not None and name in vars(family_module):
                delattr(family_module, name)


_sys.modules[__name__].__class__ = _FamilyMirroringModule
