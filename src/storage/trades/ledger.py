"""Trade-ledger write path lifted VERBATIM from src/storage/db.py (db rebuild instalment 3).

Everything that writes or reads the `trades` table and its satellites
(trade_refusals, pending_protection_restores, protection_restore_wal_audit,
pending_repegs, positions): inserts, fill/submit state transitions, stop and
take-profit amendments, stop-out recording, excursion accumulation, the
restore/repeg recovery queues, pruning and the two backfills. Standalone:
collaborators are the open sqlite3 connection, the Database lock, the
locked-write runner and the three small SQL/time helpers, all keyword-only,
so it builds with no Database/TradingPipeline behind it
(tests/boundary_harness.py). Database keeps same-named thin shims that
construct this per call.

The module-level helpers and constants below (position-id assignment, exit
vocabulary, exit-reason categorisation, decision-id resolution, PM-target
extraction) moved with the cluster; src/storage/db.py re-imports them so
`from src.storage.db import _assign_position_ids` keeps working (one
definition, here).
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
import uuid
from collections.abc import Callable
from datetime import datetime

from src.storage.analytics.calibration import _POSITION_OPEN_ACTIONS, _is_filled_trail_stop
from src.stop_price_classification import entry_stop_for_insert
from src.storage.trades import trade_refusals_store as _refusals_store
from src.util.time import UTC

logger = logging.getLogger(__name__)


def _trail_stop_reduced_position(row, action: str) -> bool:
    """True when a TRAIL_STOP row actually took shares OUT of the book.

    Share-count answer to `_is_filled_trail_stop`'s realized-exit question,
    and deliberately the WIDER of the two — they are different questions,
    so do NOT collapse them. `_is_filled_trail_stop` asks "is this a
    priceable realized exit" and therefore requires fill_status='filled'
    (or a legacy NULL status with a recorded fill_qty). A stop that filled
    PARTIALLY and was then canceled or expired carries a terminal status
    that is not 'filled' while still holding `fill_qty > 0`: there is no
    clean round trip to price, but those shares are genuinely gone from
    the broker's book, so a pure quantity ledger must subtract them or it
    will believe it holds stock it has already sold.

    Anything else — fill_status NULL / 'submitted' / 'pending_submit' with
    no fill, or a cancel or expiry that never traded — is protection
    resting at the broker and moves no shares.

    `action` is required and checked: this answers a question only about
    TRAIL_STOP rows, and every other action's quantity effect is decided
    by the signing rule in `get_symbols_with_open_ledger_qty`, not here.
    """
    if (action or "").upper() != "TRAIL_STOP":
        return False
    try:
        executed = float(row["fill_qty"] or 0)
    except (KeyError, IndexError, TypeError, ValueError):
        executed = 0.0
    if executed > 0:
        return True
    return _is_filled_trail_stop(row, "TRAIL_STOP")


def _new_position_id() -> str:
    """Opaque, stable identifier minted when a BUY opens a position from
    flat. Same shape as this codebase's run/decision ids
    (`RunContext.start`, `decision_id` in pipeline_stages.py) — a short
    prefix plus a uuid4 hex fragment — so it reads the same way in logs
    and URLs, without claiming to BE a run or decision id."""
    return f"pos-{uuid.uuid4().hex[:12]}"


#: Actions that (fully or partially) REDUCE a held position once executed,
#: split by WHICH side they can retire. SWEEP_BUY / SWEEP_SELL are
#: deliberately absent from all three: the cash-sweep vehicle is stopless and
#: excluded from calibration for the same reason — it has no entry thesis or
#: stop to chain a position around.
#:
#: The COVER family was added on 2026-08-31 (owner decision: short trades are
#: recorded and scored exactly as longs are). Before that a short opened no
#: chain and a cover retired nothing, so no short round trip existed to score.
#: `EMERGENCY_COVER` was already recognized — the short-side twin of
#: EMERGENCY_SELL, see src/pipeline.py's `_forced_close_side_and_qty` — but
#: with no short chain to attach to it could never actually close one.
_LONG_EXIT_ACTIONS: frozenset[str] = frozenset({"EMERGENCY_SELL"})
_LONG_EXIT_PREFIXES: tuple[str, ...] = ("SELL", "PARTIAL_SELL")
_SHORT_EXIT_ACTIONS: frozenset[str] = frozenset({"EMERGENCY_COVER"})
_SHORT_EXIT_PREFIXES: tuple[str, ...] = ("COVER", "PARTIAL_COVER")
#: Exits that can retire EITHER side: a stop, a trail, a take-profit, a
#: deterministic de-lever or a reviewer REDUCE all fire against whatever
#: position is open, and the trades row records no side of its own.
_EITHER_SIDE_EXIT_ACTIONS: frozenset[str] = frozenset({
    "FORCE_DELEVER", "REDUCE", "TAKE_PROFIT", "STOP_OUT", "TRAIL_STOP",
    # RECONCILED_EXIT (item 173(a)): a broker-side exit the reconciler wrote
    # back but whose order_type it could NOT prove was a protective stop —
    # an honest "the broker closed this, cause unattributed" marker, never
    # a STOP_OUT it can't stand behind. It retires a real position exactly
    # like a filled STOP_OUT, so it belongs to the exit-side chain here for
    # position_id assignment and calibration to count it as a closed lot.
    "RECONCILED_EXIT",
})

#: Kept as the flat union for every caller that only asks "is this row on the
#: exit side at all" (`backfill_position_ids`, `_categorize_exit_reason`).
_POSITION_EXIT_ACTIONS: frozenset[str] = (
    _LONG_EXIT_ACTIONS | _SHORT_EXIT_ACTIONS | _EITHER_SIDE_EXIT_ACTIONS
)
_POSITION_EXIT_PREFIXES: tuple[str, ...] = _LONG_EXIT_PREFIXES + _SHORT_EXIT_PREFIXES


def _is_position_exit_action(action: str | None) -> bool:
    """True for any action that belongs to an open position's chain on the
    exit side — SELL/PARTIAL_SELL* and COVER/PARTIAL_COVER* by prefix (
    PARTIAL_SELL(15%) etc. carry the trim fraction in the action string
    itself) plus the fixed sets above.
    A TRAIL_STOP row is included even when it is only a stop *placement*
    (not yet filled) — Phase 6 spec: a chain inherits TRAIL_STOP rows
    unconditionally; only the qty math below cares whether one actually
    fired."""
    act = (action or "").upper()
    return act.startswith(_POSITION_EXIT_PREFIXES) or act in _POSITION_EXIT_ACTIONS


def _exit_action_side(action: str | None) -> str | None:
    """Which direction of chain this exit can retire — "long", "short", or
    None for the either-side exits (stops, trails, take-profits, de-levers).

    Used only by `_assign_position_ids`, so a SELL can never be mistaken for
    the close of a short chain (or a COVER for the close of a long one). A
    row whose side does not match the chain that is open is left unattached
    rather than guessed at, exactly as an exit arriving with nothing open is.
    """
    act = (action or "").upper()
    if act.startswith(_SHORT_EXIT_PREFIXES) or act in _SHORT_EXIT_ACTIONS:
        return "short"
    if act.startswith(_LONG_EXIT_PREFIXES) or act in _LONG_EXIT_ACTIONS:
        return "long"
    return None




def _row_counts_as_executed(action: str | None, fill_status, fill_qty) -> bool:
    """Python-side mirror of `Database._executed_trade_predicate()`,
    usable on values pulled out of a row (rather than in a WHERE clause).
    Kept in sync deliberately — see that method's docstring."""
    status = fill_status or ""
    if status == "" and (action or "").upper() != "HOLD":
        return True
    if status == "filled":
        return True
    try:
        return float(fill_qty or 0) > 0
    except (TypeError, ValueError):
        return False


