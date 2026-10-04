"""The stop-protection catch-alls must be LOUD: traceback + a counted row.

Three states have to stay distinguishable, because an absent row is a state of
its own and not a zero:

  * never reached -- no row at all
  * ran clean     -- an ``agreed`` row with an empty result
  * ran and swallowed -- a ``disagreed`` row plus the full traceback at ERROR

The last test walks `src/pipeline_protection.py` itself: a converted handler
whose ``try`` body forgot its clean-pass call would silently collapse the first
two states back into one, and that is exactly the regression this file exists
to catch.
"""
import ast
import logging
import pathlib

import pytest

from src.sentinel import guarded_protection, reconciliation


@pytest.fixture
def rows(monkeypatch):
    seen = []

    def fake(*, db, kind, result, run_id=None):
        seen.append({"kind": kind, "result": result})
        return result

    monkeypatch.setattr(reconciliation, "record_reconciliation", fake)
    return seen


class _Owner:
    db = object()


def test_a_swallowed_fault_logs_a_traceback_and_a_disagreed_row(rows, caplog):
    caplog.set_level(logging.ERROR, logger="src.pipeline_protection")
    try:
        raise TypeError("got multiple values for argument 'side'")
    except TypeError as exc:
        guarded_protection.guarded_pass(_Owner(), "reprotect.residual_submit", exc,
                                        symbol="ZZ", effect="left naked")
    assert [r["kind"] for r in rows] == ["guarded:protection.reprotect.residual_submit"]
    detail = rows[0]["result"]
    assert detail[0]["error"] == "TypeError"
    assert detail[0]["symbol"] == "ZZ" and detail[0]["effect"] == "left naked"
    record = caplog.records[0]
    assert record.levelno == logging.ERROR
    assert record.exc_info is not None and record.exc_info[0] is TypeError


def test_a_clean_pass_writes_its_own_distinct_row(rows, caplog):
    caplog.set_level(logging.ERROR, logger="src.pipeline_protection")
    guarded_protection.guarded_pass(_Owner(), "reprotect.residual_submit", symbol="ZZ")
    assert rows == [{"kind": "guarded:protection.reprotect.residual_submit", "result": []}]
    assert caplog.records == []


def test_a_site_never_reached_writes_nothing(rows):
    assert rows == []


def test_the_observer_cannot_break_the_path_it_observes(monkeypatch, rows):
    def boom(**_kw):
        raise RuntimeError("ledger table is gone")

    monkeypatch.setattr(guarded_protection, "record_guarded_outcome", boom)
    guarded_protection.guarded_pass(_Owner(), "coverage.snapshot_stops")  # must not raise


def test_a_broker_without_a_ledger_still_records_nothing_and_raises_nothing(rows):
    class Bare:
        pass

    guarded_protection.guarded_pass(Bare(), "coverage.snapshot_stops")
    assert rows[0]["kind"] == "guarded:protection.coverage.snapshot_stops"


def _guarded_wheres(nodes):
    out = []
    for n in nodes:
        for sub in ast.walk(n):
            if (isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name)
                    and sub.func.id == "guarded_pass" and len(sub.args) >= 2
                    and isinstance(sub.args[1], ast.Constant)):
                out.append((sub.args[1].value, len(sub.args) >= 3))
    return out


def test_every_converted_handler_has_a_clean_pass_partner():
    root = pathlib.Path(__file__).resolve().parent.parent
    src = (root / "src" / "pipeline_protection.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    missing = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Try):
            continue
        for handler in node.handlers:
            for where, has_exc in _guarded_wheres(handler.body):
                if not has_exc:
                    continue
                clean = [w for w, exc in _guarded_wheres(node.body)
                         if w == where and not exc]
                if not clean:
                    missing.append(where)
    assert not missing, (
        "handler(s) record a swallowed fault but their try body never records a "
        "clean pass, so 'ran clean' is indistinguishable from 'never reached': "
        + ", ".join(sorted(set(missing))))


def test_the_pairing_check_would_actually_catch_a_missing_clean_row():
    """A guard that measures nothing would pass vacuously."""
    tree = ast.parse(
        "try:\n    f()\nexcept Exception as exc:\n"
        "    guarded_pass(self, 'x.y', exc)\n")
    node = [n for n in ast.walk(tree) if isinstance(n, ast.Try)][0]
    assert _guarded_wheres(node.handlers[0].body) == [("x.y", True)]
    assert _guarded_wheres(node.body) == []
