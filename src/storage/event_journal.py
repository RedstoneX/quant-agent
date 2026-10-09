"""SQLite-backed EventJournal — the L3 implementation of the journal port.

Conversion step 6. The bodies here are the former ``_persist_evidence`` and
``_record_pipeline_event`` of ``src/pipeline_stages.py``, moved verbatim; those
two names remain in ``pipeline_stages`` as thin shims over this class for the
~15 existing call sites, which are rewritten one service at a time in later
steps. Rows written through either route are byte-identical (pinned by
``tests/test_event_journal_port.py``).
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from src.ports.event_journal import EventJournal

if TYPE_CHECKING:
    from src.storage.db import Database

logger = logging.getLogger(__name__)

PIPELINE_AGENT_NAME = "pipeline"
PIPELINE_EVENT_KIND = "pipeline_event"


def pipeline_event_fields(
    *,
    run_id: str,
    decision_id: str | None,
    symbol: str | None,
    stage: str,
    outcome: str,
    reason: str,
    details: dict,
) -> dict:
    """The exact ``persist_evidence`` keyword arguments one pipeline event becomes.

    Kept as a pure function so the shim in ``pipeline_stages`` and the adapter
    method below cannot drift apart.
    """
    payload = {"stage": stage, "outcome": outcome, "reason": reason, **details}
    return {
        "run_id": run_id,
        "agent_name": PIPELINE_AGENT_NAME,
        "kind": PIPELINE_EVENT_KIND,
        "scope": "symbol" if symbol else "run",
        "symbol": symbol,
        "decision_id": decision_id,
        "evidence_json": json.dumps(payload, sort_keys=True),
    }


class DatabaseEventJournal(EventJournal):
    """Writes evidence rows to a ``Database`` (``insert_specialist_evidence``)."""

    def __init__(self, db: "Database") -> None:
        self._db = db

    def persist_evidence(
        self,
        *,
        run_id: str,
        agent_name: str,
        kind: str,
        scope: str,
        evidence_json: str,
        symbol: str | None = None,
        decision_id: str | None = None,
    ) -> None:
        """Best-effort Stage 4 structured-evidence write — NEVER raises.

        A failure here (disk full, lock contention, whatever) is a
        forensic-display gap, not a reason to mark research/decision data
        degraded or interrupt the pipeline — see
        docs/architecture/MISSION_CONTROL_API.md and
        .claude/rules/trading-core.md's "Logging/forensic persistence failure
        must never relax a deterministic block" rule.
        """
        try:
            self._db.insert_specialist_evidence(
                run_id=run_id,
                agent_name=agent_name,
                kind=kind,
                scope=scope,
                evidence_json=evidence_json,
                symbol=symbol,
                decision_id=decision_id,
            )
        except Exception as e:
            logger.warning(
                "Failed to persist Stage 4 specialist evidence (agent=%s kind=%s scope=%s symbol=%s): %s",
                agent_name,
                kind,
                scope,
                symbol,
                e,
            )

    def record_pipeline_event(
        self,
        *,
        run_id: str,
        decision_id: str | None,
        symbol: str | None,
        stage: str,
        outcome: str,
        reason: str = "",
        **details,
    ) -> None:
        """Append one typed lifecycle fact to the existing evidence stream."""
        self.persist_evidence(
            **pipeline_event_fields(
                run_id=run_id,
                decision_id=decision_id,
                symbol=symbol,
                stage=stage,
                outcome=outcome,
                reason=reason,
                details=details,
            )
        )
