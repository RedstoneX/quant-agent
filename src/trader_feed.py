"""Trader-oriented Telegram presentation for QAMC.

Observability only: this module never calls the broker, never mutates the
trading database, and never changes a trading decision. It reads already
persisted, validated evidence and turns it into a phone-friendly trader feed.

Non-trading modes and hard failure/skip statuses fall back to the established
formatter unchanged.
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
from pathlib import Path
from typing import Any

from src.notifier import (
    _actionable_coverage_gaps,
    _attr_or_key,
    _clip_text,
    _DB_PATH as _NOTIFIER_DB_PATH,
    _fmt_signed_money,
    _lookup_company_profiles,
    _new_block,
    _new_section,
    _seal_section,
    company_name,
    describe_ai_cost,
    describe_data_status,
    describe_skipped_decision,
    fmt_time_12h,
    humanize_status,
    format_session_result as _base_format_session_result,
    TelegramNotifier,
)
from src.trading_calendar import SESSION_WINDOWS, et_now, in_session_window, to_et

logger = logging.getLogger(__name__)

_DB_PATH = _NOTIFIER_DB_PATH
_SWEEP_SYMBOLS = frozenset({"SGOV", "BIL"})
_BASE_ONLY_STATUSES = frozenset(
    {
        "market_holiday", "early_close", "broker_error", "analysis_error",
        "fetch_error",
        # Guard 1 (2026-09-02): the kill switch's early check returns before
        # any of the rich per-mode data (orders/positions/trades) exists to
        # render, same shape-mismatch reason "broker_error" is here. Routes
        # to the base notifier.py formatter, which renders a plain
        # "status: kill_switch_halted" line and — critically — is NOT
        # silenced for intra_check the way "ok"/"market_holiday" are.
        "kill_switch_halted",
    }
)
# 2026-08-31 visibility fix (src/pipeline.py's `_run_intraday_opportunity_scan`
# / `_intraday_opportunity_scan_body`): these three now attach an explicit
# `result["intraday_scan"]["status"]` dict where a "never engaged a real
# candidate" tick used to leave no `intraday_scan` key at all. They stay off
# the Telegram feed on purpose — same as the old no-key ticks, per the
# "ordinary ~30-minute OK ticks are silent" policy below — because nothing
# about them needs an operator's attention: disabled-by-config and
# lock-contention are routine scheduling noise, and "no opportunity" means
# the scan ran and correctly found nothing. Only a real candidate engaged
# (intraday_no_trades/intraday_executed) or a genuine problem (crashed/
# suspended/analysis_error) is worth a message.
_INTRADAY_SILENT_STATUSES = frozenset({
    "intraday_scan_disabled", "intraday_scan_lock_contended",
    "intraday_scan_no_opportunity",
})


def format_session_result(
    mode: str,
    result: dict | None,
    elapsed_seconds: float,
    error: BaseException | None = None,
) -> str | None:
    """Build the Telegram message without affecting trading.

    Enrichment is intentionally fail-soft: if the read-only evidence lookup or
    formatter has any problem, the existing notifier format is used instead.
    """
    if error is not None or not isinstance(result, dict):
        return _base_format_session_result(mode, result, elapsed_seconds, error=error)

    status = str(result.get("status", "unknown"))
    # The pre-market earnings pass (2026-09-18): rendered here for the two
    # outcomes that speak — filings read, or the reader failing — so the
    # message names each company instead of counting them. Every silent
    # status (nothing_new / fetch_error / market_holiday) and the paid-
    # analysis latch keep the base formatter's noise policy unchanged.
    if mode == "earnings_preprocess" and status in ("preprocessed", "analysis_error"):
        try:
            return _format_earnings(result, elapsed_seconds)
        except Exception as exc:  # noqa: BLE001
            logger.warning("trader-feed earnings render failed: %s", exc)
            return _base_format_session_result(mode, result, elapsed_seconds, error=None)
    if status in _BASE_ONLY_STATUSES or status.startswith("pm_") or status == "paid_analysis_suspended":
        return _base_format_session_result(mode, result, elapsed_seconds, error=None)

    try:
        if mode == "intra_check":
            return _format_intra_check(result, elapsed_seconds)

        if mode in ("midday", "close"):
            return _format_position_review(mode, result, elapsed_seconds)

        if mode in ("morning", "once"):
            return _format_decision_session(mode, result, elapsed_seconds)

        if mode == "evening":
            return _format_evening(result, elapsed_seconds)

        # earnings/meta/daily have their own noise policy — base formatter.
        return _base_format_session_result(mode, result, elapsed_seconds, error=None)
    except Exception as exc:  # noqa: BLE001
        logger.warning("trader-feed enrichment failed for %s: %s", mode, exc)
        return _base_format_session_result(mode, result, elapsed_seconds, error=None)


def _fmt_elapsed(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.1f}s"
    return f"{int(seconds // 60)}m {int(seconds % 60)}s"


def _clip(value: Any, limit: int = 140) -> str:
    """Collapse whitespace/newlines to one line, then clip via the shared,
    boundary-aware `_clip_text` (src/notifier.py) instead of a raw
    `text[:limit]` slice.

    This used to hard-cut mid-word — the operator's actual complaint was a
    BUY CRM alert whose PM rationale read "...strong heavy accumulation
    volume" and just stopped there, an artifact of `_append_pm` calling
    this with `limit=105` on LLM prose that routinely runs 300-500+ chars.
    Callers below have also had their limits raised substantially (this
    formatter renders several such bullets per message, well inside
    Telegram's real 4096-char budget)."""
    text = " ".join(str(value or "").split())
    return _clip_text(text, limit, marker="…")


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number


def _status_emoji(status: str) -> str:
    if status in {"executed", "intraday_executed"}:
        return "🟢"
    if status in {"reviewed", "intraday_no_trades", "no_trades", "ok"}:
        return "🔵"
    if status in {"rejected", "hard_risk_block", "symbol_block", "buys_unfunded"}:
        return "🟡"
    if "error" in status or status in {
        # "emergency_sold" and the halt status beside it are kept so runs
        # stored before their retirements still render RED rather than
        # falling through to the white "nothing happened" bucket; the owner
        # is red-green colour blind, so a halted historical run reading as
        # ordinary is the wrong kind of wrong. Nothing emits either any more
        # (item 32 — the liquidation half went 2026-09-14, the halt itself
        # 2026-09-20).
        "failed", "emergency_sold", "daily_loss_halted",  # retired-ok
        "kill_switch_halted",
    }:
        return "🔴"
    return "⚪"


# === Scan-first markup helpers (2026-09-17 redesign) ===
#
# Owner feedback on the pre-redesign messages: "unless I read everything
# word for word, I have no idea what was actually really done and what
# just failed or was killed." Every formatter below now leads with a
# plain-English outcome word and three short, structured sections — DONE /
# BLOCKED-FAILED / LOOKED AT, NO TRADE (HELD / WATCH for a position review)
# — built from the SAME underlying evidence the old wall-of-text used, so
# nothing is newly invented; it is re-shaped. The full per-stock reasoning
# the owner said he is happy with is kept, verbatim, inside a collapsed
# `<b>DETAILS</b>` / `<blockquote expandable>` block (Telegram Bot API HTML
# style, https://core.telegram.org/bots/api#html-style, documents
# `<blockquote>` and `<blockquote expandable>` as supported parse_mode=HTML
# tags) — one tap opens it. `<b>`/`<blockquote expandable>` are literal
# tags in the plain text this module returns; see
# `src.notifier._escape_with_markup` for how they survive `html.escape`
# without any other text being able to inject one.


def _b(text: str) -> str:
    """A literal `<b>...</b>` section header — see the module-level note
    above. Never build a `<b>` tag around text some other way; the escape
    step in src/notifier.py only preserves this exact fixed string."""
    return f"<b>{text}</b>"


# Board item 89 clarity defect — bare tickers after the twelfth name. The
# base formatter's cap (12) sized a separate "who:" block; the trader feed
# names companies inline, so every listed symbol needs one. Cache read only.
_COMPANY_NAME_CAP = 60


def _profiles(*groups: Any) -> dict[str, Any]:
    """Company profiles for every symbol across `groups` — one cache read
    per message, uncapped at the twelfth name (see `_COMPANY_NAME_CAP`)."""
    return _lookup_company_profiles(_all_symbols(*groups), limit=_COMPANY_NAME_CAP)


def _ticker_co(symbol: str, profiles: dict[str, Any] | None) -> str:
    """'SYM (Company Name)', or bare 'SYM' when the company cache doesn't
    know it — never a broken "(?)" placeholder, same posture as
    `src.notifier._append_company_identities`."""
    symbol = str(symbol or "?").upper()
    name = company_name(symbol, profiles) if profiles is not None else None
    return f"{symbol} ({name})" if name else symbol


def _all_symbols(*groups: Any) -> list[str]:
    """First-seen-order, deduped, upper-cased union of every symbol across
    `groups` (each a list of dicts with a 'symbol' key, or a plain list of
    ticker strings) — one CompanyProfileStore batch fetch per message
    instead of one per section."""
    seen: list[str] = []
    for group in groups:
        for item in group or []:
            raw = item.get("symbol") if isinstance(item, dict) else item
            sym = str(raw or "").strip().upper()
            if sym and sym not in seen:
                seen.append(sym)
    return seen


# Plain-English "who actually stopped it" for an execution-skip `reason`
# code (src/pipeline_stages.py's `_record_execution_skip` call sites) — the
# operator asked to be told desk safety check / risk manager / broker /
# not-filled-in-time, not an internal snake_case reason code, and the desk
# guards must say so explicitly — the whole point of this wording is that
# it is NOT the broker. Each value is a complete phrase ready to sit before
# a colon (`_append_blocked` renders "{who}: {reason}"). Risk-manager
# refusals and "not filled in time" are classified separately (they don't
# come through `execution_skips` — see `_blocked_rows`).
_SKIP_WHO_LABELS: dict[str, str] = {
    "fat_finger_guard": "Blocked by desk safety check (not the broker)",
    "unusable_stop": "Blocked by desk safety check — unusable stop (not the broker)",
    "kill_switch_halted": "Blocked by desk safety check — kill switch (not the broker)",
    "broker_rejected": "Blocked by the broker",
    "insufficient_cash": "Blocked by the desk — insufficient cash",
    "below_min_notional": "Blocked by the desk — order too small",
    "no_price": "Blocked by the desk — no verifiable price",
    "stale_entry": "Blocked by the desk — price moved since the decision",
    "qty_zero": "Blocked by the desk — sizing rounds to zero",
    "latency_window": "Blocked by the desk — too slow (latency)",
    "slippage_gated": "Blocked by the desk — price ran past the slippage limit",
    "borrow_gate": "Blocked by the desk — short not available to borrow",
    "short_add_blocked": "Blocked by the desk — adding to a short isn't supported",
    "rotation_room_not_freed": "Blocked by the desk — no rotation room freed",
}


def _skip_who(reason: str) -> str:
    return _SKIP_WHO_LABELS.get(str(reason or ""), "Blocked by the desk")


# A NORMAL OPERATING STATE IS NOT AN ERROR (owner, 2026-09-23).
#
# Every execution-skip `reason` below is a desk rule declining to place an
# order ON PURPOSE. The risk system refusing a trade it is built to refuse
# is the risk system working, and a session whose only blocks are these did
# not fail — it decided. The measured cases that forced this: "⚡ INTRADAY
# OPPORTUNITY · 9:49 AM ET · FAILED" on 2026-09-21 whose single block was
# `short_add_blocked`, and "🔵 MORNING · 9:36 AM ET · FAILED" on 2026-09-22
# whose only two blocks were the 200-session history pre-check refusing DRAM
# and CBRS while the PM's own verdict was `no_trades`.
#
# THE SET IS AN ALLOWLIST AND IT IS DELIBERATELY SHORT. Anything not named
# here — `broker_rejected`, `no_price`, `submit_failed`, a code added by a
# future change, a code this map has never seen — is classified as a FAULT
# and keeps the loud header. Getting a refusal wrongly called a failure is
# noise; getting a failure wrongly called a refusal is the owner not being
# told his desk broke, so the unknown case must fall the loud way.
#
# WHAT IS NOT ON IT, AND WHY — each of these was proposed for this set and
# thrown out against the production database on 2026-09-23:
#
#   slippage_gated / latency_window  Every one of the six `slippage_gated`
#     rows in the database is a venue-data problem wearing a policy code:
#     IEX asks 391, 405, 437, 488, 580 bp above reference against a 40bp
#     ceiling, on an account whose own code comments record that IEX
#     top-of-book is routinely stale. The 2026-09-15 run lost four
#     risk-approved buys this way; calling that session a correct no-trade
#     is exactly the cover story the desk's own doctrine forbids. The two
#     codes are also not independently determined — `src/pipeline_stages.py`
#     picks between them on `ctx.desk_latency_stall`, which is set at six
#     sites and cleared at none, so one stall anywhere in a run relabels
#     every later quote refusal in it.
#   short_add_blocked  Its own detail string reads "adding to a short is
#     not built". An unbuilt feature the desk keeps trying to use is not a
#     decision; it is the desk failing to act on its own signal, five times
#     on FLNC on 2026-09-21 alone.
#   geometry_rr  Dead. `_execution_payoff_skip_reason` returns None
#     unconditionally and its docstring says the token is not emitted. It
#     survives only in stored runs, which `render_stored_session_report`
#     re-renders through this code — so whitelisting it would retroactively
#     relabel the two historical reward:risk refusals as correct.
#   stale_entry  Zero rows in the entire database. Assigning owner-facing
#     meaning to a path that has never once run is a guess.
#   borrow_gate  A failed broker asset lookup is synthesised into
#     `{"shortable": False, "reason": "asset_lookup_failed"}` and filed
#     under this code, so an API failure would read as a borrow decision.
#   insufficient_cash / unusable_stop / fat_finger_guard  "There was no
#     cash", "the desk could not compute a stop" and "the safety net caught
#     an absurd order" are all things that must be said loudly. A guard
#     firing means something upstream produced something wrong.
_DELIBERATE_SKIP_REASONS = frozenset({
    # The $500 minimum trade size — one of the three the owner named.
    "below_min_notional",
    # Deterministic sizing arithmetic resolving to nothing to place.
    "qty_zero",
    # A full book with nothing outranking a holding. PR #600 established
    # on the owner's own words that this is the desk's normal operating
    # state, not a fault: "Yes portfolio is full. But we're still reviewing
    # things, which is how we built it."
    "rotation_room_not_freed",
})


def _skip_is_fault(reason: Any) -> bool:
    """True when an execution skip means something BROKE rather than a desk
    rule deciding not to trade — see `_DELIBERATE_SKIP_REASONS`."""
    return str(reason or "").strip() not in _DELIBERATE_SKIP_REASONS


# Board item 89 defect 5 — raw internal tokens printed to the owner word
# for word. Every value below is what the token MEANS, in the same words a
# person would use; the key is the internal spelling and never reaches the
# message. An unrecognised token is DESCRIBED ("the desk recorded an
# outcome it has no plain wording for"), never pasted through and never
# guessed at, because guessing what an unknown status means to a person
# reading it as a trading fact is the failure this rule exists to stop.
_ORDER_END_WORDS: dict[str, str] = {
    "canceled": "the order was cancelled before anything filled",
    "cancelled": "the order was cancelled before anything filled",
    "expired": "the order ran out of time before anything filled",
    "rejected": "the broker turned the order down",
    "submit_failed": "the order never reached the broker",
    "replaced": "the order was replaced by another one",
    "done_for_day": "the order was closed out at the end of the day unfilled",
    "suspended": "the order was suspended by the broker before filling",
    "stopped": "the broker stopped the order before it filled",
    "pending_cancel": "the order is being cancelled",
    "pending_replace": "the order is being replaced",
}

#: The same treatment for a fill state shown ALONGSIDE an order line, where
#: the sentence above would read oddly. Kept as a separate map rather than
#: reworded from one, so neither reads as a translation of the other.
_FILL_STATE_WORDS: dict[str, str] = {
    "filled": "filled",
    "submitted": "placed, waiting to fill",
    "pending_submit": "not yet at the broker",
    "partially_filled": "part-filled, still working",
    "canceled": "cancelled unfilled",
    "cancelled": "cancelled unfilled",
    "expired": "expired unfilled",
    "rejected": "turned down by the broker",
    "submit_failed": "never reached the broker",
}


def _machine_detail(text: Any) -> str:
    """Board item 89 defect 5 — the underlying fault text, LABELLED as
    machine output instead of pasted in as though it were a sentence
    written for the reader.

    It is kept rather than dropped: it is often the only record of what
    actually broke, and inventing a friendly paraphrase of an exception
    would be inventing a fact. What changes is that the owner is told what
    he is looking at and that there is nothing in it for him to do.
    """
    return (
        "Machine fault text, kept for the record — nothing here needs "
        f"anything from you: {_clip(text, 900)}"
    )


def _order_end_plain(fill_status: Any) -> str:
    token = str(fill_status or "").strip().lower()
    known = _ORDER_END_WORDS.get(token)
    if known:
        return known
    return (
        "the order never became a live fill, and the desk recorded an "
        "outcome it has no plain wording for"
    )


def _fill_state_plain(fill_status: Any) -> str:
    token = str(fill_status or "").strip().lower()
    return _FILL_STATE_WORDS.get(token) or "state not recorded in plain words"


def _decision_action_for(symbol: str, snap: dict[str, Any]) -> str:
    """The PM/constructor's own action word for `symbol` this run (BUY /
    SELL / SHORT / COVER / HOLD / ...), so a BLOCKED/FAILED line can say
    "SHORT FLNC" rather than a bare "? FLNC"."""
    for row in snap.get("pm_orders") or []:
        if isinstance(row, dict) and str(row.get("symbol", "")).upper() == symbol:
            return str(row.get("action", "?")).upper()
    return "?"


def _symbol_stop(symbol: str, snap: dict[str, Any]) -> float | None:
    """The stop QAMC actually intends for `symbol` — the constructor's own
    `TradeDecision.stop_loss`, overridden by a risk-manager modification of
    the SAME field when one exists (Risk can retune a symbol's stop without
    the constructor knowing — see `RiskModification`). Not the broker's
    fill data: `trades` carries no stop column at all."""
    stop: float | None = None
    for row in snap.get("pm_orders") or []:
        if isinstance(row, dict) and str(row.get("symbol", "")).upper() == symbol:
            stop = _number(row.get("stop_loss"))
            break
    for row in snap.get("risk_mods") or []:
        if (
            isinstance(row, dict)
            and str(row.get("symbol", "")).upper() == symbol
            and str(row.get("field", "")).lower() == "stop_loss"
        ):
            new_value = _number(row.get("new_value"))
            if new_value is not None:
                stop = new_value
    return stop


def _trade_reached_broker(fill_status: Any) -> bool:
    """True once an order is live at the broker — filled, or still working
    ('submitted'/'pending_submit'). False for every terminal-fail status
    (`canceled`, `expired`, `rejected`, `submit_failed`, ...): a `trades`
    row exists, but nothing is protecting the operator's capital."""
    return str(fill_status or "").lower() in {"filled", "submitted", "pending_submit"}


def _classify_trades(snap: dict[str, Any]) -> tuple[list[dict], list[dict]]:
    """Real (non-HOLD, non-sweep) trades split into (reached_broker,
    did_not_reach) — see `_trade_reached_broker`."""
    _, real = _execution_rows(snap)
    reached: list[dict] = []
    stalled: list[dict] = []
    for row in real:
        (reached if _trade_reached_broker(row.get("fill_status")) else stalled).append(row)
    return reached, stalled


def _done_rows(snap: dict[str, Any]) -> list[dict]:
    reached, _ = _classify_trades(snap)
    return reached


def _blocked_rows(result: dict, snap: dict[str, Any]) -> list[dict]:
    """Everything that did NOT make it, one row per symbol, each carrying
    who actually stopped it and why — see NEW LAYOUT item 4. Three sources,
    in priority order (a symbol is never listed twice): the execution
    skips QAMC's own deterministic gates recorded; symbols the risk manager
    refused outright (`RiskVerdict.rejected_symbols`, Phase 10.1 — a
    per-symbol refusal distinct from the whole-plan `approved` bool); and
    a real trade row that reached neither a fill nor a live working order
    (a DAY order that expired unfilled, a submit that ultimately failed).

    Every row also carries `"fault"`: True when it means something BROKE,
    False when it is a desk or risk rule declining on purpose. It is set
    HERE, at construction, because this is the only place the raw reason
    code is still in hand — `_append_blocked` and `_outcome_word` see only
    the plain English and could not tell the two apart. See
    `_DELIBERATE_SKIP_REASONS` for why an unrecognised code is a fault.
    """
    rows: list[dict] = []
    seen: set[str] = set()

    skips = [row for row in (result.get("execution_skips") or []) if isinstance(row, dict)]
    if not skips:
        skips = [row for row in (snap.get("skips") or []) if isinstance(row, dict)]
    for row in skips:
        symbol = str(row.get("symbol", "?")).upper()
        if symbol in seen:
            continue
        rows.append({
            "symbol": symbol,
            "action": _decision_action_for(symbol, snap),
            "who": _skip_who(row.get("reason", "")),
            "reason": row.get("detail") or row.get("reason") or "blocked",
            "fault": _skip_is_fault(row.get("reason")),
        })
        seen.add(symbol)

    risk = snap.get("risk")
    if isinstance(risk, dict):
        for row in risk.get("rejected_symbols") or []:
            if not isinstance(row, dict):
                continue
            symbol = str(row.get("symbol", "?")).upper()
            if symbol in seen:
                continue
            stated = str(row.get("reason") or "").strip()
            rows.append({
                "symbol": symbol,
                "action": _decision_action_for(symbol, snap),
                "who": "Blocked by risk manager",
                "reason": stated or "refused without a stated reason",
                # The risk seat refusing a name it is built to refuse is
                # the risk seat working. But this field is free text with
                # no code behind it, so the ONLY thing separating a working
                # risk seat from a malfunctioning one here is whether it
                # said why. A refusal with nothing written down stays loud.
                "fault": not stated,
            })
            seen.add(symbol)

    _, stalled = _classify_trades(snap)
    for row in stalled:
        symbol = str(row.get("symbol", "?")).upper()
        if symbol in seen:
            continue
        rows.append({
            "symbol": symbol,
            "action": str(row.get("action", "?")).upper(),
            "who": "Not filled in time",
            # Board item 89 defect 5: this used to print the broker's own
            # status token verbatim ("(status: canceled)"). `_ORDER_END_WORDS`
            # says the same thing in words; an unmapped token is described,
            # never pasted.
            "reason": _order_end_plain(row.get("fill_status")),
            # An order the desk MEANT to place that reached neither a fill
            # nor a live working order is an intention that did not happen.
            # Loud.
            "fault": True,
        })
        seen.add(symbol)

    # Board item 89 defect 6 — the silent drop. A target the constructor
    # ended before an order existed reached no message at all: it produced
    # no trade row, no execution skip and no risk verdict, so every one of
    # the three sources above missed it and the session read "orders: 0"
    # with no explanation. The reason it carries is already a plain-English
    # sentence written at the refusal site (`PortfolioConstructor._note_
    # refusal`), so nothing is invented here; the internal refusal CODE
    # beside it is deliberately NOT rendered.
    for row in (snap.get("constructor_blocks") or []):
        if not isinstance(row, dict):
            continue
        symbol = str(row.get("symbol", "?")).upper()
        if symbol in seen:
            continue
        detail = str(row.get("detail") or "").strip()
        # A constructor block is a deliberate refusal ONLY when the
        # constructor recorded one as data — `_record_constructor_drops`
        # files those under `constructor_refused` with a named `refusal`
        # code beside them (measured on the whole production database:
        # insufficient_history 13, gross_exposure_ceiling_refused 5,
        # sector_crowding_leaves_below_min_order 2,
        # delta_below_min_trade_weight 1). Every one of those is a desk
        # rule declining on purpose. The other path — `constructor_dropped`,
        # recovered from a log line, or a row with no reason written down at
        # all — is exactly the case where the report CANNOT tell a rule from
        # a breakage, so it stays loud rather than guess.
        named_refusal = bool(str(row.get("refusal") or "").strip())
        fault = not (named_refusal and detail)
        if not detail:
            # Never a guess and never an internal code: say that the reason
            # was not written down.
            detail = (
                "the desk ended this plan before placing an order and did "
                "not record why"
            )
        rows.append({
            "symbol": symbol,
            "action": _decision_action_for(symbol, snap),
            "who": "Stopped by the desk before an order was placed",
            "reason": detail,
            "fault": fault,
        })
        seen.add(symbol)

    return rows


def _looked_at_rows(
    snap: dict[str, Any], candidates: list[str] | None, acted_symbols: set[str],
) -> list[dict]:
    """Analyzed signals that were neither done nor blocked — the PM/
    constructor's silent "pass". Addresses the 5-analyzed/5-actionable
    defect: the header used to claim more than the message ever said what
    happened to."""
    rows = []
    for row in _signal_rows(snap, candidates):
        symbol = str(row.get("symbol", "")).upper()
        if not symbol or symbol in acted_symbols:
            continue
        rows.append(row)
    return rows


def _traded_word(done_rows: list[dict] | None) -> str:
    """Board item 89 clarity defect — a 'TRADED' header on a run that only
    sold. The word is read off the actions that actually reached the
    broker: all exits read SOLD, all entries read BOUGHT, a mix reads
    TRADED. An unknown action falls back to TRADED rather than guessing
    a direction."""
    actions = {
        str(row.get("action", "")).upper() for row in (done_rows or [])
        if isinstance(row, dict)
    }
    exits = {"SELL", "REDUCE", "COVER", "TRIM"}
    entries = {"BUY", "SHORT", "ADD"}
    if actions and actions <= exits:
        return "SOLD"
    if actions and actions <= entries:
        return "BOUGHT"
    return "TRADED"


def _fault_count(blocked_rows: list[dict] | None) -> int:
    """How many blocked rows mean something BROKE. A row with no `fault`
    key is counted as one: an unclassified block is the case the report
    cannot tell apart, and the unknown case falls the loud way."""
    return sum(
        1 for row in (blocked_rows or [])
        if not isinstance(row, dict) or row.get("fault", True)
    )


def _outcome_word(
    status: str, done_count: int, blocked_count: int,
    done_rows: list[dict] | None = None,
    fault_count: int | None = None,
) -> str:
    """ONE plain word for the header line, computed from the SAME rows the
    sections below render, so the header can never claim something the body
    doesn't show. `done_rows` lets the word say which way the trades went.

    THE VOCABULARY, and which state earns which word:

      FAILED     something broke — a red status, or a blocked row
                 classified as a fault (`_blocked_rows`).
      PARTIAL    orders reached the broker AND something also broke.
      BOUGHT /   orders reached the broker; the word says which way
      SOLD /     (`_traded_word`). Correct refusals alongside them do not
      TRADED     change it: the session traded.
      NO TRADE   nothing reached the broker, and every block was a desk or
                 risk rule declining on purpose. A normal operating state.
      NO CHANGE  nothing reached the broker and nothing was declined.

    WHY "NO TRADE" EXISTS. Until 2026-09-23 any blocked row at all returned
    FAILED, so the gross-exposure ceiling, the $500 minimum trade size and
    the 200-session history pre-check each put the word FAILED on the top
    line of a session that had worked correctly. Owner's instruction, in his
    words: a normal operating state must not be reported as an error. It is
    a separate word from NO CHANGE on purpose — "the desk weighed names and
    declined them" and "the desk had nothing to weigh" are different facts
    and the owner has to be able to tell them apart from the header alone.

    `fault_count=None` keeps the pre-2026-09-23 behaviour (every block
    counts as a fault) for any caller that has not been given rows to
    classify — the loud direction, never the quiet one.
    """
    if _status_emoji(status) == "🔴":
        return "FAILED"
    faults = blocked_count if fault_count is None else fault_count
    if done_count and faults:
        return "PARTIAL"
    if done_count:
        return _traded_word(done_rows)
    if faults:
        return "FAILED"
    if blocked_count:
        return "NO TRADE"
    return "NO CHANGE"


# Reserved characters, not a trading number: `_wrap_details` must guarantee
# its own output fits inside `TelegramNotifier.MAX_MESSAGE_CHARS` so the
# tag-blind emergency clip in `notifier._build_payload` never has to run
# for an ordinary alert (that fallback can only cut on a text/entity
# boundary, not a `<blockquote expandable>` boundary). Covers the Mission
# Control `<a href>` link `send()` appends after this text, plus slack for
# `html.escape` expanding a handful of '&'/'"'/"'" characters in PM/risk
# prose — this module never learns the real escaped length, only text.py's
# `_build_payload` does.
_DETAILS_SAFETY_RESERVE_CHARS = 250


def _wrap_details(
    lines: list[str], detail_lines: list[str], extra_reserve: int = 0,
) -> int:
    """Append the full, unabridged per-stock reasoning as a collapsed
    `<b>DETAILS</b>` / `<blockquote expandable>` block — NEW LAYOUT item 7.
    Content is unchanged from the pre-redesign message; only its
    presentation (collapsed, tapped open) is new. Sized to fit the
    remaining Telegram budget so a clip, if one is needed, lands inside
    DETAILS and never inside the scan-first sections above it.

    `extra_reserve` is room for a section the caller has NOT appended yet —
    the candidate list, which `_budgeted_sections` deliberately holds back
    so this block gets its share of the budget first. Returns the budget
    this block did not use, which is what the caller then spends on
    rendering that list at a richer tier.
    """
    text = "\n".join(line for line in detail_lines if line is not None).strip("\n")
    wrapper_overhead = len("<b>DETAILS</b>\n<blockquote expandable></blockquote>")
    used = len("\n".join(lines))
    budget = (
        TelegramNotifier.MAX_MESSAGE_CHARS - used - wrapper_overhead
        - _DETAILS_SAFETY_RESERVE_CHARS - max(0, extra_reserve)
    )
    budget = max(0, budget)
    if not text:
        return budget
    if len(text) > budget:
        text = _clip_text(text, budget, marker="\n[details truncated — see Mission Control]")
    start = len(lines)
    lines.append(_b("DETAILS"))
    lines.append(f"<blockquote expandable>{text}</blockquote>")
    _seal_section(lines, start)
    return max(0, budget - len(text))


def _budgeted_sections(
    lines: list[str], slot: int, looked_at_rows: list[dict], profiles: dict,
    snap: dict[str, Any] | None, detail_lines: list[str],
) -> None:
    """Fit BOTH the candidate list and the reasoning block into one
    message, in the owner's order of need — and the reason the candidate
    list is appended late rather than in place.

    THE DEFECT THIS FIXES. `_wrap_details` sizes itself against whatever is
    already in `lines`. The candidate list was built first and, measured on
    run-fccb2026 (2026-09-23) replayed against post-#600 code, ran to 8,376
    characters against a 4,000-character budget — so DETAILS got a negative
    budget and rendered EMPTY. Live proof: that morning's message, and
    2026-09-21's and 2026-09-22's, all end
    `<blockquote expandable></blockquote>`. The desk's reasoning chain was
    the guaranteed daily casualty while a list of names it had already
    declined survived intact. Nothing was wrong with either section; the
    ORDER in which they claimed the budget was wrong.

    THE ORDER, highest priority first:

      1. Everything already in `lines` — heading, P&L, the stop-coverage
         and research banners, DONE, NOT TAKEN / FAILED. Never squeezed.
      2. The candidate list at its tier-3 floor (`_looked_at_block`) —
         every name, no detail. Reserved before DETAILS is sized, so the
         list can never be erased either.
      3. The reasoning block, which takes what is left.
      4. Whatever the reasoning did not need goes back into rendering the
         candidate list at a richer tier.

    Step 4 is what stops this being a straight swap of one casualty for
    another: on a quiet session with little reasoning, the list still
    renders in full.
    """
    floor = _looked_at_block(looked_at_rows, profiles, snap, budget=0)
    # +1 for the blank separator line `_seal_section` would insert.
    reserve = (len("\n".join(floor)) + 1) if floor else 0
    spare = _wrap_details(lines, detail_lines, extra_reserve=reserve)
    if not floor:
        return
    block = _looked_at_block(
        looked_at_rows, profiles, snap, budget=max(0, reserve - 1 + spare),
    )
    lines[slot:slot] = ([""] + block) if slot > 0 else block


def _empty_snapshot() -> dict[str, Any]:
    return {
        "macro": None,
        "tech": [],
        "pm_reasoning": None,
        "pm_targets": [],
        "pm_orders": [],
        "risk": None,
        "risk_mods": [],
        "skips": [],
        # Board item 89 defect 6. Targets the CONSTRUCTOR ended before an
        # order was ever built. Deliberately a separate key from `skips`:
        # `skips` is read by `_intraday_tick_actionable` to decide whether a
        # quiet half-hour tick speaks at all, and the owner's ratified
        # silence rule says a tick with nothing to act on sends nothing.
        # These rows only ever ADD lines to a message that is already going
        # out; they never cause one to be sent.
        "constructor_blocks": [],
        # The portfolio manager's per-candidate accounting (board item 133,
        # `src/pm_accounting.py`) — the named ground for every candidate it
        # did not target, keyed by symbol. It was written to
        # `specialist_evidence` from the day item 133 shipped and read by
        # nothing: `_read_run` only ever admitted `deterministic_gate`
        # pipeline events, so the reasons never reached this snapshot and
        # LOOKED AT told the owner the desk "did not record why" about
        # candidates it had recorded a ground for seconds earlier.
        "pm_accounting": {},
        # The opportunity-rotation pre-check for this run (one row, see
        # `_record_rotation_precheck`). A full book is a normal state, and
        # this is where the report gets to say so.
        "rotation": None,
        "trades": [],
        "positions": [],
        "agent_summaries": {},
        "cost": None,
        "calls": 0,
    }


def _read_run(run_id: str | None) -> dict[str, Any]:
    """Read forensic state for one run through a SQLite read-only connection."""
    snapshot = _empty_snapshot()
    if not run_id or run_id == "?" or not Path(_DB_PATH).exists():
        return snapshot

    conn = None
    try:
        uri = f"file:{Path(_DB_PATH).resolve()}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=1.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=1000")

        try:
            rows = conn.execute(
                "SELECT agent_name, kind, symbol, evidence_json "
                "FROM specialist_evidence WHERE run_id = ? ORDER BY id",
                (run_id,),
            ).fetchall()
            for row in rows:
                try:
                    data = json.loads(row["evidence_json"] or "{}")
                except (TypeError, ValueError, json.JSONDecodeError):
                    continue
                agent = row["agent_name"]
                kind = row["kind"]
                if agent == "macro_analyst" and kind == "analysis":
                    snapshot["macro"] = data
                elif agent == "tech_analyst" and kind == "analysis":
                    snapshot["tech"].append(data)
                elif agent == "portfolio_manager" and kind == "reasoning":
                    snapshot["pm_reasoning"] = data
                elif agent == "portfolio_manager" and kind == "target":
                    snapshot["pm_targets"].append(data)
                elif agent == "portfolio_manager" and kind == "proposed_order":
                    snapshot["pm_orders"].append(data)
                elif agent == "risk_manager" and kind == "verdict":
                    snapshot["risk"] = data
                elif agent == "risk_manager" and kind == "modification":
                    snapshot["risk_mods"].append(data)
                elif agent == "execution" and kind == "execution_skip":
                    snapshot["skips"].append(data)
                elif agent == "pipeline" and kind == "pipeline_event":
                    # Board item 89 defect 6: a plan the constructor ended
                    # before building an order left a durable per-symbol row
                    # here (`_record_constructor_drops` in
                    # src/pipeline_stages.py) and reached NO message at all —
                    # not the order list, not BLOCKED/FAILED, nothing. The
                    # owner could not know a decision had been made. Read
                    # only the terminal blocked outcomes; `unmeasurable`
                    # (data faults) already pages separately.
                    stage = str(data.get("stage") or "")
                    outcome = str(data.get("outcome") or "")
                    if (
                        stage == "deterministic_gate"
                        and outcome == "blocked"
                        and row["symbol"]
                    ):
                        snapshot["constructor_blocks"].append(
                            {**data, "symbol": row["symbol"]}
                        )
                    # The PM's per-candidate accounting. One row per
                    # non-targeted candidate, every one carrying the named
                    # ground the seat gave (or the honest record that it
                    # would not give one). Last row for a symbol wins: the
                    # accounting re-ask re-records the names it healed.
                    elif stage == "portfolio_manager" and row["symbol"]:
                        snapshot["pm_accounting"][
                            str(row["symbol"]).upper()
                        ] = dict(data)
                    elif stage == "rotation" and outcome == "precheck":
                        # The event's `reason` slot carries the named
                        # pre-check outcome (`rotation.precheck_outcome`);
                        # `outcome` here is the event kind. Renamed back to
                        # the field `owner_precheck_lines` reads, so the
                        # audit row and the owner's sentence stay one
                        # vocabulary rather than two spellings of it.
                        snapshot["rotation"] = {
                            **data, "outcome": data.get("reason"),
                        }
        except sqlite3.DatabaseError:
            pass

        try:
            rows = conn.execute(
                "SELECT symbol, action, qty, price, reasoning, fill_status, "
                "fill_qty, fill_price FROM trades WHERE run_id = ? ORDER BY id",
                (run_id,),
            ).fetchall()
            snapshot["trades"] = [dict(row) for row in rows]
        except sqlite3.DatabaseError:
            pass

        try:
            rows = conn.execute(
                "SELECT symbol, qty, avg_entry, current_price, market_value, "
                # qty != 0: shorts have a negative qty and must not be
                # invisible in the trader feed.
                "unrealized_pnl FROM positions WHERE qty != 0 "
                "ORDER BY ABS(market_value) DESC",
            ).fetchall()
            snapshot["positions"] = [dict(row) for row in rows]
        except sqlite3.DatabaseError:
            pass

        try:
            try:
                rows = conn.execute(
                    "SELECT agent_name, output_summary, cost_usd, provider_requests "
                    "FROM agent_logs WHERE run_id = ? ORDER BY id",
                    (run_id,),
                ).fetchall()
                snapshot["calls"] = sum(
                    (1 if row["provider_requests"] is None
                     else max(0, int(row["provider_requests"]))) for row in rows
                )
            except sqlite3.DatabaseError:
                rows = conn.execute(
                    "SELECT agent_name, output_summary, cost_usd FROM agent_logs "
                    "WHERE run_id = ? ORDER BY id",
                    (run_id,),
                ).fetchall()
                snapshot["calls"] = len(rows)
            for row in rows:
                snapshot["agent_summaries"][row["agent_name"]] = row["output_summary"]
            if rows and all(row["cost_usd"] is not None for row in rows):
                snapshot["cost"] = sum(float(row["cost_usd"]) for row in rows)
        except sqlite3.DatabaseError:
            pass
    except Exception as exc:  # noqa: BLE001
        logger.warning("trader-feed DB read failed for %s: %s", run_id, exc)
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass
    return snapshot


