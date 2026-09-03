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
        # Item 14 (2026-09-02): the runaway-loop backstop is now a plain
        # call count (docs/WORK.md item 14c), not a reservation-era
        # free-failure-session count. High by default so tests not
        # exercising the cap itself never hit it; tests for the cap
        # override it explicitly.
        "max_calls_per_session": 1000,
        "max_provider_attempts_per_call": 2,
        "input_chars_per_token": 3.5,
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




def test_optional_retry_budget_exhaustion_skips_without_opening_circuit(tmp_path):
    """Item 14 (2026-09-02): the optional-retry skip is now keyed off the
    plain per-session call-count backstop (14c), not a reservation-era
    retry-attempts counter. A session that has already used its whole
    call budget on real work still lets an OPTIONAL retry bow out quietly
    (OptionalPaidAnalysisRetrySkipped) instead of tripping the circuit --
    the caller may safely keep its already-completed primary analysis."""
    path = _db_path(tmp_path)
    circuit = LLMCostCircuitBreaker(path, _config(max_calls_per_session=2), _Notifier())
    circuit.activate_session("run-optional-retry", "morning")
    for _ in range(2):
        reservation = circuit.begin_call(
            agent_name="tech_analyst",
            model="google/gemini-3.5-flash-lite",
            system_prompt="s", user_message="u", max_output_tokens=100,
        )
        circuit.before_provider_attempt(reservation, model=reservation.model)
        circuit.complete_call(reservation, 0.01)

    with pytest.raises(OptionalPaidAnalysisRetrySkipped):
        circuit.begin_call(
            agent_name="tech_analyst",
            model="google/gemini-3.5-flash-lite",
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
            "SELECT logical_calls, status FROM llm_budget_sessions WHERE run_id=?",
            ("run-optional-retry",),
        ).fetchone()
    # The skipped optional retry never incremented logical_calls or
    # touched session status -- exactly like the reservation-era version,
    # just measured by a raw count instead of a reservation row.
    assert row == (2, "active")


def test_completed_session_spend_holds_only_that_session_and_cannot_be_reset(tmp_path):
    path = _db_path(tmp_path)
    notifier = _Notifier()
    cfg = _config(session_cost_limit_usd=0.50, daily_cost_limit_usd=2.0)
    circuit = LLMCostCircuitBreaker(path, cfg, notifier)
    circuit.activate_session("run-cost", "morning")
    reservation = circuit.begin_call(
        agent_name="tech_analyst", model="google/gemini-3.5-flash-lite",
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
        model="google/gemini-3.5-flash-lite",
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
        _config(max_provider_attempts_per_call=2),
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
    assert "no provable-zero-cost telemetry" in notifier.messages[1]


def test_failed_call_with_ambiguous_cost_latches_without_inventing_a_charge(tmp_path, monkeypatch):
    """Item 14 (2026-09-02): an ambiguous failure (not provably $0) still
    fails closed -- but there is no reservation left to convert into a
    dollar charge, so it marks the day/session inexact and latches on
    THAT, rather than booking a guessed amount. Renamed from
    `test_failed_call_without_usage_consumes_reserve_and_latches`, which
    pinned the old "charge the conservative reserve" behavior this
    replaces."""
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
    # No reservation exists to charge any more -- an ambiguous failure
    # costs the ledger nothing invented, only certainty.
    assert state["current_session_cost_usd"] == 0
    assert state["current_daily_cost_usd"] == 0
    assert "no provable-zero-cost telemetry" in notifier.messages[0]


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
                         model: str = "google/gemini-3.5-flash-lite"):
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
            "SELECT status, actual_cost_usd, costs_exact "
            "FROM llm_budget_sessions WHERE run_id=?",
            (reservation.run_id,),
        ).fetchone()
    assert row == ("active", 0.0, 1)


