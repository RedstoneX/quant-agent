"""A payment refusal is TERMINAL, and the desk says so in plain English.

Measured against the production database on 2026-10-01: the paid research
account ran out of credit, the provider answered HTTP 402 ("This request
requires more credits, or fewer max_tokens. You requested up to 16000
tokens, but can only afford 843") and the affordable figure fell across
successive calls (13290, 7311, 843, 811, 775) -- an emptying balance, not a
transient refusal. The desk nonetheless burned 12 provider attempts for
portfolio_manager and 9 for tech_analyst on the same dead account, then
suspended paid analysis reporting "the real cost is unknown and cannot be
bounded safely" when the truth was simply that the account was empty. The
owner read that wording and reasonably concluded the desk was broken.

Board item 211.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from src.agents.base import (
    BACKOFF_FATAL,
    _is_retryable,
    classify_backoff,
)
from src.cost_circuit import (
    OUT_OF_CREDIT_TRIGGER_CODE,
    LLMCostCircuitBreaker,
    any_payment_refusal,
    is_payment_refusal,
)
from tests.test_cost_circuit import (
    _Agent,
    _Notifier,
    _age_latch_past_self_clear_window,
    _config,
    _db_path,
)


def _err(status=None, message="boom", cls=RuntimeError):
    exc = cls(message)
    if status is not None:
        exc.status_code = status
    return exc


# --- classification: the STATUS CODE decides, never the English ------------


def test_402_is_a_payment_refusal_whatever_the_wording():
    assert is_payment_refusal(_err(402, "This request requires more credits"))
    # A provider may reword the message at any time; the answer must not move.
    assert is_payment_refusal(_err(402, "zahlung erforderlich"))
    assert is_payment_refusal(_err(402, ""))


def test_402_in_the_cause_chain_still_counts():
    inner = _err(402, "out of credits")
    outer = RuntimeError("wrapped")
    outer.__cause__ = inner
    assert is_payment_refusal(outer)


def test_429_credit_verification_is_not_a_payment_refusal():
    """A 429 saying credits could not be VERIFIED is genuinely transient --
    the provider could not check the balance, not that it is gone -- so it
    keeps every retry it has today."""
    exc = _err(429, "credits could not be verified, please retry")
    assert not is_payment_refusal(exc)
    assert _is_retryable(exc)
    assert classify_backoff(exc)[0] != BACKOFF_FATAL


def test_payment_refusal_is_never_retried():
    exc = _err(402, "can only afford 843")
    assert not _is_retryable(exc)
    assert classify_backoff(exc) == (BACKOFF_FATAL, None)


def test_any_payment_refusal_scans_the_whole_attempt_list():
    attempts = [_err(503, "busy"), _err(402, "no credit")]
    assert any_payment_refusal(_err(503, "busy"), attempts)
    assert not any_payment_refusal(_err(503, "busy"), [_err(503, "busy")])


# --- the loop: one attempt, no failover onto the same dead account ---------


def _run_with_402(tmp_path, monkeypatch, *, retries="3"):
    monkeypatch.setenv("QUANT_AGENT_MAX_RETRIES", retries)
    notifier = _Notifier()
    circuit = LLMCostCircuitBreaker(_db_path(tmp_path), _config(), notifier)
    circuit.activate_session("run-402", "morning")
    client = MagicMock()
    # No "can only afford N": the provider named no servable allowance, so
    # there is nothing to re-ask at and the refusal is terminal at once.
    client.messages.create.side_effect = _err(
        402,
        "This request requires more credits.",
    )
    with patch("anthropic.Anthropic", return_value=client):
        agent = _Agent(api_key="x", model="claude-sonnet-4-6", max_tokens=64)
        agent.set_cost_circuit(circuit)
        # Routes 2 and 3 land on the SAME account as the primary: an
        # account-level refusal cannot be escaped by changing the model.
        agent._fallback_provider = agent._provider
        agent._tertiary_provider = agent._provider
        failover = MagicMock(return_value=None)
        tertiary = MagicMock(return_value=None)
        agent._try_failover = failover
        agent._try_tertiary = tertiary
        with pytest.raises(Exception):
            agent.run()
    return circuit, notifier, client, failover, tertiary


def test_402_demotes_on_the_first_attempt_without_spending_the_budget(
    tmp_path,
    monkeypatch,
):
    circuit, _, client, failover, tertiary = _run_with_402(tmp_path, monkeypatch)
    # ONE provider attempt, with three retries configured and two further
    # rungs available. A terminal error has no retry count to pick.
    assert client.messages.create.call_count == 1
    assert circuit.status()["provider_attempts"] == 1
    failover.assert_not_called()
    tertiary.assert_not_called()


def test_the_suspension_says_the_account_is_out_of_credit(tmp_path, monkeypatch):
    circuit, notifier, _, _, _ = _run_with_402(tmp_path, monkeypatch)
    state = circuit.status()
    assert state["trigger_code"] == OUT_OF_CREDIT_TRIGGER_CODE
    detail = str(state["trigger_detail"]).lower()
    assert "out of credit" in detail
    assert "top the account up" in detail
    # The old, misleading wording must be gone: the cause is known.
    assert "cannot be bounded safely" not in detail
    _age_latch_past_self_clear_window(circuit)
    assert any("out of credit" in m.lower() for m in notifier.messages)


def test_a_non_payment_failure_keeps_the_unknown_cost_wording(
    tmp_path,
    monkeypatch,
):
    """The honest-cause rename must not swallow failures whose cost really
    IS unknown."""
    monkeypatch.setenv("QUANT_AGENT_MAX_RETRIES", "1")
    notifier = _Notifier()
    circuit = LLMCostCircuitBreaker(_db_path(tmp_path), _config(), notifier)
    circuit.activate_session("run-unknown", "morning")
    client = MagicMock()
    client.messages.create.side_effect = ConnectionError("stream cut")
    with patch("anthropic.Anthropic", return_value=client):
        agent = _Agent(api_key="x", model="claude-sonnet-4-6", max_tokens=64)
        agent.set_cost_circuit(circuit)
        with pytest.raises(ConnectionError):
            agent.run()
    assert circuit.status()["trigger_code"] == "failed_call_unknown_cost"
