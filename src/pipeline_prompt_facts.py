"""Prompt-facts builders: read-only DB/broker reads turned into LLM context.

Bodies live in src/prompt_facts/ (eight constructed parts); this mixin keeps same-named
thin shims, built per call so a collaborator swapped after construction is what the body
sees. The module-level names below are re-exported unchanged for importers and patchers
of this module (the ONE mirror block for this module).

Step 1 of `docs/PIPELINE_SPLIT_PLAN.md` (board item 210). Moved verbatim out of
`src/pipeline.py` as a mixin, so `TradingPipeline` keeps every one of these as
its own attribute and every test that patches or calls them is untouched.

The defining property of this module: it places no orders, cancels nothing and
amends no stop. `_handle_ex_dividends` sat in this cluster's line range and does
move live stops, so it is NOT here — it goes to the protection module in step 2
(plan §1 correction, 2026-10-01).

Nothing here may import `src.pipeline`: this module is one of its bases.
"""

import json as _json  # noqa: F401 -- re-exported
import logging
import re  # noqa: F401 -- re-exported
from pathlib import Path  # noqa: F401 -- re-exported

from src.execution.stop_read import read_stop  # noqa: F401 -- re-exported
from src.models import TechAnalysisResult  # noqa: F401 -- re-exported
from src.pipeline_context import PMFacts  # noqa: F401 -- re-exported
from src.pipeline_prompt_facts_pure import (  # noqa: F401  re-exports, see pipeline.py
    _actualize_trade_row,
    _build_macro_tech_alignment,
    _missed_ops_quality_metrics,
    _valuation_signal_from,
)
from src.pipeline_prompt_facts_review import PromptFactsReviewMixin
from src.prompt_facts.decisions import PromptDecisions
from src.prompt_facts.exposure import PromptExposure
from src.prompt_facts.heat import PromptHeat
from src.prompt_facts.history import PromptHistory
from src.prompt_facts.missed_ops_signals import MissedOpsSignals
from src.prompt_facts.pm_facts import _PM_PROFILE_SYMBOL_CAP, PromptPMFacts  # noqa: F401 -- re-exported
from src.prompt_facts.position_facts import PromptPositionFacts
from src.prompt_facts.projected import PromptProjected
from src.prompt_facts.watchlist import PromptWatchlist
from src.quantities import avg_dollar_volume, dollar_volumes  # noqa: F401 -- re-exported
from src.risk.metrics import drift_flag as _drift_flag_check  # noqa: F401 -- re-exported
from src.risk.metrics import unrealized_pnl_pct  # noqa: F401 -- re-exported
from src.risk.rules import peak_to_trough_pct, position_weight_pct  # noqa: F401 -- re-exported
from src.trading_calendar import et_today, session_date_key  # noqa: F401 -- re-exported

#: The moved code logged under `src.pipeline` before the move and still does;
#: binding the name rather than `__name__` keeps log records byte-identical.
logger = logging.getLogger("src.pipeline")


