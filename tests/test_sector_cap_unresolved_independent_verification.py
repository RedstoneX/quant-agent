"""Independent verification of the 2026-09-01 sector-cap-unresolved fix
(see tests/test_sector_cap_unresolved.py and docs/INCIDENT_HISTORY.md for
the interrupted agent's own account of the defect and fix).

Deliberately does NOT import the fixtures from test_sector_cap_unresolved.py
— these are separately authored to avoid rubber-stamping a bug shared
between the fix and its own tests.

Questions 1 and 3 asked about the 75%/90% sector ramp, deleted 2026-10-09;
the two below remain:

  2. Is a SMALL, isolated unresolved-sector order (nowhere near the
     ceiling) refused, or only warned? ("fails loudly" is ambiguous between
     "always refused" and "visible, and refused only past the same ceiling
     a real sector would be" — this pins down which one shipped.)

  4. Are instruments that legitimately have no single sector (broad-market
     index ETFs) swept into the "Unknown" bucket and its advisory, or
     correctly kept out of it via the pre-existing deterministic
     `_INDEX_ETFS` table?
"""

from unittest.mock import patch

from src.config import RiskConfig
from src.models import Position, TradeDecision
from src.risk.rules import HARD_BLOCK_RULES
from src.risk.rules import RiskRuleEngine

EQUITY = 100_000.0


def _engine(**overrides) -> RiskRuleEngine:
    kwargs = dict(
        max_position_pct=99.0,
        max_total_position_pct=300.0,
        require_stop_loss=True,
    )
    kwargs.update(overrides)
    return RiskRuleEngine(RiskConfig(**kwargs))


def _held(symbol: str, market_value: float, sector: str, *, short: bool = False) -> Position:
    qty = market_value / 100.0
    return Position(
        symbol=symbol,
        qty=-qty if short else qty,
        avg_entry=100.0,
        current_price=100.0,
        market_value=market_value,
        unrealized_pnl=0.0,
        sector=sector,
    )


def _order(symbol: str, allocation_pct: float, action: str = "BUY") -> TradeDecision:
    return TradeDecision(
        action=action,
        symbol=symbol,
        allocation_pct=allocation_pct,
        entry_price=100.0,
        stop_loss=95.0 if action == "BUY" else 105.0,
        take_profit=140.0 if action == "BUY" else 60.0,
        reasoning="t",
    )


# ---------------------------------------------------------------------------
# 2. A small, isolated unresolved order: warned, NOT refused. Documenting
#    this precisely, because "fails loudly" could otherwise be misread as
#    "every unresolved symbol is refused outright."
# ---------------------------------------------------------------------------


def test_small_isolated_unresolved_order_is_advisory_only_not_refused():
    decision = _order("NEWSYM", allocation_pct=5.0)

    with patch("src.execution.broker._get_sector", return_value="Unknown"):
        violations = _engine().check(
            decision=decision,
            positions=[],
            total_value=EQUITY,
            cash=EQUITY,
        )

    rules = [v.rule for v in violations]
    hard = [v for v in violations if v.rule in HARD_BLOCK_RULES]
    assert not hard, (
        f"a lone 5% unresolved-sector order with no other Unknown exposure "
        f"must NOT be refused (same treatment a real sector at 5% would "
        f"get); got {rules}"
    )
    assert any(r.startswith("sector_unresolved") for r in rules), (
        f"but it must still be visible — expected a sector_unresolved_* advisory; got {rules}"
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
        positions=[],
        total_value=EQUITY,
        cash=EQUITY,
    )
    rules = [v.rule for v in violations]
    assert not any(r.startswith("sector_unresolved") for r in rules), (
        f"SPY has a legitimate deterministic 'Broad' sector and must not "
        f"raise the unresolved-sector advisory; got {rules}"
    )
    assert not [v for v in violations if v.rule in HARD_BLOCK_RULES]
