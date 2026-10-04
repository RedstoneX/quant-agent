"""The smart-money text caps emit a counted row on every evaluation."""
import logging

from src.agents.smart_money_analyst import SmartMoneyAnalystAgent as A
from src.agents.smart_money_cap_record import ROW_TAG, rows_from, summarise


def _rows(caplog):
    return rows_from(r.getMessage() for r in caplog.records if ROW_TAG in r.getMessage())


def test_binding_and_non_binding_are_distinct(caplog):
    caplog.set_level(logging.INFO)
    assert A._bounded_context("x" * 150) == "x" * 93 + "..."
    assert A._bounded_context("short") == "short"
    rows = _rows(caplog)
    assert [r["bound"] for r in rows] == [True, False]
    assert rows[0]["length_before"] == 150 and rows[0]["kept"] == 96
    assert rows[1]["length_before"] == 5
    s = summarise(rows)["_MAX_CONTEXT_TEXT_CHARS"]
    assert (s["evaluated"], s["bound"], s["not_bound"], s["longest_seen"]) == (2, 1, 1, 150)


def test_reason_cap_named_separately_and_no_rows_when_not_run(caplog):
    caplog.set_level(logging.INFO)
    assert _rows(caplog) == []
    A._bounded_context("y" * 300, 220, "_MAX_REASON_TEXT_CHARS")
    A._bounded_context("y" * 10, 220, "_MAX_REASON_TEXT_CHARS")
    s = summarise(_rows(caplog))
    assert set(s) == {"_MAX_REASON_TEXT_CHARS"}
    assert s["_MAX_REASON_TEXT_CHARS"]["bound"] == 1
    assert s["_MAX_REASON_TEXT_CHARS"]["not_bound"] == 1
