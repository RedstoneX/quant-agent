"""Risk-based position sizing through the constructor — spec §2.1, §2.2, §2.4.

The failure this replaces: `target_weight_pct` is risk-blind. "BUY OKLO 3%"
says nothing about what the trade costs when it is wrong — a 3% position
stopped 10% away risks 0.3% of equity, the same 3% stopped 2% away risks
0.06%. The PM was choosing the number that does not determine the loss, while
the number that does (the stop distance) was set by the technical analyst.

Under §2.1 conviction is stated as risk and the stop determines the size, so a
wider stop produces a smaller position instead of a rejected trade. That is
what removes the incentive to squeeze stops, which is what was firing exits
inside ordinary noise.
"""

from src.models import Position, TargetPosition, TechAnalysisResult, TechReasoningChain
from src.portfolio_constructor import ConstructorConfig, PortfolioConstructor


def _tech_rc() -> TechReasoningChain:
    return TechReasoningChain(
        trend="x", momentum="x", volatility="x", volume="x", support_resistance="x",
    )


def _analysis(symbol: str, entry: float, stop: float, target: float) -> TechAnalysisResult:
    return TechAnalysisResult(
        symbol=symbol, rating="buy", entry_price=entry, stop_loss=stop,
        reference_target=target, reasoning="test",
        support_levels=[stop], resistance_levels=[target],
        setup_type="range", expected_horizon_sessions=10,
        reasoning_chain=_tech_rc(),
    )


def _pos(symbol: str, qty: float, avg_entry: float, current_price: float) -> Position:
    return Position(
        symbol=symbol, qty=qty, avg_entry=avg_entry, current_price=current_price,
        market_value=qty * current_price,
        unrealized_pnl=(current_price - avg_entry) * qty, sector="Technology",
    )


def _risk_target(symbol: str, risk_pct: float, **kw) -> TargetPosition:
    return TargetPosition(
        symbol=symbol, risk_allocation_pct=risk_pct, conviction="high",
        thesis=kw.pop("thesis", "thesis"), **kw,
    )


EQUITY = 100_000.0


# --------------------------------------------------------------------------
# §2.1 — the stop sets the size
# --------------------------------------------------------------------------

def test_size_derives_from_risk_and_stop_distance():
    """shares = (equity x risk_pct) / |entry - stop|.

    2% of $100k = $2,000 at risk. Entry $100, stop $90 → $10/share → 200
    shares → $20,000 → 20% of the book.
    """
    constructor = PortfolioConstructor()
    decisions = constructor.construct_orders(
        targets=[_risk_target("NVDA", 2.0)],
        positions=[], analyses=[_analysis("NVDA", entry=100, stop=90, target=140)],
        total_value=EQUITY, price_map={"NVDA": 100.0},
    )
    assert len(decisions) == 1
    assert abs(decisions[0].allocation_pct - 20.0) < 0.05


def test_a_wider_stop_yields_a_smaller_position_not_a_rejected_trade():
    """The whole point of §2.1. Same conviction, same equity, twice the stop
    distance — half the position, and both trades risk exactly 2%."""
    constructor = PortfolioConstructor()

    def alloc(stop):
        d = constructor.construct_orders(
            targets=[_risk_target("NVDA", 2.0)], positions=[],
            analyses=[_analysis("NVDA", entry=100, stop=stop, target=140)],
            total_value=EQUITY, price_map={"NVDA": 100.0},
        )
        return d[0].allocation_pct

    tight, wide = alloc(95), alloc(90)
    assert abs(tight - 40.0) < 0.05
    assert abs(wide - 20.0) < 0.05
    # Both put the same dollars at risk — that is the invariant §2.1 buys.
    assert abs(tight / 100 * EQUITY * 0.05 - 2000) < 20   # 5/100 stop gap
    assert abs(wide / 100 * EQUITY * 0.10 - 2000) < 20    # 10/100 stop gap


def test_a_position_with_no_structural_stop_produces_no_order():
    """Risk cannot be sized without a stop, and stops are no longer
    synthesized (Phase 1). No stop is no trade, not a guessed one."""
    constructor = PortfolioConstructor()
    analysis = _analysis("NVDA", entry=100, stop=90, target=140)
    analysis = analysis.model_copy(update={"stop_loss": 0.0})
    decisions = constructor.construct_orders(
        targets=[_risk_target("NVDA", 2.0)], positions=[], analyses=[analysis],
        total_value=EQUITY, price_map={"NVDA": 100.0},
    )
    assert decisions == []


