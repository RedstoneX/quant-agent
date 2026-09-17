"""Never-blank soft-exit: stated strings survive; missing names are refused.

The #432 isolate-unknown-only gate is a temporary last-resort. The product
is: require a real thesis_invalid_if on actionable Tech and on
opens/increases before the ticket book; one mechanical heal + one paid
retry; if still incomplete, refuse that open name with
`soft-exit missing after retry`. Reductions and closes may omit the
field — a blank trim must still become SELL/COVER. Never invent a
falsifier or catalyst string. Catalyst stays optional.
"""
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError

from src.models import (
    SOFT_EXIT_MISSING_AFTER_RETRY,
    SOFT_EXIT_UNKNOWN,
    TargetPosition,
    TechAnalysisResult,
    TradeDecision,
    missing_stated_falsifier,
    stated_soft_exit,
)
from src.pipeline_context import RunContext
from src.pipeline_stages import _isolate_empty_soft_exit_entries
from src.seat_heal import merge_retry_falsifiers, restore_stated_soft_exits


def _trc() -> dict:
    return dict(trend="t", momentum="m", volatility="v", volume="vol",
                support_resistance="sr")


def _tech(**over) -> dict:
    base = dict(
        symbol="SPY", rating="buy", conviction="high",
        entry_price=500.0, stop_loss=490.0, reference_target=525.0,
        support_levels=[490.0], resistance_levels=[525.0],
        setup_type="range", expected_horizon_sessions=10,
        reasoning="x", reasoning_chain=_trc(),
        thesis_invalid_if="closes below 490",
    )
    base.update(over)
    return base


def test_stated_falsifier_and_catalyst_survive_assignment_before_risk():
    """A later size cap must not wipe stated soft-exits to blank."""
    target = TargetPosition(
        symbol="AAPL", risk_allocation_pct=2.0, conviction="medium",
        thesis="add", thesis_invalid_if="closes below 191.5",
        catalyst="2026-09-16 8-K",
    )
    target.risk_allocation_pct = 0.5
    assert target.thesis_invalid_if == "closes below 191.5"
    assert target.catalyst == "2026-09-16 8-K"
    decision = TradeDecision(
        action="BUY", symbol="AAPL", allocation_pct=3.0,
        entry_price=190.0, stop_loss=185.0, take_profit=205.0,
        reasoning="stated",
        thesis_invalid_if=stated_soft_exit(target.thesis_invalid_if) or None,
    )
    plan = SimpleNamespace(
        decisions=[decision],
        targets=[target],
        constructor_dropped=[],
    )
    pipeline = SimpleNamespace(db=MagicMock())
    ctx = RunContext.start("morning")
    isolated = _isolate_empty_soft_exit_entries(pipeline, ctx, plan)
    assert isolated == []
    assert plan.decisions[0].thesis_invalid_if == "closes below 191.5"
    assert plan.targets[0].catalyst == "2026-09-16 8-K"


def test_heal_restores_stated_falsifier_null_is_not_a_delete():
    values = {"thesis_invalid_if": None, "catalyst": ""}
    raw = {"thesis_invalid_if": "daily close below 191.5", "catalyst": "8-K"}
    out, restored = restore_stated_soft_exits(values, raw)
    assert out["thesis_invalid_if"] == "daily close below 191.5"
    assert out["catalyst"] == "8-K"
    assert "thesis_invalid_if" in restored
    # Heal never invents when the seat wrote nothing.
    empty, restored_empty = restore_stated_soft_exits(
        {"thesis_invalid_if": ""}, {"thesis_invalid_if": None},
    )
    assert empty["thesis_invalid_if"] == ""
    assert restored_empty == []