def _assert_charged_and_tripped(circuit, notifier, reservation):
    state = circuit.status()
    assert state["suspended"] is True
    assert state["trigger_code"] == "failed_call_unknown_cost"
    # Item 14 (2026-09-02): no reservation exists to convert into a dollar
    # charge any more -- an ambiguous failure marks the ledger inexact
    # instead of booking a guessed amount.
    assert state["current_session_cost_usd"] == 0
    assert state["current_daily_cost_usd"] == 0
    assert "no provable-zero-cost telemetry" in notifier.messages[0]


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
# Defect 2, success half (2026-09-02): the same phantom charge on the branch
# where the retry SUCCEEDS. `fail_call` learned the zero-cost allow-list on
# 2026-08-31; `complete_call` did not, so a first attempt refused with a 429
# still had its conservative reserve added on top of the real cost of the
# response the retry produced. Measured twice in the live ledger at 9.6x and
# 5.4x the true cost of those calls.
# ============================================================================

def _retry_then_succeed(circuit, *, run_id, first_error, actual_cost,
                        failed_attempt_errors, agent_name="tech_analyst",
                        model="google/gemini-3.5-flash-lite"):
    """One logical call: attempt 1 authorized and failed with `first_error`,
    attempt 2 authorized and settled at `actual_cost`. Mirrors what
    `BaseAgent._execute` does -- the reservation is never failed, because the
    call as a whole succeeded -- and returns the reservation."""
    circuit.activate_session(run_id, "morning")
    reservation = circuit.begin_call(
        agent_name=agent_name, model=model,
        system_prompt="s", user_message="u", max_output_tokens=100,
    )
    circuit.before_provider_attempt(reservation, model=reservation.model)
    # attempt 1 fails; the agent keeps the exception and retries rather than
    # calling fail_call, exactly as the retry loop does.
    circuit.before_provider_attempt(reservation, model=reservation.model)
    circuit.complete_call(
        reservation, actual_cost, actual_model=reservation.model,
        failed_attempt_errors=failed_attempt_errors,
    )
    return reservation


def _settled(path, reservation):
    """This logical call's settled cost. Item 14 (2026-09-02): there is no
    per-call reservation row any more, so read the session's running total
    instead -- exactly one call was made per run_id in every caller below,
    so the session total IS this call's settled cost."""
    with sqlite3.connect(path) as conn:
        return conn.execute(
            "SELECT actual_cost_usd FROM llm_budget_sessions "
            "WHERE run_id=?", (reservation.run_id,),
        ).fetchone()[0]


@pytest.mark.parametrize("status_code", [429, 400, 401, 403, 404])
def test_success_after_provably_free_attempt_is_charged_only_the_real_cost(
    tmp_path, status_code,
):
    path = _db_path(tmp_path)
    notifier = _Notifier()
    circuit = LLMCostCircuitBreaker(path, _config(), notifier)
    refusal = _StatusCodeError(status_code, f"provider rejected ({status_code})")

    reservation = _retry_then_succeed(
        circuit, run_id=f"run-retry-{status_code}", first_error=refusal,
        actual_cost=0.0014, failed_attempt_errors=[refusal],
    )

    assert _settled(path, reservation) == pytest.approx(0.0014)
    state = circuit.status()
    assert state["current_session_cost_usd"] == pytest.approx(0.0014)
    assert state["suspended"] is False
    # The day is exact: nothing about this call is estimated any more.
    with sqlite3.connect(path) as conn:
        assert conn.execute(
            "SELECT costs_exact FROM llm_budget_sessions WHERE run_id=?",
            (f"run-retry-{status_code}",),
        ).fetchone()[0] == 1


