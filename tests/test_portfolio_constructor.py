"""PortfolioConstructor — target state → concrete orders."""

from src.models import Position, TargetPosition, TechAnalysisResult, TechReasoningChain
from src.portfolio_constructor import PortfolioConstructor, ConstructorConfig


def _tech_rc() -> TechReasoningChain:
    """Minimal valid 5-step CoT — every field is `min_length=1`-enforced
    after the PR #89 audit fix, so test fixtures must populate them."""
    return TechReasoningChain(
        trend="x", momentum="x", volatility="x",
        volume="x", support_resistance="x",
    )


def _pos(symbol: str, qty: float, avg_entry: float, current_price: float,
         sector: str = "Technology") -> Position:
    return Position(
        symbol=symbol, qty=qty, avg_entry=avg_entry, current_price=current_price,
        market_value=qty * current_price,
        unrealized_pnl=(current_price - avg_entry) * qty,
        sector=sector,
    )


def _analysis(
    symbol: str, entry: float, stop: float, target: float,
    horizon: int = 60, atr: float | None = None,
) -> TechAnalysisResult:
    """A realistic analyst result — including the two fields production sets
    in Python rather than asking the model for.

    `atr_14` and `computed_levels` are attached by `TechAnalystAgent` after
    parsing (from the indicators and from `find_structural_levels` over the
    full history). A fixture without them is not a thing the pipeline can
    produce, and since 2026-09-01 the constructor derives the take-profit
    from `computed_levels` and refuses without them.

    The ATR is set just inside the noise band so the structural stop is left
    alone — these tests are about sizing, not about stop widening. The long
    horizon is not decoration: reaching a target W away from a stop R away
    needs sqrt(sessions) >= 2.3 * W/R once the stop is held at 3.45 ATRs, so
    a 3:1 fixture payoff genuinely implies a multi-month hold.
    """
    return TechAnalysisResult(
        symbol=symbol, rating="buy", entry_price=entry,
        stop_loss=stop, reference_target=target, reasoning="test",
        support_levels=[stop], resistance_levels=[target],
        computed_levels=[stop, target],
        atr_14=(entry - stop) / 3.5 if atr is None else atr,
        setup_type="range", expected_horizon_sessions=horizon,
        reasoning_chain=_tech_rc(),
    )


def test_construct_orders_opens_new_position():
    """Target on a symbol not currently held → BUY for the full target weight."""
    constructor = PortfolioConstructor()
    targets = [TargetPosition(symbol="NVDA", target_weight_pct=8.0,
                              conviction="high", thesis="AI")]
    analyses = [_analysis("NVDA", entry=100, stop=95, target=115)]
    price_map = {"NVDA": 100.0}

    decisions = constructor.construct_orders(
        targets=targets, positions=[], analyses=analyses,
        total_value=100_000, price_map=price_map,
    )
    assert len(decisions) == 1
    d = decisions[0]
    assert d.action == "BUY"
    assert d.symbol == "NVDA"
    assert d.entry_price == 100.0
    assert d.stop_loss == 95.0
    assert d.take_profit == 115.0
    # allocation bounded by risk budget: $100k × 0.5% = $500 at risk, $5/share →
    # 100 shares max; 100 shares × $100 = $10k = 10% weight. Target was 8%, so
    # alloc stays at 8 (under the cap).
    assert d.allocation_pct == 8.0


def test_construct_orders_trims_to_target_weight():
    """Held at 15% weight, target 10% → SELL partial equivalent to the delta."""
    constructor = PortfolioConstructor()
    # $15k position on $100k equity = 15% weight
    positions = [_pos("NVDA", qty=150, avg_entry=100, current_price=100)]
    targets = [TargetPosition(symbol="NVDA", target_weight_pct=10.0,
                              conviction="medium", thesis="trim to target")]
    analyses = [_analysis("NVDA", entry=100, stop=95, target=115)]

    decisions = constructor.construct_orders(
        targets=targets, positions=positions, analyses=analyses,
        total_value=100_000, price_map={"NVDA": 100.0},
    )
    assert len(decisions) == 1
    d = decisions[0]
    assert d.action == "SELL"
    # (15 - 10) / 15 = 33.33% of the position
    assert abs(d.allocation_pct - 33.3) < 0.5


