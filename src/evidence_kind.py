"""Reuse research by EVIDENCE KIND and expiry EVENT, not by seat-count or a clock.

Owner rule (2026-09-16): pay again only when that kind of fact expired or
was never good. Remember filings/regime across days. Refresh price and news
when the tape/wire moved. Never pay twice for the same unbroken truth.

This module decides whether a stored payload may be reused. It does not
count seats (docs/WORK.md item 20 counting half stays the owner's). It
does not invent a refresh timer. The same-session hours-scale bound for
news is the session itself — already ratified by PR #430 — not a second
fitted N-minute clock.

Kinds and the event that expires them
-------------------------------------
  news       newer material wire, or the session ended (hours-scale)
  chart      live price/levels are always re-read at decision/submit;
             a full paid TA is not "every tick" and is not this module
  macro      a real regime or print change (reusable across days until then)
  earnings   next report, material amendment, or 8-K
  insider    a NEW Form 4 filing (remember the ones already read)

A blank, parse-failed, or LOST payload is never research. Heal it
(see ``src.seat_heal``) or refuse — do not carry it forward as if it ran.
"""

from __future__ import annotations

from dataclasses import dataclass

KIND_NEWS = "news"
KIND_CHART = "chart"
KIND_MACRO = "macro"
KIND_EARNINGS = "earnings"
KIND_INSIDER = "insider"

QUALITY_GOOD = "good"
QUALITY_BLANK = "blank"
QUALITY_LOST = "lost"

DECISION_REUSE = "reuse"
DECISION_REFETCH = "refetch"
DECISION_LOST = "lost"
DECISION_REREAD_LIVE = "reread_live"

# Same-session reuse of a GOOD payload is integrity-clean (PR #430).
# Cross-day remember is a different status so holding-discipline can
# still refuse to treat a prior-day regime as proof about *today*.
STATUS_CARRIED_FROM_MORNING = "carried_from_morning"
STATUS_REMEMBERED = "remembered"
STATUS_CHOSE_NOT_TO_REFETCH = "chose_not_to_refetch"


@dataclass(frozen=True)
class KindReuse:
    """Whether this kind of evidence may be reused on this tick."""

    kind: str
    quality: str
    decision: str
    status: str
    reason: str
    same_session: bool = True

    @property
    def usable(self) -> bool:
        return self.decision in (DECISION_REUSE, DECISION_REREAD_LIVE)

    @property
    def pay_again(self) -> bool:
        return self.decision == DECISION_REFETCH


def payload_quality(payload, *, required_keys: tuple[str, ...] = ()) -> str:
    """Classify a stored payload. Never raises.

    GOOD: there is a real usable answer.
    BLANK: the seat honestly had nothing to say (quiet Form 4 day).
    LOST: missing, unreadable, or a failed parse — not research.
    """
    if payload is None:
        return QUALITY_LOST
    if payload is False:
        return QUALITY_LOST
    if isinstance(payload, str):
        return QUALITY_LOST if not payload.strip() else QUALITY_GOOD
    if isinstance(payload, (list, tuple)):
        return QUALITY_BLANK if not payload else QUALITY_GOOD
    if isinstance(payload, dict):
        if not payload:
            return QUALITY_LOST
        if required_keys and any(not payload.get(k) for k in required_keys):
            return QUALITY_LOST
        return QUALITY_GOOD
    # Pydantic models and other objects: present and non-false.
    return QUALITY_GOOD


def _lost(kind: str, quality: str, *, same_session: bool, why: str) -> KindReuse:
    return KindReuse(
        kind=kind, quality=quality, decision=DECISION_LOST,
        status="carry_forward_failed" if quality == QUALITY_LOST else "carry_forward_empty",
        reason=why, same_session=same_session,
    )


def news_reuse(
    payload,
    *,
    same_session: bool,
    newer_material_wire: bool,
) -> KindReuse:
    """News/wire: same-session GOOD reuse unless a newer material wire landed.

    The hours-scale bound is the session (PR #430). A second fitted clock
    is forbidden. Cross-session news is expired: yesterday's wire is not
    this session's wire.
    """
    quality = payload_quality(payload)
    if quality != QUALITY_GOOD:
        return _lost(
            KIND_NEWS, quality, same_session=same_session,
            why="news payload is blank, missing, or unreadable — not reusable research",
        )
    if newer_material_wire:
        return KindReuse(
            kind=KIND_NEWS, quality=quality, decision=DECISION_REFETCH,
            status="expired",
            reason="newer material wire superseded the remembered news",
            same_session=same_session,
        )
    if not same_session:
        return KindReuse(
            kind=KIND_NEWS, quality=quality, decision=DECISION_REFETCH,
            status="expired",
            reason="news is session-scoped (hours); a prior session's wire has expired",
            same_session=False,
        )
    return KindReuse(
        kind=KIND_NEWS, quality=quality, decision=DECISION_REUSE,
        status=STATUS_CARRIED_FROM_MORNING,
        reason="same-session GOOD news reused; no newer material wire",
        same_session=True,
    )