def test_success_after_ambiguous_attempt_marks_inexact_instead_of_a_phantom_charge(tmp_path):
    """Renamed from `test_success_after_ambiguous_attempt_still_carries_the_
    failed_reserve`, which pinned the OLD behaviour: a 500 might have billed
    for a stream that started and died, so its conservative reserve was
    added on top of the real settled cost. That assertion is now WRONG on
    its own terms -- item 14 (2026-09-02) deleted the reservation there was
    ever anything to add. The fail-closed intent survives in a different
    place: the day/session is marked inexact (so `_enforce_settled_limits_
    locked` latches on it), while the ledger itself only ever holds the
    ACTUAL cost of the response that came back -- no invented figure for
    what an ambiguous failed attempt might have cost."""
    path = _db_path(tmp_path)
    circuit = LLMCostCircuitBreaker(path, _config(), _Notifier())
    ambiguous = _StatusCodeError(500, "upstream exploded mid-stream")

    reservation = _retry_then_succeed(
        circuit, run_id="run-retry-500", first_error=ambiguous,
        actual_cost=0.0014, failed_attempt_errors=[ambiguous],
    )

    # Only the real, settled cost is ever booked -- never a guess.
    assert _settled(path, reservation) == pytest.approx(0.0014)
    with sqlite3.connect(path) as conn:
        assert conn.execute(
            "SELECT costs_exact FROM llm_budget_sessions WHERE run_id=?",
            ("run-retry-500",),
        ).fetchone()[0] == 0
    # Fail-closed is not lost: an inexact day hard-latches on the very next
    # authorization boundary, same posture as the deleted phantom charge.
    assert circuit.status()["trigger_code"] == "legacy_unknown_cost"


def test_one_ambiguous_attempt_among_free_ones_still_marks_inexact(tmp_path):
    """Renamed from `..._keeps_the_whole_reserve`: ambiguity is still
    contagious (see `_all_attempts_provably_free`) -- one attempt that
    might have been billed still makes the whole call's exactness suspect
    -- but there is no reservation left to inflate a dollar charge with."""
    path = _db_path(tmp_path)
    circuit = LLMCostCircuitBreaker(path, _config(), _Notifier())
    free = _StatusCodeError(429, "rate limited")
    ambiguous = _StatusCodeError(503, "upstream unavailable")

    reservation = _retry_then_succeed(
        circuit, run_id="run-retry-mixed", first_error=free,
        actual_cost=0.0014, failed_attempt_errors=[free, ambiguous],
    )

    assert _settled(path, reservation) == pytest.approx(0.0014)
    with sqlite3.connect(path) as conn:
        assert conn.execute(
            "SELECT costs_exact FROM llm_budget_sessions WHERE run_id=?",
            ("run-retry-mixed",),
        ).fetchone()[0] == 0


def test_caller_that_names_no_attempts_is_treated_as_exact(tmp_path):
    """Renamed from `..._keeps_the_old_conservative_charge`: the parameter
    is still optional, but with no reservation to inflate, a caller that
    cannot enumerate its attempts no longer gets a worse-case guess -- it
    gets exactly what the provider actually reported, same as a clean
    first-attempt success. There is nothing left to be conservative WITH."""
    path = _db_path(tmp_path)
    circuit = LLMCostCircuitBreaker(path, _config(), _Notifier())

    reservation = _retry_then_succeed(
        circuit, run_id="run-retry-silent",
        first_error=_StatusCodeError(429, "rate limited"),
        actual_cost=0.0014, failed_attempt_errors=None,
    )

    assert _settled(path, reservation) == pytest.approx(0.0014)


def test_clean_first_attempt_success_is_unaffected_by_the_new_parameter(tmp_path):
    """No failed attempts, no reserve to forgive: an empty list must settle
    at exactly the provider's figure, same as before."""
    path = _db_path(tmp_path)
    circuit = LLMCostCircuitBreaker(path, _config(), _Notifier())
    circuit.activate_session("run-clean", "morning")
    reservation = circuit.begin_call(
        agent_name="tech_analyst", model="google/gemini-3.5-flash-lite",
        system_prompt="s", user_message="u", max_output_tokens=100,
    )
    circuit.before_provider_attempt(reservation, model=reservation.model)
    circuit.complete_call(
        reservation, 0.0014, actual_model=reservation.model,
        failed_attempt_errors=[],
    )

    assert _settled(path, reservation) == pytest.approx(0.0014)


