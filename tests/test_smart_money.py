from datetime import date, timedelta
from unittest.mock import Mock, patch

from src.data.smart_money import BargoCongressProvider
from src.models import SmartMoneyFinding, SmartMoneyObservation
from src.agents.base import AgentResult
from src.agents.smart_money_analyst import SmartMoneyAnalystAgent


def _observation(*, lag=40, age=0, actor="Example Member", direction="buy"):
    disclosed = date.today() - timedelta(days=age)
    return SmartMoneyObservation(
        symbol="NVDA", actor=actor, direction=direction,
        transaction_date=disclosed - timedelta(days=lag), disclosure_date=disclosed,
        source_url="https://example.test/filing", lag_days=lag, disclosure_age_days=age,
        freshness="stale" if age > 30 or lag > 30 else "fresh",
        economic_role="historical",
    )


def test_lone_congressional_observation_is_forced_historical():
    finding = SmartMoneyFinding(
        symbol="NVDA", stance="bullish", economic_role="confirmatory",
        summary="one buy", why_now="recently disclosed", observations=[_observation(lag=2)],
    )
    assert finding.economic_role == "historical"
    assert finding.support_eligible is False


def test_years_old_promptly_disclosed_cluster_is_historical():
    finding = SmartMoneyFinding(
        symbol="NVDA", stance="bullish", economic_role="confirmatory",
        summary="cluster", why_now="archive", observations=[
            _observation(lag=2, age=800, actor="A"),
            _observation(lag=3, age=799, actor="B"),
        ],
    )
    assert finding.economic_role == "historical"
    assert not finding.support_eligible


def test_recent_independent_directional_cluster_can_be_confirmatory():
    finding = SmartMoneyFinding(
        symbol="NVDA", stance="bullish", economic_role="confirmatory",
        summary="independent cluster", why_now="two new disclosures", observations=[
            _observation(lag=2, age=1, actor="A"),
            _observation(lag=3, age=2, actor="B"),
        ],
    )
    assert finding.economic_role == "confirmatory"
    assert finding.support_eligible


def test_provider_preserves_transaction_disclosure_lag_and_source():
    response = Mock()
    response.raise_for_status.return_value = None
    response.json.return_value = {"trades": [{
        "member": "Example Member", "type": "purchase", "amount_range": "$1-$15k",
        "transaction_date": "2026-06-01", "disclosure_date": "2026-07-11",
        "filing_url": "https://example.test/filing",
    }]}
    with patch("src.data.smart_money.requests.get", return_value=response):
        rows, error = BargoCongressProvider(base_url="https://api.test").fetch(["nvda"])
    assert error is None
    assert rows[0].lag_days == 40
    assert rows[0].freshness == "stale"
    assert rows[0].source_url == "https://example.test/filing"


def test_provider_failure_is_truthful_and_fail_soft():
    with patch("src.data.smart_money.requests.get", side_effect=TimeoutError("down")):
        rows, error = BargoCongressProvider(base_url="https://api.test").fetch(["NVDA"])
    assert rows == []
    assert error == "provider_error:NVDA:TimeoutError"


def test_provider_retains_success_when_later_symbol_fails():
    good = Mock()
    good.raise_for_status.return_value = None
    good.json.return_value = {"trades": [{
        "member": "A", "type": "purchase", "transaction_date": str(date.today()),
        "disclosure_date": str(date.today()), "official_url": "https://example.test/a",
    }]}
    with patch("src.data.smart_money.requests.get", side_effect=[good, TimeoutError("down")]):
        rows, error = BargoCongressProvider(base_url="https://api.test").fetch(["AAPL", "BRK-B"])
    assert [row.symbol for row in rows] == ["AAPL"]
    assert error == "provider_partial_error:BRK-B:TimeoutError"


def test_optional_key_is_sent_without_appearing_in_error():
    response = Mock()
    response.raise_for_status.side_effect = RuntimeError("forbidden")
    with patch("src.data.smart_money.requests.get", return_value=response) as get:
        _, error = BargoCongressProvider(base_url="https://api.test", api_key="secret-key").fetch(["NVDA"])
    assert get.call_args.kwargs["headers"]["X-Api-Key"] == "secret-key"
    assert "secret-key" not in error


def test_analyst_distinguishes_valid_empty_from_parse_failure():
    analyst = object.__new__(SmartMoneyAnalystAgent)
    analyst.run = lambda **_: AgentResult('{"findings":[]}', 1, "test")
    findings, _, error = analyst.analyze([_observation()])
    assert findings == [] and error is None

    analyst.run = lambda **_: AgentResult('not json', 1, "test")
    findings, _, error = analyst.analyze([_observation()])
    assert findings == [] and error == "analysis_parse_error"


def test_analyst_reports_schema_failure_not_empty():
    analyst = object.__new__(SmartMoneyAnalystAgent)
    analyst.run = lambda **_: AgentResult('{"findings":[{"symbol":"NVDA"}]}', 1, "test")
    findings, _, error = analyst.analyze([_observation()])
    assert findings == [] and error == "analysis_schema_error"
