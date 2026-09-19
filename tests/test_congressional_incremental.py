"""The congressional feed reads each disclosure once, says how old its copy
is, and leaves a record the morning health report can classify.

Owner direction 2026-09-19, before the feed is switched on: set it up like the
insider feed — a cache, proper logs the desk's monitoring reads, and no logic
that re-reads everything every day. Each test below pins one of those.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from src import log_health as L
from src.data import congressional_trading as ct
from src.data.congressional_trading import (
    CombinedSmartMoneyProvider,
    CongressionalTradingProvider,
)
from src.util.time import et_now, et_today


def _kadoa(i: int, *, filed_days_ago: int = 3, traded_days_ago: int = 50, ticker="NVDA"):
    return {
        "id": f"house_{i}", "ticker": ticker, "filer_name": f"Member {i}",
        "filer_id": f"house_member_{i}", "transaction_type": "Purchase",
        "amount_range_low": 15001, "amount_range_high": 50000,
        "amount_range_label": "$15,001 - $50,000",
        "transaction_date": (et_today() - timedelta(days=traded_days_ago)).isoformat(),
        "filing_date": (et_today() - timedelta(days=filed_days_ago)).isoformat(),
        "doc_url": f"https://disclosures-clerk.house.gov/{i}.pdf", "chamber": "house",
        # Recomputed by the source every day; must not make a row look new.
        "ret_since": 0.01 * i,
    }


def _cw(i: int, *, traded_days_ago: int = 50, ticker="MSFT"):
    return {
        "transaction_date": (et_today() - timedelta(days=traded_days_ago)).isoformat(),
        "owner": "Self", "ticker": ticker, "asset_description": f"{ticker} stock",
        "asset_type": "Stock", "type": "Sale (Full)", "amount": "$1,001 - $15,000",
        "comment": "--", "ptr_link": f"https://efdsearch.senate.gov/{i}/",
        "bioguide_id": f"S{i:06d}", "member_name": f"Senator {i}",
        "party": "X", "state": "Y", "chamber": "Senate",
    }


class _Server:
    """Two static files that honour If-None-Match, like both real sources."""

    def __init__(self, kadoa, congresswatch, *, honour_etag=True):
        self.files = {"kadoa": kadoa, "congresswatch": congresswatch}
        self.honour_etag = honour_etag
        self.down: dict[str, Exception] = {}
        self.calls: list[tuple[str, dict]] = []

    def _etag(self, name):
        return '"' + str(abs(hash(repr(self.files[name])))) + '"'

    def get(self, url, headers=None, **_kwargs):
        name = "congresswatch" if "congresswatch" in url else "kadoa"
        self.calls.append((name, dict(headers or {})))
        if name in self.down:
            raise self.down[name]
        etag = self._etag(name)
        response = Mock()
        response.headers = {"ETag": etag}
        if self.honour_etag and (headers or {}).get("If-None-Match") == etag:
            response.status_code = 304
            return response
        response.status_code = 200
        response.raise_for_status.return_value = None
        payload = self.files[name]
        if isinstance(payload, Exception):
            response.json.side_effect = payload
        else:
            response.json.return_value = payload
        return response


def _provider(tmp_path, server, **kwargs):
    session = Mock()
    session.get = Mock(side_effect=server.get)
    return CongressionalTradingProvider(
        data_dir=str(tmp_path / "congress"), session=session,
        min_transaction_value_usd=1, external_min_transaction_value_usd=1,
        **kwargs,
    )


# --- 1. nothing already processed is processed again ------------------------


def test_second_refresh_processes_zero_already_seen_disclosures(tmp_path):
    """Even when the source ignores the "has it changed?" question and sends
    the whole file again, not one row already processed is parsed again."""
    server = _Server([_kadoa(i) for i in range(5)], [_cw(i) for i in range(4)],
                     honour_etag=False)
    provider = _provider(tmp_path, server)

    first = provider.refresh()
    assert first["processed_disclosures"] == 9
    assert first["new_disclosures"] == 9

    calls = []
    original = provider._normalize_kadoa
    provider._normalize_kadoa = lambda row: calls.append(row) or original(row)
    second = provider.refresh()

    assert second["processed_disclosures"] == 0
    assert second["new_disclosures"] == 0
    assert calls == [], "a row already processed was parsed again"
    for source, fetched in (("kadoa", 5), ("congresswatch", 4)):
        counts = second["congressional_sources"][source]
        assert counts["fetched"] == fetched
        assert counts["already_seen"] == fetched
        assert counts["processed"] == 0
    assert second["cached_observations"] == first["cached_observations"] == 9


def test_an_unchanged_file_is_not_even_downloaded(tmp_path):
    server = _Server([_kadoa(1)], [_cw(1)])
    provider = _provider(tmp_path, server)
    provider.refresh()
    second = provider.refresh()

    for source in ("kadoa", "congresswatch"):
        counts = second["congressional_sources"][source]
        assert counts["outcome"] == "not_modified"
        assert counts["fetched"] == 0
    asked = [h for name, h in server.calls[2:]]
    assert all("If-None-Match" in h for h in asked)
    assert second["cached_observations"] == 2


def test_only_the_new_disclosure_is_processed_and_old_ones_are_untouched(tmp_path):
    server = _Server([_kadoa(i) for i in range(3)], [])
    provider = _provider(tmp_path, server)
    provider.refresh()
    before = {r["group_key"]: r for r in json.loads(provider.observations_path.read_text())}

    # The source recomputes its return columns daily and adds one filing.
    server.files["kadoa"] = [
        {**row, "ret_since": 9.9} for row in server.files["kadoa"]
    ] + [_kadoa(7, filed_days_ago=1)]
    second = provider.refresh()

    counts = second["congressional_sources"]["kadoa"]
    assert counts["fetched"] == 4
    assert counts["already_seen"] == 3
    assert counts["processed"] == 1 and counts["new"] == 1
    after = {r["group_key"]: r for r in json.loads(provider.observations_path.read_text())}
    assert len(after) == 4
    for key, row in before.items():
        assert after[key] == row


def test_a_dropped_row_is_remembered_not_re_judged_every_day(tmp_path):
    future = _cw(1, traded_days_ago=-90)  # the real SONY 2026-12-26 shape
    no_ticker = {**_cw(2), "ticker": ""}
    server = _Server([], [future, no_ticker, _cw(3)], honour_etag=False)
    provider = _provider(tmp_path, server)

    first = provider.refresh()["congressional_sources"]["congresswatch"]
    assert first["dropped_by_reason"]["future_dated"] == 1
    assert first["dropped_by_reason"]["no_ticker"] == 1
    assert first["new"] == 1

    second = provider.refresh()["congressional_sources"]["congresswatch"]
    assert second["processed"] == 0
    assert second["dropped"] == 0


# --- 2. the watermark -------------------------------------------------------


def test_watermark_advances_to_the_newest_filing_processed(tmp_path):
    server = _Server([_kadoa(1, filed_days_ago=10)], [_cw(1, traded_days_ago=60)])
    provider = _provider(tmp_path, server)

    first = provider.refresh()["congressional_sources"]
    assert first["kadoa"]["watermark_before"] == ""
    assert first["kadoa"]["watermark_after"] == (et_today() - timedelta(days=10)).isoformat()

    server.files["kadoa"] = server.files["kadoa"] + [_kadoa(2, filed_days_ago=2)]
    server.files["congresswatch"] = server.files["congresswatch"] + [_cw(2, traded_days_ago=44)]
    second = provider.refresh()["congressional_sources"]

    assert second["kadoa"]["watermark_before"] == (et_today() - timedelta(days=10)).isoformat()
    assert second["kadoa"]["watermark_after"] == (et_today() - timedelta(days=2)).isoformat()
    assert second["congresswatch"]["watermark_after"] == (
        et_today() - timedelta(days=44)
    ).isoformat()
    manifest = json.loads(provider.manifest_path.read_text())
    assert manifest["sources"]["kadoa"]["watermark"] == second["kadoa"]["watermark_after"]


def test_a_late_filing_older_than_the_watermark_is_still_read(tmp_path):
    """The watermark is reported, never used to skip: a late filing can carry
    an older date than one already read."""
    server = _Server([_kadoa(1, filed_days_ago=2)], [])
    provider = _provider(tmp_path, server)
    provider.refresh()
    server.files["kadoa"] = server.files["kadoa"] + [_kadoa(2, filed_days_ago=20)]
    second = provider.refresh()["congressional_sources"]["kadoa"]
    assert second["new"] == 1
    assert second["watermark_after"] == second["watermark_before"]


def test_running_out_of_time_keeps_progress_and_resumes(tmp_path, monkeypatch):
    server = _Server([_kadoa(i) for i in range(4)], [])
    provider = _provider(tmp_path, server)

    clock = {"t": 0.0}
    monkeypatch.setattr(ct.time, "monotonic", lambda: clock["t"])
    original = provider._normalize_kadoa

    def slow(row):
        clock["t"] += provider.refresh_deadline_s / 2.5
        return original(row)

    provider._normalize_kadoa = slow
    first = provider.refresh()["congressional_sources"]["kadoa"]
    assert first["complete"] is False
    assert 0 < first["processed"] < 4
    # The copy was not fully read, so the next refresh must not be told
    # "unchanged" and strand the rest.
    manifest = json.loads(provider.manifest_path.read_text())
    assert not manifest["sources"]["kadoa"].get("etag")

    clock["t"] = 0.0
    provider._normalize_kadoa = original
    second = provider.refresh()["congressional_sources"]["kadoa"]
    assert second["already_seen"] == first["processed"]
    assert second["processed"] == 4 - first["processed"]
    assert second["complete"] is True


def test_a_lost_saved_copy_forces_one_full_re_read_not_a_silent_gap(tmp_path):
    server = _Server([_kadoa(1)], [])
    provider = _provider(tmp_path, server)
    provider.refresh()
    provider.store_path.unlink()

    again = provider.refresh()["congressional_sources"]["kadoa"]
    assert again["outcome"] == "fetched"  # not asked "unchanged?"
    assert again["processed"] == 1
    assert json.loads(provider.observations_path.read_text())


# --- 3. a stale copy is served labelled -------------------------------------


def test_a_failed_source_serves_its_saved_copy_labelled_with_its_age(tmp_path, caplog):
    server = _Server([_kadoa(1)], [_cw(1)])
    provider = _provider(tmp_path, server)
    provider.refresh()

    # Make the saved copy a day and a bit old, then take kadoa down.
    manifest = json.loads(provider.manifest_path.read_text())
    yesterday = (et_now() - timedelta(hours=26)).isoformat()
    manifest["sources"]["kadoa"]["last_success_at"] = yesterday
    provider.manifest_path.write_text(json.dumps(manifest))
    server.down["kadoa"] = ConnectionError("github down")

    with caplog.at_level(logging.WARNING, logger="src.data.congressional_trading"):
        result = provider.refresh()
        rows, error = provider.fetch(["NVDA", "MSFT"])

    kadoa = result["congressional_sources"]["kadoa"]
    assert kadoa["outcome"] == "unreachable"
    assert kadoa["cache_age_hours"] == pytest.approx(26, abs=0.2)
    assert result["status"] == "partial"
    assert result["congressional_freshness"]["stale_sources"] == ["kadoa"]
    # Served, not dropped...
    assert {r.symbol for r in rows} == {"NVDA", "MSFT"}
    # ...and labelled with its age, never silently.
    assert error.startswith("congressional_stale_cache:kadoa:age_h=")
    assert f"last_success={yesterday}" in error
    assert any("serving the saved copy last refreshed" in m for m in caplog.messages)
    assert any(m.startswith("Congressional cache served stale: source=kadoa") for m in caplog.messages)


def test_a_copy_from_an_earlier_day_is_stale_even_without_a_failure(tmp_path):
    """The refresh runs once a day; a copy from yesterday means today's did not."""
    server = _Server([_kadoa(1)], [])
    provider = _provider(tmp_path, server)
    provider.refresh()
    manifest = json.loads(provider.manifest_path.read_text())
    for state in manifest["sources"].values():
        state["last_success_at"] = (et_now() - timedelta(days=2)).isoformat()
    provider.manifest_path.write_text(json.dumps(manifest))

    _rows, error = provider.fetch(["NVDA"])
    assert "kadoa:age_h=48" in error and "congresswatch:age_h=48" in error


