"""Pipeline stages — explicit, composable, per-responsibility units.

Phase 4 #1 of the architecture work. `TradingPipeline` was a 2600-line
god object whose three `run_*` methods each did data-fetching, LLM
orchestration, risk filtering, order execution, and audit logging
inline. Nothing could be tested in isolation; nothing could be reused
across sessions.

Here we extract the logical phases into stand-alone stages that take a
`RunContext` (explicit shared state), read/write specific fields on it,
and return it (or an early-exit dict) for the next stage.

Morning composes four stages:
  1. MorningResearchStage — parallel macro/news/tech/earnings fan-out
  2. DecisionStage         — L2..L8 memory + PM + Constructor
  3. RiskStage             — hard filter + correlation + RM review + mods
  4. ExecutionStage        — HOLD audit → SELLs → wait fills → BUYs

Midday and evening are *themselves* single-stage workflows (account
snapshot → review/report → log). They have no internal sub-pipeline
to compose, so they stay as TradingPipeline methods rather than being
wrapped in an artificial "stage of one".

Dependency injection pattern: research stage takes each provider/agent
by hand (demonstrates the pure form). Decision/Risk/Execution each take
a `pipeline` reference for the large surface of helpers they share with
TradingPipeline (_build_* memory layers, _filter_* risk helpers,
_order_accepted, _full_sell_qty, etc.). The pragmatic tradeoff: no
tangled re-plumbing of 15+ helpers just to say "zero coupling." Those
helpers are the right extraction boundary for a later phase.
"""

from __future__ import annotations

import logging
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from typing import TYPE_CHECKING

from src.agents.base import agent_log_kwargs
from src.cost_circuit import PaidAnalysisSuspended
from src.data.technical import compute_indicators
from src.models import NewsIntelligenceReport, Nomination, TechAnalysisResult, TechnicalIndicators
from src.nominations import select_nominations
from src.pipeline_context import RunContext

if TYPE_CHECKING:
    from src.agents.earnings_analyst import EarningsAnalystAgent
    from src.agents.macro_analyst import MacroAnalystAgent
    from src.agents.news_analyst import NewsAnalystAgent
    from src.agents.tech_analyst import TechAnalystAgent
    from src.agents.smart_money_analyst import SmartMoneyAnalystAgent
    from src.data.smart_money import SmartMoneySource
    from src.config import AppConfig
    from src.data.earnings import EarningsDataProvider
    from src.data.macro import MacroDataProvider
    from src.data.macro_store import MacroStore
    from src.data.market import MarketDataProvider
    from src.data.news import NewsCoverage, NewsDataProvider
    from src.data.news_store import NewsStore
    from src.data.tech_store import TechStore
    from src.models import TradeDecision
    from src.pipeline import TradingPipeline
    from src.storage.db import Database

logger = logging.getLogger(__name__)

#: Ceiling on how far above the verified reference price an entry limit may
#: be placed, in basis points. Raised from a hardcoded 25 to a configurable 40
#: on 2026-08-27 after VLO proved 25bp is tighter than a normal market open:
#: the ask was 28bp above the reference within seconds of 09:30, so the cap
#: produced an unfillable limit and no trade. 40bp still refuses to pay
#: through a genuinely abnormal book — a gap, a halt reopen, a fat spread —
#: while tolerating ordinary opening drift. Override in `settings.yaml` under
#: `execution.max_entry_slippage_bps`.
MAX_ENTRY_SLIPPAGE_BPS = 40.0


def _macro_regime(macro_analysis) -> str | None:
    """The regime string, from either a MacroAnalysis or a carried-forward dict."""
    if macro_analysis is None:
        return None
    if isinstance(macro_analysis, dict):
        value = macro_analysis.get("regime")
    else:
        value = getattr(macro_analysis, "regime", None)
    return str(value) if value else None


def _book_risk_inputs(ctx, total_value: float):
    """Per-symbol budget risk (% of equity) and correlation clusters, or Nones.

    Spec §2.2. Both are already computed for the PM's own facts block — the
    heat roll-up in `src/risk/metrics.py` and the clusters in
    `src/data/correlation.py` — so the constructor rations the plan against
    precisely the numbers the plan was made against, rather than a second
    view assembled a moment later.

    Returns `(None, None)` when the facts are unavailable. That leaves the
    portfolio ceilings UNENFORCED, which is the correct failure direction
    here: enforcing a 25% ceiling against a book we cannot actually see would
    either block every trade or wave everything through, and both are worse
    than the per-position sizing that still applies regardless.
    """
    facts = getattr(ctx, "facts", None)
    if facts is None:
        return (None, None)
    heat = getattr(facts, "heat", None)
    clusters = getattr(facts, "correlation_clusters", None)
    existing: dict[str, float] | None = None
    if heat is not None and total_value > 0:
        try:
            existing = {
                row.symbol: row.budget_risk_dollars / total_value * 100
                for row in heat.per_position
            }
        except Exception as e:  # noqa: BLE001 — never fail the session on telemetry
            logger.warning("constructor: per-symbol risk map failed: %s", e)
            existing = None
    return (existing, list(clusters) if clusters else None)



#: Sentinel written into `pending_repegs.new_order_id` BEFORE the replace
#: PATCH goes out, and overwritten with the real id when the broker answers.
#: A row still carrying it at session start means the process died inside the
#: replace window: the drain must ASK THE BROKER what the old order became
#: rather than assume either outcome. Mirrors `_WAL_SELL_SENTINEL`.
_WAL_REPEG_SENTINEL = "__WAL_REPEG_PENDING__"


def _repeg_settings(pipeline) -> tuple[int, float, float] | None:
    """(max_attempts, poll_seconds, slippage_bps), or None when re-peg is off.

    Returns None — feature disabled — for anything other than an explicit
    `repeg_enabled is True`. The isinstance guards are the same convention as
    the slippage-cap read above: ~58 tests build the pipeline with a MagicMock
    config whose auto-attributes are truthy, and a MagicMock must never read
    as "yes, replace live orders".
    """
    execution_cfg = getattr(pipeline.config, "execution", None)
    if getattr(execution_cfg, "repeg_enabled", None) is not True:
        return None

    raw_attempts = getattr(execution_cfg, "repeg_max_attempts", None)
    attempts = (
        int(raw_attempts)
        if isinstance(raw_attempts, int) and not isinstance(raw_attempts, bool)
        and 1 <= raw_attempts <= 5
        else 2
    )
    raw_poll = getattr(execution_cfg, "repeg_poll_seconds", None)
    poll = (
        float(raw_poll)
        if isinstance(raw_poll, (int, float)) and not isinstance(raw_poll, bool)
        and 0 < raw_poll <= 30
        else 5.0
    )
    raw_bps = getattr(execution_cfg, "max_entry_slippage_bps", None)
    bps = (
        float(raw_bps)
        if isinstance(raw_bps, (int, float)) and not isinstance(raw_bps, bool)
        and raw_bps > 0
        else MAX_ENTRY_SLIPPAGE_BPS
    )
    return attempts, poll, bps


def _repeg_entry_order(pipeline, ctx, spec: dict) -> tuple[str, float]:
    """Walk a working entry limit toward the market, bounded twice over.

    Returns ``(order_id_to_protect, shares_filled_under_superseded_ids)``.

    THE TWO BOUNDS, both hard:
      * **Price** — the limit never goes above the slippage ceiling the entry
        was already gated on: ``reference * (1 + max_entry_slippage_bps)``.
        The ceiling is computed from the reference captured at submission, NOT
        re-derived from a fresh quote, because a ceiling that follows the
        market is not a ceiling.
      * **Count** — at most `repeg_max_attempts` replacements. A replacement
        mints a new order id; an unbounded loop is an unbounded chain.

    THE FOOTGUN. Alpaca does not edit an order in place. It cancels the old
    one and creates a NEW one with a NEW id, and the old id is dead the
    instant the PATCH is accepted. Three consequences drive every branch here:

      1. `trades.broker_order_id` must be repointed or fill reconciliation
         follows a dead id and concludes the order vanished. That repoint is
         write-ahead-logged (`pending_repegs`) so a crash mid-replace is
         recoverable from the broker rather than lost.
      2. A partially filled order must NEVER be replaced. Fill counters do not
         carry across a replacement, so re-pegging after a partial is how the
         same idea gets bought twice. This function therefore re-reads the fill
         immediately before each attempt and gives up the moment it sees any
         fill at all — leaving the order working, which is the outcome that
         risks doing nothing.
      3. A replacement can be rejected because the order filled in the
         meantime. That is not an error; it is the good case. The original id
         stays authoritative and the chase stops.

    Never raises: a re-peg failing must leave the ordinary
    "protect whatever filled" path exactly as it was.
    """
    order_id = str(spec.get("order_id") or "")
    symbol = spec.get("symbol")
    if not order_id:
        return order_id, 0.0

    settings = _repeg_settings(pipeline)
    if settings is None:
        return order_id, 0.0
    max_attempts, poll_seconds, slippage_bps = settings

    reference = spec.get("reference_price")
    limit_price = spec.get("limit_price")
    requested_qty = spec.get("qty")
    trade_row_id = spec.get("trade_row_id")

    if not isinstance(reference, (int, float)) or reference <= 0:
        return order_id, 0.0
    if not isinstance(limit_price, (int, float)) or limit_price <= 0:
        # A market order has no limit to walk.
        return order_id, 0.0

    ceiling = reference * (1 + slippage_bps / 10_000.0)
    ceiling = round(ceiling, 2 if ceiling >= 1 else 4)
    if limit_price >= ceiling - 1e-9:
        # Expected for most entries: since PR #111 the submitted limit IS the
        # ceiling, so there is nothing to walk toward. Re-peg has room only
        # when the limit was set below the ceiling — e.g. the quote was
        # unavailable at submission and the analyst's entry price was used.
        logger.debug(
            "re-peg %s: limit $%.4f is already at the %.0fbp ceiling $%.4f — "
            "nothing to chase", symbol, limit_price, slippage_bps, ceiling,
        )
        return order_id, 0.0

    carried_fill = 0.0
    for attempt in range(1, max_attempts + 1):
        # Let it work first. A marketable limit usually fills here and the
        # cheapest re-peg is the one never sent.
        try:
            status = pipeline.broker.wait_for_order_terminal(
                order_id, timeout_seconds=poll_seconds,
                poll_interval=min(1.0, poll_seconds),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("re-peg %s: wait failed (%s) — leaving the order "
                           "as-is", symbol, exc)
            return order_id, carried_fill
        if str(status or "").lower() in pipeline.broker._TERMINAL_ORDER_STATES:
            return order_id, carried_fill

        try:
            info = pipeline.broker.get_order_fill_info(order_id) or {}
        except Exception as exc:  # noqa: BLE001
            logger.warning("re-peg %s: fill read failed (%s) — leaving the "
                           "order as-is", symbol, exc)
            return order_id, carried_fill
        if str(info.get("status") or "").lower() in pipeline.broker._TERMINAL_ORDER_STATES:
            return order_id, carried_fill
        try:
            filled_so_far = float(info.get("filled_qty") or 0)
        except (TypeError, ValueError):
            filled_so_far = 0.0
        if filled_so_far > 0:
            # Partial fill. STOP. Replacing now would re-peg a quantity the
            # broker has already partly executed, and the only failure mode
            # worth being paranoid about on this path is buying twice.
            logger.info(
                "re-peg %s: %.4f share(s) already filled on %s — not "
                "replacing a partially filled order; the working remainder "
                "is handed to entry protection unchanged",
                symbol, filled_so_far, order_id,
            )
            _record_pipeline_event(
                pipeline, ctx, symbol, "repeg", "abandoned_partial_fill",
                "repeg_partial_fill", broker_order_id=order_id,
                fill_qty=filled_so_far, attempt=attempt,
            )
            return order_id, carried_fill

        # Where is the market now?
        try:
            quote = pipeline.broker.get_latest_quote(symbol)
        except Exception as exc:  # noqa: BLE001
            logger.warning("re-peg %s: quote failed (%s)", symbol, exc)
            return order_id, carried_fill
        ask = quote.get("ask_price") if isinstance(quote, dict) else None
        if not isinstance(ask, (int, float)) or ask <= 0:
            return order_id, carried_fill

        target = min(float(ask), ceiling)
        target = round(target, 2 if target >= 1 else 4)
        if target <= limit_price + 1e-9:
            # Either the market came back to us or the ceiling binds. Both
            # mean: leave the order working at its current price.
            logger.info(
                "re-peg %s: no room — ask $%.4f vs limit $%.4f, ceiling "
                "$%.4f. Order left working.", symbol, ask, limit_price, ceiling,
            )
            _record_pipeline_event(
                pipeline, ctx, symbol, "repeg", "ceiling_reached",
                "repeg_no_room", broker_order_id=order_id, ask=float(ask),
                limit_price=limit_price, ceiling=ceiling, attempt=attempt,
            )
            return order_id, carried_fill

        new_id, carried_fill, keep_going = _apply_repeg(
            pipeline, ctx, symbol=symbol, order_id=order_id,
            trade_row_id=trade_row_id, target=target,
            requested_qty=requested_qty, attempt=attempt,
            ceiling=ceiling,
        )
        order_id = new_id
        if not keep_going:
            return order_id, carried_fill
        limit_price = target

    logger.info(
        "re-peg %s: attempt cap (%d) reached — order %s left working at the "
        "last re-pegged price", symbol, max_attempts, order_id,
    )
    _record_pipeline_event(
        pipeline, ctx, symbol, "repeg", "attempt_cap_reached",
        "repeg_attempt_cap", broker_order_id=order_id, attempts=max_attempts,
    )
    return order_id, carried_fill


def _apply_repeg(
    pipeline, ctx, *, symbol, order_id: str, trade_row_id, target: float,
    requested_qty, attempt: int, ceiling: float,
) -> tuple[str, float, bool]:
    """One write-ahead-logged replacement.

    Returns ``(order_id_now_authoritative, superseded_filled_qty, keep_going)``.

    The WAL row is the whole point of this function. Between the PATCH
    landing at Alpaca and `repoint_trade_broker_order_id` committing, the
    broker holds a working order under an id this system has written down
    nowhere. A SIGKILL there used to be unrecoverable: the trades row points
    at an order that will report status 'replaced' forever (a status neither
    terminal set in `_reconcile_fills` covers), and the live order is
    untracked. With the row written first, `_drain_pending_repegs` at the next
    session start re-reads the old id, follows Alpaca's `replaced_by` link,
    and repoints the trades row.
    """
    try:
        wal_row_id = pipeline.db.insert_pending_repeg(
            trade_row_id=trade_row_id, symbol=symbol, old_order_id=order_id,
            new_order_id=_WAL_REPEG_SENTINEL,
            run_id=getattr(ctx, "run_id", None),
        )
    except Exception as exc:  # noqa: BLE001
        # No durable intent ⇒ no crash-safe window ⇒ do not open one.
        logger.error(
            "re-peg %s: could not write the WAL row (%s) — NOT replacing "
            "order %s. An unlogged replacement is an untrackable order.",
            symbol, exc, order_id,
        )
        return order_id, 0.0, False

    result = pipeline.broker.replace_entry_limit(
        order_id, target,
        qty=requested_qty if isinstance(requested_qty, (int, float)) else None,
    )
    new_id = (result or {}).get("id")

    if not new_id:
        # The broker did not hand us an id. Either it refused outright (the
        # order filled first — the good case) or the call failed in a way that
        # leaves the outcome genuinely unknown (timeout). Do not guess: ASK.
        resolved = pipeline.broker.resolve_replacement_chain(order_id)
        if resolved is None:
            # Broker unreadable. Leave the WAL row standing; the drain owns it
            # from here. Stop chasing.
            logger.error(
                "re-peg %s: replacement of %s failed AND the order could not "
                "be re-read — leaving WAL row %s for the session-start drain",
                symbol, order_id, wal_row_id,
            )
            return order_id, 0.0, False
        if resolved == order_id:
            # Nothing was minted; the original order is still the only one.
            _delete_repeg_wal(pipeline, wal_row_id)
            logger.info(
                "re-peg %s: broker refused the replacement of %s (%s) — the "
                "original order remains authoritative; chase stops",
                symbol, order_id, (result or {}).get("status", "unknown"),
            )
            _record_pipeline_event(
                pipeline, ctx, symbol, "repeg", "replace_rejected",
                "repeg_replace_rejected", broker_order_id=order_id,
                detail=str((result or {}).get("detail") or
                           (result or {}).get("status") or ""),
                attempt=attempt,
            )
            return order_id, 0.0, False
        # The PATCH actually landed even though the response was lost.
        logger.warning(
            "re-peg %s: replacement of %s reported failure but the broker "
            "shows it replaced by %s — adopting the real id",
            symbol, order_id, resolved,
        )
        new_id = resolved

    # Record the minted id, THEN repoint the trades row, THEN drop the WAL.
    try:
        pipeline.db.resolve_pending_repeg(wal_row_id, str(new_id))
    except Exception as exc:  # noqa: BLE001
        logger.warning("re-peg %s: WAL resolve failed: %s", symbol, exc)
    repointed = _repoint_trade(pipeline, trade_row_id, order_id, str(new_id), symbol)
    if repointed:
        _delete_repeg_wal(pipeline, wal_row_id)

    _record_pipeline_event(
        pipeline, ctx, symbol, "repeg", "replaced", "repeg_replaced",
        broker_order_id=str(new_id), replaces_order_id=order_id,
        limit_price=target, ceiling=ceiling, attempt=attempt,
    )
    logger.info(
        "re-peg %s attempt %d: order %s → %s at $%.4f (ceiling $%.4f)",
        symbol, attempt, order_id, new_id, target, ceiling,
    )

    # THE RACE. The order could have filled between the zero-fill read above
    # and the PATCH being applied. Alpaca would then have replaced only the
    # remainder — but the shares the old order took are real, and the new
    # order's own counters know nothing about them. Chasing further from here
    # is how a partial becomes a double position, so: cancel the replacement
    # immediately and carry the ancestor's fill into entry protection so the
    # stop covers it.
    try:
        ancestor = pipeline.broker.get_order_fill_info(order_id) or {}
        ancestor_filled = float(ancestor.get("filled_qty") or 0)
    except Exception:  # noqa: BLE001
        ancestor_filled = 0.0
    if ancestor_filled > 0:
        logger.warning(
            "re-peg %s: superseded order %s filled %.4f share(s) in the "
            "replace window — cancelling replacement %s rather than risk "
            "buying the same idea twice; the stop will cover the %.4f "
            "already acquired", symbol, order_id, ancestor_filled,
            new_id, ancestor_filled,
        )
        pipeline.broker.cancel_entry_order(str(new_id))
        _record_pipeline_event(
            pipeline, ctx, symbol, "repeg", "raced_partial_fill",
            "repeg_ancestor_filled", broker_order_id=str(new_id),
            replaces_order_id=order_id, fill_qty=ancestor_filled,
            attempt=attempt,
        )
        return str(new_id), ancestor_filled, False

    return str(new_id), 0.0, True


def _repoint_trade(pipeline, trade_row_id, old_order_id: str,
                   new_order_id: str, symbol) -> bool:
    """Point the trades row at the replacement id. True when it stuck."""
    if not trade_row_id:
        logger.error(
            "re-peg %s: no trades row id for order %s — cannot repoint to "
            "%s; fill reconciliation would follow a dead order",
            symbol, old_order_id, new_order_id,
        )
        return False
    try:
        rows = pipeline.db.repoint_trade_broker_order_id(
            trade_row_id, old_order_id=old_order_id, new_order_id=new_order_id,
        )
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "re-peg %s: repointing trades row %s from %s to %s FAILED: %s — "
            "the WAL row is left for the session-start drain",
            symbol, trade_row_id, old_order_id, new_order_id, exc,
        )
        return False
    if not rows:
        logger.warning(
            "re-peg %s: trades row %s no longer pointed at %s — leaving the "
            "WAL row for the drain to adjudicate",
            symbol, trade_row_id, old_order_id,
        )
        return False
    return True


