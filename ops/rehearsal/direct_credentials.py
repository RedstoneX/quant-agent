"""Direct credential boundary for live checks against the rehearsal account.

The production desk receives Alpaca credentials as systemd credential files.
Live rehearsal checks must exercise that same boundary: no OneCLI agent token,
no placeholder header substitution, and no credential in the process
environment.  The transient launcher loads two files into systemd's private
credential directory; this module validates and returns them without logging
their values.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

from src.credentials import CREDENTIALS_DIRECTORY_ENV, load_systemd_credentials
from src.session_identity import IDENTITY_ENV, credentials_directory_var, session_identity


class RehearsalCredentialError(RuntimeError):
    """The direct rehearsal credential hand-off is absent or inconsistent."""


@dataclass(frozen=True, slots=True, repr=False)
class RehearsalCredentials:
    api_key: str
    secret_key: str


def bind_systemd_directory_to_rehearsal_identity() -> None:
    """Alias systemd's generated directory to the rehearsal identity variable.

    systemd controls the name ``CREDENTIALS_DIRECTORY``. QAMC deliberately
    resolves non-desk identities through a distinct variable, so the transient
    runner declares ``rehearsal`` first and this narrow bridge copies only the
    directory *path* (never a credential) to that derived variable.
    """
    if session_identity() != "rehearsal":
        raise RehearsalCredentialError(
            f"{IDENTITY_ENV} is not rehearsal; refusing to bind broker credentials"
        )
    systemd_directory = os.environ.get(CREDENTIALS_DIRECTORY_ENV, "").strip()
    if not systemd_directory:
        raise RehearsalCredentialError(
            "systemd did not provide a credential directory; refusing a live "
            "rehearsal check"
        )
    rehearsal_variable = credentials_directory_var("rehearsal")
    existing = os.environ.get(rehearsal_variable, "").strip()
    if existing and existing != systemd_directory:
        raise RehearsalCredentialError(
            "rehearsal credential directory conflicts with systemd delivery; "
            "refusing a live rehearsal check"
        )
    os.environ[rehearsal_variable] = systemd_directory


def load_rehearsal_credentials() -> RehearsalCredentials:
    """Read only the systemd-delivered rehearsal key pair."""
    if session_identity() != "rehearsal":
        raise RehearsalCredentialError(
            f"{IDENTITY_ENV} is not rehearsal; refusing a live rehearsal check"
        )
    variable = credentials_directory_var("rehearsal")
    raw_directory = os.environ.get(variable, "").strip()
    if not raw_directory:
        raise RehearsalCredentialError(
            "systemd did not provide a credential directory; refusing a live "
            "rehearsal check"
        )

    delivered = load_systemd_credentials()
    api_key = delivered.get("ALPACA_API_KEY", "").strip()
    secret_key = delivered.get("ALPACA_SECRET_KEY", "").strip()
    if not api_key or not secret_key:
        raise RehearsalCredentialError(
            "systemd did not provide both rehearsal Alpaca credentials; refusing "
            "a live rehearsal check"
        )

    return RehearsalCredentials(api_key, secret_key)


def force_direct_alpaca_transport() -> None:
    """Remove proxy/CA overrides so Alpaca traffic cannot traverse OneCLI."""
    for name in (
        "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
        "http_proxy", "https_proxy", "all_proxy",
        "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE",
    ):
        os.environ.pop(name, None)
    bypass = "paper-api.alpaca.markets,data.alpaca.markets"
    os.environ["NO_PROXY"] = bypass
    os.environ["no_proxy"] = bypass


def assert_expected_paper_account(account, expected: str | None = None) -> None:
    """Fail before writes unless the broker confirms an active Paper account."""
    actual = str(getattr(account, "account_number", "") or "").strip()
    if not actual or (expected is not None and actual != expected):
        raise RehearsalCredentialError(
            "broker account identity was missing or did not match the assertion; "
            "NOTHING was placed"
        )
    if not actual.startswith("PA") or not actual.isalnum():
        raise RehearsalCredentialError(
            "broker account identity did not carry Alpaca's Paper prefix; NOTHING "
            "was placed"
        )
    status = str(getattr(account, "status", "") or "").upper().split(".")[-1]
    if status != "ACTIVE":
        raise RehearsalCredentialError(
            "the rehearsal Paper account is not active; NOTHING was placed"
        )
