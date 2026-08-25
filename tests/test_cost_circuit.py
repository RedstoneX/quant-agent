"""Mandatory paid-analysis circuit: accounting, persistence and cutoffs."""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from src.agents.base import BaseAgent
from src.cost_circuit import LLMCostCircuitBreaker, PaidAnalysisSuspended, _trigger_scope
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
        "max_paid_sessions_per_mode_per_day": 2,
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

    assert client.messages.create.call_count == 2
    state = circuit.status()
    assert state["trigger_code"] == "provider_attempt_limit"
    assert state["provider_attempts"] == 2
    assert len(notifier.messages) == 1
    assert "attempts: 2 provider attempts" in notifier.messages[0]
    assert "includes conservative or unresolved-request accounting" in notifier.messages[0]


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


def test_daily_reservation_is_atomic_across_process_objects(tmp_path):
    path = _db_path(tmp_path)
    cfg = _config(
        session_cost_limit_usd=0.0007,
        daily_cost_limit_usd=0.0007,
        max_paid_sessions_per_mode_per_day=10,
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


def test_third_paid_session_in_same_mode_is_blocked(tmp_path):
    path = _db_path(tmp_path)
    notifier = _Notifier()
    circuit = LLMCostCircuitBreaker(
        path, _config(max_paid_sessions_per_mode_per_day=2), notifier,
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
    with pytest.raises(PaidAnalysisSuspended):
        circuit.begin_call(
            agent_name="tech_analyst",
            model="google/gemini-2.5-flash-lite",
            system_prompt="s", user_message="u", max_output_tokens=100,
        )
    assert circuit.status()["trigger_code"] == "session_retry_limit"
    assert "paid session attempt 3" in notifier.messages[0]
    assert circuit.status()["hold_scope"] == "mode_day"

    circuit.activate_session("run-other-mode", "midday")
    reservation = circuit.begin_call(
        agent_name="tech_analyst",
        model="google/gemini-2.5-flash-lite",
        system_prompt="s", user_message="u", max_output_tokens=100,
    )
    assert reservation.reservation_id != "disabled"

    circuit.activate_session("run-four", "morning")
    with pytest.raises(PaidAnalysisSuspended):
        circuit.require_paid_analysis("same_mode_again")


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
        ("session_retry_limit", "mode_day"),
        ("session_cost_limit", "session"),
        ("projected_session_cost_limit", "session"),
        ("provider_projected_session_cost_limit", "session"),
        ("outstanding_projected_session_cost_limit", "session"),
        ("session_retry_attempt_limit", "session"),
        ("provider_attempt_limit", "hard"),
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
