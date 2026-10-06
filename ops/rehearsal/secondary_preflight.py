"""Read-only gate before a capture from the isolated secondary Paper account.

This is a preflight, not a capture launcher.  It makes only three Alpaca reads:
account, positions, and open orders.  The caller must hold the capture/session
lease for the entire subsequent run; a point-in-time check cannot prevent a
different process from starting after it returns.
"""

from __future__ import annotations

import os
import pwd
import subprocess
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

from src.credential_placeholder import looks_like_placeholder
from src.credentials import load_systemd_credentials
from src.session_identity import credentials_directory_var, session_identity

PAPER_URL = "https://paper-api.alpaca.markets"
TRADING_UNITS = frozenset({
    "quant-agent-morning.service", "quant-agent-midday.service",
    "quant-agent-close.service", "quant-agent-evening.service",
    "quant-agent-intra_check.service", "quant-agent-intra_safety.service",
})


class SecondaryPreflightError(RuntimeError):
    """A capture cannot prove it is confined to the secondary Paper account."""


@dataclass(frozen=True)
class CapturePreflight:
    primary_account_number: str
    scratch_root: Path
    capture_db: Path
    production_root: Path
    production_session_lock: Path
    min_available_memory_mib: int
    max_load_per_cpu: float
    max_capture_seconds: int
    max_capture_bytes: int
    expected_position_count: int = 0
    expected_open_order_count: int = 0


@contextmanager
def hold_capture_session_lock(lock: Path):
    """Use the desk's existing cross-session lock for the whole capture.

    The normal morning wrapper recognises the same owner-file shape.  Never
    remove a lock whose ownership changed while this process was running.
    The separate intra-check lane remains exempt from this lock, so the
    capture launcher must also require its timer disabled.
    """
    lock = Path(lock)
    if not lock.parent.is_dir():
        raise SecondaryPreflightError("production session-lock parent is unavailable")
    try:
        lock.mkdir(mode=0o700)
    except FileExistsError:
        raise SecondaryPreflightError("a production trading session lock exists") from None
    owner = lock / "owner"
    token = (
        f"secondary_capture {datetime.now(timezone.utc).date().isoformat()} "
        f"{int(time.time())} {os.getpid()} {uuid.uuid4().hex}\n"
    )
    try:
        owner.write_text(token)
        try:
            yield
        finally:
            if owner.read_text() != token:
                raise SecondaryPreflightError("capture session lock changed owner")
            owner.unlink()
    finally:
        try:
            lock.rmdir()
        except OSError:
            # Leave a foreign/nonempty lock intact; never delete another run.
            pass


def _under(path: Path, parent: Path) -> bool:
    return path == parent or parent in path.parents


def _check_local(spec: CapturePreflight, paper: bool, base_url: str,
                 resolved_client_url: str, *, memory_mib: int,
                 load_per_cpu: float) -> None:
    if paper is not True or base_url.rstrip("/") != PAPER_URL:
        raise SecondaryPreflightError("configured broker is not Alpaca Paper")
    resolved = urlparse(str(resolved_client_url).rstrip("/"))
    if resolved.scheme != "https" or resolved.netloc != "paper-api.alpaca.markets" \
            or resolved.path not in ("", "/"):
        raise SecondaryPreflightError("resolved SDK endpoint is not Alpaca Paper")
    primary = spec.primary_account_number.strip()
    if not primary:
        raise SecondaryPreflightError("expected primary account identity is required")
    root, db, production = (p.resolve() for p in (
        spec.scratch_root, spec.capture_db, spec.production_root))
    if _under(root, production) or _under(production, root) or not _under(db, root):
        raise SecondaryPreflightError("capture database and scratch paths are not isolated")
    if db.exists() and not db.is_file():
        raise SecondaryPreflightError("capture database path is not a file")
    if spec.production_session_lock.is_dir():
        raise SecondaryPreflightError("a production trading session is active")
    if min(spec.min_available_memory_mib, spec.max_capture_seconds,
           spec.max_capture_bytes) <= 0 or spec.max_load_per_cpu <= 0:
        raise SecondaryPreflightError("positive capture and resource bounds are required")
    if memory_mib < spec.min_available_memory_mib or load_per_cpu > spec.max_load_per_cpu:
        raise SecondaryPreflightError("server resource headroom is below capture bounds")
    if spec.expected_position_count < 0 or spec.expected_open_order_count < 0:
        raise SecondaryPreflightError("expected broker baseline cannot be negative")


def _memory_and_load() -> tuple[int, float]:
    with open("/proc/meminfo", encoding="ascii") as source:
        available = next((line for line in source if line.startswith("MemAvailable:")), None)
    if available is None:
        raise SecondaryPreflightError("available server memory cannot be read")
    cpus = os.cpu_count()
    if not cpus:
        raise SecondaryPreflightError("server CPU count cannot be read")
    return int(available.split()[1]) // 1024, os.getloadavg()[0] / cpus


def _running_trading_units() -> bool:
    """Inspect current qamc user services; inability to inspect is a refusal."""
    uid = pwd.getpwnam("qamc").pw_uid
    command = ["systemctl", "--user", "list-units", "--type=service",
               "--state=running", "--no-legend", "--no-pager",
               "quant-agent-*.service"]
    if os.geteuid() == uid:
        command = ["env", f"XDG_RUNTIME_DIR=/run/user/{uid}", *command]
    else:
        command = ["sudo", "-n", "-u", "qamc", "env",
                   f"XDG_RUNTIME_DIR=/run/user/{uid}", *command]
    result = subprocess.run(
        command,
        capture_output=True, text=True, timeout=10, check=False,
    )
    if result.returncode:
        raise SecondaryPreflightError("production session status cannot be read")
    return any(line.split() and line.split()[0] in TRADING_UNITS
               for line in result.stdout.splitlines())


