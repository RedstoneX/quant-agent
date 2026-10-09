"""Cost lines: session/day AI cost, OpenRouter balance, margin interest.

Moved verbatim from the former src/notifier.py; the package re-exports it.
"""

from __future__ import annotations

import os

from src.notifier.base import (
    _DB_PATH,
    _REHEARSAL_MODE,
    logger,
)
from src.notifier.sections import (
    describe_ai_cost,
)


def _session_cost_line(run_id: str | None) -> str | None:
    """Return '💵 cost: $X.XX (N calls)' for a session's run_id, or
    None when the lookup can't produce a clean answer.

    Reasons for returning None (and not displaying anything):
      - No run_id (mode didn't set one — e.g. live scheduler startup ping)
      - DB file not at default path (test environments)
      - No agent_log rows for this run_id (session crashed before any
        LLM call landed — error path notification already covers this)
      - Some row has cost_usd=NULL (model missing from cost_table) —
        showing partial sum would understate; better to render nothing
        and let the operator notice the gap when they hit the
        agent_logs table directly.
    """
    if not run_id or run_id == "?":
        return None
    try:
        import sqlite3

        if not _DB_PATH.exists():
            return None
        conn = sqlite3.connect(str(_DB_PATH))
        try:
            rows = conn.execute(
                "SELECT cost_usd FROM agent_logs WHERE run_id = ?",
                (run_id,),
            ).fetchall()
        finally:
            conn.close()
    except Exception as exc:
        logger.warning("session cost lookup failed for %s: %s", run_id, exc)
        return None
    if not rows:
        return None
    if any(r[0] is None for r in rows):
        # Unknown model in the pricing table for at least one call —
        # cannot honestly sum. Say so; never show a partial total as though
        # it were the whole, and never show a fabricated figure.
        return "\U0001f4b5 AI cost for this run: not available — one of the models used has no price on file"
    # The provider-request count that used to sit in brackets here is gone
    # (owner review, 2026-09-18). It is an implementation detail, it is not
    # a number he can act on, and it had already been removed from the
    # evening message.
    return "\U0001f4b5 " + describe_ai_cost(sum(float(r[0]) for r in rows))


def _day_cost_line() -> str | None:
    """'📅 today: $X.XX of $Y.YY daily limit (N%)', or None.

    The per-session line above answers "what did THIS session cost". It does
    not answer "how close am I to the brake", which is the question that
    matters on a day with several sessions — and the answer lived only on the
    dashboard. On 2026-08-31 the desk hit that brake twice and the Telegram
    messages never once showed how near it was.

    NOTE this is QAMC's OWN self-imposed daily cap, not money. Reaching it
    stops paid analysis for the day but costs nothing; that is the point of
    it. The separate balance line reports actual prepaid funds. Two different
    numbers, deliberately labelled differently, because conflating them was
    already possible and would be expensive.

    Reads the same ledger the circuit enforces against, so it can never
    disagree with the brake. Never raises.
    """
    try:
        import sqlite3

        if not _DB_PATH.exists():
            return None
        from src.trading_calendar import et_now

        day = et_now().strftime("%Y-%m-%d")
        conn = sqlite3.connect(str(_DB_PATH))
        try:
            row = conn.execute(
                "SELECT COALESCE(baseline_cost_usd,0) + COALESCE(incremental_cost_usd,0) "
                "FROM llm_budget_days WHERE day = ?",
                (day,),
            ).fetchone()
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001 — never break the alert
        logger.warning("daily cost lookup failed: %s", exc)
        return None
    if row is None or row[0] is None:
        return None
    spent = float(row[0])
    limit = _daily_cost_limit()
    # In words, not a row of zeros (owner review, 2026-09-18). A day on
    # which nothing paid has run yet is the common case before the open,
    # and "$0.00 of $2.75 daily limit (0%)" said nothing he could use. The
    # percentage is dropped: it restated the two figures already on the
    # line. Both figures are read, never estimated.
    if spent <= 0:
        if not limit:
            return "\U0001f4c5 Spent today: nothing yet"
        return f"\U0001f4c5 Spent today: nothing yet, against a ${limit:,.2f} cap for the day"
    if not limit:
        return f"\U0001f4c5 Spent today: ${spent:,.2f} so far"
    return f"\U0001f4c5 Spent today: ${spent:,.2f} of the ${limit:,.2f} cap for the day"


