"""The model benchmark runs on its OWN budget, never the live desk's caps.

Offline: no network, no model calls. Every paid path is stubbed.
"""

from __future__ import annotations

import hashlib
import importlib
from pathlib import Path
from types import SimpleNamespace

import pytest

bm = importlib.import_module("ops.model_policy.benchmark_models")
scenarios_mod = importlib.import_module("ops.model_policy.scenarios")

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SETTINGS = PROJECT_ROOT / "config" / "settings.yaml"


# --- --budget-usd ---------------------------------------------------------

def test_paid_run_without_budget_is_rejected(monkeypatch):
    # Must fail at argument handling, before any wiring or network.
    monkeypatch.setattr(bm, "openrouter_pricing",
                        lambda: pytest.fail("network touched before budget check"))
    with pytest.raises(SystemExit) as exc:
        bm.main(["--models", "x/y", "--scenario", "tech_batch"])
    assert exc.value.code == 2


@pytest.mark.parametrize("bad", ["0", "-1", "nan", "inf", "abc"])
def test_budget_rejects_non_positive_or_non_finite(bad):
    with pytest.raises(SystemExit):
        bm.main(["--budget-usd", bad, "--models", "x/y"])


def test_budget_accepts_positive():
    assert bm.positive_budget("2.5") == 2.5


def test_report_needs_no_budget(tmp_path):
    import json
    f = tmp_path / "r.json"
    f.write_text(json.dumps({"trials": []}))
    assert bm.main(["--report", str(f)]) == 0


# --- breaker config is a copy ---------------------------------------------

def _load_real_config(monkeypatch):
    for key in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "OPENROUTER_API_KEY",
                "GOOGLE_API_KEY", "FRED_API_KEY", "ALPACA_API_KEY",
                "ALPACA_SECRET_KEY", "DEEPSEEK_API_KEY"):
        monkeypatch.setenv(key, "placeholder-for-test")
    from src.config import load_config
    return load_config(SETTINGS)


def test_breaker_session_cap_is_budget_and_live_config_untouched(tmp_path, monkeypatch):
    settings_hash = hashlib.sha256(SETTINGS.read_bytes()).hexdigest()
    app_config = _load_real_config(monkeypatch)
    live_before = app_config.llm_cost_circuit.model_dump()
    live_obj = app_config.llm_cost_circuit

    from src import cost_table
    monkeypatch.setattr(cost_table, "refresh_openrouter_pricing", lambda **_: True)
    from src.storage.db import Database
    db_path = tmp_path / "bench.db"
    db = Database(str(db_path))
    db.initialize()
    db.conn.close()

    class _Notifier:
        enabled = True

        def send(self, _message):
            return True

    trials = 6 * 5 * 2
    budget = round(live_before["daily_cost_limit_usd"] - 0.01, 2)
    assert budget != live_before["session_cost_limit_usd"]
    breaker = bm.build_benchmark_circuit(
        app_config, budget_usd=budget, planned_trials=trials,
        run_id="bench-test", notifier=_Notifier(), db_path=str(db_path),
    )
    assert breaker.config.session_cost_limit_usd == budget
    assert breaker.config.max_calls_per_session == trials * bm.logical_calls_per_trial_worst()
    # Everything else inherited, notably the daily cap.
    assert breaker.config.daily_cost_limit_usd == live_before["daily_cost_limit_usd"]
    assert (breaker.config.max_provider_attempts_per_call
            == live_before["max_provider_attempts_per_call"])

    assert app_config.llm_cost_circuit is live_obj
    assert app_config.llm_cost_circuit.model_dump() == live_before
    assert hashlib.sha256(SETTINGS.read_bytes()).hexdigest() == settings_hash


def test_calls_per_trial_counts_every_retry_kind():
    from src.agents.tech_analyst import _MAX_MISSING_RETRIES
    # primary + schema_repair + missing-symbol recoveries + consolidated recovery
    assert bm.logical_calls_per_trial_worst() == 3 + _MAX_MISSING_RETRIES


# --- daily cap -------------------------------------------------------------

def test_budget_above_daily_cap_fails_fast_without_raising_it():
    live = SimpleNamespace(daily_cost_limit_usd=2.75, session_cost_limit_usd=0.9,
                           max_calls_per_session=40)
    with pytest.raises(SystemExit) as exc:
        bm.benchmark_circuit_config(live, budget_usd=3.0, planned_trials=10)
    assert "daily" in str(exc.value) and "does not raise" in str(exc.value)
    assert live.daily_cost_limit_usd == 2.75 and live.session_cost_limit_usd == 0.9


def test_daily_cap_headroom_refuses_before_spending():
    msg = bm.daily_cap_problem({"current_daily_cost_usd": 2.00}, 2.75, 1.00)
    assert msg and "not raised" in msg
    assert bm.daily_cap_problem({"current_daily_cost_usd": 0.50}, 2.75, 1.00) is None


# --- budget exhaustion -----------------------------------------------------

def _trial(scenario, model, cost):
    return bm.Trial(model=model, scenario=scenario.key, role=scenario.role,
                    ok=True, quality=1.0, input_tokens=100, output_tokens=100,
                    cost_usd=cost)


