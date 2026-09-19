"""One garbled row in the technical seat's answer must cost that row only.

The two fixtures are the seat's real answers from production, copied verbatim
out of `agent_logs.full_response` (row 456, run `intra_check-26f52bf2`,
2026-09-17 14:31), split at the desk's own `--- retry ---` marker:

  - first answer: five rows; SQQQ carries a bare `n/a` line;
  - retry answer: four rows; SQQQ carries `s"thesis_invalid_if"`.

The shared fragment parser kept ONE row of each (ZS, then MTZ), so ORCL and
ETN were lost from that decision although both answers carried them
well-formed, and the log called all of them "missing-from-response".
"""
import json
import logging
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src import log_health as L
from src.agents.base import AgentResult
from src.agents.tech_analyst import TechAnalystAgent
from src.models import OHLCV, TechnicalIndicators, parse_telemetry

FIXTURES = Path(__file__).parent / "fixtures"
FIRST = (FIXTURES / "tech_answer_20260917_intra_check_26f52bf2_first.txt").read_text()
RETRY = (FIXTURES / "tech_answer_20260917_intra_check_26f52bf2_retry.txt").read_text()
SUBMITTED = ["ORCL", "SQQQ", "ETN", "MTZ", "ZS"]


def _result(text: str) -> AgentResult:
    return AgentResult(raw_text=text, tokens_used=0, model="m")


def _symbols_data(symbols):
    bars = [OHLCV(date=date(2026, 9, 16), open=1.0, high=2.0, low=0.5,
                  close=1.5, volume=1_000)]
    return [
        {"symbol": s, "bars": bars,
         "indicators": TechnicalIndicators(symbol=s, atr_14=1.0)}
        for s in symbols
    ]


def _replay(answers, asked):
    """Anthropic-client stub answering each call with the next text, and
    recording which submitted symbols each prompt asked about."""
    queue = list(answers)

    def _respond(**kw):
        content = kw["messages"][0]["content"]
        asked.append([s for s in SUBMITTED if f"### {s}" in content])
        resp = MagicMock()
        resp.content = [MagicMock(text=queue.pop(0))]
        resp.usage.input_tokens = 1
        resp.usage.output_tokens = 1
        return resp

    client = MagicMock()
    client.messages.create.side_effect = _respond
    return client


# --- the parser --------------------------------------------------------------

def test_the_shared_parser_still_keeps_one_fragment_for_other_seats():
    """The defect's mechanism, left in place on purpose: `parse_json` is used
    by every seat and is not changed. The tech seat no longer calls it."""
    assert _result(FIRST).parse_json()["symbol"] == "ZS"
    assert _result(RETRY).parse_json()["symbol"] == "MTZ"


def test_every_well_formed_row_of_the_real_answer_is_kept():
    salvage = _result(FIRST).parse_json_rows()
    assert [r["symbol"] for r in salvage.rows] == ["ORCL", "ETN", "MTZ", "ZS"]
    assert [m.key for m in salvage.malformed] == ["SQQQ"]
    assert "n/a" in salvage.malformed[0].reason

    retry = _result(RETRY).parse_json_rows()
    assert [r["symbol"] for r in retry.rows] == ["ORCL", "ETN", "MTZ"]
    assert [m.key for m in retry.malformed] == ["SQQQ"]


def test_a_clean_answer_parses_exactly_as_before():
    rows = [{"symbol": "A"}, {"symbol": "B"}]
    salvage = _result(json.dumps(rows)).parse_json_rows()
    assert salvage.rows == rows and salvage.malformed == []


def test_a_broken_quote_corrupts_only_its_own_row():
    """A string missing its closing quote would, without the newline reset,
    swallow every row after it into one unparseable blob."""
    text = (
        '[\n'
        '  {\n    "symbol": "AAA",\n    "reasoning": "unterminated,\n  },\n'
        '  {\n    "symbol": "BBB",\n    "reasoning": "fine"\n  },\n'
        '  {\n    "symbol": "CCC",\n    "reasoning": "fine"\n  }\n'
        ']'
    )
    salvage = _result(text).parse_json_rows()
    assert [r["symbol"] for r in salvage.rows] == ["BBB", "CCC"]
    assert [m.key for m in salvage.malformed] == ["AAA"]