def _daily_cost_limit() -> float | None:
    """The configured daily cap, or None if it cannot be read."""
    try:
        from src.config import load_config

        cfg = load_config("config/settings.yaml")
        return float(cfg.llm_cost_circuit.daily_cost_limit_usd)
    except Exception:  # noqa: BLE001
        return None


def _persist_margin_interest_daily(
    trading_day,
    debit_balance: float,
    rate_pct: float,
    daily_usd: float,
    days_charged: int,
    period_usd: float,
    source: str = "estimate",
) -> None:
    """Write today's margin-interest row so the cumulative view
    (`src.margin_interest.compute_cumulative_margin_interest`) has
    something to sum for this-week/current-month/all-time — `daily_pnl`
    never stored cash/debit, so THIS table is the only historical record of
    the desk's overnight debit balance from here forward.

    Delegates the actual write to `Database.insert_margin_interest_daily`
    — that method (and its `ON CONFLICT(date) DO UPDATE` upsert) is the
    ONE place this schema's insert logic is allowed to live, so the live
    morning write and the historical backfill
    (`Database.backfill_margin_interest_daily`,
    `scripts/backfill_margin_interest_history.py`) can never drift apart
    on what a row looks like. (Before 2026-09-24 this function duplicated
    that INSERT/`CREATE TABLE` by hand with its own `sqlite3` connection —
    consolidated here; `Database()` already brings up the same table via
    `initialize()`.)

    A short-lived `Database` instance is opened and closed for this one
    write, same "never raises, never blocks the alert" contract as
    before: a persistence failure here must not be able to stop a
    Telegram alert that already has its lines built.
    """
    try:
        from src.storage.db import Database

        _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        db = Database(str(_DB_PATH))
        try:
            db.initialize()
            db.insert_margin_interest_daily(
                str(trading_day),
                debit_balance,
                rate_pct,
                daily_usd,
                days_charged,
                period_usd,
                source,
            )
        finally:
            db.close()
    except Exception as exc:  # noqa: BLE001 — persistence is a nicety, not the alert
        logger.warning("margin interest daily persistence failed: %s", exc)


def _read_margin_interest_daily_all() -> list[dict]:
    """`SELECT * FROM margin_interest_daily ORDER BY date ASC` for the
    cumulative Telegram line — same table `_persist_margin_interest_daily`
    writes, read back with its own connection (this module never holds a
    live `Database()` instance). Returns `[]` on any read failure,
    including the table not existing yet, which the bucketing function
    reads as `source="no_data"`, never a fabricated zero."""
    try:
        import sqlite3

        if not _DB_PATH.exists():
            return []
        conn = sqlite3.connect(str(_DB_PATH), timeout=5.0)
        try:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT date, debit_balance, rate_pct, daily_usd, "
                "days_charged, period_usd, source "
                "FROM margin_interest_daily ORDER BY date ASC"
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001
        logger.warning("margin interest daily read failed: %s", exc)
        return []


