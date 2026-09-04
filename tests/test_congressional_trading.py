import inspect
import json
import logging
from datetime import date, datetime, timedelta
from unittest.mock import Mock

import pytest

from src.config import SmartMoneyConfig
from src.data.congressional_trading import (
    _UNRECOGNIZED_TRANSACTION_TYPES,
    CombinedSmartMoneyProvider,
    CongressionalTradingProvider,
    _direction,
)
from src.data.smart_money import SECForm4Provider
from src.models import SmartMoneyFinding, SmartMoneyObservation


TODAY = date.today()


def _kadoa_row(
    *, ticker="NVDA", filer="Kevin Hern", filer_id="house_kevin_hern",
    transaction_type="Purchase", low=15001, high=50000, label="$15,001 - $50,000",
    transaction_date=None, filing_date=None, chamber="house",
):
    transaction_date = transaction_date or (TODAY - timedelta(days=10))
    filing_date = filing_date or (TODAY - timedelta(days=3))
    return {
        "ticker": ticker,
        "filer_name": filer,
        "filer_id": filer_id,
        "transaction_type": transaction_type,
        "amount_range_low": low,
        "amount_range_high": high,
        "amount_range_label": label,
        "transaction_date": transaction_date.isoformat(),
        "filing_date": filing_date.isoformat(),
        "doc_url": "https://disclosures-clerk.house.gov/public_disc/ptr-pdfs/2026/x.pdf",
        "chamber": chamber,
    }


def _congresswatch_row(
    *, ticker="NVDA", member="Kevin Hern", bioguide="H001082",
    txn_type="Purchase", amount="$15,001 - $50,000",
    transaction_date=None, chamber="House",
):
    transaction_date = transaction_date or (TODAY - timedelta(days=10))
    return {
        "transaction_date": transaction_date.isoformat(),
        "owner": "Self",
        "ticker": ticker,
        "asset_description": f"{ticker} Common Stock",
        "asset_type": "Stock",
        "type": txn_type,
        "amount": amount,
        "comment": "--",
        "ptr_link": "https://disclosures-clerk.house.gov/public_disc/ptr-pdfs/2026/y.pdf",
        "bioguide_id": bioguide,
        "member_name": member,
        "party": "Republican",
        "state": "Oklahoma",
        "chamber": chamber,
    }


def _mock_session(kadoa_payload, congresswatch_payload):
    session = Mock()

    def get(url, **_kwargs):
        response = Mock(status_code=200)
        response.raise_for_status.return_value = None
        if "kadoa" in url or "congress-trading-monitor" in url or "raw.githubusercontent" in url:
            response.json.return_value = kadoa_payload
        else:
            response.json.return_value = congresswatch_payload
        return response

    session.get = Mock(side_effect=get)
    return session


def test_fetch_from_each_source_successfully(tmp_path):
    kadoa = [_kadoa_row()]
    congresswatch = [_congresswatch_row()]
    session = _mock_session(kadoa, congresswatch)
    provider = CongressionalTradingProvider(
        data_dir=str(tmp_path), session=session,
        min_transaction_value_usd=1,
        external_min_transaction_value_usd=1,
    )

    result = provider.refresh()

    assert result["status"] == "ok"
    assert result["kadoa_raw_count"] == 1
    assert result["congresswatch_raw_count"] == 1
    assert result["merged_count"] == 1  # deduped: same trade in both feeds
    assert result["cached_observations"] == 1

    rows, error = provider.fetch(["NVDA"])
    assert error is None
    assert len(rows) == 1
    row = rows[0]
    assert row.symbol == "NVDA"
    assert row.stream == "congressional"
    assert row.direction == "buy"
    assert row.actor == "Kevin Hern"
    assert row.cross_source_agreement == "agreement"


