"""src.prompt_facts.review.replay -- the evening replay input persistence.

Bodies moved verbatim from src/pipeline_prompt_facts_review.py (`PromptFactsReviewMixin`),
which keeps same-named thin shims built per call. Every collaborator is an explicit
keyword-only constructor argument, so this builds and runs with no pipeline behind it.
"""

import json as _json
import logging
from pathlib import Path


logger = logging.getLogger(__name__)


class ReviewReplay:
    """The evening replay input persistence; standalone, built from explicit collaborators."""

    def __init__(self) -> None:
        pass

    def _persist_evening_replay_inputs(
        self,
        *,
        date_iso: str,
        run_id: str,
        positions,
        macro_summary: dict,
        total_value: float,
        daily_pnl: float,
        daily_return_pct: float,
        today_trades: list,
        prior_outlook,
        recent_sells: list,
        recent_buys: list,
        news_intel,
        earnings_analyses: list,
        weekly_narrative: str,
        active_state_changes: str,
        outlook_calibration: dict,
        missed_ops_snapshots: list,
        thesis_health_context: dict,
        root_dir: str = "data/evening_replays",
    ) -> Path:
        """Freeze the full evening-analyst input set as JSON so a candidate
        prompt can be re-scored on the same inputs weeks later.

        Pydantic objects (Position, NewsIntelligenceReport, MissedOpportunity
        Snapshot) are serialized via model_dump; the replay script reverses
        it. Plain dicts/strings pass through untouched. Writes atomically to
        data/evening_replays/YYYY-MM-DD.json. Caller treats the whole call
        as best-effort — a disk full or permission issue on the replay dir
        should NOT break the live evening run.
        """
        from pathlib import Path as _Path
        import json as _json
        import os as _os

        def _dump(obj):
            """Recursively convert Pydantic → dict; leave plain JSON types."""
            if obj is None or isinstance(obj, (bool, int, float, str)):
                return obj
            if hasattr(obj, "model_dump"):
                return obj.model_dump(mode="json")
            if isinstance(obj, list):
                return [_dump(x) for x in obj]
            if isinstance(obj, tuple):
                return [_dump(x) for x in obj]
            if isinstance(obj, dict):
                return {str(k): _dump(v) for k, v in obj.items()}
            # Fall-through: stringify — better than crashing the persist.
            return str(obj)

        payload = {
            "schema_version": 1,
            "date": date_iso,
            "run_id": run_id,
            "kwargs": {
                "positions": [_dump(p) for p in (positions or [])],
                "macro_summary": _dump(macro_summary),
                "total_value": total_value,
                "daily_pnl": daily_pnl,
                "daily_return_pct": daily_return_pct,
                "today_trades": _dump(today_trades),
                "prior_outlook": _dump(prior_outlook),
                "recent_sells": _dump(recent_sells),
                "recent_buys": _dump(recent_buys),
                "news_intel": _dump(news_intel),
                "earnings_analyses": _dump(earnings_analyses),
                "weekly_narrative": weekly_narrative,
                "active_state_changes": active_state_changes,
                "outlook_calibration": _dump(outlook_calibration),
                "missed_ops_snapshots": [_dump(s) for s in (missed_ops_snapshots or [])],
                "thesis_health_context": _dump(thesis_health_context),
            },
        }

        out_dir = _Path(root_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f"{date_iso}.json"
        tmp = out_path.with_suffix(".json.tmp")
        tmp.write_text(_json.dumps(payload, indent=2, ensure_ascii=False))
        _os.replace(str(tmp), str(out_path))
        logger.info("Evening replay inputs frozen → %s", out_path)
        return out_path