def _assign_position_ids(rows: list[dict]) -> dict[int, str | None]:
    """Walk one symbol's trades chronologically and derive position_id for
    every row. `rows` must already be ordered oldest-first (timestamp, id)
    and each dict needs at least id/action/qty/fill_qty/fill_status/
    position_id.

    Rule: a BUY (long) or a SHORT (short) from flat mints a fresh id; every
    subsequent entry on the same side (scale-in) and every recognized
    exit-family action for that side (see `_is_position_exit_action` and
    `_exit_action_side`) inherits it until the running executed-qty net
    returns to ~0, at which point the chain is closed and the next entry
    mints a new one. A row with no open chain to attach to (an exit that
    arrives while nothing is open — typically the ledger's oldest record for
    a symbol whose real position predates this system, or a stray SELL after
    the book already went flat) is left unassigned rather than guessed, per
    spec.

    **Shorts chain identically to longs** (owner decision, 2026-08-31). A
    SHORT opens, a COVER/PARTIAL_COVER/EMERGENCY_COVER retires, and a stop or
    trail retires either side. `qty` is the magnitude of shares moved on both
    sides — a short's net counts UP as it is opened and DOWN as it is
    covered, the same arithmetic as a long — so nothing here is negated or
    special-cased for direction. Before this, `is_open` was `action == "BUY"`
    alone: no SHORT ever received a position_id, so no short round trip
    existed for §9.5's ledger to score. That was the whole of the gap.

    Two guards keep the long side's existing chains untouched, both for
    histories the desk does not currently produce:
      - an entry on the OPPOSITE side while a chain is still open is passed
        through untouched rather than flipping or closing that chain (you
        cannot be long and short the same symbol at one broker, so this is a
        malformed history, not a position);
      - an exit whose side does not match the open chain (a SELL against a
        short, a COVER against a long) is left unattached rather than
        allowed to retire the wrong position.

    Rows that ALREADY carry a position_id are treated as ground truth. That
    is what makes this function safe to call from both the live per-insert
    resolver (which only ever has one new, unassigned row) and the one-time
    historical backfill (which may run against a database where live
    trading has already assigned some rows and older history is still
    NULL) without the two ever disagreeing or re-minting an id that already
    exists — including when the ALREADY-assigned row is not the first row
    in its chain (e.g. a legacy BUY with no id, followed by a live-inserted
    TRAIL_STOP that already minted one): grouping happens in a first,
    id-blind structural pass, and only THEN does a second pass pick, per
    group, whichever id (if any) already exists in it — never one id for
    the earlier rows and a different one for the later rows of what is
    structurally the same chain.
    """
    # Pass 1 — partition into chain groups using ONLY the net-qty rule,
    # ignoring any already-persisted id. Which rows belong to the same
    # chain is a purely structural fact (ends the moment net qty returns to
    # ~0); it does not depend on which of those rows happens to carry an id
    # already.
    groups: list[list[dict]] = []
    passthrough: dict[int, str | None] = {}
    current_group: list[dict] | None = None
    current_net = 0.0
    current_direction: str | None = None
    for row in rows:
        action = (row.get("action") or "").upper()
        open_direction = _POSITION_OPEN_ACTIONS.get(action)
        is_open = open_direction is not None
        is_exit = _is_position_exit_action(action)
        if not is_open and not is_exit:
            # HOLD / SWEEP_BUY / SWEEP_SELL / anything unrecognized: never
            # part of a position chain, and never disturbs one in progress.
            passthrough[row["id"]] = row.get("position_id")
            continue

        executed = _row_counts_as_executed(action, row.get("fill_status"), row.get("fill_qty"))
        qty = float(row.get("fill_qty") if row.get("fill_qty") else row.get("qty") or 0)
        filled_trail = action == "TRAIL_STOP" and _is_filled_trail_stop(row, action)
        chain_open = current_group is not None and current_net > 1e-6

        if is_open:
            if chain_open and open_direction != current_direction:
                # A SHORT while a long chain is still open (or the reverse).
                # Not producible by this desk and not a position either way:
                # passed through so the open chain is left exactly as it was.
                passthrough[row["id"]] = row.get("position_id")
                continue
            if not chain_open:
                current_group = []
                groups.append(current_group)
                current_net = 0.0
                current_direction = open_direction
            current_group.append(row)
            if executed:
                current_net += qty
        else:
            if not chain_open:
                # An exit with nothing open — its own singleton group,
                # which pass 2 resolves to None unless it happens to
                # already carry an id (a hand-corrected row: trust it,
                # still never mint a NEW one for an unattached exit).
                groups.append([row])
                current_group = None
                current_direction = None
                continue
            exit_side = _exit_action_side(action)
            if exit_side is not None and exit_side != current_direction:
                # A SELL against an open short, or a COVER against an open
                # long. It cannot be this chain's exit; unattached, and the
                # chain it does not belong to is left running.
                groups.append([row])
                continue
            current_group.append(row)
            if action == "TRAIL_STOP":
                if executed and filled_trail:
                    current_net -= qty
            elif executed:
                current_net -= qty
            if current_net <= 1e-6:
                current_group = None
                current_net = 0.0
                current_direction = None

    # Pass 2 — resolve one id per group. An id already present ANYWHERE in
    # the group wins (ground truth, whichever row in the chain happened to
    # carry it first); otherwise mint one fresh id for the whole group, but
    # only for a group that actually opened with a BUY or a SHORT — a lone
    # unattached exit (no entry, no existing id) stays unassigned rather
    # than guessed.
    assignments: dict[int, str | None] = dict(passthrough)
    for group in groups:
        existing_ids = [r.get("position_id") for r in group if r.get("position_id")]
        opened = any(
            (r.get("action") or "").upper() in _POSITION_OPEN_ACTIONS for r in group
        )
        if existing_ids:
            resolved = existing_ids[0]
        elif opened:
            resolved = _new_position_id()
        else:
            resolved = None
        for r in group:
            assignments[r["id"]] = r.get("position_id") or resolved
    return assignments


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
    ("thesis_invalidated", (
        "thesis_invalid", "thesis invalid", "invalidation triggered",
        "broken thesis", "thesis broken",
    )),
    ("adverse_news_or_state_change", (
        "high bearish", "high-conviction bearish", "high conviction bearish",
        "adverse news", "material news", "sector shock",
    )),
    ("earnings_or_filing", (
        "bearish earnings", "bearish filing", "earnings missed",
        "earnings miss", "guidance cut",
    )),
    ("macro_regime_shift", (
        "regime shift", "regime flip", "regime flipped", "risk-off", "risk off",
    )),
    ("risk_management_hard_stop", (
        "daily loss", "daily-loss", "circuit breaker",
        # The two correlation phrases stay HERE deliberately, even though
        # they were removed from the live exit gate 2026-09-13 (WORK.md item
        # 44). This function is descriptive, not a gate: it categorises rows
        # that already exist, and dropping the phrases would silently
        # re-label historical exits as "uncategorised". No NEW exit can carry
        # them — `pipeline._HARD_TRIGGER_KEYWORDS` rejects the reason before
        # a trades row is ever written.
        "correlation breach", "correlation cluster breach",
    )),
    ("broker_stop_fill", ("stop hit", "stopped out")),
    # Constructor-stamped funding-trim. Descriptive only — not a midday
    # hard-trigger. Do NOT add this phrase to pipeline._HARD_TRIGGER_KEYWORDS
    # (correlation-breach lesson: wording with no verifier).
    ("mechanical_size_down", (
        "mechanical size-down vs live book",
    )),
)

#: Explicit fallback — never silently fold an exit-family row with no
#: recognized trigger into one of the real categories above.
_UNCATEGORISED_EXIT = "uncategorised"


