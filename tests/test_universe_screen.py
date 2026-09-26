"""Universe expansion and pruning (src/universe_screen.py).

One test (or small group) per criterion, the prune state machine, the
weekly incremental runner, the per-run cap, the side-door routing (Form 4
and nominations run the same screen when it is on, and their old gates when
it is off), the restored Form 4 age gate, and the morning message.
"""
from __future__ import annotations

import json
import math
import time
from datetime import date, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src import universe_screen as us
from src.config import UniverseScreenConfig
from src.models import OHLCV
from src.pipeline import TradingPipeline

TODAY = date(2026, 9, 18)  # a Friday


def _bars(n=300, *, price=50.0, rng=0.01, end=TODAY, close_drift=0.0):
    """n weekday bars ending at `end`, high/low = close*(1 +/- rng)."""
    out = []
    day = end
    while len(out) < n:
        if day.weekday() < 5:
            out.append(day)
        day -= timedelta(days=1)
    out.reverse()
    bars = []
    for i, d in enumerate(out):
        close = price * (1 + close_drift * i)
        bars.append(OHLCV(date=d, open=close, high=close * (1 + rng),
                          low=close * (1 - rng), close=close, volume=1_000_000))
    return bars


TH = us.ScreenThresholds(
    min_price_usd=5.0, min_market_cap_usd=30_000_000,
    max_half_spread_bps=40.0, max_atr_fraction=0.5 / 2.5, min_history_bars=210,
)

GOOD_ASSET = {
    "symbol": "ACME", "name": "Acme Corp. Common Stock", "status": "active",
    "tradable": True, "class": "us_equity", "exchange": "NYSE",
    "shortable": True, "borrow_status": "easy_to_borrow", "easy_to_borrow": True,
}


def _good_bars():
    # A calm stock: zero intraday range, tiny drift -> spread estimate 0.
    return _bars(300, price=50.0, rng=0.0005)


def _sources(asset=GOOD_ASSET, bars=None, profile=None, filings=()):
    return us.ScreenSources(
        get_asset=lambda s: asset,
        get_bars=lambda s: _good_bars() if bars is None else bars,
        get_profile=lambda s: profile if profile is not None else {
            "market_cap_usd": 5e9, "sector": "Industrials"},
        get_filings=lambda s: list(filings) if filings is not None else None,
    )


def test_a_clean_stock_passes_every_check():
    result = us.screen_symbol("ACME", _sources(), TH)
    assert result.passed, result.failures
    assert result.measured["last_price"] == pytest.approx(50.0)


# ---------------------------------------------------------------- asset ----

@pytest.mark.parametrize("asset,code", [
    (None, "asset_not_found"),
    ({**GOOD_ASSET, "status": "inactive"}, "asset_inactive"),
    ({**GOOD_ASSET, "tradable": False}, "asset_not_tradable"),
])
def test_delisted_or_halted_is_a_permanent_failure(asset, code):
    result = us.screen_symbol("ACME", _sources(asset=asset), TH)
    assert result.failures == [code]
    assert result.permanent


def test_otc_is_refused():
    result = us.screen_symbol("ACME", _sources(asset={**GOOD_ASSET, "exchange": "OTC"}), TH)
    assert "unsupported_exchange" in result.failures


@pytest.mark.parametrize("symbol,name", [
    ("ACMEW", "Acme Acquisition Corp. Warrant"),
    ("ACMEU", "Acme Acquisition Corp. Units"),
    ("ACMER", "Acme Acquisition Corp. Rights"),
    ("ACME.WS", "Acme Corp"),
    ("ACME.U", "Acme Corp"),
    ("ACME.RT", "Acme Corp"),
])
def test_warrants_units_and_rights_are_refused(symbol, name):
    asset = {**GOOD_ASSET, "symbol": symbol, "name": name}
    assert us.check_asset(symbol, asset) == ["not_common_stock"]


def test_a_company_called_united_is_not_mistaken_for_a_unit():
    asset = {**GOOD_ASSET, "symbol": "FUNC", "name": "First United Corporation Common Stock"}
    assert us.check_asset("FUNC", asset) == []


