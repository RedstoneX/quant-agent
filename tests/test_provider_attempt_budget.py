"""The circuit's per-call attempt ceiling must cover the retry loop's worst case.

THE INCIDENT THIS FILE EXISTS FOR (2026-08-31)
----------------------------------------------
`BaseAgent.run()` takes up to `_max_retries()` attempts at an agent's primary
model and then, if the primary is not itself Anthropic and a fallback key is
configured, ONE cross-provider failover. Worst case: three provider attempts.

`llm_cost_circuit.max_provider_attempts_per_call` was pinned by hand at two.

So every failover attempt was attempt three against a ceiling of two, and the
circuit stopped it. Not sometimes — always, and only in the case failover
exists to handle: a retryable primary failure (429/5xx/timeout) burns both
permitted attempts before failover is even reached. Cross-provider failover
could never once complete. On Monday 2026-08-31 an upstream rate-limit on the
primary triggered it at 09:32 ET, two minutes after the open, and because
`provider_attempt_limit` was also the one trip code wired to the durable
operator-reset latch, paid analysis stayed off for the rest of the day over
$0.05 of a $2.75 budget.

The two numbers lived in different worlds — an env-overridable module
constant in `src/agents/base.py`, a YAML setting read by `src/cost_circuit.py`
— which is how they drifted apart for six days across five separate circuit
trips without anyone noticing. These tests bind them together so they cannot
drift again silently.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from src.agents.base import _max_retries, provider_attempt_budget
from src.cost_circuit import (
    LLMCostCircuitBreaker,
    PaidAnalysisSuspended,
    _trigger_scope,
)
from src.storage.db import Database

SETTINGS = Path(__file__).resolve().parents[1] / "config" / "settings.yaml"


class _Notifier:
    def __init__(self):
        self.messages: list[str] = []

    def send(self, message: str) -> bool:
        self.messages.append(message)
        return True


def _config(**overrides):
    values = {
        "enabled": True,
        "session_cost_limit_usd": 10.0,
        "daily_cost_limit_usd": 20.0,
        "max_free_failure_sessions_per_mode": 8,
        "max_provider_attempts_per_call": provider_attempt_budget(
            failover_available=True
        ),
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


# --------------------------------------------------------------- arithmetic


def test_budget_covers_primary_attempts_plus_one_failover():
    assert provider_attempt_budget(failover_available=True) == _max_retries() + 1


def test_budget_drops_the_failover_attempt_when_no_failover_is_possible():
    """An Anthropic-primary agent never fails over (Claude to Claude is
    pointless), so it cannot spend the extra attempt."""
    assert provider_attempt_budget(failover_available=False) == _max_retries()


@pytest.mark.parametrize("retries", [1, 2, 3, 7])
def test_budget_tracks_the_env_override_the_retry_loop_actually_reads(
    monkeypatch, retries
):
    """`.env` on the box carries a commented-out QUANT_AGENT_MAX_RETRIES=7.
    Uncommenting it must move the circuit's ceiling too, not silently
    recreate the 2026-08-31 mismatch three attempts wider."""
    monkeypatch.setenv("QUANT_AGENT_MAX_RETRIES", str(retries))
    assert provider_attempt_budget(failover_available=True) == retries + 1


# ------------------------------------------------------------ configuration


def _load_settings_with(ceiling):
    from src.config import AppConfig, _walk_and_substitute

    raw = yaml.safe_load(SETTINGS.read_text())
    if ceiling is None:
        raw["llm_cost_circuit"].pop("max_provider_attempts_per_call", None)
    else:
        raw["llm_cost_circuit"]["max_provider_attempts_per_call"] = ceiling
    return AppConfig(**_walk_and_substitute(raw))


@pytest.fixture
def _keys(monkeypatch):
    for var in (
        "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "DEEPSEEK_API_KEY",
        "OPENROUTER_API_KEY", "ALPACA_API_KEY", "ALPACA_SECRET_KEY",
        "FRED_API_KEY", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID",
    ):
        monkeypatch.setenv(var, "test-key")


def test_shipped_settings_ceiling_covers_the_retry_loop(_keys):
    """The regression guard proper: whatever config/settings.yaml says today,
    it must not be below what one call can spend."""
    config = _load_settings_with(None)
    assert (
        config.llm_cost_circuit.max_provider_attempts_per_call
        >= provider_attempt_budget(failover_available=True)
    )


def test_settings_does_not_re_pin_the_ceiling(_keys):
    """Pinning it in YAML is what broke; the value belongs to the code that
    spends the attempts."""
    raw = yaml.safe_load(SETTINGS.read_text())
    assert "max_provider_attempts_per_call" not in raw["llm_cost_circuit"]


def test_config_refuses_a_ceiling_below_the_retry_loops_worst_case(_keys):
    """The exact broken configuration must now fail at load rather than at
    09:32 on a Monday."""
    with pytest.raises(ValueError) as excinfo:
        _load_settings_with(provider_attempt_budget(failover_available=True) - 1)
    message = str(excinfo.value)
    assert "max_provider_attempts_per_call" in message
    assert "cross-provider failover" in message


def test_config_allows_a_ceiling_above_the_worst_case(_keys):
    """Raising it is a legitimate operator choice and grants no extra
    attempts — the retry loop, not this ceiling, decides how many requests
    happen."""
    config = _load_settings_with(9)
    assert config.llm_cost_circuit.max_provider_attempts_per_call == 9


# ----------------------------------------------------------------- the trip


def _reserve(circuit, *, agent="tech_analyst", model="google/gemini-2.5-flash-lite"):
    return circuit.begin_call(
        agent_name=agent,
        model=model,
        system_prompt="system",
        user_message="input",
        max_output_tokens=1_000,
    )


def test_failover_attempt_is_permitted_under_the_derived_ceiling(tmp_path):
    """The 2026-08-31 sequence, end to end at the circuit boundary: two
    primary attempts are rate-limited, and the third — the failover — must be
    ALLOWED THROUGH rather than trip the circuit."""
    circuit = LLMCostCircuitBreaker(_db_path(tmp_path), _config(), _Notifier())
    circuit.activate_session("run-failover", "morning")
    reservation = _reserve(circuit)

    circuit.before_provider_attempt(reservation, model="google/gemini-2.5-flash-lite")
    circuit.before_provider_attempt(reservation, model="google/gemini-2.5-flash-lite")
    # The failover: a different provider and a dearer model, priced as such.
    circuit.before_provider_attempt(reservation, model="claude-opus-4-7")

    assert circuit.status()["suspended"] in (0, False)


def test_ceiling_still_stops_a_genuine_attempt_runaway(tmp_path):
    """Raising the ceiling to cover failover must not remove the stop. One
    attempt past the budget is still stopped — it is only no longer the
    loop's own designed behaviour that trips it."""
    circuit = LLMCostCircuitBreaker(_db_path(tmp_path), _config(), _Notifier())
    circuit.activate_session("run-runaway", "morning")
    reservation = _reserve(circuit)

    budget = provider_attempt_budget(failover_available=True)
    for _ in range(budget):
        circuit.before_provider_attempt(reservation, model="claude-opus-4-7")

    with pytest.raises(PaidAnalysisSuspended):
        circuit.before_provider_attempt(reservation, model="claude-opus-4-7")


