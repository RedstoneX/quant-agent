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

import html
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests

logger = logging.getLogger(__name__)

# Default DB path, anchored to the project root rather than CWD. The
# notifier is invoked both from launchd/systemd (which set the project
# root as WorkingDirectory) and from manual `python /abs/path/main.py`
# from somewhere else — the latter used to silently miss the cost line
# and position snapshot because `Path("data/...")` resolved relative to
# the caller's CWD.
#: Set by the rehearsal harness (`ops/rehearsal/`). When true, no operator
#: alert leaves this process — see `TelegramNotifier.send`. It is an env var
#: rather than config because it must hold for any code path that builds a
#: notifier, including ones that construct their own from `.env` directly.
_REHEARSAL_MODE = os.environ.get("QAMC_REHEARSAL") == "1"


_DB_PATH = Path(__file__).resolve().parent.parent / "data" / "quant_agent.db"


@dataclass(frozen=True)
class ProbeResult:
    """Outcome of `TelegramNotifier.probe()` — did the alert channel work.

    `stage` names the first thing that failed, because the three failures
    need three different repairs and "the alert didn't send" does not tell
    an operator which one he has:

      credentials — the process has no TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID
                    (or TELEGRAM_DISABLED is set). Fix the unit's env, not
                    the network.
      transport   — the POST never completed: DNS, TLS, proxy, an egress
                    rule, a timeout. Fix the box's outbound path.
      api         — Telegram answered and refused: revoked token (401),
                    wrong or deleted chat id (400 "chat not found"), bot
                    blocked by the user (403). Fix the credential or the
                    chat.
      delivered   — the message really went out. ok=True.

    `residue` means the send worked but the tidy-up delete did not, so one
    self-describing probe message is sitting in the operator's chat. Not a
    failure of the alert channel — the channel demonstrably works — but
    worth saying so nobody wonders what the stray message was.
    """

    ok: bool
    stage: str
    detail: str = ""
    residue: bool = False

    def summary(self) -> str:
        verdict = "alert channel PROVED" if self.ok else "alert channel BROKEN"
        line = f"{verdict} (stage={self.stage})"
        if self.detail:
            line += f": {self.detail}"
        if self.residue:
            line += " [probe message could not be deleted; it stays in the chat]"
        return line

# Cash-sweep parking vehicles — cash equivalents, never "deployed capital".
# The notifier reads the DB directly (it deliberately doesn't thread config
# in — see the comment at the sqlite3 connect), so it can't ask
# CashSweepConfig for the configured symbol. Cover the supported vehicles;
# an unknown custom symbol degrades to today's behaviour (counted as a
# position), which is visible rather than silent.
_SWEEP_SYMBOLS = frozenset({"SGOV", "BIL"})


def _clip_text(text: str, max_chars: int, marker: str = " …") -> str:
    """Shorten `text` to at most `max_chars`, cutting on a sentence or word
    boundary and appending `marker` — never a hard mid-word chop.

    The bug this replaces: `text[:N]` throughout this module (and
    src/trader_feed.py's own `_clip`) sliced on a raw character count with
    no regard for what was at that boundary. The operator's actual report
    was a BUY CRM alert whose rationale read "...strong heavy accumulation
    volume" and simply stopped — no ellipsis, no "see more", nothing to
    indicate the sentence had been cut at all, well below Telegram's real
    4096-char message limit.

    Preference order: the last '. '/'! '/'? ' inside the budget (reads as a
    complete thought); then the last whitespace (never split a word); a
    hard cut only when the text has no boundary at all within the budget
    (e.g. one unbroken token) — the single case this still can't avoid.
    """
    if max_chars <= 0:
        return ""
    if len(text) <= max_chars:
        return text
    if max_chars <= len(marker):
        return text[:max_chars]
    budget = max_chars - len(marker)
    window = text[:budget]

    best = -1
    for punct in (". ", "! ", "? "):
        idx = window.rfind(punct)
        if idx > best:
            best = idx
    # Trust a sentence boundary only if it doesn't throw away most of the
    # budget (an early ". " — an abbreviation, a list separator — would
    # otherwise clip far more aggressively than max_chars intends).
    if best >= budget * 0.4:
        return window[: best + 1].rstrip() + marker

    space = window.rfind(" ")
    if space > 0:
        return window[:space].rstrip() + marker

    return window.rstrip() + marker


def _seal_section(lines: list[str], start: int, *, may_glue: bool = False) -> None:
    """Insert one blank line before `lines[start:]` to set it apart from
    whatever precedes it — unless there is nothing to separate from yet
    (`start == 0`, still the first content), the new block turned out to be
    empty, or (`may_glue=True`) its first line is an indented continuation
    (bullets and sub-lines like "   * ..." / "   View: ..." throughout this
    module and src/trader_feed.py).

    `may_glue` defaults False — a new block always gets its own section —
    because most callers' output never starts indented, so the default is
    "always separate", not "guess from the text". Pass `may_glue=True` only
    for a block whose SHAPE depends on the data: `src/trader_feed.py`'s PM
    section sometimes renders its own "🧠 PM/Constructor:" heading and
    sometimes, when there is nothing to decide, renders only a "   View:"
    line continuing whatever came before it (normally the signals list) —
    that one has to be told to glue when it turns out to be a continuation.

    The one place both this module and src/trader_feed.py insert a section
    break, replacing what used to be ad-hoc `lines.append("")` calls
    scattered through each formatter — and the reason a message never ends
    up with a double blank line (a block that added nothing never gets a
    break inserted before it) or a leading one (nothing precedes the first
    section, so `start == 0` short-circuits).
    """
    if len(lines) <= start or start == 0:
        return
    if may_glue and lines[start].startswith("   "):
        return
    lines.insert(start, "")


def _new_section(lines: list[str], *new_lines: str, may_glue: bool = False) -> None:
    """Append `new_lines` as their own visual section — see `_seal_section`
    for the exact rule. No-op when `new_lines` is empty, so a caller can
    always call this unconditionally instead of guarding with `if text:`."""
    if not new_lines:
        return
    start = len(lines)
    lines.extend(new_lines)
    _seal_section(lines, start, may_glue=may_glue)


def _new_block(lines: list[str], render, *args, may_glue: bool = False, **kwargs) -> None:
    """Call `render(lines, *args, **kwargs)` — which appends its own block
    of zero or more lines in place via loops/conditionals rather than
    returning a ready list — then seal it off from whatever precedes it;
    see `_seal_section`. For the many `_append_*` helpers below and in
    src/trader_feed.py built that way.
    """
    start = len(lines)
    render(lines, *args, **kwargs)
    _seal_section(lines, start, may_glue=may_glue)


#: An internal status code -> the OUTCOME in plain words, written to read
#: naturally after a session name: "Pre-market filings \u2014 nothing new
#: was filed". Board item 89 clarity defect "internal status codes shown
#: as-is": a code with its underscores taken out is still a code, so these
#: are phrases rather than title-cased identifiers.
_STATUS_LABELS: dict[str, str] = {
    "ok": "nothing to report",
    "executed": "traded",
    "intraday_executed": "traded",
    "analyzed": "done",
    "reviewed": "reviewed, nothing to change",
    "preprocessed": "done",
    "reflected": "done",
    "sent": "sent",
    "no_trades": "no trade taken",
    "intraday_no_trades": "no trade taken",
    "no_data": "no data to work from",
    "nothing_new": "nothing new was filed",
    "market_holiday": "the market was shut",
    "early_close": "the market closed early",
    "rejected": "the risk check turned the plan down",
    "hard_risk_block": "blocked by the risk rules",
    "symbol_block": "blocked by the risk rules",
    "buys_unfunded": "there was not enough cash to fund the trade",
    "failed": "it did not finish",
    "error": "it did not finish",
    "analysis_error": "the thinking step failed",
    "intraday_analysis_error": "the thinking step failed",
    "broker_error": "the broker could not be reached",
    "fetch_error": "the data could not be fetched",
    "emergency_sold": "an emergency sale was made (historical)",
    # Kept, like `emergency_sold`, so a run stored before 2026-09-20 still
    # renders as words. The whole account-level loss alarm was removed that
    # day on the owner's instruction (retired item 32); nothing emits this.
    "daily_loss_halted": "stopped for the day after losses (historical)",  # retired-ok
    "kill_switch_halted": "stopped by the manual kill switch",
    "paid_analysis_suspended": "paid thinking is suspended",
    "evidence_gate_skip": "skipped \u2014 the data was incomplete",
    "intraday_scan_crashed": "the scan for movers crashed",
    "intraday_scan_disabled": "the scan for movers is switched off",
    "intraday_scan_lock_contended": "the scan for movers was delayed (busy)",
    "intraday_scan_no_opportunity": "no movers worth looking at",
    "digest_only": "only partly completed",
    "skipped": "skipped",
    "scheduler_exited": "the scheduler stopped",
    "unknown": "outcome not recorded",
}


def humanize_status(status: str) -> str:
    """Plain-English rendering of an internal status code for the owner-
    facing header line \u2014 e.g. "intraday_no_trades" -> "no trade taken".

    A status with no entry in the table above is reported as exactly that:
    an outcome nobody has plain wording for. It is never paraphrased into
    something the wording cannot support, and the raw code is never passed
    off as a sentence written for the reader. It is not dropped either: it
    is the only record of what happened.
    """
    status = str(status or "")
    label = _STATUS_LABELS.get(status)
    if label:
        return label
    if not status.strip():
        return "outcome not recorded"
    return (
        "an outcome the desk has no plain wording for (its own code for it, "
        f"kept for the record, is \u201c{status}\u201d)"
    )


#: Internal session name -> the name the owner would use for it. The mode
#: string is a scheduler identifier ("earnings_preprocess", "intra_check");
#: putting it in a message is the same defect as printing a status code.
#: Owner review, 2026-09-18, extending the evening report's ratified
#: standard (PR #471) to every other message.
_MODE_LABELS: dict[str, str] = {
    "morning": "Morning session",
    "midday": "Midday review",
    "close": "Closing review",
    "evening": "Evening report",
    "once": "One-off session",
    "intra_check": "Half-hourly check",
    "earnings_preprocess": "Pre-market filings",
    "meta": "Quarterly self-review",
    "daily": "Daily performance export",
}


def mode_label(mode: str) -> str:
    """The owner-facing name of a session, never the scheduler identifier."""
    raw = str(mode or "")
    label = _MODE_LABELS.get(raw)
    if label:
        return label
    text = raw.replace("_", " ").strip()
    return (text[:1].upper() + text[1:]) if text else "Session"


def describe_ai_cost(
    cost: float | None,
    label: str = "AI cost for this run",
    free_note: str = "this run used only free models",
) -> str:
    """The AI spend for a run, in words.

    Owner review of the live 17 September evening message: "$0.0000" read as
    broken. It is not \u2014 it is true. `src/cost_table.py` pins the
    free-tier seats at $0.00 as a deliberate price, not as an unpriced
    placeholder. So the honest rendering is a sentence, not four decimal
    places.

    This is the SINGLE implementation of that wording. The evening
    formatter's `src.trader_feed._evening_cost_line` now calls it rather
    than keeping its own copy, so the two can never drift; it lives here
    because src/trader_feed.py imports src/notifier.py and not the reverse.

    `None` means "the desk could not read what this cost" and says exactly
    that \u2014 it never renders as zero. Inventing a number in an
    owner-facing message is the worst failure mode on this desk.
    """
    if cost is None:
        return f"{label}: not available"
    try:
        value = float(cost)
    except (TypeError, ValueError):
        return f"{label}: not available"
    if value <= 0:
        return f"{label}: none \u2014 {free_note}"
    if value < 0.01:
        return f"{label}: under one cent"
    return f"{label}: ${value:,.2f}"


def _fmt_signed_money(value: float) -> str:
    """'+$12.34' / '−$12.34' — sign BEFORE the '$', and a true minus
    sign (U+2212) for negative amounts rather than Python's default
    '$+12.34' / '$-12.34', which reads oddly on a phone."""
    sign = "+" if value >= 0 else "−"
    return f"{sign}${abs(value):,.2f}"


def fmt_time_12h(dt) -> str:
    """'1:05 PM ET' — 12-hour clock, no leading zero, AM/PM, ET suffix.

    Owner ratified 2026-09-17: no 24-hour clock anywhere in a Telegram
    message — every header and any timestamp inside a message body or
    DETAILS block uses this, never a bare `strftime('%H:%M')`.
    `strftime('%-I:%M %p')` (no leading zero) is a glibc-only extension —
    computed manually here instead so this doesn't silently regress on a
    non-glibc platform."""
    hour12 = dt.hour % 12 or 12
    ampm = "AM" if dt.hour < 12 else "PM"
    return f"{hour12}:{dt.minute:02d} {ampm} ET"


# === Per-symbol tap-through links ===
#
# EXTERNAL FALLBACK, not a Mission Control deep link. As of this writing the
# cockpit (frontend/src/) has no URL routing at all — no react-router, no
# query-string or #hash parsing, nothing that reads window.location. A
# per-symbol view already exists INSIDE the running app (App.tsx's
# chartSymbol / onSelectSymbol wires SearchPanel/TradesPanel clicks to
# PriceChartPanel), but nothing outside the page can open it directly — the
# same gap that blocks a per-run deep link (see `mission_control_url`
# above). Until the cockpit grows real routing, this points at a public
# quote page instead. That leaves our own evidence behind; it is a
# deliberate, named trade-off, not a design goal.
_SYMBOL_QUOTE_URL_TEMPLATE = "https://finance.yahoo.com/quote/{symbol}"
# Ticker shape only (e.g. "CCJ", "BRK.B") — guards against linkifying
# something that was never meant to be a symbol if an upstream caller ever
# passes free text by mistake.
_SYMBOL_TOKEN_RE = re.compile(r"^[A-Z]{1,6}(?:\.[A-Z]{1,2})?$")
# Bounds worst-case message growth from linkification (each wrapped mention
# costs the ~40-50 chars of `<a href="https://finance.yahoo.com/quote/...">`
# on top of the bare ticker) and keeps the compiled regex small.
_MAX_LINKED_SYMBOLS = 10


def _symbol_quote_url(symbol: str) -> str:
    return _SYMBOL_QUOTE_URL_TEMPLATE.format(symbol=symbol)


def _linkify_symbols(escaped_text: str, symbols: list[str] | None) -> str:
    """Wrap every mention of a known ticker in `escaped_text` with a
    tap-through `<a href>` link, so the operator can tap a symbol in an
    alert the same way he taps the Mission Control link.

    MUST be called on text that has already been through `html.escape()`
    and BEFORE `link_html`/truncation are applied — see `_build_payload`.
    Calling it earlier would have the anchor markup itself escaped into
    literal `&lt;a href...&gt;` text; calling it after truncation risks a
    ticker mention landing right at the cut.

    Deliberately narrow matching: only symbols the CALLER already knows are
    real (`symbols`, sourced from structured order/trade/skip data — see
    `src/trader_feed.py::extract_alert_symbols`) are ever linked, matched
    whole-word and case-sensitive. This never scans free LLM prose for
    uppercase words — PM/risk rationale routinely contains words like
    "ALL", "GO", "GDP" that would false-positive as tickers if it did.
    """
    if not symbols or not escaped_text:
        return escaped_text

    seen: list[str] = []
    for raw in symbols:
        sym = str(raw or "").strip().upper()
        if sym and _SYMBOL_TOKEN_RE.match(sym) and sym not in seen:
            seen.append(sym)
        if len(seen) >= _MAX_LINKED_SYMBOLS:
            break
    if not seen:
        return escaped_text

    # Longest-first so a short ticker that happens to be a prefix of a
    # longer one (rare, but e.g. "A" vs "AA") can't win the alternation
    # before the longer, more specific match is tried.
    pattern = re.compile(
        r"\b(" + "|".join(re.escape(s) for s in sorted(seen, key=len, reverse=True)) + r")\b"
    )

    def _wrap(match: "re.Match[str]") -> str:
        sym = match.group(0)
        url = html.escape(_symbol_quote_url(sym), quote=True)
        return f'<a href="{url}">{sym}</a>'

    return pattern.sub(_wrap, escaped_text)


