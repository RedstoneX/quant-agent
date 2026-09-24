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
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from typing import TYPE_CHECKING

from src import evidence_gate
from src.agents.base import agent_log_kwargs
from src.agents.portfolio_manager import PortfolioManagerAgent
from src.cost_circuit import PaidAnalysisSuspended
from src.data.macro import MacroCoverage
from src.data.event_calendar import (
    EventCalendarCoverage, FOMCCoverage, fetch_earnings_proximity,
    format_event_risk_block,
)
from src.data.levels import FAULT_NO_PRICE, FAULT_STALE_PRICE
from src.data.live_price import ONLY_STALE, resolve_live_price
from src.data.technical import compute_indicators
from src.models import (
    NewsIntelligenceReport, Nomination, TechAnalysisResult, TechnicalIndicators,
    missing_stated_falsifier, open_target_missing_falsifier,
    parse_telemetry, SOFT_EXIT_MISSING_AFTER_RETRY,
)
from src.nominations import select_nominations
from src.portfolio_constructor import (
    CONSTRUCTOR_REFUSED_EVENT_REASON,
    LEVEL_BACKED_STOP_RULES,
)
from src.pipeline_context import RunContext
from src.risk.constants import (
    REWARD_RISK_FLOOR,
    STARTER_POSITION_RISK_PCT,
)

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

#: Adverse-excursion bound on an entry limit, in basis points from the
#: verified reference. A BUY ceiling sits this far *above* the reference; a
#: SHORT floor sits this far *below* it — the same number, opposite side.
#: This is fillability parity, not a second risk budget. Raised from a
#: hardcoded 25 to a configurable 40 on 2026-08-27 after VLO proved 25bp is
#: tighter than a normal market open: the ask was 28bp above the reference
#: within seconds of 09:30, so the cap produced an unfillable limit and no
#: trade. 40bp still refuses to pay through a genuinely abnormal book — a
#: gap, a halt reopen, a fat spread — while tolerating ordinary opening
#: drift. Override in `settings.yaml` under `execution.max_entry_slippage_bps`.
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



def _live_stops_from_heat(ctx) -> dict[str, float] | None:
    """{symbol: live broker stop} from the PM facts' heat roll-up, or None.

    The stops behind `_book_risk_inputs`' per-symbol risk, so a trim sized
    from them is sized against the very risk figure the PM was shown.
    """
    heat = getattr(getattr(ctx, "facts", None), "heat", None)
    if heat is None:
        return None
    try:
        return {
            row.symbol.upper(): row.stop
            for row in heat.per_position if row.protected and row.stop
        }
    except Exception as e:  # noqa: BLE001 — never fail the session on this
        logger.warning("constructor: live stop map failed: %s", e)
        return None


#: Sentinel written into `pending_repegs.new_order_id` BEFORE the replace
#: PATCH goes out, and overwritten with the real id when the broker answers.
#: A row still carrying it at session start means the process died inside the
#: replace window: the drain must ASK THE BROKER what the old order became
#: rather than assume either outcome. Mirrors `_WAL_SELL_SENTINEL`.
_WAL_REPEG_SENTINEL = "__WAL_REPEG_PENDING__"


def _rotation_execution_enabled(pipeline) -> bool:
    """Phase 14b — is automatic opportunity-cost rotation ON?

    True for an explicit `execution.rotation_enabled is True` and nothing
    else, the same convention as `_repeg_settings` below and for the same
    reason: many tests build the pipeline with a MagicMock config whose
    auto-attributes are truthy, and a MagicMock must never read as "yes,
    close a real position on your own".
    """
    execution_cfg = getattr(getattr(pipeline, "config", None), "execution", None)
    return getattr(execution_cfg, "rotation_enabled", None) is True


def _rotation_ranked_margin_enabled(pipeline) -> bool:
    """Board item 39 — is the RANKED-MARGIN rotation tier executable?

    Requires `execution.rotation_ranked_margin_enabled is True` IN ADDITION
    to `_rotation_execution_enabled`, and the same `is True` convention for
    the same reason (a MagicMock config must never read as "yes, close a
    real position on your own").

    This flag decides whether the tier is ASKED about. It does not decide
    whether a sale can be built: `src/rotation.py::rotation_sell_reason`
    raises unconditionally without a `RotationClearance`, which only
    `_rotation_buy_leg_projected_refusal` below mints, and only after the
    replacement BUY has survived every gate in `REQUIRED_BUY_LEG_GATES`
    against PROJECTED POST-SALE state.
    """
    if not _rotation_execution_enabled(pipeline):
        return False
    execution_cfg = getattr(getattr(pipeline, "config", None), "execution", None)
    return getattr(execution_cfg, "rotation_ranked_margin_enabled", None) is True


#: Trades-row fill states that mean "an order on this symbol has been
#: handed to the broker and not yet reconciled" — the same two values
#: `Database.get_symbol_last_buy(include_in_flight=True)` treats as
#: in-flight. A rotation never touches a symbol carrying one.
_IN_FLIGHT_FILL_STATUSES = frozenset({"submitted", "pending_submit"})


def _rotation_skip(pipeline, ctx, opportunity, reason: str, **details) -> None:
    """One durable `rotation` / `skipped` audit row. Every refusal to act
    lands here so the evening review can see WHY a surfaced comparison did
    not become a trade, rather than inferring it from a log line."""
    logger.info(
        "Rotation: not acting on %s -> %s (%s)",
        opportunity.held_symbol, opportunity.new_symbol, reason,
    )
    _record_pipeline_event(
        pipeline, ctx, opportunity.held_symbol, "rotation", "skipped", reason,
        new_symbol=opportunity.new_symbol, tier=opportunity.tier, **details,
    )


def _record_rotation_precheck(pipeline, ctx) -> None:
    """One durable `rotation` / `precheck` row per session, whatever the
    pre-check concluded — including when it concluded nothing.

    The gap this closes: `_apply_rotation_execution` returns silently when
    `precheck.opportunity is None`, and that silent return is the desk's
    COMMONEST rotation outcome — the book is full, every candidate was
    still ranked against what is held, and none of them won. It left no log
    line, no durable row and nothing in the owner's report, so a session
    that did the comparison looked identical to one that never made it.

    Runs regardless of `execution.rotation_enabled`: the comparison happens
    in the PM's own prompt either way, and whether the desk may ACT on it is
    a separate fact this row records rather than a reason to stay silent.
    Never raises — bookkeeping must not take a live session with it.
    """
    from src.rotation import RotationPrecheck, precheck_record

    try:
        precheck = getattr(
            getattr(pipeline, "portfolio_manager", None),
            "last_rotation_precheck", None,
        )
        if not isinstance(precheck, RotationPrecheck):
            return
        record = precheck_record(
            precheck,
            execute_enabled=_rotation_execution_enabled(pipeline),
            ranked_margin_enabled=_rotation_ranked_margin_enabled(pipeline),
        )
        logger.info(
            "Rotation pre-check: %s (headroom %.2f%% of a %.2f%% ceiling, "
            "binding [%s], %s vs %s at ratio %s%s)",
            record["outcome"], record["headroom_pct"], record["ceiling_pct"],
            record.get("binding", ""), record.get("held_symbol"),
            record.get("new_symbol"), record.get("ratio"),
            f", refused at {record['refusal_point']}"
            if record.get("refusal_point") else "",
        )
        # RUN-scoped, with the symbols in the payload. Scoping it to the
        # holding was tried and reverted on adversary review: this repo has
        # ruled three times (`src/execution/exit_path_records.py`, board
        # item 164, and the plan-edit rows in this file) that
        # `src/refusal_signature.py` reads EVERY symbol-scoped
        # `pipeline_event` as "this session considered that stock as a new
        # idea". A weakest HOLDING is not such a candidate, and this row
        # fires every session — it would have broken the monomorphic-refusal
        # streak on essentially every run and silently disarmed the jam
        # alarm. The near-miss fields are just as queryable in the payload.
        _record_pipeline_event(
            pipeline, ctx, None, "rotation", "precheck",
            record["outcome"], **{
                k: v for k, v in record.items() if k != "outcome"
            },
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Rotation pre-check record failed: %s", exc)


def _apply_rotation_execution(pipeline, ctx, portfolio_decision, positions,
                              position_history: dict | None) -> None:
    """Phase 14b — turn the CATEGORICAL rotation comparison into an ordinary
    zero-size PM target, when the desk's own data says it is safe to.

    Runs after the PM's plan is parsed and grounded and BEFORE
    `PortfolioConstructor.construct_orders`, so the close it proposes is
    built, risk-checked, reviewed and executed by exactly the machinery a
    PM-decided close goes through — `_build_sell`, the hard risk rules, the
    AI Risk Manager (which can refuse it by symbol), the holding-discipline
    claim check in `RiskStage`, and `_submit_protected_sell` in
    `ExecutionStage`. This function adds a target to the plan and records
    why; it never places an order and never removes anything the PM asked
    for. See `src/rotation.py` for the doctrine and the owner request.

    Every guard below fails CLOSED — an unanswerable question means no
    sale — and every refusal is recorded as a `rotation`/`skipped` event:

      1. `execution.rotation_enabled` must be explicitly True (else this
         function is a no-op and the run is byte-for-byte Phase 14).
      2. The PM's own prompt must have surfaced a comparison this session,
         and it must be the categorical tier. The ranked-margin tier is
         information only (see `src/rotation.py`).
      3. The held name must be a LONG the broker actually shows. Shorts
         are not automated — a COVER is a different order shape with its
         own caps, and nothing here has been verified against it.
      4. The PM must itself have targeted the new candidate with a real
         size this session. The desk never invents the buy leg; the close
         only makes room for a trade the model already decided to make.
      5. The PM must not already be targeting the held symbol (closing it
         itself, or adding to it). Either way the model has spoken and
         this function does not override a decision the PM made.
      6. Nothing may be in flight on the held symbol: no trades row for
         it today still `submitted`/`pending_submit`, no BUY of it today at
         all (a day-zero exit "never given a single day's normal range to
         breathe" is exactly what the exit noise band exists to prevent —
         OKLO 2026-08-26), no pending protection-restore WAL row (a sell
         already mid-flight), no pending re-peg row (an entry mid-chase).
      7. Its structural protection must ALREADY be broken under the item-25
         holding-discipline check (`_structural_protection_for_holding`,
         the same call `RiskStage` makes). A position whose thesis level
         is intact is protected from a plain, no-real-trigger sale by this
         desk's own doctrine, and opportunity cost is not one of the three
         real triggers — so it is surfaced, never sold.
    """
    if not _rotation_execution_enabled(pipeline):
        return
    from src.models import TargetPosition
    from src.rotation import RotationPrecheck, rotation_proposal_reason

    precheck = getattr(
        getattr(pipeline, "portfolio_manager", None), "last_rotation_precheck", None,
    )
    if not isinstance(precheck, RotationPrecheck) or precheck.opportunity is None:
        return  # nothing was surfaced this session — nothing to act on
    opportunity = precheck.opportunity
    held_symbol = opportunity.held_symbol.strip().upper()
    new_symbol = opportunity.new_symbol.strip().upper()

    if opportunity.tier == "ranked_margin":
        # Board item 39. Executable only behind its own second switch, and
        # even then this only PROPOSES the close: the sale is withdrawn
        # again in `ExecutionStage._run_session` unless the replacement BUY
        # clears every gate in `REQUIRED_BUY_LEG_GATES` against projected
        # post-sale state. Nothing here can put it on the wire.
        if not _rotation_ranked_margin_enabled(pipeline):
            _rotation_skip(
                pipeline, ctx, opportunity, "ranked_margin_tier_not_enabled",
            )
            return
    elif opportunity.tier != "ineligible_hold":
        _rotation_skip(
            pipeline, ctx, opportunity, "unknown_rotation_tier",
            tier_seen=str(opportunity.tier),
        )
        return

    held = next(
        (p for p in (positions or []) if (p.symbol or "").upper() == held_symbol),
        None,
    )
    if held is None or held.qty <= 0:
        _rotation_skip(
            pipeline, ctx, opportunity, "held_symbol_is_not_a_long_position",
            qty=getattr(held, "qty", None),
        )
        return

    targets = list(getattr(portfolio_decision, "targets", None) or [])
    new_targeted = any(
        t.symbol.upper() == new_symbol and not t.is_close for t in targets
    )
    if not new_targeted:
        _rotation_skip(
            pipeline, ctx, opportunity, "pm_did_not_target_new_candidate",
        )
        return
    if any(t.symbol.upper() == held_symbol for t in targets):
        _rotation_skip(
            pipeline, ctx, opportunity, "pm_already_targets_held_symbol",
        )
        return

    # 6. In flight? Read from the desk's own durable state machine. Any
    # failure to answer is a refusal to act, never an assumption of "clear".
    try:
        today_rows = pipeline.db.get_trades(
            symbol=held_symbol, limit=50, today_only=True,
        )
        bought_today = any(
            str(r.get("action") or "").upper() == "BUY" for r in today_rows
        )
        in_flight_rows = [
            r for r in today_rows
            if str(r.get("fill_status") or "").lower() in _IN_FLIGHT_FILL_STATUSES
            and str(r.get("action") or "").upper() != "HOLD"
        ]
        pending_restores = [
            r for r in pipeline.db.get_pending_protection_restores()
            if str(r.get("symbol") or "").upper() == held_symbol
        ]
        pending_repegs = [
            r for r in pipeline.db.get_pending_repegs()
            if str(r.get("symbol") or "").upper() == held_symbol
        ]
    except Exception as exc:  # noqa: BLE001
        _rotation_skip(
            pipeline, ctx, opportunity, "in_flight_check_failed", detail=str(exc),
        )
        return
    if bought_today:
        _rotation_skip(pipeline, ctx, opportunity, "held_symbol_bought_today")
        return
    if in_flight_rows:
        _rotation_skip(
            pipeline, ctx, opportunity, "order_in_flight_on_held_symbol",
            detail="; ".join(
                f"{r.get('action')}:{r.get('fill_status')}:{r.get('broker_order_id')}"
                for r in in_flight_rows
            )[:400],
        )
        return
    if pending_restores:
        _rotation_skip(
            pipeline, ctx, opportunity, "sell_already_in_flight_wal_row",
            detail=str(pending_restores[0].get("sell_order_id")),
        )
        return
    if pending_repegs:
        _rotation_skip(
            pipeline, ctx, opportunity, "entry_repeg_in_flight",
            detail=str(pending_repegs[0].get("old_order_id")),
        )
        return

    # 7. Item-25 holding discipline: is the position still structurally
    # protected? Same method, same inputs `RiskStage` uses.
    hist = (position_history or {}).get(held_symbol) or (
        position_history or {}
    ).get(held.symbol) or {}
    try:
        protection = pipeline._structural_protection_for_holding(
            symbol=held_symbol,
            thesis_invalid_if=hist.get("thesis_invalid_if"),
            entry_price=hist.get("entry_price"),
            stop_loss=hist.get("stop_loss"),
            is_short=False,
            run_id=ctx.run_id,
        )
    except Exception as exc:  # noqa: BLE001
        _rotation_skip(
            pipeline, ctx, opportunity, "protection_check_failed", detail=str(exc),
        )
        return
    if protection.protected:
        _rotation_skip(
            pipeline, ctx, opportunity, "held_symbol_structurally_protected",
            protection_basis=protection.basis,
            protection_detail=str(protection.detail)[:400],
        )
        return

    # A PROPOSAL, not an authorisation — see `rotation_proposal_reason`.
    # For the categorical tier this is byte-for-byte the string
    # `rotation_sell_reason` produced before; for the ranked-margin tier it
    # says in the Risk Manager's own input that the close is contingent on
    # the replacement BUY clearing its execution gates.
    reason = rotation_proposal_reason(
        opportunity,
        protection_basis=protection.basis,
        protection_detail=str(protection.detail),
        headroom_pct=precheck.headroom_pct,
        ceiling_pct=precheck.ceiling_pct,
        floor_pct=precheck.floor_pct,
        # 2026-09-23: so the clause naming why there was no room states the
        # limit that actually bound. Without this the sale's own reason
        # claims 14.50% is "under the 0.50% minimum" on a funding-bound
        # rotation — false, on the record the Risk Manager reads.
        binding=tuple(precheck.binding or ()),
        entry_budget_usd=precheck.entry_budget_usd,
        min_order_usd=precheck.min_order_usd,
    )
    # A zero-size target IS this desk's "close it" instruction
    # (`TargetPosition.is_close`; `_build_sell` turns it into a full SELL).
    # `thesis_invalid_if` is carried from the position's own entry record
    # so the built order's reasoning shows the condition the desk was
    # holding it against, exactly as a PM-authored close would.
    portfolio_decision.targets.append(TargetPosition(
        symbol=held_symbol,
        direction="long",
        risk_allocation_pct=0.0,
        conviction="high",
        thesis=reason,
        thesis_invalid_if=str(hist.get("thesis_invalid_if") or ""),
    ))
    ctx.rotation = {
        "held_symbol": held_symbol,
        "new_symbol": new_symbol,
        # Board item 39: the execution stage branches on this. A rotation
        # dict without it is treated as the categorical tier, which is what
        # every pre-item-39 caller meant.
        "tier": opportunity.tier,
        # Minted (or not) by `_rotation_sell_gate`, immediately before
        # the close is submitted. `None` here is not a
        # default that decays open: the SELL loop refuses a ranked-margin
        # rotation sale outright unless a real `RotationClearance` is
        # sitting in this slot.
        "clearance": None,
        "opportunity": opportunity,
        "protection_basis_text": protection.basis,
        "protection_detail_text": str(protection.detail),
        "floor_pct": float(precheck.floor_pct),
        # Carried onto the context so the SELL built at the wire states the
        # same binding constraint the PROPOSAL did — the two must not
        # disagree about why the room was gone.
        "binding": tuple(precheck.binding or ()),
        "entry_budget_usd": precheck.entry_budget_usd,
        "min_order_usd": precheck.min_order_usd,
        "new_score": float(opportunity.new_score),
        "held_reasons": list(opportunity.reasons),
        "protection_basis": protection.basis,
        "protection_detail": str(protection.detail),
        "headroom_pct": float(precheck.headroom_pct),
        "ceiling_pct": float(precheck.ceiling_pct),
        "reason": reason,
    }
    logger.warning(
        "Rotation: proposing a full close of %s to free room for %s — %s",
        held_symbol, new_symbol, reason,
    )
    _record_pipeline_event(
        pipeline, ctx, held_symbol, "rotation", "proposed", reason,
        new_symbol=new_symbol, new_score=float(opportunity.new_score),
        held_reasons=list(opportunity.reasons),
        protection_basis=protection.basis,
        protection_detail=str(protection.detail)[:400],
        headroom_pct=float(precheck.headroom_pct),
        ceiling_pct=float(precheck.ceiling_pct),
        tier=opportunity.tier,
    )


def _projected_sale_qty(decision, position) -> float:
    """How many shares `decision` will actually take off `position`.

    Mirrors `ExecutionStage._run_session`'s own SELL sizing exactly —
    whole-share flooring on a whole-share position, the `>= position` full-
    exit promotion, the `allocation_pct == 0` ambiguity skip — because a
    projection that sized a sale differently from the loop that places it
    would be describing a book that never exists. Returns 0.0 for anything
    the loop would skip.
    """
    try:
        held_qty = float(getattr(position, "qty", 0.0) or 0.0)
    except (TypeError, ValueError):
        return 0.0
    # The action and the position's SIGN have to agree, exactly as the two
    # execution loops require. The SELL loop refuses a SELL on a short
    # (`existing[0].qty <= 0: continue`) and the COVER loop refuses a COVER
    # on a long — so a projection that closed either one would remove
    # exposure the real session keeps, and a book that keeps a losing
    # position the projection dropped is more negative than the projection
    # said. Both directions are unsafe; both are refused here.
    #
    # A blanket `abs()` was the over-correction of the opposite bug, where
    # sizing off the SIGNED quantity made every COVER a no-op.
    covering = str(getattr(decision, "action", "") or "").upper() == "COVER"
    if covering and held_qty >= 0:
        return 0.0  # a COVER on a long: the COVER loop skips it
    if not covering and held_qty <= 0:
        return 0.0  # a SELL on a short: the SELL loop skips it
    held_qty = abs(held_qty)
    if held_qty <= 0:
        return 0.0
    try:
        pct = float(getattr(decision, "allocation_pct", 0.0) or 0.0)
    except (TypeError, ValueError):
        return 0.0
    if pct == 0:
        # The loop logs this as ambiguous and skips it, so nothing is sold.
        return 0.0
    if 0 < pct < 100:
        qty = held_qty * (pct / 100.0)
        if float(held_qty).is_integer():
            qty = max(1.0, float(int(qty)))
        if qty <= 0:
            return 0.0
        return min(qty, held_qty)
    return held_qty


def _projected_post_sale_book(positions, total_value: float,
                              sell_decisions: list, cover_decisions: list):
    """The held book and the equity as they will be once THIS SESSION'S
    exits have gone through — the state the downstream gates will actually
    read, projected before any of them has been submitted.

    Board item 39. The gates that can refuse a rotation's replacement BUY
    are measured from the book the desk will be holding once the sale has
    gone through, not the one it holds while proposing it: the deployment
    budget reads the remaining positions' gross exposure and the remaining
    settled cash, and the sizing reads the equity those are measured
    against. A check fed PRE-sale state is blind to the state change it
    depends on, which is exactly why attempt 2 on this item was unsafe by
    construction, and why this builds the post-sale book instead of
    re-implementing the gates against the pre-sale one.

    **Exactly the exits it is handed are applied, and no others.** The
    rotation gate hands it ONE decision — the rotation's own close — and
    relies on a fresh `_refresh_account_state()` read for everything else
    the session has already done. Projecting the other exits would be
    guessing at fills nobody controls; measuring them is free, because the
    rotation's close is ordered last.

    Returns `(projected_positions, equity_for_weights)`.

      * `equity_for_weights` is `total_value` LESS the concession the
        marketable limits give up against the marks (0.995 for a SELL, its
        1.005 COVER mirror). A sale is otherwise mark-to-market neutral: it
        converts a marked position into the cash that position was already
        marked at, so the book's composition changes and its total does
        not. The concession is the one real equity effect of executing, it
        is knowable, and it is subtracted rather than ignored.
        **Direction, measured rather than assumed (adversary review,
        2026-09-23).** `config/settings.yaml` ships `allow_margin: true`, so
        `_entry_deployment_budget` returns `ceiling_x * equity - held_gross`
        and never reads cash at all. The order ceiling therefore falls by
        between 0.65 and 2.0 times any reduction in equity (the §11.2 rung
        and `max_position_pct: 65`), while the replacement's estimated cost
        falls only by the allocation percentage of it. Subtracting the
        concession TIGHTENS this gate; leaving it out loosens it. An
        earlier version of this docstring asserted the opposite and was
        wrong — it reasoned about the settled-cash branch, which the
        shipped configuration does not execute.

    Cover decisions are applied the same way: a COVER closes a short, which
    also removes that name from the held book.
    """
    from src.rotation import ROTATION_MARGIN_PCT  # noqa: F401  (module sanity)

    by_symbol = {}
    for decision in list(sell_decisions or []) + list(cover_decisions or []):
        symbol = str(getattr(decision, "symbol", "") or "").strip().upper()
        if symbol:
            by_symbol.setdefault(symbol, decision)

    projected = []
    concession = 0.0
    for position in positions or []:
        symbol = str(getattr(position, "symbol", "") or "").strip().upper()
        decision = by_symbol.get(symbol)
        if decision is None:
            projected.append(position)
            continue
        held_qty = float(getattr(position, "qty", 0.0) or 0.0)
        sold = _projected_sale_qty(decision, position)
        if sold <= 0:
            projected.append(position)
            continue
        try:
            price = float(getattr(position, "current_price", 0.0) or 0.0)
        except (TypeError, ValueError):
            price = 0.0
        if price > 0:
            # What the marketable limit gives up against the mark if it
            # fills at the limit. A SELL rests BELOW the mark and a COVER
            # BUYS back ABOVE it, so the cushion is applied in opposite
            # directions and costs the account in both. Rounded the same
            # way the loops round it so the two cannot drift.
            covering = str(getattr(decision, "action", "") or "").upper() == "COVER"
            # The SAME two factors `ExecutionStage._run_session` prices its
            # own exits at — 0.995 for a SELL, its 1.005 mirror for a COVER
            # (board item 138, `config/number_ledger.yaml`). No new number:
            # a projection that priced an exit differently from the loop
            # that places it would be describing a fill that never happens.
            limit = round(price * (1.005 if covering else 0.995), 2)
            concession += abs(limit - price) * abs(sold)
        remaining = abs(held_qty) - abs(sold)
        if remaining <= 0 or held_qty == 0:
            continue  # position gone
        projected.append(_scaled_position(position, remaining / abs(held_qty)))

    return projected, max(0.0, float(total_value) - concession)


def _scaled_position(position, remaining_fraction: float):
    """A copy of `position` holding `remaining_fraction` of what it holds.

    Used only for a PARTIAL exit. Quantity and market value scale by the
    fraction, because selling half a position leaves half of it on the
    books, and `gross_exposure` — which is what the deployment budget
    measures the remaining book with — reads market value. The P&L fields
    are scaled with them for consistency of the object rather than because
    any gate now reads them: the projected account day-change that used to
    read `unrealized_intraday_pnl` went with the account-level loss halt
    (PR #584, retired-ok). Any field this does not know about is carried
    through unchanged.

    `copy.copy` rather than a constructor call: `Position` is not stable
    across this repo's fixtures (several tests use simple stand-ins), and a
    projection helper must not be the thing that decides what a position
    class looks like.
    """
    import copy
    clone = copy.copy(position)
    for field_name in ("qty", "market_value", "unrealized_intraday_pnl",
                       "unrealized_pnl", "cost_basis"):
        value = getattr(clone, field_name, None)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        try:
            setattr(clone, field_name, float(value) * remaining_fraction)
        except Exception:  # noqa: BLE001
            # A frozen or property-backed field: leave it. The numerator
            # reads `unrealized_intraday_pnl`, and a field that could not
            # be scaled down is left at its FULL value, which overstates
            # the remaining book rather than understating it.
            continue
    return clone


def _projected_post_sale_cash(cash: float, positions, sell_decisions,
                              cover_decisions) -> float:
    """Settled cash once this session's exits have gone through — a LOWER
    bound, deliberately.

    Called with the rotation's own close and nothing else — every other
    exit is already reflected in the `cash` this is handed, because that
    number comes from a broker read taken after they ran.

    A SELL adds its limit proceeds; a COVER SPENDS cash to buy the borrowed
    shares back, so it is subtracted. The cash sweep is not modelled at
    all: it can only liquidate the park INTO cash, never out of it, so
    leaving it out can only understate what is deployable. Understating
    refuses a rotation that would have worked; overstating sells a position
    to fund an order that is then refused.

    This is live on the settled-cash branch of `_entry_deployment_budget`
    only — the ladder branch compares gross exposure and never reads cash.
    That branch is reached whenever the gross ceiling cannot be resolved,
    and whenever `allow_margin` is set back to false.
    """
    try:
        cash = float(cash or 0.0)
    except (TypeError, ValueError):
        cash = 0.0
    by_symbol = {}
    for decision in list(sell_decisions or []) + list(cover_decisions or []):
        symbol = str(getattr(decision, "symbol", "") or "").strip().upper()
        if symbol:
            by_symbol.setdefault(symbol, decision)
    for position in positions or []:
        symbol = str(getattr(position, "symbol", "") or "").strip().upper()
        decision = by_symbol.get(symbol)
        if decision is None:
            continue
        sold = _projected_sale_qty(decision, position)
        if sold <= 0:
            continue
        try:
            price = float(getattr(position, "current_price", 0.0) or 0.0)
        except (TypeError, ValueError):
            continue
        if price <= 0:
            continue
        covering = str(getattr(decision, "action", "") or "").upper() == "COVER"
        limit = round(price * (1.005 if covering else 0.995), 2)
        cash += (-1.0 if covering else 1.0) * limit * abs(sold)
    return cash


def _projected_entry_cost(decision, equity: float, *,
                          budget_is_gross: bool) -> float:
    """What an entry earlier in the same session will take out of the
    deployment pool before the rotation's own buy reaches it.

    A SHORT is never SIZED by the entry budget (D11), but it still DRAWS
    the pool whenever that pool is the ladder's gross headroom rather than
    settled cash — the submit loop's own rule is
    `if budget_is_gross or not is_short: entry_budget -= estimated_cost`,
    because a short occupies gross exactly as a long does. Excluding it
    outright over-stated the pool by the whole short, which is the unsafe
    direction: the projection clears, reality refuses, and the position has
    already been sold.

    The charge is the full allocation, which is an UPPER bound on the real
    draw (the submit loop takes `min(qty_by_alloc, qty_by_risk)` and every
    later adjustment moves the quantity down, and it only draws at all once
    the broker accepts). Over-charging shrinks the pool, which refuses a
    rotation that would have worked — the side to be wrong on.
    """
    if (str(getattr(decision, "action", "") or "").upper() == "SHORT"
            and not budget_is_gross):
        return 0.0
    try:
        allocation_pct = float(getattr(decision, "allocation_pct", 0.0) or 0.0)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, float(equity) * allocation_pct / 100.0)