def _delete_repeg_wal(pipeline, wal_row_id) -> None:
    if not wal_row_id:
        return
    try:
        pipeline.db.delete_pending_repeg(wal_row_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("re-peg: could not clear WAL row %s: %s", wal_row_id, exc)


def _persist_evidence(db: "Database", *, run_id: str, agent_name: str, kind: str,
                       scope: str, evidence_json: str, symbol: str | None = None,
                       decision_id: str | None = None) -> None:
    """Best-effort Stage 4 structured-evidence write — NEVER raises.

    Wraps `Database.insert_specialist_evidence` so every call site below can
    call this unconditionally without its own try/except. A failure here
    (disk full, lock contention, whatever) is a forensic-display gap, not a
    reason to mark research/decision data degraded or interrupt the
    pipeline — see docs/architecture/MISSION_CONTROL_API.md and
    .claude/rules/trading-core.md's "Logging/forensic persistence failure
    must never relax a deterministic block" rule.
    """
    try:
        db.insert_specialist_evidence(
            run_id=run_id, agent_name=agent_name, kind=kind, scope=scope,
            evidence_json=evidence_json, symbol=symbol, decision_id=decision_id,
        )
    except Exception as e:
        logger.warning(
            "Failed to persist Stage 4 specialist evidence (agent=%s kind=%s "
            "scope=%s symbol=%s): %s", agent_name, kind, scope, symbol, e,
        )


def _record_execution_skip(pipeline, ctx, symbol: str, reason: str,
                           detail: str) -> None:
    """Durable record of a deterministic BUY skip in the execution phase.

    Every skip path in the BUY loop used to be a log-only `continue`: the
    DB, funnel, Mission Control and the evening reflection all read a
    session whose approved BUYs were dropped here as a deliberate no-trade
    (2026-08-19: three risk-approved BUYs skipped as unfunded; the evening
    analyst concluded the system needed "proactive idea generation").
    Appends to ctx.execution_skips (drives the run's final status) and
    persists an `execution_skip` evidence row (drives the funnel/journal).
    Best-effort by construction — persistence failure never affects the
    skip decision itself (trading-core rule).
    """
    ctx.execution_skips.append(
        {"symbol": symbol, "reason": reason, "detail": detail},
    )
    import json as _json
    _persist_evidence(
        pipeline.db, run_id=ctx.run_id, agent_name="execution",
        kind="execution_skip", scope="symbol", symbol=symbol,
        decision_id=ctx.decision_id,
        evidence_json=_json.dumps(
            {"symbol": symbol, "reason": reason, "detail": detail},
        ),
    )


def _record_pipeline_event(pipeline, ctx, symbol: str | None, stage: str,
                           outcome: str, reason: str = "", **details) -> None:
    """Append one typed lifecycle fact to the existing evidence stream."""
    import json as _json
    payload = {"stage": stage, "outcome": outcome, "reason": reason, **details}
    _persist_evidence(
        pipeline.db, run_id=ctx.run_id, agent_name="pipeline",
        kind="pipeline_event", scope="symbol" if symbol else "run",
        symbol=symbol, decision_id=ctx.decision_id,
        evidence_json=_json.dumps(payload, sort_keys=True),
    )


def _collect_seat_nominations(
    news_intel, macro_analysis, earnings_results,
) -> dict[str, list[Nomination]]:
    """Gather each seat's raw (not yet capped/deduped) nominations this run.

    Phase 9 §9.1. News and Macro nominations come straight off the live
    Pydantic report each seat produces once per morning session. Earnings
    is different: `EarningsAnalystAgent` runs one LLM call PER NEW FILING
    (`analyze_reports` / `_analyze_one`), so a session that reads several
    filings makes several `EarningsAnalysis` objects, not one. Its
    nominations are therefore the union across every filing analyzed this
    run, re-validated from the stored dict shape
    (`earnings_results[i]["analysis"]`, already `validated_model.model_dump()`
    — see `EarningsAnalystAgent._analyze_new`/`_load_analysis`) via
    `Nomination.model_validate` rather than trusted as already-typed.

    Always returns all three seat keys, even when a seat produced nothing
    this run, so `select_nominations` never has to special-case a missing
    seat.
    """
    seats: dict[str, list[Nomination]] = {
        "news_analyst": [], "macro_analyst": [], "earnings_analyst": [],
    }
    if news_intel is not None:
        seats["news_analyst"] = list(getattr(news_intel, "nominations", None) or [])
    # macro_analysis can be a plain carried-forward dict in other stages
    # (see _macro_analysis_as_dict), but never inside MorningResearchStage
    # — it is always either a fresh MacroAnalysis or None here. Guard
    # anyway so a future caller passing the carried-forward shape degrades
    # to "no macro nominations" instead of an AttributeError.
    if macro_analysis is not None and not isinstance(macro_analysis, dict):
        seats["macro_analyst"] = list(getattr(macro_analysis, "nominations", None) or [])
    for item in earnings_results or []:
        analysis = item.get("analysis") if isinstance(item, dict) else None
        if not analysis:
            continue
        for raw in analysis.get("nominations") or []:
            try:
                seats["earnings_analyst"].append(Nomination.model_validate(raw))
            except Exception as e:
                logger.warning("Dropping malformed earnings nomination: %s", e)
    return seats


def _macro_analysis_as_dict(macro_analysis) -> dict | None:
    """Dual-shape read: macro_analysis may be a Pydantic MacroAnalysis (a
    fresh macro run this tick) OR a plain dict carried forward from
    macro_store.load_last_state() (Pipeline._carry_forward_macro — no macro
    run today, yesterday's persisted snapshot is reused). The persisted
    snapshot is a deliberately-trimmed subset (see macro_store.save_last_state)
    and must never be coerced back into a MacroAnalysis model — it lacks
    reasoning_chain and stores sector_guidance pre-normalized as a dict, not
    the model's list[MacroSectorGuidance].

    portfolio_manager.decide() already accepts a plain dict for
    macro_analysis, so both shapes resolve to "pass a dict straight through".
    """
    if macro_analysis is None:
        return None
    if isinstance(macro_analysis, dict):
        return macro_analysis
    return macro_analysis.model_dump()


def _macro_target_invested_pct(macro_analysis) -> float | None:
    """Dual-shape read of position_guidance.target_invested_pct.

    Same carried-forward-dict vs. fresh-model split as
    `_macro_analysis_as_dict` (see there for why the dict shape exists).
    Degrades to None — the existing "not provided" path — when the key or
    attribute is missing, since a carried snapshot may legitimately lack it.
    """
    if not macro_analysis:
        return None
    if hasattr(macro_analysis, "position_guidance"):
        guidance = macro_analysis.position_guidance
        return getattr(guidance, "target_invested_pct", None) if guidance else None
    guidance = macro_analysis.get("position_guidance")
    return guidance.get("target_invested_pct") if isinstance(guidance, dict) else None


def _apply_scale_all_buys(decisions, verdict) -> tuple[list, float]:
    """Apply RiskVerdict.scale_all_buys to BUY (and Stage-3 SHORT) decisions.

    `scale_all_buys` is documented in config/prompts/risk_manager.md as
    a portfolio-level sizing knob with a ge=0.0 le=1.0 range — 0.0 is
    an explicit "kill all BUYs" veto. The pre-fix code did
    ``getattr(...) or 1.0`` which silently collapsed 0.0 to 1.0 because
    0.0 is falsy in Python, disabling the veto. Treat None/missing as
    1.0 (no scaling), but pass 0.0 through so the scaling branch zeros
    every BUY allocation.

    SHORT scales alongside BUY: both open new risk, and RM's portfolio-
    level "cut everything new" knob should not have a blind spot for one
    of the two ways to open it. SELL, COVER and HOLD are untouched.

    Returns ``(scaled_decisions, scale)`` so the caller can use the
    coerced scale for follow-up filters (re-running hard risk if the
    scale dropped allocations into different buckets).
    """
    scale_raw = getattr(verdict, "scale_all_buys", 1.0)
    scale = 1.0 if scale_raw is None else float(scale_raw)
    if scale >= 1.0 or scale < 0.0:
        return list(decisions), scale

    scaled: list = []
    for d in decisions:
        if d.action in ("BUY", "SHORT"):
            new_alloc = max(0.0, min(100.0, d.allocation_pct * scale))
            if new_alloc <= 0:
                logger.info(
                    "scale_all_buys=%.2f drops %s (alloc 0 after scaling)",
                    scale, d.symbol,
                )
                continue
            try:
                scaled.append(d.model_copy(update={"allocation_pct": new_alloc}))
                logger.info(
                    "scale_all_buys=%.2f: %s %.2f%% → %.2f%%",
                    scale, d.symbol, d.allocation_pct, new_alloc,
                )
            except Exception as e:
                logger.warning(
                    "scale_all_buys copy failed for %s: %s — keeping original",
                    d.symbol, e,
                )
                scaled.append(d)
        else:
            scaled.append(d)
    return scaled, scale


class MorningResearchStage:
    """Parallel data + LLM fan-out at morning open.

    Produces on ctx:
      macro_summary, macro_analysis, news_intel, analyses, earnings_results,
      symbols_bars, valuations, data_status

    Uses a ThreadPoolExecutor for the five parallel research branches.
    Failures are isolated so one bad branch doesn't abort the rest.
    """

    def __init__(
        self,
        *,
        config: "AppConfig",
        db: "Database",
        market: "MarketDataProvider",
        macro: "MacroDataProvider",
        news_provider: "NewsDataProvider",
        news_store: "NewsStore",
        macro_store: "MacroStore",
        tech_store: "TechStore",
        earnings_provider: "EarningsDataProvider",
        macro_analyst: "MacroAnalystAgent",
        news_analyst: "NewsAnalystAgent",
        tech_analyst: "TechAnalystAgent",
        earnings_analyst: "EarningsAnalystAgent",
        has_actionable_signal_fn,
        run_news_update_fn,
        load_earnings_analyses_fn,
        smart_money_provider: "SmartMoneySource | None" = None,
        smart_money_analyst: "SmartMoneyAnalystAgent | None" = None,
        admit_smart_money_candidates_fn=None,
        admit_nominated_candidates_fn=None,
    ):
        self.config = config
        self.db = db
        self.market = market
        self.macro = macro
        self.news_provider = news_provider
        self.news_store = news_store
        self.macro_store = macro_store
        self.tech_store = tech_store
        self.earnings_provider = earnings_provider
        self.macro_analyst = macro_analyst
        self.news_analyst = news_analyst
        self.tech_analyst = tech_analyst
        self.earnings_analyst = earnings_analyst
        self.smart_money_provider = smart_money_provider
        self.smart_money_analyst = smart_money_analyst
        self._admit_smart_money_candidates = admit_smart_money_candidates_fn
        # Phase 9 — same shape as admit_smart_money_candidates_fn:
        # (list[str] symbols) -> (admitted: set[str], details: dict[str,dict]).
        # Shares the same deterministic gate under the hood
        # (TradingPipeline._evaluate_external_admission_gates); this is a
        # SEPARATE injected callable (not reused directly) because the two
        # callers decide WHICH symbols are worth gating differently — one
        # groups/ranks SEC Form 4 rows, the other consumes an
        # already-capped nomination candidate list.
        self._admit_nominated_candidates = admit_nominated_candidates_fn
        # Injected callables so we don't duplicate pre-filter / news / earnings
        # orchestration logic. Those still live on TradingPipeline for now
        # because they touch shared state we haven't finished extracting.
        self._has_actionable_signal = has_actionable_signal_fn
        self._run_news_update = run_news_update_fn
        self._load_earnings_analyses = load_earnings_analyses_fn

    def run(self, ctx: RunContext) -> RunContext:
        logger.info("=== Stage: MorningResearch ===")
        data_status: dict[str, str] = {}
        try:
            prior_macro_state = self.macro_store.load_last_state() or {}
        except Exception as e:
            logger.warning("Failed to load prior macro state: %s", e)
            prior_macro_state = {}
        try:
            news_narrative = self.news_store.load_macro_narrative()
        except Exception as e:
            logger.warning("Failed to load macro news narrative: %s", e)
            news_narrative = None

        smart_config = getattr(self.config, "smart_money", None)
        smart_money_observations = []
        smart_money_provider_error = None
        if smart_config and smart_config.enabled and self.smart_money_provider:
            try:
                smart_money_observations, smart_money_provider_error = (
                    self.smart_money_provider.fetch(self.config.trading.universe)
                )
            except Exception as exc:
                logger.warning("Smart-money cache read failed: %s", exc)
                smart_money_provider_error = f"provider_error:{type(exc).__name__}"
        ctx.smart_money_observations = smart_money_observations
        if smart_money_observations and self._admit_smart_money_candidates:
            try:
                admitted, admissions = self._admit_smart_money_candidates(
                    smart_money_observations,
                )
                ctx.admitted_symbols = {
                    str(symbol).strip().upper() for symbol in admitted if str(symbol).strip()
                }
                ctx.smart_money_admissions = dict(admissions or {})
            except Exception as exc:
                # Admission uncertainty fails closed; the observations can
                # still be rendered as research evidence.
                logger.warning("Smart-money transient admission failed closed: %s", exc)
                ctx.admitted_symbols = set()
                ctx.smart_money_admissions = {}
        configured_symbols = [
            str(symbol).strip().upper()
            for symbol in self.config.trading.universe if str(symbol).strip()
        ]
        # Fresh run-scoped SEC admissions are the reason this session has an
        # expanded research surface.  Put them first so a large configured
        # universe cannot strand the transient opportunity in the final Tech
        # chunk after earlier chunks consume the bounded recovery budget.
        effective_symbols = list(dict.fromkeys(
            sorted(ctx.admitted_symbols) + configured_symbols
        ))

        for observation in smart_money_observations:
            symbol = str(getattr(observation, "symbol", "") or "").strip().upper()
            for field_name, field_value in (
                (
                    "in_trading_universe",
                    symbol in set(configured_symbols) or symbol in ctx.admitted_symbols,
                ),
                ("transient_admitted", symbol in ctx.admitted_symbols),
            ):
                try:
                    setattr(observation, field_name, field_value)
                except Exception:
                    pass
        for symbol, admission in ctx.smart_money_admissions.items():
            import json as _json
            _persist_evidence(
                self.db, run_id=ctx.run_id, agent_name="smart_money_analyst",
                kind="admission", scope="symbol", symbol=symbol,
                evidence_json=_json.dumps(admission, sort_keys=True),
            )
            # The deterministic admission record has its own ``reason``
            # field (for example ``material_sec_form4_purchase``).  Keep it
            # as admission detail instead of splatting it over the pipeline
            # event's positional ``reason`` argument.  Passing both used to
            # raise before any research agent ran, aborting a natural morning
            # session precisely when an external candidate qualified.
            admission_details = dict(admission)
            admission_reason = admission_details.pop("reason", None)
            _record_pipeline_event(
                self, ctx, symbol, "opportunity", "admitted",
                "smart_money_form4_admission",
                admission_reason=admission_reason,
                **admission_details,
            )

        def _run_macro():
            macro_summary = self.macro.get_macro_summary()
            logger.info(
                "Macro data: VIX=%s, HY OAS=%sbps, CPI core YoY=%s, UNRATE=%s",
                macro_summary.get("vix", {}).get("current"),
                macro_summary.get("credit_spread", {}).get("current_bps"),
                macro_summary.get("inflation", {}).get("core_cpi_yoy"),
                macro_summary.get("unemployment", {}).get("current"),
            )
            analysis, result = self.macro_analyst.analyze(
                macro_summary=macro_summary,
                universe=effective_symbols,
                last_state=prior_macro_state,
                news_narrative=news_narrative,
            )
            if analysis:
                try:
                    self.macro_store.save_last_state(analysis.model_dump())
                except Exception as e:
                    logger.warning("Failed to persist macro last state: %s", e)
            return macro_summary, analysis, result

        def _run_news():
            try:
                return self._run_news_update(
                    ctx.run_id, session="morning", universe=effective_symbols,
                )
            except TypeError as exc:
                if "unexpected keyword argument 'universe'" not in str(exc):
                    raise
                return self._run_news_update(ctx.run_id, session="morning")

        def _run_tech():
            all_symbols_data = []
            symbols_bars: dict[str, list] = {}
            for symbol in effective_symbols:
                bars = self.market.get_ohlcv(symbol, self.config.trading.lookback_days)
                if not bars:
                    logger.warning("No data for %s, skipping", symbol)
                    continue
                indicators = compute_indicators(symbol, bars)
                all_symbols_data.append({"symbol": symbol, "bars": bars, "indicators": indicators})
                symbols_bars[symbol] = bars
            ctx.symbols_bars = symbols_bars
            symbols_data = [
                s for s in all_symbols_data
                if (
                    s["symbol"] in ctx.admitted_symbols
                    or self._has_actionable_signal(
                        s["indicators"], s["symbol"], s["bars"], ctx.positions,
                    )
                )
            ]
            logger.info(
                "Tech pre-filter: %d/%d symbols have actionable signals",
                len(symbols_data), len(all_symbols_data),
            )
            for candidate in symbols_data:
                _record_pipeline_event(
                    self, ctx, candidate["symbol"], "opportunity",
                    "discovered", "actionable_technical_prefilter",
                )
            if not symbols_data:
                return {}, None
            prior_ratings = self.tech_store.load()
            valuations: dict[str, dict] = {}
            for s in symbols_data:
                sym = s.get("symbol")
                if sym:
                    try:
                        valuations[sym] = self.market.get_valuation_metrics(sym)
                    except Exception as e:
                        logger.warning("valuation fetch crashed for %s: %s", sym, e)
            ctx.valuations = valuations
            # analyses_map is guaranteed to carry every symbol in
            # symbols_data as a key (2026-08-19 Tech batch-response
            # symbol-loss fix) — a TechAnalysisResult on success, or None
            # for a symbol that failed to resolve even after tech_analyst's
            # own bounded retry. Filter before touching real analyses;
            # `analyses_map` itself (None values intact) is still returned
            # so the caller can see and report the failed count instead of
            # it silently vanishing.
            analyses_map, ta_res = self.tech_analyst.analyze_batch(
                symbols_data,
                prior_ratings=prior_ratings,
                valuations=valuations,
                prior_macro_regime=prior_macro_state.get("regime"),
                prior_macro_outlook=prior_macro_state.get("equity_outlook"),
            )
            resolved = [a for a in analyses_map.values() if a is not None]
            if resolved:
                try:
                    self.tech_store.update(resolved)
                except Exception as e:
                    logger.warning("TechStore.update failed: %s", e)
                ages = self.tech_store.compute_ages([a.symbol for a in resolved])
                for analysis in resolved:
                    if analysis.symbol in ages:
                        analysis.signal_age_days = ages[analysis.symbol]
            return analyses_map, ta_res

        def _load_earnings():
            try:
                return self._load_earnings_analyses(
                    ctx.run_id, session="morning", ctx=ctx, universe=effective_symbols,
                )
            except TypeError as exc:
                if "unexpected keyword argument 'universe'" not in str(exc):
                    raise
                return self._load_earnings_analyses(
                    ctx.run_id, session="morning", ctx=ctx,
                )

        def _run_smart_money():
            if not smart_config or not smart_config.enabled or not self.smart_money_provider or not self.smart_money_analyst:
                return [], None, smart_money_provider_error, None
            if not smart_money_observations:
                return [], None, smart_money_provider_error, None
            findings, result, analysis_error = self.smart_money_analyst.analyze(
                smart_money_observations,
            )
            return findings, result, smart_money_provider_error, analysis_error

        logger.info("Starting parallel: macro + news + tech + earnings + smart money")
        with ThreadPoolExecutor(max_workers=5) as ex:
            # ContextVar values do not automatically flow into executor
            # workers. Give every branch its own copied context so breaker
            # reservations retain this run_id/mode even if another scheduler
            # job overlaps in the parent process.
            macro_future = ex.submit(copy_context().run, _run_macro)
            news_future = ex.submit(copy_context().run, _run_news)
            tech_future = ex.submit(copy_context().run, _run_tech)
            earnings_future = ex.submit(copy_context().run, _load_earnings)
            smart_money_future = ex.submit(copy_context().run, _run_smart_money)

        try:
            findings, sm_result, provider_error, analysis_error = smart_money_future.result()
            ctx.smart_money_findings = findings
            ctx.smart_money_provider_error = provider_error or analysis_error
            import json as _sm_json
            _persist_evidence(
                self.db, run_id=ctx.run_id, agent_name="smart_money_analyst",
                kind="scan_summary", scope="run",
                evidence_json=_sm_json.dumps({
                    "source": "SEC Form 4",
                    "observations": len(smart_money_observations),
                    "findings": len(findings),
                    "temporary_admissions": sorted(ctx.admitted_symbols),
                    "state": (
                        "degraded" if provider_error or analysis_error else
                        "material" if findings else "quiet"
                    ),
                }, sort_keys=True),
            )
            if provider_error:
                data_status["smart_money"] = "degraded" if findings else "provider_error"
                _persist_evidence(
                    self.db, run_id=ctx.run_id, agent_name="smart_money_analyst",
                    kind="provider_error", scope="run",
                    evidence_json=__import__("json").dumps({"error": provider_error}),
                )
            if sm_result is not None:
                sm_log_kwargs = agent_log_kwargs(sm_result)
                if analysis_error:
                    sm_log_kwargs["status"] = "agent_failure"
                self.db.insert_agent_log(
                    agent_name="smart_money_analyst", run_id=ctx.run_id,
                    input_summary=f"{len(findings)} material findings",
                    input_message=sm_result.user_message,
                    output_summary=(
                        f"agent_failure:{analysis_error}" if analysis_error else
                        (", ".join(f"{f.symbol}:{f.stance}" for f in findings) or "no material findings")
                    ),
                    full_response=sm_result.raw_text, model=sm_result.model,
                    tokens_used=sm_result.tokens_used, input_tokens=sm_result.input_tokens,
                    output_tokens=sm_result.output_tokens, cost_usd=sm_result.cost_usd,
                    **sm_log_kwargs,
                )
                if analysis_error:
                    _persist_evidence(
                        self.db, run_id=ctx.run_id, agent_name="smart_money_analyst",
                        kind="agent_failure", scope="run",
                        evidence_json=__import__("json").dumps({"error": analysis_error}),
                    )
                    data_status["smart_money"] = "degraded"
                elif not provider_error:
                    data_status["smart_money"] = "ok"
            for finding in findings:
                symbol = finding.symbol.upper()
                is_candidate = (
                    symbol in set(configured_symbols)
                    or symbol in ctx.admitted_symbols
                    or any(
                        str(getattr(position, "symbol", "") or "").upper() == symbol
                        for position in ctx.positions
                    )
                )
                _persist_evidence(
                    self.db, run_id=ctx.run_id, agent_name="smart_money_analyst",
                    kind="finding", scope="symbol" if is_candidate else "research",
                    symbol=symbol, evidence_json=finding.model_dump_json(),
                )
            if sm_result is None and not provider_error:
                data_status["smart_money"] = "ok" if findings else "empty"
        except Exception as e:
            logger.warning("Smart-money branch failed: %s", e)
            ctx.smart_money_provider_error = f"analysis_error:{type(e).__name__}"
            data_status["smart_money"] = "provider_error"

        # Macro
        try:
            macro_summary, macro_analysis, ma_result = macro_future.result()
            # audit round 2: commit the analysis to ctx BEFORE the agent_logs
            # write — a DB lock/timeout on the log write used to discard a
            # fully successful macro run (ctx fields were assigned after it).
            ctx.macro_summary = macro_summary
            ctx.macro_analysis = macro_analysis
            self.db.insert_agent_log(
                agent_name="macro_analyst", run_id=ctx.run_id,
                input_summary=f"VIX={macro_summary.get('vix', {}).get('current')}",
                input_message=ma_result.user_message,
                output_summary=(
                    f"regime={macro_analysis.regime}, outlook={macro_analysis.equity_outlook}"
                    if macro_analysis else "parse_error"
                ),
                full_response=ma_result.raw_text,
                model=ma_result.model,
                tokens_used=ma_result.tokens_used,
                input_tokens=ma_result.input_tokens,
                output_tokens=ma_result.output_tokens,
                cost_usd=ma_result.cost_usd,
                **agent_log_kwargs(ma_result),
            )
            ctx.macro_summary = macro_summary
            ctx.macro_analysis = macro_analysis
            if macro_analysis:
                logger.info(
                    "Macro analysis: regime=%s, outlook=%s, target_invested=%s%%",
                    macro_analysis.regime, macro_analysis.equity_outlook,
                    macro_analysis.position_guidance.target_invested_pct,
                )
                data_status["macro"] = "ok"
                _persist_evidence(
                    self.db, run_id=ctx.run_id, agent_name="macro_analyst",
                    kind="analysis", scope="run",
                    evidence_json=macro_analysis.model_dump_json(),
                )
            else:
                data_status["macro"] = "parse_error"
        except PaidAnalysisSuspended:
            raise
        except Exception as e:
            logger.error("Macro analyst failed: %s. Continuing without macro.", e)
            data_status["macro"] = "failed"

        # News
        #
        # 2026-08-28 fix: before this, `data_status["news"]` was "ok" purely
        # on whether the LLM call parsed — a run where Reuters 404'd and AP
        # 403'd still said "ok" as long as the analyst produced valid JSON
        # from whatever the surviving feeds returned (or from nothing at
        # all). `news_coverage` (src.data.news.NewsCoverage) is the
        # deterministic half of the fix: it reflects how many of the
        # configured wire feeds actually returned data, independent of
        # whether the LLM call on top of them succeeded. Coverage failure
        # dominates parse success below — a cleanly parsed report built on
        # zero real headlines is not "ok" by any honest reading of the word.
        news_intel: NewsIntelligenceReport | None = None
        news_coverage: "NewsCoverage | None" = None
        try:
            news_intel, news_coverage = news_future.result()
            if news_coverage is not None:
                _persist_evidence(
                    self.db, run_id=ctx.run_id, agent_name="news_provider",
                    kind="coverage", scope="run",
                    evidence_json=__import__("json").dumps({
                        "configured": news_coverage.configured,
                        "succeeded": news_coverage.succeeded,
                        "failed": [
                            {"name": f.name, "reason": f.reason}
                            for f in news_coverage.failed
                        ],
                        "status": news_coverage.status,
                    }, sort_keys=True),
                )
            if news_intel:
                logger.info("News briefing: %s", news_intel.pm_briefing[:200])
                _persist_evidence(
                    self.db, run_id=ctx.run_id, agent_name="news_analyst",
                    kind="analysis", scope="run",
                    evidence_json=news_intel.model_dump_json(),
                )
            # Coverage is authoritative over parse success: total feed
            # failure means "failed" even if the model still emitted a
            # technically-valid report on empty input. A coverage-less
            # result (news_coverage is None — a caller/test that hasn't
            # been updated to report it) falls back to the pre-fix
            # ok/parse_error split rather than crashing on a missing value.
            if news_coverage is None:
                data_status["news"] = "ok" if news_intel else "parse_error"
            elif news_coverage.status == "failed":
                data_status["news"] = "failed"
                logger.error(
                    "News coverage FAILED this run: %s", news_coverage.describe(),
                )
            elif not news_intel:
                data_status["news"] = "parse_error"
            elif news_coverage.status == "partial":
                data_status["news"] = "partial"
                logger.warning(
                    "News coverage PARTIAL this run: %s", news_coverage.describe(),
                )
            else:
                data_status["news"] = "ok"
        except PaidAnalysisSuspended:
            raise
        except Exception as e:
            logger.error("News analyst failed: %s. Continuing without news.", e)
            data_status["news"] = "failed"
        ctx.news_intel = news_intel
        ctx.news_coverage = news_coverage

        # Tech
        analyses: list[TechAnalysisResult] = []
        try:
            analyses_map, ta_result = tech_future.result()
            # analyses_map carries every pre-filtered symbol as a key
            # (2026-08-19 Tech batch-response symbol-loss fix); None marks
            # a symbol tech_analyst could not resolve even after its own
            # bounded retry. Filter before building the real analyses
            # list, and surface the failed count explicitly rather than
            # letting it disappear into a plain "ok".
            analyses = [a for a in analyses_map.values() if a is not None]
            failed_count = len(analyses_map) - len(analyses)
            if not analyses_map:
                data_status["tech"] = "empty"
            elif failed_count == 0:
                data_status["tech"] = "ok"
            elif analyses:
                data_status["tech"] = "partial"
                logger.warning(
                    "Tech batch partial: %d/%d symbols resolved, %d failed "
                    "even after retry — proceeding with the resolved subset",
                    len(analyses), len(analyses_map), failed_count,
                )
            else:
                data_status["tech"] = "failed"
                logger.error(
                    "Tech batch: all %d submitted symbol(s) failed even after retry",
                    len(analyses_map),
                )
            if ta_result:
                self.db.insert_agent_log(
                    agent_name="tech_analyst", run_id=ctx.run_id,
                    input_summary=(
                        f"Batch: {len(analyses)}/{len(analyses_map)} symbols "
                        f"analyzed" + (f", {failed_count} failed" if failed_count else "")
                    ),
                    input_message=ta_result.user_message,
                    output_summary=", ".join(f"{a.symbol}:{a.rating}" for a in analyses),
                    full_response=ta_result.raw_text,
                    model=ta_result.model,
                    tokens_used=ta_result.tokens_used,
                    input_tokens=ta_result.input_tokens,
                    output_tokens=ta_result.output_tokens,
                    cost_usd=ta_result.cost_usd,
                    **agent_log_kwargs(ta_result),
                )
                for analysis in analyses:
                    _persist_evidence(
                        self.db, run_id=ctx.run_id, agent_name="tech_analyst",
                        kind="analysis", scope="symbol", symbol=analysis.symbol,
                        evidence_json=analysis.model_dump_json(),
                    )
                    _record_pipeline_event(
                        self, ctx, analysis.symbol, "specialist", "evaluated",
                        "technical_analysis_validated",
                        specialist="tech_analyst", rating=analysis.rating,
                    )
                for symbol, analysis in analyses_map.items():
                    if analysis is None:
                        _record_pipeline_event(
                            self, ctx, symbol, "specialist", "failed",
                            "technical_analysis_unresolved_after_retry",
                            specialist="tech_analyst",
                        )
            logger.info("Technical analysis complete: %d symbols in 1 LLM call", len(analyses))
        except PaidAnalysisSuspended:
            raise
        except Exception as e:
            logger.error("Tech analyst failed: %s. Continuing without technical data.", e)
            data_status["tech"] = "failed"
        ctx.analyses = analyses

        # Earnings
        earnings_results = []
        try:
            _, earnings_results = earnings_future.result()
            data_status["earnings"] = "ok"
            import json as _json
            for item in earnings_results:
                analysis = item.get("analysis") if isinstance(item, dict) else None
                symbol = item.get("symbol") if isinstance(item, dict) else None
                if analysis and symbol:
                    # `analysis` is already validated_model.model_dump() —
                    # see EarningsAnalystAgent._analyze_new/_load_analysis —
                    # never re-derived from raw filing text here.
                    _persist_evidence(
                        self.db, run_id=ctx.run_id, agent_name="earnings_analyst",
                        kind="analysis", scope="symbol", symbol=symbol,
                        evidence_json=_json.dumps(analysis),
                    )
        except PaidAnalysisSuspended:
            raise
        except Exception as e:
            logger.error("Earnings check failed: %s. Continuing without earnings.", e)
            data_status["earnings"] = "failed"
        ctx.earnings_results = earnings_results

        # Phase 9 (§9.1/§9.2) — the nomination responder pass. Deliberately
        # sequenced HERE, after every parallel-wave future has been
        # resolved and ctx.news_intel / ctx.macro_analysis /
        # ctx.earnings_results / ctx.analyses are all populated: News,
        # Earnings and Macro run CONCURRENTLY with Technical (the
        # ThreadPoolExecutor above), so a nomination they produce cannot
        # be known before Technical's first batch call starts. This is a
        # deliberate second, on-demand Technical call for just the
        # nominated symbols — NOT a second parallel wave; everything above
        # this line is unchanged from before Phase 9.
        self._run_nomination_responder_pass(ctx, prior_macro_state)

        ctx.data_status = data_status
        # Single grep-able summary line. Each agent's failure already logs
        # at ERROR individually, but a downstream operator scanning the
        # journal for "why did morning trade zero today?" wants one row
        # listing all degraded inputs side-by-side. The 2+ failure
        # advisory in RiskStage handles the runtime defensive response;
        # this log handles the postmortem readability.
        degraded = [k for k, v in data_status.items() if v not in ("ok", "empty")]
        if degraded:
            logger.error(
                "Morning research degraded: %s | full status=%s",
                ",".join(sorted(degraded)), data_status,
            )
        return ctx

    def _run_nomination_responder_pass(self, ctx: RunContext, prior_macro_state: dict) -> None:
        """Phase 9 (§9.1/§9.2) — Technical as RESPONDER, not gatekeeper.

        Collects every seat's nominations, applies the per-seat cap, dedupes
        across seats (a symbol nominated by two seats records both), applies
        the global cap, gates any out-of-universe survivor through the same
        deterministic admission gate the smart-money lane uses, then runs a
        SECOND on-demand Technical call for whatever is left that the first
        batch didn't already cover. Results are merged directly into
        `ctx.analyses` — the exact list `validate_grounding`'s hard gate
        reads — so a responded nomination is indistinguishable from an
        organically-prefiltered symbol by the time PM sees it.

        No nominations -> no second call, full stop: every early-return path
        below exits before `self.tech_analyst.analyze_batch` is ever called.
        """
        import json as _json

        nominations_by_seat = _collect_seat_nominations(
            ctx.news_intel, ctx.macro_analysis, ctx.earnings_results,
        )
        total_raw = sum(len(v) for v in nominations_by_seat.values())
        for seat, noms in nominations_by_seat.items():
            for nomination in noms:
                _record_pipeline_event(
                    self, ctx, nomination.symbol, "opportunity", "nominated",
                    "research_seat_nomination", seat=seat,
                    conviction=nomination.conviction,
                    observation=nomination.observation,
                )

        nom_cfg = getattr(self.config, "nominations", None)
        max_per_seat = getattr(nom_cfg, "max_per_seat_per_run", 3) if nom_cfg else 3
        max_total = getattr(nom_cfg, "max_total_per_run", 6) if nom_cfg else 6
        candidates = select_nominations(
            nominations_by_seat, max_per_seat=max_per_seat, max_total=max_total,
        )

        if not candidates:
            logger.info(
                "Nomination responder: %d raw nomination(s), 0 candidates "
                "after caps — no second Technical call.", total_raw,
            )
            _persist_evidence(
                self.db, run_id=ctx.run_id, agent_name="pipeline",
                kind="nomination_summary", scope="run",
                evidence_json=_json.dumps({
                    "raw_nominations": total_raw,
                    "raw_by_seat": {k: len(v) for k, v in nominations_by_seat.items()},
                    "candidates_selected": 0,
                    "responder_call_made": False,
                }, sort_keys=True),
            )
            return

        configured = {
            str(s).strip().upper() for s in self.config.trading.universe if str(s).strip()
        }

        # Out-of-universe candidates must clear the SAME deterministic gate
        # the SEC Form 4 smart-money lane applies — an already-admitted or
        # in-universe symbol needs no gate at all (D3).
        to_gate = [
            c for c in candidates
            if c.symbol not in configured and c.symbol not in ctx.admitted_symbols
        ]
        newly_admitted: set[str] = set()
        admission_details: dict[str, dict] = {}
        if to_gate and self._admit_nominated_candidates:
            try:
                newly_admitted, admission_details = self._admit_nominated_candidates(
                    [c.symbol for c in to_gate],
                )
            except Exception as exc:
                # Admission uncertainty fails closed, same posture as the
                # smart-money admission try/except above.
                logger.warning("Nomination external admission failed closed: %s", exc)
                newly_admitted, admission_details = set(), {}

        eligible_candidates = []
        for c in candidates:
            if c.symbol in configured or c.symbol in ctx.admitted_symbols or c.symbol in newly_admitted:
                eligible_candidates.append(c)
            else:
                _record_pipeline_event(
                    self, ctx, c.symbol, "opportunity", "rejected",
                    "nomination_failed_external_admission_gate",
                    seats=c.seats, conviction=c.conviction,
                )

        for symbol, admission in admission_details.items():
            _persist_evidence(
                self.db, run_id=ctx.run_id, agent_name="pipeline",
                kind="admission", scope="symbol", symbol=symbol,
                evidence_json=_json.dumps(admission, sort_keys=True),
            )
            admission_reason = {k: v for k, v in admission.items() if k != "reason"}
            _record_pipeline_event(
                self, ctx, symbol, "opportunity", "admitted",
                "nomination_external_admission", **admission_reason,
            )

        # Widen run-scoped BUY eligibility the SAME way smart-money transient
        # admission does — ctx.admitted_symbols feeds allowed_buy_symbols at
        # the PM call (DecisionStage) and the symbol guard (RiskStage), both
        # of which run strictly after this stage.
        ctx.admitted_symbols = set(ctx.admitted_symbols) | newly_admitted

        already_analyzed = {a.symbol.strip().upper() for a in ctx.analyses}
        for c in eligible_candidates:
            if c.symbol in already_analyzed:
                _record_pipeline_event(
                    self, ctx, c.symbol, "opportunity", "already_covered",
                    "nomination_matched_existing_technical_analysis",
                    seats=c.seats, conviction=c.conviction,
                )
        needing_responder = [c for c in eligible_candidates if c.symbol not in already_analyzed]

        if not needing_responder:
            logger.info(
                "Nomination responder: %d raw nomination(s) -> %d candidate(s) "
                "selected, all already covered by the first Technical batch — "
                "no second call.", total_raw, len(eligible_candidates),
            )
            _persist_evidence(
                self.db, run_id=ctx.run_id, agent_name="pipeline",
                kind="nomination_summary", scope="run",
                evidence_json=_json.dumps({
                    "raw_nominations": total_raw,
                    "raw_by_seat": {k: len(v) for k, v in nominations_by_seat.items()},
                    "candidates_selected": sorted(c.symbol for c in eligible_candidates),
                    "responder_call_made": False,
                }, sort_keys=True),
            )
            return

        symbols_data: list[dict] = []
        for c in needing_responder:
            bars = self.market.get_ohlcv(c.symbol, self.config.trading.lookback_days)
            if not bars:
                logger.warning("Nomination responder: no bars for %s, skipping", c.symbol)
                continue
            indicators = compute_indicators(c.symbol, bars)
            symbols_data.append({"symbol": c.symbol, "bars": bars, "indicators": indicators})
            ctx.symbols_bars[c.symbol] = bars

        if not symbols_data:
            logger.info(
                "Nomination responder: %d candidate(s) needed a call but none "
                "had market data — no second Technical call.",
                len(needing_responder),
            )
            return

        prior_ratings = self.tech_store.load()
        valuations = dict(ctx.valuations)
        for s in symbols_data:
            sym = s["symbol"]
            try:
                valuations[sym] = self.market.get_valuation_metrics(sym)
            except Exception as e:
                logger.warning("Nomination responder valuation fetch failed for %s: %s", sym, e)
        ctx.valuations = valuations

        analyses_map, ta_result = self.tech_analyst.analyze_batch(
            symbols_data,
            prior_ratings=prior_ratings,
            valuations=valuations,
            prior_macro_regime=prior_macro_state.get("regime"),
            prior_macro_outlook=prior_macro_state.get("equity_outlook"),
        )
        resolved = [a for a in analyses_map.values() if a is not None]
        if resolved:
            try:
                self.tech_store.update(resolved)
            except Exception as e:
                logger.warning("TechStore.update failed (nomination responder): %s", e)
            ages = self.tech_store.compute_ages([a.symbol for a in resolved])
            for a in resolved:
                if a.symbol in ages:
                    a.signal_age_days = ages[a.symbol]

        existing_symbols = {a.symbol.strip().upper() for a in ctx.analyses}
        for a in resolved:
            if a.symbol.strip().upper() not in existing_symbols:
                ctx.analyses.append(a)
                existing_symbols.add(a.symbol.strip().upper())

        if ta_result:
            self.db.insert_agent_log(
                agent_name="tech_analyst", run_id=ctx.run_id,
                input_summary=(
                    f"Nomination responder batch: {len(resolved)}/{len(analyses_map)} symbols"
                ),
                input_message=ta_result.user_message,
                output_summary=", ".join(f"{a.symbol}:{a.rating}" for a in resolved),
                full_response=ta_result.raw_text,
                model=ta_result.model,
                tokens_used=ta_result.tokens_used,
                input_tokens=ta_result.input_tokens,
                output_tokens=ta_result.output_tokens,
                cost_usd=ta_result.cost_usd,
                **agent_log_kwargs(ta_result),
            )
        for a in resolved:
            _persist_evidence(
                self.db, run_id=ctx.run_id, agent_name="tech_analyst",
                kind="analysis", scope="symbol", symbol=a.symbol,
                evidence_json=a.model_dump_json(),
            )
            _record_pipeline_event(
                self, ctx, a.symbol, "specialist", "evaluated",
                "technical_analysis_validated", specialist="tech_analyst",
                rating=a.rating, origin="nomination_responder",
            )
        for symbol, a in analyses_map.items():
            if a is None:
                _record_pipeline_event(
                    self, ctx, symbol, "specialist", "failed",
                    "technical_analysis_unresolved_after_retry",
                    specialist="tech_analyst", origin="nomination_responder",
                )

        responder_cost = ta_result.cost_usd if ta_result else None
        logger.info(
            "Nomination responder: %d raw nomination(s) from %d seat(s) -> "
            "%d candidate(s) selected -> %d needed a responder Technical "
            "call (%d resolved) -> cost=%s",
            total_raw,
            len([seat for seat, noms in nominations_by_seat.items() if noms]),
            len(eligible_candidates), len(symbols_data), len(resolved),
            f"${responder_cost:.4f}" if responder_cost is not None else "unknown",
        )
        _persist_evidence(
            self.db, run_id=ctx.run_id, agent_name="pipeline",
            kind="nomination_summary", scope="run",
            evidence_json=_json.dumps({
                "raw_nominations": total_raw,
                "raw_by_seat": {k: len(v) for k, v in nominations_by_seat.items()},
                "candidates_selected": sorted(c.symbol for c in eligible_candidates),
                "responder_symbols": sorted(s["symbol"] for s in symbols_data),
                "responder_call_made": True,
                "responder_resolved": sorted(a.symbol for a in resolved),
                "responder_cost_usd": responder_cost,
            }, sort_keys=True),
        )


class DecisionStage:
    """Build PM memory layers → call PM → run Constructor.

    Reads:  ctx.positions, ctx.analyses, ctx.news_intel, ctx.earnings_results,
            ctx.macro_analysis, ctx.total_value, ctx.cash, ctx.last_equity
    Writes: ctx.portfolio_decision (with .targets AND .decisions populated),
            ctx.facts
    """

    def __init__(self, *, pipeline: "TradingPipeline"):
        self._pipeline = pipeline

    def run(self, ctx: RunContext) -> RunContext:
        from src.trading_calendar import session_date_key

        pipeline = self._pipeline
        run_id = ctx.run_id
        positions = ctx.positions
        analyses = ctx.analyses
        news_intel = ctx.news_intel
        earnings_results = ctx.earnings_results
        macro_analysis = ctx.macro_analysis
        total_value = ctx.total_value
        # PM sizes against `ctx.deployable_cash` = raw cash + convertible
        # sweep value (see `_compute_deployable_cash` for the verified
        # Alpaca field semantics: a filled SGOV sale credits `cash`
        # immediately — T+1 gates only withdrawal/transfer). The sweep
        # detail is rendered informationally via `reserve_balance`;
        # execution's raw-cash recheck after the funding sale remains the
        # final authority on what a BUY can actually spend.
        cash = ctx.deployable_cash
        last_equity = ctx.last_equity

        # isinstance guard: stage tests stub `pipeline` with MagicMock, whose
        # auto-attrs would otherwise duck-type as an enabled sweeper.
        from src.execution.cash_sweep import CashSweeper
        sweeper = getattr(pipeline, "_sweeper", None)
        sweeper = sweeper() if callable(sweeper) else None
        reserve_balance = 0.0
        if isinstance(sweeper, CashSweeper):
            positions, parked = sweeper.split_positions(positions)
            if parked is not None:
                reserve_balance = sweeper.parked_value(ctx.positions)

        yesterday_insights = pipeline.db.get_latest_insights(before_date=session_date_key())
        recent_performance = pipeline._compute_recent_performance(last_equity)
        if yesterday_insights:
            logger.info(
                "Loaded yesterday's insights (risk=%s): %s",
                yesterday_insights.get("risk_rating", "?"),
                yesterday_insights.get("tomorrow_outlook", "")[:100],
            )

        position_history = pipeline._build_position_history(positions)
        # Publish both to ctx so RiskStage audits PM against the SAME holding
        # ages and drawdown state PM sized from, instead of a second snapshot
        # taken minutes later (2026-08-13 agent audit).
        ctx.position_history = position_history
        ctx.recent_performance = recent_performance
        weekly_narrative = pipeline._build_weekly_narrative()
        macro_trajectory = pipeline._build_macro_trajectory()
        active_state_changes = pipeline._build_active_state_changes()
        rm_recent_verdicts = pipeline._build_rm_recent_verdicts()
        pm_recent_decisions = pipeline._build_pm_recent_decisions()
        projected_portfolio = pipeline._build_projected_portfolio(
            positions, analyses, total_value,
        )
        calibration_note = pipeline._build_calibration_note()
        macro_tech_alignment = pipeline._build_macro_tech_alignment(macro_analysis, analyses)
        # Phase-1 evening-upgrade feedback: surface recurring missed themes
        # (L3d) and repeat loss patterns (L3f) that evening classified over
        # the last 14 days. Empty strings when no recurring pattern found.
        recent_missed_lessons = pipeline._build_recent_missed_lessons()
        recent_loss_pits = pipeline._build_recent_loss_pits()
        # Audit §1.2 — build the correlation matrix HERE, before PM decides,
        # rather than in RiskStage after it already has. RiskStage reuses the
        # memoized matrix, so the deterministic cluster check still judges PM
        # against exactly the numbers PM was shown.
        correlation_matrix = pipeline._ensure_correlation_matrix(ctx, positions)
        pm_facts = pipeline._build_pm_facts(
            positions=positions, analyses=analyses,
            total_value=total_value, cash=cash,
            recent_performance=recent_performance,
            macro_analysis=macro_analysis,
            correlation_matrix=correlation_matrix,
        )
        ctx.facts = pm_facts
        trading_config = getattr(pipeline.config, "trading", None)
        configured_universe = getattr(trading_config, "universe", []) or []

        portfolio_decision, pm_result = pipeline.portfolio_manager.decide(
            analyses=analyses,
            positions=positions,
            macro_analysis=_macro_analysis_as_dict(macro_analysis),
            cash_balance=cash,
            reserve_balance=reserve_balance,
            total_value=total_value,
            news_intel=news_intel,
            earnings_analyses=earnings_results,
            smart_money_findings=ctx.smart_money_findings,
            yesterday_insights=yesterday_insights,
            recent_performance=recent_performance,
            position_history=position_history,
            weekly_narrative=weekly_narrative,
            macro_trajectory=macro_trajectory,
            active_state_changes=active_state_changes,
            rm_recent_verdicts=rm_recent_verdicts,
            pm_recent_decisions=pm_recent_decisions,
            projected_portfolio=projected_portfolio,
            calibration_note=calibration_note,
            macro_tech_alignment=macro_tech_alignment,
            recent_missed_lessons=recent_missed_lessons,
            recent_loss_pits=recent_loss_pits,
            facts=pm_facts,
            allow_margin=bool(getattr(pipeline.config.risk, "allow_margin", False)),
            symbol_sectors=dict(getattr(pipeline, "_last_symbol_sectors", {})),
            session_type=ctx.session,
            allowed_buy_symbols={
                str(symbol).strip().upper()
                for symbol in configured_universe
                if str(symbol).strip()
            } | set(ctx.admitted_symbols),
            transient_admitted_symbols=set(ctx.admitted_symbols),
        )

        if portfolio_decision and portfolio_decision.reasoning_chain:
            rc = portfolio_decision.reasoning_chain
            # All NINE fields. This line logged seven, and the two it omitted
            # were the two the schema lets default to "" — so the operator-
            # facing log could not distinguish "PM red-teamed its book" from
            # "PM skipped the step" (2026-08-13 agent audit).
            logger.info(
                "PM Reasoning Chain:\n  Macro: %s\n  News: %s\n  Earnings: %s\n  "
                "Conflicts: %s\n  Sizing: %s\n  Balance: %s\n  Cash: %s\n  "
                "Continuity: %s\n  Pre-mortem: %s",
                rc.macro_filter[:120], rc.news_check[:120], rc.earnings_check[:120],
                rc.signal_conflicts[:120], rc.sizing_logic[:120],
                rc.portfolio_balance[:120], rc.cash_target[:120],
                rc.continuity_check[:120] or "[MISSING]",
                rc.premortem_check[:120] or "[MISSING]",
            )

        # Stage 1 (QAMC correlation plumbing): one id per PM call, generated
        # independently of run_id (not reused verbatim) so it stays correct
        # even if a future change ever calls decide() more than once per
        # run. Threaded to the risk_manager agent_logs row (RiskStage) and
        # every trades row this run's decisions produce (ExecutionStage).
        decision_id = f"{run_id}-dec-{uuid.uuid4().hex[:6]}"
        ctx.decision_id = decision_id

        pm_log_kwargs = agent_log_kwargs(pm_result)
        if portfolio_decision is None:
            ctx.analysis_failure_status = (
                pm_result.semantic_status or "pm_agent_failure"
            )
            ctx.analysis_failure_error = (
                pm_result.semantic_error or "no valid PM decision"
            )
        pipeline.db.insert_agent_log(
            agent_name="portfolio_manager", run_id=run_id,
            input_summary=f"{len(analyses)} analyses, ${total_value:.0f} total",
            input_message=pm_result.user_message,
            output_summary=(
                portfolio_decision.portfolio_view
                if portfolio_decision else
                f"{ctx.analysis_failure_status}: {ctx.analysis_failure_error}"
            ),
            full_response=pm_result.raw_text,
            model=pm_result.model,
            tokens_used=pm_result.tokens_used,
            input_tokens=pm_result.input_tokens,
            output_tokens=pm_result.output_tokens,
            cost_usd=pm_result.cost_usd,
            decision_id=decision_id,
            **pm_log_kwargs,
        )

        if not portfolio_decision:
            _record_pipeline_event(
                pipeline, ctx, None, "portfolio_manager", "failed",
                "no_valid_grounded_decision",
            )
            _persist_evidence(
                pipeline.db, run_id=run_id, agent_name="portfolio_manager",
                kind="agent_failure", scope="run", decision_id=decision_id,
                evidence_json=(
                    '{"failure":"no_valid_grounded_decision",'
                    '"stage":"portfolio_manager","decision":null}'
                ),
            )
            ctx.portfolio_decision = None
            return ctx

        import json as _json
        _persist_evidence(
            pipeline.db, run_id=run_id, agent_name="portfolio_manager",
            kind="reasoning", scope="run", decision_id=decision_id,
            evidence_json=_json.dumps({
                "portfolio_view": portfolio_decision.portfolio_view,
                "reasoning_chain": portfolio_decision.reasoning_chain.model_dump(),
            }),
        )
        for target in portfolio_decision.targets:
            _persist_evidence(
                pipeline.db, run_id=run_id, agent_name="portfolio_manager",
                kind="target", scope="symbol", symbol=target.symbol,
                decision_id=decision_id, evidence_json=target.model_dump_json(),
            )
        target_symbols = {target.symbol for target in portfolio_decision.targets}
        for analysis in analyses:
            if analysis.symbol not in target_symbols:
                _record_pipeline_event(
                    pipeline, ctx, analysis.symbol, "portfolio_manager", "omitted",
                    "candidate_not_selected_for_target",
                )

        price_map = {p.symbol: p.current_price for p in positions}
        for target in portfolio_decision.targets:
            sym = target.symbol.strip().upper()
            if sym in price_map:
                continue
            try:
                live = pipeline.broker.get_latest_price(sym)
            except Exception as e:
                logger.warning("Constructor price lookup failed for %s: %s", sym, e)
                continue
            if live and live > 0:
                price_map[sym] = live
        # Spec §2.2 — the book's risk as the constructor must ration it. Both
        # come from `ctx.facts`, which is exactly what the PM was shown before
        # it decided, so the gate judges the plan against the same numbers the
        # plan was made against. Absent facts (a stage built without them)
        # leave both None and the portfolio ceilings unenforced rather than
        # enforced against a fabricated view of the book.
        existing_risk_pct, risk_clusters = _book_risk_inputs(ctx, total_value)
        portfolio_decision.decisions = pipeline.portfolio_constructor.construct_orders(
            targets=portfolio_decision.targets,
            positions=positions,
            analyses=analyses,
            total_value=total_value,
            price_map=price_map,
            existing_risk_pct=existing_risk_pct,
            clusters=risk_clusters,
            # The tape the stop has to survive. Widening a stop past the noise
            # band is not a fixed number of ATRs — a risk-off market swings
            # wider for the same ATR reading than a trending one.
            regime=_macro_regime(macro_analysis),
        )
        logger.info(
            "Constructor: %d targets → %d decisions "
            "(%d BUY, %d SELL, %d SHORT, %d COVER, %d HOLD)",
            len(portfolio_decision.targets),
            len(portfolio_decision.decisions),
            sum(1 for d in portfolio_decision.decisions if d.action == "BUY"),
            sum(1 for d in portfolio_decision.decisions if d.action == "SELL"),
            sum(1 for d in portfolio_decision.decisions if d.action == "SHORT"),
            sum(1 for d in portfolio_decision.decisions if d.action == "COVER"),
            sum(1 for d in portfolio_decision.decisions if d.action == "HOLD"),
        )
        # "Proposed" evidence — the constructor's concrete order BEFORE the
        # AI Risk Manager reviews/modifies it (RiskStage persists the
        # post-review verdict/modifications separately). Together these let
        # the UI show a proposed-vs-executed delta per symbol without
        # re-deriving it from raw agent_logs text.
        for decision in portfolio_decision.decisions:
            _persist_evidence(
                pipeline.db, run_id=run_id, agent_name="portfolio_manager",
                kind="proposed_order", scope="symbol", symbol=decision.symbol,
                decision_id=decision_id, evidence_json=decision.model_dump_json(),
            )
            _record_pipeline_event(
                pipeline, ctx, decision.symbol, "portfolio_manager", "proposed",
                "constructor_created_order", action=decision.action,
            )
        ctx.portfolio_decision = portfolio_decision
        return ctx


class RiskStage:
    """Hard filter → earnings cap → correlation → RM review → mods → re-filter.

    Reads:  ctx.portfolio_decision, ctx.positions, ctx.total_value,
            ctx.last_equity, ctx.earnings_results, ctx.macro_analysis,
            ctx.analyses, ctx.symbols_bars, ctx.data_status, ctx.news_intel,
            ctx.macro_summary

    Writes: ctx.portfolio_decision.decisions (filtered/capped/scaled),
            ctx.correlation_matrix, ctx.daily_pnl, ctx.macro_target_pct

    Returns an early-exit dict (symbol_block / hard_risk_block / rejected)
    or None when the pipeline should proceed to execution.
    """

    def __init__(self, *, pipeline: "TradingPipeline"):
        self._pipeline = pipeline

    def run(self, ctx: RunContext) -> dict | None:
        pipeline = self._pipeline
        run_id = ctx.run_id
        portfolio_decision = ctx.portfolio_decision
        positions = ctx.positions
        total_value = ctx.total_value
        last_equity = ctx.last_equity
        earnings_results = ctx.earnings_results
        macro_analysis = ctx.macro_analysis
        analyses = ctx.analyses
        news_intel = ctx.news_intel
        data_status = ctx.data_status

        # Cash-sweep view — same contract as DecisionStage: the RiskManager
        # must never see parked T-bills as an 84%-of-book "position" (review
        # finding: PM and RM otherwise get contradictory views of the same
        # dollars in the same run). IMPORTANT: only the LLM-facing uses (RM
        # prompt, correlation pool, has_book_to_check) take the scrubbed
        # list — the hard filter keeps RAW positions because it still needs
        # to find the vehicle in the list to exclude it from net-exposure /
        # cluster math (it no longer credits any cash from it — see the
        # 2026-08-19 SGOV/deployable-liquidity forensic note below).
        from src.execution.cash_sweep import CashSweeper
        sweeper = getattr(pipeline, "_sweeper", None)
        sweeper = sweeper() if callable(sweeper) else None
        rm_positions = positions
        if isinstance(sweeper, CashSweeper):
            rm_positions, _parked = sweeper.split_positions(positions)

        # Symbol guard
        before_symbol_guard = list(portfolio_decision.decisions)
        guard_kwargs = (
            {"admitted_symbols": ctx.admitted_symbols}
            if ctx.admitted_symbols else {}
        )
        portfolio_decision.decisions, symbol_blocked_reasons = pipeline._filter_supported_symbols(
            portfolio_decision.decisions, analyses, positions, **guard_kwargs,
        )
        if symbol_blocked_reasons:
            reasons = "; ".join(dict.fromkeys(symbol_blocked_reasons))
            logger.warning("SYMBOL GUARD BLOCK: %s", reasons)
            allowed_ids = {id(d) for d in portfolio_decision.decisions}
            for decision in before_symbol_guard:
                if id(decision) not in allowed_ids:
                    _record_pipeline_event(
                        pipeline, ctx, decision.symbol, "deterministic_gate",
                        "blocked", "symbol_guard", detail=reasons,
                    )
            if not portfolio_decision.decisions:
                return {"status": "symbol_block", "orders": [], "reason": reasons}
            logger.info(
                "Allowing %d supported orders through after symbol guard filter",
                len(portfolio_decision.decisions),
            )

        # Pass the book so the cap measures the RESULTING weight, not just the
        # add: allocation_pct here is the constructor's delta, so a name already
        # at 15% with an unread filing could otherwise be topped up to 20%.
        # rm_positions (sweep-vehicle-free) is the right basis — parked T-bills
        # are cash and never carry an earnings filing.
        portfolio_decision.decisions = pipeline._clamp_queued_earnings_buys(
            portfolio_decision.decisions, earnings_results,
            positions=rm_positions, total_value=total_value,
        )

        daily_pnl = total_value - last_equity
        ctx.daily_pnl = daily_pnl
        macro_target_pct = _macro_target_invested_pct(macro_analysis)
        ctx.macro_target_pct = macro_target_pct

        # Holding ages + system-drawdown state (2026-08-13 agent audit).
        # Normally DecisionStage already published both. On the RC2 resume lane
        # it never ran, so rebuild rather than let the gate silently lose the
        # evidence. Both builders are local DB reads with no LLM call and no
        # broker call; a failure degrades to "not provided" in the prompt,
        # never to a wrong value that reads as "no drawdown".
        #
        # Resolved HERE rather than just before the RM call (where it lived
        # until the audit §1.1 fix) because the drawdown-halve is now a
        # deterministic gate that runs BEFORE the hard risk filter, and the
        # filter is upstream of RM.
        rm_position_history = ctx.position_history
        rm_recent_performance = ctx.recent_performance
        if not rm_position_history:
            try:
                rm_position_history = pipeline._build_position_history(rm_positions)
                ctx.position_history = rm_position_history
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    "RiskStage: position history rebuild failed — RM will see "
                    "holding ages as unknown: %s", e,
                )
                rm_position_history = {}
        if not rm_recent_performance:
            try:
                rm_recent_performance = pipeline._compute_recent_performance(last_equity)
                ctx.recent_performance = rm_recent_performance
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    "RiskStage: recent-performance rebuild failed — the "
                    "drawdown gate cannot fire this run: %s", e,
                )
                rm_recent_performance = {}
        in_drawdown = bool(rm_recent_performance.get("in_drawdown"))

        # Audit §1.1 — the drawdown-halve is deterministic code now, applied
        # before the hard filter so every downstream consumer (cash budget,
        # sector accumulation, RM, execution) sees the halved size rather than
        # PM's pre-halving intent. The PM prompt no longer pre-applies it.
        if in_drawdown:
            from src.risk.rules import apply_drawdown_scale
            portfolio_decision.decisions, drawdown_notes = apply_drawdown_scale(
                portfolio_decision.decisions, in_drawdown=True,
            )
            for note in drawdown_notes:
                symbol = note.split(" ", 1)[0]
                _record_pipeline_event(
                    pipeline, ctx, symbol, "deterministic_gate", "modified",
                    "drawdown_buy_halved", detail=note,
                )

        # Memoized by DecisionStage so PM and this gate score the same numbers.
        # On the RC2 resume lane DecisionStage never ran and this is the first
        # build; the helper handles both cases.
        correlation_matrix = pipeline._ensure_correlation_matrix(ctx, rm_positions)

        before_hard_gate = list(portfolio_decision.decisions)
        portfolio_decision.decisions, rule_violations, blocked_reasons = (
            pipeline._filter_hard_risk_decisions(
                portfolio_decision.decisions,
                positions, total_value, daily_pnl,
                baseline=last_equity,
                macro_target_invested_pct=macro_target_pct,
                correlation_matrix=correlation_matrix,
                cash=ctx.deployable_cash,
                in_drawdown=in_drawdown,
            )
        )
        if blocked_reasons:
            reasons = "; ".join(dict.fromkeys(blocked_reasons))
            logger.warning("HARD RISK BLOCK (BUY blocked): %s", reasons)
            allowed_ids = {id(d) for d in portfolio_decision.decisions}
            for decision in before_hard_gate:
                if id(decision) not in allowed_ids:
                    _record_pipeline_event(
                        pipeline, ctx, decision.symbol, "deterministic_gate",
                        "blocked", "hard_risk", detail=reasons,
                    )
            if not portfolio_decision.decisions:
                pipeline._persist_hard_risk_block(ctx, reasons, stage="pre_rm")
                return {"status": "hard_risk_block", "orders": [], "reason": reasons}
            logger.info(
                "Allowing %d non-blocked orders through after hard risk filter",
                len(portfolio_decision.decisions),
            )

        degraded = [k for k, v in data_status.items() if v not in ("ok", "empty")]
        if len(degraded) >= 2:
            from src.risk.rules import RiskViolation as _RV
            rule_violations.append(_RV(
                rule="data_degraded",
                message=(
                    f"Upstream data sources degraded: {', '.join(sorted(degraded))} "
                    f"(status: {data_status}). Decisions may be built on incomplete input — "
                    f"RM should consider scale_all_buys < 1.0."
                ),
                value=float(len(degraded)),
                limit=1.0,
            ))
            logger.warning("Morning data degradation: %s", data_status)

        has_book_to_check = len(rm_positions) >= 2 or any(
            d.action in ("BUY", "SHORT") for d in portfolio_decision.decisions
        )
        if (not correlation_matrix) and has_book_to_check:
            from src.risk.rules import RiskViolation as _RV
            rule_violations.append(_RV(
                rule="correlation_coverage_gap",
                message=(
                    "Correlation matrix is empty (insufficient bar data this run). "
                    "The cluster-concentration advisory is DISABLED. Consider "
                    "scale_all_buys < 1.0 until coverage returns, especially for "
                    "thematic names (AI, semis, energy)."
                ),
                value=0.0,
                limit=2.0,
            ))
            logger.warning(
                "Correlation matrix empty — cluster risk check disabled for this run "
                "(positions=%d, buy_candidates=%d)",
                len(positions),
                sum(1 for d in portfolio_decision.decisions if d.action == "BUY"),
            )

        # 2026-08-13 agent audit — "premortem/observability". `premortem_check`
        # and `continuity_check` are MANDATORY in portfolio_manager.md but
        # default to "" in ReasoningChain, so PM skipping the two disconfirming
        # steps produced a clean parse, a clean log line and a clean verdict.
        # The step could vanish and nothing in the system would say so.
        #
        # The schema stays permissive on purpose (pre-2026-06 logs carry
        # neither field and replay must keep parsing them — see
        # src/models.py::ReasoningChain), so the observability lands here as an
        # ADVISORY, the same non-blocking seam `data_degraded` and
        # `correlation_coverage_gap` already use. No order is blocked by it;
        # RM's prompt requires every advisory to be answered in the matching
        # reasoning_chain field, which is what makes the omission visible.
        rc_now = portfolio_decision.reasoning_chain
        if rc_now is not None:
            missing_audit_steps = [
                name for name, value in (
                    ("premortem_check", rc_now.premortem_check),
                    ("continuity_check", rc_now.continuity_check),
                )
                if not (value or "").strip()
            ]
            if missing_audit_steps:
                from src.risk.rules import RiskViolation as _RV
                rule_violations.append(_RV(
                    rule="pm_audit_step_missing",
                    message=(
                        f"PM returned no {' and no '.join(missing_audit_steps)} — "
                        f"mandatory in its prompt, optional in the schema, so this "
                        f"raised no parse error. The disconfirming/red-team step of "
                        f"today's plan was NOT performed. Weigh the plan as unaudited "
                        f"in that respect and address it in "
                        f"`reasoning_chain.overall`."
                    ),
                    value=float(len(missing_audit_steps)),
                    limit=0.0,
                ))
                logger.warning(
                    "PM reasoning chain missing mandatory audit step(s): %s "
                    "(run_id=%s) — surfaced to RM as a pm_audit_step_missing advisory",
                    ", ".join(missing_audit_steps), run_id,
                )

        # 2026-08-19 SGOV/deployable-liquidity forensic: RM used to be told
        # `cash + parked SGOV value`, the same overstated figure PM saw —
        # RM's cash_only / sizing_sanity audit was therefore auditing PM
        # against a number neither of them could actually spend same-day.
        # RM now gets `ctx.deployable_cash` (settled, non-margin) plus the
        # parked reserve separately/informationally via `reserve_balance`.
        rm_cash = ctx.deployable_cash
        rm_reserve_balance = 0.0
        if isinstance(sweeper, CashSweeper):
            rm_reserve_balance = sweeper.parked_value(ctx.positions)

        verdict, rm_result = pipeline.risk_manager.review(
            portfolio_decision=portfolio_decision,
            positions=rm_positions,
            macro_summary=ctx.macro_summary,
            rule_violations=rule_violations,
            tech_analyses=analyses,
            news_intel=news_intel,
            earnings_analyses=earnings_results,
            # audit round 2: the veto layer's rr_audit / sizing_sanity steps
            # ran blind — no equity, no cash, no weights.
            total_value=total_value,
            cash=rm_cash,
            reserve_balance=rm_reserve_balance,
            position_history=rm_position_history,
            recent_performance=rm_recent_performance,
            # Audit §1.3 — same heat object PM sized against, so RM audits the
            # book's real risk instead of re-deriving it from notional weights.
            heat=getattr(ctx.facts, "heat", None) if ctx.facts else None,
            risk_ceiling_pct=(
                getattr(ctx.facts, "risk_ceiling_pct", 25.0) if ctx.facts else 25.0
            ),
        )

        rm_log_kwargs = agent_log_kwargs(rm_result)
        if verdict is None:
            rm_log_kwargs["status"] = "agent_failure"
        pipeline.db.insert_agent_log(
            agent_name="risk_manager", run_id=run_id,
            input_summary=f"{len(portfolio_decision.decisions)} trades, {len(rule_violations)} violations",
            input_message=rm_result.user_message,
            output_summary=f"Approved: {verdict.approved if verdict else 'error'}",
            full_response=rm_result.raw_text,
            model=rm_result.model,
            tokens_used=rm_result.tokens_used,
            input_tokens=rm_result.input_tokens,
            output_tokens=rm_result.output_tokens,
            cost_usd=rm_result.cost_usd,
            decision_id=ctx.decision_id,
            **rm_log_kwargs,
        )

        if verdict:
            _persist_evidence(
                pipeline.db, run_id=run_id, agent_name="risk_manager",
                kind="verdict", scope="run", decision_id=ctx.decision_id,
                evidence_json=verdict.model_dump_json(),
            )
            for mod in verdict.modifications:
                _persist_evidence(
                    pipeline.db, run_id=run_id, agent_name="risk_manager",
                    kind="modification", scope="symbol", symbol=mod.symbol,
                    decision_id=ctx.decision_id, evidence_json=mod.model_dump_json(),
                )

        if verdict is None:
            logger.error(
                "Risk manager AGENT FAILURE: output remained unparseable after "
                "bounded repair; no trading verdict exists",
            )
            for decision in portfolio_decision.decisions:
                _record_pipeline_event(
                    pipeline, ctx, decision.symbol, "risk", "failed",
                    "risk_manager_unparseable_output",
                )
            _persist_evidence(
                pipeline.db, run_id=run_id, agent_name="risk_manager",
                kind="agent_failure", scope="run", decision_id=ctx.decision_id,
                evidence_json=(
                    '{"failure":"unparseable_output",'
                    '"stage":"risk_manager","verdict":null}'
                ),
            )
            return {
                "status": "agent_failure", "orders": [],
                "reason": "risk_manager_unparseable_output",
            }

        if not verdict.approved:
            logger.info(
                "Risk manager REJECTED trades: %s",
                verdict.reasoning,
            )
            for decision in portfolio_decision.decisions:
                _record_pipeline_event(
                    pipeline, ctx, decision.symbol, "risk", "rejected",
                    verdict.reasoning,
                )
            return {
                "status": "rejected", "orders": [],
                "reason": verdict.reasoning,
            }

        if verdict.modifications:
            portfolio_decision.decisions = pipeline._apply_risk_modifications(
                portfolio_decision.decisions, verdict.modifications,
            )

        portfolio_decision.decisions, scale = _apply_scale_all_buys(
            portfolio_decision.decisions, verdict,
        )

        if verdict.modifications or scale < 1.0:
            portfolio_decision.decisions, _, blocked_reasons = (
                pipeline._filter_hard_risk_decisions(
                    portfolio_decision.decisions,
                    positions, total_value, daily_pnl,
                    baseline=last_equity,
                    macro_target_invested_pct=macro_target_pct,
                    correlation_matrix=correlation_matrix,
                    cash=ctx.deployable_cash,
                )
            )
            if blocked_reasons:
                reasons = "; ".join(dict.fromkeys(blocked_reasons))
                logger.warning("HARD RISK BLOCK AFTER MODIFICATIONS: %s", reasons)
                if not portfolio_decision.decisions:
                    pipeline._persist_hard_risk_block(ctx, reasons, stage="post_rm_modifications")
                    return {"status": "hard_risk_block", "orders": [], "reason": reasons}

        modified_symbols = {mod.symbol for mod in verdict.modifications}
        for decision in portfolio_decision.decisions:
            _record_pipeline_event(
                pipeline, ctx, decision.symbol, "risk",
                "modified" if decision.symbol in modified_symbols or scale < 1.0 else "approved",
                "risk_manager_verdict",
            )
            _record_pipeline_event(
                pipeline, ctx, decision.symbol, "deterministic_gate", "allowed",
                "post_risk_checks_passed",
            )
        return None