# --------------------------------------------------------------- borrow ----

def test_not_shortable_is_refused():
    result = us.screen_symbol("ACME", _sources(asset={**GOOD_ASSET, "shortable": False}), TH)
    assert result.failures == ["not_shortable"]


def test_hard_to_borrow_is_refused_from_borrow_status():
    asset = {**GOOD_ASSET, "borrow_status": "hard_to_borrow", "easy_to_borrow": True}
    assert us.check_borrow(asset) == ["hard_to_borrow"]


def test_deprecated_easy_to_borrow_flag_is_the_fallback():
    asset = {k: v for k, v in GOOD_ASSET.items() if k != "borrow_status"}
    assert us.check_borrow(asset) == []
    assert us.check_borrow({**asset, "easy_to_borrow": False}) == ["hard_to_borrow"]


# -------------------------------------------------------------- history ----

def test_less_than_a_calendar_year_of_bars_is_refused():
    # 240 weekday bars span under a calendar year.
    failures, _ = us.check_bars(_bars(240, rng=0.0005), TH)
    assert "insufficient_history" in failures


def test_a_year_of_bars_with_gaps_below_the_indicator_window_is_refused():
    bars = _bars(300, rng=0.0005)[::2]  # a full year, but only ~150 bars
    failures, _ = us.check_bars(bars, TH)
    assert "insufficient_history" in failures


def test_no_bars_at_all():
    assert us.check_bars([], TH)[0] == ["no_price_history"]


# ---------------------------------------------------------------- price ----

def test_penny_stock_is_refused():
    failures, _ = us.check_bars(_bars(300, price=4.99, rng=0.0005), TH)
    assert failures == ["price_below_minimum"]


def test_no_maximum_price():
    failures, _ = us.check_bars(_bars(300, price=4_000.0, rng=0.0005), TH)
    assert failures == []


# --------------------------------------------------------------- spread ----

def test_corwin_schultz_recovers_a_known_constant_spread():
    """No price movement at all: every day trades only at bid and ask, so
    each day's high/low ratio IS the spread. The estimator must return it."""
    spread = 0.02
    mid = 100.0
    bars = [
        OHLCV(date=TODAY - timedelta(days=10 - i), open=mid, high=mid * (1 + spread / 2),
              low=mid * (1 - spread / 2), close=mid, volume=1)
        for i in range(10)
    ]
    estimate = us.corwin_schultz_spread(bars)
    # ln(H/L) for a 2% quoted spread; the estimator's own closed form.
    assert estimate == pytest.approx(spread, rel=0.02)


def test_negative_daily_estimates_offset_positive_ones_before_the_floor():
    """Average first, floor the average — not floor each day. Flooring each
    day turns volatility into a phantom spread (measured: AAPL 25.6 bps)."""
    const = 3 - 2 * math.sqrt(2)
    # Two pairs engineered to give +x and -x alphas cannot be built from
    # bars directly; check the property on the live formula instead: a
    # zero-spread random walk must read near zero, not at a positive floor.
    import random
    rng = random.Random(7)
    price, bars = 100.0, []
    for i in range(260):
        o = price
        path = [o]
        for _ in range(40):
            path.append(path[-1] * math.exp(rng.gauss(0, 0.004)))
        price = path[-1]
        bars.append(OHLCV(date=TODAY - timedelta(days=400 - i), open=o, high=max(path),
                          low=min(path), close=price, volume=1))
    assert const > 0
    assert us.corwin_schultz_spread(bars) * 10_000 / 2 < 5


def test_negative_estimates_are_floored_at_zero():
    # A pure trend with no bounce produces negative alphas.
    bars = _bars(50, price=50, rng=0.0, close_drift=0.01)
    bars = [OHLCV(date=b.date, open=b.open, high=b.close * 1.02, low=b.close,
                  close=b.close, volume=1) for b in bars]
    assert us.corwin_schultz_spread(bars) >= 0.0


