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
from dataclasses import replace
from typing import Any, TYPE_CHECKING

from src import evidence_gate
from src.agents.base import agent_log_kwargs, seat_acceptance_kwargs
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
    ANALYSIS_DROP_KIND as _ANALYSIS_DROP_KIND,
    DROP_CODE_UNSPECIFIED,
    NewsIntelligenceReport, Nomination, TechAnalysisResult, TechnicalIndicators,
    missing_stated_falsifier, open_target_missing_falsifier,
    parse_telemetry, SOFT_EXIT_HEAL_EVENT_REASON,
    SOFT_EXIT_MISSING_AFTER_RETRY,
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
    gap_adjusted_risk_per_share,
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

    targets = list(getattr(portfolio_decision, "targets", None) or [])
    new_targeted = any(
        t.symbol.upper() == new_symbol and not t.is_close for t in targets
    )
    if not new_targeted:
        # The buy leg is invariant across held candidates: with no new name
        # targeted there is nothing to make room FOR, so this abandons the
        # whole rotation, not merely the current candidate.
        _rotation_skip(
            pipeline, ctx, opportunity, "pm_did_not_target_new_candidate",
        )
        return

    def _sellable_this_run(cand_symbol: str, cand_reasons):
        """Every per-holding sell guard for ONE below-bar candidate.

        Returns `(held_position, protection, history, cand_opportunity)` when
        this name may be closed this run, or `None` after recording exactly
        why it may not — so the caller advances to the next-worst below-bar
        holding instead of abandoning the rotation (board item 39). Every
        guard here is a fact about THIS name only; the buy-leg precondition
        is checked once, above, because it does not depend on which held name
        makes the room.
        """
        cand_opp = replace(
            opportunity, held_symbol=cand_symbol, reasons=tuple(cand_reasons),
        )
        held_pos = next(
            (p for p in (positions or [])
             if (p.symbol or "").upper() == cand_symbol),
            None,
        )
        if held_pos is None or held_pos.qty <= 0:
            _rotation_skip(
                pipeline, ctx, cand_opp, "held_symbol_is_not_a_long_position",
                qty=getattr(held_pos, "qty", None),
            )
            return None
        if any(t.symbol.upper() == cand_symbol for t in targets):
            _rotation_skip(
                pipeline, ctx, cand_opp, "pm_already_targets_held_symbol",
            )
            return None

        # In flight? Read from the desk's own durable state machine. Any
        # failure to answer is a refusal to act on THIS name, never an
        # assumption of "clear".
        try:
            today_rows = pipeline.db.get_trades(
                symbol=cand_symbol, limit=50, today_only=True,
            )
            bought_today = any(
                str(r.get("action") or "").upper() == "BUY" for r in today_rows
            )
            in_flight_rows = [
                r for r in today_rows
                if str(r.get("fill_status") or "").lower()
                in _IN_FLIGHT_FILL_STATUSES
                and str(r.get("action") or "").upper() != "HOLD"
            ]
            pending_restores = [
                r for r in pipeline.db.get_pending_protection_restores()
                if str(r.get("symbol") or "").upper() == cand_symbol
            ]
            pending_repegs = [
                r for r in pipeline.db.get_pending_repegs()
                if str(r.get("symbol") or "").upper() == cand_symbol
            ]
        except Exception as exc:  # noqa: BLE001
            _rotation_skip(
                pipeline, ctx, cand_opp, "in_flight_check_failed",
                detail=str(exc),
            )
            return None
        if bought_today:
            _rotation_skip(
                pipeline, ctx, cand_opp, "held_symbol_bought_today",
            )
            return None
        if in_flight_rows:
            _rotation_skip(
                pipeline, ctx, cand_opp, "order_in_flight_on_held_symbol",
                detail="; ".join(
                    f"{r.get('action')}:{r.get('fill_status')}:"
                    f"{r.get('broker_order_id')}"
                    for r in in_flight_rows
                )[:400],
            )
            return None
        if pending_restores:
            _rotation_skip(
                pipeline, ctx, cand_opp, "sell_already_in_flight_wal_row",
                detail=str(pending_restores[0].get("sell_order_id")),
            )
            return None
        if pending_repegs:
            _rotation_skip(
                pipeline, ctx, cand_opp, "entry_repeg_in_flight",
                detail=str(pending_repegs[0].get("old_order_id")),
            )
            return None

        # Item-25 holding discipline: is the position still structurally
        # protected? Same method, same inputs `RiskStage` uses. A protected
        # (thesis-intact) name is NEVER sold — the walk passes OVER it to the
        # next below-bar name; it never overrides the discipline.
        cand_hist = (position_history or {}).get(cand_symbol) or (
            position_history or {}
        ).get(getattr(held_pos, "symbol", cand_symbol)) or {}
        try:
            cand_protection = pipeline._structural_protection_for_holding(
                symbol=cand_symbol,
                thesis_invalid_if=cand_hist.get("thesis_invalid_if"),
                entry_price=cand_hist.get("entry_price"),
                stop_loss=cand_hist.get("stop_loss"),
                is_short=False,
                run_id=ctx.run_id,
            )
        except Exception as exc:  # noqa: BLE001
            _rotation_skip(
                pipeline, ctx, cand_opp, "protection_check_failed",
                detail=str(exc),
            )
            return None
        if cand_protection.protected:
            _rotation_skip(
                pipeline, ctx, cand_opp, "held_symbol_structurally_protected",
                protection_basis=cand_protection.basis,
                protection_detail=str(cand_protection.detail)[:400],
            )
            return None
        return held_pos, cand_protection, cand_hist, cand_opp

    # Board item 39. Walk the below-bar cull set worst-first and close the
    # FIRST name that clears every per-holding guard. The rotation is
    # abandoned only when EVERY below-bar holding is unsellable this run —
    # not, as before, when the single worst name happened to be structurally
    # protected. `ineligible_candidates` is empty on the ranked-margin tier
    # and on a directly-constructed opportunity, so both fall back to the one
    # `held_symbol` and behave exactly as before.
    cull_set = (
        opportunity.ineligible_candidates
        if (opportunity.tier == "ineligible_hold"
            and opportunity.ineligible_candidates)
        else ((held_symbol, opportunity.reasons),)
    )
    chosen = None
    for cand_symbol, cand_reasons in cull_set:
        chosen = _sellable_this_run(
            str(cand_symbol).strip().upper(), cand_reasons,
        )
        if chosen is not None:
            break
    if chosen is None:
        # Every below-bar holding was unsellable this run; each was recorded
        # under its own reason above.
        return
    _held_pos, protection, hist, opportunity = chosen
    held_symbol = opportunity.held_symbol

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

    The list below is FOUR long and so is `REQUIRED_BUY_LEG_GATES`; a
    `gate_coverage_incomplete` refusal at the bottom of this function is
    what keeps the two from drifting, and this paragraph is the third copy
    of the count, so change all three together. It was six until
    2026-09-23, when the owner's removal of the account-level loss halt
    (PR #584) deleted the `daily_loss_recheck` refusal (retired-ok) the
    first gate anticipated, and five until 2026-09-24, when `below_min_notional`
    (retired-ok) was deleted the same way: the flat $500 `min_order_usd`
    notional floor it named was arbitrary, not a broker minimum, and Alpaca
    charges no stock commission, so `_prevent_rotation_naked_sale` no
    longer refuses a rotation's replacement buy for re-sizing small but
    nonzero — only a genuine zero still refuses, via `insufficient_cash`.
    Nothing replaced either deleted gate: the gross exposure the loss halt
    shared with the §11.2 ladder is still gated, by `insufficient_cash`.

      * `no_price` / `stale_entry` — `_live_fill_price` and the same 5%
        deviation test the preflight applies. Neither depends on the sale
        at all, so these are exact now, not projected.
      * `qty_zero` — the preflight's own sizing helpers (`_size_shares`,
        `_qty_by_risk_budget`, `_fractional_sizing_allowed`) at the
        projected equity.
      * `insufficient_cash` — `_entry_deployment_budget` over the
        projected post-sale positions, equity and settled cash, drained by
        every entry earlier in the same session exactly as the submit loop
        drains it. This is what still carries the §11.2 gross ladder: the
        budget's ladder-backed branch measures the headroom the sale
        frees, so a rotation that would breach gross is refused here even
        though the account-level alarm is gone.

    **What this does NOT close, stated plainly.** The BUY submit loop can
    still refuse an entry for reasons no pre-check can evaluate in advance:
    the latency window (`latency_window`), the borrow gate on a SHORT, and
    any broker rejection. (Board item 183, 2026-09-30: a displayed quote
    through the entry ceiling is no longer one of them — it is recorded as
    `venue_quote_through_ceiling` and the order is still sent, because the
    limit is its own protection.) Those are not knowable before the sale, and the tape
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
        # Fixed 2026-09-24 (retired the `below_min_notional` gate outright,
        # see `REQUIRED_BUY_LEG_GATES`): this used to refuse the rotation
        # whenever re-sizing to the post-sale budget landed under the flat
        # `min_order_usd` floor — an arbitrary $500 with no broker minimum
        # behind it, and Alpaca charges no stock commission. A rotation
        # whose replacement buy re-sizes small but nonzero
        # (`affordable_qty > 0`, already checked above) is cleared, not
        # refused; the real "no shares fit" case is `insufficient_cash`
        # above.

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
    path after the funding step has already returned. Call this AFTER funding
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

    # SIDE (board item 197). The slippage bound is the worst price this entry
    # was approved to pay, so it sits ABOVE the reference for a buy and BELOW
    # it for a `sell_short`. Everything downstream — the room test, which side
    # of the quote is read, and the direction the limit walks — flips with it.
    # Written before `repeg_enabled` was ever turned on, so no short entry has
    # been through this path.
    is_short = str(spec.get("side", "buy")).lower() != "buy"
    if is_short:
        ceiling = reference * (1 - slippage_bps / 10_000.0)
    else:
        ceiling = reference * (1 + slippage_bps / 10_000.0)
    ceiling = round(ceiling, 2 if ceiling >= 1 else 4)
    spec["ceiling"] = ceiling
    no_room = (limit_price <= ceiling + 1e-9) if is_short \
        else (limit_price >= ceiling - 1e-9)
    if no_room:
        # Expected for most entries: since PR #111 the submitted limit IS the
        # ceiling, so there is nothing to reprice toward. Room exists only
        # when the limit was set inside the ceiling — e.g. the quote was
        # unavailable at submission and the analyst's entry price was used.
        logger.debug(
            "re-peg %s: limit $%.4f is already at the %.0fbp %s $%.4f — "
            "nothing to chase", symbol, limit_price, slippage_bps,
            "floor" if is_short else "ceiling", ceiling,
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
    # A buy fills against the ask; a short sale fills against the bid.
    ask = quote.get("bid_price" if is_short else "ask_price") \
        if isinstance(quote, dict) else None
    if not isinstance(ask, (int, float)) or ask <= 0:
        spec["repeg_outcome"] = "quote_unavailable"
        return order_id, 0.0
    marketable = (float(ask) >= limit_price - 1e-9) if is_short \
        else (float(ask) <= limit_price + 1e-9)
    if marketable:
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
    assert target >= ceiling - 1e-9 if is_short else target <= ceiling + 1e-9
    crosses = (float(ask) >= target - 1e-9) if is_short \
        else (float(ask) <= target + 1e-9)
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


def _record_realised_sector_weights(
    pipeline, ctx, portfolio_decision, total_value,
) -> None:
    """One durable row per run with the REALISED `(sector, side)` weights of
    the orders the constructor actually built this session.

    Board item 224 (2026-10-01). RECORDING ONLY: nothing may read this back
    into a sizing, ordering or refusal decision, and it may NEVER be swept
    for the sector cap that would have performed best — see the
    `realised_sector_weights` note in `src/storage/db.py::_migrate` for the
    unit, the denominator and the full bar on its use.

    Called here, immediately after `construct_orders` has returned, because
    this is the first point at which the FINISHED order list exists: the
    gross-exposure rationing inside the constructor runs last and changes
    sizes after each order is built, so anything recorded earlier would be
    what was hoped for rather than what was built. Never raises.
    """
    try:
        db = getattr(pipeline, "db", None)
        if db is None or not hasattr(db, "record_realised_sector_weights"):
            return
        db.record_realised_sector_weights(
            decisions=list(getattr(portfolio_decision, "decisions", None) or []),
            sectors=getattr(
                pipeline.portfolio_constructor, "last_order_sectors", None,
            ),
            total_value=total_value,
            run_id=getattr(ctx, "run_id", None),
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("realised sector-weight recording failed: %s", exc)


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
    # D8: gap-risk sizing haircut — SIZING ONLY, never stop placement (the
    # stop is untouched). This execution-time belt must be at least as
    # conservative for a short as the constructor's own primary sizing, so
    # both legs call the SAME application site (board item 216): execution
    # ships `min(qty_by_alloc, qty_by_risk)`, and while these were two
    # separate multiplies a change to one of them was silently a half-change
    # to the quantity that actually reached the market.
    risk_per_share = gap_adjusted_risk_per_share(
        risk_per_share,
        is_short=is_short,
        multiple=getattr(
            getattr(pipeline.config, "risk", None),
            "short_gap_risk_multiple", None,
        ),
    )
    if risk_per_share <= 0:
        return None
    risk_dollars = total_value * _risk_budget_pct(pipeline) / 100
    return _size_shares(
        pipeline, risk_dollars / risk_per_share, fractional=fractional,
    )


def _min_order_usd(pipeline) -> float:
    """`cash_sweep.min_order_usd`, read the same way every other caller reads
    it.

    Fixed 2026-09-24: this used to be a NOTIONAL floor that refused a token
    trade outright in the risk engine, the rotation buy-leg gate and the
    execution-time cash re-size — an arbitrary $500 with no broker minimum
    behind it, justified by a false "pays commission" claim (Alpaca charges
    none). None of those three still use this value to reject a small trade;
    it is kept here only because `apply_gross_ceiling` still accepts it as an
    ignored parameter (existing callers pass it). The value's real, live job
    is gating the spare-cash SWEEP (`src/execution/cash_sweep.py`), not trade
    sizing.
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


def _record_scale_in_window_closed(pipeline, ctx, spec: dict, *, covered: bool) -> None:
    """Close the scale-in unprotected window with a measured duration.

    Board item 193. A scale-in cancels the resting protective stop so the add
    can reach the broker, which leaves the WHOLE held position — not just the
    add — with no stop until the rearm lands. This emits one event per cancel
    at the moment the rearm attempt returns, carrying:

      * `window_seconds` — broker cancel acknowledgement to broker rearm
        acknowledgement, both `time.monotonic()` inside the one run, so it
        bounds real exposure and never reflects a row's write time;
      * `held_qty_before` and `exposed_notional` — the size of what was naked;
      * `covered` — False when the rearm did NOT land, which means the window
        is still open when the event is written and the fail-closed owner
        alert below it is the thing that matters.

    Nothing is emitted when no cancel happened: a naked add has no window.
    """
    from src.execution.scale_in import unprotected_window_seconds
    seconds = unprotected_window_seconds(spec.get("cancel_confirmed_at"))
    if seconds is None:
        return
    held = abs(float(spec.get("held_qty_before") or 0.0))
    price = float(spec.get("reference_price") or 0.0)
    _record_pipeline_event(
        pipeline, ctx, spec.get("symbol"), "scale_in",
        "unprotected_window_closed" if covered else "unprotected_window_still_open",
        "rearm_acknowledged" if covered else "rearm_did_not_land",
        window_seconds=seconds,
        held_qty_before=held,
        exposed_notional=round(held * price, 2) if price > 0 else None,
        wal_row_id=spec.get("wal_row_id"),
        stop_price=spec.get("stop_price"),
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
            **seat_acceptance_kwargs(
                "no_valid_grounded_decision" if not reasked else None,
                result=reask_result,
            ),
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


def _record_mechanical_soft_exit_restores(pipeline, ctx) -> None:
    """Write down what the MECHANICAL soft-exit heal did this run.

    Board item 78. `src.seat_heal.restore_stated_soft_exits` puts back a
    `thesis_invalid_if` that a later null-wipe blanked, using the sentence
    the model itself already wrote, and it never invents one. It runs
    inside a Pydantic validator, so it has no run id and no database
    handle and has never recorded a single thing. Two of item 78's three
    removal criteria are claims about this heal, so they could not be
    judged at all.

    RECORDING ONLY. Nothing reads these rows back into a trading
    decision and they may never be swept for a threshold. Never raises.
    """
    try:
        from src.seat_heal import drain_restore_observations

        observations, dropped = drain_restore_observations()
        if not observations:
            return
        db = getattr(pipeline, "db", None)
        writer = getattr(db, "record_soft_exit_heal_restores", None)
        if not callable(writer):
            return
        writer(
            observations=observations,
            run_id=getattr(ctx, "run_id", None),
            dropped=dropped,
        )
    except Exception as exc:  # noqa: BLE001 — a recording never blocks a trade
        logger.error("mechanical soft-exit heal recording failed: %s", exc)


def _record_soft_exit_heals(pipeline, ctx) -> None:
    """Drain the PM's per-name soft-exit heal outcomes onto ctx and to disk.

    Board item 78. The heal itself (`PortfolioManagerAgent.
    _fill_missing_open_falsifiers`, the desk's standing heal order:
    mechanical restore, then ONE paid seat retry under the existing
    per-seat cap) has four exits that used to leave nothing but a log
    line — never attempted for want of a replayable message, blocked by
    the spend cap, errored, or answered without a falsifier. A name can
    therefore be refused for a blank falsifier without any durable record
    of whether the seat was ever actually re-asked.

    One `pipeline_event` row per name fixes that, and `ctx.soft_exit_heals`
    carries the same fact forward so the refusal can quote what really
    happened instead of asserting a retry. Recording only; never raises.
    """
    _record_mechanical_soft_exit_restores(pipeline, ctx)
    try:
        agent = getattr(pipeline, "portfolio_manager", None)
        drain = getattr(agent, "drain_soft_exit_heals", None)
        heals = dict(drain() if callable(drain) else {})
    except Exception as exc:  # noqa: BLE001
        logger.error("soft-exit heal drain failed: %s", exc)
        return
    if not heals:
        return
    try:
        ctx.soft_exit_heals = {**(getattr(ctx, "soft_exit_heals", None) or {}), **heals}
    except Exception:  # noqa: BLE001
        pass
    for symbol, heal in heals.items():
        _record_pipeline_event(
            pipeline, ctx, symbol, "soft_exit_heal",
            str(heal.get("outcome") or "unknown"), SOFT_EXIT_HEAL_EVENT_REASON,
            detail=str(heal.get("detail") or ""),
        )


def _soft_exit_heal_detail(ctx, symbol: str) -> str:
    """The TRUE per-name heal outcome, for the refusal's durable reason.

    Board item 78 / owner 2026-09-25 ("untrue is a lie"): the refusal used
    to assert "after mechanical heal and one paid retry" for every name,
    including names whose retry was never attempted. It now states what the
    heal record says, and says plainly when there is no heal record at all.
    """
    heal = (getattr(ctx, "soft_exit_heals", None) or {}).get(
        str(symbol).strip().upper()
    )
    if isinstance(heal, dict) and (heal.get("detail") or heal.get("outcome")):
        return (
            f"heal outcome '{heal.get('outcome') or 'unknown'}': "
            f"{heal.get('detail') or ''}".strip()
        )
    return (
        "no soft-exit heal was recorded for this name — the mechanical "
        "restore did not fill it and no paid retry outcome was filed"
    )


def _record_soft_exit_missing_after_retry(
    pipeline, ctx, symbol: str, *, action: str | None = None,
) -> None:
    _record_pipeline_event(
        pipeline, ctx, symbol, "deterministic_gate",
        "blocked", SOFT_EXIT_MISSING_AFTER_RETRY,
        detail=(
            "thesis_invalid_if still empty or unknown; refusing this "
            "name before the book. No falsifier was invented. "
            + _soft_exit_heal_detail(ctx, symbol)
        ),
        heal_outcome=str(
            (
                (getattr(ctx, "soft_exit_heals", None) or {}).get(
                    str(symbol).strip().upper()
                )
                or {}
            ).get("outcome")
            or "none_recorded"
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
    — Risk must still be told the name was proposed and refused.

    EXACTLY WHAT WOULD JUSTIFY DELETING IT (board item 78, 2026-09-26).
    It stays until a LIVE session record shows all three. None of the
    three can be shown offline: each is a claim about what the seats
    really emit when real money is at stake.
      1. `specialist_evidence` holds ZERO `pipeline_event` rows with
         reason `soft-exit missing after retry`, over a window of live
         sessions that actually produced BUY/SHORT targets — not a window
         in which the desk simply proposed nothing. Zero refusals across
         zero opens proves nothing. [measured 2026-09-26: 0 such rows in
         4,709 pipeline_event rows spanning 2026-09-02 to 2026-09-26, so
         criterion 1 alone is already met and is NOT sufficient.]
      2. Over that same window no `soft_exit_heal` row carries outcome
         `paid_retry`: the heal being needed and working is not the same
         as the producing step producing. This is the criterion item 78
         actually names — Tech and the PM emitting a real falsifier
         unaided.
      3. No `soft_exit_heal` row in that window carries `not_attempted`,
         `cap_blocked` or `failed`. Each of those says the desk does not
         yet know whether the seat can fill the field, so a zero refusal
         count in their presence is silence, not evidence.
    Until all three hold, this stays. The heal and the durable heal record
    above are what keep it unreachable in normal operation.

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


def _record_seat_stances(
    pipeline, ctx, evidence_registry, symbols, *,
    non_corroborating_sources=None,
) -> None:
    """Spec §9.5 — record who ARGUED AGAINST, not only who proposed. NEVER raises.

    `non_corroborating_sources` (board item 109) marks the stances the §9.4
    tally would not let corroborate the trade — today only a macro stance
    broadcast onto a name whose sector the macro read never mentioned. The
    ledger is read later to score who was right; a stance recorded with no
    trace that the desk declined to count it reads as a seat that backed the
    idea, which is not what happened. It is recorded in `observation`
    because that field already exists and already travels with the row; no
    schema change is taken for this.

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
        non_corroborating = non_corroborating_sources or {}
        stances: list[SeatStance] = []
        for symbol in sorted(wanted):
            gated = non_corroborating.get(symbol) or frozenset()
            for source, stance in sorted((evidence_registry.get(symbol) or {}).items()):
                seat = normalize_seat(source)
                declared = (nominations.get(symbol) or {}).get(seat) or {}
                conviction = declared.get("conviction")
                if not conviction and seat == "technical":
                    conviction = tech_conviction.get(symbol)
                observation = str(declared.get("observation") or "")
                if source in gated:
                    note = (
                        "market-wide stance, not a read on this name's "
                        "sector — did not count toward agreement for the "
                        "trade (still counted against one it opposed)"
                    )
                    observation = f"{observation} [{note}]".strip()
                stances.append(SeatStance(
                    seat=seat, symbol=symbol, stance=stance,
                    conviction=conviction or DEFAULT_CONVICTION,
                    nominated=bool(declared),
                    observation=observation,
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
    seat_edited_fields: set[str] = set()
    for m in (getattr(verdict, "modifications", None) or []):
        field = aliases.get(m.field, m.field)
        if m.symbol.strip().upper() == key[0] and field in changes:
            seat_edited_fields.add(field)
            if (m.reason or "").strip():
                seat_reasons.append(f"{field}: {m.reason}")
    # Board item 134. When a stop_loss/entry_price edit widened risk-per-share,
    # `_apply_risk_modifications` reduces `allocation_pct` to keep dollar risk
    # within the granted budget. That drop is NOT a field the seat named, so it
    # would otherwise sit in `changes` with no reason of its own — reading as an
    # unexplained move or bucketed under the stop edit. Attribute it explicitly
    # (only when the seat did not itself edit allocation_pct, the size fell, and
    # a stop/entry edit is what moved).
    alloc_change = changes.get("allocation_pct")
    if (
        alloc_change is not None
        and "allocation_pct" not in seat_edited_fields
        and decision.action in ("BUY", "SHORT")
        and isinstance(alloc_change[0], (int, float))
        and isinstance(alloc_change[1], (int, float))
        and alloc_change[1] < alloc_change[0]
        and seat_edited_fields & {"stop_loss", "entry_price"}
    ):
        widened = ", ".join(sorted(seat_edited_fields & {"stop_loss", "entry_price"}))
        seat_reasons.append(
            f"allocation_pct: reduced to keep dollar-risk within the granted "
            f"budget after the risk seat edited {widened} (wider stop / edited "
            f"entry -> smaller position, never larger dollar risk)"
        )
    # Board items 134 + 162 (owner ruling 2026-09-25): `scale_all_buys` is
    # ADVISORY on entries and no longer changes any allocation_pct, so it can
    # no longer be the cause of an allocation move here — any allocation change
    # in `changes` now comes only from the seat's per-symbol `modifications`.
    # The scale concern is recorded separately as a `scale_advisory` event in
    # `RiskStage.run`; it must not be attributed to a modification here.
    details["changes"] = changes
    reason = "; ".join(seat_reasons) or (
        f"risk manager changed {', '.join(sorted(changes))} on "
        f"{decision.symbol} without stating a reason for this symbol"
    )
    return "modified", reason, details


def _record_scale_advisory(decisions, verdict) -> tuple[list, float, list]:
    """RECORD — but do NOT APPLY — RiskVerdict.scale_all_buys on entries.

    Owner ruling 2026-09-25 (reaffirming his 2026-09-19 ruling), board items
    134 + 162: a model-picked, unverifiable portfolio-wide multiplier may not
    size real trades. On ENTRIES the risk seat is now ADVISORY — its
    `scale_all_buys` concern and reason are captured and recorded durably
    (owner-facing evidence / feed), but the multiplier is NOT applied to any
    `allocation_pct` and drops NO trade. Every entry proceeds at the size the
    constructor / allocator set, subject to the HARD aggregate limits enforced
    downstream (gross-exposure ceiling, per-trade risk %, correlation /
    at-risk budget, per-name `max_position_pct`), which are unchanged and
    remain the real constraint.

    Before this ruling the same value multiplied every BUY/SHORT allocation
    and DROPPED any entry it zeroed (board item 136). That sizing effect is
    removed. The recording it fed is kept, re-cast as an advisory record: the
    caller files one pipeline event per flagged entry so the concern + reason
    still reach the desk. `scale_all_buys` still feeds logging / metrics / the
    trader feed elsewhere (unchanged) — the ONLY behaviour removed here is its
    effect on entry sizing.

    Treats None/missing as 1.0 (no concern). Returns
    ``(decisions_unchanged, scale, advised)`` where `advised` is the list of
    ``(symbol, allocation_pct)`` BUY/SHORT entries the seat flagged, populated
    only when ``0.0 <= scale < 1.0``. SELL, COVER and HOLD were never scaled
    and are never flagged. The decisions list is returned unchanged.
    """
    scale_raw = getattr(verdict, "scale_all_buys", 1.0)
    scale = 1.0 if scale_raw is None else float(scale_raw)
    advised: list[tuple[str, float]] = []
    if not (0.0 <= scale < 1.0):
        return list(decisions), scale, advised

    for d in decisions:
        if d is not None and d.action in ("BUY", "SHORT"):
            advised.append((d.symbol, d.allocation_pct))
            logger.info(
                "scale_all_buys=%.2f is ADVISORY on entries (board item 134): "
                "recording %s's exposure concern; allocation_pct %.2f%% is "
                "UNCHANGED and the trade is NOT dropped — the hard aggregate "
                "limits remain the constraint",
                scale, d.symbol, d.allocation_pct,
            )
    return list(decisions), scale, advised



def _probe_sale_census(provider: object) -> dict | None:
    """Board item 63: find the SEC provider's last sale census, however the
    provider happens to be wrapped. Duck-typed on purpose -- the combined
    provider delegates by attribute, exactly as `form4_coverage` is probed
    a few lines below. Returns None when nothing recorded one."""
    candidates: list[object] = [provider]
    nested = getattr(provider, "providers", None)
    if isinstance(nested, (list, tuple)):
        candidates.extend(nested)
    if hasattr(provider, "__dict__"):
        candidates.extend(vars(provider).values())
    for candidate in candidates:
        census = getattr(candidate, "last_sale_census", None)
        if isinstance(census, dict) and census.get("sale_rows"):
            return census
    return None


import sys as _sys
import types as _types

# --- Re-exports: the four stage classes moved to src/stage_*.py (item 210, step 10).
# Resolved lazily through module ``__getattr__`` so that importing a stage module
# first does not create a cycle, and so that a test patching
# ``src.pipeline_stages.RiskStage`` replaces the very object this module hands out.
_STAGE_CLASS_MODULES = {
    "MorningResearchStage": "src.stage_morning_research",
    "DecisionStage": "src.stage_decision",
    "RiskStage": "src.stage_risk",
    "ExecutionStage": "src.stage_execution",
    # The five RiskStage private helpers and their two constants moved with it.
    "_record_queued_earnings_refusals": "src.stage_risk",
    "_apply_sector_unresolved_alert": "src.stage_risk",
    "_reconcile_parse_loss": "src.stage_risk",
    "_parse_loss_advisories": "src.stage_risk",
    "_persist_dropped_reasons": "src.stage_risk",
    "ANALYSIS_DROP_KIND": "src.stage_risk",
    "UNIDENTIFIED_DROP_KEY": "src.stage_risk",
}


def __getattr__(name: str):
    module_path = _STAGE_CLASS_MODULES.get(name)
    if module_path is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib

    obj = getattr(importlib.import_module(module_path), name)
    globals()[name] = obj
    return obj


def __dir__():
    return sorted(set(globals()) | set(_STAGE_CLASS_MODULES))


# --- Patch mirroring. The stage classes above moved to `src/stage_*.py` and
# import their shared helpers and module-level names FROM HERE, so each stage
# module holds its own binding. Tests patch names on `src.pipeline_stages`
# (`compute_indicators`, `_persist_evidence`, `_size_shares`,
# `_record_pipeline_event`, ...) and expect the moved code to call the patched
# object. Mirroring the assignment into any stage module that already holds
# that name keeps the patched object the SAME object on both sides, which is
# what the move is required to preserve. Nothing else about the name changes.
class _StageMirroringModule(_types.ModuleType):
    def __setattr__(self, name: str, value) -> None:
        super().__setattr__(name, value)
        for module_path in set(_STAGE_CLASS_MODULES.values()):
            stage_module = _sys.modules.get(module_path)
            if stage_module is not None and name in vars(stage_module):
                setattr(stage_module, name, value)

    def __delattr__(self, name: str) -> None:
        super().__delattr__(name)
        for module_path in set(_STAGE_CLASS_MODULES.values()):
            stage_module = _sys.modules.get(module_path)
            if stage_module is not None and name in vars(stage_module):
                delattr(stage_module, name)


_sys.modules[__name__].__class__ = _StageMirroringModule