def _rotation_buy_leg_projected_refusal(pipeline, ctx, *, rotation,
                                        buy_decision, positions,
                                        total_value: float,
                                        rotation_sell,
                                        cash: float = 0.0,
                                        buy_decisions_before: list | None = None):
    """Would the rotation's replacement BUY be refused downstream, judged
    against the book as it will be AFTER this session's exits?

    Returns `(clearance, reason, detail)`: exactly one of `clearance` (a
    `src.rotation.RotationClearance`) and `reason` is not None.

    Board item 39. Every gate below is the SAME computation the execution
    stage runs later, fed projected post-sale inputs — not a second
    implementation of it.

    The list below is FIVE long and so is `REQUIRED_BUY_LEG_GATES`; a
    `gate_coverage_incomplete` refusal at the bottom of this function is
    what keeps the two from drifting, and this paragraph is the third copy
    of the count, so change all three together. It was six until
    2026-09-23, when the owner's removal of the account-level loss halt
    (PR #584) deleted the `daily_loss_recheck` refusal (retired-ok) the
    first gate anticipated. Nothing replaced it: the gross exposure that
    halt shared with the §11.2 ladder is still gated, by the two funding
    gates.

      * `no_price` / `stale_entry` — `_live_fill_price` and the same 5%
        deviation test the preflight applies. Neither depends on the sale
        at all, so these are exact now, not projected.
      * `qty_zero` — the preflight's own sizing helpers (`_size_shares`,
        `_qty_by_risk_budget`, `_fractional_sizing_allowed`) at the
        projected equity.
      * `insufficient_cash` / `below_min_notional` — `_entry_deployment_budget`
        over the projected post-sale positions, equity and settled cash,
        drained by every entry earlier in the same session exactly as the
        submit loop drains it. This is the pair that still carries the
        §11.2 gross ladder: the budget's ladder-backed branch measures the
        headroom the sale frees, so a rotation that would breach gross is
        refused here even though the account-level alarm is gone.

    **What this does NOT close, stated plainly.** The BUY submit loop can
    still refuse an entry for reasons no pre-check can evaluate in advance:
    the latency window (`latency_window`), an unfillable marketable limit
    against the NBBO at submit time, the borrow gate on a SHORT, and any
    broker rejection. Those are not knowable before the sale, and the tape
    can also move between this projection and the real check. This gate
    removes the deterministic, knowable refusals — the ones that made the
    naked-sale outcome reproducible — and the residual is recorded in the
    PR and in `docs/INCIDENT_HISTORY.md` rather than papered over.
    """
    from src.rotation import REQUIRED_BUY_LEG_GATES, RotationClearance

    symbol = str(getattr(buy_decision, "symbol", "") or "").strip().upper()
    checked: list[str] = []

    # ONE projection, over the rotation's own close and nothing else.
    #
    # An earlier version projected the other exits this session too, and
    # then tried to bound the uncertainty by evaluating two books — "all
    # exits fill" and "only the rotation's fills". That is not a bound: the
    # worst case is per-position (an exit carrying an intraday LOSS fails
    # to fill while one carrying a GAIN fills), and that mixed book is
    # neither of the two. Sampling two points of 2^N and calling it
    # conservative is the same mistake as attempt 2, one level up.
    #
    # It is also unnecessary. This gate now runs from inside the SELL loop,
    # immediately before the rotation's own close is submitted, and the
    # rotation's close is ordered LAST among this session's exits. By the
    # time it is reached every other exit has a terminal status and the
    # account has been refreshed, so the other exits are a MEASUREMENT in
    # `positions`, not an assumption. The only thing left to project is the
    # one sale that has not happened yet — which is the thing a projection
    # is actually good for.
    # `positions` is a broker read taken after every other SELL this
    # session reached a terminal status, so everything else the session did
    # is already in it. A session with a COVER still pending never reaches
    # this function at all (`_pending_cover_symbols`), so the only thing
    # left to project is the one sale that has not happened yet.
    projected_positions, equity_for_weights = _projected_post_sale_book(
        positions, total_value,
        [rotation_sell] if rotation_sell is not None else [], [],
    )

    # --- gates 1/2: price and entry staleness, exact now -----------------
    checked.append("no_price")
    market_price = _live_fill_price(pipeline, symbol)
    if not isinstance(market_price, (int, float)) or isinstance(
        market_price, bool,
    ) or market_price <= 0:
        return None, "no_price", (
            "no verifiable live price for the replacement buy (daily bar "
            "close is not a fill reference)"
        )
    market_price = float(market_price)
    # docs/WORK.md item 120: the SHARE COUNT divides the dollar allocation by
    # the sizing price, so it must be a real TODAY PRINT, never a quote mid
    # or a prior-session trade. `market_price` above (the fill reference) may
    # be a quote mid by design; the sizing divisor may not. Folded into the
    # `no_price` gate so `REQUIRED_BUY_LEG_GATES` coverage is unchanged.
    sizing_print = _today_sizing_price(pipeline, symbol)
    if not isinstance(sizing_print, (int, float)) or isinstance(
        sizing_print, bool,
    ) or sizing_print <= 0:
        return None, "no_price", (
            "no today trade print to size the replacement buy against (a "
            "quote mid or a prior-session price is not a sizing reference) — "
            "refused rather than sized on a bad price"
        )
    sizing_print = float(sizing_print)

    checked.append("stale_entry")
    try:
        entry_price = float(getattr(buy_decision, "entry_price", 0.0) or 0.0)
    except (TypeError, ValueError):
        entry_price = 0.0
    if entry_price > 0:
        deviation = abs(entry_price - market_price) / market_price
        if deviation > 0.05:
            return None, "stale_entry", (
                f"entry ${entry_price:.2f} is {deviation * 100:.1f}% from "
                f"market ${market_price:.2f} (threshold 5%)"
            )

    # --- gate 3: does the replacement round to a tradeable size? ---------
    checked.append("qty_zero")
    # Size off the TODAY PRINT (item 120), bounded conservatively by the
    # already-approved entry — never off the fill-reference mid.
    sizing_price = max(sizing_print, entry_price or 0.0)
    is_short = getattr(buy_decision, "action", "BUY") == "SHORT"
    fractional = _fractional_sizing_allowed(
        pipeline, symbol, is_short=is_short,
    )
    try:
        allocation_pct = float(
            getattr(buy_decision, "allocation_pct", 0.0) or 0.0,
        )
    except (TypeError, ValueError):
        allocation_pct = 0.0
    qty = _size_shares(
        pipeline,
        (float(equity_for_weights) * allocation_pct / 100.0) / sizing_price,
        fractional=fractional,
    )
    if qty <= 0:
        return None, "qty_zero", (
            f"allocation {allocation_pct:.2f}% at ${sizing_price:.2f} "
            f"rounds to zero shares"
        )
    risk_qty = _qty_by_risk_budget(
        pipeline, total_value=float(equity_for_weights),
        sizing_price=sizing_price,
        stop_price=getattr(buy_decision, "stop_loss", 0.0),
        is_short=is_short, fractional=fractional,
    )
    if risk_qty is not None and risk_qty < qty:
        qty = risk_qty
    if qty <= 0:
        return None, "qty_zero", (
            f"risk budget at ${sizing_price:.2f} entry / "
            f"${getattr(buy_decision, 'stop_loss', 0.0)} stop rounds to zero "
            f"shares"
        )

    # --- gates 4/5: can the post-sale book actually FUND the replacement? -
    # Added after adversary review of attempt 3. These are the refusals a
    # rotation is MOST likely to hit, because a rotation only surfaces when
    # the risk headroom is already under the floor — and risk-based sizing
    # can ask for more notional than the sale frees whenever the new name's
    # stop is tighter than the old one's. Both are deterministic functions
    # of the post-sale book, so both belong here rather than in the
    # "unknowable" residual.
    #
    # The budget is deliberately a LOWER BOUND: it is measured on the
    # projected book with the projected cash and WITHOUT the cash sweep's
    # help. The sweep can only add deployable cash, never remove it, so a
    # replacement that fits here fits the real budget too. Being wrong in
    # this direction refuses a rotation that would have worked, which costs
    # an opportunity; being wrong the other way sells a position to fund an
    # order that is then refused, which costs the position.
    checked.append("insufficient_cash")
    checked.append("below_min_notional")
    projected_cash = _projected_post_sale_cash(
        cash, positions,
        [rotation_sell] if rotation_sell is not None else [], [],
    )
    try:
        entry_budget, ladder_backed, budget_note = _entry_deployment_budget(
            pipeline, ctx, projected_positions, float(equity_for_weights),
            projected_cash,
        )
    except Exception as exc:  # noqa: BLE001
        return None, "insufficient_cash", (
            f"the post-sale entry budget could not be measured ({exc}), so "
            f"the replacement buy cannot be cleared"
        )
    # Earlier entries in the same session drain the pool before this one
    # reaches it — the submit loop subtracts each order's cost as it goes,
    # so the projection walks the same order.
    for earlier in buy_decisions_before or []:
        entry_budget -= _projected_entry_cost(
            earlier, float(equity_for_weights),
            budget_is_gross=bool(ladder_backed),
        )
    single_name_cap = _single_name_execution_cap(
        pipeline, float(equity_for_weights),
    )
    order_ceiling = min(entry_budget, single_name_cap)
    estimated_cost = qty * sizing_price
    if not is_short and estimated_cost > order_ceiling:
        affordable_qty = _size_shares(
            pipeline, order_ceiling / sizing_price, fractional=fractional,
        )
        if affordable_qty <= 0:
            return None, "insufficient_cash", (
                f"estimated cost ${estimated_cost:.2f} exceeds the "
                f"${order_ceiling:.2f} deployable on the post-sale book "
                f"({budget_note})"
            )
        resized_cost = min(qty, affordable_qty) * sizing_price
        floor_usd = _min_order_usd(pipeline)
        if resized_cost < floor_usd:
            return None, "below_min_notional", (
                f"${order_ceiling:.2f} deployable on the post-sale book "
                f"re-sizes the order to ${resized_cost:.2f}, below the "
                f"${floor_usd:,.0f} minimum worth trading"
            )

    missing = [g for g in REQUIRED_BUY_LEG_GATES if g not in checked]
    if missing:
        # Unreachable while this function and `REQUIRED_BUY_LEG_GATES` agree.
        # It is here so that they cannot silently stop agreeing: a gate added
        # to the list and not to this function refuses the sale rather than
        # clearing it on a check that was never run.
        return None, "gate_coverage_incomplete", (
            f"gates not evaluated: {', '.join(missing)}"
        )
    clearance = RotationClearance(
        held_symbol=str(rotation.get("held_symbol") or ""),
        new_symbol=symbol,
        gates_checked=tuple(checked),
        projected_entry_budget=float(order_ceiling),
        # Truncated: this string reaches `rotation_sell_reason`, whose text
        # is cut at `ROTATION_REASON_MAX_CHARS` with the clearance clause
        # LAST, so an over-long note would delete the very thing it
        # documents.
        projected_budget_basis=str(budget_note)[:60],
        projected_positions=tuple(
            str(getattr(p, "symbol", "") or "").strip().upper()
            for p in projected_positions
        ),
        projected_equity=float(equity_for_weights),
    )
    return clearance, None, None


def _pending_cover_symbols(cover_decisions, positions) -> tuple[str, ...]:
    """The symbols this session will actually COVER when the rotation's
    close is gated, in the order they will be covered.

    **A cover that cannot move the book does not count.** The COVER loop
    refuses two classes outright — a COVER against a symbol that is not
    held short (`existing[0].qty >= 0`), and one with
    `allocation_pct == 0` — and both take zero shares off the book, change
    no intraday P&L and move no gross. Counting them would refuse the
    rotation for a cause with no effect, every session the PM keeps
    proposing that dead cover, which is a statement about the desk rather
    than about the market. `_projected_sale_qty` applies the same sign and
    allocation agreement the COVER loop itself applies, so the filter here
    and the loop there cannot drift apart.

    Board item 39. The COVER loop runs AFTER the SELL loop, so at gate time
    no cover has happened and none can be measured — unlike the other
    SELLs, which the reorder makes measurable.

    **This refusal has outlived the argument that produced it, and that is
    recorded here rather than papered over (adversary review, 2026-09-23).**
    It was adopted because one projected book could not be the worst case
    for three disagreeing consumers: the daily-loss numerator, the
    volatility-relative threshold, and gross exposure. The owner's removal
    of the account-level loss halt (PR #584) deleted the first two. The
    survivor, `_entry_deployment_budget`, turns out to be cheaply boundable
    in both of its regimes: with `allow_margin: true` — what ships — it
    returns `ceiling_x * equity - held_gross` and never reads cash, a COVER
    lowers held gross, so simply NOT applying any cover is already the
    minimum headroom; with margin off it returns `min(headroom, cash)` and
    both terms are monotone in the set of covers assumed, so the lower
    bound over every fill outcome is `min(headroom with no covers, cash
    with all covers)` — two scalars, no enumeration.

    The refusal is kept anyway, and on one ground only: it is strictly the
    more conservative posture, `execution.rotation_ranked_margin_enabled`
    ships FALSE, and loosening a live-selling gate is not something to do
    in the same change that resolves a merge. Whoever turns the flag on
    should take the bound above instead of inheriting this. What must NOT
    be inherited is the old justification, which claimed the bound was
    impossible; it is not, and saying so kept a refusal standing on a
    reason that no longer exists.

    So today the desk does not guess: a rotation is refused outright on any
    session with a cover pending. That costs a rotation on cover days and
    fails toward not trading, which is the direction every other guard on
    this path fails in.
    """
    by_symbol = {
        str(getattr(p, "symbol", "") or "").strip().upper(): p
        for p in (positions or [])
    }
    symbols = []
    for decision in cover_decisions or []:
        symbol = str(getattr(decision, "symbol", "") or "").strip().upper()
        if not symbol or symbol in symbols:
            continue
        position = by_symbol.get(symbol)
        if position is None:
            continue  # nothing held: the COVER loop skips it
        if _projected_sale_qty(decision, position) <= 0:
            continue  # a no-op cover: it cannot move anything to bound
        symbols.append(symbol)
    return tuple(symbols)


def _rotation_sell_last(sell_decisions: list, ctx) -> list:
    """This session's SELLs with a RANKED-MARGIN rotation's close moved to
    the END, and every other order preserved.

    Board item 39. The rotation's close is the only exit whose paired BUY
    can be refused for lack of the room the close frees, so it is the only
    one that must not be submitted until that question is answered — and
    the question is easiest to answer once every OTHER exit has a terminal
    status and the account has been re-read. Going last is what turns the
    other exits from something to project into something to measure.

    A no-op on every session without a ranked-margin rotation, which is
    every session while `execution.rotation_ranked_margin_enabled` is off.
    """
    rotation = getattr(ctx, "rotation", None)
    if not isinstance(rotation, dict) or rotation.get("tier") != "ranked_margin":
        return sell_decisions
    held = str(rotation.get("held_symbol") or "").strip().upper()
    if not held:
        return sell_decisions
    others = [
        d for d in sell_decisions
        if str(getattr(d, "symbol", "") or "").strip().upper() != held
    ]
    rotation_legs = [
        d for d in sell_decisions
        if str(getattr(d, "symbol", "") or "").strip().upper() == held
    ]
    return others + rotation_legs


def _rotation_sell_gate(pipeline, ctx, decision, buy_decisions, positions,
                        total_value: float, cash: float,
                        cover_decisions: list | None = None):
    """Board item 39 — may this RANKED-MARGIN rotation close be submitted?

    Returns `None` for any SELL that is not a ranked-margin rotation close;
    every pre-item-39 path lands there and is unaffected. Otherwise returns
    `(cleared, positions, total_value, cash)`, with a REFRESHED book the
    caller must adopt — clearing the gate against a fresh book while the
    loop then sizes and prices off a stale one would reintroduce, one
    statement later, exactly the divergence `_projected_sale_qty` exists to
    prevent.

    **Why it lives inside the SELL loop rather than ahead of it.** The
    rotation's close is ordered last (`_rotation_sell_last`), so by the
    time this runs every other SELL this session has been submitted and
    waited on to a terminal status. Re-reading the account here therefore
    MEASURES what those did instead of assuming it.

    COVERs are the exception: their loop runs AFTER this one, so no cover
    has happened, none can be measured, and — see `_pending_cover_symbols`
    — none can honestly be bounded either. A session with a cover pending
    refuses the rotation outright.

    An earlier version ran ahead of the whole loop and projected the other
    exits too, bounding the uncertainty by evaluating two books ("all exits
    fill" and "only this one fills"). That is not a bound — the worst case
    is per-position and is neither book — and it also put this gate in
    direct conflict with `apply_gross_ceiling`, which nets out EVERY
    planned exit when it sizes the replacement. Measuring removes both
    problems.

    `cleared=False` means: do not submit this close. The replacement BUY
    then dies on the existing `_drop_rotation_buy_if_room_not_freed` path,
    because `rotation["sell_order_id"]` is never set — the room it was
    granted was never freed. Both legs fall together and the desk simply
    keeps the position.

    Scope: the RANKED-MARGIN tier only. The categorical tier
    (`ineligible_hold`, already live) sells a holding that fails the desk's
    own entry rules today and whose structural protection has already
    broken — a sale this desk's doctrine independently supports, so it
    stands on its own and nothing here touches it.
    """
    rotation = getattr(ctx, "rotation", None)
    if not isinstance(rotation, dict) or rotation.get("tier") != "ranked_margin":
        return None
    held_symbol = str(rotation.get("held_symbol") or "").strip().upper()
    new_symbol = str(rotation.get("new_symbol") or "").strip().upper()
    if str(getattr(decision, "symbol", "") or "").strip().upper() != held_symbol:
        return None

    def _withdraw(reason: str, detail: str):
        logger.warning(
            "Rotation withdrawn BEFORE the sell (%s): %s. %s is NOT sold — "
            "the desk keeps the position rather than going naked.",
            reason, detail, held_symbol,
        )
        _record_pipeline_event(
            pipeline, ctx, held_symbol, "rotation", "withdrawn", reason,
            new_symbol=new_symbol, detail=str(detail)[:400],
            tier="ranked_margin",
        )
        _record_execution_skip(
            pipeline, ctx, held_symbol, "rotation_withdrawn", str(detail)[:400],
        )
        # `ctx.rotation` is MARKED withdrawn, not cleared. Clearing it would
        # make `_drop_rotation_buy_if_room_not_freed` a no-op, and the
        # replacement BUY — which the constructor sized on the premise that
        # this close frees room — would then go out against room that was
        # never freed, putting the book over the risk ceiling. Leaving the
        # dict in place with no `sell_order_id` is exactly the state that
        # function already reads as "not submitted, no room freed".
        rotation["withdrawn"] = reason
        rotation["clearance"] = None

    pending_covers = _pending_cover_symbols(cover_decisions, positions)
    if pending_covers:
        _withdraw(
            "cover_pending",
            f"this session still intends to cover "
            f"{', '.join(pending_covers)}, and the COVER loop runs after "
            f"this one — their effect on the daily-loss limit cannot be "
            f"measured yet and cannot be bounded either (the numerator, the "
            f"volatility-relative threshold and gross exposure each have a "
            f"different worst case). The desk does not guess: "
            f"{held_symbol} is kept.",
        )
        return False, positions, total_value, cash

    buy_leg = next(
        (d for d in (buy_decisions or [])
         if str(getattr(d, "symbol", "") or "").strip().upper() == new_symbol),
        None,
    )
    if buy_leg is None:
        _withdraw(
            "buy_leg_absent",
            f"the replacement buy of {new_symbol} is not in this session's "
            f"orders, so closing {held_symbol} would free room for nothing",
        )
        return False, positions, total_value, cash

    # Re-read the account. This is a measurement of everything the session
    # has already done, and the book the projection below starts from. A
    # read it cannot make is a refusal to act — the posture every other
    # guard on this path takes.
    try:
        account, positions, price_map = pipeline._refresh_account_state()
        total_value = float(
            account["portfolio_value"] if isinstance(account, dict)
            else getattr(account, "portfolio_value", total_value)
        )
        cash = float(
            account["cash"] if isinstance(account, dict)
            else getattr(account, "cash", cash)
        )
    except Exception as exc:  # noqa: BLE001
        _withdraw(
            "account_refresh_failed",
            f"close withheld: the post-sale book could not be projected "
            f"from a current account state ({exc})",
        )
        return False, positions, total_value, cash

    held = next(
        (p for p in (positions or [])
         if str(getattr(p, "symbol", "") or "").strip().upper() == held_symbol),
        None,
    )
    if held is None or float(getattr(held, "qty", 0.0) or 0.0) <= 0:
        # The refreshed book no longer holds it (a stop filled, a
        # broker-side close). The SELL loop would drop it silently two
        # statements below; a candidate must not leave this pipeline
        # without a durable, per-symbol reason.
        _withdraw(
            "held_position_gone",
            f"{held_symbol} is no longer held on a current broker read, so "
            f"there is nothing to close and no room to free for {new_symbol}",
        )
        return False, positions, total_value, cash

    clearance, reason, detail = _rotation_buy_leg_projected_refusal(
        pipeline, ctx, rotation=rotation, buy_decision=buy_leg,
        positions=positions, total_value=total_value, cash=cash,
        rotation_sell=decision,
        # The submit loop walks `buy_decisions` in order and subtracts each
        # order's cost from the pool as it goes, so everything ahead of the
        # rotation's own buy has already drawn the budget down by the time
        # it is reached.
        buy_decisions_before=list(
            buy_decisions[:buy_decisions.index(buy_leg)],
        ),
    )
    if clearance is None:
        _record_execution_skip(
            pipeline, ctx, new_symbol, str(reason),
            f"rotation buy leg refused on projected post-sale state: {detail}",
        )
        _withdraw(
            f"buy_leg_would_be_refused:{reason}",
            f"close withheld because the replacement buy of {new_symbol} "
            f"would be refused ({reason}: {detail})",
        )
        return False, positions, total_value, cash

    rotation["clearance"] = clearance
    _record_pipeline_event(
        pipeline, ctx, held_symbol, "rotation", "buy_leg_cleared",
        "projected_post_sale_gates_passed", new_symbol=new_symbol,
        gates=list(clearance.gates_checked),
        projected_entry_budget=clearance.projected_entry_budget,
        projected_budget_basis=clearance.projected_budget_basis,
        projected_positions=list(clearance.projected_positions),
        projected_equity=clearance.projected_equity,
        tier="ranked_margin",
    )
    return True, positions, total_value, cash


#: Sentinel returned by `_rotation_ranked_margin_sell_reason` when the SELL
#: must NOT be submitted. Distinct from `None`, which means "not a
#: ranked-margin rotation close — carry on as before".
_ROTATION_SELL_REFUSED = object()


def _rotation_ranked_margin_sell_reason(pipeline, ctx, decision):
    """The last barrier in front of a RANKED-MARGIN rotation close.

    Returns:
      * `None` — this SELL is not a ranked-margin rotation close. Every
        pre-item-39 path lands here and is unaffected.
      * a `str` — the close is permitted, and this is the reason built from
        the `RotationClearance` that permitted it.
      * `_ROTATION_SELL_REFUSED` — do not submit this SELL.

    Board item 39, and the reason it is written this way. Attempt 1 on this
    item took the structural barrier that made a ranked-margin sale
    impossible to BUILD and replaced it with a config boolean, so one truthy
    value anywhere in the settings chain was sufficient to put a real sale
    on the wire. The barrier here is `src/rotation.py::rotation_sell_reason`
    raising `ValueError` without a `RotationClearance` — an object carrying
    the projected post-sale numbers the replacement buy was cleared on, that
    no configuration can produce. This function reads no flag at all; it
    only asks whether the evidence exists, and refuses the sale when the
    answer raises.
    """
    from src.rotation import RotationOpportunity, rotation_sell_reason

    rotation = getattr(ctx, "rotation", None)
    if not isinstance(rotation, dict) or rotation.get("tier") != "ranked_margin":
        return None
    symbol = str(getattr(decision, "symbol", "") or "").strip().upper()
    if symbol != str(rotation.get("held_symbol") or "").strip().upper():
        return None
    opportunity = rotation.get("opportunity")
    if not isinstance(opportunity, RotationOpportunity):
        logger.error(
            "Rotation SELL of %s REFUSED: the run carries no rotation "
            "opportunity to build a reason from. The position is kept.",
            symbol,
        )
        _record_pipeline_event(
            pipeline, ctx, symbol, "rotation", "sell_refused",
            "no_opportunity_on_context", new_symbol=rotation.get("new_symbol"),
        )
        return _ROTATION_SELL_REFUSED
    try:
        return rotation_sell_reason(
            opportunity,
            protection_basis=str(rotation.get("protection_basis_text") or ""),
            protection_detail=str(rotation.get("protection_detail_text") or ""),
            headroom_pct=float(rotation.get("headroom_pct") or 0.0),
            ceiling_pct=float(rotation.get("ceiling_pct") or 0.0),
            floor_pct=float(rotation.get("floor_pct") or 0.0),
            clearance=rotation.get("clearance"),
            binding=tuple(rotation.get("binding") or ()),
            entry_budget_usd=rotation.get("entry_budget_usd"),
            min_order_usd=rotation.get("min_order_usd"),
        )
    except ValueError as exc:
        logger.error(
            "Rotation SELL of %s REFUSED at the wire: %s. The position is "
            "kept — the desk does not sell into an uncleared replacement.",
            symbol, exc,
        )
        _record_pipeline_event(
            pipeline, ctx, symbol, "rotation", "sell_refused",
            "no_clearance", new_symbol=rotation.get("new_symbol"),
            detail=str(exc)[:400],
        )
        _record_execution_skip(
            pipeline, ctx, symbol, "rotation_sell_refused", str(exc)[:400],
        )
        return _ROTATION_SELL_REFUSED


def _alert_rotation_executed(*, rotation: dict, qty: float, limit_price: float,
                             order_id: str | None) -> None:
    """Standalone owner alert: the desk closed a position ON ITS OWN.

    Same path and shape as the naked-position, re-peg-exhausted and
    holding-discipline-block alerts (`notifier.send_owner_alert`): its own
    Telegram message, never bundled into the run summary; severity in plain
    words, never colour alone. Fired the moment the sale is broker-accepted
    — that is the irreversible act, and a position sold automatically must
    never be silent. Never raises.
    """
    try:
        held = rotation["held_symbol"]
        new = rotation["new_symbol"]
        rules = "; ".join(rotation.get("held_reasons") or []) or "entry rules"
        body = (
            "POSITION CLOSED AUTOMATICALLY — OPPORTUNITY ROTATION\n"
            f"{held}: the desk submitted a SELL of {qty:g} share(s) at limit "
            f"${limit_price:,.2f} (broker order {order_id}) to free room for "
            f"{new}.\n"
            f"Why {held}: it fails the desk's own entry rules today ({rules}), "
            f"and its structural protection had already broken "
            f"({rotation.get('protection_basis')}).\n"
            f"Why {new}: best-ranked eligible new candidate (score "
            f"{rotation.get('new_score', 0.0):.2f}) that the Portfolio Manager "
            "asked to buy, with "
            f"{rotation.get('headroom_pct', 0.0):.2f}% risk headroom left "
            f"against the {rotation.get('ceiling_pct', 0.0):.2f}% ceiling.\n"
            "This sale went through the normal Risk Manager review and the "
            "protected-sell discipline (stops cancelled write-ahead, restored "
            f"if the sale does not fill). The BUY of {new} follows in this "
            "session only if the sale fills; a second alert follows if it "
            "does not happen."
        )
        from src import notifier as _notifier

        _notifier.send_owner_alert(body, symbols=[str(held), str(new)])
    except Exception as exc:  # noqa: BLE001
        logger.error("rotation owner alert failed: %s", exc)


def _drop_rotation_buy_if_room_not_freed(pipeline, ctx, buy_decisions: list,
                                         sell_status_by_id: dict) -> list:
    """Phase 14b — the rotation's BUY leg may only proceed on room that is
    REAL. Returns the BUY list with the new candidate removed when it is not.

    The constructor granted the new candidate its risk on the premise that
    the held name closes. If that close was refused upstream (Risk Manager,
    hard rules, protected-sell skip) or was accepted but did not fill,
    buying anyway would put the book over the portfolio risk ceiling by the
    new name's risk — a side door around the ceiling this feature must never
    open. Uses the same `_record_execution_skip` path every other
    deterministic BUY skip uses, so the funnel and the evening review see it.
    Every other BUY in the plan is untouched.
    """
    rotation = ctx.rotation
    if not isinstance(rotation, dict) or not buy_decisions:
        return buy_decisions
    sell_id = rotation.get("sell_order_id")
    sell_status = sell_status_by_id.get(sell_id) if sell_id else None
    if sell_id is None:
        block_detail = (
            f"the rotation close of {rotation.get('held_symbol')} was not "
            "submitted this session (removed before execution — see the "
            "risk / deterministic_gate events for it), so no room was freed"
        )
    elif sell_status != "filled":
        block_detail = (
            f"the rotation close of {rotation.get('held_symbol')} (order "
            f"{sell_id}) ended {sell_status or 'unknown'}, not filled, so no "
            "room was freed"
        )
    else:
        return buy_decisions
    new_symbol = rotation.get("new_symbol")
    kept: list = []
    for d in buy_decisions:
        if d.symbol.upper() == new_symbol:
            _record_execution_skip(
                pipeline, ctx, d.symbol, "rotation_room_not_freed", block_detail,
            )
            continue
        kept.append(d)
    return kept


def _record_rotation_buy_leg_outcome(pipeline, ctx, orders: list) -> None:
    """Phase 14b — record both legs' outcome durably once the buy phase has
    run. A sale that freed room for a BUY that then did not happen is the
    exact churn the anti-rotation rules exist to prevent, so that case is
    also paged (`_alert_rotation_buy_leg_missing`). No-op unless a rotation
    SELL was actually broker-accepted this run."""
    rotation = ctx.rotation
    if not isinstance(rotation, dict) or not rotation.get("sell_order_id"):
        return
    new_symbol = rotation.get("new_symbol")
    buy_submitted = any(
        str(o.get("symbol") or "").upper() == new_symbol
        and str(o.get("action") or "").upper() in ("BUY", "SHORT")
        for o in orders if isinstance(o, dict)
    )
    if buy_submitted:
        _record_pipeline_event(
            pipeline, ctx, new_symbol, "rotation", "buy_submitted",
            "replacement_entry_submitted", held_symbol=rotation.get("held_symbol"),
        )
        return
    skip = next(
        (
            s for s in reversed(ctx.execution_skips or [])
            if str(s.get("symbol") or "").upper() == new_symbol
        ),
        None,
    )
    detail = (
        f"{skip.get('reason')}: {skip.get('detail')}"
        if skip else
        "no BUY order for it reached the broker this session (dropped "
        "before execution — see its risk / deterministic_gate / "
        "execution_skip events)"
    )
    _record_pipeline_event(
        pipeline, ctx, new_symbol, "rotation", "buy_not_submitted", detail,
        held_symbol=rotation.get("held_symbol"),
    )
    _alert_rotation_buy_leg_missing(rotation=rotation, detail=detail)


def _alert_rotation_buy_leg_missing(*, rotation: dict, detail: str) -> None:
    """Standalone owner alert: the rotation SOLD but did not BUY.

    This is the one outcome the anti-churn rules exist to prevent — capital
    freed for a named trade that then did not happen — so it is paged, not
    just logged. Never raises.
    """
    try:
        held = rotation["held_symbol"]
        new = rotation["new_symbol"]
        body = (
            "ROTATION INCOMPLETE — SOLD BUT THE REPLACEMENT WAS NOT BOUGHT\n"
            f"{held} was closed this session to make room for {new}, but no "
            f"BUY of {new} was submitted.\n"
            f"Reason recorded: {detail}\n"
            "OUTCOME: the freed cash is sitting in the book. Nothing further "
            "was done automatically. The next morning session will see "
            f"{new} again as a fresh candidate with room available."
        )
        from src import notifier as _notifier

        _notifier.send_owner_alert(body, symbols=[str(held), str(new)])
    except Exception as exc:  # noqa: BLE001
        logger.error("rotation buy-leg owner alert failed: %s", exc)