def test_wide_spread_is_refused_and_the_line_is_the_slippage_belt():
    # 1% quoted spread -> 50 bps half-spread > 40 bps belt.
    failures, measured = us.check_bars(_bars(300, rng=0.005), TH)
    assert "spread_too_wide" in failures
    assert measured["half_spread_bps"] > TH.max_half_spread_bps
    # Same bars, a belt wider than the half-spread: passes.
    wider = us.ScreenThresholds(**{**TH.__dict__, "max_half_spread_bps": 80.0})
    assert "spread_too_wide" not in us.check_bars(_bars(300, rng=0.005), wider)[0]


# ----------------------------------------------------------- volatility ----

def test_volatility_ceiling_refuses_a_name_whose_minimum_stop_breaks_the_sanity_floor():
    # 25% daily range -> ATR/price ~0.5 > 0.2 ceiling. The spread check
    # would also fire; the volatility one must be among the failures.
    failures, measured = us.check_bars(_bars(300, rng=0.25), TH)
    assert "volatility_above_ceiling" in failures
    assert measured["atr_pct"] > 20


# ---------------------------------------------------------- size/sector ----

def test_small_company_is_refused():
    result = us.screen_symbol("ACME", _sources(profile={"market_cap_usd": 29e6, "sector": "Industrials"}), TH)
    assert result.failures == ["company_too_small"]


def test_unknown_company_size_is_refused():
    result = us.screen_symbol("ACME", _sources(profile={"market_cap_usd": None, "sector": "Industrials"}), TH)
    assert result.failures == ["market_cap_unknown"]


def test_unresolved_sector_is_refused():
    result = us.screen_symbol("ACME", _sources(profile={"market_cap_usd": 1e9, "sector": "Unknown"}), TH)
    assert result.failures == ["unresolved_sector"]


def test_unreadable_profile_is_inconclusive_not_a_failure():
    sources = _sources()
    sources.get_profile = lambda s: None
    result = us.screen_symbol("ACME", sources, TH)
    assert result.inconclusive


# ------------------------------------------------------------- takeover ----

def test_pending_takeover_is_refused():
    filings = [("DEFM14A", "2026-08-01", ""), ("8-K", "2026-07-01", "1.01,9.01")]
    result = us.screen_symbol("ACME", _sources(filings=filings), TH)
    assert result.failures == ["pending_takeover"]


def test_a_terminated_deal_is_no_longer_pending():
    filings = [("SC 14D9/A", "2026-06-01", ""), ("8-K", "2026-07-15", "1.02")]
    assert us.check_takeover(filings) == ([], {})


def test_no_sec_issuer_means_takeover_status_unknown():
    result = us.screen_symbol("ACME", _sources(filings=None), TH)
    assert result.failures == ["takeover_status_unknown"]


def test_no_earnings_date_requirement_exists():
    # Nothing in the screen reads an earnings date; a clean stock with no
    # earnings information at all passes.
    assert "earnings" not in " ".join(us.PLAIN_REASON)
    assert us.screen_symbol("ACME", _sources(), TH).passed


# ----------------------------------------------------------- thresholds ----

def test_thresholds_are_read_from_existing_desk_numbers():
    config = SimpleNamespace(
        universe_screen=UniverseScreenConfig(),
        execution=SimpleNamespace(max_entry_slippage_bps=40.0),
        risk=SimpleNamespace(min_stop_atr_multiple=2.5),
    )
    th = us.ScreenThresholds.from_config(config)
    assert th.min_price_usd == 5.0
    assert th.min_market_cap_usd == 30_000_000
    assert th.max_half_spread_bps == 40.0
    assert th.max_atr_fraction == pytest.approx(0.2)
    assert th.min_history_bars == 210


def test_screen_ships_off():
    assert UniverseScreenConfig().enabled is False
    import yaml
    from pathlib import Path
    settings = yaml.safe_load(
        (Path(__file__).resolve().parents[1] / "config" / "settings.yaml").read_text()
    )
    assert settings["universe_screen"]["enabled"] is False