def test_a_current_copy_carries_no_label(tmp_path):
    provider = _provider(tmp_path, _Server([_kadoa(1)], [_cw(1)]))
    provider.refresh()
    _rows, error = provider.fetch(["NVDA"])
    assert error is None


# --- 4. honest freshness ----------------------------------------------------


def test_the_seat_is_told_how_old_the_newest_disclosure_and_trade_are(tmp_path):
    server = _Server(
        [_kadoa(1, filed_days_ago=2, traded_days_ago=60),
         _kadoa(2, filed_days_ago=9, traded_days_ago=52)],
        [_cw(3, traded_days_ago=40)],
    )
    provider = _provider(tmp_path, server)
    fresh = provider.refresh()["congressional_freshness"]

    assert fresh["newest_disclosure_age_days"] == 2
    assert fresh["newest_disclosure_estimated"] is False
    assert fresh["newest_transaction_age_days"] == 40
    assert fresh["reporting_lag_days"] == {"min": 43, "median": 50.5, "max": 58, "rows": 2}
    assert provider.congressional_freshness()["newest_disclosure_age_days"] == 2


def test_a_newly_filed_disclosure_of_an_old_trade_is_not_labelled_fresh(tmp_path):
    provider = _provider(tmp_path, _Server([_kadoa(1, filed_days_ago=1, traded_days_ago=60)], []))
    provider.refresh()
    rows, _ = provider.fetch(["NVDA"])
    assert rows[0].disclosure_age_days == 1
    assert rows[0].freshness == "delayed"