def _entry_slippage_bps(pipeline) -> float:
    """Configured entry-limit bound in basis points, or the 40bp default.

    One helper, two sides: BUY uses this as a ceiling above the reference,
    SHORT as a floor below it. MagicMock configs (common in tests) must not
    read as a real bps value — same isinstance convention as `_repeg_settings`.
    """
    raw = getattr(
        getattr(pipeline.config, "execution", None),
        "max_entry_slippage_bps", None,
    )
    if (
        isinstance(raw, (int, float))
        and not isinstance(raw, bool)
        and raw > 0
    ):
        return float(raw)
    return MAX_ENTRY_SLIPPAGE_BPS


def _trade_updates_already_started(pipeline) -> bool:
    started = getattr(getattr(pipeline, "broker", None), "trade_updates_started", None)
    if not callable(started):
        return False
    try:
        return started() is True
    except Exception:  # noqa: BLE001
        return False


def _fill_stream_enabled(pipeline) -> bool:
    """Whether the desk may open the `trade_updates` socket at all.

    Defaults TRUE when the broker predates the switch (a test double with no
    such method), so this helper cannot silently remove a budget from a
    broker that really does handshake.
    """
    enabled = getattr(getattr(pipeline, "broker", None), "fill_stream_enabled", None)
    if not callable(enabled):
        return True
    try:
        return enabled() is not False
    except Exception:  # noqa: BLE001
        return True


def _known_entry_submit_budget_s(pipeline, *, will_fund: bool) -> float:
    """Programmed waits still ahead of submit. Not a fitted clock.

    Auth is omitted when the kept socket already started during Risk — that
    budget began at hub open. Funding timeouts are the cash-sweep step's
    own ceiling; they must not be added as leftover slack on the submit
    path after fund_buys has already returned. Call this AFTER funding
    with will_fund=False.

    Auth is also omitted when the socket is switched off entirely
    (`execution.fill_stream_enabled`; on since 2026-09-18, so this branch
    is the configured-off case): there is no handshake ahead of submit, so
    counting one would leave a stale 30s of
    slack in a window that is supposed to be the sum of the waits actually
    programmed. This widens nothing and tightens no existing timeout — it
    stops claiming a wait that cannot happen.
    """
    budget = 0.0
    if _fill_stream_enabled(pipeline) and not _trade_updates_already_started(pipeline):
        contended = getattr(
            getattr(pipeline, "broker", None),
            "trade_updates_lease_contended",
            None,
        )
        lease_held_elsewhere = False
        if callable(contended):
            try:
                lease_held_elsewhere = contended() is True
            except Exception:  # noqa: BLE001
                lease_held_elsewhere = False
        if not lease_held_elsewhere:
            from src.execution.broker import _ALPACA_STREAM_AUTH_DEADLINE_S
            budget += float(_ALPACA_STREAM_AUTH_DEADLINE_S)
    if will_fund:
        from src.execution.cash_sweep import (
            _FUND_CASH_SETTLE_TIMEOUT_S,
            _FUND_TERMINAL_TIMEOUT_S,
        )
        budget += float(_FUND_TERMINAL_TIMEOUT_S) + float(_FUND_CASH_SETTLE_TIMEOUT_S)
    return budget


def _encode_entry_submit_window(pipeline, ctx, *, will_fund: bool) -> None:
    """Pin the submit deadline from known step durations, not an invented timer."""
    budget = _known_entry_submit_budget_s(pipeline, will_fund=will_fund)
    ctx.entry_submit_budget_s = budget
    started = time.monotonic()
    ctx.entry_submit_started_mono = started
    ctx.entry_submit_deadline_mono = started + budget if budget > 0 else None


def _submit_window_overrun(ctx) -> bool:
    """True when submitting now would fire a ticket after the encoded window."""
    deadline = getattr(ctx, "entry_submit_deadline_mono", None)
    if not isinstance(deadline, (int, float)):
        return False
    return time.monotonic() > float(deadline)


def _start_trade_updates_early(pipeline, ctx) -> None:
    """Start trade_updates without waiting — overlap handshake with Risk review."""
    start = getattr(getattr(pipeline, "broker", None), "start_trade_updates", None)
    if not callable(start):
        return
    try:
        warmup = start()
    except Exception as exc:  # noqa: BLE001
        logger.warning("trade_updates start-during-Risk failed: %s", exc)
        ctx.desk_latency_stall = True
        return
    try:
        from src.execution.broker import TradeStreamWarmup
    except Exception:  # noqa: BLE001
        return
    if isinstance(warmup, TradeStreamWarmup) and (
        warmup.handshake_failed or warmup.retried
    ):
        ctx.desk_latency_stall = True


def _stop_trade_updates(pipeline) -> None:
    stop = getattr(getattr(pipeline, "broker", None), "stop_trade_updates", None)
    if not callable(stop):
        return
    try:
        stop()
    except Exception as exc:  # noqa: BLE001
        logger.warning("trade_updates stop failed: %s", exc)


def _pin_approved_entry_ceilings(pipeline, ctx, buy_decisions) -> None:
    """Freeze the already-approved slippage cap before a desk stall can move it.

    BUY ceiling / SHORT floor from a live quote (or the approved entry) at
    ExecutionStage start — post-Risk, pre-websocket. Recomputing the cap
    from a later last-trade would raise the ceiling, which is a chase.
    Repeg stays off. Never uses a daily bar close.
    """
    slippage_bps = _entry_slippage_bps(pipeline)
    pinned = dict(getattr(ctx, "approved_entry_ceiling", None) or {})
    for decision in buy_decisions:
        symbol = getattr(decision, "symbol", None)
        if not symbol:
            continue
        live = _today_order_price(pipeline, symbol)
        ref = live if isinstance(live, (int, float)) and live > 0 else None
        if ref is None:
            entry = getattr(decision, "entry_price", None)
            if isinstance(entry, (int, float)) and entry > 0:
                ref = float(entry)
        if ref is None:
            continue
        is_short = getattr(decision, "action", "") == "SHORT"
        if is_short:
            pinned[symbol] = ref * (1 - slippage_bps / 10_000.0)
        else:
            pinned[symbol] = ref * (1 + slippage_bps / 10_000.0)
    ctx.approved_entry_ceiling = pinned


def _adopt_stream_stall(pipeline, ctx) -> None:
    """If the fill wait REST-fell-back because auth never completed, name it."""
    warmup = getattr(getattr(pipeline, "broker", None), "_last_stream_warmup", None)
    try:
        from src.execution.broker import TradeStreamWarmup
    except Exception:  # noqa: BLE001
        return
    if isinstance(warmup, TradeStreamWarmup) and (
        warmup.handshake_failed or warmup.retried
    ):
        ctx.desk_latency_stall = True


def _warm_trade_updates(pipeline, ctx) -> None:
    """Start the kept trade_updates socket if Risk did not. Does not wait for auth."""
    ensure = getattr(getattr(pipeline, "broker", None), "ensure_trade_updates", None)
    if not callable(ensure):
        return
    try:
        warmup = ensure()
    except Exception as exc:  # noqa: BLE001
        logger.warning("trade_updates warmup failed: %s", exc)
        ctx.desk_latency_stall = True
        return
    try:
        from src.execution.broker import TradeStreamWarmup
    except Exception:  # noqa: BLE001
        return
    if isinstance(warmup, TradeStreamWarmup) and (
        warmup.handshake_failed or warmup.retried
    ):
        ctx.desk_latency_stall = True


def _today_order_price(pipeline, symbol) -> float | None:
    """A price from TODAY that an order may be placed against, or None.

    Never a daily bar close (owner 2026-09-16), and now never a price the
    provider stamped with an earlier date either. A live quote mid-session is
    a legitimate fill reference, so quotes are allowed — what is refused is
    yesterday's last print on a thin name, or any value whose timestamp
    cannot be read. Unknown freshness returns None, which the callers already
    treat as "no verifiable live price" and skip, rather than pricing an
    order off it.
    """
    broker = getattr(pipeline, "broker", None)
    stamped_getter = getattr(broker, "get_latest_price_stamped", None)
    stamped = None
    if callable(stamped_getter):
        try:
            from src.execution.broker import LivePrice

            candidate = stamped_getter(symbol)
            # isinstance, not truthiness — ~58 tests build the pipeline with
            # a MagicMock broker whose auto-attributes answer every call. A
            # MagicMock must never read as "this price is from today", and
            # must not read as "no price" either, so it falls through to the
            # bare getter below unchanged.
            if isinstance(candidate, LivePrice):
                stamped = candidate
        except Exception:  # noqa: BLE001
            return None
    if stamped is not None:
        if not (stamped.price > 0):
            return None
        if not stamped.is_today:
            logger.warning(
                "%s live price $%.2f is not stamped today (source %s) — not "
                "pricing an order against it",
                symbol, stamped.price, stamped.source,
            )
            return None
        return float(stamped.price)
    getter = getattr(broker, "get_latest_price", None)
    if not callable(getter):
        return None
    try:
        live = getter(symbol)
    except Exception:  # noqa: BLE001
        return None
    if isinstance(live, (int, float)) and live > 0:
        return float(live)
    return None


def _live_fill_price(pipeline, symbol) -> float | None:
    """Back-compat alias for `_today_order_price`."""
    return _today_order_price(pipeline, symbol)


def _today_sizing_price(pipeline, symbol) -> float | None:
    """A price the SHARE COUNT may divide the dollar allocation by, or None.

    docs/WORK.md item 120. The number of shares an entry buys is
    `dollars / price`, so this price is the DIVISOR of the allocation and a
    wrong one mis-sizes the position PROPORTIONALLY. It must therefore be a
    real TODAY PRINT (`LivePrice.is_today_print`), never a quote MID and
    never a prior-session last trade.

    This is deliberately stricter than `_today_order_price`, the FILL
    reference, where a live quote mid IS a legitimate marketable-limit
    reference mid-session (owner 2026-09-12) — bounding an order you are
    about to cross against the current book is a different act from setting
    how many shares to buy. `_today_order_price` accepts a quote mid; this
    refuses it. When this returns None the caller must refuse the name as
    unmeasurable rather than size it on a bad price.

    It accepts the SAME today prices the constructor sizes off, so the two
    stages agree: a real last-trade print, and — when there is none — today's
    forming SESSION or minute bar via `resolve_live_price` (a real intraday
    price on the entitled venue, never a quote mid). Without this second
    branch an IEX-thin name with a today bar but no print — item 120's exact
    population — would be sized and approved by the paid PM/Risk seats and
    then silently skipped here, wasting those seats.

    Test compatibility: when the broker is a MagicMock whose
    `get_latest_price_stamped` does not return a real `LivePrice` and whose
    `get_intraday_snapshots` does not return a usable payload, this falls
    through to the bare `get_latest_price` exactly as `_today_order_price`
    does, so the many MagicMock-broker execution tests keep their behaviour.
    The freshness gate only bites on a real stamped price / real snapshot.
    """
    broker = getattr(pipeline, "broker", None)

    # 1. A real today PRINT from the stamped getter is the best sizing ref.
    stamped_getter = getattr(broker, "get_latest_price_stamped", None)
    stamped_is_real = False
    if callable(stamped_getter):
        try:
            from src.execution.broker import LivePrice

            candidate = stamped_getter(symbol)
            if isinstance(candidate, LivePrice):
                stamped_is_real = True
                if candidate.price and candidate.price > 0 and candidate.is_today_print:
                    return float(candidate.price)
        except Exception:  # noqa: BLE001
            return None

    # 2. No today print: accept today's forming SESSION/minute bar through the
    #    same resolver the constructor uses (never a quote mid), so both
    #    stages agree on a thin name that has a bar but no print.
    snap_getter = getattr(broker, "get_intraday_snapshots", None)
    if callable(snap_getter):
        snap_is_real = False
        try:
            snaps = snap_getter([symbol])
            if isinstance(snaps, dict):
                snap_is_real = True
                resolved = resolve_live_price(snaps.get(symbol))
                if resolved.is_today_print:
                    return float(resolved.price)
        except Exception:  # noqa: BLE001
            snap_is_real = False
        # A REAL stamped price (real broker) that was a quote mid or stale,
        # and no usable today bar either: refuse rather than fall through to
        # the mid-capable bare getter.
        if stamped_is_real or snap_is_real:
            if stamped_is_real:
                logger.warning(
                    "%s sizing price refused: no today print and no today "
                    "session/minute bar — a quote mid or stale price cannot "
                    "set the share count", symbol,
                )
            return None
    elif stamped_is_real:
        return None

    # 3. Test / back-compat: a MagicMock broker that returned neither a real
    #    LivePrice nor a real snapshot dict. Keep existing behaviour.
    getter = getattr(broker, "get_latest_price", None)
    if not callable(getter):
        return None
    try:
        live = getter(symbol)
    except Exception:  # noqa: BLE001
        return None
    if isinstance(live, (int, float)) and not isinstance(live, bool) and live > 0:
        return float(live)
    return None


def _repeg_settings(pipeline) -> tuple[float, float] | None:
    """(poll_seconds, slippage_bps), or None when re-peg is off.

    Returns None — feature disabled — for anything other than an explicit
    `repeg_enabled is True`. The isinstance guards are the same convention as
    `_entry_slippage_bps`: ~58 tests build the pipeline with a MagicMock
    config whose auto-attributes are truthy, and a MagicMock must never read
    as "yes, replace live orders".
    """
    execution_cfg = getattr(pipeline.config, "execution", None)
    if getattr(execution_cfg, "repeg_enabled", None) is not True:
        return None

    raw_poll = getattr(execution_cfg, "repeg_poll_seconds", None)
    poll = (
        float(raw_poll)
        if isinstance(raw_poll, (int, float)) and not isinstance(raw_poll, bool)
        and 0 < raw_poll <= 30
        else 5.0
    )
    return poll, _entry_slippage_bps(pipeline)


#: Plain-language endings for the single-shot reprice, keyed by the
#: `repeg_outcome` written into the entry spec. Read by the end-of-session
#: cancel alert so the owner is told what WAS and WAS NOT tried, in words —
#: never colour or an emoji standing alone; severity must survive being
#: read as plain text.
_REPEG_OUTCOME_TEXT = {
    "disabled": "automatic repricing is switched off (execution.repeg_enabled)",
    "no_room": "it was already sitting at the slippage ceiling, so there was "
               "no legal price left to reprice to",
    "not_at_exchange": "the exchange had not acknowledged it within the "
                       "window, so a reprice was NOT attempted (Alpaca "
                       "rejects a replace on an order that has not reached "
                       "the exchange)",
    "market_within_limit": "the market was at or below the limit, so it "
                           "should have filled without a reprice",
    "replaced": "it was repriced ONCE to a market-crossing price",
    "replace_rejected": "one reprice was attempted and the broker refused it",
    "replace_unknown": "one reprice was attempted and its outcome could not "
                       "be read back from the broker",
    "wal_refused": "a reprice was NOT attempted because its recovery record "
                   "could not be written first",
    "wait_failed": "a reprice was NOT attempted because the order's status "
                   "could not be read",
    "quote_unavailable": "a reprice was NOT attempted because no quote was "
                         "available",
    "unpriced": "a reprice was NOT attempted (no reference or limit price)",
}


def _repeg_entry_order(pipeline, ctx, spec: dict) -> tuple[str, float]:
    """ONE decisive reprice of a working entry limit — not a ladder.

    Returns ``(order_id_to_protect, shares_filled_under_superseded_ids)`` and
    writes ``spec["attempted_prices"]`` / ``spec["repeg_outcome"]`` so the
    end-of-session cancel alert can say exactly what was tried.

    WHY ONE REPLACE, NOT A LADDER (rebuilt 2026-09-12, owner-approved).
    PR #311 walked the limit up in small steps, confirming each swap before
    the next. Two things from Alpaca's own community and docs retired that:
      * the practice real users converge on for a fast market is submit,
        wait a few seconds, then replace ONCE at a deliberately aggressive
        price that crosses the market — not a sequence of nudges that each
        arrive after the market has moved again;
      * every replace is another `pending_replace` window to get stuck in
        (a real user reported a position left unmanageable that way), so
        the number of replaces is exposure, and one is the minimum.

    WHAT "DECISIVE" MEANS HERE, WITH NO NEW NUMBER. The single reprice goes
    straight to the slippage CEILING — ``reference * (1 + max_entry_slippage
    _bps)`` — the price this entry was already approved to pay when it was
    gated at submission. A buy limit at the ceiling crosses any ask at or
    below it and executes at the ask, not at the limit, so it is the most
    aggressive legal price and costs nothing extra when the market is
    inside it. It is the ONE bound that was already there; no "cross by X
    cents" constant is invented on top of it. The ceiling is computed from
    the reference captured at submission, NOT a fresh quote, because a
    ceiling that follows the market is not a ceiling — and it is absolute:
    nothing here can price above it to force a fill.

    THE OPEN-MARKET DEFECT THIS FIXES. A replace against an order Alpaca has
    accepted but the exchange has not yet acknowledged is REJECTED
    ("unable to replace order, order isn't sent to exchange yet"). At the
    open — slowest acknowledgement, highest volatility, and when this desk
    trades most — the old first nudge at ~5s was the attempt most likely to
    be thrown away. So the reprice is now GATED on the order having left
    Alpaca's not-yet-at-exchange statuses (`accepted`, `pending_new`; see
    `AlpacaBroker._ORDER_PRE_EXCHANGE_STATES` for the sourced list), read
    from the same `trade_updates` websocket as the fill wait. If it never
    gets there inside the window, no replace is sent — that attempt would
    be rejected anyway — and the reason is recorded honestly.

    WHAT HAPPENS TO AN UNFILLED ORDER AFTERWARDS. It is NOT left working.
    `place_entry_protection` cancels any entry still working at the end of
    this session and pages the owner (see that method for the derivation:
    the cancel is bound to the desk's own session boundary, not a timeout).

    TIME. This adds at most ``2 * repeg_poll_seconds`` plus one replace
    round-trip before protection: one window to let the order work, and —
    only if the venue has not acknowledged it yet — one more to wait for
    that acknowledgement.

    THE FOOTGUN (unchanged). Alpaca does not edit an order in place. It
    cancels the old one and creates a NEW one with a NEW id:
      1. `trades.broker_order_id` must be repointed, write-ahead-logged
         (`pending_repegs`) so a crash mid-replace is recoverable.
      2. A partially filled order must NEVER be replaced — fill counters do
         not carry across a replacement. The fill is re-read immediately
         before the replace and any fill at all stops it.
      3. A replacement rejected because the order filled first is the good
         case; the original id stays authoritative.

    Never raises: a re-peg failing must leave the ordinary "protect whatever
    filled" path exactly as it was.
    """
    order_id = str(spec.get("order_id") or "")
    symbol = spec.get("symbol")
    spec.setdefault("attempted_prices", [])
    if not order_id:
        spec["repeg_outcome"] = "unpriced"
        return order_id, 0.0

    settings = _repeg_settings(pipeline)
    if settings is None:
        spec["repeg_outcome"] = "disabled"
        return order_id, 0.0
    poll_seconds, slippage_bps = settings

    reference = spec.get("reference_price")
    limit_price = spec.get("limit_price")
    requested_qty = spec.get("qty")
    trade_row_id = spec.get("trade_row_id")

    if not isinstance(reference, (int, float)) or reference <= 0:
        spec["repeg_outcome"] = "unpriced"
        return order_id, 0.0
    if not isinstance(limit_price, (int, float)) or limit_price <= 0:
        # A market order has no limit to walk.
        spec["repeg_outcome"] = "unpriced"
        return order_id, 0.0

    ceiling = reference * (1 + slippage_bps / 10_000.0)
    ceiling = round(ceiling, 2 if ceiling >= 1 else 4)
    spec["ceiling"] = ceiling
    if limit_price >= ceiling - 1e-9:
        # Expected for most entries: since PR #111 the submitted limit IS the
        # ceiling, so there is nothing to reprice toward. Room exists only
        # when the limit was set below the ceiling — e.g. the quote was
        # unavailable at submission and the analyst's entry price was used.
        logger.debug(
            "re-peg %s: limit $%.4f is already at the %.0fbp ceiling $%.4f — "
            "nothing to chase", symbol, limit_price, slippage_bps, ceiling,
        )
        spec["repeg_outcome"] = "no_room"
        return order_id, 0.0

    # 1. Let it work first. A marketable limit usually fills here and the
    #    cheapest reprice is the one never sent.
    try:
        status = pipeline.broker.wait_for_order_terminal(
            order_id, timeout_seconds=poll_seconds,
            poll_interval=min(1.0, poll_seconds),
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("re-peg %s: wait failed (%s) — leaving the order "
                       "as-is", symbol, exc)
        spec["repeg_outcome"] = "wait_failed"
        return order_id, 0.0
    status = str(status or "").lower()
    if status in pipeline.broker._TERMINAL_ORDER_STATES:
        spec["repeg_outcome"] = "terminal_before_reprice"
        return order_id, 0.0

    # 2. Has the EXCHANGE got it yet? A replace before that is rejected.
    #    Only pay this second window when the first one ended with the order
    #    still in a pre-exchange status.
    if status not in pipeline.broker._ORDER_REPLACEABLE_STATES and \
            status != "partially_filled":
        try:
            status = pipeline.broker.wait_for_order_at_exchange(
                order_id, timeout_seconds=poll_seconds,
                poll_interval=min(1.0, poll_seconds),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("re-peg %s: exchange-ack wait failed (%s) — "
                           "leaving the order as-is", symbol, exc)
            spec["repeg_outcome"] = "wait_failed"
            return order_id, 0.0
        status = str(status or "").lower()
        if status in pipeline.broker._TERMINAL_ORDER_STATES:
            spec["repeg_outcome"] = "terminal_before_reprice"
            return order_id, 0.0
        if status not in pipeline.broker._ORDER_REPLACEABLE_STATES and \
                status != "partially_filled":
            logger.info(
                "re-peg %s: order %s still %r after %.1fs — the exchange has "
                "not acknowledged it, so a replace would be rejected; NOT "
                "repricing. It is handed to end-of-session handling as-is.",
                symbol, order_id, status or "unknown", poll_seconds,
            )
            _record_pipeline_event(
                pipeline, ctx, symbol, "repeg", "not_at_exchange",
                "repeg_not_at_exchange", broker_order_id=order_id,
                status=status or "unknown", window_seconds=poll_seconds,
            )
            spec["repeg_outcome"] = "not_at_exchange"
            return order_id, 0.0

    # 3. The partial-fill guard, re-read immediately before the replace.
    try:
        info = pipeline.broker.get_order_fill_info(order_id) or {}
    except Exception as exc:  # noqa: BLE001
        logger.warning("re-peg %s: fill read failed (%s) — leaving the "
                       "order as-is", symbol, exc)
        spec["repeg_outcome"] = "wait_failed"
        return order_id, 0.0
    if str(info.get("status") or "").lower() in pipeline.broker._TERMINAL_ORDER_STATES:
        spec["repeg_outcome"] = "terminal_before_reprice"
        return order_id, 0.0
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
            fill_qty=filled_so_far,
        )
        spec["repeg_outcome"] = "partial_fill"
        return order_id, 0.0

    # 4. Where is the market? Only to decide whether a reprice is needed at
    #    all — the PRICE is the ceiling regardless, never the ask plus
    #    something.
    try:
        quote = pipeline.broker.get_latest_quote(symbol)
    except Exception as exc:  # noqa: BLE001
        logger.warning("re-peg %s: quote failed (%s)", symbol, exc)
        spec["repeg_outcome"] = "quote_unavailable"
        return order_id, 0.0
    ask = quote.get("ask_price") if isinstance(quote, dict) else None
    if not isinstance(ask, (int, float)) or ask <= 0:
        spec["repeg_outcome"] = "quote_unavailable"
        return order_id, 0.0
    if float(ask) <= limit_price + 1e-9:
        # The market is at or inside our limit: the order is marketable as
        # it stands and a replace would only re-queue it. Leave it working.
        logger.info(
            "re-peg %s: ask $%.4f is at/below limit $%.4f — order is "
            "marketable as-is, no reprice", symbol, ask, limit_price,
        )
        _record_pipeline_event(
            pipeline, ctx, symbol, "repeg", "market_within_limit",
            "repeg_no_reprice_needed", broker_order_id=order_id,
            ask=float(ask), limit_price=limit_price, ceiling=ceiling,
        )
        spec["repeg_outcome"] = "market_within_limit"
        return order_id, 0.0

    # 5. THE ONE REPRICE. Straight to the ceiling — the maximum price this
    #    entry was already approved for. Crosses the market when the ask is
    #    inside the ceiling; when the ask has run past the ceiling this is
    #    still the best legal price and is sent once, not chased.
    target = ceiling
    target = round(target, 2 if target >= 1 else 4)
    assert target <= ceiling + 1e-9
    crosses = float(ask) <= target + 1e-9
    if not crosses:
        logger.info(
            "re-peg %s: ask $%.4f is ABOVE the ceiling $%.4f — the single "
            "reprice cannot cross the market; sending it at the ceiling "
            "anyway as the best legal price", symbol, ask, ceiling,
        )
    spec["attempted_prices"].append(target)
    new_id, carried_fill, outcome = _apply_repeg(
        pipeline, ctx, symbol=symbol, order_id=order_id,
        trade_row_id=trade_row_id, target=target,
        requested_qty=requested_qty, ceiling=ceiling,
        ask=float(ask), crosses_market=crosses,
    )
    spec["repeg_outcome"] = outcome
    return new_id, carried_fill


# AUTO-RESUBMISSION IS DELIBERATELY NOT IMPLEMENTED — a live owner decision,
# not an oversight. The owner raised it as a possibility ("maybe there's
# other options like just wait again and resubmit... it could all be
# automatic"), which is a possibility to consider, not a ratified
# instruction, so the safe half is what ships: one bounded automatic
# reprice with no human in the loop, an end-of-session cancel, then an
# alert.
#
# Why the line is drawn exactly there. Repricing an EXISTING order is bounded
# by construction — one order stays one order, it can never cross the
# slippage ceiling the entry was approved against, and Alpaca's own replace
# semantics cap the blast radius. Submitting a BRAND NEW order after the
# original is gone is a different act with different failure modes: the
# original may fill a moment later (now two positions in one idea), the
# analysis the entry was approved on is by then minutes stale, and the
# resubmission is not covered by any budget the session already counted.
# Getting that wrong buys the same idea twice, which is the one failure this
# whole module is written to avoid.
#
# What it would need before shipping: an owner decision on whether a stale
# entry thesis may be re-entered at all without fresh analysis, and an
# idempotency key or equivalent so a resubmission cannot race the original.
# Neither exists today. Left as a follow-up.


def _alert_owner_entry_cancelled(pipeline, spec: dict, info: dict) -> None:
    """An entry was cancelled unfilled at the end of its session. PAGE.

    The owner's requirement in his own words: an order must never simply die
    with no trace, and if the automatic attempts are exhausted he wants to be
    told so he has a choice. Everything up to this point is deliberately
    hands-off — the ordinary stalling entry is repriced once and filled with
    no human involved. This fires only once the session has given up on it.

    This is ALSO the "repricing exhausted" alert: since 2026-09-12 an
    unfilled entry is cancelled rather than left working, so "the reprice
    ran out" and "the order was cancelled" are the same event and get ONE
    message, not two. It fires whether or not repricing is enabled — the
    cancel happens either way, and the text says what was and was not tried.

    A SEPARATE Telegram message via `send_owner_alert`, never a line inside
    the session summary, per this desk's standing alert-design rule (see
    `src/notifier.py`'s data-quality alert comment: "alerts get their OWN
    Telegram message, never bundled into a run summary"). Same path already
    used for a naked position with no stop and for a failed protective stop.

    WHAT DOES NOT PAGE, on purpose — each of these is a non-event, and an
    alert channel that fires on non-events is one the owner learns to swipe
    away:
      * the order FILLED (the whole point) — including a fill that landed
        mid-replace and a replacement the broker refused because it filled;
      * a PARTIAL fill — shares were acquired and the stop covers them (the
        unfilled remainder is cancelled by protection as before, silently);
      * an order that reached a terminal state on its own (expired,
        rejected) — that is a different failure with its own reporting.

    NOT deduplicated, matching the data-quality alert's stated reasoning: if
    the same symbol stalls again tomorrow that is a real repeated event, not
    noise.

    Never raises. An alerting bug must not break the execution path it is
    reporting on.
    """
    try:
        symbol = spec.get("symbol")
        attempted = list(spec.get("attempted_prices") or [])
        limit_price = spec.get("limit_price")
        ceiling = spec.get("ceiling")
        outcome = str(spec.get("repeg_outcome") or "")
        ending = _REPEG_OUTCOME_TEXT.get(
            outcome, "no automatic reprice was made",
        )
        prices = [p for p in [limit_price] if isinstance(p, (int, float))]
        prices += attempted
        tried = (
            " → ".join(f"${p:,.2f}" for p in prices)
            if prices else "(no limit price recorded — market order)"
        )
        ceiling_line = (
            f"Ceiling it may not cross: ${ceiling:,.2f}\n"
            if isinstance(ceiling, (int, float)) else ""
        )
        body = (
            "ENTRY DID NOT FILL — cancelled at the end of its session\n"
            f"{symbol}: the entry limit did not fill; {ending}.\n"
            f"Prices tried: {tried}\n"
            f"{ceiling_line}"
            f"Broker order {info.get('order_id')}: CANCELLED (last status "
            f"{info.get('status', 'unknown')}), filled 0. It was NOT left "
            "working at the broker.\n"
            "\n"
            "WHY CANCELLED: this desk re-analyses from scratch each session "
            "at current prices. An order resting past its own session would "
            "be acting on analysis the desk has already replaced. If the "
            "next session still wants this trade it will propose it again "
            "at real current prices.\n"
            "Nothing new was submitted automatically — the desk will not "
            "resubmit this one by itself. YOUR CHOICE: leave it to the next "
            "session, or place a fresh entry at a price you are willing to "
            "pay."
        )
        from src import notifier as _notifier
        _notifier.send_owner_alert(body, symbols=[str(symbol)])
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "entry-cancel alert for %s could not be sent: %s",
            spec.get("symbol"), exc,
        )


