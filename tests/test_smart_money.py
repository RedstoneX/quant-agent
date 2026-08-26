import json
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import Mock
from zoneinfo import ZoneInfo

from src.agents.base import AgentResult
from src.agents.smart_money_analyst import SmartMoneyAnalystAgent
from src.data.smart_money import SECForm4Provider
from src.models import SmartMoneyFinding, SmartMoneyObservation


ET = ZoneInfo("America/New_York")


def _congress(*, lag=40, age=0, actor="Example Member", direction="buy"):
    disclosed = date.today() - timedelta(days=age)
    return SmartMoneyObservation(
        symbol="NVDA", actor=actor, direction=direction,
        transaction_date=disclosed - timedelta(days=lag), disclosure_date=disclosed,
        source_url="https://example.test/filing", lag_days=lag,
        disclosure_age_days=age,
        freshness="stale" if age > 30 or lag > 30 else "fresh",
        economic_role="historical",
    )


def _insider(
    *, symbol="NVDA", owner="1", direction="buy", value=300_000,
    age=0, accession="0000000001-26-000001", row=0,
):
    disclosed = date.today() - timedelta(days=age)
    code = "P" if direction == "buy" else "S"
    return SmartMoneyObservation(
        symbol=symbol, stream="insider", actor=f"Owner {owner}", actor_cik=owner,
        actor_roles=["director"], direction=direction,
        transaction_date=disclosed - timedelta(days=2), disclosure_date=disclosed,
        accepted_at=datetime.combine(disclosed, datetime.min.time(), tzinfo=ET),
        source_url=f"https://www.sec.gov/{accession}.txt",
        accession_number=accession, filing_form="4", transaction_code=code,
        transaction_row=row, security_title="Common Stock", shares=value / 100,
        price_per_share=100, transaction_value_usd=value,
        post_transaction_shares=10_000, ownership_nature="direct",
        listed_exchange="Nasdaq", lag_days=2, disclosure_age_days=age,
        freshness="fresh" if age <= 7 else "delayed",
        economic_role="confirmatory",
    )


def _submission(*, code="P", acquired="A", form="4", symbol="NVDA"):
    return f"""<SEC-DOCUMENT>0000000001-26-000001.txt
<ACCEPTANCE-DATETIME>20260824173015
<DOCUMENT><TYPE>{form}<TEXT><XML><?xml version="1.0"?>
<ownershipDocument>
  <documentType>{form}</documentType>
  <issuer><issuerCik>0001045810</issuerCik><issuerTradingSymbol>{symbol}</issuerTradingSymbol></issuer>
  <reportingOwner>
    <reportingOwnerId><rptOwnerCik>0001234567</rptOwnerCik><rptOwnerName>Example Insider</rptOwnerName></reportingOwnerId>
    <reportingOwnerRelationship><isDirector>true</isDirector><isOfficer>true</isOfficer><isTenPercentOwner>false</isTenPercentOwner><isOther>false</isOther><officerTitle>CEO</officerTitle></reportingOwnerRelationship>
  </reportingOwner>
  <aff10b5One>0</aff10b5One>
  <nonDerivativeTable>
    <nonDerivativeTransaction>
      <securityTitle><value>Common Stock</value></securityTitle>
      <transactionDate><value>2026-08-22</value></transactionDate>
      <transactionCoding><transactionFormType>4</transactionFormType><transactionCode>{code}</transactionCode></transactionCoding>
      <transactionAmounts><transactionShares><value>2500</value></transactionShares><transactionPricePerShare><value>100.50</value></transactionPricePerShare><transactionAcquiredDisposedCode><value>{acquired}</value></transactionAcquiredDisposedCode></transactionAmounts>
      <postTransactionAmounts><sharesOwnedFollowingTransaction><value>10000</value></sharesOwnedFollowingTransaction></postTransactionAmounts>
      <ownershipNature><directOrIndirectOwnership><value>D</value></directOrIndirectOwnership></ownershipNature>
    </nonDerivativeTransaction>
    <nonDerivativeTransaction>
      <transactionDate><value>2026-08-22</value></transactionDate>
      <transactionCoding><transactionCode>A</transactionCode></transactionCoding>
      <transactionAmounts><transactionShares><value>999999</value></transactionShares><transactionPricePerShare><value>0</value></transactionPricePerShare><transactionAcquiredDisposedCode><value>A</value></transactionAcquiredDisposedCode></transactionAmounts>
    </nonDerivativeTransaction>
  </nonDerivativeTable>
</ownershipDocument></XML></TEXT></DOCUMENT>"""


def _write_rows(provider: SECForm4Provider, rows):
    provider.observations_path.write_text(json.dumps([
        row.model_dump(mode="json") for row in rows
    ]))