def extract_alert_symbols(run_id: str | None, result: dict | None) -> list[str]:
    """Every symbol worth making tappable in this alert, first-seen order,
    capped (see `TelegramNotifier._MAX_LINKED_SYMBOLS` in src/notifier.py).

    Deliberately narrow: pulled only from the SAME structured fields the
    renderers above already iterate for a `symbol` key (PM proposed orders,
    executed trades, execution skips, stop-coverage gaps, the midday/close
    reviewer's `result["review"]["actions"]` — including a HOLD or a
    decided-but-unexecuted action that never became a broker trade — and the
    top-level `result["orders"]` the base formatter's own
    `_append_company_identities` uses) — never a scan of the free-text PM/
    risk rationale, which routinely contains capitalized words ("ALL",
    "GO", "PASS") that would false-positive as tickers.

    Read-only and fail-soft like the rest of this module: a symbol that
    can't be determined is just not linked — see
    `TelegramNotifier._linkify_symbols` for why a bad/missing symbol must
    never be able to break message delivery.
    """
    symbols: list[str] = []

    def _add(raw: Any) -> None:
        sym = str(raw or "").strip().upper()
        if sym and sym not in symbols:
            symbols.append(sym)

    if isinstance(result, dict):
        for row in result.get("orders") or []:
            if isinstance(row, dict):
                _add(row.get("symbol"))
        for row in result.get("stop_coverage_gaps") or []:
            if isinstance(row, dict):
                _add(row.get("symbol"))
        review = result.get("review")
        if isinstance(review, dict):
            for row in review.get("actions") or []:
                if isinstance(row, dict):
                    _add(row.get("symbol"))

    try:
        snap = _read_run(run_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("extract_alert_symbols: run read failed for %s: %s", run_id, exc)
        snap = _empty_snapshot()

    for key in ("pm_orders", "trades", "skips"):
        for row in snap.get(key) or []:
            if isinstance(row, dict):
                _add(row.get("symbol"))

    return symbols[:10]


def _append_market(lines: list[str], snap: dict[str, Any]) -> None:
    macro = snap.get("macro")
    if not isinstance(macro, dict):
        return
    bits = [
        str(value)
        for value in (macro.get("regime"), macro.get("equity_outlook"), macro.get("confidence"))
        if value
    ]
    guidance = macro.get("position_guidance") or {}
    target = guidance.get("target_invested_pct") if isinstance(guidance, dict) else None
    text = " / ".join(bits)
    if isinstance(target, (int, float)):
        text += f" · target {target:g}% invested"
    if text:
        lines.append(f"📊 Market: {text}")


def _append_book(lines: list[str], snap: dict[str, Any]) -> None:
    positions = [row for row in (snap.get("positions") or []) if isinstance(row, dict)]
    if not positions:
        return
    risk_rows = [row for row in positions if str(row.get("symbol", "")).upper() not in _SWEEP_SYMBOLS]
    sweep_rows = [row for row in positions if str(row.get("symbol", "")).upper() in _SWEEP_SYMBOLS]
    invested = sum(_number(row.get("market_value")) or 0.0 for row in risk_rows)
    parked = sum(_number(row.get("market_value")) or 0.0 for row in sweep_rows)
    text = f"💼 Book: {len(risk_rows)} position(s) · ${invested:,.0f} invested"
    if parked > 0:
        text += f" · ${parked:,.0f} T-bills"
    lines.append(text)


def _signal_rows(
    snap: dict[str, Any], candidates: list[str] | None = None,
) -> list[dict]:
    """The tech rows a signals listing renders as bullets — filtered to
    `candidates` when given, priority-ordered — shared by `_append_signals`
    (the live per-run listing) and the hourly desk-check summary so both
    name exactly the symbols they actually show, never more and never a
    second, divergent ordering.

    Uncapped (2026-09-17): this used to cut off at 4 rows while the header
    line right above it ("N analyzed") kept the true count — a live
    message read "5 analyzed" and then listed only 4, silently dropping
    the 5th (SOXX). The header must never claim more than the bullets
    beneath it actually show.
    """
    tech = [row for row in (snap.get("tech") or []) if isinstance(row, dict)]
    if candidates:
        wanted = {str(symbol).upper() for symbol in candidates}
        tech = [row for row in tech if str(row.get("symbol", "")).upper() in wanted]
    priority = {"strong_buy": 0, "strong_sell": 0, "buy": 1, "sell": 1, "neutral": 2}
    return sorted(
        tech,
        key=lambda row: (
            priority.get(str(row.get("rating", "")).lower(), 3),
            str(row.get("symbol", "")),
        ),
    )


def _signal_row_line(row: dict, profiles: dict[str, Any] | None = None) -> str:
    """One '   • SYM: RATING/conviction · R/R x.xx — reason' bullet, the
    one place that renders a tech-analysis row this way — shared by
    `_append_signals` (unchanged, default `profiles=None`: this text is
    the "full existing per-stock reasons... unchanged in content" DETAILS
    content) and the hourly desk-check summary, which passes `profiles` to
    put the company name inline since it has no separate DONE/LOOKED-AT
    section to have already introduced it."""
    sym = str(row.get("symbol", "?")).upper()
    label = _ticker_co(sym, profiles) if profiles is not None else sym
    rating = str(row.get("rating", "?")).upper()
    conviction = str(row.get("conviction", "?")).lower()
    rr = row.get("risk_reward")
    # Board item 89 clarity defect — "R/R 2.5" carried no unit. Same
    # figure, said as what it is: the reward measured in multiples of the
    # risk taken to get it.
    rr_text = (
        f" · reward {rr:g}× the risk" if isinstance(rr, (int, float)) else ""
    )
    reason = _clip(row.get("reasoning"), 420)
    text = f"   • {label}: {rating}/{conviction}{rr_text}"
    if reason:
        text += f" — {reason}"
    return text


def _append_intraday_evidence_freshness(
    lines: list[str], outer: dict | None, nested: dict | None,
) -> None:
    """The freshness disclosure on an intraday tick, in the owner's words.

    The record is attached to the tick result by `run_intra_check`; an older
    stored result, or a tick that never reached the decision, simply has
    none and this renders nothing.
    """
    from src.notifier import describe_evidence_freshness

    for source in (outer, nested):
        if not isinstance(source, dict):
            continue
        block = describe_evidence_freshness(source.get("evidence_freshness"))
        if block:
            lines.extend(block)
            return


def _append_signals(
    lines: list[str],
    snap: dict[str, Any],
    candidates: list[str] | None = None,
) -> None:
    tech = [row for row in (snap.get("tech") or []) if isinstance(row, dict)]
    if candidates:
        wanted = {str(symbol).upper() for symbol in candidates}
        tech = [row for row in tech if str(row.get("symbol", "")).upper() in wanted]
    if not tech:
        if candidates:
            lines.append(f"🔎 Triggered: {', '.join(candidates[:5])}")
        return

    actionable = [
        row for row in tech
        if str(row.get("rating", "")).lower() not in ("", "neutral")
    ]
    lines.append(f"🔎 Signals: {len(tech)} analyzed · {len(actionable)} actionable")
    for row in _signal_rows(snap, candidates):
        lines.append(_signal_row_line(row))


def _append_pm(lines: list[str], snap: dict[str, Any]) -> None:
    reasoning = snap.get("pm_reasoning")
    orders = [row for row in (snap.get("pm_orders") or []) if isinstance(row, dict)]
    targets = [row for row in (snap.get("pm_targets") or []) if isinstance(row, dict)]
    pm_summary = (snap.get("agent_summaries") or {}).get("portfolio_manager")
    if not (reasoning or orders or targets or pm_summary):
        return

    actionable = [row for row in orders if str(row.get("action", "")).upper() != "HOLD"]
    holds = [row for row in orders if str(row.get("action", "")).upper() == "HOLD"]
    if orders:
        lines.append(f"🧠 PM/Constructor: {len(actionable)} change(s) · {len(holds)} hold(s)")
        for row in actionable[:4]:
            action = str(row.get("action", "?")).upper()
            symbol = str(row.get("symbol", "?")).upper()
            allocation = row.get("allocation_pct")
            # Board item 89 clarity defect — a percentage with no
            # denominator. `allocation_pct` is the constructor's target
            # for this order as a share of the account.
            alloc_text = (
                f" {allocation:g}% of the account"
                if isinstance(allocation, (int, float)) else ""
            )
            reason = _clip(row.get("reasoning"), 420)
            text = f"   • {action} {symbol}{alloc_text}"
            if reason:
                text += f" — {reason}"
            lines.append(text)
        for row in holds[:2]:
            symbol = str(row.get("symbol", "?")).upper()
            reason = _clip(row.get("reasoning"), 420)
            text = f"   • PASS {symbol}"
            if reason:
                text += f" — {reason}"
            lines.append(text)
    elif targets:
        lines.append(f"🧠 PM: {len(targets)} target(s), no constructed order evidence")

    # Labelled "PM view (this check)" rather than a bare "View:" — this text
    # is the PM's own prose verbatim (never reworded here) and on an
    # intraday tick it can say something like "no trades today", which read
    # as a bare "View:" is easy to mistake for the whole day's verdict
    # rather than what it actually is: this one check's reasoning.
    portfolio_view = reasoning.get("portfolio_view") if isinstance(reasoning, dict) else None
    if portfolio_view:
        lines.append(f"   PM view (this check): {_clip(portfolio_view, 550)}")
    elif pm_summary and str(pm_summary).lower() != "no trades":
        lines.append(f"   PM view (this check): {_clip(pm_summary, 550)}")


# `models.RiskReasonCategory`, each value in the words a person would use
# (board item 89: internal status codes shown as-is). An unmapped value is
# described, with the raw token kept for the record, never guessed at.
_RISK_CATEGORY_WORDS: dict[str, str] = {
    "clean": "no changes asked for",
    "oversized": "sizing too aggressive for the conviction",
    "rr_fail": "reward too thin for the risk",
    "concentration": "too much in one sector or one name",
    "correlation_risk": "too many positions moving together",
    "event_risk": "an event (earnings, Fed, macro) too close",
    "macro_misalign": "plan runs against the market backdrop",
    "data_degraded": "several research inputs failed",
    "signal_fidelity": "PM contradicted the chart research without saying why",
    "other": "a reason outside the usual categories",
}


def _risk_category_words(category: Any) -> str:
    token = str(category or "").strip().lower()
    if not token:
        return "no category recorded"
    return _RISK_CATEGORY_WORDS.get(token) or (
        f"a category the desk has no plain wording for (kept for the record: {token})"
    )


def _append_risk(lines: list[str], snap: dict[str, Any]) -> None:
    risk = snap.get("risk")
    if not isinstance(risk, dict):
        return
    approved = risk.get("approved")
    label = "APPROVED" if approved is True else "REJECTED" if approved is False else "UNKNOWN"
    category = _risk_category_words(risk.get("reason_category"))
    scale = risk.get("scale_all_buys")
    scale_text = (
        f" · every buy cut to {scale * 100:.0f}% of the size asked for"
        if isinstance(scale, (int, float)) else ""
    )
    mods = snap.get("risk_mods") or []
    # Phase 10.1: a verdict can now be APPROVED overall and still have refused
    # individual names. Reading only `approved` would show that run as a clean
    # approval and never mention the trade that died.
    rejected = risk.get("rejected_symbols") or []
    rej_syms = sorted({
        str(r.get("symbol")) for r in rejected if isinstance(r, dict) and r.get("symbol")
    }) if isinstance(rejected, list) else []
    refused_text = f" · refused {', '.join(rej_syms)}" if rej_syms else ""
    lines.append(
        f"🛡️ Risk: {label} · {category}{scale_text} · {len(mods)} mod(s){refused_text}"
    )
    reason = _clip(risk.get("reasoning"), 550)
    if reason:
        lines.append(f"   {reason}")


def _execution_rows(snap: dict[str, Any]) -> tuple[list[dict], list[dict]]:
    trades = [row for row in (snap.get("trades") or []) if isinstance(row, dict)]
    sweep = [row for row in trades if str(row.get("action", "")).upper().startswith("SWEEP_")]
    real = [
        row for row in trades
        if str(row.get("action", "")).upper() != "HOLD"
        and not str(row.get("action", "")).upper().startswith("SWEEP_")
    ]
    return sweep, real


def _append_gate_and_execution(
    lines: list[str],
    result: dict,
    snap: dict[str, Any],
    *,
    explain_no_trade: bool = True,
) -> None:
    status = str(result.get("status", "unknown"))
    skips = [row for row in (result.get("execution_skips") or []) if isinstance(row, dict)]
    if not skips:
        skips = [row for row in (snap.get("skips") or []) if isinstance(row, dict)]

    if status in {"hard_risk_block", "symbol_block"}:
        lines.append(f"⚙️ Deterministic gate: BLOCKED — {_clip(result.get('reason'), 650)}")
    elif skips:
        lines.append(f"⚙️ Execution gate: {len(skips)} skip(s)")
        for row in skips[:3]:
            symbol = str(row.get("symbol", "?")).upper()
            # Board item 89 defect 5: this printed the internal reason code
            # verbatim ("• NVDA: fat_finger_guard"). `_skip_who` is the
            # plain-English map that already exists for exactly these codes
            # and the BLOCKED section already uses it; only this line
            # bypassed it.
            who = _skip_who(row.get("reason", ""))
            detail = _clip(row.get("detail"), 420)
            text = f"   • {symbol}: {who}"
            if detail:
                text += f" — {detail}"
            lines.append(text)

    sweep, real = _execution_rows(snap)
    if sweep:
        releases = sum(1 for row in sweep if str(row.get("action", "")).upper() == "SWEEP_SELL")
        parks = sum(1 for row in sweep if str(row.get("action", "")).upper() == "SWEEP_BUY")
        bits = []
        if releases:
            bits.append(f"{releases} T-bill cash release")
        if parks:
            bits.append(f"{parks} cash-park buy")
        if bits:
            lines.append("💵 Cash management: " + " · ".join(bits))

    if real:
        lines.append(f"⚡ Execution: {len(real)} broker action(s)")
        for row in real[:6]:
            action = str(row.get("action", "?")).upper()
            symbol = str(row.get("symbol", "?")).upper()
            qty = _number(row.get("fill_qty")) or _number(row.get("qty"))
            price = _number(row.get("fill_price")) or _number(row.get("price"))
            # Board item 89 defect 5: the raw fill-status token used to be
            # printed here ("· pending_submit").
            fill_state = _fill_state_plain(row.get("fill_status"))
            qty_text = f" {qty:g}" if qty is not None else ""
            price_text = f" @ ${price:,.2f}" if price is not None and price > 0 else ""
            lines.append(f"   • {action} {symbol}{qty_text}{price_text} · {fill_state}")
    elif explain_no_trade:
        _append_no_trade_reason(lines, result, snap, skips)


def _append_no_trade_reason(
    lines: list[str],
    result: dict,
    snap: dict[str, Any],
    skips: list[dict],
) -> None:
    status = str(result.get("status", "unknown"))
    risk = snap.get("risk")
    pm_orders = [row for row in (snap.get("pm_orders") or []) if isinstance(row, dict)]
    actionable = [row for row in pm_orders if str(row.get("action", "")).upper() != "HOLD"]
    pm_summary = str((snap.get("agent_summaries") or {}).get("portfolio_manager") or "")

    if status == "rejected":
        reason = _clip(result.get("reason"), 650)
        lines.append(f"⏸️ NO TRADE — Risk vetoed the plan{': ' + reason if reason else ''}")
    elif status in {"hard_risk_block", "symbol_block"}:
        lines.append("⏸️ NO TRADE — deterministic eligibility blocked the proposed action(s)")
    elif status == "buys_unfunded" or skips:
        lines.append("⏸️ NO TRADE — decision(s) survived review but execution could not complete")
    elif isinstance(risk, dict) and risk.get("approved") is False:
        lines.append(f"⏸️ NO TRADE — Risk rejected: {_clip(risk.get('reasoning'), 550)}")
    elif pm_orders and not actionable:
        lines.append("⏸️ NO TRADE — PM/constructor produced HOLD only")
    elif snap.get("pm_reasoning") or pm_summary:
        lines.append("⏸️ NO TRADE — PM produced no executable portfolio change")
    else:
        # Was "...; detailed PM evidence unavailable" — internal phrasing.
        # The honest plain-words version, which does NOT overclaim: there is
        # no stored reasoning for this run, which is not the same as saying
        # there was nothing to explain.
        lines.append(
            "⏸️ NO TRADE — nothing was bought or sold, and the desk stored "
            "no reasoning to explain for this run"
        )


def _append_footer(lines: list[str], snap: dict[str, Any], elapsed: float) -> None:
    """Duration and AI spend only — the owner-facing footer. Used to also
    print `run {run_id}` and the raw provider-request count ("LLM
    $0.10/2 provider requests"); both are engineering detail with no
    action attached to it on a phone alert, so 2026-09-17 dropped them.
    The run_id is still available wherever it actually matters (DB lookups,
    `send()`'s `link_url`/`symbols` args) — nothing depends on it being
    IN this printed text; only its presence in the rendered message
    changed.
    """
    bits: list[str] = []
    cost = snap.get("cost")
    if isinstance(cost, (int, float)):
        # In words, via the one shared helper (owner review, 2026-09-18).
        # This used to render "AI cost $0.0000", which he read as broken
        # rather than as the truthful price of a free-tier model.
        bits.append(describe_ai_cost(cost, label="AI cost"))
    bits.append(f"took {_fmt_elapsed(elapsed)}")
    lines.append("\U0001f9fe " + " \u00b7 ".join(bits))


def format_coverage_gap_line(row: dict, profiles: dict | None = None) -> str:
    """One owner-facing bullet for a stop-coverage gap.

    Company name, the two quantities as words, the dollars with no stop
    over them, and the reason the automatic repair gave when it refused
    (`repair_refusal` is a plain sentence stamped at the repair site;
    absent when it has nothing to say). Nothing here is estimated.

    Module-level and shared on purpose: `src/pipeline.py` sends the same
    facts as an interrupting alert when a session-hours re-placement fails,
    and two renderings of one condition are how the feed and the alert end
    up describing the same position differently.
    """
    name = _ticker_co(str(row.get("symbol", "?")), profiles)
    held = _number(row.get("held_qty"))
    covered = _number(row.get("covered_qty"))
    bits = [name]
    if held is not None:
        bits.append(f"holding {held:g}")
    if covered is not None:
        bits.append(f"stop covers {covered:g}")
    value = _number(row.get("unprotected_value"))
    if value is not None and value > 0:
        bits.append(f"${value:,.2f} unprotected")
    text = "   \u2022 " + ", ".join(bits)
    # `repair_refusal` is why the automatic repair placed nothing. `note` is
    # the same trailing-sentence slot for a coverage condition that had no
    # repair to refuse — an elected-but-unfilled stop, where the order is
    # present and correctly sized and simply did not execute.
    refusal = str(row.get("repair_refusal") or row.get("note") or "").strip()
    if refusal:
        text += f" \u2014 {_clip(refusal, 300)}"
    return text


def _append_coverage_gaps(lines: list[str], result: dict) -> None:
    """Spec §11.1 guard 3 — two banners, never one merged count.

    A position with NO stop at all and a position whose stop is present but
    mis-sized are different conditions: the first has nothing standing watch,
    the second has an order covering most of it. Reporting them as a single
    "N gaps" line buried the worse condition inside the milder one. Shares
    `notifier._gap_is_uncovered` so the two feeds can never disagree about
    which bucket a gap is in.
    """
    from src.notifier import _gap_is_uncovered

    gaps = result.get("stop_coverage_gaps")
    if not isinstance(gaps, list) or not gaps:
        return
    from src.notifier import _gap_is_expected_fractional

    from src.notifier import _gap_is_unreadable

    # Board item 172. Third bucket, not folded into either of the two that
    # state a measured fact about coverage.
    unreadable = [
        row for row in gaps
        if isinstance(row, dict) and _gap_is_unreadable(row)
    ]
    rows = [
        row for row in gaps
        if isinstance(row, dict) and not _gap_is_expected_fractional(row)
        and not _gap_is_unreadable(row)
    ]
    uncovered = [row for row in rows if _gap_is_uncovered(row)]
    partial = [row for row in rows if not _gap_is_uncovered(row)]
    if not uncovered and not partial and not unreadable:
        return
    profiles = _profiles(uncovered, partial, unreadable)

    def _gap_line(row: dict) -> str:
        return format_coverage_gap_line(row, profiles)

    if unreadable:
        lines.append(
            f"🛑🛑 STOP UNREADABLE: {len(unreadable)} position(s) the broker "
            "could not be asked about"
        )
        for row in unreadable[:8]:
            symbol = str(row.get("symbol", "?")).upper()
            held = _number(row.get("held_qty"))
            held_text = f", holding {abs(held):g}" if held is not None else ""
            reason = str(row.get("read_error") or "").strip()
            lines.append(
                f"   • {_ticker_co(symbol, profiles)}{held_text} — "
                + (reason or "the stop query failed")
            )
        lines.append(
            "   Whether these have a stop is UNKNOWN — not confirmed "
            "missing and not confirmed present. Check the position's open "
            "orders at the broker directly."
        )
    if uncovered:
        lines.append(f"🛑🛑🛑 NO STOP AT ALL: {len(uncovered)} position(s) with nothing protecting them")
        lines.extend(_gap_line(row) for row in uncovered[:8])
        lines.append(
            "   The desk's automatic re-protection did not close this. Place "
            "a protective stop by hand or close the position."
        )
    if partial:
        lines.append(f"⚠️ STOP MIS-SIZED: {len(partial)} position(s) only partly protected")
        lines.extend(_gap_line(row) for row in partial[:8])
        lines.append(
            "   A stop is standing watch over part of each position; the "
            "rest has none. The desk re-checks at its next scheduled pass."
        )


def _append_done(lines: list[str], rows: list[dict], snap: dict, profiles: dict) -> None:
    """NEW LAYOUT item 3 — orders that actually reached the broker, one
    line each, the true fill state (never implying a fill that hasn't
    happened — `trades.fill_status` stays 'submitted' until it has)."""
    if not rows:
        return
    lines.append(_b("✅ DONE"))
    for row in rows:
        action = str(row.get("action", "?")).upper()
        symbol = str(row.get("symbol", "?")).upper()
        qty = _number(row.get("fill_qty")) or _number(row.get("qty"))
        price = _number(row.get("fill_price")) or _number(row.get("price"))
        fill_status = str(row.get("fill_status") or "").lower()
        state = "filled" if fill_status == "filled" else "placed, waiting to fill"
        qty_text = f"{qty:g}" if qty is not None else "?"
        price_text = f"${price:,.2f}" if price is not None and price > 0 else "?"
        stop = _symbol_stop(symbol, snap)
        stop_text = f" · stop ${stop:,.2f}" if stop is not None else ""
        lines.append(
            f"   • {action} {_ticker_co(symbol, profiles)} {qty_text} @ "
            f"{price_text} — {state}{stop_text}"
        )


def _append_blocked(lines: list[str], rows: list[dict], profiles: dict) -> None:
    """NEW LAYOUT item 4 — who actually stopped it, in plain words. See
    `_blocked_rows` for the four sources this merges.

    TWO headings, not one. The old single "❌ BLOCKED / FAILED" heading
    filed the risk system working correctly under the word FAILED, which is
    the same defect as the header word and had to be fixed in the same pass
    — a header that says NO TRADE over a section that says FAILED just
    moves the confusion down one line. Rows the desk refused ON PURPOSE go
    under "🚫 NOT TAKEN"; rows where something broke keep "❌ FAILED".
    Every row still appears, with the same wording as before.
    """
    if not rows:
        return
    refused = [row for row in rows if not row.get("fault", True)]
    faults = [row for row in rows if row.get("fault", True)]

    def _bullets(group: list[dict]) -> None:
        for row in group:
            # 600, not 300: a constructor refusal sentence runs ~250
            # characters and a broker reason can run longer; a cut
            # mid-sentence was item 89's "detail truncated" defect.
            reason = _clip(row.get("reason"), 600)
            lines.append(
                f"   • {row['action']} {_ticker_co(row['symbol'], profiles)} — "
                f"{row['who']}: {reason}"
            )

    if refused:
        lines.append(_b("🚫 NOT TAKEN"))
        lines.append(
            "   The desk decided against these. Nothing broke."
        )
        _bullets(refused)
    if faults:
        if refused:
            lines.append("")
        lines.append(_b("❌ FAILED"))
        _bullets(faults)


def _pm_pass_reason(
    symbol: str, snap: dict[str, Any] | None,
) -> tuple[str | None, str]:
    """The desk's own recorded ground for not trading `symbol` this run, in
    plain words, or None when there genuinely is not one.

    TWO sources, in the order of how directly they answer the question:

      1. The per-candidate accounting row (`src/pm_accounting.py`, board
         item 133). This is the seat being MADE to account for every
         candidate it dropped, so on an ordinary session it covers all of
         them. It was persisted from the day item 133 shipped and read by
         nothing — see the `pm_accounting` note in `_empty_snapshot` — and
         that is the whole defect this function fixes: on 2026-09-23 the
         desk logged "every one of the 69 non-targeted candidate(s) carries
         a named ground" seven-tenths of a second before telling the owner,
         sixty-eight times over, that it "did not record why".
      2. Failing that, a HOLD order the PM wrote its own reasoning on.

    The accounting row's CODE is never shown; `pm_accounting.plain_reason`
    turns it into the sentence a person would use.

    Returns `(ground, detail)`. The GROUND is the shared sentence the
    renderer groups on — several names die on the same one — and the DETAIL
    is that one name's own specifics, which must stay off the group header
    or nothing would ever group. A `None` ground is reserved for an honest
    gap: a candidate with no recorded ground at all.
    """
    from src.pm_accounting import plain_reason

    accounted = ((snap or {}).get("pm_accounting") or {}).get(symbol)
    if isinstance(accounted, dict):
        code = str(accounted.get("refusal") or "").strip()
        if code:
            return plain_reason(code), _clip(accounted.get("note"), 300) or ""
    for row in (snap or {}).get("pm_orders") or []:
        if (
            isinstance(row, dict)
            and str(row.get("symbol", "")).upper() == symbol
            and str(row.get("action", "")).upper() == "HOLD"
        ):
            reason = _clip(row.get("reasoning"), 300)
            if reason:
                return reason, ""
    return None, ""


def _looked_at_groups(
    rows: list[dict], profiles: dict, snap: dict[str, Any] | None,
) -> list[tuple[str, list[tuple[str, str, str]]]]:
    """`[(ground, [(ticker, name_with_rating, detail), ...]), ...]`, groups
    in the order their first name appears — which is the existing priority
    ordering from `_signal_rows`, so the strongest ratings still lead.

    Split out of `_looked_at_block` so all four render tiers below
    group identically: a tier that grouped differently from another could
    show the owner a different count for the same session.
    """
    grouped: dict[str, list[tuple[str, str, str]]] = {}
    for row in rows:
        symbol = str(row.get("symbol", "?")).upper()
        rating = str(row.get("rating", "?")).upper()
        conviction = str(row.get("conviction", "?")).lower()
        reason, detail = _pm_pass_reason(symbol, snap)
        # The honest fallback is conditioned on there being NO recorded
        # ground — never on the renderer being unable to reach one.
        ground = (
            f"the desk did not take these because {reason}" if reason
            else "the desk did not record why it passed on these"
        )
        grouped.setdefault(ground, []).append(
            (symbol, f"{_ticker_co(symbol, profiles)} {rating}/{conviction}",
             detail),
        )
    return list(grouped.items())


def _looked_at_block(
    rows: list[dict], profiles: dict, snap: dict[str, Any] | None = None,
    budget: int | None = None,
) -> list[str]:
    """NEW LAYOUT item 5 — analyzed signals the PM/constructor passed on,
    rendered at the richest of three tiers that fits `budget`.

    GROUPED BY THE GROUND, not one reason per name (PR #600). A real
    session leaves sixty-odd candidates here and most of them die on the
    SAME ground — on 2026-09-23, thirty-nine of sixty-nine on one.

    WHY THERE ARE TIERS. Measured on run-fccb2026 (2026-09-23, replayed
    against the post-#600 code): this block alone renders 8,376 characters
    for 68 candidates, against a 4,000-character message budget. It was
    built first and took all of it, which is why the DETAILS block carrying
    the desk's actual reasoning rendered EMPTY in every live morning
    message. Clipping was not an option either: the clip would land
    mid-list and silently lose names.

    So the block degrades instead, and NO NAME IS EVER DROPPED at any tier
    — the #600 rule stands that a grouping which hides a name is a worse
    failure than a repetitive one. What is given up, in order, is the
    per-name detail sentence and then the one-name-per-line layout:

      tier 1  ground heading, then each name on its own line with its own
              detail sentence.
      tier 2  ground heading, then each name on its own line.
      tier 3  ground heading, then every name and its rating comma-joined
              on one line. Telegram soft-wraps it.
      tier 4  the same, tickers only.

    The company name goes before the rating because a ticker the owner does
    not recognise tells him nothing (board item 89), so it is the last
    thing given up rather than the first.

    Tier 4 is the FLOOR and is returned whatever `budget` says, because a
    block that renders nothing would put the owner back where #600 found
    him. The count in every heading still accounts for every candidate at
    every tier. `budget=None` means "no ceiling" — tier 1, the old
    behaviour, which is what the position-review and evening formatters
    and the stored-report replay path still get.
    """
    if not rows:
        return []
    groups = _looked_at_groups(rows, profiles, snap)
    header = _b("👀 LOOKED AT, NO TRADE")

    def _tier1() -> list[str]:
        out = [header]
        for ground, entries in groups:
            out.append(f"   ▪ {len(entries)} — {ground}")
            # A per-name detail that is word-for-word the same for every
            # name under the heading is the heading again — the
            # `held_unchanged` accounting note is one fixed sentence, and
            # printing it twelve times under a heading that already says it
            # is the repetition this grouping exists to remove. Dropped
            # only when it is shared by the whole group AND the group has
            # more than one name, so a name whose detail is its own is
            # never silently lost.
            details = {detail for _, _, detail in entries}
            shared = len(entries) > 1 and len(details) == 1
            for _symbol, name, detail in entries:
                out.append(
                    f"      • {name} — {detail}"
                    if detail and not shared else f"      • {name}"
                )
        return out

    def _tier2() -> list[str]:
        out = [header]
        for ground, entries in groups:
            out.append(f"   ▪ {len(entries)} — {ground}")
            for _symbol, name, _detail in entries:
                out.append(f"      • {name}")
        return out

    def _tier3() -> list[str]:
        out = [header]
        for ground, entries in groups:
            out.append(f"   ▪ {len(entries)} — {ground}")
            out.append("      " + ", ".join(name for _s, name, _d in entries))
        return out

    def _tier4() -> list[str]:
        out = [header]
        for ground, entries in groups:
            out.append(f"   ▪ {len(entries)} — {ground}")
            out.append(
                "      " + ", ".join(symbol for symbol, _n, _d in entries)
            )
        return out

    for build in (_tier1, _tier2, _tier3):
        block = build()
        if budget is None or len("\n".join(block)) <= budget:
            return block
    return _tier4()


def _append_looked_at(
    lines: list[str], rows: list[dict], profiles: dict,
    snap: dict[str, Any] | None = None, budget: int | None = None,
) -> None:
    """`_looked_at_block`, appended in place — the `_new_block` shape.

    The two formatters that carry a candidate list no longer call this:
    they go through `_budgeted_sections`, which has to know the block's
    size before deciding where to put it. Kept as the in-place form for
    any caller that wants the block with no budget at all.
    """
    lines.extend(_looked_at_block(rows, profiles, snap, budget))


def _append_rotation(lines: list[str], snap: dict[str, Any] | None) -> None:
    """The opportunity-rotation pre-check, said to the owner.

    Sits directly under LOOKED AT because it answers the same question from
    the other side: LOOKED AT says why each individual candidate was passed
    on, and this says whether the desk weighed the whole field against what
    it already holds and what that comparison concluded.

    Deliberately NOT under a warning heading and deliberately not phrased as
    a fault. A full book is how this desk is meant to run; the owner's
    words, 2026-09-23: "Yes portfolio is full. But we're still reviewing
    things, which is how we built it."

    One vocabulary — the sentences come from `rotation.owner_precheck_lines`,
    reading the same durable row `_record_rotation_precheck` wrote. Renders
    nothing for a run with no pre-check row (every session before this
    shipped, and any stored report replayed from one).
    """
    from src.rotation import owner_precheck_lines

    lines.extend(owner_precheck_lines((snap or {}).get("rotation")))


def _append_held(lines: list[str], symbols: list[str], profiles: dict) -> None:
    """NEW LAYOUT item 6 — every held ticker, once, with the correct count
    (the defect this fixes: a live message once said "7 hold(s)" over 6
    positions with only 2 actually listed)."""
    if not symbols:
        return
    lines.append(_b(f"HELD ({len(symbols)})"))
    for symbol in symbols:
        lines.append(f"   • {_ticker_co(symbol, profiles)}")


def _watch_rows(result: dict) -> list[dict]:
    """Positions already flagged by code the desk runs today — the
    STOP MIS-SIZED half of `_append_coverage_gaps` (the milder of its two
    banners; "NO STOP AT ALL" stays the loud top-of-message 🛑🛑🛑 banner it
    already is). No new threshold is invented here — only what
    `_gap_is_uncovered` already classifies.

    Board item 172: a row whose stops could not be READ is excluded. WATCH
    renders "the stop covers only part of the position", which states that a
    stop exists and is undersized — two facts an unreadable row establishes
    neither of. It has its own banner above the numbers instead."""
    from src.notifier import _gap_is_uncovered, _gap_is_unreadable

    gaps = result.get("stop_coverage_gaps")
    if not isinstance(gaps, list):
        return []
    return [
        row for row in gaps
        if isinstance(row, dict) and not _gap_is_uncovered(row)
        and not _gap_is_unreadable(row)
    ]


def _append_watch(lines: list[str], rows: list[dict], profiles: dict) -> None:
    if not rows:
        return
    lines.append(_b("⚠️ WATCH"))
    for row in rows:
        symbol = str(row.get("symbol", "?")).upper()
        held = _number(row.get("held_qty"))
        covered = _number(row.get("covered_qty"))
        sizes = (
            f" (holding {held:g}, stop covers {covered:g})"
            if held is not None and covered is not None else ""
        )
        lines.append(
            f"   • {_ticker_co(symbol, profiles)} — the stop covers only part "
            f"of the position{sizes}"
        )


def _format_decision_session(mode: str, result: dict, elapsed: float) -> str:
    run_id = result.get("run_id")
    snap = _read_run(run_id)
    # A caller may supply `_positions` already — that is how a stored
    # morning report is re-rendered (`render_stored_session_report`).
    # `_read_run`'s positions query is deliberately NOT run-scoped (it
    # reads the book as it stands right now), so replaying an older run
    # through it would print today's holdings under that run's date. A
    # supplied snapshot wins; the live morning path never sets one.
    if isinstance(result.get("_positions"), list):
        snap = dict(snap)
        snap["positions"] = result["_positions"]
    status = str(result.get("status", "unknown"))

    done_rows = _done_rows(snap)
    blocked_rows = _blocked_rows(result, snap)
    acted = {row["symbol"] for row in done_rows} | {row["symbol"] for row in blocked_rows}
    looked_at_rows = _looked_at_rows(snap, None, acted)
    profiles = _profiles(done_rows, blocked_rows, looked_at_rows)

    outcome = _outcome_word(
        status, len(done_rows), len(blocked_rows), done_rows,
        fault_count=_fault_count(blocked_rows),
    )
    lines = [f"{_status_emoji(status)} {mode.upper()} · {fmt_time_12h(et_now())} · {outcome}"]

    # P&L FIRST, directly under the heading — owner, 2026-09-18: "all the
    # P&L information has to go at the very top of every telegram alert,
    # right after the first line, which is really the heading." A repeat
    # correction: it kept drifting below whatever banner was added next.
    _new_section(lines, *_pnl_section_lines(result))

    _new_block(lines, _append_coverage_gaps, result)

    data_status = result.get("data_status") or {}
    if isinstance(data_status, dict):
        from src import evidence_gate
        degraded = {
            name: value for name, value in data_status.items()
            if evidence_gate.counts_as_degraded(value)
        }
        if degraded:
            # Board item 89 clarity defect — this named internal components
            # ("macro, tech"). Same seats, in words, plus what it means.
            _new_section(
                lines,
                "⚠️ Research was incomplete this session:",
                *(f"   • {line}" for line in describe_data_status(degraded)),
                "   The decisions below were made without that input.",
            )

    _new_block(lines, _append_market, snap)
    _new_block(lines, _append_book, snap)
    _new_block(lines, _append_done, done_rows, snap, profiles)
    _new_block(lines, _append_blocked, blocked_rows, profiles)
    # The candidate list is rendered LAST and spliced back in HERE — see
    # `_budgeted_sections` for why it may not claim the budget first.
    looked_at_slot = len(lines)
    _new_block(lines, _append_rotation, snap)

    # THE REASONING LEADS, THE ENUMERATION TRAILS. Order matters inside
    # DETAILS for the same reason it matters outside it: if this block has
    # to be clipped, the clip must land in the per-candidate signals
    # listing — which is the same 69 names the LOOKED AT block above
    # already accounts for, read from a different seat — and never in the
    # PM's reasoning chain, the risk verdict or the execution record, which
    # appear nowhere else in the message. Measured on run-fccb2026: the
    # signals listing is 9,379 of the block's 9,799 characters; the PM
    # reasoning is 303, the risk verdict 0 and the execution record 114.
    detail_lines: list[str] = []
    _new_block(detail_lines, _append_pm, snap)
    _new_block(detail_lines, _append_risk, snap)
    _new_block(detail_lines, _append_gate_and_execution, result, snap)
    _new_block(detail_lines, _append_signals, snap)
    _budgeted_sections(
        lines, looked_at_slot, looked_at_rows, profiles, snap, detail_lines,
    )

    _new_block(lines, _append_footer, snap, elapsed)
    return "\n".join(lines)


def _held_symbols(snap: dict[str, Any]) -> list[str]:
    """Every symbol actually held, read from broker-truth `positions` —
    NOT the reviewer's self-reported `actions` list, which is LLM output
    and can under- or over-count (the defect this fixes: a live message
    once said "7 hold(s)" over 6 real positions, with only 2 ever named).
    Ordered exactly as `_append_book` already ranks them (by position
    size), excludes cash-sweep vehicles the same way."""
    positions = [row for row in (snap.get("positions") or []) if isinstance(row, dict)]
    return [
        str(row.get("symbol", "")).upper() for row in positions
        if str(row.get("symbol", "")).upper() not in _SWEEP_SYMBOLS
    ]


def _format_position_review(mode: str, result: dict, elapsed: float) -> str:
    run_id = result.get("run_id")
    snap = _read_run(run_id)
    # See `_format_decision_session`'s identical comment: a supplied
    # `_positions` snapshot is how a stored midday/close report is
    # re-rendered without printing today's book under an old date.
    if isinstance(result.get("_positions"), list):
        snap = dict(snap)
        snap["positions"] = result["_positions"]
    status = str(result.get("status", "unknown"))
    review = result.get("review") if isinstance(result.get("review"), dict) else {}

    done_rows = _done_rows(snap)
    blocked_rows = _blocked_rows(result, snap)
    held_symbols = _held_symbols(snap)
    watch_rows = _watch_rows(result)
    profiles = _profiles(done_rows, blocked_rows, held_symbols, watch_rows)

    outcome = _outcome_word(
        status, len(done_rows), len(blocked_rows), done_rows,
        fault_count=_fault_count(blocked_rows),
    )
    lines = [
        f"{_status_emoji(status)} {mode.upper()} REVIEW · "
        f"{fmt_time_12h(et_now())} · {outcome}"
    ]

    # P&L FIRST, directly under the heading (owner 2026-09-17, restated
    # 2026-09-18 as the standing rule for EVERY message). Always shown,
    # never silently dropped the way the old "Session P&L" line was on
    # every midday/close message.
    _new_section(lines, *_pnl_section_lines(result))

    _new_block(lines, _append_coverage_gaps, result)

    positions = result.get("positions")
    risk_level = review.get("risk_level")

    def _render_review_summary(lines: list[str]) -> None:
        # Board item 89 defect 2 — an assertion computed independently of
        # the list underneath it.
        #
        # This line used to print `result["positions"]`, which
        # `run_position_review` sets to `len(positions)` from the snapshot
        # taken at the START of the session, BEFORE the reviewer's own
        # exits ran and before the closing `_sync_positions_from_broker`.
        # The HELD block a few lines below reads the book AFTER all of
        # that. So on any session that actually sold something the header
        # claimed a number, and the list beneath it named a different set
        # — the owner had no way to tell which one was his book.
        #
        # The count is now taken from `held_symbols`, the very list that is
        # rendered below it, so the two cannot disagree. The pre-session
        # count is not discarded: when it differs, the message says the
        # book changed during the session rather than printing two numbers
        # and leaving the reader to reconcile them.
        if not held_symbols and not risk_level and not positions:
            return
        bits = [f"{len(held_symbols)} position(s) held now"]
        if isinstance(positions, int) and positions != len(held_symbols):
            bits.append(f"{positions} at the start of this session")
        if risk_level:
            # Board item 89 clarity defect — an unscaled risk rating. Same
            # scale treatment the evening message gives it.
            bits.append(f"risk {_risk_with_scale(risk_level)}")
        lines.append("📍 Review: " + " · ".join(bits))

    _new_block(lines, _render_review_summary)

    _new_block(lines, _append_done, done_rows, snap, profiles)
    _new_block(lines, _append_blocked, blocked_rows, profiles)
    _new_block(lines, _append_held, held_symbols, profiles)
    _new_block(lines, _append_watch, watch_rows, profiles)
    _new_block(lines, _append_book, snap)

    actions = [row for row in (review.get("actions") or []) if isinstance(row, dict)]
    actionable = [row for row in actions if str(row.get("action", "")).upper() != "HOLD"]
    holds = [row for row in actions if str(row.get("action", "")).upper() == "HOLD"]

    detail_lines: list[str] = []
    overall = _clip(review.get("overall_assessment"), 650)
    if overall:
        detail_lines.append(f"🧠 Reviewer: {overall}")

    def _render_decisions(lines: list[str]) -> None:
        if not actions:
            return
        lines.append(f"🎯 Decisions: {len(actionable)} action(s) · {len(holds)} hold(s)")
        # Uncapped — same reasoning as `_signal_rows`: a header count must
        # never claim more than the bullets beneath it actually show.
        for row in actionable:
            action = str(row.get("action", "?")).upper()
            symbol = str(row.get("symbol", "?")).upper()
            stop = _number(row.get("new_stop_price"))
            stop_text = f" → stop ${stop:,.2f}" if stop is not None else ""
            reason = _clip(row.get("reason"), 420)
            text = f"   • {action} {symbol}{stop_text}"
            if reason:
                text += f" — {reason}"
            lines.append(text)
        for row in holds:
            symbol = str(row.get("symbol", "?")).upper()
            reason = _clip(row.get("reason"), 420)
            text = f"   • HOLD {symbol}"
            if reason:
                text += f" — {reason}"
            lines.append(text)

    _new_block(detail_lines, _render_decisions, may_glue=True)

    def _render_gate_and_outcome(lines: list[str]) -> None:
        _append_gate_and_execution(lines, result, snap, explain_no_trade=False)
        _, real = _execution_rows(snap)
        if not real:
            if actions and holds and not actionable:
                lines.append("⏸️ NO ACTION — reviewer explicitly held the book")
            elif not actions and positions == 0:
                lines.append("⏸️ NO ACTION — no market-risk positions required review")
            elif not actions and status == "reviewed":
                lines.append("⏸️ NO ACTION — review completed with no broker action")

    _new_block(detail_lines, _render_gate_and_outcome)
    _wrap_details(lines, detail_lines)

    _new_block(lines, _append_footer, snap, elapsed)
    return "\n".join(lines)


def _fmt_pnl_line(label: str, pnl: float | None, ret: float | None) -> str:
    """'📈 Today's P&L: +$12.34 (+0.10%)' — a missing figure renders as the
    honest 'not available', never a fabricated 0. If BOTH are unknown the
    parenthetical is dropped too ('not available (not available)' reads as
    a bug, not an honest gap)."""
    if pnl is None and ret is None:
        return f"{label} not available"
    pnl_text = _fmt_signed_money(pnl) if pnl is not None else "not available"
    ret_text = f"{ret:+.2f}%" if ret is not None else "not available"
    return f"{label} {pnl_text} ({ret_text})"


def _pnl_section_lines(result: dict) -> list[str]:
    """The two-line P&L block every trader-feed message shows in the spot
    the old, undated 'Session P&L' line used to sit (owner request,
    2026-09-17): he did not know whether "session" meant this run, this
    calendar day, or something else — it actually meant this run's/tick's
    OWN process, i.e. it was silently absent on every midday/close message
    (`run_position_review` never set `daily_pnl` at all) and, when it did
    show on an intraday tick, was really just today's account change under
    an unexplained label.

    'Today's P&L' is the broker's own day-over-day account change
    (today's total value vs. its `last_equity`, i.e. official prior-close
    equity) — the SAME basis `run_evening`'s already-trusted 'Daily P&L'
    line uses (src/notifier.py), just not yet 4pm-close-verified mid-day.

    'Total P&L' is deliberately labelled with the date it starts from
    rather than called "total" bare: the 2026-09-02 book-wide liquidation
    archived every earlier record (docs/INCIDENT_HISTORY.md), so a total
    spanning that boundary would be meaningless — seeing "total" would
    read as "since the account began", which it is not. See
    `TradingPipeline._total_pnl_since_reset` for exactly which recorded
    value the baseline comes from (never reconstructed).
    """
    today_pnl = _number(result.get("daily_pnl"))
    today_ret = _number(result.get("daily_return_pct"))
    total_pnl = _number(result.get("total_pnl"))
    total_ret = _number(result.get("total_return_pct"))
    since = result.get("total_pnl_since")
    total_label = f"📊 Total P&L since {since}:" if since else "📊 Total P&L:"
    lines = [
        _fmt_pnl_line("📈 Today's P&L:", today_pnl, today_ret),
        _fmt_pnl_line(total_label, total_pnl, total_ret),
    ]
    # Owner, 2026-09-18: P&L leads EVERY message, and a message that cannot
    # know the figure says so rather than dropping the block — an absent
    # block and a broken figure look identical to a reader. `_fmt_pnl_line`
    # already renders each missing figure as "not available"; when NOTHING
    # is known, one short sentence says why, so "not available" is never
    # left looking like a fault.
    if today_pnl is None and today_ret is None and total_pnl is None and total_ret is None:
        lines.append("   " + _pnl_unavailable_sentence(result))
    return lines


# Why a figure is missing, keyed by the code the SESSION sets on its own
# result (`TradingPipeline._attach_pnl` / `run_earnings_preprocess`).
#
# Owner rule: an explanation the code cannot prove is itself a defect. The
# old block asserted "this message was built without an account read"
# whenever the four keys were absent — which was false for every trading
# session (2026-09-23: the same morning message that said it went on to
# print "Book: 12 position(s) · $18,178 invested"). The absence of the keys
# is evidence of nothing but their absence, so the sentence now comes from
# the only place that KNOWS the reason: the run that did or did not read the
# account. An unrecognised or absent code falls back to a sentence that
# claims no cause at all.
_PNL_UNAVAILABLE_REASONS = {
    # The message's own mode does no account read at all (pre-market
    # filing reader).
    "no_account_read":
        "This message was built without an account read, so there is no "
        "figure yet.",
    # The session ended — holiday short-circuit, kill switch, broker
    # snapshot failure — before it reached its account read.
    "ended_before_account_read":
        "This run ended before the account was read, so there is no figure "
        "yet.",
    # The account WAS read, but the broker reported no usable prior-day
    # close to measure today's change against.
    "no_prior_close":
        "The broker reported no prior-day close, so today's change cannot "
        "be measured.",
}
_PNL_UNAVAILABLE_FALLBACK = "No P&L figure was recorded with this message."


def _pnl_unavailable_sentence(result: dict) -> str:
    code = result.get("pnl_unavailable_reason")
    return _PNL_UNAVAILABLE_REASONS.get(str(code or ""), _PNL_UNAVAILABLE_FALLBACK)


# === Evening report (2026-09-18 owner redesign) ===
#
# The evening message used to be rendered by the base notifier formatter,
# which predates the scan-first layout every other session message now uses.
# Owner review of the live 2026-09-17 message asked for six removals and
# three changes; each is implemented below and named where it happens:
#
#   * the run id and the provider-request count are gone (they are internal
#     identifiers with no action attached — same reasoning as `_append_footer`)
#   * "status: analyzed" is gone: it only ever meant "the evening review
#     produced a parseable answer" (src/pipeline.py `run_evening` sets
#     'analyzed' when `analysis is not None` and an error status otherwise),
#     so the normal case carried no information. The header's outcome word
#     says REVIEWED, and a failed review now says so in a sentence.
#   * the cost renders in words, never as "$0.0000"
#   * the overnight-fractional line is silent in the expected state
#     (see `_evening_fractional_line`)
#   * today's and the dated total P&L lead the message
#   * sections carry `<b>` headings, like every other feed message


def _evening_outcome(status: str, analysis: Any) -> str:
    """ONE plain word for the header — REVIEWED / REVIEW FAILED.

    This is what "status: analyzed" is translated into. `run_evening`'s
    status is a two-state fact: 'analyzed' when the evening analyst returned
    a parseable review, 'evening_analysis_error'/'evening_parse_error' when
    it did not. The owner cannot act on the success case, so it is carried
    by the header word alone; the failure case gets a sentence below.
    """
    if analysis:
        return "REVIEWED"
    if "error" in str(status or ""):
        return "REVIEW FAILED"
    return "REVIEWED"


def _evening_pnl_lines(result: dict) -> list[str]:
    """Today's P&L and the dated total, in that order, at the very top of
    the message (owner request, 2026-09-17).

    Today's figure prefers the true 4pm close-to-close P&L the evening run
    computes from broker portfolio history (`pnl_4pm`/`equity_close`) and
    falls back to the real-time prior-close→now difference, exactly as the
    base formatter did. The total is the same dated figure every other feed
    message shows (`_pnl_section_lines`), so the two can never disagree.
    A figure that is genuinely unavailable says so — never a rendered 0.
    The account's own value follows them, as it did before the redesign.
    """
    pnl_4pm = _number(result.get("pnl_4pm"))
    equity_close = _number(result.get("equity_close"))
    today_ret: float | None
    if pnl_4pm is not None and equity_close is not None:
        today_pnl = pnl_4pm
        baseline = equity_close - pnl_4pm
        today_ret = (pnl_4pm / baseline * 100) if baseline > 0 else None
        suffix = "  ·  at the 4pm close"
    else:
        today_pnl = _number(result.get("daily_pnl"))
        today_ret = _number(result.get("daily_return_pct"))
        suffix = ""
    total_pnl = _number(result.get("total_pnl"))
    total_ret = _number(result.get("total_return_pct"))
    since = result.get("total_pnl_since")
    total_label = f"📊 Total P&L since {since}:" if since else "📊 Total P&L:"
    lines = [
        _fmt_pnl_line("📈 Today's P&L:", today_pnl, today_ret) + suffix,
        _fmt_pnl_line(total_label, total_pnl, total_ret),
    ]
    # The account's own value, kept from the pre-redesign message: it is
    # the denominator both figures above are a change in, and the owner
    # never asked for it to go.
    equity = equity_close if equity_close is not None else _number(result.get("total_value"))
    if equity is not None:
        lines.append(f"   Account value: ${equity:,.2f}")
    return lines


def _evening_fractional_line(result: dict) -> str | None:
    """The overnight sub-share remainder line — but ONLY when the overnight
    state is not the one the design produces every night.

    Owner review, 2026-09-17: "overnight fractional unprotected by design"
    appeared on every single evening message and restated a design fact he
    had already ratified. A line that never varies is not information.

    What "abnormal" means here is read off the mechanism, not chosen: a
    fractional position's whole-share leg carries a GTC stop that does not
    expire, and the sub-share remainder carries a DAY stop that the broker
    expires at the close and the next session re-places. That is the
    expected state and it now says nothing. Two states are NOT expected and
    still speak:

      * a holding of less than one whole share — there is no whole-share
        leg, so `_classify_coverage_gap` calls it 'fractional' while the
        ENTIRE position is stopless overnight (the worst case is the whole
        position, not a remainder — corrected 2026-09-04);
      * a remainder the session's own sweep recorded as replaced but which
        did not in fact come back covered.

    Positions with no stop at all, and stops mis-sized on the whole-share
    leg, are not this line's business — they have their own banners above
    and are never suppressed.
    """
    from src.notifier import _gap_is_expected_fractional

    gaps = result.get("stop_coverage_gaps")
    if not isinstance(gaps, list):
        return None
    abnormal: list[dict] = []
    for gap in gaps:
        if not isinstance(gap, dict) or not _gap_is_expected_fractional(gap):
            continue
        coverage = str(gap.get("coverage", "")).strip().lower()
        covered = _number(gap.get("covered_qty"))
        if coverage == "fractional_overnight" and (covered is None or covered <= 0):
            abnormal.append(gap)
        elif coverage == "fractional_replaced" and not gap.get("repaired"):
            abnormal.append(gap)
    if not abnormal:
        return None
    total = 0.0
    for gap in abnormal:
        value = _number(gap.get("unprotected_value"))
        if value is not None:
            total += value
    names = ", ".join(str(gap.get("symbol", "?")) for gap in abnormal[:6])
    return (
        f"🛑 NO STOP OVERNIGHT: {len(abnormal)} holding(s) under one whole "
        f"share have nothing protecting them tonight — {names}"
        + (f" (${total:,.2f})" if total > 0 else "")
    )


def _append_evening_banners(lines: list[str], result: dict) -> None:
    """Everything that must be read before the numbers. Same conditions the
    base formatter raised, in the same order, minus the nightly fractional
    line (see `_evening_fractional_line`)."""
    from src.notifier import _gap_is_expected_fractional, _gap_is_uncovered

    missing = result.get("missing_sessions")
    if isinstance(missing, list) and missing:
        hard = [m for m in missing
                if m == "morning" or str(m).startswith("morning (")]
        for entry in hard:
            detail = entry if entry != "morning" else (
                "no activity was logged this morning — check the scheduler"
            )
            lines.append(f"🛑 MORNING SESSION DID NOT RUN — {detail}")
        soft = [m for m in missing if m not in hard]
        if soft:
            lines.append(f"⚠️ No activity logged today for: {', '.join(soft)}")

    from src.notifier import _gap_is_unreadable

    gaps = [g for g in (result.get("stop_coverage_gaps") or []) if isinstance(g, dict)]
    # Board item 172 — same partition the base formatter uses, for the same
    # reason: an unreadable row asserts nothing about coverage and must not
    # be counted into a banner that does.
    unreadable = [g for g in gaps if _gap_is_unreadable(g)]
    faults = [
        g for g in gaps
        if not _gap_is_expected_fractional(g) and not _gap_is_unreadable(g)
    ]
    uncovered = [g for g in faults if _gap_is_uncovered(g)]
    partial = [g for g in faults if not _gap_is_uncovered(g)]
    if unreadable:
        names = ", ".join(str(g.get("symbol", "?")) for g in unreadable[:6])
        lines.append(
            f"🛑🛑 STOP UNREADABLE: {len(unreadable)} position(s) the broker "
            f"could not be asked about — coverage UNKNOWN — {names}"
        )
    if uncovered:
        names = ", ".join(str(g.get("symbol", "?")) for g in uncovered[:6])
        lines.append(
            f"🛑🛑🛑 NO STOP AT ALL: {len(uncovered)} position(s) with nothing "
            f"protecting them — {names}"
        )
    if partial:
        names = ", ".join(str(g.get("symbol", "?")) for g in partial[:6])
        lines.append(
            f"⚠️ STOP MIS-SIZED: {len(partial)} position(s) only partly "
            f"protected — {names}"
        )
    abnormal_fractional = _evening_fractional_line(result)
    if abnormal_fractional:
        lines.append(abnormal_fractional)

    analysis = result.get("analysis")
    if not analysis:
        lines.append(
            "🛑 The evening review did not complete, so there is no read on "
            "today or on tomorrow. The figures below are the broker's."
        )
    risk = _attr_or_key(analysis, "risk_rating")
    if isinstance(risk, str) and risk.lower() in ("elevated", "high"):
        lines.append(f"🚨 NEEDS YOUR ATTENTION — the desk graded today's risk {risk}")

    # A deterministic banner used to sit here, raised when the day's loss
    # reached 80% of the account-level loss limit. That
    # breaker was removed 2026-09-20 on the owner's instruction (retired
    # item 32, docs/INCIDENT_HISTORY.md).


def _append_evening_positions(lines: list[str], result: dict, profiles: dict) -> None:
    """The book, under a heading that looks like one (owner review item 8:
    the old "Positions: 9 invested $10,650" line was a section title that
    did not read as one). Winners and underwater rows are unchanged in
    content — he asked for those to be left alone — other than naming the
    company alongside the ticker, per the standing wording rule."""
    rows = [r for r in (result.get("_positions") or []) if isinstance(r, dict)]
    if not rows:
        return
    parked = sum(
        (_number(r.get("market_value")) or 0.0) for r in rows
        if str(r.get("symbol", "")).upper() in _SWEEP_SYMBOLS
    )
    rows = [r for r in rows if str(r.get("symbol", "")).upper() not in _SWEEP_SYMBOLS]
    if not rows:
        return
    invested = sum((_number(r.get("market_value")) or 0.0) for r in rows)
    total_value = _number(result.get("equity_close")) or _number(result.get("total_value"))
    lines.append(_b(f"POSITIONS ({len(rows)})"))
    summary = f"   ${invested:,.0f} invested"
    if total_value and total_value > 0:
        cash_pct = max(0.0, (total_value - invested) / total_value * 100)
        summary += f"  ·  {100 - cash_pct:.0f}% deployed, {cash_pct:.0f}% cash"
    if parked > 0:
        summary += f"  ·  ${parked:,.0f} parked in T-bills"
    lines.append(summary)

    def _row_line(row: dict) -> str:
        pnl = _number(row.get("unrealized_pnl")) or 0.0
        entry = _number(row.get("avg_entry"))
        price = _number(row.get("current_price"))
        pct = ((price / entry - 1) * 100) if (entry and price) else 0.0
        sign = "+" if pnl >= 0 else "−"
        name = _ticker_co(str(row.get("symbol", "?")), profiles)
        return f"   {name}  {sign}${abs(pnl):,.0f}  ({pct:+.1f}%)"

    ranked = sorted(
        (r for r in rows if _number(r.get("unrealized_pnl")) is not None),
        key=lambda r: _number(r.get("unrealized_pnl")) or 0.0,
        reverse=True,
    )
    winners = [r for r in ranked if (_number(r.get("unrealized_pnl")) or 0) > 0][:3]
    losers = [r for r in ranked if (_number(r.get("unrealized_pnl")) or 0) < 0][-3:][::-1]
    if winners:
        lines.append("📈 Top winners:")
        lines.extend(_row_line(r) for r in winners)
    if losers:
        lines.append("📉 Underwater:")
        lines.extend(_row_line(r) for r in losers)


def _append_evening_watchlist(lines: list[str], result: dict, profiles: dict) -> None:
    """Two things the desk knew at the end of the day and never said: which
    holdings sit within one ordinary day's move of their stop, and which
    report earnings imminently (owner question, 2026-09-17: "is there
    anything else that should be added?").

    Neither uses a threshold anyone picked. "Close to its stop" is measured
    against that symbol's own ATR(14) in `TradingPipeline._evening_stop_
    proximity`; "reports soon" reuses `EARNINGS_EVENT_WINDOW_SESSIONS`, the
    window every research seat already treats as imminent, and a date that
    could not be fetched is reported as not checked rather than as nothing
    due.
    """
    near = [r for r in (result.get("stop_proximity") or [])
            if isinstance(r, dict) and r.get("status") == "near"]
    through = [r for r in (result.get("stop_proximity") or [])
               if isinstance(r, dict) and r.get("status") == "through"]
    unknown_stop = [r for r in (result.get("stop_proximity") or [])
                    if isinstance(r, dict) and r.get("status") == "unknown"]
    earnings = [r for r in (result.get("earnings_proximity") or []) if isinstance(r, dict)]
    from src.data.event_calendar import EARNINGS_EVENT_WINDOW_SESSIONS
    soon = [
        r for r in earnings
        if isinstance(r.get("sessions_away"), int)
        and r["sessions_away"] <= EARNINGS_EVENT_WINDOW_SESSIONS
    ]
    if not near and not soon and not unknown_stop and not through:
        return
    lines.append(_b("WORTH KNOWING"))
    # Printed FIRST and separately from "near": a stop the tape has already
    # passed without the order filling is not a tight stop, it is shares
    # with nothing standing watch over them. The two used to render as the
    # same line.
    for row in sorted(
        through, key=lambda r: -(_number(r.get("through")) or 0.0),
    ):
        name = _ticker_co(str(row.get("symbol", "?")), profiles)
        stop = _number(row.get("stop"))
        past = _number(row.get("through"))
        stop_text = f" at ${stop:,.2f}" if stop else ""
        past_text = f" \u2014 price is ${past:,.2f} past it" if past else ""
        lines.append(
            f"   \U0001f534 {name}: the protective order{stop_text} fired "
            f"and did not fill{past_text}, so those shares have nothing "
            "standing watch over them"
        )
    for row in sorted(near, key=lambda r: _number(r.get("gap")) or 0.0):
        name = _ticker_co(str(row.get("symbol", "?")), profiles)
        stop = _number(row.get("stop"))
        stop_text = f" (stop ${stop:,.2f})" if stop else ""
        lines.append(
            f"   🎯 {name} is inside one ordinary day's move of its "
            f"stop{stop_text}"
        )
    if unknown_stop:
        names = ", ".join(
            _ticker_co(str(r.get("symbol", "?")), profiles) for r in unknown_stop[:6]
        )
        lines.append(f"   ❔ Could not check the stop distance on: {names}")
    for row in sorted(soon, key=lambda r: r.get("sessions_away", 99)):
        name = _ticker_co(str(row.get("symbol", "?")), profiles)
        sessions = row["sessions_away"]
        when = "tomorrow" if sessions <= 1 else f"in about {sessions} trading days"
        lines.append(f"   📅 {name} reports earnings {when}")


_RISK_SCALE = ("low", "moderate", "elevated", "high")


def _risk_with_scale(risk: Any) -> str:
    """'moderate — step 2 of 4 (low · moderate · elevated · high)', or the
    word alone when it is not on the desk's own four-step scale."""
    text = str(risk or "").strip()
    if text.lower() in _RISK_SCALE:
        step = _RISK_SCALE.index(text.lower()) + 1
        return f"{text} — step {step} of {len(_RISK_SCALE)} ({' · '.join(_RISK_SCALE)})"
    return text


def _append_evening_tomorrow(lines: list[str], result: dict) -> None:
    """Tomorrow, with a scale and a consequence attached (owner review item
    9: "moderate" with no scale and "bullish" with no consequence both say
    nothing).

    The scale is the model's own enum (`models.EveningAnalysis.risk_rating`
    is `Literal['low','moderate','elevated','high']`) — printed rather than
    described, so the reader can see where tonight sits in it. The
    consequence is what the code actually does with the figure: tomorrow
    morning's Portfolio Manager and the position reviewer are both handed
    this bias and conviction as context for their decisions
    (`src/agents/portfolio_manager.py`, `src/agents/position_reviewer.py`).
    """
    analysis = result.get("analysis")
    risk = _attr_or_key(analysis, "risk_rating")
    bias = _attr_or_key(analysis, "tomorrow_bias")
    conviction = _attr_or_key(analysis, "tomorrow_conviction")
    outlook = _attr_or_key(analysis, "tomorrow_outlook") or ""
    if not (risk or bias or outlook):
        return
    lines.append(_b("TOMORROW"))
    if isinstance(risk, str) and risk.lower() in _RISK_SCALE:
        step = _RISK_SCALE.index(risk.lower()) + 1
        lines.append(
            f"   Risk: {risk} — step {step} of {len(_RISK_SCALE)} "
            f"({' · '.join(_RISK_SCALE)})"
        )
    elif risk:
        lines.append(f"   Risk: {risk}")
    if bias:
        confidence = f", {conviction} confidence" if conviction else ""
        lines.append(
            f"   Leaning {bias}{confidence} — tomorrow morning's decisions "
            f"start from this"
        )
    if outlook:
        lines.append(f"   {_clip(outlook, 400)}")


def _evening_cost_line(snap: dict) -> str:
    """The AI spend for this session, in words.

    Owner review: "$0.0000" read as broken. It is not — it is true. Every
    seat the evening session runs (evening_analyst and news_analyst_evening)
    is on `gemini-3.5-flash-lite`, which `src/cost_table.py` pins at $0.00
    as a deliberate free-tier price, not as an unpriced placeholder; the one
    expensive seat, the portfolio manager, does not run in the evening at
    all. So the honest rendering is a word, not four decimal places.
    """
    # One implementation of this wording, in src/notifier.py, so the evening
    # message and every other owner-facing message cannot drift apart. The
    # words below are unchanged from the version the owner signed off.
    return describe_ai_cost(
        snap.get("cost"),
        label="AI cost tonight",
        free_note="the evening review runs on free models",
    )


def _evening_meta_line(auto_meta: Any) -> str | None:
    """The once-a-quarter self-review, in one line, or nothing.

    `run_evening` attaches `auto_meta` only on the last trading day of a
    quarter; `status='skipped'` on every other evening. The base formatter
    rendered five variants of this — kept, condensed, and moved into
    DETAILS, because on the 99% of nights it is absent nothing changes and
    on the night it is present the owner still needs to be told to look.
    """
    if not isinstance(auto_meta, dict):
        return None
    status = str(auto_meta.get("status", ""))
    period = auto_meta.get("period", "this quarter")
    if status == "skipped":
        return None
    if status == "auto_meta_error":
        return f"Quarterly self-review ({period}) failed — check the logs."
    if status == "digest_only":
        return (
            f"Quarterly self-review ({period}): the write-up was saved but the "
            f"review itself failed — check the logs."
        )
    report = auto_meta.get("editor_report") or {}
    applied = len(report.get("applied") or [])
    rejected = report.get("rejected") or []
    staged = sum(
        1 for row in rejected
        if isinstance(row, dict) and "dry_run" in str(row.get("reason", ""))
    )
    if applied:
        return (
            f"Quarterly self-review ({period}): {applied} change(s) applied, "
            f"{len(rejected)} rejected."
        )
    if staged:
        return (
            f"Quarterly self-review ({period}): {staged} proposed change(s) are "
            f"staged for you to approve."
        )
    if rejected:
        return f"Quarterly self-review ({period}): nothing applied, {len(rejected)} rejected."
    proposed = int(auto_meta.get("proposed_learnings_count") or 0)
    if proposed:
        return (
            f"Quarterly self-review ({period}): {proposed} proposal(s) generated "
            f"but the report is missing — check the logs."
        )
    return None


def _format_evening(result: dict, elapsed: float) -> str:
    status = str(result.get("status", "unknown"))
    analysis = result.get("analysis")
    run_id = result.get("run_id")
    snap = _read_run(run_id)
    # The base formatter read the positions table itself; reuse the
    # trader-feed snapshot read instead so there is one DB path, not two.
    #
    # A caller may supply `_positions` already — that is how a stored
    # report is re-rendered (`render_stored_evening`). `_read_run`'s
    # positions query is deliberately NOT run-scoped (it reads the book as
    # it stands right now), so replaying an older night through it would
    # print today's holdings under that night's date. A supplied snapshot
    # therefore wins; the live evening path never sets one and is
    # unaffected.
    result = dict(result)
    if not isinstance(result.get("_positions"), list):
        result["_positions"] = snap.get("positions") or []
    profiles = _lookup_company_profiles(
        _all_symbols(
            result["_positions"],
            result.get("stop_proximity") or [],
            result.get("earnings_proximity") or [],
        )
    )

    outcome = _evening_outcome(status, analysis)
    lines = [f"🌙 EVENING · {fmt_time_12h(et_now())} · {outcome}"]

    # P&L FIRST, directly under the heading — owner, 2026-09-18. It sat
    # below the evening banners, which is the same drift he is correcting
    # everywhere else in this pass: a banner is about the desk, the P&L is
    # about his money.
    _new_section(lines, *_evening_pnl_lines(result))
    _new_block(lines, _append_evening_banners, result)
    _new_block(lines, _append_evening_positions, result, profiles)
    _new_block(lines, _append_evening_watchlist, result, profiles)
    _new_block(lines, _append_evening_tomorrow, result)

    detail_lines: list[str] = []

    def _render_details(detail: list[str]) -> None:
        summary = _attr_or_key(analysis, "daily_summary")
        if summary:
            detail.append(_clip(summary, 700))
        risk_capital = _number(result.get("risk_capital_dollars"))
        pnl = _number(result.get("pnl_4pm"))
        if pnl is None:
            pnl = _number(result.get("daily_pnl"))
        if risk_capital is not None and risk_capital > 0 and pnl is not None:
            detail.append(
                f"Measured against the ${risk_capital:,.2f} actually at risk "
                f"today, that is {pnl / risk_capital * 100:+.2f}%."
            )
        key_risks = _attr_or_key(analysis, "tomorrow_key_risks")
        if isinstance(key_risks, list) and key_risks:
            named = "; ".join(_clip(r, 120) for r in key_risks[:3] if r)
            if named:
                detail.append(f"Watching tomorrow: {named}")
        actions = _attr_or_key(analysis, "suggested_actions")
        if isinstance(actions, list) and actions:
            detail.append("Suggested by the desk:")
            for action in actions[:5]:
                if isinstance(action, str) and action.strip():
                    detail.append(f"   • {_clip(action, 400)}")
        meta = _evening_meta_line(result.get("auto_meta"))
        if meta:
            detail.append(meta)
        detail.append(_evening_cost_line(snap))

    _new_block(detail_lines, _render_details)
    _wrap_details(lines, detail_lines)

    _new_section(lines, f"🧾 {_fmt_elapsed(elapsed)}")
    return "\n".join(lines)


# === Pre-market earnings pass (2026-09-18) ===
#
# The owner's own words on the old message ("analyzed: 1 confirmed: 1
# failed: 0"): "which one? what's the symbol? what's the company?". The
# run now records each filing it handled (`result["filings"]`, see
# `TradingPipeline.run_earnings_preprocess`) and this names every one:
# ticker, company, which report, when it was filed, what the reader
# concluded, and what that changes for him. A result that predates the
# field says plainly that the companies were not recorded.

_FORM_WORDS: dict[str, str] = {
    "10-Q": "quarterly report (10-Q)",
    "10-K": "annual report (10-K)",
}


def _form_words(form_type: Any) -> str:
    text = str(form_type or "").strip()
    return _FORM_WORDS.get(text.upper(), text or "filing")


def _earnings_filing_line(row: dict, profiles: dict) -> str:
    name = _ticker_co(str(row.get("symbol", "?")), profiles)
    filed = row.get("filing_date") or "filing date not recorded"
    return f"{name} — {_form_words(row.get('form_type'))} filed {filed}"


def _format_earnings(result: dict, elapsed: float) -> str:
    status = str(result.get("status", "unknown"))
    filings = [f for f in (result.get("filings") or []) if isinstance(f, dict)]
    read = [f for f in filings if str(f.get("outcome", "")) == "analyzed"]
    failed = [f for f in filings if str(f.get("outcome", "")) != "analyzed"]
    profiles = _profiles(filings)

    if status == "analysis_error":
        outcome = "FAILED"
    elif read and failed:
        outcome = "PARTLY READ"
    elif read:
        outcome = f"{len(read)} FILING{'S' if len(read) != 1 else ''} READ"
    elif failed:
        outcome = "NOTHING READ"
    else:
        outcome = "RAN"
    lines = [f"📄 PRE-MARKET EARNINGS · {fmt_time_12h(et_now())} · {outcome}"]

    # P&L FIRST, directly under the heading — owner, 2026-09-18: EVERY
    # message, this one included. The pre-market filing reader runs before
    # the open and does no account read, so both figures are genuinely
    # unknown here; `_pnl_section_lines` renders that as "not available"
    # plus one sentence saying why, rather than dropping the block or
    # inventing a zero.
    _new_section(lines, *_pnl_section_lines(result))

    if status == "analysis_error":
        banner = [
            "🛑 The earnings reader stopped with a fault before it finished, "
            "so no filing below was read this morning. Nothing was bought or "
            "sold because of it. The desk tries again at its next pre-market "
            "pass; until a filing is read, the Portfolio Manager treats it as "
            "unread and caps any new buy of that company.",
        ]
        if result.get("error"):
            banner.append(_machine_detail(result.get("error")))
        _new_section(lines, *banner)
        if filings:
            waiting = [_b("WAITING TO BE READ")]
            waiting += [f"   • {_earnings_filing_line(f, profiles)}" for f in filings]
            _new_section(lines, *waiting)
        else:
            _new_section(
                lines,
                "The desk did not record which companies' filings were waiting.",
            )
        _new_section(lines, f"🧾 {_fmt_elapsed(elapsed)}")
        return "\n".join(lines)

    if not filings:
        # A result from before the run recorded its filings: say so, and
        # keep the only figures the run did record, unrounded.
        _new_section(
            lines,
            "The desk did not record which companies these were. What it "
            f"did record: {result.get('analyzed', 'not recorded')} read, "
            f"{result.get('confirmed', 'not recorded')} filed as read, "
            f"{result.get('failed', 'not recorded')} failed.",
        )
        _new_section(lines, f"🧾 {_fmt_elapsed(elapsed)}")
        return "\n".join(lines)

    if read:
        block = [_b("READ AND FILED")]
        for row in read:
            verdict_bits = [
                str(v) for v in (row.get("sentiment"), row.get("conviction")) if v
            ]
            verdict = (
                f"{verdict_bits[0]}, {verdict_bits[1]} conviction"
                if len(verdict_bits) == 2 else
                (verdict_bits[0] if verdict_bits else "the reader recorded no verdict")
            )
            block.append(f"   • {_earnings_filing_line(row, profiles)}: {verdict}")
            thesis = _clip(row.get("key_thesis"), 420)
            if thesis:
                block.append(f"      {thesis}")
        block.append(
            "   What changes: from the next session on, the Portfolio Manager "
            "and the position reviewer read this verdict when they look at "
            "the company. Nothing was bought or sold on it now."
        )
        _new_section(lines, *block)

    if failed:
        block = [_b("COULD NOT BE READ")]
        for row in failed:
            block.append(
                f"   • {_earnings_filing_line(row, profiles)}: the reader did "
                "not produce a usable analysis"
            )
        block.append(
            "   What it means: nothing was traded on these. The desk tries "
            "again at its next pre-market pass, and gives up on a filing after "
            "repeated failures — this message does not record which try this "
            "was. Until it is read, the Portfolio Manager treats the filing as "
            "unread and caps any new buy of that company."
        )
        _new_section(lines, *block)

    _new_section(lines, f"🧾 {_fmt_elapsed(elapsed)}")
    return "\n".join(lines)


def _format_intraday(outer: dict, nested: dict, elapsed: float) -> str:
    run_id = nested.get("run_id") or outer.get("run_id")
    snap = _read_run(run_id)
    status = str(nested.get("status", "unknown"))

    candidates = [str(symbol).upper() for symbol in (nested.get("candidates") or []) if symbol]
    done_rows = _done_rows(snap)
    blocked_rows = _blocked_rows(nested, snap)
    acted = {row["symbol"] for row in done_rows} | {row["symbol"] for row in blocked_rows}
    looked_at_rows = _looked_at_rows(snap, candidates, acted)
    profiles = _profiles(done_rows, blocked_rows, looked_at_rows)

    outcome = _outcome_word(
        status, len(done_rows), len(blocked_rows), done_rows,
        fault_count=_fault_count(blocked_rows),
    )
    lines = [f"⚡ INTRADAY OPPORTUNITY · {fmt_time_12h(et_now())} · {outcome}"]

    def _render_status_banner(lines: list[str]) -> None:
        if status == "paid_analysis_suspended":
            lines.append(
                "🛑 SUSPENDED: paid opportunity discovery is suspended by the "
                "cost circuit; the deterministic intraday loss check completed "
                "normally."
            )
            if nested.get("error"):
                lines.append(_machine_detail(nested.get("error")))
        elif status == "intraday_analysis_error":
            # Board item 89 defect 5: `failure_status` is an internal token
            # ("pm_output_unparseable") and was printed in the middle of the
            # plain sentence. The sentence says what happened; the token
            # itself carries nothing the owner can act on and is dropped.
            lines.append(
                "🛑 FAILED: the Portfolio Manager's analysis did not "
                "complete, so nothing was decided. This was not a "
                "deliberate no-trade decision."
            )
            if nested.get("error") or nested.get("failure_status"):
                lines.append(_machine_detail(
                    " ".join(
                        str(part) for part in
                        (nested.get("failure_status"), nested.get("error"))
                        if part
                    )
                ))
        elif status == "evidence_gate_skip":
            # Was: one prose sentence, then `nested["reason"]` printed raw
            # underneath it — so the owner read the same explanation twice,
            # once in English and once in machine ("1 seat(s) ... "
            # "smart_money=expired ... docs/WORK.md item 20"). The stored
            # reason is unchanged; it simply no longer renders. Title line
            # plus bullets, shared with the standalone alert via
            # `describe_skipped_decision`.
            skip_lines = describe_skipped_decision(
                nested.get("lost_seats"), nested.get("data_status"),
                # This IS the tick's own message; "the next scheduled
                # decision tries again" belongs on the standalone alert,
                # not repeated inside every tick.
                include_next_pass=False,
            )
            lines.append(f"🟡 {skip_lines[0]}")
            lines.extend(skip_lines[1:])
        elif status == "intraday_scan_crashed":
            # Operator-honesty fix: this used to be indistinguishable from a
            # healthy tick that ran and found nothing — the scan raised, the
            # caller swallowed the exception and set scan_result to None, and no
            # `intraday_scan` key ever reached this formatter. Now the crash
            # attaches a dict with this status, so it renders through the same
            # nested path `paid_analysis_suspended` / `intraday_analysis_error`
            # already use, instead of silently reading as "Status: ok".
            lines.append(
                "🛑 CRASHED: the search for intraday opportunities stopped "
                "with a fault, so it found nothing. The automatic loss "
                "check above ran normally."
            )
            # The exception TYPE is not thrown away — it moves out of the
            # owner's sentence and into the labelled machine line with the
            # message, where it belongs and where it stays greppable.
            if nested.get("error") or nested.get("error_type"):
                lines.append(_machine_detail(
                    " ".join(
                        str(part) for part in
                        (nested.get("error_type"), nested.get("error")) if part
                    )
                ))

    # P&L FIRST, directly under the heading — owner, 2026-09-18. It used to
    # sit BELOW the status banner here, which is exactly the drift he is
    # correcting: the banner is about this tick, the P&L is about his money.
    _new_section(lines, *_pnl_section_lines(outer))

    _new_block(lines, _render_status_banner)

    _new_block(lines, _append_done, done_rows, snap, profiles)
    _new_block(lines, _append_blocked, blocked_rows, profiles)
    # Held back and spliced in here — see `_budgeted_sections`.
    looked_at_slot = len(lines)

    detail_lines: list[str] = []
    # How much of this tick's evidence was actually read on this tick.
    # Owner mandate 2026-09-18 made every seat but the chart research
    # advisory, so an intraday decision can now stand on one fresh read plus
    # this morning's book — and every carried seat reports clean. It sits
    # inside the details block rather than the headline: it belongs to every
    # tick, and a line on every tick at the top is how a banner stops being
    # read. Disclosure only; it states a count and never judges one.
    _new_block(
        detail_lines, _append_intraday_evidence_freshness, outer, nested,
    )
    # Reasoning first, the per-candidate enumeration last — same ordering
    # and same reason as `_format_decision_session`.
    _new_block(detail_lines, _append_pm, snap, may_glue=True)
    _new_block(detail_lines, _append_risk, snap)
    # `nested`, not `outer`: on the intraday path the traded-order evidence
    # (and the run_id it's keyed by) lives in the `intraday_scan` sub-dict.
    _new_block(detail_lines, _append_gate_and_execution, nested, snap)
    _new_block(detail_lines, _append_signals, snap, candidates=candidates)
    _budgeted_sections(
        lines, looked_at_slot, looked_at_rows, profiles, snap, detail_lines,
    )

    _new_block(lines, _append_footer, snap, elapsed)
    return "\n".join(lines)


# === intra_check cadence (2026-09-17) ===
#
# Owner requirement: "I want messages every hour. I want oversight and
# transparency of what is being done." Ticks still run every 30 minutes,
# unchanged — but a tick with nothing actionable (no order, trade, skip,
# fill, stop/coverage problem, or error) sends NO message at all, same as
# before AND now including the ":30 INTRADAY OPPORTUNITY ... NO TRADE"
# message, which used to always send. Silence on a quiet half-hour is fine;
# silence for a whole clock hour is not. The top-of-hour tick (":00", plus
# the 9:30 session-open tick, which is the first tick of the day and has no
# ":00" before it) is the guaranteed pulse: it ALWAYS sends one message,
# built from DB records covering the last ~hour — including the :30 tick's
# analysis, which ran under a different run_id than this tick's own — so
# the owner never goes more than a clock hour without a message, even on a
# fully quiet stretch. Anything actionable still sends immediately at any
# tick, exactly as before; if that already happened this hour, the
# top-of-hour tick still sends its own summary, as a second message body.
_HOUR_WINDOW_MINUTES = 70  # 60 minutes + a buffer for scheduler jitter

# 2026-09-17 regression, fixed same night: PR #454 moved intra_check's own
# systemd timer off the shared `*:0/30` tick onto `*:15,45` (to stop it
# firing in the same wall-clock second as morning/midday/close/evening —
# see scripts/systemd/quant-agent-intra_check.timer for the full story),
# but `_is_hourly_checkpoint` below still hard-coded `minute == 0` as "the
# top of the hour". intra_check is the ONLY session that runs this check
# (it is the one that ticks all day, 09:30-16:00 ET) and it no longer ever
# lands on minute 0 — so the owner's guaranteed once-per-hour message
# silently stopped firing on a quiet day. Never caught because nothing
# tied the checkpoint minute to the timer's actual cadence.
#
# Fix: the "top of the hour" is not minute 0, it is whatever minute
# intra_check's OWN timer first fires on each hour — read from the unit
# file itself (scripts/systemd/quant-agent-intra_check.timer) rather than
# duplicated here as a second hard-coded number, so the next cadence move
# carries the guarantee with it instead of quietly breaking it again. See
# tests/test_trader_feed.py::test_hourly_checkpoint_minute_matches_intra_check_timer_cadence
# for the guard that fails the build if this ever falls out of sync with
# the unit file.
_INTRA_CHECK_TIMER_PATH = (
    Path(__file__).resolve().parent.parent / "scripts" / "systemd" / "quant-agent-intra_check.timer"
)

# A bare `*:MM` or `*:MM,MM,...` OnCalendar spec — the only form intra_check's
# timer has ever used and the only form this parses. Anything else (a day
# restriction, a `/` step syntax, a full calendar expression, ...) is a
# cadence change this code cannot safely interpret, and it must say so
# loudly rather than guess.
_ONCALENDAR_PER_HOUR_RE = re.compile(r"\*:(\d{1,2}(?:,\d{1,2})*)")


def _intra_check_tick_minutes() -> tuple[int, ...]:
    """The minutes-past-the-hour intra_check actually fires on, read from
    its own systemd timer unit. Raises rather than guessing if the file is
    missing or its `OnCalendar=` line isn't the simple per-hour form this
    understands — a silent fallback here is exactly the class of bug this
    function exists to prevent.
    """
    try:
        text = _INTRA_CHECK_TIMER_PATH.read_text()
    except OSError as exc:
        raise RuntimeError(
            f"cannot read intra_check's own timer unit at "
            f"{_INTRA_CHECK_TIMER_PATH} to derive the hourly-checkpoint "
            f"minute: {exc}"
        ) from exc

    spec = None
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("OnCalendar="):
            spec = line[len("OnCalendar="):].strip()
            break
    if spec is None:
        raise RuntimeError(
            f"{_INTRA_CHECK_TIMER_PATH} has no OnCalendar= line; cannot "
            "derive the hourly-checkpoint minute"
        )

    match = _ONCALENDAR_PER_HOUR_RE.fullmatch(spec)
    if not match:
        raise RuntimeError(
            f"{_INTRA_CHECK_TIMER_PATH}: OnCalendar={spec!r} is not a "
            "simple '*:MM' / '*:MM,MM' per-hour cadence — "
            "_is_hourly_checkpoint doesn't know how to read this and won't "
            "guess. Update the parsing deliberately if the cadence syntax "
            "has genuinely changed."
        )
    return tuple(sorted({int(m) for m in match.group(1).split(",")}))


def _hourly_checkpoint_minute() -> int:
    """The minute-past-the-hour that owes the clock hour its guaranteed
    message: the FIRST tick intra_check makes each hour, under whatever
    cadence its timer is actually running."""
    return _intra_check_tick_minutes()[0]


def _is_hourly_checkpoint(now=None) -> bool:
    """True at the tick that owes this trading hour its guaranteed message.

    The first tick of each hour (see `_hourly_checkpoint_minute`) owns
    that hour. The 9:30 session-open tick is the exception: it is the
    first tick of the day, so there is no earlier tick this hour to have
    covered the 9:00-9:30 gap (during which the market was closed anyway)
    — 9:30 stands in for it.
    """
    now = now or et_now()
    return now.minute == _hourly_checkpoint_minute() or (now.hour == 9 and now.minute == 30)


def _intraday_tick_actionable(result: dict, nested: dict | None, snap: dict[str, Any]) -> bool:
    """Whether THIS intra_check tick has something the owner needs to see
    now, rather than folded into the next top-of-hour summary — an order,
    a trade, an execution skip, a stop-coverage problem, or a genuine
    error/interruption in paid analysis. Read-only classification; a
    mis-read here costs at most one message's timing, never a trading
    decision.

    `skips` is read from `snap` (the same `_read_run` DB snapshot every
    other renderer here uses), not from `nested["execution_skips"]` — the
    intraday scan's own result dict never sets that key (checked against
    `_intraday_opportunity_scan_body` in src/pipeline.py: it returns only
    status/candidates/orders/run_id), so a skip would otherwise never be
    seen as actionable here.
    """
    if _actionable_coverage_gaps(result.get("stop_coverage_gaps")):
        return True
    if not isinstance(nested, dict):
        return False
    status = str(nested.get("status") or "")
    if status in (
        "intraday_executed", "intraday_analysis_error",
        "intraday_scan_crashed", "paid_analysis_suspended",
        "evidence_gate_skip",
    ):
        return True
    if status == "intraday_no_trades":
        # PM/risk reached a real decision point with nothing to execute —
        # actionable only if execution actually had something to skip
        # (insufficient cash, etc.); a clean "nothing to do" is quiet.
        return bool(snap.get("skips"))
    return False  # disabled / lock_contended / no_opportunity — quiet


def _midday_collision_tick_minute() -> int | None:
    """Minute-of-ET-day of the ONE intra_check tick that collides with the
    midday position review, or None if the two never meet.

    `midday` runs once per ET date (`scripts/run_if_et_window.sh` writes a
    last-run marker for every mode but `intra_check`), so its single report
    lands on the first scheduler tick inside `SESSION_WINDOWS["midday"]`.
    The colliding intra_check tick is therefore the first intra_check tick
    at or after that window opens — derived from the window constant and
    intra_check's own timer unit rather than restated as a third number.
    """
    lo, hi = SESSION_WINDOWS["midday"]
    ticks = _intra_check_tick_minutes()
    for hour in range(lo // 60, hi // 60 + 1):
        for minute in ticks:
            candidate = hour * 60 + minute
            if lo <= candidate <= hi:
                return candidate
    return None


def _is_midday_collision_tick(now=None) -> bool:
    """True only at that single tick, and only on a session weekday."""
    now = now or et_now()
    if not in_session_window("midday", when=now):
        return False
    collision = _midday_collision_tick_minute()
    if collision is None:
        return False
    now_et = to_et(now)
    return now_et.hour * 60 + now_et.minute == collision


def _format_intra_check(result: dict, elapsed_seconds: float) -> str | None:
    nested = result.get("intraday_scan")
    run_id = (nested.get("run_id") if isinstance(nested, dict) else None) or result.get("run_id")
    snap = _read_run(run_id)
    actionable = _intraday_tick_actionable(result, nested, snap)

    own_message: str | None = None
    if actionable:
        nested_status = str(nested.get("status") or "") if isinstance(nested, dict) else ""
        if isinstance(nested, dict) and nested_status not in _INTRADAY_SILENT_STATUSES:
            own_message = _format_intraday(result, nested, elapsed_seconds)
        else:
            # Actionable for a reason `_format_intraday` doesn't render
            # (an outer-level stop-coverage gap while the scan itself was
            # disabled/contended/found nothing) — the base formatter's own
            # `_append_intra_check_body` already renders that banner.
            own_message = _base_format_session_result(
                "intra_check", result, elapsed_seconds, error=None,
            )

    # Owner decision, 2026-09-17: the midday position review and the routine
    # half-hourly intra_check "nothing to report" ping must not fire minutes
    # apart. The ratified fix is to suppress that routine ping ONCE — at the
    # single intra_check tick that actually collides with the midday report —
    # not to silence intra_check for the whole 90-minute window.
    #
    # `_midday_collision_tick_minute()` derives that one tick from the two
    # existing authoritative sources (`SESSION_WINDOWS["midday"]` and
    # intra_check's own timer unit), so no new number is introduced and a
    # future cadence or window move carries the rule with it.
    #
    # Why only one tick and not the window: `midday` runs once per ET date,
    # not once per tick — `scripts/run_if_et_window.sh` writes a last-run
    # marker for every mode except `intra_check` and exits early for the rest
    # of the day. So the window holds exactly ONE midday report (its first
    # tick, 13:00 ET), and suppressing every quiet intra_check tick in the
    # window would instead swallow the 14:00 hour's guaranteed pulse (14:15
    # under #462's derived checkpoint minute) and leave a genuinely quiet day
    # with no message at all between 13:00 and 15:15 — breaking the owner's
    # standing "never a full clock hour without a message" requirement in
    # order to fix a two-messages-at-once complaint.
    #
    # Ordering matters: this runs BEFORE `_is_hourly_checkpoint` so that the
    # 13:00 hour's guaranteed pulse (13:15) does not re-send what the midday
    # report just said. Anything actionable (an order placed/filled/cancelled/
    # refused, a stop-coverage gap, a scan crash, `paid_analysis_suspended`,
    # a degraded-fill or coverage finding, ...) already set `own_message`
    # above and is never reached by this branch, in or out of the window.
    if own_message is None and _is_midday_collision_tick():
        return None

    if not _is_hourly_checkpoint():
        return own_message  # None here means: quiet tick, no send.

    summary = _format_hourly_desk_check(result, nested, elapsed_seconds)
    if own_message:
        return own_message + "\n\n" + summary
    return summary


def _movers_scanned_text(nested: dict | None) -> str:
    """Honest one-line account of whether the paid intraday scan even ran
    this tick — 'no arbitrary numbers': there is no mover COUNT to report
    for the common no-opportunity/disabled/contended outcomes (the
    pipeline's own status dicts don't carry one), so this says what is
    actually known instead of fabricating a figure.
    """
    if not isinstance(nested, dict):
        return "not run this tick"
    status = str(nested.get("status") or "")
    if status == "intraday_scan_no_opportunity":
        return "ran, no qualifying movers"
    if status == "intraday_scan_disabled":
        return "disabled"
    if status == "intraday_scan_lock_contended":
        return "skipped (lock contended)"
    return "ran this tick"


def _read_hour_evidence(hour_window_minutes: int = _HOUR_WINDOW_MINUTES) -> list[dict]:
    """Every tech-analyst analysis logged in roughly the last hour, read by
    TIME rather than by this tick's own `run_id` — the top-of-hour summary
    has to include the :30 tick's analysis too, and that ran under a
    different run_id. One row per symbol (most recent), so a symbol
    analyzed at both ticks (cooldown normally prevents this) is not shown
    twice. Read-only, fail-soft: any DB problem yields an empty list rather
    than blocking the guaranteed hourly message.
    """
    rows: dict[str, dict] = {}
    if not Path(_DB_PATH).exists():
        return []
    conn = None
    try:
        uri = f"file:{Path(_DB_PATH).resolve()}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=1.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=1000")
        for row in conn.execute(
            "SELECT symbol, evidence_json FROM specialist_evidence "
            "WHERE agent_name = 'tech_analyst' AND kind = 'analysis' "
            "AND timestamp >= datetime('now', ?) ORDER BY timestamp",
            (f"-{hour_window_minutes} minutes",),
        ).fetchall():
            try:
                data = json.loads(row["evidence_json"] or "{}")
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            symbol = str(data.get("symbol") or row["symbol"] or "").upper()
            if symbol:
                rows[symbol] = data
    except Exception as exc:  # noqa: BLE001
        logger.warning("hourly desk check: evidence read failed: %s", exc)
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass
    return list(rows.values())


def _read_hour_trades(hour_window_minutes: int = _HOUR_WINDOW_MINUTES) -> list[dict]:
    """Every real (non-HOLD) broker trade row in roughly the last hour —
    read-only, fail-soft: any DB problem reads as an empty list rather than
    blocking the guaranteed hourly message (a real trade still sent its own
    immediate message regardless). The rows carry symbol, action, quantity,
    price and fill state so the hourly message can NAME what it counts
    (board item 89: a count is not information)."""
    if not Path(_DB_PATH).exists():
        return []
    conn = None
    try:
        uri = f"file:{Path(_DB_PATH).resolve()}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=1.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=1000")
        rows = conn.execute(
            "SELECT symbol, action, qty, price, fill_status, fill_qty, "
            "fill_price FROM trades WHERE timestamp >= datetime('now', ?) "
            "AND UPPER(action) != 'HOLD' ORDER BY id",
            (f"-{hour_window_minutes} minutes",),
        ).fetchall()
        return [dict(row) for row in rows]
    except Exception as exc:  # noqa: BLE001
        logger.warning("hourly desk check: trade read failed: %s", exc)
        return []
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass


def _read_hour_trade_count(hour_window_minutes: int = _HOUR_WINDOW_MINUTES) -> int:
    """Count of real (non-HOLD) broker trades in roughly the last hour —
    see `_read_hour_trades`; a DB problem reads as 0."""
    return len(_read_hour_trades(hour_window_minutes))


def _format_hourly_desk_check(
    result: dict,
    nested: dict | None,
    elapsed: float,
    *,
    snap: dict[str, Any] | None = None,
    trade_count: int | None = None,
    trade_rows: list[dict] | None = None,
    period_line: str = "Covering the last hour",
    coverage_text: str | None = None,
    scanned_text: str | None = None,
) -> str:
    """The guaranteed top-of-hour message: what happened across BOTH ticks
    of the last hour, in plain English, even when neither one had anything
    that needed the owner's attention on its own.

    Every keyword argument defaults to exactly what the scheduled
    top-of-hour path has always done; they exist so the on-demand desk
    status (`format_desk_status` below, `scripts/desk_status.py`) can drive
    this same formatter from live broker truth and — crucially — say
    "not available" for the two fields an on-demand read genuinely cannot
    know, rather than borrowing a scheduled tick's answer or printing a
    reassuring zero.
    """
    run_id = result.get("run_id")
    if snap is None:
        snap = _read_run(run_id)
    if trade_count is None and trade_rows is None:
        # The scheduled path: read the hour's trades once and name them.
        trade_rows = _read_hour_trades()
    if trade_count is None:
        trade_count = len(trade_rows or [])
    trade_rows = [row for row in (trade_rows or []) if isinstance(row, dict)]
    outcome = _traded_word(trade_rows) if trade_count else "NO CHANGE"
    lines = [
        f"🕐 DESK CHECK · {fmt_time_12h(et_now())} · {outcome}",
        period_line,
    ]

    # P&L FIRST, directly under the heading — owner, 2026-09-18. It used to
    # sit below the hour's order list, four sections down.
    _new_section(lines, *_pnl_section_lines(result))

    positions = [row for row in (snap.get("positions") or []) if isinstance(row, dict)]
    risk_positions = [
        row for row in positions
        if str(row.get("symbol", "")).upper() not in _SWEEP_SYMBOLS
    ]
    hour_rows = _read_hour_evidence()
    hour_symbols = [
        str(row.get("symbol", "")).upper() for row in hour_rows if row.get("symbol")
    ]
    profiles = _profiles(hour_symbols, trade_rows, risk_positions)

    if trade_count == 0:
        _new_section(lines, "⏸️ No action this hour")
    elif trade_rows:
        # Board item 89: "N order(s) this hour" named nothing. Each order,
        # with its company and true fill state, from the same rows the
        # count came from — so the count can never exceed the list.
        trade_lines = [f"⚡ {trade_count} order(s) this hour — each already alerted on its own"]
        for row in trade_rows:
            action = str(row.get("action", "?")).upper()
            symbol = str(row.get("symbol", "?")).upper()
            qty = _number(row.get("fill_qty")) or _number(row.get("qty"))
            price = _number(row.get("fill_price")) or _number(row.get("price"))
            qty_text = f" {qty:g}" if qty is not None else ""
            price_text = f" @ ${price:,.2f}" if price is not None and price > 0 else ""
            trade_lines.append(
                f"   • {action} {_ticker_co(symbol, profiles)}{qty_text}{price_text} — "
                f"{_fill_state_plain(row.get('fill_status'))}"
            )
        _new_section(lines, *trade_lines)
    else:
        _new_section(
            lines,
            f"⚡ {trade_count} order(s) this hour — see the alert(s) already sent",
        )

    # Board item 89: "Positions held: N" named none of them. Every holding,
    # company alongside, with its own open profit or loss where recorded.
    position_lines = [f"💼 Positions held: {len(risk_positions)}"]
    for row in risk_positions:
        symbol = str(row.get("symbol", "?")).upper()
        pnl = _number(row.get("unrealized_pnl"))
        pnl_text = f" · {_fmt_signed_money(pnl)} open" if pnl is not None else ""
        position_lines.append(f"   • {_ticker_co(symbol, profiles)}{pnl_text}")
    _new_section(lines, *position_lines)

    _cov_start = len(lines)
    _append_coverage_gaps(lines, result)
    _cov_added = len(lines) > _cov_start
    _seal_section(lines, _cov_start)
    if not _cov_added:
        _new_section(lines, coverage_text or "🛡️ Stop coverage: OK")

    def _render_hour_signals(lines: list[str]) -> None:
        lines.append(f"🔎 Signals this hour: {len(hour_rows)} analyzed")
        priority = {"strong_buy": 0, "strong_sell": 0, "buy": 1, "sell": 1, "neutral": 2}
        ordered = sorted(
            hour_rows,
            key=lambda row: (
                priority.get(str(row.get("rating", "")).lower(), 3),
                str(row.get("symbol", "")),
            ),
        )
        # Uncapped, same reasoning as `_signal_rows`: the header just above
        # states the real count — the bullets below it must match, not
        # silently truncate to a smaller number. Company name inline
        # (`profiles`) since this is the only per-symbol listing in this
        # message — there is no separate DONE/LOOKED-AT section to have
        # already introduced it.
        for row in ordered:
            lines.append(_signal_row_line(row, profiles=profiles))

    detail_lines: list[str] = []
    if hour_rows:
        _new_block(detail_lines, _render_hour_signals)
    else:
        _new_section(
            detail_lines,
            f"🔎 Movers scanned: {scanned_text or _movers_scanned_text(nested)}",
        )
    _wrap_details(lines, detail_lines)

    _new_block(lines, _append_footer, snap, elapsed)
    return "\n".join(lines)