def test_construct_orders_closes_at_zero_target():
    """target_weight_pct=0 on a held symbol → full-close SELL (alloc=100)."""
    constructor = PortfolioConstructor()
    positions = [_pos("AAPL", qty=50, avg_entry=180, current_price=200)]
    targets = [TargetPosition(symbol="AAPL", target_weight_pct=0.0,
                              conviction="low",
                              thesis="close — thesis broken")]

    decisions = constructor.construct_orders(
        targets=targets, positions=positions, analyses=[],
        total_value=100_000,
    )
    assert len(decisions) == 1
    assert decisions[0].action == "SELL"
    assert decisions[0].allocation_pct == 100.0


def test_construct_orders_skips_tiny_delta():
    """Held at 8.1%, target 8.2% → delta < min_trade_weight_delta → no order.

    Except: held positions get a HOLD row for audit continuity.
    """
    constructor = PortfolioConstructor()
    positions = [_pos("NVDA", qty=81, avg_entry=100, current_price=100)]  # 8.1%
    targets = [TargetPosition(symbol="NVDA", target_weight_pct=8.2,
                              conviction="high", thesis="keep")]

    decisions = constructor.construct_orders(
        targets=targets, positions=positions, analyses=[],
        total_value=100_000, price_map={"NVDA": 100.0},
    )
    # delta 0.1% < 0.5% default threshold → HOLD, not a tradeable order
    assert len(decisions) == 1
    assert decisions[0].action == "HOLD"


def test_construct_orders_risk_budget_caps_buy_size():
    """Wide-stop name: the single-name risk budget caps below the target weight.

    Pinned at 0.5% explicitly rather than relying on the default, which is now
    the owner-ratified 5% envelope (2026-08-27). What this test is for is the
    capping MECHANISM on a legacy notional target, not the size of the budget.
    """
    from src.portfolio_constructor import ConstructorConfig
    constructor = PortfolioConstructor(ConstructorConfig(risk_budget_pct=0.5))
    # Target 10% on $100k = $10k = 100 shares @ $100.
    # Stop 80 → risk_per_share = $20. Risk budget $500 / $20 = 25 shares max
    # → 25 × $100 = $2500 = 2.5% weight cap.
    targets = [TargetPosition(symbol="NVDA", target_weight_pct=10.0,
                              conviction="high", thesis="deep stop")]
    analyses = [_analysis("NVDA", entry=100, stop=80, target=140)]

    decisions = constructor.construct_orders(
        targets=targets, positions=[], analyses=analyses,
        total_value=100_000, price_map={"NVDA": 100.0},
    )
    assert len(decisions) == 1
    # Capped from 10 → 2.5
    assert abs(decisions[0].allocation_pct - 2.5) < 0.05


def test_construct_orders_orders_sells_before_buys():
    """Rotation: a close + a new open → SELL returned first so cash refreshes."""
    constructor = PortfolioConstructor()
    positions = [_pos("AAPL", qty=50, avg_entry=180, current_price=200)]
    targets = [
        TargetPosition(symbol="AAPL", target_weight_pct=0.0,
                       conviction="low", thesis="close"),
        TargetPosition(symbol="NVDA", target_weight_pct=8.0,
                       conviction="high", thesis="open"),
    ]
    analyses = [_analysis("NVDA", entry=100, stop=95, target=115)]

    decisions = constructor.construct_orders(
        targets=targets, positions=positions, analyses=analyses,
        total_value=100_000, price_map={"AAPL": 200.0, "NVDA": 100.0},
    )
    assert len(decisions) == 2
    assert decisions[0].action == "SELL"
    assert decisions[0].symbol == "AAPL"
    assert decisions[1].action == "BUY"
    assert decisions[1].symbol == "NVDA"


def test_construct_orders_orders_buys_by_weight_descending():
    """BUYs should be prioritized by larger target weight under cash rationing."""
    constructor = PortfolioConstructor()
    targets = [
        TargetPosition(symbol="AAPL", target_weight_pct=3.0,
                       conviction="medium", thesis="smaller"),
        TargetPosition(symbol="NVDA", target_weight_pct=8.0,
                       conviction="high", thesis="larger"),
    ]
    analyses = [
        _analysis("AAPL", entry=200, stop=190, target=220),
        _analysis("NVDA", entry=100, stop=95, target=115),
    ]

    decisions = constructor.construct_orders(
        targets=targets, positions=[], analyses=analyses,
        total_value=100_000, price_map={"AAPL": 200.0, "NVDA": 100.0},
    )

    assert [d.symbol for d in decisions] == ["NVDA", "AAPL"]


