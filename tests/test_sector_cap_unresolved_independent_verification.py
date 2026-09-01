"""Independent verification of the 2026-09-01 sector-cap-unresolved fix
(see tests/test_sector_cap_unresolved.py and docs/INCIDENT_HISTORY.md for
the interrupted agent's own account of the defect and fix).

Deliberately does NOT import the fixtures from test_sector_cap_unresolved.py
— these are separately authored to avoid rubber-stamping a bug shared
between the fix and its own tests.

Four questions this file exists to answer, none of which the original test
file settles on its own:

  1. Under the REAL production config (config/settings.yaml: max_sector_pct
     75, max_sector_hard_pct 90 — not the test file's own 40/60 sandbox
     numbers), does a pooled-Unknown book that would breach the hard
     ceiling actually get hard-blocked?

  2. Is a SMALL, isolated unresolved-sector order (nowhere near the
     ceiling) refused, or only warned? ("fails loudly" is ambiguous between
     "always refused" and "visible, and refused only past the same ceiling
     a real sector would be" — this pins down which one shipped.)

  3. Does the fix respect the same-day long/short sector-budget split
     (spec 12.2)? An unresolved LONG and an unresolved SHORT must not share
     one pooled budget any more than two real-sector positions would.

  4. Are instruments that legitimately have no single sector (broad-market
     index ETFs) swept into the "Unknown" bucket and its advisory, or
     correctly kept out of it via the pre-existing deterministic
     `_INDEX_ETFS` table?
"""
from unittest.mock import patch

from src.config import RiskConfig
from src.models import Position, TradeDecision
from src.pipeline import HARD_BLOCK_RULES
from src.risk.rules import RiskRuleEngine, sector_allowance_pct

EQUITY = 100_000.0
# Real production values (config/settings.yaml), not a test sandbox number.
PROD_SOFT = 75.0
PROD_HARD = 90.0


def _engine(**overrides) -> RiskRuleEngine:
    kwargs = dict(
        max_position_pct=99.0, max_total_position_pct=300.0,
        max_daily_loss_pct=99.0, max_sector_pct=PROD_SOFT,
        max_sector_hard_pct=PROD_HARD, require_stop_loss=True,
    )
    kwargs.update(overrides)
    return RiskRuleEngine(RiskConfig(**kwargs))


def _held(symbol: str, market_value: float, sector: str, *, short: bool = False) -> Position:
    qty = market_value / 100.0
    return Position(
        symbol=symbol, qty=-qty if short else qty, avg_entry=100.0,
        current_price=100.0, market_value=market_value,
        unrealized_pnl=0.0, sector=sector,
    )


def _order(symbol: str, allocation_pct: float, action: str = "BUY") -> TradeDecision:
    return TradeDecision(
        action=action, symbol=symbol, allocation_pct=allocation_pct,
        entry_price=100.0, stop_loss=95.0 if action == "BUY" else 105.0,
        take_profit=140.0 if action == "BUY" else 60.0, reasoning="t",
    )


# ---------------------------------------------------------------------------
# 1. Real 75/90 production numbers: pooled Unknown past the ceiling refuses.
# ---------------------------------------------------------------------------

def test_prod_caps_pooled_unknown_past_hard_ceiling_is_refused():
    positions = [_held("HELD1", EQUITY * 0.85, sector="Unknown")]  # 85% held
    decision = _order("NEWSYM", allocation_pct=10.0)  # -> 95% pooled

    with patch("src.execution.broker._get_sector", return_value="Unknown"):
        violations = _engine().check(
            decision=decision, positions=positions,
            total_value=EQUITY, daily_pnl=0.0, cash=EQUITY,
        )

    hard = [v for v in violations if v.rule in HARD_BLOCK_RULES]
    assert [v.rule for v in hard] == ["max_sector_hard_pct"], (
        f"85% held + 10% new unresolved-sector order should refuse under "
        f"the real 90% hard ceiling; got hard={hard}, all={[v.rule for v in violations]}"
    )
    # Cross-check against the same allowance arithmetic a real sector uses,
    # rather than trusting the rule fired for the right reason.
    allowance = sector_allowance_pct(85.0, soft_cap_pct=PROD_SOFT, hard_cap_pct=PROD_HARD)
    assert allowance < 10.0, "sanity: allowance must be under what was requested"


