"""The answer-parsing modules stand on their own.

The owner's test for a split: a piece is separated only when it can be
constructed and exercised with none of the module it came from anywhere in
sight. These two modules take the answer text and parsing policy by value, so
the proof is that every behaviour below is reachable from plain values — and
that neither module pulls ``src.agents.base`` (nor anything that would drag a
seat, a provider or a transport in) into ``sys.modules`` when imported alone.
"""

import json
import pathlib
import subprocess
import sys

from src.agents.json_answer_parse import (
    parse_json_text,
    repair_unquoted_keys,
    shape_score,
)
from src.agents.json_salvage import MalformedRow, RowSalvage, parse_json_rows_text


WEIGHTS = {"targets": 2, "symbol": 1}


def test_neither_module_imports_the_module_it_came_out_of():
    # A fresh interpreter: importing the parts must not load src.agents.base,
    # BaseAgent, AgentResult or any provider transport. A delegating shim or a
    # leftover back-reference would show up here as a loaded module.
    code = (
        "import sys;"
        "import src.agents.json_salvage, src.agents.json_answer_parse;"
        "print([m for m in sys.modules if m.startswith('src.agents')"
        " and m not in ('src.agents', 'src.agents.json_salvage',"
        " 'src.agents.json_answer_parse')])"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True, text=True,
        cwd=str(pathlib.Path(__file__).resolve().parent.parent),
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "[]", proc.stdout


def test_parses_a_plain_string_with_no_result_object():
    assert parse_json_text('{"approved": true}') == {"approved": True}
    assert parse_json_text("not json at all") is None


def test_picks_the_agent_shaped_fragment_out_of_prose():
    raw = 'thinking {"note": "scratch"} then\n```json\n{"targets": [{"symbol": "A"}]}\n```'
    assert parse_json_text(raw, WEIGHTS) == {"targets": [{"symbol": "A"}]}


def test_shape_score_accepts_policy_by_value():
    assert shape_score({"targets": []}, WEIGHTS) > shape_score(
        {"note": "scratch"}, WEIGHTS,
    )


def test_repair_unquoted_keys_is_a_text_to_text_function():
    # The one defect it repairs: a top-level key that lost its OPENING
    # quote at the start of a line (a real 2026-09-02 production payload).
    broken = '{\n  "a": 1,\n  symbol": "A"\n}'
    assert json.loads(repair_unquoted_keys(broken)) == {"a": 1, "symbol": "A"}


def test_row_salvage_keeps_good_rows_and_names_the_broken_one():
    text = '[{"symbol": "A", "r": 1}, {"symbol": "B", "r": n/a}, {"symbol": "C"}]'
    salvage = parse_json_rows_text(text, key_field="symbol")
    assert isinstance(salvage, RowSalvage)
    assert [row["symbol"] for row in salvage.rows] == ["A", "C"]
    assert [bad.key for bad in salvage.malformed] == ["B"]
    assert isinstance(salvage.malformed[0], MalformedRow)


def test_row_salvage_unwraps_a_named_list_field():
    text = '{"results": [{"symbol": "A"}, {"symbol": "B"}]}'
    salvage = parse_json_rows_text(text, key_field="symbol", list_field="results")
    assert [row["symbol"] for row in salvage.rows] == ["A", "B"]
    assert salvage.malformed == []


def test_nothing_parseable_returns_none():
    assert parse_json_rows_text("absolutely not json") is None