class PromptFactsMixin(PromptFactsReviewMixin):
    """Read-only prompt-context builders mixed into `TradingPipeline`; every body lives on a part under src/prompt_facts/.

    Each `_prompt_*` builder reads the host's collaborators at call time. Cross-family reads
    (pm facts -> heat and history; heat -> stop map) are handed the host's shim, never a body
    the part owns, so no recursion guard is needed. The projected-portfolio part is handed the
    host as its sector-cache owner, so `_last_symbol_sectors` is read and written through live."""

    # Free-standing helpers now live in src/pipeline_prompt_facts_pure.py;
    # re-bound here so `self._x(...)` / `PromptFactsMixin._x` keep working.
    _actualize_trade_row = staticmethod(_actualize_trade_row)
    _build_macro_tech_alignment = staticmethod(_build_macro_tech_alignment)

    def _prompt_history(self) -> PromptHistory:
        return PromptHistory(db=getattr(self, "db", None), tech_store=getattr(self, "tech_store", None), macro_store=getattr(self, "macro_store", None), news_store=getattr(self, "news_store", None))

    def _prompt_decisions(self) -> PromptDecisions:
        return PromptDecisions(db=getattr(self, "db", None), parse_logged_agent_response=getattr(self, "_parse_logged_agent_response", None))

    def _prompt_projected(self) -> PromptProjected:
        return PromptProjected(
            sector_cache_owner=self, portfolio_constructor=getattr(self, "portfolio_constructor", None),
            risk_engine=getattr(self, "risk_engine", None),
        )

    def _prompt_watchlist(self) -> PromptWatchlist:
        return PromptWatchlist(db=getattr(self, "db", None))

    def _prompt_exposure(self) -> PromptExposure:
        return PromptExposure(config=getattr(self, "config", None), market=getattr(self, "market", None), db=getattr(self, "db", None), broker=getattr(self, "broker", None))

    def _prompt_heat(self) -> PromptHeat:
        return PromptHeat(sweeper=getattr(self, "_sweeper", None), build_stop_map=getattr(self, "_build_stop_map", None))

    def _prompt_pm_facts(self) -> PromptPMFacts:
        return PromptPMFacts(db=getattr(self, "db", None), config=getattr(self, "config", None), parse_logged_agent_response=getattr(self, "_parse_logged_agent_response", None), build_portfolio_heat=getattr(self, "_build_portfolio_heat", None), build_position_history=getattr(self, "_build_position_history", None))

    def _prompt_position_facts(self) -> PromptPositionFacts:
        return PromptPositionFacts(db=getattr(self, "db", None), broker=getattr(self, "broker", None), atr_for_symbol=getattr(self, "_atr_for_symbol", None))

    def _build_thesis_health_context(self, *args, **kwargs):
        """Thin shim: body moved to src/prompt_facts/missed_ops_signals.py."""
        return self._missed_ops_signals()._build_thesis_health_context(*args, **kwargs)

    def _missed_ops_signals(self) -> MissedOpsSignals:
        """Standalone signal helpers for the missed-ops digest and thesis-health review
        (bodies moved to src/prompt_facts/missed_ops_signals.py). Built per call so a
        collaborator swapped after construction is what the body sees. No lifted body
        is passed back in: none of these helpers calls another, so there is nothing
        for the shim to overwrite (see src/cost_circuit/parts/shim_guard.py)."""
        return MissedOpsSignals(
            db=getattr(self, "db", None),
            news_store=getattr(self, "news_store", None),
            earnings_provider=getattr(self, "earnings_provider", None),
            macro_store=getattr(self, "macro_store", None),
            parse_logged_agent_response=getattr(self, "_parse_logged_agent_response", None),
            broker=getattr(self, "broker", None),
            market=getattr(self, "market", None),
            config=getattr(self, "config", None),
        )

    def _missed_ops_held_set(self, *args, **kwargs):
        """Thin shim: body moved to src/prompt_facts/missed_ops_signals.py."""
        return self._missed_ops_signals()._missed_ops_held_set(*args, **kwargs)

    def _missed_ops_tech_signal(self, *args, **kwargs):
        """Thin shim: body moved to src/prompt_facts/missed_ops_signals.py."""
        return self._missed_ops_signals()._missed_ops_tech_signal(*args, **kwargs)

    def _missed_ops_news_signal(self, *args, **kwargs):
        """Thin shim: body moved to src/prompt_facts/missed_ops_signals.py."""
        return self._missed_ops_signals()._missed_ops_news_signal(*args, **kwargs)

    def _missed_ops_theme_tags(self, *args, **kwargs):
        """Thin shim: body moved to src/prompt_facts/missed_ops_signals.py."""
        return self._missed_ops_signals()._missed_ops_theme_tags(*args, **kwargs)

    def _missed_ops_earnings_signal(self, *args, **kwargs):
        """Thin shim: body moved to src/prompt_facts/missed_ops_signals.py."""
        return self._missed_ops_signals()._missed_ops_earnings_signal(*args, **kwargs)

    def _missed_ops_macro_sector_map(self, *args, **kwargs):
        """Thin shim: body moved to src/prompt_facts/missed_ops_signals.py."""
        return self._missed_ops_signals()._missed_ops_macro_sector_map(*args, **kwargs)

    def _thesis_tech_trajectory_map(self, *args, **kwargs):
        """Thin shim: body moved to src/prompt_facts/missed_ops_signals.py."""
        return self._missed_ops_signals()._thesis_tech_trajectory_map(*args, **kwargs)

    def _thesis_news_events_map(self, *args, **kwargs):
        """Thin shim: body moved to src/prompt_facts/missed_ops_signals.py."""
        return self._missed_ops_signals()._thesis_news_events_map(*args, **kwargs)

    def _build_missed_opportunities_digest(self, *args, **kwargs):
        """Thin shim: body moved to src/prompt_facts/missed_ops_signals.py."""
        return self._missed_ops_signals()._build_missed_opportunities_digest(*args, **kwargs)

    def _build_position_history(self, *args, **kwargs):
        """Thin shim: body moved to src/prompt_facts/history.py."""
        return self._prompt_history()._build_position_history(*args, **kwargs)

    def _build_weekly_narrative(self, *args, **kwargs):
        """Thin shim: body moved to src/prompt_facts/history.py."""
        return self._prompt_history()._build_weekly_narrative(*args, **kwargs)

    def _build_macro_trajectory(self, *args, **kwargs):
        """Thin shim: body moved to src/prompt_facts/history.py."""
        return self._prompt_history()._build_macro_trajectory(*args, **kwargs)

    def _build_active_state_changes(self, *args, **kwargs):
        """Thin shim: body moved to src/prompt_facts/history.py."""
        return self._prompt_history()._build_active_state_changes(*args, **kwargs)

    def _build_rm_recent_verdicts(self, *args, **kwargs):
        """Thin shim: body moved to src/prompt_facts/decisions.py."""
        return self._prompt_decisions()._build_rm_recent_verdicts(*args, **kwargs)

    def _build_pm_recent_decisions(self, *args, **kwargs):
        """Thin shim: body moved to src/prompt_facts/decisions.py."""
        return self._prompt_decisions()._build_pm_recent_decisions(*args, **kwargs)

    def _build_review_metric_deltas(self, *args, **kwargs):
        """Thin shim: body moved to src/prompt_facts/decisions.py."""
        return self._prompt_decisions()._build_review_metric_deltas(*args, **kwargs)

    def _build_own_recent_decisions(self, *args, **kwargs):
        """Thin shim: body moved to src/prompt_facts/decisions.py."""
        return self._prompt_decisions()._build_own_recent_decisions(*args, **kwargs)

    def _build_projected_portfolio(self, *args, **kwargs):
        """Thin shim: body moved to src/prompt_facts/projected.py; the host stays the sector-cache owner."""
        return self._prompt_projected()._build_projected_portfolio(*args, **kwargs)

    def _build_watchlist_candidates(self, *args, **kwargs):
        """Thin shim: body moved to src/prompt_facts/watchlist.py."""
        return self._prompt_watchlist()._build_watchlist_candidates(*args, **kwargs)

    def _ensure_correlation_matrix(self, *args, **kwargs):
        """Thin shim: body moved to src/prompt_facts/exposure.py."""
        return self._prompt_exposure()._ensure_correlation_matrix(*args, **kwargs)

    def _build_stop_map(self, *args, **kwargs):
        """Thin shim: body moved to src/prompt_facts/exposure.py."""
        return self._prompt_exposure()._build_stop_map(*args, **kwargs)

    def _build_portfolio_heat(self, *args, **kwargs):
        """Thin shim: body moved to src/prompt_facts/heat.py."""
        return self._prompt_heat()._build_portfolio_heat(*args, **kwargs)

    def _build_pm_facts(self, *args, **kwargs):
        """Thin shim: body moved to src/prompt_facts/pm_facts.py."""
        return self._prompt_pm_facts()._build_pm_facts(*args, **kwargs)

    @staticmethod
    def _log_conviction_outcome_for_operator(stats: dict) -> None:
        """Thin shim: body moved to src/prompt_facts/pm_facts.py."""
        return PromptPMFacts._log_conviction_outcome_for_operator(stats)

    def _build_position_facts(self, *args, **kwargs):
        """Thin shim: body moved to src/prompt_facts/position_facts.py."""
        return self._prompt_position_facts()._build_position_facts(*args, **kwargs)
