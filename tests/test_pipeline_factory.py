"""The factory in tests/pipeline_factory.py builds the real thing, wired."""

from unittest.mock import MagicMock

from src.pipeline import TradingPipeline
from src.storage.db import Database
from tests.pipeline_factory import CONSTRUCTOR_SITES, build_pipeline


def test_real_init_runs_and_wires_every_service():
    p = build_pipeline()
    assert type(p) is TradingPipeline
    for attr in CONSTRUCTOR_SITES:
        assert getattr(p, attr) is not None, attr
    assert p.cost_circuit is None  # conftest's unmetered flag, not a network call
    assert p.risk_gate.db is p.db and isinstance(p.db, Database)


def test_stand_ins_reach_constructor_wired_services():
    db, broker = MagicMock(name="db"), MagicMock(name="broker")
    p = build_pipeline(db=db, broker=broker, _atr_for_symbol=lambda s: 2.5)
    assert p.db is db and p.risk_gate.db is db  # not bolted on after
    assert p.broker is broker
    assert p._atr_for_symbol("SPY") == 2.5


def test_every_constructor_site_name_exists_in_pipeline_module():
    import src.pipeline as m

    missing = [s for s in CONSTRUCTOR_SITES.values() if not hasattr(m, s)]
    assert not missing, missing
