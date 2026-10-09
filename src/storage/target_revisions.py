"""Adjudicated take-profit revision records.

A STANDALONE store, not a mixin: built from plain collaborators and
reaching back into nothing. Separate from `src.storage.break_records`
because a revision RECORD is the outcome, while a break record is the
cross-day evidence that may or may not produce one.
"""

import json
import sqlite3
import threading

from src.storage.locked_write import locked_write


class TargetRevisionRecords:
    """One durable `specialist_evidence` row per adjudicated target flag."""

    def __init__(self, *, conn: sqlite3.Connection, lock: threading.Lock):
        self.conn = conn
        self._lock = lock

    def _insert_evidence(
        self,
        *,
        run_id: str,
        agent_name: str,
        kind: str,
        scope: str,
        evidence_json: str,
        symbol: str | None = None,
    ) -> int:
        """Persist one evidence row under the retrying process write lock.

        This is the only WRITE path in this store; every `get_*` below takes
        the plain lock and never commits, so read and write stay telling
        apart at a glance."""

        def _do():
            cur = self.conn.execute(
                "INSERT INTO specialist_evidence "
                "(run_id, decision_id, agent_name, kind, scope, symbol, evidence_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (run_id, None, agent_name, kind, scope, symbol, evidence_json),
            )
            self.conn.commit()
            return cur.lastrowid or 0

        return locked_write(self._lock, _do, label="insert_specialist_evidence")

    #: One durable row per adjudicated flag — revision, refusal or fault
    #: alike. A flag is NEVER a silent no-op and never a blank.
    TARGET_REVISION_KIND = "target_revision"

    def record_target_revision(
        self,
        *,
        run_id: str,
        symbol: str,
        code: str,
        seat: str,
        evidence: str,
        detail: str = "",
        trigger: str = "",
        prior_price: float | None = None,
        new_price: float | None = None,
        basis: str = "",
        level_used: float | None = None,
        evidence_id: int | None = None,
        applied: bool = False,
    ) -> int:
        """File one adjudicated flag. Every outcome gets a row.

        `code` is the machine outcome — a TRIGGER_* code when the target was
        re-derived, otherwise the REFUSAL_*/FAULT_* code naming why it was
        not. `applied` says whether `trades.take_profit` actually moved, so
        the record cannot disagree with the row.
        """
        return self._insert_evidence(
            run_id=run_id,
            agent_name="risk_manager",
            kind=self.TARGET_REVISION_KIND,
            scope="symbol",
            symbol=symbol.upper(),
            evidence_json=json.dumps(
                {
                    "code": str(code),
                    "trigger": str(trigger or ""),
                    "seat": str(seat or ""),
                    "evidence": str(evidence or ""),
                    "evidence_id": evidence_id,
                    "detail": str(detail or ""),
                    "basis": str(basis or ""),
                    "prior_price": prior_price,
                    "new_price": new_price,
                    "level_used": level_used,
                    "applied": bool(applied),
                }
            ),
        )

    def get_target_revisions(self, symbols, *, limit: int = 200) -> dict[str, list[dict]]:
        """`{symbol: [payload, ...]}` newest first, for the cockpit and for
        grading whether revising targets helps.

        `limit` is PER SYMBOL. It used to cap the whole result, so a book
        of names that revise daily could push a quiet name's rows out of
        the window entirely and make its newest row read as missing — and
        the alignment exit reads that newest APPLIED row to date the
        current target, so a missing row silently undated it.
        """
        wanted = [str(s).strip().upper() for s in symbols if str(s).strip()]
        if not wanted:
            return {}
        sql = (
            "SELECT symbol, evidence_json, timestamp, run_id FROM "
            "specialist_evidence WHERE agent_name='risk_manager' AND kind=? "
            "AND symbol = ? ORDER BY timestamp DESC, id DESC LIMIT ?"
        )
        rows: list = []
        with self._lock:
            for sym in dict.fromkeys(wanted):
                rows.extend(
                    self.conn.execute(
                        sql,
                        (self.TARGET_REVISION_KIND, sym, int(limit)),
                    ).fetchall()
                )
        out: dict[str, list[dict]] = {}
        for row in rows:
            row = dict(row)
            try:
                payload = json.loads(row.get("evidence_json") or "{}")
            except (TypeError, ValueError):
                continue
            payload["timestamp"] = row.get("timestamp")
            payload["run_id"] = row.get("run_id")
            out.setdefault(row["symbol"], []).append(payload)
        return out


def build_target_revision_records(
    *,
    conn: sqlite3.Connection,
    lock: threading.Lock,
) -> TargetRevisionRecords:
    """Build the store from its collaborators BY VALUE."""
    return TargetRevisionRecords(conn=conn, lock=lock)