def _alert_unmeasurable_symbols(faults: dict[str, dict]) -> None:
    """Page the owner: these symbols could not be MEASURED this session.

    2026-09-12. A data fault — no price, no ATR, no usable bars, no
    analysis at all — used to be filed as a trade the constructor rejected,
    so a dead feed and a trade that failed its rules were the same class
    of outcome in the record and nobody could count either. The symbol was
    simply, silently, not traded. This is the alert half of the split:
    `PortfolioConstructor.last_data_faults` is recorded under its own
    `data_fault` reason (see `_record_constructor_drops`), and paged here.

    ONE message per session listing every unmeasurable symbol, not one per
    symbol: an outage hits the whole universe at once and sixty pages are
    less readable than one list. Same standalone `send_owner_alert` path
    and same rules as the entry-cancel alert above — its own Telegram
    message, never a line in the run summary; severity in TEXT, never
    colour; NOT deduplicated, because a feed that is still broken tomorrow
    should page again.

    Never raises. An alerting bug must not break the decision path.
    """
    try:
        if not faults:
            return
        symbols = sorted(faults)
        lines = []
        for sym in symbols:
            entry = faults.get(sym) or {}
            lines.append(f"  {sym}: {entry.get('fault', 'unknown')} — {entry.get('detail', '')}")
        body = (
            "DATA FAULT — symbols UNMEASURABLE this session, not judged\n"
            f"{len(symbols)} symbol(s) could not be measured because an input "
            "a real market always has (a price, volatility, usable bars, an "
            "analysis) was not obtained by the desk:\n"
            + "\n".join(lines) + "\n"
            "\n"
            "WHAT HAPPENED: none of these was traded (fail-closed, "
            "unchanged). They are recorded as data faults, NOT as trades "
            "the desk rejected, so the 'why didn't we trade' statistics are "
            "not contaminated. No trade judgement was made on any of them.\n"
            "WHAT TO CHECK: the market data provider and the bar history "
            "for these names before trusting today's no-trade outcome on "
            "them."
        )
        from src import notifier as _notifier
        _notifier.send_owner_alert(body, symbols=symbols)
    except Exception as exc:  # noqa: BLE001
        logger.warning("unmeasurable-symbols alert could not be sent: %s", exc)


def _session_candidate_ranking(pipeline) -> list[str] | None:
    """This session's candidate symbols, BEST FIRST, or None if there is no
    ranking — retired board item 49, owner decision 2026-09-12 (`docs/INCIDENT_HISTORY.md`, 2026-09-14).

    Reads `PortfolioManagerAgent.last_candidate_ranking`, the exact
    `rank_verdicts` output the PM's own prompt was rendered from. Same
    pattern, and same reason, as `last_rotation_precheck` above: the desk
    must ration the budget against the numbers the model was actually shown,
    not against a second evaluation.

    Returns None — never `[]` — when there is no ranking, because an EMPTY
    ranking and an ABSENT one mean the same thing to the allocator (fall back
    to the pre-decision ordering) and conflating them with a real, empty list
    would be indistinguishable from "every candidate ranked last".
    """
    ranked = getattr(
        getattr(pipeline, "portfolio_manager", None), "last_candidate_ranking", None,
    )
    if not ranked:
        return None
    symbols: list[str] = []
    for candidate in ranked:
        symbol = str(getattr(candidate, "symbol", "") or "").strip().upper()
        if symbol:
            symbols.append(symbol)
    return symbols or None


def _dropped_since_proposal(portfolio_decision) -> list[str]:
    """Symbols the PM proposed that are no longer in the order list.

    Targets minus decisions, and deliberately nothing cleverer: whatever
    removed the symbol, the fact the Risk Manager needs is the same one —
    "the narrative below argues for a name that is not in the list above,
    and that is expected."

    Earlier refusals that removed a target itself (never-blank soft-exit
    before the constructor) are kept: union with the existing drop list
    so a name refused before tickets were built is not silently deleted
    from what Risk is told.

    WHY THIS IS A FUNCTION AND NOT A LINE
    --------------------------------------
    It used to be computed once, immediately after `construct_orders`, and
    then left alone. Between that point and the Risk Manager's review the
    decision list is filtered at least three more times — the symbol guard,
    the queued-earnings clamp, and the hard-risk gate — and each of those
    can remove SOME names while letting the rest through. A symbol removed
    by one of them was gone from the order list and absent from the frozen
    drop list, so the Risk Manager saw a plan arguing for a name it could
    not find and nothing telling it why. That is exactly the failure the
    drop list was written to prevent (docs/INCIDENT_HISTORY.md,
    2026-08-31), reappearing on a path the original fix did not cover.

    So it is recomputed right before the review instead. HOLD still counts
    as kept: the symbol survived, it just is not being traded today.
    """
    kept = {d.symbol.upper() for d in portfolio_decision.decisions}
    dropped = [
        t.symbol.upper() for t in portfolio_decision.targets
        if t.symbol.upper() not in kept
    ]
    seen = set(dropped)
    for symbol in (getattr(portfolio_decision, "constructor_dropped", None) or []):
        upper = str(symbol).upper()
        if upper and upper not in kept and upper not in seen:
            dropped.append(upper)
            seen.add(upper)
    return dropped


def _record_constructor_drops(pipeline, ctx, portfolio_decision) -> dict[str, dict]:
    """Persist WHY each PM target the constructor dropped was dropped — and
    file the two classes of "no order" under different names.

    Funnel-queue item 2 (2026-09-03): a target the constructor drops before
    ever building a `proposed_order` row previously left NOTHING in the
    database — no verdict (RM never saw it), no execution_skip (execution
    never saw it either), just an aggregate log line.
    `blocked_proposals_census.py` counted every one as `no_order_built`,
    its largest unexplained bucket. `last_drop_reasons` (see
    `PortfolioConstructor.construct_orders`) recovers the constructor's
    OWN log line from the same call that just ran, so every dropped symbol
    gets a terminal, real-reason evidence row instead of silence.

    2026-09-12: the constructor now also reports DATA FAULTS separately
    (`PortfolioConstructor.drain_data_faults`). A symbol it could not
    MEASURE — no price, no ATR, no usable bars, no analysis — is filed as
    (stage='deterministic_gate', outcome='unmeasurable',
    reason='data_fault'), never as `constructor_dropped`, because it is not
    a trade the desk judged and must not be counted as one. Faults on
    symbols the PM never targeted (found by the eligibility preview, which
    runs over every analysed name) are recorded the same way with
    `targeted=False`, so a symbol that silently became unanalysable before
    the PM ever saw it still leaves a durable row. Returns the faults so
    the caller can page the owner.

    Best-effort like every evidence write here: never raises.
    """
    faults: dict[str, dict] = {}
    try:
        constructor = pipeline.portfolio_constructor
        drain = getattr(constructor, "drain_data_faults", None)
        faults = dict(drain() if callable(drain) else {})
        dropped = [str(s).upper() for s in (portfolio_decision.constructor_dropped or [])]
        drop_reasons = getattr(constructor, "last_drop_reasons", {})
        # 2026-09-12 (docs/WORK.md item 54): a refusal the constructor
        # recorded AS DATA (`PortfolioConstructor.last_refusals` — today a
        # stop wider than the instrument's reach, or too little history) is
        # filed under its own reason with the code beside it, never through
        # the log-text regex, whose pattern several messages miss. A data
        # fault is checked FIRST: it is not a judgement at all.
        drain_refusals = getattr(constructor, "drain_refusals", None)
        refusals = dict(drain_refusals() if callable(drain_refusals) else {})
        existing_risk_pct, _ = _book_risk_inputs(
            ctx, getattr(ctx, "total_value", 0.0) or 0.0,
        )
        for sym in dropped:
            fault = faults.get(sym)
            if fault:
                _record_pipeline_event(
                    pipeline, ctx, sym, "deterministic_gate", "unmeasurable",
                    "data_fault", fault=fault.get("fault", ""),
                    detail=fault.get("detail", ""), targeted=True,
                )
                continue
            refusal = refusals.get(sym)
            if refusal:
                _record_pipeline_event(
                    pipeline, ctx, sym, "deterministic_gate", "blocked",
                    CONSTRUCTOR_REFUSED_EVENT_REASON,
                    refusal=refusal.get("refusal", ""),
                    detail=refusal.get("detail", ""), targeted=True,
                )
                continue
            target = next(
                (
                    t for t in list(getattr(portfolio_decision, "targets", None) or [])
                    if str(t.symbol).upper() == sym
                ),
                None,
            )
            if target is not None and _target_increase_missing_falsifier(
                target,
                positions=getattr(ctx, "positions", None),
                total_value=getattr(ctx, "total_value", 0.0) or 0.0,
                existing_risk_pct=existing_risk_pct,
            ):
                # Already recorded as SOFT_EXIT_MISSING_AFTER_RETRY
                # before the constructor ran. Do not re-file as a
                # generic drop. A checkable size-down with a blank
                # falsifier is NOT this skip — it was admitted so the
                # constructor can stamp a mechanical warrant.
                continue
            # Falls back to a generic label only if a future refactor adds
            # a new drop path the capture's log-message pattern doesn't
            # match — never nothing, even then.
            _record_pipeline_event(
                pipeline, ctx, sym, "deterministic_gate", "blocked",
                "constructor_dropped",
                detail=drop_reasons.get(sym, "no matching constructor log line captured"),
            )
        # Faults and refusals on names the PM never targeted come from the
        # eligibility preview over every analysed symbol; recorded so "why
        # was X never even proposed" has a durable, named answer.
        for sym, fault in faults.items():
            if sym in dropped:
                continue
            _record_pipeline_event(
                pipeline, ctx, sym, "deterministic_gate", "unmeasurable",
                "data_fault", fault=fault.get("fault", ""),
                detail=fault.get("detail", ""), targeted=False,
            )
        for sym, refusal in refusals.items():
            if sym in dropped or sym in faults:
                continue
            _record_pipeline_event(
                pipeline, ctx, sym, "deterministic_gate", "blocked",
                CONSTRUCTOR_REFUSED_EVENT_REASON,
                refusal=refusal.get("refusal", ""),
                detail=refusal.get("detail", ""), targeted=False,
            )
    except Exception as exc:  # noqa: BLE001
        logger.error("constructor drop/fault recording failed: %s", exc)
    return faults


