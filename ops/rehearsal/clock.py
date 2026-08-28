"""Freeze every clock the session reads, so a rehearsal is reproducible.

Determinism is not a nicety here — it is what lets a rehearsal be a test.
Run the same fixture twice and you must get the same report, or a red result
means nothing.

The pipeline reads the clock in more places than it looks. `et_today()` picks
the yfinance window, the checkpoint filename, the earnings/news/macro cache
keys, the trading-day lookup and the PM's "prior evening insights" cutoff.
`_et_day_and_utc_bounds()` inside the cost circuit picks which budget day a
call is charged to. `RunContext.start()` mints a random run id.

Two of those need care:

  * **Rebound names.** `src.util.time` re-exports `et_now`/`et_today` from
    `src.trading_calendar`, and a dozen modules did `from src.util.time import
    et_today` at import time — each of which holds its own reference. Patching
    the definition site alone would leave every one of them live. So the freeze
    walks `sys.modules` and rebinds the name wherever it is bound to the
    original function.

  * **The cost circuit's own clock.** `_et_day_and_utc_bounds` calls
    `datetime.now(timezone.utc)` directly rather than going through the
    trading calendar, so it needs its own patch. Without it a rehearsal of a
    past morning would post its spending against today's budget day and
    compare it to the wrong accumulated total.

`RunContext.start` is patched to a deterministic run id. The rehearsal
deliberately does NOT reuse the historical run's id: replaying into the same
id would double-count that session's recorded spending in the ledger it is
trying to measure.
"""

from __future__ import annotations

import sys
from contextlib import ExitStack, contextmanager
from datetime import datetime, time as dt_time, timezone


@contextmanager
def frozen_clock(now_et: datetime, *, run_id: str):
    """Pin the session clock to `now_et` and the run id to `run_id`."""
    import src.trading_calendar as calendar_module

    if now_et.tzinfo is None:
        now_et = now_et.replace(tzinfo=calendar_module.ET)
    frozen_date = now_et.date()

    def et_now():
        return now_et

    def et_today():
        return frozen_date

    stack = ExitStack()
    try:
        stack.enter_context(_rebind_everywhere("et_now", calendar_module.et_now, et_now))
        stack.enter_context(_rebind_everywhere("et_today", calendar_module.et_today, et_today))

        # The trading calendar itself must be rebound even though it is the
        # definition site — `session_date_key` calls the module global.
        stack.enter_context(_set_attr(calendar_module, "et_now", et_now))
        stack.enter_context(_set_attr(calendar_module, "et_today", et_today))
        import src.util.time as time_shim

        stack.enter_context(_set_attr(time_shim, "et_now", et_now))
        stack.enter_context(_set_attr(time_shim, "et_today", et_today))

        stack.enter_context(_freeze_budget_day(now_et))
        stack.enter_context(_pin_run_id(run_id))
        yield now_et
    finally:
        stack.close()


@contextmanager
def _set_attr(module, name: str, value):
    had = hasattr(module, name)
    previous = getattr(module, name, None)
    setattr(module, name, value)
    try:
        yield
    finally:
        if had:
            setattr(module, name, previous)
        else:
            delattr(module, name)


@contextmanager
def _rebind_everywhere(name: str, original, replacement):
    """Rebind `name` in every loaded `src.*` module that holds `original`."""
    patched: list = []
    for module_name, module in list(sys.modules.items()):
        if not module_name.startswith("src.") and module_name != "src":
            continue
        if module is None:
            continue
        if getattr(module, name, None) is original:
            setattr(module, name, replacement)
            patched.append(module)
    try:
        yield
    finally:
        for module in patched:
            setattr(module, name, original)


@contextmanager
def _freeze_budget_day(now_et: datetime):
    """Pin the cost circuit's ET budget-day window to the rehearsed day."""
    import src.cost_circuit as circuit_module

    original = circuit_module._et_day_and_utc_bounds
    et = circuit_module._ET
    local = now_et.astimezone(et)
    day = local.date()
    start = datetime.combine(day, dt_time.min, tzinfo=et).astimezone(timezone.utc)
    end = datetime.combine(day, dt_time.max, tzinfo=et).astimezone(timezone.utc)
    frozen = (
        day.isoformat(),
        start.strftime("%Y-%m-%d %H:%M:%S"),
        end.strftime("%Y-%m-%d %H:%M:%S"),
    )

    def bounds(now=None):
        if now is None:
            return frozen
        return original(now)

    circuit_module._et_day_and_utc_bounds = bounds
    try:
        yield frozen
    finally:
        circuit_module._et_day_and_utc_bounds = original


@contextmanager
def _pin_run_id(run_id: str):
    """Give the rehearsed session a stable, obviously-rehearsal run id."""
    import src.pipeline_context as context_module

    original = context_module.RunContext.start

    @classmethod
    def start(cls, session):
        ctx = original.__func__(cls, session)
        ctx.run_id = run_id
        return ctx

    context_module.RunContext.start = start
    try:
        yield run_id
    finally:
        context_module.RunContext.start = original