def test_empty_open_name_is_refused_with_soft_exit_missing_after_retry():
    """Last-resort refuse after heal+retry. Empty on a BUY is missing."""
    empty_buy = TradeDecision(
        action="BUY", symbol="MRVL", allocation_pct=3.0,
        entry_price=80.0, stop_loss=75.0, take_profit=90.0,
        reasoning="blank", thesis_invalid_if=None,
    )
    stated_buy = TradeDecision(
        action="BUY", symbol="AAPL", allocation_pct=3.0,
        entry_price=190.0, stop_loss=185.0, take_profit=205.0,
        reasoning="stated", thesis_invalid_if="closes below 185",
    )
    plan = SimpleNamespace(
        decisions=[empty_buy, stated_buy],
        targets=[
            TargetPosition(
                symbol="MRVL", risk_allocation_pct=1.0, thesis="retry",
                thesis_invalid_if="",
            ),
            TargetPosition(
                symbol="AAPL", risk_allocation_pct=1.0, thesis="add",
                thesis_invalid_if="closes below 185",
            ),
        ],
        constructor_dropped=[],
    )
    pipeline = SimpleNamespace(db=MagicMock())
    ctx = RunContext.start("morning")
    isolated = _isolate_empty_soft_exit_entries(pipeline, ctx, plan)
    assert isolated == ["MRVL"]
    assert [d.symbol for d in plan.decisions] == ["AAPL"]
    # Target stays on the proposal so Risk is told the name was refused,
    # not silently deleted from both lists.
    assert [t.symbol for t in plan.targets] == ["MRVL", "AAPL"]
    assert any(
        SOFT_EXIT_MISSING_AFTER_RETRY in str(c)
        for c in pipeline.db.insert_specialist_evidence.mock_calls
    )


def test_catalyst_stays_optional_on_a_non_zero_target():
    target = TargetPosition(
        symbol="NVDA", risk_allocation_pct=1.0, thesis="breakout",
        thesis_invalid_if="closes back inside the range",
        catalyst="",
    )
    assert target.catalyst == ""
    assert not target.missing_open_falsifier


def test_close_target_may_omit_falsifier():
    close = TargetPosition(
        symbol="AAPL", risk_allocation_pct=0.0, thesis="exit",
        thesis_invalid_if="",
    )
    assert close.is_close
    assert not close.missing_open_falsifier


def test_paid_retry_merge_does_not_change_size_or_invent_catalyst():
    original = [
        TargetPosition(
            symbol="AAPL", risk_allocation_pct=1.0, thesis="add",
            thesis_invalid_if="",
        ),
    ]
    retry = [{
        "symbol": "AAPL", "risk_allocation_pct": 5.0,
        "thesis_invalid_if": "closes below 191.5",
        "catalyst": "made-up 8-K",
    }]
    merged, filled = merge_retry_falsifiers(original, retry)
    assert filled == ["AAPL"]
    assert merged[0].risk_allocation_pct == 1.0
    assert merged[0].thesis_invalid_if == "closes below 191.5"
    assert merged[0].catalyst == ""


def test_actionable_tech_without_falsifier_does_not_validate():
    with pytest.raises(ValidationError, match="thesis_invalid_if"):
        TechAnalysisResult(**_tech(thesis_invalid_if=""))
    with pytest.raises(ValidationError, match="thesis_invalid_if"):
        TechAnalysisResult(**_tech(thesis_invalid_if=SOFT_EXIT_UNKNOWN))
    ok = TechAnalysisResult(**_tech())
    assert stated_soft_exit(ok.thesis_invalid_if)


