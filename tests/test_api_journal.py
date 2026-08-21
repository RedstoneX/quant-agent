"""Stage 5 — Mission Control API functional contract tests for the
journal (`/journal/dates`, `/journal/{date}`) and forensic search
(`/search`) routes. Same pattern as test_api_contract.py / test_api_evidence.py:
seeds a real temp SQLite DB via `src.storage.db.Database`, monkeypatches
`src.api.db_reads.get_db_path`, exercises through a real FastAPI TestClient.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import src.api.db_reads as db_reads
from src.api.server import app
from src.storage.db import Database

RUN_ID = "run-abc12345"
DECISION_ID = f"{RUN_ID}-dec-000001"
DATE = "2026-08-08"


@pytest.fixture
def client() -> TestClient:
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def seeded_journal_db(tmp_path, monkeypatch):
    db_path = tmp_path / "quant_agent_test.db"
    db = Database(str(db_path))
    db.initialize()

    db.insert_agent_log(
        agent_name="tech_analyst", run_id=RUN_ID,
        input_summary="tech input", output_summary="tech output",
        full_response="{}", model="claude-opus-4-7", tokens_used=200,
        cost_usd=0.01,
    )
    db.insert_agent_log(
        agent_name="portfolio_manager", run_id=RUN_ID,
        input_summary="pm input", output_summary="Deployed into AAPL strength",
        full_response='{"targets": []}', model="gpt-5.5", tokens_used=500,
        cost_usd=0.05, decision_id=DECISION_ID,
    )
    db.insert_trade(
        symbol="AAPL", action="BUY", qty=10, price=150.0,
        reasoning="services growth thesis", run_id=RUN_ID,
        stop_loss=140.0, take_profit=170.0,
        broker_order_id="ord-1", fill_status="filled", decision_id=DECISION_ID,
    )
    # insert_agent_log/insert_trade default `timestamp` to `datetime('now')`
    # (real wall-clock UTC), not the fixed DATE this fixture is journaling
    # for. Pin both rows' timestamps into DATE's ET trading day so the
    # day-bounded journal query (src.api.db_reads.get_journal_day) finds
    # them — mirrors tests/test_db.py's today_only ET-day test pattern.
    fixed_ts = f"{DATE} 15:00:00"
    for table in ("agent_logs", "trades", "specialist_evidence"):
        db.conn.execute(
            f"UPDATE {table} SET timestamp = ? WHERE run_id = ?",
            (fixed_ts, RUN_ID),
        )
    db.conn.commit()
    db.insert_daily_pnl(date=DATE, total_value=100_000.0, daily_pnl=500.0, daily_return_pct=0.5)
    db.save_insights(
        date=DATE, tomorrow_outlook="cautiously bullish", lessons="none yet",
        suggested_actions="hold", risk_rating="low",
    )
    db.insert_specialist_evidence(
        run_id=RUN_ID, agent_name="tech_analyst", kind="analysis", scope="symbol",
        symbol="AAPL", evidence_json='{"symbol": "AAPL", "rating": "buy"}',
    )

    db.close()
    monkeypatch.setattr(db_reads, "get_db_path", lambda: str(db_path))
    return db_path


def test_journal_dates_lists_seeded_date(client, seeded_journal_db):
    r = client.get("/journal/dates")
    assert r.status_code == 200
    assert r.json()["dates"] == [DATE]


RUN_ONLY_DATE = "2026-08-09"
RUN_ONLY_RUN_ID = "midday-def45678"


@pytest.fixture
def run_only_day_db(tmp_path, monkeypatch):
    """2026-08-21 Mission Control correctness finding: a day with a real
    recorded run but no evening reflection (`insights`) and no equity
    snapshot (`daily_pnl`) must still be discoverable in `/journal/dates`
    — previously only `insights`/`daily_pnl` dates were unioned, so this
    exact day would have been silently invisible even though
    `/journal/{date}` already rendered it correctly once selected
    directly."""
    db_path = tmp_path / "quant_agent_test.db"
    db = Database(str(db_path))
    db.initialize()

    db.insert_agent_log(
        agent_name="portfolio_manager", run_id=RUN_ONLY_RUN_ID,
        input_summary="pm input", output_summary="stayed neutral",
        full_response='{"targets": []}', model="gpt-5.5", tokens_used=300,
        cost_usd=0.02,
    )
    # 15:00 UTC on RUN_ONLY_DATE is 11:00 ET the same calendar day (EDT,
    # UTC-4) — no day-boundary ambiguity for this regression test.
    fixed_ts = f"{RUN_ONLY_DATE} 15:00:00"
    db.conn.execute(
        "UPDATE agent_logs SET timestamp = ? WHERE run_id = ?",
        (fixed_ts, RUN_ONLY_RUN_ID),
    )
    db.conn.commit()
    db.close()
    monkeypatch.setattr(db_reads, "get_db_path", lambda: str(db_path))
    return db_path


def test_journal_dates_includes_a_day_with_only_a_run_no_insights_no_daily_pnl(
    client, run_only_day_db,
):
    r = client.get("/journal/dates")
    assert r.status_code == 200
    assert RUN_ONLY_DATE in r.json()["dates"]


LATE_UTC_RUN_ID = "intra_check-lateutc1"
MALFORMED_TS_RUN_ID = "run-malformedts"


@pytest.fixture
def late_utc_and_malformed_ts_db(tmp_path, monkeypatch):
    """Two edge cases for get_journal_dates' UTC->ET conversion:

    1. A UTC timestamp late enough in the evening that it belongs to the
       PRIOR ET calendar day: 2026-08-21 02:00:00 UTC is 2026-08-20 22:00
       EDT (UTC-4 in August) — the classic off-by-one a naive same-day
       string comparison would get wrong. Verified against real tzdata
       (not hand-computed) before writing this fixture.
    2. A malformed (non-parseable) agent_logs.timestamp value, which must
       be skipped rather than crash the whole /journal/dates read.
    """
    db_path = tmp_path / "quant_agent_test.db"
    db = Database(str(db_path))
    db.initialize()

    db.insert_agent_log(
        agent_name="portfolio_manager", run_id=LATE_UTC_RUN_ID,
        input_summary="i", output_summary="o", full_response="{}",
        model="m", tokens_used=1,
    )
    db.conn.execute(
        "UPDATE agent_logs SET timestamp = ? WHERE run_id = ?",
        ("2026-08-21 02:00:00", LATE_UTC_RUN_ID),
    )

    db.insert_agent_log(
        agent_name="portfolio_manager", run_id=MALFORMED_TS_RUN_ID,
        input_summary="i", output_summary="o", full_response="{}",
        model="m", tokens_used=1,
    )
    db.conn.execute(
        "UPDATE agent_logs SET timestamp = ? WHERE run_id = ?",
        ("not-a-real-timestamp", MALFORMED_TS_RUN_ID),
    )
    db.conn.commit()
    db.close()
    monkeypatch.setattr(db_reads, "get_db_path", lambda: str(db_path))
    return db_path


def test_journal_dates_converts_a_late_utc_timestamp_to_the_prior_et_day(
    client, late_utc_and_malformed_ts_db,
):
    r = client.get("/journal/dates")
    assert r.status_code == 200
    assert "2026-08-20" in r.json()["dates"]
    assert "2026-08-21" not in r.json()["dates"]


def test_journal_dates_skips_a_malformed_timestamp_without_500(
    client, late_utc_and_malformed_ts_db,
):
    """A malformed agent_logs.timestamp must degrade (skip that one run),
    never take down the whole /journal/dates read for every other day."""
    r = client.get("/journal/dates")
    assert r.status_code == 200
    # The other (well-formed) fixture row in this same DB still resolves.
    assert "2026-08-20" in r.json()["dates"]


def test_journal_day_for_run_only_date_already_renders_correctly(client, run_only_day_db):
    """The bug was discoverability (`/journal/dates`), not the day view
    itself — `/journal/{date}` already worked when the date was known."""
    r = client.get(f"/journal/{RUN_ONLY_DATE}")
    assert r.status_code == 200
    body = r.json()
    assert body["has_data"] is True
    assert len(body["runs"]) == 1
    assert body["runs"][0]["run_id"] == RUN_ONLY_RUN_ID
    assert body["daily_pnl"] is None
    assert body["reflection"] is None


def test_journal_day_assembles_full_day(client, seeded_journal_db):
    r = client.get(f"/journal/{DATE}")
    assert r.status_code == 200
    body = r.json()
    assert body["date"] == DATE
    assert body["has_data"] is True
    assert body["daily_pnl"]["daily_pnl"] == 500.0
    assert body["reflection"]["tomorrow_outlook"] == "cautiously bullish"
    assert len(body["runs"]) == 1
    assert body["runs"][0]["run_id"] == RUN_ID
    assert body["runs"][0]["decision_id"] == DECISION_ID
    assert len(body["trades"]) == 1
    assert body["trades"][0]["symbol"] == "AAPL"
    assert body["candidates"] == ["AAPL"]


def test_journal_day_404_for_date_with_no_data(client, seeded_journal_db):
    r = client.get("/journal/2020-01-01")
    assert r.status_code == 404


def test_journal_day_handles_malformed_date_without_500(client, seeded_journal_db):
    r = client.get("/journal/not-a-date")
    assert r.status_code == 404


def test_search_finds_trade_by_symbol(client, seeded_journal_db):
    r = client.get("/search", params={"q": "AAPL"})
    assert r.status_code == 200
    body = r.json()
    assert body["query"] == "AAPL"
    assert any(t["symbol"] == "AAPL" for t in body["trades"])


def test_search_finds_trade_by_reasoning_text(client, seeded_journal_db):
    r = client.get("/search", params={"q": "services growth"})
    assert r.status_code == 200
    assert len(r.json()["trades"]) == 1


def test_search_finds_agent_log_by_output_summary(client, seeded_journal_db):
    r = client.get("/search", params={"q": "Deployed into AAPL"})
    assert r.status_code == 200
    body = r.json()
    assert any(a["agent_name"] == "portfolio_manager" for a in body["agent_logs"])


def test_search_empty_query_returns_no_results(client, seeded_journal_db):
    r = client.get("/search", params={"q": ""})
    assert r.status_code == 200
    assert r.json() == {"query": "", "trades": [], "agent_logs": []}


def test_search_no_match_returns_empty_not_error(client, seeded_journal_db):
    r = client.get("/search", params={"q": "NOSUCHSYMBOLXYZ"})
    assert r.status_code == 200
    assert r.json()["trades"] == []
    assert r.json()["agent_logs"] == []


def test_search_literal_percent_does_not_wildcard_match_everything(client, seeded_journal_db):
    """A literal '%' in the search term must be escaped, not act as a
    SQL LIKE wildcard that matches every row — proves the endpoint can't
    be coaxed into an unbounded scan via LIKE metacharacters."""
    r = client.get("/search", params={"q": "%"})
    assert r.status_code == 200
    assert r.json()["trades"] == []
    assert r.json()["agent_logs"] == []