def test_attempt_limit_holds_the_session_instead_of_latching_the_desk(tmp_path):
    """Defect 5: `provider_attempt_limit` was the only trip code of its family
    left on the durable operator-reset latch, so a transient upstream fault
    cost a whole trading day and needed a human to clear. It is now scoped to
    the session that caused it, like its wider sibling
    `session_retry_attempt_limit` — later sessions stay eligible."""
    assert _trigger_scope("provider_attempt_limit") == "session"
    assert _trigger_scope("session_retry_attempt_limit") == "session"

    notifier = _Notifier()
    circuit = LLMCostCircuitBreaker(_db_path(tmp_path), _config(), notifier)
    circuit.activate_session("run-held", "morning")
    reservation = _reserve(circuit)
    for _ in range(provider_attempt_budget(failover_available=True)):
        circuit.before_provider_attempt(reservation, model="claude-opus-4-7")
    with pytest.raises(PaidAnalysisSuspended):
        circuit.before_provider_attempt(reservation, model="claude-opus-4-7")

    # `status()` reports the EFFECTIVE state for the calling session, which a
    # session hold does suspend — that is the point of the hold. What must not
    # be set is the durable singleton latch: the row that outlives the session,
    # darkens every later one, and requires a human to clear.
    with sqlite3.connect(circuit.db_path, uri=True) as conn:
        latched = conn.execute(
            "SELECT suspended FROM llm_circuit_state WHERE singleton=1"
        ).fetchone()[0]
    assert not latched

    alert = "\n".join(notifier.messages)
    assert "QUOTA HOLD" in alert
    assert "no operator reset is required" in alert
    assert "scope: run run-held only" in alert
    assert "later independent sessions remain eligible" in alert


def test_a_later_session_still_runs_after_an_attempt_limit_hold(tmp_path):
    """The operative consequence: the next scheduled session is not collateral
    damage. This is the difference between a degraded morning and a dark
    trading day."""
    path = _db_path(tmp_path)
    circuit = LLMCostCircuitBreaker(path, _config(), _Notifier())
    circuit.activate_session("run-held", "morning")
    reservation = _reserve(circuit)
    for _ in range(provider_attempt_budget(failover_available=True)):
        circuit.before_provider_attempt(reservation, model="claude-opus-4-7")
    with pytest.raises(PaidAnalysisSuspended):
        circuit.before_provider_attempt(reservation, model="claude-opus-4-7")

    later = LLMCostCircuitBreaker(path, _config(), _Notifier())
    later.activate_session("run-next", "intra_check")
    assert _reserve(later) is not None


