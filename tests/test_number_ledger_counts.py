"""The settlement-route ratchet: down-only against the trunk, stores nothing."""

from __future__ import annotations

from pathlib import Path


def test_the_route_ratchet_refuses_a_rise_and_allows_a_fall(monkeypatch) -> None:
    """It bites. The reference is the trunk's own ledger, nothing stored. A
    trunk one row BETTER than this tree must refuse; one row WORSE must not.
    The old shape summed deltas, so a refused change could raise its own limit."""
    import src.number_sources as ns

    live = len(ns.classification(ns.load_ledger())["unclassified"])
    monkeypatch.setattr(ns, "trunk_routeless_count", lambda: live - 1)
    assert [p for p in ns.audit() if p.kind == "route-ratchet"]
    monkeypatch.setattr(ns, "trunk_routeless_count", lambda: live + 1)
    assert not [p for p in ns.audit() if p.kind == "route-ratchet"]


def test_the_route_ratchet_stores_nothing() -> None:
    root = Path(__file__).resolve().parent.parent
    assert not (root / "config" / "number_ledger_route_history.yaml").exists()
    text = (root / "src" / "number_sources.py").read_text(encoding="utf-8")
    assert "MAX_ROUTELESS_ARBITRARY" not in text and "ROUTE_RATCHET_HISTORY" not in text


def test_the_route_count_matches_a_direct_parse_of_trunk() -> None:
    from src.number_ledger_counts import count_routeless
    from src.number_sources import classification, load_ledger

    text = (Path(__file__).resolve().parent.parent / "config" / "number_ledger.yaml").read_text(encoding="utf-8")
    assert count_routeless(text) == len(classification(load_ledger())["unclassified"])
