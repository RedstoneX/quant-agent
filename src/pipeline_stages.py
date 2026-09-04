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
import math
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from typing import TYPE_CHECKING

from src.agents.base import agent_log_kwargs
from src.agents.portfolio_manager import PortfolioManagerAgent
from src.cost_circuit import PaidAnalysisSuspended
from src.data.macro import MacroCoverage
from src.data.event_calendar import (
    EventCalendarCoverage, FOMCCoverage, fetch_earnings_proximity,
    format_event_risk_block,
)
from src.data.technical import compute_indicators
from src.models import (
    NewsIntelligenceReport, Nomination, TechAnalysisResult, TechnicalIndicators,
    parse_telemetry, reward_to_risk,
)
from src.nominations import select_nominations
from src.portfolio_constructor import LEVEL_BACKED_STOP_RULES
from src.pipeline_context import RunContext
from src.risk.constants import REWARD_RISK_FLOOR, STARTER_POSITION_RISK_PCT

if TYPE_CHECKING:
    from src.agents.earnings_analyst import EarningsAnalystAgent
    from src.agents.macro_analyst import MacroAnalystAgent
    from src.agents.news_analyst import NewsAnalystAgent
    from src.agents.tech_analyst import TechAnalystAgent
    from src.agents.smart_money_analyst import SmartMoneyAnalystAgent
    from src.data.smart_money import SmartMoneySource
    from src.config import AppConfig
    from src.data.earnings import EarningsDataProvider
    from src.data.event_calendar import (
        FOMCCalendarProvider, MacroEventCalendarProvider,
    )
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

#: Share of resolved tech analyses (or of the fetch universe, for the bars
#: variant) coming back with NO usable signal — empty `computed_levels`, or
#: no bars at all — above which a run is treated as a DATA FAILURE rather
#: than a quiet market, and pushed to the owner outside the session summary.
#: 1.0 is the unambiguous case: literally every symbol came back empty. That
#: can never be a legitimate market reading — `find_structural_levels`
#: refusing on EVERY name in one run means the thing that varies per-symbol
#: (each symbol's own chart) produced the same null result, which is a
#: property of the feed, not of 60-100 unrelated instruments.
LEVELS_BLIND_RUN_EMPTY_SHARE = 1.0

#: 0.5 is deliberately coarse, NOT a fitted percentile — see the long
#: comment on `_persist_levels_coverage` for why only one clean baseline run
#: exists to derive it from (2026-09-02: 1/64 resolved symbols, 1.6%, came
#: back with no computed level) and why n=1 cannot support a statistically
#: fit threshold. 50% sits roughly 30x that single observed baseline, far
#: above anything ordinary per-symbol noise (a thin IPO, a rangebound name)
#: should ever produce across a whole run, while still catching a partial
#: outage — a feed serving short/stale history to MOST but not literally
#: ALL requests — that the 1.0 rule alone would miss.
LEVELS_DEGRADED_RUN_EMPTY_SHARE = 0.5

#: Below this many resolved symbols (or fetch attempts), a share is noise —
#: one empty result out of three is 33% and means nothing. Production's
#: universe is 100+ symbols; this only guards small/degenerate universes
#: (tests, a misconfigured owner universe) from a false alarm, and is well
#: below anything the real desk runs.
LEVELS_COVERAGE_MIN_SAMPLE = 10


def _macro_regime(macro_analysis) -> str | None:
    """The regime string, from either a MacroAnalysis or a carried-forward dict."""
    if macro_analysis is None:
        return None
    if isinstance(macro_analysis, dict):
        value = macro_analysis.get("regime")
    else:
        value = getattr(macro_analysis, "regime", None)
    return str(value) if value else None


def _session_gross_ceiling(pipeline, ctx):
    """Spec §11.2 — this session's ladder-resolved gross-exposure ceiling.

    The run preamble already resolved it from account state before any agent
    ran; this re-derives it so the resume lane (where the preamble did not
    run) sizes against a real ceiling too. Returns None on any failure — the
    constructor then falls back to the standing cap, which is still a
    ceiling. It never falls back to "no ceiling".
    """
    resolve = getattr(pipeline, "_resolve_gross_ceiling", None)
    if resolve is None:
        return None
    try:
        from src.risk.rules import GrossCeiling
        ceiling = resolve(ctx)
        return ceiling if isinstance(ceiling, GrossCeiling) else None
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "§11.2: could not resolve the gross-exposure ceiling for sizing; "
            "the constructor falls back to the standing cap: %s", exc,
        )
        return None


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

    The same reasoning applies when only ONE of the two fails: this can also
    return `(None, clusters)` — heat missing, correlation clusters present —
    when `facts.heat` is `None` or building the per-symbol map raises.
    `clusters` on its own says nothing about what the book currently holds,
    so the caller (`PortfolioConstructor._plan_risk_targets`) must treat a
    missing `existing` the same as a missing pair and leave the ceilings
    unenforced rather than run the allocator against a book it wrongly
    presumes to be empty (2026-09-03 incident — see
    `docs/INCIDENT_HISTORY.md`).
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


def _check_levels_coverage(db: "Database", ctx: RunContext,
                            analyses: list["TechAnalysisResult"]) -> None:
    """Record this run's structural-level coverage; alert if it looks blind.

    2026-09-02, closing a hole found while checking whether 2026-09-01's
    zero-trade day could recur through a silent data failure. It could not
    have BEEN that day: this run's own persisted evidence shows
    `TechAnalysisResult.computed_levels` did not exist in the code that ran
    that morning (every one of that day's 59 tech_analyst evidence rows
    lacks the key entirely, not just an empty list), and the true cause was
    the R/R-geometry defect in docs/OUTCOME.md — a widened ATR stop dividing
    into a target that was never derived from structure, unrelated to
    whether structure existed. **Do not cite this function as what happened
    2026-09-01.**

    What IS true: the fix for that defect, shipped the same night, made
    `computed_levels` load-bearing. `derive_structural_target`
    (src/data/levels.py) now hard-refuses any trade when it is empty
    (REFUSAL_NO_STRUCTURE) — correctly; a stop needs a level to sit on. But
    empty-because-the-bar-feed-is-dead and empty-because-the-chart-really-
    has-no-structure produce the identical `[]`, and until now neither this
    run's per-symbol coverage nor its bars-fetch success was kept anywhere
    a postmortem could read after the fact — only a per-symbol WARNING log
    line for the latter, gone at the next log rotation, and the former not
    even that.

    `analyses` is scoped to RESOLVED symbols only (never-resolved symbols —
    an LLM parse failure — are `data_status["tech"]` partial/failed and the
    `analysis_parse_loss` advisory's job already; mixing that failure mode
    into a levels-coverage number would double-count it under a misleading
    label). The two share thresholds below are module constants —
    `LEVELS_BLIND_RUN_EMPTY_SHARE` / `LEVELS_DEGRADED_RUN_EMPTY_SHARE` — see
    their own comments for how they were derived and why 50% is a coarse
    line, not a fitted percentile.

    Best-effort and NEVER raises or blocks the research stage, matching
    `_persist_evidence`'s contract (which this calls): a coverage-tracking
    bug must never be able to stop a trading session, whatever it decides
    about alerting.
    """
    try:
        bars = ctx.tech_bars_coverage or {}
        universe = int(bars.get("universe") or 0)
        bars_missing = int(bars.get("bars_missing") or 0)

        resolved = len(analyses)
        levels_empty_symbols = sorted(
            a.symbol for a in analyses if not a.computed_levels
        )
        levels_empty = len(levels_empty_symbols)

        import json as _json
        _persist_evidence(
            db, run_id=ctx.run_id, agent_name="tech_analyst",
            kind="levels_coverage", scope="run",
            evidence_json=_json.dumps({
                "universe": universe,
                "bars_fetched": int(bars.get("bars_fetched") or 0),
                "bars_missing": bars_missing,
                "bars_missing_symbols": bars.get("bars_missing_symbols") or [],
                "resolved": resolved,
                "levels_present": resolved - levels_empty,
                "levels_empty": levels_empty,
                "levels_empty_symbols": levels_empty_symbols,
            }, sort_keys=True),
        )

        blind, degraded = [], []
        if universe >= LEVELS_COVERAGE_MIN_SAMPLE:
            share = bars_missing / universe
            if share >= LEVELS_BLIND_RUN_EMPTY_SHARE:
                blind.append(
                    f"bar fetch returned NOTHING for all {universe} "
                    f"universe symbol(s) — the data feed, not the market, "
                    f"is down"
                )
            elif share >= LEVELS_DEGRADED_RUN_EMPTY_SHARE:
                degraded.append(
                    f"bar fetch failed for {bars_missing}/{universe} "
                    f"universe symbols ({share:.0%})"
                )
        if resolved >= LEVELS_COVERAGE_MIN_SAMPLE:
            share = levels_empty / resolved
            if share >= LEVELS_BLIND_RUN_EMPTY_SHARE:
                blind.append(
                    f"every one of {resolved} analyzed symbol(s) came back "
                    f"with NO structural level — a live feed does not put "
                    f"every chart in a batch into that state at once"
                )
            elif share >= LEVELS_DEGRADED_RUN_EMPTY_SHARE:
                degraded.append(
                    f"{levels_empty}/{resolved} analyzed symbols came back "
                    f"with NO structural level ({share:.0%})"
                )
        if not (blind or degraded):
            return

        from src import notifier as _notifier
        header = "🔴 TECH DATA BLIND SPOT\n" if blind else "🟠 TECH DATA DEGRADED\n"
        _notifier.send_owner_alert(
            header + "; ".join(blind + degraded) + ".\n"
            "Every trade needs a structural level to set a stop against, so "
            "today's refusals may be a dead data feed wearing the costume "
            "of a quiet market rather than a genuine absence of setups. "
            "Check the market data provider before trusting a no-trade day."
        )
    except Exception as exc:  # noqa: BLE001
        logger.error("levels-coverage check failed: %s", exc)


def _fractional_sizing_allowed(pipeline, symbol: str, *, is_short: bool) -> bool:
    """Spec §11.1 — may THIS symbol be sized in fractional shares right now?

    Two independent gates, both of which must say yes:

    1. `execution.fractional_enabled` (default True). The owner's switch, so
       the feature can be turned off without a code change.
    2. The BROKER confirms `fractionable` for the symbol. A config flag says
       what the desk wants; only the asset directory says what Alpaca will
       accept. An unknown or failed lookup is a NO — fail closed, never
       fractional-by-assumption.

    A SHORT is always whole-share regardless: a fractional share cannot be
    borrowed, so this is not a policy choice to expose.

    Any unexpected failure in here returns False. The fallback (whole shares)
    is the behaviour that shipped for months; there is no failure mode of
    this function that should be allowed to stop a trade.
    """
    if is_short:
        return False
    try:
        execution_cfg = getattr(pipeline.config, "execution", None)
        if not bool(getattr(execution_cfg, "fractional_enabled", False)):
            return False
        info = pipeline.broker.get_fractionability(symbol)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "fractional eligibility check failed for %s (%s) — sizing in "
            "WHOLE shares (fail closed)", symbol, exc,
        )
        return False
    if not isinstance(info, dict) or not info.get("fractionable"):
        reason = (
            info.get("reason", "unknown") if isinstance(info, dict) else "unknown"
        )
        logger.info(
            "fractional sizing NOT available for %s (%s) — whole shares",
            symbol, reason,
        )
        return False
    return True