# -------------------------------------------------------- state machine ----

def _fail(symbol="ACME", code="spread_too_wide"):
    return us.ScreenResult(symbol=symbol, failures=[code])


def _pass(symbol="ACME"):
    return us.ScreenResult(symbol=symbol, failures=[], measured={"last_price": 50})


def test_candidate_that_passes_is_added():
    state = us.empty_state()
    event = us.apply_result(state, _pass(), today=TODAY, held=set())
    assert event["action"] == "added"
    assert state["admitted"]["ACME"]["status"] == "active"


def test_fail_once_flags_fail_twice_consecutively_removes():
    state = us.empty_state()
    us.apply_result(state, _pass(), today=TODAY - timedelta(days=14), held=set())
    assert us.apply_result(state, _fail(), today=TODAY - timedelta(days=7), held=set())["action"] == "flagged"
    assert state["admitted"]["ACME"]["status"] == "flagged"
    assert us.apply_result(state, _fail(), today=TODAY, held=set())["action"] == "removed"
    assert "ACME" not in state["admitted"]
    assert state["removed"]["ACME"]["reason"] == "spread_too_wide"


def test_a_second_failure_in_the_same_week_does_not_count():
    state = us.empty_state()
    us.apply_result(state, _pass(), today=TODAY - timedelta(days=14), held=set())
    us.apply_result(state, _fail(), today=TODAY - timedelta(days=1), held=set())
    assert us.apply_result(state, _fail(), today=TODAY, held=set()) is None
    assert state["admitted"]["ACME"]["status"] == "flagged"


def test_a_pass_between_failures_clears_the_flag():
    state = us.empty_state()
    us.apply_result(state, _pass(), today=TODAY - timedelta(days=21), held=set())
    us.apply_result(state, _fail(), today=TODAY - timedelta(days=14), held=set())
    assert us.apply_result(state, _pass(), today=TODAY - timedelta(days=7), held=set())["action"] == "cleared"
    assert us.apply_result(state, _fail(), today=TODAY, held=set())["action"] == "flagged"
    assert "ACME" in state["admitted"]


def test_a_held_name_is_never_removed():
    state = us.empty_state()
    us.apply_result(state, _pass(), today=TODAY - timedelta(days=14), held=set())
    us.apply_result(state, _fail(), today=TODAY - timedelta(days=7), held=set())
    event = us.apply_result(state, _fail(), today=TODAY, held={"ACME"})
    assert event["action"] == "removal_deferred_held"
    assert "ACME" in state["admitted"]


def test_delisted_is_removed_immediately_without_a_flag():
    state = us.empty_state()
    us.apply_result(state, _pass(), today=TODAY - timedelta(days=1), held=set())
    event = us.apply_result(state, _fail(code="asset_not_found"), today=TODAY, held=set())
    assert event["action"] == "removed"
    assert "ACME" not in state["admitted"]


def test_delisted_but_held_is_kept_and_reported():
    state = us.empty_state()
    us.apply_result(state, _pass(), today=TODAY - timedelta(days=1), held=set())
    event = us.apply_result(state, _fail(code="asset_not_tradable"), today=TODAY, held={"ACME"})
    assert event["action"] == "removal_deferred_held"
    assert "ACME" in state["admitted"]


def test_an_unreadable_screen_changes_nothing():
    state = us.empty_state()
    us.apply_result(state, _pass(), today=TODAY - timedelta(days=14), held=set())
    before = json.dumps(state, sort_keys=True)
    assert us.apply_result(state, _fail(code="market_data_unavailable"), today=TODAY, held=set()) is None
    assert json.dumps(state, sort_keys=True) == before


def test_store_round_trips(tmp_path):
    store = us.UniverseStore(tmp_path)
    state = us.empty_state()
    us.apply_result(state, _pass(), today=TODAY, held=set())
    store.save(state)
    assert store.load()["admitted"]["ACME"]["admitted_on"] == TODAY.isoformat()


# ------------------------------------------------------------ run_screen ----

