"""Exit-reason categorisation, decision-id linking and PM-target extraction, lifted VERBATIM from ledger.py."""

from __future__ import annotations

import json

from src.storage.analytics.calibration import _is_filled_trail_stop
from src.storage.trades.position_chain import (
    _is_position_exit_action,
    _row_counts_as_executed,
)


# ---------------------------------------------------------------------------
# Exit-reason categorization (Phase 6, spec §6.2e) — derived from the SAME
# trigger vocabulary `_HARD_TRIGGER_KEYWORDS` (src/pipeline.py) already
# requires every SELL/REDUCE to name, grouped exactly as that module's own
# comments group it. Duplicated rather than imported: src/storage/db.py must
# stay import-free of src/pipeline.py (pipeline.py is the one that imports
# Database, not the other way — importing back would be circular), matching
# how this module already duplicates `_executed_trade_predicate`-shaped
# logic instead of reaching into the trading orchestrator.
# ---------------------------------------------------------------------------

#: (category, keyword-substrings). Case-insensitive substring match against
#: `reasoning`, same tolerance-for-LLM-prose rationale as
#: `_reason_cites_hard_trigger` in src/pipeline.py.
_EXIT_TRIGGER_CATEGORIES: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "thesis_invalidated",
        (
            "thesis_invalid",
            "thesis invalid",
            "invalidation triggered",
            "broken thesis",
            "thesis broken",
        ),
    ),
    (
        "adverse_news_or_state_change",
        (
            "high bearish",
            "high-conviction bearish",
            "high conviction bearish",
            "adverse news",
            "material news",
            "sector shock",
        ),
    ),
    (
        "earnings_or_filing",
        (
            "bearish earnings",
            "bearish filing",
            "earnings missed",
            "earnings miss",
            "guidance cut",
        ),
    ),
    (
        "macro_regime_shift",
        (
            "regime shift",
            "regime flip",
            "regime flipped",
            "risk-off",
            "risk off",
        ),
    ),
    (
        "risk_management_hard_stop",
        (
            "daily loss",
            "daily-loss",
            "circuit breaker",
            # The two correlation phrases stay HERE deliberately, even though
            # they were removed from the live exit gate 2026-09-13 (WORK.md item
            # 44). This function is descriptive, not a gate: it categorises rows
            # that already exist, and dropping the phrases would silently
            # re-label historical exits as "uncategorised". No NEW exit can carry
            # them — `pipeline._HARD_TRIGGER_KEYWORDS` rejects the reason before
            # a trades row is ever written.
            "correlation breach",
            "correlation cluster breach",
        ),
    ),
    ("broker_stop_fill", ("stop hit", "stopped out")),
    # Constructor-stamped funding-trim. Descriptive only — not a midday
    # hard-trigger. Do NOT add this phrase to pipeline._HARD_TRIGGER_KEYWORDS
    # (correlation-breach lesson: wording with no verifier).
    ("mechanical_size_down", ("mechanical size-down vs live book",)),
)

#: Explicit fallback — never silently fold an exit-family row with no
#: recognized trigger into one of the real categories above.
_UNCATEGORISED_EXIT = "uncategorised"


def _categorize_exit_reason(
    action: str | None,
    reasoning: str | None,
    fill_status,
    fill_qty,
) -> str | None:
    """Deterministic exit_reason_category for one trades row, or None when
    the row isn't an exit at all (BUY, HOLD, SWEEP_*).

    Two axes, per spec: the EXIT PATH and the NAMED TRIGGER.
      - STOP_OUT and a FILLED TRAIL_STOP are broker-side stop fills — the
        broker executed the exit with no submitted decision reasoning to
        read, so the category comes from the action alone, and only once
        the fill is CONFIRMED (an unfilled TRAIL_STOP placement is
        protection sitting there, not an exit — see `_is_filled_trail_stop`).
      - TAKE_PROFIT was the deterministic auto trim's label (rule deleted
        2026-09-12; only historical rows carry it), not a judgment call —
        same confirmed-fill gate.
      - Everything else in `_is_position_exit_action` (SELL*, PARTIAL_SELL*,
        EMERGENCY_SELL, EMERGENCY_COVER, FORCE_DELEVER, REDUCE) is a reasoned
        decision: its reasoning text is checked at submission time against
        the same six trigger groups `_HARD_TRIGGER_KEYWORDS` gates behind
        SELL/REDUCE, valid regardless of eventual fill outcome. No match
        among rows that ARE exit-family gets the explicit "uncategorised"
        fallback — never a fabricated real category.
    """
    act = (action or "").upper()
    if act == "STOP_OUT":
        return "broker_stop_fill"
    if act == "RECONCILED_EXIT":
        # A recovered broker exit whose order_type could not be proven to be
        # a protective stop (item 173(a)): a distinct, honest category so
        # owner-facing attribution never files it under a stop it can't
        # substantiate, and never silently under an ordinary decided sale.
        return "reconciled_unattributed_exit"
    if act == "TRAIL_STOP":
        row = {"fill_status": fill_status, "fill_qty": fill_qty}
        return "broker_stop_fill" if _is_filled_trail_stop(row, act) else None
    if act == "TAKE_PROFIT":
        return "take_profit_target" if _row_counts_as_executed(act, fill_status, fill_qty) else None
    if not _is_position_exit_action(act):
        return None
    reason_l = (reasoning or "").lower()
    for category, keywords in _EXIT_TRIGGER_CATEGORIES:
        if any(kw in reason_l for kw in keywords):
            return category
    return _UNCATEGORISED_EXIT