# --- 5. the record and the health report ------------------------------------


def test_the_counts_are_recorded_where_the_desk_records_its_status(tmp_path):
    from src.pipeline import TradingPipeline

    provider = _provider(tmp_path, _Server([_kadoa(1)], [_cw(1)]))
    combined = CombinedSmartMoneyProvider([provider])
    refresh = combined.refresh()
    assert refresh["congressional"]["new_disclosures"] == 2

    rows = []

    class _Db:
        def insert_specialist_evidence(self, **kwargs):
            rows.append(kwargs)

    record = TradingPipeline._record_congressional_refresh.__get__(SimpleNamespace(db=_Db()))
    record("run-1", refresh)
    record("run-2", {"status": "ok"})  # switch off: no congressional block
    assert len(rows) == 1
    assert rows[0]["kind"] == "congressional_refresh"
    payload = json.loads(rows[0]["evidence_json"])
    kadoa = payload["congressional_sources"]["kadoa"]
    for key in ("fetched", "already_seen", "processed", "new", "dropped",
                "dropped_by_reason", "watermark_before", "watermark_after",
                "duration_s"):
        assert key in kadoa
    assert payload["congressional_freshness"]["newest_disclosure_age_days"] == 3


def _emitted_lines(caplog) -> list[L.LogRecord]:
    """The provider's own log records, in the production line format."""
    stamp = datetime(2026, 9, 19, 12, 0, 0)
    out = []
    for i, rec in enumerate(caplog.records):
        line = (
            f"{(stamp + timedelta(seconds=i)):%Y-%m-%d %H:%M:%S},000 "
            f"[{rec.levelname}] {rec.name}: {rec.getMessage()}"
        )
        match = L._LINE_RE.match(line)
        assert match, line
        out.append(L.LogRecord(
            timestamp=(stamp + timedelta(seconds=i)).replace(tzinfo=L.LOG_TZ),
            level=match.group("level"), source=match.group("logger"),
            message=match.group("msg"),
        ))
    return out