def test_construct_orders_uses_suggested_stop_when_provided():
    """PM override: target.suggested_stop_price wins over TA's stop."""
    constructor = PortfolioConstructor()
    targets = [TargetPosition(symbol="NVDA", target_weight_pct=5.0,
                              conviction="medium", thesis="tighter stop",
                              suggested_stop_price=97.5)]
    # Low ATR so the PM's tighter stop sits OUTSIDE the noise band and is
    # left alone — this test is about stop precedence, not stop widening.
    analyses = [_analysis("NVDA", entry=100, stop=95, target=110, atr=0.7)]

    decisions = constructor.construct_orders(
        targets=targets, positions=[], analyses=analyses,
        total_value=100_000, price_map={"NVDA": 100.0},
    )
    assert len(decisions) == 1
    assert decisions[0].stop_loss == 97.5  # PM's override, not TA's 95


def test_construct_orders_rejects_buy_without_price_reference():
    """No market_price AND no TA analysis → constructor skips the BUY."""
    constructor = PortfolioConstructor()
    targets = [TargetPosition(symbol="UNKNOWN", target_weight_pct=5.0,
                              conviction="medium", thesis="blind buy")]

    decisions = constructor.construct_orders(
        targets=targets, positions=[], analyses=[],
        total_value=100_000, price_map={},  # no price for UNKNOWN
    )
    # No price, no analysis → can't construct → empty result
    assert decisions == []


def test_construct_orders_rejects_buy_when_no_structural_stop_supplied():
    """No suggested stop, no TA analysis → no structural stop is available,
    so the BUY is rejected outright.

    Previously (before 2026-08-27) this fell back to entry × (1 - fallback_pct).
    That ATR/percent fallback family was deleted on purpose — a stop nobody
    derived from the chart can't be risk-sized honestly. Renamed from
    `test_construct_orders_falls_back_to_fallback_stop_when_no_hint`.
    """
    constructor = PortfolioConstructor()
    targets = [TargetPosition(symbol="NVDA", target_weight_pct=5.0,
                              conviction="medium", thesis="no TA")]

    decisions = constructor.construct_orders(
        targets=targets, positions=[], analyses=[],  # NO analysis
        total_value=100_000, price_map={"NVDA": 100.0},
    )
    # No structural stop from any source → the BUY is dropped, not sized
    # against an invented one.
    assert decisions == []


def test_resolve_stop_returns_none_when_no_structural_stop_supplied():
    """When `analysis.stop_loss` is None and `target.suggested_stop_price`
    is unset, `_resolve_stop` returns None — no structural stop, no trade.

    Previously (before 2026-08-27) this asserted a volatility-aware
    `entry − 2*ATR` fallback when `analysis.atr_14` was available. That
    fallback was deleted on purpose: it let PortfolioConstructor invent a
    stop nobody derived from the chart. Renamed from
    `test_resolve_stop_atr_fallback_when_llm_stop_missing`. We test
    `_resolve_stop` directly with a SimpleNamespace stand-in to bypass the
    model validator (which now requires a real stop_loss for actionable
    ratings anyway).
    """
    from types import SimpleNamespace
    constructor = PortfolioConstructor()
    target = TargetPosition(
        symbol="NVDA", target_weight_pct=5.0,
        conviction="medium", thesis="no structural stop",
    )
    # ATR is retained only as a noise-band input elsewhere; it no longer
    # feeds a stop fallback here.
    fake_analysis = SimpleNamespace(stop_loss=None, atr_14=8.0)
    stop = constructor._resolve_stop(target, fake_analysis, entry_price=100.0)
    assert stop is None


def test_resolve_stop_llm_stop_wins_over_atr():
    """LLM-supplied stop_loss takes precedence over ATR fallback."""
    from types import SimpleNamespace
    constructor = PortfolioConstructor(config=ConstructorConfig())
    target = TargetPosition(
        symbol="NVDA", target_weight_pct=5.0,
        conviction="medium", thesis="x",
    )
    fake_analysis = SimpleNamespace(stop_loss=90.0, atr_14=8.0)
    stop = constructor._resolve_stop(target, fake_analysis, entry_price=100.0)
    assert stop == 90.0


def test_resolve_stop_returns_none_when_neither_stop_nor_atr_available():
    """When neither LLM stop, ATR, nor a suggested stop is available,
    `_resolve_stop` returns None — there's no hardcoded % to fall through to
    anymore.

    Previously (before 2026-08-27) this asserted a fall-through to the
    hardcoded 5% fallback — same as the pre-audit behaviour. That fallback
    was deleted on purpose. Renamed from
    `test_resolve_stop_falls_through_to_pct_when_no_atr`.
    """
    from types import SimpleNamespace
    constructor = PortfolioConstructor()
    target = TargetPosition(
        symbol="NVDA", target_weight_pct=5.0,
        conviction="medium", thesis="x",
    )
    fake_analysis = SimpleNamespace(stop_loss=None, atr_14=None)
    stop = constructor._resolve_stop(target, fake_analysis, entry_price=100.0)
    assert stop is None


