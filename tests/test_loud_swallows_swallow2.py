"""Fourteen read-failure handlers on money-path data modules stay loud and counted.

A handler that goes back to a bare log line reappears in the guard's full
violation list, which this test reads (never the delta the guard prints).
"""
import pytest

from scripts.silent_swallow_guard import violations

CONVERTED = {
    "src/data/market.py": {"_try_fallback", "get_company_profile", "_fetch",
                           "get_next_earnings_date", "get_sector_performance"},
    "src/data/earnings.py": {"_get_recent_filings", "_download_filing"},
    "src/evidence_gate.py": {"names_missing_blocking_seat", "name_coverage"},
    "src/prompt_facts/missed_ops_signals.py": {
        "_missed_ops_tech_signal", "_missed_ops_macro_sector_map",
        "_thesis_tech_trajectory_map"},
    "src/execution/broker_parts/market_data.py": {
        "get_top_movers", "get_bars", "get_intraday_snapshots"},
}


def test_converted_sites_are_not_silent_again():
    back = [(s[0], s[1]) for s, _ in violations()
            if s[0] in CONVERTED and s[1] in CONVERTED[s[0]]]
    assert not back, f"swallow went silent again: {back}"


def test_sector_performance_failure_is_counted_with_a_traceback(monkeypatch):
    import src.data.market as m
    seen = []
    monkeypatch.setattr(m, "record_swallowed",
                        lambda where, exc, **kw: seen.append((where, exc)))
    monkeypatch.setattr(m.yf, "download",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    assert m.MarketDataProvider().get_sector_performance() == {}
    assert [w for w, _ in seen] == ["data.market.sector_performance"]
    assert isinstance(seen[0][1], RuntimeError)


def test_evidence_gate_blocking_gap_failure_is_counted(monkeypatch):
    import src.evidence_gate as g
    seen = []
    monkeypatch.setattr(g, "record_swallowed",
                        lambda where, exc, **kw: seen.append(where))
    src = open(g.__file__).read()
    assert 'record_swallowed("evidence_gate.blocking_gap_read"' in src
    assert 'record_swallowed("evidence_gate.name_coverage"' in src
