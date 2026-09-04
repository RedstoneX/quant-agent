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


def _analysis(
    symbol: str, entry: float, stop: float, target: float,
    horizon: int = 60, atr: float | None = None,
) -> TechAnalysisResult:
    """A realistic analyst result, including the fields production sets in
    Python rather than asking the model for.

    `atr_14` and `computed_levels` are attached by `TechAnalystAgent` after
    parsing; the constructor has derived the take-profit from
    `computed_levels` since 2026-09-01 and refuses without them. The default
    ATR sits just inside the noise band so the structural stop is left alone
    — these tests are about SIZING, and the widening tests below set their
    own ATR explicitly.
    """
    return TechAnalysisResult(
        symbol=symbol, rating="buy", entry_price=entry, stop_loss=stop,
        reference_target=target, reasoning="test",
        support_levels=[stop], resistance_levels=[target],
        computed_levels=[stop, target],
        computed_level_touches={stop: 5, target: 5},
        atr_14=(entry - stop) / 3.5 if atr is None else atr,
        setup_type="range", expected_horizon_sessions=horizon,
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
    distance — half the position, and both trades risk exactly 1%.

    Sized at 1% risk against 10% and 20% stops deliberately: the proportion is
    the invariant under test, and it is only observable where the 20%
    single-name notional ceiling is not the binding constraint. At this book's
    real stop distances the ceiling binds first and both positions come out
    the same size — see
    `test_the_ceiling_flattens_conviction_at_realistic_stop_distances`."""
    constructor = PortfolioConstructor()

    def alloc(stop):
        d = constructor.construct_orders(
            targets=[_risk_target("NVDA", 1.0)], positions=[],
            analyses=[_analysis("NVDA", entry=100, stop=stop, target=140)],
            total_value=EQUITY, price_map={"NVDA": 100.0},
        )
        return d[0].allocation_pct

    tight, wide = alloc(90), alloc(80)
    assert abs(tight - 10.0) < 0.05
    assert abs(wide - 5.0) < 0.05
    # Both put the same dollars at risk — that is the invariant §2.1 buys.
    assert abs(tight / 100 * EQUITY * 0.10 - 1000) < 20   # 10/100 stop gap
    assert abs(wide / 100 * EQUITY * 0.20 - 1000) < 20    # 20/100 stop gap


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
        # Target $160, not $140: the 30-wide stop is deliberately far
        # outside the noise band, and since 2026-09-02 that path is gated
        # on reward:risk like every other. (140-100)/30 = 1.33 is under the
        # 1.5 floor and would be refused on GEOMETRY, which is not what
        # this test is about. At $160 the ratio is 2.0 and the sizing clamp
        # is once again the only thing under test.
        analyses=[_analysis("NVDA", entry=100, stop=70, target=160)],
        total_value=EQUITY, price_map={"NVDA": 100.0},
    )
    # Clamped to 3% risk → 3000/30 = 100 shares → $10k → 10%. The 30%-wide
    # stop keeps the 20% single-name notional ceiling out of the way so the
    # RISK clamp is what this asserts.
    assert abs(decisions[0].allocation_pct - 10.0) < 0.05


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
    applies; the portfolio ceilings do not.

    Sector concentration is a SEPARATE, unrelated ceiling that also applies
    to these four same-sector names — since the 2026-09-04 single-name cap
    raise (20 -> 100) each now asks for its full 50% notional rather than
    being pre-clamped to 20%, which is enough for real, same-sector names to
    hit the (also real, unchanged) sector hard cap. That is correct
    behaviour, not this test's concern, so the sector dial is loosened here
    to isolate the risk-budget gate this test is actually about."""
    constructor = PortfolioConstructor(ConstructorConfig(
        max_sector_pct=400.0, max_sector_hard_pct=400.0,
    ))
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


def test_raising_the_single_name_notional_cap_does_not_raise_the_total_risk_ceiling():
    """2026-09-04: step 2 of the notional-cap fix, pinned as a test.

    `max_position_pct` moved 20 -> 100 so a tight-but-realistic stop no
    longer silently caps delivered risk. That is a NOTIONAL ceiling; the
    25% `max_portfolio_risk_pct` book-wide ceiling is a separate, RISK
    ceiling enforced earlier in the same pipeline (`allocate_risk_budget`,
    applied before the single-name notional clamp ever runs — see
    `_build_buy` in src/portfolio_constructor.py). Freeing the notional
    clamp must not free the risk one: a target that would now be large
    enough in notional terms to want more of the book still gets cut to
    whatever total risk is actually left, exactly as it did before this
    fix, at the SAME numbers.
    """
    constructor = PortfolioConstructor()
    # A tight, realistic stop (5%) and full 5% conviction ask for 100%
    # notional — reachable now, unlike before this fix — but the book
    # already carries 23% of the ratified 25% ceiling, leaving only 2%.
    decisions = constructor.construct_orders(
        targets=[_risk_target("NVDA", 5.0)], positions=[],
        analyses=[_analysis("NVDA", entry=100, stop=95, target=140)],
        total_value=EQUITY, price_map={"NVDA": 100.0},
        existing_risk_pct={"HELD": 23.0}, clusters=[],
    )
    assert len(decisions) == 1
    # 2% of the risk budget left, at a 5% stop: $2,000 risk / $5 per share
    # -> 400 shares -> $40,000 -> 40% notional. Far under the new 100% (and
    # the sector's 90%) notional ceilings — the risk ceiling is what binds,
    # unmoved by the notional-cap change.
    assert abs(decisions[0].allocation_pct - 40.0) < 0.05
    assert abs(decisions[0].allocated_risk_pct - 2.0) < 0.05

    # And a fully-spent 25% budget still denies the trade outright, same as
    # test_a_full_budget_denies_a_new_name_outright above — unaffected by
    # the single-name notional cap now being 5x looser.
    denied = constructor.construct_orders(
        targets=[_risk_target("NVDA", 5.0)], positions=[],
        analyses=[_analysis("NVDA", entry=100, stop=95, target=140)],
        total_value=EQUITY, price_map={"NVDA": 100.0},
        existing_risk_pct={"HELD": 25.0}, clusters=[],
    )
    assert denied == []


def test_released_risk_frees_budget_for_a_new_position():
    """Spec §2.3/§2.4: a position whose stop has reached entry contributes
    zero budget risk, so the book expands while trades work — with nobody
    choosing a position count. The caller computes the release
    (`src/risk/metrics.py`); the constructor simply spends what is left."""
    constructor = PortfolioConstructor()
    common = dict(
        targets=[_risk_target("NVDA", 5.0)], positions=[],
        # $160 for the same reason as
        # `test_single_name_risk_is_clamped_to_the_ratified_envelope`: the
        # 30-wide stop clears the noise band, and the reward:risk floor now
        # applies on that path too. This test is about the risk BUDGET.
        analyses=[_analysis("NVDA", entry=100, stop=70, target=160)],
        total_value=EQUITY, price_map={"NVDA": 100.0}, clusters=[],
    )
    blocked = constructor.construct_orders(existing_risk_pct={"WINNER": 25.0}, **common)
    freed = constructor.construct_orders(existing_risk_pct={"WINNER": 0.0}, **common)
    assert blocked == []
    assert len(freed) == 1
    # 5% risk against a 30%-wide stop → $5k/$30 per share → $16.7k → 16.7%,
    # under the single-name ceiling so the released BUDGET is what is measured.
    assert abs(freed[0].allocation_pct - 16.67) < 0.05


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


def test_book_risk_inputs_return_clusters_alone_when_only_heat_fails():
    """Heat and clusters are built independently in `_book_risk_inputs`; a
    heat failure (or a book with no facts.heat at all) must not silently
    swallow clusters that DID build. This is the partial-failure input the
    2026-09-03 incident is about — the caller, not this function, is what
    has to treat a lone `existing=None` as unenforceable."""
    from types import SimpleNamespace

    from src.pipeline_stages import _book_risk_inputs

    ctx = SimpleNamespace(facts=SimpleNamespace(
        heat=None, correlation_clusters=[["OKLO", "CEG"]],
    ))
    existing, clusters = _book_risk_inputs(ctx, total_value=100_000.0)
    assert existing is None
    assert clusters == [["OKLO", "CEG"]]


def test_a_heat_failure_leaves_the_budget_unenforced_even_with_clusters():
    """2026-09-03 incident: `allocate_risk_budget` treats a missing
    `existing_pct` as an empty dict — a book with ZERO risk — not as
    "unknown". Before the fix, `clusters` being present was enough on its
    own to run the allocator, so a heat failure made the cluster cap bind
    against a held book the constructor could not actually see. The fix
    requires `existing_risk_pct` before the allocator runs at all; clusters
    alone must leave the ceilings unenforced, exactly like the both-missing
    case in `test_the_budget_gate_is_inert_when_the_caller_supplies_no_book_risk`.

    Sector dial loosened here for the same reason as that sibling test —
    it is a separate, unrelated ceiling this test is not about."""
    constructor = PortfolioConstructor(ConstructorConfig(
        max_sector_pct=400.0, max_sector_hard_pct=400.0,
    ))
    targets, analyses, prices = _nuclear_setup(*NUCLEAR)
    decisions = constructor.construct_orders(
        targets=targets, positions=[], analyses=analyses,
        total_value=EQUITY, price_map=prices,
        existing_risk_pct=None, clusters=[NUCLEAR],
    )
    # All four survive: the cluster cap must NOT bind when the book's
    # existing risk is unknown. (Compare test_a_single_theme_cannot_take_
    # the_whole_risk_budget, where existing_risk_pct={} — a KNOWN empty book
    # — correctly lets the cap cut this down to two.)
    assert {d.symbol for d in decisions} == set(NUCLEAR)


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


# --------------------------------------------------------------------------
# The single-name notional ceiling — where §2.1 meets `max_position_pct`
# --------------------------------------------------------------------------

def test_size_is_clamped_to_the_single_name_ceiling_not_left_to_be_blocked():
    """`max_position_pct` is a HARD BLOCK, not a trim.

    Risk-based sizing asks for `risk_pct x entry / (entry - stop)` of equity.
    A tight enough stop can still ask for more than the single-name ceiling
    (100% since 2026-09-04 — see settings.yaml's `risk.max_position_pct` for
    the full derivation) in one name. `max_position_pct` sits in
    `HARD_BLOCK_RULES`, so the risk engine does not shrink that order, it
    drops it, and the trade never happens. The constructor therefore has to
    size UNDER the ceiling itself. Mechanism test — an explicit tighter
    config is used so a realistic stop still exercises the clamp; the real
    deployed number is covered separately below.
    """
    from src.risk.rules import RiskRuleEngine
    from src.config import RiskConfig

    constructor = PortfolioConstructor(ConstructorConfig(max_position_pct=20.0))
    decisions = constructor.construct_orders(
        targets=[_risk_target("NVDA", 2.0)], positions=[],
        analyses=[_analysis("NVDA", entry=100, stop=95, target=140)],
        total_value=EQUITY, price_map={"NVDA": 100.0},
    )
    assert len(decisions) == 1
    assert decisions[0].allocation_pct <= 20.0

    # And the engine that would have blocked it now passes it.
    engine = RiskRuleEngine(RiskConfig(
        max_position_pct=20, max_total_position_pct=100, max_daily_loss_pct=5,
        max_sector_pct=40, require_stop_loss=True,
    ))
    violations = engine.check(
        decisions[0], [], EQUITY, 0.0, cash=EQUITY,
    )
    assert [v.rule for v in violations] == []


def test_a_clamped_size_says_so_in_the_reasoning():
    """The AI Risk Manager audits the constructed order against PM's prose.

    On 2026-08-20 an unexplained deterministic cut read to it as "plan
    inconsistency" and drew a full-plan veto, so every cut carries its
    arithmetic. A clamped position also risks LESS than the PM allocated —
    that has to be visible, not silent.
    """
    constructor = PortfolioConstructor()
    decisions = constructor.construct_orders(
        targets=[_risk_target("NVDA", 4.0)], positions=[],
        analyses=[_analysis("NVDA", entry=100, stop=97, target=140)],
        total_value=EQUITY, price_map={"NVDA": 100.0},
    )
    assert len(decisions) == 1
    assert "single-name ceiling" in decisions[0].reasoning
    assert "not PM inconsistency" in decisions[0].reasoning


def test_a_tight_stop_never_produces_an_unrepresentable_allocation():
    """Regression: `TradeDecision.allocation_pct` is bounded at 100.

    Before the clamp, 4% risk against a 3% stop computed a 133% allocation and
    raised a pydantic ValidationError *inside* `construct_orders` — an
    uncaught exception on the live decision path, which takes the session's
    order construction down rather than dropping one trade.
    """
    constructor = PortfolioConstructor()
    decisions = constructor.construct_orders(
        targets=[_risk_target("NVDA", 5.0)], positions=[],
        analyses=[_analysis("NVDA", entry=100, stop=98, target=140)],
        total_value=EQUITY, price_map={"NVDA": 100.0},
    )
    assert len(decisions) == 1
    assert 0 < decisions[0].allocation_pct <= 100.0


def test_the_ceiling_accounts_for_what_is_already_held():
    """The engine caps the POSITION, not the order, so the clamp must too.

    Mechanism test: an explicit tighter config exercises the clamp at a
    realistic held-position size (the real 100% deployed ceiling would need
    an implausibly large existing position to bind here at all)."""
    constructor = PortfolioConstructor(ConstructorConfig(max_position_pct=20.0))
    decisions = constructor.construct_orders(
        targets=[_risk_target("NVDA", 5.0)],
        positions=[_pos("NVDA", qty=150, avg_entry=100, current_price=100)],  # 15%
        analyses=[_analysis("NVDA", entry=100, stop=95, target=140)],
        total_value=EQUITY, price_map={"NVDA": 100.0},
    )
    assert len(decisions) == 1
    # 20% ceiling - 15% held = 5% of headroom, and not a basis point more.
    assert decisions[0].allocation_pct == 5.0


def test_a_fully_sized_name_produces_no_order_rather_than_a_zero_one():
    constructor = PortfolioConstructor(ConstructorConfig(max_position_pct=20.0))
    decisions = constructor.construct_orders(
        targets=[_risk_target("NVDA", 5.0)],
        positions=[_pos("NVDA", qty=200, avg_entry=100, current_price=100)],  # 20%
        analyses=[_analysis("NVDA", entry=100, stop=95, target=140)],
        total_value=EQUITY, price_map={"NVDA": 100.0},
    )
    assert decisions == []


def test_the_ceiling_no_longer_flattens_conviction_at_realistic_stop_distances():
    """2026-09-04 real-data audit, and the fix pinned as a test.

    The book's real stops, post the 2026-08-27 ATR-floor fix, run roughly
    5-9% below entry (`risk.min_stop_atr_multiple`'s own measurement; the
    17-BUY 4.3%-median/1.8-7.7%-range sample below predates that floor and
    is why it was introduced). At a 20% single-name ceiling,
    `risk_pct x entry/(entry - stop)` exceeded it for any conviction above
    ~1% risk at those distances, so low/moderate/high conviction all landed
    on the SAME 20% position — conviction stopped changing the size, which
    is the entire premise of §2.1. At the now-corrected 100% single-name
    ceiling (settings.yaml's `risk.max_position_pct`) the same stops let
    conviction through instead of flattening it — NVDA here is an isolated,
    uncrowded position, so the SEPARATE sector hard cap (90%, unchanged,
    §12.3) is what actually catches the 5%-risk/5%-stop edge case rather
    than the single-name cap itself; that is a real, already-ratified
    ceiling doing its own job, not this fix leaking room elsewhere.
    """
    constructor = PortfolioConstructor()

    def alloc(risk_pct, stop):
        d = constructor.construct_orders(
            targets=[_risk_target("NVDA", risk_pct)], positions=[],
            analyses=[_analysis("NVDA", entry=100, stop=stop, target=140)],
            total_value=EQUITY, price_map={"NVDA": 100.0},
        )
        return d[0].allocation_pct

    # A 5% stop (the tight end of the desk's documented real, post-floor
    # range) and full high-conviction risk (5%) asks for exactly 100%
    # notional — no longer clamped by the single-name cap at all — but
    # lands on the sector's own 90% absolute ceiling (unrelated, unchanged).
    # Delivered risk is still 4.5% (90% x 5%), against the ~1% the old 20%
    # ceiling would have delivered here.
    assert alloc(5.0, stop=95.0) == 90.0
    # Low, moderate and high conviction now produce DIFFERENT positions at a
    # realistic stop distance (7.7%, the old sample's own measured max),
    # rather than all three flattening to one number.
    low, moderate, high = alloc(0.5, 92.3), alloc(2.0, 92.3), alloc(5.0, 92.3)
    assert low < moderate < high
    assert abs(high - 5.0 / 7.7 * 100) < 0.5


def test_the_ceiling_still_binds_a_genuinely_too_tight_stop():
    """The backstop still does its job — it just no longer does the risk
    engine's job too. A stop tighter than the desk's real ATR-floor-governed
    range (only reachable via the level-backed exception down to
    `absolute_min_stop_atr_multiple`) still gets clamped well below what
    conviction asked for, same as before — just by a different, still-real
    ceiling (the sector's 90% absolute cap binds ahead of the single-name
    cap's own 100% here, exactly as it does in the realistic-stop test
    above; either way the position is capped far short of the 200%
    conviction asked for)."""
    constructor = PortfolioConstructor()
    decisions = constructor.construct_orders(
        targets=[_risk_target("NVDA", 5.0)], positions=[],
        analyses=[_analysis("NVDA", entry=100, stop=97.5, target=140)],  # 2.5% stop
        total_value=EQUITY, price_map={"NVDA": 100.0},
    )
    assert len(decisions) == 1
    # 5% risk / 2.5% stop asks for 200% — clamped to 90% (the sector's
    # absolute ceiling binding ahead of the 100% single-name cap here), so
    # delivered risk is 2.25% (90% x 2.5%), under the 5% requested but well
    # above the ~0.5% the old 20% ceiling would have delivered here.
    assert decisions[0].allocation_pct == 90.0


# --------------------------------------------------------------------------
# Stop width — the root cause behind both the sizing squeeze and noise exits
# --------------------------------------------------------------------------

def _vol_analysis(symbol, entry, stop, target, atr, setup="range", horizon=60,
                  computed=None, touches=None):
    """A widening fixture: the stop is NOT at a computed structural level.

    `computed_levels` defaults to the target alone, and that default is
    load-bearing. Since 2026-09-01 the take-profit is derived from these
    rather than from `reference_target`, and the reward:risk check inside
    `_widen_stop_past_noise` measures against the DERIVED number — so the
    fixture has to supply the structure the derivation reads. But since
    spec §12.1 the same field also decides whether the ATR band applies at
    all: a stop sitting at a computed level is honoured instead of widened.
    Listing the stop here would therefore silently turn every widening test
    below into a level-backed test. The stop is the ANALYST's number, with
    nothing computed under it, which is precisely the case the band exists
    for. `computed` is available for the level-backed tests, which say so
    in their own names.

    `touches` (2026-09-03, Phase 12.1) is the per-price touch count
    `_level_backing_stop` now requires before honouring a tight stop — see
    `ConstructorConfig.min_level_touches_for_stop_honor` and
    docs/RESEARCH_FINDINGS.md §7. Every price in `computed` defaults to 5
    touches (the derived bar) so existing "honoured" fixtures keep meaning
    "this level is verified enough" unless a test overrides it to exercise
    the gate itself.
    """
    from src.models import TechReasoningChain
    levels = [target] if computed is None else computed
    default_touches = {price: 5 for price in levels}
    return TechAnalysisResult(
        symbol=symbol, rating="buy", entry_price=entry, stop_loss=stop,
        reference_target=target, reasoning="test", support_levels=[stop],
        resistance_levels=[target], setup_type=setup,
        expected_horizon_sessions=horizon, atr_14=atr,
        computed_levels=levels,
        computed_level_touches=default_touches if touches is None else touches,
        reasoning_chain=TechReasoningChain(
            trend="x", momentum="x", volatility="x", volume="x",
            support_resistance="x"),
    )


def test_a_stop_inside_the_noise_band_is_pushed_out():
    """Measured 2026-08-27: stops sat a median 4.3% below entry against a
    median ATR of 2.56% of price — about 1.7 ATRs. That is one ordinary day's
    range, not a thesis invalidation, and it is what both fired exits inside
    noise and forced huge positions to reach any real risk."""
    constructor = PortfolioConstructor()
    decisions = constructor.construct_orders(
        targets=[_risk_target("MSFT", 1.0)], positions=[],
        analyses=[_vol_analysis("MSFT", 100.0, 97.6, 160.0, atr=2.35)],
        total_value=EQUITY, price_map={"MSFT": 100.0},
    )
    assert len(decisions) == 1
    # A range setup earns 1.15x the 3.0 base = 3.45 ATRs, so 3.45 x 2.35 =
    # 8.11 below entry, replacing the 2.4% structural stop.
    assert abs(decisions[0].stop_loss - 91.89) < 0.01


def test_a_stop_already_outside_the_noise_band_is_left_alone():
    """This only corrects stops placed inside the noise. It never pulls a
    wide structural stop tighter — structure still decides."""
    constructor = PortfolioConstructor()
    decisions = constructor.construct_orders(
        targets=[_risk_target("OKLO", 1.0)], positions=[],
        analyses=[_vol_analysis("OKLO", 100.0, 60.0, 200.0, atr=8.22)],
        total_value=EQUITY, price_map={"OKLO": 100.0},
    )
    assert decisions[0].stop_loss == 60.0


def test_the_atr_multiple_is_not_one_constant_for_every_trade():
    """ATR already adapts the distance to each stock and session. The MULTIPLE
    adapts how many ATRs the setup earns: a breakout invalidates at a level,
    a range trade gets shaken out inside its own band, and a risk-off tape
    swings wider for the same ATR reading than a trending one."""
    constructor = PortfolioConstructor()

    def stop(setup, regime):
        d = constructor.construct_orders(
            targets=[_risk_target("MSFT", 1.0)], positions=[],
            analyses=[_vol_analysis("MSFT", 100.0, 97.6, 200.0, 2.35, setup)],
            total_value=EQUITY, price_map={"MSFT": 100.0}, regime=regime,
        )
        return d[0].stop_loss

    # Breakout earns less room than a range trade on the same name and tape.
    assert stop("breakout", "risk-on") > stop("range", "risk-on")
    # And the same setup gets more room as the tape deteriorates.
    assert stop("range", "risk-on") > stop("range", "transitional") > stop("range", "risk-off")


def test_widening_a_stop_into_a_bad_payoff_rejects_the_trade():
    """The target does not move when the stop does, so reward:risk falls. A
    setup that only cleared the bar on a stop too tight to survive was never
    the trade it appeared to be."""
    constructor = PortfolioConstructor()
    decisions = constructor.construct_orders(
        targets=[_risk_target("MSFT", 1.0)], positions=[],
        # Target only 5% up; a 7% stop leaves reward:risk well under 1.5.
        analyses=[_vol_analysis("MSFT", 100.0, 97.6, 105.0, atr=2.35)],
        total_value=EQUITY, price_map={"MSFT": 100.0},
    )
    assert decisions == []


def test_no_volatility_reading_leaves_the_structural_stop_untouched():
    """Stop widening still fails toward existing behaviour rather than
    inventing a width: with no ATR the structural stop is returned as-is."""
    constructor = PortfolioConstructor()
    analysis = _vol_analysis("MSFT", 100.0, 97.6, 160.0, atr=None)
    assert constructor._widen_stop_past_noise(
        "MSFT", analysis, 100.0, 97.6, target_price=160.0,
    ) == 97.6


def test_no_volatility_reading_refuses_the_trade_outright():
    """...but the ORDER is refused, because the target is no longer the
    analyst's guess (2026-09-01). Without an ATR there is no noise floor and
    no reachable distance, so there is nothing to derive a target from — and
    a fabricated target is exactly the defect this replaced. Fail closed,
    with a named reason, rather than trade on half a chart."""
    constructor = PortfolioConstructor()
    decisions = constructor.construct_orders(
        targets=[_risk_target("MSFT", 1.0)], positions=[],
        analyses=[_vol_analysis("MSFT", 100.0, 97.6, 160.0, atr=None)],
        total_value=EQUITY, price_map={"MSFT": 100.0},
    )
    assert decisions == []


def test_wider_stops_give_conviction_room_to_change_the_size():
    """The payoff. With stops outside the noise, conviction moves the size
    again instead of every idea clamping to the same 20% position."""
    constructor = PortfolioConstructor()

    def alloc(risk):
        d = constructor.construct_orders(
            targets=[_risk_target("MSFT", risk)], positions=[],
            analyses=[_vol_analysis("MSFT", 100.0, 97.6, 160.0, atr=2.35)],
            total_value=EQUITY, price_map={"MSFT": 100.0},
        )
        return d[0].allocation_pct

    assert alloc(0.5) < alloc(1.0) < alloc(1.5)


# --------------------------------------------------------------------------
# §12.1 — a stop at a VERIFIED structural level is honoured, however tight
# --------------------------------------------------------------------------
#
# The defect: `min_stop_atr_multiple` OVERWROTE the level-derived stop
# whenever the level sat closer to entry than the band. The stop then pointed
# at nothing real, and `min_reward_risk_after_widening` was judged against
# that fabricated number. Over a ~15-session hold a stock travels ~3.9 ATR,
# so against a 3.0-ATR stop the best achievable ratio is ~1.29 against a 1.5
# floor — essentially no trade could pass. On 2026-09-01 the desk reviewed 38
# qualified signals and placed ZERO trades.
#
# "Verified" means the price came out of `find_structural_levels` and was
# attached to the analysis IN PYTHON (`computed_levels`). A level the MODEL
# asserts earns nothing — otherwise a model could buy itself an exemption
# from the noise floor by naming a number beside its stop.
#
# Shared geometry for the block below: entry $100.00, ATR $2.35, a "range"
# setup (3.0 base x 1.15 = 3.45 ATRs), so the noise band sits at
# 100 - 3.45 x 2.35 = $91.89 and the 1x ATR absolute floor at $97.65.
# Match tolerance is 0.25 x ATR = $0.5875.

_ENTRY = 100.0
_ATR = 2.35
_BAND_EDGE = 91.89        # 3.45 x ATR below entry — the OLD unconditional stop
_HARD_FLOOR = 97.65       # 1.00 x ATR below entry — the new deterministic floor
_UPPER_LEVEL = 112.0      # computed resistance; becomes the derived target


def test_a_level_backed_tight_stop_is_honoured_not_widened():
    """The whole point of §12.1. A stop 1.7 ATRs out — the median this book
    was actually placing, measured 2026-08-27 — sits well inside the 3.45 ATR
    band. Because a COMPUTED support level sits under it, it survives."""
    constructor = PortfolioConstructor()
    decisions = constructor.construct_orders(
        targets=[_risk_target("MSFT", 1.0)], positions=[],
        analyses=[_vol_analysis(
            "MSFT", _ENTRY, 96.0, _UPPER_LEVEL, atr=_ATR,
            computed=[96.0, _UPPER_LEVEL],
        )],
        total_value=EQUITY, price_map={"MSFT": _ENTRY},
    )
    assert len(decisions) == 1
    assert decisions[0].stop_loss == 96.0
    # And emphatically NOT the band edge, which is what shipped before.
    assert decisions[0].stop_loss != _BAND_EDGE


def test_an_unbacked_tight_stop_is_still_widened_to_the_band():
    """The other half, and the part that must not regress. Identical trade,
    except nothing computed sits under the $96.00 stop — the analyst simply
    placed it there. The band applies exactly as it always did, and here it
    is fatal: risk 8.11 against reward 12.00 is R/R 1.48, under the floor."""
    constructor = PortfolioConstructor()
    decisions = constructor.construct_orders(
        targets=[_risk_target("MSFT", 1.0)], positions=[],
        analyses=[_vol_analysis(
            "MSFT", _ENTRY, 96.0, _UPPER_LEVEL, atr=_ATR,
            computed=[_UPPER_LEVEL],       # the stop is NOT a computed level
        )],
        total_value=EQUITY, price_map={"MSFT": _ENTRY},
    )
    assert decisions == []


def test_reward_risk_is_measured_against_the_stop_that_will_actually_ship():
    """The pair above IS the fix, stated as arithmetic.

    Same entry, same stop, same computed target $112.00. Against the
    fabricated band stop the ratio is 12.00 / 8.11 = 1.48 and the trade dies;
    against the stop the desk will actually place it is 12.00 / 4.00 = 3.00
    and it trades. Nothing about the trade changed — only which stop the
    division was performed on, which is the defect §12.1 removes."""
    constructor = PortfolioConstructor()

    def stop_for(computed):
        return constructor._widen_stop_past_noise(
            "MSFT",
            _vol_analysis("MSFT", _ENTRY, 96.0, _UPPER_LEVEL, atr=_ATR,
                          computed=computed),
            entry_price=_ENTRY, stop_loss=96.0, target_price=_UPPER_LEVEL,
        )

    honoured = stop_for([96.0, _UPPER_LEVEL])
    assert honoured == 96.0
    assert (_UPPER_LEVEL - _ENTRY) / (_ENTRY - honoured) == 3.0

    # Unbacked: widened, and the ratio against the widened stop is under 1.5,
    # so the function refuses rather than returning a worse trade.
    assert stop_for([_UPPER_LEVEL]) is None
    assert round((_UPPER_LEVEL - _ENTRY) / (_ENTRY - _BAND_EDGE), 2) == 1.48


def test_a_level_backed_stop_inside_one_atr_is_floored_at_one_atr_not_the_band():
    """Beyond the literal §12.1 wording, and deliberately so.

    §12.1 argues the exemption is safe because `config/prompts/tech_analyst.md`
    forbids a stop inside 1*ATR. That is a PROMPT; Invariant 2 requires the
    deterministic layer to be the final authority and to fail closed. A real
    support level $1.00 under entry is genuine structure AND a guaranteed
    whipsaw. So it is pushed out to exactly 1x ATR — NOT to the 3.45x band,
    which is the behaviour §12.1 removed."""
    constructor = PortfolioConstructor()
    decisions = constructor.construct_orders(
        targets=[_risk_target("MSFT", 1.0)], positions=[],
        analyses=[_vol_analysis(
            "MSFT", _ENTRY, 99.0, _UPPER_LEVEL, atr=_ATR,
            computed=[99.0, _UPPER_LEVEL],
        )],
        total_value=EQUITY, price_map={"MSFT": _ENTRY},
    )
    assert len(decisions) == 1
    assert decisions[0].stop_loss == _HARD_FLOOR
    assert decisions[0].stop_loss != _BAND_EDGE
    # The floor is configurable and is the only thing that moved the stop.
    assert _HARD_FLOOR == round(_ENTRY - 1.0 * _ATR, 2)


def test_the_absolute_floor_is_configurable_and_can_be_switched_off():
    """It is an owner-reversible addition, so it must be reachable from
    config rather than baked in. At 0 the ratified §12.1 wording applies
    literally: the level-backed stop is honoured however tight."""
    constructor = PortfolioConstructor(ConstructorConfig(
        absolute_min_stop_atr_multiple=0.0,
    ))
    assert constructor._widen_stop_past_noise(
        "MSFT",
        _vol_analysis("MSFT", _ENTRY, 99.0, _UPPER_LEVEL, atr=_ATR,
                      computed=[99.0, _UPPER_LEVEL]),
        entry_price=_ENTRY, stop_loss=99.0, target_price=_UPPER_LEVEL,
    ) == 99.0


def test_a_near_miss_outside_the_tolerance_is_not_level_backed():
    """The tolerance is a boundary, not a suggestion. 0.25 x ATR = $0.5875
    from the computed level at $96.00: $96.50 is sitting on it, $96.70 is
    not, and the second one gets the band like any unbacked stop."""
    constructor = PortfolioConstructor()

    def stop_for(stop):
        return constructor._widen_stop_past_noise(
            "MSFT",
            _vol_analysis("MSFT", _ENTRY, stop, 130.0, atr=_ATR,
                          computed=[96.0, 130.0]),
            entry_price=_ENTRY, stop_loss=stop, target_price=130.0,
        )

    assert stop_for(96.5) == 96.5                      # gap $0.50, inside
    assert round(stop_for(96.7), 2) == _BAND_EDGE      # gap $0.70, outside


def test_a_level_the_model_asserted_does_not_earn_the_exemption():
    """The verification would be worthless if the model could write to it.

    Here `support_levels` — which the LLM emits — names $96.00 exactly, and
    the stop sits on it. `computed_levels`, which only Python writes, does
    not. The band applies, because the analyst asserting a level is not the
    system having computed one."""
    constructor = PortfolioConstructor()
    analysis = _vol_analysis("MSFT", _ENTRY, 96.0, 130.0, atr=_ATR,
                             computed=[130.0])
    assert analysis.support_levels == [96.0]           # the model said so
    assert 96.0 not in analysis.computed_levels        # the chart did not
    assert round(constructor._widen_stop_past_noise(
        "MSFT", analysis, entry_price=_ENTRY, stop_loss=96.0,
        target_price=130.0,
    ), 2) == _BAND_EDGE


def test_a_level_below_the_touch_bar_does_not_earn_the_exemption():
    """Phase 12.1, 2026-09-03. `min_level_touches_for_stop_honor` (5, derived
    in docs/RESEARCH_FINDINGS.md §7 from the measured touch-count table) is
    a SEPARATE gate from `find_structural_levels`' own `MIN_TOUCHES` (2):
    the level is real enough to appear in `computed_levels` and to be a
    legal target, but not yet trusted enough for a tight-stop exemption.
    Identical fixture to the flagship honoured case, except the level's
    touch count sits below the bar — the stop is widened to the band exactly
    as an unbacked stop would be."""
    constructor = PortfolioConstructor()
    decisions = constructor.construct_orders(
        targets=[_risk_target("MSFT", 1.0)], positions=[],
        analyses=[_vol_analysis(
            "MSFT", _ENTRY, 96.0, _UPPER_LEVEL, atr=_ATR,
            computed=[96.0, _UPPER_LEVEL],
            touches={96.0: 4, _UPPER_LEVEL: 5},  # stop's own level: 4 touches
        )],
        total_value=EQUITY, price_map={"MSFT": _ENTRY},
    )
    # R/R against the band stop is 1.48 here (see the unbacked-tight-stop
    # test above) — under the 1.5 floor, so the trade is refused rather than
    # merely widened. That refusal IS the assertion: it proves the level was
    # NOT treated as backing the stop.
    assert decisions == []


def test_a_level_exactly_at_the_touch_bar_earns_the_exemption():
    """The other edge of the same gate: 5 touches — the bar itself, not one
    more — is enough."""
    constructor = PortfolioConstructor()
    decisions = constructor.construct_orders(
        targets=[_risk_target("MSFT", 1.0)], positions=[],
        analyses=[_vol_analysis(
            "MSFT", _ENTRY, 96.0, _UPPER_LEVEL, atr=_ATR,
            computed=[96.0, _UPPER_LEVEL],
            touches={96.0: 5, _UPPER_LEVEL: 5},
        )],
        total_value=EQUITY, price_map={"MSFT": _ENTRY},
    )
    assert len(decisions) == 1
    assert decisions[0].stop_loss == 96.0


def test_a_level_with_no_touch_count_on_record_fails_closed():
    """A level price present in `computed_levels` but absent from
    `computed_level_touches` (an older caller, a stale fixture, a partial
    write) must NOT be honoured by default — Invariant 2 requires the
    deterministic layer to fail closed, not to assume a level is good
    because its touch count is simply missing."""
    constructor = PortfolioConstructor()
    assert constructor._level_backing_stop(
        _vol_analysis("MSFT", _ENTRY, 96.0, _UPPER_LEVEL, atr=_ATR,
                      computed=[96.0, _UPPER_LEVEL], touches={}),
        _ENTRY, 96.0, _ATR, is_short=False,
    ) is None


def test_a_level_on_the_wrong_side_of_entry_cannot_back_a_stop():
    """Side discipline. A long is held up by structure at or BELOW its entry;
    a short is capped by structure at or ABOVE its entry. The same level
    cannot serve both, and the check is made against THIS ENTRY rather than
    against the last close — the same re-partition `derive_structural_target`
    performs, for the same reason."""
    constructor = PortfolioConstructor()
    analysis = _vol_analysis("MSFT", _ENTRY, 96.0, _UPPER_LEVEL, atr=_ATR,
                             computed=[96.0, _UPPER_LEVEL])
    assert constructor._level_backing_stop(
        analysis, _ENTRY, 96.0, _ATR, is_short=False,
    ) == 96.0
    # The identical level read as a SHORT's backing: it is below entry, so it
    # cannot be what a short's stop above entry is resting on.
    assert constructor._level_backing_stop(
        analysis, _ENTRY, 96.0, _ATR, is_short=True,
    ) is None


# --------------------------------------------------------------------------
# §12.1 — SLB, the 2026-09-01 refusal, reproduced
# --------------------------------------------------------------------------

class TestSLBStopIsHonoured:
    """SLB, 2026-09-01 morning run: `strong_buy` / `high` conviction, entry
    $60.10, and a shipped stop of $55.50 — 7.7% — that scored reward:risk
    1.28 against a geometric maximum of ~1.29, so it was refused before
    anyone judged the trade.

    **The level data here is SYNTHETIC**, exactly as
    `tests/test_target_derivation.py::TestSLB` states for its own fixtures.
    The production bars for that session are not in this repository. These
    assert what the RULE does with a plausible chart, not what SLB's actual
    chart contained, and no claim is made about a live outcome.

    The arithmetic is pinned to that other reproduction so the two agree: the
    shipped $55.50 stop is exactly 3.45 ATRs out (the 3.0 base x 1.15 for a
    range setup), which back-solves ATR = $1.3333. The analyst's own stop is
    placed at 1.7 ATRs — the median this book's stops actually sat at,
    measured 2026-08-27 and quoted in §12.1.
    """

    ENTRY = 60.10
    ATR = 4.60 / 3.45                  # ~$1.3333, back-solved from the 7.7% stop
    BAND_STOP = 55.50                  # 3.45 x ATR — what the old rule shipped
    LEVEL_STOP = 57.83                 # 1.70 x ATR — at a computed support shelf
    SHELF = 65.99                      # computed resistance; the derived target
    HORIZON = 15                       # the ~15-session hold §12.1 reasons about
    FLOOR = 1.5

    def _analysis(self, computed):
        return _vol_analysis(
            "SLB", self.ENTRY, self.LEVEL_STOP, self.SHELF, atr=self.ATR,
            horizon=self.HORIZON, computed=computed,
        )

    def test_the_band_stop_reproduces_the_1_28_that_was_refused(self):
        """Anchor the reproduction against the number the run produced."""
        rr = (self.SHELF - self.ENTRY) / (self.ENTRY - self.BAND_STOP)
        assert round(rr, 2) == 1.28
        assert rr < self.FLOOR
        # And the band stop is the 3.45 x ATR edge, not a chosen number.
        assert round(self.ENTRY - 3.45 * self.ATR, 2) == self.BAND_STOP

    def test_unbacked_slb_is_refused_exactly_as_it_was_on_2026_09_01(self):
        """With nothing computed under the analyst's stop the band applies,
        and the trade dies on the fabricated number. This is the behaviour
        that turned 38 qualified signals into zero trades."""
        constructor = PortfolioConstructor()
        decisions = constructor.construct_orders(
            targets=[_risk_target("SLB", 1.0)], positions=[],
            analyses=[self._analysis([self.SHELF])],
            total_value=EQUITY, price_map={"SLB": self.ENTRY},
        )
        assert decisions == []

    def test_a_level_backed_slb_stop_is_honoured_and_the_trade_passes(self):
        """The same trade with a computed support shelf under the stop. The
        stop is honoured at 1.7 ATRs, reward:risk is measured against it, and
        SLB becomes tradeable — the outcome §12.1 exists to produce."""
        constructor = PortfolioConstructor()
        decisions = constructor.construct_orders(
            targets=[_risk_target("SLB", 1.0)], positions=[],
            analyses=[self._analysis([self.LEVEL_STOP, self.SHELF])],
            total_value=EQUITY, price_map={"SLB": self.ENTRY},
        )
        assert len(decisions) == 1
        assert decisions[0].stop_loss == self.LEVEL_STOP
        rr = (self.SHELF - self.ENTRY) / (self.ENTRY - self.LEVEL_STOP)
        assert rr >= self.FLOOR
        # Recorded so the number is visible when the test is read, not only
        # when it fails: 1.28 refused, 2.59 taken, same chart.
        assert round(rr, 2) == 2.59

    def test_the_floor_does_not_move_to_accommodate_slb(self):
        """The honest other half, mirroring TestSLB in test_target_derivation.
        If the computed shelf is nearer, the honoured stop does not rescue the
        trade — 1.5 still binds. §12.1 changed which stop is divided by, not
        what the answer has to clear."""
        constructor = PortfolioConstructor()
        near_shelf = 62.50            # reward $2.40 against risk $2.27 = 1.06
        decisions = constructor.construct_orders(
            targets=[_risk_target("SLB", 1.0)], positions=[],
            analyses=[_vol_analysis(
                "SLB", self.ENTRY, self.LEVEL_STOP, near_shelf, atr=self.ATR,
                horizon=self.HORIZON,
                computed=[self.LEVEL_STOP, near_shelf],
            )],
            total_value=EQUITY, price_map={"SLB": self.ENTRY},
        )
        assert decisions == []


# --------------------------------------------------------------------------
# One reward:risk gate, on the geometry that ships (2026-09-02)
#
# What these pin, and why they are not a threshold change: until 2026-09-02
# `min_reward_risk_after_widening` was evaluated ONLY inside the two widening
# branches, behind unnamed early returns for "already outside the noise band"
# and "no ATR reading". A stop that was wide enough to begin with reached the
# broker with no deterministic reward:risk check at all.
#
# Measured on the pre-reset production database (data/resets/20260902T181859Z),
# 2026-08-18 to 2026-09-02: 14 of 49 constructed entry orders shipped under the
# 1.5 floor, and the floor's refusal message appears zero times in the whole
# production log history before 2026-09-02. Two reached the broker; XLE on
# 2026-08-21 FILLED 9 shares at $64.26 on a reward:risk of 0.81.
# --------------------------------------------------------------------------

# XLE, 2026-09-01, run-64290730 — the real numbers off the evidence rows.
_XLE_TA_ENTRY = 63.96     # tech_analyst snapshot price
_XLE_LIVE_ENTRY = 64.51   # what the constructor actually priced
_XLE_STOP = 61.54         # IDENTICAL in both — no stop ever moved
_XLE_TARGET = 68.00
_XLE_ATR = 1.21


def test_xle_the_1_67_versus_1_18_divergence_is_entry_drift_not_stop_geometry():
    """The rejection that started this, reproduced from production evidence.

    The Risk Manager killed the whole 2026-09-01 plan saying *"PM's reasoning
    assumes R/R 1.67 but the executed order has R/R 1.18"*. Both numbers were
    right. The STOP never moved — $61.54 on the tech analyst's row and $61.54
    on the proposed order. What moved was the ENTRY, $63.96 to $64.51, because
    the analyst measures at its snapshot price and the constructor prices the
    live market. Same trade, same stop, two entries.

    So the divergence is not a stop-geometry defect, and widening was never
    involved. It is one ratio computed at two different entries — which is why
    the division now lives in exactly one function.
    """
    from src.models import reward_to_risk

    analyst = reward_to_risk(_XLE_TA_ENTRY, _XLE_STOP, _XLE_TARGET, is_short=False)
    order = reward_to_risk(_XLE_LIVE_ENTRY, _XLE_STOP, _XLE_TARGET, is_short=False)
    assert round(analyst, 2) == 1.67
    assert round(order, 2) == 1.18
    # The stop is the one thing that is NOT different between them.
    assert analyst != order


def test_xle_is_now_refused_by_code_rather_than_by_the_risk_managers_prose():
    """On the day, the constructor SHIPPED this order at 1.18 and an LLM
    stopped it. The 1.5 floor never ran, because $61.54 was already outside
    the 2.42x ATR band (breakout setup, risk-on tape) and that path returned
    early. It runs now, and the refusal names the rule that placed the stop."""
    constructor = PortfolioConstructor()
    assert constructor._widen_stop_past_noise(
        "XLE",
        _vol_analysis("XLE", _XLE_LIVE_ENTRY, _XLE_STOP, _XLE_TARGET,
                      atr=_XLE_ATR, setup="breakout"),
        entry_price=_XLE_LIVE_ENTRY, stop_loss=_XLE_STOP,
        regime="risk-on", direction="long", target_price=_XLE_TARGET,
    ) is None


def test_a_wide_stop_that_clears_the_floor_still_ships_untouched():
    """The gate is a floor, not a tax. The same already-outside-the-band path
    with geometry that works returns the structural stop unchanged — the fix
    refuses trades, it never moves a stop it did not previously move."""
    constructor = PortfolioConstructor()
    # Stop $61.54 (2.45 ATR out, outside the 2.42x breakout band), target
    # $73.00 → reward 8.49 / risk 2.97 = 2.86.
    assert constructor._widen_stop_past_noise(
        "XLE",
        _vol_analysis("XLE", _XLE_LIVE_ENTRY, _XLE_STOP, 73.0,
                      atr=_XLE_ATR, setup="breakout"),
        entry_price=_XLE_LIVE_ENTRY, stop_loss=_XLE_STOP,
        regime="risk-on", direction="long", target_price=73.0,
    ) == _XLE_STOP


def test_a_level_backed_tight_stop_is_honoured_and_a_bare_one_is_not():
    """§12.1's rule, asserted as the contrast it actually is, on ONE fixture
    pair that differs only in whether Python computed the level.

    Both stops sit at $96.00, $4.00 (1.70 ATR) under a $100 entry — inside
    the 3.45x ATR band. The level-backed one ships at $96.00; the bare one is
    pushed to the band edge, and here that kills the trade on geometry."""
    constructor = PortfolioConstructor()

    def stop_for(computed):
        return constructor._widen_stop_past_noise(
            "MSFT",
            _vol_analysis("MSFT", _ENTRY, 96.0, 130.0, atr=_ATR,
                          computed=computed),
            entry_price=_ENTRY, stop_loss=96.0, target_price=130.0,
        )

    assert stop_for([96.0, 130.0]) == 96.0            # level backs it
    assert round(stop_for([130.0]), 2) == _BAND_EDGE  # nothing does


def test_both_reward_risk_computations_agree_on_one_trade():
    """The constructor's gate and `TradeDecision.reward_risk` must be the
    same arithmetic on the same geometry, or the Risk Manager is shown a
    ratio the floor did not judge. That gap is what the 1.67-vs-1.18
    rejection actually was."""
    from src.models import reward_to_risk

    constructor = PortfolioConstructor()
    analysis = _vol_analysis("MSFT", _ENTRY, 96.0, _UPPER_LEVEL, atr=_ATR,
                             computed=[96.0, _UPPER_LEVEL])
    decisions = constructor.construct_orders(
        targets=[_risk_target("MSFT", 1.0)], positions=[], analyses=[analysis],
        total_value=EQUITY, price_map={"MSFT": _ENTRY},
    )
    assert len(decisions) == 1
    d = decisions[0]
    gate = constructor._reward_risk_at(
        d.entry_price, d.stop_loss, d.take_profit, False,
    )
    assert d.reward_risk == round(gate, 2)
    assert d.reward_risk == round(reward_to_risk(
        d.entry_price, d.stop_loss, d.take_profit, is_short=False), 2)
    assert gate >= constructor.cfg.min_reward_risk_after_widening


def test_the_shipped_order_records_the_rule_that_placed_its_stop():
    """The execution stage must be able to tell a level-honoured stop from
    any other, without recomputing levels from different bars. It reads
    `TradeDecision.stop_rule`, which the constructor sets here."""
    from src.portfolio_constructor import (
        LEVEL_BACKED_STOP_RULES, STOP_RULE_LEVEL_HONOURED,
    )

    constructor = PortfolioConstructor()
    backed = constructor.construct_orders(
        targets=[_risk_target("MSFT", 1.0)], positions=[],
        analyses=[_vol_analysis("MSFT", _ENTRY, 96.0, _UPPER_LEVEL, atr=_ATR,
                                computed=[96.0, _UPPER_LEVEL])],
        total_value=EQUITY, price_map={"MSFT": _ENTRY},
    )
    assert backed[0].stop_rule == STOP_RULE_LEVEL_HONOURED
    assert backed[0].stop_rule in LEVEL_BACKED_STOP_RULES

    # A stop nothing computed backs: widened to the band, and the band edge
    # is not a level, so no exemption is recorded and the execution-time ATR
    # floor still applies to it.
    bare = constructor.construct_orders(
        targets=[_risk_target("MSFT", 1.0)], positions=[],
        analyses=[_vol_analysis("MSFT", _ENTRY, 96.0, 130.0, atr=_ATR,
                                computed=[130.0])],
        total_value=EQUITY, price_map={"MSFT": _ENTRY},
    )
    assert bare[0].stop_rule not in LEVEL_BACKED_STOP_RULES


# --------------------------------------------------------------------------
# Fail-closed: non-finite inputs must refuse, never permit
#
# The hazard is specific and has bitten this codebase before: `nan <= 0`,
# `nan >= entry` and `nan < floor` are ALL False, so a NaN passes every
# ordering check silently and comes out the far side looking valid.
# --------------------------------------------------------------------------

def test_a_non_finite_stop_is_refused_not_passed_through():
    """Before this, a NaN stop went through `_widen_stop_past_noise`
    untouched AND through the caller's `stop_loss >= entry_price` validity
    check, reaching `TradeDecision` intact."""
    constructor = PortfolioConstructor()
    nan = float("nan")
    for bad in (nan, float("inf"), float("-inf")):
        assert constructor._widen_stop_past_noise(
            "MSFT",
            _vol_analysis("MSFT", _ENTRY, 96.0, _UPPER_LEVEL, atr=_ATR),
            entry_price=_ENTRY, stop_loss=bad, target_price=_UPPER_LEVEL,
        ) is None


def test_a_non_finite_entry_price_is_refused():
    constructor = PortfolioConstructor()
    assert constructor._widen_stop_past_noise(
        "MSFT",
        _vol_analysis("MSFT", _ENTRY, 96.0, _UPPER_LEVEL, atr=_ATR),
        entry_price=float("nan"), stop_loss=96.0, target_price=_UPPER_LEVEL,
    ) is None


def test_a_non_finite_target_refuses_rather_than_clearing_the_floor():
    """The permissive direction is the dangerous one. `nan < 1.5` is False,
    so an unguarded NaN target would have PASSED the floor rather than
    failed it. A target that was supplied but cannot be measured is a
    refusal."""
    constructor = PortfolioConstructor()
    assert constructor._widen_stop_past_noise(
        "MSFT",
        _vol_analysis("MSFT", _ENTRY, 96.0, _UPPER_LEVEL, atr=_ATR),
        entry_price=_ENTRY, stop_loss=96.0, target_price=float("nan"),
    ) is None


def test_reward_to_risk_returns_none_for_every_malformed_geometry():
    """The one definition, and its fail-closed contract."""
    from src.models import reward_to_risk

    nan, inf = float("nan"), float("inf")
    assert reward_to_risk(100.0, 90.0, 130.0, is_short=False) == 3.0
    assert reward_to_risk(100.0, 110.0, 70.0, is_short=True) == 3.0
    # Non-finite, on any leg.
    assert reward_to_risk(nan, 90.0, 130.0, is_short=False) is None
    assert reward_to_risk(100.0, nan, 130.0, is_short=False) is None
    assert reward_to_risk(100.0, 90.0, nan, is_short=False) is None
    assert reward_to_risk(inf, 90.0, 130.0, is_short=False) is None
    # Missing, non-positive, or pointing the wrong way for the side.
    assert reward_to_risk(None, 90.0, 130.0, is_short=False) is None
    assert reward_to_risk(100.0, 0.0, 130.0, is_short=False) is None
    assert reward_to_risk(100.0, 110.0, 130.0, is_short=False) is None  # stop above a long
    assert reward_to_risk(100.0, 90.0, 90.0, is_short=False) is None    # target below entry
    assert reward_to_risk(100.0, 100.0, 130.0, is_short=False) is None  # zero risk