def test_construct_orders_empty_targets_returns_empty():
    constructor = PortfolioConstructor()
    assert constructor.construct_orders(
        targets=[], positions=[], analyses=[], total_value=100_000,
    ) == []


def test_construct_orders_skips_sell_when_position_market_value_is_nan():
    """Broker price glitch can produce qty>0 with market_value=NaN
    (current_price came back NaN, then qty * NaN = NaN). Without this
    guard, current_pct = NaN / total_value * 100 = NaN, the partial
    fraction math is NaN, alloc becomes NaN, and a NaN allocation_pct
    gets sent to the broker. R4 audit finding — pin the guard."""
    constructor = PortfolioConstructor()
    nan_position = Position(
        symbol="GLITCH", qty=100, avg_entry=100.0, current_price=float("nan"),
        market_value=float("nan"),  # broker glitch
        unrealized_pnl=0.0,
        sector="Technology",
    )
    targets = [TargetPosition(
        symbol="GLITCH", target_weight_pct=5.0,
        conviction="medium", thesis="trim to 5%",
    )]

    decisions = constructor.construct_orders(
        targets=targets, positions=[nan_position],
        analyses=[], total_value=100_000,
    )
    # The SELL is dropped — no NaN-tainted orders leak to the broker.
    sells = [d for d in decisions if d.action == "SELL"]
    assert sells == []


def test_resolve_stop_returns_none_when_no_structural_stop_supplied_high_vol(caplog):
    """High-vol name with no structural stop from either source: `_resolve_stop`
    returns None + WARNING log so the BUY gets rejected upstream rather than
    silently sized against an invented stop.

    Previously (before 2026-08-27) this exercised the "2*ATR >= entry_price"
    edge case of the now-deleted ATR fallback (`entry - 2*ATR` going
    non-positive at ATR=60 on a $100 stock). That whole fallback tier is
    gone, so any missing-stop case — high-vol or not — takes this same
    None-returning path now. Renamed from
    `test_resolve_stop_returns_none_when_atr_too_wide_for_entry`.
    """
    import logging
    from types import SimpleNamespace
    constructor = PortfolioConstructor()
    target = TargetPosition(
        symbol="MICRO", target_weight_pct=3.0,
        conviction="low", thesis="too volatile",
    )
    fake_analysis = SimpleNamespace(stop_loss=None, atr_14=60.0)

    with caplog.at_level(logging.WARNING):
        stop = constructor._resolve_stop(target, fake_analysis, entry_price=100.0)

    assert stop is None, (
        "no structural stop from either source must reject rather than "
        "invent one"
    )
    assert any(
        "no structural stop" in r.message and target.symbol in r.message
        for r in caplog.records
    ), "rejection must log the reason so the operator can see why the BUY was dropped"


def test_resolve_stop_returns_none_when_genuinely_no_stop_information():
    """A brand-new symbol with no volatility history and no LLM-supplied
    stop still resolves to None — there is no naive-percent fallback left
    to catch it.

    Previously (before 2026-08-27) this asserted the 5% fallback WAS the
    right answer when there was genuinely no volatility info. That
    fallback was deleted on purpose. Renamed from
    `test_resolve_stop_uses_pct_fallback_when_atr_truly_unavailable`.
    """
    from types import SimpleNamespace
    constructor = PortfolioConstructor()
    target = TargetPosition(
        symbol="NEWIPO", target_weight_pct=3.0,
        conviction="low", thesis="x",
    )
    fake_analysis = SimpleNamespace(stop_loss=None, atr_14=None)
    stop = constructor._resolve_stop(target, fake_analysis, entry_price=100.0)
    assert stop is None


def test_construct_orders_rejects_buy_when_no_reference_target_supplied():
    """New coverage (2026-08-27): a structural stop is present but the
    Tech Analyst supplied no `reference_target` → the BUY is rejected.
    Targets are no longer synthesized from `entry * (1 + 2*stop_gap_pct)`;
    a missing target now means no trade, same as a missing stop.
    """
    from types import SimpleNamespace
    constructor = PortfolioConstructor()
    targets = [TargetPosition(symbol="NVDA", target_weight_pct=5.0,
                              conviction="medium", thesis="no target")]
    # A stop is present (so this isn't the missing-stop rejection path) but
    # reference_target is None.
    fake_analysis = SimpleNamespace(
        symbol="NVDA", stop_loss=95.0, atr_14=None, reference_target=None,
    )

    decisions = constructor.construct_orders(
        targets=targets, positions=[], analyses=[fake_analysis],
        total_value=100_000, price_map={"NVDA": 100.0},
    )
    assert decisions == []


