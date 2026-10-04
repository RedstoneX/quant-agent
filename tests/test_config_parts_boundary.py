"""Boundary witnesses for the `src/config/` split.

Each settings section lives in its own module and is a real boundary: the
module's own source imports no sibling config module (it cannot secretly
depend on the whole), and its classes are constructed and exercised here from
plain dicts, without building AppConfig or reading settings.yaml. Only the
composition root (`app`) may import siblings.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from src.config.broker import AlpacaConfig
from src.config.execution import ExecutionConfig
from src.config.llm_cost import LLMCostCircuitConfig, _paid_run_count
from src.config.risk import EventRiskConfig, RiskConfig

ROOT = Path(__file__).resolve().parent.parent
SECTIONS = ("broker", "llm", "execution", "risk", "research", "operations", "llm_cost")


def _sibling_imports(module: str) -> set[str]:
    tree = ast.parse((ROOT / "src" / "config" / f"{module}.py").read_text(encoding="utf-8"))
    found = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.ImportFrom) and n.module and n.module.startswith("src.config."):
            found.add(n.module)
        if isinstance(n, ast.Import):
            found.update(a.name for a in n.names if a.name.startswith("src.config"))
    return found


@pytest.mark.parametrize("module", SECTIONS)
def test_section_module_imports_no_sibling_section(module):
    assert _sibling_imports(module) == set(), f"src/config/{module}.py leans on a sibling section"


def test_app_is_the_only_composition_root():
    assert {m.rsplit(".", 1)[1] for m in _sibling_imports("app")} >= set(SECTIONS)


def test_init_is_only_the_mirror_block():
    tree = ast.parse((ROOT / "src/config/__init__.py").read_text(encoding="utf-8"))
    kinds = {type(n).__name__ for n in tree.body}
    assert kinds <= {"Expr", "ImportFrom"}, kinds  # docstring + re-exports, no bodies


def test_risk_section_builds_and_validates_alone():
    cfg = RiskConfig(max_position_pct=10, max_total_position_pct=100, max_sector_pct=30,
                     require_stop_loss=True)
    assert cfg.sector_hard_ceiling_pct == min(30 * RiskConfig.SECTOR_HARD_MULTIPLE,
                                          RiskConfig.SECTOR_HARD_CEILING_MAX)
    with pytest.raises(ValueError):
        RiskConfig(max_position_pct=0, max_total_position_pct=100, max_sector_pct=30,
                   require_stop_loss=True)


def test_broker_section_refuses_live_alone():
    paper = AlpacaConfig(base_url="https://paper-api.alpaca.markets", paper=True)
    assert paper.paper is True
    with pytest.raises(ValueError):
        AlpacaConfig(base_url="https://api.alpaca.markets", paper=False)


def test_cost_circuit_section_derives_from_defaults_alone():
    cfg = LLMCostCircuitConfig()
    assert cfg.max_calls_per_session >= _paid_run_count() > 0
    assert cfg.daily_cost_limit_usd >= cfg.session_cost_limit_usd


def test_execution_and_event_risk_sections_default_alone():
    assert ExecutionConfig().max_entry_slippage_bps > 0
    ev = EventRiskConfig()
    assert ev.fomc_deadline_s >= ev.fomc_request_timeout_s
