"""Daily record-only universe screen (src/universe_daily.py). Alpaca, Yahoo
and SEC are all fakes: nothing here touches the network."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from types import SimpleNamespace

from src import universe_daily as ud
from tests.test_universe_screen import GOOD_ASSET, TH, TODAY, _bars, _good_bars

PROFILE = {"market_cap_usd": 5e9, "sector": "Industrials", "quote_type": "EQUITY"}


def _asset(symbol, **over):
    return {**GOOD_ASSET, "symbol": symbol, "name": f"{symbol} Corp. Common Stock", **over}


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def _run(
    assets,
    tmp_path,
    *,
    bars=None,
    profile=PROFILE,
    filings=(),
    deadline=1e9,
    clock=None,
    cache=None,
    batch=2,
    after_bars=None,
):
    calls = {"bars": [], "profile": [], "filings": []}

    def get_bars(chunk):
        if after_bars is not None:
            after_bars()
        calls["bars"].append(list(chunk))
        return {s: (bars or {}).get(s, _good_bars()) for s in chunk}

    def get_profile(s):
        calls["profile"].append(s)
        return profile(s) if callable(profile) else profile

    def get_filings(s):
        calls["filings"].append(s)
        return list(filings)

    run = ud.run_daily_record(
        assets,
        get_bars_batch=get_bars,
        get_profile=get_profile,
        get_filings=get_filings,
        th=TH,
        today=TODAY,
        deadline=deadline,
        batch_size=batch,
        cache=cache or ud.ReadCache(tmp_path),
        clock=clock or Clock(),
    )
    return run, calls


def test_every_name_gets_a_record_with_its_failing_check(tmp_path):
    assets = [
        _asset("GOOD"),
        _asset("DEAD", status="inactive"),
        _asset("PENY"),
        _asset("TINY"),
        _asset("DEAL"),
    ]
    run, calls = _run(
        assets,
        tmp_path,
        bars={"PENY": _bars(300, price=2.0, rng=0.0005)},
        profile=lambda s: {**PROFILE, "market_cap_usd": 1e6} if s == "TINY" else PROFILE,
        filings=[],
    )
    rec = run.records
    assert set(rec) == {"GOOD", "DEAD", "PENY", "TINY", "DEAL"}
    assert rec["GOOD"]["status"] == "passed"
    assert rec["DEAD"] == {"symbol": "DEAD", "status": "dropped", "failures": ["asset_inactive"], "measured": {}}
    assert rec["PENY"]["failures"] == ["price_below_minimum"]
    assert rec["TINY"]["failures"] == ["company_too_small"]
    # The dead name never cost a bar read; bars came many symbols per request.
    assert "DEAD" not in sum(calls["bars"], [])
    assert max(len(c) for c in calls["bars"]) == 2
    summary = run.summary()
    assert summary["names_listed"] == 5
    assert summary["passed"] == 2
    assert summary["dropped_by_first_reason"] == {
        "asset_inactive": 1,
        "company_too_small": 1,
        "price_below_minimum": 1,
    }
    assert summary["calls"]["yahoo_profile"] == 3  # GOOD, TINY, DEAL (PENY failed on bars)


def test_pending_takeover_is_recorded(tmp_path):
    run, _ = _run([_asset("DEAL")], tmp_path, filings=[("DEFM14A", "2026-09-01", "")])
    assert run.records["DEAL"]["failures"] == ["pending_takeover"]


def test_bar_outage_is_inconclusive_not_dropped(tmp_path):
    def boom(chunk):
        raise RuntimeError("alpaca down")

    run = ud.run_daily_record(
        [_asset("AAA")],
        get_bars_batch=boom,
        get_profile=lambda s: PROFILE,
        get_filings=lambda s: [],
        th=TH,
        today=TODAY,
        deadline=1e9,
        batch_size=50,
        cache=ud.ReadCache(tmp_path),
        clock=Clock(),
    )
    assert run.records["AAA"]["status"] == "inconclusive"
    assert run.summary()["dropped"] == 0


def test_time_limit_marks_every_unreached_name(tmp_path):
    clock = Clock()
    seen = []

    def get_profile(s):
        seen.append(s)
        clock.now = 10.0  # the first network read uses up the budget
        return PROFILE

    assets = [_asset(s) for s in ("AAA", "BBB", "CCC")]
    run, _ = _run(assets, tmp_path, profile=get_profile, deadline=5.0, clock=clock)
    statuses = {s: r["status"] for s, r in run.records.items()}
    assert statuses["AAA"] == "passed"
    assert statuses["BBB"] == statuses["CCC"] == "unreached"
    assert run.records["BBB"]["reason"] == "unreached: time limit"
    assert run.summary()["unreached"] == 2
    assert run.deadline_hit


def test_profile_and_filings_are_cached_within_the_week(tmp_path):
    cache = ud.ReadCache(tmp_path)
    _, first = _run([_asset("AAA")], tmp_path, cache=cache)
    cache.save()
    run, second = _run([_asset("AAA")], tmp_path, cache=ud.ReadCache(tmp_path))
    assert first["profile"] == ["AAA"] and first["filings"] == ["AAA"]
    assert second["profile"] == [] and second["filings"] == []
    assert run.records["AAA"]["status"] == "passed"
    assert run.records["AAA"]["profile_read_on"] == TODAY.isoformat()


def test_stale_cache_is_used_after_the_time_limit(tmp_path):
    cache = ud.ReadCache(tmp_path)
    cache.put("profile", "AAA", PROFILE, TODAY.replace(day=1))
    cache.put("filings", "AAA", [], TODAY.replace(day=1))
    clock = Clock()

    def spend_budget():
        clock.now = 10.0

    run, calls = _run([_asset("AAA")], tmp_path, cache=cache, deadline=5.0, clock=clock, after_bars=spend_budget)
    assert calls["profile"] == []
    assert run.records["AAA"]["status"] == "passed"
    assert run.records["AAA"]["profile_read_on"] == TODAY.replace(day=1).isoformat()
    # Without a cache, the same late name is unreached rather than read.
    clock.now = 0.0
    late, _ = _run([_asset("BBB")], tmp_path, deadline=5.0, clock=clock, after_bars=spend_budget)
    assert late.records["BBB"]["status"] == "unreached"


def test_write_record_is_durable_and_marks_the_day(tmp_path):
    run, _ = _run([_asset("AAA"), _asset("DEAD", tradable=False)], tmp_path)
    assert not ud.already_recorded(tmp_path, TODAY)
    summary = ud.write_record(tmp_path, run)
    assert ud.already_recorded(tmp_path, TODAY)
    lines = (tmp_path / "daily" / f"{TODAY.isoformat()}.jsonl").read_text().splitlines()
    rows = [json.loads(line) for line in lines]
    assert [r["symbol"] for r in rows] == ["AAA", "DEAD"]
    assert rows[1]["failures"] == ["asset_not_tradable"]
    on_disk = json.loads((tmp_path / "daily" / f"{TODAY.isoformat()}.summary.json").read_text())
    assert on_disk["names_listed"] == summary["names_listed"] == 2


def test_alpaca_bars_one_request_for_many_symbols_and_counts_pages():
    ts = datetime(2026, 9, 17, 4, tzinfo=timezone.utc)
    bar = SimpleNamespace(timestamp=ts, open=10, high=11, low=9, close=10.5, volume=100)
    requests = []

    class FakeClient:
        def _one_request(self, *a, **k):
            return None

        def get_stock_bars(self, req):
            requests.append(req)
            self._one_request()
            self._one_request()  # the SDK paging twice
            return SimpleNamespace(data={"BRK.B": [bar], "AAA": [bar]})

    fetch = ud.AlpacaDailyBars(FakeClient(), 400, TODAY)
    out = fetch(["AAA", "BRK-B", "NONE"])
    assert len(requests) == 1
    assert sorted(requests[0].symbol_or_symbols) == ["AAA", "BRK.B", "NONE"]
    assert out["BRK-B"][0].close == 10.5 and out["NONE"] == []
    assert fetch.calls == 2