def test_current_weights_applies_gross_multiplier_for_inverse_etfs():
    """The constructor must agree with RiskRuleEngine.check on what
    a 20% target means for inverse / leveraged ETFs. Pre-fix the
    constructor used raw market_value/total_value while engine used
    gross multiplier — same $10K SQQQ saw 10% in constructor, 30%
    gross at the engine. That mismatch caused every leveraged-ETF
    target at the ceiling to hard-block at the engine while the
    constructor saw no need to trim.

    Now both use gross. PM's target_weight_pct=20 on SQQQ means
    20% GROSS exposure (≈ 6.67% raw notional)."""
    from src.portfolio_constructor import PortfolioConstructor

    # 3 positions: SPY (raw=1x), SDS (inverse -2x → gross 2x), SQQQ (inverse -3x → gross 3x).
    spy = Position(
        symbol="SPY", qty=100, avg_entry=500.0, current_price=500.0,
        market_value=50000.0, unrealized_pnl=0.0, sector="Broad",
    )
    sds = Position(
        symbol="SDS", qty=200, avg_entry=50.0, current_price=50.0,
        market_value=10000.0, unrealized_pnl=0.0, sector="Broad",
    )
    sqqq = Position(
        symbol="SQQQ", qty=100, avg_entry=100.0, current_price=100.0,
        market_value=10000.0, unrealized_pnl=0.0, sector="Broad",
    )

    total_value = 200000.0
    weights = PortfolioConstructor._current_weights(
        [spy, sds, sqqq], total_value=total_value,
    )

    # SPY: market_value 50000, gross_mul 1.0 → 50000/200000 * 100 = 25%
    assert abs(weights["SPY"] - 25.0) < 1e-6
    # SDS: market_value 10000, gross_mul 2.0 → 10000 * 2 / 200000 * 100 = 10%
    assert abs(weights["SDS"] - 10.0) < 1e-6
    # SQQQ: market_value 10000, gross_mul 3.0 → 10000 * 3 / 200000 * 100 = 15%
    assert abs(weights["SQQQ"] - 15.0) < 1e-6


def test_current_weights_zero_or_negative_total_value_returns_empty():
    """Sanity: NaN / 0 / negative total_value still short-circuits to
    empty dict. The fix didn't change this guardrail."""
    from src.portfolio_constructor import PortfolioConstructor
    pos = Position(
        symbol="SPY", qty=100, avg_entry=500.0, current_price=500.0,
        market_value=50000.0, unrealized_pnl=0.0, sector="Broad",
    )
    assert PortfolioConstructor._current_weights([pos], total_value=0) == {}
    assert PortfolioConstructor._current_weights([pos], total_value=-100) == {}


def test_risk_budget_cap_carries_provenance_note_for_rm():
    """2026-08-20 veto forensic: the RM read 'PM said 15%, order says
    10.65%' as plan incoherence and issued a full-plan rejection —
    nobody had told it the constructor's risk budget capped the size.
    A capped BUY's reasoning must carry the [constructor: ...] note."""
    from src.models import TargetPosition, TechAnalysisResult

    from src.portfolio_constructor import ConstructorConfig
    constructor = PortfolioConstructor(ConstructorConfig(risk_budget_pct=0.5))
    target = TargetPosition(
        symbol="XLE", target_weight_pct=15.0, conviction="high",
        thesis="Energy geopolitical tailwind.", thesis_invalid_if="", catalyst="",
    )
    # Wide stop: entry 100, stop 90 -> risk 10/share. 0.5% risk budget on
    # 10_000 equity = $50 -> 5 shares -> $500 = 5% alloc, well under 15%.
    analysis = _analysis("XLE", entry=100.0, stop=90.0, target=130.0)
    decisions = constructor.construct_orders(
        targets=[target], positions=[], analyses=[analysis],
        total_value=10_000.0, price_map={"XLE": 100.0},
    )

    assert len(decisions) == 1
    d = decisions[0]
    assert d.allocation_pct < 15.0
    assert "[constructor:" in d.reasoning
    assert "not PM inconsistency" in d.reasoning


