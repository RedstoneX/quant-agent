"""Deterministic-layer backtest engine.

`src/replay.py` replays past LLM decisions; it is not a strategy backtest
(docs/QAMC_REMEDIATION_SPEC.md §7.1). Without one, every change to stop
placement, sizing, or the risk budget is a guess evaluated against one noisy
live day, and a bad change is indistinguishable from a bad week.

SCOPE — read this before reading a number out of this module
-------------------------------------------------------------
This engine does NOT replay the LLM agents. Their outputs are not
reproducible (same prompt, different day, different answer), so there is no
honest way to "backtest" a Portfolio Manager or Tech Analyst call. What CAN
be measured, and what nearly every recent engineering change has actually
touched, is the DETERMINISTIC layer underneath them:

  * entry timing        — `TradingPipeline._has_actionable_signal_fn`
  * structural stops     — `src/data/levels.py::find_structural_levels`
  * stop discipline      — `PortfolioConstructor._resolve_stop` /
                            `._widen_stop_past_noise` (src/portfolio_constructor.py)
  * position sizing       — the §2.1 risk formula
                            (shares = equity x risk_pct/100 / |entry - stop|)
  * the portfolio risk budget and cluster caps
                          — `src/risk/budget.py::allocate_risk_budget`
  * trailing stops        — `src/risk/trailing.py::compute_trailing_stop`

Every one of those is reused from the live modules, not reimplemented, so a
change in this tool's output is attributable to the deterministic change
under test and nothing else. The one piece with no live counterpart to call
is "which structural level would the Tech Analyst have picked as the stop":
in production an LLM chooses among the levels `find_structural_levels`
reports. This engine substitutes a fixed, deterministic rule — the nearest
support below the signal-day close for a long (nearest resistance above it
for a short) becomes the analyst's `stop_loss`, and the opposite side's
nearest level becomes `reference_target` — and then feeds that through the
REAL `_resolve_stop` / `_widen_stop_past_noise`, exactly as the live
constructor would if the analyst had reported those numbers. `setup_type`
("range" vs "breakout") is likewise substituted deterministically from
`MarketContext.is_consolidating` (src/data/context.py) rather than an LLM's
chart read. Both substitutions are declared here, not hidden in a helper.

NO-LOOK-AHEAD
-------------
A signal computed from bars through day D is only ever acted on at day D+1's
OPEN — never at day D's own close, and never using anything dated after D+1's
open. Stops and targets are resolved from information available at the close
of day D. Indicators, structural levels and trailing-stop updates for day D
use bars [.., D] inclusive, never a bar past D.

DIRECTION
---------
The engine is direction-aware end to end (long and short fills, stops,
exits, signed P&L) so a later change that lands short trading does not need
this rewritten. The historical run in this repo's current state only ever
emits LONG signals, because the live system cannot open a short yet and the
deterministic prefilter/structural-levels path this engine reuses has no
short-side entry rule to borrow (mirroring one here, with nothing live to
validate it against, is not "reuse" — it's a new invention this backtester
declined to make). The short side of the engine is exercised directly by
`tests/test_backtest.py` with synthetic trades, not by the real-data run.

Also note: `PortfolioConstructor._widen_stop_past_noise` is long-only math
(it computes `entry_price - multiple * atr`, a floor BELOW entry). It is
reused as-is for longs. For shorts, this engine applies the raw structural
stop without a noise-band widening step — production has no short-side
widening rule yet to reuse, and this tool does not invent one.

OTHER DECLARED SIMPLIFICATIONS
-------------------------------
* One open position per symbol at a time (no pyramiding).
* Correlation clusters are recomputed once per simulated day, from bars
  through that day only (no look-ahead), using the same
  `build_correlation_matrix` / `correlation_clusters` the live risk budget
  uses.
* Equity used for sizing and for the risk budget compounds with REALIZED
  trade P&L only; unrealized marks on open positions are not folded in
  (matches the trade-level max-drawdown methodology in `metrics.py`).
* macro regime is not modelled — `_widen_stop_past_noise` is called with
  `regime=None`, i.e. no regime scale is applied.
* When a day's bar could have hit BOTH the stop and the target, the engine
  assumes the STOP was hit first. Daily OHLC cannot resolve true intrabar
  sequence, and assuming the worse outcome is the conservative choice.
* A position still open when the data window ends is force-closed at the
  last available close (`exit_reason="end_of_data"`), not silently dropped.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date
from types import SimpleNamespace

from src.config import AppConfig
from src.data.context import compute_market_context
from src.data.correlation import build_correlation_matrix, correlation_clusters
from src.data.levels import find_structural_levels
from src.data.technical import compute_indicators
from src.models import OHLCV
from src.pipeline import TradingPipeline
from src.portfolio_constructor import ConstructorConfig, PortfolioConstructor
from src.risk.budget import RiskRequest, allocate_risk_budget
from src.risk.rules import _gross_multiplier
from src.risk.trailing import compute_trailing_stop

#: Trading days of history a symbol needs before this engine will evaluate it
#: for a signal. 210 = 200 (MA200) + 10 (the slope lookback `compute_market_context`
#: needs to say whether that average is rising or falling) — below this, both
#: `find_structural_levels` and `compute_market_context` are working with a
#: materially incomplete picture, and the live system would be too.
MIN_BARS_FOR_SIGNAL = 210

DEFAULT_MAX_HOLD_DAYS = 20
DEFAULT_INITIAL_EQUITY = 100_000.0
DEFAULT_SLIPPAGE_BPS = 5.0


@dataclass(frozen=True)
class BacktestParams:
    """Engine-only knobs. None of these has a live-system counterpart to
    reuse: the live horizon comes from the Tech Analyst's own
    `expected_horizon_sessions` estimate (an LLM output this engine cannot
    reproduce), and there is no dedicated backtest slippage field in
    `Settings` — see `scripts/backtest.py` for how the default is chosen."""

    start: date
    end: date
    max_hold_days: int = DEFAULT_MAX_HOLD_DAYS
    initial_equity: float = DEFAULT_INITIAL_EQUITY
    slippage_bps: float = DEFAULT_SLIPPAGE_BPS
    min_bars_for_signal: int = MIN_BARS_FOR_SIGNAL


@dataclass
class _OpenPosition:
    symbol: str
    direction: str  # "long" | "short"
    signal_date: date
    entry_date: date
    entry_index: int
    entry_price: float  # fill price (slippage-adjusted)
    stop_initial: float
    stop: float  # current (possibly trailed) stop
    target: float | None
    setup_type: str
    shares: float
    risk_pct: float


@dataclass(frozen=True)
class Trade:
    """One closed round-trip. Every price here is a FILL price (slippage
    applied) except `stop_price` / `target_price`, which are the structural
    price LEVELS the trade was managed against."""

    symbol: str
    direction: str  # "long" | "short"
    signal_date: date
    entry_date: date
    entry_price: float
    stop_price: float
    target_price: float | None
    exit_date: date
    exit_price: float
    exit_reason: str  # "stop" | "target" | "horizon" | "end_of_data"
    shares: float
    risk_pct: float
    setup_type: str
    hold_days: int
    pnl: float
    r_multiple: float


@dataclass(frozen=True)
class BacktestRunResult:
    trades: list[Trade]
    skipped_symbol_days: int
    symbols_used: list[str]
    symbols_with_no_data: list[str]
    params: BacktestParams
    initial_equity: float
    final_equity: float


def _fill_price(raw_price: float, direction: str, side: str, slippage_bps: float) -> float:
    """Apply a flat slippage assumption in the ADVERSE direction only.

    `side` is "open" (establishing the position) or "close" (exiting it).
    Buying always costs slippage; selling always gives it up. A long open
    and a short close are both buys; a long close and a short open are both
    sells.
    """
    frac = slippage_bps / 10_000.0
    buying = (direction == "long" and side == "open") or (direction == "short" and side == "close")
    return raw_price * (1 + frac) if buying else raw_price * (1 - frac)


def _setup_type_for(bars_through_signal: list[OHLCV]) -> str:
    """Deterministic substitute for the analyst's chart read (see module
    docstring). `is_consolidating` is the measurable half of "range or
    breakout" that `src/data/context.py` already computes."""
    ctx = compute_market_context(bars_through_signal)
    if ctx is not None and ctx.is_consolidating:
        return "range"
    return "breakout"


