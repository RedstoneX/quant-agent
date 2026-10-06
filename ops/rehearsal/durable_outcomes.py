"""Compare a captured session's durable decisions with its offline replay.

Both sides are read from private SQLite. The pre-session database is the
baseline: it distinguishes this run's inserts, deletes, and updates from
records already present. This oracle never writes a trading database.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path


class DurableOutcomeMismatch(RuntimeError):
    """Captured and replayed durable outcomes differ or cannot be linked."""


# The first four tables contain the decision and execution trail. The last
# tables cover mutable protection state and the append-only restore WAL audit.
_TABLES = (
    "specialist_evidence", "trade_refusals", "trades", "order_attempts",
    "reconciliation_runs", "positions", "pending_protection_restores",
    "protection_restore_wal_audit", "pending_repegs", "pending_stop_amends",
)
_CLOCK_COLUMNS = frozenset({
    "timestamp", "recorded_at", "ran_at", "created_at", "updated_at",
    "fill_reconciled_at",
})
_JSON_COLUMNS = frozenset({"evidence_json", "specs_json"})
_BROKER_ID_FIELDS = frozenset({
    "broker_order_id", "client_order_id", "order_id", "old_order_id",
    "new_order_id", "sell_order_id", "replaced_by", "replaces",
    "cancelled_ids", "trade_id", "asset_id", "account_id",
    "old_id", "new_id",
})


def _read(path: Path) -> dict[str, dict]:
    uri = f"{Path(path).resolve().as_uri()}?mode=ro"
    try:
        connection = sqlite3.connect(uri, uri=True)
        connection.row_factory = sqlite3.Row
        try:
            tables = {}
            for name in _TABLES:
                info = connection.execute(f"PRAGMA table_info({name})").fetchall()
                columns = tuple(row[1] for row in info)
                if not columns:
                    raise DurableOutcomeMismatch(f"missing outcome table: {name}")
                key = "id" if "id" in columns else "symbol" if name == "positions" else "row_id"
                if key not in columns:
                    raise DurableOutcomeMismatch(f"outcome table has no stable key: {name}")
                rows = connection.execute(f"SELECT * FROM {name} ORDER BY {key}").fetchall()
                tables[name] = {row[key]: dict(row) for row in rows}
            return tables
        finally:
            connection.close()
    except sqlite3.Error as exc:
        raise DurableOutcomeMismatch("cannot read a complete outcome database") from exc


def _delta(before: dict[str, dict], after: dict[str, dict], run_id: str):
    result = {}
    for table in _TABLES:
        old, new = before[table], after[table]
        inserted = [row for key, row in new.items() if key not in old]
        changed = [row for key, row in new.items()
                   if key in old and row != old[key]]
        deleted = [old[key] for key in old if key not in new]
        # The isolated capture/replay database has one active session. Keep
        # null-run cancel and reconciliation rows; they cannot be attributed
        # by run_id alone, but the before/after delta attributes their writes.
        for row in inserted:
            value = row.get("run_id")
            if value not in (None, run_id, "unattributed"):
                raise DurableOutcomeMismatch(f"foreign run added {table} evidence")
        result[table] = {"inserted": inserted, "changed": changed,
                         "deleted": deleted}
    return result


class _IdentityMap:
    def __init__(self, captured_run: str, replay_run: str):
        self.runs = (captured_run, replay_run)
        self.forward: dict[tuple[str, str], str] = {}
        self.reverse: dict[tuple[str, str], str] = {}
        self.pm_decisions: set[str] = set()

    def _pair(self, kind: str, left, right, *, anchor: bool = False):
        if left is None or right is None:
            if left is right:
                return
            raise DurableOutcomeMismatch(f"{kind} NULL linkage differs")
        if not isinstance(left, (str, int)) or not isinstance(right, (str, int)):
            raise DurableOutcomeMismatch(f"{kind} linkage is malformed")
        left, right = str(left), str(right)
        if not left or not right:
            if left == right:
                return
            raise DurableOutcomeMismatch(f"{kind} empty linkage differs")
        a, b = (kind, left), (kind, right)
        if self.forward.get(a, right) != right or self.reverse.get(b, left) != left:
            raise DurableOutcomeMismatch(f"{kind} linkage is not one-to-one")
        self.forward[a], self.reverse[b] = right, left
        if anchor and kind == "decision_id":
            self.pm_decisions.add(left)

    def compare(self, left, right, *, field: str, table: str, kind: str = ""):
        if field == "run_id":
            if left == self.runs[0] and right == self.runs[1]:
                return
            if left == right and left not in self.runs:
                return
            raise DurableOutcomeMismatch(f"{table}.run_id differs")
        if field == "decision_id":
            self._pair(field, left, right,
                       anchor=table == "specialist_evidence" and kind == "reasoning")
            return
        if field == "position_id":
            self._pair(field, left, right)
            return
        if field in _BROKER_ID_FIELDS:
            if isinstance(left, list) and isinstance(right, list):
                if len(left) != len(right):
                    raise DurableOutcomeMismatch(f"{table}.{field} length differs")
                for a, b in zip(left, right):
                    self._pair("broker_id", a, b)
            else:
                self._pair("broker_id", left, right)
            return
        if isinstance(left, str) and isinstance(right, str):
            left = left.replace(self.runs[0], "<run>")
            right = right.replace(self.runs[1], "<run>")
        if isinstance(left, dict) and isinstance(right, dict):
            if left.keys() != right.keys():
                raise DurableOutcomeMismatch(f"{table}.{field} fields differ")
            for key in left:
                self.compare(left[key], right[key], field=key, table=table, kind=kind)
            return
        if isinstance(left, list) and isinstance(right, list):
            if len(left) != len(right):
                raise DurableOutcomeMismatch(f"{table}.{field} length differs")
            for a, b in zip(left, right):
                self.compare(a, b, field=field, table=table, kind=kind)
            return
        if left != right or type(left) is not type(right):
            raise DurableOutcomeMismatch(f"{table}.{field} differs")


def _compare_rows(table: str, section: str, captured: list[dict],
                  replayed: list[dict], identities: _IdentityMap):
    if len(captured) != len(replayed):
        raise DurableOutcomeMismatch(f"{table} {section} row count differs")
    for left, right in zip(captured, replayed):
        if left.keys() != right.keys():
            raise DurableOutcomeMismatch(f"{table} {section} schema differs")
        kind = left.get("kind", "")
        for field in left:
            if field == "id" or field in _CLOCK_COLUMNS:
                continue
            a, b = left[field], right[field]
            if field in _JSON_COLUMNS:
                try:
                    a, b = json.loads(a), json.loads(b)
                except (TypeError, ValueError) as exc:
                    raise DurableOutcomeMismatch(f"malformed {table}.{field} JSON") from exc
            identities.compare(a, b, field=field, table=table, kind=kind)


def compare_durable_outcomes(before_db: Path, captured_db: Path, replay_db: Path,
                             *, captured_run: str, replay_run: str) -> None:
    """Raise unless the replay exactly reproduces run-scoped durable outcomes."""
    if not captured_run or not replay_run or captured_run == replay_run:
        raise DurableOutcomeMismatch("distinct explicit run IDs are required")
    baseline = _read(before_db)
    captured = _delta(baseline, _read(captured_db), captured_run)
    replayed = _delta(baseline, _read(replay_db), replay_run)
    identities = _IdentityMap(captured_run, replay_run)
    # PM reasoning is the anchor for a decision_id. An absent PM means this
    # run never proved the decision seam that item 233 requires.
    pm = [row for row in captured["specialist_evidence"]["inserted"]
          if row.get("agent_name") == "portfolio_manager" and row.get("kind") == "reasoning"]
    if not pm:
        raise DurableOutcomeMismatch("captured run has no Portfolio Manager decision")
    for table in _TABLES:
        for section in ("inserted", "changed", "deleted"):
            _compare_rows(table, section, captured[table][section],
                          replayed[table][section], identities)
    for table in ("specialist_evidence", "trades"):
        for section in ("inserted", "changed"):
            for row in captured[table][section]:
                decision = row.get("decision_id")
                if decision is not None and decision not in identities.pm_decisions:
                    raise DurableOutcomeMismatch("decision link has no PM reasoning anchor")
