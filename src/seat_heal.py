"""Self-heal a missing or broken research seat before the desk freezes.

Order (owner 2026-09-16):
  1. Mechanical code heal first — restore a stated soft-exit that a null
     wipe dropped; coerce MacroAnalysis shape (dict ``sector_guidance`` →
     list; stored trim vs live model). Never invent thesis/catalyst/macro
     text. Never loosen validation so garbage parses as ok.
  2. At most ONE paid retry for that seat, inside the cost circuit's
     session/day caps. WHICH ledger "one" counts against is the caller's,
     not this module's — see `can_paid_retry`. The research-seat heal
     (`TradingPipeline._try_one_paid_research_retry`) counts durably PER ET
     TRADING DAY, because its per-tick predecessor bought the news seat
     eight times in one day. The exit-trigger re-ask and the PM-accounting
     heal still count per tick, deliberately: each is reachable once per
     run. Do not read "per day" as a property of every heal.
     A seat that is EXPIRED rather than lost is eligible too — the desk
     holds an older answer and is buying a fresher one — and its owner
     alerts must say so rather than claim a seat was lost
     (`HealResult.owner_consequence`).
  3. Durable machine-readable reason (which seat, why). Success is a log
     row, not a page. Heal FAILURE and a spend-cap block each get their
     OWN Telegram OWNER ALERT.

After heal, continue only if the seat has real usable output — a green
empty after a heal is still empty. Do not re-pay a remembered GOOD /
chose-not-to-refetch seat. Do not retry past the cap.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from src.models import (
    SECTOR_DIRECTIONS,
    SECTOR_STANCE_TO_DIRECTION,
    _ALLOWED_SECTORS,
    _SECTOR_ALIASES,
    normalize_sector_stance,
)

logger = logging.getLogger(__name__)

HEAL_MECHANICAL = "mechanical"
HEAL_PAID_RETRY = "paid_retry"
HEAL_FAILED = "failed"
HEAL_SKIPPED_GOOD = "skipped_good"
HEAL_CAP_BLOCKED = "cap_blocked"
#: The seat already had its one paid heal this ET trading day. Distinct from
#: HEAL_CAP_BLOCKED, which is the cost circuit refusing a spend: this is the
#: heal's own per-day allowance, and it is recorded rather than only logged
#: because a decision not to spend is still a decision about money.
HEAL_DAY_CAP = "day_cap"

# Reverse of SECTOR_STANCE_TO_DIRECTION for restoring the live model shape
# from MacroStore's {sector: bullish|neutral|bearish} snapshot. Not an
# invented stance — it is the same map, run backwards.
_DIRECTION_TO_STANCE: dict[str, str] = {
    "bullish": "overweight",
    "bearish": "underweight",
    "neutral": "neutral",
}
for _stance, _direction in SECTOR_STANCE_TO_DIRECTION.items():
    _DIRECTION_TO_STANCE.setdefault(_direction, _stance)


@dataclass
class HealResult:
    """One seat's heal attempt. Machine-readable; persist via to_evidence()."""

    seat: str
    outcome: str
    reason: str
    #: In-memory only, and deliberately NOT in `to_evidence()`. The paid
    #: heal's answer is persisted by
    #: `TradingPipeline._persist_heal_call` through the SAME two rows an
    #: ordinary paid call writes — `agent_logs` (model, tokens, cost, raw
    #: answer) and `specialist_evidence(kind="analysis")`. Copying it in here
    #: too would duplicate it into a row that
    #: `Database.count_paid_seat_heals_today` json-parses on every heal.
    payload: object | None = None
    mechanical: bool = False
    paid_retry: bool = False
    usable: bool = False
    details: dict = field(default_factory=dict)
    #: What this failure means FOR THE OWNER, in his words, when the default
    #: sentence below would be false for this seat. Added 2026-09-18 (board
    #: item 133): the default body says "the desk will not decide on this
    #: seat as if it had answered", which is true of a lost research seat
    #: and false of a bookkeeping heal, where the decision was already made
    #: and only the explanation is missing. Telling him a trade was withheld
    #: when it was not is the owner-facing-message class of defect this desk
    #: is already fixing elsewhere (item 89). Empty keeps the default.
    owner_consequence: str = ""

    def to_evidence(self) -> dict:
        return {
            "gate": "seat_heal",
            "seat": self.seat,
            "outcome": self.outcome,
            "reason": self.reason,
            "mechanical": self.mechanical,
            "paid_retry": self.paid_retry,
            "usable": self.usable,
            "details": dict(self.details),
        }