def _size_shares(pipeline, raw_qty: float, *, fractional: bool) -> float:
    """Turn a raw, real-valued share count into an ORDERABLE quantity.

    Whole-share mode floors to an integer — the behaviour this desk has
    always had, and the silent constant tax §11.1 exists to remove (a request
    for 6% of the book delivered 3.84%).

    Fractional mode floors to `execution.fractional_share_decimals` places.
    FLOORS, never rounds: rounding up would spend a sliver more risk budget
    than the sizing math actually allowed, and a sizing rule that can exceed
    its own budget by any amount is not a budget. The residual left on the
    table is under a tenth of a cent of notional.
    """
    try:
        value = float(raw_qty)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(value) or value <= 0:
        return 0.0
    if not fractional:
        return float(int(value))
    try:
        decimals = int(getattr(
            getattr(pipeline.config, "execution", None),
            "fractional_share_decimals", 4,
        ))
    except (TypeError, ValueError):
        decimals = 4
    decimals = min(max(decimals, 1), 9)
    scale = 10 ** decimals
    return math.floor(value * scale) / scale


def _fmt_shares(qty: float) -> str:
    """Render a share count for a human without a spurious `.0` on a whole
    number or a wall of trailing zeros on a fractional one."""
    try:
        value = float(qty)
    except (TypeError, ValueError):
        return str(qty)
    if value.is_integer():
        return str(int(value))
    return f"{value:.9f}".rstrip("0").rstrip(".")


# Spec §11.1 vol-adjusted sizing budget: the fraction of EQUITY a single
# entry may put at risk between its fill and its stop.
#
# HISTORICAL NOTE (item 22, 2026-09-03 audit): this used to be a hardcoded
# module constant, `RISK_BUDGET_PCT = 0.5`, predating the owner-ratified 5%
# envelope (`config.risk.max_position_risk_pct`, decided 2026-08-27). Nothing
# connected the two, so this independent recheck silently re-capped almost
# every entry at ten times less risk than the constructor had already sized
# it to under the real rule — confirmed against real NVDA/ORCL/RSG rows
# risking ~$49 on a ~$9.85k book where ~$490 was ratified. Fixed by reading
# the same config the constructor reads, the same defensive way
# `TradingPipeline.__init__`'s `_risk_setting` reads it for
# `ConstructorConfig.risk_budget_pct` — see `docs/INCIDENT_HISTORY.md`, "the
# risk manager and order-construction audit". The 5.0 fallback here is the
# ratified default, not an invented one.
_DEFAULT_RISK_BUDGET_PCT = 5.0


def _risk_budget_pct(pipeline) -> float:
    """The configured §11.1 risk-budget percentage, or the ratified default.

    Same Mock-safety posture as the `short_gap_risk_multiple` read just below
    in this function, and as `TradingPipeline.__init__`'s `_risk_setting`:
    a MagicMock config (common in tests) auto-creates a child attribute that
    is neither the default nor a real number, so it must be checked rather
    than trusted from a bare `getattr`.
    """
    raw = getattr(
        getattr(pipeline.config, "risk", None), "max_position_risk_pct", None,
    )
    if isinstance(raw, bool) or not isinstance(raw, (int, float)) or raw <= 0:
        return _DEFAULT_RISK_BUDGET_PCT
    return float(raw)


def _qty_by_risk_budget(pipeline, *, total_value: float, sizing_price: float,
                        stop_price: float, is_short: bool,
                        fractional: bool) -> float | None:
    """Shares the §11.1 risk budget allows, or None when geometry is unusable.

    ONE definition, two callers — the BUY-submit loop (which sizes the real
    order) and the cash-sweep preflight (which sizes the funding sale). They
    were separate before: the preflight funded the ALLOCATION notional while
    the submit loop spent `min(alloc, risk)`, so on every session where the
    risk budget bound — the ordinary case — the sweep liquidated more of the
    vehicle than the BUYs could possibly spend and the bookend re-parked the
    difference minutes later. Production, 2026-08-27: SWEEP_SELL $3,422.61 at
    13:35:43, SWEEP_BUY $1,007.60 at 13:36:36. Two crossings of the spread,
    53 seconds apart, for nothing.

    The preflight passes the RM-approved stop; the submit loop may later
    ATR-WIDEN that stop, which only increases risk-per-share and therefore
    only shrinks the final quantity. So the preflight's answer is an upper
    bound on what will be spent — funding still errs long, never short.

    This is a genuinely independent recheck, not a rubber stamp of the
    constructor's own number — it recomputes risk dollars from the REAL
    executed stop/entry geometry (which can differ from what the constructor
    assumed, e.g. after an ATR-widened stop or a marketable-limit price move)
    against the ratified percentage, in Python, rather than trusting the
    constructor's or PM's claimed ratio. Keep the mechanism; only the stale
    percentage was wrong (item 22).
    """
    if not (stop_price > 0 and sizing_price > 0):
        return None
    # D4: geometry validity is direction-aware — a long's stop must sit
    # below its entry, a short's strictly above.
    valid_geometry = (
        (not is_short and sizing_price > stop_price)
        or (is_short and stop_price > sizing_price)
    )
    if not valid_geometry:
        return None
    # D4: unsigned everywhere.
    risk_per_share = abs(sizing_price - stop_price)
    if is_short:
        # D8: gap-risk sizing haircut — SIZING ONLY, never stop placement
        # (the stop is untouched). A short gaps through its stop with no
        # bound, so this execution-time vol-adjusted-sizing belt must be at
        # least as conservative for a short as the constructor's own primary
        # sizing already is.
        _cfg = getattr(
            getattr(pipeline.config, "risk", None),
            "short_gap_risk_multiple", None,
        )
        gap_multiple = (
            float(_cfg) if isinstance(_cfg, (int, float)) and _cfg > 1.0
            else 1.5
        )
        risk_per_share *= gap_multiple
    if risk_per_share <= 0:
        return None
    risk_dollars = total_value * _risk_budget_pct(pipeline) / 100
    return _size_shares(
        pipeline, risk_dollars / risk_per_share, fractional=fractional,
    )


def _min_order_usd(pipeline) -> float:
    """The §10.3 notional floor — the smallest order worth placing.

    Read from `cash_sweep.min_order_usd` exactly as `apply_gross_ceiling`'s
    caller (`TradingPipeline._enforce_gross_ceiling`) and the constructor
    read it, so the floor that refuses a token order in the risk engine is
    the same number that refuses one after the execution-time cash re-size.
    An unreadable config falls back to the shared 500.0 default rather than
    to zero: a floor that silently becomes "no floor" is the defect.
    """
    raw = getattr(
        getattr(getattr(pipeline, "config", None), "cash_sweep", None),
        "min_order_usd", None,
    )
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return 500.0
    value = float(raw)
    if not math.isfinite(value) or value < 0:
        return 500.0
    return value


# --- Spec §11.2 — the EXECUTION-time deployment budget --------------------
#
# THE DEFECT THIS REPLACES (2026-09-02, the morning margin was switched on).
# The BUY submit loop clamped every entry against `available_cash`, seeded
# from the broker's RAW CASH figure, and that clamp was gated on NOTHING.
# Raw cash is at most `equity - gross`, so the arithmetic held gross below
# 1.0x equity STRUCTURALLY: however high `risk.max_gross_exposure_x` was
# set, a long could never cost more than settled money, and the §11.2
# ceiling could never become the binding constraint on the long side.
# `allow_margin: true` shipped that morning and changed nothing a long
# could do. Shorts were exempt (D11) and so were unaffected either way.
#
# THE CLAMP IS NOT REMOVED, and deleting it was considered and rejected.
# With margin enabled the broker ACCEPTS a buy that exceeds cash, so this
# loop is the last quantitative bound before the order leaves the building;
# with no clamp a batch of entries is bounded by nothing this side of
# Alpaca's own 4x. What changes is WHICH number is clamped against: the
# ladder-resolved gross headroom, so the de-levering ladder is the one
# number that governs how much the desk deploys.
#
# Fail-closed in all three degraded directions, because this gate is last:
#   - ladder unreadable  -> raw settled cash, i.e. exactly the pre-margin
#     behaviour. NEVER the standing 2.0x cap. The constructor may fall back
#     to the standing cap because another gate still runs after it; nothing
#     runs after this one.
#   - equity unusable    -> zero budget. `_resolve_gross_ceiling` already
#     forces the ladder's FLOOR rung on a non-finite equity read (guard 2,
#     2026-09-02) and alerts the owner; multiplying that rung by a NaN
#     equity would produce a NaN budget, every `>` comparison against it
#     would be False, and the clamp would silently grant INFINITE room on
#     precisely the broken-snapshot morning the guard exists for.
#   - park symbol unreadable -> parked cash counts as gross, which shrinks
#     the headroom rather than inflating it.
def _entry_deployment_budget(pipeline, ctx, positions, equity, cash):
    """Dollars of NEW entry notional this session may still add.

    Returns `(budget_usd, ladder_backed, note)`. `ladder_backed` says which
    of the two meanings the number carries, and the submit loop needs it:
    a ladder budget is GROSS headroom, which a short consumes as surely as
    a long does, while the cash fallback is a settled-cash pool, which a
    short does not draw on at all (D11).
    """
    from src.risk.rules import gross_exposure

    ceiling = _session_gross_ceiling(pipeline, ctx)
    if ceiling is None:
        logger.warning(
            "§11.2: the gross-exposure ceiling could not be resolved for the "
            "submit loop — falling back to the pre-margin raw-cash clamp "
            "($%.2f). Entries are bounded by settled cash this session, not "
            "by the ladder.", float(cash) if isinstance(cash, (int, float)) else 0.0,
        )
        usable_cash = (
            float(cash)
            if isinstance(cash, (int, float)) and not isinstance(cash, bool)
            and math.isfinite(float(cash))
            else 0.0
        )
        return max(0.0, usable_cash), False, "raw settled cash (ladder unreadable)"

    if (isinstance(equity, bool) or not isinstance(equity, (int, float))
            or not math.isfinite(float(equity)) or float(equity) <= 0):
        logger.warning(
            "§11.2: equity read is unusable (%r) — refusing every new entry "
            "this session rather than sizing a budget against it. The ladder "
            "is already at its floor rung (%.1fx) for the same reason.",
            equity, ceiling.ceiling_x,
        )
        return 0.0, True, "no usable equity read — no new entry permitted"

    equity = float(equity)
    # The park vehicle is parked cash, not exposure — the same exclusion
    # `gross_exposure` is given everywhere else it is called. An unreadable
    # symbol falls through to None, which counts the vehicle as gross and so
    # UNDER-states the headroom; that is the safe side to be wrong on.
    try:
        park = pipeline._sweep_symbol()
    except Exception as e:  # noqa: BLE001
        logger.warning("§11.2: cash-park symbol unreadable (%s) — counting "
                       "parked cash as gross for the budget", e)
        park = None
    if not isinstance(park, str):
        park = None
    # No non-finite guard on the total: `gross_exposure` SKIPS a non-finite
    # `market_value` rather than propagating it, so this sum cannot come back
    # NaN. `unmeasurable_gross_symbols` is the guard against acting on a
    # total that quietly excluded a position, and it is the pre-trade gate's
    # job — a BUY carrying one is already hard-blocked before this loop.
    held_gross = gross_exposure(positions, cash_park_symbol=park)

    # Floored at zero. A book already ABOVE its rung has negative headroom,
    # and a negative budget is not a smaller budget: it would render to the
    # operator as "-$500 still deployable" and would silently eat the first
    # $500 of any credit a later refresh brought in.
    headroom = max(0.0, ceiling.ceiling_x * equity - held_gross)
    note = (
        f"§11.2 ladder headroom ${headroom:,.2f} "
        f"({ceiling.ceiling_x:.2f}x x ${equity:,.0f} equity "
        f"- ${held_gross:,.0f} held gross, rung {ceiling.rung})"
    )

    # `is True`, not `bool(...)`: a MagicMock config attribute is truthy, and
    # reading a stub as "margin enabled" would hand a test pipeline a levered
    # budget it was never meant to have. Only a real `True` unbinds cash.
    # RiskConfig.allow_margin is pydantic-typed `bool`, so production is
    # unaffected by the stricter read.
    allow_margin = getattr(
        getattr(getattr(pipeline, "config", None), "risk", None),
        "allow_margin", False,
    ) is True
    if not allow_margin:
        usable_cash = (
            float(cash)
            if isinstance(cash, (int, float)) and not isinstance(cash, bool)
            and math.isfinite(float(cash))
            else 0.0
        )
        usable_cash = max(0.0, usable_cash)
        if usable_cash < headroom:
            note = (
                f"raw settled cash ${usable_cash:,.2f} (margin disabled; "
                f"tighter than the {ceiling.ceiling_x:.2f}x ladder headroom "
                f"${headroom:,.2f})"
            )
        headroom = min(headroom, usable_cash)
    return headroom, True, note


