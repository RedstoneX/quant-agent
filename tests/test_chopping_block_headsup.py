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


def test_clearing_name_closing_in_is_flagged_with_margin_and_no_cutoff():
    def mp(ts, net, steps=1):
        p = _p(ts, ["ZZA", "ZZB"], [])
        p["margins"] = {"ZZA": {"r2_steps_from_neutral": steps, "r5_net_evidence": net},
                        "ZZB": {"r2_steps_from_neutral": 1, "r5_net_evidence": 3}}
        return p
    r = build_rows([mp("2026-09-30T10", 3), mp("2026-10-01T10", 2), mp("2026-10-02T10", 1)])
    assert r.holdings[0].symbol == "ZZA" and r.holdings[0].direction == "closing_in"
    m = {x.rule: x for x in r.holdings[0].margins}["net evidence"]
    assert (m.now, m.previous, m.first) == (1, 2, 3)
    assert r.holdings[1].direction == "steady"


def test_margins_for_uses_the_desks_own_net_score():
    from types import SimpleNamespace as N
    from src.rotation_margins import margins_for
    a = [N(symbol="ZZA", rating="buy"), N(symbol="ZZB", rating="neutral")]
    out = margins_for(["ZZA", "ZZB", "ZZC"], a, {"ZZA": {}}, None, None)
    assert out["ZZA"] == {"rating": "buy", "r2_steps_from_neutral": 1, "r5_net_evidence": 0}
    assert "r5_net_evidence" not in out["ZZB"] and "ZZC" not in out


def test_dispositions_writer_output_reaches_the_panel_for_uncut_names():
    """Round trip: what item 219 records for a name it did NOT cut is what the owner reads."""
    from types import SimpleNamespace as NS

    from src.rotation_dispositions import disposition_payload

    opp = NS(ineligible_candidates=[("ZZA", ["R2 neutral rating"]),
                                    ("ZZB", ["R5 net evidence -1"])])
    pre = NS(opportunity=opp, held_below_entry_bar=("ZZA", "ZZB"))
    payload = disposition_payload(pre, {"ZZA"}, True)
    p = _p("2026-10-02T10", ["ZZA", "ZZB"], ["ZZA", "ZZB"])
    p["disposition"] = payload
    by = {h.symbol: h for h in build_rows([p]).holdings}
    assert "R5 net evidence -1" in by["ZZB"].reason
    assert "Not sold yet" in by["ZZB"].reason
    assert "does not say" not in by["ZZB"].reason


def test_unrecorded_distance_is_shown_not_called_safe_and_sorts_ahead_of_known():
    a = _p("2026-10-02T10", ["ZZA", "ZZB"], [])
    a["margins"] = {"ZZB": {"r2_steps_from_neutral": 2, "r5_net_evidence": 3}}
    r = build_rows([a])
    by = {h.symbol: h for h in r.holdings}
    assert by["ZZA"].distance_known is False and "NOT recorded" in by["ZZA"].distance
    assert by["ZZB"].distance_known is True and "3 independent" in by["ZZB"].distance
    assert [h.symbol for h in r.holdings] == ["ZZA", "ZZB"]
    assert r.summary == "2 holdings: 0 below the bar, 0 closing in, 1 with no distance recorded."


def test_below_bar_name_gets_no_grace_wording_and_counts_in_summary():
    r = build_rows([_p("2026-10-02T10", ["ZZA", "ZZB"], ["ZZA"])])
    assert "no grace" in r.holdings[0].distance and r.holdings[0].symbol == "ZZA"
    assert r.summary.startswith("2 holdings: 1 below the bar")


def test_panel_ships_as_cards_not_a_wide_table():
    from pathlib import Path
    st = Path(__file__).resolve().parents[1] / "src" / "api" / "static"
    js = (st / "chopping_block.js").read_text()
    assert "/chopping-block" in js and 'el("table"' not in js
    assert "chopping_block.js" in (st / "index.html").read_text()
    assert "function loadChoppingBlock" not in (st / "app.js").read_text()