# === Structural markup (2026-09-17 scan-first redesign) ===
#
# src/trader_feed.py's formatters build their PLAIN text with a fixed, small
# set of literal HTML tags embedded in it — `<b>section header</b>` and
# `<blockquote expandable>...</blockquote>` around the collapsed DETAILS
# block (see its module docstring; Telegram Bot API HTML style,
# https://core.telegram.org/bots/api#html-style, documents both). `send()`
# still must `html.escape()` the REST of the text — PM/risk prose is full of
# '&', tickers can carry other punctuation, and an unescaped '<'/'>' from an
# LLM would either corrupt the message or get it rejected outright. Naively
# escaping the whole string would mangle the very tags this module just
# wrote. `_escape_with_markup` is the fix: swap the fixed tags out for
# placeholders no caller-controlled text can produce, escape everything
# else exactly as before, then swap the tags back in. A ticker, PM
# rationale, or any other field can never inject a tag this way — only
# these four fixed strings are ever restored.
_MARKUP_PLACEHOLDERS: tuple[tuple[str, str], ...] = (
    ("<b>", ""),
    ("</b>", ""),
    ("<blockquote expandable>", ""),
    ("</blockquote>", ""),
)


def _escape_with_markup(text: str) -> str:
    """`html.escape(text)` while preserving the fixed structural tags in
    `_MARKUP_PLACEHOLDERS` — see the block comment above."""
    working = text
    for tag, placeholder in _MARKUP_PLACEHOLDERS:
        working = working.replace(tag, placeholder)
    escaped = html.escape(working)
    for tag, placeholder in _MARKUP_PLACEHOLDERS:
        escaped = escaped.replace(placeholder, tag)
    return escaped


def _close_open_markup(body: str) -> str:
    """After the LAST-RESORT emergency truncation in `_build_payload`
    (`_clip_text` only avoids splitting a *word* or an HTML *entity* — it
    knows nothing about `<b>`/`<blockquote expandable>`), guarantee `body`
    carries no dangling partial tag and no unterminated structural tag.
    Telegram rejects the ENTIRE message ("can't parse entities") over one
    broken tag, which would be strictly worse than the plain-text
    truncation this replaces. The formatters themselves size DETAILS to
    fit before this ever runs (see `trader_feed._wrap_details`); this is
    only the safety net for the aggregate still somehow running long.
    """
    last_lt = body.rfind("<")
    last_gt = body.rfind(">")
    if last_lt > last_gt:
        body = body[:last_lt]
    if body.count("<blockquote expandable>") > body.count("</blockquote>"):
        body += "</blockquote>"
    if body.count("<b>") > body.count("</b>"):
        body += "</b>"
    return body