class ExecutionStage:
    """Record HOLDs → submit SELLs → wait → refresh → submit BUYs.

    Reads:  ctx.portfolio_decision.decisions, ctx.positions, ctx.cash,
            ctx.total_value, ctx.symbols_bars
    Writes: ctx.orders, and on SELL refresh: ctx.positions / .cash / .total_value
    """

    def __init__(self, *, pipeline: "TradingPipeline"):
        self._pipeline = pipeline

    def run(self, ctx: RunContext) -> list[dict]:
        pipeline = self._pipeline
        run_id = ctx.run_id
        # Stage 1 (QAMC correlation plumbing): links every trades row this
        # run produces back to the PM proposal / RM review that led to it.
        # None on any run that never reached a successful PM call (e.g. an
        # early-exit before DecisionStage) — trades rows from such a run
        # simply carry no decision_id, which is correct, not a bug.
        decision_id = ctx.decision_id
        positions = ctx.positions
        total_value = ctx.total_value
        cash = ctx.cash
        portfolio_decision = ctx.portfolio_decision

        orders: list[dict] = []
        sell_decisions = [d for d in portfolio_decision.decisions if d.action == "SELL"]
        # Stage 3 (shorts): SHORT is the entry-side twin of BUY — both open
        # or add to a position and both owe a mandatory protective stop, so
        # they share the entry submission loop below (branching internally
        # on `decision.action` for side / geometry / sizing). COVER is the
        # exit-side twin of SELL and gets its OWN loop further down that
        # reuses `_submit_protected_sell` with side="buy", exactly the
        # plumbing PR #135 built for emergency covers.
        buy_decisions = [
            d for d in portfolio_decision.decisions if d.action in ("BUY", "SHORT")
        ]
        cover_decisions = [d for d in portfolio_decision.decisions if d.action == "COVER"]
        hold_decisions = [d for d in portfolio_decision.decisions if d.action == "HOLD"]

        for d in hold_decisions:
            try:
                pipeline.db.insert_trade(
                    symbol=d.symbol, action="HOLD", qty=0.0, price=0.0,
                    reasoning=d.reasoning, run_id=run_id,
                    decision_id=decision_id,
                )
            except Exception as e:
                logger.warning("Failed to record HOLD decision for %s: %s", d.symbol, e)

        sell_order_ids: list[str] = []
        pending_protections: list[dict] = []
        for decision in sell_decisions:
            try:
                existing = [p for p in positions if p.symbol == decision.symbol]
                if not existing or existing[0].qty <= 0:
                    continue
                if decision.allocation_pct == 0:
                    logger.warning(
                        "Skipping SELL %s with allocation_pct=0 (ambiguous — use 100 for full exit)",
                        decision.symbol,
                    )
                    continue
                if 0 < decision.allocation_pct < 100:
                    sell_fraction = decision.allocation_pct / 100
                    qty = existing[0].qty * sell_fraction
                    if float(existing[0].qty).is_integer():
                        qty = max(1.0, float(int(qty)))
                    if qty <= 0:
                        continue
                    if qty >= existing[0].qty:
                        qty = pipeline._full_sell_qty(existing[0].qty)
                        if qty is None:
                            continue
                        action_label = "SELL"
                    else:
                        action_label = f"PARTIAL_SELL({decision.allocation_pct:.0f}%)"
                else:
                    qty = pipeline._full_sell_qty(existing[0].qty)
                    if qty is None:
                        continue
                    action_label = "SELL"
                sell_price = existing[0].current_price
                sell_limit = round(sell_price * 0.995, 2)
                position_qty = existing[0].qty
                # Single protected-sell discipline (cancel-WAL → submit →
                # accept → restore-on-failure) lives in one helper so this path
                # can't skip a step; defer reprotect/restore to the post-sell
                # wait below, which resolves the actual fill_qty.
                sale = pipeline._submit_protected_sell(
                    symbol=decision.symbol, qty=qty, limit_price=sell_limit,
                    reference_price=existing[0].current_price,
                    position_qty_before_sell=position_qty, label=action_label,
                )
                if sale is None:
                    continue
                order, prot = sale
                pending_protections.append(prot)
                orders.append(order)
                sell_order_ids.append(order["id"])
                pipeline.db.insert_trade(
                    symbol=decision.symbol, action=action_label, qty=qty,
                    price=sell_price, reasoning=decision.reasoning, run_id=run_id,
                    broker_order_id=order.get("id"),
                    fill_status="submitted",
                    decision_id=decision_id,
                )
                _record_pipeline_event(
                    pipeline, ctx, decision.symbol, "order", "submitted",
                    "broker_accepted", broker_order_id=order.get("id"), qty=qty,
                    limit_price=sell_limit, side="sell",
                )
                logger.info(
                    "Executed: %s %s %s @ limit $%.2f",
                    action_label.lower(), pipeline._format_qty(qty), decision.symbol, sell_limit,
                )
            except Exception as e:
                logger.error("Order failed for %s %s: %s", decision.action, decision.symbol, e)

        for order_id in sell_order_ids:
            # ExecutionStage was the lone SELL path missing this guard
            # — every other SELL path (force_delever / midday_emergency /
            # midday_llm / intra_check / take_profit) wraps the wait in
            # try/except. An uncaught exception here (broker 5xx, DNS
            # blip mid-poll) would propagate past the finalize loop
            # below. The audit F1 write-ahead row already covers a hard
            # process kill; this try/except additionally keeps the
            # in-process finalize path alive so coverage is rebuilt now
            # rather than waiting for the next session's drain.
            try:
                status = pipeline.broker.wait_for_order_terminal(order_id)
            except Exception as e:
                logger.warning(
                    "ExecutionStage: wait_for_order_terminal failed for %s: %s "
                    "— treating as unknown status so finalize still runs",
                    order_id, e,
                )
                status = None
            if status != "filled":
                logger.warning(
                    "Sell order %s did not fill before buy phase (status=%s); buys will use current cash only",
                    order_id, status or "unknown",
                )

        # Now that wait_for_order_terminal has returned for every sell,
        # the broker's fill_info is final. Reprotect on actual residual
        # (filled successfully) or restore originals (no-fill terminal).
        # wait=False: the sell_order_ids loop above already blocked until each
        # order reached terminal (it also gates the buy phase), so the orders
        # are terminal here — re-waiting would be a redundant no-op.
        pipeline._finalize_pending_protections(
            pending_protections, context="ExecutionStage", wait=False,
        )

        # Stage 3 (shorts): COVER loop — the exit-side twin of the SELL loop
        # just above. Reuses `_submit_protected_sell`'s side="buy" plumbing
        # (PR #135 built this for emergency covers; this is the first
        # decision-path caller). No protective stop is placed afterward —
        # covering REDUCES risk, it doesn't open any.
        cover_order_ids: list[str] = []
        cover_pending_protections: list[dict] = []
        for decision in cover_decisions:
            try:
                existing = [p for p in positions if p.symbol == decision.symbol]
                if not existing or existing[0].qty >= 0:
                    continue  # nothing short held — COVER on a long/flat is refused
                held_qty = abs(existing[0].qty)
                if decision.allocation_pct == 0:
                    logger.warning(
                        "Skipping COVER %s with allocation_pct=0 (ambiguous — use 100 for full exit)",
                        decision.symbol,
                    )
                    continue
                if 0 < decision.allocation_pct < 100:
                    cover_fraction = decision.allocation_pct / 100
                    qty = held_qty * cover_fraction
                    if float(held_qty).is_integer():
                        qty = max(1.0, float(int(qty)))
                    if qty <= 0:
                        continue
                    if qty >= held_qty:
                        qty = pipeline._full_sell_qty(held_qty)
                        if qty is None:
                            continue
                        action_label = "COVER"
                    else:
                        action_label = f"PARTIAL_COVER({decision.allocation_pct:.0f}%)"
                else:
                    qty = pipeline._full_sell_qty(held_qty)
                    if qty is None:
                        continue
                    action_label = "COVER"
                cover_price = existing[0].current_price
                # Buy-to-cover needs headroom ABOVE the reference to fill on
                # the way up — the mirror of the SELL loop's limit sitting
                # 0.5% BELOW (same reasoning as `_EMERGENCY_LIMIT_CUSHION_PCT`
                # in pipeline.py, applied here to the ordinary decision path).
                cover_limit = round(cover_price * 1.005, 2)
                sale = pipeline._submit_protected_sell(
                    symbol=decision.symbol, qty=qty, limit_price=cover_limit,
                    reference_price=existing[0].current_price,
                    position_qty_before_sell=held_qty, label=action_label,
                    side="buy",
                )
                if sale is None:
                    continue
                order, prot = sale
                cover_pending_protections.append(prot)
                orders.append(order)
                cover_order_ids.append(order["id"])
                pipeline.db.insert_trade(
                    symbol=decision.symbol, action=action_label, qty=qty,
                    price=cover_price, reasoning=decision.reasoning, run_id=run_id,
                    broker_order_id=order.get("id"),
                    fill_status="submitted",
                    decision_id=decision_id,
                )
                _record_pipeline_event(
                    pipeline, ctx, decision.symbol, "order", "submitted",
                    "broker_accepted", broker_order_id=order.get("id"), qty=qty,
                    limit_price=cover_limit, side="buy",
                )
                logger.info(
                    "Executed: %s %s %s @ limit $%.2f",
                    action_label.lower(), pipeline._format_qty(qty), decision.symbol, cover_limit,
                )
            except Exception as e:
                logger.error("Order failed for %s %s: %s", decision.action, decision.symbol, e)

        for order_id in cover_order_ids:
            try:
                status = pipeline.broker.wait_for_order_terminal(order_id)
            except Exception as e:
                logger.warning(
                    "ExecutionStage: wait_for_order_terminal failed for %s: %s "
                    "— treating as unknown status so finalize still runs",
                    order_id, e,
                )
                status = None
            if status != "filled":
                logger.warning(
                    "Cover order %s did not fill before buy phase (status=%s)",
                    order_id, status or "unknown",
                )

        pipeline._finalize_pending_protections(
            cover_pending_protections, context="ExecutionStage-Cover", wait=False,
        )

        if sell_decisions or cover_decisions:
            account, positions, price_map = pipeline._refresh_account_state()
            cash = account["cash"]
            total_value = account["portfolio_value"]
            ctx.positions = positions
            ctx.cash = cash
            ctx.deployable_cash = pipeline._compute_deployable_cash(cash, positions)
            ctx.total_value = total_value
            logger.info(
                "Post-sell refresh: $%.2f total, $%.2f cash, %d positions",
                total_value, cash, len(positions),
            )
        else:
            price_map = {p.symbol: p.current_price for p in positions}

        # Daily-loss re-check before BUYs. The initial circuit breaker ran
        # ~10 min ago (before LLM research); the tape may have gapped
        # through the limit while PM/RM was thinking, especially relevant
        # now that intra_check fires concurrently per #46. We block BUYs
        # (no new risk during a confirmed breach) but let any pending SELLs
        # stay — they reduced exposure already. intra's next tick handles
        # full emergency liquidation; morning's job here is just to not
        # add to the hole. Refresh first when sells didn't fire so the
        # check uses fresh portfolio_value, not the stale research-stage
        # snapshot.
        if buy_decisions:
            if not sell_decisions:
                # Take the FRESH price_map too (2026-07-16 audit): it was
                # discarded into `_`, leaving `price_map` at research-time
                # position prices from 5-10 minutes earlier. For an ADD to a
                # held name that stale price is what the 5% entry-staleness
                # guard compares the LLM's entry against, and what sizes the
                # order — so the guard could pass a genuinely stale entry (or
                # reject a good one) on exactly the fast-moving tape where it
                # matters. New symbols were unaffected (they miss the map and
                # fall through to a live quote).
                account, positions, fresh_prices = pipeline._refresh_account_state()
                cash = account["cash"]
                total_value = account["portfolio_value"]
                ctx.positions = positions
                ctx.cash = cash
                ctx.deployable_cash = pipeline._compute_deployable_cash(cash, positions)
                ctx.total_value = total_value
                price_map = {**price_map, **fresh_prices}
            daily_pnl_now = total_value - ctx.last_equity
            loss_violation_now = pipeline.risk_engine.check_daily_loss(
                ctx.last_equity, daily_pnl_now,
            )
            if loss_violation_now:
                logger.warning(
                    "ExecutionStage daily-loss re-check: %s — blocking "
                    "%d BUY(s); intra will liquidate on next tick",
                    loss_violation_now.message, len(buy_decisions),
                )
                for d in buy_decisions:
                    _record_execution_skip(
                        pipeline, ctx, d.symbol, "daily_loss_recheck",
                        loss_violation_now.message,
                    )
                buy_decisions = []

        # Run the cheap deterministic entry-viability checks BEFORE selling
        # SGOV. Production evidence showed the sweep funding names that were
        # guaranteed to die moments later on stale-entry / no-price / qty-zero
        # checks, creating avoidable sell/re-park churn. The full checks remain
        # in the submit loop below; this preflight only removes names whose
        # failure is already knowable and computes the actual whole-share
        # notional that funding should cover.
        fundable_notional: dict[str, float] = {}
        preflight_survivors = []
        for decision in buy_decisions:
            market_price = price_map.get(decision.symbol)
            if not isinstance(market_price, (int, float)) or market_price <= 0:
                live_price = pipeline.broker.get_latest_price(decision.symbol)
                if isinstance(live_price, (int, float)) and live_price > 0:
                    market_price = live_price
                    price_map[decision.symbol] = live_price
            if not isinstance(market_price, (int, float)) or market_price <= 0:
                bars = ctx.symbols_bars.get(decision.symbol) or []
                if bars:
                    last_close = float(bars[-1].close)
                    if last_close > 0:
                        market_price = last_close
                        price_map[decision.symbol] = last_close
            if not isinstance(market_price, (int, float)) or market_price <= 0:
                _record_execution_skip(
                    pipeline, ctx, decision.symbol, "no_price",
                    "no verifiable price reference (broker + bars unavailable)",
                )
                continue
            if decision.entry_price > 0:
                deviation = abs(decision.entry_price - market_price) / market_price
                if deviation > 0.05:
                    _record_execution_skip(
                        pipeline, ctx, decision.symbol, "stale_entry",
                        f"entry ${decision.entry_price:.2f} is "
                        f"{deviation * 100:.1f}% from market "
                        f"${market_price:.2f} (threshold 5%)",
                    )
                    continue
            preflight_price = max(market_price, decision.entry_price or 0)
            preflight_qty = int(
                (total_value * decision.allocation_pct / 100) / preflight_price
            )
            if preflight_qty <= 0:
                _record_execution_skip(
                    pipeline, ctx, decision.symbol, "qty_zero",
                    f"allocation {decision.allocation_pct:.2f}% at "
                    f"${preflight_price:.2f} rounds to zero shares",
                )
                continue
            fundable_notional[decision.symbol] = preflight_qty * preflight_price
            preflight_survivors.append(decision)
        buy_decisions = preflight_survivors

        # Cash-sweep funding: a SHORT's notional is folded into
        # `planned_notional` below alongside real BUYs even though opening a
        # short does not actually need settled cash (it sells borrowed
        # shares). That over-funds rather than under-funds a short-only
        # session — SGOV may get released when it wasn't strictly needed —
        # which is the safe direction to be wrong in and is not reworked
        # here; see the sizing loop below for where a SHORT stops treating
        # cash as a constraint.
        #
        # PM/RM/the hard gate size BUYs against
        # `deployable_cash` (raw cash + convertible sweep value), so on any
        # session with meaningful BUYs this sale IS load-bearing — the raw
        # cash on hand is typically just the reserve. `fund_buys` sells
        # enough of the vehicle to cover the planned notional, then waits
        # for the fill and CONFIRMS the observed rise in broker cash (a
        # filled sale credits `cash` immediately; the 2026-08-19 loss of a
        # fully-approved plan was a 51s fill outliving a 15s wait, not
        # settlement — see cash_sweep._FUND_TERMINAL_TIMEOUT_S). Whatever
        # it confirms, `available_cash` below governs: a BUY the sale
        # didn't actually fund is safely skipped.
        # isinstance guard: stage tests stub `pipeline` with MagicMock.
        if buy_decisions:
            from src.execution.cash_sweep import CashSweeper
            sweeper = getattr(pipeline, "_sweeper", None)
            sweeper = sweeper() if callable(sweeper) else None
            if not isinstance(sweeper, CashSweeper):
                sweeper = None
            if sweeper is not None:
                planned_notional = sum(
                    fundable_notional.get(d.symbol, 0.0) for d in buy_decisions
                )
                for d in buy_decisions:
                    _record_pipeline_event(
                        pipeline, ctx, d.symbol, "funding", "attempted",
                        "cash_sweep_funding", planned_notional=planned_notional,
                    )
                try:
                    freed = sweeper.fund_buys(ctx, planned_notional)
                except Exception as e:
                    logger.warning("cash sweep: fund_buys failed (BUYs will "
                                   "use raw cash only): %s", e)
                    freed = 0.0
                    for d in buy_decisions:
                        _record_pipeline_event(
                            pipeline, ctx, d.symbol, "funding", "failed",
                            "cash_sweep_exception", detail=str(e),
                        )
                if freed > 0:
                    positions = ctx.positions
                    cash = ctx.cash
                    total_value = ctx.total_value
                    for d in buy_decisions:
                        _record_pipeline_event(
                            pipeline, ctx, d.symbol, "funding", "funded",
                            "cash_sweep_confirmed", freed_cash=freed,
                        )
                elif buy_decisions:
                    for d in buy_decisions:
                        _record_pipeline_event(
                            pipeline, ctx, d.symbol, "funding", "no_additional_cash",
                            "cash_sweep_released_zero", raw_cash=cash,
                        )
            else:
                for d in buy_decisions:
                    _record_pipeline_event(
                        pipeline, ctx, d.symbol, "funding", "not_required",
                        "cash_sweep_disabled", raw_cash=cash,
                    )

        available_cash = cash
        pending_entry_stops: list[dict] = []
        for decision in buy_decisions:
            if decision.action not in ("BUY", "SHORT"):
                continue
            is_short = decision.action == "SHORT"
            try:
                # D6 (Stage 3): the borrow gate. Refuse to open a short
                # unless the broker reports it BOTH shortable AND easy to
                # borrow — an API error or an unreadable/unknown symbol
                # reports both False in `get_shortability` (fail closed), so
                # a lookup failure refuses the short rather than guessing it
                # open. This is paper trading against IEX data: a
                # hard-to-borrow name fills unrealistically in paper and its
                # borrow cost is not modeled anywhere in this system, so
                # restricting to easy-to-borrow keeps measured results
                # transferable to live capital.
                if is_short:
                    try:
                        borrow = pipeline.broker.get_shortability(decision.symbol)
                    except Exception as e:  # noqa: BLE001
                        logger.warning(
                            "SHORT %s: shortability lookup raised: %s",
                            decision.symbol, e,
                        )
                        borrow = {
                            "shortable": False, "easy_to_borrow": False,
                            "reason": "asset_lookup_failed",
                        }
                    if not (isinstance(borrow, dict) and borrow.get("shortable")
                            and borrow.get("easy_to_borrow")):
                        reason = (
                            borrow.get("reason", "not_shortable")
                            if isinstance(borrow, dict) else "not_shortable"
                        )
                        logger.warning(
                            "SHORT %s skipped: borrow gate refused (%s)",
                            decision.symbol, reason,
                        )
                        _record_execution_skip(
                            pipeline, ctx, decision.symbol, "borrow_gate", reason,
                        )
                        continue

                market_price = price_map.get(decision.symbol)
                if not market_price or market_price <= 0:
                    live_price = pipeline.broker.get_latest_price(decision.symbol)
                    if live_price and live_price > 0:
                        market_price = live_price
                        price_map[decision.symbol] = live_price
                if not market_price or market_price <= 0:
                    bars = ctx.symbols_bars.get(decision.symbol) or []
                    if bars:
                        last_close = float(bars[-1].close)
                        if last_close > 0:
                            logger.info(
                                "Using last-bar close $%.2f as price reference for %s "
                                "(broker pricing unavailable)",
                                last_close, decision.symbol,
                            )
                            market_price = last_close

                limit_price = None
                sizing_price = None
                if decision.entry_price > 0:
                    limit_price = decision.entry_price

                if market_price and market_price > 0:
                    if limit_price is not None:
                        deviation = abs(limit_price - market_price) / market_price
                        if deviation > 0.05:
                            # Previously fell back to market order here — that
                            # silently absorbed up to 10% slippage against the
                            # LLM's stated entry. Now we skip: if entry_price
                            # is stale by >5%, the stop_loss computed against
                            # that entry is also stale, and the whole R/R math
                            # is bogus. Better to wait for next session.
                            logger.warning(
                                "%s %s skipped: LLM entry_price $%.2f is %.1f%% "
                                "away from market $%.2f (threshold 5%%). Stop/R/R "
                                "computed against stale entry would be unsafe.",
                                decision.action, decision.symbol, decision.entry_price,
                                deviation * 100, market_price,
                            )
                            _record_execution_skip(
                                pipeline, ctx, decision.symbol, "stale_entry",
                                f"entry ${decision.entry_price:.2f} is "
                                f"{deviation * 100:.1f}% from market "
                                f"${market_price:.2f} (threshold 5%)",
                            )
                            continue
                        elif not is_short and limit_price < market_price:
                            logger.info(
                                "Adjusting limit price for %s: $%.2f → $%.2f (raised to market)",
                                decision.symbol, limit_price, market_price,
                            )
                            limit_price = market_price
                            sizing_price = market_price
                        elif is_short and limit_price > market_price:
                            # Mirror: a resting SHORT limit sitting ABOVE
                            # market is not marketable — you can't sell short
                            # above the market and expect an immediate fill —
                            # so pull it DOWN to market instead of UP.
                            logger.info(
                                "Adjusting limit price for SHORT %s: $%.2f → "
                                "$%.2f (lowered to market)",
                                decision.symbol, limit_price, market_price,
                            )
                            limit_price = market_price
                            sizing_price = market_price
                        else:
                            sizing_price = (
                                min(market_price, limit_price) if is_short
                                else max(market_price, limit_price)
                            )
                    else:
                        sizing_price = market_price
                else:
                    logger.error(
                        "%s %s skipped: no verifiable price reference "
                        "(broker + bars both unavailable). "
                        "LLM proposed entry $%.2f but cannot be validated.",
                        decision.action, decision.symbol, decision.entry_price,
                    )
                    _record_execution_skip(
                        pipeline, ctx, decision.symbol, "no_price",
                        "no verifiable price reference (broker + bars "
                        "unavailable)",
                    )
                    continue

                # Liquid-equity execution policy: cross the displayed offer
                # with a limit (never a market order), padded by 5 bps for a
                # moving quote but hard-capped 25 bps above the verified
                # reference. A wider spread therefore remains price-protected
                # and may expire after the bounded entry window instead of
                # paying through an abnormal book. If quote data is degraded,
                # retain the validated last/PM limit and the same bounded wait.
                #
                # Stage 3: this whole NBBO/ask marketable-limit refinement is
                # BUY-only (`not is_short` below) — it is written
                # asymmetrically for a BUY crossing the displayed OFFER with
                # a bounded ceiling, and mirroring it precisely for a SHORT
                # (crossing the BID, flooring instead of capping) is a
                # self-contained execution-quality task, not one of this
                # stage's architecture decisions. A SHORT still gets the
                # same >5% stale-entry protection and the same direction-
                # aware raise/lower-to-market adjustment just above — it
                # only forgoes the tighter NBBO-aware ceiling a BUY gets.
                try:
                    quote = pipeline.broker.get_latest_quote(decision.symbol)
                except Exception as e:  # noqa: BLE001
                    logger.warning("BUY %s quote lookup failed: %s", decision.symbol, e)
                    quote = None
                ask = quote.get("ask_price") if isinstance(quote, dict) else None
                if not is_short and isinstance(ask, (int, float)) and ask > 0:
                    # The protection cap and the offer are two different
                    # numbers, and when they disagree the ORDER CANNOT FILL.
                    #
                    # 2026-08-27 VLO: reference $349.99, ask $350.96 (28bp
                    # above it), cap 25bp -> limit $350.86. That limit sits
                    # TEN CENTS BELOW the offer. A buy limit below the ask
                    # does not fill, by definition — Alpaca fills a limit at
                    # the limit or better, and there was no better. The order
                    # sat unfilled for 31s, the entry-protection sweep
                    # cancelled it, and the session still reported
                    # `status: executed`. The trade was never possible; the
                    # system just never said so.
                    #
                    # Price protection itself is correct and stays: crossing
                    # an abnormal book at the open is how you pay 3% for a
                    # 0.3% idea. What changes is that an unfillable order is
                    # now a DECISION with a reason, not a doomed submission.
                    # isinstance-guarded: ~58 tests build the pipeline with a
                    # MagicMock config, whose auto-attributes are truthy and
                    # would blow up float(). Same convention as `_sweeper`.
                    _cfg = getattr(
                        getattr(pipeline.config, "execution", None),
                        "max_entry_slippage_bps", None,
                    )
                    slippage_bps = (
                        float(_cfg)
                        if isinstance(_cfg, (int, float)) and _cfg > 0
                        else MAX_ENTRY_SLIPPAGE_BPS
                    )
                    # A LIMIT IS A CEILING, NOT A PRICE.
                    #
                    # This is the correction that matters. Alpaca fills a buy
                    # limit at the NBBO or better — submitting $50.05 when the
                    # offer is $50.02 does not pay $50.05, it pays $50.02. So
                    # shaving the limit down toward the offer buys NOTHING and
                    # costs fills. The old `min(ask * 1.0005, cap)` treated the
                    # limit as if it were the execution price and haggled over
                    # it, which is how VLO ended up bid ten cents under a
                    # market it was trying to cross.
                    #
                    # Worse, the `ask` being haggled against is not the ask we
                    # trade at. This account is entitled to IEX, not SIP
                    # (verified 2026-08-27: a SIP quote request returns
                    # "subscription does not permit querying recent SIP
                    # data"). IEX is a single venue carrying a small share of
                    # volume, and its top of book is routinely stale or absurd
                    # — CCJ quoted bid $92.96 / ask $107.10, a 15% spread, in
                    # the middle of a normal session. Alpaca's matching engine
                    # uses the consolidated NBBO. Pricing an order against IEX
                    # while filling against NBBO is the root cause.
                    #
                    # So: set the limit AT the ceiling we are willing to pay,
                    # and let the match happen at the real NBBO underneath it.
                    # Price protection is unchanged — `slippage_bps` still
                    # bounds the worst possible fill — it just stops being
                    # self-defeating.
                    cap = market_price * (1 + slippage_bps / 10_000.0)
                    offer_limit = round(cap, 2 if cap >= 1 else 4)
                    ask_premium_bps = (ask - market_price) / market_price * 10_000.0

                    # The IEX ask is too unreliable to gate on directly, but a
                    # far-through reading is still information: either the
                    # market has genuinely run, or the venue is quoting
                    # nonsense. Either way this is not a book to cross blind.
                    # The multiple is deliberately loose because the input is.
                    if ask > cap * 1.02:
                        logger.warning(
                            "BUY %s NOT SUBMITTED — the displayed offer has run "
                            "beyond the slippage ceiling. Ask $%.4f is %.1fbp "
                            "above the $%.4f reference; the %.0fbp ceiling is "
                            "$%.4f. (Quote is IEX, not NBBO, so it may also "
                            "simply be a stale venue print — either way, not a "
                            "book to cross blind.)",
                            decision.symbol, ask, ask_premium_bps,
                            market_price, slippage_bps, cap,
                        )
                        _record_execution_skip(
                            pipeline, ctx, decision.symbol, "slippage_gated",
                            f"IEX ask ${ask:.4f} is {ask_premium_bps:.1f}bp "
                            f"above reference ${market_price:.4f}, beyond the "
                            f"{slippage_bps:.0f}bp ceiling ${cap:.4f}",
                        )
                        continue

                    if limit_price is None or abs(limit_price - offer_limit) > 0.000001:
                        logger.info(
                            "BUY %s marketable-limit: prior $%s → ceiling $%.4f "
                            "(%.0fbp above reference $%.4f). Fills at NBBO or "
                            "better; IEX ask reads $%.4f (%.1fbp).",
                            decision.symbol,
                            f"{limit_price:.4f}" if limit_price is not None else "none",
                            offer_limit, slippage_bps, market_price,
                            ask, ask_premium_bps,
                        )
                    limit_price = offer_limit
                    sizing_price = max(sizing_price or 0, offer_limit)

                # RC1: code-enforced ATR stop-distance floor at entry. The
                # P1 prompt rule ("fresh-entry stops never tighter than
                # 1×ATR") is advisory — LLM output still occasionally lands
                # stops inside one day's range, which converts routine
                # volatility into a same-week exit. Widen to 1×ATR(14) from
                # bars already fetched by research; qty_by_risk below sizes
                # against the wider distance, so per-trade $ risk is
                # unchanged. No bars → no floor (behavior identical).
                #
                # BUY-only (`not is_short`): the constructor's own
                # `_widen_stop_past_noise` (D5) already applies a mirrored,
                # direction-aware ATR floor to a SHORT's stop before this
                # code ever sees it; this is a SECOND, execution-time-only
                # belt that was never extended to shorts as part of this
                # stage.
                stop_price = decision.stop_loss
                if not is_short and stop_price > 0 and sizing_price > stop_price:
                    try:
                        bars = ctx.symbols_bars.get(decision.symbol) or []
                        atr14 = None
                        if len(bars) >= 15:
                            from src.data.technical import compute_indicators
                            atr14 = compute_indicators(decision.symbol, bars).atr_14
                        if atr14 and atr14 > 0 and (sizing_price - stop_price) < atr14:
                            widened = round(sizing_price - atr14, 2)
                            logger.warning(
                                "BUY %s: stop $%.2f is %.2f×ATR from entry "
                                "$%.2f — widening to $%.2f (1×ATR14=$%.2f "
                                "floor; qty sizing compensates)",
                                decision.symbol, stop_price,
                                (sizing_price - stop_price) / atr14,
                                sizing_price, widened, atr14,
                            )
                            stop_price = widened
                    except Exception as e:
                        logger.warning("ATR stop floor skipped for %s: %s",
                                       decision.symbol, e)

                # R/R re-check whenever EXECUTION changed the geometry the RM
                # audited — either the stop was ATR-widened OR the limit was
                # raised to market (audit round 2: the raise-to-market path
                # GROWS the stop distance, dodging the ATR gate, yet shrinks
                # reward against the unchanged target — the one case the old
                # nested check could never see). If the honest geometry
                # collapses below a sane floor, the setup RM approved never
                # existed — skip rather than execute a trade nobody reviewed.
                geometry_changed = (
                    stop_price != decision.stop_loss
                    or (decision.entry_price > 0 and sizing_price > decision.entry_price)
                )
                if (not is_short and geometry_changed and decision.take_profit > 0
                        and stop_price > 0 and sizing_price > stop_price):
                    reward = decision.take_profit - sizing_price
                    risk = sizing_price - stop_price
                    if risk > 0 and reward / risk < 1.2:
                        logger.warning(
                            "BUY %s skipped: executed geometry makes R/R %.2f "
                            "(<1.2) — RM approved entry $%.2f / stop $%.2f, "
                            "execution moved it to $%.2f / $%.2f.",
                            decision.symbol, reward / risk,
                            decision.entry_price, decision.stop_loss,
                            sizing_price, stop_price,
                        )
                        _record_execution_skip(
                            pipeline, ctx, decision.symbol, "geometry_rr",
                            f"executed geometry R/R {reward / risk:.2f} < 1.2 "
                            f"(RM approved ${decision.entry_price:.2f}/"
                            f"${decision.stop_loss:.2f}, execution moved to "
                            f"${sizing_price:.2f}/${stop_price:.2f})",
                        )
                        continue

                qty_by_alloc = int((total_value * decision.allocation_pct / 100) / sizing_price)
                qty_by_risk = None
                RISK_BUDGET_PCT = 0.5
                # D4: geometry validity is direction-aware — a long's stop
                # must sit below its entry, a short's strictly above.
                valid_geometry = (
                    (not is_short and stop_price > 0 and sizing_price > stop_price)
                    or (is_short and stop_price > 0 and stop_price > sizing_price)
                )
                if valid_geometry:
                    # D4: unsigned everywhere.
                    risk_per_share = abs(sizing_price - stop_price)
                    if is_short:
                        # D8: gap-risk sizing haircut — SIZING ONLY, never
                        # stop placement (the stop above is untouched). A
                        # short gaps through its stop with no bound, so this
                        # execution-time vol-adjusted-sizing belt must be at
                        # least as conservative for a short as the
                        # constructor's own primary sizing already is.
                        _cfg = getattr(
                            getattr(pipeline.config, "risk", None),
                            "short_gap_risk_multiple", None,
                        )
                        gap_multiple = (
                            float(_cfg) if isinstance(_cfg, (int, float)) and _cfg > 1.0
                            else 1.5
                        )
                        risk_per_share *= gap_multiple
                    if risk_per_share > 0:
                        risk_dollars = total_value * RISK_BUDGET_PCT / 100
                        qty_by_risk = int(risk_dollars / risk_per_share)
                if qty_by_risk is not None and qty_by_risk < qty_by_alloc:
                    logger.info(
                        "Vol-adjusted sizing for %s: qty_by_alloc=%d → qty_by_risk=%d "
                        "(risk %.2f/share, budget $%.0f = %.1f%% of equity)",
                        decision.symbol, qty_by_alloc, qty_by_risk,
                        abs(sizing_price - stop_price),
                        total_value * RISK_BUDGET_PCT / 100, RISK_BUDGET_PCT,
                    )
                    qty = qty_by_risk
                else:
                    qty = qty_by_alloc
                if qty <= 0:
                    logger.warning("Calculated qty=0 for %s, skipping", decision.symbol)
                    _record_execution_skip(
                        pipeline, ctx, decision.symbol, "qty_zero",
                        f"allocation {decision.allocation_pct:.2f}% at "
                        f"${sizing_price:.2f} rounds to zero shares",
                    )
                    continue

                estimated_cost = qty * sizing_price
                # D11: opening a short is not gated by the tracked
                # `available_cash` pool — it does not spend settled cash the
                # way a BUY does (it sells borrowed shares), and the caps
                # (D9) plus the borrow gate (D6) are the sole control
                # surface for a short, not a cash re-size here.
                if not is_short and estimated_cost > available_cash:
                    affordable_qty = int(available_cash / sizing_price)
                    if affordable_qty <= 0:
                        logger.warning(
                            "Skipping BUY %s: estimated cost $%.2f exceeds available cash $%.2f after sell phase",
                            decision.symbol, estimated_cost, available_cash,
                        )
                        _record_execution_skip(
                            pipeline, ctx, decision.symbol, "insufficient_cash",
                            f"estimated cost ${estimated_cost:.2f} exceeds "
                            f"available cash ${available_cash:.2f}",
                        )
                        continue
                    logger.warning(
                        "Resizing BUY %s from %d to %d share(s): confirmed cash "
                        "$%.2f only partially covers the approved order",
                        decision.symbol, qty, affordable_qty, available_cash,
                    )
                    qty = min(qty, affordable_qty)
                    estimated_cost = qty * sizing_price
                    _record_pipeline_event(
                        pipeline, ctx, decision.symbol, "funding", "resized",
                        "confirmed_cash_partially_funded_order",
                        approved_qty=qty_by_risk if qty_by_risk is not None and qty_by_risk < qty_by_alloc else qty_by_alloc,
                        resized_qty=qty,
                    )

                # Write-ahead intent: insert a pending row BEFORE calling
                # the broker. Closes the BUY-side phantom-fill window the
                # audit surfaced — pre-fix, submit_order could return
                # successfully and a SIGKILL before db.insert_trade left
                # the broker with an accepted order and the DB with no
                # row. _reconcile_fills queries by broker_order_id, so
                # there was no recovery path for the phantom. With the
                # pending row pre-inserted, even a crash mid-submit
                # leaves a fill_status='pending_submit' row the operator
                # (or a periodic cleanup) can reconcile against the
                # broker's order list.
                executed_price = limit_price if limit_price is not None else sizing_price
                # Phase 3.1 — pin the analyst's stated horizon and setup type to
                # the trade row at entry. Everything downstream that asks "is
                # this position on schedule?" must measure against THIS number,
                # not against the system's own rolling average hold time, which
                # shrinks every time the system sells early and thereby makes
                # the next position look stalled. None when the analysis is
                # missing (resume lanes, sweep buys): the reviewer then gets no
                # pace figure at all, which is correct — it never gets a
                # fabricated one.
                entry_analysis = next(
                    (a for a in (ctx.analyses or []) if a.symbol == decision.symbol),
                    None,
                )
                entry_side = "sell_short" if is_short else "buy"
                pending_row_id = pipeline.db.insert_trade(
                    symbol=decision.symbol, action=decision.action, qty=qty,
                    price=executed_price, reasoning=decision.reasoning, run_id=run_id,
                    stop_loss=stop_price, take_profit=decision.take_profit,
                    broker_order_id=None,
                    fill_status="pending_submit",
                    decision_id=decision_id,
                    expected_horizon_sessions=getattr(
                        entry_analysis, "expected_horizon_sessions", None,
                    ),
                    setup_type=getattr(entry_analysis, "setup_type", None),
                )

                try:
                    order = pipeline.broker.submit_order(
                        symbol=decision.symbol, qty=qty, side=entry_side,
                        limit_price=limit_price,
                        stop_loss_price=stop_price if stop_price > 0 else None,
                        reference_price=market_price,
                    )
                except Exception as e:
                    # Submit raised — broker may or may not have the
                    # order. Leave the row as 'pending_submit' so the
                    # next session's orphan sweep
                    # (_reconcile_orphan_pending_submits) can match it
                    # against broker activity by symbol + qty + time
                    # window. Audit 2026-05-27: a prior version called
                    # mark_trade_submit_failed here, but
                    # get_orphaned_pending_submits filters only
                    # fill_status='pending_submit' — flipping it to
                    # submit_failed silently HID the row from the
                    # recovery path it was supposed to be flagged for.
                    _record_pipeline_event(
                        pipeline, ctx, decision.symbol, "order", "submit_unknown",
                        "broker_submit_exception", detail=str(e),
                        trade_row_id=pending_row_id,
                    )
                    raise

                if not pipeline._order_accepted(order, decision.symbol, entry_side):
                    # Broker explicitly rejected (status != accepted/filled).
                    # Mark the pending row failed so it doesn't poison
                    # calibration as a "submitted" trade we never tracked.
                    # Distinct from the submit-raised case: here we KNOW
                    # the broker rejected, so there's no orphan to sweep.
                    pipeline.db.mark_trade_submit_failed(pending_row_id)
                    _record_pipeline_event(
                        pipeline, ctx, decision.symbol, "order", "rejected",
                        "broker_rejected", trade_row_id=pending_row_id, qty=qty,
                    )
                    _record_execution_skip(
                        pipeline, ctx, decision.symbol, "broker_rejected",
                        f"broker rejected {decision.action.lower()} {qty} @ "
                        f"{'limit $%.2f' % limit_price if limit_price else 'market'}",
                    )
                    continue

                # Submit accepted — finalize the pending row with the
                # broker's order_id and flip to 'submitted'.
                pipeline.db.confirm_trade_submitted(
                    pending_row_id, broker_order_id=order.get("id"),
                )
                _record_pipeline_event(
                    pipeline, ctx, decision.symbol, "order", "submitted",
                    "broker_accepted", broker_order_id=order.get("id"), qty=qty,
                    limit_price=executed_price,
                )
                if isinstance(order, dict):
                    order.setdefault("action", decision.action)  # audit F5
                orders.append(order)
                if not is_short:
                    # D11: a SHORT does not spend `available_cash` — see the
                    # matching skip on the affordability re-size above.
                    available_cash -= estimated_cost
                order_type = "limit" if limit_price is not None else "market"
                logger.info(
                    "Executed: %s %d %s @ %s $%.2f",
                    decision.action.lower(), qty, decision.symbol, order_type, executed_price,
                )
                # The entry still owes a protective stop: it is placed as a
                # separate GTC order AFTER the fill, because an OTO leg would
                # inherit the parent's DAY tif and be expired by the broker at
                # 16:00 ET the same day (2026-07-16 audit — positions were
                # naked every night). Deferred until all BUYs are submitted so
                # the fill waits don't serialize the submission burst.
                if isinstance(order, dict) and order.get("pending_stop_price"):
                    pending_entry_stops.append({
                        "symbol": decision.symbol,
                        "side": entry_side,
                        "order_id": order.get("id"),
                        "stop_price": order["pending_stop_price"],
                        "qty": qty,
                        # Carried for the bounded re-peg (off by default).
                        # `reference_price` is the verified reference the
                        # slippage ceiling was computed from at SUBMISSION —
                        # the re-peg re-uses it rather than re-deriving a
                        # ceiling from a fresh quote, because a ceiling that
                        # follows the market is not a ceiling.
                        "reference_price": market_price,
                        "limit_price": limit_price,
                        "trade_row_id": pending_row_id,
                    })
            except Exception as e:
                logger.error("Order failed for %s %s: %s", decision.action, decision.symbol, e)

        # Protect every filled entry (GTC stop-limit keyed to the ACTUAL fill).
        for spec in pending_entry_stops:
            if not spec.get("order_id"):
                continue
            try:
                # Bounded re-peg FIRST, protection second, always. The chase
                # may hand back a different order id (Alpaca mints one per
                # replacement) plus any shares an ancestor order filled; both
                # feed straight into the stop so no filled share is left
                # without one. With `execution.repeg_enabled` off — the
                # default — this returns the same id and 0.0 without making a
                # single broker call.
                try:
                    entry_order_id, superseded_fill = _repeg_entry_order(
                        pipeline, ctx, spec,
                    )
                except Exception as repeg_exc:  # noqa: BLE001
                    # Protection must run even if the chase blows up. Fall
                    # back to the original id: at worst the re-peg did
                    # nothing, which is the failure direction we want.
                    logger.error(
                        "re-peg raised for %s: %s — protecting the ORIGINAL "
                        "order %s unchanged", spec["symbol"], repeg_exc,
                        spec["order_id"],
                    )
                    entry_order_id, superseded_fill = spec["order_id"], 0.0
                entry_side = spec.get("side", "buy")
                protection = pipeline.broker.place_entry_protection(
                    symbol=spec["symbol"], order_id=entry_order_id,
                    stop_price=spec["stop_price"], requested_qty=spec["qty"],
                    superseded_filled_qty=superseded_fill,
                    side=entry_side,
                )
                _record_pipeline_event(
                    pipeline, ctx, spec["symbol"], "protection",
                    "placed" if protection else "not_placed",
                    "protective_stop_result",
                    entry_order_id=entry_order_id, stop_price=spec["stop_price"],
                    protective_order_id=(protection or {}).get("id") if isinstance(protection, dict) else None,
                )
                # D7 (Stage 3): MANDATORY escalation for a SHORT. A long's
                # loss is bounded at -100%; a naked short's is not, so
                # relying on the next session's coverage-reconcile belt (the
                # long behaviour, unchanged above) is not an acceptable
                # exposure window here. If the protective stop could not be
                # placed after the entry actually filled shares, submit an
                # IMMEDIATE market COVER for the filled quantity and log it
                # loudly — this is not a normal exit, it is damage control.
                if protection is None and entry_side == "sell_short":
                    try:
                        fill_info = pipeline.broker.get_order_fill_info(entry_order_id) or {}
                        filled_qty = float(fill_info.get("filled_qty") or 0)
                    except Exception as fill_exc:  # noqa: BLE001
                        logger.critical(
                            "SHORT %s: could not even determine the filled "
                            "quantity after protection failed (%s) — treating "
                            "as the full requested qty %.4f to force a cover "
                            "attempt rather than leaving a possibly-naked "
                            "short untouched",
                            spec["symbol"], fill_exc, spec["qty"],
                        )
                        filled_qty = float(spec.get("qty") or 0)
                    if filled_qty > 0:
                        logger.critical(
                            "SHORT %s: PROTECTIVE STOP FAILED after %.4f "
                            "share(s) filled — a naked short has UNBOUNDED "
                            "loss. Submitting an IMMEDIATE market COVER "
                            "instead of waiting for the next reconcile pass.",
                            spec["symbol"], filled_qty,
                        )
                        try:
                            cover_order = pipeline.broker.submit_order(
                                symbol=spec["symbol"], qty=filled_qty, side="buy",
                            )
                            cover_id = (
                                cover_order.get("id")
                                if isinstance(cover_order, dict) else None
                            )
                            pipeline.db.insert_trade(
                                symbol=spec["symbol"], action="EMERGENCY_COVER",
                                qty=filled_qty, price=0.0,
                                reasoning=(
                                    "protective stop failed to place after a "
                                    "SHORT entry filled — immediate market "
                                    "cover to bound an otherwise naked short"
                                ),
                                run_id=run_id, broker_order_id=cover_id,
                                fill_status="submitted",
                            )
                            _record_pipeline_event(
                                pipeline, ctx, spec["symbol"], "protection",
                                "emergency_cover", "naked_short_protection_failed",
                                qty=filled_qty, broker_order_id=cover_id,
                            )
                        except Exception as cover_exc:  # noqa: BLE001
                            logger.critical(
                                "SHORT %s: EMERGENCY COVER ALSO FAILED (%s) — "
                                "%.4f share(s) are NAKED SHORT with NO "
                                "protective stop and NO cover in flight. "
                                "REQUIRES IMMEDIATE OPERATOR INTERVENTION.",
                                spec["symbol"], cover_exc, filled_qty,
                            )
                            _record_pipeline_event(
                                pipeline, ctx, spec["symbol"], "protection",
                                "emergency_cover_failed",
                                "naked_short_no_protection_no_cover",
                                qty=filled_qty, detail=str(cover_exc),
                            )
            except Exception as e:  # noqa: BLE001 — never abort the session here
                logger.error(
                    "entry protection raised for %s: %s — position may be "
                    "unprotected until the next coverage reconcile",
                    spec["symbol"], e,
                )
                _record_pipeline_event(
                    pipeline, ctx, spec["symbol"], "protection", "failed",
                    "protective_stop_exception", detail=str(e),
                    entry_order_id=spec["order_id"],
                )

        ctx.orders = orders
        return orders
