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
OWNER_FACING = ("notifier", "trader_feed", "log_health", "inflight", "market_session")
_CLOCK = re.compile(r"%-?[HIl]|%[pP]|%T|%R|%X|%c")
_DATE = re.compile(r"%-?[dejmy]|%[YyBbAaDFxc]")
_BROWSER_CLOCK_CALLS = ("formatEasternTime(", "Intl.DateTimeFormat(", ".toLocaleString(")
_BROWSER_CLOCK = re.compile(r"(?:[{,]\s*)[\"']?(?:hour|timeStyle)[\"']?\s*:")
_BROWSER_DATE = re.compile(r"(?:[{,]\s*)[\"']?(?:year|month|day|dateStyle)[\"']?\s*:")
_AXIS_CLOCK_LABEL = "components/PriceChartPanel.tsx"
_AXIS_CLOCK_FUNCTION = "easternTickMarkFormatter"
_AXIS_CLOCK_CALLS = {
    'formatEasternTime(time,{hour:"numeric",minute:"2-digit"})',
    'formatEasternTime(time,{hour:"numeric",minute:"2-digit",second:"2-digit"})',
}


def _bare_clock(fmt: str) -> bool:
    """The rule is a clock WITHOUT a date; a format carrying both is allowed."""
    return bool(_CLOCK.search(fmt)) and not _DATE.search(fmt)


def _py_files(name):
    """A package directory OR a single module; never silently nothing."""
    pkg_dir, one_file = SRC / name, SRC / f"{name}.py"
    files = sorted(pkg_dir.rglob("*.py")) if pkg_dir.is_dir() else [one_file]
    assert files and all(f.exists() for f in files), f"owner-facing {name} not found"
    return files


def _balanced_call(source: str, start: int) -> str:
    """Return one JS/TS call, including nested calls and quoted strings."""
    opening = source.find("(", start)
    assert opening >= 0
    depth = 0
    quote = None
    escaped = False
    for pos in range(opening, len(source)):
        char = source[pos]
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            continue
        if char in ("'", '"', "`"):
            quote = char
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return source[start:pos + 1]
    return source[start:]


def _named_function_bounds(source: str, name: str) -> tuple[int, int] | None:
    """Locate one ordinary JS/TS function without trusting nearby comments."""
    start = source.find(f"function {name}(")
    if start < 0:
        return None
    opening = source.find("{", start)
    if opening < 0:
        return None
    depth = 0
    quote = None
    escaped = False
    for pos in range(opening, len(source)):
        char = source[pos]
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            continue
        if char in ("'", '"', "`"):
            quote = char
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return start, pos + 1
    return None


def _is_intentional_axis_clock(source: str, label: str, start: int, call: str) -> bool:
    """Exempt only the two coordinate calls in the real axis formatter."""
    if label != _AXIS_CLOCK_LABEL:
        return False
    bounds = _named_function_bounds(source, _AXIS_CLOCK_FUNCTION)
    compact_call = re.sub(r"\s+", "", call)
    return bool(bounds and bounds[0] <= start < bounds[1] and compact_call in _AXIS_CLOCK_CALLS)


def _browser_bare_clock_offenders(source: str, label: str) -> list[str]:
    """Find browser clock calls whose literal options carry no date.

    The two known chart-axis coordinate calls are the sole exception: repeating
    the date on every intraday tick makes the axis unreadable. Their exemption
    is bound to their file, function and exact call shape, never to copyable
    prose or comments.
    """
    offenders = []
    for marker in _BROWSER_CLOCK_CALLS:
        offset = 0
        while (start := source.find(marker, offset)) >= 0:
            call = _balanced_call(source, start)
            line_no = source.count("\n", 0, start) + 1
            if (
                _BROWSER_CLOCK.search(call)
                and not _BROWSER_DATE.search(call)
                and not _is_intentional_axis_clock(source, label, start, call)
            ):
                offenders.append(f"{label}:{line_no}")
            offset = start + len(marker)
    if "toLocaleTimeString" in source:
        line_no = source.count("\n", 0, source.index("toLocaleTimeString")) + 1
        offenders.append(f"{label}:{line_no}")
    return offenders


def test_shared_formatter_emits_date_then_time():
    assert fmt_time_12h(datetime(2026, 10, 4, 13, 5)) == "2026-10-04 1:05 PM ET"
    assert fmt_time_12h(datetime(2026, 10, 4, 0, 7)) == "2026-10-04 12:07 AM ET"


def test_no_owner_facing_module_formats_a_clock_time_itself():
    offenders = []
    for pkg in OWNER_FACING:
        for path in _py_files(pkg):
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
                        and _bare_clock(arg.value)
                    ):
                        offenders.append(f"{path.relative_to(SRC)}:{node.lineno}")
            # f-string format specs such as f"{dt:%H:%M}"
            for node in ast.walk(tree):
                if isinstance(node, ast.FormattedValue) and node.format_spec:
                    spec = ast.unparse(node.format_spec)
                    if _bare_clock(spec):
                        offenders.append(f"{path.relative_to(SRC)}:{node.lineno}")
    assert offenders == [], (
        f"owner-facing clock time formatted without the shared formatter: {offenders}"
    )


