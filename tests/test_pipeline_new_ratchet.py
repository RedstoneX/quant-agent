"""__new__-pipeline ratchet: no test file may gain a constructor-skipping build.

The sites that exist today are pinned by identity (path | source line) in the
fixed list config/check_allowlists/struct_pipeline_new.txt. Nothing is compared
with any trunk. The check fails on a site not in the list and on a listed site
that no longer occurs. See scripts/pipeline_new_guard.py for why, and
tests/pipeline_factory.py for the replacement.
"""
from __future__ import annotations

from scripts import pipeline_new_guard, struct_allowlist

# Assembled, not written literally: this file must not itself become an offender.
SITE = "p = TradingPipeline." + "__new__" + "(TradingPipeline)"
ENTRY = f"tests/test_planted.py | {SITE}"


def _list(tmp_path, entries):
    struct_allowlist.write("pipeline_new", entries, tmp_path)
    return tmp_path


def _pretend(monkeypatch, entries):
    monkeypatch.setattr(pipeline_new_guard, "found", lambda: list(entries))


def test_todays_sites_match_the_fixed_list():
    bad = pipeline_new_guard.violations()
    assert not bad, "Pipeline __new__ sites differ from the fixed list:\n" + "\n".join(bad)


def test_the_measurement_sees_the_test_tree():
    """A guard that scanned nothing would pass vacuously."""
    assert len(pipeline_new_guard.test_paths()) > 50


def test_a_new_site_fails(tmp_path, monkeypatch):
    _pretend(monkeypatch, [ENTRY])
    bad = pipeline_new_guard.violations(_list(tmp_path, []))
    assert len(bad) == 1 and "NEW" in bad[0] and "test_planted.py" in bad[0], bad


def test_a_listed_site_passes(tmp_path, monkeypatch):
    _pretend(monkeypatch, [ENTRY])
    assert pipeline_new_guard.violations(_list(tmp_path, [ENTRY])) == []


def test_a_stale_entry_fails(tmp_path, monkeypatch):
    _pretend(monkeypatch, [])
    bad = pipeline_new_guard.violations(_list(tmp_path, [ENTRY]))
    assert len(bad) == 1 and "STALE" in bad[0], bad


def test_removing_one_site_and_adding_another_still_fails(tmp_path, monkeypatch):
    _pretend(monkeypatch, [f"tests/test_planted.py | q = {SITE}  # moved"])
    bad = pipeline_new_guard.violations(_list(tmp_path, [ENTRY]))
    assert len(bad) == 2 and any("NEW" in b and "# moved" in b for b in bad), bad


def test_a_second_copy_of_a_listed_site_fails(tmp_path, monkeypatch):
    _pretend(monkeypatch, [ENTRY, ENTRY])
    bad = pipeline_new_guard.violations(_list(tmp_path, [ENTRY]))
    assert len(bad) == 1 and "NEW" in bad[0], bad
