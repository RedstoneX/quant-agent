"""Trade-review prompt facts: graded sells/buys, calibration, grades, missed lessons,
loss pits, blocked proposals, recent performance and the evening replay inputs.

Bodies live in src/prompt_facts/review/ (five constructed parts); this mixin keeps
same-named thin shims, built per call so a collaborator swapped after construction is
what the body sees. The module-level names below are re-exported unchanged for
importers and patchers of this module (the ONE mirror block for this module).
"""

import json as _json  # noqa: F401 -- re-exported
import logging
from pathlib import Path  # noqa: F401 -- re-exported

from src.prompt_facts.review.blocked import ReviewBlocked
from src.prompt_facts.review.calibration import ReviewCalibration
from src.prompt_facts.review.exits import ReviewExits
from src.prompt_facts.review.grading import ReviewGrading
from src.prompt_facts.review.replay import ReviewReplay
from src.risk.rules import peak_to_trough_pct  # noqa: F401 -- re-exported
from src.trading_calendar import et_today  # noqa: F401 -- re-exported

logger = logging.getLogger(__name__)


class PromptFactsReviewMixin:
    """Trade-review prompt facts; every body lives on a part under src/prompt_facts/review/.

    Each `_review_*` builder reads the host's collaborators at call time. The grading
    part is handed the host's `_build_post_exit_reality` (a shim onto the exits part, or
    whatever a test swapped in), never a body it owns, so no recursion guard is needed."""

    def _review_grading(self) -> ReviewGrading:
        return ReviewGrading(
            db=getattr(self, "db", None), broker=getattr(self, "broker", None),
            market=getattr(self, "market", None), sweeper=getattr(self, "_sweeper", None),
            build_post_exit_reality=getattr(self, "_build_post_exit_reality", None),
        )

    def _review_exits(self) -> ReviewExits:
        return ReviewExits(
            db=getattr(self, "db", None), broker=getattr(self, "broker", None),
            sweeper=getattr(self, "_sweeper", None),
            exit_audit_actions=getattr(self, "_EXIT_AUDIT_ACTIONS", None),
        )

    def _review_calibration(self) -> ReviewCalibration:
        return ReviewCalibration(
            db=getattr(self, "db", None),
            log_conviction_outcome_for_operator=getattr(self, "_log_conviction_outcome_for_operator", None),
        )

    def _review_blocked(self) -> ReviewBlocked:
        return ReviewBlocked(db=getattr(self, "db", None))

    def _review_replay(self) -> ReviewReplay:
        return ReviewReplay()

    def _build_recent_sells_for_grading(self, *args, **kwargs):
        """Thin shim: body moved to src/prompt_facts/review/grading.py."""
        return self._review_grading()._build_recent_sells_for_grading(*args, **kwargs)

    def _build_recent_buys_for_grading(self, *args, **kwargs):
        """Thin shim: body moved to src/prompt_facts/review/grading.py."""
        return self._review_grading()._build_recent_buys_for_grading(*args, **kwargs)

    def _build_trade_grade_summary(self, *args, **kwargs):
        """Thin shim: body moved to src/prompt_facts/review/grading.py."""
        return self._review_grading()._build_trade_grade_summary(*args, **kwargs)

    def _build_post_exit_reality(self, *args, **kwargs):
        """Thin shim: body moved to src/prompt_facts/review/exits.py."""
        return self._review_exits()._build_post_exit_reality(*args, **kwargs)

    def _build_recent_missed_lessons(self, *args, **kwargs):
        """Thin shim: body moved to src/prompt_facts/review/exits.py."""
        return self._review_exits()._build_recent_missed_lessons(*args, **kwargs)

    def _build_recent_loss_pits(self, *args, **kwargs):
        """Thin shim: body moved to src/prompt_facts/review/exits.py."""
        return self._review_exits()._build_recent_loss_pits(*args, **kwargs)

    def _build_recent_outlook_calibration(self, *args, **kwargs):
        """Thin shim: body moved to src/prompt_facts/review/calibration.py."""
        return self._review_calibration()._build_recent_outlook_calibration(*args, **kwargs)

    def _build_calibration_note(self, *args, **kwargs):
        """Thin shim: body moved to src/prompt_facts/review/calibration.py."""
        return self._review_calibration()._build_calibration_note(*args, **kwargs)

    def _compute_recent_performance(self, *args, **kwargs):
        """Thin shim: body moved to src/prompt_facts/review/calibration.py."""
        return self._review_calibration()._compute_recent_performance(*args, **kwargs)

    def _build_blocked_proposals(self, *args, **kwargs):
        """Thin shim: body moved to src/prompt_facts/review/blocked.py."""
        return self._review_blocked()._build_blocked_proposals(*args, **kwargs)

    def _persist_evening_replay_inputs(self, *args, **kwargs):
        """Thin shim: body moved to src/prompt_facts/review/replay.py."""
        return self._review_replay()._persist_evening_replay_inputs(*args, **kwargs)
