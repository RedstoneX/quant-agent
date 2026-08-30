import logging
from dataclasses import dataclass
from src.config import RiskConfig
from src.models import TradeDecision, Position

logger = logging.getLogger(__name__)

# Leveraged/inverse ETF multipliers for effective exposure calculation.
# Negative = inverse/short (hedge-like against the underlying index).
_ETF_LEVERAGE = {
    "SH": -1.0,    # -1x S&P 500
    "SDS": -2.0,   # -2x S&P 500
    "PSQ": -1.0,   # -1x Nasdaq 100
    "SQQQ": -3.0,  # -3x Nasdaq 100
    "DRAM": 1.0,   # 1x (normal ETF, no adjustment)
    "SMH": 1.0,
}


def _effective_multiplier(symbol: str) -> float:
    """Signed exposure multiplier (negative for inverse ETFs).

    Used for net directional exposure — hedges cancel out.
    """
    return _ETF_LEVERAGE.get(symbol, 1.0)


def _gross_multiplier(symbol: str) -> float:
    """Unsigned leverage magnitude.

    Used for per-symbol and per-sector size limits where direction doesn't matter
    (a 3x ETF still consumes 3x notional regardless of long/short bias).
    """
    return abs(_ETF_LEVERAGE.get(symbol, 1.0))


DRAWDOWN_BUY_SCALE = 0.5
"""Multiplier applied to every new BUY while the system is in drawdown.

`config/prompts/portfolio_manager.md` has instructed the LLM to halve new BUYs
whenever `in_drawdown=true` since the rule was written, and
`config/prompts/risk_manager.md` told the Risk Manager it was "the only check"
because no deterministic code enforced it. A safety rule that depends on a
language model remembering to apply it is not a rule (audit §1.1), so the
halving now lives here, in Python, and the PM prompt no longer pre-applies it —
two independent halvings would quarter the position.
"""


def apply_drawdown_scale(
    decisions: list[TradeDecision], in_drawdown: bool,
) -> tuple[list[TradeDecision], list[str]]:
    """Halve every BUY's (and Stage-3 SHORT's) allocation while in drawdown.

    Returns `(decisions, notes)`. Mutates each scaled decision in place and
    appends provenance to its `reasoning`: the AI Risk Manager audits
    CONSTRUCTED orders against PM's prose, and an unexplained size
    difference reads to it as PM contradicting itself — on 2026-08-20
    exactly that mismatch drew a full-plan veto over deterministic math
    (see `portfolio_constructor.py` `cap_note`).

    SELL, COVER and HOLD are untouched: de-risking (in either direction)
    during a drawdown is the point.
    """
    if not in_drawdown:
        return decisions, []
    notes: list[str] = []
    for decision in decisions:
        # Stage 3: a SHORT opens new risk exactly as a BUY does, so the
        # drawdown-halve applies to it too. SELL, COVER and HOLD stay
        # untouched — de-risking (in either direction) during a drawdown is
        # the point.
        if decision.action not in ("BUY", "SHORT") or decision.allocation_pct <= 0:
            continue
        before = decision.allocation_pct
        after = round(before * DRAWDOWN_BUY_SCALE, 2)
        if after <= 0:
            # Rounds to nothing — the halved trade is not worth submitting.
            decision.allocation_pct = 0.0
            notes.append(
                f"{decision.symbol} {before:.2f}% → 0% (halved below the "
                f"minimum tradable size by the drawdown rule)"
            )
            continue
        decision.allocation_pct = after
        decision.reasoning = (
            decision.reasoning
            + f" [risk engine: {before:.2f}% halved to {after:.2f}% — system "
              f"in_drawdown=true. Deterministic, not PM inconsistency.]"
        )[:800]
        notes.append(f"{decision.symbol} {before:.2f}% → {after:.2f}%")
    if notes:
        logger.warning(
            "Drawdown gate: halved %d BUY(s) — %s", len(notes), "; ".join(notes),
        )
    return decisions, notes


@dataclass
class RiskViolation:
    rule: str
    message: str
    value: float
    limit: float


