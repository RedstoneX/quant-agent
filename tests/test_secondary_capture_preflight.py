"""The secondary Paper capture gate makes only account and book reads."""

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from alpaca.common.enums import BaseURL

from ops.rehearsal.secondary_preflight import (
    CapturePreflight, SecondaryPreflightError, check_secondary_capture,
)


class ReadOnlyClient:
    _base_url = "https://paper-api.alpaca.markets"

    def __init__(self, *, account="secondary-test", status="ACTIVE",
                 positions=None, orders=None):
        self.account = account
        self.status = status
        self.positions = [] if positions is None else positions
        self.orders = [] if orders is None else orders
        self.calls = []

    def get_account(self):
        self.calls.append("account")
        return SimpleNamespace(account_number=self.account, status=self.status)

    def get_all_positions(self):
        self.calls.append("positions")
        return self.positions

    def get_orders(self, *, filter):
        self.calls.append("open_orders")
        assert str(filter.status.value).lower() == "open"
        return self.orders

    def __getattr__(self, name):
        raise AssertionError(f"unexpected broker operation: {name}")


@pytest.fixture
def setup(tmp_path):
    credentials = tmp_path / "secondary-credentials"
    credentials.mkdir()
    (credentials / "alpaca_api_key").write_text("test-key-from-secondary")
    (credentials / "alpaca_secret_key").write_text("test-secret-from-secondary")
    (credentials / "account_number").write_text("secondary-test")
    spec = CapturePreflight(
        primary_account_number="primary-test",
        scratch_root=tmp_path / "scratch",
        capture_db=tmp_path / "scratch" / "data" / "quant_agent.db",
        production_root=tmp_path / "production",
        production_session_lock=tmp_path / "production.lock",
        min_available_memory_mib=1024,
        max_load_per_cpu=0.9,
        max_capture_seconds=120,
        max_capture_bytes=1024 * 1024,
    )
    return spec, {
        "QAMC_SESSION_IDENTITY": "rehearsal",
        "CREDENTIALS_DIRECTORY": str(credentials),
        "CREDENTIALS_DIRECTORY_REHEARSAL": str(credentials),
    }


def run(spec, env, client=None, **kwargs):
    client = ReadOnlyClient() if client is None else client
    return check_secondary_capture(
        spec, paper=True, base_url="https://paper-api.alpaca.markets",
        env=env, client_factory=lambda *_: client,
        resource_reader=lambda: (2048, 0.1),
        production_session_active=lambda: False,
        **kwargs,
    )


def test_success_is_three_reads_and_no_identity_or_key_is_printed(setup, capsys):
    spec, env = setup
    client = ReadOnlyClient()
    checked, credentials = run(spec, env, client)
    assert checked is client
    assert client.calls == ["account", "positions", "open_orders"]
    assert len(credentials) == 2
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("account,status,positions,orders", [
    ("primary-test", "ACTIVE", [], []),
    ("other-test", "ACTIVE", [], []),
    ("secondary-test", "INACTIVE", [], []),
    ("secondary-test", "ACTIVE", [object()], []),
    ("secondary-test", "ACTIVE", [], [object()]),
])
def test_account_or_book_mismatch_refuses_without_identifiers(
    setup, account, status, positions, orders,
):
    spec, env = setup
    client = ReadOnlyClient(account=account, status=status,
                            positions=positions, orders=orders)
    with pytest.raises(SecondaryPreflightError) as error:
        run(spec, env, client)
    assert "test" not in str(error.value)


def test_paper_config_and_resolved_sdk_endpoint_are_independent(setup):
    spec, env = setup
    with pytest.raises(SecondaryPreflightError):
        check_secondary_capture(
            spec, paper=False, base_url="https://paper-api.alpaca.markets", env=env,
            client_factory=lambda *_: (_ for _ in ()).throw(AssertionError("built")),
        )
    client = ReadOnlyClient()
    client._base_url = "https://api.alpaca.markets"
    with pytest.raises(SecondaryPreflightError, match="resolved SDK endpoint"):
        run(spec, env, client)
    assert client.calls == []


