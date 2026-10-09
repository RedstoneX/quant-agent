"""Boundary witnesses for the `src/config/` split.

Each moved settings section is a real boundary: its module imports no other
config module (it cannot secretly depend on the whole), and its classes are
constructed and exercised here from plain dicts, without AppConfig or
settings.yaml. The package root keeps exactly three bodies, each for a
measured reason: AlpacaConfig/ApiKeysConfig (the paper lock point, where the
live-capital pre-flight gate is called; the gate no longer imports config
back, the audit check's probe is injected), RiskConfig (one 516-line class, above the 400-line floor for
a new file), and AppConfig with its loaders (needs both).
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from src.config.execution import ExecutionConfig
from src.config.llm_cost import LLMCostCircuitConfig, _paid_run_count
from src.config.risk_adjuncts import CashReserveConfig, EventRiskConfig
from src.config.smart_money import SmartMoneyConfig

ROOT = Path(__file__).resolve().parent.parent
SECTIONS = ("llm", "execution", "risk_adjuncts", "research", "smart_money", "operations", "llm_cost")
ROOT_BODIES = {
    "ApiKeysConfig",
    "AlpacaConfig",
    "RiskConfig",
    "AppConfig",
    "_substitute_env_vars",
    "_walk_and_substitute",
    "load_config",
}


def _tree(module: str) -> ast.Module:
    return ast.parse((ROOT / "src" / "config" / f"{module}.py").read_text(encoding="utf-8"))


def _config_imports(module: str) -> set[str]:
    found = set()
    for n in ast.walk(_tree(module)):
        if isinstance(n, ast.ImportFrom) and n.module and n.module.startswith("src.config"):
            found.add(n.module)
        if isinstance(n, ast.Import):
            found.update(a.name for a in n.names if a.name.startswith("src.config"))
    return found


@pytest.mark.parametrize("module", SECTIONS)
def test_section_module_imports_no_other_config_module(module):
    assert _config_imports(module) == set(), f"src/config/{module}.py leans on another config module"


def test_root_keeps_only_the_three_pinned_bodies():
    defs = {n.name for n in _tree("__init__").body if isinstance(n, (ast.ClassDef, ast.FunctionDef))}
    assert defs == ROOT_BODIES, defs


def test_root_mirrors_every_section():
    assert {m.rsplit(".", 1)[1] for m in _config_imports("__init__")} >= set(SECTIONS)


def test_cost_circuit_section_derives_from_defaults_alone():
    cfg = LLMCostCircuitConfig()
    assert cfg.max_calls_per_session >= _paid_run_count() > 0
    assert cfg.daily_cost_limit_usd >= cfg.session_cost_limit_usd
    with pytest.raises(ValueError):
        LLMCostCircuitConfig(daily_cost_limit_usd=1.0, session_cost_limit_usd=2.0)


def test_risk_adjunct_sections_validate_alone():
    ev = EventRiskConfig()
    assert ev.fomc_deadline_s >= ev.fomc_request_timeout_s
    with pytest.raises(ValueError):
        EventRiskConfig(fomc_deadline_s=1.0, fomc_request_timeout_s=5.0)
    assert 0 <= CashReserveConfig().pct <= 100


def test_execution_and_smart_money_sections_default_alone():
    assert ExecutionConfig().max_entry_slippage_bps > 0
    assert SmartMoneyConfig().requests_per_second > 0
