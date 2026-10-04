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
from src.sentinel.order_attempts import record_order_attempt_from_event
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
from src.storage.event_journal import DatabaseEventJournal, pipeline_event_fields
# Step 11 of docs/PIPELINE_SPLIT_PLAN.md (board item 210): these bodies moved
# out verbatim and are re-exported here so every original import path and
# every `monkeypatch.setattr(pipeline_stages, ...)` patch target is unchanged.
from src.pipeline_earnings_quality import (  # noqa: F401
    _EARNINGS_DATA_QUALITY_RED_FLAGS, _EARNINGS_NOT_DISCLOSED,
    _EARNINGS_XBRL_COMPARABLE_FIELDS, _EARNINGS_XBRL_DOLLAR_FLOOR,
    _EARNINGS_XBRL_EPS_FLOOR, _EARNINGS_XBRL_TOLERANCE_PCT, _FIGURE_RE,
    _UNIT_MULTIPLIERS, _classify_earnings_status,
    _earnings_analysis_has_real_figures, _earnings_data_quality_flags_problem,
    _earnings_field_disclosed, _earnings_xbrl_mismatch_fields,
    _parse_reported_figure,
)
from src.pipeline_sizing import (  # noqa: F401
    _DEFAULT_RISK_BUDGET_PCT, _entry_deployment_budget,
    _execution_payoff_skip_reason, _fmt_shares, _fractional_sizing_allowed,
    _min_order_usd, _qty_by_risk_budget, _risk_budget_pct, _single_name_execution_cap,
    _size_shares,
)

# A test that patches a moved name on THIS module must reach the object the
# moved code actually calls, which lives in the owning module. Without the
# write-through below the patch would rebind only this module's copy and
# silently no-op (e.g. `_size_shares`, called from `_qty_by_risk_budget`).
import sys as _sys  # noqa: E402
import types as _types  # noqa: E402

from src import pipeline_earnings_quality as _pipeline_earnings_quality  # noqa: E402
from src import pipeline_sizing as _pipeline_sizing  # noqa: E402

_MOVED_NAME_OWNERS = {
    **{n: _pipeline_earnings_quality for n in vars(_pipeline_earnings_quality)
       if not n.startswith("__")},
    **{n: _pipeline_sizing for n in vars(_pipeline_sizing)
       if not n.startswith("__")},
}
for _n in ("logging", "math", "re", "annotations", "logger",
           "gap_adjusted_risk_per_share"):
    _MOVED_NAME_OWNERS.pop(_n, None)

# The single `__getattr__`/write-through mirror lives at the END of this module
# and covers BOTH move sets. Defining a second one here would be shadowed by it
# and would silently stop mirroring the sizing / earnings-quality names.
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


