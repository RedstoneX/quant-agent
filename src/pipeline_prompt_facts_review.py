"""Trade-review prompt facts: the pipeline HOLDS `ReviewFacts` instead of inheriting a mixin.

Bodies live in src/prompt_facts/review/ (five constructed parts behind one standalone
`ReviewFacts`, src/prompt_facts/review/facts.py). `hold_review_facts(host_cls)` installs a
same-named delegate for each of the eleven fact names, so every existing call site
(`self._build_post_exit_reality(...)`) keeps resolving. Nothing is snapshotted: the part
is built per call from the host instance's collaborators as they stand at that moment.
The module-level names below are re-exported unchanged for importers and patchers of
this module (the ONE mirror block for this module).
"""

import json as _json  # noqa: F401 -- re-exported
import logging
from pathlib import Path  # noqa: F401 -- re-exported

from src.prompt_facts.review.facts import REVIEW_FACT_NAMES, ReviewFacts
from src.risk.rules import peak_to_trough_pct  # noqa: F401 -- re-exported
from src.trading_calendar import et_today  # noqa: F401 -- re-exported

logger = logging.getLogger(__name__)

_BODIES = "src/prompt_facts/review/facts.py"


def build_review_facts(host) -> ReviewFacts:
    """The part, wired to `host`'s collaborators as they stand now.

    The grading body is handed the host's `_build_post_exit_reality` (this module's
    delegate, or whatever a test swapped in), never a body it owns."""
    return ReviewFacts(
        db=getattr(host, "db", None), broker=getattr(host, "broker", None),
        market=getattr(host, "market", None), sweeper=getattr(host, "_sweeper", None),
        exit_audit_actions=getattr(host, "_EXIT_AUDIT_ACTIONS", None),
        log_conviction_outcome_for_operator=getattr(host, "_log_conviction_outcome_for_operator", None),
        build_post_exit_reality=getattr(host, "_build_post_exit_reality", None),
    )


def _delegate(name: str):
    """An instance method forwarding `name` to a `ReviewFacts` built from the instance."""
    def shim(self, *args, **kwargs):
        return getattr(build_review_facts(self), name)(*args, **kwargs)
    shim.__name__ = name
    shim.__qualname__ = f"hold_review_facts.<locals>.{name}"
    shim.__doc__ = f"Thin delegate: body lives in {_BODIES}."
    shim._held_delegate = name
    return shim


def hold_review_facts(host_cls):
    """Make `host_cls` hold the review facts: a delegate per fact name, no base class.

    Usable as a class decorator; returns `host_cls`."""
    host_cls._review_facts = build_review_facts
    for name in REVIEW_FACT_NAMES:
        setattr(host_cls, name, _delegate(name))
    return host_cls