class TelegramNotifier:
    """Best-effort Telegram Bot API notifier.

    Reads `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` from the
    environment at construction. If either is missing, `enabled`
    stays False and every `send` call is a no-op.

    `TELEGRAM_DISABLED=1` overrides the env-var path so an operator
    can mute notifications without unsetting the bot creds.
    """

    API_URL = "https://api.telegram.org/bot{token}/sendMessage"
    API_BASE = "https://api.telegram.org/bot{token}"
    HTTP_TIMEOUT_S = 5.0
    #: Sent by `probe()` and deleted a moment later. Written to be
    #: self-explanatory on the off chance the delete fails and the operator
    #: reads it — an unexplained robot message in the alert channel is
    #: exactly the kind of thing that teaches someone to mute the channel.
    PROBE_TEXT = (
        "QAMC alerting self-test. This is the scheduled check that the alert "
        "channel still works; it deletes itself a second later. If you are "
        "reading it, only the delete failed — the channel is fine."
    )
    # Telegram hard limit is 4096; leave room for a truncation marker.
    MAX_MESSAGE_CHARS = 4000
    DEFAULT_LINK_LABEL = "🔗 Open Mission Control"

    def __init__(
        self,
        token: str | None = None,
        chat_id: str | None = None,
        mission_control_url: str | None = None,
    ):
        self.token = (token if token is not None else os.getenv("TELEGRAM_BOT_TOKEN", "")).strip()
        self.chat_id = (chat_id if chat_id is not None else os.getenv("TELEGRAM_CHAT_ID", "")).strip()
        kill_switch = os.getenv("TELEGRAM_DISABLED", "").strip().lower() in ("1", "true", "yes")
        self.enabled = bool(self.token and self.chat_id) and not kill_switch
        # Tap-through link target for send(). Unlike token/chat_id this is
        # NOT read from the environment — src/config.py::NotificationsConfig
        # (config/settings.yaml: notifications.mission_control_url) is the
        # source of truth, so it arrives as a constructor arg from a caller
        # holding a resolved AppConfig (main.py, TradingScheduler). An
        # empty/unset value means send() appends no link — never a broken
        # one. str(...) guards against a non-str default sneaking through
        # (e.g. a test double); production always passes a validated str.
        self.mission_control_url = str(mission_control_url or "").strip()
        if not self.enabled:
            if kill_switch:
                logger.info("TelegramNotifier: disabled via TELEGRAM_DISABLED env var")
            else:
                logger.info(
                    "TelegramNotifier: disabled (set TELEGRAM_BOT_TOKEN + "
                    "TELEGRAM_CHAT_ID env vars to enable)"
                )

    def _redact(self, value: object) -> str:
        """Strip the bot token AND chat id out of anything headed for the
        log or the durable send record (see `_record_send`).

        `requests` embeds the full request URL in HTTPError /
        ConnectionError messages, and ours is
        `https://api.telegram.org/bot<TOKEN>/sendMessage` — so logging
        the raw exception wrote the bot token into quant_agent.log (and
        the systemd journal) on every Telegram failure. A wrong or
        rotated token is the most likely failure, i.e. the token leaked
        exactly when the operator was most likely to share the log.

        The chat id is stripped too (2026-09-18, `_record_send`): it is not
        a secret the way the token is, but it is a stable per-operator
        identifier with no reason to sit in a table a future export or
        support conversation might carry, so it gets the same treatment
        for free here rather than a second bespoke check at the call site.

        Non-raising on purpose: this runs INSIDE the `except` blocks
        below, and `logger.warning("%s", exc)` used to defer `str(exc)`
        to logging (which absorbs its own formatting errors). Calling
        str() eagerly here would otherwise hand an exception with a
        broken __str__ a brand-new path out of a notifier that must
        never raise into trading.
        """
        try:
            text = str(value)
            if self.token:
                text = text.replace(self.token, "<redacted>")
            if self.chat_id:
                text = text.replace(self.chat_id, "<redacted>")
            return text
        except Exception:  # noqa: BLE001
            return "<unprintable error>"

    def _record_send(
        self,
        *,
        kind: str,
        status: str,
        text: str,
        detail: str | None = None,
        run_id: str | None = None,
    ) -> None:
        """Durably record one outgoing-message attempt (sent/failed/
        suppressed) so "what did the desk try to tell the owner, and did
        it arrive" has a single answer that does not depend on the next
        message happening to land.

        Table, not a log line (see this file's module docstring for why
        `send()` never logged a success): `session_reports` /
        `intra_check_reports` / `evening_reports` (src/storage/db.py) are
        this project's established home for a run's long, rendered text —
        never the application log, which is grepped/tailed for operational
        health and would drown in 4000-char message bodies. This table
        follows the same shape (payload text + timestamp + a key to find
        it by) rather than inventing a new convention.

        Same-protection guarantee as the rest of this class: this is
        called from inside `send()`/`send_document()`'s own try/except
        (or, for the failure path, adds one more try/except around
        itself), so a recording bug — a locked DB file, a full disk, a
        schema mismatch — degrades to a `logger.warning` and the message
        still sends and the caller still gets its True/False. Recording
        must never be the reason a send looks like it failed, or the
        reason a real failure looks like it succeeded.

        Redaction: `text` and `detail` both go through `self._redact`
        before they touch SQLite. `text` should never carry the token or
        chat id (they live in the URL/payload, not the message body), but
        redacting here anyway costs nothing and means one place — not
        every call site — is responsible for the guarantee tested by
        `test_record_send_output_never_contains_token_or_chat_id`.
        """
        try:
            import sqlite3

            safe_text = self._redact(text if text is not None else "")
            safe_detail = self._redact(detail) if detail is not None else None
            # Belt and suspenders: production's data/ dir always exists by
            # the time this fires (Database() has already created it), but
            # a notifier call can in principle be the very first thing a
            # fresh checkout does (e.g. the live-scheduler startup ping in
            # main.py, before TradingPipeline/Database is constructed) —
            # don't let a missing directory be the reason recording fails.
            _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(_DB_PATH), timeout=5.0)
            try:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS notifier_sends (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        kind TEXT NOT NULL,
                        status TEXT NOT NULL,
                        run_id TEXT,
                        text TEXT NOT NULL,
                        detail TEXT,
                        timestamp TEXT NOT NULL DEFAULT (datetime('now'))
                    )
                    """
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_notifier_sends_kind_ts "
                    "ON notifier_sends(kind, timestamp)"
                )
                conn.execute(
                    "INSERT INTO notifier_sends "
                    "(kind, status, run_id, text, detail) VALUES (?, ?, ?, ?, ?)",
                    (kind, status, run_id, safe_text, safe_detail),
                )
                conn.commit()
            finally:
                conn.close()
        except Exception as exc:  # noqa: BLE001
            # Never let a recording failure look like — or cause — a send
            # failure. See docstring above.
            logger.warning("notifier: failed to record send (%s/%s): %s", kind, status, exc)

    def _safe_record_send(self, **kwargs) -> None:
        """Call `_record_send`, wrapped in its own try/except.

        `_record_send` already guards every DB failure it can anticipate
        internally, but `send()`/`send_document()` call it from inside
        their own control flow (the success path, and — for failures —
        an `except` block that must not itself raise). Doubly-defensive
        on purpose, same reasoning main.py gives for wrapping its own
        `notifier.send()` call a second time: a bug inside the recording
        path that `_record_send` did not anticipate must still be unable
        to reach the caller of `send()`/`send_document()`, which is the
        one guarantee this whole feature is not allowed to weaken.
        """
        try:
            self._record_send(**kwargs)
        except Exception as exc:  # noqa: BLE001
            logger.warning("notifier: _record_send raised unexpectedly: %s", exc)

    def send(
        self,
        text: str,
        link_url: str | None = None,
        link_label: str | None = None,
        symbols: list[str] | None = None,
        preserve_structural_markup: bool = False,
        kind: str = "generic",
        run_id: str | None = None,
    ) -> bool:
        """Fire-and-forget send. Returns True on success.

        `kind` and `run_id` (2026-09-18) label the durable record this
        call leaves behind (see `_record_send`) — `kind` is a short,
        caller-chosen tag ("morning", "owner_alert", ...; default
        "generic" for the many existing callers that have no reason to
        care), `run_id` ties it back to `agent_logs`/`session_reports`
        when a run produced this message. Neither changes delivery.

        - No-op when not enabled (returns False).
        - Escapes `text` and sends with `parse_mode="HTML"` — a stray
          underscore/asterisk/`<`/`&` in a ticker or a rationale must not
          corrupt the message or get the whole send rejected by Telegram.
        - Appends a tap-through `<a href>` link when one is available:
          `link_url` if given, else `self.mission_control_url` (from
          config). Neither set → no link, ever — never a broken one.
        - `symbols`, when given, wraps each matching ticker mentioned in
          `text` with its own tap-through link (see `_linkify_symbols`) —
          best-effort: if adding links would push the message over budget,
          it degrades to plain text rather than risk truncating markup.
        - Auto-truncates messages over MAX_MESSAGE_CHARS on a sentence/word
          boundary (see `_clip_text`) rather than mid-word.
        - Any HTTP / network / Telegram-side error is logged and
          swallowed: trading must never fail because a notifier is
          unreachable.
        - `preserve_structural_markup=False` (default): `text` is fully
          escaped — the historical, safe contract every existing caller
          relies on (cost-circuit alerts, `send_owner_alert`, scripts/*,
          none of which ever intend a literal '<' as a tag). Pass True
          ONLY for text built by src/trader_feed.py's formatters, which
          embed a fixed, small set of literal structural tags (`<b>`,
          `<blockquote expandable>`) on purpose — see
          `_escape_with_markup`'s docstring. This is opt-in, per call, not
          a global default, so a coincidental literal "<b>" in some other
          alert's free text is still rendered as visible text, not markup.
        """
        if not self.enabled:
            return False
        if not text:
            return False
        if _REHEARSAL_MODE:
            # A rehearsal replays a real session, so it raises real alerts —
            # "PAID ANALYSIS SUSPENDED", "STOP COVERAGE REPAIRED", trade
            # notifications. Delivered unmarked to the operator's normal chat
            # they are indistinguishable from production, which is worse than
            # useless: it teaches him to distrust the channel that exists to
            # tell him something is wrong.
            #
            # Refusing outright is the wrong answer too — what a rehearsal
            # WOULD have sent is evidence, and the harness captures it for the
            # report. So this suppresses delivery and says so, rather than
            # silently dropping.
            logger.info(
                "REHEARSAL: suppressed operator alert (%d chars): %s",
                len(text), text.splitlines()[0][:120] if text else "",
            )
            self._safe_record_send(kind=kind, status="suppressed", text=text, run_id=run_id)
            return False

        payload = self._build_payload(
            text, link_url, link_label, symbols,
            preserve_structural_markup=preserve_structural_markup,
        )

        try:
            response = requests.post(
                self.API_URL.format(token=self.token),
                json=payload,
                timeout=self.HTTP_TIMEOUT_S,
            )
            response.raise_for_status()
            self._safe_record_send(kind=kind, status="sent", text=text, run_id=run_id)
            return True
        except Exception as exc:
            # Catch broadly on purpose — TelegramNotifier is a
            # best-effort side channel. A 429 rate-limit, a 5xx, a
            # connection reset, a DNS failure, a bad token — none of
            # those should bubble up and crash the trading session.
            logger.warning("Telegram notify failed: %s", self._redact(exc))
            self._safe_record_send(
                kind=kind, status="failed", text=text,
                detail=self._redact(exc), run_id=run_id,
            )
            return False

    def _api_url(self, method: str) -> str:
        """Bot API endpoint for `method` (sendMessage, deleteMessage, ...)."""
        return f"{self.API_BASE.format(token=self.token)}/{method}"

    def _build_payload(
        self,
        text: str,
        link_url: str | None = None,
        link_label: str | None = None,
        symbols: list[str] | None = None,
        preserve_structural_markup: bool = False,
    ) -> dict[str, Any]:
        """The exact JSON body `send()` puts on the wire.

        Factored out so `probe()` can transmit a body built by this same
        code rather than one of its own. A probe that assembled its own
        payload would prove that *some* request reaches Telegram while
        leaving the real message shape — HTML parse mode, escaping, the
        length budget — untested, which is how a self-test ends up passing
        while the thing it stands in for is broken.
        """
        # HTML over MarkdownV2: PM/tech rationale is full of tickers with
        # underscores, "*" bullets, parentheticals, and "%" — MarkdownV2
        # demands escaping ~18 characters or Telegram rejects the entire
        # message ("can't parse entities"); HTML needs exactly '&','<','>'.
        # Escaping BEFORE the length check matters too: an unescaped '&'
        # costs 5 chars once escaped ('&amp;'), so measuring the pre-escape
        # length risks shipping something past Telegram's real 4096 cap.
        # `_escape_with_markup`, not a bare `html.escape`, ONLY when the
        # caller opted in (`preserve_structural_markup=True` — see
        # `send()`'s docstring): src/trader_feed.py embeds a fixed, small
        # set of literal structural tags (`<b>`, `<blockquote expandable>`)
        # in its plain text on purpose; no other caller does, and every
        # other caller must keep the historical "always fully escape"
        # contract.
        escaped = (
            _escape_with_markup(text) if preserve_structural_markup
            else html.escape(text)
        )

        resolved_url = link_url if link_url is not None else self.mission_control_url
        link_html = ""
        if resolved_url:
            label = link_label if link_label is not None else self.DEFAULT_LINK_LABEL
            # quote=True: this value sits inside href="...", not message
            # body text — needs '"' escaped too, not just '&','<','>'.
            safe_url = html.escape(resolved_url, quote=True)
            safe_label = html.escape(label)
            link_html = f'\n\n<a href="{safe_url}">{safe_label}</a>'

        budget = max(0, self.MAX_MESSAGE_CHARS - len(link_html))

        # Symbol links are strictly best-effort. `_clip_text` only
        # guarantees it never splits an HTML *entity* (`&amp;` has no
        # whitespace inside it); an anchor tag does (`<a href="...">` has a
        # space right after "a"), so clipping *linked* text risks shipping
        # `<a hre` — broken markup Telegram then rejects outright ("can't
        # parse entities"), losing the whole alert over a ticker link. So:
        # try the linked text first, but if it doesn't fit budget, fall
        # back to the plain (safely truncatable) escaped text instead of
        # truncating the linked one — symbol links disappear, the alert
        # still ships.
        linked = _linkify_symbols(escaped, symbols) if symbols else escaped
        if len(linked) <= budget:
            body = linked
        elif len(escaped) > budget:
            # `_clip_text` never splits an HTML entity: it only ever cuts on
            # whitespace, and an entity like '&amp;' has none inside it — so
            # the boundary search always lands on a word start/end, keeping
            # any entity in the kept text whole. This is the last-resort
            # safety net for the rare aggregate message still oversized
            # after every field-level clip below already ran — not the
            # primary fix, which is raising those per-field limits.
            body = _close_open_markup(_clip_text(escaped, budget, marker="\n[...truncated]"))
        else:
            body = escaped
        final_text = body + link_html

        return {
            "chat_id": self.chat_id,
            "text": final_text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }

    @staticmethod
    def _json_body(response: Any) -> dict[str, Any]:
        """Telegram's JSON body, or {} if it did not send parseable JSON."""
        try:
            body = response.json()
        except Exception:  # noqa: BLE001 - a proxy error page is not JSON
            return {}
        return body if isinstance(body, dict) else {}

    def probe(self, text: str | None = None) -> ProbeResult:
        """Prove the alert channel works, by using it.

        WHY THIS IS NOT AN ENV-VAR CHECK. "Are the credentials set" answers
        a question nobody has. The failures that actually silence this desk
        are a token that is set but revoked, a chat id that is set but wrong
        or deleted, a bot the operator blocked, and an egress rule that
        drops api.telegram.org. Every one of those passes a variable check
        and fails a send. So this sends.

        WHY IT IS STILL QUIET. The message goes out with
        `disable_notification` (delivered, no buzz) and is deleted
        immediately afterwards, so proving the channel does not spend the
        operator's attention. The Bot API lets a bot delete its own message
        for 48h, so the delete is reliable; when it is not, `residue` says
        so and the message itself explains what it is.

        Returns a ProbeResult rather than a bool: an operator needs to know
        WHICH of the four failures he has, and they need four different
        repairs.
        """
        if _REHEARSAL_MODE:
            # A rehearsal must not transmit. Reported as not-ok with its own
            # stage so a caller can tell "we did not check" apart from "we
            # checked and it is broken" — collapsing those two is the exact
            # defect this whole probe exists to remove.
            return ProbeResult(
                False, "rehearsal", "suppressed: QAMC_REHEARSAL=1, nothing sent",
            )
        if not self.enabled:
            return ProbeResult(
                False,
                "credentials",
                "this process has no TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID "
                "(or TELEGRAM_DISABLED is set) — an alert raised here would "
                "reach nobody",
            )

        # link_url="" — never append the Mission Control link to a probe.
        payload = self._build_payload(text or self.PROBE_TEXT, link_url="")
        # Delivered silently: the operator's phone must not buzz daily for
        # this. Deletion below removes it from the chat entirely.
        payload["disable_notification"] = True

        try:
            response = requests.post(
                self.API_URL.format(token=self.token),
                json=payload,
                timeout=self.HTTP_TIMEOUT_S,
            )
        except Exception as exc:  # noqa: BLE001
            return ProbeResult(False, "transport", self._redact(exc))

        body = self._json_body(response)
        if response.status_code >= 400 or not body.get("ok"):
            # Telegram reports refusals as HTTP 4xx with a `description`
            # ("Unauthorized", "chat not found", "bot was blocked by the
            # user"). Carry the description through — it names the repair.
            detail = body.get("description") or f"HTTP {response.status_code}"
            return ProbeResult(False, "api", self._redact(detail))

        result = body.get("result")
        message_id = result.get("message_id") if isinstance(result, dict) else None
        if message_id is None:
            # Accepted but unidentifiable. The send worked, so the channel is
            # proved; we simply cannot clean up after ourselves.
            return ProbeResult(
                True,
                "delivered",
                "Telegram accepted the probe but returned no message_id",
                residue=True,
            )

        deleted, delete_detail = self._delete_message(message_id)
        return ProbeResult(True, "delivered", delete_detail, residue=not deleted)

    def _delete_message(self, message_id: int) -> tuple[bool, str]:
        """Best-effort removal of a message this bot sent. Never raises."""
        try:
            response = requests.post(
                self._api_url("deleteMessage"),
                json={"chat_id": self.chat_id, "message_id": message_id},
                timeout=self.HTTP_TIMEOUT_S,
            )
        except Exception as exc:  # noqa: BLE001
            return False, f"delete failed: {self._redact(exc)}"
        body = self._json_body(response)
        if response.status_code >= 400 or not body.get("ok"):
            detail = body.get("description") or f"HTTP {response.status_code}"
            return False, f"delete refused: {self._redact(detail)}"
        return True, ""

    def send_document(
        self, csv_bytes: bytes, filename: str, caption: str = "",
        kind: str = "document", run_id: str | None = None,
    ) -> bool:
        """Send a file (e.g. CSV) via Telegram sendDocument. Best-effort.

        Recorded the same way `send()` is (see `_record_send`): the
        document's bytes are never stored (they are the P&L CSV, not
        text meant for the "what did we tell the owner" question), only
        `caption` — the text that actually appears in the chat next to
        it — plus `filename` so the record still says what went out.
        """
        if not self.enabled:
            return False
        recorded_text = f"[document: {filename}] {caption}".strip()
        try:
            response = requests.post(
                f"https://api.telegram.org/bot{self.token}/sendDocument",
                data={"chat_id": self.chat_id, "caption": caption},
                files={"document": (filename, csv_bytes, "text/csv")},
                timeout=30.0,
            )
            response.raise_for_status()
            self._safe_record_send(kind=kind, status="sent", text=recorded_text, run_id=run_id)
            return True
        except Exception as exc:
            logger.warning("Telegram send_document failed: %s", self._redact(exc))
            self._safe_record_send(
                kind=kind, status="failed", text=recorded_text,
                detail=self._redact(exc), run_id=run_id,
            )
            return False


# === Out-of-band owner alert ===

#: The P&L stand-in every standalone owner alert carries directly under its
#: heading. Owner, 2026-09-18, verbatim: "all the P&L information has to go
#: at the very top of every telegram alert, right after the first line,
#: which is really the heading."
#:
#: A standalone alert genuinely CANNOT carry a figure. It fires the instant
#: a problem is found \u2014 from the credential check, the stop-coverage audit,
#: a reconciliation mismatch \u2014 on paths that have done no account read, and
#: a page about a naked position must never block on a broker round-trip or
#: be able to fail inside one. So the line says exactly that, in one
#: sentence, rather than being dropped (an absent block reads as a broken
#: one) or filled with a fabricated zero.
_ALERT_NO_PNL_LINE = (
    "\U0001f4c8 P&L: not available in this alert \u2014 it is sent the moment a "
    "problem is found, before any account is read."
)


def _with_pnl_header(text: str) -> str:
    """Insert the P&L block directly under an alert's heading line.

    Enforced HERE, in the one funnel every standalone alert already goes
    through, rather than in each of the eighteen callers that build one.
    The rule has been restated by the owner more than once and drifts every
    time it depends on the next author remembering it; a single choke point
    is the only version of it that holds.

    Never raises \u2014 an alerting bug must not be able to break the thing it
    reports on. On any fault the original text goes out unchanged.
    """
    try:
        if _ALERT_NO_PNL_LINE in text:
            return text
        heading, sep, rest = text.partition("\n")
        if not sep:
            return f"{heading}\n{_ALERT_NO_PNL_LINE}"
        return f"{heading}\n{_ALERT_NO_PNL_LINE}\n{rest}"
    except Exception:  # noqa: BLE001
        logger.exception("could not attach the P&L line to an owner alert")
        return text


def send_owner_alert(text: str, *, symbols: list[str] | None = None) -> bool:
    """Push an alert to the owner NOW, outside the session-result message.

    Spec §11.1 guard 2. Some conditions cannot wait for a session to finish
    and be summarised: a position that is open at the broker with no
    protective stop on it is the canonical one. The end-of-session Telegram
    message is the wrong vehicle — an `intra_check` tick is silent unless it
    liquidates, so a naked position found at 12:30 would produce no message
    at all, and a session that crashes after the failure never sends one.

    Deliberately mirrors `src/cost_circuit.py`'s escalation shape, which is
    this desk's established owner-alert path: log at CRITICAL first so a
    Telegram outage cannot hide the event from the journal or Mission
    Control, then send. Returns whether the send succeeded; callers treat
    that as information, never as a reason to abort.

    Never raises. An alerting bug must not be able to break the trading path
    it is reporting on — see `alert_watchdog`'s "a watchdog that can break
    the thing it watches is worse than no watchdog".
    """
    if not text:
        return False
    text = _with_pnl_header(text)
    logger.critical("OWNER ALERT\n%s", text)
    try:
        return bool(TelegramNotifier().send(text, symbols=symbols, kind="owner_alert"))
    except Exception:  # noqa: BLE001
        logger.exception("owner alert delivery failed")
        return False


# === Data-quality alert (own message, not bundled) ===
#
# Before this existed, a bad analyst seat (`data_status` anything that
# `evidence_gate.counts_as_degraded` — failed, truncated, parse_error,
# partial, …) only ever showed up as one line INSIDE the routine
# session-result message (see `_append_trade_session_body`'s "degraded:"
# line below). Same-session reuse (`carried_from_morning`) and an
# intentional skip (`not_run_intraday`) are not in that set: they are
# usable, not a lost seat. That is exactly what the owner's alert-design
# rule forbids: "alerts get their OWN
# Telegram message, never bundled into a run summary." A bundled line is
# easy to miss inside a normal-looking "session OK" message, and this
# desk's whole thesis depends on the analysts' data being trustworthy — see
# docs/OUTCOME.md. This fires a SEPARATE, standalone alert through the same
# `send_owner_alert` path already used for a naked position with no stop.
#
# Severity is carried in TEXT, never colour, per the owner's alert-design
# rule — no emoji standing in as the only signal here.
#
# Deliberately NOT deduplicated: if the same seat is still broken next run,
# it alerts again. A repeated alert on a genuinely unresolved problem is
# correct, not noise — silence is what let this go unnoticed before.
#
# `"low_confidence"` (tech, 2026-09-04) is deliberately excluded from this
# page for TECH SPECIFICALLY, even though it is not "ok" — a tech batch
# that resolved every symbol but had the model itself flag one read as
# low-conviction is a real, different thing from a seat that failed or went
# silent, and paging the owner identically for both would train him to
# ignore this alert. It still reaches `_append_trade_session_body`'s
# bundled "degraded:" line below and still counts toward `RiskStage`'s
# ">= 2 degraded sources" `data_degraded` advisory (`src/pipeline_stages.py`,
# class `RiskStage`) — both softer, non-paging responses that fit a
# low-confidence-but-present read better than a standalone alert does.
#
# This exclusion is PER-SEAT, not a bare string match, on purpose: news
# and macro also use the literal value `"low_confidence"` (same day, same
# convention), but for those two seats it means the WHOLE report/analysis
# is low-confidence, not one symbol out of many resolved — a materially
# worse situation than tech's per-symbol case, and one that SHOULD still
# page. A flat `"low_confidence" not in (...)` check would have silently
# suppressed those two seats' real alerts as a side effect of tech's own,
# narrower exception — caught before merge, not after.
_ALERT_EXEMPT_PER_SEAT: dict[str, set[str]] = {
    "tech": {"low_confidence"},
}
#
# 2026-09-04: `data_status["macro"]` gained the same "low_confidence" value
# — a technically clean call (coverage fine, parsed fine) whose own
# self-reported `MacroAnalysis.confidence` came back "low" (see
# pipeline_stages.py's macro branch). Deliberately NOT added to the
# exemption dict above: a critical seat's own stated self-doubt belongs in
# the SAME "do not trust this session blind" alert as a coverage failure,
# not a quieter side channel. Considered and rejected suppressing it to
# avoid alert fatigue: the seat's OTHER known noisy self-check
# (`regime_shift`'s stale-data gate, which fired on ~52% of runs) was driven
# by a `staleness_days<=1` bar that real FRED lag could never reach, so
# "low" was not expected to duplicate it. That day-count gate is GONE as of
# 2026-09-11 — both macro gates now test whether the held reading is the
# latest FRED has published and whether a newer print is overdue, never its
# age (`src/data/macro.py::SeriesFreshness`). An overdue print gets its own
# `data_status["macro"] = "release_overdue"` value and pages through this
# same alert, which is intended: an overdue macro release is a real
# publication or fetch failure, not normal cadence.


# Board item 89 clarity defect — "a 'data degraded' warning that names
# internal components". `data_status` is keyed by the desk's internal seat
# names and valued with internal state tokens. Both maps below say what
# each one MEANS in the words a person would use; the key never reaches
# the message. An unmapped seat or value is DESCRIBED and its raw text is
# labelled as kept-for-the-record, never paraphrased into a claim.
#
# "smart_money" is deliberately NOT a fixed string here. Congressional
# trading disclosures (`src/data/congressional_trading.py`) are gated by
# `config.smart_money.congress_enabled`, switched ON 2026-09-20 per owner
# ruling (see `docs/INCIDENT_HISTORY.md`'s 2026-09-04 and 2026-09-20
# entries). Naming "congressional" in this label when that switch is off
# would tell the owner the desk reads a feed it never actually reads.
# `_smart_money_seat_label` below reads the real switch at call time, so
# the wording can never drift from what the running desk actually does.
_SEAT_WORDS: dict[str, str] = {
    "macro": "the market-backdrop research",
    "tech": "the chart research",
    "news": "the news research",
    "earnings": "the earnings-filing research",
    "sector": "the sector research",
}


def _congress_enabled_now() -> bool:
    """Whether `config.smart_money.congress_enabled` is on right now.

    Read directly from `config/settings.yaml` (the one key, falling back to
    the pydantic field default) instead of being threaded through as a
    parameter: this module renders owner-facing text for dozens of call
    sites (Telegram alerts, the intraday tick, stored-run replays) that do
    not otherwise carry a config object, and several read stored historical
    run data with no config in scope at all. Any failure to read it
    (missing file in a test environment, credential delivery issues, bad
    yaml) conservatively assumes the switch is off, `False`, regardless of
    the field's own live default — a wording helper must never raise or
    break an alert, and must never claim a feed is running when it could
    not actually confirm the setting.
    """
    # Reads only the one key, NOT through `load_config`: that also collects
    # the systemd-delivered broker credentials, which a wording helper has
    # no business touching on every alert it renders.
    try:
        import yaml

        from src.config import SmartMoneyConfig

        settings_path = Path(__file__).resolve().parent.parent / "config" / "settings.yaml"
        with open(settings_path) as f:
            raw = yaml.safe_load(f) or {}
        section = raw.get("smart_money") or {}
        if "congress_enabled" in section:
            return bool(section["congress_enabled"])
        return bool(SmartMoneyConfig.model_fields["congress_enabled"].default)
    except Exception:
        return False


def _smart_money_seat_label(congress_enabled: bool) -> str:
    """The smart-money seat's plain name, true to what it actually reads."""
    if congress_enabled:
        return "the insider-and-congressional-trading feed"
    return "the insider-trading feed"

_DATA_STATUS_WORDS: dict[str, str] = {
    "failed": "did not return an answer",
    "partial": "returned only part of an answer",
    "parse_error": "returned an answer the desk could not read",
    "truncated": "was cut off before it finished",
    "empty": "returned nothing",
    "low_confidence": "rated its own answer low-confidence",
    "provider_error": "could not reach its data provider",
    "release_overdue": "is waiting on a scheduled data release that is overdue",
    "symbol_dropped": "dropped at least one symbol from its answer",
    "degraded": "returned a degraded answer",
    # The four remaining CATEGORY_LOST states in src/evidence_gate.py had no
    # plain wording, so an evidence-gate skip naming one of them showed the
    # owner the raw token instead. Each phrase below is read straight off
    # that module's own comment for the state — not a guess at what it might
    # mean.
    "expired": (
        "had only an out-of-date answer, and this check did not fetch a "
        "fresh one"
    ),
    "content_missing": "answered, but the answer had no content in it",
    "carry_forward_empty": (
        "had nothing to carry forward from this morning — the morning never "
        "wrote an answer for today"
    ),
    "carry_forward_failed": (
        "could not be carried forward from this morning — the lookup itself "
        "failed"
    ),
}


def seat_words(seat: Any) -> str:
    """Plain words for one research seat's internal name."""
    key = str(seat or "").strip().lower()
    if key == "smart_money":
        return _smart_money_seat_label(_congress_enabled_now())
    return _SEAT_WORDS.get(key) or (
        f"a research seat the desk has no plain name for (recorded as: {key or 'blank'})"
    )


def describe_data_status(bad: dict) -> list[str]:
    """One plain sentence per degraded seat — "the chart research did not
    return an answer" — from a `{seat: state}` map. A state with no plain
    wording is described as such, with the raw token kept for the record,
    so nothing is ever guessed at on the owner's behalf."""
    lines: list[str] = []
    for seat, state in sorted((bad or {}).items()):
        token = str(state or "").strip().lower()
        words = _DATA_STATUS_WORDS.get(token)
        if words:
            lines.append(f"{seat_words(seat)} {words}")
        else:
            lines.append(
                f"{seat_words(seat)} reported a state the desk has no plain "
                f"wording for (kept for the record: {token or 'blank'})"
            )
    return lines


def _seat_list_words(seats: Any) -> str:
    """"the chart research and the news research" — never internal keys."""
    words = [seat_words(seat) for seat in (seats or []) if str(seat).strip()]
    if not words:
        return ""
    if len(words) == 1:
        return words[0]
    return ", ".join(words[:-1]) + " and " + words[-1]


def describe_evidence_freshness(freshness: Any) -> list[str]:
    """How much of this decision's evidence was read on THIS tick, in words.

    Owner mandate 2026-09-18 made every seat but the chart research
    advisory. That means a decision can now rest on ONE freshly-read seat
    plus a book carried over from the morning, and every one of those
    carried seats reports green. Nothing anywhere said so. This says so.

    It is DISCLOSURE, not a threshold: it states a count, it never judges
    one. No minimum number of fresh seats exists in this desk and none may
    be invented here — that number is the owner's (docs/WORK.md item 20).

    Takes the dict produced by `evidence_gate.EvidenceFreshness.to_evidence`
    and returns [] for anything it cannot read, so a missing or malformed
    record costs the disclosure line and never the message.
    """
    if not isinstance(freshness, dict):
        return []
    fresh = [s for s in (freshness.get("fresh_seats") or []) if str(s).strip()]
    carried = [s for s in (freshness.get("carried_seats") or []) if str(s).strip()]
    absent = [s for s in (freshness.get("absent_seats") or []) if str(s).strip()]
    unknown = [
        s for s in (freshness.get("unknown_freshness_seats") or [])
        if str(s).strip()
    ]
    stale = [
        s for s in (freshness.get("known_out_of_date_seats") or [])
        if str(s).strip()
    ]
    total = len(fresh) + len(carried) + len(absent) + len(unknown)
    if not total:
        return []
    lines = [
        f"<b>HOW FRESH THIS DECISION'S EVIDENCE WAS</b> "
        f"({len(fresh)} of {total} research seats read just now)"
    ]
    if fresh:
        lines.append(f"   • read just now: {_seat_list_words(fresh)}")
    else:
        lines.append("   • read just now: none of them")
    if carried:
        lines.append(
            f"   • carried over from earlier, not re-read: "
            f"{_seat_list_words(carried)}"
        )
    if stale:
        lines.append(
            f"   • already known to be out of date: {_seat_list_words(stale)}"
        )
    if absent:
        lines.append(f"   • no answer at all: {_seat_list_words(absent)}")
    if unknown:
        lines.append(
            f"   • state the desk cannot classify, so not counted as read: "
            f"{_seat_list_words(unknown)}"
        )
    return lines


def describe_universe_changes(block: Any) -> list[str]:
    """The owner-facing account of what the universe screen changed.

    Owner design 2026-09-01: "the owner must never discover the universe
    changed by accident" — every addition, flag and removal since the last
    morning message, in plain words. Removals, flags and held names kept
    past a failed check get one line EACH with the reason (they are the ones
    that matter and are few); additions are one line of names, clipped,
    because the first weeks can add hundreds. Silent only when the screen
    is off (no block). With it on and nothing changed, it says so.
    """
    if not isinstance(block, dict):
        return []
    from src.universe_screen import describe_event, plain_reasons

    events = [e for e in (block.get("events") or []) if isinstance(e, dict)]
    admitted = block.get("admitted_count")
    flagged = block.get("flagged_count")
    size = (
        f" \u2014 {admitted} screened stock(s) on the list, {flagged} flagged"
        if isinstance(admitted, int) and isinstance(flagged, int) else ""
    )
    if not events:
        return [f"\U0001f50e Stock list: no changes since the last morning{size}"]
    out = [f"\U0001f50e Stock list changed: {len(events)} change(s){size}"]
    grouped = {
        "added": "Added {n} (passed every check): ",
        "cleared": "Flag cleared on {n} (passing again): ",
    }
    for action, label in grouped.items():
        names = [str(e.get("symbol", "?")) for e in events if e.get("action") == action]
        if names:
            out.append(_clip_text(
                "\u2022 " + label.format(n=len(names)) + ", ".join(names), 600,
            ))
    flagged_events = [e for e in events if e.get("action") == "flagged"]
    if flagged_events:
        out.append(_clip_text(
            f"\u2022 Flagged {len(flagged_events)} (removed if they fail again "
            "next week): " + "; ".join(
                "{} ({})".format(
                    e.get("symbol", "?"), plain_reasons(e.get("reasons") or []),
                )
                for e in flagged_events
            ), 600,
        ))
    for event in events:
        if event.get("action") in ("removed", "removal_deferred_held"):
            out.append(_clip_text(f"\u2022 {describe_event(event)}", 300))
    return out


def _append_universe_changes(lines: list[str], result: dict) -> None:
    if not isinstance(result, dict):
        return
    block = describe_universe_changes(result.get("universe_changes"))
    if block:
        _new_section(lines, *block)


def _append_evidence_freshness(lines: list[str], result: dict) -> None:
    """Put the freshness disclosure into a session message body."""
    if not isinstance(result, dict):
        return
    block = describe_evidence_freshness(result.get("evidence_freshness"))
    if block:
        _new_section(lines, *block)


def describe_skipped_decision(
    lost: Any, data_status: Any, *, include_next_pass: bool = True,
) -> list[str]:
    """The owner-facing account of an evidence-gate skip: a bold title line
    that states the conclusion, then one short bullet per idea.

    The single source of this wording, used by BOTH owner-facing renderers
    of the same event (the standalone alert in
    `pipeline._evidence_gate_skip` and the intraday tick banner in
    src/trader_feed.py), so the two can never drift apart again.

    What it deliberately does NOT do is show `EvidenceVerdict.reason`. That
    string is the durable machine record — it stays exactly as it is in the
    database, the event rows and the log, where it is correct — but it
    carries a source-file reference, an internal seat key, a raw state
    token and "N seat(s)", none of which mean anything on a phone. Every
    fact in it is said here in words instead, via `describe_data_status`,
    which describes an unmapped token rather than guessing at it.
    """
    seats = [str(seat) for seat in (lost or []) if seat]
    status = data_status if isinstance(data_status, dict) else {}
    bad = {seat: status.get(seat) for seat in seats}
    lines = ["<b>DECISION SKIPPED — NOTHING WAS TRADED</b>"]
    detail = describe_data_status(bad) if bad else []
    if not detail:
        detail = ["a research seat the desk needed did not return an answer"]
    lines += [f"   • {sentence}" for sentence in detail]
    lines += [
        "   • the desk declined to decide rather than guess",
        "   • no Portfolio Manager call was paid for",
        "   • every position keeps the stop it already had",
    ]
    if include_next_pass:
        lines.append(
            "   • the next scheduled decision tries again — nothing for you to do"
        )
    return lines


def maybe_alert_data_quality(result: dict | None, *, mode: str) -> bool:
    """Fire a standalone alert when any agent's data this session was not
    clean, so a bad analyst seat can never hide inside an otherwise-normal
    run summary. Returns whether an alert was sent.

    Tech's `low_confidence` is excluded on purpose — see the module comment
    above for why a single self-reported low-conviction read on ONE symbol
    shouldn't page the owner the same way a failed or silent seat does.
    Other seats' `low_confidence` (a whole-report signal, not per-symbol)
    is NOT exempt and pages normally.
    """
    if not isinstance(result, dict):
        return False
    data_status = result.get("data_status") or {}
    if not isinstance(data_status, dict):
        return False
    from src import evidence_gate
    bad = {
        k: v for k, v in data_status.items()
        if evidence_gate.counts_as_degraded(v)
        and v not in _ALERT_EXEMPT_PER_SEAT.get(k, ())
    }
    if not bad:
        return False
    # Board item 89 clarity defect: this line used to read "macro=failed,
    # tech=partial" — internal seat names and state tokens. Same facts, in
    # words; the raw pair is kept beneath, labelled, for anyone debugging.
    from src.trading_calendar import et_now

    when = fmt_time_12h(et_now())
    detail = "\n".join(f"  • {line}" for line in describe_data_status(bad))
    raw = ", ".join(f"{k}={v}" for k, v in sorted(bad.items()))
    # Board item 89's run-identifier removal landed on the evening message
    # only; this alert still carried one. A run id is a database key, not
    # something the owner can act on — it stays in the log line above and in
    # every stored row, and leaves the message.
    text = (
        f"DATA QUALITY ALERT — the {mode} session at {when} ran on "
        f"incomplete research\n"
        f"{detail}\n"
        "WHAT THIS MEANS FOR YOU: the Portfolio Manager and the Risk "
        "Manager may have sized or decided this session on incomplete or "
        "unreadable input from the seats above. Nothing was undone; read "
        "this session's decisions with that in mind, and check Mission "
        "Control if one of them looks wrong.\n"
        f"Machine record, kept for the log — nothing here needs anything "
        f"from you: {raw}"
    )
    return send_owner_alert(text)


# === Fill-confirmation alerts (own message, not bundled) ===
#
# 2026-09-17. The desk confirms a fill by polling the broker over REST
# inside a bounded window. That path is now the ONLY fill-confirmation
# mechanism: `execution.fill_stream_enabled` is off, because the
# `trade_updates` websocket has never once authenticated on this host (see
# that flag for the two confirmed blockers). Nothing about that is an
# incident — it is the intended configuration.
#
# What WAS silent to the owner is the case that actually matters: the REST
# window ending without the broker having confirmed what happened to an
# order, or the half-hourly reconciliation finding the desk's own record
# and the broker's record disagreeing with no sale to explain the gap.
# Before these two alerts, both only ever reached a log line the owner
# never reads — the same failure shape as the 2026-08-28 ONDS/CCJ
# stop-outs, which sat silent for a full trading day.
#
# WHAT MUST NEVER PAGE: the websocket being off. That is the configuration,
# not a fault, and alerting on it would move ~150 daily error lines out of
# the log and into Telegram, which is worse. Neither condition below looks
# at the socket at all — both are true or false identically with the flag
# on or off.
#
# Standard owner alert-design rules (2026-09-02), same as the data-quality
# alert above: its OWN Telegram message, never a line in a run summary;
# severity carried in TEXT, never colour; NOT deduplicated, so a still-
# broken thing keeps alerting. Plain English, no jargon, no run ids, and
# any time is 12-hour with AM/PM and the timezone (`fmt_time_12h`).
#
# Neither function raises. An alerting bug must not break the execution or
# reconciliation path it reports on.


def alert_order_outcome_unconfirmed(
    symbol: str, order_id: str, waited_seconds: float,
    last_status: str | None = None,
) -> bool:
    """PAGE: the desk could not confirm what happened to a live order.

    Fires when the bounded confirmation window closed — AND the follow-up
    cancel-and-recheck also closed — with the order still not in a terminal
    state. The desk therefore does not know whether it bought anything.

    `waited_seconds` is the window the caller actually used
    (`_ENTRY_FILL_TIMEOUT_S`), passed in rather than restated here: this
    alert introduces no threshold of its own.

    Does NOT fire on an order that filled, partially filled, was cancelled
    cleanly, expired or was rejected. Every one of those is a KNOWN
    outcome, and three of them already have their own reporting.
    """
    try:
        from src.trading_calendar import et_now
        when = fmt_time_12h(et_now())
        sym = str(symbol or "").strip() or "an order"
        whole = int(waited_seconds) if waited_seconds else 0
        body = (
            "ORDER OUTCOME NOT CONFIRMED — the desk does not know whether "
            "this trade happened\n"
            f"{sym}: the desk sent an order to the broker and waited the "
            f"full {whole} seconds it allows, then cancelled it and waited "
            "again. The broker never confirmed the result either time. The "
            f"last thing it said was \"{(last_status or 'nothing at all')}\".\n"
            "\n"
            "WHAT THIS MEANS FOR YOU: this may have bought nothing, or it "
            f"may have bought {sym} shares that have no protective stop on "
            "them yet. The desk is assuming nothing was bought, which is "
            "the safe assumption but may be wrong. It did not invent a "
            "position or a price to cover the gap.\n"
            f"WHAT TO CHECK: your broker account's {sym} position and its "
            "order list, as of " + when + ". If shares are there, the desk "
            "re-checks stop coverage at the start of every scheduled check "
            "during market hours and will place a stop on anything it finds "
            "unprotected — but confirm it did."
        )
        return send_owner_alert(body, symbols=[sym])
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "unconfirmed-order alert for %s could not be sent: %s", symbol, exc,
        )
        return False


def alert_records_disagree_with_broker(
    symbol: str, desk_qty: float, broker_qty: float, lookback_days: int,
) -> bool:
    """PAGE: reconciliation found the desk's record and the broker's disagreeing.

    Fires on the `stop_out_gap_unexplained` outcome only: the desk believes
    it holds more shares than the broker shows, AND no untracked sale in the
    broker's own recent order history explains the difference. That is the
    case where neither record can be trusted and nothing can be written
    back without guessing.

    Deliberately NOT fired for `stop_out_pnl_unmatched`, which is a
    different thing: there the broker and the desk agree on what was sold,
    and only the desk's own older buy history is too thin to compute the
    profit. That needs review, not a page — it is not a live position
    mismatch.

    `lookback_days` is the reconciler's own configured search window
    (`ReconciliationConfig.stop_out_lookback_days`), passed in for the same
    reason as above: no threshold is invented here.
    """
    try:
        from src.trading_calendar import et_now
        when = fmt_time_12h(et_now())
        sym = str(symbol or "").strip() or "a position"
        body = (
            "RECORDS DISAGREE — the desk's records and the broker's do not "
            "match, and the desk cannot tell which is right\n"
            f"{sym}: the desk's own records say it holds "
            f"{_fmt_qty(desk_qty)} share(s). The broker shows "
            f"{_fmt_qty(broker_qty)}. The desk searched the broker's order "
            f"history for the last {int(lookback_days)} day(s) for a sale "
            "that would explain the difference and found none.\n"
            "\n"
            "WHAT THIS MEANS FOR YOU: one of the two is wrong. Until this "
            f"is resolved, treat the desk's profit-and-loss figures for "
            f"{sym} as unreliable. Nothing was changed, written back or "
            "estimated to make the two numbers agree — the desk stopped "
            "rather than guess.\n"
            f"WHAT TO CHECK: your broker account's {sym} position and order "
            "history, as of " + when + ". The likely causes are a sale the "
            "desk placed but never recorded, or a change you made in the "
            "broker account yourself."
        )
        return send_owner_alert(body, symbols=[sym])
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "records-disagree alert for %s could not be sent: %s", symbol, exc,
        )
        return False


# === Session result formatting ===
# Built as a free function (not a TelegramNotifier method) so it's
# easy to unit-test without the network stub and so main.py can
# compute the message before deciding to send.

def _pnl_lines_for(result: dict | None, mode: str = "") -> list[str]:
    """`trader_feed._pnl_section_lines` for whatever this message knows.

    One renderer for the owner's P&L block across BOTH message modules, so
    the figure and its wording can never differ between two messages sent
    minutes apart. Never raises: a P&L-rendering fault must not be able to
    stop the message it leads \u2014 it degrades to the same honest
    "not available" wording the normal path uses for a missing figure.
    """
    if mode == "evening" and isinstance(result, dict):
        # Evening has its own, richer and 4pm-close-correct block — see
        # `_evening_pnl_block`. The shared renderer would show the
        # real-time (after-hours-contaminated) figure instead.
        try:
            return _evening_pnl_block(result)
        except Exception as exc:  # noqa: BLE001
            logger.warning("evening P&L block could not be rendered: %s", exc)
    try:
        from src.trader_feed import _pnl_section_lines
        return _pnl_section_lines(result if isinstance(result, dict) else {})
    except Exception as exc:  # noqa: BLE001
        logger.warning("P&L block could not be rendered: %s", exc)
        return [
            "\U0001f4c8 Today's P&L: not available",
            "   The figure could not be read while this message was built.",
        ]


def format_session_result(
    mode: str,
    result: dict | None,
    elapsed_seconds: float,
    error: BaseException | None = None,
) -> str | None:
    """Build the human-readable message body for one completed (or
    failed) session.

    Returns None when the session shouldn't generate a notification
    per the per-mode noise policy (intra_check OK,
    earnings_preprocess nothing_new, meta skipped). Caller treats
    None as "do nothing".
    """
    from src.trading_calendar import et_now

    _now = et_now()
    timestamp = f"{_now.strftime('%Y-%m-%d')} {fmt_time_12h(_now)}"
    elapsed_str = _fmt_elapsed(elapsed_seconds)

    if error is not None:
        # Errors always notify — operator wants to see crashes loudly.
        err_type = type(error).__name__
        # 1500 chars, not the old 500 — a Python traceback's exception
        # message (chained cause, validation error detail) routinely runs
        # long, and this is a single line in a 4000-char budget; see
        # _clip_text for why it clips on a boundary instead of mid-word.
        err_msg = _clip_text(str(error), 1500) or "(no message)"
        return (
            f"\U0001f6d1 FAILED: {mode_label(mode)} did not finish  "
            f"({timestamp})\n"
            f"error: {err_type}: {err_msg}\n"
            f"\U0001f9fe took {elapsed_str}"
        )

    if not isinstance(result, dict):
        return (
            f"\u26aa {mode_label(mode)} finished but reported nothing the "
            f"desk could read ({timestamp})\n"
            f"\U0001f9fe took {elapsed_str}"
        )

    status = str(result.get("status", "unknown"))

    # === Per-mode noise policy ===
    if mode == "intra_check" and status in ("ok", "market_holiday"):
        # Silent — would otherwise be 14 pings/day. UNLESS the 30-minute
        # sweep found a stop-coverage gap (spec §11.1 guard 3): "no
        # deterministic breach fired" is not the same as "nothing is wrong",
        # and an unprotected position found at 12:30 was previously reported
        # to a log file and nowhere else, because this is the one mode whose
        # normal tick sends no message.
        #
        # Spec §11.1 hybrid fractional stops: a gap the sweep itself already
        # closed ('fractional_replaced'), or one the design expects
        # ('fractional_overnight'), is NOT a reason to break that silence.
        # This mode ticks 14 times a day; if the routine re-placement of a
        # sub-share DAY stop pinged the owner, the fractional feature would
        # turn a deliberately-quiet channel into a noisy one, and the guard
        # that is supposed to interrupt him would arrive as ping 15.
        if not _actionable_coverage_gaps(result.get("stop_coverage_gaps")):
            return None
    if mode == "earnings_preprocess" and status in (
        "market_holiday", "nothing_new", "fetch_error",
    ):
        # nothing_new is the common case (most pre-market days have
        # no fresh 10-Q to analyze). fetch_error suppresses occasional
        # SEC transients. analysis_error still notifies (real LLM bug).
        if status == "fetch_error":
            return None
        if status == "nothing_new":
            return None
        if status == "market_holiday":
            return None
    if mode == "meta" and status == "skipped":
        return None  # quarter-end check fires daily; silent on non-Q-end
    if mode == "daily" and status == "sent":
        # The CSV document push (with its self-describing caption) IS
        # the delivery confirmation — a second status text every weekday
        # would be pure noise. error / skipped still notify below.
        return None

    run_id = result.get("run_id", "?")
    emoji = _status_emoji(status)
    # Colour-blind-safe (item 21b): a 🛑 circle and a 🟢/🟡/⚪ circle must not
    # differ by hue alone. The shape itself (🛑 vs the others) already
    # breaks that tie, but the header's first word states it in text too,
    # so the line is still correct with zero emoji rendering — "status:
    # {status}" a line below is not the FIRST word of the message.
    severity_prefix = "FAILED: " if emoji == "\U0001f6d1" else ""
    # The outcome goes IN the title line, in words. Two lines that used to
    # sit under it are gone (owner review, 2026-09-18, extending PR #471's
    # ratified evening standard to every message):
    #   - "run_id: earnings_preprocess-a745ceda" \u2014 a run identifier
    #     means nothing to him and he does not need it. Board item 89
    #     clarity defect "run identifiers", previously fixed in the evening
    #     message only. `run_id` is still read below for the cost lookup;
    #     it is simply never shown.
    #   - "status: Preprocessed" \u2014 an internal status code with its
    #     underscores taken out is still an internal status code. Board
    #     item 89 clarity defect "internal status codes shown as-is".
    lines: list[str] = [
        f"{emoji} {severity_prefix}{mode_label(mode)} \u2014 "
        f"{humanize_status(status)}  ({timestamp})",
    ]

    # P&L FIRST, directly under the heading \u2014 owner, 2026-09-18, verbatim:
    # "all the P&L information has to go at the very top of every telegram
    # alert, right after the first line, which is really the heading." A
    # REPEAT correction: it kept drifting below whatever block was added
    # next, so the tests that go with this change assert the POSITION, not
    # the presence.
    #
    # Rendered by the same `trader_feed._pnl_section_lines` every other
    # message uses, so two messages can never state his P&L differently.
    # Imported lazily because `trader_feed` imports this module. A mode
    # that carries no account figures (the pre-open filing reader, a crash
    # report) renders "not available" plus one sentence saying why \u2014 never
    # a dropped block and never a fabricated zero.
    _new_section(lines, *_pnl_lines_for(result, mode))

    # Per-session LLM cost (looked up from agent_logs by run_id), the
    # day-to-date spend, and (morning/once only) the prepaid balance and
    # margin-interest lines — one "cost info" section, kept tight against
    # itself and separated from the header above and the body below.
    #
    # Per-session cost: shows for every mode that ran agents — operator
    # wants to see the dollar spend alongside the orders. Returns None
    # silently if no DB or no rows; the line is omitted rather than render
    # "$?.??" mid success-message noise.
    #
    # Day-to-date: shown on every session that spent money, because that is
    # when "how close am I" to the self-imposed brake is actually being
    # asked. Distinct from the balance line below, which is real money.
    #
    # OpenRouter balance (morning/once only): owner request 2026-08-31 — he
    # wants to see the balance falling rather than discover it empty.
    # OpenRouter is PREPAID; on 2026-08-31 the account was down to $7.10,
    # about seven clean trading days, and nothing in the system said so.
    # Morning-only because it changes slowly and repeating it on every
    # session would train the operator to skim past it.
    cost_block: list[str] = []
    cost_line = _session_cost_line(run_id)
    if cost_line:
        cost_block.append(cost_line)
    day_line = _day_cost_line()
    if day_line and cost_line:
        cost_block.append(day_line)
    if mode in ("morning", "once"):
        balance_line = _openrouter_balance_line()
        if balance_line:
            cost_block.append(balance_line)
    _new_section(lines, *cost_block)

    # Margin interest gets its OWN section, not a berth in the cost block
    # above (owner, 2026-09-18). It sat there since 2026-09-01 and he never
    # found it: model spend and the prepaid OpenRouter balance are what it
    # costs to RUN the desk, while this is the price of money the desk
    # borrowed — a different kind of number, and filing it under running
    # costs is what made it invisible.
    if mode in ("morning", "once"):
        _new_section(lines, *_margin_interest_lines())

    # === Mode-specific body ===
    if mode in ("morning", "midday", "close", "once"):
        _new_block(lines, _append_trade_session_body, result)
        _append_evidence_freshness(lines, result)
        if mode in ("morning", "once"):
            _append_universe_changes(lines, result)
    elif mode == "evening":
        _new_block(lines, _append_evening_body, result)
    elif mode == "earnings_preprocess":
        _new_block(lines, _append_earnings_body, result)
    elif mode == "intra_check":
        _new_block(lines, _append_intra_check_body, result)
        _append_evidence_freshness(lines, result)
    elif mode == "meta":
        _new_block(lines, _append_meta_body, result)
    elif mode == "daily":
        # Only error / skipped reach here ("sent" is silenced above).
        # Surface the failure reason — a bare '🛑 FAILED: status error' is
        # undebuggable from a phone.
        daily_block: list[str] = []
        filename = result.get("filename", "")
        if filename:
            daily_block.append(f"📊 {result.get('rows', '?')} rows → {filename}")
        err = result.get("error")
        if err:
            daily_block.append(f"error: {err}")
        _new_section(lines, *daily_block)

    # "elapsed: 3m 5s" \u2014 a raw label the owner called noise. Kept,
    # because a session that suddenly takes four times as long is worth
    # seeing, but as the evening message already renders it.
    _new_section(lines, f"\U0001f9fe took {elapsed_str}")
    return "\n".join(lines)


def _actionable_coverage_gaps(gaps) -> list[dict]:
    """The subset of coverage gaps that a human has to do something about.

    Spec §11.1 hybrid fractional stops. Filters out the two states the hybrid
    design produces on purpose (see `_gap_is_expected_fractional`) so callers
    that decide whether to speak at all — chiefly `intra_check`'s silence
    policy — key off real faults rather than off the daily heartbeat of a
    sub-share DAY stop lapsing and being re-placed.
    """
    if not isinstance(gaps, list):
        return []
    return [
        g for g in gaps
        if isinstance(g, dict) and not _gap_is_expected_fractional(g)
    ]


def _gap_is_uncovered(gap: dict) -> bool:
    """Spec §11.1 guard 3 — is this coverage gap "NO STOP AT ALL"?

    `_reconcile_stop_coverage` stamps `coverage` ('none' | 'partial') and
    that is the authority when present. Derived from `covered_qty` when it
    is absent, so a gap dict from an older run snapshot (or any caller that
    predates the field) is still classified rather than silently demoted to
    the milder banner.
    """
    coverage = gap.get("coverage")
    if coverage:
        return str(coverage).strip().lower() == "none"
    try:
        return float(gap.get("covered_qty") or 0) <= 0
    except (TypeError, ValueError):
        return False


def _append_coverage_gap_banner(lines: list[str], result: dict) -> None:
    """Render the broker-truth stop-coverage gap banner (🛑) when the
    reconciler found held positions with less open protective-stop coverage
    than held qty — a (partially) naked position the WAL queue didn't know
    about. This is operator-actionable: a stop needs manual re-protection.

    Spec §11.1 guard 3: NO STOP AT ALL and STOP PRESENT BUT MIS-SIZED are
    rendered as two separate banners, never merged into one count. They are
    different conditions with different urgency — a position stopped at the
    wrong size still has a broker order standing watch over most of it; a
    position with no stop has nothing. A single "N under-protected" line
    made the worse of the two invisible inside the milder one.
    """
    gaps = result.get("stop_coverage_gaps")
    if not isinstance(gaps, list) or not gaps:
        return

    def _describe(rows: list[dict]) -> str:
        # Board item 89 clarity: "NVDA(4/10)" was a fraction with no words
        # around it. Same two numbers, said as what they are.
        return "; ".join(
            f"{g.get('symbol', '?')} holding "
            f"{_fmt_qty(g.get('held_qty', 0) or 0)}, stop covers "
            f"{_fmt_qty(g.get('covered_qty', 0) or 0)}"
            for g in rows[:6]
        )

    rows = [g for g in gaps if isinstance(g, dict)]
    # Spec §11.1 hybrid fractional stops. These two classes are NOT faults
    # and must never be counted into either red banner: 'fractional_overnight'
    # is a sub-share DAY stop that lapsed at the close exactly as the design
    # intends, and 'fractional_replaced' is one the session's own sweep has
    # already put back. Both happen to every fractional position every day.
    # Rendering them as the top-severity banner would put a 🛑🛑🛑 on this
    # alert on every single run, which is how the owner learns to stop
    # reading the banner that matters.
    expected = [g for g in rows if _gap_is_expected_fractional(g)]
    faults = [g for g in rows if not _gap_is_expected_fractional(g)]
    uncovered = [g for g in faults if _gap_is_uncovered(g)]
    partial = [g for g in faults if not _gap_is_uncovered(g)]
    if uncovered:
        # Top severity tier (item 21b): a held position with ZERO stop
        # coverage is unbounded loss, not just a degraded state — the one
        # class of alert on this desk that gets the triple mark.
        lines.append(
            f"🛑🛑🛑 NO STOP AT ALL: {len(uncovered)} position(s) with nothing "
            f"protecting them — {_describe(uncovered)}"
        )
    if partial:
        # Still under-protected but a stop IS standing watch over most of
        # the position — warning tier, not the top one.
        lines.append(
            f"⚠️ STOP MIS-SIZED: {len(partial)} position(s) only partly "
            f"protected — {_describe(partial)}"
        )
    _append_fractional_overnight_line(lines, expected)


def _gap_is_expected_fractional(gap: dict) -> bool:
    """Is this gap the hybrid design working rather than failing?

    True for the two states spec §11.1's hybrid fractional stops produce on
    purpose: a sub-share DAY stop that lapsed overnight, and one the sweep
    re-placed this session. Neither is operator-actionable.
    """
    return str(gap.get("coverage", "")).strip().lower() in (
        "fractional_overnight", "fractional_replaced",
    )


def _append_fractional_overnight_line(lines: list[str], expected: list[dict]) -> None:
    """Spec §11.1 hybrid fractional stops — make the accepted exposure VISIBLE.

    The owner accepted a bounded overnight exposure on the sub-share
    remainder of a fractional position, because being locked out of expensive
    names on a ~$10k account is itself a cost. He accepted it on the explicit
    condition that it be observable: "a number he can look at beats a
    guarantee he has to trust."

    So this line reports the DOLLARS actually unprotected right now, not a
    reassurance that the design bounds them. It is deliberately not a 🛑 —
    nothing here needs doing — and it is deliberately not silent either.
    Uses 🌙 rather than a colour: the state is 'overnight', and hue carries
    no meaning on this channel.

    Silent when the remainder has already been re-placed for the session
    (nothing is exposed) — only a live, currently-unprotected remainder
    prints.
    """
    live = [
        g for g in expected
        if str(g.get("coverage", "")).strip().lower() == "fractional_overnight"
    ]
    if not live:
        return
    total = 0.0
    for gap in live:
        try:
            total += float(gap.get("unprotected_value") or 0)
        except (TypeError, ValueError):
            continue
    detail = ", ".join(
        f"{g.get('symbol', '?')} {_fmt_qty(g.get('uncovered_qty', 0) or 0)}sh"
        for g in live[:6]
    )
    lines.append(
        f"🌙 overnight fractional remainder unprotected (by design): "
        f"${total:,.2f} across {len(live)} position(s) — {detail}. "
        f"Whole-share part still covered by its GTC stop; the DAY stop is "
        f"re-placed at the next open."
    )


def _append_leverage_line(lines: list[str], result: dict) -> None:
    """Spec §11.2 — how much the book owns, its ceiling, and how far it could
    fall before the broker sells without asking.

    Nothing watched the distance to forced liquidation before this. At the
    ratified 2.0x it reads about 33% — a bad quarter, not an impossibility —
    which is precisely why it belongs on the alert the operator actually
    reads rather than in a log.

    Rendered whenever the run measured it. Silent when the block is absent
    (an older result dict, or a session that never reached the preamble) —
    an omitted line is honest; an invented "1.0x" would not be.
    """
    leverage = result.get("leverage")
    if not isinstance(leverage, dict) or not leverage:
        return
    gross_x = leverage.get("gross_x")
    ceiling_x = leverage.get("ceiling_x")
    if not isinstance(gross_x, (int, float)) or not isinstance(ceiling_x, (int, float)):
        return
    # Colour-blind-safe: the state is carried by the WORD, never by hue alone.
    de_levered = (
        isinstance(leverage.get("base_ceiling_x"), (int, float))
        and ceiling_x < leverage["base_ceiling_x"]
    )
    parts = [f"exposure: {gross_x:.2f}x of {ceiling_x:.2f}x allowed"]
    distance = leverage.get("distance_to_forced_liquidation_pct")
    if isinstance(distance, (int, float)):
        parts.append(f"{distance:.0f}% fall to a margin call")
    drawdown = leverage.get("drawdown_pct")
    if isinstance(drawdown, (int, float)) and drawdown < 0:
        parts.append(f"{abs(drawdown):.1f}% below the equity high")
    prefix = "⚠️ DE-LEVERED" if de_levered else "leverage"
    lines.append(f"{prefix} — {'  ·  '.join(parts)}")
    if leverage.get("alert_owner"):
        # The rung is READ from the resolved ceiling, never restated as a
        # literal: a hardcoded "0.5x" here would go stale the day the ratified
        # ladder changes, and an alert that misreports the cap is worse than
        # no alert. And the wording is exact — at the lowest rung new
        # positions are refused ONCE THE BOOK REACHES the cap, not
        # unconditionally; a book already below it may still trade.
        #
        # 2026-09-18: `alert_owner` is no longer only the -20% rung. It is
        # also set when the drawdown could not be MEASURED at all (an
        # unreadable equity read, rung "bad_read", which has set it since
        # 2026-09-02; and an absent equity curve, rung "unknown"). Printing
        # "DRAWDOWN PAST -20%" for those said something specific and false
        # about the book — the owner would read a measured -20% where
        # nothing had been measured. The message is chosen from the RUNG,
        # so a state that was never measured never reports a number.
        rung = leverage.get("rung")
        if rung == "unknown":
            lines.append(
                f"🛑 DRAWDOWN UNMEASURABLE: there is no equity history to "
                f"measure a high-water mark against, so the de-levering "
                f"ladder cannot fire at all. This is NOT a book at record "
                f"highs. Nothing is being trimmed and gross exposure is "
                f"held to the standing {ceiling_x:.2f}x cap until a real "
                f"equity curve exists."
            )
        elif rung == "bad_read":
            lines.append(
                f"🛑 DRAWDOWN UNMEASURABLE: the account equity reading came "
                f"back unusable, so the book's drawdown cannot be verified. "
                f"Gross exposure is held to the ladder's floor rung "
                f"({ceiling_x:.2f}x equity) until a valid reading arrives."
            )
        else:
            lines.append(
                f"🛑 DRAWDOWN PAST -20%: the de-levering ladder is at its lowest "
                f"rung. Gross exposure is capped at {ceiling_x:.2f}x equity and "
                f"new positions are refused once the book reaches it."
            )
    if leverage.get("delever_incomplete"):
        # §11.2 reporting gap: a de-lever was attempted but the account is
        # still over its limit afterward. Plain words for a non-developer
        # owner — what was tried, that it fell short, and the real number,
        # never an internal name or an invented figure.
        lines.append(
            f"⚠️ Tried to bring the account's exposure back under its limit "
            f"by selling down positions, but it is still over: the account "
            f"currently has {gross_x:.2f}x of equity invested against a "
            f"{ceiling_x:.2f}x limit."
        )


_MAX_LOOKED_UP_COMPANIES = 12


def _dedupe_symbols(symbols: list) -> list[str]:
    seen: list[str] = []
    for raw in symbols or []:
        symbol = str(raw or "").strip().upper()
        if symbol and symbol not in seen:
            seen.append(symbol)
    return seen


def _lookup_company_profiles(symbols: list, limit: int | None = None) -> dict[str, Any]:
    """symbol -> CompanyProfile for every symbol the cache already knows.

    The ONE place that calls `CompanyProfileStore` for a trader-facing
    alert — `_append_company_identities` below (the base formatter's own
    "who:" block) and `company_name()` (src/trader_feed.py's inline
    "TICKER (Company)" annotations) both build on this instead of each
    keeping its own dedupe/cap/fetch logic; do not add a second lookup,
    call this with a symbol list instead.

    `allow_fetch=False` is not an optimisation, it is the contract: an
    operator alert must never sit waiting on a network call. By the time an
    alert goes out the PM path has already warmed the cache for exactly
    these symbols, so this is a dictionary lookup. Symbols the cache does
    not know come back absent rather than blocking or inventing a name.
    """
    seen = _dedupe_symbols(symbols)
    if not seen:
        return {}
    # `limit` — board item 89 clarity defect "bare ticker symbols with no
    # company name after the twelfth name in a list": the cap below sized
    # the base formatter's "who:" block, but src/trader_feed.py annotates
    # names INLINE, where a bare ticker after the twelfth line is exactly
    # the defect. The lookup is a cache read (allow_fetch=False), so a
    # larger cap costs nothing on the wire; the trader feed passes its own.
    cap = _MAX_LOOKED_UP_COMPANIES if limit is None else max(1, int(limit))
    try:
        from src.data.company import CompanyProfileStore
        return CompanyProfileStore().get_many(seen[:cap], allow_fetch=False)
    except Exception as e:  # noqa: BLE001 — never lose an alert over prose
        logger.warning("notifier: company profiles unavailable: %s", e)
        return {}


def company_name(symbol: str, profiles: dict[str, Any] | None = None) -> str | None:
    """The one company name for `symbol`, or None if the cache doesn't have
    it. Pass a pre-fetched `profiles` dict (from `_lookup_company_profiles`,
    fetched once for every symbol a message is about to render) when
    annotating several symbols in one message, so each render is one cache
    read, not N — see src/trader_feed.py's inline "TICKER (Company)" use.
    """
    sym = str(symbol or "").strip().upper()
    if not sym:
        return None
    if profiles is None:
        profiles = _lookup_company_profiles([sym])
    profile = profiles.get(sym)
    return getattr(profile, "name", None) if profile is not None else None


def _append_company_identities(lines: list[str], symbols: list) -> None:
    """One line per relevant symbol: who the company is.

    The operator reads `BUY CCJ qty=40 @$58.10` and has to already know that
    CCJ is Cameco. Name and industry only — deliberately NOT the business
    summary that goes to the PM. A Telegram message is 4096 characters and
    competes for a phone screen; ten paragraphs of company description would
    push the order list itself out of view.

    `symbols` is a plain, already-resolved ticker list — deduplication and
    upper-casing happen in `_lookup_company_profiles`, so every caller (the
    base formatter's own order list, and src/trader_feed.py's richer
    per-mode formatters, via `extract_alert_symbols`) can hand this a raw,
    unfiltered sequence. Deliberately the ONLY place that turns a symbol
    list into "who:"-style identity text — do not duplicate this lookup
    elsewhere; give it a symbol list instead. (src/trader_feed.py no longer
    calls this — see its own inline "TICKER (Company)" annotations, which
    share the same `_lookup_company_profiles` lookup via `company_name()`.)
    """
    seen = _dedupe_symbols(symbols)
    if not seen:
        return
    profiles = _lookup_company_profiles(seen)
    if not profiles:
        return
    identities = []
    for symbol in seen[:_MAX_LOOKED_UP_COMPANIES]:
        profile = profiles.get(symbol)
        if profile is None:
            continue
        bits = [b for b in (
            getattr(profile, "name", None), getattr(profile, "industry", None),
        ) if b]
        if not bits:
            continue
        identities.append(f"  {symbol} — {' · '.join(bits)}")
    if identities:
        lines.append("who:")
        lines.extend(identities)


def _append_trade_session_body(lines: list[str], result: dict) -> None:
    # audit round 2: "analysis_error" from a trading session means the PM
    # decision was never produced (LLM output unparseable / analysis step
    # failed) — its zero orders are a FAILURE artifact, not a deliberate
    # hold. Before this line the push looked identical to a quiet no-trade
    # day, so the operator could not tell "PM chose to sit out" from "PM
    # never spoke". Rendered first: it reframes everything below it.
    status = str(result.get("status", ""))
    failure_block: list[str] = []
    if status == "paid_analysis_suspended":
        failure_block.append(
            "🛑 SUSPENDED: paid LLM analysis is halted by the mandatory cost "
            "circuit. Broker protection and deterministic safety work "
            "remain active."
        )
        err = result.get("error")
        if err:
            # 900, not 300 — this is the deterministic cost-circuit
            # breaker's trigger detail, often a multi-clause sentence
            # (which ceiling, current spend, provider) worth reading in full.
            failure_block.append(f"trigger: {_clip_text(str(err), 900)}")
    elif status.startswith("pm_") or status == "analysis_error":
        failure_block.append(
            f"🛑 FAILED: PM decision failed ({status}) — no decisions were "
            "made; this is NOT a deliberate hold and the full paid stack "
            "will not auto-repeat"
        )
        err = result.get("error")
        if err:
            failure_block.append(f"error: {_clip_text(str(err), 900)}")
    _new_section(lines, *failure_block)

    # System-health first: a naked long is more urgent than the order list.
    _new_block(lines, _append_coverage_gap_banner, result)
    _new_block(lines, _append_leverage_line, result)
    orders = result.get("orders") or []

    def _render_orders(lines: list[str]) -> None:
        # FORCE_DELEVER / EMERGENCY_SELL / EMERGENCY_COVER banner — these
        # actions mean the autonomous loop intervened automatically.
        # force_delever fires when cash < -$1 (margin disabled) and
        # biggest-loser-first sells until cash >= 0. emergency_sell fires
        # from intra_check's / midday's flash-crash protection closing a
        # long; emergency_cover is the same circuit breaker covering a
        # SHORT (a distinct action name — not "emergency_sell" — because
        # it's a BUY, and reusing the SELL name here would also have to be
        # reused in db.py's realized-P&L FIFO lot matching, which assumes a
        # "sell-family" action closes a long against open BUY lots; a short
        # has no BUY lot to match against). All three look identical to a
        # routine order on the wire otherwise — operator's most important
        # "system intervened" signal would be invisible without this
        # banner. Kept glued to the order list right below it (same
        # section) rather than gapped off, since it's an annotation of
        # exactly those orders, not a separate topic.
        forced = [
            o for o in orders
            if isinstance(o, dict) and str(o.get("action", "")).upper() in (
                "FORCE_DELEVER", "EMERGENCY_SELL", "EMERGENCY_COVER",
            )
        ]
        if forced:
            actions = sorted({str(o.get("action", "")).upper() for o in forced})
            symbols = sorted({str(o.get("symbol", "?")) for o in forced})
            lines.append(
                f"🚨 AUTONOMOUS INTERVENTION ({', '.join(actions)}): "
                f"{len(forced)} order(s) on {', '.join(symbols)}"
            )

        if orders:
            buys = [o for o in orders if _order_side(o) == "buy"]
            sells = [o for o in orders if _order_side(o) == "sell"]
            lines.append(f"orders: {len(orders)}  (BUY {len(buys)} / SELL {len(sells)})")
            # Show every order on its own line — operator wants to know what
            # was actually traded, not just a count. SELLs first (closing
            # context), then BUYs (opening context). 10-per-side cap is a
            # safety against unusual sessions; 99% of days are <10 each
            # and the full list fits in one Telegram message (4096 char limit).
            for o in sells[:10]:
                # Tag forced sells inline so operator can spot the specific
                # symbol that triggered the intervention banner above.
                action = str(o.get("action", "")).upper() if isinstance(o, dict) else ""
                label = "  SELL  "
                if action == "FORCE_DELEVER":
                    label = "  🚨FORCE"
                elif action == "EMERGENCY_SELL":
                    label = "  🚨EMER "
                lines.append(f"{label}{_order_summary(o)}")
            for o in buys[:10]:
                # EMERGENCY_COVER is a forced BUY (covering a short) — tag it
                # the same way the sells loop above tags a forced SELL, so the
                # operator can spot it without cross-referencing the banner.
                action = str(o.get("action", "")).upper() if isinstance(o, dict) else ""
                label = "  BUY   "
                if action == "EMERGENCY_COVER":
                    label = "  🚨EMER "
                lines.append(f"{label}{_order_summary(o)}")
            omitted = max(0, len(buys) - 10) + max(0, len(sells) - 10)
            if omitted:
                lines.append(f"  (+{omitted} more — see audit log)")
            _append_company_identities(
                lines, [o.get("symbol") for o in orders if isinstance(o, dict)],
            )
        else:
            lines.append("orders: 0")

    _new_block(lines, _render_orders)

    from src import evidence_gate
    data_status = result.get("data_status") or {}
    degraded = [
        k for k, v in data_status.items()
        if evidence_gate.counts_as_degraded(v)
    ]
    if degraded:
        _new_section(lines, f"⚠️ degraded: {', '.join(sorted(degraded))}")


def _evening_pnl_block(result: dict) -> list[str]:
    """The evening message's own P&L lines — Daily P&L (4pm-correct where
    available), equity, and the same day's return against capital actually
    at risk.

    Lifted OUT of `_append_evening_body` unchanged on 2026-09-18 so it can
    lead the message rather than sit below the escalation banners and the
    cost lines (owner: P&L directly under the heading, every message). Not
    one figure, basis or fallback was altered in the move — this is the
    same arithmetic in a different place, which is why the existing
    4pm-vs-real-time regression tests still pin it.

    Deliberately NOT `trader_feed._pnl_section_lines`: that renders the
    real-time `daily_pnl`, and the evening message must show the official
    close-to-close figure. Using the shared one here would leak exactly the
    after-hours number the 4pm path exists to keep out.
    """
    lines: list[str] = []
    # Daily P&L summary — the headline of the evening push. Operator wants to
    # know "did I make money today" without grepping logs.
    #
    # Prefer the TRUE close-to-close ("4pm-to-4pm") P&L the pipeline computed
    # from Alpaca portfolio_history (pnl_4pm / equity_close = today's official
    # regular-session close). That's clean of after-hours drift AND free of the
    # off-by-one trap of differencing account.last_equity (which is the PRIOR
    # day's close). Fall back to the real-time prior-close→now diff when the
    # 4pm figures aren't available (API gap / legacy result dicts).
    daily_pnl = result.get("daily_pnl")
    total_value = result.get("total_value")
    pnl_4pm = result.get("pnl_4pm")
    equity_close = result.get("equity_close")

    _fmt_pnl = _fmt_signed_money

    # Phase 6 (§6.3b) — the SAME day's P&L expressed against capital
    # actually at risk, not just against total equity. "Risk capital" here
    # is `sum((entry - stop) x shares)` across open positions — audit §1.3's
    # `budget_risk_dollars` from `src.risk.metrics.portfolio_heat`, reused
    # (not recomputed) via `TradingPipeline._build_portfolio_heat` and
    # threaded through evening's result dict as `risk_capital_dollars`.
    # Equity tells you how the whole book did; this tells you how the
    # capital that was actually exposed today did — a much bigger number on
    # a day the book was mostly in cash or mostly stopped-out to breakeven.
    risk_capital = result.get("risk_capital_dollars")

    def _append_risk_capital_line(pnl: float | None) -> None:
        if risk_capital is None:
            return  # heat build failed or wasn't available — say nothing, not a guess
        if risk_capital <= 0:
            # A flat book (or a book where every stop has trailed past
            # entry, releasing all risk) — not a divide-by-zero, and NOT a
            # fabricated 0%: there was no capital at risk to measure P&L
            # against today.
            lines.append("   vs risk capital: n/a — no capital currently at risk (flat book)")
            return
        if pnl is None:
            return
        risk_pct = pnl / risk_capital * 100
        risk_str = f"+{risk_pct:.2f}%" if pnl >= 0 else f"{risk_pct:.2f}%"
        lines.append(f"   vs risk capital: {risk_str}  (${risk_capital:,.2f} at risk)")

    if pnl_4pm is not None and equity_close is not None:
        # baseline = prior official close = equity_close - pnl_4pm.
        baseline = equity_close - pnl_4pm
        if baseline > 0:
            r = pnl_4pm / baseline * 100
            ret_str = f"+{r:.2f}%" if pnl_4pm >= 0 else f"{r:.2f}%"
        else:
            ret_str = "n/a"
        lines.append(f"💰 Daily P&L: {_fmt_pnl(pnl_4pm)} ({ret_str})  ·  4pm close")
        lines.append(f"   Equity: ${equity_close:,.2f}")
        _append_risk_capital_line(pnl_4pm)
    elif daily_pnl is not None and total_value is not None:
        # Fallback: real-time diff (prior close → 8pm, includes after-hours).
        # Return is P&L over PRIOR-day equity (= total_value − daily_pnl); using
        # current equity would understate losses (denominator includes the draw).
        prior_equity = total_value - daily_pnl
        if prior_equity > 0:
            ret_pct = (daily_pnl / prior_equity) * 100
            ret_str = f"+{ret_pct:.2f}%" if daily_pnl >= 0 else f"{ret_pct:.2f}%"
        else:
            # prior_equity <= 0 → return % undefined; "0.00%" would mislead.
            ret_str = "n/a"
        lines.append(f"💰 Daily P&L: {_fmt_pnl(daily_pnl)} ({ret_str})")
        lines.append(f"   Equity: ${total_value:,.2f}")
        _append_risk_capital_line(daily_pnl)

    return lines


def _append_evening_body(lines: list[str], result: dict) -> None:
    # === Escalation banners (first thing read, before Daily P&L) ===
    analysis = result.get("analysis")

    def _render_escalation_banners(lines: list[str]) -> None:
        # (0) Dead-man's check: a market-day session that left zero agent_logs
        # today silently never ran (disabled timer, stuck lock, half-day window
        # math). morning missing is unambiguous → 🛑; midday/close can be
        # legitimately skipped on some early-close days → softer ⚠️.
        missing = result.get("missing_sessions")
        if isinstance(missing, list) and missing:
            # Prefix match: the sharpened probes emit decorated entries like
            # "morning (PM plan never risk-reviewed — checkpoint unconsumed)" —
            # they carry the diagnosis and must hit the hard banner too.
            hard = [m for m in missing
                    if m == "morning" or str(m).startswith("morning (")]
            for m in hard:
                detail = m if m != "morning" else (
                    "morning — no agent activity logged; check the timer/scheduler"
                )
                lines.append(f"🛑 INCOMPLETE: MORNING SESSION TODAY — {detail}")
            soft = [m for m in missing if m not in hard]
            if soft:
                lines.append(f"⚠️ no activity logged today for: {', '.join(soft)}")

        # (0b) Broker-truth stop-coverage gap (last check before overnight).
        _append_coverage_gap_banner(lines, result)

        # (1) LLM-graded escalation — evening's contract maps thesis_trajectory=
        # broken / macro_warning_ignored loss patterns to risk_rating >= elevated.
        risk_for_banner = _attr_or_key(analysis, "risk_rating")
        if isinstance(risk_for_banner, str) and risk_for_banner.lower() in ("elevated", "high"):
            lines.append(f"🚨 OPERATOR ATTENTION — risk_rating={risk_for_banner}")

        # A (2) used to sit here: a deterministic banner raised when the
        # day's loss reached 80% of the account-level daily-loss circuit
        # breaker. That breaker was removed 2026-09-20 on the owner's
        # instruction (retired item 32, docs/INCIDENT_HISTORY.md), so there
        # is no limit left to measure the day against.

    _new_block(lines, _render_escalation_banners)

    # The evening P&L block is NOT rendered here any more: owner, 2026-09-18,
    # "all the P&L information has to go at the very top of every telegram
    # alert, right after the first line, which is really the heading." It now
    # renders from `_evening_pnl_block` above the escalation banners and above
    # the cost lines — see `_pnl_lines_for`. Nothing about the figures changed.

    # Suggested actions — surfaced HIGH in the message (right after the
    # headline P&L) so the tail-clip truncation in send() can never eat
    # them. On exactly the high-risk days where these are populated the
    # message is longest, and these are the lines most worth reading.
    # Only shown when risk_rating is elevated/high. (The P&L history
    # text table that used to follow was replaced by the daily CSV
    # export — PR #99.)
    _actions_start = len(lines)
    risk_for_actions = _attr_or_key(analysis, "risk_rating")
    if isinstance(risk_for_actions, str) and risk_for_actions.lower() in ("elevated", "high"):
        actions = _attr_or_key(analysis, "suggested_actions") or []
        if isinstance(actions, list) and actions:
            lines.append("⚡ Suggested actions:")
            for act in actions[:5]:
                if not isinstance(act, str):
                    continue
                # 500, not 200 — this is exactly the field the operator
                # complained about: a per-symbol call like "CRM: strong
                # heavy accumulation volume, add on any weakness..." was
                # being cut off mid-sentence at 200 chars with no ellipsis.
                lines.append(f"   • {_clip_text(act, 500)}")
    _seal_section(lines, _actions_start)

    # Position snapshot: total invested + cash + top winners/losers.
    # Helper queries the live DB so this works regardless of how the
    # evening result dict is constructed.
    # `total_value` used to be a local of the P&L block that moved to
    # `_evening_pnl_block` (2026-09-18); read it back from the same key the
    # block reads, so the snapshot's denominator is unchanged.
    _new_block(lines, _append_position_snapshot, result.get("total_value"))

    _tomorrow_start = len(lines)
    analysis = result.get("analysis")
    risk = _attr_or_key(analysis, "risk_rating")
    bias = _attr_or_key(analysis, "tomorrow_bias")
    conv = _attr_or_key(analysis, "tomorrow_conviction")
    if risk or bias or conv:
        bits = []
        if risk:
            bits.append(f"risk={risk}")
        if bias:
            bits.append(f"bias={bias}")
        if conv:
            bits.append(f"conv={conv}")
        lines.append("🔮 Tomorrow: " + "  ".join(bits))
    outlook = _attr_or_key(analysis, "tomorrow_outlook") or ""
    if outlook:
        lines.append(f"   {_clip_text(outlook, 1000)}")
    _seal_section(lines, _tomorrow_start)

    # Auto-meta piggyback (Round 2 enabled this; Round 6 adds the
    # dry-run staging hint). When today is the last trading day of a
    # quarter, run_evening invokes run_quarterly_meta_reflection and
    # stuffs the result into `result['auto_meta']`. Surface dry-run
    # proposals so the operator knows to review proposed_edits.json
    # before next quarter.
    _meta_start = len(lines)
    auto_meta = result.get("auto_meta")
    if isinstance(auto_meta, dict):
        # audit round 2 (#15/#19): the producer
        # (run_quarterly_meta_reflection) never emits top-level
        # "applied"/"rejected" ints — the counts exist only as LISTS nested
        # inside editor_report (ApplicationReport.to_dict). The old flat
        # .get("applied", 0)/.get("rejected", 0) reads always yielded 0/0,
        # so both hint branches were dead code and the once-a-quarter
        # "review proposed_edits.json" operator prompt never fired (the
        # 2026-06-30 quarter end went through this dead path). Stage-only
        # proposals surface as "rejected" entries whose reason carries
        # "dry_run" — count those separately for accurate wording.
        report = auto_meta.get("editor_report") or {}
        applied = len(report.get("applied") or [])
        rej_list = report.get("rejected") or []
        rejected = len(rej_list)
        staged = sum(
            1 for r in rej_list
            if isinstance(r, dict) and "dry_run" in str(r.get("reason", ""))
        )
        proposed = int(auto_meta.get("proposed_learnings_count") or 0)
        period = auto_meta.get("period", "?")
        status = auto_meta.get("status", "?")
        if status == "auto_meta_error":
            err = _clip_text(str(auto_meta.get("error", "?")), 600)
            lines.append(f"🧪 meta {period}: ERROR — {err}")
        elif status == "digest_only":
            # LLM reflection step failed after the digest was written —
            # the learning loop is broken until next quarter.
            lines.append(
                f"🧪 meta {period}: digest written but LLM reflection "
                f"FAILED — check logs"
            )
        elif applied > 0:
            lines.append(
                f"🧪 meta {period}: applied {applied} learning(s); "
                f"rejected {rejected}"
            )
        elif staged > 0:
            # Dry-run staged proposals (none actually applied).
            lines.append(
                f"🧪 meta {period}: {staged} proposal(s) staged "
                f"(dry-run — see data/evolution/{period}/proposed_edits.json)"
            )
        elif rejected > 0:
            # Live/off mode with everything rejected by guardrails or the
            # enabled=false short-circuit — still worth one line.
            lines.append(
                f"🧪 meta {period}: 0 applied / {rejected} rejected "
                f"(see data/evolution/edits.jsonl)"
            )
        elif proposed > 0:
            # editor_report missing (editor crashed) but the reflection
            # carried proposals — surface the review hint rather than
            # nothing (idx 19 fallback).
            lines.append(
                f"🧪 meta {period}: {proposed} proposal(s) generated but "
                f"prompt-editor report missing — check logs"
            )
        # status='skipped' (not quarter-end) → no line, normal evening.
    _seal_section(lines, _meta_start)


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
        # Unknown model in the pricing table for at least one call \u2014
        # cannot honestly sum. Say so; never show a partial total as though
        # it were the whole, and never show a fabricated figure.
        return (
            "\U0001f4b5 AI cost for this run: not available \u2014 one of "
            "the models used has no price on file"
        )
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
        return (
            f"\U0001f4c5 Spent today: nothing yet, against a ${limit:,.2f} "
            f"cap for the day"
        )
    if not limit:
        return f"\U0001f4c5 Spent today: ${spent:,.2f} so far"
    return (
        f"\U0001f4c5 Spent today: ${spent:,.2f} of the ${limit:,.2f} cap "
        f"for the day"
    )


def _daily_cost_limit() -> float | None:
    """The configured daily cap, or None if it cannot be read."""
    try:
        from src.config import load_config
        cfg = load_config("config/settings.yaml")
        return float(cfg.llm_cost_circuit.daily_cost_limit_usd)
    except Exception:  # noqa: BLE001
        return None


def _openrouter_balance_line() -> str | None:
    """'🔋 OpenRouter: $X left (~N trading days)', or None if unavailable.

    WHY THIS EXISTS. OpenRouter is prepaid. When the balance reaches zero the
    desk does not degrade gracefully — it stops at whatever point in a session
    the money ends, which on this system's form means two minutes after the
    opening bell. Nothing surfaced the balance anywhere, so the only way to
    learn it was to go and look. Owner asked for it on the morning message.

    The day estimate is deliberately based on a CLEAN day's cost, not on an
    average of recent days. Days on which the desk crashed early are cheap,
    so averaging them in flatters the runway exactly when things are going
    worst. $1.02 is the measured cost of 2026-08-27, the one day in that week
    where all six sessions ran and the morning completed first time.

    Never raises and never blocks: a balance lookup must not be able to stop
    a trading alert from going out. Any failure returns None and the line is
    simply absent. Suppressed under QAMC_REHEARSAL for the same reason every
    other outbound call is — a rehearsal must not touch the network.
    """
    if _REHEARSAL_MODE:
        return None
    key = (os.environ.get("OPENROUTER_API_KEY") or "").strip()
    if not key:
        return None
    try:
        import json as _json
        import urllib.request
        req = urllib.request.Request(
            "https://openrouter.ai/api/v1/credits",
            headers={"Authorization": f"Bearer {key}"},
        )
        # Short timeout on purpose: this is a nicety attached to an alert
        # that matters. It must never delay the alert noticeably.
        with urllib.request.urlopen(req, timeout=6) as resp:
            data = _json.load(resp).get("data") or {}
        purchased = float(data["total_credits"])
        used = float(data["total_usage"])
    except Exception as exc:  # noqa: BLE001 — a nicety must never break the alert
        logger.warning("OpenRouter balance lookup failed: %s", exc)
        return None
    remaining = purchased - used
    #: Measured cost of one clean trading day (2026-08-27: all six sessions,
    #: morning completed on its first attempt). See the docstring on why this
    #: is not an average.
    clean_day_usd = 1.02
    days = max(0, int(remaining / clean_day_usd))
    warn = " ⚠️ top up" if days <= 7 else ""
    return (
        f"🔋 OpenRouter: ${remaining:,.2f} left of ${purchased:,.2f} "
        f"(~{days} trading days){warn}"
    )


def _margin_interest_lines() -> list[str]:
    """['💳 margin interest: ...', '   broker check: ...'] — spec §11.2.

    ALWAYS returns at least one line outside rehearsal (owner decision,
    2026-09-18, verbatim: "Yes, every day, even if it's zero, that way I
    know it's still working"). `margin_interest.format_daily_line` owns
    that policy and the wording of all four states — real debit, nothing
    borrowed, cash unreadable, no rate configured; read its docstring for
    why the spec's original silent-on-zero rule is deliberately overridden
    and why a missing rate must NOT render as a zero.

    Morning-only, like the balance/day-cost lines above: interest accrues
    on the OVERNIGHT debit balance, so the morning snapshot — taken before
    any new trading — is the one honest read of what was actually carried
    across the close.

    Reads the account's actual cash regardless of `allow_margin` — that
    flag is QAMC's own risk toggle, not a broker-side guarantee that cash
    stays non-negative. `cash_only` (src/risk/rules.py) hard-blocks a
    plain BUY from taking cash negative when `allow_margin` is `False`,
    but a COVER is exempt from that rule by design (D10), and
    `src/agents/portfolio_manager.py`'s DE-LEVER MANDATE already treats
    "cash negative AND allow_margin False" as a real, live state — so a
    debit balance can exist even with margin disabled, and a short-circuit
    on `allow_margin` alone would silently miss it. On a zero-debit day
    this still costs one broker round-trip (spent on
    `overnight_debit_balance()` returning `0.0`) and now renders the
    explicit zero line it proves.

    When a debit balance IS present, this also reads the broker's own
    `INT` account activity and reports whether it confirms, denies, or has
    not yet settled the question of whether paper trading actually charges
    this — see `src.margin_interest` for why that is deliberately
    left open rather than assumed either way.

    Never raises: a broker-read failure here must not be able to block the
    alert — it degrades to a line that SAYS the read failed, rather than to
    silence. Suppressed entirely under QAMC_REHEARSAL, same as every other
    line here that touches the network: a rehearsal has no live account to
    report on, so there is no running tracker for a zero to prove alive and
    the line would be theatre.
    """
    from src.margin_interest import (
        RATE_UNAVAILABLE_LINE, UNAVAILABLE_LINE, build_estimate,
        compare_estimate_to_broker_activity, format_daily_line,
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
        estimate = build_estimate(debit_balance, rate_pct)
    except Exception as exc:  # noqa: BLE001
        logger.warning("margin interest estimate failed: %s", exc)
        return [UNAVAILABLE_LINE]

    # Always exactly one line, zero and fault states included.
    lines = [format_daily_line(cash, rate_pct)]
    if estimate is None:
        # Nothing borrowed: there is no charge to check the broker's own
        # INT records against, so the second line would have nothing to say.
        return lines

    try:
        activities = broker.get_margin_interest_activities()
        comparison = compare_estimate_to_broker_activity(estimate, activities)
        if comparison is not None:
            lines.append(f"   broker check: {comparison.note}")
    except Exception as exc:  # noqa: BLE001
        logger.warning("margin interest INT-activity check failed: %s", exc)

    return lines


def _append_position_snapshot(lines: list[str], total_value: float | None) -> None:
    """Render top-3 winners + top-3 losers by unrealized P&L from the
    live positions table. Read-only DB hit; degrades gracefully on any
    error (the rest of the message still goes out)."""
    try:
        import sqlite3
        # Default path — same as Database default. If the pipeline
        # config changed it, this snippet won't reflect that; we
        # accept that limitation rather than threading config in.
        if not _DB_PATH.exists():
            return
        conn = sqlite3.connect(str(_DB_PATH))
        try:
            rows = conn.execute(
                "SELECT symbol, qty, avg_entry, current_price, "
                "market_value, unrealized_pnl FROM positions "
                # qty != 0: a short's qty is negative and it is still an open
                # position the operator must see in the evening snapshot.
                "WHERE qty != 0 ORDER BY unrealized_pnl DESC"
            ).fetchall()
        finally:
            conn.close()
    except Exception as exc:
        logger.warning("evening position snapshot failed: %s", exc)
        return
    if not rows:
        return
    # The cash-sweep vehicle is parked CASH, not deployed capital (that's its
    # whole contract: hidden from every LLM view, credited as cash by the risk
    # engine, first to liquidate in force_delever). Counting it here reported a
    # ~99%-deployed book on a night the money was entirely in T-bills —
    # inverting the operator's one nightly glance at exposure, and listing SGOV
    # among the P&L movers (2026-07-16 audit).
    parked = sum(r[4] for r in rows
                 if r[0] in _SWEEP_SYMBOLS and r[4] is not None)
    rows = [r for r in rows if r[0] not in _SWEEP_SYMBOLS]
    invested = sum(r[4] for r in rows if r[4] is not None)
    cash_pct = None
    if total_value and total_value > 0:
        cash_pct = max(0.0, (total_value - invested) / total_value * 100)
    summary = f"   Positions: {len(rows)}  invested ${invested:,.0f}"
    if cash_pct is not None:
        summary += f"  ({100 - cash_pct:.0f}% deployed / {cash_pct:.0f}% cash)"
    if parked > 0:
        summary += f"  [+${parked:,.0f} parked in T-bills]"
    lines.append(summary)
    if not rows:
        return

    def _row_line(r: tuple) -> str:
        sym, qty, avg, curr, mv, pnl = r
        pct = ((curr / avg - 1) * 100) if avg else 0
        sign = "+" if pnl >= 0 else "−"
        return f"   {sym:<6} {sign}${abs(pnl):>8,.0f}  ({pct:+.1f}%)"

    # r[5] is positions.unrealized_pnl. SQLite allows NULL on that
    # column (broker race / stale snapshot can leave it unset for a
    # new row), and `None > 0` raises TypeError — which the outer
    # try/except in format_session_result does NOT catch at the
    # right granularity, leaving the operator without the evening
    # snapshot at all. Filter None explicitly. Audit 2026-05-27.
    winners = [r for r in rows if r[5] is not None and r[5] > 0][:3]
    if winners:
        lines.append("📈 Top winners:")
        for r in winners:
            lines.append(_row_line(r))
    losers = [r for r in rows if r[5] is not None and r[5] < 0][-3:][::-1]
    if losers:
        lines.append("📉 Underwater:")
        for r in losers:
            lines.append(_row_line(r))


def _append_earnings_body(lines: list[str], result: dict) -> None:
    """Fallback body only — the owner-facing pre-earnings message is
    `src.trader_feed._format_earnings`. This names each filing when the
    run recorded them (`result["filings"]`, 2026-09-18) and falls back to
    the bare counts for a result that predates that field."""
    filings = [f for f in (result.get("filings") or []) if isinstance(f, dict)]
    if filings:
        for row in filings:
            outcome = "read" if row.get("outcome") == "analyzed" else "could not be read"
            lines.append(
                f"{row.get('symbol', '?')} {row.get('form_type', '')} filed "
                f"{row.get('filing_date', 'date not recorded')}: {outcome}"
            )
        return
    analyzed = result.get("analyzed", 0)
    confirmed = result.get("confirmed", 0)
    failed = result.get("failed", 0)
    lines.append(f"analyzed: {analyzed}  confirmed: {confirmed}  failed: {failed}")


def _append_intra_check_body(lines: list[str], result: dict) -> None:
    # Reaches here when a deterministic breach fired, OR (spec §11.1 guard 3)
    # when an otherwise-OK 30-minute tick found a stop-coverage gap — the
    # sweep's finding is the whole reason that tick broke silence, so it is
    # the first thing on the message.
    _append_coverage_gap_banner(lines, result)
    # Operator wants the details of whatever triggered.
    emergency = result.get("orders") or result.get("emergency_orders") or []
    if emergency:
        lines.append(f"⚠️ EMERGENCY orders: {len(emergency)}")
        for o in emergency[:5]:
            lines.append(f"  {_order_summary(o)}")
    reason = result.get("reason")
    if reason:
        lines.append(f"reason: {reason}")


def _append_meta_body(lines: list[str], result: dict) -> None:
    period = result.get("period")
    if period:
        lines.append(f"period: {period}")
    # audit round 2 (#15/#19): run_quarterly_meta_reflection has no flat
    # "applied"/"rejected" keys — derive the counts from the nested
    # editor_report lists (ApplicationReport.to_dict), same as the evening
    # auto-meta consumer. The old flat reads rendered nothing, ever.
    report = result.get("editor_report") or {}
    applied = len(report.get("applied") or [])
    rej_list = report.get("rejected") or []
    rejected = len(rej_list)
    staged = sum(
        1 for r in rej_list
        if isinstance(r, dict) and "dry_run" in str(r.get("reason", ""))
    )
    if applied or rejected:
        lines.append(f"learnings: applied={applied} rejected={rejected}")
        if staged:
            lines.append(
                f"🧪 {staged} proposal(s) staged for review — "
                f"data/evolution/{period}/proposed_edits.json"
            )
    elif result.get("proposed_learnings_count"):
        lines.append(
            f"⚠️ {result['proposed_learnings_count']} proposal(s) generated "
            f"but prompt-editor report missing — check logs"
        )
    reason = result.get("reason")
    if reason:
        lines.append(f"reason: {reason}")


# === Helpers ===

def _status_emoji(status: str) -> str:
    if status in (
        "executed", "analyzed", "reviewed", "preprocessed", "reflected",
        "sent",
    ):
        return "🟢"
    if status in (
        "no_trades", "no_data", "nothing_new", "ok",
        "market_holiday", "early_close",
    ):
        return "⚪"
    # `digest_only` is intentionally classified as a warning, not success:
    # quarterly meta-reflection's digest got written but the LLM
    # reflection step itself failed (LLM exception / parse error). The
    # learning loop is half-broken until next quarter — operator should
    # notice via 🟡 rather than skim past a green check.
    # `evidence_gate_skip` (docs/WORK.md item 20) is a WARNING, never the
    # white "nothing happened" bucket: the desk deliberately declined to
    # decide because a seat's answer never arrived. It looks like a quiet
    # day and is not one, which is exactly how retired item 11 hid.
    if status in (
        "emergency_sold", "hard_risk_block", "digest_only",
        "evidence_gate_skip",
    ):
        return "🟡"
    if ("error" in status or status.startswith("pm_")
            or status in (
                "rejected", "failed", "paid_analysis_suspended",
                # Guard 1 (2026-09-02): ops halted the desk with the
                # kill-switch flag file. This is the one status that fires
                # even on an intra_check tick, which is otherwise silent —
                # see the "kill_switch_halted" not being in the
                # mode == "intra_check" silence tuple above.
                "kill_switch_halted",
            )):
        # Item 21b: shape, not colour — a red circle reads the same as the
        # green/yellow/white ones to the owner. 🛑 is the only shape swap in
        # this bucket; the plain-text "FAILED: " prefix that goes with it is
        # added by the caller, which already knows the mode/timestamp.
        return "🛑"
    return "⚪"


def _order_side(order: Any) -> str:
    """Best-effort extract of order side. Order shape varies by
    submission path: some are Alpaca SDK response dicts (have
    'side'), some are internal {'symbol','action',...} dicts."""
    if not isinstance(order, dict):
        return ""
    side = order.get("side")
    if isinstance(side, str):
        return side.lower()
    action = str(order.get("action", "")).upper()
    # Stage 3: COVER (and PARTIAL_COVER/EMERGENCY_COVER) checked FIRST — it
    # is a buy-side broker order (buying back borrowed shares) even though
    # it CLOSES risk rather than opening it, so it must not fall into the
    # SELL-ish bucket below just because "COVER" reads like an exit.
    if "COVER" in action:
        return "buy"
    if any(s in action for s in (
        "SELL", "REDUCE", "TAKE_PROFIT", "EMERGENCY_SELL",
        "FORCE_DELEVER", "PARTIAL_SELL",
        # SHORT is a sell-side broker order (selling borrowed shares) even
        # though it OPENS risk rather than closing it.
        "SHORT",
    )):
        return "sell"
    if action == "BUY":
        return "buy"
    return ""


def _order_summary(order: Any) -> str:
    """Render one order line like 'NVDA   qty=5  @$420.50  SL=$405.00'.

    Falls back gracefully when fields are missing (older broker
    response shapes, or close_position which only returns id/status)."""
    if not isinstance(order, dict):
        return str(order)[:60]
    sym = str(order.get("symbol", "?"))
    parts: list[str] = [f"{sym:<6}"]
    qty = order.get("qty") or order.get("filled_qty")
    if qty is not None:
        parts.append(f"qty={_fmt_qty(qty)}")
    # Prefer the limit_price (what we asked broker to fill at). If not
    # present (market order / older path), fall back to a generic price.
    lim = order.get("limit_price") or order.get("price")
    if lim is not None and lim > 0:
        parts.append(f"@${_fmt_price(lim)}")
    sl = order.get("stop_loss_price")
    if sl is not None and sl > 0:
        parts.append(f"SL=${_fmt_price(sl)}")
    return "  ".join(parts)


def _fmt_qty(qty: Any) -> str:
    try:
        q = float(qty)
    except (TypeError, ValueError):
        return str(qty)
    # Integer-valued quantities (the common case for stocks) render
    # without the trailing '.0'; fractional shares keep precision.
    return f"{int(q)}" if q == int(q) else f"{q:g}"


def _fmt_price(price: Any) -> str:
    try:
        p = float(price)
    except (TypeError, ValueError):
        return str(price)
    # Sub-dollar penny stocks keep 4 decimals; everything else 2.
    return f"{p:.4f}" if p < 1.0 else f"{p:,.2f}"


def _fmt_elapsed(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes = int(seconds // 60)
    secs = int(seconds % 60)
    return f"{minutes}m {secs}s"


def build_daily_csv(closes: list[tuple[str, float]]) -> bytes:
    """Build a P&L history CSV from portfolio_history closes.

    Columns: Date, NAV, Daily P&L, Daily Return %, Drawdown %, SPY Close,
    SPY Return %

    SPY data is fetched via yfinance for the same date range. On any
    yfinance failure the SPY columns are left blank.
    """
    import io, csv, math
    from datetime import datetime, timedelta

    if not closes:
        return b""

    # Fetch SPY closes for the same date range.
    spy_closes: dict[str, float] = {}
    try:
        import yfinance as yf
        import pandas as pd
        earliest = closes[0][0]
        start = (datetime.strptime(earliest, "%Y-%m-%d") - timedelta(days=5)).strftime("%Y-%m-%d")
        end_dt = datetime.strptime(closes[-1][0], "%Y-%m-%d") + timedelta(days=2)
        end = end_dt.strftime("%Y-%m-%d")
        df = yf.download("SPY", start=start, end=end, progress=False, auto_adjust=True)
        if not df.empty:
            if hasattr(df.columns, "get_level_values"):
                df.columns = df.columns.get_level_values(0)
            # dropna()+isfinite: a NaN close (data gap / halt) is truthy as a
            # float, so it would slip past the `spy_close and prev_spy` guard,
            # render as "+nan" in the CSV, AND poison prev_spy for every later
            # row. Keep only valid finite closes out of the dict entirely.
            for dt_idx, row in df["Close"].dropna().items():
                val = float(row)
                if math.isfinite(val):
                    spy_closes[str(dt_idx.date())] = val
    except Exception as exc:
        logger.warning("build_daily_csv: SPY fetch failed: %s", exc)

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["Date", "NAV", "Daily P&L", "Daily Return %", "Drawdown %", "SPY Close", "SPY Return %"])

    prev_nav: float | None = None
    prev_spy: float | None = None
    peak_nav: float | None = None
    for date, nav in closes:
        daily_pnl = nav - prev_nav if prev_nav is not None else 0.0
        daily_ret = (daily_pnl / prev_nav * 100) if prev_nav else 0.0
        peak_nav = max(peak_nav, nav) if peak_nav is not None else nav
        drawdown = (nav - peak_nav) / peak_nav * 100 if peak_nav else 0.0
        spy_close = spy_closes.get(date)
        if spy_close is not None and math.isfinite(spy_close) and prev_spy:
            spy_ret = (spy_close - prev_spy) / prev_spy * 100
        else:
            spy_ret = ""
        writer.writerow([
            date,
            f"{nav:.2f}",
            f"{daily_pnl:+.2f}",
            f"{daily_ret:+.4f}",
            f"{drawdown:+.4f}",
            f"{spy_close:.2f}" if spy_close else "",
            f"{spy_ret:+.4f}" if spy_ret != "" else "",
        ])
        prev_nav = nav
        prev_spy = spy_close if spy_close else prev_spy

    return buf.getvalue().encode("utf-8")


def _attr_or_key(obj: Any, name: str) -> Any:
    """Get `name` from either an attribute (Pydantic model) or a
    dict key (raw JSON). Returns None on miss without raising."""
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)