def _resolve_structural_stop_and_target(
    bars_through_signal: list[OHLCV], direction: str,
) -> tuple[float | None, float | None, list[float]]:
    """Nearest structural level on the protective side becomes the stop
    candidate; the nearest level on the other side becomes the reference
    target. Returns (None, None, []) when there is no level to defend a stop
    with — `find_structural_levels` already returns "no structure" honestly
    (empty lists) rather than inventing one, and this engine declines the
    signal on the same terms the live analyst is told to.

    The third element is every computed level, supports and resistances
    unioned — the same shape `TechAnalysisResult.computed_levels` carries in
    live. Spec §12.1 keys the stop rule off it, so without it this engine
    would silently measure the OLD behaviour and the parity claim in the
    module docstring would stop being true."""
    supports, resistances = find_structural_levels(bars_through_signal)
    all_levels = sorted(lv.price for lv in (*supports, *resistances))
    if direction == "long":
        if not supports:
            return None, None, []
        stop = max(lv.price for lv in supports)  # nearest support below close
        target = min((lv.price for lv in resistances), default=None)
    else:
        if not resistances:
            return None, None, []
        stop = min(lv.price for lv in resistances)  # nearest resistance above close
        target = max((lv.price for lv in supports), default=None)
    return stop, target, all_levels