def test_kadoa_failure_leaves_pipeline_running_on_congresswatch_alone(tmp_path):
    session = Mock()

    def get(url, **_kwargs):
        if "congresswatch" in url:
            response = Mock(status_code=200)
            response.raise_for_status.return_value = None
            response.json.return_value = [_congresswatch_row()]
            return response
        raise TimeoutError("kadoa is down")

    session.get = Mock(side_effect=get)
    provider = CongressionalTradingProvider(
        data_dir=str(tmp_path), session=session,
        min_transaction_value_usd=1,
        external_min_transaction_value_usd=1,
    )

    result = provider.refresh()

    assert result["status"] == "partial"
    assert "kadoa" in result["error"]
    assert result["cached_observations"] == 1
    rows, error = provider.fetch(["NVDA"])
    assert len(rows) == 1
    assert rows[0].cross_source_agreement == "single_source"


def test_congresswatch_failure_leaves_pipeline_running_on_kadoa_alone(tmp_path):
    session = Mock()

    def get(url, **_kwargs):
        if "congresswatch" in url:
            raise TimeoutError("congresswatch is down")
        response = Mock(status_code=200)
        response.raise_for_status.return_value = None
        response.json.return_value = [_kadoa_row()]
        return response

    session.get = Mock(side_effect=get)
    provider = CongressionalTradingProvider(
        data_dir=str(tmp_path), session=session,
        min_transaction_value_usd=1,
        external_min_transaction_value_usd=1,
    )

    result = provider.refresh()

    assert result["status"] == "partial"
    assert "congresswatch" in result["error"]
    rows, error = provider.fetch(["NVDA"])
    assert len(rows) == 1
    assert rows[0].cross_source_agreement == "single_source"


def test_both_sources_down_never_raises_and_yields_empty(tmp_path):
    session = Mock()
    session.get = Mock(side_effect=ConnectionError("no network"))
    provider = CongressionalTradingProvider(data_dir=str(tmp_path), session=session)

    result = provider.refresh()  # must not raise

    assert result["status"] == "provider_error"
    assert result["cached_observations"] == 0
    rows, error = provider.fetch(["NVDA"])
    assert rows == []


def test_malformed_payload_degrades_gracefully(tmp_path):
    session = Mock()

    def get(url, **_kwargs):
        response = Mock(status_code=200)
        response.raise_for_status.return_value = None
        response.json.return_value = {"unexpected": "shape"}
        return response

    session.get = Mock(side_effect=get)
    provider = CongressionalTradingProvider(data_dir=str(tmp_path), session=session)

    result = provider.refresh()  # must not raise despite bad shape

    assert result["cached_observations"] == 0
    rows, error = provider.fetch(["NVDA"])
    assert rows == []


def test_deduplication_merges_same_real_trade_across_both_sources(tmp_path):
    kadoa = [_kadoa_row(filer="Kevin Hern")]
    congresswatch = [_congresswatch_row(member="Rep. Kevin Hern")]  # naming variant
    session = _mock_session(kadoa, congresswatch)
    provider = CongressionalTradingProvider(
        data_dir=str(tmp_path), session=session,
        min_transaction_value_usd=1, external_min_transaction_value_usd=1,
    )
    provider.refresh()
    cached = json.loads(provider.observations_path.read_text())
    assert len(cached) == 1  # one merged row, not two duplicates
    assert cached[0]["cross_source_agreement"] == "agreement"


def test_disagreement_is_flagged_not_hidden(tmp_path):
    kadoa = [_kadoa_row(transaction_type="Purchase", low=15001, high=50000, label="$15,001 - $50,000")]
    congresswatch = [_congresswatch_row(txn_type="Sale (Full)", amount="$1,000,001 - $5,000,000")]
    session = _mock_session(kadoa, congresswatch)
    provider = CongressionalTradingProvider(
        data_dir=str(tmp_path), session=session,
        min_transaction_value_usd=1, external_min_transaction_value_usd=1,
    )
    result = provider.refresh()

    assert result["discrepancy_count"] == 1
    rows, _ = provider.fetch(["NVDA"])
    assert len(rows) == 1
    row = rows[0]
    assert row.cross_source_agreement == "discrepancy"
    assert "direction disagreement" in row.cross_source_note
    assert "amount bracket disagreement" in row.cross_source_note
    # Disagreement is surfaced, never silently resolved by dropping the row.
    assert row.direction == "buy"  # kadoa kept as canonical, but flagged