def _run(state, assets, *, sources=None, configured=(), held=(), confirm=None,
         deadline=None, today=TODAY, bars=None):
    return us.run_screen(
        state, assets=assets, sources=sources or _sources(),
        get_bars_batch=lambda chunk: {s: (bars or _good_bars()) for s in chunk},
        th=TH, today=today, held=held, configured=configured,
        deadline=deadline if deadline is not None else time.monotonic() + 60,
        batch_size=2, confirm_missing_asset=confirm,
    )


def _asset(symbol, **kw):
    return {**GOOD_ASSET, "symbol": symbol, **kw}


def test_run_admits_passing_candidates_and_never_screens_configured_ones():
    state = us.empty_state()
    run = _run(state, [_asset("AAA"), _asset("SPY"), _asset("BBB", shortable=False)],
               configured=["SPY"])
    assert set(state["admitted"]) == {"AAA"}
    assert "SPY" not in state["screened"] and "SPY" not in state["admitted"]
    assert state["screened"]["BBB"]["failures"] == ["not_shortable"]
    assert [e["action"] for e in run.events] == ["added"]


def test_run_screens_each_symbol_once_per_week():
    state = us.empty_state()
    _run(state, [_asset("AAA")])
    calls = []
    sources = _sources()
    original = sources.get_profile
    sources.get_profile = lambda s: calls.append(s) or original(s)
    _run(state, [_asset("AAA")], sources=sources, today=TODAY)
    assert calls == []
    _run(state, [_asset("AAA")], sources=sources, today=TODAY + timedelta(days=7))
    assert calls == ["AAA"]


def test_absence_from_the_list_alone_never_removes():
    state = us.empty_state()
    _run(state, [_asset("AAA")])
    _run(state, [], today=TODAY + timedelta(days=1))
    assert "AAA" in state["admitted"]


def test_confirmed_delisting_removes_on_the_next_run_not_the_next_week():
    state = us.empty_state()
    _run(state, [_asset("AAA")])
    run = _run(state, [], confirm=lambda s: None, today=TODAY + timedelta(days=1))
    assert "AAA" not in state["admitted"]
    assert run.events[0]["action"] == "removed"
    assert run.events[0]["reasons"] == ["asset_not_found"]


def test_the_deadline_stops_the_pass_and_the_rest_resume_later():
    state = us.empty_state()
    run = _run(state, [_asset("AAA"), _asset("BBB")], deadline=time.monotonic() - 1)
    assert run.deadline_hit
    assert state["admitted"] == {}
    _run(state, [_asset("AAA"), _asset("BBB")])
    assert set(state["admitted"]) == {"AAA", "BBB"}


def test_a_failed_bar_batch_is_inconclusive():
    state = us.empty_state()
    run = us.run_screen(
        state, assets=[_asset("AAA")], sources=_sources(),
        get_bars_batch=lambda chunk: (_ for _ in ()).throw(RuntimeError("down")),
        th=TH, today=TODAY, held=(), configured=(), deadline=time.monotonic() + 60,
        batch_size=5,
    )
    assert run.inconclusive == 1
    assert state["admitted"] == {} and "AAA" not in state["screened"]


# ----------------------------------------------------------------- cap ----

def test_select_for_run_caps_non_held_and_always_includes_held():
    state = us.empty_state()
    for sym in ("AAA", "BBB", "CCC", "DDD"):
        us.apply_result(state, _pass(sym), today=TODAY, held=set())
    chosen = us.select_for_run(state, held={"DDD"}, cap=2, today=TODAY)
    assert set(chosen) == {"AAA", "BBB", "DDD"}
    assert chosen["DDD"]["held"] is True


def test_select_for_run_rotates_least_recently_offered_first():
    state = us.empty_state()
    for sym in ("AAA", "BBB", "CCC"):
        us.apply_result(state, _pass(sym), today=TODAY, held=set())
    first = us.select_for_run(state, held=set(), cap=2, today=TODAY)
    second = us.select_for_run(state, held=set(), cap=2, today=TODAY + timedelta(days=1))
    assert set(first) == {"AAA", "BBB"}
    assert "CCC" in second