def test_uncapped_buy_has_no_provenance_note():
    from src.models import TargetPosition, TechAnalysisResult

    constructor = PortfolioConstructor()
    target = TargetPosition(
        symbol="XLF", target_weight_pct=5.0, conviction="medium",
        thesis="Financials steepener.", thesis_invalid_if="", catalyst="",
    )
    # Tight stop: entry 100, stop 99 -> cap = 0.5%*100/1 = 50% >> 5%.
    analysis = _analysis("XLF", entry=100.0, stop=99.0, target=110.0)
    decisions = constructor.construct_orders(
        targets=[target], positions=[], analyses=[analysis],
        total_value=10_000.0, price_map={"XLF": 100.0},
    )

    assert len(decisions) == 1
    assert "[constructor:" not in decisions[0].reasoning


def test_dropped_target_reason_is_captured_not_silently_lost():
    """Funnel-queue item 2 (2026-09-03) reproduction: a target the
    constructor drops before ever building an order used to leave NOTHING
    recoverable outside the log — `blocked_proposals_census.py` counted 19
    of these across 2026-08-18..09-02 as `no_order_built`, its largest
    unexplained bucket, and the module docstring on that script says the
    constructor's own reason "is only ever logger.info/logger.warning
    text — never persisted to a table."

    Same fixture as `test_construct_orders_rejects_buy_when_no_structural_stop_supplied`
    (no analysis at all → no structural stop → the BUY is dropped). The
    fix under test is `PortfolioConstructor.last_drop_reasons`: the real
    log line the constructor already emits for the drop, captured onto the
    instance so a caller (`pipeline_stages.DecisionStage`) can persist a
    terminal per-symbol evidence row instead of nothing. This test is
    scoped to the constructor's own contract — the DecisionStage
    integration (the actual DB write) is exercised by the pipeline-level
    fixtures, not re-derived here.
    """
    constructor = PortfolioConstructor()
    targets = [TargetPosition(symbol="NVDA", target_weight_pct=5.0,
                              conviction="medium", thesis="no TA")]

    decisions = constructor.construct_orders(
        targets=targets, positions=[], analyses=[],  # NO analysis
        total_value=100_000, price_map={"NVDA": 100.0},
    )

    assert decisions == []
    # The old failure mode: nothing whatsoever survives the call. The fix:
    # the dropped symbol's real reason is on the instance afterward.
    assert "NVDA" in constructor.last_drop_reasons
    reason = constructor.last_drop_reasons["NVDA"]
    assert "NVDA" in reason
    assert "rejected" in reason

    # A second call must not leak the first call's reasons onto a run that
    # dropped nothing — each call's capture is fresh, not cumulative.
    clean_analysis = _analysis("XLF", entry=100.0, stop=99.0, target=110.0)
    clean_target = TargetPosition(symbol="XLF", target_weight_pct=5.0,
                                   conviction="medium", thesis="clean")
    constructor.construct_orders(
        targets=[clean_target], positions=[], analyses=[clean_analysis],
        total_value=10_000.0, price_map={"XLF": 100.0},
    )
    assert constructor.last_drop_reasons == {}


# === thesis_invalid_if: dedicated field, not lossy text (2026-09-03) ===
#
# `TargetPosition.thesis_invalid_if` used to reach `TradeDecision` ONLY as
# text appended to `reasoning` — "(invalid if: ...)" / "(thesis_invalid_if:
# ...)" — and `reasoning` is truncated to 500 chars at every builder site
# below, then AGAIN to 280 chars by `TradingPipeline._build_position_
# history`. A long condition could silently fall past either cut. These
# tests prove the new `TradeDecision.thesis_invalid_if` field survives
# completely intact regardless of what happens to `reasoning`.

_LONG_INVALID_IF = (
    "This thesis is invalidated if the stock closes below the rising "
    "50-day simple moving average on a weekly closing basis for two "
    "consecutive weeks, OR if the company's next quarterly earnings "
    "report shows gross margin compression of more than 300 basis points "
    "year-over-year, OR if the sector rotation model flips this GICS "
    "sub-industry from leadership to laggard status for more than ten "
    "consecutive trading sessions, OR if a director or the CEO sells "
    "more than 2% of their disclosed beneficial ownership stake outside "
    "of a pre-scheduled 10b5-1 trading plan in a single reported window."
)
# Long enough to survive nothing: it blows past both the 500-char builder
# truncation AND the 280-char position-history truncation.
assert len(_LONG_INVALID_IF) > 500