def test_congresswatch_future_dated_record_is_dropped_not_repaired(tmp_path):
    """congresswatch.us has at least one observed record with a
    transaction dated months in the future; it must be rejected outright,
    never silently clamped to today (which would fabricate a fake trade
    date)."""
    future = TODAY + timedelta(days=90)
    congresswatch = [_congresswatch_row(transaction_date=future, ticker="SONY")]
    session = _mock_session([], congresswatch)
    provider = CongressionalTradingProvider(data_dir=str(tmp_path), session=session)

    result = provider.refresh()

    assert result["merged_count"] == 0
    rows, _ = provider.fetch(["SONY"])
    assert rows == []


def test_implausibly_old_date_is_dropped(tmp_path):
    ancient = TODAY - timedelta(days=365 * 30)
    kadoa = [_kadoa_row(transaction_date=ancient, filing_date=ancient, ticker="OLD")]
    session = _mock_session(kadoa, [])
    provider = CongressionalTradingProvider(data_dir=str(tmp_path), session=session)

    provider.refresh()
    rows, _ = provider.fetch(["OLD"])
    assert rows == []


def test_congresswatch_only_trade_gets_conservative_disclosure_estimate(tmp_path):
    """No filing-date field exists on congresswatch's live schema; the
    disclosure date must be estimated at the documented 45-day statutory
    ceiling, never assumed to have just been disclosed."""
    txn_date = TODAY - timedelta(days=40)
    congresswatch = [_congresswatch_row(transaction_date=txn_date)]
    session = _mock_session([], congresswatch)
    provider = CongressionalTradingProvider(
        data_dir=str(tmp_path), session=session,
        min_transaction_value_usd=1, external_min_transaction_value_usd=1,
        lookback_days=45,
    )
    provider.refresh()
    cached = json.loads(provider.observations_path.read_text())
    assert len(cached) == 1
    estimated_disclosure = date.fromisoformat(cached[0]["disclosure_date"])
    assert estimated_disclosure == min(TODAY, txn_date + timedelta(days=45))


def test_congressional_never_admits_or_grows_universe(tmp_path):
    kadoa = [_kadoa_row(ticker="XYZ", low=10_000_000, high=25_000_000, label="$10,000,000+")]
    session = _mock_session(kadoa, [])
    provider = CongressionalTradingProvider(
        data_dir=str(tmp_path), session=session,
        min_transaction_value_usd=1, external_min_transaction_value_usd=1,
    )
    provider.refresh()
    # XYZ is not in the passed-in universe/symbols list.
    rows, _ = provider.fetch(["NVDA"])
    assert rows == []


def test_cluster_window_matches_sec_form4_two_day_window(tmp_path):
    """Congressional data reuses the exact same 2-day cluster window as SEC
    Form 4 — not a separately tuned constant."""
    provider = CongressionalTradingProvider(data_dir=str(tmp_path))
    assert provider.cluster_window_days == 2


def test_combined_provider_isolates_one_subprovider_failure(tmp_path):
    working = Mock()
    working.refresh.return_value = {"status": "ok", "error": None}
    working.fetch.return_value = ([], None)

    broken = Mock()
    broken.refresh.side_effect = RuntimeError("boom")
    broken.fetch.side_effect = RuntimeError("boom")

    combined = CombinedSmartMoneyProvider([working, broken])

    refresh_result = combined.refresh()  # must not raise
    rows, error = combined.fetch(["NVDA"])  # must not raise

    assert refresh_result["status"] == "partial"
    assert rows == []
    assert error is not None and "RuntimeError" in error


def test_combined_provider_concatenates_sec_and_congressional_observations(tmp_path):
    sec_provider = SECForm4Provider(data_dir=str(tmp_path / "sec"))
    congress = CongressionalTradingProvider(
        data_dir=str(tmp_path / "congress"),
        session=_mock_session([_kadoa_row()], []),
        min_transaction_value_usd=1, external_min_transaction_value_usd=1,
    )
    congress.refresh()
    combined = CombinedSmartMoneyProvider([sec_provider, congress])

    rows, error = combined.fetch(["NVDA"])

    assert error is None
    assert len(rows) == 1
    assert rows[0].stream == "congressional"