def _budget_exhausted_report():
    scen = [scenarios_mod.SCENARIOS_BY_KEY["tech_batch"],
            scenarios_mod.SCENARIOS_BY_KEY["macro_stress"]]
    models = ["a/first", "b/never-called"]
    spent = {"usd": 0.0}
    calls: list[str] = []

    def run_one(s, m):
        calls.append(m)
        spent["usd"] += 0.6
        return _trial(s, m, 0.6)

    trials, stopped = bm.run_sweep(
        models, scen, 1, budget_usd=1.0, run_one=run_one,
        budget_state=lambda: (spent["usd"], False), log=lambda _m: None,
    )
    return trials, stopped, calls


def test_budget_exhaustion_marks_remaining_not_run():
    trials, stopped, calls = _budget_exhausted_report()
    assert stopped is True
    assert calls == ["a/first", "a/first"]
    assert [t.status for t in trials] == [
        "run", "run", "skipped_budget", "skipped_budget",
    ]


def test_summary_renders_not_run_not_zero():
    trials, _, _ = _budget_exhausted_report()
    table = bm.render_markdown(bm.aggregate(trials))
    row = next(line for line in table.splitlines() if "b/never-called" in line)
    assert "NOT RUN" in row
    assert "0.00" not in row
    first = next(line for line in table.splitlines() if "a/first" in line)
    assert "1.00" in first


def test_breaker_refusal_with_no_tokens_is_not_run_not_failure():
    scen = [scenarios_mod.SCENARIOS_BY_KEY["tech_batch"]]

    def refused(s, m):
        return bm.Trial(model=m, scenario=s.key, role=s.role, ok=False,
                        quality=0.0, error="PaidAnalysisSuspended: session cap")

    trials, stopped = bm.run_sweep(
        ["x/y"], scen, 2, budget_usd=1.0, run_one=refused,
        budget_state=lambda: (1.01, True), log=lambda _m: None,
    )
    assert stopped and all(t.status == "skipped_budget" for t in trials)


# --- estimate vs budget ---------------------------------------------------

def test_estimate_over_budget_refuses_without_allow_partial(monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://stub")
    monkeypatch.setattr(bm, "load_env_if_keys_missing", lambda: False)
    monkeypatch.setattr(bm, "openrouter_pricing",
                        lambda: {"x/y": {"input": 1000.0, "output": 1000.0}})
    monkeypatch.setattr(bm, "build_benchmark_circuit",
                        lambda *a, **k: pytest.fail("breaker built despite refusal"))
    monkeypatch.setattr(bm, "run_trial",
                        lambda *a, **k: pytest.fail("model called despite refusal"))
    argv = ["--models", "x/y", "--scenario", "earnings_filing", "--budget-usd", "0.01"]
    assert bm.main(argv) == 2


def test_estimate_over_budget_proceeds_with_allow_partial(monkeypatch, tmp_path):
    monkeypatch.setenv("HTTPS_PROXY", "http://stub")
    monkeypatch.setattr(bm, "load_env_if_keys_missing", lambda: False)
    monkeypatch.setattr(bm, "openrouter_pricing",
                        lambda: {"x/y": {"input": 1000.0, "output": 1000.0}})
    import src.config as config_mod
    live_cfg = SimpleNamespace(daily_cost_limit_usd=100.0, session_cost_limit_usd=0.9,
                               max_calls_per_session=40)
    monkeypatch.setattr(config_mod, "load_config",
                        lambda _p: SimpleNamespace(llm_cost_circuit=live_cfg))
    circuit = SimpleNamespace(
        config=SimpleNamespace(session_cost_limit_usd=0.01, max_calls_per_session=4),
        require_paid_analysis=lambda _n: None,
        status=lambda: {"current_daily_cost_usd": 0.0,
                        "current_session_cost_usd": 0.0, "suspended": False},
    )
    monkeypatch.setattr(bm, "build_benchmark_circuit", lambda *a, **k: circuit)
    scen = scenarios_mod.SCENARIOS_BY_KEY["earnings_filing"]
    monkeypatch.setattr(bm, "run_trial",
                        lambda s, m, p, cost_circuit=None: _trial(s, m, 0.0))
    out = tmp_path / "out.json"
    argv = ["--models", "x/y", "--scenario", scen.key, "--budget-usd", "0.01",
            "--allow-partial", "--out", str(out)]
    assert bm.main(argv) == 0
    assert live_cfg.session_cost_limit_usd == 0.9


# --- .env -----------------------------------------------------------------

def test_env_loaded_only_when_keys_missing(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("# c\nexport OPENROUTER_API_KEY=placeholder\nFRED_API_KEY='p2'\n")
    for key in bm._REQUIRED_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("FRED_API_KEY", "already-set")
    assert bm.load_env_if_keys_missing(env) is True
    import os
    assert os.environ["OPENROUTER_API_KEY"] == "placeholder"
    assert os.environ["FRED_API_KEY"] == "already-set"

    for key in bm._REQUIRED_ENV_KEYS:
        monkeypatch.setenv(key, "set")
    assert bm.load_env_if_keys_missing(env) is False


# --- macro fixture ----------------------------------------------------------

def test_pm_production_macro_fixture_validates():
    from src.models import MacroAnalysis
    parsed = MacroAnalysis.model_validate(scenarios_mod._PM_PRODUCTION_MACRO)
    assert parsed.regime == "risk-on"
