"""The parity refusal must leave a DURABLE, NUMERIC record (board item 218).

The owner ruled parity a trial — "for now ... see if that improves the desk
purchases" — and a trial whose only record is an English sentence in an
in-memory dict cannot be judged. These pin that every refusal lands in the
`trade_refusals` table with each quantity in its own column, and that the
three stand-downs (a reward figure the code itself does not believe) are
recorded as stand-downs rather than silently skipped.
"""

import tempfile
from pathlib import Path

import pytest

from src.portfolio_constructor import (
    STOP_REFUSAL_REWARD_BELOW_RISK, ConstructorConfig, PortfolioConstructor,
)
from src.storage.db import Database


@pytest.fixture()
def db():
    with tempfile.TemporaryDirectory() as d:
        _db = Database(str(Path(d) / "t.db"))
        _db.initialize()
        yield _db


def test_the_refusals_table_stores_numbers_in_their_own_columns(db):
    db.insert_trade_refusal(
        symbol="aaa", direction="long", refusal=STOP_REFUSAL_REWARD_BELOW_RISK,
        entry_price=100.0, stop_price=94.0, level_used=104.0,
        reward_risk=0.67, threshold=1.0, level_was_measured=True,
        stage="construction",
    )
    rows = db.get_trade_refusals(refusal=STOP_REFUSAL_REWARD_BELOW_RISK)
    assert len(rows) == 1
    r = rows[0]
    # The owner's own worked case, as DATA. Not one of these is prose.
    assert r["symbol"] == "AAA"
    assert r["entry_price"] == 100.0
    assert r["stop_price"] == 94.0
    assert r["level_used"] == 104.0
    assert r["reward_risk"] == 0.67
    assert r["threshold"] == 1.0
    assert r["level_was_measured"] == 1
    assert r["stage"] == "construction"
    assert r["timestamp"]


def test_a_standdown_is_recorded_and_is_not_a_refusal():
    """A level PAST the horizon reach is a number the derivation has already
    declared unreachable, so the gate declines to judge — and says so."""
    from tests.test_stop_width_gate import _analysis, _orders
    constructor = PortfolioConstructor()
    a = _analysis("WIDE", entry=100.0, stop=80.0, levels=[80.0, 140.0])
    decisions = _orders(constructor, a, risk_pct=1.0)
    assert [d.action for d in decisions] == ["BUY"]
    assert constructor.last_refusals == {}
    assert constructor.last_parity_standdowns["WIDE"]["reason"] == (
        PortfolioConstructor.PARITY_STANDDOWN_LEVEL_PAST_REACH
    )