def macro_reuse(
    payload,
    *,
    same_session: bool,
    regime_or_print_changed: bool,
) -> KindReuse:
    """Macro regime: reusable across days until a real regime/print change.

    A failed parse is never a regime. Same-session GOOD reuse stays #430.
    """
    quality = payload_quality(payload, required_keys=("regime",))
    if quality != QUALITY_GOOD:
        return _lost(
            KIND_MACRO, quality, same_session=same_session,
            why="macro payload is blank, missing, or unreadable — not a regime",
        )
    if regime_or_print_changed:
        return KindReuse(
            kind=KIND_MACRO, quality=quality, decision=DECISION_REFETCH,
            status="expired",
            reason="regime or print changed — remembered macro has expired",
            same_session=same_session,
        )
    if same_session:
        return KindReuse(
            kind=KIND_MACRO, quality=quality, decision=DECISION_REUSE,
            status=STATUS_CARRIED_FROM_MORNING,
            reason="same-session GOOD macro reused; no regime/print change",
            same_session=True,
        )
    return KindReuse(
        kind=KIND_MACRO, quality=quality, decision=DECISION_REUSE,
        status=STATUS_REMEMBERED,
        reason="GOOD macro remembered across days; no regime/print change",
        same_session=False,
    )


def earnings_reuse(
    payload,
    *,
    same_session: bool,
    new_report_or_8k: bool,
) -> KindReuse:
    """Earnings write-up: remember until the next report or material 8-K."""
    quality = payload_quality(payload)
    if quality == QUALITY_LOST:
        return _lost(
            KIND_EARNINGS, quality, same_session=same_session,
            why="earnings payload is missing or unreadable — not a write-up",
        )
    if new_report_or_8k:
        return KindReuse(
            kind=KIND_EARNINGS, quality=QUALITY_GOOD if quality == QUALITY_GOOD else quality,
            decision=DECISION_REFETCH, status="expired",
            reason="new report, amendment, or 8-K — remembered write-up has expired",
            same_session=same_session,
        )
    # Quiet day (blank list) is an answer: nothing to remember paying for.
    status = (
        STATUS_CARRIED_FROM_MORNING if same_session else STATUS_REMEMBERED
    )
    if quality == QUALITY_BLANK:
        status = STATUS_CHOSE_NOT_TO_REFETCH
    return KindReuse(
        kind=KIND_EARNINGS, quality=quality, decision=DECISION_REUSE,
        status=status,
        reason="earnings write-up remembered; no new report or 8-K",
        same_session=same_session,
    )


def insider_reuse(
    payload,
    *,
    same_session: bool,
    new_form4: bool,
) -> KindReuse:
    """Form 4: remember filings already read; refresh only for NEW filings."""
    quality = payload_quality(payload)
    if quality == QUALITY_LOST:
        return _lost(
            KIND_INSIDER, quality, same_session=same_session,
            why="insider payload is missing or unreadable — not a filing read",
        )
    if new_form4:
        return KindReuse(
            kind=KIND_INSIDER, quality=QUALITY_GOOD if quality == QUALITY_GOOD else quality,
            decision=DECISION_REFETCH, status="expired",
            reason="new Form 4 filing — remembered filings have expired",
            same_session=same_session,
        )
    status = (
        STATUS_CARRIED_FROM_MORNING if same_session else STATUS_REMEMBERED
    )
    if quality == QUALITY_BLANK:
        status = STATUS_CHOSE_NOT_TO_REFETCH
    return KindReuse(
        kind=KIND_INSIDER, quality=quality, decision=DECISION_REUSE,
        status=status,
        reason="Form 4 filings remembered; no new filing",
        same_session=same_session,
    )


def chart_reuse(payload, *, same_session: bool) -> KindReuse:
    """Chart: remember a GOOD TA; always re-read live price/levels at submit.

    A full paid TA is not due every tick. Live price is never remembered.
    """
    quality = payload_quality(payload)
    if quality != QUALITY_GOOD:
        return _lost(
            KIND_CHART, quality, same_session=same_session,
            why="chart payload is blank, missing, or unreadable — not reusable TA",
        )
    return KindReuse(
        kind=KIND_CHART, quality=quality, decision=DECISION_REREAD_LIVE,
        status=STATUS_CARRIED_FROM_MORNING if same_session else STATUS_REMEMBERED,
        reason="GOOD TA remembered; live price/levels must be re-read",
        same_session=same_session,
    )


def covered_news_headlines(report) -> frozenset[str]:
    """Headlines already in a remembered news report. Empty on anything else."""
    out: set[str] = set()
    stock_news = getattr(report, "stock_news", None)
    if isinstance(report, dict):
        stock_news = report.get("stock_news")
    if not isinstance(stock_news, dict):
        return frozenset()
    for items in stock_news.values():
        if not isinstance(items, list):
            continue
        for item in items:
            headline = getattr(item, "headline", None)
            if headline is None and isinstance(item, dict):
                headline = item.get("headline")
            text = str(headline or "").strip()
            if text:
                out.add(text)
    return frozenset(out)


def newer_material_wire(covered: frozenset[str], fetched_headlines: list[str] | tuple[str, ...] | frozenset[str]) -> bool:
    """True when a fetched headline is not already in the remembered report.

    Mechanical ID compare — not an LLM "is this material?" call. A headline
    the morning report already filed is not new. An empty fetch is not a
    supersede (failed fetch must not look like 'the wire moved').
    """
    if not fetched_headlines:
        return False
    incoming = {str(h).strip() for h in fetched_headlines if str(h).strip()}
    if not incoming:
        return False
    return bool(incoming - set(covered))
