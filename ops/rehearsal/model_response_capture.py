"""Private transfer of a real session's model records into its replay snapshot.

This is not public-bundle promotion. ``full_response`` and ``input_message``
contain raw model and decision content and must remain in private scratch.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from pathlib import Path


class ModelResponseCaptureError(RuntimeError):
    """The model records cannot be tied exactly to the captured session."""


def _private_file(path: Path, *, exists: bool) -> Path:
    path = Path(path).absolute()
    parent = path.parent
    if parent.is_symlink() or not parent.is_dir() or parent.stat().st_mode & 0o077:
        raise ModelResponseCaptureError("model evidence directory is not private")
    if exists:
        if path.is_symlink() or not path.is_file() or path.stat().st_mode & 0o077:
            raise ModelResponseCaptureError("model evidence file is not private")
    elif path.exists() or path.is_symlink():
        raise ModelResponseCaptureError("model evidence destination already exists")
    if Path("/home/qamc/quant-agent") in path.resolve().parents:
        raise ModelResponseCaptureError("model evidence cannot be handled in production")
    return path


def _connect(path: Path, *, writable: bool = False) -> sqlite3.Connection:
    mode = "rw" if writable else "ro"
    connection = sqlite3.connect(f"{path.as_uri()}?mode={mode}", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def _columns(connection: sqlite3.Connection) -> list[str]:
    rows = connection.execute("PRAGMA table_info(agent_logs)").fetchall()
    columns = [row[1] for row in rows]
    if not {"id", "run_id", "agent_name", "input_message", "full_response"} <= set(columns):
        raise ModelResponseCaptureError("agent_logs schema lacks replay fields")
    return columns


def _rows(connection: sqlite3.Connection, columns: list[str]) -> list[list]:
    # Names originate only from SQLite's own table metadata.
    return [list(row) for row in connection.execute(
        f'SELECT {", ".join(chr(34) + c + chr(34) for c in columns)} '
        'FROM agent_logs ORDER BY id'
    )]


def _digest(rows: list[list]) -> str:
    try:
        body = json.dumps(rows, ensure_ascii=False, separators=(",", ":"),
                          allow_nan=False).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ModelResponseCaptureError("agent_logs contains unsupported values") from exc
    return hashlib.sha256(body).hexdigest()


def export_model_responses(before_db: Path, after_db: Path, run_id: str,
                           bundle_path: Path) -> int:
    """Export exactly the new rows of ``run_id``; return their count.

    The before and after databases are the actual private session snapshots.
    No row is synthesized and no missing response is silently omitted.
    """
    before = _private_file(before_db, exists=True)
    after = _private_file(after_db, exists=True)
    bundle = _private_file(bundle_path, exists=False)
    if len({before.resolve(), after.resolve(), bundle.resolve()}) != 3:
        raise ModelResponseCaptureError("model evidence paths must be distinct")
    if not isinstance(run_id, str) or not run_id.startswith("run-") or not run_id.strip():
        raise ModelResponseCaptureError("an explicit morning run_id is required")
    with _connect(before) as base, _connect(after) as capture:
        columns = _columns(base)
        if _columns(capture) != columns:
            raise ModelResponseCaptureError("agent_logs schema changed during capture")
        original = _rows(base, columns)
        observed = _rows(capture, columns)
    if observed[:len(original)] != original:
        raise ModelResponseCaptureError("pre-session agent_logs is not the captured prefix")
    added = observed[len(original):]
    run_index = columns.index("run_id")
    prompt_index = columns.index("input_message")
    response_index = columns.index("full_response")
    if not added or any(row[run_index] != run_id for row in added):
        raise ModelResponseCaptureError("missing or ambiguous captured morning run")
    if any(not row[prompt_index] or row[response_index] is None for row in added):
        raise ModelResponseCaptureError("captured run has an unreplayable model row")
    payload = {"schema": 1, "run_id": run_id, "columns": columns,
               "before_path": str(before.resolve()),
               "before_digest": _digest(original),
               "rows_digest": _digest(added), "rows": added}
    encoded = json.dumps(payload, ensure_ascii=False, allow_nan=False,
                         separators=(",", ":")).encode("utf-8")
    descriptor = os.open(bundle, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(encoded)
            output.flush()
            os.fsync(output.fileno())
    except Exception:
        bundle.unlink(missing_ok=True)
        raise
    return len(added)


def import_model_responses(bundle_path: Path, replay_db: Path) -> int:
    """Insert captured rows into a private copy of ``before_db`` atomically."""
    bundle = _private_file(bundle_path, exists=True)
    target = _private_file(replay_db, exists=True)
    if bundle.resolve() == target.resolve():
        raise ModelResponseCaptureError("bundle and replay database must differ")
    try:
        payload = json.loads(bundle.read_text(encoding="utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ModelResponseCaptureError("malformed private model bundle") from exc
    if not isinstance(payload, dict) or payload.get("schema") != 1:
        raise ModelResponseCaptureError("unsupported private model bundle")
    run_id = payload.get("run_id")
    rows = payload.get("rows")
    columns = payload.get("columns")
    if not isinstance(run_id, str) or not run_id.startswith("run-") or \
            not isinstance(rows, list) or not rows or not isinstance(columns, list):
        raise ModelResponseCaptureError("incomplete private model bundle")
    if str(target.resolve()) == payload.get("before_path"):
        raise ModelResponseCaptureError("original pre-session snapshot cannot be modified")
    if _digest(rows) != payload.get("rows_digest"):
        raise ModelResponseCaptureError("private model bundle rows changed")
    with _connect(target, writable=True) as connection:
        connection.execute("BEGIN IMMEDIATE")
        try:
            if _columns(connection) != columns or \
                    _digest(_rows(connection, columns)) != payload.get("before_digest"):
                raise ModelResponseCaptureError("replay database differs from pre-session snapshot")
            run_index = columns.index("run_id")
            prompt_index = columns.index("input_message")
            response_index = columns.index("full_response")
            if any(not isinstance(row, list) or len(row) != len(columns) or
                   row[run_index] != run_id or not row[prompt_index] or
                   row[response_index] is None for row in rows):
                raise ModelResponseCaptureError("private model bundle has unreplayable or mixed run rows")
            names = ", ".join('"' + column + '"' for column in columns)
            marks = ", ".join("?" for _ in columns)
            connection.executemany(
                f"INSERT INTO agent_logs ({names}) VALUES ({marks})", rows
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
    return len(rows)