@pytest.mark.parametrize(
    ("break_it", "family"),
    [
        (lambda s: s.down.__setitem__("kadoa", ConnectionError("refused")),
         "congress_source_unreachable"),
        (lambda s: s.files.__setitem__("kadoa", {"not": "a list"}),
         "congress_source_unreadable"),
        (lambda s: s.files.__setitem__("kadoa", ValueError("Expecting value")),
         "congress_source_unreadable"),
    ],
)
def test_each_failure_line_is_classified_by_the_health_report(tmp_path, caplog, break_it, family):
    server = _Server([_kadoa(1)], [_cw(1)])
    provider = _provider(tmp_path, server)
    provider.refresh()
    break_it(server)
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="src.data.congressional_trading"):
        provider.refresh()
        provider.fetch(["NVDA"])

    found = {
        f.key for r in _emitted_lines(caplog)
        if (f := L.classify(r.message, r.level)) is not None
    }
    assert family in found
    assert "congress_cache_stale" in found
    report = L.analyse(
        _emitted_lines(caplog), datetime(2026, 9, 19, tzinfo=L.LOG_TZ),
        datetime(2026, 9, 20, tzinfo=L.LOG_TZ), log_dir=tmp_path / "no-logs",
    )
    reported = {f.family.key for f in report.reported}
    assert {family, "congress_cache_stale"} <= reported
    assert report.verdict == "degraded"


def test_a_source_that_answers_again_clears_its_earlier_failure(tmp_path, caplog):
    server = _Server([_kadoa(1)], [_cw(1)])
    provider = _provider(tmp_path, server)
    provider.refresh()
    server.down["kadoa"] = ConnectionError("refused")
    with caplog.at_level(logging.INFO, logger="src.data.congressional_trading"):
        provider.refresh()
        server.down.clear()
        provider.refresh()
    records = _emitted_lines(caplog)
    failure = [r for r in records if r.message.startswith("Congressional source unreachable")]
    assert failure and L.classify(failure[0].message, failure[0].level).key == (
        "congress_source_unreachable"
    )
    report = L.analyse(
        records, datetime(2026, 9, 19, tzinfo=L.LOG_TZ),
        datetime(2026, 9, 20, tzinfo=L.LOG_TZ), log_dir=tmp_path / "no-logs",
    )
    assert "congress_source_unreachable" not in {f.family.key for f in report.reported}


def test_a_healthy_refresh_produces_no_health_bullet(tmp_path, caplog):
    provider = _provider(tmp_path, _Server([_kadoa(1)], [_cw(1)]))
    with caplog.at_level(logging.INFO, logger="src.data.congressional_trading"):
        provider.refresh()
        provider.refresh()
        provider.fetch(["NVDA"])
    records = _emitted_lines(caplog)
    assert records, "the refresh must log its counts"
    assert all(L.classify(r.message, r.level) is None for r in records)
    named = [r.message for r in records if r.message.startswith("Congressional refresh: source=")]
    assert len(named) == 4  # one per source per refresh
    assert "watermark(disclosure_date)=" in named[0] and "duration_s=" in named[0]