def test_health_report_window_carries_the_date_even_within_one_day():
    from datetime import timezone
    from types import SimpleNamespace

    from src.log_health import _window_words

    rep = SimpleNamespace(
        window_start=datetime(2026, 10, 4, 14, 0, tzinfo=timezone.utc),
        window_end=datetime(2026, 10, 4, 18, 0, tzinfo=timezone.utc),
    )
    words = _window_words(rep)
    assert words.count("2026-10-04") == 2 and "today" not in words


def test_session_edges_carry_the_date():
    from src.market_session import _stamp
    from src.trading_calendar import ET

    assert _stamp(datetime(2026, 10, 5, 9, 30, tzinfo=ET)) == "2026-10-05 9:30 AM ET"


def test_dashboard_never_renders_a_bare_clock_time():
    root = SRC.parent / "frontend" / "src"
    offenders = []
    for path in sorted(root.rglob("*.ts*")):
        if ".test." not in path.name:
            offenders.extend(_browser_bare_clock_offenders(
                path.read_text(), str(path.relative_to(root)),
            ))
    legacy_path = SRC / "api" / "static" / "app.js"
    offenders.extend(_browser_bare_clock_offenders(
        legacy_path.read_text(), str(legacy_path.relative_to(SRC.parent)),
    ))
    assert offenders == [], offenders


def test_dashboard_guard_detects_hour_only_intl_formatting_in_prose():
    direct = "new Intl.DateTimeFormat('en-US', { hour: 'numeric', minute: '2-digit' })"
    wrapped = "`as of ${formatEasternTime(now, { hour: 'numeric', minute: '2-digit' })}`"
    dated = "formatEasternTime(now, { month: 'short', day: 'numeric', hour: 'numeric' })"
    assert _browser_bare_clock_offenders(direct, "direct.ts") == ["direct.ts:1"]
    assert _browser_bare_clock_offenders(wrapped, "wrapped.ts") == ["wrapped.ts:1"]
    assert _browser_bare_clock_offenders(dated, "dated.ts") == []


def test_dashboard_guard_rejects_time_style_without_a_date():
    for style in ("short", "medium", "long", "full"):
        direct = f"new Intl.DateTimeFormat('en-US', {{ timeStyle: '{style}' }})"
        locale = f"now.toLocaleString('en-US', {{ timeStyle: '{style}' }})"
        assert _browser_bare_clock_offenders(direct, "direct.ts") == ["direct.ts:1"]
        assert _browser_bare_clock_offenders(locale, "locale.ts") == ["locale.ts:1"]
    quoted_key = 'now.toLocaleString("en-US", { "timeStyle": "short" })'
    assert _browser_bare_clock_offenders(quoted_key, "quoted.ts") == ["quoted.ts:1"]


def test_dashboard_guard_allows_date_style_or_explicit_date_fields():
    styled = "new Intl.DateTimeFormat('en-US', { dateStyle: 'medium', timeStyle: 'short' })"
    explicit = "now.toLocaleString('en-US', { month: 'short', day: 'numeric', timeStyle: 'medium' })"
    quoted = 'now.toLocaleString("en-US", { "dateStyle": "short", "timeStyle": "short" })'
    assert _browser_bare_clock_offenders(styled, "styled.ts") == []
    assert _browser_bare_clock_offenders(explicit, "explicit.ts") == []
    assert _browser_bare_clock_offenders(quoted, "quoted.ts") == []


def test_axis_exemption_is_structural_and_cannot_be_borrowed_by_prose():
    forged = """// owner-clock-axis-coordinate
const label = `as of ${formatEasternTime(now, { hour: "numeric", minute: "2-digit" })}`;
"""
    copied_call = 'formatEasternTime(time, { hour: "numeric", minute: "2-digit" })'
    assert _browser_bare_clock_offenders(forged, _AXIS_CLOCK_LABEL) == [
        f"{_AXIS_CLOCK_LABEL}:2",
    ]
    assert _browser_bare_clock_offenders(copied_call, _AXIS_CLOCK_LABEL) == [
        f"{_AXIS_CLOCK_LABEL}:1",
    ]


def test_only_the_two_real_axis_calls_receive_the_structural_exemption():
    root = SRC.parent / "frontend" / "src"
    source = (root / _AXIS_CLOCK_LABEL).read_text()
    exempt_calls = []
    offset = 0
    marker = "formatEasternTime("
    while (start := source.find(marker, offset)) >= 0:
        call = _balanced_call(source, start)
        if _is_intentional_axis_clock(source, _AXIS_CLOCK_LABEL, start, call):
            exempt_calls.append(re.sub(r"\s+", "", call))
        offset = start + len(marker)
    assert set(exempt_calls) == _AXIS_CLOCK_CALLS
    assert len(exempt_calls) == 2