def test_real_sdk_paper_endpoint_enum_is_accepted(setup):
    spec, env = setup
    client = ReadOnlyClient()
    client._base_url = BaseURL.TRADING_PAPER
    checked, _ = run(spec, env, client)
    assert checked is client
    assert client.calls == ["account", "positions", "open_orders"]


@pytest.mark.parametrize("change", [
    lambda s: replace(s, capture_db=s.production_root / "data" / "quant_agent.db"),
    lambda s: replace(s, scratch_root=s.production_root / "scratch"),
    lambda s: replace(s, max_capture_seconds=0),
    lambda s: replace(s, max_capture_bytes=0),
    lambda s: replace(s, primary_account_number=""),
])
def test_isolation_or_bounds_refuse_before_credentials_are_read(setup, change):
    spec, env = setup
    env = {
        "QAMC_SESSION_IDENTITY": "rehearsal",
        "CREDENTIALS_DIRECTORY": "/path/that/does/not/exist",
        "CREDENTIALS_DIRECTORY_REHEARSAL": "/path/that/does/not/exist",
    }
    with pytest.raises(SecondaryPreflightError) as error:
        run(change(spec), env)
    assert "credential" not in str(error.value)


def test_missing_secondary_pair_does_not_fall_back_to_desk_environment(setup):
    spec, _ = setup
    with pytest.raises(SecondaryPreflightError, match="capture session identity"):
        run(spec, {"ALPACA_API_KEY": "desk-key", "ALPACA_SECRET_KEY": "desk-secret"})


def test_rehearsal_directory_must_match_systemd_delivery(setup):
    spec, env = setup
    env["CREDENTIALS_DIRECTORY"] = "/some/other/directory"
    with pytest.raises(SecondaryPreflightError, match="systemd credential directory"):
        run(spec, env)


def test_expected_secondary_identity_is_required_from_the_delivered_file(setup):
    spec, env = setup
    identity_file = Path(env["CREDENTIALS_DIRECTORY_REHEARSAL"]) / "account_number"
    identity_file.unlink()
    with pytest.raises(SecondaryPreflightError, match="account_number"):
        run(spec, env)


def test_delivered_secondary_identity_cannot_equal_primary(setup):
    spec, env = setup
    identity_file = Path(env["CREDENTIALS_DIRECTORY_REHEARSAL"]) / "account_number"
    identity_file.write_text("primary-test")
    with pytest.raises(SecondaryPreflightError, match="identities match"):
        run(spec, env)


def test_active_session_and_low_headroom_refuse(setup):
    spec, env = setup
    spec.production_session_lock.mkdir()
    with pytest.raises(SecondaryPreflightError, match="session"):
        run(spec, env)
    spec.production_session_lock.rmdir()
    with pytest.raises(SecondaryPreflightError, match="service"):
        check_secondary_capture(
            spec, paper=True, base_url="https://paper-api.alpaca.markets", env=env,
            client_factory=lambda *_: ReadOnlyClient(),
            resource_reader=lambda: (2048, 0.1),
            production_session_active=lambda: True,
        )
    with pytest.raises(SecondaryPreflightError, match="headroom"):
        check_secondary_capture(
            spec, paper=True, base_url="https://paper-api.alpaca.markets", env=env,
            client_factory=lambda *_: ReadOnlyClient(),
            resource_reader=lambda: (512, 0.1),
            production_session_active=lambda: False,
        )


def test_broker_read_failure_is_redacted(setup):
    spec, env = setup
    class FailingClient(ReadOnlyClient):
        def get_account(self):
            raise RuntimeError("backend includes secret-key")
    with pytest.raises(SecondaryPreflightError, match="read-only secondary broker") as error:
        run(spec, env, FailingClient())
    assert "secret-key" not in str(error.value)
    assert error.value.__suppress_context__ is True
