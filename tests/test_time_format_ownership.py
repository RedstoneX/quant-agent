"""The timestamp formatter is neutral while notifier imports stay compatible."""

from __future__ import annotations

import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from src.time_format import fmt_time_12h

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    ("moment", "expected"),
    [
        (datetime(2026, 10, 4, 0, 7), "2026-10-04 12:07 AM ET"),
        (datetime(2026, 10, 4, 9, 3), "2026-10-04 9:03 AM ET"),
        (datetime(2026, 10, 4, 12, 0), "2026-10-04 12:00 PM ET"),
        (datetime(2026, 10, 4, 13, 5), "2026-10-04 1:05 PM ET"),
    ],
)
def test_exact_output_is_unchanged(moment: datetime, expected: str) -> None:
    assert fmt_time_12h(moment) == expected


def test_timezone_conversion_remains_the_callers_job() -> None:
    utc_moment = datetime(2026, 10, 4, 18, 5, tzinfo=timezone.utc)
    assert fmt_time_12h(utc_moment) == "2026-10-04 6:05 PM ET"


def test_notifier_public_imports_are_the_neutral_function() -> None:
    from src.notifier import fmt_time_12h as notifier_format
    from src.notifier.sections import fmt_time_12h as sections_format

    assert sections_format is fmt_time_12h
    assert notifier_format is fmt_time_12h


@pytest.mark.parametrize("module", ["src.inflight", "src.market_session"])
def test_neutral_consumers_do_not_load_notifier_or_requests(module: str) -> None:
    script = (
        "import importlib, sys; "
        f"importlib.import_module({module!r}); "
        "assert 'src.notifier' not in sys.modules, sorted("
        "n for n in sys.modules if n.startswith('src.notifier')); "
        "assert 'requests' not in sys.modules"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr or result.stdout