# ------------------------------------------------------- pipeline wiring ----

def _pipeline(tmp_path, *, enabled=True):
    pipeline = TradingPipeline.__new__(TradingPipeline)
    pipeline.config = SimpleNamespace(
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
    # Item 165: sessions_held now comes from `broker.trading_sessions_held`
    # (holiday-aware). None of these tests span a market holiday, so
    # delegating the mock to the real weekday counter reproduces the same
    # numbers as the plain weekday count used to give directly.
    from src.trading_calendar import trading_sessions_held as _weekday_sessions_held

    pipeline.broker = MagicMock()
    pipeline.broker.trading_sessions_held.side_effect = _weekday_sessions_held
    pipeline.broker.get_asset_record.return_value = GOOD_ASSET
    pipeline.broker.get_transient_equity_eligibility.return_value = {
        "eligible": True, "reason": "eligible", "name": "Acme", "exchange": "nyse"}
    pipeline.market = MagicMock()
    pipeline.market.get_ohlcv.return_value = _good_bars()
    pipeline.market.get_company_profile.return_value = {
        "market_cap_usd": 5e9, "sector_raw": "Industrials", "quote_type": "EQUITY"}
    pipeline.sec_form4_provider = MagicMock()
    pipeline.sec_form4_provider.recent_filings.return_value = []
    return pipeline


def test_side_door_runs_the_screen_when_on(tmp_path, monkeypatch):
    pipeline = _pipeline(tmp_path)
    monkeypatch.setattr("src.pipeline._get_sector", lambda s: "Industrials")
    pipeline.sec_form4_provider.recent_filings.return_value = [("DEFM14A", "2026-09-01", "")]
    ok, reason, details = pipeline._evaluate_external_admission_gates("ACME")
    assert (ok, reason) == (False, "pending_takeover")
    pipeline.broker.get_transient_equity_eligibility.assert_not_called()


def test_side_door_keeps_its_old_gate_when_off(tmp_path, monkeypatch):
    pipeline = _pipeline(tmp_path, enabled=False)
    monkeypatch.setattr("src.pipeline._get_sector", lambda s: "Industrials")
    pipeline.market.get_ohlcv.return_value = _bars(30, price=50, rng=0.0005)
    ok, reason, _ = pipeline._evaluate_external_admission_gates("ACME")
    assert ok is True  # 30 bars would fail the screen's one-year history
    pipeline.broker.get_transient_equity_eligibility.assert_called_once()
    pipeline.sec_form4_provider.recent_filings.assert_not_called()


def test_nomination_door_uses_the_screen(tmp_path, monkeypatch):
    pipeline = _pipeline(tmp_path)
    monkeypatch.setattr("src.pipeline._get_sector", lambda s: "Industrials")
    pipeline.broker.get_asset_record.return_value = {**GOOD_ASSET, "shortable": False}
    admitted, details = pipeline._admit_nominated_external_symbols(["acme"])
    assert admitted == set()
    pipeline.broker.get_asset_record.return_value = GOOD_ASSET
    admitted, details = pipeline._admit_nominated_external_symbols(["acme"])
    assert admitted == {"ACME"}
    assert details["ACME"]["screen"] == "universe_screen"


def _purchase(days_ago):
    from src.util.time import et_today
    return SimpleNamespace(
        symbol="ACME", transaction_code="P", admission_eligible=True,
        transaction_value_usd=500_000, accession_number="0001-26-000001",
        actor="Director", known_at="2026-09-01T12:00:00Z",
        disclosure_date=et_today() - timedelta(days=days_ago),
    )


def test_form4_door_runs_the_screen_and_its_age_gate(tmp_path, monkeypatch):
    pipeline = _pipeline(tmp_path)
    monkeypatch.setattr("src.pipeline._get_sector", lambda s: "Industrials")
    admitted, details = pipeline._admit_transient_smart_money_symbols([_purchase(10)])
    assert admitted == {"ACME"}
    assert details["ACME"]["screen"] == "universe_screen"
    # A purchase disclosed ~a year ago (the RSG case) no longer admits.
    assert pipeline._admit_transient_smart_money_symbols([_purchase(364)])[0] == set()


def test_form4_age_gate_is_off_with_the_screen(tmp_path, monkeypatch):
    pipeline = _pipeline(tmp_path, enabled=False)
    monkeypatch.setattr("src.pipeline._get_sector", lambda s: "Industrials")
    pipeline.market.get_ohlcv.return_value = _bars(30, price=50, rng=0.0005)
    assert pipeline._admit_transient_smart_money_symbols([_purchase(364)])[0] == {"ACME"}


def test_screened_universe_reaches_the_session_capped(tmp_path):
    pipeline = _pipeline(tmp_path)
    store = us.UniverseStore(tmp_path)
    state = us.empty_state()
    for sym in ("AAA", "BBB", "CCC", "DDD", "EEE"):
        us.apply_result(state, _pass(sym), today=TODAY, held=set())
    store.save(state)
    symbols, details = pipeline._admit_screened_universe_symbols(
        [SimpleNamespace(symbol="EEE")],
    )
    assert "EEE" in symbols
    assert len(symbols - {"EEE"}) == 3  # nominations.max_per_seat_per_run
    assert details["AAA"]["reason"] == "universe_screen_admission"


def test_screened_universe_is_empty_when_off(tmp_path):
    pipeline = _pipeline(tmp_path, enabled=False)
    state = us.empty_state()
    us.apply_result(state, _pass("AAA"), today=TODAY, held=set())
    us.UniverseStore(tmp_path).save(state)
    assert pipeline._admit_screened_universe_symbols([]) == (set(), {})


def test_evening_pass_records_changes_and_morning_shows_them_once(tmp_path, monkeypatch):
    pipeline = _pipeline(tmp_path)
    pipeline.db = MagicMock()
    pipeline.broker.list_assets.return_value = [GOOD_ASSET]
    pipeline.broker.get_positions.return_value = []
    pipeline.market.get_ohlcv_batch.side_effect = lambda chunk, days: {
        s: _good_bars() for s in chunk}
    pipeline.sec_form4_provider.listed_map.return_value = {}
    monkeypatch.setattr("src.pipeline._get_sector", lambda s: "Industrials")
    summary = pipeline._run_universe_screen("evening-x")
    assert summary["passed"] == 1
    kinds = [c.kwargs["kind"] for c in pipeline.db.insert_specialist_evidence.call_args_list]
    assert kinds.count("universe_change") == 1 and "universe_screen_run" in kinds

    result = {}
    pipeline._attach_universe_changes(result)
    assert [e["action"] for e in result["universe_changes"]["events"]] == ["added"]
    again = {}
    pipeline._attach_universe_changes(again)
    assert again["universe_changes"]["events"] == []


def test_morning_message_lists_changes_below_the_pnl_block():
    from src.notifier import format_session_result

    result = {
        "status": "ok", "run_id": "morning-x",
        "universe_changes": {
            "events": [
                {"symbol": "AAA", "action": "added", "reasons": ["passed"]},
                {"symbol": "BBB", "action": "removed", "reasons": ["pending_takeover"]},
            ],
            "admitted_count": 1, "flagged_count": 0,
        },
    }
    text = format_session_result("morning", result, 12.0)
    assert "Stock list changed" in text
    assert "BBB removed — being taken over" in text
    lines = text.splitlines()
    pnl_at = next(i for i, l in enumerate(lines) if "P&L" in l or "profit" in l.lower())
    universe_at = next(i for i, l in enumerate(lines) if "Stock list changed" in l)
    assert pnl_at < universe_at


def test_morning_message_is_silent_about_the_screen_when_it_is_off():
    from src.notifier import format_session_result

    text = format_session_result("morning", {"status": "ok", "run_id": "m"}, 1.0)
    assert "Stock list" not in text