def test_single_name_risk_is_clamped_to_the_ratified_envelope():
    """5% per trade is the owner-ratified ceiling (2026-08-27). A PM asking
    for more is clamped rather than refused — the idea is sound, the size is
    not. The schema caps at 5 too; this is the deterministic backstop."""
    constructor = PortfolioConstructor(ConstructorConfig(risk_budget_pct=3.0))
    decisions = constructor.construct_orders(
        targets=[_risk_target("NVDA", 5.0)], positions=[],
        analyses=[_analysis("NVDA", entry=100, stop=90, target=140)],
        total_value=EQUITY, price_map={"NVDA": 100.0},
    )
    # Clamped to 3% risk → 3000/10 = 300 shares → $30k → 30%.
    assert abs(decisions[0].allocation_pct - 30.0) < 0.05


def test_zero_risk_closes_a_held_position():
    """`risk_allocation_pct == 0` is "exit this name", and must reach the sell
    builder rather than being swallowed by the churn filter — the 2026-07-16
    audit finding, which must survive the move to risk units."""
    constructor = PortfolioConstructor()
    decisions = constructor.construct_orders(
        targets=[_risk_target("AAPL", 0.0, thesis="thesis broken")],
        positions=[_pos("AAPL", qty=50, avg_entry=180, current_price=200)],
        analyses=[], total_value=EQUITY, price_map={"AAPL": 200.0},
    )
    assert len(decisions) == 1
    assert decisions[0].action == "SELL"
    assert decisions[0].allocation_pct == 100.0


def test_a_close_does_not_need_a_price_or_a_stop():
    """Routing an exit through the pricing checks would let a missing quote
    silently cancel a decision the PM had already made."""
    constructor = PortfolioConstructor()
    decisions = constructor.construct_orders(
        targets=[_risk_target("AAPL", 0.0)],
        positions=[_pos("AAPL", qty=50, avg_entry=180, current_price=200)],
        analyses=[], total_value=EQUITY, price_map={},
    )
    assert len(decisions) == 1 and decisions[0].action == "SELL"


def test_legacy_notional_targets_still_size_the_old_way():
    """Historical agent_logs and specialist_evidence rows re-validate through
    TargetPosition (src/replay.py, the Mission Control API). A stored decision
    carrying only `target_weight_pct` must still construct."""
    constructor = PortfolioConstructor()
    decisions = constructor.construct_orders(
        targets=[TargetPosition(symbol="NVDA", target_weight_pct=8.0,
                                conviction="high", thesis="legacy")],
        positions=[], analyses=[_analysis("NVDA", entry=100, stop=95, target=115)],
        total_value=EQUITY, price_map={"NVDA": 100.0},
    )
    assert len(decisions) == 1
    assert abs(decisions[0].allocation_pct - 8.0) < 0.05


# --------------------------------------------------------------------------
# §2.2 — the portfolio budget, enforced
# --------------------------------------------------------------------------

NUCLEAR = ["CCJ", "CEG", "OKLO", "VST"]


def _nuclear_setup(*symbols):
    targets = [_risk_target(s, 5.0) for s in symbols]
    analyses = [_analysis(s, entry=100, stop=90, target=140) for s in symbols]
    prices = {s: 100.0 for s in symbols}
    return targets, analyses, prices


def test_a_single_theme_cannot_take_the_whole_risk_budget():
    """Four nuclear names at 5% risk each is 20% total — under the 25%
    ceiling, and one 20% bet. The cluster cap must cut it to 10%."""
    constructor = PortfolioConstructor()
    targets, analyses, prices = _nuclear_setup(*NUCLEAR)
    decisions = constructor.construct_orders(
        targets=targets, positions=[], analyses=analyses,
        total_value=EQUITY, price_map=prices,
        existing_risk_pct={}, clusters=[NUCLEAR],
    )
    # Each granted 5% risk becomes a 50% notional weight at a 10% stop gap, so
    # count orders rather than weights: only two names survive the 10% cap.
    assert {d.symbol for d in decisions} == {"CCJ", "CEG"}


