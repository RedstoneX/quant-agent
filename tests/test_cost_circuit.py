"""Mandatory paid-analysis circuit: accounting, persistence and cutoffs."""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from src.agents.base import BaseAgent, LLMStreamInterruptedError
from src.cost_circuit import (
    LLMCostCircuitBreaker,
    OptionalPaidAnalysisRetrySkipped,
    PaidAnalysisSuspended,
    _ET,
    _is_known_zero_cost_failure,
    _trigger_scope,
    activate_paid_call_session,
)
from src.pipeline import TradingPipeline
from src.storage.db import Database


class _Notifier:
    def __init__(self):
        self.messages: list[str] = []

    def send(self, message: str) -> bool:
        self.messages.append(message)
        return True


class _Agent(BaseAgent):
    @property
    def name(self) -> str:
        return "test_agent"

    @property
    def system_prompt(self) -> str:
        return "Return JSON."

    def build_user_message(self, **kwargs) -> str:
        return "Analyze this bounded fixture."


def _config(**overrides):
    values = {
        "enabled": True,
        "session_cost_limit_usd": 10.0,
        "daily_cost_limit_usd": 20.0,
        # Backstop only as of Defect 4 (2026-08-28) -- matches
        # src/config.py's production default. Tests exercising the count
        # cap itself override it explicitly.
        "max_free_failure_sessions_per_mode": 8,
        "max_provider_attempts_per_call": 2,
        "max_retry_attempts_per_session": 2,
        "reservation_ttl_minutes": 30,
        "input_chars_per_token": 3.5,
        "reservation_multiplier": 1.05,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _db_path(tmp_path):
    path = tmp_path / "circuit.db"
    db = Database(str(path))
    db.initialize()
    db.conn.close()
    return str(path)


def test_pipeline_attaches_breaker_to_every_paid_agent():
    pipeline = TradingPipeline.__new__(TradingPipeline)
    circuit = object()
    pipeline.cost_circuit = circuit
    names = (
        "tech_analyst", "news_analyst", "macro_analyst",
        "earnings_analyst", "smart_money_analyst", "portfolio_manager",
        "risk_manager", "position_reviewer", "evening_analyst",
        "meta_reflector",
    )
    agents = {}
    for name in names:
        agent = MagicMock()
        agents[name] = agent
        setattr(pipeline, name, agent)

    pipeline._attach_cost_circuit_to_agents()

    for agent in agents.values():
        agent.set_cost_circuit.assert_called_once_with(circuit)


def test_projected_session_spend_blocks_before_provider_request(tmp_path):
    notifier = _Notifier()
    circuit = LLMCostCircuitBreaker(
        _db_path(tmp_path),
        _config(session_cost_limit_usd=0.01, daily_cost_limit_usd=1.0),
        notifier,
    )
    circuit.activate_session("run-projected", "morning")

    with pytest.raises(PaidAnalysisSuspended):
        circuit.begin_call(
            agent_name="portfolio_manager",
            model="openai/gpt-5.5",
            system_prompt="system",
            user_message="input",
            max_output_tokens=16_000,
        )

    state = circuit.status()
    assert state["trigger_code"] == "projected_session_cost_limit"
    assert state["provider_attempts"] == 0
    assert len(notifier.messages) == 1
    alert = notifier.messages[0]
    assert "affected run: run-projected" in alert
    assert "attempts: 0 provider attempts" in alert
    assert "QAMC PAID ANALYSIS QUOTA HOLD" in alert
    assert "scope: run run-projected only" in alert
    assert "later independent sessions remain eligible" in alert
    assert "preserved: broker-resident stops" in alert


def test_optional_retry_budget_exhaustion_skips_without_opening_circuit(tmp_path):
    path = _db_path(tmp_path)
    circuit = LLMCostCircuitBreaker(path, _config(), _Notifier())
    circuit.activate_session("run-optional-retry", "morning")
    with sqlite3.connect(path) as conn:
        conn.execute(
            "UPDATE llm_budget_sessions SET retry_attempts=2 WHERE run_id=?",
            ("run-optional-retry",),
        )

    with pytest.raises(OptionalPaidAnalysisRetrySkipped):
        circuit.begin_call(
            agent_name="tech_analyst",
            model="google/gemini-2.5-flash-lite",
            system_prompt="s",
            user_message="u",
            max_output_tokens=100,
            retry_kind="missing_symbol_recovery",
            optional_retry=True,
        )

    state = circuit.status()
    assert state["suspended"] is False
    with sqlite3.connect(path) as conn:
        row = conn.execute(
            "SELECT logical_calls, provider_attempts, retry_attempts, status "
            "FROM llm_budget_sessions WHERE run_id=?",
            ("run-optional-retry",),
        ).fetchone()
        reservations = conn.execute(
            "SELECT COUNT(*) FROM llm_budget_reservations WHERE run_id=?",
            ("run-optional-retry",),
        ).fetchone()[0]
    assert row == (0, 0, 2, "active")
    assert reservations == 0


def test_completed_session_spend_holds_only_that_session_and_cannot_be_reset(tmp_path):
    path = _db_path(tmp_path)
    notifier = _Notifier()
    cfg = _config(session_cost_limit_usd=0.50, daily_cost_limit_usd=2.0)
    circuit = LLMCostCircuitBreaker(path, cfg, notifier)
    circuit.activate_session("run-cost", "morning")
    reservation = circuit.begin_call(
        agent_name="tech_analyst", model="google/gemini-2.5-flash-lite",
        system_prompt="s", user_message="u", max_output_tokens=100,
    )
    circuit.before_provider_attempt(reservation, model=reservation.model)
    circuit.complete_call(reservation, 0.60, actual_model=reservation.model)

    assert circuit.status()["trigger_code"] == "session_cost_limit"
    assert circuit.status()["current_session_cost_usd"] == pytest.approx(0.60)
    assert len(notifier.messages) == 1

    # A new process/object sees the same run-scoped hold and does not duplicate
    # an alert already successfully delivered.
    second_notifier = _Notifier()
    second = LLMCostCircuitBreaker(path, cfg, second_notifier)
    second.activate_session("run-cost", "morning")
    assert second.status()["suspended"] is True
    assert second_notifier.messages == []

    with pytest.raises(ValueError):
        second.reset("  ")
    with pytest.raises(ValueError, match="no operator-resettable hard circuit"):
        second.reset("operator reviewed quota hold")

    # The affected run remains stopped, while a genuinely independent session
    # can spend under the still-available daily budget.
    second.activate_session("run-cost-next", "midday")
    reservation = second.begin_call(
        agent_name="tech_analyst",
        model="google/gemini-2.5-flash-lite",
        system_prompt="s", user_message="u", max_output_tokens=100,
    )
    assert reservation.reservation_id != "disabled"
    second.activate_session("run-cost", "morning")
    with pytest.raises(PaidAnalysisSuspended):
        second.require_paid_analysis("same_run_again")


def test_existing_daily_logs_seed_budget_and_trip_on_activation(tmp_path):
    path = _db_path(tmp_path)
    with sqlite3.connect(path) as conn:
        conn.executemany(
            "INSERT INTO agent_logs "
            "(agent_name, run_id, model, tokens_used, cost_usd) VALUES (?, ?, ?, ?, ?)",
            [
                ("tech_analyst", "run-old-1", "m", 1, 0.80),
                ("portfolio_manager", "run-old-2", "m", 1, 0.75),
            ],
        )
    notifier = _Notifier()
    circuit = LLMCostCircuitBreaker(
        path,
        _config(session_cost_limit_usd=1.0, daily_cost_limit_usd=1.50),
        notifier,
    )
    circuit.activate_session("run-new", "morning")

    state = circuit.status()
    assert state["trigger_code"] == "daily_cost_limit"
    assert state["current_daily_cost_usd"] == pytest.approx(1.55)
    assert "$1.5500 on ET day" in notifier.messages[0]
    assert "auto" in notifier.messages[0]


def test_base_agent_third_provider_attempt_is_blocked_before_network(tmp_path, monkeypatch):
    path = _db_path(tmp_path)
    notifier = _Notifier()
    circuit = LLMCostCircuitBreaker(
        path,
        _config(
            max_provider_attempts_per_call=2,
            max_retry_attempts_per_session=10,
        ),
        notifier,
    )
    circuit.activate_session("run-retries", "morning")
    monkeypatch.setenv("QUANT_AGENT_MAX_RETRIES", "3")
    monkeypatch.setattr("time.sleep", lambda _seconds: None)

    client = MagicMock()
    client.messages.create.side_effect = ConnectionError("provider down")
    with patch("anthropic.Anthropic", return_value=client):
        agent = _Agent(api_key="x", model="claude-sonnet-4-6", max_tokens=64)
        agent.set_cost_circuit(circuit)
        with pytest.raises(PaidAnalysisSuspended):
            agent.run()

    # The point of the test, unchanged: the attempt past the ceiling never
    # reaches the network.
    assert client.messages.create.call_count == 2
    assert circuit.status()["provider_attempts"] == 2

    # Defect 5 (2026-08-31): the attempt limit now HOLDS the run rather than
    # latching the desk, so it is the first of two alerts, not the only one.
    assert "QUOTA HOLD" in notifier.messages[0]
    assert "provider attempt 3 exceeds per-call safe limit 2" in notifier.messages[0]
    assert "scope: run run-retries only" in notifier.messages[0]

    # And then the call it blocked fails with no usage telemetry, which is a
    # SEPARATE, still-hard trigger. Asserted rather than glossed: this is the
    # remaining path by which a per-call attempt stop can still cost a
    # trading day, and it is deliberately left alone. `fail_call` cannot tell
    # "the circuit refused to send attempt 3" from "attempts 1 and 2 burned
    # tokens we never got told about", and on a system that trades money,
    # under-counting real spend is the worse error. It no longer fires on the
    # 2026-08-31 failure mode (see tests/test_provider_attempt_budget.py --
    # with the ceiling correct, the failover simply succeeds), so what
    # reaches here is a genuine attempt runaway, which is a thing an operator
    # SHOULD be made to look at.
    state = circuit.status()
    assert state["trigger_code"] == "failed_call_unknown_cost"
    assert len(notifier.messages) == 2
    assert "without final usage telemetry" in notifier.messages[1]


def test_failed_call_without_usage_consumes_reserve_and_latches(tmp_path, monkeypatch):
    path = _db_path(tmp_path)
    notifier = _Notifier()
    circuit = LLMCostCircuitBreaker(path, _config(), notifier)
    circuit.activate_session("run-failed", "morning")
    monkeypatch.setenv("QUANT_AGENT_MAX_RETRIES", "1")

    client = MagicMock()
    client.messages.create.side_effect = ConnectionError("stream cut")
    with patch("anthropic.Anthropic", return_value=client):
        agent = _Agent(api_key="x", model="claude-sonnet-4-6", max_tokens=64)
        agent.set_cost_circuit(circuit)
        with pytest.raises(ConnectionError):
            agent.run()

    state = circuit.status()
    assert state["trigger_code"] == "failed_call_unknown_cost"
    assert state["current_session_cost_usd"] > 0
    assert state["current_daily_cost_usd"] > 0
    assert "without final usage telemetry" in notifier.messages[0]


# ============================================================================
# Defect 2 (2026-08-28): a provably-$0 provider rejection must not be
# charged, and must not trip the circuit. `_is_known_zero_cost_failure`
# is deliberately narrow -- every one of these tests confirms one specific
# admitted class; the very last one confirms the fail-closed default is
# untouched (an unrecognized/ambiguous failure is charged and trips
# exactly as before this fix).
# ============================================================================

class _StatusCodeError(Exception):
    """Stand-in for `anthropic.APIStatusError` / `openai.APIStatusError` --
    both SDKs set `.status_code` on every subclass (RateLimitError,
    BadRequestError, AuthenticationError, PermissionDeniedError,
    NotFoundError, InternalServerError, ...)."""

    def __init__(self, status_code: int, message: str = "provider error"):
        super().__init__(message)
        self.status_code = status_code


def _wrapped(outer_message: str, *, outer_name: str, cause: BaseException) -> Exception:
    """Build `OuterName("outer_message")` with `__cause__ == cause`, the way
    the anthropic/openai SDKs wrap the concrete httpx/socket/ssl failure
    (`raise APIConnectionError(...) from exc`) instead of replacing it."""
    outer_cls = type(outer_name, (Exception,), {})
    err = outer_cls(outer_message)
    err.__cause__ = cause
    return err


def _authorize_and_fail(circuit, error: BaseException, *, run_id: str,
                         agent_name: str = "tech_analyst",
                         model: str = "google/gemini-2.5-flash-lite"):
    """Reserve + authorize one provider attempt (so `attempt_count > 0`,
    matching every real call this classification applies to -- provider
    authorization always happens before the network call that can fail),
    then fail it with `error`. Returns the reservation."""
    circuit.activate_session(run_id, "morning")
    reservation = circuit.begin_call(
        agent_name=agent_name, model=model,
        system_prompt="s", user_message="u", max_output_tokens=100,
    )
    circuit.before_provider_attempt(reservation, model=reservation.model)
    circuit.fail_call(reservation, error)
    return reservation


def _assert_charged_nothing_and_did_not_trip(circuit, notifier, path, reservation):
    state = circuit.status()
    assert state["suspended"] is False
    assert not state.get("trigger_code")
    assert state["current_session_cost_usd"] == 0
    assert state["current_daily_cost_usd"] == 0
    assert notifier.messages == []
    with sqlite3.connect(path) as conn:
        row = conn.execute(
            "SELECT status, actual_cost_usd, reserved_cost_usd "
            "FROM llm_budget_reservations WHERE reservation_id=?",
            (reservation.reservation_id,),
        ).fetchone()
    assert row == ("failed", 0.0, 0.0)


def _assert_charged_and_tripped(circuit, notifier, reservation):
    state = circuit.status()
    assert state["suspended"] is True
    assert state["trigger_code"] == "failed_call_unknown_cost"
    assert state["current_session_cost_usd"] > 0
    assert state["current_daily_cost_usd"] > 0
    assert "without final usage telemetry" in notifier.messages[0]


@pytest.mark.parametrize("status_code", [429, 400, 401, 403, 404])
def test_known_zero_cost_status_codes_charge_nothing_and_do_not_trip(tmp_path, status_code):
    path = _db_path(tmp_path)
    notifier = _Notifier()
    circuit = LLMCostCircuitBreaker(path, _config(), notifier)
    error = _StatusCodeError(status_code, f"provider rejected ({status_code})")

    reservation = _authorize_and_fail(circuit, error, run_id=f"run-{status_code}")

    assert _is_known_zero_cost_failure(error) is True
    _assert_charged_nothing_and_did_not_trip(circuit, notifier, path, reservation)


def test_known_zero_cost_pre_send_connection_refused_charges_nothing(tmp_path):
    path = _db_path(tmp_path)
    notifier = _Notifier()
    circuit = LLMCostCircuitBreaker(path, _config(), notifier)
    # A bare, unwrapped connection-refused failure -- e.g. a local outbound
    # proxy/firewall reachability problem before the request ever left the
    # box. No status code was ever received because nothing was ever sent.
    error = ConnectionRefusedError("Connection refused")

    reservation = _authorize_and_fail(circuit, error, run_id="run-conn-refused")

    assert _is_known_zero_cost_failure(error) is True
    _assert_charged_nothing_and_did_not_trip(circuit, notifier, path, reservation)


def test_known_zero_cost_pre_send_dns_failure_wrapped_by_sdk_charges_nothing(tmp_path):
    path = _db_path(tmp_path)
    notifier = _Notifier()
    circuit = LLMCostCircuitBreaker(path, _config(), notifier)
    # DNS resolution failed before a connection attempt was even possible.
    # Both provider SDKs wrap the concrete httpx/socket cause into their own
    # `APIConnectionError` rather than raising it directly -- the top-level
    # wrapper's own name is ambiguous; the *cause* is what proves this was
    # pre-send.
    import socket
    error = _wrapped(
        "Connection error.", outer_name="APIConnectionError",
        cause=socket.gaierror("Name or service not known"),
    )

    reservation = _authorize_and_fail(circuit, error, run_id="run-dns")

    assert _is_known_zero_cost_failure(error) is True
    _assert_charged_nothing_and_did_not_trip(circuit, notifier, path, reservation)


def test_known_zero_cost_pre_send_tls_handshake_failure_charges_nothing(tmp_path):
    path = _db_path(tmp_path)
    notifier = _Notifier()
    circuit = LLMCostCircuitBreaker(path, _config(), notifier)
    ssl_error_cls = type("SSLError", (Exception,), {})
    error = _wrapped(
        "Connection error.", outer_name="APIConnectionError",
        cause=ssl_error_cls("[SSL] handshake failure"),
    )

    reservation = _authorize_and_fail(circuit, error, run_id="run-tls")

    assert _is_known_zero_cost_failure(error) is True
    _assert_charged_nothing_and_did_not_trip(circuit, notifier, path, reservation)


@pytest.mark.parametrize("status_code", [500, 502, 503])
def test_5xx_keeps_conservative_charge_and_trip(tmp_path, status_code):
    path = _db_path(tmp_path)
    notifier = _Notifier()
    circuit = LLMCostCircuitBreaker(path, _config(), notifier)
    error = _StatusCodeError(status_code, f"provider error ({status_code})")

    reservation = _authorize_and_fail(circuit, error, run_id=f"run-{status_code}")

    assert _is_known_zero_cost_failure(error) is False
    _assert_charged_and_tripped(circuit, notifier, reservation)


def test_timeout_after_send_keeps_conservative_charge_and_trips(tmp_path):
    path = _db_path(tmp_path)
    notifier = _Notifier()
    circuit = LLMCostCircuitBreaker(path, _config(), notifier)
    # The request was already written to the socket; the provider stopped
    # responding while QAMC was waiting for output. Unlike ConnectTimeout
    # (never even connected), a ReadTimeout cannot prove nothing was billed.
    error = _wrapped(
        "Request timed out.", outer_name="APITimeoutError",
        cause=_wrapped("timed out", outer_name="ReadTimeout", cause=Exception("stub")),
    )

    reservation = _authorize_and_fail(circuit, error, run_id="run-read-timeout")

    assert _is_known_zero_cost_failure(error) is False
    _assert_charged_and_tripped(circuit, notifier, reservation)


def test_truncated_stream_keeps_conservative_charge_and_trips(tmp_path):
    path = _db_path(tmp_path)
    notifier = _Notifier()
    circuit = LLMCostCircuitBreaker(path, _config(), notifier)
    error = LLMStreamInterruptedError("stream cut mid-response, no usage telemetry")

    reservation = _authorize_and_fail(circuit, error, run_id="run-truncated")

    assert _is_known_zero_cost_failure(error) is False
    _assert_charged_and_tripped(circuit, notifier, reservation)


def test_unrecognized_failure_defaults_to_ambiguous_not_zero_cost(tmp_path):
    """Fail closed: an exception this classification has never seen (no
    status_code, no recognized pre-send transport name anywhere in its
    cause chain) must be treated exactly as conservatively as before --
    never assumed to be the cheaper, zero-cost outcome."""
    path = _db_path(tmp_path)
    notifier = _Notifier()
    circuit = LLMCostCircuitBreaker(path, _config(), notifier)
    error = RuntimeError("something the classifier has never heard of")

    reservation = _authorize_and_fail(circuit, error, run_id="run-unknown")

    assert _is_known_zero_cost_failure(error) is False
    _assert_charged_and_tripped(circuit, notifier, reservation)


# ============================================================================
# Defect 4 (2026-08-28): `max_free_failure_sessions_per_mode` (default 2)
# was the OPERATIVE per-mode limit, not a backstop -- intra_check fires 14
# times between 09:30-16:00 ET, so its 3rd session of the day tripped at
# 11:30 ET with only $0.1765 spent all day. Replaced with a dollar-based
# per-mode allowance (`max_mode_daily_exposure_pct`) plus an afternoon
# reserve (`afternoon_reserve_pct` / `afternoon_reserve_release_et_hour`);
# the session count is kept only as a much-higher backstop against an
# infinite loop.
# ============================================================================

def _settle_cheap_session(circuit, path, run_id, mode, cost):
    circuit.activate_session(run_id, mode)
    reservation = circuit.begin_call(
        agent_name="tech_analyst", model="google/gemini-2.5-flash-lite",
        system_prompt="s", user_message="u", max_output_tokens=100,
    )
    circuit.before_provider_attempt(reservation, model=reservation.model)
    circuit.complete_call(reservation, cost, actual_model=reservation.model)


def test_old_session_count_default_no_longer_blocks_paid_sessions(tmp_path):
    """Historically (Defect 4, pre-4.1): with the OLD default (2), a 3rd
    same-day intra_check session was blocked regardless of how little money
    had actually been spent -- exactly the 11:30 ET stop at $0.1765/day.
    That was a COUNTING bug (`logical_calls>0 OR provider_attempts>0` counts
    every session that did anything, paid or not), not a config bug -- so
    fixing the query, not just raising the number, must mean even this
    dangerously-low old default (2) does not block a 3rd PAID session
    (Defect 4.1, 2026-08-29)."""
    path = _db_path(tmp_path)
    notifier = _Notifier()
    circuit = LLMCostCircuitBreaker(
        path, _config(max_free_failure_sessions_per_mode=2), notifier,
    )
    _settle_cheap_session(circuit, path, "intra_check-0", "intra_check", 0.0588)
    _settle_cheap_session(circuit, path, "intra_check-1", "intra_check", 0.0588)

    circuit.activate_session("intra_check-2", "intra_check")
    reservation = circuit.begin_call(
        agent_name="tech_analyst", model="google/gemini-2.5-flash-lite",
        system_prompt="s", user_message="u", max_output_tokens=100,
    )
    assert reservation.reservation_id
    state = circuit.status()
    assert state["suspended"] is False
    assert notifier.messages == []


def test_fixed_default_no_longer_blocks_the_2026_08_28_scenario(tmp_path):
    """NEW behaviour with the fixed default (8, backstop only): the same
    three cheap intra_check sessions from the test above do not trip
    anything -- the gate is now dollars, and $0.1765/day is nowhere near
    any dollar ceiling."""
    path = _db_path(tmp_path)
    notifier = _Notifier()
    circuit = LLMCostCircuitBreaker(path, _config(), notifier)  # max_sessions default now 8
    _settle_cheap_session(circuit, path, "intra_check-0", "intra_check", 0.0588)
    _settle_cheap_session(circuit, path, "intra_check-1", "intra_check", 0.0588)
    _settle_cheap_session(circuit, path, "intra_check-2", "intra_check", 0.0588)

    state = circuit.status()
    assert state["suspended"] is False
    assert notifier.messages == []


def test_session_count_backstop_still_trips_a_genuine_runaway_loop(tmp_path):
    """The count cap must still exist as a backstop: a loop of sessions
    that spend essentially nothing (e.g. every attempt is a Defect-2
    known-zero-cost rejection) would never trip a dollar-based check, so
    something must still stop it.

    Post-4.1 the trip is a bounded cooling-off window rather than a sticky
    mode-day latch (see the cooling-off tests below), so unlike the old
    behaviour `circuit.status()` right after is NOT expected to still show
    it -- the raised exception's own state is the thing to check, exactly
    like the afternoon reserve's non-sticky checks elsewhere in this file."""
    path = _db_path(tmp_path)
    notifier = _Notifier()
    circuit = LLMCostCircuitBreaker(
        path, _config(max_free_failure_sessions_per_mode=3), notifier,
    )
    for i in range(3):
        _settle_cheap_session(circuit, path, f"intra_check-{i}", "intra_check", 0.0)

    circuit.activate_session("intra_check-3", "intra_check")
    with pytest.raises(PaidAnalysisSuspended) as excinfo:
        circuit.begin_call(
            agent_name="tech_analyst", model="google/gemini-2.5-flash-lite",
            system_prompt="s", user_message="u", max_output_tokens=100,
        )
    assert excinfo.value.state["trigger_code"] == "session_retry_limit"


def test_mode_daily_spend_limit_trips_on_dollars_not_session_count(tmp_path):
    """The new operative per-mode gate: two sessions of the SAME mode have
    already settled more than the per-mode dollar ceiling between them; a
    3rd is blocked on dollars while the session-count backstop (100,
    deliberately out of the way here) never enters into it."""
    path = _db_path(tmp_path)
    notifier = _Notifier()
    circuit = LLMCostCircuitBreaker(
        path,
        _config(
            daily_reserved_exposure_limit_usd=1.0,
            daily_cost_limit_usd=1.0,
            max_mode_daily_exposure_pct=50.0,  # $0.50 ceiling for one mode
            max_free_failure_sessions_per_mode=100,
        ),
        notifier,
    )
    _settle_cheap_session(circuit, path, "intra_check-0", "intra_check", 0.30)
    _settle_cheap_session(circuit, path, "intra_check-1", "intra_check", 0.25)

    circuit.activate_session("intra_check-2", "intra_check")
    with pytest.raises(PaidAnalysisSuspended):
        circuit.begin_call(
            agent_name="tech_analyst", model="google/gemini-2.5-flash-lite",
            system_prompt="s", user_message="u", max_output_tokens=100,
        )
    state = circuit.status()
    assert state["trigger_code"] == "mode_daily_spend_limit"
    assert state["current_daily_cost_usd"] == pytest.approx(0.55)


def test_mode_daily_spend_limit_does_not_block_a_different_mode(tmp_path):
    """The allowance is per-MODE: a different mode on the same day, with
    its own budget untouched, is unaffected by another mode's spend."""
    path = _db_path(tmp_path)
    notifier = _Notifier()
    circuit = LLMCostCircuitBreaker(
        path,
        _config(
            daily_reserved_exposure_limit_usd=1.0,
            daily_cost_limit_usd=1.0,
            max_mode_daily_exposure_pct=50.0,
            max_free_failure_sessions_per_mode=100,
        ),
        notifier,
    )
    _settle_cheap_session(circuit, path, "intra_check-0", "intra_check", 0.45)

    circuit.activate_session("run-morning", "morning")
    reservation = circuit.begin_call(
        agent_name="tech_analyst", model="google/gemini-2.5-flash-lite",
        system_prompt="s", user_message="u", max_output_tokens=100,
    )
    assert reservation.reservation_id


# ============================================================================
# Defect 4.1 (2026-08-29): the backstop's counting query counted every
# session that did ANY work (`logical_calls>0 OR provider_attempts>0`), not
# just a free-failure loop -- `logical_calls` is set the instant a
# reservation is admitted, before any provider attempt, and is never
# cleared on failure, so it is >0 for every session that ever placed a
# reservation, paid or not. A normal trading day burned the backstop down
# on its own, and raising the number (2 -> 8 -> 40) only masked that. Fixed
# to count only sessions with a provider attempt that settled at zero cost,
# and changed from a mode-day latch to a bounded, self-healing cooling-off
# window (see the module-level NOTE on "session_retry_limit" in
# src/cost_circuit.py).
# ============================================================================

def test_healthy_day_many_paid_sessions_never_trip_the_backstop(tmp_path):
    """A normal trading day: successful, money-spending sessions must never
    consume backstop budget, no matter how many of them there are. N=20 is
    well above the cap of 8 -- under the OLD counting rule this would
    definitely have latched the mode for the rest of the day, exactly like
    2026-08-28."""
    path = _db_path(tmp_path)
    notifier = _Notifier()
    circuit = LLMCostCircuitBreaker(
        path, _config(max_free_failure_sessions_per_mode=8), notifier,
    )
    for i in range(20):
        _settle_cheap_session(circuit, path, f"intra_check-{i}", "intra_check", 0.01)

    circuit.activate_session("intra_check-20", "intra_check")
    reservation = circuit.begin_call(
        agent_name="tech_analyst", model="google/gemini-2.5-flash-lite",
        system_prompt="s", user_message="u", max_output_tokens=100,
    )
    assert reservation.reservation_id
    assert circuit.status()["suspended"] is False
    assert notifier.messages == []


def test_genuine_free_failure_loop_trips_the_backstop_at_the_cap(tmp_path):
    """The counterpart to the healthy-day test above: sessions that made a
    provider attempt and settled at exactly zero cost DO count, and the 9th
    such session in one mode/day is blocked with `session_retry_limit`."""
    path = _db_path(tmp_path)
    notifier = _Notifier()
    circuit = LLMCostCircuitBreaker(
        path, _config(max_free_failure_sessions_per_mode=8), notifier,
    )
    for i in range(8):
        _settle_cheap_session(circuit, path, f"intra_check-{i}", "intra_check", 0.0)

    circuit.activate_session("intra_check-8", "intra_check")
    with pytest.raises(PaidAnalysisSuspended) as excinfo:
        circuit.begin_call(
            agent_name="tech_analyst", model="google/gemini-2.5-flash-lite",
            system_prompt="s", user_message="u", max_output_tokens=100,
        )
    assert excinfo.value.state["trigger_code"] == "session_retry_limit"


def test_mixed_day_only_free_failures_count_toward_the_backstop(tmp_path):
    """Free failures interleaved with paid successes in the same mode/day:
    only the free-failure sessions count. 8 paid successes contribute
    nothing; the 8 free failures interleaved with them still trip the cap
    of 8 on the 9th free failure."""
    path = _db_path(tmp_path)
    notifier = _Notifier()
    circuit = LLMCostCircuitBreaker(
        path, _config(max_free_failure_sessions_per_mode=8), notifier,
    )
    for i in range(8):
        _settle_cheap_session(circuit, path, f"paid-{i}", "intra_check", 0.02)
        _settle_cheap_session(circuit, path, f"free-{i}", "intra_check", 0.0)

    circuit.activate_session("intra_check-next", "intra_check")
    with pytest.raises(PaidAnalysisSuspended) as excinfo:
        circuit.begin_call(
            agent_name="tech_analyst", model="google/gemini-2.5-flash-lite",
            system_prompt="s", user_message="u", max_output_tokens=100,
        )
    assert excinfo.value.state["trigger_code"] == "session_retry_limit"
    assert excinfo.value.state["daily_cost_usd"] == pytest.approx(0.16)


def test_backstop_cools_off_after_the_configured_window(tmp_path):
    """After a backstop trip, a call inside the cooling-off window is still
    blocked; a call after `backstop_cooloff_minutes` have elapsed is
    admitted again -- and the dollar ceilings, untouched by this change,
    still fire normally afterward."""
    path = _db_path(tmp_path)
    notifier = _Notifier()
    circuit = LLMCostCircuitBreaker(
        path,
        _config(
            max_free_failure_sessions_per_mode=3,
            backstop_cooloff_minutes=10,
            daily_cost_limit_usd=10.0,
            daily_reserved_exposure_limit_usd=10.0,
            session_cost_limit_usd=10.0,
            session_reserved_exposure_limit_usd=10.0,
        ),
        notifier,
    )
    for i in range(3):
        _settle_cheap_session(circuit, path, f"intra_check-{i}", "intra_check", 0.0)

    circuit.activate_session("intra_check-3", "intra_check")
    with pytest.raises(PaidAnalysisSuspended) as excinfo:
        circuit.begin_call(
            agent_name="tech_analyst", model="google/gemini-2.5-flash-lite",
            system_prompt="s", user_message="u", max_output_tokens=100,
        )
    assert excinfo.value.state["trigger_code"] == "session_retry_limit"

    # Still inside the 10-minute window (no time has passed): still blocked.
    circuit.activate_session("intra_check-4", "intra_check")
    with pytest.raises(PaidAnalysisSuspended) as excinfo:
        circuit.begin_call(
            agent_name="tech_analyst", model="google/gemini-2.5-flash-lite",
            system_prompt="s", user_message="u", max_output_tokens=100,
        )
    assert excinfo.value.state["trigger_code"] == "session_retry_limit"

    # Simulate the window elapsing: backdate the free-failure sessions'
    # last activity past backstop_cooloff_minutes, the same technique
    # _seed_agent_log_history above uses to backdate agent_logs rows.
    stale = (datetime.now(timezone.utc) - timedelta(minutes=11)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    with sqlite3.connect(path) as conn:
        conn.execute(
            "UPDATE llm_budget_sessions SET updated_at=? WHERE mode='intra_check' "
            "AND run_id IN ('intra_check-0', 'intra_check-1', 'intra_check-2')",
            (stale,),
        )
        conn.commit()

    circuit.activate_session("intra_check-5", "intra_check")
    reservation = circuit.begin_call(
        agent_name="tech_analyst", model="google/gemini-2.5-flash-lite",
        system_prompt="s", user_message="u", max_output_tokens=100,
    )
    assert reservation.reservation_id
    circuit.before_provider_attempt(reservation, model=reservation.model)
    circuit.complete_call(reservation, 0.0, actual_model=reservation.model)

    # Dollar ceilings are untouched by any of this: settle real spend right
    # up to daily_cost_limit_usd and confirm the next call still trips a
    # genuine dollar-based limit, exactly as before this change.
    _settle_cheap_session(circuit, path, "intra_check-6", "intra_check", 10.0)
    circuit.activate_session("intra_check-7", "intra_check")
    with pytest.raises(PaidAnalysisSuspended) as excinfo:
        circuit.begin_call(
            agent_name="tech_analyst", model="google/gemini-2.5-flash-lite",
            system_prompt="s", user_message="u", max_output_tokens=100,
        )
    assert excinfo.value.state["trigger_code"] == "daily_cost_limit"


def test_mode_daily_spend_limit_still_latches_for_the_day_unaffected_by_cooloff(tmp_path):
    """Proof the cooling-off change is scoped to session_retry_limit only:
    a genuine dollar trip (mode_daily_spend_limit here) still latches
    mode-day exactly as before, and does NOT recover just because time
    passes the way the backstop now deliberately does -- only an ET-day
    rollover clears it."""
    path = _db_path(tmp_path)
    notifier = _Notifier()
    circuit = LLMCostCircuitBreaker(
        path,
        _config(
            daily_reserved_exposure_limit_usd=1.0,
            daily_cost_limit_usd=1.0,
            max_mode_daily_exposure_pct=50.0,
            max_free_failure_sessions_per_mode=100,
            backstop_cooloff_minutes=5,
        ),
        notifier,
    )
    _settle_cheap_session(circuit, path, "intra_check-0", "intra_check", 0.30)
    _settle_cheap_session(circuit, path, "intra_check-1", "intra_check", 0.25)

    circuit.activate_session("intra_check-2", "intra_check")
    with pytest.raises(PaidAnalysisSuspended) as excinfo:
        circuit.begin_call(
            agent_name="tech_analyst", model="google/gemini-2.5-flash-lite",
            system_prompt="s", user_message="u", max_output_tokens=100,
        )
    assert excinfo.value.state["trigger_code"] == "mode_daily_spend_limit"
    assert circuit.status()["hold_scope"] == "mode_day"

    # Fast-forward well past even a short backstop cooloff -- a persisted
    # mode_day hold must not release on a clock, only on day rollover.
    with sqlite3.connect(path) as conn:
        conn.execute(
            "UPDATE llm_quota_holds SET created_at=? WHERE active=1",
            (
                (datetime.now(timezone.utc) - timedelta(hours=6)).strftime(
                    "%Y-%m-%d %H:%M:%S"
                ),
            ),
        )
        conn.commit()

    circuit.activate_session("intra_check-3", "intra_check")
    with pytest.raises(PaidAnalysisSuspended) as excinfo:
        circuit.begin_call(
            agent_name="tech_analyst", model="google/gemini-2.5-flash-lite",
            system_prompt="s", user_message="u", max_output_tokens=100,
        )
    assert excinfo.value.state["trigger_code"] == "mode_daily_spend_limit"


def test_morning_spend_ceiling_pure_computation():
    """Direct unit coverage of the time-gated ceiling itself: active before
    the release hour, released at/after it, and disabled at pct=0."""
    path_cfg = _config(
        afternoon_reserve_pct=40.0, afternoon_reserve_release_et_hour=12,
        daily_reserved_exposure_limit_usd=2.0, daily_cost_limit_usd=2.0,
    )
    circuit = LLMCostCircuitBreaker.__new__(LLMCostCircuitBreaker)
    circuit.config = path_cfg

    before_release = datetime(2026, 8, 28, 10, 0, tzinfo=_ET)
    at_release = datetime(2026, 8, 28, 12, 0, tzinfo=_ET)
    after_release = datetime(2026, 8, 28, 15, 0, tzinfo=_ET)
    assert circuit._morning_spend_ceiling(before_release) == pytest.approx(1.2)
    assert circuit._morning_spend_ceiling(at_release) is None
    assert circuit._morning_spend_ceiling(after_release) is None

    circuit.config = _config(afternoon_reserve_pct=0.0)
    assert circuit._morning_spend_ceiling(before_release) is None


def test_afternoon_reserve_blocks_morning_spend_above_the_ceiling(tmp_path):
    path = _db_path(tmp_path)
    notifier = _Notifier()
    circuit = LLMCostCircuitBreaker(
        path,
        _config(
            daily_reserved_exposure_limit_usd=1.0,
            daily_cost_limit_usd=1.0,
            afternoon_reserve_pct=40.0,
            afternoon_reserve_release_et_hour=12,
        ),
        notifier,
    )
    circuit.activate_session("run-morning", "morning")
    with sqlite3.connect(path) as conn:
        conn.execute(
            "UPDATE llm_budget_sessions SET actual_cost_usd=0.65 "
            "WHERE run_id='run-morning'"
        )
        conn.execute("UPDATE llm_budget_days SET incremental_cost_usd=0.65")

    with patch.object(LLMCostCircuitBreaker, "_morning_spend_ceiling", return_value=0.60):
        with pytest.raises(PaidAnalysisSuspended) as excinfo:
            circuit.begin_call(
                agent_name="tech_analyst", model="google/gemini-2.5-flash-lite",
                system_prompt="s", user_message="u", max_output_tokens=100,
            )
    assert excinfo.value.state["trigger_code"] == "morning_spend_ceiling"
    with sqlite3.connect(path) as conn:
        event = conn.execute(
            "SELECT trigger_code FROM llm_circuit_events WHERE event_type='quota_held' "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()
    assert event[0] == "morning_spend_ceiling"


def test_afternoon_reserve_recovers_the_same_day_without_a_rollover(tmp_path):
    """The defining behavioural difference from every other quota hold in
    this file: this one must stop blocking once the clock crosses the
    release hour on the SAME ET day -- not at the next day's rollover."""
    path = _db_path(tmp_path)
    notifier = _Notifier()
    circuit = LLMCostCircuitBreaker(
        path,
        _config(
            daily_reserved_exposure_limit_usd=1.0,
            daily_cost_limit_usd=1.0,
            afternoon_reserve_pct=40.0,
            afternoon_reserve_release_et_hour=12,
        ),
        notifier,
    )
    circuit.activate_session("run-morning", "morning")
    with sqlite3.connect(path) as conn:
        conn.execute(
            "UPDATE llm_budget_sessions SET actual_cost_usd=0.65 "
            "WHERE run_id='run-morning'"
        )
        conn.execute("UPDATE llm_budget_days SET incremental_cost_usd=0.65")

    with patch.object(LLMCostCircuitBreaker, "_morning_spend_ceiling", return_value=0.60):
        with pytest.raises(PaidAnalysisSuspended):
            circuit.begin_call(
                agent_name="tech_analyst", model="google/gemini-2.5-flash-lite",
                system_prompt="s", user_message="u", max_output_tokens=100,
            )

    # No sticky hold: nothing in this call chain persisted a suspension.
    assert circuit.status()["suspended"] is False

    # Simulate the clock crossing the release hour -- same ET day, no
    # rollover -- and confirm the identical session can now proceed.
    with patch.object(LLMCostCircuitBreaker, "_morning_spend_ceiling", return_value=None):
        reservation = circuit.begin_call(
            agent_name="tech_analyst", model="google/gemini-2.5-flash-lite",
            system_prompt="s", user_message="u", max_output_tokens=100,
        )
    assert reservation.reservation_id
    assert notifier.messages == []


# ============================================================================
# Defect 1 (2026-08-28): the pre-fix reservation treated one UTF-8 byte as
# one token and always reserved the full max_output_tokens ceiling. The
# 09:32 ET portfolio_manager call reserved $1.8657 for a call that actually
# cost ~$0.11. Reservation is now derived from this agent+model's own
# measured history in agent_logs (LLMCostCircuitBreaker.
# _measure_reservation_tokens), with an explicit, tested fallback to
# exactly the old formula for thin/unknown/unreadable history.
# ============================================================================

# 24 real production portfolio_manager / openai/gpt-5.5 calls, 2026-08-25
# through 2026-08-28, read from the production ledger snapshot:
# (bytes of the logged user_message, actual input_tokens, actual
# output_tokens). Matches the spec's headline figures exactly: worst-ever
# actual cost $0.578255, max output_tokens 11034 (never near a 16,000
# reservation).
_REAL_PM_GPT55_HISTORY = [
    (11132, 13827, 3480), (185259, 50345, 9160), (10933, 13803, 5363),
    (181159, 49447, 11034), (11163, 13885, 5587), (185793, 50270, 8432),
    (11628, 13980, 6601), (174419, 48139, 8436), (12309, 14272, 4653),
    (183283, 50027, 8096), (11814, 14098, 6501), (183182, 50396, 7018),
    (13659, 14653, 3726), (13194, 14534, 4072), (189705, 51904, 8760),
    (17547, 16277, 5159), (16709, 16073, 3614), (34066, 20875, 3989),
    (32620, 20498, 4579), (32072, 20317, 3395), (30453, 19955, 4669),
    (29954, 19893, 4945), (28469, 19480, 3688), (29604, 19781, 3197),
]
_REAL_PM_GPT55_WORST_COST = 0.578255
_REAL_PM_GPT55_MAX_OUTPUT = 11034

# Mirrors the 09:32 ET call's inferred size (spec: "~259 KB prompt bounded
# as 259k tokens" under the old byte=token formula). Plain ASCII so
# len(str) == UTF-8 byte length exactly, keeping the arithmetic in these
# tests exact rather than approximate.
_INCIDENT_SYSTEM_PROMPT = "S" * 48_000
_INCIDENT_USER_MESSAGE = "U" * 210_744  # + system + 256 == 259,000 bytes
_INCIDENT_TOTAL_PROMPT_BYTES = 259_000


def _seed_agent_log_history(path, *, agent_name, model, rows):
    # Backdated well outside "today"'s UTC window: _seed_today() auto-
    # creates a legacy llm_budget_sessions row (logical_calls=1) for every
    # DISTINCT run_id it finds in agent_logs within TODAY's window, which
    # would otherwise make each of these synthetic history rows count as
    # an already-paid session today and trip the session-count backstop
    # before the reservation logic under test is even reached. Real
    # history legitimately spans many prior days anyway.
    past = (datetime.now(timezone.utc) - timedelta(days=3)).strftime("%Y-%m-%d %H:%M:%S")
    with sqlite3.connect(path) as conn:
        for i, (msg_bytes, input_tokens, output_tokens) in enumerate(rows):
            conn.execute(
                "INSERT INTO agent_logs (agent_name, run_id, input_message, "
                "model, input_tokens, output_tokens, cost_usd, timestamp) VALUES "
                "(?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    agent_name, f"history-{i}", "x" * msg_bytes, model,
                    input_tokens, output_tokens, 0.01, past,
                ),
            )
        conn.commit()


def _loose_config(**overrides):
    """A config with exposure ceilings wide enough that only the specific
    check each test is exercising can fire."""
    values = dict(
        session_cost_limit_usd=10.0, daily_cost_limit_usd=10.0,
        session_reserved_exposure_limit_usd=10.0,
        daily_reserved_exposure_limit_usd=10.0,
    )
    values.update(overrides)
    return _config(**values)


def _old_formula_reserve(total_prompt_bytes: int, max_output_tokens: int,
                          *, in_rate=5.0, out_rate=30.0, multiplier=1.05) -> float:
    """The exact pre-fix formula (byte=token, full output ceiling), for
    comparison. openai/gpt-5.5 rates ($5/$30 per 1M) per src/cost_table.py."""
    return multiplier * (
        total_prompt_bytes * in_rate / 1_000_000
        + max_output_tokens * out_rate / 1_000_000
    )


def test_reservation_from_real_measured_history_sits_between_worst_and_old(tmp_path):
    """The required proof: given the REAL measured distribution, the new
    reservation for a call the size of the 09:32 ET incident sits above
    the worst actual cost ever recorded for this agent+model, but far
    below what the old formula would have reserved for the same prompt."""
    path = _db_path(tmp_path)
    _seed_agent_log_history(
        path, agent_name="portfolio_manager", model="openai/gpt-5.5",
        rows=_REAL_PM_GPT55_HISTORY,
    )
    circuit = LLMCostCircuitBreaker(path, _loose_config(), _Notifier())
    circuit.activate_session("run-real-history", "morning")

    reservation = circuit.begin_call(
        agent_name="portfolio_manager", model="openai/gpt-5.5",
        system_prompt=_INCIDENT_SYSTEM_PROMPT, user_message=_INCIDENT_USER_MESSAGE,
        max_output_tokens=16_000,
    )
    with sqlite3.connect(path) as conn:
        reserved = conn.execute(
            "SELECT reserved_cost_usd FROM llm_budget_reservations "
            "WHERE reservation_id=?", (reservation.reservation_id,),
        ).fetchone()[0]

    old_reserve = _old_formula_reserve(_INCIDENT_TOTAL_PROMPT_BYTES, 16_000)
    assert old_reserve == pytest.approx(1.86375, abs=1e-3)  # sanity: ~= spec's $1.8657

    assert reserved > _REAL_PM_GPT55_WORST_COST
    assert reserved < old_reserve
    # Comfortably below, not just technically below (spec: "far below").
    assert reserved < old_reserve * 0.6
    # Also reserves less output than the ceiling -- driven by the real max
    # observed output (11034), not by max_output_tokens.
    assert reservation.max_output_tokens < 16_000
    assert reservation.max_output_tokens >= _REAL_PM_GPT55_MAX_OUTPUT


def test_regression_2026_08_28_0932_scenario_no_longer_blocks(tmp_path):
    """The actual incident, reproduced: session spend $0.0461, day spend
    $0.0476, a portfolio_manager call the size of the one that tripped
    projected_session_cost_limit at 09:32 ET. Under production exposure
    ceilings (session $2.60, daily $5.50) and real measured history, it
    must not trip anything."""
    path = _db_path(tmp_path)
    _seed_agent_log_history(
        path, agent_name="portfolio_manager", model="openai/gpt-5.5",
        rows=_REAL_PM_GPT55_HISTORY,
    )
    notifier = _Notifier()
    circuit = LLMCostCircuitBreaker(
        path,
        _config(
            session_cost_limit_usd=0.90, daily_cost_limit_usd=2.75,
            session_reserved_exposure_limit_usd=2.60,
            daily_reserved_exposure_limit_usd=5.50,
        ),
        notifier,
    )
    circuit.activate_session("run-be9f8f06", "morning")
    # Day spend ($0.0476) was this session ($0.0461) plus a separate
    # already-settled call earlier that day; the day/session ledgers must
    # agree exactly (_validate_accounting_invariants) or _seed_today fails
    # closed on the fixture itself before the reservation logic under test
    # is even reached.
    circuit.activate_session("intra_check-earlier-am", "intra_check")
    with sqlite3.connect(path) as conn:
        conn.execute(
            "UPDATE llm_budget_sessions SET actual_cost_usd=0.0461 "
            "WHERE run_id='run-be9f8f06'"
        )
        conn.execute(
            "UPDATE llm_budget_sessions SET actual_cost_usd=0.0015 "
            "WHERE run_id='intra_check-earlier-am'"
        )
        conn.execute("UPDATE llm_budget_days SET incremental_cost_usd=0.0476")
    circuit.activate_session("run-be9f8f06", "morning")

    reservation = circuit.begin_call(
        agent_name="portfolio_manager", model="openai/gpt-5.5",
        system_prompt=_INCIDENT_SYSTEM_PROMPT, user_message=_INCIDENT_USER_MESSAGE,
        max_output_tokens=16_000,
    )

    assert reservation.reservation_id
    state = circuit.status()
    assert state["suspended"] is False
    assert notifier.messages == []


def test_thin_history_falls_back_to_old_worst_case_formula(tmp_path):
    """Fewer rows than reservation_min_history_samples (default 20) for
    this exact agent+model -- must not guess from a handful of calls."""
    path = _db_path(tmp_path)
    _seed_agent_log_history(
        path, agent_name="portfolio_manager", model="openai/gpt-5.5",
        rows=_REAL_PM_GPT55_HISTORY[:5],
    )
    circuit = LLMCostCircuitBreaker(path, _loose_config(), _Notifier())
    circuit.activate_session("run-thin-history", "morning")

    reservation = circuit.begin_call(
        agent_name="portfolio_manager", model="openai/gpt-5.5",
        system_prompt=_INCIDENT_SYSTEM_PROMPT, user_message=_INCIDENT_USER_MESSAGE,
        max_output_tokens=16_000,
    )

    assert reservation.input_tokens_estimate == _INCIDENT_TOTAL_PROMPT_BYTES
    assert reservation.max_output_tokens == 16_000


def test_unknown_agent_falls_back_to_old_worst_case_formula(tmp_path):
    """A brand-new agent_name with zero rows in agent_logs at all."""
    path = _db_path(tmp_path)
    _seed_agent_log_history(
        path, agent_name="portfolio_manager", model="openai/gpt-5.5",
        rows=_REAL_PM_GPT55_HISTORY,
    )
    circuit = LLMCostCircuitBreaker(path, _loose_config(), _Notifier())
    circuit.activate_session("run-unknown-agent", "morning")

    reservation = circuit.begin_call(
        agent_name="brand_new_agent_seat", model="openai/gpt-5.5",
        system_prompt=_INCIDENT_SYSTEM_PROMPT, user_message=_INCIDENT_USER_MESSAGE,
        max_output_tokens=16_000,
    )

    assert reservation.input_tokens_estimate == _INCIDENT_TOTAL_PROMPT_BYTES
    assert reservation.max_output_tokens == 16_000


def test_unknown_model_price_still_trips_regardless_of_history(tmp_path):
    """A model this history exists for but that has no pinned price must
    still trip unknown_model_price -- history changes what is RESERVED,
    never whether an unpriceable model is allowed to proceed."""
    path = _db_path(tmp_path)
    _seed_agent_log_history(
        path, agent_name="portfolio_manager", model="not-a-real/model",
        rows=_REAL_PM_GPT55_HISTORY,
    )
    circuit = LLMCostCircuitBreaker(path, _loose_config(), _Notifier())
    circuit.activate_session("run-unknown-model", "morning")

    with pytest.raises(PaidAnalysisSuspended):
        circuit.begin_call(
            agent_name="portfolio_manager", model="not-a-real/model",
            system_prompt="s", user_message="u", max_output_tokens=100,
        )
    assert circuit.status()["trigger_code"] == "unknown_model_price"


def test_corrupt_history_schema_falls_back_to_old_worst_case_formula(tmp_path):
    """agent_logs exists but is missing the columns this measurement reads
    (a real production shape drift, not merely empty/thin data). seed_today
    only reads run_id/cost_usd/timestamp, so it still succeeds; only the
    measurement itself must fail closed."""
    path = _db_path(tmp_path)
    with sqlite3.connect(path) as conn:
        conn.execute("DROP TABLE agent_logs")
        conn.execute(
            "CREATE TABLE agent_logs (id INTEGER PRIMARY KEY, agent_name TEXT, "
            "run_id TEXT, model TEXT, cost_usd REAL, "
            "timestamp TEXT NOT NULL DEFAULT (datetime('now')))"
        )
        conn.commit()
    circuit = LLMCostCircuitBreaker(path, _loose_config(), _Notifier())
    circuit.activate_session("run-corrupt-schema", "morning")

    reservation = circuit.begin_call(
        agent_name="portfolio_manager", model="openai/gpt-5.5",
        system_prompt=_INCIDENT_SYSTEM_PROMPT, user_message=_INCIDENT_USER_MESSAGE,
        max_output_tokens=16_000,
    )

    assert reservation.input_tokens_estimate == _INCIDENT_TOTAL_PROMPT_BYTES
    assert reservation.max_output_tokens == 16_000


def test_error_reading_history_falls_back_to_old_worst_case_formula(tmp_path, monkeypatch):
    """A transient DB-level failure reading agent_logs (lock contention,
    I/O error, ...) -- distinct from a structurally corrupt table above --
    must fail the SAME way: to the conservative fallback, not to circuit
    unavailability and not to a cheaper guess."""
    path = _db_path(tmp_path)
    _seed_agent_log_history(
        path, agent_name="portfolio_manager", model="openai/gpt-5.5",
        rows=_REAL_PM_GPT55_HISTORY,
    )
    circuit = LLMCostCircuitBreaker(path, _loose_config(), _Notifier())
    circuit.activate_session("run-read-error", "morning")

    # sqlite3.Connection is a C-implemented, immutable type -- its methods
    # can't be monkeypatched directly. Wrap the connection _connect()
    # returns instead, so exactly one query (the history measurement's,
    # uniquely identified by its `msg_bytes` alias) fails while everything
    # else -- seeding, accounting, the reservation insert -- goes through
    # the real connection untouched.
    real_connect = circuit._connect

    class _FlakyConn:
        def __init__(self, real_conn):
            self._real = real_conn

        def execute(self, sql, *args, **kwargs):
            if "msg_bytes" in sql:
                raise sqlite3.OperationalError("disk I/O error")
            return self._real.execute(sql, *args, **kwargs)

        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            return self._real.__exit__(*exc_info)

        def __getattr__(self, name):
            return getattr(self._real, name)

    monkeypatch.setattr(circuit, "_connect", lambda: _FlakyConn(real_connect()))

    reservation = circuit.begin_call(
        agent_name="portfolio_manager", model="openai/gpt-5.5",
        system_prompt=_INCIDENT_SYSTEM_PROMPT, user_message=_INCIDENT_USER_MESSAGE,
        max_output_tokens=16_000,
    )

    assert reservation.input_tokens_estimate == _INCIDENT_TOTAL_PROMPT_BYTES
    assert reservation.max_output_tokens == 16_000


def test_reservation_conservative_percentile_config_rejects_non_low_values():
    """A structural guard against defeating the fix by configuration: the
    percentile must stay below the median (the whole point of "conservative
    low percentile" -- see reservation_conservative_percentile's docstring
    in src/config.py)."""
    from src.config import LLMCostCircuitConfig

    with pytest.raises(Exception):
        LLMCostCircuitConfig(reservation_conservative_percentile=0.75)


def test_percentile_helper_matches_linear_interpolation():
    from src.cost_circuit import _percentile

    assert _percentile([1.0, 2.0, 3.0, 4.0], 0.0) == 1.0
    assert _percentile([1.0, 2.0, 3.0, 4.0], 1.0) == 4.0
    assert _percentile([1.0, 2.0, 3.0, 4.0], 0.5) == pytest.approx(2.5)
    assert _percentile([5.0], 0.1) == 5.0


def test_daily_reservation_is_atomic_across_process_objects(tmp_path):
    path = _db_path(tmp_path)
    cfg = _config(
        session_cost_limit_usd=0.0007,
        daily_cost_limit_usd=0.0007,
        max_free_failure_sessions_per_mode=10,
    )
    first = LLMCostCircuitBreaker(path, cfg, _Notifier())
    second = LLMCostCircuitBreaker(path, cfg, _Notifier())
    barrier = threading.Barrier(2)
    outcomes: list[str] = []

    def reserve(circuit, run_id, mode, agent_name):
        # ContextVars intentionally do not inherit into raw threads. Each
        # independent worker/process activates its own paid session context.
        circuit.activate_session(run_id, mode)
        barrier.wait()
        try:
            circuit.begin_call(
                agent_name=agent_name,
                model="google/gemini-2.5-flash-lite",
                system_prompt="s",
                user_message="u",
                max_output_tokens=1_000,
            )
            outcomes.append("reserved")
        except PaidAnalysisSuspended:
            outcomes.append("blocked")

    threads = [
        threading.Thread(target=reserve, args=(first, "run-a", "morning", "a")),
        threading.Thread(target=reserve, args=(second, "run-b", "midday", "b")),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(outcomes) == ["blocked", "reserved"]
    with sqlite3.connect(path) as conn:
        active = conn.execute(
            "SELECT COUNT(*) FROM llm_budget_reservations WHERE status='active'"
        ).fetchone()[0]
    assert active == 1
    assert first.status()["suspended"] is True


def test_third_paid_session_in_same_mode_is_not_blocked_by_backstop(tmp_path):
    """Pre-4.1 this blocked the 3rd session purely on count, even though
    every session so far had settled real, positive cost -- the exact
    counting defect Defect 4.1 fixes. A session that spent money is the
    per-mode dollar allowance's problem (see the mode_daily_spend_limit
    tests above), never this backstop's, no matter how low the count cap
    is configured."""
    path = _db_path(tmp_path)
    notifier = _Notifier()
    circuit = LLMCostCircuitBreaker(
        path, _config(max_free_failure_sessions_per_mode=2), notifier,
    )
    for run_id in ("run-one", "run-two"):
        circuit.activate_session(run_id, "morning")
        reservation = circuit.begin_call(
            agent_name="tech_analyst",
            model="google/gemini-2.5-flash-lite",
            system_prompt="s", user_message="u", max_output_tokens=100,
        )
        circuit.before_provider_attempt(reservation, model=reservation.model)
        circuit.complete_call(reservation, 0.001, actual_model=reservation.model)

    circuit.activate_session("run-three", "morning")
    reservation = circuit.begin_call(
        agent_name="tech_analyst",
        model="google/gemini-2.5-flash-lite",
        system_prompt="s", user_message="u", max_output_tokens=100,
    )
    assert reservation.reservation_id
    assert circuit.status()["suspended"] is False
    assert notifier.messages == []
    # No mode-scoping or require_paid_analysis assertions here any more:
    # pre-4.1 this test also proved the trip was visible cross-call via
    # `require_paid_analysis`/`status()` because it was a persisted
    # mode_day quota hold. Post-4.1 the backstop is deliberately as
    # non-sticky as the afternoon reserve (see the module-level NOTE on
    # "session_retry_limit"): it only ever fires inside `begin_call` itself,
    # never via `status()`/`require_paid_analysis()` -- see the cooling-off
    # tests below for the trigger's real (bounded, self-healing) lifetime.


def test_quota_hold_cannot_authorize_an_old_reservation_above_settled_cap(tmp_path):
    path = _db_path(tmp_path)
    circuit = LLMCostCircuitBreaker(
        path,
        _config(
            session_cost_limit_usd=0.50,
            daily_cost_limit_usd=2.0,
            session_reserved_exposure_limit_usd=2.0,
            daily_reserved_exposure_limit_usd=3.0,
        ),
        _Notifier(),
    )
    circuit.activate_session("run-reset-race", "morning")
    first = circuit.begin_call(
        agent_name="first", model="google/gemini-2.5-flash-lite",
        system_prompt="s", user_message="u", max_output_tokens=100,
    )
    waiting = circuit.begin_call(
        agent_name="waiting", model="google/gemini-2.5-flash-lite",
        system_prompt="s", user_message="u", max_output_tokens=100,
    )
    circuit.before_provider_attempt(first, model=first.model)
    circuit.complete_call(first, 0.60, actual_model=first.model)
    assert circuit.status()["suspended"] is True

    with pytest.raises(ValueError, match="no operator-resettable hard circuit"):
        circuit.reset("reviewed; quota holds are not operator bypasses")
    with pytest.raises(PaidAnalysisSuspended):
        circuit.before_provider_attempt(waiting, model=waiting.model)

    state = circuit.status()
    assert state["trigger_code"] == "session_cost_limit"
    assert state["provider_attempts"] == 1


def test_provider_boundary_revalidates_ledger_after_reservation(tmp_path):
    path = _db_path(tmp_path)
    circuit = LLMCostCircuitBreaker(
        path,
        _config(
            session_cost_limit_usd=2.0,
            daily_cost_limit_usd=1.0,
            session_reserved_exposure_limit_usd=3.0,
            daily_reserved_exposure_limit_usd=3.0,
        ),
        _Notifier(),
    )
    circuit.activate_session("run-waiting", "morning")
    waiting = circuit.begin_call(
        agent_name="waiting", model="google/gemini-2.5-flash-lite",
        system_prompt="s", user_message="u", max_output_tokens=100,
    )

    circuit.activate_session("run-settled", "midday")
    settled = circuit.begin_call(
        agent_name="settled", model="google/gemini-2.5-flash-lite",
        system_prompt="s", user_message="u", max_output_tokens=100,
    )
    circuit.before_provider_attempt(settled, model=settled.model)
    circuit.complete_call(settled, 0.10, actual_model=settled.model)

    # Simulate damage after the waiting call reserved but before it reaches
    # the provider semaphore.  The day and session ledgers now disagree.
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE llm_budget_days SET incremental_cost_usd=0")

    with pytest.raises(
        RuntimeError,
        match="day/session settled-cost ledgers disagree",
    ):
        circuit.before_provider_attempt(waiting, model=waiting.model)
    with sqlite3.connect(path) as conn:
        attempt_count = conn.execute(
            "SELECT attempt_count FROM llm_budget_reservations "
            "WHERE reservation_id=?", (waiting.reservation_id,),
        ).fetchone()[0]
    assert attempt_count == 0


def test_durable_emergency_latch_survives_new_process_and_preserves_trigger(tmp_path):
    path = _db_path(tmp_path)
    cfg = _config()
    first_notifier = _Notifier()
    first = LLMCostCircuitBreaker(path, cfg, first_notifier)
    first.activate_session("run-original", "morning")
    first.mark_unavailable(
        RuntimeError("simulated accounting I/O failure"),
        run_id="run-original", mode="morning", agent_name="portfolio_manager",
        attempts=1,
    )

    marker = Path(f"{path}.llm-circuit-unavailable")
    assert marker.exists()
    original_payload = json.loads(marker.read_text())
    assert original_payload["run_id"] == "run-original"

    second = LLMCostCircuitBreaker(path, cfg, _Notifier())
    later_state = second.activate_session("run-later", "midday")
    assert later_state["run_id"] == "run-original"
    assert later_state["current_run_id"] == "run-later"
    with pytest.raises(PaidAnalysisSuspended):
        second.begin_call(
            agent_name="position_reviewer",
            model="google/gemini-2.5-flash-lite",
            system_prompt="s", user_message="u", max_output_tokens=100,
        )
    second.mark_unavailable(
        RuntimeError("later worker error"), run_id="run-later", mode="midday",
    )
    assert json.loads(marker.read_text())["run_id"] == "run-original"

    second.reset("accounting infrastructure repaired")
    assert not marker.exists()


def test_external_emergency_latch_blocks_inflight_response_at_completion(tmp_path):
    path = _db_path(tmp_path)
    cfg = _config()
    worker = LLMCostCircuitBreaker(path, cfg, _Notifier())
    worker.activate_session("run-inflight", "morning")
    reservation = worker.begin_call(
        agent_name="portfolio_manager", model="google/gemini-2.5-flash-lite",
        system_prompt="s", user_message="u", max_output_tokens=100,
    )
    worker.before_provider_attempt(reservation, model=reservation.model)

    other = LLMCostCircuitBreaker(path, cfg, _Notifier())
    other.mark_unavailable(
        RuntimeError("other process lost accounting"),
        run_id="run-other", mode="midday", attempts=0,
    )
    with pytest.raises(PaidAnalysisSuspended):
        worker.complete_call(reservation, 0.001, actual_model=reservation.model)

    with sqlite3.connect(path) as conn:
        row = conn.execute(
            "SELECT status, reserved_cost_usd FROM llm_budget_reservations "
            "WHERE reservation_id=?", (reservation.reservation_id,),
        ).fetchone()
    assert row[0] == "active"
    assert row[1] > 0


def test_emergency_alert_prefers_exact_persisted_attempt_count(tmp_path):
    path = _db_path(tmp_path)
    notifier = _Notifier()
    circuit = LLMCostCircuitBreaker(path, _config(), notifier)
    circuit.activate_session("run-snapshot", "morning")
    reservation = circuit.begin_call(
        agent_name="tech", model="google/gemini-2.5-flash-lite",
        system_prompt="s", user_message="u", max_output_tokens=100,
    )
    circuit.before_provider_attempt(reservation, model=reservation.model)
    circuit.complete_call(reservation, 0.001, actual_model=reservation.model)

    circuit.mark_unavailable(
        RuntimeError("post-call prerequisite failed"),
        run_id="run-snapshot", mode="morning", attempts=0,
    )
    assert "attempts: 1 provider attempt" in notifier.messages[-1]
    assert "at least 0" not in notifier.messages[-1]


def test_malformed_sidecar_metadata_still_constructs_clear_fail_closed_sentinel(tmp_path):
    path = _db_path(tmp_path)
    marker = Path(f"{path}.llm-circuit-unavailable")
    marker.write_text(json.dumps({
        "recorded_at": "not-a-date",
        "error": "simulated marker",
        "run_id": "run-marker",
        "mode": "morning",
        "attempts": {"bad": "shape"},
        "attempts_exact": "yes",
        "session_cost_usd": "not-money",
        "daily_cost_usd": -1,
        "costs_exact": "yes",
    }))
    notifier = _Notifier()
    circuit = LLMCostCircuitBreaker(path, _config(), notifier)

    state = circuit.activate_session("run-later", "midday")
    assert state["suspended"] is True
    assert state["run_id"] == "run-marker"
    assert state["session_attempts"] is None
    assert state["costs_available"] is False
    assert "cost: unavailable" in notifier.messages[0]


def test_two_expired_started_calls_are_both_charged_before_alert_snapshot(tmp_path):
    path = _db_path(tmp_path)
    notifier = _Notifier()
    circuit = LLMCostCircuitBreaker(path, _config(), notifier)
    circuit.activate_session("run-expired", "morning")
    reservations = []
    for agent_name in ("macro", "news"):
        reservation = circuit.begin_call(
            agent_name=agent_name, model="google/gemini-2.5-flash-lite",
            system_prompt="s", user_message="u", max_output_tokens=100,
        )
        circuit.before_provider_attempt(reservation, model=reservation.model)
        reservations.append(reservation)
    with sqlite3.connect(path) as conn:
        conn.execute(
            "UPDATE llm_budget_reservations SET expires_at=datetime('now', '-1 minute')"
        )

    state = circuit.status()
    with sqlite3.connect(path) as conn:
        rows = conn.execute(
            "SELECT status, actual_cost_usd FROM llm_budget_reservations "
            "ORDER BY created_at, reservation_id"
        ).fetchall()
    assert [row[0] for row in rows] == ["expired_attempted", "expired_attempted"]
    total = sum(row[1] for row in rows)
    assert state["daily_cost_usd"] == pytest.approx(total)
    assert state["costs_exact"] == 0
    assert len(notifier.messages) == 1


def test_missing_usage_latches_real_circuit_and_no_result_flows(tmp_path, monkeypatch):
    path = _db_path(tmp_path)
    circuit = LLMCostCircuitBreaker(path, _config(), _Notifier())
    circuit.activate_session("run-no-usage", "morning")
    monkeypatch.setenv("QUANT_AGENT_MAX_RETRIES", "1")

    client = MagicMock()
    chunk = SimpleNamespace(
        choices=[SimpleNamespace(
            delta=SimpleNamespace(content='{"ok": true}'), finish_reason="stop",
        )],
        usage=None,
    )
    client.chat.completions.create.return_value = iter([chunk])
    with patch("openai.OpenAI", return_value=client):
        agent = _Agent(api_key="x", model="gpt-5.5", max_tokens=64)
        agent.set_cost_circuit(circuit)
        # The response is transport-valid, but accounting latches. A later
        # paid boundary is blocked; the current call is conservatively charged.
        result = agent.run()

    assert result.cost_usd is None
    state = circuit.status()
    assert state["trigger_code"] == "unknown_actual_cost"
    assert state["current_session_cost_usd"] > 0
    with pytest.raises(PaidAnalysisSuspended):
        circuit.require_paid_analysis("next_agent")


def test_corrupt_state_row_blocks_before_network_and_writes_durable_latch(
    tmp_path, monkeypatch,
):
    path = _db_path(tmp_path)
    circuit = LLMCostCircuitBreaker(path, _config(), _Notifier())
    circuit.activate_session("run-corrupt-state", "morning")
    with sqlite3.connect(path) as conn:
        conn.execute("DELETE FROM llm_circuit_state WHERE singleton=1")

    monkeypatch.setenv("QUANT_AGENT_MAX_RETRIES", "1")
    client = MagicMock()
    with patch("anthropic.Anthropic", return_value=client):
        agent = _Agent(api_key="x", model="claude-sonnet-4-6", max_tokens=64)
        agent.set_cost_circuit(circuit)
        with pytest.raises(PaidAnalysisSuspended):
            agent.run()

    client.messages.create.assert_not_called()
    assert Path(f"{path}.llm-circuit-unavailable").exists()


@pytest.mark.parametrize("missing", ["day", "session", "reservation"])
def test_accounting_row_deleted_after_authorization_blocks_response(
    tmp_path, monkeypatch, missing,
):
    path = _db_path(tmp_path)
    circuit = LLMCostCircuitBreaker(path, _config(), _Notifier())
    run_id = f"run-missing-{missing}"
    circuit.activate_session(run_id, "morning")
    monkeypatch.setenv("QUANT_AGENT_MAX_RETRIES", "1")

    response = SimpleNamespace(
        content=[SimpleNamespace(text='{"ok": true}')],
        usage=SimpleNamespace(
            input_tokens=10, output_tokens=5,
            cache_creation_input_tokens=0, cache_read_input_tokens=0,
        ),
        stop_reason="end_turn",
    )
    client = MagicMock()

    def answer_then_corrupt(**_kwargs):
        with sqlite3.connect(path) as conn:
            if missing == "day":
                conn.execute("DELETE FROM llm_budget_days")
            else:
                if missing == "session":
                    conn.execute(
                        "DELETE FROM llm_budget_sessions WHERE run_id=?", (run_id,)
                    )
                else:
                    conn.execute(
                        "DELETE FROM llm_budget_reservations WHERE run_id=?", (run_id,)
                    )
        return response

    client.messages.create.side_effect = answer_then_corrupt
    with patch("anthropic.Anthropic", return_value=client):
        agent = _Agent(api_key="x", model="claude-sonnet-4-6", max_tokens=64)
        agent.set_cost_circuit(circuit)
        with pytest.raises(PaidAnalysisSuspended):
            agent.run()

    assert client.messages.create.call_count == 1
    assert Path(f"{path}.llm-circuit-unavailable").exists()


@pytest.mark.parametrize(
    "table",
    [
        "llm_budget_days", "llm_budget_sessions", "llm_budget_reservations",
        "llm_circuit_state", "llm_circuit_events",
    ],
)
def test_partial_breaker_schema_never_recreates_missing_accounting_as_empty(
    tmp_path, table,
):
    path = _db_path(tmp_path)
    with sqlite3.connect(path) as conn:
        conn.execute(f"DROP TABLE {table}")

    circuit = LLMCostCircuitBreaker(path, _config(), _Notifier())
    state = circuit.status()
    assert state["suspended"] is True
    assert state["available"] is False
    assert "schema is partial" in state["trigger_detail"]
    assert Path(f"{path}.llm-circuit-unavailable").exists()


def test_cross_et_day_reservation_is_never_authorized(tmp_path, monkeypatch):
    path = _db_path(tmp_path)
    circuit = LLMCostCircuitBreaker(path, _config(), _Notifier())
    circuit.activate_session("run-midnight", "morning")
    reservation = circuit.begin_call(
        agent_name="tech_analyst", model="google/gemini-2.5-flash-lite",
        system_prompt="s", user_message="u", max_output_tokens=100,
    )
    old_day = circuit.status()["current_day"]
    monkeypatch.setattr(
        "src.cost_circuit._et_day_and_utc_bounds",
        lambda now=None: ("2099-01-02", "2099-01-02 05:00:00", "2099-01-03 04:59:59"),
    )

    with pytest.raises(PaidAnalysisSuspended, match="daily-budget boundary"):
        circuit.before_provider_attempt(reservation, model=reservation.model)
    with sqlite3.connect(path) as conn:
        row = conn.execute(
            "SELECT day, status, attempt_count FROM llm_budget_reservations "
            "WHERE reservation_id=?", (reservation.reservation_id,),
        ).fetchone()
    assert row == (old_day, "expired_cross_day_unattempted", 0)


def test_production_pm_sized_prompt_fits_reserved_exposure_cap(tmp_path):
    circuit = LLMCostCircuitBreaker(
        _db_path(tmp_path),
        _config(
            session_cost_limit_usd=0.90,
            daily_cost_limit_usd=1.50,
            session_reserved_exposure_limit_usd=1.80,
            daily_reserved_exposure_limit_usd=1.90,
        ),
        _Notifier(),
    )
    circuit.activate_session("run-sized-pm", "morning")
    # Current production PM system+user payloads are about 225 KB. Include
    # already-settled specialist spend and prove one normal PM call remains
    # viable under the conservative byte-per-token reservation.
    with sqlite3.connect(circuit.db_path) as conn:
        conn.execute(
            "UPDATE llm_budget_sessions SET actual_cost_usd=0.05 "
            "WHERE run_id='run-sized-pm'"
        )
        conn.execute(
            "UPDATE llm_budget_days SET incremental_cost_usd=0.05"
        )
    reservation = circuit.begin_call(
        agent_name="portfolio_manager", model="openai/gpt-5.5",
        system_prompt="s" * 42_000, user_message="u" * 183_000,
        max_output_tokens=16_000,
    )
    assert reservation.input_tokens_estimate == 225_256
    assert circuit.status()["suspended"] is False


@pytest.mark.parametrize(
    ("code", "scope"),
    [
        ("daily_cost_limit", "day"),
        ("projected_daily_cost_limit", "day"),
        ("provider_projected_daily_cost_limit", "day"),
        ("outstanding_projected_daily_cost_limit", "day"),
        # "session_retry_limit" is deliberately absent here (Defect 4.1,
        # 2026-08-29): like "morning_spend_ceiling", it is never passed to
        # _trip_locked any more -- see the module-level NOTE on
        # "session_retry_limit" by `_MODE_DAY_QUOTA_TRIGGERS` in
        # src/cost_circuit.py.
        ("mode_daily_spend_limit", "mode_day"),
        ("session_cost_limit", "session"),
        ("projected_session_cost_limit", "session"),
        ("provider_projected_session_cost_limit", "session"),
        ("outstanding_projected_session_cost_limit", "session"),
        ("session_retry_attempt_limit", "session"),
        # Defect 5 (2026-08-31): "provider_attempt_limit" moved from "hard"
        # to "session". It bounds attempts within ONE call -- strictly
        # narrower than "session_retry_attempt_limit" directly above -- yet
        # was the only one of the pair on the durable operator-reset latch,
        # so the smaller problem produced the larger response. See the NOTE
        # by _SESSION_QUOTA_TRIGGERS in src/cost_circuit.py.
        ("provider_attempt_limit", "session"),
        ("legacy_unknown_cost", "hard"),
        ("unknown_model_price", "hard"),
        ("unknown_actual_cost", "hard"),
        ("failed_call_unknown_cost", "hard"),
        ("expired_attempted_reservation", "hard"),
        ("cross_day_started_reservation", "hard"),
    ],
)
def test_trigger_scope_classification_is_explicit(code, scope):
    assert _trigger_scope(code) == scope


@pytest.mark.parametrize("code", [None, "", "future_daily_limit", {"bad": "shape"}])
def test_unknown_trigger_scope_fails_closed_hard(code):
    assert _trigger_scope(code) == "hard"


def test_day_quota_hold_recovers_once_on_next_et_day(tmp_path, monkeypatch):
    clock = {
        "value": ("2099-01-01", "2099-01-01 05:00:00", "2099-01-02 04:59:59")
    }
    monkeypatch.setattr(
        "src.cost_circuit._et_day_and_utc_bounds", lambda now=None: clock["value"],
    )
    path = _db_path(tmp_path)
    notifier = _Notifier()
    circuit = LLMCostCircuitBreaker(
        path,
        _config(session_cost_limit_usd=2.0, daily_cost_limit_usd=1.0),
        notifier,
    )
    circuit.activate_session("run-old-day", "morning")
    with sqlite3.connect(path) as conn:
        conn.execute(
            "UPDATE llm_budget_sessions SET actual_cost_usd=1.0 "
            "WHERE run_id='run-old-day'"
        )
        conn.execute(
            "UPDATE llm_budget_days SET incremental_cost_usd=1.0 "
            "WHERE day='2099-01-01'"
        )
    circuit.enforce_current_limits("test_limit")
    assert circuit.status()["suspended"] is True
    assert circuit.status()["hold_scope"] == "day"
    assert len(notifier.messages) == 1

    clock["value"] = (
        "2099-01-02", "2099-01-02 05:00:00", "2099-01-03 04:59:59",
    )
    state = circuit.activate_session("run-new-day", "morning")
    assert state["suspended"] is False
    assert state["current_daily_cost_usd"] == 0
    assert len(notifier.messages) == 2
    assert notifier.messages[-1].startswith("🟢 QAMC PAID ANALYSIS REARMED")
    with sqlite3.connect(path) as conn:
        old_day = conn.execute(
            "SELECT baseline_cost_usd + incremental_cost_usd FROM llm_budget_days "
            "WHERE day='2099-01-01'"
        ).fetchone()[0]
        new_day = conn.execute(
            "SELECT baseline_cost_usd + incremental_cost_usd FROM llm_budget_days "
            "WHERE day='2099-01-02'"
        ).fetchone()[0]
        recoveries = conn.execute(
            "SELECT COUNT(*) FROM llm_circuit_events "
            "WHERE event_type='quota_rearmed'"
        ).fetchone()[0]
    assert old_day == pytest.approx(1.0)
    assert new_day == pytest.approx(0.0)
    assert recoveries == 1
    circuit.status()
    assert len(notifier.messages) == 2


@pytest.mark.parametrize("pending_state", [0, -1])
def test_rollover_delivers_pending_quota_trip_before_recovery(
    tmp_path, monkeypatch, pending_state,
):
    clock = {
        "value": ("2099-01-01", "2099-01-01 05:00:00", "2099-01-02 04:59:59")
    }
    monkeypatch.setattr(
        "src.cost_circuit._et_day_and_utc_bounds", lambda now=None: clock["value"],
    )
    path = _db_path(tmp_path)
    notifier = _Notifier()
    circuit = LLMCostCircuitBreaker(
        path,
        _config(session_cost_limit_usd=2.0, daily_cost_limit_usd=1.0),
        notifier,
    )
    circuit.activate_session("run-pending-alert", "morning")
    with sqlite3.connect(path) as conn:
        conn.execute(
            "UPDATE llm_budget_sessions SET actual_cost_usd=1.0 "
            "WHERE run_id='run-pending-alert'"
        )
        conn.execute(
            "UPDATE llm_budget_days SET incremental_cost_usd=1.0 "
            "WHERE day='2099-01-01'"
        )
    circuit.enforce_current_limits("test_limit")
    assert len(notifier.messages) == 1
    notifier.messages.clear()
    with sqlite3.connect(path) as conn:
        conn.execute(
            "UPDATE llm_quota_holds SET alert_state=?, "
            "alert_updated_at=?",
            (pending_state, "2000-01-01 00:00:00"),
        )

    clock["value"] = (
        "2099-01-02", "2099-01-02 05:00:00", "2099-01-03 04:59:59",
    )
    state = circuit.activate_session("run-after-pending-alert", "morning")

    assert state["suspended"] is False
    assert len(notifier.messages) == 2
    assert notifier.messages[0].startswith("🟠 QAMC PAID ANALYSIS QUOTA HOLD")
    assert notifier.messages[1].startswith("🟢 QAMC PAID ANALYSIS REARMED")
    circuit.status()
    assert len(notifier.messages) == 2


def test_rollover_waits_for_fresh_trip_alert_lease_before_recovery(
    tmp_path, monkeypatch,
):
    clock = {
        "value": ("2099-01-01", "2099-01-01 05:00:00", "2099-01-02 04:59:59")
    }
    monkeypatch.setattr(
        "src.cost_circuit._et_day_and_utc_bounds", lambda now=None: clock["value"],
    )
    path = _db_path(tmp_path)
    notifier = _Notifier()
    circuit = LLMCostCircuitBreaker(
        path,
        _config(session_cost_limit_usd=2.0, daily_cost_limit_usd=1.0),
        notifier,
    )
    circuit.activate_session("run-fresh-lease", "morning")
    with sqlite3.connect(path) as conn:
        conn.execute(
            "UPDATE llm_budget_sessions SET actual_cost_usd=1.0 "
            "WHERE run_id='run-fresh-lease'"
        )
        conn.execute(
            "UPDATE llm_budget_days SET incremental_cost_usd=1.0 "
            "WHERE day='2099-01-01'"
        )
    circuit.enforce_current_limits("test_limit")
    notifier.messages.clear()
    with sqlite3.connect(path) as conn:
        conn.execute(
            "UPDATE llm_quota_holds SET alert_state=-1, "
            "alert_updated_at=datetime('now')"
        )

    clock["value"] = (
        "2099-01-02", "2099-01-02 05:00:00", "2099-01-03 04:59:59",
    )
    state = circuit.activate_session("run-after-fresh-lease", "morning")
    assert state["suspended"] is False
    assert notifier.messages == []

    with sqlite3.connect(path) as conn:
        conn.execute(
            "UPDATE llm_quota_holds SET alert_updated_at='2000-01-01 00:00:00'"
        )
    circuit.status()
    assert len(notifier.messages) == 2
    assert notifier.messages[0].startswith("🟠 QAMC PAID ANALYSIS QUOTA HOLD")
    assert notifier.messages[1].startswith("🟢 QAMC PAID ANALYSIS REARMED")


def test_clock_regression_with_future_quota_hold_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "src.cost_circuit._et_day_and_utc_bounds",
        lambda now=None: (
            "2099-01-01", "2099-01-01 05:00:00", "2099-01-02 04:59:59",
        ),
    )
    path = _db_path(tmp_path)
    circuit = LLMCostCircuitBreaker(
        path,
        _config(session_cost_limit_usd=2.0, daily_cost_limit_usd=1.0),
        _Notifier(),
    )
    circuit.activate_session("run-future-hold", "morning")
    with sqlite3.connect(path) as conn:
        conn.execute(
            "UPDATE llm_budget_sessions SET actual_cost_usd=1.0 "
            "WHERE run_id='run-future-hold'"
        )
        conn.execute(
            "UPDATE llm_budget_days SET incremental_cost_usd=1.0 "
            "WHERE day='2099-01-01'"
        )
    circuit.enforce_current_limits("test_limit")
    with sqlite3.connect(path) as conn:
        conn.execute(
            "UPDATE llm_quota_holds SET day='2099-01-02', "
            "scope_key='2099-01-02'"
        )

    state = circuit.activate_session("run-clock-regressed", "morning")

    assert state["suspended"] is True
    assert state["suspension_class"] == "hard"
    assert state["trigger_code"] == "non_monotonic_quota_hold_day"
    assert state["requires_operator_reset"] is True
    with sqlite3.connect(path) as conn:
        assert conn.execute(
            "SELECT active FROM llm_quota_holds"
        ).fetchone()[0] == 1


def test_clock_regression_with_future_reservation_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "src.cost_circuit._et_day_and_utc_bounds",
        lambda now=None: (
            "2099-01-01", "2099-01-01 05:00:00", "2099-01-02 04:59:59",
        ),
    )
    path = _db_path(tmp_path)
    circuit = LLMCostCircuitBreaker(path, _config(), _Notifier())
    circuit.activate_session("run-future-reservation", "morning")
    reservation = circuit.begin_call(
        agent_name="portfolio_manager", model="openai/gpt-5.5",
        system_prompt="s", user_message="u", max_output_tokens=100,
    )
    with sqlite3.connect(path) as conn:
        conn.execute(
            "UPDATE llm_budget_reservations SET day='2099-01-02', "
            "expires_at='2000-01-01 00:00:00' "
            "WHERE reservation_id=?",
            (reservation.reservation_id,),
        )

    state = circuit.activate_session("run-clock-regressed", "morning")

    assert state["suspended"] is True
    assert state["suspension_class"] == "hard"
    assert state["trigger_code"] == "non_monotonic_reservation_day"
    assert state["requires_operator_reset"] is True
    with sqlite3.connect(path) as conn:
        assert conn.execute(
            "SELECT status FROM llm_budget_reservations WHERE reservation_id=?",
            (reservation.reservation_id,),
        ).fetchone()[0] == "active"


def test_hard_latch_survives_et_rollover_until_audited_reset(tmp_path, monkeypatch):
    clock = {
        "value": ("2099-01-01", "2099-01-01 05:00:00", "2099-01-02 04:59:59")
    }
    monkeypatch.setattr(
        "src.cost_circuit._et_day_and_utc_bounds", lambda now=None: clock["value"],
    )
    notifier = _Notifier()
    circuit = LLMCostCircuitBreaker(_db_path(tmp_path), _config(), notifier)
    circuit.activate_session("run-hard", "morning")
    with pytest.raises(PaidAnalysisSuspended):
        circuit.begin_call(
            agent_name="portfolio_manager", model="unknown/unpriced-model",
            system_prompt="s", user_message="u", max_output_tokens=100,
        )
    assert circuit.status()["suspension_class"] == "hard"
    assert circuit.status()["requires_operator_reset"] is True

    clock["value"] = (
        "2099-01-02", "2099-01-02 05:00:00", "2099-01-03 04:59:59",
    )
    state = circuit.activate_session("run-next-day", "morning")
    assert state["suspended"] is True
    assert state["trigger_code"] == "unknown_model_price"
    assert len(notifier.messages) == 1
    circuit.reset("operator verified and pinned the missing model price")
    assert circuit.status()["suspended"] is False


def test_old_daily_latch_migrates_without_early_clear_or_duplicate_alert(
    tmp_path, monkeypatch,
):
    clock = {
        "value": ("2099-01-01", "2099-01-01 05:00:00", "2099-01-02 04:59:59")
    }
    monkeypatch.setattr(
        "src.cost_circuit._et_day_and_utc_bounds", lambda now=None: clock["value"],
    )
    path = _db_path(tmp_path)
    with sqlite3.connect(path) as conn:
        conn.execute("DROP TABLE llm_quota_holds")
        conn.execute(
            "INSERT INTO llm_budget_days(day, baseline_cost_usd) "
            "VALUES ('2099-01-01', 4.211481) "
            "ON CONFLICT(day) DO UPDATE SET baseline_cost_usd=excluded.baseline_cost_usd"
        )
        conn.execute(
            "INSERT OR REPLACE INTO llm_budget_sessions(run_id, day, mode) "
            "VALUES ('deployment-legacy', '2099-01-01', 'operator')"
        )
        conn.execute(
            "UPDATE llm_circuit_state SET suspended=1, "
            "trigger_code='daily_cost_limit', "
            "trigger_detail='daily LLM spend $4.2115 reached safe limit $1.50', "
            "run_id='deployment-legacy', mode='operator', agent_name='session_start', "
            "daily_cost_usd=4.211481, session_limit_usd=.90, daily_limit_usd=1.50, "
            "suspended_at='2099-01-01 18:11:04', alert_state=1"
        )
    notifier = _Notifier()
    circuit = LLMCostCircuitBreaker(path, _config(daily_cost_limit_usd=1.50), notifier)
    circuit.activate_session("same-day-check", "morning")
    state = circuit.status()
    assert state["suspended"] is True
    assert state["suspension_class"] == "quota"
    assert state["daily_cost_usd"] == pytest.approx(4.211481)
    assert notifier.messages == []

    clock["value"] = (
        "2099-01-02", "2099-01-02 05:00:00", "2099-01-03 04:59:59",
    )
    circuit.activate_session("next-day-check", "morning")
    assert circuit.status()["suspended"] is False
    assert len(notifier.messages) == 1
    assert "REARMED" in notifier.messages[0]


def test_concurrent_rollover_has_one_recovery_event_and_alert(tmp_path, monkeypatch):
    clock = {
        "value": ("2099-01-01", "2099-01-01 05:00:00", "2099-01-02 04:59:59")
    }
    monkeypatch.setattr(
        "src.cost_circuit._et_day_and_utc_bounds", lambda now=None: clock["value"],
    )
    path = _db_path(tmp_path)
    first_notifier = _Notifier()
    cfg = _config(session_cost_limit_usd=2.0, daily_cost_limit_usd=1.0)
    first = LLMCostCircuitBreaker(path, cfg, first_notifier)
    first.activate_session("run-old", "morning")
    with sqlite3.connect(path) as conn:
        conn.execute(
            "UPDATE llm_budget_sessions SET actual_cost_usd=1.0 WHERE run_id='run-old'"
        )
        conn.execute(
            "UPDATE llm_budget_days SET incremental_cost_usd=1.0 "
            "WHERE day='2099-01-01'"
        )
    first.enforce_current_limits("test_limit")
    second_notifier = _Notifier()
    second = LLMCostCircuitBreaker(path, cfg, second_notifier)

    clock["value"] = (
        "2099-01-02", "2099-01-02 05:00:00", "2099-01-03 04:59:59",
    )
    barrier = threading.Barrier(2)
    states: list[bool] = []

    def activate(circuit, run_id):
        barrier.wait()
        states.append(bool(circuit.activate_session(run_id, "morning")["suspended"]))

    threads = [
        threading.Thread(target=activate, args=(first, "run-new-a")),
        threading.Thread(target=activate, args=(second, "run-new-b")),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert states == [False, False]
    recovery_messages = [
        message for message in first_notifier.messages + second_notifier.messages
        if "PAID ANALYSIS REARMED" in message
    ]
    assert len(recovery_messages) == 1
    with sqlite3.connect(path) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM llm_circuit_events WHERE event_type='quota_rearmed'"
        ).fetchone()[0] == 1


def test_legacy_quota_without_day_provenance_remains_hard(tmp_path):
    path = _db_path(tmp_path)
    with sqlite3.connect(path) as conn:
        conn.execute("DROP TABLE llm_quota_holds")
        conn.execute(
            "UPDATE llm_circuit_state SET suspended=1, "
            "trigger_code='daily_cost_limit', trigger_detail='legacy ambiguous hold', "
            "run_id='missing-run', mode='operator', agent_name='migration', "
            "daily_cost_usd=2, daily_limit_usd=1.5, alert_state=1"
        )
    circuit = LLMCostCircuitBreaker(path, _config(), _Notifier())
    state = circuit.status()
    assert state["suspended"] is True
    assert state["available"] is False
    assert state["trigger_code"] == "circuit_infrastructure_unavailable"
    assert Path(f"{path}.llm-circuit-unavailable").exists()


# === Pricing-staleness SPOF fix (2026-08-28) ===
#
# Before this fix, `cost_table.refresh_openrouter_pricing()` accepted a
# cached OpenRouter rate ONLY under 24h old. Past that boundary, both
# `TradingPipeline.__init__` (src/pipeline.py) and `activate_paid_call_
# session` above respond to a False return with `breaker.mark_unavailable`
# -- the durable, cross-process latch that only `LLMCostCircuitBreaker.
# reset()` (operator-only, reason mandatory) can clear. Because the cache
# is rewritten only when a fetch happens, and a fetch only happens once the
# cache is already stale, one openrouter.ai outage overlapping the first
# session past the 24h mark could stop the desk until a human intervened --
# see `test_mandatory_openrouter_refresh_rejects_stale_cache_when_network_
# is_down` in tests/test_cost_table.py, which reproduces exactly that.
#
# These tests exercise the fix at the level a real deployment actually
# hits it: `activate_paid_call_session`, and `begin_call`'s persisted
# reservation amount.


def test_stale_within_grace_reservation_is_larger_than_fresh(tmp_path, monkeypatch):
    """Integration-level proof, not just the pure-function one in
    test_cost_table.py: for the IDENTICAL prompt/model/max_output_tokens,
    begin_call's actual persisted reserved_cost_usd is provably larger when
    the OpenRouter pricing cache is stale-but-within-grace than when it is
    fresh -- the widened multiplier from cost_table.
    openrouter_pricing_reservation_multiplier reaching all the way through
    _attempt_reserve into the reservation row a real call would see."""
    import os
    import time
    from src import cost_table

    cache = tmp_path / "openrouter_pricing_cache.json"
    cache.write_text("{}")  # only the file's mtime matters here
    monkeypatch.setattr(cost_table, "_OPENROUTER_CACHE_PATH", cache)

    grace_config = _loose_config(
        openrouter_pricing_grace_period_hours=24.0,
        openrouter_pricing_stale_multiplier_max=1.5,
    )

    def _reserve(age_hours: float, run_id: str) -> tuple[float, float]:
        mtime = time.time() - age_hours * 3600
        os.utime(cache, (mtime, mtime))
        multiplier = cost_table.openrouter_pricing_reservation_multiplier(
            1.05, grace_period_hours=24.0, max_stale_multiplier=1.5,
        )
        db_dir = tmp_path / run_id
        db_dir.mkdir()
        path = _db_path(db_dir)
        circuit = LLMCostCircuitBreaker(path, grace_config, _Notifier())
        circuit.activate_session(run_id, "morning")
        reservation = circuit.begin_call(
            agent_name="portfolio_manager", model="openai/gpt-5.5",
            system_prompt="system prompt", user_message="user message",
            max_output_tokens=16_000,
        )
        with sqlite3.connect(path) as conn:
            reserved = conn.execute(
                "SELECT reserved_cost_usd FROM llm_budget_reservations "
                "WHERE reservation_id=?", (reservation.reservation_id,),
            ).fetchone()[0]
        return float(reserved), multiplier

    fresh_reserved, fresh_multiplier = _reserve(1.0, "run-fresh-cache")   # well under 24h
    stale_reserved, stale_multiplier = _reserve(30.0, "run-stale-cache")  # 6h into 24h grace

    assert fresh_multiplier == 1.05  # hard literal: fresh path is untouched
    assert stale_multiplier > fresh_multiplier
    assert stale_reserved > fresh_reserved
    # Loose (1%) relative tolerance: `multiplier` above is captured just
    # BEFORE begin_call, which recomputes the same function internally a
    # few milliseconds later against the same fixed cache mtime -- real
    # wall-clock drift between the two reads, not a modeling error.
    assert stale_reserved == pytest.approx(
        fresh_reserved * stale_multiplier / fresh_multiplier, rel=1e-2,
    )


def test_activate_paid_call_session_proceeds_on_stale_within_grace_pricing(tmp_path, monkeypatch):
    """The actual SPOF: before this fix, a stale-past-24h OpenRouter cache
    with the catalog unreachable made activate_paid_call_session latch via
    mark_unavailable on every call. Within the configured grace window it
    must instead proceed -- no suspension, and a real call can still be
    reserved -- exactly the "scoped/self-healing, not a human-reset latch"
    behaviour the fix is required to produce."""
    import os
    import time
    from src import cost_table

    cache = tmp_path / "openrouter_pricing_cache.json"
    rates = {model: dict(value) for model, value in cost_table._PRICING_OPENROUTER.items()}
    cache.write_text(json.dumps(rates))
    stale_age_s = cost_table._CACHE_MAX_AGE_SECONDS + 6 * 3600  # 6h into a 24h grace window
    old = time.time() - stale_age_s
    os.utime(cache, (old, old))
    monkeypatch.setattr(cost_table, "_OPENROUTER_CACHE_PATH", cache)
    monkeypatch.setattr(cost_table, "_fetch_openrouter_pricing", lambda: None)

    app_config = SimpleNamespace(
        llm_cost_circuit=_loose_config(
            openrouter_pricing_grace_period_hours=24.0,
            openrouter_pricing_stale_multiplier_max=1.5,
        ),
    )
    circuit = activate_paid_call_session(
        app_config, run_id="run-stale-grace", mode="morning",
        notifier=_Notifier(), db_path=_db_path(tmp_path),
    )

    state = circuit.status()
    assert state["suspended"] is False
    assert not Path(f"{circuit.db_path}.llm-circuit-unavailable").exists()

    reservation = circuit.begin_call(
        agent_name="portfolio_manager", model="openai/gpt-5.5",
        system_prompt="p", user_message="m", max_output_tokens=1000,
    )
    assert reservation.reservation_id != "disabled"


def test_activate_paid_call_session_still_latches_beyond_grace_pricing(tmp_path, monkeypatch):
    """The grace window is a bound, not a blank check: a cache older than
    24h + the configured grace, with the catalog unreachable, is genuinely
    unknown pricing and must still latch exactly as it did before this fix
    -- the global operator-reset latch is the CORRECT response to truly
    unbounded cost, and this proves the fix didn't accidentally remove it."""
    import os
    import time
    from src import cost_table

    cache = tmp_path / "openrouter_pricing_cache.json"
    rates = {model: dict(value) for model, value in cost_table._PRICING_OPENROUTER.items()}
    cache.write_text(json.dumps(rates))
    beyond_grace_s = cost_table._CACHE_MAX_AGE_SECONDS + 24 * 3600 + 3600  # 1h past the grace edge
    old = time.time() - beyond_grace_s
    os.utime(cache, (old, old))
    monkeypatch.setattr(cost_table, "_OPENROUTER_CACHE_PATH", cache)
    monkeypatch.setattr(cost_table, "_fetch_openrouter_pricing", lambda: None)

    app_config = SimpleNamespace(
        llm_cost_circuit=_loose_config(
            openrouter_pricing_grace_period_hours=24.0,
            openrouter_pricing_stale_multiplier_max=1.5,
        ),
    )
    circuit = activate_paid_call_session(
        app_config, run_id="run-beyond-grace", mode="morning",
        notifier=_Notifier(), db_path=_db_path(tmp_path),
    )

    state = circuit.status()
    assert state["suspended"] is True
    assert state["available"] is False
    assert Path(f"{circuit.db_path}.llm-circuit-unavailable").exists()