def _persist_evidence(db: "Database", *, run_id: str, agent_name: str, kind: str,
                       scope: str, evidence_json: str, symbol: str | None = None,
                       decision_id: str | None = None) -> None:
    """Best-effort Stage 4 structured-evidence write — NEVER raises.

    Conversion step 6: a compatibility shim over the `EventJournal` port
    (`src.ports.event_journal`); the body lives in
    `src.storage.event_journal.DatabaseEventJournal.persist_evidence`. Kept
    so the existing call sites work unchanged until each service takes a
    `journal: EventJournal` in its constructor.
    """
    DatabaseEventJournal(db).persist_evidence(
        run_id=run_id, agent_name=agent_name, kind=kind, scope=scope,
        evidence_json=evidence_json, symbol=symbol, decision_id=decision_id,
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
    """Append one typed lifecycle fact to the existing evidence stream.
    Conversion step 6: shim over `EventJournal.record_pipeline_event`. Routes
    through this module's `_persist_evidence` on purpose, so a test that
    patches that name still sees every event, exactly as before.
    """
    if stage == "order": record_order_attempt_from_event(db=pipeline.db, symbol=symbol, outcome=outcome, reason=reason, run_id=ctx.run_id, details=details)  # one Sentinel attempt row per order event
    _persist_evidence(pipeline.db, **pipeline_event_fields(
        run_id=ctx.run_id, decision_id=ctx.decision_id, symbol=symbol,
        stage=stage, outcome=outcome, reason=reason, details=details,
    ))


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
    "_ROTATION_SELL_REFUSED": "src.pipeline_rotation_exec",
    "_rotation_execution_enabled": "src.pipeline_rotation_exec",
    "_rotation_ranked_margin_enabled": "src.pipeline_rotation_exec",
    "_rotation_skip": "src.pipeline_rotation_exec",
    "_record_rotation_precheck": "src.pipeline_rotation_exec",
    "_apply_rotation_execution": "src.pipeline_rotation_exec",
    "_projected_sale_qty": "src.pipeline_rotation_exec",
    "_projected_post_sale_book": "src.pipeline_rotation_exec",
    "_scaled_position": "src.pipeline_rotation_exec",
    "_projected_post_sale_cash": "src.pipeline_rotation_exec",
    "_projected_entry_cost": "src.pipeline_rotation_exec",
    "_rotation_buy_leg_projected_refusal": "src.pipeline_rotation_exec",
    "_pending_cover_symbols": "src.pipeline_rotation_exec",
    "_rotation_sell_last": "src.pipeline_rotation_exec",
    "_rotation_sell_gate": "src.pipeline_rotation_exec",
    "_rotation_ranked_margin_sell_reason": "src.pipeline_rotation_exec",
    "_alert_rotation_executed": "src.pipeline_rotation_exec",
    "_drop_buys_sold_today_below_bar": "src.pipeline_rotation_exec",
    "_drop_rotation_buy_if_room_not_freed": "src.pipeline_rotation_exec",
    "_record_rotation_buy_leg_outcome": "src.pipeline_rotation_exec",
    "_alert_rotation_buy_leg_missing": "src.pipeline_rotation_exec",
    "_WAL_REPEG_SENTINEL": "src.pipeline_entry_orders",
    "_IN_FLIGHT_FILL_STATUSES": "src.pipeline_entry_orders",
    "_REPEG_OUTCOME_TEXT": "src.pipeline_entry_orders",
    "_entry_slippage_bps": "src.pipeline_entry_orders",
    "_trade_updates_already_started": "src.pipeline_entry_orders",
    "_fill_stream_enabled": "src.pipeline_entry_orders",
    "_known_entry_submit_budget_s": "src.pipeline_entry_orders",
    "_encode_entry_submit_window": "src.pipeline_entry_orders",
    "_submit_window_overrun": "src.pipeline_entry_orders",
    "_start_trade_updates_early": "src.pipeline_entry_orders",
    "_stop_trade_updates": "src.pipeline_entry_orders",
    "_pin_approved_entry_ceilings": "src.pipeline_entry_orders",
    "_adopt_stream_stall": "src.pipeline_entry_orders",
    "_warm_trade_updates": "src.pipeline_entry_orders",
    "_today_order_price": "src.pipeline_entry_orders",
    "_live_fill_price": "src.pipeline_entry_orders",
    "_repeg_settings": "src.pipeline_entry_orders",
    "_repeg_entry_order": "src.pipeline_entry_orders",
    "_alert_owner_entry_cancelled": "src.pipeline_entry_orders",
    "_alert_unmeasurable_symbols": "src.pipeline_entry_orders",
    "_session_candidate_ranking": "src.pipeline_entry_orders",
    "_dropped_since_proposal": "src.pipeline_entry_orders",
    "_record_constructor_drops": "src.pipeline_entry_orders",
    "_record_constructor_side_flips": "src.pipeline_entry_orders",
    "_record_realised_sector_weights": "src.pipeline_sector_weights",
    "_apply_repeg": "src.pipeline_entry_orders",
    "_repoint_trade": "src.pipeline_entry_orders",
    "_delete_repeg_wal": "src.pipeline_entry_orders",
}

# Step 11's moved names (sizing + earnings quality) join the SAME dict, so one
# `__getattr__` and one write-through `__setattr__` serve every moved name.
_STAGE_CLASS_MODULES.update(
    {_name: _owner.__name__ for _name, _owner in _MOVED_NAME_OWNERS.items()}
)


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