# ---------------------------------------------------------------------------
# decision_id_status (conviction ledger, spec §7.2) — an honest label for
# WHETHER an exit-family row is traceable to a PM decision, mirroring Phase
# 3.1's pace/pace_status pattern: `pace` stays None and `pace_status` names
# WHY rather than the reader having to guess. Here `decision_id` stays
# whatever the caller passed (usually None for a broker/deterministic exit)
# and `decision_id_status` says WHY: 'linked' when a real decision_id was
# supplied (this exit was built from a PM/RM-reviewed TradeDecision the same
# session — see the ordinary SELL/COVER loops in pipeline_stages.py's
# ExecutionStage); 'no_originating_decision' when the row is exit-family but
# the code path that wrote it never had ANY decision to attach (broker stop
# fills, historical TAKE_PROFIT auto trims, deterministic trailing, emergency liquidation,
# force-delever/sweep, the midday reviewer's own exits) — a labelled
# absence, not a guess. None for BUY/SHORT/HOLD/SWEEP_* rows: the field
# does not apply to them at all (mirrors `_categorize_exit_reason`
# returning None for the same non-exit rows).
#
# Still a BROADER predicate than `_is_position_exit_action`, though the gap
# it was written for has closed: COVER/PARTIAL_COVER now DO belong to the
# position_id / exit_reason_category chain (2026-08-31 — shorts are chained
# and scored exactly as longs are). What remains is the set difference for
# anything outside both lists: this predicate labels every non-entry,
# non-HOLD, non-sweep row, so a future exit action is labelled 'linked' from
# the day it exists rather than silently returning None.
_NON_POSITIONAL_ACTIONS: frozenset[str] = frozenset(
    {
        "BUY",
        "SHORT",
        "HOLD",
        "SWEEP_BUY",
        "SWEEP_SELL",
    }
)


def _is_exit_family_for_decision_linking(action: str | None) -> bool:
    act = (action or "").upper()
    return bool(act) and act not in _NON_POSITIONAL_ACTIONS


def _resolve_decision_id_status(action: str | None, decision_id: str | None) -> str | None:
    if not _is_exit_family_for_decision_linking(action):
        return None
    return "linked" if decision_id else "no_originating_decision"


def _extract_pm_targets(full_response: str | None) -> list[dict]:
    """Best-effort `targets` list out of a `portfolio_manager` agent_logs
    row's `full_response`, for `Database.backfill_conviction_ledger`.

    Real production history (verified 2026-08-30) stores this TWO ways
    depending on which point in the prompt-format's history the row was
    written: some rows fence the JSON in a ```json ... ``` code block,
    others write the raw JSON object with no fence at all. Both are tried;
    neither found or parseable returns [] rather than raising, so one
    malformed historical row can't abort the whole backfill.
    """
    if not full_response:
        return []
    import json
    import re

    m = re.search(r"```json\s*(.*?)```", full_response, re.S)
    body = m.group(1) if m else full_response
    try:
        data = json.loads(body)
    except (ValueError, TypeError):
        return []
    if not isinstance(data, dict):
        return []
    targets = data.get("targets")
    return targets if isinstance(targets, list) else []


def _find_pm_target_for_symbol(full_response: str | None, symbol: str) -> dict | None:
    """The one target (if any) in a PM response matching `symbol`.

    Case-insensitive / whitespace-tolerant match — the same normalization
    `TargetPosition.normalize_symbol` applies going in, applied here going
    back out, since the stored JSON is the raw pre-validation prose the
    model wrote.
    """
    sym_norm = (symbol or "").strip().upper()
    if not sym_norm:
        return None
    for target in _extract_pm_targets(full_response):
        if not isinstance(target, dict):
            continue
        if str(target.get("symbol", "")).strip().upper() == sym_norm:
            return target
    return None