def _resolve_stop_for_signal(
    constructor: PortfolioConstructor,
    *,
    symbol: str,
    direction: str,
    structural_stop: float,
    target: float | None,
    atr_14: float | None,
    setup_type: str,
    ref_entry: float,
    computed_levels: list[float] | None = None,
) -> float | None:
    """Reuses `PortfolioConstructor._resolve_stop` (direction-agnostic — it
    only reads whichever of `target.suggested_stop_price` /
    `analysis.stop_loss` is supplied) and, for longs only,
    `_widen_stop_past_noise` (long-only math — see module docstring).

    `computed_levels` carries the levels §12.1's stop rule verifies against,
    the same field `TechAnalystAgent` sets in Python on the live path. This
    engine's stop candidate IS one of them, so without this the backtest
    would exercise the pre-§12.1 rule while claiming to run the real one."""
    analysis = SimpleNamespace(
        stop_loss=structural_stop, atr_14=atr_14,
        setup_type=setup_type, reference_target=target,
        computed_levels=list(computed_levels or []),
    )
    target_shim = SimpleNamespace(suggested_stop_price=None)
    stop = constructor._resolve_stop(target_shim, analysis, ref_entry)
    if direction == "long":
        stop = constructor._widen_stop_past_noise(
            symbol, analysis, ref_entry, stop, regime=None,
        )
        if stop is None or stop <= 0 or stop >= ref_entry:
            return None
    else:
        if stop is None or stop <= 0 or stop <= ref_entry:
            return None
    return stop


def _size_position(
    *, equity: float, granted_risk_pct: float, fill_entry: float,
    stop: float, symbol: str, max_position_pct: float,
) -> tuple[int, float]:
    """§2.1 formula: shares = equity x risk_pct/100 / |entry - stop|,
    clamped by the single-name notional ceiling on a GROSS-leverage basis
    (mirrors `PortfolioConstructor`'s single-name trim — see
    src/portfolio_constructor.py around the `max_position_pct` comment).
    Returns (whole shares, the risk_pct actually consumed after rounding
    down to a whole share and after any clamp)."""
    risk_per_share = abs(fill_entry - stop)
    if risk_per_share <= 0 or granted_risk_pct <= 0 or equity <= 0:
        return 0, 0.0
    risk_dollars = equity * granted_risk_pct / 100.0
    shares = risk_dollars / risk_per_share
    notional = shares * fill_entry
    gross_mul = _gross_multiplier(symbol)
    gross_notional = notional * gross_mul
    max_notional = equity * max_position_pct / 100.0
    if max_notional > 0 and gross_notional > max_notional:
        shares *= max_notional / gross_notional
    shares_int = math.floor(shares)
    if shares_int < 1:
        return 0, 0.0
    effective_risk_pct = shares_int * risk_per_share / equity * 100.0
    return shares_int, effective_risk_pct


