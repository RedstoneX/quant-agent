"""One way to freeze the desk's day in a test, so there is only one to get wrong.

THREE CLOCK BUGS IN ONE NIGHT produced this. A fixture dating by the
runner's local day while the product dated by the exchange day; a fixture
freezing the ET day while SQLite kept its own real clock. Both are the same
mistake: a test pinned ONE of the two clocks a module reads, and the two
disagreed near a day boundary. Every such failure looks like a product
regression, appears only for part of the day, and costs an investigation.

A module that wants to be freezable exposes a module-level `_now_utc()` and
reads it everywhere -- including for whatever its storage layer uses as
"now". `freeze_desk_day` replaces that one function, so there is no second
clock left to disagree with it.

Noon ET is deliberate: the far side of a day boundary in both directions,
so neither a backdated stamp nor a forward-dated one silently lands on a
different exchange day than the test's own "today".
"""

from __future__ import annotations

from datetime import datetime, time as dt_time, timezone
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")


def freeze_desk_day(monkeypatch, *modules, on: datetime | None = None) -> datetime:
    """Pin every given module's `_now_utc` to noon ET and return that instant.

    `on` defaults to the real current instant, so the frozen day is still
    today's real exchange day -- only the time within it is pinned.
    """
    reference = (on or datetime.now(timezone.utc)).astimezone(ET).date()
    pinned = datetime.combine(reference, dt_time(12, 0), tzinfo=ET).astimezone(timezone.utc)
    for module in modules:
        assert hasattr(module, "_now_utc"), (
            f"{module.__name__} has no _now_utc() to freeze; a module that is "
            "not freezable through one function has more than one clock"
        )
        monkeypatch.setattr(module, "_now_utc", lambda: pinned)
    return pinned