def _secondary_credentials(env: dict[str, str]) -> tuple[str, str, str]:
    """Require the existing rehearsal identity and systemd file hand-off."""
    if session_identity(env) != "rehearsal":
        raise SecondaryPreflightError("capture session identity is not rehearsal")
    directory_name = credentials_directory_var("rehearsal")
    raw = env.get(directory_name, "").strip()
    if not raw or raw != env.get("CREDENTIALS_DIRECTORY", "").strip():
        raise SecondaryPreflightError("rehearsal systemd credential directory was not delivered")
    directory = Path(raw).resolve()
    if not directory.is_dir():
        raise SecondaryPreflightError("rehearsal credential directory is unavailable")
    try:
        delivered = load_systemd_credentials(env)
    except Exception:
        raise SecondaryPreflightError("rehearsal credentials cannot be read") from None
    try:
        account_number = (directory / "account_number").read_text().strip()
    except OSError:
        raise SecondaryPreflightError("rehearsal account_number cannot be read") from None
    key = delivered.get("ALPACA_API_KEY", "")
    secret = delivered.get("ALPACA_SECRET_KEY", "")
    if any(not value or looks_like_placeholder(value)
           for value in (key, secret, account_number)):
        raise SecondaryPreflightError("rehearsal credentials are missing or placeholders")
    return key, secret, account_number


def load_primary_account_assertion(env: dict[str, str] | None = None) -> str:
    """Read only the primary account *number* delivered to the capture unit.

    The capture process never receives primary API credentials or contacts the
    primary broker account.  This independent assertion is used solely to
    refuse a secondary key that resolves to the primary account.
    """
    environment = dict(os.environ if env is None else env)
    if session_identity(environment) != "rehearsal":
        raise SecondaryPreflightError("capture session identity is not rehearsal")
    raw = environment.get(credentials_directory_var("rehearsal"), "").strip()
    if not raw or raw != environment.get("CREDENTIALS_DIRECTORY", "").strip():
        raise SecondaryPreflightError("rehearsal systemd credential directory was not delivered")
    try:
        value = (Path(raw) / "primary_account_number").read_text().strip()
    except OSError:
        raise SecondaryPreflightError("primary account identity assertion is unavailable") from None
    if not value or looks_like_placeholder(value):
        raise SecondaryPreflightError("primary account identity assertion is invalid")
    return value


def check_secondary_capture(
    spec: CapturePreflight, *, paper: bool, base_url: str,
    env: dict[str, str] | None = None,
    client_factory: Callable[[str, str], object] | None = None,
    resource_reader: Callable[[], tuple[int, float]] = _memory_and_load,
    production_session_active: Callable[[], bool] = _running_trading_units,
) -> tuple[object, tuple[str, str]]:
    """Return the verified SDK client and key pair for the caller's capture.

    No account identifier or credential is included in exceptions or output.
    The returned pair must stay in process memory, never a capture artifact.
    """
    if not paper or base_url.rstrip("/") != PAPER_URL:
        raise SecondaryPreflightError("configured broker is not Alpaca Paper")
    try:
        memory_mib, load_per_cpu = resource_reader()
        if production_session_active():
            raise SecondaryPreflightError("a production trading service is running")
        # First reject bad paths/resource headroom before reading a credential.
        _check_local(spec, paper, base_url, PAPER_URL,
                     memory_mib=memory_mib, load_per_cpu=load_per_cpu)
        if client_factory is None:
            from alpaca.trading.client import TradingClient
            client_factory = lambda key, secret: TradingClient(key, secret, paper=True)
        key, secret, expected_secondary = _secondary_credentials(
            dict(os.environ if env is None else env))
        if expected_secondary == spec.primary_account_number.strip():
            raise SecondaryPreflightError("secondary and primary account identities match")
        credentials = (key, secret)
        client = client_factory(*credentials)
        endpoint = getattr(client, "_base_url", "")
        _check_local(spec, paper, base_url, str(getattr(endpoint, "value", endpoint)),
                     memory_mib=memory_mib, load_per_cpu=load_per_cpu)
        account = client.get_account()
        if (str(getattr(account, "account_number", "")) != expected_secondary or
                str(getattr(account, "account_number", "")) ==
                spec.primary_account_number.strip()):
            raise SecondaryPreflightError("broker account is not the expected secondary account")
        status = getattr(account, "status", None)
        if str(getattr(status, "value", status)).upper() != "ACTIVE":
            raise SecondaryPreflightError("secondary broker account is not active")
        from alpaca.trading.enums import QueryOrderStatus
        from alpaca.trading.requests import GetOrdersRequest
        positions = client.get_all_positions()
        orders = client.get_orders(filter=GetOrdersRequest(status=QueryOrderStatus.OPEN))
        if not isinstance(positions, list) or len(positions) != spec.expected_position_count:
            raise SecondaryPreflightError("secondary position baseline does not match")
        if not isinstance(orders, list) or len(orders) != spec.expected_open_order_count:
            raise SecondaryPreflightError("secondary open-order baseline does not match")
    except SecondaryPreflightError:
        raise
    except Exception:
        # SDK exceptions can echo request headers or account data. Never attach
        # the original exception to a reportable preflight failure.
        raise SecondaryPreflightError("read-only secondary broker preflight failed") from None
    return client, credentials