def test_prod_caps_resolved_sector_at_the_same_size_also_refused_control():
    """Control: a REAL sector at the same crowding level refuses identically
    — proves the Unknown pool isn't being held to a different standard."""
    positions = [_held("HELD1", EQUITY * 0.85, sector="Healthcare")]
    decision = _order("NEWSYM", allocation_pct=10.0)

    with patch("src.execution.broker._get_sector", return_value="Healthcare"):
        violations = _engine().check(
            decision=decision, positions=positions,
            total_value=EQUITY, daily_pnl=0.0, cash=EQUITY,
        )
    hard = [v for v in violations if v.rule in HARD_BLOCK_RULES]
    assert [v.rule for v in hard] == ["max_sector_hard_pct"]


# ---------------------------------------------------------------------------
# 2. A small, isolated unresolved order: warned, NOT refused. Documenting
#    this precisely, because "fails loudly" could otherwise be misread as
#    "every unresolved symbol is refused outright."
# ---------------------------------------------------------------------------

def test_small_isolated_unresolved_order_is_advisory_only_not_refused():
    decision = _order("NEWSYM", allocation_pct=5.0)  # nowhere near 75/90

    with patch("src.execution.broker._get_sector", return_value="Unknown"):
        violations = _engine().check(
            decision=decision, positions=[],
            total_value=EQUITY, daily_pnl=0.0, cash=EQUITY,
        )

    rules = [v.rule for v in violations]
    hard = [v for v in violations if v.rule in HARD_BLOCK_RULES]
    assert not hard, (
        f"a lone 5% unresolved-sector order with no other Unknown exposure "
        f"must NOT be refused (same treatment a real sector at 5% would "
        f"get); got {rules}"
    )
    assert any(r.startswith("sector_unresolved") for r in rules), (
        f"but it must still be visible — expected a sector_unresolved_* "
        f"advisory; got {rules}"
    )


# ---------------------------------------------------------------------------
# 3. Long/short split (spec 12.2) applies to the Unknown pool too.
# ---------------------------------------------------------------------------

def test_unknown_long_and_short_pools_are_independent():
    # 85% of equity held LONG in an unresolved sector.
    positions = [_held("HELD1", EQUITY * 0.85, sector="Unknown", short=False)]

    with patch("src.execution.broker._get_sector", return_value="Unknown"):
        # A new SHORT with an unresolved sector must be judged against the
        # (empty) SHORT Unknown pool, not the 85%-full LONG one.
        short_violations = _engine().check(
            decision=_order("SHORTME", allocation_pct=10.0, action="SHORT"),
            positions=positions, total_value=EQUITY, daily_pnl=0.0, cash=EQUITY,
        )
        # A new LONG, on the other hand, stacks directly on the 85% LONG pool.
        long_violations = _engine().check(
            decision=_order("LONGME", allocation_pct=10.0, action="BUY"),
            positions=positions, total_value=EQUITY, daily_pnl=0.0, cash=EQUITY,
        )

    short_hard = [v for v in short_violations if v.rule in HARD_BLOCK_RULES]
    long_hard = [v for v in long_violations if v.rule in HARD_BLOCK_RULES]
    assert not short_hard, (
        f"a SHORT into an unresolved sector must not be blocked by an "
        f"unrelated LONG-side Unknown pool (spec 12.2 side-split); got {short_hard}"
    )
    assert [v.rule for v in long_hard] == ["max_sector_hard_pct"], (
        f"a LONG into an unresolved sector must still stack on the "
        f"existing LONG Unknown pool and refuse past the ceiling; got {long_hard}"
    )


# ---------------------------------------------------------------------------
# 4. Broad-market index ETFs are NOT swept into "Unknown" — pre-existing
#    `_INDEX_ETFS` table (src/execution/broker.py) resolves them
#    deterministically to "Broad" before any network lookup, so they never
#    enter the unresolved-sector path this fix changes.
# ---------------------------------------------------------------------------

def test_broad_index_etf_is_not_treated_as_unresolved():
    from src.execution.broker import _get_sector, _sector_cache

    _sector_cache.clear()
    # No mock: SPY is in _INDEX_ETFS, so this must resolve deterministically
    # and offline, never touching the network.
    assert _get_sector("SPY") == "Broad"

    violations = _engine().check(
        decision=_order("SPY", allocation_pct=5.0),
        positions=[], total_value=EQUITY, daily_pnl=0.0, cash=EQUITY,
    )
    rules = [v.rule for v in violations]
    assert not any(r.startswith("sector_unresolved") for r in rules), (
        f"SPY has a legitimate deterministic 'Broad' sector and must not "
        f"raise the unresolved-sector advisory; got {rules}"
    )
    assert not [v for v in violations if v.rule in HARD_BLOCK_RULES]