def test_congressional_compatibility_remains_conservative():
    finding = SmartMoneyFinding(
        symbol="NVDA", stance="bullish", economic_role="confirmatory",
        summary="one buy", why_now="recently disclosed",
        observations=[_congress(lag=2)],
    )
    assert finding.economic_role == "historical"
    assert finding.support_eligible is False

    cluster = SmartMoneyFinding(
        symbol="NVDA", stance="bullish", economic_role="confirmatory",
        summary="cluster", why_now="two disclosures",
        observations=[_congress(lag=2, actor="A"), _congress(lag=3, actor="B")],
    )
    assert cluster.support_eligible is True


def test_exact_non_derivative_purchase_parse_preserves_sec_facts(tmp_path):
    provider = SECForm4Provider(data_dir=str(tmp_path))
    rows = provider._parse_submission(
        _submission(),
        source_url=(
            "https://www.sec.gov/Archives/edgar/data/1045810/"
            "000000000126000001/0000000001-26-000001.txt"
        ),
        listed={"1045810": {"NVDA": "Nasdaq"}},
    )
    assert len(rows) == 1  # code A award was discarded before the LLM
    row = rows[0]
    assert row.transaction_code == "P" and row.direction == "buy"
    assert row.transaction_value_usd == 251_250
    assert row.accepted_at == datetime(2026, 8, 24, 17, 30, 15, tzinfo=ET)
    assert row.accession_number == "0000000001-26-000001"
    assert row.actor_cik == "1234567"
    assert row.actor_roles == ["director", "officer", "CEO"]
    assert row.post_transaction_shares == 10_000
    assert row.ownership_nature == "direct"
    assert row.is_10b5_1 is False


def test_direction_inconsistent_sec_row_is_dropped(tmp_path):
    provider = SECForm4Provider(data_dir=str(tmp_path))
    rows = provider._parse_submission(
        _submission(code="P", acquired="D"),
        source_url="https://www.sec.gov/x/0000000001-26-000001.txt",
        listed={"1045810": {"NVDA": "Nasdaq"}},
    )
    assert rows == []


def test_amendment_marker_and_sale_are_preserved(tmp_path):
    provider = SECForm4Provider(data_dir=str(tmp_path))
    rows = provider._parse_submission(
        _submission(code="S", acquired="D", form="4/A"),
        source_url="https://www.sec.gov/x/0000000001-26-000001.txt",
        listed={"1045810": {"NVDA": "Nasdaq"}},
    )
    assert rows[0].amendment is True
    assert rows[0].direction == "sell"


def test_amendment_cannot_independently_clear_materiality_or_admission(tmp_path):
    provider = SECForm4Provider(data_dir=str(tmp_path))
    amended = _insider(symbol="XYZ", value=500_000)
    amended.amendment = True
    amended.filing_form = "4/A"
    _write_rows(provider, [amended])

    assert provider.fetch([]) == ([], None)


def test_fetch_is_broad_and_only_large_external_purchase_gets_admission(tmp_path):
    provider = SECForm4Provider(data_dir=str(tmp_path))
    outside_buy = _insider(symbol="XYZ", direction="buy", value=300_000)
    outside_sell = _insider(
        symbol="SELL", direction="sell", value=500_000,
        accession="0000000002-26-000001",
    )
    core_buy = _insider(
        symbol="NVDA", direction="buy", value=150_000,
        accession="0000000003-26-000001",
    )
    _write_rows(provider, [outside_buy, outside_sell, core_buy])

    rows, error = provider.fetch(["NVDA"])
    assert error is None
    assert {row.symbol for row in rows} == {"XYZ", "SELL", "NVDA"}
    by_symbol = {row.symbol: row for row in rows}
    assert by_symbol["XYZ"].transient_admission_eligible is True
    assert by_symbol["SELL"].transient_admission_eligible is False
    assert by_symbol["NVDA"].in_core_universe is True


def test_quiet_immaterial_cache_returns_no_observations(tmp_path):
    provider = SECForm4Provider(data_dir=str(tmp_path))
    _write_rows(provider, [_insider(value=99_999)])
    assert provider.fetch(["NVDA"]) == ([], None)


def test_independent_owner_cluster_survives_but_repeat_owner_does_not(tmp_path):
    provider = SECForm4Provider(data_dir=str(tmp_path))
    independent = [
        _insider(owner="1", value=60_000, accession="0000000001-26-000001"),
        _insider(owner="2", value=60_000, accession="0000000002-26-000001"),
    ]
    _write_rows(provider, independent)
    rows, _ = provider.fetch(["NVDA"])
    assert len(rows) == 2

    repeated = [
        _insider(owner="1", value=60_000, accession="0000000001-26-000001"),
        _insider(owner="1", value=60_000, accession="0000000002-26-000001"),
    ]
    _write_rows(provider, repeated)
    rows, _ = provider.fetch(["NVDA"])
    assert rows == []