def _existing_risk_pct(pos: _OpenPosition, equity: float) -> float:
    """Risk % of equity an open position is currently consuming, based on
    its LIVE (possibly trailed) stop. Once a stop trails to or past entry,
    the position stops consuming budget — same behaviour `trailing.py`
    documents ("this position stops consuming risk budget")."""
    if pos.direction == "long":
        risk_per_share = max(0.0, pos.entry_price - pos.stop)
    else:
        risk_per_share = max(0.0, pos.stop - pos.entry_price)
    if equity <= 0:
        return 0.0
    return pos.shares * risk_per_share / equity * 100.0


def _check_exit(
    pos: _OpenPosition, bar: OHLCV, idx: int, max_hold_days: int,
) -> tuple[str | None, float | None]:
    """Whether today's bar closes `pos`, and at what raw (pre-slippage)
    price. Returns `(None, None)` when the position stays open.

    STOP-FIRST ORDERING: when a day's range could have touched both the
    stop and the target, the stop is assumed to have been hit first — see
    the module docstring ("EXIT ORDERING"). Factored out from the main loop
    so it is directly unit-testable for both directions without needing the
    signal-generation machinery around it.
    """
    if pos.direction == "long":
        if bar.low <= pos.stop:
            return "stop", pos.stop
        if pos.target is not None and bar.high >= pos.target:
            return "target", pos.target
    else:
        if bar.high >= pos.stop:
            return "stop", pos.stop
        if pos.target is not None and bar.low <= pos.target:
            return "target", pos.target

    hold_days = idx - pos.entry_index
    if hold_days >= max_hold_days:
        return "horizon", bar.close
    return None, None


def _close_trade(pos: _OpenPosition, exit_idx: int, exit_date_: date, raw_exit: float,
                  exit_reason: str, slippage_bps: float) -> Trade:
    fill = _fill_price(raw_exit, pos.direction, "close", slippage_bps)
    if pos.direction == "long":
        pnl = (fill - pos.entry_price) * pos.shares
    else:
        pnl = (pos.entry_price - fill) * pos.shares
    risk_per_share = abs(pos.entry_price - pos.stop_initial)
    r_multiple = pnl / (pos.shares * risk_per_share) if risk_per_share > 0 else 0.0
    return Trade(
        symbol=pos.symbol, direction=pos.direction, signal_date=pos.signal_date,
        entry_date=pos.entry_date, entry_price=round(pos.entry_price, 4),
        stop_price=round(pos.stop_initial, 4),
        target_price=round(pos.target, 4) if pos.target is not None else None,
        exit_date=exit_date_, exit_price=round(fill, 4), exit_reason=exit_reason,
        shares=pos.shares, risk_pct=round(pos.risk_pct, 4), setup_type=pos.setup_type,
        hold_days=exit_idx - pos.entry_index, pnl=round(pnl, 2),
        r_multiple=round(r_multiple, 4),
    )