def restore_stated_soft_exits(values: dict, raw: dict | None) -> tuple[dict, list[str]]:
    """Put back a stated thesis_invalid_if / catalyst that a null-wipe dropped.

    Only restores a NON-EMPTY string present on the raw model output.
    Does not invent 'don't know' or any other placeholder. Empty/null on
    the raw stays empty.
    """
    if not isinstance(values, dict):
        return values, []
    if not isinstance(raw, dict):
        return values, []
    restored: list[str] = []
    out = dict(values)
    for field_name in ("thesis_invalid_if", "catalyst"):
        raw_val = raw.get(field_name)
        if not isinstance(raw_val, str) or not raw_val.strip():
            continue
        if raw_val.strip().lower() == "unknown":
            continue
        current = out.get(field_name)
        if (
            current is None
            or current == ""
            or (
                isinstance(current, str)
                and current.strip().lower() == "unknown"
            )
        ):
            out[field_name] = raw_val
            restored.append(field_name)
    return out, restored


def merge_retry_falsifiers(original_targets: list, retry_targets: list) -> tuple[list, list[str]]:
    """Copy a stated thesis_invalid_if from a paid retry onto empty opens.

    Never invents text. Never overwrites a string that was already stated.
    Never copies catalyst — that field stays optional except the dated
    unmeasurable-range exception already gated in Python. Returns
    (targets, symbols filled).
    """
    from src.models import stated_soft_exit

    if not original_targets:
        return original_targets, []
    retry_by_symbol: dict[str, object] = {}
    for item in retry_targets or []:
        symbol = _target_symbol(item)
        if symbol:
            retry_by_symbol[symbol] = item
    filled: list[str] = []
    out = []
    for target in original_targets:
        symbol = _target_symbol(target)
        current = _target_field(target, "thesis_invalid_if")
        if stated_soft_exit(current) or _target_is_close(target):
            out.append(target)
            continue
        retry_item = retry_by_symbol.get(symbol) if symbol else None
        retry_val = (
            _target_field(retry_item, "thesis_invalid_if")
            if retry_item is not None else None
        )
        stated = stated_soft_exit(retry_val)
        if not stated:
            out.append(target)
            continue
        out.append(_set_target_falsifier(target, stated))
        filled.append(symbol)
    return out, filled


def _target_symbol(target) -> str:
    if isinstance(target, dict):
        return str(target.get("symbol") or "").strip().upper()
    return str(getattr(target, "symbol", "") or "").strip().upper()


def _target_field(target, name: str):
    if isinstance(target, dict):
        return target.get(name)
    return getattr(target, name, None)


def _target_is_close(target) -> bool:
    if hasattr(target, "is_close"):
        try:
            return bool(target.is_close)
        except Exception:
            return False
    risk = _target_field(target, "risk_allocation_pct")
    weight = _target_field(target, "target_weight_pct")
    try:
        if risk is not None:
            return float(risk) == 0.0
        if weight is not None:
            return float(weight) == 0.0
    except (TypeError, ValueError):
        return False
    return False


def _set_target_falsifier(target, stated: str):
    if isinstance(target, dict):
        copied = dict(target)
        copied["thesis_invalid_if"] = stated
        return copied
    target.thesis_invalid_if = stated
    return target


def coerce_sector_guidance(raw) -> list[dict]:
    """Dict or list sector_guidance → list[{sector, stance, reason}].

    Mechanical shape only. A dict value is the stored MacroStore snapshot
    ({sector: bullish|neutral|bearish}); a list is the live model. Reason
    is kept when present and left empty when the snapshot never stored one
    — that empty is not invented analysis text.
    """
    cleaned: list[dict] = []
    if isinstance(raw, dict):
        items = []
        for sector, direction in raw.items():
            items.append({
                "sector": sector,
                "stance": _DIRECTION_TO_STANCE.get(str(direction or "").strip().lower(), direction),
                "reason": "",
            })
        raw = items
    if not isinstance(raw, list):
        return cleaned
    for item in raw:
        if not isinstance(item, dict):
            continue
        sec = item.get("sector")
        if not isinstance(sec, str):
            continue
        canon = _SECTOR_ALIASES.get(sec.strip().lower(), sec.strip())
        if canon not in _ALLOWED_SECTORS:
            continue
        stance = item.get("stance")
        mapped = normalize_sector_stance(stance)
        if mapped is None and isinstance(stance, str) and stance.strip().lower() in (
            "overweight", "neutral", "underweight",
        ):
            mapped_stance = stance.strip().lower()
        elif mapped in SECTOR_DIRECTIONS:
            mapped_stance = _DIRECTION_TO_STANCE.get(mapped, "neutral")
        elif isinstance(stance, str) and stance.strip().lower() in (
            "overweight", "neutral", "underweight",
        ):
            mapped_stance = stance.strip().lower()
        else:
            continue
        reason = item.get("reason")
        cleaned.append({
            "sector": canon,
            "stance": mapped_stance,
            "reason": reason if isinstance(reason, str) else "",
        })
    return cleaned