def _single_name_execution_cap(pipeline, equity: float) -> float:
    """`max_position_pct` of equity, re-applied to the size EXECUTION chose.

    Same idiom as the §10.3 minimum-notional floor a few lines below the
    clamp: the gate upstream already caps a single name, and execution only
    ever shrinks what the gate approved, so in the ordinary lane this is
    redundant. It is here because the budget above is a POOL — one order
    could otherwise draw the entire session's ladder headroom — and because
    the resume lane reaches this loop without the pre-trade gate having run.
    Redundant and local beats correct-only-if-another-file-ran.

    Falls back to the configured default (20) rather than to "no cap" when
    the setting is unreadable, and to zero on an unusable equity figure —
    the same fail-closed direction as the budget.
    """
    if (isinstance(equity, bool) or not isinstance(equity, (int, float))
            or not math.isfinite(float(equity)) or float(equity) <= 0):
        return 0.0
    raw = getattr(
        getattr(getattr(pipeline, "config", None), "risk", None),
        "max_position_pct", None,
    )
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        pct = 20.0
    else:
        pct = float(raw)
        if not math.isfinite(pct) or pct <= 0:
            pct = 20.0
    return float(equity) * pct / 100.0


def _alert_owner_protection_failed(pipeline, spec: dict, protection,
                                   entry_order_id: str) -> None:
    """Spec §11.1 guard 2 — a stop that did not land ALERTS THE OWNER.

    Fires on two states, and says which:

      * no stop at all — `place_entry_protection` exhausted its retries
        (guard 1) and returned nothing;
      * a partial cover — the broker took a stop for fewer shares than are
        held (today: the whole-share fallback for a fractional fill whose
        exact quantity the broker refused).

    Silent on the third state — protection placed, nothing uncovered — which
    is the overwhelmingly common one. An alert channel that fires on success
    is a channel the owner learns to swipe away.

    Deliberately does NOT fire when the entry filled zero shares: there is no
    position, so there is nothing to protect, and `place_entry_protection`
    returns None for that too. Waking a human for a BUY that simply did not
    fill is exactly how guard 2 gets turned off.

    Never raises.
    """
    try:
        symbol = spec.get("symbol", "?")
        stop_price = spec.get("stop_price")
        uncovered = 0.0
        if isinstance(protection, dict):
            try:
                uncovered = float(protection.get("uncovered_qty") or 0)
            except (TypeError, ValueError):
                uncovered = 0.0
            if uncovered <= 0:
                return
        if protection is None:
            # Distinguish "no stop" from "no fill". Only the first is an
            # emergency; the second is a normal, uneventful non-event.
            filled = None
            try:
                info = pipeline.broker.get_order_fill_info(entry_order_id) or {}
                filled = float(info.get("filled_qty") or 0)
            except Exception:  # noqa: BLE001
                filled = None
            if filled is not None and filled <= 0:
                return
            held = _fmt_shares(filled) if filled is not None else "an unknown number of"
            is_short = str(spec.get("side", "buy")).lower() != "buy"
            remedy = (
                "An IMMEDIATE market cover is being submitted — a naked short "
                "has unbounded loss and is not left to a sweep. Confirm it "
                "landed."
                if is_short else
                "Place a stop manually or flatten the position. The 30-minute "
                "coverage sweep will also attempt an automatic repair."
            )
            body = (
                "🛑🛑🛑 NO STOP AT ALL\n"
                f"{symbol}: the entry filled ({held} share(s)) but the "
                "protective stop could not be placed after every immediate "
                "retry. The position is open at the broker with NOTHING "
                "standing watch.\n"
                f"Intended stop: {stop_price}\n"
                f"Entry order: {entry_order_id}\n"
                f"{remedy}"
            )
        else:
            covered = protection.get("covered_qty")
            body = (
                "⚠️ STOP PARTIALLY COVERS THE POSITION\n"
                f"{symbol}: a protective stop was placed for "
                f"{_fmt_shares(covered)} share(s), but {_fmt_shares(uncovered)} "
                "share(s) of the fill are NOT covered by it — the broker "
                "refused a stop for the exact filled quantity.\n"
                f"Stop: {stop_price}\n"
                f"Entry order: {entry_order_id}\n"
                "The uncovered remainder is under one share. If this recurs, "
                "turn `execution.fractional_enabled` off."
            )
        from src import notifier as _notifier

        _notifier.send_owner_alert(body, symbols=[str(symbol)])
    except Exception as exc:  # noqa: BLE001
        logger.error("protection-failure owner alert failed: %s", exc)


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


def _link_nominations_to_decision(pipeline, ctx) -> None:
    """Spec §9.5 — close the nomination→decision join. NEVER raises.

    Nominations are recorded during MorningResearchStage, where
    `ctx.decision_id` is still None: the id is not minted until DecisionStage
    mints it from a successful PM call. Every nomination row therefore landed
    with decision_id NULL, and nothing connected a nomination to the trade it
    became.

    This back-fills the id onto those rows the moment it exists. It is an
    UPDATE on the forensic evidence table and nothing more — no pipeline
    input, no ordering change, no new state read by any later stage. The
    alternative (deferring the nomination write until DecisionStage) would
    move a forensic write into the decision path and reorder it relative to
    the responder pass that acts on the same nominations; this does not.

    Best-effort by the same rule every other evidence write here follows: a
    persistence failure is a display gap, never a reason to alter or
    interrupt a decision.
    """
    if not getattr(ctx, "decision_id", None):
        return
    try:
        linked = pipeline.db.link_nominations_to_decision(
            run_id=ctx.run_id, decision_id=ctx.decision_id,
        )
        if linked:
            logger.info(
                "Conviction ledger: joined %d nomination row(s) to decision %s",
                linked, ctx.decision_id,
            )
    except Exception as e:  # noqa: BLE001
        logger.warning("Conviction ledger: nomination join failed: %s", e)