def test_refresh_deduplicates_accession_and_uses_descriptive_header(tmp_path, monkeypatch):
    provider = SECForm4Provider(data_dir=str(tmp_path), max_filings_per_refresh=5)
    listed = {"1045810": {"NVDA": "Nasdaq"}}
    monkeypatch.setattr(provider, "_listed_map", lambda _deadline: listed)
    monkeypatch.setattr(provider, "_discover", lambda *_: [{
        "accession": "0000000001-26-000001", "form": "4", "cik": "1045810",
    }])
    response = Mock(status_code=200, content=_submission().encode())
    response.raise_for_status.return_value = None
    provider.session.get = Mock(return_value=response)

    assert provider.refresh()["new_observations"] == 1
    assert provider.refresh()["new_observations"] == 0
    assert provider.session.get.call_count == 1
    assert "QAMC/1.0" in provider.session.get.call_args.kwargs["headers"]["User-Agent"]


def test_analyst_rejects_direction_incompatible_stance(tmp_path):
    analyst = object.__new__(SmartMoneyAnalystAgent)
    analyst.synthesis_cache_path = tmp_path / "cache.json"
    analyst.run = lambda **_: AgentResult(
        '{"findings":[{"symbol":"NVDA","stance":"bearish",'
        '"economic_role":"actionable","summary":"wrong",'
        '"why_now":"new filing"}]}', 1, "test",
    )
    findings, _, error = analyst.analyze([_insider()])
    assert findings == []
    assert error == "analysis_schema_error"


def test_analyst_unchanged_evidence_uses_zero_token_cache(tmp_path):
    analyst = object.__new__(SmartMoneyAnalystAgent)
    analyst.model = "test"
    analyst.synthesis_cache_path = tmp_path / "cache.json"
    calls = []

    def run(**_):
        calls.append(1)
        return AgentResult(
            '{"findings":[{"symbol":"NVDA","stance":"bullish",'
            '"economic_role":"actionable","summary":"large purchase",'
            '"why_now":"accepted today"}]}', 10, "test",
        )

    analyst.run = run
    first, first_result, first_error = analyst.analyze([_insider()])
    second, second_result, second_error = analyst.analyze([_insider()])
    assert first_error is None and second_error is None
    assert first[0].stance == second[0].stance == "bullish"
    assert len(calls) == 1
    assert first_result.tokens_used == 10
    assert second_result.tokens_used == 0
    assert second_result.provider_requests == 0


def test_analyst_cache_key_ignores_age_and_run_membership(tmp_path):
    analyst = object.__new__(SmartMoneyAnalystAgent)
    analyst.model = "test"
    analyst.synthesis_cache_path = tmp_path / "cache.json"
    calls = []

    def run(**_):
        calls.append(1)
        return AgentResult('{"findings":[]}', 10, "test")

    analyst.run = run
    first = _insider()
    analyst.analyze([first])
    changed_context = first.model_copy(update={
        "disclosure_age_days": first.disclosure_age_days + 1,
        "in_trading_universe": True,
        "transient_admitted": True,
    })
    _, cached_result, error = analyst.analyze([changed_context])

    assert error is None
    assert calls == [1]
    assert cached_result.tokens_used == 0


def test_analyst_distinguishes_valid_empty_from_parse_failure(tmp_path):
    analyst = object.__new__(SmartMoneyAnalystAgent)
    analyst.synthesis_cache_path = tmp_path / "cache.json"
    analyst.run = lambda **_: AgentResult('{"findings":[]}', 1, "test")
    findings, _, error = analyst.analyze([_insider()])
    assert findings == [] and error is None

    analyst.synthesis_cache_path = tmp_path / "other.json"
    analyst.run = lambda **_: AgentResult("not json", 1, "test")
    findings, _, error = analyst.analyze([_insider()])
    assert findings == [] and error == "analysis_parse_error"


def _compact_payload(message: str) -> dict:
    return json.loads(message.split("\n", 1)[1])


