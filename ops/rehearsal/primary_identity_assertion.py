"""Read-only primary Paper identity for a transient secondary-capture preflight.

Run as qamc in a one-shot systemd unit.  The unit must LoadCredential the
primary pair as ``alpaca_api_key``/``alpaca_secret_key``.  This process
uses only ``TradingClient.get_account``.  It writes only the primary account
number, once, to a private scratch file for a subsequent unit's
``LoadCredential=primary_account_number:...``.  No primary key is copied there.
"""

from __future__ import annotations

import argparse
import os
import pwd
import stat
from contextlib import contextmanager
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

from src.credential_placeholder import looks_like_placeholder
from src.credentials import load_systemd_credentials
from src.session_identity import session_identity

PAPER_HOST = "paper-api.alpaca.markets"
OUTPUT_NAME = "primary_account_number"


class PrimaryIdentityError(RuntimeError):
    """The read-only primary identity assertion could not be proven."""


def _private_scratch(directory: Path, uid: int) -> Path:
    """Refuse a shared/symlinked directory or the production checkout."""
    directory = Path(directory)
    try:
        meta = directory.lstat()
        resolved = directory.resolve(strict=True)
    except OSError:
        raise PrimaryIdentityError("private scratch directory is unavailable") from None
    production = Path("/home/qamc/quant-agent").resolve()
    if (not stat.S_ISDIR(meta.st_mode) or meta.st_uid != uid or
            meta.st_mode & 0o077 or resolved != directory.absolute() or
            resolved == production or
            production in resolved.parents or resolved in production.parents):
        raise PrimaryIdentityError("identity output is not isolated private scratch")
    return directory


def _delivered(env: dict[str, str]) -> tuple[str, str]:
    if session_identity(env) != "desk":
        raise PrimaryIdentityError("primary identity check must use desk identity")
    raw = env.get("CREDENTIALS_DIRECTORY", "").strip()
    if not raw or not Path(raw).is_dir():
        raise PrimaryIdentityError("systemd primary credential directory is unavailable")
    try:
        credentials = load_systemd_credentials(env)
    except Exception:
        raise PrimaryIdentityError("systemd identity inputs could not be read") from None
    key = credentials.get("ALPACA_API_KEY", "")
    secret = credentials.get("ALPACA_SECRET_KEY", "")
    if any(not value or looks_like_placeholder(value)
           for value in (key, secret)):
        raise PrimaryIdentityError("systemd identity inputs are missing or placeholders")
    return key, secret


def _paper_endpoint(client) -> bool:
    endpoint = getattr(client, "_base_url", "")
    url = urlparse(str(getattr(endpoint, "value", endpoint)).rstrip("/"))
    return (url.scheme == "https" and url.netloc == PAPER_HOST and
            url.path in ("", "/") and not url.query and not url.fragment)


@contextmanager
def _direct_alpaca_only():
    """Bypass inherited OneCLI transport for this one read; restore on exit."""
    from ops.rehearsal.direct_credentials import force_direct_alpaca_transport

    names = (
        "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy",
        "all_proxy", "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "NO_PROXY", "no_proxy",
    )
    before = {name: os.environ.get(name) for name in names}
    try:
        force_direct_alpaca_transport()
        yield
    finally:
        for name, value in before.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def write_primary_identity_assertion(
    scratch_dir: Path,
    *,
    env: dict[str, str] | None = None,
    client_factory: Callable[[str, str], object] | None = None,
) -> Path:
    """One Paper account GET; write only its account number, O_EXCL.

    The source credential pair is in a systemd tmpfs, never an argument or an
    environment value.  Failure messages never contain a credential or account
    identifier.  The caller must arrange a transient unit with the correct
    primary source files; this function independently checks the broker's
    Paper endpoint and account identity. The capture unit compares identities.
    """
    if os.geteuid() != pwd.getpwnam("qamc").pw_uid:
        raise PrimaryIdentityError("primary identity check must run as qamc")
    directory = _private_scratch(scratch_dir, os.geteuid())
    target = directory / OUTPUT_NAME
    if target.exists() or target.is_symlink():
        raise PrimaryIdentityError("primary identity assertion already exists")
    environment = dict(os.environ if env is None else env)
    key, secret = _delivered(environment)
    if client_factory is None:
        from alpaca.trading.client import TradingClient

        client_factory = lambda api_key, secret_key: TradingClient(
            api_key, secret_key, paper=True,
        )
    try:
        with _direct_alpaca_only():
            client = client_factory(key, secret)
            if not _paper_endpoint(client):
                raise PrimaryIdentityError("broker client is not pointed at Alpaca Paper")
            account = client.get_account()  # only broker call in this module
    except PrimaryIdentityError:
        raise
    except Exception:
        raise PrimaryIdentityError("primary Paper account read failed") from None
    number = str(getattr(account, "account_number", "") or "").strip()
    if not number.startswith("PA") or not number.isalnum() or \
            looks_like_placeholder(number):
        raise PrimaryIdentityError("broker did not return a valid Paper identity")
    status = getattr(account, "status", None)
    if str(getattr(status, "value", status)).upper() != "ACTIVE":
        raise PrimaryIdentityError("primary Paper account is not active")
    created = False
    try:
        descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        created = True
        with os.fdopen(descriptor, "w", encoding="ascii") as handle:
            handle.write(number + "\n")
            handle.flush()
            os.fsync(handle.fileno())
    except FileExistsError:
        raise PrimaryIdentityError("primary identity assertion already exists") from None
    except Exception:
        if created:
            target.unlink(missing_ok=True)
        raise PrimaryIdentityError("primary identity assertion could not be written") from None
    return target


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scratch-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        write_primary_identity_assertion(args.scratch_dir)
    except PrimaryIdentityError as exc:
        print(f"IDENTITY REFUSED: {exc}")
        return 2
    print("Primary Paper identity assertion written to private scratch")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
