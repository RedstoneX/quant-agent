"""Record that the PM sizing-narrative cross-check RAN -- hit or no hit.

`src.risk_narrative_check.check_sizing_narrative` compares the portfolio
manager's free-prose `sizing_logic` against each symbol's emitted
`risk_allocation_pct`. It is detection only: it never changes a target.

THE DEFECT THIS MODULE FIXES IS THE RECORDING, NOT THE DETECTION. The call
site wrote a row only when it found a mismatch, and swallowed any failure at
`logger.debug`. So an empty evidence stream meant one of three different
things at once -- "ran on every session and agreed", "never reached the call
site", or "raised and was discarded" -- which need opposite fixes and cannot
be told apart after the fact.

That ambiguity cost a real investigation. Five genuine prose-vs-field
mismatches sit in the production record between 2026-09-15 and 2026-09-24,
and the live stream holds zero hits, which was read as a check that does
nothing. Replaying all 79 stored PM sessions through the unchanged detector
on 2026-10-05 finds exactly those five and nothing else -- every one of them
dated BEFORE 2026-09-25, the day the call site was first wired. The detector
was right and the call site was simply younger than the evidence; only the
missing "I ran" row made that unreadable.

So one `checked` row per PM session, carrying how many targets were
checkable, and a recorded `error` row plus a traceback when the check raises.
Zero `mismatch` rows beside a run of `checked` rows now means agreement, and
no rows at all means the path did not execute -- a distinction the stream
could not previously make.
"""

from __future__ import annotations

import logging
from typing import Any

from src.pipeline_candidate_records import _record_pipeline_event

logger = logging.getLogger(__name__)

#: The pipeline-event stage every row here is filed under. One name for the
#: hit, the clean pass and the failure, so a reader counts runs and hits off
#: the same `stage` rather than guessing which label a site chose.
SIZING_NARRATIVE_STAGE = "sizing_narrative_check"


def _checkable_target_count(decision: Any) -> int:
    """Targets carrying BOTH a symbol and an emitted `risk_allocation_pct`.

    The same pairing the detector itself requires, so the recorded
    denominator is the number of pairs it actually had to judge rather than
    the raw target count.
    """
    total = 0
    for target in list(getattr(decision, "targets", None) or []):
        if getattr(target, "symbol", None) is None:
            continue
        if getattr(target, "risk_allocation_pct", None) is None:
            continue
        total += 1
    return total


def audit_sizing_narrative(pipeline: Any, ctx: Any, decision: Any) -> None:
    """Run the sizing-narrative check and record that it ran.

    Records one `mismatch` row per finding (unchanged in shape from the
    previous call site) and exactly one `checked` row per session. A failure
    inside the detector is logged with its traceback and recorded as an
    `error` row instead of disappearing.
    """
    chain = getattr(decision, "reasoning_chain", None)
    prose = getattr(chain, "sizing_logic", "") if chain is not None else ""
    checkable = _checkable_target_count(decision)

    try:
        from src.risk_narrative_check import check_sizing_narrative

        mismatches = list(check_sizing_narrative(decision))
    except Exception as exc:  # pragma: no cover - exercised by test double
        logger.exception("sizing_narrative_check raised")
        _record_pipeline_event(
            pipeline, ctx, None, SIZING_NARRATIVE_STAGE, "error",
            type(exc).__name__,
            checkable_targets=checkable, prose_chars=len(prose or ""),
        )
        return

    for mismatch in mismatches:
        _record_pipeline_event(
            pipeline, ctx, mismatch.symbol,
            SIZING_NARRATIVE_STAGE, "mismatch", mismatch.detail,
            prose_pct=mismatch.prose_pct, field_pct=mismatch.field_pct,
        )

    _record_pipeline_event(
        pipeline, ctx, None, SIZING_NARRATIVE_STAGE, "checked",
        f"{len(mismatches)} mismatch(es) over {checkable} checkable target(s)",
        checkable_targets=checkable, prose_chars=len(prose or ""),
        mismatches=len(mismatches),
    )
