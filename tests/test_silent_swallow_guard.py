"""Silent-swallow ratchet: no NEW broad-except-returns-empty-records-nothing on money paths.

See scripts/silent_swallow_guard.py for the pattern, what counts as a durable
record in this codebase, and the money-module scope. The guard stores nothing:
it scans the working tree and compares with the committed allow-list
``config/check_allowlists/code_silent_swallow.txt`` by site IDENTITY (never a
total): a swallow not listed fails, and a listed entry that no longer occurs fails.
"""
from __future__ import annotations

import ast
import subprocess

from scripts import check_allowlist
from scripts import money_modules as mm
from scripts import silent_swallow_guard as g
from scripts.guard_reference import ReferenceUnavailable


def test_money_path_swallows_match_the_fixed_allow_list():
    unlisted, stale = g.check()
    assert not unlisted and not stale, (
        g.delta_report(unlisted, stale) + "\n" + g.FIX_ADVICE
    )


def _listed(tmp_path, sites):
    path = tmp_path / "code_silent_swallow.txt"
    path.write_text(check_allowlist.render("silent-swallow", sites), encoding="utf-8")
    return path


_BEFORE = "def f(b):\n    try:\n        return b.stop()\n    except Exception:\n        return None\n"
_AFTER = "def f(b):\n    try:\n        return b.stop()\n    except Exception:\n        return []\n"


def _scan_as(monkeypatch, text):
    monkeypatch.setattr(g, "scan", lambda rel: g.scan_text(rel, text))


def _sites(text):
    return [site for site, _ in g.scan_text("x.py", text)]


def test_a_new_swallow_not_in_the_list_fails(tmp_path, monkeypatch):
    _scan_as(monkeypatch, _BEFORE)
    unlisted, stale = g.check(("x.py",), _listed(tmp_path, []))
    assert len(unlisted) == 1 and "return None" in unlisted[0] and not stale


def test_a_listed_swallow_passes(tmp_path, monkeypatch):
    _scan_as(monkeypatch, _BEFORE)
    assert g.check(("x.py",), _listed(tmp_path, _sites(_BEFORE))) == ([], [])


def test_a_stale_entry_fails(tmp_path, monkeypatch):
    _scan_as(monkeypatch, "def f():\n    return 1\n")
    unlisted, stale = g.check(("x.py",), _listed(tmp_path, _sites(_BEFORE)))
    assert not unlisted and len(stale) == 1


def test_swapping_one_offender_for_another_is_still_an_addition(tmp_path, monkeypatch):
    """The net-zero hole: remove one swallow, add a different one, same total.
    Identity comparison reports the new one as unlisted and the old one as stale."""
    _scan_as(monkeypatch, _AFTER)
    unlisted, stale = g.check(("x.py",), _listed(tmp_path, _sites(_BEFORE)))
    assert len(unlisted) == 1 and "return []" in unlisted[0] and len(stale) == 1


def test_a_second_copy_of_a_listed_swallow_fails(tmp_path, monkeypatch):
    twice = _BEFORE + _BEFORE.replace("def f", "def f")
    _scan_as(monkeypatch, twice)
    unlisted, _ = g.check(("x.py",), _listed(tmp_path, _sites(_BEFORE)))
    assert len(unlisted) == 1


def test_the_guard_reads_no_git_trunk():
    assert not hasattr(g, "trunk_blobs") and not hasattr(g, "trunk_violations")


# --- self-tests: the guard fires on the pattern and stays quiet on a durable record ---

def _hits(src: str) -> list[str]:
    return [f"{rel}::{scope}" for (rel, scope, _), _ in g.scan_text("x.py", src)]


def test_fires_on_log_only_swallow_returning_empty():
    for empty in ("None", "False", "0", "[]", "{}", "list()", ""):
        src = (
            "def f(b):\n"
            "    try:\n        return b.stop()\n"
            "    except Exception as exc:\n"
            "        logger.warning('x %s', exc)\n"
            f"        return {empty}\n"
        )
        assert _hits(src) == ["x.py::f"], f"guard missed `return {empty}`"


def test_fires_on_bare_except_and_baseexception():
    for clause in ("except:", "except BaseException:", "except (ValueError, Exception):"):
        src = f"def f(b):\n    try:\n        return b.stop()\n    {clause}\n        return None\n"
        assert _hits(src) == ["x.py::f"], f"guard missed `{clause}`"