def test_pm_fill_retry_copies_stated_falsifier_and_never_invents():
    """One paid seat retry fills an empty open target from the retry
    payload. Size and catalyst are untouched. A still-empty retry leaves
    the name missing for the book-entry refuse."""
    from src.agents.base import AgentResult
    from src.agents.portfolio_manager import PortfolioManagerAgent
    from src.models import PortfolioDecision, ReasoningChain

    def _plan(*, falsifier: str) -> PortfolioDecision:
        return PortfolioDecision(
            reasoning_chain=ReasoningChain(
                macro_filter="m", news_check="n", earnings_check="e",
                signal_conflicts="s", sizing_logic="z",
                portfolio_balance="b", cash_target="c",
            ),
            targets=[TargetPosition(
                symbol="AAPL", risk_allocation_pct=1.0, thesis="add",
                thesis_invalid_if=falsifier, catalyst="",
            )],
            portfolio_view="v",
        )

    agent = PortfolioManagerAgent.__new__(PortfolioManagerAgent)
    first = AgentResult(
        raw_text="{}", tokens_used=1, model="test",
        user_message="original user message",
    )
    retry_payload = {
        "targets": [{
            "symbol": "AAPL", "risk_allocation_pct": 9.0,
            "thesis_invalid_if": "closes below 191.5",
            "catalyst": "invented 8-K",
        }],
    }
    calls: list[tuple] = []

    def _execute(self, user_message, **kwargs):
        calls.append(
            (user_message, kwargs.get("retry_kind"), kwargs.get("optional_retry")),
        )
        import json
        return AgentResult(
            raw_text=json.dumps(retry_payload), tokens_used=1, model="test",
            user_message=user_message,
        )

    agent._execute = _execute.__get__(agent, PortfolioManagerAgent)
    filled, _ = agent._fill_missing_open_falsifiers(_plan(falsifier=""), first)
    assert [c[1] for c in calls] == ["soft_exit_fill"]
    assert calls[0][2] is True
    assert "SOFT-EXIT COMPLETION REQUIRED" in calls[0][0]
    assert filled.targets[0].thesis_invalid_if == "closes below 191.5"
    assert filled.targets[0].risk_allocation_pct == 1.0
    assert filled.targets[0].catalyst == ""

    agent2 = PortfolioManagerAgent.__new__(PortfolioManagerAgent)

    def _empty_retry(self, user_message, **kwargs):
        import json
        return AgentResult(
            raw_text=json.dumps({"targets": [{"symbol": "AAPL", "thesis_invalid_if": ""}]}),
            tokens_used=1, model="test", user_message=user_message,
        )

    agent2._execute = _empty_retry.__get__(agent2, PortfolioManagerAgent)
    still, _ = agent2._fill_missing_open_falsifiers(_plan(falsifier=""), first)
    assert still.targets[0].missing_open_falsifier
    assert still.targets[0].thesis_invalid_if == ""


def test_blank_open_target_is_refused_before_the_constructor():
    """A missing falsifier never consumes risk budget as a ticket."""
    from src.pipeline_stages import (
        _dropped_since_proposal, _targets_admitted_to_book,
    )

    blank = TargetPosition(
        symbol="MRVL", risk_allocation_pct=1.0, thesis="retry",
        thesis_invalid_if="",
    )
    stated = TargetPosition(
        symbol="AAPL", risk_allocation_pct=1.0, thesis="add",
        thesis_invalid_if="closes below 185",
    )
    admitted, refused = _targets_admitted_to_book([blank, stated])
    assert refused == ["MRVL"]
    assert [t.symbol for t in admitted] == ["AAPL"]
    plan = SimpleNamespace(
        decisions=[],
        targets=[blank, stated],
        constructor_dropped=["MRVL"],
    )
    assert "MRVL" in _dropped_since_proposal(plan)


def _held(symbol: str, qty: float, price: float, *, sector="Technology"):
    from src.models import Position
    return Position(
        symbol=symbol, qty=qty, avg_entry=price, current_price=price,
        market_value=qty * price, unrealized_pnl=0.0, sector=sector,
    )


def test_held_trim_with_blank_falsifier_is_still_admitted_and_sells():
    """Held long; PM emits a lower NON-ZERO risk target with blank
    thesis_invalid_if. That is a reduction — admit it, and construct_orders
    still builds a SELL. An open and an increase with the same blank field
    stay refused. Never invents a falsifier string.

    Live figures from intra_check-44594a05 (AAPL 1.91% → 1.0%).
    """
    from src.pipeline_stages import _targets_admitted_to_book
    from src.portfolio_constructor import PortfolioConstructor

    equity = 9_694.25
    qty, px, stop = 9.763, 332.96, 315.85
    held = _held("AAPL", qty, px)
    trim = TargetPosition(
        symbol="AAPL", risk_allocation_pct=1.0, thesis="trim to fund NET",
        thesis_invalid_if="",
    )
    open_blank = TargetPosition(
        symbol="NET", risk_allocation_pct=1.75, thesis="open NET",
        thesis_invalid_if="",
    )
    increase_blank = TargetPosition(
        symbol="AAPL", risk_allocation_pct=2.5, thesis="add",
        thesis_invalid_if="",
    )
    current_risk = {"AAPL": 1.91}

    admitted, refused = _targets_admitted_to_book(
        [trim, open_blank], positions=[held], total_value=equity,
        existing_risk_pct=current_risk,
    )
    assert [t.symbol for t in admitted] == ["AAPL"]
    assert refused == ["NET"]
    assert admitted[0].thesis_invalid_if == ""  # never invented

    _, refused_add = _targets_admitted_to_book(
        [increase_blank], positions=[held], total_value=equity,
        existing_risk_pct=current_risk,
    )
    assert refused_add == ["AAPL"]

    decisions = PortfolioConstructor().construct_orders(
        targets=admitted, positions=[held], analyses=[],
        total_value=equity, price_map={"AAPL": px},
        existing_risk_pct=current_risk, live_stops={"AAPL": stop},
    )
    sells = [d for d in decisions if d.action == "SELL" and d.symbol == "AAPL"]
    assert len(sells) == 1
    assert sells[0].thesis_invalid_if in (None, "")