class RiskRuleEngine:
    def __init__(self, config: RiskConfig):
        self.config = config

    def check(self, decision: TradeDecision, positions: list[Position],
              total_value: float, daily_pnl: float,
              pending_investment: float = 0.0,
              pending_sector_investment: dict[str, float] | None = None,
              pending_symbol_investment: dict[str, float] | None = None,
              baseline: float | None = None,
              correlation_matrix: dict[str, dict[str, float]] | None = None,
              max_correlated_cluster_pct: float = 50.0,
              cash: float | None = None,
              pending_cash_outflow: float = 0.0,
              in_drawdown: bool = False,
              pending_gross_bearish_investment: float = 0.0) -> list[RiskViolation]:
        # D10 (Stage 3): a COVER can never be hard-blocked, mirroring the
        # deliberate asymmetry already used for exits — entries fail
        # closed, exits fail open, because being unable to close a
        # position is strictly worse than being unable to open one. A
        # COVER is mechanically a buy at the broker, so without this it
        # would be caught by the cash_only rule below exactly like a BUY.
        if decision.action in ("SELL", "COVER"):
            return []
        # total_value <= 0 (or NaN) means we can't compute risk percentages.
        # Pre-fix the early return was `[]` which has the same shape as
        # "all checks passed" — so an Alpaca portfolio_value=0 blip during
        # market-open silently approved every BUY, bypassing cash_only /
        # max_position_pct / max_sector_pct / max_daily_loss_pct. Emit a
        # synthetic violation in HARD_BLOCK_RULES so the pipeline filter
        # blocks the BUY instead. The empty list reserved exclusively for
        # "checked, found no violations" semantics.
        import math
        if not math.isfinite(total_value) or total_value <= 0:
            return [RiskViolation(
                rule="max_total_position_pct",   # in HARD_BLOCK_RULES
                message=(
                    f"total_value={total_value} is not a valid equity figure "
                    f"(broker glitch or fresh account) — refusing to risk-check "
                    f"BUY for {decision.symbol}; blocking until next snapshot"
                ),
                value=0.0,
                limit=0.0,
            )]

        # Daily-loss denominator: yesterday-close equity if provided, else current equity.
        # The fallback is only intended for first-day / fresh-account cases where Alpaca
        # legitimately has no last_equity. On an established account a missing baseline
        # usually signals a broker API glitch, so log a warning — the denominator silently
        # flipping from yesterday-close to current equity can make the loss cap appear
        # stricter (or more permissive) than intended within a single session.
        if baseline is None or baseline <= 0:
            logger.warning(
                "daily-loss baseline missing (%s); falling back to current total_value=%.2f",
                baseline, total_value,
            )
            baseline = total_value

        # A single non-finite position market_value poisons every sum below.
        # NaN comparisons are all False, so `sector_pct > cap` and
        # `total_pct > cap` silently evaluate False — the exposure and sector
        # caps switch OFF for the whole session on exactly the broken-snapshot
        # day they matter most (2026-07-16 audit; Alpaca has been observed to
        # return NaN market_value during market-open glitches). Block instead,
        # mirroring the total_value guard above: no risk-check, no BUY.
        bad_mv = [p.symbol for p in positions if not math.isfinite(p.market_value)]
        if bad_mv:
            return [RiskViolation(
                rule="max_total_position_pct",   # in HARD_BLOCK_RULES
                message=(
                    f"non-finite market_value for {', '.join(sorted(bad_mv))} — "
                    f"exposure / sector caps cannot be computed; refusing to "
                    f"risk-check BUY for {decision.symbol}; blocking until the "
                    f"next clean snapshot"
                ),
                value=0.0,
                limit=0.0,
            )]

        # Non-finite cash disables the cash_only comparison the same silent
        # way a NaN market_value disabled the caps (audit round 2:
        # `NaN < 0` is False, so every BUY passed). Fail closed.
        if cash is not None and not math.isfinite(cash):
            return [RiskViolation(
                rule="max_total_position_pct",   # in HARD_BLOCK_RULES
                message=(
                    f"non-finite cash={cash} — cash_only cannot be evaluated; "
                    f"refusing to risk-check BUY for {decision.symbol}; "
                    f"blocking until the next clean snapshot"
                ),
                value=0.0,
                limit=0.0,
            )]

        violations = []
        is_short = decision.action == "SHORT"
        signed_mul = _effective_multiplier(decision.symbol)  # net direction
        gross_mul = _gross_multiplier(decision.symbol)       # size magnitude
        new_investment = total_value * (decision.allocation_pct / 100)
        # A SHORT moves net exposure the OPPOSITE way a BUY of the same
        # symbol would (it adds negative, not positive, directional
        # exposure) — flip the sign so rule 2 below stays correct instead
        # of reading a growing short as growing long exposure.
        signed_new = new_investment * signed_mul * (-1.0 if is_short else 1.0)
        gross_new = new_investment * gross_mul

        # 1. Single position size limit (gross — a 3x ETF consumes 3x regardless of direction)
        #
        # SKIPPED for SHORT. This rule's arithmetic assumes `new_investment`
        # (always a positive magnitude) moves the position FURTHER in the
        # direction `current_symbol_raw` is already signed toward — true for
        # a BUY adding to a long (both positive, they sum), but wrong for a
        # SHORT adding to a short: `current_symbol_raw` is negative (a held
        # short's market_value), so `current_symbol_raw + new_investment`
        # OFFSETS toward zero instead of growing, understating a growing
        # short as shrinking and never tripping the cap. D9's
        # `max_single_short_pct` below is the correct, direction-aware
        # replacement for a SHORT — deliberately a tighter ceiling, not the
        # same one.
        if not is_short:
            current_symbol_raw = sum(p.market_value for p in positions if p.symbol == decision.symbol)
            current_symbol_raw += (pending_symbol_investment or {}).get(decision.symbol, 0.0)
            position_pct = (current_symbol_raw + new_investment) * gross_mul / total_value * 100
            if position_pct > self.config.max_position_pct:
                violations.append(RiskViolation(
                    rule="max_position_pct",
                    message=f"{decision.symbol} position would be {position_pct:.1f}% and exceed max {self.config.max_position_pct}%",
                    value=position_pct,
                    limit=self.config.max_position_pct,
                ))

        # D9 (Stage 3): the single-short notional cap. HARD BLOCK — in
        # HARD_BLOCK_RULES (src/pipeline.py) — on opening/adding a short;
        # never reached for a COVER (exempted at the top of this method) or
        # a BUY (guarded by `is_short` here — see the note below for why
        # this one, unlike the gross ceiling just after it, stays
        # short-only).
        if is_short:
            current_short_raw = sum(
                p.market_value for p in positions
                if p.symbol == decision.symbol and p.qty < 0
            )
            # `pending_symbol_investment` (like `new_investment`) is always
            # an UNSIGNED dollar magnitude — see the accumulation in
            # `TradingPipeline._filter_hard_risk_decisions`, the same
            # convention a same-batch BUY already uses. `current_short_raw`
            # is the only signed term here (a short's market_value is
            # negative), so it alone needs `abs()`.
            pending_same_symbol = (pending_symbol_investment or {}).get(decision.symbol, 0.0)
            single_short_pct = (
                (abs(current_short_raw) + pending_same_symbol + new_investment)
                * gross_mul / total_value * 100
            )
            if single_short_pct > self.config.max_single_short_pct:
                violations.append(RiskViolation(
                    rule="max_single_short_pct",
                    message=(
                        f"{decision.symbol} short would be {single_short_pct:.1f}% "
                        f"and exceed max {self.config.max_single_short_pct}% "
                        f"(half the {self.config.max_position_pct:.0f}% long "
                        f"single-name ceiling — a short's loss is unbounded)"
                    ),
                    value=single_short_pct,
                    limit=self.config.max_single_short_pct,
                ))
            # Deliberately NOT extended to a BUY of an inverse ETF, even
            # though such a BUY is bearish exposure and IS gated by the
            # gross ceiling just below. `max_single_short_pct` sits at half
            # of `max_position_pct` specifically because a SHORT's loss is
            # unbounded — a squeeze has no floor the way a long's does at
            # -100%. An inverse-ETF LONG's loss is bounded at the position's
            # notional exactly like any other long, so it does not earn
            # that extra-tight treatment; it stays governed by the ordinary
            # `max_position_pct` (rule 1 above), which already charges it at
            # its full gross leverage multiple.

        # Gross BEARISH exposure ceiling. HARD BLOCK — in HARD_BLOCK_RULES
        # (src/pipeline.py). Renamed from the old `max_short_gross_pct`
        # (2026-08-30) when it was widened to see inverse-ETF LONGs as
        # bearish exposure — and corrected again the same day for the
        # mirror-image error that first widening introduced: `is_short`
        # alone is NOT "bearish". Shorting a -3x fund like SQQQ is a
        # BULLISH bet — it profits when SQQQ falls, which is when the
        # index it inverts RISES — so gating on `decision.action ==
        # "SHORT"` charged a bullish position against the bearish ceiling.
        # `signed_new` (computed above; the same expression rule 2's net-
        # exposure check already relies on) is directionally correct in
        # all four quadrants:
        #   BUY   AAPL -> +new_investment    (bullish, excluded)
        #   BUY   SQQQ -> -3*new_investment  (bearish, INCLUDED)
        #   SHORT AAPL -> -new_investment    (bearish, INCLUDED)
        #   SHORT SQQQ -> +3*new_investment  (bullish, excluded)
        # so both the gate and the contribution key off ITS sign, not off
        # `decision.action` or a hardcoded ticker list — a fund added to
        # `_ETF_LEVERAGE` later is picked up automatically, in whichever
        # direction its sign implies.
        if signed_new < 0:
            # Same unified rule for the held book: a position's signed
            # bearish exposure is its (already-signed) market_value times
            # its signed multiplier; a negative product is bearish, and
            # `abs(...)` of it is what it costs against the ceiling. A
            # held SHORT of an ordinary name (negative mv * +1 mult) is
            # negative -> counted. A held LONG inverse ETF (positive mv *
            # negative mult) is negative -> counted. A held SHORT of an
            # INVERSE ETF (negative mv * negative mult) is POSITIVE ->
            # NOT counted — it's bullish exposure, same as a held LONG of
            # an ordinary name.
            current_gross_bearish = sum(
                abs(p.market_value * _effective_multiplier(p.symbol))
                for p in positions
                if p.market_value * _effective_multiplier(p.symbol) < 0
            )
            # `pending_gross_bearish_investment` is the running total of
            # OTHER bearish orders — by this same signed test, not by
            # `decision.action` — already allowed earlier in this same
            # batch (see `TradingPipeline._filter_hard_risk_decisions`) —
            # without it, two bearish orders in the same run would each be
            # checked against only the pre-existing book and never see
            # each other, the same gap `pending_investment` closes for net
            # exposure and `pending_sector_investment` closes for sector.
            gross_bearish_pct = (
                (current_gross_bearish + pending_gross_bearish_investment + abs(signed_new))
                / total_value * 100
            )
            if gross_bearish_pct > self.config.max_gross_bearish_pct:
                violations.append(RiskViolation(
                    rule="max_gross_bearish_pct",
                    message=(
                        f"Total gross bearish exposure (shorts + inverse-ETF "
                        f"longs) would be {gross_bearish_pct:.1f}% and exceed "
                        f"max {self.config.max_gross_bearish_pct}%"
                    ),
                    value=gross_bearish_pct,
                    limit=self.config.max_gross_bearish_pct,
                ))

        # 1b. Drawdown gate (audit §1.1). `apply_drawdown_scale` above has
        # already halved every BUY on the normal path; this is the fail-closed
        # backstop for any path that reaches the engine unscaled. It bounds the
        # NEW money only — deliberately not the whole position, because the
        # rule the prompts have always stated is "halve every new BUY", not
        # "force-trim existing winners during a drawdown".
        if in_drawdown:
            drawdown_new_cap = self.config.max_position_pct * DRAWDOWN_BUY_SCALE
            new_pct = decision.allocation_pct * gross_mul
            if new_pct > drawdown_new_cap:
                violations.append(RiskViolation(
                    rule="drawdown_buy_cap",
                    message=(
                        f"{decision.symbol} new BUY of {new_pct:.1f}% exceeds the "
                        f"{drawdown_new_cap:.1f}% drawdown cap "
                        f"({self.config.max_position_pct:.0f}% x "
                        f"{DRAWDOWN_BUY_SCALE}) — system is in drawdown"
                    ),
                    value=new_pct,
                    limit=drawdown_new_cap,
                ))

        # 2. Total net exposure limit — signed, so long+short hedges cancel
        current_net = sum(p.market_value * _effective_multiplier(p.symbol) for p in positions)
        net_exposure = current_net + pending_investment + signed_new
        total_pct = abs(net_exposure) / total_value * 100
        if total_pct > self.config.max_total_position_pct:
            violations.append(RiskViolation(
                rule="max_total_position_pct",
                message=f"Net exposure {total_pct:.1f}% would exceed max {self.config.max_total_position_pct}%",
                value=total_pct,
                limit=self.config.max_total_position_pct,
            ))

        # 3. Daily loss limit (% of the baseline — prior close equity).
        # NaN guard mirrors check_daily_loss (line 240): a NaN daily_pnl
        # (Alpaca portfolio_value glitches propagate into
        # total_value - last_equity) makes every numeric comparison
        # False, silently disabling rule 3 inside the per-BUY pipeline
        # path. Audit 2026-05-27: standalone check_daily_loss + force-
        # delever already had the guard; this per-BUY backup path did
        # not — inconsistent defense.
        if not math.isfinite(daily_pnl):
            logger.warning(
                "RiskRuleEngine.check: daily_pnl is non-finite (%s) — "
                "skipping per-BUY daily-loss rule for %s; standalone "
                "check_daily_loss + force_delever remain in force",
                daily_pnl, decision.symbol,
            )
        else:
            daily_loss_pct = abs(daily_pnl / baseline * 100) if daily_pnl < 0 else 0
            if daily_loss_pct > self.config.max_daily_loss_pct:
                violations.append(RiskViolation(
                    rule="max_daily_loss_pct",
                    message=f"Daily loss {daily_loss_pct:.1f}% exceeds max {self.config.max_daily_loss_pct}%. Trading paused.",
                    value=daily_loss_pct,
                    limit=self.config.max_daily_loss_pct,
                ))

        # 4. Stop loss required
        if self.config.require_stop_loss and decision.stop_loss <= 0:
            violations.append(RiskViolation(
                rule="require_stop_loss",
                message=f"{decision.symbol} has no stop loss set",
                value=decision.stop_loss,
                limit=0,
            ))

        # 4b. Correlation cluster (advisory) — catches the "all-AI" concentration problem
        # that sector caps miss. If the proposed BUY plus the held positions highly correlated
        # with it (|corr| >= 0.7) together exceed max_correlated_cluster_pct, flag.
        if correlation_matrix:
            from src.data.correlation import highly_correlated_peers, CLUSTER_CORRELATION_THRESHOLD
            held_symbols = [p.symbol for p in positions]
            peers = highly_correlated_peers(decision.symbol, held_symbols, correlation_matrix)
            if peers:
                # Apply gross multiplier consistently with sector / position
                # caps below — a 3x inverse ETF (SQQQ) in a cluster consumes
                # 3x notional, even though its directional sign cancels for
                # NET exposure (#2). Pre-fix this rule treated SQQQ as 1x
                # which silently under-counted cluster concentration.
                # The cluster must include the BUY symbol's OWN existing
                # position, not just its peers: `highly_correlated_peers`
                # (correctly) excludes the symbol itself, so an ADD to the
                # largest member of a cluster counted only the ADD's notional
                # and none of the stack already held — the concentration this
                # rule exists to catch was invisible exactly when it was worst
                # (2026-07-16 audit). A symbol is trivially correlated 1.0
                # with itself, so it belongs in its own cluster total.
                cluster_symbols = set(peers) | {decision.symbol}
                peer_value = sum(
                    p.market_value * _gross_multiplier(p.symbol)
                    for p in positions if p.symbol in cluster_symbols
                )
                cluster_pct = (peer_value + gross_new) / total_value * 100
                if cluster_pct > max_correlated_cluster_pct:
                    violations.append(RiskViolation(
                        rule="correlation_cluster",
                        message=(
                            f"{decision.symbol} + correlated holdings [{', '.join(peers)}] "
                            f"would total {cluster_pct:.0f}% of book, exceeding "
                            f"{max_correlated_cluster_pct:.0f}% cluster cap (advisory). "
                            f"Pairwise corr > {CLUSTER_CORRELATION_THRESHOLD}."
                        ),
                        value=cluster_pct,
                        limit=max_correlated_cluster_pct,
                    ))

        # 4c. Cash-only policy — when allow_margin is False, no BUY may spend more
        # than the cash remaining after prior BUYs in this session. `cash` is the
        # session-start broker cash; `pending_cash_outflow` is the dollar total of
        # BUYs already allowed earlier in the same filter pass. Sector / leverage
        # multipliers don't apply here — cash is spent at gross dollar notional
        # regardless of whether the symbol is an inverse / leveraged ETF.
        #
        # SHORT is exempt: opening a short does not spend the settled-cash
        # pool this rule was written to protect — it sells borrowed shares,
        # crediting cash (against a margin requirement this codebase does
        # not model). D9's dedicated caps, not this rule, are the control
        # surface for a short (D11).
        if not self.config.allow_margin and cash is not None and not is_short:
            projected_cash = cash - pending_cash_outflow - new_investment
            if projected_cash < 0:
                violations.append(RiskViolation(
                    rule="cash_only",
                    message=(
                        f"{decision.symbol} BUY for ${new_investment:,.0f} would "
                        f"spend beyond available cash (cash=${cash:,.0f}, pending "
                        f"BUYs=${pending_cash_outflow:,.0f}); margin is disabled"
                    ),
                    value=abs(projected_cash),
                    limit=max(cash - pending_cash_outflow, 0.0),
                ))

        # 5. Sector concentration — gross (existing, pending, and new all use unsigned magnitude)
        from src.execution.broker import _get_sector
        new_sector = _get_sector(decision.symbol)
        if new_sector and new_sector != "Unknown":
            sector_value = sum(p.market_value * _gross_multiplier(p.symbol)
                               for p in positions if p.sector == new_sector)
            sector_value += (pending_sector_investment or {}).get(new_sector, 0.0)
            sector_value += gross_new
            sector_pct = sector_value / total_value * 100
            if sector_pct > self.config.max_sector_pct:
                violations.append(RiskViolation(
                    rule="max_sector_pct",
                    message=f"Sector '{new_sector}' would be {sector_pct:.1f}%, exceeds max {self.config.max_sector_pct}%",
                    value=sector_pct,
                    limit=self.config.max_sector_pct,
                ))

        return violations

    def check_daily_loss(self, baseline: float, daily_pnl: float) -> RiskViolation | None:
        """Standalone daily loss check. `baseline` is the % denominator (e.g. last_equity).

        NaN handling: any NaN in `baseline` or `daily_pnl` (Alpaca has been
        observed to return NaN for `portfolio_value` during market-open
        glitches; that propagates into `last_equity` and `daily_pnl` via
        `total_value - last_equity`) makes every comparison False, which
        would SILENTLY DISABLE the circuit breaker on exactly the kind of
        broken-snapshot day where the breaker is most valuable. So:
          - NaN baseline → can't compute %, treat as "no signal" + LOG so
            the operator knows the breaker was bypassed.
          - NaN daily_pnl → same.
        Both raise no violation but emit a WARNING; force_delever is the
        downstream safety net for the actual cash-deficit case.
        """
        import math
        if not math.isfinite(baseline):
            logger.warning(
                "check_daily_loss: baseline is non-finite (%s) — circuit "
                "breaker bypassed for this call. Likely Alpaca returned "
                "NaN portfolio_value/last_equity; force_delever is the "
                "downstream safety net.",
                baseline,
            )
            return None
        if not math.isfinite(daily_pnl):
            logger.warning(
                "check_daily_loss: daily_pnl is non-finite (%s) — circuit "
                "breaker bypassed for this call.",
                daily_pnl,
            )
            return None
        if baseline <= 0:
            return None
        daily_loss_pct = abs(daily_pnl / baseline * 100) if daily_pnl < 0 else 0
        if daily_loss_pct > self.config.max_daily_loss_pct:
            return RiskViolation(
                rule="max_daily_loss_pct",
                message=f"Daily loss {daily_loss_pct:.1f}% exceeds max {self.config.max_daily_loss_pct}%",
                value=daily_loss_pct,
                limit=self.config.max_daily_loss_pct,
            )
        return None