def _extract_embedded_invalid_if(reasoning: str) -> str | None:
    """Mirrors how a downstream reader would regex the OLD embedded marker
    back out of `reasoning` — used here only to demonstrate that path is
    lossy once truncation has already run over it."""
    import re
    m = re.search(r"\((?:invalid if|thesis_invalid_if): (.*)\)\s*$", reasoning)
    return m.group(1) if m else None


def test_buy_thesis_invalid_if_survives_full_length_unlike_embedded_reasoning():
    constructor = PortfolioConstructor()
    target = TargetPosition(
        symbol="NVDA", target_weight_pct=8.0, conviction="high",
        thesis="AI leadership", thesis_invalid_if=_LONG_INVALID_IF,
    )
    analyses = [_analysis("NVDA", entry=100, stop=95, target=115)]
    decisions = constructor.construct_orders(
        targets=[target], positions=[], analyses=analyses,
        total_value=100_000, price_map={"NVDA": 100.0},
    )
    assert len(decisions) == 1
    d = decisions[0]
    assert d.action == "BUY"
    # The new field: complete, untruncated.
    assert d.thesis_invalid_if == _LONG_INVALID_IF

    # The old path really is lossy: `reasoning` is capped at 500 chars, so
    # the embedded marker's payload — appended AFTER the cut — never makes
    # it into the string a regex could extract at all.
    assert len(d.reasoning) <= 500 + 200  # + cap_note/target_note headroom
    embedded = _extract_embedded_invalid_if(d.reasoning)
    assert embedded is None or embedded != _LONG_INVALID_IF


def test_short_thesis_invalid_if_survives_full_length_unlike_embedded_reasoning():
    constructor = PortfolioConstructor()
    target = TargetPosition(
        symbol="TSLA", direction="short", target_weight_pct=5.0,
        conviction="high", thesis="overvalued",
        thesis_invalid_if=_LONG_INVALID_IF,
    )
    analysis = TechAnalysisResult(
        symbol="TSLA", rating="sell", entry_price=250.0, stop_loss=262.5,
        reference_target=200.0, reasoning="test",
        support_levels=[200.0], resistance_levels=[262.5],
        computed_levels=[200.0],
        computed_level_touches={200.0: 5},
        setup_type="range", expected_horizon_sessions=60,
        reasoning_chain=_tech_rc(),
        atr_14=(262.5 - 250.0) / 3.5,
    )
    decisions = constructor.construct_orders(
        targets=[target], positions=[], analyses=[analysis],
        total_value=100_000, price_map={"TSLA": 250.0},
    )
    assert len(decisions) == 1
    d = decisions[0]
    assert d.action == "SHORT"
    assert d.thesis_invalid_if == _LONG_INVALID_IF
    embedded = _extract_embedded_invalid_if(d.reasoning)
    assert embedded is None or embedded != _LONG_INVALID_IF


def test_sell_thesis_invalid_if_survives_full_length_unlike_embedded_reasoning():
    constructor = PortfolioConstructor()
    positions = [_pos("AAPL", qty=50, avg_entry=180, current_price=200)]
    target = TargetPosition(
        symbol="AAPL", target_weight_pct=0.0, conviction="low",
        thesis="close — thesis broken", thesis_invalid_if=_LONG_INVALID_IF,
    )
    decisions = constructor.construct_orders(
        targets=[target], positions=positions, analyses=[],
        total_value=100_000,
    )
    assert len(decisions) == 1
    d = decisions[0]
    assert d.action == "SELL"
    assert d.thesis_invalid_if == _LONG_INVALID_IF
    # reasoning is capped hard at 500 with no headroom for SELL.
    assert len(d.reasoning) <= 500
    embedded = _extract_embedded_invalid_if(d.reasoning)
    assert embedded is None or embedded != _LONG_INVALID_IF


def test_hold_and_no_condition_leave_the_field_none():
    """No stated condition → the field is None, not an empty string, on
    every action — matching the conviction-ledger fields' own discipline."""
    constructor = PortfolioConstructor()
    positions = [_pos("NVDA", qty=81, avg_entry=100, current_price=100)]
    target = TargetPosition(symbol="NVDA", target_weight_pct=8.2,
                             conviction="high", thesis="keep")
    decisions = constructor.construct_orders(
        targets=[target], positions=positions, analyses=[],
        total_value=100_000, price_map={"NVDA": 100.0},
    )
    assert len(decisions) == 1
    assert decisions[0].action == "HOLD"
    assert decisions[0].thesis_invalid_if is None


