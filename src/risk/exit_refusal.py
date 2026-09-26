"""Exit-path refusal policy — one owner, one uncertainty fail direction.

WORK.md item 60 (closed 2026-09-16). The exit path had two layers that
could refuse a SELL/REDUCE/COVER, failing in opposite directions on
uncertainty:

* `_risk_review_exits`: an unavailable, unparseable, or verdict-less Risk
  Manager failed OPEN (owner-ratified 2026-08-27). Failing closed on an
  exit would strand a thesis-invalidated position because a language
  model was unavailable.
* `_reason_cites_hard_trigger`: a case-insensitive substring miss dropped
  the exit — fail CLOSED.

Those are not the same kind of event, and treating them as if they were
is what made the pair look incoherent.

**Owner of refusal: deterministic Python.** A completed content judgment
that the reason did not name a recognised trigger is a refusal by this
owner. So are the fact gates that already run on this loop (noise band,
metric-contradiction veto, proven-false holding-discipline claim). AI
Risk is a challenge seat: it may *add* a refusal when it returns a
parseable reject. It cannot refuse by being silent, and its approval
cannot override a deterministic drop.

**Fail direction on uncertainty: OPEN, for both layers.** Uncertainty
means the layer could not produce a judgment — the Risk Manager raised,
returned no verdict, or the hard-trigger recogniser could not run
(non-string reason, or the matcher itself raised). That is the 2026-08-27
ratification, now applied to the pair rather than to one layer only. A
present reason whose text does not contain a recognised trigger is not
uncertainty: the matcher finished, the answer is no, the owner refuses.

The silent disagreement this item carried is gone because unnamed-trigger
exits are no longer sent to the Risk Manager. Fail-OPEN on a dead model
therefore only ever applies to an exit the owner already let through the
phrase gate — which is the case the 2026-08-27 ratification described
(a thesis-invalidated position that named the invalidation).

This module does not change sale-block appetite. It does not move
`NOISE_BAND_ATR_MULTIPLE`, `BREAK_CONFIRMATION_ATR_MULTIPLE` or
`absolute_min_stop_atr_multiple` (item 70).
It records every drop, every uncertainty fail-open, and (since board item
164, 2026-09-19) every AI Risk approval, as append-only per-symbol
specialist evidence so a later upsert on the cooldown ledger cannot erase
the reason. The kind keeps its historical name; `dropped` and `code` say
which outcome a row is.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Callable, Literal

logger = logging.getLogger(__name__)

#: Forensic kind written to `specialist_evidence`. Append-only; the
#: trading pipeline never reads these rows.
EXIT_REFUSAL_KIND = "exit_refusal"

#: The layer whose completed "no" is binding. AI Risk may add a refusal;
#: it is not this owner.
REFUSAL_OWNER = "deterministic"

#: Shared uncertainty posture for both layers of the item-60 pair.
UNCERTAINTY_FAIL = "open"

TriggerJudgment = Literal["named", "unnamed", "uncertain"]

# Completed refusals (dropped=True).
CODE_UNRECOGNIZED_TRIGGER = "unrecognized_trigger"
CODE_AI_RISK_REJECT = "ai_risk_reject"
CODE_NOISE_BAND = "inside_atr_noise_band"
CODE_CONTRADICTS_METRICS = "contradicts_own_metrics"
CODE_HOLDING_DISCIPLINE_FALSE = "holding_discipline_claim_false"

# Uncertainty — recorded, not a drop from that layer.
CODE_HARD_TRIGGER_UNCERTAIN = "hard_trigger_uncertain"
CODE_AI_RISK_UNAVAILABLE = "ai_risk_unavailable"

# A completed APPROVAL by the challenge seat — recorded, not a drop (board
# item 164, 2026-09-19). Until then an approved exit reached `agent_logs`
# only, so this per-symbol record held every outcome except the commonest.
CODE_AI_RISK_APPROVED = "ai_risk_approved"

_VALID_JUDGMENTS = frozenset({"named", "unnamed", "uncertain"})


def classify_trigger_reason(
    reason: object,
    *,
    cites: Callable[[str], bool],
) -> TriggerJudgment:
    """Classify a reason against the named-trigger recogniser.

    * ``named`` — the matcher ran and found a recognised trigger.
    * ``unnamed`` — the matcher ran, or the reason is missing/not a
      string, and no recognised trigger was found. A missing reason is a
      completed "no", not uncertainty: nothing was named. The owner
      refuses. Empty string is the same judgment.
    * ``uncertain`` — the matcher itself raised. That is the only case
      this layer cannot produce a judgment, and it fails OPEN, matching
      a dead Risk Manager. Agent application of the 2026-08-27
      posture, not a new owner ratification of a broader fail-open.
    """
    try:
        if not isinstance(reason, str) or not reason:
            return "unnamed"
        if cites(reason):
            return "named"
        return "unnamed"
    except Exception:  # noqa: BLE001 — matcher failure is uncertainty
        return "uncertain"


def record_exit_refusal(
    db: Any,
    *,
    symbol: str,
    run_id: str,
    action: str,
    code: str,
    dropped: bool,
    detail: str,
    layer: str,
    owner: str = REFUSAL_OWNER,
) -> None:
    """Append one per-symbol refusal/uncertainty row. Never raises.

    Written to ``specialist_evidence`` rather than ``intraday_evaluations``
    because the latter upserts on ``(symbol, run_id)`` and a later gate
    on the same symbol would erase this one. A forensic-write failure
    must not change whether the exit is dropped — callers decide that
    before calling, and this function swallows persistence errors.
    """
    symbol_u = (symbol or "").strip().upper()
    if not symbol_u or not run_id:
        return
    payload = {
        "action": str(action or "").upper(),
        "code": str(code),
        "dropped": bool(dropped),
        "detail": str(detail or "")[:500],
        "layer": str(layer),
        "owner": str(owner),
        "uncertainty_fail": UNCERTAINTY_FAIL,
    }
    try:
        db.insert_specialist_evidence(
            run_id=str(run_id),
            agent_name="pipeline",
            kind=EXIT_REFUSAL_KIND,
            scope="symbol",
            symbol=symbol_u,
            evidence_json=json.dumps(payload),
        )
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "exit refusal: durable record failed for %s %s (%s) — the "
            "drop/proceed decision is unchanged",
            action, symbol_u, e,
        )


def load_exit_refusals(
    db: Any, *, symbol: str | None = None, run_id: str | None = None,
) -> list[dict]:
    """Read back ``exit_refusal`` rows for tests and audit. Never used
    by the trading decision chain."""
    conn = getattr(db, "conn", None)
    if conn is None:
        return []
    clauses = ["kind = ?"]
    params: list[object] = [EXIT_REFUSAL_KIND]
    if symbol:
        clauses.append("symbol = ?")
        params.append(symbol.strip().upper())
    if run_id:
        clauses.append("run_id = ?")
        params.append(run_id)
    sql = (
        "SELECT symbol, run_id, evidence_json, timestamp FROM "
        f"specialist_evidence WHERE {' AND '.join(clauses)} ORDER BY id"
    )
    rows = conn.execute(sql, params).fetchall()
    out: list[dict] = []
    for row in rows:
        try:
            payload = json.loads(row["evidence_json"] or "{}")
        except (TypeError, ValueError):
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        out.append({
            "symbol": row["symbol"],
            "run_id": row["run_id"],
            "timestamp": row["timestamp"],
            **payload,
        })
    return out
