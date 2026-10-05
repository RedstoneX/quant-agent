"""The live rehearsal account must use production-shaped direct credentials."""
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from ops.rehearsal.direct_credentials import (
    RehearsalCredentialError,
    assert_expected_paper_account,
    bind_systemd_directory_to_rehearsal_identity,
    force_direct_alpaca_transport,
    load_rehearsal_credentials,
)
from src.credentials import CREDENTIALS_DIRECTORY_ENV
from src.session_identity import IDENTITY_ENV, credentials_directory_var


DUMMY_KEY = "AKFAKE7QZ2X9REHEARSAL"
DUMMY_SECRET = "s3cFAKErehearsalONLYvalue0000nothingreal"
DUMMY_ACCOUNT = "PAFAKEACCOUNT"


def _credential_dir(tmp_path: Path, **overrides: str) -> Path:
    values = {
        "alpaca_api_key": DUMMY_KEY,
        "alpaca_secret_key": DUMMY_SECRET,
        "account_number": DUMMY_ACCOUNT,
    }
    values.update(overrides)
    directory = tmp_path / "credentials"
    directory.mkdir()
    for name, value in values.items():
        (directory / name).write_text(value)
    return directory


def test_loads_all_values_only_from_systemd_directory(tmp_path, monkeypatch):
    directory = _credential_dir(tmp_path)
    monkeypatch.setenv(IDENTITY_ENV, "rehearsal")
    monkeypatch.setenv(credentials_directory_var("rehearsal"), str(directory))
    monkeypatch.setenv("ALPACA_API_KEY", "environment-must-not-win")
    monkeypatch.setenv("QAMC_SANDBOX_ACCOUNT_NUMBER", "environment-must-not-win")

    credentials = load_rehearsal_credentials()

    assert credentials.api_key == DUMMY_KEY
    assert credentials.secret_key == DUMMY_SECRET
    assert credentials.expected_account_number == DUMMY_ACCOUNT
    assert DUMMY_KEY not in repr(credentials)
    assert DUMMY_SECRET not in repr(credentials)


@pytest.mark.parametrize("missing", [
    "alpaca_api_key", "alpaca_secret_key", "account_number",
])
def test_missing_value_refuses_live_check(tmp_path, monkeypatch, missing):
    directory = _credential_dir(tmp_path)
    (directory / missing).unlink()
    monkeypatch.setenv(IDENTITY_ENV, "rehearsal")
    monkeypatch.setenv(credentials_directory_var("rehearsal"), str(directory))

    with pytest.raises(RehearsalCredentialError):
        load_rehearsal_credentials()


def test_transient_systemd_directory_binds_only_to_rehearsal(tmp_path, monkeypatch):
    directory = _credential_dir(tmp_path)
    monkeypatch.setenv(IDENTITY_ENV, "rehearsal")
    monkeypatch.setenv(CREDENTIALS_DIRECTORY_ENV, str(directory))

    bind_systemd_directory_to_rehearsal_identity()

    assert os.environ[credentials_directory_var("rehearsal")] == str(directory)


def test_transient_binding_refuses_desk_identity(tmp_path, monkeypatch):
    directory = _credential_dir(tmp_path)
    monkeypatch.setenv(IDENTITY_ENV, "desk")
    monkeypatch.setenv(CREDENTIALS_DIRECTORY_ENV, str(directory))

    with pytest.raises(RehearsalCredentialError):
        bind_systemd_directory_to_rehearsal_identity()


def test_direct_transport_removes_onecli_proxy_and_ca(monkeypatch):
    for name in (
        "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
        "http_proxy", "https_proxy", "all_proxy",
        "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE",
    ):
        monkeypatch.setenv(name, "must-go")

    force_direct_alpaca_transport()

    assert all(name not in os.environ for name in (
        "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
        "http_proxy", "https_proxy", "all_proxy",
        "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE",
    ))
    assert "paper-api.alpaca.markets" in os.environ["NO_PROXY"]
    assert "data.alpaca.markets" in os.environ["NO_PROXY"]


def test_account_assertion_accepts_only_pinned_active_paper_account():
    assert_expected_paper_account(
        SimpleNamespace(account_number=DUMMY_ACCOUNT, status="ACTIVE"),
        DUMMY_ACCOUNT,
    )


@pytest.mark.parametrize("account, expected", [
    (SimpleNamespace(account_number="PAOTHER", status="ACTIVE"), DUMMY_ACCOUNT),
    (SimpleNamespace(account_number=DUMMY_ACCOUNT, status="INACTIVE"), DUMMY_ACCOUNT),
    (SimpleNamespace(account_number="LIVEACCOUNT", status="ACTIVE"), "LIVEACCOUNT"),
])
def test_account_assertion_refuses_mismatch_inactive_or_nonpaper(account, expected):
    with pytest.raises(RehearsalCredentialError, match="NOTHING was placed"):
        assert_expected_paper_account(account, expected)