# === on-demand desk status (2026-09-17) ===
#
# Owner request: "send me the desk status now", at any time, without
# running a trading session. This deliberately holds NO message format of
# its own — it assembles read-only inputs and hands them to
# `_format_hourly_desk_check` above, the same function the scheduled
# top-of-hour message uses. Every future change to that formatter
# (wording, P&L line, sections) reaches this command with no edit here.
#
# Read-only, in the strong sense: it places and cancels nothing, calls no
# model, writes nothing to the database, and takes no session lock or
# once-per-day stamp. The scheduled sessions are unaffected by it.
_ON_DEMAND_PERIOD_LINE = "Covering the last hour · status requested on demand"
# Stop coverage is audited (and auto-repaired) by the scheduled sessions,
# which is a WRITING action — this command must not run it. So the answer
# is genuinely unknown here and says so, rather than printing the
# reassuring "OK" that the scheduled path prints when it has actually
# looked.
_ON_DEMAND_COVERAGE_TEXT = (
    "🛡️ Stop coverage: not available — the coverage audit runs with the "
    "scheduled sessions, not on demand"
)
_ON_DEMAND_SCANNED_TEXT = (
    "not available — the movers scan runs with the scheduled sessions, "
    "not on demand"
)


def _position_rows_from_broker(positions: Any) -> list[dict[str, Any]]:
    """Broker `Position` objects (or plain dicts) → the same row shape
    `_read_run` puts in `snapshot["positions"]`, so the shared formatter
    cannot tell the difference. Shorts (negative qty) are kept, matching
    that query's `qty != 0`."""
    rows: list[dict[str, Any]] = []
    for position in positions or []:
        def _get(field: str) -> Any:
            if isinstance(position, dict):
                return position.get(field)
            return getattr(position, field, None)

        qty = _number(_get("qty"))
        if qty is None or qty == 0:
            continue
        rows.append(
            {
                "symbol": str(_get("symbol") or "").upper(),
                "qty": qty,
                "avg_entry": _number(_get("avg_entry")),
                "current_price": _number(_get("current_price")),
                "market_value": _number(_get("market_value")),
                "unrealized_pnl": _number(_get("unrealized_pnl")),
            }
        )
    rows.sort(key=lambda row: abs(row.get("market_value") or 0.0), reverse=True)
    return rows