def coerce_macro_shape(payload: dict) -> tuple[dict, list[str]]:
    """Coerce a stored or live macro dict toward MacroAnalysis's shape.

    Does NOT invent reasoning_chain, summary, regime, or any other prose.
    Returns (possibly-copied dict, list of mechanical fixes applied).
    Prefers `sector_guidance_rows` (live list persisted by MacroStore)
    over the compact dict snapshot.
    """
    if not isinstance(payload, dict):
        return payload, []
    fixes: list[str] = []
    out = dict(payload)
    rows = out.get("sector_guidance_rows")
    sg = out.get("sector_guidance")
    if isinstance(rows, list) and rows:
        coerced = coerce_sector_guidance(rows)
        out["sector_guidance"] = coerced
        fixes.append("sector_guidance_rows_to_list")
    elif isinstance(sg, dict):
        out["sector_guidance"] = coerce_sector_guidance(sg)
        fixes.append("sector_guidance_dict_to_list")
    elif isinstance(sg, list):
        coerced = coerce_sector_guidance(sg)
        if coerced != sg:
            out["sector_guidance"] = coerced
            fixes.append("sector_guidance_sanitized")
    return out, fixes


def describe_macro_parse_failure(payload, error: BaseException) -> str:
    """Durable, machine-readable reason a MacroAnalysis re-parse failed.

    Never a generic 'failed to parse'. Names the missing/wrong field.
    Does not invent a chain to make garbage validate.
    """
    missing: list[str] = []
    if not isinstance(payload, dict):
        return f"macro_parse_failed: payload_not_dict ({type(payload).__name__})"
    if not isinstance(payload.get("reasoning_chain"), dict):
        missing.append("reasoning_chain")
    sg = payload.get("sector_guidance")
    if sg is not None and not isinstance(sg, (list, dict)):
        missing.append("sector_guidance_not_list_or_dict")
    pg = payload.get("position_guidance")
    if isinstance(pg, dict):
        for key in ("reasoning",):
            if key not in pg:
                missing.append(f"position_guidance.{key}")
    elif pg is None:
        missing.append("position_guidance")
    for key in ("regime", "confidence", "equity_outlook", "summary"):
        if not str(payload.get(key) or "").strip():
            missing.append(key)
    err = str(error).split("\n", 1)[0].strip()
    if missing:
        return "macro_parse_failed: missing " + ", ".join(missing) + f" ({err})"
    return f"macro_parse_failed: {err}"


def merge_carried_stock_news(fresher: dict, carried: dict | None) -> dict:
    """Keep per-symbol news the FRESHER read was never asked about.

    A paid news heal is handed the general wire text the expiry peek
    already fetched and NOTHING else — no universe and no
    `stock_mentions` (`TradingPipeline._try_one_paid_research_retry`). The
    morning read had both. So the healed report is the fresher answer about
    the wire, and simultaneously a narrower one about the book: a symbol the
    morning covered and the afternoon wire never mentioned would silently
    lose its coverage if the healed report simply replaced it.

    This fills that gap and only that gap. The fresher report wins
    everywhere it spoke — every field, and every symbol for which it
    returned a non-empty list. A symbol it has no key for at all keeps the
    carried entry. A symbol it answered with an EMPTY list keeps that empty,
    because an explicit empty is the seat saying "nothing on this name", not
    an omission (`NewsIntelligenceReport.dropped_news_symbols` is the field
    that distinguishes them, and `analyze()` fills those keys deliberately).

    Nothing here merges `state_changes`, `macro_narrative` or `pm_briefing`.
    Those are one read's reasoning and splicing two of them would invent a
    report neither seat produced. Mechanical, per-symbol, never raises.
    """
    if not isinstance(fresher, dict):
        return fresher
    if not isinstance(carried, dict):
        return fresher
    fresh_news = fresher.get("stock_news")
    carried_news = carried.get("stock_news")
    if not isinstance(carried_news, dict) or not carried_news:
        return fresher
    merged = dict(fresh_news) if isinstance(fresh_news, dict) else {}
    for symbol, items in carried_news.items():
        if symbol in merged:
            continue
        merged[symbol] = items
    out = dict(fresher)
    out["stock_news"] = merged
    return out