def test_base_agent_reports_its_failed_attempts_to_the_circuit(tmp_path, monkeypatch):
    """End-to-end: the 2026-08-28 14:31 shape. A 429 on the first provider
    attempt, a clean success on the retry, and the ledger must show the real
    cost -- not the real cost plus a reserve for a request the provider
    refused before generating a token."""
    path = _db_path(tmp_path)
    circuit = LLMCostCircuitBreaker(path, _config(), _Notifier())
    circuit.activate_session("run-agent-retry", "morning")
    monkeypatch.setenv("QUANT_AGENT_MAX_RETRIES", "2")
    monkeypatch.setenv("QUANT_AGENT_RETRY_BASE_S", "0")

    usage = SimpleNamespace(input_tokens=100, output_tokens=10)
    ok = SimpleNamespace(
        content=[SimpleNamespace(text='{"ok": true}')],
        usage=usage, stop_reason="end_turn",
    )
    client = MagicMock()
    client.messages.create.side_effect = [_StatusCodeError(429, "slow down"), ok]
    with patch("anthropic.Anthropic", return_value=client):
        agent = _Agent(api_key="x", model="claude-sonnet-4-6", max_tokens=64)
        agent.set_cost_circuit(circuit)
        result = agent.run()

    # Pin the PATH before pinning the money. Without these the test can pass
    # for the wrong reason: a single-attempt success, or a failover onto a
    # dearer model whose own attempt reserve swallows the failed one, both
    # settle at the right number while exercising none of the fix.
    assert result.provider_requests == 2
    assert result.used_fallback is False
    assert result.model == "claude-sonnet-4-6"

    with sqlite3.connect(path) as conn:
        settled, exact, attempts = conn.execute(
            "SELECT actual_cost_usd, costs_exact, provider_attempts "
            "FROM llm_budget_sessions WHERE run_id=?", ("run-agent-retry",),
        ).fetchone()
    assert attempts == 2, "both attempts must have been authorized"
    # Item 14 (2026-09-02): no reservation exists to carry two attempts'
    # worth of reserve any more -- only the actual settled cost of the
    # response that came back is ever booked.
    assert exact == 1
    assert settled == pytest.approx(result.cost_usd, rel=1e-9)
    assert settled == pytest.approx(0.00045, rel=1e-9)


# ============================================================================
# Item 14 (OWNER-APPROVED 2026-09-02, docs/WORK.md): the runaway-loop
# backstop is now a plain per-session call COUNT (`max_calls_per_session`),
# independent of price -- replacing the reservation-era free-failure-session
# counting query, its cooling-off window, and the dollar-based per-mode/
# afternoon-reserve machinery that all existed to manage the deleted
# reservation's over-holding. A loop is defined by call count, and counting
# cannot be wrong about a rate the way a dollar estimate can.
# ============================================================================

def _settle_cheap_session(circuit, path, run_id, mode, cost):
    circuit.activate_session(run_id, mode)
    reservation = circuit.begin_call(
        agent_name="tech_analyst", model="google/gemini-3.5-flash-lite",
        system_prompt="s", user_message="u", max_output_tokens=100,
    )
    circuit.before_provider_attempt(reservation, model=reservation.model)
    circuit.complete_call(reservation, cost, actual_model=reservation.model)


def test_session_count_cap_triggers_on_the_next_call(tmp_path):
    """(c): a session that has already made N calls is stopped on call
    N+1 -- a runaway loop is caught by COUNT, with no dollar amount
    involved at all (every call here settles at exactly $0)."""
    path = _db_path(tmp_path)
    notifier = _Notifier()
    circuit = LLMCostCircuitBreaker(
        path, _config(max_calls_per_session=3), notifier,
    )
    circuit.activate_session("run-loop", "intra_check")
    for _ in range(3):
        reservation = circuit.begin_call(
            agent_name="tech_analyst", model="google/gemini-3.5-flash-lite",
            system_prompt="s", user_message="u", max_output_tokens=100,
        )
        circuit.before_provider_attempt(reservation, model=reservation.model)
        circuit.complete_call(reservation, 0.0, actual_model=reservation.model)

    with pytest.raises(PaidAnalysisSuspended) as excinfo:
        circuit.begin_call(
            agent_name="tech_analyst", model="google/gemini-3.5-flash-lite",
            system_prompt="s", user_message="u", max_output_tokens=100,
        )
    state = excinfo.value.state
    assert state["trigger_code"] == "session_call_count_limit"
    assert "3 call" in state["trigger_detail"]
    # Session-scoped and self-healing: a genuinely different session is not
    # affected by another session's runaway loop.
    circuit.activate_session("run-independent", "intra_check")
    reservation = circuit.begin_call(
        agent_name="tech_analyst", model="google/gemini-3.5-flash-lite",
        system_prompt="s", user_message="u", max_output_tokens=100,
    )
    assert reservation.reservation_id != "disabled"