def _record_seat_stances(pipeline, ctx, evidence_registry, symbols) -> None:
    """Spec §9.5 — record who ARGUED AGAINST, not only who proposed. NEVER raises.

    §9.4 already computes each seat's stance per symbol into the canonical
    evidence registry, and counts only the ALIGNED ones to earn size. The
    opposing stances were computed and then discarded: nothing persisted
    "macro was underweight this name and the desk bought it anyway" in a form
    that could later be scored.

    So one `seat_stance` row per (idea, seat) is written from that same
    registry — support and dissent alike, no re-derivation, no second notion
    of what a stance is. Conviction comes from what the seat actually
    DECLARED: its nomination conviction where it nominated the symbol
    (`ctx.nomination_convictions`), Technical's own `conviction` field for the
    technical seat, and the neutral default where the schema offers none.

    `symbols` is the PM's target set — the ideas the desk actually decided on
    — not the whole registry, which would record a stance on every symbol
    merely covered this run.

    Purely additive: writes evidence rows, reads nothing back, returns
    nothing. No caller consumes its effect within the run.
    """
    if not getattr(ctx, "decision_id", None) or not evidence_registry:
        return
    try:
        from src.conviction_ledger import DEFAULT_CONVICTION, SeatStance, normalize_seat

        wanted = {str(s).strip().upper() for s in (symbols or []) if str(s).strip()}
        nominations = getattr(ctx, "nomination_convictions", None) or {}
        tech_conviction = {
            str(getattr(a, "symbol", "")).strip().upper():
                str(getattr(a, "conviction", "") or DEFAULT_CONVICTION)
            for a in (ctx.analyses or [])
        }
        stances: list[SeatStance] = []
        for symbol in sorted(wanted):
            for source, stance in sorted((evidence_registry.get(symbol) or {}).items()):
                seat = normalize_seat(source)
                declared = (nominations.get(symbol) or {}).get(seat) or {}
                conviction = declared.get("conviction")
                if not conviction and seat == "technical":
                    conviction = tech_conviction.get(symbol)
                stances.append(SeatStance(
                    seat=seat, symbol=symbol, stance=stance,
                    conviction=conviction or DEFAULT_CONVICTION,
                    nominated=bool(declared),
                    observation=str(declared.get("observation") or ""),
                ))
        if not stances:
            return
        pipeline.db.record_seat_stances(
            run_id=ctx.run_id, decision_id=ctx.decision_id, stances=stances,
        )
        logger.info(
            "Conviction ledger: recorded %d seat stance(s) across %d idea(s) "
            "for decision %s", len(stances), len(wanted), ctx.decision_id,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("Conviction ledger: seat-stance recording failed: %s", e)


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
        event_calendar: "MacroEventCalendarProvider | None" = None,
        fomc_calendar: "FOMCCalendarProvider | None" = None,
    ):
        self.config = config
        self.db = db
        self.market = market
        self.macro = macro
        # Optional so every existing construction site (tests, the
        # commissioning verifier) keeps working; when absent, the macro seat is
        # told the calendar was NOT FETCHED rather than being shown an empty
        # one it would read as "no events scheduled".
        self.event_calendar = event_calendar
        # Same optionality and the same reason: absent, the seats are told the
        # FOMC calendar was NOT FETCHED rather than shown an empty schedule
        # that reads as "no Fed decision coming".
        self.fomc_calendar = fomc_calendar
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
            # Side channel, not part of macro_summary's own shape — see
            # MacroCoverage's docstring (src/data/macro.py) for why
            # get_macro_summary() itself still returns a bare dict.
            macro_coverage = self.macro.last_coverage
            logger.info(
                "Macro data: VIX=%s, HY OAS=%sbps, CPI core YoY=%s, UNRATE=%s",
                macro_summary.get("vix", {}).get("current"),
                macro_summary.get("credit_spread", {}).get("current_bps"),
                macro_summary.get("inflation", {}).get("core_cpi_yoy"),
                macro_summary.get("unemployment", {}).get("current"),
            )
            # Forward calendar of scheduled macro releases. Fetched here, in
            # the same background worker as the macro summary, so it shares the
            # research fan-out's wall clock instead of adding to the critical
            # path — and its own hard deadline (event_risk.calendar_deadline_s)
            # bounds it independently. A failure NEVER propagates: the seat is
            # shown the coverage line and told the calendar is impaired, which
            # is the whole point of fetching it.
            macro_events: list = []
            event_coverage = None
            if self.event_calendar is not None:
                try:
                    macro_events = self.event_calendar.get_upcoming_events(
                        horizon_days=self.config.event_risk.horizon_days,
                    )
                    event_coverage = self.event_calendar.last_coverage
                except Exception as e:  # noqa: BLE001
                    logger.warning("Macro event calendar fetch failed: %s", e)
                    macro_events, event_coverage = [], None
            # FOMC meeting schedule, from the Fed's own free calendar. Fetched
            # in the same worker for the same reason, under its own deadline
            # (event_risk.fomc_deadline_s), and degrading the same way: the
            # seats read a named absence, never an empty schedule.
            fomc_meetings: list = []
            fomc_coverage = None
            if self.fomc_calendar is not None:
                try:
                    fomc_meetings = self.fomc_calendar.get_meetings(
                        horizon_days=self.config.event_risk.horizon_days,
                    )
                    fomc_coverage = self.fomc_calendar.last_coverage
                except Exception as e:  # noqa: BLE001
                    logger.warning("FOMC calendar fetch failed: %s", e)
                    fomc_meetings, fomc_coverage = [], None
            analysis, result = self.macro_analyst.analyze(
                macro_summary=macro_summary,
                universe=effective_symbols,
                last_state=prior_macro_state,
                news_narrative=news_narrative,
                macro_coverage=macro_coverage,
                macro_events=macro_events,
                event_coverage=event_coverage,
                event_horizon_days=self.config.event_risk.horizon_days,
                fomc_meetings=fomc_meetings,
                fomc_coverage=fomc_coverage,
            )
            if analysis:
                try:
                    self.macro_store.save_last_state(analysis.model_dump())
                except Exception as e:
                    logger.warning("Failed to persist macro last state: %s", e)
            return (
                macro_summary, analysis, result, macro_coverage,
                macro_events, event_coverage, fomc_meetings, fomc_coverage,
            )

        def _run_news():
            # Per-symbol news selection (2026-08-30 owner decision): held
            # positions first, then this run's admitted candidates — the
            # only run-scoped "active candidate" concept available BEFORE
            # news fetches (tech/nomination candidates don't exist yet; news
            # and tech run concurrently in this same fan-out). Both lists
            # are already in a stable, non-set order — ctx.positions is the
            # broker snapshot's own order, admitted_symbols is sorted()
            # rather than iterated as a raw set — so the selection is
            # reproducible in the offline rehearsal rig.
            held = [
                str(getattr(p, "symbol", "")).strip().upper()
                for p in ctx.positions if getattr(p, "qty", 0)
            ]
            held = [s for s in held if s]
            candidates = sorted(ctx.admitted_symbols)
            try:
                return self._run_news_update(
                    ctx.run_id, session="morning", universe=effective_symbols,
                    held_symbols=held, candidate_symbols=candidates,
                )
            except TypeError as exc:
                # Test doubles (and any future caller) may inject a
                # run_news_update_fn with a narrower signature than the real
                # method — this pre-dates per-symbol news (see the original
                # 'universe' fallback this generalizes). Any excess-kwarg
                # TypeError here can only come from the CALL SITE not
                # matching the injected callable's signature, never from
                # inside a correctly-implemented _run_news_update, so
                # retrying with the minimal 2-arg call is safe.
                if "unexpected keyword argument" not in str(exc):
                    raise
                return self._run_news_update(ctx.run_id, session="morning")

        def _run_tech():
            all_symbols_data = []
            symbols_bars: dict[str, list] = {}
            # Counted, not just logged (2026-09-02). "No data for %s,
            # skipping" used to be the ONLY trace a bar fetch ever failed —
            # gone the moment the log rotated, and never summed across the
            # run, so a feed outage that silently dropped every symbol read
            # exactly like a quiet market. See ctx.tech_bars_coverage and
            # `_check_levels_coverage` below for where this is used.
            bars_missing_symbols: list[str] = []
            for symbol in effective_symbols:
                bars = self.market.get_ohlcv(symbol, self.config.trading.lookback_days)
                if not bars:
                    logger.warning("No data for %s, skipping", symbol)
                    bars_missing_symbols.append(symbol)
                    continue
                indicators = compute_indicators(symbol, bars)
                all_symbols_data.append({"symbol": symbol, "bars": bars, "indicators": indicators})
                symbols_bars[symbol] = bars
            ctx.symbols_bars = symbols_bars
            ctx.tech_bars_coverage = {
                "universe": len(effective_symbols),
                "bars_fetched": len(all_symbols_data),
                "bars_missing": len(bars_missing_symbols),
                "bars_missing_symbols": bars_missing_symbols,
            }
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
            # A finish_reason=length truncation must never be indistinguishable
            # from "empty" (no signal found) or "ok" (clean run). The model can
            # still emit syntactically valid-but-incomplete JSON when cut off
            # mid-generation, which would otherwise silently pass through as
            # "ok"/"empty" above. Truncation always wins so operators can see
            # it separately from a genuine quiet day or a provider failure.
            if sm_result is not None and getattr(sm_result, "truncated", False):
                data_status["smart_money"] = "truncated"
        except Exception as e:
            logger.warning("Smart-money branch failed: %s", e)
            ctx.smart_money_provider_error = f"analysis_error:{type(e).__name__}"
            data_status["smart_money"] = "provider_error"

        # Macro
        #
        # Phase 4.2 fix: before this, `data_status["macro"]` was "ok" purely
        # on whether the LLM call parsed — a run where every FRED series
        # timed out (the 2026-08-26 17:01-17:03 UTC incident: all nine
        # series failed) still said "ok" as long as the macro analyst
        # produced valid JSON from all-None inputs. `macro_coverage`
        # (src.data.macro.MacroCoverage) is the deterministic half of the
        # fix, mirroring the 2026-08-28 news fix exactly: it reflects how
        # many of the configured FRED series actually returned data,
        # independent of whether the LLM call on top of them succeeded.
        # Coverage failure dominates parse success below.
        macro_coverage: "MacroCoverage | None" = None
        try:
            (
                macro_summary, macro_analysis, ma_result, macro_coverage,
                macro_events, event_coverage, fomc_meetings, fomc_coverage,
            ) = macro_future.result()
            # A test double / older caller may hand back something other
            # than a real MacroCoverage (e.g. a bare MagicMock attribute
            # off an unconfigured mock provider) — treat anything that
            # isn't the real dataclass as "coverage not reported" rather
            # than crashing json.dumps() below on non-serializable mock
            # internals. Mirrors how a coverage-less caller is already
            # handled (macro_coverage is None branch further down).
            if not isinstance(macro_coverage, MacroCoverage):
                macro_coverage = None
            # audit round 2: commit the analysis to ctx BEFORE the agent_logs
            # write — a DB lock/timeout on the log write used to discard a
            # fully successful macro run (ctx fields were assigned after it).
            ctx.macro_summary = macro_summary
            ctx.macro_analysis = macro_analysis
            ctx.macro_coverage = macro_coverage
            # Carried on ctx so RiskStage reuses this run's calendar instead of
            # re-fetching it (one FRED sweep per session, not two). Same
            # test-double guard as macro_coverage above: anything that is not
            # the real dataclass reads as "not fetched", which the renderer
            # states explicitly rather than showing as an empty calendar.
            if not isinstance(event_coverage, EventCalendarCoverage):
                event_coverage = None
                macro_events = []
            ctx.macro_events = list(macro_events or [])
            ctx.macro_event_coverage = event_coverage
            # Same test-double guard, same reason: anything that is not the
            # real dataclass reads as NOT FETCHED, which the renderer states
            # outright rather than showing as an empty FOMC schedule.
            if not isinstance(fomc_coverage, FOMCCoverage):
                fomc_coverage = None
                fomc_meetings = []
            ctx.fomc_meetings = list(fomc_meetings or [])
            ctx.fomc_coverage = fomc_coverage
            if event_coverage is not None and event_coverage.status != "ok":
                # Deliberately NOT written into `data_status`. Every key in
                # that dict feeds the `data_degraded` advisory's ">= 2 degraded
                # sources" arithmetic (below), and a release whose next date the
                # source agency has simply not published yet is a normal,
                # recurring "partial" — adding it would move an existing gate's
                # threshold as a side effect of this change. The seats are told
                # through their own event-risk block, which is where the fact
                # can actually be acted on; the operator gets this log line.
                logger.warning(
                    "Macro event calendar %s this run: %s",
                    event_coverage.status.upper(), event_coverage.describe(),
                )
            if fomc_coverage is not None and not fomc_coverage.measured:
                # Same reasoning as the line above — the seats are told in
                # their own block; this is the operator's copy. Kept out of
                # `data_status` so it cannot move the `data_degraded`
                # threshold as a side effect.
                logger.warning(
                    "FOMC calendar %s this run: %s",
                    fomc_coverage.status.upper(), fomc_coverage.describe(),
                )
            if macro_coverage is not None:
                _persist_evidence(
                    self.db, run_id=ctx.run_id, agent_name="macro_provider",
                    kind="coverage", scope="run",
                    evidence_json=__import__("json").dumps({
                        "configured": macro_coverage.configured,
                        "succeeded": macro_coverage.succeeded,
                        "failed": [
                            {"series_id": f.series_id, "reason": f.reason}
                            for f in macro_coverage.failed
                        ],
                        "status": macro_coverage.status,
                    }, sort_keys=True),
                )
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
                _persist_evidence(
                    self.db, run_id=ctx.run_id, agent_name="macro_analyst",
                    kind="analysis", scope="run",
                    evidence_json=macro_analysis.model_dump_json(),
                )
            # Coverage is authoritative over parse success: total FRED
            # failure means "failed" even if the model still emitted a
            # technically-valid report on all-None input. A coverage-less
            # result (macro_coverage is None — a caller/test double that
            # hasn't been updated to report it) falls back to the pre-fix
            # ok/parse_error split rather than crashing on a missing value.
            if macro_coverage is None:
                data_status["macro"] = "ok" if macro_analysis else "parse_error"
            elif macro_coverage.status == "failed":
                data_status["macro"] = "failed"
                logger.error(
                    "Macro coverage FAILED this run: %s", macro_coverage.describe(),
                )
            elif not macro_analysis:
                data_status["macro"] = "parse_error"
            elif macro_coverage.status == "partial":
                data_status["macro"] = "partial"
                logger.warning(
                    "Macro coverage PARTIAL this run: %s", macro_coverage.describe(),
                )
            else:
                data_status["macro"] = "ok"
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
        # Outside the try/except above on purpose: it must run whether tech
        # resolved cleanly, partially, or not at all — a bars outage severe
        # enough to crash the batch entirely is exactly the case this exists
        # to catch, not one to skip because the try block above already
        # failed. Never raises (see its own docstring), so it cannot turn a
        # degraded tech stage into a hard research-stage failure.
        _check_levels_coverage(self.db, ctx, analyses)

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
        # Parse-level losses are recorded ALONGSIDE data_status, not inside
        # it — same reasoning as `macro_coverage` in RunContext: data_status
        # carries the one-word summary per source, this carries the evidence
        # a single word cannot. Deliberately not a data_status key, because
        # every key in that dict moves the `data_degraded` advisory's ">= 2
        # degraded sources" arithmetic and this change must not shift an
        # existing gate's threshold as a side effect.
        #
        # This is the RESEARCH-stage reading, logged here so a postmortem can
        # tell a research-side loss from a PM-side one. RiskStage takes the
        # authoritative reading later, after the Portfolio Manager has also
        # parsed, and that is what reaches the advisory.
        if parse_telemetry.total_dropped():
            logger.error(
                "Analysis items DROPPED at parse during research (%d): %s — "
                "these candidates were researched and never reached the "
                "Portfolio Manager",
                parse_telemetry.total_dropped(), parse_telemetry.describe_dropped(),
            )
        if parse_telemetry.total_null_coercions():
            logger.warning(
                "Explicit nulls coerced to defaults during research (%d): %s — "
                "the objects survived, but the model said nothing where the "
                "prompt asked for something",
                parse_telemetry.total_null_coercions(),
                parse_telemetry.describe_null_coercions(),
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
        from src.conviction_ledger import normalize_seat as _normalize_seat
        for seat, noms in nominations_by_seat.items():
            for nomination in noms:
                _record_pipeline_event(
                    self, ctx, nomination.symbol, "opportunity", "nominated",
                    "research_seat_nomination", seat=seat,
                    conviction=nomination.conviction,
                    observation=nomination.observation,
                )
                # §9.5: keep what the seat DECLARED so DecisionStage can
                # RECORD it on the stance. It is a label, not a multiplier —
                # the conviction weight was removed on 2026-08-31 and the
                # ledger now reports each analyst's record broken down BY the
                # confidence it declared instead. Pure bookkeeping on the
                # context — no stage below reads this field to decide anything.
                ctx.nomination_convictions.setdefault(
                    nomination.symbol.strip().upper(), {},
                )[_normalize_seat(seat)] = {
                    "conviction": nomination.conviction,
                    "observation": nomination.observation,
                }

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
            ctx.macro_analysis, ctx.total_value, ctx.deployable_cash,
            ctx.last_equity

    `ctx.deployable_cash`, NOT `ctx.cash` — this stage sizes a plan, and the
    plan may spend the sweep vehicle because `fund_buys` converts it before
    the BUY phase. Raw broker cash here would hide the parked book from PM
    and RM and cap the desk at its reserve. The docstring said `ctx.cash`;
    the code has read `deployable_cash` since the 2026-08-19 tranche.
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
        # Names PM keeps proposing and never gets. Every other per-symbol
        # memory above is keyed on a position, so none of them can see a
        # symbol that never became one.
        blocked_proposals = pipeline._build_blocked_proposals()
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

        # Spec §2.2 — the book's EXISTING risk, before anything this session
        # proposes. Computed here (moved up from just before the constructor
        # call below, which still reuses this same pair) so the Phase 14
        # opportunity-rotation pre-check can see the same numbers the
        # constructor will later ration against, rather than a fabricated
        # "book is empty" view. Pure function of `ctx.facts` — safe to
        # compute this early since nothing between here and the constructor
        # call mutates it.
        existing_risk_pct, risk_clusters = _book_risk_inputs(ctx, total_value)

        # 2026-09-04 fix (audit finding): the PM's own eligibility gate
        # (`candidate_eligibility` / `_apply_subfloor_catalyst_rule`) used
        # to read `TechAnalysisResult.risk_reward` — real arithmetic, but
        # over the analyst's own GUESSED target, never checked against
        # structure. `construct_orders` below has computed the REAL
        # derived-target, noise-floor-widened reward:risk since 2026-09-01
        # (§12.1); this gate never got it. Measured on a real day, the two
        # gates passed DISJOINT eligible sets. Computed here, before the PM
        # decides, using the same `PortfolioConstructor` instance
        # `construct_orders` uses below — same config, same derivation, no
        # second copy of the logic. Necessarily a PREVIEW, not the final
        # number: entry is the analyst's snapshot, not the live price, and
        # the stop is the analyst's own, not yet a PM-suggested one — both
        # are only known at construction time. See
        # `PortfolioConstructor.real_reward_risk_preview`.
        _regime_for_preview = _macro_regime(macro_analysis)
        real_reward_risk_by_symbol: dict[str, float | None] = {}
        for _a in analyses:
            _direction = (
                "short" if _a.rating in ("sell", "strong_sell") else "long"
            )
            real_reward_risk_by_symbol[_a.symbol.upper()] = (
                pipeline.portfolio_constructor.real_reward_risk_preview(
                    _a, _direction, regime=_regime_for_preview,
                )
            )

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
            blocked_proposals=blocked_proposals,
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
            # The sub-floor catalyst gate reads the SAME two numbers the
            # deterministic risk layer downstream does, threaded rather than
            # re-defaulted: the PM must be gated on the floor the
            # constructor will actually enforce, and capped at the size
            # `allocate_risk_budget` will actually grant.
            rr_floor=float(getattr(
                pipeline.config.risk, "min_reward_risk_after_widening",
                REWARD_RISK_FLOOR,
            )),
            starter_risk_pct=float(getattr(
                pipeline.config.risk, "min_position_risk_pct",
                STARTER_POSITION_RISK_PCT,
            )),
            # Phase 14 (opportunity-cost rotation) — same book-risk snapshot
            # and ceiling the constructor rations against below, computed
            # once above so both stages judge the identical numbers.
            existing_risk_pct=existing_risk_pct,
            max_portfolio_risk_pct=float(getattr(
                pipeline.config.risk, "max_portfolio_risk_pct", 25.0,
            )),
            real_reward_risk_by_symbol=real_reward_risk_by_symbol,
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
        # Conviction ledger (spec §7.2): pm_result.model is the ACTUAL model
        # that answered (see RunContext.decision_model docstring), threaded
        # to ExecutionStage regardless of whether this call ultimately
        # produced a valid decision — a failed/unparseable PM call still
        # carries no trades, so an unused decision_model is harmless.
        ctx.decision_model = pm_result.model
        # Conviction ledger (spec §9.5): the nomination rows this run wrote
        # during MorningResearchStage carry decision_id NULL because the id
        # did not exist yet. Join them now — before the PM-failure early
        # return below, so a run whose PM produced nothing still shows which
        # seats had asked for what. Bookkeeping only; never raises.
        _link_nominations_to_decision(pipeline, ctx)

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
        # Spec §2.2 — the book's risk as the constructor must ration it, both
        # already computed above (before `decide()`) so the Phase 14
        # rotation pre-check and the constructor ration against the exact
        # same numbers. Absent facts (a stage built without them) leaves
        # both None and the portfolio ceilings unenforced rather than
        # enforced against a fabricated view of the book.
        # Spec §9.4 — the SAME canonical evidence registry the PM's own
        # prompt was built from (`build_evidence_registry` is a pure
        # function of these exact inputs, so recomputing it here from the
        # identical arguments passed to `decide()` above is guaranteed to
        # agree with what PM was actually shown). Feeds the constructor's
        # agreement ceiling — never invented from PM's own provenance,
        # which the PM could under-cite.
        evidence_registry = PortfolioManagerAgent.build_evidence_registry(
            analyses=analyses, positions=positions, news_intel=news_intel,
            earnings_analyses=earnings_results,
            macro_analysis=_macro_analysis_as_dict(macro_analysis),
            smart_money_findings=ctx.smart_money_findings,
            symbol_sectors=dict(getattr(pipeline, "_last_symbol_sectors", {})),
        )
        # §9.4 freshness — same pure function, same inputs, so the stances
        # the constructor refuses to pay for are exactly the ones the PM's
        # prompt marked stale. An earnings view older than
        # `EARNINGS_STANCE_MAX_AGE_DAYS` stops counting toward the agreement
        # tally; it stays in the registry above, so grounding still accepts
        # it as coverage and this can only ever shrink a ceiling.
        stale_sources = PortfolioManagerAgent.stale_evidence_sources(
            earnings_analyses=earnings_results,
        )
        # Conviction ledger (spec §9.5): persist every seat's side on every
        # idea — dissent included — from that same registry, BEFORE the
        # constructor runs so a construction failure cannot lose the record
        # of what the desk believed. Writes evidence rows only; the
        # `evidence_registry` handed to `construct_orders` below is the
        # identical object, unread and unmutated by this call.
        _record_seat_stances(
            pipeline, ctx, evidence_registry,
            [t.symbol for t in portfolio_decision.targets],
        )
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
            evidence_registry=evidence_registry,
            stale_sources=stale_sources,
            # Spec §11.2 — the session's gross-exposure ceiling, already
            # resolved from account state in the run preamble (and re-derived
            # here only on a lane where the preamble did not run). The
            # constructor sizes UNDER it; it never trims the held book.
            gross_ceiling=_session_gross_ceiling(pipeline, ctx),
        )
        # Provenance for the AI Risk Manager: which proposed symbols did the
        # deterministic constructor remove? Derived here (targets minus
        # decisions) rather than by changing construct_orders' signature.
        # HOLD decisions still count as "kept" — the symbol survived review.
        _kept = {d.symbol.upper() for d in portfolio_decision.decisions}
        portfolio_decision.constructor_dropped = [
            t.symbol.upper() for t in portfolio_decision.targets
            if t.symbol.upper() not in _kept
        ]
        if portfolio_decision.constructor_dropped:
            logger.info(
                "Constructor dropped %s — recorded for the Risk Manager so "
                "PM's narrative mentioning them does not read as incoherence.",
                ", ".join(portfolio_decision.constructor_dropped),
            )
            # Funnel-queue item 2 (2026-09-03): a target the constructor
            # drops before ever building a `proposed_order` row previously
            # left NOTHING in the database — no verdict (RM never saw it),
            # no execution_skip (execution never saw it either), just the
            # generic aggregate log line above. `blocked_proposals_census.py`
            # counts every one of these as `no_order_built`, its largest
            # unexplained bucket. The constructor's OWN reason has always
            # existed (its per-target logger.warning/info calls) but was
            # never persisted — `last_drop_reasons` (see
            # `PortfolioConstructor.construct_orders`) recovers it from the
            # SAME call that just ran, so every dropped symbol now gets a
            # terminal, real-reason evidence row instead of silence. Falls
            # back to a generic label only if a future refactor adds a new
            # drop path this capture's log-message pattern doesn't match —
            # never nothing, even then.
            drop_reasons = getattr(
                pipeline.portfolio_constructor, "last_drop_reasons", {},
            )
            for sym in portfolio_decision.constructor_dropped:
                _record_pipeline_event(
                    pipeline, ctx, sym, "deterministic_gate", "blocked",
                    "constructor_dropped",
                    detail=drop_reasons.get(sym, "no matching constructor log line captured"),
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


def _apply_sector_unresolved_alert(data_status: dict, violations: list) -> None:
    """Promote a `sector_unresolved_*` advisory (src/risk/rules.py rule 5)
    into `data_status["sector"]` — the same generic dict `notifier.py` /
    `trader_feed.py` already render as a plain "⚠️ degraded: ..." line in
    the session output, and that output IS the owner's alert (every
    session ends with a Telegram push). Matches the existing pattern
    instead of inventing a new alert channel.

    "degraded" (transient — self-heals) beats "partial" (may genuinely
    have no sector) if a run somehow surfaces both, and never downgrades
    an alert already raised earlier in the same run.
    """
    alerts = [v for v in violations if v.rule.startswith("sector_unresolved")]
    if not alerts:
        return
    status = "degraded" if any(
        v.rule == "sector_unresolved_lookup_failed" for v in alerts
    ) else "partial"
    if data_status.get("sector") == "degraded":
        status = "degraded"
    data_status["sector"] = status
    logger.warning(
        "Sector cap: unresolved sector affected a trading decision — %s",
        "; ".join(dict.fromkeys(a.message for a in alerts)),
    )


class RiskStage:
    """Hard filter → earnings cap → correlation → RM review → mods → re-filter.

    Reads:  ctx.portfolio_decision, ctx.positions, ctx.total_value,
            ctx.last_equity, ctx.earnings_results, ctx.macro_analysis,
            ctx.analyses, ctx.symbols_bars, ctx.data_status, ctx.news_intel,
            ctx.macro_summary, ctx.macro_events, ctx.macro_event_coverage,
            ctx.fomc_meetings, ctx.fomc_coverage

    Writes: ctx.portfolio_decision.decisions (filtered/capped/scaled),
            ctx.correlation_matrix, ctx.daily_pnl, ctx.macro_target_pct

    Returns an early-exit dict (symbol_block / hard_risk_block / rejected)
    or None when the pipeline should proceed to execution.
    """

    def __init__(self, *, pipeline: "TradingPipeline"):
        self._pipeline = pipeline

    @staticmethod
    def _build_event_risk_block(pipeline, ctx: RunContext) -> str:
        """The fetched Event Risk section handed to the Risk Manager.

        `RiskVerdict.reasoning_chain.event_risk` is a REQUIRED narrative field
        asking whether earnings or a macro release land in the next few
        sessions. Nothing fetched either fact before this: the earnings-date
        lookup (`MarketDataProvider.get_next_earnings_date`) had no callers
        anywhere, and no macro calendar existed — so a mandatory risk check was
        being answered from the model's recollection.

        Earnings are looked up for exactly the symbols RM is judging (this
        run's decisions), bounded per-symbol AND in aggregate so a stalled
        yfinance call can never delay the session. The macro calendar is REUSED
        from the research stage (`ctx.macro_events`) so one session issues one
        FRED sweep, and the FOMC schedule the same way (`ctx.fomc_meetings`),
        so one session issues one Fed calendar fetch; on a resume lane where
        research never ran, ctx carries its "not fetched" defaults and the
        block says exactly that.

        Never raises and never returns an empty string: on any failure the seat
        is shown the NOT FETCHED form. A missing section reads as a calm
        calendar, which is precisely the failure being fixed.
        """
        symbols = [
            d.symbol for d in (
                ctx.portfolio_decision.decisions if ctx.portfolio_decision else []
            )
        ]
        # Every lookup below is a getattr with a default: this helper is called
        # on partially-constructed pipelines (the resume lane, and several test
        # doubles built with __new__), and an event-risk block is never worth
        # aborting a risk review over. A missing dependency degrades to the
        # labelled NOT FETCHED form, which is still an honest answer.
        event_cfg = getattr(getattr(pipeline, "config", None), "event_risk", None)
        horizon_days = getattr(event_cfg, "horizon_days", 10)
        earnings = None
        try:
            if symbols and getattr(pipeline, "market", None) is not None:
                earnings = fetch_earnings_proximity(
                    pipeline.market, symbols,
                    per_symbol_timeout_s=getattr(
                        event_cfg, "earnings_symbol_timeout_s", 8.0,
                    ),
                    total_deadline_s=getattr(event_cfg, "earnings_deadline_s", 20.0),
                )
        except Exception as e:  # noqa: BLE001
            logger.warning("Earnings proximity sweep failed: %s", e)
            earnings = None

        coverage = ctx.macro_event_coverage
        if not isinstance(coverage, EventCalendarCoverage):
            coverage = None
        events = list(ctx.macro_events or []) if coverage is not None else None
        # Same pairing rule for the FOMC half: the schedule is only shown
        # alongside the coverage object that says how far it reaches, because
        # an empty schedule with no coverage line reads as "no Fed decision
        # coming" — the exact fabrication this block exists to prevent.
        fomc_coverage = getattr(ctx, "fomc_coverage", None)
        if not isinstance(fomc_coverage, FOMCCoverage):
            fomc_coverage = None
        fomc_meetings = (
            list(getattr(ctx, "fomc_meetings", None) or [])
            if fomc_coverage is not None else None
        )
        try:
            return format_event_risk_block(
                earnings=earnings, events=events, coverage=coverage,
                horizon_days=horizon_days,
                fomc_meetings=fomc_meetings, fomc_coverage=fomc_coverage,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("Event-risk block render failed: %s", e)
            return format_event_risk_block(
                earnings=None, events=None, coverage=None, horizon_days=0,
            )

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

        # Spec §11.2 — the session's gross-exposure ceiling, resolved from
        # ACCOUNT STATE and never from PM output. The run preamble already
        # acted on it before any agent was called; reading it again here is
        # what makes the execution gate below measure new orders against the
        # same rung the constructor sized them under.
        session_gross_ceiling = _session_gross_ceiling(pipeline, ctx)

        # Audit §1.1 — the drawdown-halve is deterministic code now, applied
        # before the hard filter so every downstream consumer (cash budget,
        # sector accumulation, RM, execution) sees the halved size rather than
        # PM's pre-halving intent. The PM prompt no longer pre-applies it.
        if in_drawdown:
            from src.risk.rules import apply_drawdown_scale
            portfolio_decision.decisions, drawdown_notes = apply_drawdown_scale(
                portfolio_decision.decisions, in_drawdown=True,
                ceiling=session_gross_ceiling,
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
                gross_ceiling=session_gross_ceiling,
            )
        )
        _apply_sector_unresolved_alert(data_status, rule_violations)
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

        # Parse-level losses anywhere in this session (2026-09-02). Before
        # this, an analysis discarded over one malformed field was a single
        # ERROR log line nobody counted, and an explicit null silently
        # replaced by a default left no trace at all. Both are inputs the
        # analysts produced and the desk then failed to use — invisible
        # under-deployment, and the expensive kind, because a candidate the
        # Portfolio Manager never sees cannot be traded and cannot be
        # measured as a miss either.
        #
        # ADVISORY, never blocking — the same non-blocking seam
        # `data_degraded`, `correlation_coverage_gap` and
        # `pm_audit_step_missing` above already use. It reaches the Risk
        # Manager's prompt through `rule_violations` and the operator through
        # the session log; no order is blocked by it, because a parse loss is
        # evidence about COVERAGE, not about the soundness of the orders that
        # did survive.
        #
        # Read LIVE rather than from a research-stage snapshot: the Portfolio
        # Manager parses in DecisionStage, AFTER research, and
        # `TargetPosition.thesis_invalid_if` is one of the two fields this
        # whole change is about. A snapshot taken at the end of research would
        # miss every PM-side loss.
        ctx.dropped_analyses = parse_telemetry.dropped_snapshot()
        ctx.null_coerced_fields = parse_telemetry.snapshot()
        dropped = ctx.dropped_analyses
        if dropped:
            from src.risk.rules import RiskViolation as _RV
            names = ", ".join(
                f"{model}:{key}" for (model, key), _n in sorted(dropped.items())
            )
            n_dropped = sum(dropped.values())
            rule_violations.append(_RV(
                rule="analysis_parse_loss",
                message=(
                    f"{n_dropped} item(s) were discarded at parse this session "
                    f"and are absent from the book below: {names} "
                    f"(TechAnalysisResult = a candidate PM never saw; "
                    f"TargetPosition = a position PM asked for and the desk "
                    f"could not read). The plan was therefore built from, or "
                    f"reduced to, a SMALLER set than the seats produced — "
                    f"treat a thin list as possibly truncated rather than as a "
                    f"genuine absence of setups."
                ),
                value=float(n_dropped),
                limit=0.0,
            ))
            logger.error(
                "Analysis parse loss reached the risk stage: %d item(s) — %s",
                n_dropped, names,
            )

        nulled = ctx.null_coerced_fields
        if nulled:
            from src.risk.rules import RiskViolation as _RV
            detail = ", ".join(
                f"{model}.{field}x{n}"
                for (model, field), n in sorted(nulled.items(), key=lambda kv: -kv[1])
            )
            n_nulled = sum(nulled.values())
            rule_violations.append(_RV(
                rule="analysis_field_nulled",
                message=(
                    f"{n_nulled} field(s) arrived as an explicit null and took "
                    f"their schema default: {detail}. The analyses were KEPT "
                    f"(the alternative — dropping them — is worse), but a "
                    f"nulled `thesis_invalid_if` means that idea has no "
                    f"soft-exit trigger and will be managed on the hard stop "
                    f"alone."
                ),
                value=float(n_nulled),
                limit=0.0,
            ))

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

        rm_event_risk_block = self._build_event_risk_block(pipeline, ctx)

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
            # The fetched answer to `reasoning_chain.event_risk` — see
            # RiskStage._build_event_risk_block.
            event_risk_block=rm_event_risk_block,
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
            # Phase 10.1 — the per-symbol audit trail. Written for EVERY
            # refusal the verdict carries, including one naming a symbol not
            # in the plan, so "why was this name refused" stays answerable per
            # name and not only through the run-scoped verdict blob.
            for rejection in verdict.rejected_symbols:
                _persist_evidence(
                    pipeline.db, run_id=run_id, agent_name="risk_manager",
                    kind="rejection", scope="symbol", symbol=rejection.symbol,
                    decision_id=ctx.decision_id,
                    evidence_json=rejection.model_dump_json(),
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

        # BOOK-level veto, evaluated FIRST and unchanged. A correlation
        # cluster, a total-exposure breach or a drawdown state is a property
        # of the whole account, so when the book is what fails, every leg
        # dying is the correct outcome — and a verdict that sets this AND
        # names individual symbols still refuses everything.
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

        # PER-SYMBOL refusal (spec Phase 10.1). One failing leg dies alone.
        # Before this, `approved` was the only refusal the schema had, so a
        # single sub-floor R/R took the whole plan with it — run-64290730
        # (2026-09-01) refused the morning citing XLE alone and killed CHPX,
        # a passing trade in a different sector, with it.
        rejections = verdict.rejections_by_symbol()
        refused_decisions: list = []
        if rejections:
            surviving: list = []
            for decision in portfolio_decision.decisions:
                reason = rejections.get(decision.symbol.strip().upper())
                if reason is None:
                    surviving.append(decision)
                    continue
                refused_decisions.append(decision)
                logger.info(
                    "Risk manager REFUSED %s (the rest of the plan is "
                    "unaffected): %s", decision.symbol, reason,
                )
                _record_pipeline_event(
                    pipeline, ctx, decision.symbol, "risk", "rejected", reason,
                )
            unmatched = sorted(
                set(rejections) - {d.symbol.strip().upper() for d in refused_decisions}
            )
            if unmatched:
                logger.warning(
                    "Risk manager refused %s, which is not in the proposed "
                    "plan — no-op (evidence still recorded)",
                    ", ".join(unmatched),
                )
            portfolio_decision.decisions = surviving

            # `refused_decisions` guards the case where the refusals matched
            # nothing: an empty plan plus a stray symbol name is not a
            # refusal of anything and must not become one.
            if refused_decisions and not surviving:
                # Every leg refused individually. Same terminal status as a
                # book veto because the outcome is the same — no orders — but
                # each symbol carries its OWN reason above, not one shared
                # sentence about a different symbol.
                reasons = "; ".join(
                    f"{sym}: {rejections[sym]}"
                    for sym in sorted(
                        {d.symbol.strip().upper() for d in refused_decisions}
                    )
                )
                logger.info(
                    "Every proposed trade was refused on its own merits: %s",
                    reasons,
                )
                return {"status": "rejected", "orders": [], "reason": reasons}

        if verdict.modifications:
            portfolio_decision.decisions, rejected_mods = pipeline._apply_risk_modifications(
                portfolio_decision.decisions, verdict.modifications,
                symbols_bars=getattr(ctx, "symbols_bars", None),
            )
            # A modification this method refused (exit silently zeroed, or a
            # stop/target edit that would have shipped a reward:risk / noise-
            # band floor breach) must be a visible, distinguishable event —
            # not an edit that just vanishes. See `_apply_risk_modifications`
            # docstring, guards 1 and 2 (2026-09-03 audit).
            for rejected in rejected_mods:
                _record_pipeline_event(
                    pipeline, ctx, rejected["symbol"], "risk",
                    "modification_rejected", rejected["reason"],
                    field=rejected["field"],
                )

        portfolio_decision.decisions, scale = _apply_scale_all_buys(
            portfolio_decision.decisions, verdict,
        )

        if verdict.modifications or scale < 1.0 or refused_decisions:
            portfolio_decision.decisions, post_mod_violations, blocked_reasons = (
                pipeline._filter_hard_risk_decisions(
                    portfolio_decision.decisions,
                    positions, total_value, daily_pnl,
                    baseline=last_equity,
                    macro_target_invested_pct=macro_target_pct,
                    correlation_matrix=correlation_matrix,
                    cash=ctx.deployable_cash,
                    in_drawdown=in_drawdown,
                    gross_ceiling=session_gross_ceiling,
                )
            )
            _apply_sector_unresolved_alert(data_status, post_mod_violations)
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
        # failure is already knowable and computes the actual quantized
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
            # Spec §11.1: quantized the SAME way the submit loop below will,
            # or the sweep funds a whole-share notional for an order that is
            # about to be placed fractionally — under-funding it, and letting
            # the cash gate re-impose the rounding tax this phase removes.
            # It is also the difference between skipping a sub-one-share
            # position as `qty_zero` and taking it, which under exact sizing
            # is a legitimate position rather than nothing.
            preflight_short = decision.action == "SHORT"
            preflight_fractional = _fractional_sizing_allowed(
                pipeline, decision.symbol, is_short=preflight_short,
            )
            preflight_qty = _size_shares(
                pipeline,
                (total_value * decision.allocation_pct / 100) / preflight_price,
                fractional=preflight_fractional,
            )
            if preflight_qty <= 0:
                _record_execution_skip(
                    pipeline, ctx, decision.symbol, "qty_zero",
                    f"allocation {decision.allocation_pct:.2f}% at "
                    f"${preflight_price:.2f} rounds to zero shares",
                )
                continue
            # Fund what the submit loop will SPEND, not what the allocation
            # asked for. The loop takes `min(qty_by_alloc, qty_by_risk)`; on
            # any session where the vol-adjusted budget binds — the ordinary
            # case — funding the allocation figure over-sells the vehicle and
            # the bookend re-parks the difference within the minute. Same
            # helper, same quantization, so the two cannot drift apart.
            #
            # UNDER-funding is the one direction that costs a trade rather
            # than a spread, so the reference price must be the submit
            # loop's own. It is: for a long the loop takes
            # `max(market_price, limit_price)` and for a short
            # `min(market_price, limit_price)` — which is exactly
            # `preflight_price` above. Every adjustment the loop makes AFTER
            # that point moves the quantity DOWN, never up: a marketable-
            # limit ceiling only raises the price, and the ATR floor only
            # widens the stop, and each of those shrinks the shares the risk
            # budget allows. So this is an upper bound on what will be
            # spent, which is the safe side to be wrong on.
            preflight_risk_qty = _qty_by_risk_budget(
                pipeline, total_value=total_value,
                sizing_price=preflight_price,
                stop_price=decision.stop_loss,
                is_short=preflight_short, fractional=preflight_fractional,
            )
            if preflight_risk_qty is not None and preflight_risk_qty < preflight_qty:
                preflight_qty = preflight_risk_qty
            if preflight_qty <= 0:
                # The risk budget alone cannot carry one orderable unit. The
                # submit loop will reach the same conclusion and skip; there
                # is nothing here for the sweep to fund.
                _record_execution_skip(
                    pipeline, ctx, decision.symbol, "qty_zero",
                    f"risk budget at ${preflight_price:.2f} entry / "
                    f"${decision.stop_loss:.2f} stop rounds to zero shares",
                )
                continue
            # A SHORT is deliberately excluded from the funding total: it
            # sells borrowed shares and spends no cash (see D11 in the submit
            # loop, where a short is never sized by the entry budget).
            # Funding one liquidates the vehicle to raise cash that no order
            # can spend — guaranteed churn, not a safety margin. BUY
            # notionals are still counted in full, so this can only remove
            # waste, never under-fund a BUY.
            if not preflight_short:
                fundable_notional[decision.symbol] = preflight_qty * preflight_price
            preflight_survivors.append(decision)
        buy_decisions = preflight_survivors

        # Cash-sweep funding. `planned_notional` counts BUYs ONLY, at the
        # quantity the submit loop will actually reach — allocation capped by
        # the §11.1 risk budget, quantized by the same helper. A SHORT is
        # excluded outright: it sells borrowed shares and spends no cash (see
        # D11 in the sizing loop), so funding one liquidates the vehicle for
        # cash no order can spend. Both were over-funding, and over-funding
        # is not free: the bookend re-parks
        # the residue minutes later, which is two crossings of the spread
        # for no position (2026-08-27: sold $3,422.61, re-bought $1,007.60
        # 53 seconds later; 2026-08-31: sold $503.47, re-bought $806.40
        # five seconds later).
        #
        # PM/RM/the hard gate size BUYs against
        # `deployable_cash` (raw cash + convertible sweep value), so on any
        # session with meaningful BUYs this sale IS load-bearing — the raw
        # cash on hand is typically just the reserve. `fund_buys` sells
        # enough of the vehicle to cover the planned notional, then waits
        # for the fill and CONFIRMS the observed rise in broker cash (a
        # filled sale credits `cash` immediately; the 2026-08-19 loss of a
        # fully-approved plan was a 51s fill outliving a 15s wait, not
        # settlement — see cash_sweep._FUND_TERMINAL_TIMEOUT_S).
        #
        # Since margin went on (2026-09-02) the sale is no longer what makes
        # a BUY POSSIBLE — the entry budget below is ladder headroom, and a
        # BUY the sale failed to fund now draws a margin loan instead of
        # being skipped. It is still worth doing: borrowing at
        # `margin_interest_rate_pct` against T-bills the desk already owns is
        # a guaranteed negative carry, so the sweep converts first and the
        # loan is what is left over.
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
                else:
                    # Adopt whatever the sweeper refreshed REGARDLESS of the
                    # confirmed amount. `fund_buys` re-reads the broker into
                    # ctx before it decides what it can confirm, so on the
                    # zero-confirmed path ctx already held fresher figures
                    # than these locals — and the locals, not ctx, govern the
                    # BUY loop's entry budget. Refreshing only on the
                    # success path meant an unconfirmed funding attempt left
                    # the loop sizing against a pre-sale cash reading; if
                    # anything had DRAWN cash in between, that reading is
                    # stale-HIGH and the clamp stops protecting anything.
                    # ctx is unchanged when fund_buys bailed early, so this
                    # is a no-op in the ordinary case.
                    if isinstance(getattr(ctx, "cash", None), (int, float)):
                        cash = ctx.cash
                    if isinstance(getattr(ctx, "total_value", None), (int, float)):
                        total_value = ctx.total_value
                    if ctx.positions is not None:
                        positions = ctx.positions
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

        # Spec §11.2 — how much NEW exposure this session may still add, and
        # the pool every entry below draws from. Ladder-derived (see
        # `_entry_deployment_budget`); raw cash only when the ladder cannot
        # be read at all. `total_value` and `positions` are the post-sell,
        # post-funding figures adopted above, so the headroom is measured
        # against the book the entries will actually join.
        entry_budget, budget_is_gross, budget_note = _entry_deployment_budget(
            pipeline, ctx, positions, total_value, cash,
        )
        single_name_cap = _single_name_execution_cap(pipeline, total_value)
        if buy_decisions:
            logger.info(
                "Entry budget for %d entr%s: $%.2f — %s (single-order ceiling "
                "$%.2f)",
                len(buy_decisions), "y" if len(buy_decisions) == 1 else "ies",
                entry_budget, budget_note, single_name_cap,
            )
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
                #
                # 2026-09-02: it is now also skipped for a stop the
                # constructor HONOURED at a computed structural level. This
                # belt was the last place spec §12.1 was being undone. §12.1
                # says the ATR floor applies only when nothing computed
                # backs the stop, and the constructor implements that — but
                # this code then re-applied a 1x ATR floor to the result,
                # against an ATR recomputed here from `ctx.symbols_bars`
                # rather than the `analysis.atr_14` the constructor used. Two
                # readings of the same quantity, and the larger one silently
                # won, moving the stop off the level and shrinking the R/R
                # the re-check below then judges. The constructor already
                # applies `absolute_min_stop_atr_multiple` (1x ATR) to a
                # level-backed stop, so the protection is not lost — it is
                # applied once, by the stage that can see the levels.
                stop_price = decision.stop_loss
                level_backed = decision.stop_rule in LEVEL_BACKED_STOP_RULES
                if level_backed and not is_short:
                    logger.info(
                        "BUY %s: execution-time ATR stop floor skipped — the "
                        "constructor honoured this stop at a computed "
                        "structural level [%s]. Re-widening it here would "
                        "undo §12.1 against a second ATR reading.",
                        decision.symbol, decision.stop_rule,
                    )
                if not is_short and not level_backed and stop_price > 0 and sizing_price > stop_price:
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
                #
                # The ratio comes from `models.reward_to_risk`, the single
                # definition the constructor's own floor and
                # `TradeDecision.reward_risk` both use. It used to be a
                # fourth hand-rolled division here. On 2026-09-01 two of
                # those copies were rendered to the Risk Manager on the same
                # XLE trade — 1.67 and 1.18 — and the unexplained gap
                # between them is what rejected it.
                geometry_changed = (
                    stop_price != decision.stop_loss
                    or (decision.entry_price > 0 and sizing_price > decision.entry_price)
                )
                if not is_short and geometry_changed and decision.take_profit > 0:
                    executed_rr = reward_to_risk(
                        sizing_price, stop_price, decision.take_profit,
                        is_short=False,
                    )
                    # FAIL CLOSED. None means the executed geometry is not
                    # measurable at all — a non-finite price, a stop at or
                    # above the entry, a target below it. That is strictly
                    # worse than a low ratio, not better, and it used to
                    # fall through this gate untouched because the old
                    # `risk > 0` guard simply skipped the check.
                    if executed_rr is None or executed_rr < 1.2:
                        rr_text = (
                            "unmeasurable" if executed_rr is None
                            else f"{executed_rr:.2f}"
                        )
                        logger.warning(
                            "BUY %s skipped: executed geometry makes R/R %s "
                            "(<1.2) — RM approved entry $%.2f / stop $%.2f, "
                            "execution moved it to $%.2f / $%.2f.",
                            decision.symbol, rr_text,
                            decision.entry_price, decision.stop_loss,
                            sizing_price, stop_price,
                        )
                        _record_execution_skip(
                            pipeline, ctx, decision.symbol, "geometry_rr",
                            f"executed geometry R/R {rr_text} < 1.2 "
                            f"(RM approved ${decision.entry_price:.2f}/"
                            f"${decision.stop_loss:.2f}, execution moved to "
                            f"${sizing_price:.2f}/${stop_price:.2f})",
                        )
                        continue

                # Spec §11.1. Exact sizing when the flag is on AND the broker
                # confirms the symbol is fractionable; whole shares otherwise.
                # Resolved ONCE per symbol here so every share count below —
                # allocation, risk budget, cash re-size — is quantized the
                # same way. Two different roundings inside one sizing decision
                # is how a stop ends up covering a different number of shares
                # than the entry bought.
                fractional = _fractional_sizing_allowed(
                    pipeline, decision.symbol, is_short=is_short,
                )
                qty_by_alloc = _size_shares(
                    pipeline,
                    (total_value * decision.allocation_pct / 100) / sizing_price,
                    fractional=fractional,
                )
                # Same helper the cash-sweep preflight sized funding with —
                # one definition, so the dollars released can never drift
                # from the dollars spent.
                qty_by_risk = _qty_by_risk_budget(
                    pipeline, total_value=total_value,
                    sizing_price=sizing_price, stop_price=stop_price,
                    is_short=is_short, fractional=fractional,
                )
                if qty_by_risk is not None and qty_by_risk < qty_by_alloc:
                    _risk_pct = _risk_budget_pct(pipeline)
                    logger.info(
                        "Vol-adjusted sizing for %s: qty_by_alloc=%s → qty_by_risk=%s "
                        "(risk %.2f/share, budget $%.0f = %.1f%% of equity)",
                        decision.symbol, _fmt_shares(qty_by_alloc),
                        _fmt_shares(qty_by_risk),
                        abs(sizing_price - stop_price),
                        total_value * _risk_pct / 100, _risk_pct,
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
                # The ceiling THIS order may reach: the batch pool, or the
                # single-name cap, whichever is lower. Two different jobs —
                # the pool stops the SESSION deploying past the ladder rung,
                # the cap stops ONE order draining the pool.
                order_ceiling = min(entry_budget, single_name_cap)
                # D11: a SHORT is never trimmed or refused here. It does not
                # spend settled cash (it sells borrowed shares), and the caps
                # (D9) plus the borrow gate (D6) are the sole control surface
                # for a short. It DOES consume gross exposure, so it draws
                # the ladder pool down after submission below — but it is
                # never sized by it, which keeps the short path exactly as it
                # shipped.
                if not is_short and estimated_cost > order_ceiling:
                    affordable_qty = _size_shares(
                        pipeline, order_ceiling / sizing_price,
                        fractional=fractional,
                    )
                    # The skip reason stays `insufficient_cash` even though
                    # the binding number is no longer always cash: it is a
                    # persisted evidence code the funnel, the trader feed and
                    # the blocked-proposal digest already read, and renaming
                    # it would orphan every historical row. The detail line
                    # carries the truth.
                    if affordable_qty <= 0:
                        logger.warning(
                            "Skipping BUY %s: estimated cost $%.2f exceeds the "
                            "$%.2f still deployable — %s",
                            decision.symbol, estimated_cost, order_ceiling,
                            budget_note,
                        )
                        _record_execution_skip(
                            pipeline, ctx, decision.symbol, "insufficient_cash",
                            f"estimated cost ${estimated_cost:.2f} exceeds the "
                            f"${order_ceiling:.2f} still deployable "
                            f"({budget_note})",
                        )
                        continue
                    logger.warning(
                        "Resizing BUY %s from %s to %s share(s): only $%.2f is "
                        "still deployable — %s",
                        decision.symbol, _fmt_shares(qty),
                        _fmt_shares(affordable_qty), order_ceiling, budget_note,
                    )
                    qty = min(qty, affordable_qty)
                    estimated_cost = qty * sizing_price
                    # §10.3's floor, re-applied to the size EXECUTION chose.
                    # `apply_gross_ceiling` and the constructor both refuse a
                    # trimmed order below `min_order_usd` rather than place a
                    # token position — but this clamp happens AFTER both of
                    # them, so it was the one resize with no floor under it.
                    # With fractional sizing on, `affordable_qty` no longer
                    # floors to zero shares when cash is short: a $3 residue
                    # buys 0.0281 shares and the order goes out. A position
                    # too small to pay for its own risk is not a smaller
                    # trade, it is a worse one.
                    floor_usd = _min_order_usd(pipeline)
                    if estimated_cost < floor_usd:
                        logger.warning(
                            "Skipping BUY %s: the budget re-size cut the order "
                            "to $%.2f (%s sh), below the $%.0f minimum worth "
                            "trading",
                            decision.symbol, estimated_cost,
                            _fmt_shares(qty), floor_usd,
                        )
                        _record_execution_skip(
                            pipeline, ctx, decision.symbol, "below_min_notional",
                            f"${order_ceiling:.2f} still deployable re-sized the "
                            f"order to ${estimated_cost:.2f}, below the "
                            f"${floor_usd:,.0f} minimum worth trading",
                        )
                        _record_pipeline_event(
                            pipeline, ctx, decision.symbol, "funding", "refused",
                            "resized_below_min_notional",
                            resized_notional=estimated_cost,
                            min_order_usd=floor_usd,
                        )
                        continue
                    _record_pipeline_event(
                        pipeline, ctx, decision.symbol, "funding", "resized",
                        "confirmed_cash_partially_funded_order",
                        approved_qty=qty_by_risk if qty_by_risk is not None and qty_by_risk < qty_by_alloc else qty_by_alloc,
                        resized_qty=qty,
                        deployment_budget=entry_budget,
                        order_ceiling=order_ceiling,
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
                    # Conviction ledger (spec §7.2) — pinned at entry from
                    # the constructor's TradeDecision (see portfolio_
                    # constructor._build_buy/_build_short) and from this
                    # run's PM model. None/None/None for a legacy notional
                    # target that carried no risk-based plan.
                    conviction=getattr(decision, "conviction", None),
                    requested_risk_pct=getattr(decision, "requested_risk_pct", None),
                    allocated_risk_pct=getattr(decision, "allocated_risk_pct", None),
                    decision_model=ctx.decision_model,
                    # Same entry-only pinning as the conviction ledger above
                    # — see TradeDecision.thesis_invalid_if in models.py.
                    thesis_invalid_if=getattr(decision, "thesis_invalid_if", None),
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
                        f"broker rejected {decision.action.lower()} "
                        f"{_fmt_shares(qty)} @ "
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
                if budget_is_gross or not is_short:
                    # The pool is drawn down by what the order CONSUMES of
                    # it, and the two budgets are consumed by different
                    # things. A ladder budget is GROSS headroom: a short
                    # occupies gross exactly as a long does (`gross_exposure`
                    # sums the magnitude of both), so it must draw the pool
                    # or a batch of shorts would leave the longs behind them
                    # sized against headroom that is already spent. The cash
                    # fallback is a settled-cash pool, which a short does not
                    # touch at all — D11, unchanged.
                    entry_budget -= estimated_cost
                order_type = "limit" if limit_price is not None else "market"
                logger.info(
                    "Executed: %s %s %s @ %s $%.2f",
                    decision.action.lower(), _fmt_shares(qty), decision.symbol,
                    order_type, executed_price,
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
                # Spec §11.1 guard 2. The broker has already retried hard and
                # immediately (guard 1) by the time this is reached, so a
                # falsy `protection` means a position is open at the broker
                # with NO stop on it, and a non-zero `uncovered_qty` means
                # part of one is. Neither may be reported as a log line: a log
                # line is read after the fact, and the whole reason fractional
                # sizing is acceptable is that the unprotected window is brief
                # — which is only true if a HUMAN is told the moment it stops
                # being brief. Never lets an alerting failure abort the
                # session.
                _alert_owner_protection_failed(
                    pipeline, spec, protection, entry_order_id,
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