# --------------------------------------------------------------------------
# `real_reward_risk_preview` — 2026-09-04 fix (audit finding).
#
# The PM's own eligibility gate used to read `TechAnalysisResult.
# risk_reward`, real arithmetic but over the analyst's own GUESSED target,
# never checked against structure. This method gives that earlier gate the
# SAME derived-target, noise-floor-widened number `construct_orders` gates
# on, by calling the same `_derive_target` / `_widen_stop_past_noise`
# rather than a second copy of the logic. These tests hand-verify the
# arithmetic on the same entry/stop/ATR fixture shape `_analysis` above
# uses, with `computed_levels` chosen to land exactly on a directly
# reachable structural level — no measured-move sqrt term to hand-check.
# --------------------------------------------------------------------------

def _structured_analysis(
    symbol: str, *, entry: float, stop: float, model_target: float,
    computed_levels: list[float], rating: str = "buy",
    horizon: int = 60,
) -> TechAnalysisResult:
    """Same ATR convention as `_analysis`: `(entry - stop) / 3.5` sits the
    unwidened stop just outside the noise band (§12.1's `OUTSIDE_BAND`
    path), so the stop the preview measures against is `stop`, unchanged.
    `model_target` never affects the derivation — see `derive_structural_
    target` — it only sets `TechAnalysisResult.risk_reward`, the self-
    reported figure this fix stops trusting.
    """
    is_short = rating in ("sell", "strong_sell")
    return TechAnalysisResult(
        symbol=symbol, rating=rating, conviction="medium", entry_price=entry,
        stop_loss=stop, reference_target=model_target,
        support_levels=[stop], resistance_levels=[model_target],
        computed_levels=computed_levels,
        atr_14=abs(entry - stop) / 3.5,
        setup_type="range", expected_horizon_sessions=horizon,
        reasoning="test", reasoning_chain=_tech_rc(),
    )


def test_real_preview_excludes_a_candidate_the_model_overstates():
    """Model claims R/R 10.0 (target $150 off a $5 stop). The nearest REAL
    structural level above entry is $103 — reward $3 / risk $5 = 0.60,
    under the 1.5 floor. The self-reported number would have passed the
    OLD gate; the real one must not."""
    analysis = _structured_analysis(
        "NVDA", entry=100.0, stop=95.0, model_target=150.0,
        computed_levels=[95.0, 103.0],
    )
    assert analysis.risk_reward == 10.0  # the self-reported figure — overstated
    constructor = PortfolioConstructor()
    real_rr = constructor.real_reward_risk_preview(analysis, "long")
    assert real_rr is None  # unmeasurable-or-under-floor, same contract as _widen_stop_past_noise


def test_real_preview_includes_a_candidate_the_model_understates():
    """Model claims R/R 0.8 (target $104 off a $5 stop) — sub-floor.
    The nearest REAL structural level above entry is $108, directly
    reachable — reward $8 / risk $5 = 1.60, clearing the 1.5 floor. The
    self-reported number would have pruned this at the OLD gate; the real
    one must keep it."""
    analysis = _structured_analysis(
        "GEV", entry=100.0, stop=95.0, model_target=104.0,
        computed_levels=[95.0, 108.0],
    )
    assert analysis.risk_reward == 0.8  # the self-reported figure — understated
    constructor = PortfolioConstructor()
    real_rr = constructor.real_reward_risk_preview(analysis, "long")
    assert real_rr == 1.6


def test_real_preview_mirrors_the_shorts_construction_would_take():
    """Direction-aware, same as every other §12.1 rule: a short's nearest
    real level is BELOW entry. Entry $100 / stop $105 (risk $5) / nearest
    computed level $92 (reward $8) — R/R 1.60, clears the floor — while the
    model's own guessed target of $99 would self-report as sub-floor
    (R/R 0.2)."""
    analysis = _structured_analysis(
        "TSLA", entry=100.0, stop=105.0, model_target=99.0,
        computed_levels=[105.0, 92.0], rating="sell",
    )
    assert analysis.risk_reward == 0.2
    constructor = PortfolioConstructor()
    real_rr = constructor.real_reward_risk_preview(analysis, "short")
    assert real_rr == 1.6


def test_real_preview_is_none_with_no_computed_structure():
    """A fixture with no `computed_levels` (e.g. a pre-§12.1 recording, or
    a name with too little price history to compute structure) refuses —
    same fail-closed contract as `derive_structural_target` itself, never
    silently permitted through."""
    analysis = _analysis("NVDA", entry=100, stop=95, target=150)
    analysis.computed_levels = []
    constructor = PortfolioConstructor()
    assert constructor.real_reward_risk_preview(analysis, "long") is None