def test_the_budget_gate_is_inert_when_the_caller_supplies_no_book_risk():
    """Without `existing_risk_pct`/`clusters` the constructor has no view of
    the book's risk and must not invent one. Per-position sizing still
    applies; the portfolio ceilings do not."""
    constructor = PortfolioConstructor()
    targets, analyses, prices = _nuclear_setup(*NUCLEAR)
    decisions = constructor.construct_orders(
        targets=targets, positions=[], analyses=analyses,
        total_value=EQUITY, price_map=prices,
    )
    assert len(decisions) == 4


def test_held_risk_crowds_out_a_new_name_in_the_same_theme():
    constructor = PortfolioConstructor()
    decisions = constructor.construct_orders(
        targets=[_risk_target("OKLO", 5.0)], positions=[],
        analyses=[_analysis("OKLO", entry=100, stop=90, target=140)],
        total_value=EQUITY, price_map={"OKLO": 100.0},
        existing_risk_pct={"CEG": 8.0}, clusters=[NUCLEAR],
    )
    # 10% cluster cap less CEG's 8% = 2% risk → 2000/10 = 200 sh → $20k → 20%.
    assert abs(decisions[0].allocation_pct - 20.0) < 0.05


def test_a_budget_cut_is_explained_in_the_order_reasoning():
    """The AI Risk Manager audits the constructed order against the PM's
    prose. On 2026-08-20 an unexplained deterministic cut read as "plan
    inconsistency" and drew a full-plan veto."""
    constructor = PortfolioConstructor()
    decisions = constructor.construct_orders(
        targets=[_risk_target("OKLO", 5.0)], positions=[],
        analyses=[_analysis("OKLO", entry=100, stop=90, target=140)],
        total_value=EQUITY, price_map={"OKLO": 100.0},
        existing_risk_pct={"CEG": 8.0}, clusters=[NUCLEAR],
    )
    reasoning = decisions[0].reasoning
    assert "risk budget" in reasoning
    assert "cut from 5.00% to 2.00%" in reasoning
    assert "not PM inconsistency" in reasoning


def test_a_full_budget_denies_a_new_name_outright():
    constructor = PortfolioConstructor()
    decisions = constructor.construct_orders(
        targets=[_risk_target("NVDA", 5.0)], positions=[],
        analyses=[_analysis("NVDA", entry=100, stop=90, target=140)],
        total_value=EQUITY, price_map={"NVDA": 100.0},
        existing_risk_pct={"HELD": 25.0}, clusters=[],
    )
    assert decisions == []


def test_released_risk_frees_budget_for_a_new_position():
    """Spec §2.3/§2.4: a position whose stop has reached entry contributes
    zero budget risk, so the book expands while trades work — with nobody
    choosing a position count. The caller computes the release
    (`src/risk/metrics.py`); the constructor simply spends what is left."""
    constructor = PortfolioConstructor()
    common = dict(
        targets=[_risk_target("NVDA", 5.0)], positions=[],
        analyses=[_analysis("NVDA", entry=100, stop=90, target=140)],
        total_value=EQUITY, price_map={"NVDA": 100.0}, clusters=[],
    )
    blocked = constructor.construct_orders(existing_risk_pct={"WINNER": 25.0}, **common)
    freed = constructor.construct_orders(existing_risk_pct={"WINNER": 0.0}, **common)
    assert blocked == []
    assert len(freed) == 1
    assert abs(freed[0].allocation_pct - 50.0) < 0.05


# --------------------------------------------------------------------------
# Wiring — the gate has to reach the constructor, and the cap the PM
# --------------------------------------------------------------------------

def test_book_risk_inputs_convert_heat_into_percent_of_equity():
    """The constructor rations in % of equity; heat is measured in dollars."""
    from types import SimpleNamespace

    from src.pipeline_stages import _book_risk_inputs

    ctx = SimpleNamespace(facts=SimpleNamespace(
        heat=SimpleNamespace(per_position=[
            SimpleNamespace(symbol="OKLO", budget_risk_dollars=2_000.0),
            SimpleNamespace(symbol="CEG", budget_risk_dollars=500.0),
        ]),
        correlation_clusters=[["OKLO", "CEG"]],
    ))
    existing, clusters = _book_risk_inputs(ctx, total_value=100_000.0)
    assert existing == {"OKLO": 2.0, "CEG": 0.5}
    assert clusters == [["OKLO", "CEG"]]


