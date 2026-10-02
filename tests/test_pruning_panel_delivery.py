"""Board item 219 - the pruning pass reaches the dashboard panel.

Seeds stored runs and reads them back through the real route, including the
pruned-nothing case, and proves the delivery path is read-only.
"""
import sqlite3
from pathlib import Path

from src.api import routes_pruning
from src.api.routes_pruning import get_pruning_passes
from tests.test_pruning_pass_reaches_both_surfaces import _ROW
from tests.test_trader_feed import _evidence, _make_db

_STATIC = Path(__file__).resolve().parents[1] / "src" / "api" / "static"


def _point_api_at(db, monkeypatch):
    def _ro():
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        return conn

    monkeypatch.setattr(routes_pruning, "_connect", _ro)


def _precheck(db, run, **extra):
    _evidence(db, run, "pipeline", "pipeline_event", {
        "stage": "rotation", "outcome": "precheck",
        "reason": "full_nothing_outranked_a_holding",
        "headroom_pct": 0.09, "ceiling_pct": 25.0, "floor_pct": 0.5,
        "execute_enabled": True, "ranked_margin_enabled": False, **extra,
    })


def test_cut_and_kept_names_each_carry_a_verdict_and_reason(tmp_path, monkeypatch):
    db = _make_db(tmp_path, monkeypatch)
    _precheck(db, "run-a", **_ROW)
    _point_api_at(db, monkeypatch)
    resp = get_pruning_passes()
    assert [p.run_id for p in resp.passes] == ["run-a"]
    by = {v.symbol: v for v in resp.passes[0].verdicts}
    assert by["BBB"].verdict == "cut" and "technical rule failed" in by["BBB"].reason
    assert by["CCC"].verdict == "below_bar_not_cut"
    assert by["AAA"].verdict == "kept" and by["AAA"].reason
    assert any("examined all 3 holdings" in ln for ln in resp.passes[0].lines)


def test_pruned_nothing_run_still_shows_what_it_looked_at(tmp_path, monkeypatch):
    db = _make_db(tmp_path, monkeypatch)
    _precheck(
        db, "run-b", tier="", held_symbol="", held_reasons="",
        held_examined="AAA,BBB", held_examined_count=2,
        held_below_entry_bar="", held_below_entry_bar_count=0,
    )
    _point_api_at(db, monkeypatch)
    p = get_pruning_passes().passes[0]
    assert p.examined_count == 2
    assert [v.verdict for v in p.verdicts] == ["kept", "kept"]
    assert any("nothing it was permitted to cut" in ln for ln in p.lines)


def test_no_record_says_so_instead_of_staying_silent(tmp_path, monkeypatch):
    db = _make_db(tmp_path, monkeypatch)
    _point_api_at(db, monkeypatch)
    resp = get_pruning_passes()
    assert resp.passes == [] and "No pruning pass has been recorded" in resp.note


def test_delivery_is_read_only_and_wired():
    import inspect

    from src.api.server import create_app  # noqa: F401
    src = inspect.getsource(routes_pruning)
    for banned in ("INSERT", "UPDATE", "DELETE", ".commit(", "@router.post", "@router.put"):
        assert banned not in src
    server = inspect.getsource(__import__("src.api.server", fromlist=["x"]))
    assert "pruning_router" in server
    html = (_STATIC / "index.html").read_text()
    assert "panel-pruning" in html
    assert "/pruning-passes" in (_STATIC / "app.js").read_text()
