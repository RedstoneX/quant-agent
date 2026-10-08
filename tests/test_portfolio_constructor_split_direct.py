"""Direct witnesses for the modules lifted out of `src/portfolio_constructor/__init__.py`.

Each function is called with plain stand-ins and no constructor or pipeline behind it.
"""
from __future__ import annotations

from types import SimpleNamespace

from src.portfolio_constructor import refusal_log, sector_dial, target_derivation
from src.portfolio_constructor.config import ConstructorConfig


def _owner():
    return SimpleNamespace(
        last_data_faults={}, last_refusals={}, last_parity_standdowns={},
        refusal_recorder=None,
    )


def test_refusal_log_records_and_drains_in_the_owners_dicts():
    owner = _owner()
    refusal_log._note_refusal(owner, " abc ", "short", "R1", "because")
    assert owner.last_refusals["ABC"] == {"refusal": "R1", "detail": "because", "direction": "short"}
    refusal_log._note_refusal(owner, "abc", "long", "R2", "later", only_if_unrecorded=True)
    assert owner.last_refusals["ABC"]["refusal"] == "R1"
    assert refusal_log.drain_refusals(owner)["ABC"]["refusal"] == "R1"
    assert owner.last_refusals == {}
    refusal_log._note_data_fault(owner, "xyz", "long", "F", "no price")
    assert refusal_log.drain_data_faults(owner)["XYZ"]["fault"] == "F"
    assert owner.last_data_faults == {}


def test_refusal_log_parity_standdown_without_a_level():
    owner = SimpleNamespace(PARITY_STANDDOWN_NO_LEVEL="none", last_parity_standdowns={})
    derivation = SimpleNamespace(level_used=None)
    assert refusal_log._parity_verdict(owner, 10.0, 9.0, derivation, False) == (False, None, "none")
    refusal_log._note_parity_standdown(owner, "q", "long", "none", derivation)
    assert owner.last_parity_standdowns["Q"]["reason"] == "none"


def test_sector_dial_weights_and_accrual(monkeypatch):
    assert sector_dial._current_weights([], 100.0) == {}
    monkeypatch.setattr("src.sector_reference._get_sector", lambda sym: "Tech")
    seen, weights = {}, {}
    decision = SimpleNamespace(action="BUY", symbol="AAA", allocation_pct=5.0)
    sector_dial._accrue_sector(seen, weights, decision)
    assert seen == {"AAA": "Tech"}
    assert sum(weights.values()) >= 5.0
    sector_dial._accrue_sector(seen, weights, SimpleNamespace(action="HOLD", symbol="B", allocation_pct=1.0))
    assert "B" not in seen


def test_sector_dial_leaves_an_unresolved_sector_alone(monkeypatch):
    monkeypatch.setattr("src.sector_reference._get_sector", lambda sym: "Unknown")
    out = sector_dial._apply_sector_dial(
        ConstructorConfig(), lambda *a, **k: None, "ZZZ", 4.0,
        sector_weights={}, total_value=1000.0,
    )
    assert out == (4.0, "")


def test_target_derivation_without_analysis_is_a_named_data_fault():
    faults = []
    derivation = target_derivation._derive_target(
        ConstructorConfig(), lambda *a: faults.append(a), lambda *a: None,
        "AAA", None, 10.0, "long",
    )
    assert derivation.price is None and derivation.fault
    assert faults and faults[0][0] == "AAA"


def test_target_note_is_empty_without_a_price():
    assert target_derivation._target_note(SimpleNamespace(price=None)) == ""
