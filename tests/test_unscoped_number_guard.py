"""The unscoped-number guard compares with a fixed, committed allow-list, never the trunk."""

from __future__ import annotations

from scripts import check_allowlist, unscoped_number_guard


def test_unscoped_numbers_match_the_fixed_allow_list():
    unlisted, stale = unscoped_number_guard.check()
    assert not unlisted and not stale, check_allowlist.report(unlisted, stale, unscoped_number_guard.ALLOWLIST)


def _listed(tmp_path, site_ids):
    path = tmp_path / "code_unscoped_number.txt"
    path.write_text(check_allowlist.render("unscoped-number", [(i,) for i in site_ids]), encoding="utf-8")
    return path


def _sites(monkeypatch, site_ids):
    monkeypatch.setattr(unscoped_number_guard, "working_sites", lambda: list(site_ids))


def test_a_new_number_not_in_the_list_fails(tmp_path, monkeypatch):
    _sites(monkeypatch, ["src.a.KEEP", "src.c.NEW_LIMIT"])
    unlisted, stale = unscoped_number_guard.check(_listed(tmp_path, ["src.a.KEEP"]))
    assert len(unlisted) == 1 and "src.c.NEW_LIMIT" in unlisted[0] and not stale


def test_listed_numbers_pass(tmp_path, monkeypatch):
    _sites(monkeypatch, ["src.a.KEEP"])
    assert unscoped_number_guard.check(_listed(tmp_path, ["src.a.KEEP"])) == ([], [])


def test_a_stale_entry_fails(tmp_path, monkeypatch):
    _sites(monkeypatch, [])
    unlisted, stale = unscoped_number_guard.check(_listed(tmp_path, ["src.a.GONE"]))
    assert not unlisted and len(stale) == 1


def test_a_renamed_constant_needs_the_list_edited(tmp_path, monkeypatch):
    """No trunk-derived excuse for a move or rename: the new id is unlisted, the old one stale."""
    _sites(monkeypatch, ["src.a.LIMIT"])
    unlisted, stale = unscoped_number_guard.check(_listed(tmp_path, ["src.a.OLD_LIMIT"]))
    assert len(unlisted) == 1 and len(stale) == 1


def test_the_guard_reads_no_git_trunk():
    assert not hasattr(unscoped_number_guard, "trunk_blobs") and not hasattr(unscoped_number_guard, "trunk_sites")


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

    root = _repo_with(
        tmp_path,
        {
            "main.py": "STALE_PCT = 7.5\n",
            "ops/x.py": "LIMIT = 11\n",
            "scripts/s.py": "CAP = 13\n",
            "src/a.py": "Z = 17\n",
            "tests/t.py": "T = 19\n",
            ".venv/v.py": "V = 23\n",
            "src/config/__init__.py": "",
        },
    )
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