# ---------------------------------------------------------------------------
# Lookback window (bug 1) — the shipped 30-day default could not even cover
# the STOCK Act's own 45-day filing deadline, so late-but-legitimate real
# disclosures were silently discarded before the analyst ever saw them.
# ---------------------------------------------------------------------------

def test_congress_lookback_default_covers_the_real_stock_act_disclosure_lag():
    """180 days, not 30. The statutory deadline alone is 45 days and filers
    routinely file at or past it."""
    cfg = SmartMoneyConfig()
    assert cfg.congress_lookback_days == 180
    assert cfg.congress_lookback_days > cfg.congress_assumed_max_disclosure_lag_days
    # The provider's own default must not silently disagree with config.
    assert (
        inspect.signature(CongressionalTradingProvider.__init__)
        .parameters["lookback_days"].default == 180
    )


def test_widening_congress_window_does_not_touch_the_sec_form4_window():
    """Form 4 has a ~2-business-day legal deadline, not 45 — its much tighter
    window is deliberate and must NOT be harmonised with the congressional
    one."""
    cfg = SmartMoneyConfig()
    assert cfg.lookback_days == 7
    assert cfg.cluster_window_days == 2
    assert (
        inspect.signature(SECForm4Provider.__init__)
        .parameters["lookback_days"].default == 14
    )
    assert cfg.lookback_days < cfg.congress_lookback_days


def test_a_disclosure_older_than_the_old_30_day_window_now_survives(tmp_path):
    """The regression this fixes: a real trade disclosed 60 days ago was
    dropped outright under the old default."""
    txn = TODAY - timedelta(days=75)
    filed = TODAY - timedelta(days=60)
    provider = CongressionalTradingProvider(
        data_dir=str(tmp_path / "c"),
        session=_mock_session(
            [_kadoa_row(transaction_date=txn, filing_date=filed)], [],
        ),
        min_transaction_value_usd=1, external_min_transaction_value_usd=1,
    )
    provider.refresh()

    rows, _ = provider.fetch(["NVDA"])
    assert len(rows) == 1
    assert rows[0].disclosure_age_days == 60

    narrow = CongressionalTradingProvider(
        data_dir=str(tmp_path / "c"),
        session=_mock_session([], []),
        lookback_days=30,
        min_transaction_value_usd=1, external_min_transaction_value_usd=1,
    )
    assert narrow.fetch(["NVDA"])[0] == []


def test_wider_window_does_not_make_stale_congressional_evidence_load_bearing():
    """A 180-day COVERAGE window must not weaken the separate <=7-day
    eligibility contract in models.py."""
    old = [
        SmartMoneyObservation(
            symbol="NVDA", stream="congressional", actor=actor, actor_cik="",
            direction="buy", amount_range="$15,001 - $50,000",
            transaction_date=TODAY - timedelta(days=100),
            disclosure_date=TODAY - timedelta(days=90),
            known_at=datetime.combine(TODAY - timedelta(days=90), datetime.min.time()),
            source_url="https://example.invalid/x",
            transaction_value_usd=15001.0,
            in_core_universe=True, in_trading_universe=True,
            admission_eligible=False, transient_admission_eligible=False,
            lag_days=10, disclosure_age_days=90, freshness="delayed",
            economic_role="confirmatory",
        )
        for actor in ("Kevin Hern", "Jane Doe")
    ]
    finding = SmartMoneyFinding(
        symbol="NVDA", stance="bullish", economic_role="confirmatory",
        summary="Two members bought, disclosed 90 days ago.",
        why_now="Within the 180-day coverage window but far outside the "
                "7-day eligibility contract.",
        observations=old,
    )
    assert finding.support_eligible is False


