"""No owner alert may leave src/ by a bare `notifier.send(...)` side door.

An owner alert goes through the delivery funnel (retry, then one counted
undelivered row). The scheduler's routine session summary is the one bare
send left, and it is a report, not an alert.
"""
from __future__ import annotations

import ast
from pathlib import Path

from src.notifier import SUPPRESSED
from src.notifier.owner_alert_delivery import (
    deliver_with_outcome, deliver_with_retry,
)

SRC = Path(__file__).resolve().parent.parent / "src"
ROUTINE_REPORT_SENDS = {"scheduler.py": 1}


def _bare_sends() -> dict[str, int]:
    found: dict[str, int] = {}
    for path in SRC.rglob("*.py"):
        rel = path.relative_to(SRC)
        if rel.parts[0] == "notifier":
            continue
        for node in ast.walk(ast.parse(path.read_text())):
            if not (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr in ("send", "send_message")):
                continue
            recv = ast.unparse(node.func.value).lower()
            if "notifier" in recv:
                found[str(rel)] = found.get(str(rel), 0) + 1
    return found


def test_no_new_bare_notifier_send_in_src():
    assert _bare_sends() == ROUTINE_REPORT_SENDS


class _Fake:
    enabled = True

    def __init__(self, outcomes):
        self.outcomes, self.calls, self.recorded = list(outcomes), 0, []

    def send(self, text, **kw):
        self.calls += 1
        return self.outcomes.pop(0)

    def _safe_record_send(self, **kw):
        self.recorded.append(kw)


def test_suppressed_is_settled_with_a_single_send(monkeypatch):
    f = _Fake([SUPPRESSED])
    assert deliver_with_outcome(f, "x") == (False, True)
    assert f.calls == 1 and not f.recorded


def test_failure_is_retried_then_counted_once(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda _s: None)
    f = _Fake([False] * 10)
    assert deliver_with_retry(f, "x") is False
    assert f.calls > 1 and len(f.recorded) == 1
    assert f.recorded[0]["status"] == "owner_alert_undelivered"
