"""Silent-swallow ratchet: no NEW broad-except-returns-empty-records-nothing on money paths.

See scripts/silent_swallow_guard.py for the pattern, what counts as a durable
record in this codebase, and the money-module scope. The guard stores nothing:
it scans the working tree, scans the same modules on ``origin/main``, and fails
on the DELTA. When the trunk cannot be read it REFUSES rather than passes
(docs/GUARDS_WITHOUT_STORED_STATE.md).
"""
from __future__ import annotations

import ast

import pytest

from scripts import silent_swallow_guard as g
from scripts.guard_reference import ReferenceUnavailable


def test_no_new_silent_swallow_on_money_paths():
    new = g.added()
    assert not new, (
        "NEW silent swallow(s) on a money path (broad except -> empty return, "
        "nothing durable recorded):\n" + g.delta_report(new) + "\n" + g.FIX_ADVICE
    )


def test_guard_refuses_when_the_trunk_cannot_be_read(monkeypatch):
    """Rule 3: no reference means a non-zero refusal, never a silent pass."""
    def _no_trunk(paths):
        raise ReferenceUnavailable("origin/main is unreachable in this test")

    monkeypatch.setattr(g, "trunk_blobs", _no_trunk)
    with pytest.raises(ReferenceUnavailable):
        g.added()
    assert g.main() == 2


def test_nothing_stored_on_disk():
    """Rule 1: the baseline file is gone and nothing re-creates it."""
    assert not (g.ROOT / "tests" / "silent_swallow_baseline.json").exists()
    assert not hasattr(g, "load_baseline") and not hasattr(g, "shrink_baseline")


def test_delta_report_names_the_file_and_counts_what_it_gained():
    """Rule 4: the message is a delta, not an absolute count."""
    text = g.delta_report({"src/execution/broker.py::f#1": 10,
                           "src/execution/broker.py::g#1": 20})
    assert "src/execution/broker.py: gained 2 silently-swallowed exception(s)" in text
    assert "::g#1  (line 20)" in text


# --- self-tests: the guard fires on the pattern and stays quiet on a durable record ---

def _hits(src: str) -> list[str]:
    return [k for k, _ in g.scan_text("x.py", src)]


def test_fires_on_log_only_swallow_returning_empty():
    for empty in ("None", "False", "0", "[]", "{}", "list()", ""):
        src = (
            "def f(b):\n"
            "    try:\n        return b.stop()\n"
            "    except Exception as exc:\n"
            "        logger.warning('x %s', exc)\n"
            f"        return {empty}\n"
        )
        assert _hits(src) == ["x.py::f#1"], f"guard missed `return {empty}`"


def test_fires_on_bare_except_and_baseexception():
    for clause in ("except:", "except BaseException:", "except (ValueError, Exception):"):
        src = f"def f(b):\n    try:\n        return b.stop()\n    {clause}\n        return None\n"
        assert _hits(src) == ["x.py::f#1"], f"guard missed `{clause}`"


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
    assert _hits(body) == _hits("# moved\n\n\n" + body) == ["x.py::f#1"]


def test_scoped_modules_are_all_tracked_paths():
    missing = sorted(m for m in g.MONEY_MODULES if not (g.ROOT / m).exists())
    assert not missing, "MONEY_MODULES names paths that do not exist: " + ", ".join(missing)