def test_held_short_trim_with_blank_falsifier_is_still_admitted_and_covers():
    """Held short; lower non-zero risk target, blank thesis_invalid_if →
    COVER is still built. A new short with the same blank field is refused.
    """
    from src.pipeline_stages import _targets_admitted_to_book
    from src.portfolio_constructor import PortfolioConstructor

    equity = 100_000.0
    # 80 short @ $250, live stop $270: $20/share × 80 = $1,600 = 1.6% equity.
    # Target 0.8% keeps 40 shares → COVER 40, well above the 0.50pp floor.
    qty, px, stop = -80.0, 250.0, 270.0
    held = _held("TSLA", qty, px, sector="Consumer Cyclical")
    trim = TargetPosition(
        symbol="TSLA", direction="short", risk_allocation_pct=0.8,
        thesis="cover down", thesis_invalid_if="",
    )
    open_blank = TargetPosition(
        symbol="NET", direction="short", risk_allocation_pct=1.0,
        thesis="open short", thesis_invalid_if="",
    )
    current_risk = {"TSLA": 1.6}

    admitted, refused = _targets_admitted_to_book(
        [trim, open_blank], positions=[held], total_value=equity,
        existing_risk_pct=current_risk,
    )
    assert [t.symbol for t in admitted] == ["TSLA"]
    assert refused == ["NET"]
    assert admitted[0].thesis_invalid_if == ""

    decisions = PortfolioConstructor().construct_orders(
        targets=admitted, positions=[held], analyses=[],
        total_value=equity, price_map={"TSLA": px},
        existing_risk_pct=current_risk, live_stops={"TSLA": stop},
    )
    covers = [d for d in decisions if d.action == "COVER" and d.symbol == "TSLA"]
    assert len(covers) == 1
    assert covers[0].thesis_invalid_if in (None, "")


def test_legacy_weight_trim_with_blank_falsifier_is_still_admitted_and_sells():
    """Unsigned target_weight_pct below current long weight is a reduction
    even without a risk map — same exemption as a risk-based trim.
    """
    from src.pipeline_stages import _targets_admitted_to_book
    from src.portfolio_constructor import PortfolioConstructor

    held = _held("NVDA", 150, 100.0)  # 15% of $100k
    trim = TargetPosition(
        symbol="NVDA", target_weight_pct=10.0, thesis="trim to target",
        thesis_invalid_if="",
    )
    admitted, refused = _targets_admitted_to_book(
        [trim], positions=[held], total_value=100_000.0,
    )
    assert refused == []
    assert [t.symbol for t in admitted] == ["NVDA"]
    decisions = PortfolioConstructor().construct_orders(
        targets=admitted, positions=[held], analyses=[],
        total_value=100_000.0, price_map={"NVDA": 100.0},
    )
    sells = [d for d in decisions if d.action == "SELL" and d.symbol == "NVDA"]
    assert len(sells) == 1


