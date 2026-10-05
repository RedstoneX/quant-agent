"""The unscoped-number guard stores no ceiling (docs/GUARDS_WITHOUT_STORED_STATE.md)."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from scripts import guard_reference, unscoped_number_guard
from scripts.guard_reference import ReferenceUnavailable


def test_no_unscoped_number_is_added_against_the_trunk():
    bad = unscoped_number_guard.violations()
    assert not bad, "\n  ".join(bad)


def test_the_trunk_is_actually_measured():
    assert len(unscoped_number_guard.trunk_sites()) > 50, "trunk measured nothing"


def _site(site_id, value):
    from src.number_sources import NumberSite
    return NumberSite(site_id, site_id.rsplit(".", 1)[0].replace(".", "/") + ".py", 1, value)


def _delta(monkeypatch, trunk, working):
    monkeypatch.setattr(unscoped_number_guard, "working_number_sites", lambda: working)
    monkeypatch.setattr(unscoped_number_guard, "trunk_number_sites", lambda: trunk)
    return unscoped_number_guard.violations()


def test_a_new_number_is_caught_as_a_delta(monkeypatch):
    real = unscoped_number_guard.working_sites()
    monkeypatch.setattr(unscoped_number_guard, "working_sites", lambda: real)
    monkeypatch.setattr(unscoped_number_guard, "trunk_sites", lambda callsites=False: real[:-1] if not callsites else [])
    bad = unscoped_number_guard.violations()
    assert bad and "+1" in bad[0] and real[-1] in bad[0], bad
    real = unscoped_number_guard.working_number_sites()
    bad = _delta(monkeypatch, real[:-1], real)
    assert bad and "+1" in bad[0] and real[-1].site_id in bad[0], bad


def test_deleting_one_constant_does_not_pay_for_adding_another(monkeypatch):
    """The net total is unchanged (one out, one in); the new number is still refused."""
    trunk = [_site("src.a.OLD_LIMIT", 5.0), _site("src.b.KEEP", 1.0)]
    working = [_site("src.b.KEEP", 1.0), _site("src.c.NEW_LIMIT", 7.0)]
    bad = _delta(monkeypatch, trunk, working)
    assert bad and "+1" in bad[0] and "src.c.NEW_LIMIT" in bad[0] and "OLD_LIMIT" not in bad[0], bad


def test_a_shared_value_alone_is_not_identity(monkeypatch):
    """Same value, different name AND different module: two unrelated constants."""
    trunk = [_site("src.a.OLD_LIMIT", 5.0)]
    working = [_site("src.c.NEW_LIMIT", 5.0)]
    assert _delta(monkeypatch, trunk, working), "an unrelated constant hid behind a shared value"


def test_a_renamed_constant_is_the_same_number(monkeypatch):
    trunk = [_site("src.a.OLD_LIMIT", 5.0), _site("src.b.KEEP", 1.0)]
    working = [_site("src.a.LIMIT", 5.0), _site("src.b.KEEP", 1.0)]
    assert _delta(monkeypatch, trunk, working) == []


def test_a_moved_constant_is_the_same_number(monkeypatch):
    trunk = [_site("src.a.LIMIT", 5.0), _site("src.b.KEEP", 1.0)]
    working = [_site("src.z.LIMIT", 5.0), _site("src.b.KEEP", 1.0)]
    assert _delta(monkeypatch, trunk, working) == []


def test_a_rename_with_a_changed_value_is_a_new_number(monkeypatch):
    trunk = [_site("src.a.OLD_LIMIT", 5.0)]
    working = [_site("src.a.LIMIT", 6.0)]
    assert _delta(monkeypatch, trunk, working), "a value change hid behind a rename"


def test_one_lost_site_pays_for_at_most_one_new_site(monkeypatch):
    trunk = [_site("src.a.LIMIT", 5.0)]
    working = [_site("src.a.LIMIT_A", 5.0), _site("src.a.LIMIT_B", 5.0)]
    bad = _delta(monkeypatch, trunk, working)
    assert bad and "+1" in bad[0] and "LIMIT_B" in bad[0], bad


def test_a_new_call_site_literal_is_caught_as_a_delta(monkeypatch):
    real = unscoped_number_guard.callsite_sites()
    monkeypatch.setattr(unscoped_number_guard, "working_sites", lambda: [])
    monkeypatch.setattr(unscoped_number_guard, "callsite_sites", lambda: real + ["m.f:call[g(top_n)]"])
    monkeypatch.setattr(unscoped_number_guard, "trunk_sites", lambda callsites=False: real if callsites else [])
    bad = unscoped_number_guard.violations()
    assert bad and "call-site" in bad[0] and "m.f:call[g(top_n)]" in bad[0], bad


def test_it_refuses_when_the_trunk_cannot_be_read(tmp_path, monkeypatch):
    repo = tmp_path / "norepo"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "m.py").write_text("X = 1\n")
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    subprocess.run(["git", "add", "src/m.py"], cwd=repo, check=True)
    subprocess.run(["git", "-c", "user.email=t@example.invalid", "-c", "user.name=t",
                    "commit", "-qm", "base"], cwd=repo, check=True)
    monkeypatch.setattr(guard_reference, "ROOT", Path(repo))
    monkeypatch.setattr(unscoped_number_guard, "ROOT", Path(repo))
    monkeypatch.setattr(unscoped_number_guard, "working_number_sites", lambda: [])
    with pytest.raises(ReferenceUnavailable) as exc:
        unscoped_number_guard.violations()
    assert "origin/main" in str(exc.value)
    assert unscoped_number_guard.main() == 2


def _repo_with(tmp_path, files, scoped=True):
    root = tmp_path / "r"
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text)
    from src import number_sources

    for entry in number_sources.SCOPED_PATHS if scoped else ():  # the scanner refuses a vanished scoped path
        target = root / entry
        if entry.endswith(".py"):
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("")
        else:
            target.mkdir(parents=True, exist_ok=True)
    return root


def test_the_sentinel_reaches_root_ops_and_scripts_not_only_src(tmp_path):
    from src import number_sources

    root = _repo_with(tmp_path, {
        "main.py": "STALE_PCT = 7.5\n",
        "ops/x.py": "LIMIT = 11\n",
        "scripts/s.py": "CAP = 13\n",
        "src/a.py": "Z = 17\n",
        "tests/t.py": "T = 19\n",
        ".venv/v.py": "V = 23\n",
        "src/config/__init__.py": "",
    })
    seen = [s.site_id for s in number_sources.collect_unscoped_sites(root)]
    for module in ("main.", "ops.x.", "scripts.s.", "src.a."):
        assert any(i.startswith(module) for i in seen), (module, seen)
    assert not any(i.startswith(("tests.", "t.", ".venv", "v.")) for i in seen), seen


def test_the_universe_is_derived_so_a_new_top_level_package_is_seen(tmp_path):
    from src.number_universe import py_universe

    root = _repo_with(tmp_path, {"newpkg/m.py": "Q = 29\n"}, scoped=False)
    assert [p.name for p in py_universe(root)] == ["m.py"]


def test_the_money_reach_gap_is_measured_and_nonzero():
    modules, numbers = unscoped_number_guard.money_reach_gap()
    assert modules > 0 and numbers > 0, (modules, numbers)
