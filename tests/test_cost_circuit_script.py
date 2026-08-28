"""`scripts/cost_circuit.py` command dispatch (Defect 3).

2026-08-28: an operator ran `scripts/cost_circuit.py reset --reason ...` to
clear a hard-latched circuit and it never reached `reset()`. `main()` called
the validating `breaker.activate_session(...)` unconditionally before
dispatching on the command, and that call re-seeds/re-validates the current
day's accounting ledger -- exactly the kind of fault the operator was trying
to clear. It raised, uncaught, and the operator worked around it by hand
(importing the breaker in a Python shell and calling `.reset()` directly)
instead of using the documented tool.

These tests reproduce the failure with a real, deterministic ledger fault
(not a hypothetical): construct a breaker normally (its own first seed is
internally consistent and passes), then simulate a concurrent writer -- the
file's own docstring notes the morning and intraday jobs are separate
processes sharing this DB -- corrupting today's ledger in the narrow window
between that first seed and any later call that re-validates it. That is
exactly the race `_validate_accounting_invariants` exists to catch, and
exactly what the old dispatch order could not survive.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import scripts.cost_circuit as cost_circuit_script
from src.cost_circuit import LLMCostCircuitBreaker
from tests.test_cost_circuit import _Notifier, _config, _db_path


def _corrupt_ledger_and_latch(path: str) -> None:
    """Simulate a concurrent process corrupting today's settled-cost ledger
    and hard-latching the circuit -- the state an operator's `reset` is
    meant to clear."""
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE llm_budget_days SET incremental_cost_usd=5.0")
        conn.execute(
            "UPDATE llm_circuit_state SET suspended=1, "
            "trigger_code='failed_call_unknown_cost', "
            "trigger_detail='simulated hard latch pending operator reset', "
            "session_attempts=1 WHERE singleton=1"
        )


def _patch_script_config(monkeypatch, db_path: str, cfg) -> None:
    monkeypatch.setattr(
        cost_circuit_script, "load_config",
        lambda _path: SimpleNamespace(
            storage=SimpleNamespace(db_path=db_path),
            llm_cost_circuit=cfg,
        ),
    )


def test_activate_session_raises_raw_on_a_corrupted_ledger(tmp_path):
    """Documents the underlying mechanism the two tests below rely on: a
    ledger fault surfacing between two `_seed_today` calls on the same,
    already-constructed breaker raises directly, uncaught -- construction's
    own blanket try/except cannot absorb a failure that happens after it
    already returned."""
    path = _db_path(tmp_path)
    breaker = LLMCostCircuitBreaker(path, _config(), _Notifier())
    _corrupt_ledger_and_latch(path)

    with pytest.raises(RuntimeError, match="settled-cost ledgers disagree"):
        breaker.activate_session("operator-cost-check", "operator")


def test_reset_command_no_longer_blocked_by_the_fault_it_clears(tmp_path, monkeypatch):
    """OLD behaviour reproduced directly above; here the fixed script must
    reach `reset()` and report success even though the pre-existing
    `activate_session()` path would still raise on this exact DB."""
    path = _db_path(tmp_path)
    cfg = _config()
    breaker = LLMCostCircuitBreaker(path, cfg, _Notifier())
    _corrupt_ledger_and_latch(path)

    # Confirm the trap is still live: the old, unconditional activation call
    # this script no longer makes for `reset` would still raise here.
    with pytest.raises(RuntimeError, match="settled-cost ledgers disagree"):
        breaker.activate_session("operator-cost-check", "operator")

    # `main()` constructs its own breaker; hand it this already-corrupted
    # instance instead of re-running construction (construction's own
    # try/except would otherwise absorb the fault into a sentinel and mask
    # exactly the race this test exists to reproduce).
    monkeypatch.setattr(cost_circuit_script, "LLMCostCircuitBreaker", lambda *a, **k: breaker)
    _patch_script_config(monkeypatch, path, cfg)
    monkeypatch.setattr(
        sys, "argv",
        ["cost_circuit.py", "reset", "--reason", "operator verified and repaired ledger"],
    )

    rc = cost_circuit_script.main()

    assert rc == 0
    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        state = conn.execute(
            "SELECT * FROM llm_circuit_state WHERE singleton=1"
        ).fetchone()
    assert state["suspended"] == 0
    assert state["reset_reason"] == "operator verified and repaired ledger"


def test_reset_reports_status_error_without_losing_the_reset(tmp_path, monkeypatch, capsys):
    """The corrupted ledger itself is not repaired by `reset()` (it only
    clears the latch/audit row) -- the very next validating `status()` call
    still fails closed. The script must say so plainly and still exit 0,
    not lose the fact that the reset itself already committed behind an
    uncaught traceback."""
    path = _db_path(tmp_path)
    cfg = _config()
    breaker = LLMCostCircuitBreaker(path, cfg, _Notifier())
    _corrupt_ledger_and_latch(path)

    monkeypatch.setattr(cost_circuit_script, "LLMCostCircuitBreaker", lambda *a, **k: breaker)
    _patch_script_config(monkeypatch, path, cfg)
    monkeypatch.setattr(
        sys, "argv",
        ["cost_circuit.py", "reset", "--reason", "operator investigating further"],
    )

    rc = cost_circuit_script.main()

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["reset"] is True
    assert payload["reset_reason"] == "operator investigating further"
    assert "settled-cost ledgers disagree" in payload["status_error"]
    # The reset itself is durable regardless of the still-broken ledger.
    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        state = conn.execute(
            "SELECT * FROM llm_circuit_state WHERE singleton=1"
        ).fetchone()
    assert state["suspended"] == 0


def test_status_and_check_commands_still_validate_accounting_invariants(tmp_path, monkeypatch):
    """The fix is scoped to `reset` only -- `status`/`check` must keep
    failing closed on a corrupted ledger exactly as before."""
    path = _db_path(tmp_path)
    cfg = _config()
    breaker = LLMCostCircuitBreaker(path, cfg, _Notifier())
    _corrupt_ledger_and_latch(path)

    monkeypatch.setattr(cost_circuit_script, "LLMCostCircuitBreaker", lambda *a, **k: breaker)
    _patch_script_config(monkeypatch, path, cfg)

    for command in ("status", "check"):
        monkeypatch.setattr(sys, "argv", ["cost_circuit.py", command])
        with pytest.raises(RuntimeError, match="settled-cost ledgers disagree"):
            cost_circuit_script.main()


def test_reset_without_reason_is_rejected_before_touching_the_breaker(tmp_path, monkeypatch):
    """A missing --reason must still be rejected -- and rejected before any
    breaker/DB work, so a malformed reset command can't itself become a
    half-run accounting operation."""
    path = _db_path(tmp_path)
    cfg = _config()

    def _explode(*_a, **_k):
        raise AssertionError("LLMCostCircuitBreaker must not be constructed")

    monkeypatch.setattr(cost_circuit_script, "LLMCostCircuitBreaker", _explode)
    _patch_script_config(monkeypatch, path, cfg)
    monkeypatch.setattr(sys, "argv", ["cost_circuit.py", "reset"])

    with pytest.raises(SystemExit):
        cost_circuit_script.main()
