"""Regression suite for the 2026-09-01 sector-cap-exemption defect.

THE DEFECT: a symbol's sector comes from a live network lookup
(`src.execution.broker._get_sector`); only ~21 ETFs have an offline static
fallback (`_ETF_SECTORS`). When the lookup fails or times out, the symbol
resolves to "Unknown" — and `RiskRuleEngine.check`'s sector-concentration
rule (rule 5, spec §12.2/§10.3) used to read `new_sector != "Unknown"` as
"skip this check entirely", i.e. EXEMPT from BOTH the soft concentration
target (`max_sector_pct`, advisory) and the absolute hard ceiling
(`max_sector_hard_pct`, a real HARD BLOCK — see `src.pipeline.HARD_BLOCK_RULES`).
Symmetrically, a HELD position stamped sector="Unknown" the same way was
excluded by `sector_side_gross`'s default (`include_unknown=False`) and
vanished from every sector's exposure. With margin arriving at 2.0x, a
network blip therefore silently switched off the only thing standing
between a lookup failure and unlimited concentration in one sector.

THE FIX: the gate (`RiskRuleEngine.check` rule 5 and
`accumulate_pending_sector`) now pools "Unknown" as its own `(sector, side)`
bucket and runs it through the SAME soft/hard pair as a real sector —
conservative, not exempt. This is a narrow, deliberate carve-out: the
CONSTRUCTOR's sizing pass (`PortfolioConstructor`, fractional sizing —
untouched, out of scope for this fix) still calls `sector_side_gross` with
its original `include_unknown=False` default. A resolution failure also now
raises an advisory `RiskViolation` that distinguishes a transient lookup
failure from a symbol that genuinely has no sector, and
`src.pipeline_stages._apply_sector_unresolved_alert` promotes it into
`data_status["sector"]` — the same generic channel `notifier.py` /
`trader_feed.py` already render as a plain "degraded" line in the session
output (the owner's Telegram alert).
"""
from unittest.mock import patch

import pytest

from src.config import RiskConfig
from src.models import Position, TradeDecision
from src.pipeline import HARD_BLOCK_RULES
from src.risk.rules import RiskRuleEngine


def _engine(**overrides) -> RiskRuleEngine:
    kwargs = dict(
        max_position_pct=50.0, max_total_position_pct=300.0,
        max_daily_loss_pct=3.0, max_sector_pct=40.0,
        max_sector_hard_pct=60.0, require_stop_loss=True, allow_margin=True,
    )
    kwargs.update(overrides)
    return RiskRuleEngine(RiskConfig(**kwargs))


def _held(symbol: str, market_value: float, sector: str) -> Position:
    return Position(
        symbol=symbol, qty=market_value / 100.0, avg_entry=100.0,
        current_price=100.0, market_value=market_value,
        unrealized_pnl=0.0, sector=sector,
    )


def _buy(symbol: str, allocation_pct: float, entry: float = 100.0) -> TradeDecision:
    return TradeDecision(
        action="BUY", symbol=symbol, allocation_pct=allocation_pct,
        entry_price=entry, stop_loss=entry * 0.9, take_profit=entry * 1.2,
        reasoning="t",
    )


# ===========================================================================
# 1. A symbol whose sector lookup fails is NOT exempt from the cap.
# ===========================================================================

def test_unresolved_sector_is_not_exempt_from_the_hard_ceiling():
    """Pre-fix: `_get_sector` returning "Unknown" skipped rule 5 entirely
    (both the soft target AND the hard ceiling), so this BUY into an
    already-50%-Unknown-sector book sailed through with zero violations.
    Post-fix it is pooled and must hit the same 60% hard ceiling a real
    sector would.
    """
    eng = _engine(max_sector_pct=40.0, max_sector_hard_pct=60.0)
    positions = [_held("XYZ1", 50_000.0, sector="Unknown")]  # 50% of book
    decision = _buy("XYZ2", allocation_pct=15.0)  # would push pool to 65%

    with patch("src.execution.broker._get_sector", return_value="Unknown"):
        violations = eng.check(
            decision=decision, positions=positions,
            total_value=100_000.0, daily_pnl=0.0, cash=100_000.0,
        )

    hard = [v for v in violations if v.rule in HARD_BLOCK_RULES]
    assert [v.rule for v in hard] == ["max_sector_hard_pct"], (
        f"an unresolved sector must be constrained by the hard ceiling, "
        f"not exempt from it; got {[v.rule for v in violations]}"
    )


def test_resolved_sector_book_would_not_have_tripped_the_same_ceiling():
    """Sanity check for the test above: with the SAME numbers but a real,
    DISTINCT resolved sector for the held position, the new BUY's own
    (different, resolved) sector does not breach the ceiling — proving the
    prior test's block came from pooling "Unknown", not an unrelated
    coincidence."""
    eng = _engine(max_sector_pct=40.0, max_sector_hard_pct=60.0)
    positions = [_held("XYZ1", 50_000.0, sector="Healthcare")]
    decision = _buy("XYZ2", allocation_pct=15.0)

    with patch("src.execution.broker._get_sector", return_value="Technology"):
        violations = eng.check(
            decision=decision, positions=positions,
            total_value=100_000.0, daily_pnl=0.0, cash=100_000.0,
        )

    assert not [v for v in violations if v.rule in HARD_BLOCK_RULES]