def format_desk_status(
    account: dict[str, Any],
    positions: Any,
    elapsed_seconds: float = 0.0,
    *,
    trade_count: int | None = None,
) -> str:
    """One desk-status message from live broker truth, rendered by the
    shared top-of-hour formatter.

    `account` is `AlpacaBroker.get_account()`'s dict and `positions` is
    `AlpacaBroker.get_positions()`'s list — both read-only broker calls.
    Raises nothing of its own; the caller decides what a broker failure
    means (see `scripts/desk_status.py`, which refuses to send at all
    rather than send a message with a fabricated P&L).
    """
    total_value = _number(account.get("portfolio_value"))
    last_equity = _number(account.get("last_equity"))
    daily_pnl = None
    daily_return_pct = None
    if total_value is not None and last_equity is not None:
        daily_pnl = total_value - last_equity
        if last_equity > 0:
            daily_return_pct = daily_pnl / last_equity * 100

    result = {
        "status": "ok",
        "daily_pnl": daily_pnl,
        "daily_return_pct": daily_return_pct,
        # No coverage audit was run, so there is no gap list — NOT an
        # empty list meaning "no gaps found". `coverage_text` below is what
        # the reader actually sees.
        "run_id": None,
    }
    snap = _empty_snapshot()
    snap["positions"] = _position_rows_from_broker(positions)
    return _format_hourly_desk_check(
        result,
        None,
        elapsed_seconds,
        snap=snap,
        trade_count=trade_count,
        period_line=_ON_DEMAND_PERIOD_LINE,
        coverage_text=_ON_DEMAND_COVERAGE_TEXT,
        scanned_text=_ON_DEMAND_SCANNED_TEXT,
    )


