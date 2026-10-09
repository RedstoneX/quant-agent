"""The route-event panel surfaces every journal row, whatever its type."""

import sqlite3

from src.api import routes_route_events as mod


def _db(tmp_path, monkeypatch, rows):
    path = tmp_path / "t.db"
    c = sqlite3.connect(path)
    c.execute(
        "CREATE TABLE llm_route_events (id INTEGER PRIMARY KEY, event_type TEXT, "
        "agent_name TEXT, run_id TEXT, route TEXT, from_route TEXT, tier INT, "
        "input_usd_per_mtok REAL, output_usd_per_mtok REAL, wait_s REAL, "
        "error_shape TEXT, detail TEXT, timestamp TEXT)"
    )
    for r in rows:
        c.execute(
            "INSERT INTO llm_route_events (event_type, agent_name, route, from_route,"
            "input_usd_per_mtok, output_usd_per_mtok, timestamp) VALUES (?,?,?,?,?,?,?)",
            r,
        )
    c.commit()
    c.close()

    def conn():
        k = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        k.row_factory = sqlite3.Row
        return k

    monkeypatch.setattr(mod, "_connect", conn)


def test_unknown_future_event_type_still_shows(tmp_path, monkeypatch):
    _db(
        tmp_path,
        monkeypatch,
        [
            ("route_switch", "risk_manager", "a", "b", 0.0, 0.0, "2026-01-01"),
            ("brand_new_kind", "trader", None, None, 1.5, 6.0, "2026-01-02"),
        ],
    )
    r = mod.get_route_events()
    assert [e.seat for e in r.events] == ["trader", "risk manager"]
    assert "brand new kind" in r.events[0].what
    assert r.events[1].cost == "free model"
    assert "paid model" in r.events[0].cost


def test_unreadable_is_not_reported_as_nothing_happened(monkeypatch):
    def boom():
        raise RuntimeError("x")

    monkeypatch.setattr(mod, "_connect", boom)
    r = mod.get_route_events()
    assert r.events == [] and "not the same as no fallback" in r.note


def test_missing_table_says_no_entries(tmp_path, monkeypatch):
    path = tmp_path / "e.db"
    sqlite3.connect(path).close()
    monkeypatch.setattr(mod, "_connect", lambda: sqlite3.connect(f"file:{path}?mode=ro", uri=True))
    assert "no entries" in mod.get_route_events().note