def test_quiet_when_handler_records_durably():
    for rec in (
        "self._record_pipeline_event('stop_read_failed', symbol=s, error=str(exc))",
        "send_owner_alert('stop unreadable', str(exc))",
        "self._alert_owner_unreadable_stop(s, exc)",
        "db.insert_pending_protection_restore(s, exc)",
        "record_stop_repair_refusal(s, str(exc))",
        "conn.execute('insert into failures values (?)', (s,))",
    ):
        src = (
            "from src.alerts import send_owner_alert\n"
            "from src.storage.heal import record_stop_repair_refusal\n\n"
            "def f(b, s):\n    try:\n        return b.stop(s)\n"
            f"    except Exception as exc:\n        {rec}\n        return None\n"
        )
        assert _hits(src) == [], f"guard wrongly fired despite durable record `{rec}`"


def test_quiet_on_reraise_narrow_except_or_non_empty_return():
    quiet = (
        "def f(b):\n    try:\n        return b.stop()\n    except Exception:\n        raise\n",
        "def f(b):\n    try:\n        return b.stop()\n    except Exception as e:\n        raise RuntimeError(e) from e\n",
        "def f(b):\n    try:\n        return b.stop()\n    except KeyError:\n        return None\n",
        "def f(b):\n    try:\n        return b.stop()\n    except Exception:\n        return SENTINEL_UNREADABLE\n",
        "def f(b):\n    try:\n        return b.stop()\n    except Exception:\n        logger.warning('x')\n",
    )
    for src in quiet:
        assert _hits(src) == [], f"guard wrongly fired on:\n{src}"


def test_keys_are_stable_across_line_shifts():
    body = "def f(b):\n    try:\n        return b.stop()\n    except Exception:\n        return None\n"
    assert _hits(body) == _hits("# moved\n\n\n" + body) == ["x.py::f"]


def test_scope_is_derived_not_stored():
    """The money-module set is computed from source at check time; nothing is pinned."""
    assert not hasattr(g, "MONEY_MODULES")
    mods = g.money_modules()
    assert "src/execution/broker.py" in mods
    for hand_list_never_named in (
        "src/protection/protected_sell.py",   # calls the SDK's close_position
        "src/stage_execution.py",             # submits orders
        "src/execution/order_idempotency.py", # submits orders
    ):
        assert hand_list_never_named in mods, hand_list_never_named
    assert all((g.ROOT / m).exists() for m in mods)


def test_guard_bites_in_a_module_the_hand_list_never_named(tmp_path, monkeypatch):
    """Plant a swallow in a derived-only module: the guard fails; take it out: green."""
    rel = "src/protection/protected_sell.py"
    assert rel in g.money_modules()
    before = (g.ROOT / rel).read_text()
    planted = before + (
        "\n\ndef _planted(b):\n    try:\n        return b.stop()\n"
        "    except Exception:\n        return None\n"
    )
    listed = _listed(tmp_path, _sites(before))
    monkeypatch.setattr(g, "scan", lambda r: g.scan_text(r, planted))
    unlisted, stale = g.check((rel,), listed)
    assert len(unlisted) == 1 and "_planted" in unlisted[0] and not stale
    monkeypatch.setattr(g, "scan", lambda r: g.scan_text(r, before))
    assert g.check((rel,), listed) == ([], [])


def test_guard_refuses_when_the_sdk_cannot_be_read(monkeypatch):
    def _no_sdk():
        raise ReferenceUnavailable("exchange SDK missing in this test")
    monkeypatch.setattr(mm, "sdk_write_methods", _no_sdk)
    assert g.main() == 2


def _tree(tmp_path, files: dict[str, str]):
    for rel, body in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body)
    return mm.derive(tmp_path, frozenset({"submit_order"}), "src")


WRITER = "def place(client, qty):\n    return client.submit_order(qty)\n"
CALLER = "from src.exec.{w} import place\n\ndef sell(c):\n    q = size(c)\n    return place(c, q)\n"
SIZER = "def size(c):\n    return 1\n"
BYSTANDER = "def run():\n    return 1\n"


def test_moving_renaming_or_adding_a_money_module_keeps_it_covered(tmp_path):
    base = {"src/exec/desk.py": WRITER, "src/sell.py": CALLER.format(w="desk"),
            "src/sizing.py": SIZER, "src/news.py": BYSTANDER}
    assert _tree(tmp_path, base) == ("src/exec/desk.py", "src/sell.py", "src/sizing.py")
    renamed = {"src/exec/orders.py": WRITER, "src/sell.py": CALLER.format(w="orders"),
               "src/sizing.py": SIZER, "src/news.py": BYSTANDER}
    assert "src/exec/orders.py" in _tree(tmp_path / "r", renamed)
    moved = dict(base)
    moved["src/deep/new/sell2.py"] = moved.pop("src/sell.py")
    assert "src/deep/new/sell2.py" in _tree(tmp_path / "m", moved)
    added = dict(base)
    added["src/brand_new.py"] = "import src.exec.desk as d\n\ndef go(c):\n    return d.place(c, 1)\n"
    assert "src/brand_new.py" in _tree(tmp_path / "a", added)


