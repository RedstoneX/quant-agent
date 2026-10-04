"""src.delever.risk_number -- the risk-number coercion helpers, moved verbatim from src/pipeline_delever.py.

Both are re-exported from `src.pipeline_delever` (and from there `src.pipeline`), so every
existing import keeps working; a test that PATCHES one must patch it where the caller resolves it.
"""


def _optional_risk_number(value) -> float | None:
    """Read an OPTIONAL numeric risk setting, or None.

    Same defensive posture as `_risk_setting` inside `TradingPipeline.__init__`
    (many tests build the pipeline against a MagicMock config, where attribute
    access auto-creates a child mock that pydantic coerces to 1.0), but for a
    setting whose absence is meaningful rather than an error — `None` lets
    `RiskConfig` apply its own documented default instead of a number nobody
    configured.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if value > 0 else None


def _risk_number(value, default: float) -> float:
    """`_optional_risk_number` with a documented fallback, for settings that
    always need a concrete number (§10.3's minimum order size)."""
    resolved = _optional_risk_number(value)
    return default if resolved is None else resolved
