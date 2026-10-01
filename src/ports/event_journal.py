"""EventJournal — the port through which the desk records what it did.

Conversion step 6 (two methods) plus step 9's ``insert_agent_log``. The first two are the two
module-level helpers (``_persist_evidence`` and ``_record_pipeline_event`` in
``src/pipeline_stages.py``) that every stage and service reaches for today.
Later steps hand a ``journal: EventJournal`` to each service's constructor in
place of ``self.db`` plus the module helper.

Contract shared by every implementation:

- Both methods are best-effort and NEVER raise. A failure to record is a
  forensic-display gap, never a reason to interrupt or relax a trading
  decision (``.claude/rules/trading-core.md``).
- ``record_pipeline_event`` is one typed lifecycle fact: it is persisted as
  evidence of kind ``pipeline_event`` from agent ``pipeline``, scoped to the
  symbol when one is given and to the run otherwise, with a JSON payload of
  ``{"stage", "outcome", "reason", **details}`` serialised with sorted keys.

No logic lives here; the L3 implementation is ``src.storage.event_journal``
and the test fake is ``tests/fake_event_journal.py``.
"""
from __future__ import annotations

from abc import ABC, abstractmethod


class EventJournal(ABC):
    """Append-only record of evidence and pipeline lifecycle events."""

    @abstractmethod
    def persist_evidence(
        self, *, run_id: str, agent_name: str, kind: str, scope: str,
        evidence_json: str, symbol: str | None = None,
        decision_id: str | None = None,
    ) -> None:
        """Record one already-serialised structured-evidence row. Never raises."""

    @abstractmethod
    def record_pipeline_event(
        self, *, run_id: str, decision_id: str | None, symbol: str | None,
        stage: str, outcome: str, reason: str = "", **details,
    ) -> None:
        """Record one typed lifecycle fact for this run (and symbol). Never raises."""

    @abstractmethod
    def insert_agent_log(
        self, *, agent_name: str, run_id: str, input_summary: str,
        output_summary: str, full_response: str, model: str, tokens_used: int,
        **fields,
    ) -> None:
        """Record one paid model call (``agent_logs``). Unlike the two methods
        above this MAY raise: its one caller wraps it and logs its own failure."""
