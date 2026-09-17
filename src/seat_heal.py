"""Self-heal a missing or broken research seat before the desk freezes.

Order (owner 2026-09-16):
  1. Mechanical code heal first — restore a stated soft-exit that a null
     wipe dropped; coerce MacroAnalysis shape (dict ``sector_guidance`` →
     list; stored trim vs live model). Never invent thesis/catalyst/macro
     text. Never loosen validation so garbage parses as ok.
  2. At most ONE paid retry for that LOST/empty seat, inside session/day
     cost caps.
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
    payload: object | None = None
    mechanical: bool = False
    paid_retry: bool = False
    usable: bool = False
    details: dict = field(default_factory=dict)

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
        for key in ("target_invested_pct", "cash_recommendation_pct", "reasoning"):
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


def mechanical_heal_macro(payload) -> HealResult:
    """Try to make a macro payload usable without a paid call.

    Coerce dict ``sector_guidance`` / stored rows toward MacroAnalysis's
    list shape. Do not fill reasoning_chain from summary (that would
    invent macro text). After coerce, the payload must still validate as
    MacroAnalysis — otherwise this is a durable fail, not a remembered
    regime passed silently into PM.
    """
    if payload is None:
        return HealResult(
            seat="macro", outcome=HEAL_FAILED,
            reason="no macro payload to heal",
        )
    if not isinstance(payload, dict):
        # Already a MacroAnalysis (or similar) — present is usable.
        return HealResult(
            seat="macro", outcome=HEAL_SKIPPED_GOOD,
            reason="macro payload is already an object, not a broken dict",
            payload=payload, usable=True,
        )
    if not str(payload.get("regime") or "").strip():
        return HealResult(
            seat="macro", outcome=HEAL_FAILED,
            reason="macro dict has no regime — refusing to treat a failed parse as regime ok",
            payload=payload,
        )
    coerced, fixes = coerce_macro_shape(payload)
    from src.models import MacroAnalysis
    try:
        MacroAnalysis.model_validate(coerced)
    except Exception as exc:
        # Coerce is not a loosen: a trim still missing reasoning_chain is
        # a durable fail, not a remembered regime smuggled into PM.
        return HealResult(
            seat="macro",
            outcome=HEAL_FAILED,
            reason=describe_macro_parse_failure(coerced, exc),
            payload=coerced,
            mechanical=bool(fixes),
            usable=False,
            details={
                "fixes": fixes,
                "has_reasoning_chain": isinstance(
                    coerced.get("reasoning_chain"), dict,
                ),
            },
        )
    return HealResult(
        seat="macro",
        outcome=HEAL_MECHANICAL if fixes else HEAL_SKIPPED_GOOD,
        reason=(
            "coerced stored/live macro shape: " + ", ".join(fixes)
            if fixes else "macro dict already in usable shape"
        ),
        payload=coerced,
        mechanical=bool(fixes),
        usable=True,
        details={"fixes": fixes, "has_reasoning_chain": isinstance(coerced.get("reasoning_chain"), dict)},
    )


def can_paid_retry(retries_used: dict[str, int], seat: str, *, max_retries: int = 1) -> bool:
    """At most one paid retry per seat per session."""
    used = int(retries_used.get(seat, 0) or 0)
    return used < max_retries


def record_paid_retry(retries_used: dict[str, int], seat: str) -> dict[str, int]:
    out = dict(retries_used)
    out[seat] = int(out.get(seat, 0) or 0) + 1
    return out


def heal_failure_alert_text(result: HealResult, *, cap_blocked: bool = False) -> str:
    """Own-message body for a heal failure or spend-cap block. Never empty."""
    if cap_blocked:
        return (
            f"OWNER ALERT — research heal blocked by spend cap\n"
            f"Seat: {result.seat}\n"
            f"The desk could not pay a one-time retry to replace a lost or "
            f"empty research seat because the session or day cost cap is "
            f"already bound. The seat was not treated as green-empty. "
            f"Reason: {result.reason}"
        )
    return (
        f"OWNER ALERT — research heal failed\n"
        f"Seat: {result.seat}\n"
        f"Mechanical repair did not produce usable output"
        f"{' and the one paid retry also failed' if result.paid_retry else ''}. "
        f"The desk will not decide on this seat as if it had answered. "
        f"Reason: {result.reason}"
    )
