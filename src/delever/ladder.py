"""src.delever.ladder -- the ceiling ladder -- live de-lever price, the drawdown-resolved ceiling and the
margin-floor breach test.

Bodies moved verbatim from src/pipeline_delever.py (`DeleverMixin`), which keeps
same-named thin shims built per call. Every collaborator is an explicit keyword-only
constructor argument, so this builds and runs with no pipeline behind it. Collaborators
named after a sibling body (e.g. `_live_delever_price`) are the HOST's shim, handed in,
never a body this part owns, so no recursion guard is needed.
"""

import logging
import math

from src.delever.risk_number import _risk_number

from src.pipeline_context import RunContext

from src.risk.rules import (
    GROSS_LADDER,
    GROSS_LADDER_ALERT_PCT,
    GrossCeiling,
    distance_to_forced_liquidation_pct,
    gross_exposure,
    peak_to_trough_pct,
    resolve_gross_ceiling,
)

#: The moved code logged under `src.pipeline` before the move and still does;
#: binding the name rather than `__name__` keeps log records byte-identical.
logger = logging.getLogger("src.pipeline")


class DeleverLadder:
    """The ceiling ladder -- live de-lever price, the drawdown-resolved ceiling and the margin-floor breach test;
    standalone, built from explicit collaborators."""

    def __init__(
        self, *,
        sweep_symbol=None,
        broker=None,
        config=None,
        db=None,
    ) -> None:
        self._sweep_symbol = sweep_symbol
        self.broker = broker
        self.config = config
        self.db = db

    def _live_delever_price(
        self, symbol: str, side: str,
    ) -> tuple[float | None, float | None]:
        """Price a MUST-FILL emergency de-lever off the LIVE quote at submit
        time, never off a fixed % of a possibly-stale mark.

        The emergency de-lever exists to shed exposure NOW when the book is
        over its gross ceiling (or the cash-only account is on margin); it
        must fill regardless of how far a name has gapped. A fixed-% limit off
        a stale `current_price` is the wrong mechanism: on an 8/10/20% gap the
        limit rests ABOVE the falling market and the book stays over its
        ceiling exactly when it must come down (docs/WORK.md item 118); inside
        normal noise the same % is oversized. So this reads the CURRENT bid/ask
        and prices a MARKETABLE limit that crosses it:

          * SELL  -> a limit AT the live BID. A sell limit at/below the bid is
                     immediately marketable and fills at the bid however far
                     the name gapped, because the gap is already IN the quote.
          * COVER -> a limit AT the live ASK (the buy-side mirror).

        The returned reference price is the live MID, so the broker's
        fat-finger guard (`OUTLIER_MAX_DEVIATION`) sees a ~zero deviation
        between the limit and the reference and passes the order however far
        the live quote has moved from yesterday's mark — the guard is
        measuring the limit against a STALE mark today, which is what would
        reject a legitimately gapped fill.

        Returns ``(None, reference_or_None)`` when no usable live quote exists
        (missing/zero/non-finite bid-or-ask, or a broker/data failure) so the
        caller submits a MARKET order — the guaranteed fill, and the correct
        fallback when there is no live price to cross. No new % constant is
        introduced on either branch: the price is the live quote or the market
        itself.
        """
        bid = ask = None
        try:
            quote = self.broker.get_latest_quote(symbol) or {}
            raw_bid = quote.get("bid_price")
            raw_ask = quote.get("ask_price")
            if raw_bid is not None:
                b = float(raw_bid)
                bid = b if math.isfinite(b) and b > 0 else None
            if raw_ask is not None:
                a = float(raw_ask)
                ask = a if math.isfinite(a) and a > 0 else None
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "de-lever: live quote unusable for %s (%s) — falling back to a "
                "MARKET order, the guaranteed fill", symbol, exc,
            )
            bid = ask = None
        if bid is not None and ask is not None:
            mid: float | None = (bid + ask) / 2
        elif bid is not None:
            mid = bid
        elif ask is not None:
            mid = ask
        else:
            mid = None
        # COVER is a BUY (side='buy'); cross the ASK. Everything else is a
        # SELL; cross the BID. A missing side of the book -> None -> MARKET.
        limit = ask if side == "buy" else bid
        return limit, mid

    def _resolve_gross_ceiling(self, ctx: RunContext):
        """Resolve this session's gross-exposure ceiling from ACCOUNT STATE.

        Nothing the Portfolio Manager produced is an input, and this returns
        a correct ceiling on a run where the PM returned nothing at all. That
        is deliberate: a blank/truncated model response is a measured failure
        mode, and a ceiling that needed a parseable book would leave the desk
        fully levered at exactly the moment it should be shedding exposure.

        Also records the state on `ctx.leverage` for the morning alert and
        the dashboard, including distance-to-forced-liquidation — which
        nothing in this codebase watched before §11.2.
        """
        risk_cfg = getattr(getattr(self, "config", None), "risk", None)
        base_x = _risk_number(getattr(risk_cfg, "max_gross_exposure_x", None), 2.0)
        maintenance_pct = _risk_number(
            getattr(risk_cfg, "maintenance_margin_pct", None), 25.0,
        )
        # Guard 2 (2026-09-02 operational safety guard): a non-finite
        # CURRENT equity read must not fall through to
        # `peak_to_trough_pct`'s "unmeasurable" branch, which
        # `resolve_gross_ceiling` resolves to the STANDING (loosest) cap.
        # That branch is correct for a genuinely fresh account with no
        # equity curve yet (see `resolve_gross_ceiling`'s docstring). Alpaca
        # has been observed to return NaN portfolio_value during market-open
        # glitches, and
        # holding the loosest cap on exactly that kind of broken-snapshot
        # day is the failure this guard closes: halting new risk (the
        # ladder's own floor rung) is safer than assuming zero drawdown.
        #
        # CORRECTION 2026-09-18. This comment used to assert, "by
        # inspection", that `peak_to_trough_pct` returns None ONLY when
        # today's own reading is unusable — that an empty history always
        # produced a real 0.0. That was accurate about the code and wrong
        # about safety: it meant a wiped `daily_pnl` table read as a book at
        # record highs and silently held the loosest cap. `peak_to_trough_pct`
        # now returns None when there is no usable PRIOR reading (its Guard
        # 3), so "unknown" is reachable in production from a lost equity
        # curve as well as from a bad read, and `resolve_gross_ceiling` now
        # alerts the owner in that state instead of staying silent. The two
        # paths still differ on purpose, and the difference is the point:
        # a BAD READ (below) is a book of unknown depth that already exists,
        # so it drops to the floor rung; an ABSENT CURVE may be a genuinely
        # fresh account that never fell, so it holds the standing cap and
        # trims nothing. Both now alert.
        total_value = ctx.total_value
        bad_equity_read = (
            isinstance(total_value, bool)
            or not isinstance(total_value, (int, float))
            or not math.isfinite(float(total_value))
        )
        if bad_equity_read:
            floor_x = GROSS_LADDER[-1][1]
            if base_x > 0:
                floor_x = min(base_x, floor_x)
            logger.warning(
                "§11.2: current equity read is non-finite (%r) — forcing "
                "the gross-exposure ceiling to its floor rung (%.1fx) and "
                "alerting the owner instead of assuming zero drawdown.",
                total_value, floor_x,
            )
            ceiling = GrossCeiling(
                ceiling_x=floor_x, base_x=base_x, drawdown_pct=None,
                alert_owner=True, rung="bad_read",
                reason=(
                    f"Current equity read came back non-finite "
                    f"({total_value!r}) — a documented Alpaca market-open "
                    f"glitch, not a fresh account. The book's drawdown "
                    f"cannot be verified, so gross exposure is held to the "
                    f"floor rung ({floor_x:.1f}x) until a valid read "
                    f"arrives."
                ),
            )
        else:
            drawdown_pct = None
            performance = ctx.recent_performance or {}
            if "peak_to_trough_pct" in performance:
                drawdown_pct = performance.get("peak_to_trough_pct")
            else:
                # The preamble runs before DecisionStage populates
                # `recent_performance`, so read the equity curve directly. One
                # cheap local DB read; a failure degrades to "unknown drawdown",
                # which resolves to the standing cap and trims nothing — never
                # to a wrong number that reads as "no drawdown".
                try:
                    rows = self.db.get_daily_pnl(limit=252)
                    drawdown_pct = peak_to_trough_pct(
                        [r.get("total_value") for r in (rows or [])], ctx.total_value,
                    )
                except Exception as e:  # noqa: BLE001
                    logger.warning(
                        "§11.2: could not read the equity curve for the drawdown "
                        "ladder — holding the standing %.1fx ceiling and trimming "
                        "nothing: %s", base_x, e,
                    )
            ceiling = resolve_gross_ceiling(drawdown_pct, base_x=base_x)
        gross = gross_exposure(
            ctx.positions, cash_park_symbol=self._sweep_symbol(),
        )
        equity = ctx.total_value if ctx.total_value else 0.0
        ctx.leverage = {
            "gross_usd": gross,
            "gross_x": (gross / equity) if equity > 0 else None,
            "ceiling_x": ceiling.ceiling_x,
            "base_ceiling_x": ceiling.base_x,
            "drawdown_pct": ceiling.drawdown_pct,
            "rung": ceiling.rung,
            "alert_owner": ceiling.alert_owner,
            # The level the alert fired at, carried so the owner message
            # can state it instead of restating a literal that goes stale
            # the day the sourced threshold moves (board item 182).
            "alert_pct": GROSS_LADDER_ALERT_PCT,
            "reason": ceiling.reason,
            "distance_to_forced_liquidation_pct":
                distance_to_forced_liquidation_pct(
                    gross, equity, maintenance_margin_pct=maintenance_pct,
                ),
        }
        return ceiling

    def _is_margin_floor_breach(self, ctx: RunContext) -> bool:
        """Is the book levered beyond the margin buffer the BASE cap preserves?

        The ratified base leverage cap (`RiskConfig.max_gross_exposure_x`) is
        chosen to keep a fixed distance to a broker forced liquidation
        (`distance_to_forced_liquidation_pct`: 33.3% at the 2.0x/25% default).
        The floor fires when the book's ACTUAL distance is worse than that
        engineered distance — a true margin-proximity breach, defined off two
        already-ratified numbers (the base cap and the maintenance margin), so
        no new threshold is introduced. Returns False whenever either figure
        is unmeasurable, so an unreadable snapshot never provokes an emergency
        trim on the morning lane (the midday/close full-ceiling pass, which is
        not floor-scoped, still fails closed on its own terms).
        """
        leverage = ctx.leverage or {}
        distance = leverage.get("distance_to_forced_liquidation_pct")
        base_x = leverage.get("base_ceiling_x")
        equity = ctx.total_value if ctx.total_value else 0.0
        if not isinstance(distance, (int, float)) or not isinstance(base_x, (int, float)):
            return False
        if equity <= 0:
            return False
        maintenance_pct = _risk_number(
            getattr(getattr(self, "config", None), "risk", None)
            and getattr(self.config.risk, "maintenance_margin_pct", None),
            25.0,
        )
        base_distance = distance_to_forced_liquidation_pct(
            base_x * equity, equity, maintenance_margin_pct=maintenance_pct,
        )
        if not isinstance(base_distance, (int, float)):
            return False
        return distance < base_distance