def _categorize_exit_reason(
    action: str | None, reasoning: str | None, fill_status, fill_qty,
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
        return (
            "take_profit_target"
            if _row_counts_as_executed(act, fill_status, fill_qty) else None
        )
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
_NON_POSITIONAL_ACTIONS: frozenset[str] = frozenset({
    "BUY", "SHORT", "HOLD", "SWEEP_BUY", "SWEEP_SELL",
})


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


class TradeLedger:
    """Trade-ledger write cluster; see module docstring."""

    def __init__(self, *, conn: sqlite3.Connection, lock: threading.Lock, locked_write: Callable,
                 executed_trade_predicate: Callable[[], str], sqlite_utc_timestamp: Callable[[datetime], str],
                 et_day_utc_bounds: Callable[..., tuple[str, str]]):
        self.conn = conn
        self._lock = lock
        self._locked_write = locked_write
        self._executed_trade_predicate = executed_trade_predicate
        self._sqlite_utc_timestamp = sqlite_utc_timestamp
        self._et_day_utc_bounds = et_day_utc_bounds

    def insert_trade(self, symbol: str, action: str, qty: float, price: float,
                     reasoning: str, run_id: str,
                     stop_loss: float = 0, take_profit: float = 0,
                     broker_order_id: str | None = None,
                     fill_status: str | None = None,
                     decision_id: str | None = None,
                     expected_horizon_sessions: int | None = None,
                     setup_type: str | None = None,
                     conviction: str | None = None,
                     requested_risk_pct: float | None = None,
                     allocated_risk_pct: float | None = None,
                     decision_model: str | None = None,
                     thesis_invalid_if: str | None = None,
                     structural_ceiling: bool | None = None,
                     entry_atr: float | None = None,
                     stop_basis: str | None = None,
                     stop_level_basis: str | None = None) -> int:
        """Insert a trade record. Returns the new row's id.

        `entry_atr` / `stop_basis` are STOP-FLOOR EVIDENCE, pinned at entry
        only, and are recorded for one purpose: so a future pass can ask
        whether the ratified minimum stop width was ever VIOLATED in
        practice. See `_accumulate_excursions` for the MAE/MFE legs and
        for the explicit limits on what this data may be used for.


        `fill_status` semantics:
          - 'submitted'  — sent to broker, terminal status pending
          - 'filled'     — broker confirmed execution (full or partial)
          - 'canceled' / 'rejected' / 'expired' / 'done_for_day' — terminal broker
                           status; may still carry fill_qty/fill_price for partial fills
          - None         — legacy row or non-executed audit row (currently HOLD).
                           Legacy BUY/SELL rows still count as executed for back-compat;
                           synthetic HOLD rows are explicitly excluded from executed_only.

        `position_id`, `exit_reason_category`, and `decision_id_status`
        (Phase 6, §6.2a/e; conviction ledger §7.2) are ALWAYS derived here,
        never accepted as arguments — see `_resolve_new_row_position_id`,
        `_categorize_exit_reason`, and `_resolve_decision_id_status`. Every
        one of this method's ~12 call sites across the codebase gets the
        chain-linking, exit classification, and decision-link labelling for
        free with no change to the call.

        `conviction` / `requested_risk_pct` / `allocated_risk_pct` /
        `decision_model` are the conviction ledger (§7.2) — pinned at ENTRY
        only (see `TradeDecision` in models.py for what each figure means);
        every existing caller that never passes them gets None, which is
        correct for every non-entry row and every legacy caller.

        `thesis_invalid_if` mirrors that same entry-only pinning — see
        `TradeDecision.thesis_invalid_if` in models.py. None for every
        non-entry row, every legacy caller, and any entry whose target
        stated no falsifier condition.

        `structural_ceiling` (item 82) is the MEASURED half of construction's
        breakout verdict, pinned at ENTRY (BUY/SHORT) only — see
        `TradeDecision.structural_ceiling` in models.py. Stored as 0/1; None
        for every non-entry row and every legacy caller, so readers fall back
        to `setup_type` alone (the conservative side).
        """
        # 0/1 for storage, None stays NULL — see the column's migration note.
        structural_ceiling_stored = (
            None if structural_ceiling is None else int(bool(structural_ceiling))
        )

        def _do():
            position_id = self._resolve_new_row_position_id(
                symbol, action, qty=qty, fill_status=fill_status, fill_qty=None,
            )
            exit_category = _categorize_exit_reason(action, reasoning, fill_status, None)
            decision_link_status = _resolve_decision_id_status(action, decision_id)
            initial_stop_loss = entry_stop_for_insert(action, symbol, stop_loss, _POSITION_OPEN_ACTIONS)
            # Pin the entry target the same way, and for the same reason the
            # `initial_take_profit` migration note gives: `take_profit` is
            # mutable now that a structural event can trigger a
            # re-derivation, and progress/pace must keep measuring against
            # the yardstick the trade was opened on.
            try:
                target_at_insert = float(take_profit or 0)
            except (TypeError, ValueError):
                target_at_insert = 0.0
            initial_take_profit = target_at_insert if target_at_insert > 0 else None
            cur = self.conn.execute(
                "INSERT INTO trades (symbol, action, qty, price, reasoning, run_id, "
                "stop_loss, take_profit, broker_order_id, fill_status, decision_id, "
                "expected_horizon_sessions, setup_type, position_id, exit_reason_category, "
                "conviction, requested_risk_pct, allocated_risk_pct, decision_model, "
                "decision_id_status, thesis_invalid_if, initial_stop_loss, "
                "initial_take_profit, structural_ceiling, entry_atr, stop_basis, "
                "stop_level_basis) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (symbol, action, qty, price, reasoning, run_id,
                 stop_loss, take_profit, broker_order_id, fill_status, decision_id,
                 expected_horizon_sessions, setup_type, position_id, exit_category,
                 conviction, requested_risk_pct, allocated_risk_pct, decision_model,
                 decision_link_status, thesis_invalid_if, initial_stop_loss,
                 initial_take_profit, structural_ceiling_stored,
                 entry_atr, stop_basis, stop_level_basis),
            )
            self.conn.commit()
            return cur.lastrowid
        return self._locked_write(_do, label="insert_trade")

    def insert_trade_refusal(self, **kwargs) -> int | None:
        """Lifted into `trade_refusals_store` (2026-10-05); see it for the column meanings."""
        return _refusals_store.insert(self, **kwargs)

    def get_trade_refusals(self, *, refusal: str | None = None, limit: int = 500) -> list[dict]:
        """Read back the durable refusal rows, newest first."""
        return _refusals_store.get_all(self, refusal=refusal, limit=limit)

    def update_open_stop_loss(
        self, symbol: str, new_stop_price: float, *, action: str | None = None,
    ) -> bool:
        """Write the live stop onto every opening row of this position.

        `action` is 'BUY' or 'SHORT' when the caller knows the side
        (repair, a short trail). Omitting it takes the most recent of
        either — only safe when a symbol cannot be both. Refuses an
        unknown action rather than defaulting to long.

        A scale-in leaves more than one opening row on the same
        `position_id`; the broker holds one consolidated stop, so every
        still-open row of that position is updated. Each row freezes its
        own `initial_stop_loss` if it had an entry stop; a row that opened
        with none does not mint one from the live level.
        """
        try:
            price = float(new_stop_price)
        except (TypeError, ValueError):
            return False
        if price != price or price <= 0:
            return False
        symbol_key = (symbol or "").strip()
        if not symbol_key:
            return False
        opening = (action or "").upper() or None
        if opening is not None and opening not in ("BUY", "SHORT"):
            logger.warning(
                "update_open_stop_loss: refusing unknown opening action %r "
                "for %s", action, symbol_key,
            )
            return False

        def _do():
            predicate = (
                f"({self._executed_trade_predicate()} "
                "OR fill_status IN ('submitted', 'pending_submit'))"
            )
            if opening:
                row = self.conn.execute(
                    "SELECT id, stop_loss, initial_stop_loss, position_id, action "
                    "FROM trades "
                    "WHERE symbol = ? AND action = ? "
                    f"AND {predicate} "
                    "ORDER BY timestamp DESC, id DESC LIMIT 1",
                    (symbol_key, opening),
                ).fetchone()
            else:
                row = self.conn.execute(
                    "SELECT id, stop_loss, initial_stop_loss, position_id, action "
                    "FROM trades "
                    "WHERE symbol = ? AND action IN ('BUY', 'SHORT') "
                    f"AND {predicate} "
                    "ORDER BY timestamp DESC, id DESC LIMIT 1",
                    (symbol_key,),
                ).fetchone()
            if row is None:
                logger.warning(
                    "update_open_stop_loss: no opening row for %s — live "
                    "stop $%.4f was NOT recorded", symbol_key, price,
                )
                return False
            side = opening or (row["action"] if row["action"] in ("BUY", "SHORT") else None)
            position_id = row["position_id"]
            if position_id and side:
                self.conn.execute(
                    "UPDATE trades SET "
                    "stop_loss = ?, "
                    "initial_stop_loss = CASE "
                    "WHEN initial_stop_loss IS NOT NULL AND initial_stop_loss > 0 "
                    "THEN initial_stop_loss "
                    "WHEN stop_loss IS NOT NULL AND stop_loss > 0 THEN stop_loss "
                    "ELSE initial_stop_loss END "
                    f"WHERE position_id = ? AND action = ? AND {predicate}",
                    (price, position_id, side),
                )
            else:
                try:
                    current = float(row["stop_loss"] or 0)
                except (TypeError, ValueError):
                    current = 0.0
                try:
                    initial = float(row["initial_stop_loss"] or 0)
                except (TypeError, ValueError):
                    initial = 0.0
                frozen = initial if initial > 0 else (current if current > 0 else None)
                if frozen is None:
                    self.conn.execute(
                        "UPDATE trades SET stop_loss = ? WHERE id = ?",
                        (price, row["id"]),
                    )
                else:
                    self.conn.execute(
                        "UPDATE trades SET stop_loss = ?, initial_stop_loss = ? "
                        "WHERE id = ?",
                        (price, frozen, row["id"]),
                    )
            self.conn.commit()
            return True
        return bool(self._locked_write(_do, label="update_open_stop_loss"))

    def _resolve_new_row_position_id(
        self, symbol: str, action: str, *, qty: float,
        fill_status: str | None, fill_qty: float | None,
        timestamp: str | None = None,
    ) -> str | None:
        """Position-id for a row not yet inserted. Caller must already hold
        `self._lock` (called from inside a `_locked_write` closure).

        Replays `_assign_position_ids` over this symbol's full trade history
        plus a synthetic placeholder for the row about to be inserted, and
        returns whatever that placeholder was assigned. Every prior row
        already carries its own persisted position_id, which
        `_assign_position_ids` treats as ground truth rather than
        re-deriving — so in practice this only ever has to reason about ONE
        new row, even though it re-reads the symbol's history to do it.
        Trade volume here (~15-25/day across the whole book) makes that scan
        cheap; correctness and a single source of truth shared with the
        historical backfill (`backfill_position_ids`) matter more than
        shaving it to an O(1) running counter.

        `timestamp` is only supplied by callers that already know the row's
        real (possibly historical) timestamp before insert
        (`insert_stop_out_trade`, which writes back a broker fill discovered
        after the fact) — the placeholder is then spliced into its correct
        chronological position. `insert_trade` never knows its timestamp
        ahead of the INSERT (it always writes SQLite's `datetime('now')`),
        so the default assumes "happening now" and appends last, which is
        correct in practice — a new trade is always the most recent event.
        """
        rows = self.conn.execute(
            "SELECT id, action, qty, fill_qty, fill_status, position_id, timestamp "
            "FROM trades WHERE symbol = ? ORDER BY timestamp, id",
            (symbol,),
        ).fetchall()
        history = [dict(r) for r in rows]
        placeholder = {
            "id": -1, "action": action, "qty": qty,
            "fill_qty": fill_qty, "fill_status": fill_status,
            "position_id": None, "timestamp": timestamp,
        }
        if timestamp is None:
            history.append(placeholder)
        else:
            idx = len(history)
            for i, row in enumerate(history):
                if (row.get("timestamp") or "") > timestamp:
                    idx = i
                    break
            history.insert(idx, placeholder)
        return _assign_position_ids(history).get(-1)

    def confirm_trade_submitted(
        self, row_id: int, broker_order_id: str | None,
    ) -> int:
        """Flip a pending_submit row to submitted after broker accepted.

        Part of the write-ahead-intent pattern for BUY submission (audit
        F4). The flow is:

            insert_trade(..., fill_status='pending_submit', broker_order_id=NULL)
            broker.submit_order(...)
            confirm_trade_submitted(row_id, broker_order_id)  ← this method

        On the crash window between submit_order returning and this call
        landing, the row stays as pending_submit with broker_order_id
        unset. Reconcile can detect orphans by (fill_status='pending_submit'
        AND broker_order_id IS NULL) and decide how to reconcile against
        the broker's order list.
        """
        with self._lock:
            cur = self.conn.execute(
                "UPDATE trades SET broker_order_id = ?, fill_status = 'submitted' "
                "WHERE id = ?",
                (broker_order_id, row_id),
            )
            self.conn.commit()
            return cur.rowcount

    def repoint_trade_broker_order_id(
        self, row_id: int, *, old_order_id: str, new_order_id: str,
    ) -> int:
        """Repoint a trade row at the order id an Alpaca replacement minted.

        A re-peg PATCHes a working entry limit; Alpaca answers by cancelling
        the old order and creating a NEW one with a NEW id. The old id is no
        longer authoritative — `_reconcile_fills` matches on
        `broker_order_id`, so leaving it stale makes reconciliation follow a
        dead order that will forever report status 'replaced' (a status the
        terminal sets do not cover) and conclude nothing ever happened.

        Guarded by `old_order_id` in the WHERE clause on purpose: this is
        called from a crash-recovery drain that may run twice on the same
        WAL row. Applying it a second time matches zero rows (the row now
        holds `new_order_id`) and returns 0 instead of clobbering a
        subsequent, newer re-peg's id. Idempotent by construction.
        """
        with self._lock:
            cur = self.conn.execute(
                "UPDATE trades SET broker_order_id = ? "
                "WHERE id = ? AND broker_order_id = ?",
                (new_order_id, row_id, old_order_id),
            )
            self.conn.commit()
            return cur.rowcount or 0

    def mark_trade_submit_failed(self, row_id: int) -> int:
        """Flag a pending_submit row as submit_failed.

        Used when broker.submit_order raised (broker may or may not have
        the order) OR when broker rejected the order (_order_accepted
        returned False). Distinct from rejected/canceled because those
        statuses imply the broker accepted then rejected; submit_failed
        means we don't know what the broker saw. Operator / reconcile
        sweeps these against the broker's order list by symbol + time.
        """
        with self._lock:
            cur = self.conn.execute(
                "UPDATE trades SET fill_status = 'submit_failed' "
                "WHERE id = ?",
                (row_id,),
            )
            self.conn.commit()
            return cur.rowcount

    def update_trade_fill(
        self, broker_order_id: str, fill_status: str,
        fill_qty: float | None = None, fill_price: float | None = None,
    ) -> int:
        """Update a trade row's fill reconciliation after broker terminal status.

        Matches on broker_order_id. Returns row count updated.
        """
        with self._lock:
            cur = self.conn.execute(
                "UPDATE trades SET fill_status = ?, fill_qty = ?, fill_price = ?, "
                "fill_reconciled_at = datetime('now') "
                "WHERE broker_order_id = ?",
                (fill_status, fill_qty, fill_price, broker_order_id),
            )
            try:
                has_fill = float(fill_qty or 0) > 0
            except (TypeError, ValueError):
                has_fill = False
            if has_fill and fill_price is not None:
                row = self.conn.execute(
                    "SELECT id, symbol, action, reasoning FROM trades "
                    "WHERE broker_order_id = ?",
                    (broker_order_id,),
                ).fetchone()
                if row is not None and row["action"] not in {"BUY", "SWEEP_BUY", "HOLD"}:
                    realized = self._realized_pnl_through_trade(row["symbol"], row["id"])
                    # Recompute exit_reason_category now that the fill is
                    # CONFIRMED — the broker_stop_fill (TRAIL_STOP) and
                    # take_profit_target (TAKE_PROFIT) categories are gated
                    # on a real fill and are still None from insert time
                    # (submitted, outcome unknown) until this update lands.
                    # A no-op recompute for every other action (already
                    # settled from `reasoning` at insert time).
                    exit_category = _categorize_exit_reason(
                        row["action"], row["reasoning"], fill_status, fill_qty,
                    )
                    self.conn.execute(
                        "UPDATE trades SET realized_pnl = ?, exit_reason_category = ? "
                        "WHERE id = ?",
                        (realized, exit_category, row["id"]),
                    )
            self.conn.commit()
            return cur.rowcount or 0

    def _realized_pnl_through_trade(self, symbol: str, through_id: int) -> float | None:
        """Average-cost P&L for one confirmed exit; caller holds ``_lock``."""
        rows = self.conn.execute(
            "SELECT id, action, qty, price, fill_status, fill_qty, fill_price "
            "FROM trades WHERE symbol = ? AND id <= ? ORDER BY id",
            (symbol, through_id),
        ).fetchall()
        inventory = 0.0
        average_cost = 0.0
        target_pnl: float | None = None
        for row in rows:
            status = str(row["fill_status"] or "").lower()
            actual_qty = float(row["fill_qty"] or 0)
            actual_price = row["fill_price"]
            # Only broker-confirmed execution facts are safe cost basis.
            if actual_qty <= 0 or actual_price is None or status in {
                "submitted", "pending_submit", "submit_failed",
            }:
                continue
            actual_price = float(actual_price)
            if row["action"] in {"BUY", "SWEEP_BUY"}:
                new_inventory = inventory + actual_qty
                average_cost = (
                    (inventory * average_cost + actual_qty * actual_price) / new_inventory
                    if new_inventory > 0 else 0.0
                )
                inventory = new_inventory
                continue
            if row["action"] == "HOLD":
                continue
            if inventory + 1e-9 < actual_qty:
                pnl = None  # incomplete canonical cost basis; unknown stays unknown
                inventory = max(0.0, inventory - actual_qty)
            else:
                pnl = round((actual_price - average_cost) * actual_qty, 6)
                inventory -= actual_qty
                if inventory <= 1e-9:
                    inventory = 0.0
                    average_cost = 0.0
            if row["id"] == through_id:
                target_pnl = pnl
        return target_pnl

    def get_symbols_with_open_ledger_qty(self) -> dict[str, float]:
        """Per-symbol net share count the `trades` ledger BELIEVES it holds.

        BUY / SWEEP_BUY add executed qty; every other non-HOLD executed
        action subtracts it — mirrors the accounting `_realized_pnl_
        through_trade` and `compute_trade_calibration` already do,
        collapsed to a running total per symbol instead of per-lot detail,
        because this function only needs to know WHETHER the ledger and
        the broker still agree, not how a mismatch would price out.

        This is the ledger's own, self-contained belief — it has no idea
        the broker did anything it was never told about. Comparing this
        number against `AlpacaBroker.get_positions()` is exactly how the
        2026-08-28 ONDS/CCJ gap was found: both BUY rows left this
        function reporting 17 and 2 shares respectively long after the
        broker's own book had gone to zero, because the protective stop
        that closed them was never written back to `trades`.
        `_reconcile_stop_out_fills` (src/pipeline.py) is the caller that
        acts on a mismatch.

        TRAIL_STOP IS THE ONE ACTION THIS CANNOT SIGN FROM THE ACTION NAME
        (fixed 2026-09-23). Every other exit-family row is written only
        once the desk has decided to sell, but a TRAIL_STOP row is written
        at PLACEMENT — protection resting at the broker, which may never
        fire. `_executed_trade_predicate` lets a legacy `fill_status IS
        NULL` placement through, and signing it -1 subtracted the whole
        protected position from the ledger's belief. Measured on the
        production DB 2026-09-23, that made the ledger read AMD 0 (1.7662
        actually held, so a real AMD stop-out would never have been
        detected — the caller skips any symbol it believes is flat) and
        drove COP/EQNR negative. The rule here is the quantity the broker
        actually EXECUTED (`_trail_stop_reduced_position`).

        `_is_filled_trail_stop`, `compute_trade_calibration`,
        `_assign_position_ids` and `_categorize_exit_reason` all separate
        a resting stop from a fired one too, but they ask the NARROWER
        question — "is this a priceable realized exit" — and this function
        deliberately departs from them on one row shape: a stop that
        partially filled and was then canceled is not a round trip they
        can price, yet its shares really did leave the book. See
        `_trail_stop_reduced_position` for why that is not drift.

        SIGNED BY POSITION SIDE, corrected on the short side (item 173(c),
        2026-09-25). Both short-retiring routes now sign +1: a COVER-family
        action (COVER / EMERGENCY_COVER / PARTIAL_COVER) is a buy-to-cover by
        name, and a FILLED TRAIL_STOP resting on a short is a buy-to-cover
        read from the running net (its side is not in its name). Before this,
        every non-BUY row signed -1, so a SHORT 36 covered in full read -72,
        not 0 whether it came as a COVER or as a filled buy-to-cover
        TRAIL_STOP [measured 2026-09-23]. The caller
        `_reconcile_stop_out_fills` is LONG-only and skips any negative, so
        no live behaviour changed today; the ledger's own belief is simply
        now correct for the day shorts are enabled.
        """
        with self._lock:
            rows = self.conn.execute(
                "SELECT symbol, action, qty, fill_qty, fill_status FROM trades "
                f"WHERE {self._executed_trade_predicate()} ORDER BY id",
            ).fetchall()
        net: dict[str, float] = {}
        for row in rows:
            action = (row["action"] or "").upper()
            if action == "HOLD":
                continue
            if action == "TRAIL_STOP" and not _trail_stop_reduced_position(row, action):
                # Protection sitting at the broker, not a sale: no
                # quantity effect at all.
                continue
            qty = float(row["fill_qty"] if row["fill_qty"] else row["qty"] or 0)
            if qty <= 0:
                continue
            symbol = row["symbol"]
            running = net.get(symbol, 0.0)
            # Item 173(c): sign a share-moving row by the SIDE of the
            # position it acts on, not by a hard-coded BUY-vs-everything-else
            # split. A COVER-family action is a BUY-to-cover: it RETIRES a
            # short toward zero, so it ADDS shares (+1). The old rule signed
            # every non-BUY row -1, so a SHORT 36 covered in full read -72,
            # not 0 [measured 2026-09-23]. Normalise PARTIAL_COVER(50%) ->
            # PARTIAL_COVER first, exactly as `_symbols_already_trimmed_today`
            # does. A FILLED TRAIL_STOP carries no side in its name — it is a
            # long's protective SELL or a short's protective BUY-to-cover
            # depending on the position it guards — so read that side from the
            # running net for this symbol (rows are id-ordered, so the entry
            # always precedes its stop): a stop resting on a short is a
            # buy-to-cover and ADDS. Everything else — SELL / REDUCE /
            # STOP_OUT / SWEEP_SELL / a long's fired TRAIL_STOP, and SHORT
            # (a sell-to-open) — subtracts.
            base_action = action.split("(", 1)[0].strip()
            if base_action in ("BUY", "SWEEP_BUY",
                               "COVER", "EMERGENCY_COVER", "PARTIAL_COVER"):
                sign = 1.0
            elif base_action == "TRAIL_STOP" and running < -1e-9:
                sign = 1.0  # fired protective stop on a SHORT = buy-to-cover
            else:
                sign = -1.0
            net[symbol] = running + sign * qty
        return net

    def get_known_broker_order_ids(self, symbol: str) -> set[str]:
        """Every `broker_order_id` already recorded in `trades` for `symbol`.

        The dedup key `_reconcile_stop_out_fills` uses to tell "the broker
        already told us about this order" apart from "this fill has never
        touched the ledger". The reconciler re-runs every session
        (morning / intra_check / midday / close / evening), so this set is
        what keeps recording a stop-out an exactly-once operation no
        matter how many passes see the same gap.
        """
        with self._lock:
            rows = self.conn.execute(
                "SELECT broker_order_id FROM trades "
                "WHERE symbol = ? AND broker_order_id IS NOT NULL",
                (symbol,),
            ).fetchall()
        return {r[0] for r in rows}

    def insert_stop_out_trade(
        self, *, symbol: str, qty: float, price: float,
        broker_order_id: str, filled_at: str | None,
        run_id: str | None = None, action: str = "STOP_OUT",
        reasoning: str | None = None,
    ) -> tuple[int, bool]:
        """Idempotently record a broker-initiated exit the ledger never saw.

        2026-08-28: ONDS (17 @ 8.53, stopped 7.93 → realized -$10.20) and
        CCJ (2 @ 107.465, stopped 102.955 → realized -$9.02) were both
        closed by their broker-resident protective stop with NO row ever
        written to `trades`. The stop order was placed by
        `AlpacaBroker.place_entry_protection` / `_repair_stop_coverage` /
        `shift_stops_down`, none of which log the ORDER ITSELF as a ledger
        row — unlike every system-DECIDED exit (SELL / REDUCE / TRAIL_STOP
        / SWEEP_SELL), which all call `insert_trade` at submission time and
        get picked up by `_reconcile_fills` once terminal. This is the
        write-back for that other class of order. There is no 'submitted'
        phase for a row created here: by the time `_reconcile_stop_out_
        fills` learns the order exists, the broker has already reported it
        as terminally filled.

        Idempotency: keyed on `broker_order_id`, checked and inserted
        under the SAME lock, so no matter how many times a session's
        reconciliation pass runs — or how many overlapping sessions
        observe the same gap — one broker order id can only ever produce
        ONE row. Mirrors `update_trade_fill`'s realized_pnl write, just
        for a row that does not exist yet rather than one already
        'submitted'.

        `realized_pnl` is computed the instant the row exists, via the
        SAME average-cost walk every other exit uses
        (`_realized_pnl_through_trade`) — it stays NULL, not a guess, when
        the ledger's own BUY history can't cover the exited quantity (an
        unmatched exit; `_reconcile_stop_out_fills` flags that case rather
        than silently accepting an unpriced row).

        `action` (item 173(a)): the caller sets this from the broker fill's
        own order_type — STOP_OUT only when the broker order really was a
        protective stop, SELL for a market/limit exit, and RECONCILED_EXIT
        when the type does not prove it was a stop. It defaults to STOP_OUT
        only for backward compatibility with callers that pass none; the
        reconciler always passes it explicitly so a recovered exit is never
        labelled a protective stop the broker record can't substantiate.

        Returns `(row_id, created)`. `created=False` means the order was
        already recorded — the existing row's id is returned so a caller
        never needs a second lookup to stay idempotent-safe.
        """
        if not broker_order_id:
            # Every caller constructs this from a REAL broker order dict
            # that is only ever produced with a non-empty id (see
            # AlpacaBroker.list_filled_sell_orders) — a falsy id here means
            # a caller bug, not a legitimate row. Refusing loudly beats
            # silently inserting a row the idempotency key can never find
            # again (broker_order_id IS NULL would never match on replay,
            # and this exit could get double-recorded on the next pass —
            # exactly the failure mode this function exists to prevent).
            raise ValueError(
                "insert_stop_out_trade requires a non-empty broker_order_id "
                "— it is the idempotency key that makes a stop-out record "
                "exactly-once across repeated reconciliation passes"
            )

        def _do():
            existing = self.conn.execute(
                "SELECT id FROM trades WHERE broker_order_id = ?",
                (broker_order_id,),
            ).fetchone()
            if existing is not None:
                return existing["id"], False
            ts = filled_at or self._sqlite_utc_timestamp(datetime.now(UTC))
            act_norm = (action or "").upper()
            if reasoning:
                reasoning_final = reasoning
            elif act_norm == "STOP_OUT":
                reasoning_final = (
                    "Broker-initiated protective-stop fill — the system "
                    "never submitted this order as a decision; written "
                    "back by the stop-out reconciler (2026-08-28 "
                    "ONDS/CCJ gap; see ReconciliationConfig)."
                )
            elif act_norm == "RECONCILED_EXIT":
                # item 173(a): the reconciler proved a broker exit happened but
                # NOT that it was a protective stop — say exactly that, never a
                # cause it can't stand behind.
                reasoning_final = (
                    "Broker-initiated exit the ledger never saw; the broker's "
                    "order type did not identify it as a protective stop, so it "
                    "is recorded as an unattributed reconciled exit rather than "
                    "a STOP_OUT (item 173(a); see ReconciliationConfig)."
                )
            else:
                reasoning_final = (
                    f"Broker-initiated {act_norm or 'exit'} the ledger never "
                    "saw; written back by the exit reconciler (item 173(a); "
                    "see ReconciliationConfig)."
                )
            position_id = self._resolve_new_row_position_id(
                symbol, action, qty=qty, fill_status="filled", fill_qty=qty,
                timestamp=ts,
            )
            exit_category = _categorize_exit_reason(action, reasoning_final, "filled", qty)
            # Conviction ledger (§7.2): this function's entire reason for
            # existing is "the broker closed this with NO row and NO
            # decision ever written" (see docstring above) — there is no
            # decision_id parameter to accept here, so the label is always
            # the honest absence, never conditional.
            decision_link_status = "no_originating_decision"
            cur = self.conn.execute(
                "INSERT INTO trades (symbol, action, qty, price, reasoning, "
                "run_id, broker_order_id, fill_status, fill_qty, fill_price, "
                "fill_reconciled_at, timestamp, position_id, exit_reason_category, "
                "decision_id_status) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 'filled', ?, ?, datetime('now'), ?, ?, ?, ?)",
                (
                    symbol, action, qty, price, reasoning_final,
                    run_id, broker_order_id, qty, price, ts,
                    position_id, exit_category, decision_link_status,
                ),
            )
            row_id = cur.lastrowid
            realized = self._realized_pnl_through_trade(symbol, row_id)
            self.conn.execute(
                "UPDATE trades SET realized_pnl = ? WHERE id = ?",
                (realized, row_id),
            )
            self.conn.commit()
            return row_id, True
        return self._locked_write(_do, label="insert_stop_out_trade")

    def get_unreconciled_orders(self, run_id: str | None = None) -> list[dict]:
        """Trade rows with broker_order_id set but fill_status still 'submitted'.

        Pipeline's reconciliation step fetches these and asks the broker for
        their terminal status. Scoping to run_id lets per-run reconciliation
        not touch stragglers from other runs.
        """
        conditions = ["fill_status = 'submitted'", "broker_order_id IS NOT NULL"]
        params: list = []
        if run_id:
            conditions.append("run_id = ?")
            params.append(run_id)
        where = " AND ".join(conditions)
        with self._lock:
            rows = self.conn.execute(
                f"SELECT * FROM trades WHERE {where}", tuple(params),
            ).fetchall()
        return [dict(r) for r in rows]

    def get_orphaned_pending_submits(
        self, min_age_seconds: int = 120,
    ) -> list[dict]:
        """BUY write-ahead rows the broker may or may not have received:
        fill_status 'pending_submit' with broker_order_id still NULL —
        a crash between submit_order() returning and
        confirm_trade_submitted() landing.

        audit F4: confirm_trade_submitted's docstring promised reconcile
        could detect orphans by exactly this predicate, but nothing swept
        them — a real broker fill could go forever untracked. Age-gated
        (timestamp older than min_age_seconds) so a same-process in-flight
        submit — converted to submitted/submit_failed within microseconds
        — is never misread as an orphan; real orphans are from a prior
        crashed session and are minutes-to-days old. The cutoff uses
        SQLite's own clock on both sides (datetime('now', ?)) so there's
        no host-TZ / format skew.
        """
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM trades WHERE fill_status = 'pending_submit' "
                "AND broker_order_id IS NULL "
                "AND timestamp < datetime('now', ?) "
                "ORDER BY timestamp ASC",
                (f"-{int(min_age_seconds)} seconds",),
            ).fetchall()
        return [dict(r) for r in rows]

    def has_pending_action_for_symbol(
        self, symbol: str, action: str, today_only: bool = True,
    ) -> bool:
        """True if a (symbol, action) trade row exists with fill_status
        'submitted' and a broker_order_id — i.e., a previous submission
        is still in flight at the broker.

        Used to keep consecutive intra_check ticks from re-firing the same
        EMERGENCY_SELL while the first limit order is still pending fill.
        Without this, intra at T submits a -1% LIMIT EMERGENCY_SELL, the
        tape goes through it without filling, and intra at T+30min sees
        the position still on book and submits a duplicate — risking
        double-exit on a partial fill of the first order.

        today_only restricts the lookup to the current ET trading day so
        a stale 'submitted' row from a previous session can't permanently
        block a fresh exit. If your reconciliation pass updated the row
        to a terminal status, this returns False as expected.
        """
        conditions = [
            "fill_status = 'submitted'",
            "broker_order_id IS NOT NULL",
            "symbol = ?",
            "action = ?",
        ]
        params: list = [symbol, action]
        if today_only:
            start, end = self._et_day_utc_bounds()
            conditions.append("timestamp >= ?")
            conditions.append("timestamp < ?")
            params.extend([start, end])
        where = " AND ".join(conditions)
        with self._lock:
            row = self.conn.execute(
                f"SELECT 1 FROM trades WHERE {where} LIMIT 1", tuple(params),
            ).fetchone()
        return row is not None

    def insert_pending_protection_restore(
        self, *, symbol: str, sell_order_id: str,
        position_qty_before_sell: float, specs_json: str,
        run_id: str | None = None, side: str | None = None,
    ) -> int:
        """Persist an orphaned protection-restore intent.

        Written when _finalize_protection_after_sell can't act now —
        either cancel of the lingering SELL raised, or the order didn't
        converge to terminal within the short post-cancel wait. Drained
        at session start: the pending row's sell_order_id is re-queried
        for terminal status, and if now terminal, the persisted specs
        drive a fresh finalize attempt.

        `side` is the PROTECTIVE-STOP / closing-order side, which coincide:
        "sell" (default) for a LONG (its protective stop is a SELL below
        entry, and a long is closed by selling) and "buy" for a SHORT (its
        protective stop is a BUY above entry, and a short is closed by
        covering). This is the REAL side, known at write time by whoever is
        closing the position, and is read back by
        `TradingPipeline._resolve_wal_row_side` under exactly this
        convention. (An earlier version of this docstring stated the
        mapping backwards — long→"buy", short→"sell" — which never matched
        the writers or the reader; corrected here.) NULL only for a row
        written before this column existed; the drain path treats NULL as
        the long-assuming fallback (see
        `TradingPipeline._derive_close_side_for_drain`).

        SCALE-IN rows (sell_order_id == `scale_in.WAL_SCALE_IN_SENTINEL`)
        follow the IDENTICAL convention, but they are dispatched to
        `scale_in.drain_scale_in_row` by their sentinel BEFORE any generic
        side reader runs, and that drain classifies long/short from the SIGN
        of `position_qty_before_sell` rather than this column — so a scale-in
        row is never interpreted with a generic reader's meaning either way.
        """
        def _do():
            cur = self.conn.execute(
                "INSERT INTO pending_protection_restores "
                "(symbol, sell_order_id, position_qty_before_sell, specs_json, "
                "run_id, side) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (symbol, sell_order_id, position_qty_before_sell, specs_json,
                 run_id, side),
            )
            row_id = cur.lastrowid or 0
            # Item 193: attribute the id before it can be forgotten. Best
            # effort on purpose - an audit failure must never stop a
            # protective-restore intent from being persisted.
            try:
                self.conn.execute(
                    "INSERT OR IGNORE INTO protection_restore_wal_audit "
                    "(row_id, symbol, sell_order_id, "
                    "position_qty_before_sell, side, run_id) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (row_id, symbol, sell_order_id,
                     position_qty_before_sell, side, run_id),
                )
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "protection-restore WAL audit row %s not written: %s",
                    row_id, exc,
                )
            self.conn.commit()
            return row_id
        return self._locked_write(_do, label="insert_pending_protection_restore")

    def get_protection_restore_wal_audit(self) -> list[dict]:
        """Every protection-restore WAL id ever handed out, oldest first.

        Item 193's completeness check: join these against the
        `scale_in|protective_sell_cancelled` events' own `wal_row_id`, and
        an id present here but absent there was spent by the protected-sell
        exit path or by a rolled-back preparation, not by an unrecorded
        cancel.
        """
        with self._lock:
            cur = self.conn.execute(
                "SELECT row_id, symbol, sell_order_id, "
                "position_qty_before_sell, side, run_id, created_at "
                "FROM protection_restore_wal_audit ORDER BY row_id"
            )
            return [dict(r) for r in cur.fetchall()]

    def get_pending_protection_restores(self) -> list[dict]:
        """All currently-pending protection-restore rows, oldest first."""
        with self._lock:
            rows = self.conn.execute(
                "SELECT id, symbol, sell_order_id, position_qty_before_sell, "
                "specs_json, created_at, run_id, side FROM pending_protection_restores "
                "ORDER BY created_at ASC"
            ).fetchall()
        return [dict(r) for r in rows]

    def delete_pending_protection_restore(self, row_id: int) -> int:
        """Remove a row by its primary key (after successful drain)."""
        def _do():
            cur = self.conn.execute(
                "DELETE FROM pending_protection_restores WHERE id = ?",
                (row_id,),
            )
            self.conn.commit()
            return cur.rowcount or 0
        return self._locked_write(_do, label="delete_pending_protection_restore")

    def update_pending_protection_restore(
        self, row_id: int, *,
        sell_order_id: str | None = None,
        position_qty_before_sell: float | None = None,
        specs_json: str | None = None,
        side: str | None = None,
    ) -> int:
        """Partial-update a recovery row (only the provided fields).

        audit F1 write-ahead lifecycle: a row is inserted BEFORE
        cancel_protective_stops with a sentinel sell_order_id; this flips
        it to the real broker order id once the SELL is accepted, and
        finalize-on-bail uses it to UPDATE the existing row (instead of
        INSERTing a duplicate alongside the write-ahead row).

        ``side`` (Stage 3, shorts): re-affirms which side this row
        protects — normally already set at the initial write-ahead
        INSERT, this lets a caller correct/set it on UPDATE too.
        """
        sets: list[str] = []
        params: list = []
        if sell_order_id is not None:
            sets.append("sell_order_id = ?")
            params.append(sell_order_id)
        if position_qty_before_sell is not None:
            sets.append("position_qty_before_sell = ?")
            params.append(position_qty_before_sell)
        if specs_json is not None:
            sets.append("specs_json = ?")
            params.append(specs_json)
        if side is not None:
            sets.append("side = ?")
            params.append(side)
        if not sets:
            return 0
        params.append(row_id)
        with self._lock:
            cur = self.conn.execute(
                f"UPDATE pending_protection_restores SET {', '.join(sets)} "
                "WHERE id = ?",
                tuple(params),
            )
            self.conn.commit()
            return cur.rowcount or 0

    def update_pending_protection_restore_specs(
        self, row_id: int, specs_json: str,
    ) -> int:
        """Replace the specs_json of an existing recovery row.

        Used by the drain path's partial-restore handling: when 1 of N
        specs landed on this drain attempt, the next drain should only
        retry the N-1 that failed (re-submitting the already-alive stop
        either creates a duplicate or hits held_for_orders, neither
        productive). Codex r10 #1.
        """
        with self._lock:
            cur = self.conn.execute(
                "UPDATE pending_protection_restores SET specs_json = ? WHERE id = ?",
                (specs_json, row_id),
            )
            self.conn.commit()
            return cur.rowcount or 0

    def insert_pending_repeg(
        self, *, trade_row_id: int | None, symbol: str, old_order_id: str,
        new_order_id: str, run_id: str | None = None,
    ) -> int:
        """Persist the intent to replace `old_order_id`.

        `new_order_id` is the caller's sentinel until the broker answers.
        """
        def _do():
            cur = self.conn.execute(
                "INSERT INTO pending_repegs "
                "(trade_row_id, symbol, old_order_id, new_order_id, run_id) "
                "VALUES (?, ?, ?, ?, ?)",
                (trade_row_id, symbol, old_order_id, new_order_id, run_id),
            )
            self.conn.commit()
            return cur.lastrowid or 0
        return self._locked_write(_do, label="insert_pending_repeg")

    def get_pending_repegs(self) -> list[dict]:
        """All currently-pending re-peg rows, oldest first."""
        with self._lock:
            rows = self.conn.execute(
                "SELECT id, trade_row_id, symbol, old_order_id, new_order_id, "
                "created_at, run_id FROM pending_repegs ORDER BY created_at ASC"
            ).fetchall()
        return [dict(r) for r in rows]

    def resolve_pending_repeg(self, row_id: int, new_order_id: str) -> int:
        """Record the id the broker actually minted for a pending re-peg."""
        def _do():
            cur = self.conn.execute(
                "UPDATE pending_repegs SET new_order_id = ? WHERE id = ?",
                (new_order_id, row_id),
            )
            self.conn.commit()
            return cur.rowcount or 0
        return self._locked_write(_do, label="resolve_pending_repeg")

    def delete_pending_repeg(self, row_id: int) -> int:
        """Remove a re-peg WAL row once the trades row is authoritative."""
        def _do():
            cur = self.conn.execute(
                "DELETE FROM pending_repegs WHERE id = ?", (row_id,),
            )
            self.conn.commit()
            return cur.rowcount or 0
        return self._locked_write(_do, label="delete_pending_repeg")

    def prune_pending_repegs(self, keep_days: int = 30) -> int:
        """Delete pending_repegs rows older than keep_days.

        Same reasoning as `prune_pending_protection_restores`: the drain
        re-attempts every session, so a row that survives 30 days is one the
        broker can no longer resolve (order id aged out of history). Refuses
        keep_days <= 0 rather than wiping a recovery queue.
        """
        if keep_days <= 0:
            raise ValueError(
                f"prune_pending_repegs: keep_days must be > 0, got {keep_days}"
            )
        with self._lock:
            stale = self.conn.execute(
                "SELECT id, symbol, old_order_id, created_at FROM pending_repegs "
                "WHERE created_at < datetime('now', ?)",
                (f"-{keep_days} days",),
            ).fetchall()
            if not stale:
                return 0
            for row in stale:
                logger.info(
                    "Pruning stale pending_repeg row %d: symbol=%s "
                    "old_order_id=%s created_at=%s (>%dd old)",
                    row["id"], row["symbol"], row["old_order_id"],
                    row["created_at"], keep_days,
                )
            cursor = self.conn.execute(
                "DELETE FROM pending_repegs WHERE created_at < datetime('now', ?)",
                (f"-{keep_days} days",),
            )
            self.conn.commit()
            return cursor.rowcount or 0

    def get_trades(self, symbol: str | None = None, limit: int = 100,
                    today_only: bool = False,
                    executed_only: bool = False) -> list[dict]:
        conditions = []
        params: list = []
        if symbol:
            conditions.append("symbol = ?")
            params.append(symbol)
        if today_only:
            start_utc, end_utc = self._et_day_utc_bounds()
            conditions.append("timestamp >= ? AND timestamp < ?")
            params.extend([start_utc, end_utc])
        if executed_only:
            conditions.append(self._executed_trade_predicate())
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        with self._lock:
            # Secondary order-by on id ensures tie-break ordering is
            # deterministic — SQLite's timestamp precision is 1 second, so
            # a BUY inserted at T0 and TAKE_PROFIT inserted at T0+0.01 both
            # carry the same timestamp string. Without id DESC, duplicate-
            # timestamp rows come back in indeterminate order and logic
            # that scans "trades newer than the most recent BUY" can miss
            # the newer row.
            rows = self.conn.execute(
                f"SELECT * FROM trades {where} ORDER BY timestamp DESC, id DESC LIMIT ?",
                (*params, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def _accumulate_excursions(self, position) -> None:
        """Widen the recorded worst- and best-against-entry excursions.

        STOP-FLOOR EVIDENCE, RECORDING ONLY. These are the running half of
        the facts the desk needs before it can ever check its ratified
        minimum stop width against its own trades (the pinned half,
        `entry_atr`, `initial_stop_loss` and `stop_basis`, is written at
        entry by `insert_trade`; the resolved half, `realized_pnl` and
        `exit_reason_category`, lands when the position closes). Read the
        `max_adverse_excursion` migration note in `_migrate` for the hard
        limit on its use: it may show whether the floor was ever VIOLATED in
        practice, and it may NOT be optimised against to produce a new
        multiplier. Doctrine bars fitting a number to this desk's history.

        Monotonic: the stored figure only ever widens while the position is
        open, so a recovery cannot erase the excursion that preceded it. It
        is written onto the OPENING rows of the position (the rows carrying
        `entry_atr`), which is where a later reader joins entry ATR, stop
        basis, excursion and realised outcome together.

        Side-agnostic: "against" is below entry for a long and above entry
        for a short, decided from the sign of `qty` rather than from a
        stored action, because that is the only side fact a broker position
        snapshot carries. A zero or missing entry price is skipped rather
        than guessed.

        Assumes the caller holds `self._lock` and an open transaction —
        `sync_positions` is the only caller and does both.
        """
        try:
            entry = float(getattr(position, "avg_entry", 0) or 0)
            last = float(getattr(position, "current_price", 0) or 0)
            qty = float(getattr(position, "qty", 0) or 0)
        except (TypeError, ValueError):
            return
        if entry <= 0 or last <= 0 or qty == 0:
            return
        # Excursion AGAINST the position, in price units. Never negative:
        # a position in profit contributes nothing. The favourable leg is
        # its exact mirror, and at most one of the two is positive at any
        # snapshot, so each column widens only on the snapshots that
        # actually evidence it.
        adverse = (entry - last) if qty > 0 else (last - entry)
        favourable = -adverse
        for column, excursion in (
            ("max_adverse_excursion", adverse),
            ("max_favourable_excursion", favourable),
        ):
            if excursion <= 0:
                continue
            self.conn.execute(
                f"UPDATE trades SET {column} = ? "  # noqa: S608 - literal, not input
                "WHERE symbol = ? AND action IN ('BUY', 'SHORT') "
                "AND entry_atr IS NOT NULL "
                f"AND ({column} IS NULL OR {column} < ?) "
                "AND position_id IN ("
                "  SELECT position_id FROM trades WHERE symbol = ? "
                "  AND position_id IS NOT NULL ORDER BY id DESC LIMIT 1)",
                (excursion, position.symbol, excursion, position.symbol),
            )

    def record_overnight_gap(
        self, symbol: str, prev_close: float, open_price: float,
        session_date: str,
    ) -> bool:
        """Record one session's ADVERSE overnight gap against an open SHORT.

        SHORT-SIDE GAP EVIDENCE, RECORDING ONLY. Read the
        `max_adverse_overnight_gap` migration note in `_migrate` for why
        this exists (item 186 — the sizing haircut cannot be read off the
        instrument because the evidence was never kept) and for the hard
        limit on its use: nothing may read it back into a sizing, stop or
        exit decision, and it may not be swept for an optimal multiple.

        The stored figure is the WORST (largest) adverse gap seen on any
        session the short was held, `open - prev_close` in price units,
        positive when the name gapped UP against the short. It is stored
        SIGNED and unfiltered: a short every one of whose gaps ran in its
        favour records a negative worst, which is a real and different fact
        from "never observed". `overnight_gap_sessions` counts the sessions
        observed so the two stay distinguishable.

        Written onto the OPENING rows of the position (the `SHORT` rows
        carrying `entry_atr`), which is where a later reader joins the gap
        to the volatility read and the stop distance pinned at entry —
        `entry_atr` and `initial_stop_loss` on the same row — and, once the
        position closes, to its realised outcome.

        Idempotent per session: `last_overnight_gap_date` gates the write,
        so a second position sync on the same date cannot count one gap
        twice. Returns True when a row was updated.
        """
        try:
            prev_close = float(prev_close)
            open_price = float(open_price)
        except (TypeError, ValueError):
            return False
        if prev_close <= 0 or open_price <= 0 or not session_date:
            return False
        gap = open_price - prev_close
        with self._lock:
            cur = self.conn.execute(
                "UPDATE trades SET "
                "  max_adverse_overnight_gap = CASE "
                "    WHEN max_adverse_overnight_gap IS NULL "
                "      OR max_adverse_overnight_gap < ? THEN ? "
                "    ELSE max_adverse_overnight_gap END, "
                "  overnight_gap_sessions = COALESCE(overnight_gap_sessions, 0) + 1, "
                "  last_overnight_gap_date = ? "
                "WHERE symbol = ? AND action = 'SHORT' "
                "AND entry_atr IS NOT NULL "
                "AND (last_overnight_gap_date IS NULL OR last_overnight_gap_date < ?) "
                "AND position_id IN ("
                "  SELECT position_id FROM trades WHERE symbol = ? "
                "  AND position_id IS NOT NULL ORDER BY id DESC LIMIT 1)",
                (gap, gap, session_date, symbol, session_date, symbol),
            )
            self.conn.commit()
            return cur.rowcount > 0

    def _accumulate_level_distances(self, position) -> None:
        """Widen what the market has done to the stop's structural level.

        ITEM 55 RECORDING, FALSIFICATION ONLY, and it decides nothing. The
        pinned half (`stop_level_basis`) says what the stop stood on; this
        is the running half that says what price then did to it, so that
        "is this a real level" becomes answerable from the desk's own
        record instead of from argument. Read the `stop_level_basis`
        migration note for the hard limit on its use: it may show the
        current definition of a level is WRONG, and it may NEVER be swept
        for a better pivot window or zone width.

        TWO RAW DISTANCES, NO VERDICT. `level_max_penetration` is how far
        beyond the zone's FAR edge price has travelled (monotonic upward,
        never negative); `level_closest_approach` is the smallest gap ever
        seen to the zone's NEAR edge (monotonic downward, signed, negative
        once price is inside). Nothing here calls an outcome "respected",
        "pierced" or "broken", because each of those needs a cutoff nobody
        can source; a later reader states its own cutoff and applies it to
        these numbers, which were never rounded to one.

        Side-agnostic in the same way as `_accumulate_excursions`, and for
        the same reason: the side is read off the sign of `qty`, the only
        side fact a broker position snapshot carries. A row with no
        `stop_level_basis`, or one whose record had no level behind the
        stop, is skipped and stays NULL rather than being given a
        substitute.

        Assumes the caller holds `self._lock` and an open transaction —
        `sync_positions` is the only caller and does both.
        """
        try:
            last = float(getattr(position, "current_price", 0) or 0)
            qty = float(getattr(position, "qty", 0) or 0)
        except (TypeError, ValueError):
            return
        if last <= 0 or qty == 0:
            return
        row = self.conn.execute(
            "SELECT id, stop_level_basis FROM trades "
            "WHERE symbol = ? AND action IN ('BUY', 'SHORT') "
            "AND stop_level_basis IS NOT NULL "
            "AND position_id IN ("
            "  SELECT position_id FROM trades WHERE symbol = ? "
            "  AND position_id IS NOT NULL ORDER BY id DESC LIMIT 1) "
            "ORDER BY id DESC LIMIT 1",
            (position.symbol, position.symbol),
        ).fetchone()
        if row is None:
            return
        try:
            basis = json.loads(row[1])
        except (TypeError, ValueError):
            return
        if not isinstance(basis, dict) or not basis.get("level_backed"):
            return
        low, high = basis.get("zone_low"), basis.get("zone_high")
        if not isinstance(low, (int, float)) or not isinstance(high, (int, float)):
            return
        if qty > 0:
            # Long: the level is support below, so the far edge is the
            # bottom of the zone and the near edge is the top of it.
            penetration = float(low) - last
            approach = last - float(high)
        else:
            penetration = last - float(high)
            approach = float(low) - last
        if penetration > 0:
            self.conn.execute(
                "UPDATE trades SET level_max_penetration = ? WHERE id = ? "
                "AND (level_max_penetration IS NULL OR level_max_penetration < ?)",
                (penetration, row[0], penetration),
            )
        self.conn.execute(
            "UPDATE trades SET level_closest_approach = ? WHERE id = ? "
            "AND (level_closest_approach IS NULL OR level_closest_approach > ?)",
            (approach, row[0], approach),
        )

    def sync_positions(self, positions) -> None:
        """Replace positions table with a fresh broker snapshot.

        Upserts rows for currently-held symbols and deletes rows for any symbol
        no longer present. Prevents stale closed positions from lingering in the DB.

        Wraps DELETE + INSERT loop in an explicit BEGIN/COMMIT transaction so
        a crash between the DELETE and the first INSERT cannot leave the table
        in a half-state (would otherwise leave the next session's reviewer
        reading an empty positions snapshot while the broker still holds them).
        Mirrors the atomic-write discipline used in `save_evening_snapshot`.
        """
        current_symbols = {p.symbol for p in positions}
        with self._lock:
            try:
                self.conn.execute("BEGIN")
                if current_symbols:
                    placeholders = ",".join("?" for _ in current_symbols)
                    self.conn.execute(
                        f"DELETE FROM positions WHERE symbol NOT IN ({placeholders})",
                        tuple(current_symbols),
                    )
                else:
                    self.conn.execute("DELETE FROM positions")
                for p in positions:
                    self.conn.execute(
                        """INSERT INTO positions (symbol, qty, avg_entry, current_price, market_value, unrealized_pnl, sector, updated_at)
                           VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'))
                           ON CONFLICT(symbol) DO UPDATE SET
                             qty=excluded.qty, avg_entry=excluded.avg_entry,
                             current_price=excluded.current_price, market_value=excluded.market_value,
                             unrealized_pnl=excluded.unrealized_pnl, sector=excluded.sector,
                             updated_at=datetime('now')""",
                        (p.symbol, p.qty, p.avg_entry, p.current_price, p.market_value,
                         p.unrealized_pnl, p.sector),
                    )
                    # Stop-floor evidence, RECORDING ONLY — see the
                    # `max_adverse_excursion` migration note for what this
                    # data may and may NOT be used for. Nothing reads it back
                    # into a trading decision; it cannot change sizing, stop
                    # placement or an exit. Inside the same transaction as
                    # the snapshot it is derived from, so the two can never
                    # disagree, and swallowed on error so a recording problem
                    # can never fail a position sync.
                    try:
                        self._accumulate_excursions(p)
                        self._accumulate_level_distances(p)
                    except Exception:
                        logger.debug(
                            "excursion recording skipped for %s",
                            getattr(p, "symbol", "?"), exc_info=True,
                        )
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                raise

    def update_open_take_profit(
        self, symbol: str, new_target: float, *, action: str | None = None,
    ) -> bool:
        """Write a re-derived target onto every opening row of this position.

        Mirrors `update_open_stop_loss`. Never touches `initial_take_profit`
        — that column is the entry derivation and the denominator of
        progress/pace, and a revision must not be able to move it.

        Refuses a non-positive target rather than zeroing the field: a zero
        target would make `thesis_progress_pct` undefined, and a blank is
        exactly what the per-symbol refusal record exists to avoid.
        """
        try:
            target = float(new_target)
        except (TypeError, ValueError):
            return False
        if not target > 0:
            logger.error(
                "update_open_take_profit refused a non-positive target for "
                "%s: %r", symbol, new_target,
            )
            return False
        act = (action or "").strip().upper()
        if act and act not in ("BUY", "SHORT"):
            logger.error(
                "update_open_take_profit refused unknown action %r for %s",
                action, symbol,
            )
            return False

        def _do():
            sql = (
                "UPDATE trades SET take_profit = ? WHERE symbol = ? "
                "AND UPPER(action) IN ('BUY', 'SHORT')"
            )
            params: list = [target, symbol.upper()]
            if act:
                sql = (
                    "UPDATE trades SET take_profit = ? WHERE symbol = ? "
                    "AND UPPER(action) = ?"
                )
                params = [target, symbol.upper(), act]
            sql += (
                " AND position_id = (SELECT position_id FROM trades "
                "WHERE symbol = ? AND UPPER(action) IN ('BUY', 'SHORT') "
                "ORDER BY id DESC LIMIT 1)"
            )
            params.append(symbol.upper())
            cur = self.conn.execute(sql, tuple(params))
            self.conn.commit()
            return cur.rowcount > 0
        return self._locked_write(_do, label="update_open_take_profit")

    def prune_trades(self, keep_days: int = 365 * 5) -> int:
        """Delete trades rows older than keep_days. Default retention 5 years.

        Kept long for audit purposes — still finite to bound table size over a
        decade-plus horizon. Returns count deleted.
        """
        if keep_days <= 0:
            # `datetime('now', '-0 days')` == 'now' → deletes the entire
            # trades audit log. Refuse rather than silently destroy
            # potentially years of broker history.
            raise ValueError(f"prune_trades: keep_days must be > 0, got {keep_days}")
        with self._lock:
            cursor = self.conn.execute(
                "DELETE FROM trades WHERE timestamp < datetime('now', ?)",
                (f"-{keep_days} days",),
            )
            self.conn.commit()
            return cursor.rowcount or 0

    def prune_pending_protection_restores(self, keep_days: int = 30) -> int:
        """Delete pending_protection_restores rows older than keep_days.

        Drain re-attempts these rows every session; a row that survives
        ~30 calendar days (~20 trading sessions) means either:
          - broker forgot the sell_order_id (deep history GC),
          - the underlying position is gone via other paths (manual
            close, EMERGENCY_SELL during a separate session), or
          - the row's specs_json is malformed in a way drain can't
            recover from automatically.
        In any of these cases, indefinite retention is just operational
        noise — drain can't help. Logs the symbols pruned at INFO so
        manual review remains possible. Returns count deleted.
        """
        if keep_days <= 0:
            # `datetime('now', '-0 days')` == 'now' → deletes EVERYTHING.
            # Caller almost certainly passed a typo / config bug. Refuse
            # rather than silently wipe a recovery queue.
            raise ValueError(
                f"prune_pending_protection_restores: keep_days must be > 0, got {keep_days}"
            )
        with self._lock:
            stale = self.conn.execute(
                "SELECT id, symbol, sell_order_id, created_at "
                "FROM pending_protection_restores "
                "WHERE created_at < datetime('now', ?)",
                (f"-{keep_days} days",),
            ).fetchall()
            if not stale:
                return 0
            for row in stale:
                logger.info(
                    "Pruning stale pending_protection_restore row %d: "
                    "symbol=%s sell_order_id=%s created_at=%s (>%dd old)",
                    row["id"], row["symbol"], row["sell_order_id"],
                    row["created_at"], keep_days,
                )
            cursor = self.conn.execute(
                "DELETE FROM pending_protection_restores "
                "WHERE created_at < datetime('now', ?)",
                (f"-{keep_days} days",),
            )
            self.conn.commit()
            return cursor.rowcount or 0

    def backfill_position_ids(self, *, dry_run: bool = False) -> dict:
        """One-time reconstruction of `position_id` chains for trades rows
        written before this column existed (Phase 6, §6.2a).

        Uses the exact same FIFO logic `_assign_position_ids` derives
        (matched by symbol, oldest-first, a BUY or a SHORT opens/adds, a
        recognized exit-family action for that side reduces — mirroring the
        accounting `compute_trade_calibration` already trusts for win-rate /
        avg-hold-days) so the backfilled chains never disagree with those
        numbers. Re-running this after 2026-08-31 assigns chains to short
        history that could not receive one before; a row that already carries
        an id is still never reassigned.

        Never guesses: a row this can't confidently attach to an open chain
        — typically the ledger's very first record for a symbol whose real
        position predates this system (a SELL/exit with no prior BUY on
        record), or a stray exit after the book had already gone flat — is
        left NULL rather than assigned a fabricated chain.

        Idempotent and safe to run against a database where live trading has
        already assigned SOME rows (because this migration shipped and
        started minting ids for new trades before the backfill got run
        against older history): a row that already carries a position_id is
        never reassigned, and the chain state used to fill in the gaps
        around it treats that id as ground truth.

        `dry_run=True` computes and reports without writing anything.

        Returns:
            {"total": int,            # every trades row in the database
             "already_assigned": int, # had a position_id before this ran
             "assigned": int,         # newly assigned by this run
             "left_null_ambiguous": int,  # BUY/exit-family, but no chain
                                           # to confidently attach to
             "not_applicable": int}   # HOLD / SWEEP_BUY / SWEEP_SELL /
                                       # anything not part of a position
                                       # chain by design, not by ambiguity
        """
        def _do():
            rows = self.conn.execute(
                "SELECT id, symbol, action, qty, fill_qty, fill_status, "
                "position_id FROM trades ORDER BY symbol, timestamp, id"
            ).fetchall()
            rows = [dict(r) for r in rows]
            by_symbol: dict[str, list[dict]] = {}
            for r in rows:
                by_symbol.setdefault(r["symbol"], []).append(r)

            total = len(rows)
            already_assigned = sum(1 for r in rows if r.get("position_id"))
            assigned = 0
            left_null_ambiguous = 0
            not_applicable = 0
            updates: list[tuple[str, int]] = []

            for symbol_rows in by_symbol.values():
                new_assignments = _assign_position_ids(symbol_rows)
                for r in symbol_rows:
                    if r.get("position_id"):
                        continue  # ground truth — never reassigned
                    action = (r.get("action") or "").upper()
                    is_positionable = (
                        action in _POSITION_OPEN_ACTIONS
                        or _is_position_exit_action(action)
                    )
                    new_id = new_assignments.get(r["id"])
                    if new_id:
                        assigned += 1
                        updates.append((new_id, r["id"]))
                    elif is_positionable:
                        left_null_ambiguous += 1
                    else:
                        not_applicable += 1

            if not dry_run and updates:
                self.conn.executemany(
                    "UPDATE trades SET position_id = ? WHERE id = ?", updates,
                )
                self.conn.commit()
            return {
                "total": total,
                "already_assigned": already_assigned,
                "assigned": assigned,
                "left_null_ambiguous": left_null_ambiguous,
                "not_applicable": not_applicable,
            }
        return self._locked_write(_do, label="backfill_position_ids")

    def backfill_conviction_ledger(self, *, dry_run: bool = False) -> dict:
        """One-time reconstruction of the conviction-ledger columns (spec
        §7.2) for `trades` rows written before they existed.

        Two INDEPENDENT repairs, run together because both read `trades` in
        one pass (mirrors `backfill_position_ids`'s shape and safety
        posture — dry-run by default, idempotent, never guesses):

        1. `decision_id_status` on every exit-family row (see
           `_is_exit_family_for_decision_linking`) that predates the
           column. This is NEVER ambiguous, unlike `position_id`'s
           `left_null_ambiguous` case: every `insert_trade` / `insert_
           stop_out_trade` call site in this codebase is enumerated, and an
           exit row's `decision_id` is NULL if and only if the code path
           that wrote it never had one to attach. So every eligible row
           gets EITHER 'linked' or 'no_originating_decision' — there is no
           third "can't tell" bucket the way position_id has.

        2. `conviction` / `requested_risk_pct` / `decision_model` on BUY/
           SHORT rows that already carry a real `decision_id`: recovered by
           joining `agent_logs` (agent_name='portfolio_manager', matching
           decision_id) and reading the `targets` entry matching this
           trade's symbol out of `full_response` (see
           `_find_pm_target_for_symbol` — handles both the fenced-```json
           and raw-JSON formats seen in real history).

           `allocated_risk_pct` (the POST-clamp figure the constructor's
           RiskPlan actually granted) is DELIBERATELY NEVER backfilled —
           it was never persisted anywhere retroactively readable (only
           the PM's pre-clamp ask survives, inside `full_response`), and
           reconstructing the granted figure would mean re-running the
           constructor's budget rationing against point-in-time book state
           this database does not fully preserve. Every backfilled row
           gets `allocated_risk_pct = NULL`, always, and the returned dict
           reports that as `allocated_risk_pct_recoverable: 0` rather than
           letting a caller assume the gap was closed.

        Idempotent: an exit row is only touched while `decision_id_status
        IS NULL`; an entry row only while `conviction IS NULL AND
        decision_model IS NULL` (a row already touched by this backfill,
        or by live trading after this column existed, is never
        reprocessed). `dry_run=True` (default) computes and returns counts
        without writing.
        """
        def _do():
            # ---- 1. exit rows: decision_id_status (fully recoverable) ----
            exit_rows = self.conn.execute(
                "SELECT id, action, decision_id FROM trades "
                "WHERE decision_id_status IS NULL",
            ).fetchall()
            exit_updates: list[tuple[str, int]] = []
            exit_linked = 0
            exit_no_originating_decision = 0
            exit_not_applicable = 0
            for r in exit_rows:
                status = _resolve_decision_id_status(r["action"], r["decision_id"])
                if status is None:
                    exit_not_applicable += 1
                    continue
                exit_updates.append((status, r["id"]))
                if status == "linked":
                    exit_linked += 1
                else:
                    exit_no_originating_decision += 1

            # ---- 2. entry rows: conviction / requested_risk_pct / decision_model ----
            entry_rows = self.conn.execute(
                "SELECT id, symbol, decision_id FROM trades "
                "WHERE action IN ('BUY', 'SHORT') AND decision_id IS NOT NULL "
                "AND conviction IS NULL AND decision_model IS NULL",
            ).fetchall()
            decision_ids = sorted({r["decision_id"] for r in entry_rows if r["decision_id"]})
            pm_logs: dict[str, dict] = {}
            if decision_ids:
                placeholders = ",".join("?" for _ in decision_ids)
                for row in self.conn.execute(
                    "SELECT decision_id, model, full_response FROM agent_logs "
                    f"WHERE agent_name = 'portfolio_manager' AND decision_id IN ({placeholders})",
                    tuple(decision_ids),
                ).fetchall():
                    # First row wins on a duplicate decision_id (retries are
                    # not expected to share an id, but never overwrite a
                    # resolved match with a later, possibly-unrelated one).
                    pm_logs.setdefault(row["decision_id"], dict(row))

            entry_updates: list[tuple] = []  # (conviction, requested_risk_pct, decision_model, id)
            entry_recovered = 0
            entry_unrecoverable_no_agent_log = 0
            entry_unrecoverable_no_matching_target = 0
            for r in entry_rows:
                log_row = pm_logs.get(r["decision_id"])
                if log_row is None:
                    entry_unrecoverable_no_agent_log += 1
                    continue
                target = _find_pm_target_for_symbol(log_row.get("full_response"), r["symbol"])
                if target is None:
                    entry_unrecoverable_no_matching_target += 1
                    continue
                entry_updates.append((
                    target.get("conviction"),
                    target.get("risk_allocation_pct"),
                    log_row.get("model"),
                    r["id"],
                ))
                entry_recovered += 1

            if not dry_run:
                if exit_updates:
                    self.conn.executemany(
                        "UPDATE trades SET decision_id_status = ? WHERE id = ?",
                        exit_updates,
                    )
                if entry_updates:
                    self.conn.executemany(
                        "UPDATE trades SET conviction = ?, requested_risk_pct = ?, "
                        "decision_model = ? WHERE id = ?",
                        entry_updates,
                    )
                self.conn.commit()

            return {
                "exit_rows_considered": len(exit_rows),
                "exit_linked": exit_linked,
                "exit_no_originating_decision": exit_no_originating_decision,
                "exit_not_applicable": exit_not_applicable,
                "entry_rows_considered": len(entry_rows),
                "entry_recovered": entry_recovered,
                "entry_unrecoverable_no_agent_log": entry_unrecoverable_no_agent_log,
                "entry_unrecoverable_no_matching_target": entry_unrecoverable_no_matching_target,
                # Always 0 — see docstring. Never silently "improves" as a
                # side effect of a future change without this comment being
                # revisited: allocated_risk_pct becoming recoverable would
                # require a NEW data source, not a smarter backfill.
                "allocated_risk_pct_recoverable": 0,
            }
        return self._locked_write(_do, label="backfill_conviction_ledger")