# === stored evening report, re-rendered (2026-09-18) ===
#
# The evening run's own output is now written to `evening_reports` (see
# `Database.save_evening_report`). This renders one of those rows back
# through `format_session_result`, the SAME entry point the live evening
# push uses, so the owner can read last night's report — or be shown what
# the report looks like — without running any part of the pipeline,
# without a broker call and without a model call.
#
# It holds no message format of its own, exactly like `format_desk_status`
# above: every future change to `_format_evening` reaches this with no
# edit here.
#
# The one thing it must add is honesty about gaps. A stored row can be
# partial (a run that died early, a storage failure, a row written before
# a field existed), and several of the evening formatter's inputs render as
# SILENCE when absent — silence that reads as "nothing was wrong". So the
# pieces whose absence would otherwise be indistinguishable from a clean
# result are named in plain words at the bottom. Nothing is ever defaulted,
# zeroed, estimated or back-filled.
_STORED_EVENING_GAP_WORDS: tuple[tuple[str, str], ...] = (
    ("stop_coverage_gaps", "the stop-coverage audit result"),
    ("stop_proximity", "which holdings were sitting near their stops"),
    ("earnings_proximity", "which holdings had earnings due"),
)


def read_stored_evening(
    date: str | None = None, db_path: Any = None,
) -> dict[str, Any] | None:
    """One `evening_reports` row, through a READ-ONLY connection.

    `date` is a trading day key; omit it for the most recent stored night.
    Mirrors `Database.get_evening_report`'s contract and return shape, but
    opens the file `mode=ro` the way `_read_run` does, so the on-demand
    command cannot write to (or create anything in) the live database —
    including the table itself, which is why a not-yet-migrated database
    returns None here rather than being altered.

    Returns None when there is no row, no table, no database file, or the
    stored JSON is unreadable. An unreadable row is an ABSENT report: the
    caller says so and renders nothing.
    """
    path = Path(db_path) if db_path is not None else Path(_DB_PATH)
    if not path.exists():
        return None
    conn = None
    try:
        conn = sqlite3.connect(
            f"file:{path.resolve()}?mode=ro", uri=True, timeout=1.0,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=1000")
        if date:
            row = conn.execute(
                "SELECT * FROM evening_reports WHERE date = ?", (date,),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT * FROM evening_reports ORDER BY date DESC LIMIT 1"
            ).fetchone()
    except sqlite3.DatabaseError as exc:
        logger.warning("stored evening report read failed: %s", exc)
        return None
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass
    if not row:
        return None
    record = dict(row)
    try:
        payload = json.loads(record.get("payload_json") or "")
    except (TypeError, ValueError):
        logger.error(
            "stored evening report for %s has unreadable payload",
            record.get("date"),
        )
        return None
    if not isinstance(payload, dict):
        return None
    positions = None
    if record.get("positions_json"):
        try:
            parsed = json.loads(record["positions_json"])
            positions = parsed if isinstance(parsed, list) else None
        except (TypeError, ValueError):
            positions = None
    return {
        "date": record.get("date"),
        "run_id": record.get("run_id"),
        "timestamp": record.get("timestamp"),
        "payload": payload,
        "positions": positions,
    }


def render_stored_evening(record: dict, elapsed_seconds: float = 0.0) -> str:
    """One stored evening report as a Telegram message.

    `record` is a `Database.get_evening_report` row. Raises ValueError if
    it carries no usable payload — the caller must then say the report is
    unavailable rather than render anything.
    """
    payload = record.get("payload") if isinstance(record, dict) else None
    if not isinstance(payload, dict):
        raise ValueError("stored evening report has no usable payload")

    result = dict(payload)
    gaps: list[str] = []

    # The book must come from the stored snapshot: `_read_run`'s positions
    # query reads the CURRENT table, so letting it answer would print
    # today's holdings under that night's date.
    positions = record.get("positions")
    if isinstance(positions, list):
        result["_positions"] = positions
    else:
        result["_positions"] = []
        gaps.append("the book as it stood that night")

    for key, words in _STORED_EVENING_GAP_WORDS:
        if payload.get(key) is None:
            gaps.append(words)

    body = format_session_result("evening", result, elapsed_seconds)
    if not body:
        raise ValueError("stored evening report could not be rendered")

    date_text = record.get("date") or "date not recorded"
    run_id = record.get("run_id")
    header = [
        _b(f"STORED EVENING REPORT · {date_text}"),
        "   Read back from the stored record. Nothing was run to produce "
        "this: no broker call, no model call, no order.",
    ]
    # No run identifier (owner review, 2026-09-18): it means nothing to him
    # and he does not need it. The date above already says which session
    # this was.
    lines = [*header, "", body]

    if gaps:
        if len(gaps) == 1:
            named = gaps[0]
        else:
            named = ", ".join(gaps[:-1]) + " and " + gaps[-1]
        lines += [
            "",
            _b("NOT AVAILABLE"),
            f"   The stored record does not contain {named}. That is "
            f"missing information, not an empty result — do not read the "
            f"silence above as an all-clear.",
        ]
    return "\n".join(lines)


# === stored morning / midday / close reports (2026-09-18 gap sweep) ===
#
# Same rationale and shape as the evening pair above: `run_morning` and
# `run_position_review` compute `leverage` (the §11.2 gross-ceiling
# snapshot) and `stop_coverage_gaps` (the broker-truth stop audit) from
# live state and hand them to the notifier with no other durable home.
# `Database.save_session_report` now keeps them, keyed by (date, mode).

_STORED_SESSION_GAP_WORDS: tuple[tuple[str, str], ...] = (
    ("stop_coverage_gaps", "the stop-coverage audit result"),
    ("leverage", "the gross-exposure ceiling snapshot"),
)

_STORED_SESSION_LABELS = {
    "morning": "MORNING",
    "midday": "MIDDAY REVIEW",
    "close": "CLOSE REVIEW",
}


def read_stored_session_report(
    mode: str, date: str | None = None, db_path: Any = None,
) -> dict[str, Any] | None:
    """One `session_reports` row for `mode` ('morning', 'midday', 'close'),
    through a read-only connection. Mirrors `read_stored_evening`'s
    contract exactly — see its docstring for why an unreadable row is
    treated as absent and why this opens the file `mode=ro`.
    """
    path = Path(db_path) if db_path is not None else Path(_DB_PATH)
    if not path.exists():
        return None
    conn = None
    try:
        conn = sqlite3.connect(
            f"file:{path.resolve()}?mode=ro", uri=True, timeout=1.0,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=1000")
        if date:
            row = conn.execute(
                "SELECT * FROM session_reports WHERE date = ? AND mode = ?",
                (date, mode),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT * FROM session_reports WHERE mode = ? "
                "ORDER BY date DESC LIMIT 1", (mode,),
            ).fetchone()
    except sqlite3.DatabaseError as exc:
        logger.warning("stored %s report read failed: %s", mode, exc)
        return None
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass
    if not row:
        return None
    record = dict(row)
    try:
        payload = json.loads(record.get("payload_json") or "")
    except (TypeError, ValueError):
        logger.error(
            "stored %s report for %s has unreadable payload",
            mode, record.get("date"),
        )
        return None
    if not isinstance(payload, dict):
        return None
    positions = None
    if record.get("positions_json"):
        try:
            parsed = json.loads(record["positions_json"])
            positions = parsed if isinstance(parsed, list) else None
        except (TypeError, ValueError):
            positions = None
    return {
        "date": record.get("date"),
        "mode": record.get("mode") or mode,
        "run_id": record.get("run_id"),
        "timestamp": record.get("timestamp"),
        "payload": payload,
        "positions": positions,
    }


def render_stored_session_report(
    mode: str, record: dict, elapsed_seconds: float = 0.0,
) -> str:
    """One stored morning/midday/close report as a Telegram message.

    `record` is a `read_stored_session_report`/`Database.get_session_report`
    row. Raises ValueError if it carries no usable payload.
    """
    if mode not in ("morning", "midday", "close"):
        raise ValueError(f"render_stored_session_report: unknown mode {mode!r}")
    payload = record.get("payload") if isinstance(record, dict) else None
    if not isinstance(payload, dict):
        raise ValueError(f"stored {mode} report has no usable payload")

    result = dict(payload)
    gaps: list[str] = []

    # The book must come from the stored snapshot — see the identical
    # reasoning in `render_stored_evening`.
    positions = record.get("positions")
    if isinstance(positions, list):
        result["_positions"] = positions
    else:
        result["_positions"] = []
        gaps.append("the book as it stood at the end of that session")

    for key, words in _STORED_SESSION_GAP_WORDS:
        if payload.get(key) is None:
            gaps.append(words)

    body = format_session_result(mode, result, elapsed_seconds)
    if not body:
        raise ValueError(f"stored {mode} report could not be rendered")

    date_text = record.get("date") or "date not recorded"
    run_id = record.get("run_id")
    label = _STORED_SESSION_LABELS.get(mode, mode.upper())
    header = [
        _b(f"STORED {label} REPORT · {date_text}"),
        "   Read back from the stored record. Nothing was run to produce "
        "this: no broker call, no model call, no order.",
    ]
    # No run identifier (owner review, 2026-09-18): it means nothing to him
    # and he does not need it. The date above already says which session
    # this was.
    lines = [*header, "", body]

    if gaps:
        if len(gaps) == 1:
            named = gaps[0]
        else:
            named = ", ".join(gaps[:-1]) + " and " + gaps[-1]
        lines += [
            "",
            _b("NOT AVAILABLE"),
            f"   The stored record does not contain {named}. That is "
            f"missing information, not an empty result — do not read the "
            f"silence above as an all-clear.",
        ]
    return "\n".join(lines)


# === stored intra_check ticks (2026-09-18 gap sweep) ===
#
# intra_check fires roughly every 30 minutes, not once/day, so there is
# no single "the" report for a date the way evening/morning/midday/close
# have one. `Database.save_intra_check_report` keeps every tick, keyed by
# run_id.


def read_stored_intra_check(
    run_id: str | None = None, date: str | None = None, db_path: Any = None,
) -> dict[str, Any] | None:
    """One `intra_check_reports` row, through a read-only connection.

    `run_id` selects a specific tick. Otherwise `date` (or, absent that,
    the most recent row overall) returns the latest tick recorded for
    that day.
    """
    path = Path(db_path) if db_path is not None else Path(_DB_PATH)
    if not path.exists():
        return None
    conn = None
    try:
        conn = sqlite3.connect(
            f"file:{path.resolve()}?mode=ro", uri=True, timeout=1.0,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=1000")
        if run_id:
            row = conn.execute(
                "SELECT * FROM intra_check_reports WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        elif date:
            row = conn.execute(
                "SELECT * FROM intra_check_reports WHERE date = ? "
                "ORDER BY timestamp DESC LIMIT 1", (date,),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT * FROM intra_check_reports "
                "ORDER BY timestamp DESC LIMIT 1"
            ).fetchone()
    except sqlite3.DatabaseError as exc:
        logger.warning("stored intra_check report read failed: %s", exc)
        return None
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass
    if not row:
        return None
    record = dict(row)
    try:
        payload = json.loads(record.get("payload_json") or "")
    except (TypeError, ValueError):
        logger.error(
            "stored intra_check report for %s has unreadable payload",
            record.get("run_id"),
        )
        return None
    if not isinstance(payload, dict):
        return None
    positions = None
    if record.get("positions_json"):
        try:
            parsed = json.loads(record["positions_json"])
            positions = parsed if isinstance(parsed, list) else None
        except (TypeError, ValueError):
            positions = None
    return {
        "date": record.get("date"),
        "run_id": record.get("run_id"),
        "timestamp": record.get("timestamp"),
        "payload": payload,
        "positions": positions,
    }


def render_stored_intra_check(record: dict, elapsed_seconds: float = 0.0) -> str:
    """One stored intra_check tick, rendered honestly.

    Deliberately NOT `format_session_result("intra_check", ...)`. That
    formatter decides whether to speak at all from the CURRENT wall clock
    (`_is_hourly_checkpoint`, `_is_midday_collision_tick`) and from a live
    "last hour" evidence window (`_read_hour_evidence`/
    `_read_hour_trade_count`) measured from now, not from the tick's own
    time. Reusing it to replay an old tick would either render nothing
    (today's clock says "not a checkpoint minute") or silently substitute
    TODAY's last-hour activity for that tick's — both worse than a
    purpose-built read of exactly what this tick itself recorded.

    Renders the tick's own status, P&L, stop-coverage finding, book and
    intraday-scan outcome straight from the stored payload. This is not a
    byte-for-byte reproduction of whatever Telegram message (if any) that
    tick produced live — it is the tick's own durable record.
    """
    payload = record.get("payload") if isinstance(record, dict) else None
    if not isinstance(payload, dict):
        raise ValueError("stored intra_check report has no usable payload")

    result = dict(payload)
    gaps: list[str] = []

    positions = record.get("positions")
    if isinstance(positions, list):
        result["_positions"] = positions
    else:
        result["_positions"] = []
        gaps.append("the book as it stood at this tick")

    if payload.get("stop_coverage_gaps") is None:
        gaps.append("the stop-coverage audit result")

    status = str(payload.get("status", "unknown"))
    date_text = record.get("date") or "date not recorded"
    run_id = record.get("run_id")
    lines = [
        _b(f"STORED HALF-HOURLY CHECK · {date_text}"),
        "   Read back from the stored record. Nothing was run to produce "
        "this: no broker call, no model call, no order. This tick's own "
        "status only — not a re-derivation of the hourly DESK CHECK "
        "message, which also reflects other ticks around it.",

    ]
    # P&L FIRST, directly under the heading block — owner, 2026-09-18. It
    # used to follow the status line; the status of a replayed tick is
    # desk detail, the P&L is his money.
    _new_section(lines, *_pnl_section_lines(result))
    _new_section(lines, f"{_status_emoji(status)} {humanize_status(status)}")
    _new_block(lines, _append_coverage_gaps, result)
    risk_positions = [
        row for row in result["_positions"]
        if isinstance(row, dict)
        and str(row.get("symbol", "")).upper() not in _SWEEP_SYMBOLS
    ]
    profiles = _profiles(risk_positions)
    lines.append(f"💼 Positions held: {len(risk_positions)}")
    for row in risk_positions:
        lines.append(f"   • {_ticker_co(str(row.get('symbol', '?')), profiles)}")
    scan = payload.get("intraday_scan")
    if isinstance(scan, dict):
        lines.append(f"🔎 Movers scanned: {_movers_scanned_text(scan)}")

    if gaps:
        if len(gaps) == 1:
            named = gaps[0]
        else:
            named = ", ".join(gaps[:-1]) + " and " + gaps[-1]
        lines += [
            "",
            _b("NOT AVAILABLE"),
            f"   The stored record does not contain {named}. That is "
            f"missing information, not an empty result — do not read the "
            f"silence above as an all-clear.",
        ]
    return "\n".join(lines)
