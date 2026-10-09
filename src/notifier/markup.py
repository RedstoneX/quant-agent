"""Per-symbol links, structural markup and the small formatting helpers.

Moved verbatim from the former src/notifier.py; the package re-exports it.
"""

from __future__ import annotations

import html
import re
from typing import Any

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
    pattern = re.compile(r"\b(" + "|".join(re.escape(s) for s in sorted(seen, key=len, reverse=True)) + r")\b")

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


def _dedupe_symbols(symbols: list) -> list[str]:
    seen: list[str] = []
    for raw in symbols or []:
        symbol = str(raw or "").strip().upper()
        if symbol and symbol not in seen:
            seen.append(symbol)
    return seen


# === Helpers ===


def _status_emoji(status: str) -> str:
    if status in (
        "executed",
        "analyzed",
        "reviewed",
        "preprocessed",
        "reflected",
        "sent",
    ):
        return "🟢"
    if status in (
        "no_trades",
        "no_data",
        "nothing_new",
        "ok",
        "market_holiday",
        "early_close",
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
        "emergency_sold",
        "hard_risk_block",
        "digest_only",
        "evidence_gate_skip",
    ):
        return "🟡"
    if (
        "error" in status
        or status.startswith("pm_")
        or status
        in (
            "rejected",
            "failed",
            "paid_analysis_suspended",
            # Guard 1 (2026-09-02): ops halted the desk with the
            # kill-switch flag file. This is the one status that fires
            # even on an intra_check tick, which is otherwise silent —
            # see the "kill_switch_halted" not being in the
            # mode == "intra_check" silence tuple above.
            "kill_switch_halted",
        )
    ):
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
    if any(
        s in action
        for s in (
            "SELL",
            "REDUCE",
            "TAKE_PROFIT",
            "EMERGENCY_SELL",
            "FORCE_DELEVER",
            "PARTIAL_SELL",
            # SHORT is a sell-side broker order (selling borrowed shares) even
            # though it OPENS risk rather than closing it.
            "SHORT",
        )
    ):
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


def _attr_or_key(obj: Any, name: str) -> Any:
    """Get `name` from either an attribute (Pydantic model) or a
    dict key (raw JSON). Returns None on miss without raising."""
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)
