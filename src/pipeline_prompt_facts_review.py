"""Trade-review prompt facts: the prompt-facts seat HOLDS the standalone part instead of
inheriting a mixin.

The part is `PromptFactsReview` (src/prompt_facts/review/held.py); the fact bodies live on
the five family parts under src/prompt_facts/review/. `hold_prompt_facts_review(host_cls)`
installs one same-named instance-method delegate per review name, each forwarding to the
part the host instance holds (`review_of(host)`: built on first use and kept on the
instance, because tests build hosts without running `__init__`). Nothing is snapshotted:
the part reads every collaborator off the host at each call. The module-level names below
are re-exported unchanged for importers and patchers of this module (the ONE mirror block
for this module).
"""

import json as _json  # noqa: F401 -- re-exported
import logging
from pathlib import Path  # noqa: F401 -- re-exported

from src.prompt_facts.review.held import PromptFactsReview
from src.risk.rules import peak_to_trough_pct  # noqa: F401 -- re-exported
from src.trading_calendar import et_today  # noqa: F401 -- re-exported

logger = logging.getLogger(__name__)

#: Where the host instance keeps its held part.
HOLDER_ATTR = "_prompt_facts_review"

#: Every review name the host exposes, delegated to the held part. `_build_post_exit_reality`
#: is a COLLABORATOR of the grading part, so the part reads it off the host live; the host's
#: own delegate for it goes straight to the exits part.
DELEGATED = (
    "_review_grading", "_review_exits", "_review_calibration", "_review_blocked", "_review_replay",
    "_build_recent_sells_for_grading", "_build_recent_buys_for_grading", "_build_trade_grade_summary",
    "_build_post_exit_reality", "_build_recent_missed_lessons", "_build_recent_loss_pits",
    "_build_recent_outlook_calibration", "_build_calibration_note", "_compute_recent_performance",
    "_build_blocked_proposals", "_persist_evening_replay_inputs",
)


def review_of(host) -> PromptFactsReview:
    """The part `host` holds: built on first use with the host handed in, then kept on it."""
    held = getattr(host, HOLDER_ATTR, None)
    if held is None:
        held = PromptFactsReview(host=host)
        try:
            setattr(host, HOLDER_ATTR, held)
        except AttributeError:  # a host that refuses attributes still gets a working part
            pass
    return held


def _delegate(name: str):
    if name == "_build_post_exit_reality":
        def shim(self, *args, **kwargs):
            return review_of(self)._review_exits()._build_post_exit_reality(*args, **kwargs)
    else:
        def shim(self, *args, **kwargs):
            return getattr(review_of(self), name)(*args, **kwargs)
    shim.__name__ = name
    shim.__qualname__ = f"hold_prompt_facts_review.<locals>.{name}"
    shim.__doc__ = "Thin delegate: body lives in src/prompt_facts/review/held.py."
    shim._held_delegate = name
    return shim


def hold_prompt_facts_review(host_cls):
    """Make instances of `host_cls` hold one `PromptFactsReview` and delegate the review names to it."""
    setattr(host_cls, HOLDER_ATTR, None)  # per-instance cache filled by `review_of`
    for name in DELEGATED:
        setattr(host_cls, name, _delegate(name))
    return host_cls