# ===========================================================================
# 2. The condition raises an owner-visible alert when it affects a decision.
# ===========================================================================

def test_unresolved_sector_raises_an_advisory_violation():
    """Distinct from the hard block above: even when the pooled bucket does
    NOT breach either cap, an unresolved sector must still surface — this
    is what src/pipeline_stages.py promotes into `data_status["sector"]`
    (the same channel news/macro degradation use)."""
    eng = _engine(max_sector_pct=90.0, max_sector_hard_pct=95.0)  # no block
    decision = _buy("XYZ2", allocation_pct=5.0)

    with patch("src.execution.broker._get_sector", return_value="Unknown"):
        violations = eng.check(
            decision=decision, positions=[],
            total_value=100_000.0, daily_pnl=0.0, cash=100_000.0,
        )

    alerts = [v for v in violations if v.rule.startswith("sector_unresolved")]
    assert alerts, f"expected an unresolved-sector advisory; got {[v.rule for v in violations]}"
    assert not [v for v in violations if v.rule in HARD_BLOCK_RULES]


def test_unresolved_sector_alert_promotes_to_data_status():
    """`_apply_sector_unresolved_alert` is what makes the advisory actually
    reach the session output / Telegram alert — the same generic
    `data_status[...] not in ("ok", "empty")` -> "degraded" line every
    other upstream source (news, macro, smart_money) already uses."""
    from src.pipeline_stages import _apply_sector_unresolved_alert
    from src.risk.rules import RiskViolation

    data_status: dict = {"news": "ok"}
    violations = [
        RiskViolation(
            rule="sector_unresolved_lookup_failed",
            message="AAPL: sector lookup failed or timed out.",
            value=12.0, limit=40.0,
        ),
    ]
    _apply_sector_unresolved_alert(data_status, violations)
    assert data_status["sector"] == "degraded"

    degraded = [k for k, v in data_status.items() if v not in ("ok", "empty")]
    assert "sector" in degraded, "must land in the same degraded-sources list notifier.py renders"


def test_no_alert_when_nothing_unresolved():
    """Negative case for the promotion helper: a clean run must not
    manufacture a sector alert."""
    from src.pipeline_stages import _apply_sector_unresolved_alert
    from src.risk.rules import RiskViolation

    data_status: dict = {}
    violations = [
        RiskViolation(rule="max_position_pct", message="x", value=1.0, limit=2.0),
    ]
    _apply_sector_unresolved_alert(data_status, violations)
    assert "sector" not in data_status


# ===========================================================================
# 3. A held position with an unresolvable sector still counts conservatively
#    toward exposure rather than vanishing.
# ===========================================================================

def test_held_unresolved_sector_position_counts_not_vanishes():
    """A book that is 35% "Unknown" (e.g. a held position whose sector never
    resolved) must not let a new "Unknown"-sector BUY through as if that
    35% weren't there."""
    eng = _engine(max_sector_pct=40.0, max_sector_hard_pct=95.0)
    positions = [
        _held("HELD1", 35_000.0, sector="Unknown"),
    ]  # $35,000 of $100,000 = 35%, stamped Unknown by a prior failed lookup
    decision = _buy("NEWSYM", allocation_pct=10.0)  # would push pool to 45%

    with patch("src.execution.broker._get_sector", return_value="Unknown"):
        violations = eng.check(
            decision=decision, positions=positions,
            total_value=100_000.0, daily_pnl=0.0, cash=100_000.0,
        )

    soft = next((v for v in violations if v.rule == "max_sector_pct"), None)
    assert soft is not None, "the held Unknown position must not have vanished from exposure"
    assert soft.value == pytest.approx(45.0), (
        f"expected the held 35% + new 10% pooled together, got {soft.value}"
    )


# ===========================================================================
# 4. A transient lookup failure and a genuinely sector-less instrument are
#    distinguishable.
# ===========================================================================

def test_lookup_exception_is_classified_as_lookup_failed():
    from src.execution.broker import _get_sector, _sector_cache, _sector_resolution_status_for

    _sector_cache.clear()
    with patch("src.execution.broker.yf.Ticker", side_effect=RuntimeError("rate-limited")):
        assert _get_sector("XYZFAIL") == "Unknown"
    assert _sector_resolution_status_for("XYZFAIL") == "lookup_failed"


def test_successful_fetch_with_no_sector_field_is_classified_as_no_sector():
    from src.execution.broker import _get_sector, _sector_cache, _sector_resolution_status_for

    _sector_cache.clear()
    with patch("src.execution.broker.yf.Ticker") as mock_ticker:
        # A real, non-empty response with data present, just no `sector` key —
        # e.g. an ETF outside the static table. NOT an error, NOT a timeout.
        mock_ticker.return_value.info = {"longName": "Some Fund", "quoteType": "ETF"}
        assert _get_sector("XYZETF") == "Unknown"
    assert _sector_resolution_status_for("XYZETF") == "no_sector"