def test_generic_names_do_not_leak_money_status(tmp_path):
    """`run` defined by a writer and by a bystander: a call to `.run()` is ambiguous, not money."""
    files = {"src/exec/desk.py": "def run(client):\n    return client.submit_order(1)\n",
             "src/other.py": "def run():\n    return 1\n",
             "src/driver.py": "def go(x):\n    return x.run()\n"}
    assert _tree(tmp_path, files) == ("src/exec/desk.py",)


def test_money_modules_are_derived_from_every_tracked_production_file(monkeypatch):
    from pathlib import Path
    from scripts.guard_reference import ROOT
    paths = {p.relative_to(ROOT).as_posix() for p in mm._source_paths(Path(ROOT), None)}
    assert "main.py" in paths and any(p.startswith("ops/") for p in paths)
    assert not any(p.startswith("tests/") for p in paths)
    assert mm._source_paths(Path(ROOT), "src") == sorted((Path(ROOT) / "src").rglob("*.py"))


def test_a_root_module_calling_a_writer_is_a_money_module(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "w.py").write_text(WRITER)
    (tmp_path / "main.py").write_text("from src.w import place\n\ndef run(c):\n    return place(c, 1)\n")
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "src/w.py", "main.py"], cwd=tmp_path, check=True)
    assert "main.py" in mm.derive(tmp_path, frozenset({"submit_order"}), None)


_ALIAS_SRC = '''
from {mod} import {orig} as {alias}

def f(x):
    try:
        return x.get()
    except Exception as exc:
        {alias}("site", exc)
        return None
'''


def _alias_hits(mod: str, orig: str, alias: str) -> int:
    return len(g.scan_text("src/x.py", _ALIAS_SRC.format(mod=mod, orig=orig, alias=alias)))


def test_aliased_real_recorder_is_recognised_by_identity():
    assert _alias_hits("src.storage.events", "record_site", "_site") == 0


def test_unaliased_recorder_still_passes():
    assert _alias_hits("src.storage.events", "record_site", "record_site") == 0


def test_alias_that_resolves_to_a_non_recorder_still_fails():
    assert _alias_hits("src.util", "helper", "_site") == 1


def test_recorder_lookalike_name_bound_to_stdlib_does_not_satisfy_the_guard():
    assert _alias_hits("logging", "warning", "record_failure") == 1


# -- identity, not spelling: the three evasions a name match lets through ----

_HANDLER = (
    "def f(x):\n    try:\n        return x.get()\n"
    "    except Exception as exc:\n        {call}\n        return None\n"
)


def _evasion_hits(prelude: str, call: str) -> int:
    return len(g.scan_text("src/x.py", prelude + "\n" + _HANDLER.format(call=call)))


def test_module_alias_to_a_foreign_module_is_not_a_recorder():
    # ``import os as rec`` then ``rec.write(...)``: the attribute is spelled
    # like a durable write but binds to the stdlib, which records nothing.
    assert _evasion_hits("import os as rec", "rec.write(1, b'')") == 1
    assert _evasion_hits("import logging as record_site", "record_site.warning('x')") == 1
    # The same shape bound to a repo module is the real thing.
    assert _evasion_hits("import src.storage.events as ev", "ev.record_site('s', exc)") == 0
    assert _evasion_hits("import src.storage.events", "src.storage.events.record_site('s', exc)") == 0


def test_local_def_of_a_recorder_name_is_judged_by_its_body_not_its_name():
    shadow = "def record_site(site, exc):\n    return None\n"
    assert _evasion_hits(shadow, "record_site('s', exc)") == 1
    wrapper = (
        "from src.storage.events import record_site as _rs\n\n"
        "def record_site(site, exc):\n    _rs(site, exc)\n"
    )
    assert _evasion_hits(wrapper, "record_site('s', exc)") == 0
    # A local wrapper that only logs is a shadow however it is spelled.
    logs = "import logging\n\ndef record_site(site, exc):\n    logging.warning('x')\n"
    assert _evasion_hits(logs, "record_site('s', exc)") == 1


def test_star_imported_recorder_name_has_unknown_origin_and_does_not_count():
    assert _evasion_hits("from src.storage.events import *", "record_site('s', exc)") == 1
    assert _evasion_hits("", "record_site('s', exc)") == 1  # undefined: no origin at all
