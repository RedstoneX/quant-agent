"""The secondary Paper capture gate makes only account and book reads."""

from dataclasses import replace
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from alpaca.common.enums import BaseURL

from ops.rehearsal.secondary_preflight import (
    CapturePreflight, REQUIRED_PARKED_TIMERS, SecondaryPreflightError,
    assert_natural_morning_window, assert_qamc_timers_parked,
    check_secondary_capture, hold_capture_session_lock,
    load_primary_account_assertion,
)
from src.trading_calendar import ET


class ReadOnlyClient:
    _base_url = "https://paper-api.alpaca.markets"

    def __init__(self, *, account="PASECONDARYTEST", status="ACTIVE",
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
    spec = CapturePreflight(
        primary_account_number="PAPRIMARYTEST",
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
    ("PAPRIMARYTEST", "ACTIVE", [], []),
    ("other-test", "ACTIVE", [], []),
    ("PASECONDARYTEST", "INACTIVE", [], []),
    ("PASECONDARYTEST", "ACTIVE", [object()], []),
    ("PASECONDARYTEST", "ACTIVE", [], [object()]),
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


def test_secondary_identity_is_derived_from_broker_without_delivered_file(setup):
    spec, env = setup
    checked, credentials = run(spec, env)
    assert checked.account == "PASECONDARYTEST"
    assert len(credentials) == 2


def test_broker_secondary_identity_cannot_equal_primary(setup):
    spec, env = setup
    with pytest.raises(SecondaryPreflightError, match="identities match"):
        run(spec, env, ReadOnlyClient(account="PAPRIMARYTEST"))


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


def test_runtime_qamc_checks_its_own_units_without_sudo(monkeypatch):
    from ops.rehearsal import secondary_preflight as module

    seen = []
    monkeypatch.setattr(module.os, "geteuid", lambda: 1234)
    monkeypatch.setattr(module.pwd, "getpwnam", lambda _: SimpleNamespace(pw_uid=1234))
    monkeypatch.setattr(module.subprocess, "run", lambda command, **_: (
        seen.append(command) or SimpleNamespace(returncode=0, stdout="")
    ))
    assert module._running_trading_units() is False
    assert seen[0][:2] == ["env", "XDG_RUNTIME_DIR=/run/user/1234"]
    assert "sudo" not in seen[0]


def test_primary_identity_assertion_requires_separate_systemd_file(setup):
    _, env = setup
    with pytest.raises(SecondaryPreflightError, match="primary account identity"):
        load_primary_account_assertion(env)
    directory = Path(env["CREDENTIALS_DIRECTORY_REHEARSAL"])
    (directory / "primary_account_number").write_text("primary-test")
    assert load_primary_account_assertion(env) == "primary-test"
    env["CREDENTIALS_DIRECTORY"] = "/another/directory"
    with pytest.raises(SecondaryPreflightError, match="systemd credential directory"):
        load_primary_account_assertion(env)


def test_capture_lease_uses_existing_lock_and_releases_its_own_owner(tmp_path):
    lock = tmp_path / "active-session.lock"
    with hold_capture_session_lock(lock):
        assert lock.is_dir()
        assert (lock / "owner").read_text().startswith("secondary_capture ")
        with pytest.raises(SecondaryPreflightError, match="lock exists"):
            with hold_capture_session_lock(lock):
                pass
    assert not lock.exists()


def test_capture_lease_never_removes_a_changed_owner(tmp_path):
    lock = tmp_path / "active-session.lock"
    with pytest.raises(SecondaryPreflightError, match="changed owner"):
        with hold_capture_session_lock(lock):
            (lock / "owner").write_text("another process\n")
    assert (lock / "owner").read_text() == "another process\n"


def test_capture_requires_every_qamc_timer_parked(monkeypatch):
    from ops.rehearsal import secondary_preflight as module

    monkeypatch.setattr(module.os, "geteuid", lambda: 1234)
    monkeypatch.setattr(module.pwd, "getpwnam", lambda _: SimpleNamespace(pw_uid=1234))
    states = {name: "disabled" for name in REQUIRED_PARKED_TIMERS}

    def read(command, **_):
        if "list-unit-files" in command:
            output = "".join(f"{name} {state} enabled\n" for name, state in states.items())
        else:
            output = ""
        return SimpleNamespace(returncode=0, stdout=output)

    monkeypatch.setattr(module.subprocess, "run", read)
    assert_qamc_timers_parked()
    states["quant-agent-intra_check.timer"] = "enabled"
    with pytest.raises(SecondaryPreflightError, match="timer is enabled"):
        assert_qamc_timers_parked()
    del states["quant-agent-intra_check.timer"]
    with pytest.raises(SecondaryPreflightError, match="cannot be accounted for"):
        assert_qamc_timers_parked()


def test_capture_refuses_active_timer_even_when_unit_file_disabled(monkeypatch):
    from ops.rehearsal import secondary_preflight as module

    monkeypatch.setattr(module.os, "geteuid", lambda: 1234)
    monkeypatch.setattr(module.pwd, "getpwnam", lambda _: SimpleNamespace(pw_uid=1234))

    def read(command, **_):
        if "list-unit-files" in command:
            output = "".join(f"{name} disabled enabled\n" for name in REQUIRED_PARKED_TIMERS)
        else:
            output = "quant-agent-intra_safety.timer loaded active waiting\n"
        return SimpleNamespace(returncode=0, stdout=output)

    monkeypatch.setattr(module.subprocess, "run", read)
    with pytest.raises(SecondaryPreflightError, match="timer is active"):
        assert_qamc_timers_parked()


@pytest.mark.parametrize("when,open_now,reason", [
    ("2026-10-07T10:00:00", True, None),
    ("2026-10-07T12:01:00", True, "outside the natural morning"),
    ("2026-10-10T10:00:00", True, "outside the natural morning"),
    ("2026-10-07T10:00:00", False, "market is not open"),
])
def test_capture_requires_natural_open_morning(when, open_now, reason):
    class ClockClient:
        def get_clock(self):
            return SimpleNamespace(is_open=open_now)

    moment = datetime.fromisoformat(when).replace(tzinfo=ET)
    if reason:
        with pytest.raises(SecondaryPreflightError, match=reason):
            assert_natural_morning_window(ClockClient(), moment)
    else:
        assert_natural_morning_window(ClockClient(), moment)