# ---------------------------------------------------------------------------
# Direction parsing (bug 2) — House PTR forms carry SHORT CODES (P/S/E), not
# only full words. Those fell through to "unknown" and were lost.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    # Full words (both feeds' web-rendered form).
    ("Purchase", "buy"),
    ("purchase", "buy"),
    ("Sale", "sell"),
    ("Sale (Full)", "sell"),
    ("Sale (Partial)", "sell"),
    ("sale_full", "sell"),
    ("sale_partial", "sell"),
    ("Exchange", "exchange"),
    # House PTR form short codes — the bug.
    ("P", "buy"),
    ("p", "buy"),
    ("S", "sell"),
    ("S (partial)", "sell"),
    ("S (Partial)", "sell"),
    ("S  (partial)", "sell"),
    ("S (full)", "sell"),
    ("E", "exchange"),
    ("e", "exchange"),
])
def test_direction_handles_full_words_and_short_codes(raw, expected):
    assert _direction(raw) == expected


@pytest.mark.parametrize("raw", ["Stock Split", "Stock Dividend", "Split"])
def test_short_code_support_does_not_swallow_unrelated_s_words(raw):
    """A loose single-letter prefix test would read these as sales."""
    assert _direction(raw) == "unknown"


@pytest.mark.parametrize("raw", ["", None, "   "])
def test_direction_treats_an_empty_transaction_type_as_unknown_without_noise(raw):
    _UNRECOGNIZED_TRANSACTION_TYPES.clear()
    assert _direction(raw) == "unknown"
    assert _UNRECOGNIZED_TRANSACTION_TYPES == set()


def test_a_genuinely_unrecognized_transaction_type_is_flagged_not_silently_lost(caplog):
    _UNRECOGNIZED_TRANSACTION_TYPES.clear()
    with caplog.at_level(logging.WARNING, logger="src.data.congressional_trading"):
        assert _direction("Reinvestment Of Dividends Via DRIP") == "unknown"

    assert "Reinvestment Of Dividends Via DRIP" in _UNRECOGNIZED_TRANSACTION_TYPES
    assert any(
        "unrecognized transaction_type" in r.getMessage() for r in caplog.records
    ), "an unknown transaction type must leave a trace, not vanish"


def test_repeated_unknown_values_warn_once_per_distinct_value(caplog):
    _UNRECOGNIZED_TRANSACTION_TYPES.clear()
    with caplog.at_level(logging.WARNING, logger="src.data.congressional_trading"):
        for _ in range(5):
            _direction("Totally Novel Type")
    warnings = [
        r for r in caplog.records
        if "unrecognized transaction_type" in r.getMessage()
    ]
    assert len(warnings) == 1


def test_a_short_code_row_survives_end_to_end_instead_of_being_dropped(tmp_path):
    """Before the fix a real 'P' row parsed as direction='unknown'."""
    provider = CongressionalTradingProvider(
        data_dir=str(tmp_path / "c"),
        session=_mock_session(
            [_kadoa_row(transaction_type="P")],
            [_congresswatch_row(txn_type="P")],
        ),
        min_transaction_value_usd=1, external_min_transaction_value_usd=1,
    )
    provider.refresh()

    rows, error = provider.fetch(["NVDA"])
    assert error is None
    assert len(rows) == 1
    assert rows[0].direction == "buy"
    assert rows[0].cross_source_agreement == "agreement"


def test_short_code_and_full_word_across_the_two_feeds_agree_not_disagree(tmp_path):
    """kadoa may render 'Sale' while congresswatch renders 'S' for the same
    real trade — that must NOT be flagged as a cross-source discrepancy."""
    provider = CongressionalTradingProvider(
        data_dir=str(tmp_path / "c"),
        session=_mock_session(
            [_kadoa_row(transaction_type="Sale")],
            [_congresswatch_row(txn_type="S (partial)")],
        ),
        min_transaction_value_usd=1, external_min_transaction_value_usd=1,
    )
    provider.refresh()

    rows, _ = provider.fetch(["NVDA"])
    assert len(rows) == 1
    assert rows[0].direction == "sell"
    assert "direction disagreement" not in (rows[0].cross_source_note or "")
