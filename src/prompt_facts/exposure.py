"""src.prompt_facts.exposure -- the correlation matrix and the live stop map.

Bodies moved verbatim from src/pipeline_prompt_facts.py (`PromptFactsMixin`), which keeps
same-named thin shims built per call. Every collaborator is an explicit keyword-only
constructor argument, so this builds and runs with no pipeline behind it.
"""

import logging

from src.execution.stop_read import read_stop

#: The moved code logged under `src.pipeline` before the move and still does;
#: binding the name rather than `__name__` keeps log records byte-identical.
logger = logging.getLogger("src.pipeline")


class PromptExposure:
    """The correlation matrix and the live stop map; standalone, built from explicit collaborators."""

    def __init__(
        self, *,
        config=None,
        market=None,
        db=None,
        broker=None,
    ) -> None:
        self.config = config
        self.market = market
        self.db = db
        self.broker = broker

    def _ensure_correlation_matrix(self, ctx, positions) -> dict:
        """Build the run's correlation matrix once, memoized on `ctx`.

        It used to be built inside `RiskStage`, which runs AFTER the Portfolio
        Manager has already chosen — so the PM's prompt could tell it to "avoid
        stacking highly correlated positions" while the only correlation data
        in the system was computed too late to inform that choice (audit §1.2).
        Building it here, from the DecisionStage side, lets PM see the clusters
        BEFORE it decides, and RiskStage reuses the same matrix rather than
        paying for a second one — the deterministic cluster check must judge
        PM against the numbers PM was actually shown.
        """
        cached = getattr(ctx, "correlation_matrix", None)
        if cached:
            return cached
        try:
            from src.data.correlation import build_correlation_matrix
            # THE CORRELATION WINDOW (board item 148), recorded honestly.
            # The bars that feed this matrix span `trading.lookback_days`
            # (deployed 1800 ≈ 5 trading years, config/settings.yaml) — the
            # SAME history fetched for MA200 and every other indicator, reused
            # here rather than chosen for correlation. It has NO correlation-
            # specific derivation: settings.yaml records the 320→1800 raise as
            # "purely for structure" (deterministic support/resistance), and
            # `build_correlation_matrix` needs only 20 overlapping daily returns
            # (`df.corr(min_periods=20)`) over pairwise-complete observations, so
            # the extra ~1,780 bars add older returns that may straddle regime
            # changes rather than sharpen a cluster estimate. The window moved
            # 120d → 5y silently on the switch to `trading.lookback_days`; this
            # comment is the reason that was never recorded — it is INHERITED
            # from the structural-level fetch, not justified for clustering.
            # It is not a ledgered number: `trading.lookback_days` carries no
            # numeric default (`Field(ge=1)` in src/config.py), so it is not a
            # definition site the number-ledger scanner can attach an entry to;
            # the 0.7 cutoff that used to sit beside it is GONE (item 186,
            # 2026-09-30): clusters are now read from the correlation
            # geometry itself, so the window is the only unjustified input
            # left on this path.
            pool_bars = dict(ctx.symbols_bars)
            for p in positions:
                if p.symbol not in pool_bars:
                    pool_bars[p.symbol] = self.market.get_ohlcv(
                        p.symbol, self.config.trading.lookback_days,
                    ) or []
            matrix = build_correlation_matrix(pool_bars) or {}
        except Exception as e:  # noqa: BLE001
            logger.warning("Failed to build correlation matrix: %s (continuing without)", e)
            matrix = {}
        ctx.correlation_matrix = matrix
        return matrix

    def _build_stop_map(self, positions) -> tuple[dict[str, float], dict[str, float], set[str]]:
        """`(live_stops, initial_stops, unreadable)` keyed by symbol.

        Live stops are broker truth (already trailed). Initial stops come from
        the last executed BUY row and are what an R-multiple's denominator must
        use — the bet that was actually made, not the one it was ratcheted to.
        A symbol missing from `live_stops` and not in `unreadable` is genuinely unprotected and
        `portfolio_heat` charges it at full notional; never substitute the BUY
        row's stop for a missing broker stop, because that would report
        protection the account does not have.
        """
        live_stops: dict[str, float] = {}
        initial_stops: dict[str, float] = {}
        unreadable: set[str] = set()
        for p in positions:
            sym = p.symbol
            _live_read = read_stop(self.broker, sym, db=self.db, context="prompt stop map")
            unreadable.update([sym] if _live_read.unreadable else [])
            if _live_read.found:
                live_stops[sym] = _live_read.price
            from src.execution.stop_records import recorded_initial_stop
            try:
                qty = float(getattr(p, "qty", 0) or 0)
            except (TypeError, ValueError):
                qty = 0.0
            try:
                opening = "SHORT" if qty < 0 else "BUY"
                buy = self.db.get_symbol_last_buy(sym, action=opening)
            except Exception as e:  # noqa: BLE001
                logger.warning("stop map: last-buy lookup failed for %s: %s", sym, e)
                buy = None
            initial = recorded_initial_stop(buy)
            if initial > 0:
                initial_stops[sym] = initial
        return live_stops, initial_stops, unreadable
