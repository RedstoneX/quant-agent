"""Rule (f): keyword literals at call sites are seen, and a registered one is not reported."""
from __future__ import annotations

from src import number_callsite_scan as scan
from src.number_sources import SCOPED_PATHS

SRC = "def run():\n    do_thing(window=20)\n    size(threshold=-0.35, flag=True, n=0, other=name)\n"


def test_unregistered_keyword_literals_are_reported():
    sites = scan.scan_source(SRC, "src.m", "src/m.py")
    got = {(s.site_id, s.value) for s in sites}
    assert got == {
        ("src.m.run:call[do_thing(window)]", 20.0),
        ("src.m.run:call[size(threshold)]", -0.35),
    }, got


def test_a_registered_site_is_not_reported_and_an_unregistered_one_is(tmp_path):
    for entry in SCOPED_PATHS:  # the scope list must resolve in any root
        target = tmp_path / entry
        if entry.endswith(".py"):
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("", encoding="utf-8")
        else:
            target.mkdir(parents=True, exist_ok=True)
    (tmp_path / "src" / "config").mkdir(parents=True, exist_ok=True)
    (tmp_path / "src" / "config" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "src" / "m.py").write_text(SRC, encoding="utf-8")
    everything = scan.collect_callsite_sites(tmp_path)
    assert len(everything) == 2, everything
    ledgered = {"src.m.run:call[do_thing(window)]"}
    left = scan.collect_callsite_sites(tmp_path, ledgered)
    assert [s.site_id for s in left] == ["src.m.run:call[size(threshold)]"], left
