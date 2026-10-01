"""Boundary tests for the six prompt-fact families (docs/ARCHITECTURE.md §3, clause 5).

Step 10 (second half) of the conversion, board item 210. Each family class is
built ALONE with explicit stand-ins for only the collaborators it declares and
exercised through one of its builders — no `TradingPipeline` and no `PromptFacts`
composite is constructed, and this file must never import either. A family that
cannot pass here has not been split, only renamed.
"""
from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace

from src.prompt_facts_book import BookFacts
from src.prompt_facts_candidates import CandidateFacts
from src.prompt_facts_grading import GradingFacts
from src.prompt_facts_market_context import MarketContextFacts
from src.prompt_facts_projection import ProjectionFacts
from src.prompt_facts_seat_memory import SeatMemoryFacts


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

    def get_recent_insights(self, limit: int = 7):
        return [
            {"date": "2026-01-05", "tomorrow_outlook": "stay long", "risk_rating": "LOW"},
            {"date": "2026-01-06", "lessons": "size smaller", "risk_rating": "HIGH"},
        ]

    def get_daily_pnl(self, limit: int = 14):
        return [{"date": "2026-01-06", "daily_return_pct": -1.25}]


def _positions():
    return [
        SimpleNamespace(symbol="AAA", qty=10.0, avg_entry=100.0, current_price=110.0),
        SimpleNamespace(symbol="BBB", qty=-5.0, avg_entry=50.0, current_price=48.0),
    ]


def test_book_family_builds_the_stop_map_from_its_two_ports_alone():
    broker, db = _Broker({"AAA": 95.0, "BBB": None}), _Db()
    book = BookFacts(db=db, broker=broker)
    assert book._build_stop_map(_positions()) == ({"AAA": 95.0}, {})
    assert broker.asked == ["AAA", "BBB"]
    assert db.asked == [("AAA", "BUY"), ("BBB", "SHORT")]
    assert not hasattr(book, "market") and not hasattr(book, "news_store")


def test_projection_family_memoizes_the_correlation_matrix_on_the_context():
    market_calls: list[tuple[str, int]] = []

    class _Market:
        def get_ohlcv(self, symbol: str, lookback: int):
            market_calls.append((symbol, lookback))
            return []

    config = SimpleNamespace(trading=SimpleNamespace(lookback_days=30))
    projection = ProjectionFacts(market=_Market(), config=config)
    ctx = SimpleNamespace(symbols_bars={}, correlation_matrix=None)
    first = projection._ensure_correlation_matrix(ctx, _positions()[:1])
    assert market_calls == [("AAA", 30)]
    assert ctx.correlation_matrix == first
    ctx.correlation_matrix = {"AAA": {"BBB": 0.5}}
    assert projection._ensure_correlation_matrix(ctx, _positions()) == {"AAA": {"BBB": 0.5}}
    assert market_calls == [("AAA", 30)], "a cached matrix must not hit the market again"
    assert projection.portfolio_constructor is None and projection.risk_engine is None


def test_seat_memory_family_renders_the_weekly_narrative_from_the_db_alone():
    memory = SeatMemoryFacts(db=_Db())
    lines = memory._build_weekly_narrative().splitlines()
    assert lines == [
        "- 2026-01-06: -1.25% (HIGH) — size smaller",
        "- 2026-01-05: n/a (LOW) — stay long",
    ]
    assert not hasattr(memory, "broker")


def test_grading_family_prefers_broker_confirmed_fills_without_any_port():
    grading = GradingFacts()
    row = grading._actualize_trade_row({"qty": 1, "price": 2, "fill_qty": "3", "fill_price": "4.5"})
    assert row == {"qty": 3.0, "price": 4.5, "fill_qty": "3", "fill_price": "4.5"}
    assert not hasattr(grading, "db")


def test_market_context_family_reads_the_macro_trajectory_from_its_store_alone():
    class _MacroStore:
        def load_history(self, days: int):
            return [{"date": "2026-01-05", "regime": "risk-on", "confidence": "high",
                     "equity_outlook": "bullish"}]

    context = MarketContextFacts(macro_store=_MacroStore())
    assert context._build_macro_trajectory() == "- 2026-01-05: risk-on (high) → outlook bullish"
    assert not hasattr(context, "db")


def test_candidates_family_reads_the_macro_sector_map_from_its_store_alone():
    class _MacroStore:
        def load_last_state(self):
            return {"sector_guidance": {"Energy": "bullish", "Tech": "sideways", 3: "bearish"}}

    candidates = CandidateFacts(macro_store=_MacroStore())
    assert candidates._missed_ops_macro_sector_map() == {"Energy": "bullish", "3": "bearish"}
    assert not hasattr(candidates, "db") and not hasattr(candidates, "broker")


def test_every_family_declares_only_the_ports_its_bodies_read():
    """Measured, not asserted: the keyword ports of each family's constructor
    must equal the `self.<port>` names its method bodies actually read."""
    import inspect

    port_attr = {
        "sweeper": "_sweeper", "parse_logged_agent_response": "_parse_logged_agent_response",
        "atr_for_symbol": "_atr_for_symbol", "exit_audit_actions": "_EXIT_AUDIT_ACTIONS",
        "last_symbol_sectors": "_last_symbol_sectors",
    }
    for family in (BookFacts, ProjectionFacts, SeatMemoryFacts, GradingFacts,
                   MarketContextFacts, CandidateFacts):
        declared = {port_attr.get(p, p) for p in inspect.signature(family).parameters}
        tree = ast.parse(inspect.getsource(family))
        read = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "self":
                read.add(node.attr)
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "getattr"
                    and node.args and isinstance(node.args[0], ast.Name) and node.args[0].id == "self"
                    and isinstance(node.args[1], ast.Constant)):
                read.add(node.args[1].value)
        methods = {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
        assert declared == read - methods, (family.__name__, declared ^ (read - methods))


def test_this_file_never_imports_the_pipeline_or_the_composite():
    tree = ast.parse(Path(__file__).read_text(encoding="utf-8"))
    modules = {
        node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
    } | {alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names}
    assert "src.pipeline" not in modules
    assert "src.pipeline_prompt_facts" not in modules
    assert len(modules & {m for m in modules if m.startswith("src.prompt_facts_")}) == 6
