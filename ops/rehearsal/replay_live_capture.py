"""Replay one PRIVATE secondary-Paper capture through the real morning path.

The input tree contains raw model responses and account state. Keep it in a
private directory; this command does not promote or print the bundle.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path

from ops.rehearsal.isolation import Sandbox
from ops.rehearsal.model_response_capture import import_model_responses
from ops.rehearsal.runner import run_rehearsal


class CapturedReplayError(RuntimeError):
    """The private capture is incomplete or from another code revision."""


def _private(path: Path) -> Path:
    path = Path(path).resolve(strict=True)
    if not path.is_dir() or path.stat().st_mode & 0o077:
        raise CapturedReplayError("capture directory is not private")
    return path


def _same_code(sha: str) -> None:
    if len(sha) != 40 or any(char not in "0123456789abcdef" for char in sha):
        raise CapturedReplayError("capture has no verified source commit")
    repo = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        capture_output=True, text=True, timeout=10, check=False,
    )
    if result.returncode or result.stdout.strip() != sha:
        raise CapturedReplayError("replay checkout differs from capture commit")
    if subprocess.run(
        ["git", "-C", str(repo), "status", "--porcelain"],
        capture_output=True, text=True, timeout=10, check=False,
    ).stdout.strip():
        raise CapturedReplayError("replay checkout has uncommitted changes")


def replay_private_capture(capture_root: Path, *, production_db: Path | None = None):
    root = _private(capture_root)
    data = _private(root / "data")
    caches = _private(root / "pre_session_data")
    meta = json.loads((data / "capture_meta.private.json").read_text())
    if meta.get("session") != "morning" or not meta.get("run_id"):
        raise CapturedReplayError("capture metadata does not name a morning run")
    _same_code(meta.get("source_sha", ""))
    started_at = datetime.fromisoformat(meta["started_at_et"])
    if started_at.tzinfo is None:
        raise CapturedReplayError("captured session clock has no timezone")
    before = data / "before.db"
    with tempfile.TemporaryDirectory(prefix="qamc-private-replay-") as temporary:
        working = Path(temporary)
        sandbox = Sandbox.prepare(
            before, working / "sandbox", source_data_dir=caches,
        )
        response_db = working / "response_library.db"
        descriptor = os.open(response_db, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(descriptor)
        with sqlite3.connect(f"file:{before}?mode=ro", uri=True) as source, \
                sqlite3.connect(response_db) as target:
            source.backup(target)
        count = import_model_responses(
            data / "model_responses.private.json", response_db,
        )
        if count != meta.get("model_calls"):
            raise CapturedReplayError("captured model call count differs from bundle")
        broker = json.loads((data / "broker.json").read_text())
        providers = json.loads((data / "providers.json").read_text())
        original = json.loads((data / "result.json").read_text())
        report = run_rehearsal(
            sandbox,
            session="morning", now_et=started_at,
            replay_run=meta["run_id"],
            base_settings=root / "config" / "settings.yaml",
            pricing_cache_age_hours=None,
            config_overrides={"execution.fill_stream_enabled": False},
            broker_cassette=broker,
            session_input_payload=providers,
            model_response_db=response_db,
            production_db=production_db,
            sudo_user="qamc" if production_db is not None else None,
        )
        if report.status != original.get("status"):
            raise CapturedReplayError("replayed session status differs from capture")
        return {"status": report.status, "model_calls": count,
                "network_attempts": len(report.network_attempts),
                "broker_calls": len(broker.get("entries", [])),
                "provider_calls": len(providers.get("entries", []))}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("capture_root", type=Path)
    parser.add_argument("--production-db", type=Path,
                        default=Path("/home/qamc/quant-agent/data/quant_agent.db"))
    args = parser.parse_args(argv)
    try:
        result = replay_private_capture(args.capture_root,
                                        production_db=args.production_db)
    except CapturedReplayError as exc:
        print(f"REPLAY REFUSED: {exc}")
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
