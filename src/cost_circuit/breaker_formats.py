"""src.cost_circuit.breaker_formats -- thin shims; bodies moved verbatim to src/cost_circuit/parts/alert_formats.py."""
from __future__ import annotations
from src.cost_circuit.parts.alert_formats import AlertFormats


class _BreakerFormatsMixin:
    @staticmethod
    def format_auto_reset_alert(*args, **kwargs):
        """Thin shim: body moved to src/cost_circuit/parts/alert_formats.py."""
        return AlertFormats().format_auto_reset_alert(*args, **kwargs)

    @staticmethod
    def format_quota_alert(*args, **kwargs):
        """Thin shim: body moved to src/cost_circuit/parts/alert_formats.py."""
        return AlertFormats().format_quota_alert(*args, **kwargs)

    @staticmethod
    def format_recovery_alert(*args, **kwargs):
        """Thin shim: body moved to src/cost_circuit/parts/alert_formats.py."""
        return AlertFormats().format_recovery_alert(*args, **kwargs)

    @staticmethod
    def format_alert(*args, **kwargs):
        """Thin shim: body moved to src/cost_circuit/parts/alert_formats.py."""
        return AlertFormats().format_alert(*args, **kwargs)
