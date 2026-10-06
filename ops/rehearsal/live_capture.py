"""One bounded, real secondary-Paper morning capture; never a replay.

Run only from a private, disposable copy of this checkout with systemd's
rehearsal credential files.  This command makes ordinary provider calls and
may place ordinary Paper orders if the unmodified decision/risk chain chooses
them.  It does not manufacture a candidate or weaken a guard.  Its outputs
stay private until a separate public-safety promotion and exact replay exist.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path

import yaml

from ops.rehearsal.broker_cassette import install_recording_broker_cassette
from ops.rehearsal.direct_credentials import (
    bind_systemd_directory_to_rehearsal_identity,
    load_rehearsal_credentials,
)
from ops.rehearsal.public_bundle import assert_public_safe
from ops.rehearsal.secondary_preflight import (
    CapturePreflight,
    SecondaryPreflightError,
    check_secondary_capture,
    hold_capture_session_lock,
    load_primary_account_assertion,
)

PRODUCTION_ROOT = Path("/home/qamc/quant-agent")
SESSION_LOCK = Path("/home/qamc/.cache/quant-agent/active-session.lock")


class LiveCaptureError(RuntimeError):
    """The capture cannot prove its input or output isolation."""


def _under(path: Path, parent: Path) -> bool:
    return path == parent or parent in path.parents


def assert_disposable_code_root(code_root: Path, production_root: Path) -> Path:
    """Every ``__file__``-anchored QAMC data path must point to scratch."""
    from src.data_paths import repo_root

    actual = repo_root().resolve()
    code_root = Path(code_root).resolve()
    production_root = Path(production_root).resolve()
    if actual != code_root or code_root == production_root or \
            _under(code_root, production_root) or _under(production_root, code_root):
        raise LiveCaptureError("capture code is not isolated from production")
    if not code_root.is_dir() or (code_root.stat().st_mode & 0o077):
        raise LiveCaptureError("capture code root is not private")
    data = code_root / "data"
    if data.is_symlink() or (data.exists() and not data.is_dir()):
        raise LiveCaptureError("capture data path is not a private directory")
    return code_root


def _write_private_json(path: Path, value) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, sort_keys=True, indent=2, default=str)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
    except Exception:
        path.unlink(missing_ok=True)
        raise


def _isolate_broker_environment() -> None:
    """Keep model/OneCLI routing, but never inherit a desk broker key or proxy.

    Alpaca's REST SDK uses ``requests.Session`` and honours NO_PROXY.  Only
    Alpaca hosts bypass the inherited proxy; removing HTTPS_PROXY globally
    would also disconnect model providers that rely on OneCLI.
    """
    for name in ("ALPACA_API_KEY", "ALPACA_SECRET_KEY"):
        os.environ.pop(name, None)
    hosts = {"paper-api.alpaca.markets", "data.alpaca.markets"}
    for name in ("NO_PROXY", "no_proxy"):
        existing = {entry.strip() for entry in os.environ.get(name, "").split(",")
                    if entry.strip()}
        os.environ[name] = ",".join(sorted(existing | hosts))


def _capture_config(code_root: Path, database: Path):
    """Use the real config loader, changing only scratch paths and fill wire."""
    from src.config import load_config

    raw = yaml.safe_load((code_root / "config" / "settings.yaml").read_text())
    raw["storage"]["db_path"] = str(database)
    raw["risk"]["kill_switch_path"] = str(code_root / "data" / "KILL_SWITCH")
    # The websocket is not a cassette input; real bounded REST fill polling
    # remains in force.  This is a supported execution setting, not a
    # paper-only trading rule.
    raw["execution"]["fill_stream_enabled"] = False
    output = code_root / "data" / "capture_settings.yaml"
    with output.open("x", encoding="utf-8") as stream:
        os.chmod(output, 0o600)
        yaml.safe_dump(raw, stream, sort_keys=False)
    config = load_config(output)
    if config.alpaca.paper is not True or Path(config.storage.db_path).resolve() != database:
        raise LiveCaptureError("capture config is not isolated Alpaca Paper")
    return config


def _snapshot_before_session(database: Path, destination: Path) -> None:
    source = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    try:
        source.execute("VACUUM INTO ?", (str(destination),))
    finally:
        source.close()
    os.chmod(destination, 0o600)


@contextmanager
def _scratch_cwd(code_root: Path):
    previous = Path.cwd()
    os.chdir(code_root)
    try:
        yield
    finally:
        os.chdir(previous)


def capture_morning(*, code_root: Path, max_capture_seconds: int,
                    max_capture_bytes: int, min_available_memory_mib: int,
                    max_load_per_cpu: float) -> dict:
    """Capture a natural morning, retaining private evidence for later replay."""
    code_root = assert_disposable_code_root(code_root, PRODUCTION_ROOT)
    if os.environ.get("QAMC_REHEARSAL") != "1":
        raise LiveCaptureError("rehearsal alert suppression is not enabled")
    bind_systemd_directory_to_rehearsal_identity()
    _isolate_broker_environment()
    primary_account_number = load_primary_account_assertion()
    delivered = load_rehearsal_credentials()
    data_dir = code_root / "data"
    data_dir.mkdir(mode=0o700, exist_ok=False)
    database = data_dir / "quant_agent.db"
    spec = CapturePreflight(
        primary_account_number=primary_account_number,
        scratch_root=code_root,
        capture_db=database,
        production_root=PRODUCTION_ROOT,
        production_session_lock=SESSION_LOCK,
        min_available_memory_mib=min_available_memory_mib,
        max_load_per_cpu=max_load_per_cpu,
        max_capture_seconds=max_capture_seconds,
        max_capture_bytes=max_capture_bytes,
    )
    # One read-only identity/book preflight, before constructing a pipeline.
    _client, pair = check_secondary_capture(
        spec, paper=True, base_url="https://paper-api.alpaca.markets"
    )
    if pair != (delivered.api_key, delivered.secret_key):
        raise LiveCaptureError("secondary credential hand-offs disagree")
    # The preflight checks an existing session lock; acquisition immediately
    # after it excludes ordinary morning/midday/close/evening wrappers.  The
    # launcher also requires disabled intra-check timers (they bypass it).
    with hold_capture_session_lock(SESSION_LOCK), _scratch_cwd(code_root):
        config = _capture_config(code_root, database)
        if (config.api_keys.alpaca_key, config.api_keys.alpaca_secret) != pair:
            raise LiveCaptureError("pipeline broker pair differs from preflight")
        from ops.rehearsal.runner import _rehearsal_notifier
        from src.pipeline import TradingPipeline

        with _rehearsal_notifier():
            pipeline = TradingPipeline(config)
            if Path(pipeline._storage_db_path).resolve() != database:
                raise LiveCaptureError("pipeline database escaped scratch")
            cassette = install_recording_broker_cassette(pipeline.broker)
            _snapshot_before_session(database, data_dir / "before.db")
            result = pipeline.run_morning()
        payload = cassette.to_payload()
        assert_public_safe(
            payload,
            secrets=(delivered.api_key, delivered.secret_key),
            account_ids=(delivered.expected_account_number,
                         primary_account_number),
        )
        _write_private_json(data_dir / "broker.json", payload)
        _write_private_json(data_dir / "result.json", result)
        size = sum(path.stat().st_size for path in data_dir.rglob("*") if path.is_file())
        if size > max_capture_bytes:
            raise LiveCaptureError("capture exceeded its declared byte bound")
        return {"broker_calls": len(payload["entries"]), "bytes": size,
                "result_status": str(result.get("status", "unknown"))}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--code-root", type=Path, required=True)
    parser.add_argument("--max-seconds", type=int, required=True)
    parser.add_argument("--max-bytes", type=int, required=True)
    parser.add_argument("--min-free-mib", type=int, required=True)
    parser.add_argument("--max-load-per-cpu", type=float, required=True)
    args = parser.parse_args(argv)
    try:
        summary = capture_morning(
            code_root=args.code_root, max_capture_seconds=args.max_seconds,
            max_capture_bytes=args.max_bytes,
            min_available_memory_mib=args.min_free_mib,
            max_load_per_cpu=args.max_load_per_cpu,
        )
    except (LiveCaptureError, SecondaryPreflightError) as exc:
        print(f"CAPTURE REFUSED: {exc}")
        return 2
    print(json.dumps(summary, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
