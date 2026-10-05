"""Rule (f): a numeric keyword literal at a call site is a candidate site, as a default is."""
from __future__ import annotations

import ast
from pathlib import Path

from src import number_sources
from src.number_callsite_scan import collect_callsite_sites, scan_callsite_literals

DIGEST_IDS = [
    "src.sessions.evening_session.EveningSession.run:call[self._build_missed_opportunities_digest(%s)]" % kw
    for kw in ("lookback_days", "move_threshold_pct", "top_n")
]


def _scan(source: str) -> dict[str, float]:
    sites = scan_callsite_literals(ast.parse(source), "m", "m.py", {}, {})
    return {s.site_id: s.value for s in sites}


def test_call_site_literal_the_old_scanner_missed_is_caught(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "caller.py").write_text("def run(self):\n    return digest(top_n=15, move_pct=8.0)\n")
    assert number_sources._scan_module(tmp_path / "src" / "caller.py", "src/caller.py", (), tmp_path) == []
    found = {s.site_id: s.value for s in collect_callsite_sites(tmp_path)}
    assert found == {
        "src.caller.run:call[digest(top_n)]": 15,
        "src.caller.run:call[digest(move_pct)]": 8.0,
    }


def test_noise_is_not_a_site() -> None:
    source = (
        "def f():\n"
        "    g(timeout=30, indent=2, n=1, k=-1, z=0, h=datetime(hour=9))\n"
        "    x = Field(default=5, le=100, min_length=3)\n"
    )
    assert _scan(source) == {}


def test_float_one_and_repeats_are_kept_and_stably_numbered() -> None:
    found = _scan("def f():\n    g(atr=1.0)\n    g(atr=2.5)\n")
    assert found == {"m.f:call[g(atr)]": 1.0, "m.f:call[g(atr)#1]": 2.5}


def test_the_failing_case_still_fails() -> None:
    """A new unledgered literal is a site the guard would count; this is the bite."""
    assert _scan("def f():\n    g(top_n=15)\n") != {}


def test_the_live_digest_values_are_ledgered_and_audit_is_clean() -> None:
    ledger = number_sources.load_ledger()
    ids = {s.site_id for s in collect_callsite_sites(number_sources.REPO_ROOT)}
    for site_id in DIGEST_IDS:
        assert site_id in ids
        assert ledger[site_id]["status"] == "arbitrary"
    assert [str(p) for p in number_sources.audit()] == []
