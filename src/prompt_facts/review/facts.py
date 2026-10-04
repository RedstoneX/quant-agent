"""The trade-review prompt facts as ONE standalone part the pipeline HOLDS.

`ReviewFacts` inherits from nothing. It takes the host's collaborators as keyword-only
constructor arguments and builds the five fact-family parts under this package per call,
so a collaborator the holder reads off the host at call time is what each body sees.
The eleven `_build_*` names below are the whole surface the pipeline delegates here.

The one cross-family read (`_build_trade_grade_summary` -> `_build_post_exit_reality`)
defaults to this part's own exits body; a holder hands in the host's attribute instead,
so whatever a test swapped on the host is what the grading body calls.
"""

from __future__ import annotations

from src.prompt_facts.review.blocked import ReviewBlocked
from src.prompt_facts.review.calibration import ReviewCalibration
from src.prompt_facts.review.exits import ReviewExits
from src.prompt_facts.review.grading import ReviewGrading
from src.prompt_facts.review.replay import ReviewReplay

#: Every name a holder delegates to this part.
REVIEW_FACT_NAMES = (
    "_build_recent_sells_for_grading", "_build_recent_buys_for_grading",
    "_build_trade_grade_summary", "_build_post_exit_reality",
    "_build_recent_missed_lessons", "_build_recent_loss_pits",
    "_build_recent_outlook_calibration", "_build_calibration_note",
    "_compute_recent_performance", "_build_blocked_proposals",
    "_persist_evening_replay_inputs",
)


class ReviewFacts:
    """Graded sells/buys, calibration, grades, missed lessons, loss pits, blocked
    proposals, recent performance and the evening replay inputs; no pipeline behind it."""

    def __init__(
        self, *,
        db=None,
        broker=None,
        market=None,
        sweeper=None,
        exit_audit_actions=None,
        log_conviction_outcome_for_operator=None,
        build_post_exit_reality=None,
    ) -> None:
        self.db = db
        self.broker = broker
        self.market = market
        self._sweeper = sweeper
        self._exit_audit_actions = exit_audit_actions
        self._log_conviction_outcome_for_operator = log_conviction_outcome_for_operator
        self._handed_post_exit_reality = build_post_exit_reality

    # --- the five parts, built per call ---------------------------------------

    def grading(self) -> ReviewGrading:
        handed = self._handed_post_exit_reality
        return ReviewGrading(
            db=self.db, broker=self.broker, market=self.market, sweeper=self._sweeper,
            build_post_exit_reality=handed if handed is not None else self._build_post_exit_reality,
        )

    def exits(self) -> ReviewExits:
        return ReviewExits(
            db=self.db, broker=self.broker, sweeper=self._sweeper,
            exit_audit_actions=self._exit_audit_actions,
        )

    def calibration(self) -> ReviewCalibration:
        return ReviewCalibration(
            db=self.db,
            log_conviction_outcome_for_operator=self._log_conviction_outcome_for_operator,
        )

    def blocked(self) -> ReviewBlocked:
        return ReviewBlocked(db=self.db)

    def replay(self) -> ReviewReplay:
        return ReviewReplay()

    # --- the eleven facts ------------------------------------------------------

    def _build_recent_sells_for_grading(self, *args, **kwargs):
        return self.grading()._build_recent_sells_for_grading(*args, **kwargs)

    def _build_recent_buys_for_grading(self, *args, **kwargs):
        return self.grading()._build_recent_buys_for_grading(*args, **kwargs)

    def _build_trade_grade_summary(self, *args, **kwargs):
        return self.grading()._build_trade_grade_summary(*args, **kwargs)

    def _build_post_exit_reality(self, *args, **kwargs):
        return self.exits()._build_post_exit_reality(*args, **kwargs)

    def _build_recent_missed_lessons(self, *args, **kwargs):
        return self.exits()._build_recent_missed_lessons(*args, **kwargs)

    def _build_recent_loss_pits(self, *args, **kwargs):
        return self.exits()._build_recent_loss_pits(*args, **kwargs)

    def _build_recent_outlook_calibration(self, *args, **kwargs):
        return self.calibration()._build_recent_outlook_calibration(*args, **kwargs)

    def _build_calibration_note(self, *args, **kwargs):
        return self.calibration()._build_calibration_note(*args, **kwargs)

    def _compute_recent_performance(self, *args, **kwargs):
        return self.calibration()._compute_recent_performance(*args, **kwargs)

    def _build_blocked_proposals(self, *args, **kwargs):
        return self.blocked()._build_blocked_proposals(*args, **kwargs)

    def _persist_evening_replay_inputs(self, *args, **kwargs):
        return self.replay()._persist_evening_replay_inputs(*args, **kwargs)