def _record_constructor_side_flips(pipeline, ctx) -> None:
    """One durable per-symbol row for every target the constructor collapsed
    from a side flip to a close-only leg (`PortfolioConstructor`, rule D3).

    Board item 164 (2026-09-19). The seat asked to turn a long into a short
    (or back); the constructor refuses the flip and emits only the closing
    leg. The symbol still produces an order, so it never counts as a drop,
    and the only trace of the refused half was a log line. The record states
    the held weight, the weight asked for and the weight emitted (0: flat).
    Never raises.
    """
    try:
        flips = getattr(pipeline.portfolio_constructor, "last_side_flips", None)
        if not isinstance(flips, dict):
            return
        for sym, flip in flips.items():
            held = flip.get("held_weight_pct")
            asked = flip.get("requested_weight_pct")
            _record_pipeline_event(
                pipeline, ctx, sym, "deterministic_gate", "modified",
                "side_flip_refused", gate="side_flip_refused",
                held_weight_pct=held, requested_weight_pct=asked,
                emitted_weight_pct=flip.get("emitted_weight_pct"),
                detail=(
                    f"{sym}: target asked to flip the position from "
                    f"{held:+.2f}% to {asked:+.2f}% of the book in one "
                    f"session; a single order that crosses sides is "
                    f"unprotected between legs, so only the closing leg "
                    f"(to 0%) was built. The other side can open in a later "
                    f"session once the book is flat."
                ),
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("constructor side-flip recording failed: %s", exc)


def _apply_repeg(
    pipeline, ctx, *, symbol, order_id: str, trade_row_id, target: float,
    requested_qty, ceiling: float, ask: float | None = None,
    crosses_market: bool = True,
) -> tuple[str, float, str]:
    """The one write-ahead-logged replacement.

    Returns ``(order_id_now_authoritative, superseded_filled_qty, outcome)``
    where `outcome` is a `_REPEG_OUTCOME_TEXT` key.

    The WAL row is the whole point of this function. Between the PATCH
    landing at Alpaca and `repoint_trade_broker_order_id` committing, the
    broker holds a working order under an id this system has written down
    nowhere. A SIGKILL there used to be unrecoverable: the trades row points
    at an order that will report status 'replaced' forever (a status neither
    terminal set in `_reconcile_fills` covers), and the live order is
    untracked. With the row written first, `_drain_pending_repegs` at the next
    session start re-reads the old id, follows Alpaca's `replaced_by` link,
    and repoints the trades row.

    There is no "confirm the swap before the next replace" step any more:
    this is the only replace, and nothing is sent after it. Alpaca's
    one-replace-at-a-time rule (`await_replacement_confirmed`) therefore has
    nothing to protect here.
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
        return order_id, 0.0, "wal_refused"

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
            # from here.
            logger.error(
                "re-peg %s: replacement of %s failed AND the order could not "
                "be re-read — leaving WAL row %s for the session-start drain",
                symbol, order_id, wal_row_id,
            )
            return order_id, 0.0, "replace_unknown"
        if resolved == order_id:
            # Nothing was minted; the original order is still the only one.
            _delete_repeg_wal(pipeline, wal_row_id)
            logger.info(
                "re-peg %s: broker refused the replacement of %s (%s) — the "
                "original order remains authoritative",
                symbol, order_id, (result or {}).get("status", "unknown"),
            )
            _record_pipeline_event(
                pipeline, ctx, symbol, "repeg", "replace_rejected",
                "repeg_replace_rejected", broker_order_id=order_id,
                detail=str((result or {}).get("detail") or
                           (result or {}).get("status") or ""),
            )
            return order_id, 0.0, "replace_rejected"
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
        limit_price=target, ceiling=ceiling, ask=ask,
        crosses_market=bool(crosses_market),
    )
    logger.info(
        "re-peg %s: ONE reprice, order %s → %s at $%.4f (ceiling $%.4f, "
        "ask $%s, crosses market: %s)",
        symbol, order_id, new_id, target, ceiling,
        f"{ask:.4f}" if isinstance(ask, (int, float)) else "?", crosses_market,
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
        )
        return str(new_id), ancestor_filled, "partial_fill"

    return str(new_id), 0.0, "replaced"


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
    even that. (2026-09-12: the per-symbol half is now also carried on
    `TechAnalysisResult.levels_coverage`, and the dead-feed case is a DATA
    fault — `FAULT_NO_STRUCTURE`, `data_fault` row, owner alert — rather
    than a refusal. This run-level check is unchanged and complementary.)

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


# 2026-09-04 fix: before this, `data_status["earnings"]` was "ok" purely on
# whether `earnings_future.result()` returned without raising — the exact
# shape of a real production incident where 12/67 filings once came back
# with a schema-valid `EarningsAnalysis` that had ZERO real extracted
# figures (a parsing bug at the root cause, since fixed). Exception-only
# status can never catch that class of failure, or a future one shaped like
# it, because it never inspects what the seat actually returned. Mirrors the
# macro/news/tech fixes above: coverage of REAL CONTENT is authoritative
# over "did the call merely not crash."
#
# `EarningsAnalysis`'s financial fields are all plain `str`, not
# `Optional[float]` — every one of profitability/cash_flow/balance_sheet
# defaults to the literal sentinel "not disclosed" (see src/models.py) when
# the LLM has nothing to report, and `revenue.total` (no default) can still
# just BE that sentinel string since the schema only requires "some string",
# not a real figure. That sentinel — not `None` and not `0`/falsy — is the
# only honest signal of "absent" this model exposes. A genuinely zero
# balance (e.g. a company with no debt) is reported as an explicit "$0" or
# "0", which is real content and must NOT be confused with "not disclosed" —
# the same "zero is overloaded" mistake this codebase already paid for once
# (docs/WORK.md item 13, PR #255 — a 0% target weight ambiguously meant
# refuse/close/short until it was split into distinct intents). Checking
# for the exact sentinel string, not truthiness, keeps those cases apart.
_EARNINGS_NOT_DISCLOSED = "not disclosed"


def _earnings_field_disclosed(value) -> bool:
    """True when a single EarningsAnalysis string field carries real content
    (anything but blank or the "not disclosed" sentinel, case/whitespace
    insensitive)."""
    text = str(value or "").strip()
    return bool(text) and text.lower() != _EARNINGS_NOT_DISCLOSED


def _earnings_analysis_has_real_figures(analysis: dict) -> bool:
    """Structural content check for one filing's `EarningsAnalysis.model_dump()`.

    Only the fields that should carry an actual filed number are checked —
    `revenue.total`/`yoy_growth`, all of `profitability`, all of `cash_flow`,
    and the two `balance_sheet` figures. `balance_sheet.assessment` is
    deliberately excluded: it is the analyst's own prose judgement, not a
    filed figure, and an LLM will always have SOMETHING to say there even
    when every real number above it is missing — including it would let a
    genuinely content-free filing still read as "has real content".
    """
    if not isinstance(analysis, dict):
        return False
    revenue = analysis.get("revenue") or {}
    profitability = analysis.get("profitability") or {}
    cash_flow = analysis.get("cash_flow") or {}
    balance_sheet = analysis.get("balance_sheet") or {}
    candidate_fields = [
        revenue.get("total"), revenue.get("yoy_growth"),
        profitability.get("gross_margin"), profitability.get("operating_margin"),
        profitability.get("net_income"), profitability.get("eps"),
        cash_flow.get("operating_cf"), cash_flow.get("free_cf"), cash_flow.get("capex"),
        balance_sheet.get("cash_and_equivalents"), balance_sheet.get("total_debt"),
    ]
    return any(_earnings_field_disclosed(v) for v in candidate_fields)


# Phrases in the LLM's own self-reported `EarningsAnalysis.data_quality`
# that indicate a genuine self-reported problem, not routine hedging. The
# owner's own framing (2026-09-04): a seat must never be able to claim "ok"
# while its own free-text field says something is wrong — "if there's no
# data, can it still be... everything's good, just plain lying... since
# it's critical". This is prose, not a structured field, so it is judged by
# substring match on phrases that mean "I could not actually get this data",
# not by one hardcoded exact string.
_EARNINGS_DATA_QUALITY_RED_FLAGS = (
    "unable to find", "unable to extract", "unable to locate",
    "could not extract", "could not find", "could not locate",
    "no figures available", "no financial data", "no data available",
    "not available in the filing", "filing incomplete", "incomplete filing",
    "insufficient data", "no meaningful data", "unable to determine",
    "unable to assess", "data not found", "figures not found",
)


def _earnings_data_quality_flags_problem(data_quality) -> bool:
    """True when the analyst's own `data_quality` self-report describes a
    real extraction problem, even if every structured field technically
    validated. The bare "not disclosed" default (no self-report at all) is
    NOT a red flag by itself — see `_earnings_field_disclosed`'s docstring;
    only prose that actively says something went wrong counts."""
    text = str(data_quality or "").strip().lower()
    if not text or text == _EARNINGS_NOT_DISCLOSED:
        return False
    return any(phrase in text for phrase in _EARNINGS_DATA_QUALITY_RED_FLAGS)


# --- XBRL cross-check: catches a WRONG (not merely empty/self-flagged) figure ---
#
# 2026-09-05: the emptiness/self-report check above closes "the AI returned
# nothing useful." It cannot catch the harder case the owner specifically
# flagged: a confident, plausible-looking, non-empty figure that simply
# doesn't match what the filer actually filed — the AI isn't reporting any
# doubt, so nothing above would ever see a problem. `EarningsDataProvider`
# (src/data/earnings.py) already fetches SEC's own structured XBRL figures
# for exactly this filing and hands the earnings analyst the real numbers
# directly in its prompt (the "STRUCTURED FINANCIAL FACTS" block) — so this
# doesn't need a second data source, just a comparison against ground
# truth that was already fetched.
#
# Scope is deliberately narrow: only NUMERIC, FACTUAL fields that have one
# real correct answer to check against. Prose/judgment fields (strategic
# risk, management execution, investment thesis, valuation context) have no
# single right answer — forcing a "ground truth" for those would be
# inventing a fake one, so they are out of scope on purpose, not an
# oversight.

_UNIT_MULTIPLIERS = {
    "b": 1e9, "bn": 1e9, "billion": 1e9,
    "m": 1e6, "mm": 1e6, "million": 1e6,
    "k": 1e3, "thousand": 1e3,
}

# A number, optionally $-prefixed / comma-separated / %-suffixed / unit-
# suffixed, after any surrounding parens (accounting negative notation) and
# whitespace have already been stripped by the caller.
_FIGURE_RE = re.compile(
    r"^-?\d[\d,]*(?:\.\d+)?\s*(billion|million|thousand|bn|mm|b|m|k)?$",
    re.IGNORECASE,
)


def _parse_reported_figure(value) -> float | None:
    """Best-effort real number out of one `EarningsAnalysis` free-text
    figure field — "$50B", "$5.2 billion", "12%", "($500M)", "$0",
    "not disclosed".

    Returns None for the absence sentinel (see `_earnings_field_disclosed`
    — "not disclosed" is "nothing to compare," never a mismatch) and for
    any text this parser doesn't recognize. An unparseable string is NOT
    evidence of a mismatch — it just means the model phrased it in a way
    this regex doesn't cover — so only a pair of numbers that both parsed
    AND actually disagree should ever be flagged (`_earnings_xbrl_mismatch_fields`).
    A real, disclosed zero ("$0") parses to 0.0, distinct from None.
    """
    if not _earnings_field_disclosed(value):
        return None
    text = str(value).strip()
    negative = text.startswith("(") and text.endswith(")")
    if negative:
        text = text[1:-1].strip()
    text = text.replace(",", "").replace("$", "").replace("%", "").strip()
    match = _FIGURE_RE.match(text)
    if not match:
        return None
    unit = (match.group(1) or "").lower()
    number_text = text[: match.start(1)] if match.group(1) else text
    try:
        number = float(number_text.strip())
    except ValueError:
        return None
    number *= _UNIT_MULTIPLIERS.get(unit, 1)
    return -abs(number) if negative else number


# 5% mirrors the traditional accounting-materiality rule of thumb (SEC Staff
# Accounting Bulletin No. 99 discusses 5% of the relevant base as the
# customary quantitative starting point before any qualitative override)
# and comfortably covers legitimate rounding: paraphrasing an exact XBRL
# figure to 2 significant digits for readability ("~$50B" for an exact
# $49.7B) is under 1% off, well inside this band. A gap bigger than 5% is
# not "rounded for readability" — the analyst's number and the filer's own
# reported number disagree about what happened.
_EARNINGS_XBRL_TOLERANCE_PCT = 0.05

# Floors stop the percentage test from exploding on a near-zero true value
# (breakeven net income, de-minimis cash) where even a trivial rounding
# difference would otherwise read as a huge relative mismatch — only kicks
# in when 5% of the real figure is smaller than this. $10M is small next to
# any filer this fetch actually has data for (see `_fetch_xbrl_raw`'s
# docstring in src/data/earnings.py: MSFT/AAPL/GOOGL/BAC/CVX/NFLX-scale
# filers), so it never masks a real error on a figure that size — it only
# absorbs rounding noise on a near-zero one. $0.02 is one cent above
# ordinary EPS reporting precision (nearest cent), covering print/rounding
# noise without covering an actually-wrong EPS figure.
_EARNINGS_XBRL_DOLLAR_FLOOR = 10_000_000.0
_EARNINGS_XBRL_EPS_FLOOR = 0.02

# (xbrl_key from EarningsDataProvider._xbrl_comparable_values, EarningsAnalysis
# section, field, absolute floor) — only fields where the XBRL concept and
# the model field are the SAME real-world figure by definition. See
# `EarningsDataProvider._XBRL_COMPARABLE_KEYS` (src/data/earnings.py) for
# why margins, total_debt, and cash-flow-statement fields are excluded —
# that exclusion is authoritative; this tuple must stay a subset of it.
_EARNINGS_XBRL_COMPARABLE_FIELDS = (
    ("revenue", "revenue", "total", _EARNINGS_XBRL_DOLLAR_FLOOR),
    ("net_income", "profitability", "net_income", _EARNINGS_XBRL_DOLLAR_FLOOR),
    ("eps", "profitability", "eps", _EARNINGS_XBRL_EPS_FLOOR),
    ("cash", "balance_sheet", "cash_and_equivalents", _EARNINGS_XBRL_DOLLAR_FLOOR),
)


def _earnings_xbrl_mismatch_fields(analysis: dict, xbrl_values: dict) -> list[str]:
    """Which of the analyst's own reported figures materially contradict
    the real SEC XBRL values already fetched for this same filing.

    Returns "section.field" names for every real disagreement — empty when
    nothing was comparable at all (no XBRL data for this filer/period —
    `xbrl_values` fails open to `{}`, see `EarningsReport.xbrl_facts`) or
    when every comparable field agreed within `_EARNINGS_XBRL_TOLERANCE_PCT`.
    A field the analyst reported as "not disclosed", or in a format
    `_parse_reported_figure` doesn't recognize, is skipped rather than
    flagged — only an ACTUAL, provable disagreement between two real parsed
    numbers counts.
    """
    if not isinstance(analysis, dict) or not xbrl_values:
        return []
    mismatches = []
    for xbrl_key, section, field_name, floor in _EARNINGS_XBRL_COMPARABLE_FIELDS:
        true_value = xbrl_values.get(xbrl_key)
        if true_value is None:
            continue
        reported = _parse_reported_figure((analysis.get(section) or {}).get(field_name))
        if reported is None:
            continue
        tolerance = max(abs(true_value) * _EARNINGS_XBRL_TOLERANCE_PCT, floor)
        if abs(reported - true_value) > tolerance:
            mismatches.append(f"{section}.{field_name}")
    return mismatches


def _classify_earnings_status(earnings_results: list) -> str:
    """Turn this run's `earnings_results` into an honest `data_status["earnings"]`.

    `earnings_results` items are `{"symbol", "analysis", "xbrl_facts",
    "queued", ...}` dicts from `EarningsAnalystAgent`/`_load_earnings_analyses`
    — see `EarningsAnalystAgent._analyze_new`/`_load_analysis`. Only items
    that actually carry an `analysis` dict are judged for content here; a
    `queued=True` placeholder (a new filing preprocess hasn't analyzed yet)
    is already surfaced honestly by that flag and sized around downstream —
    that is a separate, already-handled state this pass does not touch.

    - No analyzed items at all (no filings today, or every filing is still
      queued) is the ordinary, most-common day and stays "ok" — earnings
      not existing is not a data failure.
    - Every analyzed item has real figures and a clean self-report → "ok".
    - Some real, some not → "partial" (mirrors tech's partial for a
      mixed batch).
    - An analyzed item exists but NONE of them have real content (bad
      structural content, a red-flagged self-report, or both) → a status
      distinct from both "ok" and "empty". Deliberately NOT "empty": the
      notifier's data-quality alert (`maybe_alert_data_quality`,
      src/notifier.py) treats any status outside `("ok", "empty")` as
      alert-worthy, and unlike smart_money's "empty" (a quiet day is
      genuinely benign), earnings content going missing AFTER a real
      filing existed and was run through the LLM is never benign — it is
      the exact silent-failure shape this fix exists to catch, so it must
      alert every time, not blend into the "nothing to see" bucket.
    - Any analyzed item's own reported figures materially CONTRADICT the
      real SEC XBRL data already fetched for that filing → "figures_contradicted",
      regardless of how many other filings this run were clean. Deliberately
      a distinct status, not folded into "content_missing": those two are
      different failure shapes an operator needs to read differently — one
      says "the seat gave us nothing," the other says "the seat gave us a
      confident, wrong answer," which is the specific silent-failure shape
      this cross-check exists to catch and is worse than empty, not the
      same as it. Deliberately dominant over "ok"/"partial" too — a batch
      that is otherwise clean but contains one provably wrong filing is not
      "mostly fine," and diluting it into "partial" (the routine, expected
      bucket for an ordinary mixed day) would bury exactly the alert this
      exists to raise.
    """
    analyzed = [
        item for item in earnings_results
        if isinstance(item, dict) and isinstance(item.get("analysis"), dict)
    ]
    if not analyzed:
        return "ok"

    good = []
    contradicted = []
    for item in analyzed:
        a = item["analysis"]
        if not _earnings_analysis_has_real_figures(a) or _earnings_data_quality_flags_problem(
            a.get("data_quality")
        ):
            continue
        mismatches = _earnings_xbrl_mismatch_fields(a, item.get("xbrl_facts") or {})
        if mismatches:
            contradicted.append((item.get("symbol", "?"), mismatches))
            continue
        good.append(a)

    if contradicted:
        logger.error(
            "Earnings: %d filing(s) this run reported figures that "
            "materially contradict SEC XBRL data — %s",
            len(contradicted),
            "; ".join(f"{sym}: {', '.join(fields)}" for sym, fields in contradicted),
        )
        return "figures_contradicted"
    if len(good) == len(analyzed):
        return "ok"
    if good:
        return "partial"
    return "content_missing"


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


def _execution_payoff_skip_reason(
    decision,
    *,
    sizing_price: float,
    stop_price: float,
    geometry_changed: bool,
    is_short: bool,
) -> str | None:
    """Never skip on reward:risk — computed, thin, or unmeasurable.

    Owner 2026-09-17: invented R/R gates are a defect. Overnight bind:
    honesty about a payoff without a number is a recorded fact / ranking
    hint, not a refuse. Historical runs wrote `geometry_rr` (1.2 belt)
    and this helper briefly wrote `geometry_unmeasurable`; neither token
    is emitted. Arguments are accepted so callers and tests keep the
    same signature; none of them decide admission.
    """
    _ = (decision, sizing_price, stop_price, geometry_changed, is_short)
    return None


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
            is_scale_in = bool(spec.get("cover_full_position"))
            if is_scale_in:
                remedy = (
                    "The resting protective sell was cancelled so this add "
                    "could submit. Place a stop over the FULL position now "
                    "or flatten. The coverage sweep will also try to repair "
                    "from the write-ahead recovery row."
                )
                body = (
                    "STOP NOT REARMED AFTER A SCALE-IN\n"
                    f"{symbol}: the entry filled ({held} share(s)) but the "
                    "protective sell covering the full position could not be "
                    "placed after every immediate retry. The position is "
                    "open at the broker with NOTHING standing watch.\n"
                    f"Intended stop: {stop_price}\n"
                    f"Entry order: {entry_order_id}\n"
                    f"{remedy}"
                )
            else:
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


def _alert_holding_discipline_block(
    *, symbol: str, action: str, reasons: tuple[str, ...] | list[str],
) -> None:
    """Standalone owner alert: an exit was BLOCKED because the justification
    the Risk Manager gave for it is contradicted by the desk's own data.

    Own message, never bundled into the run summary, and severity carried in
    plain words — not colour and not an emoji standing in as the only
    signal (item 21, owner's alert-design rule). The leading emoji here is
    decoration on top of a plain-text header that already says everything,
    exactly as `_alert_protection_failure` above does.

    Deliberately NOT deduplicated, for the same reason
    `maybe_alert_data_quality` is not: the whole point of wiring this alert
    in alongside the 2026-09-04 escalation to a real veto is to MEASURE how
    often a stated exit reason turns out to be false. Suppressing repeats
    would destroy the count the owner asked for.

    Never raises — an alerting bug must not break the trading path it
    reports on.
    """
    try:
        detail = "\n".join(f"- The reasoning {r}." for r in reasons)
        body = (
            "🛑 TRADE BLOCKED — THE STATED REASON FOR SELLING WAS NOT TRUE\n"
            f"{symbol}: a {action} was proposed on a position whose thesis is "
            "still structurally intact, and the reason given for it does not "
            "match what actually happened today:\n"
            f"{detail}\n"
            "The trade was BLOCKED and no order was sent. This is not a "
            "judgement about whether selling is right — only that the "
            "specific justification given is contradicted by the data on "
            "record, so it cannot stand on that reason. If there is a real "
            "reason to exit, it can be proposed again next cycle citing it."
        )
        from src import notifier as _notifier

        _notifier.send_owner_alert(body, symbols=[str(symbol)])
    except Exception as exc:  # noqa: BLE001
        logger.error("holding-discipline block owner alert failed: %s", exc)


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


#: The seat this accounting spends its one paid retry under. Distinct from
#: `portfolio_manager` itself so a bookkeeping re-ask can never consume the
#: retry another PM heal may need in the same session, and vice versa.
_PM_ACCOUNTING_SEAT = "portfolio_manager_candidate_accounting"


def _record_accounted_candidate(pipeline, ctx, accounted) -> None:
    """One durable per-symbol row for one accounted candidate.

    `refusal` carries the comparable CODE and `note` the reader-facing
    prose. That split is deliberate: `refusal_signature.signature_key`
    reads `refusal` and does not read `note`, so two sessions are compared
    on the named ground rather than on the seat's choice of words.
    """
    payload = accounted.event_kwargs()
    _record_pipeline_event(
        pipeline, ctx, accounted.symbol,
        payload["stage"], payload["outcome"], payload["reason"],
        refusal=payload["refusal"], note=payload["note"],
    )


def _account_for_pm_candidates(
    pipeline, ctx, *, run_id, analyses, positions, decision, pm_decide_kwargs,
) -> None:
    """Make the portfolio manager account for every candidate it was shown.

    Board item 133 (2026-09-18). Replaces the loop that recorded every
    analysed candidate missing from `targets` as
    `omitted / candidate_not_selected_for_target` — one unvarying string
    that was not a reason, because the seat was never asked for one. See
    `src/pm_accounting.py` for why that defeated the jam detector by
    construction.

    The desk's standing heal order, no step skipped and no new retry
    invented: mechanical heal from what the seat did say, ONE re-ask under
    the existing per-seat paid-retry cap, then a durable per-symbol reason.

    Decides nothing. Every candidate's trading fate was already settled by
    `decide()` before this function runs; all that changes here is whether
    the desk can say WHY. It never raises — a bookkeeping failure must not
    take a live session with it.
    """
    from src.pm_accounting import (
        REASK_DIRECTIVE, account_for_candidates, unaccounted_row,
    )
    from src.seat_heal import (
        HealResult, HEAL_CAP_BLOCKED, HEAL_FAILED, HEAL_MECHANICAL,
        HEAL_PAID_RETRY, can_paid_retry, record_paid_retry,
    )

    result = account_for_candidates(
        analyses=analyses, decision=decision, positions=positions,
    )
    for accounted in result.accounted:
        _record_accounted_candidate(pipeline, ctx, accounted)
    if not result.unaccounted:
        if result.accounted:
            logger.info(
                "PM candidate accounting: every one of the %d non-targeted "
                "candidate(s) carries a named ground", len(result.accounted),
            )
        return

    pending = sorted(result.unaccounted)
    logger.warning(
        "PM candidate accounting: the seat dropped %s without naming a "
        "ground. Re-asking once (bookkeeping only).", ", ".join(pending),
    )

    def _finish(symbols, *, asked: bool) -> None:
        for symbol in sorted(symbols):
            _record_accounted_candidate(
                pipeline, ctx, unaccounted_row(symbol, asked=asked),
            )

    retries = dict(getattr(ctx, "heal_paid_retries", None) or {})
    if not can_paid_retry(retries, _PM_ACCOUNTING_SEAT):
        logger.warning(
            "PM candidate accounting: the one re-ask for this seat is "
            "already spent this session — %s stay(s) unaccounted and "
            "recorded", ", ".join(pending),
        )
        _finish(pending, asked=False)
        return

    from src.cost_circuit import PaidAnalysisSuspended
    try:
        pipeline._require_paid_analysis("portfolio_manager")
    except PaidAnalysisSuspended as exc:
        _record_heal_safely(pipeline, ctx, HealResult(
            seat=_PM_ACCOUNTING_SEAT, outcome=HEAL_CAP_BLOCKED,
            reason=(
                "spend cap blocked the candidate-accounting re-ask: "
                f"{exc}"
            ),
            details={"symbols": pending},
        ))
        _finish(pending, asked=False)
        return

    ctx.heal_paid_retries = record_paid_retry(retries, _PM_ACCOUNTING_SEAT)
    challenge = REASK_DIRECTIVE + ", ".join(pending)
    try:
        reasked, reask_result = pipeline.portfolio_manager.decide(
            **{**pm_decide_kwargs, "accounting_challenge": challenge},
        )
    except Exception as exc:  # noqa: BLE001
        _record_heal_safely(pipeline, ctx, HealResult(
            seat=_PM_ACCOUNTING_SEAT, outcome=HEAL_FAILED,
            reason=f"candidate-accounting re-ask raised: {exc}",
            paid_retry=True, details={"symbols": pending},
        ))
        _finish(pending, asked=True)
        return

    try:
        pipeline.db.insert_agent_log(
            agent_name="portfolio_manager", run_id=run_id,
            input_summary=(
                f"candidate-accounting re-ask | {', '.join(pending)}"
            ),
            input_message=reask_result.user_message,
            output_summary=(
                reasked.portfolio_view if reasked else "parse_error"
            ),
            full_response=reask_result.raw_text,
            model=reask_result.model,
            tokens_used=reask_result.tokens_used,
            input_tokens=reask_result.input_tokens,
            output_tokens=reask_result.output_tokens,
            cost_usd=reask_result.cost_usd,
            **agent_log_kwargs(reask_result),
        )
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "PM candidate accounting: re-ask log write failed: %s", e,
        )

    if reasked is None:
        _record_heal_safely(pipeline, ctx, HealResult(
            seat=_PM_ACCOUNTING_SEAT, outcome=HEAL_FAILED,
            reason="candidate-accounting re-ask returned no parseable decision",
            paid_retry=True, details={"symbols": pending},
        ))
        _finish(pending, asked=True)
        return

    # BOOKKEEPING ONLY. The re-ask's targets, sizes and reasoning_chain are
    # DISCARDED: the trade decision was made on the first call and a paid
    # retry must never become a way to re-decide the book. Only rejections
    # naming a CHALLENGED symbol are taken, and only where the first answer
    # had none — so the re-ask cannot overwrite a ground the seat already
    # stated, nor invent an accounting for a name it was not asked about.
    challenged = set(pending)
    already = {
        str(getattr(r, "symbol", "") or "").strip().upper()
        for r in (getattr(decision, "rejections", None) or [])
    }
    gained = [
        r for r in (getattr(reasked, "rejections", None) or [])
        if str(getattr(r, "symbol", "") or "").strip().upper() in challenged
        and str(getattr(r, "symbol", "") or "").strip().upper() not in already
    ]
    if gained:
        try:
            decision.rejections = list(
                getattr(decision, "rejections", None) or [],
            ) + gained
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "PM candidate accounting: could not attach the re-asked "
                "rejections to the decision (%s) — they are still recorded "
                "per symbol below", e,
            )

    second = account_for_candidates(
        analyses=[a for a in analyses
                  if str(getattr(a, "symbol", "") or "").strip().upper()
                  in challenged],
        decision=decision, positions=positions,
    )
    for accounted in second.accounted:
        _record_accounted_candidate(pipeline, ctx, accounted)
    still = sorted(second.unaccounted)
    logger.info(
        "PM candidate accounting: the re-ask accounted for %d of %d "
        "challenged candidate(s); %d still unaccounted",
        len(second.accounted), len(pending), len(still),
    )

    if not still:
        _record_heal_safely(pipeline, ctx, HealResult(
            seat=_PM_ACCOUNTING_SEAT, outcome=HEAL_PAID_RETRY,
            reason=(
                "the candidate-accounting re-ask named a ground for every "
                "candidate it was asked about"
            ),
            paid_retry=True, usable=True, details={"symbols": pending},
        ), alert=False)
        return

    _finish(still, asked=True)
    _record_heal_safely(pipeline, ctx, HealResult(
        seat=_PM_ACCOUNTING_SEAT, outcome=HEAL_FAILED,
        reason=(
            "the portfolio manager would not name a ground for: "
            + ", ".join(still) + ". Nothing was bought or sold differently "
            "because of this — what is lost is the desk's ability to say "
            "why these candidates were dropped. Recorded per symbol."
        ),
        paid_retry=True, details={"symbols": still},
        owner_consequence=(
            "NOTHING was bought, sold or held differently because of this — "
            "the decision itself was already made and stands. What is "
            "missing is the desk's account of why it passed on these names."
        ),
    ))
    # Mechanical-heal bookkeeping, so a reader can tell a session where the
    # seat answered from one where the code recovered the answer.
    if second.accounted:
        _record_heal_safely(pipeline, ctx, HealResult(
            seat=_PM_ACCOUNTING_SEAT, outcome=HEAL_MECHANICAL,
            reason=(
                f"{len(second.accounted)} candidate(s) accounted for after "
                "the re-ask"
            ),
            mechanical=True, paid_retry=True, usable=True,
        ), alert=False)


def _record_heal_safely(pipeline, ctx, result, alert: bool = True) -> None:
    """`TradingPipeline._record_heal`, but never fatal to the session.

    The accounting path is bookkeeping; a failure to WRITE the bookkeeping
    must not be able to end a live trading session.
    """
    try:
        pipeline._record_heal(ctx, result, alert=alert)
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "PM candidate accounting: could not record the heal result "
            "(%s): %s", getattr(result, "outcome", "?"), e,
        )


def _target_increase_missing_falsifier(
    target, *, positions=None, total_value: float = 0.0,
    existing_risk_pct=None,
) -> bool:
    """True iff this target is an open/increase still missing a real falsifier.

    Classification reuses `PortfolioManagerAgent._target_intent` (current
    size/risk vs the proposed target). Reductions and closes are never
    this check — a blank `thesis_invalid_if` on a checkable size-down
    is not a missing open falsifier. The constructor still must name a
    mechanical live-book warrant rather than PM thesis; that is not
    this function. Fail-safe matches `_target_intent`: unknown current
    risk is treated as an increase, the stricter gate. Never invents a
    string.
    """
    held = {
        str(getattr(p, "symbol", "")).upper(): p
        for p in list(positions or [])
        if getattr(p, "symbol", None)
    }
    intent = PortfolioManagerAgent._target_intent(
        target, held, total_value, existing_risk_pct=existing_risk_pct,
    )
    return open_target_missing_falsifier(target, intent=intent)


def _targets_admitted_to_book(
    targets, *, positions=None, total_value: float = 0.0,
    existing_risk_pct=None,
) -> tuple[list, list[str]]:
    """Open/increase names still missing a real falsifier never reach the constructor.

    Permanent never-blank path: heal + one paid retry already ran on the
    PM seat so it actually produces the field. Remaining blanks on
    opens/increases are refused here so they do not consume risk budget
    or become tickets. Reductions and closes with a blank
    `thesis_invalid_if` are admitted only when `_target_intent`
    classifies a checkable size-down vs the live book (risk/weight).
    Those are not soft-exits: the constructor stamps a mechanical
    size-down warrant as the named trigger. PM thesis free text cannot
    create the sell. Classification reuses `_target_intent`.
    That label can disagree with the constructor's order side when a
    lower risk request meets a tighter stop (more shares). The RiskStage
    isolate still drops any constructed BUY/SHORT whose falsifier is
    blank, so a mis-labelled add cannot be ticketed. That refuse is
    last-resort after the producing step was asked, not skip-and-continue
    as the product (owner 2026-09-17). Targets stay on the proposal so
    Risk is told why the narrative names a symbol that is not in the
    list. Never invents a falsifier or catalyst string.
    """
    admitted: list = []
    refused: list[str] = []
    for target in list(targets or []):
        if _target_increase_missing_falsifier(
            target, positions=positions, total_value=total_value,
            existing_risk_pct=existing_risk_pct,
        ):
            refused.append(str(target.symbol).upper())
        else:
            admitted.append(target)
    return admitted, refused


def _record_soft_exit_missing_after_retry(
    pipeline, ctx, symbol: str, *, action: str | None = None,
) -> None:
    _record_pipeline_event(
        pipeline, ctx, symbol, "deterministic_gate",
        "blocked", SOFT_EXIT_MISSING_AFTER_RETRY,
        detail=(
            "thesis_invalid_if still empty or unknown after "
            "mechanical heal and one paid retry; refusing this "
            "name before the book. No falsifier was invented."
        ),
        **({"action": action} if action else {}),
    )


def _isolate_empty_soft_exit_entries(pipeline, ctx, portfolio_decision) -> list[str]:
    """Refuse BUY/SHORT names still missing a real falsifier after heal+retry.

    TEMPORARY last-resort (#432 isolate-name). Owner 2026-09-17: skip/drop
    is not the product — the producing step must fill. The permanent path
    is schema + prompt + mechanical heal + one paid seat retry, then
    refuse before construct_orders. This filter does not invent a
    thesis_invalid_if or catalyst string. It does not delete the target
    — Risk must still be told the name was proposed and refused. Delete
    this isolate when a live session proves no actionable name arrives
    blank.

    Catches empty AND `unknown` on BUY/SHORT — omitted empty is missing,
    not "the analyst had nothing to say". Neutrals, reductions and closes
    are not this filter. A constructed BUY/SHORT with a blank falsifier
    is still dropped even when `_target_intent` labelled the target a
    reduction (risk-down / weight-up from a tighter stop).
    """
    if portfolio_decision is None:
        return []
    existing_risk_pct, _ = _book_risk_inputs(
        ctx, getattr(ctx, "total_value", 0.0) or 0.0,
    )
    missing_symbols = {
        str(target.symbol).upper()
        for target in list(getattr(portfolio_decision, "targets", None) or [])
        if _target_increase_missing_falsifier(
            target,
            positions=getattr(ctx, "positions", None),
            total_value=getattr(ctx, "total_value", 0.0) or 0.0,
            existing_risk_pct=existing_risk_pct,
        )
    }
    isolated: list[str] = []
    kept = []
    for decision in list(getattr(portfolio_decision, "decisions", None) or []):
        symbol = str(decision.symbol).upper()
        missing_here = (
            symbol in missing_symbols
            or (
                decision.action in ("BUY", "SHORT")
                and missing_stated_falsifier(
                    getattr(decision, "thesis_invalid_if", None)
                )
            )
        )
        if decision.action in ("BUY", "SHORT") and missing_here:
            isolated.append(symbol)
            _record_soft_exit_missing_after_retry(
                pipeline, ctx, decision.symbol, action=decision.action,
            )
            continue
        kept.append(decision)
    if not isolated:
        return []
    unique = list(dict.fromkeys(isolated))
    logger.warning(
        "Refusing %d BUY/SHORT name(s) %s: %s",
        len(unique), SOFT_EXIT_MISSING_AFTER_RETRY, unique,
    )
    portfolio_decision.decisions = kept
    existing = list(getattr(portfolio_decision, "constructor_dropped", None) or [])
    for symbol in unique:
        if symbol not in existing:
            existing.append(symbol)
    portfolio_decision.constructor_dropped = existing
    return unique


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
    """Dual-shape read: a live MacroAnalysis or a MacroStore snapshot.

    MacroStore now persists `reasoning_chain` and `sector_guidance_rows`
    so a same-day snapshot can re-validate. Coerce then validate; a trim
    that still cannot parse is None plus a durable fail reason — never a
    broken dict smuggled into PM.
    """
    if macro_analysis is None:
        return None
    from src.models import MacroAnalysis
    from src.seat_heal import coerce_macro_shape, describe_macro_parse_failure
    if isinstance(macro_analysis, MacroAnalysis):
        return macro_analysis.model_dump()
    if isinstance(macro_analysis, dict):
        payload, _fixes = coerce_macro_shape(macro_analysis)
    else:
        dump = getattr(macro_analysis, "model_dump", None)
        payload = dump() if callable(dump) else None
        if not isinstance(payload, dict):
            return None
        payload, _fixes = coerce_macro_shape(payload)
    try:
        return MacroAnalysis.model_validate(payload).model_dump()
    except Exception as exc:
        reason = describe_macro_parse_failure(payload, exc)
        logger.error(
            "macro_analysis failed to parse after coerce: %s", reason, exc_info=True,
        )
        _stash_macro_parse_failure(reason)
        return None


def _stash_macro_parse_failure(reason: str) -> None:
    """One durable reason, de-duplicated, drained by DecisionStage."""
    from src.agents.portfolio_manager import PortfolioManagerAgent
    failures = getattr(PortfolioManagerAgent, "_macro_parse_failures", None)
    if not isinstance(failures, list):
        PortfolioManagerAgent._macro_parse_failures = []
        failures = PortfolioManagerAgent._macro_parse_failures
    if reason not in failures:
        failures.append(reason)



def _revert_entry_size_increases(decisions, pre_alloc: dict) -> tuple[list, list[dict]]:
    """Revert any RM edit that ENLARGED a BUY *or a SHORT* (board item 135).

    `_apply_risk_modifications` guard 1b already does this for a BUY. It was
    written `decision.action == "BUY"`, so a SHORT — sized by the mirror of
    the same cumulative clamp in the constructor, and scaled alongside BUY by
    `_apply_scale_all_buys` precisely because both open new risk — could be
    enlarged by an edit the seat believed was protective. On a short that ADDS
    to a short already held, `allocation_pct` is an increment exactly as it is
    on a long add, so an upward edit grows the short by more than the number
    reads.

    Enforced here rather than inside guard 1b only because this sweep needs
    the pre-modification sizes, which this stage already has. Guard 1b stays:
    it fires first and more specifically for a BUY, and this is a no-op behind
    it. If the two are ever consolidated, consolidate ONTO guard 1b.

    Fails toward the SMALLER size in every branch: the pre-modification value
    is the constructor's own, which is already clamped by the single-name
    ceiling, the risk budget and the sector dial. Nothing here can raise a
    size, and a decision whose pre-modification size is unknown is left
    untouched rather than guessed at.
    """
    out: list = []
    rejected: list[dict] = []
    for d in decisions:
        if d is None or d.action not in ("BUY", "SHORT"):
            out.append(d)
            continue
        before = pre_alloc.get((d.symbol.strip().upper(), d.action))
        if before is None or d.allocation_pct <= before:
            out.append(d)
            continue
        reason = (
            f"RM modification would INCREASE {d.symbol}'s {d.action} "
            f"allocation_pct ({before:.2f} -> {d.allocation_pct:.2f}). "
            f"Reverted — the risk seat may only reduce an entry's size, "
            f"never enlarge it; on an add to a position already held this "
            f"field is an increment, so an upward edit grows the position by "
            f"more than the number reads."
        )
        logger.warning("Risk mod REJECTED for %s: %s", d.symbol, reason)
        rejected.append({
            "symbol": d.symbol, "field": "allocation_pct", "reason": reason,
        })
        try:
            out.append(d.model_copy(update={"allocation_pct": before}))
        except Exception as e:
            # Cannot restore the smaller size, so do not ship the larger one.
            logger.warning(
                "Could not revert %s's enlarged allocation_pct (%s) — "
                "DROPPING the decision rather than executing the increase",
                d.symbol, e,
            )
            # Board item 164: the entry above says "Reverted", which is no
            # longer true — the decision is gone. Restate it as the drop it
            # is, so the durable record does not claim a trade shipped at
            # its pre-edit size when nothing shipped at all.
            rejected[-1].update({
                "outcome": "dropped",
                "gate": "rm_enlargement_revert_failed",
                "action": d.action,
                "before": before,
                "requested": d.allocation_pct,
                "reason": (
                    f"RM modification would INCREASE {d.symbol}'s {d.action} "
                    f"allocation_pct ({before:.2f} -> {d.allocation_pct:.2f}); "
                    f"restoring the pre-edit size failed ({e}), so the "
                    f"{d.action} was DROPPED rather than shipped at the "
                    f"enlarged size."
                ),
            })
    return out, rejected


#: The four fields a risk-seat edit or `scale_all_buys` can change — the
#: same set `_apply_risk_modifications` accepts (`modifiable_fields` there).
_RISK_EDITABLE_FIELDS = ("allocation_pct", "entry_price", "stop_loss", "take_profit")


def _risk_edit_snapshot(decisions) -> dict:
    """`{(SYMBOL, action): {field: value}}` for the editable fields."""
    out: dict = {}
    for d in decisions or []:
        if d is None:
            continue
        out[(d.symbol.strip().upper(), d.action)] = {
            f: getattr(d, f, None) for f in _RISK_EDITABLE_FIELDS
        }
    return out


def _risk_event_for(
    decision, pre_rm_fields: dict, verdict, scale: float,
    field_aliases: dict | None = None,
):
    """The per-symbol `risk` event for a leg that SURVIVED the risk seat.

    Board item 164 (2026-09-19). This event used to carry the constant
    reason `risk_manager_verdict` on every symbol, and read `modified` for
    any symbol the seat merely NAMED in a modification — including an edit
    that was rejected, reverted or never matched — and for every leg,
    exits included, whenever `scale_all_buys` was below 1. Now:

    - `outcome` is `modified` only when a field of this decision actually
      differs from what it was before the seat's edits and scaling;
    - `reason` is the seat's OWN reason for this symbol — the stated reason
      on each modification that took effect — and only where the seat gave
      none does it say so in words;
    - `changes` carries every field that moved, as `[before, after]`.

    Pure: reads the decision, the snapshot and the verdict; changes nothing.
    """
    key = (decision.symbol.strip().upper(), decision.action)
    before = pre_rm_fields.get(key) or {}
    changes = {
        f: [before[f], getattr(decision, f, None)]
        for f in _RISK_EDITABLE_FIELDS
        if f in before and before[f] != getattr(decision, f, None)
    }
    category = getattr(verdict, "reason_category", None)
    details: dict = {"gate": "risk_manager", "reason_category": category}
    if not changes:
        reason = (
            f"risk manager approved {decision.symbol} unchanged; the seat "
            f"gave no reason specific to this symbol (verdict category "
            f"{category!r}; its run-level reasoning is on this run's verdict "
            f"row)"
        )
        return "approved", reason, details
    aliases = field_aliases if isinstance(field_aliases, dict) else {}
    seat_reasons = []
    for m in (getattr(verdict, "modifications", None) or []):
        field = aliases.get(m.field, m.field)
        if (
            m.symbol.strip().upper() == key[0] and field in changes
            and (m.reason or "").strip()
        ):
            seat_reasons.append(f"{field}: {m.reason}")
    if (
        scale < 1.0 and decision.action in ("BUY", "SHORT")
        and "allocation_pct" in changes
    ):
        seat_reasons.append(f"scale_all_buys={scale:.2f} applied to every entry")
    details["changes"] = changes
    reason = "; ".join(seat_reasons) or (
        f"risk manager changed {', '.join(sorted(changes))} on "
        f"{decision.symbol} without stating a reason for this symbol"
    )
    return "modified", reason, details


def _apply_scale_all_buys(decisions, verdict) -> tuple[list, float, list]:
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

    Returns ``(scaled_decisions, scale, dropped)`` so the caller can use the
    coerced scale for follow-up filters (re-running hard risk if the
    scale dropped allocations into different buckets) and file a visible
    pipeline event for every entry the scaling removed outright (board item
    136 — see the drop branch below).
    """
    scale_raw = getattr(verdict, "scale_all_buys", 1.0)
    scale = 1.0 if scale_raw is None else float(scale_raw)
    dropped: list[tuple[str, float]] = []
    if scale >= 1.0 or scale < 0.0:
        return list(decisions), scale, dropped

    scaled: list = []
    for d in decisions:
        if d.action in ("BUY", "SHORT"):
            new_alloc = max(0.0, min(100.0, d.allocation_pct * scale))
            if new_alloc <= 0:
                logger.info(
                    "scale_all_buys=%.2f drops %s (alloc 0 after scaling)",
                    scale, d.symbol,
                )
                # Board item 136. The DROP is correct and stays: this desk's
                # standing rule is that a refusal must remove a target, never
                # zero one, because an allocation_pct of 0 reads as SKIP at
                # execution (the same convention guard 1 in
                # `_apply_risk_modifications` protects). What was wrong is
                # that the drop left NO trace anywhere the desk can read —
                # a logger line only, and the decision is gone from the list
                # before the per-decision event loop in `RiskStage.run`
                # runs, so `scale_all_buys=0.0` silently deleted the whole
                # entry side with no pipeline event for any symbol. Recorded
                # here so the caller can file one per dropped name.
                dropped.append((d.symbol, d.allocation_pct))
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
    return scaled, scale, dropped


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
        admit_screened_universe_fn=None,
        event_calendar: "MacroEventCalendarProvider | None" = None,
        fomc_calendar: "FOMCCalendarProvider | None" = None,
        live_session_context_fn=None,
    ):
        self.config = config
        self.db = db
        self.market = market
        # (symbols) -> {sym: snapshot | {"live_unavailable": reason}}; {}
        # outside regular hours. Optional so existing construction sites
        # keep working; absent means "no live price" (pre-2026-09-14).
        self._live_session_context = live_session_context_fn
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
        # Universe screen (src/universe_screen.py): (positions) ->
        # (symbols, details) for this session's share of the screened,
        # persisted universe. Returns nothing while the screen is off.
        self._admit_screened_universe = admit_screened_universe_fn
        # Injected callables so we don't duplicate pre-filter / news / earnings
        # orchestration logic. Those still live on TradingPipeline for now
        # because they touch shared state we haven't finished extracting.
        self._has_actionable_signal = has_actionable_signal_fn
        self._run_news_update = run_news_update_fn
        self._load_earnings_analyses = load_earnings_analyses_fn

    def _live_context(self, symbols: list[str]) -> dict[str, dict]:
        """Injected live-session read; {} when not wired or on failure."""
        if self._live_session_context is None:
            return {}
        try:
            return self._live_session_context(symbols) or {}
        except Exception as exc:  # noqa: BLE001
            logger.warning("live session context failed (%s) — Tech sees completed bars only", exc)
            return {}

    @staticmethod
    def _live_price_kwarg(live_context: dict, symbol: str) -> dict:
        """`{"live_price": x}` only when a usable live price exists, so an
        injected prefilter with the old 4-argument signature still works
        outside market hours.

        Reads `live_price` — the freshness-RESOLVED number
        `_live_session_context` publishes (docs/WORK.md item 120) — not the
        raw provider `last_price`, which can be a prior session's print.
        """
        ic = live_context.get(symbol) or {}
        price = ic.get("live_price")
        if ic.get("live_unavailable") or not isinstance(price, (int, float)) or price <= 0:
            return {}
        return {"live_price": price}

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
        if self._admit_screened_universe:
            try:
                screened, screened_details = self._admit_screened_universe(
                    getattr(ctx, "positions", None) or [],
                )
            except Exception as exc:  # noqa: BLE001
                # Fails closed: no screened name this session, nothing else lost.
                logger.warning("Screened-universe admission failed closed: %s", exc)
                screened, screened_details = set(), {}
            for symbol in sorted(screened):
                if symbol in ctx.admitted_symbols:
                    continue
                ctx.admitted_symbols.add(symbol)
                ctx.smart_money_admissions[symbol] = screened_details.get(symbol, {})
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
            screened = admission.get("reason") == "universe_screen_admission"
            _persist_evidence(
                self.db, run_id=ctx.run_id,
                agent_name="universe_screen" if screened else "smart_money_analyst",
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
                "universe_screen_admission" if screened else "smart_money_form4_admission",
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
                    from src.data.macro_store import series_prints_from_summary
                    self.macro_store.save_last_state(
                        analysis.model_dump(),
                        series_prints=series_prints_from_summary(
                            macro_summary,
                            freshness=getattr(self.macro, "_run_freshness", None),
                        ),
                    )
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
            # Completed bars end at the previous close while the session is
            # open; price-vs-level judgement needs the live price (2026-09-14).
            live_context = self._live_context([s["symbol"] for s in all_symbols_data])
            symbols_data = [
                s for s in all_symbols_data
                if (
                    s["symbol"] in ctx.admitted_symbols
                    or self._has_actionable_signal(
                        s["indicators"], s["symbol"], s["bars"], ctx.positions,
                        **self._live_price_kwarg(live_context, s["symbol"]),
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
                intraday_context={
                    s["symbol"]: live_context[s["symbol"]]
                    for s in symbols_data if s["symbol"] in live_context
                },
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

        # How much of the watched set the pre-market Form 4 pass actually
        # read, per issuer. No network. The analyst's answer covers only the
        # names read through; below, a clean status is downgraded to
        # `partial` when any watched name still has unread filings, so the
        # seat never reports `ok` / `empty` over filings nobody read.
        sm_coverage = None
        coverage_probe = getattr(self.smart_money_provider, "form4_coverage", None)
        if smart_config and smart_config.enabled and callable(coverage_probe):
            try:
                sm_coverage = coverage_probe()
                if not isinstance(sm_coverage, dict):
                    sm_coverage = None
            except Exception as exc:  # noqa: BLE001
                logger.warning("Smart-money coverage read failed: %s", exc)
                sm_coverage = {"known": False, "error": type(exc).__name__}
        # How old the congressional evidence is (newest disclosure, newest
        # trade, each source's copy). No network; None when that feed is off.
        sm_congressional = None
        congress_probe = getattr(self.smart_money_provider, "congressional_freshness", None)
        if smart_config and smart_config.enabled and callable(congress_probe):
            try:
                sm_congressional = congress_probe()
            except Exception as exc:  # noqa: BLE001
                logger.warning("Congressional freshness read failed: %s", exc)
                sm_congressional = {"known": False, "error": type(exc).__name__}
        # Board item 126. EDGAR publishes its own count of the Form 4s filed
        # on each day. Until this shipped, a fetch that came back with
        # nothing because it was BROKEN and a day on which genuinely nobody
        # filed produced identical evidence — zero rows, no provider error,
        # status "ok". `verified` is the fetch saying it read EDGAR's own
        # count and walked it; unverified means the desk cannot tell those
        # two apart, which is not the same claim as "no signal found".
        #
        # This deliberately does NOT fire on a scan that spent its own
        # budget or deadline: those are the desk's bounded choices, their
        # residue is already reported through `unread` below, and treating
        # them as failure would make the seat degraded every day — the harm
        # board item 126 names in its own text. See
        # `UNVERIFIED_EDGAR_REASONS` in src/data/smart_money.py.
        sm_edgar = sm_coverage.get("edgar") if isinstance(sm_coverage, dict) else None
        sm_edgar_unverified = isinstance(sm_coverage, dict) and not (
            isinstance(sm_edgar, dict) and sm_edgar.get("verified")
        )
        # The market-wide pass read NOTHING while unread candidates were
        # outstanding. Kept apart from `sm_coverage_incomplete` on purpose:
        # that condition is TRUE on any ordinary residue and reports
        # `partial`, which is why the 2026-09-18 discovery regression sat
        # green for five sessions while external insider coverage was zero.
        sm_market_wide_blind = isinstance(sm_coverage, dict) and bool(
            sm_coverage.get("market_wide_blind")
        )
        sm_coverage_incomplete = isinstance(sm_coverage, dict) and (
            not sm_coverage.get("known")
            or bool(sm_coverage.get("unread"))
            or sm_edgar_unverified
        )
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
                    # Watched-name read-through as of the pre-market pass:
                    # {known, as_of, watched, read_through, unread[symbols]}.
                    "coverage": sm_coverage,
                    # Congressional disclosures lag the trade by weeks
                    # (median 60 days measured 2026-09-19); this is how old the
                    # newest one is, so nothing reads it as current news.
                    "congressional": sm_congressional,
                }, sort_keys=True, default=str),
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
            # Partial coverage: an answer arrived and was read on this tick,
            # but it cannot speak for watched names whose filings are not
            # all read. `partial` is REPORTED + fresh + counted as degraded
            # (src/evidence_gate.py) — honest on all three.
            if (
                sm_coverage_incomplete
                and data_status.get("smart_money") in ("ok", "empty")
            ):
                data_status["smart_money"] = "partial"
                logger.warning(
                    "Smart-money seat is partial: %s of %s watched names read "
                    "through (unread: %s); EDGAR coverage verified=%s "
                    "ratio=%s reasons=%s",
                    sm_coverage.get("read_through"), sm_coverage.get("watched"),
                    ", ".join(sm_coverage.get("unread") or []) or "coverage never recorded",
                    bool(isinstance(sm_edgar, dict) and sm_edgar.get("verified")),
                    (sm_edgar or {}).get("ratio") if isinstance(sm_edgar, dict) else None,
                    ", ".join(
                        str(r) for r in ((sm_edgar or {}).get("reasons") or [])
                    ) if isinstance(sm_edgar, dict) else "no record",
                )
            # The market-wide pass read nothing at all. Wins over `partial`
            # and over `ok`/`empty`, and is deliberately a DISTINCT word:
            # `partial` is what the seat says on a normal day with a normal
            # residue, so the one state that means "the desk is blind to
            # every insider outside its own book" has to be sayable on its
            # own. Classified REPORTED + fresh, and NOT in
            # INTEGRITY_CLEAN_STATUSES, so it pages through the standing
            # DATA QUALITY ALERT (src/notifier.py::maybe_alert_data_quality)
            # exactly as any other degraded seat does — no new channel.
            # It does not override a LOST state (`provider_error`,
            # `truncated`, `degraded`): those are worse and already page.
            if (
                sm_market_wide_blind
                and data_status.get("smart_money") in ("ok", "empty", "partial")
            ):
                data_status["smart_money"] = "market_wide_blind"
                logger.error(
                    "Smart-money seat read ZERO market-wide Form 4 filings "
                    "with %s unread candidate(s) outstanding — insider "
                    "coverage outside the desk's own %s watched name(s) is "
                    "blind this session",
                    sm_coverage.get("market_wide_pending"),
                    sm_coverage.get("watched"),
                )
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
                    "Macro analysis: regime=%s, outlook=%s, confidence=%s",
                    macro_analysis.regime, macro_analysis.equity_outlook,
                    macro_analysis.confidence,
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
            elif getattr(macro_coverage, "overdue", None):
                # Every configured series answered, but at least one of them
                # is sitting on a reading that should already have been
                # superseded — a publication or fetch failure, NOT normal
                # release timing (the freshness test is derived per series
                # from its own cadence and publication lag; see
                # src/data/macro.py::SeriesFreshness). This is the real
                # staleness that survived the removal of the old
                # calendar-day gate, so it must stay visible: "ok" here
                # would be the same "no data, but everything's fine!" gap
                # every other seat's audit closed. A new VALUE on the
                # existing `macro` key, not a new key — so the
                # ">= 2 degraded sources" advisory arithmetic below is
                # untouched.
                data_status["macro"] = "release_overdue"
                logger.error(
                    "Macro prints OVERDUE this run: %s", macro_coverage.describe(),
                )
            else:
                data_status["macro"] = "ok"
            # Self-reported confidence is a second, independent signal from
            # the coverage check above: coverage measures whether FRED
            # actually returned data, confidence is the model's own read on
            # how well it could make sense of what it got. A run can have
            # full coverage and still carry confidence="low" (contradictory
            # or ambiguous indicators) — that combination was reaching "ok"
            # with nothing anywhere to show for it, the same "no data, but
            # everything's fine!" gap flagged for every other seat tonight.
            # Deliberately separate from (and does not touch) the
            # release_overdue check above, which is a deterministic
            # publication fact rather than the model's own read. Only fires on what
            # would otherwise be "ok" — coverage-driven partial/failed/
            # parse_error already say something is wrong and take priority.
            if data_status["macro"] == "ok" and macro_analysis and macro_analysis.confidence == "low":
                data_status["macro"] = "low_confidence"
                logger.warning(
                    "Macro parsed cleanly on sufficient coverage but the "
                    "model self-reported confidence='low' — flagging "
                    "data_status['macro']='low_confidence' instead of 'ok'.",
                )
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
            # PM TEST GATE item 4, second half (2026-09-14). A structural
            # loss — the seat had real headline coverage for a symbol and
            # its answer for that symbol is missing (see
            # `NewsAnalystAgent._find_dropped_news_symbols`) — is worse than
            # a self-reported low confidence and is checked first: it is a
            # confirmed loss, not the model's own honesty signal about
            # thin/ambiguous input. Only fires on what would otherwise be
            # "ok" — coverage-driven partial/failed/parse_error above
            # already say something is wrong and take priority.
            if data_status["news"] == "ok" and news_intel and news_intel.dropped_news_symbols:
                data_status["news"] = "symbol_dropped"
                logger.error(
                    "News parsed cleanly on sufficient coverage but the "
                    "seat's own answer is missing %d symbol(s) it was shown "
                    "real headline coverage for — flagging "
                    "data_status['news']='symbol_dropped' instead of 'ok': %s",
                    len(news_intel.dropped_news_symbols),
                    news_intel.dropped_news_symbols,
                )
            # Self-reported confidence is a second, independent signal from
            # the coverage check above: coverage measures whether the wire
            # feeds returned data, confidence is the model's own read on
            # how well it could make sense of what it got. A report can
            # structurally parse clean on feeds that DID return data and
            # still carry confidence="low" (thin/contradictory/ambiguous
            # headlines) — that combination was reaching "ok" with nothing
            # anywhere to show for it, which is exactly the "no data, but
            # everything's fine!" lie the owner flagged. Mirrors how a
            # truncated smart-money response always wins over "ok"/"empty"
            # above: the model's own honesty signal overrides an otherwise-
            # clean status rather than being silently absorbed by it. Only
            # fires on what would otherwise be "ok" — coverage-driven
            # partial/failed/parse_error already say something is wrong and
            # take priority.
            if data_status["news"] == "ok" and news_intel and news_intel.confidence == "low":
                data_status["news"] = "low_confidence"
                logger.warning(
                    "News parsed cleanly on sufficient coverage but the "
                    "model self-reported confidence='low' — flagging "
                    "data_status['news']='low_confidence' instead of 'ok'.",
                )
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
                # Every submitted symbol resolved, but "resolved" only means
                # the model returned parseable content — it says nothing
                # about whether the model itself trusted that content. A
                # batch can be 100% parsed and still carry a self-reported
                # `conviction="low"` on one or more reads (thin history,
                # ambiguous setup, stale indicators the model flagged on its
                # own). Silently reporting "ok" in that case is the same
                # failure mode this whole data_status design exists to
                # close for earnings/news/macro — a seat claiming "ok" while
                # its own confidence says otherwise. This does not touch
                # ranking/sizing (conviction_ledger.py's "confidence is
                # reported, not applied" stands, 2026-08-31 owner decision);
                # it only makes the distinction visible where data_status
                # already is.
                low_conviction = [a.symbol for a in analyses if a.conviction == "low"]
                if low_conviction:
                    data_status["tech"] = "low_confidence"
                    logger.warning(
                        "Tech batch fully resolved but %d/%d read(s) carry the "
                        "model's own low conviction: %s",
                        len(low_conviction), len(analyses), ", ".join(low_conviction),
                    )
                else:
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
        #
        # 2026-09-04 fix: before this, `data_status["earnings"]` was "ok"
        # purely on whether the future returned without raising — see
        # `_classify_earnings_status`'s docstring above for the real
        # incident this closes and why content, not exception-vs-not, is
        # authoritative now.
        earnings_results = []
        try:
            _, earnings_results = earnings_future.result()
            data_status["earnings"] = _classify_earnings_status(earnings_results)
            if data_status["earnings"] == "content_missing":
                logger.error(
                    "Earnings: every analyzed filing this run came back with "
                    "no real figures and/or a self-reported data problem — "
                    "treating as a content failure, not 'ok'."
                )
            elif data_status["earnings"] == "partial":
                logger.warning(
                    "Earnings: at least one analyzed filing this run came "
                    "back with no real figures and/or a self-reported data "
                    "problem alongside others that were clean."
                )
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
        # Tech's `low_confidence` is set (see the tech branch above) whenever
        # ANY resolved read carries the model's own conviction="low" — and a
        # `neutral` rating (no view at all) is effectively always
        # low-conviction, because there is nothing for the model to be
        # confident ABOUT. Before this fix that made this line fire ERROR
        # every single morning regardless of whether a single actionable
        # BUY/SELL call was ever shaky, and log-health then reported a
        # perfectly healthy tech seat as "a research desk that could not be
        # reached". `data_status["tech"]` itself, and everything that reads
        # it (RiskStage's `data_degraded` advisory, the Risk Manager's
        # prompt), is UNCHANGED by this — only this operator-facing summary
        # line is corrected to name what actually degraded: a low-conviction
        # read on a symbol the desk was actually weighing, not a no-view
        # neutral read the model was never going to act on either way.
        tech_low_confidence_is_noise = (
            data_status.get("tech") == "low_confidence"
            and not any(
                a.conviction == "low" and a.rating != "neutral" for a in analyses
            )
        )
        degraded = [
            k for k, v in data_status.items()
            if evidence_gate.counts_as_degraded(v)
            and not (k == "tech" and tech_low_confidence_is_noise)
        ]
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
            # Same live-price rule as the main morning Tech pass (2026-09-14).
            intraday_context=self._live_context([s["symbol"] for s in symbols_data]),
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
        # Item 54 (2026-09-12): the preview above also RECORDS, by code, the
        # names the one shared funnel refused (`last_refusals` — stop wider
        # than the instrument's reach, or too young to measure). A
        # snapshot, not a drain: DecisionStage drains once per session
        # after construction, so the same refusal is filed exactly once.
        constructor_refusals_by_symbol = {
            str(sym).upper(): dict(refusal)
            for sym, refusal in dict(getattr(
                getattr(pipeline, "portfolio_constructor", None),
                "last_refusals", {},
            ) or {}).items()
        }

        # Margin capacity for the PM prompt — WORDING ONLY. Reuses the
        # EXACT §11.2 computation the execution submit loop uses to size
        # entries (`_entry_deployment_budget`, which itself resolves the
        # ladder via `_session_gross_ceiling`), so the prompt cannot state a
        # different number than execution sizes against. Book state here
        # (positions/equity/held-gross) has not changed since ctx was built
        # above, so this is the same headroom execution will see for this
        # session's opening entries — never a new formula.
        margin_headroom_usd, margin_ladder_backed, _margin_headroom_note = (
            _entry_deployment_budget(pipeline, ctx, positions, total_value, cash)
        )
        _margin_ceiling = _session_gross_ceiling(pipeline, ctx)
        margin_ladder_multiple = (
            _margin_ceiling.ceiling_x if _margin_ceiling is not None else None
        )
        margin_ladder_rung = (
            _margin_ceiling.rung if _margin_ceiling is not None else None
        )

        # Kept as a dict so the ONE accounting re-ask below (board item
        # 110) can re-ask the identical question — same inputs, same
        # prompt — with only the bookkeeping challenge added. A re-ask
        # built from different inputs would be a second decision, not a
        # re-ask.
        pm_decide_kwargs = dict(
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
            margin_headroom_usd=margin_headroom_usd,
            margin_ladder_backed=margin_ladder_backed,
            # 2026-09-23: the §10.3 notional floor, read by exactly the
            # helper the execution-time re-size and the rotation buy-leg
            # projection already read it with, so the rotation pre-check
            # tests "can this book fund the smallest order the desk will
            # place" against the DEPLOYED floor rather than a second copy.
            min_order_usd=_min_order_usd(pipeline),
            margin_ladder_multiple=margin_ladder_multiple,
            margin_ladder_rung=margin_ladder_rung,
            symbol_sectors=dict(getattr(pipeline, "_last_symbol_sectors", {})),
            session_type=ctx.session,
            allowed_buy_symbols={
                str(symbol).strip().upper()
                for symbol in configured_universe
                if str(symbol).strip()
            } | set(ctx.admitted_symbols),
            transient_admitted_symbols=set(ctx.admitted_symbols),
            # The unmeasurable-payoff gate reads the SAME starter size the
            # risk budget will actually grant. `rr_floor` is retired as a
            # size/refuse threshold (owner 2026-09-17) and is still threaded
            # so existing callers/tests do not silently re-default a number
            # that must not decide size.
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
            # Phase 14b: wording only — see `_apply_rotation_execution`.
            rotation_execute_enabled=_rotation_execution_enabled(pipeline),
            # Board item 39: the ranked-margin tier is executable behind its
            # own second switch, and the PM's prompt has to say so or the
            # model sizes its plan as though no room is being freed.
            rotation_ranked_margin_enabled=_rotation_ranked_margin_enabled(
                pipeline,
            ),
            real_reward_risk_by_symbol=real_reward_risk_by_symbol,
            constructor_refusals_by_symbol=constructor_refusals_by_symbol,
        )
        portfolio_decision, pm_result = pipeline.portfolio_manager.decide(
            **pm_decide_kwargs,
        )
        # Board item 164: read NOW — the candidate-accounting re-ask below
        # calls decide() again, which resets this list.
        _pm_dropped = getattr(pipeline.portfolio_manager, "last_dropped_targets", None)
        pm_dropped_targets = list(_pm_dropped) if isinstance(_pm_dropped, list) else []
        from src.agents.portfolio_manager import PortfolioManagerAgent
        macro_failures = list(
            getattr(PortfolioManagerAgent, "_macro_parse_failures", None) or []
        )
        instance_failures = getattr(
            pipeline.portfolio_manager, "_macro_parse_failures", None,
        )
        if instance_failures and instance_failures is not macro_failures:
            for reason in list(instance_failures):
                if reason not in macro_failures:
                    macro_failures.append(reason)
        for reason in macro_failures:
            _record_pipeline_event(
                pipeline, ctx, None, "macro_parse", "failed", reason=reason,
            )
        PortfolioManagerAgent._macro_parse_failures = []
        try:
            pipeline.portfolio_manager._macro_parse_failures = []
        except Exception:
            pass

        if portfolio_decision and portfolio_decision.reasoning_chain:
            rc = portfolio_decision.reasoning_chain
            # All TEN fields. This line logged seven, and the ones it
            # omitted were exactly the ones the schema lets default to "" —
            # so the operator-facing log could not distinguish "PM
            # red-teamed its book" from "PM skipped the step" (2026-08-13
            # agent audit). `macro_audit` is the third such field
            # (2026-09-14, item 18e) and is here for the same reason: a
            # field that validates when empty is invisible in a log that
            # does not print it.
            logger.info(
                "PM Reasoning Chain:\n  Macro: %s\n  News: %s\n  Earnings: %s\n  "
                "Conflicts: %s\n  Sizing: %s\n  Balance: %s\n  Cash: %s\n  "
                "Continuity: %s\n  Pre-mortem: %s\n  Macro audit: %s",
                rc.macro_filter[:120], rc.news_check[:120], rc.earnings_check[:120],
                rc.signal_conflicts[:120], rc.sizing_logic[:120],
                rc.portfolio_balance[:120], rc.cash_target[:120],
                rc.continuity_check[:120] or "[MISSING]",
                rc.premortem_check[:120] or "[MISSING]",
                rc.macro_audit[:120] or "[MISSING]",
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
        # Board item 164: a target the seat proposed and code then removed
        # (malformed, or an unadjudicated seat conflict) is a decision the
        # desk made about that symbol; it is persisted per symbol with the
        # gate and the reason, not only logged. Written whether or not the
        # PM call as a whole produced a usable decision.
        for dropped in pm_dropped_targets:
            _details = {
                k: v for k, v in dropped.items() if k not in ("symbol", "reason")
            }
            _record_pipeline_event(
                pipeline, ctx, dropped.get("symbol"), "portfolio_manager",
                "target_dropped", dropped.get("reason", ""), **_details,
            )

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
        _account_for_pm_candidates(
            pipeline, ctx, run_id=run_id, analyses=analyses,
            positions=positions, decision=portfolio_decision,
            pm_decide_kwargs=pm_decide_kwargs,
        )

        price_map = {p.symbol: p.current_price for p in positions}
        # A new name (one not already held) needs a live price to SIZE its
        # BUY: the constructor and ExecutionStage both do
        # `qty = total_value * alloc/100 / price`, so the price is the
        # divisor of the dollar allocation and a wrong price mis-sizes the
        # position PROPORTIONALLY. The bare broker call used here previously
        # (`get_latest_price`) returns whatever `get_latest_price_stamped`
        # finds FIRST — a real trade print if there is one, but otherwise a
        # QUOTE MIDPOINT or a prior-session last trade, unlabelled — so a
        # stale or mid price silently set the share count (docs/WORK.md item
        # 120).
        #
        # Route each new name through the desk's one freshness resolver
        # instead. `resolve_live_price` turns a `get_intraday_snapshots`
        # payload into a today price that is a real print OR today's forming
        # session bar — never a quote mid (the module has no branch that
        # reads a quote) — subject to the date-equality + 09:30-open
        # freshness rule, or an explicit refusal. A fresh price sizes the
        # buy; a name with NO usable today price this session is REFUSED as
        # unmeasurable (`unpriceable_new_syms` below, routed into the
        # constructor's existing data-fault / unmeasurable drop path) rather
        # than sized on a bad price. No fallback price is invented.
        #
        # IEX FREE-FEED CAVEAT: the price comes from the single free feed the
        # account defaults to (`get_intraday_snapshots` pins no `feed`), so
        # for a very thin name a fresh today price can simply be absent. That
        # name is then correctly refused — the safe, intended outcome, not a
        # regression.
        new_syms = list(dict.fromkeys(
            t.symbol.strip().upper()
            for t in portfolio_decision.targets
            if t.symbol.strip().upper() not in price_map
        ))
        unpriceable_new_syms: dict[str, str] = {}
        if new_syms:
            try:
                snapshots = pipeline.broker.get_intraday_snapshots(new_syms)
            except Exception as e:
                # get_intraday_snapshots is documented never to raise; guard
                # anyway so a broker fault fails CLOSED (every new name
                # refused) rather than reaching a bad-price fallback.
                logger.warning("Constructor snapshot lookup failed: %s", e)
                snapshots = {}
            for sym in new_syms:
                resolved = resolve_live_price(snapshots.get(sym))
                if resolved.is_today_print:
                    price_map[sym] = resolved.price
                else:
                    # Distinguish "only a stale prior-session price" from
                    # "no usable price at all" so the census counts them
                    # apart — the same split the resolver already draws.
                    unpriceable_new_syms[sym] = (
                        FAULT_STALE_PRICE if resolved.unavailable == ONLY_STALE
                        else FAULT_NO_PRICE
                    )
                    logger.warning(
                        "Constructor: no fresh today price for new name %s "
                        "(%s) — refusing as unmeasurable, not sizing the buy "
                        "on a stale or mid price", sym, resolved.describe(),
                    )
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
        # agreement refusal — never invented from PM's own provenance,
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
        # Phase 14b — automatic opportunity-cost rotation. Behind
        # `execution.rotation_enabled` (default OFF: a no-op here). Appends
        # ONE zero-size target for a categorically-ineligible, already-
        # unprotected holding so the constructor, RiskStage and
        # ExecutionStage below treat it exactly like a PM-authored close.
        # Sits BEFORE the constructor on purpose: the freed risk must be
        # visible to `allocate_risk_budget` when it rations the new
        # candidate's BUY, and the close must pass every gate downstream.
        # Unconditional, and BEFORE the acting path: the owner's report has
        # to be able to say the comparison was made even in the (commonest)
        # session where it surfaced nothing and the acting path returns
        # silently. See `_record_rotation_precheck`.
        _record_rotation_precheck(pipeline, ctx)
        _apply_rotation_execution(
            pipeline, ctx, portfolio_decision, positions, position_history,
        )
        _record_seat_stances(
            pipeline, ctx, evidence_registry,
            [t.symbol for t in portfolio_decision.targets],
        )
        book_targets, refused_soft_exit = _targets_admitted_to_book(
            portfolio_decision.targets,
            positions=positions,
            total_value=total_value,
            existing_risk_pct=existing_risk_pct,
        )
        for symbol in refused_soft_exit:
            _record_soft_exit_missing_after_retry(pipeline, ctx, symbol)
        if refused_soft_exit:
            logger.warning(
                "Refusing %d open target(s) %s before the ticket book: %s",
                len(refused_soft_exit), SOFT_EXIT_MISSING_AFTER_RETRY,
                refused_soft_exit,
            )
            existing = list(
                getattr(portfolio_decision, "constructor_dropped", None) or []
            )
            for symbol in refused_soft_exit:
                if symbol not in existing:
                    existing.append(symbol)
            portfolio_decision.constructor_dropped = existing
        portfolio_decision.decisions = pipeline.portfolio_constructor.construct_orders(
            targets=book_targets,
            positions=positions,
            analyses=analyses,
            total_value=total_value,
            price_map=price_map,
            # New names with no fresh today print this session (item 120):
            # the constructor refuses each as a DATA FAULT rather than
            # sizing it off a stale/mid price or the TA entry fallback.
            unpriceable_symbols=unpriceable_new_syms,
            existing_risk_pct=existing_risk_pct,
            clusters=risk_clusters,
            # Live broker stops from the same heat roll-up as
            # `existing_risk_pct`: sizes a trim of a held, unanalysed name.
            live_stops=_live_stops_from_heat(ctx),
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
            # retired board item 49, owner decision 2026-09-12 (`docs/INCIDENT_HISTORY.md`, 2026-09-14): when the risk
            # budget binds, spend it BEST-RANKED FIRST. This is the PM's own
            # `rank_verdicts` ordering, taken from the object the PM's prompt
            # was rendered from this session — never recomputed here, so the
            # order the budget is spent in is provably the order the model
            # was shown. None when no ranking was produced (no Technical
            # reads, or a PM path that never built a prompt): the allocator
            # then keeps its pre-decision ordering rather than being handed
            # an invented one.
            ranking=_session_candidate_ranking(pipeline),
        )
        # Provenance for the AI Risk Manager: which proposed symbols did the
        # deterministic constructor remove? Derived here (targets minus
        # decisions) rather than by changing construct_orders' signature.
        # HOLD decisions still count as "kept" — the symbol survived review.
        portfolio_decision.constructor_dropped = _dropped_since_proposal(
            portfolio_decision
        )
        if portfolio_decision.constructor_dropped:
            logger.info(
                "Constructor dropped %s — recorded for the Risk Manager so "
                "PM's narrative mentioning them does not read as incoherence.",
                ", ".join(portfolio_decision.constructor_dropped),
            )
        # Every drop gets a terminal, real-reason evidence row — and a DATA
        # fault (a symbol the constructor could not MEASURE) is filed under
        # its own name and paged, never counted as a trade the desk judged.
        # See `_record_constructor_drops` / `_alert_unmeasurable_symbols`.
        data_faults = _record_constructor_drops(pipeline, ctx, portfolio_decision)
        if data_faults:
            _alert_unmeasurable_symbols(data_faults)
        _record_constructor_side_flips(pipeline, ctx)
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


def _record_earnings_cap(pipeline, ctx, before: list, after: list) -> None:
    """One durable per-symbol row for every BUY the queued-earnings cap
    dropped or cut (`TradingPipeline._clamp_queued_earnings_buys`).

    Board item 164 (2026-09-19). The cap is a BUY-only gate that either
    removes a decision or replaces it with a smaller copy, so both outcomes
    are read by comparing the list it was handed with the list it returned:
    a BUY absent afterwards was DROPPED, one whose `allocation_pct` fell was
    CUT. The size is stated before and after because the symbol's
    `proposed_order` row, written earlier by DecisionStage, still carries
    the pre-cap number. Never raises — a record failure must not stop the
    stage (`_persist_evidence`'s contract).
    """
    try:
        after_by_symbol = {
            d.symbol.strip().upper(): d
            for d in (after or []) if d is not None and d.action == "BUY"
        }
        for d in before or []:
            if d is None or d.action != "BUY":
                continue
            sym = d.symbol.strip().upper()
            kept = after_by_symbol.get(sym)
            if kept is None:
                _record_pipeline_event(
                    pipeline, ctx, d.symbol, "deterministic_gate", "blocked",
                    "queued_earnings_cap", gate="queued_earnings_cap",
                    before_allocation_pct=d.allocation_pct,
                    after_allocation_pct=0.0,
                    detail=(
                        f"BUY {d.symbol} DROPPED: a just-filed earnings "
                        f"report is queued and not yet read, and the name is "
                        f"already at or over the queued-earnings weight cap, "
                        f"so there is no room to add. The proposed order "
                        f"asked for {d.allocation_pct:.2f}%."
                    ),
                )
            elif kept.allocation_pct < d.allocation_pct:
                _record_pipeline_event(
                    pipeline, ctx, d.symbol, "deterministic_gate", "modified",
                    "queued_earnings_cap", gate="queued_earnings_cap",
                    before_allocation_pct=d.allocation_pct,
                    after_allocation_pct=kept.allocation_pct,
                    detail=(
                        f"BUY {d.symbol} CUT from {d.allocation_pct:.2f}% to "
                        f"{kept.allocation_pct:.2f}%: a just-filed earnings "
                        f"report is queued and not yet read, so the resulting "
                        f"position is held to the queued-earnings weight cap."
                    ),
                )
    except Exception as exc:  # noqa: BLE001
        logger.warning("queued-earnings cap recording failed: %s", exc)


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


#: The key `AnalysisParseTelemetry.record_dropped_item` records when the
#: malformed row's own symbol could not be read out of it — see
#: `src/agents/tech_analyst.py`, which passes `"?"` for a row whose `key` is
#: not in the submitted set and for a dict with no readable `symbol`.
UNIDENTIFIED_DROP_KEY = "?"


def _reconcile_parse_loss(
    dropped: dict[tuple[str, str], int],
    book_symbols: set[str],
) -> tuple[dict[str, int], dict[str, int]]:
    """Split recorded parse drops into RECOVERED and GENUINELY LOST.

    WHY THIS EXISTS. `parse_telemetry` records a drop the moment a row fails
    to parse and NOTHING un-records it when the retry succeeds — deliberately,
    because a recovered drop is otherwise invisible to the operator while
    still costing a paid LLM round-trip (the reasoning is written out at
    `src.models.AnalysisParseTelemetry.record_dropped_item` and at both record
    sites in `src/agents/tech_analyst.py`). The counter is therefore right and
    stays exactly as it is. What was WRONG was the sentence built from it: the
    advisory told the Risk Manager every recorded drop was "absent from the
    book below".

    Measured, 2026-09-21 `intra_check`: META was held with stops at 14:15:45,
    its row dropped at 14:16:35, META was re-analysed and re-sized by the
    constructor at 14:17:31, and at 14:22:05 the risk stage still reported it
    as discarded and absent. META was in the book. The Risk Manager then
    called the environment degraded on a data-quality red flag that had
    already repaired itself. The falsifiable clause is "absent from the book
    below", not the count, and that clause is what licensed the downgrade.

    So the reconciliation happens HERE, in the risk stage, and nowhere else:
    this is the only place that holds the book the seat is actually shown.
    The tech seat's own `analyses` dict proves a row PARSED; it does not prove
    the Portfolio Manager ranked the name or that the constructor kept it.

    `book_symbols` is the caller's set of symbols visible to the risk seat,
    upper-cased. Anything recorded under `UNIDENTIFIED_DROP_KEY` is counted as
    genuinely lost whatever the book contains: a row whose symbol could not be
    read cannot be matched against anything, and one such entry may aggregate
    several separate malformed rows, so it can never be PROVEN recovered.
    Treating it as lost is the conservative direction — it keeps the advisory
    over-reporting loss rather than under-reporting it.

    READ-ONLY with respect to the telemetry. This function takes a snapshot
    dict, mutates nothing global, and un-records nothing; the tech seat runs
    twice per morning run and research runs five seats in one
    `ThreadPoolExecutor`, all against the single global counter, so a writer
    here would be a new cross-thread hazard. There is none.

    Returns `(recovered, lost)`, each an ordered `{"Model:KEY": count}` map in
    the same display shape `describe_dropped` uses.
    """
    recovered: dict[str, int] = {}
    lost: dict[str, int] = {}
    for (model, key), count in sorted(dropped.items()):
        name = f"{model}:{key}"
        if key != UNIDENTIFIED_DROP_KEY and str(key).upper() in book_symbols:
            recovered[name] = count
        else:
            lost[name] = count
    return recovered, lost


def _parse_loss_advisories(
    dropped: dict[tuple[str, str], int],
    book_symbols: set[str],
) -> list:
    """The parse-loss advisories for this session, reconciled against the book.

    Two entries at most, and they say different things because they ARE
    different things:

      * `analysis_parse_loss` — dropped and still not in the book. Unchanged
        wording, unchanged severity, still the falsifiable "absent from the
        book below" clause, because for these it is true.
      * `analysis_parse_loss_recovered` — dropped, re-asked, and present in
        the book the seat is reading. A cost-and-quality note, NOT a claim of
        missing coverage, and it never says the symbol is absent.

    Both are raised, so the operator keeps the cost signal the counter exists
    to give and the seat is told the truth rather than told less.
    """
    from src.risk.rules import RiskViolation as _RV

    recovered, lost = _reconcile_parse_loss(dropped, book_symbols)
    out: list = []
    if lost:
        n_lost = sum(lost.values())
        out.append(_RV(
            rule="analysis_parse_loss",
            message=(
                f"{n_lost} item(s) were discarded at parse this session "
                f"and are absent from the book below: {', '.join(lost)} "
                f"(TechAnalysisResult = a candidate PM never saw; "
                f"TargetPosition = a position PM asked for and the desk "
                f"could not read). The plan was therefore built from, or "
                f"reduced to, a SMALLER set than the seats produced — "
                f"treat a thin list as possibly truncated rather than as a "
                f"genuine absence of setups. An entry keyed "
                f"`{UNIDENTIFIED_DROP_KEY}` is a row whose own symbol could "
                f"not be read, so it cannot be matched against the book and "
                f"is counted here."
            ),
            value=float(n_lost),
            limit=0.0,
        ))
    if recovered:
        n_recovered = sum(recovered.values())
        out.append(_RV(
            rule="analysis_parse_loss_recovered",
            message=(
                f"{n_recovered} item(s) failed to parse and were RECOVERED by "
                f"a retry: {', '.join(recovered)}. These names ARE in the book "
                f"below — this is a cost and data-quality note, not missing "
                f"coverage, and no name is missing from the book because of "
                f"it. Do not treat it as degraded input: each one cost an "
                f"extra paid model round-trip, which is what is worth "
                f"reporting."
            ),
            value=float(n_recovered),
            limit=0.0,
        ))
    return out


class RiskStage:
    """Hard filter → earnings cap → correlation → RM review → mods → re-filter.

    Reads:  ctx.portfolio_decision, ctx.positions, ctx.total_value,
            ctx.last_equity, ctx.earnings_results, ctx.macro_analysis,
            ctx.analyses, ctx.symbols_bars, ctx.data_status, ctx.news_intel,
            ctx.macro_summary, ctx.macro_events, ctx.macro_event_coverage,
            ctx.fomc_meetings, ctx.fomc_coverage

    Writes: ctx.portfolio_decision.decisions (filtered/capped/scaled),
            ctx.correlation_matrix, ctx.daily_pnl, ctx.invested_target_pct

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
        try:
            out = self._run_review(ctx)
        except Exception:
            _stop_trade_updates(self._pipeline)
            raise
        if out is not None:
            _stop_trade_updates(self._pipeline)
        return out

    def _run_review(self, ctx: RunContext) -> dict | None:
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

        isolated = _isolate_empty_soft_exit_entries(pipeline, ctx, portfolio_decision)
        if isolated and not getattr(portfolio_decision, "decisions", None):
            logger.warning(
                "RiskStage: every BUY/SHORT was refused (%s); "
                "skipping Risk rather than vetoing an empty plan",
                SOFT_EXIT_MISSING_AFTER_RETRY,
            )
            return None

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
        before_earnings_cap = list(portfolio_decision.decisions)
        portfolio_decision.decisions = pipeline._clamp_queued_earnings_buys(
            portfolio_decision.decisions, earnings_results,
            positions=rm_positions, total_value=total_value,
        )
        # Board item 164: the cap used to reach the log only, while this
        # symbol's `proposed_order` row (written by DecisionStage, before
        # this gate) kept the pre-cap size. Recording only.
        _record_earnings_cap(
            pipeline, ctx, before_earnings_cap, portfolio_decision.decisions,
        )

        daily_pnl = total_value - last_equity
        ctx.daily_pnl = daily_pnl
        # Owner mandate 2026-09-17: fully invested, always. The advisory's
        # target is the fixed mandate; macro no longer sets or lowers it.
        from src.risk.rules import DESK_INVESTED_TARGET_PCT
        invested_target_pct = DESK_INVESTED_TARGET_PCT
        ctx.invested_target_pct = invested_target_pct

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
                    "seat sees no rolling-return context this run: %s", e,
                )
                rm_recent_performance = {}

        # Spec §11.2 — the session's gross-exposure ceiling, resolved from
        # ACCOUNT STATE and never from PM output. The run preamble already
        # acted on it before any agent was called; reading it again here is
        # what makes the execution gate below measure new orders against the
        # same rung the constructor sized them under.
        session_gross_ceiling = _session_gross_ceiling(pipeline, ctx)

        # The rolling-return drawdown halve used to run here, scaling every
        # BUY and SHORT by 0.5 whenever `in_drawdown` was true. Removed  # retired-ok
        # 2026-09-20 on the owner's instruction together with the daily-loss
        # halt (retired item 32, docs/INCIDENT_HISTORY.md). The §11.2 gross
        # ceiling resolved above is untouched and still sizes and blocks.

        # Memoized by DecisionStage so PM and this gate score the same numbers.
        # On the RC2 resume lane DecisionStage never ran and this is the first
        # build; the helper handles both cases.
        correlation_matrix = pipeline._ensure_correlation_matrix(ctx, rm_positions)

        before_hard_gate = list(portfolio_decision.decisions)
        portfolio_decision.decisions, rule_violations, blocked_reasons = (
            pipeline._filter_hard_risk_decisions(
                portfolio_decision.decisions,
                positions, total_value,
                invested_target_pct=invested_target_pct,
                correlation_matrix=correlation_matrix,
                cash=ctx.deployable_cash,
                gross_ceiling=session_gross_ceiling,)
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

        # Same-session reuse (`carried_from_morning`) and an intentional
        # skip (`not_run_intraday`) are usable, not integrity failures —
        # see evidence_gate.INTEGRITY_CLEAN_STATUSES. Interpolate ONLY the
        # degraded seats: dumping the full dict re-smuggled reuse words
        # into RM's prompt on a mixed tick (measured 2026-09-16).
        degraded = {
            k: v for k, v in data_status.items()
            if evidence_gate.counts_as_degraded(v)
        }
        if len(degraded) >= 2:
            from src.risk.rules import RiskViolation as _RV
            rule_violations.append(_RV(
                rule="data_degraded",
                message=(
                    f"Upstream data sources degraded: {', '.join(sorted(degraded))} "
                    f"(status: {degraded}). Decisions may be built on incomplete input — "
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
            # RECONCILED against the book before anything is said about it.
            # A drop whose retry succeeded leaves its counter standing for
            # ever (by design — see `_reconcile_parse_loss`), and until
            # 2026-09-23 that made the advisory assert "absent from the book
            # below" about symbols sitting in the book. Measured on
            # 2026-09-21: META, held with stops and re-sized by the
            # constructor in the same run, was reported to the Risk Manager as
            # discarded and absent, and the seat flagged the environment
            # degraded on it.
            #
            # The book is what the risk seat is actually SHOWN: the positions
            # in the prompt plus the orders proposed to it. Deliberately NOT
            # `portfolio_decision.targets` — a target the constructor dropped
            # never reaches the seat, so counting it as present would be the
            # same false reassurance in the other direction — and deliberately
            # not `analyses`, which proves only that a row parsed.
            book_symbols = {
                str(p.symbol).upper() for p in (rm_positions or [])
                if getattr(p, "symbol", None)
            } | {
                str(d.symbol).upper()
                for d in (portfolio_decision.decisions or [])
                if getattr(d, "symbol", None)
            }
            recovered_names, lost_names = _reconcile_parse_loss(
                dropped, book_symbols,
            )
            rule_violations.extend(_parse_loss_advisories(dropped, book_symbols))
            if lost_names:
                logger.error(
                    "Analysis parse loss reached the risk stage: %d item(s) "
                    "— %s", sum(lost_names.values()), ", ".join(lost_names),
                )
            if recovered_names:
                logger.warning(
                    "Analysis parse loss RECOVERED by retry before the risk "
                    "stage: %d item(s) — %s; present in the book, reported as "
                    "a cost note rather than as missing coverage",
                    sum(recovered_names.values()), ", ".join(recovered_names),
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

        # Recompute against the list the Risk Manager is about to be shown.
        # Several gates between construction and here can remove SOME orders
        # and pass the rest through; a name dropped by one of those was
        # missing from the order list AND from the drop list. See
        # `_dropped_since_proposal`.
        portfolio_decision.constructor_dropped = _dropped_since_proposal(
            portfolio_decision
        )

        # Handshake overlaps the review so auth is not serial after Risk.
        _start_trade_updates_early(pipeline, ctx)

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
            # None, not a hand-typed 25.0: absent facts, the agent resolves
            # the ceiling from `risk.max_portfolio_risk_pct` in the live
            # settings rather than from a literal copied into this call site.
            risk_ceiling_pct=(
                getattr(ctx.facts, "risk_ceiling_pct", None) if ctx.facts else None
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
            # "violations" was wrong AND owner-facing: this string is what
            # `CandidateDetailModal` shows on the dashboard, and by this point
            # `_filter_hard_risk_decisions` has already dropped every hard
            # breach, so the count can only ever be advisories (item 162).
            input_summary=(
                f"{len(portfolio_decision.decisions)} trades, "
                f"{len(rule_violations)} engine advisories"
            ),
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
            # Board item 164: a book-level veto refuses every leg for the
            # book's reason, but where the seat ALSO named this symbol with
            # its own reason, that reason is what the symbol's record
            # carries — the book reason rides beside it, not over it.
            book_veto_symbol_reasons = verdict.rejections_by_symbol()
            for decision in portfolio_decision.decisions:
                own = book_veto_symbol_reasons.get(
                    decision.symbol.strip().upper()
                )
                _record_pipeline_event(
                    pipeline, ctx, decision.symbol, "risk", "rejected",
                    own or verdict.reasoning,
                    gate="risk_manager_book_veto",
                    book_level_reason=verdict.reasoning,
                    reason_category=getattr(verdict, "reason_category", None),
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

        # Holding-discipline compliance — spec item 25 (2026-09-03, data-
        # driven replacement 2026-09-03/04). RM's own checklist
        # (config/prompts/risk_manager.md, "Holding-discipline compliance")
        # asks it to itself verify that a SELL/REDUCE/COVER on a PROTECTED
        # position names a real trigger; nothing in Python checked that the
        # trigger claimed is real. `holding_discipline_claim_check` is
        # deliberately narrow — it can only ever catch a PROVABLY FALSE claim
        # about (b) a regime flip or (c) a same-day HIGH-conviction bearish
        # state_change, never (a) thesis_invalid_if, which it does not (and
        # cannot reliably) check — see that function's module docstring.
        #
        # 2026-09-04, owner-approved escalation: a PROVEN-FALSE claim now
        # DROPS the decision, using the exact same mechanism as a Risk
        # Manager per-symbol refusal directly above (build `surviving`,
        # record a "rejected" pipeline event per dropped symbol, and if
        # nothing survives return the same terminal rejected status) — not a
        # new veto path. An UNVERIFIABLE claim keeps the old behaviour
        # exactly: logged and recorded, never dropped, never alerted. That
        # split is the owner's explicit instruction; `check.blocks` is the
        # single place it is decided.
        #
        # "Protected" no longer means "held under 5 days" (that flat window
        # had no backtest behind it and the owner rejected it as arbitrary).
        # It is now `check_structural_protection`'s data-driven answer —
        # intact unless the trade's `thesis_invalid_if` or the structural
        # level backing its stop has broken on the CLOSE of two consecutive
        # trading days (`_structural_protection_for_holding` recomputes
        # ATR/MAs/levels fresh from bars, does the cross-day confirmation
        # lookup, and persists today's read for the next trading day to
        # confirm against — all in one call), with a noise-band fallback —
        # not an automatic unprotect — when neither a stated condition nor
        # a qualifying level exists.
        if portfolio_decision.decisions:
            from src.risk.exit_guard import holding_discipline_claim_check
            hd_surviving: list = []
            hd_blocked: list[tuple[str, str]] = []
            macro_regime_today = _macro_regime(macro_analysis)
            macro_status = data_status.get("macro") if data_status else None
            try:
                hd_active_state_changes = pipeline._build_active_state_changes()
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    "RiskStage: holding-discipline state-change lookup failed "
                    "(%s) — bearish-state-change claims go unverified this run",
                    e,
                )
                hd_active_state_changes = ""
            for decision in portfolio_decision.decisions:
                if decision.action not in ("SELL", "REDUCE", "COVER"):
                    hd_surviving.append(decision)
                    continue
                symbol_u = decision.symbol.strip().upper()
                hist = rm_position_history.get(decision.symbol) or {}
                pos = next(
                    (p for p in rm_positions if p.symbol.upper() == symbol_u), None,
                )
                protection = pipeline._structural_protection_for_holding(
                    symbol=symbol_u,
                    thesis_invalid_if=hist.get("thesis_invalid_if"),
                    entry_price=hist.get("entry_price"),
                    stop_loss=hist.get("stop_loss"),
                    is_short=bool(pos and pos.qty < 0),
                    run_id=run_id,
                )
                logger.info(
                    "Holding-discipline structural protection for %s: "
                    "protected=%s basis=%s — %s",
                    symbol_u, protection.protected, protection.basis,
                    protection.detail,
                )
                check = holding_discipline_claim_check(
                    action=decision.action,
                    reason=decision.reasoning,
                    symbol=decision.symbol,
                    protected=protection.protected,
                    macro_regime_today=macro_regime_today,
                    macro_status=macro_status,
                    active_state_changes=hd_active_state_changes,
                )
                if check.blocks:
                    # PROVEN FALSE. Drop the decision exactly the way a
                    # per-symbol RM refusal above does: same "rejected"
                    # pipeline event, same surviving-list mechanism.
                    logger.warning(
                        "Holding discipline BLOCK (claim proven false): %s",
                        check.finding,
                    )
                    _record_pipeline_event(
                        pipeline, ctx, decision.symbol, "risk",
                        "rejected", check.finding,
                    )
                    _record_pipeline_event(
                        pipeline, ctx, decision.symbol, "risk",
                        "holding_discipline_claim_false", check.finding,
                    )
                    hd_blocked.append((symbol_u, check.finding or ""))
                    _alert_holding_discipline_block(
                        symbol=symbol_u,
                        action=decision.action,
                        reasons=check.reasons,
                    )
                    continue
                if check.verdict == "unverifiable":
                    # Unchanged from before the 2026-09-04 escalation:
                    # recorded for the audit trail, and that is all. No
                    # drop, no alert — absence of proof is not proof.
                    logger.warning("Holding discipline: %s", check.finding)
                    _record_pipeline_event(
                        pipeline, ctx, decision.symbol, "risk",
                        "holding_discipline_claim_unverified", check.finding,
                    )
                hd_surviving.append(decision)

            if hd_blocked:
                portfolio_decision.decisions = hd_surviving
                if not hd_surviving:
                    # Every remaining leg was blocked on a provably false
                    # justification. Same terminal status as the per-symbol
                    # refusal path above, and for the same reason: no orders,
                    # with each symbol carrying its OWN reason.
                    reasons = "; ".join(
                        finding for _sym, finding in hd_blocked
                    )
                    logger.info(
                        "Every remaining trade was blocked on a provably "
                        "false holding-discipline claim: %s", reasons,
                    )
                    return {
                        "status": "rejected", "orders": [], "reason": reasons,
                    }

        # Board item 164: what each surviving leg looked like BEFORE the
        # seat's edits and `scale_all_buys`, so the per-symbol `risk` event
        # below can say what actually changed rather than stamping one
        # constant on every symbol. Recording only.
        pre_rm_fields = _risk_edit_snapshot(portfolio_decision.decisions)

        if verdict.modifications:
            # Board item 135. `_apply_risk_modifications` guard 1b refuses an
            # `allocation_pct` edit that ENLARGES a BUY, but it tests
            # `decision.action == "BUY"` only — a SHORT was never covered,
            # although the constructor sizes it with the identical cumulative
            # arithmetic (`name_headroom_pct = (max_position_pct -
            # current_short_gross_pct) / gross_mul`, the explicit mirror of
            # the long clamp) and `_apply_scale_all_buys` already treats the
            # two sides alike because both open new risk. Snapshotted here
            # and enforced below for BOTH sides: for a BUY the inner guard
            # has already reverted the edit, so this sweep is a no-op and
            # finds nothing; for a SHORT it is the only thing standing
            # between the seat and a short it believes it is cutting.
            pre_mod_entry_alloc = {
                (d.symbol.strip().upper(), d.action): d.allocation_pct
                for d in portfolio_decision.decisions
                if d.action in ("BUY", "SHORT")
            }
            unapplied_mods: list[dict] = []
            portfolio_decision.decisions, rejected_mods = pipeline._apply_risk_modifications(
                portfolio_decision.decisions, verdict.modifications,
                symbols_bars=getattr(ctx, "symbols_bars", None),
                unapplied=unapplied_mods,
            )
            portfolio_decision.decisions, enlarged = _revert_entry_size_increases(
                portfolio_decision.decisions, pre_mod_entry_alloc,
            )
            rejected_mods = list(rejected_mods) + enlarged
            # A modification this method refused (exit silently zeroed, or a
            # stop/target edit that would have shipped a reward:risk / noise-
            # band floor breach) must be a visible, distinguishable event —
            # not an edit that just vanishes. See `_apply_risk_modifications`
            # docstring, guards 1 and 2 (2026-09-03 audit).
            # Board item 164: the dropped / not-applied outcomes
            # `_apply_risk_modifications` used to log and nothing else. Each
            # entry already carries its own outcome, gate and reason.
            #
            # An edit naming a symbol with no decision in this stage's plan
            # is filed RUN-scoped with the symbol in the payload: a
            # symbol-scoped row would make `src/refusal_signature.py` count
            # a name the run never considered as a candidate, or overwrite
            # the real refusal of a name the seat already refused above.
            in_plan = {sym for sym, _action in pre_rm_fields}
            for rejected in list(rejected_mods) + unapplied_mods:
                _details = {
                    k: v for k, v in rejected.items()
                    if k not in ("symbol", "reason", "outcome")
                }
                _sym = rejected["symbol"]
                if str(_sym or "").strip().upper() not in in_plan:
                    _details["symbol_named"] = _sym
                    _sym = None
                _record_pipeline_event(
                    pipeline, ctx, _sym, "risk",
                    rejected.get("outcome", "modification_rejected"),
                    rejected["reason"], **_details,
                )

        portfolio_decision.decisions, scale, scale_dropped = _apply_scale_all_buys(
            portfolio_decision.decisions, verdict,
        )
        # Board item 136 — a scale-driven drop is a real refusal of a real
        # trade and must leave the same kind of trace an RM refusal does.
        # It cannot use the loop at the bottom of this method: the decision
        # is no longer in the list by then.
        for _sym, _pre_alloc in scale_dropped:
            _record_pipeline_event(
                pipeline, ctx, _sym, "risk", "scaled_out",
                f"scale_all_buys={scale:.2f} reduced {_sym}'s entry "
                f"allocation_pct from {_pre_alloc:.2f} to 0 — the order is "
                f"DROPPED, not zeroed (a zero allocation reads as SKIP at "
                f"execution). RM reason category: "
                f"{getattr(verdict, 'reason_category', None)!r}",
                field="allocation_pct",
            )

        if verdict.modifications or scale < 1.0 or refused_decisions:
            portfolio_decision.decisions, post_mod_violations, blocked_reasons = (
                pipeline._filter_hard_risk_decisions(
                    portfolio_decision.decisions,
                    positions, total_value,
                    invested_target_pct=invested_target_pct,
                    correlation_matrix=correlation_matrix,
                    cash=ctx.deployable_cash,
                    gross_ceiling=session_gross_ceiling,)
            )
            _apply_sector_unresolved_alert(data_status, post_mod_violations)
            if blocked_reasons:
                reasons = "; ".join(dict.fromkeys(blocked_reasons))
                logger.warning("HARD RISK BLOCK AFTER MODIFICATIONS: %s", reasons)
                if not portfolio_decision.decisions:
                    pipeline._persist_hard_risk_block(ctx, reasons, stage="post_rm_modifications")
                    return {"status": "hard_risk_block", "orders": [], "reason": reasons}

        for decision in portfolio_decision.decisions:
            outcome, reason, details = _risk_event_for(
                decision, pre_rm_fields, verdict, scale,
                field_aliases=getattr(pipeline, "_FIELD_ALIASES", None),
            )
            _record_pipeline_event(
                pipeline, ctx, decision.symbol, "risk", outcome, reason,
                **details,
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
        try:
            return self._run_session(ctx)
        finally:
            _stop_trade_updates(self._pipeline)

    def _run_session(self, ctx: RunContext) -> list[dict]:
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

        # Board item 39 — the RANKED-MARGIN rotation's close goes LAST
        # among this session's exits, so that when its paired BUY is
        # checked (in `_rotation_sell_gate`, immediately before the close
        # is submitted) every other exit has a terminal status and the
        # account can simply be re-read rather than guessed at. A no-op on
        # every session without a ranked-margin rotation, which is every
        # session while `execution.rotation_ranked_margin_enabled` is off.
        sell_decisions = _rotation_sell_last(sell_decisions, ctx)

        for d in hold_decisions:
            try:
                pipeline.db.insert_trade(
                    symbol=d.symbol, action="HOLD", qty=0.0, price=0.0,
                    reasoning=d.reasoning, run_id=run_id,
                    decision_id=decision_id,
                )
            except Exception as e:
                logger.warning("Failed to record HOLD decision for %s: %s", d.symbol, e)

        # Terminal status per SELL order id, filled in by the per-name wait
        # below. Phase 14b reads it to decide whether the rotation's freed
        # room is REAL before the replacement BUY is allowed.
        sell_status_by_id: dict[str, str | None] = {}
        for decision in sell_decisions:
            prot = None
            try:
                # Board item 39. For a ranked-margin rotation close this
                # re-reads the account (measuring what the exits above
                # actually did), projects THIS sale, and runs the
                # replacement BUY through every refusal gate that is
                # knowable before the sale. `cleared` False means the buy
                # would be refused, so the close is not submitted and the
                # desk keeps the position instead of going naked. `None`
                # for every other SELL in the desk's history.
                rotation_gate = _rotation_sell_gate(
                    pipeline, ctx, decision, buy_decisions, positions,
                    total_value, cash, cover_decisions,
                )
                if rotation_gate is not None:
                    cleared, positions, total_value, cash = rotation_gate
                    # Adopt the refreshed book so this close is sized and
                    # priced off the same state the gate cleared against.
                    # `deployable_cash` is DERIVED from cash, so it is
                    # computed BEFORE any of the four is assigned: a raise
                    # part-way through would otherwise be swallowed by this
                    # loop's own handler and leave three fresh fields
                    # beside a stale derivation. All four, or none.
                    refreshed = (
                        positions, cash, total_value,
                        pipeline._compute_deployable_cash(cash, positions),
                    )
                    (ctx.positions, ctx.cash, ctx.total_value,
                     ctx.deployable_cash) = refreshed
                    if not cleared:
                        continue
                existing = [p for p in positions if p.symbol == decision.symbol]
                if not existing or existing[0].qty <= 0:
                    continue
                # Board item 39 — the unconditional barrier. A RANKED-MARGIN
                # rotation close may not reach the broker unless
                # `rotation_sell_reason` can build its reason from a real
                # `RotationClearance`, which only the projected post-sale
                # gate above mints. This reads no config: the feature flag
                # decides whether such a close is ever PROPOSED, and cannot
                # decide whether it is permitted to execute. Attempt 1 on
                # this item replaced exactly this kind of structural barrier
                # with a config boolean; it is not a config boolean again.
                rotation_final_reason = _rotation_ranked_margin_sell_reason(
                    pipeline, ctx, decision,
                )
                if rotation_final_reason is _ROTATION_SELL_REFUSED:
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
                orders.append(order)
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
                # Phase 14b — this SELL is the desk's own rotation close.
                # Record it durably and page the owner NOW: broker
                # acceptance is the irreversible act, and a position sold
                # without a human or a model deciding to must never be
                # silent (see `_alert_rotation_executed`).
                rotation = ctx.rotation
                if (
                    isinstance(rotation, dict)
                    and decision.symbol.upper() == rotation.get("held_symbol")
                ):
                    rotation["sell_order_id"] = order.get("id")
                    rotation["sell_qty"] = float(qty)
                    if isinstance(rotation_final_reason, str):
                        # A SECOND durable fact, not an edit of the first.
                        # The proposal the Risk Manager reviewed and the
                        # clearance the sale executed under are two
                        # different things that happened at two different
                        # times; overwriting one with the other leaves the
                        # ledger disagreeing with the alert about what was
                        # said when. Written once, never edited.
                        rotation["cleared_reason"] = rotation_final_reason
                        _record_pipeline_event(
                            pipeline, ctx, decision.symbol, "rotation",
                            "sell_cleared_reason", rotation_final_reason,
                            broker_order_id=order.get("id"),
                            new_symbol=rotation.get("new_symbol"),
                        )
                    _record_pipeline_event(
                        pipeline, ctx, decision.symbol, "rotation",
                        "sell_submitted", rotation.get("reason", ""),
                        broker_order_id=order.get("id"), qty=qty,
                        limit_price=sell_limit,
                        new_symbol=rotation.get("new_symbol"),
                    )
                    _alert_rotation_executed(
                        rotation=rotation, qty=float(qty),
                        limit_price=float(sell_limit), order_id=order.get("id"),
                    )
                logger.info(
                    "Executed: %s %s %s @ limit $%.2f",
                    action_label.lower(), pipeline._format_qty(qty), decision.symbol, sell_limit,
                )
            except Exception as e:
                logger.error("Order failed for %s %s: %s", decision.action, decision.symbol, e)
            if prot is None:
                continue
            # Wait for THIS sell and rebuild THIS name's stop coverage on its
            # actual fill before the loop cancels the next name's stops —
            # the per-name discipline the de-lever loops got (docs/WORK.md
            # item 111). Submitting every SELL first and waiting/finalizing
            # the batch afterwards left every earlier name with no
            # protective stop while later names were cancelled, submitted
            # and waited on. Runs even when the ledger write above raised:
            # the stops are off and the order is live. Which names are sold,
            # how much and at what limit are unchanged.
            order_id = prot["order_id"]
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
            sell_status_by_id[order_id] = status
            if status != "filled":
                logger.warning(
                    "Sell order %s did not fill before buy phase (status=%s); buys will use current cash only",
                    order_id, status or "unknown",
                )
            # The wait above returned, so the broker's fill_info is final.
            # Reprotect on actual residual (filled) or restore originals
            # (no-fill terminal). wait=False: this order was just waited on.
            pipeline._finalize_pending_protections(
                [prot], context="ExecutionStage", wait=False,
            )

        # Stage 3 (shorts): COVER loop — the exit-side twin of the SELL loop
        # just above. Reuses `_submit_protected_sell`'s side="buy" plumbing
        # (PR #135 built this for emergency covers; this is the first
        # decision-path caller). No protective stop is placed afterward —
        # covering REDUCES risk, it doesn't open any.
        for decision in cover_decisions:
            prot = None
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
                orders.append(order)
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
            if prot is None:
                continue
            # Same per-name discipline as the SELL loop above: this short's
            # BUY-stop coverage is rebuilt before the next short's is touched.
            order_id = prot["order_id"]
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
                [prot], context="ExecutionStage-Cover", wait=False,
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

        # Refresh the account before BUYs when no SELL fired, so sizing and
        # the entry-staleness guard read a current snapshot rather than the
        # research-stage one from ~10 minutes ago.
        #
        # An account-level daily-loss re-check used to run here too, dropping
        # every remaining BUY when the day's loss crossed the limit. Removed
        # 2026-09-20 on the owner's instruction with the rest of that
        # mechanism (retired item 32, docs/INCIDENT_HISTORY.md).
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

        # Phase 14b — the rotation's BUY leg may only proceed on room that
        # is REAL. The constructor granted the new candidate its risk on the
        # premise that the held name closes; if that close was refused
        # upstream (Risk Manager, hard rules, protected-sell skip) or was
        # accepted but did not fill, buying anyway would put the book over
        # the portfolio risk ceiling by the new name's risk — a side door
        # around the ceiling this feature must never open. Same
        # `_record_execution_skip` path every other deterministic BUY skip
        # uses, so the funnel and the evening review see it.
        buy_decisions = _drop_rotation_buy_if_room_not_freed(
            pipeline, ctx, buy_decisions, sell_status_by_id,
        )

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
            market_price = _live_fill_price(pipeline, decision.symbol)
            if market_price is not None:
                price_map[decision.symbol] = market_price
            if not isinstance(market_price, (int, float)) or market_price <= 0:
                _record_execution_skip(
                    pipeline, ctx, decision.symbol, "no_price",
                    "no verifiable live price (daily bar close is not a fill reference)",
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
            # docs/WORK.md item 120: the funding preflight must size off the
            # same TODAY PRINT the submit loop will, never the fill-reference
            # mid. No print -> the submit loop will refuse this name, so the
            # sweep must not sell SGOV to fund it.
            sizing_print = _today_sizing_price(pipeline, decision.symbol)
            if not isinstance(sizing_print, (int, float)) or sizing_print <= 0:
                _record_execution_skip(
                    pipeline, ctx, decision.symbol, "no_sizing_print",
                    "no today trade print to size the buy against (a quote "
                    "mid or a prior-session price is not a sizing reference) "
                    "— refused rather than sized on a bad price",
                )
                continue
            preflight_price = max(sizing_print, decision.entry_price or 0)
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
            _pin_approved_entry_ceilings(pipeline, ctx, buy_decisions)
            _warm_trade_updates(pipeline, ctx)
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
            _adopt_stream_stall(pipeline, ctx)
            # Encode AFTER funding so the fund step's own 180s/30s ceiling
            # cannot sit as leftover slack on a fast no-op. Remaining
            # programmed wait is auth only when the hub did not start.
            _encode_entry_submit_window(pipeline, ctx, will_fund=False)

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
            add_prep = None
            buy_accepted = False
            submit_attempted = False
            try:
                from src.execution.scale_in import LongAddPrep
                add_prep = LongAddPrep.not_scale_in()
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
                    # Short scale-in (adding to an existing short) is now a
                    # real path: it is routed through `prepare_short_add` at
                    # the same post-sizing / post-min-order-floor point the
                    # long add uses, below. It is NOT gated here — the
                    # min-order floor must run first so the buy-stop is never
                    # cancelled for an add that then gets dropped.

                live_price = _live_fill_price(pipeline, decision.symbol)
                if live_price is not None:
                    market_price = live_price
                    price_map[decision.symbol] = live_price
                else:
                    market_price = None
                # MUST NOT freeze a morning/last-bar close into a live fill.

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
                    # `sizing_price` is deliberately NOT set from market_price
                    # here: market_price is the FILL reference (a quote mid is
                    # legitimate for the marketable limit) and the SHARE COUNT
                    # must not divide by a mid. It is anchored to a today
                    # print just below (docs/WORK.md item 120).
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

                # docs/WORK.md item 120: SIZING vs FILL. `market_price` above
                # is the fill reference and may be a quote mid (a legitimate
                # marketable-limit reference, owner 2026-09-12); it drives the
                # limit price. The SHARE COUNT, however, divides the dollar
                # allocation by the price, so it must be a real TODAY PRINT —
                # never a quote mid, never a prior-session trade. Size off the
                # print, bounded conservatively by the already-approved entry
                # (which passed the 5% freshness check above); refuse the name
                # when no print is available rather than size on a bad price.
                sizing_print = _today_sizing_price(pipeline, decision.symbol)
                if sizing_print is None or sizing_print <= 0:
                    _record_execution_skip(
                        pipeline, ctx, decision.symbol, "no_sizing_print",
                        "no today trade print to size the order against (a "
                        "quote mid or a prior-session price is not a sizing "
                        "reference) — refused rather than sized on a bad price",
                    )
                    continue
                if decision.entry_price and decision.entry_price > 0:
                    # Size off a today print, bounded by the approved entry.
                    # The HIGHER divisor is conservative on the ALLOCATION
                    # path for BOTH directions (fewer shares: less capital on
                    # a buy, a smaller short on a short). On the RISK-BUDGET
                    # path it is conservative for a BUY only: there
                    # `risk_per_share = entry - stop` and a higher entry
                    # WIDENS it, shrinking qty_by_risk; for a SHORT
                    # (`stop - entry`) a higher entry NARROWS it and can
                    # INFLATE qty_by_risk when the analyst entry sits above
                    # the today print — item 181, fixed just below via
                    # `risk_sizing_price` (this `sizing_price` stays the
                    # allocation-path divisor, unchanged).
                    # What this line does fix: the short no longer divides by
                    # the below-market `bid_limit` (the over-size bug of
                    # item 120). Never size off a below-market number.
                    sizing_price = max(sizing_print, float(decision.entry_price))
                else:
                    sizing_price = sizing_print
                # item 181 FIX: the RISK-BUDGET divisor must never be
                # inflated. `sizing_price` above is the max(print, entry) —
                # correctly conservative for the ALLOCATION path in both
                # directions — but on the RISK-BUDGET path
                # `risk_per_share = |price - stop|`, and for a SHORT a
                # HIGHER price NARROWS that spread. When the analyst's entry
                # sits above today's print, sizing the risk budget off the
                # entry understates risk_per_share and inflates qty_by_risk
                # past the ratified budget (bounded only by the allocation
                # min() cap). The short actually fills near the print, so
                # the risk budget must be measured against the print. A BUY
                # is unaffected: there risk_per_share = price - stop GROWS
                # with a higher divisor, which is already the conservative
                # direction, so it keeps using `sizing_price` unchanged.
                risk_sizing_price = sizing_print if is_short else sizing_price

                # Liquid-equity execution policy: cross the displayed quote
                # with a limit (never a market order). A wider spread remains
                # price-protected and may expire after the bounded entry
                # window instead of paying through an abnormal book. If quote
                # data is degraded, retain the validated last/PM limit and
                # the same bounded wait.
                #
                # Fillability parity, not a new risk budget: BUY crosses the
                # displayed OFFER with a ceiling `reference * (1 + bps/1e4)`;
                # SHORT crosses the displayed BID with the same
                # `max_entry_slippage_bps` as a floor
                # `reference * (1 - bps/1e4)`. A SHORT still keeps the >5%
                # stale-entry skip and the direction-aware lower-to-market
                # adjustment just above; this block only adds the NBBO-aware
                # floor a BUY already had as a ceiling. Repeg stays off —
                # walking a short toward the buy-side ceiling would worsen
                # it, not fix an unmarketable birth price.
                try:
                    quote = pipeline.broker.get_latest_quote(decision.symbol)
                except Exception as e:  # noqa: BLE001
                    logger.warning(
                        "%s %s quote lookup failed: %s",
                        decision.action, decision.symbol, e,
                    )
                    quote = None
                ask = quote.get("ask_price") if isinstance(quote, dict) else None
                bid = quote.get("bid_price") if isinstance(quote, dict) else None
                if _submit_window_overrun(ctx):
                    ctx.desk_latency_stall = True
                    logger.warning(
                        "%s %s NOT SUBMITTED — latency blew the window "
                        "(encoded post-Risk budget %.1fs).",
                        decision.action, decision.symbol,
                        float(getattr(ctx, "entry_submit_budget_s", 0.0) or 0.0),
                    )
                    _record_execution_skip(
                        pipeline, ctx, decision.symbol, "latency_window",
                        "latency blew the window",
                    )
                    continue
                slippage_bps = _entry_slippage_bps(pipeline)
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
                    pinned_cap = (getattr(ctx, "approved_entry_ceiling", None) or {}).get(
                        decision.symbol,
                    )
                    if isinstance(pinned_cap, (int, float)) and pinned_cap > 0:
                        cap = float(pinned_cap)
                    else:
                        cap = market_price * (1 + slippage_bps / 10_000.0)
                    offer_limit = round(cap, 2 if cap >= 1 else 4)
                    ask_premium_bps = (ask - market_price) / market_price * 10_000.0

                    # The IEX ask is too unreliable to gate on directly, but a
                    # far-through reading is still information: either the
                    # market has genuinely run, or the venue is quoting
                    # nonsense. Either way this is not a book to cross blind.
                    # The multiple is deliberately loose because the input is.
                    if ask > cap * 1.02:
                        skip_reason = (
                            "latency_window"
                            if getattr(ctx, "desk_latency_stall", False)
                            else "slippage_gated"
                        )
                        logger.warning(
                            "BUY %s NOT SUBMITTED — the displayed offer has run "
                            "beyond the slippage ceiling%s. Ask $%.4f is %.1fbp "
                            "above the $%.4f reference; the %.0fbp ceiling is "
                            "$%.4f. (Quote is IEX, not NBBO, so it may also "
                            "simply be a stale venue print — either way, not a "
                            "book to cross blind.)",
                            decision.symbol,
                            " after a desk-caused stall; latency blew the window"
                            if skip_reason == "latency_window" else "",
                            ask, ask_premium_bps,
                            market_price, slippage_bps, cap,
                        )
                        _record_execution_skip(
                            pipeline, ctx, decision.symbol, skip_reason,
                            f"IEX ask ${ask:.4f} is {ask_premium_bps:.1f}bp "
                            f"above reference ${market_price:.4f}, beyond the "
                            f"{slippage_bps:.0f}bp ceiling ${cap:.4f}"
                            + (
                                " — latency blew the window"
                                if skip_reason == "latency_window" else ""
                            ),
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
                    # Safety net only: stall left the original entry unfillable
                    # but the live offer is still inside the pinned ceiling.
                    # Not the product — do not stamp this on a healthy path.
                    original_entry = getattr(decision, "entry_price", None)
                    if (
                        getattr(ctx, "desk_latency_stall", False)
                        and isinstance(original_entry, (int, float))
                        and ask > float(original_entry)
                        and ask <= cap
                    ):
                        used = dict(getattr(ctx, "catch_up_used", None) or {})
                        if not used.get(decision.symbol):
                            used[decision.symbol] = True
                            ctx.catch_up_used = used
                            _record_pipeline_event(
                                pipeline, ctx, decision.symbol, "execution",
                                "safety_net", "catch_up_inside_ceiling",
                                detail="stall left the original entry unfillable; "
                                "limit stays at the already-approved ceiling",
                            )
                elif is_short and isinstance(bid, (int, float)) and bid > 0:
                    # Mirror of the BUY ceiling: a sell-short limit is a
                    # FLOOR, not a price. Alpaca fills a short at the NBBO
                    # or better — submitting $49.95 when the bid is $50.00
                    # sells at $50.00, not at $49.95. Shaving the limit up
                    # toward the bid costs fills the same way VLO's shaved
                    # buy limit did. Set the limit AT the existing
                    # slippage floor and let the match happen underneath.
                    pinned_floor = (getattr(ctx, "approved_entry_ceiling", None) or {}).get(
                        decision.symbol,
                    )
                    if isinstance(pinned_floor, (int, float)) and pinned_floor > 0:
                        floor = float(pinned_floor)
                    else:
                        floor = market_price * (1 - slippage_bps / 10_000.0)
                    bid_limit = round(floor, 2 if floor >= 1 else 4)
                    bid_discount_bps = (
                        (market_price - bid) / market_price * 10_000.0
                    )

                    # Same IEX-noise tolerance as the BUY `cap * 1.02`
                    # skip: invert the multiple so a far-through bid
                    # (genuinely run, or a stale venue print) refuses
                    # rather than submitting an unfillable or unbound
                    # short. Not a new percentage.
                    if bid < floor / 1.02:
                        skip_reason = (
                            "latency_window"
                            if getattr(ctx, "desk_latency_stall", False)
                            else "slippage_gated"
                        )
                        logger.warning(
                            "SHORT %s NOT SUBMITTED — the displayed bid has "
                            "run beyond the slippage floor%s. Bid $%.4f is "
                            "%.1fbp below the $%.4f reference; the %.0fbp "
                            "floor is $%.4f. (Quote is IEX, not NBBO, so it "
                            "may also simply be a stale venue print — either "
                            "way, not a book to cross blind.)",
                            decision.symbol,
                            " after a desk-caused stall; latency blew the window"
                            if skip_reason == "latency_window" else "",
                            bid, bid_discount_bps,
                            market_price, slippage_bps, floor,
                        )
                        _record_execution_skip(
                            pipeline, ctx, decision.symbol, skip_reason,
                            f"IEX bid ${bid:.4f} is {bid_discount_bps:.1f}bp "
                            f"below reference ${market_price:.4f}, beyond the "
                            f"{slippage_bps:.0f}bp floor ${floor:.4f}"
                            + (
                                " — latency blew the window"
                                if skip_reason == "latency_window" else ""
                            ),
                        )
                        continue

                    if limit_price is None or abs(limit_price - bid_limit) > 0.000001:
                        logger.info(
                            "SHORT %s marketable-limit: prior $%s → floor "
                            "$%.4f (%.0fbp below reference $%.4f). Fills at "
                            "NBBO or better; IEX bid reads $%.4f (%.1fbp).",
                            decision.symbol,
                            f"{limit_price:.4f}" if limit_price is not None else "none",
                            bid_limit, slippage_bps, market_price,
                            bid, bid_discount_bps,
                        )
                    limit_price = bid_limit
                    # `bid_limit` is the LIMIT (a marketable floor BELOW
                    # market); it is deliberately NOT the sizing divisor.
                    # docs/WORK.md item 120: dividing the allocation by a
                    # below-market price OVER-sizes the short (more shares) —
                    # the dangerous direction. Sizing stays anchored to the
                    # today print set above, mirroring the BUY, which raises
                    # its divisor to the offer ceiling (fewer shares) and
                    # never lowers it.
                    original_entry = getattr(decision, "entry_price", None)
                    if (
                        getattr(ctx, "desk_latency_stall", False)
                        and isinstance(original_entry, (int, float))
                        and bid < float(original_entry)
                        and bid >= floor
                    ):
                        used = dict(getattr(ctx, "catch_up_used", None) or {})
                        if not used.get(decision.symbol):
                            used[decision.symbol] = True
                            ctx.catch_up_used = used
                            _record_pipeline_event(
                                pipeline, ctx, decision.symbol, "execution",
                                "safety_net", "catch_up_inside_ceiling",
                                detail="stall left the original entry unfillable; "
                                "limit stays at the already-approved floor",
                            )

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

                # Geometry may have moved since the Risk Manager audited
                # (ATR-widened stop, or limit raised to market). Reward:risk
                # — computed, thin, or unmeasurable — is never a skip
                # (owner 2026-09-17). The retired 1.2 belt killed RSG on
                # 2026-09-16; renaming that skip is also a defect.
                geometry_changed = (
                    stop_price != decision.stop_loss
                    or (decision.entry_price > 0 and sizing_price > decision.entry_price)
                )
                payoff_skip = _execution_payoff_skip_reason(
                    decision,
                    sizing_price=sizing_price,
                    stop_price=stop_price,
                    geometry_changed=geometry_changed,
                    is_short=is_short,
                )
                if payoff_skip is not None:
                    raise RuntimeError(
                        "reward:risk execution skip is retired; "
                        f"got {payoff_skip!r} for {decision.symbol}"
                    )
                if (
                    not is_short and geometry_changed
                    and decision.take_profit > 0
                ):
                    logger.info(
                        "BUY %s: execution moved the geometry (entry $%.2f -> "
                        "$%.2f, stop $%.2f -> $%.2f) — no reward-side skip "
                        "applies (invented R/R gates retired).",
                        decision.symbol, decision.entry_price, sizing_price,
                        decision.stop_loss, stop_price,
                    )

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
                    sizing_price=risk_sizing_price, stop_price=stop_price,
                    is_short=is_short, fractional=fractional,
                )
                if qty_by_risk is not None and qty_by_risk < qty_by_alloc:
                    _risk_pct = _risk_budget_pct(pipeline)
                    logger.info(
                        "Vol-adjusted sizing for %s: qty_by_alloc=%s → qty_by_risk=%s "
                        "(risk %.2f/share, budget $%.0f = %.1f%% of equity)",
                        decision.symbol, _fmt_shares(qty_by_alloc),
                        _fmt_shares(qty_by_risk),
                        abs(risk_sizing_price - stop_price),
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

                # Long scale-in path B (owner 2026-09-15): if this BUY adds
                # to a name that already has a resting protective sell,
                # cancel that sell, confirm the cancel via trade_updates,
                # then submit. WAL is written first so a crash cannot leave
                # the position naked without a recovery row. Short adds are
                # blocked above: scale-in is the long path.
                if not is_short:
                    from src.execution.scale_in import prepare_long_add
                    add_prep = prepare_long_add(
                        broker=pipeline.broker, db=pipeline.db,
                        symbol=decision.symbol, positions=positions,
                        intended_stop=stop_price,
                    )
                else:
                    # Short scale-in path (owner-approved). The buy-stop is
                    # cancelled inside prepare_short_add, so the min-order
                    # FLOOR must run FIRST — a below-floor add must be dropped
                    # BEFORE any protection comes off. D11 keeps a short off
                    # the budget-resize floor above (it never spends cash), so
                    # this is where the floor is re-applied for a short add.
                    from src.execution.scale_in import (
                        held_signed_qty, prepare_short_add,
                    )
                    if held_signed_qty(positions, decision.symbol) < 0:
                        floor_usd = _min_order_usd(pipeline)
                        if estimated_cost < floor_usd:
                            logger.warning(
                                "Skipping SHORT add %s: order $%.2f (%s sh) is "
                                "below the $%.0f minimum worth trading — dropped "
                                "before any protective buy-stop is cancelled",
                                decision.symbol, estimated_cost,
                                _fmt_shares(qty), floor_usd,
                            )
                            _record_execution_skip(
                                pipeline, ctx, decision.symbol,
                                "below_min_notional",
                                f"short add ${estimated_cost:.2f} is below the "
                                f"${floor_usd:,.0f} minimum worth trading",
                            )
                            _record_pipeline_event(
                                pipeline, ctx, decision.symbol, "funding",
                                "refused", "short_add_below_min_notional",
                                resized_notional=estimated_cost,
                                min_order_usd=floor_usd,
                            )
                            continue
                    add_prep = prepare_short_add(
                        broker=pipeline.broker, db=pipeline.db,
                        symbol=decision.symbol, positions=positions,
                        intended_stop=stop_price,
                    )
                if add_prep is not None:
                    if add_prep.skip_reason:
                        _record_execution_skip(
                            pipeline, ctx, decision.symbol,
                            add_prep.skip_reason, add_prep.skip_detail,
                        )
                        _record_pipeline_event(
                            pipeline, ctx, decision.symbol, "scale_in",
                            "skipped", add_prep.skip_reason,
                            detail=add_prep.skip_detail,
                        )
                        continue
                    if add_prep.cancelled:
                        # Persisted event code kept as-is (the refusal-
                        # signature registry and historical rows read it); it
                        # covers a cancelled buy-stop on a short add too.
                        _record_pipeline_event(
                            pipeline, ctx, decision.symbol, "scale_in",
                            "protective_sell_cancelled",
                            "cancel_confirmed_via_trade_updates",
                            wal_row_id=add_prep.wal_row_id,
                            intended_stop=add_prep.intended_stop,
                            held_qty_before=add_prep.held_qty_before,
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
                # Item 82: `setup_type` was being classified a SECOND time
                # here, independently of `PortfolioConstructor._build_buy`/
                # `_build_short` (see `TradeDecision.setup_type` in
                # models.py, which exists specifically so execution does not
                # have to re-derive this fact). On a scale-in ADD to an
                # already-held name this second lookup re-read TODAY's
                # technical read and wrote it onto the new row —
                # `get_symbol_last_buy` returns the newest row, so this
                # silently RECLASSIFIED a position whose setup_type was
                # already pinned on its original entry, which is worse than
                # a mere disagreement: pace/progress (disabled for a
                # breakout) could flip back on, or off, on a held position
                # with no new entry decision behind the change. A genuinely
                # new entry has no prior pinned row and reads the single
                # value the constructor already classified, carried on the
                # decision.
                if add_prep is not None and add_prep.is_scale_in:
                    # Carry the position's OWN pinned setup_type forward. A
                    # short add reads the last SHORT open (item 82 mirror) —
                    # get_symbol_last_buy defaults to BUY rows and would
                    # otherwise miss the short's original entry.
                    _existing_buy = pipeline.db.get_symbol_last_buy(
                        decision.symbol,
                        action="SHORT" if is_short else "BUY",
                    )
                    pinned_setup_type = (_existing_buy or {}).get("setup_type") or None
                else:
                    pinned_setup_type = getattr(decision, "setup_type", None)
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
                    setup_type=pinned_setup_type,
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
                    # Set BEFORE the call: if submit raises, the broker may
                    # still have accepted the BUY. Restoring the cancelled
                    # stop at the OLD size is the daily-breaker / partial-
                    # fill bug (under-cover a grown position, and a working
                    # BUY plus a restored SELL is the wash-trade block).
                    # Leave the scale-in WAL row; drain rearms at broker qty.
                    submit_attempted = True
                    order = pipeline.broker.submit_order(
                        symbol=decision.symbol, qty=qty, side=entry_side,
                        limit_price=limit_price,
                        # PASSED THROUGH AS-IS (docs/WORK.md item 88). This
                        # used to read `stop_price if stop_price > 0 else
                        # None`, which laundered a garbage stop into the
                        # broker's "no stop was requested" case — so a zero
                        # reaching here submitted an unprotected entry and
                        # the broker never got the chance to refuse it.
                        # Every decision on this loop is a BUY or a SHORT and
                        # therefore OWES a stop, so there is nothing legitimate
                        # to convert to None: `submit_order` judges the value
                        # and returns `rejected_bad_stop` when it is not a
                        # price (surfaced below as `unusable_stop`).
                        stop_loss_price=stop_price,
                        reference_price=market_price,
                        # WORDING ONLY (see `submit_order`'s docstring): the
                        # same measured ATR(14) the constructor sized this
                        # trade against, so a fat-finger refusal can name
                        # the stock's own daily range instead of a bare
                        # percentage. None on the resume/sweep lanes that
                        # carry no analysis — the message then omits the
                        # range rather than inventing one.
                        atr=getattr(entry_analysis, "atr_14", None),
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
                    # `order["status"]` distinguishes WHO actually stopped
                    # this — our own pre-flight guards return a status
                    # before the order ever reaches the broker
                    # (rejected_outlier=fat-finger guard, kill_switch_halted)
                    # and both come back with no `id`, same as a real
                    # broker-side rejection. Collapsing all three into one
                    # "broker rejected" skip reason (pre-2026-09-17) read as
                    # if the broker had refused a sane order every time,
                    # when it was usually QAMC's own desk safety check
                    # blocking a bad price before the broker ever saw it
                    # (operator-reported, 2026-09-17: a fat-finger-guard
                    # rejection alerted as "broker rejected short"). Mark
                    # the pending row failed so it doesn't poison
                    # calibration as a "submitted" trade we never tracked.
                    # Distinct from the submit-raised case above: here we
                    # KNOW the order did not go live, so there's no orphan
                    # to sweep.
                    from src.execution.scale_in import restore_after_failed_add
                    restore_after_failed_add(
                        pipeline.broker, pipeline.db, add_prep, decision.symbol,
                    )
                    pipeline.db.mark_trade_submit_failed(pending_row_id)
                    order_status = str((order or {}).get("status") or "")
                    order_detail = (order or {}).get("detail")
                    if order_status == "rejected_outlier":
                        # The plain-word fact only (e.g. "stop $9.66 is 24%
                        # from price $7.79", from broker.py's
                        # _PLAIN_PRICE_LABELS) — WHO blocked it ("desk
                        # safety check, not the broker") is the Telegram
                        # formatter's job (src/trader_feed.py's
                        # `_SKIP_WHO_LABELS`), not repeated here.
                        skip_reason = "fat_finger_guard"
                        skip_detail = order_detail or "price is too far from the market price"
                    elif order_status == "rejected_bad_stop":
                        # The stop-side sanity checks (non-finite, non-
                        # positive, wrong side of entry) — desk-side, like
                        # the fat-finger guard, not the broker. Kept a
                        # SEPARATE reason from `fat_finger_guard` because
                        # they are a different fact about a different
                        # price, and collapsing them would tell the owner a
                        # price was "too far from the market" when what
                        # actually happened is the stop could never work.
                        skip_reason = "unusable_stop"
                        skip_detail = order_detail or "the stop price is not usable"
                    elif order_status == "kill_switch_halted":
                        skip_reason = "kill_switch_halted"
                        skip_detail = order_detail or (
                            "the trading kill switch is active"
                        )
                    else:
                        skip_reason = "broker_rejected"
                        # Board item 89 clarity defect — "a missing broker
                        # reason on a rejection". The broker's own words are
                        # kept when it gave any; when it did not, the message
                        # says so instead of leaving the owner to wonder.
                        skip_detail = (
                            f"broker rejected {decision.action.lower()} "
                            f"{_fmt_shares(qty)} @ "
                            f"{'limit $%.2f' % limit_price if limit_price else 'market'}"
                            + (f" — broker said: {order_detail}" if order_detail
                               else " — the broker gave no reason the desk recorded")
                        )
                        # The raw broker status token is not appended to the
                        # owner-facing detail any more (it read
                        # "(status=rejected)"); it stays in the log line and
                        # the pipeline event above.
                    _record_pipeline_event(
                        pipeline, ctx, decision.symbol, "order", "rejected",
                        skip_reason, trade_row_id=pending_row_id, qty=qty,
                    )
                    _record_execution_skip(
                        pipeline, ctx, decision.symbol, skip_reason, skip_detail,
                    )
                    continue

                # Submit accepted — finalize the pending row with the
                # broker's order_id and flip to 'submitted'.
                pipeline.db.confirm_trade_submitted(
                    pending_row_id, broker_order_id=order.get("id"),
                )
                buy_accepted = True
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
                if isinstance(order, dict) and (
                    order.get("pending_stop_price") or (
                        add_prep is not None and add_prep.cancelled
                    )
                ):
                    # Scale-in: the add's own stop is not automatically the
                    # live one. Most-protective for a long is the HIGHEST
                    # trigger (already computed on the prep). Prefer that
                    # over pending_stop_price or a looser cancelled stop
                    # would be replaced by the add's wider number.
                    protect_stop = order.get("pending_stop_price") or 0
                    if (
                        add_prep is not None and add_prep.is_scale_in
                        and add_prep.intended_stop > 0
                    ):
                        protect_stop = add_prep.intended_stop
                    pending_entry_stops.append({
                        "symbol": decision.symbol,
                        "side": entry_side,
                        "order_id": order.get("id"),
                        "stop_price": protect_stop,
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
                        "cover_full_position": bool(
                            add_prep is not None and add_prep.is_scale_in
                        ),
                        "held_qty_before": (
                            add_prep.held_qty_before if add_prep else 0.0
                        ),
                        "wal_row_id": (
                            add_prep.wal_row_id if add_prep else None
                        ),
                        "cancelled_specs": (
                            add_prep.specs if add_prep else []
                        ),
                        "intended_stop": (
                            add_prep.intended_stop if add_prep else 0.0
                        ),
                    })
            except Exception as e:
                if (
                    add_prep is not None and add_prep.cancelled
                    and not buy_accepted
                ):
                    if submit_attempted:
                        logger.critical(
                            "scale-in: BUY submit for %s failed after the "
                            "protective sell was cancelled — WAL row %s "
                            "stays so drain rearms at the broker's current "
                            "qty; restoring the old stop size would under-"
                            "cover a fill that may already have landed",
                            decision.symbol, add_prep.wal_row_id,
                        )
                    else:
                        from src.execution.scale_in import restore_after_failed_add
                        restore_after_failed_add(
                            pipeline.broker, pipeline.db, add_prep,
                            decision.symbol,
                        )
                logger.error("Order failed for %s %s: %s", decision.action, decision.symbol, e)

        # Protect every filled entry (GTC stop-limit keyed to the ACTUAL fill).
        for spec in pending_entry_stops:
            if not spec.get("order_id"):
                continue
            try:
                # Single-shot reprice FIRST, protection second, always. The
                # reprice may hand back a different order id (Alpaca mints one
                # per replacement) plus any shares an ancestor order filled;
                # both feed straight into the stop so no filled share is left
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
                # End-of-session cancel of a still-unfilled entry lives
                # inside `place_entry_protection` (see its docstring for the
                # derivation); the callback is how the owner gets told, with
                # the prices this stage tried, which the broker does not know.
                protection = pipeline.broker.place_entry_protection(
                    symbol=spec["symbol"], order_id=entry_order_id,
                    stop_price=spec["stop_price"], requested_qty=spec["qty"],
                    superseded_filled_qty=superseded_fill,
                    side=entry_side,
                    on_unfilled_cancel=(
                        lambda info, _spec=spec:
                        _alert_owner_entry_cancelled(pipeline, _spec, info)
                    ),
                    cover_full_position=bool(spec.get("cover_full_position")),
                    held_qty_before=float(spec.get("held_qty_before") or 0),
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
                if spec.get("cover_full_position") or spec.get("wal_row_id") is not None:
                    from src.execution.scale_in import (
                        discharge_scale_in_wal,
                        restore_cancelled_stops,
                    )
                    filled_here = 0.0
                    try:
                        info = pipeline.broker.get_order_fill_info(
                            entry_order_id,
                        ) or {}
                        filled_here = float(info.get("filled_qty") or 0)
                    except Exception:  # noqa: BLE001
                        filled_here = 0.0
                    uncovered = 0.0
                    if isinstance(protection, dict):
                        try:
                            uncovered = float(protection.get("uncovered_qty") or 0)
                        except (TypeError, ValueError):
                            uncovered = 0.0
                    # A short add's protection is a BUY-stop; restore and
                    # write-back must both use the short side.
                    _spec_is_short = str(
                        spec.get("side", "buy")
                    ).lower() != "buy"
                    if protection is None and filled_here <= 0:
                        if restore_cancelled_stops(
                            pipeline.broker, spec["symbol"],
                            spec.get("cancelled_specs") or [],
                            side="buy" if _spec_is_short else "sell",
                        ):
                            discharge_scale_in_wal(
                                pipeline.db, spec.get("wal_row_id"),
                            )
                    elif protection is not None and uncovered <= 0:
                        from src.execution.stop_records import (
                            accepted_stop_order, write_back_stop_loss,
                        )
                        if accepted_stop_order(protection) or not isinstance(
                            protection, dict,
                        ):
                            write_back_stop_loss(
                                pipeline.db, spec["symbol"], spec["stop_price"],
                                is_short=_spec_is_short,
                            )
                        discharge_scale_in_wal(
                            pipeline.db, spec.get("wal_row_id"),
                        )
                    # else: fill happened and rearm did not fully cover.
                    # WAL stays. Guard 2 already paged the owner.
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
                    # H1: on a SHORT SCALE-IN the protective buy-stop that
                    # covered the PRE-EXISTING short leg was already cancelled
                    # in prep, so covering only the add's fill (filled_qty)
                    # would leave that older leg naked — exactly the unbounded
                    # exposure D7 exists to prevent. Cover the ENLARGED short:
                    # the broker's current qty (magnitude), the same authority
                    # cover_qty_for_rearm uses, with the |fill|+|held| fallback
                    # when the broker cannot be read. For a NEW short this
                    # equals filled_qty, so the non-scale-in path is unchanged.
                    if spec.get("cover_full_position"):
                        from src.execution.scale_in import cover_qty_for_rearm
                        cover_qty = cover_qty_for_rearm(
                            pipeline.broker, symbol=spec["symbol"],
                            filled_qty=filled_qty,
                            held_qty_before=float(spec.get("held_qty_before") or 0),
                        )
                        if cover_qty < filled_qty:
                            # Never cover LESS than what we know filled.
                            cover_qty = filled_qty
                    else:
                        cover_qty = filled_qty
                    if cover_qty > 0:
                        logger.critical(
                            "SHORT %s: PROTECTIVE STOP FAILED after %.4f "
                            "share(s) filled — a naked short has UNBOUNDED "
                            "loss. Submitting an IMMEDIATE market COVER of the "
                            "full short (%.4f) instead of waiting for the next "
                            "reconcile pass.",
                            spec["symbol"], filled_qty, cover_qty,
                        )
                        try:
                            cover_order = pipeline.broker.submit_order(
                                symbol=spec["symbol"], qty=cover_qty, side="buy",
                            )
                            cover_id = (
                                cover_order.get("id")
                                if isinstance(cover_order, dict) else None
                            )
                            pipeline.db.insert_trade(
                                symbol=spec["symbol"], action="EMERGENCY_COVER",
                                qty=cover_qty, price=0.0,
                                reasoning=(
                                    "protective stop failed to place after a "
                                    "SHORT entry filled — immediate market "
                                    "cover of the full (enlarged) short to bound "
                                    "an otherwise naked short"
                                ),
                                run_id=run_id, broker_order_id=cover_id,
                                fill_status="submitted",
                            )
                            _record_pipeline_event(
                                pipeline, ctx, spec["symbol"], "protection",
                                "emergency_cover", "naked_short_protection_failed",
                                qty=cover_qty, broker_order_id=cover_id,
                            )
                        except Exception as cover_exc:  # noqa: BLE001
                            logger.critical(
                                "SHORT %s: EMERGENCY COVER ALSO FAILED (%s) — "
                                "%.4f share(s) are NAKED SHORT with NO "
                                "protective stop and NO cover in flight. "
                                "REQUIRES IMMEDIATE OPERATOR INTERVENTION.",
                                spec["symbol"], cover_exc, cover_qty,
                            )
                            _record_pipeline_event(
                                pipeline, ctx, spec["symbol"], "protection",
                                "emergency_cover_failed",
                                "naked_short_no_protection_no_cover",
                                qty=cover_qty, detail=str(cover_exc),
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

        # Phase 14b — the rotation's outcome, both legs, recorded durably.
        # A sale that freed room for a BUY that then did not happen is the
        # exact churn the anti-rotation rules exist to prevent, so that
        # case is also paged (`_alert_rotation_buy_leg_missing`).
        _record_rotation_buy_leg_outcome(pipeline, ctx, orders)

        ctx.orders = orders
        return orders
