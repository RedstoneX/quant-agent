"""Item 231: every owner-facing timestamp carries a date, not just a time.

The owner reads alerts on a phone hours after they fire; a bare clock time
cannot be placed. The one shared formatter emits date + time, and no owner-
facing module may format a clock time by any other route.
"""

import ast
import re
from datetime import datetime
from pathlib import Path

from src.notifier.sections import fmt_time_12h

SRC = Path(__file__).resolve().parent.parent / "src"
OWNER_FACING = ("notifier", "trader_feed", "log_health", "inflight")
_CLOCK = re.compile(r"%-?[HIl]|%[pP]|%T|%R|%X|%c")


def test_shared_formatter_emits_date_then_time():
    assert fmt_time_12h(datetime(2026, 10, 4, 13, 5)) == "2026-10-04 1:05 PM ET"
    assert fmt_time_12h(datetime(2026, 10, 4, 0, 7)) == "2026-10-04 12:07 AM ET"


def test_no_owner_facing_module_formats_a_clock_time_itself():
    offenders = []
    for pkg in OWNER_FACING:
        for path in sorted((SRC / pkg).rglob("*.py")):
            tree = ast.parse(path.read_text())
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                name = getattr(func, "attr", getattr(func, "id", ""))
                if name not in ("strftime", "format"):
                    continue
                for arg in node.args[:1]:
                    if (
                        isinstance(arg, ast.Constant)
                        and isinstance(arg.value, str)
                        and _CLOCK.search(arg.value)
                    ):
                        offenders.append(f"{path.relative_to(SRC)}:{node.lineno}")
            # f-string format specs such as f"{dt:%H:%M}"
            for node in ast.walk(tree):
                if isinstance(node, ast.FormattedValue) and node.format_spec:
                    spec = ast.unparse(node.format_spec)
                    if _CLOCK.search(spec):
                        offenders.append(f"{path.relative_to(SRC)}:{node.lineno}")
    assert offenders == [], (
        f"owner-facing clock time formatted without the shared formatter: {offenders}"
    )
