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
"""
from __future__ import annotations

import html
import logging
import os
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
        """Strip the bot token out of anything headed for the log.

        `requests` embeds the full request URL in HTTPError /
        ConnectionError messages, and ours is
        `https://api.telegram.org/bot<TOKEN>/sendMessage` — so logging
        the raw exception wrote the bot token into quant_agent.log (and
        the systemd journal) on every Telegram failure. A wrong or
        rotated token is the most likely failure, i.e. the token leaked
        exactly when the operator was most likely to share the log.

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
            return text
        except Exception:  # noqa: BLE001
            return "<unprintable error>"

    def send(
        self,
        text: str,
        link_url: str | None = None,
        link_label: str | None = None,
    ) -> bool:
        """Fire-and-forget send. Returns True on success.

        - No-op when not enabled (returns False).
        - Escapes `text` and sends with `parse_mode="HTML"` — a stray
          underscore/asterisk/`<`/`&` in a ticker or a rationale must not
          corrupt the message or get the whole send rejected by Telegram.
        - Appends a tap-through `<a href>` link when one is available:
          `link_url` if given, else `self.mission_control_url` (from
          config). Neither set → no link, ever — never a broken one.
        - Auto-truncates messages over MAX_MESSAGE_CHARS on a sentence/word
          boundary (see `_clip_text`) rather than mid-word.
        - Any HTTP / network / Telegram-side error is logged and
          swallowed: trading must never fail because a notifier is
          unreachable.
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
            return False

        payload = self._build_payload(text, link_url, link_label)

        try:
            response = requests.post(
                self.API_URL.format(token=self.token),
                json=payload,
                timeout=self.HTTP_TIMEOUT_S,
            )
            response.raise_for_status()
            return True
        except Exception as exc:
            # Catch broadly on purpose — TelegramNotifier is a
            # best-effort side channel. A 429 rate-limit, a 5xx, a
            # connection reset, a DNS failure, a bad token — none of
            # those should bubble up and crash the trading session.
            logger.warning("Telegram notify failed: %s", self._redact(exc))
            return False

    def _api_url(self, method: str) -> str:
        """Bot API endpoint for `method` (sendMessage, deleteMessage, ...)."""
        return f"{self.API_BASE.format(token=self.token)}/{method}"

    def _build_payload(
        self,
        text: str,
        link_url: str | None = None,
        link_label: str | None = None,
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
        escaped = html.escape(text)

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
        if len(escaped) > budget:
            # `_clip_text` never splits an HTML entity: it only ever cuts on
            # whitespace, and an entity like '&amp;' has none inside it — so
            # the boundary search always lands on a word start/end, keeping
            # any entity in the kept text whole. This is the last-resort
            # safety net for the rare aggregate message still oversized
            # after every field-level clip below already ran — not the
            # primary fix, which is raising those per-field limits.
            escaped = _clip_text(escaped, budget, marker="\n[...truncated]")
        final_text = escaped + link_html

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

    def send_document(self, csv_bytes: bytes, filename: str, caption: str = "") -> bool:
        """Send a file (e.g. CSV) via Telegram sendDocument. Best-effort."""
        if not self.enabled:
            return False
        try:
            response = requests.post(
                f"https://api.telegram.org/bot{self.token}/sendDocument",
                data={"chat_id": self.chat_id, "caption": caption},
                files={"document": (filename, csv_bytes, "text/csv")},
                timeout=30.0,
            )
            response.raise_for_status()
            return True
        except Exception as exc:
            logger.warning("Telegram send_document failed: %s", self._redact(exc))
            return False


# === Session result formatting ===
# Built as a free function (not a TelegramNotifier method) so it's
# easy to unit-test without the network stub and so main.py can
# compute the message before deciding to send.

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

    timestamp = et_now().strftime("%Y-%m-%d %H:%M ET")
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
            f"🔴 {mode} FAILED  ({timestamp})\n"
            f"error: {err_type}: {err_msg}\n"
            f"elapsed: {elapsed_str}"
        )

    if not isinstance(result, dict):
        return (
            f"⚪ {mode} returned non-dict result ({timestamp})\n"
            f"type: {type(result).__name__}\n"
            f"elapsed: {elapsed_str}"
        )

    status = str(result.get("status", "unknown"))

    # === Per-mode noise policy ===
    if mode == "intra_check" and status in ("ok", "market_holiday"):
        return None  # silent — would otherwise be 14 pings/day
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
    lines: list[str] = [
        f"{emoji} {mode}  ({timestamp})",
        f"status: {status}",
        f"run_id: {run_id}",
    ]

    # Per-session LLM cost (looked up from agent_logs by run_id). Shows
    # for every mode that ran agents — operator wants to see the
    # dollar spend alongside the orders. Returns None silently if no
    # DB or no rows; we omit the line rather than render "$?.??" mid
    # success-message noise.
    cost_line = _session_cost_line(run_id)
    if cost_line:
        lines.append(cost_line)

    # Prepaid OpenRouter balance, once a day, on the morning message only.
    # Owner request 2026-08-31: he wants to see the balance falling rather
    # than discover it empty. OpenRouter is PREPAID — when the credit runs
    # out the desk stops mid-session, at whatever moment the money ends. On
    # 2026-08-31 the account was down to $7.10, about seven clean trading
    # days, and nothing in the system would have said so.
    # Morning only: it changes slowly, and repeating it on every session
    # would train the operator to skim past it.
    # Day-to-date against the self-imposed brake. Shown on every session that
    # spent money, because that is when "how close am I" is actually being
    # asked. Distinct from the balance line below, which is real money.
    day_line = _day_cost_line()
    if day_line and cost_line:
        lines.append(day_line)

    if mode in ("morning", "once"):
        balance_line = _openrouter_balance_line()
        if balance_line:
            lines.append(balance_line)

    # === Mode-specific body ===
    if mode in ("morning", "midday", "close", "once"):
        _append_trade_session_body(lines, result)
    elif mode == "evening":
        _append_evening_body(lines, result)
    elif mode == "earnings_preprocess":
        _append_earnings_body(lines, result)
    elif mode == "intra_check":
        _append_intra_check_body(lines, result)
    elif mode == "meta":
        _append_meta_body(lines, result)
    elif mode == "daily":
        # Only error / skipped reach here ("sent" is silenced above).
        # Surface the failure reason — a bare '🔴 status: error' is
        # undebuggable from a phone.
        filename = result.get("filename", "")
        if filename:
            lines.append(f"📊 {result.get('rows', '?')} rows → {filename}")
        err = result.get("error")
        if err:
            lines.append(f"error: {err}")

    lines.append(f"elapsed: {elapsed_str}")
    return "\n".join(lines)


def _append_coverage_gap_banner(lines: list[str], result: dict) -> None:
    """Render the broker-truth stop-coverage gap banner (🔴) when the session-
    entry reconciler found held longs with less open protective-stop coverage
    than held qty — a (partially) naked position the WAL queue didn't know
    about. This is operator-actionable: a stop needs manual re-protection."""
    gaps = result.get("stop_coverage_gaps")
    if not isinstance(gaps, list) or not gaps:
        return
    parts = []
    for g in gaps[:6]:
        if not isinstance(g, dict):
            continue
        parts.append(
            f"{g.get('symbol', '?')}"
            f"({_fmt_qty(g.get('covered_qty', 0) or 0)}/{_fmt_qty(g.get('held_qty', 0) or 0)})"
        )
    lines.append(
        f"🔴 STOP-COVERAGE GAP: {len(gaps)} long(s) under-protected "
        f"(covered/held): {', '.join(parts)}"
    )


def _append_company_identities(lines: list[str], orders: list) -> None:
    """One line per traded symbol: who the company is.

    The operator reads `BUY CCJ qty=40 @$58.10` and has to already know that
    CCJ is Cameco. Name and industry only — deliberately NOT the business
    summary that goes to the PM. A Telegram message is 4096 characters and
    competes for a phone screen; ten paragraphs of company description would
    push the order list itself out of view.

    `allow_fetch=False` is not an optimisation, it is the contract: an
    operator alert must never sit waiting on a network call. By the time an
    alert goes out the PM path has already warmed the cache for exactly
    these symbols, so this is a dictionary lookup. Symbols the cache does
    not know are silently skipped rather than rendered as unknowns.
    """
    symbols = []
    for o in orders:
        if not isinstance(o, dict):
            continue
        symbol = str(o.get("symbol") or "").strip().upper()
        if symbol and symbol not in symbols:
            symbols.append(symbol)
    if not symbols:
        return
    try:
        from src.data.company import CompanyProfileStore
        profiles = CompanyProfileStore().get_many(
            symbols[:12], allow_fetch=False,
        )
    except Exception as e:  # noqa: BLE001 — never lose an alert over prose
        logger.warning("notifier: company profiles unavailable: %s", e)
        return
    identities = []
    for symbol in symbols[:12]:
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
    if status == "paid_analysis_suspended":
        lines.append(
            "🔴 Paid LLM analysis is suspended by the mandatory cost circuit. "
            "Broker protection and deterministic safety work remain active."
        )
        err = result.get("error")
        if err:
            # 900, not 300 — this is the deterministic cost-circuit
            # breaker's trigger detail, often a multi-clause sentence
            # (which ceiling, current spend, provider) worth reading in full.
            lines.append(f"trigger: {_clip_text(str(err), 900)}")
    elif status.startswith("pm_") or status == "analysis_error":
        lines.append(
            f"🔴 PM decision failed ({status}) — no decisions were made; "
            "this is NOT a deliberate hold and the full paid stack will not auto-repeat"
        )
        err = result.get("error")
        if err:
            lines.append(f"error: {_clip_text(str(err), 900)}")

    # System-health first: a naked long is more urgent than the order list.
    _append_coverage_gap_banner(lines, result)
    orders = result.get("orders") or []

    # FORCE_DELEVER / EMERGENCY_SELL / EMERGENCY_COVER banner — these
    # actions mean the autonomous loop intervened automatically.
    # force_delever fires when cash < -$1 (margin disabled) and
    # biggest-loser-first sells until cash >= 0. emergency_sell fires from
    # intra_check's / midday's flash-crash protection closing a long;
    # emergency_cover is the same circuit breaker covering a SHORT (a
    # distinct action name — not "emergency_sell" — because it's a BUY,
    # and reusing the SELL name here would also have to be reused in
    # db.py's realized-P&L FIFO lot matching, which assumes a "sell-family"
    # action closes a long against open BUY lots; a short has no BUY lot to
    # match against). All three look identical to a routine order on the
    # wire otherwise — operator's most important "system intervened"
    # signal would be invisible without this banner. Prepended before the
    # order list so it's the first thing read.
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
        _append_company_identities(lines, orders)
    else:
        lines.append("orders: 0")

    data_status = result.get("data_status") or {}
    degraded = [k for k, v in data_status.items() if v not in ("ok", "empty")]
    if degraded:
        lines.append(f"⚠️ degraded: {', '.join(sorted(degraded))}")


def _append_evening_body(lines: list[str], result: dict) -> None:
    # === Escalation banners (first thing read, before Daily P&L) ===
    analysis = result.get("analysis")

    # (0) Dead-man's check: a market-day session that left zero agent_logs
    # today silently never ran (disabled timer, stuck lock, half-day window
    # math). morning missing is unambiguous → 🔴; midday/close can be
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
            lines.append(f"🔴 MORNING SESSION INCOMPLETE TODAY: {detail}")
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

    # (2) DETERMINISTIC escalation — does NOT depend on the LLM correctly
    # grading its own day (under-rating is exactly the failure you most want
    # caught). If today's loss is within 80% of the hard daily-loss circuit-
    # breaker limit, raise the banner regardless of risk_rating. Mirrors the
    # trading path's two-layer (hard rule OR LLM) philosophy — the observability
    # path should escalate on facts too, not just on model judgment.
    # Use the SAME basis the headline shows: prefer the 4pm close-to-close P&L
    # (esc_pnl=pnl_4pm, baseline=prior official close = equity_close - pnl_4pm)
    # so the alert evaluates the number the operator actually sees. Fall back to
    # the real-time diff when the 4pm figures aren't available. Without this, a
    # day that recovered after-hours could hide a material 4pm loss from the
    # alert (or vice-versa).
    esc_pnl = result.get("pnl_4pm")
    esc_close = result.get("equity_close")
    if esc_pnl is not None and isinstance(esc_close, (int, float)):
        esc_base = esc_close - esc_pnl
    else:
        esc_pnl = result.get("daily_pnl")
        esc_tv = result.get("total_value")
        esc_base = (esc_tv - esc_pnl) if (
            isinstance(esc_pnl, (int, float)) and isinstance(esc_tv, (int, float))
        ) else None
    dl_limit = result.get("max_daily_loss_pct")
    if (isinstance(esc_pnl, (int, float)) and isinstance(esc_base, (int, float))
            and isinstance(dl_limit, (int, float)) and dl_limit > 0
            and esc_pnl < 0 and esc_base > 0):
        loss_pct = abs(esc_pnl / esc_base * 100)
        if loss_pct >= 0.8 * dl_limit:
            lines.append(
                f"🚨 DETERMINISTIC ALERT — daily loss {loss_pct:.2f}% is "
                f"≥80% of the {dl_limit:.0f}% circuit-breaker limit"
            )

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

    def _fmt_pnl(v: float) -> str:
        return f"+${v:,.2f}" if v >= 0 else f"-${abs(v):,.2f}"

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

    # Suggested actions — surfaced HIGH in the message (right after the
    # headline P&L) so the tail-clip truncation in send() can never eat
    # them. On exactly the high-risk days where these are populated the
    # message is longest, and these are the lines most worth reading.
    # Only shown when risk_rating is elevated/high. (The P&L history
    # text table that used to follow was replaced by the daily CSV
    # export — PR #99.)
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

    # Position snapshot: total invested + cash + top winners/losers.
    # Helper queries the live DB so this works regardless of how the
    # evening result dict is constructed.
    _append_position_snapshot(lines, total_value)

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

    # Auto-meta piggyback (Round 2 enabled this; Round 6 adds the
    # dry-run staging hint). When today is the last trading day of a
    # quarter, run_evening invokes run_quarterly_meta_reflection and
    # stuffs the result into `result['auto_meta']`. Surface dry-run
    # proposals so the operator knows to review proposed_edits.json
    # before next quarter.
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
            columns = {
                row[1] for row in conn.execute("PRAGMA table_info(agent_logs)").fetchall()
            }
            if "provider_requests" in columns:
                rows = conn.execute(
                    "SELECT cost_usd, provider_requests FROM agent_logs WHERE run_id = ?",
                    (run_id,),
                ).fetchall()
                requests = sum(
                    1 if row[1] is None else max(0, int(row[1])) for row in rows
                )
            else:
                rows = conn.execute(
                    "SELECT cost_usd FROM agent_logs WHERE run_id = ?",
                    (run_id,),
                ).fetchall()
                requests = len(rows)
        finally:
            conn.close()
    except Exception as exc:
        logger.warning("session cost lookup failed for %s: %s", run_id, exc)
        return None
    if not rows:
        return None
    if any(r[0] is None for r in rows):
        # Unknown model in pricing table for at least one call →
        # cannot honestly sum. Surface a hint instead of a fake number.
        return f"💵 cost: $?.?? ({requests} provider requests — see cost_table.py)"
    total = sum(float(r[0]) for r in rows)
    # Cents-or-better precision for human readability; sub-cent
    # sessions (rare, e.g. intra_check with 0 LLM calls — but those
    # don't reach this code path anyway) use 4-decimal.
    if total < 0.01:
        return f"💵 cost: ${total:.4f} ({requests} provider requests)"
    return f"💵 cost: ${total:,.2f} ({requests} provider requests)"


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
    if not limit:
        return f"📅 today: ${spent:,.2f} so far"
    pct = int(round(spent / limit * 100))
    return f"📅 today: ${spent:,.2f} of ${limit:,.2f} daily limit ({pct}%)"


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
        sign = "+" if pnl >= 0 else "-"
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
    analyzed = result.get("analyzed", 0)
    confirmed = result.get("confirmed", 0)
    failed = result.get("failed", 0)
    lines.append(f"analyzed: {analyzed}  confirmed: {confirmed}  failed: {failed}")


def _append_intra_check_body(lines: list[str], result: dict) -> None:
    # Only reaches here when status != ok/market_holiday — operator
    # wants the details of whatever triggered.
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
    if status in ("emergency_sold", "hard_risk_block", "digest_only"):
        return "🟡"
    if ("error" in status or status.startswith("pm_")
            or status in ("rejected", "failed", "paid_analysis_suspended")):
        return "🔴"
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