def wire_titles_shown_to_model(titles, news_text: str) -> list[str]:
    """The peeked headlines the model was ACTUALLY handed, by measurement.

    Marking a headline "already covered" stops it expiring the news seat
    again, so the set has to be the wire the desk PAID the model to read —
    not the wire it happened to fetch. Those differ: the prompt text is
    truncated to `news.max_prompt_items`, so a peek can fetch more titles
    than the re-ask ever saw, and suppressing an unseen headline would be
    buying silence instead of research.

    So the bound is read off the prompt itself rather than chosen: a title
    counts only when it appears in the text handed to the analyst. No cap,
    no count, no clock. Empty text covers nothing.
    """
    text = str(news_text or "")
    if not text.strip():
        return []
    out: list[str] = []
    seen: set[str] = set()
    for raw in titles or []:
        title = str(raw or "").strip()
        if not title or title in seen:
            continue
        if title in text:
            seen.add(title)
            out.append(title)
    return out


def can_paid_retry(retries_used: dict[str, int], seat: str, *, max_retries: int = 1) -> bool:
    """At most one paid retry per seat, per whatever `retries_used` counts.

    The caller supplies the ledger, and which ledger it supplies IS the
    scope. `TradingPipeline._try_one_paid_research_retry` asks twice: once
    with `RunContext.heal_paid_retries` (one tick) and once with the durable
    per-ET-day count from `Database.count_paid_seat_heals_today`. Both must
    allow it before the desk pays.

    The day ledger exists because this docstring used to say "per session"
    while the only caller passed a per-TICK dict, and `intra_check` runs
    every 30 minutes. That was not theoretical: production shows EIGHT paid
    news heals on 2026-09-18 [measured 2026-09-23, `kind='seat_heal'` rows
    in `specialist_evidence`], each one believing it was the only retry of
    the session. The number 1 is unchanged and is not a new number — only
    the ledger it counts against was wrong.
    """
    used = int(retries_used.get(seat, 0) or 0)
    return used < max_retries


def record_paid_retry(retries_used: dict[str, int], seat: str) -> dict[str, int]:
    out = dict(retries_used)
    out[seat] = int(out.get(seat, 0) or 0) + 1
    return out


def heal_failure_alert_text(result: HealResult, *, cap_blocked: bool = False) -> str:
    """Own-message body for a heal failure or spend-cap block. Never empty."""
    consequence = (result.owner_consequence or "").strip() or (
        "The desk will not decide on this seat as if it had answered."
    )
    if cap_blocked:
        # `owner_consequence` is honoured here too, 2026-09-23. This branch
        # used to return before reading it, so a spend-cap block on an
        # EXPIRED seat told the owner the desk "could not replace a lost or
        # empty research seat" — the seat was neither. That is the
        # owner-facing-lie class of defect item 133 closed on the other
        # branch, still live on this one.
        # Keyed on the fact, not on whether someone remembered to set the
        # sentence: `details["was_expired"]` is written by the heal path
        # itself.
        was_expired = bool(result.details.get("was_expired"))
        if was_expired:
            subject = (
                "buy a fresher answer for a seat whose research it already "
                "holds"
            )
            # "not treated as green-empty" is lost-seat wording and means
            # nothing for a seat that was never empty.
            reassurance = ""
        else:
            subject = "replace a lost or empty research seat"
            reassurance = "The seat was not treated as green-empty. "
        return (
            f"OWNER ALERT — research heal blocked by spend cap\n"
            f"Seat: {result.seat}\n"
            f"The desk could not pay a one-time retry to {subject} because "
            f"the session or day cost cap is already bound. {reassurance}"
            f"{consequence} "
            f"Reason: {result.reason}"
        )
    return (
        f"OWNER ALERT — research heal failed\n"
        f"Seat: {result.seat}\n"
        f"Mechanical repair did not produce usable output"
        f"{' and the one paid retry also failed' if result.paid_retry else ''}. "
        f"{consequence} "
        f"Reason: {result.reason}"
    )
