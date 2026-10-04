"""Grading, calibration and replay facts for the review-shaped prompts.

The bodies left this file for `src/prompt_facts/` (third prompt-facts instalment):
five standalone parts, each built from explicit keyword-only collaborators. This
mixin keeps same-named thin shims so every call site and every instance patch on
the pipeline still resolves; it is a seam, not a boundary -- the boundaries are
the parts, witnessed by tests/test_prompt_facts_review_parts_boundary.py.
"""
import logging

# -- re-export block (the ONE such block in this module): the moved bodies'
# -- classes stay importable from the old location.
from src.prompt_facts.blocked_proposals import BlockedProposals  # noqa: F401 — re-exported
from src.prompt_facts.evening_replay_inputs import EveningReplayInputs  # noqa: F401 — re-exported
from src.prompt_facts.outcome_review import OutcomeReview  # noqa: F401 — re-exported
from src.prompt_facts.outlook_review import OutlookReview  # noqa: F401 — re-exported
from src.prompt_facts.trade_grading import TradeGrading  # noqa: F401 — re-exported
# -- end re-export block

logger = logging.getLogger(__name__)


def _trade_grading(host) -> TradeGrading:
    """Builds the standalone part from the host pipeline's collaborators, read live per
    call so one swapped after construction is what the body sees. Module-level, so a
    single shim bound onto a bare stub (as tests do) still resolves it."""
    return TradeGrading(build_post_exit_reality=getattr(host, "_build_post_exit_reality", None), sweeper=getattr(host, "_sweeper", None), broker=getattr(host, "broker", None), db=getattr(host, "db", None), market=getattr(host, "market", None))


def _outcome_review(host) -> OutcomeReview:
    """Builds the standalone part from the host pipeline's collaborators, read live per
    call so one swapped after construction is what the body sees. Module-level, so a
    single shim bound onto a bare stub (as tests do) still resolves it."""
    return OutcomeReview(exit_audit_actions=getattr(host, "_EXIT_AUDIT_ACTIONS", None), log_conviction_outcome_for_operator=getattr(host, "_log_conviction_outcome_for_operator", None), sweeper=getattr(host, "_sweeper", None), broker=getattr(host, "broker", None), db=getattr(host, "db", None))


def _outlook_review(host) -> OutlookReview:
    """Builds the standalone part from the host pipeline's collaborators, read live per
    call so one swapped after construction is what the body sees. Module-level, so a
    single shim bound onto a bare stub (as tests do) still resolves it."""
    return OutlookReview(db=getattr(host, "db", None))


def _blocked_proposals(host) -> BlockedProposals:
    """Builds the standalone part from the host pipeline's collaborators, read live per
    call so one swapped after construction is what the body sees. Module-level, so a
    single shim bound onto a bare stub (as tests do) still resolves it."""
    return BlockedProposals(db=getattr(host, "db", None))


def _evening_replay_inputs(host) -> EveningReplayInputs:
    """Builds the standalone part from the host pipeline's collaborators, read live per
    call so one swapped after construction is what the body sees. Module-level, so a
    single shim bound onto a bare stub (as tests do) still resolves it."""
    return EveningReplayInputs()


class PromptFactsReviewMixin:
    """Grading, calibration and replay facts; mixed into `PromptFactsMixin`."""

    def _build_recent_sells_for_grading(self, *args, **kwargs):
        """Thin shim: body moved to src/prompt_facts/trade_grading.py."""
        return _trade_grading(self)._build_recent_sells_for_grading(*args, **kwargs)

    def _build_recent_buys_for_grading(self, *args, **kwargs):
        """Thin shim: body moved to src/prompt_facts/trade_grading.py."""
        return _trade_grading(self)._build_recent_buys_for_grading(*args, **kwargs)

    def _build_trade_grade_summary(self, *args, **kwargs):
        """Thin shim: body moved to src/prompt_facts/trade_grading.py."""
        return _trade_grading(self)._build_trade_grade_summary(*args, **kwargs)

    def _build_post_exit_reality(self, *args, **kwargs):
        """Thin shim: body moved to src/prompt_facts/outcome_review.py."""
        return _outcome_review(self)._build_post_exit_reality(*args, **kwargs)

    def _build_recent_loss_pits(self, *args, **kwargs):
        """Thin shim: body moved to src/prompt_facts/outcome_review.py."""
        return _outcome_review(self)._build_recent_loss_pits(*args, **kwargs)

    def _build_calibration_note(self, *args, **kwargs):
        """Thin shim: body moved to src/prompt_facts/outcome_review.py."""
        return _outcome_review(self)._build_calibration_note(*args, **kwargs)

    def _compute_recent_performance(self, *args, **kwargs):
        """Thin shim: body moved to src/prompt_facts/outcome_review.py."""
        return _outcome_review(self)._compute_recent_performance(*args, **kwargs)

    def _build_recent_outlook_calibration(self, *args, **kwargs):
        """Thin shim: body moved to src/prompt_facts/outlook_review.py."""
        return _outlook_review(self)._build_recent_outlook_calibration(*args, **kwargs)

    def _build_recent_missed_lessons(self, *args, **kwargs):
        """Thin shim: body moved to src/prompt_facts/outlook_review.py."""
        return _outlook_review(self)._build_recent_missed_lessons(*args, **kwargs)

    def _build_blocked_proposals(self, *args, **kwargs):
        """Thin shim: body moved to src/prompt_facts/blocked_proposals.py."""
        return _blocked_proposals(self)._build_blocked_proposals(*args, **kwargs)

    def _persist_evening_replay_inputs(self, *args, **kwargs):
        """Thin shim: body moved to src/prompt_facts/evening_replay_inputs.py."""
        return _evening_replay_inputs(self)._persist_evening_replay_inputs(*args, **kwargs)
