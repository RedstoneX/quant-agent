"""No owner alert may leave src/ by a side door.

An owner alert goes through the funnel (`send_owner_alert` /
`send_owner_alert_with_outcome`): CRITICAL journal line, P&L header, bounded
retry, then one counted durable row carrying the alert's own kind and run id.
Two side doors are refused: a bare `notifier.send(...)`, and reaching past the
funnel straight into the delivery layer (`deliver_with_retry` /
`deliver_with_outcome`), which skips the journal line and the header.

The scheduler's routine session summary is the one bare send left, and it is a
report, not an alert. There is no exception to the second rule: the two callers
that needed an injected notifier, a per-alert kind and a run id were the reason
the funnel grew to carry all three, so they go through it like everyone else.
"""

from __future__ import annotations

import ast
from pathlib import Path

from src.notifier import SUPPRESSED
from src.notifier.owner_alert import send_owner_alert_with_outcome
from src.notifier.owner_alert_delivery import (
    MAX_ATTEMPTS,
    deliver_with_outcome,
    deliver_with_retry,
)

SRC = Path(__file__).resolve().parent.parent / "src"
ROUTINE_REPORT_SENDS = {"scheduler.py": 1}


def _bare_sends(root: Path = SRC) -> dict[str, int]:
    found: dict[str, int] = {}
    for path in root.rglob("*.py"):
        rel = path.relative_to(root)
        if rel.parts[0] == "notifier":
            continue
        for node in ast.walk(ast.parse(path.read_text())):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in ("send", "send_message")
            ):
                continue
            recv = ast.unparse(node.func.value).lower()
            if "notifier" in recv:
                found[str(rel)] = found.get(str(rel), 0) + 1
    return found


def _delivery_layer_calls(root: Path = SRC) -> dict[str, int]:
    """Direct uses of the delivery layer from outside the notifier package."""
    found: dict[str, int] = {}
    for path in root.rglob("*.py"):
        rel = path.relative_to(root)
        if rel.parts[0] == "notifier":
            continue
        for node in ast.walk(ast.parse(path.read_text())):
            name = None
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                name = node.func.id
            elif isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    if alias.name in DELIVERY_LAYER:
                        name = alias.name
            if name in DELIVERY_LAYER:
                found[str(rel)] = found.get(str(rel), 0) + 1
    return found


DELIVERY_LAYER = {"deliver_with_retry", "deliver_with_outcome"}
# The funnel's own module, by path. The ONLY place a notifier may be built.
FUNNEL_MODULE = Path("notifier") / "owner_alert_funnel.py"


def _direct_constructions(root: Path = SRC) -> list[str]:
    """Every `TelegramNotifier(...)` call in src/ outside the funnel module."""
    found: list[str] = []
    for path in root.rglob("*.py"):
        rel = path.relative_to(root)
        if rel == FUNNEL_MODULE:
            continue
        for node in ast.walk(ast.parse(path.read_text())):
            if not isinstance(node, ast.Call):
                continue
            f = node.func
            name = f.id if isinstance(f, ast.Name) else getattr(f, "attr", "")
            if name == "TelegramNotifier":
                found.append(f"{rel}:{node.lineno}")
    return found


def test_only_the_funnel_module_constructs_a_notifier():
    assert _direct_constructions() == []


def test_a_direct_construction_is_refused(tmp_path):
    """Failing case: plant a direct construction and watch it be caught.

    The probe lives in a private tree, never in the shared src/, so a
    parallel scanner can not see it appear and vanish.
    """
    (tmp_path / "_guard_probe_construct.py").write_text(
        "from src.notifier import TelegramNotifier\ndef f():\n    return TelegramNotifier()\n"
    )
    assert _direct_constructions(tmp_path) == ["_guard_probe_construct.py:3"]


def test_the_factory_builds_through_the_patched_class(monkeypatch):
    import src.notifier.owner_alert_funnel as funnel
    from src.notifier.owner_alert_funnel import build_default_notifier

    monkeypatch.setattr(funnel, "TelegramNotifier", lambda **kw: ("built", kw))
    assert build_default_notifier(a=1) == ("built", {"a": 1})


def test_no_new_bare_notifier_send_in_src():
    assert _bare_sends() == ROUTINE_REPORT_SENDS


def test_nothing_reaches_past_the_funnel_into_the_delivery_layer():
    assert _delivery_layer_calls() == {}


def test_a_bare_direct_send_is_still_refused(tmp_path):
    """The guard must still bite: plant a side door and watch it be caught."""
    (tmp_path / "_guard_probe_side_door.py").write_text("def f(notifier):\n    return notifier.send('x')\n")
    assert _bare_sends(tmp_path) == {"_guard_probe_side_door.py": 1}


def test_delivery_layer_guard_bites_too(tmp_path):
    (tmp_path / "_guard_probe_delivery.py").write_text(
        "from src.notifier.owner_alert_delivery import deliver_with_retry\n"
        "def f(n):\n    return deliver_with_retry(n, 'x')\n"
    )
    assert _delivery_layer_calls(tmp_path) == {"_guard_probe_delivery.py": 2}


def test_a_send_that_never_lands_is_counted_and_durable(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda _s: None)
    f = _Fake([False] * 10)
    assert send_owner_alert_with_outcome(
        "heading\nbody",
        notifier=f,
        kind="no_stop_at_all",
        run_id="r-77",
    ) == (False, False)
    assert len(f.recorded) == 1
    assert f.recorded[0]["status"] == "owner_alert_undelivered"


def test_the_run_id_and_kind_reach_the_record(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda _s: None)
    f = _Fake([False] * 10)
    send_owner_alert_with_outcome(
        "heading\nbody",
        notifier=f,
        kind="no_stop_at_all",
        run_id="r-77",
    )
    assert f.recorded[0]["run_id"] == "r-77"
    assert f.recorded[0]["kind"] == "no_stop_at_all"


def test_the_retry_budget_is_bounded(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda _s: None)
    f = _Fake([False] * 50)
    send_owner_alert_with_outcome("heading\nbody", notifier=f, max_attempts=1)
    assert f.calls == 1
    g = _Fake([False] * 50)
    send_owner_alert_with_outcome("heading\nbody", notifier=g)
    assert 1 < g.calls <= MAX_ATTEMPTS


def test_suppression_survives_the_funnel():
    f = _Fake([SUPPRESSED])
    assert send_owner_alert_with_outcome("heading\nbody", notifier=f) == (
        False,
        True,
    )
    assert f.calls == 1 and not f.recorded


def test_the_injected_notifier_is_the_one_used():
    f = _Fake([True])
    assert send_owner_alert_with_outcome("heading\nbody", notifier=f) == (
        True,
        False,
    )
    assert f.calls == 1


class _Fake:
    enabled = True

    def __init__(self, outcomes):
        self.outcomes, self.calls, self.recorded = list(outcomes), 0, []

    def send(self, text, **kw):
        self.calls += 1
        self.kwargs = kw
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
