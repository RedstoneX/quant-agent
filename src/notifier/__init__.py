"""Telegram session-status push notifications.

Disabled when TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID env vars are
missing — callers get a no-op notifier so they don't need to branch.
HTTP failures are swallowed: a Telegram outage must never affect
trading.

Per-mode noise policy (see `format_session_result`):
  - morning / midday / close / evening: always notify on completion
  - earnings_preprocess: notify only when filings were analyzed
    (skip "nothing_new" — happens most pre-market days)
  - intra_check: notify only on emergency action (skip the 14
    silent OK ticks per trading day)
  - meta: notify on actual run; skip "not_quarter_end" / etc.
  - daily (P&L CSV export): the CSV itself goes out as a Telegram
    document with a self-describing caption, so the "sent" status
    text is suppressed (the document IS the confirmation); "error"
    (with the reason) and "skipped" still notify
  - Any session that raised an exception: always notify

Readability/links: the operator reads these on his phone. Per-field
truncation used to clip PM/tech rationale, the evening outlook, and error
text well below Telegram's real 4096-char message limit, with a raw
`text[:N]` slice that could (and did — a BUY CRM alert reading "...strong
heavy accumulation volume" just stopped there) cut mid-word with no
indication anything had been dropped. `_clip_text` below is the shared,
boundary-aware replacement: every field-level clip in this module and in
src/trader_feed.py's `_clip` now goes through it, with limits raised to use
the actual budget instead of an arbitrary small one.

`TelegramNotifier.send()` now sets `parse_mode="HTML"` and escapes every
outgoing message with `html.escape()` before transmission. HTML was chosen
over MarkdownV2 specifically because PM/tech rationale is full of
underscores (tickers, snake_case), asterisks, parentheses, and percent
signs — MarkdownV2 requires escaping ~18 characters or Telegram rejects the
whole message ("can't parse entities"); HTML requires exactly three
('&','<','>'). `send()` also accepts an optional `link_url`/`link_label`
(defaulting to the instance's `mission_control_url`, itself sourced from
`config/settings.yaml: notifications.mission_control_url` — see
src/config.py::NotificationsConfig) and appends it as a real `<a href>` tap-
through link. An empty/unset URL means no link is appended, ever — never a
broken one.

`send()` also accepts an optional `symbols` list — each ticker it names
that also appears in the message text gets wrapped in its own `<a href>`
link (see `_linkify_symbols`), pointing at a public quote page (an
EXTERNAL FALLBACK: the cockpit has no URL routing yet to link a symbol, or
a run, to our own data — see the comment above `_SYMBOL_QUOTE_URL_TEMPLATE`).
Symbol linking is best-effort and silently drops rather than risk
truncating an `<a>` tag mid-markup.
"""

from __future__ import annotations

import html  # noqa: F401  (re-exported module namespace)
import logging  # noqa: F401
import os  # noqa: F401
import re  # noqa: F401
import sys as _sys
import types as _types
from dataclasses import dataclass  # noqa: F401
from pathlib import Path  # noqa: F401
from typing import Any  # noqa: F401

import requests  # noqa: F401  (tests patch src.notifier.requests)

from src.notifier.base import (
    logger,
    _REHEARSAL_MODE,
    _DB_PATH,
    ProbeResult,
    _SWEEP_SYMBOLS,
    _clip_text,
    _MALFORMED_NUMBER_MARKER,
    _NUMERIC_TOKEN_RE,
    _malformed_numeric_reason,
    _find_malformed_numeric_tokens,
    _redact_malformed_numbers,
)
from src.notifier.sections import (
    _RAW_ERROR_MARKER,
    _TRACEBACK_RE,
    _TRACE_FRAME_RE,
    _EXCEPTION_TOKEN_RE,
    _redact_raw_exception_text,
    _seal_section,
    _new_section,
    _new_block,
    _STATUS_LABELS,
    humanize_status,
    _MODE_LABELS,
    mode_label,
    format_settled_money,
    describe_ai_cost,
    _fmt_signed_money,
    fmt_time_12h,
)
from src.notifier.markup import (
    _SYMBOL_QUOTE_URL_TEMPLATE,
    _SYMBOL_TOKEN_RE,
    _MAX_LINKED_SYMBOLS,
    _symbol_quote_url,
    _linkify_symbols,
    _MARKUP_PLACEHOLDERS,
    _escape_with_markup,
    _close_open_markup,
    _dedupe_symbols,
    _status_emoji,
    _order_side,
    _order_summary,
    _fmt_qty,
    _fmt_price,
    _fmt_elapsed,
    _attr_or_key,
)
from src.notifier.category import (  # noqa: F401
    CATEGORY_OPERATIONAL,
    CATEGORY_RISK,
    SUPPRESSED,
    SuppressedSend,
    _KIND_CATEGORY,
    _risk_only_declared_default,
    filtered_by_category,
    resolve_category,
    resolve_risk_only,
    was_suppressed,
)
from src.notifier.transport import (
    TelegramNotifier,
)
from src.notifier.owner_alert import (
    _ALERT_NO_PNL_LINE,
    _with_pnl_header,
    build_default_notifier,
    send_owner_alert,
)
from src.notifier.gaps import (
    _actionable_coverage_gaps,
    _gap_is_uncovered,
    _append_coverage_gap_banner,
    _gap_is_unreadable,
    _gap_is_expected_fractional,
    _append_fractional_overnight_line,
)
from src.notifier.parts import (
    _append_leverage_line,
    _MAX_LOOKED_UP_COMPANIES,
    _lookup_company_profiles,
    company_name,
    _append_company_identities,
    describe_target_revisions,
    _pending_confirmation_reason,
    _target_revision_reason,
)
from src.notifier.snapshots import (
    _append_position_snapshot,
    _append_earnings_body,
    _append_intra_check_body,
    _append_meta_body,
    build_daily_csv,
)
from src.notifier.trade_body import (
    _append_trade_session_body,
)
from src.notifier.evening import (
    _evening_pnl_block,
    _append_evening_body,
)

