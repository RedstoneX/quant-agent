"""In-memory EventJournal for tests — inject it where a service takes a journal.

Records exactly the keyword arguments a real journal would hand to storage,
so a test can assert on the rows without a database. ``fail`` makes every
write raise inside storage, to prove a caller survives a journal failure.
"""

from __future__ import annotations

from src.ports.event_journal import EventJournal
from src.storage.event_journal import pipeline_event_fields


class InMemoryEventJournal(EventJournal):
    def __init__(self, *, fail: bool = False) -> None:
        self.rows: list[dict] = []
        self.failures: list[dict] = []
        self.fail = fail

    def persist_evidence(
        self,
        *,
        run_id,
        agent_name,
        kind,
        scope,
        evidence_json,
        symbol=None,
        decision_id=None,
    ) -> None:
        row = {
            "run_id": run_id,
            "agent_name": agent_name,
            "kind": kind,
            "scope": scope,
            "evidence_json": evidence_json,
            "symbol": symbol,
            "decision_id": decision_id,
        }
        (self.failures if self.fail else self.rows).append(row)

    def record_pipeline_event(
        self,
        *,
        run_id,
        decision_id,
        symbol,
        stage,
        outcome,
        reason="",
        **details,
    ) -> None:
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

    def events(self, *, kind: str | None = None) -> list[dict]:
        return [r for r in self.rows if kind is None or r["kind"] == kind]