def test_a_cut_off_answer_keeps_the_rows_before_the_cut():
    text = (
        '[\n  {\n    "symbol": "AAA",\n    "reasoning": "fine"\n  },\n'
        '  {\n    "symbol": "BBB",\n    "reasoning": "the answer ran out'
    )
    salvage = _result(text).parse_json_rows()
    assert [r["symbol"] for r in salvage.rows] == ["AAA"]
    assert [m.key for m in salvage.malformed] == ["BBB"]
    assert "cut off" in salvage.malformed[0].reason


def test_a_single_object_answer_falls_back_to_the_shared_parser():
    text = 'Here it is: {"symbol": "AAA", "rating": "neutral"} done'
    salvage = _result(text).parse_json_rows()
    assert salvage.rows == [{"symbol": "AAA", "rating": "neutral"}]


# --- the seat, end to end ------------------------------------------------------

@patch("anthropic.Anthropic")
def test_the_2026_09_17_decision_no_longer_loses_orcl_and_etn(mock_cls, caplog):
    """Replays both real answers. Before: ORCL, ETN and SQQQ ended as failed.
    Now only SQQQ, which was genuinely broken in both answers, is lost; the
    retry asks about SQQQ alone; and the log says malformed, not missing."""
    asked: list[list[str]] = []
    mock_cls.return_value = _replay([FIRST, RETRY], asked)
    parse_telemetry.reset()
    agent = TechAnalystAgent(api_key="test", model="claude-sonnet-4-6-20250514")

    with caplog.at_level(logging.INFO):
        results, _ = agent.analyze_batch(_symbols_data(SUBMITTED))

    kept = sorted(s for s, a in results.items() if a is not None)
    assert kept == ["ETN", "MTZ", "ORCL", "ZS"]
    assert results["SQQQ"] is None
    assert asked == [SUBMITTED, ["SQQQ"]], "the retry must target only the broken row"

    text = caplog.text
    assert "malformed-in-response=['SQQQ'], missing-from-response=[]" in text
    assert "returned but unusable: SQQQ malformed:" in text
    assert "absent from every answer: []" in text
    assert parse_telemetry.dropped_snapshot()[("TechAnalysisResult", "SQQQ")] == 2


@patch("anthropic.Anthropic")
def test_an_omitted_row_and_a_malformed_row_are_named_apart(mock_cls, caplog):
    """ZS absent from the answer, SQQQ present but broken: both retried,
    each labelled for what actually happened."""
    first = FIRST.replace('"symbol": "ZS"', '"symbol": "ZZZ-not-asked"')
    asked: list[list[str]] = []
    mock_cls.return_value = _replay([first, first], asked)
    agent = TechAnalystAgent(api_key="test", model="claude-sonnet-4-6-20250514")

    with caplog.at_level(logging.INFO):
        results, _ = agent.analyze_batch(_symbols_data(SUBMITTED))

    assert asked[1] == ["SQQQ", "ZS"]
    assert "malformed-in-response=['SQQQ'], missing-from-response=['ZS']" in caplog.text
    assert "absent from every answer: ['ZS']" in caplog.text
    assert results["ORCL"] is not None and results["ETN"] is not None


@pytest.mark.parametrize("line", [
    "Tech answer carried 1 malformed row(s) — dropped individually, the 4 "
    "well-formed row(s) beside them kept: SQQQ: Expecting property name",
    "Tech batch incomplete: submitted=5, parsed=4, validation-failed=[], "
    "malformed-in-response=['SQQQ'], missing-from-response=[] — retrying",
])
def test_the_health_report_treats_a_recoverable_broken_row_as_handled(line):
    """The loss, if any, is the later `unresolved after retry` line."""
    family = L.classify(line, "WARNING")
    assert family is not None and family.reason is None
