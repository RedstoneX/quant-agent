"""The run context the projected-portfolio preview records its sector map onto."""

from types import SimpleNamespace


def prun(pipeline):
    """Per-run sector scope for a test pipeline; created once, reused."""
    ctx = getattr(pipeline, "_sector_run_ctx", None)
    if ctx is None:
        ctx = SimpleNamespace(symbol_sectors=None)
        pipeline._sector_run_ctx = ctx
    return ctx


def tech_buy_analyses(stops: dict[str, float] | None = None):
    """Three Tech BUYs. `stops` overrides a symbol's stop so a test can give
    candidates DELIBERATELY UNEQUAL stop distances — the whole point of
    board item 221 is that unequal stops must produce unequal preview
    sizes."""
    from src.models import TechAnalysisResult, TechReasoningChain

    _trc = TechReasoningChain(
        trend="x",
        momentum="x",
        volatility="x",
        volume="x",
        support_resistance="x",
    )
    return [
        TechAnalysisResult(
            symbol=sym,
            rating="buy",
            conviction="high",
            entry_price=100,
            stop_loss=(stops or {}).get(sym, 95),
            reference_target=110,
            support_levels=[(stops or {}).get(sym, 95)],
            resistance_levels=[110],
            setup_type="range",
            expected_horizon_sessions=10,
            reasoning="test",
            reasoning_chain=_trc,
            thesis_invalid_if="closes below support",
        )
        for sym in ("NVDA", "AMD", "AAPL")
    ]
