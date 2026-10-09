"""src.prompt_facts.watchlist -- the watchlist candidates.

Bodies moved verbatim from src/pipeline_prompt_facts.py (`PromptFactsMixin`), which keeps
same-named thin shims built per call. Every collaborator is an explicit keyword-only
constructor argument, so this builds and runs with no pipeline behind it.
"""

import logging

#: The moved code logged under `src.pipeline` before the move and still does;
#: binding the name rather than `__name__` keeps log records byte-identical.
logger = logging.getLogger("src.pipeline")


class PromptWatchlist:
    """The watchlist candidates; standalone, built from explicit collaborators."""

    def __init__(
        self,
        *,
        db=None,
    ) -> None:
        self.db = db

    def _build_watchlist_candidates(
        self,
        lookback_days: int = 30,
    ) -> list[dict]:
        """Symbols the evening analyst has repeatedly flagged as "add" or
        "watch" to the trading universe — the surface the user reviews
        when deciding whether to actually expand the 77-symbol universe.

        Reads `insights.missed_opportunities_json` for the last N days,
        filters entries with `universe_addition_recommendation != "no"`,
        aggregates by symbol.

        Returns a sorted list of dicts:
          [
            {
              "symbol": "VST",
              "add_count": int,
              "watch_count": int,
              "total_flags": int,
              "dates": [ISO date, ...],   # newest first
              "themes": [str, ...],        # distinct theme_if_any seen
              "latest_reason": str,        # most recent universe_addition_reason
              "latest_miss_category": str, # e.g. "theme_blindspot"
            },
            ...
          ]

        Sort: (add_count desc, watch_count desc, total_flags desc, symbol).
        One "add" carries more weight than one "watch" — an "add" means
        the LLM cleared ALL four quality bars (volume + sustain + theme
        + fundamentals), a "watch" means most-but-not-all.

        THIS FUNCTION DOES NOT MODIFY THE UNIVERSE. Universe expansion
        is a human decision — edit config/settings.yaml manually after
        reviewing this output. By design, so that the system can't
        casually grow the curated list.

        The aggregation itself is a pure function
        (`src.watchlist_candidates.build_watchlist_candidates`) — this
        method is now a thin fetch-then-aggregate wrapper so
        `src/api/db_reads.py` can compute the identical output from its own
        read-only `insights` query without importing `TradingPipeline`
        (Stage 2 Checkpoint C).
        """
        try:
            rows = self.db.get_recent_insights(limit=lookback_days + 5)
        except Exception as exc:
            logger.warning(
                "watchlist_candidates: insights fetch failed: %s",
                exc,
            )
            return []
        if not rows:
            return []
        from src.watchlist_candidates import build_watchlist_candidates

        return build_watchlist_candidates(rows, lookback_days)
