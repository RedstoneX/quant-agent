"""Read-only primary identity assertion: no key or account crosses output."""

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from ops.rehearsal import primary_identity_assertion
from ops.rehearsal.primary_identity_assertion import (
    OUTPUT_NAME,
    PrimaryIdentityError,
    write_primary_identity_assertion,
)


@pytest.fixture
def identity_inputs(tmp_path, monkeypatch):
    uid = os.geteuid()

    def qamc_user(name):
        assert name == "qamc"
        return SimpleNamespace(pw_uid=uid)

    monkeypatch.setattr(primary_identity_assertion, "pwd", SimpleNamespace(getpwnam=qamc_user))
    # Files are already systemd's per-unit copy in this test, never .env.
    delivered = tmp_path / "systemd"
    delivered.mkdir()
    (delivered / "alpaca_api_key").write_text("PKABCDEFGHIJKLMN\n")
    (delivered / "alpaca_secret_key").write_text("SKABCDEFGHIJKLMN\n")
    scratch = tmp_path / "scratch"
    scratch.mkdir(mode=0o700)
    env = {"QAMC_SESSION_IDENTITY": "desk", "CREDENTIALS_DIRECTORY": str(delivered)}
    return env, scratch


def _client(
    number="PAPRIMARY12345", *, endpoint="https://paper-api.alpaca.markets", status="ACTIVE", fail=None, calls=None
):
    class Client:
        _base_url = endpoint

        def get_account(self):
            if calls is not None:
                calls.append("get_account")
            if fail is not None:
                raise fail
            return SimpleNamespace(account_number=number, status=status)

    return Client()


def test_only_one_read_and_private_number_file(identity_inputs, monkeypatch):
    env, scratch = identity_inputs
    calls = []
    monkeypatch.setenv("HTTPS_PROXY", "http://onecli.invalid:9999")

    def factory(key, secret):
        assert (key, secret) == ("PKABCDEFGHIJKLMN", "SKABCDEFGHIJKLMN")
        assert "HTTPS_PROXY" not in os.environ
        return _client(calls=calls)

    target = write_primary_identity_assertion(scratch, env=env, client_factory=factory)
    assert target == scratch / OUTPUT_NAME
    assert target.read_text() == "PAPRIMARY12345\n"
    assert target.stat().st_mode & 0o777 == 0o600
    assert list(scratch.iterdir()) == [target]
    assert calls == ["get_account"]
    assert os.environ["HTTPS_PROXY"] == "http://onecli.invalid:9999"


def test_broker_paper_identity_is_derived_without_secondary_file(identity_inputs):
    env, scratch = identity_inputs
    target = write_primary_identity_assertion(
        scratch,
        env=env,
        client_factory=lambda *_: _client("PASECONDARY12345"),
    )
    assert target.read_text() == "PASECONDARY12345\n"


def test_non_paper_endpoint_refused_before_account_call(identity_inputs):
    env, scratch = identity_inputs
    calls = []
    with pytest.raises(PrimaryIdentityError, match="not pointed at Alpaca Paper"):
        write_primary_identity_assertion(
            scratch,
            env=env,
            client_factory=lambda *_: _client(
                endpoint="https://api.alpaca.markets",
                calls=calls,
            ),
        )
    assert calls == []
    assert list(scratch.iterdir()) == []


def test_inactive_primary_account_refused_without_output(identity_inputs):
    env, scratch = identity_inputs
    with pytest.raises(PrimaryIdentityError, match="not active"):
        write_primary_identity_assertion(
            scratch,
            env=env,
            client_factory=lambda *_: _client(status="INACTIVE"),
        )
    assert list(scratch.iterdir()) == []


def test_broker_failure_is_generic_and_does_not_write(identity_inputs):
    env, scratch = identity_inputs
    with pytest.raises(PrimaryIdentityError) as failure:
        write_primary_identity_assertion(
            scratch,
            env=env,
            client_factory=lambda *_: _client(
                fail=RuntimeError("PKABCDEFGHIJKLMN PAPRIMARY12345"),
            ),
        )
    assert str(failure.value) == "primary Paper account read failed"
    assert list(scratch.iterdir()) == []


def test_existing_assertion_is_never_overwritten(identity_inputs):
    env, scratch = identity_inputs
    target = scratch / OUTPUT_NAME
    target.write_text("existing\n")
    with pytest.raises(PrimaryIdentityError, match="already exists"):
        write_primary_identity_assertion(
            scratch,
            env=env,
            client_factory=lambda *_: pytest.fail("must not contact broker"),
        )
    assert target.read_text() == "existing\n"


def test_missing_systemd_secret_never_falls_back_to_environment(identity_inputs):
    env, scratch = identity_inputs
    (Path(env["CREDENTIALS_DIRECTORY"]) / "alpaca_secret_key").unlink()
    env["ALPACA_SECRET_KEY"] = "environment-secret"
    with pytest.raises(PrimaryIdentityError, match="missing or placeholders"):
        write_primary_identity_assertion(
            scratch,
            env=env,
            client_factory=lambda *_: pytest.fail("must not contact broker"),
        )


def test_shared_or_production_scratch_refused(identity_inputs, monkeypatch):
    env, scratch = identity_inputs
    scratch.chmod(0o750)
    with pytest.raises(PrimaryIdentityError, match="not isolated private scratch"):
        write_primary_identity_assertion(scratch, env=env)
