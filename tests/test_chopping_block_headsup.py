"""Item 228: the chopping-block heads-up lists every holding and is read-only."""
import json
import sqlite3

from src.api.routes_chopping_block import build_rows, read_passes


def _p(ts, examined, below, **extra):
    rec = {"held_examined": ",".join(examined),
           "held_below_entry_bar": ",".join(below), **extra}
    return {"run_id": ts, "ts": ts, "record": rec, "disposition": {}}


def test_every_holding_listed_below_bar_first():
    r = build_rows([_p("2026-10-01T10", ["ZZB", "ZZA", "ZZC"], ["ZZC"])])
    assert [h.symbol for h in r.holdings] == ["ZZC", "ZZA", "ZZB"]
    assert r.holdings[0].standing == "below_bar"
    assert {h.standing for h in r.holdings[1:]} == {"clears_bar"}


def test_direction_slipped_recovered_steady_without_any_cutoff():
    passes = [_p("2026-09-30T10", ["ZZA", "ZZB", "ZZC"], ["ZZB"]),
              _p("2026-10-01T10", ["ZZA", "ZZB", "ZZC"], ["ZZA"]),
              _p("2026-10-02T10", ["ZZA", "ZZB", "ZZC"], ["ZZA"])]
    by = {h.symbol: h for h in build_rows(passes).holdings}
    assert by["ZZA"].direction == "slipped" and "2026-10-01" in by["ZZA"].headline
    assert by["ZZB"].direction == "recovered"
    assert by["ZZC"].direction == "steady"


def test_reason_is_real_and_a_missing_one_says_so():
    p = _p("2026-10-02T10", ["ZZA", "ZZB"], ["ZZA", "ZZB"],
           held_symbol="ZZA", tier="ineligible_hold",
           held_reasons="R2 neutral rating")
    p["disposition"] = {"below_bar_reasons": "ZZA=R2 neutral rating|ZZB=R5 net evidence -1"}
    by = {h.symbol: h for h in build_rows([p]).holdings}
    assert "R2 neutral rating" in by["ZZA"].reason
    assert "R5 net evidence" in by["ZZB"].reason
    p["disposition"] = {}
    by = {h.symbol: h for h in build_rows([p]).holdings}
    assert "does not say" in by["ZZB"].reason


def test_no_record_is_not_reported_as_healthy():
    assert "not the same as every holding being healthy" in build_rows([]).note


def test_read_passes_reads_rows_and_joins_dispositions():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("CREATE TABLE specialist_evidence (id INTEGER PRIMARY KEY, run_id TEXT,"
              " symbol TEXT, timestamp TEXT, agent_name TEXT, kind TEXT, evidence_json TEXT)")
    for rid, d in (("r1", {"stage": "rotation", "outcome": "precheck",
                           "held_examined": "ZZA", "held_below_entry_bar": "ZZA"}),
                   ("r1", {"stage": "rotation", "outcome": "dispositions",
                           "below_bar_reasons": "ZZA=R2 neutral rating"})):
        c.execute("INSERT INTO specialist_evidence (run_id, timestamp, agent_name, kind,"
                  " evidence_json) VALUES (?, '2026-10-02T10', 'pipeline', 'pipeline_event', ?)",
                  (rid, json.dumps(d)))
    r = build_rows(read_passes(c))
    assert r.holdings[0].reason.endswith("R2 neutral rating")