def test_session_count_backstop_still_trips_a_genuine_runaway_loop(tmp_path):
    """A loop of sessions that spend essentially nothing (e.g. every
    attempt is a Defect-2 known-zero-cost rejection) would never trip a
    dollar-based check -- the call-count cap is what stops it, one call
    per session in this shape."""
    path = _db_path(tmp_path)
    notifier = _Notifier()
    circuit = LLMCostCircuitBreaker(
        path, _config(max_calls_per_session=1), notifier,
    )
    _settle_cheap_session(circuit, path, "intra_check-0", "intra_check", 0.0)

    circuit.activate_session("intra_check-0", "intra_check")
    with pytest.raises(PaidAnalysisSuspended) as excinfo:
        circuit.begin_call(
            agent_name="tech_analyst", model="google/gemini-3.5-flash-lite",
            system_prompt="s", user_message="u", max_output_tokens=100,
        )
    assert excinfo.value.state["trigger_code"] == "session_call_count_limit"



def _loose_config(**overrides):
    """A config with cost ceilings wide enough that only the specific
    check each test is exercising can fire."""
    values = dict(session_cost_limit_usd=10.0, daily_cost_limit_usd=10.0)
    values.update(overrides)
    return _config(**values)






















def test_call_count_cap_is_atomic_across_process_objects(tmp_path):
    """Renamed/rewritten from `test_daily_reservation_is_atomic_across_
    process_objects`: that test raced two `begin_call`s against a shared
    DOLLAR reservation ceiling to prove the ``BEGIN IMMEDIATE`` write lock
    made admission atomic across process objects (no double-admit). Item
    14 (2026-09-02) deleted that dollar reservation, so there is nothing
    left to race a projected cost against -- but the SAME atomicity
    property still matters for the call-count cap (14c) that replaced it:
    two processes racing the last slot on one session must still produce
    exactly one winner, never two, and never zero."""
    path = _db_path(tmp_path)
    cfg = _config(max_calls_per_session=1)
    first = LLMCostCircuitBreaker(path, cfg, _Notifier())
    second = LLMCostCircuitBreaker(path, cfg, _Notifier())
    barrier = threading.Barrier(2)
    outcomes: list[str] = []

    def reserve(circuit):
        # ContextVars intentionally do not inherit into raw threads. Each
        # independent worker/process activates its own paid session context
        # for the SAME run_id -- exactly two systemd processes racing the
        # same session's call budget.
        circuit.activate_session("run-shared", "morning")
        barrier.wait()
        try:
            circuit.begin_call(
                agent_name="a", model="google/gemini-3.5-flash-lite",
                system_prompt="s", user_message="u", max_output_tokens=1_000,
            )
            outcomes.append("admitted")
        except PaidAnalysisSuspended:
            outcomes.append("blocked")

    threads = [
        threading.Thread(target=reserve, args=(first,)),
        threading.Thread(target=reserve, args=(second,)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(outcomes) == ["admitted", "blocked"]
    with sqlite3.connect(path) as conn:
        logical_calls = conn.execute(
            "SELECT logical_calls FROM llm_budget_sessions WHERE run_id=?",
            ("run-shared",),
        ).fetchone()[0]
    assert logical_calls == 1




def test_provider_boundary_revalidates_ledger_after_reservation(tmp_path):
    """The name predates item 14, but the property it pins does not: a
    call authorized by `begin_call` can wait behind the provider semaphore
    while a DIFFERENT session settles real cost and the ledger becomes
    internally inconsistent. `before_provider_attempt` re-validates the
    whole day's accounting invariants immediately before network I/O
    rather than trusting whatever `begin_call` saw earlier -- unrelated to
    the (deleted) reservation table, this is about not sending a request
    on top of a damaged ledger."""
    path = _db_path(tmp_path)
    circuit = LLMCostCircuitBreaker(
        path,
        _config(session_cost_limit_usd=2.0, daily_cost_limit_usd=1.0),
        _Notifier(),
    )
    circuit.activate_session("run-waiting", "morning")
    waiting = circuit.begin_call(
        agent_name="waiting", model="google/gemini-3.5-flash-lite",
        system_prompt="s", user_message="u", max_output_tokens=100,
    )

    circuit.activate_session("run-settled", "midday")
    settled = circuit.begin_call(
        agent_name="settled", model="google/gemini-3.5-flash-lite",
        system_prompt="s", user_message="u", max_output_tokens=100,
    )
    circuit.before_provider_attempt(settled, model=settled.model)
    circuit.complete_call(settled, 0.10, actual_model=settled.model)

    # Simulate damage after the waiting call was authorized but before it
    # reaches the provider semaphore. The day and session ledgers now
    # disagree.
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE llm_budget_days SET incremental_cost_usd=0")

    with pytest.raises(
        RuntimeError,
        match="day/session settled-cost ledgers disagree",
    ):
        circuit.before_provider_attempt(waiting, model=waiting.model)
    # The waiting call never reached the network -- its in-process attempt
    # counter is untouched.
    assert waiting.attempt_count == 0


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
            model="google/gemini-3.5-flash-lite",
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
        agent_name="portfolio_manager", model="google/gemini-3.5-flash-lite",
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

    # The in-flight response's real cost was never accounted -- another
    # process's accounting failure blocks it before any ledger write, so
    # the session shows no settled spend at all rather than a phantom
    # reservation charge.
    with sqlite3.connect(path) as conn:
        row = conn.execute(
            "SELECT status, actual_cost_usd FROM llm_budget_sessions "
            "WHERE run_id=?", (reservation.run_id,),
        ).fetchone()
    assert row == ("active", 0.0)


def test_emergency_alert_prefers_exact_persisted_attempt_count(tmp_path):
    path = _db_path(tmp_path)
    notifier = _Notifier()
    circuit = LLMCostCircuitBreaker(path, _config(), notifier)
    circuit.activate_session("run-snapshot", "morning")
    reservation = circuit.begin_call(
        agent_name="tech", model="google/gemini-3.5-flash-lite",
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
    # Item 14 (2026-09-02): no reservation exists to charge for a request
    # with no usable telemetry any more -- the ledger stays exactly $0
    # while the trip itself still fails closed on the missing usage data.
    assert state["current_session_cost_usd"] == 0
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


@pytest.mark.parametrize("missing", ["day", "session"])
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
                conn.execute(
                    "DELETE FROM llm_budget_sessions WHERE run_id=?", (run_id,)
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
        "llm_budget_days", "llm_budget_sessions",
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






@pytest.mark.parametrize(
    ("code", "scope"),
    [
        # Item 14 (2026-09-02): every projection-based trigger this list
        # used to carry ("projected_*_cost_limit", "provider_projected_*",
        # "outstanding_projected_*", "mode_daily_spend_limit",
        # "session_retry_attempt_limit") is gone along with the reservation
        # layer that computed them. "daily_cost_limit"/"session_cost_limit"
        # now fire on REAL SETTLED spend only; "session_call_count_limit"
        # is the new call-count runaway-loop backstop (item 14c).
        ("daily_cost_limit", "day"),
        ("session_cost_limit", "session"),
        ("session_call_count_limit", "session"),
        # Defect 5 (2026-08-31, unaffected by item 14): "provider_attempt_
        # limit" stays session-scoped. It bounds attempts within ONE call,
        # independent of the item-14c call-count backstop -- see the NOTE
        # by _SESSION_QUOTA_TRIGGERS in src/cost_circuit.py.
        ("provider_attempt_limit", "session"),
        ("legacy_unknown_cost", "hard"),
        ("unknown_model_price", "hard"),
        ("unknown_actual_cost", "hard"),
        ("failed_call_unknown_cost", "hard"),
        # Unrecognized/removed codes fail closed to "hard" by design --
        # includes every reservation-era code deleted by item 14.
        ("expired_attempted_reservation", "hard"),
        ("cross_day_started_reservation", "hard"),
        ("mode_daily_spend_limit", "hard"),
        ("projected_daily_cost_limit", "hard"),
        ("projected_session_cost_limit", "hard"),
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




def test_hard_latch_survives_et_rollover_until_audited_reset(tmp_path, monkeypatch):
    """The hard-latch scenario used to be `begin_call` on an unpriced
    model (`unknown_model_price`) -- gone along with the reservation layer
    that priced calls at all. `unknown_actual_cost` (no usable cost/token
    telemetry at completion) is a different, still-real hard trigger that
    exercises the identical durability property: unlike a quota hold, it
    does NOT clear on ET rollover and needs an audited `reset()`."""
    clock = {
        "value": ("2099-01-01", "2099-01-01 05:00:00", "2099-01-02 04:59:59")
    }
    monkeypatch.setattr(
        "src.cost_circuit._et_day_and_utc_bounds", lambda now=None: clock["value"],
    )
    notifier = _Notifier()
    circuit = LLMCostCircuitBreaker(_db_path(tmp_path), _config(), notifier)
    circuit.activate_session("run-hard", "morning")
    reservation = circuit.begin_call(
        agent_name="portfolio_manager", model="openai/gpt-5.5",
        system_prompt="s", user_message="u", max_output_tokens=100,
    )
    circuit.before_provider_attempt(reservation, model=reservation.model)
    circuit.complete_call(reservation, None)  # no usable telemetry -> hard latch
    assert circuit.status()["suspension_class"] == "hard"
    assert circuit.status()["requires_operator_reset"] is True

    clock["value"] = (
        "2099-01-02", "2099-01-02 05:00:00", "2099-01-03 04:59:59",
    )
    state = circuit.activate_session("run-next-day", "morning")
    assert state["suspended"] is True
    assert state["trigger_code"] == "unknown_actual_cost"
    assert len(notifier.messages) == 1
    circuit.reset("operator verified spend and the missing telemetry cause")
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


# ============================================================================
# Item 14 (OWNER-APPROVED 2026-09-02, docs/WORK.md): the three behaviors the
# owner's replacement design specifies, pinned directly.
# ============================================================================

def test_settled_cost_cap_triggers_only_after_a_call_actually_pushes_it_over(tmp_path):
    """(b): the desk stops when REAL SETTLED cost -- the actual amount a
    completed call's provider response reported -- pushes today's spend
    over the daily cap. No projection is involved: the call that trips it
    is authorized normally (its own dollar amount is unknown until it
    settles) and only the ACTUAL reported cost, applied by `complete_call`,
    crosses the line."""
    path = _db_path(tmp_path)
    notifier = _Notifier()
    circuit = LLMCostCircuitBreaker(
        path, _config(session_cost_limit_usd=5.0, daily_cost_limit_usd=1.00), notifier,
    )
    circuit.activate_session("run-daily-cap", "morning")
    reservation = circuit.begin_call(
        agent_name="portfolio_manager", model="openai/gpt-5.5",
        system_prompt="s", user_message="u", max_output_tokens=16_000,
    )
    circuit.before_provider_attempt(reservation, model=reservation.model)
    # The call itself was never blocked going in -- only once its ACTUAL
    # returned cost lands does the cap trip.
    assert circuit.status()["suspended"] is False
    circuit.complete_call(reservation, 1.20, actual_model=reservation.model)

    state = circuit.status()
    assert state["suspended"] is True
    assert state["trigger_code"] == "daily_cost_limit"
    assert state["current_daily_cost_usd"] == pytest.approx(1.20)

    # And it stops the DESK, not just this one caller: the very next call,
    # from any session, is refused before it can even be authorized.
    circuit.activate_session("run-next", "midday")
    with pytest.raises(PaidAnalysisSuspended):
        circuit.begin_call(
            agent_name="tech_analyst", model="google/gemini-3.5-flash-lite",
            system_prompt="s", user_message="u", max_output_tokens=100,
        )


def test_call_count_cap_triggers_correctly(tmp_path):
    """(c): a session that exceeds `max_calls_per_session` calls is
    stopped -- a runaway-loop guard by COUNT, independent of price. Every
    call here settles at exactly $0, so a dollar-based check could never
    catch this; only counting can."""
    path = _db_path(tmp_path)
    notifier = _Notifier()
    circuit = LLMCostCircuitBreaker(
        path, _config(max_calls_per_session=5), notifier,
    )
    circuit.activate_session("run-loop", "intra_check")
    for _ in range(5):
        reservation = circuit.begin_call(
            agent_name="tech_analyst", model="google/gemini-3.5-flash-lite",
            system_prompt="s", user_message="u", max_output_tokens=100,
        )
        circuit.before_provider_attempt(reservation, model=reservation.model)
        circuit.complete_call(reservation, 0.0, actual_model=reservation.model)

    with pytest.raises(PaidAnalysisSuspended) as excinfo:
        circuit.begin_call(
            agent_name="tech_analyst", model="google/gemini-3.5-flash-lite",
            system_prompt="s", user_message="u", max_output_tokens=100,
        )
    assert excinfo.value.state["trigger_code"] == "session_call_count_limit"
    assert circuit.status()["current_daily_cost_usd"] == 0
    with sqlite3.connect(path) as conn:
        logical_calls = conn.execute(
            "SELECT logical_calls FROM llm_budget_sessions WHERE run_id=?",
            ("run-loop",),
        ).fetchone()[0]
    assert logical_calls == 5


def test_a_call_is_no_longer_preemptively_blocked_by_an_unspent_reservation(tmp_path):
    """The actual defect item 14 replaces, pinned directly. Adapted from
    the deleted `test_projected_session_spend_blocks_before_provider_
    request`, which pinned the OLD, now-removed behavior: `begin_call`
    computed a conservative worst-case dollar RESERVATION from the prompt
    size and `max_output_tokens` alone, and refused the call outright if
    that estimate alone would have projected session/daily spend over a
    ("reserved-exposure") ceiling -- even though no money had been spent
    yet. Measured: real calls settled at a median 0.38x of that estimate,
    so the desk was stopped by money that was never spent (docs/WORK.md
    item 14).

    That old assertion is now WRONG on its own terms. Pinned here instead:
    a call with a large prompt and a large `max_output_tokens` ceiling --
    priced by the OLD formula alone at far more than the session limit --
    is authorized without hesitation, because nothing is estimated or
    reserved before the call. Only what the call actually, eventually
    settles at can ever stop the desk."""
    path = _db_path(tmp_path)
    notifier = _Notifier()
    # A limit an old worst-case reservation for this prompt/ceiling would
    # have blown through instantly: ~$1.86 for a ~259KB prompt at
    # openai/gpt-5.5 rates under the old byte=token/full-ceiling formula
    # (see the 2026-08-28 09:32 ET incident in docs/WORK.md item 14),
    # against a cap two orders of magnitude smaller.
    circuit = LLMCostCircuitBreaker(
        path, _config(session_cost_limit_usd=0.01, daily_cost_limit_usd=1.0), notifier,
    )
    circuit.activate_session("run-not-preemptively-blocked", "morning")

    # No PaidAnalysisSuspended here: item (b)'s settled-cost check has
    # nothing to compare against yet (this session has spent exactly $0
    # so far), and there is no reservation left to project a worst case
    # from at all.
    reservation = circuit.begin_call(
        agent_name="portfolio_manager", model="openai/gpt-5.5",
        system_prompt="S" * 48_000, user_message="U" * 210_744,
        max_output_tokens=16_000,
    )
    assert reservation.reservation_id != "disabled"
    assert circuit.status()["suspended"] is False
    assert notifier.messages == []

    # The real cap still bites once real money is actually reported.
    circuit.before_provider_attempt(reservation, model=reservation.model)
    circuit.complete_call(reservation, 0.02, actual_model=reservation.model)
    assert circuit.status()["trigger_code"] == "session_cost_limit"