# ------------------------------------- failover WITH the circuit attached

# WHY THIS SECTION IS THE POINT OF THE FILE
#
# `tests/test_base_agent.py` already covers cross-provider failover, and every
# one of those tests passed throughout the six days this defect was live. They
# could not have caught it: `tests/conftest.py` sets
# `BaseAgent._allow_unmetered_for_tests = True` for the whole suite, so those
# agents run with NO cost circuit attached at all. And `test_cost_circuit.py`
# exercises the circuit with no failover.
#
# So the suite proved failover works without the circuit, and the circuit
# works without failover, and nothing anywhere ran the two together — which is
# precisely where they contradicted each other. These tests attach a real
# breaker to a real agent and make the primary fail for real reasons.


class _Rate429(Exception):
    """Shaped like a provider rate-limit: `_is_retryable` reads `status_code`."""

    status_code = 429


class _CircuitAgent:
    """Built in the test rather than imported so the agent under test is
    unambiguously running the production `run()` with a live breaker."""


def _agent_with_circuit(circuit, **kwargs):
    from tests.test_base_agent import ConcreteAgent

    agent = ConcreteAgent(
        api_key="k", model="google/gemini-2.5-flash-lite", max_tokens=64,
        fallback_api_key="fk", provider="openrouter", **kwargs
    )
    agent.set_cost_circuit(circuit)
    return agent


def test_rate_limited_primary_fails_over_with_a_live_circuit(tmp_path, monkeypatch):
    """The whole incident, reproduced against the real breaker.

    Two rate-limited primary attempts, then the failover — which under the old
    ceiling of 2 was attempt 3 and tripped the circuit instead of rescuing the
    session. It must now succeed, and the run must come back priced at the
    model that actually answered.
    """
    from unittest.mock import MagicMock, patch

    from tests.test_base_agent import _good_anthropic_response

    monkeypatch.setattr("time.sleep", lambda s: None)
    monkeypatch.setenv("QUANT_AGENT_MAX_RETRIES", "2")
    monkeypatch.setattr("src.agents.base.BaseAgent._allow_unmetered_for_tests", False)

    circuit = LLMCostCircuitBreaker(_db_path(tmp_path), _config(), _Notifier())
    circuit.activate_session("run-live-failover", "morning")

    openrouter = MagicMock()
    openrouter.chat.completions.create.side_effect = _Rate429("rate-limited upstream")
    anthropic_client = MagicMock()
    anthropic_client.messages.create.return_value = _good_anthropic_response()

    with patch("openai.OpenAI", return_value=openrouter), \
            patch("anthropic.Anthropic", return_value=anthropic_client):
        agent = _agent_with_circuit(circuit)
        result = agent.run(data="x")

    assert result.raw_text == '{"result": "ok"}'
    assert result.model == "claude-opus-4-7"
    assert openrouter.chat.completions.create.call_count == 2   # primary exhausted
    anthropic_client.messages.create.assert_called_once()       # single-shot failover

    with sqlite3.connect(circuit.db_path, uri=True) as conn:
        latched = conn.execute(
            "SELECT suspended FROM llm_circuit_state WHERE singleton=1"
        ).fetchone()[0]
    assert not latched, "a transient rate-limit must not latch the desk"


def test_a_failover_that_also_fails_does_not_latch_the_desk(tmp_path, monkeypatch):
    """The worse case: both providers are down. The session cannot produce
    analysis and must not pretend otherwise — but tomorrow's sessions, and the
    next half-hour's, are not the outage's to take."""
    from unittest.mock import MagicMock, patch

    monkeypatch.setattr("time.sleep", lambda s: None)
    monkeypatch.setenv("QUANT_AGENT_MAX_RETRIES", "2")
    monkeypatch.setattr("src.agents.base.BaseAgent._allow_unmetered_for_tests", False)

    circuit = LLMCostCircuitBreaker(_db_path(tmp_path), _config(), _Notifier())
    circuit.activate_session("run-both-down", "morning")

    openrouter = MagicMock()
    openrouter.chat.completions.create.side_effect = _Rate429("primary down")
    anthropic_client = MagicMock()
    anthropic_client.messages.create.side_effect = _Rate429("fallback down too")

    with patch("openai.OpenAI", return_value=openrouter), \
            patch("anthropic.Anthropic", return_value=anthropic_client):
        agent = _agent_with_circuit(circuit)
        with pytest.raises(Exception):
            agent.run(data="x")

    with sqlite3.connect(circuit.db_path, uri=True) as conn:
        latched = conn.execute(
            "SELECT suspended FROM llm_circuit_state WHERE singleton=1"
        ).fetchone()[0]
    assert not latched