def test_the_two_failure_modes_produce_different_risk_violations():
    """End-to-end: the risk engine's advisory rule name and message text
    must differ between the two conditions, not read the same."""
    from src.execution.broker import _sector_cache

    eng = _engine(max_sector_pct=90.0, max_sector_hard_pct=95.0)

    _sector_cache.clear()
    with patch("src.execution.broker.yf.Ticker", side_effect=RuntimeError("boom")):
        violations_failed = eng.check(
            decision=_buy("TRANSIENT1", allocation_pct=5.0), positions=[],
            total_value=100_000.0, daily_pnl=0.0, cash=100_000.0,
        )

    _sector_cache.clear()
    with patch("src.execution.broker.yf.Ticker") as mock_ticker:
        mock_ticker.return_value.info = {"longName": "Some Fund"}
        violations_no_sector = eng.check(
            decision=_buy("NOSECTOR1", allocation_pct=5.0), positions=[],
            total_value=100_000.0, daily_pnl=0.0, cash=100_000.0,
        )

    rule_failed = next(v.rule for v in violations_failed if v.rule.startswith("sector_unresolved"))
    rule_no_sector = next(v.rule for v in violations_no_sector if v.rule.startswith("sector_unresolved"))

    assert rule_failed == "sector_unresolved_lookup_failed"
    assert rule_no_sector == "sector_unresolved_no_sector"
    assert rule_failed != rule_no_sector

    msg_failed = next(v.message for v in violations_failed if v.rule.startswith("sector_unresolved"))
    msg_no_sector = next(v.message for v in violations_no_sector if v.rule.startswith("sector_unresolved"))
    assert msg_failed != msg_no_sector


# ===========================================================================
# 5. Normal resolution is completely unchanged (the regression that matters
#    most).
# ===========================================================================

def test_normal_resolution_unaffected_no_extra_advisory():
    """A cleanly resolved sector must behave exactly as before: the soft
    target fires only when the REAL sector breaches it, and no
    sector_unresolved_* advisory is manufactured for a symbol that resolved
    fine."""
    eng = _engine(max_sector_pct=40.0, max_sector_hard_pct=95.0)
    positions = [
        _held("AAPL", 1_900.0, sector="Technology"),
        _held("MSFT", 2_050.0, sector="Technology"),
    ]
    decision = _buy("NVDA", allocation_pct=15.0, entry=850.0)

    with patch("src.execution.broker._get_sector", return_value="Technology"):
        violations = eng.check(
            decision=decision, positions=positions,
            total_value=10_000.0, daily_pnl=0.0,
        )

    assert any(v.rule == "max_sector_pct" for v in violations)
    assert not any(v.rule.startswith("sector_unresolved") for v in violations), (
        "a resolved sector must never raise the unresolved-sector advisory"
    )


def test_normal_resolution_under_cap_produces_zero_violations():
    eng = _engine(max_sector_pct=90.0, max_sector_hard_pct=95.0)
    decision = _buy("AAPL", allocation_pct=5.0)

    with patch("src.execution.broker._get_sector", return_value="Technology"):
        violations = eng.check(
            decision=decision, positions=[],
            total_value=100_000.0, daily_pnl=0.0, cash=100_000.0,
        )

    assert violations == []


# ===========================================================================
# Batch accumulation: TradingPipeline._filter_hard_risk_decisions must pool
# "Unknown" across decisions in the same run via `accumulate_pending_sector`,
# not just within one check().
# ===========================================================================

def test_pending_sector_investment_pools_unknown_across_the_batch():
    from src.pipeline import TradingPipeline

    pipeline = TradingPipeline.__new__(TradingPipeline)
    pipeline.risk_engine = _engine(max_sector_pct=10.0, max_sector_hard_pct=15.0)
    decisions = [
        _buy("FIRST", allocation_pct=8.0),
        _buy("SECOND", allocation_pct=8.0),
    ]

    with patch("src.pipeline._get_sector", return_value="Unknown"), patch(
        "src.execution.broker._get_sector", return_value="Unknown"
    ):
        allowed, violations, blocked = pipeline._filter_hard_risk_decisions(
            decisions, positions=[], total_value=100_000, daily_pnl=0,
        )

    # FIRST alone (8%, prior 0%) is under the 15% hard ceiling's allowance
    # and passes; SECOND then sees FIRST's 8% already pooled under
    # ("Unknown", "long") — prior=8%, allowance=15-8=7%, but SECOND asks
    # for 8% > 7% -> hard-blocked. Pre-fix, `accumulate_pending_sector`
    # excluded "Unknown" entirely, so SECOND would never have seen FIRST's
    # contribution and both would have been allowed.
    assert len(allowed) == 1 and allowed[0].symbol == "FIRST"
    assert blocked, "SECOND should have been hard-blocked by the pooled Unknown bucket"
    assert any("SECOND" in b for b in blocked)
