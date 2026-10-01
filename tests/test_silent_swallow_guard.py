"""Silent-swallow ratchet: no NEW broad-except-returns-empty-records-nothing on money paths.

See scripts/silent_swallow_guard.py for the pattern, what counts as a durable
record in this codebase, and the money-module scope. Baseline in
tests/silent_swallow_baseline.json may only shrink.
"""
from __future__ import annotations

import ast

from scripts import silent_swallow_guard as g

_FIX = "PYTHONPATH=. .venv/bin/python -m scripts.silent_swallow_guard --shrink-baseline"


def test_no_new_silent_swallow_on_money_paths():
    now = g.violations()
    new = sorted(set(now) - g.load_baseline())
    assert not new, (
        "NEW silent swallow(s) on a money path (broad except -> empty return, nothing durable recorded):\n"
        + "\n".join(f"  {k}  (line {now[k]})" for k in new)
        + "\nA swallowed broker error that returns None/False/[] is read by the caller as "
        "'nothing found' (get_current_stop_price -> 'no stop to adjust'). "
        "Fix: re-raise, or record the failure durably before returning (event journal, "
        "insert_*/save_* row, send_owner_alert / _alert_owner_*, record_*). "
        "A logger call is NOT a record. Do NOT add it to tests/silent_swallow_baseline.json; "
        "that list may only shrink."
    )


def test_baseline_only_shrinks():
    stale = sorted(g.load_baseline() - set(g.violations()))
    assert not stale, (
        "These baseline silent swallows are gone (good, you fixed them): "
        + ", ".join(stale)
        + f"\nFix: run `{_FIX}` and commit tests/silent_swallow_baseline.json so the ratchet tightens."
    )


def test_baseline_keys_point_at_scoped_modules():
    bad = sorted(k for k in g.load_baseline() if k.split("::")[0] not in g.MONEY_MODULES)
    assert not bad, "Baseline keys outside the money-module scope: " + ", ".join(bad)


# --- self-tests: the guard fires on the pattern and stays quiet on a durable record ---

def _hits(src: str) -> list[str]:
    f = g._Finder("x.py")
    f.visit(ast.parse(src))
    return [k for k, _ in f.hits]


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
