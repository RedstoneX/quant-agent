"""The trade-review seat as ONE standalone part: `PromptFactsReview`.

Built alone with its host handed in (`PromptFactsReview(host=...)`) and exercised with
nothing behind it. Every collaborator -- `db`, `broker`, `market`, `_sweeper`,
`_build_post_exit_reality`, `_EXIT_AUDIT_ACTIONS`, `_log_conviction_outcome_for_operator` --
is read off the host at EVERY call through `_LiveRead`, never copied at construction, so a
collaborator swapped on the host after the part was built is the one the bodies see.

The five `_review_*` builders and the eleven same-named shims are moved VERBATIM (AST-identical)
from `PromptFactsReviewMixin` (src/pipeline_prompt_facts_review.py, retired 2026-10-04). The
prompt-facts seat HOLDS one of these, installed by `hold_prompt_facts_review`, and inherits
nothing from it; the fact bodies themselves stay on the five family parts in this package.
"""

from src.prompt_facts.review.blocked import ReviewBlocked
from src.prompt_facts.review.calibration import ReviewCalibration
from src.prompt_facts.review.exits import ReviewExits
from src.prompt_facts.review.grading import ReviewGrading
from src.prompt_facts.review.replay import ReviewReplay

#: The host attributes the builders read; each is a live read, never a snapshot.
HOST_COLLABORATORS = (
    "db", "broker", "market", "_sweeper", "_build_post_exit_reality",
    "_EXIT_AUDIT_ACTIONS", "_log_conviction_outcome_for_operator",
)


class _LiveRead:
    """Descriptor: `part.<name>` is `getattr(part._host, name, None)` taken at this call."""

    def __init__(self, name: str) -> None:
        self.name = name

    def __get__(self, part, owner=None):
        if part is None:
            return self
        return getattr(part._host, self.name, None)


class PromptFactsReview:
    """Trade-review prompt facts; every body lives on a part under src/prompt_facts/review/.

    Each `_review_*` builder reads the host's collaborators at call time. The grading
    part is handed the host's `_build_post_exit_reality` (a shim onto the exits part, or
    whatever a test swapped in), never a body it owns, so no recursion guard is needed."""

    db = _LiveRead("db")
    broker = _LiveRead("broker")
    market = _LiveRead("market")
    _sweeper = _LiveRead("_sweeper")
    # A collaborator of the grading part, so it is the HOST's (its delegate onto the exits
    # part, or whatever a test swapped in), read live; exercised alone through `_review_exits()`.
    _build_post_exit_reality = _LiveRead("_build_post_exit_reality")
    _EXIT_AUDIT_ACTIONS = _LiveRead("_EXIT_AUDIT_ACTIONS")
    _log_conviction_outcome_for_operator = _LiveRead("_log_conviction_outcome_for_operator")

    def __init__(self, *, host) -> None:
        self._host = host

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