def test_fill_retry_is_not_spent_on_a_held_trim():
    """A reduction is not an open — do not spend the paid fill retry, and
    do not invent thesis_invalid_if text.
    """
    from src.agents.base import AgentResult
    from src.agents.portfolio_manager import PortfolioManagerAgent
    from src.models import PortfolioDecision, ReasoningChain

    held = _held("AAPL", 100, 230.0)
    decision = PortfolioDecision(
        reasoning_chain=ReasoningChain(
            macro_filter="m", news_check="n", earnings_check="e",
            signal_conflicts="s", sizing_logic="z",
            portfolio_balance="b", cash_target="c",
        ),
        targets=[TargetPosition(
            symbol="AAPL", risk_allocation_pct=1.0, thesis="trim",
            thesis_invalid_if="", catalyst="",
        )],
        portfolio_view="v",
    )
    agent = PortfolioManagerAgent.__new__(PortfolioManagerAgent)
    first = AgentResult(raw_text="{}", tokens_used=1, model="test",
                        user_message="original user message")
    calls: list = []

    def _execute(self, user_message, **kwargs):
        calls.append(kwargs.get("retry_kind"))
        raise AssertionError("fill retry must not run for a reduction")

    agent._execute = _execute.__get__(agent, PortfolioManagerAgent)
    filled, _ = agent._fill_missing_open_falsifiers(
        decision, first, positions=[held], total_value=100_000.0,
        existing_risk_pct={"AAPL": 1.91},
    )
    assert calls == []
    assert filled.targets[0].thesis_invalid_if == ""
    assert getattr(agent, "_soft_exit_retry_used", False) is False


def test_blank_falsifier_buy_from_a_risk_down_target_is_still_isolated():
    """`_target_intent` can label a lower-risk target a reduction while a
    tighter stop converts it to more shares (BUY). Book-admit may let that
    target through so a genuine trim can become a SELL; isolate still
    drops the constructed BUY. Never invents a falsifier.
    """
    from src.pipeline_stages import (
        _isolate_empty_soft_exit_entries, _targets_admitted_to_book,
    )
    from src.portfolio_constructor import PortfolioConstructor

    equity = 100_000.0
    held = _held("NVDA", 100, 100.0)  # 10% weight
    trim = TargetPosition(
        symbol="NVDA", risk_allocation_pct=1.5, thesis="lower risk",
        thesis_invalid_if="",
    )
    # Current stop ~$80 (2% risk on 100 shares). Fresh stop $95 is tighter,
    # so 1.5% risk sizes MORE shares than 100 — a BUY, not a SELL.
    analysis = TechAnalysisResult(**_tech(
        symbol="NVDA", entry_price=100.0, stop_loss=95.0,
        reference_target=140.0, support_levels=[95.0],
        resistance_levels=[140.0], computed_levels=[95.0, 140.0],
        atr_14=5.0 / 3.5, thesis_invalid_if="closes below 95",
    ))
    admitted, refused = _targets_admitted_to_book(
        [trim], positions=[held], total_value=equity,
        existing_risk_pct={"NVDA": 2.0},
    )
    assert refused == []
    assert admitted[0].thesis_invalid_if == ""
    decisions = PortfolioConstructor().construct_orders(
        targets=admitted, positions=[held], analyses=[analysis],
        total_value=equity, price_map={"NVDA": 100.0},
        existing_risk_pct={"NVDA": 2.0},
    )
    buys = [d for d in decisions if d.action == "BUY" and d.symbol == "NVDA"]
    assert buys, "this fixture must produce a BUY so isolate is the backstop"
    assert missing_stated_falsifier(buys[0].thesis_invalid_if)
    plan = SimpleNamespace(
        decisions=list(decisions),
        targets=[trim],
        constructor_dropped=[],
    )
    pipeline = SimpleNamespace(db=MagicMock())
    ctx = RunContext.start("morning")
    ctx.positions = [held]
    ctx.total_value = equity
    isolated = _isolate_empty_soft_exit_entries(pipeline, ctx, plan)
    assert "NVDA" in isolated
    assert not any(d.action == "BUY" for d in plan.decisions)


def test_owner_missing_data_rule_forbids_skip_as_the_product():
    """Owner 2026-09-17: skip/drop/ignore is not the permanent product.
    Isolate stays labelled TEMPORARY. The producing step must fill."""
    from pathlib import Path
    repo = Path(__file__).resolve().parents[1]
    agents = (repo / "AGENTS.md").read_text()
    assert "Never make skip/drop/ignore-and-continue the permanent product" in agents
    work = (repo / "docs" / "WORK.md").read_text()
    assert "**78." in work and "TEMPORARY" in work
    outcome = (repo / "docs" / "OUTCOME.md").read_text()
    assert "Missing data is a defect in the step that should have produced it" in outcome