def test_analyst_compacts_and_ranks_production_shaped_observations():
    observations = []
    for index in range(40):
        observation = _insider(
            symbol=f"S{index:02d}",
            owner=f"owner-{index}",
            value=(index + 1) * 10_000,
            age=index % 8,
            accession=f"{index + 1:010d}-26-000001",
        )
        observation.actor = f"Verbose reporting owner {index} " + ("x" * 200)
        observation.source_url = "https://www.sec.gov/Archives/" + ("y" * 300)
        observations.append(observation)

    # Actual admission wins over merely eligible, higher-value symbols;
    # remaining symbols rank by materiality. Reversing input must not change
    # the compact request.
    for observation in observations:
        observation.transient_admission_eligible = True
        observation.admission_eligible = True
    observations[0].transient_admitted = True
    analyst = object.__new__(SmartMoneyAnalystAgent)
    message = analyst.build_user_message(observations=observations)
    reversed_message = analyst.build_user_message(observations=list(reversed(observations)))
    payload = _compact_payload(message)

    assert message == reversed_message
    assert payload["input_observation_count"] == 40
    assert payload["input_symbol_count"] == 40
    assert payload["presented_symbol_count"] == 8
    assert payload["omitted_symbol_count"] == 32
    assert [row["symbol"] for row in payload["symbol_facts"]] == [
        "S00", "S39", "S38", "S37", "S36", "S35", "S34", "S33",
    ]
    assert all(
        len(row["representative_transactions"]) <= 3
        for row in payload["symbol_facts"]
    )
    assert all(
        len(row["representative_transactions"][0]["actor"]) <= 96
        for row in payload["symbol_facts"]
    )
    assert "https://www.sec.gov/Archives/" not in message
    assert len(message) < 15_000


def test_analyst_compaction_does_not_replace_canonical_finding_evidence(tmp_path):
    observations = [
        _insider(
            owner=str(index),
            value=100_000 + index,
            accession=f"{index + 1:010d}-26-000001",
            row=index,
        )
        for index in range(5)
    ]
    analyst = object.__new__(SmartMoneyAnalystAgent)
    analyst.synthesis_cache_path = tmp_path / "cache.json"
    captured = {}

    def run(**kwargs):
        captured["message"] = analyst.build_user_message(**kwargs)
        return AgentResult(
            '{"findings":[{"symbol":"NVDA","stance":"bullish",'
            '"economic_role":"confirmatory","summary":"owner cluster",'
            '"why_now":"recent accepted filings"}]}',
            10,
            "test",
        )

    analyst.run = run
    findings, _, error = analyst.analyze(observations)
    compact = _compact_payload(captured["message"])

    assert error is None
    assert len(compact["symbol_facts"][0]["representative_transactions"]) == 3
    assert len(findings[0].observations) == 5
    assert {
        row.accession_number for row in findings[0].observations
    } == {row.accession_number for row in observations}


def test_analyst_enforces_presented_unique_symbol_boundary(tmp_path):
    observations = [
        _insider(
            symbol=f"S{index:02d}",
            owner=f"owner-{index}",
            value=(index + 1) * 10_000,
            accession=f"{index + 1:010d}-26-000001",
        )
        for index in range(10)
    ]
    raw_findings = [{
        "symbol": observation.symbol,
        "stance": "bullish",
        "economic_role": "actionable",
        "summary": "purchase",
        "why_now": "recent filing",
    } for observation in observations]
    raw_findings.append(dict(raw_findings[-1]))
    analyst = object.__new__(SmartMoneyAnalystAgent)
    analyst.synthesis_cache_path = tmp_path / "cache.json"
    analyst.run = lambda **_: AgentResult(
        json.dumps({"findings": raw_findings}), 10, "test",
    )

    findings, _, error = analyst.analyze(observations)

    assert error == "analysis_partial_schema_error"
    assert len(findings) == 8
    assert len({finding.symbol for finding in findings}) == 8
    assert {finding.symbol for finding in findings} == {
        row["symbol"]
        for row in _compact_payload(
            analyst.build_user_message(observations=observations)
        )["symbol_facts"]
    }


def test_analyst_cache_is_bound_to_run_scoped_presented_symbols(tmp_path):
    observations = [
        _insider(
            symbol=f"S{index:02d}",
            owner=f"owner-{index}",
            value=(index + 1) * 10_000,
            accession=f"{index + 1:010d}-26-000001",
        ).model_copy(update={
            "transient_admission_eligible": True,
            "admission_eligible": True,
            "transient_admitted": index == 0,
        })
        for index in range(9)
    ]
    analyst = object.__new__(SmartMoneyAnalystAgent)
    analyst.model = "test"
    analyst.synthesis_cache_path = tmp_path / "cache.json"
    calls = []

    def run(**_):
        calls.append(1)
        return AgentResult('{"findings":[]}', 10, "test")

    analyst.run = run
    analyst.analyze(observations)
    changed_admission = [
        observation.model_copy(update={"transient_admitted": index == 1})
        for index, observation in enumerate(observations)
    ]
    _, result, error = analyst.analyze(changed_admission)

    assert error is None
    assert calls == [1, 1]
    assert result.tokens_used == 10
