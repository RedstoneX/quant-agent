"""Boundary test for `PromptFacts` (docs/ARCHITECTURE.md §3, clause 5).

Step 10 (first half) of the conversion, board item 210. `PromptFacts` is built
with explicit stand-ins for its ports and exercised through public builders —
no pipeline object is constructed, and this file must never import one.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from src.pipeline_prompt_facts import PromptFacts, prompt_facts_for


class _Broker:
    def __init__(self, stops: dict[str, float | None]) -> None:
        self.stops = stops
        self.asked: list[str] = []

    def get_current_stop_price(self, symbol: str):
        self.asked.append(symbol)
        return self.stops.get(symbol)


class _Db:
    def __init__(self) -> None:
        self.asked: list[tuple[str, str]] = []

    def get_symbol_last_buy(self, symbol: str, action: str = "BUY"):
        self.asked.append((symbol, action))
        return None


def _facts(broker: _Broker, db: _Db, sweeper=lambda: None) -> PromptFacts:
    return PromptFacts(
        db=db, broker=broker, market=object(), news_store=object(),
        macro_store=object(), earnings_provider=object(), config=object(),
        tech_store=object(), sweeper=sweeper,
        parse_logged_agent_response=lambda row: None,
        atr_for_symbol=lambda symbol: None,
        exit_audit_actions=("SELL",),
    )


def _positions():
    return [
        SimpleNamespace(symbol="AAA", qty=10.0, avg_entry=100.0, current_price=110.0),
        SimpleNamespace(symbol="BBB", qty=-5.0, avg_entry=50.0, current_price=48.0),
    ]


def test_stop_map_is_built_from_the_injected_broker_and_db_alone():
    broker = _Broker({"AAA": 95.0, "BBB": None})
    db = _Db()
    live, initial = _facts(broker, db)._build_stop_map(_positions())
    assert live == {"AAA": 95.0}
    assert initial == {}
    assert broker.asked == ["AAA", "BBB"]
    assert db.asked == [("AAA", "BUY"), ("BBB", "SHORT")]


def test_portfolio_heat_excludes_the_sweep_vehicle_named_by_the_injected_sweeper():
    broker = _Broker({"AAA": 95.0, "SGOV": None})
    positions = _positions()[:1] + [
        SimpleNamespace(symbol="SGOV", qty=800.0, avg_entry=100.5, current_price=100.6),
    ]
    heat = _facts(broker, _Db(), sweeper=lambda: SimpleNamespace(symbol="sgov"))._build_portfolio_heat(positions, 25_000.0)
    assert heat is not None and heat.equity == 25_000.0
    assert [row.symbol for row in heat.per_position] == ["AAA"]


def test_portfolio_heat_degrades_to_none_when_the_sweeper_port_raises():
    def _boom():
        raise RuntimeError("no sweeper")
    assert _facts(_Broker({}), _Db(), sweeper=_boom)._build_portfolio_heat(_positions(), 1.0) is None


def test_absent_ports_stay_absent_so_getattr_guards_keep_their_meaning():
    facts = PromptFacts(db=_Db())
    assert not hasattr(facts, "config") and not hasattr(facts, "broker")
    assert getattr(facts, "config", None) is None
    assert facts.portfolio_constructor is None and facts.risk_engine is None


def test_factory_reads_ports_off_any_owner_without_a_pipeline():
    owner = SimpleNamespace(db=_Db(), broker=_Broker({"AAA": 91.0}), _sweeper=lambda: None)
    facts = prompt_facts_for(owner)
    assert facts.db is owner.db and facts.broker is owner.broker
    assert facts._build_stop_map(_positions()[:1]) == ({"AAA": 91.0}, {})


def test_this_file_never_imports_the_pipeline():
    """Clause 5 of the boundary definition, checked on this file's own AST."""
    import ast
    tree = ast.parse(Path(__file__).read_text(encoding="utf-8"))
    modules = {
        node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
    } | {alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names}
    assert "src.pipeline" not in modules
    assert modules & {"src.pipeline_prompt_facts"}
