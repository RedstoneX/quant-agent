"""Boundary test for `AdmissionService` (conversion step 8).

Clause 5 of `docs/ARCHITECTURE.md` section 3: the service is built with explicit
stand-ins and the in-memory journal, and this file never imports
`TradingPipeline`.
"""
import json
from types import SimpleNamespace
from unittest.mock import MagicMock

from src.config import UniverseScreenConfig
from src.models import OHLCV, TradeDecision
from src.pipeline_admission import AdmissionService
from tests.fake_event_journal import InMemoryEventJournal


def _bars(n=300, price=50.0):
    """n weekday bars ending on the 2026-09-18 Friday the screen tests use."""
    from datetime import date, timedelta

    days, day = [], date(2026, 9, 18)
    while len(days) < n:
        if day.weekday() < 5:
            days.append(day)
        day -= timedelta(days=1)
    days.reverse()
    return [
        OHLCV(date=d, open=price, high=price * 1.0005, low=price * 0.9995,
              close=price, volume=1_000_000)
        for d in days
    ]


ASSET = {
    "symbol": "ACME", "name": "Acme Corp. Common Stock", "status": "active",
    "tradable": True, "class": "us_equity", "exchange": "NYSE",
    "shortable": True, "borrow_status": "easy_to_borrow", "easy_to_borrow": True,
}


def _config(tmp_path, *, enabled):
    return SimpleNamespace(
        trading=SimpleNamespace(universe=["SPY"], lookback_days=120),
        smart_money=SimpleNamespace(
            max_external_candidates=3, min_external_history_days=20,
            min_external_price_usd=5.0, min_external_avg_dollar_volume_usd=10_000_000,
            request_timeout_s=15,
        ),
        universe_screen=UniverseScreenConfig(enabled=enabled, data_dir=str(tmp_path)),
        execution=SimpleNamespace(max_entry_slippage_bps=40.0),
        risk=SimpleNamespace(min_stop_atr_multiple=2.5, max_target_horizon_sessions=60),
        nominations=SimpleNamespace(max_per_seat_per_run=3),
    )


def _service(tmp_path, *, enabled, journal=None):
    broker, market = MagicMock(), MagicMock()
    broker.get_transient_equity_eligibility.return_value = {"eligible": True, "reason": "eligible"}
    broker.get_asset_record.return_value = ASSET
    broker.list_assets.return_value = [ASSET]
    broker.get_positions.return_value = []
    market.get_ohlcv.return_value = _bars(300)
    market.get_ohlcv_batch.side_effect = lambda chunk, days: {s: _bars(300) for s in chunk}
    market.get_company_profile.return_value = {
        "market_cap_usd": 5e9, "sector_raw": "Industrials", "quote_type": "EQUITY"}
    provider = MagicMock()
    provider.recent_filings.return_value = []
    provider.listed_map.return_value = {}
    journal = journal if journal is not None else InMemoryEventJournal()
    return AdmissionService(
        config=_config(tmp_path, enabled=enabled), broker=broker, market=market,
        journal=journal, sec_form4_provider=provider,
    ), journal


def test_filter_blocks_a_buy_outside_the_universe_and_allows_an_admitted_one(tmp_path):
    svc, _ = _service(tmp_path, enabled=False)
    buys = [TradeDecision(symbol=s, action="BUY", allocation_pct=5.0, entry_price=10.0,
                  stop_loss=9.0, take_profit=12.0, reasoning="r")
            for s in ("SPY", "ACME", "ZZZZ")]
    allowed, blocked = svc._filter_supported_symbols(
        buys, [SimpleNamespace(symbol=s) for s in ("SPY", "ACME", "ZZZZ")], [], {"acme"},
    )
    assert [d.symbol for d in allowed] == ["SPY", "ACME"]
    assert len(blocked) == 1 and "ZZZZ" in blocked[0]


def test_old_gate_admits_a_good_name_and_refuses_a_cheap_one(tmp_path, monkeypatch):
    monkeypatch.setattr("src.pipeline_admission._get_sector", lambda s: "Industrials")
    svc, _ = _service(tmp_path, enabled=False)
    ok, reason, details = svc._evaluate_external_admission_gates("ACME")
    assert ok is True and details["sector"] == "Industrials"
    svc.market.get_ohlcv.return_value = _bars(300, price=1.0)
    ok, reason, _ = svc._evaluate_external_admission_gates("ACME")
    assert (ok, reason) == (False, "price_below_minimum")


def test_universe_screen_writes_its_evidence_to_the_injected_journal(tmp_path, monkeypatch):
    monkeypatch.setattr("src.pipeline_admission._get_sector", lambda s: "Industrials")
    svc, journal = _service(tmp_path, enabled=True)
    summary = svc._run_universe_screen("evening-x")
    assert summary["passed"] == 1
    kinds = [r["kind"] for r in journal.rows]
    assert kinds.count("universe_change") == 1 and "universe_screen_run" in kinds
    run_row = journal.events(kind="universe_screen_run")[0]
    assert run_row["scope"] == "run" and run_row["agent_name"] == "universe_screen"
    assert "events" not in json.loads(run_row["evidence_json"])


def test_a_failing_journal_never_interrupts_the_screen(tmp_path, monkeypatch):
    monkeypatch.setattr("src.pipeline_admission._get_sector", lambda s: "Industrials")
    svc, journal = _service(tmp_path, enabled=True, journal=InMemoryEventJournal(fail=True))
    summary = svc._run_universe_screen("evening-x")
    assert summary["passed"] == 1 and journal.rows == [] and len(journal.failures) == 2


class TestBuiltWithoutAPipeline:
    """Step 8b: the builder takes values, so the service can reach nothing else.

    `TradingPipeline` is never imported in this file; these build the very
    object the pipeline builds, from fakes, and exercise it.
    """

    def test_the_builder_makes_a_working_service_from_plain_collaborators(self, tmp_path):
        from src.pipeline_admission_build import build_admission_service

        broker, market = MagicMock(), MagicMock()
        broker.get_asset_record.return_value = ASSET
        service = build_admission_service(
            config=_config(tmp_path, enabled=False), broker=broker, market=market, db=None,
        )
        buys = [TradeDecision(symbol=s, action="BUY", allocation_pct=5.0, entry_price=10.0,
                              stop_loss=9.0, take_profit=12.0, reasoning="r")
                for s in ("SPY", "ZZZZ")]
        allowed, blocked = service._filter_supported_symbols(
            buys, [SimpleNamespace(symbol=s) for s in ("SPY", "ZZZZ")], [],
        )
        assert [d.symbol for d in allowed] == ["SPY"] and len(blocked) == 1

    def test_the_service_holds_no_object_that_can_reach_a_pipeline(self, tmp_path):
        from src.pipeline_admission_build import build_admission_service

        constructor = SimpleNamespace(cfg=SimpleNamespace(min_stop_atr_multiple=4.0))
        config, broker, market = _config(tmp_path, enabled=False), MagicMock(), MagicMock()
        service = build_admission_service(
            config=config, broker=broker, market=market, db=None,
            portfolio_constructor=constructor,
        )
        # The live constructor was read by VALUE at build time, not through a host.
        assert service._constructor_cfg_or_none() is constructor.cfg
        # Every collaborator is the object handed in -- nothing was wrapped in a
        # shell that could reach back somewhere else.
        assert (service.config, service.broker, service.market) == (config, broker, market)
        assert service.sec_form4_provider is None and service.journal.db is None
        import inspect

        taken = set(inspect.signature(build_admission_service).parameters)
        assert taken == {"config", "broker", "market", "db", "sec_form4_provider",
                         "portfolio_constructor"}