def test_book_risk_inputs_return_none_when_the_book_cannot_be_seen():
    """Enforcing a 25% ceiling against a book we cannot measure would either
    block every trade or wave everything through. Leaving the portfolio
    ceilings unenforced is the correct failure direction — per-position
    sizing and the single-name cap still apply."""
    from types import SimpleNamespace

    from src.pipeline_stages import _book_risk_inputs

    assert _book_risk_inputs(SimpleNamespace(facts=None), 100_000.0) == (None, None)
    assert _book_risk_inputs(SimpleNamespace(), 100_000.0) == (None, None)
    ctx = SimpleNamespace(facts=SimpleNamespace(heat=None, correlation_clusters=[]))
    assert _book_risk_inputs(ctx, 100_000.0) == (None, None)


def test_the_cluster_cap_is_rendered_to_the_portfolio_manager():
    """The PM is told a per-cluster cap exists; it must also be shown the
    number, or it sizes a theme blind and meets the cap as a surprise."""
    from src.pipeline_context import PMFacts

    facts = PMFacts()
    facts.correlation_coverage = True
    facts.correlation_clusters = [["CCJ", "CEG", "OKLO", "VST"]]
    facts.risk_ceiling_pct = 25.0
    facts.cluster_risk_share_pct = 40.0
    block = facts._render_correlation()
    assert "at most 10.0% of equity at risk" in block
    assert "40% of the 25.0% total" in block
    assert "CCJ / CEG / OKLO / VST" in block


def test_risk_envelope_reaches_the_constructor_from_config():
    """The ratified envelope lives in `risk:` config, not in a dataclass
    default nobody chose — which is how the 0.5% per-trade figure survived
    inherited. It was written by the UPSTREAM author (`995fcb0`, yebof,
    2026-04-18), before QAMC existed — QAMC's own history begins
    2026-08-09. It was never a QAMC decision that went stale; it was a
    default that came with the fork and that nobody had revisited."""
    from pathlib import Path

    import yaml

    from src.config import RiskConfig

    # The deployed YAML is read directly rather than through `load_config`:
    # the hermetic suite has no OPENROUTER_API_KEY, so full AppConfig
    # validation fails for reasons unrelated to the risk envelope.
    root = Path(__file__).resolve().parent.parent
    raw = yaml.safe_load((root / "config" / "settings.yaml").read_text())["risk"]
    assert raw["max_position_risk_pct"] == 5
    assert raw["min_position_risk_pct"] == 0.5
    assert raw["max_portfolio_risk_pct"] == 25
    assert raw["max_cluster_risk_share_pct"] == 40

    # And the defaults agree, so a config that omits them lands in the same
    # place rather than silently reverting to a looser envelope.
    defaults = RiskConfig(
        max_position_pct=20, max_total_position_pct=90,
        max_daily_loss_pct=3, max_sector_pct=40, require_stop_loss=True,
    )
    assert defaults.max_position_risk_pct == 5.0
    assert defaults.min_position_risk_pct == 0.5
    assert defaults.max_portfolio_risk_pct == 25.0
    assert defaults.max_cluster_risk_share_pct == 40.0


def test_the_api_reports_risk_sized_targets_rather_than_dropping_them():
    """Spec §2.1 regression guard.

    `CandidateFunnelItem.pm_target_weight_pct` reads
    `TargetPosition.target_weight_pct`, which is None for every risk-sized
    target. Reporting only that field silently dropped the size of every
    target the PM sized the new way — the cockpit rendered the candidate with
    no size at all. The two fields are not interchangeable and neither is
    derivable from the other without the stop, so both are carried and the
    view shows whichever the PM actually stated.
    """
    from src.api.schemas import CandidateFunnelItem

    item = CandidateFunnelItem(
        symbol="NVDA", direction="bullish", is_bearish_hedge=False,
        reached_pm_target=True,
        pm_target_weight_pct=None, pm_risk_allocation_pct=2.0,
        reached_proposed_order=False, risk_modified=False, executed=False,
    )
    assert item.pm_risk_allocation_pct == 2.0
    assert item.pm_target_weight_pct is None

    # A legacy notional target still reports the way it always did.
    legacy = CandidateFunnelItem(
        symbol="NVDA", direction="bullish", is_bearish_hedge=False,
        reached_pm_target=True,
        pm_target_weight_pct=8.0, pm_risk_allocation_pct=None,
        reached_proposed_order=False, risk_modified=False, executed=False,
    )
    assert legacy.pm_target_weight_pct == 8.0
    assert legacy.pm_risk_allocation_pct is None