# The three modules below carry the call-site imports that put src.notifier
# in import cycles (src.config, src.trader_feed, src.execution.broker, ...).
# They are resolved lazily through the ONE `__getattr__` so the package itself
# holds no static edge to them; every name still reads as `src.notifier.X`.
_LAZY_NAME_OWNERS = {
    "_ALERT_EXEMPT_PER_SEAT": "src.notifier.wording",
    "_SEAT_WORDS": "src.notifier.wording",
    "_congress_enabled_now": "src.notifier.wording",
    "_smart_money_seat_label": "src.notifier.wording",
    "_DATA_STATUS_WORDS": "src.notifier.wording",
    "seat_words": "src.notifier.wording",
    "describe_data_status": "src.notifier.wording",
    "_seat_list_words": "src.notifier.wording",
    "describe_evidence_freshness": "src.notifier.wording",
    "describe_short_handed_decision": "src.notifier.wording",
    "describe_universe_changes": "src.notifier.wording",
    "_append_universe_changes": "src.notifier.wording",
    "_append_evidence_freshness": "src.notifier.alerts",
    "describe_skipped_decision": "src.notifier.alerts",
    "maybe_alert_data_quality": "src.notifier.alerts",
    "alert_order_outcome_unconfirmed": "src.notifier.alerts",
    "alert_records_disagree_with_broker": "src.notifier.alerts",
    "alert_stop_out_recorded": "src.notifier.alerts",
    "alert_positions_reprotected": "src.notifier.alerts",
    "_session_cost_line": "src.notifier.costs",
    "_day_cost_line": "src.notifier.costs",
    "_daily_cost_limit": "src.notifier.costs",
    "_persist_margin_interest_daily": "src.notifier.costs",
    "_read_margin_interest_daily_all": "src.notifier.costs",
    "_margin_interest_lines": "src.notifier.costs",
    "_pnl_lines_for": "src.notifier.session",
    "format_session_result": "src.notifier.session",
}
_SUBMODULES = (
    "src.notifier.base",
    "src.notifier.category",
    "src.notifier.send_log",
    "src.notifier.sections",
    "src.notifier.markup",
    "src.notifier.transport",
    "src.notifier.send_funnel",
    "src.notifier.owner_alert",
    "src.notifier.wording",
    "src.notifier.alerts",
    "src.notifier.gaps",
    "src.notifier.parts",
    "src.notifier.snapshots",
    "src.notifier.trade_body",
    "src.notifier.evening",
    "src.notifier.costs",
    "src.notifier.session",
)


def __getattr__(name: str):
    module_path = _LAZY_NAME_OWNERS.get(name)
    if module_path is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib

    obj = getattr(importlib.import_module(module_path), name)
    globals()[name] = obj
    return obj


def __dir__():
    return sorted(set(globals()) | set(_LAZY_NAME_OWNERS))


# --- Patch mirroring. Every definition moved verbatim into one of the
# submodules, each of which holds its own binding of the shared names it
# imports. Tests patch names on `src.notifier` (`requests`, `send_owner_alert`,
# `_DB_PATH`, `_REHEARSAL_MODE`, ...) and expect the moved code to see the
# patched object. Mirroring the assignment into any submodule that already
# holds the name keeps it the SAME object on both sides. A delete (how
# `mock.patch` unwinds a name it found through `__getattr__`) puts the
# pristine object back in every submodule instead of deleting it there.
# This is the ONE mirror block for this package; do not add a second one.
_PRISTINE: dict[str, object] = {}


class _NotifierMirroringModule(_types.ModuleType):
    def __setattr__(self, name: str, value) -> None:
        if name in vars(self):
            _PRISTINE.setdefault(name, vars(self)[name])
        super().__setattr__(name, value)
        for module_path in _SUBMODULES:
            sub = _sys.modules.get(module_path)
            if sub is not None and name in vars(sub):
                setattr(sub, name, value)

    def __delattr__(self, name: str) -> None:
        super().__delattr__(name)
        pristine = _PRISTINE.pop(name, None)
        for module_path in _SUBMODULES:
            sub = _sys.modules.get(module_path)
            if sub is not None and name in vars(sub):
                if pristine is None:
                    delattr(sub, name)
                else:
                    setattr(sub, name, pristine)


_sys.modules[__name__].__class__ = _NotifierMirroringModule