def run_backtest(
    *, config: AppConfig, bars_by_symbol: dict[str, list[OHLCV]], params: BacktestParams,
) -> BacktestRunResult:
    """Run the day-by-day simulation. `bars_by_symbol` must already be
    fetched (see `src/backtest/data.py`) — this function makes no network
    calls, which is what makes it unit-testable offline."""
    constructor = PortfolioConstructor(ConstructorConfig(
        # Mirrors src/pipeline.py's ConstructorConfig wiring exactly, so a
        # change to `config.risk.*` is the same experiment here as live.
        risk_budget_pct=config.risk.max_position_risk_pct,
        min_risk_pct=config.risk.min_position_risk_pct,
        max_portfolio_risk_pct=config.risk.max_portfolio_risk_pct,
        max_cluster_risk_share_pct=config.risk.max_cluster_risk_share_pct,
        max_position_pct=config.risk.max_position_pct,
        min_stop_atr_multiple=config.risk.min_stop_atr_multiple,
        min_reward_risk_after_widening=config.risk.min_reward_risk_after_widening,
        # Spec §12.1 — a stop at a COMPUTED level is honoured whatever the
        # band says, down to a deterministic 1x ATR floor. Wired here so a
        # change to `config.risk.*` is the same experiment in the backtest
        # as it is live.
        level_match_atr_tolerance=config.risk.level_match_atr_tolerance,
        absolute_min_stop_atr_multiple=config.risk.absolute_min_stop_atr_multiple,
        # Target-derivation tunables (2026-09-01). Wired for parity with
        # live, though this engine does not reach `_derive_target`: it
        # computes its own nearest-level target in
        # `_resolve_structural_stop_and_target` and hands it to
        # `_widen_stop_past_noise` directly, which is the same rule by a
        # shorter path and was never exposed to the guessed-target defect.
        min_target_atr_multiple=config.risk.min_target_atr_multiple,
        breakout_projection_atr_multiple=config.risk.breakout_projection_atr_multiple,
        max_target_reach_atr_multiple=config.risk.max_target_reach_atr_multiple,
        max_target_horizon_sessions=config.risk.max_target_horizon_sessions,
        target_divergence_warn_pct=config.risk.target_divergence_warn_pct,
    ))

    symbols_with_data = sorted(sym for sym, bars in bars_by_symbol.items() if bars)
    symbols_with_no_data = sorted(sym for sym, bars in bars_by_symbol.items() if not bars)

    bars_sorted: dict[str, list[OHLCV]] = {
        sym: sorted(bars_by_symbol[sym], key=lambda b: b.date) for sym in symbols_with_data
    }
    index_of_date: dict[str, dict[date, int]] = {
        sym: {b.date: i for i, b in enumerate(bars_sorted[sym])} for sym in symbols_with_data
    }

    calendar = sorted({
        b.date for bars in bars_sorted.values() for b in bars
        if params.start <= b.date <= params.end
    })

    trades: list[Trade] = []
    open_positions: dict[str, _OpenPosition] = {}
    realized_pnl = 0.0
    skipped_symbol_days = 0

    for i, day in enumerate(calendar):
        # ---- 1. Exits, then trailing-stop updates, for open positions ----
        for symbol in list(open_positions):
            pos = open_positions[symbol]
            idx_map = index_of_date[symbol]
            if day not in idx_map:
                continue
            idx = idx_map[day]
            bar = bars_sorted[symbol][idx]

            exit_reason, raw_exit = _check_exit(pos, bar, idx, params.max_hold_days)

            if exit_reason is not None:
                trade = _close_trade(pos, idx, day, raw_exit, exit_reason, params.slippage_bps)
                trades.append(trade)
                realized_pnl += trade.pnl
                del open_positions[symbol]
                continue

            # Still open: propose a trail using bars SINCE ENTRY, through today.
            bars_since_entry = bars_sorted[symbol][pos.entry_index: idx + 1]
            atr_today = compute_indicators(symbol, bars_sorted[symbol][: idx + 1]).atr_14
            qty_sign = pos.shares if pos.direction == "long" else -pos.shares
            proposal = compute_trailing_stop(
                symbol=symbol, setup_type=pos.setup_type, entry=pos.entry_price,
                current_price=bar.close, current_stop=pos.stop,
                reference_target=pos.target, bars=bars_since_entry, atr=atr_today,
                qty=qty_sign,
            )
            if proposal is not None:
                pos.stop = proposal.new_stop

        # ---- 2. New entries, filled at the NEXT day's open ----
        if i + 1 >= len(calendar):
            continue  # no next-day open available; the window has ended
        next_day = calendar[i + 1]
        equity = params.initial_equity + realized_pnl

        candidates: list[dict] = []
        for symbol in symbols_with_data:
            if symbol in open_positions:
                continue
            idx_map = index_of_date[symbol]
            if day not in idx_map or next_day not in idx_map:
                continue
            idx = idx_map[day]
            bars_through_today = bars_sorted[symbol][: idx + 1]
            if len(bars_through_today) < params.min_bars_for_signal:
                skipped_symbol_days += 1
                continue

            indicators = compute_indicators(symbol, bars_through_today)
            if not TradingPipeline._has_actionable_signal_fn(
                indicators, symbol, bars_through_today, [],
            ):
                continue

            direction = "long"  # see module docstring: real-data run is long-only
            structural_stop, target, computed_levels = (
                _resolve_structural_stop_and_target(bars_through_today, direction)
            )
            if structural_stop is None:
                continue
            setup_type = _setup_type_for(bars_through_today)
            next_idx = idx_map[next_day]
            ref_entry = bars_sorted[symbol][next_idx].open
            if not ref_entry or ref_entry <= 0:
                continue
            # The target was picked relative to the SIGNAL day's close; the
            # actual entry is the NEXT day's open, which can gap past it. A
            # "target" behind the real entry is not a profit objective any
            # more (a long's target must sit above what was actually paid),
            # so drop it rather than manage the trade against a number that
            # no longer means what it says.
            if target is not None:
                if direction == "long" and target <= ref_entry:
                    target = None
                elif direction == "short" and target >= ref_entry:
                    target = None
            stop = _resolve_stop_for_signal(
                constructor, symbol=symbol, direction=direction,
                structural_stop=structural_stop, target=target,
                atr_14=indicators.atr_14, setup_type=setup_type, ref_entry=ref_entry,
                computed_levels=computed_levels,
            )
            if stop is None:
                continue
            candidates.append(dict(
                symbol=symbol, direction=direction, ref_entry=ref_entry, stop=stop,
                target=target, setup_type=setup_type, next_idx=next_idx, signal_date=day,
            ))

        if not candidates:
            continue

        hist_for_matrix = {
            sym: bars_sorted[sym][: index_of_date[sym][day] + 1]
            for sym in symbols_with_data if day in index_of_date[sym]
        }
        matrix = build_correlation_matrix(hist_for_matrix)
        cluster_universe = list(open_positions) + [c["symbol"] for c in candidates]
        clusters = correlation_clusters(cluster_universe, matrix)

        existing_pct = {
            sym: _existing_risk_pct(pos, equity) for sym, pos in open_positions.items()
        }
        requests = [
            RiskRequest(c["symbol"], config.risk.max_position_risk_pct) for c in candidates
        ]
        allocation = allocate_risk_budget(
            requests, existing_pct=existing_pct, clusters=clusters,
            ceiling_pct=config.risk.max_portfolio_risk_pct,
            cluster_share_pct=config.risk.max_cluster_risk_share_pct,
            floor_pct=config.risk.min_position_risk_pct,
        )

        for c in candidates:
            granted = allocation.granted(c["symbol"])
            if granted <= 0:
                continue
            fill_entry = _fill_price(c["ref_entry"], c["direction"], "open", params.slippage_bps)
            shares, eff_risk_pct = _size_position(
                equity=equity, granted_risk_pct=granted, fill_entry=fill_entry,
                stop=c["stop"], symbol=c["symbol"], max_position_pct=config.risk.max_position_pct,
            )
            if shares <= 0:
                continue
            open_positions[c["symbol"]] = _OpenPosition(
                symbol=c["symbol"], direction=c["direction"], signal_date=c["signal_date"],
                entry_date=next_day, entry_index=c["next_idx"], entry_price=fill_entry,
                stop_initial=c["stop"], stop=c["stop"], target=c["target"],
                setup_type=c["setup_type"], shares=shares, risk_pct=eff_risk_pct,
            )

    # ---- Force-close anything still open when the data window ends ----
    for symbol, pos in list(open_positions.items()):
        idx_map = index_of_date[symbol]
        last_idx = None
        last_day = None
        for day in reversed(calendar):
            if day in idx_map:
                last_idx, last_day = idx_map[day], day
                break
        if last_idx is None or last_idx <= pos.entry_index:
            continue  # no bar since entry to mark it against
        bar = bars_sorted[symbol][last_idx]
        trade = _close_trade(pos, last_idx, last_day, bar.close, "end_of_data", params.slippage_bps)
        trades.append(trade)
        realized_pnl += trade.pnl

    return BacktestRunResult(
        trades=trades,
        skipped_symbol_days=skipped_symbol_days,
        symbols_used=symbols_with_data,
        symbols_with_no_data=symbols_with_no_data,
        params=params,
        initial_equity=params.initial_equity,
        final_equity=round(params.initial_equity + realized_pnl, 2),
    )
