"""The broker module mirrors the sector cluster owned by src.sector_reference.

A patch of `src.execution.broker._get_sector` must rebind the object the
moved code (src/risk/rules.py's sector gate) actually calls. Without the
write-through `__setattr__` the patch would land in broker's own dict and the
gate would silently keep calling the real lookup.
"""

import src.execution.broker as broker
import src.sector_reference as sector_reference
from src.config import RiskConfig
from src.models import TradeDecision
from src.risk.rules import RiskRuleEngine


def test_patching_broker_get_sector_reaches_the_risk_gate(monkeypatch):
    calls = []

    def _spy(symbol):
        calls.append(symbol)
        return "Technology"

    monkeypatch.setattr(broker, "_get_sector", _spy)
    # The owner module's attribute IS the patched object (write-through)...
    assert sector_reference._get_sector is _spy
    # ...and the broker-side read resolves to the same object.
    assert broker._get_sector is _spy

    engine = RiskRuleEngine(
        RiskConfig(
            max_position_pct=20,
            max_total_position_pct=90,
            max_sector_pct=40,
            require_stop_loss=True,
        )
    )
    decision = TradeDecision(
        action="BUY",
        symbol="NVDA",
        allocation_pct=5.0,
        entry_price=850.0,
        stop_loss=810.0,
        take_profit=920.0,
        reasoning="Test",
    )
    engine.check(decision, positions=[], total_value=10000.0)
    assert "NVDA" in calls, "risk gate did not call the patched _get_sector"


def test_undo_restores_the_real_function(monkeypatch):
    real = sector_reference._get_sector
    with monkeypatch.context() as m:
        m.setattr(broker, "_get_sector", lambda s: "Energy")
        assert sector_reference._get_sector is not real
    assert sector_reference._get_sector is real
    assert broker._get_sector is real


def test_non_mirrored_names_still_set_normally(monkeypatch):
    monkeypatch.setattr(broker, "_BROKER_HTTP_TIMEOUT", 7.0)
    assert broker._BROKER_HTTP_TIMEOUT == 7.0
    assert not hasattr(sector_reference, "_BROKER_HTTP_TIMEOUT")


def test_sector_reference_imports_nothing_from_execution():
    import ast, pathlib

    tree = ast.parse(pathlib.Path(sector_reference.__file__).read_text())
    mods = [n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)]
    mods += [a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names]
    assert not [m for m in mods if m and (m.startswith("src.execution") or "adapter" in m)]