def _margin_interest_lines() -> list[str]:
    """['💳 margin interest — this week $X, <Month> $Y (est.), all-time $Z
    (est.) (since <date>)'] — owner ask, 2026-09-24, replacing the old
    per-day/per-year figures and the ESTIMATE-caveat paragraph with a
    short cumulative summary. `src.margin_interest.format_cumulative_line`
    owns the wording; `compute_cumulative_margin_interest` owns the
    broker-actual-preferred, estimate-fallback bucketing (this week /
    current month / up to 6 months / all-time).

    Morning-only, like the balance/day-cost lines above: interest accrues
    on the OVERNIGHT debit balance, so the morning snapshot — taken before
    any new trading — is the one honest read of what was actually carried
    across the close. That reading is also PERSISTED here
    (`_persist_margin_interest_daily`) — `daily_pnl` never stored cash/
    debit, so this is the only historical record of the desk's overnight
    debit balance, which is what makes "this week"/"this month"/"all-time"
    (for the estimate-fallback path) possible at all going forward.

    Reads the account's actual cash regardless of `allow_margin` — that
    flag is QAMC's own risk toggle, not a broker-side guarantee that cash
    stays non-negative. `cash_only` (src/risk/rules.py) hard-blocks a
    plain BUY from taking cash negative when `allow_margin` is `False`,
    but a COVER is exempt from that rule by design (D10), and
    `src/agents/portfolio_manager.py`'s DE-LEVER MANDATE already treats
    "cash negative AND allow_margin False" as a real, live state — so a
    debit balance can exist even with margin disabled, and a short-circuit
    on `allow_margin` alone would silently miss it.

    Never raises: a broker-read failure here must not be able to block the
    alert — it degrades to a line that SAYS the read failed, rather than to
    silence. Suppressed entirely under QAMC_REHEARSAL, same as every other
    line here that touches the network: a rehearsal has no live account to
    report on, so there is no running tracker for a zero to prove alive and
    the line would be theatre.
    """
    from src.margin_interest import (
        RATE_UNAVAILABLE_LINE,
        UNAVAILABLE_LINE,
        build_estimate,
        compare_estimate_to_broker_activity,
        compute_cumulative_margin_interest,
        days_charged_until_next_trading_day,
        format_cumulative_line,
        overnight_debit_balance,
    )

    if _REHEARSAL_MODE:
        return []
    try:
        from src.config import load_config

        cfg = load_config("config/settings.yaml")
        rate_pct = cfg.risk.margin_interest_rate_pct
    except Exception as exc:  # noqa: BLE001 — a nicety must never break the alert
        logger.warning("margin interest config read failed: %s", exc)
        return [RATE_UNAVAILABLE_LINE]

    try:
        from src.api.deps import get_alpaca_credentials, get_alpaca_paper
        from src.execution.broker import AlpacaBroker

        key, secret = get_alpaca_credentials()
        broker = AlpacaBroker(api_key=key, secret_key=secret, paper=get_alpaca_paper())
        account = broker.get_account()
        cash = account.get("cash")
        debit_balance = overnight_debit_balance(cash)
        # Alpaca charges for every calendar day a debit is carried, so a
        # Friday's overnight is 3 days (4 before a Monday holiday). Read the
        # exchange calendar for how many days tonight's carry spans; the
        # helper never raises and degrades to 1 if the calendar can't be read.
        from src.util.time import et_today

        today = et_today()
        days_charged = days_charged_until_next_trading_day(broker.is_trading_day, today)
        estimate = build_estimate(debit_balance, rate_pct, days_charged)
    except Exception as exc:  # noqa: BLE001
        logger.warning("margin interest estimate failed: %s", exc)
        return [UNAVAILABLE_LINE]

    period_usd = estimate.period_usd if estimate is not None else 0.0
    source = "estimate"
    try:
        full_history = broker.get_margin_interest_activities()
        if estimate is not None:
            comparison = compare_estimate_to_broker_activity(estimate, full_history)
            if comparison is not None and comparison.charge_confirmed and comparison.observed_usd:
                # The broker's own INT record for tonight beats our formula
                # — use its real number for THIS row (source flips to
                # broker_actual), without changing the fallback logic the
                # cumulative view applies to every OTHER day.
                period_usd = comparison.observed_usd
                source = "broker_actual"
    except Exception as exc:  # noqa: BLE001
        logger.warning("margin interest INT-activity check failed: %s", exc)
        full_history = []

    _persist_margin_interest_daily(
        today,
        debit_balance,
        rate_pct,
        estimate.daily_usd if estimate is not None else 0.0,
        days_charged,
        period_usd,
        source,
    )

    try:
        estimate_rows = _read_margin_interest_daily_all()
        cumulative = compute_cumulative_margin_interest(full_history, estimate_rows, today)
        return [format_cumulative_line(cumulative)]
    except Exception as exc:  # noqa: BLE001
        logger.warning("margin interest cumulative view failed: %s", exc)
        return [UNAVAILABLE_LINE]
